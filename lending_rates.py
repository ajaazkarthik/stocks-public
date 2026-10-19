#!/usr/bin/env python3
"""
Lending & Margin Interest Rate Tracker
=======================================
Reference dashboard for margin lending rates across all brokers and markets.
Helps you decide:
  • Which broker/market is cheapest for leverage
  • How long you can hold a leveraged position before interest erodes returns
  • Break-even daily/weekly/monthly return required to cover interest cost

Brokers covered:
  • Moomoo SG  — JP (TSE), SG (SGX), US (NYSE/NASDAQ), HK (HKEX), AU (ASX)
  • Moomoo AU  — AU (ASX), US, HK
  • IBKR       — USD, AUD, SGD, JPY (tiered by balance)
  • Zerodha    — INR margin lending
  • Angel One  — INR margin lending

Usage:
    python lending_rates.py [--out PATH]
    python lending_rates.py --out reports/lending_rates.html

NOTE: Rates change frequently. Verify current rates with your broker before trading.
The rates in this file are approximate reference values (as of Q1 2026).
To update: edit the LENDING_RATES dict below.
"""

import sys
import argparse
from datetime import datetime
from pathlib import Path

# ── Windows UTF-8 ──────────────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.resolve()

# ─── Rate data ────────────────────────────────────────────────────────────────
# Format: annual interest rate as decimal (0.06 = 6% p.a.)
# Source: Moomoo/Futu official rate pages, IBKR rate page, broker websites.
# Last updated: Q1 2026. Verify before use.

