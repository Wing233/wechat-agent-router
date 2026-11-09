//! Daily worklog service: sample → local OCR → text note → LYPH upload.
//!
//! This replaces the LYPH screenshot collector that shipped in the Rust MVP.
//! It intentionally never uploads pixels: the report is one text-only `note`
//! capture per day, which is what the server-side publisher accepts.

use crate::logs::EventLog;
use crate::secrets::SecretStore;
use crate::worklog::config;
use crate::worklog::db::{DayRow, WorklogDb};
use crate::worklog::note::build_note;
use crate::worklog::sample::sample_once;
use crate::worklog::sources;
use crate::worklog::upload::client_for;
use serde::Serialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const DONE_STATUSES: [&str; 4] = ["uploaded", "published", "conflict", "empty"];

#[derive(Clone, Debug, Default, Serialize)]
pub struct JobState {
    pub kind: String,
    pub target: String,
    pub started_at: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    pub error: String,
}

pub struct WorklogService {
    root: PathBuf,
    config_path: PathBuf,
    log_path: PathBuf,
    db: WorklogDb,
    log: Arc<EventLog>,
    stop: Arc<AtomicBool>,
    wake: Arc<(Mutex<bool>, Condvar)>,
    handle: Mutex<Option<std::thread::JoinHandle<()>>>,
    job: Arc<Mutex<Option<JobState>>>,
    last_error: Arc<Mutex<String>>,
    started_at: Arc<Mutex<i64>>,
}

