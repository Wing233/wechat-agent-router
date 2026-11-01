# Changelog

## 1.2.0 — 2026-10-31

多渠道消息层。

- 新增 `channels.py`：`Endpoint`、`Principal`、`Envelope`
- 新增 `POST /v1/channels/inbound`（HMAC 签名）
- 新增 `/v1/channels/outbound` 租约式出站与 ack
- 微信仍走内置 poll，行为不变；回复、提醒默认回到来源会话
- 非站主默认只能聊天，不能调用工具或驱动本机 Agent
- 定时任务支持 `target_endpoint`
- 新增 `examples/koishi-bridge`（QQ）
- 新增 `docs/channels.md`

自然语言运维：
- 新增 `models.py` 模型目录与 `model_bindings` 角色绑定，支持免费模型优先、失败 fallback
- 新增 `spend.py` 成本账本与 `cost_report` / `budget_set`
- 新增 `traces.py` 请求级全链路追踪与 `trace_search` / `trace_get` / `trace_explain`
- 新增 `evals.py` 确定性评测、`eval_run` / `eval_report` / `eval_drift`
- 新增 `insights.py` 异常归因与 `insight_report` / `optimize_suggest` / `optimize_apply`
- 新增 `docs/capabilities.md`
- 新增 `show_help` / `help`：按当前入口、身份和已加载工具返回能力清单与示例；访客只读
- `config.json` 新增 `trace_db`、`eval_db`、`eval_cases`、`model_bindings`、`spend_db`
- 访客即使开启 `allow_guest_tools`，也会过滤模型、成本、链路、评测和 ops 等站主工具

## 1.1.1 — 2026-10-30

文档与分发更新，无破坏性代码改动。

- 新增英文 README
- 新增文件审批演示 GIF
- 新增 `examples/plugins/site` 可选站点数据插件
- 新增 GHCR Docker 自动构建
- 首页与文档补充 LYPH 相关项目链接

## 1.1.0 — 2026-10-30

首个公开版本。

### 核心

- FastAPI Router，SQLite WAL 状态层
- OpeniLink Hub 文本 / 图片 / 文件收发
- 文本、引用、审批码、4 位回复编号
- 6 轮 LLM 工具循环，重复调用检测，16K 工具结果
- 通知状态机 + outbox 重试
- 定时任务调度器，持久化下次运行时间

### 多模态

- 入站图片交给视觉模型
- 入站 txt / md / csv / json / pdf / docx 文本提取
- 入站视频/语音保存（暂不解析内容）
- 出站图片、视频、文件
- SiliconFlow Kolors 文生图

### Agent

- 出站轮询型本机 Agent（NAT 友好）
- HTTP 回调型 Agent + HMAC 签名
- 引擎：OpenCode / Codex / Claude Code / 任意命令
- 工作区别名 + 路径白名单
- 任务超时终止进程树

### 桌面端

- PyWebView 桌面控制台
- 本机 Agent 配置 / 启停 / 日志
- 引擎与工作区管理
- 远程 Router 管理：Agent、插件、任务、脱敏配置
- DPAPI 加密服务令牌
- PyInstaller 打包

### 插件

- `admin` 自省与配置
- `agents` Agent 路由
- `reminders` 一次性 / 间隔 / cron 提醒
- `todo` 待办清单
- `ops` 审批制文件操作
- `leetcode` 做题数据与 Cookie 校验（默认禁用）
- `deepseek_balance` 余额查询
- `evolution` 插件生成与安装（默认禁用）
- `demo_time` / `demo_weather` 示例

### 安全

- `service_token` / Agent Key / 登录签名 / Fernet 加密
- 文件路径沙箱
- 写/删/移动审批
- 配置脱敏
- 启动时拒绝 `REPLACE` 占位符
