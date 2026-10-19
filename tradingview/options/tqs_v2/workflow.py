"""
TQS v2 Workflow Entry Points
==============================
Public API for the TQS v2 package.

Called from main.py handlers:
  run_call_scan() → for menu option 10d
  run_put_scan()  → for menu option 10f
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Allow running directly: python tradingview/options/tqs_v2/workflow.py --scan-type calls
if __name__ == "__main__" and __package__ is None:
    _repo_root = Path(__file__).resolve().parents[3]  # d:\workspace\repo\stocks
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from tradingview.options.tqs_v2.config import DEFAULT_CONFIG, TQSv2Config
    from tradingview.options.tqs_v2.orchestrator import TQSv2Orchestrator
    from tradingview.options.tqs_v2.presets import get_preset
    from tradingview.options.tqs_v2.ranking.models import RankedContract
else:
    from .config import DEFAULT_CONFIG, TQSv2Config
    from .orchestrator import TQSv2Orchestrator
    from .presets import get_preset
    from .ranking.models import RankedContract

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_log = logging.getLogger("tqs_v2.workflow")


def run_call_scan(config: TQSv2Config = DEFAULT_CONFIG) -> list[RankedContract]:
    """
    Execute US 300 Quality Options Scan — top CALL per quality stock.

    Returns ranked CALL contracts. Also writes HTML + JSON reports.
    """
    _configure_logging(config)
    orch = TQSv2Orchestrator(config=config)
    return orch.run(scan_type="calls")


def run_put_scan(config: TQSv2Config = DEFAULT_CONFIG) -> list[RankedContract]:
    """
    Execute US 300 Quality Options Scan — top PUT per quality stock.

    Returns ranked PUT contracts. Also writes HTML + JSON reports.
    """
    _configure_logging(config)
    orch = TQSv2Orchestrator(config=config)
    return orch.run(scan_type="puts")


def run_catalyst_calls(config_override: dict | None = None) -> list[RankedContract]:
    """
    Run TQS v2 scan with catalyst_calls preset.

    Returns top-N ranked call contracts sorted by combined catalyst + standard score.
    Output: tqs_v2_catalyst_calls.html + tqs_v2_catalyst_calls.json

    Usage::

        from tradingview.options.tqs_v2.workflow import run_catalyst_calls
        picks = run_catalyst_calls()              # standard run
        picks = run_catalyst_calls({"top_n_global": 5})  # override top_n
    """
    import dataclasses
    cfg = get_preset("catalyst_calls")
    if config_override:
        cfg = dataclasses.replace(cfg, **config_override)
    _configure_logging(cfg)
    return TQSv2Orchestrator(config=cfg).run(scan_type="calls")


def run_catalyst_puts(config_override: dict | None = None) -> list[RankedContract]:
    """
    Run TQS v2 scan with catalyst_puts preset.
    Output: tqs_v2_catalyst_puts.html + tqs_v2_catalyst_puts.json
    """
    import dataclasses
    cfg = get_preset("catalyst_puts")
    if config_override:
        cfg = dataclasses.replace(cfg, **config_override)
    _configure_logging(cfg)
    return TQSv2Orchestrator(config=cfg).run(scan_type="puts")


def _configure_logging(config: TQSv2Config, verbose: bool = False) -> None:
    """Configure logging for a scan run (console + rotating file)."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("tqs_v2")
    root.setLevel(level)
    root.handlers.clear()

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))
    root.addHandler(ch)

    # Rotating file handler — always DEBUG level so the file captures everything
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        filename=str(config.logs_dir / f"{config.log_name}.log"),
        maxBytes=10 * 1024 * 1024,   # 10 MB per log file
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    ))
    root.addHandler(fh)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TQS v2 — US Options Quality Scan")
    parser.add_argument(
        "--scan-type",
        choices=["calls", "puts", "both"],
        default="both",
        help="Which options to scan: calls, puts, or both (default: both)",
    )
    parser.add_argument(
        "--sort-by",
        default="market_cap_basic",
        help="TV stock sort field (default: market_cap_basic). Use relative_volume_10d_calc for momentum filter.",
    )
    parser.add_argument(
        "--min-rv",
        type=float,
        default=0.0,
        help="Minimum relative volume for stock universe (default: 0.0 = off). Set 0.8 for momentum filter.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help=(
            "Number of stocks to scan from the TV universe (default: 20). "
            "Use 100/200/300 for a broader scan. "
            "The final report shows up to top_n×3 contracts (filtered by --min-score)."
        ),
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=55.0,
        help="Minimum composite score for report inclusion (default: 55.0)",
    )
    parser.add_argument(
        "--max-premium",
        type=float,
        default=8.00,
        help="Maximum option ask price / budget cap per contract (default: 8.00 = $800/contract). "
             "Raise to 10+ to include high-priced stocks like NVDA, GOOG, AAPL.",
    )
    parser.add_argument(
        "--per-expiry-cap",
        type=int,
        default=None,
        help="Max contracts per expiry date (default: uses config default=15). "
             "Lower to enforce expiry diversification; raise to show more from a single OPEX.",
    )
    parser.add_argument(
        "--per-sector-cap",
        type=int,
        default=None,
        help="Max contracts per sector (default: uses config default=5). "
             "Lower to enforce sector diversity (e.g. 3); raise for concentrated sector bets.",
    )
    parser.add_argument(
        "--catalyst",
        action="store_true",
        default=False,
        help=(
            "Enable catalyst mode (7-21 DTE, near-ATM delta 0.30-0.65, earnings-inclusive). "
            "Uses catalyst_calls/catalyst_puts preset instead of the standard quality preset. "
            "Output: tqs_v2_catalyst_calls.html / tqs_v2_catalyst_puts.html"
        ),
    )
    args = parser.parse_args()

    # stock_max_results  = how many stocks TV will return (the scan universe)
    # top_n_global       = how many ranked contracts appear in the final report
    #   → set to stock_count × 3 (top_n_per_stock default) so we never cut
    #     good contracts just because top_n_global < scored contracts.
    #   → min_score_for_report already acts as the real quality gate.
    _stock_count = args.top_n
    _cfg_overrides: dict = dict(
        stock_sort_by=args.sort_by,
        stock_min_relative_volume=args.min_rv,
        stock_max_results=_stock_count,
        top_n_global=_stock_count * 3,      # show up to 3 contracts per stock
        min_score_for_report=args.min_score,
        contract_max_premium=args.max_premium,
        max_concurrent_scan2=8,
    )
    # Wire optional cap CLI overrides (only if explicitly passed)
    if args.per_expiry_cap is not None:
        _cfg_overrides["top_n_per_expiry"] = args.per_expiry_cap
    if args.per_sector_cap is not None:
        _cfg_overrides["max_per_sector"] = args.per_sector_cap

    if args.catalyst:
        # Catalyst mode: load catalyst preset then apply user CLI overrides on top
        if args.scan_type in ("calls", "both"):
            run_catalyst_calls(config_override=_cfg_overrides)
        if args.scan_type in ("puts", "both"):
            run_catalyst_puts(config_override=_cfg_overrides)
    else:
        cfg = TQSv2Config(**_cfg_overrides)
        if args.scan_type == "both":
            run_call_scan(config=cfg)
            run_put_scan(config=cfg)
        elif args.scan_type == "calls":
            run_call_scan(config=cfg)
        else:
            run_put_scan(config=cfg)
