"""SQLite spend ledger for LLM calls.

Kept in its own database (default ``data/spend.db``) so cost accounting never
touches the router state database. Every helper opens a short-lived connection;
SQLite WAL handles the low write volume of a single chat bot.
"""
from __future__ import annotations

import contextlib
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  trace_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS spend_role_ts ON spend(role, ts);
CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts);
"""

_DB_PATH = Path(os.environ.get("ROUTER_SPEND_DB") or "data/spend.db")


def configure(path: str | os.PathLike | None) -> None:
    """Point the ledger at ``path`` and create the table if needed."""
    global _DB_PATH
    if path:
        _DB_PATH = Path(path)
    init_db()


def _db_path(path: str | os.PathLike | None = None) -> Path:
    target = Path(path) if path else _DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


@contextlib.contextmanager
def _conn(path: str | os.PathLike | None = None):
    conn = sqlite3.connect(_db_path(path), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: str | os.PathLike | None = None) -> None:
    with _conn(path) as conn:
        conn.executescript(SCHEMA)


def _today() -> str:
    return datetime.now(CST).date().isoformat()


def record_spend(role: str, model: str, prompt_tokens: int, completion_tokens: int,
                 cost: float, trace_id: str = "",
                 path: str | os.PathLike | None = None) -> float:
    """Append one usage row and return the recorded cost."""
    amount = max(0.0, float(cost or 0.0))
    stamp = datetime.now(CST).isoformat(timespec="seconds")
    row = (
        stamp,
        str(role or "owner"),
        str(model or "")[:200],
        max(0, int(prompt_tokens or 0)),
        max(0, int(completion_tokens or 0)),
        amount,
        str(trace_id or "")[:64],
    )
    try:
        with _conn(path) as conn:
            conn.execute(
                "INSERT INTO spend(ts,role,model,prompt_tokens,completion_tokens,cost,trace_id) "
                "VALUES(?,?,?,?,?,?,?)",
                row,
            )
    except Exception:
        # Accounting must never break a chat reply.
        return amount
    return amount


def today_total(role: str | None = None,
                path: str | os.PathLike | None = None) -> float:
    """Sum today's cost in Asia/Shanghai, optionally for one role."""
    sql = "SELECT coalesce(sum(cost), 0) FROM spend WHERE substr(ts, 1, 10) = ?"
    args: list[str] = [_today()]
    if role:
        sql += " AND role = ?"
        args.append(str(role))
    try:
        with _conn(path) as conn:
            value = conn.execute(sql, args).fetchone()[0]
    except Exception:
        return 0.0
    return float(value or 0.0)


def budget_left(role: str, budget: float,
                path: str | os.PathLike | None = None) -> float:
    """Remaining budget for today. A non-positive budget means unlimited."""
    limit = float(budget or 0.0)
    if limit <= 0:
        return math.inf
    return max(0.0, limit - today_total(role, path))


def estimate_cost(prompt_tokens: int, completion_tokens: int,
                  input_price_per_m: float = 0.0,
                  output_price_per_m: float = 0.0) -> float:
    """Fallback price estimate using per-million-token prices."""
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    return (prompt / 1_000_000.0) * float(input_price_per_m or 0.0) + (
        completion / 1_000_000.0) * float(output_price_per_m or 0.0)


def _since(days: float) -> str:
    return (datetime.now(CST) - timedelta(days=max(0.0, float(days)))).isoformat(
        timespec="seconds")


def rows_for_trace(trace_id: str, path: str | os.PathLike | None = None) -> list[dict]:
    """Return every billed call that belongs to one trace id."""
    if not trace_id:
        return []
    try:
        with _conn(path) as conn:
            rows = conn.execute(
                "SELECT ts, role, model, prompt_tokens, completion_tokens, cost "
                "FROM spend WHERE trace_id = ? ORDER BY id", (trace_id,)).fetchall()
    except Exception:
        return []
    return [
        {"ts": row[0], "role": row[1], "model": row[2],
         "prompt_tokens": row[3], "completion_tokens": row[4],
         "cost": round(float(row[5] or 0.0), 6)}
        for row in rows
    ]


