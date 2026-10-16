<#
  Recruit mail notifier - autostart (Task Scheduler) management

  Usage (no admin rights needed):
    powershell -File tools\install_autostart.ps1            register + enable
    powershell -File tools\install_autostart.ps1 -Status    show status
    powershell -File tools\install_autostart.ps1 -Disable   disable (keeps config)
    powershell -File tools\install_autostart.ps1 -Enable    re-enable
    powershell -File tools\install_autostart.ps1 -Remove    delete the task

  The task runs start_daemon.cmd at logon. That script runs
  "main.py run" once and exits -- it is NOT a daemon.

  NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1
  files with the system ANSI codepage when there is no BOM, so any
  non-ASCII text here would be garbled.
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$Disable,
    [switch]$Enable,
    [switch]$Status,
    [string]$TaskName = "RecruitMailNotifier",
    [int]$DelayMinutes = 10
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root "start_daemon.cmd"
$userId = "$env:USERDOMAIN\$env:USERNAME"

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "Not registered: $TaskName"; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task name : $TaskName"
    Write-Host "State     : $($task.State)"
    Write-Host "Enabled   : $($task.Settings.Enabled)"
    Write-Host "Last run  : $($info.LastRunTime)   result=$($info.LastTaskResult)"
    Write-Host "Delay     : $($task.Triggers[0].Delay)"
    Write-Host "Launcher  : $($task.Actions[0].Execute)"
    exit 0
}

if ($Disable) {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "Not registered: $TaskName"; exit 0
    }
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Disabled: $TaskName (config kept, use -Enable to turn it back on)"
    exit 0
}

if ($Enable) {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "Not registered yet; run without arguments first"; exit 1
    }
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Enabled: $TaskName"
    exit 0
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed: $TaskName"
    } else {
        Write-Host "Not registered: $TaskName"
    }
    exit 0
}

if (-not (Test-Path $launcher)) { throw "Launcher not found: $launcher" }

# NOTE: do NOT set -RestartCount. Task Scheduler requires it to be >= 1,
# and 0 makes Register-ScheduledTask fail with a task-XML error.
# The launcher always ends with "exit /b 0", so restarts never trigger
# anyway; retries are pointless and would only re-poll the mailbox.
$action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
# Logon trigger delay, ISO-8601 duration: PT10M = 10 minutes.
# Gives the network time to come up after logon.
if ($DelayMinutes -gt 0) { $trigger.Delay = "PT${DelayMinutes}M" }
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered and enabled: $TaskName"
Write-Host "  Launcher : $launcher"
Write-Host "  Trigger  : at logon"
Write-Host "  Instances: ignore new (never runs twice)"
Write-Host ""
Write-Host "Run it now: Start-ScheduledTask -TaskName $TaskName"