impl WorklogService {
    pub fn new(root: PathBuf, log: Arc<EventLog>) -> Self {
        let _ = std::fs::create_dir_all(&root);
        let _ = std::fs::create_dir_all(root.join("notes"));
        let config_path = root.join("config.json");
        let log_path = root.join("collector.log");
        let db = WorklogDb::new(root.join("worklog.sqlite3"));
        let service = Self {
            root,
            config_path,
            log_path,
            db,
            log,
            stop: Arc::new(AtomicBool::new(false)),
            wake: Arc::new((Mutex::new(false), Condvar::new())),
            handle: Mutex::new(None),
            job: Arc::new(Mutex::new(None)),
            last_error: Arc::new(Mutex::new(String::new())),
            started_at: Arc::new(Mutex::new(0)),
        };
        // Import legacy data before creating our own SQLite file, otherwise the
        // migration would see an empty database and skip the copy.
        service.migrate_legacy();
        let _ = service.db.init();
        service.ensure_config();
        service
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    fn secrets(&self) -> SecretStore {
        SecretStore::new(self.root.join("secrets.json"))
    }

    fn ensure_config(&self) {
        if self.config_path.is_file() {
            return;
        }
        let mut value = config::default_config();
        if let Some(obj) = value.as_object_mut() {
            obj.insert(
                "worklog_dir".into(),
                json!(self.root.join("notes").to_string_lossy()),
            );
        }
        let _ = config::save(&self.config_path, &value);
    }

    /// Copy a legacy worklog directory once so old days stay visible.
    ///
    /// Candidates: the old standalone collector, the packaged Python desktop
    /// (`<exe>/data/worklog`) and the source-mode Python desktop
    /// (`desktop/.runtime/worklog`).
    fn migrate_legacy(&self) {
        if self.root.join("worklog.sqlite3").exists() {
            return;
        }
        let mut candidates: Vec<PathBuf> = vec![config::local_app_data().join("WeChatAgentWorklog")];
        let mut bases: Vec<PathBuf> = Vec::new();
        if let Ok(exe) = std::env::current_exe() {
            bases.extend(exe.ancestors().take(6).map(PathBuf::from));
        }
        if let Ok(cwd) = std::env::current_dir() {
            bases.extend(cwd.ancestors().take(6).map(PathBuf::from));
        }
        for base in &bases {
            candidates.push(base.join("data").join("worklog"));
            candidates.push(base.join("desktop").join(".runtime").join("worklog"));
        }
        candidates.dedup();
        for old in candidates {
            if !old.is_dir() || old == self.root {
                continue;
            }
            let mut copied = false;
            for (from, to) in [
                ("config.json", self.config_path.clone()),
                ("worklog.sqlite3", self.root.join("worklog.sqlite3")),
                ("secrets.json", self.root.join("secrets.json")),
            ] {
                let source = old.join(from);
                if source.is_file() && !to.exists() {
                    if std::fs::copy(&source, &to).is_ok() {
                        copied = true;
                    }
                }
            }
            if copied {
                self.log
                    .add("INFO", &format!("已迁移旧工作日志数据：{}", old.display()));
                return;
            }
        }
    }

    pub fn config(&self) -> Value {
        config::load(&self.config_path)
    }

    fn save_config(&self, value: &Value) -> anyhow::Result<()> {
        config::save(&self.config_path, value)
    }

    fn token(&self) -> String {
        self.secrets().get("device_token")
    }

    pub fn running(&self) -> bool {
        self.handle
            .lock()
            .map(|guard| guard.is_some() && !self.stop.load(Ordering::Relaxed))
            .unwrap_or(false)
    }

    fn set_error(&self, message: &str) {
        *self.last_error.lock().unwrap() = message.chars().take(500).collect();
    }

    // --- lifecycle ------------------------------------------------------
    pub fn start(&self) -> anyhow::Result<Value> {
        if self.running() {
            return Ok(self.status());
        }
        self.stop.store(false, Ordering::Relaxed);
        self.set_error("");
        *self.started_at.lock().unwrap() = now_secs();
        let stop = self.stop.clone();
        let wake = self.wake.clone();
        let root = self.root.clone();
        let config_path = self.config_path.clone();
        let log = self.log.clone();
        let last_error = self.last_error.clone();
        let handle = std::thread::Builder::new()
            .name("worklog".into())
            .spawn(move || {
                let db = WorklogDb::new(root.join("worklog.sqlite3"));
                let secrets = SecretStore::new(root.join("secrets.json"));
                let _ = db.init();
                catch_up(&db, &config_path, &secrets, &log, &last_error);
                loop {
                    if stop.load(Ordering::Relaxed) {
                        break;
                    }
                    let cfg = config::load(&config_path);
                    if let Ok(paused) = db.state_get("local_paused") {
                        if paused.as_deref() == Some("true") {
                            wait(&wake, 5);
                            continue;
                        }
                    }
                    if let Err(error) = sample_once(&db, &cfg) {
                        *last_error.lock().unwrap() = error.to_string();
                        log.add("WARN", &format!("工作日志采样失败：{error}"));
                    }
                    maybe_finalize_today(&db, &cfg, &secrets, &log, &last_error);
                    let _ = db.prune(30);

                    let interval = config::get_i64(&cfg, "sample_seconds", 30).clamp(5, 600);
                    wait(&wake, interval);
                }
            });
        match handle {
            Ok(handle) => {
                *self.handle.lock().unwrap() = Some(handle);
                self.log.add("INFO", "工作日志采集已启动");
                Ok(self.status())
            }
            Err(error) => {
                self.set_error(&error.to_string());
                Err(error.into())
            }
        }
    }

    pub fn stop(&self) -> Value {
        self.stop.store(true, Ordering::Relaxed);
        self.wake.1.notify_all();
        if let Some(handle) = self.handle.lock().unwrap().take() {
            let _ = handle.join();
        }
        self.log.add("INFO", "工作日志采集已停止");
        self.status()
    }

    pub fn restart(&self) -> Value {
        self.stop();
        let _ = self.start();
        self.status()
    }

    // --- manual actions -------------------------------------------------
    pub fn capture_now(&self) -> Value {
        let cfg = self.config();
        match sample_once(&self.db, &cfg) {
            Ok(()) => self.status(),
            Err(error) => {
                self.set_error(&error.to_string());
                self.status()
            }
        }
    }

    pub fn sync_now(&self) -> Value {
        sources::clear_caches();
        catch_up(
            &self.db,
            &self.config_path,
            &self.secrets(),
            &self.log,
            &self.last_error,
        );
        self.status()
    }

    pub fn action(&self, command: &str) -> anyhow::Result<Value> {
        match command {
            "capture" => Ok(self.capture_now()),
            "sync" => Ok(self.sync_now()),
            other => anyhow::bail!("未知采集动作：{other}"),
        }
    }

    pub fn pause(&self, paused: bool) -> anyhow::Result<Value> {
        self.db
            .state_set("local_paused", if paused { "true" } else { "false" })?;
        Ok(self.status())
    }

    // --- report jobs ----------------------------------------------------
    fn job_busy(&self) -> bool {
        let guard = self.job.lock().unwrap();
        match guard.as_ref() {
            Some(job) if job.result.is_none() && job.error.is_empty() => {
                now_secs().saturating_sub(job.started_at) < 1800
            }
            _ => false,
        }
    }

    fn start_job<F>(&self, kind: &str, target: &str, work: F) -> anyhow::Result<Value>
    where
        F: FnOnce() -> anyhow::Result<Value> + Send + 'static,
    {
        if self.job_busy() {
            anyhow::bail!("已有工作日志任务在运行");
        }
        *self.job.lock().unwrap() = Some(JobState {
            kind: kind.to_string(),
            target: target.to_string(),
            started_at: now_secs(),
            result: None,
            error: String::new(),
        });
        let job = self.job.clone();
        let log = self.log.clone();
        let last_error = self.last_error.clone();
        let kind_owned = kind.to_string();
        std::thread::Builder::new()
            .name("worklog-job".into())
            .spawn(move || {
                let outcome = work();
                {
                    let mut guard = job.lock().unwrap();
                    if let Some(slot) = guard.as_mut() {
                        match outcome {
                            Ok(result) => slot.result = Some(result),
                            Err(error) => {
                                slot.error = error.to_string().chars().take(500).collect();
                                *last_error.lock().unwrap() = slot.error.clone();
                            }
                        }
                    }
                }
                let guard = job.lock().unwrap();
                match guard.as_ref() {
                    Some(state) if state.error.is_empty() => {
                        log.add("INFO", &format!("工作日志任务完成：{kind_owned}"));
                    }
                    Some(state) => {
                        log.add(
                            "ERROR",
                            &format!("工作日志任务失败：{kind_owned} {}", state.error),
                        );
                    }
                    None => {}
                }
            })?;
        Ok(self.status())
    }

    pub fn finalize(&self, day: &str) -> anyhow::Result<Value> {
        let day = if day.trim().is_empty() {
            chrono::Local::now().format("%Y-%m-%d").to_string()
        } else {
            day.trim().to_string()
        };
        let root = self.root.clone();
        let config_path = self.config_path.clone();
        let target = day.clone();
        self.start_job("finalize", &target, move || {
            let db = WorklogDb::new(root.join("worklog.sqlite3"));
            let secrets = SecretStore::new(root.join("secrets.json"));
            let _ = db.init();
            finalize_day(&db, &config_path, &secrets, &day, false)
        })
    }

    pub fn backfill(&self, days: u64) -> anyhow::Result<Value> {
        let days = days.clamp(1, 90);
        let root = self.root.clone();
        let config_path = self.config_path.clone();
        self.start_job("backfill", &days.to_string(), move || {
            let db = WorklogDb::new(root.join("worklog.sqlite3"));
            let secrets = SecretStore::new(root.join("secrets.json"));
            let _ = db.init();
            sources::clear_caches();
            let today = chrono::Local::now().date_naive();
            let mut processed = 0u64;
            for offset in 1..=days {
                let day = (today - chrono::Duration::days(offset as i64))
                    .format("%Y-%m-%d")
                    .to_string();
                // One broken day must not stop the whole backfill.
                if finalize_day(&db, &config_path, &secrets, &day, false).is_ok() {
                    processed += 1;
                }
            }
            Ok(json!({"days": days, "processed": processed}))
        })
    }

    // --- credentials ----------------------------------------------------
    pub fn pair(&self, base_url: &str, device_token: &str) -> anyhow::Result<Value> {
        let token = device_token.trim();
        if token.is_empty() {
            anyhow::bail!("设备 token 不能为空");
        }
        let mut cfg = self.config();
        if !base_url.trim().is_empty() {
            if let Some(obj) = cfg.as_object_mut() {
                obj.insert("lyph_base_url".into(), json!(base_url.trim()));
            }
            self.save_config(&cfg)?;
        }
        self.secrets().set("device_token", token)?;
        if self.running() {
            self.restart();
        }
        Ok(self.status())
    }

    pub fn clear_token(&self) -> anyhow::Result<Value> {
        self.secrets().set("device_token", "")?;
        if self.running() {
            self.restart();
        }
        Ok(self.status())
    }

    pub fn update_config(&self, payload: &Value) -> anyhow::Result<Value> {
        let mut cfg = self.config();
        {
            let obj = match cfg.as_object_mut() {
                Some(obj) => obj,
                None => anyhow::bail!("工作日志配置损坏"),
            };
            for (key, min, max) in [
                ("sample_seconds", 5i64, 600i64),
                ("idle_skip_seconds", 60, 7200),
                ("min_note_chars", 0, 16000),
                ("ocr_max_chars", 0, 16000),
                ("ocr_lines_per_app", 1, 500),
            ] {
                if let Some(value) = payload.get(key) {
                    let number = value
                        .as_i64()
                        .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
                        .ok_or_else(|| anyhow::anyhow!("{key} 必须是数字"))?;
                    obj.insert(key.into(), json!(number.clamp(min, max)));
                }
            }
            if let Some(value) = payload.get("screenshot_enabled") {
                let enabled = value
                    .as_bool()
                    .or_else(|| {
                        value.as_str().map(|text| {
                            matches!(text.to_lowercase().as_str(), "1" | "true" | "yes" | "on")
                        })
                    })
                    .unwrap_or(true);
                obj.insert("screenshot_enabled".into(), json!(enabled));
            }
            if let Some(value) = payload.get("ocr_language") {
                let language = value.as_str().unwrap_or("").trim();
                obj.insert(
                    "ocr_language".into(),
                    json!(if language.is_empty() { "zh-CN" } else { language }),
                );
            }
            if let Some(value) = payload.get("finalize_time") {
                let text = value.as_str().unwrap_or("").trim();
                let (hour, minute) = parse_clock(text)?;
                obj.insert(
                    "finalize_time".into(),
                    json!(format!("{hour:02}:{minute:02}")),
                );
            }
            if let Some(value) = payload.get("worklog_dir") {
                obj.insert(
                    "worklog_dir".into(),
                    json!(value.as_str().unwrap_or("").trim()),
                );
            }
        }
        self.save_config(&cfg)?;
        if self.running() {
            self.restart();
        }
        Ok(self.status())
    }

    // --- status ---------------------------------------------------------
    pub fn status(&self) -> Value {
        let cfg = self.config();
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        let samples_today = self.db.samples_count(&today).unwrap_or(0);
        let days = self.db.recent_days(10).unwrap_or_default();
        let last_upload = days
            .iter()
            .find(|row| matches!(row.status.as_str(), "uploaded" | "published"))
            .map(day_json);
        let (hour, minute) = config::parse_time(&config::get_str(&cfg, "finalize_time", "22:00"));
        let next_finalize = next_finalize_time(hour, minute);
        let job = self.job.lock().unwrap().clone();
        let started = *self.started_at.lock().unwrap();
        let notes_dir = config::get_str(&cfg, "worklog_dir", "");
        json!({
            "running": self.running(),
            "paused": self
                .db
                .state_get("local_paused")
                .ok()
                .flatten()
                .as_deref()
                == Some("true"),
            "started_at": if started > 0 { json!(started) } else { Value::Null },
            "device_configured": !self.token().is_empty(),
            "config": {
                "sample_seconds": config::get_i64(&cfg, "sample_seconds", 30),
                "idle_skip_seconds": config::get_i64(&cfg, "idle_skip_seconds", 300),
                "finalize_time": config::get_str(&cfg, "finalize_time", "22:00"),
                "min_note_chars": config::get_i64(&cfg, "min_note_chars", 100),
                "worklog_dir": notes_dir,
                "screenshot_enabled": config::get_bool(&cfg, "screenshot_enabled", true),
                "ocr_language": config::get_str(&cfg, "ocr_language", "zh-CN"),
                "ocr_max_chars": config::get_i64(&cfg, "ocr_max_chars", 4000),
                "ocr_lines_per_app": config::get_i64(&cfg, "ocr_lines_per_app", 30)
            },
            "today": today,
            "samples_today": samples_today,
            "days": days.iter().map(day_json).collect::<Vec<_>>(),
            "last_upload": last_upload,
            "next_finalize": next_finalize,
            "job": job.map(|value| serde_json::to_value(value).unwrap_or(Value::Null)),
            "last_error": self.last_error.lock().unwrap().clone(),
            "paths": {
                "data": self.root.to_string_lossy(),
                "config": self.config_path.to_string_lossy(),
                "log": self.log_path.to_string_lossy(),
                "notes": config::get_str(&self.config(), "worklog_dir", "")
            }
        })
    }

    /// Manual note shim kept for the legacy `/api/worklog/note` route.
    pub fn note(&self, text: &str) -> anyhow::Result<Value> {
        if text.trim().is_empty() {
            anyhow::bail!("内容不能为空");
        }
        let day = chrono::Local::now().format("%Y-%m-%d").to_string();
        self.db.save_day(&day, text, "", "manual")?;
        Ok(json!({"day": day, "saved": true}))
    }
}

fn day_json(row: &DayRow) -> Value {
    json!({
        "day": row.day,
        "status": row.status,
        "chars": row.chars,
        "updated_at": row.updated_at
    })
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs() as i64)
        .unwrap_or(0)
}

