use crate::{client, json, readonly, Config};
use serde_json::{json, Value};
use std::{
    io::{self, BufRead, BufReader, Read, Write},
    net::{Shutdown, TcpStream},
    time::SystemTime,
};
use subtle::ConstantTimeEq;

const MAX_BODY: usize = 65536;
const MAX_LINE: u64 = 65536;
const PYTHON_VERSION: &str = match option_env!("NCM_BRIDGE_PYTHON_VERSION") {
    Some(version) => version,
    None => "3.13.17",
};

pub fn serve(stream: TcpStream, config: &Config) -> io::Result<()> {
    let mut reader = BufReader::new(stream);
    let Some(request) = read_request(&mut reader)? else {
        return Ok(());
    };
    let response = match request.error {
        Some(response) => response,
        None => match (request.method.as_str(), request.path.as_str()) {
            ("GET", "/health") => Response::json(
                200,
                json!({
                    "ok": true, "readonly": true, "service": "ncm-cli-bridge", "commands": readonly::COMMANDS.len()
                }),
            ),
            ("POST", "/run") => {
                let Some(response) = run(&mut reader, &request, config)? else {
                    return Ok(());
                };
                response
            }
            ("GET" | "POST", _) => Response::error(404, "not_found", "not found"),
            (method, _) => Response::html(
                501,
                &format!("Unsupported method ({})", json::repr_string(method)),
                "Server does not support this operation",
            ),
        },
    };
    let status = response.status;
    if response.connection_close {
        eprintln!(
            "[ncm-bridge] code {status}, message {}",
            quote_log(&response.reason)
        );
    }
    eprintln!("[ncm-bridge] \"{}\" {status} -", quote_log(&request.line));
    response.write(
        reader.get_mut(),
        request.version == "HTTP/0.9",
        request.method == "HEAD",
    )
}

fn run(
    reader: &mut impl BufRead,
    request: &Request,
    config: &Config,
) -> io::Result<Option<Response>> {
    let token = request.header("authorization").strip_prefix("Bearer ");
    if token.is_some_and(|token| !token.is_ascii() || !config.token.is_ascii()) {
        // CPython's compare_digest on str raises before it sends a response for non-ASCII text.
        return Ok(None);
    }
    if !token.is_some_and(|token| bool::from(token.as_bytes().ct_eq(config.token.as_bytes()))) {
        return Ok(Some(Response::error(
            401,
            "unauthorized",
            "invalid bridge token",
        )));
    }
    let length = readonly::integer(request.header("content-length"))
        .and_then(|value| value.parse::<usize>().ok());
    let Some(length) = length.filter(|len| (1..=MAX_BODY).contains(len)) else {
        return Ok(Some(Response::error(
            413,
            "body_too_large",
            "request body is empty or too large",
        )));
    };

    let mut content = Vec::with_capacity(length);
    reader.take(length as u64).read_to_end(&mut content)?;
    let body = match std::str::from_utf8(&content).ok().and_then(json::decode) {
        Some(value) => value,
        None => {
            return Ok(Some(Response::error(
                400,
                "invalid_json",
                "request body must be JSON",
            )))
        }
    };
    if !body.is_object() {
        return Ok(Some(Response::error(
            400,
            "invalid_json",
            "request body must be an object",
        )));
    }
    let args = match readonly::validate_args(body.get("args")) {
        Ok(args) => args,
        Err(err) => return Ok(Some(Response::error(403, "forbidden_command", &err))),
    };
    Ok(Some(
        match client::run(&config.binary, &args, config.timeout) {
            Ok(data) => Response::json(
                200,
                Value::Object(serde_json::Map::from_iter([
                    ("ok".into(), Value::Bool(true)),
                    ("data".into(), data),
                ])),
            ),
            Err(err) => Response::error(err.status, &err.code, &err.message),
        },
    ))
}

