# 复用检查报告 · 通过 Rust 控制简历打印所用的 Chromium

## 能力需求

- 沿用 `playwright-core@1.49.1` 的 Chromium headless shell `1148`（`131.0.6778.33`），保留原镜像的 Jammy、浏览器系统依赖和 Noto CJK 字体。原镜像安装目录为 `/ms-playwright`，Linux 二进制为 `/ms-playwright/chromium_headless_shell-1148/chrome-linux/headless_shell`。Windows 的发行文件将二进制放在 `chrome-win/headless_shell.exe`。 [Playwright 浏览器清单](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/browsers.json) · [可执行文件定位规则](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/registry/index.ts#L48-L58) · [官方 Jammy 镜像定义](https://github.com/microsoft/playwright/blob/v1.49.1/utils/docker/Dockerfile.jammy)
- 复现新浏览器上下文、页面请求的过滤和完成条件、实际屏幕参数、字体和打印设置。服务启动时沿用延迟创建一个浏览器，每次调用创建新的页面和上下文。

## 候选清单

| 名称 | 星数 | 活跃度依据 | 许可证 | 匹配度 |
| --- | --- | --- | --- | --- |
| [tokio-tungstenite](https://github.com/snapview/tokio-tungstenite) 与 Chromium CDP | 约 2.5k | 非归档，维护者发布了 0.30.0 | MIT | 提供传输，事件和命令可以固定在 Chromium 131 所需子集 |
| [chromiumoxide](https://github.com/mattsse/chromiumoxide) | 约 1.4k | 非归档，0.9.1 发布于 2026-02-25 | MIT 或 Apache-2.0 | 可启动固定浏览器、管理上下文和发送 CDP 命令，需要自行对照 Playwright 的等待与拦截语义 |

## 裁决

复用 Chromium headless shell、WebSocket 与 JSON 库，由 Rust 调用 CDP。`chromiumoxide` 可作为 CDP 参考，`0.9.x` 提供指定 executable、关闭默认启动参数、设置视口及拦截的配置入口。其更新日志显示 CDP 已跟随更高 Chromium revision。其 `NetworkManager` 在收到响应头时即可将请求交给 `RequestFinished`，所以不能据此直接实现 Playwright 的 500 ms 空闲判定。处理重定向、请求时间和输出参数仍需项目定义。使用 `tokio-tungstenite` 时只维护所需的 CDP 命令及事件，连接限制在回环地址和临时调试端口。 [chromiumoxide 配置 API](https://docs.rs/chromiumoxide/0.9.1/chromiumoxide/browser/struct.BrowserConfigBuilder.html) · [chromiumoxide 更新日志](https://github.com/mattsse/chromiumoxide/blob/main/CHANGELOG.md) · [chromiumoxide 网络事件实现](https://github.com/mattsse/chromiumoxide/blob/v0.9.1/src/handler/network.rs#L199-L214) · [tokio-tungstenite 更新日志](https://github.com/snapview/tokio-tungstenite/blob/master/CHANGELOG.md)

## 迁移要核对的行为

### 启动与上下文

- 默认开关由 Playwright 1.49.1 的 `chromiumSwitches` 定义。随后启用 `--headless`、隐藏滚动条、关闭声音、提供桌面指针的 Blink 设置、追加 `--no-sandbox` 与应用的显式参数。`--user-data-dir` 指向临时目录。Playwright 默认使用调试 pipe，也实现了通过端口 `0` 启动并解析浏览器 WebSocket 地址。连接应保留会话 ID 与命令 ID 的区分。对每次渲染建立独立上下文和页面，结束时关闭页面并处置上下文。 [默认开关](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/chromiumSwitches.ts#L17-L58) · [参数与调试连接](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/chromium.ts#L253-L335) · [CDP Target](https://chromedevtools.github.io/devtools-protocol/tot/Target/)
- 页面会话先启用 Network、Page 生命周期和 Runtime。`Security.setIgnoreCertificateErrors` 设为 `true`。`Emulation.setDeviceMetricsOverride` 使用 794 × 1122 的视口和屏幕、比例 `1`、`mobile: false` 和桌面方向 `landscapePrimary` / `0`。默认页面还启用焦点模拟，并设置默认媒体偏好。跨进程 iframe 及其请求出现在独立会话，需要同时处理自动挂接和子目标恢复。可参照源实现逐项复用。 [页面会话初始化](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/crPage.ts#L468-L540) · [视口与媒体实现](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/crPage.ts#L908-L980)
- `Page.setFontFamilies` 会在 headless 会话初始化时配置默认字体。Linux 和 Windows 的通用字体映射均使用 Times New Roman（`standard`、`serif`）、Arial（`sansSerif`）、Comic Sans MS（`cursive`）和 Impact（`fantasy`）。`fixed` 在 Linux 为 `Monospace`，在 Windows 为 `Consolas`。Windows 还提供以下 `forScripts` 映射。 [页面字体设置](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/crPage.ts#L990-L993) · [已核对的 1.49.1 发布包](https://registry.npmjs.org/playwright-core/-/playwright-core-1.49.1.tgz) 中的 `package/lib/server/chromium/defaultFontFamilies.js`。

| script | standard | fixed | serif | sansSerif | cursive |
| --- | --- | --- | --- | --- | --- |
| `cyrl` | Times New Roman | Courier New | Times New Roman | Arial | |
| `arab` | | Courier New | | Segoe UI | |
| `grek` | Times New Roman | Courier New | Times New Roman | Arial | |
| `jpan` | `,Meiryo,Yu Gothic` | MS Gothic | `,Yu Mincho,MS PMincho` | `,Meiryo,Yu Gothic` | |
| `hang` | Malgun Gothic | Gulimche | Batang | Malgun Gothic | Gungsuh |
| `hans` | Microsoft YaHei | NSimsun | Simsun | Microsoft YaHei | KaiTi |
| `hant` | Microsoft JhengHei | MingLiU | PMingLiU | Microsoft JhengHei | DFKai-SB |

### 请求、空闲与导航结果

- `page.route` 启用后禁用缓存，并在请求阶段发送 `Fetch.enable`。Playwright 配对 `Network.requestWillBeSent` 与 `Fetch.requestPaused`，两条事件可能以任意顺序到达。依据 URL 通过或拒绝请求，`route.abort()` 的默认 CDP 错误原因是 `Failed`。Playwright 自身还处理没有 `networkId` 的事件、favicon、鉴权、浏览器内部重试以及 CORS 预检。 [拦截配置和事件配对](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/crNetworkManager.ts#L129-L250) · [abort 实现](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/crNetworkManager.ts#L591-L640)
- 1.49.1 只把重定向链的首个请求传给应用的 `page.route` 处理器，之后的请求由其 CDP 网络层处理。这决定首跳与后续请求的白名单表现。导航结果绑定主 frame 的文档请求及其最终响应。 [重定向处理](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/crNetworkManager.ts#L253-L319) · [最终导航响应](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/frames.ts#L604-L655)
- `networkidle` 的等待从浏览器请求生命周期得出。Playwright 按 frame 跟踪在途请求，忽略 favicon，收到请求结束或失败时清除，并在每个 frame 连续 500 ms 无请求后聚合整棵 frame 子树。新文档提交时重置生命周期。建议录制加载缓慢的子资源、iframe、阻断的资源和最终 HTTP 错误。 [请求跟踪](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/frames.ts#L353-L369) · [子树聚合](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/frames.ts#L551-L570) · [500 ms 时钟](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/frames.ts#L1551-L1568)
- 导航期限覆盖 `Page.navigate` 和后续等待，不延伸到 `document.fonts.ready` 或打印。保留 Playwright 对 `Page.navigate` 错误的包装、超时毫秒数和调用日志中的 URL。上游正常完成且状态 ≥ 400 时由应用返回 `502`。 [CDP 导航](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/chromium/crPage.ts#L553-L565) · [导航期限和调用日志](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/src/server/frames.ts#L596-L606)

### 打印与验证

- 先在主 frame 执行 `Runtime.evaluate` 等待 `document.fonts.ready`，再执行原选择器和高度计算。`Page.printToPDF` 的参数为 A4 8.27 × 11.7 英寸、四边距为 0、缩放为 1、背景开启、CSS 页面尺寸优先、页眉页脚关闭，`generateTaggedPDF` 和 `generateDocumentOutline` 均为 `false`。1.49.1 通过 `ReturnAsStream` 读取数据。应保留浏览器生成的 PDF 原始字节来计算 `bytes` 并编码 `pdf_base64`。 [CDP 打印接口](https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-printToPDF) · [已核对的 1.49.1 发布包](https://registry.npmjs.org/playwright-core/-/playwright-core-1.49.1.tgz) 中的 `package/lib/server/chromium/crPdf.js`。
- Chromium 会将当前时间写入 PDF 创建和修改时间。对原实现先做两次跨秒录制，验证原始字节差异的位置。对候选验证 PDF 页数、尺寸、文字、页面栅格结果、`bytes` 与解码长度，并对除已观察到的可变字段之外的字节比较。验证 Windows 和 Jammy 时分别在同一平台和同一 Chromium 版本上运行两套服务。 [Chromium 131 PDF 创建时间实现](https://github.com/chromium/chromium/blob/131.0.6778.33/printing/common/metafile_utils.cc#L221-L234)
