"""Windows work-activity collector for LYPH daily logs.

Design:
- sample the foreground window every 30s (no keystrokes, no clipboard, no screen)
- store samples locally in SQLite
- at 22:50 build one structured note for the day, upload it to LYPH as a
  ``source_kind=note`` capture, and keep a local Markdown copy
- a server-side publisher turns worklog-only days into public LYPH journal
  entries through the existing daily pipeline

Run as a current-user scheduled task at logon. See docs/worklog.md.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from ctypes import wintypes
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import worklog_sources

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WeChatAgentWorklog"
DEFAULT_CONFIG = {
    "lyph_base_url": "https://123.57.153.49",
    "device_name": "pc-home-worklog",
    "sample_seconds": 30,
    "idle_skip_seconds": 300,
    "finalize_time": "04:00",
    "min_note_chars": 100,
    "worklog_dir": str(Path.home() / "Documents" / "worklog"),
    "browser_apps": ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"],
    "redact_patterns": [
        "password", "passwd", "token", "secret", "api key", "apikey",
        "密码", "口令", "私钥", "助记词", "银行卡", "身份证", "验证码",
        "支付", "转账", "登录", "sign in", "authenticator",
    ],
    "sources": {
        "git": {
            "enabled": True,
            "repos": [
                r"D:\xx\wechatbot\agent-router",
                r"D:\xx\wechatbot\lyph",
                r"D:\xx\wechatbot\public\wechat-agent-router",
                r"D:\xx\wechatbot\public\lyph",
                r"D:\xx\lyph",
            ],
            "authors": ["liubo", "Wing233"],
            "exclude_authors": ["wechat-agent-router", "evolution", "bot"],
        },
        "browser": {
            "enabled": True,
            "profiles": [
                {
                    "name": "edge",
                    "path": str(Path.home() / "AppData/Local/Microsoft/Edge/User Data/Default/History"),
                    "exclude_domains": ["localhost", "127.0.0.1", "newtab", "bing.com"],
                },
                {
                    "name": "chrome",
                    "path": str(Path.home() / "AppData/Local/Google/Chrome/User Data/Default/History"),
                    "exclude_domains": ["localhost", "127.0.0.1", "newtab", "google.com"],
                },
            ],
        },
        "files": {
            "enabled": True,
            "roots": [r"D:\xx\wechatbot", r"D:\xx\lyph"],
            "exclude_projects": ["scratch", "data", "backups",
                                  "agent-router-merge", "agent-router-session",
                                  "agent-router-task-state", "agent-router-trending",
                                  "agent-router-media", "agent-router-worklog"],
        },
        "codex": {"enabled": True, "root": str(Path.home() / ".codex" / "sessions")},
        "workbuddy": {"enabled": False},
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    result = json.loads(json.dumps(base))
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _deep_merge(DEFAULT_CONFIG, data)
        except Exception as exc:
            raise SystemExit(f"配置读取失败：{exc}") from None
    return json.loads(json.dumps(DEFAULT_CONFIG))


def setup_logging(log_path: Path, stream: bool = True) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.replace(log_path.with_name(log_path.name + ".1"))
    except OSError:
        pass
    handlers = [logging.FileHandler(log_path, encoding="utf-8")]
    if stream:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


class SecretStore:
    """DPAPI-backed token store, with a restricted-file fallback."""

    def __init__(self, path: Path):
        self.path = path
        self._store = None
        try:
            from desktop.secret_store import SecretStore as Store
            self._store = Store(path)
        except Exception:
            self._store = None

    def get(self, key: str, default: str = "") -> str:
        if self._store is not None:
            return self._store.get(key, default)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return str(data.get(key) or default)
        except Exception:
            return default

    def set(self, key: str, value: str) -> None:
        if self._store is not None:
            self._store.set(key, value)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data[key] = value
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class WorklogDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS samples(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL, day TEXT NOT NULL, app TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '', idle INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS samples_day ON samples(day, id);
            CREATE TABLE IF NOT EXISTS days(
              day TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '',
              capture_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            """)

    def sample(self, day: str, app: str, title: str, idle: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO samples(ts,day,app,title,idle) VALUES(?,?,?,?,?)",
                (datetime.now().astimezone().isoformat(timespec="seconds"),
                 day, app[:80], title[:160], max(0, int(idle))),
            )

    def samples(self, day: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT app,title,idle FROM samples WHERE day=? ORDER BY id", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    def day(self, day: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM days WHERE day=?", (day,)).fetchone()
        return dict(row) if row else None

    def save_day(self, day: str, *, note: str = "", capture_id: str = "",
                 status: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO days(day,note,capture_id,status,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET note=excluded.note,"
                "capture_id=excluded.capture_id,status=excluded.status,"
                "updated_at=excluded.updated_at",
                (day, note[:16000], capture_id[:100], status,
                 datetime.now().astimezone().isoformat(timespec="seconds")),
            )

    def prune(self, days: int = 30) -> None:
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).date().isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM samples WHERE day < ?", (cutoff,))