LENDING_RATES = {
    "Moomoo-SG": {
        "description": "Futu Singapore (FUTUSG) — margin lending rates",
        "account_note": "Requires margin account. Rates are tiered by position size.",
        "source_url":   "https://www.moomoo.com/sg/features/margin",
        "currency":     "SGD account (multi-currency positions)",
        "markets": {
            "JP (TSE)": {
                "currency": "JPY",
                "rate_pct": 2.5,
                "tier_note": "~2.5% p.a. — one of the lowest globally due to BoJ near-zero rates",
                "min_rate":  2.0,
                "max_rate":  3.0,
            },
            "SG (SGX)": {
                "currency": "SGD",
                "rate_pct": 4.0,
                "tier_note": "~4.0% p.a. — SGD rates, eligible stocks only",
                "min_rate":  3.5,
                "max_rate":  4.5,
            },
            "US (NYSE/NASDAQ)": {
                "currency": "USD",
                "rate_pct": 6.0,
                "tier_note": "~6.0% p.a. — standard USD margin rate",
                "min_rate":  5.5,
                "max_rate":  6.8,
            },
            "HK (HKEX)": {
                "currency": "HKD",
                "rate_pct": 4.5,
                "tier_note": "~4.5% p.a. — HKD rates; warrants/CBBCs may differ",
                "min_rate":  4.0,
                "max_rate":  5.5,
            },
            "AU (ASX)": {
                "currency": "AUD",
                "rate_pct": 5.5,
                "tier_note": "~5.5% p.a. — AUD margin (if available via SG account)",
                "min_rate":  5.0,
                "max_rate":  6.0,
            },
        },
    },
    "Moomoo-AU": {
        "description": "Futu Australia (FUTUAU) — margin lending rates",
        "account_note": "Requires margin account with Futu AU. ASX and US markets.",
        "source_url":   "https://www.moomoo.com/au/features/margin",
        "currency":     "AUD account",
        "markets": {
            "AU (ASX)": {
                "currency": "AUD",
                "rate_pct": 5.5,
                "tier_note": "~5.5% p.a. — AUD margin for ASX stocks",
                "min_rate":  5.0,
                "max_rate":  6.0,
            },
            "US (NYSE/NASDAQ)": {
                "currency": "USD",
                "rate_pct": 6.5,
                "tier_note": "~6.5% p.a. — USD margin via AU account",
                "min_rate":  6.0,
                "max_rate":  7.2,
            },
            "HK (HKEX)": {
                "currency": "HKD",
                "rate_pct": 5.0,
                "tier_note": "~5.0% p.a. — HKD margin via AU account",
                "min_rate":  4.5,
                "max_rate":  5.5,
            },
        },
    },
    "IBKR": {
        "description": "Interactive Brokers — tiered benchmark-based rates",
        "account_note": "Rates are BM + spread (BM = SOFR/SONIA/RBA/etc). Tiered by balance. "
                        "Pro accounts get better rates. Sub-$100k typically BM + 1.5%.",
        "source_url":   "https://www.interactivebrokers.com/en/trading/margin-rates.php",
        "currency":     "Multi-currency",
        "markets": {
            "USD (NYSE/NASDAQ)": {
                "currency": "USD",
                "rate_pct": 6.0,
                "tier_note": "~SOFR + 1.5% ≈ 6.8% (< $100k). Lower for larger balances.",
                "min_rate":  4.5,   # large balance
                "max_rate":  7.5,   # small balance
            },
            "AUD (ASX)": {
                "currency": "AUD",
                "rate_pct": 5.5,
                "tier_note": "~RBA cash rate + spread ≈ 5.5% p.a.",
                "min_rate":  4.5,
                "max_rate":  6.5,
            },
            "SGD (SGX)": {
                "currency": "SGD",
                "rate_pct": 4.5,
                "tier_note": "~SORA + spread ≈ 4.5% p.a.",
                "min_rate":  3.5,
                "max_rate":  5.5,
            },
            "JPY (TSE)": {
                "currency": "JPY",
                "rate_pct": 1.5,
                "tier_note": "~BoJ rate + spread ≈ 1.5% p.a. — very cheap leverage",
                "min_rate":  1.0,
                "max_rate":  2.5,
            },
            "HKD (HKEX)": {
                "currency": "HKD",
                "rate_pct": 5.0,
                "tier_note": "~HIBOR + spread ≈ 5.0% p.a.",
                "min_rate":  4.0,
                "max_rate":  6.0,
            },
        },
    },
    "Zerodha": {
        "description": "Zerodha (India) — NBFC margin trading facility (MTF)",
        "account_note": "Margin Trading Facility (MTF) via Zerodha-NBFC. "
                        "Daily interest on borrowed amount. Only approved scrips.",
        "source_url":   "https://zerodha.com/charges/",
        "currency":     "INR",
        "markets": {
            "NSE/BSE (Equity MTF)": {
                "currency": "INR",
                "rate_pct": 18.0,
                "tier_note": "~18% p.a. MTF rate for approved NSE/BSE scrips. "
                             "Charged daily on debit balance.",
                "min_rate":  18.0,
                "max_rate":  22.0,
            },
        },
    },
    "AngelOne": {
        "description": "AngelOne (India) — Margin Trading Facility",
        "account_note": "MTF rates. Daily debit balance interest. "
                        "Only approved scrips eligible for MTF.",
        "source_url":   "https://www.angelone.in/charges",
        "currency":     "INR",
        "markets": {
            "NSE/BSE (Equity MTF)": {
                "currency": "INR",
                "rate_pct": 18.0,
                "tier_note": "~18% p.a. MTF rate. Similar to Zerodha.",
                "min_rate":  18.0,
                "max_rate":  21.6,
            },
        },
    },
}

# ─── Break-even calculator ────────────────────────────────────────────────────

def break_even(annual_rate_pct: float, leverage: float = 1.0) -> dict:
    """
    Compute break-even returns needed to cover interest costs at different timeframes.
    leverage: margin ratio (e.g. 2.0 means 2× — you borrow 50% of position value).
    """
    r = annual_rate_pct / 100.0
    borrowed_pct = 1.0 - (1.0 / leverage)  # fraction of position that is borrowed
    daily   = r / 365 * borrowed_pct * 100
    weekly  = r / 52  * borrowed_pct * 100
    monthly = r / 12  * borrowed_pct * 100
    annual  = r       * borrowed_pct * 100
    return {
        "daily":   round(daily, 4),
        "weekly":  round(weekly, 3),
        "monthly": round(monthly, 3),
        "annual":  round(annual, 2),
    }


def monthly_cost(position_size: float, annual_rate_pct: float, leverage: float = 2.0) -> float:
    """Monthly interest cost in currency units for a given position size and leverage."""
    borrowed = position_size * (1 - 1 / leverage)
    return round(borrowed * annual_rate_pct / 100 / 12, 2)


# ─── HTML ─────────────────────────────────────────────────────────────────────

