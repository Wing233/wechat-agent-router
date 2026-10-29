"""OpenAI-compatible chat client (DeepSeek)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 90):
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 max_tokens: int | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "temperature": 0.3}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {exc.code}: {text}") from None
        except urllib.error.URLError as exc:
            raise LLMError(f"network: {exc.reason}") from None
        except Exception as exc:
            raise LLMError(str(exc)) from None
        if data.get("error"):
            raise LLMError(str(data["error"])[:800])
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("empty response")
        return choices[0].get("message") or {}
