use serde_json::Value;
use url::Url;

pub(crate) fn origin(raw: &str) -> Option<String> {
    let url = Url::parse(raw).ok()?;
    Some(url_origin(&url))
}

fn url_origin(url: &Url) -> String {
    let mut origin = format!("{}://{}", url.scheme(), url.host_str().unwrap_or_default());
    if let Some(port) = url.port() {
        origin.push_str(&format!(":{port}"));
    }
    origin
}

pub(crate) fn target(payload: &Value, allowed: &[String]) -> Result<Option<Url>, String> {
    if payload.is_null() {
        return Err("Cannot read properties of null (reading 'url')".into());
    }
    let Some(raw) = payload.get("url").filter(|value| truthy(value)) else {
        return Ok(None);
    };
    let Ok(url) = Url::parse(&js_string(raw)?) else {
        return Ok(None);
    };
    if matches!(url.scheme(), "http" | "https")
        && url.path().starts_with("/print/resume/")
        && allowed.contains(&url_origin(&url))
    {
        Ok(Some(url))
    } else {
        Ok(None)
    }
}

pub(crate) fn timeout(payload: &Value) -> Result<f64, String> {
    let Some(raw) = payload.get("timeout_ms").filter(|value| truthy(value)) else {
        return Ok(45000.0);
    };
    let value = match raw {
        Value::Bool(value) => u8::from(*value).into(),
        Value::Number(value) => value.to_string().parse().unwrap_or(f64::NAN),
        _ => js_number(&js_string(raw)?),
    };
    Ok(value.clamp(5000.0, 60000.0))
}

pub(crate) fn js_number(raw: &str) -> f64 {
    let text = raw.trim_matches(js_whitespace);
    match text {
        "" => 0.0,
        "Infinity" | "+Infinity" => f64::INFINITY,
        "-Infinity" => f64::NEG_INFINITY,
        _ => {
            if let Some(base) = match text.as_bytes().get(..2) {
                Some(b"0x" | b"0X") => Some(16),
                Some(b"0o" | b"0O") => Some(8),
                Some(b"0b" | b"0B") => Some(2),
                _ => None,
            } {
                if text.len() == 2 {
                    return f64::NAN;
                }
                return text[2..].chars().fold(0.0, |number, c| {
                    c.to_digit(base).map_or(f64::NAN, |digit| {
                        number * f64::from(base) + f64::from(digit)
                    })
                });
            }
            if text
                .bytes()
                .all(|c| c.is_ascii_digit() || matches!(c, b'.' | b'+' | b'-' | b'e' | b'E'))
            {
                text.parse().unwrap_or(f64::NAN)
            } else {
                f64::NAN
            }
        }
    }
}

pub(crate) fn js_whitespace(c: char) -> bool {
    matches!(c, '\u{0009}'..='\u{000D}' | ' ' | '\u{00A0}' | '\u{1680}' | '\u{2000}'..='\u{200A}'
        | '\u{2028}' | '\u{2029}' | '\u{202F}' | '\u{205F}' | '\u{3000}' | '\u{FEFF}')
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value
            .to_string()
            .parse::<f64>()
            .is_ok_and(|number| number != 0.0),
        Value::String(value) => !value.is_empty(),
        _ => true,
    }
}

fn js_string(value: &Value) -> Result<String, String> {
    match value {
        Value::String(value) => Ok(value.clone()),
        Value::Array(values) => values
            .iter()
            .map(|value| {
                if value.is_null() {
                    Ok(String::new())
                } else {
                    js_string(value)
                }
            })
            .collect::<Result<Vec<_>, _>>()
            .map(|values| values.join(",")),
        Value::Object(map) if map.contains_key("toString") => {
            Err("Cannot convert object to primitive value".into())
        }
        Value::Object(_) => Ok("[object Object]".into()),
        _ => Ok(value.to_string()),
    }
}

pub(crate) fn parse(body: &str) -> Result<Value, String> {
    let body = if body.is_empty() { "{}" } else { body };
    serde_json::from_str(body).or_else(|error| {
        if let Some(compatible) = replace_lone_surrogates(body) {
            return serde_json::from_str(&compatible).map_err(|error| parse_error(body, &error));
        }
        Err(parse_error(body, &error))
    })
}

// JSON.parse accepts lone UTF-16 surrogates. URL converts them to U+FFFD, and
// they cannot name a field the renderer reads or contribute to a numeric timeout.
fn replace_lone_surrogates(body: &str) -> Option<String> {
    let bytes = body.as_bytes();
    let mut result = None;
    let mut cursor = 0;
    while let Some(next_escape) = bytes.get(cursor..)?.iter().position(|&byte| byte == b'\\') {
        cursor += next_escape;
        let pair = |offset: usize| -> Option<u16> {
            let escape = bytes.get(offset..offset + 6)?.strip_prefix(b"\\u")?;
            u16::from_str_radix(std::str::from_utf8(escape).ok()?, 16).ok()
        };
        match pair(cursor) {
            Some(0xd800..=0xdbff) if matches!(pair(cursor + 6), Some(0xdc00..=0xdfff)) => {
                cursor += 12
            }
            Some(0xd800..=0xdfff) => {
                result.get_or_insert_with(|| bytes.to_vec())[cursor..cursor + 6]
                    .copy_from_slice(b"\\ufffd");
                cursor += 6;
            }
            _ => cursor = (cursor + 2).min(bytes.len()),
        }
    }
    result.and_then(|bytes| String::from_utf8(bytes).ok())
}

