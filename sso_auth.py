"""Shared Authenticator (TOTP) single sign-on primitives.

One TOTP secret and one signed session cookie cover the Koishi console and the
lyph admin console. The legacy ``/koishi-auth/*`` gate keeps its own cookie; the
new ``/sso/*`` gate never touches it.

Config (all optional, under the existing ``sso`` object in config.json):

    "sso": {
      "cookie_name": "lyph_sso",        # default
      "cookie_domain": "",              # default: host-only cookie
      "edge_secret": "REPLACE_ME",      # default: koishi_auth.session_secret
      "extra_cookies": ["lyph_owner", "koishi_totp"]
    }

The edge secret is shared with lyph so it can verify ``X-SSO-Verified``. It is
signed per request and only travels on the private Docker network.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import unquote, urlsplit

import koishi_auth as ka

DEFAULT_COOKIE = "lyph_sso"
DEFAULT_NEXT = "/"
MAX_NEXT = 2048
ALLOWED_NEXT_PREFIXES = ("/koishi", "/admin", "/api/v1/admin")
SAFE_NEXT = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?=-]*$")
EDGE_VERSION = "v1"

_replay: dict[str, float] = {}
_REPLAY_TTL = 180.0


def settings(config: dict) -> dict:
    return dict(config.get("sso") or {})


def knobs(config: dict) -> dict:
    return config.get("koishi_auth") or {}


def cookie_name(config: dict) -> str:
    return str(settings(config).get("cookie_name") or DEFAULT_COOKIE)


def session_ttl(config: dict) -> int:
    return int(knobs(config).get("session_ttl", 43200))


def edge_secret(config: dict) -> str:
    return str(settings(config).get("edge_secret")
               or knobs(config).get("session_secret") or "")


def session_value(config: dict, request) -> str:
    return str(request.cookies.get(cookie_name(config), "") or "")


def session_ok(config: dict, request) -> bool:
    secret = str(knobs(config).get("session_secret") or "")
    if not secret:
        return False
    try:
        return bool(ka.verify_session(secret, session_value(config, request),
                                      session_ttl(config)))
    except (TypeError, ValueError):
        return False


def issue_session(config: dict) -> str:
    secret = str(knobs(config).get("session_secret") or "")
    if not secret:
        return ""
    return ka.issue_session(secret, session_ttl(config))


def set_cookie(config: dict, response) -> None:
    value = issue_session(config)
    if not value:
        return
    ttl = session_ttl(config)
    domain = str(settings(config).get("cookie_domain") or "") or None
    response.set_cookie(
        cookie_name(config), value,
        max_age=ttl, httponly=True, secure=True, samesite="lax",
        path="/", domain=domain,
    )


def clear_cookies(config: dict, response) -> None:
    domain = str(settings(config).get("cookie_domain") or "") or None
    response.delete_cookie(cookie_name(config), path="/", domain=domain, secure=True)
    for name in settings(config).get("extra_cookies") or ("koishi_totp", "lyph_owner"):
        response.delete_cookie(str(name), path="/", secure=True)


def safe_next(value: str, fallback: str = DEFAULT_NEXT) -> str:
    """Return a same-site path under a known console prefix, or ``fallback``."""
    text = str(value or "")
    if not text or len(text) > MAX_NEXT:
        return fallback
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return fallback
    if "\\" in text or "#" in text or not text.startswith("/") or text.startswith("//"):
        return fallback
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc:
        return fallback
    decoded = unquote(parsed.path)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded):
        return fallback
    if "\\" in decoded or ".." in decoded or "//" in decoded:
        return fallback
    if not SAFE_NEXT.fullmatch(text):
        return fallback
    if not any(decoded == prefix or decoded.startswith(prefix + "/")
               for prefix in ALLOWED_NEXT_PREFIXES):
        return fallback
    return text


def form_csrf(config: dict) -> str:
    secret = str(knobs(config).get("session_secret") or "")
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), b"sso-auth-form",
                    hashlib.sha256).hexdigest()[:32]


def edge_header(config: dict, value: str, at: float | None = None) -> str:
    """Sign the session cookie for lyph: ``v1.<unix_ts>.<hex hmac>``."""
    secret = edge_secret(config)
    if not secret or not value:
        return ""
    issued = int(time.time() if at is None else at)
    message = f"{EDGE_VERSION}|{value}|{issued}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{EDGE_VERSION}.{issued}.{signature}"


def verify_totp_once(secret: str, code: str, window: int = 1) -> bool:
    """TOTP check with a short replay cache (single router process)."""
    text = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(text) != 6 or not secret:
        return False
    now = time.time()
    matched = None
    for offset in range(-window, window + 1):
        at = now + offset * 30
        if hmac.compare_digest(ka.totp(secret, at), text):
            matched = int(at // 30)
            break
    if matched is None:
        return False
    key = hashlib.sha256(f"{secret}|{matched}|{text}".encode("utf-8")).hexdigest()
    for name, stamp in list(_replay.items()):
        if now - stamp > _REPLAY_TTL:
            _replay.pop(name, None)
    if key in _replay:
        return False
    _replay[key] = now
    return True
