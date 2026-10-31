# 多渠道接入

Router 核心不假设消息来自微信。每个入口先把消息归一化成 **Envelope**，Router 再按
**Endpoint** 路由。微信是内置入口，QQ（Koishi）通过桥接插件接入，以后加 Discord、
Telegram 或自建 Web 表单不需要改 Router 核心。

```text
WeChat ─┐
QQ ─────┼──> Channel Adapter ──> Envelope ──> Router ──> Outbox ──> Adapter
Future ─┘                          │
                                   ├─ endpoint_id   从哪个会话来
                                   ├─ principal_id  谁发的
                                   └─ message_id    哪条消息
```

## 核心对象

| 对象 | 说明 | 示例 |
|---|---|---|
| `channel` | 入口类型 | `wechat`、`qq` |
| `endpoint_id` | 具体会话，结果默认回到这里 | `wechat:owner`、`qq:group:123456`、`qq:private:789` |
| `conversation_type` | 会话形态 | `private`、`group` |
| `principal_id` | 发起人；站主统一为 `owner` | `owner`、`guest:qq:789` |
| `message_id` | 渠道原始消息 ID，用于引用和排查 | `...` |
| `attachments` | 图片/文件描述 | `[{ "type": "image", "url": "..." }]` |

## 配置

在 `config.json` 增加 `channels` 和可选的 `endpoints`：

```json
{
  "channels": {
    "qq": {
      "adapter": "koishi",
      "secret": "随机长字符串",
      "owner_ids": ["你的 QQ 号"],
      "allow_guest_tools": false,
      "allow_guest_agents": false
    }
  },
  "endpoints": {
    "qq:group:123456": {
      "channel": "qq",
      "kind": "group",
      "label": "QQ 测试群",
      "conversation_id": "123456",
      "capabilities": ["text", "image", "file", "quote"]
    }
  }
}
```

- `secret`：入站 HMAC 签名和出站轮询的共享密钥。**不要复用 Hub 的 channel key。**
- `owner_ids`：该渠道中视为站主的用户 ID。微信的 `hub.owner_id` 仍然自动算站主。
- `allow_guest_tools`：是否允许非站主触发工具。默认 `false`，访客只能聊天。
- `allow_guest_agents`：是否允许非站主把任务转给本机 Agent。默认 `false`。

`endpoints` 只是给常用会话一个可读标签，不是白名单。适配器第一次上报某个会话时会
自动登记为 discovered endpoint，因此不需要提前写配置。

## 入站 API

```http
POST /v1/channels/inbound
Content-Type: application/json
X-Router-Signature: <hex HMAC-SHA256(secret, raw_body)>
```

```json
{
  "channel": "qq",
  "adapter": "koishi",
  "account_id": "qq:10001",
  "endpoint_id": "qq:group:123456",
  "conversation_type": "group",
  "conversation_id": "123456",
  "message_id": "abc",
  "sender_id": "789",
  "sender_name": "小明",
  "text": "帮我看下今天的成本",
  "attachments": [],
  "mentions": [],
  "raw": {}
}
```

响应：

```json
{
  "ok": true,
  "accepted": true,
  "endpoint_id": "qq:group:123456",
  "principal_id": "guest:qq:789"
}
```

Router 返回 `202` 语义的“已接收”后异步处理，LLM 调用不会阻塞适配器。

签名计算：

```python
hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
```

```js
crypto.createHmac("sha256", secret).update(rawBody).digest("hex")
```

## 出站 API

适配器轮询自己渠道的待发送消息：

```http
GET /v1/channels/outbound?channel=qq&limit=20
X-Channel-Secret: <channels.qq.secret>
```

```json
{
  "channel": "qq",
  "items": [
    {
      "id": 12,
      "endpoint_id": "qq:group:123456",
      "channel": "qq",
      "payload": { "kind": "text", "text": "..." },
      "tries": 0
    }
  ]
}
```

发送成功后确认：

```http
POST /v1/channels/outbound/ack
X-Channel-Secret: <secret>
Content-Type: application/json

{ "channel": "qq", "ids": [12] }
```

租约 60 秒：适配器崩溃或发送失败时不 ack，消息会在租约过期后重新投递。

## Koishi 桥接

`examples/koishi-bridge` 是一个可直接放进 Koishi 的插件：

```text
examples/koishi-bridge/
├── index.js
├── package.json
└── README.md
```

最小配置：

```yaml
plugins:
  ./plugins/router-bridge:
    routerUrl: http://127.0.0.1:9900
    channel: qq
    secret: 与 channels.qq.secret 相同
    platform: qq
    requireAt: true
```

行为：

- 群消息默认只在被 @ 时处理，`requireAt: false` 可关闭。
- 私聊直接处理。
- 自动过滤自己发送的消息和重复事件。
- 文字、图片、文件会归一化后发给 Router；附件只传 URL，不下载。
- Router 的回复由轮询器取回，再通过 Koishi bot 发回原群/私聊。

## 来源、任务与目标

每条消息处理时，Router 会设置两个上下文变量：

```text
CURRENT_ENDPOINT   = 消息来自哪个 endpoint
CURRENT_PRINCIPAL  = 谁发的
```

这样回复、提醒、审批码默认回到来源会话；定时任务可以在 `jobs.<id>.target_endpoint`
指定固定目标：

```json
{
  "jobs": {
    "leetcode_check": {
      "interval_minutes": 360,
      "target_endpoint": "qq:group:123456"
    }
  }
}
```

默认目标仍是 `wechat:owner`，因此升级不会改变现有微信行为。

## 安全边界

- 入站必须带正确 HMAC，否则 `401`。
- 出站轮询和 ack 必须带 `X-Channel-Secret`。
- 审批码、登录通知只发给站主；群成员猜编号不会命中站主的待回复通知。
- 非站主默认不能调用工具、不能驱动本机 Agent。
- 跨渠道转发属于高风险操作，后续会走审批码。

## 当前限制

- 出站附件目前只在微信侧直接发送；QQ 侧收到的是文字说明。
- QQ 的文件/图片 URL 需要适配器可访问，Router 暂不代理下载。
- `principal` 目前按渠道 ID 归属；跨渠道身份合并（微信 owner ↔ QQ owner）下一步再做。
