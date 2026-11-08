use std::path::PathBuf;

pub fn shortcut_path() -> PathBuf {
    let appdata = std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("AppData/Roaming"));
    appdata.join("Microsoft/Windows/Start Menu/Programs/Startup/WeChatAgentDesktop.lnk")
}

pub fn is_enabled() -> bool {
    shortcut_path().is_file()
}

pub fn set_enabled(enabled: bool) -> bool {
    if !enabled {
        let _ = std::fs::remove_file(shortcut_path());
        return false;
    }
    // Windows shortcut creation still uses PowerShell COM, same as the Python版.
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("WeChatAgentDesktop.exe"));
    let workdir = exe.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from("."));
    let path = shortcut_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let target = exe.to_string_lossy().replace('\'', "''");
    let args = "";
    let working = workdir.to_string_lossy().replace('\'', "''");
    let script = format!(
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}');$s.TargetPath='{target}';$s.Arguments='{args}';$s.WorkingDirectory='{working}';$s.WindowStyle=1;$s.Description='微信 Agent 桌面控制台（开机启动）';$s.Save()",
        path = path.to_string_lossy(),
        target = target,
        working = working
    );
    let _ = std::process::Command::new("powershell")
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", &script])
        .output();
    is_enabled()
}
