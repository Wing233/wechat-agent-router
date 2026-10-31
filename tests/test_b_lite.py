"""Offline checks for the B-lite gateway pieces (no network, no model calls).

Run directly:  python tests/test_b_lite.py
Or with pytest: pytest -q tests/test_b_lite.py
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm as llm_module
import spend
import trace_ctx


def assert_raises(exc_type, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def test_spend_ledger(tmp_path: Path) -> None:
    db = tmp_path / "spend.db"
    spend.init_db(db)
    spend.record_spend("guest", "m", 1_000_000, 1_000_000, 0.25,
                       trace_id="abc123", path=db)
    spend.record_spend("owner", "m", 0, 0, 1.5, path=db)

    assert spend.today_total("guest", path=db) == 0.25
    assert spend.today_total("owner", path=db) == 1.5
    assert spend.today_total(None, path=db) == 1.75
    assert spend.budget_left("guest", 1.0, path=db) == 0.75
    assert spend.budget_left("guest", 0, path=db) == math.inf
    assert spend.estimate_cost(1_000_000, 500_000, 2.0, 8.0) == 6.0
    assert spend.estimate_cost(123, 456, 0, 0) == 0.0

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT role,model,prompt_tokens,completion_tokens,cost,trace_id "
            "FROM spend ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows[0] == ("guest", "m", 1_000_000, 1_000_000, 0.25, "abc123")


def test_budget_guard(tmp_path: Path) -> None:
    db = tmp_path / "budget.db"
    spend.configure(db)
    spend.record_spend("guest", "m", 10, 10, 0.02, path=db)
    model = llm_module.LLM("http://127.0.0.1:9", "test-key", "deepseek-flash",
                           timeout=1, spend_db=str(db), budgets={"guest": 0.01})
    exc = assert_raises(llm_module.LLMBudgetError, model.complete,
                        [{"role": "user", "content": "hi"}], role="guest")
    assert exc.code == "budget_exceeded"
    assert "预算" in exc.user_message
    # Owner has no budget configured, so the guard must not fire.
    assert model.budget_for("owner") == 0


def test_trace_format() -> None:
    trace_id = trace_ctx.new_trace_id()
    assert re.fullmatch(r"[0-9a-f]{12}", trace_id)

    with trace_ctx.request(role="guest") as bound:
        assert bound == trace_ctx.get_trace_id()
        assert re.fullmatch(r"[0-9a-f]{12}", bound)
        assert trace_ctx.get_role() == "guest"

        record = logging.LogRecord("router", logging.INFO, __file__, 1, "hello", None, None)
        trace_ctx.TraceFilter().filter(record)
        assert record.trace_id == bound
        rendered = logging.Formatter("[%(trace_id)s] %(message)s").format(record)
        assert rendered == f"[{bound}] hello"

    assert trace_ctx.get_trace_id() == trace_ctx.TRACE_DEFAULT
    assert trace_ctx.get_role() == "owner"


def test_message_normalization() -> None:
    message = llm_module._normalize_message({"choices": [{"message": {
        "content": None,
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "websearch__web_search", "arguments": '{"q":"x"}'},
        }],
    }}]})
    assert message["content"] is None
    assert message["tool_calls"][0]["function"]["name"] == "websearch__web_search"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"q":"x"}'

    plain = llm_module._normalize_message({"choices": [{"message": {"content": "ok"}}]})
    assert plain["content"] == "ok"
    assert "tool_calls" not in plain
    assert_raises(llm_module.LLMError, llm_module._normalize_message, {"choices": []})


def test_litellm_payload_mapping(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self):
            self.kwargs = None

        def completion(self, **kwargs):
            self.kwargs = kwargs
            return {"choices": [{"message": {"content": "ok"}}]}

    model = llm_module.LLM("https://example.com", "test-key", "deepseek-flash",
                           timeout=7, spend_db=str(tmp_path / "payload.db"))
    client = FakeClient()
    result = model._litellm_call(client, "https://example.com", "test-key",
                                 "deepseek-flash", [{"role": "user", "content": "hi"}],
                                 None, None)
    assert result["choices"][0]["message"]["content"] == "ok"
    assert client.kwargs["model"] == "deepseek-flash"
    assert client.kwargs["custom_llm_provider"] == "openai"
    assert client.kwargs["api_base"] == "https://example.com"
    assert client.kwargs["timeout"] == 7

    client = FakeClient()
    model._litellm_call(client, "https://example.com", "test-key",
                        "deepseek-flash", [{"role": "user", "content": "hi"}],
                        [{"type": "function", "function": {"name": "t", "parameters": {}}}],
                        None)
    assert client.kwargs["_skip_mcp_handler"] is True
    assert client.kwargs["tool_choice"] == "auto"

    client = FakeClient()
    model._litellm_call(client, "https://example.com", "test-key",
                        "deepseek/deepseek-chat", [], None, None)
    assert client.kwargs["model"] == "deepseek/deepseek-chat"
    assert client.kwargs["custom_llm_provider"] == "openai"


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_spend_ledger(root)
        test_budget_guard(root)
        test_message_normalization()
        test_litellm_payload_mapping(root)
    test_trace_format()
    print("test_b_lite: 5 checks passed")
