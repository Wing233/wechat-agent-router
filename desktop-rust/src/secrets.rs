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
        let map = self.load();
        let value = map.get(key).and_then(|v| v.as_str()).unwrap_or("");
        if let Some(payload) = value.strip_prefix("plain:") {
            decode_plain(payload)
        } else if let Some(payload) = value.strip_prefix("dpapi:") {
            decode_dpapi(payload)
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
            #[cfg(windows)]
            let payload = match encode_dpapi(value.as_bytes()) {
                Ok(encoded) => format!("dpapi:{encoded}"),
                Err(_) => format!("plain:{}", base64::engine::general_purpose::STANDARD.encode(value.as_bytes())),
            };
            #[cfg(not(windows))]
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

fn decode_plain(payload: &str) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD
        .decode(payload)
        .ok()
        .map(|bytes| String::from_utf8_lossy(&bytes).to_string())
        .unwrap_or_default()
}

#[cfg(windows)]
fn decode_dpapi(payload: &str) -> String {
    use base64::Engine;
    let bytes = match base64::engine::general_purpose::STANDARD.decode(payload) {
        Ok(bytes) => bytes,
        Err(_) => return String::new(),
    };
    match windows_dpapi::decrypt_data(&bytes, windows_dpapi::Scope::User, None) {
        Ok(decrypted) => String::from_utf8_lossy(&decrypted).to_string(),
        Err(_) => String::new(),
    }
}

#[cfg(not(windows))]
fn decode_dpapi(_payload: &str) -> String {
    String::new()
}

#[cfg(windows)]
fn encode_dpapi(data: &[u8]) -> Result<String> {
    use base64::Engine;
    let encrypted = windows_dpapi::encrypt_data(data, windows_dpapi::Scope::User, None)
        .context("DPAPI 加密失败")?;
    Ok(base64::engine::general_purpose::STANDARD.encode(encrypted))
}

#[cfg(not(windows))]
fn encode_dpapi(_data: &[u8]) -> Result<String> {
    anyhow::bail!("DPAPI 仅 Windows 可用")
}
