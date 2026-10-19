#!/usr/bin/env python3
"""
Report Scheduler Bot — @AjaazAlertBot
========================================
Automatically generates all trading reports on a schedule and sends them
via Telegram.  Runs as a persistent background service.

Bot:     @AjaazAlertBot
Token:   set via AJAAZ_ALERT_BOT_TOKEN env var (see scheduler_env.ps1)

Default schedule (IST):
  • Morning  08:30  Mon–Fri  → screener + portfolio + india + signals + sell
  • Evening  17:30  Mon–Fri  → portfolio + india + signals + sell + alerts

Manual commands (any authorized user can send to the bot):
  /run_morning   — trigger full morning run immediately
  /run_evening   — trigger evening run immediately
  /run_now       — same as /run_morning
  /status        — broker & token health check
  /zerodha_token <token>  — update Zerodha access token (auth recovery)
  /next          — show next scheduled run times
  /pause         — pause scheduled runs (until /resume)
  /resume        — resume scheduled runs
  /help          — list all commands

Zerodha auth recovery:
  Token expires ~06:05 IST daily.  When expired the scheduler:
  1. Sends a Telegram warning at 08:15 IST
  2. Skips the India dashboard in the morning run (other reports still run)
  3. Waits for /zerodha_token <token> from the user (or PC login)
  4. Once token received → runs India dashboard and sends it immediately

Setup:
  Source scheduler_env.ps1 then run:
    . .\\scheduler_env.ps1
    python report_scheduler.py

  OR pass env vars inline:
    $env:AJAAZ_ALERT_BOT_TOKEN  = "<your-bot-token>"
    $env:TELEGRAM_ALLOWED_USERS = "<your-telegram-user-id>"
    python report_scheduler.py

  Optional env var overrides:
    SCHEDULER_MORNING_IST  = "08:30"   (24h HH:MM, default 08:30)
    SCHEDULER_EVENING_IST  = "17:30"   (24h HH:MM, default 17:30)
    SCHEDULER_EVERY_HOURS  = "2"        (if set: run every N hours instead)
    SCHEDULER_DAYS         = "1,2,3,4,5"  (Mon=1…Sun=7, default weekdays)
    SCHEDULER_ZERODHA_WARN = "08:15"   (Zerodha warning time, default 08:15)
"""

import asyncio
import json
import logging
import math
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timezone
from pathlib import Path

from tradingview.options.options_snapshot import read_snapshot

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
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

# ── Project paths ──────────────────────────────────────────────────────────
ROOT    = Path(__file__).parent
TV      = ROOT / "tradingview"
INDIA   = ROOT / "india-brokers"
CGT     = ROOT / "cgt"
REPORTS = ROOT / "reports"
OPTIONS_SNAPSHOT = REPORTS / "options_signals.json"
_DB_PATH = ROOT / "database" / "alert_tracker.db"

# ── Bot token ────────────────────────────────────────────────────────────────
# Resolved at startup from AJAAZ_ALERT_BOT_TOKEN (see tradingview/scheduler/config.py).
# There is deliberately NO embedded fallback: a committed literal is both a leak
# and a silent-misconfiguration hazard. Missing credentials fail loudly at
# startup with an actionable, secret-free message.

# ── Morning run cooldown guard ────────────────────────────────────────────
# Prevents concurrent morning runs from three systems (main.py, telegram_bot.py,
# report_scheduler.py) flooding Telegram with 60+ messages simultaneously.
try:
    _tg_dir = str(TV)
    if _tg_dir not in sys.path:
        sys.path.insert(0, _tg_dir)
    from morning_guard import try_claim_morning_run, release_morning_run  # type: ignore
    _MORNING_GUARD_AVAILABLE = True
except ImportError:
    _MORNING_GUARD_AVAILABLE = False
    def try_claim_morning_run(caller: str) -> bool: return True   # noqa: E704
    def release_morning_run() -> None: pass                        # noqa: E704

# ── Global pause flag ──────────────────────────────────────────────────────
_scheduler_paused: bool = False

# ── Running process registry (user_id → Popen) ────────────────────────────
_running_proc: dict[int, subprocess.Popen] = {}

# ── Thread pool for subprocess calls ──────────────────────────────────────
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sched-worker")

# ── Step timeout: 15 minutes per step ─────────────────────────────────────
_STEP_TIMEOUT = 900


def _now_utc() -> datetime:
    """Return the current UTC instant.

    Indirection point so tests can freeze time without patching ``datetime``.
    """
    return datetime.now(timezone.utc)


def _display_now(moment: datetime | None = None) -> str:
    """Render *moment* (default: now) in the configured display timezone.

    Shows the real zone label and UTC offset, so a Sydney host is never
    mislabelled as IST.
    """
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.timeutil import format_display

    try:
        zone = load_config().display_timezone
    except Exception:
        zone = "Australia/Sydney"
    return format_display(moment or _now_utc(), zone)

# ── Alert score threshold (configurable via SCHEDULER_SCORE_THRESHOLD env var) ─
SCHEDULER_SCORE_THRESHOLD: int = int(os.environ.get("SCHEDULER_SCORE_THRESHOLD", "70"))

# ── Background task registry (prevents GC from cancelling asyncio tasks) ──
_BACKGROUND_TASKS: set = set()

# Scanner callbacks wake frequently so an exchange opening is not missed and
# bounded failure retries occur promptly. Persistent success cadence is stored
# beside scheduler runs; a single market runs per tick under the WIN shared lock.
_scanner_last_attempted: dict[str, float] = {}


def _keep_task(task: asyncio.Task) -> asyncio.Task:
    """Store task reference so GC cannot cancel it; auto-remove on completion."""
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# ── Alert tracker DB path ──────────────────────────────────────────────────

# ── Base env (same as main.py) ─────────────────────────────────────────────
BASE_ENV: dict = {
    **os.environ,
    "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
    "PYTHONIOENCODING": "utf-8",
    "OPENBLAS_NUM_THREADS": "1",
}


# ═══════════════════════════════════════════════════════════════════════════
# Security helpers
# ═══════════════════════════════════════════════════════════════════════════

def _allowed_users() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _is_allowed(user_id: int) -> bool:
    allowed = _allowed_users()
    if not allowed:
        return True  # open (dev mode)
    return user_id in allowed


