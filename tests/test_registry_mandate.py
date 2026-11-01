"""Offline checks that the registry enforces mandates before high-risk handlers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import governance
from registry import ToolRegistry


class FakeMandateStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def require_mandate(self, name: str, args: dict) -> dict:
        self.calls.append((name, dict(args)))
        if args.get("mandate") != "OK":
            return {"error": "需要审批", "required": True}
        cleaned = dict(args)
        cleaned.pop("mandate", None)
        return {"ok": True, "args": cleaned}


@pytest.fixture()
def fake_store():
    store = FakeMandateStore()
    governance.set_mandate_store(store)
    yield store
    governance.set_mandate_store(None)


def test_definitions_inject_mandate(fake_store) -> None:
    registry = ToolRegistry()
    registry.register(
        "toolbox__python_repl", "run python",
        {"type": "object", "properties": {"code": {"type": "string"}}},
        lambda args: {"ok": True},
    )
    registry.register(
        "site__visit_stats", "stats",
        {"type": "object", "properties": {}},
        lambda args: {"ok": True},
    )
    definitions = {item["function"]["name"]: item for item in registry.definitions()}
    assert "mandate" in definitions["toolbox__python_repl"]["function"]["parameters"]["properties"]
    assert "mandate" not in definitions["site__visit_stats"]["function"]["parameters"]["properties"]


def test_high_risk_execute_is_blocked_without_mandate(fake_store) -> None:
    registry = ToolRegistry()
    calls: list[dict] = []
    registry.register(
        "toolbox__python_repl", "run python",
        {"type": "object", "properties": {"code": {"type": "string"}}},
        lambda args: calls.append(dict(args)) or {"ok": True},
    )
    result = registry.execute("toolbox__python_repl", {"code": "print(1)"})
    assert result["error"] == "需要审批"
    assert calls == []


def test_high_risk_execute_strips_mandate(fake_store) -> None:
    registry = ToolRegistry()
    calls: list[dict] = []
    registry.register(
        "toolbox__python_repl", "run python",
        {"type": "object", "properties": {"code": {"type": "string"}}},
        lambda args: calls.append(dict(args)) or {"ok": True},
    )
    result = registry.execute(
        "toolbox__python_repl", {"code": "print(1)", "mandate": "OK"})
    assert result["ok"] is True
    assert calls == [{"code": "print(1)"}]


def test_non_high_risk_execute_skips_mandate(fake_store) -> None:
    registry = ToolRegistry()
    registry.register(
        "site__visit_stats", "stats",
        {"type": "object", "properties": {}},
        lambda args: {"ok": True},
    )
    result = registry.execute("site__visit_stats", {})
    assert result["ok"] is True
    assert fake_store.calls == []
