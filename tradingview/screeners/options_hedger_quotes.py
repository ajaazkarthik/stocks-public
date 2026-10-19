"""
options_hedger_quotes.py — yfinance option chain fetching and option code parsing.

Provides:
  _fetch_full_chain    — full calls+puts DataFrame for a ticker/expiry
  _fetch_option_quote  — single strike bid/ask mid (with caching)
  _parse_option_code   — decode Moomoo option position codes
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Optional

import structlog

_log = structlog.get_logger(__name__)

# ── Module-level caches (persist across calls in one process run) ────────────
_OPT_QUOTE_CACHE: dict[tuple, tuple] = {}
_CHAIN_CACHE: dict[tuple, object] = {}   # (ticker, expiry_ymd) → result dict


# ─────────────────────────────────────────────────────────────────────────────
# Real option quote fetcher — yfinance option_chain
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_full_chain(underlying: str, expiry_str: str) -> "Optional[object]":
    """
    Fetch the full yfinance option_chain for *underlying* at the expiry nearest
    to *expiry_str* (format: "24 Apr 2026").  Returns the calls DataFrame or
    None on failure.  Results are cached to avoid repeat network calls.
    """
    key = (underlying.upper(), expiry_str)
    if key in _CHAIN_CACHE:
        return _CHAIN_CACHE[key]

    result = None
    try:
        import yfinance as yf

        t = yf.Ticker(underlying)
        available = t.options
        if not available:
            _CHAIN_CACHE[key] = None
            return None

        try:
            td = datetime.strptime(expiry_str, "%d %b %Y")
        except ValueError:
            _CHAIN_CACHE[key] = None
            return None

        best_exp, best_diff = None, float("inf")
        for exp_str in available:
            try:
                exp  = datetime.strptime(exp_str, "%Y-%m-%d")
                diff = abs((exp - td).days)
                if diff < best_diff:
                    best_diff, best_exp = diff, exp_str
            except ValueError:
                continue

        if best_exp is None or best_diff > 60:
            _CHAIN_CACHE[key] = None
            return None

        chain = t.option_chain(best_exp)
        calls = chain.calls.copy()
        puts  = chain.puts.copy()
        calls["_type"] = "C"
        puts["_type"]  = "P"
        calls["mid"]   = (calls["bid"] + calls["ask"]) / 2.0
        puts["mid"]    = (puts["bid"]  + puts["ask"])  / 2.0

        # Fetch spot price via history (more reliable than fast_info which can return NaN)
        spot_price: Optional[float] = None
        try:
            spot_h = t.history(period="2d", auto_adjust=True)
            if not spot_h.empty:
                spot_price = float(spot_h["Close"].iloc[-1])
        except Exception:
            try:
                fi = t.fast_info
                _v = float(getattr(fi, "last_price", 0) or 0)
                if _v > 0 and math.isfinite(_v):
                    spot_price = _v
            except Exception:
                pass

        result = {"calls": calls, "puts": puts, "expiry_used": best_exp, "spot": spot_price}

        # Also cache individual call quotes for _fetch_option_quote
        for _, row in calls.iterrows():
            k    = (underlying.upper(), expiry_str, round(float(row["strike"]), 2), "C")
            bid  = float(row.get("bid", 0) or 0)
            ask  = float(row.get("ask", 0) or 0)
            last = float(row.get("lastPrice", 0) or 0)
            if bid > 0 and ask > 0:
                _OPT_QUOTE_CACHE[k] = (round((bid + ask) / 2, 2), "live")
            elif last > 0:
                _OPT_QUOTE_CACHE[k] = (round(last, 2), "last")

    except Exception:
        result = None

    _CHAIN_CACHE[key] = result
    return result


def _fetch_option_quote(
    ticker: str,
    target_expiry: str,  # e.g. "17 Apr 2026"
    strike: float,
    opt_type: str,       # "C" or "P"
) -> tuple[Optional[float], str]:
    """
    Fetch a real option bid/ask mid-price from yfinance option_chain.

    Returns (price_per_share, source) where source is:
      "live"     – real bid/ask mid computed from yfinance quote
      "last"     – last traded price (no live bid/ask)
      "est"      – fallback static estimate (not returned here, caller decides)
      "error"    – yfinance call failed / no data
    """
    key = (ticker.upper(), target_expiry, round(strike, 2), opt_type.upper())
    if key in _OPT_QUOTE_CACHE:
        return _OPT_QUOTE_CACHE[key]

    result: tuple[Optional[float], str] = (None, "error")
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        available = t.options   # list of "YYYY-MM-DD" strings
        if not available:
            _OPT_QUOTE_CACHE[key] = (None, "no_options")
            return (None, "no_options")

        try:
            td = datetime.strptime(target_expiry, "%d %b %Y")
        except ValueError:
            _OPT_QUOTE_CACHE[key] = (None, "bad_date")
            return (None, "bad_date")

        # Find nearest available expiry within 60 days
        best_exp, best_diff = None, float("inf")
        for exp_str in available:
            try:
                exp  = datetime.strptime(exp_str, "%Y-%m-%d")
                diff = abs((exp - td).days)
                if diff < best_diff:
                    best_diff = diff
                    best_exp  = exp_str
            except ValueError:
                continue

        if best_exp is None or best_diff > 60:
            _OPT_QUOTE_CACHE[key] = (None, "no_expiry")
            return (None, "no_expiry")

        chain = t.option_chain(best_exp)
        df    = chain.calls if opt_type.upper() == "C" else chain.puts

        if df is None or df.empty:
            _OPT_QUOTE_CACHE[key] = (None, "empty_chain")
            return (None, "empty_chain")

        df = df.copy()
        df["_diff"] = abs(df["strike"] - strike)
        row  = df.nsmallest(1, "_diff").iloc[0]
        bid  = float(row.get("bid",       0) or 0)
        ask  = float(row.get("ask",       0) or 0)
        last = float(row.get("lastPrice", 0) or 0)

        if bid > 0 and ask > 0 and ask >= bid:
            result = (round((bid + ask) / 2, 2), "live")
        elif last > 0:
            result = (round(last, 2), "last")
        else:
            result = (None, "stale")

    except Exception:
        result = (None, "error")

    _OPT_QUOTE_CACHE[key] = result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Option code parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_option_code(raw: str) -> Optional[dict]:
    """
    Parse a Moomoo option position code like 'GOOGL260424C335000' into components.

    Format: {TICKER}{YYMMDD}{C|P}{STRIKE*1000}

    Examples:
      GOOGL260424C335000  → underlying=GOOGL, expiry=24 Apr 2026, type=CALL, strike=$335.00
      SPY260319P415000    → underlying=SPY,   expiry=19 Mar 2026, type=PUT,  strike=$415.00

    Returns dict with: underlying, expiry_date, expiry_str, option_type, strike.
    Returns None if not a recognisable option code.
    """
    # Strip exchange prefix: "US.GOOGL260424C335000" → "GOOGL260424C335000"
    clean = raw.split(".")[-1].upper()

    # Match: 1–6 uppercase letters + 6 digits (YYMMDD) + C/P + 1+ digits (strike×1000)
    m = re.match(r'^([A-Z]{1,6})(\d{6})([CP])(\d+)$', clean)
    if not m:
        return None

    underlying, yymmdd, opt_type, strike_raw = m.groups()
    strike = int(strike_raw) / 1000.0

    try:
        exp_date = datetime.strptime(yymmdd, "%y%m%d")
    except ValueError:
        return None

    return {
        "underlying": underlying,
        "expiry_date": exp_date,
        "expiry_str":  exp_date.strftime("%d %b %Y"),
        "option_type": opt_type,   # "C" = call, "P" = put
        "strike":      strike,
    }
