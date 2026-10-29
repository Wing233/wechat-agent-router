"""Window shells: WebView2 via pywebview first, Edge app window as fallback."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path

from paths import edge_profile_dir


def _find_browser() -> Path | None:
    candidates = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for item in candidates:
        if item.is_file():
            return item
    return None


def open_app_window(url: str) -> subprocess.Popen | None:
    browser = _find_browser()
    if not browser:
        return None
    profile = edge_profile_dir()
    args = [
        str(browser),
        f"--app={url}",
        "--window-size=1320,900",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
    ]
    return subprocess.Popen(args, close_fds=True)


def _run_webview(url: str, app) -> None:
    import webview  # type: ignore
    window = webview.create_window(
        "微信 Agent 控制台",
        url,
        width=1320,
        height=900,
        min_size=(1080, 700),
        background_color="#f4f6f8",
    )
    app.on_quit = window.destroy
    webview.start(debug=False)


def run_window(url: str, app, mode: str = "auto") -> str:
    """Block until the UI window closes. Returns the mode that was used."""
    if mode in ("auto", "webview"):
        try:
            _run_webview(url, app)
            return "webview"
        except Exception as exc:
            app.log.add("WARN", f"WebView 窗口不可用：{exc}")
            if mode == "webview":
                raise
    if mode in ("auto", "edge"):
        proc = open_app_window(url)
        if proc is not None:
            app.log.add("INFO", "已打开 Edge 应用窗口")
            app.enable_ui_watchdog(180)
            app.on_quit = proc.terminate
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
            finally:
                app.on_quit = None
            return "edge"
    app.log.add("INFO", "未找到 Edge/Chrome，改用默认浏览器")
    webbrowser.open(url)
    app.enable_ui_watchdog(180)
    while not app.stop_event.wait(1):
        pass
    return "browser"


def open_existing(url: str) -> str:
    """Open the UI of an already-running instance, then let the caller exit."""
    try:
        import webview  # type: ignore
        webview.create_window("微信 Agent 控制台", url, width=1320, height=900,
                              min_size=(1080, 700), background_color="#f4f6f8")
        webview.start(debug=False)
        return "webview"
    except Exception:
        pass
    proc = open_app_window(url)
    if proc is not None:
        return "edge"
    webbrowser.open(url)
    return "browser"
