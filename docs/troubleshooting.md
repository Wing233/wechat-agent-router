# 故障排查

## 先看这三个地方

```bash
# 1. 进程和健康
docker ps
curl -s http://127.0.0.1:9900/health | jq

# 2. Router 日志
docker logs --tail 200 wechat-agent-router

# 3. Hub 日志
docker logs --tail 200 openilink-hub
```

## 启动失败

### `配置未完成，请修改 config.json`

启动时会拒绝 `REPLACE` 占位符。检查：

```text
hub.channel_key
hub.owner_id
service_token
login_token_secret
credential_key
llm.api_key
agents[].api_key
```

### `credential_key` 报 Fernet 错误

重新生成：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

换 key 后，旧的力扣 Cookie 无法解密，需要重新登录。

### `ModuleNotFoundError`

```bash
pip install -r requirements.txt
# 或
docker compose build --no-cache
```

## Hub 相关

### 401 invalid key

- `hub.channel_key` 不对
- 频道被禁用
- key 复制时多了空格/换行

### 409 / 503

- 微信 24 小时窗口过期
- Bot 掉线
- 到 Hub 后台看 Bot 状态，在微信里回一条消息续期

### 收不到消息

- `hub.owner_id` 是否等于实际发送者
- Hub 频道是否启用
- `poll_interval` 是否太大
- `data/router.db` 的 `state.last_message_id` 是否卡住

### 媒体下载失败

- `media.encrypt_query_param` / `aes_key` 是否存在
- Hub 的 `/api/v1/channels/media` 是否可访问
- 文件是否超过 `max_image_mb` / `max_file_mb`

## Agent 相关

### Agent 一直离线

```bash
# 本机
curl http://<router>:9900/health

# 检查 local-agent 配置
python local-agent/agent.py --config local-agent/config.json
```

- `agent_id` 是否和 Router `agents[]` 一致
- `api_key` 是否一致
- 本机是否能出站访问 Router
- 代理/防火墙是否拦截

### 任务一直“已交给电脑”

- 本机 Agent 没在轮询
- `agent_wait_seconds` 太短
- 任务执行超过等待时间；完成后会通过 `/v1/notify` 补发

### 引擎找不到

```json
"engines": {
  "opencode": ["opencode", "run"]
}
```

- 命令必须在 PATH 里，或写绝对路径
- Windows 下确认 PowerShell 能执行
- 桌面端“引擎”页有“试跑”

### 工作区被拒绝

- 路径必须是绝对路径
- 必须在 `workspaces` 白名单里
- 目录必须存在

## LLM 相关

### 工具循环 / 重复调用

- 6 轮上限
- 重复调用检测
- 工具结果 16K 截断
- 换工具调用能力更强的模型

### 回答很慢

- 模型 reasoning 时间
- 工具多轮
- `agent_wait_seconds` 等待
- 网络到 LLM/Hub

### 图片看不懂

- 模型不支持 vision
- 图片超过 `max_image_mb`
- Hub 媒体下载失败

## 文生图

### `还没有配置生图 API`

- `image.enabled=true`
- `image.api_key` 非空
- `image.base_url` 正确

### HTTP 400 / 401

- Key 无效或余额不足
- 模型名不对（示例：`Kwai-Kolors/Kolors`）
- 尺寸不支持

### 生成成功但没发到微信

- `send_media` / Hub 发送失败
- 文件超过 32 MB
- Hub 窗口过期

## 文件与审批

### 审批码无效

- 通知过期（默认 15 分钟）
- 已经被回复过
- 编号打错（字母表去掉了 0/O/1/I）

### 写文件被拒绝

- 路径不在 `/app`、`/app/plugins`、`/workspace`
- 命中了禁止目录
- 文件超过 64 KB

## QQ / 多渠道

### `channel qq is not configured`

`config.json` 里没有 `channels.qq`，或键名和适配器发送的 `channel` 不一致。

### 入站 401

- 签名不是对**原始 body** 计算
- 适配器和 Router 的 `channels.qq.secret` 不一致
- 反向代理改写了 body

### 收不到 QQ 回复

```text
1. Router 日志是否显示 inbound accepted
2. GET /v1/channels/outbound 是否有 pending
3. 适配器是否在轮询、bot 是否在线
4. 发送成功后是否 ack
```

### 重复回复

- 多个适配器实例轮询同一个 `channel`
- 发送成功后没有 ack
- 租约 60 秒过期后重新投递

### 访客不能执行工具

这是默认行为：非 `owner_ids` 用户只能聊天。需要放开时配置
`allow_guest_tools` / `allow_guest_agents`，并确认群成员可信。

## 桌面端

### 打不开窗口

```powershell
python desktop\main.py --browser
python desktop\main.py --headless
```

- 没装 WebView2：用 `--browser` 或安装 WebView2 Runtime
- 端口 8765 被占用：会自动换端口

### 连接服务器失败

- Router 地址是否带 `https://`
- 反向代理是否支持长连接/大 body
- `service_token` 是否正确

### 服务令牌丢了

在服务器仓库目录：

```bash
python -c "import json;print(json.load(open('config.json'))['service_token'])"
```

不要把它发到公开渠道。

## 数据库

### `database is locked`

- 不要多实例写同一个 SQLite
- 检查是否有长事务
- WAL 文件是否可写

### 清理旧数据

```sql
-- 先备份！
DELETE FROM notifications WHERE status IN ('expired','cancelled') AND created_at < datetime('now','-30 days');
DELETE FROM messages WHERE created_at < datetime('now','-90 days');
VACUUM;
```

## 还是解决不了

开 Issue 时请附：

- 版本 / commit
- 部署方式（Docker / 原生）
- 脱敏后的日志
- 复现步骤
- 期望结果 / 实际结果

**不要**附 API Key、Cookie、完整 `config.json`、真实服务器地址。
