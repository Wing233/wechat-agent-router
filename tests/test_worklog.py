"""Offline tests for the Windows worklog collector."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worklog_collector as collector
import worklog_ocr as ocr
import worklog_sources as sources


# 哨兵提取：严格正则（标记定位 + 单个 JSON 对象 + 注释结束符收尾 + 允许跨行），
# 解析一律包 try/except，失败降级为空，绝不让页面报错。
_SENTINEL_RE = re.compile(r"<!--\s*WORKLOG_TL_V1\s+(\{.*\})\s*-->", re.DOTALL)


def _sentinel_json_text(note: str) -> str:
    try:
        match = _SENTINEL_RE.search(note or "")
        return match.group(1) if match else ""
    except Exception:
        return ""


def _extract_sentinel(note: str) -> dict:
    try:
        raw = _sentinel_json_text(note)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _ocr_config(**overrides):
    config = {
        "sample_seconds": 30,
        "idle_skip_seconds": 300,
        "redact_patterns": ["密码", "token", "银行卡"],
        "browser_apps": ["chrome.exe", "msedge.exe"],
        "screenshot_enabled": True,
        "ocr_language": "zh-CN",
        "ocr_max_chars": 4000,
        "ocr_lines_per_app": 30,
        "sources": {
            "git": {"enabled": False},
            "browser": {"enabled": False},
            "files": {"enabled": False},
            "codex": {"enabled": False},
            "workbuddy": {"enabled": False},
        },
    }
    config.update(overrides)
    return config


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


def test_build_note_includes_ocr_clues(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-04"
    db.sample(day, "code.exe", "main.py - VS Code", 0)
    db.add_ocr_sample(day, "code.exe", "def main():\n    pass")
    db.add_ocr_sample(day, "code.exe", "def main():")  # 重复行应去重
    db.add_ocr_sample(day, "notepad.exe", "会议纪要\n项目计划")
    note = collector.build_note(day, db, _ocr_config())
    assert "[worklog] 2026-11-04" in note
    assert "屏幕文字线索" in note
    assert "def main():" in note
    assert "[code.exe]" in note and "[notepad.exe]" in note  # 按 app 聚合
    assert "会议纪要" in note
    assert note.count("def main():") == 1  # 去重
    # 纯文字：不含任何图片 / 附件字段
    for token in ("object_id", "data:image", "base64", "files=", "files\":", "png", "PNG"):
        assert token not in note


def test_ocr_clues_respect_max_chars(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-04"
    db.sample(day, "code.exe", "main.py", 0)
    for i in range(200):
        db.add_ocr_sample(day, "code.exe", f"第 {i} 行屏幕文字内容 abcdefghij")
    note = collector.build_note(day, db, _ocr_config(ocr_max_chars=200))
    assert "屏幕文字线索" in note
    assert len(note) <= 16000
    # 哨兵段追加在末尾，不参与「OCR 章节限长」的测量（先剥离哨兵再看章节）。
    plain = note.split("<!--WORKLOG_TL_V1", 1)[0]
    section = plain.split("屏幕文字线索", 1)[1]
    assert len(section) <= 400  # 章节整体限长，不撞 16000 字上限


def test_upload_note_posts_text_only(monkeypatch) -> None:
    import requests
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "x"}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    client = collector.LyphClient("https://example.invalid", "token")
    client.upload_note("2026-11-04", "纯文字日报", 22, 0)
    assert set(captured["data"]) == {"capture_id", "captured_at", "source_kind", "text"}
    assert captured["data"]["text"] == "纯文字日报"
    assert captured["data"]["captured_at"] == "2026-11-04T22:00:00+08:00"
    assert captured["data"]["source_kind"] == "note"
    assert "files" not in captured  # requests.post 未带 files=
    assert "object_id" not in captured["data"]


def test_ocr_skips_sensitive_frames(monkeypatch, tmp_path: Path) -> None:
    grabs = []

    def fake_grab():
        grabs.append(1)
        return (2, 2, b"\x00" * 16)

    monkeypatch.setattr(ocr, "_grab_bitmap", fake_grab)
    config = _ocr_config()
    frames = [
        ("chrome.exe", "普通页面", 0),   # 浏览器
        ("notepad.exe", "输入密码", 0),   # 敏感词
        ("lockapp.exe", "", 0),          # 锁屏
        ("code.exe", "main.py", 999),    # idle 超时
    ]
    for app, title, idle in frames:
        monkeypatch.setattr(ocr.collector, "foreground", lambda a=app, t=title: (a, t))
        monkeypatch.setattr(ocr.collector, "idle_seconds", lambda i=idle: i)
        assert ocr.capture_screen_text(config) == ""
    assert grabs == []  # 零抓屏
    # 截图路径不写任何图片文件到本地
    assert not list(Path(tmp_path).glob("*.png"))
    assert not list(Path(tmp_path).glob("*.bmp"))


def test_capture_disabled_and_ocr_failure_are_safe(monkeypatch) -> None:
    grabs = []
    monkeypatch.setattr(ocr, "_grab_bitmap", lambda: grabs.append(1) or (2, 2, b"\x00" * 16))
    monkeypatch.setattr(ocr.collector, "foreground", lambda: ("code.exe", "main.py"))
    monkeypatch.setattr(ocr.collector, "idle_seconds", lambda: 0)
    assert ocr.capture_screen_text(_ocr_config(screenshot_enabled=False)) == ""
    assert grabs == []

    def boom(*args):
        raise RuntimeError("winsdk 未安装")

    monkeypatch.setattr(ocr, "_ocr_bitmap", boom)
    assert ocr.capture_screen_text(_ocr_config()) == ""  # 异常降级，不外抛


def test_redact_ocr_text_hides_lines_not_truncated() -> None:
    config = _ocr_config()
    text = "普通一行\n我的密码是 abc\n另一行 token=xxx\n正常内容\n"
    out = ocr.redact_ocr_text(text, config)
    assert "普通一行" in out
    assert "正常内容" in out
    assert "[敏感内容已隐藏]" in out
    assert "abc" not in out
    assert "token=xxx" not in out
    long_text = "这是一段很长的普通屏幕文字" * 20
    assert len(ocr.redact_ocr_text(long_text, config)) > 120  # 不套 120 截断


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


def test_finalize_target_uses_same_day_at_22() -> None:
    from datetime import datetime
    # 汇总时刻（22:00）之后目标为当天，之前为昨天（避免半天数据被提前上传）
    assert collector._finalize_target(datetime(2026, 11, 2, 21, 0), 22, 0).isoformat() == "2026-11-01"
    assert collector._finalize_target(datetime(2026, 11, 2, 22, 0), 22, 0).isoformat() == "2026-11-02"
    assert collector._finalize_target(datetime(2026, 11, 2, 23, 30), 22, 0).isoformat() == "2026-11-02"


def test_finalize_target_honors_minute_of_non_round_time() -> None:
    from datetime import datetime
    # 非整点汇总 22:30：22:15/22:29 仍返回昨天，22:30 才切到当天
    assert collector._finalize_target(datetime(2026, 11, 2, 22, 15), 22, 30).isoformat() == "2026-11-01"
    assert collector._finalize_target(datetime(2026, 11, 2, 22, 29), 22, 30).isoformat() == "2026-11-01"
    assert collector._finalize_target(datetime(2026, 11, 2, 22, 30), 22, 30).isoformat() == "2026-11-02"
    assert collector._finalize_target(datetime(2026, 11, 2, 23, 0), 22, 30).isoformat() == "2026-11-02"


def test_backfill_skips_today() -> None:
    from datetime import datetime, timedelta
    from unittest.mock import patch

    called_days = []

    def fake_finalize_day(day, db, client, config, **kwargs):
        called_days.append(day)
        return {"day": day, "status": "empty"}

    # 用最简假对象：backfill 只把它们透传给已打桩的 finalize_day
    with patch.object(collector, "finalize_day", side_effect=fake_finalize_day):
        collector.backfill(3, object(), object(), {})

    today = datetime.now().astimezone().date()
    yesterday = (today - timedelta(days=1)).isoformat()
    assert today.isoformat() not in called_days  # 不碰当天，避免半天数据被锁死
    assert yesterday in called_days              # 含昨天
    assert len(called_days) == 3                 # days=3 仍覆盖 3 天（昨天及往日）


def test_file_index(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "project").mkdir(parents=True)
    target = root / "project" / "a.py"
    target.write_text("x", encoding="utf-8")
    day = __import__("datetime").datetime.fromtimestamp(target.stat().st_mtime).astimezone().date().isoformat()
    index = sources.FileIndex([str(root)], ["scratch"])
    assert index.day(day)[0]["project"] == "repo/project"
    assert index.day(day)[0]["files"] == 1


# ---------------------------------------------------------------------------
# 哨兵段（WORKLOG_TL_V1）测试
# ---------------------------------------------------------------------------
def test_sentinel_shape_marker_and_closing(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-04"
    db.sample(day, "code.exe", "main.py - VS Code", 0)
    note = collector.build_note(day, db, _ocr_config())
    assert "WORKLOG_TL_V1" in note
    assert note.rstrip().endswith("-->")            # a) 以注释结束符收尾
    assert "```" not in note                        # 不用三反引号围栏


def test_sentinel_json_parseable_and_keys(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-04"
    db.sample(day, "code.exe", "main.py - VS Code", 0)
    note = collector.build_note(day, db, _ocr_config())
    payload = _extract_sentinel(note)               # b) 严格正则 + json.loads
    assert isinstance(payload, dict) and payload
    assert {"v", "day", "pts", "hl", "stats"} <= set(payload)
    assert payload["v"] == 1
    assert payload["day"] == day
    assert isinstance(payload["pts"], list)
    assert isinstance(payload["hl"], str)
    stats = payload["stats"]
    assert {"active_human", "git_commits", "ai_sessions", "ai_messages",
            "files_changed", "app_top", "browser_categories"} <= set(stats)
    assert isinstance(stats["app_top"], list)
    assert isinstance(stats["browser_categories"], list)


def test_sentinel_size_caps(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-04"
    for _ in range(10):
        db.sample(day, "code.exe", "main.py - VS Code", 0)
    note = collector.build_note(day, db, _ocr_config())
    assert len(note) <= 16000                        # c) 合并文本上限（字符）
    raw = _sentinel_json_text(note)
    assert raw
    assert len(raw.encode("utf-8")) <= 6000         # JSON <= 6000 字节
    plain = note.split("<!--WORKLOG_TL_V1", 1)[0].rstrip("\n")
    assert len(plain.encode("utf-8")) <= 10000      # 纯文字 <= 10000 字节


def test_sample_timeline_app_switch_boundary(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-04"
    rows = [
        ("2026-11-04T09:00:00+08:00", "code.exe", "a.py", 0),
        ("2026-11-04T09:00:30+08:00", "code.exe", "a.py", 0),   # 同 app，跳过
        ("2026-11-04T09:01:00+08:00", "msedge.exe", "page", 0), # 切换，记点
        ("2026-11-04T09:01:30+08:00", "msedge.exe", "page", 0), # 同 app，跳过
        ("2026-11-04T09:02:00+08:00", "code.exe", "b.py", 0),   # 切换，记点
        ("2026-11-04T09:02:30+08:00", "code.exe", "", 0),       # 空标题，跳过
    ]
    with db._conn() as conn:
        conn.executemany(
            "INSERT INTO samples(ts,day,app,title,idle) VALUES(?,?,?,?,?)",
            [(ts, day, app, title, idle) for ts, app, title, idle in rows],
        )
    pts = collector.sample_timeline(db, day, 48)    # d) 真实 ts / app 切换边界
    assert pts == [
        {"t": "09:00:00", "app": "code.exe", "l": "a.py"},
        {"t": "09:01:00", "app": "msedge.exe", "l": "page"},
        {"t": "09:02:00", "app": "code.exe", "l": "b.py"},
    ]
    note = collector.build_note(day, db, _ocr_config())
    payload = _extract_sentinel(note)
    assert payload["pts"] == pts
    real_times = {ts.split("T", 1)[1][:8] for ts, *_ in rows}
    for point in payload["pts"]:
        assert point["t"] in real_times             # t 来自真实 ts 的时分秒


def test_sentinel_extreme_long_title_and_ocr(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    day = "2026-11-04"
    for i in range(60):
        db.sample(day, "code.exe", f"标题{i}-" + "窗口内容" * 30, 0)
    for i in range(300):
        db.add_ocr_sample(day, "code.exe", f"第{i}行 " + "屏幕文字线索" * 30)
    note = collector.build_note(day, db, _ocr_config(ocr_max_chars=4000))
    assert len(note) <= 16000                        # e) 极端长文本仍不超 16000
    assert "[worklog]" in note
    assert any(w in note for w in ("活跃时长", "浏览", "Git 提交", "文件改动", "AI 会话"))
    payload = _extract_sentinel(note)
    assert payload.get("v") == 1
    assert len(_sentinel_json_text(note).encode("utf-8")) <= 6000
    assert len(note.split("<!--WORKLOG_TL_V1", 1)[0].rstrip("\n").encode("utf-8")) <= 10000


def test_sentinel_empty_data_is_safe(tmp_path: Path) -> None:
    db = collector.WorklogDB(tmp_path / "worklog.sqlite3")
    db.init()
    note = collector.build_note("2026-11-04", db, _ocr_config())
    # f) 空数据整体省略哨兵且不报错
    assert note == ""
    assert _extract_sentinel(note) == {}
