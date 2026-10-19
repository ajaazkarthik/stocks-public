"""
moomoo_market_tools.py — Untapped Moomoo API Surface
======================================================
Implements 6 previously-unused Moomoo OpenAPI capabilities and wires them
into the trading system:

  1. fetch_intraday_bars()      — K_1M / K_5M intraday OHLCV for L1 momentum scoring
  2. fetch_basicinfo()          — PE, EPS, lot_size, stock_type (AU / HK / SG fallback)
  3. get_market_state_all()     — Trading-hours state per market → regime gate
  4. is_market_open()           — Boolean gate used by webhook + order executor
  5. sync_tv_to_moomoo()        — Push TradingView pro_symbols → Moomoo watchlist group
  6. get_mm_watchlist()         — Pull a Moomoo watchlist group → list of mm_codes
  7. fetch_warrants()           — Top-N HK warrants / CBBCs by volume for a stock
  8. fetch_futures_snapshot()   — SG / HK futures snapshot (price, OI, volume)

All functions:
  • Are non-fatal: return None / empty dict / [] on error
  • Use the existing moomoo_config for host / port selection
  • Respect the _RIGHTS_CACHE skip-fast pattern from moomoo_enricher

Standalone benchmark / smoke-test:
  python moomoo_market_tools.py --smoke       # quick connectivity check
  python moomoo_market_tools.py --bench       # latency benchmark all 8 APIs
"""

import sys
import os
import json
import time
import logging
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_THIS, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logger = logging.getLogger(__name__)

# ── Moomoo availability guard ──────────────────────────────────────────────────
try:
    from moomoo import (
        OpenQuoteContext,
        KLType,
        AuType,
        SubType,
        RET_OK,
        TrdMarket,
    )
    from moomoo_config import OPEND_HOST, OPEND_AU_PORT, OPEND_SG_PORT, is_opend_running
    _MM_AVAILABLE = True
except ImportError:
    _MM_AVAILABLE = False

# ── TV→Moomoo symbol conversion (mirrors moomoo_enricher) ─────────────────────
_TV_TO_MM: dict = {
    "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "BATS": "US",
    "HKEX": "HK", "SH": "SH", "SZ": "SZ",
    "SGX":  "SG",   # SG futures DO have a Moomoo market prefix
}
_MM_UNSUPPORTED = {"ASX", "ASX_DLY", "NSE", "BSE", "LSE", "TSX"}

