# 入站 Gate 与 Mandate

这套机制解决两个问题：

1. **一条消息该不该进入工具循环**：由确定性代码判断，不交给模型。
2. **高危工具能不能执行**：必须带主人批准过的 Mandate，不能只靠提示词自觉。

## 1. 四类入站判定

`governance.classify_inbound()` 是纯函数，无模型调用、无随机性。

| 分类 | 含义 | 行为 |
|---|---|---|
| `read_only` | 查询、问答、附件识图 | 只暴露只读工具 |
| `action_explicit` | 主人明确指令 | 暴露全部工具，高危仍需 Mandate |
| `action_inferred` | “应该加个…”“能不能…” | 只写 Decision，不执行，回一句提示 |
| `ignore` | 访客动作、空消息、访客 @Agent | 不进入工具循环 |

判定顺序：

```text
审批回复（主人） > 明确 @Agent > 空消息/附件 > 访客规则 > 主人规则
```

主人规则的默认值是 `read_only`。只有出现明确的动作动词、强指令前缀，才会升级为 `action_explicit`。疑问句默认按只读处理；只有“能不能/可不可以/要不要”这类疑问式建议才记为 `action_inferred`。否定式指令（“先别执行”“不要删除”）也按只读处理，不会授予动作工具。

### 只读工具

`READ_ONLY_TOOLS` 是**精确名称**白名单，不做后缀匹配。只读轮次里，未列出的工具默认拒绝，避免新装插件用同名后缀混进来。

`toolbox__sql_query`、`toolbox__governance_report`、`toolbox__confirm_action` 故意不在只读名单里：前者能读 `credentials.secret_json`，后两者会暴露审批码或写过期状态。需要时用明确动作指令重新发起。

新增只读工具时，把它加进 `governance.py` 的 `READ_ONLY_TOOLS`，并确认它不会读写 secrets。

## 2. Decision 审计

每条入站消息都会写入主库 `/data/router.db` 的 `decisions` 表：

```sql
decisions(
  id, trace_id, role, chat_id, text_hash, text_preview,
  classification, would_accept, executed, reason, rule,
  meta_json, created_at
)
```

- `text_hash`：原文 SHA-256，便于去重和追查。
- `text_preview`：前 240 字符，便于人工翻看。
- `would_accept`：如果切到 auto，它本来会不会接。
- `executed`：这次是否真的进入了执行路径。

查看方式：

```text
toolbox__governance_report(kind="decisions", limit=20)
toolbox__governance_report(kind="mandates", limit=20)
```

`action_inferred` 的 `would_accept=1, executed=0`，就是 dsh 里的 “would accept” 记录。先观察几天，再决定要不要放宽规则。

## 3. Mandate

`confirmations` 表在原审批码基础上扩展：

```text
code, action, summary, status,
tool, scope_json, policy_version,
approved_by, strength,
expires_at, revoked_at, used_at,
max_uses, uses,
created_at, decided_at, updated_at
```

### 生命周期

```text
pending -> approved -> used
       \-> rejected
       \-> expired
       \-> revoked
```

- `pending`：已发微信审批，等待 `APPROVE XXXX` / `REJECT XXXX`。
- `approved`：主人已批准，可以执行。
- `used`：一次性或次数用尽。
- `expired`：超过 `expires_at`。
- `revoked`：主人主动撤销。

审批必须使用严格格式：`APPROVE XXXX` 或 `REJECT XXXX`，code 必须和通知里的 code 完全一致。普通聊天里的“执行”“可以”“同意”不再触发批准；错误 code 会直接提示“确认码不存在或已过期”，不会回退到唯一的 pending。

### 绑定范围

`scope_json` 至少包含：

```json
{
  "tool": "toolbox__python_repl",
  "args_hash": "sha256...",
  "args_preview": "{\"code\": \"print(1)\"}",
  "paths": [],
  "agent_id": "",
  "max_uses": 1,
  "policy_version": "2026-11-01.1"
}
```

执行时校验：

- 工具名必须匹配；审批时如果是裸别名（如 `python_repl`），会先归一化成 `toolbox__python_repl`，两个不同插件即使后缀相同也不能互相复用。
- 高危工具必须绑定非空 `args`，`args_hash` 必须匹配；`mandate`、`confirm_code`、`timeout`、`wait_ms` 不参与哈希。空参数审批不能变成“空白支票”。
- `agent_id`、`paths` 如果填写，必须匹配；路径 scope 使用 `path/cwd/target/source/src/dst/dest/destination`。
- `policy_version` 必须与当前 `governance.POLICY_VERSION` 一致。
- `expires_at` 未过期、`revoked_at` 为空、`uses < max_uses`；`uses` 的递增是单条原子 SQL，避免并发复用。

范围要扩大，不是改旧记录，而是重新走一次审批。

### 审批流程

