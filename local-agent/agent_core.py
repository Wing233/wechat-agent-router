"""Core local agent runtime shared by the CLI and the desktop console."""
from __future__ import annotations

import json
import locale
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

VERSION = "1.2.0"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
ENGINE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
WORKSPACE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
TRUE_VALUES = {"1", "true", "yes", "on"}


def default_config() -> dict:
    workspace = str(Path.home() / "wechat-agent-workspace")
    try:
        Path(workspace).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return {
        "router": "",
        "agent_id": "pc-home",
        "agent_key": "pc_" + secrets.token_hex(20),
        "name": "我的电脑",
        "description": "本地 Windows 电脑，可运行 OpenCode / Codex / Claude Code。",
        "default_engine": "opencode",
        "default_workspace": "workspace",
        "workspaces": {"workspace": workspace},
        "engines": {
            "opencode": ["opencode", "run"],
            "codex": ["codex", "exec"],
            "claude": ["claude", "-p"],
        },
        "wait_seconds": 60,
        "task_timeout": 1800,
        "poll_seconds": 2,
    }


def split_command(text: str) -> list[str]:
    """Split a command line while keeping Windows backslashes and quoted groups."""
    tokens: list[str] = []
    buffer: list[str] = []
    quote = ""
    for char in str(text or ""):
        if quote:
            if char == quote:
                quote = ""
            else:
                buffer.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char.isspace():
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
        else:
            buffer.append(char)
    if buffer:
        tokens.append("".join(buffer))
    return tokens


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_config(raw: dict) -> dict:
    """Validate and normalize a config dict, raising ValueError with a readable reason."""
    cfg = dict(raw or {})
    router = str(cfg.get("router") or "").strip().rstrip("/")
    if router and not re.match(r"^https?://[^\s]+$", router):
        raise ValueError("Router 地址必须是 http:// 或 https:// 开头的 URL")
    cfg["router"] = router

    agent_id = str(cfg.get("agent_id") or "").strip()
    if not AGENT_ID_RE.match(agent_id):
        raise ValueError("Agent ID 只能用小写字母、数字、下划线或短横线，且以字母开头")
    cfg["agent_id"] = agent_id
    key = str(cfg.get("agent_key") or "").strip()
    cfg["agent_key"] = key or ("pc_" + secrets.token_hex(20))
    cfg["name"] = str(cfg.get("name") or agent_id).strip()[:80]
    cfg["description"] = str(cfg.get("description") or "").strip()[:500]

    engines: dict[str, list[str]] = {}
    for name, command in (cfg.get("engines") or {}).items():
        name = str(name).strip()
        if not ENGINE_RE.match(name):
            raise ValueError(f"引擎名称不合法：{name}")
        if isinstance(command, str):
            command = split_command(command)
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError(f"引擎 {name} 的命令不能为空")
        parts = [str(part) for part in command if str(part).strip()]
        if not parts:
            raise ValueError(f"引擎 {name} 的命令不能为空")
        engines[name] = parts
    if not engines:
        raise ValueError("至少配置一个引擎")
    cfg["engines"] = engines

    workspaces: dict[str, str] = {}
    for alias, path in (cfg.get("workspaces") or {}).items():
        alias = str(alias).strip()
        if not WORKSPACE_RE.match(alias):
            raise ValueError(f"工作区别名不合法：{alias}")
        text = str(path or "").strip()
        if not text:
            raise ValueError(f"工作区 {alias} 的路径不能为空")
        expanded = os.path.expandvars(os.path.expanduser(text))
        if not os.path.isabs(expanded):
            raise ValueError(f"工作区 {alias} 必须使用绝对路径")
        workspaces[alias] = os.path.normpath(expanded)
    if not workspaces:
        raise ValueError("至少配置一个工作区")
    cfg["workspaces"] = workspaces

    default_engine = str(cfg.get("default_engine") or "").strip()
    cfg["default_engine"] = default_engine if default_engine in engines else next(iter(engines))
    default_workspace = str(cfg.get("default_workspace") or "").strip()
    cfg["default_workspace"] = default_workspace if default_workspace in workspaces else next(iter(workspaces))

    wait_seconds = _as_int(cfg.get("wait_seconds"), 60)
    if wait_seconds < 5 or wait_seconds > 600:
        raise ValueError("wait_seconds 应在 5–600 秒之间")
    task_timeout = _as_int(cfg.get("task_timeout"), 1800)
    if task_timeout < 60 or task_timeout > 86400:
        raise ValueError("task_timeout 应在 60–86400 秒之间")
    poll_seconds = _as_int(cfg.get("poll_seconds"), 2)
    if poll_seconds < 1 or poll_seconds > 60:
        raise ValueError("poll_seconds 应在 1–60 秒之间")
    cfg["wait_seconds"] = wait_seconds
    cfg["task_timeout"] = task_timeout
    cfg["poll_seconds"] = poll_seconds
    return cfg


