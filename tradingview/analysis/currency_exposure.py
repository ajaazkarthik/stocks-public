"""F3 — Currency Exposure Report.

Analyses per-currency exposure across all 6 brokers.
Shows AUD-equivalent breakdown + FX P&L attribution.
"""
from __future__ import annotations

import json
import sys
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = ROOT / ".cache_global_positions.json"
DEFAULT_OUT = ROOT / "reports" / "currency_exposure.html"

_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]

# Approximate FX rates to AUD (updated manually or via API)
DEFAULT_FX_RATES: dict[str, float] = {
    "AUD": 1.0,
    "USD": 1.55,   # 1 USD = ~1.55 AUD
    "SGD": 1.16,   # 1 SGD = ~1.16 AUD
    "HKD": 0.20,   # 1 HKD = ~0.20 AUD
    "INR": 0.019,  # 1 INR = ~0.019 AUD
    "GBP": 2.05,   # 1 GBP = ~2.05 AUD
    "EUR": 1.72,   # 1 EUR = ~1.72 AUD
    "JPY": 0.011,  # 1 JPY = ~0.011 AUD
    "CNY": 0.22,   # 1 CNY = ~0.22 AUD
}

# Currency inference from broker (fallback when position has no currency field)
BROKER_DEFAULT_CURRENCY: dict[str, str] = {
    "Moomoo-AU": "AUD",
    "Moomoo-SG": "SGD",
    "IBKR": "USD",
    "Webull AU": "AUD",
    "Zerodha": "INR",
    "AngelOne": "INR",
}


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
class CurrencyBucket:
    """Aggregation for one currency."""
    currency: str
    value_native: float = 0.0      # total market_val in native currency
    value_aud: float = 0.0         # total market_val in AUD equivalent
    pnl_native: float = 0.0       # total P&L in native currency
    pnl_aud: float = 0.0          # total P&L in AUD equivalent
    position_count: int = 0
    brokers: set = field(default_factory=set)

    @property
    def pct_of_portfolio(self) -> float:
        """Placeholder — set externally after computing total."""
        return 0.0


def infer_currency(pos: dict[str, Any]) -> str:
    """Determine the currency for a position."""
    cur = pos.get("currency", "").strip().upper()
    if cur:
        return cur
    broker = pos.get("broker", "")
    return BROKER_DEFAULT_CURRENCY.get(broker, "USD")


def build_exposure(
    positions: list[dict[str, Any]],
    fx_rates: dict[str, float] | None = None,
) -> list[CurrencyBucket]:
    """Group positions by currency and compute AUD-equivalent exposure.

    Args:
        positions: list of position dicts.
        fx_rates: currency → AUD multiplier. Uses defaults if None.

    Returns:
        list of CurrencyBucket sorted by value_aud descending.
    """
    rates = fx_rates or DEFAULT_FX_RATES
    buckets: dict[str, CurrencyBucket] = {}

    for pos in positions:
        cur = infer_currency(pos)
        if cur not in buckets:
            buckets[cur] = CurrencyBucket(currency=cur)

        b = buckets[cur]
        val = float(pos.get("market_val", 0))
        pnl = float(pos.get("pnl", 0))
        rate = rates.get(cur, 1.0)

        b.value_native += val
        b.value_aud += val * rate
        b.pnl_native += pnl
        b.pnl_aud += pnl * rate
        b.position_count += 1
        b.brokers.add(pos.get("broker", "Unknown"))

    result = sorted(buckets.values(), key=lambda b: b.value_aud, reverse=True)
    return result


def compute_percentages(
    buckets: list[CurrencyBucket],
) -> list[tuple[CurrencyBucket, float]]:
    """Return [(bucket, percentage_of_total)] tuples."""
    total = sum(b.value_aud for b in buckets)
    if total <= 0:
        return [(b, 0.0) for b in buckets]
    return [(b, b.value_aud / total * 100) for b in buckets]


# ─── HTML report ──────────────────────────────────────────────────────────────

def generate_html(
    buckets: list[CurrencyBucket],
    out: Path | str | None = None,
) -> str:
    """Build an HTML currency exposure report."""
    total_aud = sum(b.value_aud for b in buckets)
    total_pnl_aud = sum(b.pnl_aud for b in buckets)

    rows = ""
    for b in buckets:
        pct = (b.value_aud / total_aud * 100) if total_aud > 0 else 0
        pnl_cls = "pos" if b.pnl_aud >= 0 else "neg"
        bar_w = max(int(pct * 3), 1)  # visual bar width
        rows += (
            f"<tr>"
            f"<td><strong>{b.currency}</strong></td>"
            f"<td class='num'>{b.position_count}</td>"
            f"<td>{', '.join(sorted(b.brokers))}</td>"
            f"<td class='num'>${b.value_aud:,.0f}</td>"
            f"<td class='num {pnl_cls}'>${b.pnl_aud:,.0f}</td>"
            f"<td class='num'>{pct:.1f}%</td>"
            f"<td><div class='bar' style='width:{bar_w}px'></div></td>"
            f"</tr>\n"
        )

    pnl_cls = "pos" if total_pnl_aud >= 0 else "neg"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Currency Exposure Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1100px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
th {{ background: #161b22; color: #8b949e; text-align: left; padding: 8px 12px;
      border-bottom: 1px solid #30363d; font-size: .85em; text-transform: uppercase; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #3fb950; }}
.neg {{ color: #f85149; }}
.bar {{ background: #58a6ff; height: 14px; border-radius: 3px; }}
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; display: flex; gap: 32px; }}
.stat {{ text-align: center; }}
.stat .val {{ font-size: 1.5em; font-weight: 600; }}
.stat .lbl {{ font-size: .8em; color: #8b949e; }}
</style></head><body>
<h1>Currency Exposure Report</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(buckets)}</div><div class="lbl">Currencies</div></div>
  <div class="stat"><div class="val">${total_aud:,.0f}</div><div class="lbl">Total (AUD)</div></div>
  <div class="stat"><div class="val {pnl_cls}">${total_pnl_aud:,.0f}</div><div class="lbl">P&L (AUD)</div></div>
</div>
<table>
<thead><tr><th>Currency</th><th>Positions</th><th>Brokers</th>
<th>Value (AUD)</th><th>P&L (AUD)</th><th>% Portfolio</th><th></th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="7" style="text-align:center;color:#8b949e">No positions found</td></tr>'}
</tbody></table>
<p style="color:#8b949e;font-size:.8em;margin-top:24px">
FX rates are approximate. Use for allocation analysis, not accounting.
</p>
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
        parser = argparse.ArgumentParser(description="Currency Exposure Report")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found. Run global_portfolio_dashboard.py first.")
        return

    buckets = build_exposure(positions)
    generate_html(buckets, args.out)

    total_aud = sum(b.value_aud for b in buckets)
    print(f"\n{'═' * 55}")
    print(f"  CURRENCY EXPOSURE — {len(buckets)} currencies")
    print(f"{'═' * 55}")
    for b in buckets:
        pct = (b.value_aud / total_aud * 100) if total_aud > 0 else 0
        print(f"  {b.currency:4s}  ${b.value_aud:>12,.0f}  {pct:5.1f}%  ({b.position_count} pos)")
    print()


if __name__ == "__main__":
    main()
