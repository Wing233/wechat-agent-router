# 微信 Agent 桌面控制台

本目录是 `local-agent` 的桌面壳和可视化配置界面。它同时管理：

- 本机 Agent：身份、Agent Key、Router 地址、默认引擎/工作区、超时、启停、取消任务
- 本地引擎：OpenCode / Codex / Claude Code / 任意命令行程序，支持试跑
- 本地工作区：路径白名单、目录选择、打开目录
- 服务器：Agent 注册、插件启停/重载、定时任务、脱敏配置、联网搜索、DeepSeek 余额
- 渠道：QQ / Telegram / Discord 等经 Koishi 接入，统一身份映射、群聊 @、访客只读
- 工作日志：前台窗口、本机屏幕文字线索（OCR）、Git、Codex、浏览器分类、文件改动，每天 22:00 汇总当天并上传 LYPH，服务器按 22:30 cron 自动生成日报并 public 发布，无人工审核环节（如需撤回在后台手动撤稿）
- 本机日志：Agent 轮询、任务执行、配置修改

## 直接运行（源码）

```powershell
cd D:\xx\wechatbot\agent-router
python desktop\main.py
```

也可以双击 `desktop\run.bat`。

> 同一台电脑只运行一个实例：要么用 exe，要么用源码。两边各自使用独立的数据目录，同时运行会重复注册同一个 Agent ID 并争抢任务。exe 已在后台运行时，再次双击 exe 只会打开现有界面。

首次启动会：

1. 读取本机 `config.json`（源码模式是 `local-agent/config.json`，exe 模式在 exe 同目录）；不存在则生成一份空配置
2. 启动本地 HTTP 服务（只监听 `127.0.0.1`，端口自动分配）
3. 打开 WebView2 窗口；没有 WebView2 时回退 Edge 应用窗口/默认浏览器
4. 如果配置了 Router，自动启动本机 Agent

无窗口后台运行：

```powershell
python desktop\main.py --headless
```

再次双击 exe/主程序时，会检测已运行的实例并直接打开它的界面，不会重复启动 Agent。

## 工作日志（LYPH）

左侧「工作日志」页面已经集成采集器，不需要单独安装计划任务：

- 随桌面端启动/退出自动启停
- 每 30 秒采样前台应用和窗口标题；空闲超过 5 分钟跳过
- 每 30 秒本机截屏做 OCR：只保留脱敏文字，像素不出本机、不上传原图、不送云端 vision、不落盘、不留存
- 汇总 Git 提交、Codex 会话数量、浏览器分类、文件修改数
- 每天 22:00 生成当天的 `[worklog]` note（纯文字），上传到 LYPH；服务器按 22:30 cron 自动生成日报并 public 发布，无人工审核环节（如需撤回在后台手动撤稿）
- 支持「立即汇总当天」和「补全最近 30 天」
- 设备 token 用 Windows DPAPI 加密
- 不采集键盘、剪贴板、浏览器 URL；窗口标题脱敏

数据目录在 `desktop\.runtime\worklog`（源码）或 `data\worklog`（exe）。完整说明见 [docs/worklog.md](../docs/worklog.md)。

## 技术栈周更

桌面端「技术栈周更」使用本机后台线程按周采集，默认周日 10:00；首次启动没有报告时会在后台立即补采；如果桌面端关闭而错过预定运行时间，会在下次启动后补跑。请保持 WeChatAgentDesktop 运行，或在「设置」开启开机自启。采集由桌面端直接执行，不依赖 WorkBuddy 定时任务。

- 扫描配置工程根目录中的依赖清单，并在本地读取 Codex JSONL、OpenCode SQLite（只读）和 WorkBuddy 官方导出文件。
- 报告写入 exe 同目录 data/tech-stack/reports；只保留技术名、聚合计数、图标/说明和进度估算，不上传会话原文、源码或本机路径。
- WorkBuddy 会话需从官方界面导出后放入 data/tech-stack/workbuddy-exports；该目录可从「技术栈周更」页面打开。
- 报告只作为待审草稿，不自动发布到 Lyph；页面可立即采集、启停周计划并打开报告。

## 首次配置

1. 打开「本机 Agent」，填写：
   - Agent ID：例如 `pc-home`、`pc-office`，必须唯一
   - Router 地址：例如 `http://123.57.153.49:9900`
   - Agent Key：可以点「生成」，保存后同步到服务器
   - 默认引擎 / 默认工作区
