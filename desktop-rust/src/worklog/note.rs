//! Note construction: plain-text sections plus the machine-readable sentinel.
//!
//! Faithful port of `worklog_collector.build_note` and its helpers. The server
//! and the daily-report detail page depend on the exact text layout and on the
//! trailing `<!--WORKLOG_TL_V1 ... -->` payload, so the formatting here is
//! intentionally byte-for-byte compatible with the Python collector.
//!
//! Only `serde_json`, `regex`, `chrono`-free stdlib and crate-local modules are
//! used. No function panics on malformed input: parsing failures degrade to
//! empty strings / defaults just like the Python originals.

use crate::worklog::config::home_dir;
use crate::worklog::db::{OcrRow, SampleRow};
use crate::worklog::sources::Evidence;
use regex::Regex;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

/// Python `text[:16000]` fallback for the whole note.
const NOTE_MAX_CHARS: usize = 16000;
/// Python `plain_bytes_cap`.
const PLAIN_MAX_BYTES: usize = 10000;
/// Python `_shrink_payload(max_bytes=6000)`.
const SENTINEL_MAX_BYTES: usize = 6000;
/// Python `_shrink_payload` hl floor.
const HL_MAX_CHARS: usize = 64;

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

fn char_count(text: &str) -> usize {
    text.chars().count()
}

/// Python `value[:limit]` — slices by character, never by byte.
fn truncate_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

/// Python's `text.strip()` on a single line.
fn py_strip(text: &str) -> &str {
    text.trim()
}

