# lyph ↔ Router 能力桥（Agent Bridge）

本文是 **代码骨架与契约说明**，不包含任何真实密钥，也没有修改线上 `config.json`。

## 1. 方向与边界

```
lyph chat_agent ──POST /agent/tools/{tool}──▶ Router agent_bridge
      │                                          │
      │                                     websearch 插件
      │                                （web_search / web_fetch /
      │                                  weather / calculator）
      │                                     lyph_tools 插件
      │                                （公开资料 / 已授权内容 /
      │                                  企业快照 / 游戏条目）
      │
Router send_to_agent ──POST callback_url──▶ lyph /api/v1/internal/router/task
```

- lyph → Router：十个只读工具（4 个联网 + 6 个 lyph 公开域），白名单硬编码，
  无法通过配置放宽。lyph 侧只保留入口、权限、会话、配额、缓存与 UI 渲染。
- Router → lyph：沿用现有 `send_to_agent` + `call_agent`，`transport=http` 时向
  `callback_url` 发送带 `X-Router-Signature` 和 `X-Router-Timestamp` 的 POST。
  lyph 侧接收端点是 `api/lyph/agent_protocol.py` 的
  `POST /api/v1/internal/router/task`：对原始请求体做 HMAC-SHA256，时间戳超过
  300 秒即拒绝，密钥取自 lyph 的 `ROUTER_CALLBACK_SECRET`（不足 32 字符时端点
  返回 503，即未启用）。该端点只做整轮问答，不是工具级回调。
- 明确不开放：`ops`、`devtools`、`admin`、`reminders`、`todo`、
  `run_command`、`fs_*`、`http_request`、`send_to_agent`、`generate_image`、
  `send_media`。

## 2. Router 接口契约

### POST `/agent/tools/{tool}`

鉴权：`X-Agent-Key: <lyph 的 bridge_key>`，也接受
`Authorization: Bearer <key>`。只认 `agents` 配置中 id 属于
`agent_bridge.allowed_agents`（默认 `["lyph"]`）且 `bridge_key` 或 `api_key`
非空的条目；两者都存在时**只接受 `bridge_key`**，未配置 `bridge_key` 才回落到
`api_key`。这是为了让工具凭证与 broker 凭证（`/agent/register|tasks|result`）分离。

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
  "tools": ["aggregate_companies", "calculator", "get_company", "get_public_content",
            "get_public_profile", "search_companies", "search_public_content",
            "weather", "web_fetch", "web_search"],
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
| `get_public_profile` | `lyph_tools__get_public_profile` | ✅ |
| `search_public_content` | `lyph_tools__search_public_content` | ✅ |
| `get_public_content` | `lyph_tools__get_public_content` | ✅ |
| `search_companies` | `lyph_tools__search_companies` | ✅ |
| `get_company` | `lyph_tools__get_company` | ✅ |
| `aggregate_companies` | `lyph_tools__aggregate_companies` | ✅ |
| 其他任意名称 | — | ❌ 403 |

白名单的键是 lyph 侧工具名，值才是 Router 内部工具名。`FORBIDDEN_TOOL_NAMES` /
`FORBIDDEN_TOOL_PREFIXES` 只检查**传入名**，所以把 `lyph_tools__*` 当作目标不会
误伤；反过来，任何以 `site__`/`leetcode__`/`ops__`/`devtools__`/`admin__` 等
前缀传入的请求一律 403，即使它出现在某个插件的注册表里。

- 限流：滑动窗口 **60 次/分钟/Agent**（按 agent id 计数，默认 60）。
- 审计：写入 `plugin_events`，`plugin_id="agent_bridge"`，action 为
  `tool_call` / `auth_denied` / `rate_limited`；detail 含 agent、工具名、参数
  （截断 1000 字符）、耗时、成功与否，**不记录 api_key**。
- `web_fetch` 的公网限制由 `plugins/websearch/plugin.py` 现有逻辑负责：
  仅 http/https、80/443、解析后必须是公网 IP，拒绝本机/内网/保留地址。

## 3.1 数据来源与挂载

六个 `lyph_tools__*` 工具直读 lyph 的公开数据，不经过 lyph 的 HTTP 接口：

