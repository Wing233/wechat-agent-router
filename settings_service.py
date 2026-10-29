"""Kernel settings service: inspect and change schedules/plugins/agents at runtime."""
from __future__ import annotations

import json
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from timeutil import CST

AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
TRUE_VALUES = {"1", "true", "yes", "on"}


class SettingsService:
    def __init__(self, config: dict, store, config_path: str):
        self.config = config
        self.store = store
        self.config_path = Path(config_path)
        self.loader = None
        self.scheduler = None
        self.rebuild = None
        self.refresh = None
        self.tools = None
        self.reload_agents = None
        self.reload_llm = None
        self.reload_hub = None
        self.reload_public = None

    def attach(self, loader, scheduler, rebuild, refresh, tools=None,
               reload_agents=None, reload_llm=None, reload_hub=None,
               reload_public=None) -> None:
        self.loader = loader
        self.scheduler = scheduler
        self.rebuild = rebuild
        self.refresh = refresh
        self.tools = tools
        self.reload_agents = reload_agents
        self.reload_llm = reload_llm
        self.reload_hub = reload_hub
        self.reload_public = reload_public
        self._ensure_refs()

    def _ensure_refs(self) -> None:
        if self.loader is None:
            return
        self.loader.root_config["jobs"] = self.config.setdefault("jobs", {})
        self.loader.root_config["plugins"] = self.config.setdefault("plugins", {})
        disabled = self.config.setdefault("disabled_plugins", [])
        self.loader.root_config["disabled_plugins"] = disabled
        self.loader.disabled = set(disabled)

    def _save(self) -> None:
        text = json.dumps(self.config, ensure_ascii=False, indent=2)
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.config_path)

    def _plugin_for(self, job) -> str:
        return str((job.meta or {}).get("plugin") or "")

    def _next(self, job):
        try:
            stamp = self.scheduler.next_run(job)
            return datetime.fromtimestamp(stamp, timezone.utc).astimezone(CST).isoformat(timespec="seconds")
        except Exception:
            return None

    def list_jobs(self) -> list[dict]:
        items = []
        disabled = set(self.config.get("disabled_plugins") or [])
        loaded = self.scheduler.jobs if self.scheduler else {}
        for job_id, job in loaded.items():
            items.append({
                "id": job_id,
                "interval_minutes": job.interval_minutes,
                "window_minutes": job.window_minutes,
                "plugin": self._plugin_for(job),
                "next_run": self._next(job),
                "enabled": True,
            })
        for job_id, opts in (self.config.get("jobs") or {}).items():
            if job_id not in loaded:
                items.append({
                    "id": job_id,
                    "interval_minutes": opts.get("interval_minutes"),
                    "window_minutes": opts.get("window_minutes"),
                    "plugin": opts.get("plugin", ""),
                    "next_run": None,
                    "enabled": bool(opts.get("enabled", True)) and opts.get("plugin") not in disabled,
                })
        return items

    def get_job(self, job_id: str) -> dict | None:
        for item in self.list_jobs():
            if item["id"] == job_id:
                return item
        return None

    def set_job_interval(self, job_id: str, minutes: int, window_minutes: int | None = None) -> dict:
        minutes = int(minutes)
        if minutes < 1 or minutes > 10080:
            raise ValueError("间隔应在 1–10080 分钟之间")
        job = self.scheduler.jobs.get(job_id) if self.scheduler else None
        if job is None:
            raise ValueError(f"任务 {job_id} 当前未启用或不存在")
        plugin_id = self._plugin_for(job)
        if not plugin_id:
            raise ValueError("该任务没有可重载的插件归属")
        jobs_cfg = self.config.setdefault("jobs", {})
        old = json.loads(json.dumps(jobs_cfg.get(job_id, {})))
        options = jobs_cfg.setdefault(job_id, {})
        options["plugin"] = plugin_id
        options["enabled"] = True
        options["interval_minutes"] = minutes
        options["window_minutes"] = int(window_minutes or minutes)
        self._save()
        self._ensure_refs()
        self.store.state_set(f"job:{job_id}:next", "")
        try:
            self.loader.reload(plugin_id)
        except Exception:
            jobs_cfg[job_id] = old
            self._save()
            self._ensure_refs()
            raise
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "set_job_interval", f"{job_id} {minutes}")
        return self.get_job(job_id) or {"id": job_id}

    def set_job_enabled(self, job_id: str, enabled: bool) -> dict:
        job = self.scheduler.jobs.get(job_id) if self.scheduler else None
        if job is None and job_id not in (self.config.get("jobs") or {}):
            raise ValueError(f"任务 {job_id} 不存在")
        options = self.config.setdefault("jobs", {}).setdefault(job_id, {})
        plugin_id = self._plugin_for(job) if job else str(options.get("plugin") or "")
        if not plugin_id:
            raise ValueError("该任务没有可重载的插件归属")
        options["plugin"] = plugin_id
        options["enabled"] = bool(enabled)
        self._save()
        self._ensure_refs()
        self.store.state_set(f"job:{job_id}:next", "")
        self.loader.reload(plugin_id)
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "set_job_enabled", f"{job_id} {bool(enabled)}")
        return self.get_job(job_id) or {"id": job_id}

    # ------------------------------------------------------------------
    # plugins
    # ------------------------------------------------------------------
    def plugins(self) -> list[dict]:
        return self.loader.describe_all()

    def reload_plugin(self, plugin_id: str) -> list[dict]:
        if plugin_id not in self.loader.loaded:
            raise ValueError("插件已禁用，请先启用")
        self.loader.reload(plugin_id)
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "reload_plugin", plugin_id)
        return self.plugins()

    def enable_plugin(self, plugin_id: str) -> list[dict]:
        disabled = self.config.setdefault("disabled_plugins", [])
        was_disabled = plugin_id in disabled
        if was_disabled:
            disabled.remove(plugin_id)
        try:
            if plugin_id in self.loader.loaded:
                if not was_disabled:
                    raise ValueError("插件已经启用")
                self.loader.reload(plugin_id)
            else:
                self.loader.enable(plugin_id)
        except Exception:
            if was_disabled and plugin_id not in disabled:
                disabled.append(plugin_id)
                self.loader.disabled.add(plugin_id)
            raise
        self._save()
        self._ensure_refs()
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "enable_plugin", plugin_id)
        return self.plugins()

    def disable_plugin(self, plugin_id: str) -> list[dict]:
        if plugin_id not in self.loader.loaded:
            raise ValueError("插件未加载")
        disabled = self.config.setdefault("disabled_plugins", [])
        if plugin_id in disabled:
            raise ValueError("插件已经禁用")
        disabled.append(plugin_id)
        self._save()
        self._ensure_refs()
        self.loader.unload(plugin_id)
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "disable_plugin", plugin_id)
        return self.plugins()

    def tool_names(self) -> list[str]:
        return self.tools.names() if self.tools else []

    # ------------------------------------------------------------------
    # agents
    # ------------------------------------------------------------------
    def _mask(self, value) -> str:
        text = str(value or "")
        if not text:
            return ""
        return text[:6] + "..." + text[-4:] if len(text) > 12 else "***"

    def _masked_agent(self, agent: dict) -> dict:
        item = dict(agent)
        item["api_key"] = self._mask(item.get("api_key"))
        item["callback_secret"] = self._mask(item.get("callback_secret"))
        return item

    def agents_view(self) -> list[dict]:
        return [self._masked_agent(item) for item in (self.config.get("agents") or [])]

    def upsert_agent(self, payload: dict) -> dict:
        agent_id = str(payload.get("id") or "").strip()
        if not AGENT_ID_RE.match(agent_id):
            raise ValueError("Agent ID 只能用小写字母、数字、下划线或短横线，且以字母开头")
        agents = self.config.setdefault("agents", [])
        existing = next((item for item in agents if item.get("id") == agent_id), None)
        item = dict(existing or {})
        item["id"] = agent_id
        for field in ("name", "description"):
            if field in payload:
                item[field] = str(payload.get(field) or "").strip()[:500]
        item["name"] = item.get("name") or agent_id
        transport = str(payload.get("transport") or item.get("transport") or "poll").strip()
        if transport not in ("poll", "http"):
            raise ValueError("transport 只能是 poll 或 http")
        item["transport"] = transport
        if "callback_url" in payload:
            item["callback_url"] = str(payload.get("callback_url") or "").strip()[:500]
        if "callback_secret" in payload:
            secret = str(payload.get("callback_secret") or "").strip()
            if secret and "..." not in secret:
                item["callback_secret"] = secret
        if transport == "http" and not item.get("callback_url"):
            raise ValueError("http 模式必须填写 callback_url")
        raw_key = str(payload.get("api_key") or "").strip()
        created_key = ""
        if raw_key and "..." not in raw_key:
            item["api_key"] = raw_key
        elif not item.get("api_key"):
            created_key = "pc_" + secrets.token_hex(20)
            item["api_key"] = created_key
        if existing is None:
            agents.append(item)
        else:
            index = agents.index(existing)
            agents[index] = item
        self._save()
        reload_error = ""
        if self.reload_agents:
            try:
                self.reload_agents()
            except Exception as exc:
                reload_error = str(exc)[:300]
        self.store.log_plugin_event("admin", "upsert_agent", agent_id)
        return {"agent": self._masked_agent(item), "api_key_once": created_key or None,
                "reload_error": reload_error}

    def delete_agent(self, agent_id: str) -> dict:
        if agent_id == "echo":
            raise ValueError("内置 echo Agent 不可删除")
        agents = self.config.get("agents") or []
        item = next((row for row in agents if row.get("id") == agent_id), None)
        if not item:
            raise ValueError(f"Agent {agent_id} 不存在")
        self.config["agents"] = [row for row in agents if row.get("id") != agent_id]
        self._save()
        reload_error = ""
        if self.reload_agents:
            try:
                self.reload_agents()
            except Exception as exc:
                reload_error = str(exc)[:300]
        self.store.log_plugin_event("admin", "delete_agent", agent_id)
        return {"deleted": agent_id, "reload_error": reload_error}

    # ------------------------------------------------------------------
    # config view / write
    # ------------------------------------------------------------------
    def config_view(self, section: str = "all") -> dict:
        cfg = self.config
        llm = dict(cfg.get("llm") or {})
        llm["api_key"] = self._mask(llm.get("api_key"))
        llm["system_prompt"] = llm.get("system_prompt") or ""
        hub = dict(cfg.get("hub") or {})
        hub["channel_key"] = self._mask(hub.get("channel_key"))
        credentials = {key: self._mask(cfg.get(key)) for key in
                       ("credential_key", "service_token", "login_token_secret")}
        data = {
            "llm": llm,
            "hub": hub,
            "jobs": cfg.get("jobs") or {},
            "agents": self.agents_view(),
            "plugins": cfg.get("plugins") or {},
            "disabled_plugins": cfg.get("disabled_plugins") or [],
            "poll_interval": cfg.get("poll_interval"),
            "agent_wait_seconds": cfg.get("agent_wait_seconds", 60),
            "public_base_url": cfg.get("public_base_url"),
            "credentials": credentials,
        }
        return data if section == "all" else {section: data.get(section)}

    def _write_secret(self, key: str, value) -> bool:
        text = str(value or "").strip()
        if not text or "..." in text or text == "***":
            return False
        section, field = key.split(".", 1)
        self.config.setdefault(section, {})[field] = text
        return True

    def config_set(self, key: str, value) -> dict:
        key = str(key or "").strip()
        if key == "poll_interval":
            interval = int(value)
            if interval < 1 or interval > 60:
                raise ValueError("poll_interval 应在 1–60 秒之间")
            self.config["poll_interval"] = interval
            self._save()
            return {"key": key, "value": interval}
        if key == "agent_wait_seconds":
            seconds = int(value)
            if seconds < 5 or seconds > 600:
                raise ValueError("agent_wait_seconds 应在 5–600 秒之间")
            self.config["agent_wait_seconds"] = seconds
            self._save()
            return {"key": key, "value": seconds}
        if key in ("llm.base_url", "hub.base_url", "public_base_url"):
            text = str(value or "").strip().rstrip("/")
            if not re.match(r"^https?://[^\s]+$", text):
                raise ValueError("地址必须是 http:// 或 https:// 开头的 URL")
            if key == "public_base_url":
                self.config["public_base_url"] = text
                if self.reload_public:
                    self.reload_public()
            else:
                section, field = key.split(".", 1)
                self.config.setdefault(section, {})[field] = text
                if key == "llm.base_url" and self.reload_llm:
                    self.reload_llm()
                if key == "hub.base_url" and self.reload_hub:
                    self.reload_hub()
            self._save()
            return {"key": key, "value": text}
        if key == "llm.model":
            model = str(value or "").strip()
            if not model:
                raise ValueError("模型名不能为空")
            self.config.setdefault("llm", {})["model"] = model
            self._save()
            if self.reload_llm:
                self.reload_llm()
            return {"key": key, "value": model}
        if key == "llm.system_prompt":
            prompt = str(value or "")
            if len(prompt) > 20000:
                raise ValueError("system_prompt 过长（最多 20000 字符）")
            self.config.setdefault("llm", {})["system_prompt"] = prompt
            self._save()
            return {"key": key, "value": prompt}
        if key in ("llm.api_key", "hub.channel_key"):
            if not self._write_secret(key, value):
                return {"key": key, "changed": False}
            if self.loader is not None and key == "llm.api_key":
                self.loader.secrets["llm_api_key"] = self.config["llm"]["api_key"]
            self._save()
            if key == "llm.api_key" and self.reload_llm:
                self.reload_llm()
            if key == "hub.channel_key" and self.reload_hub:
                self.reload_hub()
            self.store.log_plugin_event("admin", "config_set", key)
            return {"key": key, "changed": True}
        parts = key.split(".")
        if len(parts) == 3 and parts[0] == "jobs":
            job_id, field = parts[1], parts[2]
            if field == "interval_minutes":
                return self.set_job_interval(job_id, int(value))
            if field == "window_minutes":
                current = self.get_job(job_id) or {}
                return self.set_job_interval(job_id, int(current.get("interval_minutes") or value), int(value))
            if field == "enabled":
                enabled = value if isinstance(value, bool) else str(value).lower() in TRUE_VALUES
                return self.set_job_enabled(job_id, enabled)
        raise ValueError("不允许修改这个配置项")

    def api_balance(self) -> dict:
        llm = self.config.get("llm") or {}
        base = str(llm.get("base_url") or "https://api.deepseek.com").rstrip("/")
        key = str(llm.get("api_key") or "")
        if not key:
            raise ValueError("未配置 llm.api_key")
        request = urllib.request.Request(base + "/user/balance",
                                         headers={"Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ValueError(f"HTTP {exc.code}: {detail}") from None
        balances = []
        for item in data.get("balance_infos") or []:
            balances.append({
                "currency": item.get("currency"),
                "total": item.get("total_balance"),
                "granted": item.get("granted_balance"),
                "topped_up": item.get("topped_up_balance"),
            })
        return {"is_available": data.get("is_available"), "balances": balances}
