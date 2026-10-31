"""OpenAI-compatible chat client.

Primary path is ``litellm.completion`` when the optional dependency is
available; any litellm failure falls back to a direct urllib call so the bot
keeps working on a 2C2G box without litellm. Token usage is written to the
local spend ledger on every successful call.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

import spend
import trace_ctx
import traces

log = logging.getLogger("llm")

_litellm = None
_litellm_ready = False
_litellm_lock = threading.Lock()


class LLMError(Exception):
    pass


class LLMBudgetError(LLMError):
    """Raised before a call when today's role budget is exhausted."""

    code = "budget_exceeded"

    def __init__(self, role: str, budget: float, spent: float):
        self.role = role
        self.budget = float(budget)
        self.spent = float(spent)
        label = "主人" if role == "owner" else "访客"
        self.user_message = (
            f"今天的 AI 预算已经用完（{label}上限 {self.budget:g}，已用 {self.spent:.2f}）。"
            f"明天会自动恢复，或让主人调整 llm.daily_budget_{role}。"
        )
        super().__init__(self.user_message)


def _get_litellm():
    """Import litellm lazily so a missing/broken install never blocks startup."""
    global _litellm, _litellm_ready
    if _litellm_ready:
        return _litellm
    with _litellm_lock:
        if not _litellm_ready:
            try:
                # Never fetch litellm's remote price map from a 2C2G box in
                # China: keep the bundled snapshot and avoid multi-second stalls.
                os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
                import litellm as module
                module.suppress_debug_info = True
                module.drop_params = True
                module.set_verbose = False
                module.telemetry = False
                _litellm = module
                try:
                    from importlib.metadata import version as _pkg_version
                    litellm_version = _pkg_version("litellm")
                except Exception:
                    litellm_version = "unknown"
                log.info("litellm gateway enabled: %s", litellm_version)
            except Exception as exc:
                _litellm = None
                log.warning("litellm unavailable, using urllib fallback: %s", exc)
            _litellm_ready = True
    return _litellm


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {}


