"""Self-evolution plugin: generate a connector plugin, sandbox-test it, ask for approval."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .generator import SpecError, generate, run_tests

ADMIN = "http://127.0.0.1:9900/internal/plugins"


def _code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(alphabet[b % len(alphabet)] for b in os.urandom(4))


class Evolution:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = ctx.resource("store")
        self.owner = ctx.root_config.get("owner", "")
        self.token = ctx.secret("service_token")

    # ------------------------------------------------------------------
    def propose(self, args: dict) -> dict:
        spec = {
            "id": args.get("id", ""),
            "name": args.get("name", ""),
            "description": args.get("description", ""),
            "tool_name": args.get("tool_name", "run"),
            "tool_description": args.get("tool_description", ""),
            "method": args.get("method", "GET"),
            "url": args.get("url", ""),
            "parameters": args.get("parameters") or {"type": "object", "properties": {}},
            "timeout": args.get("timeout", 10),
        }
        try:
            built = generate(Path("/sandbox"), spec)
        except SpecError as exc:
            return {"ok": False, "error": str(exc)}
        test = run_tests(Path(built["path"]))
        if not test["ok"]:
            return {"ok": False, "error": "沙箱测试失败", "test": test}

        code = _code()
        permissions = ", ".join(built["permissions"])
        text = (
            f"🧩 插件提案：{built['plugin_id']}\n"
            f"工具：{built['plugin_id']}__{built['tool']}\n"
            f"权限：{permissions}\n"
            f"沙箱测试：通过\n"
            f"回复 APPROVE {code} 安装，REJECT {code} 取消"
        )
        notification = self.store.create_notification(
            agent_id="evolution",
            chat_id=self.owner,
            text=text,
            code=code,
            expect_reply=True,
            ttl_seconds=3600,
            meta={"proposal_path": built["path"], "plugin_id": built["plugin_id"]},
        )
        self.ctx.send(text)
        return {"ok": True, "code": code, "plugin_id": built["plugin_id"],
                "notification_id": notification["id"], "test": test}

    # ------------------------------------------------------------------
    def _admin(self, pid: str, action: str) -> tuple[int, dict]:
        request = urllib.request.Request(f"{ADMIN}/{pid}/{action}", data=b"{}", method="POST")
        request.add_header("X-Service-Token", self.token)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {"error": str(exc)}
            return exc.code, payload

    def _activate(self, pid: str) -> tuple[bool, dict]:
        code, payload = self._admin(pid, "enable")
        if code == 409:
            code, payload = self._admin(pid, "reload")
        if code != 200:
            return False, payload
        plugins = {item["id"] for item in payload.get("plugins", [])}
        if pid not in plugins:
            return False, {"error": "插件未出现在已加载列表"}
        return True, payload

    def _git_commit(self, pid: str) -> tuple[bool, str]:
        base = ["git", "-c", "safe.directory=/app", "-C", "/app"]
        commands = [
            base + ["add", f"plugins/{pid}"],
            base + ["-c", "user.name=evolution", "-c", "user.email=evolution@local",
                    "commit", "-m", f"plugin: install {pid}"],
            base + ["push", "origin", "main"],
        ]
        for command in commands:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                output = (proc.stdout + proc.stderr).strip()
                if "nothing to commit" in output or "no changes added" in output:
                    continue
                return False, output[-400:]
        return True, "ok"

    def install(self, meta: dict) -> dict:
        pid = str(meta.get("plugin_id") or "")
        source = Path(str(meta.get("proposal_path") or ""))
        if not pid or not source.is_dir() or Path("/sandbox") not in source.parents:
            return {"text": "提案路径无效，已取消。", "final": True}
        target = Path("/app/plugins") / pid
        backup = Path("/sandbox/.backup") / f"{pid}-{int(time.time())}"
        if target.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        try:
            shutil.copytree(source, target)
            ok, payload = self._activate(pid)
            if not ok:
                raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:500])
        except Exception as exc:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup.exists():
                shutil.copytree(backup, target)
                self._activate(pid)
            return {"text": f"插件安装失败，已回滚：{exc}", "final": True}
        synced, detail = self._git_commit(pid)
        note = "已同步到 git" if synced else f"已安装，但 git 同步失败：{detail}"
        return {"text": f"✅ 插件 {pid} 已安装并热加载。{note}", "final": True}

    def handle_reply(self, notification: dict, text: str) -> dict:
        meta = notification.get("meta") or {}
        if any(word in text.lower() for word in ("approve", "同意", "通过", "安装")):
            return self.install(meta)
        return {"text": "已取消这次插件安装。", "final": True}


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def apply(ctx) -> None:
    service = Evolution(ctx)
    ctx.provide("evolution", service)
    ctx.tools.register(
        "propose_plugin",
        "当用户要求给 bot 增加一个新的 HTTP API 功能时，生成连接器插件提案并发微信审批。必须提供真实的 API 地址、方法和参数，不能编造。",
        _schema({
            "id": {"type": "string", "description": "插件 id，小写字母开头，a-z0-9_"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "tool_name": {"type": "string", "description": "工具名，例如 now"},
            "tool_description": {"type": "string"},
            "method": {"type": "string", "enum": ["GET", "POST"]},
            "url": {"type": "string", "description": "完整 API URL，可含 {param} 占位符"},
            "parameters": {"type": "object", "description": "JSON Schema object"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 60},
        }, ["id", "name", "url", "tool_name"]),
        lambda a: service.propose(a),
        aliases=("propose_plugin",),
    )
    ctx.log.info("evolution service registered")
    ctx.on_unload(lambda: ctx.log.info("evolution plugin unloaded"))
