# WeChat Agent Router

把微信变成你自己的 Agent 遥控器：消息进来，Router 负责权限、排队、状态和回传；Agent 负责真正执行。

它不是另一个聊天模型，也不是通用客服 SaaS。主路径只有一条：

```text
微信 / OpenILink Hub
        ↓
Router :9900  （入站去重、权限判定、任务状态、结果回传）
        ↓
Agent（本机 local-agent / lyph / 其他 HTTP Agent）
        ↓
结果回到微信
```

## 它解决什么

- 人在微信里发一句话，远端 Agent 可靠执行，完成后把结果送回来。
- Agent 离线、超时、崩溃、重启时，任务不丢，也不会假称“已交给”。
- 主人和访客走不同的权限路径；高危动作需要 Mandate 或人工确认。
- Python/Shell、文件写入、补丁可以按需切到独立执行器，缩小误操作和提示注入的影响范围。

## 首次使用主路径

### 0. 前置

- Docker / Docker Compose。
- 一个 OpenILink Hub（或兼容的消息 Hub），Router 通过它收发微信消息。
- `lyph-api:0.1.0` 基础镜像；`Dockerfile` 以它为 `FROM`。
- 如果还没有 `openilink-hub_default` / `lyph_private` 网络，先启动对应服务，或从 `docker-compose.yml` 删掉暂时不用的 external network。

### 1. 准备配置

```bash
cp config.example.json config.json
```

至少填写：

```json
{
  "hub": {
    "base_url": "http://openilink-hub:9800",
    "channel_key": "你的 Hub channel key",
    "owner_id": "你的微信 ID"
  },
  "llm": {
    "base_url": "https://api.deepseek.com",
    "api_key": "你的模型 key",
    "model": "deepseek-flash"
  },
  "agents": [
    {
      "id": "pc-home",
      "name": "我的电脑",
      "transport": "http",
      "api_key": "与 local-agent 的 agent_key 一致"
    }
  ]
}
```

### 2. 启动 Router

```bash
docker compose up -d router
curl -s http://127.0.0.1:9900/health
```

健康检查应返回 `"ok": true`，并能看到已加载的 `tools`、`plugins` 和 `jobs`。

### 3. 启动一个 Agent

本机 Agent：

```bash
cd local-agent
cp config.example.json config.json
# 填写 router、agent_id、agent_key，以及本机 workspace / engine 路径
python agent.py --config config.json
```

远端 Agent 也可以直接实现 Router 的任务协议：

```text
GET  /agent/tasks?agent_id=pc-home
POST /agent/tasks/{task_id}/heartbeat   {"lease_token": "..."}
POST /agent/result                     {"task_id":"...", "ok":true, "text":"...", "lease_token":"..."}
```

### 4. 发第一条消息

在微信里给 Router 绑定的主人账号发一条明确指令，例如：

```text
@pc-home 看一下当前目录，告诉我有哪些文件
```

Router 会先落库入站消息，再创建 Agent 任务；Agent 领取后执行，结果通过 Hub 回到微信。Agent 没有及时领取时，Router 会如实回复“已排队/未领取”，不会假装已经交给它。

## 常用配置

| 配置 | 作用 |
|---|---|
| `hub.base_url` / `hub.channel_key` / `hub.owner_id` | 连接 OpenILink Hub，接收和发送微信消息 |
| `llm.*` | Router 自身的意图判定、摘要和工具循环 |
| `agents[]` | 可被 `@` 或委派的 Agent；`api_key` 同时用于 Agent 认证 |
| `executor.mode` | `host`（默认，高权限）或 `isolated`（独立非 root 执行器） |
| `governance` / `toolbox` | 入站 Gate、Mandate、工具与 MCP 配置 |

## 执行器模式

默认是 `host`，适合个人使用：Python/Shell 和文件操作在 Router 容器里执行，能访问 `/app`、`/data`、`/lyph-src` 等挂载。

需要降低影响范围时切到 `isolated`：

```json
"executor": {
  "mode": "isolated",
  "url": "http://executor:9800",
  "token": "单独生成的 token",
  "allow_control_plane": false
}
```

`isolated` 下 Python/Shell、`write_patch`、`ops__fs_*` 走独立执行器，只挂 `/workspace`，非 root、只读根文件系统、无外网；控制面工具默认拒绝。执行器不可用时直接报错，不会回退到 host。详见 [docs/executor-isolation.md](docs/executor-isolation.md)。

## 状态

版本 `0.1.0`，当前生产 `main`。状态按“代码存在 ≠ 已验收”标注。

| 能力 | 状态 | 边界 |
|---|---|---|
| 入站消息持久化、去重、重试、崩溃恢复 | 稳定 | `tests/test_task_state.py`；at-least-once |
| Agent 任务 `queued/claimed/running/completed/failed`、lease、heartbeat、单次结果投递 | 稳定 | 长任务依赖 Agent 侧 heartbeat |
| 微信/Hub 回传、outbox 重试 | 稳定 | 依赖 Hub 可达；失败会排队 |
| 入站 Gate、Mandate、高危工具审批 | 稳定 | 只读工具是精确白名单 |
| `host` 执行器（Python/Shell/文件/补丁） | 稳定 | 个人高权限模式，默认 |
| `isolated` 执行器（Python/Shell/文件/补丁） | 实验 | 默认关闭；控制面默认 deny；完整多租户隔离未做 |
| 浏览器/MCP/多 Agent 委派 | 实验 | 取决于外部服务和配置 |
| Windows 桌面控制台、其他 Agent 适配 | 未验收 | 以对应 Agent 的 README/真机验证为准 |

## 文档

- [任务状态与重启恢复](docs/task-state.md)
- [入站 Gate 与 Mandate](docs/governance.md)
- [可选执行器隔离](docs/executor-isolation.md)
- [app.py 拆分评估](docs/app-py-split.md)
- [Agent bridge](docs/agent-bridge.md)

## 测试

```bash
bash scripts/ci.sh
```

它等价于：

```bash
python -m py_compile app.py agent_broker.py executor_client.py executor/server.py governance.py registry.py store.py
python -m pytest -q tests
```

CI 会运行同一套离线测试；真实 Hub、微信和外部 Agent 的联调不放进 CI。
