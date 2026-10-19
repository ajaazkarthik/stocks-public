# start_scheduler.ps1
# Launch the AjaazAlert Report Scheduler bot (@AjaazAlertBot)
#
# Usage:
#   .\start_scheduler.ps1                   # morning (08:30) + evening (17:30)
#   .\start_scheduler.ps1 -Every2Hours      # run every 2 hours
#   .\start_scheduler.ps1 -Morning 09:00 -Evening 18:00  # custom times
#
param(
    [switch]$Every2Hours,
    [string]$Morning = "",
    [string]$Evening = "",
    [switch]$Help
)

if ($Help) {
    Write-Host ""
    Write-Host "AjaazAlert Report Scheduler — @AjaazAlertBot"
    Write-Host ""
    Write-Host "  .\start_scheduler.ps1                    # default schedule (08:30 + 17:30 IST)"
    Write-Host "  .\start_scheduler.ps1 -Every2Hours       # every 2 hours"
    Write-Host "  .\start_scheduler.ps1 -Morning 09:00 -Evening 18:30  # custom times"
    Write-Host ""
    exit 0
}

Set-Location $PSScriptRoot
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

# Activate venv
& .\.venv\Scripts\Activate.ps1

# Load env vars
. .\scheduler_env.ps1

# Apply overrides
if ($Every2Hours) {
    $env:SCHEDULER_EVERY_HOURS = "2"
    Write-Host "Mode: every 2 hours" -ForegroundColor Cyan
} else {
    Remove-Item Env:\SCHEDULER_EVERY_HOURS -ErrorAction SilentlyContinue
}

if ($Morning -ne "") {
    $env:SCHEDULER_MORNING_IST = $Morning
    Write-Host "Morning run overridden to: $Morning IST" -ForegroundColor Cyan
}
if ($Evening -ne "") {
    $env:SCHEDULER_EVENING_IST = $Evening
    Write-Host "Evening run overridden to: $Evening IST" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "Starting AjaazAlert Scheduler..." -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host ""

python report_scheduler.py
