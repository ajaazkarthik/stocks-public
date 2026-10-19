"""
US Stock Universe Builder — 300 Quality US Stocks for Options Screening
=======================================================================
Fetches quality US stocks from TradingView screener API:
  • Price > $10 (excludes penny stocks)
  • Market cap > $200M (excludes micro-caps)
  • Daily avg volume > 100K (excludes illiquid)
  • No OTC stocks

Output: tradingview/watchlists/us_300_watchlist.json

Usage:
    python us_stock_universe.py
    python us_stock_universe.py --count 300 --verbose
    python us_stock_universe.py --out /path/to/watchlist.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE         = Path(__file__).parent.resolve()   # tradingview/options/
_TV_DIR       = _HERE.parent.resolve()            # tradingview/
_WATCHLIST_DIR = _TV_DIR / "watchlists"
_DEFAULT_OUT  = _WATCHLIST_DIR / "us_300_watchlist.json"

_SCANNER_URL = "https://scanner.tradingview.com/america/scan"

# Server-side filters — only use confirmed-valid TV scanner fields
# OTC / exchange filtering is done CLIENT-SIDE (exchange filter causes HTTP 400)
_QUALITY_FILTERS: list[dict] = [
    {"left": "average_volume_10d_calc", "operation": "greater", "right": 100_000},
    {"left": "market_cap_basic",        "operation": "greater", "right": 200_000_000},
    {"left": "close",                   "operation": "greater", "right": 10.0},
]

# US major exchanges — client-side OTC exclusion
_US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "AMEX PINK", "BATS", "CBOE"}

_COLUMNS = [
    "name",
    "close",
    "market_cap_basic",
    "average_volume_10d_calc",
    "price_earnings_ttm",      # pe_ttm is invalid; correct name is price_earnings_ttm
    "relative_volume_10d_calc",
]


def fetch_quality_us_stocks(count: int = 300, verbose: bool = False) -> list[dict]:
    """
    Fetch up to *count* quality US stocks from TradingView screener.

    Returns a list of dicts with keys:
        symbol, ticker, exchange, price, market_cap, volume_10d, pe_ttm, rel_vol
    Returns [] on any network / parse error.
    """
    payload = {
        "columns": _COLUMNS,
        "filter":  _QUALITY_FILTERS,
        "range":   [0, min(count + 50, 500)],
        "sort":    {"sortBy": "market_cap_basic", "sortOrder": "desc"},
    }
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        _SCANNER_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"[USUniverse] TV scanner error: {exc}")
        return []

    raw = data.get("data", [])
    if verbose:
        print(f"[USUniverse] TV scanner returned {len(raw)} raw rows")

    stocks: list[dict] = []
    for item in raw:
        sym = item.get("s", "")
        d   = item.get("d", [])
        if len(d) < 3:
            continue
        ticker = sym.split(":")[-1] if ":" in sym else sym
        exch   = sym.split(":")[0]  if ":" in sym else ""

        # Client-side: exclude OTC and non-major-exchange stocks
        if exch not in _US_EXCHANGES:
            continue

        stocks.append({
            "symbol":     sym,
            "ticker":     ticker,
            "exchange":   exch,
            "price":      d[1] if len(d) > 1 else None,
            "market_cap": d[2] if len(d) > 2 else None,
            "volume_10d": d[3] if len(d) > 3 else None,
            "pe_ttm":     d[4] if len(d) > 4 else None,
            "rel_vol":    d[5] if len(d) > 5 else None,
        })
        if len(stocks) >= count:
            break

    if verbose:
        print(f"[USUniverse] After OTC filter: {len(stocks)} stocks")
    return stocks


def build_watchlist(
    count: int = 300,
    out_path: Path = _DEFAULT_OUT,
    verbose: bool = False,
) -> list[dict]:
    """
    Fetch stock list and persist to *out_path* as JSON.
    Returns the stock list ([] on failure).
    """
    stocks = fetch_quality_us_stocks(count=count, verbose=verbose)
    if not stocks:
        if verbose:
            print("[USUniverse] No stocks returned — check network / TV scanner")
        return []

    out_path.parent.mkdir(parents=True, exist_ok=True)
    watchlist = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe":     "US",
        "count":        len(stocks),
        "stocks":       stocks,
    }
    out_path.write_text(json.dumps(watchlist, indent=2), encoding="utf-8")
    if verbose:
        print(f"[USUniverse] Saved {len(stocks)} stocks → {out_path}")
    return stocks


def load_watchlist(path: Path = _DEFAULT_OUT) -> list[dict]:
    """
    Load a previously saved watchlist from *path*.
    Returns [] if file does not exist or is malformed.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
        return data.get("stocks", [])
    except Exception:
        return []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build US 300-stock quality watchlist for options screening"
    )
    parser.add_argument(
        "--count",   type=int, default=300,
        help="Number of stocks to fetch (default: 300)"
    )
    parser.add_argument(
        "--out", default=str(_DEFAULT_OUT),
        help="Output JSON path"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = build_watchlist(count=args.count, out_path=Path(args.out), verbose=True)
    print(f"Done — {len(result)} stocks saved to {args.out}")
