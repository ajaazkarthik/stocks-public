"""
tradingview/telegram_scheduler.py
===================================
4-hour scheduled Telegram alert dispatcher.

Runs three trading workflows every 4 hours and sends 3 tabular MarkdownV2
alerts to Telegram — one per workflow:

  Table 1 — Stocks Actionable  (signal_stack.py)
  Table 2 — Options Screener   (options_contract_screener.py)
  Table 3 — Options Analysis   (run_options.py)

Smart deduplication: each alert has a 24-hour TTL window keyed to the alert
content (symbol + strike + expiry). If the same alert was already sent within
the last 24 hours, it is silently skipped. This prevents flooding.

Market-hours gate: cycles that fire when all markets are closed (US/AU/IN) are
silently skipped — no messages sent at 3 AM. Override with --force.

Usage (standalone daemon):
    python tradingview/telegram_scheduler.py          # run one cycle then sleep 4h loop
    python tradingview/telegram_scheduler.py --once   # run one cycle and exit
    python tradingview/telegram_scheduler.py --dry-run  # print tables, no Telegram send
    python tradingview/telegram_scheduler.py --force  # skip market-hours gate

Used by telegram_bot.py (called from async job via run_in_executor):
    from tradingview.telegram_scheduler import TelegramScheduler
    scheduler = TelegramScheduler()
    loop.run_in_executor(None, scheduler.run_cycle)

Requires env vars (same as telegram_alerts.py):
    TELEGRAM_BOT_TOKEN   — Bot API token
    TELEGRAM_CHAT_ID     — Target chat / user ID

Architect fixes applied (2026-05-18):
  - BLOCKER-1: run_cycle() is sync; async job uses loop.run_in_executor()
  - BLOCKER-2: cycle_start_ts captured BEFORE subprocess; window = last 20 min
  - BLOCKER-3: send_table() uses MarkdownV2 parse mode (not V1 Markdown)
  - HIGH-4: INSERT OR IGNORE atomic dedup (not check-then-insert)
  - HIGH-5: max_instances=1 guard documented; bot adds job_kwargs
  - HIGH-6: screener timeout 1800s, --n 50
  - MEDIUM-7: new sqlite3.connect() per method call (thread-safe)
  - MEDIUM-8: locale-aware strike parsing (handles ₹36,800 and $220.50)
  - MEDIUM-9: market-hours gate at cycle start
  - LOW-10: 24-hour TTL (changed from 6h which caused 3×/day re-flood)
  - Always send 3 tables per cycle (empty-notice when no data)
"""

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Windows UTF-8 output ─────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Project paths ────────────────────────────────────────────────────────
_THIS_DIR  = Path(__file__).parent          # tradingview/
_ROOT      = _THIS_DIR.parent               # workspace root

# Ensure project root is importable so `from tradingview.X import` works BOTH when
# run standalone (python tradingview/telegram_scheduler.py) and when imported as a
# package (from tradingview.telegram_scheduler import ...) — BUG-T5 fix.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_OPTS_DIR  = _THIS_DIR / "options"         # tradingview/options/
_REPORTS   = _ROOT / "reports"             # reports/
_DB_DIR    = _ROOT / "database"            # database/
_ALERTS_DB = _DB_DIR / "alert_tracker.db"
_DEDUP_DB  = _ROOT / "telegram_dedup.db"   # standalone; gitignored

# ── Auto-load Telegram credentials ──────────────────────────────────────
# The scheduler uses @AjaazAlertBot (separate from @Ajaaz_bot in telegram_bot.py).
# Set TELEGRAM_BOT_TOKEN so telegram_alerts.py picks it up at import time.
# Priority: TELEGRAM_BOT_TOKEN (explicit) > AJAAZ_ALERT_BOT_TOKEN.
# No embedded fallback: a committed literal token is a leak and hides
# misconfiguration. Missing credentials surface as a clear startup error.
if not os.environ.get("TELEGRAM_BOT_TOKEN"):
    _configured = os.environ.get("AJAAZ_ALERT_BOT_TOKEN", "").strip()
    if _configured:
        os.environ["TELEGRAM_BOT_TOKEN"] = _configured
os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "5986275415")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")


def _validate_telegram() -> bool:
    """Validate bot token via getMe API. Returns True if bot is reachable."""
    try:
        import requests
    except ImportError:
        print("[scheduler] \u274c requests not installed"); return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = (
        os.environ.get("TELEGRAM_CHAT_ID", "")
        or os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip()
    )
    if not token or not chat_id:
        print(f"[scheduler] \u274c Missing: token={'SET' if token else 'EMPTY'}, "
              f"chat_id={'SET' if chat_id else 'EMPTY'}")
        return False
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if resp.status_code != 200:
            print(f"[scheduler] \u274c getMe HTTP {resp.status_code}"); return False
        bot = resp.json().get("result", {})
        print(f"[scheduler] \u2705 Bot: @{bot.get('username', '?')} (id:{bot.get('id', '?')})")
        print(f"[scheduler] \u2705 Chat: {chat_id}")
        return True
    except Exception as e:
        from tradingview.scheduler.config import redact as _redact
        print(f"[scheduler] \u274c Connection: {_redact(e)}"); return False


# ── Script paths ─────────────────────────────────────────────────────────
_SIGNAL_STACK   = _THIS_DIR / "signal_stack.py"
_OPT_SCREENER   = _OPTS_DIR / "options_contract_screener.py"
_OPT_ANALYSIS   = _OPTS_DIR / "run_options.py"

# ── Timing constants ─────────────────────────────────────────────────────
_SIGNAL_TIMEOUT    = 900   # 15 min hard cap for signal_stack.py
_SCREENER_TIMEOUT  = 1800  # 30 min hard cap for options_contract_screener.py (yfinance throttle)
_ANALYSIS_TIMEOUT  = 1800  # 30 min hard cap for run_options.py (5 symbols × 3 min)
_DEDUP_TTL_HOURS   = 24    # re-send same alert after 24h (prevents flooding: 30h look-back window
                            # kept same symbols visible for 30h; 6h TTL caused 3× re-sends/day)

# ── Thresholds ───────────────────────────────────────────────────────────
_SIGNAL_SCORE_THRESHOLD = 60   # composite_score >= 60 to include in stocks table
_MAX_SYMBOLS_FOR_ANALYSIS = 5  # top N symbols from signal_stack → run_options.py

# ── Market windows (UTC hours when at least one market is open) ──────────
# US:  09:30–16:00 ET = 13:30–20:00 UTC (approximate)
# AU:  10:00–16:00 AEST = 00:00–06:00 UTC
# IN:  09:15–15:30 IST = 03:45–10:00 UTC
_MARKET_WINDOWS_UTC: list[tuple[int, int]] = [
    (0,  6),    # AU market
    (3,  10),   # IN market
    (13, 21),   # US market
]