def load_config(path: Path, create: bool = True) -> dict:
    path = Path(path)
    if not path.is_file():
        if not create:
            raise FileNotFoundError(path)
        cfg = normalize_config(default_config())
        save_config(path, cfg)
        return cfg
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return normalize_config(raw)


def save_config(path: Path, config: dict) -> dict:
    path = Path(path)
    cfg = normalize_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    return cfg


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def decode_output(raw: bytes) -> str:
    if not raw:
        return ""
    encodings = ["utf-8", "gb18030", locale.getpreferredencoding(False) or "utf-8"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def clean_output(text: str) -> str:
    text = ANSI_RE.sub("", text or "")
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


class EventLog:
    """Thread-safe bounded log with optional file persistence."""

    def __init__(self, capacity: int = 600, file_path: Path | None = None,
                 printer=None, source: str = "agent"):
        self.items: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._seq = 0
        self.file_path = Path(file_path) if file_path else None
        self.printer = printer
        self.source = source
        if self.file_path:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, level: str, message: str, source: str | None = None) -> dict:
        now = time.time()
        record = {
            "id": 0,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "level": str(level or "INFO").upper(),
            "source": source or self.source,
            "message": str(message or "")[:6000],
        }
        with self._lock:
            self._seq += 1
            record["id"] = self._seq
            self.items.append(record)
            if self.file_path:
                self._append_file(record)
        if self.printer:
            try:
                self.printer(record)
            except Exception:
                pass
        return record

    def _append_file(self, record: dict) -> None:
        try:
            if self.file_path.stat().st_size > 2 * 1024 * 1024:
                backup = self.file_path.with_suffix(self.file_path.suffix + ".1")
                if backup.exists():
                    backup.unlink()
                self.file_path.replace(backup)
        except OSError:
            pass
        try:
            with open(self.file_path, "a", encoding="utf-8") as handle:
                handle.write(f"{record['ts']} [{record['level']}] {record['message']}\n")
        except OSError:
            pass

    def since(self, after: int = 0, limit: int = 300) -> list[dict]:
        with self._lock:
            return [item for item in self.items if item["id"] > after][-max(1, limit):]

    def clear(self) -> None:
        with self._lock:
            self.items.clear()


