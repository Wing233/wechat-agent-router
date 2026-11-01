"""Deterministic help/capability catalog for chat windows.

The assistant should not invent what it can do. ``help`` and "你能做什么"
return a fixed, tool-aware list with examples, filtered by role and by the
tools that are actually loaded.
"""
from __future__ import annotations

import re
from typing import Iterable

_HELP_WORDS = {
    "help", "/help", "h", "hepl", "hlep", "halp", "helpme",
    "帮助", "帮助中心", "说明书", "使用说明", "使用帮助",
    "命令", "命令列表", "有哪些命令", "能力", "能力列表",
    "功能", "功能列表", "功能清单", "能力清单", "命令帮助", "使用方式",
    "你能做什么", "你会做什么", "能做什么",
    "你能干啥", "你会干啥", "能干啥", "会什么",
    "有什么功能", "有哪些功能", "有什么能力", "有哪些能力",
    "怎么用", "如何使用", "能帮我做什么",
}


def is_help_request(text: str) -> bool:
    """Return True for short help/ability questions, not for normal requests."""
    raw = str(text or "").strip()
    probe = re.sub(r"[\s!！。.,，、?？~～:：]+", "", raw.lower())
    if not probe and raw in {"?", "？"}:
        return True
    if probe in _HELP_WORDS:
        return True
    if len(probe) <= 24 and probe.endswith(
            ("能做什么", "会做什么", "能干啥", "会干啥", "有哪些功能",
             "有什么功能", "有哪些能力", "有什么能力", "能帮我做什么")):
        return True
    return False


def _has(tools: set[str], name: str) -> bool:
    if name in tools:
        return True
    return any(str(item).split("__", 1)[-1] == name for item in tools)


def _has_prefix(tools: set[str], prefix: str) -> bool:
    return any(str(item).split("__", 1)[-1].startswith(prefix) for item in tools)


def _agent_ids(agents) -> list[str]:
    if not agents:
        return []
    items = agents.keys() if isinstance(agents, dict) else agents
    result = []
    for item in items:
        text = str(item or "")
        if text and text != "echo" and text not in result:
            result.append(text)
    return sorted(result)


def build_help(*, role: str = "owner", channel: str = "", endpoint: str = "",
               chat_type: str = "", tool_names: Iterable[str] | None = None,
               agents=None, plugins: Iterable[str] | None = None,
               model_bindings: dict | None = None) -> str:
    tools = {str(item) for item in (tool_names or [])}
    owner = str(role or "owner") == "owner"
    place = str(channel or "微信").strip() or "微信"
    if endpoint:
        place += f" · {endpoint}"
    if chat_type:
        place += f" · {chat_type}"
    lines = ["微桥能力总览", f"当前入口：{place} · 身份：{'主人' if owner else '访客'}"]

    if not owner:
        lines += [
            "",
            "【你可以】",
            "• 问公开数据、站点统计、力扣统计",
            "• 搜网页、读网页、查时间/天气/算数",
            "• 让机器人解释、总结、改写",
            "• 群聊里 @ 机器人",
            "",
            "【不能】",
            "• 改模型/预算、看成本/链路/评测",
            "• 读写文件、执行命令、跨入口发送",
            "• 驱动本机 Agent",
            "",
            "直接说“help”随时回到这份清单。",
        ]
        return "\n".join(lines)

    def add(title: str, items: list[tuple[str, str, str]]) -> None:
        rows = [f"• {example} —— {label}" for example, label, need in items
                if _has(tools, need)]
        if rows:
            lines.extend(["", f"【{title}】", *rows])

    add("先看这些", [
        ("help / 你能做什么", "show_help", "show_help"),
        ("现在用的什么模型？", "model_list", "model_list"),
        ("这个月花了多少？", "cost_report", "cost_report"),
        ("今天整体怎么样？", "insight_report", "insight_report"),
    ])
    add("模型与成本", [
        ("加一个免费模型：地址 …，key …，模型 …", "model_add", "model_add"),
        ("把 chat 切到 xxx", "model_switch_chat", "model_switch_chat"),
        ("让 job.default 先用免费模型，失败再用主模型", "model_bind", "model_bind"),
        ("测一下 xxx 通不通", "model_test", "model_test"),
        ("删掉 xxx 模型", "model_remove", "model_remove"),
        ("访客每天最多花 1 块", "budget_set", "budget_set"),
    ])
    add("排查与质量", [
        ("刚才那条为什么失败？", "trace_search + trace_explain", "trace_search"),
        ("查一下 trace xxxx", "trace_get", "trace_get"),
        ("跑一下评测", "eval_run", "eval_run"),
        ("最近模型是不是变差了？", "eval_drift", "eval_drift"),
        ("加一条评测：……", "eval_add_case", "eval_add_case"),
        ("帮我看看哪里还能省/哪里不稳", "optimize_suggest", "optimize_suggest"),
        ("按第一条建议改", "optimize_apply", "optimize_apply"),
    ])
    add("消息与入口", [
        ("查看已接入入口", "list_channels", "list_channels"),
        ("把这条发到 QQ 群：……", "send_to_channel", "send_to_channel"),
        ("把 /workspace/a.png 发给我", "send_media", "send_media"),
        ("画一张……", "generate_image", "generate_image"),
    ])
    add("提醒与待办", [
        ("明天 9 点提醒我……", "create_reminder", "create_reminder"),
        ("每天 8 点……", "schedule_cron", "schedule_cron"),
        ("待办：……", "todo_add", "todo_add"),
        ("查看待办 / 完成第 2 条", "todo_*", "todo_list"),
    ])
    add("服务器与网络", [
        ("看 /app 下有什么", "fs_list", "fs_list"),
        ("读 / 写 / 替换 / 删除文件", "fs_*", "fs_list"),
        ("执行命令：……", "run_command", "run_command"),
        ("搜一下…… / 打开这个网页", "web_search / web_fetch", "web_search"),
        ("调这个 API：……", "http_request", "http_request"),
    ])
    add("Agent", [
        ("@pc-home 帮我……", "send_to_agent", "send_to_agent"),
    ])

    agent_ids = _agent_ids(agents)
    if agent_ids:
        lines.extend(["", "【可用 Agent】", "• " + "、".join(agent_ids)])
    if plugins:
        names = sorted({str(item) for item in plugins if str(item)})
        if names:
            lines.extend(["", "【当前插件】", "• " + "、".join(names)])
    if model_bindings:
        chat = model_bindings.get("chat") or []
        if chat:
            lines.extend(["", "【当前 chat】", "• " + " → ".join(str(item) for item in chat)])

    lines.extend(["", "直接说“help”随时回到这份清单。"])
    return "\n".join(lines)
