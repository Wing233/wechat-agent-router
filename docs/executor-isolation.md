# 可选执行器隔离

默认模式是 `host`：Python/Shell 仍在 Router 容器里执行，保留个人使用需要的高权限。

如果准备给别人部署，或者想降低误操作/提示注入的影响范围，可以切到 `isolated`：

```json
"executor": {
  "mode": "isolated",
  "url": "http://executor:9800",
  "token": "REPLACE_EXECUTOR_TOKEN"
}
```

## 两种模式

| 模式 | 行为 |
|---|---|
| `host`（默认） | 保持现状：`python_repl`、`run_sandbox`、`devtools run_command`、文件写入/补丁在 Router 容器内执行，能访问 `/app`、`/data`、`/lyph-src` 等挂载。 |
| `isolated` | Python/Shell、`write_patch`、`ops__fs_*` 都发到独立 executor 容器；只挂 `/workspace`，非 root、只读根文件系统、无 `/data`、无源码挂载、无外网。执行器不可用时直接报错，**不会回退到 host**。 |

## 启用步骤

1. 生成 token：

```bash
openssl rand -hex 32
```

2. 写入 Router 配置：

```json
"executor": {
  "mode": "isolated",
  "url": "http://executor:9800",
  "token": "刚才生成的 token"
}
```

3. 让 executor 用户能写 workspace：

```bash
sudo chown -R 10001:10001 /opt/wechat-agent-router/workspace
```

4. 启动 executor（compose 里是可选 profile）：

```bash
cd /opt/wechat-agent-router
EXECUTOR_TOKEN='刚才生成的 token' \
  docker compose --profile isolated-executor up -d executor
# 让 router 加入 executor 内部网络；必须重建容器，restart 不会加网络
docker compose up -d --force-recreate router
```

5. 验证：

```text
python_repl("import os; print(os.getuid())")   -> 10001
python_repl("open('/data/router.db')")         -> 失败（没有 /data）
devtools run_command("ls /data")               -> 失败
ops__fs_write("/workspace/test.txt", "hi")     -> 成功
ops__fs_write("/app/test.txt", "hi")           -> 拒绝
write_patch(cwd="/workspace")                  -> 在 /workspace 应用
```

## executor 的安全边界

compose 中的 executor：

```yaml
user: "10001:10001"
read_only: true
tmpfs: ["/tmp"]
volumes:
  - ./executor:/app/executor:ro
  - ./workspace:/workspace
cap_drop: [ALL]
security_opt: ["no-new-privileges:true"]
mem_limit: 512m
cpus: 1
networks: [executor]   # internal: true，无外网
```

- 没有 `/data`，读不到 Router/工具/审批数据库和模型密钥。
- 没有 `/opt/lyph`、`/lyph-data`、git 仓库挂载。
- 根文件系统只读，只有 `/workspace` 和 `/tmp` 可写。
- 非 root（10001），drop 全部 capabilities。
- 无外网；`pip install`、外部 API 调用在隔离模式下不可用。

## 控制面工具

`isolated` 模式默认拒绝以下工具（不会执行），避免模型绕过执行器碰 Router 的 `/data`、模型配置或外发通道：

```text
admin__config_set / enable_plugin / disable_plugin / reload_plugin / set_job_*
model_add / model_remove / model_bind / model_switch_chat
budget_set / optimize_apply
send_to_channel / send_media / generate_image
webhook_post / slack_post / mcp_call / browser_click_fill
agents__send_to_agent / toolbox__delegate_to_subagent
toolbox__github_api / toolbox__sql_query
trace_get / trace_search / trace_explain / insight_report
toolbox__snapshot_checkpoint / memory_update / learn_skill
toolbox__list_mandates / revoke_mandate / governance_report
site__set_preset_questions
```

需要时可以在配置里显式放开：

```json
"executor": {
  "mode": "isolated",
  "allow_control_plane": true
}
```

默认 `false`。个人高权限使用建议直接用 `mode=host`，不要把 `isolated` 和 `allow_control_plane=true` 当成默认组合。

`write_patch`、`ops__fs_*` 在 isolated 模式下仍然可用，但只允许 `/workspace`；`snapshot_checkpoint` 暂不支持 isolated，会直接报错。

## 故障行为

- `mode=isolated` 且 `executor.url` 为空：工具返回“隔离执行器不可用”，不执行。
- executor 不可达/401/超时：工具返回错误，不回退 host。
- token 不匹配：executor 返回 401。
- `EXECUTOR_TOKEN` 为空：executor 拒绝所有执行请求，但 `/health` 仍可用于排查。
- executor 的 `cwd` 只允许 `/workspace` 及其子目录；`/app`、`/data` 会被 400 拒绝。

## 测试

```bash
python -m pytest -q tests/test_executor_client.py tests/test_executor_server.py
```

覆盖：host 模式关闭、isolated 请求体/token、缺 URL fail-closed、executor 认证、Python 执行、`/workspace` 之外的 cwd 拒绝、Linux shell（Windows 跳过）。