#[derive(Default)]
struct Request {
    line: String,
    method: String,
    path: String,
    version: String,
    headers: Vec<(String, String)>,
    error: Option<Response>,
}

impl Request {
    fn header(&self, name: &str) -> &str {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map_or("", |(_, value)| value)
    }

    fn error(mut self, status: u16, reason: &str, explanation: &str) -> Option<Self> {
        self.error = Some(Response::html(status, reason, explanation));
        Some(self)
    }
}

fn read_line(reader: &mut impl BufRead) -> io::Result<Vec<u8>> {
    let mut line = Vec::new();
    reader.take(MAX_LINE + 1).read_until(b'\n', &mut line)?;
    Ok(line)
}

fn latin1(bytes: &[u8]) -> String {
    bytes.iter().copied().map(char::from).collect()
}

fn read_request(reader: &mut impl BufRead) -> io::Result<Option<Request>> {
    let raw = read_line(reader)?;
    let mut request = Request::default();
    if raw.len() as u64 > MAX_LINE {
        return Ok(request.error(414, "URI Too Long", "URI is too long"));
    }
    request.line = latin1(&raw).trim_end_matches(['\r', '\n']).into();
    let words: Vec<_> = request
        .line
        .split(readonly::whitespace)
        .filter(|word| !word.is_empty())
        .collect();
    if words.is_empty() {
        return Ok(None);
    }
    let explanation = "Bad request syntax or unsupported method";
    if words.len() >= 3 {
        let version = words[words.len() - 1];
        let split = version
            .strip_prefix("HTTP/")
            .and_then(|value| value.split_once('.'));
        let part = |value: &str| -> Option<u64> {
            if value.len() <= 10 && value.bytes().all(|byte| byte.is_ascii_digit()) {
                value.parse().ok()
            } else {
                None
            }
        };
        let Some((major, _)) = split.and_then(|(major, minor)| Some((part(major)?, part(minor)?)))
        else {
            let reason = format!("Bad request version ({})", json::repr_string(version));
            return Ok(request.error(400, &reason, explanation));
        };
        request.version = version.into();
        if major >= 2 {
            let reason = format!("Invalid HTTP version ({})", &version[5..]);
            return Ok(request.error(505, &reason, "Cannot fulfill request"));
        }
    }
    if !(2..=3).contains(&words.len()) {
        let reason = format!("Bad request syntax ({})", json::repr_string(&request.line));
        request.version.clear();
        return Ok(request.error(400, &reason, explanation));
    }
    if words.len() == 2 && words[0] != "GET" {
        let reason = format!(
            "Bad HTTP/0.9 request type ({})",
            json::repr_string(words[0])
        );
        return Ok(request.error(400, &reason, explanation));
    }
    request.method = words[0].into();
    request.path = if words[1].starts_with("//") {
        format!("/{}", words[1].trim_start_matches('/'))
    } else {
        words[1].into()
    };
    if words.len() == 2 {
        request.version = "HTTP/0.9".into();
        return Ok(Some(request));
    }

    let mut parsing_headers = true;
    let mut continuation = "\r\n";
    for count in 1.. {
        let line = read_line(reader)?;
        if line.len() as u64 > MAX_LINE {
            return Ok(request.error(
                431,
                "Line too long",
                "got more than 65536 bytes when reading header line",
            ));
        }
        if count > 100 {
            return Ok(request.error(431, "Too many headers", "got more than 100 headers"));
        }
        if matches!(line.as_slice(), b"" | b"\r\n" | b"\n") {
            break;
        }
        if !parsing_headers {
            continue;
        }
        let value = latin1(&line);
        let value = value.trim_end_matches(['\r', '\n']);
        if value.starts_with([' ', '\t']) {
            if let Some((_, previous)) = request.headers.last_mut() {
                previous.push_str(continuation);
                previous.push_str(value);
            }
        } else if let Some((key, value)) = value.split_once(':') {
            if key.is_empty() {
                continue;
            }
            if !key.bytes().all(|byte| (33..=126).contains(&byte)) {
                parsing_headers = false;
            } else {
                request
                    .headers
                    .push((key.into(), value.trim_start_matches([' ', '\t']).into()));
            }
        } else if !value.starts_with("From ") {
            parsing_headers = false;
        }
        continuation = if line.ends_with(b"\r\n") {
            "\r\n"
        } else {
            "\n"
        };
    }
    Ok(Some(request))
}

