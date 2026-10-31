"""Agent routing capability: one tool that dispatches to registered project agents.

Registered agents are configuration, not code. A ``transport: http`` agent such
as ``lyph`` receives a POST to its ``callback_url`` with an
``X-Router-Signature`` HMAC; ``transport: poll`` agents pull from the broker.
The reverse direction (lyph -> router read-only tools) is served by
``agent_bridge.py`` and never goes through this tool.
"""
from __future__ import annotations


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


def apply(ctx) -> None:
    agents = ctx.resource("agents")
    call_agent = ctx.resource("call_agent")
    owner = ctx.root_config.get("owner", "")

    def send_to_agent(args: dict) -> dict:
        agent = agents.get(args.get("agent_id"))
        if not agent:
            return {"error": "未注册的 Agent"}
        if agent.get("dispatch") is False:
            return {"error": "该 Agent 只接入了只读工具桥，暂未开启任务派发"}
        try:
            result = call_agent(agent, {
                "notification_id": "",
                "agent_id": agent["id"],
                "reply": args.get("message", ""),
                "engine": args.get("engine", ""),
                "workspace": args.get("workspace", ""),
                "meta": {"kind": "task"},
                "chat_id": owner,
            })
            return {"agent": agent["id"], "response": result.get("text") or "",
                    "final": result.get("final", True)}
        except Exception as exc:
            return {"error": f"Agent 调用失败：{exc}"}

    ctx.tools.register(
        "send_to_agent",
        "把用户请求转交给已注册的项目 Agent 处理，例如 lyph 站点问答、爬虫、部署。",
        _schema({
            "agent_id": {"type": "string", "enum": sorted(
                k for k, item in agents.items()
                if k != "echo" and item.get("dispatch") is not False) or ["none"]},
            "message": {"type": "string", "description": "完整问题或任务；HTTP Agent（如 lyph）只接收文本"},
            "engine": {"type": "string", "description": "可选：opencode / codex / claude；仅本地 poll Agent 使用，HTTP Agent 忽略"},
            "workspace": {"type": "string", "description": "可选：工作区别名或绝对路径；仅本地 poll Agent 使用"},
        }, ["agent_id", "message"]),
        send_to_agent,
        aliases=("send_to_agent",),
    )
    ctx.log.info("agent router registered (%s)", sorted(agents))
    ctx.on_unload(lambda: ctx.log.info("agents plugin unloaded"))
