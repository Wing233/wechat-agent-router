//! Read-only historical evidence sources for worklog backfill.
//!
//! Rust port of the retired Python `worklog_sources.py`. Only aggregated facts
//! (git subjects, browser domains, project paths, file counts) are produced; raw
//! browser URLs, chat transcripts and file contents never leave this module.
//!
//! Every collector is best-effort: on any error it yields an empty result and
//! keeps going, mirroring the Python try/except behaviour.

use chrono::{DateTime, Local, NaiveDate, NaiveDateTime, TimeZone, Utc};
use rusqlite::types::ValueRef;
use rusqlite::OpenFlags;
use serde::Serialize;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime};

#[derive(Clone, Debug, Default, Serialize)]
pub struct GitCommit {
    pub repo: String,
    pub sha: String,
    pub time: String,
    pub author: String,
    pub subject: String,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct BrowserCategory {
    pub category: String,
    pub visits: i64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct FileCount {
    pub project: String,
    pub files: i64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct WorkbuddySession {
    pub cwd: String,
    pub title: String,
}

#[derive(Clone, Debug, Default)]
pub struct CodexDay {
    pub sessions: usize,
    pub messages: i64,
    pub events: i64,
    /// `most_common(8)` order: count descending, first-seen order on ties.
    pub cwd: Vec<(String, i64)>,
}

#[derive(Clone, Debug, Default)]
pub struct Evidence {
    pub git: Vec<GitCommit>,
    pub browser: Vec<BrowserCategory>,
    pub files: Vec<FileCount>,
    pub codex: CodexDay,
    pub workbuddy: Vec<WorkbuddySession>,
}

/// Same set as `EXCLUDE_DIRS` in `worklog_sources.py`.
const EXCLUDE_DIRS: [&str; 15] = [
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    "data",
    ".runtime",
    "site-packages",
    ".next",
    ".cache",
];

// ---------------------------------------------------------------------------
// tiny insertion-ordered counter (Python Counter.most_common semantics)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Default)]
struct Counter {
    items: Vec<(String, i64)>,
    index: HashMap<String, usize>,
}

impl Counter {
    fn add(&mut self, key: &str, amount: i64) {
        if let Some(&pos) = self.index.get(key) {
            if let Some(item) = self.items.get_mut(pos) {
                item.1 += amount;
            }
            return;
        }
        self.index.insert(key.to_string(), self.items.len());
        self.items.push((key.to_string(), amount));
    }

    fn most_common(&self, limit: usize) -> Vec<(String, i64)> {
        let mut items = self.items.clone();
        items.sort_by(|a, b| b.1.cmp(&a.1));
        items.truncate(limit.max(1));
        items
    }
}

// ---------------------------------------------------------------------------
// caches
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Default)]
struct CodexBucket {
    sessions: HashSet<String>,
    messages: i64,
    events: i64,
    cwd: Counter,
}

type CodexDays = HashMap<String, CodexBucket>;
type CodexCache = Mutex<HashMap<PathBuf, CodexDays>>;
type FileKey = (Vec<String>, Vec<String>);
type FileDays = HashMap<String, Counter>;
type FileCache = Mutex<HashMap<FileKey, FileDays>>;

fn codex_cache() -> &'static CodexCache {
    static CACHE: OnceLock<CodexCache> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn file_cache() -> &'static FileCache {
    static CACHE: OnceLock<FileCache> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Drop history indexes so a long-running daemon sees new commits/sessions.
pub fn clear_caches() {
    codex_cache()
        .lock()
        .unwrap_or_else(|err| err.into_inner())
        .clear();
    file_cache()
        .lock()
        .unwrap_or_else(|err| err.into_inner())
        .clear();
}

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_default()
}

fn take_chars(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}

/// Python truthiness for JSON values.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|v| v != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn scalar_string(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        Value::Bool(flag) => {
            if *flag {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn section<'a>(config: &'a Value, name: &str) -> Option<&'a Value> {
    config
        .get("sources")
        .and_then(|sources| sources.get(name))
        .filter(|value| value.is_object())
}

fn enabled(cfg: Option<&Value>, fallback: bool) -> bool {
    match cfg.and_then(|value| value.get("enabled")) {
        Some(value) => truthy(Some(value)),
        None => fallback,
    }
}