2. 点「保存并应用」。
3. 打开「服务器」，填写服务令牌并连接：

```powershell
ssh root@123.57.153.49 --% "python3 -c 'import json;print(json.load(open(\"/opt/wechat-agent-router/config.json\"))[\"service_token\"])'"
```

bash / WSL 使用：

```bash
ssh root@123.57.153.49 "python3 -c \"import json;print(json.load(open('/opt/wechat-agent-router/config.json'))['service_token'])\""
```

4. 连接成功后点「Agents」→「注册本机」。
5. 微信里发：

```text
@pc-home 用 codex 在 scratch 工作区只回复 PONG，不要修改任何文件
```

`@pc-home` 是确定性路由，不经过 LLM；不带前缀时由 LLM 按 Agent 描述选择。

## QQ / Telegram 渠道（Koishi）

Router 不直接实现 QQ/Telegram 协议，渠道层由 `koishi/` 目录里的 Koishi 网关负责。

服务器上：

```bash
cd /opt/wechat-agent-router/koishi
npm install --omit=dev
# .env 里的 ROUTER_CHANNEL_TOKEN 来自 Router config.json 的 channels.koishi.token
systemctl enable --now wechat-koishi
```

配置平台账号：

1. 打开 `https://123.57.153.49/koishi/`
2. 输入 Basic Auth 用户名/密码（保存在服务器 `/root/koishi-console-password.txt`）
3. 首次访问会要求绑定 Authenticator；之后每次输入 6 位动态码
4. 「插件配置」→ 启用 `adapter-onebot` 或 `adapter-telegram`
5. 「沙盒」先测试

忘记 Authenticator 时，在桌面端「渠道」页点「重置 2FA」，再重新绑定。

也可以继续用 SSH 隧道作为备用：

```powershell
ssh -N -L 5140:127.0.0.1:5140 root@123.57.153.49
```

身份映射：

1. 在 QQ/Telegram 私聊机器人发送 `/id`
2. 把返回的 `platform:user_id` 填到桌面端「渠道 → 主人身份」
3. 保存后，该账号的私聊与微信共用同一个 owner 身份、历史、工具和 Agent 路由

群聊规则：

- 必须 @ 机器人才会处理
- 非 owner 只读：只能问公开数据，不能碰文件、配置和 Agent
- 群聊上下文与私聊隔离

当前行为：

- 文本消息同步回复到原渠道
- 60 秒内完成的 Agent 任务也会回到原渠道
- 超过 60 秒的 Agent 结果暂时仍走微信通知
- 图片/文件双向传输暂未接入 Router

详见 `koishi/README.md`。

## Agent 常用工具

Router 内置了 `websearch` 插件：

- `web_search`：联网搜索，默认豆包搜索 API，失败自动回退 Bing
- `web_fetch`：抓取公开网页正文，带 SSRF 防护
- `weather`：城市当前天气
- `calculator`：安全数学表达式计算

Owner 额外有 `devtools` 插件：

- `run_command`：在服务器容器内执行 shell（装依赖、跑测试、git、看日志）
- `http_request`：直接调用任意公开 HTTP API

Owner 的 `fs_write` / `fs_replace` / `fs_delete` / `fs_move` 默认直接生效，不再走微信审批；想恢复审批，在 `config.json` 里把 `plugins.ops.auto_approve` 设为 `false`。

访客只读身份可以使用搜索、抓取、天气、计算器、站点统计、力扣统计这些只读工具；`ops`、`devtools`、`admin`、`agents`、`reminders` 仍然只对 Owner 开放。

在服务器配置页「联网搜索」里可以切换豆包/Bing、设置默认结果数和豆包 Key。豆包 Key 只写不回显；Key 无效时会自动回退 Bing。

## 多台电脑

每台电脑单独运行一份桌面端：

- `agent_id` 必须不同：`pc-home` / `pc-office` / `pc-laptop`
- `agent_key` 必须不同：每台点「生成」即可
- 每台电脑自己配置引擎和工作区；服务器只传别名
- 连接同一个 Router，各自点「注册本机」
- 微信里用 `@pc-home` / `@pc-office` 显式路由；不带前缀时由 LLM 按描述选择

不要把 `local-agent/config.json` 或 `desktop/.runtime/` 提交到 git。

## 打包 exe

