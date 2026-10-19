"""
pnl_attribution.py — Cross-Broker P&L Attribution Report
==========================================================
Reads cached positions from global_portfolio_dashboard.py and produces
a unified P&L attribution report showing top winners, losers, sector
breakdown, and broker breakdown.

Output: tradingview/reports/pnl_attribution.html

Usage:
    python tradingview/analysis/pnl_attribution.py
    python tradingview/analysis/pnl_attribution.py --positions-file .cache_global_positions.json
    python tradingview/analysis/pnl_attribution.py --out reports/pnl_attribution.html
"""

import sys, os, argparse, json
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent.parent
TV   = Path(__file__).parent.parent
_DEFAULT_POS  = ROOT / ".cache_global_positions.json"
_DEFAULT_OUT  = ROOT / "reports" / "pnl_attribution.html"
_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]


def compute_attribution(positions: list[dict]) -> dict:
    """
    Compute P&L attribution from a list of position dicts.

    Each position should have at minimum: symbol, pnl, broker, market_val
    Optional: sector, pnl_pct

    Returns:
        {
            "top_winners": [top 5 by PnL],
            "top_losers": [bottom 5 by PnL],
            "broker_pnl": {broker: total_pnl},
            "sector_pnl": {sector: total_pnl},
            "total_pnl": float,
            "total_positions": int,
        }
    """
    if not positions:
        return _empty_attribution()

    valid = [p for p in positions if p.get("pnl") is not None]
    if not valid:
        return _empty_attribution()

    # Sort by PnL
    by_pnl = sorted(valid, key=lambda p: float(p.get("pnl", 0)), reverse=True)

    top_winners = by_pnl[:5]
    top_losers = list(reversed(by_pnl[-5:])) if len(by_pnl) >= 5 else list(reversed(by_pnl))

    # Broker PnL — pre-seed all 6
    broker_pnl: dict[str, float] = {b: 0.0 for b in _KNOWN_BROKERS}
    for p in valid:
        b = p.get("broker", "Unknown")
        broker_pnl.setdefault(b, 0.0)
        broker_pnl[b] += float(p.get("pnl", 0))

    # Sector PnL
    sector_pnl: dict[str, float] = {}
    for p in valid:
        s = p.get("sector", "Unknown") or "Unknown"
        sector_pnl.setdefault(s, 0.0)
        sector_pnl[s] += float(p.get("pnl", 0))

    total_pnl = sum(float(p.get("pnl", 0)) for p in valid)

    return {
        "top_winners": top_winners,
        "top_losers": top_losers,
        "broker_pnl": broker_pnl,
        "sector_pnl": sector_pnl,
        "total_pnl": round(total_pnl, 2),
        "total_positions": len(valid),
    }


def _empty_attribution() -> dict:
    return {
        "top_winners": [],
        "top_losers": [],
        "broker_pnl": {b: 0.0 for b in _KNOWN_BROKERS},
        "sector_pnl": {},
        "total_pnl": 0.0,
        "total_positions": 0,
    }


def _pnl_color(pnl: float) -> str:
    if pnl > 0:
        return "#27ae60"
    elif pnl < 0:
        return "#e74c3c"
    return "#666"


def _pos_row(p: dict) -> str:
    sym = p.get("symbol", "?")
    pnl = float(p.get("pnl", 0))
    pct = float(p.get("pnl_pct", 0))
    broker = p.get("broker", "?")
    mval = float(p.get("market_val", 0))
    color = _pnl_color(pnl)
    return (
        f"<tr>"
        f"<td style='padding:6px 10px'><b>{sym}</b></td>"
        f"<td style='padding:6px 10px;text-align:right'>${mval:,.0f}</td>"
        f"<td style='padding:6px 10px;text-align:right;color:{color}'>${pnl:+,.2f}</td>"
        f"<td style='padding:6px 10px;text-align:right;color:{color}'>{pct:+.1f}%</td>"
        f"<td style='padding:6px 10px'>{broker}</td>"
        f"</tr>"
    )