fn string_list(cfg: Option<&Value>, key: &str) -> Vec<String> {
    cfg.and_then(|value| value.get(key))
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(|text| text.to_string()))
                .collect()
        })
        .unwrap_or_default()
}

fn object_list(cfg: Option<&Value>, key: &str) -> Vec<Value> {
    cfg.and_then(|value| value.get(key))
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .filter(|item| item.is_object())
                .cloned()
                .collect()
        })
        .unwrap_or_default()
}

fn local_midnight(day: &str) -> Option<DateTime<Local>> {
    let date = NaiveDate::parse_from_str(day.trim(), "%Y-%m-%d").ok()?;
    let naive = date.and_hms_opt(0, 0, 0)?;
    resolve_local(naive)
}

fn resolve_local(naive: NaiveDateTime) -> Option<DateTime<Local>> {
    let resolved = Local.from_local_datetime(&naive);
    resolved
        .earliest()
        .or_else(|| Some(Utc.from_utc_datetime(&naive).with_timezone(&Local)))
}

fn parse_iso(value: &str) -> Option<DateTime<Local>> {
    let text = value.trim();
    if text.is_empty() {
        return None;
    }
    if let Ok(stamp) = DateTime::parse_from_rfc3339(text) {
        return Some(stamp.with_timezone(&Local));
    }
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M:%S%:z",
        "%Y-%m-%dT%H:%M:%S%.f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%:z",
        "%Y-%m-%d %H:%M:%S%.f%z",
        "%Y-%m-%d %H:%M:%S%z",
    ] {
        if let Ok(stamp) = DateTime::parse_from_str(text, fmt) {
            return Some(stamp.with_timezone(&Local));
        }
    }
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ] {
        if let Ok(naive) = NaiveDateTime::parse_from_str(text, fmt) {
            return resolve_local(naive);
        }
    }
    if let Ok(date) = NaiveDate::parse_from_str(text, "%Y-%m-%d") {
        if let Some(naive) = date.and_hms_opt(0, 0, 0) {
            return resolve_local(naive);
        }
    }
    None
}

fn local_day(stamp: &DateTime<Local>) -> String {
    stamp.format("%Y-%m-%d").to_string()
}

fn day_of_system_time(time: SystemTime) -> String {
    let stamp: DateTime<Local> = time.into();
    stamp.format("%Y-%m-%d").to_string()
}

/// `_run`: capture stdout, 30s timeout, hidden console window on Windows.
fn run_command(program: &str, args: &[String]) -> String {
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(_) => return String::new(),
    };
    let mut stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            return String::new();
        }
    };
    let reader = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stdout.read_to_end(&mut buffer);
        buffer
    });
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let bytes = reader.join().unwrap_or_default();
                return String::from_utf8_lossy(&bytes).to_string();
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = reader.join();
                    return String::new();
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = reader.join();
                return String::new();
            }
        }
    }
}

// ---------------------------------------------------------------------------
// git
// ---------------------------------------------------------------------------

fn collect_git(
    day: &str,
    repos: &[String],
    authors: &[String],
    exclude_authors: &[String],
) -> Vec<GitCommit> {
    let start = format!("{day} 00:00:00");
    let end = format!("{day} 23:59:59");
    let wanted: Vec<String> = authors
        .iter()
        .filter(|item| !item.trim().is_empty())
        .map(|item| item.to_lowercase())
        .collect();
    let blocked: Vec<String> = exclude_authors
        .iter()
        .filter(|item| !item.trim().is_empty())
        .map(|item| item.to_lowercase())
        .collect();
    let mut items = Vec::new();
    for repo in repos {
        let root = Path::new(repo);
        if !root.join(".git").exists() {
            continue;
        }
        let args = vec![
            "-C".to_string(),
            root.to_string_lossy().to_string(),
            "log".to_string(),
            format!("--since={start}"),
            format!("--until={end}"),
            "--date=iso-strict".to_string(),
            "--pretty=format:%h%x09%ad%x09%an%x09%s".to_string(),
        ];
        let output = run_command("git", &args);
        let repo_name = root
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_default();
        for line in output.lines() {
            let parts: Vec<&str> = line.splitn(4, '\t').collect();
            if parts.len() != 4 {
                continue;
            }
            let sha = parts[0];
            let stamp = parts[1];
            let author = parts[2];
            let subject = parts[3];
            let author_l = author.to_lowercase();
            if !wanted.is_empty() && !wanted.iter().any(|word| author_l.contains(word.as_str())) {
                continue;
            }
            if blocked.iter().any(|word| author_l.contains(word.as_str())) {
                continue;
            }
            items.push(GitCommit {
                repo: repo_name.clone(),
                sha: take_chars(sha, 10),
                time: stamp.to_string(),
                author: author.to_string(),
                subject: take_chars(subject, 160),
            });
        }
    }
    items
}

