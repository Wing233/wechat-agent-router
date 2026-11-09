//! End-to-end check that the Rust news/tech-stack pipeline actually writes files.

use serde_json::json;
use std::sync::Arc;
use wechat_agent_desktop::tech_stack::TechStackService;

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
fn tech_stack_run_generates_json_and_html() {
    let base = temp_base("tech");
    let root = base.join("tech-stack");
    let project = base.join("demo-project");
    std::fs::create_dir_all(project.join("src")).expect("src");
    std::fs::write(
        project.join("package.json"),
        r#"{"dependencies":{"react":"^18.2.0"},"devDependencies":{"typescript":"^5"}}"#,
    )
    .expect("package.json");
    std::fs::write(project.join("src/main.py"), "print(1)\n").expect("main.py");
    std::fs::create_dir_all(root.join("reports")).expect("reports");

    let config = json!({
        "enabled": false,
        "weekly_day": 6,
        "weekly_time": "10:00",
        "project_roots": [project.to_string_lossy()],
        "output_dir": root.join("reports").to_string_lossy(),
        "workbuddy_exports": root.join("workbuddy-exports").to_string_lossy(),
        "codex_home": base.join("no-codex").to_string_lossy(),
        "opencode_database": base.join("no-opencode.db").to_string_lossy(),
        "public_site_url": "",
        "history_days": 30,
        "max_depth": 3
    });
    std::fs::write(
        root.join("config.json"),
        serde_json::to_string_pretty(&config).expect("config json"),
    )
    .expect("config write");

    let service = TechStackService::new(
        root.clone(),
        Arc::new(wechat_agent_desktop::logs::EventLog::new(50, None, "test")),
    );
    service.run_now();

    let mut counted = 0;
    for _ in 0..100 {
        std::thread::sleep(std::time::Duration::from_millis(100));
        counted = service
            .status()
            .get("technology_count")
            .and_then(|value| value.as_u64())
            .unwrap_or(0);
        if counted > 0 {
            break;
        }
    }
    assert!(counted > 0, "no technologies discovered: {}", service.status());

    let latest_json = root.join("reports").join("latest.json");
    let latest_html = root.join("reports").join("latest.html");
    assert!(latest_json.is_file(), "latest.json missing");
    assert!(latest_html.is_file(), "latest.html missing");

    let report: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&latest_json).expect("read json"))
            .expect("parse json");
    let names: Vec<String> = report["technologies"]
        .as_array()
        .expect("technologies")
        .iter()
        .map(|item| item["name"].as_str().unwrap_or("").to_string())
        .collect();
    assert!(names.iter().any(|name| name == "React"), "names: {names:?}");
    assert!(names.iter().any(|name| name == "Python"), "names: {names:?}");
    assert_eq!(
        report["summary"]["projects_scanned"].as_u64().unwrap_or(0),
        1
    );

    let html = std::fs::read_to_string(&latest_html).expect("read html");
    assert!(html.contains("Lyph 技术栈周更"));
    assert!(html.contains("React"));

    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn tech_stack_pushes_to_lyph_device_endpoint() {
    // --- mock LYPH device API -------------------------------------------
    let server = tiny_http::Server::http("127.0.0.1:0").expect("mock server");
    let port = match server.server_addr() {
        tiny_http::ListenAddr::IP(address) => address.port(),
    };
    let (sender, receiver) = std::sync::mpsc::channel::<(String, String)>();
    let sender = std::sync::Mutex::new(sender);
    std::thread::spawn(move || {
        for mut request in server.incoming_requests() {
            let path = request.url().to_string();
            let mut body = String::new();
            let _ = std::io::Read::read_to_string(request.as_reader(), &mut body);
            let _ = sender.lock().unwrap().send((path, body));
            let response = tiny_http::Response::from_string(r#"{"updated":true,"revision":9}"#)
                .with_status_code(200)
                .with_header(
                    tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                        .unwrap(),
                );
            let _ = request.respond(response);
        }
    });

    // --- service under test ---------------------------------------------
    let base = temp_base("tech-sync");
    let root = base.join("tech-stack");
    let project = base.join("demo-project");
    std::fs::create_dir_all(project.join("src")).expect("src");
    std::fs::write(
        project.join("package.json"),
        r#"{"dependencies":{"react":"^18.2.0"}}"#,
    )
    .expect("package.json");
    std::fs::write(project.join("src/main.py"), "print(1)\n").expect("main.py");
    std::fs::create_dir_all(root.join("reports")).expect("reports");
    // The desktop reads the device token from the worklog secrets file.
    wechat_agent_desktop::secrets::SecretStore::new(
        base.join("worklog").join("secrets.json"),
    )
    .set("device_token", "sync-device-token")
    .expect("secret");

    let config = json!({
        "enabled": false,
        "weekly_day": 6,
        "weekly_time": "10:00",
        "project_roots": [project.to_string_lossy()],
        "output_dir": root.join("reports").to_string_lossy(),
        "workbuddy_exports": root.join("workbuddy-exports").to_string_lossy(),
        "codex_home": base.join("no-codex").to_string_lossy(),
        "opencode_database": base.join("no-opencode.db").to_string_lossy(),
        "public_site_url": "",
        "history_days": 30,
        "max_depth": 3,
        "lyph_base_url": format!("http://127.0.0.1:{port}"),
        "lyph_sync_enabled": true
    });
    std::fs::write(
        root.join("config.json"),
        serde_json::to_string_pretty(&config).expect("config json"),
    )
    .expect("config write");

    let service = TechStackService::new(
        root.clone(),
        Arc::new(wechat_agent_desktop::logs::EventLog::new(50, None, "test")),
    );
    service.run_now();

    let (path, body) = receiver
        .recv_timeout(std::time::Duration::from_secs(30))
        .expect("no sync request received");
    assert_eq!(path, "/api/v1/device/site-stack");
    assert!(body.contains("\"items\""), "body: {body}");
    assert!(body.contains("React"), "body: {body}");
    assert!(!body.contains("generated_at\":null"), "body: {body}");

    let status = service.status();
    for _ in 0..60 {
        if !status["lyph_sync"]["last_at"].as_str().unwrap_or("").is_empty() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    let status = service.status();
    assert!(
        status["lyph_sync"]["last_error"].as_str().unwrap_or("").is_empty(),
        "status: {status}"
    );
    assert!(
        !status["lyph_sync"]["last_at"].as_str().unwrap_or("").is_empty(),
        "status: {status}"
    );

    let _ = std::fs::remove_dir_all(&base);
}
