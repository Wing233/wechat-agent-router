"""SQLite state for the WeChat agent router."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  text TEXT NOT NULL,
  image_path TEXT,
  status TEXT NOT NULL,
  expect_reply INTEGER NOT NULL DEFAULT 0,
  meta_json TEXT NOT NULL DEFAULT '{}',
  thread_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  replied_at TEXT,
  reply_text TEXT
);
CREATE INDEX IF NOT EXISTS notifications_status ON notifications(status, expires_at);
CREATE INDEX IF NOT EXISTS notifications_code ON notifications(code, status);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  tries INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS channel_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  chat_id TEXT NOT NULL,
  user_id TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  sent_at TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS channel_outbox_pending
  ON channel_outbox(channel_id, status, id);
CREATE INDEX IF NOT EXISTS channel_outbox_created
  ON channel_outbox(created_at);

CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
  service TEXT PRIMARY KEY,
  account TEXT NOT NULL DEFAULT '',
  secret_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'missing',
  last_checked TEXT,
  last_error TEXT,
  last_notified TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routing_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  text TEXT NOT NULL,
  route TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
  id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  text TEXT NOT NULL,
  schedule_type TEXT NOT NULL,
  schedule_json TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  next_run_at TEXT,
  last_run_at TEXT,
  remaining INTEGER,
  end_at TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, next_run_at);
CREATE INDEX IF NOT EXISTS reminders_chat ON reminders(chat_id, status);

CREATE TABLE IF NOT EXISTS reminder_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reminder_id TEXT NOT NULL,
  scheduled_at TEXT NOT NULL,
  sent_at TEXT,
  status TEXT NOT NULL,
  error TEXT,
  UNIQUE(reminder_id, scheduled_at)
);

CREATE TABLE IF NOT EXISTS plugin_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin_id TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  chat_id TEXT NOT NULL DEFAULT '',
  text_hash TEXT NOT NULL DEFAULT '',
  text_preview TEXT NOT NULL DEFAULT '',
  classification TEXT NOT NULL,
  would_accept INTEGER NOT NULL DEFAULT 0,
  executed INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  rule TEXT NOT NULL DEFAULT '',
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS decisions_class ON decisions(classification, created_at);

CREATE TABLE IF NOT EXISTS inbound_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedup_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL DEFAULT 'hub',
  message_id TEXT NOT NULL DEFAULT '',
  chat_id TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS inbound_status ON inbound_messages(status, updated_at);
CREATE INDEX IF NOT EXISTS inbound_created ON inbound_messages(created_at);

CREATE TABLE IF NOT EXISTS agent_tasks (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  result_json TEXT,
  error TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  lease_token TEXT NOT NULL DEFAULT '',
  lease_until TEXT,
  delivered INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS agent_tasks_agent ON agent_tasks(agent_id, status, created_at);
CREATE INDEX IF NOT EXISTS agent_tasks_status ON agent_tasks(status, lease_until);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


INBOUND_MAX_ATTEMPTS = 3
TASK_MAX_ATTEMPTS = 3
TASK_LEASE_SECONDS = 1800


class Store:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # --- generic state -------------------------------------------------
    def state_get(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def state_set(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- conversation --------------------------------------------------
    def add_message(self, chat_id: str, role: str, content: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages(chat_id,role,content,created_at) VALUES(?,?,?,?)",
                (chat_id, role, content, now_iso()),
            )

    def recent_messages(self, chat_id: str, limit: int = 12) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role,content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # --- notifications -------------------------------------------------
    def create_notification(
        self,
        *,
        agent_id: str,
        chat_id: str,
        text: str,
        code: str,
        expect_reply: bool,
        ttl_seconds: int,
        image_path: str | None = None,
        meta: dict | None = None,
        thread_id: str | None = None,
    ) -> dict:
        nid = uuid.uuid4().hex
        created = datetime.now(timezone.utc)
        expires = created.timestamp() + ttl_seconds
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO notifications
                   (id,code,agent_id,chat_id,text,image_path,status,expect_reply,
                    meta_json,thread_id,created_at,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    nid,
                    code,
                    agent_id,
                    chat_id,
                    text,
                    image_path,
                    "pending" if expect_reply else "sent",
                    1 if expect_reply else 0,
                    json.dumps(meta or {}, ensure_ascii=False),
                    thread_id,
                    created.isoformat(timespec="seconds"),
                    datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        return self.get_notification(nid)

    def get_notification(self, nid: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["meta"] = json.loads(item.pop("meta_json") or "{}")
        return item

    def by_code(self, code: str, chat_id: str = "") -> dict | None:
        sql = "SELECT * FROM notifications WHERE code=? AND status='pending'"
        params: list = [code.upper()]
        if chat_id:
            sql += " AND chat_id=?"
            params.append(chat_id)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        item = dict(row)
        item["meta"] = json.loads(item.pop("meta_json") or "{}")
        return item

    def active_pending(self, chat_id: str) -> list[dict]:
        now = now_iso()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM notifications
                   WHERE chat_id=? AND status='pending'
                     AND (expires_at IS NULL OR expires_at>?)
                   ORDER BY created_at DESC""",
                (chat_id, now),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["meta"] = json.loads(item.pop("meta_json") or "{}")
            items.append(item)
        return items

    def expire_stale(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE notifications SET status='expired' "
                "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?",
                (now_iso(),),
            )
            return cur.rowcount

    def set_status(self, nid: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE notifications SET status=? WHERE id=?", (status, nid))

    def mark_replied(self, nid: str, reply: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notifications SET status='replied',replied_at=?,reply_text=? WHERE id=?",
                (now_iso(), reply[:4000], nid),
            )

    # --- outbox --------------------------------------------------------
    def enqueue_outbox(self, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO outbox(payload_json,created_at) VALUES(?,?)",
                (json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    def list_outbox(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id,payload_json,tries FROM outbox ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"id": row["id"], "payload": json.loads(row["payload_json"]), "tries": row["tries"]}
            for row in rows
        ]

    def drop_outbox(self, oid: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM outbox WHERE id=?", (oid,))

    def bump_outbox(self, oid: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE outbox SET tries=tries+1 WHERE id=?", (oid,))

    # --- channel outbox -------------------------------------------------
    def enqueue_channel(self, channel_id: str, chat_id: str, text: str, *,
                        platform: str = "", user_id: str = "") -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO channel_outbox(channel_id,platform,chat_id,user_id,text,"
                "status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                (str(channel_id)[:40], str(platform)[:40], str(chat_id)[:200],
                 str(user_id)[:200], str(text)[:8000], now_iso()),
            )
            return int(cursor.lastrowid)

    def list_channel_outbox(self, channel_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id,channel_id,platform,chat_id,user_id,text,status,created_at "
                "FROM channel_outbox WHERE channel_id=? AND status='pending' "
                "ORDER BY id LIMIT ?", (str(channel_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def ack_channel_outbox(self, channel_id: str, ids: list[int], *,
                           ok: bool = True, error: str = "") -> int:
        clean = [int(value) for value in ids if str(value).isdigit()][:100]
        if not clean:
            return 0
        placeholders = ",".join("?" for _ in clean)
        with self._conn() as conn:
            cursor = conn.execute(
                f"UPDATE channel_outbox SET status=?, sent_at=?, error=? "
                f"WHERE channel_id=? AND status='pending' AND id IN ({placeholders})",
                [("sent" if ok else "error"), now_iso(),
                 str(error or "")[:500], str(channel_id), *clean],
            )
            return int(cursor.rowcount or 0)

    def recent_channel_outbox(self, channel_id: str = "", limit: int = 20) -> list[dict]:
        sql = ("SELECT id,channel_id,platform,chat_id,user_id,text,status,created_at,"
               "sent_at,error FROM channel_outbox")
        args: list = []
        if channel_id:
            sql += " WHERE channel_id=?"
            args.append(str(channel_id))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def cleanup_channel_outbox(self, days: int = 7) -> int:
        cutoff = time.time() - max(1, int(days)) * 86400
        stamp = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM channel_outbox WHERE created_at < ?",
                (stamp,),
            )
            return int(cursor.rowcount or 0)

    # --- credentials ---------------------------------------------------
    def get_credential(self, service: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE service=?", (service,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["secret"] = json.loads(item.pop("secret_json") or "{}")
        return item

    def save_credential(self, service: str, account: str, secret: dict, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO credentials(service,account,secret_json,status,last_checked,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(service) DO UPDATE SET account=excluded.account,
                     secret_json=excluded.secret_json,status=excluded.status,
                     last_checked=excluded.last_checked,last_error=NULL,
                     last_notified=NULL,updated_at=excluded.updated_at""",
                (service, account, json.dumps(secret, ensure_ascii=False), status, now_iso(), now_iso()),
            )

    def mark_credential(self, service: str, status: str, error: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE credentials SET status=?,last_checked=?,last_error=?,updated_at=? WHERE service=?",
                (status, now_iso(), error[:1000], now_iso(), service),
            )

    def mark_notified(self, service: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE credentials SET last_notified=? WHERE service=?", (now_iso(), service))

    # --- logging -------------------------------------------------------
    def log_route(self, chat_id: str, text: str, route: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO routing_log(chat_id,text,route,detail,created_at) VALUES(?,?,?,?,?)",
                (chat_id, text[:1000], route, detail[:1000], now_iso()),
            )

    # --- reminders -----------------------------------------------------
    def create_reminder(self, *, chat_id: str, text: str, schedule_type: str, schedule: dict,
                        timezone_name: str, next_run_at: str | None, remaining: int | None,
                        end_at: str | None) -> dict:
        rid = uuid.uuid4().hex
        stamp = now_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO reminders
                   (id,chat_id,text,schedule_type,schedule_json,timezone,next_run_at,
                    remaining,end_at,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, chat_id, text[:500], schedule_type,
                 json.dumps(schedule, ensure_ascii=False), timezone_name,
                 next_run_at, remaining, end_at, "active", stamp, stamp),
            )
        return self.get_reminder(rid)

    def get_reminder(self, rid: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        return self._reminder_row(row)

    def _reminder_row(self, row) -> dict | None:
        if not row:
            return None
        item = dict(row)
        item["schedule"] = json.loads(item.pop("schedule_json") or "{}")
        return item

    def due_reminders(self, now: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM reminders
                   WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?
                   ORDER BY next_run_at LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [self._reminder_row(row) for row in rows]

    def claim_reminder_run(self, reminder_id: str, scheduled_at: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO reminder_runs(reminder_id,scheduled_at,status) VALUES(?,?,?)",
                (reminder_id, scheduled_at, "claimed"),
            )
            return cur.rowcount == 1

    def finish_reminder_run(self, reminder_id: str, scheduled_at: str, status: str, error: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE reminder_runs SET status=?,sent_at=?,error=?
                   WHERE reminder_id=? AND scheduled_at=?""",
                (status, now_iso(), error[:500], reminder_id, scheduled_at),
            )

    def advance_reminder(self, rid: str, *, next_run_at: str | None, last_run_at: str,
                         remaining: int | None, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE reminders SET next_run_at=?,last_run_at=?,remaining=?,status=?,updated_at=?
                   WHERE id=?""",
                (next_run_at, last_run_at, remaining, status, now_iso(), rid),
            )

    def list_reminders(self, chat_id: str, statuses: tuple[str, ...] = ("active", "paused")) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT * FROM reminders WHERE chat_id=? AND status IN ({marks})
                    ORDER BY coalesce(next_run_at,'9999')""",
                [chat_id, *statuses],
            ).fetchall()
        return [self._reminder_row(row) for row in rows]

    def count_active_reminders(self, chat_id: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT count(*) FROM reminders WHERE chat_id=? AND status='active'", (chat_id,)
            ).fetchone()[0]

    def find_reminders(self, chat_id: str, text: str,
                       statuses: tuple[str, ...] = ("active", "paused")) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT * FROM reminders WHERE chat_id=? AND status IN ({marks})
                    AND (id LIKE ? OR lower(text) LIKE ?)""",
                [chat_id, *statuses, text + "%", "%" + text.lower() + "%"],
            ).fetchall()
        return [self._reminder_row(row) for row in rows]

    def set_reminder_status(self, rid: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE reminders SET status=?,updated_at=? WHERE id=?", (status, now_iso(), rid))

    def snooze_reminder(self, rid: str, next_run_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE reminders SET next_run_at=?,status='active',updated_at=? WHERE id=?",
                (next_run_at, now_iso(), rid),
            )

    # --- plugin audit --------------------------------------------------
    def log_plugin_event(self, plugin_id: str, action: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO plugin_events(plugin_id,action,detail,created_at) VALUES(?,?,?,?)",
                (plugin_id, action, detail[:1000], now_iso()),
            )

    def list_plugin_events(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM plugin_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # --- inbound gate audit --------------------------------------------
    def log_decision(
        self,
        *,
        trace_id: str = "",
        role: str = "",
        chat_id: str = "",
        text: str = "",
        classification: str = "",
        would_accept: bool = False,
        executed: bool = False,
        reason: str = "",
        rule: str = "",
        meta: dict | None = None,
    ) -> int:
        digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO decisions
                   (trace_id,role,chat_id,text_hash,text_preview,classification,
                    would_accept,executed,reason,rule,meta_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trace_id,
                    role,
                    chat_id,
                    digest,
                    (text or "")[:240],
                    classification,
                    1 if would_accept else 0,
                    1 if executed else 0,
                    reason[:500],
                    rule[:120],
                    json.dumps(meta or {}, ensure_ascii=False, default=str),
                    now_iso(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_decisions(self, limit: int = 50, classification: str = "") -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        with self._conn() as conn:
            if classification:
                rows = conn.execute(
                    "SELECT * FROM decisions WHERE classification=? "
                    "ORDER BY id DESC LIMIT ?", (classification, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["meta"] = json.loads(item.pop("meta_json") or "{}")
            item["would_accept"] = bool(item["would_accept"])
            item["executed"] = bool(item["executed"])
            items.append(item)
        return items

    def decision_stats(self, days: int = 7) -> dict:
        days = max(1, min(int(days or 7), 90))
        since = time.time() - days * 86400
        since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT classification,COUNT(*) AS count FROM decisions "
                "WHERE created_at>=? GROUP BY classification", (since_iso,)
            ).fetchall()
        counts = {row["classification"]: row["count"] for row in rows}
        return {"days": days, "counts": counts,
                "total": sum(counts.values())}

    def prune_decisions(self, days: int = 30) -> int:
        days = max(1, min(int(days or 30), 365))
        cutoff = datetime.fromtimestamp(
            time.time() - days * 86400, timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM decisions WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    # --- inbound message journal ---------------------------------------
    def claim_inbound(
        self,
        dedup_key: str,
        *,
        source: str = "hub",
        message_id: str = "",
        chat_id: str = "",
        user_id: str = "",
        text: str = "",
        stale_seconds: int = 1800,
    ) -> dict | None:
        now = now_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM inbound_messages WHERE dedup_key=?",
                (dedup_key,)).fetchone()
            if row:
                item = dict(row)
                if item["status"] in {"done", "dead"}:
                    return None
                if item["status"] == "processing":
                    try:
                        updated = datetime.fromisoformat(item["updated_at"])
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - updated).total_seconds()
                        if age < stale_seconds:
                            return None
                    except Exception:
                        pass
                if int(item["attempts"]) >= INBOUND_MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE inbound_messages SET status='dead',"
                        "last_error=?,updated_at=? WHERE id=?",
                        ("max attempts exceeded", now, item["id"]))
                    return None
                conn.execute(
                    "UPDATE inbound_messages SET status='processing',"
                    "attempts=attempts+1,last_error='',updated_at=? WHERE id=?",
                    (now, item["id"]))
                item["status"] = "processing"
                item["attempts"] = int(item["attempts"]) + 1
                return item
            cursor = conn.execute(
                """INSERT INTO inbound_messages
                   (dedup_key,source,message_id,chat_id,user_id,text,status,
                    attempts,last_error,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (dedup_key, source[:40], message_id[:200], chat_id[:200],
                 user_id[:200], text[:4000], "processing", 1, "", now, now))
            row = conn.execute(
                "SELECT * FROM inbound_messages WHERE id=?",
                (cursor.lastrowid,)).fetchone()
            return dict(row) if row else None

    def finish_inbound(self, inbound_id: int) -> None:
        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE inbound_messages SET status='done',processed_at=?,"
                "updated_at=? WHERE id=?", (now, now, inbound_id))

    def fail_inbound(self, inbound_id: int, error: str) -> int:
        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE inbound_messages SET status='failed',last_error=?,"
                "updated_at=? WHERE id=?", (str(error)[:2000], now, inbound_id))
            row = conn.execute(
                "SELECT attempts FROM inbound_messages WHERE id=?",
                (inbound_id,)).fetchone()
        return int(row["attempts"] or 0) if row else 0

    def mark_inbound_dead(self, inbound_id: int, error: str) -> None:
        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE inbound_messages SET status='dead',last_error=?,"
                "updated_at=? WHERE id=?", (str(error)[:2000], now, inbound_id))

    def recover_inbound(self) -> int:
        now = now_iso()
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE inbound_messages SET status='pending',"
                "last_error='worker restarted',updated_at=? "
                "WHERE status='processing'", (now,))
            return int(cursor.rowcount or 0)

    def list_inbound(self, limit: int = 50, status: str = "") -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM inbound_messages WHERE status=? "
                    "ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inbound_messages ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(row) for row in rows]

    def prune_inbound(self, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(1, int(days or 30)))).isoformat(
                      timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM inbound_messages WHERE created_at<?", (cutoff,))
            return int(cursor.rowcount or 0)

    # --- durable agent tasks ------------------------------------------
    def create_agent_task(self, agent_id: str, payload: dict,
                          max_attempts: int = TASK_MAX_ATTEMPTS) -> dict:
        task_id = uuid.uuid4().hex
        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO agent_tasks
                   (id,agent_id,status,payload_json,result_json,error,attempts,
                    max_attempts,lease_token,lease_until,delivered,created_at,
                    updated_at,completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, agent_id[:120], "queued",
                 json.dumps(payload or {}, ensure_ascii=False, default=str),
                 None, "", 0, max(1, int(max_attempts)), "", None, 0,
                 now, now, None))
        return self.get_agent_task(task_id) or {"id": task_id, "status": "queued"}

    def get_agent_task(self, task_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        return self._agent_task(row) if row else None

    @staticmethod
    def _agent_task(row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        raw_result = item.pop("result_json", None)
        item["result"] = json.loads(raw_result) if raw_result else None
        item["delivered"] = bool(item.get("delivered"))
        return item

    def claim_agent_task(self, agent_id: str,
                         lease_seconds: int = TASK_LEASE_SECONDS) -> dict | None:
        now = now_iso()
        lease_until = (datetime.now(timezone.utc)
                       + timedelta(seconds=max(60, int(lease_seconds)))
                       ).isoformat(timespec="seconds")
        lease_token = uuid.uuid4().hex
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM agent_tasks WHERE agent_id=? AND status='queued' "
                "AND attempts<max_attempts ORDER BY created_at LIMIT 1",
                (agent_id,)).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                "UPDATE agent_tasks SET status='claimed',attempts=attempts+1,"
                "lease_token=?,lease_until=?,updated_at=? "
                "WHERE id=? AND status='queued'",
                (lease_token, lease_until, now, row["id"]))
            if cursor.rowcount != 1:
                return None
        return self.get_agent_task(row["id"])

    def complete_agent_task(self, task_id: str, ok: bool, text: str,
                            *, agent_id: str = "",
                            lease_token: str = "") -> bool:
        now = now_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return False
            item = dict(row)
            if agent_id and item["agent_id"] != agent_id:
                return False
            if item["status"] in {"completed", "failed", "expired", "cancelled"}:
                return True
            if lease_token and item["lease_token"] and lease_token != item["lease_token"]:
                return False
            status = "completed" if ok else "failed"
            result = json.dumps({"ok": bool(ok), "text": str(text)[:8000]},
                                ensure_ascii=False)
            conn.execute(
                "UPDATE agent_tasks SET status=?,result_json=?,error=?,"
                "lease_token='',lease_until=NULL,delivered=0,updated_at=?,"
                "completed_at=? WHERE id=?",
                (status, result, "" if ok else str(text)[:2000], now, now, task_id))
        return True

    def mark_agent_task_delivered(self, task_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE agent_tasks SET delivered=1 WHERE id=? AND delivered=0",
                (task_id,))
            return int(cursor.rowcount or 0) == 1

    def heartbeat_agent_task(self, task_id: str, lease_token: str,
                             *, agent_id: str = "",
                             lease_seconds: int = TASK_LEASE_SECONDS) -> bool:
        now = now_iso()
        lease_until = (datetime.now(timezone.utc)
                       + timedelta(seconds=max(60, int(lease_seconds)))
                       ).isoformat(timespec="seconds")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT agent_id,lease_token,status FROM agent_tasks WHERE id=?",
                (task_id,)).fetchone()
            if not row:
                return False
            if agent_id and row["agent_id"] != agent_id:
                return False
            if lease_token and row["lease_token"] and lease_token != row["lease_token"]:
                return False
            if row["status"] not in {"claimed", "running"}:
                return False
            conn.execute(
                "UPDATE agent_tasks SET status='running',lease_until=?,"
                "updated_at=? WHERE id=?", (lease_until, now, task_id))
            return True

    def requeue_expired_agent_tasks(self) -> list[dict]:
        now = now_iso()
        failed_ids: list[str] = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE status IN ('claimed','running') "
                "AND lease_until IS NOT NULL AND lease_until<?",
                (now,)).fetchall()
            for row in rows:
                item = dict(row)
                if int(item["attempts"]) >= int(item["max_attempts"]):
                    conn.execute(
                        "UPDATE agent_tasks SET status='failed',error=?,"
                        "lease_token='',lease_until=NULL,delivered=0,"
                        "updated_at=?,completed_at=? WHERE id=?",
                        (f"lease expired after {item['attempts']} attempts",
                         now, now, item["id"]))
                    failed_ids.append(item["id"])
                else:
                    conn.execute(
                        "UPDATE agent_tasks SET status='queued',"
                        "error='lease expired; requeued',lease_token='',"
                        "lease_until=NULL,updated_at=? WHERE id=?",
                        (now, item["id"]))
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE status='queued' "
                "AND attempts>=max_attempts").fetchall()
            for row in rows:
                item = dict(row)
                conn.execute(
                    "UPDATE agent_tasks SET status='failed',error=?,"
                    "updated_at=?,completed_at=? WHERE id=?",
                    (f"attempts exhausted ({item['attempts']})",
                     now, now, item["id"]))
                failed_ids.append(item["id"])
        return [task for task in (self.get_agent_task(tid) for tid in failed_ids)
                if task]

    def recover_agent_tasks(self) -> list[dict]:
        """On startup, requeue tasks left claimed/running by the previous process."""
        now = now_iso()
        failed_ids: list[str] = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE status IN ('claimed','running')"
            ).fetchall()
            for row in rows:
                item = dict(row)
                if int(item["attempts"]) >= int(item["max_attempts"]):
                    conn.execute(
                        "UPDATE agent_tasks SET status='failed',error=?,"
                        "lease_token='',lease_until=NULL,delivered=0,"
                        "updated_at=?,completed_at=? WHERE id=?",
                        ("worker restarted; attempts exhausted",
                         now, now, item["id"]))
                    failed_ids.append(item["id"])
                else:
                    conn.execute(
                        "UPDATE agent_tasks SET status='queued',"
                        "error='worker restarted; requeued',lease_token='',"
                        "lease_until=NULL,updated_at=? WHERE id=?",
                        (now, item["id"]))
        return [task for task in (self.get_agent_task(tid) for tid in failed_ids)
                if task]

    def agent_task_counts(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS count FROM agent_tasks GROUP BY status"
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def agent_task_pending_by_agent(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT agent_id,COUNT(*) AS count FROM agent_tasks "
                "WHERE status='queued' GROUP BY agent_id").fetchall()
        return {row["agent_id"]: row["count"] for row in rows}

    def list_agent_tasks(self, limit: int = 50, status: str = "") -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM agent_tasks WHERE status=? "
                    "ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,)).fetchall()
        return [self._agent_task(row) for row in rows]

    def prune_agent_tasks(self, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(1, int(days or 30)))).isoformat(
                      timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_tasks WHERE status IN "
                "('completed','failed','expired','cancelled') AND updated_at<?",
                (cutoff,))
            return int(cursor.rowcount or 0)
