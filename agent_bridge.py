"""Narrow HTTP bridge that lets the lyph site agent call four read-only tools.

The bridge is deliberately independent from the chat tool loop and from the
admin surface: only the names in ``ALLOWED_TOOLS`` can be executed, callers
authenticate with a registered agent's ``api_key`` and every call is audited.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable

from fastapi import FastAPI, HTTPException, Request

import service_tokens

log = logging.getLogger("agent_bridge")

# The only names the lyph agent may call, mapped to router-internal tool names.
# This mapping is hard-coded on purpose: configuration must never widen it.
ALLOWED_TOOLS = {
    "web_search": "websearch__web_search",
    "web_fetch": "websearch__web_fetch",
    "weather": "websearch__weather",
    "calculator": "websearch__calculator",
    # lyph public-domain tools, implemented by plugins/lyph_tools.
    "get_public_profile": "lyph_tools__get_public_profile",
    "search_public_content": "lyph_tools__search_public_content",
    "get_public_content": "lyph_tools__get_public_content",
    "search_companies": "lyph_tools__search_companies",
    "get_company": "lyph_tools__get_company",
    "aggregate_companies": "lyph_tools__aggregate_companies",
}

# Explicit denials keep the intent auditable even though the allow-list above
# is already authoritative.
FORBIDDEN_TOOL_NAMES = {
    "send_to_agent",
    "run_command",
    "http_request",
    "propose_plugin",
    "generate_image",
    "send_media",
    "fs_list",
    "fs_read",
    "fs_write",
    "fs_replace",
    "fs_delete",
    "fs_move",
}
FORBIDDEN_TOOL_PREFIXES = (
    "ops__",
    "devtools__",
    "admin__",
    "reminders__",
    "todo__",
    "site__",
    "leetcode__",
    "evolution__",
    "agents__",
)

DEFAULT_AGENT_IDS = ("lyph",)
RATE_LIMIT_PER_MINUTE = 60
RATE_WINDOW_SECONDS = 60.0
MAX_BODY_BYTES = 32 * 1024
MAX_RESULT_BYTES = 256 * 1024
AUDIT_DETAIL_LIMIT = 1000


class RateLimiter:
    """Sliding-window limiter keyed by agent id."""

    def __init__(self, limit: int = RATE_LIMIT_PER_MINUTE,
                 window: float = RATE_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> int:
        """Record one call; return 0 when allowed or retry-after seconds."""
        now = self.clock()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return max(1, int(round(self.window - (now - events[0]))))
            events.append(now)
            return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_key(request: Request) -> str:
    key = request.headers.get("x-agent-key", "")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key.strip()


def _agent_key(agent: dict) -> str:
    """Bridge-only key, falling back to the legacy agent key.

    ``bridge_key`` keeps the tool bridge separate from the broker routes
    (``/agent/tasks`` etc.) so a leaked tool key cannot poll or complete tasks.
    """
    return str(agent.get("bridge_key") or agent.get("api_key") or "").strip()


def _audit_text(detail: dict) -> str:
    try:
        encoded = json.dumps(detail, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = "{}"
    return encoded[:AUDIT_DETAIL_LIMIT]


def _store_audit(store) -> Callable[[str, dict], None]:
    def audit(action: str, detail: dict) -> None:
        store.log_plugin_event("agent_bridge", action, _audit_text(detail))
    return audit


def _host_module():
    """Resolve the running app module without importing it a second time."""
    for name in ("app", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "TOOLS") and hasattr(module, "CFG"):
            return module
    raise RuntimeError(
        "agent_bridge.register(app) cannot resolve dependencies; "
        "pass config=, tools= and audit= explicitly"
    )


class AgentBridge:
    def __init__(self, *, tools, agents,
                 allowed_agents: set[str] | None = None,
                 audit: Callable[[str, dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 rate_limit: int = RATE_LIMIT_PER_MINUTE,
                 notify_token: str = "") -> None:
        self.tools = tools
        self._agents = agents
        self.allowed_agents = allowed_agents
        self.audit = audit
        self.clock = clock
        self.limiter = RateLimiter(limit=rate_limit, clock=clock)
        self.notify_token = notify_token

    def available_agents(self) -> dict[str, dict]:
        """Read the live mapping so key rotation/removal applies without restart."""
        source = self._agents() if callable(self._agents) else self._agents
        return {
            agent_id: agent for agent_id, agent in (source or {}).items()
            if isinstance(agent, dict)
            and (self.allowed_agents is None or agent_id in self.allowed_agents)
            and _agent_key(agent)
        }

    # --- audit ---------------------------------------------------------
    def _audit(self, action: str, detail: dict) -> None:
        if self.audit is None:
            return
        try:
            self.audit(action, detail)
        except Exception:
            # Auditing must never turn a read-only tool failure into a 500.
            log.warning("agent bridge audit failed", exc_info=True)

    # --- steps ---------------------------------------------------------
    def _authenticate(self, request: Request) -> dict:
        key = _extract_key(request)
        matched = None
        if key:
            for agent in self.available_agents().values():
                candidate = _agent_key(agent)
                if candidate and hmac.compare_digest(candidate, key):
                    matched = agent
        if matched is None:
            self._audit("auth_denied", {
                "ip": request.client.host if request.client else "",
            })
            raise HTTPException(401, "invalid agent key")
        return matched

    @staticmethod
    def _target(tool: str) -> str:
        if tool in FORBIDDEN_TOOL_NAMES or tool.startswith(FORBIDDEN_TOOL_PREFIXES):
            raise HTTPException(403, "tool is not available through the bridge")
        target = ALLOWED_TOOLS.get(tool)
        if target is None:
            raise HTTPException(403, "tool is not available through the bridge")
        return target

    @staticmethod
    async def _arguments(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "request body too large")
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "body must be a JSON object") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "body must be a JSON object")
        return payload

    async def handle(self, tool: str, request: Request) -> dict:
        agent = self._authenticate(request)
        target = self._target(tool)
        retry_after = self.limiter.hit(str(agent["id"]))
        if retry_after:
            self._audit("rate_limited", {"agent": agent["id"], "tool": tool,
                                         "retry_after": retry_after})
            raise HTTPException(429, "rate limit exceeded",
                                headers={"Retry-After": str(retry_after)})

        arguments = await self._arguments(request)
        started = self.clock()
        try:
            result = await asyncio.to_thread(self.tools.execute, target, arguments)
        except Exception as exc:  # defensive: ToolRegistry normally returns errors
            log.exception("agent bridge tool %s crashed", target)
            result = {"error": f"工具执行失败：{exc}"}

        elapsed_ms = max(0, int(round((self.clock() - started) * 1000)))
        audit_detail = {
            "agent": agent["id"],
            "tool": tool,
            "arguments": _audit_text(arguments),
            "elapsed_ms": elapsed_ms,
        }
        if isinstance(result, dict) and result.get("error"):
            # Handlers may return a coded error; anything else stays a generic tool_error.
            raw_error = result.get("error")
            if isinstance(raw_error, dict):
                code = str(raw_error.get("code") or "tool_error")[:40]
                message = str(raw_error.get("message") or "")[:300]
            else:
                code, message = "tool_error", str(raw_error)[:300]
            payload = {
                "ok": False,
                "agent": agent["id"],
                "tool": tool,
                "error": {"code": code, "message": message},
                "elapsed_ms": elapsed_ms,
            }
            audit_detail.update(ok=False, error=code)
        else:
            payload = {
                "ok": True,
                "agent": agent["id"],
                "tool": tool,
                "result": result,
                "elapsed_ms": elapsed_ms,
            }
            audit_detail.update(ok=True)

        # Bounded guarantee: a handler can never stream unbounded data back.
        if len(json.dumps(payload, ensure_ascii=False, default=str)) > MAX_RESULT_BYTES:
            payload = {
                "ok": False,
                "agent": agent["id"],
                "tool": tool,
                "error": {"code": "result_too_large", "message": "结果过大，请缩小查询范围。"},
                "elapsed_ms": elapsed_ms,
            }
            audit_detail.update(ok=False, error="result_too_large")

        self._audit("tool_call", audit_detail)
        log.info("agent bridge tool=%s agent=%s ok=%s elapsed_ms=%s",
                 tool, agent["id"], audit_detail.get("ok"), elapsed_ms)
        return payload

    async def notify(self, request: Request, notify: Callable[[str], bool]) -> dict:
        try:
            agent = self._authenticate(request)
        except HTTPException:
            # Integrations that only push messages should not need a tool key.
            token = service_tokens.from_request(request)
            if not service_tokens.match(token, self.notify_token):
                raise
            agent = {"id": "service", "name": "系统通知"}
        payload = await self._arguments(request)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        text = text[:2000]
        retry_after = self.limiter.hit(str(agent["id"]))
        if retry_after:
            raise HTTPException(429, "rate limit exceeded",
                                headers={"Retry-After": str(retry_after)})
        result = notify(text)
        if isinstance(result, str):
            status = result if result in {"sent", "queued", "failed"} else (
                "sent" if result else "failed")
        else:
            status = "sent" if result else "failed"
        self._audit("notify", {"agent": agent["id"], "status": status,
                               "chars": len(text)})
        return {"status": status, "queued": status == "queued"}

    def health(self) -> dict:
        return {
            "ok": True,
            "service": "agent-bridge",
            "time": _utc_now(),
            "agents": sorted(self.available_agents()),
            "tools": sorted(ALLOWED_TOOLS),
            "rate_limit_per_minute": self.limiter.limit,
        }


def register(app: FastAPI, *, config: dict | None = None, tools=None,
             audit: Callable[[str, dict], None] | None = None,
             allowed_agents: list[str] | None = None,
             notify: Callable[[str], bool] | None = None,
             notify_token: str = "",
             clock: Callable[[], float] = time.monotonic) -> AgentBridge:
    """Attach ``POST /agent/tools/{tool}`` and ``GET /agent/health``.

    One-line integration in ``app.py``::

        agent_bridge.register(app)

    Dependencies default to the running module's ``CFG``/``TOOLS``/``store``
    so app.py needs no extra wiring; tests may inject all three.
    """
    existing = getattr(app.state, "agent_bridge", None)
    if existing is not None:
        return existing

    host = None
    if config is None or tools is None:
        host = _host_module()
    if config is None:
        config = host.CFG
    if tools is None:
        tools = host.TOOLS
    if audit is None and host is not None:
        audit = _store_audit(host.store)
    if notify is None and host is not None:
        notify = getattr(host, "send_text", None)
    if notify is None:
        notify = lambda _text: False

    ids = allowed_agents
    if ids is None:
        bridge_config = config.get("agent_bridge")
        ids = bridge_config.get("allowed_agents") if isinstance(bridge_config, dict) else None
    if not ids:
        ids = list(DEFAULT_AGENT_IDS)
    if isinstance(ids, str):
        ids = [ids]
    allowed = {str(item).strip() for item in ids if str(item).strip()}

    if host is not None and isinstance(getattr(host, "AGENTS", None), dict):
        # Live mapping: admin key rotation/removal applies without a restart.
        source = host.AGENTS
    else:
        source = {str(item.get("id")): item for item in config.get("agents") or []
                  if isinstance(item, dict) and item.get("id")}

    bridge = AgentBridge(tools=tools, agents=source, allowed_agents=allowed,
                         audit=audit, clock=clock, notify_token=notify_token)
    app.state.agent_bridge = bridge

    @app.post("/agent/tools/{tool}")
    async def agent_tool(tool: str, request: Request):
        return await bridge.handle(tool, request)

    @app.post("/agent/notify")
    async def agent_notify(request: Request):
        return await bridge.notify(request, notify)

    @app.get("/agent/health")
    def agent_health():
        return bridge.health()

    available = bridge.available_agents()
    if not available:
        log.warning("agent bridge registered without a usable agent key (allowed=%s)", sorted(allowed))
    else:
        log.info("agent bridge registered (agents=%s tools=%s)",
                 sorted(available), sorted(ALLOWED_TOOLS))
    return bridge
