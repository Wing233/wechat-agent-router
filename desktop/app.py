"""Desktop application core: local agent lifecycle + router proxy."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / "local-agent", Path(__file__).resolve().parent):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from agent_core import EventLog, LocalAgent, VERSION as AGENT_VERSION, load_config, save_config
from autostart import is_enabled as autostart_enabled, set_enabled as set_autostart
from errors import ApiError
from paths import (APP_NAME, VERSION, config_path as default_config_path, data_dir,
                   log_file, secrets_file, settings_file, state_file, web_dir)
from store import DesktopSettings
from secret_store import SecretStore

import secrets as pysecrets


def _mask(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text[:6] + "..." + text[-4:] if len(text) > 12 else "***"


def _json_request(method: str, url: str, body: dict | None = None,
                  headers: dict | None = None, timeout: float = 10) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method.upper())
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", f"wechat-agent-desktop/{VERSION}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")[:800]
        try:
            parsed = json.loads(raw)
            detail = parsed.get("detail") or parsed.get("error") or raw
        except Exception:
            detail = raw
        raise ApiError(exc.code, str(detail)) from None
    except urllib.error.URLError as exc:
        raise ApiError(502, f"无法连接服务器：{exc.reason}") from None
    except TimeoutError:
        raise ApiError(504, "连接服务器超时") from None


class DesktopApp:
    def __init__(self, explicit_config: str | None = None):
        self.config_path = default_config_path(explicit_config)
        self.web_dir = web_dir()
        self.data_dir = data_dir()
        self.log = EventLog(capacity=800, file_path=log_file(), source="desktop")
        self.token = pysecrets.token_urlsafe(32)
        self.started_at = time.time()
        self.last_ui_seen = 0.0
        self._shutdown_lock = threading.RLock()
        self._shutting_down = False
        self.stop_event = threading.Event()
        self.headless = False
        self.window_mode = "auto"
        self.server = None
        self.on_quit = None
        self.agent: LocalAgent | None = None
        self.config_error = ""
        self.settings = DesktopSettings(settings_file(), SecretStore(secrets_file()))
        self.config = self._load_initial_config()
        self.window_mode = str(self.settings.get("window_mode", "auto"))

    # ------------------------------------------------------------------
    def _load_initial_config(self) -> dict:
        try:
            if not self.config_path.is_file():
                raise FileNotFoundError(self.config_path)
            return load_config(self.config_path)
        except FileNotFoundError:
            from agent_core import default_config, normalize_config
            cfg = normalize_config(default_config())
            save_config(self.config_path, cfg)
            self.log.add("INFO", f"已创建本机配置：{self.config_path}")
            return cfg
        except Exception as exc:
            self.config_error = str(exc)
            self.log.add("ERROR", f"本机配置读取失败：{exc}")
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                return raw if isinstance(raw, dict) else {}
            except Exception:
                from agent_core import default_config
                return default_config()

    def start(self) -> None:
        from api_server import ApiServer
        self.server = ApiServer(self)
        self.server.start()
        self.log.add("INFO", f"桌面控制台已启动：{self.server.url}")
        if self.config.get("router") and not self.config_error:
            try:
                self.start_agent()
            except Exception as exc:
                self.log.add("WARN", f"本机 Agent 未启动：{exc}")

    # ------------------------------------------------------------------
    def effective_server_url(self) -> str:
        return str(self.settings.get("server_url") or self.config.get("router") or "").rstrip("/")

    def masked_config(self) -> dict:
        cfg = json.loads(json.dumps(self.config, ensure_ascii=False, default=str))
        cfg["agent_key"] = _mask(cfg.get("agent_key"))
        return cfg

    def config_for_ui(self) -> dict:
        cfg = self.masked_config()
        cfg["_meta"] = {
            "config_path": str(self.config_path),
            "config_error": self.config_error,
            "engine_checks": self.engine_checks(),
            "workspace_checks": self.workspace_checks(),
        }
        return cfg

    def engine_checks(self) -> dict:
        result = {}
        for name, command in (self.config.get("engines") or {}).items():
            executable = shutil.which(str(command[0])) if command else None
            result[name] = {
                "command": list(command or []),
                "executable": executable or "",
                "found": bool(executable or (command and Path(str(command[0])).is_file())),
            }
        return result

    def workspace_checks(self) -> dict:
        return {alias: {"path": path, "exists": Path(path).is_dir()}
                for alias, path in (self.config.get("workspaces") or {}).items()}

    # ------------------------------------------------------------------
    def agent_status(self) -> dict:
        if self.agent is not None:
            status = self.agent.snapshot()
        else:
            status = {
                "status": "config_error" if self.config_error else "stopped",
                "running": False,
                "agent_id": self.config.get("agent_id", ""),
                "router": self.config.get("router", ""),
                "last_error": self.config_error,
                "last_seen": None,
                "started_at": None,
                "current": None,
                "tasks_done": 0,
                "last_task": None,
                "engines": sorted((self.config.get("engines") or {}).keys()),
                "workspaces": sorted((self.config.get("workspaces") or {}).keys()),
                "default_engine": self.config.get("default_engine", ""),
                "default_workspace": self.config.get("default_workspace", ""),
                "wait_seconds": self.config.get("wait_seconds", 60),
                "poll_seconds": self.config.get("poll_seconds", 2),
            }
        status["version"] = AGENT_VERSION
        status["config_error"] = self.config_error
        return status

    def start_agent(self) -> dict:
        if self.config_error:
            try:
                self.config = load_config(self.config_path)
                self.config_error = ""
            except Exception as exc:
                raise ApiError(400, f"配置仍未修复：{exc}") from None
        if not self.config.get("router"):
            raise ApiError(400, "请先填写 Router 地址")
        if self.agent and self.agent.snapshot().get("running"):
            return self.agent_status()
        self.agent = LocalAgent(self.config, self.log)
        self.agent.start()
        self.log.add("INFO", f"本机 Agent 已启动：{self.agent.agent_id}")
        return self.agent_status()

    def stop_agent(self) -> dict:
        if self.agent:
            self.agent.stop()
        return self.agent_status()

    def restart_agent(self) -> dict:
        if self.agent:
            self.agent.stop()
        self.agent = None
        return self.start_agent()

    def cancel_agent_task(self) -> dict:
        if not self.agent:
            return {"cancelled": False}
        return {"cancelled": self.agent.cancel()}

    def test_engine(self, engine: str, workspace: str = "") -> dict:
        cfg = self.config
        try:
            runner = LocalAgent(cfg, self.log)
        except Exception as exc:
            raise ApiError(400, f"配置无效：{exc}") from None
        try:
            return runner.test_engine(engine, workspace)
        except Exception as exc:
            raise ApiError(400, str(exc)) from None

    # ------------------------------------------------------------------
    def update_config(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ApiError(400, "配置必须是 JSON 对象")
        data = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        old_router = str(self.config.get("router") or "").rstrip("/")
        merged = dict(self.config)
        for key in ("router", "agent_id", "agent_key", "name", "description",
                    "default_engine", "default_workspace", "workspaces", "engines",
                    "wait_seconds", "task_timeout", "poll_seconds"):
            if key not in data:
                continue
            value = data[key]
            if key == "agent_key":
                text = str(value or "").strip()
                if not text or "..." in text or text == "***":
                    continue
                value = text
            merged[key] = value
        try:
            cfg = save_config(self.config_path, merged)
        except Exception as exc:
            raise ApiError(400, str(exc)) from None
        was_running = bool(self.agent and self.agent.snapshot().get("running"))
        if self.agent:
            self.agent.stop()
        self.config = cfg
        self.config_error = ""
        if self.settings.get("server_url") and old_router and \
                str(self.settings.get("server_url")).rstrip("/") == old_router:
            self.settings.set("server_url", cfg["router"])
        self.agent = LocalAgent(cfg, self.log)
        if was_running and cfg.get("router"):
            self.agent.start()
        self.log.add("INFO", "本机配置已保存")
        return self.config_for_ui()

    # ------------------------------------------------------------------
    def router_health(self, timeout: float = 3.0) -> dict:
        base = str(self.config.get("router") or "").rstrip("/")
        if not base:
            return {"ok": False, "error": "未配置 Router", "url": ""}
        started = time.time()
        try:
            status, data = _json_request("GET", base + "/health", timeout=timeout)
            return {"ok": status == 200 and bool(data.get("ok")),
                    "url": base, "latency_ms": int((time.time() - started) * 1000),
                    "data": data}
        except ApiError as exc:
            return {"ok": False, "url": base, "error": exc.message,
                    "latency_ms": int((time.time() - started) * 1000)}
        except Exception as exc:
            return {"ok": False, "url": base, "error": str(exc)[:300],
                    "latency_ms": int((time.time() - started) * 1000)}

    def router_json(self, method: str, path: str, body: dict | None = None,
                    token: str | None = None, base: str | None = None,
                    timeout: float = 15) -> dict:
        base = str(base or self.effective_server_url()).rstrip("/")
        if not base:
            raise ApiError(400, "尚未配置 Router 地址")
        token = self.settings.service_token if token is None else token
        if not token:
            raise ApiError(401, "尚未连接服务器，请先填写服务令牌")
        status, data = _json_request(method, base + path, body,
                                     headers={"X-Service-Token": token}, timeout=timeout)
        return data

    def server_connect(self, token: str, url: str = "") -> dict:
        token = str(token or "").strip()
        if not token:
            raise ApiError(400, "请填写服务器服务令牌")
        base = str(url or self.effective_server_url()).strip().rstrip("/")
        if not base:
            raise ApiError(400, "请填写服务器地址")
        overview = self.router_json("GET", "/internal/settings/overview",
                                    token=token, base=base, timeout=10)
        old_router = str(self.config.get("router") or "").rstrip("/")
        if base != old_router:
            merged = dict(self.config)
            merged["router"] = base
            try:
                self.config = save_config(self.config_path, merged)
            except Exception as exc:
                raise ApiError(400, f"服务器地址已连通，但保存本机配置失败：{exc}") from None
            if self.agent:
                self.agent.stop()
            self.agent = LocalAgent(self.config, self.log)
            if self.config.get("router"):
                try:
                    self.agent.start()
                except Exception as exc:
                    self.log.add("WARN", f"Agent 未能自动重连：{exc}")
        self.settings.set("server_url", base)
        self.settings.set_service_token(token)
        self.settings.set("first_run", False)
        self.log.add("INFO", f"已连接服务器：{base}")
        return {"overview": overview, "server": {"url": base, "connected": True},
                "agent": self.agent_status(), "config": self.config_for_ui()}

    def server_disconnect(self) -> dict:
        self.settings.set_service_token("")
        self.log.add("INFO", "已断开服务器连接（本机 Agent 继续运行）")
        return {"connected": False}

    def server_overview(self) -> dict:
        data = self.router_json("GET", "/internal/settings/overview")
        data["_server_url"] = self.effective_server_url()
        return data

    def server_set_config(self, key: str, value) -> dict:
        return self.router_json("PATCH", "/internal/settings/config", {"key": key, "value": value})

    def server_upsert_agent(self, payload: dict) -> dict:
        return self.router_json("POST", "/internal/settings/agents", payload)

    def server_delete_agent(self, agent_id: str) -> dict:
        from urllib.parse import quote
        return self.router_json("DELETE", "/internal/settings/agents/" + quote(agent_id))

    def server_job_update(self, job_id: str, payload: dict) -> dict:
        from urllib.parse import quote
        return self.router_json("PATCH", "/internal/settings/jobs/" + quote(job_id), payload)

    def server_job_run(self, job_id: str) -> dict:
        from urllib.parse import quote
        return self.router_json("POST", "/internal/settings/jobs/" + quote(job_id) + "/run")

    def server_plugin_action(self, plugin_id: str, action: str) -> dict:
        if action not in ("reload", "enable", "disable"):
            raise ApiError(400, "未知插件操作")
        from urllib.parse import quote
        return self.router_json("POST", f"/internal/settings/plugins/{quote(plugin_id)}/{action}")

    def server_balance(self) -> dict:
        return self.router_json("GET", "/internal/settings/balance")

    def server_logs(self, after: int = 0) -> dict:
        return self.router_json("GET", f"/internal/settings/logs?after={int(after)}")

    def register_self(self) -> dict:
        token = self.settings.service_token
        if not token:
            raise ApiError(401, "请先连接服务器")
        cfg = self.config
        payload = {
            "id": cfg.get("agent_id"),
            "name": cfg.get("name") or cfg.get("agent_id"),
            "description": cfg.get("description") or "本地 Agent",
            "transport": "poll",
            "api_key": cfg.get("agent_key"),
        }
        result = self.server_upsert_agent(payload)
        if not (self.agent and self.agent.snapshot().get("running")) and cfg.get("router"):
            try:
                self.start_agent()
            except Exception as exc:
                self.log.add("WARN", f"注册成功但 Agent 未启动：{exc}")
        self.log.add("INFO", f"已把本机注册到服务器：{cfg.get('agent_id')}")
        return {"result": result, "overview": self.server_overview(),
                "agent": self.agent_status()}

    # ------------------------------------------------------------------
    def logs_since(self, after: int = 0) -> list[dict]:
        return self.log.since(after, 500)

    def clear_logs(self) -> None:
        self.log.clear()

    def autostart_status(self) -> bool:
        return autostart_enabled()

    def set_autostart(self, enabled: bool) -> bool:
        result = set_autostart(bool(enabled))
        self.log.add("INFO", f"开机自启已{'开启' if result else '关闭'}")
        return result

    def open_path(self, kind: str, path: str = "") -> None:
        target: Path | None = None
        if kind == "config":
            target = self.config_path.parent
        elif kind == "data":
            target = self.data_dir
        elif kind == "log":
            target = log_file()
        elif kind == "workspace":
            allowed = {str(Path(item).resolve()) for item in (self.config.get("workspaces") or {}).values()
                       if Path(item).is_dir()}
            candidate = Path(path).resolve()
            if str(candidate) not in allowed:
                raise ApiError(400, "只能打开已配置的工作区")
            target = candidate
        if target is None:
            raise ApiError(400, "未知路径类型")
        if not target.exists():
            raise ApiError(404, f"路径不存在：{target}")
        os.startfile(str(target))  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    def bootstrap(self) -> dict:
        return {
            "app": {"name": APP_NAME, "version": VERSION, "pid": os.getpid(),
                    "started_at": int(self.started_at), "headless": self.headless,
                    "window_mode": self.window_mode},
            "config": self.config_for_ui(),
            "config_path": str(self.config_path),
            "data_dir": str(self.data_dir),
            "log_path": str(log_file()),
            "agent": self.agent_status(),
            "router": self.router_health(timeout=2.5),
            "server": {"url": self.effective_server_url(),
                       "connected": self.settings.has_service_token()},
            "autostart": self.autostart_status(),
            "paths": {"config": str(self.config_path), "data": str(self.data_dir),
                      "log": str(log_file())},
        }

    def status(self) -> dict:
        return {"time": int(time.time()), "agent": self.agent_status(),
                "router": self.router_health(timeout=1.5),
                "server_connected": self.settings.has_service_token()}

    def touch_ui(self) -> None:
        self.last_ui_seen = time.time()

    def enable_ui_watchdog(self, timeout: float = 180) -> None:
        """Exit if a browser/Edge-hosted UI disappears (Edge cannot tell us directly)."""
        if self.headless:
            return
        self.last_ui_seen = time.time()

        def watch() -> None:
            while not self.stop_event.wait(5):
                if time.time() - self.last_ui_seen > timeout:
                    self.log.add("WARN", "控制台界面已断开，桌面进程退出")
                    self.shutdown()
                    if self.on_quit:
                        try:
                            self.on_quit()
                        except Exception:
                            pass
                    return

        threading.Thread(target=watch, name="ui-watchdog", daemon=True).start()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutting_down:
                return
            self._shutting_down = True
        self.log.add("INFO", "桌面控制台正在退出")
        self.stop_event.set()
        if self.agent:
            try:
                self.agent.stop()
            except Exception:
                pass
        if self.server:
            try:
                self.server.stop()
            except Exception:
                pass
        from instance import clear_state
        clear_state(state_file(), os.getpid())
