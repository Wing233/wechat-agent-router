"""OpenAI-compatible chat gateway backed by the Router's free model chain.

Lyph already speaks the OpenAI chat-completions protocol. This endpoint lets it
use the Router's model catalog and fallback order without changing Lyph's own
tool loop, cards or access control.

Only the service token can call this endpoint. It is deliberately separate from
the public notify API and from the agent task broker.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from llm import LLMBudgetError, LLMError

log = logging.getLogger("openai_compat")

ALLOWED_MODELS = {"free-router", "chat", "lyph"}
MAX_MESSAGES = 60
MAX_MESSAGE_CHARS = 200_000
MAX_TOOLS = 40
MAX_TOKENS_CAP = 8192


def _token(request: Request) -> str:
    token = request.headers.get("x-service-token", "").strip()
    if token:
        return token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return "" if value is None else str(value)


def _usage(value: dict) -> dict:
    prompt = int(value.get("prompt_tokens") or 0)
    completion = int(value.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(value.get("total_tokens") or prompt + completion),
    }


def _tool_calls(message: dict) -> list[dict]:
    result = []
    for index, item in enumerate(message.get("tool_calls") or []):
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        if not name:
            continue
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        result.append({
            "index": index,
            "id": str(item.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
            "type": "function",
            "function": {"name": name, "arguments": arguments or "{}"},
        })
    return result


def register(app: FastAPI, *, llm, service_token: str) -> None:
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        token = _token(request)
        if not service_token or not hmac.compare_digest(token, service_token):
            raise HTTPException(401, "invalid service token")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON object required")

        model = str(body.get("model") or "free-router").strip()
        if model not in ALLOWED_MODELS:
            raise HTTPException(400, "unsupported model")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(400, "messages required")
        if len(messages) > MAX_MESSAGES:
            raise HTTPException(413, "too many messages")
        if len(json.dumps(messages, ensure_ascii=False)) > MAX_MESSAGE_CHARS:
            raise HTTPException(413, "messages too large")
        tools = body.get("tools")
        if tools is not None and not isinstance(tools, list):
            raise HTTPException(400, "tools must be an array")
        if tools and len(tools) > MAX_TOOLS:
            raise HTTPException(413, "too many tools")
        if tools and any(
            not isinstance(item, dict)
            or not isinstance(item.get("function"), dict)
            or not isinstance(item["function"].get("name"), str)
            or not item["function"]["name"]
            for item in tools
        ):
            raise HTTPException(400, "tools must contain named function definitions")
        tool_choice = body.get("tool_choice", "auto")
        if tool_choice == "none":
            tools = None
        elif tool_choice == "auto":
            pass
        elif tool_choice == "required":
            if not tools:
                raise HTTPException(400, "tool_choice required needs tools")
        elif isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict):
            wanted = tool_choice["function"].get("name")
            tools = [item for item in (tools or []) if item["function"]["name"] == wanted]
            if not tools:
                raise HTTPException(400, "tool_choice names an unavailable tool")
        else:
            raise HTTPException(400, "unsupported tool_choice")
        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            try:
                max_tokens = max(1, min(MAX_TOKENS_CAP, int(max_tokens)))
            except (TypeError, ValueError):
                raise HTTPException(400, "max_tokens must be an integer") from None
        stream = bool(body.get("stream"))
        stream_options = body.get("stream_options")
        include_usage = isinstance(stream_options, dict) and bool(
            stream_options.get("include_usage")
        )

        usage: dict = {}
        try:
            message = await asyncio.to_thread(
                llm.complete,
                messages,
                tools or None,
                max_tokens,
                role="guest",
                purpose="chat",
                usage_sink=usage,
            )
        except LLMBudgetError as exc:
            log.warning("openai-compat budget exceeded: %s", exc)
            raise HTTPException(429, exc.user_message) from None
        except LLMError as exc:
            log.warning("openai-compat provider chain failed: %s", str(exc)[:400])
            raise HTTPException(503, "免费模型路由暂时不可用，请稍后重试") from None

        content = _content(message.get("content"))
        calls = _tool_calls(message)
        finish_reason = "tool_calls" if calls else "stop"
        created = int(time.time())
        completion_id = "chatcmpl-" + uuid.uuid4().hex
        usage_payload = _usage(usage)

        if not stream:
            payload = {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        **({"tool_calls": calls} if calls else {}),
                    },
                    "finish_reason": finish_reason,
                }],
                "usage": usage_payload,
            }
            return JSONResponse(payload)

        def chunk(delta: dict, finish=None) -> str:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        async def generate():
            yield chunk({"role": "assistant", "content": content or None})
            if calls:
                yield chunk({"tool_calls": calls})
            yield chunk({}, finish_reason)
            if include_usage:
                yield "data: " + json.dumps({
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": usage_payload,
                }, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