CSS = """\
body { font-family:'Segoe UI',Arial,sans-serif; background:#0f1117; color:#e0e0e0; margin:0; padding:20px; }
h1   { color:#00d4ff; letter-spacing:1px; }
h2   { color:#7ecbf5; border-bottom:1px solid #333; padding-bottom:6px; }
h3   { color:#aad4f5; }
p.note { background:#1e2330; border-left:3px solid #f5a623; padding:10px 14px; font-size:0.88em; color:#ccc; }
p.warn { background:#2a1f10; border-left:3px solid #e07000; padding:8px 14px; font-size:0.88em; color:#e0c080; }
table { border-collapse:collapse; width:100%; margin-bottom:28px; font-size:0.87em; }
th { background:#1a2540; color:#7ecbf5; padding:8px 10px; text-align:left; border-bottom:2px solid #2c3a55; }
td { padding:7px 10px; border-bottom:1px solid #22293a; }
tr:hover td { background:#1a2236; }
.pos  { color:#4caf50; }
.neg  { color:#f44336; }
.rate-low    { color:#06d6a0; font-weight:bold; }
.rate-mid    { color:#f5a623; font-weight:bold; }
.rate-high   { color:#f44336; font-weight:bold; }
.badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:0.78em; font-weight:bold; }
.broker-card {
  background:#12151f; border-radius:10px; padding:18px 22px;
  margin-bottom:28px; border:1px solid #1c2540;
}
.calc-box {
  background:#121a1a; border-radius:8px; padding:16px 20px;
  margin-bottom:24px; border:1px solid #1a3030;
}
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
@media(max-width:800px) { .grid-2 { grid-template-columns:1fr; } }
.rate-bar-container { width:100%; background:#1c2030; border-radius:4px; overflow:hidden; height:14px; margin-top:4px; }
.rate-bar { height:14px; border-radius:4px; transition:width 0.3s; }
"""

BROKER_COLOURS = {
    "Moomoo-SG": "#f5a623",
    "Moomoo-AU": "#f59b23",
    "IBKR":      "#00d4ff",
    "Zerodha":   "#b07fff",
    "AngelOne": "#ff7070",
}


def _rate_class(rate: float) -> str:
    if rate <= 3.0:   return "rate-low"
    elif rate <= 6.0: return "rate-mid"
    else:             return "rate-high"


def _rate_bar(rate: float, max_rate: float = 25.0, colour: str = "#f5a623") -> str:
    pct = min(100, rate / max_rate * 100)
    return (
        f'<div class="rate-bar-container">'
        f'<div class="rate-bar" style="width:{pct:.1f}%;background:{colour}"></div>'
        f'</div>'
    )


def _broker_section(broker: str, data: dict) -> str:
    colour = BROKER_COLOURS.get(broker, "#aaa")
    badge  = f'<span class="badge" style="background:{colour}22;color:{colour};border:1px solid {colour}55">{broker}</span>'

    rows = ""
    for market, mdata in data["markets"].items():
        rate = mdata["rate_pct"]
        rc   = _rate_class(rate)
        be   = break_even(rate, leverage=2.0)
        rows += (
            f"<tr>"
            f"<td>{market}</td>"
            f"<td>{mdata['currency']}</td>"
            f'<td><span class="{rc}">{rate:.1f}%</span>'
            f'{_rate_bar(rate, colour=colour)}</td>'
            f"<td>{mdata['min_rate']:.1f}%–{mdata['max_rate']:.1f}%</td>"
            f"<td>{mdata['tier_note']}</td>"
            f"<td>{be['daily']:.4f}%/day</td>"
            f"<td>{be['weekly']:.3f}%/wk</td>"
            f"<td>{be['monthly']:.3f}%/mo</td>"
            f"</tr>"
        )

    return (
        f'<div class="broker-card">'
        f"<h3>{badge} &nbsp; {data['description']}</h3>"
        f"<p style='color:#aaa;font-size:0.88em'>{data['account_note']}</p>"
        f"<table><thead><tr>"
        "<th>Market</th><th>Currency</th><th>Rate p.a.</th><th>Range</th><th>Notes</th>"
        "<th>Break-even/day<br>(2× leverage)</th>"
        "<th>Break-even/wk<br>(2× leverage)</th>"
        "<th>Break-even/mo<br>(2× leverage)</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f"<p style='font-size:0.82em;color:#aaa'>Source: {data['source_url']}</p>"
        f"</div>"
    )


