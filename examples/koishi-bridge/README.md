# Koishi Router Bridge

把 QQ（Koishi）接进 WeChat Agent Router 的最小桥接插件。

## 它做什么

```text
QQ 消息
  → Koishi session
  → 归一化 Envelope
  → POST /v1/channels/inbound（HMAC）
  → Router

Router 回复
  → GET /v1/channels/outbound（租约）
  → Koishi bot.sendMessage / sendPrivateMessage
  → POST /v1/channels/outbound/ack
```

Router 不依赖 Koishi 的数据库，也不需要公网地址：出站采用轮询，和本机 Agent 的模式一致。

## 安装

1. 把整个目录复制到 Koishi 的 `plugins/router-bridge`。
2. 在 Router 的 `config.json` 里配置渠道密钥：

```json
{
  "channels": {
    "qq": {
      "adapter": "koishi",
      "secret": "换成随机长字符串",
      "owner_ids": ["你的 QQ 号"],
      "allow_guest_tools": false,
      "allow_guest_agents": false
    }
  }
}
```

3. 在 `koishi.yml` 中加载：

```yaml
plugins:
  ./plugins/router-bridge:
    routerUrl: http://127.0.0.1:9900
    channel: qq
    secret: 换成和上面一致的值
    platform: qq
    requireAt: true
```

4. 重启 Koishi，在 QQ 群里 @ 机器人 发消息。

## 配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `routerUrl` | `http://127.0.0.1:9900` | Router 地址 |
| `channel` | `qq` | 渠道名，必须与 Router `channels` 的键一致 |
| `secret` | 空 | 共享密钥；为空时插件不启动 |
| `platform` | `qq` | 过滤 Koishi bot 的平台 |
| `pollInterval` | `2000` | 出站轮询间隔（毫秒） |
| `requireAt` | `true` | 群里是否必须 @ 机器人才处理 |
| `groupAllowlist` | `[]` | 只处理这些群；空表示全部 |
| `accountId` | 空 | 指定 `platform:selfId`，多账号时使用 |
| `textLimit` | `4000` | 单条文本上限 |

## 行为说明

- 自动忽略机器人自己发的消息。
- 同一 `messageId` 在 60 秒内只提交一次，避免 Koishi 重连导致重复。
- 文本使用 `session.stripped.content`，会去掉 @ 元素。
- 图片和文件只把 URL 传给 Router，不下载。
- Router 侧出站附件如果是服务器本地路径，QQ 侧只能收到文字说明。

## 排查

```text
Router 返回 401  → secret 不一致，或签名不是对原始 body 计算
Router 返回 503  → config.json 里没有 channels.qq
收不到回复       → 检查 Router 日志和 /v1/channels/outbound 是否有 pending
重复回复         → 确认 ack 请求成功，且没有多个 Koishi 实例轮询同一个渠道
```

Router 侧日志：

```bash
docker compose logs -f router
```
