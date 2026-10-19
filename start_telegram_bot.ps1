#!/usr/bin/env pwsh
# ============================================================
#  Stocks Trading Bot — Daily Start Script
#  Run this every morning to launch the Telegram bot.
# ============================================================

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path

# ── Load saved config if it exists ─────────────────────────
$envFile = "$ROOT\telegram_env.ps1"
if (Test-Path $envFile) {
    . $envFile
} else {
    Write-Host "ERROR: telegram_env.ps1 not found." -ForegroundColor Red
    Write-Host "Run setup_telegram.ps1 first to configure the bot." -ForegroundColor Yellow
    exit 1
}

# ── Validate token is set ───────────────────────────────────
if (-not $env:TELEGRAM_BOT_TOKEN) {
    Write-Host "ERROR: TELEGRAM_BOT_TOKEN is not set in telegram_env.ps1" -ForegroundColor Red
    exit 1
}

# ── Quick connectivity check ────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Stocks Trading Bot — @Ajaaz_bot" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

try {
    $me = Invoke-RestMethod "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/getMe"
    Write-Host "  Bot:     $($me.result.first_name) (@$($me.result.username))" -ForegroundColor Green
} catch {
    Write-Host "  WARNING: Could not reach Telegram API — offline mode" -ForegroundColor DarkYellow
}

$allowed = $env:TELEGRAM_ALLOWED_USERS
if ($allowed) {
    Write-Host "  Auth:    Restricted to user ID(s): $allowed" -ForegroundColor Green
} else {
    Write-Host "  Auth:    OPEN (no whitelist — run setup_telegram.ps1)" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "  Press Ctrl+C to stop the bot." -ForegroundColor DarkGray
Write-Host "  → Now open Telegram and message @Ajaaz_bot" -ForegroundColor Cyan
Write-Host ""

# ── Launch the bot as background service ───────────────────
Write-Host "  Starting bot as background service (hidden window)..." -ForegroundColor Cyan
$logDir  = Join-Path $ROOT "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = Join-Path $logDir "telegram_bot.log"
$errFile = Join-Path $logDir "telegram_bot_err.log"

$proc = Start-Process `
    -FilePath "$ROOT\.venv\Scripts\python.exe" `
    -ArgumentList "`"$ROOT\telegram_bot.py`"" `
    -WorkingDirectory $ROOT `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError  $errFile `
    -PassThru

if ($proc) {
    $proc.Id | Out-File -FilePath (Join-Path $ROOT ".telegram_bot.pid") -Encoding ascii -Force
    Write-Host "  ✅ Bot started in background (PID $($proc.Id))" -ForegroundColor Green
    Write-Host "  Logs: $logFile" -ForegroundColor DarkGray
    Write-Host "  Stop: Get-Process -Id $($proc.Id) | Stop-Process" -ForegroundColor DarkGray
} else {
    Write-Host "  ❌ Failed to start background process." -ForegroundColor Red
    exit 1
}
