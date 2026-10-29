"""Loopback HTTP server that serves the desktop UI and its JSON API."""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from dialogs import pick_file, pick_folder
from errors import ApiError

MAX_BODY = 2 * 1024 * 1024


def _as_int(values: dict, key: str, default: int = 0) -> int:
    try:
        return int((values.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default


class ApiServer:
    def __init__(self, app):
        self.app = app
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.url = ""
        self.ui_url = ""
        self.static: dict[str, tuple[bytes, str]] = {}
        self._load_static()

    def _load_static(self) -> None:
        for name, mime in (("index.html", "text/html; charset=utf-8"),
                           ("app.js", "application/javascript; charset=utf-8"),
                           ("styles.css", "text/css; charset=utf-8"),
                           ("favicon.svg", "image/svg+xml")):
            path = Path(self.app.web_dir) / name
            if path.is_file():
                self.static["/" + name] = (path.read_bytes(), mime)
        if "/index.html" not in self.static:
            raise RuntimeError(f"Web UI not found in {self.app.web_dir}")

    def start(self) -> None:
        handler = _make_handler(self.app, self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}/"
        self.ui_url = self.url + "#t=" + self.app.token
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="desktop-api", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if not self.httpd:
            return
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        self.httpd = None


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WeChatAgentDesktop/1.0"
    api_app = None
    api_server = None

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.api_server.port
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _token_ok(self) -> bool:
        token = self.headers.get("X-Desktop-Token", "")
        return bool(token) and hmac.compare_digest(token, self.api_app.token)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 无效") from None
        if length > MAX_BODY:
            raise ApiError(413, "请求体过大")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "请求体不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _send_bytes(self, status: int, data: bytes, content_type: str,
                    cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            path = "/index.html"
        item = self.api_server.static.get(path)
        if not item:
            raise ApiError(404, "页面不存在")
        data, mime = item
        self._send_bytes(200, data, mime, cache="no-cache")

    def _handle(self) -> None:
        if not self._host_ok():
            self._send_json(403, {"detail": "仅允许本机访问"})
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not path.startswith("/api/"):
            try:
                self._serve_static(path)
            except ApiError as exc:
                self._send_json(exc.status, {"detail": exc.message})
            return
        if not self._token_ok():
            self._send_json(401, {"detail": "本地控制台令牌无效，请从桌面 App 重新打开"})
            return
        self.api_app.touch_ui()
        try:
            body = self._read_body() if self.command in ("POST", "PUT", "PATCH") else {}
            result = self._route(path, query, body)
            if result is None:
                self._send_bytes(204, b"", "text/plain; charset=utf-8")
            else:
                self._send_json(200, result)
        except ApiError as exc:
            self._send_json(exc.status, {"detail": exc.message})
        except ValueError as exc:
            self._send_json(400, {"detail": str(exc)})
        except Exception as exc:
            self.api_app.log.add("ERROR", f"API 异常：{exc}")
            self.api_app.log.add("ERROR", traceback.format_exc()[-3000:])
            self._send_json(500, {"detail": f"内部错误：{exc}"})

    def _route(self, path: str, query: dict, body: dict):
        app = self.api_app
        method = self.command

        def match(pattern: str):
            return re.fullmatch(pattern, path)

        if method == "GET" and path == "/api/ping":
            return {"ok": True, "app": "wechat-agent-desktop", "pid": os.getpid(),
                    "time": int(time.time())}
        if method == "GET" and path == "/api/bootstrap":
            return app.bootstrap()
        if method == "GET" and path == "/api/status":
            return app.status()
        if method == "GET" and path == "/api/config":
            return app.config_for_ui()
        if method == "PUT" and path == "/api/config":
            return app.update_config(body)
        if method == "POST" and path == "/api/agent/start":
            return app.start_agent()
        if method == "POST" and path == "/api/agent/stop":
            return app.stop_agent()
        if method == "POST" and path == "/api/agent/restart":
            return app.restart_agent()
        if method == "POST" and path == "/api/agent/cancel":
            return app.cancel_agent_task()
        if method == "POST" and path == "/api/agent/test-engine":
            return app.test_engine(str(body.get("engine") or ""),
                                   str(body.get("workspace") or ""))
        if method == "GET" and path == "/api/router/test":
            return app.router_health(timeout=5)
        if method == "GET" and path == "/api/logs":
            return {"logs": app.logs_since(_as_int(query, "after", 0))}
        if method == "POST" and path == "/api/logs/clear":
            app.clear_logs()
            return {"ok": True}
        if method == "GET" and path == "/api/dialog/folder":
            initial = (query.get("initial") or [""])[0]
            return {"path": pick_folder(initial)}
        if method == "GET" and path == "/api/dialog/file":
            initial = (query.get("initial") or [""])[0]
            kind = (query.get("kind") or ["exe"])[0]
            return {"path": pick_file(initial, kind)}
        if method == "POST" and path == "/api/open-path":
            app.open_path(str(body.get("kind") or ""), str(body.get("path") or ""))
            return {"ok": True}
        if method == "GET" and path == "/api/autostart":
            return {"enabled": app.autostart_status()}
        if method == "POST" and path == "/api/autostart":
            return {"enabled": app.set_autostart(bool(body.get("enabled")))}
        if method == "POST" and path == "/api/server/connect":
            return app.server_connect(str(body.get("token") or ""), str(body.get("url") or ""))
        if method == "POST" and path == "/api/server/disconnect":
            return app.server_disconnect()
        if method == "GET" and path == "/api/server/overview":
            return app.server_overview()
        if method == "PATCH" and path == "/api/server/config":
            return app.server_set_config(str(body.get("key") or ""), body.get("value"))
        if method == "POST" and path == "/api/server/agents":
            return app.server_upsert_agent(body)
        m = match(r"/api/server/agents/([^/]+)")
        if m and method == "DELETE":
            return app.server_delete_agent(unquote(m.group(1)))
        m = match(r"/api/server/jobs/([^/]+)")
        if m and method == "PATCH":
            return app.server_job_update(unquote(m.group(1)), body)
        m = match(r"/api/server/jobs/([^/]+)/run")
        if m and method == "POST":
            return app.server_job_run(unquote(m.group(1)))
        m = match(r"/api/server/plugins/([^/]+)/(reload|enable|disable)")
        if m and method == "POST":
            return app.server_plugin_action(unquote(m.group(1)), m.group(2))
        if method == "GET" and path == "/api/server/balance":
            return app.server_balance()
        if method == "GET" and path == "/api/server/logs":
            return app.server_logs(_as_int(query, "after", 0))
        if method == "POST" and path == "/api/server/register-self":
            return app.register_self()
        if method == "POST" and path == "/api/quit":
            def quit_later():
                app.shutdown()
                callback = getattr(app, "on_quit", None)
                if callback:
                    try:
                        callback()
                    except Exception:
                        pass
            threading.Thread(target=quit_later, daemon=True).start()
            return {"ok": True}
        raise ApiError(404, "接口不存在")


def _make_handler(app, server):
    class BoundHandler(ApiHandler):
        pass
    BoundHandler.api_app = app
    BoundHandler.api_server = server
    return BoundHandler
