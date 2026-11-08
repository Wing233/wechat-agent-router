use crate::logs::EventLog;
use crate::paths;
use anyhow::{Context, Result};
use chrono::Utc;
use lyph_collector_core::{api::HttpApi, Config, Engine, Store};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread;
use std::time::Duration;

pub struct CollectorService {
    root: PathBuf,
    log: Arc<EventLog>,
    running: Arc<AtomicBool>,
    handle: Mutex<Option<thread::JoinHandle<()>>>,
}

enum Work {
    Capture,
    Sync,
}

impl CollectorService {
    pub fn new(root: PathBuf, log: Arc<EventLog>) -> Self {
        let _ = std::fs::create_dir_all(&root);
        Self {
            root,
            log,
            running: Arc::new(AtomicBool::new(false)),
            handle: Mutex::new(None),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn running(&self) -> bool {
        self.running.load(Ordering::Relaxed)
    }

    fn store(&self) -> Result<Store> {
        Store::open(&self.root)
    }

    fn config(&self) -> Result<Config> {
        Config::load(&self.root)
    }

    pub fn status(&self) -> Value {
        let store = self.store();
        let config = self.config();
        let status = store.as_ref().ok().and_then(|s| s.status().ok());
        let pending = status.as_ref().map(|s| s.pending_count).unwrap_or(0);
        let confirmed = status.as_ref().map(|s| s.confirmed_count).unwrap_or(0);
        let capture_interval = status.as_ref().map(|s| s.capture_interval_seconds).unwrap_or(300);
        let running = self.running();
        json!({
            "running": running,
            "started_at": null,
            "device_configured": config.is_ok(),
            "config": {
                "sample_seconds": capture_interval,
                "idle_skip_seconds": 300,
                "finalize_time": "22:00",
                "worklog_dir": self.root.join("notes").to_string_lossy(),
                "screenshot_enabled": true,
                "ocr_language": "zh-CN",
                "ocr_max_chars": 4000,
                "ocr_lines_per_app": 30
            },
            "collector": {
                "pending_count": pending,
                "confirmed_count": confirmed,
                "cache_bytes": status.as_ref().map(|s| s.cache_bytes).unwrap_or(0),
                "capture_status": status.as_ref().map(|s| s.capture_status.clone()).unwrap_or_else(|| "waiting".into()),
                "last_capture_at": status.as_ref().and_then(|s| s.last_capture_at.clone()),
                "last_error": status.as_ref().and_then(|s| s.last_error.clone()),
                "revoked": status.as_ref().map(|s| s.revoked).unwrap_or(false),
                "local_paused": status.as_ref().map(|s| s.local_paused).unwrap_or(false),
                "remote_paused": status.as_ref().map(|s| s.remote_paused).unwrap_or(false)
            },
            "days": [],
            "paths": {
                "data": self.root.to_string_lossy(),
                "config": self.root.join("config.json").to_string_lossy(),
                "log": self.root.join("collector.log").to_string_lossy(),
                "notes": self.root.join("notes").to_string_lossy()
            },
            "next_finalize": null,
            "last_error": status.as_ref().and_then(|s| s.last_error.clone()).unwrap_or_default(),
            "job": null
        })
    }

    pub fn start(&self) -> Result<Value> {
        if self.running() {
            return Ok(self.status());
        }
        let config = self.config().context("尚未配对采集器：先在工作日志页保存设备 token")?;
        let store = self.store()?;
        let mut engine = Engine::new(store.clone(), config.clone(), HttpApi::new(config)?).context("初始化采集引擎失败")?;
        let running = self.running.clone();
        running.store(true, Ordering::Relaxed);
        let (tx, rx) = std::sync::mpsc::channel::<Work>();
        let handle = thread::spawn(move || {
            while running.load(Ordering::Relaxed) {
                while let Ok(work) = rx.try_recv() {
                    match work {
                        Work::Capture => {
                            let _ = engine.capture_now();
                        }
                        Work::Sync => {
                            let _ = store.make_due();
                            let _ = engine.refresh_config();
                        }
                    }
                }
                if let Err(error) = engine.tick() {
                    let _ = store.set_state("last_error", &error.to_string());
                }
                thread::sleep(Duration::from_millis(500));
            }
        });
        *self.handle.lock().unwrap() = Some(handle);
        self.log.add("INFO", "采集服务已启动");
        Ok(self.status())
    }

    pub fn stop(&self) -> Value {
        self.running.store(false, Ordering::Relaxed);
        if let Some(handle) = self.handle.lock().unwrap().take() {
            let _ = handle.join();
        }
        self.log.add("INFO", "采集服务已停止");
        self.status()
    }

    pub fn pause(&self, paused: bool) -> Result<Value> {
        self.store()?
            .set_state("local_paused", if paused { "true" } else { "false" })?;
        Ok(self.status())
    }

    pub fn action(&self, command: &str) -> Result<Value> {
        // This keeps parity with the Tauri collector commands; the HTTP compatibility layer maps
        // the legacy worklog buttons onto these integration seams.
        match command {
            "capture" => {
                let config = self.config()?;
                let store = self.store()?;
                let mut engine = Engine::new(store.clone(), config.clone(), HttpApi::new(config)?)?;
                engine.capture_now()?;
            }
            "sync" => {
                self.store()?.make_due()?;
            }
            _ => anyhow::bail!("未知采集动作"),
        }
        Ok(self.status())
    }

    pub fn note(&self, text: &str) -> Result<Value> {
        let config = self.config()?;
        let store = self.store()?;
        let status = store.status()?;
        if status.revoked {
            anyhow::bail!("设备已吊销");
        }
        let event = store.enqueue(
            "note",
            text,
            None,
            &Utc::now().to_rfc3339(),
            status.cache_limit_bytes.min(config.cache_limit_bytes),
        )?;
        Ok(json!({"capture_id": event.id, "cached": true}))
    }

    pub fn pair(&self, base_url: &str, device_token: &str) -> Result<Value> {
        if self.running() {
            anyhow::bail!("先停止采集再更换设备凭证");
        }
        let config = Config::new(base_url.to_string(), device_token.to_string())?;
        config.save(&self.root)?;
        self.store()?.set_state("revoked", "false")?;
        Ok(self.status())
    }
}
