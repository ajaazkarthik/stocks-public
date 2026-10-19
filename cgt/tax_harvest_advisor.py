#!/usr/bin/env python3
"""
Tax Burden Dashboard & Harvest Advisor
=======================================
Shows your estimated tax liability per country (Australia, India, Singapore)
and identifies tax-loss harvesting opportunities across all brokers.

Countries covered:
  • Australia  — CGT (FY: 1 Jul – 30 Jun). 50% discount for LT gains.
                 Marginal rate configurable (default: 47% incl. 2% Medicare).
  • India      — Delivery STCG @ 20%, LTCG @ 12.5% above ₹1.25L exemption;
                 Intraday/F&O at income-tax slab rate (default 30%).
  • Singapore  — Zero capital gains tax on securities for investors.
                 Note: AU residents still owe Australian CGT on all gains.

Harvest Analysis:
  • Identifies open positions with unrealized losses.
  • Computes potential tax saving if you sell them before FY end.
  • Ranks harvest candidates by tax saving value.

Usage:
    python tax_harvest_advisor.py [options]
    python tax_harvest_advisor.py --no-ibkr --out /tmp/harvest.html
    python tax_harvest_advisor.py --marginal-rate-au 0.39 --india-slab 0.30

Options:
    --no-ibkr            Skip IBKR data
    --no-moomoo          Skip Moomoo AU/SG data
    --no-india           Skip India (Zerodha + Angel One) data
    --marginal-rate-au   AU marginal tax rate as decimal (default: 0.47)
    --india-slab         India income-tax slab for intraday/F&O (default: 0.30)
    --out                Output HTML path (default: reports/cgt/tax_harvest_report.html)
"""

import os
import sys
import argparse
import importlib.util
from datetime import datetime, date
from collections import defaultdict
from pathlib import Path

import pandas as pd

# ── Windows UTF-8 ──────────────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE  = Path(__file__).parent.resolve()
ROOT  = HERE.parent

# ─── Module loader ────────────────────────────────────────────────────────────

