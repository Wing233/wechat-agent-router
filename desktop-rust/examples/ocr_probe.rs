//! Smoke test: capture the primary screen, OCR it locally, print only counts.
//!
//! Run with: cargo run --example ocr_probe
//! The recognized text never leaves the process; only the character count prints.

fn main() {
    let config = serde_json::json!({
        "screenshot_enabled": true,
        "idle_skip_seconds": 300,
        "browser_apps": [],
        "redact_patterns": [],
        "ocr_language": "zh-CN"
    });
    let text = wechat_agent_desktop::worklog::ocr::capture_screen_text(&config);
    println!(
        "chars={} lines={}",
        text.chars().count(),
        text.lines().count()
    );
    println!("probe: {}", wechat_agent_desktop::worklog::ocr::probe());
}
