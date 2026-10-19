"""
NSE F&O Eligible Symbols — India Derivatives Universe
======================================================
Hardcoded list of NSE stocks eligible for Futures & Options trading.
SEBI periodically adds/removes stocks; this list is current as of April 2026.

Sources:
  https://www.nseindia.com/products-services/equity-derivatives-list-underlyings-info
  https://www.nseindia.com/regulations/circulars-and-guidelines

Usage:
  from options.nse_fo_list import is_fo_eligible, NSE_FO_SYMBOLS, NSE_FO_LOT_SIZES

  if is_fo_eligible("INFY"):
      lot = NSE_FO_LOT_SIZES.get("INFY", 400)
"""

from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path as _Path
from threading import Lock as _Lock

# ─── NSE F&O Stock Universe (Symbol: Lot Size) ──────────────────────────────
# Lot sizes updated April 2026. Always verify against NSE before live trading.
# https://www.nseindia.com/products-services/equity-derivatives-security-lot-size

NSE_FO_LOT_SIZES: dict[str, int] = {
    # NIFTY 50 components
    "ADANIENT":       1250,
    "ADANIPORTS":     1250,
    "APOLLOHOSP":      125,
    "ASIANPAINT":      200,
    "AXISBANK":       1200,
    "BAJAJ-AUTO":      250,
    "BAJAJFINSV":      500,
    "BAJFINANCE":      125,
    "BHARTIARTL":      500,
    "BPCL":            650,
    "BRITANNIA":        50,
    "CIPLA":           650,
    "COALINDIA":       400,
    "DIVISLAB":        200,
    "DRREDDY":          50,
    "EICHERMOT":       175,
    "GRASIM":          375,
    "HCLTECH":         700,
    "HDFCBANK":        550,
    "HDFCLIFE":       1000,
    "HEROMOTOCO":      300,
    "HINDALCO":       1400,
    "HINDUNILVR":      300,
    "ICICIBANK":       700,
    "INDUSINDBK":      500,
    "INFY":            300,
    "IOC":            1350,
    "ITC":            1600,
    "JSWSTEEL":        600,
    "KOTAKBANK":       400,
    "LT":              300,
    "M&M":             700,
    "MARUTI":           75,
    "NESTLEIND":        50,
    "NTPC":           3000,
    "ONGC":           2750,
    "POWERGRID":      2700,
    "RELIANCE":        250,
    "SBIN":            750,
    "SBILIFE":         750,
    "SHREECEM":         25,
    "SUNPHARMA":       700,
    "TATACONSUM":      500,
    "TATAMOTORS":      700,
    "TATASTEEL":       900,
    "TCS":             150,
    "TECHM":           400,
    "TITAN":           375,
    "ULTRACEMCO":       50,
    "UPL":             650,
    "WIPRO":          1500,
    # NIFTY Next 50 / NIFTY 100 additions
    "ABB":             250,
    "ABCAPITAL":      2800,
    "ABFRL":          2800,
    "ACC":             250,
    "ALKEM":           100,
    "AMBUJACEM":       600,
    "APLAPOLLO":       250,
    "AUBANK":          500,
    "BALKRISIND":      200,
    "BANDHANBNK":     1800,
    "BANKBARODA":     2950,
    "BATAINDIA":       250,
    "BEL":            3400,
    "BERGEPAINT":      500,
    "BIOCON":         2800,
    "BOSCHLTD":         46,
    "BSOFT":          1600,
    "CANBK":          1800,
    "CANFINHOME":      500,
    "CHOLAFIN":        500,
    "COFORGE":         150,
    "COLPAL":          400,
    "CONCOR":          500,
    "CUMMINSIND":      300,
    "DABUR":          1500,
    "DALBHARAT":       200,
    "DEEPAKNTR":       125,
    "DLF":             825,
    "ESCORTS":         275,
    "EXIDEIND":       2200,
    "FEDERALBNK":     5000,
    "GAIL":           3825,
    "GLENMARK":       575,
    "GMRINFRA":       22500,
    "GNFC":            350,
    "GODREJCP":        500,
    "GODREJPROP":      300,
    "GRANULES":       1200,
    "GSPL":           1250,
    "GUJGASLTD":       500,
    "HAL":              175,
    "HAVELLS":         500,
    "HFCL":           5000,
    "HINDPETRO":       700,
    "HINDCOPPER":     2900,
    "HONAUT":           15,
    "IDFCFIRSTB":     7500,
    "IEX":            3750,
    "IGL":            1375,
    "INDUSTOWER":     2800,
    "INOXWIND":        900,
    "IOB":            7200,
    "IRCTC":           625,
    "IRFC":           3750,
    "KPITTECH":        500,
    "LAURUSLABS":      1000,
    "LICHSGFIN":       1000,
    "LTIM":            150,
    "LTTS":            150,
    "LUPIN":           500,
    "MANAPPURAM":     2000,
    "MARICO":          900,
    "MCDOWELL-N":      250,
    "MCX":             400,
    "METROPOLIS":      200,
    "MFSL":            400,
    "MPHASIS":         300,
    "MRF":              15,
    "MUTHOOTFIN":      300,
    "NATIONALUM":     3750,
    "NAUKRI":          150,
    "NAVINFLUOR":      100,
    "NMDC":           5400,
    "OBEROIRLTY":      700,
    "OFSS":             50,
    "OIL":             700,
    "PAGEIND":          15,
    "PEL":             125,
    "PERSISTENT":       75,
    "PETRONET":       3000,
    "PFC":            2400,
    "PIDILITIND":      250,
    "POLYCAB":         150,
    "PRIC":            450,
    "PVRINOX":         300,
    "RAYMOND":         250,
    "RBL BANK":        2000,
    "RECLTD":         2250,
    "SAIL":           4300,
    "SCHAEFFLER":      200,
    "SCI":            2350,
    "SIEMENS":         250,
    "SRF":             250,
    "SUNPHARMA":       700,
    "TATACOMM":        500,
    "TATACHEM":        500,
    "TATAELXSI":       100,
    "TATAPOWER":      2700,
    "TIINDIA":         350,
    "TORNTPHARM":      300,
    "TORNTPOWER":      750,
    "TRENT":           350,
    "TVSMOTOR":        350,
    "UBL":             300,
    "UNIONBANK":      7000,
    "VARUNBEV":        500,
    "VBL":             250,
    "VEDL":           1750,
    "VOLTAS":          500,
    "YESBANK":        60000,
    "ZOMATO":         4500,
    "ZYDUSLIFE":       750,
}