# ---------------------------------------------------------------------------
# foreground sampling
# ---------------------------------------------------------------------------
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> int:
    if os.name != "nt":
        return 0
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0, int(millis / 1000))
    except Exception:
        return 0


def foreground() -> tuple[str, str]:
    if os.name != "nt":
        return "", ""
    try:
        import psutil
        import win32gui
        import win32process
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return "", title
        try:
            app = psutil.Process(pid).name()
        except Exception:
            app = f"pid-{pid}"
        return app, title
    except Exception:
        return "", ""


def _short_project(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = Path(text).parts
        return parts[-1] if parts else text
    except Exception:
        return text[:80]


def sanitize_text(text: str) -> str:
    value = str(text or "")
    home = str(Path.home())
    if home:
        value = value.replace(home, "~")
    value = value.replace(r"D:\xx\wechatbot", "wechatbot")
    value = value.replace(r"D:\xx\lyph", "lyph")
    return value


def redact_title(title: str, app: str, config: dict) -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return ""
    if app.lower() in {str(item).lower() for item in config.get("browser_apps") or []}:
        return "[浏览器页面已隐藏]"
    lower = text.lower()
    if any(str(pattern).lower() in lower for pattern in config.get("redact_patterns") or []):
        return "[敏感窗口已隐藏]"
    return sanitize_text(text)[:120]


def sample_once(db: WorklogDB, config: dict) -> None:
    app, title = foreground()
    if not app:
        return
    idle = idle_seconds()
    if idle >= int(config.get("idle_skip_seconds") or 300):
        return
    if app.lower() in {"lockapp.exe", "logonui.exe"}:
        return
    clean = redact_title(title, app, config)
    day = datetime.now().astimezone().date().isoformat()
    db.sample(day, app, clean, idle)


# ---------------------------------------------------------------------------
# note construction
# ---------------------------------------------------------------------------
def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _evidence_lines(evidence: dict) -> list[str]:
    lines: list[str] = []
    git = evidence.get("git") or []
    if git:
        lines.append(f"Git 提交（共 {len(git)} 条）:")
        for item in git[:20]:
            lines.append(f"- {item['repo']}: {item['subject']}")
    codex = evidence.get("codex") or {}
    if codex.get("sessions") or codex.get("messages"):
        lines.append("AI 会话:")
        lines.append(
            f"- Codex: {codex.get('sessions', 0)} 个会话 / "
            f"{codex.get('messages', 0)} 条用户消息"
        )
        for cwd, count in (codex.get("cwd") or [])[:6]:
            lines.append(f"- 项目: {_short_project(cwd)} ({count})")
    browser = evidence.get("browser") or []
    if browser:
        lines.append("浏览（访问次数，不是小时）:")
        for item in browser[:12]:
            lines.append(f"- {item.get('category') or item.get('domain')}: {item['visits']} 次")
    files = evidence.get("files") or []
    if files:
        lines.append("文件改动（修改文件数）:")
        for item in files[:10]:
            lines.append(f"- {item['project']}: {item['files']} 个文件")
    workbuddy = evidence.get("workbuddy") or []
    if workbuddy:
        lines.append("WorkBuddy:")
        for item in workbuddy[:8]:
            label = item.get("title") or _short_project(item.get("cwd") or "") or "session"
            lines.append(f"- {sanitize_text(label)[:100]}")
    return lines


def build_note(day: str, db: WorklogDB, config: dict) -> str:
    samples = db.samples(day)
    sample_seconds = max(5, int(config.get("sample_seconds") or 30))
    idle_limit = max(1, int(config.get("idle_skip_seconds") or 300))
    app_seconds: Counter = Counter()
    title_seconds: Counter = Counter()
    active = 0
    for item in samples:
        if int(item.get("idle") or 0) >= idle_limit:
            continue
        active += sample_seconds
        app = str(item.get("app") or "").strip() or "unknown"
        app_seconds[app] += sample_seconds
        title = str(item.get("title") or "").strip()
        if title:
            title_seconds[(app, title)] += sample_seconds

    evidence = worklog_sources.collect_evidence(day, config)
    has_evidence = bool(
        evidence.get("git") or evidence.get("browser") or evidence.get("files")
        or (evidence.get("codex") or {}).get("sessions")
        or (evidence.get("codex") or {}).get("messages")
        or evidence.get("workbuddy")
    )
    if not samples and not has_evidence:
        return ""
    lines = [f"[worklog] {day} 工作活动"]
    lines.append("统计口径：浏览=访问次数，文件=修改文件数，AI 会话=消息数；没有活跃时长数据时不要写工时。")
    if active:
        lines.append(f"活跃时长: {format_duration(active)}")
    if app_seconds:
        lines.append("应用:")
        for app, seconds in app_seconds.most_common(12):
            lines.append(f"- {app}: {format_duration(seconds)}")
    if title_seconds:
        lines.append("窗口片段:")
        for (app, title), seconds in title_seconds.most_common(18):
            lines.append(f"- {format_duration(seconds)} {app}: {title}")
    lines.extend(_evidence_lines(evidence))
    text = "\n".join(lines).strip()
    return text[:16000]


class LyphClient:
    def __init__(self, base_url: str, device_token: str):
        self.base_url = base_url.rstrip("/")
        self.device_token = device_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.device_token}"}

    def get_config(self) -> dict:
        import requests
        response = requests.get(
            f"{self.base_url}/api/v1/device/config",
            headers=self._headers(), timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def upload_note(self, day: str, text: str) -> dict:
        import requests
        capture_id = f"worklog-{day}-v1"
        response = requests.post(
            f"{self.base_url}/api/v1/device/captures",
            headers=self._headers(),
            data={
                "capture_id": capture_id,
                "captured_at": f"{day}T22:50:00+08:00",
                "source_kind": "note",
                "text": text,
            },
            timeout=45,
        )
        if response.status_code == 409:
            return {"duplicate": True, "conflict": True, "status": 409}
        response.raise_for_status()
        return response.json()


def finalize_day(day: str, db: WorklogDB, client: LyphClient | None,
                 config: dict, *, force: bool = False, dry_run: bool = False,
                 refresh_sources: bool = True) -> dict:
    if refresh_sources:
        worklog_sources.clear_caches()
    existing = db.day(day) or {}
    if existing.get("status") in ("uploaded", "published", "empty") and not force:
        return {"day": day, "status": "skipped", "reason": "already processed"}
    note = existing.get("note") or build_note(day, db, config)
    if not note.strip():
        db.save_day(day, status="empty")
        return {"day": day, "status": "empty"}
    minimum = max(0, int(config.get("min_note_chars") or 100))
    if len(note.strip()) < minimum and not force:
        db.save_day(day, note=note, status="empty")
        return {"day": day, "status": "empty", "reason": "below min_note_chars"}
    if dry_run:
        print(f"\n===== {day} =====\n{note}\n")
        return {"day": day, "status": "dry-run", "chars": len(note)}
    worklog_dir = Path(config.get("worklog_dir") or "")
    if str(worklog_dir):
        worklog_dir.mkdir(parents=True, exist_ok=True)
        (worklog_dir / f"{day}.md").write_text(note, encoding="utf-8")
    if client is None:
        db.save_day(day, note=note, status="local")
        return {"day": day, "status": "local", "chars": len(note)}
    result = client.upload_note(day, note)
    duplicate = bool(result.get("duplicate"))
    db.save_day(day, note=note, capture_id=f"worklog-{day}-v1",
                status="uploaded" if not result.get("conflict") else "conflict")
    return {"day": day, "status": "uploaded" if not result.get("conflict") else "conflict",
            "duplicate": duplicate, "chars": len(note)}


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = str(value).split(":", 1)
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except Exception:
        return 22, 50


def _client(config: dict, secrets: SecretStore) -> LyphClient | None:
    token = secrets.get("device_token")
    if not token:
        logging.error("缺少 device token；先运行 --import-device device.json")
        return None
    return LyphClient(str(config.get("lyph_base_url") or ""), token)


def backfill(days: int, db: WorklogDB, client: LyphClient | None, config: dict,
             *, dry_run: bool = False) -> None:
    worklog_sources.clear_caches()
    today = datetime.now().astimezone().date()
    for offset in range(1, max(1, days) + 1):
        day = (today - timedelta(days=offset)).isoformat()
        try:
            result = finalize_day(day, db, client, config, dry_run=dry_run,
                                  refresh_sources=False)
            logging.info("backfill %s -> %s", day, result)
        except Exception as exc:
            logging.warning("backfill %s failed: %s", day, exc)


def _finalize_target(now: datetime, finalize_hour: int) -> date:
    """At 04:00 collect yesterday; before 04:00 the latest complete day is two days ago."""
    return now.date() - timedelta(days=1 if now.hour >= finalize_hour else 2)


def catch_up(db: WorklogDB, client: LyphClient | None, config: dict,
             finalize_hour: int, days: int = 7) -> None:
    now = datetime.now().astimezone()
    newest = _finalize_target(now, finalize_hour)
    for offset in range(max(1, days)):
        day = (newest - timedelta(days=offset)).isoformat()
        existing = db.day(day) or {}
        if existing.get("status") in ("uploaded", "published", "conflict", "empty"):
            continue
        if not db.samples(day):
            continue
        try:
            logging.info("catch-up %s -> %s", day,
                         finalize_day(day, db, client, config))
        except Exception as exc:
            logging.warning("catch-up %s failed: %s", day, exc)


def daemon(db: WorklogDB, client: LyphClient | None, config: dict,
           stop_event: threading.Event | None = None) -> None:
    sample_seconds = max(5, int(config.get("sample_seconds") or 30))
    finalize_hour, finalize_minute = _parse_time(str(config.get("finalize_time") or "04:00"))
    logging.info("collector started: sample=%ss finalize=%02d:%02d",
                 sample_seconds, finalize_hour, finalize_minute)
    catch_up(db, client, config, finalize_hour)
    while not (stop_event and stop_event.is_set()):
        try:
            sample_once(db, config)
            now = datetime.now().astimezone()
            if (now.hour, now.minute) >= (finalize_hour, finalize_minute):
                day = (now.date() - timedelta(days=1)).isoformat()
                existing = db.day(day) or {}
                if existing.get("status") not in ("uploaded", "published", "conflict", "empty"):
                    finalize_day(day, db, client, config)
            db.prune(30)
        except Exception as exc:
            logging.warning("collector loop failed: %s", exc)
        if stop_event is not None:
            stop_event.wait(sample_seconds)
        else:
            time.sleep(sample_seconds)
    logging.info("collector stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="LYPH worklog collector")
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    parser.add_argument("--import-device", metavar="JSON")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--finalize", metavar="YYYY-MM-DD")
    parser.add_argument("--backfill", type=int, metavar="DAYS")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    APP_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(APP_DIR / "collector.log")
    config = load_config(Path(args.config))
    secrets = SecretStore(APP_DIR / "secrets.json")

    if args.import_device:
        data = json.loads(Path(args.import_device).read_text(encoding="utf-8-sig"))
        token = str(data.get("token") or "").strip()
        if not token:
            raise SystemExit("device JSON 中没有 token")
        secrets.set("device_token", token)
        print("device token 已保存到 DPAPI")
        return

    db = WorklogDB(APP_DIR / "worklog.sqlite3")
    db.init()
    client = _client(config, secrets)

    if args.once:
        sample_once(db, config)
        print("sampled")
        return
    if args.finalize:
        print(json.dumps(finalize_day(args.finalize, db, client, config,
                                      force=args.force, dry_run=args.dry_run),
                         ensure_ascii=False))
        return
    if args.backfill:
        backfill(args.backfill, db, client, config, dry_run=args.dry_run)
        return
    if client is None:
        raise SystemExit("缺少 device token；先运行 --import-device device.json")
    daemon(db, client, config)


if __name__ == "__main__":
    main()
