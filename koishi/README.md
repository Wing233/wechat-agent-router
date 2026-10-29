# Koishi 渠道网关

这里只做**渠道适配**：QQ、Telegram、Discord、飞书等由 Koishi 接入，统一转发给 Router。Router 仍然负责 LLM、工具、Agent、审批和工作区。

## 角色

```text
QQ / Telegram / Discord / ...
        ↓
   Koishi（本目录）
        ↓ POST /channel/inbound
   Router（/opt/wechat-agent-router）
        ↓ reply
   Koishi → 原渠道
```

- 微信个人号继续走 OpeniLink Hub，不经过 Koishi。
- Koishi 控制台负责平台账号、二维码、Token、连接状态。
- Router 桌面端「渠道」页负责绑定主人、允许列表、@ 规则和渠道状态。

## 启动

```bash
cd /opt/wechat-agent-router/koishi
npm install --omit=dev
cp .env.example .env
cp koishi.example.yml koishi.yml
# 编辑 .env，填入 Router 的 channels.koishi.token
systemctl daemon-reload
systemctl enable --now wechat-koishi
systemctl status wechat-koishi
```

日志：

```bash
journalctl -u wechat-koishi -f
```

## 打开控制台

Koishi 控制台只监听服务器 `127.0.0.1:5140`。从本机开 SSH 隧道：

```powershell
ssh -N -L 5140:127.0.0.1:5140 root@123.57.153.49
```

然后浏览器打开：

```text
http://127.0.0.1:5140
```

在控制台里：

1. 进入「插件配置」→ `adapter-onebot` 或 `adapter-telegram`
2. 填入 NapCat/OneBot 或 Telegram Bot 的连接信息
3. 启用插件
4. 在「沙盒」里先测试一条消息

## 绑定主人

Router 默认不给任何 QQ/Telegram 用户权限。首次使用：

1. 打开桌面端「渠道」页，查看绑定码
2. 在 QQ/Telegram 私聊机器人发送：

```text
绑定 ABC123
```

3. 绑定成功后，该用户成为 owner
4. 其他用户需要加入允许列表，或打开 `allow_all`（不建议）

发送 `/id` 可以查看自己的 `platform:user_id` 和 `chat_id`。

## 配置来源

`koishi.yml` 里的 `router-bridge` 配置：

```yaml
router-bridge:
  routerUrl: http://127.0.0.1:9900
  token: "${{ env.ROUTER_CHANNEL_TOKEN }}"
  channel: koishi
  timeoutMs: 90000
```

`ROUTER_CHANNEL_TOKEN` 来自 Router 的 `config.json`：

```bash
python3 -c "import json;print(json.load(open('/opt/wechat-agent-router/config.json'))['channels']['koishi']['token'])"
```

不要把这个 token 提交到 git，也不要放进前端。

## 已实现 / 未实现

已实现：

- 统一入站消息：平台、会话、用户、文本、@ 标记
- 同步回复：Router 的答案发回原渠道
- 绑定码、owner、允许列表、群聊 @ 限制
- 渠道状态和最近消息（桌面端「渠道」页）
- 长任务 60 秒内直接回原渠道

暂未实现：

- 超过 60 秒的 Agent 结果目前仍走微信通知；后续把 origin 带到 `/v1/notify` 即可回原渠道
- 图片/文件双向传输（Koishi 已收到，Router 暂只处理文本）
- 主动播报按渠道分发（定时任务目前仍发微信）

## 安全

- Koishi 控制台只绑定 `127.0.0.1`，通过 SSH 隧道访问
- 平台账号密码/Token 存在 Koishi 自己的数据库和 `koishi.yml`，两者都已 git 忽略
- Router 只保存一个渠道令牌，且 API 只返回掩码
- 未绑定或未授权的用户不能触发工具/Agent
- 不要把 5140 直接暴露到公网
