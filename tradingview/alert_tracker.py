"""
Alert Tracker — Durable Storage, Live Price Tracking & Performance Analysis
============================================================================
Stores every processed alert to SQLite, periodically snapshots live prices,
and computes signal performance metrics to identify what works and what does not.

Design decisions:
  • SQLite — zero-dependency, reliable, fast for thousands of rows.
  • yfinance — live price fetching (same dep as signal_stack.py).
  • 7-day tracking window — covers swing-trade holding periods.
  • Price snapshots at 1h, 4h, 1d, 3d, 7d nominal intervals.
  • Success threshold: +5% return at any snapshot within 7 days.
  • Drawdown threshold: −5% at any snapshot = failure.

Schema
------
    alerts             — one row per alert, full metadata + scores
    price_snapshots    — periodic price readings per alert
    performance_metrics— computed return/drawdown metrics per alert

Usage (from webhook_server.py):
    tracker = get_tracker()
    tracker.record_alert(fire, result)   # call after analyse_fire()

Standalone:
    python alert_tracker.py --update    # fetch prices for all open alerts
    python alert_tracker.py --report    # build HTML performance report
    python alert_tracker.py --stats     # print summary to console

Run --update on a schedule (e.g. every hour via Task Scheduler / cron).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Path setup ────────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
_PROJECT_ROOT = _THIS_DIR.parent
DB_PATH   = _PROJECT_ROOT / "database" / "alert_tracker.db"

# Import source tags (graceful fallback if not installed)
try:
    from input_sources import (
        SOURCE_WEBHOOK, SOURCE_DOWNLOAD, SOURCE_SCREENER, SOURCE_MANUAL,
    )
except ImportError:
    SOURCE_WEBHOOK  = "webhook"
    SOURCE_DOWNLOAD = "download"
    SOURCE_SCREENER = "screener"
    SOURCE_MANUAL   = "manual"

# ── yfinance (used for live price updates) ────────────────────────────────────
try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False

# ── Performance thresholds ────────────────────────────────────────────────────
SUCCESS_RETURN_PCT   = 5.0    # price moved ≥ +5% at any T+? → hit_target
FAILURE_DRAWDOWN_PCT = -5.0   # price moved ≤ -5% at any T+? → max_drawdown exceeded
EXPIRY_DAYS          = 7      # stop tracking after 7 calendar days
SNAPSHOT_INTERVALS_H = [1, 4, 24, 72, 168]  # hours: T+1h, T+4h, T+1d, T+3d, T+7d

# ── Max realistic return (sanity cap) ─────────────────────────────────────────
# Returns outside this range indicate a wrong ticker was used (e.g. TSX:IAU → AMEX:IAU)
# or a penny-stock artefact.  Snapshots outside this range are stored as NULL.
_MAX_SANE_RETURN_PCT = 500.0

# ── TradingView exchange → yfinance ticker suffix mapping ─────────────────────
# US exchanges need no suffix; non-US exchanges need one.
# None = exchange not supported by yfinance → skip price fetch entirely.
_EXCHANGE_YF_SUFFIX: dict[str, str | None] = {
    # US / OTC — no suffix needed
    "NASDAQ": "", "NYSE": "", "AMEX": "", "BATS": "", "OTC": "", "CBOE": "",
    "NYSE MKT": "", "NYSEARCA": "",
    # Canada
    "TSX": ".TO", "TSXV": ".V", "CSE": ".CN",
    # Australia
    "ASX": ".AX",
    # UK
    "LSE": ".L",
    # Hong Kong
    "HKEX": ".HK",
    # Japan — TYO/TSE both map to .T in yfinance
    "TSE": ".T", "TYO": ".T",
    # Singapore
    "SGX": ".SI",
    # Korea — KOSPI stocks use .KS, KOSDAQ stocks use .KQ
    "KRX": ".KS", "KOSDAQ": ".KQ",
    # India — NSE/BSE not reliably supported by yfinance; skip
    "NSE": None, "BSE": None,
    # Futures / indices — no price ticker equivalent
    "CME": None, "CBOT": None, "NYMEX": None, "MCX": None,
    "HSI": None, "SZSE": None, "SSE": None,
    # Europe
    "EURONEXT": ".PA",  # rough default; Euronext Paris tickers work, others may not
    "XETRA": ".DE",     # Official TV name for Xetra
    "XETR":  ".DE",     # TV sometimes uses XETR shortform — same .DE suffix
    "SIX": ".SW", "BME": ".MC",
    # Brazil — B3 tickers available in yfinance with .SA suffix
    "BMFBOVESPA": ".SA", "B3": ".SA",
    # Exchanges with no reliable yfinance coverage — explicitly skip
    "TASE": None,   # Tel Aviv Stock Exchange
    "MYX":  None,   # Malaysia Derivatives Exchange (futures)
    "IDX":  None,   # Indonesia Stock Exchange
    "SET":  None,   # Stock Exchange of Thailand
    "PSE":  None,   # Philippine Stock Exchange
}

# ── TV scanner URLs for exchanges that yfinance doesn't support ───────────────
# Used as a fallback in update_price_evolution() to fetch live prices from TV.
# Only include TRADABLE exchanges here (India NSE/BSE — yfinance has no coverage).
# Non-tradable exchanges (CBOT, EURONEXT, TASE, MYX etc.) removed 2026-04-20:
# they should never appear in the signal pipeline at all.
_TV_SCANNER_AT: dict[str, str] = {
    # India — NSE/BSE not supported by yfinance; use TV scanner for price fetching
    "NSE":  "https://scanner.tradingview.com/india/scan",
    "BSE":  "https://scanner.tradingview.com/india/scan",
}


def _fetch_tv_close_prices(tv_symbols: list) -> dict:
    """
    Batch-fetch the latest ``close`` price for a list of TradingView pro_symbols
    using the TV scanner API.  Used as a fallback when yfinance doesn't cover
    an exchange (e.g. CBOT futures, TASE, EURONEXT).

    Returns ``{tv_symbol: close_price_float}``.  Missing/failed symbols are omitted.
    """
    import json as _json
    import urllib.request as _ur

    by_url: dict[str, list[str]] = {}
    sym_map: dict[str, str] = {}   # bare_ticker → first pro_symbol seen

    for ps in tv_symbols:
        if ":" not in ps:
            continue
        exch, ticker = ps.split(":", 1)
        url = _TV_SCANNER_AT.get(exch.upper())
        if not url:
            continue
        by_url.setdefault(url, []).append(ticker)
        sym_map.setdefault(ticker, ps)   # keep first mapping (dedup)

    if not by_url:
        return {}

    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/",
    }
    results: dict = {}
    for url, tickers in by_url.items():
        payload = {
            "columns": ["name", "close"],
            "filter":  [{"left": "name", "operation": "in_range", "right": tickers}],
            "range":   [0, len(tickers) + 10],
        }
        try:
            body = _json.dumps(payload).encode()
            req  = _ur.Request(url, data=body, headers=headers)
            resp = _ur.urlopen(req, timeout=10)
            data = _json.loads(resp.read())
            for item in data.get("data", []):
                raw_sym  = item.get("s", "")
                d        = item.get("d", [])
                close_v  = d[1] if len(d) > 1 and d[1] is not None else None
                if close_v is None:
                    continue
                tok = raw_sym.split(":")[-1] if ":" in raw_sym else raw_sym
                ps  = sym_map.get(tok) or raw_sym
                try:
                    results[ps] = float(close_v)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass   # non-fatal
    return results


def _normalise_tv_symbol(tv_symbol: str) -> str:
    """
    Normalise a TradingView symbol string that may be wrapped in a JSON object.

    TradingView sometimes wraps symbols in JSON like:
        '={"adjustment":"splits","currency-id":"INR","session":"regular","symbol":"NSE:WAAREEENER"}'

    This function extracts the plain symbol (e.g. "NSE:WAAREEENER") from such wrappers.
    If the input is already a plain symbol (e.g. "NASDAQ:AAPL"), it is returned unchanged.

    Args:
        tv_symbol: Raw TV symbol string, possibly JSON-wrapped.

    Returns:
        Plain TV symbol string.
    """
    if not tv_symbol:
        return tv_symbol
    # Handle wrapped JSON format: ={"symbol":"EXCHANGE:TICKER",...}
    if tv_symbol.startswith("={") and '"symbol"' in tv_symbol:
        try:
            import json as _json
            data = _json.loads(tv_symbol[1:])
            return data.get("symbol", tv_symbol)
        except Exception:
            pass
    return tv_symbol


def _resolve_yf_ticker(tv_symbol: str, stored_yf_ticker: str = "") -> str | None:
    """
    Convert a TradingView symbol (e.g. ``TSX:IAU``) to a yfinance-compatible ticker.

    Priority:
    1. Use ``stored_yf_ticker`` if non-empty (already mapped by L2 analysis engine).
    2. Apply ``_EXCHANGE_YF_SUFFIX`` mapping based on the exchange prefix.
    3. Return ``None`` for exchanges not supported by yfinance (prevents fetching a
       completely different stock for the same bare ticker on another exchange).

    Examples:
        TSX:IAU  →  "IAU.TO"   (Iamgold Corp on Toronto, not AMEX iShares Gold)
        LSE:WISE →  "WISE.L"
        NSE:RELIANCE → None    (unsupported; skip price fetch)
        NASDAQ:AAPL  →  "AAPL"
    """
    if stored_yf_ticker:
        # Ignore garbage JSON fragments stored as yf_ticker (e.g. partial JSON
        # strings from wrapped TV symbols that were persisted incorrectly).
        # A valid yf_ticker is alphanumeric with optional dot suffix (e.g. AAPL, BHP.AX)
        import re as _re_yf
        if _re_yf.match(r'^[A-Za-z0-9.^-]{1,20}$', stored_yf_ticker.strip()):
            return stored_yf_ticker
        # Otherwise treat as invalid — fall through to exchange mapping
    if ":" not in tv_symbol:
        return tv_symbol  # no exchange prefix — assume US
    exchange, sym = tv_symbol.split(":", 1)
    suffix = _EXCHANGE_YF_SUFFIX.get(exchange.upper())
    if suffix is None:
        return None  # unsupported exchange
    return sym + suffix if suffix else sym


# ── Live price fetching ───────────────────────────────────────────────────────
# fast_info is I/O-bound and yfinance is thread-safe, so these run pooled.
# Matches parallel_fetch.WORKERS_YFINANCE_INFO.
_PRICE_FETCH_WORKERS = 8
# yfinance hourly bars are indexed by their start. A trusted point is limited
# to three completed bars while the market is open; a weekend/closure may
# leave the latest valid bar stale for up to 72 hours, but never a week.
_OBSERVATION_INTERVAL = timedelta(hours=1)
_OBSERVATION_OPEN_MAX_AGE = _OBSERVATION_INTERVAL * 3
_OBSERVATION_CLOSURE_MAX_AGE = timedelta(days=3)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_price(ticker: str) -> float | None:
    """Fetch one live price. Network only — no DB, so it is safe off the main thread."""
    if not _YF_OK:
        return None
    try:
        info = yf.Ticker(ticker).fast_info
        for attr in ("last_price", "last_close", "regularMarketPrice"):
            val = getattr(info, attr, None)
            if val and float(val) > 0:
                return float(val)
    except Exception:
        return None
    return None


def _fetch_observation(ticker: str) -> dict[str, Any] | None:
    """Return a completed-bar observation or an explicitly untrusted fallback.

    The provider timestamp is the bar start, so trusted observations use the
    bar end. A still-forming bar is never stored as a trusted outcome point.
    """
    available_at = _utc_now()
    if not _YF_OK:
        return None
    try:
        history = yf.Ticker(ticker).history(
            period="5d", interval="1h", auto_adjust=False
        )
        if history is not None and not history.empty:
            history = history.sort_index()
            raw_start = history.index[-1]
            bar_start = raw_start.to_pydatetime() if hasattr(raw_start, "to_pydatetime") else raw_start
            if bar_start.tzinfo is None or bar_start.utcoffset() is None:
                raise ValueError("provider bar timestamp is naive")
            bar_start = bar_start.astimezone(timezone.utc)
            bar_end = bar_start + _OBSERVATION_INTERVAL
            if available_at < bar_end:
                raise ValueError("provider bar is still forming")
            closure_window = available_at.weekday() >= 5 or (
                bar_end.weekday() == 4 and available_at.weekday() == 0
            )
            max_age = _OBSERVATION_CLOSURE_MAX_AGE if closure_window else _OBSERVATION_OPEN_MAX_AGE
            if available_at - bar_end > max_age:
                raise ValueError("provider bar timestamp is stale")
            price = float(history.iloc[-1]["Close"])
            if not math.isfinite(price):
                raise ValueError("provider close is non-finite")
            if price <= 0:
                raise ValueError("provider close is non-positive")
            return {
                "price": price,
                "observed_at": bar_end,
                "available_at": available_at,
                "price_source": "yfinance.history.last_completed_bar",
                "trusted": True,
                "reason": None,
            }
    except Exception as exc:
        if isinstance(exc, ValueError):
            reason = str(exc) or type(exc).__name__
        else:
            reason = f"provider error: {type(exc).__name__}: {exc}"
    else:
        reason = "history unavailable"

    price = _fetch_price(ticker)
    if price is None:
        return None
    return {
        "price": price,
        "observed_at": None,
        "available_at": available_at,
        "price_source": None,
        "trusted": False,
        "reason": reason,
    }


def _fetch_observations(tickers: list[str]) -> dict[str, dict[str, Any] | None]:
    """Fetch one structured observation per unique ticker."""
    if not tickers:
        return {}
    unique_tickers = list(dict.fromkeys(tickers))
    if len(unique_tickers) == 1:
        return {unique_tickers[0]: _fetch_observation(unique_tickers[0])}
    ex = ThreadPoolExecutor(max_workers=min(_PRICE_FETCH_WORKERS, len(unique_tickers)))
    try:
        return dict(zip(unique_tickers, ex.map(_fetch_observation, unique_tickers)))
    finally:
        ex.shutdown(wait=False)


def _fetch_prices(tickers: list[str]) -> dict[str, float | None]:
    """Fetch one price per unique ticker, concurrently."""
    if not tickers:
        return {}
    if len(tickers) == 1:
        return {tickers[0]: _fetch_price(tickers[0])}
    # SD-004: explicit executor + shutdown(wait=False); never block on a stuck fetch.
    ex = ThreadPoolExecutor(max_workers=min(_PRICE_FETCH_WORKERS, len(tickers)))
    try:
        return dict(zip(tickers, ex.map(_fetch_price, tickers)))
    finally:
        ex.shutdown(wait=False)


# ── Thread safety ─────────────────────────────────────────────────────────────
_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────
_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_uuid     TEXT    UNIQUE NOT NULL,   -- deterministic: source:symbol:fire_time
    tv_symbol      TEXT    NOT NULL,
    yf_ticker      TEXT,
    source         TEXT    NOT NULL,          -- webhook|download|screener|manual
    signal         TEXT,                      -- BUY|SELL|SCREENER|MANUAL
    fire_time      TEXT,                      -- ISO-8601 timestamp from the fire
    resolution     TEXT,                      -- '60'=1H, 'D'=daily, etc.
    entry_price    REAL,                      -- price at time of alert (may be None)
    composite_score REAL,
    l1_score       REAL,
    l2_score       REAL,
    l3_score       REAL,
    l4_score       REAL,
    l5_score       REAL,
    l6_score       REAL,
    l7_score       REAL,
    actionable     INTEGER DEFAULT 0,         -- 1 if composite >= threshold
    win_rate       REAL,
    rsi            REAL,
    adx            REAL,
    meta_json      TEXT,                      -- full fire dict (JSON)
    result_json    TEXT,                      -- full analysis result dict (JSON)
    created_at     TEXT    NOT NULL,
    expires_at     TEXT    NOT NULL           -- when we stop tracking prices
);

CREATE INDEX IF NOT EXISTS idx_alerts_symbol    ON alerts(tv_symbol);
CREATE INDEX IF NOT EXISTS idx_alerts_source    ON alerts(source);
CREATE INDEX IF NOT EXISTS idx_alerts_created   ON alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_composite ON alerts(composite_score);
CREATE INDEX IF NOT EXISTS idx_alerts_expires   ON alerts(expires_at);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_uuid     TEXT    NOT NULL REFERENCES alerts(alert_uuid),
    snapshot_time  TEXT    NOT NULL,          -- ISO-8601
    price          REAL    NOT NULL,
    elapsed_hours  REAL,                      -- hours since fire_time
    return_pct     REAL                       -- (price - entry_price)/entry_price * 100
);

CREATE INDEX IF NOT EXISTS idx_snaps_uuid ON price_snapshots(alert_uuid);
CREATE INDEX IF NOT EXISTS idx_snaps_time ON price_snapshots(snapshot_time);

CREATE TABLE IF NOT EXISTS performance_metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_uuid     TEXT    UNIQUE NOT NULL REFERENCES alerts(alert_uuid),
    computed_at    TEXT    NOT NULL,
    return_1h      REAL,   -- % return at T+1h snapshot
    return_4h      REAL,   -- % return at T+4h snapshot
    return_1d      REAL,   -- % return at T+24h snapshot
    return_3d      REAL,   -- % return at T+72h snapshot
    return_7d      REAL,   -- % return at T+168h snapshot
    max_return     REAL,   -- max positive return seen across all snapshots
    max_drawdown   REAL,   -- max negative return seen (most negative value)
    hit_target     INTEGER DEFAULT 0,  -- 1 if any snapshot >= SUCCESS_RETURN_PCT
    hit_stop       INTEGER DEFAULT 0,  -- 1 if any snapshot <= FAILURE_DRAWDOWN_PCT
    status         TEXT    DEFAULT 'open'  -- open|success|failure|expired
);

CREATE INDEX IF NOT EXISTS idx_perf_uuid   ON performance_metrics(alert_uuid);
CREATE INDEX IF NOT EXISTS idx_perf_status ON performance_metrics(status);
"""

