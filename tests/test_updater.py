"""Offline tests for the desktop update checker."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

import updater


def test_version_key_orders_releases():
    assert updater._version_key("1.2.1") > updater._version_key("1.1.0")
    assert updater._version_key("v1.10.0") > updater._version_key("1.9.9")
    assert updater._version_key("") == (0,)


def test_check_marks_newer_release_available(monkeypatch):
    monkeypatch.setattr(updater, "_latest_tag", lambda timeout: "v9.9.9")
    result = updater.check()
    assert result["available"] is True
    assert result["latest"] == "9.9.9"
    assert result["state"] == "idle"


def test_check_marks_same_release_current(monkeypatch):
    monkeypatch.setattr(updater, "_latest_tag", lambda timeout: "v" + updater.VERSION)
    result = updater.check()
    assert result["available"] is False


def test_check_surfaces_errors(monkeypatch):
    def fail(timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(updater, "_latest_tag", fail)
    result = updater.check()
    assert result["state"] == "error"
    assert result["error"] == "boom"


def test_updater_script_uses_relative_paths(tmp_path):
    script = updater._updater_script(tmp_path / "WeChatAgentDesktop.exe")
    text = script.read_text(encoding="ascii")
    assert "%~dp0" in text
    assert "WeChatAgentDesktop.exe.new" in text
    assert ".WeChatAgentDesktop.exe.previous" in text
