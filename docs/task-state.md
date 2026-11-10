# 任务状态与重启恢复

Router 现在把“收到消息”和“派给 Agent”都当成持久任务，而不是内存里的临时状态。

## 1. 入站消息日志

表：`inbound_messages`

```text
dedup_key, source, message_id, chat_id, user_id, text,
status, attempts, last_error, created_at, updated_at, processed_at
```

状态：

```text
processing -> done
           \-> failed -> processing（重试）
                      \-> dead（超过 3 次）
```

规则：

- 每条 Hub 消息用 `hub:<message_id>` 作为 `dedup_key`，唯一约束保证重复投递不会重复处理。
- 先 `claim_inbound()` 写 `processing`，再 `handle_inbound()`；成功后才写 `done` 并推进 `last_message_id`。
- 处理中崩溃：启动时 `recover_inbound()` 把 `processing` 改回 `pending`，`last_id` 没推进，Hub 会重新投递，消息会被重试。
- 连续失败 3 次：标 `dead`，推进 `last_id`，并给主人发一条“消息处理失败”的通知，避免一条坏消息堵死整个队列。
- `last_message_id` 只在消息 `done`/重复跳过/进入 dead 后推进；非主人消息和非入站消息直接推进。
- 日志保留 30 天（`prune_inbound`）。

语义是 **at-least-once**：如果进程在“已发送回复、还没写 done”之间崩溃，重启后可能重复回复一次。优先保证不丢消息；要做到 exactly-once 需要给发送链路也加幂等键。

## 2. Agent 任务状态

表：`agent_tasks`

```text
id, agent_id, status, payload_json, result_json, error,
attempts, max_attempts, lease_token, lease_until,
delivered, created_at, updated_at, completed_at
```

状态：

```text
queued -> claimed -> running -> completed
   ^          |          |
   |          v          v
   +------ requeue <-- lease expired
                         \-> failed（attempts >= max_attempts）
```

- `queued`：已接收/排队，Agent 还没领取。
- `claimed`：Agent 通过 `GET /agent/tasks` 领取。
- `running`：Agent 调用 `POST /agent/tasks/{id}/heartbeat` 后进入执行中。
- `completed` / `failed`：Agent 回传结果。
- 默认 lease 30 分钟，`max_attempts=3`；维护循环每 30 秒检查过期 lease，未超过次数则重新排队，超过则标 `failed` 并通知主人。
- 任务不再因为 `wait_seconds` 超时被删除；`wait_seconds` 只决定 Router 等多久后给用户回“排队/已领取”。

### 用户会看到什么

| 实际状态 | 回复 |
|---|---|
| Agent 快速完成 | 直接返回结果 |
| Agent 已领取 | `任务 xxxx 已被 X 领取，正在执行；完成后通知你。` |
| Agent 在线但还没领取 | `X 还没有领取，任务 xxxx 已排队；它领取后会通知你。` |
| Agent 离线/忙 | `X 当前没有领取（可能离线或忙），任务 xxxx 已排队；它领取后会通知你。` |
| 任务失败/超时 | `任务 xxxx 失败：...` |

不会再出现“任务已交给 X，完成后通知你”但任务已经不在队列里的情况。

### 结果只投递一次

- Agent 调 `POST /agent/result` 时带 `task_id`、`ok`、`text`、`lease_token`，可选 `channel`、`chat_id`。
- `complete_agent_task()` 在**同一条 UPDATE 语句**里校验 `status IN ('claimed','running') AND lease_token=?`，并在同一事务里向 `agent_deliveries` 写一条 `dedupe_key='agent-task:<task_id>'` 的待发记录。任务被重新排队后，旧执行端即使回传也会被拒绝，省略或伪造 lease 同样失败。
- 发送由后台 `deliver_agent_results()` 完成：先原子领取 `pending` 行（`status='sending'` + 短期发送租约），发送成功后才写 `sent`；进程在“取出结果、发送前”退出，重启时 `recover_agent_deliveries()` 会把 `sending` 改回 `pending` 再重试。
- 同步等待的 `call_agent` 与后台发送共用同一行：`claim_agent_delivery_inline()` 先把 `pending` 改成 `inline`，只有一个消费者能拿到结果，不会重复发。
- 目标渠道和会话来自任务 payload（入站时记录的 `channel` / `chat_id`）。`wechat` / `hub` 走 Hub 直发；其他轮询渠道写入 `channel_outbox`，由对应桥接层拉取并 ack。
- `POST /agent/result` 返回 `{"ok":..., "queued":..., "delivery":...}`；`queued=true` 表示结果已持久化、等待发送，不再表示“已经发出去”。
- 留言通知 `/agent/notify` 也返回真实状态：`sent`（已发出）、`queued`（Hub 忙，已进重试队列）、`failed`（鉴权失败等，未入队）。
- 已完成任务重复回传是幂等的，不会覆盖第一次结果，也不会生成第二条待发记录。

### Agent 侧接口

```text
GET  /agent/tasks?agent_id=...
POST /agent/tasks/{task_id}/heartbeat   {"lease_token": "..."}
POST /agent/result                     {"task_id":"...", "ok":true, "text":"...", "lease_token":"..."}
```

本地 Agent 回传 `lease_token` 后，旧 lease 的迟到结果会被拒绝，避免任务被重新排队后旧执行又完成新领取。

## 3. 重启恢复

启动时：

1. `store.recover_inbound()`：`processing` 入站消息改回 `pending`。
2. `store.recover_agent_deliveries()`：`sending` 的结果投递改回 `pending`，重启后继续发送。
3. `BROKER.recover()`：`claimed`/`running` 任务按 attempts 改回 `queued` 或 `failed`。
4. 失败的 Agent 任务会发一条通知；排队的任务等 Agent 下次轮询领取。
5. `last_message_id` 保持在最后一条 `done` 的位置，Hub 会重新投递未完成消息。

## 4. 已知边界

- 入站和 Agent 任务都是 at-least-once。Agent 任务最好用 `task_id` 做幂等；本地 CLI 任务重复执行的风险由 Agent 侧控制。
- 结果投递同样是 at-least-once：进程若在“发送成功、还没写 `sent`”之间崩溃，重启后可能再发一次；`dedupe_key` 只保证同一任务最多一条待发记录，不对下游做去重。
- 本地 Agent 目前不在执行期间发 heartbeat；Router 默认 lease 30 分钟。超过 30 分钟的长任务建议在 Agent 侧加 heartbeat，或调大 `TASK_LEASE_SECONDS`。
- `channel_inbound`（Koishi/QQ）目前仍按请求处理；如果桥接层会重复投递同一个 `message_id`，下一步可以复用 `claim_inbound` 做同样的去重。
- 任务表保留 30 天；`list_agent_tasks` / `agent_task_counts` 可在管理接口或 SQL 里查看。
