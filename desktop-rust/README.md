# WeChat Agent Desktop（Rust/Tauri 版）

这是统一桌面端的 Rust 实现：**Tauri 2 + 现有 `../desktop/web` + 本机 loopback HTTP API + 本机工作日志/技术栈采集**。

## 已集成

- 本机 Agent 配置/轮询/引擎试跑（移植 `local-agent/agent_core.py` 的核心流程）
- 工作日志采集：前台窗口采样 + Windows 本机 OCR + Git/Codex/浏览器/文件证据 + 每日纯文字 note
- 技术栈周更：工程依赖、源码后缀、Codex/OpenCode/WorkBuddy 会话文本扫描，生成 `latest.json`/`latest.html`
- 本地 HTTP API：`127.0.0.1` 随机端口 + `X-Desktop-Token`，复用现有 Web UI
- 服务端代理：`X-Service-Token` 转调 Router `/internal/settings/*`
- 本机目录选择（`/api/dialog/folder`）、打开目录/文件（`/api/open-path`）
- autostart 快捷方式（Windows Startup `.lnk`，复用 PowerShell COM）
- 保留 Python 版作为回退：`agent-router/desktop/` 不变

## 未完成

- updater 已实现 GitHub Releases 检查、下载、SHA-256 校验、替换 exe 与失败回滚；发布时必须同时上传 `WeChatAgentDesktop.exe.sha256`，缺少校验值的版本会被拒绝安装
- Tauri 窗口目前通过 loopback URL 打开 Web UI；如需关闭本地 HTTP，可把现有 `api()` 改为 Tauri invoke

## 发布

1. 确认 `Cargo.toml`、`Cargo.lock`、`tauri.conf.json` 与 `src/paths.rs` 的版本一致；
2. 从该提交构建 `npx --yes @tauri-apps/cli@2 build`；
3. 发布 `target/release/WeChatAgentDesktop.exe`、同名 `.sha256`、NSIS 安装包及其 `.sha256`；
4. 打与版本一致的源码标签，并开启 GitHub immutable releases，避免发布后替换资产。

1.3.7 发布记录：标签 `v1.3.7`（提交 `a8bfa67`），exe SHA-256 `7020382886e30d21d12df4b0c2ed9bff22fb6e271370faf65ee103554bc0f213`，release 为 immutable。

1.3.8 发布记录：标签 `v1.3.8`（提交 `7646bf2`），exe SHA-256 `a681b4583cfe27c1162d393de2323e58e78e3ecb4d4115e85fb5c92bf98af089`，release 为 immutable。修复 `apply()` 持锁调用 `set_state` 的死锁；运行中 1.3.6 应用手动替换 exe 后已启动 1.3.8，本地控制台恢复响应。

## 运行

```powershell
cd D:\xx\wechatbot\agent-router\desktop-rust
cargo run --release -- --headless
cargo run --release
```

数据目录：`WECHAT_AGENT_DATA` / `%LOCALAPPDATA%\WeChatAgentDesktop`。

- 工作日志：`worklog\`（`worklog.sqlite3`、`config.json`、`notes\`）
- 技术栈：`tech-stack\`（`reports\latest.json`、`reports\latest.html`）
- 旧的 `collector\queue.sqlite3` 截图队列不再写入；首次启动会把旧工作日志数据迁移到 `worklog\`

## 架构

```text
Tauri WebView
  └── http://127.0.0.1:<port>/#t=<token>
        ├── 静态：../desktop/web
        ├── API：/api/bootstrap、/api/config、/api/agent/*、/api/worklog*、/api/tech-stack*、/api/server/*
        ├── LocalAgent：Router 轮询 + CLI 引擎
        ├── WorklogService：采样 + 本机 OCR + 证据 + 22:00 note 上传
        └── TechStackService：每周本机技术栈扫描 + 报告
```
