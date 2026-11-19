use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use axum::body::{Body, to_bytes};
use axum::http::{Method, Request, StatusCode, header};
use axum::response::Response;
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Value, json};
use tokio::sync::OnceCell;

use crate::browser::{Browser, Page};
use crate::payload;

const BODY_LIMIT: usize = 64 * 1024;
const DEFAULT_ORIGINS: &str = "https://lyph-nginx-1:8443,http://web:3000";

pub struct Service {
    token: String,
    allowed: Vec<String>,
    busy: AtomicBool,
    browser: OnceCell<Result<Browser, String>>,
}

impl Service {
    pub fn from_env() -> Self {
        let token = std::env::var("RENDER_TOKEN").unwrap_or_default();
        let allowed = std::env::var("ALLOWED_ORIGINS")
            .ok()
            .filter(|value| !value.is_empty());
        Self::new(token, allowed.as_deref().unwrap_or(DEFAULT_ORIGINS))
    }

    pub fn new(token: String, allowed: &str) -> Self {
        Self {
            token,
            allowed: allowed
                .split(',')
                .map(|item| item.trim_matches(payload::js_whitespace))
                .filter(|item| !item.is_empty())
                .map(str::to_owned)
                .collect(),
            busy: AtomicBool::new(false),
            browser: OnceCell::new(),
        }
    }

