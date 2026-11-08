use crate::agent::LocalAgent;
use crate::autostart;
use crate::collector::CollectorService;
use crate::config::{load_config, save_config, AgentConfig, Settings};
use crate::logs::EventLog;
use crate::paths;
use crate::secrets::SecretStore;
use crate::tech_stack::TechStackService;
use anyhow::{Context, Result};
use rand::RngCore;
use reqwest::blocking::Client;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub struct DesktopApp {
    pub config_path: PathBuf,
    pub data_dir: PathBuf,
    pub log: Arc<EventLog>,
    pub token: String,
    pub started_at: u64,
    pub config_error: String,
    pub settings: Settings,
    pub secrets: SecretStore,
    config: std::sync::Mutex<AgentConfig>,
    agent: std::sync::Mutex<Option<LocalAgent>>,
    service_client: Client,
}

impl DesktopApp {
    pub fn new(explicit_config: Option<String>) -> Result<Self> {
        let data_dir = paths::data_dir();
        std::fs::create_dir_all(&data_dir)?;
        let config_path = paths::config_path(explicit_config);
        let log = Arc::new(EventLog::new(800, Some(paths::log_file()), "desktop"));
        let mut token_bytes = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut token_bytes);
        let token = base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, token_bytes);
        let settings = Settings::load(&paths::settings_file());
        let secrets = SecretStore::new(paths::secrets_file());
        let (config, config_error) = match load_config(&config_path) {
            Ok(cfg) => (cfg, String::new()),
            Err(error) => {
                log.add("ERROR", &format!("本机配置读取失败：{error}"));
                (AgentConfig::default(), error.to_string())
            }
        };
        let service_client = Client::builder()
            .timeout(Duration::from_secs(15))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            config_path,
            data_dir,
            log,
            token,
            started_at: SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs(),
            config_error,
            settings,
            secrets,
            config: std::sync::Mutex::new(config),
            agent: std::sync::Mutex::new(None),
            service_client,
        })
    }

    pub fn config(&self) -> AgentConfig {
        self.config.lock().unwrap().clone()
    }

    pub fn config_for_ui(&self) -> Value {
        let cfg = self.config();
        let mut value = cfg.masked_json();
        if let Some(obj) = value.as_object_mut() {
            obj.insert("_meta".into(), json!({
                "config_path": self.config_path.to_string_lossy(),
                "config_error": self.config_error,
                "engine_checks": self.engine_checks(),
                "workspace_checks": self.workspace_checks()
            }));
        }
        value
    }

    fn engine_checks(&self) -> Value {
        let cfg = self.config();
        let mut map = serde_json::Map::new();
        for (name, command) in cfg.engines {
            let executable = command.first().map(|exe| which(exe.as_str())).unwrap_or_default();
            map.insert(name.clone(), json!({
                "command": command,
                "executable": executable,
                "found": !executable.is_empty()
            }));
        }
        Value::Object(map)
    }

    fn workspace_checks(&self) -> Value {
        let cfg = self.config();
        let mut map = serde_json::Map::new();
        for (alias, path) in cfg.workspaces {
            map.insert(alias, json!({"path": path, "exists": Path::new(&path).is_dir()}));
        }
        Value::Object(map)
    }

    pub fn agent_status(&self) -> Value {
        let guard = self.agent.lock().unwrap();
        if let Some(agent) = guard.as_ref() {
            let mut status = agent.snapshot();
            status["version"] = json!(paths::VERSION);
            status["config_error"] = json!(self.config_error);
            return status;
        }
        let cfg = self.config();
        json!({
            "status": if self.config_error.is_empty() { "stopped" } else { "config_error" },
            "running": false,
            "agent_id": cfg.agent_id,
            "router": cfg.router,
            "last_error": self.config_error,
            "last_seen": null,
            "started_at": null,
            "current": null,
            "tasks_done": 0,
            "last_task": null,
            "engines": cfg.engines.keys().collect::<Vec<_>>(),
            "workspaces": cfg.workspaces.keys().collect::<Vec<_>>(),
            "default_engine": cfg.default_engine,
            "default_workspace": cfg.default_workspace,
            "wait_seconds": cfg.wait_seconds,
            "poll_seconds": cfg.poll_seconds,
            "version": paths::VERSION,
            "config_error": self.config_error
        })
    }

    pub fn start_agent(&self) -> Result<Value> {
        if !self.config_error.is_empty() {
            anyhow::bail!("配置仍未修复：{}", self.config_error);
        }
        let cfg = self.config();
        if cfg.router.is_empty() {
            anyhow::bail!("请先填写 Router 地址");
        }
        let mut guard = self.agent.lock().unwrap();
        if let Some(agent) = guard.as_ref() {
            return Ok(agent.snapshot());
        }
        let agent = LocalAgent::new(cfg, self.log.clone())?;
        agent.start()?;
        let status = agent.snapshot();
        *guard = Some(agent);
        Ok(status)
    }

    pub fn stop_agent(&self) -> Value {
        if let Some(agent) = self.agent.lock().unwrap().take() {
            agent.stop();
        }
        self.agent_status()
    }

    pub fn test_engine(&self, engine: &str, workspace: &str) -> Result<Value> {
        let cfg = self.config();
        let agent = LocalAgent::new(cfg, self.log.clone())?;
        agent.test_engine(engine, workspace)
    }

    pub fn update_config(&self, payload: &Value) -> Result<Value> {
        let data = payload.get("config").cloned().unwrap_or_else(|| payload.clone());
        let mut cfg = self.config();
        if let Some(obj) = data.as_object() {
            if let Some(v) = obj.get("router") { cfg.router = v.as_str().unwrap_or("").to_string(); }
            if let Some(v) = obj.get("agent_id") { cfg.agent_id = v.as_str().unwrap_or("").to_string(); }
            if let Some(v) = obj.get("agent_key") {
                let text = v.as_str().unwrap_or("").trim();
                if !text.is_empty() && !text.contains("...") && text != "***" {
                    cfg.agent_key = text.to_string();
                }
            }
            if let Some(v) = obj.get("name") { cfg.name = v.as_str().unwrap_or("").to_string(); }
            if let Some(v) = obj.get("description") { cfg.description = v.as_str().unwrap_or("").to_string(); }
            if let Some(v) = obj.get("default_engine") { cfg.default_engine = v.as_str().unwrap_or("").to_string(); }
            if let Some(v) = obj.get("default_workspace") { cfg.default_workspace = v.as_str().unwrap_or("").to_string(); }
            if let Some(v) = obj.get("workspaces") {
                cfg.workspaces = serde_json::from_value(v.clone()).unwrap_or_default();
            }
            if let Some(v) = obj.get("engines") {
                cfg.engines = serde_json::from_value(v.clone()).unwrap_or_default();
            }
            if let Some(v) = obj.get("wait_seconds") { cfg.wait_seconds = v.as_u64().unwrap_or(cfg.wait_seconds); }
            if let Some(v) = obj.get("task_timeout") { cfg.task_timeout = v.as_u64().unwrap_or(cfg.task_timeout); }
            if let Some(v) = obj.get("poll_seconds") { cfg.poll_seconds = v.as_u64().unwrap_or(cfg.poll_seconds); }
        }
        save_config(&self.config_path, &cfg)?;
        *self.config.lock().unwrap() = cfg;
        self.log.add("INFO", "本机配置已保存");
        Ok(self.config_for_ui())
    }

    pub fn router_health(&self, timeout_secs: u64) -> Value {
        let base = self.config().router.trim_end_matches('/').to_string();
        if base.is_empty() {
            return json!({"ok": false, "error": "未配置 Router", "url": ""});
        }
        let started = std::time::Instant::now();
        let client = Client::builder().timeout(Duration::from_secs(timeout_secs)).build();
        match client {
            Ok(client) => match client.get(format!("{base}/health")).send() {
                Ok(response) => {
                    let ok = response.status().is_success();
                    json!({"ok": ok, "url": base, "latency_ms": started.elapsed().as_millis(), "data": response.json::<Value>().unwrap_or(Value::Null)})
                }
                Err(error) => json!({"ok": false, "url": base, "error": error.to_string(), "latency_ms": started.elapsed().as_millis()}),
            },
            Err(error) => json!({"ok": false, "url": base, "error": error.to_string()}),
        }
    }

    pub fn effective_server_url(&self) -> String {
        let settings_url = self.settings.server_url.clone();
        if !settings_url.is_empty() {
            return settings_url.trim_end_matches('/').to_string();
        }
        self.config().router.trim_end_matches('/').to_string()
    }

    pub fn router_json(&self, method: &str, path: &str, body: Option<Value>, token: Option<&str>) -> Result<Value> {
        let base = self.effective_server_url();
        if base.is_empty() {
            anyhow::bail!("尚未配置 Router 地址");
        }
        let token = token.map(|v| v.to_string()).unwrap_or_else(|| self.secrets.get("service_token"));
        if token.is_empty() {
            anyhow::bail!("尚未连接服务器，请先填写服务令牌");
        }
        let url = format!("{base}{path}");
        let mut builder = match method {
            "GET" => self.service_client.get(&url),
            "POST" => self.service_client.post(&url),
            "PATCH" => self.service_client.patch(&url),
            "DELETE" => self.service_client.delete(&url),
            other => anyhow::bail!("不支持的方法：{other}"),
        };
        builder = builder.header("X-Service-Token", token).header("Content-Type", "application/json");
        if let Some(payload) = body {
            builder = builder.json(&payload);
        }
        let response = builder.send().context("无法连接服务器")?;
        let status = response.status();
        let text = response.text().unwrap_or_default();
        if !status.is_success() {
            anyhow::bail!("HTTP {}: {}", status.as_u16(), text.chars().take(800).collect::<String>());
        }
        if text.is_empty() {
            Ok(Value::Null)
        } else {
            serde_json::from_str(&text).context("服务器返回非 JSON")
        }
    }

    pub fn server_connect(&self, token: &str, url: &str) -> Result<Value> {
        let token = token.trim();
        if token.is_empty() {
            anyhow::bail!("请填写服务器服务令牌");
        }
        let base = if url.trim().is_empty() { self.effective_server_url() } else { url.trim().trim_end_matches('/').to_string() };
        if base.is_empty() {
            anyhow::bail!("请填写服务器地址");
        }
        let overview = self.router_json("GET", "/internal/settings/overview", None, Some(token))?;
        let mut cfg = self.config();
        if base != cfg.router {
            cfg.router = base.clone();
            save_config(&self.config_path, &cfg)?;
            *self.config.lock().unwrap() = cfg;
        }
        let mut settings = self.settings.clone();
        settings.server_url = base.clone();
        settings.first_run = false;
        settings.save(&paths::settings_file())?;
        self.secrets.set("service_token", token)?;
        self.log.add("INFO", &format!("已连接服务器：{base}"));
        Ok(json!({"overview": overview, "server": {"url": base, "connected": true}, "agent": self.agent_status(), "config": self.config_for_ui()}))
    }

    pub fn bootstrap(&self, collector: &CollectorService, tech_stack: &TechStackService) -> Value {
        json!({
            "app": {"name": paths::APP_NAME, "version": paths::VERSION, "started_at": self.started_at, "headless": false, "window_mode": self.settings.window_mode},
            "config": self.config_for_ui(),
            "config_path": self.config_path.to_string_lossy(),
            "data_dir": self.data_dir.to_string_lossy(),
            "log_path": paths::log_file().to_string_lossy(),
            "agent": self.agent_status(),
            "router": self.router_health(3),
            "server": {"url": self.effective_server_url(), "connected": self.secrets.has("service_token")},
            "autostart": autostart::is_enabled(),
            "worklog": collector.status(),
            "collector": collector.status(),
            "tech_stack": tech_stack.status(),
            "paths": {"config": self.config_path.to_string_lossy(), "data": self.data_dir.to_string_lossy(), "log": paths::log_file().to_string_lossy()}
        })
    }

    pub fn status(&self, collector: &CollectorService, tech_stack: &TechStackService) -> Value {
        json!({
            "time": SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs(),
            "agent": self.agent_status(),
            "router": self.router_health(2),
            "server_connected": self.secrets.has("service_token"),
            "worklog": collector.status(),
            "collector": collector.status(),
            "tech_stack": tech_stack.status()
        })
    }
}

pub fn which(name: &str) -> String {
    // Cheap PATH lookup without extra crates.
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            let candidate = dir.join(name);
            if candidate.is_file() {
                return candidate.to_string_lossy().to_string();
            }
            #[cfg(windows)]
            {
                let exe = dir.join(format!("{name}.exe"));
                if exe.is_file() {
                    return exe.to_string_lossy().to_string();
                }
            }
        }
    }
    String::new()
}
