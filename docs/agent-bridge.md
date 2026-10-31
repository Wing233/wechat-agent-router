# lyph ↔ Router 能力桥（Agent Bridge）

本文是 **代码骨架与契约说明**，不包含任何真实密钥，也没有修改线上 `config.json`。

## 1. 方向与边界

```
lyph chat_agent ──POST /agent/tools/{tool}──▶ Router agent_bridge
      │                                          │
      │                                     websearch 插件
      │                                （web_search / web_fetch /
      │                                  weather / calculator）
      │
Router send_to_agent ──POST callback_url──▶ lyph（待实现，见第 5 节）
```

- lyph → Router：四个只读工具，白名单硬编码，无法通过配置放宽。
- Router → lyph：沿用现有 `send_to_agent` + `call_agent`，`transport=http` 时向
  `callback_url` 发送带 `X-Router-Signature` 的 POST。lyph 侧的接收端点尚未实现，
  因此当前只是配置骨架。
- 明确不开放：`ops`、`devtools`、`admin`、`reminders`、`todo`、
  `run_command`、`fs_*`、`http_request`、`send_to_agent`、`generate_image`、
  `send_media`。

## 2. Router 接口契约

### POST `/agent/tools/{tool}`

鉴权：`X-Agent-Key: <lyph 的 api_key>`，也接受
`Authorization: Bearer <key>`。只认 `agents` 配置中 id 属于
`agent_bridge.allowed_agents`（默认 `["lyph"]`）且 `api_key` 非空的条目。

请求体就是工具参数对象（最大 32 KiB）：

```bash
curl -X POST http://router:9900/agent/tools/web_search \
  -H "X-Agent-Key: $ROUTER_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"上海 人工智能 政策","count":5}'
```

成功响应（HTTP 200）：

```json
{
  "ok": true,
  "agent": "lyph",
  "tool": "web_search",
  "result": {
    "provider": "bing",
    "query": "上海 人工智能 政策",
    "warning": "",
    "results": [
      {"title": "标题", "url": "https://example.com/a", "site": "example.com", "snippet": "摘要"}
    ]
  },
  "elapsed_ms": 320
}
```

工具本身失败（HTTP 200，`ok=false`）：

```json
{
  "ok": false,
  "agent": "lyph",
  "tool": "web_fetch",
  "error": {"code": "tool_error", "message": "抓取失败：..."},
  "elapsed_ms": 2510
}
```

桥接层错误：

| HTTP | 场景 | 说明 |
| --- | --- | --- |
| 400 | body 不是 JSON 对象 | 不做任何工具调用 |
| 401 | 缺少/错误 key | 记录 `auth_denied` 审计 |
| 403 | 不在白名单 | 记录 `tool_call` 审计，`ok` 不返回 |
| 413 | body 超过 32 KiB | |
| 429 | 超过 60 次/分钟 | 返回 `Retry-After` 秒数 |

结果上限 256 KiB（序列化后）；超出时返回 `ok=false`、
`error.code="result_too_large"`。

### GET `/agent/health`

无需鉴权，只暴露服务状态、可用工具名、Agent id 和限流值，不暴露密钥：

```json
{
  "ok": true,
  "service": "agent-bridge",
  "time": "2026-10-31T00:00:00+00:00",
  "agents": ["lyph"],
  "tools": ["calculator", "weather", "web_fetch", "web_search"],
  "rate_limit_per_minute": 60
}
```

## 3. 白名单与限流

| lyph 工具名 | Router 内部工具 | 允许 |
| --- | --- | --- |
| `web_search` | `websearch__web_search` | ✅ |
| `web_fetch` | `websearch__web_fetch` | ✅ |
| `weather` | `websearch__weather` | ✅ |
| `calculator` | `websearch__calculator` | ✅ |
| 其他任意名称 | — | ❌ 403 |

- 限流：滑动窗口 **60 次/分钟/Agent**（按 agent id 计数，默认 60）。
- 审计：写入 `plugin_events`，`plugin_id="agent_bridge"`，action 为
  `tool_call` / `auth_denied` / `rate_limited`；detail 含 agent、工具名、参数
  （截断 1000 字符）、耗时、成功与否，**不记录 api_key**。
- `web_fetch` 的公网限制由 `plugins/websearch/plugin.py` 现有逻辑负责：
  仅 http/https、80/443、解析后必须是公网 IP，拒绝本机/内网/保留地址。

## 4. 配置示例（占位，不要提交真实值）