// ---------------------------------------------------------------------------
// codex
// ---------------------------------------------------------------------------

fn jsonl_files(root: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(folder) = stack.pop() {
        let entries = match std::fs::read_dir(&folder) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let file_type = match entry.file_type() {
                Ok(file_type) => file_type,
                Err(_) => continue,
            };
            if file_type.is_dir() {
                stack.push(entry.path());
                continue;
            }
            if !file_type.is_file() {
                continue;
            }
            if entry.path().extension().and_then(|ext| ext.to_str()) == Some("jsonl") {
                files.push(entry.path());
            }
        }
    }
    files
}

fn pick_stamp(entry: &Value) -> String {
    let first = entry.get("timestamp");
    let candidate = if truthy(first) {
        first
    } else {
        entry.get("ts")
    };
    match candidate {
        Some(value) => scalar_string(value),
        None => String::new(),
    }
}

fn codex_days(root: &Path) -> CodexDays {
    let key = root.to_path_buf();
    {
        let guard = codex_cache().lock().unwrap_or_else(|err| err.into_inner());
        if let Some(cached) = guard.get(&key) {
            return cached.clone();
        }
    }
    let mut days: CodexDays = HashMap::new();
    for file in jsonl_files(root) {
        let session = file
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_default();
        let data = match std::fs::read(&file) {
            Ok(data) => data,
            Err(_) => continue,
        };
        for raw in data.split(|byte| *byte == b'\n') {
            let line = String::from_utf8_lossy(raw);
            let line = line.trim_end_matches('\r');
            if line.is_empty() {
                continue;
            }
            let entry: Value = match serde_json::from_str(line) {
                Ok(entry) => entry,
                Err(_) => continue,
            };
            let stamp = match parse_iso(&pick_stamp(&entry)) {
                Some(stamp) => stamp,
                None => continue,
            };
            let day = local_day(&stamp);
            let bucket = days.entry(day).or_default();
            bucket.events += 1;
            bucket.sessions.insert(session.clone());
            let payload = match entry.get("payload").and_then(|value| value.as_object()) {
                Some(payload) => payload,
                None => continue,
            };
            if let Some(cwd) = payload.get("cwd").and_then(|value| value.as_str()) {
                let cwd = cwd.trim();
                if !cwd.is_empty() {
                    bucket.cwd.add(cwd, 1);
                }
            }
            if payload.get("type").and_then(|value| value.as_str()) == Some("message")
                && payload.get("role").and_then(|value| value.as_str()) == Some("user")
            {
                bucket.messages += 1;
            }
        }
    }
    let mut guard = codex_cache().lock().unwrap_or_else(|err| err.into_inner());
    guard.insert(key, days.clone());
    days
}

fn collect_codex(day: &str, root: &Path) -> CodexBucket {
    codex_days(root)
        .get(day)
        .cloned()
        .unwrap_or_else(CodexBucket::default)
}

// ---------------------------------------------------------------------------
// browser
// ---------------------------------------------------------------------------

fn chrome_time(stamp: &DateTime<Local>) -> i64 {
    stamp.timestamp_micros() + 11_644_473_600_000_000
}

