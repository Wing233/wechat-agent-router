"""Offline checks for the deterministic help/capability catalog."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import help_center


def test_help_intent_detection() -> None:
    for text in ("help", "/help", "HELP", "帮助", "你能做什么", "微桥能做什么",
                 "这个机器人会做什么", "有哪些功能", "怎么用", "?", "？",
                 "hepl", "help me"):
        assert help_center.is_help_request(text), text
    for text in ("帮助我写一个快排", "你能做什么项目", "帮我查一下天气",
                 "执行：重启服务"):
        assert not help_center.is_help_request(text), text


def test_owner_help_lists_capabilities_and_examples() -> None:
    tools = {
        "show_help", "model_list", "model_add", "model_bind",
        "model_switch_chat", "model_test", "model_remove", "cost_report",
        "budget_set", "trace_search", "trace_get", "trace_explain",
        "insight_report", "eval_run", "eval_report", "eval_drift",
        "eval_add_case", "optimize_suggest", "optimize_apply",
        "list_channels", "send_to_channel", "send_media", "generate_image",
        "create_reminder", "schedule_cron", "todo_add", "todo_list",
        "fs_list", "run_command", "web_search", "http_request",
        "send_to_agent",
    }
    text = help_center.build_help(
        role="owner", channel="qq", endpoint="qq:group:1",
        chat_type="group", tool_names=tools,
        agents={"echo": {}, "pc-home": {}},
        plugins=["admin", "reminders", "toolbox"],
        model_bindings={"chat": ["deepseek-flash"]},
    )
    assert "身份：主人" in text
    assert "qq:group:1" in text
    assert "model_switch_chat" in text
    assert "cost_report" in text
    assert "trace_explain" in text
    assert "eval_drift" in text
    assert "send_to_channel" in text
    assert "pc-home" in text
    assert "deepseek-flash" in text
    assert "身份：访客" not in text


def test_guest_help_is_read_only() -> None:
    text = help_center.build_help(role="guest", channel="qq", tool_names={"show_help"})
    assert "身份：访客" in text
    assert "访客" in text
    assert "model_switch_chat" not in text
    assert "cost_report" not in text
    assert "run_command" not in text


def test_help_filters_unloaded_tools() -> None:
    text = help_center.build_help(role="owner", tool_names={"web_search", "show_help"})
    assert "web_search" in text
    assert "模型与成本" not in text
    assert "排查与质量" not in text