```text
1. 模型调用 ask_human_confirmation(
     action="运行 Python 脚本",
     summary="...",
     tool="toolbox__python_repl",
     args={"code": "..."},
     ttl_minutes=30,
     max_uses=1
   )
2. 主人收到微信，回复 `APPROVE XXXX`（或 `REJECT XXXX`）
3. 模型下一轮把 XXXX 作为 mandate 传给高危工具
4. registry 在执行 handler 前硬校验，失败直接返回错误
```

审批消息会显示工具名、参数预览、路径/Agent scope 和有效期。高危工具不传 `tool` 或 `args` 时，`ask_human_confirmation` 直接拒绝，避免批准一个看不见的动作。

`ask_human_confirmation` 不传 `tool` 也能创建审批，但这种审批**不能**用于高危工具，只适合查看状态。系统会在返回值和批准回复里提示。

### 会话授权（平衡模式）

如果每次高危操作都弹审批太碎，主人可以在**私聊**里发一条确定性命令，开启一段限时授权：

```text
开发模式 2 小时
运维 30 分钟
模型 30 分钟
授权全部 1 小时
查看授权
取消授权
```

预设：

| 预设 | 覆盖工具 | 路径 scope |
|---|---|---|
| `dev` | python_repl、run_sandbox、write_patch、devtools shell、ops 文件写入/删除等 | `/workspace`、`/app`、`/lyph-src` |
| `ops` | devtools shell、admin 任务/配置、send_to_channel、webhook、slack、send_to_agent | `/workspace`、`/app`、`/lyph-src` |
| `model` | model_add/remove/bind/switch、budget_set、optimize_apply | 不限 |
| `all` | 所有高危工具 | `/workspace`、`/app`、`/lyph-src` |

规则：

- 只接受主人私聊命令，解析在 `governance.parse_session_command()`，**不是 LLM 工具**，模型不能给自己授权。
- 最长 8 小时；`max_uses = min(2000, max(100, 分钟数 × 20))`。
- 高危工具调用时如果没带 per-call mandate，`require_mandate` 会自动查找匹配的会话授权并扣减一次；per-call mandate 仍然优先。
- 有路径参数的工具会校验 `path/cwd/src/dst/...` 是否在 scope 内；`python_repl` 这类没有路径参数的工具只受工具和时限约束。
- 会话授权记录在 `confirmations` 表，`strength='session'`，`approved_by` 是实际发命令的主人；`uses` 每次扣减，到期/撤销/用完即失效。
- 主人可以随时发“取消授权”立即撤销所有生效中的会话授权。

注意：`python_repl` 和 shell 运行在 Router 容器里，能读到 `/data` 下的数据库和配置密钥。会话授权是限时止损，不是隔离沙箱；建议只在需要时开短窗口，用完就“取消授权”。

## 4. 高危工具

当前强制 Mandate 的工具：

```text
toolbox__python_repl
toolbox__run_sandbox
toolbox__run_docker_sandbox
toolbox__write_patch
toolbox__snapshot_checkpoint        # 仅 action=restore
toolbox__delegate_to_subagent
toolbox__webhook_post
toolbox__slack_post
toolbox__mcp_call
toolbox__browser_click_fill
toolbox__github_api                 # 非 GET/HEAD/OPTIONS
toolbox__memory_update
toolbox__learn_skill
devtools__execute_bash
devtools__run_command
devtools__http_request
ops__fs_write
ops__fs_replace
ops__fs_delete
ops__fs_move
admin__config_set
admin__enable_plugin
admin__disable_plugin
admin__reload_plugin
admin__set_job_interval
admin__set_job_enabled
agents__send_to_agent
```

校验点在 `ToolRegistry.execute()`，不是各插件自己实现。这样新增调用路径（HTTP、定时任务、子 Agent）也会走同一道门。

## 5. 已知边界

- 分类器是规则 + 打分，不是语义模型。先用 `decisions` 观察误判，再按真实语料调整 `governance.py` 的词表。
- `delegate_to_subagent` 和 `agents__send_to_agent` 已经要求 Mandate，但子 Agent 侧还没有独立的 scope 校验；这部分是下一阶段。
- `evolution` 的插件安装走严格 `APPROVE XXXX` 审批，但没有走 ToolRegistry 的 Mandate；插件 ID 已限制为安全字符，提案路径必须在 `/sandbox` 下。
- 多 owner 场景下，`match_pending` 仍以 hub owner 的 chat 查找待审批通知；`approved_by` 会记录实际回复者的 `user_id`，但跨 owner 的审批隔离还需要下一阶段。
- `site_report` 等主动播报还没有 observe 模式；建议下一步先记录 “would send”，确认频率和内容后再真发。
- `POLICY_VERSION` 变更会让旧审批失效，这是有意的：策略变了，旧授权不能自动继承。
