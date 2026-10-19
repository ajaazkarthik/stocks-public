"""
Sell Strategy — Position Exit Decision Engine
=============================================
For each open position, applies 8 evidence-based exit rules and produces
an ACTION recommendation.

Rules:
  1. Hard Stop        — entry − 2×ATR_at_entry  (floor: entry × 0.92)
  2. Break-Even Stop  — once price hit entry + 1×ATR, stop moves to entry
  3. Trailing Stop    — rolling_high − 1.5×ATR_current (activates after TP1)
  4. R-Multiple TPs   — TP1 @+1.5R → reduce 30%  |  TP2 @+3.0R → reduce 50%
  5. Time Stop        — 20 trading days with < +3% gain → EXIT (dead money)
  6. Signal Decay     — RSI<45, ADX<20, RelVol<0.5 → decay flags (2+ = REDUCE)
  7. VIX Regime       — VIX>35 force exit losers; VIX>25 tighten stops
  8. Fundamental Drift— PE expansion or earnings miss flag (review trigger)

Input:  positions.json  (in same folder as this script)
Output: sell_strategy_report.html

Usage:
  python sell_strategy.py
  python sell_strategy.py --positions my_positions.json --out my_report.html
  python sell_strategy.py --demo          # run with built-in demo positions
"""

import json, sys, os, argparse, traceback, math, sqlite3, csv, re
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

# ── Windows: force UTF-8 console so ₹/→/emoji render correctly ────────────────
if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
try:
    from moomoo import (
        OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK
    )
    _MOOMOO_OK = True
except ImportError:
    _MOOMOO_OK = False

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("ERROR: pip install yfinance pandas numpy")

# ─── Optional: Signal Engine (L1-L4 analysis) ──────────────────────────────────
try:
    _sell_dir = Path(__file__).parent
    if str(_sell_dir.parent) not in sys.path:
        sys.path.insert(0, str(_sell_dir.parent))
    from signal_engine.analysis_engine import AnalysisEngine as _AnalysisEngine
    _ANALYSIS_ENGINE = _AnalysisEngine()
    _SIGNAL_INSIGHT_OK = True
except Exception:
    _SIGNAL_INSIGHT_OK = False
    _ANALYSIS_ENGINE   = None  # type: ignore[assignment]

# ─── Jinja2 template environment ──────────────────────────────────────────────
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=False,
)

# ─── Structured logging ────────────────────────────────────────────────────────
try:
    from log_config import get_logger as _get_logger
except ImportError:
    import logging as _logging_fb
    _get_logger = lambda n: _logging_fb.getLogger(n)
_log = _get_logger("sell_strategy")

# ─── Constants ─────────────────────────────────────────────────────────────────
ROOT            = Path(__file__).parent.parent
POSITIONS_FILE  = ROOT / "positions.json"
OUTPUT_HTML     = ROOT.parent / "reports" / "sell_strategy_report.html"
_SELL_DB_PATH   = ROOT.parent / "database" / "sell_history.db"
_ARCHIVE_ROOT   = ROOT.parent / "reports" / "archive" / "sell"
ATR_PERIOD      = 14
HARD_STOP_MULT  = 2.0          # ATR multiples below entry for hard stop
HARD_STOP_FLOOR = 0.08         # minimum 8% loss before hard stop triggers
BREAKEVEN_MULT  = 1.0          # +1×ATR to trigger break-even stop
TRAIL_MULT      = 1.5          # ATR multiples for trailing stop
TP1_R           = 1.5          # R-multiples for TP1
TP2_R           = 3.0          # R-multiples for TP2
TIME_STOP_DAYS  = 20           # trading days
TIME_STOP_GAIN  = 0.03         # need > 3% gain to avoid time stop

EXCHANGE_MAP = {
    "ASX": ".AX", "ASX_DLY": ".AX",
    "NSE": ".NS", "BSE": ".BO",
    "SGX": ".SI", "HKEX": ".HK",
    "TYO": ".T", "KRX": ".KS",
    "TSX": ".TO", "LON": ".L",
    "NYSE": "", "NASDAQ": "", "AMEX": "", "BATS": "",
}

_TV_SCANNER = {
    "NYSE": "https://scanner.tradingview.com/america/scan",
    "NASDAQ": "https://scanner.tradingview.com/america/scan",
    "AMEX": "https://scanner.tradingview.com/america/scan",
    "BATS": "https://scanner.tradingview.com/america/scan",
    "ASX": "https://scanner.tradingview.com/australia/scan",
    "ASX_DLY": "https://scanner.tradingview.com/australia/scan",
    "NSE": "https://scanner.tradingview.com/india/scan",
    "BSE": "https://scanner.tradingview.com/india/scan",
    "HKEX": "https://scanner.tradingview.com/hongkong/scan",
    "SGX": "https://scanner.tradingview.com/singapore/scan",
    "TYO": "https://scanner.tradingview.com/japan/scan",
    "KRX": "https://scanner.tradingview.com/korea/scan",
    "TSX": "https://scanner.tradingview.com/canada/scan",
    "LON": "https://scanner.tradingview.com/uk/scan",
}

# ─── Delisted ticker detection (P2-B) ─────────────────────────────────────────
_ALERT_DB_PATH = ROOT.parent / "database" / "alert_tracker.db"