fn parse_clock(text: &str) -> anyhow::Result<(u32, u32)> {
    let mut parts = text.split(':');
    let hour = parts
        .next()
        .and_then(|value| value.trim().parse::<u32>().ok())
        .ok_or_else(|| anyhow::anyhow!("采集时间格式应为 HH:MM"))?;
    let minute = parts
        .next()
        .and_then(|value| value.trim().parse::<u32>().ok())
        .ok_or_else(|| anyhow::anyhow!("采集时间格式应为 HH:MM"))?;
    if hour > 23 || minute > 59 {
        anyhow::bail!("采集时间格式应为 HH:MM");
    }
    Ok((hour, minute))
}

fn wait(wake: &Arc<(Mutex<bool>, Condvar)>, seconds: i64) {
    let (lock, cvar) = &**wake;
    let guard = lock.lock().unwrap();
    let _ = cvar.wait_timeout(guard, Duration::from_secs(seconds.max(1) as u64));
}

/// Next finalize timestamp in local `YYYY-MM-DDTHH:MM` form.
fn next_finalize_time(hour: u32, minute: u32) -> String {
    use chrono::Timelike;
    let now = chrono::Local::now();
    let mut target = now
        .with_hour(hour)
        .and_then(|value| value.with_minute(minute))
        .and_then(|value| value.with_second(0))
        .and_then(|value| value.with_nanosecond(0))
        .unwrap_or(now);
    if target <= now {
        target += chrono::Duration::days(1);
    }
    target.format("%Y-%m-%dT%H:%M").to_string()
}

