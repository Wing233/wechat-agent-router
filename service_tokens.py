"""Purpose-scoped service credentials.

Historically one shared ``service_token`` unlocked inference, outbound
notifications and router administration. A token handed to a chat client could
therefore rewrite agents, plugins and jobs.

This module resolves one token per purpose from ``service_tokens`` and falls
back to the legacy ``service_token`` **only for a scope with no dedicated
value**. That keeps an existing single-token install working while allowing a
split deployment to rotate one purpose at a time.
"""
from __future__ import annotations

import hmac

SCOPES = ("inference", "notify", "admin")


def resolve(config: dict) -> dict[str, str]:
    legacy = str(config.get("service_token") or "")
    configured = config.get("service_tokens")
    resolved: dict[str, str] = {}
    for scope in SCOPES:
        value = ""
        if isinstance(configured, dict):
            value = str(configured.get(scope) or "").strip()
        resolved[scope] = value or legacy
    return resolved


def from_request(request) -> str:
    """Accept the header used by internal clients and Bearer clients alike."""
    token = request.headers.get("x-service-token", "").strip()
    if token:
        return token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def match(token: str, expected: str) -> bool:
    return bool(token and expected and hmac.compare_digest(token, expected))