fn parse_error(body: &str, error: &serde_json::Error) -> String {
    let line = error.line();
    let offset = body
        .split_inclusive('\n')
        .take(line.saturating_sub(1))
        .map(str::len)
        .sum::<usize>();
    let column = error.column();
    let at = if error.is_eof() {
        body.len()
    } else {
        offset + column.saturating_sub(1)
    };
    let mut at = at.min(body.len());
    while !body.is_char_boundary(at) {
        at -= 1;
    }
    let before = body[..at].trim_end_matches([' ', '\t', '\n', '\r']);
    let token = body[at..].chars().next();
    let error = error.to_string();
    let message = match error.split(" at line ").next().unwrap_or_default() {
        "key must be a string" | "EOF while parsing an object" if before.ends_with('{') => {
            "Expected property name or '}' in JSON"
        }
        "key must be a string" | "trailing comma" | "EOF while parsing an object"
            if before.ends_with(',') && token != Some(']') =>
        {
            "Expected double-quoted property name in JSON"
        }
        "expected `:`" => "Expected ':' after property name in JSON",
        "expected `,` or `}`" | "EOF while parsing an object" => {
            "Expected ',' or '}' after property value in JSON"
        }
        "expected `,` or `]`" => "Expected ',' or ']' after array element in JSON",
        "trailing characters" => "Unexpected non-whitespace character after JSON",
        "invalid number" if before.ends_with('-') => "No number after minus sign in JSON",
        "invalid number" if before.ends_with('.') => "Unterminated fractional number in JSON",
        "invalid number" if before.ends_with(['e', 'E', '+', '-']) => {
            "Exponent part is missing a number in JSON"
        }
        "invalid number" => "Unexpected number in JSON",
        "invalid escape" => "Bad escaped character in JSON",
        "control character (\\u0000-\\u001F) found while parsing a string" => {
            "Bad control character in string literal in JSON"
        }
        "EOF while parsing a string" => "Unterminated string in JSON",
        _ => return unexpected(body, at, token),
    };
    let position = body[..at].encode_utf16().count();
    let column = body[offset.min(at)..at].encode_utf16().count() + 1;
    format!("{message} at position {position} (line {line} column {column})")
}

fn unexpected(body: &str, at: usize, token: Option<char>) -> String {
    let Some(token) = token else {
        return "Unexpected end of JSON input".into();
    };
    if matches!(body, "undefined" | "NaN" | "Infinity" | "[object Object]") {
        return format!("\"{body}\" is not valid JSON");
    }
    let prefix = format!("Unexpected token '{token}', ");
    let position = body[..at].chars().count();
    let length = body.chars().count();
    if length <= 20 {
        return format!("{prefix}\"{body}\" is not valid JSON");
    }
    let start = position.saturating_sub(10);
    let stop = (position + 10).min(length);
    let context = body
        .chars()
        .skip(start)
        .take(stop - start)
        .collect::<String>();
    let ellipsis_before = if start > 0 { "..." } else { "" };
    let ellipsis_after = if stop < length { "..." } else { "" };
    format!("{prefix}{ellipsis_before}\"{context}\"{ellipsis_after} is not valid JSON")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn checks_normalized_origin_and_path() {
        let allowed = ["https://example.test".into()];
        let valid = json!({"url": ["HTTPS://eXaMpLe.test:443/print/resume/cv?ticket=a"]});
        assert_eq!(
            target(&valid, &allowed)
                .ok()
                .flatten()
                .as_ref()
                .map(Url::as_str),
            Some("https://example.test/print/resume/cv?ticket=a")
        );
        for raw in [
            "https://example.test/print/resume/../else",
            "https://example.test:404/print/resume/cv",
        ] {
            assert!(matches!(target(&json!({"url":raw}), &allowed), Ok(None)));
        }
    }

    #[test]
    fn preserves_timeout_coercion_and_clamping() {
        for (value, expected) in [
            (json!(0), 45000.0),
            (json!("0"), 5000.0),
            (json!("0x4e20"), 20000.0),
            (json!([24000]), 24000.0),
            (json!(99999), 60000.0),
        ] {
            assert_eq!(timeout(&json!({"timeout_ms":value})), Ok(expected));
        }
        assert!(timeout(&json!({"timeout_ms":"invalid"})).is_ok_and(f64::is_nan));
    }

    #[test]
    fn accepts_javascript_strings_with_unpaired_surrogates() {
        let input = r#"{"other":["\ud800","\ud83d\ude00","\\udc00","\udc00"]}"#;
        assert_eq!(parse(input), Ok(json!({"other":["�","😀","\\udc00","�"]})));
    }
}
