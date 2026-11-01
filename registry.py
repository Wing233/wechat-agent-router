"""Small registries so tools, agents and scheduled jobs are configuration, not code paths."""
from __future__ import annotations

import copy
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

    def definitions(self, allowed: set[str] | None = None) -> list[dict]:
        try:
            import governance
        except Exception:  # pragma: no cover - governance is optional in tests
            governance = None
        items = []
        for tool in self._tools.values():
            if allowed is not None and tool.name not in allowed:
                continue
            if governance is not None and not governance.executor_tool_allowed(tool.name):
                continue
            parameters = tool.parameters
            if governance is not None and governance.is_high_risk_tool(tool.name):
                parameters = copy.deepcopy(parameters)
                properties = parameters.setdefault("properties", {})
                properties.setdefault("mandate", {
                    "type": "string",
                    "description": ("主人批准后得到的审批码。高危工具必须携带；"
                                    "先用 ask_human_confirmation 并传 tool/args 获取。"),
                })
            items.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": parameters,
                },
            })
        return items

    def names(self) -> list[str]:
        return sorted(self._tools)

    def canonical(self, name: str) -> str:
        """Resolve an alias to the registered tool name."""
        return self._aliases.get(name, name)

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
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        try:
            import governance
        except ImportError:  # pragma: no cover - governance is required in production
            return {"error": "执行策略模块不可用，已拒绝执行"}
        try:
            policy = governance.check_executor_policy(name)
            if policy is not None:
                return policy
            checked = governance.check_mandate(name, arguments)
        except Exception as exc:
            log.warning("tool %s policy check failed: %s", name, exc)
            return {"error": f"执行策略校验失败，已拒绝执行：{exc}"}
        if checked is not None:
            if checked.get("error"):
                return checked
            arguments = checked.get("args", arguments)
        try:
            return tool.handler(arguments)
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
