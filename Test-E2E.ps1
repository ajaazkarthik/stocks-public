#!/usr/bin/env pwsh
<#
.SYNOPSIS
    End-to-End Menu Test Runner — actually runs scripts and validates HTML reports.

.DESCRIPTION
    Unlike Test-MenuHandlers.ps1 (which runs mock-based pytest unit tests),
    this runner ACTUALLY EXECUTES each script and validates the generated HTML
    output files. It catches bugs that only appear in real execution:
      • NaN / null in report tables
      • Python tracebacks embedded in HTML
      • Empty tables (0 data rows)
      • Missing required brokers in global dashboard
      • Non-zero exit codes from scripts

    Usage:
        .\Test-E2E.ps1                          # run all non-live tests
        .\Test-E2E.ps1 -Key 14                  # run only option 14 (Signal Stack)
        .\Test-E2E.ps1 -Section TRADER          # run all TRADER options
        .\Test-E2E.ps1 -IncludeLive             # also run live-broker tests
        .\Test-E2E.ps1 -OpenReport              # open HTML report in browser when done
        .\Test-E2E.ps1 -Quick                   # summary totals only

.PARAMETER Key
    Run only one menu option key (e.g. "14", "6", "27").

.PARAMETER Section
    Run only one section: TRADER | TAX | ADMIN

.PARAMETER IncludeLive
    Include tests that require live broker connections (Moomoo OpenD, Zerodha, etc).
    These are skipped by default because they depend on external services.

.PARAMETER OpenReport
    Open the generated HTML report in the default browser when done.

.PARAMETER Quick
    Show only summary totals — no per-option detail.

.PARAMETER List
    List all registered test options and exit (no execution).

