"""Read-only ncm-cli client used by the music plugin and the local bridge."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable


class MusicError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def extract_json(text: str):
    """Parse the last/outermost JSON value from ncm-cli output.

    ncm-cli occasionally prints a warning before the JSON body (for example a
    token-storage migration notice). It never uses a shell, so this is only a
    formatting tolerance, not a security boundary.
    """
    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    best = None
    for start, char in enumerate(text):
        if char not in "{[":
            continue
        candidate = _balanced(text, start)
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except Exception:
            continue
        if best is None or len(candidate) > best[0]:
            best = (len(candidate), value)
    return best[1] if best else None


def _balanced(text: str, start: int) -> str | None:
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def resolve_binary(binary: str) -> str:
    value = (binary or "ncm-cli").strip()
    found = shutil.which(value)
    if found:
        return found
    path = Path(value).expanduser()
    if path.is_file():
        return str(path)
    raise MusicError(
        "not_installed",
        "未找到 ncm-cli。请先安装 @music163/ncm-cli，或在 plugins.music.bin 配置完整路径。",
    )


def build_command(binary: str, args: list[str]) -> list[str]:
    executable = resolve_binary(binary)
    command = [executable, *args, "--output", "json"]
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        # CreateProcess cannot execute .cmd/.bat directly. list2cmdline keeps
        # each argv token quoted for cmd.exe; no user text is passed through a
        # shell without quoting.
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
                subprocess.list2cmdline(command)]
    return command


def run_ncm_cli(binary: str, args: list[str], timeout: int = 30) -> dict:
    command = build_command(binary, args)
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout)),
            env=env,
        )
    except FileNotFoundError:
        raise MusicError("not_installed", "未找到 ncm-cli 或系统命令解释器。") from None
    except subprocess.TimeoutExpired:
        raise MusicError("timeout", f"ncm-cli 在 {timeout} 秒内没有返回。") from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MusicError(*_friendly_error(detail))

    payload = extract_json(proc.stdout)
    if payload is None:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MusicError("invalid_output", f"ncm-cli 没有返回可解析的 JSON：{detail[-300:]}")
    _raise_for_payload(payload)
    return payload


def _friendly_error(detail: str) -> tuple[str, str]:
    text = (detail or "").strip()
    lowered = text.lower()
    if "api key 未设置" in text or "apikey" in lowered and "未设置" in text:
        return "not_configured", "ncm-cli 还没有配置 API Key，请先运行 ncm-cli configure。"
    if "未登录" in text or "请先登录" in text or "login required" in lowered:
        return "login_required", "ncm-cli 还没有登录，请先运行 ncm-cli login 并扫码授权。"
    if "请求总量超限" in text or "quota" in lowered or "rate limit" in lowered:
        return "quota_exceeded", "网易云开放平台今日调用次数已用尽，请稍后再试。"
    if "not found" in lowered or "404" in text:
        return "not_found", "没有找到对应的音乐资源。"
    return "cli_error", f"ncm-cli 执行失败：{text[-500:] or '未知错误'}"


def _raise_for_payload(payload) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            raise MusicError(str(error.get("code") or "api_error"),
                             str(error.get("message") or "网易云接口返回错误。"))
        raise MusicError("api_error", str(error))
    if payload.get("success") is False:
        raise MusicError("api_error", str(payload.get("message") or "ncm-cli 执行失败。"))
    code = payload.get("code")
    if code is not None:
        try:
            number = int(code)
        except (TypeError, ValueError):
            number = 200
        if number not in (0, 200):
            raise MusicError(str(number), str(payload.get("message") or "网易云接口返回错误。"))


class MusicClient:
    """CLI or HTTP bridge transport for the read-only tool set."""

    def __init__(
        self,
        *,
        transport: str,
        binary: str = "ncm-cli",
        bridge_url: str = "",
        bridge_token: str = "",
        timeout: int = 30,
        http_request: Callable[..., tuple[int, str]] | None = None,
    ):
        self.transport = transport
        self.binary = binary or "ncm-cli"
        self.bridge_url = (bridge_url or "").rstrip("/")
        self.bridge_token = bridge_token or ""
        self.timeout = max(1, int(timeout or 30))
        self._http_request = http_request

    def call(self, args: list[str]) -> dict:
        if self.transport == "cli":
            return run_ncm_cli(self.binary, args, self.timeout)
        if self.transport == "http":
            return self._call_http(args)
        raise MusicError("invalid_config", f"未知的 music transport：{self.transport}")

    def _call_http(self, args: list[str]) -> dict:
        if not self.bridge_url:
            raise MusicError("invalid_config", "music.transport=http 但未配置 bridge_url。")
        if self._http_request is None:
            raise MusicError("invalid_config", "当前插件上下文不支持 HTTP 请求。")
        headers = {"Content-Type": "application/json"}
        if self.bridge_token:
            headers["Authorization"] = f"Bearer {self.bridge_token}"
        body = json.dumps({"args": args}, ensure_ascii=False).encode("utf-8")
        try:
            status, text = self._http_request(
                "POST", f"{self.bridge_url}/run", headers=headers,
                body=body, timeout=self.timeout, limit=500_000,
            )
        except Exception as exc:
            http_code = getattr(exc, "code", None)
            if http_code:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                payload = extract_json(detail)
                message = ""
                if isinstance(payload, dict):
                    error = payload.get("error")
                    message = str(error.get("message") if isinstance(error, dict) else error or "")
                raise MusicError("bridge_error",
                                 message or f"音乐桥接服务返回 HTTP {http_code}。") from None
            raise MusicError("bridge_unreachable", f"音乐桥接服务不可达：{exc}") from None
        payload = extract_json(text)
        if status != 200:
            message = ""
            if isinstance(payload, dict):
                error = payload.get("error")
                message = str(error.get("message") if isinstance(error, dict) else error or "")
            raise MusicError("bridge_error", message or f"音乐桥接服务返回 HTTP {status}。")
        if not isinstance(payload, dict):
            raise MusicError("invalid_output", "音乐桥接服务没有返回 JSON 对象。")
        if not payload.get("ok"):
            error = payload.get("error") or {}
            if isinstance(error, dict):
                raise MusicError(str(error.get("code") or "bridge_error"),
                                 str(error.get("message") or "音乐桥接服务执行失败。"))
            raise MusicError("bridge_error", str(error))
        data = payload.get("data")
        _raise_for_payload(data)
        return data if isinstance(data, dict) else {"data": data}
