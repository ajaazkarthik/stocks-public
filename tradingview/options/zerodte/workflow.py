"""
ZeroDTE Workflow Entry Points
==============================
Public entry points for the ZeroDTE scanner.
This module is invoked directly from main.py via subprocess.

Usage (as script):
    python workflow.py --direction both --max-premium 1.00 --min-score 60

Usage (as module):
    from tradingview.options.zerodte.workflow import run_zerodte_scan
    result = run_zerodte_scan()
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Windows UTF-8 fix (MANDATORY for standalone scripts) ──────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Path setup (ensure imports work from any cwd) ─────────────────────────────
_THIS_DIR = Path(__file__).parent
_OPTIONS_DIR = _THIS_DIR.parent        # tradingview/options/
_TV_DIR = _OPTIONS_DIR.parent          # tradingview/
_ROOT_DIR = _TV_DIR.parent             # workspace root

for _p in [str(_ROOT_DIR), str(_TV_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tradingview.options.zerodte.config import ZeroDTEConfig, DEFAULT_CONFIG
from tradingview.options.zerodte.models import ZeroDTERunResult
from tradingview.options.zerodte.orchestrator import ZeroDTEOrchestrator


# ── Logging setup ──────────────────────────────────────────────────────────────

def _configure_logging(config: ZeroDTEConfig) -> None:
    """Configure rotating file handler + console handler."""
    logs_dir = config.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_path = logs_dir / f"{config.log_name}.log"
    root = logging.getLogger("zerodte")
    root.setLevel(logging.DEBUG)

    # Rotating file: 5 MB max, keep 5 backups
    fh = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # Console: INFO only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root.addHandler(ch)

    root.info("ZeroDTE logging initialized → %s", log_path)


# ── Public API ─────────────────────────────────────────────────────────────────

def run_zerodte_scan(
    direction: str = "both",
    max_premium: float | None = None,
    min_score: float | None = None,
    out_dir: str | None = None,
    config: ZeroDTEConfig | None = None,
) -> ZeroDTERunResult:
    """
    Run the ZeroDTE scanner.

    Args:
        direction: "calls" | "puts" | "both"
        max_premium: override max option premium (default from config)
        min_score: override minimum composite score (default from config)
        out_dir: override reports output directory (default from config)
        config: optional custom ZeroDTEConfig (default = DEFAULT_CONFIG)

    Returns:
        ZeroDTERunResult with contracts and report paths
    """
    # Build config with overrides
    if config is None:
        if max_premium is not None or min_score is not None or out_dir is not None:
            # Build new config with overrides using dataclasses.replace
            import dataclasses
            kwargs: dict = {}
            if max_premium is not None:
                kwargs["contract_max_premium"] = float(max_premium)
            if min_score is not None:
                kwargs["min_score_for_report"] = float(min_score)
            if out_dir is not None:
                kwargs["reports_dir"] = Path(out_dir)
            config = dataclasses.replace(DEFAULT_CONFIG, **kwargs)
        else:
            config = DEFAULT_CONFIG

    _configure_logging(config)

    # Resolve direction
    direction_lower = direction.lower().strip()
    if direction_lower in ("calls", "call"):
        scan_directions = ["call"]
    elif direction_lower in ("puts", "put"):
        scan_directions = ["put"]
    else:  # "both" or default
        scan_directions = ["call", "put"]

    print(f"\n🔍 ZeroDTE Scanner — direction={direction_lower.upper()}, min_score={config.min_score_for_report:.0f}")
    print(f"   Universe: {len(config.fixed_watchlist)} fixed + TV top-{config.tv_scan_top_n} movers → DTE pre-filtered")
    print(f"   DTE range: {config.contract_min_dte}-{config.contract_max_dte} days")
    print(f"   Max premium: ${config.contract_max_premium:.2f}")

    # Cookie check — validate cookie before running; print clear actionable instructions
    from tradingview.options.zerodte.fetcher import _load_tv_cookies
    _cookies_loaded = _load_tv_cookies(config.cookie_path)
    _cookie_ok = bool(_cookies_loaded.get("sessionid"))
    if not config.cookie_path.exists():
        print(f"\n{'─'*60}")
        print("⚠️  TV COOKIE NOT FOUND — IV/VOI scoring disabled (max score = 40/100pts)")
        print(f"   Expected: {config.cookie_path}")
        print()
        print("   To enable full 100pt scoring:")
        print("   1. Open Chrome → tradingview.com → log in")
        print("   2. Install 'Cookie-Editor' browser extension")
        print("   3. Export cookies → copy the full Netscape cookie text")
        print(f"   4. Save it to:  {config.cookie_path}")
        print(f"{'─'*60}\n")
    elif not _cookie_ok:
        print(f"\n{'─'*60}")
        print("⚠️  TV COOKIE INVALID — 'sessionid' not found in cookie file")
        print(f"   File at: {config.cookie_path} ({config.cookie_path.stat().st_size} bytes)")
        print()
        print("   Common causes:")
        print("   • Exported wrong cookies (need tradingview.com, not subdomain)")
        print("   • Cookie session expired — log in to TradingView and re-export")
        print("   • File format wrong — use Netscape format (Cookie-Editor extension)")
        print(f"{'─'*60}\n")
    else:
        print()

    orchestrator = ZeroDTEOrchestrator(config=config)
    result = orchestrator.run(scan_directions=scan_directions)

    # Print summary
    print(f"\n{'─'*60}")
    print(f"✅ ZeroDTE Scan Complete — {result.run_date}")
    print(f"   Candidates evaluated: {result.candidates_evaluated}")
    print(f"   Call signals found:   {result.signals_found_calls}")
    print(f"   Put signals found:    {result.signals_found_puts}")
    print(f"   Call contracts:       {len(result.contracts_calls)}")
    print(f"   Put contracts:        {len(result.contracts_puts)}")
    if result.call_html_path:
        print(f"   Call report:          {result.call_html_path}")
    if result.put_html_path:
        print(f"   Put report:           {result.put_html_path}")
    if result.errors:
        print(f"   ⚠️  Errors ({len(result.errors)}): {result.errors[0]}")

    return result


# ── CLI entry point \u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014

def _run_close_positions(config: ZeroDTEConfig | None = None) -> None:
    """
    GAP-7 fix (2026-05-31): EOD position close tracker.

    Queries zerodte.db for today's expiring (DTE=0) contracts, fetches the
    actual underlying move (open\u2192close) via yfinance, and writes actual_move_pct
    back to the database so outcome quality can be tracked over time.

    Usage:
        python workflow.py --close-positions
    """
    import sqlite3
    from datetime import date as _date

    import yfinance as yf

    if config is None:
        config = DEFAULT_CONFIG

    _configure_logging(config)

    db_path = config.database_path
    today = _date.today().isoformat()  # "YYYY-MM-DD"

    print(f"\n\U0001f4ca ZeroDTE Close Tracker \u2014 {today}")

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            "SELECT id, symbol, expiry, strike, option_type, spot_price "
            "FROM contracts "
            "WHERE expiry = ? AND actual_move_pct IS NULL",
            (today,),
        )
        rows = cur.fetchall()
    except Exception as exc:
        print(f"\u26a0\ufe0f  DB query failed: {exc}")
        return

    if not rows:
        print(f"   No expiring contracts found for {today} (or all already updated)")
        conn.close()
        return

    print(f"   Found {len(rows)} expiring contract(s) to close")

    # Batch-fetch EOD prices for all unique symbols
    symbols = list({r[1] for r in rows})
    spot_closes: dict[str, float] = {}
    spot_opens: dict[str, float] = {}

    for sym in symbols:
        try:
            hist = yf.Ticker(sym).history(period="1d", interval="1d")
            if not hist.empty:
                spot_closes[sym] = float(hist["Close"].iloc[-1])
                spot_opens[sym] = float(hist["Open"].iloc[-1])
        except Exception as exc:
            print(f"   \u26a0\ufe0f  yfinance failed for {sym}: {exc}")

    updated = 0
    for row_id, sym, expiry, strike, opt_type, _entry_spot in rows:
        spot_close = spot_closes.get(sym)
        spot_open = spot_opens.get(sym)

        if spot_close is None or spot_open is None or spot_open == 0:
            print(f"   \u26a0\ufe0f  No EOD price for {sym} \u2014 skipping")
            continue

        # actual_move_pct = absolute % move of underlying on expiry day
        actual_move_pct = abs(spot_close / spot_open - 1.0) * 100.0
        direction = "\u2191" if spot_close > spot_open else "\u2193"

        try:
            cur.execute(
                "UPDATE contracts SET actual_move_pct = ? WHERE id = ?",
                (round(actual_move_pct, 4), row_id),
            )
            updated += 1
            print(
                f"   {sym} {opt_type.upper()} ${strike:.1f} exp={expiry}: "
                f"move={actual_move_pct:.2f}% {direction} "
                f"(open=${spot_open:.2f} \u2192 close=${spot_close:.2f})"
            )
        except Exception as exc:
            print(f"   \u26a0\ufe0f  DB update failed for contract id={row_id}: {exc}")

    conn.commit()
    conn.close()
    print(f"\n\u2705 Updated {updated}/{len(rows)} contract(s) \u2192 {db_path}")

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ZeroDTE 0-2DTE Options Pattern Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python workflow.py --direction both
  python workflow.py --direction calls --min-score 75
  python workflow.py --direction puts --max-premium 1.00 --min-score 60
        """,
    )
    parser.add_argument(
        "--direction",
        choices=["calls", "puts", "both", "call", "put"],
        default="both",
        help="Which direction to scan (default: both)",
    )
    parser.add_argument(
        "--max-premium",
        type=float,
        default=None,
        help="Max option premium in $ (default: 1.00 — 0-2DTE lottery-ticket range)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Min composite score 0-100 to include in report (default: 50)",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="Output directory for reports (default: reports/)",
    )
    parser.add_argument(
        "--close-positions",
        action="store_true",
        default=False,
        help="GAP-7: EOD mode \u2014 update actual_move_pct for today's DTE=0 expiring contracts",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.close_positions:
        _run_close_positions()
    else:
        run_zerodte_scan(
            direction=args.direction,
            max_premium=args.max_premium,
            min_score=args.min_score,
            out_dir=args.out_dir,
        )