_HORIZON_TOLERANCE_HOURS = {
    1.0: 0.75,
    4.0: 2.0,
    24.0: 6.0,
    72.0: 12.0,
    168.0: 24.0,
}


def _return_at_horizon(
    returns: list[tuple[float, float]],
    horizon_hours: float,
) -> float | None:
    """Return the nearest observation only when it is close to the horizon."""
    tolerance = _HORIZON_TOLERANCE_HOURS[horizon_hours]
    candidates = [
        (abs(elapsed - horizon_hours), value)
        for elapsed, value in returns
        if elapsed is not None and abs(elapsed - horizon_hours) <= tolerance
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]

# ── Migration: columns added after initial schema deployment ─────────────────
# Each ALTER is wrapped in a separate try/except so the tracker initialises
# cleanly whether the DB is brand-new or an existing install.
_MIGRATION_COLUMNS = [
    ("signal_engine_outputs", "scanner_setup_id", "ALTER TABLE signal_engine_outputs ADD COLUMN scanner_setup_id TEXT"),
    ("alerts", "signal_composite",       "ALTER TABLE alerts ADD COLUMN signal_composite       REAL"),
    ("alerts", "signal_threshold",       "ALTER TABLE alerts ADD COLUMN signal_threshold       REAL"),
    ("alerts", "tradable",               "ALTER TABLE alerts ADD COLUMN tradable               INTEGER DEFAULT 1"),
    ("alerts", "tradable_limitations",   "ALTER TABLE alerts ADD COLUMN tradable_limitations   INTEGER DEFAULT 0"),
    ("alerts", "watchlist_cleaned",      "ALTER TABLE alerts ADD COLUMN watchlist_cleaned      INTEGER DEFAULT 0"),
    ("alerts", "input_source",           "ALTER TABLE alerts ADD COLUMN input_source           TEXT"),
    ("alerts", "win_rate_at_fire",       "ALTER TABLE alerts ADD COLUMN win_rate_at_fire       REAL"),
    ("alerts", "vix_at_fire",            "ALTER TABLE alerts ADD COLUMN vix_at_fire            REAL"),
    ("alerts", "market_regime",          "ALTER TABLE alerts ADD COLUMN market_regime          TEXT"),
    # ── New columns added in batch ────────────────────────────────────────
    ("alerts", "net_profit_pct",         "ALTER TABLE alerts ADD COLUMN net_profit_pct         REAL"),
    ("alerts", "squeeze_off",            "ALTER TABLE alerts ADD COLUMN squeeze_off             INTEGER DEFAULT 0"),
    ("alerts", "adx_at_entry",           "ALTER TABLE alerts ADD COLUMN adx_at_entry           REAL"),
    ("alerts", "rsi_at_entry",           "ALTER TABLE alerts ADD COLUMN rsi_at_entry           REAL"),
    ("alerts", "ma_cross_at_entry",      "ALTER TABLE alerts ADD COLUMN ma_cross_at_entry      INTEGER DEFAULT 0"),
    ("alerts", "win_rate_pct",           "ALTER TABLE alerts ADD COLUMN win_rate_pct           REAL"),
    ("alerts", "l5_score",               "ALTER TABLE alerts ADD COLUMN l5_score               REAL"),
    ("alerts", "plus_di",                "ALTER TABLE alerts ADD COLUMN plus_di                REAL"),
    ("alerts", "minus_di",               "ALTER TABLE alerts ADD COLUMN minus_di               REAL"),
    ("alerts", "trigger_price",          "ALTER TABLE alerts ADD COLUMN trigger_price          REAL"),
    # ── Options positions new columns ──────────────────────────────────────
    ("options_positions", "dte_at_entry",            "ALTER TABLE options_positions ADD COLUMN dte_at_entry            INTEGER"),
    ("options_positions", "moneyness",               "ALTER TABLE options_positions ADD COLUMN moneyness               TEXT"),
    ("options_positions", "underlying_price_at_open","ALTER TABLE options_positions ADD COLUMN underlying_price_at_open REAL"),
    ("options_positions", "pnl_pct",                 "ALTER TABLE options_positions ADD COLUMN pnl_pct                 REAL"),
    ("options_positions", "hold_days",               "ALTER TABLE options_positions ADD COLUMN hold_days               INTEGER"),
    ("options_positions", "outcome",                 "ALTER TABLE options_positions ADD COLUMN outcome                 TEXT DEFAULT 'OPEN'"),
    ("options_positions", "first_seen_at",           "ALTER TABLE options_positions ADD COLUMN first_seen_at           TEXT"),
    ("options_positions", "last_seen_at",            "ALTER TABLE options_positions ADD COLUMN last_seen_at            TEXT"),
    # ── Delisted ticker detection (P2-B) ─────────────────────────────────
    ("alerts", "is_delisted",             "ALTER TABLE alerts ADD COLUMN is_delisted             INTEGER DEFAULT 0"),
    # ── Book features (VPA, Darvas, Gap, AM, RS, Expectancy) ──────────
    ("alerts", "book_vpa_score",          "ALTER TABLE alerts ADD COLUMN book_vpa_score          REAL"),
    ("alerts", "book_darvas_score",       "ALTER TABLE alerts ADD COLUMN book_darvas_score       REAL"),
    ("alerts", "book_gap_score",          "ALTER TABLE alerts ADD COLUMN book_gap_score          REAL"),
    ("alerts", "book_am_score",           "ALTER TABLE alerts ADD COLUMN book_am_score           REAL"),
    ("alerts", "book_rs_score",           "ALTER TABLE alerts ADD COLUMN book_rs_score           REAL"),
    ("alerts", "book_expectancy_score",   "ALTER TABLE alerts ADD COLUMN book_expectancy_score   REAL"),
    ("alerts", "book_total_bonus",        "ALTER TABLE alerts ADD COLUMN book_total_bonus        REAL"),
    # ── Moneyball options scoring (population-relative) ──────────────
    ("options_recommendations", "moneyball_score", "ALTER TABLE options_recommendations ADD COLUMN moneyball_score REAL"),
    ("options_recommendations", "final_score",     "ALTER TABLE options_recommendations ADD COLUMN final_score     REAL"),
]

