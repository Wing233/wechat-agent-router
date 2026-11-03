"""Reliability checks for the durable agent task broker and inbound journal."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_broker import AgentBroker  # noqa: E402
from store import Store  # noqa: E402


def test_task_survives_without_worker_and_is_claimed_once(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "router.db"))
    store.init()
    broker = AgentBroker(store)

    result, task_id, status, _task = broker.enqueue(
        "pc-home", {"message": "ping"}, 0)
    assert result is None and status == "queued"

    claimed = broker.take("pc-home")
    assert claimed and claimed["task_id"] == task_id
    assert broker.take("pc-home") is None  # 同一任务不会被第二个 worker 领走

    # 重启：claimed/running 的任务回到 queued，而不是消失
    broker2 = AgentBroker(store)
    failed = broker2.recover()
    assert not [item for item in failed if item["id"] == task_id]  # 还有重试次数，不算失败
    assert store.get_agent_task(task_id)["status"] == "queued"

    broker2.take("pc-home")
    assert broker2.complete(task_id, True, "PONG", agent_id="pc-home") is True
    assert store.get_agent_task(task_id)["status"] == "completed"
    assert broker2.claim_delivery(task_id) is True
    assert broker2.claim_delivery(task_id) is False  # 结果只投递一次


def test_lease_expiry_requeues_until_attempts_exhausted(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "router.db"))
    store.init()
    broker = AgentBroker(store)

    _, task_id, _, _ = broker.enqueue("pc-home", {"message": "ping"}, 0, max_attempts=2)
    broker.take("pc-home")
    with store._conn() as conn:
        conn.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' "
                     "WHERE id=?", (task_id,))

    broker.requeue_expired()
    assert store.get_agent_task(task_id)["status"] == "queued"

    broker.take("pc-home")
    with store._conn() as conn:
        conn.execute("UPDATE agent_tasks SET lease_until='2000-01-01T00:00:00+00:00' "
                     "WHERE id=?", (task_id,))
    broker.requeue_expired()
    assert store.get_agent_task(task_id)["status"] == "failed"


def test_inbound_journal_dedups_and_recovers(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "router.db"))
    store.init()

    first = store.claim_inbound("hub:1", source="hub", text="你好")
    assert first is not None
    assert store.claim_inbound("hub:1", source="hub", text="你好") is None  # 处理中不再重复

    store.finish_inbound(first["id"])
    assert store.claim_inbound("hub:1", source="hub", text="你好") is None  # 已完成不再重放

    second = store.claim_inbound("hub:2", source="hub", text="再试一次")
    store.fail_inbound(second["id"], "boom")
    assert store.claim_inbound("hub:2", source="hub", text="再试一次")["attempts"] == 2

    crash = store.claim_inbound("hub:3", source="hub", text="崩溃消息")
    recovered = store.recover_inbound()  # 崩溃遗留的 processing 回到 pending
    assert recovered >= 1
    again = store.claim_inbound("hub:3", source="hub", text="崩溃消息")
    assert again is not None and again["id"] == crash["id"]