fn quote_log(value: &str) -> String {
    let mut clean = String::new();
    for char in value.chars() {
        if char == '\\' {
            clean.push_str("\\\\");
        } else if (char as u32) < 32 || (0x7f..=0x9f).contains(&(char as u32)) {
            clean.push_str(&format!("\\x{:02x}", char as u32));
        } else {
            clean.push(char);
        }
    }
    clean
}

struct Response {
    status: u16,
    reason: String,
    content_type: &'static str,
    content: String,
    connection_close: bool,
}

impl Response {
    fn json(status: u16, body: Value) -> Self {
        let reason = match status {
            200 => "OK",
            400 => "Bad Request",
            401 => "Unauthorized",
            403 => "Forbidden",
            404 => "Not Found",
            413 => "Content Too Large",
            500 => "Internal Server Error",
            502 => "Bad Gateway",
            _ => "Unknown",
        };
        Self {
            status,
            reason: reason.into(),
            content_type: "application/json; charset=utf-8",
            content: json::encode(&body),
            connection_close: false,
        }
    }

    fn error(status: u16, code: &str, message: &str) -> Self {
        Self::json(
            status,
            json!({"ok": false, "error": {"code": code, "message": message}}),
        )
    }

    fn html(status: u16, reason: &str, explanation: &str) -> Self {
        let reason_html = html_escape(reason);
        let explanation = html_escape(explanation);
        let mut version = PYTHON_VERSION
            .split('.')
            .filter_map(|part| part.parse::<u16>().ok());
        let style = if (version.next(), version.next()) >= (Some(3), Some(14)) {
            "        <style type=\"text/css\">\n            :root {\n                color-scheme: light dark;\n            }\n        </style>\n"
        } else {
            ""
        };
        let content = format!("<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n{style}        <title>Error response</title>\n    </head>\n    <body>\n        <h1>Error response</h1>\n        <p>Error code: {status}</p>\n        <p>Message: {reason_html}.</p>\n        <p>Error code explanation: {status} - {explanation}.</p>\n    </body>\n</html>\n");
        Self {
            status,
            reason: reason.into(),
            content_type: "text/html;charset=utf-8",
            content,
            connection_close: true,
        }
    }

    fn write(self, stream: &mut TcpStream, version_09: bool, head_only: bool) -> io::Result<()> {
        if !version_09 {
            let status = self.status;
            let reason: Vec<_> = self.reason.chars().map(|ch| ch as u8).collect();
            let date = httpdate::fmt_http_date(SystemTime::now());
            let length = self.content.len();
            let content_type = self.content_type;
            let close = if self.connection_close {
                "Connection: close\r\n"
            } else {
                ""
            };
            let cache = if self.connection_close {
                ""
            } else {
                "Cache-Control: no-store\r\n"
            };
            let mut response = format!("HTTP/1.0 {status} ").into_bytes();
            response.extend(reason);
            write!(response, "\r\nServer: ncm-bridge/1.0 Python/{PYTHON_VERSION}\r\nDate: {date}\r\n{close}Content-Type: {content_type}\r\nContent-Length: {length}\r\n{cache}\r\n")?;
            stream.write_all(&response)?;
        }
        if !head_only {
            stream.write_all(self.content.as_bytes())?;
        }
        stream.flush()?;
        // Match socketserver.shutdown_request before dropping a socket whose
        // request body may still be unread (for example, a 413 response).
        stream.shutdown(Shutdown::Write)
    }
}

fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}
