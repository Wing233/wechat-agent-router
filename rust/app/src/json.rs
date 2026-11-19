use serde_json::{Number, Value};

pub fn decode(text: &str) -> Option<Value> {
    if let Ok(value) = serde_json::from_str(text) {
        return Some(value);
    }
    if !text.contains("NaN") && !text.contains("Infinity") {
        return None;
    }
    // Use markers that cannot coincide with any decoded string in the input.
    // serde_json still validates the entire structure, including keys and delimiters.
    let bytes = text.as_bytes();
    let mut index = 0;
    let mut replacements = Vec::new();
    let mut strings = Vec::new();
    while index < bytes.len() {
        if bytes[index] == b'"' {
            let start = index;
            index += 1;
            while index < bytes.len() {
                match bytes[index] {
                    b'\\' => index += 2,
                    b'"' => break,
                    _ => index += 1,
                }
            }
            let literal = text.get(start..=index)?;
            strings.push(serde_json::from_str::<String>(literal).ok()?);
            index += 1;
            continue;
        }
        let before = index.checked_sub(1).map(|pos| bytes[pos]);
        let valid_before = before
            .is_none_or(|byte| matches!(byte, b' ' | b'\t' | b'\r' | b'\n' | b':' | b'[' | b','));
        if valid_before {
            if let Some(token) = ["NaN", "Infinity", "-Infinity"]
                .into_iter()
                .find(|token| bytes[index..].starts_with(token.as_bytes()))
            {
                let end = index + token.len();
                let after = bytes[end..]
                    .iter()
                    .copied()
                    .find(|b| !matches!(b, b' ' | b'\t' | b'\r' | b'\n'));
                if after.is_none_or(|byte| matches!(byte, b',' | b']' | b'}')) {
                    replacements.push((index, end, token));
                    index = end;
                    continue;
                }
            }
        }
        index += 1;
    }
    if replacements.is_empty() {
        return None;
    }
    let mut marker = "\0ncm-json:".to_owned();
    while strings.iter().any(|value| value.starts_with(&marker)) {
        marker.push(':');
    }
    let mut rewritten = String::with_capacity(text.len());
    let mut cursor = 0;
    for (start, end, token) in replacements {
        rewritten.push_str(&text[cursor..start]);
        rewritten.push_str(&Value::String(format!("{marker}{token}")).to_string());
        cursor = end;
    }
    rewritten.push_str(&text[cursor..]);
    let mut result = serde_json::from_str(&rewritten).ok()?;
    restore_constants(&mut result, &marker);
    Some(result)
}

fn restore_constants(value: &mut Value, marker: &str) {
    match value {
        Value::String(text) => {
            if let Some(literal) = text.strip_prefix(marker) {
                // serde_json's Number has no public non-finite constructor. This is
                // consumed only by the bridge's Python-compatible value formatter.
                *value = Value::Number(Number::from_string_unchecked(literal.into()));
            }
        }
        Value::Object(object) => object
            .values_mut()
            .for_each(|value| restore_constants(value, marker)),
        Value::Array(array) => array
            .iter_mut()
            .for_each(|value| restore_constants(value, marker)),
        _ => {}
    }
}

pub fn encode(value: &Value) -> String {
    let mut result = String::new();
    write(value, &mut result);
    result
}

fn write(value: &Value, result: &mut String) {
    match value {
        Value::Object(object) => {
            result.push('{');
            for (index, (key, value)) in object.iter().enumerate() {
                if index > 0 {
                    result.push_str(", ");
                }
                result.push_str(&Value::String(key.clone()).to_string());
                result.push_str(": ");
                write(value, result);
            }
            result.push('}');
        }
        Value::Array(items) => {
            result.push('[');
            for (index, value) in items.iter().enumerate() {
                if index > 0 {
                    result.push_str(", ");
                }
                write(value, result);
            }
            result.push(']');
        }
        Value::Number(number) => result.push_str(&number_text(number, true)),
        other => result.push_str(&other.to_string()),
    }
}

pub fn repr(value: &Value, quote_strings: bool) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(number) => number_text(number, false),
        Value::String(text) if !quote_strings => text.clone(),
        Value::String(text) => repr_string(text),
        Value::Array(items) => {
            let items: Vec<_> = items.iter().map(|item| repr(item, true)).collect();
            format!("[{}]", items.join(", "))
        }
        Value::Object(object) => {
            let items: Vec<_> = object
                .iter()
                .map(|(key, value)| format!("{}: {}", repr_string(key), repr(value, true)))
                .collect();
            format!("{{{}}}", items.join(", "))
        }
    }
}

pub fn repr_string(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::from(quote);
    for ch in text.chars() {
        match ch {
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            ch if ch == quote || ch == '\\' => {
                result.push('\\');
                result.push(ch);
            }
            ch if ch.is_control() || (ch != ' ' && ch.is_whitespace()) => {
                if ch as u32 <= 0xff {
                    result.push_str(&format!("\\x{:02x}", ch as u32));
                } else {
                    result.push_str(&format!("\\u{:04x}", ch as u32));
                }
            }
            ch => result.push(ch),
        }
    }
    result.push(quote);
    result
}

fn number_text(number: &Number, wire: bool) -> String {
    let raw = number.as_str();
    match raw {
        "NaN" => return if wire { "NaN" } else { "nan" }.into(),
        "Infinity" => return if wire { "Infinity" } else { "inf" }.into(),
        "-Infinity" => return if wire { "-Infinity" } else { "-inf" }.into(),
        _ => {}
    }
    if !raw.contains(['.', 'e', 'E']) {
        return if raw == "-0" { "0" } else { raw }.into();
    }
    let Ok(float) = raw.parse::<f64>() else {
        return raw.into();
    };
    if float.is_infinite() {
        return match (wire, float.is_sign_negative()) {
            (true, false) => "Infinity",
            (true, true) => "-Infinity",
            (false, false) => "inf",
            (false, true) => "-inf",
        }
        .into();
    }
    let text = format!("{float:?}");
    if let Some((mantissa, exponent)) = text.split_once('e') {
        if let Ok(exponent) = exponent.parse::<i32>() {
            return format!("{mantissa}e{exponent:+03}");
        }
    }
    text
}

pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(object) => !object.is_empty(),
        Value::Number(number) => number.as_str().parse::<f64>() != Ok(0.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn emits_python_json_format() {
        let input = r#"{"z":[1e0,1e-7,1e20,-0,-0.0,123456789012345678901234567890],"a":"晴天🎵"}"#;
        let expected =
            r#"{"z": [1.0, 1e-07, 1e+20, 0, -0.0, 123456789012345678901234567890], "a": "晴天🎵"}"#;
        assert_eq!(encode(&decode(input).unwrap()), expected);
    }

    #[test]
    fn accepts_python_constants_only_in_value_positions() {
        let input = r#"{"value":[NaN,Infinity,-Infinity,"\u0000ncm-json:NaN"]}"#;
        let expected = r#"{"value": [NaN, Infinity, -Infinity, "\u0000ncm-json:NaN"]}"#;
        assert_eq!(encode(&decode(input).unwrap()), expected);
        assert!(decode("{NaN:0}").is_none());
        assert!(decode("[NaN1]").is_none());
    }
}
