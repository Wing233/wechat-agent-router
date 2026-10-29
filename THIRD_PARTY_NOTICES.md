# Third-Party Notices

微桥使用或参考了以下开源项目。各项目的版权归其作者所有，遵循各自许可证。

## 运行时依赖

| 项目 | 许可证 | 用途 |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | HTTP API |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | 数据模型 |
| [APScheduler](https://github.com/agronholm/apscheduler) | MIT | 定时任务 |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 / BSD-3-Clause | Fernet 加密 |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | PDF 文本提取 |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT | DOCX 文本提取 |
| [tzdata](https://github.com/python/tzdata) | Apache-2.0 | 时区数据 |

## 桌面端

| 项目 | 许可证 | 用途 |
|---|---|---|
| [PyWebView](https://github.com/r0x0r/pywebview) | BSD-3-Clause | 桌面 WebView 壳 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0-or-later with exception | 打包 exe |
| [pythonnet](https://github.com/pythonnet/pythonnet) | MIT | PyWebView 的 .NET 互操作 |
| [clr-loader](https://github.com/pythonnet/clr-loader) | MIT | .NET 运行时加载 |
| [Bottle](https://github.com/bottlepy/bottle) | MIT | PyWebView 内置 HTTP server |
| Microsoft Edge WebView2 | Microsoft 许可 | Windows WebView 运行时 |

> 如果你分发打包后的 exe，请同时遵守 PyInstaller、PyWebView、WebView2 的许可条款。

## 图标

桌面端图标为内联 SVG，风格参考 [Feather Icons](https://github.com/feathericons/feather)（MIT，Copyright © Cole Bemis）。

## 外部服务

以下服务不随本项目分发，使用时需遵守各自条款：

- [OpeniLink Hub](https://github.com/openilink/openilink-hub) — 微信通道
- [DeepSeek](https://www.deepseek.com/) — LLM
- [SiliconFlow](https://www.siliconflow.cn/) — 文生图
- [LeetCode](https://leetcode.cn/) — 做题数据（可选插件）
- [Open-Meteo](https://open-meteo.com/) — 天气示例
- [httpbin](https://httpbin.org/) — 时间示例

## 字体

README 与宣传图使用系统字体（Microsoft YaHei / Segoe UI）渲染，不随仓库分发字体文件。
