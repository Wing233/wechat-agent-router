"""Turn traces, spend and eval history into explanations and safe suggestions.

Everything here is deterministic. The assistant can call it and then phrase the
result in natural language; no hidden model call is needed to diagnose a failed
request.
"""
from __future__ import annotations

import re
from typing import Any


def _blob(trace: dict) -> str:
    parts = [str(trace.get("error") or "")]
    for span in trace.get("spans") or []:
        detail = span.get("detail")
        parts.append(str(detail or ""))
        parts.append(str(span.get("status") or ""))
    return "\n".join(parts).lower()


def _classify(text: str, trace: dict) -> tuple[str, str, str]:
    """Return (verdict, evidence, suggestion)."""
    if trace.get("status") == "running":
        return ("still_running", "这条消息还在处理中，或进程在处理时退出",
                "等一会儿再查；如果长期停在 running，检查 router 是否重启过")
    if "budget" in text or "预算" in text:
        return ("budget_exceeded", "命中每日预算上限",
                "让主人调高对应角色的 budget，或把低风险任务切到免费模型")
    if re.search(r"\b401\b|\b403\b|unauthorized|invalid api key|invalid_agent", text):
        return ("auth_failed", "模型或渠道鉴权失败（401/403）",
                "检查对应模型的 api_key 是否过期，渠道 token 是否一致")
    if re.search(r"\b429\b|rate.?limit|too many requests", text):
        return ("rate_limited", "模型服务限流（429）",
                "给该角色绑定一个备用模型，或降低定时任务的调用频率")
    if re.search(r"timeout|timed out|read timed out|超时", text):
        return ("timeout", "模型或上游服务超时",
                "确认网络与 base_url；给该模型设置更短的 timeout 并绑定 fallback")
    if re.search(r"\b5\d\d\b|bad gateway|service unavailable|internal server error", text):
        return ("provider_error", "模型服务端返回 5xx",
                "稍后重试；长期出现时把该模型从绑定里移除或降级")
    if re.search(r"connection|dns|name or service not known|network", text):
        return ("network", "网络连接失败",
                "检查容器 DNS、出站网络和 base_url 是否可达")
    tool_errors = [span for span in trace.get("spans") or []
                   if span.get("kind") == "tool" and span.get("status") != "ok"]
    if tool_errors:
        names = "、".join(sorted({str(span.get("name")) for span in tool_errors})[:3])
        return ("tool_error", f"工具执行失败：{names}",
                "查看该工具的返回错误；必要时修参数或重载对应插件")
    llm_errors = [span for span in trace.get("spans") or []
                  if span.get("kind") == "llm" and span.get("status") != "ok"]
    if llm_errors and not (trace.get("output") or "").strip():
        return ("llm_empty", "模型调用失败且没有产生回复",
                "检查模型绑定与 provider 日志；确认 fallback 模型可用")
    if str(trace.get("status")) not in ("ok", ""):
        return ("unknown_failure", str(trace.get("error") or "没有记录到具体错误")[:300],
                "先看 trace 里的 spans 和 spend，再决定是换模型还是修工具")
    output = str(trace.get("output") or "").strip()
    if not output:
        return ("empty_reply", "请求成功但回复为空",
                "检查模型是否只返回了工具调用；必要时给 chat 角色换模型")
    latency = int(trace.get("latency_ms") or 0)
    if latency >= 30000:
        return ("slow", f"整条请求耗时 {latency / 1000:.1f} 秒",
                "把低风险任务切到更快的模型，或减少工具轮数")
    return ("ok", "没有发现异常", "无需处理")


def explain(trace: dict) -> dict:
    """Explain one trace using only its recorded spans/spend."""
    if not trace:
        return {"error": "没有找到这条 trace"}
    verdict, evidence, suggestion = _classify(_blob(trace), trace)
    timeline = []
    for span in trace.get("spans") or []:
        timeline.append({
            "ts": span.get("ts"),
            "kind": span.get("kind"),
            "name": span.get("name"),
            "status": span.get("status"),
            "latency_ms": span.get("latency_ms"),
            "model": span.get("model") or "",
            "cost": span.get("cost") or 0,
        })
    spend = trace.get("spend") or []
    return {
        "trace_id": trace.get("trace_id"),
        "status": trace.get("status"),
        "verdict": verdict,
        "evidence": evidence,
        "suggestion": suggestion,
        "input": str(trace.get("input") or "")[:300],
        "error": str(trace.get("error") or "")[:500],
        "latency_ms": trace.get("latency_ms") or 0,
        "cost": round(sum(float(item.get("cost") or 0) for item in spend), 6),
        "timeline": timeline,
    }


