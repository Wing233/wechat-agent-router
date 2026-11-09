//! Read-only probe: build today's note from the live worklog DB and print
//! only aggregate facts (never the recognized text itself).
//!
//! Run with: cargo run --example note_probe -- <worklog-dir>

use std::time::Instant;
use wechat_agent_desktop::worklog::{config, db::WorklogDb, note, sources};

fn main() {
    let root = std::env::args()
        .nth(1)
        .unwrap_or_else(|| std::env::var("WECHAT_AGENT_DATA").unwrap_or_default());
    if root.is_empty() {
        eprintln!("用法：note_probe <worklog 数据目录>");
        std::process::exit(2);
    }
    let root = std::path::PathBuf::from(root);
    let config_path = root.join("config.json");
    let cfg = config::load(&config_path);
    let day = chrono::Local::now().format("%Y-%m-%d").to_string();
    let db = WorklogDb::new(root.join("worklog.sqlite3"));

    let started = Instant::now();
    let samples = db.samples(&day).unwrap_or_default();
    let ocr = db.ocr_samples(&day).unwrap_or_default();
    let evidence_started = Instant::now();
    let evidence = sources::collect_evidence(&day, &cfg);
    let evidence_ms = evidence_started.elapsed().as_millis();
    let note = note::build_note(&day, &samples, &ocr, &evidence, &cfg);
    let total_ms = started.elapsed().as_millis();

    println!("day={day}");
    println!("samples={} ocr_rows={}", samples.len(), ocr.len());
    println!(
        "evidence git={} browser={} files={} codex_sessions={} workbuddy={} ({evidence_ms}ms)",
        evidence.git.len(),
        evidence.browser.len(),
        evidence.files.len(),
        evidence.codex.sessions,
        evidence.workbuddy.len()
    );
    println!(
        "note chars={} bytes={} total={total_ms}ms",
        note.chars().count(),
        note.len()
    );
    for marker in [
        "[worklog]",
        "活跃时长",
        "Git 提交",
        "AI 会话",
        "浏览",
        "文件改动",
        "屏幕文字线索",
        "<!--WORKLOG_TL_V1 ",
    ] {
        println!("  marker {marker}: {}", note.contains(marker));
    }
    let minimum = config::get_i64(&cfg, "min_note_chars", 100).max(0) as usize;
    println!(
        "meets min_note_chars({minimum}): {}",
        note.trim().chars().count() >= minimum
    );
    println!("publisher length>=50: {}", note.trim().chars().count() >= 50);
}
