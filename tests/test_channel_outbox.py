"""Offline tests for channel outbox and cross-channel delivery."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels import ChannelManager
from store import Store


def test_channel_outbox_roundtrip(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "router.db"))
    store.init()
    oid = store.enqueue_channel("koishi", "qq:123", "跨入口消息",
                                platform="qq", user_id="qq:u1")
    rows = store.list_channel_outbox("koishi")
    assert len(rows) == 1
    assert rows[0]["id"] == oid
    assert rows[0]["chat_id"] == "qq:123"
    assert store.list_channel_outbox("wechat") == []

    assert store.ack_channel_outbox("koishi", [oid], ok=True) == 1
    assert store.list_channel_outbox("koishi") == []
    recent = store.recent_channel_outbox("koishi")
    assert recent[0]["status"] == "sent"

    failed = store.enqueue_channel("koishi", "qq:123", "第二条", platform="qq")
    assert store.ack_channel_outbox("koishi", [failed], ok=False,
                                    error="bot offline") == 1
    recent = store.recent_channel_outbox("koishi")
    assert recent[0]["status"] == "error"
    assert recent[0]["error"] == "bot offline"


def test_channel_manager_tracks_last_target(tmp_path: Path) -> None:
    manager = ChannelManager({"channels": {
        "koishi": {"id": "koishi", "name": "Koishi", "type": "koishi",
                   "enabled": True, "token": "secret"},
    }})
    channel, error = manager.authenticate("koishi", "secret")
    assert channel is not None and error == ""
    assert manager.authenticate("koishi", "wrong")[0] is None

    manager.record("koishi", "in", platform="qq", chat_id="qq:group:1",
                   user_id="qq:u1", text="hi", account_id="bot1")
    status = manager.status("koishi")
    assert status["last_chat_id"] == "qq:group:1"
    assert status["last_platform"] == "qq"
    assert status["last_account_id"] == "bot1"
    target = manager.target("koishi")
    assert target["chat_id"] == "qq:group:1"
    assert target["platform"] == "qq"


class FakeStore:
    def __init__(self):
        self.queued = []

    def enqueue_channel(self, channel_id, chat_id, text, *, platform="", user_id=""):
        self.queued.append((channel_id, chat_id, text, platform, user_id))
        return 7


class FakeSettings:
    def __init__(self):
        self.store = FakeStore()
        self.channels = {"koishi": {"id": "koishi", "enabled": True}}

    def channels_view(self):
        return [{"id": "koishi", "name": "Koishi", "type": "koishi",
                 "enabled": True, "token": "ch_...abcd"}]

    def channel_get(self, channel_id):
        if channel_id not in self.channels:
            raise ValueError("渠道不存在")
        return self.channels[channel_id]


def test_send_to_channel_tool() -> None:
    import capabilities

    settings = FakeSettings()
    manager = ChannelManager({"channels": settings.channels})
    manager.record("koishi", "in", platform="qq", chat_id="qq:group:1",
                   user_id="qq:u1")
    sent = []
    service = capabilities.CapabilityService(
        None, settings=settings, catalog=None, llm=None, spend_path="",
        reload_llm=lambda: None, channels=manager,
        send_wechat=lambda text: sent.append(text) or True,
    )
    result = service._send_to_channel({"channel": "koishi", "text": "去群里说"})
    assert result["queued"] == 7
    assert settings.store.queued[0][:3] == ("koishi", "qq:group:1", "去群里说")

    result = service._send_to_channel({"channel": "wechat", "text": "微信你好"})
    assert result["sent"] is True
    assert sent == ["微信你好"]

    try:
        service._send_to_channel({"channel": "koishi", "text": ""})
    except ValueError as exc:
        assert "text" in str(exc)
    else:
        raise AssertionError("empty text must fail")
