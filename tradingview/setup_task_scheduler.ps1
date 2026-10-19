# setup_task_scheduler.ps1
# Creates a Windows Task Scheduler task to auto-start the webhook server on login.
# Run once as Administrator: .\setup_task_scheduler.ps1
# To remove:                  .\setup_task_scheduler.ps1 -Remove

param(
    [switch]$Remove
)

$TaskName  = "SignalStack-WebhookServer"
$BatFile   = Join-Path $PSScriptRoot "start_webhook.bat"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Yellow
    exit 0
}

# Build the task action (run the bat file silently in a minimised window)
$Action  = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$BatFile`"" `
    -WorkingDirectory (Split-Path $BatFile -Parent)

# Trigger: at user log-on (current user)
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings: run only when network is available, restart every 1 min on failure (up to 3 times)
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# Register / update the task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action   $Action `
    -Trigger  $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host ""
Write-Host " =============================================" -ForegroundColor Cyan
Write-Host "  Task Scheduler setup complete!" -ForegroundColor White
Write-Host ""
Write-Host "  Task name : $TaskName" -ForegroundColor Green
Write-Host "  Triggers  : At log-on ($env:USERNAME)" -ForegroundColor Green
Write-Host "  On failure: Restart up to 3 times" -ForegroundColor Green
Write-Host ""
Write-Host "  The webhook server will now start automatically" -ForegroundColor White
Write-Host "  whenever you log in to Windows." -ForegroundColor White
Write-Host ""
Write-Host "  To start immediately without rebooting, run:" -ForegroundColor Gray
Write-Host "    Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Yellow
Write-Host " =============================================" -ForegroundColor Cyan
Write-Host ""
