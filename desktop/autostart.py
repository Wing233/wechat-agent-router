"""Install/remove a per-user Startup shortcut for headless auto-start."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from paths import is_frozen, source_root

SHORTCUT_NAME = "WeChatAgentDesktop.lnk"


def shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / SHORTCUT_NAME


def is_enabled() -> bool:
    return shortcut_path().is_file()


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def set_enabled(enabled: bool) -> bool:
    path = shortcut_path()
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_frozen():
        target = Path(sys.executable).resolve()
        arguments = "--headless"
        workdir = target.parent
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        target = pythonw if pythonw.exists() else Path(sys.executable).resolve()
        script = Path(__file__).resolve().parent / "main.py"
        arguments = f'"{script}" --headless'
        workdir = source_root()
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(str(path)) + ");"
        "$s.TargetPath=" + _ps_quote(str(target)) + ";"
        "$s.Arguments=" + _ps_quote(arguments) + ";"
        "$s.WorkingDirectory=" + _ps_quote(str(workdir)) + ";"
        "$s.WindowStyle=7;"
        "$s.Description='微信 Agent 桌面控制台（后台服务）';"
        "$s.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        raise RuntimeError(f"创建开机自启快捷方式失败：{detail}")
    return is_enabled()
