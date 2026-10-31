"""Natural-language operations for models, cost, traces, evals and tuning.

There is deliberately no admin page for these features. Every action below is
a normal tool that the assistant can call from a chat window. Destructive or
costly actions still return an explicit result instead of hiding what changed.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import evals
import insights
import spend
import traces
import trace_ctx


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def _guard(fn: Callable[[dict], Any]):
    def wrapper(args: dict) -> dict:
        try:
            return {"ok": True, "result": fn(args or {})}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}
    return wrapper


def _short(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_short(item, limit) for item in value[:20]]
    if isinstance(value, dict):
        return {key: _short(item, limit) for key, item in value.items()}
    return value


class CapabilityService:
    def __init__(self, tools, *, settings, catalog, llm, spend_path: str,
                 reload_llm: Callable[[], None],
                 trace_path: str = "", eval_db: str = "",
                 channels=None, send_wechat: Callable[[str], bool] | None = None,
                 with_channels: bool = True):
        self.tools = tools
        self.settings = settings
        self.catalog = catalog
        self.llm = llm
        self.spend_path = spend_path
        self.trace_path = trace_path
        self.eval_db = eval_db
        self.reload_llm = reload_llm
        self.channels = channels
        self.send_wechat = send_wechat
        self.with_channels = with_channels

    def _audit(self, action: str, detail: str) -> None:
        """Audit is best-effort: a missing table must not undo a real change."""
        try:
            self.settings.store.log_plugin_event("capabilities", action, detail)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # model catalog
    # ------------------------------------------------------------------
    def _model_list(self, args: dict) -> dict:
        return self.catalog.list()

    def _model_add(self, args: dict) -> dict:
        item = self.catalog.add(args)
        if args.get("set_chat"):
            self._model_switch_chat({"model_id": item["id"]})
        self._audit("model_add", item["id"])
        return item

    def _model_remove(self, args: dict) -> dict:
        result = self.catalog.remove(str(args.get("model_id") or ""))
        self._audit("model_remove", str(args.get("model_id") or ""))
        self._sync_chat_legacy()
        return result

    def _model_bind(self, args: dict) -> dict:
        result = self.catalog.bind(str(args.get("role") or ""),
                                   args.get("models") or args.get("model_ids") or "")
        if result["role"] == "chat":
            self._sync_chat_legacy()
        self._audit(
            "model_bind", f"{result['role']}={','.join(result['models'])}")
        return result

    def _model_switch_chat(self, args: dict) -> dict:
        model_id = str(args.get("model_id") or args.get("id") or "").strip()
        item = self.catalog.get(model_id)
        if item is None:
            raise ValueError(f"模型 {model_id} 不存在，先用 model_list 看已配置模型")
        self.catalog.bind("chat", [model_id])
        self._sync_chat_legacy()
        self._audit("switch_chat", model_id)
        return {"chat": self.catalog.get(model_id) and self.catalog.public_get(model_id),
                "binding": self.catalog.list()["bindings"].get("chat")}

    def _sync_chat_legacy(self) -> None:
        """Keep the old llm block pointing at the first chat model, for consoles."""
        bindings = self.catalog.list()["bindings"].get("chat") or []
        if not bindings:
            return
        item = self.catalog.get(bindings[0])
        if item is None:
            return
        llm_cfg = self.settings.config.setdefault("llm", {})
        llm_cfg["base_url"] = item.get("base_url") or llm_cfg.get("base_url")
        llm_cfg["api_key"] = item.get("api_key") or llm_cfg.get("api_key")
        llm_cfg["model"] = item.get("model") or llm_cfg.get("model")
        self.settings.save()
        self.reload_llm()

    def _model_test(self, args: dict) -> dict:
        model_id = str(args.get("model_id") or args.get("id") or "").strip()
        if not self.catalog.get(model_id):
            raise ValueError(f"模型 {model_id} 不存在")
        prompt = str(args.get("prompt") or "只回复两个字：可用")
        started = time.perf_counter()
        usage: dict = {}
        message = self.llm.complete(
            [{"role": "user", "content": prompt}], None, max_tokens=64,
            role="eval", purpose=f"model:{model_id}", usage_sink=usage)
        content = (message.get("content") or "").strip()
        return {
            "model_id": model_id,
            "ok": bool(content),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "reply": content[:800],
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cost": round(float(usage.get("cost") or 0), 6),
        }

    # ------------------------------------------------------------------
    # cost and budgets
    # ------------------------------------------------------------------
    def _cost_report(self, args: dict) -> dict:
        days = float(args.get("days") or 7)
        data = spend.report(days, path=self.spend_path)
        data["budgets"] = spend.budget_status(self.llm.budgets, path=self.spend_path)
        data["models"] = [
            {"id": item["id"], "price_in": item["input_price_per_m"],
             "price_out": item["output_price_per_m"], "tags": item["tags"]}
            for item in self.catalog.list()["models"]
        ]
        return data

    def _budget_set(self, args: dict) -> dict:
        role = str(args.get("role") or "").strip().lower()
        if not role or not role.replace("_", "").isalnum():
            raise ValueError("role 只能包含字母、数字和下划线")
        amount = float(args.get("amount") if args.get("amount") is not None else 0)
        if amount < 0:
            raise ValueError("预算不能为负数；0 表示不限")
        llm_cfg = self.settings.config.setdefault("llm", {})
        llm_cfg[f"daily_budget_{role}"] = amount
        self.settings.save()
        self.reload_llm()
        self._audit("budget_set", f"{role}={amount:g}")
        return {"role": role, "daily_budget": amount,
                "status": spend.budget_status(self.llm.budgets,
                                              path=self.spend_path)}

    # ------------------------------------------------------------------
    # traces
    # ------------------------------------------------------------------
    def _trace_get(self, args: dict) -> dict:
        trace = traces.get(str(args.get("trace_id") or ""),
                           spend_path=self.spend_path)
        if trace is None:
            raise ValueError("没有找到这条 trace")
        trace["explain"] = insights.explain(trace)
        return trace

    def _trace_search(self, args: dict) -> dict:
        rows = traces.search(
            str(args.get("query") or ""),
            hours=float(args.get("hours") or 24),
            status=str(args.get("status") or ""),
            limit=int(args.get("limit") or 20),
        )
        for row in rows:
            row["input"] = str(row.get("input") or "")[:200]
            row["output"] = str(row.get("output") or "")[:200]
            row["error"] = str(row.get("error") or "")[:300]
        return {"count": len(rows), "traces": rows}

    def _trace_explain(self, args: dict) -> dict:
        trace = traces.get(str(args.get("trace_id") or ""),
                           spend_path=self.spend_path)
        if trace is None:
            raise ValueError("没有找到这条 trace")
        return insights.explain(trace)

    # ------------------------------------------------------------------
    # channels
    # ------------------------------------------------------------------
    def _list_channels(self, args: dict) -> dict:
        items = []
        for item in self.settings.channels_view():
            channel_id = item["id"]
            status = self.channels.status(channel_id) if self.channels else {}
            merged = dict(item)
            merged.update(status)
            if not merged.get("last_chat_id"):
                raw = self.settings.store.state_get(
                    f"channel:{channel_id}:last_target", "")
                try:
                    saved = json.loads(raw) if raw else {}
                except Exception:
                    saved = {}
                merged["last_chat_id"] = saved.get("chat_id") or ""
                merged["last_platform"] = saved.get("platform") or ""
            merged["pending_outbound"] = len(
                self.settings.store.list_channel_outbox(channel_id, 100))
            merged["recent"] = (self.channels.recent_logs(channel_id, 5)
                                if self.channels else [])
            items.append(merged)
        items.append({
            "id": "wechat",
            "name": "微信（Hub）",
            "type": "wechat",
            "enabled": True,
            "virtual": True,
            "last_chat_id": "owner",
            "note": "微信回复默认回到当前会话；send_to_channel 可主动发到站主微信",
        })
        return {"channels": items}

    def _send_to_channel(self, args: dict) -> dict:
        channel_id = str(args.get("channel") or "").strip().lower()
        text = str(args.get("text") or "").strip()
        if not channel_id:
            raise ValueError("缺少 channel")
        if not text:
            raise ValueError("缺少 text")
        if len(text) > 8000:
            raise ValueError("消息过长（最多 8000 字）")
        if channel_id in ("wechat", "weixin", "hub"):
            if not self.send_wechat:
                raise ValueError("当前运行环境没有配置微信发送通道")
            ok = bool(self.send_wechat(text))
            traces.span(trace_ctx.get_trace_id(), "channel", "wechat",
                        status="ok" if ok else "error",
                        detail={"sent": ok, "text": text[:200]})
            return {"sent": ok, "channel": "wechat", "queued": False}
        channel = self.settings.channel_get(channel_id)
        if not channel.get("enabled", True):
            raise ValueError(f"渠道 {channel_id} 未启用")
        target = self.channels.target(channel_id) if self.channels else {}
        chat_id = str(args.get("chat_id") or target.get("chat_id") or "").strip()
        platform = str(args.get("platform") or target.get("platform") or "").strip()
        if not chat_id:
            raw = self.settings.store.state_get(
                f"channel:{channel_id}:last_target", "")
            try:
                saved = json.loads(raw) if raw else {}
            except Exception:
                saved = {}
            chat_id = str(saved.get("chat_id") or "").strip()
            platform = platform or str(saved.get("platform") or "").strip()
            target = {"user_id": saved.get("user_id") or ""}
        if not chat_id:
            raise ValueError(
                "不知道这个渠道最近活跃的会话，请先让对方发一条消息，或显式传 chat_id")
        oid = self.settings.store.enqueue_channel(
            channel_id, chat_id, text, platform=platform,
            user_id=str(target.get("user_id") or ""))
        traces.span(trace_ctx.get_trace_id(), "channel", channel_id,
                    detail={"chat_id": chat_id, "platform": platform,
                            "queued": oid, "text": text[:200]})
        self._audit("send_to_channel", f"{channel_id}:{chat_id}")
        return {"queued": oid, "channel": channel_id, "chat_id": chat_id,
                "platform": platform,
                "note": "桥接端下一次轮询时会发送；可用 list_channels 查看待发数量"}

    # ------------------------------------------------------------------
    # evals
    # ------------------------------------------------------------------
    def _eval_cases(self, args: dict) -> dict:
        data = evals.load_cases()
        return {"cases": data["cases"], "runs": _short(evals.runs(5), 300)}

    def _eval_add_case(self, args: dict) -> dict:
        item = evals.add_case(args)
        self._audit("eval_add_case", item["id"])
        return item

    def _default_eval_models(self) -> list[str]:
        providers = self.catalog.resolve("chat")
        return [providers[0]["id"]] if providers else []

    def _eval_run(self, args: dict) -> dict:
        data = evals.load_cases()
        wanted = args.get("case_ids") or args.get("cases")
        if isinstance(wanted, str):
            wanted = [part.strip() for part in wanted.split(",") if part.strip()]
        cases = data["cases"]
        if wanted:
            cases = [item for item in cases if item["id"] in set(wanted)]
        max_cases = max(1, min(int(args.get("max_cases") or 12), 30))
        cases = cases[:max_cases]
        if not cases:
            raise ValueError("没有匹配的评测用例")

        model_ids = args.get("model_ids") or args.get("model_id")
        if isinstance(model_ids, str):
            model_ids = [part.strip() for part in model_ids.split(",") if part.strip()]
        model_ids = list(model_ids or self._default_eval_models())
        if not model_ids:
            raise ValueError("没有可评测的模型，先用 model_bind 绑定 chat")

        summaries = []
        worst: list[dict] = []
        for model_id in model_ids[:4]:
            if not self.catalog.get(str(model_id)):
                raise ValueError(f"模型 {model_id} 不存在")
            results = []
            for case in cases:
                started = time.perf_counter()
                usage: dict = {}
                output = ""
                error = ""
                try:
                    message = self.llm.complete(
                        [{"role": "system",
                          "content": "你在参加自动化评测。严格按用户要求回答，不要调用工具，不要解释。"},
                         {"role": "user", "content": case["prompt"]}],
                        None, max_tokens=220, role="eval",
                        purpose=f"model:{model_id}", usage_sink=usage)
                    output = (message.get("content") or "").strip()
                except Exception as exc:
                    error = str(exc)[:400]
                scored = evals.score_case(case, output)
                result = {
                    "case_id": case["id"],
                    "score": 0.0 if error else scored["score"],
                    "passed": False if error else scored["passed"],
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "cost": float(usage.get("cost") or 0.0),
                    "output": output[:1500],
                    "detail": {"checks": scored["checks"], "error": error} if error
                              else {"checks": scored["checks"]},
                }
                results.append(result)
                if not result["passed"]:
                    worst.append({
                        "model_id": model_id,
                        "case_id": case["id"],
                        "score": result["score"],
                        "error": error,
                        "output": output[:300],
                        "failed_checks": [check["message"] for check in scored["checks"]
                                          if not check["ok"]][:4],
                    })
            summary = evals.record_run(
                str(args.get("label") or "chat"), str(model_id), results,
                {"source": "eval_run"})
            summary["model_id"] = str(model_id)
            summaries.append(summary)
        return {"runs": summaries, "worst": worst[:12]}

    def _eval_report(self, args: dict) -> dict:
        run_id = args.get("run_id")
        data = evals.report(int(run_id)) if run_id else evals.latest(
            str(args.get("model_id") or ""))
        if data is None:
            return {"message": "还没有评测记录，先运行 eval_run",
                    "runs": evals.runs(5)}
        data["results"] = _short(data.get("results") or [], 500)
        return data

    def _eval_drift(self, args: dict) -> dict:
        return evals.drift(int(args.get("baseline_runs") or 5),
                           int(args.get("recent_runs") or 3),
                           str(args.get("model_id") or ""))

    # ------------------------------------------------------------------
    # insight and optimization
    # ------------------------------------------------------------------
    def _collect(self, hours: float) -> tuple[dict, dict, dict | None, dict | None]:
        trace_summary = traces.summary(hours)
        cost_report = spend.report(max(0.1, hours / 24), path=self.spend_path)
        cost_report["budgets"] = spend.budget_status(self.llm.budgets,
                                                     path=self.spend_path)
        providers = self.catalog.resolve("chat")
        chat_model = providers[0]["id"] if providers else ""
        try:
            latest = evals.latest(chat_model)
        except Exception:
            latest = None
        try:
            drift = evals.drift(model_id=chat_model)
        except Exception:
            drift = None
        return trace_summary, cost_report, latest, drift

    def _insight(self, args: dict) -> dict:
        hours = float(args.get("hours") or 24)
        trace_summary, cost_report, latest, drift = self._collect(hours)
        return insights.analyze(trace_summary, cost_report, latest, drift,
                                self.catalog.list())

    def _optimize_suggest(self, args: dict) -> dict:
        return self._insight({"hours": args.get("hours") or 168})

    def _optimize_apply(self, args: dict) -> dict:
        suggestion_id = str(args.get("suggestion_id") or args.get("id") or "").strip()
        report = self._insight({"hours": args.get("hours") or 168})
        found = next((item for item in report.get("suggestions") or []
                      if item.get("id") == suggestion_id), None)
        if found is None:
            raise ValueError(f"没有找到建议 {suggestion_id}，可能情况已经变化，请重新 optimize_suggest")
        action = found.get("action") or {}
        kind = action.get("type")
        if kind == "bind":
            result = self._model_bind({"role": action.get("role"),
                                       "models": action.get("models") or []})
        elif kind == "budget":
            result = self._budget_set({"role": action.get("role"),
                                       "amount": action.get("amount")})
        elif kind == "manual":
            return {"applied": False, "suggestion": found,
                    "message": "这条建议需要人工确认或额外信息：" + str(action.get("hint") or "")}
        elif kind == "none":
            return {"applied": False, "suggestion": found, "message": "没有需要执行的动作"}
        else:
            raise ValueError("这条建议没有可自动执行的动作")
        self._audit("optimize_apply", suggestion_id)
        return {"applied": True, "suggestion": found, "result": result}

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register(self) -> None:
        tools = self.tools
        tools.register(
            "model_list",
            "查看已配置的模型、脱敏密钥、价格标签，以及 chat/vision/judge/job.default 等角色当前绑定。",
            _schema({}),
            _guard(self._model_list),
        )
        tools.register(
            "model_add",
            "新增或更新一个 OpenAI 兼容模型。需要 base_url、model 和 api_key；可为免费模型加 free 标签。不会自动切换 chat。",
            _schema({
                "id": {"type": "string", "description": "模型短 ID，例如 deepseek-flash"},
                "name": {"type": "string"},
                "base_url": {"type": "string", "description": "以 /v1 结尾的 OpenAI 兼容地址"},
                "api_key": {"type": "string"},
                "model": {"type": "string", "description": "请求体里的 model 名"},
                "input_price_per_m": {"type": "number"},
                "output_price_per_m": {"type": "number"},
                "tags": {"type": "string", "description": "逗号分隔，例如 free,cheap,vision"},
                "notes": {"type": "string"},
                "set_chat": {"type": "boolean", "description": "是否立即切为 chat"},
            }, ["base_url", "model"]),
            _guard(self._model_add),
        )
        tools.register(
            "model_remove",
            "删除一个模型，并从所有角色绑定里移除。chat 至少保留一个模型。",
            _schema({"model_id": {"type": "string"}}, ["model_id"]),
            _guard(self._model_remove),
        )
        tools.register(
            "model_bind",
            "把一个或多个模型按顺序绑定到角色。前面失败会自动尝试后面。角色示例：chat、vision、judge、control、job.default、plugin.default。",
            _schema({
                "role": {"type": "string"},
                "models": {"type": "string", "description": "逗号分隔的模型 ID，按优先级排序"},
            }, ["role", "models"]),
            _guard(self._model_bind),
        )
        tools.register(
            "model_switch_chat",
            "切换主聊天模型。等价于把 chat 角色绑定到这一个模型。",
            _schema({"model_id": {"type": "string"}}, ["model_id"]),
            _guard(self._model_switch_chat),
        )
        tools.register(
            "model_test",
            "用一句短提示测试指定模型是否可用，返回延迟、回复和本次成本。",
            _schema({
                "model_id": {"type": "string"},
                "prompt": {"type": "string"},
            }, ["model_id"]),
            _guard(self._model_test),
        )

        if self.with_channels:
            tools.register(
                "list_channels",
                "查看已接入的消息入口（微信、QQ/Koishi 等）、最近活跃会话、待发消息和最近收发记录。",
                _schema({}),
                _guard(self._list_channels),
            )
            tools.register(
                "send_to_channel",
                "把一条文字发到指定入口。channel 可用 list_channels 查看；不传 chat_id 时发到该入口最近活跃的会话。微信固定发到站主。",
                _schema({
                    "channel": {"type": "string", "description": "例如 koishi 或 wechat"},
                    "text": {"type": "string"},
                    "chat_id": {"type": "string", "description": "可选，目标会话 ID"},
                    "platform": {"type": "string", "description": "可选，例如 qq / telegram"},
                }, ["channel", "text"]),
                _guard(self._send_to_channel),
            )

        tools.register(
            "cost_report",
            "按天、角色、模型汇总 AI 花费，并给出今日预算使用情况。",
            _schema({"days": {"type": "number", "description": "统计最近几天，默认 7"}}),
            _guard(self._cost_report),
        )
        tools.register(
            "budget_set",
            "设置某个角色每天的 AI 预算（账户同币种）；金额 0 表示不限。角色通常是 owner、guest。",
            _schema({
                "role": {"type": "string"},
                "amount": {"type": "number"},
            }, ["role", "amount"]),
            _guard(self._budget_set),
        )

        tools.register(
            "trace_search",
            "搜索最近的请求轨迹。可按关键词（消息内容、错误、trace id）和状态过滤。",
            _schema({
                "query": {"type": "string"},
                "hours": {"type": "number", "description": "最近多少小时，默认 24"},
                "status": {"type": "string", "description": "ok / error / budget_exceeded 等"},
                "limit": {"type": "integer"},
            }),
            _guard(self._trace_search),
        )
        tools.register(
            "trace_get",
            "按 trace id 取完整轨迹：消息、模型调用、工具调用、花费和耗时。",
            _schema({"trace_id": {"type": "string"}}, ["trace_id"]),
            _guard(self._trace_get),
        )
        tools.register(
            "trace_explain",
            "按 trace id 解释失败原因并给出处理建议，不返回完整原始数据。",
            _schema({"trace_id": {"type": "string"}}, ["trace_id"]),
            _guard(self._trace_explain),
        )
        tools.register(
            "insight_report",
            "汇总最近一段时间的成功率、延迟、花费、评测与异常，给出诊断和优化建议。",
            _schema({"hours": {"type": "number", "description": "默认 24 小时"}}),
            _guard(self._insight),
        )

        tools.register(
            "eval_cases",
            "查看评测用例和最近几次评测记录。",
            _schema({}),
            _guard(self._eval_cases),
        )
        tools.register(
            "eval_add_case",
            "新增一个评测用例。checks 支持 contains_any / not_contains / regex / max_chars / json。",
            _schema({
                "id": {"type": "string"},
                "prompt": {"type": "string"},
                "checks": {"type": "array", "items": {"type": "object"}},
            }, ["id", "prompt", "checks"]),
            _guard(self._eval_add_case),
        )
        tools.register(
            "eval_run",
            "对当前 chat 模型或指定模型跑评测，记录分数、逐用例结果、延迟和成本。",
            _schema({
                "model_id": {"type": "string"},
                "model_ids": {"type": "string"},
                "case_ids": {"type": "string"},
                "max_cases": {"type": "integer"},
                "label": {"type": "string"},
            }),
            _guard(self._eval_run),
        )
        tools.register(
            "eval_report",
            "查看最近一次或指定 run_id 的评测详情。",
            _schema({
                "run_id": {"type": "integer"},
                "model_id": {"type": "string", "description": "不传 run_id 时按模型取最近一次"},
            }),
            _guard(self._eval_report),
        )
        tools.register(
            "eval_drift",
            "对比最近几次评测与 baseline，判断模型质量是否漂移，并列出变化最大的用例。",
            _schema({
                "baseline_runs": {"type": "integer"},
                "recent_runs": {"type": "integer"},
                "model_id": {"type": "string", "description": "默认当前 chat 模型"},
            }),
            _guard(self._eval_drift),
        )

        tools.register(
            "optimize_suggest",
            "基于轨迹、成本、评测生成可执行的优化建议。只给建议，不直接改配置。",
            _schema({"hours": {"type": "number", "description": "默认 168 小时"}}),
            _guard(self._optimize_suggest),
        )
        tools.register(
            "optimize_apply",
            "执行 optimize_suggest 返回的某条建议。只支持低风险动作：切换角色绑定、调整预算。",
            _schema({"suggestion_id": {"type": "string"}}, ["suggestion_id"]),
            _guard(self._optimize_apply),
        )
