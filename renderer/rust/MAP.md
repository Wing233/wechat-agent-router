# wechat-resume-render

目标是用独立 Rust HTTP 服务替换 `server.js` 的运行入口。原版作为回放基准。

| 单元 | 原入口 | 范围 | 状态 |
| --- | --- | --- | --- |
| `resume-render-service` | `server.js` | 路由、鉴权、单并发、URL 白名单、Chromium 渲染、PDF 计页 | 迁移完成，已通过应用回放和独立对照 |

- [决策](DECISIONS.md)
- [依赖调研](research)
- [对照工具](verification)
- [构建与验收记录](../README.md)

Linux amd64 release 已完成编译。正式 Jammy 运行环境中，45/45 条 HTTP 用例、11 份 PDF 和并发用例均与原版一致。
