"""Kernel plugin loader: manifests, restricted context, lifecycle hooks."""
from __future__ import annotations

import importlib
import json
import logging
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger("plugins")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
API_VERSION = "1"


class PluginError(Exception):
    pass


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    api_version: str
    entry: str
    permissions: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    description: str = ""


class ToolRegistrar:
    """Tools always land in the shared registry under the plugin namespace."""

    def __init__(self, registry, plugin_id: str):
        self.registry = registry
        self.plugin_id = plugin_id

    def register(self, name: str, description: str, parameters: dict, handler: Callable,
                 aliases: tuple[str, ...] = (), tags: tuple[str, ...] = ()) -> None:
        prefix = f"{self.plugin_id}__"
        full = name if name.startswith(prefix) else prefix + name
        self.registry.register(full, description, parameters, handler,
                               tags=(self.plugin_id, *tags), aliases=tuple(aliases))


class JobRegistrar:
    def __init__(self, jobs: list, plugin_id: str):
        self.jobs = jobs
        self.plugin_id = plugin_id

    def register(self, job) -> None:
        self.jobs.append(job)


class PluginContext:
    def __init__(self, manifest: PluginManifest, tools, jobs, resources: dict,
                 config: dict, root_config: dict, send_fn: Callable[[str], bool], logger,
                 services: dict, secrets: dict, login_url_fn: Callable[[str], str]):
        self.manifest = manifest
        self.id = manifest.id
        self.permissions = set(manifest.permissions)
        self.tools = ToolRegistrar(tools, manifest.id)
        self.jobs = JobRegistrar(jobs, manifest.id)
        self.config = config
        self.root_config = root_config
        self._resources = resources
        self._send = send_fn
        self._services = services
        self._secrets = secrets
        self._login_url = login_url_fn
        self.log = logger
        self._disposers: list[Callable[[], None]] = []
        self._provided: list[str] = []

    def resource(self, name: str):
        required = {
            "store": "db:router",
            "agents": "agent:read", "call_agent": "agent:call", "settings": "admin",
        }.get(name)
        if required and required not in self.permissions:
            raise PermissionError(f"plugin {self.id} lacks permission {required}")
        if name not in self._resources:
            raise KeyError(f"unknown resource: {name}")
        return self._resources[name]

    def provide(self, name: str, service) -> None:
        """Expose a plugin-owned service to the kernel (loaded after apply)."""
        self._services[name] = service
        if name not in self._provided:
            self._provided.append(name)

    def secret(self, name: str) -> str:
        if name not in self.manifest.secrets:
            raise PermissionError(f"plugin {self.id} did not declare secret {name}")
        if name not in self._secrets:
            raise KeyError(f"secret not configured: {name}")
        return self._secrets[name]

    def login_url(self, service: str) -> str:
        if "credential" not in self.manifest.capabilities:
            raise PermissionError(f"plugin {self.id} lacks credential capability")
        return self._login_url(service)

    def http_request(self, method: str, url: str, headers: dict | None = None,
                     body: bytes | str | None = None, timeout: int = 10) -> tuple[int, str]:
        host = urlsplit(url).hostname or ""
        if not host or f"net:{host}" not in self.permissions:
            raise PermissionError(f"plugin {self.id} lacks permission net:{host or '?'}")
        if isinstance(body, str):
            body = body.encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method.upper(), headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(16000).decode("utf-8", "replace")

    def send(self, text: str) -> bool:
        if "send:wechat" not in self.permissions:
            raise PermissionError(f"plugin {self.id} lacks permission send:wechat")
        return self._send(text)

    def on_unload(self, disposer: Callable[[], None]) -> None:
        self._disposers.append(disposer)


