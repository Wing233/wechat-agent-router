# 自然语言运维：模型、成本、链路、评测与优化

这套能力没有管理页。所有操作都是普通工具，主人直接在微信、QQ 或任意已接入入口里说一句话即可。

## 1. 设计边界

- **只对主人开放**：这些工具默认只对站主开放；访客即使被允许使用工具，也会被 `owner_only_tool` 过滤。
- **不隐藏变更**：切换模型、改预算、执行优化都会写 `plugin_events` 审计，并返回实际生效的值。
- **不把密钥交给模型**：`model_list`、`config_get`、`list_channels` 只返回脱敏后的 key。
- **不依赖新的数据库服务**：成本、轨迹、评测分别使用独立 SQLite 文件，沿用现有 WAL 模式。
- **免费优先、付费兜底**：角色可以绑定多个模型，按顺序尝试；免费模型失败会自动落到后面的付费模型。

## 2. 模型角色

`config.json` 里的 `model_bindings` 把“用途”映射到模型 ID 列表：

```json
{
  "chat": ["deepseek-chat"],
  "vision": ["qwen-vl", "deepseek-chat"],
  "judge": ["deepseek-chat"],
  "control": ["free-model", "deepseek-chat"],
  "job.default": ["free-model", "deepseek-chat"],
  "plugin.default": ["free-model", "deepseek-chat"],
  "embedding": []
}
```

| 角色 | 用途 | 建议 |
|---|---|---|
| `chat` | 主对话 | 质量优先，至少两个模型做 fallback |
| `vision` | 图片理解 | 必须支持 image_url |
| `judge` | 评测/评审 | 可以比 chat 更便宜 |
| `control` | 意图识别、短分类 | 免费模型优先 |
| `job.default` | 定时任务、摘要 | 免费模型优先 |
| `plugin.default` | 插件内部低风险调用 | 免费模型优先 |
| `embedding` | 向量化 | 需要单独的 embedding 接口，当前留空 |

解析顺序：

```text
job.site_report -> job.default -> default -> chat
plugin.websearch -> plugin.default -> default -> chat
model:<id> -> 直接使用该模型（评测/测试用）
```

每个模型条目包含：

```json
{
  "id": "free-model",
  "name": "免费模型",
  "kind": "llm",
  "base_url": "https://example.com/v1",
  "api_key": "...",
  "model": "free-model",
  "input_price_per_m": 0,
  "output_price_per_m": 0,
  "tags": ["free", "cheap"],
  "enabled": true
}
```

`tags` 里的 `free` 会被优化建议用来识别“可以承接低风险任务的免费模型”。

## 3. 常用说法

### 帮助

| 你说 | 实际调用 |
|---|---|
| `help` / 你能做什么 / 有哪些功能 / 怎么用 | `show_help` |

`help` 会直接返回当前入口、身份、已加载工具、可用 Agent 和示例命令；访客只会看到只读能力。它不经过 LLM，因此不会被模型自由发挥或漏项。

### 模型

| 你说 | 实际调用 |
|---|---|
| “现在用的什么模型？” | `model_list` |
| “加一个免费模型，地址是 …，key 是 …” | `model_add` |
| “把 chat 切到 xxx” | `model_switch_chat` |
| “让低风险角色先用免费模型，失败再用主模型” | `model_bind role=job.default models=free,primary` |
| “测一下 xxx 通不通” | `model_test` |
| “删掉 xxx 模型” | `model_remove` |

### 成本

| 你说 | 实际调用 |
|---|---|
| “这个月花了多少？” | `cost_report days=30` |
| “今天谁花得最多？” | `cost_report days=1`，看 `by_role` |
| “访客每天最多花 1 块” | `budget_set role=guest amount=1` |
| “主人不限额度” | `budget_set role=owner amount=0` |

成本账本按 Asia/Shanghai 自然日统计。金额单位与模型供应商账单一致；免费模型会记为 0。

### 链路

每条入站消息都会生成 12 位 trace id，记录：

- 来源（微信 / QQ / 其他）、会话、用户、身份
- 模型调用：用途、模型、耗时、token、成本
- 工具调用：名称、参数、结果、耗时、是否报错
- 最终回复或错误

| 你说 | 实际调用 |
|---|---|
| “刚才 QQ 那条为什么失败？” | `trace_search query=…` 然后 `trace_explain` |
| “查一下 trace 4f6456aee642” | `trace_get` |
| “最近一小时有哪些失败？” | `trace_search status=error hours=1` |
| “今天整体怎么样？” | `insight_report hours=24` |

