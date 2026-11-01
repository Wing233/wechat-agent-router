"""Owner toolbox: execution, patches, snapshots, memory, HITL and integrations.

Everything here is owner-only (not in GUEST_TOOLS). Heavy runtimes that do not
fit the 2C2G router are exposed as optional sidecar interfaces:
- browser_fetch_page / browser_click_fill -> browser.service_url
- run_sandbox is a resource-limited subprocess, not a Docker daemon sandbox.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import governance
except ImportError:  # pragma: no cover - keeps the plugin importable standalone
    governance = None

from executor_client import ExecutorClient

CST = timezone.utc
MAX_OUTPUT = 8000
MAX_PATCH = 64 * 1024
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
ALLOWED_ROOTS = (Path("/app"), Path("/lyph-src"), Path("/workspace"))
RESTORE_ROOTS = (Path("/lyph-src"), Path("/workspace"))
SNAPSHOT_DIR = Path("/data/snapshots")
SNAPSHOT_EXCLUDES = {".git", "node_modules", ".next", "__pycache__", "data",
                     "snapshots", ".venv", "dist", "build"}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _text(value, limit: int = MAX_OUTPUT) -> str:
    text = str(value or "")
    return text[-limit:] if len(text) > limit else text


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _safe_root(raw: str, roots=ALLOWED_ROOTS) -> Path:
    path = Path(str(raw or "")).resolve(strict=False)
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError("路径不在允许范围：" + "、".join(str(item) for item in roots))
    return path


def _code() -> str:
    return "".join(CODE_ALPHABET[secrets.randbelow(len(CODE_ALPHABET))] for _ in range(4))


class Toolbox:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = ctx.resource("store")
        self.owner = ctx.root_config.get("owner", "")
        self.cfg = ctx.root_config.get("toolbox") or ctx.config
        self.db_path = Path(self.cfg.get("db") or "/data/toolbox.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if governance is not None:
            governance.set_mandate_store(self)
        self.executor = ExecutorClient(ctx)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory(
              key TEXT PRIMARY KEY, content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skills(
              name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
              content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS confirmations(
              code TEXT PRIMARY KEY, action TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
              decided_at TEXT NOT NULL DEFAULT '');
            """)
            columns = {row["name"] for row in conn.execute(
                "PRAGMA table_info(confirmations)")}
            migrations = {
                "decided_at": "TEXT NOT NULL DEFAULT ''",
                "tool": "TEXT NOT NULL DEFAULT ''",
                "scope_json": "TEXT NOT NULL DEFAULT '{}'",
                "policy_version": "TEXT NOT NULL DEFAULT ''",
                "approved_by": "TEXT NOT NULL DEFAULT ''",
                "strength": "TEXT NOT NULL DEFAULT 'explicit'",
                "expires_at": "TEXT NOT NULL DEFAULT ''",
                "revoked_at": "TEXT NOT NULL DEFAULT ''",
                "used_at": "TEXT NOT NULL DEFAULT ''",
                "max_uses": "INTEGER NOT NULL DEFAULT 1",
                "uses": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, ddl in migrations.items():
                if name in columns:
                    continue
                try:
                    conn.execute(
                        f"ALTER TABLE confirmations ADD COLUMN {name} {ddl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

    # --- memory / skills ------------------------------------------------
    def memory_search(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()[:200]
        limit = max(1, min(int(args.get("limit") or 5), 20))
        if not query:
            return {"error": "query 不能为空"}
        pattern = f"%{query.lower()}%"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key,content,tags,updated_at FROM memory "
                "WHERE lower(key||' '||content||' '||tags) LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?", (pattern, limit)).fetchall()
        return {"query": query, "items": [dict(row) for row in rows]}

    def memory_update(self, args: dict) -> dict:
        key = str(args.get("key") or "").strip()[:120]
        if not key:
            return {"error": "key 不能为空"}
        if str(args.get("action") or "upsert") == "delete":
            with self._conn() as conn:
                conn.execute("DELETE FROM memory WHERE key=?", (key,))
            return {"deleted": key}
        content = str(args.get("content") or "").strip()[:8000]
        if not content:
            return {"error": "content 不能为空"}
        tags = str(args.get("tags") or "").strip()[:200]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO memory(key,content,tags,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET content=excluded.content,"
                "tags=excluded.tags,updated_at=excluded.updated_at",
                (key, content, tags, _now()))
        return {"ok": True, "key": key}

    def load_skill(self, args: dict) -> dict:
        name = str(args.get("name") or "").strip()[:120]
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
            if row:
                return {"skill": dict(row)}
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM skills ORDER BY updated_at DESC LIMIT 20")]
        return {"error": f"没有技能 {name}", "available": names}

    def learn_skill(self, args: dict) -> dict:
        name = str(args.get("name") or "").strip()[:120]
        content = str(args.get("content") or "").strip()[:20000]
        if not name or not content:
            return {"error": "name 和 content 不能为空"}
        description = str(args.get("description") or "").strip()[:500]
        tags = str(args.get("tags") or "").strip()[:200]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO skills(name,description,content,tags,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description,"
                "content=excluded.content,tags=excluded.tags,updated_at=excluded.updated_at",
                (name, description, content, tags, _now()))
        return {"ok": True, "name": name}

    # --- execution ------------------------------------------------------
    def python_repl(self, args: dict) -> dict:
        code = str(args.get("code") or "")
        if not code.strip():
            return {"error": "code 不能为空"}
        timeout = max(3, min(int(args.get("timeout") or 30), 120))
        if self.executor.enabled:
            try:
                return self.executor.python(code, timeout)
            except Exception as exc:
                return {"error": f"隔离执行器不可用：{exc}"}
        cwd = Path("/workspace")
        if not cwd.is_dir():
            cwd = Path("/tmp")
        try:
            proc = subprocess.run(
                [sys.executable, "-"], input=code, cwd=str(cwd),
                capture_output=True, text=True, errors="replace",
                timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired as exc:
            return {"error": f"执行超时（{timeout}s）", "stdout": _text(exc.stdout),
                    "stderr": _text(exc.stderr)}
        return {"exit_code": proc.returncode, "stdout": _text(proc.stdout),
                "stderr": _text(proc.stderr), "cwd": str(cwd)}

    def run_sandbox(self, args: dict) -> dict:
        command = str(args.get("command") or "").strip()[:2000]
        if not command:
            return {"error": "command 不能为空"}
        timeout = max(3, min(int(args.get("timeout") or 60), 120))
        if self.executor.enabled:
            try:
                return self.executor.shell(command, timeout, sandbox=True)
            except Exception as exc:
                return {"error": f"隔离执行器不可用：{exc}"}
        base = Path("/workspace")
        if not base.is_dir():
            base = Path("/tmp")
        workdir = Path(tempfile.mkdtemp(prefix="sandbox-", dir=str(base)))
        try:
            proc = subprocess.Popen(
                ["bash", "-lc", command], cwd=str(workdir),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", start_new_session=True,
                env={**os.environ, "HOME": str(workdir), "PYTHONIOENCODING": "utf-8"},
            )
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    proc.kill()
                out, err = proc.communicate()
                return {"error": f"沙箱命令超时（{timeout}s）", "stdout": _text(out),
                        "stderr": _text(err), "cwd": str(workdir)}
            return {"exit_code": proc.returncode, "stdout": _text(out),
                    "stderr": _text(err), "cwd": str(workdir)}
        finally:
            # Keep the working directory for inspection, but cap it at 50MB.
            try:
                total = sum(p.stat().st_size for p in workdir.rglob("*") if p.is_file())
                if total > 50 * 1024 * 1024:
                    shutil.rmtree(workdir, ignore_errors=True)
            except OSError:
                pass

    # --- patches / snapshots -------------------------------------------
    def write_patch(self, args: dict) -> dict:
        patch = str(args.get("patch") or "")
        if not patch.strip():
            return {"error": "patch 不能为空"}
        if len(patch.encode("utf-8")) > MAX_PATCH:
            return {"error": "patch 超过 64KB"}
        if self.executor.enabled:
            cwd = str(args.get("cwd") or "/workspace").strip()
            if not (cwd == "/workspace" or cwd.startswith("/workspace/")):
                return {"error": "隔离模式只允许 /workspace"}
            try:
                return self.executor.patch(patch, cwd)
            except Exception as exc:
                return {"error": f"隔离执行器不可用：{exc}"}
        cwd = _safe_root(args.get("cwd") or "/app")
        if not cwd.is_dir():
            return {"error": f"目录不存在：{cwd}"}
        tmp = Path(tempfile.mkstemp(prefix="router-", suffix=".patch")[1])
        tmp.write_text(patch, encoding="utf-8")
        try:
            git = shutil.which("git")
            if git:
                check = subprocess.run([git, "apply", "--check", str(tmp)], cwd=str(cwd),
                                       capture_output=True, text=True, timeout=20)
                if check.returncode == 0:
                    apply = subprocess.run([git, "apply", str(tmp)], cwd=str(cwd),
                                           capture_output=True, text=True, timeout=30)
                    if apply.returncode != 0:
                        return {"error": f"git apply 失败：{apply.stderr[-800:]}"}
                    return {"ok": True, "cwd": str(cwd), "method": "git apply"}
                check_error = check.stderr[-800:]
            else:
                check_error = "git 不可用"
            patch_bin = shutil.which("patch")
            if patch_bin:
                apply = subprocess.run([patch_bin, "-p1", "--batch", "--forward", "-i", str(tmp)],
                                       cwd=str(cwd), capture_output=True, text=True, timeout=30)
                if apply.returncode == 0:
                    return {"ok": True, "cwd": str(cwd), "method": "patch"}
                return {"error": f"patch 失败：{apply.stderr[-800:] or apply.stdout[-800:]}"}
            return {"error": f"补丁无法应用：{check_error}"}
        finally:
            tmp.unlink(missing_ok=True)

    def _snapshot_path(self, name: str) -> Path:
        safe = Path(name).name
        if not safe.endswith(".tar.gz") or safe.startswith("."):
            raise ValueError("快照文件名无效")
        return SNAPSHOT_DIR / safe

    def snapshot(self, args: dict) -> dict:
        action = str(args.get("action") or "create").lower()
        if self.executor.enabled:
            return {"error": "隔离模式暂不支持快照；请在 host 模式或使用 /workspace 文件工具"}
        if action == "list":
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            items = []
            for path in sorted(SNAPSHOT_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                items.append({"id": path.name, "size": path.stat().st_size,
                              "created_at": datetime.fromtimestamp(path.stat().st_mtime, CST).isoformat(timespec="seconds")})
            return {"items": items}
        path = _safe_root(args.get("path") or "/workspace")
        if action == "restore":
            if str(args.get("confirm") or "").lower() not in {"1", "true", "yes"}:
                return {"error": "恢复会覆盖文件，必须传 confirm=true"}
            if not any(path == root or root in path.parents for root in RESTORE_ROOTS):
                return {"error": "只允许恢复到 /lyph-src 或 /workspace"}
            source = self._snapshot_path(str(args.get("snapshot_id") or ""))
            if not source.is_file():
                return {"error": "快照不存在"}
            with tarfile.open(source, "r:gz") as tar:
                for member in tar.getmembers():
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        return {"error": "快照包含非法路径"}
                tar.extractall(path)
            return {"ok": True, "restored": source.name, "path": str(path)}
        if action != "create":
            return {"error": "action 只能是 create/list/restore"}
        if not path.is_dir():
            return {"error": f"目录不存在：{path}"}
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"snap-{datetime.now(CST).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.tar.gz"
        target = SNAPSHOT_DIR / name

        def keep(info: tarfile.TarInfo):
            parts = set(Path(info.name).parts)
            return None if parts & SNAPSHOT_EXCLUDES else info

        with tarfile.open(target, "w:gz") as tar:
            tar.add(path, arcname=".", filter=keep)
        if target.stat().st_size > MAX_SNAPSHOT_BYTES:
            target.unlink(missing_ok=True)
            return {"error": "快照超过 200MB，已放弃"}
        return {"ok": True, "snapshot_id": name, "size": target.stat().st_size}

    # --- human confirmation / mandates ---------------------------------
    def _policy_version(self) -> str:
        return governance.POLICY_VERSION if governance is not None else "1"

    def _mandate_row(self, code: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def _refresh_mandate(self, row: dict | None) -> dict | None:
        if not row:
            return row
        raw = str(row.get("expires_at") or "")
        expired = True
        if raw:
            try:
                expired = datetime.fromisoformat(raw) <= datetime.now(CST)
            except ValueError:
                expired = True
        if expired and row.get("status") in {"pending", "approved"}:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE confirmations SET status='expired',updated_at=? WHERE code=?",
                    (_now(), row["code"]))
            row = dict(row)
            row["status"] = "expired"
        return row

    @staticmethod
    def _tool_matches(approved: str, actual: str) -> bool:
        approved = str(approved or "")
        actual = str(actual or "")
        if not approved or not actual:
            return False
        # ask_confirmation normalizes aliases to the registered full name.
        # A mandate that still has a bare name must not match another plugin.
        return approved == actual

    @staticmethod
    def _paths_allowed(scope_paths: list[str], args: dict) -> bool:
        roots = [Path(item).resolve(strict=False) for item in scope_paths if item]
        if not roots:
            return True
        candidates = []
        for key in ("path", "cwd", "target", "source", "src", "dst",
                    "dest", "destination"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(Path(value).resolve(strict=False))
        if not candidates:
            return False
        return all(
            any(item == root or root in item.parents for root in roots)
            for item in candidates
        )

    def ask_confirmation(self, args: dict) -> dict:
        action = str(args.get("action") or "").strip()[:500]
        summary = str(args.get("summary") or "").strip()[:1000]
        if not action:
            return {"error": "action 不能为空"}
        tool = str(args.get("tool") or "").strip()[:120]
        if tool:
            tools = getattr(self.ctx, "tools", None)
            registry = getattr(tools, "registry", None)
            if registry is not None:
                canonical = registry.canonical(tool)
                if canonical not in registry.names():
                    return {"error": f"未知工具：{tool}"}
                tool = canonical
        raw_args = args.get("args")
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                return {"error": "args 必须是 JSON 对象"}
        if raw_args is None:
            raw_args = {}
        if not isinstance(raw_args, dict):
            return {"error": "args 必须是对象"}
        if raw_args and len(json.dumps(raw_args, ensure_ascii=False, default=str)) > 8192:
            return {"error": "args 太大，无法绑定审批"}
        if (tool and governance is not None
                and governance.mandate_required(tool, raw_args)
                and not raw_args):
            return {"error": "高危工具的审批必须传 args，不能空白授权"}
        ttl_minutes = max(1, min(int(args.get("ttl_minutes") or 30), 1440))
        max_uses = max(1, min(int(args.get("max_uses") or 1), 100))
        paths = args.get("paths") if isinstance(args.get("paths"), list) else []
        agent_id = str(args.get("agent_id") or "").strip()[:120]
        scope = {
            "tool": tool,
            "args_hash": governance.args_hash(raw_args) if governance else "",
            "args_preview": json.dumps(raw_args, ensure_ascii=False, default=str)[:8192],
            "paths": [str(item)[:500] for item in paths[:10]],
            "agent_id": agent_id,
            "max_uses": max_uses,
            "policy_version": self._policy_version(),
        }
        expires_at = (datetime.now(CST) + timedelta(minutes=ttl_minutes)).isoformat(
            timespec="seconds")
        with self._conn() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM confirmations "
                "WHERE status='pending' AND expires_at>?", (_now(),)).fetchone()[0]
        if pending_count >= 5:
            return {"error": "待审批数量过多，请先处理现有审批"}
        code = _code()
        for _ in range(30):
            with self._conn() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM confirmations WHERE code=?", (code,)).fetchone()
            if not exists and not self.store.by_code(code):
                break
            code = _code()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO confirmations
                   (code,action,summary,status,created_at,decided_at,tool,scope_json,
                    policy_version,approved_by,strength,expires_at,revoked_at,used_at,
                    max_uses,uses,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, action, summary, "pending", _now(), "", tool,
                 json.dumps(scope, ensure_ascii=False), self._policy_version(),
                 "", "explicit", expires_at, "", "", max_uses, 0, _now()))
        text = (f"需要确认：{summary or action}\n"
                f"工具：{tool or '未绑定（不能用于高危工具）'}\n")
        if raw_args:
            preview = scope["args_preview"]
            if len(preview) <= 1200:
                text += f"参数：{preview}\n"
            else:
                text += (f"参数（共 {len(preview)} 字符，显示头尾）：\n"
                         f"{preview[:800]}\n...（中间省略）...\n{preview[-400:]}\n")
        if scope["paths"]:
            text += f"路径：{', '.join(scope['paths'])}\n"
        if scope["agent_id"]:
            text += f"Agent：{scope['agent_id']}\n"
        text += (f"回复 APPROVE {code} 批准，REJECT {code} 取消\n"
                 f"有效期 {ttl_minutes} 分钟，最多执行 {max_uses} 次")
        self.store.create_notification(
            agent_id="toolbox", chat_id=self.owner, text=text, code=code,
            expect_reply=True, ttl_seconds=ttl_minutes * 60,
            meta={"action": action, "summary": summary, "tool": tool,
                  "args_hash": scope["args_hash"], "expires_at": expires_at,
                  "max_uses": max_uses, "policy_version": self._policy_version()},
        )
        self.ctx.send(text)
        result = {"pending": True, "code": code, "tool": tool,
                  "expires_at": expires_at, "max_uses": max_uses,
                  "message": "已发送确认请求，等待主人回复 APPROVE/REJECT"}
        if not tool:
            result["warning"] = "未绑定 tool，审批后不能执行高危工具；请重新调用并传 tool 和 args"
        return result

    def session_grant(self, preset: str, minutes: int = 60,
                      approved_by: str = "") -> dict:
        spec = (governance.SESSION_PRESETS.get(preset)
                if governance is not None else None)
        if not spec:
            return {"error": f"未知授权类型：{preset}"}
        minutes = max(1, min(int(minutes or 60), 480))
        max_uses = min(2000, max(100, minutes * 20))
        expires_at = (datetime.now(CST) + timedelta(minutes=minutes)).isoformat(
            timespec="seconds")
        code = _code()
        for _ in range(30):
            with self._conn() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM confirmations WHERE code=?", (code,)).fetchone()
            if not exists:
                break
            code = _code()
        scope = {
            "session": True,
            "preset": preset,
            "label": spec["label"],
            "tools": list(spec["tools"]),
            "paths": list(spec["paths"]),
            "agent_id": "",
            "max_uses": max_uses,
            "policy_version": self._policy_version(),
        }
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO confirmations
                   (code,action,summary,status,created_at,decided_at,tool,scope_json,
                    policy_version,approved_by,strength,expires_at,revoked_at,used_at,
                    max_uses,uses,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, f"会话授权：{spec['label']}",
                 f"{minutes} 分钟，{len(spec['tools'])} 个工具",
                 "approved", _now(), _now(), f"session:{preset}",
                 json.dumps(scope, ensure_ascii=False), self._policy_version(),
                 approved_by or self.owner, "session", expires_at, "", "",
                 max_uses, 0, _now()))
        return {"ok": True, "code": code, "preset": preset,
                "label": spec["label"], "minutes": minutes,
                "tools": list(spec["tools"]), "paths": list(spec["paths"]),
                "max_uses": max_uses, "expires_at": expires_at}

    def revoke_sessions(self) -> dict:
        now = _now()
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE confirmations SET status='revoked',revoked_at=?,updated_at=? "
                "WHERE strength='session' AND status='approved' AND revoked_at=''",
                (now, now))
            return {"ok": True, "revoked": int(cursor.rowcount or 0)}

    def list_sessions(self, limit: int = 10) -> dict:
        limit = max(1, min(int(limit or 10), 50))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM confirmations WHERE strength='session' "
                "AND status='approved' AND revoked_at='' AND used_at='' "
                "AND expires_at>? AND uses<max_uses "
                "ORDER BY created_at DESC LIMIT ?", (_now(), limit)).fetchall()
        items = []
        for row in rows:
            scope = json.loads(row["scope_json"] or "{}")
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                remaining_minutes = max(
                    0, int((expires - datetime.now(CST)).total_seconds() // 60))
            except Exception:
                remaining_minutes = 0
            items.append({
                "code": row["code"],
                "preset": scope.get("preset") or "",
                "label": scope.get("label") or "",
                "expires_at": row["expires_at"],
                "remaining_minutes": remaining_minutes,
                "max_uses": int(row["max_uses"] or 0),
                "uses": int(row["uses"] or 0),
                "remaining": max(0, int(row["max_uses"] or 0)
                                 - int(row["uses"] or 0)),
                "paths": scope.get("paths") or [],
            })
        return {"items": items}

    @staticmethod
    def _has_path_args(args: dict) -> bool:
        return any(
            isinstance(args.get(key), str) and str(args.get(key)).strip()
            for key in ("path", "cwd", "target", "source", "src", "dst",
                        "dest", "destination")
        )

    def _find_session(self, tool: str, args: dict) -> dict | None:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM confirmations WHERE strength='session' "
                "AND status='approved' AND revoked_at='' AND used_at='' "
                "AND expires_at>? AND uses<max_uses "
                "ORDER BY created_at DESC", (_now(),)).fetchall()
        for row in rows:
            item = dict(row)
            scope = json.loads(item.get("scope_json") or "{}")
            tools = scope.get("tools") or []
            if tool not in tools and "*" not in tools:
                continue
            if scope.get("agent_id") and str(args.get("agent_id") or "") != scope["agent_id"]:
                continue
            paths = [str(path) for path in (scope.get("paths") or []) if path]
            if paths and self._has_path_args(args) and not self._paths_allowed(paths, args):
                continue
            return item
        return None

    def _consume_session(self, row: dict, tool: str, args: dict) -> dict:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE confirmations SET uses=uses+1,"
                "used_at=CASE WHEN uses+1>=max_uses THEN ? ELSE '' END,"
                "status=CASE WHEN uses+1>=max_uses THEN 'used' ELSE 'approved' END,"
                "updated_at=? WHERE code=? AND status='approved' "
                "AND strength='session' AND revoked_at='' AND used_at='' "
                "AND uses<max_uses", (_now(), _now(), row["code"]))
            if cursor.rowcount != 1:
                return {"error": "会话授权已被并发使用或已失效",
                        "status": "used"}
        fresh = self._mandate_row(row["code"]) or row
        max_uses = int(fresh.get("max_uses") or 0)
        uses = int(fresh.get("uses") or 0)
        return {"ok": True, "session": True, "code": row["code"],
                "tool": tool, "args": args, "uses": uses,
                "max_uses": max_uses, "remaining": max(0, max_uses - uses)}

    def confirm_action(self, args: dict) -> dict:
        code = str(args.get("code") or "").strip().upper()
        row = self._refresh_mandate(self._mandate_row(code))
        if not row:
            return {"error": "确认码不存在"}
        scope = json.loads(row.get("scope_json") or "{}")
        approved = row["status"] == "approved" and not row.get("used_at")
        result = {
            "code": code,
            "status": row["status"],
            "approved": approved,
            "action": row["action"],
            "summary": row["summary"],
            "tool": row.get("tool") or scope.get("tool") or "",
            "scope": scope,
            "policy_version": row.get("policy_version") or scope.get("policy_version") or "",
            "approved_by": row.get("approved_by") or "",
            "expires_at": row.get("expires_at") or "",
            "revoked_at": row.get("revoked_at") or "",
            "used_at": row.get("used_at") or "",
            "max_uses": int(row.get("max_uses") or 1),
            "uses": int(row.get("uses") or 0),
            "remaining": max(0, int(row.get("max_uses") or 1) - int(row.get("uses") or 0)),
        }
        tool = str(args.get("tool") or result["tool"] or "").strip()
        raw_args = args.get("args") if isinstance(args.get("args"), dict) else {}
        if approved and tool and raw_args:
            binding = self.require_mandate(tool, raw_args, code=code, consume=False)
            result["binding"] = binding
            if binding.get("error"):
                result["approved"] = False
                result["error"] = binding["error"]
        return result

    def require_mandate(self, tool: str, args: dict, code: str = "",
                        consume: bool = True) -> dict:
        args = dict(args or {})
        popped = (args.pop("mandate", "") or args.pop("confirm_code", "")
                  or args.pop("_mandate", ""))
        code = str(code or popped).strip().upper()
        if not code:
            session = self._find_session(tool, args)
            if session:
                return self._consume_session(session, tool, args)
            return {
                "error": "高危工具需要审批码 mandate",
                "required": True,
                "hint": ("先调用 ask_human_confirmation，并传 tool 和 args；"
                         "主人 APPROVE 后把返回的 code 作为 mandate 传入；"
                         "也可以让主人用“开发模式 2 小时”开启会话授权"),
            }
        row = self._refresh_mandate(self._mandate_row(code))
        if not row:
            return {"error": f"审批码不存在：{code}", "status": "missing"}
        status = str(row.get("status") or "")
        if status == "pending":
            return {"error": "审批尚未通过", "status": status}
        if status == "rejected":
            return {"error": "审批已被拒绝", "status": status}
        if status == "expired":
            return {"error": "审批已过期", "status": status}
        if status == "revoked" or row.get("revoked_at"):
            return {"error": "审批已撤销", "status": "revoked"}
        if status == "used" or row.get("used_at"):
            return {"error": "审批已使用", "status": "used"}
        if status != "approved":
            return {"error": f"审批状态不可用：{status}", "status": status}
        scope = json.loads(row.get("scope_json") or "{}")
        scope_tool = str(scope.get("tool") or row.get("tool") or "")
        if not scope_tool:
            return {"error": "审批未绑定工具，请重新审批并传 tool", "status": "unbound"}
        if not self._tool_matches(scope_tool, tool):
            return {"error": f"审批绑定的是 {scope_tool}，不是 {tool}",
                    "status": "tool_changed"}
        approved_policy = str(scope.get("policy_version")
                              or row.get("policy_version") or "")
        if approved_policy and approved_policy != self._policy_version():
            return {"error": "策略版本已变化，请重新审批",
                    "status": "policy_changed"}
        if (governance is not None
                and governance.mandate_required(tool, args)
                and not scope.get("args_hash")):
            return {"error": "审批未绑定参数，不能执行高危工具",
                    "status": "unbound_args"}
        if scope.get("args_hash") and governance is not None:
            if scope["args_hash"] != governance.args_hash(args):
                return {"error": "审批参数与本次执行不一致，请重新审批",
                        "status": "args_changed"}
        if scope.get("agent_id") and str(args.get("agent_id") or "") != scope["agent_id"]:
            return {"error": "审批绑定的 Agent 不一致", "status": "agent_changed"}
        scope_paths = [str(item) for item in (scope.get("paths") or []) if item]
        if scope_paths and not self._paths_allowed(scope_paths, args):
            return {"error": "审批路径不包含本次操作路径",
                    "status": "path_changed"}
        max_uses = int(row.get("max_uses") or 1)
        uses = int(row.get("uses") or 0)
        if uses >= max_uses:
            return {"error": "审批使用次数已用完", "status": "used"}
        if consume:
            with self._conn() as conn:
                cursor = conn.execute(
                    "UPDATE confirmations SET uses=uses+1,"
                    "used_at=CASE WHEN uses+1>=max_uses THEN ? ELSE '' END,"
                    "status=CASE WHEN uses+1>=max_uses THEN 'used' ELSE 'approved' END,"
                    "updated_at=? WHERE code=? AND status='approved' "
                    "AND revoked_at='' AND used_at='' AND uses<max_uses",
                    (_now(), _now(), code))
                if cursor.rowcount != 1:
                    return {"error": "审批已被并发使用或已失效",
                            "status": "used"}
            row = self._mandate_row(code) or row
            uses = int(row.get("uses") or 0)
            max_uses = int(row.get("max_uses") or max_uses)
        return {"ok": True, "code": code, "tool": tool, "args": args,
                "uses": uses, "max_uses": max_uses,
                "remaining": max(0, max_uses - uses)}

    def revoke_mandate(self, args: dict) -> dict:
        code = str(args.get("code") or "").strip().upper()
        row = self._refresh_mandate(self._mandate_row(code))
        if not row:
            return {"error": "确认码不存在"}
        if row.get("status") in {"used", "expired"}:
            return {"error": f"审批状态不可撤销：{row.get('status')}"}
        with self._conn() as conn:
            conn.execute(
                "UPDATE confirmations SET status='revoked',revoked_at=?,updated_at=? "
                "WHERE code=?", (_now(), _now(), code))
        return {"ok": True, "code": code, "status": "revoked"}

    def list_mandates(self, args: dict) -> dict:
        status = str(args.get("status") or "").strip()
        limit = max(1, min(int(args.get("limit") or 20), 100))
        sql = ("SELECT code,action,summary,status,tool,policy_version,strength,"
               "approved_by,created_at,decided_at,expires_at,revoked_at,used_at,"
               "max_uses,uses FROM confirmations")
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params += (limit,)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {"items": [dict(row) for row in rows]}

    def recent_approved(self, limit: int = 3) -> list[dict]:
        limit = max(1, min(int(limit or 3), 20))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT code,action,summary,tool,expires_at,max_uses,uses "
                "FROM confirmations WHERE status='approved' AND used_at='' "
                "AND revoked_at='' AND expires_at>? AND policy_version=? "
                "AND uses<max_uses AND strength!='session' "
                "ORDER BY decided_at DESC LIMIT ?",
                (_now(), self._policy_version(), limit)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["remaining"] = max(0, int(item.get("max_uses") or 1)
                                    - int(item.get("uses") or 0))
            items.append(item)
        return items

    def governance_report(self, args: dict) -> dict:
        kind = str(args.get("kind") or "decisions").strip().lower()
        limit = max(1, min(int(args.get("limit") or 20), 100))
        if kind == "mandates":
            return self.list_mandates({"status": args.get("status") or "",
                                       "limit": limit})
        classification = str(args.get("classification") or "").strip()
        return {
            "items": self.store.list_decisions(limit=limit,
                                               classification=classification),
            "stats": self.store.decision_stats(days=7),
        }

    def handle_reply(self, notification: dict, text: str,
                     approved_by: str = "") -> dict:
        code = str(notification.get("code") or "").upper()
        approval = governance.parse_approval(text) if governance is not None else None
        if approval is None:
            return {"text": "请回复 APPROVE 码 或 REJECT 码。", "final": True}
        if any(word in (text or "").lower() for word in (
                "不要", "别", "先别", "取消", "不执行")):
            return {"text": "审批文本包含否定词，请只回复 APPROVE 码 或 REJECT 码。",
                    "final": True}
        if approval.code != code:
            return {"text": "确认码不匹配，请重新回复。", "final": True}
        status = "approved" if approval.approved else "rejected"
        now = _now()
        warning = ""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE code=?", (code,)).fetchone()
            if row:
                expires_at = row["expires_at"] or (
                    datetime.now(CST) + timedelta(minutes=30)).isoformat(
                        timespec="seconds")
                conn.execute(
                    "UPDATE confirmations SET status=?,decided_at=?,approved_by=?,"
                    "expires_at=?,updated_at=? WHERE code=?",
                    (status, now,
                     approved_by or self.owner or str(notification.get("chat_id") or ""),
                     expires_at, now, code))
                scope = json.loads(row["scope_json"] or "{}")
                if approval.approved and not scope.get("tool"):
                    warning = "（这次审批未绑定工具，不能执行高危工具。）"
        return {"text": ("已批准，可以继续执行。" if approval.approved
                         else "已取消这次操作。") + warning,
                "final": True}

    # --- integrations ---------------------------------------------------
    def _secret(self, name: str) -> str:
        try:
            return str(self.ctx.secret(name) or "")
        except Exception:
            return ""

    def github_api(self, args: dict) -> dict:
        token = self._secret("github_token")
        if not token:
            return {"error": "未配置 GitHub token"}
        method = str(args.get("method") or "GET").upper()
        path = str(args.get("path") or "/").strip()
        if not path.startswith("/") or ".." in path:
            return {"error": "path 必须是以 / 开头的 GitHub API 路径"}
        body = args.get("body")
        data = None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "User-Agent": "wechat-agent-router"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request("https://api.github.com" + path, data=data, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(40000).decode("utf-8", "replace")
                return {"status": response.status, "body": _text(raw)}
        except urllib.error.HTTPError as exc:
            return {"error": f"HTTP {exc.code}", "body": _text(exc.read().decode("utf-8", "replace"))}
        except Exception as exc:
            return {"error": f"GitHub 请求失败：{exc}"}

    @staticmethod
    def _public_webhook(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Webhook 只允许 http/https")
        host = parsed.hostname or ""
        if not host or host == "localhost" or host.endswith(".local"):
            raise ValueError("不允许本机或内网 Webhook")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in (80, 443):
            raise ValueError("Webhook 只允许 80/443 端口")
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise ValueError("不允许内网或保留地址 Webhook")
        return url

    def webhook_post(self, args: dict) -> dict:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"error": "url 不能为空"}
        try:
            self._public_webhook(url)
        except Exception as exc:
            return {"error": str(exc)}
        text = str(args.get("text") or "").strip()[:4000]
        fmt = str(args.get("format") or "auto").lower()
        payload = args.get("payload")
        if payload is not None and not isinstance(payload, dict):
            return {"error": "payload 必须是 JSON 对象"}
        if payload is None:
            if fmt == "discord":
                payload = {"content": text}
            elif fmt == "feishu":
                payload = {"msg_type": "text", "content": {"text": text}}
            elif fmt == "dingtalk":
                payload = {"msgtype": "text", "text": {"content": text}}
            else:
                payload = {"text": text}
        if not payload:
            return {"error": "text 或 payload 不能为空"}
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "wechat-agent-router")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return {"status": response.status, "format": fmt,
                        "body": _text(response.read().decode("utf-8", "replace"), 2000)}
        except urllib.error.HTTPError as exc:
            return {"error": f"HTTP {exc.code}", "body": _text(exc.read().decode("utf-8", "replace"), 1000)}
        except Exception as exc:
            return {"error": f"Webhook 发送失败：{exc}"}

    def slack_post(self, args: dict) -> dict:
        webhook = self._secret("slack_webhook")
        if not webhook:
            return {"error": "未配置 Slack webhook"}
        text = str(args.get("text") or "").strip()[:4000]
        if not text:
            return {"error": "text 不能为空"}
        payload = {"text": text}
        channel = str(args.get("channel") or "").strip()
        if channel:
            payload["channel"] = channel
        req = urllib.request.Request(webhook, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return {"status": response.status, "body": _text(response.read().decode("utf-8", "replace"))}
        except Exception as exc:
            return {"error": f"Slack 发送失败：{exc}"}

    def sql_query(self, args: dict) -> dict:
        name = str(args.get("database") or "router").strip()
        databases = self.cfg.get("sql_databases") or {
            "router": "/data/router.db", "lyph": "/lyph-data/lyph.sqlite3"}
        path = databases.get(name)
        if not path:
            return {"error": f"未配置数据库 {name}", "available": sorted(databases)}
        sql = str(args.get("sql") or "").strip()
        if not re.match(r"^(select|with|pragma\s+table_info)\b", sql, re.I):
            return {"error": "只允许 SELECT / WITH / PRAGMA table_info 只读查询"}
        limit = max(1, min(int(args.get("limit") or 100), 500))
        try:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(sql).fetchmany(limit)
            columns = list(rows[0].keys()) if rows else []
            result = [dict(row) for row in rows]
            conn.close()
            return {"database": name, "columns": columns, "rows": result, "count": len(result)}
        except Exception as exc:
            return {"error": f"SQL 查询失败：{exc}"}

    # --- MCP client -----------------------------------------------------
    def _mcp_server(self, name: str) -> dict:
        servers = self.cfg.get("mcp_servers") or []
        for item in servers:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        raise ValueError(f"未配置 MCP 服务 {name}")

    async def _mcp_run(self, server: dict, tool: str, arguments: dict, list_only: bool):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        headers = {}
        if server.get("token"):
            headers["Authorization"] = "Bearer " + str(server["token"])
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            async with streamable_http_client(server["url"], http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if list_only:
                        result = await session.list_tools()
                        return {"tools": [{"name": item.name, "description": item.description,
                                           "input_schema": item.inputSchema} for item in result.tools]}
                    result = await session.call_tool(tool, arguments or {})
                    return {"is_error": bool(result.isError),
                            "content": [getattr(item, "text", str(item)) for item in (result.content or [])],
                            "structured": result.structuredContent}

    def mcp_list_tools(self, args: dict) -> dict:
        name = str(args.get("server") or "").strip()
        try:
            server = self._mcp_server(name)
            return asyncio.run(self._mcp_run(server, "", {}, True))
        except Exception as exc:
            return {"error": f"MCP 失败：{exc}"}

    def mcp_call(self, args: dict) -> dict:
        name = str(args.get("server") or "").strip()
        tool = str(args.get("tool") or "").strip()
        if not tool:
            return {"error": "tool 不能为空"}
        try:
            server = self._mcp_server(name)
            return asyncio.run(self._mcp_run(server, tool, args.get("arguments") or {}, False))
        except Exception as exc:
            return {"error": f"MCP 失败：{exc}"}

    # --- optional browser sidecar --------------------------------------
    def _browser(self, path: str, payload: dict) -> dict:
        base = str(self.cfg.get("browser_service_url") or "").rstrip("/")
        if not base:
            return {"error": "未配置浏览器侧车 browser_service_url；当前只能用 web_fetch 抓静态页"}
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"error": f"浏览器侧车失败：{exc}"}

    def browser_fetch_page(self, args: dict) -> dict:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"error": "url 不能为空"}
        base = str(self.cfg.get("browser_service_url") or "").rstrip("/")
        if base:
            return self._browser("/fetch", {"url": url, "wait_ms": int(args.get("wait_ms") or 2000)})
        # No local Chromium: use a remote headless renderer and return text only.
        reader = str(self.cfg.get("browser_reader_url") or "https://api.microlink.io/").rstrip("/")
        params = {
            "url": url, "meta": "false",
            "data.content.selector": "body", "data.content.type": "text",
        }
        key = str(self.cfg.get("browser_reader_key") or "").strip()
        if key:
            params["apiKey"] = key
        try:
            request = urllib.request.Request(reader + "?" + urllib.parse.urlencode(params))
            request.add_header("User-Agent", "wechat-agent-router")
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            return {"error": f"远程浏览器读取失败：{exc}"}
        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message") if isinstance(payload, dict) else "invalid response"
            return {"error": f"远程浏览器失败：{message}"}
        raw = str((payload.get("data") or {}).get("content") or "")
        raw = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return {"error": "远程浏览器没有提取到正文"}
        return {"url": url, "provider": "microlink", "text": text[:8000],
                "truncated": len(text) > 8000}

    def browser_click_fill(self, args: dict) -> dict:
        url = str(args.get("url") or "").strip()
        actions = args.get("actions") or []
        if not url or not isinstance(actions, list):
            return {"error": "需要 url 和 actions 数组"}
        return self._browser("/act", {"url": url, "actions": actions[:20]})

    # --- delegation -----------------------------------------------------
    def delegate(self, args: dict) -> dict:
        agents = self.ctx.resource("agents")
        call_agent = self.ctx.resource("call_agent")
        agent_id = str(args.get("agent_id") or "").strip()
        agent = agents.get(agent_id)
        if not agent or agent.get("dispatch") is False:
            return {"error": "未注册或未开启任务派发的 Agent"}
        message = str(args.get("task") or "").strip()
        if not message:
            return {"error": "task 不能为空"}
        try:
            result = call_agent(agent, {
                "notification_id": "", "agent_id": agent_id, "reply": message,
                "meta": {"kind": "task"}, "chat_id": self.owner,
            })
            return {"agent": agent_id, "response": result.get("text") or "",
                    "final": result.get("final", True)}
        except Exception as exc:
            return {"error": f"Agent 调用失败：{exc}"}


