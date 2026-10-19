"""
tradingview/telegram_alerts.py
================================
Send-only Telegram alert dispatcher for the stocks trading system.

Sends signal alerts and morning-run summaries to the user's Telegram chat.
Uses the Telegram Bot API directly via `requests.post` (no interactive loop,
no polling — fire-and-forget).

Requires env vars:
    TELEGRAM_BOT_TOKEN   — Bot API token (same token used by telegram_bot.py)
    TELEGRAM_CHAT_ID     — Target chat / user ID for notifications

If TELEGRAM_BOT_TOKEN is not set, ALL functions silently return (no crash,
no blocking of the morning run or signal pipeline). This is intentional.

Usage from signal_stack.py:
    from tradingview.telegram_alerts import send_hot_alert, is_enabled
    if is_enabled():
        send_hot_alert(symbol=tv_sym, score=comp, signal=signal,
                       l1=l1, l2=l2, l3=l3, l4=l4,
                       buy_vehicle=buy_advice.get("vehicle",""),
                       company=company_name)

Usage from main.py after morning run:
    from tradingview.telegram_alerts import send_morning_summary
    send_morning_summary(reports=report_list, failures=fail_list)
"""

import os
import sys
import time
from typing import Sequence

# ── Windows UTF-8 output ────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Credentials — dynamic reading (env may be set after import) ─────────
# Module-level cache for backward compatibility with external importers.
# All internal functions use _get_token()/_get_chat_id() for live env reading.
_BOT_TOKEN_CACHE: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID_CACHE: str = (
    os.environ.get("TELEGRAM_CHAT_ID", "")
    or os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip()
)

# Backward-compat aliases (external code may reference these directly)
BOT_TOKEN: str = _BOT_TOKEN_CACHE
CHAT_ID: str = _CHAT_ID_CACHE


def _get_token() -> str:
    """Read bot token dynamically — env may be set after this module is imported."""
    return os.environ.get("TELEGRAM_BOT_TOKEN", "") or _BOT_TOKEN_CACHE


def _get_chat_id() -> str:
    """Read chat ID dynamically — env may be set after this module is imported."""
    return (
        os.environ.get("TELEGRAM_CHAT_ID", "")
        or os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip()
        or _CHAT_ID_CACHE
    )


# ── Score thresholds ─────────────────────────────────────────────────────
HOT_ALERT_MIN_SCORE = 60   # minimum composite score for a push notification

# ── Staging mode (deprecated global — use staging=True param in send_morning_summary) ──
# Kept for backward compatibility only. send_morning_summary() no longer reads
# this global. Pass staging=True to the function directly instead.
_STAGING_MODE: bool = False

# ── API settings ─────────────────────────────────────────────────────────
_API_TIMEOUT = 10           # seconds — hard cap on each HTTP request (BUG-T3)
_MAX_RETRIES = 3            # retry attempts on 429 / transient network errors
_RETRY_BACKOFF = (1, 3, 7) # sleep seconds between retries


def is_enabled() -> bool:
    """Return True if Telegram alerts are configured (both token and chat_id set)."""
    return bool(_get_token() and _get_chat_id())


def _send(text: str) -> bool:
    """
    Post a message to the configured chat via Bot API.

    Implements BUG-T3 fix: hard timeout=10 on every request.
    Returns True on success, False on any failure (never raises).
    """
    if not is_enabled():
        # BUG-T5 fix: silently skip when token not configured
        print("[telegram_alerts] Telegram alerts disabled — TELEGRAM_BOT_TOKEN not set",
              flush=True)
        return False

    try:
        import requests
    except ImportError:
        print("[telegram_alerts] WARN: requests not installed — cannot send Telegram message",
              flush=True)
        return False

    url = f"https://api.telegram.org/bot{_get_token()}/sendMessage"
    payload = {
        "chat_id": _get_chat_id(),
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(url, json=payload, timeout=_API_TIMEOUT)  # BUG-T3
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                # Rate limit — respect Retry-After header (always delta-seconds from Telegram)
                try:
                    retry_after = int(resp.headers.get("Retry-After", _RETRY_BACKOFF[attempt]))
                except (ValueError, TypeError):
                    retry_after = _RETRY_BACKOFF[attempt]
                print(f"[telegram_alerts] WARN: rate-limited (429), waiting {retry_after}s",
                      flush=True)
                time.sleep(retry_after)
                continue
            print(f"[telegram_alerts] WARN: API returned {resp.status_code}: {resp.text[:200]}",
                  flush=True)
            return False
        except (requests.Timeout, requests.ConnectionError) as exc:
            # BUG-T3 fix: catch network errors, log warning, continue
            print(f"[telegram_alerts] WARN: Telegram send failed ({type(exc).__name__}: {exc})"
                  " — continuing without notification", flush=True)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF[attempt])
            else:
                return False

    return False


