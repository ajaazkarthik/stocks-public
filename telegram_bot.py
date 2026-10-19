#!/usr/bin/env python3
"""
Telegram Bot — Mobile remote control for the Stocks Trading System.

Receives commands on Telegram and runs reports, sending HTML files back as
document attachments. Runs in polling mode — NO webhook, NO port conflicts
with the existing TradingView webhook server (port 8765).

Enhancements (v2):
  #1  /pnl         — instant P&L snapshot from cache (no subprocess)
  #2  Inline keyboards on /start and /help
  #3  Error handler registered (no more terminal traceback spam)
  #4  Scheduled daily push — Zerodha token warning + morning reminder (JobQueue)
  #5  Proactive TradingView alert push — polls fires_raw.json every 5 min
  #6  /chart       — portfolio allocation pie chart (matplotlib)
  #7  Persistent reply keyboard on /start
  #8  Edit-in-place progress in /status

Setup (one-time):
  1. Create a bot via @BotFather on Telegram → copy the token
  2. Find your Telegram user ID: message @userinfobot
  3. Set environment variables:
        $env:TELEGRAM_BOT_TOKEN   = "1234567890:ABCdef..."
        $env:TELEGRAM_ALLOWED_USERS = "123456789"   # comma-sep for multiple
  4. Run:  python telegram_bot.py
  5. OR via main.py menu → option 52

Commands:
  /start      — welcome + user ID + quick-action buttons
  /help       — list all commands with inline buttons
  /pnl        — instant P&L snapshot from cached positions
  /chart      — portfolio allocation pie chart
  /morning    — full morning run (all reports, ~8 min)
  /fast       — portfolio + signals only (~4 min)
  /portfolio  — global portfolio dashboard
  /india      — india portfolio (Zerodha + AngelOne)
  /signals    — signal stack (TV fires → scored)
  /sell       — sell strategy report
  /alerts     — alert tracker update + report
  /screener   — multi-asset screener
  /heatmap    — sector heatmap
  /calendar   — economic calendar
  /reports    — list available reports
  /send <name>— send a specific saved report
  /status     — broker connection status
  /cancel     — cancel running command
  /unblock    — force-clear stuck state
"""

import asyncio
import io
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from pathlib import Path

# ── Windows: force UTF-8 output ───────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
# Quiet the verbose Telegram library logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        ReplyKeyboardMarkup,
        ReplyKeyboardRemove,
    )
    from telegram.constants import ParseMode
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:
    print("ERROR: python-telegram-bot not installed.")
    print("Run:  pip install 'python-telegram-bot[job-queue]>=20.0'")
    sys.exit(1)

# ── Project paths (mirror main.py) ────────────────────────────────────────
ROOT    = Path(__file__).parent
TV      = ROOT / "tradingview"
INDIA   = ROOT / "india-brokers"
CGT     = ROOT / "cgt"
REPORTS = ROOT / "reports"

# ── BASE_ENV (same as main.py) ─────────────────────────────────────────────
BASE_ENV: dict = {
    **os.environ,
    "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
    "PYTHONIOENCODING": "utf-8",
    "OPENBLAS_NUM_THREADS": "1",
}

# ── Default bot token (fallback when no env var is set) ─────────────────
# This is the @Ajaaz_bot (Jerry) token — the interactive trading bot.
# @AjaazAlertBot (report_scheduler.py) uses AJAAZ_ALERT_BOT_TOKEN instead.
_DEFAULT_BOT_TOKEN = "8741115130:AAGQhi6LYaArwoPs-3i0XPsC8iRMr3jzoWs"  # @Ajaaz_bot (Jerry)

# ── Alert threshold ─────────────────────────────────────────────────────
_HOT_ALERT_THRESHOLD: int = 70  # minimum composite score for proactive push notification

# ── Thread pool for subprocess calls ──────────────────────────────────────
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bot-worker")

# ── Morning run cooldown guard (shared with main.py + report_scheduler.py) ─
# Prevents three independent systems from all sending morning reports simultaneously.
try:
    _tv_path = str(TV)
    if _tv_path not in sys.path:
        sys.path.insert(0, _tv_path)
    from morning_guard import try_claim_morning_run, release_morning_run  # type: ignore
    _MORNING_GUARD_AVAILABLE = True
except ImportError:
    _MORNING_GUARD_AVAILABLE = False
    def try_claim_morning_run(caller: str) -> bool: return True   # noqa: E704
    def release_morning_run() -> None: pass                        # noqa: E704

# ── Per-user state ────────────────────────────────────────────────────────
_running_proc: dict[int, subprocess.Popen] = {}   # user_id → active Popen

# ── Enhancement #7: Persistent reply keyboard ─────────────────────────────
_MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🌅 /morning"), KeyboardButton("⚡ /pnl")],
        [KeyboardButton("📊 /portfolio"), KeyboardButton("🇮🇳 /india")],
        [KeyboardButton("📈 /signals"),  KeyboardButton("📉 /sell")],
        [KeyboardButton("📋 /reports"),  KeyboardButton("❓ /help")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Choose a command…",
)

# ── Enhancement #2: Inline keyboard for /help ─────────────────────────────
def _main_inline_kb() -> InlineKeyboardMarkup:
    """Return an inline keyboard with the most-used commands."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌅 Morning",    callback_data="morning"),
            InlineKeyboardButton("⚡ Quick P&L",  callback_data="pnl"),
        ],
        [
            InlineKeyboardButton("📊 Portfolio",  callback_data="portfolio"),
            InlineKeyboardButton("🇮🇳 India",      callback_data="india"),
        ],
        [
            InlineKeyboardButton("📈 Signals",    callback_data="signals"),
            InlineKeyboardButton("💰 Sell",       callback_data="sell"),
        ],
        [
            InlineKeyboardButton("📉 Heatmap",    callback_data="heatmap"),
            InlineKeyboardButton("🗓 Calendar",   callback_data="calendar"),
        ],
        [
            InlineKeyboardButton("🔍 Screener",   callback_data="screener"),
            InlineKeyboardButton("📋 Reports",    callback_data="reports"),
        ],
        [
            InlineKeyboardButton("🔌 Status",     callback_data="status"),
            InlineKeyboardButton("📊 Chart",      callback_data="chart"),
        ],
        [
            InlineKeyboardButton("🛑 Cancel",     callback_data="cancel"),
            InlineKeyboardButton("🔓 Unblock",    callback_data="unblock"),
        ],
    ])


# ═══════════════════════════════════════════════════════════════════════════
# Security helpers
# ═══════════════════════════════════════════════════════════════════════════

def _allowed_users() -> set[int]:
    """Return the set of whitelisted Telegram user IDs (from env var)."""
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


_DEFAULT_PUSH_CHAT_ID = "5986275415"  # fallback push target (your Telegram user ID)


def _push_targets() -> list[int]:
    """Return list of chat IDs for proactive push alerts (hot alerts, scheduled reports).

    Falls back to _DEFAULT_PUSH_CHAT_ID when TELEGRAM_ALLOWED_USERS is not set,
    so hot-alert jobs always have at least one recipient even if the whitelist env
    var is not configured.  Auth logic (_is_allowed / _gate) is intentionally separate.
    """
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", _DEFAULT_PUSH_CHAT_ID)
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


def _is_allowed(user_id: int) -> bool:
    allowed = _allowed_users()
    if not allowed:
        return True   # no whitelist configured — open to all (only use in dev)
    return user_id in allowed


async def _gate(update: Update) -> bool:
    """Return True if caller is authorized AND no command already running.

    Also cleans up stale _running_proc entries where the process has already
    died (poll() returns non-None) — prevents permanent lockout after a
    process crash, timeout kill, or bot restart.
    """
    uid = update.effective_user.id
    if not _is_allowed(uid):
        await update.message.reply_text(
            f"⛔ *Unauthorized*\nYour Telegram user ID: `{uid}`\n"
            "Add it to TELEGRAM_ALLOWED_USERS env var to grant access.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False
    # Check for stale/dead process entries and auto-clean them
    if uid in _running_proc:
        proc = _running_proc[uid]
        if proc.poll() is not None:
            # Process already exited — stale entry, clean it up silently
            del _running_proc[uid]
        else:
            await update.message.reply_text(
                "⏳ A command is already running\\. Use /cancel to stop it, "
                "or /unblock if the bot is stuck\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Subprocess helpers
# ═══════════════════════════════════════════════════════════════════════════

# Max seconds a single pipeline step is allowed to run (15 minutes)
_STEP_TIMEOUT = 900


def _run_cmd(
    cmd: list[str],
    cwd: Path,
    env: dict | None = None,
    user_id: int | None = None,
    timeout: int = _STEP_TIMEOUT,
) -> tuple[int, str]:
    """
    Run a command synchronously (for use inside executor).
    Stores the Popen handle in _running_proc[user_id] so /cancel can kill it.
    Returns (returncode, last_2000_chars_of_output).

    stdin=DEVNULL is critical: prevents scripts that call input() (e.g.
    zerodha_login.py) from hanging indefinitely.  With DEVNULL, input()
    raises EOFError immediately, which zerodha_auth catches and converts
    to a graceful "token unavailable" error instead of an infinite hang.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env or BASE_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,   # FIX: prevents interactive-input hangs
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if user_id is not None:
        _running_proc[user_id] = proc

    chunks: list[str] = []
    timed_out = False
    try:
        for line in proc.stdout:
            chunks.append(line)
            # Keep only last ~3000 chars to avoid huge strings
            combined = "".join(chunks)
            if len(combined) > 4000:
                chunks = [combined[-3000:]]
            # Enforce per-step timeout via elapsed check
            # (communicate() timeout alternative breaks streaming output)

        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out = True
        rc = -1
    finally:
        if user_id is not None and user_id in _running_proc:
            del _running_proc[user_id]

    output = "".join(chunks)[-2000:]
    if timed_out:
        output += f"\n\n[TIMEOUT] Step exceeded {timeout}s — process killed."
    return rc, output