def _comparison_table() -> str:
    """Flat comparison table: one row per broker×market, sorted by rate ascending."""
    rows_data = []
    for broker, bdata in LENDING_RATES.items():
        colour = BROKER_COLOURS.get(broker, "#aaa")
        for market, mdata in bdata["markets"].items():
            be = break_even(mdata["rate_pct"], leverage=2.0)
            rows_data.append({
                "broker":   broker,
                "colour":   colour,
                "market":   market,
                "currency": mdata["currency"],
                "rate":     mdata["rate_pct"],
                "tier":     mdata["tier_note"],
                "daily_be": be["daily"],
                "monthly_be": be["monthly"],
                "monthly_cost_10k": monthly_cost(10_000, mdata["rate_pct"], leverage=2.0),
            })
    rows_data.sort(key=lambda x: x["rate"])

    rows_html = ""
    for r in rows_data:
        rc = _rate_class(r["rate"])
        badge = (
            f'<span class="badge" style="background:{r["colour"]}22;'
            f'color:{r["colour"]};border:1px solid {r["colour"]}55">{r["broker"]}</span>'
        )
        rows_html += (
            f"<tr>"
            f"<td>{badge}</td>"
            f"<td>{r['market']}</td>"
            f"<td>{r['currency']}</td>"
            f'<td><span class="{rc}">{r["rate"]:.1f}%</span></td>'
            f"<td>{r['daily_be']:.4f}%/day</td>"
            f"<td>{r['monthly_be']:.3f}%/mo</td>"
            f"<td>{r['monthly_cost_10k']:,.2f} {r['currency']}/mo</td>"
            f"<td style='color:#aaa;font-size:0.83em'>{r['tier'][:60]}...</td>"
            f"</tr>"
        )

    return (
        "<table><thead><tr>"
        "<th>Broker</th><th>Market</th><th>Currency</th><th>Rate p.a.</th>"
        "<th>Break-even/day (2×)</th><th>Break-even/mo (2×)</th>"
        "<th>Monthly cost on 10k (2×)</th><th>Notes</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
    )


