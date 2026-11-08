use crate::config::AgentConfig;
use crate::logs::EventLog;
use anyhow::{Context, Result};
use reqwest::blocking::Client;
use serde_json::{json, Value};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread;
use std::time::Duration;

#[derive(Clone, Default)]
pub struct AgentState {
    pub status: String,
    pub running: bool,
    pub agent_id: String,
    pub router: String,
    pub last_error: String,
    pub last_seen: Option<u64>,
    pub started_at: Option<u64>,
    pub current: Option<Value>,
    pub tasks_done: u64,
    pub last_task: Option<Value>,
    pub engines: Vec<String>,
    pub workspaces: Vec<String>,
    pub default_engine: String,
    pub default_workspace: String,
    pub wait_seconds: u64,
    pub poll_seconds: u64,
}

pub struct LocalAgent {
    config: AgentConfig,
    log: Arc<EventLog>,
    client: Client,
    state: Arc<Mutex<AgentState>>,
    running: Arc<AtomicBool>,
    handle: Mutex<Option<thread::JoinHandle<()>>>,
}

impl LocalAgent {
    pub fn new(config: AgentConfig, log: Arc<EventLog>) -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(35))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            config: config.clone(),
            log,
            client,
            state: Arc::new(Mutex::new(AgentState {
                status: "stopped".into(),
                agent_id: config.agent_id.clone(),
                router: config.router.clone(),
                engines: config.engines.keys().cloned().collect(),
                workspaces: config.workspaces.keys().cloned().collect(),
                default_engine: config.default_engine.clone(),
                default_workspace: config.default_workspace.clone(),
                wait_seconds: config.wait_seconds,
                poll_seconds: config.poll_seconds,
                ..Default::default()
            })),
            running: Arc::new(AtomicBool::new(false)),
            handle: Mutex::new(None),
        })
    }

    pub fn config(&self) -> &AgentConfig {
        &self.config
    }

    pub fn snapshot(&self) -> Value {
        let state = self.state.lock().unwrap().clone();
        json!({
            "status": state.status,
            "running": state.running,
            "agent_id": state.agent_id,
            "router": state.router,
            "last_error": state.last_error,
            "last_seen": state.last_seen,
            "started_at": state.started_at,
            "current": state.current,
            "tasks_done": state.tasks_done,
            "last_task": state.last_task,
            "engines": state.engines,
            "workspaces": state.workspaces,
            "default_engine": state.default_engine,
            "default_workspace": state.default_workspace,
            "wait_seconds": state.wait_seconds,
            "poll_seconds": state.poll_seconds
        })
    }

    fn set_status(&self, status: &str, error: &str) {
        let mut guard = self.state.lock().unwrap();
        guard.status = status.to_string();
        if !error.is_empty() {
            guard.last_error = error.to_string();
        }
    }

    pub fn start(&self) -> Result<()> {
        if self.config.router.is_empty() {
            anyhow::bail!("请先填写 Router 地址");
        }
        let mut guard = self.handle.lock().unwrap();
        if guard.is_some() {
            return Ok(());
        }
        self.running.store(true, Ordering::Relaxed);
        self.set_status("connecting", "");
        {
            let mut state = self.state.lock().unwrap();
            state.started_at = Some(
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs(),
            );
            state.running = true;
        }
        let config = self.config.clone();
        let client = self.client.clone();
        let log = self.log.clone();
        let running = self.running.clone();
        let state = self.state.clone();
        let handle = thread::spawn(move || {
            while running.load(Ordering::Relaxed) {
                let result: Result<()> = (|| {
                    let url = format!("{}/agent/register", config.router);
                    let response = client
                        .post(&url)
                        .header("X-Agent-Key", &config.agent_key)
                        .header("Content-Type", "application/json")
                        .header("User-Agent", "wechat-local-agent/rust")
                        .json(&json!({
                            "version": "1.3.6",
                            "name": config.name,
                            "description": config.description,
                            "platform": std::env::consts::OS,
                            "engines": config.engines.keys().collect::<Vec<_>>(),
                            "workspaces": config.workspaces.keys().collect::<Vec<_>>(),
                            "default_engine": config.default_engine,
                            "default_workspace": config.default_workspace
                        }))
                        .send()
                        .context("无法连接 Router")?;
                    if !response.status().is_success() {
                        anyhow::bail!("HTTP {}", response.status().as_u16());
                    }
                    let task_url = format!("{}/agent/tasks?agent_id={}", config.router, config.agent_id);
                    let task_response = client
                        .get(&task_url)
                        .header("X-Agent-Key", &config.agent_key)
                        .send()
                        .context("无法连接 Router")?;
                    if !task_response.status().is_success() {
                        anyhow::bail!("HTTP {}", task_response.status().as_u16());
                    }
                    let task = task_response.json::<Value>().unwrap_or(Value::Null);
                    if !task.is_null() {
                        let task_id = task.get("task_id").and_then(|v| v.as_str()).unwrap_or("");
                        if !task_id.is_empty() {
                            let message = task.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string();
                            let engine_name = task.get("engine").and_then(|v| v.as_str()).unwrap_or(&config.default_engine).to_string();
                            let workspace_name = task.get("workspace").and_then(|v| v.as_str()).unwrap_or(&config.default_workspace).to_string();
                            {
                                let mut guard = state.lock().unwrap();
                                guard.status = "busy".into();
                                guard.current = Some(json!({"task_id": task_id, "message": message, "started_at": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs()}));
                            }
                            log.add("INFO", &format!("收到任务 {}：{}", &task_id[..task_id.len().min(8)], &message[..message.len().min(120)]));
                            let workspace_path = config.workspaces.get(&workspace_name).cloned().unwrap_or(workspace_name.clone());
                            let command = config.engines.get(&engine_name).cloned().unwrap_or_default();
                            let (code, output) = if command.is_empty() {
                                (-1, format!("引擎不可用：{engine_name}"))
                            } else {
                                let mut cmd = Command::new(&command[0]);
                                cmd.args(&command[1..]).arg(&message).current_dir(&workspace_path)
                                    .stdout(Stdio::piped())
                                    .stderr(Stdio::piped());
                                #[cfg(windows)]
                                {
                                    use std::os::windows::process::CommandExt;
                                    cmd.creation_flags(0x08000000);
                                }
                                match cmd.output() {
                                    Ok(output) => (output.status.code().unwrap_or(-1), String::from_utf8_lossy(&output.stdout).to_string() + &String::from_utf8_lossy(&output.stderr)),
                                    Err(error) => (-1, error.to_string()),
                                }
                            };
                            let ok = code == 0;
                            let text = format!("[{} @ {}] exit={}\n{}", engine_name, workspace_path, code, output.chars().rev().take(6000).collect::<String>().chars().rev().collect::<String>());
                            {
                                let mut guard = state.lock().unwrap();
                                guard.tasks_done += 1;
                                guard.last_task = Some(json!({"task_id": task_id, "ok": ok, "engine": engine_name, "workspace": workspace_path, "finished_at": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs()}));
                                guard.current = None;
                                guard.status = "online".into();
                            }
                            let _ = client.post(format!("{}/agent/result", config.router))
                                .header("X-Agent-Key", &config.agent_key)
                                .json(&json!({"task_id": task_id, "ok": ok, "text": text.chars().take(8000).collect::<String>(), "lease_token": task.get("lease_token").and_then(|v| v.as_str()).unwrap_or("")}))
                                .send();
                        }
                    }
                    Ok(())
                })();
                if let Err(error) = result {
                    let message = error.to_string();
                    let mut guard = state.lock().unwrap();
                    if guard.status != "busy" {
                        guard.status = "error".into();
                    }
                    guard.last_error = message.clone();
                    drop(guard);
                    log.add("ERROR", &message);
                    std::thread::sleep(Duration::from_secs(5));
                    continue;
                }
                {
                    let mut guard = state.lock().unwrap();
                    if guard.status != "busy" {
                        guard.status = "online".into();
                    }
                    guard.last_seen = Some(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs());
                    guard.last_error.clear();
                }
                std::thread::sleep(Duration::from_secs(config.poll_seconds.max(1)));
            }
            log.add("INFO", "本地 Agent 轮询已结束");
        });
        *guard = Some(handle);
        self.log.add("INFO", &format!("本地 Agent 启动：{} → {}", self.config.agent_id, self.config.router));
        Ok(())
    }

    pub fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
        let mut guard = self.state.lock().unwrap();
        guard.running = false;
        guard.status = "stopped".into();
        drop(guard);
        if let Some(handle) = self.handle.lock().unwrap().take() {
            let _ = handle.join();
        }
        self.log.add("INFO", "本地 Agent 已停止");
    }



    fn resolve(&self, task: &Value) -> Result<(String, String)> {
        let engine = task.get("engine").and_then(|v| v.as_str()).unwrap_or(&self.config.default_engine).to_string();
        if !self.config.engines.contains_key(&engine) {
            anyhow::bail!("引擎不可用：{engine}");
        }
        let workspace = task.get("workspace").and_then(|v| v.as_str()).unwrap_or(&self.config.default_workspace).to_string();
        let path = if let Some(path) = self.config.workspaces.get(&workspace) {
            path.clone()
        } else if self.config.workspaces.values().any(|v| v == &workspace) {
            workspace.clone()
        } else {
            anyhow::bail!("工作区不允许：{workspace}");
        };
        if !Path::new(&path).is_dir() {
            anyhow::bail!("工作区不存在：{path}");
        }
        Ok((engine, path))
    }

    fn run_cli(&self, engine: &str, workspace: &str, message: &str) -> Result<(i32, String)> {
        let command = self.config.engines.get(engine).cloned().unwrap_or_default();
        if command.is_empty() {
            anyhow::bail!("引擎命令不能为空");
        }
        let mut cmd = Command::new(&command[0]);
        cmd.args(&command[1..]).arg(message).current_dir(workspace)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let output = cmd.output().context("启动本地引擎失败")?;
        let text = String::from_utf8_lossy(&output.stdout).to_string()
            + &String::from_utf8_lossy(&output.stderr);
        Ok((output.status.code().unwrap_or(-1), text.trim().to_string()))
    }

    pub fn test_engine(&self, engine: &str, workspace: &str) -> Result<Value> {
        let engine = if engine.is_empty() { self.config.default_engine.clone() } else { engine.to_string() };
        let workspace_name = if workspace.is_empty() { self.config.default_workspace.clone() } else { workspace.to_string() };
        let task = json!({"engine": engine, "workspace": workspace_name});
        let (engine, path) = self.resolve(&task)?;
        let started = std::time::Instant::now();
        let (code, output) = self.run_cli(&engine, &path, "只回复 OK，不要修改任何文件。")?;
        Ok(json!({
            "ok": code == 0,
            "exit_code": code,
            "seconds": started.elapsed().as_secs_f64().round(),
            "engine": engine,
            "workspace": path,
            "output": output.chars().rev().take(6000).collect::<String>().chars().rev().collect::<String>()
        }))
    }

    pub fn cancel(&self) -> bool {
        false
    }
}


