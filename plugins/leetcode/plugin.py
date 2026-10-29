"""LeetCode capability plugin: stats tool, credential service and login page."""
from __future__ import annotations

from .adapter import LeetCode


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

    ctx.log.info("leetcode adapter registered")
    ctx.on_unload(lambda: ctx.log.info("leetcode plugin unloaded"))
