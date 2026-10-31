"""Contract tests for the narrowly scoped lyph -> router tool bridge."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_bridge

KEY = "test-agent-key"
CONFIG = {"agents": [{"id": "lyph", "api_key": KEY},
                     {"id": "other", "api_key": "other-key"}]}


class Tools:
    def __init__(self):
        self.calls = []
        self.reply = {"provider": "bing", "results": [{"title": "t", "url": "https://example.com"}]}

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return self.reply


def build(tools=None, config=None, rate_limit=60, clock=None):
    app = FastAPI()
    fake = tools or Tools()
    audits = []
    bridge = agent_bridge.register(
        app, config=config or CONFIG, tools=fake,
        audit=lambda action, detail: audits.append((action, detail)),
        clock=clock or (lambda: 1000.0),
    )
    # allow tests to lower the limit without sleeping
    bridge.limiter.limit = rate_limit
    return TestClient(app), fake, audits, bridge


def test_health_is_open_and_limited_to_whitelisted_tools():
    client, _, _, _ = build()
    response = client.get("/agent/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["agents"] == ["lyph"]
    assert body["tools"] == ["calculator", "weather", "web_fetch", "web_search"]
    assert body["rate_limit_per_minute"] == 60


def test_requires_agent_key_and_accepts_bearer():
    client, tools, audits, _ = build()
    assert client.post("/agent/tools/web_search", json={"query": "x"}).status_code == 401
    assert tools.calls == []
    assert client.post("/agent/tools/web_search", json={"query": "x"},
                       headers={"X-Agent-Key": "wrong"}).status_code == 401
    response = client.post("/agent/tools/web_search", json={"query": "x"},
                           headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 200
    assert tools.calls == [("websearch__web_search", {"query": "x"})]
    assert any(action == "auth_denied" for action, _ in audits)


def test_other_agent_key_does_not_pass_the_lyph_bridge():
    client, tools, _, _ = build()
    response = client.post("/agent/tools/weather", json={"city": "北京"},
                           headers={"X-Agent-Key": "other-key"})
    assert response.status_code == 401
    assert tools.calls == []


def test_bridge_key_is_separate_from_the_broker_key():
    config = {"agents": [{"id": "lyph", "api_key": "broker-key", "bridge_key": KEY}]}
    client, tools, _, _ = build(config=config)
    denied = client.post("/agent/tools/weather", json={"city": "北京"},
                         headers={"X-Agent-Key": "broker-key"})
    assert denied.status_code == 401
    assert tools.calls == []
    allowed = client.post("/agent/tools/weather", json={"city": "北京"},
                          headers={"X-Agent-Key": KEY})
    assert allowed.status_code == 200


def test_whitelist_is_hard_coded_and_admin_tools_are_rejected():
    client, tools, _, _ = build()
    for tool in ["run_command", "fs_read", "send_to_agent", "admin__list_jobs",
                 "ops__run_command", "devtools__http_request", "websearch__web_search"]:
        response = client.post(f"/agent/tools/{tool}", json={},
                               headers={"X-Agent-Key": KEY})
        assert response.status_code == 403, tool
    assert tools.calls == []


def test_request_body_must_be_a_json_object():
    client, tools, _, _ = build()
    for body in [b"[1,2]", b"null", b"not-json"]:
        response = client.post("/agent/tools/calculator", content=body,
                               headers={"X-Agent-Key": KEY,
                                        "Content-Type": "application/json"})
        assert response.status_code == 400
    assert tools.calls == []


def test_success_and_tool_error_envelopes():
    client, tools, audits, _ = build()
    ok = client.post("/agent/tools/calculator", json={"expression": "1+1"},
                     headers={"X-Agent-Key": KEY})
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True and body["tool"] == "calculator"
    assert body["result"]["provider"] == "bing"
    assert isinstance(body["elapsed_ms"], int)
    assert audits and audits[-1][0] == "tool_call" and audits[-1][1]["ok"] is True

    tools.reply = {"error": "搜索失败：上游不可用"}
    failed = client.post("/agent/tools/web_search", json={"query": "x"},
                         headers={"X-Agent-Key": KEY})
    assert failed.status_code == 200
    body = failed.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "tool_error"
    assert "上游不可用" in body["error"]["message"]
    assert audits[-1][1]["ok"] is False


def test_rate_limit_is_per_agent_and_returns_retry_after():
    client, _, audits, _ = build(rate_limit=2)
    headers = {"X-Agent-Key": KEY}
    assert client.post("/agent/tools/weather", json={}, headers=headers).status_code == 200
    assert client.post("/agent/tools/weather", json={}, headers=headers).status_code == 200
    limited = client.post("/agent/tools/weather", json={}, headers=headers)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert any(action == "rate_limited" for action, _ in audits)


def test_audit_never_contains_the_raw_key():
    client, _, audits, _ = build()
    client.post("/agent/tools/weather", json={"city": "北京"},
                headers={"X-Agent-Key": KEY})
    assert KEY not in json.dumps(audits, ensure_ascii=False)


def test_register_is_idempotent():
    app = FastAPI()
    first = agent_bridge.register(app, config=CONFIG, tools=Tools(),
                                  audit=lambda *_: None)
    second = agent_bridge.register(app, config=CONFIG, tools=Tools(),
                                   audit=lambda *_: None)
    assert first is second


def test_one_line_register_resolves_live_host_graph(monkeypatch):
    import sys
    import types

    class Store:
        def __init__(self):
            self.events = []

        def log_plugin_event(self, plugin_id, action, detail=""):
            self.events.append((plugin_id, action, detail))

    module = types.ModuleType("app")
    module.CFG = CONFIG
    module.TOOLS = Tools()
    module.AGENTS = {item["id"]: dict(item) for item in CONFIG["agents"]}
    module.store = Store()
    monkeypatch.setitem(sys.modules, "app", module)

    app = FastAPI()
    agent_bridge.register(app)
    client = TestClient(app)
    assert client.post("/agent/tools/calculator", json={"expression": "1+1"},
                       headers={"X-Agent-Key": KEY}).status_code == 200
    assert module.store.events[0][0] == "agent_bridge"

    # Key rotation in the live AGENTS mapping is picked up without re-registering.
    module.AGENTS["lyph"]["api_key"] = "rotated"
    assert client.post("/agent/tools/calculator", json={"expression": "1+1"},
                       headers={"X-Agent-Key": KEY}).status_code == 401
    assert client.post("/agent/tools/calculator", json={"expression": "1+1"},
                       headers={"X-Agent-Key": "rotated"}).status_code == 200


def test_missing_lyph_key_means_no_agent_can_authenticate():
    config = {"agents": [{"id": "lyph", "api_key": ""}]}
    client, tools, _, _ = build(config=config)
    response = client.post("/agent/tools/weather", json={},
                           headers={"X-Agent-Key": ""})
    assert response.status_code == 401
    assert tools.calls == []
