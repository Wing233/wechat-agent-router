//! On-device screen OCR. Pixels never leave this process: the frame is captured
//! into memory, recognized locally, and only redacted text is kept.

use crate::worklog::config::{get_bool, get_i64, get_list, get_str};
use crate::worklog::note::sanitize_text;
use serde_json::Value;

const LOCK_APPS: [&str; 2] = ["lockapp.exe", "logonui.exe"];

/// Frames we must not even OCR: idle, lock screen, browsers, sensitive titles.
pub fn should_skip_frame(app: &str, title: &str, idle: i64, config: &Value) -> bool {
    let idle_limit = get_i64(config, "idle_skip_seconds", 300).max(1);
    if idle >= idle_limit {
        return true;
    }
    let app_lower = app.to_lowercase();
    if LOCK_APPS.contains(&app_lower.as_str()) {
        return true;
    }
    let browsers = get_list(config, "browser_apps")
        .into_iter()
        .map(|item| item.to_lowercase())
        .collect::<Vec<_>>();
    if !app_lower.is_empty() && browsers.contains(&app_lower) {
        return true;
    }
    let lower_title = title.to_lowercase();
    get_list(config, "redact_patterns")
        .iter()
        .any(|pattern| lower_title.contains(&pattern.to_lowercase()))
}

/// Replace every line that matches a sensitive pattern, then sanitize paths.
pub fn redact_ocr_text(text: &str, config: &Value) -> String {
    let patterns = get_list(config, "redact_patterns")
        .into_iter()
        .map(|item| item.to_lowercase())
        .collect::<Vec<_>>();
    let mut kept: Vec<String> = Vec::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        if patterns.iter().any(|pattern| line.to_lowercase().contains(pattern)) {
            kept.push("[敏感内容已隐藏]".to_string());
        } else {
            kept.push(line.to_string());
        }
    }
    sanitize_text(&kept.join("\n"))
}

/// Capture the primary screen and return recognized text. Any failure yields "".
pub fn capture_screen_text(config: &Value) -> String {
    #[cfg(windows)]
    {
        if !get_bool(config, "screenshot_enabled", true) {
            return String::new();
        }
        let (app, title) = crate::worklog::sample::foreground();
        let idle = crate::worklog::sample::idle_seconds();
        if should_skip_frame(&app, &title, idle, config) {
            return String::new();
        }
        let language = get_str(config, "ocr_language", "zh-CN");
        match capture_pixels() {
            Ok((width, height, pixels)) => recognize(&pixels, width, height, &language).unwrap_or_default(),
            Err(_) => String::new(),
        }
    }
    #[cfg(not(windows))]
    {
        let _ = config;
        String::new()
    }
}

/// Diagnostics for the `ocr_probe` example: capture + OCR with explicit errors.
#[cfg(windows)]
pub fn probe() -> String {
    match capture_pixels() {
        Ok((width, height, pixels)) => {
            let (app, title) = crate::worklog::sample::foreground();
            let idle = crate::worklog::sample::idle_seconds();
            match recognize(&pixels, width, height, "zh-CN") {
                Ok(text) => format!(
                    "capture={width}x{height} bytes={} app={app} title_len={} idle={idle}s ocr_chars={} ocr_lines={}",
                    pixels.len(),
                    title.chars().count(),
                    text.chars().count(),
                    text.lines().count()
                ),
                Err(error) => format!("ocr_error={error}"),
            }
        }
        Err(error) => format!("capture_error={error}"),
    }
}

/// Grab the primary monitor as BGRA bytes (no file, no encode).
#[cfg(windows)]
fn capture_pixels() -> anyhow::Result<(u32, u32, Vec<u8>)> {
    let mut monitors = xcap::Monitor::all()?;
    let primary = monitors
        .iter()
        .position(|monitor| monitor.is_primary().unwrap_or(false));
    let monitor = match primary {
        Some(index) => monitors.swap_remove(index),
        None if monitors.len() == 1 => monitors.pop().expect("one monitor"),
        None => anyhow::bail!("capture_unavailable：没有主屏"),
    };
    let image = monitor.capture_image()?;
    let width = image.width();
    let height = image.height();
    let rgba = image.into_raw();
    let mut bgra = Vec::with_capacity(rgba.len());
    for pixel in rgba.chunks_exact(4) {
        bgra.push(pixel[2]);
        bgra.push(pixel[1]);
        bgra.push(pixel[0]);
        bgra.push(pixel[3]);
    }
    Ok((width, height, bgra))
}

