#!/usr/bin/env bash
# start_chrome_cdp.sh — Launch Chrome with CDP debug port enabled (macOS)
# This allows webull_au_live.py --cdp to connect to your existing Chrome session.
#
# USAGE:
#   ./start_chrome_cdp.sh
#   Then log in to https://www.webull.com.au in Chrome.
#   Then run: python webull_au_live.py --cdp
#
# NOTE: Chrome must be fully closed before running this script.
#       If Chrome is already running, close it first (all windows).

set -euo pipefail

# ── Find Google Chrome on macOS ─────────────────────────────────────────────────
CHROME_PATHS=(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    "/Applications/Chromium.app/Contents/MacOS/Chromium"
)

CHROME=""
for p in "${CHROME_PATHS[@]}"; do
    if [[ -f "$p" ]]; then
        CHROME="$p"
        break
    fi
done

if [[ -z "$CHROME" ]]; then
    # Try to find via mdfind (Spotlight)
    CHROME=$(mdfind 'kMDItemCFBundleIdentifier == "com.google.Chrome"' 2>/dev/null | head -1)
    if [[ -n "$CHROME" ]]; then
        CHROME="$CHROME/Contents/MacOS/Google Chrome"
    fi
fi

if [[ -z "$CHROME" ]] || [[ ! -f "$CHROME" ]]; then
    echo "ERROR: Google Chrome not found." >&2
    echo "       Install Chrome from https://www.google.com/chrome/" >&2
    echo "       Or set CHROME_PATH env var to the Chrome binary." >&2
    exit 1
fi

echo "Launching Chrome with CDP on port 9222 ..."
echo "  $CHROME"
echo ""
echo "After Chrome opens:"
echo "  1. Log in to https://www.webull.com.au"
echo "  2. Run: python webull_au_live.py --cdp"

"$CHROME" --remote-debugging-port=9222 "https://www.webull.com.au"
