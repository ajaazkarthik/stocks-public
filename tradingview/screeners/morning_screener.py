#!/usr/bin/env python3
"""
Morning Pre-Screener — Moomoo get_stock_filter Quality Gate
============================================================
Run once at market open to build a "pre-qualified pool" of stocks that
pass fundamental + liquidity quality criteria.

For US and HK markets, if Moomoo OpenD is unavailable or lacks quote rights,
automatically falls back to the TradingView screener API (no auth required)
using equivalent filters.

signal_stack.py loads screener_pool.json to:
  - Show a "Pre-Screened ✓" badge on qualifying signals
  - Apply a +3 composite-point boost to pre-qualified stocks

Usage:
  python morning_screener.py                    # US market, default thresholds
  python morning_screener.py --market HK        # HK market
  python morning_screener.py --market US --limit 200 --min-price 10
  python morning_screener.py --cross-check      # show which current fires pass

Thresholds (default US):
  • Price          ≥ $5       (no penny stocks)
  • Market cap     ≥ $500M    (established companies)
  • RSI            50 – 75    (bullish momentum zone, not overbought)
  • ADX            ≥ 20       (trending, not ranging)
  • Price > SMA200            (above long-term trend — client-side filter)
  Sort: relative volume descending (most active vs. average — genuine momentum)

Output: screener_pool.json (in this directory)
"""

import os, sys, json, time, argparse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Protobuf env must be set before moomoo import ─────────────────────────────
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

ROOT        = Path(__file__).parent.parent
POOL_FILE   = ROOT / "screener_pool.json"
FIRES_FILE  = ROOT / "fires_raw.json"

# ── Market thresholds ─────────────────────────────────────────────────────────
MARKET_DEFAULTS = {
    "US": {
        "min_price":        5.0,
        "min_market_cap":   500_000_000,   # $500M USD
        "min_pe":           0.5,
        "max_pe":           150.0,
        "min_turnover_rate":0.2,
        "sort_field":       "MARKET_VAL",
        "description":      "US: price≥$5, mktcap≥$500M, RSI 50–75, ADX≥20, price>SMA200, sorted by relative volume",
    },
    "HK": {
        "min_price":        1.0,
        "min_market_cap":   1_000_000_000, # HKD 10億
        "min_pe":           0.5,
        "max_pe":           100.0,
        "min_turnover_rate":0.1,
        "sort_field":       "MARKET_VAL",
        "description":      "HK: price≥HKD1, mktcap≥10億, PE 0.5–100, turnover≥0.1%",
    },
    "SH": {
        "min_price":        3.0,
        "min_market_cap":   2_000_000_000,
        "min_pe":           0.5,
        "max_pe":           80.0,
        "min_turnover_rate":0.2,
        "sort_field":       "MARKET_VAL",
        "description":      "SH: price≥3, mktcap≥20亿, PE 0.5–80, turnover≥0.2%",
    },
    "SZ": {
        "min_price":        3.0,
        "min_market_cap":   2_000_000_000,
        "min_pe":           0.5,
        "max_pe":           80.0,
        "min_turnover_rate":0.2,
        "sort_field":       "MARKET_VAL",
        "description":      "SZ: price≥3, mktcap≥20亿, PE 0.5–80, turnover≥0.2%",
    },
}

MARKET_MAP = {"US": "US", "HK": "HK", "SH": "SH", "SZ": "SZ"}