class PluginLoader:
    def __init__(self, plugins_dir: Path, tools, jobs: list, resources: dict,
                 root_config: dict, send_fn: Callable[[str], bool], services: dict | None = None,
                 secrets: dict | None = None, login_url_fn: Callable[[str], str] | None = None,
                 audit_fn: Callable[[str, str, str], None] | None = None):
        self.plugins_dir = Path(plugins_dir)
        self.tools = tools
        self.jobs = jobs
        self.resources = resources
        self.root_config = root_config
        self.send_fn = send_fn
        self.services = services if services is not None else {}
        self.secrets = secrets if secrets is not None else {}
        self.login_url_fn = login_url_fn or (lambda service: "")
        self.audit_fn = audit_fn
        self.loaded: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.disabled: set[str] = set(root_config.get("disabled_plugins") or [])
        if str(self.plugins_dir.parent) not in sys.path:
            sys.path.insert(0, str(self.plugins_dir.parent))

    def _audit(self, action: str, plugin_id: str, detail: str = "") -> None:
        if not self.audit_fn:
            return
        try:
            self.audit_fn(plugin_id, action, detail)
        except Exception:
            log.exception("plugin audit failed")

    def _manifest(self, path: Path) -> PluginManifest:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = PluginManifest(
            id=raw.get("id", ""), name=raw.get("name", ""), version=raw.get("version", ""),
            api_version=str(raw.get("api_version", "")), entry=raw.get("entry", "plugin.py"),
            permissions=list(raw.get("permissions") or []),
            capabilities=list(raw.get("capabilities") or []),
            secrets=list(raw.get("secrets") or []),
            description=raw.get("description", ""),
        )
        if not ID_RE.match(manifest.id):
            raise PluginError(f"invalid plugin id: {manifest.id!r}")
        if manifest.api_version != API_VERSION:
            raise PluginError(f"unsupported api_version {manifest.api_version!r}")
        if path.parent.name != manifest.id:
            raise PluginError("manifest id does not match directory name")
        entry = (path.parent / manifest.entry).resolve()
        if path.parent.resolve() not in entry.parents or not entry.is_file():
            raise PluginError(f"entry not found: {manifest.entry}")
        return manifest

    def load_all(self) -> None:
        for manifest_path in sorted(self.plugins_dir.glob("*/manifest.json")):
            pid = manifest_path.parent.name
            if pid in self.disabled:
                log.info("plugin disabled, skipping: %s", pid)
                continue
            try:
                self.load(manifest_path)
            except Exception as exc:
                self.errors[pid] = str(exc)[:500]
                self._audit("error", pid, str(exc)[:500])
                log.exception("plugin %s failed to load", pid)

    def load(self, manifest_path: Path) -> None:
        manifest = self._manifest(manifest_path)
        pid = manifest.id
        if pid in self.disabled:
            raise PluginError(f"plugin disabled: {pid}")
        module = importlib.import_module(f"plugins.{pid}.{Path(manifest.entry).stem}")
        if pid in self.loaded:
            self._dispose(pid)
        ctx = PluginContext(
            manifest=manifest, tools=self.tools, jobs=self.jobs, resources=self.resources,
            config=(self.root_config.get("plugins", {}) or {}).get(pid, {}),
            root_config=self.root_config, send_fn=self.send_fn,
            logger=logging.getLogger(f"plugin.{pid}"), services=self.services,
            secrets=self.secrets, login_url_fn=self.login_url_fn,
        )
        apply = getattr(module, "apply", None)
        if not callable(apply):
            raise PluginError("plugin.py must expose apply(ctx)")
        apply(ctx)
        self.loaded[pid] = {"manifest": manifest, "module": module, "disposers": ctx._disposers,
                            "services": list(ctx._provided)}
        self.errors.pop(pid, None)
        self._audit("load", pid, f"{manifest.version} {','.join(manifest.capabilities)}")
        log.info("plugin loaded: %s %s (%s)", pid, manifest.version, ",".join(manifest.capabilities))

    def _dispose(self, pid: str) -> None:
        entry = self.loaded.pop(pid, None)
        if not entry:
            return
        for disposer in reversed(entry.get("disposers") or []):
            try:
                disposer()
            except Exception:
                log.exception("plugin %s disposer failed", pid)
        for name in entry.get("services") or []:
            self.services.pop(name, None)
        self._audit("unload", pid, entry["manifest"].version)

    def unload(self, pid: str) -> bool:
        if pid not in self.loaded:
            return False
        self._dispose(pid)
        self.tools.unregister_plugin(pid)
        self.jobs[:] = [job for job in self.jobs if (job.meta or {}).get("plugin") != pid]
        return True

    def reload(self, pid: str) -> None:
        manifest_path = self.plugins_dir / pid / "manifest.json"
        if not manifest_path.is_file():
            raise PluginError(f"plugin not found: {pid}")
        self.unload(pid)
        for name in list(sys.modules):
            if name == f"plugins.{pid}" or name.startswith(f"plugins.{pid}."):
                del sys.modules[name]
        importlib.invalidate_caches()
        self.load(manifest_path)
        self._audit("reload", pid, "ok")

    def enable(self, pid: str) -> bool:
        if pid in self.loaded:
            return False
        manifest_path = self.plugins_dir / pid / "manifest.json"
        if not manifest_path.is_file():
            raise PluginError(f"plugin not found: {pid}")
        self.disabled.discard(pid)
        self.load(manifest_path)
        return True

    def disable(self, pid: str) -> bool:
        if pid not in self.loaded:
            return False
        self.disabled.add(pid)
        return self.unload(pid)

    def _describe_entry(self, pid: str, manifest: PluginManifest, entry: dict | None) -> dict:
        return {
            "id": pid,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "capabilities": manifest.capabilities,
            "permissions": manifest.permissions,
            "secrets": manifest.secrets,
            "tools": self.tools.names_for_plugin(pid) if entry else [],
            "jobs": [job.id for job in self.jobs if (job.meta or {}).get("plugin") == pid] if entry else [],
            "services": (entry or {}).get("services") or [],
            "enabled": pid not in self.disabled,
            "loaded": entry is not None,
            "error": self.errors.get(pid, ""),
        }

    def describe(self) -> list[dict]:
        items = []
        for pid, entry in self.loaded.items():
            items.append(self._describe_entry(pid, entry["manifest"], entry))
        return items

    def describe_all(self) -> list[dict]:
        items: dict[str, dict] = {}
        for pid, entry in self.loaded.items():
            items[pid] = self._describe_entry(pid, entry["manifest"], entry)
        for manifest_path in sorted(self.plugins_dir.glob("*/manifest.json")):
            pid = manifest_path.parent.name
            if pid in items:
                continue
            try:
                manifest = self._manifest(manifest_path)
            except Exception as exc:
                items[pid] = {
                    "id": pid, "name": pid, "version": "", "description": "",
                    "capabilities": [], "permissions": [], "secrets": [],
                    "tools": [], "jobs": [], "services": [],
                    "enabled": pid not in self.disabled, "loaded": False,
                    "error": str(exc)[:300],
                }
                continue
            items[pid] = self._describe_entry(pid, manifest, None)
        return sorted(items.values(), key=lambda item: item["id"])

    def unload_all(self) -> None:
        for pid in list(self.loaded):
            self._dispose(pid)
