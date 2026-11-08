use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

pub type JsonMap = Map<String, Value>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentConfig {
    #[serde(default)]
    pub router: String,
    #[serde(default = "default_agent_id")]
    pub agent_id: String,
    #[serde(default)]
    pub agent_key: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "default_engine")]
    pub default_engine: String,
    #[serde(default = "default_workspace")]
    pub default_workspace: String,
    #[serde(default = "default_workspaces")]
    pub workspaces: BTreeMap<String, String>,
    #[serde(default = "default_engines")]
    pub engines: BTreeMap<String, Vec<String>>,
    #[serde(default = "default_wait")]
    pub wait_seconds: u64,
    #[serde(default = "default_timeout")]
    pub task_timeout: u64,
    #[serde(default = "default_poll")]
    pub poll_seconds: u64,
}

fn default_agent_id() -> String {
    "pc-home".to_string()
}
fn default_engine() -> String {
    "opencode".to_string()
}
fn default_workspace() -> String {
    "workspace".to_string()
}
fn default_wait() -> u64 {
    60
}
fn default_timeout() -> u64 {
    1800
}
fn default_poll() -> u64 {
    2
}
fn default_workspaces() -> BTreeMap<String, String> {
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    BTreeMap::from([("workspace".to_string(), home.join("wechat-agent-workspace").to_string_lossy().to_string())])
}
fn default_engines() -> BTreeMap<String, Vec<String>> {
    BTreeMap::from([
        ("opencode".to_string(), vec!["opencode".into(), "run".into()]),
        ("codex".to_string(), vec!["codex".into(), "exec".into()]),
        ("claude".to_string(), vec!["claude".into(), "-p".into()]),
    ])
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            router: String::new(),
            agent_id: default_agent_id(),
            agent_key: String::new(),
            name: "我的电脑".to_string(),
            description: "本地电脑，可运行 OpenCode / Codex / Claude Code。".to_string(),
            default_engine: default_engine(),
            default_workspace: default_workspace(),
            workspaces: default_workspaces(),
            engines: default_engines(),
            wait_seconds: default_wait(),
            task_timeout: default_timeout(),
            poll_seconds: default_poll(),
        }
    }
}

impl AgentConfig {
    pub fn normalize(mut self) -> Result<Self> {
        self.router = self.router.trim().trim_end_matches('/').to_string();
        if !self.router.is_empty() && !self.router.starts_with("http://") && !self.router.starts_with("https://") {
            bail!("Router 地址必须是 http:// 或 https:// 开头的 URL");
        }
        self.agent_id = self.agent_id.trim().to_string();
        if self.agent_id.len() < 2 || !self.agent_id.chars().next().map(|c| c.is_ascii_lowercase()).unwrap_or(false) {
            bail!("Agent ID 只能用小写字母、数字、下划线或短横线，且以字母开头");
        }
        if self.agent_key.trim().is_empty() {
            self.agent_key = format!("pc_{}", uuid::Uuid::new_v4().simple());
        }
        self.name = if self.name.trim().is_empty() { self.agent_id.clone() } else { self.name.trim().to_string() };
        self.description = self.description.trim().to_string();
        if self.engines.is_empty() {
            bail!("至少配置一个引擎");
        }
        for (name, command) in &self.engines {
            if name.is_empty() || command.is_empty() {
                bail!("引擎命令不能为空");
            }
        }
        if self.workspaces.is_empty() {
            bail!("至少配置一个工作区");
        }
        for path in self.workspaces.values() {
            let expanded = PathBuf::from(path);
            if !expanded.is_absolute() {
                bail!("工作区必须使用绝对路径");
            }
        }
        if !self.engines.contains_key(&self.default_engine) {
            self.default_engine = self.engines.keys().next().cloned().unwrap_or_default();
        }
        if !self.workspaces.contains_key(&self.default_workspace) {
            self.default_workspace = self.workspaces.keys().next().cloned().unwrap_or_default();
        }
        if !(5..=600).contains(&self.wait_seconds) {
            bail!("wait_seconds 应在 5–600 秒之间");
        }
        if !(60..=86400).contains(&self.task_timeout) {
            bail!("task_timeout 应在 60–86400 秒之间");
        }
        if !(1..=60).contains(&self.poll_seconds) {
            bail!("poll_seconds 应在 1–60 秒之间");
        }
        Ok(self)
    }

    pub fn to_json(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Object(Map::new()))
    }

    pub fn masked_json(&self) -> Value {
        let mut value = self.to_json();
        if let Some(obj) = value.as_object_mut() {
            let key = obj.get("agent_key").and_then(|v| v.as_str()).unwrap_or("");
            obj.insert("agent_key".into(), Value::String(mask(key)));
        }
        value
    }
}

pub fn mask(value: &str) -> String {
    if value.is_empty() {
        return String::new();
    }
    if value.len() > 12 {
        format!("{}...{}", &value[..6], &value[value.len() - 4..])
    } else {
        "***".to_string()
    }
}

pub fn load_config(path: &Path) -> Result<AgentConfig> {
    if !path.is_file() {
        let cfg = AgentConfig::default().normalize()?;
        save_config(path, &cfg)?;
        return Ok(cfg);
    }
    let raw = fs::read_to_string(path).with_context(|| format!("读取配置失败：{}", path.display()))?;
    let cfg: AgentConfig = serde_json::from_str(&raw).context("配置 JSON 无效")?;
    cfg.normalize()
}

pub fn save_config(path: &Path, cfg: &AgentConfig) -> Result<()> {
    let cfg = cfg.clone().normalize()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_string_pretty(&cfg)?)?;
    fs::rename(tmp, path)?;
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Settings {
    #[serde(default)]
    pub server_url: String,
    #[serde(default = "default_window_mode")]
    pub window_mode: String,
    #[serde(default = "default_page")]
    pub last_page: String,
    #[serde(default = "default_true")]
    pub first_run: bool,
}

fn default_window_mode() -> String {
    "auto".to_string()
}
fn default_page() -> String {
    "overview".to_string()
}
fn default_true() -> bool {
    true
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            server_url: String::new(),
            window_mode: default_window_mode(),
            last_page: default_page(),
            first_run: true,
        }
    }
}

impl Settings {
    pub fn load(path: &Path) -> Self {
        fs::read_to_string(path)
            .ok()
            .and_then(|text| serde_json::from_str::<Settings>(&text).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let tmp = path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_string_pretty(self)?)?;
        fs::rename(tmp, path)?;
        Ok(())
    }
}
