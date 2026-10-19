"""F14 — Broker P&L Reconciliation.

Compares calculated P&L (shares × (current_price − avg_cost))
against broker-reported P&L from .cache_global_positions.json.
Flags discrepancies > threshold %.
"""
from __future__ import annotations

import json
import sys
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = ROOT / ".cache_global_positions.json"
DEFAULT_OUT = ROOT / "reports" / "pnl_reconciliation.html"

_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]

# ─── helpers ──────────────────────────────────────────────────────────────────


def load_positions(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load cached positions JSON."""
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


@dataclass
class Discrepancy:
    """A position where calculated P&L ≠ broker-reported P&L."""
    symbol: str
    broker: str
    shares: float
    avg_cost: float
    current_price: float
    calculated_pnl: float
    reported_pnl: float
    diff_abs: float
    diff_pct: float
    likely_cause: str


def _guess_cause(calc: float, reported: float, shares: float) -> str:
    """Best-guess explanation for the discrepancy."""
    diff = abs(calc - reported)
    if shares == 0:
        return "zero shares — possible closed position or data lag"
    if diff < 1.0:
        return "rounding"
    if reported == 0 and calc != 0:
        return "broker reports zero — may not include unrealized P&L"
    if abs(calc) > 0 and diff / max(abs(calc), 1) > 0.5:
        return "possible stock split or corporate action"
    return "brokerage fees or FX conversion difference"


def reconcile_positions(
    positions: list[dict[str, Any]],
    threshold_pct: float = 1.0,
) -> list[Discrepancy]:
    """Compare calculated vs broker-reported P&L.

    Args:
        positions: list of position dicts from cache.
        threshold_pct: minimum absolute difference (%) to flag.

    Returns:
        list of Discrepancy sorted by diff_abs descending.
    """
    results: list[Discrepancy] = []

    for pos in positions:
        sym = pos.get("symbol", "")
        broker = pos.get("broker", "Unknown")
        shares = float(pos.get("shares", 0))
        avg_cost = float(pos.get("avg_cost", 0))
        cur_price = float(pos.get("current_price", 0))
        reported_pnl = float(pos.get("pnl", 0))

        calculated_pnl = shares * (cur_price - avg_cost)
        diff_abs = abs(calculated_pnl - reported_pnl)

        # Calculate percentage diff based on larger absolute value
        base = max(abs(calculated_pnl), abs(reported_pnl), 1.0)
        diff_pct = (diff_abs / base) * 100

        if diff_pct >= threshold_pct and diff_abs > 0.01:
            cause = _guess_cause(calculated_pnl, reported_pnl, shares)
            results.append(Discrepancy(
                symbol=sym,
                broker=broker,
                shares=shares,
                avg_cost=avg_cost,
                current_price=cur_price,
                calculated_pnl=round(calculated_pnl, 2),
                reported_pnl=round(reported_pnl, 2),
                diff_abs=round(diff_abs, 2),
                diff_pct=round(diff_pct, 2),
                likely_cause=cause,
            ))

    results.sort(key=lambda d: d.diff_abs, reverse=True)
    return results


def broker_summary(
    positions: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """Aggregate P&L reconciliation per broker."""
    summary: dict[str, dict[str, float]] = {
        b: {"calc_pnl": 0.0, "reported_pnl": 0.0, "count": 0}
        for b in _KNOWN_BROKERS
    }
    for pos in positions:
        broker = pos.get("broker", "Unknown")
        if broker not in summary:
            summary[broker] = {"calc_pnl": 0.0, "reported_pnl": 0.0, "count": 0}
        shares = float(pos.get("shares", 0))
        avg_cost = float(pos.get("avg_cost", 0))
        cur_price = float(pos.get("current_price", 0))
        summary[broker]["calc_pnl"] += shares * (cur_price - avg_cost)
        summary[broker]["reported_pnl"] += float(pos.get("pnl", 0))
        summary[broker]["count"] += 1
    return summary


# ─── HTML report ──────────────────────────────────────────────────────────────

def generate_html(
    discrepancies: list[Discrepancy],
    broker_stats: dict[str, dict[str, float]],
    out: Path | str | None = None,
) -> str:
    """Build an HTML P&L reconciliation report."""
    # Broker summary table
    broker_rows = ""
    for b in _KNOWN_BROKERS:
        s = broker_stats.get(b, {"calc_pnl": 0, "reported_pnl": 0, "count": 0})
        diff = s["calc_pnl"] - s["reported_pnl"]
        cls = "pos" if diff >= 0 else "neg"
        broker_rows += (
            f"<tr>"
            f"<td><strong>{b}</strong></td>"
            f"<td class='num'>{int(s['count'])}</td>"
            f"<td class='num'>${s['calc_pnl']:,.0f}</td>"
            f"<td class='num'>${s['reported_pnl']:,.0f}</td>"
            f"<td class='num {cls}'>${diff:,.0f}</td>"
            f"</tr>\n"
        )

    # Discrepancy detail rows
    detail_rows = ""
    for d in discrepancies[:50]:  # cap at 50
        cls = "pos" if d.calculated_pnl >= d.reported_pnl else "neg"
        detail_rows += (
            f"<tr>"
            f"<td>{d.symbol}</td>"
            f"<td>{d.broker}</td>"
            f"<td class='num'>${d.calculated_pnl:,.2f}</td>"
            f"<td class='num'>${d.reported_pnl:,.2f}</td>"
            f"<td class='num {cls}'>${d.diff_abs:,.2f} ({d.diff_pct:.1f}%)</td>"
            f"<td>{d.likely_cause}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>P&L Reconciliation Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1200px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
h1,h2 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
th {{ background: #161b22; color: #8b949e; text-align: left; padding: 8px 12px;
      border-bottom: 1px solid #30363d; font-size: .85em; text-transform: uppercase; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #3fb950; }}
.neg {{ color: #f85149; }}
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; }}
</style></head><body>
<h1>Broker P&L Reconciliation</h1>
<div class="summary">
  <strong>{len(discrepancies)}</strong> positions with P&L discrepancy &gt; threshold
</div>

<h2>Broker Summary</h2>
<table>
<thead><tr><th>Broker</th><th>Positions</th><th>Calculated P&L</th>
<th>Reported P&L</th><th>Difference</th></tr></thead>
<tbody>{broker_rows}</tbody></table>

<h2>Discrepancy Details</h2>
<table>
<thead><tr><th>Symbol</th><th>Broker</th><th>Calculated</th>
<th>Reported</th><th>Diff</th><th>Likely Cause</th></tr></thead>
<tbody>
{detail_rows if detail_rows else '<tr><td colspan="6" style="text-align:center;color:#8b949e">No discrepancies above threshold</td></tr>'}
</tbody></table>
</body></html>"""

    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        print(f"Report saved → {p}")
    return html


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Broker P&L Reconciliation")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        parser.add_argument("--threshold", type=float, default=1.0,
                            help="Min discrepancy %% to flag (default 1.0)")
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found. Run global_portfolio_dashboard.py first.")
        return

    discrepancies = reconcile_positions(positions, args.threshold)
    summary = broker_summary(positions)
    generate_html(discrepancies, summary, args.out)

    print(f"\n{'═' * 60}")
    print(f"  P&L RECONCILIATION — {len(discrepancies)} discrepancies found")
    print(f"{'═' * 60}")
    for d in discrepancies[:10]:
        print(f"  {d.symbol:8s} ({d.broker}) — calc ${d.calculated_pnl:,.2f} vs "
              f"reported ${d.reported_pnl:,.2f} → Δ${d.diff_abs:,.2f} ({d.diff_pct:.1f}%)")
        print(f"           Likely: {d.likely_cause}")
    print()


if __name__ == "__main__":
    main()
