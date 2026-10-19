#!/usr/bin/env python3
"""
Market Screener — US / India (Nifty 200) / Singapore
=====================================================
Scans top stocks for US (S&P 500 proxy), India (Nifty 200 proxy), and
Singapore (STI + top SGX) markets using the TradingView scanner API.

Applies the same technical signal pass as the AustralianSuper screener:
  BUY    — RSI in [rsi_min, rsi_max] AND ADX > adx_min AND close > SMA200
  WATCH  — RSI in [30, rsi_min) AND ADX > adx_min AND close > SMA200
  AVOID  — close < SMA200 OR RSI > 75 OR ADX < 15

Each market produces its own named HTML report in reports/.

Usage:
  python market_screener.py --market us               # S&P 500 proxy (~500 stocks)
  python market_screener.py --market ind              # Nifty 200 proxy
  python market_screener.py --market sg               # SGX top stocks
  python market_screener.py --market us --bearish     # bearish mode
  python market_screener.py --market ind --no-pipeline
  python market_screener.py --market sg --rsi-min 50 --adx-min 25
"""
import sys
import argparse
from datetime import datetime
from pathlib import Path

# ── Windows: force UTF-8 console ──────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ──────────────────────────────────────────────────────────────────────
TV = Path(__file__).parent.parent
ROOT = TV.parent
REPORTS = ROOT / "reports"
DB_DIR = ROOT / "database"

# ── Import shared base ─────────────────────────────────────────────────────────
from market_screener_base import (
    tv_scan, run_screener_core, build_report_html,
)

# ── TV Scanner URLs ────────────────────────────────────────────────────────────
_TV_URLS = {
    "us": "https://scanner.tradingview.com/america/scan",
    "ind": "https://scanner.tradingview.com/india/scan",
    "sg": "https://scanner.tradingview.com/singapore/scan",
}

# ── Market configurations ──────────────────────────────────────────────────────

MARKET_CONFIG = {
    "us": {
        "label": "US Market (S&P 500 proxy)",
        "title": "🇺🇸 US Market Screener — S&P 500 Universe",
        "subtitle": "Top US stocks by market cap (NYSE + NASDAQ)",
        "header_color": "#1a237e",
        "currency_prefix": "US$",
        "chart_base_url": "https://www.tradingview.com/chart/?symbol=",
        "report_name": "us_market_report.html",
        "db_name": "us_market_screener.db",
        "source_tag": "us_market",
    },
    "ind": {
        "label": "India Market (Nifty 200 proxy)",
        "title": "🇮🇳 India Market Screener — Nifty 200 Universe",
        "subtitle": "Top Indian stocks by market cap (NSE)",
        "header_color": "#e65100",
        "currency_prefix": "₹",
        "chart_base_url": "https://www.tradingview.com/chart/?symbol=",
        "report_name": "india_market_report.html",
        "db_name": "india_market_screener.db",
        "source_tag": "india_market",
    },
    "sg": {
        "label": "Singapore Market (SGX top stocks)",
        "title": "🇸🇬 Singapore Market Screener — SGX Universe",
        "subtitle": "Top Singapore stocks by market cap (SGX)",
        "header_color": "#b71c1c",
        "currency_prefix": "S$",
        "chart_base_url": "https://www.tradingview.com/chart/?symbol=",
        "report_name": "sg_market_report.html",
        "db_name": "sg_market_screener.db",
        "source_tag": "sg_market",
    },
}


# ── Market-specific stock fetchers ─────────────────────────────────────────────

def fetch_us_stocks(verbose: bool = True) -> dict[str, list[dict]]:
    """Fetch top US stocks (~500) by market cap — S&P 500 proxy."""
    if verbose:
        print("  Fetching US large-cap stocks from TradingView scanner...", end=" ", flush=True)

    rows = tv_scan(
        url=_TV_URLS["us"],
        filters=[
            {"left": "market_cap_basic", "operation": "greater", "right": 2_000_000_000},
            {"left": "close", "operation": "greater", "right": 5.0},
            {"left": "is_primary", "operation": "equal", "right": True},
        ],
        sort_by="market_cap_basic",
        count=550,
    )

    stocks = []
    for r in rows:
        t = r.get("type", "")
        if t in ("dr", "fund"):
            continue
        r["_universe"] = "us_large_cap"
        stocks.append(r)
        if len(stocks) >= 500:
            break

    if verbose:
        print(f"{len(stocks)} stocks")

    return {"large_cap": stocks}


def fetch_india_stocks(verbose: bool = True) -> dict[str, list[dict]]:
    """Fetch top Indian stocks (~200) by market cap — Nifty 200 proxy."""
    if verbose:
        print("  Fetching India Nifty 200 proxy from TradingView scanner...", end=" ", flush=True)

    rows = tv_scan(
        url=_TV_URLS["ind"],
        filters=[
            {"left": "market_cap_basic", "operation": "greater", "right": 50_000_000_000},
            {"left": "close", "operation": "greater", "right": 10.0},
            {"left": "is_primary", "operation": "equal", "right": True},
        ],
        sort_by="market_cap_basic",
        count=250,
    )

    stocks = []
    for r in rows:
        t = r.get("type", "")
        if t in ("dr", "fund"):
            continue
        r["_universe"] = "nifty200"
        stocks.append(r)
        if len(stocks) >= 200:
            break

    if verbose:
        print(f"{len(stocks)} stocks")

    return {"nifty200": stocks}


