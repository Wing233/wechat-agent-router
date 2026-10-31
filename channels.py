"""Channel-agnostic message envelope, endpoints and principals.

The Router core should not know whether a message came from WeChat, QQ or a
future adapter. Adapters normalize into Envelope; Router routes by endpoint.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ENDPOINT = "wechat:owner"
DEFAULT_PRINCIPAL = "owner"


@dataclass
class Endpoint:
    id: str
    channel: str
    kind: str = "private"
    label: str = ""
    conversation_id: str = ""
    account_id: str = ""
    capabilities: list[str] = field(default_factory=lambda: ["text"])
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "channel": self.channel,
            "kind": self.kind,
            "label": self.label,
            "conversation_id": self.conversation_id,
            "account_id": self.account_id,
            "capabilities": list(self.capabilities),
            "meta": self.meta,
        }


@dataclass
class Principal:
    id: str
    display: str = ""
    owner: bool = False
    channel_user_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display": self.display,
            "owner": self.owner,
            "channel_user_id": self.channel_user_id,
        }


@dataclass
class Envelope:
    channel: str
    adapter: str
    endpoint_id: str
    text: str = ""
    conversation_type: str = "private"
    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    principal_id: str = ""
    account_id: str = ""
    attachments: list[dict] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "adapter": self.adapter,
            "endpoint_id": self.endpoint_id,
            "text": self.text,
            "conversation_type": self.conversation_type,
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "principal_id": self.principal_id,
            "account_id": self.account_id,
            "attachments": self.attachments,
            "mentions": self.mentions,
            "raw": self.raw,
            "received_at": self.received_at,
        }


class ChannelRegistry:
    def __init__(self, config: dict, wechat_owner: str):
        self.config = config or {}
        self.wechat_owner = wechat_owner
        self.channels: dict[str, dict] = dict(self.config.get("channels") or {})
        self.endpoints: dict[str, Endpoint] = {}
        self.principals: dict[str, Principal] = {
            "owner": Principal("owner", "站主", True),
        }
        for endpoint_id, raw in (self.config.get("endpoints") or {}).items():
            if not isinstance(raw, dict):
                continue
            self.endpoints[endpoint_id] = Endpoint(
                id=endpoint_id,
                channel=str(raw.get("channel") or endpoint_id.split(":", 1)[0]),
                kind=str(raw.get("kind") or raw.get("type") or "private"),
                label=str(raw.get("label") or endpoint_id),
                conversation_id=str(raw.get("conversation_id") or ""),
                account_id=str(raw.get("account_id") or ""),
                capabilities=list(raw.get("capabilities") or ["text"]),
                meta=dict(raw.get("meta") or {}),
            )
        self.endpoints.setdefault(DEFAULT_ENDPOINT, Endpoint(
            id=DEFAULT_ENDPOINT,
            channel="wechat",
            kind="private",
            label="微信主号",
            conversation_id=wechat_owner,
            capabilities=["text", "image", "file", "quote"],
        ))

    # ------------------------------------------------------------------
    def channel(self, name: str) -> dict:
        return dict(self.channels.get(name) or {})

    def secret(self, channel: str) -> str:
        return str(self.channel(channel).get("secret") or "")

    def allow_guest_tools(self, channel: str) -> bool:
        return bool(self.channel(channel).get("allow_guest_tools", False))

    def allow_guest_agents(self, channel: str) -> bool:
        return bool(self.channel(channel).get("allow_guest_agents", False))

    def endpoint(self, endpoint_id: str) -> Endpoint | None:
        return self.endpoints.get(endpoint_id)

    def principal(self, principal_id: str) -> Principal | None:
        return self.principals.get(principal_id or "owner")

    def list_endpoints(self) -> list[dict]:
        return [item.to_dict() for item in self.endpoints.values()]

    def owner_ids(self, channel: str) -> set[str]:
        raw = self.channel(channel).get("owner_ids") or []
        if isinstance(raw, str):
            raw = [raw]
        result = {str(item) for item in raw if str(item)}
        if channel == "wechat" and self.wechat_owner:
            result.add(self.wechat_owner)
        return result

    def principal_for(self, channel: str, sender_id: str, sender_name: str = "") -> Principal:
        sender_id = str(sender_id or "")
        if sender_id and sender_id in self.owner_ids(channel):
            return self.principals["owner"]
        key = f"guest:{channel}:{sender_id}"
        if key not in self.principals:
            self.principals[key] = Principal(
                id=key,
                display=sender_name or sender_id or "访客",
                owner=False,
                channel_user_id=sender_id,
            )
        return self.principals[key]

    def ensure_endpoint(self, channel: str, endpoint_id: str, conversation_type: str = "private",
                        conversation_id: str = "", sender_name: str = "") -> Endpoint:
        if endpoint_id in self.endpoints:
            return self.endpoints[endpoint_id]
        kind = "group" if conversation_type in {"group", "guild", "channel"} else "private"
        endpoint = Endpoint(
            id=endpoint_id,
            channel=channel,
            kind=kind,
            label=sender_name or endpoint_id,
            conversation_id=conversation_id or endpoint_id.rsplit(":", 1)[-1],
            capabilities=["text"],
            meta={"discovered": True},
        )
        self.endpoints[endpoint_id] = endpoint
        return endpoint

    def envelope(self, data: dict) -> Envelope:
        channel = str(data.get("channel") or "qq")
        endpoint_id = str(data.get("endpoint_id") or "")
        conversation_type = str(data.get("conversation_type") or "private")
        conversation_id = str(data.get("conversation_id") or "")
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        sender_id = str(data.get("sender_id") or sender.get("id") or "")
        sender_name = str(data.get("sender_name") or sender.get("name") or "")
        if not endpoint_id:
            suffix = conversation_id or sender_id or "unknown"
            endpoint_id = f"{channel}:{conversation_type}:{suffix}" if conversation_type != "private" else f"{channel}:private:{suffix}"
        self.ensure_endpoint(channel, endpoint_id, conversation_type, conversation_id, sender_name)
        principal = self.principal_for(channel, sender_id, sender_name)
        attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
        mentions = data.get("mentions") if isinstance(data.get("mentions"), list) else []
        return Envelope(
            channel=channel,
            adapter=str(data.get("adapter") or "unknown"),
            endpoint_id=endpoint_id,
            text=str(data.get("text") or "").strip(),
            conversation_type=conversation_type,
            message_id=str(data.get("message_id") or ""),
            sender_id=sender_id,
            sender_name=sender_name,
            principal_id=principal.id,
            account_id=str(data.get("account_id") or ""),
            attachments=attachments,
            mentions=mentions,
            raw=dict(data.get("raw") or data),
        )

    def wechat_envelope(self, message: dict, owner_id: str, text: str) -> Envelope:
        return Envelope(
            channel="wechat",
            adapter="openilink",
            endpoint_id=DEFAULT_ENDPOINT,
            text=text,
            conversation_type="private",
            message_id=str(message.get("message_id") or message.get("id") or ""),
            sender_id=str(message.get("from_user_id") or owner_id),
            sender_name="站主",
            principal_id="owner",
            raw=message,
        )
