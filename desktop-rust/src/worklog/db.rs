//! Local SQLite store for worklog samples and per-day report state.
//!
//! Schema is intentionally identical to the retired Python collector so an
//! existing `worklog.sqlite3` keeps working after the Rust desktop upgrade.

use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Default)]
pub struct SampleRow {
    pub ts: String,
    pub app: String,
    pub title: String,
    pub idle: i64,
}

#[derive(Clone, Debug, Default)]
pub struct OcrRow {
    pub app: String,
    pub text: String,
}

#[derive(Clone, Debug, Default)]
pub struct DayRow {
    pub day: String,
    pub note: String,
    pub capture_id: String,
    pub status: String,
    pub updated_at: String,
    pub chars: i64,
}

pub struct WorklogDb {
    path: PathBuf,
}

impl WorklogDb {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn conn(&self) -> Result<Connection> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(&self.path)
            .with_context(|| format!("打开工作日志数据库失败：{}", self.path.display()))?;
        conn.busy_timeout(std::time::Duration::from_secs(10))?;
        let _: String = conn.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))?;
        Ok(conn)
    }

    pub fn init(&self) -> Result<()> {
        let conn = self.conn()?;
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS samples(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL, day TEXT NOT NULL, app TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '', idle INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS samples_day ON samples(day, id);
            CREATE TABLE IF NOT EXISTS days(
              day TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '',
              capture_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ocr_samples(
              ts TEXT NOT NULL, day TEXT NOT NULL, app TEXT NOT NULL,
              ocr_text TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ocr_samples_day ON ocr_samples(day);
            "#,
        )?;
        Ok(())
    }

    pub fn add_sample(&self, day: &str, app: &str, title: &str, idle: i64, ts: &str) -> Result<()> {
        let conn = self.conn()?;
        conn.execute(
            "INSERT INTO samples(ts,day,app,title,idle) VALUES(?,?,?,?,?)",
            params![ts, day, truncate(app, 80), truncate(title, 160), idle.max(0)],
        )?;
        Ok(())
    }

    pub fn samples(&self, day: &str) -> Result<Vec<SampleRow>> {
        let conn = self.conn()?;
        let mut stmt = conn.prepare(
            "SELECT ts,app,title,idle FROM samples WHERE day=? ORDER BY id",
        )?;
        let rows = stmt
            .query_map(params![day], |row| {
                Ok(SampleRow {
                    ts: row.get(0)?,
                    app: row.get(1)?,
                    title: row.get(2)?,
                    idle: row.get(3)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn add_ocr_sample(&self, day: &str, app: &str, text: &str, ts: &str) -> Result<()> {
        let conn = self.conn()?;
        conn.execute(
            "INSERT INTO ocr_samples(ts,day,app,ocr_text) VALUES(?,?,?,?)",
            params![ts, day, truncate(app, 80), text],
        )?;
        Ok(())
    }

    pub fn ocr_samples(&self, day: &str) -> Result<Vec<OcrRow>> {
        let conn = self.conn()?;
        let mut stmt =
            conn.prepare("SELECT app,ocr_text FROM ocr_samples WHERE day=? ORDER BY rowid")?;
        let rows = stmt
            .query_map(params![day], |row| {
                Ok(OcrRow {
                    app: row.get(0)?,
                    text: row.get(1)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn day(&self, day: &str) -> Result<Option<DayRow>> {
        let conn = self.conn()?;
        let row = conn
            .query_row(
                "SELECT day,note,capture_id,status,updated_at,length(note) \
                 FROM days WHERE day=?",
                params![day],
                |row| {
                    Ok(DayRow {
                        day: row.get(0)?,
                        note: row.get(1)?,
                        capture_id: row.get(2)?,
                        status: row.get(3)?,
                        updated_at: row.get(4)?,
                        chars: row.get(5)?,
                    })
                },
            )
            .optional()?;
        Ok(row)
    }

    pub fn save_day(&self, day: &str, note: &str, capture_id: &str, status: &str) -> Result<()> {
        let conn = self.conn()?;
        let now = chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
        conn.execute(
            "INSERT INTO days(day,note,capture_id,status,updated_at) VALUES(?,?,?,?,?) \
             ON CONFLICT(day) DO UPDATE SET note=excluded.note,\
             capture_id=excluded.capture_id,status=excluded.status,updated_at=excluded.updated_at",
            params![day, truncate(note, 16000), truncate(capture_id, 100), status, now],
        )?;
        Ok(())
    }

    pub fn recent_days(&self, limit: usize) -> Result<Vec<DayRow>> {
        let conn = self.conn()?;
        let mut stmt = conn.prepare(
            "SELECT day,note,capture_id,status,updated_at,length(note) FROM days \
             ORDER BY day DESC LIMIT ?",
        )?;
        let rows = stmt
            .query_map(params![limit as i64], |row| {
                Ok(DayRow {
                    day: row.get(0)?,
                    note: row.get(1)?,
                    capture_id: row.get(2)?,
                    status: row.get(3)?,
                    updated_at: row.get(4)?,
                    chars: row.get(5)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn samples_count(&self, day: &str) -> Result<i64> {
        let conn = self.conn()?;
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM samples WHERE day=?",
            params![day],
            |row| row.get(0),
        )?;
        Ok(count)
    }

    pub fn has_samples(&self, day: &str) -> Result<bool> {
        Ok(self.samples_count(day)? > 0)
    }

    pub fn state_get(&self, key: &str) -> Result<Option<String>> {
        let conn = self.conn()?;
        let value = conn
            .query_row("SELECT value FROM state WHERE key=?", params![key], |row| {
                row.get(0)
            })
            .optional()?;
        Ok(value)
    }

    pub fn state_set(&self, key: &str, value: &str) -> Result<()> {
        let conn = self.conn()?;
        conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) \
             ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn prune(&self, days: i64) -> Result<()> {
        let cutoff = (chrono::Local::now() - chrono::Duration::days(days))
            .format("%Y-%m-%d")
            .to_string();
        let conn = self.conn()?;
        conn.execute("DELETE FROM samples WHERE day < ?", params![cutoff])?;
        conn.execute("DELETE FROM ocr_samples WHERE day < ?", params![cutoff])?;
        Ok(())
    }
}

fn truncate(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}