def run_screener(market: str = "US", limit: int = 200, overrides: dict = None) -> list[dict]:
    """
    Run get_stock_filter and return list of qualifying stock dicts.
    Each dict: { code, name, price, change_rate, market_val, pe, pb, turnover_rate }
    Returns [] if Moomoo is unavailable.
    """
    _TV_SUPPORTED = market.upper() in ("US", "HK")

    try:
        from moomoo import (
            OpenQuoteContext, Market,
            SimpleFilter, AccumulateFilter, FinancialFilter,
            StockField, SortDir,
        )
    except ImportError:
        print("  ✗ moomoo-api not installed. Run: pip install moomoo-api")
        if _TV_SUPPORTED:
            print("  → Falling back to TradingView screener ...")
            return _run_screener_tv(market, limit, overrides)
        return []

    cfg    = MARKET_DEFAULTS.get(market.upper(), MARKET_DEFAULTS["US"])
    if overrides:
        cfg = {**cfg, **overrides}

    mkt_enum = {"US": Market.US, "HK": Market.HK, "SH": Market.SH, "SZ": Market.SZ}[market.upper()]

    filters = []

    # ── Price filter ──────────────────────────────────────────────────────────
    if cfg.get("min_price") is not None:
        pf = SimpleFilter()
        pf.stock_field = StockField.CUR_PRICE
        pf.filter_min  = cfg["min_price"]
        filters.append(pf)

    # ── Market cap filter ─────────────────────────────────────────────────────
    if cfg.get("min_market_cap") is not None:
        mf = SimpleFilter()
        mf.stock_field = StockField.MARKET_VAL
        mf.filter_min  = cfg["min_market_cap"]
        filters.append(mf)

    # ── PE TTM filter ─────────────────────────────────────────────────────────
    if cfg.get("min_pe") is not None or cfg.get("max_pe") is not None:
        pef = SimpleFilter()
        pef.stock_field = StockField.PE_TTM
        if cfg.get("min_pe") is not None:
            pef.filter_min = cfg["min_pe"]
        if cfg.get("max_pe") is not None:
            pef.filter_max = cfg["max_pe"]
        filters.append(pef)

    # ── Turnover rate filter (liquidity) ──────────────────────────────────────
    if cfg.get("min_turnover_rate") is not None:
        tf = AccumulateFilter()
        tf.stock_field = StockField.TURNOVER_RATE
        tf.filter_min  = cfg["min_turnover_rate"]
        filters.append(tf)

    # ── Sort by market cap descending ─────────────────────────────────────────
    sf_sort = SimpleFilter()
    sf_sort.stock_field = StockField.MARKET_VAL
    sf_sort.is_no_filter = True
    sf_sort.sort = SortDir.DESCEND
    filters.append(sf_sort)

    ctx = None
    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, data = ctx.get_stock_filter(mkt_enum, filters, begin=0, num=limit)

        if ret != 0:
            print(f"  ✗ get_stock_filter error: {data}")
            if _TV_SUPPORTED:
                print("  → Moomoo quote right unavailable. Falling back to TradingView screener ...")
                return _run_screener_tv(market, limit, overrides)
            return []

        _last_page, all_count, stock_list = data
        print(f"  ✓ Moomoo returned {len(stock_list)} stocks (total matching: {all_count})")

        results = []
        for item in stock_list:
            def _flt(attr, default=0.0):
                try:
                    return float(getattr(item, attr, default) or default)
                except (ValueError, TypeError):
                    return default

            results.append({
                "code":          getattr(item, "stock_code", ""),
                "name":          getattr(item, "stock_name", ""),
                "price":         _flt("cur_price"),
                "change_rate":   _flt("change_rate"),
                "market_val":    _flt("market_val"),
                "pe":            _flt("pe_ttm"),
                "pb":            _flt("pb_rate"),
                "turnover_rate": _flt("turnover_rate"),
            })
        return results

    except Exception as e:
        print(f"  ✗ Screener error: {e}")
        if _TV_SUPPORTED:
            print("  → Falling back to TradingView screener ...")
            return _run_screener_tv(market, limit, overrides)
        return []
    finally:
        if ctx:
            try: ctx.close()
            except Exception:  pass  # cleanup


