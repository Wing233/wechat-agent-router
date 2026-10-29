"""Minimal OpeniLink Hub channel client (stdlib only)."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid


class HubError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"{status}: {message}")


class Hub:
    def __init__(self, base_url: str, channel_key: str, owner_id: str, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.key = channel_key
        self.owner = owner_id
        self.timeout = timeout

    def _request(self, method: str, path: str, *, body: bytes = None, headers: dict | None = None):
        url = self.base + path
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
                return resp.status, data
        except urllib.error.HTTPError as exc:
            data = exc.read()
            try:
                payload = json.loads(data.decode("utf-8", "replace"))
                message = payload.get("error") or payload.get("message") or str(payload)
            except Exception:
                message = data.decode("utf-8", "replace")[:500]
            raise HubError(exc.code, message) from None
        except urllib.error.URLError as exc:
            raise HubError(0, f"network: {exc.reason}") from None

    def send_text(self, text: str, recipient: str | None = None) -> None:
        payload = json.dumps(
            {"recipient": recipient or self.owner, "text": text}, ensure_ascii=False
        ).encode("utf-8")
        self._request(
            "POST",
            "/api/v1/channels/send?key=" + urllib.parse.quote(self.key),
            body=payload,
            headers={"Content-Type": "application/json"},
        )

    def send_image(self, path: str, caption: str = "", recipient: str | None = None) -> None:
        boundary = "----router" + uuid.uuid4().hex
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("recipient", recipient or self.owner)
        if caption:
            field("text", caption)
        filename = os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {ctype}\r\n\r\n".encode(),
            ]
        )
        with open(path, "rb") as handle:
            parts.append(handle.read())
        parts.extend([b"\r\n", f"--{boundary}--\r\n".encode()])
        body = b"".join(parts)
        self._request(
            "POST",
            "/api/v1/channels/send?key=" + urllib.parse.quote(self.key),
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def poll(self, after_id: int, limit: int = 50) -> list[dict]:
        query = {"key": self.key, "limit": str(limit)}
        if after_id:
            cursor = base64.urlsafe_b64encode(f"v1:{after_id}".encode()).decode().rstrip("=")
            query["cursor"] = cursor
        path = "/api/v1/channels/messages?" + urllib.parse.urlencode(query)
        status, data = self._request("GET", path)
        payload = json.loads(data.decode("utf-8"))
        return payload.get("messages") or []

    def latest_context_ok(self) -> bool:
        """The hub enforces the 24h window; this is only a soft hint for the UI."""
        return True


def parse_text(message: dict) -> str:
    try:
        items = message.get("item_list") or []
        if isinstance(items, str):
            items = json.loads(items)
    except Exception:
        return ""
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in (None, "text") and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return "".join(chunks).strip()
