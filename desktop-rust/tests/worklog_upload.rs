//! End-to-end upload check against a local mock of the LYPH device API.
//!
//! Verifies the exact multipart contract the server publisher depends on:
//! `source_kind=note`, `capture_id=worklog-<day>-v1`, and the text payload.

use serde_json::json;
use std::sync::mpsc;
use std::sync::Arc;
use std::time::Duration;
use wechat_agent_desktop::logs::EventLog;
use wechat_agent_desktop::secrets::SecretStore;
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
fn finalize_uploads_note_with_expected_contract() {
    // --- mock LYPH device API -------------------------------------------
    let server = tiny_http::Server::http("127.0.0.1:0").expect("mock server");
    let port = match server.server_addr() {
        tiny_http::ListenAddr::IP(address) => address.port(),
    };
    let (sender, receiver) = mpsc::channel::<(String, String)>();
    let sender = std::sync::Mutex::new(sender);
    std::thread::spawn(move || {
        for mut request in server.incoming_requests() {
            let path = request.url().to_string();
            let mut body = String::new();
            let _ = request.as_reader().read_to_string(&mut body);
            let _ = sender.lock().unwrap().send((path, body));
            let response = tiny_http::Response::from_string(r#"{"id":"capture-1"}"#)
                .with_status_code(201)
                .with_header(
                    tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                        .unwrap(),
                );
            let _ = request.respond(response);
        }
    });

    // --- service under test ---------------------------------------------
    let base = temp_base("upload");
    let root = base.join("worklog");
    std::fs::create_dir_all(root.join("notes")).expect("notes");
    let config = json!({
        "min_note_chars": 0,
        "finalize_time": "22:00",
        "lyph_base_url": format!("http://127.0.0.1:{port}"),
        "worklog_dir": root.join("notes").to_string_lossy(),
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
        serde_json::to_string_pretty(&config).expect("config"),
    )
    .expect("write config");
    SecretStore::new(root.join("secrets.json"))
        .set("device_token", "test-device-token")
        .expect("secret");

    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    let db = WorklogDb::new(root.join("worklog.sqlite3"));
    db.init().expect("db");
    for minute in [0, 1, 2] {
        db.add_sample(
            &today,
            "Code.exe",
            "main.rs - wechatbot",
            0,
            &format!("{today}T09:{minute:02}:00+08:00"),
        )
        .expect("sample");
    }

    let service = WorklogService::new(root.clone(), Arc::new(EventLog::new(50, None, "test")));
    service.finalize("").expect("finalize starts");

    let mut finished = false;
    for _ in 0..100 {
        std::thread::sleep(Duration::from_millis(100));
        let status = service.status();
        let job = &status["job"];
        if job.get("result").is_some()
            || !job
                .get("error")
                .and_then(|value| value.as_str())
                .unwrap_or("")
                .is_empty()
        {
            finished = true;
            break;
        }
    }
    assert!(finished, "job did not finish: {}", service.status());
    let status = service.status();
    assert_eq!(
        status["job"]["result"]["status"].as_str(),
        Some("uploaded"),
        "job: {}",
        status["job"]
    );

    // --- assert the request the server would have received --------------
    let (path, body) = receiver
        .recv_timeout(Duration::from_secs(5))
        .expect("server never received an upload");
    assert_eq!(path, "/api/v1/device/captures");
    assert!(body.contains("name=\"capture_id\""), "body: {body}");
    assert!(body.contains(&format!("worklog-{today}-v1")), "body: {body}");
    assert!(body.contains("name=\"source_kind\""), "body: {body}");
    assert!(body.contains("\r\nnote\r\n"), "body: {body}");
    assert!(body.contains("name=\"text\""), "body: {body}");
    assert!(body.contains("[worklog]"), "body: {body}");
    assert!(body.contains("<!--WORKLOG_TL_V1 "), "body: {body}");
    // Pixels must never be part of the request.
    assert!(!body.contains("name=\"file\""), "body: {body}");

    let _ = std::fs::remove_dir_all(&base);
}
