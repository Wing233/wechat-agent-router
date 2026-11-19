# wechat-resume-render

Rust HTTP 侧车，使用 Chromium 131.0.6778.33 打印 LYPH 简历页面。容器继续使用 Playwright 1.49.1 Jammy 的 Chromium 和 Noto CJK 字体。`Dockerfile` 构建并启动 Rust 服务，`Dockerfile.node-reference` 保留原镜像的构建方式。

| 环境变量 | 默认值 |
| --- | --- |
| `PORT` | `9801` |
| `RENDER_TOKEN` | 空，拒绝渲染请求 |
| `ALLOWED_ORIGINS` | `https://lyph-nginx-1:8443,http://web:3000` |
| `PLAYWRIGHT_BROWSERS_PATH` | Linux 使用 `/ms-playwright`，Windows 使用 `%LOCALAPPDATA%\ms-playwright` |
| `CHROMIUM_EXECUTABLE` | 上述浏览器目录内的 Chromium headless shell `1148` |

`GET /health` 报告忙闲状态。`POST /render` 接受带 `X-Render-Token` 的 JSON 请求。请求体的 `url` 指向允许来源上的 `/print/resume/` 页面，可用 `timeout_ms` 设置导航超时。单并发，页面高度超过 1130 CSS 像素时返回 `422`。成功响应包含 `overflow`、`height`、`pages`、`bytes` 和 `pdf_base64`。

## 构建与验证

在此目录构建二进制或容器。

```powershell
cargo build --release --locked --manifest-path rust/app/Cargo.toml
docker build -t wechat-resume-render:dev .
```

对照验证使用原版 `server.js`、Node.js、Python 和 PyMuPDF。第一次验证时执行 `npm install --no-package-lock`、`node node_modules/playwright-core/cli.js install chromium --only-shell`。如果已安装 Chromium `1148`，可直接设置 `PLAYWRIGHT_BROWSERS_PATH`。Windows 本机的复验命令如下。

```powershell
python rust/verification/verify.py replay rust/app/target/release/wechat-resume-render.exe
python rust/verification/verify.py --isolated compare rust/app/target/release/wechat-resume-render.exe
```

回放使用 `rust-anything` 记录的合成请求。独立对照启动两套服务，Rust 在空工作目录和空 `PATH` 下运行。PDF 对照依次验证原版跨秒变化、除两个日期字段外的全部字节、页数、文字、尺寸与栅格像素。测试过程只访问本机合成打印页。

## 验收记录

- `rust-anything app lift` 通过，过渡入口回退率为 0，最终入口已移除回退路径。
- Windows 上最终二进制通过 32/32 条 HTTP 回放。另有 45 条独立 HTTP 对照，其中 11 条比较完整 PDF。并发渲染时的 `busy`、`429`、鉴权顺序和忙闲恢复也与原版一致。
- 使用 Chromium `131.0.6778.33`。原版跨秒重复打印仅有 PDF `CreationDate` 和 `ModDate` 变化。屏蔽这两个日期后，11 份 PDF 的所有字节一致，页数、文字、页面尺寸与像素也一致。
- Linux amd64 release 编译通过。二进制在 Alpine 构建容器中监听默认端口 `9801`，与原版对照的 30 条非浏览器 HTTP 用例全部一致。
- Linux Jammy 容器中，45/45 条 HTTP 用例和并发用例通过。11 份 PDF 除生成日期外字节一致，页数和页面高度一致。对照覆盖了 Linux 原版超时响应使用纯文本的行为。

## 回退镜像

按原 Dockerfile 重新构建以回退部署镜像。

```powershell
docker build -f Dockerfile.node-reference -t wechat-resume-render:node-reference .
```