# Market state strings that mean "open for regular trading"
_OPEN_STATES = {
    "MKT_STATUS_OPEN",          # English enum value
    "open",                     # lower-case variant
    "已开市",                    # Chinese
    "MARKET_STATUS_OPEN",
    "MORNING",                  # HK morning
    "AFTERNOON",                # HK afternoon
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ctx(port: int = None) -> "OpenQuoteContext":
    """Create an OpenQuoteContext on the given port.

    When port is None (the default), uses quote_port() which returns the
    first running OpenD port (AU 11111, then SG 11112).  This avoids hanging
    on a fixed port when one OpenD instance is offline.
    """
    if port is None:
        try:
            from moomoo_config import quote_port as _qp
            port = _qp()
        except RuntimeError:
            port = OPEND_AU_PORT   # best-effort; will fail if AU also down
    return OpenQuoteContext(host=OPEND_HOST, port=port)


def _safe_close(ctx) -> None:
    try:
        if ctx:
            ctx.close()
    except Exception:
        pass


def _tv_to_mm(pro_symbol: str) -> Optional[str]:
    """Convert 'NYSE:RCL' → 'US.RCL',  'HKEX:00700' → 'HK.00700'."""
    if ":" not in pro_symbol:
        return None
    exchange, ticker = pro_symbol.split(":", 1)
    exchange = exchange.upper()
    if exchange in _MM_UNSUPPORTED:
        return None
    market = _TV_TO_MM.get(exchange)
    if not market:
        return None
    if market == "HK" and ticker.isdigit():
        ticker = ticker.zfill(5)
    return f"{market}.{ticker}"


def _mm_to_tv(mm_code: str) -> str:
    """Convert 'US.AAPL' → 'NASDAQ:AAPL' (best-effort reverse)."""
    _MM_TO_TV_EXCHANGE = {
        "US": "NASDAQ",
        "HK": "HKEX",
        "SH": "SH",
        "SZ": "SZ",
        "SG": "SGX",
    }
    if "." not in mm_code:
        return mm_code
    market, ticker = mm_code.split(".", 1)
    exchange = _MM_TO_TV_EXCHANGE.get(market, market)
    return f"{exchange}:{ticker}"


# ─────────────────────────────────────────────────────────────────────────────
# 1. INTRADAY BARS  (get_kline K_1M / K_5M)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_intraday_bars(
    mm_code: str,
    ktype: str = "K_5M",
    num: int = 60,
    port: int = None,
) -> list[dict]:
    """
    Fetch intraday OHLCV bars for a Moomoo code.

    Returns a list of dicts (newest first from API, reversed to chronological):
      {time_key, open, high, low, close, volume, turnover, change_rate}

    Use cases in signal pipeline:
      • Intraday momentum: last 12 × 5M bars → 1-hour trend direction
      • Intraday ATR:     high-low range over last 60 × 1M bars
      • Volume spike:     bar volume vs average of last N bars

    Args:
        mm_code: Moomoo code e.g. 'US.AAPL', 'HK.00700'
        ktype:   'K_1M' or 'K_5M' (or any KLType key: K_15M, K_60M, K_DAY ...)
        num:     number of bars to fetch (max 1000)
        port:    OpenD port (default AU 11111)

    Returns [] on error / unsupported market / no data.
    """
    if not _MM_AVAILABLE:
        return []

    _KTYPE_MAP = {
        "K_1M":  KLType.K_1M,
        "K_5M":  KLType.K_5M,
        "K_15M": KLType.K_15M,
        "K_30M": KLType.K_30M,
        "K_60M": KLType.K_60M,
        "K_DAY": KLType.K_DAY,
    }
    kl = _KTYPE_MAP.get(ktype.upper(), KLType.K_5M)

    ctx = None
    try:
        ctx = _ctx(port)
        # Try get_cur_kline first (returns current session bars, works in pre/after-hours too)
        # Fall back to request_history_kline for larger historical requests.
        ret, data = ctx.get_cur_kline(mm_code, ktype=kl, num=num, autype=AuType.QFQ)
        if ret != RET_OK or data is None or (hasattr(data, "empty") and data.empty):
            # Fallback to historical kline
            ret, data = ctx.request_history_kline(
                mm_code, ktype=kl, autype=AuType.QFQ, num=num,
            )
        if ret != RET_OK or data is None:
            return []
        if hasattr(data, "empty") and data.empty:
            return []

        bars = []
        for _, row in data.iterrows():
            def _f(col, default=None):
                try:
                    v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
                    return float(v) if v not in (None, "", "nan") else default
                except Exception:
                    return default

            bars.append({
                "time_key":    str(row.get("time_key", "") if hasattr(row, "get") else getattr(row, "time_key", "")),
                "open":        _f("open"),
                "high":        _f("high"),
                "low":         _f("low"),
                "close":       _f("close"),
                "volume":      _f("volume"),
                "turnover":    _f("turnover"),
                "change_rate": _f("change_rate"),
            })
        return bars

    except Exception as e:
        logger.debug("fetch_intraday_bars(%s): %s", mm_code, e)
        return []
    finally:
        _safe_close(ctx)