def apply(ctx) -> None:
    service = Toolbox(ctx)
    ctx.provide("toolbox", service)

    ctx.tools.register(
        "python_repl",
        "在服务器容器里运行 Python 代码并返回 stdout/stderr，用于计算、调试算法、处理数据。",
        _schema({"code": {"type": "string"}, "timeout": {"type": "integer", "minimum": 3, "maximum": 120}},
                ["code"]),
        service.python_repl, aliases=("python_repl",),
    )
    ctx.tools.register(
        "run_sandbox",
        "在临时工作目录运行一条 bash 命令，带超时和隔离；不是 Docker 守护进程沙箱。",
        _schema({"command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 3, "maximum": 120}},
                ["command"]),
        service.run_sandbox, aliases=("run_sandbox", "run_docker_sandbox"),
    )
    ctx.tools.register(
        "run_docker_sandbox",
        "受限子进程沙箱（超时/临时目录/进程组回收）；当前不是 Docker 守护进程沙箱，真 Docker 需要额外侧车。",
        _schema({"command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 3, "maximum": 120}},
                ["command"]),
        service.run_sandbox, aliases=("run_docker_sandbox",),
    )
    ctx.tools.register(
        "write_patch",
        "把 unified diff 补丁应用到 /app、/lyph-src 或 /workspace；先检查再应用，失败不改文件。",
        _schema({"patch": {"type": "string"}, "cwd": {"type": "string"}}, ["patch"]),
        service.write_patch, aliases=("write_patch",),
    )
    ctx.tools.register(
        "snapshot_checkpoint",
        "创建/列出/恢复目录快照，用于犯错回滚；恢复必须传 confirm=true，且只允许 /lyph-src 或 /workspace。",
        _schema({"action": {"type": "string", "enum": ["create", "list", "restore"]},
                 "path": {"type": "string"}, "snapshot_id": {"type": "string"},
                 "confirm": {"type": "boolean"}}, []),
        service.snapshot, aliases=("snapshot_checkpoint",),
    )
    ctx.tools.register(
        "memory_search",
        "搜索长期记忆：用户偏好、项目约定、以前解决过的问题。回答个人化问题前先搜。",
        _schema({"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                ["query"]),
        service.memory_search, aliases=("memory_search",),
    )
    ctx.tools.register(
        "memory_update",
        "写入或删除长期记忆；key 用稳定短标识，content 写结论而不是整段聊天。",
        _schema({"key": {"type": "string"}, "content": {"type": "string"},
                 "tags": {"type": "string"}, "action": {"type": "string", "enum": ["upsert", "delete"]}},
                ["key"]),
        service.memory_update, aliases=("memory_update",),
    )
    ctx.tools.register(
        "load_skill",
        "按名称加载已固化的技能/工作流。",
        _schema({"name": {"type": "string"}}, ["name"]),
        service.load_skill, aliases=("load_skill",),
    )
    ctx.tools.register(
        "learn_skill",
        "把成功解决的问题固化成可复用技能（名称、描述、步骤/代码、标签）。",
        _schema({"name": {"type": "string"}, "description": {"type": "string"},
                 "content": {"type": "string"}, "tags": {"type": "string"}},
                ["name", "content"]),
        service.learn_skill, aliases=("learn_skill",),
    )
    ctx.tools.register(
        "ask_human_confirmation",
        "高危操作前请求主人确认；建议传 tool 和 args 绑定本次动作。返回 code，等主人回复 APPROVE 后再执行。",
        _schema({
            "action": {"type": "string"},
            "summary": {"type": "string"},
            "tool": {"type": "string", "description": "将要执行的高危工具名，例如 toolbox__python_repl"},
            "args": {"type": "object", "description": "将要传给该工具的参数（不含 mandate）"},
            "ttl_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "max_uses": {"type": "integer", "minimum": 1, "maximum": 100},
            "paths": {"type": "array", "items": {"type": "string"}},
            "agent_id": {"type": "string"},
        }, ["action"]),
        service.ask_confirmation, aliases=("ask_human_confirmation",),
    )
    ctx.tools.register(
        "confirm_action",
        "检查确认码是否已被主人批准；传 tool/args 可校验审批是否绑定到本次动作。",
        _schema({"code": {"type": "string"}, "tool": {"type": "string"},
                 "args": {"type": "object"}}, ["code"]),
        service.confirm_action, aliases=("confirm_action",),
    )
    ctx.tools.register(
        "revoke_mandate",
        "撤销一个已批准或待批准的确认码，撤销后不能再用。",
        _schema({"code": {"type": "string"}}, ["code"]),
        service.revoke_mandate, aliases=("revoke_mandate",),
    )
    ctx.tools.register(
        "list_mandates",
        "列出审批记录：状态、绑定工具、批准人、过期/撤销/使用情况。",
        _schema({"status": {"type": "string"}, "limit": {"type": "integer",
                                                          "minimum": 1, "maximum": 100}}, []),
        service.list_mandates, aliases=("list_mandates",),
    )
    ctx.tools.register(
        "governance_report",
        "查看最近的入站 gate 决策和审批统计，用于核对“它本来会做什么”。",
        _schema({"kind": {"type": "string", "enum": ["decisions", "mandates"]},
                 "classification": {"type": "string"},
                 "status": {"type": "string"},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
        service.governance_report, aliases=("governance_report",),
    )
    ctx.tools.register(
        "github_api",
        "调用 GitHub REST API（需配置 github_token）；path 例如 /repos/owner/repo/issues。",
        _schema({"method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                 "path": {"type": "string"}, "body": {"type": "object"}}, ["method", "path"]),
        service.github_api, aliases=("github_api",),
    )
    ctx.tools.register(
        "slack_post",
        "向 Slack Incoming Webhook 发消息（需配置 slack_webhook）。",
        _schema({"text": {"type": "string"}, "channel": {"type": "string"}}, ["text"]),
        service.slack_post, aliases=("slack_post",),
    )
    ctx.tools.register(
        "webhook_post",
        "向任意公开 Webhook 发消息：Slack/Discord/飞书/钉钉或自定义 JSON；不能访问内网。",
        _schema({
            "url": {"type": "string"},
            "text": {"type": "string"},
            "format": {"type": "string", "enum": ["auto", "slack", "discord", "feishu", "dingtalk", "raw"]},
            "payload": {"type": "object"},
        }, ["url"]),
        service.webhook_post, aliases=("webhook_post",),
    )
    ctx.tools.register(
        "sql_query",
        "对配置的 SQLite 数据库执行只读 SELECT/WITH 查询；默认 router、lyph。",
        _schema({"database": {"type": "string"}, "sql": {"type": "string"},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 500}},
                ["sql"]),
        service.sql_query, aliases=("sql_query",),
    )
    ctx.tools.register(
        "mcp_list_tools",
        "列出已配置 MCP 服务暴露的工具；需要 mcp_servers 配置。",
        _schema({"server": {"type": "string"}}, ["server"]),
        service.mcp_list_tools, aliases=("mcp_list_tools",),
    )
    ctx.tools.register(
        "mcp_call",
        "调用已配置 MCP 服务的工具。",
        _schema({"server": {"type": "string"}, "tool": {"type": "string"},
                 "arguments": {"type": "object"}}, ["server", "tool"]),
        service.mcp_call, aliases=("mcp_call",),
    )
    ctx.tools.register(
        "browser_fetch_page",
        "抓取需要执行 JS 的页面：优先 browser_service_url 侧车，未配置时用 Microlink 远程渲染；不占本机浏览器内存。",
        _schema({"url": {"type": "string"}, "wait_ms": {"type": "integer"}}, ["url"]),
        service.browser_fetch_page, aliases=("browser_fetch_page",),
    )
    ctx.tools.register(
        "browser_click_fill",
        "用浏览器侧车执行点击/填表动作；未配置 browser_service_url 时返回提示。",
        _schema({"url": {"type": "string"}, "actions": {"type": "array"}}, ["url", "actions"]),
        service.browser_click_fill, aliases=("browser_click_fill",),
    )
    ctx.tools.register(
        "delegate_to_subagent",
        "把任务派给已注册 Agent（如 pc-home、lyph）处理，复杂任务可拆给子 Agent。",
        _schema({"agent_id": {"type": "string"}, "task": {"type": "string"}}, ["agent_id", "task"]),
        service.delegate, aliases=("delegate_to_subagent",),
    )
    ctx.log.info("toolbox registered (memory, exec, patch, snapshot, hitl, integrations)")