async def _auth_gate(update: Update) -> bool:
    uid = update.effective_user.id
    if not _is_allowed(uid):
        await update.effective_message.reply_text(
            f"⛔ Not authorized. Your ID: `{uid}`\n"
            "Add to TELEGRAM_ALLOWED_USERS env var.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Zerodha token helpers
# ═══════════════════════════════════════════════════════════════════════════

def _zerodha_token_ok() -> bool:
    """Return True if a valid (unexpired) Zerodha token is cached."""
    token_file = INDIA / ".zerodha_token_cache"
    if not token_file.exists():
        return False
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
        tok = data.get("token", "") or data.get("access_token", "")
        if not tok:
            return False
        auth_ts = data.get("auth_time")
        if auth_ts:
            age_hours = (time.time() - float(auth_ts)) / 3600.0
            return age_hours < 24.75
        from datetime import date as _date
        return data.get("date", "") == str(_date.today())
    except Exception:
        return False


def _save_zerodha_token(token: str) -> bool:
    """Save a Zerodha access token to the cache file.  Returns True on success."""
    try:
        token_file = INDIA / ".zerodha_token_cache"
        today = datetime.now().strftime("%Y-%m-%d")
        cache = {
            "token":       token,
            "access_token": token,   # keep both keys for compat
            "date":        today,
            "auth_time":   time.time(),
        }
        token_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        # Also inject into the current process env so subprocesses inherit it
        os.environ["ZERODHA_ACCESS_TOKEN"] = token
        return True
    except Exception as exc:
        logger.error("Failed to save Zerodha token: %s", exc)
        return False


def _india_env() -> dict:
    """Build env dict with current Zerodha token injected."""
    env = dict(BASE_ENV)
    token_file = INDIA / ".zerodha_token_cache"
    if token_file.exists():
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
            tok = data.get("token", "") or data.get("access_token", "")
            if tok:
                env["ZERODHA_ACCESS_TOKEN"] = tok
        except Exception:
            pass
    return env


# ═══════════════════════════════════════════════════════════════════════════
# Subprocess helpers
# ═══════════════════════════════════════════════════════════════════════════

def _run_cmd(
    cmd: list[str],
    cwd: Path,
    env: dict | None = None,
    timeout: int = _STEP_TIMEOUT,
) -> tuple[int, str]:
    """Run a command synchronously (blocking, for use inside executor).

    Delegates to :func:`tradingview.scheduler.steps.run_step`, which starts the
    deadline when the process launches and drains output on a reader thread.
    The previous implementation iterated ``proc.stdout`` to EOF *before* calling
    ``proc.wait(timeout=...)``, so a hung child never reached the timed wait.
    """
    from tradingview.scheduler.steps import run_step

    result = run_step(
        label=" ".join(str(part) for part in cmd[:2]),
        cmd=cmd,
        cwd=cwd,
        env=env or BASE_ENV,
        timeout=timeout,
    )
    exit_code = result.exit_code if result.exit_code is not None else -1
    return exit_code, result.output

async def _run_step_scheduled(
    bot,
    chat_id: int,
    label: str,
    cmd: list[str],
    cwd: Path,
    env: dict | None = None,
    report_path: Path | None = None,
    report_label: str | None = None,
) -> bool:
    """Run scheduled maintenance silently; notify only on failure."""
    from tradingview.scheduler import formatting

    loop = asyncio.get_running_loop()
    try:
        rc, output = await loop.run_in_executor(
            _executor, lambda: _run_cmd(cmd, cwd, env)
        )
    except Exception as exc:
        rc, output = -1, str(exc)
    if rc != 0:
        parts = formatting.build_parts([formatting.Block(lines=[
            formatting.render_error_note(label, f"failed (exit {rc})"),
            formatting.render_error_note("Details", output[-400:]),
        ])])
        await formatting.send_parts(bot, chat_id, parts)
    logger.info("Scheduled step %s finished with exit %s", label, rc)
    return rc == 0


async def _send_report_scheduled(bot, chat_id: int, path: Path, label: str) -> None:
    """Send a report file as a Telegram document (scheduled context)."""
    if not path.exists():
        await bot.send_message(
            chat_id=chat_id,
            text=f"📂 _{label}_ — file not found.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if path.stat().st_size == 0:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📂 _{label}_ — empty file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        with open(path, "rb") as fh:
            await bot.send_document(
                chat_id=chat_id,
                document=fh,
                filename=path.name,
                caption=f"📊 {label}",
            )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Could not send _{label}_: {exc}",
            parse_mode=ParseMode.MARKDOWN,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Alert summary helpers  (data source: alert_tracker.db + options_signals.json)
# ═══════════════════════════════════════════════════════════════════════════

def _parse_exchange_symbol(tv_symbol: str) -> tuple:
    """Extract (exchange, symbol) from 'SGX:BS6'; bare symbols default to 'US'."""
    if ":" in tv_symbol:
        parts = tv_symbol.split(":", 1)
        return parts[0].upper(), parts[1].upper()
    return "US", tv_symbol.upper()


def _extract_stock_alerts(hours: int = 24) -> list:
    """Return the latest-assessment actionable stock rows (manual/latest view).

    This is the **historical/latest watchlist** used by manual commands such as
    ``/alerts_now``. Automated new-alert delivery does NOT use it: the interval
    pipeline consumes the current run's durable stock payload instead.

    Delegates to :func:`tradingview.scheduler.stock_results.query_latest_assessment_stocks`,
    which compares real UTC instants (``julianday``) rather than ISO text and
    selects the latest assessment per symbol *before* filtering for
    actionability.
    """
    from tradingview.scheduler.stock_results import query_latest_assessment_stocks

    result = query_latest_assessment_stocks(
        _DB_PATH, window_hours=float(hours), limit=20
    )
    if result.status in {"unavailable", "failed"}:
        logger.warning("Stock data %s: %s", result.status, "; ".join(result.errors))
    return result.stocks


def _extract_stock_result(hours: int = 24):
    """Return the full typed stock result for the historical/latest view."""
    from tradingview.scheduler.stock_results import query_latest_assessment_stocks

    return query_latest_assessment_stocks(_DB_PATH, window_hours=float(hours), limit=20)



def _extract_options_alerts(
    threshold: float,
    expected_run_id: str | None = None,
) -> dict:
    """Read current OptionsAnalyzer badge contracts from the structured snapshot."""
    from tradingview.options.options_snapshot import option_quote_fields

    def _error_texts(errors: list) -> list[str]:
        return [
            str(error.get("message", error)) if isinstance(error, dict) else str(error)
            for error in errors
        ]

    snapshot, validation_errors = read_snapshot(
        OPTIONS_SNAPSHOT,
        expected_run_id=expected_run_id,
        max_age_hours=6.0,
    )
    if validation_errors or snapshot is None:
        return {
            "options": [],
            "status": "unavailable",
            "errors": validation_errors or ["options snapshot unavailable"],
        }

    status = snapshot.get("status", "unavailable")
    if status in {"disabled", "failed", "unavailable"}:
        return {
            "options": [],
            "status": status,
            "errors": _error_texts(
                snapshot.get("errors") or [f"options status: {status}"]
            ),
        }

    options = []
    for contract in snapshot.get("contracts", []):
        try:
            score = float(contract["option_score"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(score) or score < threshold:
            continue
        options.append({
            "exchange": contract["exchange"],
            "symbol": contract["symbol"],
            "opt_type": contract["option_type"],
            "strike": contract["strike"],
            "expiry": contract["expiry"],
            "dte": contract["dte"],
            "score": round(score, 1),
            "parent_score": contract["parent_signal_score"],
            "strategy_modes": contract.get("strategy_modes", []),
            **option_quote_fields(contract),
        })
    return {
        "options": options,
        "status": "empty" if not options and status == "success" else status,
        "errors": _error_texts(snapshot.get("errors") or []),
    }


def _format_alert_summary(
    stocks: list,
    options: list,
    threshold: float,
    options_status: str = "success",
    options_errors: list | None = None,
    stock_status: str = "success",
    stock_errors: list | None = None,
    display_timezone: str | None = None,
    analysis_time: datetime | None = None,
) -> list:
    """Format alert data into safe, bounded Telegram message parts.

    Returns a list of :class:`tradingview.scheduler.formatting.MessagePart`
    objects. Dynamic text is HTML-escaped, so an error string containing
    ``run_id``, underscores, brackets, or backticks can no longer break the
    message. Times are rendered in an explicit display timezone with its real
    label instead of a hardcoded ``IST``.
    """
    from tradingview.scheduler import formatting
    from tradingview.scheduler.config import load_config

    try:
        config = load_config()
        zone = display_timezone or config.display_timezone
        limit = config.message_limit
    except Exception:
        zone = display_timezone or "Australia/Sydney"
        limit = formatting.TELEGRAM_SAFE_LIMIT

    from tradingview.scheduler.cycle import CycleResult, build_summary_parts
    from tradingview.scheduler.stock_results import StockResult

    moment = analysis_time or _now_utc()
    result = CycleResult(
        run_id="", started_at=moment.isoformat(), display_timezone=zone,
        stock_result=StockResult(status=stock_status, stocks=stocks, errors=stock_errors or []),
        option_result={"options": options, "status": options_status, "errors": options_errors or []},
    )
    return build_summary_parts(result, limit=limit, analysis_time=moment)


def _chunk_text(text: str, limit: int = 4096) -> list:
    """Split text into <=limit-char chunks, preferring line boundaries."""
    chunks: list = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:          # a single over-long line
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks



async def _post_alert_summary(
    bot,
    chat_id: int,
    threshold: float = None,
    expected_run_id: str | None = None,
    notes: list[str] | None = None,
    stock_result=None,
    option_result: dict | None = None,
) -> None:
    """Post the compact alert summary to a Telegram chat.

    All summary/error paths go through the same centralized formatter and
    sender, so escaping, chunking, and the plain-text fallback apply uniformly.
    A confirmed formatting rejection is retried as plain text; permission and
    token failures are not.

    ``stock_result``/``option_result`` let the interval pipeline pass the
    current run's durable results. When omitted (manual commands), the
    historical/latest watchlist is used and is explicitly labelled as such.
    """
    from tradingview.scheduler import formatting

    if threshold is None:
        threshold = float(SCHEDULER_SCORE_THRESHOLD)

    if stock_result is None:
        stock_result = _extract_stock_result()
    if option_result is None:
        option_result = _extract_options_alerts(threshold, expected_run_id=expected_run_id)

    parts = _format_alert_summary(
        stock_result.stocks,
        option_result["options"],
        threshold,
        option_result["status"],
        option_result["errors"],
        stock_status=stock_result.status,
        stock_errors=stock_result.errors,
    )

    if notes:
        note_lines = [formatting.render_error_note("note", note) for note in notes]
        note_text = "\n".join(note_lines)
        # Append to the last summary part when it still fits; otherwise send the
        # notes as their own independently valid message(s). The summary itself
        # is never pushed over the limit.
        if parts and parts[-1].length(formatting.PARSE_MODE_HTML) + 2 + len(note_text) <= 4096:
            parts[-1].html_text += "\n\n" + formatting.escape_html(note_text)
            parts[-1].plain_text += "\n\n" + note_text
        else:
            parts.extend(
                formatting.build_parts(
                    [formatting.Block(lines=note_lines)],
                    limit=formatting.TELEGRAM_SAFE_LIMIT,
                    label_parts=False,
                )
            )

    receipts = await formatting.send_parts(bot, chat_id, parts)
    failed = [receipt for receipt in receipts if not receipt.delivered]
    if failed:
        logger.warning(
            "Alert summary: %d/%d part(s) failed — %s",
            len(failed), len(receipts),
            "; ".join(receipt.error for receipt in failed)[:500],
        )



# ═══════════════════════════════════════════════════════════════════════════

async def _pipeline_morning(bot, chat_id: int) -> None:
    """Morning run: screener + portfolio + india + signals + sell."""
    # ── Cooldown guard: prevent concurrent morning runs from flooding Telegram ──
    if not try_claim_morning_run("report_scheduler"):
        logger.info("Morning run already in progress; skipping")
        return

    started = time.time()
    env = _india_env()
    zerodha_ok = _zerodha_token_ok()

    # 1. Morning Screener
    await _run_step_scheduled(
        bot, chat_id, "[1] Morning Screener",
        [sys.executable, str(TV / "screeners" / "morning_screener.py"), "--no-open"],
        cwd=TV,
        report_path=REPORTS / "morning_screener_report.html",
        report_label="Morning Screener",
    )

    # 2. Download TradingView fires
    await _run_step_scheduled(
        bot, chat_id, "[2] Downloading TradingView fires",
        [sys.executable, str(TV / "tv_data" / "download_fires.py")],
        cwd=TV,
    )

    # 3. Global Portfolio Dashboard
    await _run_step_scheduled(
        bot, chat_id, "[3] Global Portfolio Dashboard",
        [sys.executable, str(ROOT / "global_portfolio_dashboard.py")],
        cwd=ROOT, env=env,
        report_path=REPORTS / "global_portfolio_dashboard.html",
        report_label="Global Portfolio",
    )

    # 4. India Portfolio Dashboard (only if Zerodha OK)
    if zerodha_ok:
        await _run_step_scheduled(
            bot, chat_id, "[4] India Portfolio Dashboard",
            [sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
            cwd=INDIA, env=env,
            report_path=REPORTS / "india_portfolio_dashboard.html",
            report_label="India Portfolio",
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏭ *[4] India Portfolio* — skipped (no Zerodha token)\n\n"
                "To fix: send `/zerodha_token <your_access_token>` and I'll "
                "run the India dashboard immediately."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    # 5. Signal Stack
    await _run_step_scheduled(
        bot, chat_id, "[5] Signal Stack",
        [
            sys.executable, str(TV / "signal_stack.py"),
            "--n", "100", "--threshold", "62.5", "--no-external",
        ],
        cwd=TV, env=env,
        report_path=REPORTS / "signal_stack_report.html",
        report_label="Signal Stack",
    )


    # 6. Sell Strategy
    await _run_step_scheduled(
        bot, chat_id, "[6] Sell Strategy",
        [sys.executable, str(TV / "sell" / "sell_strategy.py"), "--live"],
        cwd=TV, env=env,
        report_path=REPORTS / "sell_strategy_report.html",
        report_label="Sell Strategy",
    )

    # 7. Options Outcome Check (settle expired US + India options)
    await _run_step_scheduled(
        bot, chat_id, "[7] Options Outcome Check",
        [sys.executable, str(TV / "options" / "outcome_tracker.py")],
        cwd=TV / "options",
    )

    # Post compact alert summary (score > threshold, exchange-grouped)
    await _post_alert_summary(bot, chat_id)

    logger.info("Morning run complete in %.1fs", time.time() - started)
    release_morning_run()  # release slot — next run now allowed


async def _pipeline_evening(bot, chat_id: int) -> None:
    """Evening run: portfolio + india + signals + sell + alerts."""
    started = time.time()
    env = _india_env()
    zerodha_ok = _zerodha_token_ok()

    # 1. Download fires
    await _run_step_scheduled(
        bot, chat_id, "[1] Downloading TradingView fires",
        [sys.executable, str(TV / "tv_data" / "download_fires.py")],
        cwd=TV,
    )

    # 2. Global Portfolio Dashboard
    await _run_step_scheduled(
        bot, chat_id, "[2] Global Portfolio Dashboard",
        [sys.executable, str(ROOT / "global_portfolio_dashboard.py")],
        cwd=ROOT, env=env,
        report_path=REPORTS / "global_portfolio_dashboard.html",
        report_label="Global Portfolio",
    )

    # 3. India Portfolio Dashboard (if Zerodha OK)
    if zerodha_ok:
        await _run_step_scheduled(
            bot, chat_id, "[3] India Portfolio Dashboard",
            [sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
            cwd=INDIA, env=env,
            report_path=REPORTS / "india_portfolio_dashboard.html",
            report_label="India Portfolio",
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏭ *[3] India Portfolio* — skipped (no Zerodha token)\n"
                "Send `/zerodha_token <token>` to recover."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    # 4. Signal Stack
    await _run_step_scheduled(
        bot, chat_id, "[4] Signal Stack",
        [
            sys.executable, str(TV / "signal_stack.py"),
            "--n", "100", "--threshold", "62.5", "--no-external",
        ],
        cwd=TV, env=env,
        report_path=REPORTS / "signal_stack_report.html",
        report_label="Signal Stack",
    )


    # 5. Sell Strategy
    await _run_step_scheduled(
        bot, chat_id, "[5] Sell Strategy",
        [sys.executable, str(TV / "sell" / "sell_strategy.py"), "--live"],
        cwd=TV, env=env,
        report_path=REPORTS / "sell_strategy_report.html",
        report_label="Sell Strategy",
    )

    # 6. Alert Performance (extra in evening)
    await _run_step_scheduled(
        bot, chat_id, "[6] Alert Tracker",
        [sys.executable, str(TV / "alert_tracker.py"), "--all"],
        cwd=TV,
        report_path=REPORTS / "alert_performance_report.html",
        report_label="Alert Performance",
    )

    # Post compact alert summary (score > threshold, exchange-grouped)
    await _post_alert_summary(bot, chat_id)

    logger.info("Evening run complete in %.1fs", time.time() - started)


async def _broadcast_run(bot, pipeline_fn) -> None:
    """Run a pipeline and broadcast to all authorized users."""
    users = _allowed_users()
    if not users:
        logger.warning("No authorized users — skipping broadcast")
        return
    for uid in users:
        try:
            await pipeline_fn(bot, uid)
        except Exception as exc:
            logger.error("Broadcast to %s failed: %s", uid, exc)


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled job callbacks
# ═══════════════════════════════════════════════════════════════════════════

async def _job_zerodha_warning(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pre-market warning: alert if Zerodha token is missing/expired."""
    if _zerodha_token_ok():
        return
    for uid in _allowed_users():
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "⚠️ *Zerodha Token Alert*\n\n"
                    "Your Zerodha access token is *missing or expired*\\.\n\n"
                    "*Option 1 \\(easiest\\):* Send your access token here:\n"
                    "`/zerodha_token YOUR_ACCESS_TOKEN`\n\n"
                    "*Option 2:* Run on your PC:\n"
                    "`python india\\-brokers/zerodha_login\\.py`\n\n"
                    "The morning run starts at 08:30 IST\\.\n"
                    "India dashboard will be skipped until token is provided\\."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as exc:
            logger.error("Zerodha warning to %s failed: %s", uid, exc)


async def _job_morning_run(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled morning run."""
    global _scheduler_paused
    if _scheduler_paused:
        logger.info("Scheduler paused — skipping morning run")
        return
    logger.info("Starting scheduled morning run")
    await _broadcast_run(context.bot, _pipeline_morning)


async def _job_evening_run(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled evening run."""
    global _scheduler_paused
    if _scheduler_paused:
        logger.info("Scheduler paused — skipping evening run")
        return
    logger.info("Starting scheduled evening run")
    await _broadcast_run(context.bot, _pipeline_evening)


def _scheduler_outbox():
    """Return the durable notification outbox (lazily constructed)."""
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory

    config = load_config()
    path = config.outbox_path or (ROOT / "database" / "notification_outbox.db")
    return NotificationOutbox(
        sqlite_factory(path),
        max_attempts=config.max_attempts,
        backoff_base_seconds=config.backoff_base_seconds,
        backoff_cap_seconds=config.backoff_cap_seconds,
        claim_lease_seconds=config.claim_lease_seconds,
        alert_ttl_hours=config.alert_ttl_hours,
    )


def _cycle_paths(run_id: str):
    """Return run-scoped output paths for *run_id*."""
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.cycle import CyclePaths

    config = load_config()
    root = config.run_root or (ROOT / "reports" / "scheduler_runs")
    return CyclePaths(root, run_id)


async def _produce_signal_cycle(run_id: str):
    """Produce one cycle result (analysis runs exactly once)."""
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.cycle import produce_cycle

    loop = asyncio.get_running_loop()
    config = load_config()
    paths = _cycle_paths(run_id)

    def _run(label, cmd, cwd=None, env=None, timeout=900, **kwargs):
        from tradingview.scheduler.steps import run_step

        return run_step(label, cmd, cwd=cwd, env=env, timeout=timeout)

    from tradingview.scheduler.cycle import CycleLock

    lock = CycleLock(
        (config.run_root or (ROOT / "reports" / "scheduler_runs")) / ".cycle.lock",
        stale_seconds=config.step_timeout * 3,
    )
    return await loop.run_in_executor(
        _executor,
        lambda: produce_cycle(
            run_id=run_id,
            paths=paths,
            config=config,
            python_executable=sys.executable,
            project_root=ROOT,
            env=_india_env(),
            runner=_run,
            lock=lock,
        ),
    )


async def _pipeline_signal_only(bot, chat_id: int) -> None:
    """Signal-only run for a single recipient (manual/legacy entry point).

    Produces one cycle result and delivers it to *chat_id*. The interval job
    uses :func:`_run_signal_cycle_broadcast` instead, which produces once and
    delivers to every recipient.
    """
    run_id = _new_run_id()
    result = await _produce_signal_cycle(run_id)
    await _deliver_cycle_result(bot, result, [chat_id])


def _new_run_id() -> str:
    from tradingview.scheduler.cycle import new_run_id

    return new_run_id()


async def _deliver_cycle_result(bot, result, recipients: list[int]) -> None:
    """Deliver one immutable cycle result to every recipient."""
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    config = load_config()
    outbox = _scheduler_outbox()
    enqueue_cycle(
        outbox, result, recipients, limit=config.message_limit,
        scanner_option_cap=(config.scanner_us_options_max_contracts_per_symbol
                            if getattr(config, "scanner_us_options_enabled", False) else None),
    )
    receipts = await deliver_cycle(
        outbox, result, recipients, bot=bot, limit=config.message_limit
    )
    counts = outbox.counts()
    logger.info(
        "Cycle %s complete: status=%s stocks=%d options=%d recipients=%d outbox=%s",
        result.run_id, result.status, len(result.stocks), len(result.options),
        len(recipients), counts,
    )
    for recipient, recipient_receipts in receipts.items():
        failed = [r for r in recipient_receipts if not r.delivered]
        if failed:
            logger.warning(
                "Delivery to %s: %d/%d part(s) failed",
                recipient, len(failed), len(recipient_receipts),
            )


async def _run_signal_cycle_broadcast(bot) -> None:
    """Produce one cycle and deliver it to all recipients.

    Analysis runs exactly once regardless of recipient count, so a second
    recipient cannot consume the new fires before the first is served.
    """
    from tradingview.scheduler.identity import normalize_recipients

    recipients = normalize_recipients(_allowed_users())
    if not recipients:
        logger.warning("No authorized recipients — skipping cycle (no analysis run)")
        return

    run_id = _new_run_id()
    result = await _produce_signal_cycle(run_id)
    await _deliver_cycle_result(bot, result, recipients)


def _reconcile_durable_work(recipients: list[int]) -> int:
    """Enqueue notification work for durable results lacking a delivery record.

    Covers the crash-after-commit-before-enqueue window. Idempotent: the
    per-fire delivery identity is unique, so repeated runs cannot duplicate.
    """
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.dispatch import reconcile, reconcile_cycle_results

    config = load_config()
    outbox = _scheduler_outbox()
    queued = 0
    try:
        from database.fires_ledger import _conn as _ledger_conn

        with _ledger_conn() as conn:
            queued += reconcile(
                outbox, conn, recipients, ttl_hours=config.alert_ttl_hours
            )
    except Exception as exc:
        logger.warning("Scoring-result reconciliation failed: %s", exc)

    try:
        root = config.run_root or (ROOT / "reports" / "scheduler_runs")
        queued += reconcile_cycle_results(
            outbox, root, recipients, ttl_hours=config.alert_ttl_hours
        )
    except Exception as exc:
        logger.warning("Cycle-result reconciliation failed: %s", exc)

    if queued:
        logger.info("Reconciliation enqueued %d pending notification(s)", queued)
    return queued


async def _dispatch_pending_outbox(bot) -> None:
    """Drain due outbox rows, including rows left by earlier runs.

    Registered as a startup job and a periodic job so a failed send is retried
    automatically. Independent of the weekday analysis gate; subject to the
    row's own freshness deadline and to the pause flag.
    """
    from tradingview.scheduler.dispatch import dispatch_pending

    outbox = _scheduler_outbox()
    report = await dispatch_pending(outbox, bot=bot, limit=20)
    if report.claimed or report.expired:
        logger.info(
            "Outbox dispatch: claimed=%d sent=%d failed=%d ambiguous=%d expired=%d",
            report.claimed, report.sent, report.failed, report.ambiguous,
            report.expired,
        )
    for error in report.errors:
        logger.warning("Outbox dispatch error: %s", error)


async def _job_outbox_dispatch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic pending-outbox dispatcher (registered job)."""
    global _scheduler_paused
    if _scheduler_paused:
        logger.info("Scheduler paused — skipping outbox dispatch")
        return
    await _dispatch_pending_outbox(context.bot)


async def _job_startup_recovery(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Startup recovery: reconcile durable work, then drain the outbox.

    Runs once shortly after startup so a restart recovers pending notifications
    without waiting for the next analysis cycle.
    """
    global _scheduler_paused
    if _scheduler_paused:
        logger.info("Scheduler paused — skipping startup recovery")
        return
    from tradingview.scheduler.identity import normalize_recipients

    recipients = normalize_recipients(_allowed_users())
    if recipients:
        _reconcile_durable_work(recipients)
    await _dispatch_pending_outbox(context.bot)



async def _job_interval_run(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every-N-hours run (when SCHEDULER_EVERY_HOURS is set). Signal Stack + alert only.

    The configured weekday calendar is enforced here, evaluated in
    ``SCHEDULER_TIMEZONE`` rather than on the host clock. Pending outbox
    delivery is independent of this gate: a queued alert is still retried on an
    excluded analysis day, subject to its own freshness deadline.
    """
    global _scheduler_paused
    if _scheduler_paused:
        logger.info("Scheduler paused — skipping interval run")
        return

    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.cycle import should_run_analysis

    try:
        config = load_config()
    except Exception as exc:  # configuration error must not kill the job
        logger.error("Interval run skipped — invalid configuration: %s", exc)
        return

    now = _now_utc()
    if not should_run_analysis(now, config.days, config.schedule_timezone):
        logger.info(
            "Interval run skipped — %s is not an allowed analysis day in %s",
            now.astimezone(__import__("zoneinfo").ZoneInfo(config.schedule_timezone)).strftime("%a"),
            config.schedule_timezone,
        )
        # Pending delivery is independent of the analysis calendar: still drain
        # the outbox so a queued alert is retried (subject to its own expiry).
        await _dispatch_pending_outbox(context.bot)
        return

    logger.info("Starting interval-scheduled run (signal-only mode)")
    await _run_signal_cycle_broadcast(context.bot)
    # Retry anything still pending from this or an earlier cycle.
    await _dispatch_pending_outbox(context.bot)


async def _run_scanner_cycle(bot, markets: tuple[str, ...], *, force: bool = False):
    """Produce and deliver a scanner cycle through the durable shared outbox."""
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.cycle import CycleLock
    from tradingview.scheduler.identity import normalize_recipients
    from tradingview.scheduler.scanner import produce_scanner_cycle
    from dataclasses import replace

    config = load_config()
    recipients = normalize_recipients(_allowed_users())
    if not recipients:
        logger.warning("No authorized recipients — skipping scanner cycle")
        return None
    run_id = f"scanner_{_new_run_id()}"
    root = config.run_root or (ROOT / "reports" / "scheduler_runs")
    if config.scanner_scope == "test":
        # Keep test artifacts outside the production recovery scan root.
        root = root.parent / f"{root.name}_scanner_test_scope"
        config = replace(config, run_root=root)
    lock = CycleLock(
        root / ".cycle.lock",
        stale_seconds=max(config.step_timeout, config.scanner_step_timeout) * 3,
    )
    loop = asyncio.get_running_loop()

    def _run(label, cmd, cwd=None, env=None, timeout=900, **kwargs):
        from tradingview.scheduler.steps import run_step
        return run_step(label, cmd, cwd=cwd, env=env, timeout=timeout)

    result = await loop.run_in_executor(
        _executor,
        lambda: produce_scanner_cycle(
            run_id=run_id, config=replace(config, scanner_markets=tuple(markets)),
            project_root=ROOT, python_executable=sys.executable, runner=_run,
            now=_now_utc(), clock=_now_utc, force=force, lock=lock,
        ),
    )
    # Persist/queue only informative scanner cycles. Valid empty/off-hours
    # results remain in cycle_result.json and per-market status logs without a
    # Telegram no-new message every scan interval.
    failed = result.status in {"failed", "partial"} and any(
        value.get("status") in {"failed", "partial"}
        or value.get("options_status") in {"failed", "unavailable", "partial"}
        for value in result.scanner_markets.values()
    )
    if config.scanner_scope == "production" and (result.has_new_actionable or failed):
        await _deliver_cycle_result(bot, result, recipients)
    logger.info(
        "Scanner cycle %s complete: status=%s markets=%s actionable=%d",
        result.run_id, result.status, result.scanner_markets, len(result.stocks),
    )
    return result


async def _job_scanner_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered scanner callback, independent of the WIN weekday gate."""
    global _scheduler_paused
    if _scheduler_paused:
        return
    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.scanner import market_session, market_session_bounds
    from tradingview.scheduler.freshness import BAR_INTERVAL_MINUTES
    from tradingview.scheduler.scanner_state import load_cadence, save_cadence

    try:
        config = load_config()
    except Exception as exc:
        logger.error("Scanner tick skipped — invalid configuration: %s", exc)
        return
    if not config.scanner_markets:
        return
    # Automatic cadence state is production state. Test-scope runs are only
    # available through an explicit manual invocation and never persist cadence.
    if config.scanner_scope == "test":
        return
    now = _now_utc()
    run_root = config.run_root or (ROOT / "reports" / "scheduler_runs")
    state_path = run_root / "scanner_cadence.json"
    cadence = load_cadence(state_path)
    due = []
    interval = config.scanner_every_hours * 3600
    for market in config.scanner_markets:
        state = cadence.get(market, {})
        now_ts = now.timestamp()
        retry_at = float(state.get("retry_at", 0) or 0)
        if retry_at > now_ts:
            continue
        try:
            opened, _session_date = market_session(market, now)
        except Exception as exc:
            logger.error("Scanner %s calendar lookup failed: %s", market, exc)
            due.append(market)
            continue
        if not opened:
            continue
        # Allow the first complete chart bar plus the configured provider delay.
        # This is a scheduling grace period, never permission to accept stale data.
        session_open, _ = market_session_bounds(market, _session_date)
        if (now - session_open).total_seconds() < 60 * (
            BAR_INTERVAL_MINUTES + config.scanner_provider_delay_minutes
        ):
            continue
        last_success = float(state.get("last_success", 0) or 0)
        if retry_at > now_ts or (last_success and now_ts - last_success < interval):
            continue
        due.append(market)
    if not due:
        return
    # Keep the global WIN/scanner lock around one bounded market subprocess.
    # This prevents one slow market from holding the shared lock for a batch.
    due.sort(key=lambda market: (
        float(cadence.get(market, {}).get("last_attempt", 0) or 0),
        config.scanner_markets.index(market),
    ))
    market = due[0]
    result = await _run_scanner_cycle(context.bot, (market,))
    # A busy shared lock is transient contention, not an attempted market scan.
    if result is not None and not any("shared execution lock" in e for e in result.errors):
        stamp = _now_utc().timestamp()
        market_status = result.scanner_markets.get(market, {}).get("status")
        state = dict(cadence.get(market, {}))
        state["last_attempt"] = stamp
        if market_status in {"success", "no_new", "empty", "ok"}:
            state.update({"last_success": stamp, "failure_count": 0, "retry_at": 0})
        else:
            failures = int(state.get("failure_count", 0) or 0) + 1
            delay = min(
                config.scanner_retry_base_seconds * (2 ** min(failures - 1, 20)),
                config.scanner_retry_cap_seconds,
            )
            state.update({
                "failure_count": failures, "retry_at": stamp + delay,
            })
        cadence[market] = state
        _scanner_last_attempted[market] = stamp
        try:
            save_cadence(state_path, cadence)
        except OSError as exc:
            logger.error("Could not persist scanner cadence state: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# Command handlers
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    name = update.effective_user.first_name or "User"
    auth = _is_allowed(uid)
    print(f"\n{'='*60}\n  /start from {name} (ID: {uid})\n{'='*60}")
    await update.effective_message.reply_text(
        f"👋 *Hi {name}!*\n\n"
        f"*AjaazAlert Scheduler* is running.\n"
        f"Your ID: `{uid}` | Status: {'✅ Authorized' if auth else '⛔ Not authorized'}\n\n"
        "This bot delivers scheduled trading reports automatically.\n\n"
        "Use /help to see all commands.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🌅 Run Morning Now", callback_data="run_morning"),
                InlineKeyboardButton("🌆 Run Evening Now", callback_data="run_evening"),
            ],
            [
                InlineKeyboardButton("🔌 Status",  callback_data="status"),
                InlineKeyboardButton("⏭ Next Run", callback_data="next"),
            ],
            [
                InlineKeyboardButton("⏸ Pause",  callback_data="pause"),
                InlineKeyboardButton("▶️ Resume", callback_data="resume"),
            ],
        ]),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_gate(update):
        return
    paused_note = "\n⏸ *Scheduler is currently PAUSED*\n" if _scheduler_paused else ""
    await update.effective_message.reply_text(
        "📱 *AjaazAlert Scheduler — Commands*\n"
        + paused_note + "\n"
        "🌅 *Manual Runs*\n"
        "  `/run_morning` — trigger morning run now\n"
        "  `/run_evening` — trigger evening run now\n"
        "  `/run_now`     — same as /run_morning\n"
        "  `/scan_now`    — run the configured TradingView market scanner now\n"
        "  `/alerts_now`  — post alert summary (score ≥ threshold)\n"
        "  `/alerts_now 80` — override threshold to 80\n\n"
        "🔑 *Zerodha Auth Recovery*\n"
        "  `/zerodha_token <token>` — update access token\n"
        "  `/zerodha_check`         — test if token is valid\n\n"
        "⏰ *Scheduler Control*\n"
        "  `/next`   — show next scheduled run times\n"
        "  `/pause`  — pause automatic runs\n"
        "  `/resume` — resume automatic runs\n\n"
        "📊 *Status*\n"
        "  `/status` — broker & token health\n"
        "  `/id`     — show your Telegram user ID\n\n"
        f"_Alert threshold: >{SCHEDULER_SCORE_THRESHOLD} (override with SCHEDULER\\_SCORE\\_THRESHOLD env var)_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    print(f"\nTelegram user ID: {uid}  (add to TELEGRAM_ALLOWED_USERS)")
    await update.effective_message.reply_text(
        f"👤 Your Telegram ID: `{uid}`\n\n"
        f"To authorize: `$env:TELEGRAM_ALLOWED_USERS = \"{uid}\"`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_gate(update):
        return
    msg = await update.effective_message.reply_text("🔍 Checking status…")

    def _check_port(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True
        except (OSError, TimeoutError):
            return False

    lines = ["🔌 *Broker & Token Status*\n"]

    # Moomoo OpenD
    for port, label in [(11111, "Moomoo AU"), (11112, "Moomoo SG")]:
        icon = "✅" if _check_port("127.0.0.1", port) else "❌"
        lines.append(f"{icon} {label} (:{port})")

    # IBKR TWS
    lines.append(
        "✅ IBKR TWS (live)" if _check_port("127.0.0.1", 7496)
        else "⚪ IBKR TWS (offline — Flex OK)"
    )

    # Zerodha
    if _zerodha_token_ok():
        tf = INDIA / ".zerodha_token_cache"
        try:
            data = json.loads(tf.read_text(encoding="utf-8"))
            auth_ts = data.get("auth_time", 0)
            age_h = (time.time() - float(auth_ts)) / 3600.0
            lines.append(f"✅ Zerodha (token age: {age_h:.1f}h)")
        except Exception:
            lines.append("✅ Zerodha (token valid)")
    else:
        lines.append("❌ Zerodha (token missing/expired — send /zerodha_token <token>)")

    lines.append("✅ AngelOne (TOTP auto-auth)")
    lines.append("✅ Webull (PDF statements)")
    lines.append("✅ IBKR Flex (HTTPS)")

    # Scheduler state
    lines.append("")
    lines.append(f"⏰ Scheduler: {'⏸ PAUSED' if _scheduler_paused else '▶️ Running'}")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the actual registered next-run times where available.

    Times are rendered in the configured display timezone with its real label
    and offset. The schedule itself is evaluated in ``SCHEDULER_TIMEZONE``.
    """
    if not await _auth_gate(update):
        return

    from tradingview.scheduler.config import load_config
    from tradingview.scheduler.timeutil import format_display, load_timezone

    try:
        config = load_config()
        schedule_zone = load_timezone(config.schedule_timezone)
        display_zone = config.display_timezone
    except Exception:
        schedule_zone = load_timezone("Asia/Kolkata")
        display_zone = "Australia/Sydney"

    now_local = _now_utc().astimezone(schedule_zone)
    every_hours = _get_interval_hours()

    lines = ["⏰ *Next Scheduled Runs*\n"]
    if every_hours:
        lines.append(f"• Interval mode: every {every_hours}h (first run 60s after startup)")
        lines.append(f"• Analysis days: {_format_days(config.days)}")
        lines.append(f"• Schedule timezone: {config.schedule_timezone}")
    else:
        from datetime import timedelta

        morning_h, morning_m = _parse_time_env("SCHEDULER_MORNING_IST", "08:30")
        evening_h, evening_m = _parse_time_env("SCHEDULER_EVENING_IST", "17:30")
        today = now_local.date()
        morning_dt = datetime(
            today.year, today.month, today.day, morning_h, morning_m, tzinfo=schedule_zone
        )
        evening_dt = datetime(
            today.year, today.month, today.day, evening_h, evening_m, tzinfo=schedule_zone
        )

        def _next_occurrence(target_dt):
            return target_dt if target_dt > now_local else target_dt + timedelta(days=1)

        lines.append(f"• 🌅 Morning: {format_display(_next_occurrence(morning_dt), display_zone)}")
        lines.append(f"• 🌆 Evening: {format_display(_next_occurrence(evening_dt), display_zone)}")

    lines.append(f"\nScheduler: {'⏸ PAUSED' if _scheduler_paused else '▶️ Active'}")
    lines.append(f"Now: {format_display(_now_utc(), display_zone)}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _format_days(days) -> str:
    """Render weekday numbers as a readable list."""
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return ", ".join(names[day] for day in days if 0 <= day <= 6) or "none"


async def cmd_run_morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_gate(update):
        return
    uid = update.effective_user.id
    await update.effective_message.reply_text("⚡ Starting morning run now…")
    _keep_task(asyncio.create_task(_pipeline_morning(context.bot, uid)))


async def cmd_run_evening(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_gate(update):
        return
    uid = update.effective_user.id
    await update.effective_message.reply_text("⚡ Starting evening run now…")
    _keep_task(asyncio.create_task(_pipeline_evening(context.bot, uid)))


async def cmd_run_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_run_morning(update, context)


async def cmd_scan_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run configured TradingView scanner markets immediately, including off-hours."""
    if not await _auth_gate(update):
        return
    from tradingview.scheduler.config import load_config

    config = load_config()
    if not config.scanner_markets:
        await update.effective_message.reply_text(
            "No scanner markets are enabled. Set SCHEDULER_SCANNER_MARKETS to US,AU,IN,SG."
        )
        return
    await update.effective_message.reply_text(
        f"🔎 Starting TradingView scanner for {', '.join(config.scanner_markets)}…"
    )
    _keep_task(asyncio.create_task(_manual_scanner_run(
        context.bot, update.effective_chat.id, config.scanner_markets,
    )))


async def _manual_scanner_run(bot, chat_id: int, markets: tuple[str, ...]) -> None:
    """Run /scan_now and always report completion or shared-lock contention."""
    result = await _run_scanner_cycle(bot, markets, force=True)
    if result is None:
        await bot.send_message(chat_id=chat_id, text="Scanner skipped: no authorized recipients are configured.")
    elif any("shared execution lock" in error for error in result.errors):
        await bot.send_message(
            chat_id=chat_id,
            text="Scanner could not start because another analysis cycle holds the shared lock. "
                 "Scheduled scanning will retry on its next tick; use /scan_now to retry manually.",
        )
    else:
        statuses = ", ".join(
            f"{market} {item['status']} ({item.get('count', 0)} alerts)"
            for market, item in result.scanner_markets.items()
        ) or result.status
        await bot.send_message(chat_id=chat_id, text=f"Scanner complete: {statuses}.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _scheduler_paused
    if not await _auth_gate(update):
        return
    _scheduler_paused = True
    await update.effective_message.reply_text(
        "⏸ *Scheduler paused*\nAutomatic runs are disabled.\nSend /resume to re-enable.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _scheduler_paused
    if not await _auth_gate(update):
        return
    _scheduler_paused = False
    await update.effective_message.reply_text(
        "▶️ *Scheduler resumed*\nAutomatic runs are enabled.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_zerodha_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a Zerodha access token via Telegram and save it to cache."""
    if not await _auth_gate(update):
        return
    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "🔑 *Zerodha Token Update*\n\n"
            "Usage: `/zerodha_token YOUR_ACCESS_TOKEN`\n\n"
            "To get your access token:\n"
            "1. Log in to [kite.zerodha.com](https://kite.zerodha.com)\n"
            "2. Open DevTools → Network → look for `access_token` in a request\n"
            "   OR run: `python india-brokers/zerodha_login.py` on your PC\n\n"
            "*The token looks like:* `XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return

    token = args[0].strip()
    if len(token) < 20:
        await update.effective_message.reply_text(
            "⚠️ Token looks too short. Please check and try again.\n"
            "Zerodha access tokens are typically 32+ characters.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if _save_zerodha_token(token):
        await update.effective_message.reply_text(
            "✅ *Zerodha token saved!*\n\n"
            "India Portfolio Dashboard will be included in the next run.\n"
            "Want to run it right now?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🇮🇳 Run India Now", callback_data="india_now"),
                    InlineKeyboardButton("🌅 Full Morning Run", callback_data="run_morning"),
                ],
            ]),
        )
    else:
        await update.effective_message.reply_text(
            "❌ Failed to save token — check bot logs.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_zerodha_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_gate(update):
        return
    ok = _zerodha_token_ok()
    if ok:
        tf = INDIA / ".zerodha_token_cache"
        try:
            data = json.loads(tf.read_text(encoding="utf-8"))
            auth_ts = data.get("auth_time", 0)
            age_h = (time.time() - float(auth_ts)) / 3600.0
            remain_h = max(0, 24.75 - age_h)
            await update.effective_message.reply_text(
                f"✅ *Zerodha token is valid*\n\n"
                f"Age: {age_h:.1f}h | Expires in: ~{remain_h:.1f}h\n"
                f"Date: {data.get('date', '?')}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await update.effective_message.reply_text("✅ Zerodha token is valid.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.effective_message.reply_text(
            "❌ *Zerodha token is missing or expired*\n\n"
            "Send `/zerodha_token YOUR_ACCESS_TOKEN` to recover.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def on_button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button callbacks."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not _is_allowed(uid):
        await query.answer("Not authorized", show_alert=True)
        return
    data = query.data
    if data == "run_morning":
        await query.message.reply_text("⚡ Starting morning run now…")
        _keep_task(asyncio.create_task(_pipeline_morning(context.bot, uid)))
    elif data == "run_evening":
        await query.message.reply_text("⚡ Starting evening run now…")
        _keep_task(asyncio.create_task(_pipeline_evening(context.bot, uid)))
    elif data == "status":
        # BUG-2 fix: Update.message is None in callback context; use effective_message
        await cmd_status(update, context)
    elif data == "next":
        # BUG-2 fix: same
        await cmd_next(update, context)
    elif data == "pause":
        global _scheduler_paused
        _scheduler_paused = True
        await query.message.reply_text("⏸ Scheduler paused.")
    elif data == "resume":
        _scheduler_paused = False
        await query.message.reply_text("▶️ Scheduler resumed.")
    elif data == "india_now":
        await query.message.reply_text("🇮🇳 Running India Dashboard…")
        env = _india_env()
        _keep_task(asyncio.create_task(
            _run_step_scheduled(
                context.bot, uid, "India Portfolio Dashboard",
                [sys.executable, str(INDIA / "india_portfolio_dashboard.py")],
                cwd=INDIA, env=env,
                report_path=REPORTS / "india_portfolio_dashboard.html",
                report_label="India Portfolio",
            )
        ))


async def cmd_alerts_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post the current alert summary on demand.

    Usage:  /alerts_now          — uses default threshold (SCHEDULER_SCORE_THRESHOLD)
            /alerts_now 80       — override threshold to 80
    """
    if not await _auth_gate(update):
        return
    threshold = float(SCHEDULER_SCORE_THRESHOLD)
    if context.args:
        try:
            threshold = float(context.args[0])
        except ValueError:
            await update.effective_message.reply_text(
                "⚠️ Invalid threshold. Usage: `/alerts_now [score]`\n"
                f"Example: `/alerts_now 75`\n\nUsing default: {threshold:.0f}",
                parse_mode=ParseMode.MARKDOWN,
            )
    msg = await update.effective_message.reply_text(
        f"🔍 Building alert summary (score ≥ {threshold:.0f})…"
    )
    try:
        await msg.delete()
    except Exception:
        pass
    await _post_alert_summary(context.bot, update.effective_chat.id, threshold)


# ═══════════════════════════════════════════════════════════════════════════
# Schedule helpers
# ═══════════════════════════════════════════════════════════════════════════

def _parse_time_env(env_key: str, default: str) -> tuple[int, int]:
    """Parse HH:MM from an env var.  Returns (hour, minute)."""
    raw = os.environ.get(env_key, default).strip()
    try:
        parts = raw.split(":")
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        default_parts = default.split(":")
        return int(default_parts[0]), int(default_parts[1])


def _get_interval_hours() -> int | None:
    """Return SCHEDULER_EVERY_HOURS as int, or None if not set."""
    raw = os.environ.get("SCHEDULER_EVERY_HOURS", "").strip()
    if raw.isdigit():
        n = int(raw)
        return n if n > 0 else None
    return None


def _parse_days_env() -> tuple[int, ...]:
    """Parse SCHEDULER_DAYS (e.g. '1,2,3,4,5') → tuple of int (0=Mon … 6=Sun)."""
    raw = os.environ.get("SCHEDULER_DAYS", "1,2,3,4,5").strip()
    days: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            # Telegram uses 0=Sun…6=Sat; pytz days=(0,1…6) where 0=Mon?
            # APScheduler / python-telegram-bot JobQueue uses:
            #   days=(0,1,2,3,4,5,6) where 0=Mon, 6=Sun
            d = int(part)
            if 1 <= d <= 7:
                days.append(d - 1)  # convert 1=Mon→0, 7=Sun→6
    return tuple(days) if days else (0, 1, 2, 3, 4)


async def _job_options_premium_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 3h: fetch current_premium for all open non-expired US options.

    Skipped automatically outside US market hours (UTC 13:00–22:00 = EDT 09:00–18:00).
    Also detects PREMIUM_WIN (>= 3× entry premium) and closes those records.
    Runs in addition to — not instead of — the morning / evening pipelines.
    """
    global _scheduler_paused
    if _scheduler_paused:
        return
    import datetime as _dt
    _utc_h = _dt.datetime.utcnow().hour
    if not (13 <= _utc_h <= 22):  # outside US market hours
        logger.info(
            "Options premium update: outside US market hours (%02d:xx UTC), skipping",
            _utc_h,
        )
        return
    logger.info("Starting scheduled options premium update (every-3h job)")
    for uid in _allowed_users():
        await _run_step_scheduled(
            context.bot, uid, "💹 Options Premium Update",
            [sys.executable, str(TV / "options" / "options_outcome_checker.py"),
             "--update-premiums"],
            cwd=TV / "options",
        )


def _setup_job_queue(app) -> None:
    """Register all scheduled jobs with the app's JobQueue."""
    import pytz
    tz = pytz.timezone("Asia/Kolkata")
    days = _parse_days_env()
    every_hours = _get_interval_hours()

    warn_h, warn_m = _parse_time_env("SCHEDULER_ZERODHA_WARN", "08:15")
    morning_h, morning_m = _parse_time_env("SCHEDULER_MORNING_IST", "08:30")
    evening_h, evening_m = _parse_time_env("SCHEDULER_EVENING_IST", "17:30")

    # Zerodha warning job — disabled by user request (2026-09-28): only Signal Stack
    # should run automatically. Re-enable by uncommenting below if needed again.
    # app.job_queue.run_daily(
    #     _job_zerodha_warning,
    #     time=dtime(hour=warn_h, minute=warn_m, tzinfo=tz),
    #     days=days,
    #     name="zerodha_warning",
    # )

    if every_hours:
        # Interval mode: run every N hours starting from now
        logger.info("Scheduler: interval mode every %dh", every_hours)
        app.job_queue.run_repeating(
            _job_interval_run,
            interval=every_hours * 3600,
            first=60,  # first run after 60s (startup delay)
            name="interval_run",
        )
    else:
        # Morning + Evening mode
        logger.info(
            "Scheduler: morning=%02d:%02d IST, evening=%02d:%02d IST, days=%s",
            morning_h, morning_m, evening_h, evening_m, days,
        )
        app.job_queue.run_daily(
            _job_morning_run,
            time=dtime(hour=morning_h, minute=morning_m, tzinfo=tz),
            days=days,
            name="morning_run",
        )
        app.job_queue.run_daily(
            _job_evening_run,
            time=dtime(hour=evening_h, minute=evening_m, tzinfo=tz),
            days=days,
            name="evening_run",
        )

    # Options: premium refresh every 3h — always active (market-hours guard inside job)
    app.job_queue.run_repeating(
        _job_options_premium_update,
        interval=3 * 3600,
        first=600,   # first fire 10min after bot startup
        name="options_premium_update",
    )

    # Startup recovery: reconcile durable work and drain the outbox once, shortly
    # after startup, so a restart recovers pending notifications without waiting
    # for the next analysis cycle.
    app.job_queue.run_once(
        _job_startup_recovery,
        when=15,
        name="startup_recovery",
    )

    # Pending-outbox dispatcher: retries failed sends from this or any earlier
    # run. Independent of the weekday analysis gate.
    app.job_queue.run_repeating(
        _job_outbox_dispatch,
        interval=300,   # every 5 minutes
        first=90,
        name="outbox_dispatch",
    )

    # Market-local scanner gate runs independently of SCHEDULER_DAYS and the
    # host timezone. Lock collisions retry on the next five-minute tick.
    app.job_queue.run_repeating(
        _job_scanner_tick,
        interval=300,
        first=120,
        name="tradingview_scanner",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    # Use AJAAZ_ALERT_BOT_TOKEN for the scheduler bot (@AjaazAlertBot).
    # Do NOT fall back to TELEGRAM_BOT_TOKEN — that belongs to telegram_bot.py (@Ajaaz_bot).
    # Using the same token in two processes causes a Telegram polling conflict.
    from tradingview.scheduler.config import MissingCredential, load_config

    try:
        scheduler_config = load_config(require_token=True)
    except MissingCredential as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Hint: source an ignored local secrets file "
            "(scheduler_env.local.sh) or export the variable before starting.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    token = scheduler_config.bot_token

    os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "5986275415")

    allowed = _allowed_users()
    if not allowed:
        print("WARNING: TELEGRAM_ALLOWED_USERS not set — all users can send commands!")
    else:
        print(f"Authorized user IDs: {sorted(allowed)}")

    morning_h, morning_m = _parse_time_env("SCHEDULER_MORNING_IST", "08:30")
    evening_h, evening_m = _parse_time_env("SCHEDULER_EVENING_IST", "17:30")
    every_hours = _get_interval_hours()

    print(f"\n{'='*60}")
    print("  AjaazAlert Report Scheduler — @AjaazAlertBot")
    print(f"{'='*60}")
    if every_hours:
        print(f"  Schedule: interval every {every_hours}h")
        print(f"  Analysis days: {_format_days(scheduler_config.days)}")
        print(f"  Schedule timezone: {scheduler_config.schedule_timezone}")
    else:
        print(
            f"  Morning run: {morning_h:02d}:{morning_m:02d} "
            f"{scheduler_config.schedule_timezone} ({_format_days(scheduler_config.days)})"
        )
        print(
            f"  Evening run: {evening_h:02d}:{evening_m:02d} "
            f"{scheduler_config.schedule_timezone} ({_format_days(scheduler_config.days)})"
        )
    print(f"  Display timezone: {scheduler_config.display_timezone}")
    print(f"  Reports: {ROOT}")
    print(f"{'='*60}\n")

    # BUG-T3: add network timeouts to prevent morning run hanging when
    # Telegram servers are unreachable or slow.
    app = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    if app.job_queue is not None:
        _setup_job_queue(app)
        print("✅ JobQueue active: scheduled runs enabled")
    else:
        print("⚠️  JobQueue not available — install: pip install 'python-telegram-bot[job-queue]'")

    # Error handler
    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        silent = {"TimedOut", "NetworkError", "RetryAfter", "Conflict"}
        if type(err).__name__ in silent:
            return
        logger.error("Bot error: %s: %s", type(err).__name__, err)

    app.add_error_handler(_error_handler)

    # Register commands
    app.add_handler(CommandHandler("start",           cmd_start))
    app.add_handler(CommandHandler("help",            cmd_help))
    app.add_handler(CommandHandler("id",              cmd_id))
    app.add_handler(CommandHandler("status",          cmd_status))
    app.add_handler(CommandHandler("next",            cmd_next))
    app.add_handler(CommandHandler("run_morning",     cmd_run_morning))
    app.add_handler(CommandHandler("run_evening",     cmd_run_evening))
    app.add_handler(CommandHandler("run_now",         cmd_run_now))
    app.add_handler(CommandHandler("alerts_now",      cmd_alerts_now))
    app.add_handler(CommandHandler("scan_now",        cmd_scan_now))
    app.add_handler(CommandHandler("pause",           cmd_pause))
    app.add_handler(CommandHandler("resume",          cmd_resume))
    app.add_handler(CommandHandler("zerodha_token",   cmd_zerodha_token))
    app.add_handler(CommandHandler("zerodha_check",   cmd_zerodha_check))
    app.add_handler(CallbackQueryHandler(on_button_tap))
    app.add_handler(MessageHandler(filters.COMMAND, _unknown_cmd))

    print("Scheduler bot running. Press Ctrl+C to stop.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def _unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await update.effective_message.reply_text(
        "❓ Unknown command. Use /help to see available commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


if __name__ == "__main__":
    main()


