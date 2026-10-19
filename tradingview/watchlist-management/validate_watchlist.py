#!/usr/bin/env python3
"""
validate_watchlist.py
=====================
Validates every symbol in watchlist_1000.json against:
  1. TradingView Screener API (real-time symbol existence check per exchange)
  2. yfinance (for BATS/NYSE US symbols as secondary check)

Reports all INVALID / NOT FOUND symbols so they can be replaced.
"""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict

# ─── Config ───────────────────────────────────────────────────────────────────
WATCHLIST_FILE = "watchlist_1000.json"
_COOKIE_PATH   = Path(__file__).parent / "cookie"
_TV_COOKIE     = _COOKIE_PATH.read_text(encoding="utf-8").strip() if _COOKIE_PATH.exists() else None

# TV Scanner endpoint per exchange prefix
TV_SCAN_URLS = {
    "ASX":      "https://scanner.tradingview.com/australia/scan",
    "ASX_DLY":  "https://scanner.tradingview.com/australia/scan",
    "BATS":     "https://scanner.tradingview.com/america/scan",
    "NYSE":     "https://scanner.tradingview.com/america/scan",
    "NSE":      "https://scanner.tradingview.com/india/scan",
    "BSE_DLY":  "https://scanner.tradingview.com/india/scan",
    "HKEX":     "https://scanner.tradingview.com/hongkong/scan",
    "HKEX_DLY": "https://scanner.tradingview.com/hongkong/scan",
    "SGX":      "https://scanner.tradingview.com/singapore/scan",
    "SGX_DLY":  "https://scanner.tradingview.com/singapore/scan",
    "TSE":      "https://scanner.tradingview.com/japan/scan",
    "BINANCE":  "https://scanner.tradingview.com/crypto/scan",
    "FX":       "https://scanner.tradingview.com/forex/scan",
    "NYMEX":    "https://scanner.tradingview.com/futures/scan",
    "COMEX":    "https://scanner.tradingview.com/futures/scan",
    "CME":      "https://scanner.tradingview.com/futures/scan",
}

# ─── TV Symbol Lookup via Search API ─────────────────────────────────────────

def tv_search_symbol(tv_symbol: str) -> bool:
    """
    Use TV symbol search API to check if a symbol exists.
    tv_symbol format: "EXCHANGE:TICKER"
    """
    parts = tv_symbol.split(":")
    if len(parts) != 2:
        return False
    exchange, ticker = parts

    # Use TV's symbol search endpoint
    ticker_enc = urllib.parse.quote(ticker)
    url = f"https://symbol-search.tradingview.com/symbol_search/v3/?text={ticker_enc}&exchange={exchange}&lang=en&search_type=undefined&domain=production"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    }
    if _TV_COOKIE:
        headers["Cookie"] = _TV_COOKIE

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        symbols = data.get("symbols", [])
        for sym in symbols:
            if sym.get("symbol") == ticker and (
                sym.get("exchange") == exchange or
                sym.get("prefix_name") == exchange or
                sym.get("full_name", "").startswith(exchange + ":")
            ):
                return True
        return False
    except Exception:
        return None  # None = could not check


def tv_screener_batch(url: str, symbols: list) -> set:
    """
    Fetch from TV screener without any filters (just columns=['name']).
    Returns set of 'EXCHANGE:TICKER' strings that the screener knows about.
    Max 2000 results per call.
    """
    payload = {
        "columns": ["name"],
        "filter": [],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, 1000],
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    }
    if _TV_COOKIE:
        headers["Cookie"] = _TV_COOKIE

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return {item["s"] for item in data.get("data", []) if "s" in item}
    except Exception as e:
        print(f"    [WARN] screener fetch error for {url}: {e}")
        return set()


import urllib.parse