class LocalAgent:
    """Outbound-polling agent that executes local coding CLIs."""

    def __init__(self, config: dict, log: EventLog | None = None):
        self.config = normalize_config(config)
        self.log = log or EventLog()
        self.router = self.config["router"]
        self.agent_id = self.config["agent_id"]
        self.key = self.config["agent_key"]
        self.engines = self.config["engines"]
        self.workspaces = self.config["workspaces"]
        self.default_engine = self.config["default_engine"]
        self.default_workspace = self.config["default_workspace"]
        self.wait_seconds = self.config["wait_seconds"]
        self.task_timeout = self.config["task_timeout"]
        self.poll_seconds = self.config["poll_seconds"]
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._status = "stopped"
        self._last_error = ""
        self._last_seen = 0.0
        self._started_at = 0.0
        self._current: dict | None = None
        self._tasks_done = 0
        self._last_task: dict | None = None

    # ------------------------------------------------------------------
    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            if not self.router:
                raise ValueError("请先填写 Router 地址")
            self._stop.clear()
            self._status = "connecting"
            self._last_error = ""
            self._started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="local-agent", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 8) -> bool:
        self._stop.set()
        self.cancel()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None
            self._current = None
            self._status = "stopped"
        self.log.add("INFO", "本地 Agent 已停止")
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "running": bool(self._thread and self._thread.is_alive()),
                "agent_id": self.agent_id,
                "router": self.router,
                "last_error": self._last_error,
                "last_seen": int(self._last_seen) if self._last_seen else None,
                "started_at": int(self._started_at) if self._started_at else None,
                "current": dict(self._current) if self._current else None,
                "tasks_done": self._tasks_done,
                "last_task": dict(self._last_task) if self._last_task else None,
                "engines": sorted(self.engines),
                "workspaces": sorted(self.workspaces),
                "default_engine": self.default_engine,
                "default_workspace": self.default_workspace,
                "wait_seconds": self.wait_seconds,
                "poll_seconds": self.poll_seconds,
            }

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None, timeout: int = 30):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.router + path, data=data, method=method)
        request.add_header("X-Agent-Key", self.key)
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", f"wechat-local-agent/{VERSION}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status == 204:
                    return None
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return None
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 Router：{exc.reason}") from None
        except TimeoutError:
            raise RuntimeError("连接 Router 超时") from None

    def register(self) -> None:
        self._request("POST", "/agent/register", {
            "version": VERSION,
            "name": self.config.get("name", ""),
            "description": self.config.get("description", ""),
            "platform": sys.platform,
            "engines": sorted(self.engines),
            "workspaces": sorted(self.workspaces),
            "default_engine": self.default_engine,
            "default_workspace": self.default_workspace,
        })
        with self._lock:
            self._last_seen = time.time()
            self._last_error = ""

    def resolve(self, task: dict) -> tuple[str, str]:
        engine = str(task.get("engine") or self.default_engine)
        if engine not in self.engines:
            raise ValueError(f"引擎不可用：{engine}；可用：{', '.join(sorted(self.engines))}")
        workspace = str(task.get("workspace") or self.default_workspace)
        if workspace in self.workspaces:
            path = self.workspaces[workspace]
        elif workspace in self.workspaces.values():
            path = workspace
        else:
            raise ValueError(f"工作区不允许：{workspace}；可用：{', '.join(sorted(self.workspaces))}")
        if not Path(path).is_dir():
            raise ValueError(f"工作区不存在：{path}")
        return engine, path

    def run_cli(self, engine: str, workspace: str, message: str,
                timeout: int | None = None) -> tuple[int, str]:
        command = list(self.engines[engine])
        executable = shutil.which(command[0]) or command[0]
        if not shutil.which(command[0]) and not Path(command[0]).is_file():
            raise ValueError(f"引擎可执行文件未找到：{command[0]}")
        args = command[1:] + [message]
        if os.name == "nt":
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  "$OutputEncoding=[Text.Encoding]::UTF8; & " + ps_quote(executable) + " " +
                  " ".join(ps_quote(arg) for arg in args))
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(argv, cwd=workspace, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, creationflags=flags)
        else:
            proc = subprocess.Popen([executable, *args], cwd=workspace,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with self._lock:
            self._proc = proc
        timed_out = False
        try:
            raw, _ = proc.communicate(timeout=timeout or self.task_timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(proc)
            try:
                raw, _ = proc.communicate(timeout=5)
            except Exception:
                raw = b""
            code = -1
        finally:
            with self._lock:
                self._proc = None
        text = clean_output(decode_output(raw or b""))
        if timed_out:
            text = (text + "\n本地任务超时，已终止进程。").strip()
        return code, text

    def _kill(self, proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is not None:
                return
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def cancel(self) -> bool:
        with self._lock:
            proc = self._proc
            current = self._current
        if proc and proc.poll() is None:
            self.log.add("WARN", f"正在取消任务：{(current or {}).get('message', '')[:80]}")
            self._kill(proc)
            return True
        return False

    def handle(self, task: dict) -> None:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            self.log.add("ERROR", "收到没有 task_id 的任务，已忽略")
            return
        message = str(task.get("message") or "")
        started = time.time()
        with self._lock:
            self._current = {"task_id": task_id, "message": message, "started_at": int(started)}
            self._status = "busy"
        self.log.add("INFO", f"收到任务 {task_id[:8]}：{message[:120]}")
        try:
            engine, workspace = self.resolve(task)
            code, output = self.run_cli(engine, workspace, message)
            ok = code == 0
            text = f"[{engine} @ {workspace}] exit={code}\n{output[-6000:]}"
        except Exception as exc:
            engine = str(task.get("engine") or self.default_engine)
            workspace = str(task.get("workspace") or self.default_workspace)
            ok = False
            text = f"本地任务失败：{exc}"
        elapsed = time.time() - started
        with self._lock:
            self._tasks_done += 1
            self._last_task = {"task_id": task_id, "ok": ok, "seconds": round(elapsed, 1),
                               "engine": engine, "workspace": workspace,
                               "finished_at": int(time.time())}
            self._current = None
            self._status = "online"
        level = "INFO" if ok else "ERROR"
        self.log.add(level, f"任务 {task_id[:8]} 完成，exit={0 if ok else 1}，耗时 {elapsed:.1f}s")
        try:
            self._request("POST", "/agent/result",
                          {"task_id": task_id, "ok": ok, "text": text[:8000]})
        except Exception as exc:
            self.log.add("WARN", f"回传任务结果失败：{exc}")
        if elapsed > self.wait_seconds:
            try:
                prefix = f"【{self.config.get('name') or self.agent_id}】"
                self._request("POST", "/v1/notify", {"text": prefix + text[-3500:]})
            except Exception as exc:
                self.log.add("WARN", f"异步通知失败：{exc}")

    def test_engine(self, engine: str, workspace: str | None = None,
                    message: str = "只回复 OK，不要修改任何文件。") -> dict:
        engine = str(engine or self.default_engine)
        if engine not in self.engines:
            raise ValueError(f"引擎不可用：{engine}")
        workspace = str(workspace or self.default_workspace)
        if workspace in self.workspaces:
            path = self.workspaces[workspace]
        elif workspace in self.workspaces.values():
            path = workspace
        else:
            raise ValueError(f"工作区不允许：{workspace}")
        if not Path(path).is_dir():
            raise ValueError(f"工作区不存在：{path}")
        started = time.time()
        code, output = self.run_cli(engine, path, message,
                                    timeout=min(self.task_timeout, 180))
        elapsed = time.time() - started
        ok = code == 0
        self.log.add("INFO" if ok else "ERROR",
                     f"引擎测试 {engine} @ {path} exit={code}，耗时 {elapsed:.1f}s")
        return {"ok": ok, "exit_code": code, "seconds": round(elapsed, 1),
                "engine": engine, "workspace": path, "output": output[-6000:]}

    # ------------------------------------------------------------------
    def _run(self) -> None:
        self.log.add("INFO", f"本地 Agent 启动：{self.agent_id} → {self.router}")
        while not self._stop.is_set():
            try:
                self.register()
                task = self._request("GET", "/agent/tasks?agent_id=" +
                                     urllib.parse.quote(self.agent_id))
                with self._lock:
                    if self._status not in ("busy",):
                        self._status = "online"
                    self._last_seen = time.time()
                    self._last_error = ""
                if task:
                    self.handle(task)
                    continue
            except Exception as exc:
                message = str(exc)
                with self._lock:
                    self._status = "error"
                    self._last_error = message
                self.log.add("ERROR", message)
                if self._stop.wait(5):
                    break
                continue
            if self._stop.wait(self.poll_seconds):
                break
        with self._lock:
            if self._status != "stopped":
                self._status = "stopped"
        self.log.add("INFO", "本地 Agent 轮询已结束")
