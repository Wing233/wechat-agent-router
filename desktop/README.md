# 微桥桌面控制台

桌面控制台是本机 Agent 的可视化壳，同时可以管理远程 Router。

## 功能

- 本机 Agent：Agent ID、Agent Key、Router 地址、默认引擎/工作区、启停、取消任务
- 引擎：OpenCode / Codex / Claude Code / 任意命令行程序，支持试跑
- 工作区：路径白名单、目录选择、打开目录
- 服务器：Agent 注册、插件启停/重载、定时任务、脱敏配置、DeepSeek 余额
- 日志：轮询、任务执行、配置修改
- 开机自启：无窗口后台运行

## 源码运行

```powershell
cd <repo>
python -m pip install -r desktop\requirements.txt
python desktop\main.py
```

也可以双击 `desktop\run.bat`。

首次启动会：

1. 读取 `local-agent/config.json`；不存在则生成一份空配置
2. 启动本地 HTTP 服务（只监听 `127.0.0.1`，端口自动分配）
3. 打开 WebView2 窗口；没有 WebView2 时回退 Edge 应用窗口/默认浏览器
4. 如果配置了 Router，自动启动本机 Agent

无窗口后台运行：

```powershell
python desktop\main.py --headless
```

再次双击 exe/主程序时，会检测已运行的实例并直接打开它的界面，不会重复启动 Agent。

## 首次配置

1. 打开「本机 Agent」，填写：
   - Agent ID：例如 `pc-home`、`pc-office`，必须唯一
   - Router 地址：例如 `https://router.example.com`
   - Agent Key：和 Router `config.json` 里 `agents[].api_key` 一致
   - 默认引擎 / 默认工作区
2. 点「保存并应用」。
3. 打开「服务器」，填写服务令牌并连接。

服务令牌在服务器仓库目录读取：

```bash
python -c "import json;print(json.load(open('config.json'))['service_token'])"
```

不要把它写进仓库、截图或公开聊天。

4. 连接成功后点「Agents」→「注册本机」。
5. 微信里发：

```text
用 pc-home 的 codex 在 scratch 工作区只回复 PONG，不要修改任何文件
```

## 多台电脑

每台电脑单独运行一份桌面端：

- `agent_id` 必须不同：`pc-home` / `pc-office` / `pc-laptop`
- `agent_key` 必须不同：每台点「生成」即可
- 每台电脑自己配置引擎和工作区；服务器只传别名
- 连接同一个 Router，各自点「注册本机」
- 微信里按描述或 `@id` 区分 Agent

不要把 `local-agent/config.json` 或 `desktop/.runtime/` 提交到 git。

## 打包 exe

```powershell
cd <repo>
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

输出：

```text
desktop\dist\WeChatAgentDesktop.exe
```

## 安全边界

- 本地 API 只监听 `127.0.0.1`
- 每次启动生成随机 `X-Desktop-Token`
- 服务令牌用 Windows DPAPI 加密保存
- 本机 Agent 只做出站轮询，不开放端口
- 本地 CLI 只能在配置的工作区里执行
- 日志和配置目录已在 `.gitignore` 中排除

## 文件说明

| 文件 | 用途 |
|---|---|
| `main.py` | 入口，选择 WebView/Edge/浏览器/headless |
| `app.py` | 应用状态、Agent 生命周期、配置、服务器代理 |
| `api_server.py` | 本地 HTTP API 和静态文件 |
| `window.py` | PyWebView 窗口 |
| `instance.py` | 单实例锁和本地 token |
| `secret_store.py` | Windows DPAPI |
| `store.py` | 桌面端设置 |
| `web/` | 原生 HTML/CSS/JS 界面 |
| `build.ps1` | PyInstaller 打包脚本 |

## 故障排查

| 问题 | 处理 |
|---|---|
| 打不开窗口 | 安装 WebView2 Runtime，或用 `--browser` / `--headless` |
| 端口被占用 | 自动换端口；看日志里的实际地址 |
| 连接服务器失败 | 检查 Router 地址、TLS、`service_token` |
| Agent 离线 | 检查 `agent_id`、`api_key`、本机到 Router 的网络 |
| 引擎找不到 | 在「引擎」页试跑，或写绝对路径 |
