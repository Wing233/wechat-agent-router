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
               ok: bool = True, detail: str = "") -> dict:
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
                "last_error": "", "last_user": "",
            })
            state["messages"] += 1
            if direction == "in":
                state["last_inbound"] = int(now)
                state["last_user"] = user_id
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
            "enabled": bool(channel.get("enabled", True)),
            "configured": bool(channel.get("owner_id")),
            "owner_id": channel.get("owner_id", ""),
            "pair_code": channel.get("pair_code", ""),
            "messages": state.get("messages", 0),
            "last_inbound": state.get("last_inbound"),
            "last_outbound": state.get("last_outbound"),
            "last_user": state.get("last_user", ""),
            "last_error": state.get("last_error", ""),
        }

    def status_all(self) -> dict[str, dict]:
        return {cid: self.status(cid) for cid in (self.config.get("channels") or {})}

    def recent_logs(self, channel_id: str = "", limit: int = 50) -> list[dict]:
        with self._lock:
            rows = list(self.logs)
        if channel_id:
            rows = [row for row in rows if row["channel"] == channel_id]
        return rows[-max(1, min(limit, 200)):]
