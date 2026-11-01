"""HTTP client for the optional isolated executor sidecar.

The client is deliberately fail-closed: when ``executor.mode == "isolated"`` and
the sidecar is unreachable, callers get an error and must not fall back to the
Router container's own shell.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class ExecutorError(Exception):
    pass


class ExecutorClient:
    def __init__(self, ctx):
        cfg = (ctx.root_config.get("executor") or {})
        self.mode = str(cfg.get("mode") or "host").strip().lower()
        self.url = str(cfg.get("url") or "").strip().rstrip("/")
        try:
            self.token = str(ctx.secret("executor_token") or "")
        except Exception:
            self.token = ""

    @property
    def enabled(self) -> bool:
        return self.mode == "isolated"

    def _post(self, path: str, payload: dict, timeout: int) -> dict:
        if not self.url:
            raise ExecutorError("executor.url 未配置")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url + path, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("X-Executor-Token", self.token)
        try:
            with urllib.request.urlopen(
                    request, timeout=max(5, int(timeout) + 5)) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ExecutorError(f"HTTP {exc.code}: {detail}") from None
        except Exception as exc:
            raise ExecutorError(str(exc)) from None
        try:
            data = json.loads(raw)
        except Exception:
            raise ExecutorError("执行器返回的不是 JSON") from None
        if not isinstance(data, dict):
            raise ExecutorError("执行器返回格式错误")
        return data

    def python(self, code: str, timeout: int = 30) -> dict:
        return self._post(
            "/v1/python", {"code": code, "timeout": int(timeout)}, timeout)

    def shell(self, command: str, timeout: int = 60, *,
              cwd: str = "/workspace", sandbox: bool = False) -> dict:
        return self._post(
            "/v1/shell",
            {"command": command, "timeout": int(timeout),
             "cwd": cwd, "sandbox": bool(sandbox)},
            timeout,
        )
