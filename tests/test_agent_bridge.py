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
    assert body["tools"] == [
        "aggregate_companies", "calculator", "get_company", "get_public_content",
        "get_public_profile", "search_companies", "search_public_content",
        "weather", "web_fetch", "web_search",
    ]
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


def test_lyph_domain_tools_route_to_the_lyph_plugin():
    client, tools, audits, _ = build()
    cases = {
        "get_public_profile": {"section": "stack"},
        "search_public_content": {"query": "缓存"},
        "get_public_content": {"content_id": "c1"},
        "search_companies": {"city": "深圳"},
        "get_company": {"company_id": "c-1"},
        "aggregate_companies": {"group_by": "industry"},
    }
    for name, payload in cases.items():
        response = client.post(f"/agent/tools/{name}", json=payload,
                               headers={"X-Agent-Key": KEY})
        assert response.status_code == 200, name
        assert tools.calls[-1] == ("lyph_tools__" + name, payload)
    assert {action for action, _ in audits} == {"tool_call"}


def test_coded_tool_errors_keep_their_code_and_hide_nothing_else():
    client, tools, audits, _ = build()
    tools.reply = {"data": None, "blocks": [],
                   "error": {"code": "not_found", "message": "内容不存在或尚未授权用于访客对话。"}}
    response = client.post("/agent/tools/get_public_content", json={"content_id": "x"},
                           headers={"X-Agent-Key": KEY})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == {"code": "not_found", "message": "内容不存在或尚未授权用于访客对话。"}
    assert audits[-1][1]["ok"] is False and audits[-1][1]["error"] == "not_found"


def test_no_allowed_tool_can_start_with_a_forbidden_prefix():
    forbidden = agent_bridge.FORBIDDEN_TOOL_PREFIXES
    assert not [name for name in agent_bridge.ALLOWED_TOOLS if name.startswith(forbidden)]
    assert not [name for name in agent_bridge.ALLOWED_TOOLS
                if name in agent_bridge.FORBIDDEN_TOOL_NAMES]
    # Every target must exist in the registry naming convention, not in a denied namespace
    # for the *caller*; targets are router-internal names.
    assert set(agent_bridge.ALLOWED_TOOLS.values()) == {
        "websearch__web_search", "websearch__web_fetch", "websearch__weather",
        "websearch__calculator",
        "lyph_tools__get_public_profile", "lyph_tools__search_public_content",
        "lyph_tools__get_public_content", "lyph_tools__search_companies",
        "lyph_tools__get_company", "lyph_tools__aggregate_companies",
    }

