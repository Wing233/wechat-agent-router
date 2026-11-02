# 工作电脑活动采集与 LYPH 日报

工作日志采集已经集成到 **WeChatAgentDesktop** 桌面端，不再单独安装计划任务。桌面端启动时自动启动采集线程，退出时自动停止。

```text
WeChatAgentDesktop
  ├─ 本机 Agent（pc-home）
  ├─ 本地控制台 API / UI
  └─ WorklogService
        ├─ 前台窗口采样（30s）
        ├─ Git / Codex / 浏览器分类 / 文件改动
        ├─ 每天 04:00 汇总前一天
        └─ 上传 LYPH device API
                ↓
        LYPH 04:30 生成并发布日报
```

## 1. 在桌面端使用

打开桌面端左侧「工作日志」：

- 查看采集状态、今日采样数、LYPH 设备是否已配对
- 启动 / 停止采集
- 立即补采昨天
- 补全最近 30 天
- 修改采样间隔、空闲跳过、每日采集时间、本地日报目录
- 保存 / 清除 LYPH 设备 token
- 打开数据目录、本地日报目录、采集日志

桌面端「设置」里的「开机自动启动」建议保持开启；这样登录 Windows 后桌面端会以无窗口模式启动，工作日志采集也会一起启动。

## 2. 采集什么

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

## 3. 数据位置

源码模式：

```text
D:\xx\wechatbot\agent-router\desktop\.runtime\worklog\
  config.json
  secrets.json          # DPAPI 加密的 LYPH 设备 token
  worklog.sqlite3
  collector.log
  notes\                # 如果配置里使用默认目录
```

打包 exe 模式：

```text
<exe 同目录>\data\worklog\
```

旧版独立采集器如果存在，桌面端首次启动会自动迁移：

```text
%LOCALAPPDATA%\WeChatAgentWorklog
```

迁移后旧目录保留作备份，不再写入。

## 4. 每天的时间线

```text
04:00  WorklogService 汇总前一天并上传 [worklog] note
04:30  LYPH 服务器 cron 生成日报并发布
```

- 采集线程随桌面端启动，不需要单独任务。
- 如果 04:00 时电脑关机/休眠，下次桌面端启动会自动补跑前一天。
- 如果 04:30 时还没上传，第二天 04:30 会回看最近 3 天。
- 如果桌面端被关闭，采集也会停止；重新打开后继续。

## 5. 补全往日

桌面端「工作日志」→「补全最近 30 天」，会读取：

- Git 提交
- Codex 会话（cwd/数量）
- 浏览器分类（次数）
- 文件修改数
- 本地已有采样

每天生成一条 `[worklog]` note，上传到 LYPH。没有证据的日期会跳过；内容过短的日期也会跳过。

也可以命令行补全（路径以桌面端数据目录为准）：

```powershell
python D:\xx\wechatbot\agent-router\worklog_collector.py `
  --config "D:\xx\wechatbot\agent-router\desktop\.runtime\worklog\config.json" `
  --backfill 30
```

服务器端发布：

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

- 在桌面端「工作日志」点「停止采集」。
- 或者直接退出桌面端。
- 要彻底删除本地数据，退出桌面端后删除 `desktop\.runtime\worklog`。
- 吊销 LYPH 设备：在 LYPH 后台「桌面与日报」或数据库 `devices` 表把对应设备 `revoked=1`。
