use crate::app::DesktopApp;
use crate::paths;
use crate::tech_stack::TechStackService;
use crate::worklog::WorklogService;
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::net::TcpListener;
use std::sync::Arc;
use tiny_http::{Header, Method, Response, Server, StatusCode};

pub struct ApiServer {
    pub port: u16,
    pub url: String,
    pub ui_url: String,
}

pub fn start(app: Arc<DesktopApp>, worklog: Arc<WorklogService>, tech_stack: Arc<TechStackService>) -> Result<ApiServer> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    let url = format!("http://127.0.0.1:{port}/");
    let ui_url = format!("{url}#t={}", percent_encode(&app.token));
    let server = Server::from_listener(listener, None).map_err(|error| anyhow::anyhow!("创建本地 HTTP 服务失败：{error}"))?;
    let static_files = load_static(&app);
    std::thread::spawn(move || {
        for request in server.incoming_requests() {
            let _ = handle(request, &app, &worklog, &tech_stack, &static_files);
        }
    });
    Ok(ApiServer { port, url, ui_url })
}

fn load_static(app: &DesktopApp) -> HashMap<String, (Vec<u8>, &'static str)> {
    let mut map = HashMap::new();
    let base = paths::web_dir();
    for (name, mime) in [
        ("index.html", "text/html; charset=utf-8"),
        ("app.js", "application/javascript; charset=utf-8"),
        ("theme.js", "application/javascript; charset=utf-8"),
        ("styles.css", "text/css; charset=utf-8"),
        ("favicon.svg", "image/svg+xml"),
    ] {
        let path = base.join(name);
        if path.is_file() {
            if let Ok(bytes) = fs::read(path) {
                map.insert(format!("/{name}"), (bytes, mime));
            }
        }
    }
    let _ = app;
    map
}

fn handle(mut request: tiny_http::Request, app: &DesktopApp, worklog: &WorklogService, tech_stack: &TechStackService, static_files: &HashMap<String, (Vec<u8>, &'static str)>) -> Result<()> {
    let host = request.headers().iter().find(|h| h.field.as_str().to_ascii_lowercase() == "host")
        .map(|h| h.value.as_str().to_string())
        .unwrap_or_default();
    if !is_local_host(&host) {
        return send_json(request, 403, &json!({"detail": "仅允许本机访问"}));
    }
    let path = request.url().to_string();
    let (path_only, query) = split_query(&path);
    if !path_only.starts_with("/api/") {
        let static_path = if path_only == "/" { "/index.html" } else { path_only.as_str() };
        if let Some((bytes, mime)) = static_files.get(static_path) {
            return send_bytes(request, 200, bytes, mime);
        }
        return send_json(request, 404, &json!({"detail": "页面不存在"}));
    }
    let token = request.headers().iter().find(|h| h.field.as_str().to_ascii_lowercase() == "x-desktop-token")
        .map(|h| h.value.as_str().to_string())
        .unwrap_or_default();
    if token != app.token {
        return send_json(request, 401, &json!({"detail": "本地控制台令牌无效，请从桌面 App 重新打开"}));
    }
    let method = request.method().clone();
    let body = read_body(&mut request)?;
    let result = route(method, &path_only, &query, &body, app, worklog, tech_stack);
    match result {
        Ok((status, value)) => send_json(request, status, &value),
        Err((status, message)) => send_json(request, status, &json!({"detail": message})),
    }
}

fn is_local_host(host: &str) -> bool {
    let lower = host.to_lowercase();
    lower.starts_with("127.0.0.1:") || lower.starts_with("localhost:") || lower.starts_with("[::1]:")
}

fn split_query(text: &str) -> (String, HashMap<String, String>) {
    if let Some((path, query)) = text.split_once('?') {
        let mut map = HashMap::new();
        for pair in query.split('&') {
            if pair.is_empty() { continue; }
            if let Some((k, v)) = pair.split_once('=') {
                map.insert(url_decode(k), url_decode(v));
            } else {
                map.insert(url_decode(pair), String::new());
            }
        }
        (path.to_string(), map)
    } else {
        (text.to_string(), HashMap::new())
    }
}

fn url_decode(text: &str) -> String {
    let mut out = String::new();
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c == '%' {
            let hi = chars.next().unwrap_or('0');
            let lo = chars.next().unwrap_or('0');
            if let Ok(byte) = u8::from_str_radix(&format!("{hi}{lo}"), 16) {
                out.push(byte as char);
            }
        } else if c == '+' {
            out.push(' ');
        } else {
            out.push(c);
        }
    }
    out
}

