"""GitHub Trending fetching, daily digest and repository evaluation."""
from __future__ import annotations

import html
import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import trace_ctx
from data import CST

USER_AGENT = "WeChatAgentRouter/1.0 (+https://github.com/Wing233/wechat-agent-router)"
TRENDING_URL = "https://github.com/trending?since=daily"
API = "https://api.github.com"


class TrendingError(Exception):
    pass


def parse_stars(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip().lower().replace(",", "")
    match = re.search(r"([\d.]+)\s*([km]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    if match.group(2) == "k":
        number *= 1_000
    elif match.group(2) == "m":
        number *= 1_000_000
    return int(number)


def format_stars(value: Any) -> str:
    stars = parse_stars(value)
    if stars >= 1_000_000:
        return f"{stars / 1_000_000:.1f}m"
    if stars >= 1_000:
        return f"{stars / 1_000:.1f}k"
    return str(stars)


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_repo(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://github\.com/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^github\.com/", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if len(parts) >= 2:
        text = "/".join(parts[:2])
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def parse_daily_at(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not match:
        return 9, 0
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 9, 0
    return hour, minute


def next_daily_ts(hour: int = 9, minute: int = 0,
                  now: datetime | None = None) -> float:
    now = now or datetime.now(CST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def parse_trending_html(text: str) -> list[dict]:
    articles = re.findall(
        r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
        text or "", re.S)
    items: list[dict] = []
    for article in articles:
        repo_match = re.search(
            r'<h2[^>]*>.*?<a[^>]*href="/([^"/]+/[^"/]+)"', article, re.S)
        if not repo_match:
            continue
        repo = normalize_repo(repo_match.group(1))
        if not repo:
            continue
        desc_match = re.search(
            r'<p[^>]*class="[^"]*color-fg-muted[^"]*"[^>]*>(.*?)</p>',
            article, re.S)
        stars_match = re.search(
            r'<a[^>]*href="/[^"]+/stargazers"[^>]*>(.*?)</a>', article, re.S)
        lang_match = re.search(
            r'itemprop="programmingLanguage"[^>]*>(.*?)</span>', article, re.S)
        today_match = re.search(r"([\d,]+)\s+stars?\s+today", article, re.I)
        items.append({
            "repo": repo,
            "url": f"https://github.com/{repo}",
            "description": _clean_html(desc_match.group(1)) if desc_match else "",
            "stars": parse_stars(_clean_html(stars_match.group(1))) if stars_match else 0,
            "language": _clean_html(lang_match.group(1)) if lang_match else "",
            "today_stars": parse_stars(today_match.group(1)) if today_match else 0,
            "source": "trending",
        })
    return items


def resolve_repo(items: list[dict], index: Any = None, repo: str = "") -> str:
    if repo:
        normalized = normalize_repo(repo)
        if not normalized:
            raise TrendingError("repo 格式应为 owner/name 或 GitHub URL")
        return normalized
    if index is not None:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise TrendingError("index 必须是数字") from None
        if idx < 1 or idx > len(items):
            raise TrendingError(f"index 超出范围：1–{len(items)}")
        return str(items[idx - 1].get("repo") or "")
    raise TrendingError("需要 repo 或 index")


def build_digest_text(items: list[dict], overview: str, date: str) -> str:
    lines = [f"🧭 GitHub Trending · {date}"]
    if overview:
        lines.extend(["", overview.strip()])
    lines.append("")
    for index, item in enumerate(items, 1):
        suffix = " · 再次上榜" if item.get("seen_before") else ""
        lines.append(
            f"{index}. {item['repo']} ⭐ {format_stars(item.get('stars'))} · "
            f"{item.get('language') or '未知语言'}{suffix}")
        description = str(item.get("description") or "").strip()
        if description:
            if len(description) > 180:
                description = description[:177].rstrip() + "..."
            lines.append(f"   {description}")
        if item.get("today_stars"):
            lines.append(f"   今日 +{format_stars(item.get('today_stars'))}")
    lines.extend(["", "回复“评估 1”或“评估 owner/repo”让我深入分析；回复“看 1,3”也可以。"])
    return "\n".join(lines)


class TrendingStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS digests(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              digest_date TEXT NOT NULL,
              created_at TEXT NOT NULL,
              text TEXT NOT NULL,
              items_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS digests_date ON digests(digest_date);
            CREATE TABLE IF NOT EXISTS seen(
              repo TEXT PRIMARY KEY,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              times INTEGER NOT NULL DEFAULT 1
            );
            """)

    def save_digest(self, date: str, text: str, items: list[dict]) -> None:
        now = datetime.now(CST).isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO digests(digest_date,created_at,text,items_json) "
                "VALUES(?,?,?,?) ON CONFLICT(digest_date) DO UPDATE SET "
                "created_at=excluded.created_at,text=excluded.text,"
                "items_json=excluded.items_json",
                (date, now, text, json.dumps(items, ensure_ascii=False)))

    def latest(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        item = dict(row)
        item["items"] = json.loads(item.pop("items_json") or "[]")
        return item

    def seen_recent(self, days: int = 7) -> set[str]:
        cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat(
            timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT repo FROM seen WHERE last_seen>=?", (cutoff,)).fetchall()
        return {row["repo"] for row in rows}

    def mark_seen(self, repos: list[str]) -> None:
        now = datetime.now(CST).isoformat(timespec="seconds")
        with self._conn() as conn:
            for repo in repos:
                conn.execute(
                    "INSERT INTO seen(repo,first_seen,last_seen,times) "
                    "VALUES(?,?,?,1) ON CONFLICT(repo) DO UPDATE SET "
                    "last_seen=excluded.last_seen,times=seen.times+1",
                    (repo, now, now))

    def prune(self, days: int = 90) -> None:
        cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._conn() as conn:
            conn.execute("DELETE FROM digests WHERE digest_date<?", (cutoff,))


class TrendingService:
    def __init__(self, ctx):
        self.ctx = ctx
        job_opts = (ctx.root_config.get("jobs") or {}).get("github_trending") or {}
        self.count = max(5, min(int(ctx.config.get("count")
                                     or job_opts.get("count") or 12), 20))
        self.db = TrendingStore(
            str(ctx.config.get("db") or "/data/github_trending.db"))
        self.main_store = ctx.resource("store")
        try:
            self.token = str(ctx.secret("github_token") or "")
        except Exception:
            self.token = ""
        try:
            self.llm = ctx.resource("llm")
        except Exception:
            self.llm = None

    # --- fetching ------------------------------------------------------
    def fetch_trending(self) -> list[dict]:
        items: list[dict] = []
        try:
            status, body = self.ctx.http_request(
                "GET", TRENDING_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=45, limit=2_000_000)
            if status == 200:
                items = parse_trending_html(body)
        except Exception as exc:
            self.ctx.log.warning("github trending html failed: %s", exc)
        if len(items) < 3:
            try:
                items = self._search_fallback()
            except Exception as exc:
                self.ctx.log.warning("github trending fallback failed: %s", exc)
        elif len(items) < self.count:
            try:
                extra = self._search_fallback()
                known = {str(item.get("repo") or "").lower() for item in items}
                for item in extra:
                    repo = normalize_repo(str(item.get("repo") or ""))
                    if repo and repo.lower() not in known:
                        items.append(item)
                        known.add(repo.lower())
                    if len(items) >= self.count:
                        break
            except Exception as exc:
                self.ctx.log.warning("github trending fill fallback failed: %s", exc)
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in items:
            repo = normalize_repo(str(item.get("repo") or ""))
            if not repo or repo.lower() in seen:
                continue
            seen.add(repo.lower())
            item["repo"] = repo
            item["url"] = item.get("url") or f"https://github.com/{repo}"
            deduped.append(item)
            if len(deduped) >= self.count:
                break
        if not deduped:
            raise TrendingError("GitHub Trending 没有拿到数据")
        return deduped

    def _search_fallback(self) -> list[dict]:
        since = (datetime.now(CST) - timedelta(days=7)).strftime("%Y-%m-%d")
        query = urllib.parse.urlencode({
            "q": f"created:>={since} stars:>50",
            "sort": "stars", "order": "desc", "per_page": self.count,
        })
        body = self._api_get(f"/search/repositories?{query}", limit=300_000)
        data = json.loads(body)
        items = []
        for row in data.get("items") or []:
            items.append({
                "repo": row.get("full_name") or "",
                "url": row.get("html_url") or "",
                "description": row.get("description") or "",
                "stars": int(row.get("stargazers_count") or 0),
                "language": row.get("language") or "",
                "today_stars": 0,
                "source": "search",
            })
        return items

    def _api_get(self, path: str, *, accept: str = "application/vnd.github+json",
                 limit: int = 200_000) -> str:
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        status, body = self.ctx.http_request(
            "GET", API + path, headers=headers, timeout=30, limit=limit)
        if status >= 400:
            raise TrendingError(f"GitHub API {status}: {path}")
        return body

    # --- digest --------------------------------------------------------
    def _overview(self, items: list[dict]) -> str:
        if self.llm is None:
            return ""
        payload = [{
            "repo": item["repo"],
            "language": item.get("language") or "",
            "stars": item.get("stars") or 0,
            "description": item.get("description") or "",
        } for item in items]
        messages = [
            {"role": "system", "content": (
                "你是技术雷达编辑。根据 GitHub Trending 项目列表，用 2-3 句中文"
                "总结今天的技术趋势或共同主题。不要列项目清单，不要编造数据，"
                "不要 Markdown 表格。")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            with trace_ctx.request(role="owner") as trace_id:
                message = self.llm.complete(
                    messages, None, max_tokens=300, role="owner",
                    trace_id=trace_id)
            return str(message.get("content") or "").strip()[:600]
        except Exception as exc:
            self.ctx.log.warning("github trending overview failed: %s", exc)
            return ""

    def build_digest(self, items: list[dict]) -> dict:
        date = datetime.now(CST).strftime("%Y-%m-%d")
        seen = self.db.seen_recent(7)
        for item in items:
            item["seen_before"] = item["repo"] in seen
        overview = self._overview(items)
        text = build_digest_text(items, overview, date)
        return {"date": date, "text": text, "items": items,
                "overview": overview}

    def preview(self, refresh: bool = False) -> dict:
        today = datetime.now(CST).strftime("%Y-%m-%d")
        if not refresh:
            latest = self.db.latest()
            if latest and latest["digest_date"] == today:
                return {"date": today, "text": latest["text"],
                        "items": latest["items"], "cached": True}
        items = self.fetch_trending()
        digest = self.build_digest(items)
        self.db.save_digest(digest["date"], digest["text"], digest["items"])
        return digest

    def latest_list(self) -> dict:
        latest = self.db.latest()
        if not latest:
            return {"date": "", "items": []}
        items = []
        for index, item in enumerate(latest["items"], 1):
            items.append({
                "index": index,
                "repo": item["repo"],
                "stars": item.get("stars") or 0,
                "language": item.get("language") or "",
                "description": item.get("description") or "",
            })
        return {"date": latest["digest_date"], "items": items}

    def daily(self) -> dict:
        today = datetime.now(CST).strftime("%Y-%m-%d")
        if self.main_store.state_get("github_trending:last_sent") == today:
            return {"skipped": "already_sent", "date": today}
        items = self.fetch_trending()
        digest = self.build_digest(items)
        self.send_long(digest["text"])
        self.db.save_digest(digest["date"], digest["text"], digest["items"])
        self.db.mark_seen([item["repo"] for item in digest["items"]])
        self.main_store.state_set("github_trending:last_sent", today)
        self.db.prune()
        return {"date": today, "count": len(digest["items"]),
                "chars": len(digest["text"])}

    def send_long(self, text: str) -> None:
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 1200 and current:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks, 1):
            prefix = f"({index}/{len(chunks)})\n" if len(chunks) > 1 else ""
            self.ctx.send(prefix + chunk)

    # --- evaluation ----------------------------------------------------
    def evaluate(self, repo: str, focus: str = "both") -> dict:
        repo = normalize_repo(repo)
        if not repo:
            raise TrendingError("repo 格式应为 owner/name 或 GitHub URL")
        meta = json.loads(self._api_get(f"/repos/{repo}"))
        readme = ""
        try:
            readme = self._api_get(
                f"/repos/{repo}/readme",
                accept="application/vnd.github.v3.raw", limit=200_000)
        except Exception:
            readme = ""
        tree: list[dict] = []
        try:
            contents = json.loads(self._api_get(f"/repos/{repo}/contents"))
            tree = [{
                "name": item.get("name"),
                "type": item.get("type"),
                "size": item.get("size"),
            } for item in contents][:120]
        except Exception:
            tree = []
        names = [str(item.get("name") or "") for item in tree
                 if item.get("type") == "file"]
        manifests: dict[str, str] = {}
        for name in ("pyproject.toml", "package.json", "requirements.txt",
                     "go.mod", "Cargo.toml", "pom.xml", "build.gradle"):
            if name in names and len(manifests) < 3:
                try:
                    manifests[name] = self._api_get(
                        f"/repos/{repo}/contents/{name}",
                        accept="application/vnd.github.v3.raw",
                        limit=50_000)[:4000]
                except Exception:
                    continue
        license_info = meta.get("license") or {}
        return {
            "repo": repo,
            "url": meta.get("html_url") or f"https://github.com/{repo}",
            "description": meta.get("description") or "",
            "stars": int(meta.get("stargazers_count") or 0),
            "forks": int(meta.get("forks_count") or 0),
            "open_issues": int(meta.get("open_issues_count") or 0),
            "language": meta.get("language") or "",
            "license": license_info.get("spdx_id") or "",
            "topics": meta.get("topics") or [],
            "created_at": meta.get("created_at") or "",
            "pushed_at": meta.get("pushed_at") or "",
            "default_branch": meta.get("default_branch") or "",
            "readme_excerpt": readme[:8000],
            "tree": tree,
            "manifests": manifests,
            "focus": focus if focus in {"both", "borrow", "integrate"} else "both",
            "hint": (
                "结合当前 agent 架构（微信 Router、插件式工具、定时任务、"
                "gate/mandate 治理、记忆、MCP、Agent 派发）评估：值得借鉴什么、"
                "是否适合作为插件接入、风险/成本、建议的第一步。不要直接改代码。"
            ),
        }

    def evaluate_for_tool(self, args: dict) -> dict:
        repo = str(args.get("repo") or "")
        index = args.get("index")
        focus = str(args.get("focus") or "both")
        if not repo and index is None:
            return {"error": "需要 repo 或 index", "latest": self.latest_list()}
        if not repo:
            latest = self.db.latest()
            items = latest["items"] if latest else []
            repo = resolve_repo(items, index=index)
        return self.evaluate(repo, focus=focus)
