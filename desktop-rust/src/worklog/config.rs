//! Worklog configuration: same keys and defaults as the retired Python collector.

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

pub fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn local_app_data() -> PathBuf {
    std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(home_dir)
}

/// Default config, mirroring `worklog_collector.DEFAULT_CONFIG`.
pub fn default_config() -> Value {
    let home = home_dir();
    let la = local_app_data();
    let s = |p: PathBuf| p.to_string_lossy().to_string();
    json!({
        "lyph_base_url": "https://123.57.153.49",
        "device_name": "pc-home-worklog",
        "sample_seconds": 30,
        "idle_skip_seconds": 300,
        "finalize_time": "22:00",
        "min_note_chars": 100,
        "screenshot_enabled": true,
        "ocr_language": "zh-CN",
        "ocr_max_chars": 4000,
        "ocr_lines_per_app": 30,
        "worklog_dir": s(home.join("Documents").join("worklog")),
        "browser_apps": ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"],
        "redact_patterns": [
            "password", "passwd", "token", "secret", "api key", "apikey",
            "密码", "口令", "私钥", "助记词", "银行卡", "身份证", "验证码",
            "支付", "转账", "登录", "sign in", "authenticator"
        ],
        "sources": {
            "git": {
                "enabled": true,
                "repos": [
                    r"D:\xx\wechatbot\agent-router",
                    r"D:\xx\wechatbot\lyph",
                    r"D:\xx\wechatbot\public\wechat-agent-router",
                    r"D:\xx\wechatbot\public\lyph",
                    r"D:\xx\lyph"
                ],
                "authors": ["liubo", "Wing233"],
                "exclude_authors": ["wechat-agent-router", "evolution", "bot"]
            },
            "browser": {
                "enabled": true,
                "profiles": [
                    {
                        "name": "edge",
                        "path": s(la.join("Microsoft").join("Edge").join("User Data").join("Default").join("History")),
                        "exclude_domains": ["localhost", "127.0.0.1", "newtab", "bing.com"]
                    },
                    {
                        "name": "chrome",
                        "path": s(la.join("Google").join("Chrome").join("User Data").join("Default").join("History")),
                        "exclude_domains": ["localhost", "127.0.0.1", "newtab", "google.com"]
                    }
                ]
            },
            "files": {
                "enabled": true,
                "roots": [r"D:\xx\wechatbot", r"D:\xx\lyph"],
                "exclude_projects": [
                    "scratch", "data", "backups",
                    "agent-router-merge", "agent-router-session",
                    "agent-router-task-state", "agent-router-trending",
                    "agent-router-media", "agent-router-worklog"
                ]
            },
            "codex": { "enabled": true, "root": s(home.join(".codex").join("sessions")) },
            "workbuddy": { "enabled": false }
        }
    })
}

/// Recursive override: objects merge, everything else replaces (Python `_deep_merge`).
pub fn deep_merge(base: &mut Value, patch: &Value) {
    match (base, patch) {
        (Value::Object(base_map), Value::Object(patch_map)) => {
            for (key, value) in patch_map {
                match base_map.get_mut(key) {
                    Some(existing) if existing.is_object() && value.is_object() => {
                        deep_merge(existing, value)
                    }
                    _ => {
                        base_map.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        (base, patch) => *base = patch.clone(),
    }
}

pub fn load(path: &Path) -> Value {
    let mut config = default_config();
    if let Ok(text) = std::fs::read_to_string(path) {
        if let Ok(data) = serde_json::from_str::<Value>(&text) {
            deep_merge(&mut config, &data);
        }
    }
    config
}

pub fn save(path: &Path, config: &Value) -> anyhow::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let text = serde_json::to_string_pretty(config)?;
    std::fs::write(path, text)?;
    Ok(())
}

/// 返回稳定的安装 ID，用于隔离各设备的 `capture_id` 命名空间。
/// 首次缺失时生成 UUID v4 并写回配置；已有配置缺字段时自动补齐。
pub fn ensure_install_id(path: &Path) -> anyhow::Result<String> {
    let mut config = load(path);
    if let Some(id) = config
        .get("install_id")
        .and_then(|value| value.as_str())
        .filter(|text| !text.trim().is_empty())
    {
        return Ok(id.to_string());
    }
    let id = uuid::Uuid::new_v4().to_string();
    object_mut(&mut config).insert("install_id".into(), json!(id));
    save(path, &config)?;
    Ok(id)
}

pub fn get_i64(config: &Value, key: &str, fallback: i64) -> i64 {
    config.get(key).and_then(|v| v.as_i64()).unwrap_or(fallback)
}

pub fn get_str(config: &Value, key: &str, fallback: &str) -> String {
    config
        .get(key)
        .and_then(|v| v.as_str())
        .unwrap_or(fallback)
        .to_string()
}

pub fn get_bool(config: &Value, key: &str, fallback: bool) -> bool {
    config.get(key).and_then(|v| v.as_bool()).unwrap_or(fallback)
}

pub fn get_list(config: &Value, key: &str) -> Vec<String> {
    config
        .get(key)
        .and_then(|v| v.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default()
}

/// `HH:MM` → (hour, minute), falling back to 22:00 like the Python parser.
pub fn parse_time(value: &str) -> (u32, u32) {
    let mut parts = value.split(':');
    let hour = parts.next().and_then(|v| v.trim().parse::<u32>().ok());
    let minute = parts.next().and_then(|v| v.trim().parse::<u32>().ok());
    match (hour, minute) {
        (Some(h), Some(m)) if h <= 23 && m <= 59 => (h, m),
        _ => (22, 0),
    }
}

pub fn object_mut(value: &mut Value) -> &mut Map<String, Value> {
    if !value.is_object() {
        *value = Value::Object(Map::new());
    }
    value.as_object_mut().expect("object")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn install_id_is_generated_once_and_backfilled() {
        let dir = std::env::temp_dir().join(format!("worklog-cfg-{}", uuid::Uuid::new_v4()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("config.json");

        let first = ensure_install_id(&path).unwrap();
        assert!(!first.is_empty());
        // 再次调用保持稳定
        assert_eq!(ensure_install_id(&path).unwrap(), first);

        // 已有配置缺字段时自动补齐，且不影响原有字段
        save(&path, &json!({"sample_seconds": 42})).unwrap();
        let backfilled = ensure_install_id(&path).unwrap();
        assert!(!backfilled.is_empty());
        let cfg = load(&path);
        assert_eq!(get_str(&cfg, "install_id", ""), backfilled);
        assert_eq!(get_i64(&cfg, "sample_seconds", 0), 42);

        let _ = std::fs::remove_dir_all(&dir);
    }
}
