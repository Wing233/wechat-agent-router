"""Local weekly technology-stack collection owned by WeChatAgentDesktop."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

import tech_stack_collector as collector


class TechStackService:
    """Run the privacy-preserving collector on a local weekly schedule."""

    def __init__(self, base_dir: Path, log):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir = self.base_dir / "reports"
        self.export_dir = self.base_dir / "workbuddy-exports"
        self.config_path = self.base_dir / "config.json"
        self.log = log
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._running = False
        self._last_error = ""
        self._load_or_create_config()

    def _default_config(self) -> dict[str, Any]:
        home = Path.home()
        roots = [
            Path("D:/xx"),
            Path("D:/工作"),
            Path("D:/论文"),
            Path("D:/期末作业"),
            home / "PycharmProjects",
            home / "CodeBuddy",
            home / "WorkBuddy",
            home / "wechat-agent-workspace",
        ]
        roots = list(dict.fromkeys(str(path) for path in roots if path.is_dir()))
        if not roots:
            roots = [str(home)]
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        return {
            "enabled": True,
            "weekly_day": 6,
            "weekly_time": "10:00",
            "project_roots": roots,
            "output_dir": str(self.report_dir),
            "workbuddy_exports": str(self.export_dir),
            "codex_home": str(home / ".codex"),
            "opencode_database": str(home / ".local/share/opencode/opencode.db"),
            "public_site_url": "https://123.57.153.49/api/v1/public/site",
            "history_days": 365,
            "max_depth": 7,
        }

    def _load_or_create_config(self) -> dict[str, Any]:
        try:
            data = json.loads(self.config_path.read_text("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("配置必须是 JSON 对象")
        except (OSError, ValueError):
            data = self._default_config()
            self._write_config(data)
        self.config = data
        return data

    def _write_config(self, data: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.config_path)

    @staticmethod
    def _parse_time(value: str) -> tuple[int, int]:
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
        except (TypeError, ValueError):
            raise ValueError("每周时间格式应为 HH:MM") from None
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("每周时间无效")
        return hour, minute

    def update_config(self, payload: dict) -> dict:
        config = dict(self.config)
        if "enabled" in payload:
            config["enabled"] = bool(payload["enabled"])
        if "weekly_day" in payload:
            day = int(payload["weekly_day"])
            if not 0 <= day <= 6:
                raise ValueError("星期范围应为 0 到 6")
            config["weekly_day"] = day
        if "weekly_time" in payload:
            value = str(payload["weekly_time"] or "")
            self._parse_time(value)
            config["weekly_time"] = value
        self._write_config(config)
        self.config = config
        self.log.add("INFO", "技术栈周更计划已保存")
        return self.status()

    def _latest_report(self) -> dict | None:
        path = self.report_dir / "latest.json"
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            return report if isinstance(report, dict) else None
        except (OSError, ValueError):
            return None

    def _last_success(self) -> dt.datetime | None:
        report = self._latest_report()
        value = str((report or {}).get("generated_at") or "")
        if not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return parsed.astimezone()
        except ValueError:
            return None

    def _next_scheduled(self, now: dt.datetime) -> dt.datetime:
        hour, minute = self._parse_time(str(self.config.get("weekly_time") or "10:00"))
        weekday = int(self.config.get("weekly_day", 6))
        days = (weekday - now.weekday()) % 7
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidate += dt.timedelta(days=days)
        last = self._last_success()
        while last and candidate <= last:
            candidate += dt.timedelta(days=7)
        return now if candidate <= now else candidate

    def _collect(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = ""
        try:
            collector.collect(self.config_path)
            self.log.add("INFO", "技术栈周更采集完成，报告保存在本机")
            return True
        except Exception as exc:
            self._last_error = type(exc).__name__
            self.log.add("ERROR", f"技术栈周更采集失败：{type(exc).__name__}")
            return False
        finally:
            with self._lock:
                self._running = False

    def run_now(self) -> dict:
        with self._lock:
            if self._running:
                return self.status()
            worker = threading.Thread(target=self._collect, name="tech-stack-manual", daemon=True)
            worker.start()
        return self.status()

    def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            if not bool(self.config.get("enabled", True)):
                self._stop.wait(60)
                continue
            now = dt.datetime.now().astimezone()
            last = self._last_success()
            due = now if last is None else self._next_scheduled(now)
            if due <= now:
                success = self._collect()
                if not success:
                    self._stop.wait(3600)
                continue
            self._stop.wait(min(60, max(1, (due - now).total_seconds())))

    def start(self) -> dict:
        if self._scheduler and self._scheduler.is_alive():
            return self.status()
        self._stop.clear()
        self._scheduler = threading.Thread(
            target=self._schedule_loop, name="tech-stack-weekly-scheduler", daemon=True
        )
        self._scheduler.start()
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        if self._scheduler and self._scheduler.is_alive():
            self._scheduler.join(timeout=2)
        self._scheduler = None

    def open_path(self, kind: str) -> None:
        import os

        paths = {
            "report": self.report_dir / "latest.html",
            "reports": self.report_dir,
            "exports": self.export_dir,
            "config": self.config_path,
        }
        target = paths.get(kind)
        if target is None:
            raise ValueError("未知的技术栈路径")
        if kind == "report" and not target.is_file():
            raise ValueError("还没有技术栈报告，请先立即采集")
        if kind == "config" and not target.is_file():
            raise ValueError("技术栈配置不存在")
        os.startfile(str(target))  # type: ignore[attr-defined]

    def status(self) -> dict:
        report = self._latest_report() or {}
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        with self._lock:
            running = self._running
            last_error = self._last_error
        enabled = bool(self.config.get("enabled", True))
        next_run = None
        if enabled:
            last = self._last_success()
            next_run = (dt.datetime.now().astimezone() if last is None
                        else self._next_scheduled(dt.datetime.now().astimezone())).isoformat(timespec="minutes")
        return {
            "enabled": enabled,
            "running": running,
            "weekly_day": int(self.config.get("weekly_day", 6)),
            "weekly_time": str(self.config.get("weekly_time") or "10:00"),
            "next_run_at": next_run,
            "last_completed_at": str(report.get("generated_at") or ""),
            "last_error": last_error,
            "technology_count": len(report.get("technologies") or []),
            "summary": {
                key: summary.get(key, 0)
                for key in ("projects_scanned", "codex_files_scanned",
                            "opencode_messages_scanned", "workbuddy_files_scanned")
            },
            "paths": {
                "reports": str(self.report_dir),
                "latest_html": str(self.report_dir / "latest.html"),
                "workbuddy_exports": str(self.export_dir),
                "config": str(self.config_path),
            },
            "project_roots": list(self.config.get("project_roots") or []),
            "public_baseline_status": str(summary.get("public_baseline_status") or "尚未采集"),
        }
