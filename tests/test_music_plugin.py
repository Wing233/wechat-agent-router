"""Contract tests for the read-only NetEase music plugin and bridge allowlist."""
from __future__ import annotations

import json
import logging
import sys

import pytest

from plugin_loader import ToolRegistrar
from plugins.music import plugin as music_plugin
from plugins.music.client import MusicError, build_command, extract_json
from plugins.music.readonly import ReadonlyError, validate_args
from registry import ToolRegistry


def test_allowlist_accepts_readonly_search():
    assert validate_args(["search", "song", "--keyword", "晴天", "--limit", "5"]) == [
        "search", "song", "--keyword", "晴天", "--limit", "5",
    ]


@pytest.mark.parametrize("args", [
    ["playlist", "create", "--playlistName", "test"],
    ["playlist", "add", "--playlistId", "x", "--songIdList", '["y"]'],
    ["song", "like", "--songId", "x"],
    ["play", "--song", "--encrypted-id", "x"],
    ["cloudupload", "upload", "a.mp3"],
    ["comment", "post", "--content", "hi"],
    ["note", "publish", "--content", "hi"],
    ["aisong", "generate", "--prompt", "hi"],
    ["login"],
])
def test_allowlist_rejects_write_and_playback(args):
    with pytest.raises(ReadonlyError):
        validate_args(args)


def test_allowlist_rejects_output_override_and_bad_limit():
    with pytest.raises(ReadonlyError):
        validate_args(["search", "song", "--keyword", "x", "--output", "yaml"])
    with pytest.raises(ReadonlyError):
        validate_args(["search", "song", "--keyword", "x", "--limit", "999"])
    with pytest.raises(ReadonlyError):
        validate_args(["search", "song", "--keyword", "x\nrm"])


def test_extract_json_skips_warning_prefix():
    payload = {"code": 200, "data": {"records": [{"name": "晴天"}]}}
    text = "[TokenStorage] 迁移失败，跳过: ENOENT\n" + json.dumps(payload, ensure_ascii=False)
    assert extract_json(text) == payload


def test_build_command_uses_argument_array():
    command = build_command(sys.executable, ["search", "song", "--keyword", "晴天"])
    assert command[0] == sys.executable
    assert command[-2:] == ["--output", "json"]


def test_http_transport_uses_bridge_payload():
    from plugins.music.client import MusicClient

    calls = []

    def fake_http(method, url, headers=None, body=None, timeout=10, limit=16000):
        calls.append((method, url, headers, json.loads(body.decode("utf-8"))))
        return 200, json.dumps({
            "ok": True,
            "data": {"code": 200, "data": {"records": [{"name": "晴天"}]}},
        })

    client = MusicClient(
        transport="http",
        bridge_url="http://host.docker.internal:18888",
        bridge_token="secret",
        http_request=fake_http,
    )
    payload = client.call(["search", "song", "--keyword", "晴天"])
    assert payload["data"]["records"][0]["name"] == "晴天"
    method, url, headers, body = calls[0]
    assert method == "POST"
    assert url.endswith("/run")
    assert headers["Authorization"] == "Bearer secret"
    assert body["args"][0:2] == ["search", "song"]


class FakeClient:
    def __init__(self, **_kwargs):
        pass

    def call(self, args):
        if args[:2] == ["search", "song"]:
            return {"code": 200, "data": {"records": [{
                "name": "晴天", "id": "ENC", "originalId": 123,
                "artists": [{"name": "周杰伦"}], "album": {"name": "叶惠美"},
                "duration": 269000, "visible": True, "liked": False,
            }]}}
        if args[:2] == ["song", "lyric"]:
            return {"code": 200, "data": {"lyric": "词" * 5000, "noLyric": False}}
        if args[:2] == ["user", "favorite"]:
            return {"code": 200, "data": {
                "name": "我喜欢的音乐", "id": "FAV", "originalId": 456,
                "trackCount": 10, "playCount": 2,
            }}
        if args[:2] == ["user", "info"]:
            return {"code": 200, "data": {
                "nickname": "tester", "signature": "hi", "originalId": 7,
                "vipDetail": [{"type": 1}],
            }}
        raise AssertionError(f"unexpected args: {args}")


class Ctx:
    def __init__(self):
        self.config = {"transport": "cli", "max_items": 20}
        self.root_config = {}
        self.log = logging.getLogger("test.music")
        self.registry = ToolRegistry()
        self.tools = ToolRegistrar(self.registry, "music")

    def http_request(self, *_args, **_kwargs):
        raise AssertionError("http_request should not be used in CLI tests")

    def on_unload(self, _fn):
        pass


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(music_plugin, "MusicClient", FakeClient)
    ctx = Ctx()
    music_plugin.apply(ctx)
    return ctx.registry


def test_search_tool_normalizes_song(tools):
    result = tools.execute("music__search", {"query": "晴天", "type": "song", "limit": 3})
    assert result["ok"] is True
    item = result["data"]["items"][0]
    assert item["name"] == "晴天"
    assert item["artists"] == ["周杰伦"]
    assert item["url"] == "https://music.163.com/#/song?id=123"


def test_lyric_tool_truncates(tools):
    result = tools.execute("music__lyric", {"song_id": "ENC"})
    assert result["ok"] is True
    assert len(result["data"]["lyric"]) == 4000


def test_favorite_and_profile_tools(tools):
    favorite = tools.execute("music__favorite_playlist", {})
    assert favorite["data"]["track_count"] == 10
    profile = tools.execute("music__user_profile", {})
    assert profile["data"]["nickname"] == "tester"
    assert profile["data"]["vip"] is True


def test_invalid_arguments_are_reported(tools):
    result = tools.execute("music__search", {"query": "", "type": "song"})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_music_error_is_reported(monkeypatch):
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        def call(self, _args):
            raise MusicError("login_required", "ncm-cli 还没有登录")

    monkeypatch.setattr(music_plugin, "MusicClient", FailingClient)
    ctx = Ctx()
    music_plugin.apply(ctx)
    result = ctx.registry.execute("music__user_profile", {})
    assert result["ok"] is False
    assert result["error"]["code"] == "login_required"
