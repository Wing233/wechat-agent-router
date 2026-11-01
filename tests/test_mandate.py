"""Offline checks for persistent mandates and high-risk tool enforcement."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import governance
from registry import ToolRegistry

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


def test_high_risk_requires_args(toolbox) -> None:
    service, _ = toolbox
    result = service.ask_confirmation({
        "action": "运行 Python",
        "tool": "toolbox__python_repl",
    })
    assert "args" in result["error"]


def test_high_risk_accepts_json_string_args(toolbox) -> None:
    service, _ = toolbox
    result = service.ask_confirmation({
        "action": "运行 Python",
        "tool": "toolbox__python_repl",
        "args": '{"code": "print(1)"}',
    })
    assert result["pending"] is True
    approve(service, result["code"])
    used = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)", "mandate": result["code"]})
    assert used["ok"] is True


def test_handle_reply_is_strict(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)

    response = service.handle_reply({"code": result["code"]}, "执行这个会怎样")
    assert "APPROVE" in response["text"]
    assert service.confirm_action({"code": result["code"]})["status"] == "pending"

    response = service.handle_reply({"code": result["code"]}, "APPROVE ZZZZ")
    assert "不匹配" in response["text"]
    assert service.confirm_action({"code": result["code"]})["status"] == "pending"

    response = service.handle_reply(
        {"code": result["code"]}, f"APPROVE {result['code']} 但不要执行")
    assert "否定词" in response["text"]
    assert service.confirm_action({"code": result["code"]})["status"] == "pending"

    approve(service, result["code"])
    assert service.confirm_action({"code": result["code"]})["approved"] is True


def test_path_scope_helper(toolbox) -> None:
    service, _ = toolbox
    assert service._paths_allowed(["/workspace"], {"cwd": "/workspace/x"}) is True
    assert service._paths_allowed(["/workspace"], {"cwd": "/app"}) is False
    assert service._paths_allowed(["/workspace"], {}) is False


def test_concurrent_use_is_atomic(toolbox) -> None:
    import threading

    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])
    results = []

    def worker() -> None:
        results.append(service.require_mandate(
            "toolbox__python_repl",
            {"code": "print(1)", "mandate": result["code"]},
        ))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for item in results if item.get("ok")) == 1


def test_require_mandate_rejects_wrong_tool(toolbox) -> None:
    service, _ = toolbox
    result = ask(service)
    approve(service, result["code"])

    changed = service.require_mandate(
        "toolbox__run_sandbox", {"command": "ls", "mandate": result["code"]})
    assert changed["status"] == "tool_changed"

    assert service._tool_matches("toolbox__python_repl", "toolbox__python_repl") is True
    assert service._tool_matches(
        "toolbox__python_repl", "devtools__python_repl") is False
    assert service._tool_matches("python_repl", "toolbox__python_repl") is False


def test_approval_message_shows_args(toolbox) -> None:
    service, store = toolbox
    ask(service, args={"code": "print('visible-args')"})
    text = store.notifications[-1]["text"]
    assert "visible-args" in text


def test_long_args_preview_shows_tail(toolbox) -> None:
    service, store = toolbox
    payload = "A" * 1500 + "TAIL_PAYLOAD"
    ask(service, args={"code": payload})
    text = store.notifications[-1]["text"]
    assert "TAIL_PAYLOAD" in text
    assert "中间省略" in text


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


def test_migrates_baseline_schema(tmp_path: Path) -> None:
    db = tmp_path / "toolbox.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE confirmations(
      code TEXT PRIMARY KEY,
      action TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()

    store = FakeStore()
    service = Toolbox(FakeCtx(store, db))
    try:
        result = ask(service)
        approve(service, result["code"])
        used = service.require_mandate(
            "toolbox__python_repl",
            {"code": "print(1)", "mandate": result["code"]},
        )
        assert used["ok"] is True
    finally:
        governance.set_mandate_store(None)


def test_canonicalizes_bare_tool_name(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        "toolbox__python_repl", "run python",
        {"type": "object", "properties": {"code": {"type": "string"}}},
        lambda args: {"ok": True},
        aliases=("python_repl",),
    )
    store = FakeStore()
    ctx = FakeCtx(store, tmp_path / "toolbox.db")
    ctx.tools = type("FakeTools", (), {"registry": registry})()
    service = Toolbox(ctx)
    try:
        result = service.ask_confirmation({
            "action": "运行 Python",
            "tool": "python_repl",
            "args": {"code": "print(1)"},
        })
        assert result["tool"] == "toolbox__python_repl"
    finally:
        governance.set_mandate_store(None)


def test_session_grant_allows_dev_tools(toolbox) -> None:
    service, _ = toolbox
    grant = service.session_grant("dev", 60, approved_by="owner-1")
    assert grant["ok"] is True

    allowed = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)"})
    assert allowed["ok"] is True
    assert allowed["session"] is True

    blocked = service.require_mandate("model_add", {"id": "x", "model": "y"})
    assert blocked.get("error")

    listed = service.list_sessions()
    assert listed["items"][0]["preset"] == "dev"

    revoked = service.revoke_sessions()
    assert revoked["revoked"] == 1
    blocked_again = service.require_mandate(
        "toolbox__python_repl", {"code": "print(1)"})
    assert blocked_again.get("error")


def test_session_grant_all_and_path_scope(toolbox) -> None:
    service, _ = toolbox
    service.session_grant("all", 60, approved_by="owner-1")

    assert service.require_mandate(
        "model_add", {"id": "x", "model": "y"})["ok"] is True
    assert service.require_mandate(
        "ops__fs_write", {"path": "/workspace/x", "content": "x"})["ok"] is True

    blocked = service.require_mandate(
        "ops__fs_write", {"path": "/tmp/x", "content": "x"})
    assert blocked.get("error")
