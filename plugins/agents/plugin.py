"""Agent routing capability: one tool that dispatches to registered project agents."""
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
        "把用户请求转交给已注册的项目 Agent 处理，例如爬虫、部署。",
        _schema({
            "agent_id": {"type": "string", "enum": sorted(k for k in agents if k != "echo") or ["none"]},
            "message": {"type": "string"},
            "engine": {"type": "string", "description": "可选：opencode / codex / claude；不填用本地默认"},
            "workspace": {"type": "string", "description": "可选：工作区别名或绝对路径；不填用默认工作区"},
        }, ["agent_id", "message"]),
        send_to_agent,
        aliases=("send_to_agent",),
    )
    ctx.log.info("agent router registered (%s)", sorted(agents))
    ctx.on_unload(lambda: ctx.log.info("agents plugin unloaded"))
