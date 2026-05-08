<#
.SYNOPSIS
    Register a Windows Scheduled Task that triggers polymarket_weather.cli.run_loop
    every 15 minutes (paper mode by default).

.DESCRIPTION
    Reads the repo root from the script's location, builds the absolute path
    to .venv\Scripts\python.exe and a one-shot run_loop invocation, then
    registers / updates a scheduled task with that command.

    The task is intentionally a *one-shot per trigger* (no --loop). Windows
    Task Scheduler is the supervising loop; the orchestrator is idempotent so
    overlap is safe.

    The default mode is "paper" so registration alone is risk-free. To go live
    later, the user (not this script) must:
      1. set WEATHER_AUTOMATION_ENABLED=1 in the user's environment, AND
      2. re-register with -Mode live.

    The script *does not* set or modify any environment variable; it only
    reads them so the registered command can pick them up at runtime.

.PARAMETER TaskName
    Name of the scheduled task. Default: PolymarketWeatherOrchestrator.

.PARAMETER Mode
    paper (default) or live. live is honored only if WEATHER_AUTOMATION_ENABLED=1
    at run time inside the orchestrator (see automation/order_manager.py).

.PARAMETER IntervalMinutes
    How often to run the orchestrator. Default 15.

.PARAMETER DaysAhead
    --days-ahead value. Default 7.

.PARAMETER Bankroll
    --bankroll value passed to run_loop. Default 500.

.PARAMETER PerBucketCap
    --per-bucket-cap value. Default 5.

.PARAMETER PerEventCap
    --per-event-cap value. Default 20.

.PARAMETER PerDayCap
    --per-day-cap value. Default 100.

.PARAMETER DryRun
    Print the resulting command and skip Register-ScheduledTask.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_run_loop_task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_run_loop_task.ps1 -DryRun

.NOTES
    Requires that the venv at .venv\Scripts\python.exe is functional.
    Requires admin privileges if you want -RunLevel Highest; this script
    uses the current user with default rights so it works without admin.
#>

[CmdletBinding()]
param(
    [string]$TaskName = "PolymarketWeatherOrchestrator",
    [ValidateSet("paper", "live")]
    [string]$Mode = "paper",
    [int]$IntervalMinutes = 15,
    [int]$DaysAhead = 7,
    [double]$Bankroll = 500,
    [double]$PerBucketCap = 5,
    [double]$PerEventCap = 20,
    [double]$PerDayCap = 100,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $repoRoot "logs"

if (!(Test-Path $python)) {
    throw "Could not find venv python at $python. Activate the venv at least once or run 'pip install -e .'."
}
if (!(Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

$argList = @(
    "-m", "polymarket_weather.cli.run_loop",
    "--mode", $Mode,
    "--days-ahead", $DaysAhead,
    "--bankroll", $Bankroll,
    "--per-bucket-cap", $PerBucketCap,
    "--per-event-cap", $PerEventCap,
    "--per-day-cap", $PerDayCap
)

$argLine = ($argList | ForEach-Object { '"{0}"' -f $_ }) -join ' '

Write-Host "Repo root  : $repoRoot"
Write-Host "Python     : $python"
Write-Host "Log dir    : $logDir"
Write-Host "Mode       : $Mode"
Write-Host "Interval   : every $IntervalMinutes minutes"
Write-Host "Args       : $argLine"

if ($Mode -eq "live") {
    Write-Warning "Mode=live: orchestrator will only place real orders if WEATHER_AUTOMATION_ENABLED=1 at run time. The order manager keeps its own kill switch and notional caps. Review the run before enabling."
}

if ($DryRun) {
    Write-Host "Dry run only; not registering."
    exit 0
}

$action = New-ScheduledTaskAction -Execute $python -Argument $argLine -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 12) `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -RestartCount 0
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Updating existing task $TaskName ..."
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
} else {
    Write-Host "Registering new task $TaskName ..."
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Polymarket weather orchestrator (paper / live, mode=$Mode)" | Out-Null
}

Write-Host "Done. Inspect with: Get-ScheduledTask -TaskName $TaskName | Select-Object *"
Write-Host "Run once manually with: Start-ScheduledTask -TaskName $TaskName"
Write-Host "Tail logs with        : Get-Content -Wait $logDir\orch_$(Get-Date -Format yyyy-MM-dd).jsonl"
