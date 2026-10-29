$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
$Venv = Join-Path $Here ".venv-build"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "Creating build virtualenv..."
    python -m venv $Venv
}

Write-Host "Installing build dependencies..."
& $Python -m pip install --disable-pip-version-check --no-input -r (Join-Path $Here "requirements.txt")

Push-Location $Root
try {
    Write-Host "Building WeChatAgentDesktop.exe ..."
    $WebData = Join-Path $Here "web"
    $AgentPath = Join-Path $Root "local-agent"
    $Entry = Join-Path $Here "main.py"
    & (Join-Path $Venv "Scripts\pyinstaller.exe") `
        --noconfirm --clean --onefile --windowed `
        --name "WeChatAgentDesktop" `
        --distpath "desktop\dist" `
        --workpath "desktop\build" `
        --specpath "desktop" `
        --add-data "$WebData;desktop\web" `
        --paths "$AgentPath" `
        --collect-all webview `
        --collect-all pythonnet `
        --collect-all clr_loader `
        "$Entry"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    Write-Host ""
    Write-Host "Done: $Here\dist\WeChatAgentDesktop.exe"
    Write-Host "First run creates config.json next to the exe."
} finally {
    Pop-Location
}
exit 0