fn maybe_finalize_today(
    db: &WorklogDb,
    cfg: &Value,
    secrets: &SecretStore,
    log: &EventLog,
    last_error: &Arc<Mutex<String>>,
) {
    use chrono::Timelike;
    let now = chrono::Local::now();
    let (hour, minute) = config::parse_time(&config::get_str(cfg, "finalize_time", "22:00"));
    if (now.hour(), now.minute()) < (hour, minute) {
        return;
    }
    let day = now.format("%Y-%m-%d").to_string();
    let status = db
        .day(&day)
        .ok()
        .flatten()
        .map(|row| row.status)
        .unwrap_or_default();
    if DONE_STATUSES.contains(&status.as_str()) {
        return;
    }
    if let Err(error) = finalize_with_config(db, cfg, secrets, &day, false) {
        *last_error.lock().unwrap() = error.to_string();
        log.add("WARN", &format!("工作日志汇总失败：{error}"));
    }
}

fn catch_up(
    db: &WorklogDb,
    config_path: &Path,
    secrets: &SecretStore,
    log: &EventLog,
    last_error: &Arc<Mutex<String>>,
) {
    use chrono::Timelike;
    let cfg = config::load(config_path);
    let (hour, minute) = config::parse_time(&config::get_str(&cfg, "finalize_time", "22:00"));
    let now = chrono::Local::now();
    let reached = (now.hour(), now.minute()) >= (hour, minute);
    let newest = if reached {
        now.date_naive()
    } else {
        now.date_naive() - chrono::Duration::days(1)
    };
    for offset in 0..7i64 {
        let day = (newest - chrono::Duration::days(offset))
            .format("%Y-%m-%d")
            .to_string();
        let status = db
            .day(&day)
            .ok()
            .flatten()
            .map(|row| row.status)
            .unwrap_or_default();
        if DONE_STATUSES.contains(&status.as_str()) {
            continue;
        }
        if !db.has_samples(&day).unwrap_or(false) {
            continue;
        }
        if let Err(error) = finalize_with_config(db, &cfg, secrets, &day, false) {
            *last_error.lock().unwrap() = error.to_string();
            log.add("WARN", &format!("补跑 {day} 失败：{error}"));
        }
    }
}

