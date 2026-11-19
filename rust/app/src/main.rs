#![forbid(unsafe_code)]

mod client;
mod http;
mod json;
mod readonly;

use std::{
    env, io,
    net::{TcpListener, ToSocketAddrs},
    process::ExitCode,
    sync::Arc,
    thread,
};

const USAGE: &str = "usage: ncm_bridge.py [-h] [--host HOST] [--port PORT] [--token TOKEN]\n                     [--bin BIN] [--timeout TIMEOUT]";
const HELP: &str = "Read-only ncm-cli HTTP bridge\n\noptions:\n  -h, --help         show this help message and exit\n  --host HOST\n  --port PORT\n  --token TOKEN\n  --bin BIN\n  --timeout TIMEOUT";
const OPTIONS: [&str; 6] = [
    "--help",
    "--host",
    "--port",
    "--token",
    "--bin",
    "--timeout",
];

struct Config {
    host: String,
    port: String,
    token: String,
    binary: String,
    timeout: u64,
}

fn parse(args: Vec<String>) -> Result<Option<Config>, String> {
    let mut config = Config {
        host: "127.0.0.1".into(),
        port: "18888".into(),
        token: env::var("NCM_BRIDGE_TOKEN").unwrap_or_default(),
        binary: env::var("NCM_CLI_BIN").unwrap_or_else(|_| "ncm-cli".into()),
        timeout: 30,
    };
    let mut rest = args.iter().map(String::as_str).peekable();
    let mut unknown = Vec::new();
    while let Some(argument) = rest.next() {
        if argument == "--" {
            unknown.push(argument);
            unknown.extend(rest);
            break;
        }
        let (name, inline) = argument
            .split_once('=')
            .map_or((argument, None), |(name, value)| (name, Some(value)));
        let option = if name == "-h" {
            "--help"
        } else if OPTIONS.contains(&name) {
            name
        } else if name.starts_with("--") {
            let matches: Vec<_> = OPTIONS
                .iter()
                .copied()
                .filter(|option| option.starts_with(name))
                .collect();
            match matches.as_slice() {
                [option] => *option,
                [] => {
                    unknown.push(argument);
                    continue;
                }
                _ => {
                    return Err(format!(
                        "ambiguous option: {argument} could match {}",
                        matches.join(", ")
                    ))
                }
            }
        } else {
            unknown.push(argument);
            continue;
        };
        if option == "--help" {
            if let Some(value) = inline {
                return Err(format!(
                    "argument -h/--help: ignored explicit argument {}",
                    json::repr_string(value)
                ));
            }
            return Ok(None);
        }
        let acceptable =
            |value: &str| !value.starts_with('-') || value == "-" || negative_literal(value);
        let value = inline
            .or_else(|| rest.next_if(|value| acceptable(value)))
            .ok_or_else(|| format!("argument {option}: expected one argument"))?;
        match option {
            "--host" => config.host = value.into(),
            "--token" => config.token = value.into(),
            "--bin" => config.binary = value.into(),
            "--port" | "--timeout" => {
                let number = readonly::integer(value).ok_or_else(|| {
                    format!(
                        "argument {option}: invalid int value: {}",
                        json::repr_string(value)
                    )
                })?;
                if option == "--port" {
                    config.port = number;
                } else if number.starts_with('-') {
                    config.timeout = 1;
                } else {
                    config.timeout = number.parse::<u64>().unwrap_or(u64::MAX).max(1);
                }
            }
            _ => {}
        }
    }
    if !unknown.is_empty() {
        return Err(format!("unrecognized arguments: {}", unknown.join(" ")));
    }
    Ok(Some(config))
}

fn negative_literal(value: &str) -> bool {
    let Some(number) = value.strip_prefix('-') else {
        return false;
    };
    let digit = |ch: char| ch.is_ascii_digit();
    if let Some((left, right)) = number.split_once('.') {
        !right.is_empty() && left.chars().all(digit) && right.chars().all(digit)
    } else {
        !number.is_empty() && number.chars().all(digit)
    }
}

fn listen(config: Config) -> io::Result<()> {
    if !matches!(config.host.as_str(), "127.0.0.1" | "::1" | "localhost") {
        eprintln!(
            "[ncm-bridge] listening on {}; keep the port behind a firewall.",
            config.host
        );
    }
    let port = config
        .port
        .parse::<u16>()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "bind(): port must be 0-65535"))?;
    let host = if config.host.is_empty() {
        "0.0.0.0"
    } else {
        &config.host
    };
    let address = (host, port)
        .to_socket_addrs()?
        .find(std::net::SocketAddr::is_ipv4)
        .ok_or_else(|| io::Error::new(io::ErrorKind::AddrNotAvailable, "no IPv4 address"))?;
    let listener = TcpListener::bind(address)?;
    ctrlc::set_handler(|| std::process::exit(0)).map_err(io::Error::other)?;
    eprintln!(
        "[ncm-bridge] read-only bridge on http://{}:{}",
        config.host, config.port
    );
    let config = Arc::new(config);
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let config = Arc::clone(&config);
                thread::spawn(move || {
                    let _ = http::serve(stream, &config);
                });
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    match parse(env::args().skip(1).collect()) {
        Ok(None) => {
            println!("{USAGE}\n\n{HELP}");
            ExitCode::SUCCESS
        }
        Ok(Some(config)) if config.token.is_empty() => {
            eprintln!("NCM_BRIDGE_TOKEN (or --token) is required; refusing to expose an unauthenticated bridge.");
            ExitCode::from(2)
        }
        Ok(Some(config)) => match listen(config) {
            Ok(()) => ExitCode::SUCCESS,
            Err(err) => {
                eprintln!("[ncm-bridge] {err}");
                ExitCode::FAILURE
            }
        },
        Err(err) => {
            eprintln!("{USAGE}\nncm_bridge.py: error: {err}");
            ExitCode::from(2)
        }
    }
}
