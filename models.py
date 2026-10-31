"""Model catalog and role bindings.

The router used to have exactly one chat model plus an optional fallback. That
is too rigid once the same bot has to do cheap scheduled summaries, vision,
judging and occasional expensive reasoning. This module keeps a small catalog
in ``config.json`` and resolves a *purpose* (``chat``, ``vision``, ``judge``,
``job.default``, ``plugin.default``, ``model:<id>``) to an ordered provider
list. Order matters: the first healthy model wins and later entries are
fallbacks, which is how free models can serve low-risk work without making the
bot brittle.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))
MODEL_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

DEFAULT_BINDINGS = {
    "chat": ["__primary__"],
    "vision": ["__primary__"],
    "judge": ["__primary__"],
    "control": ["__primary__"],
    "job.default": ["__primary__"],
    "plugin.default": ["__primary__"],
    "embedding": [],
}


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _mask(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text[:6] + "..." + text[-4:] if len(text) > 12 else "***"


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").lower()).strip("-._")
    if not text:
        text = "model-" + secrets.token_hex(3)
    if not text[0].isalpha():
        text = "m-" + text
    return text[:64]


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = []
    result: list[str] = []
    for part in parts:
        text = str(part or "").strip().lower()[:32]
        if text and text not in result:
            result.append(text)
    return result


class ModelCatalog:
    def __init__(self, config: dict, save_fn=None):
        self.config = config
        self.save_fn = save_fn
        self.ensure_seed()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _save(self) -> None:
        if self.save_fn:
            self.save_fn()

    def _models(self) -> list[dict]:
        items = self.config.setdefault("models", [])
        if not isinstance(items, list):
            items = []
            self.config["models"] = items
        return items

    def _bindings(self) -> dict[str, list[str]]:
        items = self.config.setdefault("model_bindings", {})
        if not isinstance(items, dict):
            items = {}
            self.config["model_bindings"] = items
        return items

    def ensure_seed(self) -> None:
        """Create a catalog entry from the legacy ``llm`` block once."""
        models = self._models()
        bindings = self._bindings()
        changed = False
        primary = ""
        if not models:
            llm = self.config.get("llm") or {}
            if llm.get("base_url") and llm.get("model"):
                primary = _slug(llm.get("model"))
                models.append({
                    "id": primary,
                    "name": str(llm.get("model")),
                    "kind": "llm",
                    "provider": "openai",
                    "base_url": str(llm.get("base_url") or "").rstrip("/"),
                    "api_key": str(llm.get("api_key") or ""),
                    "model": str(llm.get("model") or ""),
                    "input_price_per_m": float(llm.get("input_price_per_m") or 0),
                    "output_price_per_m": float(llm.get("output_price_per_m") or 0),
                    "tags": ["primary"],
                    "enabled": True,
                    "notes": "由 llm 配置自动生成",
                    "created_at": _now(),
                })
                changed = True
        for item in models:
            item.setdefault("kind", "llm")
            item.setdefault("provider", "openai")
            item.setdefault("enabled", True)
            item.setdefault("tags", [])
            item.setdefault("input_price_per_m", 0.0)
            item.setdefault("output_price_per_m", 0.0)
            item.setdefault("created_at", _now())
        if not primary:
            primary = next((str(item.get("id")) for item in models
                            if item.get("enabled", True)), "")
        model_ids = {str(item.get("id")) for item in models}
        for role, ids in list(bindings.items()):
            if role == "embedding" or not ids:
                continue
            if primary and not any(model_id in model_ids for model_id in ids):
                bindings[role] = [primary]
                changed = True
        for role, ids in DEFAULT_BINDINGS.items():
            if role not in bindings:
                bindings[role] = [primary] if (primary and "__primary__" in ids) else []
                changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    @staticmethod
    def view(item: dict, roles: list[str] | None = None) -> dict:
        data = {
            "id": item.get("id"),
            "name": item.get("name") or item.get("id"),
            "kind": item.get("kind") or "llm",
            "provider": item.get("provider") or "openai",
            "base_url": item.get("base_url") or "",
            "model": item.get("model") or "",
            "api_key": _mask(item.get("api_key")),
            "has_api_key": bool(item.get("api_key")),
            "input_price_per_m": float(item.get("input_price_per_m") or 0),
            "output_price_per_m": float(item.get("output_price_per_m") or 0),
            "tags": list(item.get("tags") or []),
            "enabled": bool(item.get("enabled", True)),
            "notes": item.get("notes") or "",
        }
        if roles is not None:
            data["roles"] = roles
        return data

    def roles_for(self, model_id: str) -> list[str]:
        return sorted(role for role, ids in self._bindings().items()
                      if model_id in (ids or []))

    def list(self) -> dict:
        models = [self.view(item, self.roles_for(str(item.get("id"))))
                  for item in self._models()]
        return {
            "models": sorted(models, key=lambda item: item["id"]),
            "bindings": {role: list(ids or []) for role, ids in sorted(
                self._bindings().items())},
            "default_purpose": "chat",
        }

    def get(self, model_id: str) -> dict | None:
        target = str(model_id or "").strip().lower()
        for item in self._models():
            if str(item.get("id") or "").lower() == target:
                return item
        return None

    def public_get(self, model_id: str) -> dict | None:
        item = self.get(model_id)
        return self.view(item, self.roles_for(str(item.get("id")))) if item else None

    def free_models(self) -> list[str]:
        return [str(item.get("id")) for item in self._models()
                if item.get("enabled", True) and "free" in (
                    item.get("tags") or [])]

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def add(self, payload: dict) -> dict:
        raw_id = str(payload.get("id") or "").strip().lower()
        raw_model = str(payload.get("model") or "").strip()
        model_id = raw_id or _slug(raw_model or payload.get("name") or "")
        if not MODEL_ID_RE.match(model_id):
            raise ValueError("模型 ID 只能用小写字母、数字、点、下划线或短横线，且以字母开头")
        existing = self.get(model_id)
        item = dict(existing or {})
        item["id"] = model_id
        item["name"] = str(payload.get("name") or item.get("name") or model_id)[:80]
        item["kind"] = str(payload.get("kind") or item.get("kind") or "llm")[:20]
        if item["kind"] not in ("llm", "image"):
            raise ValueError("kind 只能是 llm 或 image")
        item["provider"] = str(payload.get("provider") or item.get("provider")
                               or "openai")[:32]
        base_url = str(payload.get("base_url") or item.get("base_url") or "").strip().rstrip("/")
        if base_url and not re.match(r"^https?://[^\s]+$", base_url):
            raise ValueError("base_url 必须是 http:// 或 https:// 开头的地址")
        item["base_url"] = base_url
        item["model"] = str(payload.get("model") or item.get("model") or "")[:200]
        key = str(payload.get("api_key") or "").strip()
        if key and "..." not in key and key != "***":
            item["api_key"] = key
        elif not item.get("api_key"):
            item["api_key"] = ""
        for field in ("input_price_per_m", "output_price_per_m"):
            if field in payload and payload.get(field) is not None:
                try:
                    item[field] = max(0.0, float(payload.get(field)))
                except (TypeError, ValueError):
                    raise ValueError(f"{field} 必须是数字") from None
            else:
                item[field] = float(item.get(field) or 0)
        if "tags" in payload:
            item["tags"] = _tags(payload.get("tags"))
        else:
            item["tags"] = _tags(item.get("tags"))
        if "enabled" in payload:
            item["enabled"] = bool(payload.get("enabled"))
        else:
            item["enabled"] = bool(item.get("enabled", True))
        item["notes"] = str(payload.get("notes") or item.get("notes") or "")[:300]
        item.setdefault("created_at", _now())
        item["updated_at"] = _now()
        if item["kind"] == "llm" and (not item["base_url"] or not item["model"]):
            raise ValueError("LLM 模型必须填写 base_url 和 model")
        models = self._models()
        if existing is None:
            models.append(item)
        else:
            models[models.index(existing)] = item
        self._save()
        return self.view(item, self.roles_for(model_id))

    def remove(self, model_id: str) -> dict:
        item = self.get(model_id)
        if item is None:
            raise ValueError(f"模型 {model_id} 不存在")
        models = self._models()
        models.remove(item)
        bindings = self._bindings()
        for role, ids in bindings.items():
            bindings[role] = [value for value in (ids or []) if value != model_id]
        if not bindings.get("chat"):
            models.append(item)
            raise ValueError("chat 角色至少要保留一个模型，请先切换再删除")
        self._save()
        return {"deleted": model_id}

    def bind(self, role: str, model_ids: list[str] | str) -> dict:
        role = str(role or "").strip().lower()
        if not ROLE_RE.match(role):
            raise ValueError("角色名只能用小写字母、数字、点、下划线或短横线")
        if isinstance(model_ids, str):
            ids = [part.strip() for part in re.split(r"[\s,;]+", model_ids) if part.strip()]
        else:
            ids = [str(part or "").strip() for part in (model_ids or [])]
        seen: list[str] = []
        for model_id in ids:
            if model_id in seen:
                continue
            item = self.get(model_id)
            if item is None:
                raise ValueError(f"模型 {model_id} 不存在")
            if not item.get("enabled", True):
                raise ValueError(f"模型 {model_id} 已禁用")
            if item.get("kind") != "llm":
                raise ValueError(f"模型 {model_id} 不是文本模型，不能绑定到 {role}")
            seen.append(model_id)
        if role == "chat" and not seen:
            raise ValueError("chat 角色不能为空")
        self._bindings()[role] = seen
        self._save()
        return {"role": role, "models": seen}

    def unbind(self, role: str) -> dict:
        role = str(role or "").strip().lower()
        if role == "chat":
            raise ValueError("chat 角色不能解绑，请直接切换到别的模型")
        self._bindings().pop(role, None)
        self._save()
        return {"role": role, "models": []}

    # ------------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------------
    def _chain(self, purpose: str) -> list[str]:
        purpose = str(purpose or "chat").strip().lower()
        if purpose.startswith("model:"):
            return [purpose[6:]]
        chain = [purpose]
        if "." in purpose:
            chain.append(purpose.split(".", 1)[0] + ".default")
        chain.extend(["default", "chat"])
        result: list[str] = []
        for role in chain:
            if role and role not in result:
                result.append(role)
        return result

    def resolve(self, purpose: str = "chat") -> list[dict]:
        """Return ordered providers for a purpose, skipping missing/disabled ones."""
        purpose = str(purpose or "chat").strip().lower()
        if purpose.startswith("model:"):
            item = self.get(purpose[6:])
            if item is None or not item.get("enabled", True) or item.get("kind") != "llm":
                return []
            if not item.get("base_url") or not item.get("model"):
                return []
            return [{
                "id": str(item.get("id")),
                "label": str(item.get("id")),
                "base_url": str(item.get("base_url")),
                "api_key": str(item.get("api_key") or ""),
                "model": str(item.get("model")),
                "input_price_per_m": float(item.get("input_price_per_m") or 0),
                "output_price_per_m": float(item.get("output_price_per_m") or 0),
                "tags": list(item.get("tags") or []),
            }]
        bindings = self._bindings()
        picked: list[str] = []
        for role in self._chain(purpose):
            for model_id in bindings.get(role) or []:
                if model_id not in picked:
                    picked.append(model_id)
        providers = []
        for model_id in picked:
            item = self.get(model_id)
            if item is None or not item.get("enabled", True):
                continue
            if item.get("kind") != "llm" or not item.get("base_url") or not item.get("model"):
                continue
            providers.append({
                "id": str(item.get("id")),
                "label": str(item.get("id")),
                "base_url": str(item.get("base_url")),
                "api_key": str(item.get("api_key") or ""),
                "model": str(item.get("model")),
                "input_price_per_m": float(item.get("input_price_per_m") or 0),
                "output_price_per_m": float(item.get("output_price_per_m") or 0),
                "tags": list(item.get("tags") or []),
            })
        return providers