def _to_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalize_message(data: dict) -> dict:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("empty response")
    raw = _as_dict(choices[0].get("message") if isinstance(choices[0], dict) else {})
    if not raw and not isinstance(choices[0], dict):
        raw = _as_dict(getattr(choices[0], "message", None))
    message: dict = {
        "role": raw.get("role") or "assistant",
        "content": raw.get("content"),
    }
    calls: list[dict] = []
    for item in raw.get("tool_calls") or []:
        entry = _as_dict(item)
        function = _as_dict(entry.get("function"))
        name = function.get("name") or ""
        if not name:
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        calls.append({
            "id": entry.get("id") or "",
            "type": entry.get("type") or "function",
            "function": {"name": name, "arguments": arguments or "{}"},
        })
    if calls:
        message["tool_calls"] = calls
    return message


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 90, *,
                 fallback_base_url: str = "", fallback_api_key: str = "",
                 fallback_model: str = "", input_price_per_m: float = 0.0,
                 output_price_per_m: float = 0.0, spend_db: str = "",
                 budgets: dict | None = None, default_role: str = "owner",
                 provider_resolver=None):
        self.base_url = ""
        self.endpoint = ""
        self.api_key = ""
        self.model = ""
        self.timeout = 90
        self.fallback_base_url = ""
        self.fallback_api_key = ""
        self.fallback_model = ""
        self.input_price_per_m = 0.0
        self.output_price_per_m = 0.0
        self.spend_db = ""
        self.budgets: dict[str, float] = {}
        self.default_role = "owner"
        self.provider_resolver = None
        self.configure(
            base_url=base_url, api_key=api_key, model=model, timeout=timeout,
            fallback_base_url=fallback_base_url, fallback_api_key=fallback_api_key,
            fallback_model=fallback_model, input_price_per_m=input_price_per_m,
            output_price_per_m=output_price_per_m, spend_db=spend_db,
            budgets=budgets, default_role=default_role,
            provider_resolver=provider_resolver,
        )

    def configure(self, *, base_url: str | None = None, api_key: str | None = None,
                  model: str | None = None, timeout: int | None = None,
                  fallback_base_url: str | None = None,
                  fallback_api_key: str | None = None,
                  fallback_model: str | None = None,
                  input_price_per_m: float | None = None,
                  output_price_per_m: float | None = None,
                  spend_db: str | None = None, budgets: dict | None = None,
                  default_role: str | None = None,
                  provider_resolver=None) -> None:
        """Update live config (called by reload_llm) without rebuilding the object."""
        if base_url is not None:
            self.base_url = str(base_url).rstrip("/")
            self.endpoint = self.base_url + "/chat/completions"
        if api_key is not None:
            self.api_key = str(api_key)
        if model is not None:
            self.model = str(model)
        if timeout is not None:
            self.timeout = max(1, int(timeout))
        if fallback_base_url is not None:
            self.fallback_base_url = str(fallback_base_url).rstrip("/")
        if fallback_api_key is not None:
            self.fallback_api_key = str(fallback_api_key)
        if fallback_model is not None:
            self.fallback_model = str(fallback_model)
        if input_price_per_m is not None:
            self.input_price_per_m = max(0.0, float(input_price_per_m))
        if output_price_per_m is not None:
            self.output_price_per_m = max(0.0, float(output_price_per_m))
        if spend_db is not None:
            self.spend_db = str(spend_db)
            if self.spend_db:
                try:
                    spend.configure(self.spend_db)
                except Exception as exc:
                    log.warning("spend ledger init failed (%s): %s", self.spend_db, exc)
        if budgets is not None:
            self.budgets = {str(key): float(value or 0) for key, value in budgets.items()}
        if default_role is not None:
            self.default_role = str(default_role or "owner")
        if provider_resolver is not None:
            self.provider_resolver = provider_resolver or None

    # --- budget ---------------------------------------------------------
    def budget_for(self, role: str) -> float:
        return float(self.budgets.get(role, self.budgets.get("default", 0)) or 0.0)

    def check_budget(self, role: str) -> None:
        budget = self.budget_for(role)
        if budget <= 0:
            return
        left = spend.budget_left(role, budget, path=self.spend_db or None)
        if left <= 0:
            spent = spend.today_total(role, path=self.spend_db or None)
            raise LLMBudgetError(role, budget, spent)

    # --- providers ------------------------------------------------------
    def _legacy_providers(self) -> list[dict]:
        items = [{
            "id": "primary", "label": "primary",
            "base_url": self.base_url, "api_key": self.api_key,
            "model": self.model,
            "input_price_per_m": self.input_price_per_m,
            "output_price_per_m": self.output_price_per_m,
            "tags": [],
        }]
        if self.fallback_model and self.fallback_base_url:
            items.append({
                "id": "fallback", "label": "fallback",
                "base_url": self.fallback_base_url,
                "api_key": self.fallback_api_key,
                "model": self.fallback_model,
                "input_price_per_m": self.input_price_per_m,
                "output_price_per_m": self.output_price_per_m,
                "tags": [],
            })
        return [item for item in items if item["base_url"] and item["model"]]

    def _providers(self, purpose: str = "chat") -> list[dict]:
        """Resolve a purpose to ordered providers, falling back to legacy config."""
        if callable(self.provider_resolver):
            try:
                resolved = self.provider_resolver(purpose)
            except Exception as exc:
                log.warning("model resolver failed for %s: %s", purpose, exc)
                resolved = []
            providers = []
            for item in resolved or []:
                if not isinstance(item, dict):
                    continue
                base_url = str(item.get("base_url") or "").rstrip("/")
                model = str(item.get("model") or "")
                if not base_url or not model:
                    continue
                providers.append({
                    "id": str(item.get("id") or model),
                    "label": str(item.get("label") or item.get("id") or model),
                    "base_url": base_url,
                    "api_key": str(item.get("api_key") or ""),
                    "model": model,
                    "input_price_per_m": float(item.get("input_price_per_m") or 0),
                    "output_price_per_m": float(item.get("output_price_per_m") or 0),
                    "tags": list(item.get("tags") or []),
                })
            if providers:
                return providers
            log.warning("no model bound for purpose=%s, using legacy llm block", purpose)
        return self._legacy_providers()

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 max_tokens: int | None = None, *, role: str | None = None,
                 trace_id: str | None = None, purpose: str = "chat",
                 usage_sink: dict | None = None) -> dict:
        role = str(role or trace_ctx.get_role() or self.default_role)
        trace_id = str(trace_id or trace_ctx.get_trace_id() or trace_ctx.TRACE_DEFAULT)
        self.check_budget(role)

        errors: list[str] = []
        for provider in self._providers(purpose):
            started = time.perf_counter()
            try:
                message = self._call_provider(provider, messages, tools, max_tokens,
                                              role, trace_id, purpose)
                usage = message.pop("_usage", {}) or {}
                if usage_sink is not None:
                    usage_sink.update(usage)
                traces.span(
                    trace_id, "llm", purpose, status="ok",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    model=str(provider.get("id") or provider.get("model")),
                    cost=float(usage.get("cost") or 0.0),
                    detail={"provider": provider.get("label"),
                            "model": provider.get("model"),
                            "prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0)},
                )
                return message
            except LLMBudgetError:
                raise
            except Exception as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                errors.append(f"{provider['label']}/{provider['model']}: {str(exc)[:400]}")
                traces.span(
                    trace_id, "llm", purpose, status="error", latency_ms=elapsed,
                    model=str(provider.get("id") or provider.get("model")),
                    detail={"provider": provider.get("label"),
                            "error": str(exc)[:400]},
                )
                log.warning("llm provider %s failed (trace=%s): %s", provider["label"],
                            trace_id, str(exc)[:400])
        raise LLMError("; ".join(errors) or "no llm provider configured")

    def _call_provider(self, provider: dict, messages: list[dict],
                       tools: list[dict] | None, max_tokens: int | None,
                       role: str, trace_id: str, purpose: str) -> dict:
        base_url = provider["base_url"]
        api_key = provider["api_key"]
        model = provider["model"]
        client = _get_litellm()
        if client is not None:
            try:
                response = self._litellm_call(client, base_url, api_key, model,
                                              messages, tools, max_tokens)
                data = _as_dict(response)
                message = _normalize_message(data)
                message["_usage"] = self._bill(role, provider, data, response, trace_id)
                return message
            except LLMBudgetError:
                raise
            except Exception as exc:
                log.warning("litellm call failed (trace=%s): %s", trace_id, str(exc)[:400])
        data = self._urllib_call(base_url, api_key, model, messages, tools, max_tokens)
        message = _normalize_message(data)
        message["_usage"] = self._bill(role, provider, data, None, trace_id)
        return message

    def _litellm_call(self, client, base_url: str, api_key: str, model: str,
                      messages: list[dict], tools: list[dict] | None,
                      max_tokens: int | None) -> object:
        # Force the OpenAI-compatible transport so arbitrary proxy model names
        # (including ones containing "/") never get routed to another provider.
        kwargs = {
            "model": model,
            "custom_llm_provider": "openai",
            "messages": messages,
            "api_base": base_url,
            "api_key": api_key,
            "timeout": self.timeout,
            "max_retries": 0,
            "temperature": 0.3,
        }
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            # litellm 1.106.6 imports its MCP proxy stack whenever tools are
            # present; that optional extra is not installed on the router and
            # is unrelated to plain OpenAI function calling. This documented
            # internal switch keeps the call on the normal chat path.
            kwargs["_skip_mcp_handler"] = True
        return client.completion(**kwargs)

    def _urllib_call(self, base_url: str, api_key: str, model: str,
                     messages: list[dict], tools: list[dict] | None,
                     max_tokens: int | None) -> dict:
        endpoint = base_url.rstrip("/") + "/chat/completions"
        payload: dict = {"model": model, "messages": messages, "temperature": 0.3}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer " + api_key)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {exc.code}: {text}") from None
        except urllib.error.URLError as exc:
            raise LLMError(f"network: {exc.reason}") from None
        except Exception as exc:
            raise LLMError(str(exc)) from None
        if data.get("error"):
            raise LLMError(str(data["error"])[:800])
        return data

    # --- accounting -----------------------------------------------------
    def _bill(self, role: str, provider: dict, data: dict, response,
              trace_id: str) -> dict:
        """Persist usage; any accounting failure is logged, never raised."""
        model = str(provider.get("id") or provider.get("model") or "")
        info = {"model": model, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        try:
            usage = _as_dict(data.get("usage"))
            prompt_tokens = _to_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
            completion_tokens = _to_int(
                usage.get("completion_tokens") or usage.get("output_tokens"))
            cost = 0.0
            if response is not None and (prompt_tokens or completion_tokens):
                cost = spend.completion_cost(response, model=provider.get("model") or model)
            if cost <= 0:
                cost = spend.estimate_cost(
                    prompt_tokens, completion_tokens,
                    float(provider.get("input_price_per_m") or 0),
                    float(provider.get("output_price_per_m") or 0))
            spend.record_spend(role, model, prompt_tokens, completion_tokens, cost,
                               trace_id, path=self.spend_db or None)
            info = {"model": model, "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens, "cost": cost}
            log.info("llm usage role=%s model=%s prompt=%s completion=%s cost=%.6f trace=%s",
                     role, model, prompt_tokens, completion_tokens, cost, trace_id)
        except Exception as exc:
            log.warning("spend accounting failed (trace=%s): %s", trace_id, exc)
        return info
