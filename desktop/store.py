"""Desktop-only preferences (never contains raw secrets)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from secret_store import SecretStore

DEFAULTS = {
    "server_url": "",
    "window_mode": "auto",
    "last_page": "overview",
    "first_run": True,
}


class DesktopSettings:
    def __init__(self, path: Path, secrets: SecretStore):
        self.path = Path(path)
        self.secrets = secrets
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                self.data.update(raw)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def get(self, key: str, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    @property
    def service_token(self) -> str:
        return self.secrets.get("service_token")

    def set_service_token(self, token: str) -> None:
        self.secrets.set("service_token", token)

    def has_service_token(self) -> bool:
        return bool(self.service_token)
