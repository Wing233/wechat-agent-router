#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use clap::Parser;
use std::sync::Arc;
use wechat_agent_desktop::{app::DesktopApp, collector::CollectorService, instance, paths, server, tech_stack::TechStackService};

#[derive(Parser, Debug)]
#[command(name = "WeChatAgentDesktop", about = "微信 Agent 桌面控制台（Rust/Tauri 版）")]
struct Args {
    #[arg(long)]
    config: Option<String>,
    #[arg(long)]
    headless: bool,
    #[arg(long, default_value = "auto")]
    window: String,
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let state_path = paths::state_file();
    let existing = instance::read_state(&state_path);
    if instance::is_alive(&existing) {
        let url = instance::existing_url(&existing).unwrap_or_default();
        if args.headless {
            println!("桌面控制台已在后台运行");
            return Ok(());
        }
        #[cfg(windows)]
        {
            run_tauri(url)?;
        }
        #[cfg(not(windows))]
        {
            println!("桌面控制台已在后台运行：{url}");
            let _ = open::that(url);
        }
        return Ok(());
    }
    if !existing.is_null() && !existing.as_object().map(|o| o.is_empty()).unwrap_or(true) {
        instance::clear_state(&state_path, None);
    }
    let app = Arc::new(DesktopApp::new(args.config)?);
    let log = app.log.clone();
    let collector = Arc::new(CollectorService::new(paths::collector_dir(), log.clone()));
    let tech_stack = Arc::new(TechStackService::new(paths::tech_stack_dir()));
    let api = server::start(app.clone(), collector.clone(), tech_stack.clone())?;
    log.add("INFO", &format!("桌面控制台已启动：{}", api.url));
    let _ = instance::write_state(&state_path, api.port, &app.token, &api.ui_url, args.headless);
    if args.headless {
        println!("微信 Agent 控制台后台运行中：{}", api.ui_url);
        loop {
            std::thread::park();
        }
    }
    #[cfg(windows)]
    {
        run_tauri(api.ui_url)?;
    }
    #[cfg(not(windows))]
    {
        println!("打开界面：{}", api.ui_url);
        loop {
            std::thread::park();
        }
    }
    Ok(())
}

#[cfg(windows)]
fn run_tauri(url: String) -> anyhow::Result<()> {
    let builder = tauri::Builder::default()
        .setup(move |app| {
            let _window = tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::External(url.parse().unwrap()))
                .title("微信 Agent 控制台")
                .inner_size(1320.0, 900.0)
                .min_inner_size(1080.0, 700.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!());
    match builder {
        Ok(()) => Ok(()),
        Err(error) => Err(anyhow::anyhow!("Tauri 启动失败：{error}")),
    }
}