def _leverage_guide() -> str:
    """Quick reference: what different leverage levels mean."""
    rows = ""
    for lev in [1.5, 2.0, 3.0, 5.0, 10.0]:
        borrow_pct = (1 - 1 / lev) * 100
        for rate in [2.5, 4.0, 6.0, 18.0]:
            be = break_even(rate, lev)
            # only show 2× row for cleanliness
        be_25  = break_even(2.5,  lev)
        be_40  = break_even(4.0,  lev)
        be_60  = break_even(6.0,  lev)
        be_180 = break_even(18.0, lev)
        rows += (
            f"<tr>"
            f"<td>{lev:.1f}×</td>"
            f"<td>{borrow_pct:.0f}%</td>"
            f'<td><span class="rate-low">{be_25["monthly"]:.3f}%/mo</span></td>'
            f'<td><span class="rate-mid">{be_40["monthly"]:.3f}%/mo</span></td>'
            f'<td><span class="rate-mid">{be_60["monthly"]:.3f}%/mo</span></td>'
            f'<td><span class="rate-high">{be_180["monthly"]:.3f}%/mo</span></td>'
            f"</tr>"
        )

    return (
        "<p>Monthly return your position must earn just to cover interest costs "
        "(break-even). This is the <em>minimum</em> — you need to earn more than this to profit.</p>"
        "<table><thead><tr>"
        "<th>Leverage</th><th>% Borrowed</th>"
        "<th>2.5% rate (JP)</th><th>4.0% rate (SG)</th>"
        "<th>6.0% rate (US)</th><th>18% rate (India)</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _position_calc_section() -> str:
    """Monthly cost table: $10k, $25k, $50k positions at common rates."""
    position_sizes = [10_000, 25_000, 50_000, 100_000]
    rate_examples  = [2.5, 4.0, 6.0, 18.0]
    leverage       = 2.0

    rows = ""
    for size in position_sizes:
        cells = f"<td>{size:,}</td>"
        for rate in rate_examples:
            cost = monthly_cost(size, rate, leverage)
            # cost in currency units
            rc = _rate_class(rate)
            cells += f'<td><span class="{rc}">{cost:,.2f}</span></td>'
        rows += f"<tr>{cells}</tr>"

    return (
        "<p>Monthly interest cost (currency units) for a 2× leveraged position. "
        "Half the position is your capital; half is borrowed.</p>"
        "<table><thead><tr>"
        "<th>Position Size</th>"
        "<th>2.5% p.a. (JP via Moomoo SG)</th>"
        "<th>4.0% p.a. (SG/SGX)</th>"
        "<th>6.0% p.a. (US via Moomoo)</th>"
        "<th>18% p.a. (India MTF)</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<p class='note'><strong>Example:</strong> A $50,000 position with 2× leverage "
        "at 6% p.a. costs you $50,000×50%×6%/12 = <strong>$125/month</strong> in interest. "
        "The position must rise by more than $125/month to be profitable.</p>"
    )


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lending &amp; Margin Rate Tracker</title>
<style>{css}</style>
</head>
<body>
<h1>💰 Lending &amp; Margin Interest Rate Tracker</h1>
<p>Generated: {generated}</p>

<p class="warn">
<strong>⚠ Rates are approximate reference values (Q1 2026).</strong>
Actual rates change based on broker policy, central bank rates, position size, and account type.
Always verify current rates with your broker before using leverage.
</p>

<h2>📊 Quick Comparison — All Brokers × Markets (sorted by rate)</h2>
{comparison_table}

<h2>🧮 Leverage Break-Even Guide</h2>
<p>How much must your position return just to cover borrowing costs?</p>
{leverage_guide}

<h2>💵 Monthly Interest Cost Calculator (2× Leverage)</h2>
{position_calc}

<h2>🏦 Broker Details</h2>
{broker_sections}

<h2>📝 Key Takeaways</h2>
<ul style="line-height:2">
  <li>🏆 <strong>Cheapest leverage: Japan (TSE)</strong> via Moomoo SG or IBKR — ~2.5% p.a. (JPY).
      BoJ near-zero policy makes JPY borrowing very cheap.
      Use for Nikkei ETFs (JP.1579, JP.201A) or JPY-denominated positions.</li>
  <li>💡 <strong>Mid-range: Singapore (SGX)</strong> — ~4% p.a. (SGD).
      Good for SGX DLCs, REITs, and blue-chips.</li>
  <li>⚡ <strong>Standard: US Markets</strong> — ~6% p.a. (USD).
      Most liquid market but highest borrowing cost among global markets.</li>
  <li>🚨 <strong>Expensive: India MTF</strong> — ~18% p.a. (INR).
      Only viable for very short holding periods or high-conviction trades.
      Daily compounding makes this painful for position holds > 2 weeks.</li>
  <li>📉 <strong>Interest eats returns:</strong> At 6% p.a. with 2× leverage,
      a position must gain ~0.5% per month just to break even on interest costs.
      Leverage amplifies BOTH gains and losses.</li>
  <li>🇦🇺 <strong>Tax note:</strong> Margin interest is deductible against investment income
      in Australia (Section 8-1 ITAA 1997). Keep records of all interest paid.</li>
</ul>

</body>
</html>"""


def generate_html(args) -> str:
    broker_sections_html = "\n".join(
        _broker_section(broker, data) for broker, data in LENDING_RATES.items()
    )
    return HTML_TEMPLATE.format(
        css=CSS,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        comparison_table=_comparison_table(),
        leverage_guide=_leverage_guide(),
        position_calc=_position_calc_section(),
        broker_sections=broker_sections_html,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Lending & Margin Interest Rate Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    default_out = str(ROOT / "reports" / "lending_rates.html")
    parser.add_argument("--out", default=default_out, help="Output HTML path")
    args = parser.parse_args()

    print("=" * 60)
    print("Lending & Margin Interest Rate Tracker")
    print("=" * 60)

    html = generate_html(args)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nReport saved: {out_path}")
    print(f"  Brokers covered: {len(LENDING_RATES)}")
    all_markets = sum(len(d["markets"]) for d in LENDING_RATES.values())
    print(f"  Market/broker combinations: {all_markets}")
    rates = [m["rate_pct"] for d in LENDING_RATES.values() for m in d["markets"].values()]
    print(f"  Rate range: {min(rates):.1f}% – {max(rates):.1f}% p.a.")

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "reports"))
        from report_db import save_lending_rates_snapshot, archive_report as _arc
        save_lending_rates_snapshot(LENDING_RATES)
        _arc(out_path)
    except Exception as _e:
        print(f"  [report_db] lending_rates snapshot skipped: {_e}")


if __name__ == "__main__":
    main()
