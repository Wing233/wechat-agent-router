"""Entry point for the WeChat Agent desktop console."""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent
for extra in (str(DESKTOP_DIR), str(ROOT / "local-agent")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from app import DesktopApp
from instance import clear_state, is_alive, make_state, read_state, write_state
from paths import state_file
from window import open_existing, run_window


def _fatal(message: str, gui: bool = True) -> None:
    print(message, file=sys.stderr)
    if gui and os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, "微信 Agent 控制台", 0x10)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="微信 Agent 桌面控制台")
    parser.add_argument("--config", help="local-agent 配置文件路径")
    parser.add_argument("--headless", action="store_true", help="只启动后台服务，不打开窗口")
    parser.add_argument("--window", choices=["auto", "webview", "edge", "browser"],
                        default="auto", help="窗口承载方式")
    args = parser.parse_args()

    state_path = state_file()
    existing = read_state(state_path)
    if existing and is_alive(existing):
        if args.headless:
            print("桌面控制台已在后台运行")
            return
        port = existing.get("port")
        token = existing.get("token")
        url = existing.get("url") or f"http://127.0.0.1:{port}/#t={token}"
        mode = open_existing(url)
        print(f"已打开正在运行的桌面控制台（{mode}）")
        return
    if existing:
        clear_state(state_path)

    app = DesktopApp(args.config)
    app.headless = bool(args.headless)
    app.window_mode = args.window
    try:
        app.start()
    except Exception as exc:
        _fatal(f"桌面控制台启动失败：\n{exc}", gui=not args.headless)
        raise SystemExit(1) from None

    state = make_state(app.server.port, app.token, app.server.ui_url, app.headless)
    write_state(state_path, state)
    app.log.add("INFO", f"本地接口：http://127.0.0.1:{app.server.port}/")

    if app.headless:
        print(f"微信 Agent 控制台后台运行中：http://127.0.0.1:{app.server.port}/")
        try:
            while not app.stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            app.shutdown()
        return

    mode = args.window
    try:
        used = run_window(app.server.ui_url, app, mode)
    except Exception as exc:
        app.log.add("WARN", f"{mode} 窗口启动失败：{exc}")
        try:
            used = run_window(app.server.ui_url, app, "auto")
        except Exception as exc2:
            _fatal(f"无法打开界面：\n{exc2}")
            app.shutdown()
            raise SystemExit(1) from None
    app.window_mode = used
    app.log.add("INFO", f"窗口模式：{used}")
    try:
        app.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    main()
