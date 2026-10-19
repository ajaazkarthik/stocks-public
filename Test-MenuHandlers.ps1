#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Automatic PowerShell test runner for every main.py menu option.

.DESCRIPTION
    Runs the full tests/test_menu_handlers.py + tests/test_html_reports.py suite via pytest
    and presents a colour-coded per-menu-option pass/fail dashboard.

    Usage:
        .\Test-MenuHandlers.ps1                   # full run, coloured table
        .\Test-MenuHandlers.ps1 -Key 14           # test only option 14 (Signal Stack)
        .\Test-MenuHandlers.ps1 -Section TRADER   # test all TRADER menu options
        .\Test-MenuHandlers.ps1 -Quick            # summary totals only (no per-option table)
        .\Test-MenuHandlers.ps1 -HtmlReport       # also open the HTML report when done

.PARAMETER Key
    Run tests for a single menu key (e.g. "14", "6a", "48").

.PARAMETER Section
    Run tests for one section: TRADER | TAX | ADMIN

.PARAMETER Quick
    Print only the pass/fail totals — no per-option table.

.PARAMETER HtmlReport
    Open reports/menu_test_results.html in the default browser when done.

.PARAMETER NoColor
    Disable ANSI colours (useful for CI pipelines that can't render them).
#>
param(
    [string]  $Key        = "",
    [string]  $Section    = "",   # TRADER | TAX | ADMIN
    [switch]  $Quick,
    [switch]  $HtmlReport,
    [switch]  $NoColor
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
function Dim([string]$t)     { Col $t "2" }
function Bold([string]$t)    { Col $t "1" }
function Magenta([string]$t) { Col $t "35;1" }
function Blue([string]$t)    { Col $t "34;1" }

# ─── Menu key → pytest -k filter + human label ───────────────────────────────
# Format: [key] = @{ label="…"; filter="pytest -k expression"; section="TRADER|TAX|ADMIN" }
$MenuMap = [ordered]@{
    "0"  = @{ label="Morning Run (all reports)";              filter="TestRunMorningAll";          section="TRADER" }
    "1"  = @{ label="Global Portfolio Dashboard";             filter="TestRunGlobalDashboard";      section="TRADER" }
    "2"  = @{ label="India Portfolio Dashboard";              filter="TestIndiaDashboard";           section="TRADER" }
    "3"  = @{ label="Portfolio Analytics";                    filter="TestRunPortfolioAnalytics";    section="TRADER" }
    "4"  = @{ label="Report Analytics";                       filter="TestRunReportAnalytics";       section="TRADER" }
    "5"  = @{ label="India GTT Risk Mgmt [submenu]";          filter="TestIndiaZerodhaGTT or TestIndiaAngelOneGTT"; section="TRADER" }
    "6"  = @{ label="Options from Actionable Signals";        filter="TestFOOptions";                section="TRADER" }
    "6a" = @{ label="PUT Options from Actionable Signals";    filter="TestPutOptions";               section="TRADER" }
    "6b" = @{ label="PUT Options from Bearish Screen";        filter="TestPutOptions";               section="TRADER" }
    "7"  = @{ label="Options for single symbol";              filter="TestFOOptions";                section="TRADER" }
    "8"  = @{ label="India NSE F&O only";                     filter="TestFOOptions";                section="TRADER" }
    "9"  = @{ label="US Options only";                        filter="TestFOOptions";                section="TRADER" }
    "10" = @{ label="Option Position Manager";                filter="TestRunOptionPositionManager"; section="TRADER" }
    "11" = @{ label="F&O / Forex Arbitrage Engine";           filter="TestRunFoArbitrage";           section="TRADER" }
    "12" = @{ label="Options Hedging Advisor";                filter="TestRunOptionsHedger";         section="TRADER" }
    "13" = @{ label="Download fired TV alerts";               filter="TestTVDownloadFires";          section="TRADER" }
    "14" = @{ label="Signal Stack (rank + score)";            filter="TestTVSignalStack";            section="TRADER" }
    "15" = @{ label="Sell Strategy";                          filter="TestSellStrategy";             section="TRADER" }
    "16" = @{ label="Multi-Asset Screener";                   filter="TestScreeners";                section="TRADER" }
    "17" = @{ label="Morning Screener US";                    filter="TestScreeners";                section="TRADER" }
    "18" = @{ label="Morning Screener HK";                    filter="TestScreeners";                section="TRADER" }
    "19" = @{ label="Morning Screener Custom";                filter="TestScreeners";                section="TRADER" }
    "20" = @{ label="India Market Internals";                 filter="TestIndiaMarketInternals";     section="TRADER" }
    "21" = @{ label="India Order Executor";                   filter="TestIndiaOrderTest";           section="TRADER" }
    "22" = @{ label="India F&O Analyzer";                     filter="TestIndiaTests";               section="TRADER" }
    "23" = @{ label="Moomoo CGT (AU)";                        filter="TestMoomooCGT";                section="TAX"    }
    "24" = @{ label="Moomoo CGT (SG)";                        filter="TestMoomooCGT";                section="TAX"    }
    "25" = @{ label="IBKR CGT";                               filter="TestIBKRCGT";                  section="TAX"    }
    "26" = @{ label="Webull CGT";                             filter="TestWebullCGT";                section="TAX"    }
    "27" = @{ label="Combined CGT";                           filter="TestCombinedCGT";              section="TAX"    }
    "28" = @{ label="Test Enrichers";                         filter="TestTVTestEnrichers";          section="ADMIN"  }
    "29" = @{ label="Strategy Backtest";                      filter="TestStrategyBacktest";         section="ADMIN"  }
    "30" = @{ label="Webhook Server";                         filter="TestTVWebhookServer";          section="ADMIN"  }
    "31" = @{ label="Broker API Audit";                       filter="TestBrokerApiAudit";           section="ADMIN"  }
    "32" = @{ label="Moomoo Market Tools";                    filter="TestRunMoomooMarketTools";     section="ADMIN"  }
    "33" = @{ label="Auto Trader";                            filter="TestAutoTrader";               section="ADMIN"  }
    "34" = @{ label="Live Dashboard (Streamlit)";             filter="TestLiveDashboard";            section="ADMIN"  }
    "35" = @{ label="Signal Backtest (vectorbt)";             filter="TestVectorbbtBacktest";        section="ADMIN"  }
    "36" = @{ label="Analyse tickers (manual)";               filter="TestRunManualAnalyse";         section="ADMIN"  }
    "37" = @{ label="TV screener scan";                       filter="TestRunScreenerRun";           section="ADMIN"  }
    "38" = @{ label="Alert tracker: update prices";           filter="TestRunAlertTrackerUpdate";    section="ADMIN"  }
    "39" = @{ label="Alert tracker: performance report";      filter="TestRunAlertTrackerReport";    section="ADMIN"  }
    "40" = @{ label="Alert tracker: stats summary";           filter="TestRunAlertTrackerStats";     section="ADMIN"  }
    "41" = @{ label="Alert tracker: full run";                filter="TestRunAlertTrackerAll";       section="ADMIN"  }
    "42" = @{ label="News scan: bullish signals";             filter="TestNewsSignalHandlers";       section="ADMIN"  }
    "43" = @{ label="News scan: bearish signals";             filter="TestNewsSignalHandlers";       section="ADMIN"  }
    "44" = @{ label="News scan: all directions";              filter="TestNewsSignalHandlers";       section="ADMIN"  }
    "44a"= @{ label="Moomoo Community Intel";                 filter="TestMoomooCommunityIntel";     section="ADMIN"  }
    "45" = @{ label="Trade History Analysis";                 filter="TestTradeHistoryAnalysis";     section="ADMIN"  }
    "46" = @{ label="Fundamental Analysis";                   filter="TestRunFundamentalAnalysis";   section="ADMIN"  }
    "47" = @{ label="Performance Analyzer";                   filter="TestPerformanceAnalyzer";      section="ADMIN"  }
    "48" = @{ label="Tests submenu";                          filter="TestMetaTestRunners";          section="ADMIN"  }
}

# ─── Resolve which keys to test ───────────────────────────────────────────────
$TargetKeys = @()
if ($Key -ne "") {
    if (-not $MenuMap.Contains($Key)) {
        Write-Host (Red "Unknown menu key: '$Key'")
        Write-Host "Valid keys: $($MenuMap.Keys -join ', ')"
        exit 1
    }
    $TargetKeys = @($Key)
} elseif ($Section -ne "") {
    $sec = $Section.ToUpper()
    $TargetKeys = @($MenuMap.Keys | Where-Object { $MenuMap[$_].section -eq $sec })
    if ($TargetKeys.Count -eq 0) {
        Write-Host (Red "No keys found for section '$Section'. Use TRADER, TAX, or ADMIN.")
        exit 1
    }
} else {
    $TargetKeys = @($MenuMap.Keys)
}

# ─── Header ───────────────────────────────────────────────────────────────────
$Now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host ""
Write-Host (Cyan "╔══════════════════════════════════════════════════════════╗")
Write-Host (Cyan "║   Stocks Trading System — Menu Auto-Test Runner         ║")
Write-Host (Cyan "║   $Now                                  ║")
Write-Host (Cyan "╚══════════════════════════════════════════════════════════╝")
Write-Host ""
if ($Section -ne "") { Write-Host (Bold "Section filter: $Section ($($TargetKeys.Count) options)") }
elseif ($Key -ne "")  { Write-Host (Bold "Single key filter: $Key") }
else                  { Write-Host (Bold "Running all $($TargetKeys.Count) menu options") }
Write-Host ""

# ─── Step 1: Run full suite once with JUnit XML output ────────────────────────
$XmlPath  = Join-Path $ScriptDir "reports\menu_test_results.xml"
$HtmlPath = Join-Path $ScriptDir "reports\menu_test_results.html"
$null = New-Item -ItemType Directory -Force -Path (Join-Path $ScriptDir "reports") | Out-Null

# Build -k filter for targeted runs
$KFilter = ""
if ($TargetKeys.Count -lt $MenuMap.Count) {
    # Collect unique pytest filter expressions for selected keys
    $Filters = @($TargetKeys | ForEach-Object { $MenuMap[$_].filter } | Sort-Object -Unique)
    $KFilter = $Filters -join " or "
}

$PytestArgs = @(
    "tests/test_menu_handlers.py",
    "tests/test_html_reports.py",
    "--junit-xml=$XmlPath",
    "-q",
    "--tb=line",
    "--no-header"
)
if ($HtmlReport) {
    $PytestArgs += "--html=$HtmlPath"
    $PytestArgs += "--self-contained-html"
}
if ($KFilter -ne "") {
    $PytestArgs += "-k"
    $PytestArgs += $KFilter
}

Write-Host (Dim "Running: python -m pytest $($PytestArgs -join ' ')")
Write-Host ""

Push-Location $ScriptDir
$StartTime = Get-Date
$PytestOutput = python -m pytest @PytestArgs 2>&1
$ExitCode = $LASTEXITCODE
$Elapsed = (Get-Date) - $StartTime
Pop-Location

# ─── Step 2: Parse JUnit XML ──────────────────────────────────────────────────
$Results = @{}   # key: test class name  →  @{ pass=N; fail=N; skip=N; errors=[] }

if (Test-Path $XmlPath) {
    [xml]$Xml = Get-Content $XmlPath -Encoding UTF8
    foreach ($suite in $Xml.testsuites.testsuite) {
        foreach ($case in $suite.testcase) {
            $cls = $case.classname -replace "^.*\.", ""   # strip module prefix
            if (-not $Results.Contains($cls)) {
                $Results[$cls] = @{ pass=0; fail=0; skip=0; errors=@() }
            }
            if ($case.PSObject.Properties['failure'] -and $case.failure) {
                $Results[$cls].fail++
                $errMsg = if ($case.failure.PSObject.Properties['message']) { $case.failure.message } else { "$($case.failure)" }
                $Results[$cls].errors += "$($case.name): $($errMsg -replace '\n',' ')"
            } elseif ($case.PSObject.Properties['skipped'] -and $case.skipped) {
                $Results[$cls].skip++
            } else {
                $Results[$cls].pass++
            }
        }
    }
} else {
    Write-Host (Yellow "Warning: JUnit XML not found at $XmlPath — using raw output only.")
}

# ─── Step 3: Per-option table ─────────────────────────────────────────────────
# Build de-duplication: same filter → same result entry shown once per key
$ShownFilters = @{}

if (-not $Quick) {
    # Print section banners
    $CurrentSection = ""

    foreach ($k in $TargetKeys) {
        $entry   = $MenuMap[$k]
        $label   = $entry.label
        $filter  = $entry.filter
        $sec     = $entry.section

        # Section header
        if ($sec -ne $CurrentSection) {
            $CurrentSection = $sec
            Write-Host ""
            switch ($sec) {
                "TRADER" { Write-Host (Cyan  "  ── TRADER ──────────────────────────────────────────────") }
                "TAX"    { Write-Host (Green "  ── TAX ────────────────────────────────────────────────") }
                "ADMIN"  { Write-Host (Blue  "  ── ADMIN ──────────────────────────────────────────────") }
            }
        }

        # Look up result for each unique filter class
        $FilterClasses = $filter -split " or " | ForEach-Object { $_.Trim() }
        $TotalPass = 0; $TotalFail = 0; $TotalSkip = 0; $AllErrors = @()
        foreach ($cls in $FilterClasses) {
            if ($Results.Contains($cls)) {
                $TotalPass += $Results[$cls].pass
                $TotalFail += $Results[$cls].fail
                $TotalSkip += $Results[$cls].skip
                $AllErrors += $Results[$cls].errors
            }
        }

        # Status icon
        $KeyStr = "[$k]".PadRight(5)
        $LabelStr = $label.PadRight(42)

        if (-not $Results.Keys -or ($TotalPass -eq 0 -and $TotalFail -eq 0 -and $TotalSkip -eq 0)) {
            $Status = Yellow "  NO TESTS"
            $Counts = Dim "(filter: $filter)"
        } elseif ($TotalFail -gt 0) {
            $Status = Red   "  FAIL ✗   "
            $Counts = "$(Red "$TotalFail failed") · $(Green "$TotalPass passed") · $(Dim "$TotalSkip skipped")"
        } else {
            $Status = Green "  PASS ✓   "
            $Counts = "$(Green "$TotalPass passed") · $(Dim "$TotalSkip skipped")"
        }

        Write-Host "  $(Cyan $KeyStr) $LabelStr $Status  $Counts"

        # Show first error detail inline
        if ($TotalFail -gt 0 -and $AllErrors.Count -gt 0) {
            $short = ($AllErrors[0] -replace "`n"," ").Substring(0, [Math]::Min(100, $AllErrors[0].Length))
            Write-Host "          $(Dim "↳ $short")"
        }
    }
    Write-Host ""
}

# ─── Step 4: Summary totals ───────────────────────────────────────────────────
$TotalP = 0; $TotalF = 0; $TotalS = 0
foreach ($v in $Results.Values) { $TotalP += $v.pass; $TotalF += $v.fail; $TotalS += $v.skip }
$TotalTests = $TotalP + $TotalF + $TotalS
$ElapsedStr = "{0:F1}s" -f $Elapsed.TotalSeconds

Write-Host (Cyan "─────────────────────────────────────────────────────────")
if ($TotalF -eq 0 -and $TotalTests -gt 0) {
    Write-Host (Green "  ALL TESTS PASSED  ✓")
} elseif ($TotalF -gt 0) {
    Write-Host (Red "  FAILURES DETECTED  ✗")
}
Write-Host "  Tests run  : $(Bold $TotalTests) in $ElapsedStr"
Write-Host "  Passed     : $(Green $TotalP)"
if ($TotalF -gt 0) { Write-Host "  Failed     : $(Red $TotalF)" }
if ($TotalS -gt 0) { Write-Host "  Skipped    : $(Dim $TotalS)" }
Write-Host ""

# ─── Step 5: Raw pytest output if failures ────────────────────────────────────
if ($ExitCode -ne 0 -and -not $Quick) {
    Write-Host (Yellow "──── pytest output (failures only) ──────────────────────")
    $PytestOutput | ForEach-Object {
        $line = "$_"
        if ($line -match "^FAILED|^ERROR") { Write-Host (Red $line) }
        elseif ($line -match "AssertionError|assert") { Write-Host (Yellow "  $line") }
        elseif ($line -match "^PASSED") { } # suppress passing lines in failure mode
        else { Write-Host (Dim $line) }
    }
    Write-Host ""
}

# ─── Step 6: Open HTML report ─────────────────────────────────────────────────
if ($HtmlReport -and (Test-Path $HtmlPath)) {
    Write-Host (Dim "Opening $HtmlPath ...")
    Start-Process $HtmlPath
}

# ─── Step 7: XML report path ──────────────────────────────────────────────────
if (Test-Path $XmlPath) {
    Write-Host (Dim "  JUnit XML  : $XmlPath")
}
Write-Host ""

exit $ExitCode