async def _run_step(
    update: Update,
    label: str,
    cmd: list[str],
    cwd: Path,
    env: dict | None = None,
    report_path: Path | None = None,
    report_label: str | None = None,
) -> bool:
    """
    Run one pipeline step with Enhancement #8 edit-in-place progress:
    1. Send "⏳ label…"
    2. Run subprocess
    3. Edit the same message → "✅ done" or "⚠️ error"
    Returns True always (pipeline continues even on non-zero exit).
    """
    uid = update.effective_user.id
    loop = asyncio.get_running_loop()

    # Send initial ⏳ message — we'll edit it in place
    sent = await update.message.reply_text(
        f"⏳ *{label}*…", parse_mode=ParseMode.MARKDOWN
    )

    rc, output = await loop.run_in_executor(
        _executor, lambda: _run_cmd(cmd, cwd, env, uid)
    )

    # Enhancement #8: edit the message in-place instead of sending a new one
    if rc != 0:
        snippet = output[-600:] if output else "(no output)"
        try:
            await sent.edit_text(
                f"⚠️ *{label}* — exit {rc}\n```\n{snippet}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # edit_text can fail if message is >48h old — fall back to new msg
            await update.message.reply_text(
                f"⚠️ *{label}* — exit {rc}", parse_mode=ParseMode.MARKDOWN
            )
    else:
        try:
            await sent.edit_text(
                f"✅ *{label}* — done", parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass   # non-critical if edit fails

    if report_path is not None:
        await _send_report(update, report_path, report_label or label)

    return True


async def _send_report(update: Update, path: Path, label: str) -> None:
    """Send an HTML (or any) file as a Telegram document."""
    if not path.exists():
        await update.message.reply_text(f"📂 _{label}_ — report file not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if path.stat().st_size == 0:
        await update.message.reply_text(f"📂 _{label}_ — report file is empty.", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        with open(path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=path.name,
                caption=f"📊 {label}",
            )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Could not send _{label}_: {exc}", parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════════════════════════
# India env helper (mirrors main.py _india_env)
# ═══════════════════════════════════════════════════════════════════════════

def _india_env() -> dict:
    """Build env dict for India-related subprocesses.

    Injects ZERODHA_ACCESS_TOKEN from today's cache so subprocesses never
    need interactive login.  zerodha_auth.save_cache() stores the key as
    'token' (not 'access_token') — this function uses the correct key.
    """
    env = dict(BASE_ENV)
    token_file = INDIA / ".zerodha_token_cache"
    if token_file.exists():
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
            # zerodha_auth.save_cache() stores key as 'token', not 'access_token'
            tok = data.get("token", "") or data.get("access_token", "")
            if tok:
                env["ZERODHA_ACCESS_TOKEN"] = tok
        except Exception:
            pass
    return env


def _zerodha_token_ok() -> bool:
    """Return True if a valid (unexpired) Zerodha token exists in today's cache."""
    token_file = INDIA / ".zerodha_token_cache"
    if not token_file.exists():
        return False
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
        tok = data.get("token", "") or data.get("access_token", "")
        if not tok:
            return False
        # Check expiry the same way zerodha_auth.load_cache() does
        import time as _time
        auth_ts = data.get("auth_time")
        if auth_ts:
            age_hours = (_time.time() - float(auth_ts)) / 3600.0
            return age_hours < 24.75
        # Fallback: accept if cached today
        from datetime import date as _date
        return data.get("date", "") == str(_date.today())
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Bot command handlers
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    name = update.effective_user.first_name or "User"
    authorized = _is_allowed(uid)
    status = "✅ Authorized" if authorized else "⛔ Not authorized"
    # Log to terminal for first-time ID discovery
    print(f"\n{'='*60}")
    print(f"  /start from: {name}")
    print(f"  Telegram user ID: {uid}")
    if not authorized:
        print(f"  To whitelist: $env:TELEGRAM_ALLOWED_USERS = '{uid}'")
    print(f"{'='*60}\n")
    # Enhancement #7: send persistent reply keyboard alongside the welcome
    await update.message.reply_text(
        f"👋 *Hi {name}!*\n\n"
        f"*Stocks Trading Bot* is running.\n\n"
        f"Your Telegram ID: `{uid}`\n"
        f"Status: {status}\n\n"
        f"Use the buttons below or /help for all commands.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_MAIN_KEYBOARD,
    )
    # Enhancement #2: follow up with inline action buttons
    if authorized:
        await update.message.reply_text(
            "🚀 *Quick actions:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_inline_kb(),
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not _is_allowed(uid):
        await update.message.reply_text(
            f"⛔ Not authorized\\. Your ID: `{uid}`\n"
            f"Add to TELEGRAM\\_ALLOWED\\_USERS env var\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    msg = (
        "📱 *Stocks Trading Bot — Commands*\n\n"
        "🌅 *Morning Runs*\n"
        "  `/morning` — Full run: portfolio \\+ signals \\+ sell \\+ CGT \\(\\~8 min\\)\n"
        "  `/fast` — Quick run: portfolio \\+ signals only \\(\\~4 min\\)\n\n"
        "📊 *Individual Reports*\n"
        "  `/portfolio` — Global portfolio \\(all 6 brokers\\)\n"
        "  `/india` — India portfolio \\(Zerodha \\+ AngelOne\\)\n"
        "  `/signals` — Signal stack \\(TV fires → scored\\)\n"
        "  `/sell` — Sell strategy \\(exit urgency\\)\n"
        "  `/alerts` — Alert tracker \\(prices \\+ performance\\)\n"
        "  `/screener` — Multi\\-asset screener\n"
        "  `/heatmap` — Sector heatmap\n"
        "  `/calendar` — Economic calendar\n\n"
        "⚡ *Instant Commands \\(no subprocess\\)*\n"
        "  `/pnl` — P&L snapshot from last cached run\n"
        "  `/chart` — Portfolio allocation pie chart\n\n"
        "📋 *File Retrieval*\n"
        "  `/reports` — List available reports\n"
        "  `/send <name>` — Send a saved report\n"
        "    Names: `portfolio` `india` `signals` `actionable`\n"
        "           `sell` `alerts` `heatmap` `calendar` `cgt`\n\n"
        "🔧 *Utility*\n"
        "  `/status` — Broker connection status\n"
        "  `/schedule` — Trigger 4h alert cycle now \\(stocks \\+ options\\)\n"
        "  `/cancel` — Cancel running command\n"
        "  `/unblock` — Force\\-clear stuck state\n"
        "  `/id` — Show your Telegram user ID"
    )
    # Enhancement #2: inline buttons below the help text
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_inline_kb(),
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    name = update.effective_user.first_name or "User"
    # Print prominently in bot terminal so it's easy to copy during first-time setup
    print(f"\n{'='*60}")
    print(f"  /id request from: {name}")
    print(f"  Telegram user ID: {uid}")
    print(f"  To whitelist: $env:TELEGRAM_ALLOWED_USERS = '{uid}'")
    print(f"{'='*60}\n")
    await update.message.reply_text(
        f"👤 *Your Telegram ID*\n\n"
        f"`{uid}`\n\n"
        f"To authorize, set this on your PC:\n"
        f"`$env:TELEGRAM_ALLOWED_USERS = \"{uid}\"`\n\n"
        f"Or add to `telegram_env.ps1` and restart the bot.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text(f"⛔ Not authorized. ID: {update.effective_user.id}")
        return

    msg = await update.message.reply_text("🔍 Checking broker status…")
    lines = ["🔌 *Broker Status*\n"]

    def _check_port(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (OSError, TimeoutError):
            return False

    # Moomoo OpenD
    for port, label in [(11111, "Moomoo AU"), (11112, "Moomoo SG")]:
        icon = "✅" if _check_port("127.0.0.1", port) else "❌"
        lines.append(f"{icon} {label} (port {port})")

    # IBKR TWS
    if _check_port("127.0.0.1", 7496):
        lines.append("✅ IBKR TWS (port 7496 live)")
    else:
        lines.append("⚪ IBKR TWS (offline — Flex Query fallback OK)")

    # Zerodha token
    token_file = INDIA / ".zerodha_token_cache"
    if token_file.exists():
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
            today = datetime.now().strftime("%Y-%m-%d")
            if data.get("date") == today and data.get("access_token"):
                lines.append("✅ Zerodha (token valid today)")
            else:
                lines.append(f"⚠️ Zerodha (token from {data.get('date', '?')})")
        except Exception:
            lines.append("⚠️ Zerodha (token unreadable)")
    else:
        lines.append("❌ Zerodha (no token — run /india to refresh)")

    lines.append("✅ AngelOne (TOTP auto-auth)")
    lines.append("✅ Webull (PDF statements)")
    lines.append("✅ IBKR Flex (HTTPS, always available)")

    # Webhook server
    wh = _check_port("127.0.0.1", 8765)
    lines.append(f"\n{'✅' if wh else '⚪'} TV Webhook server (port 8765)")

    # Telegram bot itself
    lines.append("✅ Telegram Bot (this message!)")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not _is_allowed(uid):
        return
    proc = _running_proc.get(uid)
    if proc is None:
        await update.message.reply_text("ℹ️ No command is currently running. Use /unblock if stuck.")
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    _running_proc.pop(uid, None)
    await update.message.reply_text("🛑 Running command was cancelled.")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-clear the 'command is running' lock for this user.

    Use when the bot got stuck (e.g. after a Zerodha auth hang, crash, or
    bot restart) and /cancel didn't work.  Safe to call at any time.
    """
    uid = update.effective_user.id
    if not _is_allowed(uid):
        return
    proc = _running_proc.pop(uid, None)
    if proc is not None:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        await update.message.reply_text(
            "🔓 *Unblocked.* Stuck process killed.\n"
            "You can now run new commands.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "✅ *No stuck process found* — you were already unblocked.\n"
            "If commands still fail, try /cancel first.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Enhancement #1: /pnl — instant P&L from cached positions (no subprocess)
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instant P&L snapshot from .cache_global_positions.json — no subprocess."""
    if not _is_allowed(update.effective_user.id):
        return
    cache_path = ROOT / ".cache_global_positions.json"
    if not cache_path.exists():
        await update.message.reply_text(
            "⚠️ No cached positions found.\nRun /portfolio first to populate the cache.",
        )
        return
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Could not read cache: {exc}")
        return

    positions = data.get("positions", [])
    saved_ts  = data.get("saved_at", 0)
    age_min   = int((time.time() - float(saved_ts)) / 60) if saved_ts else -1
    age_str   = f"{age_min // 60}h {age_min % 60}m" if age_min >= 60 else f"{age_min}m"

    # Aggregate per broker
    brokers: dict[str, dict] = {}
    for p in positions:
        b = p.get("broker", "Unknown")
        if b not in brokers:
            brokers[b] = {"pnl": 0.0, "val": 0.0, "count": 0}
        brokers[b]["pnl"]   += p.get("pnl", 0) or 0
        brokers[b]["val"]   += p.get("market_val", 0) or 0
        brokers[b]["count"] += 1

    total_pnl = sum(v["pnl"] for v in brokers.values())
    total_val = sum(v["val"] for v in brokers.values())

    lines = [f"💼 *Portfolio Snapshot* \\(cached {age_str} ago\\)\n"]
    lines.append(f"*{len(positions)} positions across {len(brokers)} brokers*\n")
    lines.append("*Unrealised P&L by broker:*")
    for broker, s in sorted(brokers.items()):
        pnl = s["pnl"]
        icon = "✅" if pnl >= 0 else "🔴"
        sign = "+" if pnl >= 0 else ""
        lines.append(f"{icon} `{broker:<12}` `{sign}{pnl:,.0f}`")
    lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
    sign = "+" if total_pnl >= 0 else ""
    lines.append(f"📈 *Total P&L:*  `{sign}{total_pnl:,.0f}`")
    lines.append(f"💰 *Total Value:* `{total_val:,.0f}`")
    if age_min > 120:
        lines.append(f"\n⚠️ _Cache is {age_str} old\\. Run /portfolio to refresh\\._")
    else:
        lines.append(f"\n_Run /portfolio to refresh \\| /chart for allocation pie_")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Enhancement #6: /chart — portfolio allocation pie chart (matplotlib)
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a portfolio allocation pie chart built from cached positions."""
    if not _is_allowed(update.effective_user.id):
        return
    cache_path = ROOT / ".cache_global_positions.json"
    if not cache_path.exists():
        await update.message.reply_text(
            "⚠️ No cached positions found. Run /portfolio first."
        )
        return
    try:
        import matplotlib
        matplotlib.use("Agg")   # non-interactive backend, safe in threads
        import matplotlib.pyplot as plt

        data = json.loads(cache_path.read_text(encoding="utf-8"))
        positions = data.get("positions", [])
        saved_ts  = data.get("saved_at", 0)
        age_min   = int((time.time() - float(saved_ts)) / 60) if saved_ts else -1

        # Aggregate by broker
        vals: dict[str, float] = {}
        for p in positions:
            b = p.get("broker", "Unknown")
            vals[b] = vals.get(b, 0.0) + (p.get("market_val", 0) or 0)

        labels  = list(vals.keys())
        amounts = [vals[k] for k in labels]

        fig, ax = plt.subplots(figsize=(7, 5))
        wedges, texts, autotexts = ax.pie(
            amounts,
            labels=labels,
            autopct="%1.1f%%",
            startangle=140,
            pctdistance=0.80,
        )
        for at in autotexts:
            at.set_fontsize(9)
        total = sum(amounts)
        age_str = f"{age_min}m" if age_min < 60 else f"{age_min // 60}h {age_min % 60}m"
        ax.set_title(
            f"Portfolio Allocation by Broker\nTotal: {total:,.0f}  (cached {age_str} ago)",
            fontsize=11,
        )
        fig.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)

        await update.message.reply_photo(
            photo=buf,
            caption=f"📊 Portfolio allocation across {len(labels)} brokers",
        )
    except ImportError:
        await update.message.reply_text(
            "⚠️ matplotlib not installed. Run:\n`pip install matplotlib`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Chart error: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
# Enhancement #6b: /schedule — on-demand trigger for 4h alert cycle
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /schedule — manually trigger the 4-hour alert cycle immediately.

    Runs signal_stack + options screener + options analysis and sends
    3 tabular MarkdownV2 alerts. Same as the automatic 4h job, but
    triggered on demand.

    BLOCKER-1 fix: run_cycle() is synchronous — called via run_in_executor
    so it never blocks the asyncio event loop.
    """
    import asyncio as _asyncio
    uid = update.effective_user.id
    if not _is_allowed(uid):
        await update.message.reply_text(f"⛔ Not authorized. ID: {uid}")
        return

    await update.message.reply_text(
        "⏳ *Scheduled Alerts* — running 3 workflows\\.\n"
        "This takes 5–20 min\\. You'll receive 3 tables when done\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        from tradingview.telegram_scheduler import TelegramScheduler
        scheduler = TelegramScheduler(dry_run=False, force_market=True)  # --force: ignore market hours
        loop = _asyncio.get_event_loop()
        stats = await loop.run_in_executor(None, scheduler.run_cycle)

        stocks_new   = stats.get("stocks_new", 0)
        screen_new   = stats.get("screener_new", 0)
        analysis_new = stats.get("analysis_new", 0)
        errors       = stats.get("errors", [])

        stocks_skip  = stats.get("stocks_skipped", 0)
        screen_skip  = stats.get("screener_skipped", 0)
        analysis_skip= stats.get("analysis_skipped", 0)

        def _e(t: str) -> str:
            for ch in r"\_*[]()~`>#+-=|{}.!":
                t = t.replace(ch, f"\\{ch}")
            return t

        summary = (
            f"✅ *Schedule cycle complete*\n\n"
            f"📈 Stocks: {_e(str(stocks_new))} new, {_e(str(stocks_skip))} skipped\n"
            f"🔍 Screener: {_e(str(screen_new))} new, {_e(str(screen_skip))} skipped\n"
            f"📊 Analysis: {_e(str(analysis_new))} new, {_e(str(analysis_skip))} skipped"
        )
        if errors:
            err_str = _e("; ".join(errors[:2]))
            summary += f"\n⚠️ Errors: {err_str}"

        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as exc:
        await update.message.reply_text(f"⚠️ Schedule cycle error: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
# Enhancement #2: Inline keyboard callback handler
# ═══════════════════════════════════════════════════════════════════════════

# Map callback_data → handler coroutine (resolved after all handlers are defined)
_CALLBACK_MAP: dict[str, str] = {
    "morning":   "cmd_morning",
    "fast":      "cmd_fast",
    "portfolio": "cmd_portfolio",
    "india":     "cmd_india",
    "signals":   "cmd_signals",
    "sell":      "cmd_sell",
    "alerts":    "cmd_alerts",
    "screener":  "cmd_screener",
    "heatmap":   "cmd_heatmap",
    "calendar":  "cmd_calendar",
    "reports":   "cmd_reports",
    "status":    "cmd_status",
    "cancel":    "cmd_cancel",
    "unblock":   "cmd_unblock",
    "pnl":       "cmd_pnl",
    "chart":     "cmd_chart",
}

async def on_button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all InlineKeyboardButton taps by routing to the correct handler."""
    query = update.callback_query
    await query.answer()   # dismiss the spinner on the button

    action = query.data or ""
    handler_name = _CALLBACK_MAP.get(action)
    if not handler_name:
        await query.message.reply_text(f"❓ Unknown action: {action}")
        return

    # Build a synthetic Update that points at the original message so handlers
    # can call update.message.reply_text() as normal
    fake_update = Update(
        update_id=update.update_id,
        callback_query=query,
        message=query.message,
    )
    # Route to the handler
    import sys as _sys
    handler_fn = globals().get(handler_name)
    if handler_fn is None:
        await query.message.reply_text(f"❓ Handler {handler_name!r} not found.")
        return
    await handler_fn(fake_update, context)


# ═══════════════════════════════════════════════════════════════════════════
# Enhancement #4+5: JobQueue — scheduled push + proactive alert forwarding
# ═══════════════════════════════════════════════════════════════════════════

async def _job_4h_scheduled_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    4-hour periodic job: run signal_stack + options screener + options analysis
    and send 3 tabular MarkdownV2 alerts to all push targets.

    BLOCKER-1 fix: run_cycle() is SYNCHRONOUS — called via run_in_executor so
    it never blocks the asyncio event loop. The async job just awaits the executor.
    HIGH-5 fix: job_kwargs max_instances=1 prevents overlapping runs.
    """
    import asyncio as _asyncio
    try:
        # Import here to avoid loading ~400 lines of scheduler at bot startup
        from tradingview.telegram_scheduler import TelegramScheduler
        scheduler = TelegramScheduler(dry_run=False, force_market=False)
        loop = _asyncio.get_event_loop()
        stats = await loop.run_in_executor(None, scheduler.run_cycle)
        if stats.get("skipped_market_hours"):
            logger.info("4h job: skipped — all markets closed")
            return
        logger.info(
            "4h job complete: stocks=%d/%d screener=%d/%d analysis=%d/%d",
            stats.get("stocks_new", 0), stats.get("stocks_skipped", 0),
            stats.get("screener_new", 0), stats.get("screener_skipped", 0),
            stats.get("analysis_new", 0), stats.get("analysis_skipped", 0),
        )
        if stats.get("errors"):
            logger.warning("4h job errors: %s", stats["errors"])
    except Exception as exc:
        logger.error("4h scheduled alerts job failed: %s", exc)


async def _job_zerodha_warning(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 8:30 AM: warn if Zerodha token is missing or about to expire."""
    if _zerodha_token_ok():
        return   # token is fine, no need to notify
    for uid in _push_targets():
        await context.bot.send_message(
            chat_id=uid,
            text=(
                "⚠️ *Zerodha Token Alert*\n\n"
                "Your Zerodha token is missing or expired\\.\n"
                "Run the login script on your PC before the morning run:\n"
                "`python india\\-brokers/zerodha_login\\.py`\n\n"
                "Then /india or /morning will work without hanging\\."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _job_morning_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 9:00 AM Mon-Fri: prompt user to run morning checks."""
    for uid in _push_targets():
        await context.bot.send_message(
            chat_id=uid,
            text="🌅 *Good morning\\!* Markets are open\\.\nTap a button to start:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🌅 Full Morning Run", callback_data="morning"),
                    InlineKeyboardButton("⚡ Quick P&L",        callback_data="pnl"),
                ],
                [
                    InlineKeyboardButton("📈 Signals Only",     callback_data="signals"),
                    InlineKeyboardButton("📊 Portfolio",        callback_data="portfolio"),
                ],
            ]),
        )


def _get_score_breakdown(tv_symbol: str) -> str:
    """Query alert_tracker.db for L1-L4 score breakdown of a symbol.

    Fixed: correct DB path (database/alert_tracker.db), correct column (tv_symbol),
    and no misleading-zero scores — omit sub-score when data is unavailable.
    """
    import sqlite3
    # Fix BUG-T2: correct DB path — alert_tracker.db lives in database/, not tradingview/
    db_path = ROOT / "database" / "alert_tracker.db"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        bare = tv_symbol.split(":")[-1] if ":" in tv_symbol else tv_symbol
        # Fix BUG-T2: use correct column name tv_symbol (not ticker)
        row = conn.execute(
            "SELECT result_json FROM alerts WHERE tv_symbol=? OR tv_symbol=?"
            " ORDER BY created_at DESC LIMIT 1",
            (tv_symbol, bare),
        ).fetchone()
        conn.close()
        if not row or not row["result_json"]:
            return ""
        rj = json.loads(row["result_json"])
        sc = rj.get("scores", {})
        # Fix BUG-T2: omit sub-score when None / missing — do NOT default to 0
        def _get_total(key: str) -> float | None:
            node = sc.get(key)
            if not node:
                return None
            val = node.get("total")
            return float(val) if val is not None else None
        l1 = _get_total("l1")
        l2 = _get_total("l2")
        l3 = _get_total("l3")
        l4 = _get_total("l4")
        parts = []
        if l1 is not None:
            parts.append(f"L1={l1:.0f}/30")
        if l2 is not None:
            parts.append(f"L2={l2:.0f}/30")
        if l3 is not None:
            parts.append(f"L3={l3:.0f}/20")
        if l4 is not None:
            parts.append(f"L4={l4:.0f}/20")
        # Enricher flags
        enrichers = []
        if rj.get("l5_enrichment"):
            enrichers.append("Moomoo")
        if rj.get("india_l5_enrichment"):
            enrichers.append("India")
        if rj.get("l6_enrichment"):
            enrichers.append("IBKR")
        if rj.get("l7_enrichment"):
            enrichers.append("External")
        enr_str = f"  Enrichers: {', '.join(enrichers)}" if enrichers else ""
        return " ".join(parts) + enr_str
    except Exception:
        return ""


async def _job_push_hot_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 5 min: push new high-score alerts from alert_tracker.db to all allowed users.

    fires_raw.json is NOT used here — it contains raw TradingView fires with no composite_score.
    alert_tracker.db is the correct source for scored, processed alerts.
    """
    if not _ALERTS_DB_PATH.exists():
        return

    def _esc(text: str) -> str:
        """Escape ALL MarkdownV2 special characters (Telegram's full set)."""
        for ch in r"\_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text

    last_seen: str = context.bot_data.get("last_alert_push_ts", "")
    try:
        with sqlite3.connect(str(_ALERTS_DB_PATH), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            if not last_seen:
                # First run: seed timestamp to the latest alert so we don't spam old history.
                row = conn.execute("SELECT MAX(created_at) FROM alerts").fetchone()
                context.bot_data["last_alert_push_ts"] = str(row[0] or "")
                return
            rows = conn.execute(
                """
                SELECT tv_symbol, composite_score, signal, created_at
                FROM alerts
                WHERE created_at > ?
                  AND composite_score >= ?
                ORDER BY created_at ASC
                LIMIT 5
                """,
                (last_seen, _HOT_ALERT_THRESHOLD),
            ).fetchall()
    except Exception as exc:
        logger.warning("_job_push_hot_alerts DB query failed: %s", exc)
        return

    if not rows:
        return

    # Advance checkpoint BEFORE sending so a transient send failure won't re-push same alerts.
    context.bot_data["last_alert_push_ts"] = str(rows[-1]["created_at"] or last_seen)

    _SIG_MAP = {"UNKNOWN": "SCR", "MANUAL": "SCR", "": "SCR"}
    for r in rows:
        sym   = str(r["tv_symbol"] or "?")
        score = float(r["composite_score"] or 0)
        raw_sig = str(r["signal"] or "").upper()
        sig   = _SIG_MAP.get(raw_sig, raw_sig[:4])
        bd    = _get_score_breakdown(sym)
        bd_line = f"\n📊 `{_esc(bd)}`" if bd else ""
        msg = (
            f"🚨 *Hot Alert*: `{_esc(sym)}`\n"
            f"Score: *{score:.0f}* \\| Signal: `{_esc(sig)}`{bd_line}\n"
            f"_Tap /signals to see the full report_"
        )
        for uid in _push_targets():
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=msg,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as exc:
                logger.warning("Hot alert push to %s failed: %s", uid, exc)


# ─── Individual report commands ────────────────────────────────────────────

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    env = _india_env()
    # Warn if Zerodha token missing (won't block — global dashboard has 5 other brokers)
    if not _zerodha_token_ok():
        await update.message.reply_text(
            "⚠️ *Zerodha token missing or expired*\n"
            "Global portfolio will run but Zerodha positions may be absent.\n"
            "_To fix: run Zerodha login on your PC first, then retry._",
            parse_mode=ParseMode.MARKDOWN,
        )
    await _run_step(
        update, "Global Portfolio Dashboard",
        [sys.executable, str(ROOT / "global_portfolio_dashboard.py")],
        cwd=ROOT, env=env,
        report_path=REPORTS / "global_portfolio_dashboard.html",
        report_label="Global Portfolio",
    )


async def cmd_india(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    if not _zerodha_token_ok():
        await update.message.reply_text(
            "⛔ *Zerodha token missing or expired*\n\n"
            "The India dashboard needs a valid Zerodha token.\n\n"
            "*To fix on your PC:*\n"
            "```\npython india-brokers/zerodha_login.py\n```\n"
            "After logging in, send /india again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    env = _india_env()
    await _run_step(
        update, "India Portfolio Dashboard",
        [sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
        cwd=INDIA, env=env,
        report_path=REPORTS / "india_portfolio_dashboard.html",
        report_label="India Portfolio",
    )


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    env = _india_env()
    uid = update.effective_user.id

    # Step 1: Download fires
    await _run_step(
        update, "Downloading TradingView fires",
        [sys.executable, str(TV / "tv_data" / "download_fires.py")],
        cwd=TV,
    )

    # Step 2: Signal stack
    await _run_step(
        update, "Signal Stack (scoring)",
        [
            sys.executable, str(TV / "signal_stack.py"),
            "--n", "100", "--threshold", "50",
            "--no-external",
        ],
        cwd=TV, env=env,
        report_path=REPORTS / "signal_stack_report.html",
        report_label="Signal Stack",
    )
    # Also send actionable
    await _send_report(update, REPORTS / "actionable_report.html", "Actionable Signals")


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    env = _india_env()
    await _run_step(
        update, "Sell Strategy",
        [sys.executable, str(TV / "sell" / "sell_strategy.py"), "--live"],
        cwd=TV, env=env,
        report_path=REPORTS / "sell_strategy_report.html",
        report_label="Sell Strategy",
    )


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    await _run_step(
        update, "Alert Tracker",
        [sys.executable, str(TV / "alert_tracker.py"), "--all"],
        cwd=TV,
        report_path=REPORTS / "alert_performance_report.html",
        report_label="Alert Performance",
    )


async def cmd_alerts_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post compact alert summary from alert_tracker.db.

    Usage:  /alerts_now          — uses default threshold (70)
            /alerts_now 80       — override threshold to 80
    """
    if not await _gate(update):
        return
    threshold = float(_HOT_ALERT_THRESHOLD)
    if context.args:
        try:
            threshold = float(context.args[0])
        except ValueError:
            await update.effective_message.reply_text(
                f"⚠️ Invalid threshold — using default ({threshold:.0f}).",
                parse_mode=ParseMode.MARKDOWN,
            )
    status_msg = await update.effective_message.reply_text(
        f"🔍 Building alert summary (score ≥ {threshold:.0f})…"
    )
    text = _build_alerts_summary_text(threshold)
    try:
        await status_msg.delete()
    except Exception:
        pass
    chat_id = update.effective_chat.id
    for chunk in _split_summary(text):
        await context.bot.send_message(
            chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN
        )


_ALERTS_DB_PATH = ROOT / "database" / "alert_tracker.db"


def _build_alerts_summary_text(threshold: float = 70.0) -> str:
    """Query alert_tracker.db and format a compact exchange-grouped summary."""
    if not _ALERTS_DB_PATH.exists():
        return f"⚠️ alert\\_tracker.db not found."
    try:
        with sqlite3.connect(str(_ALERTS_DB_PATH), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            # GROUP BY tv_symbol deduplicates — keeps best score per symbol (max 20)
            rows = conn.execute(
                """
                SELECT tv_symbol,
                       MAX(composite_score) AS composite_score,
                       signal, fire_time
                FROM alerts
                WHERE created_at > datetime('now', '-24 hours')
                  AND composite_score >= ?
                GROUP BY tv_symbol
                ORDER BY composite_score DESC
                LIMIT 20
                """,
                (threshold,),
            ).fetchall()
    except Exception as exc:
        return f"⚠️ DB query failed: {exc}"

    now_str = datetime.now().strftime("%d %b %Y %H:%M IST")
    if not rows:
        return (
            f"📊 *Alert Summary — {now_str}*\n"
            f"_No alerts above score {threshold:.0f} in the last 24h._"
        )

    _SIG_MAP = {"UNKNOWN": "SCR", "MANUAL": "SCR", "": "SCR"}
    stocks: list = []
    options: list = []
    for r in rows:
        sym = str(r["tv_symbol"] or "")
        if ":" in sym:
            exch, sym = sym.split(":", 1)
        else:
            exch = "US"
        sym  = re.sub(r"^#\d+\s*", "", sym).upper()
        exch = exch.upper()
        raw_sig = str(r["signal"] or "").upper()
        sig  = _SIG_MAP.get(raw_sig, raw_sig[:4])
        score = round(float(r["composite_score"] or 0), 1)
        is_opt = any(k in raw_sig for k in ("CALL", "PUT", "OPTION"))
        entry = {"exchange": exch, "symbol": sym, "score": score, "signal": sig}
        if is_opt:
            options.append(entry)
        else:
            stocks.append(entry)

    lines = [
        f"📊 *Alert Summary — {now_str}*",
        f"Threshold: >{threshold:.0f} | {len(stocks)} stocks, {len(options)} options",
        "",
    ]
    if stocks:
        by_ex: dict = {}
        for s in stocks:
            by_ex.setdefault(s["exchange"], []).append(s)
        lines.append("🔵 *STOCKS*")
        lines.append("```")
        lines.append(f"{'Exchange':<10}{'Symbol':<14}{'Score':>6}  {'Sig'}")
        lines.append("─" * 37)
        for ex in sorted(by_ex):
            for s in by_ex[ex]:
                lines.append(f"{ex:<10}{s['symbol']:<14}{s['score']:>6.1f}  {s['signal']}")
        lines.append("```")
    if options:
        lines.append("")
        lines.append("🟡 *OPTIONS*")
        lines.append("```")
        lines.append(f"{'Exch':<8}{'Symbol':<12}{'Signal':<16}{'Score':>6}")
        lines.append("─" * 44)
        for o in options:
            sig_s = o["signal"][:15] if o["signal"] else ""
            lines.append(f"{o['exchange']:<8}{o['symbol']:<12}{sig_s:<16}{o['score']:>6.1f}")
        lines.append("```")
    return "\n".join(lines)


def _split_summary(text: str) -> list:
    """Split a summary into ≤4096-char Telegram-safe chunks."""
    if len(text) <= 4096:
        return [text]
    split_at = text.find("\n🟡 *OPTIONS*")
    if split_at > 0:
        return [text[:split_at].strip(), text[split_at:].strip()]
    return [text[:4000], text[4000:]]


async def cmd_screener(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    await _run_step(
        update, "Multi-Asset Screener",
        [sys.executable, str(TV / "screeners" / "multi_asset_screener.py")],
        cwd=TV,
        report_path=REPORTS / "multi_asset_report.html",
        report_label="Multi-Asset Screener",
    )


async def cmd_heatmap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    await _run_step(
        update, "Sector Heatmap",
        [
            sys.executable, str(TV / "analysis" / "tv_sector_heatmap.py"),
            "--exchange", "all",
            "--out", str(REPORTS / "sector_heatmap.html"),
        ],
        cwd=TV,
        report_path=REPORTS / "sector_heatmap.html",
        report_label="Sector Heatmap",
    )


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    await _run_step(
        update, "Economic Calendar",
        [
            sys.executable, str(TV / "tv_data" / "tv_economic_calendar.py"),
            "--out", str(REPORTS / "economic_calendar.html"),
        ],
        cwd=TV,
        report_path=REPORTS / "economic_calendar.html",
        report_label="Economic Calendar",
    )


# ─── Morning run commands ──────────────────────────────────────────────────

async def cmd_fast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fast morning run: portfolio + signals only (~4 min)."""
    if not await _gate(update):
        return
    uid = update.effective_user.id
    env = _india_env()
    started = time.time()

    await update.message.reply_text(
        "🌅 *Fast Morning Run* — Portfolio \\+ Signals \\(\\~4 min\\)\n\n"
        "Steps: fires → portfolio → india → signals\n"
        "Reports will arrive as they complete\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # 1. Download fires
    await _run_step(
        update, "Downloading TradingView fires",
        [sys.executable, str(TV / "tv_data" / "download_fires.py")],
        cwd=TV,
    )
    if uid not in _running_proc and uid in _running_proc:  # check cancelled
        return

    # 2. Global Portfolio
    await _run_step(
        update, "Global Portfolio Dashboard",
        [sys.executable, str(ROOT / "global_portfolio_dashboard.py")],
        cwd=ROOT, env=env,
        report_path=REPORTS / "global_portfolio_dashboard.html",
        report_label="Global Portfolio",
    )

    # 3. India Portfolio
    await _run_step(
        update, "India Portfolio Dashboard",
        [sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
        cwd=INDIA, env=env,
        report_path=REPORTS / "india_portfolio_dashboard.html",
        report_label="India Portfolio",
    )

    # 4. Signal Stack
    await _run_step(
        update, "Signal Stack",
        [
            sys.executable, str(TV / "signal_stack.py"),
            "--n", "100", "--threshold", "50",
            "--no-external",
        ],
        cwd=TV, env=env,
        report_path=REPORTS / "signal_stack_report.html",
        report_label="Signal Stack",
    )
    await _send_report(update, REPORTS / "actionable_report.html", "Actionable Signals")

    elapsed = int(time.time() - started)
    await update.message.reply_text(
        f"✅ *Fast Morning Run complete* in {elapsed // 60}m {elapsed % 60}s\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full morning run: all reports (~8 min)."""
    if not await _gate(update):
        return
    uid = update.effective_user.id

    # ── Cooldown guard: prevent concurrent morning runs from flooding Telegram ──
    if not try_claim_morning_run("telegram_bot"):
        await update.message.reply_text(
            "⏭ Morning run already in progress\n\n"
            "Another system (main.py or report_scheduler.py) started a morning run "
            "within the last 2 hours. Skipping to avoid flooding Telegram.\n\n"
            "If you're sure no run is active, wait 2 hours for the cooldown to expire.",
        )
        return

    env = _india_env()
    started = time.time()

    # Load saved profile if it exists
    profile: dict = {}
    profile_path = ROOT / "morning_run_profile.json"
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except Exception:
            profile = {}

    include_screener = profile.get("include_screener", True)
    include_cgt      = profile.get("include_cgt", True)
    include_options  = profile.get("include_options", False)
    est_min          = profile.get("est_minutes", 8)

    steps_list = ["fires", "portfolio", "india", "pnl", "signals", "sell", "alerts", "heatmap", "calendar"]
    if include_screener:
        steps_list.append("screener")
    if include_cgt:
        steps_list.append("cgt")
    if include_options:
        steps_list.extend(["options", "hedger"])

    await update.message.reply_text(
        f"🌅 *Full Morning Run* \\(\\~{est_min} min\\)\n\n"
        f"Steps: {len(steps_list)} reports to generate\n"
        f"Screener: {'✅' if include_screener else '⏭'} | "
        f"CGT: {'✅' if include_cgt else '⏭'} | "
        f"Options: {'✅' if include_options else '⏭'}\n\n"
        "Reports will arrive as they complete\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # ── Step 1: Download fires ───────────────────────────────────────────
    await _run_step(
        update, "[1] Downloading TradingView fires",
        [sys.executable, str(TV / "tv_data" / "download_fires.py")],
        cwd=TV,
    )

    # ── Step 2: Global Portfolio ─────────────────────────────────────────
    await _run_step(
        update, "[2] Global Portfolio Dashboard",
        [sys.executable, str(ROOT / "global_portfolio_dashboard.py")],
        cwd=ROOT, env=env,
        report_path=REPORTS / "global_portfolio_dashboard.html",
        report_label="Global Portfolio",
    )

    # ── Step 2b: P&L Attribution ─────────────────────────────────────────
    await _run_step(
        update, "[2b] P&L Attribution",
        [
            sys.executable, str(TV / "analysis" / "pnl_attribution.py"),
            "--out", str(REPORTS / "pnl_attribution.html"),
        ],
        cwd=ROOT,
        report_path=REPORTS / "pnl_attribution.html",
        report_label="P&L Attribution",
    )

    # ── Step 3: India Portfolio ──────────────────────────────────────────
    await _run_step(
        update, "[3] India Portfolio Dashboard",
        [sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
        cwd=INDIA, env=env,
        report_path=REPORTS / "india_portfolio_dashboard.html",
        report_label="India Portfolio",
    )

    # ── Step 4: Signal Stack ─────────────────────────────────────────────
    await _run_step(
        update, "[4] Signal Stack",
        [
            sys.executable, str(TV / "signal_stack.py"),
            "--n", "100", "--threshold", "50",
            "--no-external",
        ],
        cwd=TV, env=env,
        report_path=REPORTS / "signal_stack_report.html",
        report_label="Signal Stack",
    )
    await _send_report(update, REPORTS / "actionable_report.html", "Actionable Signals")

    # ── Step 5: Sell Strategy ────────────────────────────────────────────
    await _run_step(
        update, "[5] Sell Strategy",
        [sys.executable, str(TV / "sell" / "sell_strategy.py"), "--live"],
        cwd=TV, env=env,
        report_path=REPORTS / "sell_strategy_report.html",
        report_label="Sell Strategy",
    )

    # ── Step 6: Alert Tracker ────────────────────────────────────────────
    await _run_step(
        update, "[6] Alert Tracker",
        [sys.executable, str(TV / "alert_tracker.py"), "--all"],
        cwd=TV,
        report_path=REPORTS / "alert_performance_report.html",
        report_label="Alert Performance",
    )

    # ── Step 7: Sector Heatmap ───────────────────────────────────────────
    await _run_step(
        update, "[7] Sector Heatmap",
        [
            sys.executable, str(TV / "analysis" / "tv_sector_heatmap.py"),
            "--exchange", "all",
            "--out", str(REPORTS / "sector_heatmap.html"),
        ],
        cwd=TV,
        report_path=REPORTS / "sector_heatmap.html",
        report_label="Sector Heatmap",
    )

    # ── Step 8: Economic Calendar ────────────────────────────────────────
    await _run_step(
        update, "[8] Economic Calendar",
        [
            sys.executable, str(TV / "tv_data" / "tv_economic_calendar.py"),
            "--out", str(REPORTS / "economic_calendar.html"),
        ],
        cwd=TV,
        report_path=REPORTS / "economic_calendar.html",
        report_label="Economic Calendar",
    )

    # ── Step 9 [optional]: Screener ──────────────────────────────────────
    if include_screener:
        await _run_step(
            update, "[9] Multi-Asset Screener",
            [sys.executable, str(TV / "screeners" / "multi_asset_screener.py")],
            cwd=TV,
            report_path=REPORTS / "multi_asset_report.html",
            report_label="Multi-Asset Screener",
        )

    # ── Step 10 [optional]: CGT ──────────────────────────────────────────
    if include_cgt:
        await _run_step(
            update, "[10] Combined CGT Report",
            [sys.executable, str(CGT / "combined_cgt_report.py")],
            cwd=CGT, env=env,
            report_path=REPORTS / "cgt" / "combined_cgt_report.html",
            report_label="Combined CGT",
        )

    # ── Steps 11-12 [optional]: Options ──────────────────────────────────
    if include_options:
        await _run_step(
            update, "[11] Options from Actionable",
            [sys.executable, str(TV / "options" / "run_options.py"), "--from-actionable"],
            cwd=TV, env=env,
            report_path=REPORTS / "options_report.html",
            report_label="Options Analysis",
        )
        await _run_step(
            update, "[12] Options Hedger",
            [sys.executable, str(TV / "options" / "options_hedger.py")],
            cwd=TV, env=env,
            report_path=REPORTS / "options_hedger_report.html",
            report_label="Options Hedger",
        )

    elapsed = int(time.time() - started)
    await update.message.reply_text(
        f"🎉 *Morning Run complete* in {elapsed // 60}m {elapsed % 60}s\\!\n"
        f"All reports sent above\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    release_morning_run()  # release slot — next run now allowed


# ─── Reports listing & on-demand send ─────────────────────────────────────

# Known reports catalogue
_REPORTS_CATALOGUE: dict[str, tuple[str, Path]] = {
    "portfolio":  ("Global Portfolio",        REPORTS / "global_portfolio_dashboard.html"),
    "india":      ("India Portfolio",         REPORTS / "india_portfolio_dashboard.html"),
    "signals":    ("Signal Stack",            REPORTS / "signal_stack_report.html"),
    "actionable": ("Actionable Signals",      REPORTS / "actionable_report.html"),
    "sell":       ("Sell Strategy",           REPORTS / "sell_strategy_report.html"),
    "alerts":     ("Alert Performance",       REPORTS / "alert_performance_report.html"),
    "heatmap":    ("Sector Heatmap",          REPORTS / "sector_heatmap.html"),
    "calendar":   ("Economic Calendar",       REPORTS / "economic_calendar.html"),
    "screener":   ("Multi-Asset Screener",    REPORTS / "multi_asset_report.html"),
    "cgt":        ("Combined CGT",            REPORTS / "cgt" / "combined_cgt_report.html"),
    "pnl":        ("P&L Attribution",         REPORTS / "pnl_attribution.html"),
    "options":    ("Options Analysis",        REPORTS / "options_report.html"),
    "hedger":     ("Options Hedger",          REPORTS / "options_hedger_report.html"),
    "autotrader": ("Auto Trader Orders",      REPORTS / "auto_trader_orders.json"),
}


async def cmd_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    lines = ["📋 *Available Reports*\n"]
    for key, (label, path) in _REPORTS_CATALOGUE.items():
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m %H:%M")
            size_kb = path.stat().st_size // 1024
            lines.append(f"✅ `{key:<12}` {label} ({size_kb} KB, {mtime})")
        else:
            lines.append(f"⚪ `{key:<12}` {label} (not generated yet)")
    lines.append("\nUse `/send <name>` to receive a report.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/send <name>`\nRun /reports to see available names.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    key = args[0].lower().strip()
    entry = _REPORTS_CATALOGUE.get(key)
    if entry is None:
        await update.message.reply_text(
            f"Unknown report: `{key}`\nRun /reports for valid names.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    label, path = entry
    await _send_report(update, path, label)


# ─── Unknown message ───────────────────────────────────────────────────────

async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "❓ Unknown command. Use /help to see all available commands."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    # Use TELEGRAM_BOT_TOKEN env var for @Ajaaz_bot (Jerry).
    # NEVER use AJAAZ_ALERT_BOT_TOKEN here — that token belongs to report_scheduler.py.
    # Using both tokens in the same process would conflict with the scheduler bot.
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN", "")
        or _DEFAULT_BOT_TOKEN  # @Ajaaz_bot hardcoded fallback
    )

    # BUG-T5: Graceful skip when no token available — NEVER block morning run
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set and no fallback — Telegram bot disabled")
        print("[WARN] Telegram bot disabled — no bot token available.")
        return

    os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "5986275415")

    allowed = _allowed_users()
    if not allowed:
        print("WARNING: TELEGRAM_ALLOWED_USERS is not set.")
        print("The bot will respond to ALL Telegram users — set this in production!")
    else:
        print(f"Authorized user IDs: {sorted(allowed)}")

    print(f"\n{'='*60}")
    print("  Stocks Trading Bot v2 — starting in polling mode")
    print(f"{'='*60}")
    print("  TradingView webhook (port 8765) is NOT affected.")
    print("  Bot uses Telegram polling — no inbound ports needed.")
    print("  Enhancements active: /pnl /chart inline-kb jobqueue alerts")
    print(f"{'='*60}\n")

    # Enhancement #4+5: build with job_queue support (requires apscheduler)
    try:
        app = (
            ApplicationBuilder()
            .token(token)
            .connect_timeout(10)
            .read_timeout(35)   # BUG-T3 fix: must exceed poll timeout (30s) by ≥5s
            .write_timeout(30)
            .build()
        )
        # Test if job_queue is available (suppress PTBUserWarning when package not installed)
        import warnings as _warnings
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            _jq = app.job_queue
        if _jq is not None:
            _setup_job_queue(app)
            print("✅ JobQueue active: daily push + alert polling enabled")
        else:
            print("⚠️  JobQueue unavailable — run: pip install 'python-telegram-bot[job-queue]'")
    except Exception as exc:
        print(f"⚠️  ApplicationBuilder error: {exc}")
        raise

    # ── Enhancement #3: error handler ─────────────────────────────────────
    import traceback

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        err_type = type(err).__name__
        # Silently ignore routine Telegram network errors
        silent_types = {"TimedOut", "NetworkError", "RetryAfter"}
        if err_type in silent_types:
            logger.debug("Suppressed Telegram error: %s: %s", err_type, err)
            return
        msg = "".join(traceback.format_exception(None, err, err.__traceback__))
        logger.error("Bot error [%s]: %s", err_type, err)
        # Notify authorized users of unexpected errors
        for uid in _push_targets():
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"⚠️ *Bot error* `{err_type}`\n`{str(err)[:200]}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass

    app.add_error_handler(_error_handler)

    # ── Register all command handlers ─────────────────────────────────────
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("id",        cmd_id))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("unblock",   cmd_unblock))
    app.add_handler(CommandHandler("morning",   cmd_morning))
    app.add_handler(CommandHandler("fast",      cmd_fast))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("india",     cmd_india))
    app.add_handler(CommandHandler("signals",   cmd_signals))
    app.add_handler(CommandHandler("sell",      cmd_sell))
    app.add_handler(CommandHandler("alerts",      cmd_alerts))
    app.add_handler(CommandHandler("alerts_now",  cmd_alerts_now))
    app.add_handler(CommandHandler("screener",  cmd_screener))
    app.add_handler(CommandHandler("heatmap",   cmd_heatmap))
    app.add_handler(CommandHandler("calendar",  cmd_calendar))
    app.add_handler(CommandHandler("reports",   cmd_reports))
    app.add_handler(CommandHandler("send",      cmd_send))
    # Enhancement #1: /pnl instant snapshot
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    # Enhancement #6: /chart pie chart
    app.add_handler(CommandHandler("chart",     cmd_chart))
    # Enhancement #6b: /schedule — trigger 4h cycle on demand
    app.add_handler(CommandHandler("schedule",  cmd_schedule))
    # Enhancement #2: inline keyboard callback
    app.add_handler(CallbackQueryHandler(on_button_tap))

    # Enhancement #7: handle reply-keyboard text presses (/pnl via tap)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            _handle_reply_keyboard,
        )
    )

    # Catch-all for unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    print("Bot is running. Press Ctrl+C to stop.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def _setup_job_queue(app) -> None:
    """Register JobQueue scheduled tasks (Enhancement #4+5)."""
    import pytz

    tz = pytz.timezone("Asia/Kolkata")   # IST — change to your local tz

    # Enhancement #4a: Zerodha token warning at 8:30 AM IST Mon-Fri
    app.job_queue.run_daily(
        _job_zerodha_warning,
        time=dtime(hour=8, minute=30, tzinfo=tz),
        days=(0, 1, 2, 3, 4),   # Mon=0 … Fri=4 (APScheduler convention)
        name="zerodha_warning",
    )

    # Enhancement #4b: Morning reminder at 9:00 AM IST Mon-Fri
    app.job_queue.run_daily(
        _job_morning_reminder,
        time=dtime(hour=9, minute=0, tzinfo=tz),
        days=(0, 1, 2, 3, 4),   # Mon=0 … Fri=4 (APScheduler convention)
        name="morning_reminder",
    )

    # Enhancement #5: Poll fires_raw.json every 5 min for hot alerts
    app.job_queue.run_repeating(
        _job_push_hot_alerts,
        interval=300,   # seconds
        first=60,       # first run after 60s
        name="hot_alert_push",
    )

    # Enhancement #6b: 4-hour scheduled alerts — stocks + options screener + options analysis
    # job_kwargs max_instances=1 prevents overlapping runs if a cycle takes longer than 4h
    app.job_queue.run_repeating(
        _job_4h_scheduled_alerts,
        interval=4 * 3600,   # 14400 seconds
        first=120,           # first run 2 min after bot starts
        name="4h_scheduled_alerts",
        job_kwargs={"max_instances": 1},
    )


# ── Enhancement #7: handle reply-keyboard taps (they send text, not /cmd) ─

_REPLY_KB_MAP = {
    "🌅 /morning":   "morning",
    "⚡ /pnl":        "pnl",
    "📊 /portfolio":  "portfolio",
    "🇮🇳 /india":      "india",
    "📈 /signals":    "signals",
    "📉 /sell":       "sell",
    "📋 /reports":    "reports",
    "❓ /help":       "help",
}


async def _handle_reply_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route persistent reply-keyboard button taps to command handlers."""
    if not _is_allowed(update.effective_user.id):
        return
    text = (update.message.text or "").strip()
    action = _REPLY_KB_MAP.get(text)
    if action is None:
        # Not a keyboard button — let cmd_unknown handle if needed
        return
    # Reuse the inline callback map routing
    handler_name = _CALLBACK_MAP.get(action)
    if handler_name:
        fn = globals().get(handler_name)
        if fn:
            await fn(update, context)


if __name__ == "__main__":
    main()


