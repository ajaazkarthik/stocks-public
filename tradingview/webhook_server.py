#!/usr/bin/env python3
"""
Webhook Server — TradingView → Signal Stack Auto-Filter → Moomoo Auto-Buy
==========================================================================

HOW IT WORKS (quick mental model)
──────────────────────────────────
1. This script runs a tiny HTTP server on your machine (port 8765 by default).
2. TradingView fires an alert → it sends a POST request to your webhook URL.
3. The server parses the payload, appends the fire to fires_raw.json, then
   runs the full 4-layer Signal Stack analysis on that symbol in the background.
4. If composite score ≥ ACTIONABLE_THRESHOLD (default 55):
     • Appends to actionable_log.json  (persistent history)
     • Rebuilds actionable_report.html (open this in your browser — just refresh)
     • Prints a 🚨 ACTIONABLE banner to the console

DO I NEED TO RUN THIS ALL DAY?
───────────────────────────────
Yes — but only when you want live alerts processed. Options:

  1. Local background process (simplest):
       start a terminal; python webhook_server.py; leave it running
       Keep your laptop on while markets are open.

  2. Windows Task Scheduler:
       Schedule this script to start at login / market open, stop after close.

  3. Cloud VM (24/7, ~$5/month):
       Rent a tiny VPS (e.g. AWS EC2 t3.micro free tier, DigitalOcean $6/mo,
       Render free tier).  Run this script there — no laptop dependency.

HOW TO EXPOSE TO TRADINGVIEW (must be a public HTTPS URL)
──────────────────────────────────────────────────────────
TradingView's servers are in the cloud — they can't reach "localhost:8765".
You need to tunnel your local port to a public URL:

  Option A — ngrok (easiest, free):
    1. Download: https://ngrok.com/download  (or: choco install ngrok / winget install ngrok)
    2. ngrok http 8765
    3. Copy the HTTPS URL, e.g. https://a1b2-203-0-113-42.ngrok-free.app
    4. In TradingView alert → Notifications → Webhook URL:
         https://a1b2-203-0-113-42.ngrok-free.app/webhook
    Note: free ngrok URL changes on every restart. Use `ngrok authtoken <token>`
          and add a static domain in the ngrok dashboard (free tier: 1 static domain).

  Option B — localtunnel (no account needed):
    1. npx localtunnel --port 8765
    2. Use the printed URL + /webhook

  Option C — Cloudflare Tunnel (free, persistent URL, most robust):
    1. https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
    2. cloudflared tunnel --url http://localhost:8765

TRADINGVIEW ALERT MESSAGE TEMPLATE
────────────────────────────────────
In TradingView → Create Alert → Notifications → Message, paste:

  {"s":"{{exchange}}:{{ticker}}","r":"{{interval}}","msg":"{{strategy.order.comment}}"}

If your strategy uses alert() calls directly (not strategy.order.comment), use:
  {"s":"{{exchange}}:{{ticker}}","r":"{{interval}}","msg":"{{strategy.order.comment}}"}

Or if the strategy message already contains all the data (WinRate:, NetProfit%:, etc.)
just use — the server will try JSON first then fall back to raw text:
  {{exchange}}:{{ticker}} | {{interval}} | WinRate:70%,NetProfit%:35,...

TESTING LOCALLY (no TradingView needed)
────────────────────────────────────────
  # Terminal 1 — start server:
  cd tradingview && python webhook_server.py

  # Terminal 2 — fire a test webhook:
  python test_webhook.py

USAGE
─────
  python webhook_server.py              # default port 8765, threshold 55
  python webhook_server.py --port 9000 --threshold 60
"""

import json
import math
import os
import sys
import re
import time
import threading
import concurrent.futures
import traceback
import argparse
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Make sure signal_stack is importable from the same directory ───────────────
BASE_DIR     = Path(__file__).parent
REPORTS_DIR  = BASE_DIR.parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent))

# ── Centralised path setup ────────────────────────────────────────────────────
try:
    from path_config import ensure_paths, TV, INDIA, TV_SCREENERS, TV_BACKTEST
    ensure_paths(TV, INDIA, TV_SCREENERS, TV_BACKTEST)
except ImportError:
    pass  # path_config not available — sys.path.insert above is fallback

# ── Structured logging ─────────────────────────────────────────────────────────
try:
    from log_config import get_logger as _get_logger
    _log = _get_logger(__name__)
except Exception:
    import logging as _logging
    _log = _logging.getLogger(__name__)

try:
    import signal_stack as ss
    import yfinance as yf
except ImportError as e:
    sys.exit(f"ERROR: {e}\nRun:  pip install yfinance vaderSentiment")

# ── Holdings sync (non-blocking, optional) ─────────────────────────────────────
try:
    from sync_holdings_watchlist import on_buy_executed as _holdings_on_buy
    _HOLDINGS_SYNC = True
except ImportError:
    _HOLDINGS_SYNC = False
    def _holdings_on_buy(*_, **__): pass

# ── TV Screener fundamentals (replaces yfinance .info — better ASX/ETF coverage) ──
try:
    from tv_screener_fundamentals import (
        fetch_tv_fundamentals     as _tv_fund_fetch,
        tv_fund_to_compat         as _tv_fund_compat,
        fetch_yield_curve,
        fetch_vix_level,
        fetch_sector_etf_momentum,
    )
    _TV_FUND_AVAILABLE = True
except ImportError:
    _TV_FUND_AVAILABLE = False
    def fetch_yield_curve(): return {}
    def fetch_vix_level(): return None
    def fetch_sector_etf_momentum(): return {}
    def _tv_fund_fetch(syms, **kw): return {}
    def _tv_fund_compat(d): return d

# ── Moomoo order placement ─────────────────────────────────────────────────────
try:
    os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    from moomoo import (
        OpenSecTradeContext, TrdMarket, TrdEnv, TrdSide,
        OrderType, TimeInForce, SecurityFirm, Currency,
        RET_OK as MM_RET_OK,
    )
    _MOOMOO_TRADE_OK = True
except ImportError:
    _MOOMOO_TRADE_OK = False

# ── Backtest position sizer (optional — used to scale order quantity) ───────────
try:
    from backtest.backtest_sizer import get_position_size as _wh_get_position_size
    _SIZER_AVAILABLE = True
except ImportError:
    _SIZER_AVAILABLE = False
    def _wh_get_position_size(*a, **kw): return {}

# ── L6 IBKR enricher (optional) ───────────────────────────────────────────────
try:
    from ibkr_enricher import compute_l6_score as _wh_compute_l6_score
    _L6_AVAILABLE = True
except ImportError:
    _L6_AVAILABLE = False
    def _wh_compute_l6_score(*a, **kw): return {"l6_score": None}

# ── L7 External enricher (YouTube + Smallcase + Twitter, optional) ─────────────
try:
    from external_signal_enricher import compute_l7_score as _wh_compute_l7_score
    _L7_AVAILABLE = True
except ImportError:
    _L7_AVAILABLE = False
    def _wh_compute_l7_score(*a, **kw): return {"l7_score": None}

# ── Platform watchdog (broker session health monitoring) ──────────────────────
try:
    import platform_watchdog as _watchdog
    _WATCHDOG_OK = True
except ImportError:
    _WATCHDOG_OK = False
    class _watchdog:  # type: ignore[no-redef]
        @staticmethod
        def start_watchdog(*a, **kw): pass
        @staticmethod
        def is_healthy(*a, **kw): return True  # fail-open if not installed
        @staticmethod
        def get_health_report(): return {}

# ── Order event ledger (durable signal state machine + dedup) ─────────────────
try:
    from order_ledger import get_ledger as _get_order_ledger
    _LEDGER_OK = True
except ImportError:
    _LEDGER_OK = False
    def _get_order_ledger(): return None  # type: ignore[return-value]

# ── Input Sources (screener + manual ticker ingestion + news) ─────────────────
try:
    from input_sources import (
        manual_to_fires      as _manual_to_fires,
        tv_screener_to_fires as _screener_to_fires,
        news_to_fires        as _news_to_fires,
        news_scan_meta       as _news_scan_meta,
        SOURCE_WEBHOOK, SOURCE_DOWNLOAD, SOURCE_SCREENER, SOURCE_MANUAL, SOURCE_NEWS,
    )
    _INPUT_SOURCES_OK = True
except ImportError:
    _INPUT_SOURCES_OK = False
    SOURCE_WEBHOOK = "webhook"; SOURCE_DOWNLOAD = "download"
    SOURCE_SCREENER = "screener"; SOURCE_MANUAL = "manual"; SOURCE_NEWS = "news"

    def _manual_to_fires(*a, **kw):  # type: ignore[misc]
        return [], {"error": "input_sources not installed"}

    def _screener_to_fires(*a, **kw):  # type: ignore[misc]
        return [], {}

    def _news_to_fires(*a, **kw):  # type: ignore[misc]
        return [], []

    def _news_scan_meta(*a, **kw):  # type: ignore[misc]
        return {"error": "input_sources not installed"}

# ── Alert Tracker (persistent storage + performance analysis) ─────────────────
try:
    from alert_tracker import get_tracker as _get_tracker
    _TRACKER_OK = True
except ImportError:
    _TRACKER_OK = False

    def _get_tracker():  # type: ignore[misc]
        return None

# ── Config (overridden by CLI args in main()) ──────────────────────────────────
PORT                 = 8765
ACTIONABLE_THRESHOLD = 55        # composite ≥ this → ACTIONABLE
MAX_ACTIONABLE       = 100       # keep last N actionable entries in memory + log
FIRES_FILE           = BASE_DIR / "fires_raw.json"
ACTIONABLE_LOG       = REPORTS_DIR / "actionable_log.json"
ACTIONABLE_REPORT    = REPORTS_DIR / "signal_report_actionable_webhook.html"
STACK_REPORT         = REPORTS_DIR / "signal_report_full_webhook.html"
PENDING_FIRES_FILE   = BASE_DIR / "pending_fires.json"   # durable pending queue

