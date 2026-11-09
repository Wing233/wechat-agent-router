//! End-to-end check of the daily worklog pipeline without any network upload.

use serde_json::json;
use std::sync::Arc;
use wechat_agent_desktop::logs::EventLog;
use wechat_agent_desktop::worklog::db::WorklogDb;
use wechat_agent_desktop::worklog::WorklogService;

fn temp_base(tag: &str) -> std::path::PathBuf {
    let base = std::env::temp_dir().join(format!(
        "wad-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_millis())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&base).expect("temp dir");
    base
}

#[test]
fn finalize_without_token_writes_local_markdown() {
    let base = temp_base("worklog");
    let root = base.join("worklog");
    std::fs::create_dir_all(root.join("notes")).expect("notes dir");
    let notes = root.join("notes");

    let config = json!({
        "min_note_chars": 0,
        "finalize_time": "22:00",
        "worklog_dir": notes.to_string_lossy(),
        "screenshot_enabled": false,
        "sources": {
            "git": {"enabled": false},
            "browser": {"enabled": false},
            "files": {"enabled": false},
            "codex": {"enabled": false},
            "workbuddy": {"enabled": false}
        }
    });
    std::fs::write(
        root.join("config.json"),
        serde_json::to_string_pretty(&config).expect("config json"),
    )
    .expect("config write");

    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    let db = WorklogDb::new(root.join("worklog.sqlite3"));
    db.init().expect("db init");
    for minute in [0, 1, 2, 5] {
        let stamp = format!("{today}T09:{minute:02}:00+08:00");
        db.add_sample(&today, "Code.exe", "main.rs - wechatbot", 0, &stamp)
            .expect("sample");
    }
    db.add_ocr_sample(&today, "Code.exe", "fn main() {\n    println!(\"hi\")\n", "2026-01-01T09:00:00+08:00")
        .expect("ocr sample");

    let service = WorklogService::new(
        root.clone(),
        Arc::new(EventLog::new(50, None, "test")),
    );
    let status = service.status();
    assert_eq!(status["samples_today"].as_i64().unwrap_or(0), 4);
    assert_eq!(status["device_configured"].as_bool(), Some(false));
    assert!(status["next_finalize"].as_str().unwrap_or("").contains('T'));

    service.finalize("").expect("finalize starts");

    let mut done = false;
    for _ in 0..100 {
        std::thread::sleep(std::time::Duration::from_millis(100));
        let status = service.status();
        let job = &status["job"];
        let finished = job.get("result").is_some()
            || !job.get("error").and_then(|value| value.as_str()).unwrap_or("").is_empty();
        if finished {
            done = true;
            break;
        }
    }
    assert!(done, "finalize job never finished: {}", service.status());

    let status = service.status();
    let job = &status["job"];
    assert_eq!(
        job["result"]["status"].as_str(),
        Some("local"),
        "job: {job}"
    );

    let note_path = notes.join(format!("{today}.md"));
    assert!(note_path.is_file(), "local markdown missing");
    let note = std::fs::read_to_string(&note_path).expect("read note");
    assert!(note.starts_with(&format!("[worklog] {today} 工作活动")));
    assert!(note.contains("Code.exe"));
    assert!(note.contains("<!--WORKLOG_TL_V1 "));

    let row = db.day(&today).expect("day query").expect("day row");
    assert_eq!(row.status, "local");

    let _ = std::fs::remove_dir_all(&base);
}
