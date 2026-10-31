# 架构

## 总览

微桥分为三层：

```text
接入层   OpeniLink Hub / Koishi  ←→  Channel Adapter  ←→  WeChat / QQ
路由层   Router（FastAPI + SQLite）
执行层   插件 / 本机 Agent / 外部 API
```

Router 是唯一有状态的服务。桌面端和本机 Agent 都是客户端。

## 进程与端口

| 组件 | 进程 | 端口 | 说明 |
|---|---|---|---|
| Router | `uvicorn app:app` | 9900 | 常驻服务 |
| OpeniLink Hub | `oih` | 9800 | 外部依赖 |
| 桌面控制台 | `desktop/main.py` | 127.0.0.1:8765 | 仅本机 |
| 本机 Agent | `local-agent/agent.py` | 无 | 出站轮询 |

## Router 启动流程

1. 读取 `ROUTER_CONFIG`（默认 `/app/config.json`）
2. 校验 `service_token`、`login_token_secret`、`credential_key`、`hub.channel_key`、`llm.api_key`、Agent Key，拒绝 `REPLACE` 占位符
3. 初始化 `Store`（SQLite WAL）
4. 初始化 `Hub`、`LLM`、`Attachments`、`ImageGen`
5. 注册核心工具 `send_media`、`generate_image`
6. 扫描 `plugins/*/manifest.json` 并加载
7. 启动 4 个循环：
   - `poll_loop`：拉 Hub 消息
   - `maintenance_loop`：过期通知、outbox 重试
   - `job_loop`：定时任务
   - `reminder_loop`：提醒

## 数据模型

| 表 | 用途 |
|---|---|
| `notifications` | 通知/审批：id、code、agent_id、status、meta、TTL、回复 |
| `messages` | 对话历史：chat_id、role、content |
| `outbox` | 微信发送失败重试 |
| `channel_outbound` | 其他渠道出站队列：租约、重试、确认 |
| `state` | KV：游标、last_message_id、todo 等 |
| `credentials` | 加密凭据：service、account、secret_json、status |
| `routing_log` | 消息路由审计 |
| `reminders` | 提醒定义 |
| `reminder_runs` | 提醒执行去重 |
| `plugin_events` | 插件加载/卸载/操作审计 |

SQLite 使用 WAL + `busy_timeout=10000`，单进程写入。

## 多渠道消息层

`channels.py` 定义三个核心对象：

| 对象 | 作用 |
|---|---|
| `Endpoint` | 具体会话：`wechat:owner`、`qq:group:123456`、`qq:private:789` |
| `Principal` | 发起人：`owner` 或 `guest:<channel>:<id>` |
| `Envelope` | 归一化消息：channel、endpoint、sender、text、attachments、raw |

微信由 `poll_once` 直接构造 Envelope；其他渠道通过
`POST /v1/channels/inbound` 进入。Router 处理时用 contextvar 记录当前
endpoint 和 principal：

```text
handle_inbound(envelope)
  → CURRENT_ENDPOINT = envelope.endpoint_id
  → CURRENT_PRINCIPAL = envelope.principal_id
  → 回复 / 提醒 / 审批默认回到来源会话
```

非微信渠道的回复写入 `channel_outbound`，由适配器租约式拉取并 ack。租约机制保证
适配器崩溃后消息能重新投递，而不是丢失。

站主与访客的边界：

- 只有站主能回复审批通知、触发本机 Agent
- 访客默认只能聊天，不能调用工具
- `channels.<id>.allow_guest_tools` / `allow_guest_agents` 可显式放开

详见 [多渠道接入](channels.md)。

## 入站优先级

```text
1. 力扣登录口令
2. 待回复通知匹配（编号 / 引用 / 唯一 pending）
3. @agent_id 显式路由
4. 附件 / LLM 对话
```

### 待回复匹配

- 文本里出现 4 位编号（字母表 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`）
- 引用消息里出现编号
- 引用消息正文与某条 pending 正文唯一匹配
- 只有 1 条 pending：直接匹配
- 多条 pending + “好了/已登录/完成/ok”：取 10 分钟内最新
- 否则列出候选并返回 `ASKED`

## LLM 工具循环

```python
for round in range(6):
    message = llm.complete(messages, tools)
    if no tool_calls:
        return content
    for call in tool_calls:
        signature = name + args
        if signature seen:
            return "重复调用，停止"
        result = tools.execute(name, args)
        messages.append(tool result)
# 6 轮后强制无工具总结
```

- 工具结果截断 16000 字符
- 工具异常转为 `{"error": ...}`
- 重复调用检测按 `name + sorted(args)` 去重

## Agent Broker

```python
enqueue(agent_id, payload, wait_seconds) -> (result, task_id, done)
take(agent_id) -> task | None
complete(task_id, ok, text) -> bool
```

- 内存字典 + `threading.Event`
- 在线判定：`now - last_seen < 120s`
- 任务超时后从队列移除；Agent 完成时 `complete` 返回 False，结果通过 `/v1/notify` 补发

## 插件加载

```text
plugins/<id>/manifest.json
plugins/<id>/plugin.py
```

`PluginLoader` 校验：

- `id` 匹配 `^[a-z][a-z0-9_]{1,63}$`
- `api_version == "1"`
- 目录名 = id
- `entry` 在插件目录内
- 工具名只允许 `[A-Za-z0-9_-]`

`PluginContext` 提供：

- `tools` / `jobs`
- `config` / `root_config`
- `resource(name)`：受权限控制
- `send(text)`：需要 `send:wechat`
- `http_request()`：需要 `net:<host>`
- `secret(name)`：manifest 声明
- `provide(name, service)`：暴露服务
- `on_unload(fn)`

## 出站可靠性

- `send_text` 失败时写 `outbox`
- `maintenance_loop` 每 30 秒重试 10 条
- 401 只记日志，不重试
- 图片/文件通过 Hub multipart 发送

## 媒体

- 入站媒体：`media.encrypt_query_param` + `media.aes_key` → Hub `/api/v1/channels/media` 代理下载
- 图片：base64 data URI 给视觉模型
- 文件：保存到 `/workspace/inbox/<时间>-<随机>/`
- 文生图：SiliconFlow → `/workspace/generated/`
- 出站：Hub multipart，MIME 自动识别

## 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新工具 | `plugins/<id>/plugin.py` |
| 新定时任务 | `ctx.jobs.register(Job(...))` |
| 新 Agent 类型 | `call_agent` + `agents` 配置 |
| 新 LLM | `config.llm`，OpenAI 兼容即可 |
| 新文生图供应商 | `imagegen.py` |
| 新消息渠道 | `hub.py` + `app.poll_once` |
