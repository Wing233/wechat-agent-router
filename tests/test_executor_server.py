"""Offline checks for the isolated executor sidecar."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import executor.server as server


def make_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("EXECUTOR_TOKEN", "test-token")
    monkeypatch.setenv("EXECUTOR_WORKSPACE", str(tmp_path))
    importlib.reload(server)
    return TestClient(server.app)


def test_health_and_auth(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["token_configured"] is True
    denied = client.post("/v1/python", json={"code": "print(1)"})
    assert denied.status_code == 401


def test_python_runs_in_workspace(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/v1/python", json={"code": "print(2 + 2)"},
        headers={"X-Executor-Token": "test-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "4"


def test_cwd_outside_workspace_is_rejected(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/v1/shell", json={"command": "pwd", "cwd": "/app"},
        headers={"X-Executor-Token": "test-token"})
    assert response.status_code == 400


@pytest.mark.skipif(os.name != "posix", reason="executor shell is Linux-only")
def test_shell_runs_or_reports_missing_bash(tmp_path: Path, monkeypatch) -> None:
    client = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/v1/shell", json={"command": "echo hello"},
        headers={"X-Executor-Token": "test-token"})
    assert response.status_code == 200
    body = response.json()
    if shutil.which("bash"):
        assert body["stdout"].strip() == "hello"
    else:
        assert "bash" in body.get("error", "")
