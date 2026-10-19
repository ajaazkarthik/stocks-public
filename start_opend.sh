#!/usr/bin/env bash
# start_opend.sh - Launch both CLI OpenD instances (AU + SG) on macOS.
# Password is stored in OpenD.xml in each account's directory.
#
# Accounts:
#   AU - phone +61 470625861 - port 11111 - telnet 22221 - acc_id 284008279318020118
#   SG - email karthiyuvi99@gmail.com - port 11112 - telnet 22222 - acc_id 283726805871679562
#
# Usage:
#   ./start_opend.sh           # start both AU and SG
#   ./start_opend.sh --skip-sg  # start AU only
#
# SETUP:
#   Download Moomoo OpenD (CLI version) from:
#   https://www.moomoo.com/en-au/topics/moomoo-api/open-d
#
#   Default expected paths (edit AU_DIR / SG_DIR below if different):
#     macOS: ~/moomoo_opend/au/OpenD   and   ~/moomoo_opend/sg/OpenD
#
#   Or set environment variables before sourcing:
#     export OPEND_AU_DIR="$HOME/moomoo_opend/au"
#     export OPEND_SG_DIR="$HOME/moomoo_opend/sg"
#
#   CREDENTIALS (required — this is the #1 cause of "no credentials available"):
#     OpenD reads its login_account/login_pwd from an OpenD.xml file placed
#     NEXT TO the OpenD binary (i.e. $AU_DIR/OpenD.xml, $SG_DIR/OpenD.xml).
#     This file is intentionally NEVER committed to git (see opend_configs/*/OpenD.xml
#     for the field reference/schema WITHOUT a password) so every new machine
#     — including this one — needs it created by hand once:
#
#       mkdir -p "$AU_DIR" "$SG_DIR"
#       cp opend_configs/au/OpenD.xml "$AU_DIR/OpenD.xml"
#       cp opend_configs/sg/OpenD.xml "$SG_DIR/OpenD.xml"
#       # then edit both copies:
#         - add a <login_pwd>...</login_pwd> line with the real password
#         - change <log_path> to a real path on this machine, e.g.
#           <log_path>$HOME/moomoo_opend/au/logs</log_path>

set -uo pipefail

SKIP_SG=false
for arg in "$@"; do
    case "$arg" in
        --skip-sg) SKIP_SG=true ;;
    esac
done

# ── Configurable paths (override with env vars or edit here) ────────────────────
AU_DIR="${OPEND_AU_DIR:-$HOME/moomoo_opend/au}"
SG_DIR="${OPEND_SG_DIR:-$HOME/moomoo_opend/sg}"
AU_API_PORT=11111
SG_API_PORT=11112
AU_TELNET=22221
SG_TELNET=22222

# Determine OpenD executable name
# macOS: the CLI binary is typically named "OpenD" (no extension)
resolve_opend_exe() {
    local dir="$1"
    if [[ -f "$dir/OpenD" ]]; then
        printf '%s\n' "$dir/OpenD"
    elif [[ -f "$dir/moomoo_OpenD" ]]; then
        printf '%s\n' "$dir/moomoo_OpenD"
    elif [[ -f "$dir/OpenD.app/Contents/MacOS/OpenD" ]]; then
        printf '%s\n' "$dir/OpenD.app/Contents/MacOS/OpenD"
    fi
}
AU_EXE="$(resolve_opend_exe "$AU_DIR")"
SG_EXE="$(resolve_opend_exe "$SG_DIR")"

# ── Verify installs ─────────────────────────────────────────────────────────────
if [[ -z "$AU_EXE" ]]; then
    echo "ERROR: AU OpenD binary not found in: $AU_DIR" >&2
    echo "       Looked for: OpenD, moomoo_OpenD, OpenD.app/Contents/MacOS/OpenD" >&2
    echo "       Download from https://www.moomoo.com/en-au/topics/moomoo-api/open-d" >&2
    echo "       Or set OPEND_AU_DIR env var to the correct directory." >&2
    exit 1
fi
if ! $SKIP_SG && [[ ! -f "$SG_EXE" ]]; then
    echo "ERROR: SG OpenD binary not found: $SG_EXE" >&2
    echo "       Set OPEND_SG_DIR env var or use --skip-sg to run AU only." >&2
    exit 1
fi