def _run_screener_tv(market: str, limit: int, overrides: dict | None) -> list[dict]:
    """
    TradingView-based screener fallback for US/HK when Moomoo is unavailable.
    Maps approximate TV fields to the same dict schema as run_screener().
    Note: turnover_rate is always 0.0 — TV does not expose this field directly.
    """
    _TV_MARKET_MAP = {"US": "america", "HK": "hongkong"}
    tv_market = _TV_MARKET_MAP.get(market.upper())
    if not tv_market:
        print(f"  ✗ TV fallback not supported for market: {market}")
        return []

    code_prefix = market.upper()
    cfg = MARKET_DEFAULTS.get(market.upper(), MARKET_DEFAULTS["US"])
    if overrides:
        cfg = {**cfg, **overrides}

    filters = []
    if cfg.get("min_price") is not None:
        filters.append({"left": "close", "operation": "egreater", "right": cfg["min_price"]})
    if cfg.get("min_market_cap") is not None:
        filters.append({"left": "market_cap_basic", "operation": "egreater", "right": cfg["min_market_cap"]})
    if cfg.get("min_pe") is not None:
        filters.append({"left": "price_earnings_ttm", "operation": "egreater", "right": cfg["min_pe"]})
    if cfg.get("max_pe") is not None:
        filters.append({"left": "price_earnings_ttm", "operation": "eless", "right": cfg["max_pe"]})
    # Relative volume proxy for turnover_rate — require some trading activity
    if cfg.get("min_turnover_rate") is not None:
        filters.append({"left": "relative_volume_10d_calc", "operation": "egreater", "right": 0.1})
    # Momentum filters: RSI bullish zone, strong trend (ADX), above long-term trend (SMA200)
    filters.append({"left": "RSI", "operation": "in_range", "right": [50, 75]})
    filters.append({"left": "ADX", "operation": "egreater", "right": 20})

    payload = {
        "filter": filters,
        "columns": ["name", "description", "close", "change", "market_cap_basic",
                    "RSI", "ADX", "SMA200", "relative_volume_10d_calc"],
        "sort": {"sortBy": "relative_volume_10d_calc", "sortOrder": "desc"},
        "range": [0, min(limit, 2000)],
        "options": {"lang": "en"},
    }

    url = f"https://scanner.tradingview.com/{tv_market}/scan"
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"  ✗ TV screener network error: {e.reason}")
        return []
    except Exception as e:
        print(f"  ✗ TV screener error: {e}")
        return []

    results = []
    for row in data.get("data") or []:
        # TV scanner row format: {"s": "NASDAQ:AAPL", "d": [name, description, close, change, mktcap, RSI, ADX, SMA200, relvol]}
        symbol = row.get("s", "")
        d = row.get("d") or []
        if len(d) < 6:
            continue
        ticker = symbol.split(":")[-1] if ":" in symbol else symbol
        try:
            close  = float(d[2] or 0)
            sma200 = float(d[7] or 0) if len(d) > 7 else 0.0
            # Client-side: skip stocks trading below their 200-day SMA (no uptrend)
            if sma200 > 0 and close <= sma200:
                continue
            results.append({
                "code":          f"{code_prefix}.{ticker}",
                "name":          (d[1] or d[0] or ticker)[:40],   # description = company name
                "price":         close,
                "change_rate":   float(d[3] or 0),
                "market_val":    float(d[4] or 0),
                "rsi":           float(d[5] or 0) if len(d) > 5 else 0.0,
                "adx":           float(d[6] or 0) if len(d) > 6 else 0.0,
                "rel_vol":       float(d[8] or 0) if len(d) > 8 else 0.0,
                "turnover_rate": 0.0,  # kept for schema compatibility
            })
        except (ValueError, TypeError):
            continue

    print(f"  ✓ TradingView screener returned {len(results)} stocks (via {url})")
    return results


def load_fires() -> list[str]:
    """Load current fire symbols from fires_raw.json. Returns list of TV pro_symbols."""
    if not FIRES_FILE.exists():
        return []
    try:
        data = json.loads(FIRES_FILE.read_text(encoding="utf-8"))
        fires = data.get("all", [])
        return list(dict.fromkeys(
            f.get("pro_symbol") or f.get("symbol", "")
            for f in fires
            if f.get("pro_symbol") or f.get("symbol")
        ))
    except Exception:
        return []


def tv_ticker(pro_symbol: str) -> str:
    """Extract base ticker from TradingView pro_symbol. e.g. 'NASDAQ:NVDA' → 'NVDA'"""
    return pro_symbol.split(":")[-1] if ":" in pro_symbol else pro_symbol