# ── Execution quality gates ──────────────────────────────────────────────────
MAX_ALERT_AGE_SECS   = 900    # reject orders on alerts older than 15 minutes
MAX_PRICE_DRIFT_PCT  = 1.5   # reject order if price drifted > 1.5% from alert price

# ── Bounded worker pool (CF7 fix) ───────────────────────────────────────────
WORKER_POOL_SIZE     = 10     # max concurrent analysis threads
_executor_pool: "concurrent.futures.ThreadPoolExecutor | None" = None

# ── fires_raw.json growth cap ──────────────────────────────────────────────────────
MAX_FIRES            = 2000   # keep only the most recent N entries in fires_raw.json

# ── IP-based rate limiting for /webhook ──────────────────────────────────────
# Only /webhook (TradingView external source) is rate-limited; 127.0.0.1 is
# always whitelisted so manual triggers from localhost are never rejected.
_MAX_REQ_PER_WINDOW  = 60     # max POST requests per IP per _RATE_WINDOW_SECS
_RATE_WINDOW_SECS    = 60.0   # rolling window width in seconds
_request_counts: dict = {}    # ip → list[float] of monotonic timestamps in window

# ── Moomoo order config ───────────────────────────────────────────────────────
MOOMOO_HOST       = "127.0.0.1"
MOOMOO_PORT_OPEND = 11111
MOOMOO_ACC_ID     = 284008279318020118
MOOMOO_FIRM       = (SecurityFirm.FUTUAU  # type: ignore[name-defined]
                     if _MOOMOO_TRADE_OK else None)
MOOMOO_TRD_ENV    = (TrdEnv.REAL          # type: ignore[name-defined]
                     if _MOOMOO_TRADE_OK else None)

# TV exchange → Moomoo market prefix (for order placement, not just enrichment)
_TV_TO_MM_ORDER = {
    "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "BATS": "US",
    "ASX":  "AU", "ASX_DLY": "AU",
    "HKEX": "HK", "HKEX_DLY": "HK",
    "SGX":  "SG",
    "SH":   "SH",  "SZ": "SZ",
}

def tv_to_mm_order_code(pro_symbol: str) -> str | None:
    """Convert TV pro_symbol to Moomoo order code, e.g. NYSE:RCL → US.RCL"""
    if ":" not in pro_symbol:
        return None
    exch, ticker = pro_symbol.split(":", 1)
    market = _TV_TO_MM_ORDER.get(exch.upper())
    if not market:
        return None
    if market == "HK" and ticker.isdigit():
        ticker = ticker.zfill(5)
    return f"{market}.{ticker}"

# ── Shared state protected by a lock ──────────────────────────────────────────
_lock             = threading.Lock()
_actionable       : list = []     # in-memory store (survives server restarts via log)
_all_results      : list = []     # ALL results ever (no filter) — for stack report
_processing       : set  = set()  # symbols currently being analysed (debounce)
_trade_pwd        : str  = ""     # set from --trade-pwd CLI arg or MOOMOO_TRADE_PWD env
_order_size_usd   : int  = 0      # set from --order-size CLI arg; 0 = disabled
_india_order_size_inr : int = 0   # set from --india-order-size CLI arg; 0 = disabled
_use_sizer        : bool = True   # set False by --no-sizer CLI arg

# ─────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_state() -> None:
    global _actionable
    if ACTIONABLE_LOG.exists():
        try:
            _actionable = json.loads(ACTIONABLE_LOG.read_text(encoding="utf-8"))
            if not isinstance(_actionable, list):
                _actionable = []
            print(f"  Loaded {len(_actionable)} previous actionable signals from log.")
        except Exception:
            _actionable = []


def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp file then rename — crash-safe on Windows & POSIX."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)   # atomic on same filesystem


def save_state() -> None:
    trimmed = _actionable[-MAX_ACTIONABLE:]
    _atomic_write(
        ACTIONABLE_LOG,
        json.dumps(trimmed, indent=2, default=str),
    )


