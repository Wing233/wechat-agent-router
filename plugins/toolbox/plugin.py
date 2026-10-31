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
from datetime import datetime, timezone
from pathlib import Path

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

    # --- human confirmation --------------------------------------------
    def ask_confirmation(self, args: dict) -> dict:
        action = str(args.get("action") or "").strip()[:500]
        summary = str(args.get("summary") or "").strip()[:1000]
        if not action:
            return {"error": "action 不能为空"}
        code = _code()
        for _ in range(20):
            if not self.store.by_code(code):
                break
            code = _code()
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO confirmations(code,action,summary,status,created_at) "
                         "VALUES(?,?,?,?,?)", (code, action, summary, "pending", _now()))
        text = f"需要确认：{summary or action}\n\n回复 APPROVE {code} 批准，REJECT {code} 取消"
        self.store.create_notification(
            agent_id="toolbox", chat_id=self.owner, text=text, code=code,
            expect_reply=True, ttl_seconds=3600,
            meta={"action": action, "summary": summary},
        )
        self.ctx.send(text)
        return {"pending": True, "code": code, "message": "已发送确认请求，等待主人回复 APPROVE/REJECT"}

    def confirm_action(self, args: dict) -> dict:
        code = str(args.get("code") or "").strip().upper()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM confirmations WHERE code=?", (code,)).fetchone()
        if not row:
            return {"error": "确认码不存在"}
        return {"code": code, "status": row["status"], "approved": row["status"] == "approved",
                "action": row["action"], "summary": row["summary"]}

    def handle_reply(self, notification: dict, text: str) -> dict:
        code = str(notification.get("code") or "").upper()
        approved = any(word in text.lower() for word in ("approve", "同意", "执行", "通过"))
        status = "approved" if approved else "rejected"
        with self._conn() as conn:
            conn.execute("UPDATE confirmations SET status=?,decided_at=? WHERE code=?",
                         (status, _now(), code))
        return {"text": "已批准，可以继续执行。" if approved else "已取消这次操作。", "final": True}

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
        "高危操作前请求主人确认；返回确认码，等主人回复 APPROVE 后再用 confirm_action 检查。",
        _schema({"action": {"type": "string"}, "summary": {"type": "string"}}, ["action"]),
        service.ask_confirmation, aliases=("ask_human_confirmation",),
    )
    ctx.tools.register(
        "confirm_action",
        "检查确认码是否已被主人批准；approved=true 才能继续高危操作。",
        _schema({"code": {"type": "string"}}, ["code"]),
        service.confirm_action, aliases=("confirm_action",),
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
