"""Contract tests for the Router-side lyph public tools.

These mirror lyph/api/tests/test_chat_tools.py: the point is that the migrated
implementation keeps the same public boundary (allowlists, visibility gates,
literal matching, bounded results) after the move.
"""
import json
import sqlite3

import pytest

from lyph_data import LyphPublicData, QueryError
from plugins.lyph_tools import plugin as lyph_plugin

STAMP = "2026-10-25T11:50:29+08:00"


@pytest.fixture
def public_data(tmp_path):
    site_db = tmp_path / "site.sqlite"
    enterprise_db = tmp_path / "published.sqlite"
    with sqlite3.connect(site_db) as conn:
        conn.executescript("""
            CREATE TABLE site_settings(id INTEGER PRIMARY KEY,payload_json TEXT,updated_at TEXT);
            CREATE TABLE contents(id TEXT PRIMARY KEY,draft_json TEXT,published_json TEXT,updated_at TEXT);
            CREATE TABLE gaming_accounts(platform TEXT PRIMARY KEY,account_id TEXT NOT NULL,display_name TEXT NOT NULL,
              credential_enc TEXT NOT NULL,snapshot_json TEXT,publish_enabled INTEGER NOT NULL DEFAULT 0,
              connected_at TEXT NOT NULL,synced_at TEXT,link_method TEXT);
            CREATE TABLE maimemo_config(id INTEGER PRIMARY KEY CHECK(id=1),token_enc TEXT NOT NULL,
              publish_enabled INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,synced_at TEXT,last_error TEXT);
            CREATE TABLE maimemo_daily(day TEXT PRIMARY KEY,finished INTEGER NOT NULL,target INTEGER NOT NULL,
              study_time_ms INTEGER NOT NULL,new_words INTEGER NOT NULL,unfinished INTEGER NOT NULL,
              plan_words INTEGER NOT NULL,truncated INTEGER NOT NULL DEFAULT 0,captured_at TEXT NOT NULL);
        """)
        site = {
            "profile": {"name": "lyph", "bio": "公开介绍", "secret": "hidden-profile-key"},
            "projects": [{"id": "one", "title": "真实项目", "description": "可查项目",
                          "api_key": "hidden-project-key", "url": "javascript:alert(1)",
                          "image": "https://example.com/project.png"}],
            "stack": [{"name": "FastAPI", "description": "提供接口"}],
            "interests": [{"name": "手工兴趣", "description": "公开"}],
            "secret": "hidden-site-key", "contact": {"owner_secret": "hidden-contact-key"},
        }
        conn.execute("INSERT INTO site_settings VALUES(1,?,?)", (json.dumps(site), STAMP))
        for identifier, visibility, rag, published, override in [
            ("allowed", "public", True, True, {}),
            ("private", "private", True, True, {}),
            ("not-rag", "public", False, True, {}),
            ("draft", "public", True, False, {}),
            ("revoked", "public", True, True, {"rag_enabled": False}),
        ]:
            payload = {"title": "共同关键词 " + identifier, "summary": "摘要",
                       "markdown": "已发布正文 " + identifier, "type": "blog",
                       "date": "2026-10-25", "category": "技术",
                       "visibility": visibility, "rag_enabled": rag}
            draft = dict(payload, markdown="未发布的秘密草稿", **override)
            conn.execute("INSERT INTO contents VALUES(?,?,?,?)",
                         (identifier, json.dumps(draft), json.dumps(payload) if published else None, STAMP))
        snapshot = {"scope": "近两周", "source": "Steam 官方接口", "items": [
            {"app_id": 10, "name": "已公开游戏", "played_minutes_2weeks": 120, "total_minutes": 600,
             "url": "https://store.steampowered.com/app/10"}]}
        conn.execute(
            "INSERT INTO gaming_accounts VALUES('steam','1','站主',?,?,1,?,?,?)",
            ("ENCRYPTED-BLOB", json.dumps(snapshot), STAMP, STAMP, "manual_unverified"))
        conn.execute(
            "INSERT INTO maimemo_config VALUES(1,'ENCRYPTED-MAIMEMO-TOKEN',1,?,?,NULL)",
            (STAMP, STAMP))
        conn.execute(
            "INSERT INTO maimemo_daily VALUES('2026-11-09',10,32405,66365,4,144,8315,0,?)",
            (STAMP,))
        conn.execute(
            "INSERT INTO maimemo_daily VALUES('2026-11-08',12,0,80000,3,120,8310,0,?)",
            (STAMP,))
    with sqlite3.connect(enterprise_db) as conn:
        conn.executescript("""
            CREATE TABLE company(id INTEGER PRIMARY KEY,name TEXT,name_en TEXT,status TEXT,province TEXT,
              city TEXT,industry TEXT,website TEXT,description TEXT,search_text TEXT,active_jobs INTEGER,
              latest_round TEXT,company_type TEXT,primary_source TEXT,primary_source_url TEXT,updated_at TEXT,secret TEXT);
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        """)
        conn.execute("INSERT INTO meta VALUES('built_at',?)", (STAMP,))
        for identifier, province, city, industry, jobs in [
            (1, "北京", "北京", "软件", 3), (2, "北京", "北京", "硬件", 2),
            (3, "上海", "上海", "软件", 4), (4, None, None, "软件", 0), (5, "", "", "其他", 0),
        ]:
            conn.execute("INSERT INTO company VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (identifier, "企业%d" % identifier, None, "存续", province, city, industry,
                          "https://example.com", "公开企业说明", "企业%d %s" % (identifier, industry), jobs,
                          "A轮", "vc_portfolio", "公开来源", "https://example.com/source", STAMP,
                          "do-not-expose-unknown-column"))
    return LyphPublicData(str(site_db), str(enterprise_db))


