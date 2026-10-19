#!/usr/bin/env bash
# ============================================================
#  Stocks Trading Bot — Daily Start Script
#  Run this every morning to launch the Telegram bot.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Load saved config if it exists ─────────────────────────
ENV_FILE="$SCRIPT_DIR/telegram_env.sh"
if [[ -f "$ENV_FILE" ]]; then
    source "$ENV_FILE"
else
    echo "ERROR: telegram_env.sh not found." >&2
    echo "Run setup_telegram.sh first to configure the bot." >&2
    exit 1
fi

# ── Validate token is set ───────────────────────────────────
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN is not set in telegram_env.sh" >&2
    exit 1
fi

# ── Quick connectivity check ────────────────────────────────
echo ""
echo "============================================================"
echo "  Stocks Trading Bot — @Ajaaz_bot"
echo "============================================================"
echo ""

if curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" -o /dev/null 2>&1; then
    BOT_INFO=$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null || true)
    echo "  Bot:     Connected to Telegram API"
else
    echo "  WARNING: Could not reach Telegram API — offline mode"
fi

ALLOWED="${TELEGRAM_ALLOWED_USERS:-}"
if [[ -n "$ALLOWED" ]]; then
    echo "  Auth:    Restricted to user ID(s): $ALLOWED"
else
    echo "  Auth:    OPEN (no whitelist — run setup_telegram.sh)"
fi

echo ""
echo "  Press Ctrl+C to stop the bot."
echo "  → Now open Telegram and message @Ajaaz_bot"
echo ""

# ── Activate venv ───────────────────────────────────────────
if [[ -f ".venv/bin/activate" ]]; then
    source ".venv/bin/activate"
fi

# ── Create logs directory ───────────────────────────────────
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/telegram_bot.log"
ERR_FILE="$LOG_DIR/telegram_bot_err.log"
PID_FILE="$SCRIPT_DIR/.telegram_bot.pid"

# ── Launch the bot as background process ────────────────────
echo "  Starting bot in background..."
nohup python "$SCRIPT_DIR/telegram_bot.py" \
    >"$LOG_FILE" 2>"$ERR_FILE" &
BOT_PID=$!

echo $BOT_PID > "$PID_FILE"
echo "  Bot started in background (PID $BOT_PID)"
echo "  Logs: $LOG_FILE"
echo "  Stop: kill \$(cat $PID_FILE)"