def fetch_sg_stocks(verbose: bool = True) -> dict[str, list[dict]]:
    """Fetch top Singapore SGX stocks by market cap."""
    if verbose:
        print("  Fetching SGX stocks from TradingView scanner...", end=" ", flush=True)

    rows = tv_scan(
        url=_TV_URLS["sg"],
        filters=[
            {"left": "market_cap_basic", "operation": "greater", "right": 500_000_000},
            {"left": "close", "operation": "greater", "right": 0.10},
        ],
        sort_by="market_cap_basic",
        count=200,
    )

    stocks = []
    for r in rows:
        t = r.get("type", "")
        if t in ("dr",):
            continue
        r["_universe"] = "sgx"
        stocks.append(r)

    if verbose:
        print(f"{len(stocks)} stocks")

    return {"sgx": stocks}


_FETCHERS = {
    "us": fetch_us_stocks,
    "ind": fetch_india_stocks,
    "sg": fetch_sg_stocks,
}

# Section display metadata per market
_SECTION_META = {
    "us": {"large_cap": ("US Large-Cap Stocks (S&P 500 proxy)", "&#127482;&#127480;")},
    "ind": {"nifty200": ("India Nifty 200 Proxy", "&#127470;&#127475;")},
    "sg": {"sgx": ("SGX Stocks (Singapore)", "&#127480;&#127468;")},
}


# ── Main ───────────────────────────────────────────────────────────────────────

def main(args) -> None:
    market = args.market.lower()
    if market not in MARKET_CONFIG:
        print(f"  ✗ Unknown market: {market}. Choose from: us, ind, sg")
        sys.exit(1)

    cfg = MARKET_CONFIG[market]
    db_path = DB_DIR / cfg["db_name"]
    out_path = Path(args.out) if args.out else REPORTS / cfg["report_name"]

    print("\n" + "=" * 70)
    print(f"  {cfg['label']} — Signal Screener")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
          f"Mode: {'Bearish' if args.bearish else 'Bullish'}  |  "
          f"RSI {args.rsi_min}-{args.rsi_max}  ADX>{args.adx_min}")
    print("=" * 70)

    # Fetch market-specific stocks
    fetcher = _FETCHERS[market]
    sections = fetcher(verbose=True)

    # Run shared screener core
    run_screener_core(
        market_label=cfg["label"],
        sections=sections,
        db_path=db_path,
        rsi_min=args.rsi_min,
        rsi_max=args.rsi_max,
        adx_min=args.adx_min,
        bearish=args.bearish,
        pipeline=args.pipeline,
        source_tag=cfg["source_tag"],
        skip_moomoo=getattr(args, "skip_moomoo", False),
        skip_india=getattr(args, "skip_india", False),
    )

    # Build HTML report
    run_at_local = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = _SECTION_META[market]
    report_sections = {}
    for key, rows in sections.items():
        title, icon = meta.get(key, (key, ""))
        report_sections[key] = (title, icon, rows)

    html = build_report_html(
        title=cfg["title"],
        subtitle=cfg["subtitle"],
        sections=report_sections,
        rsi_min=args.rsi_min,
        rsi_max=args.rsi_max,
        adx_min=args.adx_min,
        bearish=args.bearish,
        run_at=run_at_local,
        header_color=cfg["header_color"],
        currency_prefix=cfg["currency_prefix"],
        chart_base_url=cfg["chart_base_url"],
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n  📊 Report written → {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Market Screener — US / India / Singapore",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--market", choices=["us", "ind", "sg"], required=True,
        help="Market to screen: us (S&P 500), ind (Nifty 200), sg (SGX)",
    )
    parser.add_argument("--rsi-min", type=float, default=45.0,
                        help="Minimum RSI for BUY signal (default: 45)")
    parser.add_argument("--rsi-max", type=float, default=72.0,
                        help="Maximum RSI for BUY signal (default: 72)")
    parser.add_argument("--adx-min", type=float, default=20.0,
                        help="Minimum ADX for BUY signal (default: 20)")
    parser.add_argument("--bearish", action="store_true",
                        help="Bearish mode (inverse RSI filter, close < SMA200)")
    parser.add_argument("--out", default=None,
                        help="Output HTML report path (default: market-specific in reports/)")
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-open the HTML report in browser")
    parser.add_argument("--pipeline", action="store_true", default=True,
                        help="Run full signal_stack.py pipeline (default: on)")
    parser.add_argument("--no-pipeline", dest="pipeline", action="store_false",
                        help="Skip signal_stack.py pipeline run")
    parser.add_argument("--skip-moomoo", action="store_true",
                        help="Pass --skip-moomoo to signal_stack.py")
    parser.add_argument("--skip-india", action="store_true",
                        help="Pass --skip-india to signal_stack.py")

    args = parser.parse_args()
    main(args)
