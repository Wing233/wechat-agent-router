"""Todo plugin: per-owner JSON snapshot in the shared state table, optional reminders."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
MAX_ITEMS = 200


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def parse_due(value: str) -> datetime | None:
    """Accept '2026-11-04 12:00', '2026-11-04' (-> 09:00), or ISO with timezone."""
    text = (value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(hour=9, tzinfo=CST)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"due 时间看不懂：{value}（可用 2026-11-04 12:00 / 2026-11-04 / 带时区 ISO）") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=CST)


class TodoError(Exception):
    pass


class TodoEngine:
    def __init__(self, store, default_owner: str = "", reminders_getter=None):
        self.store = store
        self.default_owner = default_owner or ""
        self._reminders = reminders_getter or (lambda: None)

    # --- storage -------------------------------------------------------
    def _key(self, chat_id: str) -> str:
        return f"todo:{chat_id or self.default_owner or 'default'}"

    def _load(self, chat_id: str) -> dict:
        raw = self.store.state_get(self._key(chat_id), "")
        if not raw:
            return {"items": [], "seq": 0}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"items": [], "seq": 0}
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {"items": [], "seq": 0}
        for item in data["items"]:
            item.setdefault("done", False)
            item.setdefault("due", "")
            item.setdefault("reminder_id", "")
        return data

    def _save(self, chat_id: str, data: dict) -> None:
        self.store.state_set(self._key(chat_id), json.dumps(data, ensure_ascii=False))

    # --- helpers -------------------------------------------------------
    def _new_id(self, data: dict) -> str:
        taken = {item.get("id") for item in data["items"]}
        for _ in range(50):
            cand = uuid.uuid4().hex[:4]
            if cand not in taken:
                return cand
        raise TodoError("编号分配失败了，再试一次")

    def _view(self, item: dict) -> dict:
        return {
            "id": item.get("id", ""),
            "text": item.get("text", ""),
            "done": bool(item.get("done")),
            "due": item.get("due") or "-",
            "reminder_id": item.get("reminder_id") or "",
            "created": item.get("created", ""),
        }

    def _match(self, data: dict, key: str) -> list[dict]:
        key = (key or "").strip().lower()
        if not key:
            raise TodoError("要给我一个待办编号（或关键词）")
        hits = [i for i in data["items"] if str(i.get("id", "")).lower().startswith(key)]
        if hits:
            return hits
        return [i for i in data["items"] if key in str(i.get("text", "")).lower()]

    def _one(self, data: dict, key: str):
        hits = self._match(data, key)
        if not hits:
            raise TodoError(f"没找到匹配「{key}」的待办")
        if len(hits) > 1:
            return None, {"ok": False, "need_choice": [self._view(i) for i in hits]}
        return hits[0], None

    def _hook_reminder(self, chat_id: str, data: dict, item: dict, when: datetime) -> str:
        service = self._reminders()
        if service is None:
            return "提醒插件没加载，只存了待办"
        now = datetime.now(CST)
        if when <= now:
            return "时间已经过了，没建提醒"
        if when > now + timedelta(days=730):
            return "超过两年，没建提醒"
        try:
            res = service.create(
                chat_id=chat_id or self.default_owner,
                text=f"待办 {item['id']}：{item['text']}",
                schedule_type="once",
                at=when.isoformat(timespec="seconds"),
            )
        except Exception as exc:  # reminder failure never blocks the todo
            return f"提醒没建成：{exc}"
        rid = str((res or {}).get("id") or "")
        if not rid:
            return "提醒没建成"
        item["reminder_id"] = rid
        self._save(chat_id, data)
        return f"已挂提醒，{res.get('next') or '待触发'}"

    def _drop_reminder(self, item: dict) -> str:
        rid = str(item.get("reminder_id") or "")
        if not rid:
            return ""
        try:
            self.store.set_reminder_status(rid, "cancelled")
        except Exception as exc:
            return f"提醒撤销失败：{exc}"
        item["reminder_id"] = ""
        return "已撤提醒"

    # --- operations ----------------------------------------------------
    def add(self, chat_id: str, text: str, due: str = "") -> dict:
        text = (text or "").strip()
        if not text:
            raise TodoError("待办内容不能为空")
        data = self._load(chat_id)
        if len(data["items"]) >= MAX_ITEMS:
            raise TodoError(f"待办上限 {MAX_ITEMS} 条，先清掉一些")
        when = parse_due(due)
        item = {
            "id": self._new_id(data),
            "text": text[:300],
            "done": False,
            "created": _now_iso(),
            "due": when.isoformat(timespec="seconds") if when else "",
            "reminder_id": "",
        }
        data["items"].append(item)
        self._save(chat_id, data)
        note = self._hook_reminder(chat_id, data, item, when) if when else ""
        return {"ok": True, "item": self._view(item), "note": note}

    def list(self, chat_id: str, include_done: bool = False) -> dict:
        data = self._load(chat_id)
        items = data["items"]
        pending = [i for i in items if not i.get("done")]
        shown = items if include_done else pending
        return {
            "ok": True,
            "total": len(items),
            "pending": len(pending),
            "items": [self._view(i) for i in shown],
        }

    def update(self, chat_id: str, key: str, text: str | None = None, due: str | None = None) -> dict:
        data = self._load(chat_id)
        item, choice = self._one(data, key)
        if choice:
            return choice
        notes: list[str] = []
        if text is not None and str(text).strip():
            item["text"] = str(text).strip()[:300]
        if due is not None:
            when = parse_due(due)
            dropped = self._drop_reminder(item)
            if dropped:
                notes.append(dropped)
            item["due"] = when.isoformat(timespec="seconds") if when else ""
            if when:
                notes.append(self._hook_reminder(chat_id, data, item, when))
        self._save(chat_id, data)
        return {"ok": True, "item": self._view(item), "note": "；".join(n for n in notes if n)}

    def done(self, chat_id: str, key: str) -> dict:
        data = self._load(chat_id)
        item, choice = self._one(data, key)
        if choice:
            return choice
        if item.get("done"):
            return {"ok": True, "item": self._view(item), "note": "这条早就勾完了"}
        item["done"] = True
        item["done_at"] = _now_iso()
        note = self._drop_reminder(item)
        self._save(chat_id, data)
        return {"ok": True, "item": self._view(item), "note": note}

    def delete(self, chat_id: str, key: str) -> dict:
        data = self._load(chat_id)
        item, choice = self._one(data, key)
        if choice:
            return choice
        data["items"] = [i for i in data["items"] if i.get("id") != item.get("id")]
        note = self._drop_reminder(item)
        self._save(chat_id, data)
        return {"ok": True, "deleted": self._view(item), "remaining": len(data["items"]), "note": note}

    def clear_done(self, chat_id: str) -> dict:
        data = self._load(chat_id)
        kept, removed = [], []
        for item in data["items"]:
            if item.get("done"):
                removed.append(item)
            else:
                kept.append(item)
        for item in removed:
            self._drop_reminder(item)
        data["items"] = kept
        self._save(chat_id, data)
        return {"ok": True, "cleared": len(removed), "remaining": len(kept)}


def apply(ctx) -> None:
    owner = ctx.config.get("owner") or ctx.root_config.get("owner") or ""
    store = ctx.resource("store")
    engine = TodoEngine(store, owner, lambda: ctx._services.get("reminders"))
    ctx.provide("todo", engine)

    due_desc = "可选，到期时间：2026-11-04 12:00 / 2026-11-04（当天 09:00）/ 带时区 ISO"

    ctx.tools.register(
        "todo_add",
        "新增一条待办；给了 due 会顺带建一次性提醒。",
        _schema({
            "text": {"type": "string", "description": "待办内容"},
            "due": {"type": "string", "description": due_desc},
        }, ["text"]),
        lambda a: engine.add(owner, a.get("text", ""), a.get("due", "")),
    )
    ctx.tools.register(
        "todo_list",
        "查看待办清单，默认只列未完成的。",
        _schema({"include_done": {"type": "boolean", "description": "是否连已完成的也列出来"}}),
        lambda a: engine.list(owner, bool(a.get("include_done"))),
    )
    ctx.tools.register(
        "todo_update",
        "修改待办的文字或到期时间；传空 due 表示清掉时间并撤销提醒。",
        _schema({
            "key": {"type": "string", "description": "待办编号（支持前缀）或关键词"},
            "text": {"type": "string", "description": "新的内容，不改就别传"},
            "due": {"type": "string", "description": "新的到期时间；空字符串表示清空"},
        }, ["key"]),
        lambda a: engine.update(owner, a.get("key", ""), a.get("text"), a.get("due")),
    )
    ctx.tools.register(
        "todo_done",
        "把一条待办标记为完成，并撤销它挂的提醒。",
        _schema({"key": {"type": "string", "description": "待办编号（支持前缀）或关键词"}}, ["key"]),
        lambda a: engine.done(owner, a.get("key", "")),
    )
    ctx.tools.register(
        "todo_delete",
        "删除一条待办，并撤销它挂的提醒。",
        _schema({"key": {"type": "string", "description": "待办编号（支持前缀）或关键词"}}, ["key"]),
        lambda a: engine.delete(owner, a.get("key", "")),
    )
    ctx.tools.register(
        "todo_clear_done",
        "一次性清掉所有已完成的待办。",
        _schema({}),
        lambda a: engine.clear_done(owner),
    )

    ctx.log.info("todo service registered (owner=%s)", owner or "-")
    ctx.on_unload(lambda: ctx.log.info("todo plugin unloaded"))
