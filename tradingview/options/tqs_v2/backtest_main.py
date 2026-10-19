"""
TQS v2 Backtest Entry Point
===========================

Steps:
  1. OutcomeTracker.fetch_pending_outcomes() — fill expiry prices for expired contracts
  2. VectorBTEngine.run()                    — compute win rates + factor correlations
  3. BacktestReporter.generate()             — write HTML + JSON report

Usage (from workspace root):
  python tradingview/options/tqs_v2/backtest_main.py [--out PATH]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ── Ensure repo root is on sys.path when run directly ────────────────────────
if __name__ == "__main__" and __package__ is None:
    _repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(_repo_root))

# ── Imports (absolute, works when sys.path contains repo root) ───────────────
from tradingview.options.tqs_v2.config import TQSv2Config  # noqa: E402
from tradingview.options.tqs_v2.backtest.outcome_tracker import OutcomeTracker  # noqa: E402
from tradingview.options.tqs_v2.backtest.vectorbt_engine import VectorBTEngine  # noqa: E402
from tradingview.options.tqs_v2.reporting.backtest_reporter import BacktestReporter  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
_log = logging.getLogger(__name__)

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TQS v2 Backtest Analyzer — fetch outcomes and generate report",
    )
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Override output HTML path (default: reports/tqs_v2_backtest_TIMESTAMP.html)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = TQSv2Config()
    db_path = cfg.database_dir / cfg.database_name

    if not db_path.exists():
        _log.error("TQS v2 database not found: %s", db_path)
        _log.error("Run a TQS v2 scan first (menu options 61/62) to populate data.")
        sys.exit(1)

    # Step 1 — Fetch pending outcomes (expired contracts without final prices)
    _log.info("Step 1/4 — Fetching outcomes for expired contracts …")
    with OutcomeTracker(db_path) as tracker:
        updated = tracker.fetch_pending_outcomes()
        _log.info("  %d expired outcomes updated", updated)

        # Step 2 — Fetch live premiums (unexpired contracts, mark-to-market)
        _log.info("Step 2/4 — Fetching live premiums for unexpired contracts …")
        live_updated = tracker.fetch_live_outcomes()
    _log.info("  %d live (unrealised) contracts updated", live_updated)

    # Step 3 — Run analysis
    _log.info("Step 3/4 — Running backtest analysis …")
    with VectorBTEngine(db_path) as engine:
        result = engine.run()

    if result.total_contracts == 0:
        _log.warning(
            "No completed contracts found in DB. "
            "Contracts need to expire and outcomes need to be fetched first."
        )

    # Step 4 — Generate report
    _log.info("Step 4/4 — Generating report …")
    reporter = BacktestReporter(out_html=args.out)
    html_path, json_path = reporter.generate(result)

    print(f"\n{'='*60}")
    print(f"  TQS v2 Backtest Report")
    print(f"{'='*60}")
    print(f"  Contracts analysed : {result.total_contracts}")
    print(f"  Winners / Losers   : {result.winners} / {result.losers}")
    print(f"  Win Rate           : {result.win_rate:.1f}%")
    print(f"  Avg P&L %%         : {result.avg_pnl_pct:+.2f}%%")
    print(f"  HTML report        : {html_path}")
    print(f"  JSON report        : {json_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
