# 插件开发

插件是微桥的主要扩展方式。一个插件就是一个目录：

```text
plugins/
  hello/
    manifest.json
    plugin.py
    __init__.py
```

## manifest

```json
{
  "id": "hello",
  "name": "Hello",
  "version": "1.0.0",
  "api_version": "1",
  "entry": "plugin.py",
  "capabilities": ["tools"],
  "permissions": ["send:wechat"],
  "secrets": [],
  "description": "最小示例插件"
}
```

| 字段 | 说明 |
|---|---|
| `id` | `^[a-z][a-z0-9_]{1,63}$`，必须和目录名一致 |
| `api_version` | 当前固定 `"1"` |
| `entry` | 入口文件，必须在插件目录内 |
| `capabilities` | `tools` / `service` / `credential` / `jobs` |
| `permissions` | 资源与网络权限 |
| `secrets` | 需要的密钥名 |
| `description` | 给管理台和 LLM 看 |

## 最小插件

```python
def apply(ctx) -> None:
    def hello(args: dict) -> dict:
        name = str(args.get("name") or "世界")
        return {"message": f"你好，{name}"}

    ctx.tools.register(
        "hello",
        "向用户问好",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
        hello,
    )
```

注册后的工具名是 `hello__hello`，同时保留别名 `hello`。

## 工具

```python
ctx.tools.register(
    name: str,
    description: str,
    parameters: dict,       # JSON Schema object
    handler: Callable[[dict], Any],
    aliases: tuple[str, ...] = (),
)
```

- 工具名只允许 `[A-Za-z0-9_-]`
- 异常会被捕获并转成 `{"error": "..."}`
- 结果会 JSON 序列化后回填给 LLM

## 定时任务

```python
from registry import Job

ctx.jobs.register(Job(
    id="hello_tick",
    interval_minutes=30,
    handler=lambda: {"ok": True},
    enabled=True,
    skip_empty=False,
    window_minutes=0,
    meta={"type": "service", "plugin": "hello"},
))
```

任务状态持久化在 `state` 表（`job:<id>:next/last_run/last_status`）。

## 服务

```python
class HelloService:
    def handle_reply(self, notification: dict, text: str) -> dict:
        return {"text": "收到", "final": True}

ctx.provide("hello", HelloService())
```

其他插件可以通过 `ctx.resource("hello")` 拿到服务（需要资源在 kernel 注册）。

## 密钥

manifest 声明：

```json
"secrets": ["hello_token"]
```

使用：

```python
token = ctx.secret("hello_token")
```

密钥来自 kernel 的 `secrets` 字典，不会写日志。

## 权限

| 调用 | 需要 |
|---|---|
| `ctx.resource("store")` | `db:router` |
| `ctx.resource("agents")` | `agent:read` |
| `ctx.resource("call_agent")` | `agent:call` |
| `ctx.resource("settings")` | `admin` |
| `ctx.send(text)` | `send:wechat` |
| `ctx.http_request(url)` | `net:<hostname>` |
| `ctx.login_url(service)` | capability `credential` |

`net:` 只匹配 hostname，不匹配端口；请求前请确认目标。

## 生命周期

| 方法 | 说明 |
|---|---|
| `apply(ctx)` | 加载时调用 |
| `ctx.on_unload(fn)` | 卸载时清理 |
| `ctx.provide(name, service)` | 暴露服务 |
| `ctx.log` | 插件 logger |

支持热重载：

```bash
curl -X POST -H "X-Service-Token: $TOKEN" \
  http://127.0.0.1:9900/internal/plugins/hello/reload
```

## 现有插件

| 插件 | 说明 |
|---|---|
| `admin` | 自省、配置、任务、插件 |
| `agents` | 把请求转给本机 Agent |
| `reminders` | 一次性 / 间隔 / cron 提醒 |
| `todo` | 待办清单 |
| `ops` | 审批制文件操作 |
| `leetcode` | 力扣数据与 Cookie |
| `deepseek_balance` | DeepSeek 余额 |
| `evolution` | 生成 HTTP 连接器插件 |
| `demo_time` | 时间示例 |
| `demo_weather` | 天气示例 |

## 测试插件

```python
# plugins/hello/test_plugin.py
class Ctx:
    class tools:
        @staticmethod
        def register(*args, **kwargs):
            print("registered", args[0])

    class log:
        @staticmethod
        def info(*args):
            pass

    def on_unload(self, fn):
        pass

from .plugin import apply
apply(Ctx())
```

运行：

```bash
python -m plugins.hello.test_plugin
```

## 安全清单

- 只申请需要的权限
- 不把密钥写进代码/日志
- 外部输入必须校验
- 文件路径必须限制在允许根目录
- 网络请求只访问声明的 host
- 生成代码/安装插件必须走审批
