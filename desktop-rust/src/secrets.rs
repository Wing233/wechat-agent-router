use anyhow::{Context, Result};
use serde_json::{Map, Value};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Clone)]
pub struct SecretStore {
    path: PathBuf,
}

impl SecretStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    fn load(&self) -> Map<String, Value> {
        fs::read_to_string(&self.path)
            .ok()
            .and_then(|text| serde_json::from_str::<Map<String, Value>>(&text).ok())
            .unwrap_or_default()
    }

    fn save(&self, data: &Map<String, Value>) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let tmp = self.path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_string_pretty(data)?)?;
        fs::rename(tmp, &self.path)?;
        Ok(())
    }

    pub fn get(&self, key: &str) -> String {
        // Rust 版先兼容 Python 的 plain/dpapi 存储格式：dpapi 值在 Windows 上仍需原生 DPAPI 解码，这里返回空并提示重新保存。
        let map = self.load();
        let value = map.get(key).and_then(|v| v.as_str()).unwrap_or("");
        if let Some(payload) = value.strip_prefix("plain:") {
            use base64::Engine;
            base64::engine::general_purpose::STANDARD
                .decode(payload)
                .ok()
                .map(|bytes| String::from_utf8_lossy(&bytes).to_string())
                .unwrap_or_default()
        } else {
            String::new()
        }
    }

    pub fn set(&self, key: &str, value: &str) -> Result<()> {
        use base64::Engine;
        let mut map = self.load();
        if value.is_empty() {
            map.remove(key);
        } else {
            let payload = format!("plain:{}", base64::engine::general_purpose::STANDARD.encode(value.as_bytes()));
            map.insert(key.to_string(), Value::String(payload));
        }
        self.save(&map)
            .with_context(|| format!("保存密钥失败：{}", self.path.display()))
    }

    pub fn has(&self, key: &str) -> bool {
        !self.get(key).is_empty()
    }
}

pub fn read_path(path: &Path) -> String {
    fs::read_to_string(path).unwrap_or_default()
}