def _any_market_open() -> bool:
    """Return True if at least one of US/AU/IN markets is currently open (UTC)."""
    now_h = datetime.now(timezone.utc).hour
    return any(start <= now_h < end for start, end in _MARKET_WINDOWS_UTC)


# ═══════════════════════════════════════════════════════════════════════════
# DedupDB — SQLite-backed deduplication with 6-hour TTL
# ═══════════════════════════════════════════════════════════════════════════

class DedupDB:
    """
    Thread-safe SQLite deduplication store.

    Dedup key format:
      Stocks:           stock:{SYMBOL}
      Options screener: optscreen:{SYM}:{strike}:{expiry}:{type}
      Options analysis: optanalysis:{SYMBOL}

    TTL window: 24 hours — each (symbol, type) pair sent at most once per day.
    (Previously 6h, which caused the same 30 stocks to re-flood every 8h because
    the 30h look-back window kept the same symbols returning every cycle.)

    Thread safety: each method opens its own connection (sqlite3 check_same_thread
    is True by default — NEVER share a Connection across threads).
    """

    def __init__(self, db_path: Path = _DEDUP_DB) -> None:
        self._path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_alerts (
                    alert_key   TEXT PRIMARY KEY,
                    alert_type  TEXT NOT NULL,
                    sent_at     TEXT NOT NULL   -- ISO-8601 UTC
                )
            """)
            conn.commit()

    def is_sent_recently(self, key: str) -> bool:
        """Return True if this key was sent within the last _DEDUP_TTL_HOURS hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=_DEDUP_TTL_HOURS)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent_alerts WHERE alert_key = ? AND sent_at >= ?",
                (key, cutoff),
            ).fetchone()
        return row is not None

    def mark_sent_atomic(self, key: str, alert_type: str) -> bool:
        """
        Atomically insert the key (INSERT OR IGNORE). Returns True if this process
        won the race (key was not already present), False if duplicate.

        Uses INSERT OR IGNORE + rowcount check — the only way to make check-and-insert
        atomic in SQLite, preventing double-sends from concurrent daemon + bot job.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO sent_alerts (alert_key, alert_type, sent_at) VALUES (?, ?, ?)",
                (key, alert_type, now_iso),
            )
            conn.commit()
            return cur.rowcount == 1   # 1 = inserted (we won); 0 = already existed

    def cleanup_old(self) -> int:
        """Delete entries older than TTL+buffer (24h). Returns number of rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sent_alerts WHERE sent_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount


# ═══════════════════════════════════════════════════════════════════════════
# AlertRunner — subprocess orchestration
# ═══════════════════════════════════════════════════════════════════════════

