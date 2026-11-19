# 决策

## ADR-001 保持侧车契约

保留 HTTP 方法、路径、状态码、JSON 字段、鉴权、单并发及三项服务环境变量。保持生产镜像中的 Chromium 131.0.6778.33 和 Noto CJK 字体。Windows 用于本机对照，Linux amd64 是部署目标。

## ADR-002 复用 Rust 库和原浏览器

Axum/Tokio 提供 HTTP 与进程管理。通过本机 Chromium DevTools Protocol 执行浏览器操作，`lopdf` 只读取页树。PDF 响应使用 Chromium 原始字节。`server.js` 保留为行为基准，Rust 服务进程无需 Node.js。

## ADR-003 比较 PDF 和 HTTP 契约

状态码及稳定响应字段直接比较。对于 PDF，先验证原版跨秒重复渲染产生的波动，再逐字节比较，仅屏蔽已证实会波动的 PDF 日期元数据。另比较页数、字节数、页面尺寸和像素，验证 Content-Length。

超时响应保留 Playwright 的调用日志格式。原版在 Windows 默认带 ANSI 样式，在生产 Jammy 容器中返回纯文本。对照用例逐字节比较错误响应，包含该差异产生的 `Content-Length`。
