"""
Tax Lot Optimizer — Select optimal lots to sell for CGT minimisation.
=====================================================================
When selling, choose WHICH lot to sell. Lots held >12 months get 50% CGT
discount in Australia. This module recommends which lot to sell to minimise
the tax bill.

Input:  CGT trade data from cgt/ scripts
Output: cgt/reports/tax_lot_report.html

Usage:
    python cgt/tax_lot_optimizer.py --symbol AAPL --shares 10
    python cgt/tax_lot_optimizer.py --broker IBKR --list
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from path_config import CGT, REPORTS, ROOT
except ImportError:
    ROOT = _ROOT
    CGT = _ROOT / "cgt"
    REPORTS = _ROOT / "reports"

DEFAULT_OUT = REPORTS / "tax_lot_report.html"
CGT_DISCOUNT_THRESHOLD_DAYS = 365  # >12 months for 50% CGT discount
CGT_DISCOUNT_FACTOR = 0.50  # 50% discount for long-term holdings (Australia)


@dataclass
class TaxLot:
    """A single purchase lot."""
    symbol: str
    buy_date: date
    shares: float
    cost_per_share: float
    broker: str = ""
    currency: str = "AUD"

    @property
    def total_cost(self) -> float:
        return self.shares * self.cost_per_share

    @property
    def holding_days(self) -> int:
        return (date.today() - self.buy_date).days

    @property
    def is_long_term(self) -> bool:
        """Whether this lot qualifies for 50% CGT discount (>12 months)."""
        return self.holding_days > CGT_DISCOUNT_THRESHOLD_DAYS

    @property
    def discount_factor(self) -> float:
        """CGT discount factor: 0.5 for long-term, 1.0 for short-term."""
        return CGT_DISCOUNT_FACTOR if self.is_long_term else 1.0


@dataclass
class SellRecommendation:
    """A recommendation for which lot to sell."""
    lot: TaxLot
    shares_to_sell: float
    current_price: float
    gain_per_share: float
    total_gain: float
    taxable_gain: float  # After CGT discount
    estimated_tax: float
    reason: str
    priority: int  # 1=best, 2=ok, 3=worst


def compute_tax_on_gain(gain: float, discount: float, marginal_rate: float = 0.37) -> float:
    """Compute estimated CGT on a capital gain."""
    if gain <= 0:
        return 0.0  # Capital losses are not taxed
    taxable = gain * discount
    return taxable * marginal_rate


def optimize_lot_selection(
    lots: list[TaxLot],
    shares_to_sell: float,
    current_price: float,
    marginal_tax_rate: float = 0.37,
) -> list[SellRecommendation]:
    """
    Determine optimal lot selection to minimise CGT.

    Strategy:
    1. Prefer lots with LOSSES (harvest tax losses first)
    2. Then prefer LONG-TERM lots (50% CGT discount)
    3. Then prefer lots with SMALLEST gains
    
    Args:
        lots: Available tax lots for the symbol
        shares_to_sell: Number of shares to sell
        current_price: Current market price
        marginal_tax_rate: Taxpayer's marginal rate (default 37%)

    Returns:
        Ordered list of SellRecommendation (best first)
    """
    if not lots or shares_to_sell <= 0 or current_price <= 0:
        return []

    recommendations = []
    remaining = shares_to_sell

    # Score each lot
    scored_lots = []
    for lot in lots:
        if lot.shares <= 0:
            continue

        gain_per_share = current_price - lot.cost_per_share
        sellable = min(lot.shares, remaining) if remaining > 0 else lot.shares
        total_gain = gain_per_share * sellable
        taxable_gain = total_gain * lot.discount_factor if total_gain > 0 else total_gain
        estimated_tax = compute_tax_on_gain(total_gain, lot.discount_factor, marginal_tax_rate)

        # Scoring: lower tax = better
        # Priority: 1=loss lots, 2=discounted gain lots, 3=full-rate gain lots
        if total_gain <= 0:
            priority = 1
            reason = f"Tax loss harvesting — ${abs(total_gain):,.0f} loss offsets other gains"
        elif lot.is_long_term:
            priority = 2
            reason = f"50% CGT discount — held {lot.holding_days} days (>{CGT_DISCOUNT_THRESHOLD_DAYS})"
        else:
            priority = 3
            reason = f"No discount — held {lot.holding_days} days. Consider waiting {CGT_DISCOUNT_THRESHOLD_DAYS - lot.holding_days} more days"

        scored_lots.append((priority, estimated_tax, lot, sellable, gain_per_share, total_gain, taxable_gain, estimated_tax, reason))

    # Sort by priority, then by estimated tax (ascending)
    scored_lots.sort(key=lambda x: (x[0], x[1]))

    for priority, _, lot, sellable, gain_per_share, total_gain, taxable_gain, est_tax, reason in scored_lots:
        if remaining <= 0:
            sellable = 0  # Still show the lot but with 0 shares

        actual_sell = min(sellable, remaining) if remaining > 0 else 0
        if actual_sell > 0:
            remaining -= actual_sell

        recommendations.append(SellRecommendation(
            lot=lot,
            shares_to_sell=actual_sell,
            current_price=current_price,
            gain_per_share=gain_per_share,
            total_gain=gain_per_share * actual_sell if actual_sell > 0 else total_gain,
            taxable_gain=taxable_gain if actual_sell > 0 else taxable_gain,
            estimated_tax=compute_tax_on_gain(gain_per_share * actual_sell, lot.discount_factor, marginal_tax_rate) if actual_sell > 0 else est_tax,
            reason=reason,
            priority=priority,
        ))

    return recommendations


def compute_savings(recommendations: list[SellRecommendation]) -> dict:
    """Compute total tax savings vs worst-case (FIFO) selection."""
    if not recommendations:
        return {"optimal_tax": 0, "worst_case_tax": 0, "savings": 0}

    # Optimal: use our recommended order
    optimal_tax = sum(r.estimated_tax for r in recommendations if r.shares_to_sell > 0)

    # Worst case: reverse priority (sell worst lots first)
    reversed_recs = sorted(recommendations, key=lambda r: (-r.priority, -r.estimated_tax))
    worst_tax = sum(r.estimated_tax for r in reversed_recs if r.shares_to_sell > 0)

    return {
        "optimal_tax": optimal_tax,
        "worst_case_tax": worst_tax,
        "savings": worst_tax - optimal_tax,
    }


def generate_tax_lot_html(
    symbol: str,
    recommendations: list[SellRecommendation],
    savings: dict,
    output_path: Path = DEFAULT_OUT,
) -> Path:
    """Generate HTML report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = ""
    for r in recommendations:
        badge = "🟢" if r.priority == 1 else "🟡" if r.priority == 2 else "🔴"
        discount_text = "50% discount" if r.lot.is_long_term else "No discount"
        sell_marker = f"<strong>{r.shares_to_sell:.0f}</strong>" if r.shares_to_sell > 0 else "—"

        rows += f"""<tr>
  <td>{badge} {r.priority}</td>
  <td>{r.lot.buy_date}</td>
  <td>{r.lot.shares:.0f}</td>
  <td>${r.lot.cost_per_share:.2f}</td>
  <td>{r.lot.holding_days}d</td>
  <td>{discount_text}</td>
  <td>{sell_marker}</td>
  <td>${r.gain_per_share:+.2f}</td>
  <td>${r.estimated_tax:,.0f}</td>
  <td>{r.reason}</td>
</tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Tax Lot Optimizer — {symbol}</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; background: #0d1117; color: #c9d1d9; }}
  h1,h2 {{ color: #58a6ff; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 20px 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }}
  .card .value {{ font-size: 1.8em; font-weight: bold; }}
  .card .label {{ font-size: 0.85em; color: #8b949e; }}
  .savings {{ color: #3fb950; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th,td {{ border: 1px solid #30363d; padding: 8px 10px; text-align: center; font-size: 0.9em; }}
  th {{ background: #161b22; color: #58a6ff; }}
  footer {{ margin-top: 20px; color: #484f58; font-size: 0.85em; }}
</style>
</head><body>
<h1>🏦 Tax Lot Optimizer — {symbol}</h1>

<div class="grid">
  <div class="card"><div class="value">${savings.get('optimal_tax', 0):,.0f}</div><div class="label">Optimal Tax</div></div>
  <div class="card"><div class="value">${savings.get('worst_case_tax', 0):,.0f}</div><div class="label">Worst Case Tax</div></div>
  <div class="card"><div class="value savings">${savings.get('savings', 0):,.0f}</div><div class="label">Tax Savings</div></div>
</div>

<h2>Lot Analysis (Best → Worst)</h2>
<table>
<thead><tr><th>Priority</th><th>Buy Date</th><th>Shares</th><th>Cost/Share</th><th>Held</th><th>Discount</th><th>Sell Qty</th><th>Gain/Share</th><th>Est. Tax</th><th>Reason</th></tr></thead>
<tbody>{rows}</tbody>
</table>

<footer>Tax Lot Optimizer v1.0 | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | Not financial advice</footer>
</body></html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"✅ Tax lot report saved to {output_path}")
    return output_path


def main(args: Optional[argparse.Namespace] = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Tax Lot Optimizer — minimise CGT")
        parser.add_argument("--symbol", required=True, help="Stock symbol")
        parser.add_argument("--shares", type=float, required=True, help="Shares to sell")
        parser.add_argument("--price", type=float, help="Current price (auto-fetched if omitted)")
        parser.add_argument("--tax-rate", type=float, default=0.37, help="Marginal tax rate")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output HTML path")
        parser.add_argument("--lots-json", help="Path to JSON file with lot data")
        args = parser.parse_args()

    print(f"🏦 Optimizing lot selection for {args.symbol} ({args.shares} shares)...")

    # Load lots from JSON if provided, otherwise use sample
    lots = []
    if args.lots_json and Path(args.lots_json).exists():
        with open(args.lots_json, "r") as f:
            raw = json.load(f)
        for item in raw:
            lots.append(TaxLot(
                symbol=item["symbol"],
                buy_date=date.fromisoformat(item["buy_date"]),
                shares=float(item["shares"]),
                cost_per_share=float(item["cost_per_share"]),
                broker=item.get("broker", ""),
                currency=item.get("currency", "AUD"),
            ))
    else:
        print("  ⚠️ No --lots-json provided. Use --lots-json to specify lot data.")
        return

    # Get current price
    current_price = args.price
    if not current_price:
        try:
            import yfinance as yf
            t = yf.Ticker(args.symbol)
            current_price = t.fast_info.last_price
        except Exception:
            print("  ❌ Could not fetch price. Use --price flag.")
            return

    recommendations = optimize_lot_selection(lots, args.shares, current_price, args.tax_rate)
    savings = compute_savings(recommendations)

    print(f"  Optimal tax: ${savings['optimal_tax']:,.0f}")
    print(f"  Worst case:  ${savings['worst_case_tax']:,.0f}")
    print(f"  Savings:     ${savings['savings']:,.0f}")

    generate_tax_lot_html(args.symbol, recommendations, savings, Path(args.out))


if __name__ == "__main__":
    main()
