"""Text-to-speech delivered with the current reply.

QQ private chats only accept passive replies, so the default mode attaches the
generated audio to the reply of the inbound message (``voice_pending``). An
explicit ``chat_id`` switches to the channel outbox for proactive delivery,
which works on channels that allow it.

Breeze is Cloudflare-fronted and stalls intermittently from mainland China;
edge-tts (Microsoft) is fast and reliable here, so it is the fallback whenever
Breeze fails.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.request
import uuid
from pathlib import Path

import trace_ctx
import voice_pending

API_BASE = "https://api.breeze.blue"
VOICE_DIR = Path("/data/voice")
# Host-side prefix of the container's /data mount; the Koishi bridge runs on
# the host and reads the generated audio directly from disk.
HOST_DATA_DIR = "/opt/wechat-agent-router/data"

DEFAULT_MODEL = "breeze-tts-2"
DEFAULT_VOICE = "voc_dcccp6b9p466"
EDGE_VOICE = "zh-CN-XiaoxiaoNeural"


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _config(ctx) -> dict:
    plugins = (ctx.root_config or {}).get("plugins") or {}
    return dict(plugins.get("voice") or ctx.config or {})


def _breeze(text: str, voice_id: str, model_id: str, api_key: str,
            timeout: int = 25) -> bytes:
    payload = {
        "text": text,
        "model_id": model_id,
        "language_code": "zh",
        "instructions": "自然口语，语速适中",
    }
    url = f"{API_BASE}/v1/text-to-speech/{voice_id}?output_format=mp3_24000_128"
    try:
        from curl_cffi import CurlOpt, requests as curl_requests
    except Exception:
        curl_requests = None
    if curl_requests is not None:
        response = curl_requests.post(
            url,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            impersonate="chrome",
            timeout=timeout,
            curl_options={CurlOpt.HTTP_VERSION: 2},
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:160]}")
        if not response.content:
            raise RuntimeError("空响应")
        return response.content
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     method="POST")
    request.add_header("xi-api-key", api_key)
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout, context=context) as handle:
        data = handle.read()
    if not data:
        raise RuntimeError("空响应")
    return data


def _edge(text: str, voice: str, timeout: int = 30) -> bytes:
    import edge_tts

    async def run() -> bytes:
        communicate = edge_tts.Communicate(text, voice)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        return b"".join(chunks)

    return asyncio.run(asyncio.wait_for(run(), timeout=timeout))


def _synthesize(text: str, voice_id: str, model_id: str, api_key: str,
                provider: str = "auto", edge_voice: str = EDGE_VOICE) -> tuple[bytes, str]:
    provider = (provider or "auto").lower()
    errors: list[str] = []
    if provider in ("auto", "breeze") and api_key:
        try:
            return _breeze(text, voice_id, model_id, api_key), "breeze"
        except Exception as exc:  # noqa: BLE001 - fall through to edge
            errors.append(f"breeze: {exc}")
    if provider in ("auto", "edge"):
        try:
            return _edge(text, edge_voice), "edge"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"edge: {exc}")
    raise RuntimeError("语音合成失败：" + "；".join(errors))


def apply(ctx) -> None:
    cfg = _config(ctx)
    api_key = str(cfg.get("api_key") or "").strip()
    default_voice = str(cfg.get("voice_id") or DEFAULT_VOICE).strip()
    default_model = str(cfg.get("model_id") or DEFAULT_MODEL).strip()
    provider = str(cfg.get("provider") or "auto").strip()
    edge_voice = str(cfg.get("edge_voice") or EDGE_VOICE).strip()

    def _save(audio: bytes) -> str:
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.mp3"
        (VOICE_DIR / name).write_bytes(audio)
        return name

    def send_voice(args: dict) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "缺少 text"}
        text = text[:400]
        voice_id = str(args.get("voice_id") or default_voice).strip()
        try:
            audio, used = _synthesize(text, voice_id, default_model, api_key,
                                      provider, edge_voice)
        except Exception as exc:
            return {"error": str(exc)[:300]}
        name = _save(audio)
        relative = f"voice/{name}"
        host_path = f"{HOST_DATA_DIR}/{relative}"

        channel = str(args.get("channel") or "").strip().lower()
        chat_id = str(args.get("chat_id") or "").strip()
        if channel and chat_id:
            store = ctx.resource("store")
            oid = store.enqueue_channel(
                channel, chat_id, text,
                platform=str(args.get("platform") or "").strip(),
                media_type="audio", media_path=relative)
            return {"queued": oid, "channel": channel, "chat_id": chat_id,
                    "bytes": len(audio), "provider": used, "file": host_path}

        voice_pending.put(trace_ctx.get_trace_id(), host_path)
        return {"ok": True, "mode": "reply", "bytes": len(audio),
                "provider": used, "file": host_path,
                "note": "已附加到本轮回复，随文字一起发出"}

    def tts(args: dict) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "缺少 text"}
        voice_id = str(args.get("voice_id") or default_voice).strip()
        try:
            audio, used = _synthesize(text[:400], voice_id, default_model,
                                      api_key, provider, edge_voice)
        except Exception as exc:
            return {"error": str(exc)[:300]}
        name = _save(audio)
        return {"file": f"{HOST_DATA_DIR}/voice/{name}", "bytes": len(audio),
                "provider": used}

    ctx.tools.register(
        "send_voice",
        "把一段文字合成为语音，作为本轮回复发出去（QQ 等渠道）。用户要求语音回复时调用。",
        _schema({
            "text": {"type": "string", "description": "要朗读的文字，最多 400 字"},
            "voice_id": {"type": "string", "description": "可选，覆盖默认音色"},
            "channel": {"type": "string", "description": "可选，配合 chat_id 做主动推送"},
            "chat_id": {"type": "string", "description": "可选，主动推送目标"},
            "platform": {"type": "string", "description": "可选，主动推送平台"},
        }, ["text"]),
        send_voice, aliases=("send_voice", "voice_reply"),
    )
    ctx.tools.register(
        "tts",
        "把文字合成为 mp3 语音文件并返回路径，不发送。",
        _schema({
            "text": {"type": "string", "description": "要朗读的文字，最多 400 字"},
            "voice_id": {"type": "string", "description": "可选，覆盖默认音色"},
        }, ["text"]),
        tts, aliases=("tts", "text_to_speech"),
    )
    ctx.log.info("voice tools registered (provider=%s voice=%s)", provider,
                 default_voice)
