# 安全模型

微桥的默认假设是：**单用户、自托管、内网或受控公网入口**。它不是多租户平台。

## 资产

| 资产 | 风险 |
|---|---|
| `config.json` | 含 Hub key、LLM key、service token、Fernet key |
| `data/router.db` | 通知、对话、凭据密文 |
| `workspace/` | 用户文件、生成图片 |
| 本机 Agent 配置 | Agent key、工作区路径 |
| 桌面端 `secrets.json` | DPAPI 加密的 service token |

## 信任边界

```text
微信用户（owner）
   │
   ▼
OpeniLink Hub  ──  不可信输入：文本、图片、文件、引用
   │
   ▼
Router  ──  认证、路由、工具、审批
   │
   ├── 插件（受权限约束）
   ├── 本机 Agent（出站轮询、工作区白名单）
   └── 外部 API（LLM、文生图）
```

## 认证

| 入口 | 令牌 | 说明 |
|---|---|---|
| Agent 通知 | Agent API Key | 每个 Agent 独立 |
| 本机 Agent | Agent Key | 与 agents[] 对应 |
| 内部 API | `service_token` | 桌面端、插件、管理 |
| 登录链接 | HMAC + `login_token_secret` | TTL 1800 秒 |
| 微信 owner | `hub.owner_id` | 只处理 owner 消息 |

启动时拒绝空值/`REPLACE`，避免示例配置直接上线。

## 审批

写文件、删除、移动、安装插件都走通知审批：

```text
Agent → /v1/notify (expect_reply=true)
Router → 微信：【ops】回复编号：ABCD ...
用户 → 回复 ABCD 同意
Router → 执行 → 记录 plugin_events
```

- 4 位码，字母表去掉易混淆字符
- TTL 默认 900 秒
- 只有包含 `approve/同意/执行/通过` 才执行
- 审批码只能用一次

## 文件沙箱

`ops` 插件：

- 允许根：`/app`、`/app/plugins`、`/workspace`
- 禁止：`.git`、`.env`、`config.json`、`data`、`node_modules`、`__pycache__`
- 单文件 64 KB
- `fs_replace` 要求 old 文本恰好出现一次
- 只能删文件或空目录
- 路径 `resolve()` 后检查，防 `..` 和 symlink 逃逸

## 本机 Agent

- 只出站轮询，不监听端口
- 工作区由本机配置决定，Router 只能传别名
- 引擎命令由本机配置决定，Router 不能传任意命令
- 超时终止进程树
- 输出截断后回传

## SSRF

- Hub 媒体下载只允许 `/api/v1/channels/media` 路径
- 插件 `ctx.http_request` 需要 `net:<hostname>` 权限
- 文生图只访问配置的 `base_url`
- LLM 只访问配置的 `base_url`

## 密钥存储

| 密钥 | 存储 |
|---|---|
| Router 配置 | `config.json`（600，gitignore） |
| 桌面端 service token | Windows DPAPI |
| 力扣 Cookie | Fernet(`credential_key`) 加密进 SQLite |
| Agent Key | `config.json` + `local-agent/config.json`（600） |

## 已知边界

- Router 容器默认以 root 运行（为了 `plugins/` 可写和 `evolution` 安装）；生产可改用非 root + 只挂载必要目录
- `evolution` 能生成并安装代码，默认禁用
- `leetcode` 使用 Cookie 自动化，默认禁用；使用前确认平台 ToS
- 没有多用户隔离；`owner_id` 是唯一用户
- 没有内置速率限制；公网部署请放在反向代理后
- 模型可能被 prompt injection 影响；工具权限是最后一道边界

## 生产加固

```yaml
services:
  router:
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    volumes:
      - ./data:/data
      - ./workspace:/workspace
      - ./plugins:/app/plugins
      - ./config.json:/app/config.json
```

> `read_only` 需要把可写目录都挂成 volume；`evolution` 需要 `/app` 可写，二者按需取舍。

## 事件响应

如果怀疑密钥泄露：

1. 停 Router / 本机 Agent
2. 轮换 `service_token`、`login_token_secret`、所有 Agent Key
3. 轮换 Hub Channel Key、LLM Key、文生图 Key
4. 检查 `plugin_events`、`routing_log`、Hub 消息记录
5. 重新绑定力扣 Cookie
6. 检查 `workspace/` 是否有异常文件
