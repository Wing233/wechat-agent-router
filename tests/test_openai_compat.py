import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import openai_compat

TOOL = {
    "type": "function",
    "function": {
        "name": "get_public_profile",
        "parameters": {"type": "object", "properties": {}},
    },
}


class FakeLLM:
    def __init__(self):
        self.calls = []

    def complete(self, messages, tools=None, max_tokens=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        return {"content": "PONG", "tool_calls": []}


def make_client():
    llm = FakeLLM()
    app = FastAPI()
    openai_compat.register(app, llm=llm, service_token="test-token")
    return TestClient(app), llm


def request(client, **overrides):
    payload = {
        "model": "free-router",
        "messages": [{"role": "user", "content": "test"}],
        "tools": [TOOL],
    }
    payload.update(overrides)
    return client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer test-token"},
    )


def test_tool_choice_none_omits_tools():
    client, llm = make_client()
    response = request(client, tool_choice="none")
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert llm.calls[0]["tools"] is None


def test_tool_choice_specific_filters_definitions():
    client, llm = make_client()
    response = request(client, tool_choice={"type": "function", "function": {"name": "get_public_profile"}})
    assert response.status_code == 200
    assert [tool["function"]["name"] for tool in llm.calls[0]["tools"]] == ["get_public_profile"]
    assert response.json()["choices"][0]["finish_reason"] == "stop"


def test_tool_choice_required_without_tools_is_rejected():
    client, _ = make_client()
    response = request(client, tools=[], tool_choice="required")
    assert response.status_code == 400
    assert response.json()["detail"] == "tool_choice required needs tools"


def test_stream_chunk_uses_no_tool_calls_when_tool_choice_is_none():
    client, _ = make_client()
    response = request(client, tool_choice="none", stream=True)
    assert response.status_code == 200
    assert '"finish_reason": "stop"' in response.text
    assert "tool_calls" not in response.text
    assert "data: [DONE]" in response.text
