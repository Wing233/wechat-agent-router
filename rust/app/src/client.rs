use crate::{
    json,
    readonly::{integer, trim},
};
use serde_json::Value;
use std::{
    env,
    io::{self, Read},
    path::{Path, PathBuf},
    process::{Command, ExitStatus, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

pub struct MusicError {
    pub code: String,
    pub message: String,
    pub status: u16,
}

impl MusicError {
    fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            status: 502,
        }
    }

    fn internal(message: impl Into<String>) -> Self {
        Self {
            code: "bridge_error".into(),
            message: message.into(),
            status: 500,
        }
    }
}

pub fn tail(text: &str, limit: usize) -> &str {
    text.char_indices()
        .rev()
        .nth(limit.saturating_sub(1))
        .map_or(text, |(index, _)| &text[index..])
}

pub fn extract_json(text: &str) -> Option<Value> {
    let text = trim(text.trim_start_matches('\u{feff}'));
    if let Some(value) = json::decode(text) {
        return Some(value);
    }
    let mut best = None;
    let mut best_len = 0;
    for (start, ch) in text.char_indices() {
        if !matches!(ch, '{' | '[') {
            continue;
        }
        if let Some(candidate) = balanced(&text[start..], ch) {
            let len = candidate.chars().count();
            if len > best_len {
                if let Some(value) = json::decode(candidate) {
                    best = Some(value);
                    best_len = len;
                }
            }
        }
    }
    best
}

fn balanced(text: &str, opening: char) -> Option<&str> {
    let closing = if opening == '{' { '}' } else { ']' };
    let mut depth = 0;
    let mut in_string = false;
    let mut escaped = false;
    for (index, ch) in text.char_indices() {
        if escaped {
            escaped = false;
        } else if in_string && ch == '\\' {
            escaped = true;
        } else if ch == '"' {
            in_string = !in_string;
        } else if !in_string && ch == opening {
            depth += 1;
        } else if !in_string && ch == closing {
            depth -= 1;
            if depth == 0 {
                return Some(&text[..=index]);
            }
        }
    }
    None
}

fn executable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        path.metadata()
            .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
    }
    #[cfg(windows)]
    {
        path.is_file()
    }
}

pub fn resolve_binary(binary: &str) -> Result<PathBuf, MusicError> {
    let value = trim(if binary.is_empty() { "ncm-cli" } else { binary });
    let path = Path::new(value);
    #[cfg(windows)]
    let candidates = {
        let extensions = env::var("PATHEXT")
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC".into());
        let extensions = extensions
            .split(';')
            .filter(|ext| !ext.is_empty())
            .map(|ext| ext.trim_end_matches('.'));
        let mut candidates = Vec::new();
        let upper = value.to_uppercase();
        if extensions
            .clone()
            .any(|extension| upper.ends_with(&extension.to_uppercase()))
        {
            candidates.push(path.to_owned());
        }
        for extension in extensions {
            candidates.push(PathBuf::from(format!("{value}{extension}")));
        }
        candidates
    };
    #[cfg(unix)]
    let candidates = [path.to_owned()];

    if path
        .parent()
        .is_some_and(|parent| !parent.as_os_str().is_empty())
    {
        if let Some(found) = candidates.into_iter().find(|p| executable(p)) {
            return Ok(found);
        }
    } else {
        let default_path = if cfg!(unix) {
            "/bin:/usr/bin"
        } else {
            r".;C:\bin"
        };
        let paths = env::var_os("PATH").unwrap_or_else(|| default_path.into());
        let directories = env::split_paths(&paths);
        #[cfg(windows)]
        let directories = (env::var_os("NoDefaultCurrentDirectoryInExePath").is_none())
            .then(|| PathBuf::from("."))
            .into_iter()
            .chain(directories);
        if !paths.is_empty() {
            if let Some(found) = directories
                .flat_map(|dir| candidates.iter().map(move |p| dir.join(p)))
                .find(|p| executable(p))
            {
                return Ok(found);
            }
        }
    }
    let home = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    let expanded = match (
        value
            .strip_prefix("~/")
            .or_else(|| value.strip_prefix("~\\")),
        env::var_os(home),
    ) {
        (Some(relative), Some(home)) => PathBuf::from(home).join(relative),
        _ => path.to_owned(),
    };
    if expanded.is_file() {
        return Ok(expanded);
    }
    Err(MusicError::new(
        "not_installed",
        "未找到 ncm-cli。请先安装 @music163/ncm-cli，或在 plugins.music.bin 配置完整路径。",
    ))
}

