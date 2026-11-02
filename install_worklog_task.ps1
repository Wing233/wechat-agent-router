# Register a current-user scheduled task for the LYPH worklog collector.
# No administrator rights are required. The task runs at logon and at 22:50;
# MultipleInstances=IgnoreNew keeps a single long-running collector.
$ErrorActionPreference = "Stop"

$pythonw = "C:\Program Files\Python313\pythonw.exe"
$script = "D:\xx\wechatbot\agent-router\worklog_collector.py"
$config = Join-Path $env:LOCALAPPDATA "WeChatAgentWorklog\config.json"
$taskName = "WeChatAgentWorklog"

if (!(Test-Path $pythonw)) { throw "pythonw.exe not found: $pythonw" }
if (!(Test-Path $script)) { throw "collector not found: $script" }
if (!(Test-Path $config)) { throw "config not found: $config" }

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$script`" --config `"$config`"" `
    -WorkingDirectory (Split-Path $script)

$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$daily = New-ScheduledTaskTrigger -Daily -At "03:55"

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($logon, $daily) `
    -Principal $principal `
    -Settings $settings `
    -Description "Collect Windows work activity and upload daily notes to LYPH" `
    -Force | Out-Null

Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
