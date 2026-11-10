"""Owner-only read-only NetEase Cloud Music tools backed by official ncm-cli.

The plugin never exposes playback or account writes. It talks to ncm-cli
directly when the Router runs on the same machine, or to a token-protected
local bridge (``scripts/ncm_bridge.py``) when the Router runs in Docker.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from registry import Job

from .client import MusicClient, MusicError
from .lyph_sync import LyphSync, MusicSync

log = logging.getLogger("plugin.music")

SEARCH_TYPES = ("song", "album", "playlist", "mv", "podcast", "voice", "all")


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "required": list(required),
    }


def _text(args: dict, key: str, maximum: int, *, required: bool = True) -> str:
    value = args.get(key)
    if value is None:
        if required:
            raise MusicError("invalid_arguments", f"{key} 不能为空。")
        return ""
    if not isinstance(value, str):
        raise MusicError("invalid_arguments", f"{key} 必须是字符串。")
    value = value.strip()
    if required and not value:
        raise MusicError("invalid_arguments", f"{key} 不能为空。")
    if len(value) > maximum:
        raise MusicError("invalid_arguments", f"{key} 不能超过 {maximum} 个字符。")
    return value


def _integer(args: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool):
        raise MusicError("invalid_arguments", f"{key} 必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MusicError("invalid_arguments", f"{key} 必须是整数。") from None
    if not minimum <= number <= maximum:
        raise MusicError("invalid_arguments", f"{key} 必须在 {minimum}..{maximum} 之间。")
    return number


def _choice(args: dict, key: str, default: str, choices: tuple[str, ...]) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or value not in choices:
        raise MusicError("invalid_arguments", f"{key} 必须是：{', '.join(choices)}。")
    return value


def _data(payload):
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _dict(payload) -> dict:
    data = _data(payload)
    return data if isinstance(data, dict) else {}


def _items(data, keys: tuple[str, ...] = ()) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (*keys, "records", "list", "songListVo", "songList", "songs", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _artists(item: dict) -> list[str]:
    values = item.get("artists") or item.get("fullArtists") or []
    result = []
    for artist in values:
        if isinstance(artist, dict) and artist.get("name"):
            result.append(str(artist["name"]))
        elif isinstance(artist, str) and artist:
            result.append(artist)
    return result


def _url(kind: str, original_id) -> str | None:
    if original_id in (None, "", 0):
        return None
    return f"https://music.163.com/#/{kind}?id={original_id}"


def _song(item: dict) -> dict:
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    return {
        "name": item.get("name"),
        "id": item.get("id"),
        "original_id": item.get("originalId"),
        "artists": _artists(item),
        "album": album.get("name"),
        "duration_ms": item.get("duration"),
        "visible": item.get("visible"),
        "playable": item.get("visible") is True,
        "liked": item.get("liked"),
        "url": _url("song", item.get("originalId")),
    }


def _playlist(item: dict) -> dict:
    return {
        "name": item.get("name"),
        "id": item.get("id"),
        "original_id": item.get("originalId"),
        "track_count": item.get("trackCount"),
        "play_count": item.get("playCount"),
        "creator": item.get("creatorNickName"),
        "special_type": item.get("specialType"),
        "url": _url("playlist", item.get("originalId")),
    }


def _album(item: dict) -> dict:
    return {
        "name": item.get("name"),
        "id": item.get("id"),
        "original_id": item.get("originalId"),
        "artists": _artists(item),
        "publish_time": item.get("publishTime"),
        "company": item.get("company"),
        "language": item.get("language"),
        "url": _url("album", item.get("originalId")),
    }


def _artist(item: dict) -> dict:
    return {
        "name": item.get("name"),
        "id": item.get("id"),
        "original_id": item.get("originalId"),
        "url": _url("artist", item.get("originalId")),
    }


def _mv(item: dict) -> dict:
    return {
        "name": item.get("name"),
        "mv_id": item.get("mvId"),
        "artist": item.get("artistName"),
        "play_count": item.get("playCount"),
        "publish_time": item.get("publishTime"),
        "cover_url": item.get("coverUrl"),
    }


def _podcast(item: dict) -> dict:
    return {
        "name": item.get("name"),
        "id": item.get("id") or item.get("originalId"),
        "original_id": item.get("originalId") or item.get("id"),
        "program_count": item.get("programCount"),
        "play_count": item.get("playCount"),
        "category": item.get("category"),
        "dj_name": item.get("djName"),
        "subscriber_count": item.get("subCnt"),
        "url": _url("djradio", item.get("originalId") or item.get("id")),
    }


def _voice(item: dict) -> dict:
    return {
        "name": item.get("name"),
        "id": item.get("encVoiceId") or item.get("id"),
        "original_id": item.get("id"),
        "duration_ms": item.get("duration"),
        "play_count": item.get("playCount"),
        "dj_name": item.get("djName"),
        "podcast_id": item.get("podcastId") or item.get("radioId"),
    }


def _cloud_song(item: dict) -> dict:
    return {
        "name": item.get("name") or item.get("songName"),
        "id": item.get("id") or item.get("songId"),
        "original_id": item.get("originalId"),
        "artist": item.get("artist") or item.get("artistName"),
        "album": item.get("album") or item.get("albumName"),
        "url": _url("song", item.get("originalId")),
    }


def _comment(item: dict) -> dict:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    return {
        "content": str(item.get("content") or "")[:500],
        "liked_count": item.get("likedCount"),
        "user": user.get("nickname"),
        "time": item.get("time"),
    }


def _topic(item: dict) -> dict:
    return {
        "name": item.get("name"),
        "id": item.get("id"),
        "read_num": item.get("readNum"),
        "sub_title": item.get("subTitle"),
    }


def _category(item: dict) -> dict:
    return {"name": item.get("name"), "id": item.get("id")}


class MusicTools:
    def __init__(self, client: MusicClient, max_items: int):
        self.client = client
        self.max_items = max_items

    def _run(self, args: list[str]) -> dict:
        return self.client.call(args)

    def _payload(self, args: list[str]):
        return _data(self._run(args))

    def search(self, args: dict) -> dict:
        query = _text(args, "query", 200)
        kind = _choice(args, "type", "song", SEARCH_TYPES)
        limit = _integer(args, "limit", 10, 1, min(20, self.max_items))
        if kind == "all":
            data = _dict(self._run(["search", "all", "--keyword", query]))
            take = min(5, limit)
            return {
                "query": query,
                "type": "all",
                "songs": [_song(item) for item in (data.get("songs") or [])[:take]],
                "artists": [_artist(item) for item in (data.get("artists") or [])[:take]],
                "albums": [_album(item) for item in (data.get("albums") or [])[:take]],
                "playlists": [_playlist(item) for item in (data.get("playlists") or [])[:take]],
            }
        data = self._payload(["search", kind, "--keyword", query, "--limit", str(limit), "--offset", "0"])
        normalizer = {
            "song": _song, "album": _album, "playlist": _playlist,
            "mv": _mv, "podcast": _podcast, "voice": _voice,
        }[kind]
        return {
            "query": query,
            "type": kind,
            "items": [normalizer(item) for item in _items(data)[:limit]],
        }

    def user_profile(self, _args: dict) -> dict:
        data = _dict(self._run(["user", "info"]))
        return {
            "nickname": data.get("nickname"),
            "signature": data.get("signature"),
            "user_id": data.get("originalId"),
            "vip": bool(data.get("vipDetail") or data.get("fullVipDetail")),
            "avatar_url": data.get("avatarUrl"),
        }

    def favorite_playlist(self, _args: dict) -> dict:
        return _playlist(_dict(self._run(["user", "favorite"])))

    def playlists(self, args: dict) -> dict:
        kind = _choice(args, "kind", "created", ("created", "collected"))
        limit = _integer(args, "limit", 20, 1, min(50, self.max_items))
        offset = _integer(args, "offset", 0, 0, 100_000)
        data = self._payload([
            "playlist", kind, "--limit", str(limit), "--offset", str(offset),
        ])
        return {
            "kind": kind,
            "items": [_playlist(item) for item in _items(data)[:limit]],
        }

    def playlist_detail(self, args: dict) -> dict:
        playlist_id = _text(args, "playlist_id", 100)
        return _playlist(_dict(self._run(["playlist", "get", "--playlistId", playlist_id])))

    def playlist_tracks(self, args: dict) -> dict:
        playlist_id = _text(args, "playlist_id", 100)
        limit = _integer(args, "limit", 30, 1, min(100, self.max_items))
        offset = _integer(args, "offset", 0, 0, 100_000)
        data = self._payload([
            "playlist", "tracks", "--playlistId", playlist_id,
            "--limit", str(limit), "--offset", str(offset),
        ])
        return {"playlist_id": playlist_id, "items": [_song(item) for item in _items(data)[:limit]]}

    def daily_recommend(self, args: dict) -> dict:
        limit = _integer(args, "limit", 20, 1, min(40, self.max_items))
        data = self._payload(["recommend", "daily", "--limit", str(limit)])
        return {"items": [_song(item) for item in _items(data)[:limit]]}

    def lyric(self, args: dict) -> dict:
        song_id = _text(args, "song_id", 100)
        data = _dict(self._run(["song", "lyric", "--songId", song_id]))
        return {
            "song_id": song_id,
            "no_lyric": data.get("noLyric"),
            "lyric": str(data.get("lyric") or "")[:4000],
            "translated_lyric": str(data.get("transLyric") or "")[:2000],
        }

    def recent_history(self, args: dict) -> dict:
        limit = _integer(args, "limit", 30, 1, min(50, self.max_items))
        data = self._payload(["user", "history", "--limit", str(limit)])
        return {"items": [_song(item) for item in _items(data)[:limit]]}

    def listen_ranking(self, args: dict) -> dict:
        kind = _integer(args, "type", 0, 0, 1)
        limit = _integer(args, "limit", 20, 1, min(50, self.max_items))
        data = self._payload([
            "user", "listen-ranking", "--type", str(kind), "--limit", str(limit),
        ])
        result = []
        for item in _items(data)[:limit]:
            song = item.get("song") if isinstance(item.get("song"), dict) else item
            result.append({
                "play_count": item.get("playCount"),
                "score": item.get("score"),
                "song": _song(song),
            })
        return {"type": kind, "items": result}

    def cloud_songs(self, args: dict) -> dict:
        limit = _integer(args, "limit", 30, 1, min(50, self.max_items))
        data = self._payload(["cloud", "list", "--limit", str(limit)])
        return {"items": [_cloud_song(item) for item in _items(data)[:limit]]}

    def album_tracks(self, args: dict) -> dict:
        album_id = _text(args, "album_id", 100)
        data = self._payload(["album", "tracks", "--albumId", album_id])
        return {"album_id": album_id,
                "items": [_song(item) for item in _items(data)[:self.max_items]]}

    def artist_songs(self, args: dict) -> dict:
        artist_id = _text(args, "artist_id", 100)
        limit = _integer(args, "limit", 20, 1, min(50, self.max_items))
        start = _integer(args, "start_time", 946684800000, 0, 4_102_444_800_000)
        end = _integer(args, "end_time", int(time.time() * 1000) + 86_400_000,
                       0, 4_102_444_800_000)
        if end <= start:
            raise MusicError("invalid_arguments", "end_time 必须大于 start_time。")
        data = self._payload([
            "artist", "songs", "--artistId", artist_id, "--limit", str(limit),
            "--offset", "0", "--startTime", str(start), "--endTime", str(end),
        ])
        return {"artist_id": artist_id, "items": [_song(item) for item in _items(data)[:limit]]}

    def podcast_categories(self, _args: dict) -> dict:
        data = self._payload(["podcast", "categories"])
        return {"items": [_category(item) for item in _items(data)]}

    def note_topics(self, args: dict) -> dict:
        keyword = _text(args, "keyword", 100)
        limit = _integer(args, "limit", 10, 1, min(20, self.max_items))
        data = _dict(self._run([
            "note", "topicSearch", "--keyword", keyword, "--limit", str(limit), "--offset", "0",
        ]))
        return {"keyword": keyword, "items": [_topic(item) for item in (data.get("topics") or [])[:limit]]}

    def hot_comments(self, args: dict) -> dict:
        kind = _choice(args, "type", "song", ("song", "playlist", "album"))
        resource_id = _text(args, "resource_id", 100)
        limit = _integer(args, "limit", 10, 1, min(20, self.max_items))
        data = self._payload([
            "comment", "list-hot", "--type", kind, "--resourceId", resource_id,
            "--limit", str(limit), "--offset", "0",
        ])
        return {"type": kind, "resource_id": resource_id,
                "items": [_comment(item) for item in _items(data)[:limit]]}

    def ai_song_balance(self, _args: dict) -> dict:
        data = _dict(self._run(["aisong", "pointBalance"]))
        return {"balance": data.get("balance")}


def _success(data) -> dict:
    return {
        "ok": True,
        "data": data,
        "source": "ncm-cli",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _safe(handler):
    def wrapped(arguments):
        try:
            return _success(handler(arguments if isinstance(arguments, dict) else {}))
        except MusicError as exc:
            return _error(exc.code, str(exc))
        except Exception:
            log.warning("music tool failed", exc_info=True)
            return _error("unavailable", "音乐数据源暂时不可用。")
    return wrapped


def apply(ctx) -> None:
    cfg = ctx.config or {}
    transport = str(cfg.get("transport") or ("http" if cfg.get("bridge_url") else "cli")).lower()
    if transport not in ("cli", "http"):
        raise RuntimeError("plugins.music.transport must be cli or http")
    client = MusicClient(
        transport=transport,
        binary=str(cfg.get("bin") or "ncm-cli"),
        bridge_url=str(cfg.get("bridge_url") or ""),
        bridge_token=str(cfg.get("bridge_token") or ""),
        timeout=int(cfg.get("timeout") or 30),
        http_request=ctx.http_request,
    )
    max_items = max(1, min(50, int(cfg.get("max_items") or 20)))
    tools = MusicTools(client, max_items)
    log.info("music ready transport=%s bin=%s bridge=%s",
             transport, cfg.get("bin") or "ncm-cli", cfg.get("bridge_url") or "-")

    if bool(cfg.get("sync_enabled", False)):
        try:
            lyph = LyphSync(
                config_path=str(cfg.get("router_config_path") or "/app/config.json"),
                api_url=str(cfg.get("lyph_api_url") or "http://api-internal:8000"),
                origin=str(cfg.get("lyph_origin") or "https://123.57.153.49"),
                timeout=int(cfg.get("timeout") or 30),
            )
            sync = MusicSync(
                client=client,
                tools=tools,
                lyph=lyph,
                recent_limit=int(cfg.get("recent_limit") or 10),
                knowledge_filename=str(cfg.get("knowledge_filename") or "netease-liked-songs.md"),
                knowledge_visibility=str(cfg.get("knowledge_visibility") or "public"),
                knowledge_rag=bool(cfg.get("knowledge_rag", True)),
                liked_interval_days=int(cfg.get("liked_interval_days") or 7),
            )
            store = ctx.resource("store")
            if not store.state_get("music:last_liked_sync"):
                store.state_set("music:last_liked_sync", str(time.time()))
            ctx.jobs.register(Job(
                id="music_sync",
                interval_minutes=60,
                handler=lambda: sync.run(store),
                enabled=True,
                skip_empty=False,
                window_minutes=60,
                meta={"type": "sync", "plugin": "music"},
            ))
            log.info("music sync registered hourly; liked interval=%s days",
                     int(cfg.get("liked_interval_days") or 7))
        except Exception:
            log.exception("music sync setup failed; read-only tools remain available")

    registrations = (
        ("search",
         "搜索网易云音乐的歌曲、专辑、歌单、MV、播客或声音。返回加密 ID、原始 ID 和 music.163.com 链接；visible=false 表示不可播放；只读，不播放。",
         _schema({
             "query": {"type": "string", "minLength": 1, "maxLength": 200},
             "type": {"type": "string", "enum": list(SEARCH_TYPES)},
             "limit": {"type": "integer", "minimum": 1, "maximum": 20},
         }, ("query",)),
         tools.search),
        ("user_profile",
         "读取当前登录的网易云账号资料：昵称、签名、用户 ID、VIP 状态。仅站主可用。",
         _schema({}), tools.user_profile),
        ("favorite_playlist",
         "读取当前账号的红心歌单「我喜欢的音乐」及其歌曲数。仅站主可用。",
         _schema({}), tools.favorite_playlist),
        ("playlists",
         "读取我创建或收藏的歌单列表。kind=created 为创建，collected 为收藏。",
         _schema({
             "kind": {"type": "string", "enum": ["created", "collected"]},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50},
             "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
         }), tools.playlists),
        ("playlist_detail",
         "按歌单加密 ID 读取歌单详情（名称、歌曲数、播放数、创建者）。",
         _schema({"playlist_id": {"type": "string", "minLength": 1, "maxLength": 100}},
                 ("playlist_id",)), tools.playlist_detail),
        ("playlist_tracks",
         "按歌单加密 ID 读取歌曲列表。歌单 ID 来自 search 或 playlists 返回的 id 字段。",
         _schema({
             "playlist_id": {"type": "string", "minLength": 1, "maxLength": 100},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100},
             "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
         }, ("playlist_id",)), tools.playlist_tracks),
        ("daily_recommend",
         "读取网易云每日推荐歌曲。",
         _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 40}}),
         tools.daily_recommend),
        ("lyric",
         "按歌曲加密 ID 读取歌词；返回内容截断到 4000 字，可能包含翻译歌词。",
         _schema({"song_id": {"type": "string", "minLength": 1, "maxLength": 100}},
                 ("song_id",)), tools.lyric),
        ("recent_history",
         "读取最近播放歌曲列表。",
         _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
         tools.recent_history),
        ("listen_ranking",
         "读取听歌排行；type=0 总排行，type=1 周排行。",
         _schema({
             "type": {"type": "integer", "enum": [0, 1]},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50},
         }), tools.listen_ranking),
        ("cloud_songs",
         "读取音乐云盘歌曲列表。",
         _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
         tools.cloud_songs),
        ("album_tracks",
         "按专辑加密 ID 读取专辑歌曲列表。",
         _schema({"album_id": {"type": "string", "minLength": 1, "maxLength": 100}},
                 ("album_id",)), tools.album_tracks),
        ("artist_songs",
         "按艺人加密 ID 读取歌曲列表；可传毫秒时间戳 start_time/end_time 限定发布时间。",
         _schema({
             "artist_id": {"type": "string", "minLength": 1, "maxLength": 100},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50},
             "start_time": {"type": "integer", "minimum": 0},
             "end_time": {"type": "integer", "minimum": 0},
         }, ("artist_id",)), tools.artist_songs),
        ("podcast_categories",
         "读取网易云播客一级分类列表。",
         _schema({}), tools.podcast_categories),
        ("note_topics",
         "搜索可用于笔记发布的话题；只读，不发布笔记。",
         _schema({
             "keyword": {"type": "string", "minLength": 1, "maxLength": 100},
             "limit": {"type": "integer", "minimum": 1, "maximum": 20},
         }, ("keyword",)), tools.note_topics),
        ("hot_comments",
         "读取歌曲、歌单或专辑的热门评论。type 为 song/playlist/album，resource_id 用加密 ID。",
         _schema({
             "type": {"type": "string", "enum": ["song", "playlist", "album"]},
             "resource_id": {"type": "string", "minLength": 1, "maxLength": 100},
             "limit": {"type": "integer", "minimum": 1, "maximum": 20},
         }, ("resource_id",)), tools.hot_comments),
        ("ai_song_balance",
         "读取 AI 写歌积分余额；只读，不生成歌曲。",
         _schema({}), tools.ai_song_balance),
    )

    for name, description, parameters, handler in registrations:
        ctx.tools.register(name, description, parameters, _safe(handler))

    ctx.on_unload(lambda: log.info("music plugin unloaded"))
