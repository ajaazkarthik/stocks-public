"""F6 — Dividend Income Calendar.

Sources: yfinance dividends + TV screener next_dividend_date.
Produces 90-day calendar sorted by ex-div date with projected income.
"""
from __future__ import annotations

import json
import sys
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = ROOT / ".cache_global_positions.json"
DEFAULT_OUT = ROOT / "reports" / "dividend_calendar.html"


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
class DividendEntry:
    """A single upcoming dividend event."""
    symbol: str
    broker: str
    ex_date: str             # YYYY-MM-DD
    amount_per_share: float
    shares: float
    projected_income: float
    frequency: str = "quarterly"   # quarterly, semi-annual, annual, monthly
    yield_pct: float = 0.0
    currency: str = "USD"


def estimate_frequency(dividends_per_year: int) -> str:
    if dividends_per_year >= 10:
        return "monthly"
    if dividends_per_year >= 3:
        return "quarterly"
    if dividends_per_year >= 2:
        return "semi-annual"
    return "annual"


def build_calendar(
    positions: list[dict[str, Any]],
    dividend_data: dict[str, dict[str, Any]] | None = None,
    horizon_days: int = 90,
) -> list[DividendEntry]:
    """Build a 90-day dividend calendar from positions + external data.

    Args:
        positions: list of position dicts.
        dividend_data: optional dict keyed by symbol with:
            - next_ex_date (YYYY-MM-DD)
            - amount_per_share (float)
            - dividends_per_year (int)
            - dividend_yield (float, %)
        horizon_days: how many days ahead to show (default 90).

    Returns:
        list of DividendEntry sorted by ex_date ascending.
    """
    seen: set[str] = set()
    entries: list[DividendEntry] = []
    div_data = dividend_data or {}
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() + timedelta(days=horizon_days)).strftime("%Y-%m-%d")

    for pos in positions:
        sym = pos.get("symbol", "")
        if not sym or sym in seen:
            continue
        seen.add(sym)

        d = div_data.get(sym, {})
        ex_date = d.get("next_ex_date", "")
        if not ex_date:
            continue

        # Filter to horizon
        if ex_date < today or ex_date > cutoff:
            continue

        amount = float(d.get("amount_per_share", 0))
        shares = float(pos.get("shares", 0))
        projected = amount * shares
        freq = estimate_frequency(int(d.get("dividends_per_year", 4)))
        yield_pct = float(d.get("dividend_yield", 0))
        currency = pos.get("currency", "USD")

        entries.append(DividendEntry(
            symbol=sym,
            broker=pos.get("broker", ""),
            ex_date=ex_date,
            amount_per_share=round(amount, 4),
            shares=shares,
            projected_income=round(projected, 2),
            frequency=freq,
            yield_pct=round(yield_pct, 2),
            currency=currency,
        ))

    entries.sort(key=lambda e: e.ex_date)
    return entries


def total_projected_income(entries: list[DividendEntry]) -> float:
    return sum(e.projected_income for e in entries)


# ─── HTML ─────────────────────────────────────────────────────────────────────

def generate_html(
    entries: list[DividendEntry],
    out: Path | str | None = None,
) -> str:
    total = total_projected_income(entries)

    rows = ""
    for e in entries:
        rows += (
            f"<tr>"
            f"<td>{e.ex_date}</td>"
            f"<td><strong>{e.symbol}</strong></td>"
            f"<td>{e.broker}</td>"
            f"<td class='num'>${e.amount_per_share:.4f}</td>"
            f"<td class='num'>{e.shares:,.0f}</td>"
            f"<td class='num pos'>${e.projected_income:,.2f}</td>"
            f"<td class='num'>{e.yield_pct:.2f}%</td>"
            f"<td>{e.frequency}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Dividend Income Calendar</title>
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
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; display: flex; gap: 32px; }}
.stat {{ text-align: center; }}
.stat .val {{ font-size: 1.4em; font-weight: 600; }}
.stat .lbl {{ font-size: .8em; color: #8b949e; }}
</style></head><body>
<h1>Dividend Income Calendar (90 days)</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(entries)}</div><div class="lbl">Upcoming Dividends</div></div>
  <div class="stat"><div class="val pos">${total:,.2f}</div><div class="lbl">Projected Income</div></div>
</div>
<table>
<thead><tr><th>Ex-Date</th><th>Symbol</th><th>Broker</th><th>$/Share</th>
<th>Shares</th><th>Projected</th><th>Yield</th><th>Freq</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="8" style="text-align:center;color:#8b949e">No upcoming dividends in next 90 days</td></tr>'}
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
        parser = argparse.ArgumentParser(description="Dividend Income Calendar")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        parser.add_argument("--days", type=int, default=90)
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    entries = build_calendar(positions, horizon_days=args.days)
    generate_html(entries, args.out)
    print(f"Dividend calendar: {len(entries)} upcoming dividends, "
          f"${total_projected_income(entries):,.2f} projected income")


if __name__ == "__main__":
    main()