def append_fire_to_disk(fire: dict) -> None:
    """Prepend the fire to fires_raw.json so signal_stack picks it up as most recent.

    Trims the list to MAX_FIRES entries (default 2000) so the file cannot grow
    without bound over a long-running server session.
    """
    with _lock:
        data = {"all": []}
        if FIRES_FILE.exists():
            try:
                data = json.loads(FIRES_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        data.setdefault("all", [])
        data["all"].insert(0, fire)
        # Keep only the most recent MAX_FIRES entries to bound file size
        if len(data["all"]) > MAX_FIRES:
            data["all"] = data["all"][:MAX_FIRES]
        _atomic_write(FIRES_FILE, json.dumps(data, indent=2, default=str))


# ─────────────────────────────────────────────────────────────────────────────
# Durable pending queue (CF2 fix)
# Each fire is written here before threading; removed when processing finishes.
# On startup, pending entries are replayed so no accepted alert is silently lost.
# ─────────────────────────────────────────────────────────────────────────────
def _add_pending(fire: dict) -> None:
    """Write a fire to the persistent pending queue before processing starts."""
    fire_id = str(fire.get("fire_id") or fire.get("fire_time", "unknown"))
    with _lock:
        data: dict = {}
        if PENDING_FIRES_FILE.exists():
            try:
                data = json.loads(PENDING_FIRES_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[fire_id] = fire
        _atomic_write(PENDING_FIRES_FILE, json.dumps(data, default=str))


def _remove_pending(fire_id: str) -> None:
    """Remove a fire from the pending queue when processing completes or errors."""
    with _lock:
        if not PENDING_FIRES_FILE.exists():
            return
        try:
            data = json.loads(PENDING_FIRES_FILE.read_text(encoding="utf-8"))
            data.pop(str(fire_id), None)
            _atomic_write(PENDING_FIRES_FILE, json.dumps(data, default=str))
        except Exception:
            pass


def _get_pending_fires() -> list:
    """Return all fires currently in the persistent pending queue."""
    if not PENDING_FIRES_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_FIRES_FILE.read_text(encoding="utf-8"))
        return list(data.values()) if isinstance(data, dict) else []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Payload parsing
# ─────────────────────────────────────────────────────────────────────────────
_SAFE_SYMBOL = re.compile(r'^[A-Z0-9:._\-]{1,30}$', re.IGNORECASE)


def _resolve_exchange_for_bare_ticker(bare_tick: str) -> str:
    """
    Resolve the exchange prefix for a bare ticker (no EXCHANGE: prefix).

    TradingView strategy alerts do NOT substitute {{exchange}} / {{ticker}} in
    the webhook URL — only the message body gets Pine runtime values.  So when
    the URL arrives with literal '{{exchange}}:{{ticker}}' we must look the
    exchange up ourselves.

    Strategy (fastest / least-auth first):
      1. fires_raw.json  — tickers we have already seen with their pro_symbol
      2. TV public symbol-search API — no auth required, 5 s timeout
    Returns the exchange string (e.g. 'NSE') or '' if nothing found.
    """
    bare = bare_tick.upper()
    # 1. fires_raw.json lookup (offline, instant)
    try:
        if FIRES_FILE.exists():
            data = json.loads(FIRES_FILE.read_text(encoding="utf-8"))
            for fire in data.get("all", []):
                ps = fire.get("pro_symbol", "")
                if ":" in ps:
                    exch, tick = ps.split(":", 1)
                    if tick.upper() == bare:
                        print(f"  [exchange-resolve] {bare_tick} → {exch} (from fires_raw.json)")
                        return exch.upper()
    except Exception:
        pass
    # 2. TV public symbol-search API (no auth required)
    try:
        import urllib.parse as _up
        params = _up.urlencode({
            "text": bare_tick,
            "hl": "false",
            "exchange": "",
            "lang": "en",
            "search_type": "undefined",
            "domain": "production",
        })
        url = f"https://symbol-search.tradingview.com/symbol_search/v3/?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://www.tradingview.com/",
            "Origin":  "https://www.tradingview.com",
            "Accept":  "application/json, text/javascript, */*; q=0.01",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            hits = json.loads(resp.read().decode("utf-8"))
        for item in (hits if isinstance(hits, list) else hits.get("symbols", [])):
            if item.get("symbol", "").upper() == bare:
                exch = item.get("exchange", "").upper()
                if exch:
                    print(f"  [exchange-resolve] {bare_tick} → {exch} (from TV search API)")
                    return exch
    except Exception as exc:
        print(f"  [exchange-resolve] TV search failed for {bare_tick}: {exc}")
    print(f"  [exchange-resolve] could not resolve exchange for bare ticker '{bare_tick}'")
    return ""


def parse_payload(body: bytes, query: dict) -> dict:
    """
    Turn a raw webhook POST body into a fire-record dict.

    Accepts:
      • JSON body: {"s":"NYSE:RCL","r":"60","msg":"BUY ..."}
      • Plain text body (Pine alert() output) with symbol via URL query params:
          ?s={{exchange}}:{{ticker}}&r={{interval}}
        or
          ?exchange={{exchange}}&ticker={{ticker}}&r={{interval}}

    IMPORTANT — set the TradingView webhook URL to:
        https://<your-ngrok-domain>/webhook?s={{exchange}}:{{ticker}}&r={{interval}}
    This injects the EXCHANGE:TICKER into the URL even when the Pine alert()
    only sends the bare syminfo.ticker in the body.
    """
    text = body.decode("utf-8", errors="replace").strip()
    parsed: dict = {}

    # — Try JSON —
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"msg": text}

    # — Symbol ——
    # Precedence:
    #   1. JSON body field "s"  (e.g. {"s":"NSE:TDPOWERSYS"})
    #   2. JSON body fields "exchange" + "symbol"/"ticker"
    #   3. URL query param "s"  (TV template: ?s={{exchange}}:{{ticker}})   ← PRIMARY for PIN alerts
    #   4. URL query params "exchange" + "ticker"                            ← fallback URL template
    #   5. URL query param "symbol"
    #   6. Regex scan of body text for EXCHANGE:TICKER pattern
    #   7. → UNKNOWN (raises ValueError)
    symbol = parsed.get("s") or ""
    if not symbol:
        exch = parsed.get("exchange", "")
        tick = parsed.get("ticker") or parsed.get("symbol", "")
        if exch and tick:
            symbol = f"{exch}:{tick}"
        else:
            symbol = tick or ""
    # ── URL query params ──────────────────────────────────────────────────────
    # IMPORTANT: TradingView strategy alerts do NOT substitute {{exchange}} /
    # {{ticker}} template variables in the webhook URL — only the message body
    # gets Pine runtime values.  When we see literal '{{...}}' in the URL params
    # we silently discard them and fall through to body-text parsing instead of
    # returning 400.
    if not symbol:
        _url_s = query.get("s", [""])[0]   # ?s={{exchange}}:{{ticker}}
        symbol = "" if ('{' in _url_s or '}' in _url_s) else _url_s
    if not symbol:
        q_exch = query.get("exchange", [""])[0]
        q_tick = query.get("ticker", [""])[0] or query.get("symbol", [""])[0]
        # Discard if still containing unsubstituted template literals
        if '{' in q_exch or '{' in q_tick:
            q_exch = q_tick = ""
        if q_exch and q_tick:
            symbol = f"{q_exch}:{q_tick}"
        else:
            symbol = q_tick or q_exch or query.get("symbol", [""])[0]
    if not symbol:
        # Pine-format fallback: "BUY TICKER price:..." — \s+ handles the \n
        # that Pine inserts between strategy.order.action and syminfo.ticker.
        m_pine = re.search(r'\b(?:BUY|SELL)\s+([A-Z][A-Z0-9]{1,19})\s+price:', text, re.IGNORECASE)
        if m_pine:
            bare_tick = m_pine.group(1).upper()
            # Try to get exchange from URL; if it's a template literal, look it up.
            q_exch = query.get("exchange", [""])[0].upper()
            if not q_exch or '{' in q_exch:
                q_exch = _resolve_exchange_for_bare_ticker(bare_tick)
            symbol = f"{q_exch}:{bare_tick}" if q_exch else bare_tick
            print(f"  [parse] Pine body fallback → {symbol}")
        else:
            # Last resort: grab first word that looks like EXCHANGE:TICKER
            m = re.search(r'\b([A-Z]+:[A-Z0-9]+)\b', text, re.IGNORECASE)
            symbol = m.group(1) if m else "UNKNOWN"

    # Sanitise symbol (prevent path traversal / injection)
    symbol = symbol.upper().strip()
    # At this point any remaining {{}} means we exhausted ALL fallbacks — 400
    if '{' in symbol or '}' in symbol:
        raise ValueError(
            "Could not resolve symbol: TV template variables were not substituted "
            "and body parsing also failed.  Add the ticker directly to your Pine "
            "script alert message body, e.g.: "
            r'alert(\"BUY \" + syminfo.exchange + \":\" + syminfo.ticker + \" price:...\")'
        )
    if not _SAFE_SYMBOL.match(symbol):
        raise ValueError(f"Invalid symbol: {symbol!r}")
    if symbol == "UNKNOWN":
        raise ValueError("Could not extract a valid EXCHANGE:TICKER symbol from payload")

    resolution = str(
        parsed.get("r") or parsed.get("resolution") or
        query.get("r", [""])[0] or
        query.get("resolution", ["60"])[0]
    )
    # Build message — use explicit msg/message field if present; otherwise
    # synthesise a parse_fire_meta-compatible string from structured JSON keys.
    # This handles both: plain TV webhooks (JSON with winrate/rsi keys) and
    # the canonical TV alert message format ("BUY | WinRate:75%, RSI:67 ...").
    if parsed.get("msg") or parsed.get("message"):
        message = str(parsed.get("msg") or parsed.get("message"))
    else:
        # Build synthetic message from well-known structured fields
        parts = []
        for src_key, out_key in [
            ("winrate",    "WinRate"),
            ("win_rate",   "WinRate"),
            ("netprofit",  "NetProfit%"),
            ("rsi",        "rsi"),
            ("adx",        "ADX"),
            ("sqzoff",     "sqzOff"),
        ]:
            val = parsed.get(src_key) or parsed.get(out_key)
            if val not in (None, ""):
                parts.append(f"{out_key}:{val}")
        # strategy/action goes first
        strategy = parsed.get("strategy") or parsed.get("action") or ""
        message = (strategy + " " + ", ".join(parts)).strip() if parts else text
    fire_time  = datetime.now(timezone.utc).isoformat()

    # Extract close price from payload if provided (used as current_price fallback)
    close_raw = parsed.get("close") or parsed.get("price") or parsed.get("last") or ""
    try:
        close_price: float | None = float(close_raw) if close_raw else None
    except (ValueError, TypeError):
        close_price = None

    return {
        "fire_id":        int(time.time() * 1000),
        "alert_id":       0,
        "symbol":         symbol,
        "pro_symbol":     symbol,
        "resolution":     resolution,
        "sound_file":     None,
        "sound_duration": 0,
        "popup":          False,
        "cross_interval": False,
        "message":        message,
        "fire_time":      fire_time,
        "bar_time":       fire_time,
        "name":           f"webhook_{symbol}",
        "kinds":          ["webhook"],
        "close":          close_price,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Analysis pipeline (runs in background thread)
# ─────────────────────────────────────────────────────────────────────────────
def analyse_fire(fire: dict) -> dict:
    """
    Run full 4-layer + optional L5/L6/L7 Signal Stack analysis.
    Uses TV Screener fundamentals (fixes ASX/ETF gaps), yield curve + VIX +
    sector-ETF momentum for L4, India L5 for NSE/BSE symbols, pool/Smallcase
    boosts from L7, and the backtest sizer – matching the batch signal_stack path.
    """
    tv_sym  = fire.get("pro_symbol") or fire.get("symbol", "")
    yf_tick = ss.tv_to_yf(tv_sym)
    meta    = ss.parse_fire_meta(fire.get("message", ""))

    # ── Leveraged ETF underlying resolution ──────────────────────────────────
    underlying_base, underlying_tv = ss.resolve_etf_underlying(tv_sym)
    if underlying_base == "INDEX":
        is_etf = True; etf_label = "Index ETF"
        score_tv = tv_sym; score_yf = yf_tick
    elif underlying_base and underlying_tv:
        is_etf = True; etf_label = f"via {underlying_base}"
        score_tv = underlying_tv
        score_yf = underlying_base      # e.g. "MSFT"
    else:
        is_etf = False; etf_label = ""
        score_tv = tv_sym; score_yf = yf_tick

    # ── L1: Technical (rel_vol from TV screener) ──────────────────────────────
    screener_cache = ss.fetch_tv_screener([tv_sym])
    l1s = ss.score_technical(meta, screener=screener_cache.get(tv_sym))

    # ── L2: Fundamentals — TV Screener batch (beats yfinance for ASX/ETFs) ───
    # For leveraged ETFs, fetch underlying company's fundamentals
    close_from_fire: float | None = fire.get("close")
    fetch_syms = list({tv_sym, score_tv})   # dedupe (same when not ETF)
    tv_fund_raw: dict = {}                  # initialise before conditional to avoid NameError
    if _TV_FUND_AVAILABLE:
        tv_fund_raw  = _tv_fund_fetch(fetch_syms, use_auth=True)
        fund         = _tv_fund_compat(tv_fund_raw.get(score_tv, {}), current_price=close_from_fire)
        # Ensure fallback fields exist that HTML report expects
        if not fund.get("company_name"):
            fund["company_name"] = fund.get("description", score_yf)
    else:
        fund = ss.fetch_fundamentals(score_yf)   # legacy yfinance fallback
    # Ensure current_price is set — use webhook's close field as fallback
    if not fund.get("current_price") and close_from_fire:
        fund["current_price"] = close_from_fire

    company_name = fund.get("company_name") or fund.get("description") or score_yf
    l2s = ss.score_fundamental(fund)

    # ── L3: Sentiment, news, analyst ─────────────────────────────────────────
    ticker_obj = yf.Ticker(score_yf)   # uses underlying for leveraged ETFs
    sent = ss.fetch_sentiment_data(
        ticker_obj,
        company_name,
        score_tv,
        tv_fund=tv_fund_raw.get(score_tv, {}),
    )
    l3s  = ss.score_sentiment(sent)

    # ── L4: Market internals + yield curve + VIX + sector ETF (macro regime) ─
    yc              = fetch_yield_curve()
    vix_level_val   = fetch_vix_level() if _TV_FUND_AVAILABLE else None
    sector_etf_map  = fetch_sector_etf_momentum() if _TV_FUND_AVAILABLE else {}
    ints            = ss.fetch_market_internals(ticker_obj, fund, score_tv)
    ints["sector_etf_score"] = sector_etf_map.get(fund.get("sector"))
    try:
        l4s = ss.score_market_internals(ints, yc, vix_level=vix_level_val)
    except TypeError:
        # score_market_internals may not yet accept vix_level kwarg in older builds
        l4s = ss.score_market_internals(ints, yc)

    # ── L5: Moomoo institutional flow (optional; overridden by India L5 below) ─
    l5_enrichment = None
    l5_score_val  = None
    try:
        from moomoo_enricher import tv_to_moomoo, compute_l5_score
        mm_code = tv_to_moomoo(tv_sym)
        if mm_code:
            l5_enrichment = compute_l5_score(mm_code)
            l5_score_val  = l5_enrichment.get("l5_score")
    except Exception:
        pass

    # ── L5 override: India portfolio context (NSE/BSE only) ──────────────────
    india_l5_enrichment = None
    _exch_upper = tv_sym.split(":")[0].upper() if ":" in tv_sym else ""
    if _exch_upper in ("NSE", "BSE", "NSE_DLY", "BSE_DLY"):
        try:
            import importlib
            india_enricher_mod = importlib.import_module("india_enricher")
            _ie_inst = india_enricher_mod.IndiaEnricher()
            india_l5_enrichment = _ie_inst.enrich(tv_sym)
            india_l5_score_val  = india_l5_enrichment.get("l5_score")
            if india_l5_score_val is not None:
                l5_enrichment = india_l5_enrichment
                l5_score_val  = india_l5_score_val
        except Exception:
            pass

    # ── L6: IBKR market data + PCR (optional) ────────────────────────────────
    l6_enrichment = None
    l6_score_val  = None
    if _L6_AVAILABLE:
        try:
            l6_enrichment = _wh_compute_l6_score(score_tv)
            l6_score_val  = l6_enrichment.get("l6_score")
        except Exception:
            pass

    # ── L7: External expert signals (YouTube + Smallcase + Twitter) ──────────
    l7_enrichment = None
    l7_score_val  = None
    if _L7_AVAILABLE:
        try:
            l7_enrichment = _wh_compute_l7_score(tv_sym, score_yf)
            l7_score_val  = l7_enrichment.get("l7_score")
        except Exception:
            pass

    # ── Composite (L1–L4 baseline; optional layers add up to 10%/8%/5%) ──────
    comp = ss.composite(l1s["total"], l2s["total"], l3s["total"], l4s["total"],
                        l5_score_val, l6_score_val, l7_score_val)

    # ── Pool boost (+3) and Smallcase boost (+5 to +10) — matching batch path ─
    pool_boost: float = 0.0
    sc_in_basket: bool = False
    sc_boost: float = 0.0
    if l7_enrichment:
        sc_data = l7_enrichment.get("smallcase", {})
        if sc_data and sc_data.get("in_subscription"):
            sc_in_basket = True
            sc_score_raw = sc_data.get("smallcase_score", 5.0)
            sc_boost = round(min(10.0, max(5.0, float(sc_score_raw or 5.0))), 2)
    comp_final = round(min(100.0, comp + pool_boost + sc_boost), 2)

    # ── Backtest position sizer (runs in background of webhook worker thread) ─
    backtest_data = None
    if _SIZER_AVAILABLE and _use_sizer:
        try:
            backtest_data = _wh_get_position_size(
                tv_sym, yf_tick, interval="1d", period="5y", use_cache=True
            )
        except Exception:
            pass

    return {
        "tv_symbol":        tv_sym,
        "yf_ticker":        yf_tick,
        "fire_time":        fire.get("fire_time", ""),
        "resolution":       fire.get("resolution", ""),
        "meta":             meta,
        "fundamentals":     fund,
        "sentiment_data":   sent,
        "market_internals": ints,
        "l5_enrichment":    l5_enrichment,
        "india_l5_enrichment": india_l5_enrichment,
        "l6_enrichment":    l6_enrichment,
        "l7_enrichment":    l7_enrichment,
        "yield_curve":      yc,
        "vix_level":        vix_level_val,
        "scores":           {"l1": l1s, "l2": l2s, "l3": l3s, "l4": l4s},
        "composite":        comp_final,
        "pool_boost":       pool_boost,
        "sc_boost":         sc_boost,
        "sc_in_basket":     sc_in_basket,
        "is_etf":           is_etf,
        "etf_label":        etf_label,
        "backtest_data":    backtest_data,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Moomoo order placement
# ─────────────────────────────────────────────────────────────────────────────
def place_moomoo_order(tv_sym: str, current_price: float | None,
                       order_size_usd: int, trade_pwd: str,
                       units: int = 1) -> dict:
    """
    Place a BUY MARKET order in Moomoo for the given symbol.
    1. Opens a trade context and unlocks (handles GUI-only OpenD).
    2. Queries account buying power to determine how much we can actually spend.
    3. Caps the order to min(order_size_usd * units, available_power * 0.95) to
       leave a 5% buffer for fees and rounding.
       ``units`` comes from the backtest position sizer (0 = skip, 1–3 = scale).
    4. Calculates qty = floor(actual_spend / current_price), minimum 1.
    5. Aborts if available balance < current_price (can't afford 1 share).
    Returns a dict: success, order_id, qty, price, mm_code, balance, error.
    """
    result = {"success": False, "order_id": None, "qty": 0,
              "price": current_price, "mm_code": None,
              "balance": None, "actual_spend": None, "error": ""}

    if not _MOOMOO_TRADE_OK:
        result["error"] = "moomoo-api not installed"
        return result

    mm_code = tv_to_mm_order_code(tv_sym)
    if not mm_code:
        result["error"] = f"No Moomoo market mapping for {tv_sym}"
        return result
    result["mm_code"] = mm_code

    if not current_price or current_price <= 0:
        result["error"] = "No valid current price — cannot calculate qty"
        return result

    try:
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.NONE,
            host=MOOMOO_HOST,
            port=MOOMOO_PORT_OPEND,
            security_firm=MOOMOO_FIRM,
        )

        # ── 1. Unlock trading ─────────────────────────────────────────────────
        # GUI OpenD disables the unlock API — user must unlock manually in app.
        if trade_pwd:
            ret_ul, msg_ul = ctx.unlock_trade(trade_pwd)
            if ret_ul != MM_RET_OK:
                gui_locked = any(kw in str(msg_ul).lower()
                                 for kw in ("unlock button", "gui", "disabled"))
                if not gui_locked:
                    ctx.close()
                    result["error"] = f"unlock_trade failed: {msg_ul}"
                    return result
                print(f"  [WARN] unlock_trade (GUI mode): {msg_ul}", flush=True)

        # ── 2. Check available balance ────────────────────────────────────────
        available_power: float = 0.0
        try:
            ret_acc, acc_data = ctx.accinfo_query(
                acc_id=MOOMOO_ACC_ID, trd_env=MOOMOO_TRD_ENV
            )
            if ret_acc == MM_RET_OK and acc_data is not None and not acc_data.empty:
                available_power = float(acc_data.iloc[0].get("power", 0) or 0)
                if not math.isfinite(available_power):
                    print(f"  [WARN] available_power is {available_power}, clamping to 0", flush=True)
                    available_power = 0.0
                result["balance"] = round(available_power, 2)
        except Exception as e_bal:
            print(f"  [WARN] Could not fetch balance: {e_bal}", flush=True)

        # ── 3. Determine actual spend (cap at 95% of available power) ─────────
        target_spend = order_size_usd * max(1, units)       # scale by position-sizer units
        result["units"] = units
        spendable = available_power * 0.95 if available_power > 0 else target_spend
        actual_spend = min(target_spend, spendable)
        result["actual_spend"] = round(actual_spend, 2)

        if actual_spend < current_price:
            ctx.close()
            result["error"] = (
                f"Insufficient balance: need ${current_price:.2f} for 1 share, "
                f"have ${available_power:.2f} buying power"
            )
            return result

        # ── 4. Calculate qty ──────────────────────────────────────────────────
        qty = max(1, int(actual_spend / current_price))
        result["qty"] = qty
        print(f"  Balance: ${available_power:.0f}  Spend: ${actual_spend:.0f}  "
              f"Price: ${current_price:.2f}  Qty: {qty}", flush=True)

        # ── 5. Place MARKET BUY order ─────────────────────────────────────────
        ret, data = ctx.place_order(
            price=0.0,                   # ignored for MARKET orders
            qty=qty,
            code=mm_code,
            trd_side=TrdSide.BUY,
            order_type=OrderType.MARKET,
            trd_env=MOOMOO_TRD_ENV,
            acc_id=MOOMOO_ACC_ID,
            time_in_force=TimeInForce.DAY,
            fill_outside_rth=False,
            remark="webhook_signal_stack",
        )
        ctx.close()

        if ret == MM_RET_OK and data is not None and not data.empty:
            order_id          = data.iloc[0].get("order_id", "?")
            result["success"] = True
            result["order_id"]= str(order_id)
        else:
            result["error"] = f"place_order returned: {data}"

    except Exception as e:
        result["error"] = str(e)

    return result


def process_fire_background(fire: dict, threshold: int) -> None:
    """Worker: analyse → judge → log/report/order if actionable."""
    sym     = fire.get("pro_symbol", "?")
    ts      = datetime.now().strftime("%H:%M:%S")
    fire_id = str(fire.get("fire_id") or fire.get("fire_time", "unknown"))
    _ldr_sid: "str | None" = fire.get("_ledger_sid")  # set by webhook handler

    try:
        print(f"[{ts}] ⏳ Analysing {sym} ...", flush=True)
        _log.info("analysis_started", symbol=sym, fire_id=fire_id)
        result = analyse_fire(fire)
    except Exception as e:
        print(f"[{ts}] ⚠️  Analysis failed for {sym}: {e}")
        _log.error("analysis_failed", symbol=sym, fire_id=fire_id, error=str(e))
        traceback.print_exc()
        return
    finally:
        with _lock:
            _processing.discard(sym)
        _remove_pending(fire_id)   # CF2: remove from durable pending queue

    comp              = result["composite"]
    g_letter, g_color = ss.grade(comp)
    # Ledger: record analysis outcome
    if _ldr_sid and _LEDGER_OK:
        try:
            _get_order_ledger().record(_ldr_sid, "ANALYZED",
                                       symbol=sym, composite=comp, threshold=threshold)
        except Exception:
            pass
    l   = result["scores"]
    yc  = result.get("yield_curve", {})
    ts  = datetime.now().strftime("%H:%M:%S")
    spread = yc.get("spread_bps")
    yc_str = f"  YC={spread:+.0f}bps" if spread is not None else ""

    print(
        f"[{ts}] {sym:22s}  Composite={comp:5.1f} ({g_letter})  "
        f"L1={l['l1']['total']:.1f} L2={l['l2']['total']:.1f} "
        f"L3={l['l3']['total']:.1f} L4={l['l4']['total']:.1f}{yc_str}",
        end="",
        flush=True,
    )

    if comp >= threshold:
        result["actionable_at"]  = datetime.now(timezone.utc).isoformat()
        result["trigger_source"] = fire.get("_source", SOURCE_WEBHOOK)
        result["moomoo_order"]   = None

        # ── CF3: Alert staleness check — skip orders for expired alerts ────────
        _alert_too_old = False
        fire_dt_str = fire.get("fire_time") or fire.get("bar_time")
        if fire_dt_str:
            try:
                fire_dt = datetime.fromisoformat(str(fire_dt_str).replace("Z", "+00:00"))
                if fire_dt.tzinfo is None:
                    fire_dt = fire_dt.replace(tzinfo=timezone.utc)
                age_secs = (datetime.now(timezone.utc) - fire_dt).total_seconds()
                if age_secs > MAX_ALERT_AGE_SECS:
                    print(f"\n  ⚠️  Alert is {age_secs:.0f}s old (> {MAX_ALERT_AGE_SECS}s) — "
                          f"orders skipped (still logged as actionable)", flush=True)
                    _alert_too_old = True
            except Exception:
                pass  # unparseable timestamp — allow through

        # ── CF6: Determine buy multiplier — fail closed when sizer is expected ─
        backtest_data = result.get("backtest_data")
        backtest_size = backtest_data.get("size_units") if backtest_data else None

        if _use_sizer and _SIZER_AVAILABLE and backtest_data is None:
            # Sizer was available but entire call crashed → fail closed
            print(f"\n  ⛔ Sizer crashed for {sym} — orders blocked (fail-closed). "
                  f"Use --no-sizer to override.", flush=True)
            effective_units = 0
        elif _use_sizer and _SIZER_AVAILABLE and backtest_size is None:
            # Sizer returned an error result (size_units = None) → fail closed
            sizer_err = (backtest_data or {}).get("error", "unknown error")
            print(f"\n  ⛔ Sizer error for {sym} ({sizer_err}) — orders blocked (fail-closed). "
                  f"Use --no-sizer to override.", flush=True)
            effective_units = 0
        elif backtest_size == 0:
            # Sizer explicitly says AVOID
            effective_units = 0
            print(f"\n  ⛔ Backtest says AVOID {sym} — auto-buy skipped", flush=True)
        else:
            effective_units = int(backtest_size) if backtest_size is not None else 1

        if effective_units != 0 and not _alert_too_old:
            # L7 enricher boost: strong external signal bumps units +1 (cap 3)
            l7_enr = result.get("l7_enrichment") or {}
            l7_val = l7_enr.get("l7_score") if l7_enr else None
            if l7_val is not None and l7_val >= 7.0 and effective_units > 0:
                old_u = effective_units
                effective_units = min(3, effective_units + 1)
                if effective_units != old_u:
                    print(f"  🚀 L7 boost: size_units {old_u}→{effective_units} "
                          f"(L7={l7_val:.1f})", flush=True)

        # ── Moomoo auto-buy (if --order-size > 0 and OpenD running) ───────────
        if (_order_size_usd > 0 and _MOOMOO_TRADE_OK
                and effective_units != 0 and not _alert_too_old):
            # Watchdog: block order if OpenD is known-unhealthy
            if _WATCHDOG_OK and not _watchdog.is_healthy("moomoo"):
                print(f"\n  ⚠️  Moomoo UNHEALTHY (watchdog) — order blocked. "
                      f"Run: python platform_watchdog.py --broker moomoo", flush=True)
                _log.warning("order_blocked", symbol=sym, broker="moomoo", reason="watchdog_unhealthy")
            else:
                if _ldr_sid and _LEDGER_OK:
                    try:
                        _get_order_ledger().record(_ldr_sid, "ROUTED", broker="moomoo")
                    except Exception:
                        pass
                current_price = result["fundamentals"].get("current_price")
                print(f"  💰 Placing Moomoo BUY ${_order_size_usd}×{effective_units}u "
                      f"@ ~{current_price} ...",
                      end=" ", flush=True)
                order_result = place_moomoo_order(
                    tv_sym=sym,
                    current_price=current_price,
                    order_size_usd=_order_size_usd,
                    trade_pwd=_trade_pwd,
                    units=effective_units,
                )
                result["moomoo_order"] = order_result
                if order_result["success"]:
                    print(f"✅ order_id={order_result['order_id']} qty={order_result['qty']} {order_result['mm_code']}",
                          flush=True)
                    _log.info("order_placed", symbol=sym, broker="moomoo",
                              order_id=str(order_result["order_id"]),
                              qty=order_result["qty"], mm_code=order_result["mm_code"])
                    if _ldr_sid and _LEDGER_OK:
                        try:
                            _get_order_ledger().record(_ldr_sid, "PLACED",
                                                       broker="moomoo",
                                                       order_id=str(order_result["order_id"]))
                        except Exception:
                            pass
                    # ── Sync HOLDINGS sell alert ────────────────────────────
                    if _HOLDINGS_SYNC:
                        threading.Thread(
                            target=_holdings_on_buy,
                            args=(sym, result["fundamentals"].get("current_price", 0.0),
                                  order_result.get("qty", 1)),
                            daemon=True,
                        ).start()
                else:
                    print(f"⚠️  {order_result['error']}", flush=True)
                    _log.warning("order_failed", symbol=sym, broker="moomoo",
                                 error=order_result["error"])
                    if _ldr_sid and _LEDGER_OK:
                        try:
                            _get_order_ledger().record(_ldr_sid, "REJECTED",
                                                       broker="moomoo",
                                                       error=order_result["error"])
                        except Exception:
                            pass
        elif _order_size_usd > 0 and not _MOOMOO_TRADE_OK:
            print("  ⚠️  Moomoo order skipped — moomoo-api not installed", flush=True)

        # ── India auto-buy (if NSE/BSE symbol and --india-order-size > 0) ─────
        _exch = sym.split(":")[0].upper() if ":" in sym else ""
        if (_india_order_size_inr > 0 and _exch in ("NSE", "BSE", "NSE_DLY", "BSE_DLY")
                and effective_units != 0 and not _alert_too_old):

            # Watchdog: block order if Zerodha session is known-unhealthy
            if _WATCHDOG_OK and not _watchdog.is_healthy("zerodha"):
                print(f"\n  ⚠️  Zerodha UNHEALTHY (watchdog) — India order blocked. "
                      f"Run: python platform_watchdog.py --broker zerodha", flush=True)
            else:
                # India Regime Engine: adjust threshold + size based on market conditions
                _india_units = effective_units
                try:
                    import sys as _s
                    _s.path.insert(0, str(Path(__file__).parent.parent / "india-brokers"))
                    from india_regime import get_regime as _get_regime
                    _regime = _get_regime()
                    if _regime["regime"] != "UNKNOWN":
                        _adj_threshold = threshold + _regime["threshold_adj"]
                        _adj_units     = max(0, round(_india_units * _regime["size_multiplier"]))
                        print(f"\n  🇮🇳 India regime={_regime['regime']} "
                              f"score={_regime['score']:.1f} "
                              f"thr_adj={_regime['threshold_adj']:+d} "
                              f"size_mult={_regime['size_multiplier']}× "
                              f"→ units {_india_units}→{_adj_units}", flush=True)
                        if comp < _adj_threshold:
                            print(f"  ⛔ India order blocked: composite {comp:.1f} < "
                                  f"regime-adjusted threshold {_adj_threshold}", flush=True)
                            _india_units = 0
                        else:
                            _india_units = _adj_units
                except Exception as _re:
                    print(f"  ⚠️  India regime check failed ({_re}) — default sizing", flush=True)

                if _india_units > 0:
                    if _ldr_sid and _LEDGER_OK:
                        try:
                            _get_order_ledger().record(_ldr_sid, "ROUTED", broker="zerodha")
                        except Exception:
                            pass
                    current_price = result["fundamentals"].get("current_price")
                    print(f"  💰 Placing India BUY ₹{_india_order_size_inr} @ ~{current_price} ...",
                          end=" ", flush=True)
                    try:
                        from india_order_executor import place_india_order
                        india_order_result = place_india_order(sym, current_price, _india_order_size_inr)
                        result["india_order"] = india_order_result
                        if india_order_result.get("success"):
                            print(f"✅ order_id={india_order_result.get('order_id')} "
                                  f"qty={india_order_result.get('qty')} {india_order_result.get('symbol')}",
                                  flush=True)
                            _log.info("order_placed", symbol=sym, broker="zerodha",
                                      order_id=str(india_order_result.get("order_id", "")),
                                      qty=india_order_result.get("qty"))
                            if _ldr_sid and _LEDGER_OK:
                                try:
                                    _get_order_ledger().record(
                                        _ldr_sid, "PLACED",
                                        broker="zerodha",
                                        order_id=str(india_order_result.get("order_id", "")))
                                except Exception:
                                    pass
                        else:
                            print(f"⚠️  {india_order_result.get('error')}", flush=True)
                            _log.warning("order_failed", symbol=sym, broker="zerodha",
                                         error=india_order_result.get("error", ""))
                            if _ldr_sid and _LEDGER_OK:
                                try:
                                    _get_order_ledger().record(
                                        _ldr_sid, "REJECTED",
                                        broker="zerodha",
                                        error=india_order_result.get("error", ""))
                                except Exception:
                                    pass
                    except Exception as _ie:
                        print(f"⚠️  India order error: {_ie}", flush=True)
                        _log.error("order_error", symbol=sym, broker="zerodha", error=str(_ie))

        with _lock:
            _actionable.append(result)
            del _actionable[:-MAX_ACTIONABLE]     # trim
            save_state()
            ranked = sorted(_actionable, key=lambda x: x["composite"], reverse=True)

        _rebuild_report(ranked, threshold)
        print(f"  🚨 ACTIONABLE  → {ACTIONABLE_REPORT.name}", flush=True)
    else:
        print(f"  (score {comp:.1f} < threshold {threshold} — not actionable)", flush=True)

    # Always: track result and rebuild full stack report (no threshold filter)
    with _lock:
        _all_results.append(result)
        del _all_results[:-200]
    _rebuild_stack_report()
    _record_alert_safe(fire, result)


def _record_alert_safe(fire: dict, result: dict) -> None:
    """Non-blocking wrapper — alert tracking must never crash the pipeline."""
    if not _TRACKER_OK:
        return
    try:
        tracker = _get_tracker()
        if tracker:
            # Include parsed meta in fire dict so record_alert can read WinRate/RSI/ADX
            fire_with_meta = {**fire, "meta": result.get("meta") or fire.get("meta") or {}}
            tracker.record_alert(fire_with_meta, result)
    except Exception as exc:
        print(f"  [tracker] record_alert failed (non-fatal): {exc}", flush=True)


def _rebuild_stack_report() -> None:
    """Rebuild signal_stack_report.html from ALL results (no threshold filter)."""
    with _lock:
        all_copy = list(_all_results)
    seen: dict[str, dict] = {}
    for e in all_copy:
        s = e.get("tv_symbol", "")
        if s and (s not in seen or e["composite"] > seen[s]["composite"]):
            seen[s] = e
    ranked  = sorted(seen.values(), key=lambda x: x["composite"], reverse=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html    = ss.build_html(ranked, len(ranked), title="Signal Stack — All Signals")
    banner  = (
        f'<div style="background:#0369a1;color:#fff;padding:12px 28px;'
        f'font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px">'
        f'<span style="font-size:18px">📊</span>'
        f'<span>All webhook signals — {len(ranked)} symbol(s) | '
        f'Last updated: {now_str}</span>'
        f'</div>'
    )
    html = html.replace("<body>", "<body>" + banner, 1)
    _atomic_write(STACK_REPORT, html)


def _rebuild_report(ranked: list, threshold: int) -> None:
    """Write actionable_report.html."""
    # Deduplicate by tv_symbol — keep the highest composite entry per symbol.
    # Multiple fires of the same symbol (retries, testing) all accumulate in
    # the log, but the report should show only the best result per symbol.
    seen: dict[str, dict] = {}
    for entry in ranked:
        sym = entry.get("tv_symbol", "")
        if not sym or sym.upper() == "UNKNOWN":
            continue  # skip entries with unparseable symbol (e.g. from test payloads)
        if sym not in seen or entry["composite"] > seen[sym]["composite"]:
            seen[sym] = entry
    deduped = sorted(seen.values(), key=lambda x: x["composite"], reverse=True)

    base = ss.build_html(deduped, len(deduped), title="Actionable Signals")

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    banner  = (
        f'<div style="background:#7c3aed;color:#fff;padding:12px 28px;'
        f'font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px">'
        f'<span style="font-size:18px">🚨</span>'
        f'<span>ACTIONABLE SIGNALS — Composite ≥ {threshold} | '
        f'{len(deduped)} signal(s) | Last updated: {now_str}</span>'
        f'</div>'
    )
    html = base.replace("<body>", "<body>" + banner, 1)
    _atomic_write(ACTIONABLE_REPORT, html)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────────────────────────────────────
class WebhookHandler(BaseHTTPRequestHandler):

    # Suppress default noisy access log
    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            with _lock:
                count = len(_actionable)
            self._send_json(200, {
                "status": "ok",
                "actionable_count": count,
                "threshold": self.server.threshold,  # type: ignore[attr-defined]
                "time": datetime.now(timezone.utc).isoformat(),
            })
        elif parsed.path == "/actionable":
            if ACTIONABLE_REPORT.exists():
                self._send_html(ACTIONABLE_REPORT.read_text(encoding="utf-8"))
            else:
                self._send_html("<p style='font-family:sans-serif;color:#888;padding:2rem'>"
                                "No actionable signals yet — waiting for webhooks.</p>")
        elif parsed.path == "/performance":
            perf_report = REPORTS_DIR / "alert_performance_report.html"
            if perf_report.exists():
                self._send_html(perf_report.read_text(encoding="utf-8"))
            elif _TRACKER_OK:
                tracker = _get_tracker()
                html = tracker.build_performance_report() if tracker else "<p>No data yet.</p>"
                _atomic_write(perf_report, html)
                self._send_html(html)
            else:
                self._send_html("<p style='font-family:sans-serif;color:#888;padding:2rem'>"
                                "Alert tracker not available.</p>")
        else:
            self._send_json(200, {
                "service": "Signal Stack Webhook Server",
                "endpoints": {
                    "POST /webhook":      "Receive TradingView alert",
                    "GET  /health":       "Server status + actionable count",
                    "GET  /actionable":   "Actionable signals report (HTML)",
                    "GET  /performance":  "Alert performance analysis report (HTML)",
                    "POST /analyse":      'Analyse manual tickers: {"tickers": ["NVDA", "oracle"]}',
                    "POST /screener-run": 'Run TV screener: {"classes": ["stocks_us"], "rsi_min": 50}',
                },
            })

    def do_POST(self):  # noqa: C901
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/webhook", "/analyse", "/screener-run"):
            self.send_response(404)
            self.end_headers()
            return

        # ── IP-based rate limiting (only for external /webhook calls) ─────────
        if parsed.path == "/webhook":
            client_ip = self.client_address[0]
            if client_ip != "127.0.0.1":
                now = time.monotonic()
                with _lock:
                    timestamps = _request_counts.get(client_ip, [])
                    # Evict timestamps outside the rolling window
                    timestamps = [t for t in timestamps if now - t < _RATE_WINDOW_SECS]
                    if len(timestamps) >= _MAX_REQ_PER_WINDOW:
                        self._send_json(
                            429,
                            {"error": f"rate limit exceeded — max {_MAX_REQ_PER_WINDOW} requests/{int(_RATE_WINDOW_SECS)}s"},
                        )
                        return
                    timestamps.append(now)
                    _request_counts[client_ip] = timestamps

        length = int(self.headers.get("Content-Length", 0))
        MAX_BODY_BYTES = 65_536  # 64 KB — guard against oversized payloads
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"Payload too large ({length} bytes)"})
            return
        body   = self.rfile.read(min(length, MAX_BODY_BYTES)) if length > 0 else b""
        query  = urllib.parse.parse_qs(parsed.query)

        # ── /analyse — manual ticker ingestion ──────────────────────────────
        if parsed.path == "/analyse":
            try:
                bd = json.loads(body.decode("utf-8", errors="replace")) if body else {}
                raw_tick = bd.get("tickers") or bd.get("symbols") or []
                if isinstance(raw_tick, str):
                    raw_tick = [t.strip() for t in raw_tick.split(",")]
            except Exception:
                self._send_json(400, {"error": 'invalid JSON; expected {"tickers": [...]}'})
                return
            if not raw_tick:
                self._send_json(400, {"error": "no tickers provided"})
                return
            fires_m, errors_m = _manual_to_fires(raw_tick)
            queued_m: list[str] = []
            _thresh = self.server.threshold  # type: ignore[attr-defined]
            for _mf in fires_m:
                _sym = _mf["pro_symbol"]
                with _lock:
                    if _sym not in _processing:
                        _processing.add(_sym)
                        _add_pending(_mf)
                        append_fire_to_disk(_mf)
                        _executor_pool.submit(process_fire_background, _mf, _thresh)
                        queued_m.append(_sym)
            self._send_json(200, {"status": "accepted", "queued": queued_m,
                                   "errors": errors_m, "count": len(queued_m)})
            return

        # ── /screener-run — TV screener ingestion ────────────────────────────
        if parsed.path == "/screener-run":
            try:
                bd = json.loads(body.decode("utf-8", errors="replace")) if body else {}
            except Exception:
                bd = {}
            classes_s = bd.get("classes") or None
            rsi_min_s = float(bd.get("rsi_min", 50.0))
            adx_min_s = float(bd.get("adx_min", 20.0))
            max_per_s = bd.get("max_per_class")
            try:
                fires_s, meta_s = _screener_to_fires(
                    classes=classes_s, rsi_min=rsi_min_s,
                    adx_min=adx_min_s, max_per_class=max_per_s,
                )
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            queued_s = 0
            _thresh = self.server.threshold  # type: ignore[attr-defined]
            for _sf in fires_s:
                _sym = _sf["pro_symbol"]
                with _lock:
                    if _sym not in _processing:
                        _processing.add(_sym)
                        _add_pending(_sf)
                        append_fire_to_disk(_sf)
                        _executor_pool.submit(process_fire_background, _sf, _thresh)
                        queued_s += 1
            self._send_json(200, {"status": "accepted", "queued": queued_s, "meta": meta_s})
            return

        # ── /news-scan — TradingView news signal ingestion ───────────────────
        if parsed.path == "/news-scan":
            try:
                bd = json.loads(body.decode("utf-8", errors="replace")) if body else {}
            except Exception:
                bd = {}
            ns_providers      = bd.get("providers") or None
            ns_max_age        = float(bd.get("max_age_hours", 24.0))
            ns_bull_thresh    = float(bd.get("bullish_threshold", 0.35))
            ns_bear_thresh    = float(bd.get("bearish_threshold", -0.35))
            ns_min_articles   = int(bd.get("min_articles", 1))
            ns_bearish_only   = bool(bd.get("bearish_only", False))
            ns_bullish_only   = bool(bd.get("bullish_only", False))
            try:
                bullish_fires, bearish_fires = _news_to_fires(
                    providers=ns_providers,
                    max_age_hours=ns_max_age,
                    bullish_threshold=ns_bull_thresh,
                    bearish_threshold=ns_bear_thresh,
                    min_articles=ns_min_articles,
                )
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            # Select which signals to queue based on caller preference
            to_queue: list[dict] = []
            if ns_bearish_only:
                to_queue = bearish_fires
            elif ns_bullish_only:
                to_queue = bullish_fires
            else:
                to_queue = bullish_fires + bearish_fires
            queued_n = 0
            _thresh = self.server.threshold  # type: ignore[attr-defined]
            for _nf in to_queue:
                _sym = _nf["pro_symbol"]
                with _lock:
                    if _sym not in _processing:
                        _processing.add(_sym)
                        _add_pending(_nf)
                        append_fire_to_disk(_nf)
                        _executor_pool.submit(process_fire_background, _nf, _thresh)
                        queued_n += 1
            meta_n = _news_scan_meta(
                bullish_fires, bearish_fires,
                ns_providers, ns_max_age, ns_bull_thresh, ns_bear_thresh,
            )
            self._send_json(200, {
                "status": "accepted",
                "queued": queued_n,
                "bullish_available": len(bullish_fires),
                "bearish_available": len(bearish_fires),
                "meta": meta_n,
            })
            return

        # Debug log — shows exact URL params and body prefix received from TV
        print(f"[WEBHOOK] path={self.path!r}  query={dict(query)}  body={body[:120]!r}", flush=True)
        _log.info("webhook_received", path=self.path, body_preview=body[:120])

        try:
            fire = parse_payload(body, query)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        sym = fire["pro_symbol"]

        # Debounce: skip if already processing this symbol
        with _lock:
            if sym in _processing:
                self._send_json(202, {
                    "status":  "debounced",
                    "symbol":  sym,
                    "message": f"{sym} is already being analysed — duplicate skipped.",
                })
                return
            _processing.add(sym)

        # ── Order ledger dedup: reject re-delivered fires already PLACED/FILLED ──
        if _LEDGER_OK:
            try:
                _ldr = _get_order_ledger()
                _ldr_sid = _ldr.make_sid(
                    fire_id=str(fire.get("fire_id") or fire.get("fire_time", "")),
                    symbol=sym,
                    fire_time_iso=str(fire.get("fire_time", "")),
                )
                if _ldr.is_duplicate(_ldr_sid):
                    self._send_json(202, {
                        "status":  "duplicate",
                        "symbol":  sym,
                        "message": "Signal already processed or in-flight (ledger dedup).",
                    })
                    with _lock:
                        _processing.discard(sym)
                    return
                _ldr.record(_ldr_sid, "RECEIVED",
                            symbol=sym,
                            fire_id=str(fire.get("fire_id") or ""))
                fire["_ledger_sid"] = _ldr_sid
            except Exception:
                pass  # ledger errors never block execution

        _add_pending(fire)                   # CF2: durable pending queue
        append_fire_to_disk(fire)
        threshold = self.server.threshold  # type: ignore[attr-defined]
        _executor_pool.submit(               # CF7: bounded thread pool
            process_fire_background, fire, threshold
        )

        self._send_json(200, {
            "status":    "accepted",
            "symbol":    sym,
            "fire_time": fire["fire_time"],
            "message":   "Analysis running — check console and GET /actionable for results.",
        })


