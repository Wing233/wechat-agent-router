"""Offline tests for model catalog, cost, traces, evals and insights."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evals
import insights
import models
import spend
import traces


def build_catalog(tmp_path: Path):
    config = {
        "llm": {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-primary-1234567890",
            "model": "deepseek-flash",
            "input_price_per_m": 2.0,
            "output_price_per_m": 8.0,
        }
    }
    saved = []
    catalog = models.ModelCatalog(config, save_fn=lambda: saved.append(1))
    return catalog, config, saved


def test_model_catalog_seed_and_bindings(tmp_path: Path) -> None:
    catalog, config, saved = build_catalog(tmp_path)
    data = catalog.list()
    assert [item["id"] for item in data["models"]] == ["deepseek-flash"]
    assert data["bindings"]["chat"] == ["deepseek-flash"]
    assert data["models"][0]["api_key"] != "sk-primary-1234567890"
    assert data["models"][0]["has_api_key"] is True
    assert saved  # seeding persisted once

    free = catalog.add({
        "id": "free-x",
        "name": "Free X",
        "base_url": "https://free.example.com/v1",
        "model": "free-x",
        "api_key": "sk-free-1234567890",
        "tags": "free,cheap",
    })
    assert free["tags"] == ["free", "cheap"]
    assert free["api_key"] != "sk-free-1234567890"

    bound = catalog.bind("job.default", "free-x,deepseek-flash")
    assert bound["models"] == ["free-x", "deepseek-flash"]
    providers = catalog.resolve("job.default")
    assert [item["id"] for item in providers] == ["free-x", "deepseek-flash"]

    # Unknown purposes fall back through family/default/chat.
    providers = catalog.resolve("plugin.websearch")
    assert [item["id"] for item in providers] == ["deepseek-flash"]

    direct = catalog.resolve("model:free-x")
    assert len(direct) == 1 and direct[0]["model"] == "free-x"

    catalog.bind("chat", ["free-x"])
    assert catalog.remove("deepseek-flash")["deleted"] == "deepseek-flash"
    assert catalog.list()["bindings"]["chat"] == ["free-x"]
    try:
        catalog.bind("chat", [])
    except ValueError as exc:
        assert "不能为空" in str(exc)
    else:
        raise AssertionError("empty chat binding must fail")


def test_spend_report_and_budget_status(tmp_path: Path) -> None:
    db = tmp_path / "spend.db"
    spend.init_db(db)
    spend.record_spend("owner", "deepseek-flash", 1_000_000, 500_000, 6.0, path=db)
    spend.record_spend("guest", "free-x", 100, 100, 0.0, path=db)
    report = spend.report(7, path=db)
    assert report["calls"] == 2
    assert report["total"] == 6.0
    assert report["by_model"][0]["model"] == "deepseek-flash"
    status = spend.budget_status({"owner": 10, "guest": 1}, path=db)
    owner = next(item for item in status if item["role"] == "owner")
    guest = next(item for item in status if item["role"] == "guest")
    assert owner["spent"] == 6.0 and owner["left"] == 4.0
    assert guest["unlimited"] is False and guest["spent"] == 0.0


def test_trace_store_roundtrip(tmp_path: Path) -> None:
    traces.configure(tmp_path / "traces.db")
    try:
        traces.start("abc123", source="qq", role="owner", session="c1",
                     user_id="u1", input="你好", meta={"chat_type": "private"})
        traces.span("abc123", "llm", "chat", model="deepseek-flash",
                    latency_ms=120, cost=0.01,
                    detail={"prompt_tokens": 10, "completion_tokens": 5})
        traces.span("abc123", "tool", "web_search", status="error",
                    detail={"error": "timeout"})
        traces.finish("abc123", status="ok", output="回复", latency_ms=400)

        item = traces.get("abc123")
        assert item and item["input"] == "你好"
        assert item["output"] == "回复"
        assert len(item["spans"]) == 2
        assert item["spans"][1]["detail"]["error"] == "timeout"
        assert traces.search("你好")[0]["trace_id"] == "abc123"
        assert traces.search("", status="ok")[0]["trace_id"] == "abc123"
        summary = traces.summary(24)
        assert summary["total"] == 1
        assert summary["tool_errors"][0]["name"] == "web_search"
        assert summary["models"][0]["model"] == "deepseek-flash"
    finally:
        traces.configure(None)


def test_eval_scoring_and_drift(tmp_path: Path) -> None:
    evals.configure(tmp_path / "evals.db", tmp_path / "cases.json")
    case = {"id": "case1", "prompt": "p", "checks": [
        {"type": "contains_any", "values": ["ok"]},
        {"type": "max_chars", "value": 10},
    ]}
    assert evals.score_case(case, "ok")["score"] == 100.0
    assert evals.score_case(case, "wrong")["passed"] is False
    assert evals.score_case(case, "ok " * 20)["score"] == 50.0

    def record(score: float, run_case: str = "case1") -> None:
        passed = score >= 99.9
        evals.record_run("test", "m", [{
            "case_id": run_case, "score": score, "passed": passed,
            "latency_ms": 10, "cost": 0.0, "output": "ok", "detail": {},
        }])

    record(100.0)
    record(100.0)
    record(40.0)
    record(30.0)
    report = evals.latest()
    assert report and report["score"] == 30.0
    drift = evals.drift(baseline_runs=2, recent_runs=2)
    assert drift["status"] == "drifting"
    assert drift["delta"] < 0
    assert evals.runs(10)


def test_insights_explain_and_suggest(tmp_path: Path) -> None:
    rate = insights.explain({
        "trace_id": "t1", "status": "error", "error": "HTTP 429 rate limit",
        "spans": [], "spend": [],
    })
    assert rate["verdict"] == "rate_limited"
    budget = insights.explain({
        "trace_id": "t2", "status": "error", "error": "budget_exceeded",
        "spans": [], "spend": [],
    })
    assert budget["verdict"] == "budget_exceeded"
    ok = insights.explain({
        "trace_id": "t3", "status": "ok", "error": "", "output": "hi",
        "latency_ms": 100, "spans": [], "spend": [],
    })
    assert ok["verdict"] == "ok"

    catalog = {"models": [
        {"id": "paid", "enabled": True, "tags": ["paid"]},
        {"id": "free", "enabled": True, "tags": ["free"]},
    ], "bindings": {"chat": ["paid"], "job.default": ["paid"]}}
    summary = {"hours": 24, "total": 10, "failed": 2, "success_rate": 80,
               "p95_latency_ms": 1000, "tool_errors": [], "recent_errors": [],
               "models": []}
    cost = {"days": 7, "total": 3.0, "today": 1.0, "by_model": [],
            "budgets": [{"role": "guest", "unlimited": True}]}
    report = insights.analyze(summary, cost, None, None, catalog)
    codes = {item["code"] for item in report["anomalies"]}
    assert "error_rate" in codes and "eval_missing" in codes
    ids = {item["id"] for item in report["suggestions"]}
    assert "free-job.default" in ids
    assert "chat-fallback" in ids
    assert "guest-budget" in ids


class FakeEvalLLM:
    def complete(self, messages, tools=None, max_tokens=None, **kwargs):
        sink = kwargs.get("usage_sink")
        if sink is not None:
            sink.update({"prompt_tokens": 10, "completion_tokens": 5,
                         "cost": 0.002})
        return {"content": "703"}


class FakeAuditStore:
    def log_plugin_event(self, *args, **kwargs):
        return None


class FakeSettings:
    def __init__(self):
        self.store = FakeAuditStore()
        self.config = {}


def test_eval_run_records_cost_and_score(tmp_path: Path) -> None:
    import capabilities

    catalog, _, _ = build_catalog(tmp_path)
    evals.configure(tmp_path / "evals.db", tmp_path / "cases.json")
    service = capabilities.CapabilityService(
        None, settings=FakeSettings(), catalog=catalog, llm=FakeEvalLLM(),
        spend_path="", reload_llm=lambda: None,
    )
    result = service._eval_run({
        "model_id": "deepseek-flash", "case_ids": "math", "max_cases": 1,
    })
    run = result["runs"][0]
    assert run["score"] == 100.0
    assert run["passed"] == 1
    detail = evals.report(run["run_id"])
    assert detail and detail["results"][0]["cost"] == 0.002
    assert detail["results"][0]["output"] == "703"
