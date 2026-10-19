"""F10 — 13F Filing Tracker (SEC EDGAR).

Tracks institutional filings from SEC EDGAR.
Monitors: Berkshire Hathaway, ARK Invest, Vanguard positions in stocks you hold.
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
DEFAULT_OUT = ROOT / "reports" / "sec_13f.html"

# CIK numbers for tracked institutions
TRACKED_INSTITUTIONS: dict[str, str] = {
    "Berkshire Hathaway": "0001067983",
    "ARK Invest": "0001603466",
    "Vanguard Group": "0000102909",
    "BlackRock": "0001364742",
    "Bridgewater Associates": "0001350694",
    "Citadel Advisors": "0001423053",
}

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{symbol}%22&dateRange=custom&startdt={start}&enddt={end}&forms=13F-HR"
EDGAR_FILINGS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


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
class InstitutionalHolding:
    """A tracked institution's holding in one of our stocks."""
    institution: str
    symbol: str
    shares: int
    value_usd: float
    filing_date: str        # YYYY-MM-DD
    change_type: str = ""   # "new", "increased", "decreased", "sold"
    change_pct: float = 0.0


def build_institutional_data(
    positions: list[dict[str, Any]],
    filings_data: dict[str, list[dict[str, Any]]] | None = None,
) -> list[InstitutionalHolding]:
    """Match institutional filings against our holdings.

    Args:
        positions: our portfolio positions.
        filings_data: dict keyed by institution name → list of
            {"symbol", "shares", "value_usd", "filing_date", "change_type", "change_pct"}.

    Returns:
        list of InstitutionalHolding sorted by value_usd descending.
    """
    our_symbols = {p.get("symbol", "").upper() for p in positions if p.get("symbol")}
    holdings: list[InstitutionalHolding] = []
    data = filings_data or {}

    for inst, entries in data.items():
        for e in entries:
            sym = e.get("symbol", "").upper()
            if sym not in our_symbols:
                continue
            holdings.append(InstitutionalHolding(
                institution=inst,
                symbol=sym,
                shares=int(e.get("shares", 0)),
                value_usd=float(e.get("value_usd", 0)),
                filing_date=e.get("filing_date", ""),
                change_type=e.get("change_type", ""),
                change_pct=float(e.get("change_pct", 0)),
            ))

    holdings.sort(key=lambda h: h.value_usd, reverse=True)
    return holdings


def summarize_by_symbol(
    holdings: list[InstitutionalHolding],
) -> dict[str, list[InstitutionalHolding]]:
    """Group holdings by stock symbol."""
    result: dict[str, list[InstitutionalHolding]] = {}
    for h in holdings:
        result.setdefault(h.symbol, []).append(h)
    return result


def summarize_by_institution(
    holdings: list[InstitutionalHolding],
) -> dict[str, int]:
    """Count how many of our stocks each institution holds."""
    counts: dict[str, int] = {}
    for h in holdings:
        counts[h.institution] = counts.get(h.institution, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


# ─── HTML ─────────────────────────────────────────────────────────────────────

def generate_html(
    holdings: list[InstitutionalHolding],
    out: Path | str | None = None,
) -> str:
    inst_counts = summarize_by_institution(holdings)
    unique_stocks = len(set(h.symbol for h in holdings))

    rows = ""
    for h in holdings[:100]:
        change_cls = ""
        if h.change_type in ("new", "increased"):
            change_cls = "pos"
        elif h.change_type in ("decreased", "sold"):
            change_cls = "neg"
        change_str = f"{h.change_type}" + (f" ({h.change_pct:+.1f}%)" if h.change_pct else "")
        rows += (
            f"<tr>"
            f"<td><strong>{h.symbol}</strong></td>"
            f"<td>{h.institution}</td>"
            f"<td class='num'>{h.shares:,}</td>"
            f"<td class='num'>${h.value_usd:,.0f}</td>"
            f"<td>{h.filing_date}</td>"
            f"<td class='{change_cls}'>{change_str}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>13F Filing Tracker</title>
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
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; display: flex; gap: 32px; }}
.stat {{ text-align: center; }}
.stat .val {{ font-size: 1.4em; font-weight: 600; }}
.stat .lbl {{ font-size: .8em; color: #8b949e; }}
</style></head><body>
<h1>13F Institutional Filing Tracker</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(holdings)}</div><div class="lbl">Matches</div></div>
  <div class="stat"><div class="val">{unique_stocks}</div><div class="lbl">Stocks Tracked</div></div>
  <div class="stat"><div class="val">{len(inst_counts)}</div><div class="lbl">Institutions</div></div>
</div>
<table>
<thead><tr><th>Symbol</th><th>Institution</th><th>Shares</th>
<th>Value</th><th>Filing Date</th><th>Change</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="6" style="text-align:center;color:#8b949e">No matching 13F filings — provide filings data</td></tr>'}
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
        parser = argparse.ArgumentParser(description="13F Filing Tracker")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    # Without live EDGAR data, produce empty report
    holdings = build_institutional_data(positions)
    generate_html(holdings, args.out)
    print(f"13F Tracker: {len(holdings)} institutional matches")


if __name__ == "__main__":
    main()
