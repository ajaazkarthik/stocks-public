# Launch Chrome with CDP (Chrome DevTools Protocol) debug port enabled.
# This allows webull_au_live.py --cdp to connect to your existing Chrome session.
#
# USAGE:
#   .\start_chrome_cdp.ps1
#   Then log in to https://www.webull.com.au in Chrome.
#   Then run: python webull_au_live.py --cdp
#
# NOTE: Chrome must be fully closed before running this script.
#       If Chrome is already running, close it first (all windows).

$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)

$chrome = $null
foreach ($p in $chromePaths) {
    if (Test-Path $p) {
        $chrome = $p
        break
    }
}

if (-not $chrome) {
    Write-Host "Chrome not found. Please set the path manually." -ForegroundColor Red
    exit 1
}

Write-Host "Launching Chrome with CDP on port 9222 …" -ForegroundColor Green
Write-Host "  $chrome" -ForegroundColor DarkGray
Write-Host ""
Write-Host "After Chrome opens:" -ForegroundColor Yellow
Write-Host "  1. Log in to https://www.webull.com.au" -ForegroundColor Yellow
Write-Host "  2. Run: python webull_au_live.py --cdp" -ForegroundColor Yellow

& $chrome --remote-debugging-port=9222 "https://www.webull.com.au"
