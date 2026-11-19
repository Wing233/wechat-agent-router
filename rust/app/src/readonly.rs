use serde_json::Value;

pub const COMMANDS: &[(&str, &str)] = &[
    ("search", "song"),
    ("search", "album"),
    ("search", "playlist"),
    ("search", "mv"),
    ("search", "podcast"),
    ("search", "voice"),
    ("search", "all"),
    ("user", "info"),
    ("user", "favorite"),
    ("user", "history"),
    ("user", "listen-ranking"),
    ("user", "album-history"),
    ("user", "playlist-history"),
    ("user", "radio-history"),
    ("user", "voice-history"),
    ("user", "voicebook-history"),
    ("playlist", "created"),
    ("playlist", "collected"),
    ("playlist", "get"),
    ("playlist", "tracks"),
    ("playlist", "getTags"),
    ("playlist", "radar"),
    ("album", "collected"),
    ("album", "get"),
    ("album", "tracks"),
    ("artist", "songs"),
    ("recommend", "daily"),
    ("recommend", "fm"),
    ("recommend", "heartbeat"),
    ("song", "lyric"),
    ("comment", "list-hot"),
    ("note", "topicSearch"),
    ("podcast", "categories"),
    ("podcast", "created"),
    ("podcast", "get"),
    ("podcast", "voices"),
    ("podcast", "voiceDetail"),
    ("cloud", "list"),
    ("cloud", "listFile"),
    ("aisong", "pointBalance"),
    ("aisong", "list"),
    ("aisong", "detail"),
    ("aisong", "batchDetail"),
];

pub fn validate_args(args: Option<&Value>) -> Result<Vec<&str>, String> {
    let args = args
        .and_then(Value::as_array)
        .filter(|args| args.len() >= 2)
        .ok_or("命令参数必须是至少包含命令和子命令的数组")?;
    if args.len() > 24 {
        return Err("命令参数过多，最多 24 个".into());
    }

    let mut clean = Vec::with_capacity(args.len());
    for raw in args {
        let token = trim(raw.as_str().ok_or("命令参数必须是字符串")?);
        if token.is_empty() {
            return Err("命令参数不能为空".into());
        }
        if token.chars().count() > 200 {
            return Err("单个参数不能超过 200 个字符".into());
        }
        if token.contains(['\0', '\r', '\n']) {
            return Err("命令参数不能包含控制字符".into());
        }
        if matches!(token, "--output" | "--help" | "-h" | "--userInput") {
            return Err(format!("不允许参数 {token}"));
        }
        clean.push(token);
    }
    if !COMMANDS.contains(&(clean[0], clean[1])) {
        return Err(format!(
            "只允许只读音乐命令，收到：{} {}",
            clean[0], clean[1]
        ));
    }
    for pair in clean.windows(2) {
        let (min, max) = match pair[0] {
            "--limit" => (1, 200),
            "--offset" => (0, 1_000_000),
            _ => continue,
        };
        let value = integer(pair[1]).ok_or_else(|| format!("{} 必须是整数", pair[0]))?;
        if !value.parse::<i64>().is_ok_and(|n| (min..=max).contains(&n)) {
            return Err(format!("{} 必须在 {min}..{max} 之间", pair[0]));
        }
    }
    Ok(clean)
}

pub fn whitespace(ch: char) -> bool {
    ch.is_whitespace() || ('\u{001c}'..='\u{001f}').contains(&ch)
}

pub fn trim(text: &str) -> &str {
    text.trim_matches(whitespace)
}

pub fn integer(text: &str) -> Option<String> {
    // CPython's int accepts Unicode decimal digits and separators between digits.
    // Its ASCII whitespace rules exclude U+001C through U+001F.
    let text = text.trim_matches(char::is_whitespace);
    let negative = text.starts_with('-');
    let mut chars = text
        .strip_prefix(['-', '+'])
        .unwrap_or(text)
        .chars()
        .peekable();
    let mut result = String::new();
    let mut count = 0;
    while let Some(ch) = chars.next() {
        let digit = decimal(ch)?;
        count += 1;
        if digit != '0' || !result.is_empty() {
            result.push(digit);
        }
        if chars.peek() == Some(&'_') {
            chars.next();
            chars.peek()?;
        }
    }
    if count == 0 || count > 4300 {
        return None;
    }
    if result.is_empty() {
        result.push('0');
    } else if negative {
        result.insert(0, '-');
    }
    Some(result)
}

fn decimal(ch: char) -> Option<char> {
    if ch.is_ascii_digit() {
        return Some(ch);
    }
    // Zero code points of the Unicode 15.1 decimal digit blocks used by Python 3.13.
    const ZEROS: &[u32] = &[
        0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6,
        0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60,
        0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0,
        0x1e950, 0x1fbf0,
    ];
    let ch = ch as u32;
    let index = ZEROS.partition_point(|&zero| zero <= ch).checked_sub(1)?;
    char::from_digit(ch - ZEROS[index], 10)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn interprets_python_decimal_spellings() {
        assert_eq!(integer(" +２_٠_０ ").as_deref(), Some("200"));
        assert_eq!(integer("-٠").as_deref(), Some("0"));
        assert_eq!(integer("1__2"), None);
        assert_eq!(integer("²"), None);
    }

    #[test]
    fn keeps_allowlist_and_validation_order() {
        let args = json!(["\u{001f}search", "song\u{3000}", "--limit", "２_０_０"]);
        assert_eq!(
            validate_args(Some(&args)).unwrap(),
            ["search", "song", "--limit", "２_０_０"]
        );
        let args = json!(["playlist", "create", "--output"]);
        assert_eq!(
            validate_args(Some(&args)).unwrap_err(),
            "不允许参数 --output"
        );
    }
}
