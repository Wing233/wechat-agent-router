"""Helpers for the WeChat iLink 24-hour reply window.

The window is anchored to the last inbound user message. Bot messages never
refresh it, so the only compliant ways to keep proactive notifications flowing
are to send inside the window or wait for the user's next message.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

WINDOW_SECONDS = 24 * 60 * 60
CHECKIN_AFTER_SECONDS = 23 * 60 * 60
CHECKIN_TEXT = "回复「1」继续接收通知。"
WINDOWED_CHANNELS = {"wechat", "weixin", "hub"}


def parse_timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def window_state(row: dict | None, now: float | None = None) -> dict:
    """Return a small, UI-safe view of one stored window."""
    if not row:
        return {"open": False, "last_inbound_at": None, "expires_at": None,
                "seconds_left": 0, "checkin_due": False}
    stamp = parse_timestamp(row.get("last_inbound_at"))
    now = now or datetime.now().timestamp()
    left = 0 if stamp is None else max(0, int(stamp + WINDOW_SECONDS - now))
    checkin_due = bool(
        stamp is not None
        and 0 < (stamp + WINDOW_SECONDS - now) <= (WINDOW_SECONDS - CHECKIN_AFTER_SECONDS)
        and not row.get("last_checkin_at")
    )
    return {
        "open": left > 0,
        "last_inbound_at": row.get("last_inbound_at"),
        "expires_at": None if stamp is None else datetime.fromtimestamp(
            stamp + WINDOW_SECONDS).isoformat(timespec="seconds"),
        "seconds_left": left,
        "checkin_due": checkin_due,
    }


def is_open(row: dict | None, now: float | None = None) -> bool:
    return bool(window_state(row, now)["open"])
