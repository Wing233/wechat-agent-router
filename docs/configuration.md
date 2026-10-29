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
    "skip_empty": false
  }
},
"plugins": {},
"disabled_plugins": ["evolution", "leetcode"]
```

- `jobs.<id>.interval_minutes` 可由 `admin__set_job_interval` 修改
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
