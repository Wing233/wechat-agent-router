"""Reminder capability plugin: tools plus the tick service used by the kernel."""
from __future__ import annotations

from .engine import ReminderEngine


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def apply(ctx) -> None:
    engine = ReminderEngine(ctx.resource("store"), ctx.send)
    ctx.provide("reminders", engine)

    ctx.tools.register(
        "create_reminder",
        "创建提醒。用户说“提醒我/到点提醒/每X分钟提醒”时调用。一次性时间用带时区的 ISO（如 2026-11-04T12:00:00+08:00）；循环用 cron；间隔用 interval_minutes。",
        _schema({
            "text": {"type": "string", "description": "提醒内容，例如“喝水”“报名普通话考试”"},
            "schedule_type": {"type": "string", "enum": ["once", "interval", "cron"]},
            "at": {"type": "string", "description": "once 时必填，ISO 时间，带 +08:00"},
            "interval_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "description": "interval 时必填"},
            "cron": {"type": "string", "description": "cron 时必填，5 段：分 时 日 月 周"},
            "max_occurrences": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "可选，最多提醒几次"},
            "end_at": {"type": "string", "description": "可选，重复提醒的结束时间，ISO"},
        }, ["text", "schedule_type"]),
        lambda a: engine.create(
            chat_id=ctx.config.get("owner") or ctx.root_config.get("owner") or "",
            text=a.get("text", ""), schedule_type=a.get("schedule_type", "once"),
            at=a.get("at", ""), interval_minutes=int(a.get("interval_minutes") or 0),
            cron=a.get("cron", ""), max_occurrences=a.get("max_occurrences"),
            end_at=a.get("end_at", ""),
        ),
        aliases=("create_reminder",),
    )
    ctx.tools.register(
        "list_reminders",
        "查看当前还未完成的提醒（active/paused）。",
        _schema({}),
        lambda a: {"items": engine.list(ctx.config.get("owner") or ctx.root_config.get("owner") or "")},
        aliases=("list_reminders",),
    )
    ctx.tools.register(
        "cancel_reminder",
        "按提醒 id 或文本关键词取消提醒；如果有多个匹配会返回候选，需要再问用户。",
        _schema({"key": {"type": "string", "description": "提醒 id 或文本关键词"}}, ["key"]),
        lambda a: engine.cancel(ctx.config.get("owner") or ctx.root_config.get("owner") or "", a.get("key", "")),
        aliases=("cancel_reminder",),
    )
    ctx.tools.register(
        "pause_reminder",
        "暂停一条提醒；key 是提醒 id 或文本关键词。",
        _schema({"key": {"type": "string"}}, ["key"]),
        lambda a: engine.pause(ctx.config.get("owner") or ctx.root_config.get("owner") or "", a.get("key", "")),
        aliases=("pause_reminder",),
    )
    ctx.tools.register(
        "resume_reminder",
        "恢复一条暂停的提醒；key 是提醒 id 或文本关键词。",
        _schema({"key": {"type": "string"}}, ["key"]),
        lambda a: engine.resume(ctx.config.get("owner") or ctx.root_config.get("owner") or "", a.get("key", "")),
        aliases=("resume_reminder",),
    )
    ctx.tools.register(
        "snooze_reminder",
        "把某条提醒推迟 N 分钟；key 是提醒 id 或文本关键词。",
        _schema({"key": {"type": "string"}, "minutes": {"type": "integer", "minimum": 1, "maximum": 1440}},
                ["key", "minutes"]),
        lambda a: engine.snooze(ctx.config.get("owner") or ctx.root_config.get("owner") or "",
                                a.get("key", ""), int(a.get("minutes") or 10)),
        aliases=("snooze_reminder",),
    )

    ctx.log.info("reminder service registered")
    ctx.on_unload(lambda: ctx.log.info("reminders plugin unloaded"))
