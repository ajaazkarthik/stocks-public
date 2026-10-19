#!/usr/bin/env bash
# start_scheduler.sh
# Launch the AjaazAlert Report Scheduler bot (@AjaazAlertBot)
#
# Usage:
#   ./start_scheduler.sh                     # morning (08:30) + evening (17:30)
#   ./start_scheduler.sh --every-2-hours     # run every 2 hours
#   ./start_scheduler.sh --morning 09:00 --evening 18:00  # custom times
#   ./start_scheduler.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

EVERY_2_HOURS=false
MORNING_TIME=""
EVENING_TIME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --every-2-hours) EVERY_2_HOURS=true; shift ;;
        --morning)       MORNING_TIME="$2"; shift 2 ;;
        --evening)       EVENING_TIME="$2"; shift 2 ;;
        --help|-h)
            echo ""
            echo "AjaazAlert Report Scheduler — @AjaazAlertBot"
            echo ""
            echo "  ./start_scheduler.sh                          # default (08:30 + 17:30 IST)"
            echo "  ./start_scheduler.sh --every-2-hours          # every 2 hours"
            echo "  ./start_scheduler.sh --morning 09:00 --evening 18:30  # custom times"
            echo ""
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Activate venv (Mac/Linux path)
if [[ -f ".venv/bin/activate" ]]; then
    source ".venv/bin/activate"
fi

# Load env vars (scheduler_env.sh sources scheduler_env.local.sh when present)
if [[ -f "./scheduler_env.sh" ]]; then
    source ./scheduler_env.sh
else
    echo "ERROR: scheduler_env.sh not found. Copy from scheduler_env.local.sh.example and update." >&2
    exit 1
fi

# Apply overrides
if $EVERY_2_HOURS; then
    export SCHEDULER_EVERY_HOURS="2"
    echo "Mode: every 2 hours"
else
    unset SCHEDULER_EVERY_HOURS 2>/dev/null || true
fi

if [[ -n "$MORNING_TIME" ]]; then
    export SCHEDULER_MORNING_IST="$MORNING_TIME"
    echo "Morning run overridden to: $MORNING_TIME IST"
fi
if [[ -n "$EVENING_TIME" ]]; then
    export SCHEDULER_EVENING_IST="$EVENING_TIME"
    echo "Evening run overridden to: $EVENING_TIME IST"
fi

echo ""
echo "Starting AjaazAlert Scheduler..."
echo "Press Ctrl+C to stop."
echo ""

python report_scheduler.py
