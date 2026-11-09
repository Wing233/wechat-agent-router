# 本机技术栈周更（Rust 桌面端）

桌面端「技术栈」页在本机扫描工程依赖与获准的 Agent 会话文本，生成**待人工复核**的周更报告；不上传源码、会话原文或本机路径，也不会自动发布到 Lyph。

## 运行位置

- 实现：`desktop-rust/src/tech_stack.rs`（纯 Rust，无 Python 依赖）
- 数据目录：`%LOCALAPPDATA%\WeChatAgentDesktop\tech-stack\`

```text
tech-stack\
  config.json                     # 计划、扫描目录、数据源
  reports\
    latest.json                   # 最近一次报告（结构化）
    latest.html                   # 最近一次报告（可读页面）
    tech-stack-weekly-YYYYmmdd-HHMMSS.json/.html
  workbuddy-exports\              # 手动放 WorkBuddy 导出文件
```

## 计划与开关

- `weekly_day`：0=周一 … 6=周日，默认 6（周日）
- `weekly_time`：本地时间 `HH:MM`，默认 `10:00`
- `enabled`：关闭后不再自动采集，手动「立即采集」仍可用
- 进程启动时若从未生成过报告，会立即采集一次；之后按周计划执行
- 需要桌面端保持运行（或开启开机自启）

## 采集什么

| 来源 | 内容 |
|---|---|
| 工程依赖 | 配置目录下的 `package.json`、`pyproject.toml`、`requirements*.txt`、`Cargo.toml`、`pom.xml`、`go.mod`、`Dockerfile` |
| 源码后缀 | 项目顶层与 `src/app/lib/tests/test` 下的 `.py/.java/.rs/.go/.ts/.tsx/.js/.jsx/.cpp/.cc/.c/.ipynb` |
| Codex | `~/.codex/sessions`、`~/.codex/archived_sessions` 中近一年 JSONL 的用户/助手文本，只提取技术名与计数 |
| OpenCode | `opencode.db` 的 message/part 表（只读），同样只提取技术名与计数 |
| WorkBuddy | `workbuddy-exports` 下手动导出的 `.md/.txt/.json/.jsonl` |
| 公网基线 | `public_site_url` 的公开技术栈名称，用于标记「公开栈已有 / 建议新增候选」 |

命中 `api key`、`token`、`password`、`secret`、`authorization`、私钥等敏感模式的文本片段**整体跳过**。

## 报告口径

- `suggested_progress` 是基于实作证据的估算，不是自评：`min(95, 22 + min(工程数,5)*13 + min(会话来源数,3)*2)`
- 按工程目录数降序、名称升序排列
- `already_published` 只有在成功读取公网基线时才判定；读取失败记为「公网基线未读取」，不会当作新增
- 报告默认只写本机，发布仍需站主在 Lyph 后台确认

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tech-stack` | 状态 + 最近报告摘要 |
| POST | `/api/tech-stack/run` | 立即采集（后台线程） |
| PUT | `/api/tech-stack/config` | 保存 `enabled / weekly_day / weekly_time` |
| POST | `/api/tech-stack/open` | 打开 `report / reports / exports / config` |

## 排障

- 报告目录为空：确认 `project_roots` 指向真实工程目录；旧配置为空数组时启动会自动回填默认目录。
- 「公开栈读取失败」：检查 `public_site_url` 是否可访问；不影响本机报告生成。
- 采集报错会写入状态里的「最近错误」，同时写入桌面端运行日志。
