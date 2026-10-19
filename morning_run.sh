#!/usr/bin/env bash
# morning_run.sh — Scheduled Morning Run for Stocks Trading System
#
# SYNOPSIS
#   Runs the complete morning analysis pipeline: dashboards, screeners, signals.
#   Designed for macOS launchd or cron (daily at 08:00 local time).
#
# DESCRIPTION
#   Executes in order:
#     1. Global Portfolio Dashboard (all 6 brokers)
#     2. India Portfolio Dashboard (Zerodha + AngelOne)
#     3. Morning Screener (all markets)
#     4. Signal Stack (process any pending fires)
#     5. Auto-Validation (weekly quality check, Mondays only)
#
# USAGE
#   ./morning_run.sh                  # run full pipeline
#   ./morning_run.sh --skip-india     # skip India dashboards
#   ./morning_run.sh --skip-screener  # skip morning screener
#   ./morning_run.sh --skip-validation
#   ./morning_run.sh --dry-run        # print without executing
#
# LAUNCHD SETUP (macOS)
#   See docs/launchd_morning_run.plist for the macOS launchd configuration.

set -uo pipefail  # -e removed so individual script failures don't abort the whole run

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Configuration ──────────────────────────────────────────────────────────────
SKIP_INDIA=false
SKIP_SCREENER=false
SKIP_VALIDATION=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-india)      SKIP_INDIA=true; shift ;;
        --skip-screener)   SKIP_SCREENER=true; shift ;;
        --skip-validation) SKIP_VALIDATION=true; shift ;;
        --dry-run)         DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Detect Python: prefer .venv/bin/python, fallback to system python3
if [[ -f "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    PYTHON="python"
fi

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_DATE="$(date +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/morning_run_${LOG_DATE}.log"

# ── Logging ────────────────────────────────────────────────────────────────────
log() {
    local level="${2:-INFO}"
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    local entry="[$timestamp] [$level] $1"
    echo "$entry"
    echo "$entry" >> "$LOG_FILE"
}

# ── Step runner ────────────────────────────────────────────────────────────────
run_step() {
    local name="$1"
    local script="$2"
    shift 2
    local args=("$@")

    log "Starting: $name"
    local start_time
    start_time=$(date +%s)

    if $DRY_RUN; then
        log "[DRY RUN] Would execute: $PYTHON $script ${args[*]:-}" "DRY"
        echo "true"
        return 0
    fi

    local full_path="$SCRIPT_DIR/$script"
    if [[ ! -f "$full_path" ]]; then
        log "SKIP: $script not found at $full_path" "WARN"
        echo "false"
        return 0
    fi

    local stdout_log="$LOG_DIR/${name}_stdout.log"
    local stderr_log="$LOG_DIR/${name}_stderr.log"

    "$PYTHON" "$full_path" "${args[@]:-}" \
        >"$stdout_log" 2>"$stderr_log"
    local exit_code=$?

    local end_time
    end_time=$(date +%s)
    local elapsed=$(( end_time - start_time ))
    local mm=$(( elapsed / 60 ))
    local ss=$(( elapsed % 60 ))
    local elapsed_str
    printf -v elapsed_str "%02d:%02d" "$mm" "$ss"

    if [[ $exit_code -eq 0 ]]; then
        log "DONE: $name (${elapsed_str})" "OK"
        echo "true"
    else
        log "FAIL: $name exited with code $exit_code (${elapsed_str})" "ERROR"
        echo "false"
    fi
}

# ── Environment Setup ──────────────────────────────────────────────────────────
log "$(printf '=%.0s' {1..60})"
log "Morning Run Starting"
log "ROOT: $SCRIPT_DIR"
log "Python: $PYTHON"
log "$(printf '=%.0s' {1..60})"

export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION="python"
export OPENBLAS_NUM_THREADS="1"

# ── Step 1: Global Portfolio Dashboard ─────────────────────────────────────────
RESULT_GLOBAL=$(run_step "global_dashboard" "global_portfolio_dashboard.py" "--morning")

# ── Step 2: India Portfolio Dashboard ──────────────────────────────────────────
if ! $SKIP_INDIA; then
    RESULT_INDIA=$(run_step "india_dashboard" "india-brokers/india_portfolio_dashboard.py" "--morning")
else
    log "SKIP: India Dashboard (--skip-india)" "SKIP"
    RESULT_INDIA="SKIPPED"
fi

# ── Step 3: Morning Screener ───────────────────────────────────────────────────
if ! $SKIP_SCREENER; then
    RESULT_SCREENER=$(run_step "morning_screener" "tradingview/screeners/morning_screener.py" "--morning")
else
    log "SKIP: Morning Screener (--skip-screener)" "SKIP"
    RESULT_SCREENER="SKIPPED"
fi

# ── Step 4: Signal Stack (process pending fires) ──────────────────────────────
RESULT_SIGNALS=$(run_step "signal_stack" "tradingview/signal_stack.py" "--morning")

# ── Step 5: Auto-Validation (Mondays only) ────────────────────────────────────
if ! $SKIP_VALIDATION; then
    DAY_OF_WEEK=$(date +%u)  # 1=Mon, 7=Sun
    if [[ "$DAY_OF_WEEK" -eq 1 ]] || $DRY_RUN; then
        RESULT_VALIDATION=$(run_step "auto_validation" "tradingview/analysis/auto_validation.py")
    else
        log "SKIP: Auto-Validation (not Monday)" "SKIP"
        RESULT_VALIDATION="SKIPPED"
    fi
else
    log "SKIP: Auto-Validation (--skip-validation)" "SKIP"
    RESULT_VALIDATION="SKIPPED"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
log "$(printf '=%.0s' {1..60})"
log "Morning Run Complete"

PASS=0; FAIL=0; SKIP=0

check_result() {
    local step_name="$1"
    local result="$2"
    if [[ "$result" == "true" ]]; then
        (( PASS++ )) || true
        log "  PASS: $step_name"
    elif [[ "$result" == "SKIPPED" ]]; then
        (( SKIP++ )) || true
        log "  SKIP: $step_name (SKIPPED)"
    else
        (( FAIL++ )) || true
        log "  FAIL: $step_name" "ERROR"
    fi
}

check_result "Global Dashboard" "$RESULT_GLOBAL"
check_result "India Dashboard"  "$RESULT_INDIA"
check_result "Morning Screener" "$RESULT_SCREENER"
check_result "Signal Stack"     "$RESULT_SIGNALS"
check_result "Auto Validation"  "$RESULT_VALIDATION"

log "Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"
log "Log: $LOG_FILE"
log "$(printf '=%.0s' {1..60})"

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