fn finalize_day(
    db: &WorklogDb,
    config_path: &Path,
    secrets: &SecretStore,
    day: &str,
    force: bool,
) -> anyhow::Result<Value> {
    let cfg = config::load(config_path);
    finalize_with_config(db, &cfg, secrets, day, force)
}

fn finalize_with_config(
    db: &WorklogDb,
    cfg: &Value,
    secrets: &SecretStore,
    day: &str,
    force: bool,
) -> anyhow::Result<Value> {
    sources::clear_caches();
    let existing = db.day(day)?;
    if let Some(row) = existing.as_ref() {
        if DONE_STATUSES.contains(&row.status.as_str()) && !force {
            return Ok(json!({"day": day, "status": "skipped", "reason": "already processed"}));
        }
    }
    let note = existing
        .as_ref()
        .map(|row| row.note.clone())
        .filter(|text| !text.trim().is_empty())
        .unwrap_or_else(|| {
            build_note(
                day,
                &db.samples(day).unwrap_or_default(),
                &db.ocr_samples(day).unwrap_or_default(),
                &sources::collect_evidence(day, cfg),
                cfg,
            )
        });
    if note.trim().is_empty() {
        db.save_day(day, "", "", "empty")?;
        return Ok(json!({"day": day, "status": "empty"}));
    }
    let minimum = config::get_i64(cfg, "min_note_chars", 100).max(0) as usize;
    if note.trim().chars().count() < minimum && !force {
        db.save_day(day, &note, "", "empty")?;
        return Ok(json!({"day": day, "status": "empty", "reason": "below min_note_chars"}));
    }

    let worklog_dir = config::get_str(cfg, "worklog_dir", "");
    if !worklog_dir.trim().is_empty() {
        let dir = PathBuf::from(&worklog_dir);
        let _ = std::fs::create_dir_all(&dir);
        let _ = std::fs::write(dir.join(format!("{day}.md")), &note);
    }

    let token = secrets.get("device_token");
    let Some(client) = client_for(cfg, &token) else {
        db.save_day(day, &note, "", "local")?;
        return Ok(json!({"day": day, "status": "local", "chars": note.chars().count()}));
    };
    let (hour, minute) = config::parse_time(&config::get_str(cfg, "finalize_time", "22:00"));
    let result = client.upload_note(day, &note, hour, minute)?;
    let conflict = result
        .get("conflict")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    db.save_day(
        day,
        &note,
        &format!("worklog-{day}-v1"),
        if conflict { "conflict" } else { "uploaded" },
    )?;
    Ok(json!({
        "day": day,
        "status": if conflict { "conflict" } else { "uploaded" },
        "duplicate": result.get("duplicate").cloned().unwrap_or(json!(false)),
        "chars": note.chars().count()
    }))
}