fn browser_category(host: &str) -> &'static str {
    let host = host.to_lowercase();
    if ["github", "gitlab", "gitee", "bitbucket"]
        .iter()
        .any(|word| host.contains(*word))
    {
        return "代码托管";
    }
    if host.contains("leetcode") {
        return "刷题";
    }
    if [
        "stackoverflow",
        "mozilla",
        "python.org",
        "docs.",
        "readthedocs",
        "npmjs",
        "pypi",
    ]
    .iter()
    .any(|word| host.contains(*word))
    {
        return "技术文档";
    }
    if [
        "volcengine",
        "aliyun",
        "tencent",
        "cloudflare",
        "render.com",
        "vercel",
        "backblaze",
        "tidbcloud",
        "aws.amazon",
        "azure.com",
        "console.",
    ]
    .iter()
    .any(|word| host.contains(*word))
    {
        return "云服务/部署";
    }
    if ["mail.", "outlook", "qq.com", "weixin"]
        .iter()
        .any(|word| host.contains(*word))
    {
        return "邮件/沟通";
    }
    if ["bilibili", "youtube", "coursera", "udemy", "juejin", "zhihu"]
        .iter()
        .any(|word| host.contains(*word))
    {
        return "学习/视频";
    }
    "其他浏览"
}

/// Minimal `urlparse(...).hostname` replacement (lowercased host, no port/userinfo).
fn url_hostname(url: &str) -> String {
    let text = url.trim();
    if text.is_empty() {
        return String::new();
    }
    let rest = if let Some((scheme, rest)) = text.split_once("://") {
        if scheme.is_empty() || !scheme.chars().next().map(|c| c.is_ascii_alphabetic()).unwrap_or(false)
            || !scheme
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
        {
            return String::new();
        }
        rest
    } else if let Some(stripped) = text.strip_prefix("//") {
        stripped
    } else {
        return String::new();
    };
    let end = rest
        .find(|c| c == '/' || c == '?' || c == '#')
        .unwrap_or(rest.len());
    let netloc = &rest[..end];
    if netloc.is_empty() {
        return String::new();
    }
    let after_user = match netloc.rfind('@') {
        Some(pos) => &netloc[pos + 1..],
        None => netloc,
    };
    let host = if let Some(open) = after_user.find('[') {
        match after_user[open + 1..].find(']') {
            Some(close) => after_user[open + 1..open + 1 + close].to_string(),
            None => return String::new(),
        }
    } else {
        match after_user.find(':') {
            Some(pos) => after_user[..pos].to_string(),
            None => after_user.to_string(),
        }
    };
    host.to_lowercase()
}

