"""Read-only historical sources for worklog backfill.

The collector only uploads aggregated facts: app names, project paths, git
subjects, browser domains and file counts. It never uploads raw browser URLs,
chat transcripts or file contents.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "dist", "build", "target", "data", ".runtime",
    "site-packages", ".next", ".cache",
}
_CODEX_CACHE: dict[str, dict] = {}
_FILE_CACHE: dict[tuple, dict[str, Counter]] = {}


def _local_day(ts: datetime) -> str:
    return ts.astimezone().date().isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout or ""


def collect_git(day: str, repos: list[str], authors: list[str] | None = None,
                exclude_authors: list[str] | None = None) -> list[dict]:
    """Return commits authored on ``day`` from the configured repositories."""
    start = f"{day} 00:00:00"
    end = f"{day} 23:59:59"
    wanted = [str(item).lower() for item in (authors or []) if str(item).strip()]
    blocked = [str(item).lower() for item in (exclude_authors or []) if str(item).strip()]
    items: list[dict] = []
    for repo in repos or []:
        root = Path(repo)
        if not (root / ".git").exists():
            continue
        output = _run([
            "git", "-C", str(root), "log",
            f"--since={start}", f"--until={end}",
            "--date=iso-strict", "--pretty=format:%h%x09%ad%x09%an%x09%s",
        ])
        for line in output.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            sha, stamp, author, subject = parts
            author_l = author.lower()
            if wanted and not any(word in author_l for word in wanted):
                continue
            if any(word in author_l for word in blocked):
                continue
            items.append({
                "repo": root.name,
                "sha": sha[:10],
                "time": stamp,
                "author": author,
                "subject": subject[:160],
            })
    return items


class CodexIndex:
    """Lazy index of Codex JSONL sessions, grouped by local date."""

    def __init__(self, root: str | None = None):
        self.root = Path(root or (Path.home() / ".codex" / "sessions"))
        self._key = str(self.root)
        self._days: dict[str, dict] | None = _CODEX_CACHE.get(self._key)

    def load(self) -> dict[str, dict]:
        if self._days is not None:
            return self._days
        days: dict[str, dict] = defaultdict(lambda: {
            "cwd": Counter(), "sessions": set(), "messages": 0, "events": 0,
        })
        files = glob.glob(str(self.root / "**" / "*.jsonl"), recursive=True)
        for name in files:
            try:
                session = Path(name).name
                with open(name, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        stamp = _parse_iso(entry.get("timestamp") or entry.get("ts") or "")
                        if stamp is None:
                            continue
                        day = _local_day(stamp)
                        bucket = days[day]
                        bucket["events"] += 1
                        bucket["sessions"].add(session)
                        payload = entry.get("payload") or {}
                        if isinstance(payload, dict):
                            cwd = payload.get("cwd")
                            if isinstance(cwd, str) and cwd.strip():
                                bucket["cwd"][cwd.strip()] += 1
                            if (payload.get("type") == "message"
                                    and str(payload.get("role") or "") == "user"):
                                bucket["messages"] += 1
            except OSError:
                continue
        self._days = dict(days)
        _CODEX_CACHE[self._key] = self._days
        return self._days

    def day(self, day: str) -> dict:
        return self.load().get(day) or {
            "cwd": Counter(), "sessions": set(), "messages": 0, "events": 0,
        }


def _chrome_time(dt: datetime) -> int:
    return int((dt.timestamp() + 11644473600) * 1_000_000)


def _browser_category(host: str) -> str:
    host = host.lower()
    if any(word in host for word in ("github", "gitlab", "gitee", "bitbucket")):
        return "代码托管"
    if "leetcode" in host:
        return "刷题"
    if any(word in host for word in ("stackoverflow", "mozilla", "python.org",
                                      "docs.", "readthedocs", "npmjs", "pypi")):
        return "技术文档"
    if any(word in host for word in ("volcengine", "aliyun", "tencent",
                                      "cloudflare", "render.com", "vercel",
                                      "backblaze", "tidbcloud", "aws.amazon",
                                      "azure.com", "console.")):
        return "云服务/部署"
    if any(word in host for word in ("mail.", "outlook", "qq.com", "weixin")):
        return "邮件/沟通"
    if any(word in host for word in ("bilibili", "youtube", "coursera",
                                      "udemy", "juejin", "zhihu")):
        return "学习/视频"
    return "其他浏览"


def collect_browser(day: str, profiles: list[dict] | None = None,
                    limit: int = 12) -> list[dict]:
    """Return visited-domain categories for a day; raw URLs/titles are discarded."""
    if not profiles:
        return []
    start = datetime.fromisoformat(day + "T00:00:00").astimezone()
    end = start + timedelta(days=1)
    categories: Counter = Counter()
    for profile in profiles:
        path = Path(str(profile.get("path") or ""))
        if not path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5)
            rows = conn.execute(
                "SELECT urls.url FROM visits JOIN urls ON visits.url=urls.id "
                "WHERE visits.visit_time >= ? AND visits.visit_time < ?",
                (_chrome_time(start), _chrome_time(end)),
            ).fetchall()
            conn.close()
        except Exception:
            continue
        for (url,) in rows:
            try:
                host = (urlparse(str(url)).hostname or "").lower()
            except Exception:
                continue
            if not host:
                continue
            host = host[4:] if host.startswith("www.") else host
            if any(host == item or host.endswith("." + item)
                   for item in profile.get("exclude_domains") or []):
                continue
            categories[_browser_category(host)] += 1
    return [{"category": name, "visits": count}
            for name, count in categories.most_common(max(1, limit))]


class FileIndex:
    """Walk configured roots once and group modified files by local date."""

    def __init__(self, roots: list[str], exclude_projects: list[str] | None = None):
        self.roots = [str(item) for item in (roots or [])]
        self.excluded = {str(item).lower() for item in (exclude_projects or [])}
        self._key = (tuple(self.roots), tuple(sorted(self.excluded)))
        self._days: dict[str, Counter] | None = _FILE_CACHE.get(self._key)

    def load(self) -> dict[str, Counter]:
        if self._days is not None:
            return self._days
        days: dict[str, Counter] = defaultdict(Counter)
        seen = 0
        for raw in self.roots:
            root = Path(str(raw))
            if not root.exists():
                continue
            stack = [root]
            while stack and seen < 200_000:
                folder = stack.pop()
                try:
                    entries = list(os.scandir(folder))
                except OSError:
                    continue
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in EXCLUDE_DIRS and not entry.name.startswith("."):
                                stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        seen += 1
                        stat = entry.stat(follow_symlinks=False)
                        day = datetime.fromtimestamp(stat.st_mtime).astimezone().date().isoformat()
                        rel = Path(entry.path).relative_to(root)
                        first = rel.parts[0] if len(rel.parts) > 1 else root.name
                        project = first if root.name.lower() == "wechatbot" else f"{root.name}/{first}"
                        if project.lower() not in self.excluded:
                            days[day][project] += 1
                    except OSError:
                        continue
        self._days = dict(days)
        _FILE_CACHE[self._key] = self._days
        return self._days

    def day(self, day: str, limit: int = 12) -> list[dict]:
        counts = self.load().get(day) or Counter()
        return [{"project": name, "files": count}
                for name, count in counts.most_common(max(1, limit))]


def collect_files(day: str, roots: list[str] | None = None,
                  limit: int = 12, exclude_projects: list[str] | None = None) -> list[dict]:
    """Count files modified on ``day``, grouped by project directory."""
    return FileIndex(roots or [], exclude_projects).day(day, limit)


def collect_workbuddy(day: str, db_path: str | None = None) -> list[dict]:
    """Optional: WorkBuddy session titles/cwd counts for one day."""
    path = Path(db_path or (Path.home() / ".workbuddy" / "workbuddy.db"))
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            "SELECT cwd, title FROM sessions "
            "WHERE date(created_at/1000,'unixepoch','localtime')=?",
            (day,),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    result = []
    for cwd, title in rows:
        result.append({"cwd": str(cwd or "")[:200], "title": str(title or "")[:120]})
    return result


def collect_evidence(day: str, config: dict) -> dict:
    sources = config.get("sources") or {}
    git_cfg = sources.get("git") or {}
    browser_cfg = sources.get("browser") or {}
    files_cfg = sources.get("files") or {}
    codex_cfg = sources.get("codex") or {}
    workbuddy_cfg = sources.get("workbuddy") or {}

    codex = CodexIndex(codex_cfg.get("root")) if codex_cfg.get("enabled", True) else None
    codex_day = codex.day(day) if codex else {"cwd": Counter(), "sessions": set(),
                                              "messages": 0, "events": 0}
    return {
        "git": collect_git(
            day, git_cfg.get("repos") or [], git_cfg.get("authors") or [],
            git_cfg.get("exclude_authors") or [],
        ) if git_cfg.get("enabled", True) else [],
        "browser": collect_browser(day, browser_cfg.get("profiles") or [])
        if browser_cfg.get("enabled", True) else [],
        "files": collect_files(day, files_cfg.get("roots") or [],
                               exclude_projects=files_cfg.get("exclude_projects") or [])
        if files_cfg.get("enabled", True) else [],
        "codex": {
            "sessions": len(codex_day["sessions"]),
            "messages": int(codex_day["messages"]),
            "events": int(codex_day["events"]),
            "cwd": codex_day["cwd"].most_common(8),
        },
        "workbuddy": collect_workbuddy(day, workbuddy_cfg.get("db"))
        if workbuddy_cfg.get("enabled", False) else [],
    }
