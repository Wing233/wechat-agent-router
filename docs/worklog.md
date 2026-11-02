# 工作电脑活动采集与 LYPH 日报

这套能力把 Windows 工作电脑的活动汇总成 LYPH 日志：

```text
Windows 前台窗口采样
  + Git 提交
  + Codex 会话（只取 cwd/数量，不取对话内容）
  + 浏览器分类（只取分类/次数，不取 URL/标题）
  + 文件修改数量
        ↓ 每天 04:00
  一条 [worklog] note
        ↓ LYPH device API
  captures
        ↓ 服务器 04:30
  LYPH 日报生成 → 发布为 public automatic 日志
```

## 1. 采集什么

| 来源 | 内容 | 是否上传 |
|---|---|---|
| 前台窗口 | 应用名、窗口标题（截断 120 字、敏感词替换） | 是 |
| 空闲检测 | `GetLastInputInfo`，空闲超过 5 分钟不采样 | 只用于过滤 |
| Git | 指定仓库、指定作者的 commit 主题 | 是 |
| Codex | 会话数、用户消息数、cwd 的最后一级项目名 | 是 |
| 浏览器 | 按分类统计访问次数（代码托管/刷题/技术文档/云服务/邮件/学习/其他） | 是，只有分类和次数 |
| 文件 | 指定根目录下当天修改文件数，按项目分组 | 是 |
| 截图/键盘/剪贴板 | **不采集** | 否 |
| 浏览器 URL/页面标题 | **不采集** | 否 |

窗口标题命中 `password`、`token`、`密码`、`私钥`、`银行卡`、`验证码` 等词时，上传为 `[敏感窗口已隐藏]`。浏览器窗口标题统一为 `[浏览器页面已隐藏]`。

## 2. 文件与路径

```text
D:\xx\wechatbot\agent-router\worklog_collector.py       # 采集守护进程
D:\xx\wechatbot\agent-router\worklog_sources.py         # Git/Codex/浏览器/文件历史
D:\xx\wechatbot\agent-router\worklog_config.example.json
D:\xx\wechatbot\agent-router\install_worklog_task.ps1   # 当前用户计划任务
%LOCALAPPDATA%\WeChatAgentWorklog\config.json           # 实际配置
%LOCALAPPDATA%\WeChatAgentWorklog\secrets.json          # DPAPI 加密的设备 token
%LOCALAPPDATA%\WeChatAgentWorklog\worklog.sqlite3       # 采样、每日状态
%LOCALAPPDATA%\WeChatAgentWorklog\collector.log
C:\Users\liubo\Documents\worklog\YYYY-MM-DD.md          # 本地 Markdown 副本
```

## 3. 首次配置

1. 复制配置：

```powershell
Copy-Item D:\xx\wechatbot\agent-router\worklog_config.example.json `
  "$env:LOCALAPPDATA\WeChatAgentWorklog\config.json"
```

2. 在 LYPH 服务器创建一个专用设备，拿到一次性 token：

```bash
# 在服务器 /opt/lyph-worklog
docker cp create_device.py lyph-api-1:/data/create_device.py
docker exec -u root lyph-api-1 chmod 644 /data/create_device.py
docker exec lyph-api-1 python /data/create_device.py pc-home-worklog > device.json
```

把 `device.json` 拷回 Windows，然后导入 DPAPI：

```powershell
python D:\xx\wechatbot\agent-router\worklog_collector.py `
  --import-device .\device.json
Remove-Item .\device.json
```

3. 安装当前用户计划任务（不需要管理员）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File D:\xx\wechatbot\agent-router\install_worklog_task.ps1
```

任务在登录时启动，并在每天 03:55 再检查一次；`MultipleInstances=IgnoreNew` 保证只有一个采集进程。

## 4. 日常运行

- 采集进程每 30 秒采样一次前台窗口。
- 每天 04:00 生成前一天 note，上传到 LYPH。
- 服务器每天 04:30 运行 `publish_daily.sh`：
  - 只处理当天全部 capture 都是 `[worklog]` note 的日期；
  - 触发 LYPH 日报生成；
  - 把生成结果更新为 `visibility=public` 并发布。
- 如果 04:30 时电脑还没上传，第二天 04:30 会回看最近 3 天。
- 如果机器关机/休眠，下次启动会补传昨天；采集进程的本地 SQLite 会保留采样。

## 5. 补全往日

在 Windows 上：

```powershell
python D:\xx\wechatbot\agent-router\worklog_collector.py --backfill 30
```

它会按天读取：

- Git 提交
- Codex 会话（cwd/数量）
- 浏览器分类（次数）
- 文件修改数
- 本地已有的采样

每天生成一条 `[worklog]` note，上传到 LYPH。没有证据的日期会跳过；内容过短的日期也会跳过，避免生成“无意义日报”。

然后在服务器上触发日报生成并发布：

```bash
docker exec lyph-api-1 python /data/worklog_publish.py \
  --days 30 --visibility public --timeout 300
```

单日重跑：

```bash
docker exec lyph-api-1 python /data/worklog_publish.py \
  --date 2026-10-21 --visibility public
```

只预览不发布：

```bash
docker exec lyph-api-1 python /data/worklog_publish.py \
  --date 2026-10-21 --dry-run
```

## 6. 隐私与边界

- 不采集键盘输入、剪贴板、摄像头、麦克风、完整屏幕截图。
- 浏览器只上传分类和次数，不上传 URL、查询参数、页面标题。
- Codex 只上传 cwd 最后一级和消息数量，不上传提示词或回复正文。
- 窗口标题会脱敏；浏览器标题统一隐藏。
- 公开日志默认只包含高层活动统计；如果不想公开，把发布命令改成 `--visibility private`，或改 `publish_daily.sh`。
- 设备 token 用 Windows DPAPI 加密，只有当前 Windows 用户能解密。
- 采集进程只上传到 `https://123.57.153.49`，不访问其他地址。

## 7. 停止/卸载

```powershell
Stop-ScheduledTask -TaskName "WeChatAgentWorklog"
Unregister-ScheduledTask -TaskName "WeChatAgentWorklog" -Confirm:$false
```

删除本地数据：

```powershell
Remove-Item -Recurse "$env:LOCALAPPDATA\WeChatAgentWorklog"
```

吊销 LYPH 设备：在 LYPH 后台「桌面与日报」或数据库 `devices` 表把对应设备 `revoked=1`。