fn command(binary: &Path) -> io::Result<Command> {
    let executable = std::path::absolute(binary)?;
    #[cfg(windows)]
    {
        // Use the installed npm package's real entry point to pass search text as argv to Node.
        if executable
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.eq_ignore_ascii_case("ncm-cli.cmd"))
        {
            if let Some(directory) = executable.parent() {
                let entry = directory.join("node_modules/@music163/ncm-cli/dist/index.js");
                if entry.is_file() {
                    let adjacent_node = directory.join("node.exe");
                    let mut node = if adjacent_node.is_file() {
                        Command::new(adjacent_node)
                    } else {
                        Command::new("node.exe")
                    };
                    node.arg(entry);
                    return Ok(node);
                }
            }
        }
    }
    Ok(Command::new(executable))
}

pub fn run(binary: &str, args: &[&str], timeout: u64) -> Result<Value, MusicError> {
    let binary = resolve_binary(binary)?;
    let mut command = command(&binary).map_err(|err| process_error(&binary, err))?;
    command
        .args(cli_args(args))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if env::var_os("NO_COLOR").is_none() {
        command.env("NO_COLOR", "1");
    }
    let mut child = command.spawn().map_err(|err| process_error(&binary, err))?;
    let stdout = capture(child.stdout.take());
    let stderr = capture(child.stderr.take());
    let start = Instant::now();
    let duration = Duration::from_secs(timeout);
    let read = || -> io::Result<(ExitStatus, Vec<u8>, Vec<u8>)> {
        let status = loop {
            if let Some(status) = child.try_wait()? {
                break status;
            }
            let remaining = duration.saturating_sub(start.elapsed());
            if remaining.is_zero() {
                return Err(io::ErrorKind::TimedOut.into());
            }
            thread::sleep(remaining.min(Duration::from_millis(5)));
        };
        let stdout = receive(stdout, duration.saturating_sub(start.elapsed()))?;
        let stderr = receive(stderr, duration.saturating_sub(start.elapsed()))?;
        Ok((status, stdout, stderr))
    };
    let (status, stdout, stderr) = match read() {
        Ok(values) => values,
        Err(err) => {
            let _ = child.kill();
            let _ = child.wait();
            if err.kind() == io::ErrorKind::TimedOut {
                return Err(MusicError::new(
                    "timeout",
                    format!("ncm-cli 在 {timeout} 秒内没有返回。"),
                ));
            }
            return Err(process_error(&binary, err));
        }
    };

    let stdout = decode(&stdout);
    let stderr = decode(&stderr);
    let detail = trim(if stderr.is_empty() { &stdout } else { &stderr });
    if !status.success() {
        return Err(friendly_error(detail));
    }
    let payload = extract_json(&stdout)
        .filter(|value| !value.is_null())
        .ok_or_else(|| {
            MusicError::new(
                "invalid_output",
                format!("ncm-cli 没有返回可解析的 JSON：{}", tail(detail, 300)),
            )
        })?;
    check_payload(&payload)?;
    Ok(payload)
}

pub fn cli_args<'a>(args: &'a [&'a str]) -> impl Iterator<Item = &'a str> {
    args.iter().copied().chain(["--output", "json"])
}

fn capture(pipe: Option<impl Read + Send + 'static>) -> mpsc::Receiver<io::Result<Vec<u8>>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut buffer = Vec::new();
        let result = pipe.map_or(Ok(0), |mut pipe| pipe.read_to_end(&mut buffer));
        let _ = sender.send(result.map(|_| buffer));
    });
    receiver
}

fn receive(pipe: mpsc::Receiver<io::Result<Vec<u8>>>, timeout: Duration) -> io::Result<Vec<u8>> {
    pipe.recv_timeout(timeout).map_err(|err| match err {
        mpsc::RecvTimeoutError::Timeout => io::Error::from(io::ErrorKind::TimedOut),
        mpsc::RecvTimeoutError::Disconnected => io::Error::from(io::ErrorKind::BrokenPipe),
    })?
}

