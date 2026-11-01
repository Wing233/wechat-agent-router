# app.py 拆分评估

`app.py` 现在约 2200 行，同时承担 FastAPI 应用、启动/关闭、入站轮询、Agent 调用、结果回传、维护循环、管理接口和渠道适配。它已经能工作，所以拆分的目标不是“行数更少”，而是让状态机和副作用边界更清楚，降低后续改动的回归面。

## 当前耦合点

- 全局单例：`CFG`、`store`、`TOOLS`、`AGENTS`、`SERVICES`、`BROKER`、`CHANNELS`、`MODELS`。
- 线程上下文：`ORIGIN` 用 `contextvars` 传递当前消息来源，`send_text` 依赖它决定回微信还是回采集器。
- FastAPI 装饰器：路由和 `app` 生命周期直接写在模块顶层，测试通过 `import app` 复用。
- 启动顺序：`store.init()` → 插件加载 → `BROKER.recover()` → 后台任务；拆模块时不能打乱。
- 隐式约定：`poll_once` 在线程里跑，`maintenance_loop` 在事件循环里跑；同步阻塞调用必须留在正确的执行上下文。

## 建议拆分顺序

### 1. 出站层（优先）

抽出 `outbound.py`：

```text
send_payload / send_text / flush_outbox / enqueue 语义
```

依赖只有 `hub`、`store`、`traces`、`ORIGIN`。边界清晰，且维护循环已经通过 `asyncio.to_thread` 调用它，适合作为第一个切口。

### 2. 维护循环

抽出 `maintenance.py`：

```text
maintenance_loop / 过期任务通知 / 定期清理
```

只依赖 `store`、`BROKER`、`send_text`、`LEETCODE`。抽完后 `app.py` 只负责启动任务。

### 3. 入站管线

抽出 `inbound.py`：

```text
poll_once / handle_inbound / _handle_inbound / match_pending
```

这是最大的一块，依赖 `governance`、`help_center`、`AGENTS`、`TOOLS`、`send_text`、`store`。建议先抽 `poll_once` 和去重/重试，保留 `_handle_inbound` 在 `app.py`，分两步走。

### 4. Agent 运行时

抽出 `agent_runtime.py`：

```text
call_agent / _call_agent_impl / send_agent_result / 结果投递
```

依赖 `AGENTS`、`BROKER`、`store`、`send_text`。任务状态已经持久化，抽出的风险主要是循环导入。

### 5. HTTP 路由

最后再按领域拆 `routes/health.py`、`routes/agent.py`、`routes/admin.py`。FastAPI 的 `app` 仍由 `app.py` 创建并 `include_router`，避免测试和部署入口变化。

## 拆分前提

1. 先引入一个显式的应用上下文对象，集中持有 `cfg/store/tools/agents/services/broker/send_text`，避免每个模块再 `import app`。
2. 保持 `app:app` 这个 ASGI 入口不变；Docker、Compose、测试都依赖它。
3. 每次只搬一个模块，搬完跑 `python -m pytest -q tests`，不合并多个切口。
4. 不引入新的依赖注入框架，不做“大爆炸”重写。
5. 不在拆分中改变行为；行为变更单独提交。

## 非目标

- 不为了行数把强相关的入站判定拆成很多小文件。
- 不把 `governance` 的确定性判定搬进模型提示词。
- 不改变 `host` / `isolated` 执行器语义。
- 不改变 `app.py` 的启动顺序和后台任务数量。

## 当前建议

先做第 1、2 步（出站层 + 维护循环），观察一个版本；稳定后再碰入站管线。第 3 步之前，先补一个“入站消息在重试/崩溃恢复下不重复推进 `last_message_id`”的回归测试。
