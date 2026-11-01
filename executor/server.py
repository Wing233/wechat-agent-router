"""Minimal isolated executor sidecar.

It intentionally knows nothing about the Router: no /data, no source mounts,
no model keys. It only runs Python/shell inside EXECUTOR_WORKSPACE.
"""
from __future__ import annotations

import hmac
import os
import shutil
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


def _workspace_path(raw: str, *, must_exist: bool = False) -> Path:
    text = str(raw or "")
    if not text:
        raise HTTPException(400, "path required")
    if text == "/workspace":
        candidate = WORKSPACE
    elif text.startswith("/workspace/"):
        candidate = WORKSPACE / text[len("/workspace/"):]
    elif text.startswith("/"):
        raise HTTPException(400, "path must be under /workspace")
    else:
        candidate = WORKSPACE / text
    candidate = candidate.resolve()
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise HTTPException(400, "path must be under /workspace")
    if must_exist and not candidate.exists():
        raise HTTPException(404, "path does not exist")
    return candidate


class PythonIn(BaseModel):
    code: str
    timeout: int = 30


class ShellIn(BaseModel):
    command: str
    timeout: int = 60
    cwd: str = "/workspace"
    sandbox: bool = False


class FsIn(BaseModel):
    action: str
    path: str
    content: str = ""
    dst: str = ""


class PatchIn(BaseModel):
    patch: str
    cwd: str = "/workspace"


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


@app.post("/v1/fs")
def run_fs(body: FsIn, x_executor_token: str = Header(default="")):
    _auth(x_executor_token)
    action = str(body.action or "").lower()
    path = _workspace_path(
        body.path, must_exist=action in {"list", "read", "delete", "move"})
    try:
        if action == "list":
            if not path.is_dir():
                raise HTTPException(400, "path is not a directory")
            items = []
            for entry in sorted(path.iterdir(), key=lambda item: item.name.lower())[:200]:
                items.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else None,
                })
            return {"path": str(path), "items": items}
        if action == "read":
            if not path.is_file():
                raise HTTPException(400, "path is not a file")
            raw = path.read_text(encoding="utf-8", errors="replace")
            content, truncated = _truncate(raw)
            return {"path": str(path), "content": content,
                    "truncated": truncated}
        if action in {"write", "replace"}:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(body.content or "")[:64_000], encoding="utf-8")
            return {"ok": True, "path": str(path),
                    "detail": f"已写入 {path}"}
        if action == "delete":
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            return {"ok": True, "path": str(path),
                    "detail": f"已删除 {path}"}
        if action == "move":
            dst = _workspace_path(body.dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dst))
            return {"ok": True, "path": str(dst),
                    "detail": f"已移动 {path} → {dst}"}
        raise HTTPException(400, "unknown fs action")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/v1/patch")
def run_patch(body: PatchIn, x_executor_token: str = Header(default="")):
    _auth(x_executor_token)
    cwd = _workspace_path(body.cwd, must_exist=True)
    if not cwd.is_dir():
        raise HTTPException(400, "cwd is not a directory")
    patch = str(body.patch or "")
    if not patch.strip():
        raise HTTPException(400, "patch required")
    if len(patch.encode("utf-8")) > 64 * 1024:
        raise HTTPException(400, "patch exceeds 64KB")
    fd, raw_path = tempfile.mkstemp(prefix="executor-", suffix=".patch")
    os.close(fd)
    tmp = Path(raw_path)
    tmp.write_text(patch, encoding="utf-8")
    try:
        git = shutil.which("git")
        if git:
            check = subprocess.run(
                [git, "apply", "--check", str(tmp)], cwd=str(cwd),
                capture_output=True, text=True, timeout=20)
            if check.returncode == 0:
                apply = subprocess.run(
                    [git, "apply", str(tmp)], cwd=str(cwd),
                    capture_output=True, text=True, timeout=30)
                if apply.returncode != 0:
                    return {"error": f"git apply 失败：{apply.stderr[-800:]}"}
                return {"ok": True, "cwd": str(cwd), "method": "git apply"}
            check_error = check.stderr[-800:]
        else:
            check_error = "git 不可用"
        patch_bin = shutil.which("patch")
        if patch_bin:
            apply = subprocess.run(
                [patch_bin, "-p1", "--batch", "--forward", "-i", str(tmp)],
                cwd=str(cwd), capture_output=True, text=True, timeout=30)
            if apply.returncode == 0:
                return {"ok": True, "cwd": str(cwd), "method": "patch"}
            return {"error": f"patch 失败：{apply.stderr[-800:] or apply.stdout[-800:]}"}
        return {"error": f"补丁无法应用：{check_error}"}
    except subprocess.TimeoutExpired:
        return {"error": "补丁应用超时"}
    finally:
        tmp.unlink(missing_ok=True)
