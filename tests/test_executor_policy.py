"""Offline checks for the isolated-mode control-plane policy."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import governance
from registry import ToolRegistry


def setup_function() -> None:
    governance.set_executor_policy("host", False)


def teardown_function() -> None:
    governance.set_executor_policy("host", False)


def test_isolated_allows_executor_tools_and_denies_control_plane() -> None:
    governance.set_executor_policy("isolated", False)
    assert governance.executor_tool_allowed("toolbox__python_repl") is True
    assert governance.executor_tool_allowed("toolbox__write_patch") is True
    assert governance.executor_tool_allowed("ops__fs_write") is True
    assert governance.executor_tool_allowed("devtools__run_command") is True

    assert governance.executor_tool_allowed("admin__config_set") is False
    assert governance.executor_tool_allowed("model_add") is False
    assert governance.executor_tool_allowed("send_to_channel") is False
    denied = governance.check_executor_policy("admin__config_set")
    assert denied is not None
    assert denied["policy"] == "isolated_control_plane"


def test_allow_control_plane_opt_in() -> None:
    governance.set_executor_policy("isolated", True)
    assert governance.executor_tool_allowed("admin__config_set") is True
    assert governance.check_executor_policy("model_add") is None


def test_registry_filters_and_blocks_control_plane() -> None:
    governance.set_executor_policy("isolated", False)
    registry = ToolRegistry()
    registry.register(
        "toolbox__python_repl", "run",
        {"type": "object", "properties": {}},
        lambda args: {"ok": True},
    )
    registry.register(
        "admin__config_set", "config",
        {"type": "object", "properties": {}},
        lambda args: {"ok": True},
    )
    names = [item["function"]["name"] for item in registry.definitions()]
    assert "toolbox__python_repl" in names
    assert "admin__config_set" not in names

    blocked = registry.execute("admin__config_set", {})
    assert blocked["policy"] == "isolated_control_plane"