| 数据 | 路径（容器内） | 说明 |
| --- | --- | --- |
| 站点资料、已授权内容 | `/lyph-data/lyph.sqlite3` | `site_settings`、`contents`，只读连接 |
| 企业公开快照 | `/lyph-src/public-apps/companies/data/web.sqlite` | `company`、`meta.built_at` |
| 游戏公开条目 | 同一主库 `gaming_accounts` | 只读 `link_method` 派生列，**不读 `credential_enc`** |

- 配置项：`lyph.db`、`lyph.enterprise_db`（见 `config.example.json`）。
- compose：`/opt/lyph → /lyph-src`、lyph_business 卷 → `/lyph-data`，可用
  `LYPH_SRC_PATH` / `LYPH_DATA_PATH` 覆盖。
- 只读保障：`mode=ro` + `query_only` + `trusted_schema=OFF` + 禁用扩展加载 +
  2 秒进度回调超时；模型参数只作为绑定值，永远不进 SQL 标识符。
- `gaming_accounts.link_method` 由 lyph 在绑定/连接/同步时写入，lyph 启动时回填。
  Router 只读该列；列为空时丢弃该平台的公开条目，而不是猜一个“已验证”标签。

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
      "callback_url": "http://lyph-api-1:8000/api/v1/internal/router/task",
      "health_url": "http://lyph-api-1:8000/health",
      "callback_secret": "REPLACE_ROUTER_TO_LYPH_HMAC",
      "api_key": "REPLACE_BROKER_KEY",
      "bridge_key": "REPLACE_LYPH_TO_ROUTER_KEY"
    }
  ],
  "agent_bridge": {
    "allowed_agents": ["lyph"]
  }
}
```

- `bridge_key`：Router 校验 lyph 调用 `/agent/tools/*` 的凭证，对应 lyph 的
  `ROUTER_AGENT_TOKEN`。两处必须相同。未配置时回落 `api_key`；生产应只配
  `bridge_key`，避免工具凭证同时具备 broker 权限。
- `api_key`：`/agent/register|tasks|result|heartbeat` 的凭证，不应给 lyph 的工具调用使用。
  bridge 读取运行时 `AGENTS` 映射，后台改 key 或删除 Agent 后立即生效，无需重启。
- `callback_url` / `callback_secret`：Router 调 lyph 用，对应 lyph 的
  `ROUTER_CALLBACK_SECRET`。端点已实现（`/api/v1/internal/router/task`），只接受
  整轮问答；不要把它当工具级回调，也不要借它扩大权限。
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
5. 若要从微信把任务转给 lyph：lyph 侧端点已存在，只需在两边配置
   `callback_secret` / `ROUTER_CALLBACK_SECRET`（≥32 字符）并把该 Agent 暴露给
   `send_to_agent`。注意该端点经 Nginx `/api/` 反代在公网可达，仅靠 HMAC 保护。

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
- **表结构耦合**：直读意味着 lyph 改 `site_settings`/`contents`/`company` 的
  字段时 Router 必须同步改；快照缺列会返回 `unavailable` 而不是静默降级。
  部署顺序应为先 lyph（完成 `link_method` 回填）再 Router。
- **公网抓取是开放世界**：`web_fetch` 只允许公网 80/443，但目标内容不可信；
  lyph 侧把返回文本视为不可信数据，模型指令不能被网页内容覆盖。
- **同步阻塞**：`web_fetch` 在 Router 最多 25 秒，桥接用线程池执行，仍会占用
  worker；60/min 限流可防止单 Agent 打满。
- **错误语义**：工具失败返回 HTTP 200 + `ok=false`，调用方必须同时检查
  `ok` 与 HTTP 状态；只有鉴权/白名单/限流/body 错误使用 4xx。
- **MCP 自动暴露**：lyph 的 `TOOL_DEFINITIONS` 同时供 MCP 使用，新增的四个工具
  会自动出现在 MCP 工具列表；MCP 凭证持有者也能调用，需在 lyph 侧按需限流/吊销。
- **Router → lyph 仅整轮问答**：`/api/v1/internal/router/task` 用 `chat_agent`
  跑完整一轮并同步返回文本，没有工具级回传、取消或幂等；回调地址经公网 Nginx 暴露，
  当前只有 HMAC 保护，不要在此基础上叠加新的写能力。
