"""Restricted file operations for the owner, with WeChat approval for writes."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ALLOWED_ROOTS = (Path("/app"), Path("/app/plugins"), Path("/workspace"))
DENY_PARTS = {".git", ".env", "config.json", "data", "node_modules", "__pycache__",
              "runtime", "host-before", "host-packages"}
MAX_BYTES = 64 * 1024
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class OpsError(Exception):
    pass


def _code() -> str:
    return "".join(CODE_ALPHABET[b % len(CODE_ALPHABET)] for b in os.urandom(4))


class Ops:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = ctx.resource("store")
        self.owner = ctx.root_config.get("owner", "")

    # ------------------------------------------------------------------
    def _resolve(self, raw: str) -> Path:
        if not raw or not str(raw).startswith("/"):
            raise OpsError("必须使用绝对路径")
        path = Path(raw).resolve(strict=False)
        root = next((item for item in ALLOWED_ROOTS if path == item or item in path.parents), None)
        if root is None:
            raise OpsError("路径不在允许范围：/app、/app/plugins、/workspace")
        if any(part in DENY_PARTS for part in path.parts):
            raise OpsError("该路径属于禁止访问的区域")
        return path

    def _summary(self, path: Path, text: str) -> str:
        return f"{text}：{path}"

    def list_dir(self, raw: str) -> dict:
        path = self._resolve(raw)
        if not path.is_dir():
            raise OpsError("目录不存在")
        items = []
        for entry in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            try:
                size = entry.stat().st_size if entry.is_file() else None
            except OSError:
                size = None
            items.append({"name": entry.name, "type": "dir" if entry.is_dir() else "file", "size": size})
        return {"path": str(path), "items": items[:200]}

    def read(self, raw: str) -> dict:
        path = self._resolve(raw)
        if not path.is_file():
            raise OpsError("文件不存在")
        if path.stat().st_size > MAX_BYTES:
            raise OpsError("文件超过 64KB，请先缩小范围")
        return {"path": str(path), "content": path.read_text(encoding="utf-8", errors="replace"),
                "truncated": False}

    def _propose(self, action: str, path: Path, summary: str, **meta) -> dict:
        code = _code()
        payload = {"action": action, "path": str(path), **meta}
        notification = self.store.create_notification(
            agent_id="ops", chat_id=self.owner, text=summary, code=code,
            expect_reply=True, ttl_seconds=3600, meta=payload,
        )
        self.ctx.send(f"{summary}\n\n回复 APPROVE {code} 执行，REJECT {code} 取消")
        return {"ok": True, "pending": True, "code": code, "path": str(path),
                "notification_id": notification["id"]}

    def write(self, raw: str, content: str) -> dict:
        path = self._resolve(raw)
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise OpsError("内容超过 64KB")
        return self._propose("write", path, f"准备写入 {path}（{len(content)} 字符）", content=content)

    def replace(self, raw: str, old: str, new: str) -> dict:
        path = self._resolve(raw)
        if not path.is_file():
            raise OpsError("文件不存在")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise OpsError(f"待替换文本出现 {count} 次，必须恰好 1 次")
        updated = text.replace(old, new, 1)
        if len(updated.encode("utf-8")) > MAX_BYTES:
            raise OpsError("修改后超过 64KB")
        return self._propose("replace", path, f"准备修改 {path}（替换 1 处）", content=updated)

    def delete(self, raw: str) -> dict:
        path = self._resolve(raw)
        if not path.exists():
            raise OpsError("路径不存在")
        if path.is_dir() and any(path.iterdir()):
            raise OpsError("只能删除空目录或文件")
        return self._propose("delete", path, f"准备删除 {path}")

    def move(self, src: str, dst: str) -> dict:
        source = self._resolve(src)
        target = self._resolve(dst)
        if not source.exists():
            raise OpsError("源路径不存在")
        return self._propose("move", source, f"准备移动 {source} → {target}", dst=str(target))

    # ------------------------------------------------------------------
    def handle_reply(self, notification: dict, text: str) -> dict:
        meta = notification.get("meta") or {}
        if not any(word in text.lower() for word in ("approve", "同意", "执行", "通过")):
            return {"text": "已取消这次文件操作。", "final": True}
        action = meta.get("action")
        path = Path(str(meta.get("path") or ""))
        try:
            if action == "write":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(meta.get("content") or ""), encoding="utf-8")
                result = f"已写入 {path}"
            elif action == "replace":
                path.write_text(str(meta.get("content") or ""), encoding="utf-8")
                result = f"已修改 {path}"
            elif action == "delete":
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()
                result = f"已删除 {path}"
            elif action == "move":
                shutil.move(str(path), str(meta.get("dst") or ""))
                result = f"已移动 {path} → {meta.get('dst')}"
            else:
                return {"text": "未知操作，已取消。", "final": True}
        except Exception as exc:
            self.store.log_plugin_event("ops", "error", f"{action} {path}: {exc}")
            return {"text": f"操作失败：{exc}", "final": True}
        self.store.log_plugin_event("ops", action, str(path))
        return {"text": f"✅ {result}", "final": True}


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _guard(fn):
    def wrapper(args: dict) -> dict:
        try:
            return {"ok": True, "result": fn(args or {})}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}
    return wrapper


def apply(ctx) -> None:
    service = Ops(ctx)
    ctx.provide("ops", service)

    ctx.tools.register(
        "fs_list",
        "列出服务器目录。允许的根：/app、/app/plugins、/workspace；禁止 .git/.env/config.json/data。",
        _schema({"path": {"type": "string"}}, ["path"]),
        _guard(lambda a: service.list_dir(a.get("path", ""))),
        aliases=("fs_list",),
    )
    ctx.tools.register(
        "fs_read",
        "读取服务器文本文件（最大 64KB）。允许的根：/app、/app/plugins、/workspace。",
        _schema({"path": {"type": "string"}}, ["path"]),
        _guard(lambda a: service.read(a.get("path", ""))),
        aliases=("fs_read",),
    )
    ctx.tools.register(
        "fs_write",
        "写入服务器文件（最大 64KB），会先发微信审批。允许的根：/app、/app/plugins、/workspace。",
        _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        _guard(lambda a: service.write(a.get("path", ""), a.get("content", ""))),
        aliases=("fs_write",),
    )
    ctx.tools.register(
        "fs_replace",
        "在服务器文件中替换一段文本（必须恰好出现 1 次），会先发微信审批。",
        _schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                ["path", "old", "new"]),
        _guard(lambda a: service.replace(a.get("path", ""), a.get("old", ""), a.get("new", ""))),
        aliases=("fs_replace",),
    )
    ctx.tools.register(
        "fs_delete",
        "删除服务器文件或空目录，会先发微信审批。",
        _schema({"path": {"type": "string"}}, ["path"]),
        _guard(lambda a: service.delete(a.get("path", ""))),
        aliases=("fs_delete",),
    )
    ctx.tools.register(
        "fs_move",
        "移动服务器文件，会先发微信审批。",
        _schema({"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"]),
        _guard(lambda a: service.move(a.get("src", ""), a.get("dst", ""))),
        aliases=("fs_move",),
    )

    ctx.log.info("ops file service registered")
    ctx.on_unload(lambda: ctx.log.info("ops plugin unloaded"))
