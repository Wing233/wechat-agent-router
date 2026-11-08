use std::path::PathBuf;

pub const APP_NAME: &str = "WeChatAgentDesktop";
pub const VERSION: &str = "0.1.0";

pub fn data_dir() -> PathBuf {
    if let Some(path) = std::env::var_os("WECHAT_AGENT_DATA") {
        return PathBuf::from(path);
    }
    if cfg!(windows) {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        base.join(APP_NAME)
    } else {
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                std::env::var_os("HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join(".local/share")
            });
        base.join(APP_NAME)
    }
}

pub fn config_path(explicit: Option<String>) -> PathBuf {
    if let Some(path) = explicit {
        return PathBuf::from(path);
    }
    if let Some(path) = std::env::var_os("WECHAT_AGENT_CONFIG") {
        return PathBuf::from(path);
    }
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|v| v.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    let portable = exe_dir.join("config.json");
    if portable.is_file() {
        return portable;
    }
    data_dir().join("config.json")
}

pub fn state_file() -> PathBuf {
    data_dir().join("instance.json")
}

pub fn settings_file() -> PathBuf {
    data_dir().join("settings.json")
}

pub fn secrets_file() -> PathBuf {
    data_dir().join("secrets.json")
}

pub fn log_file() -> PathBuf {
    data_dir().join("desktop.log")
}

pub fn collector_dir() -> PathBuf {
    data_dir().join("collector")
}

pub fn tech_stack_dir() -> PathBuf {
    data_dir().join("tech-stack")
}

pub fn web_dir() -> PathBuf {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|v| v.to_path_buf()));
    if let Some(dir) = exe_dir {
        let bundled = dir.join("web");
        if bundled.is_dir() {
            return bundled;
        }
        // Source layout: agent-router/desktop/web
        let candidate = dir.join("../../desktop/web");
        if candidate.is_dir() {
            return candidate;
        }
    }
    PathBuf::from("../desktop/web")
}