def recent(limit: int = 20, path: str | os.PathLike | None = None) -> list[dict]:
    try:
        with _conn(path) as conn:
            rows = conn.execute(
                "SELECT ts, role, model, prompt_tokens, completion_tokens, cost, trace_id "
                "FROM spend ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
    except Exception:
        return []
    return [
        {"ts": row[0], "role": row[1], "model": row[2],
         "prompt_tokens": row[3], "completion_tokens": row[4],
         "cost": round(float(row[5] or 0.0), 6), "trace_id": row[6]}
        for row in rows
    ]


def report(days: float = 7, path: str | os.PathLike | None = None) -> dict:
    """Aggregate spend by day, role and model for natural-language reports."""
    days = max(0.1, float(days or 7))
    since = _since(days)
    empty = {"days": days, "total": 0.0, "calls": 0, "prompt_tokens": 0,
             "completion_tokens": 0, "by_day": [], "by_role": [], "by_model": [],
             "today": today_total(path=path)}
    try:
        with _conn(path) as conn:
            rows = conn.execute(
                "SELECT ts, role, model, prompt_tokens, completion_tokens, cost "
                "FROM spend WHERE ts >= ? ORDER BY id", (since,)).fetchall()
    except Exception:
        return empty
    if not rows:
        return empty

    day_map: dict[str, dict] = {}
    role_map: dict[str, dict] = {}
    model_map: dict[str, dict] = {}
    total = 0.0
    prompt = 0
    completion = 0
    for ts, role, model, prompt_tokens, completion_tokens, cost in rows:
        amount = float(cost or 0.0)
        total += amount
        prompt += int(prompt_tokens or 0)
        completion += int(completion_tokens or 0)
        for bucket, key in ((day_map, str(ts)[:10]), (role_map, str(role or "")),
                            (model_map, str(model or ""))):
            item = bucket.setdefault(key, {"calls": 0, "cost": 0.0})
            item["calls"] += 1
            item["cost"] += amount

    def _rows(mapping: dict[str, dict], key_name: str) -> list[dict]:
        items = []
        for key, item in mapping.items():
            items.append({key_name: key, "calls": item["calls"],
                          "cost": round(item["cost"], 6)})
        return sorted(items, key=lambda row: row["cost"], reverse=True)

    return {
        "days": days,
        "total": round(total, 6),
        "calls": len(rows),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "today": round(today_total(path=path), 6),
        "by_day": sorted(_rows(day_map, "day"), key=lambda row: row["day"]),
        "by_role": _rows(role_map, "role"),
        "by_model": _rows(model_map, "model"),
    }


def budget_status(budgets: dict[str, float] | None,
                  path: str | os.PathLike | None = None) -> list[dict]:
    """Return today's spent/left/percent for each configured role budget."""
    items = []
    for role, raw in (budgets or {}).items():
        limit = float(raw or 0.0)
        spent = today_total(role, path=path)
        left = max(0.0, limit - spent) if limit > 0 else None
        items.append({
            "role": role,
            "budget": limit,
            "spent": round(spent, 6),
            "left": None if left is None else round(left, 6),
            "percent": None if limit <= 0 else round(spent / limit * 100, 1),
            "unlimited": limit <= 0,
        })
    return sorted(items, key=lambda item: item["role"])


def completion_cost(response, model: str = "") -> float:
    """Use litellm's price table when possible; 0.0 means caller should estimate."""
    try:
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm  # lazy: litellm stays an optional dependency
        litellm.suppress_debug_info = True
        value = litellm.completion_cost(completion_response=response,
                                        model=model or None)
    except Exception:
        return 0.0
    try:
        cost = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return cost if math.isfinite(cost) and cost > 0 else 0.0
