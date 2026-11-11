"""Desktop-owned worklog collector.

The standalone scheduled task is retired: the desktop app starts this service
when it starts, and stops it on exit. The same process now owns the local agent,
the UI and the activity collector.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import worklog_collector as collector


class WorklogService:
    def __init__(self, base_dir: Path, event_log=None):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.base_dir / "config.json"
        self.db_path = self.base_dir / "worklog.sqlite3"
        self.secrets = collector.SecretStore(self.base_dir / "secrets.json")
        self.log_path = self.base_dir / "collector.log"
        self.event_log = event_log
        self.db = collector.WorklogDB(self.db_path)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._job: dict | None = None
        self._last_error = ""
        self._started_at = 0.0
        self._migrate_legacy()
        self._ensure_config()
        self.db.init()
        collector.setup_logging(self.log_path, stream=False)

    # ------------------------------------------------------------------
    def _event(self, level: str, message: str) -> None:
        if self.event_log is not None:
            try:
                self.event_log.add(level, message)
            except Exception:
                pass

    def _migrate_legacy(self) -> None:
        old = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WeChatAgentWorklog"
        if not old.is_dir() or self.db_path.exists():
            return
        try:
            old_config = old / "config.json"
            if old_config.is_file() and not self.config_path.exists():
                shutil.copy2(old_config, self.config_path)
            old_db = old / "worklog.sqlite3"
            if old_db.is_file():
                shutil.copy2(old_db, self.db_path)
            token = collector.SecretStore(old / "secrets.json").get("device_token")
            if token:
                self.secrets.set("device_token", token)
            self._event("INFO", f"已迁移旧工作日志数据：{old}")
        except Exception as exc:
            self._event("WARN", f"迁移旧工作日志数据失败：{exc}")

    def _ensure_config(self) -> None:
        if self.config_path.is_file():
            return
        default = json.loads(json.dumps(collector.DEFAULT_CONFIG))
        default["worklog_dir"] = str(self.base_dir / "notes")
        self.config_path.write_text(
            json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> dict:
        with self._lock:
            if self.running:
                return self.status()
            self._stop.clear()
            self._last_error = ""
            self._started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="worklog", daemon=True)
            self._thread.start()
            self._event("INFO", "工作日志采集已启动")
            return self.status()

    def _run(self) -> None:
        try:
            config = collector.load_config(self.config_path)
            client = collector._client(config, self.secrets)
            collector.daemon(self.db, client, config, stop_event=self._stop)
        except Exception as exc:
            self._last_error = str(exc)[:500]
            self._event("ERROR", f"工作日志采集失败：{exc}")

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=8)
        self._event("INFO", "工作日志采集已停止")
        return self.status()

    def restart(self) -> dict:
        self.stop()
        return self.start()

    # ------------------------------------------------------------------
    def _set_job(self, kind: str, target=None) -> None:
        self._job = {"kind": kind, "target": target, "started_at": int(time.time()),
                     "result": None, "error": ""}

    def _run_job(self, kind: str, fn) -> None:
        try:
            result = fn()
            self._job = {**(self._job or {}), "result": result, "error": ""}
            self._event("INFO", f"工作日志任务完成：{kind} {result}")
        except Exception as exc:
            self._job = {**(self._job or {}), "error": str(exc)[:500]}
            self._last_error = str(exc)[:500]
            self._event("ERROR", f"工作日志任务失败：{kind} {exc}")

    def _start_job(self, kind: str, target, fn) -> dict:
        with self._lock:
            if self._job and self._job.get("result") is None and not self._job.get("error"):
                # A job with neither result nor error is still running.
                if self._job.get("started_at") and time.time() - self._job["started_at"] < 1800:
                    raise ValueError("已有工作日志任务在运行")
            self._set_job(kind, target)
            threading.Thread(target=self._run_job, args=(kind, fn),
                             name="worklog-job", daemon=True).start()
        return self.status()

    def finalize(self, day: str = "") -> dict:
        config = collector.load_config(self.config_path)
        target = day or datetime.now().astimezone().date().isoformat()
        client = collector._client(config, self.secrets)
        # 手动补传用于纠正 empty；已上传的日期仍保持幂等，避免 409。
        existing = self.db.day(target) or {}
        force = existing.get("status") == "empty"
        return self._start_job(
            "finalize", target,
            lambda: collector.finalize_day(target, self.db, client, config, force=force),
        )

    def backfill(self, days: int = 30) -> dict:
        config = collector.load_config(self.config_path)
        client = collector._client(config, self.secrets)
        days = max(1, min(int(days or 30), 90))
        return self._start_job(
            "backfill", days,
            lambda: (collector.backfill(days, self.db, client, config), {"days": days})[1],
        )

    # ------------------------------------------------------------------
    def update_config(self, payload: dict) -> dict:
        config = collector.load_config(self.config_path)
        for key in ("sample_seconds", "idle_skip_seconds", "min_note_chars"):
            if key in payload:
                try:
                    value = int(payload[key])
                except (TypeError, ValueError):
                    raise ValueError(f"{key} 必须是数字") from None
                if key == "sample_seconds":
                    value = max(5, min(value, 600))
                elif key == "idle_skip_seconds":
                    value = max(60, min(value, 7200))
                else:
                    value = max(0, min(value, 16000))
                config[key] = value
        for key in ("ocr_max_chars", "ocr_lines_per_app"):
            if key in payload:
                try:
                    value = int(payload[key])
                except (TypeError, ValueError):
                    raise ValueError(f"{key} 必须是数字") from None
                if key == "ocr_max_chars":
                    value = max(0, min(value, 16000))
                else:
                    value = max(1, min(value, 500))
                config[key] = value
        if "screenshot_enabled" in payload:
            raw = payload["screenshot_enabled"]
            config["screenshot_enabled"] = (
                raw if isinstance(raw, bool)
                else str(raw).lower() in ("1", "true", "yes", "on"))
        if "ocr_language" in payload:
            config["ocr_language"] = (
                str(payload.get("ocr_language") or "zh-CN").strip() or "zh-CN")
        if "finalize_time" in payload:
            text = str(payload.get("finalize_time") or "").strip()
            try:
                hour, minute = text.split(":", 1)
                hour, minute = int(hour), int(minute)
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError()
            except ValueError:
                raise ValueError("采集时间格式应为 HH:MM") from None
            config["finalize_time"] = f"{hour:02d}:{minute:02d}"
        if "worklog_dir" in payload:
            config["worklog_dir"] = str(payload.get("worklog_dir") or "").strip()
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass
        if self.running:
            self.restart()
        return self.status()

    def set_device_token(self, token: str) -> dict:
        text = str(token or "").strip()
        if not text:
            raise ValueError("设备 token 不能为空")
        self.secrets.set("device_token", text)
        if self.running:
            self.restart()
        return self.status()

    def clear_device_token(self) -> dict:
        self.secrets.set("device_token", "")
        if self.running:
            self.restart()
        return self.status()

    # ------------------------------------------------------------------
    def status(self) -> dict:
        config = collector.load_config(self.config_path)
        today = datetime.now().astimezone().date().isoformat()
        samples_today = len(self.db.samples(today))
        with self.db._conn() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT day,status,length(note) AS chars,updated_at FROM days "
                "ORDER BY day DESC LIMIT 10")]
        last_upload = next((row for row in rows
                            if row["status"] in ("uploaded", "published")), None)
        hour, minute = collector._parse_time(str(config.get("finalize_time") or "22:00"))
        next_finalize = datetime.now().astimezone().replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if next_finalize <= datetime.now().astimezone():
            next_finalize += timedelta(days=1)
        return {
            "running": self.running,
            "started_at": int(self._started_at) if self._started_at else None,
            "device_configured": bool(self.secrets.get("device_token")),
            "config": {
                "sample_seconds": config.get("sample_seconds"),
                "idle_skip_seconds": config.get("idle_skip_seconds"),
                "finalize_time": config.get("finalize_time"),
                "min_note_chars": config.get("min_note_chars"),
                "worklog_dir": config.get("worklog_dir"),
                "screenshot_enabled": config.get("screenshot_enabled", True),
                "ocr_language": config.get("ocr_language", "zh-CN"),
                "ocr_max_chars": config.get("ocr_max_chars", 4000),
                "ocr_lines_per_app": config.get("ocr_lines_per_app", 30),
            },
            "today": today,
            "samples_today": samples_today,
            "days": rows,
            "last_upload": last_upload,
            "next_finalize": next_finalize.isoformat(timespec="minutes"),
            "job": self._job,
            "last_error": self._last_error,
            "paths": {
                "data": str(self.base_dir),
                "config": str(self.config_path),
                "log": str(self.log_path),
                "notes": str(config.get("worklog_dir") or ""),
            },
        }
