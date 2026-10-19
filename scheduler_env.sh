#!/usr/bin/env bash
# AjaazAlert Report Scheduler — Environment Variables (TRACKED TEMPLATE)
# Bot: @AjaazAlertBot
#
# ⚠️  This file is committed to Git. Do NOT put real credentials here.
#
# Real values live in the ignored local file `scheduler_env.local.sh`, which is
# sourced automatically below when present. Create it once with:
#
#   cp scheduler_env.local.sh.example scheduler_env.local.sh
#   chmod 600 scheduler_env.local.sh
#   # then fill in the real values
#
# Non-secret scheduling configuration may stay in this tracked file.

# ── Schedule (IST, 24h format) ─────────────────────────────────────────────
export SCHEDULER_MORNING_IST="08:30"   # morning run
export SCHEDULER_EVENING_IST="17:30"   # evening run
export SCHEDULER_ZERODHA_WARN="08:15"  # warn before morning run

# ── Interval mode: run every N hours ───────────────────────────────────────
export SCHEDULER_EVERY_HOURS="2"

# ── Days to run (1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat, 7=Sun) ─────────
export SCHEDULER_DAYS="1,2,3,4,5"   # Mon–Fri

# ── Timezones ──────────────────────────────────────────────────────────────
# The weekday calendar is evaluated in this zone; user-facing times are rendered
# in the display zone with its real label/offset.
export SCHEDULER_TIMEZONE="Asia/Kolkata"
export SCHEDULER_DISPLAY_TIMEZONE="Australia/Sydney"

# ── Thresholds (separate policies) ─────────────────────────────────────────
export SCHEDULER_STOCK_THRESHOLD="62.5"
export SCHEDULER_SCORE_THRESHOLD="70"

# ── Delivery policy ────────────────────────────────────────────────────────
export SCHEDULER_ALERT_TTL_HOURS="12"
export SCHEDULER_MAX_ATTEMPTS="5"

# ── Load local secrets if present (ignored by Git) ─────────────────────────
_local_secrets="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scheduler_env.local.sh"
if [[ -f "$_local_secrets" ]]; then
    # shellcheck disable=SC1090
    source "$_local_secrets"
fi