# ─────────────────────────────────────────────────────────────────────────────
# Enricher self-test (--test-enrichers CLI option, no server needed)
# ─────────────────────────────────────────────────────────────────────────────
def _run_enricher_test(source: str, symbol: str) -> None:  # noqa: C901
    """
    Probe one or all external enrichers for *symbol* and print results.
    Designed for manual validation:
        python webhook_server.py --test-enrichers youtube --test-symbol NSE:RELIANCE
        python webhook_server.py --test-enrichers all     --test-symbol NVDA
        python webhook_server.py --test-enrichers sizer   --test-symbol NASDAQ:NVDA
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    SEP = "─" * 60

    print(f"\n{SEP}")
    print(f"  Enricher Test — symbol: {symbol}")
    print(f"  Sources     : {source}")
    print(f"{SEP}\n")

    # ── YouTube ──────────────────────────────────────────────────────────────
    if source in ("youtube", "all"):
        print("── YouTube (L7a) " + "─" * 44)
        if _L7_AVAILABLE:
            try:
                from youtube_enricher import (
                    get_youtube_mentions, score_youtube,
                    EXPERT_CHANNELS, MEMBER_CHANNELS, MEMBER_WEIGHT_MULTIPLIER,
                )
                print(f"  Channels : {', '.join(EXPERT_CHANNELS.keys())}")
                member_names = [n for n, cid in EXPERT_CHANNELS.items()
                                if cid in MEMBER_CHANNELS]
                if member_names:
                    print(f"  Members  : {', '.join(member_names)} "
                          f"({MEMBER_WEIGHT_MULTIPLIER}× weight)")
                data  = get_youtube_mentions(symbol)
                score = score_youtube(data)
                has_member = any(v.get("is_member_channel") for v in data.get("videos", []))
                print(f"  Mentions : {data['mention_count']}  "
                      f"Sentiment: {data['sentiment']:+.3f}  "
                      f"Score: {score:.1f}/10"
                      + ("  [MEMBER ×1.5]" if has_member else ""))
                for v in data.get("videos", []):
                    flag = "👑" if v.get("is_member_channel") else ("📌" if v.get("in_title") else "📄")
                    print(f"    {flag} [{v['channel']}] {v['title'][:75]}")
                    print(f"       published={v['published_at']}  "
                          f"sentiment={v['sentiment']:+.3f}  "
                          f"in_title={v['in_title']}")
                if not data.get("videos"):
                    print(f"  (no mentions of {symbol} in last 72 h)")
            except Exception as exc:
                print(f"  ERROR: {exc}")
        else:
            print("  ⚠️  L7 / youtube_enricher not available (check pip install)")
        print()

    # ── Smallcase ────────────────────────────────────────────────────────────
    if source in ("smallcase", "all"):
        print("── Smallcase (L7b) " + "─" * 42)
        if _L7_AVAILABLE:
            try:
                from smallcase_enricher import score_smallcase
                result = score_smallcase(symbol)
                print(f"  In subscription : {result.get('in_subscription')}")
                print(f"  Baskets         : {result.get('basket_names') or 'none'}")
                print(f"  Fetched stocks  : {result.get('all_fetched', 0)}")
                print(f"  Score           : {result.get('smallcase_score'):.1f}/10")
                if result.get("error"):
                    print(f"  Note            : {result['error']}")
            except Exception as exc:
                print(f"  ERROR: {exc}")
        else:
            print("  ⚠️  L7 / smallcase_enricher not available (check pip install)")
        print()

    # ── Twitter ───────────────────────────────────────────────────────────────
    if source in ("twitter", "all"):
        print("── Twitter (L7c) " + "─" * 43)
        if _L7_AVAILABLE:
            try:
                from twitter_enricher import get_twitter_mentions, score_twitter, EXPERT_HANDLES
                print(f"  Handles  : {', '.join(EXPERT_HANDLES)}")
                data  = get_twitter_mentions(symbol)
                score = score_twitter(data)
                print(f"  Mentions : {data['mention_count']}  "
                      f"Sentiment: {data['sentiment']:+.3f}  "
                      f"Score: {score if score is not None else 'None (abstain)'}")
                for tw in data.get("tweets", [])[:5]:
                    print(f"    @{tw['handle']} [{tw['date']}]: {tw['text'][:80]}")
                if not data.get("tweets"):
                    print(f"  (no mentions of {symbol} in recent tweets — twscrape accounts needed)")
            except Exception as exc:
                print(f"  ERROR: {exc}")
        else:
            print("  ⚠️  L7 / twitter_enricher not available (check pip install)")
        print()

    # ── Combined L7 ───────────────────────────────────────────────────────────
    if source == "all":
        print("── L7 Combined " + "─" * 45)
        if _L7_AVAILABLE:
            try:
                result = _wh_compute_l7_score(symbol)
                l7s    = result.get("l7_score")
                print(f"  YT score    : {result.get('yt_score')}")
                print(f"  SC score    : {result.get('sc_score')}")
                print(f"  TW score    : {result.get('tw_score')}")
                print(f"  L7 combined : {l7s if l7s is not None else 'N/A'}")
                print(f"  Sources     : {result.get('sources_active', 0)} active")
            except Exception as exc:
                print(f"  ERROR: {exc}")
        print()

    # ── Backtest position sizer ───────────────────────────────────────────────
    if source in ("sizer", "all"):
        print("── Backtest Position Sizer " + "─" * 33)
        if _SIZER_AVAILABLE:
            try:
                yf_tick = ss.tv_to_yf(symbol)
                data    = _wh_get_position_size(symbol, yf_tick,
                                                interval="1d", period="5y",
                                                use_cache=True)
                if data.get("error"):
                    print(f"  Error      : {data['error']}")
                else:
                    print(f"  Win rate   : {data.get('win_rate', 0):.1f}%")
                    print(f"  Total P&L  : {data.get('total_pnl', 0):.1f}%")
                    print(f"  Sharpe     : {data.get('sharpe', 0):.3f}")
                    print(f"  # trades   : {data.get('num_closed', 0)}")
                    print(f"  Avg win    : {data.get('avg_win', 0):.1f}%")
                    print(f"  Avg loss   : {data.get('avg_loss', 0):.1f}%")
                    print(f"  Avg hold   : {data.get('avg_hold', 0):.1f} bars")
                    print(f"  Size units : {data.get('size_units')} — {data.get('size_label')}")
                    print(f"  From cache : {data.get('from_cache', False)}")
            except Exception as exc:
                print(f"  ERROR: {exc}")
        else:
            print("  ⚠️  backtest_sizer not available (check backtest/ folder)")
        print()

    print(f"{SEP}")
    print("  Test complete.\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    global _trade_pwd, _order_size_usd, _india_order_size_inr, _use_sizer, _executor_pool

    parser = argparse.ArgumentParser(description="Signal Stack Webhook Server")
    parser.add_argument("--port",       type=int, default=PORT,
                        help=f"Listening port (default: {PORT})")
    parser.add_argument("--threshold",  type=int, default=ACTIONABLE_THRESHOLD,
                        help=f"Composite ≥ N → actionable (default: {ACTIONABLE_THRESHOLD})")
    parser.add_argument("--order-size", type=int, default=0,
                        help="Auto-buy size in USD per signal (0 = disabled, e.g. 500)")
    parser.add_argument("--trade-pwd",  type=str, default="",
                        help="Moomoo trading password (or set MOOMOO_TRADE_PWD env var)")
    parser.add_argument("--india-order-size", type=int, default=0,
                        help="Auto-buy size in INR per NSE/BSE signal (0 = disabled, e.g. 10000)")
    parser.add_argument("--no-sizer",   action="store_true",
                        help="Skip backtest position sizer — faster but qty scaling disabled")
    parser.add_argument("--test-enrichers", metavar="SOURCE",
                        choices=["youtube", "smallcase", "twitter", "sizer", "all"],
                        help="Probe an enricher and exit (no server started)")
    parser.add_argument("--test-symbol", default="NSE:RELIANCE",
                        help="Symbol to use with --test-enrichers (default: NSE:RELIANCE)")
    parser.add_argument("--analyse", metavar="TICKERS",
                        help='Analyse tickers without starting server, e.g. --analyse "NVDA,oracle"')
    parser.add_argument("--screener-run", metavar="CLASSES",
                        help='Run screener without starting server, e.g. --screener-run "stocks_us"')
    parser.add_argument("--news-scan",  action="store_true",
                        help="Scan TradingView news for bullish signals (no server)")
    parser.add_argument("--news-scan-bearish", action="store_true",
                        help="Scan TradingView news for bearish/PUT signals (no server)")
    parser.add_argument("--news-providers", default=None,
                        help="Comma-separated news providers for --news-scan (default: quartr,reuters,dow-jones,market-watch,invezz)")
    parser.add_argument("--news-max-age", type=float, default=24.0,
                        help="Max article age in hours for --news-scan (default: 24)")
    parser.add_argument("--rsi-min", type=float, default=50.0,
                        help="Min RSI for --screener-run (default: 50)")
    parser.add_argument("--adx-min", type=float, default=20.0,
                        help="Min ADX for --screener-run (default: 20)")
    parser.add_argument("--price-update-interval", type=int, default=3600,
                        help="Seconds between background price snapshots (default: 3600)")
    args = parser.parse_args()

    # ── C2 fix: validate --threshold and --order-size ranges ──────────────
    if not (0 <= args.threshold <= 100):
        parser.error("--threshold must be between 0 and 100")
    if args.order_size < 0:
        parser.error("--order-size cannot be negative")
    if args.india_order_size < 0:
        parser.error("--india-order-size cannot be negative")

    # Trading password — CLI arg takes priority over env var
    _trade_pwd      = args.trade_pwd or os.environ.get("MOOMOO_TRADE_PWD", "")
    _order_size_usd = args.order_size
    _india_order_size_inr = args.india_order_size
    _use_sizer      = not args.no_sizer  # type: ignore[assignment]

    # ── Enricher self-test mode (no server) ───────────────────────────────────
    if args.test_enrichers:
        _run_enricher_test(args.test_enrichers, args.test_symbol)
        return

    # ── One-shot analyse / screener-run mode (no server) ─────────────────────
    if args.analyse or args.screener_run:
        _executor_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="wh-worker"
        )
        load_state()
        if args.analyse:
            _fires_os, _errors_os = _manual_to_fires(args.analyse)
            if _errors_os:
                print(f"  ⚠️  Could not resolve: {_errors_os}")
        else:
            _class_list = [c.strip() for c in args.screener_run.split(",") if c.strip()]
            _fires_os, _ = _screener_to_fires(
                classes=_class_list if _class_list else None,
                rsi_min=args.rsi_min,
                adx_min=args.adx_min,
            )
        _futs_os = [
            _executor_pool.submit(process_fire_background, _f, args.threshold)
            for _f in _fires_os
        ]
        for _fut in concurrent.futures.as_completed(_futs_os):
            try:
                _fut.result()
            except Exception as exc:
                print(f"  ERROR: {exc}")
        _executor_pool.shutdown(wait=True)
        return

    # ── One-shot news-scan mode (no server) ──────────────────────────────────
    if args.news_scan or args.news_scan_bearish:
        _executor_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="wh-worker"
        )
        load_state()
        _ns_providers = (
            [p.strip() for p in args.news_providers.split(",") if p.strip()]
            if args.news_providers else None
        )
        _bullish_fires, _bearish_fires = _news_to_fires(
            providers=_ns_providers,
            max_age_hours=args.news_max_age,
        )
        _news_fires_to_run: list[dict] = (
            _bearish_fires if args.news_scan_bearish else _bullish_fires
        )
        if not _news_fires_to_run:
            _direction = "bearish" if args.news_scan_bearish else "bullish"
            print(f"  (no {_direction} news signals found above threshold)")
        else:
            _futs_ns = [
                _executor_pool.submit(process_fire_background, _f, args.threshold)
                for _f in _news_fires_to_run
            ]
            for _fut_ns in concurrent.futures.as_completed(_futs_ns):
                try:
                    _fut_ns.result()
                except Exception as exc:
                    print(f"  ERROR: {exc}")
        _executor_pool.shutdown(wait=True)
        return

    order_status = (
        f"${_order_size_usd} per signal (MARKET BUY)" if _order_size_usd > 0
        else "disabled (use --order-size 500 to enable)"
    )
    mm_status = "✅ moomoo-api ready" if _MOOMOO_TRADE_OK else "⚠️  moomoo-api not installed"
    tv_fund_status = "✅ TV Screener fundamentals" if _TV_FUND_AVAILABLE else "⚠️  yfinance fallback"

    load_state()

    # Start platform health watchdog (non-blocking background thread)
    if _WATCHDOG_OK:
        _watchdog.start_watchdog(interval=90)
        print("  🩺 Platform watchdog started (90s poll interval)")

    # Initialize order event ledger (creates SQLite DB on first use)
    if _LEDGER_OK:
        _get_order_ledger()  # triggers DB schema creation
        print("  📋 Order event ledger initialized (order_ledger.db)")

    # Initialize alert tracker + start background price-update thread
    if _TRACKER_OK:
        _get_tracker()  # triggers DB schema creation
        print("  📊 Alert tracker initialized (alert_tracker.db)")

        def _bg_price_loop() -> None:
            import time as _time
            while True:
                _time.sleep(args.price_update_interval)
                try:
                    _t = _get_tracker()
                    if _t:
                        _t.update_prices()
                        _t.compute_metrics()
                except Exception:
                    pass

        _pt = threading.Thread(target=_bg_price_loop, daemon=True, name="price-updater")
        _pt.start()

    # CF7: Create bounded worker pool
    _executor_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=WORKER_POOL_SIZE, thread_name_prefix="wh-worker"
    )

    # CF2: Replay alerts that were in-flight when the server last died
    _pending = _get_pending_fires()
    if _pending:
        print(f"  Replaying {len(_pending)} pending fire(s) from before shutdown ...")
        for _pf in _pending:
            _executor_pool.submit(process_fire_background, _pf, args.threshold)

    # Rebuild report from existing log on startup
    if _actionable:
        with _lock:
            ranked = sorted(_actionable, key=lambda x: x["composite"], reverse=True)
        _rebuild_report(ranked, args.threshold)
        print(f"  Rebuilt report from {len(ranked)} existing signals.")

    print(f"""
+----------------------------------------------------------------------+
|  Signal Stack  -  Webhook Server  (Enhanced)                        |
+----------------------------------------------------------------------+
|  Listening on  :  http://0.0.0.0:{args.port:<5}                          |
|  Webhook URL   :  POST http://localhost:{args.port}/webhook            |
|  Actionable >=  : {args.threshold} composite score                           |
|  Auto-order    :  {order_status:<48}|
|  {mm_status:<68}|
|  {tv_fund_status:<68}|
+----------------------------------------------------------------------+
|  Test locally:  python test_webhook.py                               |
|  View report:   GET http://localhost:{args.port}/actionable              |
|  Health check:  GET http://localhost:{args.port}/health               |
+----------------------------------------------------------------------+
""")

    server = HTTPServer(("0.0.0.0", args.port), WebhookHandler)
    server.threshold = args.threshold  # type: ignore[attr-defined]

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋  Shutting down.")
        server.shutdown()
        if _executor_pool is not None:
            _executor_pool.shutdown(wait=True)





if __name__ == "__main__":
    main()
