# AjaazAlert Report Scheduler — Environment Variables (TRACKED TEMPLATE)
# Bot: @AjaazAlertBot
#
# ⚠️  This file is committed to Git. Do NOT put real credentials here.
#
# Real values live in the ignored local file `scheduler_env.local.ps1`, which is
# dot-sourced automatically below when present. Create it once with:
#
#   Copy-Item scheduler_env.local.ps1.example scheduler_env.local.ps1
#   # then fill in the real values
#
# Non-secret scheduling configuration may stay in this tracked file.

# ── Schedule (IST, 24h format) ─────────────────────────────────────────────
$env:SCHEDULER_MORNING_IST  = "08:30"   # morning run
$env:SCHEDULER_EVENING_IST  = "17:30"   # evening run
$env:SCHEDULER_ZERODHA_WARN = "08:15"   # warn before morning run

# ── Interval mode: run every N hours ───────────────────────────────────────
$env:SCHEDULER_EVERY_HOURS  = "2"

# ── Days to run (1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat, 7=Sun) ─────────
$env:SCHEDULER_DAYS         = "1,2,3,4,5"   # Mon–Fri

# ── Timezones ──────────────────────────────────────────────────────────────
$env:SCHEDULER_TIMEZONE         = "Asia/Kolkata"
$env:SCHEDULER_DISPLAY_TIMEZONE = "Australia/Sydney"

# ── Thresholds (separate policies) ─────────────────────────────────────────
$env:SCHEDULER_STOCK_THRESHOLD = "62.5"
$env:SCHEDULER_SCORE_THRESHOLD = "70"

# ── Delivery policy ────────────────────────────────────────────────────────
$env:SCHEDULER_ALERT_TTL_HOURS = "12"
$env:SCHEDULER_MAX_ATTEMPTS    = "5"

$env:PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION = "python"
$env:PYTHONIOENCODING       = "utf-8"

# ── Load local secrets if present (ignored by Git) ─────────────────────────
$localSecrets = Join-Path $PSScriptRoot "scheduler_env.local.ps1"
if (Test-Path $localSecrets) {
    . $localSecrets
}

Write-Host "AjaazAlert scheduler env loaded." -ForegroundColor Green
Write-Host "Bot: @AjaazAlertBot | Morning: $env:SCHEDULER_MORNING_IST IST | Evening: $env:SCHEDULER_EVENING_IST IST"
