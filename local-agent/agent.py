"""Outbound-polling local agent: pulls tasks from the Router, runs a local coding CLI."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class Agent:
    def __init__(self, config: dict):
        self.config = config
        self.router = str(config["router"]).rstrip("/")
        self.agent_id = str(config["agent_id"])
        self.key = str(config["agent_key"])
        self.engines = config.get("engines") or {}
        self.workspaces = config.get("workspaces") or {}
        self.default_engine = config.get("default_engine") or "opencode"
        self.default_workspace = config.get("default_workspace") or "router"
        self.wait_seconds = int(config.get("wait_seconds", 60))
        self.task_timeout = int(config.get("task_timeout", 1800))
        self.poll_seconds = max(1, int(config.get("poll_seconds", 2)))

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.router + path, data=data, method=method)
        request.add_header("X-Agent-Key", self.key)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 204:
                    return None
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return None
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None

    def register(self):
        self.request("POST", "/agent/register", {
            "engines": sorted(self.engines),
            "workspaces": sorted(self.workspaces),
            "default_engine": self.default_engine,
            "default_workspace": self.default_workspace,
        })

    def resolve(self, task: dict) -> tuple[str, str]:
        engine = str(task.get("engine") or self.default_engine)
        if engine not in self.engines:
            raise ValueError(f"引擎不可用：{engine}；可用：{', '.join(sorted(self.engines))}")
        workspace = str(task.get("workspace") or self.default_workspace)
        if workspace in self.workspaces:
            path = self.workspaces[workspace]
        elif workspace in self.workspaces.values():
            path = workspace
        else:
            raise ValueError(f"工作区不允许：{workspace}；可用：{', '.join(sorted(self.workspaces))}")
        if not Path(path).is_dir():
            raise ValueError(f"工作区不存在：{path}")
        return engine, path

    def run_cli(self, engine: str, workspace: str, message: str) -> tuple[int, str]:
        command = list(self.engines[engine])
        executable = shutil.which(command[0]) or command[0]
        args = command[1:] + [message]
        ps = "& " + ps_quote(executable) + " " + " ".join(ps_quote(arg) for arg in args)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=self.task_timeout,
        )
        output = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else str(proc.stdout)
        return proc.returncode, ANSI.sub("", output)

    def handle(self, task: dict):
        task_id = str(task.get("task_id") or "")
        message = str(task.get("message") or "")
        started = time.time()
        try:
            engine, workspace = self.resolve(task)
            code, output = self.run_cli(engine, workspace, message)
            ok = code == 0
            text = f"[{engine} @ {workspace}] exit={code}\n{output[-4000:]}"
        except Exception as exc:
            ok = False
            text = f"本地任务失败：{exc}"
        elapsed = time.time() - started
        try:
            self.request("POST", "/agent/result", {"task_id": task_id, "ok": ok, "text": text})
        except Exception:
            pass
        if elapsed > self.wait_seconds:
            try:
                self.request("POST", "/v1/notify", {"text": text[-3500:]})
            except Exception:
                pass

    def run(self):
        while True:
            try:
                self.register()
                task = self.request("GET", "/agent/tasks?agent_id=" + urllib.parse.quote(self.agent_id))
                if task:
                    self.handle(task)
                    continue
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(f"[local-agent] {exc}", file=sys.stderr)
                time.sleep(5)
                continue
            time.sleep(self.poll_seconds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args()
    Agent(load_config(Path(args.config))).run()


if __name__ == "__main__":
    main()
