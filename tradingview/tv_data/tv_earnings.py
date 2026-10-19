"""
TradingView Earnings Scanner — fetch next earnings dates for any market.

Uses the public TradingView Scanner API which covers all markets:
  India (NSE/BSE), Australia (ASX), Hong Kong (HKEX), UK (LSE),
  Canada (TSX), US (NASDAQ/NYSE/AMEX), and more.

This is a fallback for when the TV screener batch lacks
earnings_release_next_date (most common for non-US stocks).

Verified working 2026-04-09 (no auth required for earnings dates).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import requests

# ── Config ────────────────────────────────────────────────────────────────────

_SCANNER_URL = "https://scanner.tradingview.com/{market}/scan"
_HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://www.tradingview.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
}

# TradingView exchange prefix → scanner market slug
_EXCHANGE_MARKET: dict[str, str] = {
    "NSE":    "india",
    "BSE":    "india",
    "ASX":    "australia",
    "HKEX":   "hongkong",
    "LSE":    "uk",
    "LON":    "uk",
    "TSX":    "canada",
    "TSXV":   "canada",
    "NASDAQ": "america",
    "NYSE":   "america",
    "AMEX":   "america",
    "OTC":    "america",
    "SGX":    "singapore",
}


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_tv_earnings_date(
    tv_symbol: str,
    timeout: int = 8,
) -> tuple[Optional[int], Optional[str]]:
    """
    Fetch next earnings release date for any TradingView symbol.

    Uses the TradingView Scanner API which covers all markets.
    Designed as a fallback when the TV screener batch or yfinance lacks the date.

    Args:
        tv_symbol: TradingView symbol, e.g. "NSE:RELIANCE", "ASX:CBA".
        timeout:   HTTP timeout in seconds.

    Returns:
        (unix_timestamp, "YYYY-MM-DD") if a future date is found,
        (None, None) otherwise.
    """
    if ":" not in tv_symbol:
        return None, None

    exchange, ticker = tv_symbol.split(":", 1)
    market = _EXCHANGE_MARKET.get(exchange.upper(), "america")

    body = {
        "filter": [{"left": "name", "operation": "equal", "right": ticker}],
        "columns": ["name", "earnings_release_next_date"],
        "range": [0, 5],
    }
    try:
        r = requests.post(
            _SCANNER_URL.format(market=market),
            headers=_HEADERS,
            data=json.dumps(body),
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        today = datetime.now(tz=timezone.utc).date()
        for entry in data:
            d = entry.get("d") or []
            if len(d) >= 2 and d[1]:
                ts = int(float(d[1]))
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                # Accept dates up to 2 days in the past (timezone slippage)
                if (dt.date() - today).days >= -2:
                    return ts, dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None, None


# ── CLI self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    symbols = sys.argv[1:] if len(sys.argv) > 1 else [
        "NSE:RELIANCE", "ASX:CBA", "HKEX:700", "NASDAQ:NVDA", "NYSE:AAPL",
    ]
    print("=== TV Earnings Scanner ===")
    for sym in symbols:
        ts, date_str = fetch_tv_earnings_date(sym)
        if date_str:
            today = datetime.now(tz=timezone.utc)
            days = (datetime.fromtimestamp(ts, tz=timezone.utc).date() - today.date()).days
            print(f"  {sym:20} → {date_str}  ({days:+d} days)")
        else:
            print(f"  {sym:20} → Not found")
