"""Offline checks for the GitHub Trending plugin parsers."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import CST
from plugins.github_trending.service import (
    build_digest_text,
    format_stars,
    next_daily_ts,
    normalize_repo,
    parse_daily_at,
    parse_stars,
    parse_trending_html,
    resolve_repo,
)

SAMPLE = """
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/acme/agent-kit">acme / agent-kit</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">
    A tiny agent toolkit for tool calling.
  </p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/acme/agent-kit/stargazers">12,345</a>
  <span>1,234 stars today</span>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/acme/ui-kit">acme / ui-kit</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Components for AI apps.</p>
  <span itemprop="programmingLanguage">TypeScript</span>
  <a href="/acme/ui-kit/stargazers">8.2k</a>
</article>
"""


def test_parse_stars() -> None:
    assert parse_stars("12,345") == 12345
    assert parse_stars("8.2k") == 8200
    assert parse_stars("1.5m") == 1_500_000
    assert parse_stars("") == 0
    assert format_stars(12345) == "12.3k"
    assert format_stars(999) == "999"


def test_parse_trending_html() -> None:
    items = parse_trending_html(SAMPLE)
    assert len(items) == 2
    first = items[0]
    assert first["repo"] == "acme/agent-kit"
    assert first["url"] == "https://github.com/acme/agent-kit"
    assert first["stars"] == 12345
    assert first["today_stars"] == 1234
    assert first["language"] == "Python"
    assert "tool calling" in first["description"]
    assert items[1]["repo"] == "acme/ui-kit"
    assert items[1]["stars"] == 8200


def test_normalize_repo() -> None:
    assert normalize_repo("acme/agent-kit") == "acme/agent-kit"
    assert normalize_repo("https://github.com/acme/agent-kit") == "acme/agent-kit"
    assert normalize_repo("https://github.com/acme/agent-kit/tree/main") == "acme/agent-kit"
    assert normalize_repo("acme/agent-kit.git") == "acme/agent-kit"
    assert normalize_repo("not a repo") == ""


def test_resolve_repo() -> None:
    items = [{"repo": "acme/one"}, {"repo": "acme/two"}]
    assert resolve_repo(items, index=1) == "acme/one"
    assert resolve_repo(items, index=2) == "acme/two"
    assert resolve_repo(items, repo="https://github.com/acme/three") == "acme/three"
    try:
        resolve_repo(items, index=3)
    except Exception as exc:
        assert "超出范围" in str(exc)
    else:
        raise AssertionError("expected range error")


def test_parse_daily_at() -> None:
    assert parse_daily_at("09:00") == (9, 0)
    assert parse_daily_at("9:30") == (9, 30)
    assert parse_daily_at("25:00") == (9, 0)
    assert parse_daily_at("bad") == (9, 0)


def test_next_daily_ts() -> None:
    morning = datetime(2026, 11, 1, 8, 0, tzinfo=CST)
    assert datetime.fromtimestamp(
        next_daily_ts(9, 0, morning), CST).day == 1
    evening = datetime(2026, 11, 1, 10, 0, tzinfo=CST)
    assert datetime.fromtimestamp(
        next_daily_ts(9, 0, evening), CST).day == 2


def test_build_digest_text() -> None:
    items = [
        {"repo": "acme/one", "stars": 12345, "language": "Python",
         "description": "A tiny toolkit.", "today_stars": 1234,
         "seen_before": False},
        {"repo": "acme/two", "stars": 8200, "language": "TypeScript",
         "description": "Components.", "today_stars": 0,
         "seen_before": True},
    ]
    text = build_digest_text(items, "今天主题是 agent 工具链。", "2026-11-01")
    assert "GitHub Trending · 2026-11-01" in text
    assert "1. acme/one" in text
    assert "12.3k" in text
    assert "2. acme/two" in text
    assert "再次上榜" in text
    assert "评估 1" in text
