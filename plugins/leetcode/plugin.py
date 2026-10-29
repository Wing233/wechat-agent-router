"""LeetCode capability plugin: stats tool, credential service and login page."""
from __future__ import annotations

from .adapter import LeetCode

from registry import Job


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def apply(ctx) -> None:
    adapter = LeetCode(
        store=ctx.resource("store"),
        fernet_key=ctx.secret("credential_key"),
        notify=ctx.send,
        login_url=ctx.login_url,
    )
    ctx.provide("leetcode", adapter)
    ctx.provide("credential:leetcode", adapter)

    ctx.tools.register(
        "leetcode_stats",
        "查询站主力扣（leetcode.cn）做题数据：通过题数（简单/中等/困难）、排名、最近提交。",
        _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 20}}),
        lambda a: adapter.get_stats(int(a.get("limit") or 10)),
        aliases=("leetcode_stats",),
    )

    options = (ctx.root_config.get("jobs") or {}).get("leetcode_check", {})
    if options.get("enabled", True):
        interval = int(options.get("interval_minutes", 360))

        def check_cookie() -> dict:
            data = adapter.get_stats(5)
            return {"login_status": data.get("login_status"),
                    "username": data.get("username")}

        ctx.jobs.register(Job(
            id="leetcode_check",
            interval_minutes=interval,
            handler=check_cookie,
            enabled=True,
            skip_empty=False,
            window_minutes=int(options.get("window_minutes", 0) or 0),
            meta={"type": "service", "plugin": "leetcode"},
        ))
        ctx.log.info("registered leetcode_check every %s minutes", interval)

    ctx.log.info("leetcode adapter registered")
    ctx.on_unload(lambda: ctx.log.info("leetcode plugin unloaded"))