fn read_body(request: &mut tiny_http::Request) -> Result<Value> {
    let mut text = String::new();
    request.as_reader().read_to_string(&mut text)?;
    if text.trim().is_empty() {
        return Ok(Value::Object(Default::default()));
    }
    serde_json::from_str(&text).context("请求体不是合法 JSON")
}

fn send_json(request: tiny_http::Request, status: u16, value: &Value) -> Result<()> {
    let data = serde_json::to_vec(value)?;
    send_bytes(request, status, &data, "application/json; charset=utf-8")
}

fn send_bytes(request: tiny_http::Request, status: u16, data: &[u8], mime: &str) -> Result<()> {
    let mut response = Response::from_data(data.to_vec())
        .with_status_code(StatusCode(status))
        .with_header(Header::from_bytes(&b"Content-Type"[..], mime.as_bytes()).unwrap())
        .with_header(Header::from_bytes(&b"Cache-Control"[..], &b"no-store"[..]).unwrap())
        .with_header(Header::from_bytes(&b"X-Content-Type-Options"[..], &b"nosniff"[..]).unwrap())
        .with_header(Header::from_bytes(&b"X-Frame-Options"[..], &b"DENY"[..]).unwrap());
    if mime.starts_with("text/html") {
        response.add_header(Header::from_bytes(&b"Content-Security-Policy"[..], &b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"[..]).unwrap());
    }
    request.respond(response)?;
    Ok(())
}

fn route(method: Method, path: &str, query: &HashMap<String, String>, body: &Value, app: &DesktopApp, worklog: &WorklogService, tech_stack: &TechStackService) -> std::result::Result<(u16, Value), (u16, String)> {
    let result: Result<Value> = (|| {
        Ok(match (method.clone(), path) {
            (Method::Get, "/api/ping") => json!({"ok": true, "app": "wechat-agent-desktop-rust", "pid": std::process::id(), "time": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs()}),
            (Method::Get, "/api/bootstrap") => app.bootstrap(worklog, tech_stack),
            (Method::Get, "/api/status") => app.status(worklog, tech_stack),
            (Method::Get, "/api/config") => app.config_for_ui(),
            (Method::Put, "/api/config") => app.update_config(body)?,
            (Method::Post, "/api/agent/start") => app.start_agent()?,
            (Method::Post, "/api/agent/stop") => app.stop_agent(),
            (Method::Post, "/api/agent/restart") => { app.stop_agent(); app.start_agent()? }
            (Method::Post, "/api/agent/cancel") => app.cancel_agent_task(),
            (Method::Post, "/api/agent/test-engine") => app.test_engine(body.get("engine").and_then(|v| v.as_str()).unwrap_or(""), body.get("workspace").and_then(|v| v.as_str()).unwrap_or(""))?,
            (Method::Get, "/api/router/test") => app.router_health(5),
            (Method::Get, "/api/logs") => {
                let after = query.get("after").and_then(|v| v.parse::<u64>().ok()).unwrap_or(0);
                json!({"logs": app.log.since(after)})
            }
            (Method::Post, "/api/logs/clear") => { app.log.clear(); json!({"ok": true}) }
            (Method::Get, "/api/autostart") => json!({"enabled": crate::autostart::is_enabled()}),
            (Method::Post, "/api/autostart") => json!({"enabled": crate::autostart::set_enabled(body.get("enabled").and_then(|v| v.as_bool()).unwrap_or(false))}),
            (Method::Get, "/api/update/check") => crate::updater::check(),
            (Method::Get, "/api/update/status") => crate::updater::status(),
            (Method::Post, "/api/update/apply") => crate::updater::apply(),
            (Method::Get, "/api/worklog") => worklog.status(),
            (Method::Post, "/api/worklog/start") => worklog.start()?,
            (Method::Post, "/api/worklog/stop") => worklog.stop(),
            (Method::Post, "/api/worklog/pause") => worklog.pause(body.get("paused").and_then(|v| v.as_bool()).unwrap_or(true))?,
            (Method::Post, "/api/worklog/action") => worklog.action(body.get("command").and_then(|v| v.as_str()).unwrap_or(""))?,
            (Method::Post, "/api/worklog/note") => worklog.note(body.get("text").and_then(|v| v.as_str()).unwrap_or(""))?,
            (Method::Post, "/api/worklog/finalize") => worklog.finalize(body.get("day").and_then(|v| v.as_str()).unwrap_or(""))?,
            (Method::Post, "/api/worklog/backfill") => worklog.backfill(body.get("days").and_then(|v| v.as_u64()).unwrap_or(30))?,
            (Method::Put, "/api/worklog/config") => worklog.update_config(body)?,
            (Method::Post, "/api/worklog/device-token") => {
                let token = body.get("token").and_then(|v| v.as_str()).unwrap_or("");
                if token.trim().is_empty() { anyhow::bail!("设备 token 不能为空"); }
                let base = body.get("base_url").and_then(|v| v.as_str()).unwrap_or("https://123.57.153.49");
                worklog.pair(base, token.trim())?
            }
            (Method::Post, "/api/worklog/device-token/clear") => worklog.clear_token()?,
            (Method::Post, "/api/worklog/open") => {
                let kind = body.get("kind").and_then(|v| v.as_str()).unwrap_or("data");
                let notes = worklog.status()["paths"]["notes"].as_str().unwrap_or("").to_string();
                match kind {
                    "data" => open_path(&worklog.root().to_path_buf()),
                    "config" => open_path(&worklog.root().join("config.json")),
                    "log" => open_path(&worklog.root().join("collector.log")),
                    "notes" if !notes.is_empty() => open_path(std::path::Path::new(&notes)),
                    "notes" => open_path(&worklog.root().join("notes")),
                    _ => anyhow::bail!("未知路径"),
                }
                json!({"ok": true})
            }
            (Method::Get, "/api/tech-stack") => tech_stack.status(),
            (Method::Post, "/api/tech-stack/run") => tech_stack.run_now(),
            (Method::Put, "/api/tech-stack/config") => tech_stack.update_config(body)?,
            (Method::Post, "/api/tech-stack/open") => {
                let kind = body.get("kind").and_then(|v| v.as_str()).unwrap_or("report");
                tech_stack.open_path(kind)?;
                json!({"ok": true})
            }
            (Method::Get, "/api/dialog/folder") => {
                let initial = query.get("initial").cloned().unwrap_or_default();
                json!({"path": pick_folder(&initial)})
            }
            (Method::Get, "/api/dialog/file") => {
                let initial = query.get("initial").cloned().unwrap_or_default();
                json!({"path": pick_file(&initial)})
            }
            (Method::Post, "/api/open-path") => {
                let kind = body.get("kind").and_then(|v| v.as_str()).unwrap_or("");
                let explicit = body.get("path").and_then(|v| v.as_str()).unwrap_or("");
                if !explicit.is_empty() {
                    open_path(std::path::Path::new(explicit));
                } else {
                    match kind {
                        "config" => open_path(&app.config_path.clone()),
                        "data" => open_path(&app.data_dir.clone()),
                        "log" => open_path(&paths::log_file()),
                        _ => anyhow::bail!("未知路径"),
                    }
                }
                json!({"ok": true})
            }
            (Method::Post, "/api/server/connect") => app.server_connect(body.get("token").and_then(|v| v.as_str()).unwrap_or(""), body.get("url").and_then(|v| v.as_str()).unwrap_or(""))?,
            (Method::Post, "/api/server/disconnect") => { app.secrets.set("service_token", "")?; json!({"connected": false}) }
            (Method::Get, "/api/server/overview") => app.router_json("GET", "/internal/settings/overview", None, None)?,
            (Method::Post, "/api/server/register-self") => {
                let cfg = app.config();
                let payload = json!({"id": cfg.agent_id, "name": cfg.name, "description": cfg.description, "transport": "poll", "api_key": cfg.agent_key});
                let result = app.router_json("POST", "/internal/settings/agents", Some(payload), None)?;
                json!({"result": result, "agent": app.agent_status()})
            }
            (Method::Post, "/api/quit") => {
                std::thread::spawn(|| std::thread::sleep(std::time::Duration::from_millis(200)));
                json!({"ok": true})
            }
            _ => {
                if path.starts_with("/api/server/") {
                    // Generic proxy keeps the existing web UI working while individual server pages are ported.
                    let suffix = path.trim_start_matches("/api/server");
                    let upstream = format!("/internal/settings{}", suffix);
                    let upstream_path = if query.is_empty() { upstream } else {
                        let qs = query.iter().map(|(k, v)| format!("{}={}", percent_encode(k), percent_encode(v))).collect::<Vec<_>>().join("&");
                        format!("{upstream}?{qs}")
                    };
                    app.router_json(method.as_str(), &upstream_path, Some(body.clone()).filter(|v| !v.is_null()), None)?
                } else {
                    anyhow::bail!("接口不存在");
                }
            }
        })
    })();
    match result {
        Ok(value) => Ok((200, value)),
        Err(error) => {
            let text = error.to_string();
            let status = if text.contains("接口不存在") { 404 } else if text.contains("令牌") { 401 } else { 400 };
            Err((status, text))
        }
    }
}

fn percent_encode(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

fn open_path(path: &std::path::Path) {
    // `open::that` launches the default handler; Explorer is used for folders.
    #[cfg(windows)]
    {
        if path.is_dir() {
            let _ = std::process::Command::new("explorer").arg(path).spawn();
        } else {
            let _ = open::that(path);
        }
    }
    #[cfg(not(windows))]
    {
        let _ = open::that(path);
    }
}

/// Native folder picker for the "select workspace" button.
fn pick_folder(initial: &str) -> String {
    #[cfg(windows)]
    {
        let initial = initial.replace('\'', "''");
        let script = format!(
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null;\
             $d = New-Object System.Windows.Forms.FolderBrowserDialog;\
             $d.Description = '选择目录';\
             if ('{initial}' -ne '') {{ $d.SelectedPath = '{initial}' }};\
             if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{ [Console]::Out.Write($d.SelectedPath) }}"
        );
        return run_powershell(&script);
    }
    #[cfg(not(windows))]
    {
        let _ = initial;
        String::new()
    }
}

/// Native file picker used for importing a device token JSON.
fn pick_file(initial: &str) -> String {
    #[cfg(windows)]
    {
        let initial = initial.replace('\'', "''");
        let script = format!(
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null;\
             $f = New-Object System.Windows.Forms.OpenFileDialog;\
             if ('{initial}' -ne '') {{ $f.InitialDirectory = '{initial}' }};\
             if ($f.ShowDialog() -eq 'OK') {{ [Console]::Out.Write($f.FileName) }}"
        );
        return run_powershell(&script);
    }
    #[cfg(not(windows))]
    {
        let _ = initial;
        String::new()
    }
}

#[cfg(windows)]
fn run_powershell(script: &str) -> String {
    use std::os::windows::process::CommandExt;
    // CREATE_NO_WINDOW hides the console; the WinForms dialog still shows.
    let output = std::process::Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-STA", "-Command", script])
        .creation_flags(0x0800_0000)
        .output();
    match output {
        Ok(output) => String::from_utf8_lossy(&output.stdout).trim().to_string(),
        Err(_) => String::new(),
    }
}