def test_profile_keeps_the_public_allowlist_and_sanitizes_urls(public_data):
    result = public_data.profile("all", "", 8)
    data = result["data"]
    assert data["profile"] == {"name": "lyph", "bio": "公开介绍"}
    assert data["projects"]["items"][0]["title"] == "真实项目"
    assert "api_key" not in data["projects"]["items"][0]
    assert "url" not in data["projects"]["items"][0]
    assert data["projects"]["items"][0]["image"] == "https://example.com/project.png"
    encoded = json.dumps(data, ensure_ascii=False)
    for secret in ("hidden-profile-key", "hidden-project-key", "hidden-site-key", "owner_secret"):
        assert secret not in encoded
    assert result["source"] == "lyph 公开站点资料"
    assert result["updated_at"] == STAMP


def test_profile_merges_only_published_games_and_never_reads_credentials(public_data):
    data = public_data.profile("interests", "", 8)["data"]
    names = [item["name"] for item in data["interests"]["items"]]
    assert "已公开游戏" in names and "手工兴趣" in names
    game = next(item for item in data["interests"]["items"] if item["name"] == "已公开游戏")
    assert "未验证档案" in game["category"]
    assert game["recent_minutes"] == 120 and game["total_minutes"] == 600
    assert "ENCRYPTED-BLOB" not in json.dumps(data, ensure_ascii=False)


def test_profile_study_returns_only_published_aggregates(public_data):
    study = public_data.profile("study", "", 8)["data"]["study"]
    assert study["enabled"] is True
    assert study["plan_words"] == 8315
    assert study["today"]["finished"] == 10
    assert study["trend"][-1]["day"] == "2026-11-09"
    encoded = json.dumps(study, ensure_ascii=False)
    assert "ENCRYPTED-MAIMEMO-TOKEN" not in encoded
    assert "不是累计或已掌握词汇量" in encoded


def test_profile_study_hides_when_not_published(public_data):
    database = public_data.db_path
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE maimemo_config SET publish_enabled=0 WHERE id=1")
    study = public_data.profile("study", "", 8)["data"]["study"]
    assert study == {"enabled": False}


def test_content_search_only_returns_published_and_rag_enabled(public_data):
    data = public_data.content_search("共同关键词", "all", 8)["data"]
    assert [item["id"] for item in data["items"]] == ["allowed"]
    assert data["total"] == 1
    assert "未发布的秘密草稿" not in json.dumps(data, ensure_ascii=False)


def test_content_detail_rejects_private_and_marks_truncation(public_data):
    assert public_data.content_detail("allowed")["data"]["markdown"] == "已发布正文 allowed"
    for identifier in ("private", "not-rag", "draft", "revoked", "missing"):
        with pytest.raises(QueryError) as caught:
            public_data.content_detail(identifier)
        assert caught.value.code == "not_found"
        assert "SELECT" not in caught.value.message


def test_company_search_filters_literally_and_reports_paging(public_data):
    data = public_data.company_search({"city": "北京市", "limit": 1, "page": 1})["data"]
    assert data["total"] == 2 and len(data["items"]) == 1 and data["has_more"] is True
    assert data["items"][0]["province"] == "北京"
    assert "secret" not in data["items"][0]
    wildcard = public_data.company_search({"query": "%", "limit": 20})["data"]
    assert wildcard["total"] == 0
    jobs = public_data.company_search({"sort": "jobs_desc", "limit": 3})["data"]
    assert [item["active_jobs"] for item in jobs["items"]] == [4, 3, 2]