/// Python `re.sub(r"\s+", " ", text).strip()`.
fn collapse_whitespace(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Python `str.splitlines()` (splits on \n, \r, \r\n and the other Unicode
/// line boundaries instead of only `\n`).
fn py_split_lines(text: &str) -> Vec<&str> {
    let mut out: Vec<&str> = Vec::new();
    let mut start = 0usize;
    let mut iter = text.char_indices().peekable();
    while let Some((index, ch)) = iter.next() {
        let is_break = matches!(
            ch,
            '\n' | '\r'
                | '\u{000B}'
                | '\u{000C}'
                | '\u{001C}'
                | '\u{001D}'
                | '\u{001E}'
                | '\u{0085}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !is_break {
            continue;
        }
        out.push(&text[start..index]);
        let mut next_start = index + ch.len_utf8();
        if ch == '\r' {
            if let Some(&(newline_index, '\n')) = iter.peek() {
                iter.next();
                next_start = newline_index + 1;
            }
        }
        start = next_start;
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// Largest prefix of `text` whose UTF-8 byte length is at most `max_bytes`.
fn floor_char_prefix(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return text.to_string();
    }
    let mut end = max_bytes;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_string()
}

fn config_string_list(config: &Value, key: &str) -> Vec<String> {
    config
        .get(key)
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .map(|item| match item {
                    Value::String(text) => text.clone(),
                    other => other.to_string(),
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Python `int(config.get(key) or default)`.
fn config_number(config: &Value, key: &str, fallback: i64) -> i64 {
    let value = match config.get(key) {
        Some(value) => value,
        None => return fallback,
    };
    match value {
        Value::Null => fallback,
        Value::Bool(true) => 1,
        Value::Bool(false) => fallback,
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                if int == 0 {
                    fallback
                } else {
                    int
                }
            } else if let Some(float) = number.as_f64() {
                if float == 0.0 {
                    fallback
                } else {
                    float as i64
                }
            } else {
                fallback
            }
        }
        Value::String(text) => {
            if text.is_empty() {
                fallback
            } else {
                text.trim().parse::<i64>().unwrap_or(fallback)
            }
        }
        // Python `int()` would raise here; degrade to the default instead.
        _ => fallback,
    }
}

/// Python `_short_project`.
fn short_project(value: &str) -> String {
    let text = py_strip(value);
    if text.is_empty() {
        return String::new();
    }
    // Windows drive roots keep their separator in `pathlib.PurePath.parts`.
    let bytes = text.as_bytes();
    if bytes.len() == 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' {
        return text.to_string();
    }
    if bytes.len() == 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && (bytes[2] == b'\\' || bytes[2] == b'/')
    {
        return text.to_string();
    }
    match text.rsplit(['\\', '/']).find(|part| !part.is_empty()) {
        Some(part) => part.to_string(),
        None => truncate_chars(text, 80),
    }
}

// ---------------------------------------------------------------------------
// public helpers
// ---------------------------------------------------------------------------

/// `3600 -> "1h00m"`, `90 -> "1m"` (Python `format_duration`).
pub fn format_duration(seconds: i64) -> String {
    let seconds = seconds.max(0);
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    if hours > 0 {
        format!("{hours}h{minutes:02}m")
    } else {
        format!("{minutes}m")
    }
}

/// Replace the user home and the two known repo roots with short labels.
pub fn sanitize_text(text: &str) -> String {
    let mut value = text.to_string();
    let home = home_dir().to_string_lossy().to_string();
    if !home.is_empty() {
        value = value.replace(&home, "~");
    }
    value = value.replace(r"D:\xx\wechatbot", "wechatbot");
    value = value.replace(r"D:\xx\lyph", "lyph");
    value
}

/// Collapse whitespace, then hide browser tabs and sensitive windows.
///
/// `Option` is for future use: the current port always returns `Some`.
pub fn redact_title(title: &str, app: &str, config: &Value) -> Option<String> {
    let text = collapse_whitespace(title);
    if text.is_empty() {
        return Some(String::new());
    }
    let app_lower = app.to_lowercase();
    if config_string_list(config, "browser_apps")
        .iter()
        .any(|item| item.to_lowercase() == app_lower)
    {
        return Some("[浏览器页面已隐藏]".to_string());
    }
    let lower = text.to_lowercase();
    if config_string_list(config, "redact_patterns")
        .iter()
        .any(|pattern| lower.contains(&pattern.to_lowercase()))
    {
        return Some("[敏感窗口已隐藏]".to_string());
    }
    Some(truncate_chars(&sanitize_text(&text), 120))
}

/// Group OCR lines per app, dedupe globally, then cap with a character budget.
pub fn ocr_clue_lines(ocr: &[OcrRow], config: &Value) -> Vec<String> {
    if ocr.is_empty() {
        return Vec::new();
    }
    let max_chars = config_number(config, "ocr_max_chars", 4000).max(0);
    let lines_per_app = config_number(config, "ocr_lines_per_app", 30).max(1) as usize;

    let mut order: Vec<(String, Vec<String>)> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    let mut seen: HashSet<String> = HashSet::new();

    for row in ocr {
        let app = {
            let trimmed = py_strip(&row.app);
            if trimmed.is_empty() {
                "unknown".to_string()
            } else {
                trimmed.to_string()
            }
        };
        for raw_line in py_split_lines(&row.text) {
            let line = py_strip(raw_line);
            if line.is_empty() || seen.contains(line) {
                continue;
            }
            seen.insert(line.to_string());
            let position = match index.get(&app) {
                Some(position) => *position,
                None => {
                    order.push((app.clone(), Vec::new()));
                    let position = order.len() - 1;
                    index.insert(app.clone(), position);
                    position
                }
            };
            if order[position].1.len() < lines_per_app {
                order[position].1.push(line.to_string());
            }
        }
    }
    if order.is_empty() {
        return Vec::new();
    }

    let mut lines = vec!["屏幕文字线索（本机 OCR 仅文字，已脱敏，像素不出本机）:".to_string()];
    let mut budget = max_chars;
    for (app, texts) in &order {
        if budget <= 0 {
            break;
        }
        let header = format!("[{app}]");
        lines.push(header.clone());
        budget -= char_count(&header) as i64 + 1;
        for text in texts {
            if budget <= 0 {
                break;
            }
            let mut piece = format!("- {text}");
            if char_count(&piece) as i64 > budget {
                piece = truncate_chars(&piece, budget as usize);
            }
            lines.push(piece.clone());
            budget -= char_count(&piece) as i64 + 1;
        }
    }
    lines
}

/// Time-of-day `HH:MM:SS` from a sample timestamp (Python `_hhmmss`).
fn hhmmss(ts: &str) -> String {
    let text = py_strip(ts);
    let time_part = if let Some(position) = text.find('T') {
        &text[position + 1..]
    } else if let Some(position) = text.find(' ') {
        &text[position + 1..]
    } else {
        text
    };
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    let pattern = PATTERN
        .get_or_init(|| Regex::new(r"^(\d{1,2}):(\d{2}):(\d{2})").expect("static regex"));
    match pattern.captures(time_part) {
        Some(captures) => {
            let hour: u32 = captures[1].parse().unwrap_or(0);
            format!("{:02}:{}:{}", hour, &captures[2], &captures[3])
        }
        None => String::new(),
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct TimelinePoint {
    t: String,
    app: String,
    l: String,
}

/// App-switch boundaries only; the title must be non-empty and `prev_app` is
/// updated only after a point is actually recorded (Python `sample_timeline`).
fn sample_timeline_points(samples: &[SampleRow], limit: usize) -> Vec<TimelinePoint> {
    if limit == 0 {
        return Vec::new();
    }
    let mut result: Vec<TimelinePoint> = Vec::new();
    let mut prev_app: Option<String> = None;
    for item in samples {
        let app = truncate_chars(py_strip(&item.app), 80);
        let title = py_strip(&item.title);
        if title.is_empty() {
            continue;
        }
        if prev_app.as_deref() == Some(app.as_str()) {
            continue;
        }
        let time = hhmmss(&item.ts);
        if time.is_empty() {
            continue;
        }
        result.push(TimelinePoint {
            t: time,
            app: app.clone(),
            l: truncate_chars(title, 120),
        });
        prev_app = Some(app);
        if result.len() >= limit {
            break;
        }
    }
    result
}

/// Serialised sample points (`{"t","app","l"}`) for the sentinel payload.
pub fn sample_timeline(samples: &[SampleRow], limit: usize) -> Vec<Value> {
    sample_timeline_points(samples, limit)
        .into_iter()
        .map(|point| json!({"t": point.t, "app": point.app, "l": point.l}))
        .collect()
}

// ---------------------------------------------------------------------------
// sentinel payload (ordered JSON, matching Python `json.dumps`)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Default)]
struct AppStat {
    app: String,
    seconds: i64,
}

#[derive(Clone, Debug, Default)]
struct BrowserStat {
    category: String,
    visits: i64,
}

#[derive(Clone, Debug, Default)]
struct StatsPayload {
    active_human: String,
    git_commits: i64,
    ai_sessions: usize,
    ai_messages: i64,
    files_changed: i64,
    app_top: Vec<AppStat>,
    browser_categories: Vec<BrowserStat>,
}

#[derive(Clone, Debug, Default)]
struct NotePayload {
    day: String,
    pts: Vec<TimelinePoint>,
    hl: String,
    stats: StatsPayload,
}

/// One string encoded exactly like `json.dumps(..., ensure_ascii=False)`.
fn json_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string())
}

/// `json.dumps(payload, ensure_ascii=False, separators=(",", ":"))`.
///
/// Built by hand instead of going through `serde_json::Value` so the key order
/// stays `v,day,pts,hl,stats` (Python dict order); a `Value` would sort keys and
/// change the bytes.
fn render_payload(payload: &NotePayload) -> String {
    let points = payload
        .pts
        .iter()
        .map(|point| {
            format!(
                "{{\"t\":{},\"app\":{},\"l\":{}}}",
                json_string(&point.t),
                json_string(&point.app),
                json_string(&point.l)
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let app_top = payload
        .stats
        .app_top
        .iter()
        .map(|item| {
            format!(
                "{{\"app\":{},\"seconds\":{}}}",
                json_string(&item.app),
                item.seconds
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let browser = payload
        .stats
        .browser_categories
        .iter()
        .map(|item| {
            format!(
                "{{\"category\":{},\"visits\":{}}}",
                json_string(&item.category),
                item.visits
            )
        })
        .collect::<Vec<_>>()
        .join(",");

    let mut out = String::new();
    out.push_str("{\"v\":1,\"day\":");
    out.push_str(&json_string(&payload.day));
    out.push_str(",\"pts\":[");
    out.push_str(&points);
    out.push_str("],\"hl\":");
    out.push_str(&json_string(&payload.hl));
    out.push_str(",\"stats\":{\"active_human\":");
    out.push_str(&json_string(&payload.stats.active_human));
    out.push_str(",\"git_commits\":");
    out.push_str(&payload.stats.git_commits.to_string());
    out.push_str(",\"ai_sessions\":");
    out.push_str(&payload.stats.ai_sessions.to_string());
    out.push_str(",\"ai_messages\":");
    out.push_str(&payload.stats.ai_messages.to_string());
    out.push_str(",\"files_changed\":");
    out.push_str(&payload.stats.files_changed.to_string());
    out.push_str(",\"app_top\":[");
    out.push_str(&app_top);
    out.push_str("],\"browser_categories\":[");
    out.push_str(&browser);
    out.push_str("]}}");
    out
}

/// Python `_compact_json`: compact single-line JSON, with `--` downgraded so it
/// cannot close the surrounding HTML comment early.
fn compact_json(payload: &NotePayload) -> String {
    render_payload(payload).replace("--", "- -")
}

/// Python `_shrink_payload`: drop timeline tail, then list tails, then hl.
fn shrink_payload(payload: &mut NotePayload, max_bytes: usize) {
    loop {
        if compact_json(payload).as_bytes().len() <= max_bytes {
            break;
        }
        if !payload.pts.is_empty() {
            payload.pts.pop();
            continue;
        }
        let mut trimmed = false;
        if payload.stats.app_top.pop().is_some() {
            trimmed = true;
        } else if payload.stats.browser_categories.pop().is_some() {
            trimmed = true;
        }
        if trimmed {
            continue;
        }
        if char_count(&payload.hl) > HL_MAX_CHARS {
            payload.hl = truncate_chars(&payload.hl, HL_MAX_CHARS);
            continue;
        }
        break;
    }
}

// ---------------------------------------------------------------------------
// evidence lines
// ---------------------------------------------------------------------------

/// Python `_evidence_lines`: fixed Git → AI → browser → files → WorkBuddy order.
fn evidence_lines(evidence: &Evidence) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();

    if !evidence.git.is_empty() {
        lines.push(format!("Git 提交（共 {} 条）:", evidence.git.len()));
        for item in evidence.git.iter().take(20) {
            lines.push(format!("- {}: {}", item.repo, item.subject));
        }
    }

    if evidence.codex.sessions != 0 || evidence.codex.messages != 0 {
        lines.push("AI 会话:".to_string());
        lines.push(format!(
            "- Codex: {} 个会话 / {} 条用户消息",
            evidence.codex.sessions, evidence.codex.messages
        ));
        for (cwd, count) in evidence.codex.cwd.iter().take(6) {
            lines.push(format!("- 项目: {} ({})", short_project(cwd), count));
        }
    }

    if !evidence.browser.is_empty() {
        lines.push("浏览（访问次数，不是小时）:".to_string());
        for item in evidence.browser.iter().take(12) {
            lines.push(format!("- {}: {} 次", item.category, item.visits));
        }
    }

    if !evidence.files.is_empty() {
        lines.push("文件改动（修改文件数）:".to_string());
        for item in evidence.files.iter().take(10) {
            lines.push(format!("- {}: {} 个文件", item.project, item.files));
        }
    }

    if !evidence.workbuddy.is_empty() {
        lines.push("WorkBuddy:".to_string());
        for item in evidence.workbuddy.iter().take(8) {
            let label = if !item.title.is_empty() {
                item.title.clone()
            } else {
                let project = short_project(&item.cwd);
                if project.is_empty() {
                    "session".to_string()
                } else {
                    project
                }
            };
            lines.push(format!("- {}", truncate_chars(&sanitize_text(&label), 100)));
        }
    }

    lines
}

/// Python `Counter.most_common`: count descending, insertion order on ties.
fn most_common<T: Clone>(items: &[(T, i64)], limit: usize) -> Vec<(T, i64)> {
    let mut sorted: Vec<(T, i64)> = items.to_vec();
    sorted.sort_by(|left, right| right.1.cmp(&left.1));
    sorted.truncate(limit);
    sorted
}

// ---------------------------------------------------------------------------
// note assembly
// ---------------------------------------------------------------------------

/// Build one day's note (plain text + `<!--WORKLOG_TL_V1 ... -->` sentinel).
///
/// Returns `""` when there is no sample, no evidence and no OCR row, mirroring
/// `worklog_collector.build_note`.
pub fn build_note(
    day: &str,
    samples: &[SampleRow],
    ocr: &[OcrRow],
    evidence: &Evidence,
    config: &Value,
) -> String {
    let sample_seconds = config_number(config, "sample_seconds", 30).max(5);
    let idle_limit = config_number(config, "idle_skip_seconds", 300).max(1);

    let mut app_seconds: Vec<(String, i64)> = Vec::new();
    let mut app_index: HashMap<String, usize> = HashMap::new();
    let mut title_seconds: Vec<((String, String), i64)> = Vec::new();
    let mut title_index: HashMap<(String, String), usize> = HashMap::new();
    let mut active: i64 = 0;

    for item in samples {
        if item.idle >= idle_limit {
            continue;
        }
        active += sample_seconds;
        let app = {
            let trimmed = py_strip(&item.app);
            if trimmed.is_empty() {
                "unknown".to_string()
            } else {
                trimmed.to_string()
            }
        };
        match app_index.get(&app) {
            Some(position) => app_seconds[*position].1 += sample_seconds,
            None => {
                app_seconds.push((app.clone(), sample_seconds));
                app_index.insert(app.clone(), app_seconds.len() - 1);
            }
        }
        let title = py_strip(&item.title);
        if !title.is_empty() {
            let key = (app, title.to_string());
            match title_index.get(&key) {
                Some(position) => title_seconds[*position].1 += sample_seconds,
                None => {
                    title_seconds.push((key.clone(), sample_seconds));
                    title_index.insert(key, title_seconds.len() - 1);
                }
            }
        }
    }

    let has_evidence = !evidence.git.is_empty()
        || !evidence.browser.is_empty()
        || !evidence.files.is_empty()
        || evidence.codex.sessions != 0
        || evidence.codex.messages != 0
        || !evidence.workbuddy.is_empty();
    if samples.is_empty() && !has_evidence && ocr.is_empty() {
        return String::new();
    }

    // Protected head: always keeps the `[worklog]` marker.
    let mut head_lines: Vec<String> = vec![format!("[worklog] {day} 工作活动")];
    head_lines.push(
        "统计口径：浏览=访问次数，文件=修改文件数，AI 会话=消息数；没有活跃时长数据时不要写工时。"
            .to_string(),
    );
    if active != 0 {
        head_lines.push(format!("活跃时长: {}", format_duration(active)));
    }
    if !app_seconds.is_empty() {
        head_lines.push("应用:".to_string());
        for (app, seconds) in most_common(&app_seconds, 12) {
            head_lines.push(format!("- {app}: {}", format_duration(seconds)));
        }
    }

    let evidence_text = evidence_lines(evidence);

    let mut window_lines: Vec<String> = Vec::new();
    if !title_seconds.is_empty() {
        window_lines.push("窗口片段:".to_string());
        for ((app, title), seconds) in most_common(&title_seconds, 18) {
            window_lines.push(format!("- {} {app}: {title}", format_duration(seconds)));
        }
    }

    let mut ocr_lines = ocr_clue_lines(ocr, config);

    // Sentinel payload — every value here is already redacted text/numbers.
    let top_app = most_common(&app_seconds, 1)
        .first()
        .map(|(app, _)| app.clone())
        .unwrap_or_default();
    let git_count = evidence.git.len() as i64;

    let mut hl_parts = vec![format!("活跃 {}", format_duration(active))];
    if !top_app.is_empty() {
        hl_parts.push(format!("主用 {}", truncate_chars(&top_app, 40)));
    }
    hl_parts.push(format!("Git {git_count} 次提交"));
    let hl = hl_parts.join("，");

    let files_changed = evidence.files.iter().map(|item| item.files).sum();

    let stats = StatsPayload {
        active_human: format_duration(active),
        git_commits: git_count,
        ai_sessions: evidence.codex.sessions,
        ai_messages: evidence.codex.messages,
        files_changed,
        app_top: most_common(&app_seconds, 8)
            .into_iter()
            .map(|(app, seconds)| AppStat { app, seconds })
            .collect(),
        browser_categories: evidence
            .browser
            .iter()
            .take(8)
            .map(|item| BrowserStat {
                category: item.category.clone(),
                visits: item.visits,
            })
            .collect(),
    };

    let mut payload = NotePayload {
        day: day.to_string(),
        pts: sample_timeline_points(samples, 48),
        hl,
        stats,
    };
    shrink_payload(&mut payload, SENTINEL_MAX_BYTES);
    let sentinel = format!("<!--WORKLOG_TL_V1 {} -->", compact_json(&payload));

    // Plain-text budget: <=10000 bytes and leave room for the sentinel.
    let plain_chars_cap = NOTE_MAX_CHARS.saturating_sub(char_count(&sentinel) + 1);

    let assemble = |head: &[String], evidence: &[String], window: &[String], ocr: &[String]| {
        head.iter()
            .chain(evidence.iter())
            .chain(window.iter())
            .chain(ocr.iter())
            .cloned()
            .collect::<Vec<String>>()
            .join("\n")
            .trim()
            .to_string()
    };

    let over_budget =
        |value: &str| value.as_bytes().len() > PLAIN_MAX_BYTES || char_count(value) > plain_chars_cap;

    let mut plain = assemble(&head_lines, &evidence_text, &window_lines, &ocr_lines);
    while over_budget(&plain) && (!ocr_lines.is_empty() || !window_lines.is_empty()) {
        if !ocr_lines.is_empty() {
            ocr_lines.pop(); // OCR tail first
        } else {
            window_lines.pop(); // then window snippets
        }
        plain = assemble(&head_lines, &evidence_text, &window_lines, &ocr_lines);
    }
    if over_budget(&plain) {
        // Extreme fallback: trim the tail, the `[worklog]` head survives.
        plain = truncate_chars(&plain, plain_chars_cap);
        if plain.as_bytes().len() > PLAIN_MAX_BYTES {
            plain = floor_char_prefix(&plain, PLAIN_MAX_BYTES);
        }
    }

    let mut text = format!("{plain}\n{sentinel}");
    text = truncate_chars(&text, NOTE_MAX_CHARS);
    if !text.ends_with(&sentinel) {
        // Truncation broke the sentinel: ship plain text only, still valid.
        text = truncate_chars(&plain, NOTE_MAX_CHARS);
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sample(ts: &str, app: &str, title: &str, idle: i64) -> SampleRow {
        SampleRow {
            ts: ts.to_string(),
            app: app.to_string(),
            title: title.to_string(),
            idle,
        }
    }

    fn ocr_row(app: &str, text: &str) -> OcrRow {
        OcrRow {
            app: app.to_string(),
            text: text.to_string(),
        }
    }

    #[test]
    fn formats_durations_like_python() {
        assert_eq!(format_duration(0), "0m");
        assert_eq!(format_duration(90), "1m");
        assert_eq!(format_duration(59), "0m");
        assert_eq!(format_duration(3600), "1h00m");
        assert_eq!(format_duration(3600 + 5 * 60), "1h05m");
        assert_eq!(format_duration(3599), "59m");
        assert_eq!(format_duration(-10), "0m");
    }

    #[test]
    fn redacts_titles_in_three_branches() {
        let config = json!({
            "browser_apps": ["chrome.exe", "msedge.exe"],
            "redact_patterns": ["密码", "token"]
        });
        assert_eq!(redact_title("", "code.exe", &config), Some(String::new()));
        assert_eq!(
            redact_title("Chrome - secret page", "CHROME.EXE", &config),
            Some("[浏览器页面已隐藏]".to_string())
        );
        assert_eq!(
            redact_title("输入密码", "notepad.exe", &config),
            Some("[敏感窗口已隐藏]".to_string())
        );
        assert_eq!(
            redact_title("  main.py   -   VS\tCode ", "code.exe", &config),
            Some("main.py - VS Code".to_string())
        );
        let long = "窗".repeat(200);
        assert_eq!(
            redact_title(&long, "code.exe", &config).map(|text| text.chars().count()),
            Some(120)
        );
    }

    #[test]
    fn ocr_lines_dedupe_per_app_and_respect_budget() {
        let rows = vec![
            ocr_row("code.exe", "def main():\n    pass"),
            ocr_row("code.exe", "def main():"),
            ocr_row("notepad.exe", "会议纪要\n项目计划"),
        ];
        let lines = ocr_clue_lines(&rows, &json!({}));
        assert_eq!(
            lines.first().map(String::as_str),
            Some("屏幕文字线索（本机 OCR 仅文字，已脱敏，像素不出本机）:")
        );
        assert_eq!(lines.get(1).map(String::as_str), Some("[code.exe]"));
        assert!(lines.contains(&"[notepad.exe]".to_string()));
        assert_eq!(
            lines.iter().filter(|line| *line == "- def main():").count(),
            1
        );
        assert!(lines.contains(&"- 会议纪要".to_string()));
        assert!(lines.contains(&"- 项目计划".to_string()));

        let many: Vec<OcrRow> = (0..40)
            .map(|index| ocr_row("code.exe", &format!("第{index}行屏幕文字内容 abcdefghij")))
            .collect();
        let budget_lines = ocr_clue_lines(&many, &json!({"ocr_max_chars": 40}));
        // The fixed section title is not part of the budget, same as Python.
        assert!(budget_lines.len() > 1);
        assert!(budget_lines[1..].join("\n").chars().count() <= 40);
    }

    #[test]
    fn timeline_only_records_app_switches() {
        let rows = vec![
            sample("2026-11-04T09:00:00+08:00", "code.exe", "a.py", 0),
            sample("2026-11-04T09:00:30+08:00", "code.exe", "a.py", 0),
            sample("2026-11-04T09:01:00+08:00", "msedge.exe", "page", 0),
            sample("2026-11-04T09:01:30+08:00", "msedge.exe", "page", 0),
            sample("2026-11-04T09:02:00+08:00", "code.exe", "b.py", 0),
            sample("2026-11-04T09:02:30+08:00", "code.exe", "", 0),
        ];
        assert_eq!(
            sample_timeline(&rows, 48),
            vec![
                json!({"t": "09:00:00", "app": "code.exe", "l": "a.py"}),
                json!({"t": "09:01:00", "app": "msedge.exe", "l": "page"}),
                json!({"t": "09:02:00", "app": "code.exe", "l": "b.py"}),
            ]
        );

        // A skipped point (bad ts / empty title) must not advance prev_app.
        let odd = vec![
            sample("2026-11-04T09:00:00+08:00", "code.exe", "a.py", 0),
            sample("not-a-timestamp", "msedge.exe", "x", 0),
            sample("2026-11-04T09:03:00+08:00", "msedge.exe", "y", 0),
        ];
        let points = sample_timeline(&odd, 48);
        assert_eq!(points.len(), 2);
        assert_eq!(points[1]["app"], "msedge.exe");
        assert_eq!(points[1]["t"], "09:03:00");
    }

    #[test]
    fn empty_input_builds_no_note() {
        let note = build_note("2026-11-04", &[], &[], &Evidence::default(), &json!({}));
        assert_eq!(note, "");
    }

    #[test]
    fn minimal_sample_builds_note_with_sentinel() {
        let rows = vec![sample(
            "2026-11-04T09:00:00+08:00",
            "code.exe",
            "main.py - VS Code",
            0,
        )];
        let note = build_note(
            "2026-11-04",
            &rows,
            &[],
            &Evidence::default(),
            &json!({}),
        );
        assert!(note.contains("[worklog] 2026-11-04 工作活动"));
        assert!(note.contains("活跃时长: 0m"));
        assert!(note.contains("- code.exe: 0m"));
        assert!(
            note.contains("<!--WORKLOG_TL_V1 "),
            "sentinel missing: {note}"
        );
        assert!(note.trim_end().ends_with("-->"));
        assert!(note.contains("\"v\":1,\"day\":\"2026-11-04\",\"pts\":[{\"t\":\"09:00:00\""));

        let payload = note
            .split_once("<!--WORKLOG_TL_V1 ")
            .and_then(|(_, rest)| rest.rsplit_once(" -->"))
            .map(|(json_text, _)| json_text)
            .expect("sentinel body");
        let parsed: Value = serde_json::from_str(payload).expect("sentinel json");
        assert_eq!(parsed["v"], 1);
        assert_eq!(parsed["day"], "2026-11-04");
        assert_eq!(parsed["stats"]["git_commits"], 0);
        assert_eq!(parsed["pts"].as_array().map(|items| items.len()), Some(1));
    }
}
