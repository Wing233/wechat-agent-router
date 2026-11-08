use serde_json::json;

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

pub fn check() -> serde_json::Value {
    status()
}

pub fn apply() -> serde_json::Value {
    json!({"started": false, "error": "Rust updater is not implemented yet; please use the Python release or GitHub Releases page"})
}
