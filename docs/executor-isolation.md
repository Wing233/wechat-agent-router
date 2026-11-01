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
| `host`（默认） | 保持现状：`python_repl`、`run_sandbox`、`devtools run_command` 在 Router 容器内执行，能访问 `/app`、`/data`、`/lyph-src` 等挂载。 |
| `isolated` | Python/Shell 发到独立 executor 容器；只挂 `/workspace`，非 root、只读根文件系统、无 `/data`、无源码挂载、无外网。执行器不可用时直接报错，**不会回退到 host**。 |

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
docker restart wechat-agent-router
```

5. 验证：

```text
python_repl("import os; print(os.getuid())")   -> 10001
python_repl("open('/data/router.db')")         -> 失败（没有 /data）
devtools run_command("ls /data")               -> 失败
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

## 还没隔离的部分

这次只把 **Python/Shell 执行** 切到 executor。以下工具仍在 Router 容器内：

```text
write_patch
ops__fs_write / fs_replace / fs_delete / fs_move
snapshot_checkpoint restore
admin__config_set
model_add / model_remove / model_bind / model_switch_chat
send_to_channel / webhook / slack / mcp_call
```

所以 `isolated` 不是完整的“安全多租户”方案。对外部署时，建议继续把文件写入、配置修改和模型管理拆到独立服务，或者给 Router 本身做非 root + 最小挂载。

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
