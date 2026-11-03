"""Baseline eval skeleton for the router agent.

Usage:
    python evals/run_evals.py --dry-run
    python evals/run_evals.py --dry-run --verbose
    python evals/run_evals.py --config config.json --tools evals/tools.json

Dry-run only validates cases; it never imports litellm or calls the network.
Live mode is intentionally one-shot per case (no tool execution): it records
which tool the model chose and checks text assertions. Tool cases are skipped
when no tool definitions are supplied.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CASES = HERE / "cases.jsonl"

VALID_CATEGORIES = {"tool_selection", "permission", "factual", "injection"}
VALID_ROLES = {"owner", "guest"}
# The set grows with each migrated capability; the floor keeps coverage honest,
# the ceiling catches an accidentally duplicated or appended file.
MIN_CASES = 10
MAX_CASES = 40
REQUIRED = ("id", "category", "role", "input")
GUEST_TOOLS = {
    "site__chat_stats",
    "site__visit_stats",
    "site__realtime",
    "leetcode__leetcode_stats",
    "leetcode__leetcode_question",
    "demo_time__now",
    "demo_weather__now",
    "websearch__web_search",
    "websearch__web_fetch",
    "websearch__weather",
    "websearch__calculator",
    "lyph_tools__get_public_profile",
    "lyph_tools__search_public_content",
    "lyph_tools__get_public_content",
    "lyph_tools__search_companies",
    "lyph_tools__get_company",
    "lyph_tools__aggregate_companies",
}
TOOL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from None
            item["_line"] = line_no
            cases.append(item)
    return cases


def validate(cases: list[dict]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for item in cases:
        where = f"line {item.get('_line', '?')}"
        for key in REQUIRED:
            if not str(item.get(key) or "").strip():
                problems.append(f"{where}: missing {key}")
        case_id = str(item.get("id") or "")
        if case_id in seen:
            problems.append(f"{where}: duplicate id {case_id}")
        seen.add(case_id)
        if item.get("category") not in VALID_CATEGORIES:
            problems.append(f"{where}: bad category {item.get('category')!r}")
        if item.get("role") not in VALID_ROLES:
            problems.append(f"{where}: bad role {item.get('role')!r}")
        for field in ("expect_tools", "forbid_tools", "expect_any", "forbid_any"):
            value = item.get(field, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                problems.append(f"{where}: {field} must be a list of strings")
                continue
            if field.endswith("tools"):
                for name in value:
                    if not TOOL_RE.match(name):
                        problems.append(f"{where}: invalid tool name {name!r}")
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        problems.append(f"case count is {len(cases)}, expected {MIN_CASES}-{MAX_CASES}")
    return problems


def dry_run(cases: list[dict], verbose: bool) -> int:
    problems = validate(cases)
    if problems:
        for problem in problems:
            print("ERROR", problem)
        return 1
    counts = Counter(item["category"] for item in cases)
    print(f"validated {len(cases)} cases")
    for name in sorted(counts):
        print(f"  {name}: {counts[name]}")
    if verbose:
        for item in cases:
            print(f"  - {item['id']} [{item['category']}/{item['role']}] "
                  f"expect={item.get('expect_tools') or '-'} "
                  f"forbid={item.get('forbid_tools') or '-'}")
    print("dry-run only: no model calls, no network")
    return 0


def load_tools(path: Path | None) -> list[dict]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("tools") or []
    return [item for item in data if isinstance(item, dict)]


def check(item: dict, message: dict) -> tuple[str, list[str]]:
    calls = message.get("tool_calls") or []
    names = [(call.get("function") or {}).get("name", "") for call in calls]
    text = str(message.get("content") or "")
    failures: list[str] = []
    expected = item.get("expect_tools") or []
    if expected and not any(name in expected for name in names):
        failures.append(f"expected one of {expected}, got {names or 'no tool'}")
    for name in item.get("forbid_tools") or []:
        if name in names:
            failures.append(f"forbidden tool called: {name}")
    expect_any = item.get("expect_any") or []
    if expect_any and not names and not any(probe in text for probe in expect_any):
        failures.append(f"response missing any of {expect_any}")
    for probe in item.get("forbid_any") or []:
        if probe in text:
            failures.append(f"response leaked forbidden text: {probe!r}")
    return ("PASS" if not failures else "FAIL"), failures


def live_run(cases: list[dict], config_path: Path, tools_path: Path | None) -> int:
    sys.path.insert(0, str(ROOT))
    from llm import LLM  # imported only for live runs

    config = json.loads(config_path.read_text(encoding="utf-8"))
    cfg = config.get("llm") or {}
    tools = load_tools(tools_path)
    spend_dir = tempfile.mkdtemp(prefix="router-evals-")
    llm = LLM(
        str(cfg.get("base_url") or ""), str(cfg.get("api_key") or ""),
        str(cfg.get("model") or ""), timeout=int(cfg.get("timeout") or 90),
        input_price_per_m=float(cfg.get("input_price_per_m") or 0),
        output_price_per_m=float(cfg.get("output_price_per_m") or 0),
        spend_db=os.path.join(spend_dir, "spend.db"),
    )
    print(f"live eval: {len(cases)} cases, tools={len(tools) or 'none'}")
    failures = 0
    for item in cases:
        allowed = tools if item["role"] == "owner" else [
            tool for tool in tools
            if (tool.get("function") or {}).get("name") in GUEST_TOOLS
        ]
        if (item.get("expect_tools") or item.get("forbid_tools")) and not allowed:
            print(f"SKIP {item['id']} (no tool definitions; pass --tools)")
            continue
        messages = [
            {"role": "system", "content": "你是聊天机器人。能查就查，不能做就直说。"},
            {"role": "user", "content": item["input"]},
        ]
        try:
            message = llm.complete(messages, allowed or None, role=item["role"])
        except Exception as exc:
            failures += 1
            print(f"FAIL {item['id']} error: {str(exc)[:200]}")
            continue
        status, reasons = check(item, message)
        if status == "FAIL":
            failures += 1
        print(f"{status} {item['id']}" + (f" :: {'; '.join(reasons)}" if reasons else ""))
    print(f"done: {len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--dry-run", action="store_true", help="validate only, never call a model")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--config", type=Path,
                        default=Path(os.environ.get("ROUTER_CONFIG", ROOT / "config.json")))
    parser.add_argument("--tools", type=Path, default=None,
                        help="JSON file with OpenAI function definitions for live runs")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.dry_run:
        return dry_run(cases, args.verbose)

    problems = validate(cases)
    if problems:
        for problem in problems:
            print("ERROR", problem)
        return 1
    return live_run(cases, args.config, args.tools)


if __name__ == "__main__":
    raise SystemExit(main())