`trace_explain` 会给出确定性的归因：预算、401/403、429、超时、5xx、网络、工具错误、空回复、慢请求等，不会让模型猜。

### 评测与漂移

评测用例保存在 `data/eval_cases.json`，默认 8 条，覆盖算术、翻译、JSON、拒答、日期、单位、简洁性和单句约束。

| 你说 | 实际调用 |
|---|---|
| “跑一下评测” | `eval_run` |
| “用 xxx 模型跑评测” | `eval_run model_id=xxx` |
| “看看评测结果” | `eval_report` |
| “最近模型是不是变差了？” | `eval_drift` |
| “加一条评测：…” | `eval_add_case` |

`eval_drift` 把最近几次 run 与更早的 baseline 窗口比较：

- 下降 ≥ 10 分：`drifting`
- 下降 5–10 分：`watch`
- 上升 ≥ 10 分：`improving`
- 数据不足：`insufficient`

评测只测“不调用工具时的基础质量”，不是 MMLU，也不替代真实业务回归。它的价值是低成本、可重复、能看出换模型后的明显退化。

### 优化

| 你说 | 实际调用 |
|---|---|
| “帮我看看哪里还能省/哪里不稳” | `optimize_suggest` |
| “按第一条建议改” | `optimize_apply suggestion_id=…` |

可自动执行的只有低风险动作：

- 把 `job.default` / `plugin.default` / `control` 改成“免费模型 + 原模型 fallback”
- 调整某个角色的每日预算

需要新 key、换主模型、修插件等动作只会返回 `manual`，不会擅自执行。

## 4. 多入口投递

### 查看入口

`list_channels` 返回所有 Endpoint：

- endpoint id（例如 `qq:group:123456`、`wechat:owner`）
- 渠道、类型、标签
- conversation_id、account_id
- 能力列表（text / image / file / quote）

### 发到别的入口

```text
把这条发到 QQ 群：今晚的站点报告已经生成
```

调用：

```json
{
  "endpoint": "qq:group:123456",
  "text": "今晚的站点报告已经生成"
}
```

跨渠道链路：

```text
Router send_to_channel
  -> store.enqueue_channel_outbound(endpoint, channel, payload)
  -> 桥接端 GET /v1/channels/outbound
  -> bot.sendMessage(conversation_id, text)
  -> POST /v1/channels/outbound/ack
```

投递语义是“至少一次”：桥接端发送成功后才 ack；如果进程在发送后、ack 前退出，消息可能重复一次。对通知类消息这是可接受的取舍。

### 微信

`wechat:owner` 直接走 Hub 发给站主，不进入队列。微信里的普通回复仍然自动回到来源会话，不需要显式调用。

## 5. 数据文件与保留

| 文件 | 内容 | 默认保留 |
|---|---|---|
| `data/spend.db` | 每次模型调用的 token、成本、trace id | 永久（很小） |
| `data/traces.db` | 请求轨迹与 span | 30 天，启动时清理 |
| `data/evals.db` | 评测 run 与逐用例结果 | 永久（很小） |
| `data/eval_cases.json` | 评测用例 | 手动维护 |

相关配置：

```json
{
  "trace_db": "/data/traces.db",
  "trace_retention_days": 30,
  "eval_db": "/data/evals.db",
  "eval_cases": "/data/eval_cases.json"
}
```

## 6. 安全与审计

- 模型 key 只存在 `config.json`（容器内 0600），不会进入 trace、spend、eval 或聊天回复。
- `model_add` 更新已有模型时，传 `***` 或带 `...` 的 key 会保留原 key。
- 渠道 token 在 `list_channels` / `config_get` 中只显示前后各几位。
- 所有工具调用仍受主人/访客白名单约束；访客不能看成本、轨迹、评测，也不能切模型。
- `send_to_channel` 只允许发到已配置且启用的渠道，或虚拟 `wechat`；不能任意指定 URL。
- 预算在调用模型前检查，超限直接返回中文提示，不会产生额外费用。

## 7. 已知限制

- 评测不会调用工具，因此不能发现“工具参数写错”这类问题；这类问题看 `trace_search status=error` 和工具 span。
- `embedding` 角色目前没有接入向量模型；需要时新增独立客户端，不要复用 chat。
- 定时任务可用 `jobs.<id>.target_endpoint` 指定入口；任务逻辑里也可以调用 `send_to_channel`。
- 轨迹是请求级采样，不是分布式 tracing；跨 Router 与 Agent 的完整链路还需要 Agent 侧回传 trace id。