```powershell
cd D:\xx\wechatbot\agent-router
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

输出：

```text
desktop\dist\WeChatAgentDesktop.exe
```

建议把 exe 放在独立目录（例如 `D:\WeChatAgent\`）再运行。首次运行会在 exe 同目录创建：

```text
config.json        # 本机 Agent 配置（含 Agent Key，权限 600）
data\              # 日志、实例状态、加密后的服务令牌
```

如果 exe 所在目录不可写（例如 Program Files），会自动改用 `%LOCALAPPDATA%\WeChatAgentDesktop`。

## 开机自启

「设置」→「开机自动启动」会创建当前用户的 Startup 快捷方式，目标参数为 `--headless`。不会注册 Windows 服务，也不需要管理员权限。

关闭：

- 在界面里关掉开关，或删除
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WeChatAgentDesktop.lnk`

## 软件更新

「设置」→「关于」→「检查更新」会读取 GitHub Releases 的最新版本，与当前版本比较；有新版本时出现「下载并更新」：

- 更新源：`Wing233/wechat-agent-router` 的 latest release，资产名固定为 `WeChatAgentDesktop.exe`。
- 只比较 GitHub Releases 的版本号；如果本地版本比 latest release 还新（例如本地已构建 1.3.0、线上仍是 v1.2.1），会提示“暂无可更新版本”，不会降级。需要先发布一个更高的 release。
- 下载完成后，程序会退出，由同目录的临时脚本把旧 exe 备份为 `.WeChatAgentDesktop.exe.previous`，替换成新 exe 并重新启动。
- 重启前会清理 `_MEIPASS2` / `_MEIPASS` 并设置 `PYINSTALLER_RESET_ENVIRONMENT=1`，避免新进程复用已删除的 PyInstaller 临时目录而报 `Failed to load Python DLL ... python312.dll`。
- 只对打包后的 exe 生效；源码模式会提示手动下载。
- 如果安装目录不可写（例如 Program Files），下载会失败并提示；请把 exe 放到可写目录后再更新。

## 安全边界

- 本地 HTTP 只监听 `127.0.0.1`
- 每次启动生成随机 `X-Desktop-Token`；页面通过 URL fragment 获取，不进入服务器日志
- 校验 `Host`，拒绝非本机 Host（防 DNS rebinding）
- 服务令牌用 Windows DPAPI 加密保存在 `data/secrets.json`，API 只返回是否已配置
- Agent Key / Channel Key / API Key 在界面中始终掩码，写接口只写不回显
- 所有服务器管理接口要求 `X-Service-Token`
- 文件操作/服务器配置修改会写入插件审计事件

## 常用参数

```text
--config PATH     指定 local-agent/config.json
--headless        只启动后台服务，不打开窗口
--window auto     WebView2 → Edge → 浏览器（默认）
--window webview  强制 WebView2
--window edge     强制 Edge 应用窗口
--window browser  强制默认浏览器
```

## 故障排查

| 现象 | 处理 |
|---|---|
| 窗口打不开 | 确认已安装 Microsoft Edge WebView2 Runtime；或加 `--window edge` |
| 连不上 Router | 用「测试 Router」检查地址、端口、服务器安全组 |
| Agent 不在线 | 检查 Agent ID/Key 是否与服务器 `agents` 一致；看「日志」 |
| Codex 拒绝执行 | 工作区需要是 git 仓库或被 Codex 信任；可在工作区 `git init` |
| Claude 报订阅错误 | 本地 Claude Code 的账号订阅无效/过期，与桥无关 |
| 试跑没输出 | 看「日志」和试跑弹窗；确认 CLI 已安装并在 PATH |
| 修改配置后没生效 | 保存会重启本机 Agent；服务器配置保存后立即生效 |

## 开发

主要文件：

```text
desktop/main.py          入口、单实例、窗口模式
desktop/app.py           DesktopApp：Agent 生命周期 + 服务器代理
desktop/api_server.py    127.0.0.1 HTTP API + 静态资源
desktop/secret_store.py  Windows DPAPI
desktop/autostart.py     Startup 快捷方式
desktop/web/             无构建步骤的原生 HTML/CSS/JS
local-agent/agent_core.py  Agent 核心（CLI 和桌面端共用）
local-agent/agent.py      命令行入口
```

改 `desktop/web/` 后重启桌面进程即可（静态资源在启动时加载）。
