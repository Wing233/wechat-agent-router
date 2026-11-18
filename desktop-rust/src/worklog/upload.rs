//! Minimal LYPH device client: uploads one text-only `note` capture per day.

use crate::worklog::config::get_str;
use anyhow::{Context, Result};
use reqwest::blocking::Client;
use serde_json::{json, Value};
use std::time::Duration;

/// `capture_id` 命名空间按安装 ID 隔离，避免多设备/多次上传互相 409。
/// 去重与合并交给 LYPH 端用大模型处理。
pub fn capture_id(install_id: &str, day: &str) -> String {
    format!("worklog-{install_id}-{day}-v1")
}

pub struct LyphClient {
    base_url: String,
    device_token: String,
    install_id: String,
    client: Client,
}

impl LyphClient {
    pub fn new(base_url: &str, device_token: &str, install_id: &str) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(45))
            .build()
            .unwrap_or_else(|_| Client::new());
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            device_token: device_token.to_string(),
            install_id: install_id.to_string(),
            client,
        }
    }

    fn auth(&self) -> String {
        format!("Bearer {}", self.device_token)
    }

    /// 与实际上报使用的 `capture_id` 保持一致（本地展示用）。
    pub fn capture_id(&self, day: &str) -> String {
        capture_id(&self.install_id, day)
    }

    /// `POST /api/v1/device/captures` with `source_kind=note`; never sends pixels.
    pub fn upload_note(
        &self,
        day: &str,
        text: &str,
        captured_hour: u32,
        captured_minute: u32,
    ) -> Result<Value> {
        let capture_id = self.capture_id(day);
        let captured_at = format!(
            "{day}T{captured_hour:02}:{captured_minute:02}:00+08:00"
        );
        let form = reqwest::blocking::multipart::Form::new()
            .text("capture_id", capture_id)
            .text("captured_at", captured_at)
            .text("source_kind", "note")
            .text("text", text.to_string());
        let response = self
            .client
            .post(format!("{}/api/v1/device/captures", self.base_url))
            .header("Authorization", self.auth())
            .multipart(form)
            .send()
            .context("上传日报失败：无法连接 LYPH")?;
        let status = response.status();
        if status.as_u16() == 409 {
            return Ok(json!({"duplicate": true, "conflict": true, "status": 409}));
        }
        if !status.is_success() {
            let body = response.text().unwrap_or_default();
            anyhow::bail!(
                "上传日报失败：HTTP {} {}",
                status.as_u16(),
                body.chars().take(300).collect::<String>()
            );
        }
        Ok(response.json::<Value>().unwrap_or(Value::Null))
    }
}

/// Build the client from the worklog config + DPAPI token.
/// `install_id` 由 `config::ensure_install_id` 持久化，这里只读取；异常兜底生成一次并缓存到 client。
pub fn client_for(config: &Value, token: &str) -> Option<LyphClient> {
    if token.trim().is_empty() {
        return None;
    }
    let base = get_str(config, "lyph_base_url", "https://123.57.153.49");
    let install_id = {
        let id = get_str(config, "install_id", "");
        if id.is_empty() {
            uuid::Uuid::new_v4().to_string()
        } else {
            id
        }
    };
    Some(LyphClient::new(&base, token.trim(), &install_id))
}

#[cfg(test)]
mod tests {
    use super::capture_id;

    #[test]
    fn capture_id_is_namespaced_by_install_id_and_stable() {
        let a = capture_id("device-A", "2026-11-17");
        assert_eq!(a, "worklog-device-A-2026-11-17-v1");
        assert_eq!(a, capture_id("device-A", "2026-11-17"));
        assert_ne!(a, capture_id("device-B", "2026-11-17"));
        assert_ne!(a, capture_id("device-A", "2026-11-18"));
    }
}
