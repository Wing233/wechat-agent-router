$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $Here
try {
    Write-Host "Building Tauri NSIS package..."
    npx --yes @tauri-apps/cli@2 build
    if ($LASTEXITCODE -ne 0) { throw "Tauri build failed with exit code $LASTEXITCODE" }
    Write-Host "Done: $Here\target\release\bundle\nsis"
} finally {
    Pop-Location
}