.PARAMETER NoColor
    Disable ANSI colours (for CI pipelines that can't render them).

.PARAMETER Out
    Path for the HTML output report. Default: reports/e2e_test_report.html
#>
param(
    [string] $Key          = "",
    [string] $Section      = "",
    [switch] $IncludeLive,
    [switch] $OpenReport,
    [switch] $Quick,
    [switch] $List,
    [switch] $NoColor,
    [string] $Out          = "reports/e2e_test_report.html"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ─── Colour helpers ───────────────────────────────────────────────────────────
function Col([string]$text, [string]$code) {
    if ($NoColor) { return $text }
    return "`e[$($code)m$text`e[0m"
}
function Green([string]$t)   { Col $t "32;1" }
function Red([string]$t)     { Col $t "31;1" }
function Yellow([string]$t)  { Col $t "33;1" }
function Cyan([string]$t)    { Col $t "36;1" }
function Magenta([string]$t) { Col $t "35;1" }
function Blue([string]$t)    { Col $t "34;1" }
function Purple([string]$t)  { Col $t "35" }
function Dim([string]$t)     { Col $t "2" }
function Bold([string]$t)    { Col $t "1" }

# ─── Header ───────────────────────────────────────────────────────────────────
$Now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host ""
Write-Host (Cyan "╔══════════════════════════════════════════════════════════╗")
Write-Host (Cyan "║   Stocks Trading System — E2E Menu Test Runner          ║")
Write-Host (Cyan "║   Runs REAL scripts · validates HTML output             ║")
Write-Host (Cyan "║   $Now                                  ║")
Write-Host (Cyan "╚══════════════════════════════════════════════════════════╝")
Write-Host ""

# ─── Build args for run_e2e_tests.py ─────────────────────────────────────────
$PyArgs = @("run_e2e_tests.py", "--out", $Out, "--json-out", "reports/e2e_test_results.json")
if ($Key)         { $PyArgs += "--key";      $PyArgs += $Key }
if ($Section)     { $PyArgs += "--section";  $PyArgs += $Section }
if ($IncludeLive) { $PyArgs += "--include-live" }
if ($Quick)       { $PyArgs += "--quick" }
if ($List)        { $PyArgs += "--list" }

# ─── Explain mode ─────────────────────────────────────────────────────────────
if ($Key)           { Write-Host (Bold "Mode: single option [$Key]") }
elseif ($Section)   { Write-Host (Bold "Mode: section $Section") }
else                { Write-Host (Bold "Mode: ALL options") }
if ($IncludeLive)   { Write-Host (Yellow "  ⚠  --IncludeLive: broker-dependent tests INCLUDED") }
else                { Write-Host (Dim   "  ℹ  Live broker tests SKIPPED (use -IncludeLive to include)") }
Write-Host ""

# ─── Run ──────────────────────────────────────────────────────────────────────
Push-Location $ScriptDir
$StartTime = Get-Date

Write-Host (Dim "  Running: python $($PyArgs -join ' ')")
Write-Host ""

$Process = Start-Process python -ArgumentList $PyArgs `
    -Wait -PassThru -NoNewWindow 2>&1

$ExitCode = $Process.ExitCode
$Elapsed  = (Get-Date) - $StartTime
Pop-Location

# ─── Parse JSON results ───────────────────────────────────────────────────────
$JsonPath = Join-Path $ScriptDir "reports\e2e_test_results.json"
$Results  = @()
if (Test-Path $JsonPath) {
    try {
        $Results = Get-Content $JsonPath -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Host (Yellow "Warning: could not parse $JsonPath — $($_.Exception.Message)")
    }
}

# ─── Per-option table ─────────────────────────────────────────────────────────
if (-not $Quick -and $Results.Count -gt 0) {
    $CurrentSection = ""

    foreach ($r in $Results) {
        $section = $r.section

        if ($section -ne $CurrentSection) {
            $CurrentSection = $section
            Write-Host ""
            switch ($section) {
                "TRADER" { Write-Host (Cyan  "  ── TRADER ──────────────────────────────────────────────") }
                "TAX"    { Write-Host (Green "  ── TAX ────────────────────────────────────────────────") }
                "ADMIN"  { Write-Host (Blue  "  ── ADMIN ──────────────────────────────────────────────") }
            }
        }

        $KeyStr   = "[$($r.key)]".PadRight(6)
        $LabelStr = $r.label.PadRight(40)
        $DurStr   = "$($r.duration)s".PadLeft(7)

        switch ($r.status) {
            "PASS"    { $StatusStr = Green   "  PASS ✓   " }
            "FAIL"    { $StatusStr = Red     "  FAIL ✗   " }
            "WARN"    { $StatusStr = Yellow  "  WARN !   " }
            "SKIP"    { $StatusStr = Dim     "  SKIP –   " }
            "TIMEOUT" { $StatusStr = Purple  "  TIME ⏱   " }
            "ERROR"   { $StatusStr = Red     "  ERR  E   " }
            default   { $StatusStr = "  ??? " }
        }

        Write-Host "  $(Cyan $KeyStr) $LabelStr $StatusStr $DurStr"

        # Show bugs / skip reason
        if ($r.html_bugs -and $r.html_bugs.Count -gt 0) {
            foreach ($bug in $r.html_bugs | Select-Object -First 3) {
                $short = $bug.Substring(0, [Math]::Min(90, $bug.Length))
                Write-Host "          $(Red "↳ BUG: $short")"
            }
        } elseif ($r.skip_reason) {
            Write-Host "          $(Dim "↳ $($r.skip_reason)")"
        } elseif ($r.stderr_snippet) {
            $short = $r.stderr_snippet.Substring(0, [Math]::Min(90, $r.stderr_snippet.Length))
            Write-Host "          $(Yellow "↳ $short")"
        }
    }
    Write-Host ""
}

# ─── Summary ──────────────────────────────────────────────────────────────────
if ($Results.Count -gt 0) {
    $TotalPass = ($Results | Where-Object { $_.status -eq "PASS" }).Count
    $TotalFail = ($Results | Where-Object { $_.status -eq "FAIL" }).Count
    $TotalWarn = ($Results | Where-Object { $_.status -eq "WARN" }).Count
    $TotalSkip = ($Results | Where-Object { $_.status -eq "SKIP" }).Count
    $TotalTime = ($Results | Where-Object { $_.status -eq "TIMEOUT" }).Count
    $TotalErr  = ($Results | Where-Object { $_.status -eq "ERROR"   }).Count
    $TotalBugs = ($Results | ForEach-Object { $_.html_bugs } | Measure-Object).Count
}

$ElapsedStr = "{0:F1}s" -f $Elapsed.TotalSeconds
Write-Host (Cyan "─────────────────────────────────────────────────────────")
if ($ExitCode -eq 0) {
    Write-Host (Green "  ALL E2E TESTS PASSED  ✓")
} else {
    Write-Host (Red   "  E2E FAILURES DETECTED  ✗")
}
Write-Host "  Wall time  : $(Bold $ElapsedStr)"
if ($Results.Count -gt 0) {
    Write-Host "  Passed     : $(Green $TotalPass)"
    if ($TotalFail   -gt 0) { Write-Host "  Failed     : $(Red     $TotalFail)" }
    if ($TotalWarn   -gt 0) { Write-Host "  Warned     : $(Yellow  $TotalWarn)" }
    if ($TotalSkip   -gt 0) { Write-Host "  Skipped    : $(Dim     $TotalSkip)" }
    if ($TotalTime   -gt 0) { Write-Host "  Timeouts   : $(Purple  $TotalTime)" }
    if ($TotalErr    -gt 0) { Write-Host "  Errors     : $(Red     $TotalErr)" }
    if ($TotalBugs   -gt 0) { Write-Host "  HTML bugs  : $(Red     "$TotalBugs bugs found in output")" }
}
Write-Host ""

# ─── Report paths ─────────────────────────────────────────────────────────────
$HtmlPath = Join-Path $ScriptDir $Out
if (Test-Path $HtmlPath) {
    Write-Host (Dim "  HTML report: $HtmlPath")
}
if (Test-Path $JsonPath) {
    Write-Host (Dim "  JSON data  : $JsonPath")
}
Write-Host ""

# ─── Open report ──────────────────────────────────────────────────────────────
if ($OpenReport -and (Test-Path $HtmlPath)) {
    Write-Host (Dim "  Opening $HtmlPath ...")
    Start-Process $HtmlPath
}

exit $ExitCode
