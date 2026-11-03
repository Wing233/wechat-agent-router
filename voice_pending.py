"""Voice reply slot.

QQ private chats only accept passive replies, so a voice message has to ride
along with the reply to the inbound message that triggered it. The tool runs in
a worker thread (``asyncio.to_thread``), so a contextvar key does not reliably
match the request coroutine. A single global slot is enough for a personal bot
and is popped immediately after the turn.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_pending: list[str] = []


def put(trace_id: str, path: str) -> None:
    if not path:
        return
    with _lock:
        _pending.append(str(path))


def pop(trace_id: str = "") -> str:
    with _lock:
        if not _pending:
            return ""
        return _pending.pop(0)


def clear() -> None:
    with _lock:
        _pending.clear()
