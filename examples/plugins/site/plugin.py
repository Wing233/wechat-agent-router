"""Optional example: read visit/chat stats from a site-owned SQLite database.

This plugin is intentionally NOT under `plugins/` so it is not loaded by default.
Copy this directory to `plugins/site` and configure `plugins.site.db` to enable it.

Expected schema: see schema.sql in this directory.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _range_start(value: str) -> datetime:
    now = datetime.now(CST)
    table = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "24h": now - timedelta(hours=24),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
    }
    return table.get(str(value or "7d"), table["7d"])


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise ValueError(f"site db not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


class SiteStats:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def visit_stats(self, range_name: str = "7d", group_by: str = "day") -> dict:
        start = _range_start(range_name)
        group = "strftime('%Y-%m-%d', created_at)" if group_by == "day" else "path"
        since = start.isoformat(timespec="seconds")
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                f"""SELECT {group} AS bucket,
                           COUNT(*) AS pv,
                           COUNT(DISTINCT visitor_id) AS uv
                    FROM visits
                    WHERE created_at >= ?
                    GROUP BY bucket
                    ORDER BY bucket""",
                (since,),
            ).fetchall()
            top_pages = conn.execute(
                """SELECT path, COUNT(*) AS pv
                   FROM visits WHERE created_at >= ?
                   GROUP BY path ORDER BY pv DESC LIMIT 10""",
                (since,),
            ).fetchall()
        return {
            "range": range_name,
            "group_by": group_by,
            "series": [dict(row) for row in rows],
            "top_pages": [dict(row) for row in top_pages],
        }

    def chat_messages(self, range_name: str = "7d", keyword: str = "", limit: int = 20) -> dict:
        start = _range_start(range_name)
        limit = max(1, min(int(limit or 20), 100))
        sql = """SELECT question, answer, created_at FROM chat_messages
                 WHERE created_at >= ?"""
        params: list = [start.isoformat(timespec="seconds")]
        if keyword:
            sql += " AND (question LIKE ? OR answer LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(sql, params).fetchall()
        return {"range": range_name, "count": len(rows), "messages": [dict(row) for row in rows]}

    def chat_stats(self, range_name: str = "7d") -> dict:
        start = _range_start(range_name)
        since = start.isoformat(timespec="seconds")
        with closing(_connect(self.db_path)) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?", (since,)
            ).fetchone()[0]
            by_day = conn.execute(
                """SELECT strftime('%Y-%m-%d', created_at) AS day, COUNT(*) AS count
                   FROM chat_messages WHERE created_at >= ?
                   GROUP BY day ORDER BY day""",
                (since,),
            ).fetchall()
        return {"range": range_name, "total": total, "by_day": [dict(row) for row in by_day]}


def apply(ctx) -> None:
    db_path = str((ctx.config or {}).get("db") or "")
    if not db_path:
        ctx.log.warning("plugins.site.db is not configured; site tools will return errors")
    service = SiteStats(db_path)

    ctx.tools.register(
        "visit_stats",
        "查询站点访问统计（PV/UV）。range 支持 today/24h/7d/30d。",
        _schema({
            "range": {"type": "string", "enum": ["today", "24h", "7d", "30d"]},
            "group_by": {"type": "string", "enum": ["day", "path"]},
        }),
        lambda a: service.visit_stats(a.get("range", "7d"), a.get("group_by", "day")),
    )
    ctx.tools.register(
        "chat_messages",
        "查询站点 AI 对话，可按关键词过滤。",
        _schema({
            "range": {"type": "string", "enum": ["today", "24h", "7d", "30d"]},
            "keyword": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }),
        lambda a: service.chat_messages(a.get("range", "7d"), a.get("keyword", ""),
                                        int(a.get("limit") or 20)),
    )
    ctx.tools.register(
        "chat_stats",
        "查询站点 AI 对话数量趋势。",
        _schema({"range": {"type": "string", "enum": ["today", "24h", "7d", "30d"]}}),
        lambda a: service.chat_stats(a.get("range", "7d")),
    )
    ctx.log.info("site example loaded (db configured: %s)", bool(db_path))