def cross_check_fires(pool_codes: set[str], fire_symbols: list[str]) -> dict:
    """
    For each fire symbol, check if its ticker matches any code in the pool.
    pool_codes: set of Moomoo codes like {"US.NVDA", "US.AAPL", ...}
    Returns dict: { "NASDAQ:NVDA": True/False, ... }
    """
    # Build ticker → mm_code lookup from pool
    ticker_in_pool = {code.split(".")[-1]: code for code in pool_codes}
    result = {}
    for sym in fire_symbols:
        ticker = tv_ticker(sym)
        result[sym] = ticker in ticker_in_pool
    return result


def save_pool(market: str, stocks: list[dict], thresholds: dict) -> None:
    """Save screener pool to JSON file."""
    pool_codes = {s["code"] for s in stocks}
    payload = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "market":        market.upper(),
        "thresholds":    thresholds.get("description", ""),
        "count":         len(stocks),
        "codes":         sorted(pool_codes),   # e.g. ["US.AAPL", "US.NVDA", ...]
        "stocks":        stocks,               # full details
    }
    POOL_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  ✓ Pool saved → {POOL_FILE}  ({len(stocks)} stocks)")


def print_summary(stocks: list[dict], fire_check: dict | None = None) -> None:
    """Print a formatted table of the top 30 stocks."""
    zero_price_count = sum(1 for s in stocks if s.get("price", 0) == 0)
    print(f"\n{'='*100}")
    if zero_price_count > len(stocks) // 2:
        print(f"  \u26a0\ufe0f  STALE DATA — {zero_price_count}/{len(stocks)} stocks have price=0. Market is likely CLOSED.")
        print(f"  {'─'*97}")
    print(f"  {'Code':<15} {'Name':<20} {'Price':>8} {'Chg%':>6} {'RSI':>6} {'ADX':>6} {'RelVol':>8}  Fires?")
    print(f"  {'-'*89}")
    for s in stocks[:30]:
        fire_flag = ""
        if fire_check:
            ticker = s["code"].split(".")[-1]
            # Find matching TV symbol
            matched = [sym for sym, ok in fire_check.items() if tv_ticker(sym) == ticker and ok]
            if matched:
                fire_flag = f"  🔥 {matched[0]}"

        stale_tag = "  (STALE)" if s.get("price", 0) == 0 else ""
        rsi_s = f"{s.get('rsi', 0):>6.1f}"
        adx_s = f"{s.get('adx', 0):>6.1f}"
        rv_s  = f"{s.get('rel_vol', 0):>8.2f}x"
        print(f"  {s['code']:<15} {s['name'][:20]:<20} {s['price']:>8.2f} {s['change_rate']:>6.2f}% {rsi_s} {adx_s} {rv_s}{fire_flag}{stale_tag}")
    if len(stocks) > 30:
        print(f"  ... and {len(stocks)-30} more (all saved to {POOL_FILE.name})")
    print(f"{'='*100}")


