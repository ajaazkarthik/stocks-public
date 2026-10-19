"""F5 — Broker Fee/Cost Tracker.

Parses broker trade history for commissions/fees.
Reports: monthly cost, cost as % of trade value, cheapest broker per market.
Sources: Moomoo (commission field), Zerodha (brokerage), IBKR Flex (commissions), Webull PDF.
"""
from __future__ import annotations

import json
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
DEFAULT_OUT = ROOT / "reports" / "broker_fees.html"

_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]


@dataclass
class TradeFee:
    """A single trade's fee record."""
    broker: str
    symbol: str
    date: str             # YYYY-MM-DD
    trade_value: float    # absolute $ amount of the trade
    commission: float     # broker commission
    other_fees: float = 0.0   # stamp duty, exchange levy, etc.
    market: str = ""      # AU, US, IN, SG, HK

    @property
    def total_fee(self) -> float:
        return self.commission + self.other_fees

    @property
    def fee_pct(self) -> float:
        if self.trade_value <= 0:
            return 0.0
        return (self.total_fee / self.trade_value) * 100


@dataclass
class BrokerFeeStats:
    """Aggregated fee stats for one broker."""
    broker: str
    total_fees: float = 0.0
    total_trade_value: float = 0.0
    trade_count: int = 0
    monthly_avg: float = 0.0  # computed later
    markets: set = field(default_factory=set)

    @property
    def fee_pct(self) -> float:
        if self.total_trade_value <= 0:
            return 0.0
        return (self.total_fees / self.total_trade_value) * 100


def parse_trade_fees(raw_trades: list[dict[str, Any]]) -> list[TradeFee]:
    """Parse raw trade data into TradeFee objects.

    Expected dict keys: broker, symbol, date, trade_value, commission,
    other_fees (optional), market (optional).
    """
    fees: list[TradeFee] = []
    for t in raw_trades:
        broker = t.get("broker", "Unknown")
        symbol = t.get("symbol", "")
        date = t.get("date", "")
        trade_value = abs(float(t.get("trade_value", 0)))
        commission = abs(float(t.get("commission", 0)))
        other_fees = abs(float(t.get("other_fees", 0)))
        market = t.get("market", "")

        fees.append(TradeFee(
            broker=broker,
            symbol=symbol,
            date=date,
            trade_value=trade_value,
            commission=commission,
            other_fees=other_fees,
            market=market,
        ))
    return fees


def aggregate_by_broker(fees: list[TradeFee]) -> dict[str, BrokerFeeStats]:
    """Aggregate fees by broker. Pre-seeds all 6 known brokers."""
    stats: dict[str, BrokerFeeStats] = {
        b: BrokerFeeStats(broker=b) for b in _KNOWN_BROKERS
    }

    for f in fees:
        if f.broker not in stats:
            stats[f.broker] = BrokerFeeStats(broker=f.broker)
        s = stats[f.broker]
        s.total_fees += f.total_fee
        s.total_trade_value += f.trade_value
        s.trade_count += 1
        if f.market:
            s.markets.add(f.market)

    return stats


def aggregate_by_month(fees: list[TradeFee]) -> dict[str, dict[str, float]]:
    """Aggregate fees by YYYY-MM → {broker: total_fees}."""
    result: dict[str, dict[str, float]] = {}
    for f in fees:
        month = f.date[:7] if len(f.date) >= 7 else "unknown"
        if month not in result:
            result[month] = defaultdict(float)
        result[month][f.broker] += f.total_fee
    return dict(result)


def cheapest_broker_by_market(fees: list[TradeFee]) -> dict[str, tuple[str, float]]:
    """For each market, find the broker with lowest avg fee %.

    Returns: {market: (broker, avg_fee_pct)}.
    """
    # market → broker → (total_fees, total_value)
    data: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for f in fees:
        if not f.market:
            continue
        data[f.market][f.broker][0] += f.total_fee
        data[f.market][f.broker][1] += f.trade_value

    result: dict[str, tuple[str, float]] = {}
    for market, brokers in data.items():
        best_broker = ""
        best_pct = float("inf")
        for broker, (tot_fee, tot_val) in brokers.items():
            pct = (tot_fee / tot_val * 100) if tot_val > 0 else 0
            if pct < best_pct:
                best_pct = pct
                best_broker = broker
        result[market] = (best_broker, round(best_pct, 4))
    return result


