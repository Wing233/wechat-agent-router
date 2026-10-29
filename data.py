"""Read-only queries over the lyph SQLite database."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")


def _parse(value: str) -> datetime:
    text = (value or "").strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def resolve_range(name: str, start: str = "", end: str = "") -> tuple[str, str, str]:
    now = datetime.now(CST)
    if name == "custom":
        left = _parse(start) if start else now - timedelta(hours=1)
        right = _parse(end) if end else now
    elif name == "yesterday":
        right = now.replace(hour=0, minute=0, second=0, microsecond=0)
        left = right - timedelta(days=1)
    elif name == "last_24h":
        right, left = now, now - timedelta(hours=24)
    elif name == "last_7d":
        right, left = now, now - timedelta(days=7)
    elif name == "last_30m":
        right, left = now, now - timedelta(minutes=30)
    elif name == "this_hour":
        right = now
        left = now.replace(minute=0, second=0, microsecond=0)
    else:  # today
        right = now
        left = now.replace(hour=0, minute=0, second=0, microsecond=0)
    label = f"{left.strftime('%m-%d %H:%M')}–{right.strftime('%H:%M')}"
    return left.astimezone(timezone.utc).isoformat(timespec="seconds"), right.astimezone(timezone.utc).isoformat(timespec="seconds"), label


def local_time(iso: str) -> str:
    try:
        return _parse(iso).astimezone(CST).strftime("%m-%d %H:%M")
    except Exception:
        return iso[:16]


class SiteData:
    def __init__(self, db_path: str, geo, exclude_prefixes: list[str] | None = None, exclude_visitors: list[str] | None = None):
        self.path = Path(db_path)
        self.geo = geo
        self.exclude_prefixes = exclude_prefixes or ["unlimited-check-"]
        self.exclude_visitors = set(exclude_visitors or [])
        self._ip_cache: dict[str, str] = {}

    def _conn(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _chat_filter(self) -> tuple[str, list]:
        clauses = ["message IS NOT NULL", "message!=''", "status!='running'"]
        values: list = []
        for prefix in self.exclude_prefixes:
            clauses.append("conversation_id NOT LIKE ?")
            values.append(prefix.replace("%", "\\%") + "%")
        if self.exclude_visitors:
            marks = ",".join("?" for _ in self.exclude_visitors)
            clauses.append(f"visitor_id NOT IN ({marks})")
            values.extend(sorted(self.exclude_visitors))
        return " AND ".join(clauses), values

    def _ips_for(self, visitor_ids: list[str]) -> dict[str, str]:
        if not visitor_ids:
            return {}
        marks = ",".join("?" for _ in visitor_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT visitor_id, ip FROM visit_events
                    WHERE visitor_id IN ({marks}) AND ip!=''
                    ORDER BY created_at DESC""",
                visitor_ids,
            ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(row["visitor_id"], row["ip"])
        return result

    def _geo(self, ip: str) -> str:
        if not ip:
            return "未知"
        if ip not in self._ip_cache:
            self._ip_cache[ip] = self.geo.lookup(ip)
        return self._ip_cache[ip]

    # ------------------------------------------------------------------
    def visit_stats(self, range_name: str, group_by: str = "none", **kw) -> dict:
        left, right, label = resolve_range(range_name, kw.get("start", ""), kw.get("end", ""))
        with self._conn() as conn:
            totals = dict(
                conn.execute(
                    """SELECT count(*) pv, count(distinct visitor_id) uv,
                              count(distinct session_id) sessions
                       FROM visit_events
                       WHERE created_at>=? AND created_at<? AND event_name='page_view'""",
                    (left, right),
                ).fetchone()
            )
            rows = []
            if group_by in {"hour", "day"}:
                expr = "strftime('%m-%d %H', created_at, '+8 hours')" if group_by == "hour" else "date(created_at,'+8 hours')"
                rows = [dict(r) for r in conn.execute(
                    f"""SELECT {expr} AS bucket, count(*) pv, count(distinct visitor_id) uv
                        FROM visit_events WHERE created_at>=? AND created_at<? AND event_name='page_view'
                        GROUP BY bucket ORDER BY bucket""",
                    (left, right),
                )]
            elif group_by in {"path", "source", "device"}:
                rows = [dict(r) for r in conn.execute(
                    f"""SELECT {group_by} AS bucket, count(*) pv, count(distinct visitor_id) uv
                        FROM visit_events WHERE created_at>=? AND created_at<? AND event_name='page_view'
                        GROUP BY bucket ORDER BY pv DESC LIMIT 20""",
                    (left, right),
                )]
        return {"range": label, "group_by": group_by, "totals": totals, "rows": rows}

    def visitor_detail(self, range_name: str, limit: int = 20, **kw) -> dict:
        left, right, label = resolve_range(range_name, kw.get("start", ""), kw.get("end", ""))
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT visitor_id, session_id, device, source, path, ip, created_at
                   FROM visit_events
                   WHERE created_at>=? AND created_at<? AND event_name='page_view'
                   ORDER BY created_at""",
                (left, right),
            ).fetchall()
            first_seen = {
                r["visitor_id"]: r["first_at"]
                for r in conn.execute(
                    """SELECT visitor_id, min(created_at) first_at FROM visit_events
                       WHERE event_name='page_view' GROUP BY visitor_id"""
                )
            }
        if not rows:
            return {"range": label, "visitors": [], "note": "该时段没有访问"}
        grouped: dict[str, dict] = {}
        for row in rows:
            if row["visitor_id"] in self.exclude_visitors:
                continue
            item = grouped.setdefault(
                row["visitor_id"],
                {"visitor": row["visitor_id"][:6], "device": row["device"], "source": row["source"],
                 "ip": "", "pv": 0, "paths": {}, "first": row["created_at"], "last": row["created_at"]},
            )
            item["pv"] += 1
            item["paths"][row["path"]] = item["paths"].get(row["path"], 0) + 1
            if row["ip"]:
                item["ip"] = row["ip"]
            item["last"] = row["created_at"]
        visitors = []
        for vid, item in grouped.items():
            item["pages"] = [
                p for p, _ in sorted(item.pop("paths").items(), key=lambda kv: kv[1], reverse=True)[:4]
            ]
            item["first"] = local_time(item["first"])
            item["last"] = local_time(item["last"])
            item["geo"] = self._geo(item["ip"])
            item["returning"] = bool(first_seen.get(vid) and first_seen[vid] < left)
            visitors.append(item)
        visitors.sort(key=lambda v: v["pv"], reverse=True)
        return {"range": label, "visitors": visitors[:limit], "total_visitors": len(visitors)}

    def chat_messages(self, range_name: str, keyword: str = "", limit: int = 20, **kw) -> dict:
        left, right, label = resolve_range(range_name, kw.get("start", ""), kw.get("end", ""))
        where, values = self._chat_filter()
        if keyword:
            like = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where += " AND (message LIKE ? ESCAPE '\\' OR answer LIKE ? ESCAPE '\\')"
            values.extend([like, like])
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT id,visitor_id,conversation_id,message,answer,status,created_at
                    FROM chat_requests WHERE created_at>=? AND created_at<? AND {where}
                    ORDER BY created_at DESC LIMIT ?""",
                [left, right, *values, limit],
            ).fetchall()
        ips = self._ips_for(list({r["visitor_id"] for r in rows}))
        items = []
        for row in rows:
            item = dict(row)
            item["visitor"] = item["visitor_id"][:6]
            item["time"] = local_time(item["created_at"])
            item["ip"] = ips.get(item["visitor_id"], "")
            item["geo"] = self._geo(item["ip"])
            item["message"] = (item["message"] or "")[:300]
            item["answer"] = (item["answer"] or "")[:300]
            items.append(item)
        return {"range": label, "items": items, "count": len(items)}

    def chat_stats(self, range_name: str, **kw) -> dict:
        left, right, label = resolve_range(range_name, kw.get("start", ""), kw.get("end", ""))
        where, values = self._chat_filter()
        with self._conn() as conn:
            totals = dict(conn.execute(
                f"""SELECT count(*) requests, count(distinct conversation_id) conversations,
                           count(distinct visitor_id) visitors,
                           sum(status='completed') completed, sum(status='failed') failed
                    FROM chat_requests WHERE created_at>=? AND created_at<? AND {where}""",
                [left, right, *values],
            ).fetchone())
            top = [dict(r) for r in conn.execute(
                f"""SELECT message, count(*) n FROM chat_requests
                    WHERE created_at>=? AND created_at<? AND {where}
                    GROUP BY message ORDER BY n DESC LIMIT 10""",
                [left, right, *values],
            )]
        return {"range": label, "totals": totals, "top_questions": top}

    def realtime(self, minutes: int = 30) -> dict:
        name = "custom"
        now = datetime.now(CST)
        start = now - timedelta(minutes=max(1, min(minutes, 1440)))
        left = start.astimezone(timezone.utc).isoformat(timespec="seconds")
        right = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        visits = self.visit_stats(name, "none", start=start.isoformat(), end=now.isoformat())
        chats = self.chat_messages(name, "", 20, start=start.isoformat(), end=now.isoformat())
        return {"range": f"最近 {minutes} 分钟", "visits": visits["totals"], "chats": chats["items"][:10],
                "note": "数据来自 lyph，只读"}