def main():
    # Fix Windows console encoding for unicode threshold descriptions
    if hasattr(sys.stdout, 'reconfigure'):
        if sys.platform == "win32":  # cp1252 fix — not needed on macOS/Linux (already UTF-8)
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description="Morning pre-screener via Moomoo get_stock_filter")
    parser.add_argument("--market",    choices=["US", "HK", "SH", "SZ"], default="US")
    parser.add_argument("--limit",     type=int,   default=200, help="Max stocks to fetch (default: 200)")
    parser.add_argument("--min-price", type=float, default=None, help="Override min price")
    parser.add_argument("--max-pe",    type=float, default=None, help="Override max PE TTM")
    parser.add_argument("--min-mktcap",type=float, default=None, help="Override min market cap (USD/HKD)")
    parser.add_argument("--cross-check", action="store_true",
                        help="Cross-check against current fires_raw.json and show matches")
    parser.add_argument("--no-save",   action="store_true", help="Print results only, don't save JSON")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Morning Screener — {args.market} Market")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  (Moomoo primary · TradingView fallback for US/HK)")
    print(f"{'='*60}")

    # ── Market-hours guard ─────────────────────────────────────────────────
    _now_utc = datetime.now(timezone.utc)
    _h_utc   = _now_utc.hour + _now_utc.minute / 60
    _HOURS   = {"US": (14.5, 21.0), "HK": (1.5, 8.0), "SH": (1.5, 7.0), "SZ": (1.5, 7.0)}
    _open_h, _close_h = _HOURS.get(args.market, (14.5, 21.0))
    if _now_utc.weekday() >= 5 or not (_open_h <= _h_utc <= _close_h):
        _why = "weekend" if _now_utc.weekday() >= 5 else "outside regular trading hours"
        print(f"\n\u26a0\ufe0f  {args.market} market is currently {_why} — screener results may be stale.")
        print(f"   UTC now: {_now_utc.strftime('%H:%M')}  |  "
              f"Market window (UTC): {int(_open_h):02d}:{int((_open_h%1)*60):02d}\u2013{int(_close_h):02d}:{int((_close_h%1)*60):02d}\n")

    cfg = dict(MARKET_DEFAULTS.get(args.market, MARKET_DEFAULTS["US"]))
    if args.min_price  is not None: cfg["min_price"]     = args.min_price
    if args.max_pe     is not None: cfg["max_pe"]        = args.max_pe
    if args.min_mktcap is not None: cfg["min_market_cap"]= args.min_mktcap
    print(f"  Thresholds: {cfg['description']}")

    print(f"\nRunning get_stock_filter on {args.market} ...")
    stocks = run_screener(market=args.market, limit=args.limit, overrides=cfg)

    if not stocks:
        if args.market in ("US", "HK"):
            print("\n⚠️  No results from Moomoo or TradingView screener. Check internet connectivity.")
        else:
            print("\n⚠️  No results. Is Moomoo OpenD running on port 11111?")
        sys.exit(1)

    # Guard: if ALL stocks have price=0, Moomoo is returning no live data (market closed).
    # Abort immediately — do NOT overwrite screener_pool.json with stale zero-price data.
    _zero_count = sum(1 for s in stocks if s.get("price", 0) == 0)
    if _zero_count == len(stocks):
        print(f"\n⛔  STALE DATA DETECTED — all {len(stocks)} stocks returned price=0.00.")
        print(f"    Moomoo is not returning live prices (market likely CLOSED or OpenD not streaming).")
        print(f"    screener_pool.json NOT updated — previous valid data is preserved.")
        _open_str = f"{int(_open_h):02d}:{int((_open_h % 1) * 60):02d}"
        print(f"    Re-run after market opens at {_open_str} UTC.")
        sys.exit(0)

    fire_check = None
    if args.cross_check:
        fire_symbols = load_fires()
        if fire_symbols:
            fire_check = cross_check_fires({s["code"] for s in stocks}, fire_symbols)
            qualified  = [sym for sym, ok in fire_check.items() if ok]
            total      = len(fire_symbols)
            print(f"\n  Fires cross-check: {len(qualified)}/{total} fires pass the quality gate")
            if qualified:
                print(f"  Pre-qualified fires: {', '.join(qualified)}")
            else:
                print("  None of the current fires pass the quality gate.")
        else:
            print("  Could not load fires_raw.json for cross-check.")

    print_summary(stocks, fire_check)

    if not args.no_save:
        print(f"\nSaving pool ...")
        save_pool(args.market, stocks, cfg)

    # Summary stats
    total_val = sum(s["market_val"] for s in stocks)
    avg_pe    = sum(s.get("pe") or 0 for s in stocks if (s.get("pe") or 0) > 0) / max(1, sum(1 for s in stocks if (s.get("pe") or 0) > 0))
    print(f"\n📊 Pool Summary:")
    print(f"   Stocks qualified : {len(stocks)}")
    print(f"   Aggregate mktcap : ${total_val/1e12:.1f}T" if total_val >= 1e12 else f"   Aggregate mktcap : ${total_val/1e9:.0f}B")
    print(f"   Avg PE TTM       : {avg_pe:.1f}x")
    print(f"   Largest by mktcap: {stocks[0]['code']} — {stocks[0]['name']}")
    print(f"\n✅ Screener complete. Use with signal_stack.py (pool auto-loaded from {POOL_FILE.name})")


if __name__ == "__main__":
    main()
    # Force-exit: moomoo SDK creates non-daemon threads that block Python from exiting,
    # causing Start-Process -Wait (morning_run.ps1) to hang indefinitely.
    import os as _os
    _os._exit(0)