def _score_bucket(score: float) -> str:
    """Return the score bucket label matching signal_stack.py thresholds."""
    if score >= 85:
        return "APEX"
    if score >= 70:
        return "HIGH"
    if score >= 55:
        return "MED"
    return "LOW"


def _format_breakdown(l1: float | None, l2: float | None,
                      l3: float | None, l4: float | None) -> str:
    """
    Build the score breakdown string.
    BUG-T2 fix: omit sub-score when data is unavailable (not show as 0).
    """
    parts = []
    if l1 is not None:
        parts.append(f"L1={l1:.0f}/30")
    if l2 is not None:
        parts.append(f"L2={l2:.0f}/30")
    if l3 is not None:
        parts.append(f"L3={l3:.0f}/20")
    if l4 is not None:
        parts.append(f"L4={l4:.0f}/20")
    return " ".join(parts) if parts else "breakdown unavailable"


def send_hot_alert(
    symbol: str,
    score: float,
    signal: str = "buy",
    l1: float | None = None,
    l2: float | None = None,
    l3: float | None = None,
    l4: float | None = None,
    buy_vehicle: str = "",
    company: str = "",
    enrichers: Sequence[str] | None = None,
) -> bool:
    """
    Send a hot signal alert to Telegram.

    BUG-T2 fix: message includes full L1-L4 breakdown, enrichers, buy vehicle.
    BUG-T5 fix: no-op (returns False) if token not configured.
    BUG-T3 fix: timeout=10 on the underlying HTTP call.

    Parameters
    ----------
    symbol      : TV symbol (e.g. "ASX:BHP")
    score       : composite score (0–100)
    signal      : "buy" | "sell" | etc.
    l1..l4      : sub-scores (None = not computed)
    buy_vehicle : broker routing string (e.g. "Moomoo-AU")
    company     : human-readable company name (e.g. "BHP Group")
    enrichers   : list of enricher names that fired (e.g. ["Moomoo", "IBKR"])
    """
    if not is_enabled():
        return False

    if score < HOT_ALERT_MIN_SCORE:
        return False  # below threshold — skip silently

    bucket = _score_bucket(score)
    breakdown = _format_breakdown(l1, l2, l3, l4)
    company_str = f" ({company})" if company else ""
    action_upper = signal.upper()

    enr_list = list(enrichers) if enrichers else []
    enr_str = f"Enrichers: {', '.join(enr_list)}" if enr_list else "Enrichers: none"
    vehicle_str = f"\n🛒 Buy via: *{buy_vehicle}*" if buy_vehicle else ""

    text = (
        f"🚨 *{action_upper}: {symbol}*{company_str}\n"
        f"Score: *{score:.0f}/100* ({bucket})\n"
        f"📊 `{breakdown}`\n"
        f"{enr_str}"
        f"{vehicle_str}"
    )

    return _send(text)


