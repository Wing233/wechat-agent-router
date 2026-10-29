"""Template-based connector plugin generator used by the evolution plugin."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SpecError(Exception):
    pass


def generate(base: Path, spec: dict) -> dict:
    pid = str(spec.get("id") or "").strip()
    if not ID_RE.match(pid):
        raise SpecError("插件 id 必须是小写字母开头，只含 a-z0-9_，2-64 位")
    name = str(spec.get("name") or pid).strip()[:80]
    description = str(spec.get("description") or "").strip()[:500]
    tool_name = str(spec.get("tool_name") or "run").strip()
    if not NAME_RE.match(tool_name):
        raise SpecError("工具名只能含字母、数字、下划线、短横线")
    method = str(spec.get("method") or "GET").upper()
    if method not in {"GET", "POST"}:
        raise SpecError("目前只支持 GET/POST")
    url = str(spec.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SpecError("url 必须是 http/https 完整地址")
    if "{" in parsed.hostname:
        raise SpecError("url 的 host 不能使用模板变量")
    parameters = spec.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise SpecError("parameters 必须是 JSON Schema object")
    timeout = int(spec.get("timeout") or 10)
    if timeout < 1 or timeout > 60:
        raise SpecError("timeout 应在 1–60 秒")

    pkg = base / f"{pid}-{os.urandom(3).hex()}"
    pkg.mkdir(parents=True, exist_ok=False)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    manifest = {
        "id": pid, "name": name, "version": "0.1.0", "api_version": "1",
        "entry": "plugin.py", "capabilities": ["tools"],
        "permissions": [f"net:{parsed.hostname}"],
        "description": description or f"{name} HTTP connector",
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    tool = {
        "name": tool_name,
        "description": str(spec.get("tool_description") or description or name)[:500],
        "parameters": parameters,
    }
    template = '''"""Generated HTTP connector plugin."""
import urllib.parse

URL = {url}
METHOD = {method}
TIMEOUT = {timeout}
TOOL = {tool}


def apply(ctx):
    def handler(args):
        url = URL
        for key, value in (args or {{}}).items():
            url = url.replace("{{" + key + "}}", urllib.parse.quote(str(value)))
        status, body = ctx.http_request(METHOD, url, timeout=TIMEOUT)
        return {{"status": status, "body": body[:4000]}}

    ctx.tools.register(TOOL["name"], TOOL["description"], TOOL["parameters"], handler,
                       aliases=(TOOL["name"],))
'''.format(
        url=json.dumps(url, ensure_ascii=False),
        method=json.dumps(method),
        timeout=timeout,
        tool=json.dumps(tool, ensure_ascii=False, indent=4),
    )
    # Template braces around handler args are doubled only in the format string; restore them.
    template = template.replace('(args or {{}})', '(args or {})').replace('"{{" + key + "}}"', '"{" + key + "}"')
    (pkg / "plugin.py").write_text(template, encoding="utf-8")

    test = '''import importlib.util

spec = importlib.util.spec_from_file_location("candidate", "plugin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Tools:
    def register(self, *args, **kwargs):
        assert args and isinstance(args[0], str)


class Ctx:
    tools = Tools()

    def http_request(self, *args, **kwargs):
        return 200, "{}"


module.apply(Ctx())
print("ok")
'''
    (pkg / "test_plugin.py").write_text(test, encoding="utf-8")
    return {"path": str(pkg), "manifest": manifest, "permissions": manifest["permissions"],
            "plugin_id": pid, "tool": tool_name}


def run_tests(pkg: Path) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "PYTHONPATH": str(pkg), "PYTHONDONTWRITEBYTECODE": "1"}
    steps = []
    for args in ([sys.executable, "-m", "py_compile", "plugin.py"],
                 [sys.executable, "test_plugin.py"]):
        try:
            proc = subprocess.run(args, cwd=str(pkg), env=env, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return {"ok": False, "steps": steps, "output": "timeout"}
        steps.append({"cmd": " ".join(args), "code": proc.returncode,
                      "output": (proc.stdout + proc.stderr)[-1000:]})
        if proc.returncode != 0:
            return {"ok": False, "steps": steps, "output": (proc.stdout + proc.stderr)[-1000:]}
    return {"ok": True, "steps": steps, "output": "ok"}
