"""Deterministic inbound gate and high-risk tool mandate policy.

This module deliberately contains no model calls. Classification and authority
checks must be auditable code: the LLM may describe an action, but only this
module decides whether an action is allowed to run.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

POLICY_VERSION = "2026-11-01.1"

READ_ONLY = "read_only"
ACTION_EXPLICIT = "action_explicit"
ACTION_INFERRED = "action_inferred"
IGNORE = "ignore"
CLASSIFICATIONS = (READ_ONLY, ACTION_EXPLICIT, ACTION_INFERRED, IGNORE)

# Tools that are safe to expose during a read-only turn. Unknown tools are
# denied by default so a newly installed plugin cannot silently mutate state.
READ_ONLY_TOOLS = {
    "site__chat_stats",
    "site__visit_stats",
    "site__realtime",
    "site__visitor_detail",
    "site__chat_messages",
    "site__preset_questions",
    "leetcode__leetcode_stats",
    "demo_time__now",
    "demo_weather__now",
    "websearch__web_search",
    "websearch__web_fetch",
    "websearch__weather",
    "websearch__calculator",
    "deepseek_balance__deepseek_balance",
    "admin__api_balance",
    "admin__config_get",
    "admin__get_job",
    "admin__list_jobs",
    "admin__list_plugins",
    "admin__list_tools",
    "list_channels",
    "model_list",
    "model_test",
    "cost_report",
    "trace_search",
    "trace_get",
    "trace_explain",
    "eval_cases",
    "eval_report",
    "eval_drift",
    "insight_report",
    "optimize_suggest",
    "reminders__list_reminders",
    "todo__todo_list",
    "toolbox__memory_search",
    "toolbox__load_skill",
    "toolbox__sql_query",
    "toolbox__mcp_list_tools",
    "toolbox__browser_fetch_page",
    "toolbox__confirm_action",
    "toolbox__governance_report",
    "ops__fs_list",
    "ops__fs_read",
    "ops__list_dir",
    "ops__read_file",
}
_READ_ONLY_SUFFIXES = {name.split("__")[-1] for name in READ_ONLY_TOOLS}

# High-risk tools always require a mandate, even when the inbound message was
# classified as an explicit owner action. The registry enforces this before a
# handler is allowed to run.
HIGH_RISK_TOOLS = {
    "toolbox__python_repl",
    "toolbox__run_sandbox",
    "toolbox__run_docker_sandbox",
    "toolbox__write_patch",
    "toolbox__snapshot_checkpoint",
    "toolbox__delegate_to_subagent",
    "toolbox__webhook_post",
    "toolbox__slack_post",
    "toolbox__mcp_call",
    "toolbox__browser_click_fill",
    "toolbox__github_api",
    "devtools__execute_bash",
    "devtools__run_command",
    "devtools__http_request",
    "ops__fs_write",
    "ops__fs_replace",
    "ops__fs_delete",
    "ops__fs_move",
    "admin__config_set",
    "admin__enable_plugin",
    "admin__disable_plugin",
    "admin__reload_plugin",
}
_HIGH_RISK_SUFFIXES = {name.split("__")[-1] for name in HIGH_RISK_TOOLS}

_HASH_IGNORED_KEYS = {"mandate", "confirm_code", "_mandate", "timeout", "wait_ms"}

_READ_VERBS = (
    "查", "查询", "查看", "看看", "看一下", "列出", "显示", "统计",
    "多少", "几个", "什么", "谁", "哪", "怎么", "如何", "为什么",
    "是否", "有没有", "状态", "情况", "报告", "分析", "总结", "介绍",
    "解释", "搜索", "搜", "读", "读取", "打开", "浏览", "对比", "比较",
    "计算", "知道", "了解",
)
_STRONG_VERBS = (
    "删除", "删掉", "删", "移除", "重启", "部署", "发布", "上线",
    "执行", "运行", "跑", "发送", "发给", "发我", "发到", "写入", "写",
    "修改", "改", "设置", "设为", "创建", "新建", "生成", "安装", "卸载",
    "启用", "禁用", "重载", "回滚", "恢复", "上传", "下载", "提交",
    "推送", "合并", "停止", "停", "启动", "清空", "移动", "复制",
    "重命名", "编译", "测试", "抓取", "点击", "填表", "登录", "批准",
    "同意", "拒绝", "撤销", "取消", "保存", "替换", "每天", "每隔",
    "定时", "提醒我", "通知我",
)
_WEAK_VERBS = (
    "增加", "优化", "修复", "实现", "支持", "接入", "同步", "备份",
    "监控", "加", "调整", "整理", "清理", "检查", "排查", "处理", "弄", "搞",
)
_SUGGESTION_MARKERS = (
    "应该", "建议", "最好", "可以考虑", "或许", "也许", "要是", "如果",
    "希望能", "需要加", "得有", "缺个", "缺少", "可以加", "改进",
    "优化", "修复", "实现", "接入", "同步", "该加",
)
_QUESTION_SUGGESTION_MARKERS = (
    "能不能", "可不可以", "要不要", "是不是", "是否",
)
_ACTION_NOUNS = (
    "功能", "提醒", "定时", "任务", "插件", "接口", "api", "脚本", "配置",
    "文件", "目录", "部署", "服务", "机器人", "bot", "播报", "报告",
    "监控", "备份", "日志", "测试", "补丁", "代码", "页面", "按钮",
    "表单", "模型", "预算", "仓库", "分支", "提交", "pr", "issue",
    "计划", "流程", "规则", "权限", "审批", "记忆", "技能", "间隔",
)
_QUESTION_MARKERS = (
    "吗", "呢", "？", "?", "多少", "几", "什么", "谁", "哪", "怎么",
    "如何", "为什么", "是否", "有没有", "能不能", "可不可以", "要不要",
    "是不是",
)
_INTENT_PREFIXES = (
    "帮我", "给我", "替我", "请", "麻烦", "立刻", "马上", "现在", "去",
    "我想", "我要", "我需要", "把",
)
_NEGATIVE_PREFIXES = ("别", "不要", "不用", "无需", "不需要")


@dataclass(frozen=True)
class Decision:
    classification: str
    reason: str
    rule: str
    would_accept: bool
    executed: bool
    explicit_agent: str = ""
    scores: dict[str, Any] = field(default_factory=dict)


def _scores(text: str) -> dict[str, Any]:
    """Small deterministic scorer; no model and no randomness."""
    t = (text or "").lower().strip()
    read = 0
    strong = 0
    weak = 0
    inferred = 0
    matched: list[str] = []
    for word in _READ_VERBS:
        if word in t:
            read += 2
            matched.append(f"read:{word}")
    for word in _STRONG_VERBS:
        if word in t:
            strong += 3
            matched.append(f"strong:{word}")
    for word in _WEAK_VERBS:
        if word in t:
            weak += 1
            matched.append(f"weak:{word}")
    for word in _SUGGESTION_MARKERS:
        if word in t:
            inferred += 2
            matched.append(f"suggest:{word}")
    for word in _QUESTION_SUGGESTION_MARKERS:
        if word in t:
            inferred += 1
            matched.append(f"question-suggest:{word}")
    for word in _ACTION_NOUNS:
        if word in t:
            inferred += 1
            matched.append(f"noun:{word}")
    question = any(word in t for word in _QUESTION_MARKERS)
    for word in _QUESTION_MARKERS:
        if word in t:
            read += 1
            matched.append(f"question:{word}")
    starts_intent = any(t.startswith(word) for word in _INTENT_PREFIXES)
    starts_negative = any(t.startswith(word) for word in _NEGATIVE_PREFIXES)
    starts_action = any(t.startswith(word) for word in (*_STRONG_VERBS, *_WEAK_VERBS))
    if starts_intent:
        weak += 2
        matched.append("prefix:intent")
    if starts_negative:
        strong += 3
        matched.append("prefix:negative")
    if starts_action and not question:
        strong += 2
        matched.append("prefix:action")
    if question and read == 0 and strong == 0 and weak == 0:
        read += 1
        matched.append("question:default")
    return {
        "read": read,
        "strong": strong,
        "weak": weak,
        "explicit": strong + weak,
        "inferred": inferred,
        "question": int(question),
        "matched": matched,
    }


def _decide_owner(text: str) -> tuple[str, str, dict[str, Any]]:
    s = _scores(text)
    explicit = int(s["explicit"])
    inferred = int(s["inferred"])
    read = int(s["read"])
    question = bool(s["question"])
    strong = int(s["strong"])
    suggestion = any(word in text.lower() for word in _SUGGESTION_MARKERS)
    starts_intent = any(text.lower().startswith(word) for word in _INTENT_PREFIXES)

    # A question that contains an action verb is not permission to execute it.
    if question and strong > 0:
        if explicit >= 7:
            return ACTION_EXPLICIT, "疑问句但指令很强，按明确动作处理", s
        return ACTION_INFERRED, "疑问句里的动作请求，只记录不执行", s
    if inferred >= 2 and explicit < 5 and (explicit > 0 or suggestion or starts_intent):
        return ACTION_INFERRED, "建议或可能性表达，只记录不执行", s
    if explicit >= 3:
        return ACTION_EXPLICIT, "明确的动作指令", s
    if read >= 2:
        return READ_ONLY, "只读查询或一般问答", s
    return READ_ONLY, "默认按只读问答处理", s


def classify_inbound(
    text: str,
    origin: dict | None = None,
    *,
    has_attachments: bool = False,
    explicit_agent: tuple[str, str] | None = None,
    pending_reply: bool = False,
) -> Decision:
    """Classify one inbound message into the four gate buckets.

    The default is deliberately conservative: ambiguous owner messages are
    treated as read-only, and ambiguous action requests are recorded as
    inferred instead of being executed.
    """
    raw = (text or "").strip()
    role = str((origin or {}).get("role") or "owner")

    if pending_reply:
        return Decision(ACTION_EXPLICIT, "审批回复", "pending_reply", True, True)
    if explicit_agent:
        agent_id = explicit_agent[0]
        if role == "owner":
            return Decision(ACTION_EXPLICIT, f"明确 @{agent_id}", "explicit_agent",
                            True, True, agent_id)
        return Decision(IGNORE, "访客不能派 Agent 任务", "guest_agent",
                        False, False, agent_id)
    if not raw:
        if has_attachments:
            return Decision(READ_ONLY, "仅附件，按只读问答处理", "attachment_only",
                            True, True)
        return Decision(IGNORE, "空消息", "empty", False, False)

    scores = _scores(raw)
    if role != "owner":
        if int(scores["explicit"]) >= 3 or (
                int(scores["inferred"]) >= 2 and int(scores["explicit"]) > 0):
            return Decision(IGNORE, "访客不能执行动作", "guest_action",
                            False, False, scores=scores)
        return Decision(READ_ONLY, "访客只读问答", "guest_read", True, True,
                        scores=scores)

    classification, reason, scores = _decide_owner(raw)
    return Decision(
        classification=classification,
        reason=reason,
        rule="owner_rules",
        would_accept=classification != IGNORE,
        executed=classification in {READ_ONLY, ACTION_EXPLICIT},
        scores=scores,
    )


def is_read_only_tool(name: str) -> bool:
    return name in READ_ONLY_TOOLS or name.split("__")[-1] in _READ_ONLY_SUFFIXES


def read_only_tools(available: set[str] | list[str] | tuple[str, ...]) -> set[str]:
    return {name for name in available if is_read_only_tool(name)}


def tool_allowed(name: str, classification: str, role: str,
                 allowed: set[str] | None = None) -> bool:
    if not classification:
        return True
    if classification == READ_ONLY:
        return is_read_only_tool(name)
    if classification == ACTION_EXPLICIT:
        return role == "owner" or (allowed is not None and name in allowed)
    return False


def is_high_risk_tool(name: str) -> bool:
    return name in HIGH_RISK_TOOLS or name.split("__")[-1] in _HIGH_RISK_SUFFIXES


def mandate_required(name: str, args: dict | None = None) -> bool:
    if not is_high_risk_tool(name):
        return False
    arguments = args or {}
    suffix = name.split("__")[-1]
    if suffix == "snapshot_checkpoint":
        return str(arguments.get("action") or "create").lower() == "restore"
    if suffix == "github_api":
        return str(arguments.get("method") or "GET").upper() not in {
            "GET", "HEAD", "OPTIONS",
        }
    return True


def args_hash(args: dict | None) -> str:
    cleaned = {
        key: value for key, value in (args or {}).items()
        if key not in _HASH_IGNORED_KEYS
    }
    raw = json.dumps(cleaned, ensure_ascii=False, sort_keys=True,
                     default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_MANDATE_STORE = None


def set_mandate_store(store) -> None:
    """Register the toolbox service that owns the confirmations table."""
    global _MANDATE_STORE
    _MANDATE_STORE = store


def check_mandate(name: str, args: dict | None) -> dict | None:
    """Return None when no mandate is needed, otherwise a guard result.

    On success the result contains cleaned ``args`` (with the mandate argument
    removed) so the registry can pass them to the real handler.
    """
    arguments = dict(args or {})
    if not mandate_required(name, arguments):
        return None
    if _MANDATE_STORE is None:
        return {
            "error": "审批服务未加载，高危工具已拒绝执行",
            "required": True,
            "hint": "确认 toolbox 插件已加载后重试",
        }
    result = _MANDATE_STORE.require_mandate(name, arguments)
    if result.get("error"):
        return result
    return {"args": result.get("args", arguments), "mandate": result}


def extract_approval_code(text: str) -> str:
    """Extract an approval code from a free-form owner reply."""
    match = re.search(r"\b([A-Z2-9]{4,8})\b", (text or "").upper())
    return match.group(1) if match else ""
