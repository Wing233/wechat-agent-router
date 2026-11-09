# GitHub Trending 每日简报与项目评估

`github_trending` 插件做两件事：

1. 每天 09:00（Asia/Shanghai）抓取 GitHub Trending，生成一份适合微信阅读的简报推给主人。
2. 主人点名某个项目后，抓取该项目的元数据、README、目录和依赖清单，交给模型评估“值得借鉴什么 / 是否适合作为插件接入 / 风险与第一步”。

## 每日简报

默认配置：

```json
{
  "plugins": {
    "github_trending": {
      "db": "/data/github_trending.db",
      "count": 12
    }
  },
  "jobs": {
    "github_trending": {
      "enabled": true,
      "interval_minutes": 1440,
      "window_minutes": 1440,
      "daily_at": "09:00"
    }
  }
}
```

- `daily_at` 是本地时间（UTC+8），插件会在首次加载时把 `job:github_trending:next` 设到下一个 09:00。
- `count` 是每次保留的项目数，默认 12，范围 5–20。
- 如果当天已经推送过，任务会跳过，避免重启后重复发送。
- 抓取顺序：GitHub Trending 页面（`?since=daily` → 默认页）→ 失败时回退到 GitHub Search API，并按涨星速度（`stars / 创建天数`）排序，而不是总 stars 排名。Trending 页已返回 ≥3 项时直接按页内排名推送，不再补齐，避免掺入总 Star 最多的老项目。
- 简报包含：序号、`owner/repo`、stars、语言、描述、今日新增 stars；最近 7 天出现过的项目会标“再次上榜”。
- 简报顶部有一段 2–3 句的 LLM 趋势总结；LLM 不可用时自动省略，不影响列表。

手动查看：

```text
今天 GitHub trending 有什么
现在发一份 trending
```

对应工具：`github_trending__trending_now`。

## 点名评估

简报发出后，可以直接回复：

```text
评估 1
评估 1 和 3
评估 acme/agent-kit
看看 acme/agent-kit 值不值得借鉴
```

工具：

- `github_trending__trending_list`：列出最近一份简报的序号和仓库。
- `github_trending__trending_evaluate`：抓取指定仓库的：
  - stars / forks / issues / license / topics / 创建与更新时间
  - README 前 8000 字符
  - 顶层目录（最多 120 项）
  - 依赖清单（`pyproject.toml`、`package.json`、`requirements.txt`、`go.mod`、`Cargo.toml`、`pom.xml`、`build.gradle`，最多 3 个）

返回数据后，模型会结合当前 agent 架构给出：

- 值得借鉴的设计/实现
- 是否适合作为插件接入，还是只借鉴思路
- 许可证、维护活跃度、安全/复杂度风险
- 建议的第一步

## 接入流程

评估只是建议，不会直接改代码。主人确认“接入”后：

- 代码/文件修改仍然走 `write_patch`、`python_repl`、`ops__fs_*` 等高危工具。
- 如果当前有 `开发模式` 会话授权，会直接在 scope 内执行；否则需要单次 Mandate 或先开一个会话授权。
- 建议先让 agent 产出“插件目录 + manifest + 工具/Job 设计 + 测试”的最小方案，再决定是否落代码。

## 权限与限制

manifest 声明：

```text
db:router        读取主库 state，用于记录 last_sent / next_run
send:wechat      推送简报
llm:use          生成趋势总结
net:github.com   Trending HTML
net:api.github.com  仓库元数据 / README / 目录 / 依赖
secrets: github_token
```

- `github_token` 来自 `config.json` 的 `toolbox.github_token`；有 token 时 API 限额 5000 次/小时，没有时 60 次/小时。
- 每日简报只用 1–2 次请求；评估一个项目约 3–6 次请求。
- GitHub Trending 页面结构变化时自动回退 Search API；如果两者都失败，任务会返回错误，不会推送空简报。
- 插件自己的数据在 `/data/github_trending.db`，保留 90 天简报，`seen` 表用于“再次上榜”标记。

## 测试

```bash
python -m pytest -q tests/test_github_trending.py
```

覆盖 HTML 解析、stars 格式化、repo 归一化、序号解析、09:00 调度计算和简报文本生成。
