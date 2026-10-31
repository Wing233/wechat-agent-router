"""Owner-only developer tools: shell execution and raw HTTP requests.

These tools are deliberately not part of GUEST_TOOLS, so only the owner can
reach them. They run inside the router container, where /app, /lyph-src,
/workspace and /data are mounted from the host.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import urllib.parse
import urllib.request

DEFAULT_CWD = "/app"
ALLOWED_CWD = ("/app", "/lyph-src", "/workspace", "/data", "/lyph-data", "/tmp")
MAX_OUTPUT = 8000
MAX_COMMAND = 2000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Commands that would damage the container beyond a normal mistake. The owner
# can still do almost anything else, but these need a manual docker rebuild.
DENY_PATTERNS = (
    r"\brm\s+(-[a-zA-Z]*\s+)*/(\s|$|\*)",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
    r"\b(shutdown|reboot|poweroff|halt)\b",
)


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _truncate(text: str, limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    text = text or ""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("只允许 http/https URL")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL 缺少主机名")
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("不允许访问本机或内网地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValueError("只允许 80/443 端口")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"域名解析失败：{exc}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("不允许访问内网、回环或保留地址")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def apply(ctx) -> None:
    store = ctx.resource("store")

    def run_command(args: dict) -> dict:
        command = str(args.get("command") or "").strip()
        if not command:
            return {"error": "命令不能为空"}
        if len(command) > MAX_COMMAND:
            return {"error": f"命令过长（上限 {MAX_COMMAND} 字符）"}
        for pattern in DENY_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return {"error": "这条命令会破坏运行环境，已拒绝。需要重建容器请手动操作。"}
        cwd = str(args.get("cwd") or DEFAULT_CWD).strip()
        if not cwd.startswith("/"):
            return {"error": "cwd 必须是绝对路径"}
        if not any(cwd == root or cwd.startswith(root + "/") for root in ALLOWED_CWD):
            return {"error": "cwd 只允许 " + "、".join(ALLOWED_CWD)}
        if not os.path.isdir(cwd):
            return {"error": f"目录不存在：{cwd}"}
        try:
            timeout = max(3, min(int(args.get("timeout") or 60), 120))
        except (TypeError, ValueError):
            timeout = 60
        env = os.environ.copy()
        env.setdefault("LANG", "C.UTF-8")
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        store.log_plugin_event("devtools", "exec", f"{cwd}: {command[:300]}")
        try:
            proc = subprocess.run(
                ["bash", "-lc", command], cwd=cwd, capture_output=True,
                text=True, errors="replace", timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            return {"error": f"命令超时（{timeout}s）", "stdout": _truncate(out)[0],
                    "stderr": _truncate(err)[0]}
        except Exception as exc:
            return {"error": f"执行失败：{exc}"}
        stdout, cut_out = _truncate(proc.stdout)
        stderr, cut_err = _truncate(proc.stderr)
        return {"exit_code": proc.returncode, "cwd": cwd, "stdout": stdout,
                "stderr": stderr, "truncated": cut_out or cut_err}

    def http_request(args: dict) -> dict:
        method = str(args.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            return {"error": "不支持的 HTTP 方法"}
        url = str(args.get("url") or "").strip()
        if not url:
            return {"error": "URL 不能为空"}
        headers = args.get("headers") or {}
        if not isinstance(headers, dict):
            return {"error": "headers 必须是对象"}
        headers = {str(k): str(v) for k, v in headers.items()}
        body = args.get("body")
        data = None
        if body not in (None, ""):
            if isinstance(body, (dict, list)):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                data = str(body).encode("utf-8")
        headers.setdefault("User-Agent", UA)
        try:
            timeout = max(3, min(int(args.get("timeout") or 30), 60))
        except (TypeError, ValueError):
            timeout = 30
        try:
            _assert_public_url(url)
            request = urllib.request.Request(url, data=data, method=method)
            for key, value in headers.items():
                request.add_header(key, value)
            opener = urllib.request.build_opener(
                _SafeRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            )
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(40000)
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                final_url = response.geturl()
        except Exception as exc:
            return {"error": f"请求失败：{exc}"}
        text = raw.decode("utf-8", "replace")
        text, truncated = _truncate(text)
        store.log_plugin_event("devtools", "http", f"{method} {final_url} -> {status}")
        return {"status": status, "url": final_url, "content_type": content_type,
                "text": text, "truncated": truncated}

    ctx.tools.register(
        "run_command",
        "在服务器容器里执行一条 shell 命令，用于安装依赖、跑测试、git、查看日志、处理文件。"
        "一次只跑一条；执行前先想清楚影响。默认工作目录 /app，可用 /app、/lyph-src、/workspace、/data、/tmp。",
        _schema({
            "command": {"type": "string", "description": "bash 命令，例如 git status 或 pytest -q"},
            "cwd": {"type": "string", "description": "工作目录，默认 /app"},
            "timeout": {"type": "integer", "minimum": 3, "maximum": 120,
                        "description": "超时秒数，默认 60"},
        }, ["command"]),
        run_command, aliases=("run_command", "shell", "exec_command"),
    )
    ctx.tools.register(
        "http_request",
        "直接调用任意公开 HTTP API（GET/POST/PUT/PATCH/DELETE），自定义请求头和 JSON 正文。"
        "用于查接口、触发 webhook、调用第三方服务；不能访问内网地址。",
        _schema({
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]},
            "url": {"type": "string", "description": "http/https 公开 URL"},
            "headers": {"type": "object", "description": "可选请求头"},
            "body": {"description": "可选：字符串或 JSON 对象"},
            "timeout": {"type": "integer", "minimum": 3, "maximum": 60},
        }, ["method", "url"]),
        http_request, aliases=("http_request", "api_call"),
    )
    ctx.log.info("devtools registered (shell + http)")
