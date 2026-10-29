"""Admin capability: answer questions about schedules/plugins and change them at runtime."""
from __future__ import annotations


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _guard(fn):
    def wrapper(args: dict) -> dict:
        try:
            return {"ok": True, "result": fn(args or {})}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}
    return wrapper


def apply(ctx) -> None:
    settings = ctx.resource("settings")

    ctx.tools.register(
        "list_jobs",
        "查询 bot 当前所有定时任务：任务 id、间隔分钟、窗口、所属插件、下次运行时间、是否启用。用户问“这个任务多久跑一次”时调用。",
        _schema({}),
        _guard(lambda a: settings.list_jobs()),
        aliases=("list_jobs",),
    )
    ctx.tools.register(
        "get_job",
        "查询单个定时任务的间隔、窗口、下次运行时间。",
        _schema({"job_id": {"type": "string"}}, ["job_id"]),
        _guard(lambda a: settings.get_job(a.get("job_id", "")) or {"error": "任务不存在"}),
        aliases=("get_job",),
    )
    ctx.tools.register(
        "set_job_interval",
        "修改定时任务的播报间隔（分钟）。修改后会持久化到 config.json、重载所属插件并重建调度。",
        _schema({
            "job_id": {"type": "string", "description": "例如 leetcode_check"},
            "minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
            "window_minutes": {"type": "integer", "minimum": 1, "maximum": 10080,
                               "description": "可选，报告统计窗口；默认等于间隔"},
        }, ["job_id", "minutes"]),
        _guard(lambda a: settings.set_job_interval(
            a.get("job_id", ""), int(a.get("minutes") or 0), a.get("window_minutes"))),
        aliases=("set_job_interval",),
    )
    ctx.tools.register(
        "set_job_enabled",
        "启用或停用一个定时任务。",
        _schema({"job_id": {"type": "string"}, "enabled": {"type": "boolean"}}, ["job_id", "enabled"]),
        _guard(lambda a: settings.set_job_enabled(a.get("job_id", ""), bool(a.get("enabled")))),
        aliases=("set_job_enabled",),
    )
    ctx.tools.register(
        "list_plugins",
        "查看已加载插件的版本、权限、工具、任务和服务。",
        _schema({}),
        _guard(lambda a: settings.plugins()),
        aliases=("list_plugins",),
    )
    ctx.tools.register(
        "list_tools",
        "查看当前 LLM 可调用的全部工具名。",
        _schema({}),
        _guard(lambda a: settings.tool_names()),
        aliases=("list_tools",),
    )
    ctx.tools.register(
        "config_get",
        "查看 bot 当前配置，密钥会脱敏。可查 llm、hub、jobs、agents、plugins、credentials、poll_interval、public_base_url。",
        _schema({"section": {"type": "string", "enum": [
            "all", "llm", "hub", "jobs", "agents", "plugins", "credentials",
            "poll_interval", "public_base_url"]}}),
        _guard(lambda a: settings.config_view(a.get("section", "all"))),
        aliases=("config_get",),
    )
    ctx.tools.register(
        "config_set",
        "修改非敏感配置：poll_interval、jobs.<id>.interval_minutes、jobs.<id>.window_minutes、jobs.<id>.enabled。不能修改任何密钥。",
        _schema({
            "key": {"type": "string", "description": "例如 poll_interval 或 jobs.leetcode_check.interval_minutes"},
            "value": {"type": "string", "description": "新值；数字/布尔也用字符串传入"},
        }, ["key", "value"]),
        _guard(lambda a: settings.config_set(a.get("key", ""), a.get("value"))),
        aliases=("config_set",),
    )
    ctx.tools.register(
        "api_balance",
        "查询 DeepSeek API 账户余额。使用已配置的 API key，不会把 key 暴露给用户。",
        _schema({}),
        _guard(lambda a: settings.api_balance()),
        aliases=("api_balance",),
    )
    ctx.tools.register(
        "reload_plugin",
        "热加载指定插件；修改插件代码后使用。",
        _schema({"plugin_id": {"type": "string"}}, ["plugin_id"]),
        _guard(lambda a: settings.reload_plugin(a.get("plugin_id", ""))),
        aliases=("reload_plugin",),
    )
    ctx.tools.register(
        "enable_plugin",
        "启用指定插件。",
        _schema({"plugin_id": {"type": "string"}}, ["plugin_id"]),
        _guard(lambda a: settings.enable_plugin(a.get("plugin_id", ""))),
        aliases=("enable_plugin",),
    )
    ctx.tools.register(
        "disable_plugin",
        "停用指定插件。",
        _schema({"plugin_id": {"type": "string"}}, ["plugin_id"]),
        _guard(lambda a: settings.disable_plugin(a.get("plugin_id", ""))),
        aliases=("disable_plugin",),
    )

    ctx.log.info("admin service registered")
    ctx.on_unload(lambda: ctx.log.info("admin plugin unloaded"))