def send_morning_summary(
    reports: Sequence[str],
    failures: Sequence[str] | None = None,
    staging: bool = False,
) -> bool:
    """
    Send a morning-run completion summary to Telegram.

    Designed to be called from main.py run_morning_all() after all reports
    complete. BUG-T5: silently skips (returns False) if token not set — the
    morning run is NEVER blocked by this function.

    Parameters
    ----------
    reports  : list of successfully generated report names
    failures : list of report names that failed (optional)
    staging  : if True, queue locally instead of sending immediately.
               The queued message can be reviewed and dispatched via
               `python tradingview/telegram_queue.py --list / --flush`.
    """
    if not is_enabled():
        return False

    n_ok = len(reports)
    n_fail = len(failures) if failures else 0

    status_icon = "✅" if n_fail == 0 else "⚠️"
    text_parts = [
        f"{status_icon} *Morning run complete*",
        f"Reports: {n_ok} generated",
    ]

    if failures:
        text_parts.append(f"⚠️ Failed: {n_fail} ({', '.join(failures[:5])})")
    else:
        text_parts.append("No failures.")

    message = "\n".join(text_parts)

    # ── Staging mode: queue locally instead of sending immediately ──────────
    # Enabled by passing staging=True (previously _STAGING_MODE global —
    # that approach had module identity issues; parameter is cleaner).
    if staging:
        try:
            from pathlib import Path as _Path
            import sys as _sys
            _tq_dir = str(_Path(__file__).parent)
            if _tq_dir not in _sys.path:
                _sys.path.insert(0, _tq_dir)
            from telegram_queue import stage as _stage  # noqa: PLC0415
            from datetime import datetime as _dt, timezone as _tz
            _today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
            _staged = _stage(
                "morning_summary",
                message,
                metadata={
                    "reports_ok": n_ok,
                    "reports_failed": n_fail,
                    "failed_names": list(failures or []),  # full list for agent review
                },
                dedup_key=f"morning-summary-{_today}",
            )
            if _staged:
                print(
                    "[telegram_queue] Morning summary staged (not sent yet).\n"
                    "  Review:  python tradingview/telegram_queue.py --list\n"
                    "  Send:    python tradingview/telegram_queue.py --flush\n"
                    "  Discard: python tradingview/telegram_queue.py --clear"
                )
            else:
                print("[telegram_queue] Morning summary already staged today (dedup'd — skipped).")
            return True  # treat "staged" as success from caller's perspective
        except Exception as _exc:
            print(f"[telegram_queue] Stage failed ({_exc}); summary NOT sent.")
            return False  # do NOT fall through to direct send — staging is required

    return _send(message)


def send_text(message: str) -> bool:
    """
    Send an arbitrary text message to the configured Telegram chat.
    Convenience wrapper used by signal_stack.py and other callers.
    Returns False silently if token not configured (BUG-T5).
    """
    if not is_enabled():
        return False
    return _send(message)


def send_table_v2(text: str) -> bool:
    """
    Send a MarkdownV2-formatted message (tables, monospace blocks) to Telegram.

    This is a separate function from _send() which uses Markdown V1.
    BLOCKER-3 fix: the scheduler's tabular output MUST use MarkdownV2 or
    Telegram's API rejects the message (400 Bad Request) due to V2 escape
    sequences being misinterpreted by the V1 parser.

    Callers MUST have already escaped all special chars via AlertFormatter._esc().
    This function does NOT escape — it passes text verbatim to the API.

    Returns True on success, False on any failure (never raises).
    """
    if not is_enabled():
        print("[telegram_alerts] send_table_v2: Telegram not configured", flush=True)
        return False

    try:
        import requests
    except ImportError:
        print("[telegram_alerts] WARN: requests not installed — cannot send Telegram message",
              flush=True)
        return False

    url = f"https://api.telegram.org/bot{_get_token()}/sendMessage"
    payload = {
        "chat_id": _get_chat_id(),
        "text": text,
        "parse_mode": "MarkdownV2",   # BLOCKER-3 fix: V2, not V1
        "disable_web_page_preview": True,
    }

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(url, json=payload, timeout=_API_TIMEOUT)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                try:
                    retry_after = int(resp.headers.get("Retry-After", _RETRY_BACKOFF[attempt]))
                except (ValueError, TypeError):
                    retry_after = _RETRY_BACKOFF[attempt]
                print(f"[telegram_alerts] send_table_v2: rate-limited (429), waiting {retry_after}s",
                      flush=True)
                time.sleep(retry_after)
                continue
            print(f"[telegram_alerts] send_table_v2: API returned {resp.status_code}: {resp.text[:200]}",
                  flush=True)
            return False
        except (requests.Timeout, requests.ConnectionError) as exc:
            print(f"[telegram_alerts] send_table_v2: failed ({type(exc).__name__}: {exc})",
                  flush=True)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF[attempt])
            else:
                return False

    return False
