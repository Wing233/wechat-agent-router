"""Offline checks for durable inbound messages and agent tasks."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_broker import AgentBroker
from store import INBOUND_MAX_ATTEMPTS, Store


def make_store(tmp_path: Path) -> Store:
    store = Store(str(tmp_path / "router.db"))
    store.init()
    return store


def test_inbound_dedup_and_done(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    row = store.claim_inbound("hub:1", source="hub", message_id="1",
                              chat_id="c", user_id="u", text="hello")
    assert row is not None
    assert row["status"] == "processing"
    assert store.claim_inbound("hub:1", text="hello") is None

    store.finish_inbound(row["id"])
    assert store.claim_inbound("hub:1", text="hello") is None
    assert store.list_inbound()[0]["status"] == "done"


def test_inbound_failure_retries_then_dead(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    row = store.claim_inbound("hub:2", text="boom")
    for attempt in range(1, INBOUND_MAX_ATTEMPTS + 1):
        assert row is not None
        assert store.fail_inbound(row["id"], "boom") == attempt
        row = store.claim_inbound("hub:2", text="boom")
    assert row is None
    assert store.list_inbound(status="dead")


def test_inbound_recovery_requeues_processing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    row = store.claim_inbound("hub:3", text="retry me")
    assert row is not None
    assert store.recover_inbound() == 1
    again = store.claim_inbound("hub:3", text="retry me")
    assert again is not None
    assert again["attempts"] == 2


def test_agent_task_lifecycle_and_single_delivery(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"})
    assert task["status"] == "queued"

    claimed = store.claim_agent_task("pc")
    assert claimed is not None
    assert claimed["status"] == "claimed"
    assert claimed["attempts"] == 1
    assert claimed["lease_token"]
    assert store.claim_agent_task("pc") is None

    assert store.complete_agent_task(
        claimed["id"], True, "done", agent_id="pc",
        lease_token=claimed["lease_token"]) is True
    done = store.get_agent_task(claimed["id"])
    assert done["status"] == "completed"
    assert done["result"]["text"] == "done"

    assert store.mark_agent_task_delivered(claimed["id"]) is True
    assert store.mark_agent_task_delivered(claimed["id"]) is False
    # Idempotent completion must not overwrite the first result.
    assert store.complete_agent_task(claimed["id"], True, "again", agent_id="pc") is True
    assert store.get_agent_task(claimed["id"])["result"]["text"] == "done"


def test_agent_task_rejects_wrong_agent_and_lease(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"})
    claimed = store.claim_agent_task("pc")
    assert claimed is not None
    assert store.complete_agent_task(
        claimed["id"], True, "bad", agent_id="other") is False
    assert store.complete_agent_task(
        claimed["id"], True, "bad", agent_id="pc",
        lease_token="wrong") is False
    assert store.get_agent_task(claimed["id"])["status"] == "claimed"


def test_agent_task_lease_requeues_then_fails(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"}, max_attempts=2)
    first = store.claim_agent_task("pc")
    assert first is not None
    with store._conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' "
            "WHERE id=?", (first["id"],))
    assert store.requeue_expired_agent_tasks() == []
    assert store.get_agent_task(first["id"])["status"] == "queued"

    second = store.claim_agent_task("pc")
    assert second is not None
    with store._conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' "
            "WHERE id=?", (second["id"],))
    failed = store.requeue_expired_agent_tasks()
    assert failed
    assert failed[0]["id"] == task["id"]
    assert failed[0]["status"] == "failed"


def test_agent_task_heartbeat_and_recovery(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"})
    claimed = store.claim_agent_task("pc")
    assert claimed is not None
    assert store.heartbeat_agent_task(
        claimed["id"], claimed["lease_token"], agent_id="pc") is True
    assert store.get_agent_task(claimed["id"])["status"] == "running"

    assert store.recover_agent_tasks() == []
    assert store.get_agent_task(claimed["id"])["status"] == "queued"


def test_broker_uses_durable_tasks_and_wakes_on_claim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    broker = AgentBroker(store)
    assert broker.is_online("pc") is False
    broker.register("pc", {"name": "PC"})
    assert broker.is_online("pc") is True

    results: dict = {}

    def wait_for_result() -> None:
        results["value"] = broker.enqueue("pc", {"message": "hello"}, 5)

    thread = threading.Thread(target=wait_for_result)
    thread.start()
    for _ in range(100):
        queued = store.list_agent_tasks(status="queued")
        if queued:
            break
        time.sleep(0.01)
    taken = broker.take("pc")
    thread.join(2)
    assert taken is not None
    assert taken["task_id"] == results["value"][1]
    assert results["value"][2] in {"claimed", "completed"}

    assert broker.complete(
        taken["task_id"], True, "done", agent_id="pc",
        lease_token=taken["lease_token"]) is True
    assert broker.claim_delivery(taken["task_id"]) is True
    assert broker.claim_delivery(taken["task_id"]) is False
    assert broker.status()["tasks"]["completed"] == 1


def test_stale_or_missing_lease_cannot_finish_requeued_task(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"}, max_attempts=3)
    first = store.claim_agent_task("pc")
    assert first is not None
    # Simulate the lease expiring and the task being reclaimed elsewhere.
    with store._conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET status='queued',lease_token='',"
            "lease_until=NULL WHERE id=?", (task["id"],))
    second = store.claim_agent_task("pc")
    assert second is not None
    assert second["lease_token"] != first["lease_token"]

    assert store.complete_agent_task(
        task["id"], True, "old worker", agent_id="pc",
        lease_token=first["lease_token"]) is False
    assert store.complete_agent_task(
        task["id"], True, "no lease", agent_id="pc") is False
    assert store.get_agent_task(task["id"])["status"] == "claimed"
    assert store.complete_agent_task(
        task["id"], True, "new worker", agent_id="pc",
        lease_token=second["lease_token"]) is True
    assert store.get_agent_task(task["id"])["result"]["text"] == "new worker"


def test_completion_writes_one_durable_delivery(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"})
    claimed = store.claim_agent_task("pc")
    assert claimed is not None
    assert store.complete_agent_task(
        task["id"], True, "done", agent_id="pc",
        lease_token=claimed["lease_token"],
        channel="wechat", chat_id="chat-1") is True

    row = store.pending_agent_delivery(task["id"])
    assert row is not None
    assert row["status"] == "pending"
    assert row["channel"] == "wechat"
    assert row["chat_id"] == "chat-1"

    first = store.claim_agent_deliveries()
    assert len(first) == 1 and first[0]["text"] == "done"
    # A second sender must not get the same row while the first lease is live.
    assert store.claim_agent_deliveries() == []

    # Crash before sending: startup recovery makes the row claimable again.
    assert store.recover_agent_deliveries() == 1
    again = store.claim_agent_deliveries()
    assert len(again) == 1
    assert store.finish_agent_delivery(again[0]["dedupe_key"], True) is True
    assert store.pending_agent_delivery(task["id"])["status"] == "sent"

    # A duplicate completion never creates a second delivery.
    assert store.complete_agent_task(
        task["id"], True, "done", agent_id="pc", lease_token="whatever") is True
    assert store.claim_agent_deliveries() == []


def test_inline_claim_excludes_background_sender(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"})
    claimed = store.claim_agent_task("pc")
    assert claimed is not None
    seed = store.claim_agent_deliveries()
    assert seed == []  # nothing queued before completion
    assert store.complete_agent_task(
        task["id"], True, "inline", agent_id="pc",
        lease_token=claimed["lease_token"]) is True

    assert store.claim_agent_delivery_inline(task["id"]) is True
    assert store.claim_agent_deliveries() == []
    assert store.pending_agent_delivery(task["id"])["status"] == "inline"


def test_failed_delivery_retries_with_backoff(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = store.create_agent_task("pc", {"message": "hello"})
    claimed = store.claim_agent_task("pc")
    assert claimed is not None
    store.complete_agent_task(
        task["id"], True, "retry me", agent_id="pc",
        lease_token=claimed["lease_token"])
    batch = store.claim_agent_deliveries()
    assert len(batch) == 1
    assert store.finish_agent_delivery(
        batch[0]["dedupe_key"], False, error="hub down",
        retry_seconds=60) is True
    row = store.pending_agent_delivery(task["id"])
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["error"] == "hub down"
    assert store.claim_agent_deliveries() == []  # next_attempt_at is in the future
