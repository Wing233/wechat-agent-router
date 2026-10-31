# 配置

Router 只读 JSON 配置，默认路径 `/app/config.json`，可用 `ROUTER_CONFIG` 覆盖。

从示例开始：

```bash
cp config.example.json config.json
chmod 600 config.json
```

## 生成密钥

```bash
# Fernet（credential_key）
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 随机 token（service_token / login_token_secret）
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Agent Key
python -c "import secrets; print('pc_' + secrets.token_hex(20))"
```

启动时会拒绝空值或 `REPLACE...` 占位符，避免把示例配置直接上线。

## 字段

### hub

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `base_url` | string | ✅ | Hub 地址，如 `http://host.docker.internal:9800` |
| `channel_key` | string | ✅ | Hub 频道 key |
| `owner_id` | string | ✅ | 你的微信 owner id |

### channels

多渠道入口的共享密钥和权限。没有这段配置时只有微信可用。

```json
{
  "qq": {
    "adapter": "koishi",
    "secret": "随机长字符串",
    "owner_ids": ["你的 QQ 号"],
    "allow_guest_tools": false,
    "allow_guest_agents": false
  }
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `adapter` | — | 适配器名，仅作标识 |
| `secret` | — | 入站 HMAC / 出站轮询共享密钥 |
| `owner_ids` | `[]` | 该渠道中视为站主的用户 ID |
| `allow_guest_tools` | `false` | 是否允许非站主触发工具 |
| `allow_guest_agents` | `false` | 是否允许非站主驱动本机 Agent |

### endpoints

给常用会话起可读名字，不是白名单；未登记会话会自动发现。

```json
{
  "qq:group:123456": {
    "channel": "qq",
    "kind": "group",
    "label": "QQ 测试群",
    "conversation_id": "123456",
    "capabilities": ["text", "image", "file", "quote"]
  }
}
```

完整协议见 [多渠道接入](channels.md)。

### 根字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `db` | `/data/router.db` | SQLite 路径 |
| `data_dir` | `/data` | 图片目录 |
| `public_base_url` | — | 公网地址，用于登录链接 |
| `poll_interval` | `2` | Hub 拉取间隔（秒） |
| `initial_last_id` | `0` | 首次游标 |
| `credential_key` | — | Fernet key |
| `login_token_secret` | — | 登录 HMAC |
| `service_token` | — | 内部 API 令牌 |
| `agent_wait_seconds` | `60` | Agent 同步等待 |

### llm

```json
{
  "base_url": "https://api.deepseek.com",
  "api_key": "...",
  "model": "deepseek-chat",
  "system_prompt": "你叫微桥，说话简洁。"
}
```

- 任何 OpenAI 兼容 `/chat/completions` 均可
- 支持 tools 的模型才能使用完整工具链
- 支持 vision 的模型才能直接理解入站图片
- `system_prompt` 会拼在默认 system prompt 前面

### attachments

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否处理入站附件 |
| `dir` | `/workspace/inbox` | 保存目录 |
| `max_files` | `3` | 每条消息最多附件数 |
| `max_image_mb` | `4` | 单张图片上限 |
| `max_file_mb` | `10` | 单文件上限 |
| `text_chars` | `20000` | 提取文本上限 |

### image

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 是否启用文生图 |
| `base_url` | `https://api.siliconflow.cn/v1` | API 地址 |
| `api_key` | — | API Key |
| `model` | `Kwai-Kolors/Kolors` | 模型 |
| `size` | `1024x1024` | 默认尺寸 |
| `steps` | `20` | 推理步数 |
| `guidance` | `7.5` | CFG scale |
| `dir` | `/workspace/generated` | 保存目录 |

### agents

| 字段 | 说明 |
|---|---|
| `id` | 唯一 ID，如 `pc-home` |
| `name` | 显示名 |
| `description` | 给 LLM 看的能力描述 |
| `transport` | `poll` 或 `http` |
| `callback_url` | `http` 模式的回调地址 |
| `callback_secret` | `http` 模式 HMAC 密钥 |
| `api_key` | Agent 认证 key |

`poll` 模式不需要开放端口；`http` 模式适合有固定地址的服务。

### jobs / plugins

```json
"jobs": {
  "leetcode_check": {
    "plugin": "leetcode",
    "enabled": true,
    "interval_minutes": 240,
    "window_minutes": 0,
    "skip_empty": false,
    "target_endpoint": "qq:group:123456"
  }
},
"plugins": {},
"disabled_plugins": ["evolution", "leetcode"]
```

- `jobs.<id>.interval_minutes` 可由 `admin__set_job_interval` 修改
- `jobs.<id>.target_endpoint` 指定任务通知发到哪个会话；默认 `wechat:owner`
- `plugins.<id>` 是插件自己的配置
- `disabled_plugins` 在加载前过滤

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ROUTER_CONFIG` | `/app/config.json` | 配置文件路径 |
| `WECHAT_AGENT_CONFIG` | `local-agent/config.json` | 桌面端 Agent 配置 |
| `WECHAT_AGENT_DATA` | `desktop/.runtime` | 桌面端数据目录 |

## 配置修改

- 直接改 `config.json` 后重启容器
- 或通过桌面端“服务器 → 配置”修改（脱敏视图，密钥只写不回显）
- 或让 bot 调 `admin__config_set`（只允许 `poll_interval` 和 `jobs.*`）

> 不要把 `config.json` 提交到 git。`.gitignore` 和 `.dockerignore` 已默认排除。
