from channels import DEFAULT_ENDPOINT, ChannelRegistry
from store import Store


def _config() -> dict:
    return {
        "channels": {
            "qq": {
                "adapter": "koishi",
                "secret": "s3cret",
                "owner_ids": ["10001"],
                "allow_guest_tools": False,
            }
        },
        "endpoints": {
            "qq:group:123456": {
                "channel": "qq",
                "kind": "group",
                "label": "QQ 测试群",
                "conversation_id": "123456",
                "capabilities": ["text", "image"],
            }
        },
    }


def test_wechat_envelope_keeps_owner_identity():
    registry = ChannelRegistry({}, "wx-owner")
    envelope = registry.wechat_envelope({"message_id": "m1"}, "wx-owner", "你好")
    assert envelope.endpoint_id == DEFAULT_ENDPOINT
    assert envelope.principal_id == "owner"
    assert registry.endpoint(DEFAULT_ENDPOINT).conversation_id == "wx-owner"


def test_qq_group_guest_gets_guest_principal():
    registry = ChannelRegistry(_config(), "wx-owner")
    envelope = registry.envelope({
        "channel": "qq",
        "adapter": "koishi",
        "endpoint_id": "qq:group:123456",
        "conversation_type": "group",
        "sender_id": "20002",
        "sender_name": "访客",
        "text": "今天花了多少钱",
    })
    assert envelope.endpoint_id == "qq:group:123456"
    assert envelope.principal_id == "guest:qq:20002"
    assert registry.allow_guest_tools("qq") is False
    assert registry.allow_guest_agents("qq") is False


def test_qq_owner_is_mapped_to_owner_principal():
    registry = ChannelRegistry(_config(), "wx-owner")
    envelope = registry.envelope({
        "channel": "qq",
        "endpoint_id": "qq:private:10001",
        "conversation_type": "private",
        "sender_id": "10001",
        "text": "查看今天成本",
    })
    assert envelope.principal_id == "owner"
    assert registry.endpoint("qq:private:10001").kind == "private"


def test_discovered_endpoint_is_registered():
    registry = ChannelRegistry({}, "wx-owner")
    envelope = registry.envelope({
        "channel": "qq",
        "endpoint_id": "qq:group:999",
        "conversation_type": "group",
        "sender_id": "30003",
        "text": "hi",
    })
    endpoint = registry.endpoint("qq:group:999")
    assert endpoint is not None
    assert endpoint.meta.get("discovered") is True
    assert envelope.principal_id == "guest:qq:30003"


def test_channel_outbound_lease_and_ack(tmp_path):
    store = Store(str(tmp_path / "router.db"))
    store.init()
    store.enqueue_channel_outbound("qq:group:123456", "qq", {"kind": "text", "text": "hi"})

    items = store.lease_channel_outbound("qq")
    assert len(items) == 1
    assert items[0]["endpoint_id"] == "qq:group:123456"
    assert items[0]["payload"]["text"] == "hi"

    # A leased row is invisible until the lease expires.
    assert store.lease_channel_outbound("qq") == []
    assert store.ack_channel_outbound([items[0]["id"]]) == 1
    assert store.lease_channel_outbound("qq") == []