    pub async fn handle(self: Arc<Self>, request: Request<Body>) -> Response {
        let uri = request.uri().to_string();
        if request.method() == Method::GET && uri == "/health" {
            return json_response(
                StatusCode::OK,
                json!({"ok":true,"busy":self.busy.load(Ordering::Acquire)}),
            );
        }
        if request.method() != Method::POST || uri != "/render" {
            return Error::new(StatusCode::NOT_FOUND, "not found").response();
        }
        let token = request
            .headers()
            .get_all("x-render-token")
            .iter()
            .map(|token| {
                token
                    .as_bytes()
                    .iter()
                    .copied()
                    .map(char::from)
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join(", ");
        if self.token.is_empty() || token != self.token {
            return Error::new(StatusCode::UNAUTHORIZED, "invalid render token").response();
        }
        if self.busy.swap(true, Ordering::AcqRel) {
            return Error::new(StatusCode::TOO_MANY_REQUESTS, "renderer busy").response();
        }
        let busy = Busy(self);
        let result = tokio::spawn(async move {
            let result = busy.0.render_request(request.into_body()).await;
            drop(busy);
            result
        })
        .await;
        match result {
            Ok(Ok(value)) => json_response(StatusCode::OK, value),
            Ok(Err(error)) => {
                if error.status == StatusCode::UNPROCESSABLE_ENTITY {
                    return error.response();
                }
                eprintln!("render failed: {}", error.message);
                error.response()
            }
            Err(_) => Error::internal("render failed").response(),
        }
    }

    async fn render_request(&self, body: Body) -> Result<Value, Error> {
        let bytes = to_bytes(body, BODY_LIMIT).await.map_err(|error| {
            use std::error::Error as _;
            let detail = error.source().map_or_else(String::new, ToString::to_string);
            Error::internal(if detail.contains("length limit exceeded") {
                "body too large"
            } else {
                "aborted"
            })
        })?;
        let payload = payload::parse(&String::from_utf8_lossy(&bytes)).map_err(Error::internal)?;
        let target = payload::target(&payload, &self.allowed)
            .map_err(Error::internal)?
            .ok_or_else(|| Error::new(StatusCode::FORBIDDEN, "url not allowed"))?;
        let timeout = payload::timeout(&payload).map_err(Error::internal)?;
        let browser = self
            .browser
            .get_or_init(|| async { Browser::launch().await.map_err(|error| error.to_string()) })
            .await
            .as_ref()
            .map_err(Error::internal)?;
        let mut page = browser
            .open(&self.allowed)
            .await
            .map_err(|error| Error::internal(error.to_string()))?;
        let result = render_page(&mut page, target.as_str(), timeout).await;
        page.close().await;
        result
    }
}

pub fn port_from_env() -> Result<u16, String> {
    let port = std::env::var("PORT").ok().filter(|value| !value.is_empty());
    let number = port.as_deref().map_or(9801.0, payload::js_number);
    if number.is_finite() && (0.0..=65535.0).contains(&number) {
        Ok(number as u16)
    } else {
        Err(format!(
            "invalid PORT: {}",
            port.as_deref().unwrap_or_default()
        ))
    }
}

struct Busy(Arc<Service>);

impl Drop for Busy {
    fn drop(&mut self) {
        self.0.busy.store(false, Ordering::Release);
    }
}

async fn render_page(page: &mut Page<'_>, target: &str, limit: f64) -> Result<Value, Error> {
    let (dim, reset) = if color_call_log() {
        ("\x1b[2m", "\x1b[22m")
    } else {
        ("", "")
    };
    let call_log = format!(
        "\nCall log:\n{dim}  - navigating to \"{target}\", waiting until \"networkidle\"{reset}\n"
    );
    let navigation = page.navigate(target);
    let status = if limit.is_nan() {
        navigation.await
    } else {
        tokio::time::timeout(Duration::from_millis(limit as u64), navigation)
            .await
            .map_err(|_| {
                Error::internal(format!("page.goto: Timeout {limit}ms exceeded.{call_log}"))
            })?
    }
    .map_err(|error| Error::internal(format!("page.goto: {error}{call_log}")))?;
    if status.is_none_or(|status| status >= 400) {
        let status = status.map_or_else(|| "no response".to_owned(), |status| status.to_string());
        return Err(Error::new(
            StatusCode::BAD_GATEWAY,
            format!("print page HTTP {status}"),
        ));
    }
    let height = page
        .height()
        .await
        .map_err(|error| Error::internal(error.to_string()))?;
    if height > 1130 {
        return Err(Error {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            message: "overflow".into(),
            height: Some(height),
        });
    }
    let pdf = page
        .pdf()
        .await
        .map_err(|error| Error::internal(error.to_string()))?;
    let pages = lopdf::Document::load_mem(&pdf)
        .map_err(|error| Error::internal(error.to_string()))?
        .page_iter()
        .count();
    Ok(
        json!({"overflow":false,"height":height,"pages":pages,"bytes":pdf.len(),"pdf_base64":STANDARD.encode(pdf)}),
    )
}

// Playwright 1.49.1's bundled colors checks the environment without a stream.
// A plain Linux service emits plain text, and Windows enables dim by default.
fn color_call_log() -> bool {
    use std::env::{var, var_os};

    if let Ok(force) = var("FORCE_COLOR") {
        let text = force.trim_start_matches(payload::js_whitespace);
        let digits = text.strip_prefix(['+', '-']).unwrap_or(text).as_bytes();
        return !digits.first().is_some_and(u8::is_ascii_digit)
            || digits
                .iter()
                .take_while(|b| b.is_ascii_digit())
                .any(|b| *b != b'0');
    }
    if cfg!(windows) {
        return true;
    }
    if var_os("CI").is_some() {
        return ["TRAVIS", "CIRCLECI", "APPVEYOR", "GITLAB_CI"]
            .iter()
            .any(|key| var_os(key).is_some())
            || var("CI_NAME").is_ok_and(|name| name == "codeship");
    }
    if let Ok(version) = var("TEAMCITY_VERSION") {
        let mut parts = version.splitn(3, '.');
        return match (parts.next(), parts.next(), parts.next()) {
            (Some(major), Some(minor), Some(_)) => {
                major.parse::<u32>().is_ok_and(|n| n >= 10)
                    || (major == "9" && minor.parse::<u32>().is_ok_and(|n| n >= 1))
            }
            _ => false,
        };
    }
    let terminal = var("TERM").unwrap_or_default().to_ascii_lowercase();
    var_os("COLORTERM").is_some()
        || var("TERM_PROGRAM")
            .is_ok_and(|name| matches!(name.as_str(), "iTerm.app" | "Hyper" | "Apple_Terminal"))
        || ["screen", "xterm", "vt100", "rxvt"]
            .iter()
            .any(|prefix| terminal.starts_with(prefix))
        || ["color", "ansi", "cygwin", "linux"]
            .iter()
            .any(|pattern| terminal.contains(pattern))
        || terminal.ends_with("-256")
}

struct Error {
    status: StatusCode,
    message: String,
    height: Option<u64>,
}

impl Error {
    fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
            height: None,
        }
    }

    fn internal(message: impl Into<String>) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, message)
    }

    fn response(self) -> Response {
        let value = match self.height {
            Some(height) => json!({"error":self.message,"height":height}),
            None => json!({"error":self.message}),
        };
        json_response(self.status, value)
    }
}

fn json_response(status: StatusCode, value: Value) -> Response {
    let body = value.to_string();
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json; charset=utf-8")
        .header(header::CONTENT_LENGTH, body.len())
        .header(header::CACHE_CONTROL, "no-store")
        .body(Body::from(body))
        // INFALLIBLE: status and headers above have fixed names and valid values.
        .expect("valid JSON response")
}
