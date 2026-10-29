"""In-memory task broker for outbound-polling agents (NAT friendly)."""
from __future__ import annotations

import threading
import time
import uuid


class AgentBroker:
    def __init__(self):
        self._lock = threading.Lock()
        self._agents: dict[str, dict] = {}
        self._tasks: dict[str, dict] = {}
        self._queues: dict[str, list[str]] = {}

    def register(self, agent_id: str, meta: dict) -> None:
        with self._lock:
            self._agents[agent_id] = {"meta": meta or {}, "last_seen": time.time()}

    def take(self, agent_id: str) -> dict | None:
        with self._lock:
            while self._queues.get(agent_id):
                task_id = self._queues[agent_id].pop(0)
                task = self._tasks.get(task_id)
                if task and task["status"] == "pending":
                    task["status"] = "delivered"
                    return {"task_id": task_id, **task["payload"]}
        return None

    def enqueue(self, agent_id: str, payload: dict, wait_seconds: int) -> tuple[dict | None, str, bool]:
        task_id = uuid.uuid4().hex
        event = threading.Event()
        with self._lock:
            self._tasks[task_id] = {"agent_id": agent_id, "payload": payload,
                                    "status": "pending", "result": None, "event": event}
            self._queues.setdefault(agent_id, []).append(task_id)
        completed = event.wait(max(1, wait_seconds))
        with self._lock:
            task = self._tasks.pop(task_id, None)
            queue = self._queues.get(agent_id) or []
            if task_id in queue:
                queue.remove(task_id)
        return (task or {}).get("result"), task_id, completed

    def complete(self, task_id: str, ok: bool, text: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task["result"] = {"ok": bool(ok), "text": str(text)[:8000]}
            task["status"] = "done"
            task["event"].set()
            return True

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            agents = {
                agent_id: {
                    "online": now - info["last_seen"] < 120,
                    "last_seen": int(info["last_seen"]),
                    "meta": info["meta"],
                }
                for agent_id, info in self._agents.items()
            }
            pending = {agent_id: len(queue) for agent_id, queue in self._queues.items() if queue}
        return {"agents": agents, "pending": pending}


BROKER = AgentBroker()
