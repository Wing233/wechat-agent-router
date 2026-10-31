"""SQLite trace store for inbound messages, model calls and tool calls.

The router already logs a trace id on every line, but logs alone cannot answer
"what happened to that QQ message half an hour ago". This module keeps a small
bounded record of each request so the assistant can retrieve and explain it
through natural language tools.

The store is optional: until :func:`configure` is called every write is a no-op.
That keeps unit tests and one-off scripts from creating stray databases.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import math
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  trace_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  session TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  input TEXT NOT NULL DEFAULT '',
  output TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS traces_started ON traces(started_at);
CREATE INDEX IF NOT EXISTS traces_status ON traces(status, started_at);

CREATE TABLE IF NOT EXISTS spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ok',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',
  cost REAL NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id, id);
CREATE INDEX IF NOT EXISTS spans_ts ON spans(ts);
"""

_DB_PATH: Path | None = None
_INIT_DONE = False
_LOCK = threading.RLock()
_OUTPUT: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "trace_output", default=None)
_FAIL: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "trace_failure", default=None)


def configure(path: str | Path | None) -> None:
    """Point the store at ``path`` and create tables once."""
    global _DB_PATH, _INIT_DONE
    with _LOCK:
        _DB_PATH = Path(path) if path else None
        _INIT_DONE = False
    if _DB_PATH is not None:
        _init()


def enabled() -> bool:
    return _DB_PATH is not None


def _init() -> None:
    global _INIT_DONE
    if _DB_PATH is None or _INIT_DONE:
        return
    with _LOCK:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(_DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        _INIT_DONE = True


@contextlib.contextmanager
def _conn():
    if _DB_PATH is None:
        yield None
        return
    _init()
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _since(hours: float) -> str:
    return (datetime.now(CST) - timedelta(hours=max(0.0, float(hours)))).isoformat(
        timespec="seconds")


def _json(value: Any, limit: int = 4000) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)[:limit]


# ---------------------------------------------------------------------------
# output capture for send_text()
# ---------------------------------------------------------------------------
def begin_capture() -> contextvars.Token:
    _FAIL.set(None)
    return _OUTPUT.set([])


def mark_failed(status: str, error: str) -> None:
    """Remember a handled failure so the outer wrapper can mark the trace."""
    _FAIL.set((str(status or "error"), str(error or "")[:2000]))


def take_failure() -> tuple[str, str] | None:
    value = _FAIL.get()
    _FAIL.set(None)
    return value


def end_capture(token: contextvars.Token | None) -> str:
    text = ""
    try:
        items = _OUTPUT.get() or []
        text = "\n\n".join(str(item) for item in items if str(item).strip())
    finally:
        if token is not None:
            _OUTPUT.reset(token)
    return text

def note_output(text: str) -> None:
    items = _OUTPUT.get()
    if items is not None:
        items.append(str(text))


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
def start(trace_id: str, *, source: str = "", role: str = "", session: str = "",
          channel: str = "", user_id: str = "", input: str = "",
          meta: dict | None = None) -> None:
    """Create (or refresh) a trace row when a message starts."""
    if not trace_id or _DB_PATH is None:
        return
    stamp = _now()
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO traces(trace_id,started_at,updated_at,source,role,session,"
                "channel,user_id,status,input,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(trace_id) DO UPDATE SET updated_at=excluded.updated_at,"
                "source=excluded.source,role=excluded.role,session=excluded.session,"
                "channel=excluded.channel,user_id=excluded.user_id,input=excluded.input,"
                "meta=excluded.meta",
                (trace_id, stamp, stamp, source[:40], role[:40], session[:200],
                 channel[:60], user_id[:200], "running", input[:8000], _json(meta or {})),
            )
    except Exception:
        return


def finish(trace_id: str, *, status: str = "ok", error: str = "",
           output: str | None = None, latency_ms: int = 0) -> None:
    if not trace_id or _DB_PATH is None:
        return
    if output is None:
        output = ""
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE traces SET updated_at=?, status=?, error=?, output=?, latency_ms=? "
                "WHERE trace_id=?",
                (_now(), status[:30], error[:4000], output[:8000],
                 max(0, int(latency_ms)), trace_id),
            )
    except Exception:
        return


def span(trace_id: str, kind: str, name: str = "", *, status: str = "ok",
         latency_ms: int = 0, model: str = "", cost: float = 0.0,
         detail: Any = None) -> None:
    """Append one operation inside a trace (model call, tool call, ...)."""
    if not trace_id or _DB_PATH is None:
        return
    text = "" if detail is None else (
        detail if isinstance(detail, str) else _json(detail))
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO spans(trace_id,ts,kind,name,status,latency_ms,model,cost,detail) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (trace_id, _now(), kind[:30], name[:120], status[:30],
                 max(0, int(latency_ms)), model[:200], float(cost or 0.0), text[:8000]),
            )
    except Exception:
        return


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["meta"] = json.loads(item.get("meta") or "{}")
    except Exception:
        item["meta"] = {}
    return item


