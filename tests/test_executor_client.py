"""Offline checks for the optional isolated executor client."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from executor_client import ExecutorClient, ExecutorError


class Ctx:
    def __init__(self, config: dict, token: str = "test-token"):
        self.root_config = {"executor": config}
        self._token = token

    def secret(self, name: str) -> str:
        return self._token


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_host_mode_is_disabled() -> None:
    client = ExecutorClient(Ctx({"mode": "host"}))
    assert client.enabled is False


def test_isolated_python_request(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["headers"] = {key.lower(): value
                               for key, value in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"exit_code": 0, "stdout": "4\n", "stderr": ""})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = ExecutorClient(Ctx({
        "mode": "isolated", "url": "http://executor:9800",
    }))
    result = client.python("print(4)", 10)
    assert result["stdout"] == "4\n"
    assert captured["url"] == "http://executor:9800/v1/python"
    assert captured["body"]["code"] == "print(4)"
    assert captured["body"]["timeout"] == 10
    assert captured["headers"]["x-executor-token"] == "test-token"


def test_isolated_shell_request(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"exit_code": 0, "stdout": "ok\n", "stderr": ""})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = ExecutorClient(Ctx({
        "mode": "isolated", "url": "http://executor:9800",
    }))
    result = client.shell("echo ok", 20, cwd="/workspace", sandbox=True)
    assert result["stdout"] == "ok\n"
    assert captured["body"]["command"] == "echo ok"
    assert captured["body"]["cwd"] == "/workspace"
    assert captured["body"]["sandbox"] is True


def test_missing_url_fails_closed() -> None:
    client = ExecutorClient(Ctx({"mode": "isolated", "url": ""}))
    try:
        client.python("print(1)", 5)
    except ExecutorError as exc:
        assert "url" in str(exc)
    else:
        raise AssertionError("expected ExecutorError")
