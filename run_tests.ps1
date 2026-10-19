# run_tests.ps1 — Full CI Gate for stocks trading system
# =====================================================================
# Run this before ANY commit to verify all tests pass.
# Expected: 680+ tests across all test files.
#
# Usage:
#   .\run_tests.ps1          # run all tests
#   .\run_tests.ps1 -Quick   # run only the core gate (fast)
# =====================================================================

param(
    [switch]$Quick
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot

if ($Quick) {
    Write-Host "`n=== QUICK GATE (core tests only) ===" -ForegroundColor Cyan
    python -m pytest tests/test_menu_handlers.py tests/test_html_reports.py -q --tb=short
} else {
    Write-Host "`n=== FULL CI GATE ===" -ForegroundColor Cyan
    python -m pytest `
        tests/test_menu_handlers.py `
        tests/test_html_reports.py `
        tests/test_ibkr_client_portal.py `
        india-brokers/test_india_market_internals.py `
        tradingview/signal_engine/tests/test_moomoo_enricher.py `
        tests/contracts/ `
        --cov=tradingview --cov=india-brokers `
        --cov-fail-under=8 `
        --cov-report=term-missing:skip-covered `
        -q --tb=short
}

$exitCode = $LASTEXITCODE
Pop-Location

if ($exitCode -eq 0) {
    Write-Host "`n=== ALL TESTS PASSED ===" -ForegroundColor Green
} else {
    Write-Host "`n=== TESTS FAILED (exit code: $exitCode) ===" -ForegroundColor Red
}

exit $exitCode
