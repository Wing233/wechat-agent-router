"""Persistent reminder engine: once / interval / cron, with dedupe and restart safety."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

CST = ZoneInfo("Asia/Shanghai")
log = logging.getLogger("reminders")


class ReminderError(Exception):
    pass


def _parse(value: str) -> datetime:
    text = (value or "").strip().replace("Z", "+00:00")
    if not text:
        raise ReminderError("缺少时间")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ReminderError(f"时间格式无效：{value}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return _parse(iso).astimezone(CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


class ReminderEngine:
    def __init__(self, store, send_text, min_interval_minutes: int = 1, max_active: int = 100):
        self.store = store
        self.send_text = send_text
        self.min_interval = max(1, min_interval_minutes)
        self.max_active = max(1, max_active)

    # ------------------------------------------------------------------
    def create(self, *, chat_id: str, text: str, schedule_type: str,
               at: str = "", interval_minutes: int = 0, cron: str = "",
               max_occurrences: int | None = None, end_at: str = "") -> dict:
        text = (text or "").strip()
        if not text:
            raise ReminderError("提醒内容不能为空")
        if schedule_type not in {"once", "interval", "cron"}:
            raise ReminderError("schedule_type 必须是 once / interval / cron")
        if self.store.count_active_reminders(chat_id) >= self.max_active:
            raise ReminderError(f"提醒太多了（上限 {self.max_active} 条），先清理一些")

        now = datetime.now(timezone.utc)
        schedule: dict = {}
        remaining = None
        if max_occurrences is not None:
            try:
                remaining = int(max_occurrences)
            except (TypeError, ValueError):
                raise ReminderError("max_occurrences 必须是整数") from None
            if remaining < 1 or remaining > 1000:
                raise ReminderError("max_occurrences 应在 1–1000 之间")

        if schedule_type == "once":
            when = _parse(at)
            if when <= now:
                raise ReminderError("这个时间已经过去了，请说一个将来的时间")
            if when > now + timedelta(days=730):
                raise ReminderError("一次性提醒不要超过两年")
            schedule = {"at": when.isoformat()}
            next_run = _utc_iso(when)
            remaining = 1
        elif schedule_type == "interval":
            try:
                minutes = int(interval_minutes)
            except (TypeError, ValueError):
                raise ReminderError("interval_minutes 必须是整数") from None
            if minutes < self.min_interval or minutes > 10080:
                raise ReminderError(f"间隔应在 {self.min_interval}–10080 分钟之间")
            schedule = {"interval_minutes": minutes}
            next_run = _utc_iso(now + timedelta(minutes=minutes))
        else:
            cron = (cron or "").strip()
            if len(cron.split()) != 5:
                raise ReminderError("cron 必须是 5 段：分 时 日 月 周")
            try:
                trigger = CronTrigger.from_crontab(cron, timezone=CST)
                nxt = trigger.get_next_fire_time(None, now.astimezone(CST))
            except Exception as exc:
                raise ReminderError(f"cron 表达式无效：{exc}") from None
            if nxt is None:
                raise ReminderError("这个 cron 永远不会触发")
            schedule = {"cron": cron}
            next_run = _utc_iso(nxt)

        end_iso = None
        if end_at and schedule_type != "once":
            end_dt = _parse(end_at)
            if end_dt <= now:
                raise ReminderError("结束时间必须是将来的时间")
            end_iso = _utc_iso(end_dt)
            if _parse(next_run) > end_dt:
                raise ReminderError("第一次触发时间已经晚于结束时间")

        reminder = self.store.create_reminder(
            chat_id=chat_id, text=text, schedule_type=schedule_type,
            schedule=schedule, timezone_name="Asia/Shanghai",
            next_run_at=next_run, remaining=remaining, end_at=end_iso,
        )
        return {"ok": True, "id": reminder["id"], "summary": self.describe(reminder),
                "next": _local(next_run), "reminder": reminder}

    # ------------------------------------------------------------------
    def _next(self, reminder: dict, scheduled_iso: str, now: datetime):
        remaining = reminder.get("remaining")
        if remaining is not None:
            remaining = int(remaining) - 1
            if remaining <= 0:
                return None, 0, "done"
        scheduled = _parse(scheduled_iso)
        nxt = None
        if reminder["schedule_type"] == "once":
            return None, remaining, "done"
        if reminder["schedule_type"] == "interval":
            minutes = int(reminder["schedule"].get("interval_minutes") or 0)
            nxt = scheduled + timedelta(minutes=minutes)
            if nxt <= now:
                nxt = now + timedelta(minutes=minutes)
        elif reminder["schedule_type"] == "cron":
            trigger = CronTrigger.from_crontab(reminder["schedule"]["cron"], timezone=CST)
            nxt = trigger.get_next_fire_time(scheduled.astimezone(CST), now.astimezone(CST))
            if nxt is None:
                return None, remaining, "done"
        if nxt is None:
            return None, remaining, "done"
        if reminder.get("end_at") and nxt > _parse(reminder["end_at"]):
            return None, remaining, "done"
        return _utc_iso(nxt), remaining, "active"

    def tick(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        fired = []
        for reminder in self.store.due_reminders(_utc_iso(now)):
            scheduled = reminder["next_run_at"]
            if not self.store.claim_reminder_run(reminder["id"], scheduled):
                continue
            status = "sent"
            try:
                ok = self.send_text(f"⏰ 提醒：{reminder['text']}")
                if not ok:
                    status = "queued"
            except Exception as exc:
                status = "error"
                self.store.finish_reminder_run(reminder["id"], scheduled, status, str(exc))
                log.warning("reminder send failed: %s", exc)
                self.store.advance_reminder(
                    reminder["id"], next_run_at=_utc_iso(now + timedelta(minutes=1)),
                    last_run_at=_utc_iso(now), remaining=reminder.get("remaining"), status="active",
                )
                continue
            self.store.finish_reminder_run(reminder["id"], scheduled, status)
            nxt, remaining, state = self._next(reminder, scheduled, now)
            self.store.advance_reminder(
                reminder["id"], next_run_at=nxt, last_run_at=_utc_iso(now),
                remaining=remaining, status=state,
            )
            fired.append({"id": reminder["id"], "text": reminder["text"], "status": status})
        return fired

    # ------------------------------------------------------------------
    def describe(self, reminder: dict) -> str:
        kind = reminder["schedule_type"]
        if kind == "once":
            return f"{_local(reminder.get('next_run_at'))} 提醒你{reminder['text']}"
        if kind == "interval":
            minutes = reminder["schedule"].get("interval_minutes")
            extra = f"，共 {reminder.get('remaining')} 次" if reminder.get("remaining") is not None else ""
            return f"每 {minutes} 分钟提醒你{reminder['text']}{extra}"
        return f"按 cron「{reminder['schedule'].get('cron')}」提醒你{reminder['text']}"

    def list(self, chat_id: str) -> list[dict]:
        items = []
        for reminder in self.store.list_reminders(chat_id):
            items.append({
                "id": reminder["id"],
                "text": reminder["text"],
                "status": reminder["status"],
                "next": _local(reminder.get("next_run_at")),
                "remaining": reminder.get("remaining"),
                "schedule": self.describe(reminder),
                "type": reminder["schedule_type"],
            })
        return items

    def _match(self, chat_id: str, key: str) -> list[dict]:
        key = (key or "").strip()
        if not key:
            return []
        return self.store.find_reminders(chat_id, key)

    def cancel(self, chat_id: str, key: str) -> dict:
        matches = self._match(chat_id, key)
        if not matches:
            raise ReminderError("没找到匹配的提醒")
        if len(matches) > 1:
            return {"ok": False, "need_choice": [
                {"id": item["id"], "text": item["text"], "next": _local(item.get("next_run_at"))}
                for item in matches
            ]}
        self.store.set_reminder_status(matches[0]["id"], "cancelled")
        return {"ok": True, "id": matches[0]["id"], "text": matches[0]["text"]}

    def pause(self, chat_id: str, key: str) -> dict:
        matches = self._match(chat_id, key)
        if not matches:
            raise ReminderError("没找到匹配的提醒")
        if len(matches) > 1:
            return {"ok": False, "need_choice": [{"id": m["id"], "text": m["text"]} for m in matches]}
        self.store.set_reminder_status(matches[0]["id"], "paused")
        return {"ok": True, "id": matches[0]["id"], "text": matches[0]["text"]}

    def resume(self, chat_id: str, key: str) -> dict:
        matches = self._match(chat_id, key)
        if not matches:
            raise ReminderError("没找到匹配的提醒")
        if len(matches) > 1:
            return {"ok": False, "need_choice": [{"id": m["id"], "text": m["text"]} for m in matches]}
        reminder = matches[0]
        now = datetime.now(timezone.utc)
        nxt = reminder.get("next_run_at")
        if reminder["schedule_type"] == "once" and nxt and _parse(nxt) <= now:
            raise ReminderError("这条一次性提醒已经过期，请重新创建")
        if not nxt or _parse(nxt) <= now:
            if reminder["schedule_type"] == "interval":
                nxt = _utc_iso(now + timedelta(minutes=int(reminder["schedule"]["interval_minutes"])))
            elif reminder["schedule_type"] == "cron":
                trigger = CronTrigger.from_crontab(reminder["schedule"]["cron"], timezone=CST)
                fire = trigger.get_next_fire_time(None, now.astimezone(CST))
                nxt = _utc_iso(fire) if fire else None
            else:
                nxt = _utc_iso(now + timedelta(minutes=5))
        self.store.advance_reminder(reminder["id"], next_run_at=nxt, last_run_at=reminder.get("last_run_at") or "",
                                    remaining=reminder.get("remaining"), status="active")
        return {"ok": True, "id": reminder["id"], "text": reminder["text"], "next": _local(nxt)}

    def snooze(self, chat_id: str, key: str, minutes: int) -> dict:
        matches = self._match(chat_id, key)
        if not matches:
            raise ReminderError("没找到匹配的提醒")
        if len(matches) > 1:
            return {"ok": False, "need_choice": [{"id": m["id"], "text": m["text"]} for m in matches]}
        minutes = max(1, min(int(minutes), 1440))
        nxt = _utc_iso(datetime.now(timezone.utc) + timedelta(minutes=minutes))
        self.store.snooze_reminder(matches[0]["id"], nxt)
        return {"ok": True, "id": matches[0]["id"], "text": matches[0]["text"], "next": _local(nxt)}