# ── Kill any running OpenD processes ────────────────────────────────────────────
EXISTING=$(pgrep -x "OpenD" 2>/dev/null || true)
if [[ -n "$EXISTING" ]]; then
    echo "Stopping existing OpenD process(es) (PIDs: $EXISTING)..."
    kill $EXISTING 2>/dev/null || true
    sleep 2
fi

# ── Helper: check if port is listening ──────────────────────────────────────────
port_open() {
    local port="$1"
    # Use /dev/tcp (bash built-in) — no netcat dependency
    (echo > /dev/tcp/127.0.0.1/"$port") 2>/dev/null
}

# ── Helper: watch OpenD log for pattern ─────────────────────────────────────────
wait_for_log() {
    local log_dir="$1"
    local pattern="$2"
    local timeout="${3:-30}"
    local end_time=$(( $(date +%s) + timeout ))

    while [[ $(date +%s) -lt $end_time ]]; do
        local latest_log
        latest_log=$(find "$log_dir/Log" -name "*.log" 2>/dev/null | sort -t_ -k1 | tail -1)
        if [[ -n "$latest_log" ]]; then
            if tail -40 "$latest_log" 2>/dev/null | grep -qE "$pattern"; then
                return 0
            fi
        fi
        sleep 0.5
    done
    return 1
}

# ── Launch an OpenD instance ─────────────────────────────────────────────────────
start_opend_instance() {
    local label="$1"
    local exe="$2"
    local dir="$3"
    local api_port="$4"
    local log_dir="$5"
    local label_lc
    label_lc=$(printf '%s' "$label" | tr 'A-Z' 'a-z')  # portable lowercase (bash 3.2 has no ${var,,})

    echo "Starting $label OpenD (port $api_port)..."

    if [[ ! -f "$dir/OpenD.xml" ]]; then
        echo "  ERROR: $dir/OpenD.xml not found — OpenD has no login_account/login_pwd" >&2
        echo "         to read, so it will refuse to log in (\"no credentials available\")." >&2
        echo "         Create $dir/OpenD.xml using opend_configs/$label_lc/OpenD.xml as the" >&2
        echo "         field reference, then add your real login_pwd (never stored in git)" >&2
        echo "         and a real log_path for this machine. See the SETUP note at the top" >&2
        echo "         of this script." >&2
        return 1
    fi

    chmod +x "$exe"
    # Launch from inside $dir (not the caller's cwd) so OpenD finds OpenD.xml next to
    # its binary, and pass -cfg_file explicitly so the config location is never ambiguous.
    (
        cd "$dir" || exit 1
        exec "$exe" -cfg_file "$dir/OpenD.xml"
    ) &
    local pid=$!
    echo "  $label PID: $pid"
    echo $pid > "$dir/.opend_${label_lc}.pid"

    echo "  Waiting for $label to log in (up to 30s)..."
    if wait_for_log "$dir" "Login success|Qot authority|login success" 30; then
        echo "  $label: Logged in successfully!"
        return 0
    else
        # Check if port is at least open (might have logged in without matching log pattern)
        if port_open "$api_port"; then
            echo "  $label: Port $api_port is open (likely logged in)"
            return 0
        else
            echo "  WARNING: $label login status unknown. Check logs: $dir/Log/" >&2
            return 1
        fi
    fi
}

# ── Launch AU ────────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  Moomoo OpenD Launcher (macOS)"
echo "  AU dir: $AU_DIR"
if ! $SKIP_SG; then
    echo "  SG dir: $SG_DIR"
fi
echo "══════════════════════════════════════════════════"
echo ""

AU_OK=false
start_opend_instance "AU" "$AU_EXE" "$AU_DIR" "$AU_API_PORT" "$AU_DIR" && AU_OK=true

# ── Launch SG ────────────────────────────────────────────────────────────────────
SG_OK=true
if ! $SKIP_SG; then
    start_opend_instance "SG" "$SG_EXE" "$SG_DIR" "$SG_API_PORT" "$SG_DIR" && SG_OK=true || SG_OK=false
fi

# ── Summary ───────────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
if $AU_OK; then
    echo "  AU: port $AU_API_PORT — READY"
else
    echo "  AU: FAILED (check $AU_DIR/Log/)"
fi
if ! $SKIP_SG; then
    if $SG_OK; then
        echo "  SG: port $SG_API_PORT — READY"
    else
        echo "  SG: FAILED (check $SG_DIR/Log/)"
    fi
fi
echo "══════════════════════════════════════════════════"
echo ""

if ! $AU_OK; then
    exit 1
fi
exit 0
