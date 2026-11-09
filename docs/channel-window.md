# 微信 24 小时窗口与通知队列

## 规则

iLink / 微信 Bot 的主动消息依赖 Context Token。Token 只在用户给 Bot 发消息时下发，有效期 24 小时；Bot 自己发消息不会续期。因此：

- 用户最后一条消息后 24 小时内：可以主动发送
- 超过 24 小时：不能直接发送，只能等用户再次发消息
- OpeniLink Hub 的“自动续期”续的是 Bot 登录会话，不是每个用户的 Context Token

## 实现

### 窗口记录

`channel_windows` 表按 `(channel_id, chat_id)` 保存：

- `last_inbound_at`：最后一次用户入站时间
- `last_checkin_at`：最后一次签到提醒时间
- `platform` / `user_id` / `updated_at`

`/channel/inbound` 与 Hub 轮询收到用户消息时都会刷新窗口，并把该会话 `status='window'` 的待发消息释放为 `pending`。

### 主动发送

`send_to_channel` 对 `wechat` / `weixin` / `hub`：

- 窗口有效：直接经 Hub 发送
- 窗口过期或从未收到入站：写入 `channel_outbox`，状态 `window`，不直接发送
- 返回 `window_expired: true` 与排队 ID
- 用户下次发消息后，`flush_wechat_window()` 自动补发

参数：

```json
{
  "channel": "wechat",
  "text": "通知内容",
  "fallback": ["koishi", "email"],
  "must_deliver": true
}
```

`must_deliver=true` 且未显式给 `fallback` 时，默认尝试 `koishi` 与 `email`。

### 23 小时签到

后台 `window_loop` 每 10 分钟检查一次：

- 窗口仍在 24 小时内
- 距最后入站已超过 23 小时
- 本轮尚未发过签到

满足时发送“回复「1」继续接收通知。”。用户回复后窗口重新计时；不回复则窗口到期，通知继续排队。

### 备用通道

- `koishi`：写入对应渠道的 `channel_outbox`，由桥接端轮询发送（QQ/Telegram 等）
- `email`：读取 `channel_window.email` 配置，用 SMTP 直接发送
- 未配置或发送失败会在返回的 `fallback` 列表中给出原因

邮件配置示例见 `config.example.json` 的 `channel_window.email`。

## 边界

- 定时任务不能凭空续期 Context Token；没有用户回复就无法保证微信送达
- 签到提醒本身必须在 24 小时内发送，否则同样会被拒绝
- 不建议伪造用户消息刷新 Token
- 必须送达的通知应配置 Telegram/QQ/邮件等不受微信窗口限制的通道