# Pure symbol set (no lot sizes) — for fast O(1) membership check
NSE_FO_SYMBOLS: frozenset[str] = frozenset(NSE_FO_LOT_SIZES.keys())

# Index derivatives (not stock F&O but tradeable via NSE F&O)
NSE_INDEX_FO: frozenset[str] = frozenset({
    "NIFTY", "BANKNIFTY", "NIFTYNXT50", "FINNIFTY", "MIDCPNIFTY",
})


def is_fo_eligible(symbol: str) -> bool:
    """
    Return True if *symbol* is eligible for NSE F&O trading.

    Accepts symbols with or without exchange prefix:
      is_fo_eligible("INFY")       → True
      is_fo_eligible("NSE:INFY")   → True
      is_fo_eligible("NSE:WIPRO")  → True
      is_fo_eligible("NSE:SHARDACROP") → False (not in F&O)
      is_fo_eligible("NIFTY")      → True (index)
    """
    clean = symbol.split(":")[-1].upper().replace("-EQ", "").strip()
    return clean in NSE_FO_SYMBOLS or clean in NSE_INDEX_FO


def get_lot_size(symbol: str) -> int:
    """Return F&O lot size for *symbol*, or 0 if not eligible."""
    clean = symbol.split(":")[-1].upper().replace("-EQ", "").strip()
    return NSE_FO_LOT_SIZES.get(clean, 0)


# ─── Persistent Ineligible Cache ────────────────────────────────────────────
# Accumulates NSE symbols confirmed ineligible for F&O over time.
# Persists across runs so known-ineligible symbols are skipped immediately
# without re-checking or hitting any API.
#
# Cache file: tradingview/options/nse_fo_ineligible_cache.json
# Format:     {"KITEX": {"added": "2026-05-06", "reason": "..."}, ...}

_INELIGIBLE_CACHE_PATH: _Path = _Path(__file__).parent / "nse_fo_ineligible_cache.json"
_ineligible_lock = _Lock()
_ineligible_cache: dict[str, dict] | None = None


def _load_ineligible_cache() -> dict[str, dict]:
    """Load the ineligible cache from disk (once per process, then cached in memory)."""
    global _ineligible_cache
    if _ineligible_cache is not None:
        return _ineligible_cache
    if _INELIGIBLE_CACHE_PATH.exists():
        try:
            _ineligible_cache = json.loads(
                _INELIGIBLE_CACHE_PATH.read_text(encoding="utf-8")
            )
        except Exception:
            _ineligible_cache = {}
    else:
        _ineligible_cache = {}
    return _ineligible_cache


def is_fo_ineligible_cached(symbol: str) -> bool:
    """Return True if symbol has been previously confirmed NOT in NSE F&O.

    Checks the persistent JSON cache populated by mark_fo_ineligible().
    This is an O(1) dict lookup — safe to call in hot loops.
    """
    clean = symbol.split(":")[-1].upper().replace("-EQ", "").strip()
    return clean in _load_ineligible_cache()


def mark_fo_ineligible(symbol: str, reason: str = "") -> None:
    """Record symbol as confirmed F&O-ineligible.

    Saves to in-memory cache AND to nse_fo_ineligible_cache.json so future
    runs skip this symbol without checking is_fo_eligible or hitting any API.
    Only writes to disk when a NEW symbol is added (idempotent).
    """
    clean = symbol.split(":")[-1].upper().replace("-EQ", "").strip()
    with _ineligible_lock:
        cache = _load_ineligible_cache()
        if clean not in cache:
            cache[clean] = {
                "added": str(_date.today()),
                "reason": (reason or "")[:120],
            }
            try:
                _INELIGIBLE_CACHE_PATH.write_text(
                    json.dumps(cache, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass  # disk write failure is non-blocking


def get_ineligible_cache() -> dict[str, dict]:
    """Return a read-only copy of the current ineligible cache."""
    return dict(_load_ineligible_cache())

