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

    def send_file(self, path: str, caption: str = "", recipient: str | None = None) -> None:
        """Send any file as a WeChat attachment (image/video/file is auto-detected)."""
        self.send_image(path, caption, recipient)

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

    def fetch_media(self, item: dict, max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, str]:
        """Download one inbound attachment through the Hub media proxy."""
        eqp = str(item.get("encrypt_query_param") or "")
        aes = str(item.get("aes_key") or "")
        if eqp and aes:
            query = urllib.parse.urlencode({"key": self.key, "eqp": eqp, "aes": aes})
            url = self.base + "/api/v1/channels/media?" + query
        else:
            url = str(item.get("url") or "")
            if not url:
                raise HubError(0, "缺少媒体地址")
            if url.startswith("/"):
                url = self.base + url
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ("http", "https"):
                raise HubError(0, "媒体地址不合法")
            if "/api/v1/channels/media" not in parsed.path:
                raise HubError(0, "不支持的媒体地址")
            query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            query.setdefault("key", self.key)
            url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path,
                 urllib.parse.urlencode(query), parsed.fragment)
            )
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise HubError(0, f"附件过大（{length} 字节）")
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise HubError(0, f"附件超过 {max_bytes} 字节")
                return data, response.headers.get("Content-Type", "") or ""
        except HubError:
            raise
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise HubError(exc.code, detail) from None
        except urllib.error.URLError as exc:
            raise HubError(0, f"network: {exc.reason}") from None

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


def _message_items(message: dict) -> list[dict]:
    try:
        items = message.get("item_list") or []
        if isinstance(items, str):
            items = json.loads(items)
    except Exception:
        return []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def parse_text(message: dict) -> str:
    chunks: list[str] = []
    for item in _message_items(message):
        if item.get("type") in (None, "text") and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return "".join(chunks).strip()


def parse_quote(message: dict) -> dict | None:
    """Extract the quoted/referenced message, if the channel provides one."""
    for item in _message_items(message):
        ref = item.get("ref_msg")
        if not isinstance(ref, dict):
            continue
        ref_item = ref.get("item") or {}
        if not isinstance(ref_item, dict):
            ref_item = {}
        return {
            "title": str(ref.get("title") or ""),
            "type": str(ref_item.get("type") or ""),
            "text": str(ref_item.get("text") or ""),
        }
    return None


ATTACHMENT_TYPES = ("image", "file", "video", "voice")


def parse_attachments(message: dict) -> list[dict]:
    """Return non-text items. Media is fetched later via Hub.fetch_media()."""
    attachments: list[dict] = []
    for item in _message_items(message):
        kind = str(item.get("type") or "")
        if kind not in ATTACHMENT_TYPES:
            continue
        media = item.get("media") if isinstance(item.get("media"), dict) else {}
        attachments.append({
            "type": kind,
            "file_name": str(item.get("file_name") or ""),
            "url": str(media.get("url") or ""),
            "encrypt_query_param": str(media.get("encrypt_query_param") or ""),
            "aes_key": str(media.get("aes_key") or ""),
            "file_size": int(media.get("file_size") or 0),
            "media_type": str(media.get("media_type") or ""),
        })
    return attachments