# ─── HTML report ──────────────────────────────────────────────────────────────

def generate_html(
    broker_stats: dict[str, BrokerFeeStats],
    monthly: dict[str, dict[str, float]],
    cheapest: dict[str, tuple[str, float]],
    out: Path | str | None = None,
) -> str:
    """Build an HTML broker fee report."""
    total_fees = sum(s.total_fees for s in broker_stats.values())
    total_trades = sum(s.trade_count for s in broker_stats.values())

    # Broker summary rows
    broker_rows = ""
    for b in _KNOWN_BROKERS:
        s = broker_stats.get(b, BrokerFeeStats(broker=b))
        broker_rows += (
            f"<tr>"
            f"<td><strong>{b}</strong></td>"
            f"<td class='num'>{s.trade_count}</td>"
            f"<td class='num'>${s.total_trade_value:,.0f}</td>"
            f"<td class='num'>${s.total_fees:,.2f}</td>"
            f"<td class='num'>{s.fee_pct:.3f}%</td>"
            f"</tr>\n"
        )

    # Cheapest broker rows
    cheapest_rows = ""
    for market, (broker, pct) in sorted(cheapest.items()):
        cheapest_rows += (
            f"<tr>"
            f"<td>{market}</td>"
            f"<td><strong>{broker}</strong></td>"
            f"<td class='num pos'>{pct:.4f}%</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Broker Fee/Cost Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1100px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
h1,h2 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
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
<h1>Broker Fee/Cost Report</h1>
<div class="summary">
  <div class="stat"><div class="val">{total_trades}</div><div class="lbl">Trades</div></div>
  <div class="stat"><div class="val">${total_fees:,.2f}</div><div class="lbl">Total Fees</div></div>
</div>

<h2>Fee Summary by Broker</h2>
<table>
<thead><tr><th>Broker</th><th>Trades</th><th>Trade Value</th>
<th>Total Fees</th><th>Fee %</th></tr></thead>
<tbody>{broker_rows}</tbody></table>

<h2>Cheapest Broker by Market</h2>
<table>
<thead><tr><th>Market</th><th>Cheapest Broker</th><th>Avg Fee %</th></tr></thead>
<tbody>
{cheapest_rows if cheapest_rows else '<tr><td colspan="3" style="text-align:center;color:#8b949e">No market data available</td></tr>'}
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
        parser = argparse.ArgumentParser(description="Broker Fee/Cost Tracker")
        parser.add_argument("--trades", help="Path to JSON file with trade records")
        parser.add_argument("--out", default=str(DEFAULT_OUT))
        args = parser.parse_args()

    raw_trades: list[dict] = []
    if args.trades:
        p = Path(args.trades)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                raw_trades = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                pass

    if not raw_trades:
        print("No trade data found. Provide --trades <json_file>.")
        return

    fees = parse_trade_fees(raw_trades)
    broker_stats = aggregate_by_broker(fees)
    monthly = aggregate_by_month(fees)
    cheapest = cheapest_broker_by_market(fees)
    generate_html(broker_stats, monthly, cheapest, args.out)

    print(f"\n{'═' * 55}")
    print(f"  BROKER FEES — {sum(s.trade_count for s in broker_stats.values())} trades analysed")
    print(f"{'═' * 55}")
    for b in _KNOWN_BROKERS:
        s = broker_stats.get(b, BrokerFeeStats(broker=b))
        if s.trade_count > 0:
            print(f"  {b:12s}  {s.trade_count:4d} trades  ${s.total_fees:>8,.2f}  ({s.fee_pct:.3f}%)")
    print()


if __name__ == "__main__":
    main()