def generate_html(data: dict) -> str:
    """Generate the P&L attribution HTML report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = data["total_pnl"]
    total_color = _pnl_color(total)

    # Top winners
    winner_rows = "\n".join(_pos_row(p) for p in data["top_winners"]) or "<tr><td colspan='5'>No data</td></tr>"

    # Top losers
    loser_rows = "\n".join(_pos_row(p) for p in data["top_losers"]) or "<tr><td colspan='5'>No data</td></tr>"

    # Broker PnL
    broker_rows = ""
    for b in _KNOWN_BROKERS:
        pnl = data["broker_pnl"].get(b, 0.0)
        color = _pnl_color(pnl)
        status = f"<span style='color:{color}'>${pnl:+,.2f}</span>" if pnl != 0 else "<span style='opacity:.45'>offline</span>"
        broker_rows += f"<tr><td style='padding:6px 10px'><b>{b}</b></td><td style='padding:6px 10px;text-align:right'>{status}</td></tr>\n"
    # Unknown brokers
    for b, pnl in data["broker_pnl"].items():
        if b not in _KNOWN_BROKERS:
            color = _pnl_color(pnl)
            broker_rows += f"<tr><td style='padding:6px 10px'>{b}</td><td style='padding:6px 10px;text-align:right;color:{color}'>${pnl:+,.2f}</td></tr>\n"

    # Sector PnL
    sector_rows = ""
    for s, pnl in sorted(data["sector_pnl"].items(), key=lambda x: -abs(x[1])):
        color = _pnl_color(pnl)
        sector_rows += f"<tr><td style='padding:6px 10px'>{s}</td><td style='padding:6px 10px;text-align:right;color:{color}'>${pnl:+,.2f}</td></tr>\n"
    if not sector_rows:
        sector_rows = "<tr><td colspan='2'>No sector data</td></tr>"

    header_th = "<th style='padding:6px 10px;text-align:left'>Symbol</th><th style='padding:6px 10px;text-align:right'>Market Val</th><th style='padding:6px 10px;text-align:right'>P&amp;L</th><th style='padding:6px 10px;text-align:right'>P&amp;L %</th><th style='padding:6px 10px'>Broker</th>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P&amp;L Attribution Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 20px; background: #f5f6fa; color: #2c3e50; }}
  .card {{ background: #fff; border-radius: 10px; padding: 20px; margin: 15px 0;
           box-shadow: 0 2px 8px rgba(0,0,0,.08); }}
  h1 {{ color: #2c3e50; }}
  h2 {{ color: #34495e; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ background: #2c3e50; color: #fff; padding: 8px 10px; text-align: left; }}
  tr:nth-child(even) {{ background: #f9f9f9; }}
  .total {{ font-size: 1.5em; font-weight: bold; }}
</style>
</head><body>
<h1>P&amp;L Attribution Report</h1>
<p style="color:#888">Generated: {now} | {data['total_positions']} positions</p>

<div class="card">
  <h2>Total Portfolio P&amp;L</h2>
  <div class="total" style="color:{total_color}">${total:+,.2f}</div>
</div>

<div class="card">
  <h2>Top 5 Winners</h2>
  <table><thead><tr>{header_th}</tr></thead><tbody>{winner_rows}</tbody></table>
</div>

<div class="card">
  <h2>Top 5 Losers</h2>
  <table><thead><tr>{header_th}</tr></thead><tbody>{loser_rows}</tbody></table>
</div>

<div class="card">
  <h2>Broker P&amp;L</h2>
  <table><thead><tr>
    <th style="padding:6px 10px">Broker</th>
    <th style="padding:6px 10px;text-align:right">P&amp;L</th>
  </tr></thead><tbody>{broker_rows}</tbody></table>
</div>

<div class="card">
  <h2>Sector P&amp;L</h2>
  <table><thead><tr>
    <th style="padding:6px 10px">Sector</th>
    <th style="padding:6px 10px;text-align:right">P&amp;L</th>
  </tr></thead><tbody>{sector_rows}</tbody></table>
</div>

</body></html>"""


def main(args) -> None:
    pos_path = Path(args.positions_file)
    out_path = Path(args.out)

    if not pos_path.exists():
        print(f"Positions file not found: {pos_path}")
        print("Run global_portfolio_dashboard.py first.")
        sys.exit(1)

    with open(pos_path, encoding="utf-8") as f:
        raw = json.load(f)

    # Cache file may be a dict {"positions": [...], "saved_at": ...} or a plain list
    if isinstance(raw, dict):
        positions = raw.get("positions", [])
    else:
        positions = raw

    print(f"Loaded {len(positions)} positions from {pos_path}")

    data = compute_attribution(positions)
    html = generate_html(data)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Report written to {out_path}")

    # Summary
    print(f"\n  Total P&L: ${data['total_pnl']:+,.2f}")
    if data["top_winners"]:
        w = data["top_winners"][0]
        print(f"  Top Winner: {w.get('symbol','?')} (${float(w.get('pnl',0)):+,.2f})")
    if data["top_losers"]:
        l = data["top_losers"][0]
        print(f"  Top Loser:  {l.get('symbol','?')} (${float(l.get('pnl',0)):+,.2f})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cross-Broker P&L Attribution Report")
    parser.add_argument(
        "--positions-file",
        default=str(_DEFAULT_POS),
        help="Path to cached positions JSON",
    )
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_OUT),
        help="Output HTML report path",
    )
    args = parser.parse_args()
    main(args)
