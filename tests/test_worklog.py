"""Offline tests for the Windows worklog collector."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worklog_collector as collector
import worklog_sources as sources


def test_format_and_redaction() -> None:
    assert collector.format_duration(0) == "0m"
    assert collector.format_duration(3600 + 5 * 60) == "1h05m"
    config = {"browser_apps": ["chrome.exe"], "redact_patterns": ["密码", "token"]}
    assert collector.redact_title("Chrome - secret page", "chrome.exe", config) == "[浏览器页面已隐藏]"
    assert collector.redact_title("输入密码", "notepad.exe", config) == "[敏感窗口已隐藏]"
    assert collector.redact_title("main.py - VS Code", "code.exe", config) == "main.py - VS Code"


def test_build_note_from_samples(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-01"
    for _ in range(10):
        db.sample(day, "code.exe", "main.py - VS Code", 0)
    for _ in range(5):
        db.sample(day, "msedge.exe", "page", 0)
    db.sample(day, "code.exe", "idle", 999)
    config = {
        "sample_seconds": 30,
        "idle_skip_seconds": 300,
        "sources": {
            "git": {"enabled": False},
            "browser": {"enabled": False},
            "files": {"enabled": False},
            "codex": {"enabled": False},
            "workbuddy": {"enabled": False},
        },
    }
    note = collector.build_note(day, db, config)
    assert "[worklog] 2026-11-01" in note
    assert "code.exe" in note
    assert "7m" in note  # 15 samples * 30s
    assert "main.py - VS Code" in note


def test_browser_categories() -> None:
    assert sources._browser_category("github.com") == "代码托管"
    assert sources._browser_category("leetcode.cn") == "刷题"
    assert sources._browser_category("console.volcengine.com") == "云服务/部署"
    assert sources._browser_category("example.com") == "其他浏览"


def test_codex_index(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    session = root / "2026-11-01.jsonl"
    rows = [
        {"timestamp": "2026-11-01T09:00:00+08:00",
         "payload": {"type": "message", "role": "user", "cwd": r"D:\work\a"}},
        {"timestamp": "2026-11-01T09:01:00+08:00",
         "payload": {"type": "message", "role": "assistant", "cwd": r"D:\work\a"}},
        {"timestamp": "2026-10-31T23:00:00+08:00",
         "payload": {"type": "message", "role": "user", "cwd": r"D:\work\b"}},
    ]
    session.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    index = sources.CodexIndex(str(root))
    day = index.day("2026-11-01")
    assert len(day["sessions"]) == 1
    assert day["messages"] == 1
    assert day["cwd"][r"D:\work\a"] == 2


def test_file_index(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "project").mkdir(parents=True)
    target = root / "project" / "a.py"
    target.write_text("x", encoding="utf-8")
    day = __import__("datetime").datetime.fromtimestamp(target.stat().st_mtime).astimezone().date().isoformat()
    index = sources.FileIndex([str(root)], ["scratch"])
    assert index.day(day)[0]["project"] == "repo/project"
    assert index.day(day)[0]["files"] == 1
