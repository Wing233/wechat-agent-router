# 站点数据示例插件

这是一个**可选**插件，演示如何让微桥读取任意站点自己的 SQLite 数据。

它不在默认 `plugins/` 目录里，所以不会被自动加载。需要时复制过去：

```bash
cp -r examples/plugins/site plugins/site
```

准备数据库：

```bash
sqlite3 data/site.db < examples/plugins/site/schema.sql
sqlite3 data/site.db < examples/plugins/site/seed.sql
```

在 `config.json` 里配置路径：

```json
{
  "plugins": {
    "site": {
      "db": "/data/site.db"
    }
  }
}
```

重载插件：

```bash
curl -X POST -H "X-Service-Token: $SERVICE_TOKEN" \
  http://127.0.0.1:9900/internal/plugins/site/reload
```

然后在微信里问：

```text
站点最近 7 天访问量怎么样？
最近有哪些 AI 对话？
```

## 工具

| 工具 | 说明 |
|---|---|
| `site__visit_stats` | PV/UV，按天或按路径 |
| `site__chat_messages` | 最近的问答，可按关键词 |
| `site__chat_stats` | 对话数量趋势 |

## 表结构

见 [`schema.sql`](schema.sql)：

```sql
visits(id, path, visitor_id, created_at)
chat_messages(id, question, answer, created_at)
```

`created_at` 使用 ISO 8601 字符串（例如 `2026-10-30T09:00:00+08:00`）。

## 说明

- 这个插件只读，不会写站点库
- 数据库路径由 `plugins.site.db` 控制
- 如果你的表名/字段不同，直接改 `plugin.py`
- 不要把真实站点数据库提交到 git
