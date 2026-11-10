"""LYPH admin content client used by the handwritten-daily plugin.

Mirrors plugins/music/lyph_sync.py: it authenticates through the Router's SSO
credentials and calls lyph's admin HTTP API. It never writes lyph's SQLite
database directly.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests
import sso_auth

log = logging.getLogger("plugin.handwritten_daily.lyph")


class LyphContentClient:
    def __init__(self, *, config_path: str, api_url: str, origin: str, timeout: int = 30):
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.cookie = sso_auth.issue_session(self.config)
        if not self.cookie:
            raise RuntimeError("could not issue lyph SSO session; check sso/koishi_auth config")
        self.cookie_name = sso_auth.cookie_name(self.config)
        self.api_url = api_url.rstrip("/")
        self.origin = origin.rstrip("/")
        self.timeout = max(5, int(timeout))
        self.session = requests.Session()
        self.csrf = ""

    def auth(self) -> None:
        response = self.session.get(
            f"{self.api_url}/api/v1/auth/session",
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("authenticated"):
            raise RuntimeError("lyph SSO authentication failed")
        self.csrf = str(data.get("csrf_token") or "")
        if not self.csrf:
            raise RuntimeError("lyph SSO did not return a CSRF token")

    def _headers(self, csrf: str = "") -> dict:
        headers = {
            "Cookie": f"{self.cookie_name}={self.cookie}",
            "X-SSO-Verified": sso_auth.edge_header(self.config, self.cookie),
            "Origin": self.origin,
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return headers

    def create_draft(self, payload: dict) -> dict:
        response = self.session.post(
            f"{self.api_url}/api/v1/admin/contents",
            json=payload,
            headers=self._headers(self.csrf),
            timeout=max(30, self.timeout),
        )
        response.raise_for_status()
        return response.json()

    def get(self, content_id: str) -> dict:
        response = self.session.get(
            f"{self.api_url}/api/v1/admin/contents/{content_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def update_draft(self, content_id: str, payload: dict, revision: int) -> dict:
        body = dict(payload)
        body["revision"] = revision
        response = self.session.put(
            f"{self.api_url}/api/v1/admin/contents/{content_id}",
            json=body,
            headers=self._headers(self.csrf),
            timeout=max(30, self.timeout),
        )
        response.raise_for_status()
        return response.json()

    def publish(self, content_id: str, revision: int) -> dict:
        response = self.session.post(
            f"{self.api_url}/api/v1/admin/contents/{content_id}/publish",
            json={"revision": revision},
            headers=self._headers(self.csrf),
            timeout=max(30, self.timeout),
        )
        response.raise_for_status()
        return response.json()

    def withdraw(self, content_id: str, revision: int) -> dict:
        response = self.session.post(
            f"{self.api_url}/api/v1/admin/contents/{content_id}/withdraw",
            json={"revision": revision},
            headers=self._headers(self.csrf),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def list_contents(self, content_type: str | None = None, limit: int = 20) -> list[dict]:
        params = {}
        if content_type:
            params["type"] = content_type
        response = self.session.get(
            f"{self.api_url}/api/v1/admin/contents",
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        items = response.json().get("items") or []
        return items[: max(1, min(int(limit), 100))]

    def public_url(self, content_id: str) -> str:
        return f"{self.origin}/entry?id={content_id}"