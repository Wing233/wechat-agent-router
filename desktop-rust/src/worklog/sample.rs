//! Foreground window sampling and local persistence of one 30s tick.

use crate::worklog::config::{get_i64, get_list};
use crate::worklog::db::WorklogDb;
use crate::worklog::note::redact_title;
use crate::worklog::ocr;
use serde_json::Value;

/// Milliseconds since the last keyboard/mouse input.
pub fn idle_seconds() -> i64 {
    #[cfg(windows)]
    {
        use windows::Win32::System::SystemInformation::GetTickCount;
        use windows::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
        unsafe {
            let mut info = LASTINPUTINFO {
                cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32,
                dwTime: 0,
            };
            if !GetLastInputInfo(&mut info).as_bool() {
                return 0;
            }
            let millis = GetTickCount().wrapping_sub(info.dwTime);
            return (millis / 1000) as i64;
        }
    }
    #[cfg(not(windows))]
    {
        0
    }
}

/// (process image name, window title) for the current foreground window.
pub fn foreground() -> (String, String) {
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::CloseHandle;
        use windows::Win32::System::Threading::{
            OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
            PROCESS_QUERY_LIMITED_INFORMATION,
        };
        use windows::Win32::UI::WindowsAndMessaging::{
            GetForegroundWindow, GetWindowTextW, GetWindowThreadProcessId,
        };
        use windows::core::PWSTR;
        unsafe {
            let hwnd = GetForegroundWindow();
            if hwnd.is_invalid() {
                return (String::new(), String::new());
            }
            let mut title_buffer = [0u16; 512];
            let title_len = GetWindowTextW(hwnd, &mut title_buffer);
            let title = String::from_utf16_lossy(&title_buffer[..title_len.max(0) as usize]);

            let mut pid = 0u32;
            GetWindowThreadProcessId(hwnd, Some(&mut pid));
            if pid == 0 {
                return (String::new(), title);
            }
            let handle = match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
                Ok(handle) => handle,
                Err(_) => return (format!("pid-{pid}"), title),
            };
            let mut path_buffer = [0u16; 1024];
            let mut size = path_buffer.len() as u32;
            let result = QueryFullProcessImageNameW(
                handle,
                PROCESS_NAME_WIN32,
                PWSTR(path_buffer.as_mut_ptr()),
                &mut size,
            );
            let _ = CloseHandle(handle);
            if result.is_err() || size == 0 {
                return (format!("pid-{pid}"), title);
            }
            let path = String::from_utf16_lossy(&path_buffer[..size as usize]);
            let name = path
                .rsplit(['\\', '/'])
                .next()
                .unwrap_or(path.as_str())
                .to_string();
            (name, title)
        }
    }
    #[cfg(not(windows))]
    {
        (String::new(), String::new())
    }
}

/// One sampling tick: record the foreground app and (optionally) OCR text.
pub fn sample_once(db: &WorklogDb, config: &Value) -> anyhow::Result<()> {
    let (app, title) = foreground();
    if app.is_empty() {
        return Ok(());
    }
    let idle = idle_seconds();
    if idle >= get_i64(config, "idle_skip_seconds", 300) {
        return Ok(());
    }
    let app_lower = app.to_lowercase();
    if app_lower == "lockapp.exe" || app_lower == "logonui.exe" {
        return Ok(());
    }
    let clean = redact_title(&title, &app, config).unwrap_or_default();
    let now = chrono::Local::now();
    let day = now.format("%Y-%m-%d").to_string();
    let stamp = now.to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
    db.add_sample(&day, &app, &clean, idle, &stamp)?;

    if config
        .get("screenshot_enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true)
    {
        let raw = ocr::capture_screen_text(config);
        if !raw.trim().is_empty() {
            let redacted = ocr::redact_ocr_text(&raw, config);
            if !redacted.trim().is_empty() {
                db.add_ocr_sample(&day, &app, &redacted, &stamp)?;
            }
        }
    }
    Ok(())
}

/// Apps that must never be recorded even if a title slips through.
pub fn blocked_apps(config: &Value) -> Vec<String> {
    let mut apps = get_list(config, "browser_apps");
    apps.push("lockapp.exe".to_string());
    apps.push("logonui.exe".to_string());
    apps
}