_SCHEMA_NEW_TABLES = """
CREATE TABLE IF NOT EXISTS signal_engine_outputs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    tv_symbol       TEXT,
    timestamp       TEXT NOT NULL,
    signal_type     TEXT,
    composite_score REAL,
    confidence_band TEXT,
    l1_score        REAL,
    l2_score        REAL,
    l3_score        REAL,
    l4_score        REAL,
    win_rate        REAL,
    threshold_used  REAL,
    price_at_signal REAL,
    vix_at_signal   REAL,
    market_regime   TEXT,
    input_source    TEXT,
    parameters_json TEXT,
    tradable        INTEGER DEFAULT 1,
    run_id          TEXT,
    scanner_setup_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_seo_ticker    ON signal_engine_outputs(ticker);
CREATE INDEX IF NOT EXISTS idx_seo_timestamp ON signal_engine_outputs(timestamp);
CREATE INDEX IF NOT EXISTS idx_seo_run_id    ON signal_engine_outputs(run_id);

CREATE TABLE IF NOT EXISTS price_evolution (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id        INTEGER REFERENCES alerts(id),
    output_id       INTEGER REFERENCES signal_engine_outputs(id),
    snapshot_at     TEXT NOT NULL,
    hours_elapsed   REAL,
    price           REAL,
    return_pct      REAL,
    max_favorable   REAL,
    max_adverse     REAL,
    outcome         TEXT DEFAULT 'open'
);

CREATE INDEX IF NOT EXISTS idx_pe_alert_id  ON price_evolution(alert_id);
CREATE INDEX IF NOT EXISTS idx_pe_output_id ON price_evolution(output_id);

-- ── Options positions tracking ───────────────────────────────────────────────
-- Records every options position captured from broker cache or signal alerts.
-- op_uuid = OCC contract symbol (e.g. NVDA260501C210000) for broker-sourced,
--           or a UUID for manually recorded trades.
-- alert_uuid links back to the alerts table (the signal that triggered the trade).
CREATE TABLE IF NOT EXISTS options_positions (
    op_uuid                  TEXT PRIMARY KEY,
    alert_uuid               TEXT,
    tv_symbol                TEXT NOT NULL,
    contract_symbol          TEXT,
    strategy                 TEXT NOT NULL,
    strike                   REAL,
    expiry                   TEXT,
    premium_paid             REAL,
    quantity                 INTEGER DEFAULT 1,
    open_date                TEXT NOT NULL,
    status                   TEXT DEFAULT 'open',
    close_date               TEXT,
    close_premium            REAL,
    pnl_usd                  REAL,
    broker                   TEXT DEFAULT 'Moomoo-AU',
    notes                    TEXT,
    created_at               TEXT DEFAULT (datetime('now')),
    dte_at_entry             INTEGER,
    moneyness                TEXT,
    underlying_price_at_open REAL,
    pnl_pct                  REAL,
    hold_days                INTEGER,
    outcome                  TEXT DEFAULT 'OPEN',
    first_seen_at            TEXT,
    last_seen_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_op_alert_uuid ON options_positions(alert_uuid);
CREATE INDEX IF NOT EXISTS idx_op_tv_symbol  ON options_positions(tv_symbol);
CREATE INDEX IF NOT EXISTS idx_op_status     ON options_positions(status);

-- ── Options recommendations tracking ────────────────────────────────────────
-- Records every contract recommended by options_report.html at generation time.
-- Enables win/loss tracking by mode, DTE bucket, moneyness, score.
-- rec_uuid = deterministic UUID4 keyed on run_date+tv_symbol+mode+strike+expiry.
CREATE TABLE IF NOT EXISTS options_recommendations (
    rec_uuid             TEXT PRIMARY KEY,
    run_date             TEXT NOT NULL,         -- ISO date "2026-04-25"
    run_timestamp        TEXT,                  -- full ISO datetime
    tv_symbol            TEXT NOT NULL,
    signal_score         REAL,                  -- composite L1-L4 (0-100)
    l1_score             REAL,
    l2_score             REAL,
    l3_score             REAL,
    l4_score             REAL,
    news_sentiment_score REAL,                  -- sentiment_data.news.score
    news_count           INTEGER,               -- headline count
    earnings_days_away   INTEGER,               -- days to next earnings
    earnings_risk        TEXT,                  -- "High" / "Low" / "None"
    pcr                  REAL,                  -- put/call ratio
    vix_at_rec           REAL,                  -- VIX at recommendation time
    mode                 TEXT NOT NULL,         -- SAFE/BALANCED/AGGRESSIVE
    rank                 INTEGER DEFAULT 1,     -- 1=best pick, 2=second, ...
    option_type          TEXT,                  -- CALL/PUT
    strike               REAL,
    expiry               TEXT,                  -- "2026-05-01"
    dte_at_rec           INTEGER,
    dte_bucket           TEXT,                  -- "0-7"/"8-21"/"22-45"/"45+"
    spot_price           REAL,                  -- underlying at rec time
    moneyness_pct        REAL,                  -- %OTM (+) or %ITM (-)
    moneyness_label      TEXT,                  -- ITM/ATM/OTM
    premium              REAL,                  -- mid_price
    delta                REAL,
    theta                REAL,
    iv_pct               REAL,
    volume               INTEGER,
    open_interest        INTEGER,
    spread_pct           REAL,
    option_score         REAL,                  -- 0-100 Moneyball-blended score (= final_score)
    liq_score            REAL,
    iv_score             REAL,
    strike_score         REAL,
    dte_score            REAL,
    moneyball_score      REAL,                  -- 0-100 chain-relative population score
    final_score          REAL,                  -- 0.60*current_score + 0.40*moneyball_score
    market               TEXT,                  -- US/NSE/HK
    lot_size             INTEGER DEFAULT 1,
    data_source          TEXT,                  -- "moomoo"/"yfinance"
    outcome              TEXT DEFAULT 'OPEN',   -- OPEN/WIN/LOSS/EXPIRED
    outcome_checked_at   TEXT,
    close_spot           REAL,                  -- underlying at outcome check
    close_date           TEXT,
    created_at           TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orec_run_date  ON options_recommendations(run_date);
CREATE INDEX IF NOT EXISTS idx_orec_tv_symbol ON options_recommendations(tv_symbol);
CREATE INDEX IF NOT EXISTS idx_orec_mode      ON options_recommendations(mode);
CREATE INDEX IF NOT EXISTS idx_orec_outcome   ON options_recommendations(outcome);

-- ── US Options Screener scan results ────────────────────────────────────────
-- Records every contract surfaced by options_contract_screener.py (the US 300
-- scan).  One row per contract per scan run.  Enables vectorbt backtesting:
-- "did TQS≥80 contracts actually profit after 7/14/30 days?"
-- scan_id     = ISO datetime of the scan run (same across all rows in one scan)
-- scan_rank   = global sort rank within this scan (1 = best TQS)
-- outcome     = OPEN (default) → WIN / LOSS / EXPIRED (filled by outcome checker)
-- result_json = full contract dict (all hybrid_options.py fields) as JSON blob
CREATE TABLE IF NOT EXISTS options_scan_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id          TEXT NOT NULL,             -- ISO datetime "2026-05-11T14:22:00Z"
    scan_date        TEXT NOT NULL,             -- ISO date  "2026-05-11"
    scan_rank        INTEGER,                   -- 1 = best TQS in this scan
    tv_symbol        TEXT NOT NULL,             -- "NASDAQ:NVDA"
    code             TEXT NOT NULL,             -- "OPRA:NVDA260515C200.0"
    option_type      TEXT,                      -- "CALL" / "PUT"
    strike           REAL,
    expiry           TEXT,                      -- "2026-05-16"
    dte              INTEGER,
    spot_price       REAL,                      -- underlying price at scan time
    moneyness_pct    REAL,
    bid              REAL,
    ask              REAL,
    mid_price        REAL,
    iv               REAL,                      -- IV % (TV scan2 × 100)
    bid_iv           REAL,
    ask_iv           REAL,
    delta            REAL,
    gamma            REAL,
    theta            REAL,
    vega             REAL,
    theo_price       REAL,
    spread_pct       REAL,
    oi               INTEGER,                   -- open interest (Moomoo source)
    oi_source        TEXT,                      -- "moomoo" / "unavailable"
    volume           INTEGER,                   -- from TV scan2 (0 when closed)
    ws_volume        INTEGER,                   -- last-session volume from WS
    ws_lp            REAL,                      -- last price from WS
    ohlc_range_pct   REAL,
    pressure         REAL,                      -- bid/ask size pressure -1..+1
    iv_spread        REAL,                      -- |ask_iv - bid_iv|
    tqs_v2           REAL NOT NULL,             -- Trade Quality Score 0-100 ★
    tqs_v2_badge     TEXT,                      -- "[Market Hours]" etc.
    ws_source        TEXT,                      -- "live"/"last-session"/"unavailable"
    -- Screener sub-scores (from inject_screener_subscores)
    screener_score   REAL,
    signal_score     REAL,
    l1_score         REAL,
    l2_score         REAL,
    l3_score         REAL,
    l4_score         REAL,
    -- Outcome tracking (filled by options_outcome_checker.py)
    outcome          TEXT DEFAULT 'OPEN',       -- OPEN/WIN/LOSS/EXPIRED
    outcome_checked_at TEXT,
    close_spot       REAL,                      -- underlying at close/check
    close_date       TEXT,
    pnl_pct          REAL,                      -- % gain/loss on option premium
    -- Full data blob for any field not listed above
    result_json      TEXT,                      -- JSON of complete contract dict
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_osr_scan_id    ON options_scan_results(scan_id);
CREATE INDEX IF NOT EXISTS idx_osr_scan_date  ON options_scan_results(scan_date);
CREATE INDEX IF NOT EXISTS idx_osr_tv_symbol  ON options_scan_results(tv_symbol);
CREATE INDEX IF NOT EXISTS idx_osr_tqs_v2     ON options_scan_results(tqs_v2);
CREATE INDEX IF NOT EXISTS idx_osr_outcome    ON options_scan_results(outcome);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_uuid(source: str, symbol: str, fire_time: str) -> str:
    """Deterministic ID for deduplication — no external libs required."""
    raw = f"{source}:{symbol}:{fire_time}"
    # Simple 64-bit FNV-1a hash (no hashlib needed)
    h = 14695981039346656037
    for ch in raw.encode():
        h ^= ch
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _parse_signal(message: str) -> str:
    """Extract BUY/SELL/SCREENER signal tag from message."""
    upper = message.upper()
    if upper.startswith("SCREENER"):
        return "SCREENER"
    if upper.startswith("MANUAL"):
        return "MANUAL"
    if re.search(r'\bBUY\b', upper):
        return "BUY"
    if re.search(r'\bSELL\b', upper):
        return "SELL"
    return "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# AlertTracker
# ─────────────────────────────────────────────────────────────────────────────
class AlertTracker:
    """
    Thread-safe SQLite-backed alert tracker.

    Typical lifecycle:
        tracker = AlertTracker()
        # After every analyse_fire() call:
        tracker.record_alert(fire, result)

        # Periodically (e.g. every hour via cron/scheduler):
        tracker.update_prices()

        # For analysis:
        report_html = tracker.build_performance_report()
        stats = tracker.get_stats()
    """
    _native_outcome_tracking = True

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Thread-safe connection context manager."""
        conn = sqlite3.connect(str(self._db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.executescript(_SCHEMA_NEW_TABLES)
            from tradingview.outcome_store import ensure_schema
            ensure_schema(conn)
            # Migrate columns added after v1 schema.
            # Legacy approach (idempotent ALTER TABLE per column) kept as
            # fallback; the versioned migration system in database/migrations.py
            # tracks which migrations have already run via schema_version table.
            try:
                _db_parent = str(_THIS_DIR.parent)
                if _db_parent not in sys.path:
                    sys.path.insert(0, _db_parent)
                from database.migrations import run_migrations
                run_migrations("alert_tracker", conn)
            except Exception:
                # Fallback to legacy per-ALTER approach if migrations module
                # is unavailable (e.g. running alert_tracker.py standalone)
                for _table, _col, _sql in _MIGRATION_COLUMNS:
                    try:
                        conn.execute(_sql)
                    except Exception:
                        pass   # column already exists
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_seo_scanner_setup "
                "ON signal_engine_outputs(scanner_setup_id) WHERE scanner_setup_id IS NOT NULL"
            )

    # ── Record alert ─────────────────────────────────────────────────────────
    def record_alert(self, fire: dict, result: dict) -> str | None:
        """
        Persist an alert and its analysis result.

        Parameters
        ----------
        fire   : dict  — the fire dict (from webhook / screener / manual)
        result : dict  — the full dict returned by analyse_fire()

        Returns
        -------
        alert_uuid on success; None if the alert was a duplicate.
        """
        tv_sym    = _normalise_tv_symbol(fire.get("pro_symbol") or fire.get("symbol", ""))
        source    = fire.get("_source", SOURCE_WEBHOOK)
        fire_time = fire.get("fire_time") or _now_iso()
        uuid      = _make_uuid(source, tv_sym, fire_time)

        scores    = result.get("scores", {})
        comp      = _safe_float(result.get("composite"))
        l1        = _safe_float((scores.get("l1") or {}).get("total"))
        l2        = _safe_float((scores.get("l2") or {}).get("total"))
        l3        = _safe_float((scores.get("l3") or {}).get("total"))
        l4        = _safe_float((scores.get("l4") or {}).get("total"))
        l5_enr    = result.get("l5_enrichment") or {}
        l6_enr    = result.get("l6_enrichment") or {}
        l7_enr    = result.get("l7_enrichment") or {}
        l5        = _safe_float(l5_enr.get("l5_score") if l5_enr else None)
        l6        = _safe_float(l6_enr.get("l6_score") if l6_enr else None)
        l7        = _safe_float(l7_enr.get("l7_score") if l7_enr else None)

        fund      = result.get("fundamentals") or {}
        entry_price = _safe_float(fund.get("current_price") or fire.get("close"))

        # meta: prefer from fire dict (set by signal_stack/webhook_server callers);
        # fall back to result dict where analyse_fire stores the parsed meta.
        meta      = fire.get("meta") or result.get("meta") or {}
        signal    = _parse_signal(fire.get("message", ""))
        actionable = 1 if (comp is not None and result.get("actionable_at")) else 0

        # win_rate: prefer strategy WinRate from Pine alert meta (webhook fires);
        # fall back to backtest win_rate computed by position_sizer (all other sources)
        _bd_data  = result.get("backtest_data") or {}
        win_rate  = _safe_float(meta.get("WinRate") or _bd_data.get("win_rate"))

        # rsi/adx: from parsed fire meta (set for screener fires via message parsing)
        rsi_val   = _safe_float(meta.get("rsi") or meta.get("RSI"))
        adx_val   = _safe_float(meta.get("ADX"))
        # New columns: +DI, -DI, trigger_price, net_profit_pct, squeeze_off, ma_cross, win_rate_pct
        plus_di_val    = _safe_float(meta.get("+DI"))
        minus_di_val   = _safe_float(meta.get("-DI"))
        trigger_price  = _safe_float(meta.get("trigger_price"))
        net_profit_pct = _safe_float(meta.get("NetProfit%"))
        squeeze_off    = 1 if meta.get("sqzOff") else 0
        ma_cross       = 1 if meta.get("ma_up") else 0
        win_rate_pct   = _safe_float(meta.get("WinRate") or _bd_data.get("win_rate"))

        # ── Book features (VPA, Darvas, Gap, AM, RS, Expectancy) ──────────
        _bf = result.get("book_features") or {}
        book_vpa     = _safe_float(_bf.get("vpa", {}).get("score"))
        book_darvas  = _safe_float(_bf.get("darvas", {}).get("score"))
        book_gap     = _safe_float(_bf.get("gap", {}).get("score"))
        book_am      = _safe_float(_bf.get("am", {}).get("score"))
        book_rs      = _safe_float(_bf.get("rs", {}).get("score"))
        book_exp     = _safe_float(_bf.get("expectancy", {}).get("score"))
        book_total   = _safe_float(_bf.get("total_bonus"))

        created   = _now_iso()
        expires   = (datetime.now(timezone.utc) + timedelta(days=EXPIRY_DAYS)).isoformat()
        outcome_payload = result.get("outcome_measurement") or {}
        if outcome_payload:
            from tradingview.outcome_store import measurement as _outcome_measurement
            _outcome_measurement(
                {
                    "alert_uuid": uuid,
                    "fire_time": fire_time,
                    "created_at": created,
                },
                outcome_payload,
            )

        # Truncate large JSONs to prevent DB bloat (keep essential fields)
        def _slim(d: dict, max_keys: int = 80) -> str:
            if not isinstance(d, dict):
                return json.dumps(d, default=str)
            keys = list(d.keys())[:max_keys]
            return json.dumps({k: d[k] for k in keys}, default=str)

        try:
            with _lock, self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO alerts (
                        alert_uuid, tv_symbol, yf_ticker, source, signal,
                        fire_time, resolution, entry_price, composite_score,
                        l1_score, l2_score, l3_score, l4_score,
                        l5_score, l6_score, l7_score, actionable,
                        win_rate, rsi, adx,
                        net_profit_pct, squeeze_off, adx_at_entry, rsi_at_entry,
                        ma_cross_at_entry, win_rate_pct,
                        plus_di, minus_di, trigger_price,
                        book_vpa_score, book_darvas_score, book_gap_score,
                        book_am_score, book_rs_score, book_expectancy_score,
                        book_total_bonus,
                        meta_json, result_json, created_at, expires_at
                    ) VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        uuid, tv_sym,
                        _resolve_yf_ticker(tv_sym, result.get("yf_ticker", "") or "") or "",
                        source, signal,
                        fire_time,
                        fire.get("resolution", "D"),
                        entry_price, comp,
                        l1, l2, l3, l4, l5, l6, l7,
                        actionable,
                        win_rate,
                        rsi_val,
                        adx_val,
                        net_profit_pct, squeeze_off, adx_val, rsi_val,
                        ma_cross, win_rate_pct,
                        plus_di_val, minus_di_val, trigger_price,
                        book_vpa, book_darvas, book_gap,
                        book_am, book_rs, book_exp,
                        book_total,
                        _slim(fire),
                        _slim(result),
                        created, expires,
                    ),
                )
                # INSERT OR IGNORE: if rows_affected == 0 it's a duplicate
                rows = conn.execute(
                    "SELECT changes()"
                ).fetchone()[0]
                from tradingview.outcome_store import (
                    record_entry as _record_outcome_entry,
                    record_resolution_mapping as _record_resolution_mapping,
                )
                _record_resolution_mapping(conn, uuid, fire, source)
                if outcome_payload:
                    _outcome_row = conn.execute(
                        "SELECT alert_uuid, fire_time, created_at FROM alerts WHERE alert_uuid=?",
                        (uuid,),
                    ).fetchone()
                    _record_outcome_entry(conn, dict(_outcome_row), outcome_payload)

            return uuid if rows > 0 else None
        except Exception as exc:
            if outcome_payload:
                raise
            print(f"[alert_tracker] record_alert failed: {exc}", flush=True)
            return None

    # ── Snapshot price for a single alert ────────────────────────────────────
    def _snapshot_price_for(
        self,
        conn: sqlite3.Connection,
        alert_uuid: str,
        tv_symbol: str,
        yf_ticker: str,
        fire_time_str: str,
        entry_price: float | None,
    ) -> float | None:
        """
        Fetch the current price for one alert and write a snapshot row.
        Returns the current price (or None on failure).
        """
        if not _YF_OK:
            return None

        resolved = _resolve_yf_ticker(tv_symbol, yf_ticker)
        if resolved is None:
            return None  # unsupported exchange — skip price fetch

        observation = _fetch_observation(resolved)
        if observation is None:
            return None

        self._write_snapshot(
            conn, alert_uuid, observation["price"], fire_time_str, entry_price,
            observed_at=(observation["observed_at"].isoformat()
                         if observation.get("observed_at") else None),
            available_at=observation["available_at"].isoformat(),
            price_source=observation.get("price_source"),
            observation_reason=observation.get("reason"),
        )
        return observation["price"]

    def _write_snapshot(
        self,
        conn: sqlite3.Connection,
        alert_uuid: str,
        price: float,
        fire_time_str: str,
        entry_price: float | None,
        *, observed_at: str | None = None, available_at: str | None = None,
        price_source: str | None = None, observation_reason: str | None = None,
    ) -> None:
        """Write one price_snapshots row for an already-fetched price."""
        now_iso = _now_iso()
        fire_dt = None
        try:
            fire_dt = datetime.fromisoformat(
                fire_time_str.replace("Z", "+00:00")
            )
            if fire_dt.tzinfo is None:
                fire_dt = fire_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

        elapsed_h = None
        return_pct = None
        if fire_dt:
            elapsed_h = (datetime.now(timezone.utc) - fire_dt).total_seconds() / 3600
        if entry_price and entry_price > 0:
            rp = round((price - entry_price) / entry_price * 100, 4)
            # Sanity cap: returns outside ±_MAX_SANE_RETURN_PCT suggest a wrong
            # ticker was resolved (e.g. same bare ticker on a different exchange).
            if abs(rp) <= _MAX_SANE_RETURN_PCT:
                return_pct = rp

        conn.execute(
            """
            INSERT INTO price_snapshots
                (alert_uuid, snapshot_time, price, elapsed_hours, return_pct)
            VALUES (?,?,?,?,?)
            """,
            (alert_uuid, now_iso, price, elapsed_h, return_pct),
        )
        from tradingview.outcome_store import record_observation_attempt, record_point
        record_observation_attempt(
            conn, alert_uuid, price, observed_at, available_at,
            price_source, observed_at is not None and bool(price_source),
            observation_reason,
        )
        record_point(conn, alert_uuid, price, observed_at, available_at, price_source)

    # ── Record signal engine output ──────────────────────────────────────────

    def record_signal_output(self, result: dict, run_id: str = "") -> int | None:
        """
        Insert one row into ``signal_engine_outputs`` for every scored result.

        Called from BatchAnalyzer.run() and WebhookServer._process_fire().

        Parameters
        ----------
        result : dict   — the analysis result dict (from analyse_fire / batch_analyzer)
        run_id : str    — UUID string identifying the batch run (empty for webhook fires)

        Returns
        -------
        row id on success; None on failure.
        """
        import uuid as _uuid_mod

        tv_sym  = result.get("tv_symbol", result.get("symbol", ""))
        ticker  = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
        ts      = result.get("analysis_timestamp") or _now_iso()
        sc      = result.get("scores", {})
        comp    = _safe_float(result.get("composite"))
        l1      = _safe_float((sc.get("l1") or {}).get("total"))
        l2      = _safe_float((sc.get("l2") or {}).get("total"))
        l3      = _safe_float((sc.get("l3") or {}).get("total"))
        l4      = _safe_float((sc.get("l4") or {}).get("total"))

        # Confidence band
        if comp is not None:
            if comp >= 75:
                confidence_band = "high"
            elif comp >= 50:
                confidence_band = "medium"
            else:
                confidence_band = "low"
        else:
            confidence_band = None

        fund        = result.get("fundamentals") or {}
        price_sig   = _safe_float(fund.get("current_price"))
        meta        = result.get("meta") or {}
        win_rate_v  = _safe_float(meta.get("WinRate"))
        threshold_v = _safe_float(result.get("threshold_used"))
        sig_type    = result.get("signal_type", "")
        if not sig_type:
            sig_type = "actionable" if result.get("actionable_at") else "scored"
        input_src   = result.get("_source", result.get("input_source", ""))
        tradable    = 0 if result.get("_tradability", {}).get("tradable") is False else 1

        # VIX / market regime — if enriched by L5/L6 callers
        market_data = result.get("market_internals") or {}
        vix_sig     = _safe_float(
            result.get("vix_at_signal") or result.get("vix") or market_data.get("vix_level")
        )
        regime      = result.get("market_regime", "")

        params_json = json.dumps({
            "model_version":      result.get("score_model_version"),
            "fire_time":          result.get("fire_time"),
            "resolution":         result.get("resolution"),
            "quality_score":      result.get("quality_score"),
            "decision_score":     result.get("composite"),
            "scores":             sc,
            "l5_score":           (result.get("l5_enrichment") or {}).get("l5_score"),
            "l6_context_score":   (result.get("l6_enrichment") or {}).get("l6_score"),
            "l7_context_score":   (result.get("l7_enrichment") or {}).get("l7_score"),
            "active_age_hours":   result.get("age_hours"),
            "calendar_age_hours": result.get("calendar_age_hours"),
            "regime_weights":     result.get("regime_weights"),
            "data_confidence":    result.get("data_confidence"),
            "actionability_reasons": result.get("actionability_reasons", []),
            "overlays": {
                "pool_member":          result.get("in_pool"),
                "pool_is_fresh":        result.get("pool_is_fresh"),
                "smallcase_member":     result.get("sc_in_basket"),
                "smallcase_preference": result.get("sc_preference_score"),
                "book_context_score":   result.get("book_context_score"),
                "india_portfolio_l5":   (result.get("india_l5_enrichment") or {}).get("l5_score"),
            },
            "tradability": result.get("_tradability", {}),
            "fundamentals": {
                key: fund.get(key)
                for key in ("sector", "market_cap", "forward_pe", "current_price")
            },
        }, default=str)

        run_id_final = run_id or ""

        try:
            with _lock, self._conn() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO signal_engine_outputs (
                        ticker, tv_symbol, timestamp, signal_type,
                        composite_score, confidence_band,
                        l1_score, l2_score, l3_score, l4_score,
                        win_rate, threshold_used, price_at_signal, vix_at_signal,
                        market_regime, input_source, parameters_json,
                        tradable, run_id, scanner_setup_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ticker, tv_sym, ts, sig_type,
                        comp, confidence_band,
                        l1, l2, l3, l4,
                        win_rate_v, threshold_v, price_sig, vix_sig,
                        regime, input_src, params_json,
                        tradable, run_id_final, result.get("_scanner_setup_id"),
                    ),
                )
                if cur.rowcount == 1:
                    return cur.lastrowid
                if result.get("_scanner_setup_id"):
                    existing = conn.execute(
                        "SELECT id FROM signal_engine_outputs WHERE scanner_setup_id=?",
                        (result["_scanner_setup_id"],),
                    ).fetchone()
                    return int(existing[0]) if existing else None
                return None
        except Exception as exc:
            print(f"[alert_tracker] record_signal_output failed: {exc}", flush=True)
            return None

    # ── Update price evolution (MFE/MAE) ─────────────────────────────────────

    def update_price_evolution(self, max_outputs: int = 2000) -> dict:
        """
        For all open ``signal_engine_outputs`` within EXPIRY_DAYS:
          - fetch current price via yfinance
          - compute return_pct, rolling max_favorable (MFE), rolling max_adverse (MAE)
          - classify outcome: success / failure / open

        Returns summary {"updated": N, "failed": N}.
        """
        if not _YF_OK:
            return {"updated": 0, "failed": 0, "error": "yfinance not installed"}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=EXPIRY_DAYS)).isoformat()
        with _lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, tv_symbol, timestamp, price_at_signal
                FROM   signal_engine_outputs
                WHERE  timestamp > ?
                ORDER  BY timestamp DESC
                LIMIT  ?
                """,
                (cutoff, max_outputs),
            ).fetchall()

        updated = failed = 0

        # ── Pre-fetch TV prices for exchanges yfinance doesn't support ────────
        tv_only_symbols = [
            row["tv_symbol"] or ""
            for row in rows
            if _resolve_yf_ticker(row["tv_symbol"] or "", "") is None
               and (row["tv_symbol"] or "").strip()
        ]
        tv_close_cache: dict = _fetch_tv_close_prices(tv_only_symbols) if tv_only_symbols else {}

        # One network call per unique ticker instead of one per row.
        yf_price_cache = _fetch_prices(sorted({
            t for t in (_resolve_yf_ticker(r["tv_symbol"] or "", "") for r in rows) if t
        }))

        # Prior MFE/MAE for every row in a few bulk queries, not one query per row.
        prior_by_output = self._latest_evolution_by_output([r["id"] for r in rows])

        with _lock, self._conn() as conn:
            for row in rows:
                output_id    = row["id"]
                tv_sym_row   = row["tv_symbol"] or ""
                price_entry  = row["price_at_signal"]
                ts_str       = row["timestamp"]

                resolved = _resolve_yf_ticker(tv_sym_row, "")
                if resolved is None:
                    # TV scanner fallback for exchanges yfinance doesn't cover
                    price = tv_close_cache.get(tv_sym_row)
                else:
                    price = yf_price_cache.get(resolved)

                if price is None:
                    failed += 1
                    continue

                try:
                    fired_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if fired_at.tzinfo is None:
                        fired_at = fired_at.replace(tzinfo=timezone.utc)
                    hours_elapsed = (datetime.now(timezone.utc) - fired_at).total_seconds() / 3600
                except Exception:
                    hours_elapsed = None

                return_pct = None
                if price_entry and price_entry > 0:
                    rp = round((price - price_entry) / price_entry * 100, 4)
                    # Sanity cap: discard snapshot if return is implausibly large
                    if abs(rp) <= _MAX_SANE_RETURN_PCT:
                        return_pct = rp

                prior = prior_by_output.get(output_id)
                prev_mfe = prior[0] if prior else None
                prev_mae = prior[1] if prior else None

                if return_pct is not None:
                    prior_mfe = prev_mfe if prev_mfe is not None else return_pct
                    prior_mae = prev_mae if prev_mae is not None else return_pct
                    max_favorable = max(prior_mfe, return_pct)
                    max_adverse   = min(prior_mae, return_pct)
                else:
                    max_favorable = prev_mfe
                    max_adverse   = prev_mae

                # Outcome classification
                if max_favorable is not None and max_favorable >= SUCCESS_RETURN_PCT:
                    outcome = "success"
                elif max_adverse is not None and max_adverse <= FAILURE_DRAWDOWN_PCT:
                    outcome = "failure"
                else:
                    outcome = "open"

                try:
                    conn.execute(
                        """
                        INSERT INTO price_evolution
                            (output_id, snapshot_at, hours_elapsed, price, return_pct,
                             max_favorable, max_adverse, outcome)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (output_id, _now_iso(), hours_elapsed, price, return_pct,
                         max_favorable, max_adverse, outcome),
                    )
                    updated += 1
                except Exception as exc:
                    print(f"[alert_tracker] price_evolution insert failed: {exc}", flush=True)
                    failed += 1

        return {"updated": updated, "failed": failed}

    def _latest_evolution_by_output(self, output_ids: list) -> dict:
        """Most recent (max_favorable, max_adverse) per output_id, in bulk."""
        result: dict = {}
        ids = [i for i in {o for o in output_ids} if i is not None]
        if not ids:
            return result
        with _lock, self._conn() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"""
                    SELECT output_id, max_favorable, max_adverse FROM (
                        SELECT output_id, max_favorable, max_adverse,
                               ROW_NUMBER() OVER (
                                   PARTITION BY output_id ORDER BY snapshot_at DESC
                               ) rn
                        FROM   price_evolution
                        WHERE  output_id IN ({placeholders})
                    ) WHERE rn = 1
                    """,
                    chunk,
                ).fetchall():
                    result[r["output_id"]] = (r["max_favorable"], r["max_adverse"])
        return result

    # ── Update prices for all open/non-expired alerts ────────────────────────
    def update_prices(self, max_alerts: int = 500) -> dict:
        """
        Fetch and store the current price for every alert that has not expired.

        Returns summary dict: {"updated": N, "failed": N, "skipped_expired": N}
        """
        if not _YF_OK:
            return {"updated": 0, "failed": 0, "skipped_expired": 0, "error": "yfinance not installed"}

        now_iso = _now_iso()
        with _lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT alert_uuid, tv_symbol, yf_ticker, fire_time, entry_price
                FROM   alerts
                WHERE  expires_at > ?
                ORDER  BY created_at DESC
                LIMIT  ?
                """,
                (now_iso, max_alerts),
            ).fetchall()

        # Many alerts share a symbol, so fetch once per ticker rather than per row.
        resolved_per_row = [
            _resolve_yf_ticker(r["tv_symbol"] or "", r["yf_ticker"] or "") for r in rows
        ]
        observations = _fetch_observations(sorted({t for t in resolved_per_row if t}))

        updated = failed = skipped = 0
        with _lock, self._conn() as conn:
            for row, resolved in zip(rows, resolved_per_row):
                observation = observations.get(resolved) if resolved else None
                if observation is None:
                    failed += 1
                    continue
                try:
                    self._write_snapshot(
                        conn,
                        row["alert_uuid"],
                        observation["price"],
                        row["fire_time"] or "",
                        row["entry_price"],
                        observed_at=(observation["observed_at"].isoformat()
                                     if observation.get("observed_at") else None),
                        available_at=observation["available_at"].isoformat(),
                        price_source=observation.get("price_source"),
                        observation_reason=observation.get("reason"),
                    )
                    updated += 1
                except Exception:
                    failed += 1

        return {"updated": updated, "failed": failed, "skipped_expired": skipped}

    # ── Compute / refresh performance metrics ────────────────────────────────
    def compute_metrics(self) -> int:
        """
        Recompute performance_metrics for all alerts that have ≥ 1 price snapshot.
        Returns count of metrics rows written/updated.
        """
        with _lock, self._conn() as conn:
            # Alerts that have at least one snapshot
            alert_ids = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT alert_uuid FROM price_snapshots"
                ).fetchall()
            ]

        written = 0
        # One connection and one transaction: this loop opened and committed a
        # fresh connection per alert, which cost ~26ms each and no network.
        with _lock, self._conn() as conn:
            for uuid in alert_ids:
                try:
                    snaps = conn.execute(
                        """
                        SELECT elapsed_hours, return_pct
                        FROM   price_snapshots
                        WHERE  alert_uuid = ?
                        ORDER  BY elapsed_hours
                        """,
                        (uuid,),
                    ).fetchall()

                    if not snaps:
                        continue

                    returns = [(s["elapsed_hours"], s["return_pct"]) for s in snaps if s["return_pct"] is not None]
                    if not returns:
                        continue

                    r_1h  = _return_at_horizon(returns, 1.0)
                    r_4h  = _return_at_horizon(returns, 4.0)
                    r_1d  = _return_at_horizon(returns, 24.0)
                    r_3d  = _return_at_horizon(returns, 72.0)
                    r_7d  = _return_at_horizon(returns, 168.0)

                    all_ret   = [r[1] for r in returns]
                    max_ret   = max(all_ret) if all_ret else None
                    max_dd    = min(all_ret) if all_ret else None
                    hit_tgt   = 1 if (max_ret is not None and max_ret >= SUCCESS_RETURN_PCT)  else 0
                    hit_stop  = 1 if (max_dd  is not None and max_dd  <= FAILURE_DRAWDOWN_PCT) else 0

                    # Status: expired if oldest snapshot > 7d
                    max_elapsed = max(r[0] for r in returns if r[0] is not None) if returns else 0
                    if hit_tgt:
                        status = "success"
                    elif hit_stop:
                        status = "failure"
                    elif max_elapsed >= 168:
                        status = "expired"
                    else:
                        status = "open"

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO performance_metrics (
                            alert_uuid, computed_at,
                            return_1h, return_4h, return_1d, return_3d, return_7d,
                            max_return, max_drawdown, hit_target, hit_stop, status
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (uuid, _now_iso(),
                         r_1h, r_4h, r_1d, r_3d, r_7d,
                         max_ret, max_dd, hit_tgt, hit_stop, status),
                    )
                    written += 1
                except Exception as exc:
                    print(f"[alert_tracker] compute_metrics error for {uuid}: {exc}")

        return written

    # ── Summary statistics ────────────────────────────────────────────────────
    def get_stats(self) -> dict:
        """
        Aggregate stats for the console summary / API response.
        Returns a dict with totals, success rates, source breakdown, layer averages.
        """
        with _lock, self._conn() as conn:
            total_q  = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            by_src_q = conn.execute(
                "SELECT source, COUNT(*) n FROM alerts GROUP BY source"
            ).fetchall()
            perf_q   = conn.execute(
                """
                SELECT status, COUNT(*) n
                FROM   performance_metrics
                GROUP  BY status
                """
            ).fetchall()
            layer_q  = conn.execute(
                """
                SELECT
                    ROUND(AVG(l1_score),2) avg_l1,
                    ROUND(AVG(l2_score),2) avg_l2,
                    ROUND(AVG(l3_score),2) avg_l3,
                    ROUND(AVG(l4_score),2) avg_l4,
                    ROUND(AVG(composite_score),2) avg_comp
                FROM alerts
                """
            ).fetchone()
            # Success rate by source
            src_perf_q = conn.execute(
                """
                SELECT a.source,
                       COUNT(*) total,
                       SUM(p.hit_target) hits,
                       ROUND(AVG(p.max_return),2) avg_max_return
                FROM   alerts a
                JOIN   performance_metrics p ON a.alert_uuid = p.alert_uuid
                GROUP  BY a.source
                """
            ).fetchall()

        by_source = {r["source"]: r["n"] for r in by_src_q}
        perf_breakdown = {r["status"]: r["n"] for r in perf_q}
        src_perf = [
            {
                "source": r["source"],
                "total":  r["total"],
                "success_rate_pct": round(r["hits"] / r["total"] * 100, 1) if r["total"] else 0,
                "avg_max_return":   r["avg_max_return"],
            }
            for r in src_perf_q
        ]

        observational_denominators = {}
        observational_status_counts = {}
        observational_invalid_records = []
        observational_coverage = {}
        try:
            from tradingview.outcome_store import evaluate_records, observation_coverage
            with _lock, self._conn() as conn:
                _, observational_summary, observational_invalid_records = evaluate_records(conn)
                observational_coverage = observation_coverage(conn)
            observational_denominators = vars(observational_summary.denominators)
            observational_status_counts = {
                key.value: value
                for key, value in observational_summary.status_counts.items()
            }
        except Exception as exc:
            print(f"[alert_tracker] observational stats unavailable: {exc}", flush=True)

        return {
            "total_alerts":     total_q,
            "by_source":        by_source,
            "performance":      perf_breakdown,
            "source_perf":      src_perf,
            "layer_averages": {
                "l1": layer_q["avg_l1"] if layer_q else None,
                "l2": layer_q["avg_l2"] if layer_q else None,
                "l3": layer_q["avg_l3"] if layer_q else None,
                "l4": layer_q["avg_l4"] if layer_q else None,
                "composite": layer_q["avg_comp"] if layer_q else None,
            },
            "observational_denominators": observational_denominators,
            "observational_status_counts": observational_status_counts,
            "observational_invalid_records": observational_invalid_records,
            "observational_coverage": observational_coverage,
        }

    def build_outcome_audit_report(self) -> str:
        """Build the observational audit without changing the legacy report."""
        from tradingview.outcome_store import report
        with _lock, self._conn() as conn:
            return report(conn)

    # ── Detailed pattern analysis ─────────────────────────────────────────────
    def analyse_patterns(self) -> dict:
        """
        Identify patterns in successful vs failed alerts.

        Returns a dict with:
          - score_buckets: success rate per composite score bucket (0-20, 20-40, etc.)
          - l1_correlation: avg max_return by L1 score decile
          - source_comparison: success rate per source
          - time_of_day: success rate by hour-of-day
          - resolution_breakdown: success rate per resolution (1H, 4H, D)
        """
        with _lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT a.composite_score, a.l1_score, a.l2_score, a.l3_score, a.l4_score,
                       a.source, a.resolution, a.fire_time,
                       p.max_return, p.hit_target, p.hit_stop, p.status
                FROM   alerts a
                JOIN   performance_metrics p ON a.alert_uuid = p.alert_uuid
                WHERE  p.status != 'open'
                """
            ).fetchall()

        if not rows:
            return {"note": "no completed alerts yet"}

        # ── Score bucket analysis ─────────────────────────────────────────────
        buckets: dict[str, list[float]] = {
            "0-20": [], "20-40": [], "40-55": [], "55-70": [], "70-100": []
        }
        for r in rows:
            comp = r["composite_score"]
            if comp is None:
                continue
            if   comp <  20: k = "0-20"
            elif comp <  40: k = "20-40"
            elif comp <  55: k = "40-55"
            elif comp <  70: k = "55-70"
            else:            k = "70-100"
            if r["max_return"] is not None:
                buckets[k].append(r["max_return"])

        score_buckets = {
            k: {
                "count":       len(v),
                "avg_return":  round(sum(v) / len(v), 2) if v else None,
                "win_rate_pct": round(
                    sum(1 for x in v if x >= SUCCESS_RETURN_PCT) / len(v) * 100, 1
                ) if v else None,
            }
            for k, v in buckets.items()
        }

        # ── Source comparison ─────────────────────────────────────────────────
        src_map: dict[str, list[int]] = {}
        for r in rows:
            src = r["source"]
            src_map.setdefault(src, [])
            src_map[src].append(r["hit_target"] or 0)

        source_comparison = {
            src: {
                "total":          len(hits),
                "win_rate_pct":   round(sum(hits) / len(hits) * 100, 1) if hits else None,
            }
            for src, hits in src_map.items()
        }

        # ── Time-of-day pattern ───────────────────────────────────────────────
        hour_map: dict[int, list[int]] = {}
        for r in rows:
            ft = r["fire_time"]
            if not ft:
                continue
            try:
                dt = datetime.fromisoformat(ft.replace("Z", "+00:00"))
                h = dt.hour
                hour_map.setdefault(h, [])
                hour_map[h].append(r["hit_target"] or 0)
            except Exception:
                pass

        time_of_day = {
            f"{h:02d}:00": {
                "count":         len(hits),
                "win_rate_pct":  round(sum(hits) / len(hits) * 100, 1),
            }
            for h, hits in sorted(hour_map.items()) if hits
        }

        # ── Resolution breakdown ──────────────────────────────────────────────
        res_map: dict[str, list[int]] = {}
        for r in rows:
            res = r["resolution"] or "unknown"
            res_map.setdefault(res, [])
            res_map[res].append(r["hit_target"] or 0)

        resolution_breakdown = {
            res: {
                "count":         len(hits),
                "win_rate_pct":  round(sum(hits) / len(hits) * 100, 1),
            }
            for res, hits in res_map.items()
        }

        # ── Layer correlation (L1..L4 vs max_return) ─────────────────────────
        def _correlation(xs: list[float], ys: list[float]) -> float | None:
            """Pearson r — returns None when std dev is 0."""
            n = len(xs)
            if n < 2:
                return None
            mx = sum(xs) / n
            my = sum(ys) / n
            num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            denom = (
                (sum((x - mx) ** 2 for x in xs) ** 0.5) *
                (sum((y - my) ** 2 for y in ys) ** 0.5)
            )
            return round(num / denom, 4) if denom else None

        corr_data: dict[str, tuple[list, list]] = {
            "l1": ([], []), "l2": ([], []),
            "l3": ([], []), "l4": ([], []),
        }
        for r in rows:
            mr = r["max_return"]
            if mr is None:
                continue
            for lyr in ("l1", "l2", "l3", "l4"):
                score = r[f"{lyr}_score"]
                if score is not None:
                    corr_data[lyr][0].append(score)
                    corr_data[lyr][1].append(mr)

        layer_correlations = {
            lyr: _correlation(xs, ys)
            for lyr, (xs, ys) in corr_data.items()
        }

        return {
            "total_completed":      len(rows),
            "score_buckets":        score_buckets,
            "source_comparison":    source_comparison,
            "time_of_day":          time_of_day,
            "resolution_breakdown": resolution_breakdown,
            "layer_correlations":   layer_correlations,
            "success_threshold_pct": SUCCESS_RETURN_PCT,
            "drawdown_threshold_pct": FAILURE_DRAWDOWN_PCT,
        }

    # ── HTML performance report ───────────────────────────────────────────────
    def build_performance_report(self) -> str:
        """
        Generate a self-contained HTML report with expandable drill-down.
        Click any Source row to expand all individual alerts from that source.
        Click any Score Bucket row to expand all alerts in that score range.
        """
        import json as _json
        sys.path.insert(0, str(_PROJECT_ROOT)) if str(_PROJECT_ROOT) not in sys.path else None
        from html_utils import JS_FILTER_PERSIST, JS_CSV_EXPORT  # C-5, C-6
        stats    = self.get_stats()
        patterns = self.analyse_patterns()
        now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # ── Helpers ───────────────────────────────────────────────────────────
        def _esc(v: Any) -> str:
            return str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # ── Fetch all individual alerts for drill-down ────────────────────────
        with _lock, self._conn() as conn:
            all_rows = conn.execute("""
                SELECT a.tv_symbol, a.source, a.composite_score, a.fire_time,
                       a.entry_price, a.signal, a.resolution,
                       pm.status, pm.max_return, pm.max_drawdown, pm.hit_target
                FROM alerts a
                LEFT JOIN performance_metrics pm ON a.alert_uuid = pm.alert_uuid
                ORDER BY a.fire_time DESC
            """).fetchall()

        # Group by source
        detail_by_source: dict = {}
        for row in all_rows:
            rd = dict(row)
            detail_by_source.setdefault(rd.get("source") or "unknown", []).append(rd)

        # Group by score bucket
        detail_by_bucket: dict = {
            "0-20": [], "20-40": [], "40-55": [], "55-70": [], "70-100": []
        }
        for row in all_rows:
            rd   = dict(row)
            comp = rd.get("composite_score")
            if comp is None:
                continue
            if   comp <  20: k = "0-20"
            elif comp <  40: k = "20-40"
            elif comp <  55: k = "40-55"
            elif comp <  70: k = "55-70"
            else:            k = "70-100"
            detail_by_bucket[k].append(rd)

        # ── Mini alert table (rendered inside each accordion panel) ───────────
        def _mini_table(alerts: list, section_id: str) -> str:
            if not alerts:
                return "<p class='empty-row'>No alerts</p>"
            closed   = [a for a in alerts if a.get("status") in ("success", "failure", "expired")]
            wins     = [a for a in closed  if a.get("status") == "success"]
            wr_str   = f"{len(wins)/len(closed)*100:.0f}%" if closed else "—"
            wr_cls   = ("c-ok-b" if (closed and len(wins)/len(closed) >= 0.5 and len(closed) >= 20)
                        else "c-warn-b" if (closed and len(closed) < 20)
                        else "c-err-b")
            ret_vals = [a.get("max_return") for a in closed if a.get("max_return") is not None]
            avg_ret  = sum(ret_vals) / len(ret_vals) if ret_vals else None
            avg_str  = f"{avg_ret:+.1f}%" if avg_ret is not None else "—"
            avg_cls  = "c-ok" if (avg_ret or 0) >= 0 else "c-err"

            rows_html = ""
            for a in alerts:
                fire     = (a.get("fire_time") or "")[:16]
                comp     = a.get("composite_score")
                comp_str = f"{comp:.0f}" if comp is not None else "—"
                comp_cls = ("c-ok-b7" if (comp or 0) >= 70 else
                            "c-warn-b" if (comp or 0) >= 50 else "c-err-b7")
                status   = a.get("status") or "open"
                ret      = a.get("max_return")
                ret_str  = f"{ret:+.1f}%" if ret is not None else "—"
                s_cls    = ("c-ok" if status == "success" else
                            "c-err" if status == "failure" else "c-muted")
                r_cls    = ("c-ok-b" if (ret or 0) >= SUCCESS_RETURN_PCT else
                            "c-err-b" if (ret or 0) <= FAILURE_DRAWDOWN_PCT else "c-muted-b")
                sym      = _esc(a.get("tv_symbol") or "")
                tv_url   = f"https://www.tradingview.com/chart/?symbol={sym}"
                rows_html += (
                    f'<tr data-sym="{sym.lower()}">'
                    f'<td><a href="{tv_url}" target="_blank" class="c-link">{sym}</a></td>'
                    f'<td class="c-muted-sm">{fire}</td>'
                    f'<td class="c-muted">{_esc(a.get("resolution") or "")}</td>'
                    f'<td class="c-text">{_esc(a.get("signal") or "")}</td>'
                    f'<td class="{comp_cls}">{comp_str}</td>'
                    f'<td class="{s_cls}">{status}</td>'
                    f'<td class="{r_cls}">{ret_str}</td>'
                    f'</tr>'
                )

            sid = section_id.replace("-", "_")
            return (
                f'<div class="mini-wrap">'
                f'<div class="mini-summary">'
                f'<span class="c-sub">Total: <b class="c-text">{len(alerts)}</b></span>'
                f'<span class="c-sub">Closed: <b class="c-text">{len(closed)}</b></span>'
                f'<span class="c-sub">Win Rate: <b class="{wr_cls}">{wr_str}</b>'
                f'<span style="color:#475569;font-size:10px"> ({len(closed)}/{len(alerts)} closed)</span></span>'
                f'<span class="c-sub">Avg Return: <b class="{avg_cls}">{avg_str}</b></span>'
                f'<input id="filter-{sid}" placeholder="🔍 filter symbol…" class="mini-filter" '
                f'oninput="filterAlerts(\'{sid}\', this.value)">'
                f'</div>'
                f'<div id="tbl-wrap-{sid}" class="mini-scroll">'
                f'<table class="mini-table" data-alert-table>'
                f'<thead><tr>'
                f'<th scope="col">Symbol</th>'
                f'<th scope="col">Fire Time</th>'
                f'<th scope="col">Res</th>'
                f'<th scope="col">Signal</th>'
                f'<th scope="col">Score</th>'
                f'<th scope="col">Status</th>'
                f'<th scope="col">Max Ret</th>'
                f'</tr></thead>'
                f'<tbody id="tbody-{sid}">{rows_html}</tbody>'
                f'</table></div>'
                f'<p class="mini-foot">{len(alerts)} alerts in group</p>'
                f'</div>'
            )

        # ── Source accordion rows + chart data ────────────────────────────────
        src_rows_html    = ""
        src_chart_labels = []
        src_chart_wr     = []
        src_chart_counts = []
        for sp in stats.get("source_perf", []):
            src     = sp["source"]
            alerts  = detail_by_source.get(src, [])
            mini    = _mini_table(alerts, f"src-{src}")
            wr      = sp.get("success_rate_pct")
            ret     = sp.get("avg_max_return")
            wr_cls  = "c-ok-b7" if (wr or 0) >= 60 else ("c-err-b7" if (wr or 0) < 40 else "c-warn-b")
            ret_cls = ("c-ok-b" if (ret or 0) >= SUCCESS_RETURN_PCT else
                       "c-err-b" if (ret or 0) <= FAILURE_DRAWDOWN_PCT else "c-warn-b")
            wr_str  = f"{wr:.1f}%" if wr is not None else "n/a"
            ret_str = f"{ret:+.2f}%" if ret is not None else "n/a"
            src_chart_labels.append(src)
            src_chart_wr.append(wr or 0)
            src_chart_counts.append(sp["total"])
            safe_src = src.replace("'", "\\'")
            src_rows_html += (
                f'<tr class="acc-row" onclick="toggleAcc(\'acc-src-{safe_src}\')" '
                f'style="cursor:pointer">'
                f'<td class="acc-cell">'
                f'<span id="arrow-acc-src-{safe_src}" class="acc-arrow">▶</span>'
                f'<b class="c-text">{_esc(src)}</b>'
                f'<span class="acc-pill">{sp["total"]}</span>'
                f'</td>'
                f'<td class="acc-cell {wr_cls}">{wr_str}</td>'
                f'<td class="acc-cell {ret_cls}">{ret_str}</td>'
                f'</tr>'
                f'<tr id="acc-src-{safe_src}" style="display:none">'
                f'<td colspan="3" class="acc-detail">{mini}</td>'
                f'</tr>'
            )

        # ── Score bucket accordion rows ────────────────────────────────────────
        bucket_rows = ""
        for bkt, bdata in (patterns.get("score_buckets") or {}).items():
            count = bdata.get("count", 0)
            if count == 0:
                continue
            alerts  = detail_by_bucket.get(bkt, [])
            bkt_id  = bkt.replace("-", "_")
            mini    = _mini_table(alerts, f"bkt-{bkt_id}")
            wr      = bdata.get("win_rate_pct")
            ret     = bdata.get("avg_return")
            wr_cls  = "c-ok-b7" if (wr or 0) >= 60 else ("c-err-b7" if (wr or 0) < 40 else "c-warn-b")
            ret_cls = ("c-ok-b" if (ret or 0) >= SUCCESS_RETURN_PCT else
                       "c-err-b" if (ret or 0) <= FAILURE_DRAWDOWN_PCT else "c-warn-b")
            wr_str  = f"{wr:.1f}%" if wr is not None else "n/a"
            ret_str = f"{ret:+.2f}%" if ret is not None else "n/a"
            bucket_rows += (
                f'<tr class="acc-row" onclick="toggleAcc(\'acc-bkt-{bkt_id}\')" '
                f'style="cursor:pointer">'
                f'<td class="acc-cell">'
                f'<span id="arrow-acc-bkt-{bkt_id}" class="acc-arrow">▶</span>'
                f'<b class="c-text">{_esc(bkt)}</b>'
                f'<span class="acc-pill">{count}</span>'
                f'</td>'
                f'<td class="acc-cell {ret_cls}">{ret_str}</td>'
                f'<td class="acc-cell {wr_cls}">{wr_str}</td>'
                f'</tr>'
                f'<tr id="acc-bkt-{bkt_id}" style="display:none">'
                f'<td colspan="3" class="acc-detail">{mini}</td>'
                f'</tr>'
            )

        # ── Layer correlation table ───────────────────────────────────────────
        lc = patterns.get("layer_correlations", {})
        corr_rows = ""
        for lyr, corr in lc.items():
            corr_str = f"{corr:.4f}" if corr is not None else "n/a"
            corr_cls = ""
            if corr is not None:
                corr_cls = "c-ok" if corr > 0.2 else ("c-err" if corr < -0.1 else "")
            corr_rows += (
                f'<tr><td class="corr-cell">{lyr.upper()}</td>'
                f'<td class="corr-cell {corr_cls}">{corr_str}</td></tr>'
            )

        # ── Resolution breakdown ──────────────────────────────────────────────
        res_rows = ""
        for res, rdata in (patterns.get("resolution_breakdown") or {}).items():
            wr    = rdata.get("win_rate_pct")
            wr_cls = "c-ok-b" if (wr or 0) >= 60 else ("c-err-b" if (wr or 0) < 40 else "c-warn-b")
            wr_s  = f"{wr:.1f}%" if wr is not None else "n/a"
            res_rows += (
                f'<tr><td class="corr-cell">{_esc(res)}</td>'
                f'<td class="corr-cell">{rdata.get("count",0)}</td>'
                f'<td class="corr-cell {wr_cls}">{wr_s}</td></tr>'
            )

        # ── Hour-of-day best/worst ────────────────────────────────────────────
        tod = patterns.get("time_of_day") or {}
        if tod:
            best_hour  = max(tod.items(), key=lambda x: x[1].get("win_rate_pct") or 0)
            worst_hour = min(tod.items(), key=lambda x: x[1].get("win_rate_pct") or 100)
            tod_note   = (
                f"Best: <b>{best_hour[0]} UTC</b> "
                f"({best_hour[1].get('win_rate_pct')}% wr, {best_hour[1].get('count')} alerts)"
                f"&nbsp;|&nbsp;Worst: <b>{worst_hour[0]} UTC</b> "
                f"({worst_hour[1].get('win_rate_pct')}% wr)"
            )
        else:
            tod_note = "Not enough data yet."

        # ── Performance overview numbers ──────────────────────────────────────
        perf            = stats.get("performance", {})
        total_alerts    = stats.get("total_alerts", 0)
        total_completed = sum(perf.get(s, 0) for s in ("success", "failure", "expired"))
        success_n       = perf.get("success", 0)
        open_n          = perf.get("open", 0)
        overall_wr      = round(success_n / total_completed * 100, 1) if total_completed else 0
        effective_wr    = round(success_n / total_alerts  * 100, 1) if total_alerts    else 0
        pct_open        = round(open_n    / total_alerts  * 100, 1) if total_alerts    else 0
        wr_caveat       = (
            f"⚠ {total_completed} closed / {total_alerts} total — {pct_open:.0f}% still open"
        ) if total_completed < 50 else f"based on {total_completed} closed"
        la              = stats.get("layer_averages", {})
        wr_col          = "#22c55e" if overall_wr   >= 55 else ("#ef4444" if overall_wr   < 40 else "#f59e0b")
        eff_col         = "#22c55e" if effective_wr >= 20 else ("#ef4444" if effective_wr <  5 else "#f59e0b")
        src_colors_js   = _json.dumps([
            "#22c55e" if w >= 60 else ("#ef4444" if w < 40 else "#f59e0b")
            for w in src_chart_wr
        ])

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Performance Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" integrity="sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" crossorigin="anonymous"></script>
<style>
  :root {{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--sub:#94a3b8;--accent:#7c3aed}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px}}
  nav{{background:#0d1424;border-bottom:1px solid var(--border);padding:.6rem 1.5rem;display:flex;gap:.8rem;flex-wrap:wrap;position:sticky;top:0;z-index:100}}
  nav a{{color:#64748b;font-size:.78rem;text-decoration:none;padding:.2rem .5rem;border-radius:4px;white-space:nowrap}}
  nav a:hover{{color:#e2e8f0;background:var(--card)}}
  .hdr{{padding:24px 28px 12px;border-bottom:1px solid var(--border)}}
  h1{{font-size:22px;font-weight:700;color:#f8fafc}}
  h2{{font-size:15px;font-weight:600;color:var(--accent);margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
  .meta{{color:var(--sub);font-size:12px;margin-top:4px}}
  .sec{{padding:0 28px 28px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:8px}}
  .kpi{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}}
  .kpi .val{{font-size:28px;font-weight:700}}
  .kpi .lbl{{color:var(--sub);font-size:12px;margin-top:3px}}
  .kpi .sub{{font-size:10px;margin-top:3px}}
  table.acc{{border-collapse:collapse;width:100%;max-width:820px;margin-top:10px}}
  table.acc th{{color:var(--sub);font-size:11px;font-weight:600;text-align:left;padding:7px 12px;border-bottom:1px solid var(--border)}}
  .acc-row:hover td{{background:rgba(255,255,255,.04)}}
  table.plain{{border-collapse:collapse;max-width:400px}}
  table.plain th{{color:var(--sub);font-size:11px;font-weight:600;text-align:left;padding:6px 10px;border-bottom:1px solid var(--border)}}
  .note{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 16px;color:var(--sub);font-size:13px;max-width:820px}}
  canvas{{max-height:200px;max-width:700px;margin:0 0 16px}}
  .hint{{font-size:11px;color:#475569;font-weight:400;margin-left:8px}}
  footer{{padding:16px 28px;color:#374151;font-size:12px;border-top:1px solid var(--border);margin-top:8px}}
  /* A-1: utility classes (replace 20k inline styles) */
  .c-muted{{color:#94a3b8}}
  .c-muted-sm{{color:#94a3b8;font-size:10px}}
  .c-muted-b{{color:#94a3b8;font-weight:600}}
  .c-text{{color:#e2e8f0}}
  .c-link{{color:#7dd3fc;font-weight:600;text-decoration:none}}
  .c-ok{{color:#22c55e}}
  .c-ok-b{{color:#22c55e;font-weight:600}}
  .c-ok-b7{{color:#22c55e;font-weight:700}}
  .c-warn-b{{color:#f59e0b;font-weight:700}}
  .c-err{{color:#ef4444}}
  .c-err-b{{color:#ef4444;font-weight:600}}
  .c-err-b7{{color:#ef4444;font-weight:700}}
  .c-sub{{color:#64748b}}
  .empty-row{{color:#94a3b8;padding:8px}}
  .hidden-row{{display:none}}
  .show-all-btn{{background:#1e293b;color:#38bdf8;border:1px solid #334155;padding:6px 16px;border-radius:6px;cursor:pointer;margin:8px 0;font-size:13px}}
  .mini-summary{{display:flex;gap:16px;flex-wrap:wrap;padding:6px 10px;background:#0f172a;border-radius:6px;margin-bottom:6px;font-size:12px;align-items:center}}
  .mini-filter{{margin-left:auto;background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:3px 8px;border-radius:4px;font-size:11px}}
  .mini-wrap{{padding:4px 0 8px}}
  .mini-scroll{{max-height:360px;overflow-y:auto;background:#070d1a;border-radius:6px}}
  .mini-table{{font-size:11px;width:100%;border-collapse:collapse}}
  .mini-table thead{{position:sticky;top:0;background:#1a2540;z-index:2}}
  .mini-table th{{padding:5px 10px;color:#64748b;font-weight:600;text-align:left}}
  .mini-foot{{color:#475569;font-size:11px;padding:4px 8px;text-align:right}}
  .acc-arrow{{display:inline-block;transition:transform .2s;margin-right:6px;color:#64748b}}
  .acc-pill{{background:#1e3a5f;color:#7dd3fc;border-radius:10px;padding:1px 8px;font-size:11px;margin-left:6px}}
  .acc-cell{{padding:10px 12px}}
  .acc-detail{{padding:4px 12px 16px;background:#0b1221;border-left:3px solid #334155}}
  .corr-cell{{padding:6px 10px;border-bottom:1px solid #1e293b}}
</style>
</head>
<body>
<header>
<nav>
  <a href="#overview">📊 Overview</a>
  <a href="#sources">📂 Sources</a>
  <a href="#scores">🎯 Score Buckets</a>
  <a href="#correlations">🔬 Correlations</a>
  <a href="#timing">⏰ Timing</a>
</nav>
<div class="hdr">
  <h1>📡 Signal Performance Analysis</h1>
  <p class="meta">Generated: {now} &nbsp;|&nbsp; DB: {_esc(str(self._db_path.name))}</p>
</div>
</header>
<main>

<div class="sec" id="overview">
  <h2>Overview</h2>
  <div class="grid">
    <div class="kpi">
      <div class="val">{total_alerts}</div>
      <div class="lbl">Total Alerts Tracked</div>
    </div>
    <div class="kpi">
      <div class="val" style="color:#22c55e">{success_n}</div>
      <div class="lbl">Winners (+{SUCCESS_RETURN_PCT:.0f}%+)</div>
    </div>
    <div class="kpi" title="Win rate on CLOSED trades only">
      <div class="val" style="color:{wr_col}">{overall_wr}%</div>
      <div class="lbl">Win Rate (closed only)</div>
      <div class="sub" style="color:#ef4444">{wr_caveat}</div>
    </div>
    <div class="kpi" title="Winners as % of ALL tracked alerts — more honest metric">
      <div class="val" style="color:{eff_col}">{effective_wr}%</div>
      <div class="lbl">Effective Rate (all alerts)</div>
      <div class="sub" style="color:#94a3b8">USE THIS FIGURE</div>
    </div>
    <div class="kpi">
      <div class="val">{open_n}</div>
      <div class="lbl">Open / Tracking</div>
    </div>
    <div class="kpi">
      <div class="val" style="color:#94a3b8">{la.get('composite', 'n/a')}</div>
      <div class="lbl">Avg Composite Score</div>
    </div>
  </div>
</div>

<div class="sec" id="sources">
  <h2>📂 Performance by Source
    <span class="hint">▶ Click any row to expand all alerts from that source</span>
  </h2>
  <canvas id="srcChart"></canvas>
  <table class="acc">
    <thead><tr>
      <th scope='col'>Source</th>
      <th scope='col'>Win Rate</th>
      <th scope='col'>Avg Max Return</th>
    </tr></thead>
    <tbody>
      {src_rows_html or '<tr><td colspan="3" style="color:#64748b;padding:10px 12px">No source performance data yet</td></tr>'}
    </tbody>
  </table>
</div>

<div class="sec" id="scores">
  <h2>🎯 Win Rate by Composite Score Bucket
    <span class="hint">▶ Click any row to see all alerts in that score range</span>
  </h2>
  <div class="note">
    <b>Interpretation:</b> Higher composite score should correlate with higher win rate.
    If low-score buckets outperform, the scoring model needs recalibration.
  </div>
  <table class="acc" style="margin-top:14px">
    <thead><tr>
      <th scope='col'>Score Range</th>
      <th scope='col'>Avg Max Return</th>
      <th scope='col'>Win Rate</th>
    </tr></thead>
    <tbody>
      {bucket_rows or '<tr><td colspan="3" style="color:#64748b;padding:10px 12px">No closed data yet</td></tr>'}
    </tbody>
  </table>
</div>

<div class="sec" id="correlations">
  <h2>🔬 Layer Correlation with Max Return (Pearson r)</h2>
  <p style="color:var(--sub);font-size:12px;margin-bottom:10px">
    r &gt; 0.2 = useful predictor &nbsp;|&nbsp; r &lt; −0.1 = detrimental &nbsp;|&nbsp; n/a = insufficient data
  </p>
  <table class="plain" style="max-width:320px">
    <thead><tr>
      <th scope='col'>Layer</th><th scope='col'>Correlation (r)</th>
    </tr></thead>
    <tbody>{corr_rows or '<tr><td colspan="2" style="color:#64748b;padding:6px 10px">No data</td></tr>'}</tbody>
  </table>

  <h2 style="margin-top:28px">⏱ Performance by Timeframe / Resolution</h2>
  <table class="plain" style="max-width:420px">
    <thead><tr>
      <th scope='col'>Resolution</th><th scope='col'>Count</th><th scope='col'>Win Rate</th>
    </tr></thead>
    <tbody>{res_rows or '<tr><td colspan="3" style="color:#64748b;padding:6px 10px">No data yet</td></tr>'}</tbody>
  </table>

  <h2 style="margin-top:28px">📊 Layer Score Averages</h2>
  <table class="plain" style="max-width:420px">
    <thead><tr>
      <th scope='col'>Layer</th><th scope='col'>Avg Score (/10)</th>
    </tr></thead>
    <tbody>
      <tr><td style="padding:6px 10px;border-bottom:1px solid #1e293b">L1 Technical</td><td style="padding:6px 10px;border-bottom:1px solid #1e293b">{la.get('l1') or 'n/a'}</td></tr>
      <tr><td style="padding:6px 10px;border-bottom:1px solid #1e293b">L2 Fundamental</td><td style="padding:6px 10px;border-bottom:1px solid #1e293b">{la.get('l2') or 'n/a'}</td></tr>
      <tr><td style="padding:6px 10px;border-bottom:1px solid #1e293b">L3 Sentiment</td><td style="padding:6px 10px;border-bottom:1px solid #1e293b">{la.get('l3') or 'n/a'}</td></tr>
      <tr><td style="padding:6px 10px;border-bottom:1px solid #1e293b">L4 Market Macro</td><td style="padding:6px 10px;border-bottom:1px solid #1e293b">{la.get('l4') or 'n/a'}</td></tr>
      <tr><td style="padding:6px 10px"><b>Composite</b></td><td style="padding:6px 10px"><b>{la.get('composite') or 'n/a'}</b></td></tr>
    </tbody>
  </table>
</div>

<div class="sec" id="timing">
  <h2>⏰ Best Trading Hours (UTC)</h2>
  <div class="note">{tod_note}</div>
</div>

<footer>
  <b>Methodology:</b> Success = price rose ≥ {SUCCESS_RETURN_PCT:.0f}% at any T+1h/4h/1d/3d/7d snapshot.
  Failure = price fell ≤ {abs(FAILURE_DRAWDOWN_PCT):.0f}%.  Expired = 7-day window elapsed without trigger.
  Correlations use Pearson r on non-open alerts with entry price data.
</footer>

<script>
(function() {{
  // Source win rate bar + alert count line chart
  const ctx = document.getElementById('srcChart');
  if (ctx && {_json.dumps(src_chart_labels)}.length > 0) {{
    new Chart(ctx.getContext('2d'), {{
      type: 'bar',
      data: {{
        labels: {_json.dumps(src_chart_labels)},
        datasets: [
          {{
            label: 'Win Rate %',
            data: {_json.dumps(src_chart_wr)},
            backgroundColor: {src_colors_js},
            borderRadius: 4,
            yAxisID: 'y',
          }},
          {{
            label: 'Alert Count',
            data: {_json.dumps(src_chart_counts)},
            type: 'line',
            borderColor: 'rgba(56,189,248,0.8)',
            backgroundColor: 'transparent',
            pointBackgroundColor: 'rgba(56,189,248,0.9)',
            pointRadius: 5, borderWidth: 2,
            yAxisID: 'y2',
          }},
        ],
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ labels: {{ color: '#94a3b8', font: {{ size: 11 }} }} }} }},
        scales: {{
          x:  {{ ticks: {{ color: '#94a3b8', font: {{ size: 11 }} }}, grid: {{ color: '#1e293b' }} }},
          y:  {{ min: 0, max: 100, ticks: {{ color: '#94a3b8', callback: v => v + '%' }}, grid: {{ color: '#1e293b' }} }},
          y2: {{ position: 'right', ticks: {{ color: '#64748b' }}, grid: {{ drawOnChartArea: false }} }},
        }},
      }},
    }});
  }}

  // Accordion toggle
  window.toggleAcc = function(id) {{
    const row   = document.getElementById(id);
    const arrow = document.getElementById('arrow-' + id);
    if (!row) return;
    const isOpen = row.style.display !== 'none';
    row.style.display = isOpen ? 'none' : 'table-row';
    if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
  }};

  // Symbol filter inside expanded panels
  window.filterAlerts = function(sectionId, query) {{
    const q     = query.toLowerCase();
    const tbody = document.getElementById('tbody-' + sectionId);
    if (!tbody) return;
    tbody.querySelectorAll('tr').forEach(tr => {{
      const sym = tr.getAttribute('data-sym') || '';
      tr.style.display = sym.includes(q) || q === '' ? '' : 'none';
    }});
  }};

  // A-9: Client-side pagination for large alert tables
  const CHUNK = 100;
  document.querySelectorAll('[data-alert-table]').forEach(tbl => {{
    const rows = Array.from(tbl.querySelectorAll('tbody tr'));
    rows.slice(CHUNK).forEach(r => r.classList.add('hidden-row'));
    if (rows.length > CHUNK) {{
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'show-all-btn';
      btn.textContent = 'Show all ' + rows.length + ' rows';
      btn.onclick = () => {{ rows.forEach(r => r.classList.remove('hidden-row')); btn.remove(); }};
      tbl.parentNode.insertAdjacentElement('afterend', btn);
    }}
  }});

  // C-5: Persist filter inputs across page refreshes (sessionStorage)
  __JS_FILTER_PERSIST__
  document.querySelectorAll('.mini-filter').forEach(function(el) {{
    persistFilter(el.id, 'alert_filter_' + el.id);
  }});

  // C-6: CSV export
  __JS_CSV_EXPORT__
}})();
</script>
</main>
</body>
</html>"""
        # C-5 / C-6: inject JS snippets (avoid f-string brace conflicts)
        html = html.replace("__JS_FILTER_PERSIST__", JS_FILTER_PERSIST)
        html = html.replace("__JS_CSV_EXPORT__", JS_CSV_EXPORT)
        return html

    # ── Options position tracking ─────────────────────────────────────────────

    def upsert_options_position(self, pos: dict) -> str:
        """
        Insert or update a single options position from broker cache snapshot.

        Keyed on op_uuid (= OCC contract symbol, e.g. NVDA260501C210000).
        Preserves first_seen_at on updates so we track how long the position
        has been open. Updates last_seen_at, pnl_usd, close_premium every call.

        Returns op_uuid.
        """
        op_uuid = str(pos.get("op_uuid") or "")
        if not op_uuid:
            return ""
        now_iso = _now_iso()
        try:
            with _lock, self._conn() as conn:
                existing = conn.execute(
                    "SELECT op_uuid, first_seen_at, open_date FROM options_positions WHERE op_uuid=?",
                    (op_uuid,)
                ).fetchone()
                first_seen = (existing["first_seen_at"] if existing else None) or now_iso
                open_date  = (existing["open_date"]    if existing else None) or now_iso

                conn.execute(
                    """
                    INSERT OR REPLACE INTO options_positions
                        (op_uuid, alert_uuid, tv_symbol, contract_symbol, strategy,
                         strike, expiry, premium_paid, quantity, open_date, status,
                         close_date, close_premium, pnl_usd, broker, notes, created_at,
                         dte_at_entry, moneyness, underlying_price_at_open,
                         pnl_pct, hold_days, outcome, first_seen_at, last_seen_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                            COALESCE((SELECT created_at FROM options_positions WHERE op_uuid=?), datetime('now')),
                            ?,?,?,?,?,?,?,?)
                    """,
                    (
                        op_uuid,
                        pos.get("alert_uuid"),
                        pos.get("tv_symbol", ""),
                        pos.get("contract_symbol"),
                        pos.get("strategy", "OPTION"),
                        pos.get("strike"),
                        pos.get("expiry"),
                        pos.get("premium_paid"),
                        pos.get("quantity", 1),
                        open_date,
                        pos.get("status", "open"),
                        pos.get("close_date"),
                        pos.get("close_premium"),
                        pos.get("pnl_usd"),
                        pos.get("broker", ""),
                        pos.get("notes", ""),
                        op_uuid,          # for COALESCE(SELECT created_at ...)
                        pos.get("dte_at_entry"),
                        pos.get("moneyness"),
                        pos.get("underlying_price_at_open"),
                        pos.get("pnl_pct"),
                        pos.get("hold_days"),
                        pos.get("outcome", "OPEN"),
                        first_seen,
                        now_iso,
                    ),
                )
        except Exception as exc:
            print(f"[alert_tracker] upsert_options_position failed: {exc}", flush=True)
        return op_uuid

    def update_options_position(self, op_uuid: str, updates: dict) -> None:
        """
        Partially update an existing options position (e.g. mark as closed/expired).

        Allowed keys in `updates`: status, close_date, close_premium, pnl_usd,
        pnl_pct, hold_days, outcome, last_seen_at, notes.
        """
        _ALLOWED = {
            "status", "close_date", "close_premium", "pnl_usd",
            "pnl_pct", "hold_days", "outcome", "last_seen_at", "notes",
        }
        sets = {k: v for k, v in updates.items() if k in _ALLOWED}
        if not sets:
            return
        cols  = ", ".join(f"{k}=?" for k in sets)
        vals  = list(sets.values()) + [op_uuid]
        try:
            with _lock, self._conn() as conn:
                conn.execute(f"UPDATE options_positions SET {cols} WHERE op_uuid=?", vals)
        except Exception as exc:
            print(f"[alert_tracker] update_options_position failed: {exc}", flush=True)

    def list_options_positions(self, status: str | None = None) -> list[dict]:
        """
        Return options positions from DB as list of dicts.

        Parameters
        ----------
        status : str | None
            If provided, filter by status ('open', 'closed', 'expired').
            If None, return all positions.
        """
        try:
            with _lock, self._conn() as conn:
                if status:
                    rows = conn.execute(
                        "SELECT * FROM options_positions WHERE status=? ORDER BY first_seen_at DESC",
                        (status,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM options_positions ORDER BY first_seen_at DESC"
                    ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            print(f"[alert_tracker] list_options_positions failed: {exc}", flush=True)
            return []

    def close_vanished_options(self, active_symbols: set, close_time: str | None = None) -> int:
        """
        Mark as closed any options_positions with status='open' whose op_uuid
        is NOT in active_symbols (i.e. they've disappeared from the broker cache).

        Returns count of positions closed.
        """
        if not active_symbols:
            return 0
        now_iso = close_time or _now_iso()
        closed_count = 0
        try:
            open_positions = self.list_options_positions(status="open")
            for pos in open_positions:
                uid = pos.get("op_uuid", "")
                if uid and uid not in active_symbols:
                    pnl = pos.get("pnl_usd") or 0
                    outcome = "WIN" if pnl > 0 else "LOSS"
                    # Check if expired (expiry date has passed)
                    expiry = pos.get("expiry", "")
                    if expiry:
                        try:
                            from datetime import date as _d
                            if _d.fromisoformat(expiry) <= _d.today():
                                outcome = "EXPIRED"
                        except Exception:
                            pass
                    # Compute hold_days from first_seen_at
                    hold_days = None
                    first_seen = pos.get("first_seen_at", "")
                    if first_seen:
                        try:
                            from datetime import datetime as _dt, timezone as _tz
                            fs = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                            hold_days = (datetime.now(timezone.utc) - fs).days
                        except Exception:
                            pass
                    self.update_options_position(uid, {
                        "status":     "closed",
                        "close_date": now_iso,
                        "outcome":    outcome,
                        "hold_days":  hold_days,
                    })
                    closed_count += 1
                    print(f"  [options] auto-closed {uid} → {outcome}", flush=True)
        except Exception as exc:
            print(f"[alert_tracker] close_vanished_options failed: {exc}", flush=True)
        return closed_count

    # ── Options recommendations ───────────────────────────────────────────────

    def save_options_recommendation(self, rec: dict) -> str:
        """
        Persist a single recommended contract from options_report.html.

        ``rec`` must have at minimum: run_date, tv_symbol, mode, option_type.
        All other fields are optional (stored as NULL if absent).

        Returns rec_uuid (generated deterministically from key fields).
        """
        import uuid as _uuid
        key   = "|".join(str(rec.get(k, "")) for k in
                         ("run_date", "tv_symbol", "mode", "strike", "expiry", "rank"))
        uid   = str(_uuid.uuid5(_uuid.NAMESPACE_OID, key))
        now   = _now_iso()

        # Compute DTE bucket
        dte   = rec.get("dte_at_rec")
        if dte is None:
            dte_bucket = None
        elif dte <= 7:
            dte_bucket = "0-7"
        elif dte <= 21:
            dte_bucket = "8-21"
        elif dte <= 45:
            dte_bucket = "22-45"
        else:
            dte_bucket = "45+"

        # Compute moneyness label
        m_pct = rec.get("moneyness_pct")
        if m_pct is None:
            m_label = None
        elif m_pct < -3:
            m_label = "ITM"
        elif m_pct > 3:
            m_label = "OTM"
        else:
            m_label = "ATM"

        try:
            with _lock, self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO options_recommendations
                        (rec_uuid, run_date, run_timestamp, tv_symbol,
                         signal_score, l1_score, l2_score, l3_score, l4_score,
                         news_sentiment_score, news_count,
                         earnings_days_away, earnings_risk,
                         pcr, vix_at_rec,
                         mode, rank, option_type, strike, expiry,
                         dte_at_rec, dte_bucket, spot_price,
                         moneyness_pct, moneyness_label,
                         premium, delta, theta, iv_pct,
                         volume, open_interest, spread_pct,
                         option_score, liq_score, iv_score, strike_score, dte_score,
                         moneyball_score, final_score,
                         market, lot_size, data_source, outcome, created_at)
                    VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        uid,
                        rec.get("run_date", now[:10]),
                        rec.get("run_timestamp", now),
                        rec.get("tv_symbol", ""),
                        rec.get("signal_score"),
                        rec.get("l1_score"),   rec.get("l2_score"),
                        rec.get("l3_score"),   rec.get("l4_score"),
                        rec.get("news_sentiment_score"),
                        rec.get("news_count"),
                        rec.get("earnings_days_away"),
                        rec.get("earnings_risk"),
                        rec.get("pcr"),
                        rec.get("vix_at_rec"),
                        rec.get("mode", "BALANCED"),
                        rec.get("rank", 1),
                        rec.get("option_type"),
                        rec.get("strike"),
                        rec.get("expiry"),
                        dte,
                        dte_bucket,
                        rec.get("spot_price"),
                        m_pct,
                        m_label,
                        rec.get("premium"),
                        rec.get("delta"),
                        rec.get("theta"),
                        rec.get("iv_pct"),
                        rec.get("volume"),
                        rec.get("open_interest"),
                        rec.get("spread_pct"),
                        rec.get("option_score"),
                        rec.get("liq_score"),  rec.get("iv_score"),
                        rec.get("strike_score"), rec.get("dte_score"),
                        rec.get("moneyball_score"),
                        rec.get("final_score"),
                        rec.get("market"),
                        rec.get("lot_size", 1),
                        rec.get("data_source"),
                        rec.get("outcome", "OPEN"),
                        now,
                    ),
                )
        except Exception as exc:
            print(f"[alert_tracker] save_options_recommendation failed: {exc}", flush=True)
        return uid

    def list_options_recommendations(
        self,
        outcome: str | None = None,
        mode: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """
        Return options recommendation rows from DB.

        Parameters
        ----------
        outcome : str | None  — filter by outcome ("OPEN","WIN","LOSS","EXPIRED")
        mode    : str | None  — filter by mode ("SAFE","BALANCED","AGGRESSIVE")
        limit   : int         — max rows (default 500)
        """
        try:
            with _lock, self._conn() as conn:
                where, params = [], []
                if outcome:
                    where.append("outcome=?"); params.append(outcome)
                if mode:
                    where.append("mode=?"); params.append(mode)
                sql = "SELECT * FROM options_recommendations"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY run_date DESC, rank ASC"
                sql += f" LIMIT {int(limit)}"
                rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            print(f"[alert_tracker] list_options_recommendations failed: {exc}", flush=True)
            return []

    def update_options_rec_outcome(
        self,
        rec_uuid: str,
        outcome: str,
        close_spot: float | None = None,
        close_date: str | None = None,
    ) -> None:
        """
        Set the outcome of a recommendation (WIN / LOSS / EXPIRED).

        Called by a background job after expiry.
        """
        now = _now_iso()
        try:
            with _lock, self._conn() as conn:
                conn.execute(
                    """UPDATE options_recommendations
                       SET outcome=?, outcome_checked_at=?,
                           close_spot=COALESCE(?,close_spot),
                           close_date=COALESCE(?,close_date)
                       WHERE rec_uuid=?""",
                    (outcome, now, close_spot, close_date or now, rec_uuid),
                )
        except Exception as exc:
            print(f"[alert_tracker] update_options_rec_outcome failed: {exc}", flush=True)

    def update_options_rec_current_premium(
        self,
        rec_uuid: str,
        current_premium: float,
    ) -> None:
        """
        Store the live/last-session mid-price for an OPEN recommendation.

        Called by run_premium_update() on every backtest run to keep
        the current_premium column up-to-date so the HTML report can
        show live P&L (curr / entry ratio).
        """
        now = _now_iso()
        try:
            with _lock, self._conn() as conn:
                conn.execute(
                    """UPDATE options_recommendations
                       SET current_premium=?, current_premium_at=?
                       WHERE rec_uuid=?""",
                    (current_premium, now, rec_uuid),
                )
        except Exception as exc:
            print(f"[alert_tracker] update_options_rec_current_premium failed: {exc}", flush=True)

    # ── Get recent alerts for API ─────────────────────────────────────────────
    def get_recent_alerts(self, n: int = 50) -> list[dict]:
        """Return the N most recent alerts as list of dicts."""
        with _lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT a.alert_uuid, a.tv_symbol, a.source, a.signal,
                       a.composite_score, a.entry_price, a.fire_time, a.created_at,
                       p.status, p.max_return, p.hit_target
                FROM   alerts a
                LEFT   JOIN performance_metrics p ON a.alert_uuid = p.alert_uuid
                ORDER  BY a.created_at DESC
                LIMIT  ?
                """,
                (n,),
            ).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Singleton accessor (thread-safe lazy init)
# ─────────────────────────────────────────────────────────────────────────────
_tracker_instance: AlertTracker | None = None
_tracker_init_lock = threading.Lock()


def get_tracker(db_path: Path | str = DB_PATH) -> AlertTracker:
    """Return the shared AlertTracker, lazily constructed on first call."""
    global _tracker_instance
    if _tracker_instance is None:
        with _tracker_init_lock:
            if _tracker_instance is None:  # double-checked locking
                _tracker_instance = AlertTracker(db_path)
    return _tracker_instance


# ─────────────────────────────────────────────────────────────────────────────
# Standalone helper: options scan results (called by options_contract_screener)
# ─────────────────────────────────────────────────────────────────────────────
def save_options_scan(
    contracts: list[dict],
    scan_id: str,
    scan_date: str,
    db_path: Path | str = DB_PATH,
) -> int:
    """
    Persist every contract from a US-300 options screener scan run.

    Parameters
    ----------
    contracts : list[dict]   — output from options_contract_screener.run_scan()
                               (each dict is a fully merged hybrid_options contract)
    scan_id   : str          — ISO datetime string e.g. "2026-05-11T14:22:00Z"
                               (same for all rows in one scan run)
    scan_date : str          — ISO date string e.g. "2026-05-11"
    db_path   : Path | str   — SQLite DB path (default: alert_tracker.db)

    Returns
    -------
    Number of rows written.
    """
    import json as _json

    tracker = AlertTracker(db_path)
    written = 0
    for rank, c in enumerate(contracts, start=1):
        try:
            with _lock, tracker._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO options_scan_results (
                        scan_id, scan_date, scan_rank,
                        tv_symbol, code, option_type,
                        strike, expiry, dte,
                        spot_price, moneyness_pct,
                        bid, ask, mid_price,
                        iv, bid_iv, ask_iv,
                        delta, gamma, theta, vega,
                        theo_price, spread_pct,
                        oi, oi_source, volume,
                        ws_volume, ws_lp,
                        ohlc_range_pct, pressure, iv_spread,
                        tqs_v2, tqs_v2_badge, ws_source,
                        screener_score, signal_score,
                        l1_score, l2_score, l3_score, l4_score,
                        outcome, result_json
                    ) VALUES (
                        ?,?,?,  ?,?,?,  ?,?,?,  ?,?,
                        ?,?,?,  ?,?,?,  ?,?,?,?,  ?,?,
                        ?,?,?,  ?,?,  ?,?,?,
                        ?,?,?,  ?,?,  ?,?,?,?,  ?,?
                    )
                    """,
                    (
                        scan_id, scan_date, rank,
                        c.get("tv_symbol", ""), c.get("code", ""), c.get("option_type", "CALL"),
                        c.get("strike"), c.get("expiry"), c.get("dte"),
                        c.get("spot_price"), c.get("moneyness_pct"),
                        c.get("bid"), c.get("ask"), c.get("mid_price"),
                        c.get("iv"), c.get("bid_iv"), c.get("ask_iv"),
                        c.get("delta"), c.get("gamma"), c.get("theta"), c.get("vega"),
                        c.get("theo_price"), c.get("spread_pct"),
                        c.get("oi"), c.get("oi_source"), c.get("volume"),
                        c.get("ws_volume"), c.get("ws_lp"),
                        c.get("ohlc_range_pct"), c.get("pressure"), c.get("iv_spread"),
                        c.get("tqs_v2"), c.get("tqs_v2_badge"), c.get("ws_source"),
                        c.get("screener_score"), c.get("signal_score"),
                        c.get("l1_score"), c.get("l2_score"), c.get("l3_score"), c.get("l4_score"),
                        "OPEN",
                        _json.dumps(c, default=str),
                    ),
                )
            written += 1
        except Exception as exc:
            print(f"[alert_tracker] save_options_scan row {rank} failed: {exc}", flush=True)
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Standalone CLI
# ─────────────────────────────────────────────────────────────────────────────
def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Alert Tracker — performance analysis tool")
    parser.add_argument("--update",  action="store_true",
                        help="Fetch current prices for all open alerts")
    parser.add_argument("--compute", action="store_true",
                        help="Recompute performance metrics from stored snapshots")
    parser.add_argument("--report",  action="store_true",
                        help="Build alert_performance_report.html")
    parser.add_argument("--audit-report", action="store_true",
                        help="Build the observational outcome audit report")
    parser.add_argument("--audit-out", default=str(_PROJECT_ROOT / "reports" / "alert_outcome_audit_report.html"),
                        help="Output path for the observational audit HTML")
    parser.add_argument("--stats",   action="store_true",
                        help="Print stats summary to console")
    parser.add_argument("--all",     action="store_true",
                        help="Run --update, --compute, --report in sequence")
    parser.add_argument("--db",      default=str(DB_PATH),
                        help="Path to SQLite DB (default: alert_tracker.db)")
    args = parser.parse_args()

    tracker = AlertTracker(args.db)

    if args.update or args.all:
        print("Updating prices for open alerts ...")
        result = tracker.update_prices()
        print(f"  updated={result['updated']}  failed={result['failed']}")
        print("Updating signal engine output price evolution ...")
        evo_result = tracker.update_price_evolution()
        upd = evo_result.get("updated", 0)
        fail = evo_result.get("failed", 0)
        err = evo_result.get("error", "")
        if err:
            print(f"  price_evolution skipped: {err}")
        else:
            print(f"  price_evolution updated={upd}  failed={fail}")

    if args.compute or args.all:
        n = tracker.compute_metrics()
        print(f"Computed metrics for {n} alert(s).")

    if args.report or args.all:
        html = tracker.build_performance_report()
        out  = _THIS_DIR.parent / "reports" / "alert_performance_report.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"Report saved → {out}")
        # ── Persist snapshot to report analytics DB ───────────────────────────
        try:
            _rdb_path = str(_THIS_DIR.parent / "reports")
            if _rdb_path not in sys.path:
                sys.path.insert(0, _rdb_path)
            from report_db import save_alert_tracker_snapshot, archive_report as _arc
            save_alert_tracker_snapshot(tracker.get_stats())
            _arc(out)
        except Exception as _rdb_exc:
            print(f"  [report_db] snapshot not saved: {_rdb_exc}")

    if args.audit_report:
        audit_out = Path(args.audit_out)
        audit_out.parent.mkdir(parents=True, exist_ok=True)
        audit_out.write_text(tracker.build_outcome_audit_report(), encoding="utf-8")
        audit_json = audit_out.with_suffix(".json")
        from tradingview.outcome_store import report_json
        with tracker._conn() as conn:
            payload = report_json(conn)
        payload["metadata"]["db"] = str(args.db)
        audit_json.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        print(f"Outcome audit report saved -> {audit_out}")

    if args.stats or args.all:
        stats = tracker.get_stats()
        perf  = stats.get("performance", {})
        total = stats.get("total_alerts", 0)
        open_ = perf.get("open", 0)
        succ    = perf.get("success", 0)
        fail    = perf.get("failure", 0)
        expired = perf.get("expired", 0)
        # Win rate on closed trades only (include expired as non-winners, matching HTML report)
        total_closed = succ + fail + expired
        win_rate_closed = round(succ / max(1, total_closed) * 100, 1)
        # Effective win rate: closed wins as % of ALL tracked alerts (honest view)
        win_rate_all = round(succ / max(1, total) * 100, 1)
        pct_open = round(open_ / max(1, total) * 100, 1)

        print("\n" + "=" * 52)
        print("  ALERT TRACKER — STATS SUMMARY")
        print("=" * 52)
        print(f"  Total alerts      : {total}")
        print(f"  Open (pending)    : {open_}  ({pct_open}% of total — unresolved)")
        print(f"  Closed — success  : {succ}")
        print(f"  Closed — failed   : {fail}")

        # Canonical win rate (return_7d >= 3.0%) — single source of truth
        try:
            _db_parent = str(_THIS_DIR.parent)
            if _db_parent not in sys.path:
                sys.path.insert(0, _db_parent)
            from database.queries import canonical_win_rate as _cwr
            with tracker._conn() as _cwrc:
                _cwr_result = _cwr(_cwrc)
            if _cwr_result["total_closed"] > 0:
                print(f"  Canonical win rate: {_cwr_result['win_rate_pct']}%  "
                      f"({_cwr_result['wins']}/{_cwr_result['total_closed']} "
                      f"closed, return_7d >= 3.0%)")
        except Exception:
            pass  # canonical win rate is non-fatal
        # Reality-check: only show headline win rate if enough closed trades exist
        if succ + fail == 0:
            print(f"  Win rate          : N/A  (no closed trades yet)")
        elif succ + fail < 20:
            print(f"  ⚠ WARNING: {open_} alerts still open — many may be unresolved losers.")
            print(f"     Run --update to refresh prices before trusting these stats.")
        else:
            if open_ > 0:
                print(f"  ⚠ Note: {open_} alerts still open — run --update to refresh.")

        # Source breakdown
        by_src = stats.get("by_source", {})
        if by_src:
            print("\n  By source:")
            for src, cnt in sorted(by_src.items(), key=lambda x: -x[1]):
                print(f"    {src:<22s} {cnt:>5d}")

        # Source performance table
        src_perf = stats.get("source_perf", [])
        if src_perf:
            print("\n  Performance by source:")
            print(f"  {'Source':<22s} {'Total':>6s} {'Win%':>7s} {'AvgMax':>8s}")
            print("  " + "-" * 46)
            for sp in sorted(src_perf, key=lambda x: -(x.get("success_rate_pct") or 0)):
                wr  = sp.get("success_rate_pct") or 0
                amr = sp.get("avg_max_return")
                amr_str = f"{amr:+.1f}%" if amr is not None else "  N/A"
                print(f"  {sp['source']:<22s} {sp['total']:>6d} {wr:>6.1f}% {amr_str:>8s}")

        # Layer score averages
        la = stats.get("layer_averages", {})
        if any(v is not None for v in la.values()):
            print("\n  Layer score averages:")
            for k, label in [("l1","L1 Technical"), ("l2","L2 Fundamental"),
                              ("l3","L3 Sentiment"), ("l4","L4 Market Macro"),
                              ("composite","Composite")]:
                v = la.get(k)
                bar = ("█" * int((v or 0) * 5))[:50] if v else ""
                print(f"    {label:<18s} {(f'{v:.2f}' if v is not None else 'N/A'):>6s}  {bar}")

        print("=" * 52 + "\n")

        # ── Persist stats snapshot to report analytics DB (same as --report) ──
        try:
            _rdb_path = str(_THIS_DIR.parent / "reports")
            if _rdb_path not in sys.path:
                sys.path.insert(0, _rdb_path)
            from report_db import save_alert_tracker_snapshot  # noqa: F811
            save_alert_tracker_snapshot(stats)
        except Exception as _rdb_exc:
            pass  # snapshot persistence is non-fatal

    if not any([args.update, args.compute, args.report, args.stats, args.all]):
        parser.print_help()


# ── Signal Moneyball: population-relative percentile ranking ──────────────────

def _mb_signal_percentile_rank(value: float, population: list) -> float:
    """Return percentile rank (0-100) of value within population list."""
    if not population or len(population) < 2:
        return 50.0
    rank = sum(1.0 for v in population if v < value) + 0.5 * sum(1.0 for v in population if v == value)
    return round(100.0 * rank / len(population), 1)


def compute_signal_moneyball(composite_score: float, lookback_days: int = 30) -> tuple:
    """
    Compute moneyball percentile rank of composite_score vs last `lookback_days` days of alerts.

    Returns (moneyball_score: float 0-100, final_score: float).
    - moneyball_score: where this alert ranks in the recent population (0=bottom, 100=top)
    - final_score: 0.60 × composite_score + 0.40 × moneyball_score (blended)
    """
    import os
    db = str(DB_PATH)
    if not os.path.exists(db):
        return 50.0, round(composite_score, 1)
    try:
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT composite_score FROM alerts "
                "WHERE processed_at > datetime('now', ?) "
                "AND composite_score IS NOT NULL",
                (f"-{lookback_days} days",)
            ).fetchall()
        population = [r[0] for r in rows if r[0] is not None]
        mb = _mb_signal_percentile_rank(composite_score, population)
        final = round(0.60 * composite_score + 0.40 * mb, 1)
        return mb, final
    except Exception:
        return 50.0, round(composite_score, 1)

if __name__ == "__main__":
    _cli()
