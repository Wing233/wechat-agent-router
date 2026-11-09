//! Worklog (daily report) module: local sampling, OCR, note building and upload.

pub mod config;
pub mod db;
pub mod note;
pub mod ocr;
pub mod sample;
pub mod service;
pub mod sources;
pub mod upload;

pub use service::WorklogService;