/// Microsoft OCR language tags differ from the `zh-CN` value used in config.
#[cfg(windows)]
fn windows_ocr_language(requested: &str) -> String {
    let lower = requested.trim().to_lowercase();
    if lower.starts_with("zh") && !lower.contains("hans") && !lower.contains("hant") {
        "zh-Hans-CN".to_string()
    } else if requested.trim().is_empty() {
        "zh-Hans-CN".to_string()
    } else {
        requested.trim().to_string()
    }
}

#[cfg(windows)]
fn recognize(pixels: &[u8], width: u32, height: u32, language: &str) -> anyhow::Result<String> {
    use windows::core::HSTRING;
    use windows::Globalization::Language;
    use windows::Graphics::Imaging::{BitmapPixelFormat, SoftwareBitmap};
    use windows::Media::Ocr::OcrEngine;
    use windows::Storage::Streams::DataWriter;
    use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};

    static COM_READY: std::sync::Once = std::sync::Once::new();
    COM_READY.call_once(|| unsafe {
        // The OCR engine is a WinRT component; MTAs are enough for a worker thread.
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
    });

    let writer = DataWriter::new()?;
    writer.WriteBytes(pixels)?;
    let buffer = writer.DetachBuffer()?;
    let bitmap = SoftwareBitmap::CreateCopyFromBuffer(
        &buffer,
        BitmapPixelFormat::Bgra8,
        width as i32,
        height as i32,
    )?;
    let language = windows_ocr_language(&language);
    let engine = match OcrEngine::TryCreateFromLanguage(&Language::CreateLanguage(&HSTRING::from(
        language.clone(),
    ))?) {
        Ok(engine) => engine,
        Err(_) => match OcrEngine::TryCreateFromUserProfileLanguages() {
            Ok(engine) => engine,
            Err(_) => return Ok(String::new()),
        },
    };
    let operation = engine.RecognizeAsync(&bitmap)?;
    // WinRT rejects GetResults while the operation is still running
    // (E_ILLEGAL_METHOD_CALL); retry until it completes.
    let mut last_error: Option<windows::core::Error> = None;
    for _ in 0..400 {
        match operation.GetResults() {
            Ok(result) => {
                let mut parts: Vec<String> = Vec::new();
                if let Ok(lines) = result.Lines() {
                    for line in lines {
                        if let Ok(text) = line.Text() {
                            let value = text.to_string();
                            if !value.trim().is_empty() {
                                parts.push(value);
                            }
                        }
                    }
                }
                if parts.is_empty() {
                    return Ok(result.Text()?.to_string());
                }
                return Ok(parts.join("\n"));
            }
            Err(error) => {
                let code = error.code().0 as u32;
                last_error = Some(error);
                if code != 0x8000_000E {
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(25));
            }
        }
    }
    Err(last_error
        .map(|error| anyhow::anyhow!(error))
        .unwrap_or_else(|| anyhow::anyhow!("OCR 未返回结果")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn config() -> Value {
        json!({
            "idle_skip_seconds": 300,
            "browser_apps": ["chrome.exe"],
            "redact_patterns": ["password", "密码"]
        })
    }

    #[test]
    fn skips_idle_lock_browser_and_secrets() {
        let cfg = config();
        assert!(should_skip_frame("Code.exe", "main.rs", 301, &cfg));
        assert!(should_skip_frame("LockApp.exe", "", 0, &cfg));
        assert!(should_skip_frame("chrome.exe", "docs", 0, &cfg));
        assert!(should_skip_frame("Code.exe", "输入密码", 0, &cfg));
        assert!(!should_skip_frame("Code.exe", "main.rs", 10, &cfg));
    }

    #[test]
    fn redacts_matching_lines_only() {
        let result = redact_ocr_text("正常一行\npassword: 123\n另一行", &config());
        assert!(result.contains("正常一行"));
        assert!(result.contains("[敏感内容已隐藏]"));
        assert!(!result.contains("123"));
    }
}
