"""WeChat Agent Router: notifications, replies, data Q&A, credentials and scheduled jobs."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agent_broker import BROKER
from attachments import AttachmentError, Attachments
from timeutil import CST
from hub import Hub, HubError, parse_quote, parse_text
from imagegen import ImageGen
from llm import LLM, LLMError
from plugin_loader import PluginError, PluginLoader
from registry import JobScheduler, ToolRegistry
from settings_service import SettingsService
from store import Store, now_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("router")


class RingLogHandler(logging.Handler):
    """Keep a bounded in-memory log tail for the desktop console."""

    def __init__(self, capacity: int = 800):
        super().__init__()
        self.items: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = str(record.msg)
        with self._lock:
            self._seq += 1
            self.items.append({
                "id": self._seq,
                "ts": datetime.fromtimestamp(record.created, timezone.utc).astimezone(CST).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": message[:4000],
            })

    def since(self, after: int = 0, limit: int = 300) -> list[dict]:
        with self._lock:
            return [item for item in self.items if item["id"] > after][-limit:]


LOG_RING = RingLogHandler()
LOG_RING.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(LOG_RING)

CONFIG_PATH = os.environ.get("ROUTER_CONFIG", "/app/config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
    CFG = json.load(fh)


def _configured(value) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.upper().startswith("REPLACE")


_required = {
    "hub.channel_key": (CFG.get("hub") or {}).get("channel_key"),
    "hub.owner_id": (CFG.get("hub") or {}).get("owner_id"),
    "service_token": CFG.get("service_token"),
    "login_token_secret": CFG.get("login_token_secret"),
    "credential_key": CFG.get("credential_key"),
    "llm.api_key": (CFG.get("llm") or {}).get("api_key"),
}
for _agent in CFG.get("agents", []):
    _required[f"agents[{_agent.get('id')}].api_key"] = _agent.get("api_key")
_missing = [key for key, value in _required.items() if not _configured(value)]
if _missing:
    raise RuntimeError("配置未完成，请修改 config.json：" + ", ".join(_missing))

BASE = CFG["hub"]["base_url"]
OWNER = CFG["hub"]["owner_id"]
DATA_DIR = Path(CFG.get("data_dir", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR = DATA_DIR / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

store = Store(CFG["db"])
store.init()
hub = Hub(BASE, CFG["hub"]["channel_key"], OWNER)
llm = LLM(CFG["llm"]["base_url"], CFG["llm"]["api_key"], CFG["llm"]["model"])
ATTACH_CFG = CFG.get("attachments") or {}
ATTACHMENTS = Attachments(
    hub, llm,
    base_dir=str(ATTACH_CFG.get("dir") or "/workspace/inbox"),
    enabled=bool(ATTACH_CFG.get("enabled", True)),
    max_files=int(ATTACH_CFG.get("max_files", 3)),
    max_image_bytes=int(float(ATTACH_CFG.get("max_image_mb", 4)) * 1024 * 1024),
    max_file_bytes=int(float(ATTACH_CFG.get("max_file_mb", 10)) * 1024 * 1024),
    max_text_chars=int(ATTACH_CFG.get("text_chars", 20000)),
)
AGENTS = {item["id"]: item for item in CFG.get("agents", [])}
IMAGE_CFG = CFG.get("image") or {}
IMAGEGEN = ImageGen(
    IMAGE_CFG.get("base_url") or "https://api.siliconflow.cn/v1",
    IMAGE_CFG.get("api_key", ""),
    IMAGE_CFG.get("model") or "Kwai-Kolors/Kolors",
    enabled=bool(IMAGE_CFG.get("enabled", True)),
    size=IMAGE_CFG.get("size") or "1024x1024",
    steps=int(IMAGE_CFG.get("steps", 20)),
    guidance=float(IMAGE_CFG.get("guidance", 7.5)),
)
GENERATED_DIR = Path(IMAGE_CFG.get("dir") or "/workspace/generated")
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
LOGIN_SECRET = CFG["login_token_secret"].encode()
SERVICE_TOKEN = CFG["service_token"]
PUBLIC_BASE = CFG["public_base_url"].rstrip("/")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"\b([" + CODE_ALPHABET + r"]{4})\b")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_login_token(service: str, ttl: int = 1800) -> str:
    exp = int(time.time()) + ttl
    raw = f"{service}.{exp}"
    sig = hmac.new(LOGIN_SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{raw}.{sig}".encode()).decode().rstrip("=")


def verify_login_token(service: str, token: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        svc, exp, sig = decoded.rsplit(".", 2)
    except Exception:
        return False
    if svc != service or int(exp) < time.time():
        return False
    expected = hmac.new(LOGIN_SECRET, f"{svc}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


def login_url(service: str) -> str:
    return f"{PUBLIC_BASE}/agent-login/{service}?token={make_login_token(service)}"


def auth_agent(request: Request) -> dict:
    key = request.headers.get("x-api-key", "")
    auth = request.headers.get("authorization", "")
    if not key and auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    for agent in AGENTS.values():
        if key and hmac.compare_digest(agent.get("api_key", ""), key):
            return agent
    raise HTTPException(401, "invalid agent key")


def service_auth(request: Request) -> None:
    token = request.headers.get("x-service-token", "")
    if not hmac.compare_digest(token, SERVICE_TOKEN):
        raise HTTPException(401, "invalid service token")


def send_payload(payload: dict) -> bool:
    try:
        if payload.get("kind") == "image":
            hub.send_image(payload["path"], payload.get("text", ""), payload.get("recipient"))
        else:
            hub.send_text(payload.get("text", ""), payload.get("recipient"))
        return True
    except HubError as exc:
        if exc.status in (409, 503, 0):
            store.enqueue_outbox(payload)
            log.warning("hub send queued (%s): %s", exc.status, exc.message[:200])
            return False
        if exc.status == 401:
            log.error("hub channel key rejected: %s", exc.message[:200])
            return False
        log.error("hub send failed: %s", exc)
        return False


def send_text(text: str) -> bool:
    return send_payload({"kind": "text", "text": text, "recipient": OWNER})


def flush_outbox() -> None:
    for item in store.list_outbox(10):
        payload = item["payload"]
        try:
            if payload.get("kind") == "image":
                hub.send_image(payload["path"], payload.get("text", ""), payload.get("recipient"))
            else:
                hub.send_text(payload.get("text", ""), payload.get("recipient"))
            store.drop_outbox(item["id"])
            log.info("outbox %s flushed", item["id"])
        except HubError as exc:
            store.bump_outbox(item["id"])
            if exc.status in (401, 400, 409, 503):
                break
        except Exception:
            store.bump_outbox(item["id"])
            break


def new_code() -> str:
    for _ in range(30):
        code = "".join(CODE_ALPHABET[b % len(CODE_ALPHABET)] for b in os.urandom(4))
        if not store.by_code(code):
            return code
    raise RuntimeError("no code available")


def call_agent(agent: dict, payload: dict, timeout: int = 20) -> dict:
    if agent.get("transport") == "poll":
        wait_seconds = int(CFG.get("agent_wait_seconds", 60))
        result, task_id, done = BROKER.enqueue(agent["id"], {
            "message": payload.get("reply", ""),
            "engine": payload.get("engine", ""),
            "workspace": payload.get("workspace", ""),
            "chat_id": payload.get("chat_id", OWNER),
            "meta": payload.get("meta") or {},
            "wait_seconds": wait_seconds,
        }, wait_seconds)
        if result:
            return {"text": result.get("text", ""), "final": True}
        return {"text": f"任务已交给 {agent.get('name', agent['id'])}，完成后会通知你。",
                "final": False, "task_id": task_id}
    body = json.dumps(payload, ensure_ascii=False).encode()
    sig = hmac.new(agent.get("callback_secret", "").encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(agent["callback_url"], data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Router-Signature", sig)
    req.add_header("X-Router-Timestamp", str(int(time.time())))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def agent_auth(request: Request) -> dict:
    key = request.headers.get("x-agent-key", "")
    auth = request.headers.get("authorization", "")
    if not key and auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    for agent in AGENTS.values():
        if key and hmac.compare_digest(agent.get("api_key", ""), key):
            return agent
    raise HTTPException(401, "invalid agent key")


def send_agent_result(result: dict) -> None:
    text = (result.get("text") or "").strip()
    image = result.get("image_base64")
    if image:
        try:
            raw = base64.b64decode(image.split(",")[-1])
            path = IMAGE_DIR / f"{uuid.uuid4().hex}.png"
            path.write_bytes(raw)
            send_payload({"kind": "image", "path": str(path), "text": text or "请查看图片", "recipient": OWNER})
            return
        except Exception:
            pass
    send_text(text or "已收到你的回复，正在处理。")


# ---------------------------------------------------------------------------
# credentials and capabilities
# ---------------------------------------------------------------------------
TOOLS = ToolRegistry()
JOBS: list = []
SERVICES: dict = {}
SETTINGS = SettingsService(CFG, store, CONFIG_PATH)
LOADER = PluginLoader(
    plugins_dir=Path("/app/plugins"),
    tools=TOOLS,
    jobs=JOBS,
    resources={"store": store, "agents": AGENTS,
               "call_agent": call_agent, "settings": SETTINGS},
    root_config={"jobs": CFG.get("jobs", {}), "plugins": CFG.get("plugins", {}),
                 "owner": OWNER, "disabled_plugins": CFG.get("disabled_plugins", [])},
    send_fn=send_text,
    services=SERVICES,
    secrets={"credential_key": CFG["credential_key"], "service_token": SERVICE_TOKEN,
             "llm_api_key": CFG["llm"]["api_key"],
             "ops_token": (CFG.get("ops_agent") or {}).get("token", "")},
    login_url_fn=login_url,
    audit_fn=lambda plugin_id, action, detail: store.log_plugin_event(plugin_id, action, detail),
)
LOADER.load_all()


def refresh_services() -> None:
    global LEETCODE, CREDENTIALS, REMINDERS
    LEETCODE = SERVICES.get("leetcode")
    REMINDERS = SERVICES.get("reminders")
    CREDENTIALS = {key.split(":", 1)[1]: value for key, value in SERVICES.items()
                   if key.startswith("credential:")}


def rebuild_scheduler() -> None:
    global SCHEDULER
    SCHEDULER = JobScheduler(store, JOBS)
    SETTINGS.scheduler = SCHEDULER


refresh_services()
rebuild_scheduler()


def reload_agents() -> None:
    global SYSTEM_PROMPT
    AGENTS.clear()
    AGENTS.update({item["id"]: item for item in CFG.get("agents", [])})
    if "agents" in LOADER.loaded:
        try:
            LOADER.reload("agents")
        except Exception:
            log.exception("agents plugin refresh failed")
    try:
        SYSTEM_PROMPT = build_system_prompt()
    except Exception:
        log.exception("system prompt refresh failed")


def reload_llm() -> None:
    cfg = CFG.get("llm") or {}
    llm.endpoint = str(cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
    llm.api_key = str(cfg.get("api_key") or "")
    llm.model = str(cfg.get("model") or "")


def reload_hub() -> None:
    global BASE, hub
    BASE = str(CFG["hub"]["base_url"]).rstrip("/")
    hub = Hub(BASE, CFG["hub"]["channel_key"], OWNER)


def reload_public() -> None:
    global PUBLIC_BASE
    PUBLIC_BASE = str(CFG.get("public_base_url") or "").rstrip("/")


SETTINGS.attach(LOADER, SCHEDULER, rebuild_scheduler, refresh_services, TOOLS,
                reload_agents=reload_agents, reload_llm=reload_llm,
                reload_hub=reload_hub, reload_public=reload_public)


def send_media_tool(args: dict) -> dict:
    raw = str(args.get("path") or "").strip()
    if not raw:
        return {"error": "缺少 path"}
    path = Path(raw).resolve()
    roots = (Path("/workspace").resolve(), IMAGE_DIR.resolve())
    if not any(path == root or root in path.parents for root in roots):
        return {"error": "只允许发送 /workspace 或 /data/images 下的文件"}
    if not path.is_file():
        return {"error": f"文件不存在：{path}"}
    size = path.stat().st_size
    if size > 32 * 1024 * 1024:
        return {"error": f"文件过大：{size} 字节（上限 32MB）"}
    try:
        hub.send_file(str(path), str(args.get("caption") or ""))
    except Exception as exc:
        return {"error": f"发送失败：{exc}"}
    return {"ok": True, "path": str(path), "size": size}


TOOLS.register(
    "send_media",
    "把服务器上的图片、视频或文件发到微信。路径必须在 /workspace 或 /data/images 下，单文件不超过 32MB。",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "服务器绝对路径"},
        "caption": {"type": "string", "description": "可选说明文字"},
    }, "required": ["path"]},
    send_media_tool,
    aliases=("send_file", "send_image"),
)


def generate_image_tool(args: dict) -> dict:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "缺少 prompt"}
    if not IMAGEGEN.enabled:
        return {"error": "还没有配置生图 API"}
    try:
        path = IMAGEGEN.generate_to_file(prompt, GENERATED_DIR, args.get("size"))
        hub.send_file(str(path), str(args.get("caption") or "画好了"))
    except Exception as exc:
        return {"error": f"生图失败：{exc}"}
    return {"ok": True, "path": str(path), "prompt": prompt[:200]}


TOOLS.register(
    "generate_image",
    "根据文字描述生成图片并发到微信（SiliconFlow Kolors）。用户让你画图、画自画像、生成插画时使用。",
    {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "详细的中文画面描述"},
        "size": {"type": "string", "description": "可选尺寸，如 1024x1024、960x1280、768x1024"},
        "caption": {"type": "string", "description": "可选图片说明"},
    }, "required": ["prompt"]},
    generate_image_tool,
    aliases=("draw_image", "text_to_image"),
)


def build_system_prompt() -> str:
    lines = [
        CFG["llm"].get("system_prompt", ""),
        "",
        "## 微信网关职责",
        "你在微信里服务站主。回答要简洁、口语化，不要 Markdown 表格。",
        "站点访问、访客、IP 归属地、AI 对话内容必须调用工具查询，绝不能编造数字；查不到就直说。",
        "力扣/LeetCode/刷题/做题相关的问题调用 leetcode_stats。",
        "用户发图片或文件时，系统会把图片交给视觉模型、把文件文字附在消息里；只根据实际提供的内容回答，不要假装看到了没读到的东西。",
        "用户要求某个项目 Agent 做事时调用 send_to_agent，不要假装已经完成。",
        "用户消息以 @AgentID 开头时，系统会直接路由到该 Agent，不经过你；你可以按普通对话理解。",
        "如果用户是在回复某条待处理通知，系统会先做匹配；你不需要猜。",
        "不确定用户指哪个 Agent 时，先反问一句。",
        f"当前时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai，UTC+8）。",
        "用户说“提醒我/到点提醒/每X分钟提醒/每天X点提醒”时调用 create_reminder；一次性时间换算成带 +08:00 的 ISO。",
        "查看、取消、暂停、推迟提醒用对应工具；取消时如果返回 need_choice，先让用户从候选里选。",
        "用户要求给 bot 增加新的 HTTP API 功能时调用 propose_plugin；必须提供真实 API 地址、方法、参数，不能编造。",
        "用户问定时任务多久一次、下次什么时候、修改播报间隔、启停插件时，调用 admin 的 list_jobs / get_job / set_job_interval / set_job_enabled / list_plugins 等工具，不要猜。",
        "用户要求查看/修改服务器文件时调用 ops 的 fs_list / fs_read / fs_write / fs_replace / fs_delete / fs_move；写或删会发微信审批。允许的根目录：/app、/app/plugins、/workspace。",
        "用户要求把服务器上的图片、视频或文件发到微信时调用 send_media；路径只允许 /workspace 或 /data/images。",
        "用户让你画图、生成图片、画自画像时调用 generate_image，不要用 ASCII、HTML 或代码块糊弄；prompt 用详细中文描述，生成后会自动发到微信。",
        "代码检查任务：只读目标插件的 plugin.py、adapter.py、manifest.json，最多读 3 个文件；不要列 /app 根目录；拿到 job 定义和 adapter 逻辑后立即回答。",
        "可用 Agent：",
    ]
    for agent in AGENTS.values():
        if agent.get("id") == "echo":
            continue
        lines.append(f"- {agent['id']}：{agent.get('name','')}，{agent.get('description','')}")
    if not AGENTS:
        lines.append("- （暂无）")
    return "\n".join(lines)


SYSTEM_PROMPT = build_system_prompt()


def llm_answer(text: str, history: list[dict]) -> str:
    messages = [{"role": "system", "content": build_system_prompt()}]
    for item in history:
        if item["role"] in ("user", "assistant"):
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": text})
    tool_trace: list[dict] = []
    seen: dict[str, dict] = {}
    for round_no in range(6):
        message = llm.complete(messages, TOOLS.definitions())
        calls = message.get("tool_calls") or []
        if not calls:
            return (message.get("content") or "").strip() or "我在，但这次没想出怎么回答。"
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            signature = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
            if signature in seen:
                log.info("llm repeated tool call: %s", signature[:300])
                previous = json.dumps(seen[signature], ensure_ascii=False, default=str)[:2000]
                return f"我重复调用了 {name}，先停一下。上一次结果是：\n{previous}\n你想继续查哪一部分？"
            seen[signature] = {"pending": True}
            log.info("llm round %s tool=%s args=%s", round_no, name,
                     json.dumps(args, ensure_ascii=False)[:300])
            result = TOOLS.execute(name, args)
            seen[signature] = result
            tool_trace.append({"name": name, "args": args, "result": result})
            log.info("llm tool result %s: %s", name, str(result)[:300])
            encoded = json.dumps(result, ensure_ascii=False, default=str)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": encoded[:16000]})
    if tool_trace:
        try:
            messages.append({"role": "user",
                             "content": "请基于以上工具结果直接回答，不要再调用工具；信息不足就说明缺什么。"})
            final = llm.complete(messages, None)
            content = (final.get("content") or "").strip()
            if content:
                return content
        except Exception as exc:
            log.warning("final summarization failed: %s", exc)
        reads = [item for item in tool_trace if "fs_read" in item["name"]]
        if reads:
            payload = json.dumps(reads[-1]["result"], ensure_ascii=False, default=str)
            return f"我读到的内容：\n{payload[:1500]}"
        last = tool_trace[-1]
        result = last["result"]
        if isinstance(result, dict) and result.get("error"):
            return f"工具 {last['name']} 报错了：{str(result['error'])[:300]}。要不要换个查法？"
        payload = json.dumps(result, ensure_ascii=False, default=str)
        return f"我查到一些结果，但工具调用到上限了：\n{payload[:800]}"
    return "我没能完成这次查询，换个更具体的问法试试？"


# ---------------------------------------------------------------------------
# inbound handling
# ---------------------------------------------------------------------------
def _squash(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


EXPLICIT_AGENT_RE = re.compile(r"^\s*@([a-z][a-z0-9_-]{1,63})(?:\s+([\s\S]+))?$")


def parse_explicit_agent(text: str):
    match = EXPLICIT_AGENT_RE.match(text or "")
    if not match:
        return None
    return match.group(1), (match.group(2) or "").strip()


def match_quote(quoted: str, pending: list[dict]) -> dict | None:
    """Match a quoted message to exactly one pending notification by content."""
    probe = _squash(quoted)
    if len(probe) < 6:
        return None
    hits: list[dict] = []
    for item in pending:
        body = _squash(item.get("text") or "")
        if len(body) < 6:
            continue
        if body[:60] in probe or (len(probe) >= 12 and probe in body):
            hits.append(item)
    return hits[0] if len(hits) == 1 else None


def match_pending(text: str, chat_id: str, quote: dict | None = None) -> dict | str | None:
    pending = store.active_pending(chat_id)
    match = CODE_RE.search(text.upper())
    if match:
        found = store.by_code(match.group(1))
        if found:
            return found
    quoted = str((quote or {}).get("text") or (quote or {}).get("title") or "").strip()
    if quote is not None:
        qmatch = CODE_RE.search(quoted.upper())
        if qmatch:
            found = store.by_code(qmatch.group(1))
            if found:
                return found
        # A quoted message is an explicit target. If it is not one of the
        # pending notifications, treat this as a normal chat message.
        found = match_quote(quoted, pending)
        if found:
            return found
        return None
    if len(pending) == 1:
        return pending[0]
    if len(pending) > 1:
        short = text.strip().lower()
        if short in {"好了", "已登录", "完成", "ok", "好", "可以了", "登录好了"}:
            newest = pending[0]
            try:
                age = time.time() - datetime.fromisoformat(newest["created_at"]).timestamp()
            except Exception:
                age = 9999
            if age < 600:
                return newest
        lines = ["现在有多个待回复，请带上编号回我："]
        for item in pending[:5]:
            lines.append(f"- {item['code']}：{item['agent_id']}，{item['text'][:40]}")
        send_text("\n".join(lines))
        return "ASKED"
    return None


def handle_reply(notification: dict, text: str) -> None:
    agent = AGENTS.get(notification["agent_id"])
    store.mark_replied(notification["id"], text)
    if notification["agent_id"] == "evolution" and SERVICES.get("evolution"):
        try:
            result = SERVICES["evolution"].handle_reply(notification, text)
            send_agent_result(result)
            store.set_status(notification["id"], "answered")
        except Exception as exc:
            log.warning("evolution reply failed: %s", exc)
            send_text(f"插件安装处理失败：{exc}")
        return
    if notification["agent_id"] == "ops" and SERVICES.get("ops"):
        try:
            result = SERVICES["ops"].handle_reply(notification, text)
            send_agent_result(result)
            store.set_status(notification["id"], "answered")
        except Exception as exc:
            log.warning("ops reply failed: %s", exc)
            send_text(f"文件操作失败：{exc}")
        return
    if not agent:
        send_text("这条通知对应的 Agent 已下线。")
        return
    try:
        result = call_agent(agent, {
            "notification_id": notification["id"],
            "agent_id": agent["id"],
            "reply": text,
            "meta": notification.get("meta") or {},
            "chat_id": OWNER,
        })
        send_agent_result(result)
        if result.get("final", True):
            store.set_status(notification["id"], "answered")
    except Exception as exc:
        log.warning("agent callback failed: %s", exc)
        send_text(f"已收到，但 {agent.get('name', agent['id'])} 暂时没有响应，稍后我会再试。")


def answer_with_attachments(text: str, history: list[dict], bundle: dict) -> str:
    notes = list(bundle.get("notes") or [])
    errors = list(bundle.get("errors") or [])
    if errors:
        notes.append("附件处理失败：\n" + "\n".join(errors))
    images = bundle.get("images") or []
    if images:
        prompt = text or "请说明这些图片里有什么。"
        if notes:
            prompt += "\n\n附件信息：\n" + "\n\n".join(notes)
        vision = ATTACHMENTS.describe(images, prompt)
        if not notes:
            return vision
        combined = [f"用户发了图片。视觉模型看到：\n{vision}",
                    "附件内容：\n" + "\n\n".join(notes)]
        if text:
            combined.append("用户问题：" + text)
        return llm_answer("\n\n".join(combined), history)
    prompt = text or "请查看我发的附件。"
    if notes:
        prompt += "\n\n[附件内容]\n" + "\n\n".join(notes)
    return llm_answer(prompt, history)


def handle_inbound(text: str, message: dict) -> None:
    text = (text or "").strip()
    quote = parse_quote(message)
    try:
        bundle = ATTACHMENTS.collect(message)
    except Exception as exc:
        log.warning("attachment collect failed: %s", exc)
        bundle = {"items": 0, "images": [], "notes": [], "errors": [str(exc)], "saved": []}
    has_attachments = bool(bundle.get("items"))
    if not text and not has_attachments:
        send_text("暂不支持这种消息类型，发文字给我吧。")
        return
    history = store.recent_messages(OWNER, 10)
    display = text or ("[图片]" if bundle.get("images") else "[附件]")
    store.add_message(OWNER, "user", display)
    flush_outbox()
    if text in {"力扣登录", "更新力扣", "绑定力扣", "leetcode登录"}:
        if LEETCODE is not None:
            LEETCODE.notify_login("missing", force=True)
        return
    if not has_attachments:
        pending = match_pending(text, OWNER, quote)
        if pending == "ASKED":
            return
        if isinstance(pending, dict):
            store.log_route(OWNER, text, "pending", pending["code"])
            handle_reply(pending, text)
            return
        explicit = parse_explicit_agent(text)
        if explicit:
            agent_id, task_text = explicit
            agent = AGENTS.get(agent_id)
            if agent is None or agent_id == "echo":
                send_text(f"没有这个 Agent：@{agent_id}。可用：{', '.join(k for k in AGENTS if k != 'echo')}")
                return
            if not task_text:
                send_text(f"要给 {agent.get('name', agent_id)} 什么任务？")
                return
            try:
                result = call_agent(agent, {
                    "notification_id": "",
                    "agent_id": agent_id,
                    "reply": task_text,
                    "meta": {"kind": "task", "explicit": True},
                    "chat_id": OWNER,
                })
                send_agent_result(result)
                store.log_route(OWNER, text, "agent", agent_id)
            except Exception as exc:
                log.warning("explicit agent call failed: %s", exc)
                send_text(f"已经收到，但 {agent.get('name', agent_id)} 暂时没有响应。")
            return
    try:
        answer = answer_with_attachments(text, history, bundle)
    except AttachmentError as exc:
        log.error("attachment vision failed: %s", exc)
        answer = f"附件收到了，但识图失败：{exc}"
    except LLMError as exc:
        log.error("llm failed: %s", exc)
        answer = "AI 暂时不可用，稍后我再试。"
    store.log_route(OWNER, display, "llm", "attachment" if has_attachments else "")
    store.add_message(OWNER, "assistant", answer)
    send_text(answer)


# ---------------------------------------------------------------------------
# background loops
# ---------------------------------------------------------------------------
def poll_once(last_id: int) -> int:
    messages = hub.poll(last_id)
    for message in messages:
        mid = int(message.get("id") or 0)
        if mid <= last_id:
            continue
        last_id = mid
        store.state_set("last_message_id", str(last_id))
        if message.get("direction") != "inbound":
            continue
        if message.get("from_user_id") != OWNER:
            continue
        handle_inbound(parse_text(message), message)
    return last_id


async def poll_loop() -> None:
    last_id = int(store.state_get("last_message_id", str(CFG.get("initial_last_id", 0))) or 0)
    while True:
        try:
            last_id = await asyncio.to_thread(poll_once, last_id)
        except Exception as exc:
            log.warning("poll failed: %s", exc)
        await asyncio.sleep(float(CFG.get("poll_interval", 2)))


async def maintenance_loop() -> None:
    last_check = 0.0
    while True:
        try:
            store.expire_stale()
            flush_outbox()
            if time.time() - last_check > 6 * 3600:
                last_check = time.time()
                if LEETCODE is not None:
                    await asyncio.to_thread(LEETCODE.get_stats, 5)
        except Exception as exc:
            log.warning("maintenance failed: %s", exc)
        await asyncio.sleep(30)


async def job_loop() -> None:
    while True:
        try:
            results = await asyncio.to_thread(SCHEDULER.tick)
            for item in results:
                log.info("job %s: %s", item["id"], item["status"])
        except Exception as exc:
            log.warning("job tick failed: %s", exc)
        await asyncio.sleep(30)


async def reminder_loop() -> None:
    while True:
        try:
            if REMINDERS is not None:
                fired = await asyncio.to_thread(REMINDERS.tick)
                for item in fired:
                    log.info("reminder fired: %s %s", item["id"], item["status"])
        except Exception as exc:
            log.warning("reminder tick failed: %s", exc)
        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class NotifyIn(BaseModel):
    text: str = ""
    image_base64: str | None = None
    expect_reply: bool = False
    ttl_seconds: int = Field(default=900, ge=60, le=86400)
    meta: dict = Field(default_factory=dict)
    thread_id: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    tasks = [
        asyncio.create_task(poll_loop()),
        asyncio.create_task(maintenance_loop()),
        asyncio.create_task(job_loop()),
        asyncio.create_task(reminder_loop()),
    ]
    log.info("router started, owner=%s agents=%s plugins=%s services=%s tools=%s jobs=%s",
             OWNER, list(AGENTS), list(LOADER.loaded), list(SERVICES), TOOLS.names(), list(SCHEDULER.jobs))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="WeChat Agent Router", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    return {"ok": True, "time": now_iso(), "agents": list(AGENTS), "tools": TOOLS.names(),
            "jobs": list(SCHEDULER.jobs), "plugins": list(LOADER.loaded),
            "plugin_errors": LOADER.errors, "services": list(SERVICES),
            "credentials": list(CREDENTIALS), "agent_broker": BROKER.status()}


@app.post("/v1/notify")
def notify(body: NotifyIn, request: Request):
    agent = auth_agent(request)
    if not body.text and not body.image_base64:
        raise HTTPException(400, "text or image_base64 required")
    text = body.text.strip()[:2000]
    image_path = None
    if body.image_base64:
        raw = base64.b64decode(body.image_base64.split(",")[-1], validate=False)
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(400, "image too large (max 2MB)")
        ext = ".png" if raw.startswith(b"\x89PNG") else ".jpg"
        path = IMAGE_DIR / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(raw)
        image_path = str(path)

    code = new_code() if body.expect_reply else ""
    notification = store.create_notification(
        agent_id=agent["id"], chat_id=OWNER, text=text, code=code,
        expect_reply=body.expect_reply, ttl_seconds=body.ttl_seconds,
        image_path=image_path, meta=body.meta, thread_id=body.thread_id,
    )
    header = f"【{agent.get('name', agent['id'])}】"
    if body.expect_reply:
        main_text = (f"{header}回复编号：{code}\n{text}" if text
                     else f"{header}回复编号：{code}")
    else:
        main_text = f"{header}{text}" if text else f"{header}请查看图片。"
    if image_path:
        send_payload({"kind": "image", "path": image_path, "text": f"{header}请查看图片", "recipient": OWNER})
        ok = send_text(main_text)
    else:
        ok = send_text(main_text)
    return {"id": notification["id"], "code": code, "status": "sent" if ok else "queued"}


@app.get("/v1/notify/{nid}")
def notify_status(nid: str, request: Request):
    agent = auth_agent(request)
    item = store.get_notification(nid)
    if not item or item["agent_id"] != agent["id"]:
        raise HTTPException(404, "not found")
    return {key: item[key] for key in ("id", "code", "status", "reply_text", "created_at", "expires_at")}


@app.post("/agent/register")
async def agent_register(request: Request):
    agent = agent_auth(request)
    body = await request.json()
    BROKER.register(agent["id"], body)
    return {"ok": True, "agent_id": agent["id"]}


@app.get("/agent/tasks")
def agent_tasks(request: Request, agent_id: str = ""):
    agent = agent_auth(request)
    if agent_id and agent_id != agent["id"]:
        raise HTTPException(403, "agent mismatch")
    task = BROKER.take(agent["id"])
    if not task:
        return Response(status_code=204)
    return task


@app.post("/agent/result")
async def agent_result(request: Request):
    agent = agent_auth(request)
    body = await request.json()
    task_id = str(body.get("task_id") or "")
    if not task_id:
        raise HTTPException(400, "task_id required")
    ok = BROKER.complete(task_id, bool(body.get("ok")), str(body.get("text") or ""))
    return {"ok": ok}


@app.post("/v1/notify/{nid}/cancel")
def notify_cancel(nid: str, request: Request):
    agent = auth_agent(request)
    item = store.get_notification(nid)
    if not item or item["agent_id"] != agent["id"]:
        raise HTTPException(404, "not found")
    if item["status"] == "pending":
        store.set_status(nid, "cancelled")
    return {"ok": True, "status": store.get_notification(nid)["status"]}


@app.post("/internal/leetcode/stats")
def leetcode_stats(request: Request):
    service_auth(request)
    if LEETCODE is None:
        raise HTTPException(503, "leetcode plugin unavailable")
    return LEETCODE.get_stats(10)


@app.post("/internal/ask")
async def internal_ask(request: Request):
    service_auth(request)
    body = await request.json()
    answer = await asyncio.to_thread(llm_answer, body.get("text", ""), [])
    return {"answer": answer}


@app.post("/internal/jobs/{job_id}/run")
def run_job(job_id: str, request: Request):
    service_auth(request)
    return SCHEDULER.run_by_id(job_id)


def plugin_state() -> dict:
    return {"plugins": LOADER.describe(), "errors": LOADER.errors,
            "services": list(SERVICES), "tools": TOOLS.names(),
            "jobs": list(SCHEDULER.jobs)}


@app.get("/internal/plugins")
def list_plugins(request: Request):
    service_auth(request)
    return plugin_state() | {"events": store.list_plugin_events(30)}


@app.post("/internal/plugins/{pid}/reload")
def reload_plugin(pid: str, request: Request):
    service_auth(request)
    try:
        LOADER.reload(pid)
    finally:
        rebuild_scheduler()
        refresh_services()
    return plugin_state()


@app.post("/internal/plugins/{pid}/disable")
def disable_plugin(pid: str, request: Request):
    service_auth(request)
    if not LOADER.unload(pid):
        raise HTTPException(404, "plugin not loaded")
    rebuild_scheduler()
    refresh_services()
    return plugin_state()


@app.post("/internal/plugins/{pid}/enable")
def enable_plugin(pid: str, request: Request):
    service_auth(request)
    if not LOADER.enable(pid):
        raise HTTPException(409, "plugin already loaded")
    rebuild_scheduler()
    refresh_services()
    return plugin_state()


# ---------------------------------------------------------------------------
# settings API for the desktop console (service token only)
# ---------------------------------------------------------------------------
def _settings_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except PluginError as exc:
        raise HTTPException(400, str(exc)) from None


def _agents_with_presence() -> list[dict]:
    broker = BROKER.status()
    items = []
    for item in SETTINGS.agents_view():
        info = broker["agents"].get(item["id"])
        item["online"] = bool(info and info.get("online"))
        item["last_seen"] = info.get("last_seen") if info else None
        item["runtime"] = (info or {}).get("meta") or {}
        items.append(item)
    return items


@app.get("/internal/settings/overview")
def settings_overview(request: Request):
    service_auth(request)
    return {
        "time": now_iso(),
        "version": "1.1.0",
        "agents": _agents_with_presence(),
        "plugins": SETTINGS.plugins(),
        "plugin_errors": LOADER.errors,
        "jobs": SETTINGS.list_jobs(),
        "config": SETTINGS.config_view(),
        "services": list(SERVICES),
        "tools": TOOLS.names(),
        "credentials": list(CREDENTIALS),
        "broker": BROKER.status(),
        "events": store.list_plugin_events(20),
        "logs": LOG_RING.since(0, 80),
    }


@app.get("/internal/settings/config")
def settings_config(request: Request, section: str = "all"):
    service_auth(request)
    return SETTINGS.config_view(section)


@app.patch("/internal/settings/config")
async def settings_config_set(request: Request):
    service_auth(request)
    body = await request.json()
    key = str(body.get("key") or "")
    if not key:
        raise HTTPException(400, "key required")
    return _settings_call(SETTINGS.config_set, key, body.get("value"))


@app.get("/internal/settings/agents")
def settings_agents(request: Request):
    service_auth(request)
    return {"agents": _agents_with_presence(), "broker": BROKER.status()}


@app.post("/internal/settings/agents")
async def settings_agent_upsert(request: Request):
    service_auth(request)
    body = await request.json()
    return _settings_call(SETTINGS.upsert_agent, body)


@app.delete("/internal/settings/agents/{agent_id}")
def settings_agent_delete(agent_id: str, request: Request):
    service_auth(request)
    return _settings_call(SETTINGS.delete_agent, agent_id)


@app.get("/internal/settings/plugins")
def settings_plugins(request: Request):
    service_auth(request)
    return {"plugins": SETTINGS.plugins(), "errors": LOADER.errors,
            "events": store.list_plugin_events(30)}


@app.post("/internal/settings/plugins/{pid}/reload")
def settings_plugin_reload(pid: str, request: Request):
    service_auth(request)
    return _settings_call(SETTINGS.reload_plugin, pid)


@app.post("/internal/settings/plugins/{pid}/enable")
def settings_plugin_enable(pid: str, request: Request):
    service_auth(request)
    return _settings_call(SETTINGS.enable_plugin, pid)


@app.post("/internal/settings/plugins/{pid}/disable")
def settings_plugin_disable(pid: str, request: Request):
    service_auth(request)
    return _settings_call(SETTINGS.disable_plugin, pid)


@app.get("/internal/settings/jobs")
def settings_jobs(request: Request):
    service_auth(request)
    return {"jobs": SETTINGS.list_jobs()}


@app.patch("/internal/settings/jobs/{job_id}")
async def settings_job_update(job_id: str, request: Request):
    service_auth(request)
    body = await request.json()
    current = SETTINGS.get_job(job_id)
    if current is None:
        raise HTTPException(404, "job not found")
    if "interval_minutes" in body:
        _settings_call(SETTINGS.set_job_interval, job_id, int(body["interval_minutes"]),
                       int(body.get("window_minutes") or body["interval_minutes"]))
    if "enabled" in body:
        _settings_call(SETTINGS.set_job_enabled, job_id, bool(body["enabled"]))
    return SETTINGS.get_job(job_id) or {"id": job_id}


@app.post("/internal/settings/jobs/{job_id}/run")
def settings_job_run(job_id: str, request: Request):
    service_auth(request)
    return SCHEDULER.run_by_id(job_id)


@app.get("/internal/settings/balance")
def settings_balance(request: Request):
    service_auth(request)
    return _settings_call(SETTINGS.api_balance)


@app.get("/internal/settings/logs")
def settings_logs(request: Request, after: int = 0, limit: int = 300):
    service_auth(request)
    return {"logs": LOG_RING.since(after, max(1, min(limit, 800)))}


@app.post("/internal/reminders/tick")
def reminder_tick(request: Request):
    service_auth(request)
    if REMINDERS is None:
        raise HTTPException(503, "reminders plugin unavailable")
    return {"fired": REMINDERS.tick()}


@app.post("/internal/echo")
async def internal_echo(request: Request):
    body = await request.json()
    return {"text": f"收到你的回复：{body.get('reply','')}（测试 Agent 回显）", "final": True}


@app.get("/login/{service}", response_class=HTMLResponse)
def login_page(service: str, token: str = ""):
    adapter = CREDENTIALS.get(service)
    if not adapter:
        raise HTTPException(404, "unknown service")
    if not verify_login_token(service, token):
        return HTMLResponse(adapter.login_html('<p class="err">链接无效或已过期，请在微信里重新获取。</p>'), status_code=403)
    return HTMLResponse(adapter.login_html())


@app.post("/login/{service}", response_class=HTMLResponse)
async def login_submit(request: Request, service: str, token: str = ""):
    adapter = CREDENTIALS.get(service)
    if not adapter:
        raise HTTPException(404, "unknown service")
    if not verify_login_token(service, token):
        return HTMLResponse(adapter.login_html('<p class="err">链接无效或已过期。</p>'), status_code=403)
    raw = (await request.body()).decode("utf-8", "replace")
    form = {key: (values[0] if values else "") for key, values in parse_qs(raw).items()}
    result = adapter.submit(form)
    if not result.get("ok"):
        return HTMLResponse(adapter.login_html(f'<p class="err">校验失败：{html.escape(result.get("error", "未知错误"))}</p>'))
    send_text(f"✅ {service} 登录已更新，账号：{result.get('username') or '未知'}")
    return HTMLResponse(adapter.login_html(f'<p class="ok">已保存并校验通过：{html.escape(result.get("username") or "")}</p>'))


@app.get("/agent-login/{service}", response_class=HTMLResponse)
def login_page_alias(service: str, token: str = ""):
    return login_page(service, token)


@app.post("/agent-login/{service}", response_class=HTMLResponse)
async def login_submit_alias(request: Request, service: str, token: str = ""):
    return await login_submit(request, service, token)