def get(trace_id: str, *, spend_path: str | None = None,
        limit_spans: int = 200) -> dict | None:
    if not trace_id:
        return None
    with _conn() as conn:
        if conn is None:
            return None
        found = _row(conn.execute(
            "SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone())
        if found is None:
            return None
        spans = [dict(row) for row in conn.execute(
            "SELECT * FROM spans WHERE trace_id=? ORDER BY id LIMIT ?",
            (trace_id, max(1, int(limit_spans)))).fetchall()]
    for item in spans:
        if item.get("detail"):
            try:
                item["detail"] = json.loads(item["detail"])
            except Exception:
                pass
    found["spans"] = spans
    if spend_path:
        try:
            import spend as spend_module
            found["spend"] = spend_module.rows_for_trace(trace_id, path=spend_path)
        except Exception:
            found["spend"] = []
    return found


def search(query: str = "", *, hours: float = 24, status: str = "",
           limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM traces WHERE started_at >= ?"
    args: list[Any] = [_since(hours)]
    if status:
        sql += " AND status = ?"
        args.append(status)
    probe = str(query or "").strip()
    if probe:
        like = f"%{probe}%"
        sql += (" AND (trace_id LIKE ? OR input LIKE ? OR output LIKE ? "
                "OR error LIKE ? OR user_id LIKE ? OR session LIKE ? "
                "OR source LIKE ? OR channel LIKE ?)")
        args.extend([like] * 8)
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with _conn() as conn:
        if conn is None:
            return []
        rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
    for row in rows:
        try:
            row["meta"] = json.loads(row.get("meta") or "{}")
        except Exception:
            row["meta"] = {}
    return rows


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return int(ordered[index])


def summary(hours: float = 24) -> dict:
    since = _since(hours)
    with _conn() as conn:
        if conn is None:
            return {"enabled": False, "hours": hours}
        rows = conn.execute(
            "SELECT status, latency_ms, source, channel FROM traces WHERE started_at >= ?",
            (since,)).fetchall()
        errors = [dict(row) for row in conn.execute(
            "SELECT trace_id, started_at, status, source, input, error FROM traces "
            "WHERE started_at >= ? AND status NOT IN ('ok','running') "
            "ORDER BY started_at DESC LIMIT 10", (since,)).fetchall()]
        tool_errors = [dict(row) for row in conn.execute(
            "SELECT name, count(*) AS runs, sum(CASE WHEN status!='ok' THEN 1 ELSE 0 END) "
            "AS errors FROM spans WHERE ts >= ? AND kind='tool' GROUP BY name "
            "ORDER BY errors DESC, runs DESC LIMIT 10", (since,)).fetchall()]
        model_rows = [dict(row) for row in conn.execute(
            "SELECT model, count(*) AS calls, sum(cost) AS cost, "
            "avg(latency_ms) AS avg_ms FROM spans WHERE ts >= ? AND kind='llm' "
            "AND model!='' GROUP BY model ORDER BY calls DESC LIMIT 10", (since,)).fetchall()]
    total = len(rows)
    ok = sum(1 for row in rows if row["status"] == "ok")
    failed = sum(1 for row in rows if row["status"] not in ("ok", "running"))
    latencies = [int(row["latency_ms"] or 0) for row in rows]
    by_source: dict[str, int] = {}
    for row in rows:
        key = str(row["source"] or row["channel"] or "unknown")
        by_source[key] = by_source.get(key, 0) + 1
    for item in model_rows:
        item["cost"] = round(float(item.get("cost") or 0.0), 6)
        item["avg_ms"] = int(item.get("avg_ms") or 0)
    return {
        "enabled": True,
        "hours": hours,
        "total": total,
        "ok": ok,
        "failed": failed,
        "success_rate": round(ok / total * 100, 1) if total else None,
        "p95_latency_ms": _p95(latencies),
        "by_source": by_source,
        "recent_errors": errors,
        "tool_errors": tool_errors,
        "models": model_rows,
    }


def prune(days: int = 30) -> int:
    """Delete traces and spans older than ``days``; returns deleted trace count."""
    if _DB_PATH is None:
        return 0
    cutoff = (datetime.now(CST) - timedelta(days=max(1, int(days)))).isoformat(
        timespec="seconds")
    try:
        with _conn() as conn:
            if conn is None:
                return 0
            ids = [row[0] for row in conn.execute(
                "SELECT trace_id FROM traces WHERE started_at < ?", (cutoff,)).fetchall()]
            conn.execute("DELETE FROM spans WHERE trace_id IN "
                         "(SELECT trace_id FROM traces WHERE started_at < ?)", (cutoff,))
            conn.execute("DELETE FROM traces WHERE started_at < ?", (cutoff,))
            return len(ids)
    except Exception:
        return 0
