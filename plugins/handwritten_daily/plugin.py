"""Handwritten daily report publishing for LYPH.

This plugin lets the Router (and therefore WeChat/QQ/Telegram/... channels) draft
and publish "handwritten" daily reports to LYPH's public content store, using the
same SSO path as plugins/music/lyph_sync.py. It never writes lyph's SQLite.

Writing workflow (the three skills are installed globally and should be used in
this order):
  1. samber/cc-skills@technical-article-writer  -> draft (title, hook, structure)
  2. ninehills/public-skills@deslop-zh            -> de-AI polish + fact check
  3. samber/developer-relations-skills@engineering-blog-post
                                                 -> only for deep postmortems
The publish tool below is the last step: it creates a draft and publishes it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .lyph_content import LyphContentClient

log = logging.getLogger("plugin.handwritten_daily")

GUIDE_TEXT = """手写日报写作与发布流程（在微信/QQ 等端自然语言触发）：

1. 先用 skill `technical-article-writer` 起草：按 Phase 1-7 走，标题 10 个候选、钩子、结构、配图建议。
2. 用 skill `deslop-zh` 去 AI 味并做事实核查：数字、边界、限制要可追溯。
3. 深度复盘/架构/性能稿，再启用 skill `engineering-blog-post`：claim ledger + credibility gate。
4. 写完后调用 `publish_handwritten_daily`，把 title + markdown 传入；visibility 默认 public，type 默认 manual。
5. 发布成功后，把 content_id 和 public_url 回给用户；如要撤回用 `withdraw_handwritten_daily`。