fn collect_browser(day: &str, profiles: &[Value]) -> Vec<BrowserCategory> {
    if profiles.is_empty() {
        return Vec::new();
    }
    let start = match local_midnight(day) {
        Some(stamp) => stamp,
        None => return Vec::new(),
    };
    let end = match start
        .date_naive()
        .succ_opt()
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .and_then(resolve_local)
    {
        Some(stamp) => stamp,
        None => return Vec::new(),
    };
    let low = chrome_time(&start);
    let high = chrome_time(&end);
    let mut counter = Counter::default();
    for profile in profiles {
        let path = profile.get("path").and_then(|value| value.as_str()).unwrap_or("");
        if path.is_empty() {
            continue;
        }
        let file = Path::new(path);
        if !file.is_file() {
            continue;
        }
        let excluded: Vec<String> = profile
            .get("exclude_domains")
            .and_then(|value| value.as_array())
            .map(|items| {
                items
                    .iter()
                    .filter_map(|item| item.as_str().map(|text| text.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        let uri = format!("file:{}?mode=ro&immutable=1", file.display());
        let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI;
        let conn = match rusqlite::Connection::open_with_flags(&uri, flags) {
            Ok(conn) => conn,
            Err(_) => continue,
        };
        let _ = conn.busy_timeout(Duration::from_secs(5));
        let mut stmt = match conn.prepare(
            "SELECT urls.url FROM visits JOIN urls ON visits.url=urls.id \
             WHERE visits.visit_time>=? AND visit_time<?",
        ) {
            Ok(stmt) => stmt,
            Err(_) => continue,
        };
        let rows = match stmt.query_map(rusqlite::params![low, high], |row| {
            Ok(match row.get_ref(0) {
                Ok(ValueRef::Text(bytes)) => String::from_utf8_lossy(bytes).to_string(),
                Ok(ValueRef::Integer(value)) => value.to_string(),
                Ok(ValueRef::Real(value)) => value.to_string(),
                _ => String::new(),
            })
        }) {
            Ok(rows) => rows,
            Err(_) => continue,
        };
        for row in rows {
            let url = match row {
                Ok(url) => url,
                Err(_) => continue,
            };
            let mut host = url_hostname(&url);
            if host.is_empty() {
                continue;
            }
            if host.starts_with("www.") {
                let trimmed = host[4..].to_string();
                host = trimmed;
            }
            if excluded
                .iter()
                .any(|item| host == item.as_str() || host.ends_with(&format!(".{item}")))
            {
                continue;
            }
            counter.add(browser_category(&host), 1);
        }
    }
    counter
        .most_common(12)
        .into_iter()
        .map(|(category, visits)| BrowserCategory { category, visits })
        .collect()
}

// ---------------------------------------------------------------------------
// files
// ---------------------------------------------------------------------------

fn file_days(roots: &[String], excluded: &HashSet<String>, key: &FileKey) -> FileDays {
    {
        let guard = file_cache().lock().unwrap_or_else(|err| err.into_inner());
        if let Some(cached) = guard.get(key) {
            return cached.clone();
        }
    }
    let mut days: FileDays = HashMap::new();
    let mut seen: usize = 0;
    for raw in roots {
        let root = PathBuf::from(raw);
        if !root.exists() {
            continue;
        }
        let root_name = root
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_default();
        let mut stack = vec![root.clone()];
        while !stack.is_empty() && seen < 200_000 {
            let folder = match stack.pop() {
                Some(folder) => folder,
                None => break,
            };
            let entries = match std::fs::read_dir(&folder) {
                Ok(entries) => entries,
                Err(_) => continue,
            };
            for entry in entries.flatten() {
                let file_type = match entry.file_type() {
                    Ok(file_type) => file_type,
                    Err(_) => continue,
                };
                if file_type.is_dir() {
                    let name = entry.file_name().to_string_lossy().to_string();
                    if !EXCLUDE_DIRS.contains(&name.as_str()) && !name.starts_with('.') {
                        stack.push(entry.path());
                    }
                    continue;
                }
                if !file_type.is_file() {
                    continue;
                }
                seen += 1;
                let modified = match entry.metadata().and_then(|meta| meta.modified()) {
                    Ok(modified) => modified,
                    Err(_) => continue,
                };
                let day = day_of_system_time(modified);
                let path = entry.path();
                let rel = match path.strip_prefix(&root) {
                    Ok(rel) => rel,
                    Err(_) => continue,
                };
                let parts: Vec<String> = rel
                    .components()
                    .filter_map(|part| match part {
                        Component::Normal(name) => Some(name.to_string_lossy().to_string()),
                        _ => None,
                    })
                    .collect();
                let first = if parts.len() > 1 {
                    parts[0].clone()
                } else {
                    root_name.clone()
                };
                let project = if root_name.to_lowercase() == "wechatbot" {
                    first
                } else {
                    format!("{root_name}/{first}")
                };
                if excluded.contains(&project.to_lowercase()) {
                    continue;
                }
                days.entry(day).or_default().add(&project, 1);
            }
        }
    }
    let mut guard = file_cache().lock().unwrap_or_else(|err| err.into_inner());
    guard.insert(key.clone(), days.clone());
    days
}

fn collect_files(
    day: &str,
    roots: &[String],
    limit: usize,
    exclude_projects: &[String],
) -> Vec<FileCount> {
    let excluded: HashSet<String> = exclude_projects
        .iter()
        .map(|item| item.to_lowercase())
        .collect();
    let mut sorted_excluded: Vec<String> = excluded.iter().cloned().collect();
    sorted_excluded.sort();
    let key: FileKey = (roots.to_vec(), sorted_excluded);
    let days = file_days(roots, &excluded, &key);
    let counts = match days.get(day) {
        Some(counter) => counter.most_common(limit.max(1)),
        None => Vec::new(),
    };
    counts
        .into_iter()
        .map(|(project, files)| FileCount { project, files })
        .collect()
}

// ---------------------------------------------------------------------------
// workbuddy
// ---------------------------------------------------------------------------

fn column_text(row: &rusqlite::Row<'_>, index: usize) -> String {
    match row.get_ref(index) {
        Ok(ValueRef::Text(bytes)) => String::from_utf8_lossy(bytes).to_string(),
        Ok(ValueRef::Integer(value)) => value.to_string(),
        Ok(ValueRef::Real(value)) => value.to_string(),
        Ok(ValueRef::Blob(bytes)) => String::from_utf8_lossy(bytes).to_string(),
        _ => String::new(),
    }
}

fn collect_workbuddy(day: &str, db_path: Option<&str>) -> Vec<WorkbuddySession> {
    let path = match db_path {
        Some(text) if !text.is_empty() => PathBuf::from(text),
        _ => home_dir().join(".workbuddy").join("workbuddy.db"),
    };
    if !path.is_file() {
        return Vec::new();
    }
    let uri = format!("file:{}?mode=ro", path.display());
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI;
    let conn = match rusqlite::Connection::open_with_flags(&uri, flags) {
        Ok(conn) => conn,
        Err(_) => return Vec::new(),
    };
    let _ = conn.busy_timeout(Duration::from_secs(5));
    let mut stmt = match conn.prepare(
        "SELECT cwd, title FROM sessions \
         WHERE date(created_at/1000,'unixepoch','localtime')=?",
    ) {
        Ok(stmt) => stmt,
        Err(_) => return Vec::new(),
    };
    let rows = match stmt.query_map(rusqlite::params![day], |row| {
        Ok((column_text(row, 0), column_text(row, 1)))
    }) {
        Ok(rows) => rows,
        Err(_) => return Vec::new(),
    };
    rows.filter_map(|row| row.ok())
        .map(|(cwd, title)| WorkbuddySession {
            cwd: take_chars(&cwd, 200),
            title: take_chars(&title, 120),
        })
        .collect()
}

// ---------------------------------------------------------------------------
// public entry points
// ---------------------------------------------------------------------------

/// Collect every enabled source for the local day `YYYY-MM-DD`.
pub fn collect_evidence(day: &str, config: &Value) -> Evidence {
    let git_cfg = section(config, "git");
    let browser_cfg = section(config, "browser");
    let files_cfg = section(config, "files");
    let codex_cfg = section(config, "codex");
    let workbuddy_cfg = section(config, "workbuddy");

    let git = if enabled(git_cfg, true) {
        collect_git(
            day,
            &string_list(git_cfg, "repos"),
            &string_list(git_cfg, "authors"),
            &string_list(git_cfg, "exclude_authors"),
        )
    } else {
        Vec::new()
    };

    let browser = if enabled(browser_cfg, true) {
        collect_browser(day, &object_list(browser_cfg, "profiles"))
    } else {
        Vec::new()
    };

    let files = if enabled(files_cfg, true) {
        collect_files(
            day,
            &string_list(files_cfg, "roots"),
            12,
            &string_list(files_cfg, "exclude_projects"),
        )
    } else {
        Vec::new()
    };

    let codex = if enabled(codex_cfg, true) {
        let root = codex_cfg
            .and_then(|cfg| cfg.get("root"))
            .and_then(|value| value.as_str())
            .filter(|text| !text.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home_dir().join(".codex").join("sessions"));
        collect_codex(day, &root)
    } else {
        CodexBucket::default()
    };

    let workbuddy = if enabled(workbuddy_cfg, false) {
        collect_workbuddy(
            day,
            workbuddy_cfg
                .and_then(|cfg| cfg.get("db"))
                .and_then(|value| value.as_str()),
        )
    } else {
        Vec::new()
    };

    Evidence {
        git,
        browser,
        files,
        codex: CodexDay {
            sessions: codex.sessions.len(),
            messages: codex.messages,
            events: codex.events,
            cwd: codex.cwd.most_common(8),
        },
        workbuddy,
    }
}

/// Strip the user home directory and the fixed workspace prefixes from text.
/// Mirrors Python `sanitize_text`; separators and case are left untouched.
pub fn sanitize_paths(text: &str) -> String {
    let mut value = text.to_string();
    let home = home_dir();
    let home_text = home.to_string_lossy().to_string();
    if !home_text.is_empty() {
        value = value.replace(&home_text, "~");
    }
    value = value.replace(r"D:\xx\wechatbot", "wechatbot");
    value = value.replace(r"D:\xx\lyph", "lyph");
    value
}
