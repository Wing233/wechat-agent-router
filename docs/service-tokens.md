# 用途隔离的 service token

Router 以前只有一个 `service_token`，推理客户端拿到的凭证同时能改 Agent、插件
和定时任务。现在按用途拆成三个：

```json
{
  "service_token": "<旧共享令牌，仍作为 admin 兜底>",
  "service_tokens": {
    "inference": "<只用于模型推理和只读工具>",
    "notify": "<只用于推送通知>",
    "admin": "<只用于管理接口>"
  }
}
```

## 解析规则

`service_tokens.resolve()` 对每个用途单独取值。某个用途没有配置时，才回退到
旧 `service_token`。因此可以分步迁移：

1. 先给 `inference` 和 `notify` 生成独立值，`admin` 留空继续用旧令牌；
2. 更新客户端后，再把 `admin` 也换成独立值并删除旧 `service_token`。

旧 `service_token` 不会因为配置了某个用途就获得该用途的权限。

## 接口矩阵

| 用途 | 允许的接口 |
| --- | --- |
| `inference` | `POST /v1/chat/completions`、`POST /internal/ask`、`POST /internal/leetcode/stats`、`POST /internal/leetcode/question` |
| `notify` | `POST /agent/notify`、`POST /v1/notify`、`GET /v1/notify/{id}`、`POST /v1/notify/{id}/cancel` |
| `admin` | 其余 `/internal/settings/*`、`/internal/plugins/*`、`/internal/jobs/*`、`/internal/reminders/tick` |

`/agent/notify` 和 `/v1/notify` 仍接受已注册 Agent 的 `X-Agent-Key`；service token
只是给只推送、不调用工具的集成使用。通过 notify token 创建的通知归属
`agent_id=service`，不能读取其他 Agent 的通知。

## 请求头

- `X-Service-Token: <token>`
- 或 `Authorization: Bearer <token>`（仅 `/v1/chat/completions` 和通知接口）

失败的请求返回 `401 invalid <scope> service token`，不会提示其他用途的令牌是否正确。

## 客户端配置

- Lyph 推理：`.env` 中 `ROUTER_INFERENCE_TOKEN`（模型客户端优先读取）和
  `ROUTER_SERVICE_TOKEN`（力扣只读工具）都填 inference token。
- Lyph 通知：`.env` 中 `ROUTER_NOTIFY_TOKEN`；未配置时继续用
  `ROUTER_AGENT_TOKEN` 的 Agent Key。
- 桌面端 / evolution 插件：使用 admin token。插件侧仍通过原来的
  `service_token` secret 名获取，值由 Router 注入为 admin token。

## 轮换

1. 在 `config.json` 的 `service_tokens` 中替换目标用途的值；
2. `docker compose up -d --force-recreate router`；
3. 更新对应客户端的 `.env` 或桌面端保存的令牌；
4. 用 `401` 验证旧令牌已被拒绝，且其他用途不受影响。

## 部署记录（2026-11-10）

- Router 从 `0f59165` 快进到 `7646bf2` 并重建为 `wechat-agent-router:dev`，镜像 `sha256:0809af2c95986f0eca306ff4946e290327ca5027de086a7171efb49cba32cb64`；回退镜像 `wechat-agent-router:rollback-scoped-20261110-114026`。
- `config.json` 新增 `service_tokens.inference` 与 `service_tokens.notify`（48 字节随机值），`admin` 仍回退到 legacy `service_token`；`.env` 写入 `ROUTER_INFERENCE_TOKEN`、`ROUTER_NOTIFY_TOKEN`，`ROUTER_SERVICE_TOKEN` 改为 inference token。
- 隔离验证全部通过：inference 只能调用 `/v1/chat/completions` 和 `/internal/leetcode/*`；notify 只能推通知；admin 只能访问管理接口；legacy token 不再能调用 inference 或 notify。
- Lyph API `ROUTER_INFERENCE_TOKEN`、`ROUTER_NOTIFY_TOKEN` 已写入 `/opt/lyph/.env` 并重建 API 容器；`lyph-api:0.1.0` digest `sha256:1dd280573a8bce669e9813bae34e7d92a8583d3803485cfbe41b3b8b2361351e`（b06b9ec），health 200，所有相关环境变量已确认 set。
- 回退：`docker tag wechat-agent-router:rollback-scoped-20261110-114026 wechat-agent-router:dev && docker compose up -d --no-deps --force-recreate router`，并恢复 `/opt/wechat-agent-router/config.json.bak-20261110-194045` 和 `/opt/lyph/.env.bak-20261110-194045`。
