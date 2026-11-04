"""Check GitHub Releases and apply a one-click update for the packaged app.

The release workflow publishes a single ``WeChatAgentDesktop.exe`` asset.
Checking uses the GitHub API first and falls back to the release page
redirect, so a rate-limited API does not break the button.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from paths import VERSION, is_frozen

REPO = "Wing233/wechat-agent-router"
ASSET = "WeChatAgentDesktop.exe"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
LATEST_PAGE = f"https://github.com/{REPO}/releases/latest"
DOWNLOAD_URL = f"https://github.com/{REPO}/releases/latest/download/{ASSET}"
USER_AGENT = f"wechat-agent-desktop/{VERSION}"

_lock = threading.Lock()
_state = {
    "state": "idle",  # idle | checking | downloading | applying | error
    "progress": 0.0,
    "error": "",
    "current": VERSION,
    "latest": "",
    "tag": "",
    "available": False,
    "size": 0,
    "checked_at": 0,
}


def _set(**changes) -> None:
    with _lock:
        _state.update(changes)


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def _safe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "网络不可达"
    return str(exc)[:300] or type(exc).__name__


def _version_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _latest_tag(timeout: float) -> str:
    request = urllib.request.Request(
        API_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tag = str(payload.get("tag_name") or "").strip()
        if tag:
            return tag
    except Exception:
        pass

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(LATEST_PAGE, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with opener.open(request, timeout=timeout) as response:
            location = response.headers.get("Location") or ""
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") or ""
    match = re.search(r"/releases/tag/([^/?#]+)", location)
    if not match:
        raise RuntimeError("无法读取最新版本号")
    return urllib.parse.unquote(match.group(1))


def check(timeout: float = 12) -> dict:
    _set(state="checking", error="", progress=0.0)
    try:
        tag = _latest_tag(timeout)
        latest = tag.lstrip("vV")
        _set(
            state="idle",
            tag=tag,
            latest=latest,
            available=_version_key(latest) > _version_key(VERSION),
            checked_at=int(time.time()),
            error="",
        )
    except Exception as exc:
        _set(state="error", error=_safe(exc), checked_at=int(time.time()))
    return snapshot()


def apply_async(on_ready) -> dict:
    if not is_frozen():
        return {"started": False, "error": "源码模式不支持自动更新，请下载最新 exe"}
    with _lock:
        if _state["state"] in ("downloading", "applying"):
            return {"started": False, "error": "更新正在进行"}
        _state.update(state="downloading", progress=0.0, error="")
    threading.Thread(target=_download_and_install, args=(on_ready,), name="app-update", daemon=True).start()
    return {"started": True}


def _updater_script(exe: Path) -> Path:
    script = exe.with_name(".update-WeChatAgentDesktop.cmd")
    content = (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "DIR=%~dp0"\r\n'
        'set "EXE=%DIR%WeChatAgentDesktop.exe"\r\n'
        'set "NEW=%DIR%WeChatAgentDesktop.exe.new"\r\n'
        'set "BAK=%DIR%.WeChatAgentDesktop.exe.previous"\r\n'
        ":wait\r\n"
        'tasklist /FI "IMAGENAME eq WeChatAgentDesktop.exe" 2>nul | find /I "WeChatAgentDesktop.exe" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        "for /L %%i in (1,1,20) do (\r\n"
        '  move /Y "%EXE%" "%BAK%" >nul 2>nul\r\n'
        '  if not exist "%EXE%" goto replace\r\n'
        "  timeout /t 1 /nobreak >nul\r\n"
        ")\r\n"
        "goto done\r\n"
        ":replace\r\n"
        'move /Y "%NEW%" "%EXE%" >nul 2>nul\r\n'
        'start "" "%EXE%"\r\n'
        ":done\r\n"
        'del "%~f0"\r\n'
    )
    script.write_text(content, encoding="ascii", newline="")
    return script


def _download_and_install(on_ready) -> None:
    exe = Path(sys.executable).resolve()
    target = exe.with_name(exe.name + ".new")
    try:
        request = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            _set(size=total)
            done = 0
            with open(target, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if total:
                        _set(progress=min(0.99, done / total))
        if target.stat().st_size < 1_000_000:
            raise RuntimeError("下载文件过小，可能不是有效的安装包")
        with open(target, "rb") as handle:
            if handle.read(2) != b"MZ":
                raise RuntimeError("下载文件不是有效的 Windows 程序")
        script = _updater_script(exe)
        _set(state="applying", progress=1.0)
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            cwd=str(exe.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        on_ready()
    except Exception as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        _set(state="error", error=_safe(exc))
