use anyhow::Result;
use serde_json::{json, Value};
use std::path::Path;
use std::time::Duration;

pub fn read_state(path: &Path) -> Value {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(|value| value.is_object())
        .unwrap_or(Value::Object(Default::default()))
}

pub fn write_state(path: &Path, port: u16, token: &str, url: &str, headless: bool) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let state = json!({
        "port": port,
        "token": token,
        "url": url,
        "pid": std::process::id(),
        "headless": headless,
        "started_at": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs()
    });
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(&state)?)?;
    std::fs::rename(tmp, path)?;
    Ok(())
}

pub fn clear_state(path: &Path, pid: Option<u32>) {
    let state = read_state(path);
    if let Some(pid) = pid {
        if state.get("pid").and_then(|value| value.as_u64()) != Some(pid as u64) {
            return;
        }
    }
    let _ = std::fs::remove_file(path);
}

pub fn is_alive(state: &Value) -> bool {
    let port = state.get("port").and_then(|value| value.as_u64()).unwrap_or(0);
    let token = state.get("token").and_then(|value| value.as_str()).unwrap_or("");
    if port == 0 || token.is_empty() {
        return false;
    }
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_millis(1500))
        .build();
    let client = match client {
        Ok(client) => client,
        Err(_) => return false,
    };
    client
        .get(format!("http://127.0.0.1:{port}/api/ping"))
        .header("X-Desktop-Token", token)
        .send()
        .map(|response| response.status().is_success())
        .unwrap_or(false)
}

pub fn existing_url(state: &Value) -> Option<String> {
    let url = state.get("url").and_then(|value| value.as_str()).unwrap_or("");
    if !url.is_empty() {
        return Some(url.to_string());
    }
    let port = state.get("port").and_then(|value| value.as_u64()).unwrap_or(0);
    let token = state.get("token").and_then(|value| value.as_str()).unwrap_or("");
    if port > 0 && !token.is_empty() {
        Some(format!("http://127.0.0.1:{port}/#t={token}"))
    } else {
        None
    }
}
