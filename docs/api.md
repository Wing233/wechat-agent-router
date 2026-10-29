# API 参考

Router 监听 `9900`。除 `/health` 和登录页外，内部接口都需要 `X-Service-Token`。

## 认证

### Agent

```http
X-API-Key: <agents[].api_key>
```

或：

```http
Authorization: Bearer <agents[].api_key>
```

### 本机 Agent

```http
X-Agent-Key: <agents[].api_key>
```

### 内部 / 桌面端

```http
X-Service-Token: <service_token>
```

## Agent API

### POST /v1/notify

主动发通知；`expect_reply=true` 时生成 4 位回复码。

```bash
curl -X POST http://127.0.0.1:9900/v1/notify \
  -H "X-API-Key: $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "部署完成，是否继续？",
    "expect_reply": true,
    "ttl_seconds": 900,
    "meta": {"task": "deploy"}
  }'
```

响应：

```json
{"id": "...", "code": "ABCD", "status": "sent"}
```

字段：

| 字段 | 说明 |
|---|---|
| `text` | 文本，最多 2000 字符 |
| `image_base64` | 可选，PNG/JPEG data URI 或纯 base64 |
| `expect_reply` | 是否需要用户回复 |
| `ttl_seconds` | 60–86400，默认 900 |
| `meta` | 任意 JSON，回复时原样带回 |
| `thread_id` | 可选线程 id |

### GET /v1/notify/{id}

```json
{
  "id": "...",
  "code": "ABCD",
  "status": "pending",
  "reply_text": "",
  "created_at": "...",
  "expires_at": "..."
}
```

### POST /v1/notify/{id}/cancel

取消 pending 通知。

## 本机 Agent API

### POST /agent/register

```json
{
  "engines": ["opencode", "codex", "claude"],
  "workspaces": ["router", "scratch"],
  "default_engine": "opencode",
  "default_workspace": "router"
}
```

### GET /agent/tasks?agent_id=pc-home

- 有任务：返回 `task_id` + payload
- 无任务：`204 No Content`

### POST /agent/result

```json
{"task_id": "...", "ok": true, "text": "输出内容"}
```

## 内部 API

### GET /health

返回：

```json
{
  "ok": true,
  "time": "...",
  "agents": ["echo", "pc-home"],
  "tools": ["..."],
  "jobs": ["leetcode_check"],
  "plugins": ["admin", "..."],
  "services": ["..."],
  "credentials": ["leetcode"],
  "agent_broker": {"agents": {}, "pending": {}}
}
```

### POST /internal/ask

```bash
curl -X POST http://127.0.0.1:9900/internal/ask \
  -H "X-Service-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"力扣检查间隔是多久"}'
```

### GET /internal/settings/overview

桌面端总览：agents / plugins / jobs / config / services / tools / logs / events。

### PATCH /internal/settings/config

```json
{"key": "jobs.leetcode_check.interval_minutes", "value": 240}
```

只允许安全字段；密钥不可写。

### PATCH /internal/settings/jobs/{id}

```json
{"interval_minutes": 240, "window_minutes": 0, "enabled": true}
```

### POST /internal/settings/jobs/{id}/run

立即执行一次。

### POST /internal/plugins/{id}/reload

热重载插件。

### POST /internal/plugins/{id}/enable

启用插件。

### POST /internal/plugins/{id}/disable

停用插件。

### GET /internal/settings/logs

最近日志（内存环形缓冲）。

## 登录页

### GET /login/{service}?token=...

用于力扣 Cookie 绑定。`token` 由 Router 用 `login_token_secret` HMAC 签发，TTL 1800 秒。

### POST /login/{service}

提交 Cookie，服务端校验后加密存 `credentials`。

## 错误格式

```json
{"detail": "invalid service token"}
```

| 状态码 | 含义 |
|---|---|
| 400 | 参数错误 |
| 401 | 认证失败 |
| 403 | Agent 不匹配 |
| 404 | 通知/插件不存在 |
| 409 | 插件状态冲突 / Hub 窗口过期 |
| 503 | Hub/Bot 不可用 |
