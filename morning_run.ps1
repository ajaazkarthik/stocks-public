<# morning_run.ps1 — Scheduled Morning Run for Stocks Trading System
.SYNOPSIS
  Runs the complete morning analysis pipeline: dashboards, screeners, signals.
  Designed for Windows Task Scheduler (daily at 08:00 local time).

.DESCRIPTION
  Executes in order:
    1. Global Portfolio Dashboard (all 6 brokers)
    2. India Portfolio Dashboard (Zerodha + AngelOne)
    3. Morning Screener (all markets)
    4. Signal Stack (process any pending fires)
    5. Auto-Validation (weekly quality check)

.PARAMETER SkipIndia
  Skip India dashboards (use when market is closed)

.PARAMETER SkipScreener
  Skip screener (use when you only want dashboards)

.PARAMETER DryRun
  Print what would run without executing

.EXAMPLE
  .\morning_run.ps1
  .\morning_run.ps1 -SkipIndia
  .\morning_run.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [switch]$SkipIndia,
    [switch]$SkipScreener,
    [switch]$SkipValidation,
    [switch]$DryRun
)

# ── Configuration ──────────────────────────────────────────────────────────────
$ErrorActionPreference = "Continue"  # Don't stop on individual script failures
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ROOT) { $ROOT = $PSScriptRoot }
if (-not $ROOT) { $ROOT = Get-Location }

$PYTHON = Join-Path $ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $PYTHON)) {
    $PYTHON = "python"  # Fallback to system Python
}

$LOG_DIR = Join-Path $ROOT "logs"
$LOG_FILE = Join-Path $LOG_DIR ("morning_run_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

# Ensure log directory exists
if (-not (Test-Path $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
}

# ── Logging ────────────────────────────────────────────────────────────────────
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $entry = "[$timestamp] [$Level] $Message"
    Write-Host $entry
    Add-Content -Path $LOG_FILE -Value $entry -Encoding UTF8
}

function Run-Step {
    param(
        [string]$Name,
        [string]$Script,
        [string[]]$Arguments = @()
    )

    Write-Log "Starting: $Name"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    if ($DryRun) {
        Write-Log "[DRY RUN] Would execute: $PYTHON $Script $($Arguments -join ' ')" "DRY"
        return $true
    }

    try {
        $fullPath = Join-Path $ROOT $Script
        if (-not (Test-Path $fullPath)) {
            Write-Log "SKIP: $Script not found at $fullPath" "WARN"
            return $false
        }

        $allArgs = @($fullPath) + $Arguments
        $proc = Start-Process -FilePath $PYTHON -ArgumentList $allArgs `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput (Join-Path $LOG_DIR "${Name}_stdout.log") `
            -RedirectStandardError (Join-Path $LOG_DIR "${Name}_stderr.log")

        $sw.Stop()
        $elapsed = $sw.Elapsed.ToString("mm\:ss")

        if ($proc.ExitCode -eq 0) {
            Write-Log "DONE: $Name (${elapsed})" "OK"
            return $true
        } else {
            Write-Log "FAIL: $Name exited with code $($proc.ExitCode) (${elapsed})" "ERROR"
            return $false
        }
    }
    catch {
        $sw.Stop()
        Write-Log "EXCEPTION in ${Name}: $_" "ERROR"
        return $false
    }
}

# ── Environment Setup ──────────────────────────────────────────────────────────
Write-Log "=" * 60
Write-Log "Morning Run Starting"
Write-Log "ROOT: $ROOT"
Write-Log "Python: $PYTHON"
Write-Log "=" * 60

# Set mandatory env vars
$env:PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION = "python"
$env:OPENBLAS_NUM_THREADS = "1"

# ── Step 1: Global Portfolio Dashboard ─────────────────────────────────────────
$results = @{}
$results["Global Dashboard"] = Run-Step -Name "global_dashboard" `
    -Script "global_portfolio_dashboard.py" `
    -Arguments @("--morning")

# ── Step 2: India Portfolio Dashboard ──────────────────────────────────────────
if (-not $SkipIndia) {
    $results["India Dashboard"] = Run-Step -Name "india_dashboard" `
        -Script "india-brokers\india_portfolio_dashboard.py" `
        -Arguments @("--morning")
} else {
    Write-Log "SKIP: India Dashboard (--SkipIndia)" "SKIP"
    $results["India Dashboard"] = "SKIPPED"
}

# ── Step 3: Morning Screener ───────────────────────────────────────────────────
if (-not $SkipScreener) {
    $results["Morning Screener"] = Run-Step -Name "morning_screener" `
        -Script "tradingview\screeners\morning_screener.py" `
        -Arguments @("--morning")
} else {
    Write-Log "SKIP: Morning Screener (--SkipScreener)" "SKIP"
    $results["Morning Screener"] = "SKIPPED"
}

# ── Step 4: Signal Stack (process pending fires) ──────────────────────────────
$results["Signal Stack"] = Run-Step -Name "signal_stack" `
    -Script "tradingview\signal_stack.py" `
    -Arguments @("--morning")

# ── Step 5: Auto-Validation (weekly check) ────────────────────────────────────
if (-not $SkipValidation) {
    # Only run on Mondays
    $dayOfWeek = (Get-Date).DayOfWeek
    if ($dayOfWeek -eq "Monday" -or $DryRun) {
        $results["Auto Validation"] = Run-Step -Name "auto_validation" `
            -Script "tradingview\analysis\auto_validation.py"
    } else {
        Write-Log "SKIP: Auto-Validation (not Monday)" "SKIP"
        $results["Auto Validation"] = "SKIPPED"
    }
} else {
    Write-Log "SKIP: Auto-Validation (--SkipValidation)" "SKIP"
    $results["Auto Validation"] = "SKIPPED"
}

# ── Summary ────────────────────────────────────────────────────────────────────
Write-Log "=" * 60
Write-Log "Morning Run Complete"
$pass = 0; $fail = 0; $skip = 0
foreach ($key in $results.Keys) {
    $val = $results[$key]
    if ($val -eq $true) { $pass++; Write-Log "  ✅ $key" }
    elseif ($val -eq $false) { $fail++; Write-Log "  ❌ $key" "ERROR" }
    else { $skip++; Write-Log "  ⏭️ $key ($val)" "SKIP" }
}
Write-Log "Results: ${pass} passed, ${fail} failed, ${skip} skipped"
Write-Log "Log: $LOG_FILE"
Write-Log "=" * 60

# Exit with failure if any step failed
if ($fail -gt 0) { exit 1 }
exit 0