写作约束：简体中文；函数名/字段名/命令保持原样；不要编造事实；不要写 CTA/订阅框/营销话术；代码片段不超过 25 行；每篇必须有「做不到的部分」一节。"""


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def apply(ctx) -> None:
    cfg = ctx.config or {}
    root = ctx.root_config if isinstance(ctx.root_config, dict) else {}
    lyph_root = (root.get("lyph") or {}) if isinstance(root, dict) else {}

    api_url = str(cfg.get("lyph_api_url") or lyph_root.get("lyph_api_url") or "http://api-internal:8000")
    origin = str(cfg.get("lyph_origin") or lyph_root.get("lyph_origin") or "https://123.57.153.49")
    config_path = str(cfg.get("router_config_path") or "/app/config.json")
    timeout = int(cfg.get("timeout") or 30)

    try:
        client = LyphContentClient(config_path=config_path, api_url=api_url, origin=origin, timeout=timeout)
        client.auth()
        log.info("handwritten_daily ready api=%s origin=%s", api_url, origin)
    except Exception as exc:
        log.exception("handwritten_daily setup failed; tools will report unavailable")
        client = None
        setup_error = str(exc)[:300]
    else:
        setup_error = ""

    def _ensure_client() -> LyphContentClient | None:
        nonlocal client, setup_error
        if client is not None:
            return client
        try:
            client = LyphContentClient(config_path=config_path, api_url=api_url, origin=origin, timeout=timeout)
            client.auth()
            setup_error = ""
            return client
        except Exception as exc:
            setup_error = str(exc)[:300]
            return None

    def publish(args: dict) -> dict:
        c = _ensure_client()
        if c is None:
            return _error(f"LYPH 内容服务不可用：{setup_error}")
        title = str(args.get("title") or "").strip()
        markdown = str(args.get("markdown") or "").strip()
        if not title:
            return _error("缺少 title")
        if not markdown:
            return _error("缺少 markdown")
        if len(markdown) > 500000:
            return _error("markdown 超过 500000 字符")
        visibility = str(args.get("visibility") or "public").strip().lower()
        if visibility not in ("public", "private"):
            return _error("visibility 只能是 public 或 private")
        content_type = str(args.get("type") or "manual").strip().lower()
        if content_type not in ("blog", "document", "manual", "automatic", "weekly", "monthly"):
            return _error("type 不合法")
        summary = str(args.get("summary") or "").strip()
        if not summary:
            summary = markdown.replace("#", "").strip()[:160]
        category = str(args.get("category") or "").strip()
        tags = args.get("tags") or []
        if not isinstance(tags, list):
            return _error("tags 必须是字符串数组")
        tags = [str(t).strip() for t in tags if str(t).strip()][:30]
        date_value = str(args.get("date") or _today()).strip()
        try:
            from datetime import date as _date
            _date.fromisoformat(date_value)
        except ValueError:
            return _error("date 必须是 YYYY-MM-DD")

        payload = {
            "type": content_type,
            "title": title[:300],
            "summary": summary[:2000],
            "markdown": markdown,
            "date": date_value,
            "visibility": visibility,
            "rag_enabled": False,
            "category": category[:200],
            "tags": tags,
            "media_ids": [],
        }
        try:
            draft = c.create_draft(payload)
            content_id = draft["id"]
            revision = int(draft["revision"])
            published = c.publish(content_id, revision)
            revision = int(published["revision"])
        except Exception as exc:
            log.warning("publish_handwritten_daily failed", exc_info=True)
            return _error(f"发布失败：{str(exc)[:300]}")
        return {
            "ok": True,
            "content_id": content_id,
            "revision": revision,
            "type": content_type,
            "visibility": visibility,
            "published": True,
            "public_url": c.public_url(content_id),
            "note": "已创建草稿并发布。如需撤回，调用 withdraw_handwritten_daily。",
        }

    def list_items(args: dict) -> dict:
        c = _ensure_client()
        if c is None:
            return _error(f"LYPH 内容服务不可用：{setup_error}")
        limit = int(args.get("limit") or 20)
        limit = max(1, min(limit, 100))
        content_type = str(args.get("type") or "").strip().lower() or None
        try:
            items = c.list_contents(content_type=content_type, limit=limit)
        except Exception as exc:
            log.warning("list_handwritten_dailies failed", exc_info=True)
            return _error(f"查询失败：{str(exc)[:300]}")
        out = []
        for item in items:
            out.append({
                "id": item.get("id"),
                "title": item.get("title"),
                "type": item.get("type"),
                "date": item.get("date"),
                "visibility": item.get("visibility"),
                "published": bool(item.get("published") or item.get("published_version")),
                "summary": (item.get("summary") or "")[:120],
            })
        return {"ok": True, "items": out, "count": len(out)}

    def withdraw(args: dict) -> dict:
        c = _ensure_client()
        if c is None:
            return _error(f"LYPH 内容服务不可用：{setup_error}")
        content_id = str(args.get("content_id") or "").strip()
        if not content_id:
            return _error("缺少 content_id")
        revision = args.get("revision")
        if revision is None:
            try:
                detail = c.get(content_id)
                revision = int(detail["revision"])
            except Exception as exc:
                return _error(f"读取内容失败：{str(exc)[:300]}")
        try:
            result = c.withdraw(content_id, int(revision))
        except Exception as exc:
            log.warning("withdraw_handwritten_daily failed", exc_info=True)
            return _error(f"撤回失败：{str(exc)[:300]}")
        return {
            "ok": True,
            "content_id": content_id,
            "revision": int(result["revision"]),
            "published": False,
            "note": "已撤回发布，草稿仍保留。",
        }

    def guide(args: dict) -> dict:
        return {"ok": True, "guide": GUIDE_TEXT}

    ctx.tools.register(
        "publish_handwritten_daily",
        "发布一篇手写日报/技术分享到 LYPH 公开内容。调用前应先用 skill technical-article-writer 起草，再用 deslop-zh 去 AI 味与事实核查；深度复盘用 engineering-blog-post。"
        "当用户在微信/QQ 等端说“把这篇/这个日报发布到 lyph”“发布手写日报”“同步到站点”时调用。需要 title 和 markdown；visibility 默认 public，type 默认 manual。",
        _schema({
            "title": {"type": "string", "maxLength": 300},
            "markdown": {"type": "string", "maxLength": 500000},
            "summary": {"type": "string", "maxLength": 2000},
            "category": {"type": "string", "maxLength": 200},
            "tags": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 30},
            "date": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
            "visibility": {"type": "string", "enum": ["public", "private"]},
            "type": {"type": "string", "enum": ["blog", "document", "manual", "automatic", "weekly", "monthly"]},
        }, ["title", "markdown"]),
        publish,
        aliases=("publish_handwritten_daily",),
    )

    ctx.tools.register(
        "list_handwritten_dailies",
        "列出 LYPH 已发布/草稿的内容（默认 manual 类型，即手写日报）。用于发布前查重或发布后确认。",
        _schema({
            "type": {"type": "string", "enum": ["all", "blog", "document", "manual", "automatic", "weekly", "monthly"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }),
        list_items,
        aliases=("list_handwritten_dailies",),
    )

    ctx.tools.register(
        "withdraw_handwritten_daily",
        "撤回一篇已发布的手写日报（保留草稿）。需要 content_id；revision 可选，不传则自动读取当前版本。",
        _schema({
            "content_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "revision": {"type": "integer", "minimum": 1},
        }, ["content_id"]),
        withdraw,
        aliases=("withdraw_handwritten_daily",),
    )

    ctx.tools.register(
        "handwritten_daily_workflow",
        "返回手写日报的写作与发布流程（03 主笔 + 05 终稿 + 02 深度复盘），以及写作约束。在用户要求写/发布手写日报时先调用，再按流程起草。",
        _schema({}),
        guide,
        aliases=("handwritten_daily_workflow",),
    )

    ctx.log.info("handwritten_daily registered (api=%s)", api_url)
    ctx.on_unload(lambda: log.info("handwritten_daily unloaded"))