"""Filesystem locations for both source runs and the packaged executable."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "WeChatAgentDesktop"
VERSION = "1.0.0"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def web_dir() -> Path:
    if is_frozen():
        return bundle_dir() / "desktop" / "web"
    return Path(__file__).resolve().parent / "web"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    env = os.environ.get("WECHAT_AGENT_DATA")
    if env:
        path = Path(env).expanduser().resolve()
    elif is_frozen():
        portable = Path(sys.executable).resolve().parent / "data"
        path = portable if _writable(portable) else Path(os.environ.get(
            "LOCALAPPDATA", tempfile.gettempdir())) / APP_NAME
    else:
        path = Path(__file__).resolve().parent / ".runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("WECHAT_AGENT_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        exe = Path(sys.executable).resolve().parent
        for candidate in (exe / "config.json", exe / "local-agent" / "config.json",
                          data_dir() / "config.json"):
            if candidate.is_file():
                return candidate
        if _writable(exe):
            return exe / "config.json"
        return data_dir() / "config.json"
    return source_root() / "local-agent" / "config.json"


def state_file() -> Path:
    return data_dir() / "instance.json"


def settings_file() -> Path:
    return data_dir() / "settings.json"


def secrets_file() -> Path:
    return data_dir() / "secrets.json"


def log_file() -> Path:
    return data_dir() / "desktop.log"


def edge_profile_dir() -> Path:
    path = data_dir() / "edge-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path
