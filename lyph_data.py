"""Read-only access to lyph's public data: site settings, contents, company snapshot.

Mirrors the query semantics of lyph's api/lyph/chat_tools.py so the Router
becomes the single implementation of those six tools. Every connection is
read-only, query-only, extension-disabled and deadline-guarded; model
arguments never become SQL identifiers or URLs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

QUERY_TIMEOUT_SECONDS = 2.0
SITE_SOURCE = "lyph 公开站点资料"
CONTENT_SOURCE = "lyph 已发布且已授权对话的内容"
ENTERPRISE_SOURCE = "发掘企业公开快照（非实时、非全量）"

PUBLIC_WHERE = (
    "published_json IS NOT NULL"
    " AND json_extract(published_json,'$.visibility')='public'"
    " AND json_extract(draft_json,'$.visibility')='public'"
)
CONTENT_WHERE = (
    PUBLIC_WHERE
    + " AND json_extract(published_json,'$.rag_enabled')=1"
    + " AND json_extract(draft_json,'$.rag_enabled')=1"
)

COMPANY_REQUIRED = {
    "id", "name", "province", "city", "industry", "search_text",
    "active_jobs", "latest_round", "company_type", "updated_at",
}


class QueryError(Exception):
    """Safe, user-facing failure; never carries internal detail."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _number(value):
    """Snapshot fields are sometimes strings; keep only finite numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str) and len(value) <= 20:
        try:
            number = float(value)
        except ValueError:
            return None
        return number if number == number and abs(number) != float("inf") else None
    return None


@contextmanager
def _readonly(path: Path):
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        raise QueryError("unavailable", "数据源尚未配置或暂时不可用。") from None
    if not resolved.is_file():
        raise QueryError("unavailable", "数据源尚未配置或暂时不可用。")
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=QUERY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.enable_load_extension(False)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
        yield conn
    finally:
        conn.close()


def _text(value, maximum: int = 2000) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def _public_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        if ".." in parsed.path.split("/") or parsed.path.startswith(("/admin", "/collector", "/api/v1/admin")):
            return None
        return value
    if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not parsed.password:
        return value
    return None


def _safe_id(value: str) -> str:
    return quote(value, safe="")


class LyphPublicData:
    def __init__(self, db_path: str, enterprise_path: str | None = None) -> None:
        self.db_path = Path(db_path)
        self.enterprise_path = Path(enterprise_path) if enterprise_path else None

    @staticmethod
    def _result(data, source: str, updated_at):
        # Uniform envelope: error is only filled on failure, so callers can
        # branch on one key instead of guessing whether it exists.
        return {"data": data, "source": source, "updated_at": updated_at, "error": None}

    # --- site profile -------------------------------------------------
    def profile(self, section: str = "all", query: str = "", limit: int = 8) -> dict:
        with _readonly(self.db_path) as conn:
            row = conn.execute("SELECT payload_json,updated_at FROM site_settings WHERE id=1").fetchone()
            gaming_items = self._public_games(conn) if section in {"all", "interests"} else []
        if not row:
            raise QueryError("unavailable", "公开站点资料暂时不可用。")
        try:
            site = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            raise QueryError("unavailable", "公开站点资料格式不兼容。") from None
        if not isinstance(site, dict):
            raise QueryError("unavailable", "公开站点资料格式不兼容。")

        result = {}
        sections = ["profile", "projects", "stack", "interests"] if section == "all" else [section]
        needle = query.casefold()
        for name in sections:
            raw = site.get(name)
            if name == "profile":
                result[name] = {
                    key: _text(raw.get(key), 8000)
                    for key in ("name", "bio", "introduction", "hero")
                    if isinstance(raw, dict) and isinstance(raw.get(key), str)
                }
                continue
            items = list(raw) if isinstance(raw, list) else []
            if name == "interests":
                items = items + list(gaming_items)
            entries = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                clean = {
                    key: (_text(item[key], 12000 if key == "markdown" else 2000)
                          if isinstance(item.get(key), str) else item[key])
                    for key in ("id", "title", "name", "description", "summary", "status", "category", "stack",
                                "markdown", "platform", "recent_minutes", "total_minutes", "earned_achievements",
                                "current_gamerscore", "max_gamerscore", "data_scope", "source", "updated_at")
                    if isinstance(item.get(key), (str, int, float))
                }
                for key in ("url", "image"):
                    url = _public_url(item.get(key))
                    if url:
                        clean[key] = url
                if needle and needle not in " ".join(str(value) for value in clean.values()).casefold():
                    continue
                entries.append(clean)
            result[name] = {"items": entries[:limit], "total": len(entries)}
        return self._result(result, SITE_SOURCE, row["updated_at"])

    def _public_games(self, conn) -> list:
        """Published game snapshots; credentials and keys never leave lyph."""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gaming_accounts'"
        ).fetchone()
        if not exists:
            return []
        try:
            rows = conn.execute(
                "SELECT platform,account_id,display_name,snapshot_json,synced_at,link_method"
                " FROM gaming_accounts WHERE publish_enabled=1 AND snapshot_json IS NOT NULL"
                " ORDER BY platform"
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        entries = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except (ValueError, TypeError):
                continue
            if not isinstance(snapshot, dict):
                continue
            if row["platform"] == "steam" and not row["link_method"]:
                # Without the derived label we cannot prove how the profile was
                # linked, and guessing would misstate a public disclosure.
                continue
            notice = ("站主手动指定的公开档案，未经过 Steam 登录验证。"
                      if row["platform"] == "steam" and row["link_method"] == "manual_unverified" else "")
            for item in snapshot.get("items", [])[:8]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    continue
                name = item["name"][:160]
                if row["platform"] == "steam":
                    recent = _number(item.get("played_minutes_2weeks"))
                    total = _number(item.get("total_minutes"))
                    description = (f"近两周记录 {recent / 60:.1f} 小时 · 累计记录 {total / 60:.1f} 小时。"
                                   "数据来自 Steam 官方接口。")
                    if notice:
                        description += " " + notice
                    entry = {
                        "id": "game-steam-" + str(item.get("app_id", "")), "name": name,
                        "description": description,
                        "category": "Steam · 近期游玩" + (" · 未验证档案" if notice else ""),
                        "platform": "Steam", "recent_minutes": recent, "total_minutes": total,
                        "data_scope": snapshot.get("scope", ""), "source": snapshot.get("source", ""),
                        "updated_at": row["synced_at"] or snapshot.get("synced_at"),
                    }
                    if notice:
                        entry["identity_notice"] = notice
                    if isinstance(item.get("url"), str) and item["url"].startswith("https://store.steampowered.com/"):
                        entry["url"] = item["url"]
                    if isinstance(item.get("image"), str) and item["image"].startswith("https://media.steampowered.com/"):
                        entry["image"] = item["image"]
                else:
                    achievements = _number(item.get("achievements_earned"))
                    current = _number(item.get("gamerscore_current"))
                    maximum = _number(item.get("gamerscore_max"))
                    entry = {
                        "id": "game-xbox-" + str(item.get("title_id", "")), "name": name,
                        "description": (f"成就 {achievements} 项 · 玩家分数 {current}/{maximum}。"
                                        "仅覆盖 Xbox 接口首批最多 50 项中的最近游戏。"),
                        "category": "Xbox · 成就进度", "platform": "Xbox",
                        "earned_achievements": achievements, "current_gamerscore": current,
                        "max_gamerscore": maximum, "data_scope": snapshot.get("scope", ""),
                        "source": snapshot.get("source", ""),
                        "updated_at": row["synced_at"] or snapshot.get("synced_at"),
                    }
                    if isinstance(item.get("url"), str) and item["url"].startswith("https://www.xbox.com/"):
                        entry["url"] = item["url"]
                entries.append(entry)
        return entries

    # --- contents -----------------------------------------------------
    def content_search(self, query: str = "", content_type: str = "all", limit: int = 8) -> dict:
        conditions, values = [CONTENT_WHERE], []
        if content_type != "all":
            conditions.append("json_extract(published_json,'$.type')=?")
            values.append(content_type)
        if query:
            conditions.append(
                "(instr(lower(json_extract(published_json,'$.title')),lower(?))>0"
                " OR instr(lower(json_extract(published_json,'$.summary')),lower(?))>0"
                " OR instr(lower(json_extract(published_json,'$.markdown')),lower(?))>0)"
            )
            values.extend([query] * 3)
        where = " AND ".join(conditions)
        with _readonly(self.db_path) as conn:
            total = conn.execute("SELECT count(*) FROM contents WHERE " + where, values).fetchone()[0]
            rows = conn.execute(
                "SELECT id,published_json,updated_at FROM contents WHERE " + where
                + " ORDER BY json_extract(published_json,'$.date') DESC,id LIMIT ?",
                [*values, limit],
            ).fetchall()
        items = [self._content_item(row) for row in rows]
        updated = max((item["updated_at"] for item in items), default=None)
        return self._result({"items": items, "total": total}, CONTENT_SOURCE, updated)

    @staticmethod
    def _content_item(row, body: bool = False) -> dict:
        try:
            payload = json.loads(row["published_json"])
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        item = {key: _text(payload.get(key), 2000) for key in ("title", "summary", "date", "category", "type")}
        item.update(id=row["id"], url="/entry?id=" + _safe_id(row["id"]), updated_at=row["updated_at"])
        if body:
            markdown = _text(payload.get("markdown"), 500000)
            item.update(markdown=markdown[:30000], truncated=len(markdown) > 30000)
        return item

    def content_detail(self, content_id: str) -> dict:
        with _readonly(self.db_path) as conn:
            row = conn.execute(
                "SELECT id,published_json,updated_at FROM contents WHERE " + CONTENT_WHERE + " AND id=?",
                (content_id,),
            ).fetchone()
        if not row:
            raise QueryError("not_found", "内容不存在或尚未授权用于访客对话。")
        item = self._content_item(row, body=True)
        return self._result(item, CONTENT_SOURCE, item["updated_at"])

    # --- company snapshot ---------------------------------------------
    @contextmanager
    def _enterprise(self):
        if self.enterprise_path is None:
            raise QueryError("unavailable", "企业公开快照尚未配置。")
        with _readonly(self.enterprise_path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"company", "meta"}.issubset(tables):
                raise QueryError("unavailable", "企业公开快照格式不兼容，请重新发布导出快照。")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(company)")}
            if not COMPANY_REQUIRED.issubset(columns):
                raise QueryError("unavailable", "企业公开快照字段不兼容，请重新发布导出快照。")
            stamp = conn.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
            if not stamp:
                raise QueryError("unavailable", "企业数据缺少公开快照构建时间。")
            yield conn, columns, _text(stamp[0], 100)

    @staticmethod
    def _company_where(arguments: dict):
        conditions, values = [], []
        query = arguments.get("query") or ""
        if query:
            conditions.append("instr(lower(coalesce(search_text,'') || ' ' || coalesce(name,'')),lower(?))>0")
            values.append(query)
        for key in ("province", "city", "industry"):
            value = arguments.get(key) or ""
            if value:
                if key in {"province", "city"}:
                    value = value.removesuffix("省").removesuffix("市")
                conditions.append(key + "=?")
                values.append(value)
        if arguments.get("has_jobs"):
            conditions.append("coalesce(active_jobs,0)>0")
        return " AND ".join(conditions) or "1=1", values

    @staticmethod
    def _clean_company(row, selected, description_limit: int = 2000) -> dict:
        item = {
            key: (_text(row[key], description_limit if key == "description" else 2000)
                  if isinstance(row[key], str) else row[key])
            for key in selected
        }
        for key in ("website", "primary_source_url"):
            if key in item:
                item[key] = _public_url(item[key])
        return item

    def company_search(self, arguments: dict) -> dict:
        where, values = self._company_where(arguments)
        limit = int(arguments.get("limit") or 8)
        page = int(arguments.get("page") or 1)
        sort = arguments.get("sort") or "name"
        with self._enterprise() as (conn, columns, stamp):
            selected = [
                key for key in ("id", "name", "name_en", "status", "province", "city", "industry", "website",
                                "description", "latest_round", "investor_names", "list_names", "tags", "active_jobs",
                                "primary_source", "primary_source_url", "updated_at")
                if key in columns
            ]
            total = conn.execute("SELECT count(*) FROM company WHERE " + where, values).fetchone()[0]
            order = "coalesce(active_jobs,0) DESC,name,id" if sort == "jobs_desc" else "name,id"
            rows = conn.execute(
                "SELECT " + ",".join(selected) + " FROM company WHERE " + where + " ORDER BY " + order
                + " LIMIT ? OFFSET ?",
                [*values, limit, (page - 1) * limit],
            ).fetchall()
        items = [self._clean_company(row, selected) for row in rows]
        return self._result({
            "items": items, "total": total, "page": page, "limit": limit,
            "has_more": page * limit < total, "snapshot": True,
        }, ENTERPRISE_SOURCE, stamp)

    def company_detail(self, company_id: str) -> dict:
        with self._enterprise() as (conn, columns, stamp):
            selected = [
                key for key in ("id", "name", "name_en", "status", "province", "city", "industry", "website",
                                "description", "latest_round", "active_jobs", "primary_source",
                                "primary_source_url", "updated_at")
                if key in columns
            ]
            row = conn.execute(
                "SELECT " + ",".join(selected) + " FROM company WHERE id=?", (company_id,)
            ).fetchone()
        if not row:
            raise QueryError("not_found", "企业不存在于当前公开快照。")
        item = self._clean_company(row, selected, description_limit=12000)
        return self._result({
            "item": item, "snapshot": True,
            "note": "岗位数使用企业 active_jobs 字段，不代表实时岗位明细或实时在招总量。",
        }, ENTERPRISE_SOURCE, stamp)

    def company_aggregate(self, arguments: dict) -> dict:
        where, values = self._company_where(arguments)
        group_by = arguments.get("group_by") or "province"
        metric_name = arguments.get("metric") or "companies"
        limit = int(arguments.get("limit") or 10)
        label = "coalesce(nullif(trim(" + group_by + "),''),'未知')"
        metric = "count(*)" if metric_name == "companies" else "sum(max(coalesce(active_jobs,0),0))"
        with self._enterprise() as (conn, _, stamp):
            total = conn.execute("SELECT " + metric + " FROM company WHERE " + where, values).fetchone()[0] or 0
            rows = [dict(row) for row in conn.execute(
                "SELECT " + label + " AS label," + metric + " AS value FROM company WHERE " + where
                + " GROUP BY " + label + " ORDER BY value DESC,label LIMIT ?",
                [*values, limit],
            )]
            group_count = conn.execute(
                "SELECT count(DISTINCT " + label + ") FROM company WHERE " + where, values
            ).fetchone()[0]
        displayed_total = sum(row["value"] for row in rows)
        note = "仅统计已发布快照；未知字段独立分组，Top 分组未覆盖部分计入 other_total。"
        if metric_name == "active_jobs":
            note += "岗位数按企业 active_jobs 字段求和，可能不同于岗位明细条数，不代表当前实时在招岗位总数。"
        return self._result({
            "group_by": group_by, "metric": metric_name, "rows": rows, "total": total,
            "group_count": group_count, "displayed_total": displayed_total,
            "other_total": total - displayed_total, "snapshot": True, "note": note,
        }, ENTERPRISE_SOURCE, stamp)


def default_enterprise_path():
    configured = os.getenv("LYPH_ENTERPRISE_DATABASE_PATH")
    if configured:
        return configured
    candidate = Path("/lyph-src/public-apps/companies/data/web.sqlite")
    return str(candidate) if candidate.is_file() else None
