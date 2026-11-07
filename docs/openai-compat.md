# OpenAI 兼容免费模型网关

`POST /v1/chat/completions` 让 lyph 这类已经使用 OpenAI Chat Completions 协议的客户端，直接复用 Router 的模型目录、免费模型优先顺序和 fallback，而不需要改自己的工具循环或 UI。

## 鉴权

只接受 Router 的 service token：

```http
X-Service-Token: <router service_token>
```

或：

```http
Authorization: Bearer <router service_token>
```

不要把该 token 放进浏览器、公开仓库或 lyph 前端；它只应保存在 lyph API 的 `.env` 中。

## 请求

- `model`：`free-router`、`chat` 或 `lyph`，都会解析到 `purpose=chat` 的绑定链。
- `messages`：标准 OpenAI messages，最多 60 条、总计约 200 KB。
- `tools`：标准 OpenAI function tools，最多 40 个。
- `tool_choice`：`auto`/`none`/`required` 或指定单个函数的对象。`none` 时不会把工具定义传给上游模型；指定函数时只保留对应定义。
- `max_tokens`：可选，最大 8192。
- `stream`：支持 SSE；Router 内部是非流式模型调用，因此首包会一次性给出完整文本或 tool_calls。
- `stream_options.include_usage`：为 true 时最后发送 usage chunk。

```bash
curl http://127.0.0.1:9900/v1/chat/completions \
  -H "X-Service-Token: $ROUTER_SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "free-router",
    "messages": [{"role": "user", "content": "只回复 PONG"}],
    "max_tokens": 16,
    "stream": false
  }'
```

## 行为

- 使用 `llm.complete(..., role="guest", purpose="chat")`，按 `model_bindings.chat` 的顺序尝试，例如 `kilo-free` → `qoder-cn` → `trae-doubao` → `raccoon-sensenova` → `opencode-go` → `deepseek-flash`。
- 免费模型成功时成本为 0；只有 fallback 到付费模型才计入 guest 预算。
- 返回标准的 `chat.completion` / `chat.completion.chunk`，包含 `tool_calls`、`finish_reason` 和 `usage`。
- `tool_choice="none"` 保证最终合成阶段不再产生 `tool_calls`；不合法的 `tool_choice` 返回 400。
- Provider 全部失败时返回 503；预算耗尽返回 429。不会把供应商原始错误体透传给客户端。

## lyph 侧配置

在 lyph 后台或模型配置中使用：

```text
provider: wechat-agent-router
base_url: http://wechat-agent-router:9900/v1
model: free-router
api_key: <ROUTER_SERVICE_TOKEN>
```

lyph 的 `models_routes.validate_endpoint` 对 `provider=wechat-agent-router` 放行这个内网地址，其他 provider 仍然只允许 HTTPS 公网端点。
