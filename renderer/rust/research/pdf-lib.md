# PDF 页数依赖复用检查

## 能力需求

- 从内存读取 Chromium 生成的 PDF，遍历页树并返回实际页数。
- 保持接口中的 `bytes` 和 `pdf_base64` 来自同一份原始 PDF。
- 在同一 Chromium、操作系统和字体环境下，对照验证页数、页面尺寸和文件字节。

`server.js` 只用 `pdf-lib@1.17.1` 载入浏览器返回的缓冲区和遍历页树，最终返回的是原始缓冲区。`getPageCount()` 取 `getPages().length`。Router 当前的调用链不含 Rust PDF 解析器。[pdf-lib 对应版本源码](https://github.com/Hopding/pdf-lib/blob/v1.17.1/src/api/PDFDocument.ts)

## 候选清单

| 名称 | 星数 | 活跃度证据 | 许可证 | 匹配度 |
| --- | ---: | --- | --- | --- |
| [lopdf](https://github.com/J-F-Liu/lopdf) | 约 2.3k | 未归档，2026-11-04 发布 [0.47.0](https://github.com/J-F-Liu/lopdf/releases/tag/v0.47.0)，仓库页面显示发布后继续提交 | MIT | 高 |
| [pdf-rs/pdf](https://github.com/pdf-rs/pdf) | 约 1.7k | 未归档，查到 [2026-06-06 的提交](https://github.com/pdf-rs/pdf/commits/master/) | MIT | 中 |

## 深度裁决

- `lopdf` 直接支持 `Document::load_mem(&pdf)` 和 `page_iter().count()`。`get_pages()` 也遍历页树，但会构建页号到对象 ID 的映射。0.47.0 声明 Rust 1.88，基本内存解析无需 `chrono-clock` 或 `rayon`，可以禁用默认特性。[Document API](https://docs.rs/lopdf/0.47.0/lopdf/struct.Document.html) · [0.47.0 Cargo.toml](https://github.com/J-F-Liu/lopdf/blob/v0.47.0/Cargo.toml)
- `pdf` 可从内存解析并枚举页，但其 `pages()` 返回有错误分支的迭代器，使用时需要核对每一页的错误行为。对 Chromium 自产 PDF 的单一用途，`lopdf` 的内存加载和页树迭代更直接。[pdf 0.10.0 File API](https://docs.rs/pdf/0.10.0/pdf/file/struct.File.html)

## 裁决

组装现有库。复用 Chromium 生成 PDF，用 `lopdf` 读取页数，用标准 Base64 编码原始缓冲区。该调用域限定为 Chromium 生成的 PDF，`pdf-lib` 与 `lopdf` 对任意坏文件或加密文件的解析错误不在此兼容保证内。现有 API 足以覆盖本用途，无需 fork。

## 动手清单

- 直接以 Chromium 结果填充 `bytes` 和 `pdf_base64`。Rust 解析器只读取页数，避免重新序列化引入额外变动。
- 复用 Playwright 1.49.1 对应的 Chromium 131.0.6778.33（headless shell 修订号 1148）。两侧在相同平台、浏览器参数和字体前提下渲染同一批静态用例，包含中文和采用 CSS `@page` 的多页文档。[浏览器版本清单](https://github.com/microsoft/playwright/blob/v1.49.1/packages/playwright-core/browsers.json)
- 先让旧实现跨秒渲染同一输入至少两次，找出自身漂移。Chromium 131 在创建 PDF 时用同一墙钟时间填充 `fCreation`、`fModified`。Skia 将这两项写入 `/CreationDate` 和 `/ModDate`。[Chromium 131 打印源码](https://github.com/chromium/chromium/blob/131.0.6778.33/printing/common/metafile_utils.cc#L222-L233) · [Skia PDF 元数据写入源码](https://chromium.googlesource.com/experimental/external/gob/skia/skia/+/d2b9e48baf1697760afc1dc8ea3ad40110b8cacc/src/pdf/SkPDFMetadata.cpp)
- 对原始响应验证 `Content-Length` 等于 JSON 的字节长度、严格解码 `pdf_base64`、`bytes` 等于解码后的字节长度、`pages` 等于解析的页数。验证 PDF 差分时，仅在原版两次执行确实观测到变化、且日期字段格式有效的情况下，对 Info 字典的两个日期值做等宽替换，然后逐字节比较整个文件。记录所用归一化与对照结果，测试之外继续返回 Chromium 的原始时间。由于 `rust-anything` 1.4.0 HTTP 回放会跳过 `Content-Length` 等传输头，PDF 和长度断言应独立执行。
- 若日期外仍有差分，保留样本并找出差分对象。可额外使用同一版本的 `pdfinfo` 比较页尺寸，使用 `pdftoppm` 比较每页光栅图像，并核对文本和链接等非像素信息。像素或布局一致的结论仅覆盖这些维度。当前 Windows 测试机已检测到 `pdfinfo` 和 `pdftoppm`。
