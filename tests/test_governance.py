"""Offline checks for the deterministic inbound gate."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import governance
from store import Store


def classify(text: str, role: str = "owner", **kwargs):
    return governance.classify_inbound(text, {"role": role}, **kwargs)


def test_read_only_queries() -> None:
    for text in (
        "今天站点访问量多少",
        "帮我查一下日志",
        "site_report 间隔是不是太长了",
        "你好",
        "谢谢",
        "部署文档在哪里",
        "重启之后日志在哪看",
        "删除操作有日志吗",
        "改天天气怎么样",
        "帮我看看这个报错怎么改",
        "删除操作会不会有风险",
        "重启和删除哪个更危险",
        "这个操作执行了会怎样",
        "别播报了",
        "先别执行",
        "不要执行",
        "比较删除和重启",
        "分析删除和重启",
        "列出删除和重启的顺序",
        "把删除和重启的顺序列出来",
    ):
        decision = classify(text)
        assert decision.classification == governance.READ_ONLY, (text, decision)


def test_explicit_actions() -> None:
    for text in (
        "帮我重启 router",
        "加个提醒，明天9点",
        "每天早上9点提醒我",
        "把 site_report 的间隔改成 30 分钟",
        "帮我删掉日志",
        "加几个提醒",
        "添加几个监控",
        "把 site_report 的间隔调到 30 分钟",
        "通知大家明天开会",
        "把风险日志删掉",
        "帮我查日志然后删除",
    ):
        decision = classify(text)
        assert decision.classification == governance.ACTION_EXPLICIT, (text, decision)
        assert decision.executed is True


def test_inferred_suggestions() -> None:
    for text in (
        "这个功能应该加个提醒",
        "能不能加个提醒",
        "建议加监控",
        "要不要重启",
    ):
        decision = classify(text)
        assert decision.classification == governance.ACTION_INFERRED, (text, decision)
        assert decision.executed is False
        assert decision.would_accept is True


def test_guest_action_is_ignored() -> None:
    decision = classify("帮我重启 router", role="guest")
    assert decision.classification == governance.IGNORE
    assert decision.executed is False


def test_guest_read_only_is_allowed() -> None:
    for text in ("今天访问量多少", "部署文档在哪里", "删除操作有日志吗",
                 "帮我看看删除日志", "比较删除和重启"):
        decision = classify(text, role="guest")
        assert decision.classification == governance.READ_ONLY, (text, decision)
        assert decision.executed is True


def test_explicit_agent_scope() -> None:
    owner = classify("", explicit_agent=("pc-home", "跑测试"))
    assert owner.classification == governance.ACTION_EXPLICIT
    assert owner.explicit_agent == "pc-home"

    guest = classify("", role="guest", explicit_agent=("pc-home", "跑测试"))
    assert guest.classification == governance.IGNORE


def test_approval_reply_is_explicit() -> None:
    decision = classify("APPROVE 7GSJ", pending_reply=True)
    assert decision.classification == governance.ACTION_EXPLICIT
    assert decision.rule == "pending_reply"

    guest = classify("APPROVE 7GSJ", role="guest", pending_reply=True)
    assert guest.classification == governance.READ_ONLY


def test_attachment_only_is_read_only() -> None:
    decision = classify("", has_attachments=True)
    assert decision.classification == governance.READ_ONLY


def test_read_only_tool_policy() -> None:
    assert governance.tool_allowed(
        "site__visit_stats", governance.READ_ONLY, "owner") is True
    assert governance.tool_allowed(
        "toolbox__python_repl", governance.READ_ONLY, "owner") is False
    assert governance.tool_allowed(
        "toolbox__python_repl", governance.ACTION_EXPLICIT, "owner") is True
    assert governance.tool_allowed(
        "toolbox__python_repl", governance.ACTION_EXPLICIT, "guest") is False
    assert governance.tool_allowed(
        "toolbox__python_repl", governance.ACTION_INFERRED, "owner") is False

    allowed = governance.read_only_tools(
        ["site__visit_stats", "toolbox__python_repl", "toolbox__memory_search"])
    assert allowed == {"site__visit_stats", "toolbox__memory_search"}
    assert governance.is_read_only_tool("evil__now") is False
    assert governance.is_read_only_tool("toolbox__sql_query") is False
    assert governance.is_read_only_tool("toolbox__governance_report") is False
    assert governance.is_read_only_tool("toolbox__confirm_action") is False
    assert governance.is_read_only_tool("trace_get") is False


def test_high_risk_detection() -> None:
    assert governance.is_high_risk_tool("toolbox__python_repl") is True
    assert governance.is_high_risk_tool("python_repl") is True
    assert governance.is_high_risk_tool("site__visit_stats") is False
    assert governance.is_high_risk_tool("agents__send_to_agent") is True
    assert governance.is_high_risk_tool("admin__set_job_interval") is True
    assert governance.is_high_risk_tool("toolbox__memory_update") is True
    assert governance.is_high_risk_tool("toolbox__learn_skill") is True
    assert governance.is_high_risk_tool("model_add") is True
    assert governance.is_high_risk_tool("send_to_channel") is True
    assert governance.is_high_risk_tool("trace_get") is True
    assert governance.is_high_risk_tool("toolbox__sql_query") is True
    assert governance.mandate_required(
        "toolbox__snapshot_checkpoint", {"action": "list"}) is False
    assert governance.mandate_required(
        "toolbox__snapshot_checkpoint", {"action": "restore"}) is True
    assert governance.mandate_required(
        "toolbox__github_api", {"method": "GET"}) is False
    assert governance.mandate_required(
        "toolbox__github_api", {"method": "POST"}) is True


def test_parse_approval_is_strict() -> None:
    parsed = governance.parse_approval("APPROVE 7GSJ")
    assert parsed is not None
    assert parsed.approved is True
    assert parsed.code == "7GSJ"

    rejected = governance.parse_approval("REJECT 7GSJ")
    assert rejected is not None
    assert rejected.approved is False

    assert governance.parse_approval("APPROVE 是什么意思") is None
    assert governance.parse_approval("先别执行") is None
    assert governance.parse_approval("7GSJ") is None


def test_parse_session_command() -> None:
    assert governance.parse_session_command("开发模式 2 小时") == {
        "action": "grant", "preset": "dev", "minutes": 120}
    assert governance.parse_session_command("授权全部 30 分钟") == {
        "action": "grant", "preset": "all", "minutes": 30}
    assert governance.parse_session_command("运维 1 小时") == {
        "action": "grant", "preset": "ops", "minutes": 60}
    assert governance.parse_session_command("开发模式") == {
        "action": "grant", "preset": "dev", "minutes": 60}
    assert governance.parse_session_command("取消授权") == {"action": "revoke"}
    assert governance.parse_session_command("查看授权") == {"action": "list"}
    assert governance.parse_session_command("开发文档在哪里") is None


def test_args_hash_binding() -> None:
    base = {"code": "print(1)"}
    assert governance.args_hash(base) == governance.args_hash(dict(base))
    assert governance.args_hash(base) != governance.args_hash({"code": "print(2)"})
    assert governance.args_hash(base) == governance.args_hash(
        {**base, "mandate": "ABCD", "timeout": 30})


def test_decision_retention(tmp_path) -> None:
    store = Store(str(tmp_path / "router.db"))
    store.init()
    store.log_decision(text="old message", classification="read_only",
                       would_accept=True, executed=True)
    with store._conn() as conn:
        conn.execute(
            "UPDATE decisions SET created_at='2000-01-01T00:00:00+00:00'")
    assert store.prune_decisions(30) == 1
    assert store.list_decisions() == []
