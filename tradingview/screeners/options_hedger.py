"""
options_hedger.py — Global Portfolio Hedging Strategy Advisor (CLI orchestrator)

Analyses open India holdings and recommends appropriate hedging strategies
based on position size, unrealized P&L, holding period risk, and market regime.
Global (Moomoo/IBKR) positions are covered for all major markets.

See individual sub-modules for implementation details:
  options_hedger_constants.py   — shared thresholds
  options_hedger_data.py        — broker data fetchers (_load_*)
  options_hedger_quotes.py      — yfinance option chain + code parsing
  options_hedger_india.py       — India hedge rules + NIFTY beta engine
  options_hedger_global.py      — global + long-option hedge engine
  options_hedger_report.py      — HTML report builder

Usage:
  python tradingview/screeners/options_hedger.py
  python tradingview/screeners/options_hedger.py --min-value 50000
  python tradingview/screeners/options_hedger.py --out path/to/report.html
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Sub-module imports ──────────────────────────────────────────────────────────
from .options_hedger_constants import (
    BIG_GAIN_PCT,
    LOSS_PCT,
    INDIA_VIX_FEAR,
)
from .options_hedger_data import (
    _load_holdings,
    _load_global_holdings,
    _load_pcr_map,
    _load_oi_map,
    _load_regime,
    _load_india_vix,
)
from .options_hedger_india import (
    _recommend_hedge,
    _build_nifty_portfolio_hedge,
)
from .options_hedger_global import (
    _recommend_hedge_global,
    _recommend_hedge_long_option,
    _prefetch_live_scores,
    _reset_score_provider,
)
from .options_hedger_report import build_html

ROOT      = Path(__file__).parent.parent        # tradingview/
REPO_ROOT = ROOT.parent                         # stocks/

OUTPUT_HTML = REPO_ROOT / "reports" / "hedging_advisor_report.html"

_log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    _reset_score_provider()  # clear per-run signal score cache

    # Reconfigure stdout to UTF-8 so emoji/unicode prints work on Windows cp1252
    if hasattr(sys.stdout, "reconfigure"):
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        description="Global Portfolio Hedging Strategy Advisor — India + global broker holdings",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--min-value",   type=float, default=0.0,
                        help="Minimum position value in INR to include")
    parser.add_argument("--no-ao",       action="store_true",
                        help="Skip AngelOne PCR + OI Buildup calls")
    parser.add_argument("--no-regime",   action="store_true",
                        help="Skip India regime detection")
    parser.add_argument("--big-gain",    type=float, default=BIG_GAIN_PCT,
                        help="P&L%% threshold for Protective Put / Collar")
    parser.add_argument("--loss",        type=float, default=LOSS_PCT,
                        help="P&L%% threshold below which EXIT is considered")
    parser.add_argument("--out",         type=str, default=str(OUTPUT_HTML),
                        help="Output HTML report path")
    parser.add_argument("--sentiment",   choices=["vader", "finbert", "both"],
                        default="vader",
                        help="Sentiment backend for live signal scoring (default: vader, fast)")
    args = parser.parse_args()

    # ── Sentiment backend — explicitly set VADER (fast) by default ────────────
    try:
        import importlib as _imp
        _ss_mod = _imp.import_module("signal_stack")
        _dual = (args.sentiment == "both")
        _ss_mod._set_sentiment_backend(args.sentiment if args.sentiment != "both" else "vader",
                                       dual_scores=_dual)
    except Exception:
        pass  # signal_stack unavailable — LiveScoreSource will use module default

    _big_gain = args.big_gain
    _loss     = args.loss

    print(f"\n{'='*60}")
    print(f"  Options Hedging Advisor")
    print(f"  Big Gain={_big_gain}%  Loss threshold={_loss}%")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    t0 = time.time()

    # ── Load holdings ────────────────────────────────────────────────────────
    print("  📂 Loading India holdings (Zerodha + AngelOne) …", end=" ", flush=True)
    holdings = _load_holdings(args.min_value)
    print(f"{len(holdings)} positions")
    if not holdings:
        print("  ⚠ No India holdings found. Check Zerodha/AngelOne connectivity.")

    # ── Load global holdings (Moomoo + IBKR) ────────────────────────────────
    print("  🌏 Loading global holdings (Moomoo-AU / Moomoo-SG / IBKR) …")
    global_holdings, long_option_positions = _load_global_holdings(args.min_value)
    print(f"     {len(global_holdings)} global positions, {len(long_option_positions)} long option contracts")

    if not holdings and not global_holdings:
        print("  ⚠ No holdings found from any broker. Check connectivity.")
        return

    # ── Load PCR + OI ────────────────────────────────────────────────────────
    pcr_map: dict[str, float] = {}
    oi_map:  dict[str, str]   = {}
    if not args.no_ao:
        print("  🇮🇳 Loading PCR (AngelOne) …", end=" ", flush=True)
        pcr_map = _load_pcr_map()
        print(f"{len(pcr_map)} symbols")
        print("  🇮🇳 Loading OI Buildup (AngelOne) …", end=" ", flush=True)
        oi_map = _load_oi_map()
        print(f"{len(oi_map)} symbols")

    # ── Load India regime ─────────────────────────────────────────────────────
    regime: dict = {}
    if not args.no_regime:
        print("  📊 Loading India market regime …", end=" ", flush=True)
        try:
            regime = _load_regime()
            print(f"{regime.get('regime', 'Unknown')}")
        except Exception as e:
            print(f"⚠ {e}")

    # ── Load India VIX ───────────────────────────────────────────────────────
    india_vix: Optional[float] = None
    if not args.no_regime:
        print("  📈 Loading India VIX (^INDIAVIX) …", end=" ", flush=True)
        india_vix = _load_india_vix()
        if india_vix is not None:
            fear_label = " ⚠ FEAR ZONE" if india_vix > INDIA_VIX_FEAR else ""
            print(f"{india_vix:.1f}{fear_label}")
        else:
            print("N/A (yfinance unavailable)")

    # ── Fetch NIFTY price once (used for beta hedge calculation) ─────────────
    nifty_price: Optional[float] = None
    if holdings:
        print("  📊 Loading NIFTY price …", end=" ", flush=True)
        try:
            import yfinance as yf
            nd = yf.download("^NSEI", period="2d", auto_adjust=True, progress=False)
            if not nd.empty:
                nifty_price = float(nd["Close"].iloc[-1].item())
                print(f"₹{nifty_price:,.0f}")
            else:
                nifty_price = 22_000.0
                print("N/A — using ₹22,000 fallback")
        except Exception:
            nifty_price = 22_000.0
            print("N/A — using ₹22,000 fallback")

    # ── Generate India recommendations ───────────────────────────────────────
    recs = []
    for h in holdings:
        sym = h.get("symbol", "").upper()
        pcr = pcr_map.get(sym)
        oi  = oi_map.get(sym)
        rec = _recommend_hedge(h, pcr, oi, regime, _big_gain, _loss, india_vix, nifty_price)
        recs.append({"holding": h, "rec": rec})

    # ── Generate global recommendations ──────────────────────────────────────
    global_recs = []
    for h in global_holdings:
        rec = _recommend_hedge_global(h, _big_gain, _loss)
        global_recs.append({"holding": h, "rec": rec})

    # ── Prefetch live signal scores for option underlyings in one batch ───────
    try:
        _prefetch_live_scores(
            [h["opt_info"]["underlying"] for h in long_option_positions
             if h.get("opt_info") and h["opt_info"].get("underlying")]
        )
    except Exception:
        pass

    # ── Generate long-option recommendations ──────────────────────────────────
    long_option_recs = []
    for h in long_option_positions:
        rec = _recommend_hedge_long_option(h, h["opt_info"])
        long_option_recs.append({"holding": h, "rec": rec})

    if long_option_recs:
        print(f"  📋 Long option positions: {len(long_option_recs)} contract(s)")
        for item in long_option_recs:
            h   = item["holding"]
            rec = item["rec"]
            opt = h.get("opt_info", {})
            print(f"     {h['symbol']} ${opt.get('strike',0):,.2f}{opt.get('option_type','?')} "
                  f"@ {opt.get('expiry_str','?')} — {rec['strategy']} [{rec['urgency']}]")

    # ── Print console summary ─────────────────────────────────────────────────
    if recs:
        print(f"\n  {'Symbol':<22}  {'Val ₹':>10}  {'P&L%':>7}  {'Strategy':<18}  Urgency")
        print(f"  {'-'*76}  [India]")
        for item in recs:
            h   = item["holding"]
            rec = item["rec"]
            print(f"  {h.get('symbol','?'):<22}  {h.get('combined_val',0):>10,.0f}  "
                  f"{h.get('combined_pnl_pct',0):>+7.1f}%  {rec['strategy']:<18}  {rec['urgency']}")

    if global_recs:
        cur_sym_map = {"AUD": "A$", "HKD": "HK$", "SGD": "S$", "USD": "$", "GBP": "£"}
        print(f"\n  {'Symbol':<22}  {'Broker':<12}  {'Val':>12}  {'P&L%':>7}  {'Strategy':<18}  Urgency")
        print(f"  {'-'*86}  [Global]")
        for item in global_recs:
            h   = item["holding"]
            rec = item["rec"]
            cur = h.get("currency", "USD")
            cs  = cur_sym_map.get(cur, cur + " ")
            print(f"  {h.get('symbol','?'):<22}  {h.get('broker','?'):<12}  "
                  f"{cs}{h.get('combined_val',0):>10,.0f}  "
                  f"{h.get('combined_pnl_pct',0):>+7.1f}%  {rec['strategy']:<18}  {rec['urgency']}")

    # ── Consolidated NIFTY portfolio hedge ───────────────────────────────────
    nifty_hedge_summary: Optional[dict] = None
    if nifty_price and recs:
        nifty_hedge_summary = _build_nifty_portfolio_hedge(recs, oi_map, nifty_price)
        if nifty_hedge_summary:
            lots = nifty_hedge_summary.get("lots", 0)
            n    = len(nifty_hedge_summary.get("positions", []))
            if lots:
                print(f"  📐 Portfolio NIFTY hedge: Buy {lots} lot(s) for {n} non-F&O stock(s)")
            else:
                print(f"  📐 Portfolio NIFTY hedge: exposure < 1 lot threshold ({n} stock(s))")

    # ── Build and save HTML ───────────────────────────────────────────────────
    print()
    html     = build_html(recs, regime, pcr_map, oi_map, args, india_vix, global_recs, long_option_recs, nifty_hedge_summary)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Persist snapshot to report analytics DB ───────────────────────────────
    try:
        _rdb_path = str(REPO_ROOT / "reports")
        if _rdb_path not in sys.path:
            sys.path.insert(0, _rdb_path)
        from report_db import save_hedging_snapshot, archive_before_write as _abw, archive_report as _arc
        _abw(out_path)
    except Exception as _rdb_exc:
        print(f"  [report_db] archive before write skipped: {_rdb_exc}")
        _abw = _arc = save_hedging_snapshot = None  # type: ignore

    out_path.write_text(html, encoding="utf-8")
    print(f"📄 Report saved → {out_path}  ({time.time()-t0:.1f}s)")

    try:
        if save_hedging_snapshot is not None:
            regime_str = (regime.get("regime", "") if isinstance(regime, dict) else str(regime or ""))
            save_hedging_snapshot(recs, regime_str, india_vix or 0)
            print(f"  [report_db] persisted hedging snapshot to report_analytics.db")
        if _arc is not None:
            _arc(out_path)
    except Exception as _rdb_exc:
        print(f"  [report_db] snapshot not saved: {_rdb_exc}")

    print()


if __name__ == "__main__":
    main()
