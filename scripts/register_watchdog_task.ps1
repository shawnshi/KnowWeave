<#
Register the Vector Lake watchdog as an independent scheduled task.

Independence: the task is owned by the Task Scheduler service, not by any shell, session or pi
session that happens to be open. Its two triggers give both boot persistence and self-heal:
  * at logon / at startup -> the family comes back after a reboot
  * every 5 minutes, MultipleInstances=IgnoreNew -> a crashed or hard-killed watchdog is
    relaunched, and a healthy one is never duplicated (the running instance blocks the repeat)
ExecutionTimeLimit=PT0S (unlimited) because a daemon that is stopped after 72h is not a daemon.

Reversal: Unregister-ScheduledTask -TaskName 'VectorLake-Watchdog' -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'VectorLake-Watchdog',
    # Defaults are derived from this file's own location so the script carries no machine paths.
    [string]$Script = '',
    [string]$WorkDir = ''
)

$ErrorActionPreference = 'Stop'

if (-not $Script) { $Script = Join-Path $PSScriptRoot 'watchdog_service.ps1' }
if (-not $WorkDir) { $WorkDir = Split-Path -Parent $PSScriptRoot }

if (-not (Test-Path $Script -PathType Leaf)) { throw "wrapper not found: $Script" }
if (-not (Test-Path $WorkDir -PathType Container)) { throw "working directory not found: $WorkDir" }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "task already exists; updating it after validation (State=$($existing.State))"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $Script) `
    -WorkingDirectory $WorkDir

$atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$atStartup = New-ScheduledTaskTrigger -AtStartup
# Repetition is what turns a crash into a bounded restart. [TimeSpan]::MaxValue is rejected by
# the task XML (out of range), so use a decade-long window and never stop at its end.
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$repeat.Repetition.StopAtDurationEnd = $false

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RunOnlyIfIdle:$false `
    -DontStopOnIdleEnd

$description = 'Vector Lake watchdog daemon (outbox, incremental index, scheduled lint, gram rebuild, WAL checkpoint, ingest-runner supervision). Self-heals via a 5-minute repetition trigger; one instance only.'

$principal = $null
foreach ($logonType in 'S4U', 'Interactive') {
    try {
        $candidate = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $logonType -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $atLogon, $atStartup, $repeat `
            -Settings $settings -Principal $candidate -Description $description -Force | Out-Null
        $principal = $candidate
        Write-Host "registered with LogonType=$logonType"
        break
    }
    catch {
        Write-Host "LogonType=$logonType rejected: $($_.Exception.Message)"
    }
}
if (-not $principal) { throw 'registration failed for every logon type' }

$task = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{
    TaskName  = $task.TaskName
    State     = $task.State
    LogonType = $task.Principal.LogonType
    RunLevel  = $task.Principal.RunLevel
    Triggers  = ($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', '
    Settings  = "RunOnlyIfIdle=$($task.Settings.RunOnlyIfIdle) StopOnIdleEnd=$($task.Settings.StopOnIdleEnd) MultipleInstances=$($task.Settings.MultipleInstances) ExecTimeLimit=$($task.Settings.ExecutionTimeLimit)"
} | Format-List

Write-Host '--- exported XML ---'
Export-ScheduledTask -TaskName $TaskName