def check_symbols_via_screener_filter(symbols_to_check: list, scan_url: str) -> dict:
    """
    Use TV screener with a symbol-list filter to verify existence.
    Returns {symbol: True/False}
    """
    if not symbols_to_check:
        return {}

    # Use the 'name' filter with 'in_range' would not work; use 'text' search approach
    # Better: use the symbols field in the scan payload (TV Pro supports symbol list scan)
    payload = {
        "columns": ["name", "description", "close", "volume"],
        "symbols": {"tickers": symbols_to_check},
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    }
    if _TV_COOKIE:
        headers["Cookie"] = _TV_COOKIE

    # Use the quotes/bulk endpoint which accepts a list of symbols
    bulk_url = "https://scanner.tradingview.com/symbol/bulk-quote"
    req = urllib.request.Request(scan_url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        found = {item["s"] for item in data.get("data", []) if "s" in item}
        return {sym: (sym in found) for sym in symbols_to_check}
    except Exception as e:
        return {sym: None for sym in symbols_to_check}


def yfinance_check(tickers_yf: list) -> dict:
    """
    Check list of yfinance-format tickers.
    Returns {yf_ticker: bool}
    """
    import yfinance as yf
    results = {}
    # Batch download
    if not tickers_yf:
        return results
    try:
        data = yf.download(tickers_yf, period="2d", auto_adjust=True,
                           progress=False, threads=True)
        if hasattr(data.columns, "levels"):
            valid_tickers = set(data.columns.get_level_values(1))
        else:
            valid_tickers = set(data.columns)
        for t in tickers_yf:
            results[t] = t in valid_tickers
    except Exception as e:
        print(f"  yfinance batch error: {e}")
        for t in tickers_yf:
            results[t] = None
    return results


# ─── Convert TV symbol to yfinance format ─────────────────────────────────────
def tv_to_yf(tv_sym: str) -> str:
    """Best-effort convert TradingView symbol to yfinance ticker."""
    exchange, ticker = tv_sym.split(":", 1)
    if exchange in ("BATS", "NYSE", "NASDAQ"):
        return ticker.replace(".", "-")  # BRK.B → BRK-B
    if exchange == "ASX_DLY":
        return ticker + ".AX"
    if exchange == "NSE":
        return ticker + ".NS"
    if exchange == "HKEX":
        return ticker.zfill(4) + ".HK"
    if exchange == "HKEX_DLY":
        n = ticker.lstrip("0")
        return ticker + ".HK"
    if exchange == "SGX_DLY":
        return ticker + ".SI"
    if exchange == "TSE":
        return ticker + ".T"
    return ticker


# ─── Main Validation ──────────────────────────────────────────────────────────

def main():
    with open(WATCHLIST_FILE) as f:
        symbols = json.load(f)
    print(f"Loaded {len(symbols)} symbols from {WATCHLIST_FILE}")
    print()

    # Group by exchange prefix
    by_exchange = defaultdict(list)
    for sym in symbols:
        parts = sym.split(":", 1)
        if len(parts) == 2:
            by_exchange[parts[0]].append(sym)
        else:
            by_exchange["UNKNOWN"].append(sym)

    invalid = {}   # sym → reason
    valid   = set()
    unknown = {}   # sym → reason (could not check)

    # ── 1. Validate using TV screener 'symbols' payload (most reliable) ───────
    print("=" * 65)
    print("STEP 1: TV Screener Symbol Validation (batch per exchange)")
    print("=" * 65)

    screener_exchange_groups = [
        ("AU",  ["ASX", "ASX_DLY"],     "https://scanner.tradingview.com/australia/scan"),
        ("US",  ["BATS", "NYSE"],       "https://scanner.tradingview.com/america/scan"),
        ("IN",  ["NSE", "BSE_DLY"],     "https://scanner.tradingview.com/india/scan"),
        ("HK",  ["HKEX", "HKEX_DLY"],  "https://scanner.tradingview.com/hongkong/scan"),
        ("SG",  ["SGX", "SGX_DLY"],     "https://scanner.tradingview.com/singapore/scan"),
        ("JP",  ["TSE"],                "https://scanner.tradingview.com/japan/scan"),
        ("CRYPTO", ["BINANCE"],         "https://scanner.tradingview.com/crypto/scan"),
        ("FX",  ["FX"],                 "https://scanner.tradingview.com/forex/scan"),
        ("FUT", ["NYMEX","COMEX","CME"], "https://scanner.tradingview.com/futures/scan"),
    ]

    for region, prefixes, url in screener_exchange_groups:
        batch = []
        for pfx in prefixes:
            batch.extend(by_exchange.get(pfx, []))
        if not batch:
            continue

        print(f"\n[{region}] Checking {len(batch)} symbols ...", end=" ", flush=True)
        results = check_symbols_via_screener_filter(batch, url)
        time.sleep(0.5)

        found_count = sum(1 for v in results.values() if v is True)
        miss_count  = sum(1 for v in results.values() if v is False)
        none_count  = sum(1 for v in results.values() if v is None)
        print(f"✓{found_count}  ✗{miss_count}  ?{none_count}")

        for sym, result in results.items():
            if result is True:
                valid.add(sym)
            elif result is False:
                invalid[sym] = f"Not found in TV {region} screener"
            else:
                unknown[sym] = f"Screener check failed for {region}"

    # ── 2. yfinance secondary check for marked invalid/unknown US/AU/IN  ──────
    print(f"\n{'='*65}")
    print("STEP 2: yfinance cross-check for INVALID/UNKNOWN symbols")
    print(f"{'='*65}")

    # Collect US + AU + IN that are in invalid or unknown
    need_yf_check = {}
    for sym in list(invalid.keys()) + list(unknown.keys()):
        exchange = sym.split(":")[0]
        if exchange in ("BATS", "NYSE", "ASX_DLY", "ASX", "NSE", "HKEX", "HKEX_DLY", "SGX_DLY", "TSE"):
            need_yf_check[sym] = tv_to_yf(sym)

    if need_yf_check:
        yf_tickers = list(need_yf_check.values())
        print(f"  Checking {len(yf_tickers)} tickers via yfinance ...")
        yf_results = yfinance_check(yf_tickers)

        for tv_sym, yf_ticker in need_yf_check.items():
            yf_ok = yf_results.get(yf_ticker)
            if yf_ok is True:
                # yfinance confirms it exists — move out of invalid
                if tv_sym in invalid:
                    del invalid[tv_sym]
                    valid.add(tv_sym)
                    print(f"    RESCUED by yfinance: {tv_sym} (yf:{yf_ticker})")
            elif yf_ok is False:
                if tv_sym not in invalid:
                    invalid[tv_sym] = f"Not found in yfinance ({yf_ticker})"
    else:
        print("  Nothing to cross-check.")

    # ── 3. Manual known-bad patterns (FX, specific problematic ones) ─────────
    print(f"\n{'='*65}")
    print("STEP 3: Manual known-issue checks")
    print(f"{'='*65}")

    known_issues = {
        # ASX: symbols from original watchlist with wrong prefix
        # ASX:BKW, ASX:DHG, ASX:GOR, ASX:SPR — these were in original watchlist
        # TV uses ASX_DLY for delayed data; ASX (real-time) is Pro-only
        # GOR = Gold Road Resources → ASX_DLY:GOR exists but not ASX:GOR in search
        "ASX:GOR": "ASX real-time prefix unreliable; use ASX_DLY:GOR",
        "ASX:BKW": "Check ASX_DLY:BKW",
        "ASX:DHG": "Check ASX_DLY:DHG",
        "ASX:SPR": "Check ASX_DLY:SPR",
        # FX
        "FX:USDSGD": "TV FX feed uses SGDUSD not USDSGD — or use OANDA:USDSGD",
        "FX:USDINR": "Check — may need OANDA:USDINR or FX_IDC:USDINR",
        # SGX small tickers
        "SGX:564": "SGX real-time prefix; verify",
        "SGX:M04": "SGX real-time prefix; verify",
        "SGX:YYN": "SGX real-time prefix; verify",
        # Crypto
        "BINANCE:BNBUSDT": "Verify BNBUSDT exists on Binance feed",
        # NYSE specific
        "NYSE:BRK.A": "BRK.A rarely traded; TV may not have it — BRK.B safer",
        # TSE
        "TSE:7203": "Toyota — verify TSE prefix (not TYO)",
    }

    for sym, issue in known_issues.items():
        if sym in symbols and sym not in valid:
            if sym not in invalid:
                print(f"  FLAGGED: {sym} — {issue}")
                invalid[sym] = issue

    # ── Report ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("VALIDATION REPORT")
    print(f"{'='*65}")
    print(f"Total symbols checked : {len(symbols)}")
    print(f"Confirmed valid       : {len(valid)}")
    print(f"INVALID / NOT FOUND   : {len(invalid)}")
    print(f"Could not check       : {len(unknown)}")

    if invalid:
        print(f"\n🚫 INVALID SYMBOLS ({len(invalid)}):")
        for sym, reason in sorted(invalid.items()):
            print(f"   {sym:<35} → {reason}")

    if unknown and len(unknown) < 30:
        print(f"\n⚠️  COULD NOT CHECK ({len(unknown)}):")
        for sym, reason in sorted(unknown.items())[:30]:
            print(f"   {sym:<35} → {reason}")

    # Save report
    report = {
        "valid_count": len(valid),
        "invalid": {k: v for k, v in sorted(invalid.items())},
        "unknown": {k: v for k, v in sorted(unknown.items())},
    }
    with open("watchlist_validation_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n✅ Full report saved → watchlist_validation_report.json")


if __name__ == "__main__":
    main()
