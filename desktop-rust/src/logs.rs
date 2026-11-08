use chrono::TimeZone;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

pub struct EventLog {
    items: Mutex<VecDeque<Value>>,
    seq: Mutex<u64>,
    file: Option<PathBuf>,
    source: String,
}

impl EventLog {
    pub fn new(capacity: usize, file: Option<PathBuf>, source: &str) -> Self {
        Self {
            items: Mutex::new(VecDeque::with_capacity(capacity)),
            seq: Mutex::new(0),
            file,
            source: source.to_string(),
        }
    }

    pub fn add(&self, level: &str, message: &str) -> Value {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
        let mut seq = self.seq.lock().unwrap();
        *seq += 1;
        let record = json!({
            "id": *seq,
            "ts": chrono::Local.timestamp_opt(now as i64, 0).single().map(|t| t.format("%Y-%m-%dT%H:%M:%S").to_string()).unwrap_or_default(),
            "level": level.to_uppercase(),
            "source": self.source,
            "message": message.chars().take(6000).collect::<String>()
        });
        {
            let mut items = self.items.lock().unwrap();
            items.push_back(record.clone());
            while items.len() > 800 {
                items.pop_front();
            }
        }
        if let Some(path) = &self.file {
            if let Some(parent) = path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
                let _ = writeln!(f, "{} [{}] {}", record["ts"], level, message);
            }
        }
        record
    }

    pub fn since(&self, after: u64) -> Vec<Value> {
        self.items
            .lock()
            .unwrap()
            .iter()
            .filter(|item| item.get("id").and_then(|v| v.as_u64()).unwrap_or(0) > after)
            .cloned()
            .collect()
    }

    pub fn clear(&self) {
        self.items.lock().unwrap().clear();
    }
}
