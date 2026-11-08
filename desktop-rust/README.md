# WeChat Agent Desktop（Rust/Tauri 版，MVP）

这是统一桌面端的 Rust 骨架：**Tauri 2 + 现有 `../desktop/web` + 本机 loopback HTTP API + `lyph-collector-core` 采集服务**。

## 已集成

- 本机 Agent 配置/轮询/引擎试跑（移植 `local-agent/agent_core.py` 的核心流程）
- LYPH 截图采集器复用 `lyph/collectors/core`（SQLite 队列、单上传、重试、心跳、吊销状态）
- 本地 HTTP API：`127.0.0.1` 随机端口 + `X-Desktop-Token`，复用现有 Web UI
- 服务端代理：`X-Service-Token` 转调 Router `/internal/settings/*`
- autostart 快捷方式（Windows Startup `.lnk`，复用 PowerShell COM）
- 保留 Python 版作为回退：`agent-router/desktop/` 不变

## 未完成

- DPAPI：Windows 上使用 `windows-dpapi` 兼容 Python 版 `dpapi:*` 密钥；非 Windows 仅支持 `plain:*`
- 工作日志 `finalize/backfill` 已接采集队列摘要；OCR 细节仍未移植，Python 版仍可回退
- updater 已实现 GitHub Releases 检查；下载替换 exe 仍未实现，tech-stack 采集主体仍是状态/文件兼容层
- Tauri 窗口目前通过 loopback URL 打开 Web UI；如需关闭本地 HTTP，可把现有 `api()` 改为 Tauri invoke

## 运行

```powershell
cd D:\xx\wechatbot\agent-router\desktop-rust
cargo run --release -- --headless
cargo run --release
```

数据目录复用 Rust 版 `WECHAT_AGENT_DATA` / `%LOCALAPPDATA%\WeChatAgentDesktop`，采集队列位于 `collector/queue.sqlite3`。

## 架构

```text
Tauri WebView
  └── http://127.0.0.1:<port>/#t=<token>
        ├── 静态：../desktop/web
        ├── API：/api/bootstrap、/api/config、/api/agent/*、/api/worklog*、/api/server/*
        ├── LocalAgent：Router 轮询 + CLI 引擎
        └── CollectorService：lyph-collector-core Engine（截图/速记/同步）
```
