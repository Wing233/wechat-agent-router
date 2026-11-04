"""本机屏幕文字 OCR：像素绝不出本机。

抓屏只在内存里完成：用 pywin32 的 GDI BitBlt 把主屏拷进内存 bitmap，
转成 WinRT SoftwareBitmap 后交给 winsdk 的 Windows.Media.Ocr 识别文字。
识别结果只以纯文字形态返回；bitmap 用完即弃，绝不写 PNG/图片到磁盘、
绝不上传、绝不入库图片，也不调用任何云端 vision。

整条链路包在 try/except 里：winsdk 未安装、无中文语言包、WinRT 初始化
失败或超时等任何异常都降级为空串，绝不把异常抛进 daemon 主循环。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import worklog_collector as collector  # noqa: E402

# 跳过抓屏时的返回占位符：无文字内容，调用方据此跳过入库。
SKIP_PLACEHOLDER = ""

_LOCK_APPS = {"lockapp.exe", "logonui.exe"}


def should_skip_frame(app: str, title: str, idle: int, config: dict) -> bool:
    """帧级判定：命中浏览器 / 敏感词 / 锁屏 / 空闲超时 → 跳过抓屏。"""
    idle_limit = max(1, int(config.get("idle_skip_seconds") or 300))
    if int(idle or 0) >= idle_limit:
        return True
    app_lower = str(app or "").lower()
    if app_lower in _LOCK_APPS:
        return True
    browsers = {str(item).lower() for item in config.get("browser_apps") or []}
    if app_lower and app_lower in browsers:
        return True
    lower = str(title or "").lower()
    if any(str(p).lower() in lower for p in config.get("redact_patterns") or []):
        return True
    return False


def redact_ocr_text(text: str, config: dict) -> str:
    """逐行脱敏：命中敏感词整行替换，再走 sanitize_text；不套 120 截断。"""
    patterns = [str(p).lower() for p in config.get("redact_patterns") or []]
    kept: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(p in line.lower() for p in patterns):
            kept.append("[敏感内容已隐藏]")
        else:
            kept.append(line)
    return collector.sanitize_text("\n".join(kept))


def _grab_bitmap():
    """GDI BitBlt 抓主屏到内存 bitmap，返回 (宽, 高, 自上而下 BGRA 字节)。

    像素只在内存中流转，不写任何文件、不做任何网络调用。
    """
    import win32con
    import win32gui
    import win32ui

    desktop = win32gui.GetDesktopWindow()
    width = win32gui.GetSystemMetrics(win32con.SM_CXSCREEN)
    height = win32gui.GetSystemMetrics(win32con.SM_CYSCREEN)
    if width <= 0 or height <= 0:
        return None
    window_dc = win32gui.GetWindowDC(desktop)
    src_dc = win32ui.CreateDCFromHandle(window_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(src_dc, width, height)
    mem_dc.SelectObject(bitmap)
    try:
        mem_dc.BitBlt((0, 0), (width, height), src_dc, (0, 0), win32con.SRCCOPY)
        raw = bitmap.GetBitmapBits(True)
    finally:
        mem_dc.DeleteDC()
        src_dc.DeleteDC()
        win32gui.ReleaseDC(desktop, window_dc)
    if isinstance(raw, str):  # 旧版 pywin32 可能返回 str
        raw = raw.encode("latin-1", "ignore")
    raw = bytes(raw)
    stride = width * 4
    rows = [raw[i * stride:(i + 1) * stride] for i in range(height)]
    return width, height, b"".join(reversed(rows))  # DIB 自下而上 → 翻成自上而下


async def _recognize(pixels: bytes, width: int, height: int, language: str) -> str:
    """把内存像素喂给 WinRT OCR；IAsyncOperation 由 asyncio await 驱动。"""
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter

    writer = DataWriter()
    writer.write_bytes(pixels)
    buffer = writer.detach_buffer()
    software_bitmap = SoftwareBitmap.create_copy_from_buffer(
        buffer, BitmapPixelFormat.BGRA8, width, height)
    engine = OcrEngine.try_create_from_language(Language(language))
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return ""
    result = await engine.recognize_async(software_bitmap)
    return str(result.text or "")


def _ocr_bitmap(bitmap, language: str) -> str:
    if not bitmap:
        return ""
    width, height, pixels = bitmap

    async def _drive():
        return await asyncio.wait_for(
            _recognize(pixels, width, height, language), timeout=10)

    return asyncio.run(_drive())


def capture_screen_text(config: dict) -> str:
    """抓主屏 → 本机 OCR → 纯文字。跳过或任何异常都返回空串，绝不外抛。"""
    try:
        if not bool(config.get("screenshot_enabled", True)):
            return SKIP_PLACEHOLDER
        app, title = collector.foreground()
        idle = collector.idle_seconds()
        if should_skip_frame(app, title, idle, config):
            logging.debug("本地 OCR：帧级判定跳过（app=%s）", app)
            return SKIP_PLACEHOLDER
        bitmap = _grab_bitmap()
        if not bitmap:
            return SKIP_PLACEHOLDER
        text = _ocr_bitmap(bitmap, str(config.get("ocr_language") or "zh-CN"))
        return text or SKIP_PLACEHOLDER
    except Exception as exc:  # 任何失败都降级，绝不抛进 daemon 主循环
        logging.debug("本地 OCR 降级为空串：%s", exc)
        return SKIP_PLACEHOLDER
