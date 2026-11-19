use std::{
    env,
    io::{self, Write},
    process, thread,
    time::Duration,
};

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if !args.ends_with(&["--output".into(), "json".into()]) {
        process::exit(9);
    }
    match args.get(2).map(String::as_str) {
        Some("fixture-warning") => {
            println!("[TokenStorage] warning\n[1]\n{{\"z\": [1, \"晴天🎵\"], \"a\": 2}}")
        }
        Some("fixture-bom") => println!("\u{feff}\u{feff}{{\"code\":0}}"),
        Some("fixture-tie") => println!("notice {{\"a\":1}} {{\"b\":2}}"),
        Some("fixture-array") => println!("[false, 0, \"text\"]"),
        Some("fixture-string") => println!("\"晴天\""),
        Some("fixture-false") => println!("false"),
        Some("fixture-float") => println!("[1e0,1e-7,1e20,-0.0,1.23456789012345,1e15,0.0001]"),
        Some("fixture-nonfinite") => {
            println!("notice [NaN,Infinity,-Infinity,1e400,\"NaN\",\"\\u0000ncm-json:NaN\"]")
        }
        Some("fixture-bigint") => println!("{{\"large\":123456789012345678901234567890}}"),
        Some("fixture-duplicate") => println!("{{\"b\":1,\"a\":2,\"b\":3}}"),
        Some("fixture-null") => println!("null"),
        Some("fixture-invalid") => print!("{}", "错".repeat(350)),
        Some("fixture-invalid-utf8") => {
            io::stdout().write_all(b"{\"raw\":\"\xff\"}").unwrap();
        }
        Some("fixture-empty") => {}
        Some("fixture-config") => fail("APIKey 未设置"),
        Some("fixture-login") => fail("LOGIN REQUIRED"),
        Some("fixture-quota") => fail("rate limit"),
        Some("fixture-missing") => fail("HTTP 404"),
        Some("fixture-unknown") => fail(&"错".repeat(550)),
        Some("fixture-empty-error") => process::exit(1),
        Some("fixture-stdout-error") => {
            println!("quota");
            process::exit(1);
        }
        Some("fixture-stderr-first") => {
            println!("quota");
            fail("custom\rproblem");
        }
        Some("fixture-api") => println!("{{\"error\": {{\"code\": 401, \"message\": \"测试\"}}}}"),
        Some("fixture-api-default") => println!("{{\"error\": {{\"other\": 1}}}}"),
        Some("fixture-api-list") => println!("{{\"error\": [true, null, \"x\"]}}"),
        Some("fixture-bridge-code") => {
            println!("{{\"error\": {{\"code\": \"bridge_error\", \"message\": \"from CLI\"}}}}")
        }
        Some("fixture-success-false") => println!("{{\"success\":false}}"),
        Some("fixture-success-zero") => println!("{{\"success\":0,\"code\":\"junk\"}}"),
        Some("fixture-code") => println!("{{\"code\":\"４０４\",\"message\":\"没找到\"}}"),
        Some("fixture-code-float") => println!("{{\"code\":404.5}}"),
        Some("fixture-code-nan") => println!("{{\"code\":NaN}}"),
        Some("fixture-code-infinity") => println!("{{\"code\":Infinity}}"),
        Some("fixture-full-pipes") => {
            eprintln!("{}", "e".repeat(100_000));
            println!("{}\n{{\"code\":200}}", "s".repeat(100_000));
        }
        Some("fixture-slow") => thread::sleep(Duration::from_secs(5)),
        _ => {
            let mut escaped = Vec::new();
            for arg in args {
                let arg = arg
                    .replace('\\', "\\\\")
                    .replace('"', "\\\"")
                    .replace('\t', "\\t");
                escaped.push(format!("\"{arg}\""));
            }
            println!(
                "{{\"argv\":[{}],\"no_color\":\"{}\"}}",
                escaped.join(","),
                env::var("NO_COLOR").unwrap_or_default()
            );
        }
    }
}

fn fail(message: &str) -> ! {
    eprintln!("{message}");
    process::exit(1);
}
