use anyhow::Result;
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

pub fn status() -> serde_json::Value {
    let state = STATE.lock().unwrap().clone();
    if !state.is_null() {
        return state;
    }
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

fn begin_download() -> Result<serde_json::Value, serde_json::Value> {
    let state = STATE.lock().unwrap();
    if state.get("state").and_then(|value| value.as_str()) == Some("downloading")
        || state.get("state").and_then(|value| value.as_str()) == Some("applying")
    {
        return Err(json!({"started": false, "error": "更新正在进行"}));
    }
    let exe_url = state.get("exe_url").and_then(|value| value.as_str()).unwrap_or("").to_string();
    let sha_url = state.get("sha_url").and_then(|value| value.as_str()).unwrap_or("").to_string();
    if exe_url.is_empty() {
        return Err(json!({"started": false, "error": "请先检查更新"}));
    }
    if sha_url.is_empty() {
        return Err(json!({"started": false, "error": "更新包缺少校验值，拒绝安装"}));
    }
    Ok(json!({
        "state": "downloading",
        "progress": 0.0,
        "error": "",
        "current": crate::paths::VERSION,
        "latest": state.get("latest"),
        "tag": state.get("tag"),
        "available": state.get("available"),
        "relation": state.get("relation"),
        "exe_url": exe_url,
        "sha_url": sha_url,
        "size": state.get("size").cloned().unwrap_or(json!(0)),
        "checked_at": state.get("checked_at").cloned().unwrap_or(json!(0))
    }))
}

pub fn apply() -> serde_json::Value {
    let exe = match std::env::current_exe() {
        Ok(exe) => exe,
        Err(error) => return json!({"started": false, "error": error.to_string()}),
    };
    if !exe.file_name().map(|name| name.to_string_lossy().eq_ignore_ascii_case("WeChatAgentDesktop.exe")).unwrap_or(false) {
        return json!({"started": false, "error": "源码模式不支持自动更新，请下载最新 exe"});
    }
    // The guard is released by begin_download before set_state; locking the
    // same mutex twice here deadlocks the single local-API thread and leaves
    // the UI permanently unresponsive.
    let next = match begin_download() {
        Ok(next) => next,
        Err(error) => return error,
    };
    set_state(next);
    std::thread::spawn(move || {
        if let Err(error) = download_and_install(&exe, &exe_url_value(), &sha_url_value()) {
            set_state_with_error(&error.to_string());
        }
    });
    json!({"started": true})
}

fn exe_url_value() -> String {
    STATE.lock().unwrap().get("exe_url").and_then(|value| value.as_str()).unwrap_or("").to_string()
}

fn sha_url_value() -> String {
    STATE.lock().unwrap().get("sha_url").and_then(|value| value.as_str()).unwrap_or("").to_string()
}

fn set_state_with_error(error: &str) {
    let mut state = STATE.lock().unwrap().clone();
    if let Some(obj) = state.as_object_mut() {
        obj.insert("state".into(), json!("error"));
        obj.insert("error".into(), json!(error));
    }
    set_state(state);
}

fn download_and_install(exe: &std::path::Path, exe_url: &str, sha_url: &str) -> Result<()> {
    let client = Client::builder()
        .timeout(std::time::Duration::from_secs(60))
        .build()?;
    let exe_url = if exe_url.is_empty() { crate::paths::VERSION.to_string() } else { exe_url.to_string() };
    let _ = exe_url;
    let target = exe.with_extension("exe.new");
    let expected = if sha_url.is_empty() {
        String::new()
    } else {
        let response = client
            .get(sha_url)
            .header("User-Agent", format!("wechat-agent-desktop/{}", crate::paths::VERSION))
            .send()?;
        let text = response.text()?;
        text.split_whitespace().next().unwrap_or("").to_lowercase()
    };
    let mut response = client
        .get(exe_url_value())
        .header("User-Agent", format!("wechat-agent-desktop/{}", crate::paths::VERSION))
        .send()?;
    let total = response.content_length().unwrap_or(0);
    {
        let mut state = STATE.lock().unwrap();
        if let Some(obj) = state.as_object_mut() {
            obj.insert("size".into(), json!(total));
        }
    }
    use std::io::Read;
    let mut downloaded: u64 = 0;
    let mut file = std::fs::File::create(&target)?;
    let mut buffer = [0u8; 262144];
    loop {
        let read = response.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        use std::io::Write;
        file.write_all(&buffer[..read])?;
        downloaded += read as u64;
        if total > 0 {
            let mut state = STATE.lock().unwrap();
            if let Some(obj) = state.as_object_mut() {
                let progress = ((downloaded as f64 / total as f64) * 100.0).min(99.0) / 100.0;
                obj.insert("progress".into(), json!(progress));
            }
        }
    }
    drop(file);
    if target.metadata()?.len() < 1_000_000 {
        anyhow::bail!("下载文件过小，可能不是有效的安装包");
    }
    let bytes = std::fs::read(&target)?;
    if !bytes.starts_with(b"MZ") {
        anyhow::bail!("下载文件不是有效的 Windows 程序");
    }
    if expected.is_empty() {
        anyhow::bail!("更新包缺少校验值，拒绝安装");
    }
    let digest = sha256_hex(&bytes);
    if digest != expected {
        anyhow::bail!("下载文件校验失败，请重试");
    }
    let script = updater_script(exe);
    {
        let mut state = STATE.lock().unwrap();
        if let Some(obj) = state.as_object_mut() {
            obj.insert("state".into(), json!("applying"));
            obj.insert("progress".into(), json!(1.0));
        }
    }
    std::process::Command::new("cmd")
        .args(["/c", script.to_str().unwrap_or("")])
        .current_dir(exe.parent().unwrap_or(std::path::Path::new(".")))
        .spawn()?;
    std::thread::sleep(std::time::Duration::from_millis(300));
    std::process::exit(0);
}

fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let hash = Sha256::digest(data);
    hash.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn updater_script(exe: &std::path::Path) -> std::path::PathBuf {
    let script = exe.with_file_name(".update-WeChatAgentDesktop.cmd");
    let content = r#"@echo off
setlocal
set "DIR=%~dp0"
set "EXE=%DIR%WeChatAgentDesktop.exe"
set "NEW=%DIR%WeChatAgentDesktop.exe.new"
set "BAK=%DIR%.WeChatAgentDesktop.exe.previous"
:wait
tasklist /FI "IMAGENAME eq WeChatAgentDesktop.exe" 2>nul | find /I "WeChatAgentDesktop.exe" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)
for /L %%i in (1,1,20) do (
  move /Y "%EXE%" "%BAK%" >nul 2>nul
  if not exist "%EXE%" goto replace
  timeout /t 1 /nobreak >nul
)
goto done
:replace
move /Y "%NEW%" "%EXE%" >nul 2>nul
if not exist "%EXE%" if exist "%BAK%" move /Y "%BAK%" "%EXE%" >nul 2>nul
timeout /t 2 /nobreak >nul
start "" /D "%DIR%" "%EXE%"
:done
del "%~f0"
"#;
    let _ = std::fs::write(&script, content);
    script
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn begin_download_does_not_hold_the_state_lock() {
        set_state(json!({
            "state": "idle",
            "exe_url": "https://example.com/app.exe",
            "sha_url": "https://example.com/app.exe.sha256",
            "latest": "1.3.8"
        }));
        let next = begin_download().expect("valid state must start a download");
        assert_eq!(next["state"], "downloading");
        // set_state locks the same mutex; this would deadlock if begin_download
        // still held the guard.
        set_state(next);
        assert_eq!(status()["state"], "downloading");

        let busy = begin_download().unwrap_err();
        assert!(busy["error"].as_str().unwrap_or("").contains("更新正在进行"));

        set_state(json!({"state": "idle", "exe_url": "https://example.com/app.exe"}));
        let missing = begin_download().unwrap_err();
        assert!(missing["error"].as_str().unwrap_or("").contains("校验值"));
        set_state(serde_json::Value::Null);
    }
}

