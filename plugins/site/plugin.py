"""Site data capabilities: read-only tools and the periodic report job."""
from __future__ import annotations

from registry import Job

from .report import build_site_report, send_long


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def apply(ctx) -> None:
    site = ctx.resource("site")

    ctx.tools.register(
        "visit_stats",
        "查询 lyph 站点访问统计（PV/UV/会话数），可按小时/天/页面/来源/设备分组。",
        _schema({
            "range": {"type": "string", "enum": ["today", "yesterday", "last_24h", "last_7d", "last_30m", "this_hour", "custom"]},
            "group_by": {"type": "string", "enum": ["none", "hour", "day", "path", "source", "device"]},
            "start": {"type": "string", "description": "custom 时的开始时间，ISO 格式"},
            "end": {"type": "string", "description": "custom 时的结束时间，ISO 格式"},
        }, ["range"]),
        lambda a: site.visit_stats(a.get("range", "today"), a.get("group_by", "none"),
                                   start=a.get("start", ""), end=a.get("end", "")),
        aliases=("visit_stats",),
    )
    ctx.tools.register(
        "visitor_detail",
        "列出访客明细：匿名编号、IP、归属地、设备、来源、浏览路径、新老访客。",
        _schema({
            "range": {"type": "string", "enum": ["today", "yesterday", "last_24h", "last_7d", "last_30m", "this_hour"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, ["range"]),
        lambda a: site.visitor_detail(a.get("range", "today"), int(a.get("limit") or 20)),
        aliases=("visitor_detail",),
    )
    ctx.tools.register(
        "chat_messages",
        "查询访客和 lyph AI 的对话，包含问题、回答、访客 IP 与归属地。",
        _schema({
            "range": {"type": "string", "enum": ["today", "yesterday", "last_24h", "last_7d", "last_30m", "this_hour"]},
            "keyword": {"type": "string", "description": "可选关键词"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, ["range"]),
        lambda a: site.chat_messages(a.get("range", "today"), a.get("keyword", ""), int(a.get("limit") or 20)),
        aliases=("chat_messages",),
    )
    ctx.tools.register(
        "chat_stats",
        "对话统计：对话数、请求数、完成/失败、热门问题。",
        _schema({"range": {"type": "string", "enum": ["today", "yesterday", "last_24h", "last_7d", "last_30m", "this_hour"]}}, ["range"]),
        lambda a: site.chat_stats(a.get("range", "today")),
        aliases=("chat_stats",),
    )
    ctx.tools.register(
        "realtime",
        "最近 N 分钟的访问和对话概览，适合“刚刚/现在”类问题。",
        _schema({"minutes": {"type": "integer", "minimum": 1, "maximum": 1440}}, ["minutes"]),
        lambda a: site.realtime(int(a.get("minutes") or 30)),
        aliases=("realtime",),
    )

    presets = {
        "default": ["有哪些项目", "你的技术栈", "有哪些文章"],
        "content": ["博客列表", "文档列表", "生活日报列表"],
        "companies": ["只看深圳", "再比较岗位", "这些企业按行业分布"],
    }
    ctx.tools.register(
        "preset_questions",
        "查询 lyph 对话的预设问题/建议问题。用户问“lyph 预设问题有哪些”时调用。",
        _schema({"intent": {"type": "string", "enum": ["default", "content", "companies"]}}),
        lambda a: {"intent": a.get("intent") or "default",
                   "questions": presets.get(a.get("intent") or "default", presets["default"])},
        aliases=("preset_questions",),
    )

    options = (ctx.root_config.get("jobs") or {}).get("site_report", {})
    if options.get("enabled", True):
        interval = int(options.get("interval_minutes", 30))
        window = int(options.get("window_minutes", interval))

        def run_report() -> dict | None:
            text = build_site_report(site, window)
            if text is None:
                return None
            send_long(text, ctx.send)
            return {"chars": len(text)}

        ctx.jobs.register(Job(
            id="site_report",
            interval_minutes=interval,
            handler=run_report,
            enabled=True,
            skip_empty=True,
            window_minutes=window,
            meta={"type": "report", "plugin": "site"},
        ))
        ctx.log.info("registered site_report every %s minutes (window %s)", interval, window)

    def dispose() -> None:
        ctx.log.info("site plugin unloaded")

    ctx.on_unload(dispose)
