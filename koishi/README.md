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

需要 Node.js 18+（推荐 22）。服务器已安装 Node 22。

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

推荐直接使用 Nginx：

```text
https://123.57.153.49/koishi/
```

登录分两层：

1. **Basic Auth**：用户名 `koishi`，密码保存在服务器 `/root/koishi-console-password.txt`
2. **Authenticator**：首次访问会进入绑定页，用 Google Authenticator / Microsoft Authenticator / Authy 手动输入密钥并验证一次；之后每次登录输入 6 位动态码

如果忘记 Authenticator，可在桌面端「渠道」页点「重置 2FA」，再重新绑定。

Koishi 本身只绑定 Docker 网关地址，不直接暴露 5140。需要备用通道时，仍可开 SSH 隧道：

```powershell
ssh -N -L 5140:127.0.0.1:5140 root@123.57.153.49
```

然后浏览器打开 `http://127.0.0.1:5140/koishi/`。

在控制台里：

1. 进入「插件配置」→ `adapter-onebot` 或 `adapter-telegram`
2. 填入 NapCat/OneBot 或 Telegram Bot 的连接信息
3. 启用插件
4. 在「沙盒」里先测试一条消息

## 身份映射

Router 默认把所有 QQ/Telegram 用户当作**只读访客**。要获得和微信主人相同的权限：

1. 在 QQ/Telegram 私聊机器人发送：

```text
/id
```

2. 复制返回的 `platform:user_id`，例如 `qq:123456789`
3. 打开桌面端「渠道 → 主人身份」，把它填进 Owner IDs 并保存
4. 之后该账号的私聊与微信共用同一个 owner 身份、历史、工具和 Agent 路由

群聊：

- 必须 @ 机器人才会处理
- 非 owner 只读：只能问公开数据，不能执行文件操作、配置修改或 Agent 任务
- 群聊上下文与私聊隔离

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
- 全局 owner 身份映射、群聊必须 @、非 owner 只读
- 渠道状态和最近消息（桌面端「渠道」页）
- 长任务 60 秒内直接回原渠道

暂未实现：

- 超过 60 秒的 Agent 结果目前仍走微信通知；后续把 origin 带到 `/v1/notify` 即可回原渠道
- 图片/文件双向传输（Koishi 已收到，Router 暂只处理文本）
- 主动播报按渠道分发（定时任务目前仍发微信）

## 安全

- Koishi 控制台通过 Nginx `https://123.57.153.49/koishi/` 访问，使用 Basic Auth + Authenticator 二次验证；也保留 SSH 隧道作为备用
- `/koishi-auth/*` 需要 Nginx 注入的内部网关令牌，直接访问 Router 9900 会返回 404
- 平台账号密码/Token 存在 Koishi 自己的数据库和 `koishi.yml`，两者都已 git 忽略
- Router 只保存一个渠道令牌，且 API 只返回掩码
- 非 owner 用户是只读访客，不能触发工具/Agent
- 不要把 5140 直接暴露到公网
