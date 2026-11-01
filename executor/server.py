"""Minimal isolated executor sidecar.

It intentionally knows nothing about the Router: no /data, no source mounts,
no model keys. It only runs Python/shell inside EXECUTOR_WORKSPACE.
"""
from __future__ import annotations

import hmac
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

TOKEN = os.environ.get("EXECUTOR_TOKEN", "")
WORKSPACE = Path(os.environ.get("EXECUTOR_WORKSPACE", "/workspace")).resolve()
MAX_OUTPUT = 8000

app = FastAPI(title="WeChat Agent Executor", docs_url=None, redoc_url=None,
              openapi_url=None)


def _auth(token: str) -> None:
    if not TOKEN or not token or not hmac.compare_digest(TOKEN, token):
        raise HTTPException(401, "invalid executor token")


def _env() -> dict:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def _truncate(text) -> tuple[str, bool]:
    value = str(text or "")
    if len(value) <= MAX_OUTPUT:
        return value, False
    return value[-MAX_OUTPUT:], True


def _cwd(raw: str, sandbox: bool) -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if sandbox:
        return Path(tempfile.mkdtemp(prefix="sandbox-", dir=str(WORKSPACE)))
    text = str(raw or "/workspace")
    if text == "/workspace":
        candidate = WORKSPACE
    elif text.startswith("/workspace/"):
        candidate = WORKSPACE / text[len("/workspace/"):]
    elif text.startswith("/"):
        raise HTTPException(400, "cwd must be under /workspace")
    else:
        candidate = WORKSPACE / text
    candidate = candidate.resolve()
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise HTTPException(400, "cwd must be under /workspace")
    if not candidate.is_dir():
        raise HTTPException(400, "cwd does not exist")
    return candidate


class PythonIn(BaseModel):
    code: str
    timeout: int = 30


class ShellIn(BaseModel):
    command: str
    timeout: int = 60
    cwd: str = "/workspace"
    sandbox: bool = False


@app.get("/health")
def health():
    return {"ok": True, "workspace": str(WORKSPACE),
            "token_configured": bool(TOKEN)}


@app.post("/v1/python")
def run_python(body: PythonIn, x_executor_token: str = Header(default="")):
    _auth(x_executor_token)
    timeout = max(3, min(int(body.timeout or 30), 120))
    cwd = _cwd("/workspace", False)
    try:
        proc = subprocess.run(
            [sys.executable, "-"], input=body.code, cwd=str(cwd),
            capture_output=True, text=True, errors="replace",
            timeout=timeout, env=_env(),
        )
    except subprocess.TimeoutExpired as exc:
        out, _ = _truncate(exc.stdout or "")
        err, _ = _truncate(exc.stderr or "")
        return {"error": f"执行超时（{timeout}s）", "stdout": out, "stderr": err}
    out, cut_out = _truncate(proc.stdout)
    err, cut_err = _truncate(proc.stderr)
    return {"exit_code": proc.returncode, "stdout": out, "stderr": err,
            "cwd": str(cwd), "truncated": cut_out or cut_err}


@app.post("/v1/shell")
def run_shell(body: ShellIn, x_executor_token: str = Header(default="")):
    _auth(x_executor_token)
    timeout = max(3, min(int(body.timeout or 60), 120))
    cwd = _cwd(body.cwd, body.sandbox)
    try:
        proc = subprocess.run(
            ["bash", "-lc", body.command], cwd=str(cwd),
            capture_output=True, text=True, errors="replace",
            timeout=timeout, env=_env(),
        )
    except subprocess.TimeoutExpired as exc:
        out, _ = _truncate(exc.stdout or "")
        err, _ = _truncate(exc.stderr or "")
        return {"error": f"命令超时（{timeout}s）", "stdout": out, "stderr": err}
    except FileNotFoundError:
        return {"error": "执行器缺少 bash"}
    out, cut_out = _truncate(proc.stdout)
    err, cut_err = _truncate(proc.stderr)
    return {"exit_code": proc.returncode, "stdout": out, "stderr": err,
            "cwd": str(cwd), "truncated": cut_out or cut_err}