def _is_delisted(symbol: str) -> bool:
    """Return True if *symbol* is flagged as delisted in alert_tracker.db."""
    if not _ALERT_DB_PATH.exists():
        return False
    try:
        with sqlite3.connect(str(_ALERT_DB_PATH), timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            row = conn.execute(
                "SELECT is_delisted FROM alerts WHERE tv_symbol = ? AND is_delisted = 1 LIMIT 1",
                (symbol,),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def _mark_delisted(symbol: str) -> None:
    """Flag *symbol* as delisted in alert_tracker.db."""
    if not _ALERT_DB_PATH.exists():
        return
    try:
        with sqlite3.connect(str(_ALERT_DB_PATH), timeout=30) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cur = conn.execute(
                "UPDATE alerts SET is_delisted = 1 WHERE tv_symbol = ?",
                (symbol,),
            )
            conn.commit()
            if cur.rowcount == 0:
                print(f"  [WARN] _mark_delisted: symbol '{symbol}' not found in alerts table")
    except Exception:
        pass

# ─── Moomoo live-positions config ──────────────────────────────────────────────
_MOOMOO_HOST = "127.0.0.1"
_MOOMOO_ENV  = TrdEnv.REAL if _MOOMOO_OK else None
# Account list loaded at call time from moomoo_config (supports both AU and SG)
_MOOMOO_NYSE = {"XLV","SOXL","CURE","LRCX","XOM","JPM","GS","V","MA","WMT",
                "HD","UNH","JNJ","PFE","KO","PEP","BA","CAT","HON","GE","CVX"}

def _moomoo_to_tv(code: str) -> str:
    """Convert Moomoo OpenD code (e.g. US.AAPL, AU.CBA, HK.00700, JP.1579) to TradingView symbol."""
    if "." not in code:
        return code
    prefix, ticker = code.split(".", 1)
    prefix = prefix.upper()
    if prefix == "AU":  return f"ASX:{ticker}"
    if prefix == "HK":  return f"HKEX:{ticker}"
    if prefix == "SG":  return f"SGX:{ticker}"
    if prefix == "JP":  return f"TYO:{ticker}"
    if prefix == "KR":  return f"KRX:{ticker}"
    if prefix == "CA":  return f"TSX:{ticker}"
    if prefix == "UK":  return f"LON:{ticker}"
    if prefix in ("US", ""):
        return f"NYSE:{ticker}" if ticker in _MOOMOO_NYSE else f"NASDAQ:{ticker}"
    return f"NASDAQ:{ticker}"

def _fetch_live_positions() -> list[dict]:
    """Fetch open positions live from Moomoo OpenD — both AU and SG accounts."""
    if not _MOOMOO_OK:
        print("  [WARN] moomoo-api not installed — cannot fetch live positions")
        return []
    _root = str(Path(__file__).parent.parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from moomoo_config import ACCOUNTS  # [(label, port, firm, acc_id), ...]
    all_positions: list[dict] = []
    for label, port, firm, acc_id in ACCOUNTS:
        print(f"  Connecting to Moomoo OpenD ({label}, port {port}) ...")
        try:
            ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.NONE, host=_MOOMOO_HOST, port=port,
                security_firm=firm
            )
            try:
                ret, df = ctx.position_list_query(trd_env=_MOOMOO_ENV, acc_id=acc_id)
                if ret != RET_OK:
                    print(f"  [WARN] OpenD {label} error: {df}")
                    continue

                # Build earliest BUY date lookup from deal history
                earliest_buy: dict[str, str] = {}
                try:
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    ret_d, deals_df = ctx.history_deal_list_query(
                        trd_env=_MOOMOO_ENV, acc_id=acc_id,
                        start="2018-01-01", end=today_str,
                    )
                    if ret_d == RET_OK and not deals_df.empty:
                        for _, deal_row in deals_df.iterrows():
                            side = str(deal_row.get("trd_side", "")).upper()
                            if "BUY" not in side and side != "B":
                                continue
                            dcode = str(deal_row.get("code", ""))
                            raw_dt = str(deal_row.get("create_time", "") or
                                         deal_row.get("create_timestamp", ""))
                            dt_str = raw_dt[:10] if raw_dt else ""  # YYYY-MM-DD
                            if not dt_str:
                                continue
                            if dcode not in earliest_buy or dt_str < earliest_buy[dcode]:
                                earliest_buy[dcode] = dt_str
                        print(f"  Deal history: {len(earliest_buy)} symbols with buy dates ({label})")
                except Exception as _de:
                    print(f"  [INFO] Could not fetch deal history for {label}: {_de}")

                count = 0
                for _, row in df.iterrows():
                    code       = str(row.get("code", ""))
                    qty        = float(row.get("qty", 0))
                    avg_cost   = float(row.get("average_cost", 0))
                    market_val = float(row.get("market_val", 0))
                    if qty <= 0 or not math.isfinite(avg_cost):
                        continue
                    if avg_cost <= 0:
                        # Leveraged ETFs (e.g. SOXL) sometimes report avg_cost=0 from OpenD.
                        # Fall back to current market_val / qty so they still appear in report.
                        # WARNING: PNL calculations will be unreliable (entry ≈ current price → 0% PNL).
                        if market_val > 0 and qty > 0:
                            avg_cost = abs(market_val / qty)
                            print(f"  [WARN] {code}: avg_cost=0 from OpenD — PNL unreliable (fallback to market_val/qty={avg_cost:.4f})")
                        else:
                            continue  # cannot determine entry price
                    avg_cost_fallback = float(row.get("average_cost", 0)) <= 0
                    nom_price  = float(row.get("nominal_price") or 0) or (market_val / qty if qty else 0)
                    tv_sym = _moomoo_to_tv(code)
                    pos_dict = {
                        "symbol":          tv_sym,
                        "entry_price":     round(avg_cost, 4),
                        "entry_date":      earliest_buy.get(code),  # None if not found
                        "shares":          int(qty),
                        "atr_at_entry":    None,
                        "high_water_mark": None,
                        "current_price":   round(nom_price, 4) if nom_price else None,
                        "broker":          f"Moomoo-{label}",
                    }
                    if avg_cost_fallback:
                        pos_dict["avg_cost_unreliable"] = True
                    all_positions.append(pos_dict)
                    count += 1
                print(f"  Fetched {count} live positions from {label}")
            except Exception as e:
                print(f"  [WARN] OpenD {label} connection error: {e}")
            finally:
                ctx.close()
        except Exception as e:
            print(f"  [WARN] Could not connect to OpenD {label} (port {port}): {e}")
    print(f"  Total Moomoo positions fetched: {len(all_positions)} (AU + SG)")
    return all_positions


def _fetch_india_positions() -> tuple[list[dict], int, int]:
    """Fetch India (Zerodha + Angel One) positions via india_cgt module.
    Returns (positions, zerodha_count, angelone_count)."""
    try:
        _india_dir = Path(__file__).parent.parent.parent / "india-brokers"
        if str(_india_dir) not in sys.path:
            sys.path.insert(0, str(_india_dir))
        import india_cgt as _india_cgt  # type: ignore
        pos = _india_cgt.positions_for_sell_strategy()
        zerodha_n  = sum(1 for p in pos if p.get("broker") == "Zerodha")
        angelone_n = sum(1 for p in pos if p.get("broker") == "AngelOne")
        print(f"  Fetched {len(pos)} India positions (Zerodha: {zerodha_n}, AngelOne: {angelone_n})")
        return pos, zerodha_n, angelone_n
    except Exception as e:
        print(f"  [WARN] India positions fetch failed: {e}")
        return [], 0, 0


def _fetch_ibkr_positions() -> list[dict]:
    """Fetch IBKR open positions from cached Flex XML or fresh API call."""
    try:
        _ibkr_dir = Path(__file__).parent.parent.parent / "cgt" / "ibkr-cgt"
        if str(_ibkr_dir) not in sys.path:
            sys.path.insert(0, str(_ibkr_dir))
        import ibkr_cgt_analysis as ibkr  # type: ignore

        cache_path = _ibkr_dir / "_last_flex.xml"
        if cache_path.exists():
            print(f"  Reading cached IBKR Flex XML ({cache_path.name}) ...")
            xml_str = cache_path.read_text(encoding="utf-8")
            _, stmt_pos, _, prior_pos = ibkr.parse_flex_xml(xml_str, ibkr.FETCH_START)
        else:
            print("  No cached IBKR XML — fetching fresh Flex statement (~2 min) ...")
            _, stmt_pos, _, prior_pos = ibkr.fetch_flex_statement(
                ibkr.FLEX_TOKEN, ibkr.FLEX_QUERY_ID, ibkr.FETCH_START
            )

        if stmt_pos is None or stmt_pos.empty:
            print("  [WARN] No IBKR statement positions found")
            return []

        positions = []
        for _, row in stmt_pos.iterrows():
            code     = str(row.get("code", ""))
            qty      = float(row.get("qty", 0))
            avg_cost = float(row.get("avg_cost", 0))
            last_p   = float(row.get("last_price", 0) or 0)
            if qty <= 0 or avg_cost <= 0 or not math.isfinite(avg_cost):
                continue
            tv_sym = _moomoo_to_tv(code)  # IBKR uses same US.SYMBOL format as Moomoo
            # Enrich entry_date from prior period positions if available
            pp = prior_pos.get(code, {})
            entry_dt = str(pp["open_date"]) if pp.get("open_date") else None
            positions.append({
                "symbol":          tv_sym,
                "entry_price":     round(avg_cost, 4),
                "entry_date":      entry_dt,
                "shares":          int(qty),
                "atr_at_entry":    None,
                "high_water_mark": None,
                "current_price":   round(last_p, 4) if last_p else None,
                "broker":          "IBKR",
            })

        print(f"  Fetched {len(positions)} IBKR positions")
        return positions
    except Exception as e:
        print(f"  [WARN] IBKR positions fetch failed: {e}")
        return []


def _fetch_webull_positions_api() -> list[dict] | None:
    """Try the official Webull AU OpenAPI (developer.webull.com.au) first.
    Returns None only when the API is unavailable, and a list (including an
    empty list) after a successful response. See
    cgt/webull-cgt/webull_au_api.py for setup instructions."""
    try:
        _wb_dir = Path(__file__).parent.parent.parent / "cgt" / "webull-cgt"
        if str(_wb_dir) not in sys.path:
            sys.path.insert(0, str(_wb_dir))
        from webull_au_api import is_configured, get_holdings  # type: ignore

        if not is_configured():
            return None
        raw_positions, status = get_holdings()
        if not raw_positions:
            if str(status).startswith("Live API"):
                print(f"  Fetched 0 Webull AU positions ({status})")
                return []
            print(f"  [WARN] Webull API unavailable ({status})")
            return None

        positions = []
        for p in raw_positions:
            symbol   = str(p.get("symbol", "")).strip().upper()
            currency = str(p.get("currency", "USD")).upper()
            exchange = str(p.get("exchange", "")).upper()
            qty      = p.get("shares", 0) or 0
            if not symbol or not qty:
                continue

            if currency == "AUD" or exchange == "AU":
                tv_sym = f"ASX:{symbol}"
            elif currency in ("HKD", "CNH") or exchange == "HK":
                tv_sym = f"HKEX:{symbol}"
            elif currency == "SGD" or exchange == "SG":
                tv_sym = f"SGX:{symbol}"
            else:
                tv_sym = f"NYSE:{symbol}" if symbol in _MOOMOO_NYSE else f"NASDAQ:{symbol}"

            positions.append({
                "symbol":          tv_sym,
                "entry_price":     round(float(p.get("avg_cost", 0) or 0), 4),
                "entry_date":      None,
                "shares":          int(qty) if float(qty) == int(qty) else qty,
                "atr_at_entry":    None,
                "high_water_mark": None,
                "current_price":   p.get("current_price"),
                "broker":          "Webull",
            })

        print(f"  Fetched {len(positions)} Webull AU positions (Live API, {status})")
        return positions
    except Exception as e:
        print(f"  [WARN] Webull API fetch failed: {e}")
        return None


def _fetch_webull_positions_pdf() -> list[dict]:
    """Fetch Webull AU positions from PDF statements (latest statement holdings).

    Results are cached in webull-statement/_webull_parse_cache.json keyed by
    PDF fingerprint (filename → mtime).  Re-parsing only happens when PDFs change.
    The cache is shared with global_portfolio_dashboard.py so a recent dashboard
    run avoids a second PDF parse here.
    """
    try:
        _wb_dir = Path(__file__).parent.parent.parent / "cgt" / "webull-cgt"
        if str(_wb_dir) not in sys.path:
            sys.path.insert(0, str(_wb_dir))
        from webull_statement_reader import parse_all_pdfs  # type: ignore

        pdf_dir = Path(__file__).parent.parent.parent / "webull-statement"
        _cache_path = pdf_dir / "_webull_parse_cache.json"

        # Build fingerprint: {filename: mtime} for all PDFs in folder
        pdf_files = sorted(pdf_dir.glob("*.pdf"))
        fingerprint = {f.name: f.stat().st_mtime for f in pdf_files}

        # Try loading from cache if fingerprint matches
        data: dict | None = None
        _cache_source = "parsed"
        if _cache_path.exists():
            try:
                _disk = json.loads(_cache_path.read_text(encoding="utf-8"))
                if _disk.get("fingerprint") == fingerprint:
                    data = _disk.get("data")
                    _cache_source = "cache"
            except Exception as exc:
                _log.debug("webull_pdf_cache_read_failed", error=str(exc))

        if data is None:
            data = parse_all_pdfs(str(pdf_dir))
            try:
                _cache_path.write_text(
                    json.dumps({"fingerprint": fingerprint, "data": data},
                               indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception as exc:
                _log.debug("webull_pdf_cache_save_failed", error=str(exc))

        holdings = data.get("holdings", [])
        if not holdings:
            print("  [WARN] No Webull holdings found in PDF statements")
            return []

        positions = []
        for h in holdings:
            symbol    = str(h.get("symbol", "")).strip().upper()
            qty       = float(h.get("qty", 0) or 0)
            avg_price = float(h.get("avg_price", 0) or 0)
            currency  = str(h.get("currency", "USD")).upper()
            exchange  = str(h.get("exchange", "")).upper()
            close_p   = float(h.get("close_price", 0) or 0)

            if not symbol or qty <= 0 or avg_price <= 0:
                continue

            # Convert to TradingView symbol (exchange:ticker)
            if currency == "AUD" or exchange in ("ASX", "XASX"):
                tv_sym = f"ASX:{symbol}"
            elif currency in ("HKD", "CNH") or exchange == "HKEX":
                tv_sym = f"HKEX:{symbol}"
            elif currency == "SGD" or exchange == "SGX":
                tv_sym = f"SGX:{symbol}"
            elif exchange in ("NYSE", "XNYS", "NYSEARCA", "CBOE"):
                tv_sym = f"NYSE:{symbol}"
            elif exchange in ("NASDAQ", "NSQ", "NMS"):
                tv_sym = f"NASDAQ:{symbol}"
            else:
                # Default US: check known NYSE tickers, else NASDAQ
                tv_sym = f"NYSE:{symbol}" if symbol in _MOOMOO_NYSE else f"NASDAQ:{symbol}"

            positions.append({
                "symbol":          tv_sym,
                "entry_price":     round(avg_price, 4),
                "entry_date":      None,
                "shares":          int(qty),
                "atr_at_entry":    None,
                "high_water_mark": None,
                "current_price":   round(close_p, 4) if close_p else None,
                "broker":          "Webull",
            })

        print(f"  Fetched {len(positions)} Webull AU positions (PDF statements, {_cache_source})")
        return positions
    except Exception as e:
        print(f"  [WARN] Webull positions fetch failed: {e}")
        return []


def _fetch_webull_positions() -> list[dict]:
    """
    Fetch Webull AU positions from the official OpenAPI. PDF statements are
    an outdated-data fallback and are read only when the API is unavailable.
    """
    api_positions = _fetch_webull_positions_api()
    if api_positions is not None:
        return api_positions
    return _fetch_webull_positions_pdf()


def _combine_broker_positions(moomoo_pos: list[dict], ibkr_pos: list[dict],
                              india_pos: list[dict], webull_pos: list[dict]) -> list[dict]:
    """
    Combine live positions from all broker sources into one list for --live mode.

    IMPORTANT: a symbol held in 2+ brokers (including 2+ Moomoo accounts, e.g.
    the same US stock in both Moomoo-AU and Moomoo-SG) appears as SEPARATE rows.
    Each broker/account's shares, cost basis, entry date, and stop levels are
    genuinely different and must all be analysed independently -- this matches
    how the Global Portfolio Dashboard (main.py option 1) treats multi-broker
    holdings.

    Prior versions of this function silently DROPPED the lower-priority
    broker's entire position for any overlapping symbol (priority order
    Moomoo > IBKR > India > Webull) -- not merged, just gone: that broker's
    shares, cost basis, and exit signal for the symbol were completely invisible
    to the report, with only a one-line console log (never in the HTML). Fixed
    2026-07-06 -- see sell-strategy critique findings #1/#2 (2026-07-06).

    Overlaps are still detected and logged for the user's awareness, but every
    position from every broker is now included in the returned list.
    """
    _mm_syms = [p["symbol"] for p in moomoo_pos]
    _mm_dupe_syms = sorted({s for s in _mm_syms if _mm_syms.count(s) > 1})
    if _mm_dupe_syms:
        print(f"  [INFO] {len(_mm_dupe_syms)} symbol(s) held in multiple Moomoo accounts "
              f"(shown as separate rows): {_mm_dupe_syms}")

    _mm_set = set(_mm_syms)
    ibkr_overlap = sorted({p["symbol"] for p in ibkr_pos} & _mm_set)
    if ibkr_overlap:
        print(f"  [INFO] {len(ibkr_overlap)} symbol(s) held in BOTH Moomoo and IBKR "
              f"(shown as separate rows): {ibkr_overlap}")

    _seen_syms = _mm_set | {p["symbol"] for p in ibkr_pos}
    india_overlap = sorted({p["symbol"] for p in india_pos} & _seen_syms)
    if india_overlap:
        print(f"  [INFO] {len(india_overlap)} symbol(s) held in BOTH India and Moomoo/IBKR "
              f"(shown as separate rows): {india_overlap}")

    _seen_syms = _seen_syms | {p["symbol"] for p in india_pos}
    webull_overlap = sorted({p["symbol"] for p in webull_pos} & _seen_syms)
    if webull_overlap:
        print(f"  [INFO] {len(webull_overlap)} symbol(s) held in BOTH Webull and another "
              f"broker (shown as separate rows): {webull_overlap}")

    return moomoo_pos + ibkr_pos + india_pos + webull_pos


DEMO_POSITIONS = [
    {
        "symbol": "NYSE:RCL",
        "entry_price": 180.50,
        "entry_date": "2026-03-01",
        "shares": 10,
        "atr_at_entry": 4.20,
        "high_water_mark": 195.00
    },
    {
        "symbol": "NASDAQ:AMAT",
        "entry_price": 175.00,
        "entry_date": "2026-03-15",
        "shares": 15,
        "atr_at_entry": 5.10,
        "high_water_mark": 180.00
    },
    {
        "symbol": "ASX:ORI",
        "entry_price": 18.20,
        "entry_date": "2026-03-10",
        "shares": 200,
        "atr_at_entry": 0.45,
        "high_water_mark": 19.10
    },
    {
        "symbol": "NSE:RELIANCE",
        "entry_price": 1280.00,
        "entry_date": "2026-02-20",
        "shares": 5,
        "atr_at_entry": 28.00,
        "high_water_mark": 1350.00
    },
    {
        "symbol": "NASDAQ:TSLA",
        "entry_price": 290.00,
        "entry_date": "2026-01-20",
        "shares": 5,
        "atr_at_entry": 12.50,
        "high_water_mark": 295.00
    },
]

# ─── SellEngine: import pure-logic helpers from signal_engine ────────────────────
sys.path.insert(0, str(ROOT))
from signal_engine.sell_engine import SellEngine  # noqa: E402

try:
    sys.path.insert(0, str(ROOT.parent))
    from position_manager import PositionManager, render_position_mgmt_block
    _PM_AVAILABLE = True
except ImportError:
    _PM_AVAILABLE = False

# Conviction data helpers (from sell_actionable)
try:
    # Package import path (common when running tests / module imports)
    from sell.sell_actionable import (
        fetch_fundamentals,
        fetch_sentiment_data,
        LEVERAGED_ETF_UNDERLYING,
        UNDERLYING_TO_TV,
    )
    _CONVICTION_OK = True
except ImportError:
    try:
        # Script-local fallback path (when running from tradingview/sell/)
        from sell_actionable import (
            fetch_fundamentals,
            fetch_sentiment_data,
            LEVERAGED_ETF_UNDERLYING,
            UNDERLYING_TO_TV,
        )
        _CONVICTION_OK = True
    except ImportError:
        _CONVICTION_OK = False
        LEVERAGED_ETF_UNDERLYING: dict = {}
        UNDERLYING_TO_TV: dict = {}

# TV Screener fundamentals — fallback when yfinance returns 404 (ASX, SGX, ETFs, DLCs)
try:
    from tv_screener_fundamentals import (
        fetch_tv_fundamentals   as _fetch_tv_fund,
        tv_fund_to_compat       as _tv_fund_compat,
    )
    _TV_FUND_FALLBACK_OK = True
except ImportError:
    _TV_FUND_FALLBACK_OK = False

# AI growth prediction (optional — reuses GitHub Models API like signal_stack)
try:
    _sell_github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    from openai import OpenAI as _SellOpenAI
    if _sell_github_token:
        _SELL_AI_CLIENT = _SellOpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=_sell_github_token,
            timeout=18.0,
            max_retries=1,
        )
        _SELL_AI_MODEL = "gpt-4.1-nano"
        _GROWTH_AI_OK  = True
    else:
        _SELL_AI_CLIENT = None
        _SELL_AI_MODEL  = ""
        _GROWTH_AI_OK   = False
except Exception:
    _SELL_AI_CLIENT = None
    _SELL_AI_MODEL  = ""
    _GROWTH_AI_OK   = False

# ─── Helpers ───────────────────────────────────────────────────────────────────
def tv_to_yf(pro_symbol: str) -> str:
    return SellEngine.tv_to_yf(pro_symbol)


def compute_atr(hist: "pd.DataFrame", period: int = ATR_PERIOD) -> float:
    return SellEngine.compute_atr(hist, period)


def compute_rsi(hist: "pd.DataFrame", period: int = 14) -> float:
    return SellEngine.compute_rsi(hist, period)


def compute_adx(hist: "pd.DataFrame", period: int = 14) -> float:
    return SellEngine.compute_adx(hist, period)


# ─── Disk caches ─────────────────────────────────────────────────────────────
_CACHE_DIR = Path(__file__).parent / ".cache"
_CACHE_DIR.mkdir(exist_ok=True)

_PRICE_CACHE_FILE   = _CACHE_DIR / "_price_hist_cache.json"
_VIX_CACHE_FILE     = _CACHE_DIR / "_vix_cache.json"
_SCREENER_CACHE_FILE = _CACHE_DIR / "_screener_cache.json"

_PRICE_CACHE_TTL_SECONDS   = 6 * 3600   # 6 hours
_VIX_SCREENER_CACHE_TTL    = 15 * 60    # 15 minutes


def _load_json_cache(path: Path) -> dict:
    """Load JSON cache file, return {} on any error."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.debug("json_cache_load_failed", path=str(path), error=str(exc))
    return {}


def _save_json_cache(path: Path, data: dict) -> None:
    """Persist cache dict to JSON, silently skip on error."""
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        _log.debug("json_cache_save_failed", path=str(path), error=str(exc))


def _fetch_yf_history(ticker: str, period: str = "3mo") -> "pd.DataFrame":
    """Fetch yfinance history with a 6-hour disk cache.

    Returns the history DataFrame (may be empty on failure).
    The cache key is ``"{ticker}:{period}"``.
    """
    import io as _io2, contextlib as _ctx2, logging as _log2
    key = f"{ticker}:{period}"
    now_ts = datetime.now(timezone.utc).timestamp()
    cache = _load_json_cache(_PRICE_CACHE_FILE)
    entry = cache.get(key)
    if entry and now_ts - entry.get("ts", 0) < _PRICE_CACHE_TTL_SECONDS:
        try:
            records = entry["records"]
            if records:
                df = pd.DataFrame(records)
                df.index = pd.to_datetime(df.index)
                # ensure DatetimeIndex is tz-aware (UTC) so downstream comparisons work
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                return df
        except Exception as exc:
            _log.debug("price_cache_deserialize_failed", key=key, error=str(exc))

    # Suppress noisy yfinance / urllib3 output
    _yfl = _log2.getLogger("yfinance");  _ul  = _log2.getLogger("urllib3")
    _pf,  _pu  = _yfl.level, _ul.level
    _yfl.setLevel(_log2.CRITICAL); _ul.setLevel(_log2.CRITICAL)
    try:
        with _ctx2.redirect_stdout(_io2.StringIO()), _ctx2.redirect_stderr(_io2.StringIO()):
            hist = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    except Exception:
        hist = pd.DataFrame()
    finally:
        _yfl.setLevel(_pf); _ul.setLevel(_pu)

    if not hist.empty:
        try:
            idx = [str(i) for i in hist.index]
            cache[key] = {"ts": now_ts, "records": {k: dict(v) for k, v in zip(idx, hist.to_dict("records"))}}
            _save_json_cache(_PRICE_CACHE_FILE, cache)
        except Exception as exc:
            _log.debug("price_cache_save_failed", error=str(exc))
    return hist


def fetch_tv_screener(pro_symbols: list) -> dict:
    """Batch fetch rel_vol and rec_mark from TV Screener (15-min disk cache)."""
    now_ts  = datetime.now(timezone.utc).timestamp()
    cache   = _load_json_cache(_SCREENER_CACHE_FILE)
    # Cache key: sorted tuple of symbols so order doesn't matter
    cache_key = "technical-v2:" + ",".join(sorted(pro_symbols))
    entry = cache.get(cache_key)
    if entry and now_ts - entry.get("ts", 0) < _VIX_SCREENER_CACHE_TTL:
        return entry.get("data", {})

    by_url = {}
    sym_to_scanner = {}
    sym_to_ticker = {}
    for ps in pro_symbols:
        if ":" not in ps:
            continue
        exch, ticker = ps.split(":", 1)
        url = _TV_SCANNER.get(exch.upper())
        if not url:
            continue
        by_url.setdefault(url, [])
        by_url[url].append(ticker)
        sym_to_scanner[f"{exch.upper()}:{ticker}"] = ps
        sym_to_ticker[(url, ticker)] = ps

    results = {}
    fields = ["name", "relative_volume_10d_calc", "recommendation_mark",
              "price_earnings_ttm", "return_on_equity", "close", "SMA200",
              "ADX", "RSI", "MACD.macd", "MACD.signal",
              "average_volume_10d_calc", "Volatility.D", "beta_1_year",
              "BB.upper", "BB.lower"]

    for url, tickers in by_url.items():
        try:
            payload = {
                "columns": fields,
                "filter": [{"left": "name", "operation": "in_range", "right": tickers}],
                "range": [0, len(tickers) + 5],
            }
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            for item in data.get("data", []):
                raw_sym = item.get("s", "")
                d = item.get("d", [])
                ticker_only = raw_sym.split(":")[-1] if ":" in raw_sym else raw_sym
                ps = (sym_to_scanner.get(raw_sym)
                      or sym_to_ticker.get((url, ticker_only))
                      or raw_sym)
                close = d[5] if len(d) > 5 and d[5] is not None else None
                sma200 = d[6] if len(d) > 6 and d[6] is not None else None
                sma200_pct = None
                if sma200 and close and sma200 > 0:
                    sma200_pct = round((close - sma200) / sma200 * 100, 2)
                macd_value = d[9] if len(d) > 9 and d[9] is not None else None
                macd_signal = d[10] if len(d) > 10 and d[10] is not None else None
                macd_hist = None
                if macd_value is not None and macd_signal is not None:
                    macd_hist = round(macd_value - macd_signal, 4)
                bb_upper = d[14] if len(d) > 14 and d[14] is not None else None
                bb_lower = d[15] if len(d) > 15 and d[15] is not None else None
                bb_width = None
                if bb_upper is not None and bb_lower is not None and close and close > 0:
                    bb_width = round((bb_upper - bb_lower) / close * 100, 2)
                results[ps] = {
                    "rel_vol":    d[1] if len(d) > 1 and d[1] is not None else None,
                    "rec_mark":   d[2] if len(d) > 2 and d[2] is not None else None,
                    "pe":         d[3] if len(d) > 3 and d[3] is not None else None,
                    "roe":        d[4] if len(d) > 4 and d[4] is not None else None,
                    "close":      close,
                    "sma200_pct": sma200_pct,
                    "tv_adx":     d[7] if len(d) > 7 and d[7] is not None else None,
                    "tv_rsi":     d[8] if len(d) > 8 and d[8] is not None else None,
                    "macd_hist":  macd_hist,
                    "avg_vol":    d[11] if len(d) > 11 and d[11] is not None else None,
                    "volatility": d[12] if len(d) > 12 and d[12] is not None else None,
                    "beta":       d[13] if len(d) > 13 and d[13] is not None else None,
                    "bb_width":   bb_width,
                }
        except Exception as exc:
            _log.debug("tv_screener_batch_failed", url=url, error=str(exc))
    if results:
        cache[cache_key] = {"ts": now_ts, "data": results}
        _save_json_cache(_SCREENER_CACHE_FILE, cache)
    return results


def fetch_vix() -> float:
    """Fetch current VIX level (15-min disk cache)."""
    now_ts = datetime.now(timezone.utc).timestamp()
    cache  = _load_json_cache(_VIX_CACHE_FILE)
    if now_ts - cache.get("ts", 0) < _VIX_SCREENER_CACHE_TTL:
        return float(cache.get("vix", 20.0))
    try:
        hist = yf.Ticker("^VIX").history(period="5d")
        if not hist.empty:
            val = round(float(hist["Close"].iloc[-1]), 2)
            _save_json_cache(_VIX_CACHE_FILE, {"ts": now_ts, "vix": val})
            return val
    except Exception as exc:
        _log.debug("vix_history_fetch_failed", error=str(exc))
    try:
        info = yf.Ticker("^VIX").fast_info
        val  = round(float(info["lastPrice"]), 2)
        _save_json_cache(_VIX_CACHE_FILE, {"ts": now_ts, "vix": val})
        return val
    except Exception as exc:
        _log.debug("vix_fast_info_fetch_failed", error=str(exc))
    return 20.0


def trading_days_between(start_date: str, end_date: datetime) -> int:
    return SellEngine.trading_days_between(start_date, end_date)


def fmt_price(p, currency_symbol=""):
    return SellEngine.fmt_price(p, currency_symbol)


def fmt_pct(v):
    return SellEngine.fmt_pct(v)


# ─── AI Growth Prediction ──────────────────────────────────────────────────────
_AI_GROWTH_SYSTEM_PROMPT = """\
You are a portfolio growth analyst. Given a held equity position's fundamentals and technicals, \
predict future price performance.

Return ONLY valid JSON (no prose, no explanation):
{
  "outlook_6m": "BULLISH|NEUTRAL|BEARISH",
  "outlook_12m": "BULLISH|NEUTRAL|BEARISH",
  "growth_est_6m": <float, expected % return over next 6 months, e.g. 12.5 or -3.0>,
  "growth_est_12m": <float, expected % return over next 12 months>,
  "hold_rec": "HOLD_FOR_GROWTH|MONITOR_CLOSELY|EXIT_NOW",
  "reason": "<max 20 words>"
}"""


def _compute_holding_period(entry_date_str: str | None, now: datetime) -> dict:
    """Pure function: compute holding period fields from entry_date string and current datetime.

    Returns dict with: held_days, cgt_eligible, holding_term, days_to_lt.
    holding_term values: LONG_TERM (>=365d), MED_TERM (>=180d), SHORT_TERM (>=90d), RECENT (<90d).
    CGT 50% discount (Australian) applies at >= 365 days held.
    """
    if not entry_date_str:
        return {"held_days": None, "cgt_eligible": False,
                "holding_term": "UNKNOWN", "days_to_lt": None}
    try:
        _entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d")
        _held     = max((now.date() - _entry_dt.date()).days, 0)
        _cgt_ok   = _held >= 365
        if _held >= 365:
            _term = "LONG_TERM"
        elif _held >= 180:
            _term = "MED_TERM"
        elif _held >= 90:
            _term = "SHORT_TERM"
        else:
            _term = "RECENT"
        return {
            "held_days":    _held,
            "cgt_eligible": _cgt_ok,
            "holding_term": _term,
            "days_to_lt":   max(365 - _held, 0),
        }
    except (ValueError, TypeError):
        return {"held_days": None, "cgt_eligible": False,
                "holding_term": "UNKNOWN", "days_to_lt": None}


def _call_ai_growth(result: dict, fund: dict, vix: float) -> dict:
    """Ask AI to predict 6m/12m growth outlook for a held position.

    Builds a prompt from the position's fundamentals, technicals, and holding
    period, then calls the GitHub Models API (gpt-4.1-nano) for a structured
    JSON prediction.  Returns {} on any failure or when AI is unavailable.
    """
    if not _GROWTH_AI_OK or not _SELL_AI_CLIENT:
        return {}

    symbol = result.get("symbol", "?")
    held_d = result.get("held_days") or 0
    term   = result.get("holding_term", "UNKNOWN")

    # current_gain_pct may not be set yet (populated later by SellEngine)
    gain_pct = result.get("current_gain_pct")
    if gain_pct is None:
        _cp = result.get("current_price")
        _ep = result.get("entry_price")
        gain_pct = round((_cp / _ep - 1) * 100, 2) if (_cp and _ep) else 0.0
    else:
        gain_pct = float(gain_pct)

    rsi = result.get("rsi")
    adx = result.get("adx")
    eg  = fund.get("earnings_growth")
    rg  = fund.get("revenue_growth")
    roe = fund.get("roe")
    de  = fund.get("debt_to_equity")
    fpe = fund.get("forward_pe")
    peg = fund.get("peg_ratio")
    at  = fund.get("analyst_target")
    cp  = result.get("current_price")
    upside = round((at / cp - 1) * 100, 1) if (at and cp) else None

    def _fmt(v, mult=1, suffix="", fmtspec=".1f"):
        return f"{v * mult:{fmtspec}}{suffix}" if v is not None else "N/A"

    user_text = (
        f"Symbol: {symbol}\n"
        f"Held: {held_d}d ({term})\n"
        f"Current gain: {gain_pct:+.1f}%\n"
        f"RSI: {rsi or 'N/A'}  ADX: {adx or 'N/A'}  VIX: {vix:.1f}\n"
        f"Earnings growth: {_fmt(eg, 100, '%')}\n"
        f"Revenue growth:  {_fmt(rg, 100, '%')}\n"
        f"ROE: {_fmt(roe, suffix='%')}  D/E: {_fmt(de)}\n"
        f"Forward PE: {_fmt(fpe)}  PEG: {_fmt(peg)}\n"
        f"Analyst upside: {f'{upside:+.1f}%' if upside is not None else 'N/A'}\n"
    )

    import time as _time
    _t0 = _time.monotonic()
    try:
        resp   = _SELL_AI_CLIENT.chat.completions.create(
            model=_SELL_AI_MODEL,
            messages=[
                {"role": "system", "content": _AI_GROWTH_SYSTEM_PROMPT},
                {"role": "user",   "content": user_text},
            ],
            max_tokens=100,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw    = resp.choices[0].message.content or ""
        parsed = json.loads(raw)
        parsed["latency_s"] = round(_time.monotonic() - _t0, 2)
        parsed["model"]     = resp.model
        return parsed
    except Exception:
        return {}


# ─── Hedging Recommendations ────────────────────────────────────────────────────
# Sector → inverse/hedge ETF mapping (US-listed unless noted)
_SECTOR_HEDGE_MAP: dict[str, dict] = {
    "Technology":              {"ticker": "SQQQ",  "name": "UltraPro Short QQQ (-3×)"},
    "Communication Services":  {"ticker": "SQQQ",  "name": "UltraPro Short QQQ (-3×)"},
    "Consumer Cyclical":       {"ticker": "SDS",   "name": "UltraShort S&P500 (-2×)"},
    "Consumer Defensive":      {"ticker": "SH",    "name": "Short S&P500 (-1×)"},
    "Financial Services":      {"ticker": "FAZ",   "name": "Direxion Financial Bear 3×"},
    "Healthcare":              {"ticker": "RXD",   "name": "UltraShort Health Care (-2×)"},
    "Energy":                  {"ticker": "DUG",   "name": "UltraShort Oil & Gas (-2×)"},
    "Industrials":             {"ticker": "SDS",   "name": "UltraShort S&P500 (-2×)"},
    "Basic Materials":         {"ticker": "GLD",   "name": "SPDR Gold (safe haven)"},
    "Real Estate":             {"ticker": "SRS",   "name": "UltraShort Real Estate (-2×)"},
    "Utilities":               {"ticker": "SH",    "name": "Short S&P500 (-1×)"},
    # Region fallbacks (used when broker or yf suffix hints at non-US)
    "_AU":                     {"ticker": "BBOZ",  "name": "BetaShares Aus Bear (-2×, ASX)"},
    "_IN":                     {"ticker": "NIFTYBEES", "name": "Nifty puts / consult broker"},
    "_default":                {"ticker": "SH",    "name": "Short S&P500 (-1×)"},
}

# VIX-based portfolio hedge suggestion (applies per-report, not per-position)
_VIX_PORTFOLIO_HEDGE: dict[str, dict] = {
    "EXTREME_FEAR": {"ticker": "VIXY", "name": "VIX Short-Term Futures ETF", "sizing_pct": 5},
    "FEAR":         {"ticker": "GLD",  "name": "SPDR Gold Shares",           "sizing_pct": 3},
    "NORMAL":       {},
}


def _compute_hedge_recommendation(result: dict, vix: float) -> dict:
    """Compute a per-position hedging recommendation (pure logic, no network).

    Returns a dict with fields:
      hedge_urgency    — "NONE" | "CONSIDER" | "RECOMMEND" | "URGENT"
      hedge_vehicle    — {"ticker", "name"} or None
      hedge_sizing_pct — % of position value to allocate to hedge (0 if not needed)
      hedge_triggers   — list[str] of human-readable trigger labels
    """
    action        = result.get("action", "HOLD")
    gain_pct      = result.get("current_gain_pct") or 0.0
    vix_regime    = result.get("vix_regime", "NORMAL")
    tp1_hit       = result.get("tp1_hit", False)
    decay_count   = result.get("decay_count", 0) or 0
    conv_score    = result.get("conviction_score")          # may be None
    cgt_eligible  = result.get("cgt_eligible", False)
    broker        = result.get("broker", "")
    sector        = result.get("sector", "") or ""

    # ── Determine hedge vehicle by sector / region ───────────────────────────
    if broker in ("AngelOne", "Zerodha"):
        vehicle = _SECTOR_HEDGE_MAP["_IN"]
    elif broker.startswith("Moomoo-AU") and sector not in _SECTOR_HEDGE_MAP:
        # Moomoo-SG deliberately falls through to _default: no single regional
        # inverse ETF covers the mixed SGX/TYO/KRX exposure that account holds.
        vehicle = _SECTOR_HEDGE_MAP["_AU"]
    else:
        vehicle = _SECTOR_HEDGE_MAP.get(sector, _SECTOR_HEDGE_MAP["_default"])

    # ── Evaluate triggers (highest urgency wins) ─────────────────────────────
    triggers:  list[str] = []
    sizing_pct: float    = 0.0
    urgency:    str      = "NONE"

    # T1: Extreme fear + big unrealised gain → urgent protect
    if vix_regime == "EXTREME_FEAR" and gain_pct >= 10.0:
        triggers.append(f"VIX EXTREME FEAR ({vix:.1f}) + gain {gain_pct:+.1f}%")
        sizing_pct = max(sizing_pct, 40.0)
        urgency    = "URGENT"

    # T2: Fear regime + TP1 hit (meaningful gain locked in)
    if vix_regime == "FEAR" and tp1_hit:
        triggers.append(f"VIX FEAR ({vix:.1f}) + TP1 hit")
        sizing_pct = max(sizing_pct, 25.0)
        urgency    = "URGENT" if urgency == "URGENT" else "RECOMMEND"

    # T3: Low conviction + significant gain (risk of mean-reversion)
    if conv_score is not None and conv_score < 4.0 and gain_pct >= 15.0:
        triggers.append(f"Low conviction ({conv_score:.1f}/10) + large gain {gain_pct:+.1f}%")
        sizing_pct = max(sizing_pct, 30.0)
        urgency    = "URGENT" if urgency == "URGENT" else "RECOMMEND"

    # T4: Signal decay + large gain
    if decay_count >= 2 and gain_pct >= 20.0:
        triggers.append(f"{decay_count}/{SellEngine.DECAY_FACTOR_COUNT} decay flags + gain {gain_pct:+.1f}%")
        sizing_pct = max(sizing_pct, 20.0)
        urgency    = "URGENT" if urgency == "URGENT" else "RECOMMEND"

    # T5: CGT-eligible + very large gain → protect before potential policy change
    if cgt_eligible and gain_pct >= 30.0 and vix >= 20.0:
        triggers.append(f"CGT eligible + {gain_pct:+.1f}% gain + VIX {vix:.1f}")
        sizing_pct = max(sizing_pct, 15.0)
        if urgency == "NONE":
            urgency = "CONSIDER"

    # T6: Action is REDUCE/SELL but position has gain → hedge remainder
    if action in ("REDUCE",) and gain_pct >= 10.0:
        triggers.append(f"REDUCE action + remaining position at {gain_pct:+.1f}%")
        sizing_pct = max(sizing_pct, 15.0)
        if urgency == "NONE":
            urgency = "CONSIDER"

    return {
        "hedge_urgency":    urgency,
        "hedge_vehicle":    vehicle if urgency != "NONE" else None,
        "hedge_sizing_pct": round(sizing_pct, 0),
        "hedge_triggers":   triggers,
    }


def build_portfolio_hedge_banner(results: list, vix: float) -> str:
    """Banner showing portfolio-level VIX hedge + per-position hedge detail table."""
    vix_regime = ("EXTREME_FEAR" if vix >= 35 else "FEAR" if vix >= 25 else "NORMAL")
    portfolio_hedge = _VIX_PORTFOLIO_HEDGE.get(vix_regime, {})

    hedged   = [r for r in results if r.get("hedge_urgency", "NONE") != "NONE"]
    no_hedge = len(results) - len(hedged)

    urgent_count    = sum(1 for r in hedged if r.get("hedge_urgency") == "URGENT")
    recommend_count = sum(1 for r in hedged if r.get("hedge_urgency") == "RECOMMEND")
    consider_count  = sum(1 for r in hedged if r.get("hedge_urgency") == "CONSIDER")

    total_hedge = len(hedged)
    # Always render the hedge section so users can see the full picture.
    # When no triggers fire we show a "All Clear" green panel instead of hiding.

    _cols = {"URGENT": "#e63946", "RECOMMEND": "#f9c74f", "CONSIDER": "#ff9f1c"}
    counts_html = ""
    for label, count in (("URGENT", urgent_count), ("RECOMMEND", recommend_count),
                         ("CONSIDER", consider_count)):
        if count:
            c = _cols[label]
            counts_html += (
                f'<span style="background:{c}22;color:{c};padding:3px 10px;'
                f'border-radius:8px;font-size:0.82rem;font-weight:700">'
                f'&#x1F6E1; {label}: {count}</span> '
            )

    ph_html = ""
    if portfolio_hedge:
        t   = portfolio_hedge.get("ticker", "")
        nm  = portfolio_hedge.get("name", "")
        sz  = portfolio_hedge.get("sizing_pct", 0)
        col = "#e63946" if vix_regime == "EXTREME_FEAR" else "#f9c74f"
        ph_html = (
            f'<span style="color:{col};font-weight:700">&#x1F4CA; Portfolio hedge:</span> '
            f'<code style="background:{col}22;color:{col};padding:2px 6px;border-radius:4px">'
            f'{t}</code> {nm} — allocate ~{sz}% of cash'
        )

    # ── Per-position hedge detail table ────────────────────────────────────────
    _urgency_order = {"URGENT": 0, "RECOMMEND": 1, "CONSIDER": 2}
    hedged_sorted = sorted(hedged, key=lambda r: _urgency_order.get(r.get("hedge_urgency", ""), 9))
    detail_rows = ""
    for r in hedged_sorted:
        hu  = r.get("hedge_urgency", "NONE")
        hv  = r.get("hedge_vehicle") or {}
        hsz = r.get("hedge_sizing_pct", 0)
        htg = r.get("hedge_triggers") or []
        hc  = _cols.get(hu, "#888")
        sym = r.get("symbol", "?")
        brk = r.get("broker", "")
        gpct = r.get("current_gain_pct", 0)
        gpct_col = "#2ecc71" if gpct >= 0 else "#e74c3c"
        hticker = hv.get("ticker", "")
        hname   = hv.get("name", "")
        triggers_str = "; ".join(htg[:3]) if htg else "—"
        detail_rows += (
            f"<tr>"
            f"<td style='padding:5px 8px'>"
            f"<span style='background:{hc}33;color:{hc};padding:2px 8px;"
            f"border-radius:4px;font-size:0.78rem;font-weight:700'>{hu}</span></td>"
            f"<td style='padding:5px 8px'><strong style='color:#fff'>{sym}</strong></td>"
            f"<td style='color:#888;font-size:0.82rem;padding:5px 8px'>{brk}</td>"
            f"<td style='padding:5px 8px'>"
            f"<code style='background:#1a2a3a;color:#7dd3fc;padding:2px 6px;"
            f"border-radius:3px;font-size:0.82rem'>{hticker}</code>"
            f" <span style='color:#888;font-size:0.78rem'>{hname}</span></td>"
            f"<td style='color:#aaa;font-size:0.82rem;padding:5px 8px;text-align:center'>"
            f"~{int(hsz)}%</td>"
            f"<td style='color:{gpct_col};text-align:right;font-family:monospace;"
            f"padding:5px 8px'>{gpct:+.1f}%</td>"
            f"<td style='color:#888;font-size:0.78rem;padding:5px 8px;max-width:280px'>"
            f"{triggers_str}</td>"
            f"</tr>\n"
        )

    detail_table = ""
    if detail_rows:
        detail_table = (
            f'<table style="width:100%;border-collapse:collapse;margin-top:10px">'
            f'<tr style="color:#555;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.5px">'
            f'<th scope="col" style="padding:4px 8px;text-align:left">Priority</th>'
            f'<th scope="col" style="padding:4px 8px;text-align:left">Symbol</th>'
            f'<th scope="col" style="padding:4px 8px;text-align:left">Broker</th>'
            f'<th scope="col" style="padding:4px 8px;text-align:left">Hedge Vehicle (Buy)</th>'
            f'<th scope="col" style="padding:4px 8px;text-align:center">Sizing</th>'
            f'<th scope="col" style="padding:4px 8px;text-align:right">PnL %</th>'
            f'<th scope="col" style="padding:4px 8px;text-align:left">Trigger Reason</th>'
            f'</tr>'
            f'{detail_rows}'
            f'</table>'
        )

    # ── No-hedge explanation ───────────────────────────────────────────────────
    no_hedge_note = ""
    if no_hedge > 0:
        no_hedge_note = (
            f'<div style="color:#555;font-size:0.78rem;margin-top:8px;font-style:italic">'
            f'&#x2705; {no_hedge} position{"s" if no_hedge != 1 else ""} require no hedging '
            f'(VIX {vix_regime.replace("_"," ").lower()}, no gain/decay/conviction triggers met)</div>'
        )

    border_col = (
        "#1db954" if total_hedge == 0 and not portfolio_hedge
        else "#e63946" if urgent_count
        else "#f9c74f" if recommend_count
        else "#ff9f1c"
    )
    section_title = (
        "&#x2705; HEDGE STATUS &#x2014; All Clear"
        if total_hedge == 0 and not portfolio_hedge
        else f"&#x1F6E1; HEDGE ALERTS &#x2014; {total_hedge} Position{'s' if total_hedge != 1 else ''} Need Hedging"
    )
    return (
        f'<div style="background:#1a0a0a;border:2px solid {border_col};border-radius:10px;'
        f'padding:14px 20px;margin-bottom:16px">'
        f'<div style="color:{border_col};font-size:0.9rem;font-weight:700;margin-bottom:6px">'
        f'{section_title}</div>'
        f'<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center">'
        f'{counts_html}'
        f'{"&nbsp;&nbsp;" + ph_html if ph_html else ""}'
        f'</div>'
        f'{detail_table}'
        f'{no_hedge_note}'
        f'</div>'
    )


def _render_momentum_row(pct: float | None) -> str:
    """Return an HTML table row showing 5-day price momentum.

    Args:
        pct: Fraction e.g. 0.05 = +5%.  None → N/A row.

    Used by sell-strategy card template; also tested directly.
    """
    if pct is None:
        return ('<tr><td>5d Momentum</td>'
                '<td style="color:#888">N/A</td></tr>')
    sign   = "+" if pct >= 0 else ""
    colour = "#1db954" if pct >= 0 else "#e63946"
    label  = f"{sign}{pct * 100:.1f}%"
    return (f'<tr><td>5d Momentum</td>'
            f'<td style="color:{colour}">{label}</td></tr>')


# ─── Core Analysis ─────────────────────────────────────────────────────────────
def analyse_position(pos: dict, screener: dict, vix: float, now: datetime) -> dict:
    """
    Run all 8 exit rules for a single position.
    Returns a rich result dict for HTML rendering.
    """
    symbol      = pos.get("symbol") or ""
    _raw_ep     = pos.get("entry_price")
    entry_price = float(_raw_ep) if _raw_ep is not None else 0.0
    entry_date  = pos.get("entry_date")
    shares      = int(pos.get("shares", 1))
    _raw_atr    = pos.get("atr_at_entry")
    atr_entry   = float(_raw_atr) if _raw_atr is not None else None  # may be None → computed later
    _raw_hwm    = pos.get("high_water_mark")
    hwm         = float(_raw_hwm) if _raw_hwm is not None else entry_price  # default to entry

    yf_ticker = tv_to_yf(symbol)
    result = {
        "symbol": symbol,
        "entry_price": entry_price,
        "entry_date": entry_date,
        "shares": shares,
        "atr_entry": atr_entry,
        "high_water_mark": hwm,
        "yf_ticker": yf_ticker,
        "broker": pos.get("broker", ""),
        "current_price": None,
        "atr_current": None,
        "rsi": None,
        "adx": None,
        "rel_vol": None,
        "rec_mark": None,
        "pe": None,
        "roe": None,
        "errors": [],
        "sector": "",
        # Holding period (populated immediately below by _compute_holding_period)
        "held_days":    None,
        "cgt_eligible": False,
        "holding_term": "UNKNOWN",
        "days_to_lt":   None,
        "growth_ai":    {},
        # 5-day price momentum (None when history < 6 bars)
        "price_change_5d": None,
        # Propagate avg_cost reliability flag from broker data fetch
        # (True when avg_cost=0 from OpenD — e.g. leveraged ETFs, DLCs)
        # sell_engine.py reads this flag to suppress R-multiple-based exit rules
        "avg_cost_unreliable": pos.get("avg_cost_unreliable", False),
    }

    # ── Holding period classification (pure logic — no network) ───────────────
    result.update(_compute_holding_period(entry_date, now))

    # ── Guard: entry_price missing → cannot compute P&L or R-multiple ─────────
    if _raw_ep is None:
        result["errors"].append(
            f"entry_price missing for {symbol} — P&L and R-multiple calculations skipped"
        )
        result["action"]       = "REVIEW"
        result["action_label"] = "REVIEW — entry_price missing"
        return result

    # ── Delisted ticker check (P2-B) ──────────────────────────────────────────
    if _is_delisted(symbol):
        result["errors"].append(f"{symbol} flagged as delisted — skipping yfinance fetch")
        result["action"] = "REVIEW"
        result["action_label"] = "REVIEW — delisted ticker"
        # Still try TV Screener for current price (may work for some)
        sc = screener.get(symbol, {})
        if sc.get("close"):
            result["current_price"] = float(sc["close"])
        return result

    # ── Fetch market data ──────────────────────────────────────────────────────
    import logging as _logging
    import io as _io, contextlib as _ctxlib
    _yf_log = _logging.getLogger("yfinance")
    _ul_log = _logging.getLogger("urllib3")
    _prev_yf, _prev_ul = _yf_log.level, _ul_log.level
    _yf_log.setLevel(_logging.CRITICAL)
    _ul_log.setLevel(_logging.CRITICAL)
    try:
        with _ctxlib.redirect_stdout(_io.StringIO()), _ctxlib.redirect_stderr(_io.StringIO()):
            hist = _fetch_yf_history(yf_ticker, period="1y")
        if hist.empty:
            # Structured products (DLCs, warrants) and some exchange-listed
            # instruments are absent from yfinance.  Don't return early —
            # fall through so the TV Screener block below can supply the
            # current price from TradingView's live feed.
            result["errors"].append(
                "No price history from yfinance (structured product / DLC?)"
                " — using TV Screener price if available"
            )
            # For leveraged ETFs / DLCs: try to compute ATR from the underlying's
            # price history so stop-loss / TP levels are meaningful.
            # e.g. SGX:Z8TW (AVGO 3x DLC) → fetch AVGO ATR from yfinance.
            # ATR is scaled as underlying_atr_pct × DLC_entry_price so the stop
            # levels remain in the DLC's own price range/currency.
            # NOTE: leverage multiplier (3x) is not applied here — actual DLC
            # volatility is ~3x the underlying %, so stops may be tighter than ideal.
            _raw_dlc_ticker = symbol.split(":", 1)[-1]
            _underlying_for_atr = LEVERAGED_ETF_UNDERLYING.get(_raw_dlc_ticker) if _CONVICTION_OK else None
            if _underlying_for_atr:
                try:
                    with _ctxlib.redirect_stdout(_io.StringIO()), _ctxlib.redirect_stderr(_io.StringIO()):
                        _underlying_hist = _fetch_yf_history(_underlying_for_atr, period="1y")
                    if not _underlying_hist.empty:
                        _ul_atr   = compute_atr(_underlying_hist)
                        _ul_price = float(_underlying_hist["Close"].iloc[-1])
                        if _ul_price > 0:
                            # Scale ATR to DLC price range: underlying_atr_pct × DLC_entry_price
                            result["atr_current"] = round((_ul_atr / _ul_price) * entry_price, 4)
                        result["rsi"] = compute_rsi(_underlying_hist)
                        result["adx"] = compute_adx(_underlying_hist)
                        if result["adx"] is not None:
                            result["adx"] = min(result["adx"], 100.0)  # cap: ADX is always 0–100
                except Exception as exc:
                    _log.debug("dlc_underlying_atr_failed", symbol=symbol, error=str(exc))
        else:
            current_price = float(hist["Close"].iloc[-1])
            result["current_price"] = round(current_price, 4)
            result["_yf_ok"] = True
            result["high_water_mark"] = max(hwm, current_price)  # update HWM
            try:
                if entry_date:
                    # Only scan history from purchase date onwards — pre-purchase highs
                    # must NOT inflate HWM (would falsely trigger trailing stop).
                    entry_dt   = datetime.strptime(entry_date, "%Y-%m-%d")
                    hist_slice = hist[hist.index >= pd.Timestamp(entry_dt, tz=hist.index.tz)]
                    if not hist_slice.empty:
                        historical_high = float(hist_slice["High"].max())
                        result["high_water_mark"] = max(hwm, historical_high, current_price)
                # When entry_date is None (live Moomoo — no buy date available):
                # HWM stays at max(entry_price, current_price) set above.
                # Using 3-month pre-purchase history would inflate HWM and immediately
                # trigger the trailing stop on freshly bought positions (e.g. CSCL bug).
            except Exception as exc:
                _log.debug("hwm_calc_failed", symbol=symbol, error=str(exc))

            result["atr_current"] = compute_atr(hist)
            result["rsi"]         = compute_rsi(hist)
            result["adx"]         = compute_adx(hist)
            if result["adx"] is not None:
                result["adx"] = min(result["adx"], 100.0)  # cap: ADX is always 0–100

            # ── Book-sourced decay signals (Darvas box, VPA anomaly) ──────────────
            # darvas_score / vpa_score are read by sell_engine.apply_exit_rules()
            # to feed into the signal-decay counter (Rule 6).
            try:
                from analysis.book_features_scorer import score_book_features as _sbf
                if len(hist) >= 20:
                    _bf = _sbf(hist.tail(100), fund_dict=None, rs_value=None,
                               flags={"disable_gap": True, "disable_am": True, "disable_rs": True})
                    result["darvas_score"]  = _bf.get("darvas", {}).get("score", 0)
                    result["vpa_score"]     = _bf.get("vpa", {}).get("score", 0)
                    result["book_features"] = _bf
            except Exception:
                pass  # book features are optional — never block sell scoring
            try:
                if len(hist) >= 6:
                    result["price_change_5d"] = round(
                        float(hist["Close"].iloc[-1]) / float(hist["Close"].iloc[-6]) - 1, 4
                    )
                if len(hist) >= 2:
                    result["price_change_1d"] = round(
                        float(hist["Close"].iloc[-1]) / float(hist["Close"].iloc[-2]) - 1, 4
                    )
                if len(hist) >= 15:
                    result["price_change_14d"] = round(
                        float(hist["Close"].iloc[-1]) / float(hist["Close"].iloc[-15]) - 1, 4
                    )
                if len(hist) >= 20:
                    ema20 = float(hist["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
                    result["above_ema20"] = float(hist["Close"].iloc[-1]) > ema20
                # else: stays None (short history — not enough bars)
            except Exception as exc:
                _log.debug("price_change_calc_failed", symbol=symbol, error=str(exc))

            # ── Extract price history for LWC chart ────────────────────────────────
            # Use the FULL 1-year history so the chart fills its width.
            # The entry_date arrowUp marker (added in _render_lwc_chart) shows
            # where the position was opened — no need to slice from that date.
            try:
                _hist_chart = hist  # full 1-year window
                if not _hist_chart.empty:
                    result["price_history"] = [
                        {"time": str(_idx.date()), "value": round(float(_cl), 6)}
                        for _idx, _cl in zip(_hist_chart.index, _hist_chart["Close"])
                        if not pd.isna(_cl)
                    ]
            except Exception as exc:
                _log.debug("price_history_extract_failed", symbol=symbol, error=str(exc))

    except Exception as e:
        result["errors"].append(f"yfinance fetch error: {e}")
        _fe = _finalise(result, vix, now, conviction_data=None)
        _fe.update(_compute_hedge_recommendation(_fe, vix))
        return _fe
    finally:
        _yf_log.setLevel(_prev_yf)
        _ul_log.setLevel(_prev_ul)

    # ── TV Screener data (live intraday prices override yfinance daily close) ──
    sc = screener.get(symbol, {})
    tv_close = sc.get("close")
    if tv_close is not None:
        # TV Screener 'close' is updated in real-time during market hours
        current_price = float(tv_close)
        result["current_price"] = round(current_price, 4)
        result["high_water_mark"] = max(result["high_water_mark"], current_price)
    result["rel_vol"]  = sc.get("rel_vol")
    result["rec_mark"] = sc.get("rec_mark")
    result["pe"]       = sc.get("pe")

    # ── Broker price fallback ────────────────────────────────────────────────
    # If neither yfinance nor TV Screener returned a price, fall back to the
    # broker-supplied snapshot so DLCs/structured products are not left at None.
    if result["current_price"] is None:
        _broker_price = pos.get("current_price")
        if _broker_price is not None and float(_broker_price) > 0:
            result["current_price"] = round(float(_broker_price), 4)
            result["high_water_mark"] = max(result["high_water_mark"], result["current_price"])
            result["errors"].append(
                f"Using broker snapshot price {result['current_price']} "
                "(yfinance + TV Screener unavailable)"
            )

    # ── Delisted auto-detection (P2-B) ────────────────────────────────────────
    # If yfinance returned empty AND TV Screener has no close price AND broker
    # had no price either, the symbol is likely delisted.
    if result["current_price"] is None and not result.get("_yf_ok"):
        _mark_delisted(symbol)
        result["errors"].append(
            f"[WARN] {symbol} may be delisted — no price from yfinance, TV Screener, or broker"
        )
    # TV Screener returns return_on_equity already in percentage form
    # (e.g. 66.76 means 66.76%).  Do NOT multiply by 100 again.
    _raw_roe = sc.get("roe")
    result["roe"] = round(_raw_roe, 2) if _raw_roe is not None else None

    # ── Build conviction data for multi-factor scoring ─────────────────────────
    conviction_data = None
    if _CONVICTION_OK:
        try:
            # For single-stock leveraged ETFs, fetch fundamentals from the underlying equity
            # (e.g. PTIR→PLTR, AMDL→AMD, GGLL→GOOGL) — ETF itself has no fundamentals on yfinance
            raw_ticker  = symbol.split(":", 1)[-1]
            underlying  = LEVERAGED_ETF_UNDERLYING.get(raw_ticker)
            fund_ticker = underlying if underlying else yf_ticker  # e.g. "PLTR" instead of "PTIR"
            sent_obj    = yf.Ticker(fund_ticker)

            # Resolve TV pro_symbol for the fundamentals ticker
            # (needed for TV fallback; underlying may map to a different exchange)
            try:
                from sell.sell_actionable import UNDERLYING_TO_TV
            except ImportError:
                from sell_actionable import UNDERLYING_TO_TV
            if underlying:
                _fund_tv_sym = UNDERLYING_TO_TV.get(underlying, symbol)
            else:
                _fund_tv_sym = symbol

            fund = fetch_fundamentals(fund_ticker)

            # Fallback: if yfinance 404'd, try TV Screener fundamentals
            # (common for ASX, SGX, ETFs, DLCs, warrants, Japan ETFs)
            if "error" in fund and _TV_FUND_FALLBACK_OK:
                tv_raw = _fetch_tv_fund([_fund_tv_sym], use_auth=False)
                tv_data = tv_raw.get(_fund_tv_sym, {})
                if tv_data:
                    fund = _tv_fund_compat(tv_data, pro_symbol=_fund_tv_sym)
                    fund.pop("error", None)  # clear the error — TV data succeeded

            if "error" not in fund:
                company = fund.get("company") or fund.get("company_name") or fund_ticker
                result["sector"] = fund.get("sector", "") or ""
                if underlying:
                    fund["_underlying_for"] = underlying  # tag for downstream display
                    # For leveraged ETFs, ALWAYS use the underlying's PE/ROE for display.
                    # The ETF's own PE (e.g. PTIR/METU have no meaningful PE) should never
                    # be used — overwrite whatever TV Screener returned for the ETF symbol.
                    underlying_pe = fund.get("forward_pe") or fund.get("pe")
                    underlying_roe = fund.get("roe")
                    if underlying_pe is not None:
                        result["pe"] = underlying_pe
                    if underlying_roe is not None:
                        # fund.roe already in % (pct() * 100) — matches normalized TV Screener ROE
                        result["roe"] = underlying_roe
                sent = fetch_sentiment_data(sent_obj, company, symbol)
                conviction_data = {
                    "fundamental": {
                        "earnings_growth":  fund.get("earnings_growth"),
                        "revenue_growth":   fund.get("revenue_growth"),
                        "profit_margin":    fund.get("profit_margin"),
                        "roe":              fund.get("roe"),
                        "debt_to_equity":   fund.get("debt_to_equity"),
                        "free_cashflow":    fund.get("free_cashflow"),
                        "analyst_target":   fund.get("analyst_target"),
                        "current_price":    result.get("current_price"),
                    },
                    "technical": {
                        "rsi":          result.get("rsi"),
                        "adx":          result.get("adx"),
                        "macd_bullish": sc.get("rec_mark") in ("buy", "strong_buy"),
                        "ema_bullish":  None,  # to be set below
                    },
                    "sentiment": {
                        "news_score":        sent.get("news", {}).get("compound", 0),
                        "analyst_bull":      sent.get("analyst", {}).get("bull_ratio", 0.5),
                        "recent_downgrade":  sent.get("recent_downgrade", False),
                        "earnings_beat_rate": sent.get("earnings_beats", {}).get("beat_rate", 0.5),
                    },
                    "macro": {
                        "vix": vix,
                        "yield_curve_inv": False,  # placeholder
                    },
                    "valuation": {
                        "forward_pe": fund.get("forward_pe"),
                        "peg_ratio":  fund.get("peg_ratio"),
                    },
                    "earnings_days_away": sent.get("earnings_days_away"),
                }
            # AI growth prediction (reuses fund data already fetched)
            if _GROWTH_AI_OK:
                try:
                    result["growth_ai"] = _call_ai_growth(result, fund, vix)
                except Exception as exc:
                    _log.debug("ai_growth_prediction_failed", symbol=symbol, error=str(exc))
        except Exception as exc:
            _log.debug("conviction_scoring_failed", symbol=symbol, error=str(exc))

    finalised = _finalise(result, vix, now, conviction_data=conviction_data)
    finalised.update(_compute_hedge_recommendation(finalised, vix))
    return finalised


def _finalise(result: dict, vix: float, now: datetime,
              conviction_data: dict | None = None) -> dict:
    """Apply all 8 exit rules + conviction-aware position management."""
    result = SellEngine.apply_exit_rules(result, vix, now)
    if _PM_AVAILABLE:
        result = PositionManager.evaluate(result, conviction_data=conviction_data)

    # ── Post-conviction SELL softener ──────────────────────────────────────────
    # After PositionManager sets conviction_score (0–10), if a SELL was triggered
    # by a stop on a very high-conviction position still in profit, downgrade to
    # REVIEW.  The high conviction (≥ 7.5) means multiple factors (fundamentals,
    # technical, sentiment) agree the stock is strong — a stop breach is likely
    # noise, not thesis failure.
    if result.get("action") == "SELL":
        conv  = float(result.get("conviction_score") or 0)
        gain  = float(result.get("current_gain_pct") or 0)   # stored as % value e.g. 65.2
        if conv >= 7.5 and gain > 10:
            old_reason = result.get("action_reason", "Stop hit")
            result["action"] = "REVIEW"
            result["action_reason"] = (
                f"High-conviction position (conviction {conv:.1f}/10) stopped out "
                f"with +{gain:.1f}% gain — review thesis before executing exit. "
                f"Original trigger: {old_reason}"
            )
            # Re-run so scaling/profit/quality fields describe REVIEW, not the
            # superseded SELL, which otherwise contradicts the card's action.
            if _PM_AVAILABLE:
                _softened_reason = result["action_reason"]
                result = PositionManager.evaluate(result, conviction_data=conviction_data)
                result["action"] = "REVIEW"
                result["action_reason"] = _softened_reason
    return result


# ─── HTML Report ───────────────────────────────────────────────────────────────
def action_badge(action: str) -> str:
    colors = {
        "HOLD":   ("#1db954", "HOLD"),
        "TRAIL":  ("#3a86ff", "TRAIL"),
        "REDUCE": ("#f9c74f", "REDUCE"),
        "REVIEW": ("#ff9f1c", "REVIEW"),
        "SELL":   ("#e63946", "SELL"),
    }
    bg, label = colors.get(action, ("#888", action))
    return (f'<span style="background:{bg};color:#fff;font-size:1.3rem;font-weight:700;'
            f'padding:6px 18px;border-radius:8px;letter-spacing:2px">{label}</span>')


def broker_badge(broker: str) -> str:
    """Small badge showing which brokerage account owns this position."""
    colors = {
        "Moomoo":   ("#f5a623", "#1a1a00"),
        "IBKR":     ("#00d4ff", "#001a1a"),
        "Zerodha":  ("#b07fff", "#12001a"),
        "AngelOne": ("#ff7070", "#1a0000"),
        "Webull":   ("#00c9a7", "#001a15"),
    }
    if not broker:
        return ""
    if broker.startswith("Moomoo"):
        _key = "Moomoo"
    elif broker.startswith("Webull"):
        _key = "Webull"
    else:
        _key = broker
    bg, text = colors.get(_key, ("#888", "#111"))
    return (f'<span style="background:{bg};color:{text};font-size:0.72rem;font-weight:700;'
            f'padding:3px 10px;border-radius:10px;letter-spacing:0.5px;opacity:0.9">{broker}</span>')


def flag_span(val, label, good=True):
    col = "#1db954" if good else "#e63946"
    icon = "✓" if good else "✗"
    return f'<span style="color:{col}">{icon} {label}: {val}</span>'


def rule_row(label, value_html, triggered, warn=False):
    icon = "✗" if triggered else ("⚠" if warn else "✓")
    col  = "#e63946" if triggered else ("#f9c74f" if warn else "#1db954")
    return (f'<tr><td>{label}</td>'
            f'<td>{value_html}</td>'
            f'<td style="color:{col}">{icon}</td></tr>')


def mini_price_bar(entry, current, hard_stop, tp1, tp2, effective_stop):
    """SVG mini price bar showing stops and targets relative to entry."""
    if current is None:
        return ""
    all_pts = [p for p in [hard_stop, effective_stop, entry, current, tp1, tp2]
               if p is not None and math.isfinite(p)]
    if not all_pts:
        return ""
    lo, hi = min(all_pts), max(all_pts)
    span = (hi - lo) or 1

    def pct(v):
        return round(100 * (v - lo) / span, 1) if v is not None else None

    W = 400
    def x(v):
        return int(W * (v - lo) / span) if (v is not None and math.isfinite(v)) else None

    def vline(v, col, label, yoff=0):
        xv = x(v)
        if xv is None:
            return ""
        return (f'<line x1="{xv}" y1="5" x2="{xv}" y2="45" stroke="{col}" stroke-width="2"/>'
                f'<text x="{xv}" y="{58+yoff}" text-anchor="middle" font-size="8" fill="{col}">'
                f'{fmt_price(v)}</text>')

    svg = (f'<svg width="{W}" height="70" style="display:block;margin:8px 0">'
           f'<rect x="0" y="20" width="{W}" height="10" rx="3" fill="#2a2a3a"/>')
    # Current price bar
    ex = x(entry)
    cx = x(current)
    if ex is not None and cx is not None:
        bar_x = min(ex, cx)
        bar_w = abs(cx - ex)
        bar_col = "#1db954" if current >= entry else "#e63946"
        svg += f'<rect x="{bar_x}" y="20" width="{max(bar_w,2)}" height="10" fill="{bar_col}" opacity="0.6"/>'

    svg += vline(hard_stop,     "#e63946", "Hard Stop")
    if effective_stop != hard_stop:
        svg += vline(effective_stop, "#ff6b35", "Stop",   yoff=10)
    svg += vline(entry,         "#ffffff", "Entry")
    svg += vline(current,       "#00d4ff", "Now")
    svg += vline(tp1,           "#f9c74f", "TP1",    yoff=0)
    svg += vline(tp2,           "#1db954", "TP2",    yoff=10)
    svg += "</svg>"
    return svg


def _render_lwc_chart(r: dict) -> str:
    """Return a collapsible LWC v5.1.0 mini price chart for a position card.

    The chart shows historical daily closes from entry_date to today, with
    horizontal price lines for entry, hard_stop, trailing_stop, TP1, TP2, and HWM.
    Uses lazy initialisation — the chart is rendered only when the user clicks
    'Price Chart', so page load is not affected even with many positions.

    Chart creation logic lives in the shared _lwcFactory() function in
    templates/sell_report.html; each card only emits its data + a _lwcRegister() call.
    """
    import json as _json

    price_history = r.get("price_history") or []
    if not price_history:
        return ""

    sym       = r.get("symbol", "UNK")
    # Unique but deterministic ID for the chart container
    chart_id  = f"lwc_{sym.replace(':','_').replace(' ','_').replace('-','_')}_{abs(hash(sym)) % 9999:04d}"
    data_js   = _json.dumps(price_history)

    entry_px      = r.get("entry_price") or 0
    hard_stop     = r.get("hard_stop")
    trailing_stop = r.get("trailing_stop")
    tp1           = r.get("tp1_price")
    tp2           = r.get("tp2_price")
    hwm           = r.get("high_water_mark")
    entry_date    = r.get("entry_date") or ""

    # Build price lines as JSON-serialisable list of dicts
    plines: list[dict] = []
    if entry_px:
        plines.append({"price": entry_px, "color": "#3498db", "lineWidth": 1.5, "lineStyle": 0, "axisLabelVisible": True, "title": "Entry"})
    if hard_stop:
        plines.append({"price": hard_stop, "color": "#e74c3c", "lineWidth": 1.5, "lineStyle": 2, "axisLabelVisible": True, "title": "Stop"})
    if trailing_stop and trailing_stop != hard_stop:
        plines.append({"price": trailing_stop, "color": "#e67e22", "lineWidth": 1.5, "lineStyle": 2, "axisLabelVisible": True, "title": "Trail"})
    if tp1:
        plines.append({"price": tp1, "color": "#1db954", "lineWidth": 1, "lineStyle": 2, "axisLabelVisible": True, "title": "TP1"})
    if tp2:
        plines.append({"price": tp2, "color": "#2ecc71", "lineWidth": 1, "lineStyle": 2, "axisLabelVisible": True, "title": "TP2"})
    if hwm and entry_px and hwm > entry_px:
        plines.append({"price": hwm, "color": "#f9c74f", "lineWidth": 1, "lineStyle": 1, "axisLabelVisible": True, "title": "HWM"})

    plines_js = _json.dumps(plines)

    # Entry marker — LWC v5: use createSeriesMarkers(series, markers) not series.setMarkers()
    entry_dates = {d["time"] for d in price_history}
    if entry_date and entry_date in entry_dates:
        marker_js = _json.dumps({"time": entry_date, "position": "belowBar", "color": "#3498db", "shape": "arrowUp", "text": "Entry", "size": 1})
    else:
        marker_js = "null"

    return f"""<div style="margin-top:10px">
  <button type="button" onclick="lwcToggle('{chart_id}')"
    style="font-size:0.75rem;padding:3px 12px;background:#1a2b3c;color:#7eb8f7;border:1px solid #2d4a66;border-radius:4px;cursor:pointer">
    &#128200; Price Chart
  </button>
  <div id="{chart_id}" style="display:none;margin-top:6px;border-radius:6px;overflow:hidden">
    <div id="{chart_id}__c" style="height:240px;width:100%;background:#1a2334"></div>
  </div>
</div>
<script>_lwcRegister('{chart_id}',{data_js},{plines_js},{marker_js});</script>"""


def render_card(r: dict) -> str:
    action = r["action"]
    cp     = r["current_price"]
    gained = r["current_gain_pct"]
    cr     = r["current_r"]
    sym    = r["symbol"]
    errors = r["errors"]
    trading_days = r.get("trading_days")
    trading_days_label = f"{trading_days}d" if trading_days is not None else "Unknown"

    gain_col = "#1db954" if gained >= 0 else "#e63946"
    gain_sign = "+" if gained >= 0 else ""

    # Conviction badge
    conv_score = r.get("conviction_score")
    conv_label = r.get("conviction_label", "")
    if conv_score is not None:
        if conv_score >= 8:     conv_col = "#1db954"
        elif conv_score >= 6.5: conv_col = "#4ecdc4"
        elif conv_score >= 4.5: conv_col = "#f9c74f"
        elif conv_score >= 3:   conv_col = "#ff6b35"
        else:                   conv_col = "#e63946"
        conv_badge = (
            f'<span class="badge" style="background:{conv_col}22;color:{conv_col}">'
            f'🎯 {conv_score}/10 {conv_label}</span>'
        )
    else:
        conv_badge = ""

    # Quality badge (Oversized / Undersized warning in header)
    _q = r.get("position_quality", "UNKNOWN")
    _q_cols = {"OVERSIZED": "#e63946", "UNDERSIZED": "#ff9f1c", "OPTIMAL": "#1db954", "UNKNOWN": "#888"}
    _q_labels = {"OVERSIZED": "\U0001f4c9 Oversized", "UNDERSIZED": "\U0001f4c8 Undersized",
                 "OPTIMAL": "\u2705 Optimal", "UNKNOWN": ""}
    q_badge = ""
    if _q in ("OVERSIZED", "UNDERSIZED"):
        _qc = _q_cols[_q]
        q_badge = (f'<span class="badge" style="background:{_qc}22;color:{_qc}">{_q_labels[_q]}</span>')

    # stop/target display
    def fp(v):
        return fmt_price(v)

    # SVG bar
    price_bar = mini_price_bar(
        r["entry_price"], cp,
        r.get("hard_stop"), r.get("tp1_price"), r.get("tp2_price"),
        r.get("effective_stop")
    )

    # VIX regime badge
    vix_cols = {"NORMAL": "#1db954", "FEAR": "#f9c74f", "EXTREME_FEAR": "#e63946"}
    vix_col = vix_cols.get(r["vix_regime"], "#888")
    vix_label = r["vix_regime"].replace("_", " ")

    # Decay flags
    decay_parts = []
    if r["decay_rsi"]:
        decay_parts.append(f'<span style="color:#e63946">RSI&lt;45({r["rsi"]:.0f})</span>')
    elif r["rsi"] is not None:
        decay_parts.append(f'<span style="color:#1db954">RSI({r["rsi"]:.0f})</span>')
    if r["decay_adx"]:
        decay_parts.append(f'<span style="color:#e63946"><abbr title="Average Directional Index (ADX): measures trend strength 0–100. Below 20 = weak/ranging market, above 25 = strong trend.">ADX</abbr>&lt;20({r["adx"]:.0f})</span>')
    elif r["adx"] is not None:
        decay_parts.append(f'<span style="color:#1db954"><abbr title="Average Directional Index (ADX): measures trend strength 0–100. Below 20 = weak/ranging market, above 25 = strong trend.">ADX</abbr>({r["adx"]:.0f})</span>')
    if r["decay_rv"]:
        decay_parts.append(f'<span style="color:#e63946">RelVol&lt;0.5({r["rel_vol"]:.2f})</span>')
    elif r["rel_vol"] is not None:
        decay_parts.append(f'<span style="color:#1db954">RelVol({r["rel_vol"]:.2f})</span>')
    decay_html = " &nbsp;".join(decay_parts) if decay_parts else "<span style='color:#888'>N/A</span>"

    # Fundamentals row
    pe_html = (f'<span style="color:#e63946">PE {r["pe"]:.1f} ⚠</span>'
               if r.get("fund_pe_flag") else
               f'<span style="color:#aaa">PE {r["pe"]:.1f}</span>'
               if r.get("pe") else "<span style='color:#555'>PE N/A</span>")
    roe_html = (f'<span style="color:#e63946">ROE {r["roe"]:.1f}% ⚠</span>'
                if r.get("fund_roe_flag") else
                f'<span style="color:#aaa">ROE {r["roe"]:.1f}%</span>'
                if r.get("roe") else "<span style='color:#555'>ROE N/A</span>")

    # R display
    r_color = "#1db954" if (cr or 0) >= 0 else "#e63946"
    r_html  = (f'<span class="val-lg" style="color:{r_color}">'
               f'{("+" if cr >= 0 else "") if cr is not None else ""}'
               f'{cr:.2f}R</span>'
               if cr is not None else "<span style='color:#888'>R N/A</span>")

    tp1_col = "#1db954" if r["tp1_hit"] else "#aaa"
    tp2_col = "#1db954" if r["tp2_hit"] else "#aaa"

    error_html = ""
    if errors:
        error_html = f'<div style="color:#e63946;font-size:0.8rem;margin-top:6px">⚠ {"; ".join(errors)}</div>'

    atr_now = r.get("atr_current")
    atr_html = f'{atr_now:.4f}' if atr_now else "N/A (using entry ATR)"

    _quality = r.get("position_quality", "UNKNOWN")

    # ── Holding Period & AI Growth section ───────────────────────────────────
    _hp  = r.get("held_days")
    _ht  = r.get("holding_term", "UNKNOWN")
    _cgt = r.get("cgt_eligible", False)
    _d2l = r.get("days_to_lt")
    _gai = r.get("growth_ai") or {}

    _term_styles = {
        "LONG_TERM":  ("#1db954", "Long-Term \U0001f340"),
        "MED_TERM":   ("#4ecdc4", "Mid-Term"),
        "SHORT_TERM": ("#f9c74f", "Short-Term"),
        "RECENT":     ("#ff6b35", "Recent Entry"),
        "UNKNOWN":    ("#888",    "? Hold Period"),
    }
    _htc, _htl = _term_styles.get(_ht, ("#888", _ht))
    _hold_badge = (
        f'<span class="badge" style="background:{_htc}22;color:{_htc}">\U0001f4c5 {_htl}</span>'
    )
    _held_str = f"{_hp}d" if _hp is not None else "?"
    if _cgt:
        _cgt_label = '<span style="color:#1db954;font-size:0.78rem">\u2713 CGT 50% discount eligible</span>'
    elif _d2l:
        _cgt_label = f'<span style="color:#888;font-size:0.78rem">{_d2l}d to long-term CGT discount</span>'
    else:
        _cgt_label = ""

    _oc  = {"BULLISH": "#1db954", "NEUTRAL": "#f9c74f", "BEARISH": "#e63946"}
    _hrc = {"HOLD_FOR_GROWTH": "#1db954", "MONITOR_CLOSELY": "#f9c74f", "EXIT_NOW": "#e63946"}

    def _olbadge(lbl, val, est):
        _c = _oc.get(val, "#888")
        _e = f" ({'+' if est >= 0 else ''}{est:.1f}%)" if est is not None else ""
        return (f'<span class="badge-sm" style="background:{_c}22;color:{_c}">{lbl}: {val}{_e}</span>')

    if _gai:
        _o6   = _gai.get("outlook_6m")
        _o12  = _gai.get("outlook_12m")
        _g6   = _gai.get("growth_est_6m")
        _g12  = _gai.get("growth_est_12m")
        _hrec = _gai.get("hold_rec", "")
        _greg = _gai.get("reason", "")
        _hrcol = _hrc.get(_hrec, "#888")
        _grow_inner = (
            (_olbadge("6m",  _o6,  _g6)  if _o6  else "") + " " +
            (_olbadge("12m", _o12, _g12) if _o12 else "") + " " +
            f'<span class="badge-sm" style="background:{_hrcol}22;color:{_hrcol}">'
            f'{_hrec.replace("_", " ")}</span> ' +
            f'<span style="color:#888;font-size:0.78rem;font-style:italic">{_greg}</span>'
        )
        _growth_sec = (
            f'<div class="growth-sec">{_grow_inner}</div>'
        )
    else:
        _growth_sec = ""

    holding_period_html = (
        f'<div class="hold-row">'
        f'{_hold_badge}'
        f'<span style="color:#aaa;font-size:0.85rem">{_held_str} held</span>'
        f'{(" &nbsp; " + _cgt_label) if _cgt_label else ""}'
        f'{(" &nbsp; " + _growth_sec) if _growth_sec else ""}'
        f'</div>'
    )

    # ── Hedge badge ───────────────────────────────────────────────────────────
    _hu  = r.get("hedge_urgency", "NONE")
    _hv  = r.get("hedge_vehicle") or {}
    _hsz = r.get("hedge_sizing_pct") or 0
    _htg = r.get("hedge_triggers") or []
    _hedge_cols = {"URGENT": "#e63946", "RECOMMEND": "#f9c74f", "CONSIDER": "#ff9f1c"}
    if _hu != "NONE" and _hv:
        _hc  = _hedge_cols.get(_hu, "#888")
        _hticker = _hv.get("ticker", "")
        _hname   = _hv.get("name", "")
        _htrigger_str = "; ".join(_htg[:2])  # show first two triggers max
        hedge_html = (
            f'<div class="hedge-box" style="background:{_hc}11;border:1px solid {_hc}44">'
            f'<span class="hedge-title" style="color:{_hc}">'
            f'&#x1F6E1; HEDGE {_hu}</span>'
            f'<code class="hedge-code" style="background:{_hc}22;color:{_hc}">{_hticker}</code>'
            f'<span style="color:#aaa;font-size:0.8rem">{_hname}</span>'
            f'<span style="color:#888;font-size:0.78rem">~{int(_hsz)}% of position</span>'
            f'{"<span style=\\'color:#666;font-size:0.75rem\\'>" + _htrigger_str + "</span>" if _htrigger_str else ""}'
            f'</div>'
        )
    else:
        hedge_html = ""

    _si     = r.get("signal_insight")
    _signal_confirmation = r.get("signal_confirmation")
    _si_grade = (_si.get("_grade", "-") if _si else "-")

    # Moneyball for sell urgency: rank vs recent buy-signal population (scaled ×10 for /100 display)
    _urg_score = r.get("urgency_score") or r.get("conviction_score") or 5.0
    _urg_score_100 = round(float(_urg_score) * 10.0, 1)   # scale 0-10 → 0-100
    try:
        import sys as _sys
        import os as _os
        _tv_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _tv_dir not in _sys.path:
            _sys.path.insert(0, _tv_dir)
        from alert_tracker import compute_signal_moneyball as _csm
        _mb_sell, _fin_sell = _csm(_urg_score_100)   # _mb_sell=percentile rank, _fin_sell=blended
    except Exception:
        _mb_sell, _fin_sell = 50.0, _urg_score_100
    _mb_sell_html = (
        f'<span style="display:inline-flex;align-items:center;gap:3px;margin-left:8px;vertical-align:middle">'
        f'<span style="font-size:.57rem;color:#9ca3af;letter-spacing:.06em;text-transform:uppercase">CURRENT</span>'
        f'<span style="color:#f59e0b;font-weight:700;font-size:.9rem">&nbsp;{_urg_score_100:.0f}</span>'
        f'<span style="color:#6b7280;padding:0 3px;font-weight:300">+</span>'
        f'<span style="font-size:.57rem;color:#9ca3af;letter-spacing:.06em;text-transform:uppercase">MONEYBALL</span>'
        f'<span style="color:#60a5fa;font-weight:700;font-size:.9rem">&nbsp;{_mb_sell:.0f}</span>'
        f'<span style="color:#6b7280;padding:0 3px;font-weight:300">=</span>'
        f'<span style="font-size:.57rem;color:#9ca3af;letter-spacing:.06em;text-transform:uppercase">Blended</span>'
        f'<span style="color:#22c55e;font-weight:700;font-size:.9rem">&nbsp;{_fin_sell:.0f}</span>'
        f'</span>'
    )

    return f"""
<div class="card" data-quality="{_quality}" data-profit-action="{r.get('profit_action','HOLD_GAINS')}" data-scaling-action="{r.get('scaling_action','HOLD_SIZE')}" data-action="{r.get('action','HOLD')}" data-gain="{r.get('current_gain_pct',0):.4f}" data-r="{r.get('current_r') or 0:.4f}" data-conviction="{r.get('conviction_score') or 0:.4f}" data-symbol="{sym}" data-broker="{r.get('broker','')}" data-held-days="{r.get('held_days') or 0}" data-grade="{_si_grade}">
  <!-- Header -->
  <div class="card-hdr">
    <div>
      <span class="card-sym">{sym}</span>
      <span class="card-meta">entered {r["entry_date"]} · {r["shares"]} shares</span>
    </div>
    <div class="card-hdr-right">
      {action_badge(action)}
      {broker_badge(r.get("broker", ""))}
      {conv_badge}{_mb_sell_html}
      {q_badge}
      {r_html}
      <span class="val-gain" style="color:{gain_col}">{gain_sign}{gained:.1f}%</span>
    </div>
  </div>

  <!-- Action reason -->
  <div class="card-reason" style="border-left:3px solid {vix_cols.get(r['vix_regime'],'#888')}">
    <strong>Reason:</strong> {r["action_reason"]}
  </div>

  <!-- Momentum Override Notice (amber banner when stop touched but rally active) -->
  {_render_momentum_override_notice(r)}

  <!-- Price Bar -->
  {price_bar}

  <!-- LWC Price Chart (lazy, opens on click) -->
  {_render_lwc_chart(r)}

  <!-- Momentum Trend Badges (1d / 5d / 14d price change + EMA20) -->
  {_render_trend_badge(r)}

  <!-- Holding Period & AI Growth -->
  {holding_period_html}

  <!-- Hedge Recommendation -->
  {hedge_html}

  <!-- Two column detail table -->
  <div class="detail-grid">

    <!-- Left: Price & Stops -->
    <div>
      <div class="sec-label">Prices & Stops</div>
      <table class="detail-tbl">
        <thead><tr><th>Metric</th><th>Value</th><th>Status</th></tr></thead>
        {rule_row("Entry",          f'<strong>{fp(r["entry_price"])}</strong>', False)}
        {rule_row("Current",        f'<strong style="color:#00d4ff">{fp(cp)}</strong>', False)}
        {rule_row("High-Water Mk",  fp(r["high_water_mark"]), False)}
        {rule_row("Hard Stop",      f'<span style="color:#e63946">{fp(r["hard_stop"])}</span>', r.get("stopped_out", False))}
        {rule_row("Eff. Stop",      f'<span style="color:#ff6b35">{fp(r["effective_stop"])}</span>',
                  cp is not None and cp <= r["effective_stop"])}
        {rule_row("TP1 (+1.5R)",    f'<span style="color:{tp1_col}">{fp(r["tp1_price"])}</span>',
                  False, warn=not r["tp1_hit"])}
        {rule_row("TP2 (+3.0R)",    f'<span style="color:{tp2_col}">{fp(r["tp2_price"])}</span>',
                  False, warn=not r["tp2_hit"])}
        {rule_row('<abbr title="1R = initial risk per share = entry − hard_stop. R-multiples measure trade progress: +1.5R = TP1, +3.0R = TP2, negative = in drawdown.">1R (risk/share)</abbr>',fp(r["R_value"]), False)}
        {rule_row('<abbr title="Average True Range (14-bar) — measures daily price volatility. Used to set stops and position size. Hard stop = entry − 2×ATR; trail = HWM − 1.5×ATR.">ATR(14) now</abbr>', atr_html, False)}
      </table>
    </div>

    <!-- Right: Rules Check -->
    <div>
      <div class="sec-label">Rules Check</div>
      <table class="detail-tbl">
        <thead><tr><th>Rule</th><th>Detail</th><th>Status</th></tr></thead>
        {rule_row("Break-Even",
                  f'trigger @ {fp(r["breakeven_trigger"])} — {"HIT ✓" if r["breakeven_hit"] else "not yet"}',
                  False, warn=not r["breakeven_hit"])}
        {rule_row("TP1 Hit",
                  "Yes" if r["tp1_hit"] else "No",
                  False, warn=not r["tp1_hit"])}
        {rule_row("TP2 Hit",
                  "Yes" if r["tp2_hit"] else "No",
                  False, warn=not r["tp2_hit"])}
        {rule_row("Time Stop",
                  f'{trading_days_label} / {TIME_STOP_DAYS}d target',
                  r["time_stop_triggered"],
                  warn=(trading_days is not None and trading_days > 15
                        and not r["time_stop_triggered"]))}
        {rule_row("Decay Flags",
                  f'{r["decay_count"]}/{SellEngine.DECAY_FACTOR_COUNT}',
                  r["decay_count"] >= 2, warn=r["decay_count"] == 1)}
        {rule_row("VIX Regime",
                  f'<span style="color:{vix_col}">{vix_label} ({r["vix"]:.1f})</span>',
                  r["vix_force_exit"], warn=r["vix_regime"] == "FEAR")}
        {rule_row("Fundamentals",
                  pe_html + " &nbsp; " + roe_html,
                  r.get("fund_pe_flag", False) and r.get("fund_roe_flag", False),
                  warn=r.get("fund_pe_flag", False) or r.get("fund_roe_flag", False))}
      </table>

      <!-- Decay detail -->
      <div class="decay-sec">
        <strong>Signal Decay:</strong><br>{decay_html}
      </div>
    </div>

  </div>
  {error_html}
  {render_position_mgmt_block(r) if _PM_AVAILABLE else ""}
    {render_signal_insight(_si, urgency_100=_urg_score_100, confirmation=_signal_confirmation)}
</div>
"""


def _render_trend_badge(r: dict) -> str:
    """Render 1d/5d/14d price momentum badges + EMA20 indicator in a card."""
    parts = []
    for key, label in [("price_change_1d", "1d"), ("price_change_5d", "5d"), ("price_change_14d", "14d")]:
        val = r.get(key)
        if val is not None:
            col  = "#1db954" if val >= 0 else "#e63946"
            sign = "+" if val >= 0 else ""
            parts.append(f'<span style="color:{col};font-weight:600">{label}: {sign}{val*100:.1f}%</span>')
    above = r.get("above_ema20")
    if above is not None:
        ema_col = "#1db954" if above else "#e63946"
        parts.append(f'<span style="color:{ema_col}">EMA20: {"above" if above else "below"}</span>')
    return ('<span style="font-size:0.85rem">' + " &nbsp;|&nbsp; ".join(parts) + "</span>"
            if parts else "")


def _render_momentum_override_notice(r: dict) -> str:
    """Render an amber warning box when a momentum override is active (stop touched but rally present)."""
    if not r.get("momentum_override"):
        return ""
    note = r.get("momentum_note", "Momentum override active")
    return (
        f'<div style="background:#fff3cd;color:#856404;padding:8px 12px;'
        f'border-radius:6px;margin:8px 0;font-size:0.88rem">&#9889; {note}</div>'
    )


def build_summary_bar(results: list, vix: float) -> str:
    """Top-of-page summary bar — action counts + VIX badge."""
    counts = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1

    order  = ["SELL", "REDUCE", "REVIEW", "TRAIL", "HOLD"]
    colors = {"SELL": "#e63946", "REDUCE": "#f9c74f", "REVIEW": "#ff9f1c",
              "TRAIL": "#3a86ff", "HOLD": "#1db954"}
    subtitles = {
        "SELL":   "stop hit / exit now",
        "REDUCE": "partial exit (TP1+decay)",
        "REVIEW": "check signal decay",
        "TRAIL":  "move stop higher",
        "HOLD":   "no action needed",
    }
    badges = ""
    for a in order:
        cnt = counts.get(a, 0)
        c = colors.get(a, "#888")
        opacity = "1" if cnt > 0 else "0.3"
        sub = subtitles.get(a, "")
        badges += (
            f'<div class="act-btn" onclick="filterAction(this,\'{a}\')" '
            f'style="background:{c};color:#fff;padding:8px 20px;border-radius:8px;'
            f'font-weight:700;font-size:1rem;cursor:pointer;transition:opacity .15s;'
            f'opacity:{opacity};text-align:center">'
            f'{a}: {cnt}'
            f'<br><span style="font-size:0.65rem;font-weight:400;opacity:0.85">{sub}</span>'
            f'</div>'
        )

    vix_regime_lbl = "EXTREME FEAR" if vix >= 35 else "FEAR" if vix >= 25 else "NORMAL"
    vix_col = "#e63946" if vix >= 35 else "#f9c74f" if vix >= 25 else "#1db954"
    vix_badge = (f'<div style="margin-left:auto;background:#0d0d20;border:1px solid {vix_col};'
                 f'color:{vix_col};padding:8px 18px;border-radius:8px;font-weight:700;text-align:center">'
                 f'VIX {vix:.1f}'
                 f'<br><span style="font-size:0.65rem;font-weight:400;opacity:0.85">{vix_regime_lbl}</span>'
                 f'</div>')

    return (f'<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;'
            f'background:#0d0d20;padding:16px 20px;border-radius:10px;margin-bottom:24px">'
            f'{badges}{vix_badge}</div>')


def build_legend() -> str:
    rules = [
        ("1", "#e63946", "Hard Stop", f"Entry − {HARD_STOP_MULT}×ATR_entry (min {int(HARD_STOP_FLOOR*100)}% below entry)"),
        ("2", "#ff6b35", "Break-Even", f"Once price hits Entry + {BREAKEVEN_MULT}×ATR, stop moves to entry"),
        ("3", "#3a86ff", "Trailing Stop", f"Rolling high − {TRAIL_MULT}×ATR_current (from TP1 onwards)"),
        ("4", "#f9c74f", "R-Multiples", f"TP1 @ +{TP1_R}R (reduce 30%), TP2 @ +{TP2_R}R (reduce 50%), trail rest"),
        ("5", "#ffbe0b", "Time Stop", f"{TIME_STOP_DAYS} trading days without +{int(TIME_STOP_GAIN*100)}% gain"),
        ("6", "#8338ec", "Signal Decay", "RSI<45, ADX<20, RelVol<0.5 — 2+ flags = REDUCE"),
        ("7", "#06d6a0", "VIX Regime", "VIX>25=FEAR(tighten), VIX>35=EXTREME FEAR(force exit losers)"),
        ("8", "#ff6b6b", "Fund. Drift", "PE>60 or ROE<5% → REVIEW flag"),
    ]
    rows = "".join(
        f'<tr><td style="color:{col};font-weight:700;padding:4px 12px 4px 0">#{n}</td>'
        f'<td style="color:#ccc;font-weight:600;padding:4px 16px 4px 0">{name}</td>'
        f'<td style="color:#999;font-size:0.82rem;padding:4px 0">{desc}</td></tr>'
        for n, col, name, desc in rules
    )
    return (f'<details style="background:#0d0d20;border:1px solid #2d2d4e;border-radius:8px;'
            f'padding:12px 16px;margin-bottom:20px">'
            f'<summary style="color:#888;cursor:pointer;font-size:0.85rem">Exit Rule Reference (click to expand)</summary>'
            f'<table style="margin-top:10px;border-collapse:collapse;font-family:monospace">'
            f'<thead><tr><th>#</th><th>Rule</th><th>Description</th></tr></thead>'
            f'{rows}</table>'
            f'</details>')


def build_top5_urgent(results: list) -> str:
    """Build TOP 5 ACTION REQUIRED banner for urgent exit positions."""
    _priority = {"SELL": 4, "REDUCE": 3, "TRAIL": 2, "REVIEW": 1, "HOLD": 0}
    urgent = [r for r in results if r.get("action") in ("SELL", "REDUCE", "TRAIL", "REVIEW")]
    if not urgent:
        return ""
    urgent.sort(key=lambda r: (_priority.get(r.get("action", "HOLD"), 0),
                                -abs(r.get("current_gain_pct", 0))), reverse=True)
    top5 = urgent[:5]
    _colors = {"SELL": "#e63946", "REDUCE": "#f9c74f", "TRAIL": "#3a86ff", "REVIEW": "#ff9f1c"}
    rows = ""
    for i, r in enumerate(top5, 1):
        action  = r.get("action", "HOLD")
        col     = _colors.get(action, "#888")
        sym     = r.get("symbol", "?")
        reason  = r.get("action_reason", "")
        broker  = r.get("broker", "")
        pnl     = r.get("current_gain_pct", 0)
        pnl_col = "#2ecc71" if pnl >= 0 else "#e74c3c"
        _cp     = r.get("current_price")
        _ep     = r.get("entry_price")
        _sh     = r.get("shares", "?")
        _cp_str = fmt_price(_cp) if _cp else "N/A"
        _ep_str = fmt_price(_ep) if _ep else "N/A"
        rows += (
            f"<tr>"
            f"<td style='color:#888;text-align:center;padding:5px 8px'>{i}</td>"
            f"<td style='padding:5px 8px'><strong style='color:#fff'>{sym}</strong></td>"
            f"<td style='padding:5px 8px'><span style='background:{col};color:#0f0f1a;font-weight:700;"
            f"padding:2px 10px;border-radius:6px;font-size:0.8rem'>{action}</span></td>"
            f"<td style='color:#888;font-size:0.8rem;padding:5px 8px'>{broker}</td>"
            f"<td style='color:#00d4ff;font-family:monospace;padding:5px 8px'>{_cp_str}</td>"
            f"<td style='color:#aaa;font-family:monospace;padding:5px 8px'>{_ep_str}</td>"
            f"<td style='color:#aaa;text-align:right;padding:5px 8px'>{_sh}</td>"
            f"<td style='color:#bbb;font-size:0.82rem;padding:5px 8px'>{reason}</td>"
            f"<td style='color:{pnl_col};text-align:right;font-family:monospace;padding:5px 8px'>{pnl:+.1f}%</td>"
            f"</tr>\n"
        )
    has_sell    = any(r.get("action") == "SELL" for r in top5)
    border_col  = "#e63946" if has_sell else "#f9c74f"
    return (
        f'<div style="background:#1a0a0a;border:2px solid {border_col};border-radius:10px;'
        f'padding:16px 20px;margin-bottom:24px">'
        f'<div style="color:{border_col};font-size:1rem;font-weight:700;margin-bottom:10px;'
        f'letter-spacing:1px">&#9888; TOP {len(top5)} URGENT EXITS — ACTION REQUIRED</div>'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<tr style="color:#555;font-size:0.75rem;text-transform:uppercase">'
        f'<th scope="col" style="padding:4px 8px;text-align:center">#</th>'
        f'<th scope="col" style="padding:4px 8px">Symbol</th>'
        f'<th scope="col" style="padding:4px 8px">Action</th>'
        f'<th scope="col" style="padding:4px 8px">Broker</th>'
        f'<th scope="col" style="padding:4px 8px">Price</th>'
        f'<th scope="col" style="padding:4px 8px">Entry</th>'
        f'<th scope="col" style="padding:4px 8px;text-align:right">Shares</th>'
        f'<th scope="col" style="padding:4px 8px">Reason</th>'
        f'<th scope="col" style="padding:4px 8px;text-align:right">PnL %</th>'
        f'</tr>'
        f'{rows}'
        f'</table>'
        f'</div>'
    )


# ─── Signal Engine Insight helpers ─────────────────────────────────────────────

_SIGNAL_POLICY_VERSION = "l1l4-confirmation-v3"
_SIGNAL_SHALLOW_STOP_MAX_ATR = 0.5
_SIGNAL_BULLISH_DIRECTIONAL = 6.0
_SIGNAL_BULLISH_CONTEXT = 6.0
_SIGNAL_BULLISH_COMPONENT_FLOOR = 4.0
_SIGNAL_WEAK_DIRECTIONAL = 3.0
_SIGNAL_WEAK_CONTEXT = 4.0


def _block_pyramid_add(result: dict, reason: str) -> None:
    """Fail-close a proposed add while preserving its full recommendation."""
    result["blocked_add_recommendation"] = {
        "recommendation": result.get("add_recommendation"),
        "suggested_qty": result.get("scaling_qty", 0),
        "original_reason": result.get("scaling_reason", ""),
    }
    result["scaling_action"] = "PYRAMID_BLOCKED"
    result["scaling_qty"] = 0
    result["add_recommendation"] = None
    result["scaling_reason"] = reason


def _apply_signal_confirmation(result: dict) -> dict:
    """Annotate risk actions and fail-close adds using separate L1 and L2-L4 views."""
    original_action = result.get("action", "HOLD")
    original_scaling = result.get("scaling_action", "HOLD_SIZE")
    insight = result.get("signal_insight") or {}
    scores = insight.get("scores") or {}
    l1_data = scores.get("l1") or {}

    try:
        composite = float(insight["composite"])
        l1_score = float(l1_data["total"])
    except (KeyError, TypeError, ValueError):
        scaling_action_changed = original_scaling == "PYRAMID_ADD"
        if scaling_action_changed:
            _block_pyramid_add(
                result,
                "L1-L4 confirmation unavailable; pyramid add blocked.",
            )
        result["signal_confirmation"] = {
            "policy": _SIGNAL_POLICY_VERSION,
            "evaluated": False,
            "scaling_action_changed": scaling_action_changed,
            "status": "INSUFFICIENT",
            "risk_action_unchanged": True,
            "risk_action": original_action,
            "original_scaling_action": original_scaling,
            "final_scaling_action": result.get("scaling_action", original_scaling),
            "reason": "L1-L4 scores unavailable",
        }
        return result

    directional_items = []
    for score_key, raw_key, weight in (
        ("rsi", "raw_rsi", 0.30),
        ("sma200", "raw_sma200_pct", 0.40),
        ("macd", "raw_macd_hist", 0.30),
    ):
        if l1_data.get(raw_key) is not None and l1_data.get(score_key) is not None:
            directional_items.append((score_key, float(l1_data[score_key]), weight))
    directional_score = None
    if len(directional_items) >= 2:
        total_weight = sum(weight for _, _, weight in directional_items)
        directional_score = sum(score * weight for _, score, weight in directional_items) / total_weight

    context_items = []
    for layer, weight in (("l2", 0.30), ("l3", 0.20), ("l4", 0.20)):
        layer_total = (scores.get(layer) or {}).get("total")
        if layer_total is not None:
            context_items.append((layer, float(layer_total), weight))
    context_score = None
    if len(context_items) == 3:
        total_weight = sum(weight for _, _, weight in context_items)
        context_score = sum(score * weight for _, score, weight in context_items) / total_weight

    current = result.get("current_price")
    effective_stop = result.get("effective_stop")
    stop_breach = None
    if current is not None and effective_stop:
        stop_breach = max(0.0, (float(effective_stop) - float(current)) / float(effective_stop))
    atr = result.get("atr_current") or result.get("atr_entry")
    stop_breach_atr = None
    if current is not None and effective_stop and atr and float(current) <= float(effective_stop):
        stop_breach_atr = (float(effective_stop) - float(current)) / float(atr)

    status = "INSUFFICIENT"
    confirmation_reason = "Directional L1 or L2-L4 context is incomplete"
    reliable_market_price = bool(result.get("_yf_ok"))
    directional_floor = (
        min(score for _, score, _ in directional_items)
        if directional_items else None
    )
    bullish_evidence = (
        directional_score is not None
        and directional_score >= _SIGNAL_BULLISH_DIRECTIONAL
        and context_score is not None
        and context_score >= _SIGNAL_BULLISH_CONTEXT
        and directional_floor is not None
        and directional_floor >= _SIGNAL_BULLISH_COMPONENT_FLOOR
    )

    if directional_score is not None and context_score is not None:
        if original_action in ("SELL", "REDUCE"):
            if bullish_evidence:
                status = "CONFLICTS"
                shallow_note = (
                    f" after a shallow {stop_breach_atr:.2f} ATR stop breach"
                    if stop_breach_atr is not None
                    and stop_breach_atr <= _SIGNAL_SHALLOW_STOP_MAX_ATR
                    and reliable_market_price
                    else ""
                )
                confirmation_reason = (
                    f"Bullish directional L1 ({directional_score:.1f}/10) and L2-L4 context "
                    f"({context_score:.1f}/10) conflict with {original_action}{shallow_note}; "
                    "risk action remains unchanged"
                )
            elif directional_score <= 4.0 or context_score <= 4.5:
                status = "CONFIRMS"
                confirmation_reason = (
                    f"Weak directional L1 ({directional_score:.1f}/10) or L2-L4 context "
                    f"({context_score:.1f}/10) confirms {original_action}"
                )
            else:
                status = "MIXED"
                confirmation_reason = (
                    f"Directional L1 ({directional_score:.1f}/10) and L2-L4 context "
                    f"({context_score:.1f}/10) are mixed; risk action remains unchanged"
                )
        elif original_action == "HOLD":
            if directional_score <= _SIGNAL_WEAK_DIRECTIONAL and context_score <= _SIGNAL_WEAK_CONTEXT:
                status = "CONFLICTS"
                confirmation_reason = (
                    f"Weak directional L1 ({directional_score:.1f}/10) and L2-L4 context "
                    f"({context_score:.1f}/10) conflict with HOLD; risk action remains unchanged"
                )
            elif bullish_evidence:
                status = "CONFIRMS"
                confirmation_reason = (
                    f"Directional L1 ({directional_score:.1f}/10) and L2-L4 context "
                    f"({context_score:.1f}/10) confirm HOLD"
                )
            else:
                status = "MIXED"
                confirmation_reason = (
                    f"Directional L1 ({directional_score:.1f}/10) and L2-L4 context "
                    f"({context_score:.1f}/10) are mixed; HOLD remains unchanged"
                )
        else:
            status = "MIXED"
            confirmation_reason = (
                f"Directional L1 {directional_score:.1f}/10; "
                f"L2-L4 context {context_score:.1f}/10"
            )

    scaling_action_changed = original_scaling == "PYRAMID_ADD" and not bullish_evidence
    if scaling_action_changed:
        _block_pyramid_add(
            result,
            "L1-L4 add confirmation failed: requires directional L1 "
            f">= {_SIGNAL_BULLISH_DIRECTIONAL:.1f}/10, every observed directional "
            f"component >= {_SIGNAL_BULLISH_COMPONENT_FLOOR:.1f}/10, and L2-L4 context "
            f">= {_SIGNAL_BULLISH_CONTEXT:.1f}/10; got directional "
            f"{directional_score if directional_score is not None else 'N/A'}, floor "
            f"{directional_floor if directional_floor is not None else 'N/A'}, context "
            f"{context_score if context_score is not None else 'N/A'}.",
        )

    result["signal_confirmation"] = {
        "policy": _SIGNAL_POLICY_VERSION,
        "evaluated": True,
        "status": status,
        "scaling_action_changed": scaling_action_changed,
        "risk_action_unchanged": result.get("action") == original_action,
        "risk_action": original_action,
        "original_scaling_action": original_scaling,
        "final_scaling_action": result.get("scaling_action", original_scaling),
        "l1_score": round(l1_score, 2),
        "directional_l1_score": round(directional_score, 2) if directional_score is not None else None,
        "directional_l1_components": [name for name, _, _ in directional_items],
        "directional_l1_floor": round(directional_floor, 2) if directional_floor is not None else None,
        "l2_l4_context_score": round(context_score, 2) if context_score is not None else None,
        "l2_l4_context_components": [name for name, _, _ in context_items],
        "composite": round(composite, 2),
        "stop_breach_pct": round(stop_breach * 100, 2) if stop_breach is not None else None,
        "stop_breach_atr": round(stop_breach_atr, 2) if stop_breach_atr is not None else None,
        "reason": confirmation_reason,
    }
    return result

def _fetch_signal_insight(symbol: str,
                          screener_cache: dict | None = None,
                          tv_fund_cache: dict | None = None,
                          yc: dict | None = None) -> dict:
    """
    Run the L1-L4 analysis pipeline for a held position symbol.

    Automatically resolves leveraged ETFs to their underlying equity so
    scoring uses meaningful fundamentals.  Examples:
      - NYSE:PTIR  → NYSE:PLTR  (Leverage Shares PLTR)
      - SGX:NVIW   → NASDAQ:NVDA (Societe Generale NVDA DLC)
      - NYSE:TSLL  → NASDAQ:TSLA (Direxion TSLA bull 2×)

    Any ticker present in LEVERAGED_ETF_UNDERLYING is redirected to its
    underlying before calling the signal engine, so new ETFs added to that
    dict are handled automatically with no further code changes.

    Returns {} when _SIGNAL_INSIGHT_OK is False or on any error.
    On success, result dict has: composite, scores, fundamentals, sentiment_data
    plus an injected `_grade` key (A/B/C/D).
    """
    if not _SIGNAL_INSIGHT_OK or _ANALYSIS_ENGINE is None:
        return {}
    try:
        # ── Leveraged ETF resolution ──────────────────────────────────────────
        # analyse_signal() resolves ETF→underlying internally for L2/L3/L4.
        # We also pre-populate tv_fund_cache with the underlying's data so the
        # engine doesn't need to make a separate per-symbol fetch at call time.
        if _CONVICTION_OK and tv_fund_cache is not None:
            _raw = symbol.split(":", 1)[-1]
            _underlying_raw = LEVERAGED_ETF_UNDERLYING.get(_raw)
            if _underlying_raw:
                _u_tv = UNDERLYING_TO_TV.get(_underlying_raw,
                                              f"NASDAQ:{_underlying_raw}")
                if _u_tv not in tv_fund_cache:
                    try:
                        _extra = _ANALYSIS_ENGINE.fetch_tv_fundamentals(
                            [_u_tv], use_auth=True
                        )
                        tv_fund_cache.update(_extra)
                    except Exception:
                        pass  # cache miss is non-fatal; engine will re-fetch

        fire = {"pro_symbol": symbol, "message": ""}
        result = _ANALYSIS_ENGINE.analyse_signal(
            fire,
            screener_cache=screener_cache or {},
            tv_fund_cache=tv_fund_cache,
            yc=yc,
            use_auth=True,
        )
        comp = result.get("composite", 0) or 0
        if comp >= 75:
            grade = "A"
        elif comp >= 60:
            grade = "B"
        elif comp >= 45:
            grade = "C"
        else:
            grade = "D"
        result["_grade"] = grade
        return result
    except Exception as exc:
        return {"_error": str(exc)}


def render_signal_insight(insight: dict | None, urgency_100: float | None = None,
                          confirmation: dict | None = None) -> str:
    """
    Render a collapsible <details> block showing the L1-L4 signal analysis.

    Returns "" for None or empty input so cards without insight aren't changed.
    urgency_100: optional sell urgency score (0-100) to show next to the signal analysis score.
    """
    if not insight:
        return ""

    # Error state
    if "_error" in insight and not insight.get("composite"):
        return (
            '<details style="margin-top:12px;background:#1a0a0a;border:1px solid #3a1515;'
            'border-radius:8px;padding:10px 14px">'
            '<summary style="color:#e63946;font-size:0.82rem;cursor:pointer">'
            '&#x26A0; Signal Engine — unavailable for this symbol</summary>'
            f'<p style="color:#888;font-size:0.78rem;margin-top:6px">'
            f'{insight["_error"]}</p>'
            '</details>'
        )

    comp    = insight.get("composite") or 0
    scores  = insight.get("scores") or {}
    fund    = insight.get("fundamentals") or {}
    sent    = insight.get("sentiment_data") or {}
    grade   = insight.get("_grade", "?")
    grade_colors = {"A": "#1db954", "B": "#eab308", "C": "#ff9f1c", "D": "#e63946", "?": "#888"}
    g_col   = grade_colors.get(grade, "#888")
    cname   = fund.get("company_name") or ""

    def _bar(val: float, max_val: float = 10.0, col: str = "#3a86ff") -> str:
        pct = min(100, max(0, val / max_val * 100))
        return (
            f'<div style="background:#1a1a2e;border-radius:4px;height:6px;width:100%;margin-top:3px">'
            f'<div style="background:{col};width:{pct:.0f}%;height:100%;border-radius:4px"></div>'
            f'</div>'
        )

    l1 = (scores.get("l1") or {}).get("total", 0) or 0
    l2 = (scores.get("l2") or {}).get("total", 0) or 0
    l3 = (scores.get("l3") or {}).get("total", 0) or 0
    l4 = (scores.get("l4") or {}).get("total", 0) or 0

    bars_html = (
        f'<div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin-top:8px">'
        f'<div><div style="color:#aaa;font-size:0.72rem">L1 Technical</div>'
        f'{_bar(l1, col="#3a86ff")}<div style="color:#ccc;font-size:0.78rem">{l1:.1f}/10</div></div>'
        f'<div><div style="color:#aaa;font-size:0.72rem">L2 Fundamental</div>'
        f'{_bar(l2, col="#1db954")}<div style="color:#ccc;font-size:0.78rem">{l2:.1f}/10</div></div>'
        f'<div><div style="color:#aaa;font-size:0.72rem">L3 Sentiment</div>'
        f'{_bar(l3, col="#eab308")}<div style="color:#ccc;font-size:0.78rem">{l3:.1f}/10</div></div>'
        f'<div><div style="color:#aaa;font-size:0.72rem">L4 Internals</div>'
        f'{_bar(l4, col="#8b5cf6")}<div style="color:#ccc;font-size:0.78rem">{l4:.1f}/10</div></div>'
        f'</div>'
    )

    # Fundamentals mini row
    fwd_pe  = fund.get("forward_pe")
    roe     = fund.get("roe")
    rev_grw = fund.get("revenue_growth")
    tgt     = sent.get("analyst_target") or fund.get("analyst_target_price")
    cur_px  = fund.get("current_price")
    upside  = None
    if tgt and cur_px and cur_px > 0:
        upside = (tgt - cur_px) / cur_px * 100

    fund_parts = []
    if fwd_pe:  fund_parts.append(f'Fwd PE: {fwd_pe:.1f}')
    if roe:     fund_parts.append(f'ROE: {roe:.1f}%')
    if rev_grw: fund_parts.append(f'Rev Grw: {rev_grw:+.1f}%')
    if tgt:     fund_parts.append(f'Target: {tgt:.2f}' + (f' ({upside:+.1f}% upside)' if upside else ''))
    fund_row = (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;font-size:0.78rem;color:#aaa;margin-top:8px">'
        + ''.join(f'<span>{p}</span>' for p in fund_parts)
        + '</div>'
    ) if fund_parts else ""

    # Sentiment row
    news_sent   = sent.get("news_sentiment_label") or sent.get("vader_label", "")
    analyst_rec = sent.get("analyst_consensus") or sent.get("rec_label", "")
    _sent_col = {"POSITIVE": "#1db954", "BULLISH": "#1db954",
                 "NEGATIVE": "#e63946", "BEARISH": "#e63946",
                 "NEUTRAL": "#888"}.get((news_sent or "").upper(), "#888")
    sent_row = ""
    if news_sent or analyst_rec:
        sent_row = (
            '<div style="display:flex;gap:10px;font-size:0.78rem;margin-top:6px">'
            + (f'<span style="color:{_sent_col}">&#x1F4F0; News: {news_sent}</span>' if news_sent else "")
            + (f'<span style="color:#aaa">&#x1F4CA; Analyst: {analyst_rec}</span>' if analyst_rec else "")
            + '</div>'
        )

    cname_html = f'<span style="color:#888;font-size:0.78rem;margin-left:8px">{cname}</span>' if cname else ""
    urgency_badge = (
        f'<span style="background:#f59e0b22;color:#f59e0b;padding:2px 8px;border-radius:8px;'
        f'font-size:0.78rem;font-weight:700;margin-left:4px">Sell {urgency_100:.0f}/100</span>'
        if urgency_100 is not None else ""
    )
    confirmation = confirmation or {}
    confirmation_status = confirmation.get("status")
    confirmation_colors = {
        "CONFIRMS": "#1db954",
        "CONFLICTS": "#ff9f1c",
        "MIXED": "#94a3b8",
        "INSUFFICIENT": "#64748b",
    }
    confirmation_color = confirmation_colors.get(confirmation_status, "#64748b")
    confirmation_badge = (
        f'<span style="background:{confirmation_color}22;color:{confirmation_color};'
        f'padding:2px 8px;border-radius:8px;font-size:0.74rem;font-weight:700">'
        f'{confirmation_status.title()}</span>'
        if confirmation_status else ""
    )
    confirmation_reason = confirmation.get("reason")
    confirmation_row = (
        f'<div style="color:{confirmation_color};font-size:0.78rem;margin-top:8px">'
        f'<strong>L1-L4 confirmation:</strong> {confirmation_reason}</div>'
        if confirmation_reason else ""
    )

    return (
        f'<details style="margin-top:12px;background:#0d0d20;border:1px solid #2d2d4e;'
        f'border-radius:8px;padding:10px 14px">'
        f'<summary style="cursor:pointer;display:flex;align-items:center;gap:8px;font-size:0.85rem">'
        f'<span style="color:#aaa">&#x1F4CA; Signal Analysis</span>'
        f'<span style="background:{g_col}22;color:{g_col};padding:2px 10px;border-radius:10px;'
        f'font-weight:700;font-size:0.85rem">{grade}</span>'
        f'<span style="color:#ccc;font-size:0.85rem">{comp:.0f}/100</span>'
        f'{urgency_badge}'
        f'{confirmation_badge}'
        f'{cname_html}'
        f'<span style="color:#555;font-size:0.75rem;margin-left:auto">click to expand</span>'
        f'</summary>'
        f'{bars_html}'
        f'{confirmation_row}'
        f'{fund_row}'
        f'{sent_row}'
        f'</details>'
    )


def build_grade_filter(results: list) -> str:
    """
    Signal grade filter bar — A / B / C / D.

    Returns "" when no results have a non-"-" _grade in their signal_insight
    (i.e., when signal insight is disabled or no insights were fetched).
    """
    grade_counts: dict = {}
    for r in results:
        si = r.get("signal_insight") or {}
        g  = si.get("_grade", "-") if si else "-"
        if g and g != "-":
            grade_counts[g] = grade_counts.get(g, 0) + 1

    if not grade_counts:
        return ""

    order  = ["A", "B", "C", "D"]
    colors = {"A": "#1db954", "B": "#eab308", "C": "#ff9f1c", "D": "#e63946"}
    labels = {"A": "A (Strong)", "B": "B (Good)", "C": "C (Fair)", "D": "D (Weak)"}
    total  = len(results)

    def _btn(g_val: str, label: str, col: str, is_active: bool = False) -> str:
        active_extra = ";border-width:2px;box-shadow:0 0 6px " + col if is_active else ""
        return (
            f'<button type="button" class="gr-btn{"  active" if is_active else ""}" '
            f'onclick="filterGrade(this,\'{g_val}\')" '
            f'style="background:transparent;color:{col};border:1px solid {col};'
            f'padding:5px 14px;border-radius:6px;cursor:pointer;'
            f'font-size:0.82rem;font-weight:600{active_extra}">'
            f'{label}</button>'
        )

    btns = _btn("ALL", f"All ({total})", "#38bdf8", is_active=True)
    for g in order:
        if grade_counts.get(g, 0):
            btns += _btn(g, f"{labels[g]} ({grade_counts[g]})", colors[g])

    return (
        f'<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;'
        f'background:#0d0d20;padding:12px 16px;border-radius:8px;margin-bottom:16px">'
        f'<span style="color:#888;font-size:0.8rem;margin-right:4px">Signal Grade:</span>'
        f'{btns}</div>'
    )


def build_size_filter(results: list) -> str:
    """Position quality filter bar — UNDERSIZED / OVERSIZED / OPTIMAL / ALL."""
    quality_counts: dict = {}
    for r in results:
        q = r.get("position_quality", "UNKNOWN")
        quality_counts[q] = quality_counts.get(q, 0) + 1

    order  = ["UNDERSIZED", "OVERSIZED", "OPTIMAL", "UNKNOWN"]
    colors = {"UNDERSIZED": "#ff9f1c", "OVERSIZED": "#e63946", "OPTIMAL": "#1db954", "UNKNOWN": "#888"}
    labels = {"UNDERSIZED": "&#x1F4C8; Undersized", "OVERSIZED": "&#x1F4C9; Oversized",
              "OPTIMAL": "&#x2705; Optimal", "UNKNOWN": "&#x2753; Other"}
    total  = len(results)

    def _btn(q_val: str, label: str, col: str, is_active: bool = False) -> str:
        active_extra = ";border-width:2px;box-shadow:0 0 6px " + col if is_active else ""
        return (
            f'<button type="button" class="sz-btn{"  active" if is_active else ""}" '
            f'onclick="filterSize(this,\'{q_val}\')" '
            f'style="background:transparent;color:{col};border:1px solid {col};'
            f'padding:5px 14px;border-radius:6px;cursor:pointer;'
            f'font-size:0.82rem;font-weight:600{active_extra}">'
            f'{label}</button>'
        )

    btns = _btn("ALL", f"All ({total})", "#38bdf8", is_active=True)
    for q in order:
        if quality_counts.get(q, 0):
            btns += _btn(q, f"{labels[q]} ({quality_counts[q]})", colors[q])

    return (f'<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;'
            f'background:#0d0d20;padding:12px 16px;border-radius:8px;margin-bottom:16px">'
            f'<span style="color:#888;font-size:0.8rem;margin-right:4px">Size Filter:</span>'
            f'{btns}</div>')


def build_decision_filter(results: list) -> str:
    """Position Decision filter bar including actionable and blocked scaling states."""
    # Map each result to a display category
    _cat_map = {
        "TAKE_PROFIT_MAJOR":   "TAKE_PROFIT",
        "TAKE_PROFIT_PARTIAL": "TAKE_PROFIT",
    }
    category_counts: dict = {}
    for r in results:
        pa = r.get("profit_action", "HOLD_GAINS")
        sa = r.get("scaling_action", "HOLD_SIZE")
        if sa == "PYRAMID_BLOCKED":
            cat = "PYRAMID_BLOCKED"
        elif pa in _cat_map:
            cat = _cat_map[pa]
        elif sa == "PYRAMID_ADD":
            cat = "PYRAMID_ADD"
        elif sa == "AVG_DOWN":
            cat = "AVG_DOWN"
        else:
            cat = "HOLD"
        category_counts[cat] = category_counts.get(cat, 0) + 1

    order  = ["TAKE_PROFIT", "PYRAMID_ADD", "PYRAMID_BLOCKED", "AVG_DOWN", "HOLD"]
    colors = {"TAKE_PROFIT": "#e63946", "PYRAMID_ADD": "#1db954",
              "PYRAMID_BLOCKED": "#64748b", "AVG_DOWN": "#ff9f1c", "HOLD": "#3a86ff"}
    labels = {"TAKE_PROFIT": "&#x1F4B0; Take Profit", "PYRAMID_ADD": "&#x1F4C8; Add Position",
              "PYRAMID_BLOCKED": "Add Blocked", "AVG_DOWN": "&#x1F300; Avg Down",
              "HOLD": "&#x270B; Hold"}
    total  = len(results)

    def _btn(cat_val: str, label: str, col: str, is_active: bool = False) -> str:
        active_extra = ";border-width:2px;box-shadow:0 0 6px " + col if is_active else ""
        return (
            f'<button type="button" class="dec-btn{"  active" if is_active else ""}" '
            f'onclick="filterDecision(this,\'{cat_val}\')" '
            f'style="background:transparent;color:{col};border:1px solid {col};'
            f'padding:5px 14px;border-radius:6px;cursor:pointer;'
            f'font-size:0.82rem;font-weight:600{active_extra}">'
            f'{label}</button>'
        )

    btns = _btn("ALL", f"All ({total})", "#38bdf8", is_active=True)
    for cat in order:
        if category_counts.get(cat, 0):
            btns += _btn(cat, f"{labels[cat]} ({category_counts[cat]})", colors[cat])

    return (f'<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;'
            f'background:#0d0d20;padding:12px 16px;border-radius:8px;margin-bottom:16px">'
            f'<span style="color:#888;font-size:0.8rem;margin-right:4px">Decision Filter:</span>'
            f'{btns}</div>')


def build_portfolio_summary(results: list) -> str:
    """Portfolio-level statistics bar — total positions, winners, losers, avg gain, broker breakdown."""
    total = len(results)
    if not total:
        return ""
    avg_gain = sum(r.get("current_gain_pct", 0) for r in results) / total
    winners = sum(1 for r in results if (r.get("current_gain_pct") or 0) > 0)
    losers = sum(1 for r in results if (r.get("current_gain_pct") or 0) < 0)
    flat = total - winners - losers
    # Broker breakdown
    brokers: dict = {}
    for r in results:
        b = r.get("broker", "Unknown")
        brokers[b] = brokers.get(b, 0) + 1
    broker_pills = " ".join(
        f'<span style="background:#1a1a2e;border:1px solid #2d2d4e;padding:3px 10px;'
        f'border-radius:12px;font-size:0.78rem;color:#aaa">{b}: {c}</span>'
        for b, c in sorted(brokers.items(), key=lambda x: -x[1])
    )
    return (
        f'<div style="display:flex;flex-wrap:wrap;gap:16px;background:#0d0d20;'
        f'padding:14px 20px;border-radius:10px;margin-bottom:16px;align-items:center">'
        f'<div style="color:#fff;font-weight:700;font-size:0.9rem">'
        f'&#x1F4CA; {total} Positions</div>'
        f'<span style="color:#2ecc71;font-size:0.85rem">&#x25B2; {winners} winners</span>'
        f'<span style="color:#e74c3c;font-size:0.85rem">&#x25BC; {losers} losers</span>'
        f'<span style="color:#888;font-size:0.85rem">&#x2014; {flat} flat</span>'
        f'<span style="color:#aaa;font-size:0.85rem">Avg: {avg_gain:+.1f}%</span>'
        f'<div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">{broker_pills}</div>'
        f'</div>'
    )


# ── Data Coverage Banner ────────────────────────────────────────────────────
_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]

def build_data_coverage_banner(results: list) -> str:
    """Show which brokers contributed data and which are missing."""
    broker_counts: dict[str, int] = {b: 0 for b in _KNOWN_BROKERS}
    for r in results:
        b = r.get("broker", "Unknown")
        if b in broker_counts:
            broker_counts[b] += 1
        else:
            broker_counts.setdefault(b, 0)
            broker_counts[b] += 1

    active  = [f"<span style='color:#4caf50'>{b} ({n})</span>" for b, n in broker_counts.items() if n > 0]
    missing = [f"<span style='color:#f59e0b;opacity:.6'>{b} (offline)</span>" for b, n in broker_counts.items() if n == 0 and b in _KNOWN_BROKERS]

    if not missing:
        return ""  # all brokers reporting — no banner needed

    parts = " &nbsp;·&nbsp; ".join(active + missing)
    return (
        '<div style="background:#1a1200;border:1px solid #f59e0b33;border-radius:8px;'
        'padding:10px 16px;margin-bottom:16px;font-size:0.82rem;color:#ccc">'
        f'<strong style="color:#f59e0b">⚠ Broker Coverage:</strong> {parts}'
        '</div>'
    )


def render_html(results: list, vix: float, generated_at: str, positions_file: str) -> str:
    cards         = "\n".join(render_card(r) for r in results)
    summary       = build_summary_bar(results, vix)
    hedge_banner  = build_portfolio_hedge_banner(results, vix)
    grade_filt    = build_grade_filter(results)
    size_filt     = build_size_filter(results)
    decision_filt = build_decision_filter(results)
    legend        = build_legend()
    top5          = build_top5_urgent(results)
    port_summary  = build_portfolio_summary(results)
    coverage      = build_data_coverage_banner(results)
    search_bar    = (
        '<div style="display:flex;align-items:center;gap:10px;background:#0d0d20;'
        'padding:12px 16px;border-radius:8px;margin-bottom:16px">'
        '<input id="searchInput" type="text" placeholder="Filter by symbol or broker..." '
        'oninput="filterSearch()" style="flex:1;max-width:360px;background:#1e293b;'
        'border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:6px 12px;'
        'font-size:13px;outline:none">'
        '<span id="matchCount" style="color:#64748b;font-size:12px"></span>'
        '</div>'
    )
    sort_bar      = (
        '<div class="sort-bar">'
        '<span style="color:#666;font-size:0.82rem">Sort:</span>'
        '<button type="button" class="srt-btn active" onclick="sortCards(this,\'urgency\')">Urgency</button>'
        '<button type="button" class="srt-btn" onclick="sortCards(this,\'gain\')">Gain ↑</button>'
        '<button type="button" class="srt-btn" onclick="sortCards(this,\'r_multiple\')">R-Multiple ↑</button>'
        '<button type="button" class="srt-btn" onclick="sortCards(this,\'conviction\')">Conviction ↓</button>'
        '<button type="button" class="srt-btn" onclick="sortCards(this,\'held\')">Held Days ↑</button>'
        '<button type="button" class="srt-btn" onclick="sortCards(this,\'broker\')">Broker</button>'
        '<button type="button" class="srt-btn" onclick="sortCards(this,\'name\')">Name</button>'
        '</div>'
    )

    tmpl = _jinja_env.get_template("sell_report.html")
    return tmpl.render(
        generated_at=generated_at,
        positions_file=positions_file,
        top5=top5,
        port_summary=port_summary,
        summary=summary,
        hedge_banner=hedge_banner,
        coverage=coverage,
        search_bar=search_bar,
        grade_filt=grade_filt,
        size_filt=size_filt,
        decision_filt=decision_filt,
        sort_bar=sort_bar,
        legend=legend,
        cards=cards,
    )


# ─── Database helpers (Task 5) ─────────────────────────────────────────────────

_SELL_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sell_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts          TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    broker          TEXT,
    action          TEXT,
    action_reason   TEXT,
    entry_date      TEXT,
    entry_price     REAL,
    current_price   REAL,
    gain_pct        REAL,
    current_r       REAL,
    vix             REAL,
    conviction_score REAL,
    hedge_urgency   TEXT,
    holding_term    TEXT,
    cgt_eligible    INTEGER
);
"""

def _store_run_to_db(results: list, run_ts: datetime, db_path: Path) -> None:
    """Persist this run's sell decisions to SQLite for historical tracking."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ts_str = run_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.executescript(_SELL_DB_SCHEMA)
        # Wire into versioned migration system for future schema evolution
        try:
            _root_dir = str(db_path.parent.parent)
            import sys as _sys
            if _root_dir not in _sys.path:
                _sys.path.insert(0, _root_dir)
            from database.migrations import run_migrations
            run_migrations("sell_history", conn)
        except Exception:
            pass  # migrations module unavailable (standalone use)
        rows = [
            (
                ts_str,
                r.get("symbol"), r.get("broker"),
                r.get("action"), r.get("action_reason"),
                r.get("entry_date"),
                r.get("entry_price"), r.get("current_price"),
                r.get("current_gain_pct"), r.get("current_r"),
                r.get("vix"), r.get("conviction_score"),
                r.get("hedge_urgency"), r.get("holding_term"),
                int(bool(r.get("cgt_eligible"))),
            )
            for r in results
        ]
        conn.executemany(
            "INSERT INTO sell_events (run_ts,symbol,broker,action,action_reason,"
            "entry_date,entry_price,current_price,gain_pct,current_r,vix,"
            "conviction_score,hedge_urgency,holding_term,cgt_eligible) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _query_history(symbol: str, db_path: Path, limit: int = 10) -> list:
    """Return the last *limit* sell events for *symbol* (newest first)."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM sell_events WHERE symbol=? ORDER BY run_ts DESC LIMIT ?",
            (symbol, limit),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ─── Archive helpers (Task 6) ──────────────────────────────────────────────────

def _archive_report(html: str, results: list, vix: float, run_ts: datetime) -> Path:
    """Copy HTML report to a timestamped archive folder and update the index."""
    folder_name = run_ts.strftime("%Y-%m-%d_%H-%M")
    archive_dir = _ARCHIVE_ROOT / folder_name
    archive_dir.mkdir(parents=True, exist_ok=True)
    report_path = archive_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")

    # Build / rebuild index
    _update_archive_index(results, vix, run_ts, folder_name)
    return report_path


def _update_archive_index(results: list, vix: float, run_ts: datetime, folder_name: str) -> None:
    """Maintain a table-of-contents index.html in the archive root."""
    index_path = _ARCHIVE_ROOT / "index.html"
    # Read existing entries
    entries: list[dict] = []
    if index_path.exists():
        raw = index_path.read_text(encoding="utf-8")
        # cheap parse: extract JSON payload embedded as a comment
        m = re.search(r"<!-- ENTRIES_JSON:(.*?)-->" , raw, re.DOTALL)
        if m:
            try:
                entries = json.loads(m.group(1).strip())
            except Exception:
                entries = []
    # Append new entry (duplicate guard by folder_name)
    action_counts = {}
    for r in results:
        a = r.get("action", "HOLD")
        action_counts[a] = action_counts.get(a, 0) + 1
    new_entry = {
        "folder": folder_name,
        "ts":     run_ts.strftime("%Y-%m-%d %H:%M UTC"),
        "total":  len(results),
        "sell":   action_counts.get("SELL", 0),
        "reduce": action_counts.get("REDUCE", 0),
        "trail":  action_counts.get("TRAIL", 0),
        "hold":   action_counts.get("HOLD", 0),
        "vix":    round(vix, 1),
    }
    entries = [e for e in entries if e.get("folder") != folder_name]
    entries.insert(0, new_entry)
    entries = entries[:100]  # keep last 100 runs

    rows_html = ""
    for e in entries:
        link = f'./{e["folder"]}/report.html'
        rows_html += (
            f"<tr>"
            f"<td><a href='{link}'>{e['ts']}</a></td>"
            f"<td style='text-align:center'>{e['total']}</td>"
            f"<td style='text-align:center;color:#e63946'>{e['sell']}</td>"
            f"<td style='text-align:center;color:#f9c74f'>{e['reduce']}</td>"
            f"<td style='text-align:center;color:#3a86ff'>{e['trail']}</td>"
            f"<td style='text-align:center;color:#1db954'>{e['hold']}</td>"
            f"<td style='text-align:center'>{e['vix']}</td>"
            f"</tr>\n"
        )
    entries_json = json.dumps(entries)
    index_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sell Strategy — Report Archive</title>
<style>
  body{{background:#0f0f1a;color:#e0e0e0;font-family:'Segoe UI',sans-serif;padding:24px}}
  h1{{color:#fff;margin-bottom:16px}}
  table{{border-collapse:collapse;width:100%}}
  th{{background:#1a1a2e;padding:8px 14px;text-align:left;color:#888;font-size:0.85rem}}
  td{{padding:8px 14px;border-top:1px solid #2d2d4e;font-size:0.9rem}}
  a{{color:#3a86ff;text-decoration:none}} a:hover{{text-decoration:underline}}
</style>
</head><body>
<h1>Sell Strategy — Report Archive</h1>
<table>
<thead><tr>
  <th scope='col'>Run Time</th><th scope='col'>Positions</th><th scope='col'>SELL</th><th scope='col'>REDUCE</th><th scope='col'>TRAIL</th><th scope='col'>HOLD</th><th scope='col'>VIX</th>
</tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
<!-- ENTRIES_JSON:{entries_json}-->
</body></html>"""
    _ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    index_path.write_text(index_html, encoding="utf-8")


# ─── CSV export helper (Task 10) ───────────────────────────────────────────────

_CSV_FIELDS = [
    "symbol", "broker", "action", "action_reason",
    "entry_price", "current_price", "current_gain_pct", "current_r",
    "conviction_score", "hedge_urgency", "holding_term", "cgt_eligible",
    "entry_date", "vix",
]

def _write_csv(results: list, csv_path: Path) -> None:
    """Save key sell decision fields as CSV."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, "") for k in _CSV_FIELDS})


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sell Strategy — Position Exit Decision Engine")
    parser.add_argument("--positions", default=str(POSITIONS_FILE),
                        help="Path to positions JSON file")
    parser.add_argument("--out", default=str(OUTPUT_HTML),
                        help="Output HTML report path")
    parser.add_argument("--demo", action="store_true",
                        help="Run with built-in demo positions (ignores --positions)")
    parser.add_argument("--live", action="store_true",
                        help="Fetch positions live from Moomoo OpenD (falls back to positions.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyse positions but do NOT write HTML/DB/archive — prints summary only")
    parser.add_argument("--symbol", metavar="SYM",
                        help="Analyse only this symbol (case-insensitive match on symbol field)")
    parser.add_argument("--sort", default="urgency",
                        choices=["urgency", "gain", "r_multiple", "conviction"],
                        help="Sort results by: urgency (default), gain, r_multiple, conviction")
    parser.add_argument("--json-out", metavar="PATH",
                        help="Save results as JSON to PATH (for downstream use)")
    parser.add_argument("--no-db", action="store_true",
                        help="Skip writing sell events to the SQLite history database")
    parser.add_argument("--no-archive", action="store_true",
                        help="Skip archiving the HTML report")
    parser.add_argument("--csv-out", metavar="PATH",
                        help="Save key result fields as CSV to PATH")
    parser.add_argument("--no-signal-insight", action="store_true",
                        help="Skip L1-L4 signal engine analysis (faster, no insight section in report)")
    parser.add_argument("--morning", action="store_true",
                        help="Non-interactive morning mode: --live with all defaults. Zero prompts.")
    args = parser.parse_args()
    # --morning overrides
    if args.morning:
        args.live = True

    # ── Load positions ─────────────────────────────────────────────────────────
    if args.demo:
        positions = DEMO_POSITIONS
        pos_label = "demo (built-in)"
        print("[INFO] Running in DEMO mode with built-in positions")
    elif args.live:
        print("[INFO] Fetching live positions from Moomoo OpenD + IBKR + India ...")
        moomoo_pos = _fetch_live_positions()
        ibkr_pos   = _fetch_ibkr_positions()
        india_pos, zerodha_n, angelone_n = _fetch_india_positions()
        webull_pos = _fetch_webull_positions()
        positions  = _combine_broker_positions(moomoo_pos, ibkr_pos, india_pos, webull_pos)
        if not positions:
            print("[WARN] No live positions returned — falling back to positions.json")
            pos_path = Path(args.positions)
            if not pos_path.exists():
                print(f"[ERROR] Fallback file not found: {pos_path}")
                sys.exit(1)
            with open(pos_path, encoding="utf-8") as f:
                positions = json.load(f)
            pos_label = f"fallback file: {pos_path}"
            print(f"[INFO] Loaded {len(positions)} positions from {pos_label}")
        else:
            pos_label = (f"live Moomoo({len(moomoo_pos)}) + IBKR({len(ibkr_pos)})"
                         f" + Zerodha({zerodha_n}) + AngelOne({angelone_n})"
                         f" + Webull({len(webull_pos)})")
    else:
        pos_path = Path(args.positions)
        if not pos_path.exists():
            print(f"[ERROR] Positions file not found: {pos_path}")
            print("        Run with --live for live Moomoo data, or --demo for built-in positions")
            sys.exit(1)
        with open(pos_path, encoding="utf-8") as f:
            positions = json.load(f)
        pos_label = str(pos_path)
        print(f"[INFO] Loaded {len(positions)} positions from {pos_label}")

    # ── Filter by --symbol if requested ──────────────────────────────────────
    if args.symbol:
        sym_upper = args.symbol.upper()
        positions = [p for p in positions if p.get("symbol", "").upper() == sym_upper]
        if not positions:
            print(f"[WARN] No position found matching --symbol '{args.symbol}'")
            sys.exit(1)

    now = datetime.now(timezone.utc)

    # ── Fetch VIX ──────────────────────────────────────────────────────────────
    print("[INFO] Fetching VIX...")
    vix = fetch_vix()
    vix_regime = "EXTREME_FEAR" if vix >= 35 else "FEAR" if vix >= 25 else "NORMAL"
    print(f"  VIX = {vix:.1f} ({vix_regime})")

    # ── Batch TV Screener ──────────────────────────────────────────────────────
    symbols = [p["symbol"] for p in positions]
    print(f"[INFO] Fetching TV Screener data for {len(symbols)} symbols...")
    screener = fetch_tv_screener(symbols)
    sc_hits = sum(1 for s in symbols if s in screener)
    print(f"  Screener data: {sc_hits}/{len(symbols)} matched")

    # ── Analyse each position ──────────────────────────────────────────────────
    print("[INFO] Analysing positions...")
    results = []
    for i, pos in enumerate(positions, 1):
        sym = pos["symbol"]
        print(f"  [{i}/{len(positions)}] {sym} ...", end=" ", flush=True)
        try:
            r = analyse_position(pos, screener, vix, now)
            results.append(r)
            cp = r.get("current_price")
            action = r.get("action", "?")
            gain = r.get("current_gain_pct", 0)
            r_val = r.get("current_r")
            r_str = f"{r_val:+.2f}R" if r_val is not None else "N/A"
            print(f"price={cp or 'N/A'} gain={gain:+.1f}% {r_str} --> {action}")
        except Exception as e:
            print(f"ERROR: {e}")
            traceback.print_exc()

    # ── Signal Engine Insight enrichment (L1-L4) ───────────────────────────────
    if _SIGNAL_INSIGHT_OK and not getattr(args, "no_signal_insight", False):
        print(f"[INFO] Enriching {len(results)} positions with signal engine (L1-L4) ...")
        # Batch-fetch TV fundamentals once for all symbols
        try:
            tv_fund_cache = _ANALYSIS_ENGINE.fetch_tv_fundamentals(
                [r["symbol"] for r in results], use_auth=True
            )
        except Exception as _tf_exc:
            print(f"  [WARN] TV fund batch fetch failed: {_tf_exc}")
            tv_fund_cache = {}
        try:
            yc = _ANALYSIS_ENGINE.fetch_yield_curve()
        except Exception:
            yc = {}
        for i, r in enumerate(results, 1):
            sym = r["symbol"]
            print(f"  [{i}/{len(results)}] signal insight: {sym} ...", end=" ", flush=True)
            try:
                r["signal_insight"] = _fetch_signal_insight(
                    sym, screener_cache=screener,
                    tv_fund_cache=tv_fund_cache, yc=yc,
                )
                grade = r["signal_insight"].get("_grade", "?")
                print(f"grade={grade}")
            except Exception as _si_exc:
                r["signal_insight"] = {"_error": str(_si_exc)}
                print(f"error: {_si_exc}")
    else:
        if not _SIGNAL_INSIGHT_OK:
            print("[INFO] Signal engine not available — skipping insight enrichment")
        else:
            print("[INFO] Signal insight disabled — pyramid adds will fail closed")

    for r in results:
        _apply_signal_confirmation(r)

    # ── Sort results ───────────────────────────────────────────────────────────
    _sort = args.sort
    action_order = {"SELL": 0, "REDUCE": 1, "REVIEW": 2, "TRAIL": 3, "HOLD": 4}
    if _sort == "urgency":
        results.sort(key=lambda r: action_order.get(r.get("action", "HOLD"), 5))
    elif _sort == "gain":
        results.sort(key=lambda r: r.get("current_gain_pct", 0))
    elif _sort == "r_multiple":
        results.sort(key=lambda r: (r.get("current_r") or 0))
    elif _sort == "conviction":
        results.sort(key=lambda r: -(r.get("conviction_score") or 0))

    # ── Generate HTML ──────────────────────────────────────────────────────────
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    html = render_html(results, vix, generated_at, pos_label)

    # P2-A: Always write HTML report (even in dry-run). Only gate DB writes.
    out_path = Path(args.out)
    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out_path)
    except Exception:
        pass
    out_path.write_text(html, encoding="utf-8")
    size_kb = out_path.stat().st_size // 1024
    print(f"\n[OK] Report saved to {out_path}  ({size_kb}KB)")

    # ── Companion JSON (Golden Rule #20: every HTML report gets a sibling .json) ──
    json_companion = out_path.with_suffix(".json")
    _json_payload = {
        "generated_at": now.isoformat(),
        "vix": vix,
        "vix_regime": vix_regime,
        "source": pos_label,
        "total_positions": len(results),
        "action_counts": {
            a: sum(1 for r in results if r.get("action") == a)
            for a in ("SELL", "REDUCE", "REVIEW", "TRAIL", "HOLD")
        },
        "rows": results,
    }
    def _json_default(obj):
        """Convert numpy/pandas types to native Python for JSON serialization."""
        import numpy as _np
        if isinstance(obj, (_np.bool_, bool)):
            return bool(obj)
        if isinstance(obj, (_np.integer,)):
            return int(obj)
        if isinstance(obj, (_np.floating,)):
            return float(obj)
        return str(obj)
    json_companion.write_text(json.dumps(_json_payload, indent=2, default=_json_default), encoding="utf-8")
    print(f"[OK] JSON companion → {json_companion}")

    if not args.dry_run:
        # ── Store events to SQLite DB ──────────────────────────────────────────
        if not args.no_db:
            try:
                _store_run_to_db(results, now, _SELL_DB_PATH)
                print(f"[OK] Events stored to DB: {_SELL_DB_PATH}")
            except Exception as exc:
                print(f"[WARN] DB write failed: {exc}")

            # ── Persist summary to report analytics DB ─────────────────────────
            try:
                _rdb_path = str(ROOT.parent / "reports")
                if _rdb_path not in sys.path:
                    sys.path.insert(0, _rdb_path)
                from report_db import save_sell_strategy_snapshot
                save_sell_strategy_snapshot(results, vix, vix_regime)
            except Exception as _rdb_exc:
                print(f"[WARN] report_db snapshot not saved: {_rdb_exc}")

        # ── Archive HTML report ────────────────────────────────────────────────
        if not args.no_archive:
            try:
                archive_path = _archive_report(html, results, vix, now)
                print(f"[OK] Archived to {archive_path}")
            except Exception as exc:
                print(f"[WARN] Archive failed: {exc}")

        # ── CSV output ────────────────────────────────────────────────────────
        if args.csv_out:
            try:
                _write_csv(results, Path(args.csv_out))
                print(f"[OK] CSV saved to {args.csv_out}")
            except Exception as exc:
                print(f"[WARN] CSV write failed: {exc}")

        # ── JSON output ───────────────────────────────────────────────────────
        if args.json_out:
            json_path = Path(args.json_out)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(json_path, "w", encoding="utf-8") as fj:
                json.dump(results, fj, indent=2, default=str)
            print(f"[OK] JSON results saved to {json_path}")
    else:
        print("\n[DRY-RUN] DB, archive, CSV, and JSON NOT written (--dry-run). HTML report was written.")

    # ── Terminal summary ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  ACTION SUMMARY  |  VIX {vix:.1f} ({vix_regime})")
    print("=" * 60)

    _act_grp = {"SELL": 0, "REDUCE": 1, "REVIEW": 2, "TRAIL": 3, "HOLD": 4}

    def _urgency_key(r):
        act = r["action"]
        cr  = r.get("current_r") or 0
        grp = _act_grp.get(act, 5)
        return (grp, cr if act == "SELL" else -cr)  # SELL: worst R first; rest: best R first

    sorted_results = sorted(results, key=_urgency_key)
    last_group = None
    group_labels = {0: "SELL", 1: "REDUCE", 2: "REVIEW", 3: "TRAIL", 4: "HOLD"}
    for r in sorted_results:
        act = r["action"]
        cr  = r.get("current_r") or 0
        group = _act_grp.get(act, 4)
        if group != last_group:
            last_group = group
            print(f"  --- {group_labels[group]} ---")
        sym    = r["symbol"].ljust(20)
        act_s  = act.ljust(7)
        gain   = f'{r["current_gain_pct"]:+6.1f}%'
        r_s    = f'{cr:+.2f}R' if r.get("current_r") is not None else ' N/A '
        reason = r["action_reason"][:40]
        print(f"  {sym} {act_s} {gain}  {r_s}  {reason}")
    print("=" * 60)


if __name__ == "__main__":
    main()
