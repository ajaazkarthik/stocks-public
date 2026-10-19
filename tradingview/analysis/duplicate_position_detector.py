"""F2 — Duplicate Position Detector (Cross-Broker).

Reads .cache_global_positions.json, groups by normalized symbol,
flags positions held across multiple brokers.

Output: console summary + HTML report.
"""
from __future__ import annotations

import json
import re
import sys
import argparse
from collections import defaultdict
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
DEFAULT_OUT = ROOT / "reports" / "duplicate_positions.html"

_SUFFIX_RE = re.compile(r"\.(AX|NS|BO|HK|SI|TO|L|SS|SZ)$", re.IGNORECASE)
_EXCHANGE_PREFIX_RE = re.compile(r"^(ASX|NSE|BSE|HKEX|SGX|NYSE|NASDAQ|AMEX):", re.IGNORECASE)

# ─── helpers ──────────────────────────────────────────────────────────────────

def normalize_symbol(raw: str) -> str:
    """Strip exchange prefix and yfinance suffix to get the bare ticker.

    >>> normalize_symbol("ASX:BHP")
    'BHP'
    >>> normalize_symbol("BHP.AX")
    'BHP'
    >>> normalize_symbol("INFY.NS")
    'INFY'
    >>> normalize_symbol("NVDA")
    'NVDA'
    """
    s = raw.strip().upper()
    s = _EXCHANGE_PREFIX_RE.sub("", s)
    s = _SUFFIX_RE.sub("", s)
    return s


def load_positions(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load cached positions JSON.  Returns empty list on any failure."""
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


@dataclass
class DuplicateGroup:
    """A symbol held in ≥ 2 brokers."""
    symbol: str
    positions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def broker_count(self) -> int:
        return len({p.get("broker", "?") for p in self.positions})

    @property
    def total_value(self) -> float:
        return sum(float(p.get("market_val", 0)) for p in self.positions)

    @property
    def total_shares(self) -> float:
        return sum(float(p.get("shares", 0)) for p in self.positions)

    @property
    def brokers(self) -> list[str]:
        return sorted({p.get("broker", "?") for p in self.positions})


def detect_duplicates(positions: list[dict[str, Any]]) -> list[DuplicateGroup]:
    """Find symbols held across multiple brokers.

    Returns list of DuplicateGroup sorted by total_value descending.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for pos in positions:
        sym = normalize_symbol(pos.get("symbol", ""))
        if sym:
            groups[sym].append(pos)

    dupes: list[DuplicateGroup] = []
    for sym, pos_list in groups.items():
        brokers = {p.get("broker", "?") for p in pos_list}
        if len(brokers) >= 2:
            dupes.append(DuplicateGroup(symbol=sym, positions=pos_list))

    dupes.sort(key=lambda g: g.total_value, reverse=True)
    return dupes


def portfolio_total_value(positions: list[dict[str, Any]]) -> float:
    """Sum of all market_val across all positions."""
    return sum(float(p.get("market_val", 0)) for p in positions)


# ─── HTML report ──────────────────────────────────────────────────────────────

def generate_html(
    groups: list[DuplicateGroup],
    total_portfolio: float,
    out: Path | str | None = None,
) -> str:
    """Build an HTML report of cross-broker duplicate positions."""
    rows = ""
    for g in groups:
        pct = (g.total_value / total_portfolio * 100) if total_portfolio > 0 else 0
        broker_list = ", ".join(g.brokers)
        rows += (
            f"<tr>"
            f"<td><strong>{g.symbol}</strong></td>"
            f"<td>{g.broker_count}</td>"
            f"<td>{broker_list}</td>"
            f"<td class='num'>{g.total_shares:,.0f}</td>"
            f"<td class='num'>${g.total_value:,.0f}</td>"
            f"<td class='num'>{pct:.1f}%</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Duplicate Position Detector</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1100px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
th {{ background: #161b22; color: #8b949e; text-align: left; padding: 8px 12px;
      border-bottom: 1px solid #30363d; font-size: .85em; text-transform: uppercase; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; }}
</style></head><body>
<h1>Cross-Broker Duplicate Positions</h1>
<div class="summary">
  <strong>{len(groups)}</strong> symbols held across multiple brokers &nbsp;|&nbsp;
  Total duplicate exposure: <strong>${sum(g.total_value for g in groups):,.0f}</strong>
  ({(sum(g.total_value for g in groups) / total_portfolio * 100) if total_portfolio > 0 else 0:.1f}% of portfolio)
</div>
<table>
<thead><tr><th>Symbol</th><th>Brokers</th><th>Broker Names</th>
<th>Total Shares</th><th>Total Value</th><th>% of Portfolio</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="6" style="text-align:center;color:#8b949e">No cross-broker duplicates found</td></tr>'}
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
        parser = argparse.ArgumentParser(description="Duplicate Position Detector")
        parser.add_argument("--cache", default=str(CACHE_PATH), help="Path to positions JSON")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="HTML output path")
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found. Run global_portfolio_dashboard.py first.")
        return

    dupes = detect_duplicates(positions)
    total = portfolio_total_value(positions)

    print(f"\n{'═' * 60}")
    print(f"  CROSS-BROKER DUPLICATE POSITIONS")
    print(f"{'═' * 60}")
    if dupes:
        for g in dupes:
            pct = (g.total_value / total * 100) if total > 0 else 0
            print(f"  {g.symbol:8s} — {g.broker_count} brokers ({', '.join(g.brokers)})")
            print(f"           ${g.total_value:>10,.0f} total | {pct:.1f}% of portfolio")
    else:
        print("  No cross-broker duplicates found.")
    print(f"{'═' * 60}\n")

    generate_html(dupes, total, args.out)


if __name__ == "__main__":
    main()
