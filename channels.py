"""Generic message-channel bridge state and authentication.

Platform-specific protocols stay in the bridge (Koishi/OneBot/Telegram).
This module only knows a channel id, its token and a small audit tail.
"""
from __future__ import annotations

import hmac
import threading
import time
from collections import deque


class ChannelManager:
    def __init__(self, config: dict):
        self.config = config
        self._lock = threading.RLock()
        self._state: dict[str, dict] = {}
        self._seq = 0
        self.logs: deque[dict] = deque(maxlen=300)

    def get(self, channel_id: str) -> dict | None:
        return (self.config.get("channels") or {}).get(channel_id)

    def authenticate(self, channel_id: str, token: str) -> tuple[dict | None, str]:
        channel = self.get(channel_id)
        if not channel:
            return None, "未知渠道"
        if not channel.get("enabled", True):
            return None, "渠道未启用"
        expected = str(channel.get("token") or "")
        if not expected or not token or not hmac.compare_digest(expected, str(token)):
            return None, "渠道令牌无效"
        return channel, ""

    def record(self, channel_id: str, direction: str, *, platform: str = "",
               chat_id: str = "", user_id: str = "", text: str = "",
               ok: bool = True, detail: str = "", account_id: str = "") -> dict:
        now = time.time()
        with self._lock:
            self._seq += 1
            item = {
                "id": self._seq,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                "channel": channel_id,
                "direction": direction,
                "platform": platform,
                "chat_id": chat_id,
                "user_id": user_id,
                "text": str(text or "")[:600],
                "ok": bool(ok),
                "detail": str(detail or "")[:300],
            }
            self.logs.append(item)
            state = self._state.setdefault(channel_id, {
                "messages": 0, "last_inbound": None, "last_outbound": None,
                "last_error": "", "last_user": "", "last_chat_id": "",
                "last_platform": "", "last_account_id": "",
            })
            state["messages"] += 1
            if direction == "in":
                state["last_inbound"] = int(now)
                state["last_user"] = user_id
                state["last_chat_id"] = chat_id
                state["last_platform"] = platform
                state["last_account_id"] = account_id
            else:
                state["last_outbound"] = int(now)
            if not ok:
                state["last_error"] = str(detail or text or "error")[:300]
            return item

    def status(self, channel_id: str) -> dict:
        with self._lock:
            state = dict(self._state.get(channel_id) or {})
        channel = self.get(channel_id) or {}
        return {
            "id": channel_id,
            "name": channel.get("name") or channel_id,
            "type": channel.get("type") or "koishi",
            "enabled": bool(channel.get("enabled", True)),
            "messages": state.get("messages", 0),
            "last_inbound": state.get("last_inbound"),
            "last_outbound": state.get("last_outbound"),
            "last_user": state.get("last_user", ""),
            "last_chat_id": state.get("last_chat_id", ""),
            "last_platform": state.get("last_platform", ""),
            "last_account_id": state.get("last_account_id", ""),
            "last_error": state.get("last_error", ""),
        }

    def target(self, channel_id: str) -> dict:
        """Best-effort last known destination for a channel."""
        status = self.status(channel_id)
        return {
            "channel": channel_id,
            "chat_id": status.get("last_chat_id") or "",
            "platform": status.get("last_platform") or "",
            "user_id": status.get("last_user") or "",
        }

    def status_all(self) -> dict[str, dict]:
        return {cid: self.status(cid) for cid in (self.config.get("channels") or {})}

    def recent_logs(self, channel_id: str = "", limit: int = 50) -> list[dict]:
        with self._lock:
            rows = list(self.logs)
        if channel_id:
            rows = [row for row in rows if row["channel"] == channel_id]
        return rows[-max(1, min(limit, 200)):]