def _pct(value: float) -> str:
    return f"{value:.1f}%"


def anomalies(trace_summary: dict, cost_report: dict, eval_latest: dict | None,
              eval_drift: dict | None) -> list[dict]:
    items: list[dict] = []
    total = int(trace_summary.get("total") or 0)
    failed = int(trace_summary.get("failed") or 0)
    if total and failed / total >= 0.1:
        items.append({
            "code": "error_rate",
            "severity": "high" if failed / total >= 0.25 else "medium",
            "message": f"最近 {trace_summary.get('hours')} 小时失败率 {_pct(failed / total * 100)}"
                       f"（{failed}/{total}）",
            "suggestion": "用 trace_search status=failed 找最近失败样本，再逐条 trace_explain",
        })
    for row in trace_summary.get("tool_errors") or []:
        if int(row.get("errors") or 0) > 0:
            items.append({
                "code": "tool_error",
                "severity": "medium",
                "message": f"工具 {row.get('name')} 失败 {row.get('errors')}/{row.get('runs')} 次",
                "suggestion": "查看对应插件日志；如果是参数问题，调整工具描述或提示词",
            })
    for row in trace_summary.get("recent_errors") or []:
        text = str(row.get("error") or "").lower()
        if "budget" in text or "预算" in text:
            items.append({
                "code": "budget",
                "severity": "high",
                "message": f"trace {row.get('trace_id')} 因预算被拦截",
                "suggestion": "调高对应角色预算，或把低风险任务切到免费模型",
            })
            break
    p95 = int(trace_summary.get("p95_latency_ms") or 0)
    if p95 >= 45000:
        items.append({
            "code": "latency",
            "severity": "medium",
            "message": f"P95 延迟 {p95 / 1000:.1f} 秒",
            "suggestion": "检查慢 trace 的模型 span；考虑更快的主模型或缩短超时",
        })
    total_cost = float(cost_report.get("total") or 0)
    today = float(cost_report.get("today") or 0)
    days = max(0.1, float(cost_report.get("days") or 1))
    average = total_cost / days if total_cost > 0 else 0.0
    if today > 0 and average > 0 and today > average * 1.6:
        items.append({
            "code": "cost_spike",
            "severity": "medium",
            "message": f"今天已花 {today:.2f}，高于近 {days:g} 天日均 {average:.2f}",
            "suggestion": "用 cost_report 按模型拆分，检查是否有异常高频调用",
        })
    if eval_latest is None:
        items.append({
            "code": "eval_missing",
            "severity": "low",
            "message": "还没有任何评测记录，无法判断模型是否漂移",
            "suggestion": "运行一次 eval_run，建立基线",
        })
    elif eval_drift and eval_drift.get("status") == "drifting":
        items.append({
            "code": "eval_drift",
            "severity": "high",
            "message": f"评测均分从 {eval_drift.get('baseline_mean')} 降到 "
                       f"{eval_drift.get('recent_mean')}（{eval_drift.get('delta')}）",
            "suggestion": "查看 eval_drift 的逐用例变化，必要时切回上一个 chat 模型",
        })
    elif eval_drift and eval_drift.get("status") == "watch":
        items.append({
            "code": "eval_watch",
            "severity": "medium",
            "message": f"评测均分下降 {abs(float(eval_drift.get('delta') or 0)):.1f} 分，仍在观察",
            "suggestion": "连续再跑两次 eval_run，确认不是单次波动",
        })
    return items


