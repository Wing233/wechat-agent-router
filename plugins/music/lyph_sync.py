"""Scheduled music -> lyph synchronization.

The job lives in the Router (integration owner). It uses the Router's SSO
credentials to call lyph's admin API; it never writes lyph's SQLite database
directly.

- hourly: replace the ten `ncm-history-*` interests with the latest plays
- weekly: rebuild the public liked-songs knowledge source
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests
import sso_auth


def _data(payload):
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _items(payload):
    data = _data(payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("records", "list", "songListVo", "songList", "songs"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _song(item: dict) -> dict:
    artists = [a.get("name") for a in (item.get("artists") or [])
               if isinstance(a, dict) and a.get("name")]
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    original = item.get("originalId")
    return {
        "name": item.get("name") or "",
        "artists": artists,
        "album": album.get("name") or "",
        "duration_ms": item.get("duration"),
        "original_id": original,
        "encrypted_id": item.get("id"),
        "visible": item.get("visible"),
        "url": f"https://music.163.com/#/song?id={original}" if original else "",
    }


def _markdown(songs: list[dict]) -> str:
    lines = [
        "# 网易云音乐红心歌单",
        "",
        f"歌曲数：{len(songs)}",
        "来源：网易云音乐官方 ncm-cli（账号红心歌单）",
        f"同步时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 歌曲",
        "",
    ]
    for index, item in enumerate(songs, 1):
        artists = "、".join(item["artists"]) or "未知艺人"
        album = item["album"] or "未知专辑"
        duration = ""
        if isinstance(item.get("duration_ms"), (int, float)):
            seconds = int(item["duration_ms"] / 1000)
            duration = f" · {seconds // 60}:{seconds % 60:02d}"
        visible = "" if item.get("visible") is not False else " · 不可播放"
        url = f" · {item['url']}" if item.get("url") else ""
        lines.append(f"{index}. {item['name']} — {artists} — {album}{duration}{visible}{url}")
    return "\n".join(lines) + "\n"


class LyphSync:
    def __init__(self, *, config_path: str, api_url: str, origin: str, timeout: int = 60):
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.cookie = sso_auth.issue_session(self.config)
        if not self.cookie:
            raise RuntimeError("could not issue lyph SSO session")
        self.cookie_name = sso_auth.cookie_name(self.config)
        self.api_url = api_url.rstrip("/")
        self.origin = origin.rstrip("/")
        self.timeout = max(5, int(timeout))
        self.session = requests.Session()
        self.csrf = ""

    def auth(self) -> None:
        response = self.session.get(
            f"{self.api_url}/api/v1/auth/session",
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("authenticated"):
            raise RuntimeError("lyph SSO authentication failed")
        self.csrf = str(data.get("csrf_token") or "")
        if not self.csrf:
            raise RuntimeError("lyph SSO did not return a CSRF token")

    def _headers(self, csrf: str = "") -> dict:
        headers = {
            "Cookie": f"{self.cookie_name}={self.cookie}",
            "X-SSO-Verified": sso_auth.edge_header(self.config, self.cookie),
            "Origin": self.origin,
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return headers

    def update_interests(self, entries: list[dict]) -> dict:
        if not entries:
            return {"updated": 0, "skipped": "no recent plays"}
        for attempt in range(2):
            response = self.session.get(
                f"{self.api_url}/api/v1/admin/settings",
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            site = payload["site"]
            existing = [
                item for item in (site.get("interests") or [])
                if isinstance(item, dict)
                and not str(item.get("id") or "").startswith("ncm-history-")
            ]
            if len(existing) + len(entries) > 100:
                raise RuntimeError("lyph interests would exceed 100 entries")
            site["interests"] = existing + entries
            saved = self.session.put(
                f"{self.api_url}/api/v1/admin/settings",
                json={"revision": payload["revision"], "site": site},
                headers=self._headers(self.csrf),
                timeout=self.timeout,
            )
            if saved.status_code == 409 and attempt == 0:
                continue
            saved.raise_for_status()
            return {"updated": len(entries), "total": len(site["interests"])}
        raise RuntimeError("lyph settings revision changed repeatedly")

    def replace_knowledge(self, filename: str, data: bytes, *, visibility: str = "public",
                          rag_enabled: bool = True) -> dict:
        listing = self.session.get(
            f"{self.api_url}/api/v1/admin/knowledge",
            headers=self._headers(),
            timeout=self.timeout,
        )
        listing.raise_for_status()
        old_ids = [
            item["id"] for item in listing.json().get("items") or []
            if item.get("filename") == filename
        ]
        uploaded = self.session.post(
            f"{self.api_url}/api/v1/admin/knowledge",
            headers=self._headers(self.csrf),
            files={"file": (filename, data, "text/markdown")},
            data={"visibility": visibility, "rag_enabled": str(bool(rag_enabled)).lower()},
            timeout=max(120, self.timeout),
        )
        uploaded.raise_for_status()
        source = uploaded.json()
        source_id = source["id"]
        status = source.get("status")
        for _ in range(90):
            if status in ("ready", "failed"):
                break
            time.sleep(10)
            detail = self.session.get(
                f"{self.api_url}/api/v1/admin/knowledge/{source_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
            detail.raise_for_status()
            row = detail.json()
            status = row.get("status")
            if status == "failed":
                raise RuntimeError(f"knowledge indexing failed: {row.get('error')}")
        if status != "ready":
            raise RuntimeError(f"knowledge indexing timed out: {status}")
        for old_id in old_ids:
            if old_id == source_id:
                continue
            deleted = self.session.delete(
                f"{self.api_url}/api/v1/admin/knowledge/{old_id}",
                headers=self._headers(self.csrf),
                timeout=self.timeout,
            )
            deleted.raise_for_status()
        return {"source_id": source_id, "status": status, "deleted_old": len(old_ids)}


class MusicSync:
    def __init__(self, *, client, tools, lyph: LyphSync, recent_limit: int = 10,
                 knowledge_filename: str = "netease-liked-songs.md",
                 knowledge_visibility: str = "public", knowledge_rag: bool = True,
                 liked_interval_days: int = 7):
        self.client = client
        self.tools = tools
        self.lyph = lyph
        self.recent_limit = max(1, min(20, int(recent_limit)))
        self.knowledge_filename = knowledge_filename
        self.knowledge_visibility = knowledge_visibility
        self.knowledge_rag = knowledge_rag
        self.liked_interval_seconds = max(1, int(liked_interval_days)) * 86400

    def sync_recent(self) -> dict:
        result = self.tools.recent_history({"limit": self.recent_limit})
        items = result.get("items") or []
        today = time.strftime("%Y-%m-%d")
        entries = []
        for item in items:
            original = item.get("original_id")
            artists = "、".join(item.get("artists") or []) or "未知艺人"
            album = item.get("album") or "未知专辑"
            entries.append({
                "id": f"ncm-history-{original or item.get('id') or len(entries)}",
                "name": item.get("name") or "未知歌曲",
                "description": f"{artists} · {album} · 网易云音乐最近播放",
                "category": "网易云音乐 · 最近播放",
                "url": item.get("url") or "",
                "source": "网易云音乐 ncm-cli",
                "updated_at": today,
            })
        return self.lyph.update_interests(entries)

    def sync_liked(self) -> dict:
        favorite = _data(self.client.call(["user", "favorite"]))
        playlist_id = favorite.get("id") if isinstance(favorite, dict) else ""
        total = int(favorite.get("trackCount") or 0) if isinstance(favorite, dict) else 0
        if not playlist_id:
            raise RuntimeError("favorite playlist id is missing")
        songs: list[dict] = []
        offset = 0
        while offset < total:
            payload = self.client.call([
                "playlist", "tracks", "--playlistId", playlist_id,
                "--limit", "200", "--offset", str(offset),
            ])
            page = [_song(item) for item in _items(payload)]
            if not page:
                break
            songs.extend(page)
            offset += len(page)
            if len(page) < 200:
                break
        seen = set()
        unique = []
        for item in songs:
            key = item.get("original_id") or item.get("encrypted_id") or item.get("name")
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        data = _markdown(unique).encode("utf-8")
        result = self.lyph.replace_knowledge(
            self.knowledge_filename, data,
            visibility=self.knowledge_visibility,
            rag_enabled=self.knowledge_rag,
        )
        result["songs"] = len(unique)
        return result

    def run(self, store) -> dict:
        self.lyph.auth()
        recent = self.sync_recent()
        now = time.time()
        last = float(store.state_get("music:last_liked_sync") or 0)
        liked = {"skipped": "not due"}
        if now - last >= self.liked_interval_seconds:
            liked = self.sync_liked()
            store.state_set("music:last_liked_sync", str(now))
        return {"recent": recent, "liked": liked}
