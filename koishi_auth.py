"""TOTP and signed-session helpers for the Koishi console gate.

No third-party dependency: RFC 6238 TOTP over HMAC-SHA1, plus an HMAC-signed
cookie value. Secrets are generated and stored by SettingsService.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    text = str(secret or "").strip().replace(" ", "").upper()
    text += "=" * (-len(text) % 8)
    return base64.b32decode(text, casefold=True)


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    text = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(text) != 6 or not secret:
        return False
    now = time.time()
    return any(hmac.compare_digest(totp(secret, now + step * 30), text)
               for step in range(-window, window + 1))


def issue_session(secret: str, ttl: int = 43200) -> str:
    issued = int(time.time())
    signature = hmac.new(secret.encode("utf-8"), str(issued).encode("ascii"),
                         hashlib.sha256).hexdigest()
    return f"{issued}.{signature}"


def verify_session(secret: str, value: str, ttl: int = 43200) -> bool:
    try:
        issued, signature = str(value or "").split(".", 1)
        issued_at = int(issued)
    except (TypeError, ValueError):
        return False
    now = time.time()
    if issued_at > now + 60 or now - issued_at > int(ttl):
        return False
    expected = hmac.new(secret.encode("utf-8"), issued.encode("ascii"),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