def test_company_detail_is_exact_and_hides_unknown_columns(public_data):
    item = public_data.company_detail("1")["data"]["item"]
    assert item["name"] == "企业1" and item["description"] == "公开企业说明"
    assert "secret" not in item
    with pytest.raises(QueryError) as caught:
        public_data.company_detail("999")
    assert caught.value.code == "not_found"


def test_aggregate_totals_come_from_sqlite(public_data):
    data = public_data.company_aggregate({"group_by": "industry", "metric": "companies", "limit": 2})["data"]
    assert data["total"] == 5 and data["group_count"] == 3
    assert data["rows"][0] == {"label": "软件", "value": 3}
    assert data["displayed_total"] + data["other_total"] == data["total"]
    jobs = public_data.company_aggregate({"group_by": "industry", "metric": "active_jobs"})["data"]
    assert jobs["total"] == 9


def test_missing_or_incompatible_sources_are_unavailable(tmp_path):
    missing = LyphPublicData(str(tmp_path / "nope.sqlite"), None)
    with pytest.raises(QueryError) as caught:
        missing.profile()
    assert caught.value.code == "unavailable"
    with pytest.raises(QueryError) as caught:
        missing.company_search({})
    assert caught.value.code == "unavailable"

    broken = tmp_path / "broken.sqlite"
    with sqlite3.connect(broken) as conn:
        conn.execute("CREATE TABLE company(id INTEGER)")
        conn.execute("CREATE TABLE meta(key TEXT,value TEXT)")
    with pytest.raises(QueryError) as caught:
        LyphPublicData(str(tmp_path / "nope.sqlite"), str(broken)).company_detail("1")
    assert caught.value.code == "unavailable"
    assert "sqlite" not in caught.value.message.lower()


def test_handlers_validate_arguments_and_return_coded_errors(public_data):
    specs = {name: (model, run) for name, _, _, model, run in lyph_plugin.TOOL_SPECS}
    handler = lyph_plugin.build(public_data, *specs["search_companies"])
    bad = handler({"limit": 999})
    assert bad["error"]["code"] == "invalid_arguments" and bad["data"] is None
    unknown = handler({"query": "企业", "nope": 1})
    assert unknown["error"]["code"] == "invalid_arguments"
    good = handler({"query": "企业1"})
    assert good["error"] is None and good["data"]["total"] == 1


class _Registrar:
    def __init__(self):
        self.names = []

    def register(self, name, description, parameters, handler, aliases=(), tags=()):
        self.names.append(name)


class _Ctx:
    def __init__(self, root):
        self.tools = _Registrar()
        self.config = {}
        self.root_config = root

    def on_unload(self, fn):
        self.unloaded = fn


def test_plugin_registers_prefixed_read_only_tools(tmp_path):
    db = tmp_path / "lyph.sqlite3"
    sqlite3.connect(db).close()
    ctx = _Ctx({"lyph": {"db": str(db), "enterprise_db": None}})
    lyph_plugin.apply(ctx)
    assert sorted(ctx.tools.names) == sorted([
        "get_public_profile", "search_public_content", "get_public_content",
        "search_companies", "get_company", "aggregate_companies",
    ])


def test_manifest_and_entry_load_through_the_real_plugin_loader(tmp_path):
    import shutil
    from pathlib import Path

    from plugin_loader import PluginLoader
    from registry import ToolRegistry

    source = Path(__file__).resolve().parents[1] / "plugins" / "lyph_tools"
    target = tmp_path / "plugins" / "lyph_tools"
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    registry = ToolRegistry()
    db = tmp_path / "lyph.sqlite3"
    sqlite3.connect(db).close()
    loader = PluginLoader(
        tmp_path / "plugins", registry, [], {},
        {"lyph": {"db": str(db), "enterprise_db": None}},
        lambda text: True,
    )
    loader.load_all()
    assert loader.errors == {}
    assert "lyph_tools" in loader.loaded
    assert sorted(registry.names()) == sorted([
        "lyph_tools__aggregate_companies", "lyph_tools__get_company",
        "lyph_tools__get_public_content", "lyph_tools__get_public_profile",
        "lyph_tools__search_companies", "lyph_tools__search_public_content",
    ])


def test_missing_db_path_fails_the_plugin_load(tmp_path):
    import shutil
    from pathlib import Path

    from plugin_loader import PluginLoader
    from registry import ToolRegistry

    source = Path(__file__).resolve().parents[1] / "plugins" / "lyph_tools"
    target = tmp_path / "plugins" / "lyph_tools"
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    registry = ToolRegistry()
    loader = PluginLoader(
        tmp_path / "plugins", registry, [], {},
        {"jobs": {}, "plugins": {}},          # loader root_config without lyph
        lambda text: True,
    )
    loader.load_all()
    assert "lyph_tools" in loader.errors
    assert not [name for name in registry.names() if name.startswith("lyph_tools__")]