Router `config.json` 片段（线上文件由主代理合并，本仓库示例仅作参考）：

```json
{
  "agents": [
    {
      "id": "lyph",
      "name": "lyph 站点",
      "description": "lyph 公开站点与聊天 Agent",
      "transport": "http",
      "callback_url": "http://api:8000/api/v1/agent/router-callback",
      "health_url": "http://api:8000/health",
      "callback_secret": "REPLACE_ROUTER_TO_LYPH_HMAC",
      "api_key": "REPLACE_LYPH_TO_ROUTER_KEY"
    }
  ],
  "agent_bridge": {
    "allowed_agents": ["lyph"]
  }
}
```

- `api_key`：Router 校验 lyph 调用 `/agent/tools/*` 的凭证，同时对应 lyph 的
  `ROUTER_AGENT_TOKEN`。两处必须相同。bridge 读取运行时 `AGENTS` 映射，
  后台改 key 或删除 Agent 后立即生效，无需重启。
- `callback_url` / `callback_secret`：Router 调 lyph 用。lyph 需实现校验
  `X-Router-Signature` 的接收端点；在实现前不要启用 `send_to_agent` 到 lyph。
- `health_url`：供人工/监控探活，当前 Router 不自动轮询。
- `agent_bridge.allowed_agents` 可省略，省略时默认只允许 `lyph`。

lyph `.env` 占位（不要写真实 key）：

```dotenv
# Router 只读能力桥接；值由部署时注入，不要提交真实密钥
ROUTER_URL=http://wechat-agent-router:9900
ROUTER_AGENT_TOKEN=
```

## 5. 集成步骤（由主代理合并）

1. Router 增加一行导入（放在其他本地模块导入附近）：

   ```python
   import agent_bridge
   ```

2. 在 `app = FastAPI(...)` 之后增加一行注册（`CFG`/`TOOLS`/`store` 已定义）：

   ```python
   agent_bridge.register(app)
   ```

3. 在 Router 的 `config.json` 的 `agents` 中加入 lyph 条目（键名见上），并确保
   `api_key` 与 lyph 的 `ROUTER_AGENT_TOKEN` 一致。
4. lyph 部署环境注入 `ROUTER_URL`、`ROUTER_AGENT_TOKEN`；不需要新增 FastAPI 路由，
   四个工具直接由 `chat_agent` 调用。
5. 若要从微信把任务转给 lyph，再实现 lyph 的 callback 接收端点（校验 HMAC），
   然后把该 Agent 暴露给 `send_to_agent`。

## 6. 测试

Router：

```powershell
cd D:\xx\wechatbot\agent-router
python -m pytest tests\test_agent_bridge.py -q
```

lyph：

```powershell
cd D:\xx\lyph\api
.\.venv\Scripts\python -m pytest tests\test_chat_tools.py tests\test_chat_tools_router.py -q
```

## 7. 风险与已知限制

- **密钥是共享凭证**：任何拿到 lyph 容器环境变量的人都能调用这四个工具；
  轮换时同时更新 Router `agents[lyph].api_key` 与 lyph `ROUTER_AGENT_TOKEN`。
  该 key 同时能通过 Router 现有的 `/agent/register|tasks|result` 鉴权；lyph 只应使用
  `/agent/tools/*`。若要彻底隔离，后续应增加专用 bridge key 或在 broker 路由上按
  agent 限制。
- **公网抓取是开放世界**：`web_fetch` 只允许公网 80/443，但目标内容不可信；
  lyph 侧把返回文本视为不可信数据，模型指令不能被网页内容覆盖。
- **同步阻塞**：`web_fetch` 在 Router 最多 25 秒，桥接用线程池执行，仍会占用
  worker；60/min 限流可防止单 Agent 打满。
- **错误语义**：工具失败返回 HTTP 200 + `ok=false`，调用方必须同时检查
  `ok` 与 HTTP 状态；只有鉴权/白名单/限流/body 错误使用 4xx。
- **MCP 自动暴露**：lyph 的 `TOOL_DEFINITIONS` 同时供 MCP 使用，新增的四个工具
  会自动出现在 MCP 工具列表；MCP 凭证持有者也能调用，需在 lyph 侧按需限流/吊销。
- **Router → lyph 未闭环**：lyph 尚无 `callback_url` 接收端点，`send_to_agent`
  到 lyph 会失败；这是刻意保留的骨架，避免在未实现签名校验前开放写能力。
