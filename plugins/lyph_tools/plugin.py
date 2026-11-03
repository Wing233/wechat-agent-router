"""Public lyph domain tools: profile, published contents, company snapshot.

This plugin is the Router-side implementation of the six tools lyph used to run
locally. It is read-only by construction: every query goes through
lyph_data.LyphPublicData, which opens SQLite with mode=ro/query_only and
rejects anything that is not already public.

Tool results carry data/source/updated_at. UI blocks stay in lyph: the model
must never be handed presentation logic it could invent numbers from.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lyph_data import LyphPublicData, QueryError, default_enterprise_path

log = logging.getLogger("plugin.lyph_tools")


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProfileArguments(Arguments):
    section: Literal["all", "profile", "projects", "stack", "interests"] = "all"
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=8, ge=1, le=20)


class ContentSearchArguments(Arguments):
    query: str = Field(default="", max_length=200)
    type: Literal["all", "blog", "document", "manual", "automatic"] = "all"
    limit: int = Field(default=8, ge=1, le=20)


class ContentArguments(Arguments):
    content_id: str = Field(min_length=1, max_length=100)


class CompanySearchArguments(Arguments):
    query: str = Field(default="", max_length=200)
    province: str = Field(default="", max_length=80)
    city: str = Field(default="", max_length=80)
    industry: str = Field(default="", max_length=100)
    has_jobs: bool = False
    limit: int = Field(default=8, ge=1, le=20)
    page: int = Field(default=1, ge=1, le=100000)
    sort: Literal["name", "jobs_desc"] = "name"


class CompanyArguments(Arguments):
    company_id: str = Field(min_length=1, max_length=100)


class CompanyAggregateArguments(CompanySearchArguments):
    group_by: Literal["province", "city", "industry", "latest_round", "company_type"] = "province"
    metric: Literal["companies", "active_jobs"] = "companies"
    limit: int = Field(default=10, ge=1, le=20)


def _schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": False,
            "required": list(required)}


SEARCH_PROPS = {
    "query": {"type": "string", "maxLength": 200},
    "province": {"type": "string", "maxLength": 80},
    "city": {"type": "string", "maxLength": 80},
    "industry": {"type": "string", "maxLength": 100},
    "has_jobs": {"type": "boolean"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    "page": {"type": "integer", "minimum": 1, "maximum": 100000},
    "sort": {"type": "string", "enum": ["name", "jobs_desc"]},
}

TOOL_SPECS = (
    (
        "get_public_profile",
        "查询站主公开介绍、项目、技术栈或兴趣；只能读取已公开站点资料。",
        _schema({
            "section": {"type": "string", "enum": ["all", "profile", "projects", "stack", "interests"]},
            "query": {"type": "string", "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }),
        ProfileArguments,
        lambda data, args: data.profile(args.section, args.query, args.limit),
    ),
    (
        "search_public_content",
        "搜索已发布、公开且明确授权访客对话的博客、文档和日报；返回摘要及内容ID。",
        _schema({
            "query": {"type": "string", "maxLength": 200},
            "type": {"type": "string", "enum": ["all", "blog", "document", "manual", "automatic"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }),
        ContentSearchArguments,
        lambda data, args: data.content_search(args.query, args.type, args.limit),
    ),
    (
        "get_public_content",
        "按内容ID读取授权访客对话的已发布正文；长正文会截断并标记。",
        _schema({"content_id": {"type": "string", "minLength": 1, "maxLength": 100}}, ["content_id"]),
        ContentArguments,
        lambda data, args: data.content_detail(args.content_id),
    ),
    (
        "search_companies",
        "从已发布的企业只读快照查询企业；query按字面关键词搜索；省份/城市/行业为准确筛选。仅为快照，非实时全量。",
        _schema(SEARCH_PROPS),
        CompanySearchArguments,
        lambda data, args: data.company_search(args.model_dump()),
    ),
    (
        "get_company",
        "使用search_companies返回或当前选中的明确企业ID，读取该企业的公开快照资料。不能猜测企业ID；不返回实时岗位或私密字段。",
        _schema({"company_id": {"type": "string", "minLength": 1, "maxLength": 100}}, ["company_id"]),
        CompanyArguments,
        lambda data, args: data.company_detail(args.company_id),
    ),
    (
        "aggregate_companies",
        "对企业公开快照做数据库分组统计并返回分布；可按省份/城市/行业/轮次/类型分组，统计企业数或企业active_jobs字段合计（不是实时岗位总数）。",
        _schema({**SEARCH_PROPS,
                 "group_by": {"type": "string",
                              "enum": ["province", "city", "industry", "latest_round", "company_type"]},
                 "metric": {"type": "string", "enum": ["companies", "active_jobs"]}}),
        CompanyAggregateArguments,
        lambda data, args: data.company_aggregate(args.model_dump()),
    ),
)


def _error(code: str, message: str) -> dict:
    return {"data": None, "blocks": [], "source": None, "updated_at": None,
            "error": {"code": code, "message": message}}


def build(data: LyphPublicData, model, run):
    """Validate model arguments, then run one bounded read-only query."""

    def handler(arguments: dict) -> dict:
        try:
            validated = model.model_validate(arguments if isinstance(arguments, dict) else {})
        except ValidationError:
            return _error("invalid_arguments", "工具参数无效：请检查字段、类型、长度、枚举和条数限制。")
        try:
            return run(data, validated)
        except QueryError as exc:
            return _error(exc.code, exc.message)
        except Exception:
            log.warning("lyph_tools query failed", exc_info=True)
            return _error("unavailable", "数据源暂时不可用。")

    return handler


def apply(ctx) -> None:
    cfg = ctx.config or {}
    root = (ctx.root_config.get("lyph") or {}) if isinstance(ctx.root_config, dict) else {}
    db_path = str(cfg.get("db") or root.get("db") or "")
    enterprise = cfg.get("enterprise_db") or root.get("enterprise_db") or default_enterprise_path()
    if not db_path:
        log.warning("lyph_tools registered without lyph db path; every query will report unavailable")
    data = LyphPublicData(db_path or "/nonexistent/lyph.sqlite3", enterprise)
    log.info("lyph_tools ready db=%s enterprise=%s", db_path or "-", enterprise or "-")

    for name, description, parameters, model, run in TOOL_SPECS:
        ctx.tools.register(name, description, parameters, build(data, model, run))

    ctx.on_unload(lambda: log.info("lyph_tools unloaded"))
