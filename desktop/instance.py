"""Single-instance handoff: a second launch opens the already-running console."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path


def read_state(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def clear_state(path: Path, pid: int | None = None) -> None:
    state = read_state(path)
    if pid is not None and state.get("pid") not in (None, pid):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def is_alive(state: dict, timeout: float = 1.5) -> bool:
    port = state.get("port")
    token = state.get("token")
    if not port or not token:
        return False
    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/api/ping")
    request.add_header("X-Desktop-Token", str(token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def make_state(port: int, token: str, url: str, headless: bool) -> dict:
    return {
        "pid": os.getpid(),
        "port": int(port),
        "token": token,
        "url": url,
        "headless": bool(headless),
        "started_at": int(time.time()),
    }
