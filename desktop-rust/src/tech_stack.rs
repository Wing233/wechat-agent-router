use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;

pub struct TechStackService {
    root: PathBuf,
}

impl TechStackService {
    pub fn new(root: PathBuf) -> Self {
        let _ = fs::create_dir_all(root.join("reports"));
        let _ = fs::create_dir_all(root.join("workbuddy-exports"));
        Self { root }
    }

    pub fn status(&self) -> Value {
        let report_path = self.root.join("reports/latest.json");
        let report = fs::read_to_string(&report_path)
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .unwrap_or(Value::Object(Default::default()));
        let config_path = self.root.join("config.json");
        let config = fs::read_to_string(&config_path)
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .unwrap_or(json!({
                "enabled": true,
                "weekly_day": 6,
                "weekly_time": "10:00",
                "project_roots": [],
                "output_dir": self.root.join("reports").to_string_lossy(),
                "workbuddy_exports": self.root.join("workbuddy-exports").to_string_lossy()
            }));
        let enabled = config.get("enabled").and_then(|value| value.as_bool()).unwrap_or(true);
        let weekly_day = config.get("weekly_day").and_then(|value| value.as_u64()).unwrap_or(6);
        let weekly_time = config.get("weekly_time").and_then(|value| value.as_str()).unwrap_or("10:00").to_string();
        let project_roots = config.get("project_roots").cloned().unwrap_or(json!([]));
        let technology_count = report.get("technologies").and_then(|value| value.as_array()).map(|items| items.len()).unwrap_or(0);
        let summary = report.get("summary").cloned().unwrap_or(json!({}));
        let public_baseline_status = summary.get("public_baseline_status").cloned().unwrap_or(json!(""));
        json!({
            "running": false,
            "enabled": enabled,
            "weekly_day": weekly_day,
            "weekly_time": weekly_time,
            "project_roots": project_roots,
            "technology_count": technology_count,
            "public_baseline_status": public_baseline_status,
            "next_run_at": null,
            "last_completed_at": report.get("generated_at").cloned().unwrap_or(Value::Null),
            "last_error": "",
            "summary": summary,
            "config": config,
            "report": report,
            "paths": {
                "report": self.root.join("reports/latest.html").to_string_lossy(),
                "reports": self.root.join("reports").to_string_lossy(),
                "exports": self.root.join("workbuddy-exports").to_string_lossy(),
                "config": config_path.to_string_lossy()
            }
        })
    }

    pub fn run_now(&self) -> Value {
        // Rust MVP: report reading/writing is preserved; the old Python collector can be invoked
        // during transition, then ported to a Rust collector behind this seam.
        let mut status = self.status();
        if let Some(obj) = status.as_object_mut() {
            obj.insert("last_run_requested".into(), json!(chrono::Utc::now().to_rfc3339()));
        }
        status
    }

    pub fn update_config(&self, payload: &Value) -> Value {
        let config_path = self.root.join("config.json");
        let mut config = self.status()["config"].clone();
        if let Some(obj) = payload.as_object() {
            if let Some(value) = obj.get("enabled") {
                config["enabled"] = value.clone();
            }
            if let Some(value) = obj.get("weekly_day") {
                config["weekly_day"] = value.clone();
            }
            if let Some(value) = obj.get("weekly_time") {
                config["weekly_time"] = value.clone();
            }
        }
        if let Some(parent) = config_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(&config_path, serde_json::to_string_pretty(&config).unwrap_or_default());
        self.status()
    }
}