def suggestions(trace_summary: dict, cost_report: dict, eval_latest: dict | None,
                eval_drift: dict | None, catalog: dict) -> list[dict]:
    """Build deterministic, re-checkable optimization suggestions."""
    items: list[dict] = []
    models = {item["id"]: item for item in catalog.get("models") or []}
    bindings = catalog.get("bindings") or {}
    free = [model_id for model_id, item in models.items()
            if item.get("enabled", True) and "free" in (item.get("tags") or [])]
    bindings_chat = bindings.get("chat") or []

    if free:
        for role in ("job.default", "plugin.default", "control"):
            current = bindings.get(role) or []
            if current and not any(model_id in free for model_id in current):
                items.append({
                    "id": f"free-{role}",
                    "title": f"把 {role} 切到免费模型",
                    "reason": f"当前绑定 {current} 都不是免费模型，低风险任务没必要用付费模型",
                    "risk": "low",
                    "expected": "降低定时任务和插件调用成本",
                    "action": {"type": "bind", "role": role, "models": free + current},
                })
    if len(bindings_chat) < 2:
        items.append({
            "id": "chat-fallback",
            "title": "给 chat 增加一个备用模型",
            "reason": "chat 只绑定了一个模型，主模型限流或 5xx 时会直接失败",
            "risk": "low",
            "expected": "提高可用性；主模型失败时自动切换",
            "action": {"type": "manual", "hint": "先 model_add 一个模型，再 model_bind role=chat"},
        })
    budgets = cost_report.get("budgets") or []
    guest = next((item for item in budgets if item.get("role") == "guest"), None)
    if guest and guest.get("unlimited"):
        items.append({
            "id": "guest-budget",
            "title": "给访客设置每日预算",
            "reason": "访客目前不限额，群聊被刷时成本不可控",
            "risk": "medium",
            "expected": "限制最坏情况成本",
            "action": {"type": "budget", "role": "guest", "amount": 1.0},
        })
    if eval_latest is None:
        items.append({
            "id": "eval-baseline",
            "title": "跑一次评测建立基线",
            "reason": "没有基线就无法判断换模型后是否变差",
            "risk": "low",
            "expected": "得到 chat 模型的初始分数和逐用例结果",
            "action": {"type": "manual", "hint": "调用 eval_run，默认模型是当前 chat 绑定"},
        })
    elif eval_drift and eval_drift.get("status") in ("drifting", "watch"):
        items.append({
            "id": "eval-review",
            "title": "检查评测漂移",
            "reason": f"最近均分 {eval_drift.get('recent_mean')}，baseline "
                      f"{eval_drift.get('baseline_mean')}",
            "risk": "low",
            "expected": "定位退化的用例，再决定是否换模型",
            "action": {"type": "manual", "hint": "调用 eval_drift 看逐用例变化"},
        })
    for row in trace_summary.get("tool_errors") or []:
        if int(row.get("errors") or 0) >= 3:
            items.append({
                "id": f"tool-{row.get('name')}",
                "title": f"修一下工具 {row.get('name')}",
                "reason": f"最近失败 {row.get('errors')} 次",
                "risk": "low",
                "expected": "减少无效工具轮次和模型重试成本",
                "action": {"type": "manual", "hint": "查看插件日志并重载插件"},
            })
    if not items:
        items.append({
            "id": "healthy",
            "title": "当前没有明显可优化的项",
            "reason": "失败率、延迟、成本和评测都在阈值内",
            "risk": "low",
            "expected": "保持现状，继续观察",
            "action": {"type": "none"},
        })
    return items


def analyze(trace_summary: dict, cost_report: dict, eval_latest: dict | None,
            eval_drift: dict | None, catalog: dict) -> dict:
    found = anomalies(trace_summary, cost_report, eval_latest, eval_drift)
    return {
        "window_hours": trace_summary.get("hours"),
        "traces": {
            "total": trace_summary.get("total"),
            "failed": trace_summary.get("failed"),
            "success_rate": trace_summary.get("success_rate"),
            "p95_latency_ms": trace_summary.get("p95_latency_ms"),
        },
        "cost": {
            "days": cost_report.get("days"),
            "total": cost_report.get("total"),
            "today": cost_report.get("today"),
            "top_models": (cost_report.get("by_model") or [])[:3],
        },
        "eval": {
            "latest_score": (eval_latest or {}).get("score"),
            "latest_run": (eval_latest or {}).get("id"),
            "drift": (eval_drift or {}).get("status"),
        },
        "anomalies": found,
        "suggestions": suggestions(trace_summary, cost_report, eval_latest,
                                   eval_drift, catalog),
    }