class AlertRunner:
    """
    Runs the three trading workflow subprocesses and extracts structured data.

    All subprocesses:
    - Use stdin=DEVNULL to prevent interactive-input hangs (same as telegram_bot.py)
    - Inherit BASE_ENV (PYTHONIOENCODING=utf-8)
    - Have hard timeout caps

    Parallel execution: options_screener + options_analysis run concurrently via
    ThreadPoolExecutor(max_workers=2) with shutdown(wait=False) per Golden Rule #16.
    """

    def __init__(self) -> None:
        self._env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "OPENBLAS_NUM_THREADS": "1",
            "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
        }

    def _run_subprocess(self, cmd: list[str], cwd: Path, timeout_s: int) -> tuple[int, str]:
        """
        Run a command with a hard wall-clock deadline. Returns (returncode, last_2000_chars).

        Golden Rule #16 fix: uses a daemon reader thread + proc.wait(timeout=N).
        The old `for line in proc.stdout:` loop blocked FOREVER if the child never
        closed stdout (hung network call). Now:
          1. A daemon thread drains stdout continuously.
          2. proc.wait(timeout=timeout_s) raises TimeoutExpired when deadline hit.
          3. On timeout: proc.kill() then proc.wait() (no infinite block).
        stdin=DEVNULL prevents interactive-input hangs.
        """
        import threading
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=self._env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            chunks: list[str] = []

            def _reader() -> None:
                for line in proc.stdout:  # type: ignore[union-attr]
                    chunks.append(line)
                    combined = "".join(chunks)
                    if len(combined) > 6000:
                        chunks.clear()
                        chunks.append(combined[-4000:])

            reader = threading.Thread(target=_reader, daemon=True, name="sched-stdout-reader")
            reader.start()

            try:
                rc = proc.wait(timeout=timeout_s)
                reader.join(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                reader.join(timeout=2)
                return -1, "".join(chunks)[-2000:] + f"\n[TIMEOUT] Process killed after {timeout_s}s."

            return rc, "".join(chunks)[-2000:]

        except Exception as exc:
            return -1, f"[ERROR] Subprocess launch failed: {exc}"

    # ── Signal stack ────────────────────────────────────────────────────

    def run_signal_stack(self) -> tuple[list[dict], datetime]:
        """
        Run signal_stack.py and return (alerts_list, cycle_start_ts).

        cycle_start_ts is captured BEFORE the subprocess starts (BLOCKER-2 fix)
        so the subsequent alert_tracker.db query window is correct.

        Returns ([], now) on failure — caller handles empty gracefully.
        """
        # Capture window START before subprocess (BLOCKER-2 fix).
        # Window = last 30h: fire_time is the original TV alert timestamp (UTC).
        # 30h (not 24h) provides safe margin when scheduler runs later in the day
        # than the previous signal_stack run (e.g., morning alerts, afternoon cycle).
        cycle_start = datetime.now(timezone.utc)
        window_start = cycle_start - timedelta(hours=30)  # 30h look-back

        cmd = [
            sys.executable,
            str(_SIGNAL_STACK),
            "--n", "100",
            "--threshold", str(_SIGNAL_SCORE_THRESHOLD),
            "--no-moomoo",
            "--no-india",
            "--no-external",
            "--no-options",
            # Route output to reports/scheduler/ so reports/actionable_report.html
            # (generated by morning run WITH buy-vehicle advisor) is never overwritten.
            "--out", str(_ROOT / "reports" / "scheduler" / "telegram_signals.html"),
            # Note: --ibkr is opt-IN (not opt-out) — do NOT pass --ibkr for background runs
        ]

        print(f"[scheduler] Running signal_stack.py (timeout={_SIGNAL_TIMEOUT}s)…", flush=True)
        rc, out = self._run_subprocess(cmd, cwd=_THIS_DIR, timeout_s=_SIGNAL_TIMEOUT)

        if rc != 0:
            print(f"[scheduler] signal_stack.py exited {rc}:\n{out[-500:]}", flush=True)
            # Still query DB — partial results may exist from a previous good run

        # Query alert_tracker.db for results from this cycle window
        alerts = self._query_alert_tracker(window_start)
        print(f"[scheduler] signal_stack → {len(alerts)} alerts above score {_SIGNAL_SCORE_THRESHOLD}", flush=True)
        return alerts, cycle_start

    def _query_alert_tracker(self, since: datetime) -> list[dict]:
        """Query alert_tracker.db for alerts >= threshold inserted after `since`."""
        if not _ALERTS_DB.exists():
            print(f"[scheduler] WARN: alert_tracker.db not found at {_ALERTS_DB}", flush=True)
            return []
        try:
            since_iso = since.isoformat()
            with sqlite3.connect(str(_ALERTS_DB), timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                # GROUP BY tv_symbol → one row per symbol (highest score) to avoid dupes
            # from multiple signal_stack runs that INSERT OR IGNORE the same symbol.
            _ALERT_SQL = """
                    SELECT tv_symbol, MAX(composite_score) AS composite_score,
                           signal, fire_time,
                           l1_score, l2_score, l3_score, l4_score,
                           win_rate_pct, market_regime,
                           entry_price, result_json
                    FROM alerts
                    WHERE composite_score >= ?
                      AND fire_time >= ?
                      AND (is_delisted IS NULL OR is_delisted = 0)
                    GROUP BY tv_symbol
                    ORDER BY MAX(composite_score) DESC
                    LIMIT 30
                    """
            rows = conn.execute(_ALERT_SQL, (_SIGNAL_SCORE_THRESHOLD, since_iso)).fetchall()
            # Fallback: if the 30h window found nothing, show all-time top results
            # so something useful is always sent on quiet market days.
            if not rows:
                rows = conn.execute(
                    """
                    SELECT tv_symbol, MAX(composite_score) AS composite_score,
                           signal, fire_time,
                           l1_score, l2_score, l3_score, l4_score,
                           win_rate_pct, market_regime,
                           entry_price, result_json
                    FROM alerts
                    WHERE composite_score >= ?
                      AND (is_delisted IS NULL OR is_delisted = 0)
                    GROUP BY tv_symbol
                    ORDER BY MAX(composite_score) DESC
                    LIMIT 30
                    """,
                    (_SIGNAL_SCORE_THRESHOLD,),
                ).fetchall()
                if rows:
                    print("[scheduler] Using best available alerts (30h window empty; showing all-time top results)", flush=True)

            import json as _json  # local import avoids top-level name collision

            def _parse_rich(r) -> dict:
                """Extract rich trader-relevant fields from a DB row."""
                rj: dict = {}
                try:
                    raw = r["result_json"]
                    if raw:
                        rj = _json.loads(raw)
                except Exception:
                    pass
                fund  = rj.get("fundamentals") or {}
                sent  = rj.get("sentiment_data") or {}
                mi    = rj.get("market_internals") or {}
                news  = sent.get("news") or {}
                analy = sent.get("analyst") or {}
                scores= rj.get("scores") or {}
                price = fund.get("current_price") or float(r["entry_price"] or 0) or 0
                target= float(fund.get("analyst_target") or 0)
                upside= round((target - price) / price * 100, 1) if price and target else None
                analyst_buys = int(analy.get("strongBuy", 0) or 0) + int(analy.get("buy", 0) or 0)
                l1 = r["l1_score"]
                l2 = r["l2_score"]
                l3 = r["l3_score"]
                l4 = r["l4_score"]
                # Try to get per-sub-score detail from scores block
                if not l1:
                    l1 = (scores.get("l1") or {}).get("total") or l1
                if not l2:
                    l2 = (scores.get("l2") or {}).get("total") or l2
                if not l3:
                    l3 = (scores.get("l3") or {}).get("total") or l3
                if not l4:
                    l4 = (scores.get("l4") or {}).get("total") or l4
                return {
                    "symbol":         str(r["tv_symbol"] or ""),
                    "score":          float(r["composite_score"] or 0),
                    "signal":         str(r["signal"] or "BUY").upper(),
                    "l1":             l1,
                    "l2":             l2,
                    "l3":             l3,
                    "l4":             l4,
                    "wr":             r["win_rate_pct"],
                    "regime":         str(r["market_regime"] or ""),
                    "price":          price,
                    "analyst_target": target,
                    "analyst_upside": upside,
                    "company":        str(fund.get("company_name") or "")[:22],
                    "sector":         str(fund.get("sector") or "")[:14],
                    "news_count":     int(news.get("count") or 0),
                    "news_label":     str(news.get("label") or ""),
                    "analyst_buys":   analyst_buys,
                    "recent_upgrade": bool(sent.get("recent_upgrade", False)),
                    "earnings_days":  sent.get("earnings_days_away"),
                    "earnings_risk":  str(sent.get("earnings_risk") or ""),
                    "short_pct":      float(mi.get("short_pct_float") or 0),
                    "pcr_label":      str(mi.get("pcr_label") or ""),
                }

            return [
                _parse_rich(r)
                for r in rows
                if str(r["tv_symbol"] or "").strip()
            ]
        except Exception as exc:
            print(f"[scheduler] WARN: alert_tracker.db query failed: {exc}", flush=True)
            return []

    # ── Options screener ────────────────────────────────────────────────

    def _query_top_symbols_from_db(self, hours: int = 24, limit: int = 5) -> list[str]:
        """
        Query alert_tracker.db for the top composite_score symbols from the last `hours`.
        Used as fallback when signal_stack finds 0 alerts in the current cycle window.
        """
        if not _ALERTS_DB.exists():
            return []
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            with sqlite3.connect(str(_ALERTS_DB), timeout=5) as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT tv_symbol FROM alerts
                    WHERE composite_score >= ? AND fire_time >= ?
                      AND (is_delisted IS NULL OR is_delisted = 0)
                    ORDER BY composite_score DESC LIMIT ?
                    """,
                    (_SIGNAL_SCORE_THRESHOLD, cutoff, limit),
                ).fetchall()
                if not rows:
                    # Broaden to all-time if nothing in window
                    rows = conn.execute(
                        """
                        SELECT DISTINCT tv_symbol FROM alerts
                        WHERE composite_score >= ?
                          AND (is_delisted IS NULL OR is_delisted = 0)
                        ORDER BY composite_score DESC LIMIT ?
                        """,
                        (_SIGNAL_SCORE_THRESHOLD, limit),
                    ).fetchall()
            syms = [r[0] for r in rows if r[0]]
            if syms:
                print(f"[scheduler] DB fallback symbols for options analysis: {syms}", flush=True)
            return syms
        except Exception as exc:
            print(f"[scheduler] WARN: _query_top_symbols_from_db failed: {exc}", flush=True)
            return []

    def run_options_screener(self) -> list[dict]:
        """
        Run options_contract_screener.py and parse options_actionable.html.

        Returns list of dicts: {underlying, strike, expiry, type, iv, tqs, dte, premium}
        Returns [] on timeout or parse failure (logs warning, never raises).
        Timeout: 1800s (HIGH-6 fix — yfinance throttle on slow days).
        """
        cmd = [
            sys.executable,
            str(_OPT_SCREENER),
            "--n", "50",          # HIGH-6: 50 not 100 (safe ~450s budget)
            "--max-dte", "28",
            "--option-type", "BOTH",
            "--actionable-out", str(_REPORTS / "options_actionable.html"),
            "--max-be-sigma", "0.70",  # Relaxed threshold (default 0.55 too strict)
        ]

        print(f"[scheduler] Running options_contract_screener.py (timeout={_SCREENER_TIMEOUT}s)…", flush=True)
        rc, out = self._run_subprocess(cmd, cwd=_OPTS_DIR, timeout_s=_SCREENER_TIMEOUT)

        if rc != 0:
            print(f"[scheduler] options_contract_screener.py exited {rc}", flush=True)
            if "[TIMEOUT]" in out:
                print(f"[scheduler] Screener timed out — using cached options_actionable.html", flush=True)

        return self._parse_options_actionable_html()

    def _parse_options_actionable_html(self) -> list[dict]:
        """Parse reports/options_actionable.html using BeautifulSoup data-* attributes."""
        html_path = _REPORTS / "options_actionable.html"
        if not html_path.exists():
            print(f"[scheduler] WARN: {html_path} not found", flush=True)
            return []
        try:
            from bs4 import BeautifulSoup  # confirmed in requirements.txt
            html = html_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select(".contract-card")
            if not cards:
                print("[scheduler] options_actionable.html: no .contract-card elements (0 contracts passed filter)", flush=True)
                return []
            results: list[dict] = []
            for card in cards:
                sym     = card.get("data-symbol", "")
                tqs_raw = card.get("data-tqs", "0")
                dte_raw = card.get("data-dte", "0")
                pw_raw  = card.get("data-pw", "0")
                # Extract additional fields from text content if data-attrs are partial
                strike_raw  = card.get("data-strike", "") or _text(card, ".cc-strike")
                expiry_raw  = card.get("data-expiry", "") or _text(card, ".cc-expiry")
                otype_raw   = card.get("data-type", "")  or _text(card, ".cc-type")
                iv_raw      = card.get("data-iv", "")    or _text(card, ".cc-iv")
                premium_raw = card.get("data-premium","")or _text(card, ".cc-premium")

                try:
                    tqs = float(tqs_raw)
                except (ValueError, TypeError):
                    tqs = 0.0
                try:
                    dte = int(float(dte_raw))
                except (ValueError, TypeError):
                    dte = 0
                try:
                    premium = _parse_numeric(premium_raw)
                except (ValueError, TypeError):
                    premium = 0.0
                try:
                    iv = _parse_pct(iv_raw)
                except (ValueError, TypeError):
                    iv = 0.0

                if not sym:
                    continue  # skip cards with no symbol

                results.append({
                    "underlying": sym,
                    "strike":     _parse_numeric(strike_raw) if strike_raw else 0.0,
                    "expiry":     _clean_text(expiry_raw),
                    "type":       _clean_text(otype_raw).upper() or "?",
                    "iv":         iv,
                    "tqs":        tqs,
                    "dte":        dte,
                    "premium":    premium,
                })

            # Sanity check: if ≥3 fields are empty across all results, format may have changed
            if results and sum(1 for r in results if not r["underlying"]) > len(results) * 0.5:
                print("[scheduler] WARN: >50% of screener cards had empty symbol — HTML format may have changed", flush=True)

            print(f"[scheduler] options_screener → {len(results)} contracts parsed", flush=True)
            return results
        except Exception as exc:
            print(f"[scheduler] WARN: options_actionable.html parse failed: {exc}", flush=True)
            return []

    # ── Options analysis ────────────────────────────────────────────────

    def run_options_analysis(self, symbols: list[str]) -> list[dict]:
        """
        Run run_options.py for the given symbols and parse options_report.html.

        Pre-flight gate: returns [] immediately if symbols is empty.
        Returns list of dicts: {symbol, strategy, recommendation, score, market}
        """
        if not symbols:
            print("[scheduler] Skipping options analysis (no symbols from signal_stack)", flush=True)
            return []

        sym_str = ",".join(symbols[:_MAX_SYMBOLS_FOR_ANALYSIS])
        cmd = [
            sys.executable,
            str(_OPT_ANALYSIS),
            "--symbols", sym_str,
            "--mode", "BALANCED",
            "--output", str(_REPORTS / "options_report.html"),
        ]

        print(f"[scheduler] Running run_options.py for {sym_str} (timeout={_ANALYSIS_TIMEOUT}s)…", flush=True)
        rc, out = self._run_subprocess(cmd, cwd=_OPTS_DIR, timeout_s=_ANALYSIS_TIMEOUT)

        if rc != 0 and "[TIMEOUT]" not in out:
            print(f"[scheduler] run_options.py exited {rc}", flush=True)

        return self._parse_options_report_html()

    def _parse_options_report_html(self) -> list[dict]:
        """Parse reports/options_report.html using BeautifulSoup CSS selectors."""
        html_path = _REPORTS / "options_report.html"
        if not html_path.exists():
            print(f"[scheduler] WARN: {html_path} not found", flush=True)
            return []
        try:
            from bs4 import BeautifulSoup
            html = html_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")

            results: list[dict] = []
            for sym_card in soup.select(".sym-card"):
                symbol = sym_card.get("data-sym", "") or _text(sym_card, ".card-title")
                market = sym_card.get("data-market", "")

                for cc in sym_card.select(".contract-card"):
                    raw_strike  = _text(cc, ".cc-strike")    # e.g. "CALL ₹36,800"
                    raw_expiry  = _text(cc, ".cc-expiry")    # e.g. "exp 30JUN2026 (44d)"
                    raw_premium = _text(cc, ".cc-premium")   # e.g. "₹1525.49" or "$2.50"
                    raw_score   = _text(cc, ".comp-score")   # e.g. "55"

                    # MEDIUM-8: locale-aware strike parsing handles ₹36,800 and $220.50
                    try:
                        strike = _parse_numeric(raw_strike)
                    except (ValueError, TypeError):
                        strike = 0.0

                    try:
                        score = float(re.sub(r"[^\d.]", "", raw_score or "0") or "0")
                    except (ValueError, TypeError):
                        score = 0.0

                    # Determine option type from strike text
                    strike_upper = raw_strike.upper()
                    otype = "CALL" if "CALL" in strike_upper else ("PUT" if "PUT" in strike_upper else "?")

                    if not symbol:
                        continue

                    results.append({
                        "symbol":         symbol,
                        "market":         market,
                        "type":           otype,
                        "strike":         strike,
                        "expiry":         _clean_text(raw_expiry),
                        "premium":        _parse_numeric(raw_premium) if raw_premium else 0.0,
                        "score":          score,
                        "strategy":       f"{otype} {_clean_text(raw_expiry)}",
                        "recommendation": "BUY" if score >= 55 else "NEUTRAL",
                    })

            if not results:
                print("[scheduler] options_report.html: no .sym-card/.contract-card elements found", flush=True)
            else:
                print(f"[scheduler] options_analysis → {len(results)} recommendations parsed", flush=True)
            return results
        except Exception as exc:
            print(f"[scheduler] WARN: options_report.html parse failed: {exc}", flush=True)
            return []


# ═══════════════════════════════════════════════════════════════════════════
# HTML parsing helpers
# ═══════════════════════════════════════════════════════════════════════════

def _text(soup_element, selector: str) -> str:
    """Extract text from a CSS selector within a BeautifulSoup element."""
    try:
        el = soup_element.select_one(selector)
        return el.get_text(strip=True) if el else ""
    except Exception:
        return ""


def _clean_text(raw: str) -> str:
    """Strip whitespace and common prefixes from a field value."""
    return re.sub(r"\s+", " ", raw or "").strip()


def _parse_numeric(raw: str) -> float:
    """
    Parse a numeric value from a localized string.
    Handles: "₹36,800", "$220.50", "CALL ₹36,800", "1525.49", "0.45"
    MEDIUM-8 fix: strips all non-digit/non-dot characters after comma removal.
    """
    if not raw:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    return float(cleaned) if cleaned else 0.0


def _parse_pct(raw: str) -> float:
    """Parse a percentage value (e.g. "45.2%" → 0.452, "0.45" → 0.45)."""
    cleaned = re.sub(r"[^\d.]", "", raw or "")
    if not cleaned:
        return 0.0
    val = float(cleaned)
    # If value > 1 assume it's a percentage like 45.2
    return val / 100 if val > 1 else val


# ═══════════════════════════════════════════════════════════════════════════
# AlertFormatter — MarkdownV2 tabular message builder
# ═══════════════════════════════════════════════════════════════════════════

class AlertFormatter:
    """
    Builds MarkdownV2 tabular messages for Telegram.

    BLOCKER-3 fix: all output is MarkdownV2 (NOT V1 Markdown). All data fields
    are escaped through _esc() before inclusion in any message string.
    Chunking at 4000 chars (Telegram hard limit = 4096; 96-char buffer for
    escape sequences inflating length).
    """

    _CHUNK_MAX = 4000

    @staticmethod
    def _esc(text: str) -> str:
        """Escape ALL MarkdownV2 special characters per Telegram docs."""
        for ch in r"\_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text

    @staticmethod
    def _score_bucket(score: float) -> str:
        if score >= 85:
            return "APEX"
        if score >= 70:
            return "HIGH"
        if score >= 55:
            return "MED"
        return "LOW"

    def format_stocks_table(self, alerts: list[dict], skipped: int) -> str:
        """
        Format stocks actionable alerts as MarkdownV2 tabular table.
        Section 1: ranked single-row per stock (score, tier, WR, L1-L4, upside).
        Section 2: catalyst context (news, earnings, short interest).
        Always returns non-empty string (sends even when no new alerts).
        """
        e = self._esc
        now_str = datetime.now().strftime("%d %b %Y %H:%M")
        header = f"📈 *Stocks Actionable* — {e(now_str)}\n"

        if not alerts:
            body = "(no new alerts"
            if skipped:
                body += f" — {skipped} already sent today"
            body += ")"
            return header + "```\n" + body + "\n```"

        # ── Section 1: ranked table ───────────────────────────────────────
        COLS = f"{'Symbol':<16} {'Scr':>3} {'Tier':<4} {'WR':>3} {'L1':>3} {'L2':>3} {'L3':>3} {'L4':>3} {'Up%':>6}"
        sep  = "─" * len(COLS)
        lines = [header, "```", COLS, sep]
        catalyst_lines: list[str] = []

        for a in alerts:
            sym    = (a.get("symbol") or "")[:16]
            score  = float(a.get("score") or 0.0)
            bucket = self._score_bucket(score)
            try:
                wr_val = float(a.get("wr") or 0.0)
            except (ValueError, TypeError):
                wr_val = 0.0
            try:
                l1v = float(a.get("l1") or 0)
                l2v = float(a.get("l2") or 0)
                l3v = float(a.get("l3") or 0)
                l4v = float(a.get("l4") or 0)
            except (ValueError, TypeError):
                l1v = l2v = l3v = l4v = 0.0
            upside = a.get("analyst_upside")
            try:
                up_val: float | None = float(upside) if upside is not None else None
            except (ValueError, TypeError):
                up_val = None
            up_s = f"{up_val:>+5.1f}" if up_val is not None else "     "
            lines.append(
                f"{sym:<16} {score:>3.0f} {bucket:<4} {wr_val:>3.0f}"
                f" {l1v:>3.0f} {l2v:>3.0f} {l3v:>3.0f} {l4v:>3.0f} {up_s}"
            )

            # ── Catalyst line ─────────────────────────────────────────────
            news_count = a.get("news_count") or 0
            news_label = (a.get("news_label") or "").strip()
            earn_days  = a.get("earnings_days")
            short_pct  = float(a.get("short_pct") or 0.0)
            _nl = {"Very Bullish": "V.Bll", "Bullish": "Bll",
                   "Very Bearish": "V.Br",  "Bearish": "Br", "Neutral": "Ntr"}
            nl_short = _nl.get(news_label, news_label[:5]) if news_label else ""
            news_s = f"{nl_short}\u00d7{news_count}" if nl_short else (f"{news_count}news" if news_count else "")
            earn_s = f"Earn:{earn_days}d" if earn_days is not None else ""
            shrt_s = f"Shrt:{short_pct:.1f}%" if short_pct > 0.5 else ""
            short_sym = sym.split(":")[-1][:5] if ":" in sym else sym[:5]
            parts = [p for p in [news_s, earn_s, shrt_s] if p]
            catalyst_lines.append(f"{short_sym:<5}: {' | '.join(parts) or chr(8212)}")

        lines.append(sep)

        # ── Section 2: catalyst context ──────────────────────────────────
        if catalyst_lines:
            lines.append("Catalysts:")
            lines.extend(catalyst_lines)
            lines.append(sep)

        lines.append(f"{len(alerts)} new  \u2022  {skipped} sent today")
        lines.append("```")
        return "\n".join(lines)

    def format_options_screener_table(self, contracts: list[dict], skipped: int) -> str:
        """Format options screener contracts as MarkdownV2 table with IV, DTE, premium."""
        e = self._esc
        now_str = datetime.now().strftime("%d %b %Y %H:%M")
        header = f"🔍 *Options Screener* — {e(now_str)}\n"

        if not contracts:
            body = "(no new contracts"
            if skipped:
                body += f" \u2014 {skipped} already sent today"
            body += ")"
            return header + "```\n" + body + "\n```"

        lines = [header]
        lines.append("```")
        lines.append(f"{'Symbol':<14} T  {'Strike':>7}  {'Exp':>9}  {'DTE':>3}  {'IV%':>4}  {'TQS':>4}  Prem")
        lines.append("─" * 58)
        for c in contracts:
            sym    = (c.get("underlying") or "")[:13]
            ctype  = (c.get("type") or "?")[:1]   # C or P
            strike = c.get("strike") or 0.0
            expiry = (c.get("expiry") or "")[:10]
            # Short date: "20Jun" from "2026-06-20"
            try:
                from datetime import date as _date
                exp_dt = _date.fromisoformat(expiry)
                exp_short = exp_dt.strftime("%d%b").lstrip("0")
            except Exception:
                exp_short = expiry[-5:]
            dte    = c.get("dte") or 0
            iv_raw = c.get("iv") or 0.0
            iv_pct = iv_raw * 100 if iv_raw < 5 else iv_raw  # normalise decimal→%
            tqs    = c.get("tqs") or 0.0
            prem   = c.get("premium") or 0.0
            prem_s = f"${prem:.2f}" if prem else ""
            lines.append(f"{sym:<14} {ctype}  {strike:>7.1f}  {exp_short:>9}  {dte:>3}  {iv_pct:>4.0f}  {tqs:>4.1f}  {prem_s}")
        lines.append("─" * 58)
        lines.append(f"{len(contracts)} new  •  {skipped} already sent")
        lines.append("```")
        return "\n".join(lines)

    def format_options_analysis_table(self, recs: list[dict], skipped: int) -> str:
        """
        Format options analysis recommendations as a flat MarkdownV2 tabular table.
        Each contract in one row: symbol, type, strike, expiry, recommendation, score, premium.
        Always returns non-empty string (sends even when no new recommendations).
        """
        e = self._esc
        now_str = datetime.now().strftime("%d %b %Y %H:%M")
        header = f"\U0001f4ca *Options Analysis* \u2014 {e(now_str)}\n"

        if not recs:
            body = "(no new recommendations"
            if skipped:
                body += f" \u2014 {skipped} already sent today"
            body += ")"
            return header + "```\n" + body + "\n```"

        COLS = f"{'Symbol':<13} T  {'Strike':>7}  {'Exp':>6}  {'Rec':<7}  {'Scr':>3}  Prem"
        sep  = "─" * len(COLS)
        lines = [header, "```", COLS, sep]

        import re as _re
        from datetime import date as _date

        for r in recs:
            sym    = (r.get("symbol") or "")[:13]
            otype  = (r.get("type") or "?")[:1]      # C or P
            strike = float(r.get("strike") or 0.0)
            expiry = str(r.get("expiry") or "")
            rec    = (r.get("recommendation") or "")[:7]
            scr    = float(r.get("score") or 0.0)
            prem   = float(r.get("premium") or 0.0)
            prem_s = f"${prem:.2f}" if prem else ""

            # Parse expiry → short date ("20Jun" format)
            exp_short = "?"
            m = _re.search(r"(\d{4}-\d{2}-\d{2})", expiry)
            if m:
                try:
                    exp_dt    = _date.fromisoformat(m.group(1))
                    exp_short = exp_dt.strftime("%d%b").lstrip("0")
                except Exception:
                    exp_short = expiry[-5:]
            elif len(expiry) >= 10:
                try:
                    exp_dt    = _date.fromisoformat(expiry[:10])
                    exp_short = exp_dt.strftime("%d%b").lstrip("0")
                except Exception:
                    exp_short = expiry[:6]

            lines.append(
                f"{sym:<13} {otype}  {strike:>7.1f}  {exp_short:>6}  {rec:<7}  {scr:>3.0f}  {prem_s}"
            )

        lines.append(sep)
        lines.append(f"{len(recs)} new  \u2022  {skipped} sent today")
        lines.append("```")
        return "\n".join(lines)

    def chunk(self, text: str) -> list[str]:
        """Split text into chunks of max _CHUNK_MAX chars for Telegram's limit."""
        if len(text) <= self._CHUNK_MAX:
            return [text]
        chunks = []
        remaining = text
        part = 1
        while remaining:
            if len(remaining) <= self._CHUNK_MAX:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, self._CHUNK_MAX)
            if split_at == -1:
                split_at = self._CHUNK_MAX
            chunks.append(remaining[:split_at] + f"\n_\\({part}/{part + len(remaining) // self._CHUNK_MAX + 1}\\)_")
            remaining = remaining[split_at:].lstrip("\n")
            part += 1
        return chunks


# ═══════════════════════════════════════════════════════════════════════════
# TelegramScheduler — main orchestrator
# ═══════════════════════════════════════════════════════════════════════════

class TelegramScheduler:
    """
    Orchestrates the 4-hour alert cycle:
      1. Run signal_stack.py (subprocess)
      2. Dedup filter on stocks results
      3. In parallel: run options_screener + options_analysis (top 5 symbols)
      4. Dedup filter on options results
      5. Format 3 MarkdownV2 tables
      6. Send via telegram_alerts.send_table() (MarkdownV2, BLOCKER-3 fix)
      7. Return stats dict

    This class is SYNCHRONOUS — the async APScheduler job wraps it via
    loop.run_in_executor(None, scheduler.run_cycle) (BLOCKER-1 fix).
    """

    def __init__(self, dry_run: bool = False, force_market: bool = False, test_mode: bool = False, force_resend: bool = False) -> None:
        self._dry_run = dry_run
        self._force_market = force_market
        self._test_mode = test_mode   # --test: skip signal_stack subprocess, use DB directly, bypass dedup
        self._force_resend = force_resend  # --force: also bypass dedup so re-runs always send
        self._dedup = DedupDB()
        self._runner = AlertRunner()
        self._fmt = AlertFormatter()

    def run_cycle(self) -> dict:
        """
        Execute one full 4-hour cycle.
        Returns stats: {stocks_new, stocks_skipped, screener_new, screener_skipped,
                        analysis_new, analysis_skipped, errors, skipped_market_hours}
        """
        stats: dict = {
            "stocks_new": 0, "stocks_skipped": 0,
            "screener_new": 0, "screener_skipped": 0,
            "analysis_new": 0, "analysis_skipped": 0,
            "errors": [],
            "skipped_market_hours": False,
        }

        # Market hours gate (MEDIUM-9 fix): skip if all markets closed
        if not self._force_market and not _any_market_open():
            print("[scheduler] Skipping cycle — all markets closed (use --force to override)", flush=True)
            stats["skipped_market_hours"] = True
            return stats

        print(f"\n[scheduler] ═══ Starting 4h cycle at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ═══", flush=True)

        # Cleanup old dedup entries (run once per cycle, not per send)
        cleaned = self._dedup.cleanup_old()
        if cleaned:
            print(f"[scheduler] Cleaned {cleaned} expired dedup entries", flush=True)

        # ── Step 1: Signal stack ─────────────────────────────────────────
        if self._test_mode:
            # --test mode: skip the 6-min signal_stack subprocess entirely.
            # Query alert_tracker.db directly for top 5 symbols — runs in <1s.
            cycle_ts = datetime.now(timezone.utc)
            raw_stocks = self._runner._query_alert_tracker(
                cycle_ts - timedelta(hours=168)  # 7-day window to ensure results
            )[:5]
            print(f"[scheduler] TEST MODE: skipped signal_stack.py — loaded {len(raw_stocks)} stocks from DB", flush=True)
        else:
            try:
                raw_stocks, cycle_ts = self._runner.run_signal_stack()
            except Exception as exc:
                msg = f"signal_stack error: {exc}"
                stats["errors"].append(msg)
                print(f"[scheduler] ERROR: {msg}", flush=True)
                raw_stocks, cycle_ts = [], datetime.now(timezone.utc)

        # ── Step 2: Dedup stocks ─────────────────────────────────────────
        # --test or --force bypass dedup so re-runs always show results.
        bypass_dedup = self._test_mode or self._force_resend
        new_stocks: list[dict] = []
        skipped_stocks = 0
        for alert in raw_stocks:
            sym = alert.get("symbol", "")
            if not sym:
                continue
            key = f"stock:{sym}"
            if not bypass_dedup and self._dedup.is_sent_recently(key):
                skipped_stocks += 1
                continue
            new_stocks.append(alert)

        stats["stocks_skipped"] = skipped_stocks

        # Top symbols for options analysis: always use full cycle result (raw_stocks),
        # not the dedup-filtered new_stocks (Bug #4 fix — dedup should not affect
        # which symbols are analysed, only which Telegram messages are sent).
        # If signal_stack found nothing this cycle, fall back to last-24h DB query
        # so options analysis always runs independently (Bug #2 fix).
        top_symbols = [a["symbol"] for a in raw_stocks[:_MAX_SYMBOLS_FOR_ANALYSIS]]
        if not top_symbols:
            top_symbols = self._runner._query_top_symbols_from_db(hours=30, limit=_MAX_SYMBOLS_FOR_ANALYSIS)
            if top_symbols:
                print(f"[scheduler] Using DB fallback symbols for options analysis: {top_symbols}", flush=True)

        # ── Step 3: Parallel options runs ────────────────────────────────
        # Golden Rule #16: use shutdown(wait=False), NOT `with ThreadPoolExecutor() as ex:`
        raw_screener: list[dict] = []
        raw_analysis: list[dict] = []
        screener_err: Optional[str] = None
        analysis_err: Optional[str] = None

        _ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scheduler-opts")
        # --test mode: skip the slow screener subprocess (always times out), parse cached HTML
        if self._test_mode:
            fut_screener = _ex.submit(self._runner._parse_options_actionable_html)
            print("[scheduler] TEST MODE: skipped options_contract_screener.py — using cached HTML", flush=True)
        else:
            fut_screener = _ex.submit(self._runner.run_options_screener)
        fut_analysis = _ex.submit(self._runner.run_options_analysis, top_symbols)

        try:
            raw_screener = fut_screener.result(timeout=_SCREENER_TIMEOUT + 30)
        except Exception as exc:
            screener_err = f"options_screener error: {exc}"
            print(f"[scheduler] ERROR: {screener_err}", flush=True)
            stats["errors"].append(screener_err)
            # Fallback: try to parse the cached HTML without re-running the script
            raw_screener = self._runner._parse_options_actionable_html()

        try:
            raw_analysis = fut_analysis.result(timeout=_ANALYSIS_TIMEOUT + 30)
        except Exception as exc:
            analysis_err = f"options_analysis error: {exc}"
            print(f"[scheduler] ERROR: {analysis_err}", flush=True)
            stats["errors"].append(analysis_err)

        _ex.shutdown(wait=False)  # Golden Rule #16

        # ── Step 4: Dedup options screener ───────────────────────────────
        new_screener: list[dict] = []
        skipped_screener = 0
        for c in raw_screener:
            sym     = c.get("underlying", "")
            strike  = c.get("strike", 0)
            expiry  = c.get("expiry", "")
            ctype   = c.get("type", "")
            key     = f"optscreen:{sym}:{strike:.0f}:{expiry}:{ctype}"
            if not bypass_dedup and self._dedup.is_sent_recently(key):
                skipped_screener += 1
                continue
            new_screener.append(c)

        stats["screener_skipped"] = skipped_screener

        # ── Step 5: Dedup options analysis ──────────────────────────────
        new_analysis: list[dict] = []
        skipped_analysis = 0
        for r in raw_analysis:
            sym    = r.get("symbol", "")
            strike = r.get("strike", 0)
            expiry = r.get("expiry", "")
            ctype  = r.get("type", "")
            key    = f"optanalysis:{sym}:{strike:.0f}:{expiry}:{ctype}"
            if not bypass_dedup and self._dedup.is_sent_recently(key):
                skipped_analysis += 1
                continue
            new_analysis.append(r)

        stats["analysis_skipped"] = skipped_analysis

        # ── Step 6: Format & send ────────────────────────────────────────
        # FLOOD FIX: If ALL 3 tables are empty (no new data), skip sending entirely.
        # Only send when at least one table has actual new data to report.
        if not new_stocks and not new_screener and not new_analysis:
            print(
                f"[scheduler] Nothing new this cycle "
                f"(skipped: {skipped_stocks} stocks, {skipped_screener} screener, {skipped_analysis} analysis) — not sending",
                flush=True,
            )
            stats["stocks_new"]   = 0
            stats["screener_new"] = 0
            stats["analysis_new"] = 0
            stats["stocks_skipped"]   = skipped_stocks
            stats["screener_skipped"] = skipped_screener
            stats["analysis_skipped"] = skipped_analysis
            return stats

        # Formatters always return a non-empty string (empty-table notice when no data).
        stocks_msg    = self._fmt.format_stocks_table(new_stocks, skipped_stocks)
        screener_msg  = self._fmt.format_options_screener_table(new_screener, skipped_screener)
        analysis_msg  = self._fmt.format_options_analysis_table(new_analysis, skipped_analysis)

        if self._dry_run:
            print("\n" + "═" * 60, flush=True)
            print("[DRY-RUN] Stocks Table:", flush=True)
            print(stocks_msg or "(empty)", flush=True)
            print("\n[DRY-RUN] Options Screener Table:", flush=True)
            print(screener_msg or "(empty)", flush=True)
            print("\n[DRY-RUN] Options Analysis Table:", flush=True)
            print(analysis_msg or "(empty)", flush=True)
            print("═" * 60 + "\n", flush=True)
        else:
            # Import lazily — works standalone (tradingview/ in sys.path) and as package
            # (root in sys.path added at module top). BUG-T5 fix.
            try:
                from tradingview.telegram_alerts import send_table_v2 as _send_tbl, is_enabled as _ok
            except ImportError:
                from telegram_alerts import send_table_v2 as _send_tbl, is_enabled as _ok  # type: ignore[no-redef]
            if not _ok():
                print("[scheduler] WARN: Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID", flush=True)
            else:
                _labels = ["stocks", "screener", "analysis"]
                for label, msg in zip(_labels, [stocks_msg, screener_msg, analysis_msg]):
                    chunks = self._fmt.chunk(msg)
                    for i, chunk in enumerate(chunks, 1):
                        ok = _send_tbl(chunk)
                        status = "✓ OK" if ok else "✗ FAILED"
                        print(f"[scheduler] Telegram {label} chunk {i}/{len(chunks)}: {status} ({len(chunk)} chars)", flush=True)

        # ── Step 7: Mark sent in dedup DB ────────────────────────────────
        if not self._dry_run:
            for a in new_stocks:
                sym = a.get("symbol", "")
                if sym:
                    self._dedup.mark_sent_atomic(f"stock:{sym}", "stock")
            for c in new_screener:
                sym    = c.get("underlying", "")
                strike = c.get("strike", 0)
                expiry = c.get("expiry", "")
                ctype  = c.get("type", "")
                self._dedup.mark_sent_atomic(f"optscreen:{sym}:{strike:.0f}:{expiry}:{ctype}", "optscreen")
            for r in new_analysis:
                sym    = r.get("symbol", "")
                strike = r.get("strike", 0)
                expiry = r.get("expiry", "")
                ctype  = r.get("type", "")
                self._dedup.mark_sent_atomic(f"optanalysis:{sym}:{strike:.0f}:{expiry}:{ctype}", "optanalysis")

        stats["stocks_new"]   = len(new_stocks)
        stats["screener_new"] = len(new_screener)
        stats["analysis_new"] = len(new_analysis)

        print(
            f"[scheduler] Cycle complete: stocks={len(new_stocks)} new/{skipped_stocks} skip | "
            f"screener={len(new_screener)} new/{skipped_screener} skip | "
            f"analysis={len(new_analysis)} new/{skipped_analysis} skip",
            flush=True,
        )
        return stats


# ═══════════════════════════════════════════════════════════════════════════
# Standalone daemon entry point
# ═══════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="4-hour Telegram scheduled alerts: stocks + options screener + options analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tradingview/telegram_scheduler.py --once
  python tradingview/telegram_scheduler.py --dry-run
  python tradingview/telegram_scheduler.py --once --force
  python tradingview/telegram_scheduler.py            # daemon loop (4h)
""",
    )
    parser.add_argument("--once",    action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print tables to stdout, do not send to Telegram")
    parser.add_argument("--force",   action="store_true", help="Skip market-hours gate AND bypass dedup (re-send everything)")
    parser.add_argument("--test",    action="store_true", help="Fast test: skip signal_stack subprocess, use DB, limit 5 stocks (~1 min)")
    parser.add_argument("--health-check", action="store_true", help="Validate Telegram connectivity (getMe + chat_id) and exit")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # ── Health check mode: validate and exit ────────────────────────────────
    if args.health_check:
        ok = _validate_telegram()
        sys.exit(0 if ok else 1)

    # ── Startup validation (non-dry-run) ────────────────────────────────────
    if not args.dry_run:
        if not _validate_telegram():
            print("[scheduler] \u26a0\ufe0f Telegram validation FAILED — messages will NOT be delivered.", flush=True)
            print("[scheduler] Run with --dry-run to test locally, or fix credentials.", flush=True)
            sys.exit(1)
    else:
        print("[scheduler] Dry-run mode — skipping Telegram validation.", flush=True)

    scheduler = TelegramScheduler(
        dry_run=args.dry_run,
        force_market=args.force,
        test_mode=args.test,
        force_resend=args.force,   # --force bypasses both market gate AND dedup
    )

    if args.once:
        stats = scheduler.run_cycle()
        print(f"\n[scheduler] Done. Stats: {stats}", flush=True)
        return

    # Daemon loop
    print(f"[scheduler] Starting daemon — cycle every 4 hours. PID={os.getpid()}", flush=True)
    print(f"[scheduler] Dry-run={'yes' if args.dry_run else 'no'}  Force={'yes' if args.force else 'no'}", flush=True)
    print(f"[scheduler] First cycle starting immediately…", flush=True)

    while True:
        try:
            stats = scheduler.run_cycle()
            print(f"[scheduler] Sleeping 4h. Next cycle: {(datetime.now() + timedelta(hours=4)).strftime('%H:%M')}", flush=True)
        except KeyboardInterrupt:
            print("\n[scheduler] Interrupted by user. Exiting.", flush=True)
            break
        except Exception as exc:
            print(f"[scheduler] Unexpected cycle error: {exc}", flush=True)

        try:
            time.sleep(4 * 3600)  # 14400 seconds
        except KeyboardInterrupt:
            print("\n[scheduler] Interrupted during sleep. Exiting.", flush=True)
            break


if __name__ == "__main__":
    main()
