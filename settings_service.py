"""Kernel settings service: inspect and change schedules/plugins at runtime."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from data import CST


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

    def attach(self, loader, scheduler, rebuild, refresh, tools=None) -> None:
        self.loader = loader
        self.scheduler = scheduler
        self.rebuild = rebuild
        self.refresh = refresh
        self.tools = tools
        self._ensure_jobs_ref()

    def _ensure_jobs_ref(self) -> None:
        if self.loader is not None:
            self.loader.root_config["jobs"] = self.config.setdefault("jobs", {})

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
                    "enabled": bool(opts.get("enabled", True)),
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
        self._ensure_jobs_ref()
        self.store.state_set(f"job:{job_id}:next", "")
        try:
            self.loader.reload(plugin_id)
        except Exception:
            jobs_cfg[job_id] = old
            self._save()
            self._ensure_jobs_ref()
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
        self._ensure_jobs_ref()
        self.store.state_set(f"job:{job_id}:next", "")
        self.loader.reload(plugin_id)
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "set_job_enabled", f"{job_id} {bool(enabled)}")
        return self.get_job(job_id) or {"id": job_id}

    def plugins(self) -> list[dict]:
        return self.loader.describe()

    def reload_plugin(self, plugin_id: str) -> list[dict]:
        self.loader.reload(plugin_id)
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "reload_plugin", plugin_id)
        return self.plugins()

    def enable_plugin(self, plugin_id: str) -> list[dict]:
        if not self.loader.enable(plugin_id):
            raise ValueError("插件已经加载")
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "enable_plugin", plugin_id)
        return self.plugins()

    def disable_plugin(self, plugin_id: str) -> list[dict]:
        if not self.loader.unload(plugin_id):
            raise ValueError("插件未加载")
        self.rebuild()
        self.refresh()
        self.store.log_plugin_event("admin", "disable_plugin", plugin_id)
        return self.plugins()

    def tool_names(self) -> list[str]:
        return self.tools.names() if self.tools else []
