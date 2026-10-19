"""F12 — Covered Call Scanner.

Identifies positions suitable for covered call writing.
Criteria: IV rank > 60, not near earnings, profitable position.
Shows estimated premium income.
"""
from __future__ import annotations

import json
import math
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
DEFAULT_OUT = ROOT / "reports" / "covered_calls.html"


def load_positions(path: Path | str | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


@dataclass
class CoveredCallCandidate:
    """A position eligible for covered call writing."""
    symbol: str
    broker: str
    shares: int
    current_price: float
    avg_cost: float
    pnl_pct: float
    iv_rank: float          # 0-100
    days_to_earnings: int   # -1 if unknown
    eligible: bool = True
    reason: str = ""        # why not eligible
    contracts: int = 0      # shares // 100
    strike_price: float = 0.0
    est_premium: float = 0.0


def estimate_premium(price: float, iv_rank: float, dte: int = 30) -> float:
    """Estimate option premium per share using simplified Black-Scholes proxy.

    Premium ≈ price × (iv_rank/100) × sqrt(dte/365) × 0.05
    This is a rough estimate — actual premium depends on full options chain.
    """
    if price <= 0 or iv_rank <= 0 or dte <= 0:
        return 0.0
    return price * (iv_rank / 100) * math.sqrt(dte / 365) * 0.05


def suggest_strike(price: float, iv_rank: float) -> float:
    """Suggest OTM strike price based on IV rank.

    Higher IV → further OTM (more premium cushion).
    """
    if price <= 0:
        return 0.0
    if iv_rank > 80:
        otm_pct = 0.10   # 10% OTM for very high IV
    elif iv_rank > 60:
        otm_pct = 0.07   # 7% OTM
    elif iv_rank > 40:
        otm_pct = 0.05   # 5% OTM
    else:
        otm_pct = 0.03   # 3% OTM

    # Round to nearest $0.50
    raw = price * (1 + otm_pct)
    return round(raw * 2) / 2


def scan_covered_calls(
    positions: list[dict[str, Any]],
    options_data: dict[str, dict[str, Any]] | None = None,
    min_iv_rank: float = 40.0,
    min_shares: int = 100,
    earnings_buffer_days: int = 14,
) -> list[CoveredCallCandidate]:
    """Scan positions for covered call candidates.

    Args:
        positions: portfolio positions.
        options_data: dict keyed by symbol → {"iv_rank": float, "days_to_earnings": int}.
        min_iv_rank: minimum IV rank to consider (default 40).
        min_shares: minimum shares needed (default 100 for 1 contract).
        earnings_buffer_days: avoid selling calls within N days of earnings.
    """
    data = options_data or {}
    candidates: list[CoveredCallCandidate] = []

    for pos in positions:
        sym = pos.get("symbol", "")
        shares = int(float(pos.get("shares", 0)))
        price = float(pos.get("current_price", 0))
        avg_cost = float(pos.get("avg_cost", 0))
        pnl_pct = float(pos.get("pnl_pct", 0))
        broker = pos.get("broker", "")

        if pos.get("is_option"):
            continue

        opts = data.get(sym.upper(), {})
        iv_rank = float(opts.get("iv_rank", 0))
        dte = int(opts.get("days_to_earnings", -1))

        eligible = True
        reason = ""

        if shares < min_shares:
            eligible = False
            reason = f"Only {shares} shares (need {min_shares}+)"
        elif price <= 0:
            eligible = False
            reason = "No valid price"
        elif iv_rank < min_iv_rank:
            eligible = False
            reason = f"IV rank {iv_rank:.0f} < {min_iv_rank:.0f}"
        elif 0 <= dte <= earnings_buffer_days:
            eligible = False
            reason = f"Earnings in {dte} days"
        elif pnl_pct < -10:
            eligible = False
            reason = f"Position down {pnl_pct:.1f}% — risky call assignment"

        contracts = shares // 100
        strike = suggest_strike(price, iv_rank) if eligible else 0.0
        prem = estimate_premium(price, iv_rank) * 100 * contracts if eligible else 0.0

        candidates.append(CoveredCallCandidate(
            symbol=sym,
            broker=broker,
            shares=shares,
            current_price=round(price, 2),
            avg_cost=round(avg_cost, 2),
            pnl_pct=round(pnl_pct, 2),
            iv_rank=round(iv_rank, 1),
            days_to_earnings=dte,
            eligible=eligible,
            reason=reason,
            contracts=contracts,
            strike_price=round(strike, 2),
            est_premium=round(prem, 2),
        ))

    candidates.sort(key=lambda c: (not c.eligible, -c.est_premium))
    return candidates


# ─── HTML ─────────────────────────────────────────────────────────────────────

def generate_html(
    candidates: list[CoveredCallCandidate],
    out: Path | str | None = None,
) -> str:
    eligible = [c for c in candidates if c.eligible]
    total_premium = sum(c.est_premium for c in eligible)

    rows = ""
    for c in candidates[:50]:
        cls = "" if c.eligible else "style='opacity:.45'"
        pnl_cls = "pos" if c.pnl_pct >= 0 else "neg"
        rows += (
            f"<tr {cls}>"
            f"<td><strong>{c.symbol}</strong></td>"
            f"<td>{c.broker}</td>"
            f"<td class='num'>{c.shares}</td>"
            f"<td class='num'>${c.current_price:,.2f}</td>"
            f"<td class='num {pnl_cls}'>{c.pnl_pct:+.1f}%</td>"
            f"<td class='num'>{c.iv_rank:.0f}</td>"
            f"<td class='num'>{c.contracts}</td>"
            f"<td class='num'>${c.strike_price:,.2f}</td>"
            f"<td class='num pos'>${c.est_premium:,.0f}</td>"
            f"<td>{c.reason}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Covered Call Scanner</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1200px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
th {{ background: #161b22; color: #8b949e; text-align: left; padding: 8px 12px;
      border-bottom: 1px solid #30363d; font-size: .85em; text-transform: uppercase; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #3fb950; }}
.neg {{ color: #f85149; }}
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; display: flex; gap: 32px; }}
.stat {{ text-align: center; }}
.stat .val {{ font-size: 1.4em; font-weight: 600; }}
.stat .lbl {{ font-size: .8em; color: #8b949e; }}
</style></head><body>
<h1>Covered Call Scanner</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(eligible)}</div><div class="lbl">Eligible</div></div>
  <div class="stat"><div class="val">{len(candidates) - len(eligible)}</div><div class="lbl">Ineligible</div></div>
  <div class="stat"><div class="val pos">${total_premium:,.0f}</div><div class="lbl">Est. Monthly Premium</div></div>
</div>
<table>
<thead><tr><th>Symbol</th><th>Broker</th><th>Shares</th><th>Price</th>
<th>P&L</th><th>IV Rank</th><th>Contracts</th><th>Strike</th><th>Premium</th><th>Notes</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="10" style="text-align:center;color:#8b949e">No positions found</td></tr>'}
</tbody></table>
</body></html>"""

    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        print(f"Report saved → {p}")
    return html


def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Covered Call Scanner")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        parser.add_argument("--min-iv", type=float, default=40.0)
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    candidates = scan_covered_calls(positions, min_iv_rank=args.min_iv)
    generate_html(candidates, args.out)
    eligible = [c for c in candidates if c.eligible]
    print(f"Covered calls: {len(eligible)} eligible of {len(candidates)} positions")


if __name__ == "__main__":
    main()
