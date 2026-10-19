"""F8 — Price Target Tracker.

For all holdings, pulls analyst consensus target from TV screener.
Shows: current price, target, upside %, number of analysts.
Sorted by biggest upside.
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
DEFAULT_OUT = ROOT / "reports" / "price_targets.html"


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
class PriceTarget:
    """Analyst target data for one symbol."""
    symbol: str
    current_price: float
    target_price: float
    upside_pct: float
    analyst_count: int
    recommendation: str   # "buy", "hold", "sell"
    broker: str = ""


def calculate_upside(current: float, target: float) -> float:
    """Calculate upside/downside percentage."""
    if current <= 0:
        return 0.0
    return ((target - current) / current) * 100


def classify_recommendation(rec_value: float | str | None) -> str:
    """Convert TV screener recommendation_mark to string.

    TV screener uses: 1=Strong Buy, 2=Buy, 3=Hold, 4=Sell, 5=Strong Sell.
    """
    if rec_value is None:
        return "unknown"
    try:
        val = float(rec_value)
    except (ValueError, TypeError):
        return str(rec_value).lower() if rec_value else "unknown"

    if val <= 1.5:
        return "strong buy"
    if val <= 2.5:
        return "buy"
    if val <= 3.5:
        return "hold"
    if val <= 4.5:
        return "sell"
    return "strong sell"


def build_targets(
    positions: list[dict[str, Any]],
    screener_data: dict[str, dict[str, Any]] | None = None,
) -> list[PriceTarget]:
    """Build price target records from positions + optional TV screener data.

    Args:
        positions: list of position dicts.
        screener_data: optional dict keyed by symbol with fields:
            - target_price
            - number_of_analysts
            - recommendation_mark

    Returns:
        list of PriceTarget sorted by upside_pct descending.
    """
    seen: set[str] = set()
    targets: list[PriceTarget] = []
    tv = screener_data or {}

    for pos in positions:
        sym = pos.get("symbol", "")
        if not sym or sym in seen:
            continue
        seen.add(sym)

        current = float(pos.get("current_price", 0))
        tv_data = tv.get(sym, {})

        raw_target = tv_data.get("target_price")
        if raw_target is None:
            continue  # skip if no target price
        try:
            target = float(raw_target)
        except (ValueError, TypeError):
            continue

        analyst_count = 0
        raw_count = tv_data.get("number_of_analysts")
        if raw_count is not None:
            try:
                analyst_count = int(float(raw_count))
            except (ValueError, TypeError):
                pass

        rec = classify_recommendation(tv_data.get("recommendation_mark"))
        upside = calculate_upside(current, target)

        targets.append(PriceTarget(
            symbol=sym,
            current_price=round(current, 2),
            target_price=round(target, 2),
            upside_pct=round(upside, 2),
            analyst_count=analyst_count,
            recommendation=rec,
            broker=pos.get("broker", ""),
        ))

    targets.sort(key=lambda t: t.upside_pct, reverse=True)
    return targets


# ─── HTML report ──────────────────────────────────────────────────────────────

def generate_html(
    targets: list[PriceTarget],
    out: Path | str | None = None,
) -> str:
    """Build an HTML price target report."""
    above = sum(1 for t in targets if t.upside_pct > 0)
    below = sum(1 for t in targets if t.upside_pct < 0)
    avg_upside = sum(t.upside_pct for t in targets) / len(targets) if targets else 0

    rows = ""
    for t in targets:
        cls = "pos" if t.upside_pct > 0 else ("neg" if t.upside_pct < 0 else "")
        rec_cls = "pos" if "buy" in t.recommendation else ("neg" if "sell" in t.recommendation else "")
        rows += (
            f"<tr>"
            f"<td><strong>{t.symbol}</strong></td>"
            f"<td>{t.broker}</td>"
            f"<td class='num'>${t.current_price:,.2f}</td>"
            f"<td class='num'>${t.target_price:,.2f}</td>"
            f"<td class='num {cls}'>{t.upside_pct:+.1f}%</td>"
            f"<td class='num'>{t.analyst_count}</td>"
            f"<td class='{rec_cls}'>{t.recommendation}</td>"
            f"</tr>\n"
        )

    avg_cls = "pos" if avg_upside > 0 else "neg"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Price Target Tracker</title>
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
<h1>Price Target Tracker</h1>
<div class="summary">
  <div class="stat"><div class="val">{len(targets)}</div><div class="lbl">Symbols</div></div>
  <div class="stat"><div class="val pos">{above}</div><div class="lbl">Above Target</div></div>
  <div class="stat"><div class="val neg">{below}</div><div class="lbl">Below Target</div></div>
  <div class="stat"><div class="val {avg_cls}">{avg_upside:+.1f}%</div><div class="lbl">Avg Upside</div></div>
</div>
<table>
<thead><tr><th>Symbol</th><th>Broker</th><th>Current</th>
<th>Target</th><th>Upside</th><th>Analysts</th><th>Rec</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="7" style="text-align:center;color:#8b949e">No price targets available</td></tr>'}
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
        parser = argparse.ArgumentParser(description="Price Target Tracker")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        args = parser.parse_args()

    positions = load_positions(args.cache)
    if not positions:
        print("No positions found.")
        return

    # Without TV screener data, we can only list symbols without targets
    targets = build_targets(positions)
    generate_html(targets, args.out)
    print(f"Price targets: {len(targets)} symbols with analyst targets")


if __name__ == "__main__":
    main()