fn decode(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}

fn process_error(path: &Path, err: io::Error) -> MusicError {
    if err.kind() == io::ErrorKind::NotFound {
        return MusicError::new("not_installed", "未找到 ncm-cli 或系统命令解释器。");
    }
    #[cfg(unix)]
    let message = if let Some(errno) = err.raw_os_error() {
        let reason = err.to_string();
        let reason = reason.split(" (os error ").next().unwrap_or(&reason);
        format!(
            "[Errno {errno}] {reason}: {}",
            json::repr_string(&path.to_string_lossy())
        )
    } else {
        err.to_string()
    };
    #[cfg(windows)]
    let message = if let Some(code) = err.raw_os_error() {
        let reason = err.to_string();
        let reason = reason.split(" (os error ").next().unwrap_or(&reason);
        format!("[WinError {code}] {}", reason.trim())
    } else {
        let _ = path;
        err.to_string()
    };
    MusicError::internal(message.chars().take(300).collect::<String>())
}

fn friendly_error(text: &str) -> MusicError {
    let lowered = text.to_lowercase();
    if text.contains("api key 未设置") || lowered.contains("apikey") && text.contains("未设置")
    {
        MusicError::new(
            "not_configured",
            "ncm-cli 还没有配置 API Key，请先运行 ncm-cli configure。",
        )
    } else if text.contains("未登录")
        || text.contains("请先登录")
        || lowered.contains("login required")
    {
        MusicError::new(
            "login_required",
            "ncm-cli 还没有登录，请先运行 ncm-cli login 并扫码授权。",
        )
    } else if text.contains("请求总量超限")
        || lowered.contains("quota")
        || lowered.contains("rate limit")
    {
        MusicError::new(
            "quota_exceeded",
            "网易云开放平台今日调用次数已用尽，请稍后再试。",
        )
    } else if lowered.contains("not found") || text.contains("404") {
        MusicError::new("not_found", "没有找到对应的音乐资源。")
    } else {
        let detail = if text.is_empty() {
            "未知错误"
        } else {
            tail(text, 500)
        };
        MusicError::new("cli_error", format!("ncm-cli 执行失败：{detail}"))
    }
}

fn message(value: Option<&Value>, default: &str) -> String {
    value
        .filter(|v| json::truthy(v))
        .map_or_else(|| default.into(), |v| json::repr(v, false))
}

fn check_payload(payload: &Value) -> Result<(), MusicError> {
    let Some(payload) = payload.as_object() else {
        return Ok(());
    };
    let default = "网易云接口返回错误。";
    if let Some(err) = payload.get("error").filter(|v| json::truthy(v)) {
        return Err(match err.as_object() {
            Some(err) => MusicError::new(
                message(err.get("code"), "api_error"),
                message(err.get("message"), default),
            ),
            None => MusicError::new("api_error", json::repr(err, false)),
        });
    }
    if payload.get("success") == Some(&Value::Bool(false)) {
        return Err(MusicError::new(
            "api_error",
            message(payload.get("message"), "ncm-cli 执行失败。"),
        ));
    }
    let number = match payload.get("code") {
        Some(Value::Bool(flag)) => Some(if *flag { "1" } else { "0" }.into()),
        Some(Value::String(code)) => integer(code),
        Some(Value::Number(code)) => {
            let raw = code.as_str();
            if raw == "NaN" {
                None
            } else if raw.contains(['.', 'e', 'E']) || matches!(raw, "Infinity" | "-Infinity") {
                let number = raw
                    .parse::<f64>()
                    .ok()
                    .filter(|number| number.is_finite())
                    .ok_or_else(|| {
                        MusicError::internal("cannot convert float infinity to integer")
                    })?;
                Some(if number.trunc() == 0.0 {
                    "0".into()
                } else {
                    format!("{:.0}", number.trunc())
                })
            } else {
                integer(raw)
            }
        }
        _ => None,
    };
    match number {
        Some(number) if !matches!(number.as_str(), "0" | "200") => Err(MusicError::new(
            number,
            message(payload.get("message"), default),
        )),
        _ => Ok(()),
    }
}