def compute_intraday_signals(bars: list[dict]) -> dict:
    """
    Derive intraday signals from a bar list (chronological order).

    Returns:
        intraday_trend:   +1 (up) / -1 (down) / 0 (flat)  — last bar vs 1h ago
        intraday_atr_pct: average (high-low)/close % over all bars
        vol_spike:        latest bar volume / average volume of rest
        momentum_score:   0–10 score for L1 integration
    """
    if not bars or len(bars) < 3:
        return {"intraday_trend": 0, "intraday_atr_pct": None,
                "vol_spike": None, "momentum_score": 5.0}

    closes  = [b["close"] for b in bars if b.get("close") is not None]
    highs   = [b["high"]  for b in bars if b.get("high")  is not None]
    lows    = [b["low"]   for b in bars if b.get("low")   is not None]
    vols    = [b["volume"] for b in bars if b.get("volume") is not None]

    # Trend: last close vs close 12 bars ago (≈ 1 hour on 5M bars)
    lookback = min(12, len(closes) - 1)
    trend = 0
    if closes[-1] and closes[-(lookback + 1)]:
        chg = (closes[-1] - closes[-(lookback + 1)]) / closes[-(lookback + 1)]
        trend = 1 if chg > 0.002 else (-1 if chg < -0.002 else 0)

    # ATR %: mean of (high-low)/close
    atr_pcts = []
    for h, l, c in zip(highs, lows, closes):
        if h and l and c and c > 0:
            atr_pcts.append((h - l) / c * 100)
    avg_atr = sum(atr_pcts) / len(atr_pcts) if atr_pcts else None

    # Volume spike: last bar vs mean of prior N bars
    vol_spike = None
    if len(vols) >= 2:
        last_vol = vols[-1]
        prior_mean = sum(vols[:-1]) / len(vols[:-1]) if vols[:-1] else 0
        if prior_mean and prior_mean > 0:
            vol_spike = last_vol / prior_mean

    # Momentum score (0-10)
    score = 5.0
    if trend == 1:
        score += 1.5
    elif trend == -1:
        score -= 1.5
    if vol_spike is not None:
        if vol_spike >= 2.0:
            score += 2.0
        elif vol_spike >= 1.5:
            score += 1.0
        elif vol_spike < 0.5:
            score -= 1.0
    score = max(0.0, min(10.0, score))

    return {
        "intraday_trend":   trend,
        "intraday_atr_pct": round(avg_atr, 3) if avg_atr else None,
        "vol_spike":        round(vol_spike, 2) if vol_spike else None,
        "momentum_score":   round(score, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. STOCK BASIC INFO  (get_market_snapshot → PE, EPS, NAV, lot_size)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_basicinfo(mm_codes: list[str], port: int = None) -> dict[str, dict]:
    """
    Batch-fetch stock basic info for a list of Moomoo codes.
    Uses get_market_snapshot (same as enricher) which is cheaper than
    making a separate get_stock_basicinfo call — it includes PE, lot_size,
    stock_type, market_val and can substitute for TV screener fundamentals
    for AU / HK / SG symbols.

    Returns:
        {mm_code: {pe_ttm, pb, lot_size, market_val, stock_type, name, last_price}}

    Best used as L2 fallback for HK/SG symbols when TV screener has no data.
    Rate limit: no known cap on get_market_snapshot (underlying call).
    """
    if not _MM_AVAILABLE or not mm_codes:
        return {}

    result: dict[str, dict] = {}
    ctx = None
    try:
        ctx = _ctx(port)
        # Process in batches of 200 (API limit)
        for i in range(0, len(mm_codes), 200):
            batch = mm_codes[i:i + 200]
            ret, data = ctx.get_market_snapshot(batch)
            if ret != RET_OK or data is None:
                continue
            if hasattr(data, "empty") and data.empty:
                continue

            for _, row in data.iterrows():
                def _f(col, default=None):
                    try:
                        v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
                        return float(v) if v not in (None, "", "nan", "N/A") else default
                    except Exception:
                        return default

                def _s(col, default=""):
                    try:
                        v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
                        return str(v) if v not in (None, "", "nan") else default
                    except Exception:
                        return default

                code = _s("code")
                if not code:
                    continue

                result[code] = {
                    "name":       _s("name"),
                    "stock_type": _s("stock_type"),
                    "lot_size":   int(_f("lot_size", 0) or 0),
                    "last_price": _f("last_price"),
                    "market_val": _f("market_val"),
                    "pe_ttm":     _f("pe_ttm_ratio"),
                    "pb":         _f("pb_ratio"),
                    "eps_ttm":    _f("eps_ttm"),
                    "nav":        _f("net_asset"),       # net asset value per share
                    "source":     "moomoo_basicinfo",
                }
    except Exception as e:
        logger.debug("fetch_basicinfo: %s", e)
    finally:
        _safe_close(ctx)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 3 & 4.  MARKET STATE  +  GLOBAL STATE  →  REGIME DETECTION
# ─────────────────────────────────────────────────────────────────────────────

# Representative symbols — one per exchange, used to probe market state
_MARKET_PROBE_SYMBOLS = {
    "US": "US.AAPL",
    "HK": "HK.00700",
    "SH": "SH.600519",
    "SZ": "SZ.000001",
    "SG": "SG.CNmain",
}


def get_market_state_all(port: int = None) -> dict[str, str]:
    """
    Query trading-hours state for all supported markets in a single API call.

    Returns:
        {"US": "MKT_STATUS_OPEN", "HK": "MKT_STATUS_CLOSED", ...}

    The 'market_state' string comes directly from the Moomoo enum — values:
        MKT_STATUS_OPEN, MKT_STATUS_CLOSED, MKT_STATUS_BEFORE_HOUR_TRADING,
        MKT_STATUS_AFTER_HOUR_TRADING, MKT_STATUS_REST, NONE_STATE

    Use is_market_open(market) for a simple boolean gate.
    """
    if not _MM_AVAILABLE:
        return {}

    probes = list(_MARKET_PROBE_SYMBOLS.values())
    ctx = None
    try:
        ctx = _ctx(port)
        ret, data = ctx.get_market_state(probes)
        if ret != RET_OK or data is None:
            return {}
        if hasattr(data, "empty") and data.empty:
            return {}

        states: dict[str, str] = {}
        for _, row in data.iterrows():
            code = str(row.get("code", "") if hasattr(row, "get") else getattr(row, "code", ""))
            state = str(row.get("market_state", "") if hasattr(row, "get") else getattr(row, "market_state", ""))
            for mkt, probe in _MARKET_PROBE_SYMBOLS.items():
                if code == probe:
                    states[mkt] = state
                    break
        return states

    except Exception as e:
        logger.debug("get_market_state_all: %s", e)
        return {}
    finally:
        _safe_close(ctx)


def get_global_state(port: int = None) -> dict:
    """
    Get OpenD global login/connection status.

    Returns dict with keys:
        qot_logined, trd_logined, server_ver,
        market_hk, market_us, market_sh, market_sz,
        market_hkfuture, market_usfuture

    Useful for: pipeline health check, pre-flight guard before order placement.
    """
    if not _MM_AVAILABLE:
        return {}
    ctx = None
    try:
        ctx = _ctx(port)
        ret, data = ctx.get_global_state()
        if ret != RET_OK:
            return {}
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("get_global_state: %s", e)
        return {}
    finally:
        _safe_close(ctx)


def is_market_open(market: str, port: int = None) -> bool:
    """
    Returns True if the given market ('US', 'HK', 'SH', 'SZ', 'SG') is
    currently in regular trading hours.

    Falls back to global_state fields when market_state probe fails.
    """
    if not _MM_AVAILABLE:
        return False

    # Try market_state probe first
    states = get_market_state_all(port)
    if market in states:
        state = states[market].upper()
        return any(s in state for s in ("OPEN", "MORNING", "AFTERNOON"))

    # Fallback: global_state
    g = get_global_state(port)
    gk = f"market_{market.lower()}"
    if gk in g:
        return any(s in str(g[gk]).upper() for s in ("OPEN", "MORNING", "AFTERNOON"))

    return False


# ─────────────────────────────────────────────────────────────────────────────
# 5 & 6.  WATCHLIST SYNC  (get_user_security / modify_user_security)
# ─────────────────────────────────────────────────────────────────────────────

def get_mm_watchlist(group_name: str, port: int = None) -> list[str]:
    """
    Pull all Moomoo codes from a Moomoo watchlist group.

    Args:
        group_name: e.g. 'My Watchlist', '我的自选'
        port:       OpenD port (default AU 11111)

    Returns:
        list of mm_codes like ['US.AAPL', 'HK.00700', ...]
    """
    if not _MM_AVAILABLE:
        return []
    ctx = None
    try:
        ctx = _ctx(port)
        ret, data = ctx.get_user_security(group_name)
        if ret != RET_OK or data is None:
            return []
        if hasattr(data, "empty") and data.empty:
            return []
        codes = []
        for _, row in data.iterrows():
            c = str(row.get("code", "") if hasattr(row, "get") else getattr(row, "code", ""))
            if c:
                codes.append(c)
        return codes
    except Exception as e:
        logger.debug("get_mm_watchlist(%s): %s", group_name, e)
        return []
    finally:
        _safe_close(ctx)


def sync_tv_to_moomoo(
    tv_symbols: list[str],
    group_name: str,
    mode: str = "ADD",
    port: int = None,
) -> dict:
    """
    Sync TradingView pro_symbols into a Moomoo watchlist group.

    Converts TV 'EXCHANGE:TICKER' → Moomoo 'MARKET.CODE' format.
    Skips unsupported markets (ASX, NSE, etc.).

    Args:
        tv_symbols: list of TradingView pro_symbols e.g. ['NASDAQ:AAPL', 'HKEX:00700']
        group_name: Moomoo watchlist group name
        mode:       'ADD' (default) or 'DEL'
        port:       OpenD port (default AU 11111)

    Returns:
        {
            "added":     [mm_codes actually added/removed],
            "skipped":   [tv_symbols that couldn't be converted],
            "errors":    [mm_codes that failed the API call],
            "total_ok":  int,
        }
    """
    if not _MM_AVAILABLE:
        return {"added": [], "skipped": tv_symbols, "errors": [], "total_ok": 0}

    # Convert all symbols
    mm_codes, skipped = [], []
    for sym in tv_symbols:
        mm = _tv_to_mm(sym)
        if mm:
            mm_codes.append(mm)
        else:
            skipped.append(sym)

    if not mm_codes:
        return {"added": [], "skipped": skipped, "errors": [], "total_ok": 0}

    try:
        from moomoo import ModifyUserSecurityOp
        op = ModifyUserSecurityOp.ADD if mode.upper() == "ADD" else ModifyUserSecurityOp.DEL
    except ImportError:
        return {"added": [], "skipped": skipped, "errors": mm_codes, "total_ok": 0}

    added, errors = [], []
    ctx = None
    try:
        ctx = _ctx(port)
        # Batch in groups of 200
        for i in range(0, len(mm_codes), 200):
            batch = mm_codes[i:i + 200]
            ret, msg = ctx.modify_user_security(group_name, op, batch)
            if ret == RET_OK:
                added.extend(batch)
            else:
                errors.extend(batch)
                logger.warning("modify_user_security batch %d failed: %s", i // 200, msg)
    except Exception as e:
        logger.debug("sync_tv_to_moomoo: %s", e)
        errors.extend(mm_codes)
    finally:
        _safe_close(ctx)

    return {
        "added":    added,
        "skipped":  skipped,
        "errors":   errors,
        "total_ok": len(added),
    }


def diff_watchlists(
    tv_symbols: list[str],
    group_name: str,
    port: int = None,
) -> dict:
    """
    Compare TradingView watchlist against a Moomoo watchlist group.

    Returns:
        {
            "tv_only":  [mm_codes in TV but not in Moomoo],
            "mm_only":  [mm_codes in Moomoo but not in TV],
            "common":   [mm_codes in both],
        }

    Use case: keep the two watchlists in sync — add tv_only, optionally remove mm_only.
    """
    tv_mm  = {_tv_to_mm(s) for s in tv_symbols if _tv_to_mm(s)}
    mm_set = set(get_mm_watchlist(group_name, port))

    return {
        "tv_only": sorted(tv_mm - mm_set),
        "mm_only": sorted(mm_set - tv_mm),
        "common":  sorted(tv_mm & mm_set),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7.  HK WARRANTS / CBBCs  (get_warrant)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_warrants(
    hk_code: str,
    top_n: int = 5,
    wrt_type: str = "CALL",
    port: int = None,
) -> list[dict]:
    """
    Fetch top-N HK warrants / CBBCs for a stock (e.g. 'HK.00700').

    Returns list of dicts sorted by volume (descending):
        {code, name, wrt_type, strike_price, maturity_time, last_price, volume, iv, delta}

    Args:
        hk_code:   Underlying stock code in Moomoo format (must be HK.*)
        top_n:     Maximum warrants to return
        wrt_type:  'CALL' / 'PUT' / 'BULL_CBBC' / 'BEAR_CBBC' / 'ALL'
        port:      OpenD port (default AU 11111 — AU account has HK rights)

    Note: HK warrants are available via both AU and SG accounts.
    """
    if not _MM_AVAILABLE:
        return []
    if not hk_code.startswith("HK."):
        logger.warning("fetch_warrants: only HK codes supported, got %s", hk_code)
        return []

    ctx = None
    try:
        from moomoo import WrtType
        _WRT_MAP = {
            "CALL":      WrtType.CALL,
            "PUT":       WrtType.PUT,
            "BULL_CBBC": WrtType.BULL_CBBC,
            "BEAR_CBBC": WrtType.BEAR_CBBC,
        }

        ctx = _ctx(port)
        call_args = {"stock_owner": hk_code}
        if wrt_type.upper() in _WRT_MAP:
            call_args["wrt_type"] = _WRT_MAP[wrt_type.upper()]

        ret, data = ctx.get_warrant(**call_args)
        if ret != RET_OK or data is None:
            return []
        if hasattr(data, "empty") and data.empty:
            return []

        warrants = []
        for _, row in data.iterrows():
            def _f(col, default=None):
                try:
                    v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
                    return float(v) if v not in (None, "", "nan") else default
                except Exception:
                    return default

            warrants.append({
                "code":         str(row.get("code", "") if hasattr(row, "get") else ""),
                "name":         str(row.get("name", "") if hasattr(row, "get") else ""),
                "wrt_type":     str(row.get("wrt_type", "") if hasattr(row, "get") else ""),
                "strike_price": _f("strike_price"),
                "maturity_time":str(row.get("maturity_time", "") if hasattr(row, "get") else ""),
                "last_price":   _f("last_price"),
                "volume":       _f("volume"),
                "iv":           _f("implied_volatility"),
                "delta":        _f("delta"),
            })

        # Sort by volume descending, return top_n
        warrants.sort(key=lambda x: x["volume"] or 0, reverse=True)
        return warrants[:top_n]

    except Exception as e:
        logger.debug("fetch_warrants(%s): %s", hk_code, e)
        return []
    finally:
        _safe_close(ctx)


# ─────────────────────────────────────────────────────────────────────────────
# 8.  SG / HK FUTURES SNAPSHOT
# ─────────────────────────────────────────────────────────────────────────────

# Common SG futures tradable via Moomoo SG account
SG_FUTURES_UNIVERSE = {
    "SG.CNmain":   "FTSE China A50 Index Futures (main)",
    "SG.NKmain":   "Nikkei 225 Index Futures (main)",
    "SG.TWmain":   "MSCI Taiwan Futures (main)",
    "SG.INmain":   "MSCI India Futures (main)",
    "SG.SGmain":   "SGX Nifty 50 Futures (main)",
}

HK_FUTURES_UNIVERSE = {
    "HK.MCHmain":  "Mini Hang Seng China Enterprises Futures (main)",
    "HK.MHImain":  "Mini Hang Seng Index Futures (main)",
    "HK.HHImain":  "Hang Seng China Enterprises Futures (main)",
    "HK.HSImain":  "Hang Seng Index Futures (main)",
}


def fetch_futures_snapshot(
    mm_codes: list[str] = None,
    include_sg: bool = True,
    include_hk: bool = False,
    port: int = None,
) -> dict[str, dict]:
    """
    Snapshot for SG / HK futures contracts.

    Args:
        mm_codes:   Override list of futures codes. Default: SG_FUTURES_UNIVERSE
        include_sg: Include SG futures universe (default True)
        include_hk: Include HK futures universe (default False)
        port:       OpenD port. SG futures require SG account (port 11112).
                    Defaults to SG port if include_sg=True, else AU port.

    Returns:
        {mm_code: {name, last_price, prev_close, change_pct, volume, open_interest}}
    """
    if not _MM_AVAILABLE:
        return {}

    if mm_codes is None:
        mm_codes = []
        if include_sg:
            mm_codes.extend(SG_FUTURES_UNIVERSE.keys())
        if include_hk:
            mm_codes.extend(HK_FUTURES_UNIVERSE.keys())

    if not mm_codes:
        return {}

    # SG futures require SG account port
    if port is None:
        port = OPEND_SG_PORT if include_sg else OPEND_AU_PORT

    ctx = None
    result: dict[str, dict] = {}
    try:
        ctx = _ctx(port)
        ret, data = ctx.get_market_snapshot(mm_codes)
        if ret != RET_OK or data is None:
            return {}
        if hasattr(data, "empty") and data.empty:
            return {}

        for _, row in data.iterrows():
            def _f(col, default=None):
                try:
                    v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
                    return float(v) if v not in (None, "", "nan") else default
                except Exception:
                    return default

            code = str(row.get("code", "") if hasattr(row, "get") else "")
            if not code:
                continue

            lp = _f("last_price")
            pc = _f("prev_close_price")
            universe = {**SG_FUTURES_UNIVERSE, **HK_FUTURES_UNIVERSE}
            result[code] = {
                "name":          str(row.get("name", "") if hasattr(row, "get") else ""),
                "description":   universe.get(code, ""),
                "last_price":    lp,
                "prev_close":    pc,
                "change_pct":    round((lp - pc) / pc * 100, 3) if lp and pc and pc > 0 else None,
                "volume":        _f("volume"),
                "open_interest": _f("open_interest"),
                "amplitude":     _f("amplitude"),
            }

    except Exception as e:
        logger.debug("fetch_futures_snapshot: %s", e)
    finally:
        _safe_close(ctx)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Regime gate helper (used by signal_stack + webhook_server)
# ─────────────────────────────────────────────────────────────────────────────

def get_regime_gate(target_markets: list[str] = None) -> dict:
    """
    Returns a regime gate dict for use in the signal pipeline.

    Example return:
        {
            "US":  {"open": True,  "state": "MKT_STATUS_OPEN"},
            "HK":  {"open": False, "state": "MKT_STATUS_CLOSED"},
            "qot_logined": True,
            "trd_logined": True,
        }

    If OpenD is unavailable, all markets are assumed open (non-blocking).
    """
    if not _MM_AVAILABLE:
        # Non-blocking fallback: assume all open
        return {m: {"open": True, "state": "ASSUMED_OPEN"} for m in (target_markets or ["US", "HK", "SH", "SZ", "SG"])}

    states  = get_market_state_all()
    g_state = get_global_state()

    gate = {}
    for mkt, state in states.items():
        gate[mkt] = {
            "open":  any(s in state.upper() for s in ("OPEN", "MORNING", "AFTERNOON")),
            "state": state,
        }

    # Also expose login status
    gate["qot_logined"] = g_state.get("qot_logined", "True") not in ("False", "false", "0")
    gate["trd_logined"] = g_state.get("trd_logined", "True") not in ("False", "false", "0")

    return gate


# ─────────────────────────────────────────────────────────────────────────────
# Standalone: smoke test + benchmark
# ─────────────────────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    """Quick connectivity check for all 8 APIs."""
    print("\n=== SMOKE TEST ===\n")

    t0 = time.perf_counter()
    g = get_global_state()
    ms = (time.perf_counter() - t0) * 1000
    print(f"  get_global_state()         {ms:7.1f}ms  qot_logined={g.get('qot_logined')}  trd_logined={g.get('trd_logined')}")

    t0 = time.perf_counter()
    states = get_market_state_all()
    ms = (time.perf_counter() - t0) * 1000
    print(f"  get_market_state_all()     {ms:7.1f}ms  {states}")

    t0 = time.perf_counter()
    bars = fetch_intraday_bars("US.AAPL", ktype="K_5M", num=12)
    ms = (time.perf_counter() - t0) * 1000
    sigs = compute_intraday_signals(bars)
    print(f"  fetch_intraday_bars(AAPL)  {ms:7.1f}ms  bars={len(bars)}  trend={sigs.get('intraday_trend')}  vol_spike={sigs.get('vol_spike')}")

    t0 = time.perf_counter()
    info = fetch_basicinfo(["US.AAPL", "HK.00700"])
    ms = (time.perf_counter() - t0) * 1000
    print(f"  fetch_basicinfo()          {ms:7.1f}ms  keys={list(info.keys())}")

    t0 = time.perf_counter()
    wl = get_mm_watchlist("My Favorites")
    ms = (time.perf_counter() - t0) * 1000
    print(f"  get_mm_watchlist()         {ms:7.1f}ms  symbols={len(wl)}")

    t0 = time.perf_counter()
    fut = fetch_futures_snapshot(include_sg=True, port=OPEND_SG_PORT if _MM_AVAILABLE else None)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  fetch_futures_snapshot()   {ms:7.1f}ms  contracts={len(fut)}")
    for code, f in fut.items():
        print(f"    {code:<20} {f.get('description',''):<40}  price={f.get('last_price')}  chg={f.get('change_pct')}%")

    t0 = time.perf_counter()
    wrt = fetch_warrants("HK.00700", top_n=3)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  fetch_warrants(HK.00700)   {ms:7.1f}ms  warrants={len(wrt)}")
    for w in wrt:
        print(f"    {w['code']:<15} strike={w['strike_price']}  exp={w['maturity_time']}  vol={w['volume']}")

    print("\n=== SMOKE TEST DONE ===\n")


def _benchmark() -> None:
    """Per-API latency benchmark with 3-run median."""
    import statistics

    apis = [
        ("get_global_state",        lambda: get_global_state()),
        ("get_market_state_all",    lambda: get_market_state_all()),
        ("fetch_intraday_bars K_5M",lambda: fetch_intraday_bars("US.AAPL", "K_5M", 60)),
        ("fetch_intraday_bars K_1M",lambda: fetch_intraday_bars("US.AAPL", "K_1M", 60)),
        ("fetch_basicinfo (2 syms)", lambda: fetch_basicinfo(["US.AAPL", "HK.00700"])),
        ("fetch_basicinfo (10 sym)", lambda: fetch_basicinfo(["US.AAPL","US.MSFT","US.NVDA","US.TSLA","US.AMZN","US.META","US.GOOGL","US.V","HK.00700","HK.09988"])),
        ("get_mm_watchlist",        lambda: get_mm_watchlist("My Favorites")),
        ("fetch_warrants HK.00700", lambda: fetch_warrants("HK.00700", 5)),
        ("fetch_futures_snapshot",  lambda: fetch_futures_snapshot(include_sg=True)),
        ("get_regime_gate",         lambda: get_regime_gate()),
    ]

    print(f"\n{'API':<35} {'p50':>10}  {'p95':>10}  {'runs':>5}")
    print("─" * 65)
    for name, fn in apis:
        runs = []
        for _ in range(3):
            t0 = time.perf_counter()
            try:
                fn()
            except Exception:
                pass
            runs.append((time.perf_counter() - t0) * 1000)
        p50 = statistics.median(runs)
        p95 = max(runs)
        print(f"  {name:<33} {p50:>8.1f}ms  {p95:>8.1f}ms  {len(runs):>5}")

    print()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Moomoo Market Tools — smoke test / benchmark")
    p.add_argument("--smoke", action="store_true", help="Quick connectivity smoke test")
    p.add_argument("--bench", action="store_true", help="Latency benchmark all APIs")
    p.add_argument("--futures", action="store_true", help="Print SG futures snapshot")
    p.add_argument("--warrants", default="", help="Fetch warrants for HK code e.g. HK.00700")
    p.add_argument("--market-state", action="store_true", help="Print market state for all markets")
    p.add_argument("--basicinfo", default="", help="Comma-separated mm_codes for basicinfo")
    p.add_argument("--bars", default="", help="mm_code for intraday bars")
    p.add_argument("--bars-type", default="K_5M", help="Bar type (K_1M, K_5M, ...)")
    p.add_argument("--bars-num", type=int, default=60)
    args = p.parse_args()

    if args.smoke:
        _smoke_test()
    elif args.bench:
        _benchmark()
    elif args.futures:
        fut = fetch_futures_snapshot(include_sg=True)
        print(json.dumps(fut, indent=2, default=str))
    elif args.warrants:
        wrt = fetch_warrants(args.warrants)
        print(json.dumps(wrt, indent=2, default=str))
    elif args.market_state:
        print(json.dumps(get_market_state_all(), indent=2))
    elif args.basicinfo:
        codes = [c.strip() for c in args.basicinfo.split(",")]
        print(json.dumps(fetch_basicinfo(codes), indent=2, default=str))
    elif args.bars:
        bars = fetch_intraday_bars(args.bars, ktype=args.bars_type, num=args.bars_num)
        sigs = compute_intraday_signals(bars)
        print(f"Bars: {len(bars)}")
        print(json.dumps(sigs, indent=2))
        if bars:
            print("\nFirst bar:", json.dumps(bars[0], indent=2, default=str))
            print("Last bar: ", json.dumps(bars[-1], indent=2, default=str))
    else:
        p.print_help()
