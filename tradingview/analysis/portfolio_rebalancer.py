"""
Portfolio Rebalancer — Calculate required trades to reach target allocation.
============================================================================
Given target allocation percentages per region/asset class, calculates exactly
which positions to trim/add to reach target.

Input:  .cache_global_positions.json (from global_portfolio_dashboard.py)
Output: tradingview/reports/rebalance_report.html

Usage:
    python tradingview/analysis/portfolio_rebalancer.py
    python tradingview/analysis/portfolio_rebalancer.py --target-us 30 --target-au 30 --target-in 20 --target-sg 20
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_THIS_DIR = Path(__file__).resolve().parent
_TV = _THIS_DIR.parent
_ROOT = _TV.parent
sys.path.insert(0, str(_ROOT))

try:
    from path_config import REPORTS
except ImportError:
    REPORTS = _ROOT / "reports"

POSITIONS_CACHE = _ROOT / ".cache_global_positions.json"
DEFAULT_OUT = REPORTS / "rebalance_report.html"

# ── Region classification ─────────────────────────────────────────────────────

_BROKER_REGION_MAP = {
    "Moomoo-AU": "AU",
    "Moomoo-SG": "SG",
    "IBKR": "US",         # Mostly US positions
    "Webull AU": "AU",
    "Zerodha": "IN",
    "AngelOne": "IN",
}

_EXCHANGE_REGION_MAP = {
    "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "CBOE": "US", "BATS": "US",
    "ASX": "AU",
    "NSE": "IN", "BSE": "IN",
    "SGX": "SG",
    "HKEX": "HK", "SEHK": "HK",
}


@dataclass
class Position:
    symbol: str
    broker: str
    market_val: float
    shares: float
    current_price: float
    region: str = ""
    pnl_pct: float = 0.0


@dataclass
class RebalanceAction:
    symbol: str
    broker: str
    region: str
    action: str  # "BUY" or "SELL"
    current_val: float
    target_val: float
    delta_val: float
    delta_shares: float
    priority: int = 0  # 1=high, 2=medium, 3=low


def classify_region(position: dict) -> str:
    """Classify position into region based on broker and exchange."""
    broker = position.get("broker", "")
    exchange = position.get("exchange", "")
    symbol = position.get("symbol", "")

    # First try broker mapping
    if broker in _BROKER_REGION_MAP:
        # Moomoo can trade US/HK from AU and SG
        if broker in ("Moomoo-AU", "Moomoo-SG"):
            if exchange in _EXCHANGE_REGION_MAP:
                return _EXCHANGE_REGION_MAP[exchange]
            # Check symbol suffix
            if ".HK" in symbol or ":HKEX" in symbol:
                return "HK"
            if any(x in exchange.upper() for x in ("NYSE", "NASDAQ", "AMEX")):
                return "US"
            return _BROKER_REGION_MAP[broker]
        return _BROKER_REGION_MAP[broker]

    # Try exchange mapping
    for ex, region in _EXCHANGE_REGION_MAP.items():
        if ex in (exchange or "").upper():
            return region

    return "OTHER"


def load_positions(cache_path: Path = POSITIONS_CACHE) -> list[Position]:
    """Load positions from the cached global positions JSON."""
    if not cache_path.exists():
        return []

    with open(cache_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    positions = []
    items = raw if isinstance(raw, list) else raw.get("positions", [])

    for p in items:
        market_val = float(p.get("market_val", 0) or 0)
        if market_val <= 0:
            continue

        pos = Position(
            symbol=p.get("symbol", "UNKNOWN"),
            broker=p.get("broker", ""),
            market_val=market_val,
            shares=float(p.get("shares", 0) or 0),
            current_price=float(p.get("current_price", 0) or 0),
            region=classify_region(p),
            pnl_pct=float(p.get("pnl_pct", 0) or 0),
        )
        positions.append(pos)

    return positions


def compute_rebalance(
    positions: list[Position],
    target_alloc: dict[str, float],
    tolerance_pct: float = 2.0,
) -> tuple[list[RebalanceAction], dict]:
    """
    Compute rebalance actions to align portfolio with target allocation.

    Args:
        positions: Current positions
        target_alloc: Target allocation {region: percentage} e.g. {"US": 30, "AU": 30}
        tolerance_pct: Don't rebalance if within this tolerance

    Returns:
        (actions, stats) tuple
    """
    total_portfolio = sum(p.market_val for p in positions)
    if total_portfolio <= 0:
        return [], {"total": 0, "regions": {}}

    # Current allocation by region
    region_current: dict[str, float] = {}
    for p in positions:
        region_current[p.region] = region_current.get(p.region, 0) + p.market_val

    # Normalize target to sum to 100
    target_sum = sum(target_alloc.values())
    if target_sum > 0:
        target_alloc = {k: v / target_sum * 100 for k, v in target_alloc.items()}

    # Compute deltas
    stats = {
        "total": total_portfolio,
        "regions": {},
    }

    actions: list[RebalanceAction] = []
    all_regions = set(list(region_current.keys()) + list(target_alloc.keys()))

    for region in sorted(all_regions):
        current_val = region_current.get(region, 0)
        current_pct = current_val / total_portfolio * 100
        target_pct = target_alloc.get(region, 0)
        target_val = total_portfolio * target_pct / 100
        delta = target_val - current_val

        stats["regions"][region] = {
            "current_val": current_val,
            "current_pct": current_pct,
            "target_pct": target_pct,
            "target_val": target_val,
            "delta": delta,
        }

        # Skip if within tolerance
        if abs(current_pct - target_pct) <= tolerance_pct:
            continue

        # Generate per-position actions
        region_positions = [p for p in positions if p.region == region]

        if delta > 0:
            # Need to BUY more in this region
            # Suggest adding to existing positions proportionally
            for p in sorted(region_positions, key=lambda x: x.market_val):
                share_of_region = p.market_val / current_val if current_val > 0 else 1.0 / max(len(region_positions), 1)
                pos_delta = delta * share_of_region
                delta_shares = pos_delta / p.current_price if p.current_price > 0 else 0

                actions.append(RebalanceAction(
                    symbol=p.symbol,
                    broker=p.broker,
                    region=region,
                    action="BUY",
                    current_val=p.market_val,
                    target_val=p.market_val + pos_delta,
                    delta_val=pos_delta,
                    delta_shares=round(delta_shares, 2),
                    priority=1 if abs(current_pct - target_pct) > 10 else 2,
                ))

        elif delta < 0:
            # Need to SELL in this region
            # Trim from largest positions first
            for p in sorted(region_positions, key=lambda x: -x.market_val):
                share_of_region = p.market_val / current_val if current_val > 0 else 1.0 / max(len(region_positions), 1)
                pos_delta = abs(delta) * share_of_region
                delta_shares = pos_delta / p.current_price if p.current_price > 0 else 0

                actions.append(RebalanceAction(
                    symbol=p.symbol,
                    broker=p.broker,
                    region=region,
                    action="SELL",
                    current_val=p.market_val,
                    target_val=p.market_val - pos_delta,
                    delta_val=-pos_delta,
                    delta_shares=-round(delta_shares, 2),
                    priority=1 if abs(current_pct - target_pct) > 10 else 2,
                ))

    # Sort by priority then absolute delta
    actions.sort(key=lambda a: (a.priority, -abs(a.delta_val)))
    return actions, stats


def generate_rebalance_html(
    actions: list[RebalanceAction],
    stats: dict,
    target_alloc: dict[str, float],
    output_path: Path = DEFAULT_OUT,
) -> Path:
    """Generate HTML rebalance report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = stats.get("total", 0)
    regions = stats.get("regions", {})

    # Region summary cards
    region_cards = ""
    for region, info in sorted(regions.items()):
        delta_class = "surplus" if info["delta"] > 0 else "deficit" if info["delta"] < 0 else ""
        region_cards += f"""<div class="stat-card {delta_class}">
  <div class="value">{info['current_pct']:.1f}%</div>
  <div class="label">{region} (target: {info['target_pct']:.1f}%)</div>
  <div class="delta">{'+'if info['delta']>0 else ''}{info['delta']:,.0f} USD</div>
</div>
"""

    # Action table
    action_rows = ""
    for a in actions:
        action_class = "buy" if a.action == "BUY" else "sell"
        action_rows += f"""<tr class='{action_class}'>
  <td>{a.symbol}</td>
  <td>{a.broker}</td>
  <td>{a.region}</td>
  <td class='{action_class}'>{a.action}</td>
  <td>${a.current_val:,.0f}</td>
  <td>${a.target_val:,.0f}</td>
  <td>{'+'if a.delta_val>0 else ''}{a.delta_val:,.0f}</td>
  <td>{a.delta_shares:+.2f}</td>
</tr>
"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Portfolio Rebalancer</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; background: #0d1117; color: #c9d1d9; }}
  h1 {{ color: #58a6ff; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
  .stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }}
  .stat-card .value {{ font-size: 1.6em; font-weight: bold; color: #58a6ff; }}
  .stat-card .label {{ color: #8b949e; font-size: 0.85em; }}
  .stat-card .delta {{ color: #d29922; font-size: 0.9em; margin-top: 4px; }}
  .surplus .delta {{ color: #3fb950; }}
  .deficit .delta {{ color: #f85149; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
  th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: center; }}
  th {{ background: #161b22; color: #58a6ff; }}
  .buy {{ color: #3fb950; }}
  .sell {{ color: #f85149; }}
  footer {{ margin-top: 20px; color: #484f58; font-size: 0.85em; }}
</style>
</head><body>
<h1>⚖️ Portfolio Rebalancer</h1>
<p>Total portfolio: <strong>${total:,.0f}</strong> | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="stats">{region_cards}</div>

<h2>Recommended Actions</h2>
<table>
<thead><tr><th>Symbol</th><th>Broker</th><th>Region</th><th>Action</th><th>Current</th><th>Target</th><th>Delta $</th><th>Shares</th></tr></thead>
<tbody>{action_rows if action_rows else '<tr><td colspan="8">Portfolio is balanced within tolerance ✅</td></tr>'}</tbody>
</table>

<footer>Portfolio Rebalancer v1.0</footer>
</body></html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"✅ Rebalance report saved to {output_path}")
    return output_path


def main(args: Optional[argparse.Namespace] = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Portfolio Rebalancer")
        parser.add_argument("--target-us", type=float, default=30.0, help="Target US allocation %%")
        parser.add_argument("--target-au", type=float, default=30.0, help="Target AU allocation %%")
        parser.add_argument("--target-in", type=float, default=20.0, help="Target IN allocation %%")
        parser.add_argument("--target-sg", type=float, default=10.0, help="Target SG allocation %%")
        parser.add_argument("--target-hk", type=float, default=10.0, help="Target HK allocation %%")
        parser.add_argument("--tolerance", type=float, default=2.0, help="Tolerance %% before rebalancing")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output HTML path")
        parser.add_argument("--positions-file", default=str(POSITIONS_CACHE), help="Positions JSON file")
        args = parser.parse_args()

    target_alloc = {
        "US": args.target_us, "AU": args.target_au, "IN": getattr(args, 'target_in', 20.0),
        "SG": args.target_sg, "HK": args.target_hk,
    }

    positions = load_positions(Path(args.positions_file))
    if not positions:
        print("❌ No positions found. Run global_portfolio_dashboard.py first.")
        return

    print(f"📊 {len(positions)} positions, total ${sum(p.market_val for p in positions):,.0f}")
    actions, stats = compute_rebalance(positions, target_alloc, getattr(args, 'tolerance', 2.0))
    generate_rebalance_html(actions, stats, target_alloc, Path(args.out))


if __name__ == "__main__":
    main()
