"""Offline checks for persistent mandates and high-risk tool enforcement."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import governance

_SPEC = importlib.util.spec_from_file_location(
    "toolbox_plugin_for_tests", ROOT / "plugins" / "toolbox" / "plugin.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
Toolbox = _MODULE.Toolbox


class FakeStore:
    def __init__(self) -> None:
        self.notifications: list[dict] = []
        self.pending_code = ""

    def by_code(self, code: str):
        return {"code": code} if code == self.pending_code else None

    def create_notification(self, **kwargs):
        self.notifications.append(kwargs)
        return {"id": "notification-1", **kwargs}


class FakeCtx:
    def __init__(self, store: FakeStore, db_path: Path) -> None:
        self._store = store
        self.root_config = {
            "owner": "owner-1",
            "toolbox": {"db": str(db_path)},
        }
        self.config = {}
        self.sent: list[str] = []

    def resource(self, name: str):
        if name == "store":
            return self._store
        raise KeyError(name)

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def secret(self, name: str) -> str:
        return ""


@pytest.fixture()
def toolbox(tmp_path: Path):
    store = FakeStore()
    ctx = FakeCtx(store, tmp_path / "toolbox.db")
    service = Toolbox(ctx)
    yield service, store
    governance.set_mandate_store(None)


def approve(toolbox, code: str) -> None:
    toolbox.handle_reply({"code": code, "chat_id": "owner-1"}, f"APPROVE {code}")


def ask(toolbox, *, tool="toolbox__python_repl", args=None, **extra):
    return toolbox.ask_confirmation({
        "action": "运行 Python",
        "summary": "测试审批",
        "tool": tool,
        "args": args if args is not None else {"code": "print(1)"},
        **extra,
    })


def test_ask_and_confirm(toolbox) -> None:
    service, store = toolbox
    result = ask(service)
    assert result["pending"] is True
    assert len(result["code"]) == 4
    assert result["tool"] == "toolbox__python_repl"
    assert result["expires_at"]
    assert store.notifications

    pending = service.confirm_action({"code": result["code"]})
    assert pending["approved"] is False
    assert pending["status"] == "pending"

    approve(service, result["code"])
    approved = service.confirm_action({"code": result["code"]})
    assert approved["approved"] is True
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "owner-1"


def test_require_mandate_one_time(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])

    used = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert used["ok"] is True
    assert used["args"] == {"code": "print(1)"}

    again = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert again["error"] == "审批已使用"


def test_require_mandate_rejects_wrong_args(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])

    changed = service.require_mandate(
        "toolbox__python_repl", {"code": "print(2)", "mandate": result["code"]})
    assert changed["status"] == "args_changed"


def test_require_mandate_rejects_wrong_tool(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])

    changed = service.require_mandate(
        "toolbox__run_sandbox", {"command": "ls", "mandate": result["code"]})
    assert changed["status"] == "tool_changed"


def test_expired_mandate(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])
    with service._conn() as conn:
        conn.execute(
            "UPDATE confirmations SET expires_at=? WHERE code=?",
            ("2000-01-01T00:00:00+00:00", result["code"]))

    expired = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert expired["status"] == "expired"


def test_revoked_mandate(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])
    revoked = service.revoke_mandate({"code": result["code"]})
    assert revoked["ok"] is True

    blocked = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert blocked["status"] == "revoked"


def test_multi_use_mandate(toolbox) -> None:
    service, _ = toolbox
    result = ask(service, max_uses=2)
    approve(service, result["code"])

    first = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    second = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    third = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert first["ok"] is True
    assert second["ok"] is True
    assert third["error"] == "审批已使用"


def test_unbound_mandate_is_rejected(toolbox) -> None:
    service, _ = toolbox
    result = service.ask_confirmation({
        "action": "运行 Python",
        "summary": "没有绑定工具",
    })
    approve(service, result["code"])

    blocked = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert blocked["status"] == "unbound"


def test_policy_version_change(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])
    with service._conn() as conn:
        row = conn.execute(
            "SELECT scope_json FROM confirmations WHERE code=?",
            (result["code"],)).fetchone()
        scope = json.loads(row["scope_json"])
        scope["policy_version"] = "old-policy"
        conn.execute(
            "UPDATE confirmations SET scope_json=? WHERE code=?",
            (json.dumps(scope), result["code"]))

    blocked = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert blocked["status"] == "policy_changed"


def test_recent_approved_and_list(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])

    recent = service.recent_approved(limit=3)
    assert [item["code"] for item in recent] == [result["code"]]
    listed = service.list_mandates({"limit": 10})
    assert listed["items"][0]["code"] == result["code"]

    service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert service.recent_approved(limit=3) == []


def test_confirm_action_checks_binding(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])

    checked = service.confirm_action({
        "code": result["code"],
        "tool": "toolbox__run_sandbox",
        "args": {"command": "ls"},
    })
    assert checked["approved"] is False
    assert checked["binding"]["status"] == "tool_changed"
