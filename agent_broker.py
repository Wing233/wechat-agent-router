"""Durable task broker for outbound-polling agents (NAT friendly).

The SQLite task table is the source of truth. The in-memory waiters only wake a
caller that is currently waiting for a result; they are never the queue.
"""
from __future__ import annotations

import threading
import time

from store import TASK_LEASE_SECONDS


class AgentBroker:
    def __init__(self, store=None):
        self._lock = threading.Lock()
        self._agents: dict[str, dict] = {}
        self._waiters: dict[str, threading.Event] = {}
        self.store = store

    def attach_store(self, store) -> None:
        self.store = store

    def register(self, agent_id: str, meta: dict) -> None:
        with self._lock:
            self._agents[agent_id] = {"meta": meta or {}, "last_seen": time.time()}

    def is_online(self, agent_id: str, max_age: int = 120) -> bool:
        with self._lock:
            info = self._agents.get(agent_id)
            return bool(info and time.time() - info["last_seen"] < max_age)

    def enqueue(self, agent_id: str, payload: dict, wait_seconds: int,
                max_attempts: int = 3) -> tuple[dict | None, str, str, dict]:
        if self.store is None:
            raise RuntimeError("agent task store not attached")
        task = self.store.create_agent_task(
            agent_id, payload, max_attempts=max_attempts)
        task_id = task["id"]
        event = threading.Event()
        with self._lock:
            self._waiters[task_id] = event
        try:
            if wait_seconds > 0:
                event.wait(max(0, int(wait_seconds)))
        finally:
            with self._lock:
                self._waiters.pop(task_id, None)
        task = self.store.get_agent_task(task_id) or task
        status = str(task.get("status") or "queued")
        result = task.get("result") if status == "completed" else None
        return result, task_id, status, task

    def take(self, agent_id: str) -> dict | None:
        if self.store is None:
            return None
        task = self.store.claim_agent_task(
            agent_id, lease_seconds=TASK_LEASE_SECONDS)
        if not task:
            return None
        with self._lock:
            event = self._waiters.get(task["id"])
            if event:
                event.set()
        payload = task.get("payload") or {}
        return {
            "task_id": task["id"],
            "status": task["status"],
            "attempt": task["attempts"],
            "lease_token": task["lease_token"],
            "lease_until": task["lease_until"],
            **payload,
        }

    def complete(self, task_id: str, ok: bool, text: str, *,
                 agent_id: str = "", lease_token: str = "") -> bool:
        if self.store is None:
            return False
        updated = self.store.complete_agent_task(
            task_id, ok, text, agent_id=agent_id, lease_token=lease_token)
        if not updated:
            return False
        with self._lock:
            event = self._waiters.get(task_id)
            if event:
                event.set()
        return True

    def claim_delivery(self, task_id: str) -> bool:
        return bool(self.store and self.store.mark_agent_task_delivered(task_id))

    def heartbeat(self, task_id: str, lease_token: str, *,
                  agent_id: str = "") -> bool:
        return bool(self.store and self.store.heartbeat_agent_task(
            task_id, lease_token, agent_id=agent_id))

    def requeue_expired(self) -> list[dict]:
        return self.store.requeue_expired_agent_tasks() if self.store else []

    def recover(self) -> list[dict]:
        return self.store.recover_agent_tasks() if self.store else []

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
        tasks = self.store.agent_task_counts() if self.store else {}
        pending = self.store.agent_task_pending_by_agent() if self.store else {}
        return {"agents": agents, "tasks": tasks, "pending": pending}


BROKER = AgentBroker()
