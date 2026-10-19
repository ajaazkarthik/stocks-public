"""F7 — ETF Holdings Overlap Analyzer.

Crosschecks ETF holdings (via yfinance get_funds_data) against direct stock positions.
Shows overlap: "NVDA 9.2% from ETFs + 3.1% direct = 12.3% total exposure".
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
DEFAULT_OUT = ROOT / "reports" / "etf_overlap.html"


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
class ETFHolding:
    """A single ETF's holding of a stock."""
    etf_symbol: str
    stock_symbol: str
    weight_pct: float      # % of ETF allocated to this stock
    etf_value: float       # our position value in the ETF
    implied_value: float   # weight_pct * etf_value = indirect exposure


@dataclass
class OverlapEntry:
    """Total overlap for one stock across all ETFs + direct holdings."""
    stock_symbol: str
    direct_value: float = 0.0
    direct_pct: float = 0.0         # % of total portfolio
    indirect_value: float = 0.0     # sum of implied values via ETFs
    indirect_pct: float = 0.0       # % of total portfolio via ETFs
    total_pct: float = 0.0          # direct_pct + indirect_pct
    etf_sources: list = field(default_factory=list)  # [(etf_sym, weight_pct)]


def is_etf(symbol: str) -> bool:
    """Simple heuristic for ETF detection."""
    known_etfs = {
        "SPY", "QQQ", "IVV", "VOO", "VTI", "VXUS", "VEA", "VWO",
        "AGG", "BND", "VNQ", "GLD", "SLV", "TLT", "EFA", "EEM",
        "XLK", "XLF", "XLE", "XLV", "XLP", "XLY", "XLI", "XLU",
        "ARKK", "ARKG", "ARKW", "ARKF", "ARKQ",
        "SOXL", "TQQQ", "UPRO", "SPXL", "SOXS",
        "IWM", "DIA", "VGT", "SCHD", "JEPI", "JEPQ",
        "IOZ", "VAS", "A200", "STW", "VGS", "VDHG", "NDQ",
        # Indian ETFs
        "NIFTYBEES", "BANKBEES", "GOLDBEES",
    }
    return symbol.upper() in known_etfs


def identify_etfs(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter positions to just ETFs."""
    return [p for p in positions if is_etf(p.get("symbol", ""))]


def identify_stocks(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter positions to non-ETF stocks."""
    return [p for p in positions if not is_etf(p.get("symbol", ""))]


def compute_overlap(
    positions: list[dict[str, Any]],
    etf_holdings: dict[str, list[dict[str, Any]]] | None = None,
) -> list[OverlapEntry]:
    """Compute overlap between ETF indirect exposure and direct holdings.

    Args:
        positions: all positions (ETFs + stocks).
        etf_holdings: dict keyed by ETF symbol → list of
            {"symbol": "AAPL", "weight": 6.5} (% weight).

    Returns:
        list of OverlapEntry sorted by total_pct descending.
    """
    total_portfolio = sum(float(p.get("market_val", 0)) for p in positions)
    if total_portfolio <= 0:
        return []

    holdings_map = etf_holdings or {}

    # Direct stock exposure
    direct: dict[str, float] = {}
    for p in identify_stocks(positions):
        sym = p.get("symbol", "").upper()
        if sym:
            direct[sym] = direct.get(sym, 0) + float(p.get("market_val", 0))

    # Indirect exposure via ETFs
    indirect: dict[str, float] = {}        # stock → total indirect value
    etf_sources: dict[str, list] = {}      # stock → [(etf, weight)]
    for p in identify_etfs(positions):
        etf_sym = p.get("symbol", "").upper()
        etf_val = float(p.get("market_val", 0))
        for h in holdings_map.get(etf_sym, []):
            stock = h.get("symbol", "").upper()
            weight = float(h.get("weight", 0)) / 100.0  # convert % to fraction
            implied = etf_val * weight
            indirect[stock] = indirect.get(stock, 0) + implied
            if stock not in etf_sources:
                etf_sources[stock] = []
            etf_sources[stock].append((etf_sym, h.get("weight", 0)))

    # Combine — only show stocks with BOTH direct AND indirect, or just indirect > 0
    all_stocks = set(direct.keys()) | set(indirect.keys())
    entries: list[OverlapEntry] = []

    for sym in all_stocks:
        d_val = direct.get(sym, 0)
        i_val = indirect.get(sym, 0)

        # Only show if there's overlap (stock held directly AND via ETF)
        # or if indirect exposure is meaningful
        if d_val == 0 and i_val == 0:
            continue

        d_pct = (d_val / total_portfolio) * 100
        i_pct = (i_val / total_portfolio) * 100

        entries.append(OverlapEntry(
            stock_symbol=sym,
            direct_value=round(d_val, 2),
            direct_pct=round(d_pct, 2),
            indirect_value=round(i_val, 2),
            indirect_pct=round(i_pct, 2),
            total_pct=round(d_pct + i_pct, 2),
            etf_sources=etf_sources.get(sym, []),
        ))

    entries.sort(key=lambda e: e.total_pct, reverse=True)
    return entries


def filter_overlapping(entries: list[OverlapEntry]) -> list[OverlapEntry]:
    """Keep only entries with both direct and indirect exposure."""
    return [e for e in entries if e.direct_value > 0 and e.indirect_value > 0]


# ─── HTML ─────────────────────────────────────────────────────────────────────

def generate_html(
    entries: list[OverlapEntry],
    out: Path | str | None = None,
) -> str:
    overlapping = filter_overlapping(entries)

    rows = ""
    for e in entries[:50]:
        has_both = e.direct_value > 0 and e.indirect_value > 0
        overlap_badge = "<span style='color:#d29922'>⚠ overlap</span>" if has_both else ""
        etf_list = ", ".join(f"{s[0]} ({s[1]:.1f}%)" for s in e.etf_sources[:5])
        rows += (
            f"<tr>"
            f"<td><strong>{e.stock_symbol}</strong> {overlap_badge}</td>"
            f"<td class='num'>{e.direct_pct:.2f}%</td>"
            f"<td class='num'>{e.indirect_pct:.2f}%</td>"
            f"<td class='num'><strong>{e.total_pct:.2f}%</strong></td>"
            f"<td style='font-size:.85em'>{etf_list}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>ETF Holdings Overlap</title>
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
            border-radius: 6px; margin: 16px 0; display: flex; gap: 32px; }}
.stat {{ text-align: center; }}
.stat .val {{ font-size: 1.4em; font-weight: 600; }}
.stat .lbl {{ font-size: .8em; color: #8b949e; }}
</style></head><body>
<h1>ETF Holdings Overlap</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(entries)}</div><div class="lbl">Stocks Exposed</div></div>
  <div class="stat"><div class="val" style="color:#d29922">{len(overlapping)}</div><div class="lbl">Direct + ETF Overlap</div></div>
</div>
<table>
<thead><tr><th>Stock</th><th>Direct %</th><th>Via ETF %</th>
<th>Total %</th><th>ETF Sources</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="5" style="text-align:center;color:#8b949e">No overlap data — provide ETF holdings</td></tr>'}
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
        parser = argparse.ArgumentParser(description="ETF Holdings Overlap Analyzer")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    entries = compute_overlap(positions)
    generate_html(entries, args.out)
    overlapping = filter_overlapping(entries)
    print(f"ETF overlap: {len(entries)} stocks, {len(overlapping)} with dual exposure")


if __name__ == "__main__":
    main()
