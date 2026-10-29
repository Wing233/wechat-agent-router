"""Small registries so tools, agents and scheduled jobs are configuration, not code paths."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("registry")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Any]
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}

    def register(self, name: str, description: str, parameters: dict,
                 handler: Callable[[dict], Any], tags: tuple[str, ...] = (),
                 aliases: tuple[str, ...] = ()) -> None:
        # OpenAI-compatible function names only allow [A-Za-z0-9_-]; dots are rejected.
        for candidate in (name, *aliases):
            if not NAME_RE.match(candidate):
                raise ValueError(f"invalid tool name {candidate!r}: use [A-Za-z0-9_-] only")
        self._tools[name] = Tool(name, description, parameters, handler, tags, aliases)
        for alias in aliases:
            if alias != name:
                self._aliases[alias] = name

    def definitions(self) -> list[dict]:
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.parameters}}
            for t in self._tools.values()
        ]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def names_for_plugin(self, plugin_id: str) -> list[str]:
        return sorted(name for name, tool in self._tools.items() if plugin_id in tool.tags)

    def unregister_plugin(self, plugin_id: str) -> int:
        names = [name for name, tool in self._tools.items() if plugin_id in tool.tags]
        for name in names:
            self._tools.pop(name, None)
        self._aliases = {alias: target for alias, target in self._aliases.items()
                         if target in self._tools}
        return len(names)

    def execute(self, name: str, arguments: dict) -> Any:
        name = self._aliases.get(name, name)
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"unknown tool: {name}"}
        try:
            return tool.handler(arguments or {})
        except Exception as exc:  # tool failures never crash the chat loop
            log.warning("tool %s failed: %s", name, exc)
            return {"error": str(exc)[:500]}


@dataclass
class Job:
    id: str
    interval_minutes: int
    handler: Callable[[], Any]
    enabled: bool = True
    skip_empty: bool = True
    window_minutes: int = 30
    meta: dict = field(default_factory=dict)


class JobScheduler:
    """Interval jobs with persisted next-run timestamps; missed windows are skipped, not replayed."""

    def __init__(self, store, jobs: list[Job]):
        self.store = store
        self.jobs = {job.id: job for job in jobs if job.enabled}

    def _key(self, job: Job, suffix: str) -> str:
        return f"job:{job.id}:{suffix}"

    def next_run(self, job: Job, now: float | None = None) -> float:
        now = time.time() if now is None else now
        raw = self.store.state_get(self._key(job, "next"), "")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        interval = max(60, job.interval_minutes * 60)
        nxt = now + (interval - (now % interval))
        self.store.state_set(self._key(job, "next"), str(nxt))
        return nxt

    def due(self, now: float | None = None) -> list[Job]:
        now = time.time() if now is None else now
        result = []
        for job in self.jobs.values():
            if now >= self.next_run(job, now):
                result.append(job)
        return result

    def run(self, job: Job) -> dict:
        started = time.time()
        try:
            result = job.handler()
            status = "ok" if result is not None else "empty"
        except Exception as exc:
            log.exception("job %s failed", job.id)
            result = {"error": str(exc)[:500]}
            status = "error"
        self.store.state_set(self._key(job, "last_run"), str(int(started)))
        self.store.state_set(self._key(job, "last_status"), status)
        return {"id": job.id, "status": status, "result": result, "seconds": round(time.time() - started, 2)}

    def run_by_id(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            return {"error": f"unknown job: {job_id}"}
        return self.run(job)

    def tick(self) -> list[dict]:
        results = []
        now = time.time()
        for job in self.due(now):
            results.append(self.run(job))
            self.store.state_set(self._key(job, "next"), str(now + max(60, job.interval_minutes * 60)))
        return results
