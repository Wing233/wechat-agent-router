use anyhow::{Context, Result};
use reqwest::blocking::Client;
use serde_json::json;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

const REPO: &str = "Wing233/wechat-agent-router";
const ASSET: &str = "WeChatAgentDesktop.exe";

static STATE: Mutex<serde_json::Value> = Mutex::new(serde_json::Value::Null);

fn set_state(value: serde_json::Value) {
    *STATE.lock().unwrap() = value;
}

fn current_state() -> serde_json::Value {
    let guard = STATE.lock().unwrap();
    if guard.is_null() {
        status()
    } else {
        guard.clone()
    }
}

pub fn status() -> serde_json::Value {
    json!({
        "state": "idle",
        "progress": 0.0,
        "error": "",
        "current": crate::paths::VERSION,
        "latest": "",
        "tag": "",
        "available": false,
        "relation": "same",
        "size": 0,
        "checked_at": 0
    })
}

fn version_key(value: &str) -> Vec<u64> {
    value
        .split(|c: char| !c.is_ascii_digit())
        .filter_map(|part| part.parse::<u64>().ok())
        .collect()
}

fn fetch_latest() -> Result<(String, String, String)> {
    let client = Client::builder()
        .timeout(std::time::Duration::from_secs(12))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let page = format!("https://github.com/{REPO}/releases/latest?t={}", SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs());
    if let Ok(response) = client.head(&page).send() {
        if let Some(location) = response.headers().get(reqwest::header::LOCATION).and_then(|v| v.to_str().ok()) {
            if let Some(tag) = location.split("/releases/tag/").nth(1) {
                let tag = tag.split(['?', '#', '/']).next().unwrap_or("").to_string();
                if !tag.is_empty() {
                    let base = format!("https://github.com/{REPO}/releases/download/{tag}");
                    return Ok((tag.clone(), format!("{base}/{ASSET}"), format!("{base}/{ASSET}.sha256")));
                }
            }
        }
    }
    let api = format!("https://api.github.com/repos/{REPO}/releases/latest");
    let response = client
        .get(&api)
        .header("User-Agent", format!("wechat-agent-desktop/{}", crate::paths::VERSION))
        .header("Accept", "application/vnd.github+json")
        .send()?;
    let payload: serde_json::Value = response.json()?;
    let tag = payload.get("tag_name").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let assets = payload.get("assets").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let exe_url = assets
        .iter()
        .find(|asset| asset.get("name").and_then(|v| v.as_str()) == Some(ASSET))
        .and_then(|asset| asset.get("browser_download_url").and_then(|v| v.as_str()))
        .unwrap_or("")
        .to_string();
    let sha_url = assets
        .iter()
        .find(|asset| asset.get("name").and_then(|v| v.as_str()) == Some(&format!("{ASSET}.sha256")))
        .and_then(|asset| asset.get("browser_download_url").and_then(|v| v.as_str()))
        .unwrap_or("")
        .to_string();
    if tag.is_empty() || exe_url.is_empty() {
        anyhow::bail!("无法读取最新版本号");
    }
    Ok((tag, exe_url, sha_url))
}

pub fn check() -> serde_json::Value {
    match fetch_latest() {
        Ok((tag, exe_url, sha_url)) => {
            let latest = tag.trim_start_matches(['v', 'V']);
            let current = version_key(crate::paths::VERSION);
            let latest_key = version_key(latest);
            let relation = if latest_key > current {
                "newer"
            } else if latest_key == current {
                "same"
            } else {
                "older"
            };
            let value = json!({
                "state": "idle",
                "progress": 0.0,
                "error": "",
                "current": crate::paths::VERSION,
                "latest": latest,
                "tag": tag,
                "available": relation == "newer",
                "relation": relation,
                "exe_url": exe_url,
                "sha_url": sha_url,
                "size": 0,
                "checked_at": SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs()
            });
            set_state(value.clone());
            value
        }
        Err(error) => {
            let value = json!({
                "state": "error",
                "progress": 0.0,
                "error": error.to_string(),
                "current": crate::paths::VERSION,
                "latest": "",
                "tag": "",
                "available": false,
                "relation": "",
                "size": 0,
                "checked_at": SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs()
            });
            set_state(value.clone());
            value
        }
    }
}

pub fn apply() -> serde_json::Value {
    json!({"started": false, "error": "Rust 版 updater 下载后替换逻辑尚未实现；请使用 GitHub Releases 页面手动更新"})
}