def _load_module(name: str, rel_path: str):
    path = str(HERE / rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── Fast Moomoo positions fetch (OpenD — seconds, not hours) ─────────────────

def _check_opend(port: int) -> bool:
    """Return True if Moomoo OpenD is reachable on the given port."""
    try:
        sys.path.insert(0, str(ROOT))
        from moomoo_config import is_opend_running
        return is_opend_running(port)
    except Exception:
        return False


def _fetch_moomoo_open_positions(port: int, broker_label: str) -> list[dict]:
    """
    Fetch current open positions from Moomoo OpenD.
    Uses position_list_query() — completes in seconds, does NOT load trade history.
    """
    try:
        import moomoo as ft
        trd_ctx = ft.OpenSecTradeContext(
            filter_trdmarket=ft.TrdMarket.ALL,
            host='127.0.0.1',
            port=port,
        )
        ret, data = trd_ctx.position_list_query()
        trd_ctx.close()
        if ret != ft.RET_OK:
            print(f"  [{broker_label}] position_list_query failed: {data}")
            return []

        positions = []
        for _, row in data.iterrows():
            code     = str(row.get('code', ''))
            qty      = float(row.get('qty', 0) or 0)
            avg_cost = float(row.get('cost_price', 0) or 0)
            cur_price = float(row.get('current_price', 0) or 0)
            market_val = float(row.get('market_val', 0) or 0)
            pl_val = float(row.get('pl_val', 0) or 0)  # unrealised P&L

            # Infer currency from code prefix / suffix
            if code.startswith('HK.') or '.HK' in code:
                currency = 'HKD'
            elif code.startswith('SG.') or '.SI' in code:
                currency = 'SGD'
            elif code.startswith('AU.') or '.AX' in code:
                currency = 'AUD'
            elif code.startswith('JP.') or '.T' in code:
                currency = 'JPY'
            else:
                currency = 'USD'

            positions.append({
                'code':             code,
                'broker':           broker_label,
                'currency':         currency,
                'qty':              qty,
                'avg_cost':         avg_cost,
                'last_price':       cur_price,
                'Total Unrealized': pl_val,
                'hold_days':        0,   # OpenD doesn't expose hold days directly
                'term':             'Short',
            })
        print(f"  [{broker_label}] {len(positions)} open positions fetched (fast path).")
        return positions
    except Exception as e:
        print(f"  [{broker_label}] Positions fetch failed: {e}")
        return []


# ─── FY helpers ───────────────────────────────────────────────────────────────

def au_fy(d: date) -> str:
    """Return 'FY2026' for Australian financial year."""
    year = d.year if d.month >= 7 else d.year - 1
    return f"FY{year + 1}"


def au_fy_end(fy_label: str) -> date:
    """Return June 30 end date for an AU FY label."""
    year = int(fy_label[2:])
    return date(year, 6, 30)


def india_fy(d: date) -> str:
    """Return 'IND FY2026-27' for Indian financial year (Apr-Mar)."""
    year = d.year if d.month >= 4 else d.year - 1
    return f"IND FY{year}-{str(year + 1)[2:]}"


def current_au_fy() -> str:
    return au_fy(date.today())


def current_india_fy() -> str:
    return india_fy(date.today())


# ─── Tax rules ────────────────────────────────────────────────────────────────

def calc_au_tax(net_taxable: float, marginal_rate: float) -> float:
    """Australian CGT: net_taxable already has 50% discount applied. Tax = net × rate."""
    return max(0.0, net_taxable) * marginal_rate


def calc_india_tax(st_gains: float, lt_gains: float, intraday: float,
                   slab_rate: float) -> dict:
    """
    India tax calculation.
    - STCG (equity delivery < 1yr): 20%
    - LTCG (equity delivery >= 1yr): 12.5% above ₹1.25L p.a. exemption
    - Intraday/F&O: income-tax slab rate
    """
    INDIA_STCG_RATE   = 0.20
    INDIA_LTCG_RATE   = 0.125
    INDIA_LTCG_EXEMPT = 125_000.0   # ₹1.25 lakh p.a.

    st_tax    = max(0.0, st_gains) * INDIA_STCG_RATE
    lt_taxable = max(0.0, lt_gains - INDIA_LTCG_EXEMPT)
    lt_tax    = lt_taxable * INDIA_LTCG_RATE
    intra_tax = max(0.0, intraday) * slab_rate
    return {
        "st_tax":     round(st_tax, 2),
        "lt_tax":     round(lt_tax, 2),
        "intra_tax":  round(intra_tax, 2),
        "total_tax":  round(st_tax + lt_tax + intra_tax, 2),
    }


# ─── Harvest opportunity builder ──────────────────────────────────────────────

def build_harvest_opportunities(
    open_positions: list[dict],
    au_net_taxable_usd: float,
    au_net_taxable_aud: float,
    india_st_gains: float,
    india_lt_gains: float,
    marginal_rate_au: float,
    india_stcg_rate: float = 0.20,
) -> list[dict]:
    """
    For each open position with unrealized loss, compute:
    - Harvest saving: loss × applicable_tax_rate
    - Applicable rate is determined by currency:
        AUD/USD/HKD/SGD/JPY → Australian CGT (marginal rate applied to loss)
        INR                  → India STCG 20% (most are short-term)
    - Days to AU FY end (to prioritise urgency)
    """
    today    = date.today()
    fy_end   = au_fy_end(current_au_fy())
    days_left = (fy_end - today).days

    rows = []
    for pos in open_positions:
        total_upl = float(pos.get("Total Unrealized", 0) or 0)
        if total_upl >= 0:
            continue  # only losses

        code       = pos.get("code", "?")
        broker     = pos.get("broker", "?")
        currency   = pos.get("currency", "USD")
        qty        = float(pos.get("qty", 0) or 0)
        avg_cost   = float(pos.get("avg_cost", 0) or 0)
        last_price = float(pos.get("last_price", 0) or 0)
        hold_days  = pos.get("hold_days", 0)
        term       = pos.get("term", "Short")

        loss_value = abs(total_upl)

        if currency == "INR":
            # India: STCG rate (most positions < 1yr; if LT, use LTCG rate)
            rate = 0.125 if (isinstance(hold_days, int) and hold_days >= 365) else india_stcg_rate
            tax_saving = loss_value * rate
            jurisdiction = "India"
        else:
            # Australia CGT: loss offsets net taxable gains
            # For LT positions (>= 365 days), the loss reduces LT gains which are at half rate
            if isinstance(hold_days, int) and hold_days >= 365:
                effective_rate = marginal_rate_au * 0.5   # LT loss saves at half rate
            else:
                effective_rate = marginal_rate_au
            tax_saving = loss_value * effective_rate
            jurisdiction = "Australia"

        rows.append({
            "Broker":         broker,
            "Symbol":         code,
            "Currency":       currency,
            "Qty":            qty,
            "Avg Cost":       round(avg_cost, 4),
            "Last Price":     round(last_price, 4),
            "Unrealised Loss": round(-loss_value, 2),  # negative
            "Term":           term,
            "Hold Days":      hold_days if isinstance(hold_days, int) else "—",
            "Tax Saving":     round(tax_saving, 2),
            "Jurisdiction":   jurisdiction,
        })

    rows.sort(key=lambda r: r["Tax Saving"], reverse=True)
    return rows, days_left


# ─── HTML helpers ─────────────────────────────────────────────────────────────

CSS = """\
body { font-family:'Segoe UI',Arial,sans-serif; background:#0f1117; color:#e0e0e0; margin:0; padding:20px; }
h1   { color:#00d4ff; letter-spacing:1px; }
h2   { color:#7ecbf5; border-bottom:1px solid #333; padding-bottom:6px; }
h3   { color:#aad4f5; }
p.note { background:#1e2330; border-left:3px solid #f5a623; padding:10px 14px; font-size:0.88em; color:#ccc; }
p.warn { background:#2a1f10; border-left:3px solid #e07000; padding:8px 14px; font-size:0.88em; color:#e0c080; }
table { border-collapse:collapse; width:100%; margin-bottom:24px; font-size:0.86em; }
th { background:#1a2540; color:#7ecbf5; padding:8px 10px; text-align:left; border-bottom:2px solid #2c3a55; }
td { padding:7px 10px; border-bottom:1px solid #22293a; }
tr:hover td { background:#1a2236; }
.pos  { color:#4caf50; }
.neg  { color:#f44336; }
.disc { color:#f5a623; }
.harvest-card {
  background:#121a1a; border:1px solid #1a3a2a; border-radius:10px;
  padding:20px 24px; margin-bottom:24px;
}
.tax-card {
  background:#12121a; border-radius:10px; padding:20px 24px;
  margin-bottom:20px; display:inline-block; min-width:240px;
  border:1px solid #1e1e40; vertical-align:top; margin-right:16px;
}
.tax-amount { font-size:1.8rem; font-weight:bold; }
.tax-au     { color:#00d4ff; }
.tax-in     { color:#b07fff; }
.tax-sg     { color:#06d6a0; }
.badge {
  display:inline-block; padding:3px 10px; border-radius:12px;
  font-size:0.78em; font-weight:bold; margin-right:4px;
}
.badge-au  { background:#00d4ff22; color:#00d4ff; border:1px solid #00d4ff55; }
.badge-in  { background:#b07fff22; color:#b07fff; border:1px solid #b07fff55; }
.badge-sg  { background:#06d6a022; color:#06d6a0; border:1px solid #06d6a055; }
.badge-ibkr    { background:#00d4ff22; color:#00d4ff; border:1px solid #00d4ff55; }
.badge-moomoo  { background:#f5a62322; color:#f5a623; border:1px solid #f5a62355; }
.badge-india   { background:#b07fff22; color:#b07fff; border:1px solid #b07fff55; }
.urgency-high   { background:#3a1010; border-left:4px solid #f44336; padding:10px 14px; border-radius:6px; margin-bottom:12px; }
.urgency-medium { background:#2a2010; border-left:4px solid #f5a623; padding:10px 14px; border-radius:6px; margin-bottom:12px; }
.urgency-low    { background:#102010; border-left:4px solid #4caf50; padding:10px 14px; border-radius:6px; margin-bottom:12px; }
.summary-grid { display:flex; flex-wrap:wrap; gap:16px; margin-bottom:28px; }
"""

def _fmt(val, decimals=2, prefix=""):
    val = round(float(val), decimals)
    cls = "pos" if val > 0 else ("neg" if val < 0 else "")
    sign = "+" if val > 0 else ""
    return f'<span class="{cls}">{sign}{prefix}{val:,.{decimals}f}</span>'


def _harvest_table_html(rows: list[dict]) -> str:
    if not rows:
        return "<p><em>No harvest opportunities found (no positions with unrealised losses).</em></p>"

    headers = [
        "Broker", "Symbol", "Currency", "Qty", "Avg Cost", "Last Price",
        "Unrealised Loss", "Term", "Hold Days", "Tax Saving", "Jurisdiction"
    ]
    numeric = {"Unrealised Loss", "Tax Saving"}
    thead = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    tbody = ""
    for r in rows:
        cells = []
        for h in headers:
            v = r.get(h, "")
            if h in numeric:
                cells.append(f"<td>{_fmt(v)}</td>")
            elif h == "Broker":
                key = v.lower()
                badge = f'<span class="badge badge-{key}">{v}</span>'
                cells.append(f"<td>{badge}</td>")
            elif h == "Jurisdiction":
                badge_cls = "badge-au" if v == "Australia" else "badge-in"
                cells.append(f'<td><span class="badge {badge_cls}">{v}</span></td>')
            else:
                cells.append(f"<td>{v}</td>")
        tbody += "<tr>" + "".join(cells) + "</tr>\n"

    total_saving = sum(r["Tax Saving"] for r in rows)
    total_loss   = sum(r["Unrealised Loss"] for r in rows)
    tbody += (
        f'<tr style="background:#1a2030;font-weight:bold">'
        f'<td colspan="6">TOTAL (sell all loss positions)</td>'
        f'<td>{_fmt(total_loss)}</td>'
        f'<td colspan="2"></td>'
        f'<td>{_fmt(total_saving)}</td>'
        f'<td></td>'
        f'</tr>'
    )

    return f"<table><thead>{thead}</thead><tbody>{tbody}</tbody></table>"


def _au_tax_section(fy_label: str, currency_rows: list[dict], marginal_rate: float) -> str:
    if not currency_rows:
        return "<p><em>No realized AU-CGT data available for this FY.</em></p>"

    rows_html = ""
    total_net_taxable = 0.0
    total_tax = 0.0
    for r in currency_rows:
        cur = r.get("Currency", "?")
        net_taxable = float(r.get("Net Taxable", 0) or 0)
        st_gains    = float(r.get("ST Gains", 0) or 0)
        lt_gains    = float(r.get("LT Gross Gains", 0) or 0)
        lt_losses   = float(r.get("LT Losses", 0) or 0)
        st_losses   = float(r.get("ST Losses", 0) or 0)
        discount    = float(r.get("CGT Discount", 0) or 0)
        est_tax     = calc_au_tax(net_taxable, marginal_rate)
        total_net_taxable += net_taxable
        total_tax += est_tax
        net_cls = "pos" if net_taxable >= 0 else "neg"
        tax_cls = "neg" if est_tax > 0 else "pos"
        rows_html += (
            f"<tr>"
            f"<td>{cur}</td>"
            f"<td>{_fmt(st_gains)}</td>"
            f"<td>{_fmt(st_losses)}</td>"
            f"<td>{_fmt(lt_gains)}</td>"
            f"<td>{_fmt(lt_losses)}</td>"
            f'<td><span class="disc">{discount:,.2f}</span></td>'
            f'<td class="{net_cls}">{net_taxable:,.2f}</td>'
            f'<td class="{tax_cls}">≈ {est_tax:,.2f}</td>'
            f"</tr>\n"
        )

    rate_pct = marginal_rate * 100
    return (
        f"<p>Marginal rate applied: <strong>{rate_pct:.0f}%</strong> &nbsp;|&nbsp; "
        f"Net taxable total: <strong>{_fmt(total_net_taxable)}</strong> &nbsp;|&nbsp; "
        f"Estimated tax: <strong class='neg'>{total_tax:,.2f}</strong></p>"
        "<table><thead><tr>"
        "<th>Currency</th><th>ST Gains</th><th>ST Losses</th>"
        "<th>LT Gross Gains</th><th>LT Losses</th><th>50% Discount</th>"
        "<th>Net Taxable</th><th>Est. Tax</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        "<p class='note'>Foreign currency amounts (USD, HKD, SGD, JPY) must be converted "
        "to AUD using RBA published rates on each trade date for exact AUD liability. "
        "The estimated tax above is in the trade's native currency as an approximation.</p>"
    )


def _india_tax_section(india_fy_rows: list[dict], slab_rate: float) -> str:
    if not india_fy_rows:
        return "<p><em>No India CGT data available.</em></p>"

    rows_html = ""
    total_tax = 0.0
    cur_fy = current_india_fy()
    for r in india_fy_rows:
        fy     = r.get("fy", "")
        broker = r.get("broker", "")
        st     = float(r.get("st_inr", 0) or 0)
        lt     = float(r.get("lt_inr", 0) or 0)
        intra  = float(r.get("intraday_inr", 0) or 0)
        tax_d  = calc_india_tax(st, lt, intra, slab_rate)
        t      = tax_d["total_tax"]
        total_tax += t
        is_cur = "★" if fy == cur_fy else ""
        style  = ' style="background:#1a1a2e"' if fy == cur_fy else ""
        rows_html += (
            f"<tr{style}>"
            f"<td>{fy} {is_cur}</td>"
            f"<td>{broker}</td>"
            f"<td>{_fmt(st, prefix='₹')}</td>"
            f"<td>{_fmt(lt, prefix='₹')}</td>"
            f"<td>{_fmt(intra, prefix='₹')}</td>"
            f'<td class="neg">₹{t:,.2f}</td>'
            f"</tr>\n"
        )

    return (
        "<table><thead><tr>"
        "<th>FY</th><th>Broker</th>"
        "<th>Delivery ST (₹)</th><th>Delivery LT (₹)</th>"
        "<th>Intraday/F&O (₹)</th><th>Est. Tax (₹)</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        f"<p class='note'>STCG @ 20% · LTCG @ 12.5% above ₹1.25L · Intraday/F&O @ {slab_rate*100:.0f}% slab. "
        "★ = current Indian FY. Consult a CA for exact liability.</p>"
    )


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tax Burden &amp; Harvest Advisor</title>
<style>
{css}
</style>
</head>
<body>
<h1>🌏 Tax Burden &amp; Harvest Advisor</h1>
<p>Generated: {generated} &nbsp;|&nbsp; Current AU FY: <strong>{au_fy}</strong>
 &nbsp;|&nbsp; Current India FY: <strong>{india_fy}</strong></p>

<p class="note">
<strong>Disclaimer:</strong> This report provides <em>estimates only</em> for planning purposes.
Tax laws change and individual circumstances vary. Consult a registered tax agent before
making any decisions. This tool does <strong>not</strong> constitute tax advice.
</p>

{summary_section}

<h2>🇦🇺 Australia — Capital Gains Tax (CGT)</h2>
<p class="note">
  Australian residents owe CGT on <em>all</em> capital gains globally, including from
  Moomoo SG (Singapore), IBKR, and Webull AU accounts.
  FY runs <strong>1 July – 30 June</strong>. 50% CGT discount for assets held ≥ 12 months.
  Tax-loss harvesting deadline: <strong>30 June {au_fy_year}</strong>
  ({days_to_fy_end} days away).
</p>
{au_urgency_badge}
<h3>Current AU FY ({au_fy}) — Realised Gains</h3>
{au_tax_section}
<h3>All FY History</h3>
{au_history_section}

<h2>🇮🇳 India — Capital Gains Tax</h2>
<p class="note">
  India FY runs <strong>1 April – 31 March</strong>.
  Delivery STCG @ 20% (held &lt; 1 yr) · Delivery LTCG @ 12.5% above ₹1.25L p.a. (held ≥ 1 yr) ·
  Intraday &amp; F&amp;O @ income-tax slab rate.
  Data comes from Zerodha + Angel One annual P&amp;L statements.
</p>
{india_tax_section}

<h2>🇸🇬 Singapore — No Capital Gains Tax</h2>
<p class="note">
  Singapore does <strong>not</strong> impose capital gains tax on securities trading.
  Gains from SGX, NYSE (via Moomoo SG), TSE, HKEX are CGT-free <em>as a Singapore entity</em>.
  <br><br>
  <strong>⚠ Important (AU residents):</strong> If you are an Australian tax resident, you owe
  Australian CGT on gains made through your Moomoo SG account regardless of where the
  account is held. The ATO taxes Australian residents on worldwide income and gains.
</p>

<h2>🌾 Tax-Loss Harvesting Opportunities</h2>
<p>
  Positions with unrealised losses below. Selling these before your FY end
  crystallises the loss and can offset realised gains, reducing your tax bill.
  <strong>AU FY end: 30 June {au_fy_year}</strong> ({days_to_fy_end} days away).
</p>
<p class="note">
  <strong>Wash-sale awareness:</strong> Australia has no formal wash-sale rule, but the ATO
  may apply Part IVA (anti-avoidance) if you buy back the same asset immediately.
  India has no wash-sale rule either. Consider waiting at least 30 days before re-entering.
  These rules apply differently per jurisdiction — confirm with your tax agent.
</p>
<div class="harvest-card">
{harvest_table}
</div>

<h2>📅 Tax Calendar</h2>
<table>
<thead><tr><th>Country</th><th>FY End</th><th>Harvest Deadline</th><th>Filing Deadline</th><th>Key Rate</th></tr></thead>
<tbody>
<tr>
  <td><span class="badge badge-au">🇦🇺 Australia</span></td>
  <td>30 Jun {au_fy_year}</td>
  <td><strong>30 Jun {au_fy_year}</strong> — sell loss positions by this date</td>
  <td>31 Oct {au_fy_year} (individual) / tax agent extension available</td>
  <td>Marginal rate (up to 47% incl. Medicare) · 50% LT discount</td>
</tr>
<tr>
  <td><span class="badge badge-in">🇮🇳 India</span></td>
  <td>31 Mar 2027</td>
  <td><strong>31 Mar 2027</strong> — sell INR loss positions by this date</td>
  <td>31 Jul 2027 (ITR filing)</td>
  <td>STCG 20% · LTCG 12.5% (above ₹1.25L) · Intraday @ slab</td>
</tr>
<tr>
  <td><span class="badge badge-sg">🇸🇬 Singapore</span></td>
  <td>31 Dec 2026</td>
  <td>N/A — no CGT</td>
  <td>15 Apr 2027 (individual income tax)</td>
  <td>No capital gains tax</td>
</tr>
</tbody>
</table>

</body>
</html>"""


def _au_urgency_badge(days_left: int) -> str:
    if days_left <= 30:
        return (
            f'<div class="urgency-high">🚨 <strong>URGENT:</strong> Only {days_left} days until '
            f'AU FY end (30 June). Act now to crystallise losses before the tax deadline!</div>'
        )
    elif days_left <= 90:
        return (
            f'<div class="urgency-medium">⚠ <strong>{days_left} days</strong> until AU FY end (30 June). '
            f'Review and action your harvest candidates soon.</div>'
        )
    else:
        return (
            f'<div class="urgency-low">✅ <strong>{days_left} days</strong> until AU FY end (30 June). '
            f'Plenty of time — but review harvest candidates regularly.</div>'
        )


def _build_summary_cards(
    au_current_fy_rows: list,
    marginal_rate_au: float,
    india_fy_rows: list,
    india_slab_rate: float,
    harvest_rows: list,
    days_left: int,
) -> str:
    au_total_tax = 0.0
    for r in au_current_fy_rows:
        net_taxable = float(r.get("Net Taxable", 0) or 0)
        au_total_tax += calc_au_tax(net_taxable, marginal_rate_au)

    india_total_tax = 0.0
    cur_india = current_india_fy()
    for r in india_fy_rows:
        if r.get("fy") == cur_india:
            td = calc_india_tax(
                float(r.get("st_inr", 0) or 0),
                float(r.get("lt_inr", 0) or 0),
                float(r.get("intraday_inr", 0) or 0),
                india_slab_rate,
            )
            india_total_tax += td["total_tax"]

    total_harvest_saving = sum(r["Tax Saving"] for r in harvest_rows)
    net_after_harvest = au_total_tax - sum(
        r["Tax Saving"] for r in harvest_rows if r["Jurisdiction"] == "Australia"
    )

    return f"""
<div class="summary-grid">
  <div class="tax-card">
    <div><span class="badge badge-au">🇦🇺 Australia</span> Current FY Tax Estimate</div>
    <div class="tax-amount tax-au">≈ {au_total_tax:,.0f}</div>
    <div style="color:#aaa;font-size:0.85em">(in native currencies, before harvesting)</div>
    <div style="margin-top:8px;font-size:0.9em">Marginal rate: {marginal_rate_au*100:.0f}%</div>
  </div>
  <div class="tax-card">
    <div><span class="badge badge-in">🇮🇳 India</span> Current FY Tax Estimate</div>
    <div class="tax-amount tax-in">≈ ₹{india_total_tax:,.0f}</div>
    <div style="color:#aaa;font-size:0.85em">(delivery + intraday/F&O)</div>
    <div style="margin-top:8px;font-size:0.9em">STCG 20% · LTCG 12.5% · Slab {india_slab_rate*100:.0f}%</div>
  </div>
  <div class="tax-card">
    <div><span class="badge badge-sg">🇸🇬 Singapore</span> CGT</div>
    <div class="tax-amount tax-sg">₀</div>
    <div style="color:#aaa;font-size:0.85em">No capital gains tax on securities</div>
    <div style="margin-top:8px;font-size:0.9em">AU CGT still applies for AU residents</div>
  </div>
  <div class="tax-card" style="border-color:#1a3a1a">
    <div>🌾 Harvest Saving Potential</div>
    <div class="tax-amount" style="color:#06d6a0">≈ {total_harvest_saving:,.0f}</div>
    <div style="color:#aaa;font-size:0.85em">({len(harvest_rows)} positions with unrealised losses)</div>
    <div style="margin-top:8px;font-size:0.9em">AU net after harvest: ≈ {net_after_harvest:,.0f}</div>
  </div>
</div>
"""


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Tax Burden Dashboard & Tax-Loss Harvest Advisor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-ibkr",    action="store_true", help="Skip IBKR data")
    parser.add_argument("--no-moomoo",  action="store_true", help="Skip Moomoo AU/SG data")
    parser.add_argument("--no-india",   action="store_true", help="Skip India data")
    parser.add_argument("--with-moomoo-history", action="store_true",
                        dest="with_moomoo_history",
                        help="Also load full Moomoo CGT trade history for FY burden accuracy. "
                             "WARNING: slow — fetches all deals since 2018 (can take ~60 min). "
                             "By default only current open positions are fetched (fast).")
    parser.add_argument("--marginal-rate-au", type=float, default=0.30,
                        dest="marginal_rate_au",
                        help="AU marginal income-tax rate (default: 0.30 = 30%%)")
    parser.add_argument("--india-slab", type=float, default=0.20,
                        dest="india_slab",
                        help="India income-tax slab for intraday/F&O (default: 0.20 = 20%%)")
    default_out = str(ROOT / "reports" / "cgt" / "tax_harvest_report.html")
    parser.add_argument("--out", default=default_out, help="Output HTML path")
    args = parser.parse_args()

    print("=" * 60)
    print("Tax Burden Dashboard & Harvest Advisor")
    print("=" * 60)

    ibkr_data:   dict = {"matched": [], "open_lots": [], "fy_sum_df": pd.DataFrame(), "warnings": []}
    moomoo_data: dict = {"matched": [], "open_lots": [], "fy_sum_df": pd.DataFrame(),
                         "positions_df": pd.DataFrame(), "warnings": []}
    india_fy_rows: list = []
    open_positions: list = []

    # ── Load IBKR ──────────────────────────────────────────────────────────────
    if not args.no_ibkr:
        print("\n[IBKR] Loading data ...")
        try:
            ibkr_mod = _load_module("cgt_ibkr", "ibkr-cgt/ibkr_cgt_analysis.py")
            ibkr_data = ibkr_mod.get_data()
            n_matched = len(ibkr_data.get("matched", []))
            print(f"  IBKR: {n_matched} matched sell events loaded.")
        except Exception as e:
            print(f"  [IBKR] Skipped: {e}")

    # ── Load Moomoo ────────────────────────────────────────────────────────────
    # Fast path (default): fetch only current open positions via OpenD.
    # Slow path (--with-moomoo-history): load full CGT trade history since 2018.
    if not args.no_moomoo:
        print("\n[Moomoo] Loading open positions (fast path — current holdings only) ...")
        au_running = _check_opend(11111)
        sg_running = _check_opend(11112)
        if au_running:
            open_positions.extend(_fetch_moomoo_open_positions(11111, "Moomoo AU"))
        else:
            print("  [Moomoo AU] OpenD port 11111 not running — skipped.")
        if sg_running:
            open_positions.extend(_fetch_moomoo_open_positions(11112, "Moomoo SG"))
        else:
            print("  [Moomoo SG] OpenD port 11112 not running — skipped.")

        if args.with_moomoo_history:
            print("  [Moomoo] Loading full CGT history (--with-moomoo-history). "
                  "This fetches all deals since 2018 and may take up to 60 minutes ...")
            try:
                moomoo_mod  = _load_module("cgt_moomoo", "moomoo-cgt/cgt_analysis.py")
                moomoo_data = moomoo_mod.get_data()
                n_matched   = len(moomoo_data.get("matched", []))
                print(f"  Moomoo: {n_matched} matched sell events loaded for FY burden.")
            except Exception as e:
                print(f"  [Moomoo history] Skipped: {e}")
        else:
            print("  [Moomoo] FY realised-gains history skipped (use --with-moomoo-history to include). "
                  "Only IBKR data will show in the FY burden section for Moomoo positions.")

    # ── Load India ─────────────────────────────────────────────────────────────
    if not args.no_india:
        print("\n[India] Loading annual P&L statements ...")
        try:
            sys.path.insert(0, str(ROOT / "india-brokers"))
            from india_cgt import load_india_fy_rows
            india_fy_rows = load_india_fy_rows()
            print(f"  India: {len(india_fy_rows)} FY rows loaded.")
        except Exception as e:
            print(f"  [India] Skipped: {e}")

    # ── Augment open positions from IBKR open lots ───────────────────────────
    # IBKR open lots from the Flex Query are added to complement Moomoo positions.
    for lot in ibkr_data.get("open_lots", []):
        # open_lots rows: symbol, currency, qty, cost_basis, market_value, unrealised
        code    = str(lot.get("symbol", lot.get("Symbol", "")))
        cur     = str(lot.get("currency", lot.get("Currency", "USD")))
        qty     = float(lot.get("quantity", lot.get("Quantity", lot.get("qty", 0))) or 0)
        cost_b  = float(lot.get("costBasisMoney", lot.get("cost_basis", 0)) or 0)
        mkt_val = float(lot.get("markPrice", lot.get("market_value", 0)) or 0)
        avg_c   = cost_b / qty if qty else 0
        upl     = float(lot.get("fifoPnlUnrealized", lot.get("unrealised", 0)) or 0)
        open_positions.append({
            "code":             code,
            "broker":           "IBKR",
            "currency":         cur,
            "qty":              qty,
            "avg_cost":         avg_c,
            "last_price":       mkt_val,
            "Total Unrealized": upl,
            "hold_days":        0,
            "term":             "Short",
        })
    print(f"\n  {len(open_positions)} total open positions for harvest analysis.")

    # ── Per-FY AU data ─────────────────────────────────────────────────────────
    cur_au_fy = current_au_fy()
    au_all_rows: list[dict] = []
    au_current_fy_rows: list[dict] = []

    for src_df in [ibkr_data.get("fy_sum_df"), moomoo_data.get("fy_sum_df")]:
        if src_df is not None and not src_df.empty:
            for _, r in src_df.iterrows():
                row_dict = r.to_dict()
                au_all_rows.append(row_dict)
                if str(r.get("FY", "")) == cur_au_fy:
                    au_current_fy_rows.append(row_dict)

    # ── Harvest analysis ───────────────────────────────────────────────────────
    harvest_rows, days_left = build_harvest_opportunities(
        open_positions,
        au_net_taxable_usd=sum(
            float(r.get("Net Taxable", 0) or 0)
            for r in au_current_fy_rows
            if r.get("Currency") == "USD"
        ),
        au_net_taxable_aud=sum(
            float(r.get("Net Taxable", 0) or 0)
            for r in au_current_fy_rows
            if r.get("Currency") == "AUD"
        ),
        india_st_gains=sum(
            float(r.get("st_inr", 0) or 0)
            for r in india_fy_rows
            if r.get("fy") == current_india_fy()
        ),
        india_lt_gains=sum(
            float(r.get("lt_inr", 0) or 0)
            for r in india_fy_rows
            if r.get("fy") == current_india_fy()
        ),
        marginal_rate_au=args.marginal_rate_au,
    )

    # ── Build HTML sections ────────────────────────────────────────────────────
    au_fy_year = int(cur_au_fy[2:])

    # AU history table (all FYs combined)
    if au_all_rows:
        au_hist_df = pd.DataFrame(au_all_rows)
        grouped = au_hist_df.groupby(["FY", "Currency"])[
            ["ST Gains", "ST Losses", "LT Gross Gains", "LT Losses", "CGT Discount", "Net Taxable"]
        ].sum().reset_index().sort_values(["FY", "Currency"])

        def _hist_row(r):
            net = float(r.get("Net Taxable", 0) or 0)
            est = calc_au_tax(net, args.marginal_rate_au)
            is_cur = "★" if str(r.get("FY", "")) == cur_au_fy else ""
            style = ' style="background:#1a1a2e"' if is_cur else ""
            return (
                f"<tr{style}>"
                f"<td>{r['FY']} {is_cur}</td>"
                f"<td>{r.get('Currency','')}</td>"
                f"<td>{_fmt(r.get('ST Gains',0))}</td>"
                f"<td>{_fmt(r.get('ST Losses',0))}</td>"
                f"<td>{_fmt(r.get('LT Gross Gains',0))}</td>"
                f"<td>{_fmt(r.get('LT Losses',0))}</td>"
                f'<td><span class="disc">{float(r.get("CGT Discount",0)):,.2f}</span></td>'
                f"<td>{_fmt(net)}</td>"
                f'<td class="neg">≈ {est:,.2f}</td>'
                f"</tr>"
            )
        hist_rows_html = "".join(_hist_row(r) for _, r in grouped.iterrows())
        au_history_html = (
            "<table><thead><tr>"
            "<th>FY</th><th>Currency</th><th>ST Gains</th><th>ST Losses</th>"
            "<th>LT Gross Gains</th><th>LT Losses</th><th>50% Discount</th>"
            "<th>Net Taxable</th><th>Est. Tax</th>"
            "</tr></thead><tbody>"
            f"{hist_rows_html}</tbody></table>"
            f"<p class='note'>★ = current FY. Est. Tax uses {args.marginal_rate_au*100:.0f}% marginal rate "
            "on Net Taxable (which already has 50% LT discount applied).</p>"
        )
    else:
        au_history_html = "<p><em>No Australian CGT data available.</em></p>"

    summary_html = _build_summary_cards(
        au_current_fy_rows, args.marginal_rate_au,
        india_fy_rows, args.india_slab,
        harvest_rows, days_left,
    )

    html = HTML_TEMPLATE.format(
        css=CSS,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        au_fy=cur_au_fy,
        india_fy=current_india_fy(),
        au_fy_year=au_fy_year,
        days_to_fy_end=days_left,
        summary_section=summary_html,
        au_urgency_badge=_au_urgency_badge(days_left),
        au_tax_section=_au_tax_section(cur_au_fy, au_current_fy_rows, args.marginal_rate_au),
        au_history_section=au_history_html,
        india_tax_section=_india_tax_section(india_fy_rows, args.india_slab),
        harvest_table=_harvest_table_html(harvest_rows),
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nReport saved: {out_path}")
    print(f"  Harvest opportunities: {len(harvest_rows)} positions")
    total_saving = sum(r["Tax Saving"] for r in harvest_rows)
    print(f"  Total harvest saving potential: {total_saving:,.2f}")
    print(f"  Days to AU FY end: {days_left}")

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent / "reports"))
        from report_db import save_tax_harvest_snapshot, archive_report as _arc
        save_tax_harvest_snapshot(harvest_rows, days_left)
        _arc(out_path)
    except Exception as _e:
        print(f"  [report_db] tax_harvest snapshot skipped: {_e}")


if __name__ == "__main__":
    main()
