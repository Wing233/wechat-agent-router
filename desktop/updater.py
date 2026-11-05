"""Check GitHub Releases and apply a one-click update for the packaged app.

The release workflow publishes a single ``WeChatAgentDesktop.exe`` asset.
Checking uses the GitHub API first and falls back to the release page
redirect, so a rate-limited API does not break the button.
"""
from __future__ import annotations

import hashlib
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
DOWNLOAD_BASE = f"https://github.com/{REPO}/releases/download"
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
    "relation": "",  # newer | same | older (local vs. latest release)
    "exe_url": "",
    "sha_url": "",
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


def _page_tag(timeout: float) -> str:
    opener = urllib.request.build_opener(_NoRedirect)
    url = f"{LATEST_PAGE}?t={int(time.time())}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        method="HEAD",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            location = response.headers.get("Location") or ""
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") or ""
    match = re.search(r"/releases/tag/([^/?#]+)", location)
    if not match:
        raise RuntimeError("无法读取最新版本号")
    return urllib.parse.unquote(match.group(1))


def _release_info(timeout: float) -> tuple[str, str, str]:
    """Return (tag, exe_url, sha_url) for the latest release.

    Prefer the release page redirect: it is not subject to the unauthenticated
    API rate limit and is the authoritative "latest" pointer. Exact per-tag
    asset URLs are constructed from the tag, so a stale CDN response for
    ``releases/latest/download`` cannot leak an older exe into the update.
    The API is only a fallback.
    """
    try:
        tag = _page_tag(timeout)
        base = f"{DOWNLOAD_BASE}/{urllib.parse.quote(tag)}"
        return tag, f"{base}/{ASSET}", f"{base}/{ASSET}.sha256"
    except Exception:
        pass

    request = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tag = str(payload.get("tag_name") or "").strip()
        assets = {
            str(asset.get("name") or ""): str(asset.get("browser_download_url") or "")
            for asset in payload.get("assets") or []
        }
        if tag and assets.get(ASSET):
            return tag, assets[ASSET], assets.get(ASSET + ".sha256", "")
    except Exception:
        pass
    raise RuntimeError("无法读取最新版本号")


def _latest_tag(timeout: float) -> str:
    return _release_info(timeout)[0]


def check(timeout: float = 12) -> dict:
    _set(state="checking", error="", progress=0.0)
    try:
        tag, exe_url, sha_url = _release_info(timeout)
        latest = tag.lstrip("vV")
        current_key = _version_key(VERSION)
        latest_key = _version_key(latest)
        relation = "newer" if latest_key > current_key else "same" if latest_key == current_key else "older"
        _set(
            state="idle",
            tag=tag,
            latest=latest,
            available=relation == "newer",
            relation=relation,
            exe_url=exe_url,
            sha_url=sha_url,
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
        "timeout /t 2 /nobreak >nul\r\n"
        'set "_MEIPASS2="\r\n'
        'set "_MEIPASS="\r\n'
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"\r\n'
        'start "" /D "%DIR%" "%EXE%"\r\n'
        ":done\r\n"
        'del "%~f0"\r\n'
    )
    script.write_text(content, encoding="ascii", newline="")
    return script


def _download_and_install(on_ready) -> None:
    exe = Path(sys.executable).resolve()
    target = exe.with_name(exe.name + ".new")
    try:
        exe_url = str(_state.get("exe_url") or "")
        sha_url = str(_state.get("sha_url") or "")
        if not exe_url:
            _tag, exe_url, sha_url = _release_info(12)
            _set(exe_url=exe_url, sha_url=sha_url)

        expected = ""
        if sha_url:
            try:
                request = urllib.request.Request(
                    sha_url,
                    headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    expected = response.read().decode("utf-8", "replace").split()[0].lower()
            except Exception:
                expected = ""

        request = urllib.request.Request(
            exe_url,
            headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        )
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
        if expected:
            digest = hashlib.sha256()
            with open(target, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != expected:
                raise RuntimeError("下载文件校验失败，请重试")
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
