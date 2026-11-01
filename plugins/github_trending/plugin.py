"""GitHub Trending digest and repository evaluation plugin."""
from __future__ import annotations

from registry import Job

from .service import TrendingService, next_daily_ts, parse_daily_at


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def apply(ctx) -> None:
    service = TrendingService(ctx)
    ctx.provide("github_trending", service)

    ctx.tools.register(
        "trending_now",
        "获取今天的 GitHub Trending 简报。用户问“今天 GitHub trending / 每日开源项目 / 现在发一份”时调用；refresh=true 强制重新抓取。",
        _schema({"refresh": {"type": "boolean"}}),
        lambda a: service.preview(refresh=bool(a.get("refresh"))),
        aliases=("trending_now",),
    )
    ctx.tools.register(
        "trending_list",
        "列出最近一份 GitHub Trending 简报的项目和序号，用于把“评估 1/3”解析成具体仓库。",
        _schema({}),
        lambda a: service.latest_list(),
        aliases=("trending_list",),
    )
    ctx.tools.register(
        "trending_evaluate",
        "获取指定 GitHub 项目的元数据、README、目录和依赖清单，供你评估是否值得借鉴或接入。用户点名 trending 项目、问“值得借鉴吗/要不要接入”时调用；index 来自最近简报，repo 可传 owner/name 或 GitHub URL。",
        _schema({
            "repo": {"type": "string", "description": "owner/name 或 GitHub URL"},
            "index": {"type": "integer", "minimum": 1, "description": "最近一份简报里的序号"},
            "focus": {"type": "string", "enum": ["both", "borrow", "integrate"]},
        }),
        lambda a: service.evaluate_for_tool(a),
        aliases=("trending_evaluate",),
    )

    options = (ctx.root_config.get("jobs") or {}).get("github_trending") or {}
    if options.get("enabled", True):
        daily_at = str(options.get("daily_at") or "09:00")
        hour, minute = parse_daily_at(daily_at)
        store = ctx.resource("store")
        next_key = "job:github_trending:next"
        marker_key = "job:github_trending:daily_at"
        if store.state_get(marker_key) != daily_at or not store.state_get(next_key):
            store.state_set(marker_key, daily_at)
            store.state_set(next_key, str(next_daily_ts(hour, minute)))
        ctx.jobs.register(Job(
            id="github_trending",
            interval_minutes=int(options.get("interval_minutes", 1440)),
            handler=service.daily,
            enabled=True,
            skip_empty=False,
            window_minutes=int(options.get("window_minutes", 1440)),
            meta={"type": "report", "plugin": "github_trending"},
        ))
        ctx.log.info("registered github_trending daily at %s", daily_at)

    ctx.on_unload(lambda: ctx.log.info("github_trending plugin unloaded"))
