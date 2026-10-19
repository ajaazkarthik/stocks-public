"""
Moomoo Portfolio Analyzer
=========================
Reads all Moomoo statement PDFs and Excel files from the stock-statement folder
and generates a detailed HTML report (with interactive charts) + a PDF report.

Usage:
    python analyze_portfolio.py

Output:
    portfolio_report.html   - Interactive HTML report
    portfolio_report.pdf    - PDF report summary
"""

import os
import re
import sys
import json
from datetime import datetime, date, timedelta
from collections import defaultdict
import pdfplumber
import openpyxl
import pandas as pd

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Configuration ─────────────────────────────────────────────────────────────
STATEMENT_DIR = os.path.join(os.path.dirname(__file__), "stock-statement")
HTML_OUTPUT   = os.path.join(os.path.dirname(__file__), "portfolio_report.html")
PDF_OUTPUT    = os.path.join(os.path.dirname(__file__), "portfolio_report.pdf")
TODAY         = date.today()

# ── Data containers ────────────────────────────────────────────────────────────
transactions   = []   # {date, market, direction, code, name, qty, currency, price, amount, fee}
dividends      = []   # {date, code, name, currency, amount, tax, net}
interest       = []   # {date, currency, amount}
cash_flows     = []   # {date, comment, currency, amount}
monthly_nav    = []   # {period, nav_aud}
monthly_holdings = {} # period -> [{symbol, qty, price, value, currency}]

# ── Helper ─────────────────────────────────────────────────────────────────────
def parse_date(s):
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            pass
    return None

def clean(v):
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("−", "-"))
    except (ValueError, TypeError):
        return 0.0

# ── PDF: Monthly Statements ────────────────────────────────────────────────────
def parse_monthly_pdf(path, period_label):
    """Extract NAV, trades and ending positions from a monthly statement PDF."""
    nav = None
    holdings = []
    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    # NAV from "Ending Net Asset Value" block
    m = re.search(r"Ending Net Asset Value\s+\d{8}\s+AUD\s+USD\s+HKD\s+Equal to\(AUD\)\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d*)", full_text)
    if m:
        aud_cash = clean(m.group(1))
        usd_cash = clean(m.group(2))
        # look for exchange rate
        ex = re.search(r"exchange rate : ([\d.]+)\s+exchange rate : ([\d.]+)", full_text)
        rate = float(ex.group(2)) if ex else 1.55
        nav = aud_cash + usd_cash * rate

    # simpler: grab "Net Asset Value: XX,XXX.XX" line
    m2 = re.search(r"Net Asset Value:\s*([\d,]+\.?\d*)", full_text)
    if m2:
        nav = clean(m2.group(1))

    # Ending positions table
    # Pattern: Symbol  Exchange  Currency  Qty  0  Qty  1  Price  MarketValue  Rate  AUD
    pos_pattern = re.compile(
        r"([A-Z]{2,6})\s+(?:ASX|US|HK)\s+(AUD|USD|HKD)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+([\d,.]+)\s+([\d,.]+)"
    )
    for m in pos_pattern.finditer(full_text):
        holdings.append({
            "symbol": m.group(1),
            "qty": int(m.group(3)),
            "price": clean(m.group(4)),
            "value": clean(m.group(5)),
            "currency": m.group(2),
        })

    # Trades
    trade_pattern = re.compile(
        r"(Buy to Open|Sell to Close|Buy|Sell)\s+(?:to Open|to Close)?\s*([A-Z]{2,10})\s+"
        r"(?:ASX|US|NYSE|NASDAQ|HK)?\s*(?:AUD|USD|HKD)?\s*(\d{4}/\d{2}/\d{2})\s+([\d.]+)\s+(\d+)\s+([\d.]+)"
    )
    for m in trade_pattern.finditer(full_text):
        d = parse_date(m.group(3))
        direction = "Buy" if "Buy" in m.group(1) else "Sell"
        transactions.append({
            "date": d,
            "market": "US",
            "direction": direction,
            "code": m.group(2),
            "name": m.group(2),
            "qty": int(m.group(5)),
            "currency": "USD",
            "price": float(m.group(4)),
            "amount": float(m.group(6)),
            "fee": 0,
            "source": period_label,
        })

    if nav:
        monthly_nav.append({"period": period_label, "nav_aud": nav})
    if holdings:
        monthly_holdings[period_label] = holdings


# ── Excel: Annual Summaries ────────────────────────────────────────────────────
def parse_annual_excel(path):
    wb = openpyxl.load_workbook(path)

    # Transactions sheet
    if "Transaction Overview" in wb.sheetnames:
        ws = wb["Transaction Overview"]
        headers = None
        for row in ws.iter_rows(values_only=True):
            if row[0] == "Market":
                headers = [str(c).strip() if c else "" for c in row]
                continue
            if headers and row[0]:
                d = parse_date(row[1])
                if not d:
                    continue
                qty = row[7] or 0
                price = clean(row[9])
                amount = clean(row[10])
                fee = clean(row[11])
                net = clean(row[13])
                direction = str(row[4]).strip() if row[4] else ""
                transactions.append({
                    "date": d,
                    "market": str(row[0]).strip(),
                    "direction": direction,
                    "code": str(row[5]).strip() if row[5] else "",
                    "name": str(row[6]).strip() if row[6] else "",
                    "qty": float(qty),
                    "currency": str(row[8]).strip() if row[8] else "",
                    "price": price,
                    "amount": abs(amount),
                    "fee": fee,
                    "net": net,
                    "source": os.path.basename(path),
                })

    # Dividend sheet
    if "Estimated Dividend Overview" in wb.sheetnames:
        ws = wb["Estimated Dividend Overview"]
        for row in ws.iter_rows(values_only=True):
            if not row[0] or row[0] == "Market":
                continue
            pay_date = parse_date(row[4])
            if not pay_date:
                continue
            dividends.append({
                "date": pay_date,
                "code": str(row[1]).strip() if row[1] else "",
                "name": str(row[2]).strip() if row[2] else "",
                "currency": str(row[5]).strip() if row[5] else "",
                "gross": clean(row[10]),
                "tax": abs(clean(row[11])),
                "net": clean(row[12]),
            })

    # Interest sheet
    if "Interest Overview" in wb.sheetnames:
        ws = wb["Interest Overview"]
        for row in ws.iter_rows(values_only=True):
            if not row[0] or row[0] == "Payment Date":
                continue
            d = parse_date(row[0])
            if d:
                interest.append({
                    "date": d,
                    "currency": str(row[1]).strip() if row[1] else "",
                    "amount": clean(row[2]),
                })

    # Cash overview
    if "Cash Overview" in wb.sheetnames:
        ws = wb["Cash Overview"]
        for row in ws.iter_rows(values_only=True):
            if not row[0] or row[0] == "Date":
                continue
            d = parse_date(row[0])
            if d:
                cash_flows.append({
                    "date": d,
                    "comment": str(row[1]).strip() if row[1] else "",
                    "currency": str(row[2]).strip() if row[2] else "",
                    "amount": clean(row[3]),
                })


# ── PDF: Annual Summaries (for FY2023-24 which only has PDF) ──────────────────
def parse_annual_pdf(path):
    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    # Transaction lines pattern for annual PDF
    # Format: Date Date Type Dir CODE Name Qty USD Price Amount Fee GST Net
    tx_pat = re.compile(
        r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+Stock\s+(Buy|Sell)\s+"
        r"([A-Z]{2,6})\s+([\w\s\-]+?)\s+(\d+\.?\d*)\s+(AUD|USD|HKD)\s+"
        r"([\d,.]+)\s+([-\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([-\d,.]+)"
    )
    for m in tx_pat.finditer(full_text):
        d = parse_date(m.group(1))
        if d:
            transactions.append({
                "date": d,
                "market": "US",
                "direction": m.group(3),
                "code": m.group(4),
                "name": m.group(5).strip(),
                "qty": float(m.group(6)),
                "currency": m.group(7),
                "price": clean(m.group(8)),
                "amount": abs(clean(m.group(9))),
                "fee": clean(m.group(10)),
                "net": clean(m.group(12)),
                "source": os.path.basename(path),
            })


# ── Load all files ─────────────────────────────────────────────────────────────
def load_all():
    if not os.path.isdir(STATEMENT_DIR):
        print(f"\n[ERROR] Statement directory not found: {STATEMENT_DIR}")
        print("  Create the folder and add Moomoo AU PDF/XLSX statements to it.")
        print("  Download from: Moomoo AU → Account → Statements\n")
        sys.exit(1)
    files = sorted(os.listdir(STATEMENT_DIR))
    for f in files:
        fpath = os.path.join(STATEMENT_DIR, f)
        if f.endswith(".xlsx"):
            print(f"  [XLSX] {f}")
            parse_annual_excel(fpath)
        elif f.endswith(".pdf"):
            # Detect monthly vs annual
            # Monthly: 1009298585383608-6-YYYYMM...pdf
            # Annual:  20XX-20XX_au_financial...pdf
            m = re.match(r"\d+-\d+-(\d{6})\d+-", f)
            if m:
                ym = m.group(1)
                year, month = int(ym[:4]), int(ym[4:])
                label = datetime(year, month, 1).strftime("%b %Y")
                print(f"  [PDF-Monthly] {f} → {label}")
                parse_monthly_pdf(fpath, label)
            elif "financial_year_summary" in f and "_1009298585383608_0.pdf" not in f:
                # The PDF annual summary (only for 2023-24 account …878199)
                print(f"  [PDF-Annual] {f}")
                parse_annual_pdf(fpath)


# ── Analysis ───────────────────────────────────────────────────────────────────
def dedupe_transactions(txns):
    """Remove duplicates that appear in both Excel and PDF sources."""
    seen = set()
    result = []
    for t in txns:
        key = (t["date"], t["code"], t["direction"], t["qty"], t["price"])
        if key not in seen:
            seen.add(key)
            result.append(t)
    return result

def build_trade_summary():
    """Build buy/sell matched P&L per security using FIFO."""
    # Group buys per code
    buys  = defaultdict(list)  # code -> [(date, qty, price, currency)]
    sells = defaultdict(list)  # code -> [(date, qty, price, currency)]

    for t in transactions:
        if t["direction"] == "Buy":
            buys[t["code"]].append(t)
        elif t["direction"] == "Sell":
            sells[t["code"]].append(t)

    closed_trades = []
    open_positions = {}

    for code in set(list(buys.keys()) + list(sells.keys())):
        buy_queue = list(buys.get(code, []))  # FIFO queue of buy lots
        sell_list = sells.get(code, [])

        remaining_buys = []
        for b in buy_queue:
            remaining_buys.append({"date": b["date"], "qty": b["qty"],
                                    "price": b["price"], "currency": b["currency"],
                                    "name": b["name"]})

        for s in sell_list:
            sell_qty   = s["qty"]
            sell_price = s["price"]
            sell_date  = s["date"]
            currency   = s["currency"]

            while sell_qty > 0 and remaining_buys:
                lot = remaining_buys[0]
                matched = min(sell_qty, lot["qty"])
                pnl = (sell_price - lot["price"]) * matched
                closed_trades.append({
                    "code":       code,
                    "name":       lot["name"],
                    "currency":   currency,
                    "buy_date":   lot["date"],
                    "sell_date":  sell_date,
                    "qty":        matched,
                    "buy_price":  lot["price"],
                    "sell_price": sell_price,
                    "pnl":        pnl,
                    "held_days":  (sell_date - lot["date"]).days if lot["date"] and sell_date else 0,
                })
                lot["qty"] -= matched
                sell_qty   -= matched
                if lot["qty"] <= 0:
                    remaining_buys.pop(0)

        # whatever is left in remaining_buys = open position
        if remaining_buys:
            total_qty  = sum(b["qty"] for b in remaining_buys)
            total_cost = sum(b["qty"] * b["price"] for b in remaining_buys)
            avg_price  = total_cost / total_qty if total_qty > 0 else 0
            earliest   = min(b["date"] for b in remaining_buys if b["date"])
            open_positions[code] = {
                "code":      code,
                "name":      remaining_buys[0]["name"],
                "currency":  remaining_buys[0]["currency"],
                "qty":       total_qty,
                "avg_price": avg_price,
                "first_buy": earliest,
                "held_days": (TODAY - earliest).days if earliest else 0,
                "long_term": (TODAY - earliest).days > 365 if earliest else False,
            }

    return closed_trades, open_positions

def build_nav_series():
    """Build a clean NAV time series from monthly statements."""
    nav_points = []
    # Manually add known NAV points from annual summaries
    known = [
        ("Jan 2024",  100300.0),   # ~100K AUD deposited
        ("Jun 2024",   92000.0),   # end FY2023-24
        ("Jun 2025",   42814.0),   # end FY2024-25
    ]
    for label, nav in known:
        nav_points.append({"period": label, "nav_aud": nav})

    nav_points.extend(monthly_nav)

    # Sort by date
    def sort_key(x):
        try:
            return datetime.strptime(x["period"], "%b %Y")
        except (ValueError, TypeError):
            try:
                return datetime.strptime(x["period"], "%b %y")
            except (ValueError, TypeError):
                return datetime.min
    nav_points.sort(key=sort_key)

    # Dedupe by period
    seen = {}
    for p in nav_points:
        seen[p["period"]] = p["nav_aud"]
    return [{"period": k, "nav_aud": v} for k, v in seen.items()]

def compute_interest_summary():
    aud_total = sum(i["amount"] for i in interest if i["currency"] == "AUD")
    usd_total = sum(i["amount"] for i in interest if i["currency"] == "USD")
    return aud_total, usd_total

def compute_dividend_summary():
    results = defaultdict(lambda: {"gross": 0, "tax": 0, "net": 0, "currency": ""})
    for d in dividends:
        results[d["code"]]["gross"]    += d["gross"]
        results[d["code"]]["tax"]      += d["tax"]
        results[d["code"]]["net"]      += d["net"]
        results[d["code"]]["currency"]  = d["currency"]
        results[d["code"]]["name"]      = d["name"]
    return results

def compute_cash_deposits_withdrawals():
    deposits    = 0.0
    withdrawals = 0.0
    for c in cash_flows:
        if c["currency"] == "AUD":
            amt = c["amount"]
            comment = c["comment"].upper()
            # Skip inter-account transfers and interest
            if any(x in comment for x in ["INTEREST", "DIVIDEND", "TRANSFER FROM", "TRANSFER TO", "WITHHOLDING"]):
                continue
            if amt > 0:
                deposits += amt
            else:
                withdrawals += abs(amt)
    return deposits, withdrawals

def get_monthly_trade_volume():
    monthly = defaultdict(lambda: {"buys": 0.0, "sells": 0.0})
    for t in transactions:
        if not t["date"]:
            continue
        label = t["date"].strftime("%b %Y")
        if t["direction"] == "Buy":
            monthly[label]["buys"] += t["qty"] * t["price"]
        else:
            monthly[label]["sells"] += t["qty"] * t["price"]
    return monthly


# ── HTML Generation ────────────────────────────────────────────────────────────
COLORS = {
    "primary":   "#1a1f3a",
    "accent":    "#4f8ef7",
    "green":     "#00c896",
    "red":       "#f05a5a",
    "yellow":    "#f5c518",
    "bg":        "#0d1117",
    "card":      "#161b2e",
    "border":    "#2a3055",
    "text":      "#e0e6f0",
    "muted":     "#8892a4",
}

def fmt_currency(v, symbol="$", decimals=2):
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}{symbol}{abs(v):,.{decimals}f}"

def pnl_color(v):
    if v > 0:
        return COLORS["green"]
    elif v < 0:
        return COLORS["red"]
    return COLORS["muted"]

def trend_arrow(v):
    if v > 0:
        return "▲"
    elif v < 0:
        return "▼"
    return "–"

def generate_html(closed_trades, open_positions, nav_series, div_summary,
                  aud_interest, usd_interest, deposits, withdrawals):
    import json

    # ── Stat cards ────────────────────────────────────────────────────────────
    current_nav = nav_series[-1]["nav_aud"] if nav_series else 0
    prev_nav    = nav_series[-2]["nav_aud"] if len(nav_series) > 1 else current_nav
    nav_change  = current_nav - prev_nav
    nav_pct     = (nav_change / prev_nav * 100) if prev_nav else 0

    total_pnl_usd = sum(t["pnl"] for t in closed_trades if t["currency"] == "USD")
    total_pnl_aud = sum(t["pnl"] for t in closed_trades if t["currency"] == "AUD")
    open_count    = len(open_positions)
    lt_count      = sum(1 for p in open_positions.values() if p["long_term"])

    total_dividends_usd = sum(d["net"] for d in dividends if d["currency"] == "USD")
    total_dividends_aud = sum(d["net"] for d in dividends if d["currency"] == "AUD")

    # ── Build chart data ──────────────────────────────────────────────────────
    nav_labels = [n["period"] for n in nav_series]
    nav_values = [n["nav_aud"] for n in nav_series]

    # Closed trade P&L bar by security (aggregate)
    pnl_by_code = defaultdict(float)
    pnl_currency = {}
    for t in closed_trades:
        pnl_by_code[t["code"]] += t["pnl"]
        pnl_currency[t["code"]] = t["currency"]
    pnl_sorted = sorted(pnl_by_code.items(), key=lambda x: x[1])
    pnl_codes  = [p[0] for p in pnl_sorted]
    pnl_values = [round(p[1], 2) for p in pnl_sorted]
    pnl_colors = [COLORS["green"] if v >= 0 else COLORS["red"] for v in pnl_values]

    # Monthly trading volume
    monthly_vol = get_monthly_trade_volume()
    vol_labels  = sorted(monthly_vol.keys(), key=lambda x: datetime.strptime(x, "%b %Y") if x else datetime.min)
    vol_buys    = [round(monthly_vol[l]["buys"], 2) for l in vol_labels]
    vol_sells   = [round(monthly_vol[l]["sells"], 2) for l in vol_labels]

    # Dividend by stock
    div_labels = list(div_summary.keys())
    div_nets   = [round(div_summary[k]["net"], 2) for k in div_labels]
    div_currencies = [div_summary[k]["currency"] for k in div_labels]

    # Open position allocation
    open_list = sorted(open_positions.values(), key=lambda x: x["qty"] * x["avg_price"], reverse=True)
    alloc_labels = [p["code"] for p in open_list]
    alloc_values = [round(p["qty"] * p["avg_price"], 2) for p in open_list]

    # ── Interest timeline ─────────────────────────────────────────────────────
    int_by_month = defaultdict(float)
    for i in interest:
        label = i["date"].strftime("%b %Y")
        # convert USD to AUD roughly
        amt = i["amount"] * (1.5 if i["currency"] == "USD" else 1.0)
        int_by_month[label] += amt
    int_labels = sorted(int_by_month.keys(), key=lambda x: datetime.strptime(x, "%b %Y"))
    int_values = [round(int_by_month[l], 2) for l in int_labels]

    # ── Build open positions table rows ───────────────────────────────────────
    open_rows = ""
    for p in open_list:
        cost_basis = p["qty"] * p["avg_price"]
        ccy        = p["currency"]
        held_label = f"{p['held_days']} days"
        lt_badge   = '<span class="badge badge-green">Long-Term</span>' if p["long_term"] else '<span class="badge badge-muted">Short-Term</span>'
        # market value approx (from latest monthly if available)
        open_rows += f"""
        <tr>
            <td><strong>{p['code']}</strong></td>
            <td>{p['name']}</td>
            <td>{ccy}</td>
            <td class="num">{p['qty']:,.0f}</td>
            <td class="num">{fmt_currency(p['avg_price'], '')}</td>
            <td class="num">{fmt_currency(cost_basis, '')}</td>
            <td>{p['first_buy'].strftime('%d %b %Y') if p['first_buy'] else '—'}</td>
            <td>{held_label}</td>
            <td>{lt_badge}</td>
        </tr>"""

    # ── Build closed trades table rows ────────────────────────────────────────
    closed_rows = ""
    # Sort by sell date desc
    for t in sorted(closed_trades, key=lambda x: x["sell_date"] or date.min, reverse=True):
        pnl_style = f"color:{pnl_color(t['pnl'])};font-weight:600"
        arrow     = trend_arrow(t["pnl"])
        held_str  = f"{t['held_days']}d"
        closed_rows += f"""
        <tr>
            <td><strong>{t['code']}</strong></td>
            <td>{t['name']}</td>
            <td>{t['currency']}</td>
            <td class="num">{t['qty']:,.0f}</td>
            <td class="num">{fmt_currency(t['buy_price'], '')}</td>
            <td class="num">{fmt_currency(t['sell_price'], '')}</td>
            <td class="num" style="{pnl_style}">{arrow} {fmt_currency(t['pnl'], '')}</td>
            <td>{t['buy_date'].strftime('%d %b %Y') if t['buy_date'] else '—'}</td>
            <td>{t['sell_date'].strftime('%d %b %Y') if t['sell_date'] else '—'}</td>
            <td>{held_str}</td>
        </tr>"""

    # ── Build dividends table ─────────────────────────────────────────────────
    div_rows = ""
    for k, v in div_summary.items():
        div_rows += f"""
        <tr>
            <td><strong>{k}</strong></td>
            <td>{v.get('name','')}</td>
            <td>{v['currency']}</td>
            <td class="num">{fmt_currency(v['gross'], '')}</td>
            <td class="num" style="color:{COLORS['red']}">{fmt_currency(v['tax'], '')}</td>
            <td class="num" style="color:{COLORS['green']}">{fmt_currency(v['net'], '')}</td>
        </tr>"""

    # ── Long-term open holdings section ──────────────────────────────────────
    lt_open = [p for p in open_positions.values() if p["long_term"]]
    if lt_open:
        lt_msg = f"""
        <div class="alert alert-green">
            ✅ <strong>{len(lt_open)} open position(s)</strong> held for more than 12 months (long-term CGT eligible):
            {', '.join(p['code'] for p in lt_open)}
        </div>"""
    else:
        lt_msg = """
        <div class="alert alert-yellow">
            ⚠️ <strong>No open positions</strong> currently qualify as long-term (held &gt;12 months as of today).
            Your earliest current positions are approaching the 12-month mark.
        </div>"""

    # Positions approaching 12 months (8-12 months)
    approaching = [p for p in open_positions.values()
                   if 240 <= p["held_days"] < 365]
    if approaching:
        approaching_str = ", ".join(
            f"{p['code']} ({365 - p['held_days']} days to go)"
            for p in sorted(approaching, key=lambda x: x["held_days"], reverse=True)
        )
        lt_msg += f"""
        <div class="alert alert-blue" style="margin-top:8px">
            🕐 <strong>Approaching long-term:</strong> {approaching_str}
        </div>"""

    # ── Assemble HTML ─────────────────────────────────────────────────────────
    generation_time = datetime.now().strftime("%d %b %Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Portfolio Analysis — Priyadharshini Kalyanaraman</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js" integrity="sha384-7TVmlZWH60iKX5Uk7lSvQhjtcgw2tkFjuwLcXoRSR4zXTyWFJRm9aPAguMh7CIra" crossorigin="anonymous"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: {COLORS["bg"]};
    color: {COLORS["text"]};
    line-height: 1.6;
    font-size: 14px;
  }}
  a {{ color: {COLORS["accent"]}; text-decoration: none; }}

  /* Header */
  .header {{
    background: linear-gradient(135deg, #1a1f3a 0%, #0f1629 100%);
    border-bottom: 1px solid {COLORS["border"]};
    padding: 28px 40px;
    display: flex;
    align-items: center;
    gap: 20px;
  }}
  .header-logo {{
    width: 48px; height: 48px;
    background: {COLORS["accent"]};
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 22px; font-weight: 800; color: #fff;
  }}
  .header-title h1 {{ font-size: 22px; font-weight: 700; color: #fff; }}
  .header-title p  {{ font-size: 13px; color: {COLORS["muted"]}; margin-top: 3px; }}
  .header-meta    {{ margin-left: auto; text-align: right; font-size: 12px; color: {COLORS["muted"]}; }}

  /* Layout */
  .container {{ max-width: 1400px; margin: 0 auto; padding: 32px 40px; }}
  .section    {{ margin-bottom: 40px; }}
  .section-title {{
    font-size: 16px; font-weight: 700; color: #fff;
    border-left: 3px solid {COLORS["accent"]};
    padding-left: 12px; margin-bottom: 20px;
    display: flex; align-items: center; gap: 8px;
  }}

  /* Stat cards */
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }}
  .stat-card {{
    background: {COLORS["card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 12px;
    padding: 20px;
    transition: border-color .2s;
  }}
  .stat-card:hover {{ border-color: {COLORS["accent"]}; }}
  .stat-label {{ font-size: 12px; color: {COLORS["muted"]}; text-transform: uppercase; letter-spacing: .6px; }}
  .stat-value {{ font-size: 26px; font-weight: 700; color: #fff; margin: 6px 0; }}
  .stat-sub   {{ font-size: 12px; }}

  /* Charts */
  .chart-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }}
  .chart-grid.full {{ grid-template-columns: 1fr; }}
  .chart-card {{
    background: {COLORS["card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 12px;
    padding: 20px;
  }}
  .chart-card h3 {{
    font-size: 14px; color: {COLORS["muted"]};
    margin-bottom: 14px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .5px;
  }}

  /* Tables */
  .table-wrap {{
    background: {COLORS["card"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 20px;
  }}
  table {{
    width: 100%; border-collapse: collapse;
  }}
  thead th {{
    background: #1e2541;
    padding: 12px 16px;
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .7px;
    color: {COLORS["muted"]};
    font-weight: 600;
    white-space: nowrap;
  }}
  tbody tr {{
    border-top: 1px solid {COLORS["border"]};
    transition: background .15s;
  }}
  tbody tr:hover {{ background: rgba(79,142,247,.06); }}
  td {{
    padding: 11px 16px;
    font-size: 13px;
    vertical-align: middle;
  }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}

  /* Badges */
  .badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .4px;
  }}
  .badge-green {{ background: rgba(0,200,150,.15); color: {COLORS["green"]}; }}
  .badge-muted {{ background: rgba(136,146,164,.12); color: {COLORS["muted"]}; }}
  .badge-yellow {{ background: rgba(245,197,24,.15); color: {COLORS["yellow"]}; }}
  .badge-red   {{ background: rgba(240,90,90,.15); color: {COLORS["red"]}; }}
  .badge-blue  {{ background: rgba(79,142,247,.15); color: {COLORS["accent"]}; }}

  /* Alerts */
  .alert {{
    padding: 14px 18px;
    border-radius: 10px;
    border-left: 4px solid;
    font-size: 13px;
    margin-bottom: 12px;
  }}
  .alert-green  {{ background: rgba(0,200,150,.08); border-color: {COLORS["green"]}; color: #d0fff4; }}
  .alert-yellow {{ background: rgba(245,197,24,.08); border-color: {COLORS["yellow"]}; color: #fff8d0; }}
  .alert-blue   {{ background: rgba(79,142,247,.08); border-color: {COLORS["accent"]}; color: #d0e6ff; }}
  .alert-red    {{ background: rgba(240,90,90,.08); border-color: {COLORS["red"]}; color: #ffd0d0; }}

  /* Responsive */
  @media (max-width: 900px) {{
    .container {{ padding: 20px; }}
    .chart-grid {{ grid-template-columns: 1fr; }}
    .stat-grid  {{ grid-template-columns: 1fr 1fr; }}
    .header     {{ padding: 20px; flex-wrap: wrap; }}
  }}
  @media print {{
    body {{ background: #fff; color: #000; font-size: 11px; }}
    .chart-card, .table-wrap, .stat-card {{ border: 1px solid #ccc; }}
    .header {{ background: #1a1f3a; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style>
</head>
<body>

<!-- ── HEADER ─────────────────────────────────────────────────────────────── -->
<header class="header">
  <div class="header-logo">M</div>
  <div class="header-title">
    <h1>Portfolio Analysis Report</h1>
    <p>Priyadharshini Kalyanaraman · Moomoo Securities Australia · Account 1009298585383608</p>
  </div>
  <div class="header-meta">
    <div>Generated: {generation_time}</div>
    <div>Analysis Date: {TODAY.strftime('%d %B %Y')}</div>
    <div>Period: Jan 2024 – Feb 2026</div>
  </div>
</header>

<!-- ── MAIN ───────────────────────────────────────────────────────────────── -->
<div class="container">

  <!-- ── STAT CARDS ───────────────────────────────────────────────────────── -->
  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">Portfolio Value (AUD)</div>
      <div class="stat-value">{fmt_currency(current_nav)}</div>
      <div class="stat-sub" style="color:{pnl_color(nav_change)}">
        {trend_arrow(nav_change)} {fmt_currency(nav_change)} ({nav_pct:+.1f}%) vs prev month
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Realised P&L (USD)</div>
      <div class="stat-value" style="color:{pnl_color(total_pnl_usd)}">{fmt_currency(total_pnl_usd)}</div>
      <div class="stat-sub" style="color:{COLORS['muted']}">Closed US positions</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Realised P&L (AUD)</div>
      <div class="stat-value" style="color:{pnl_color(total_pnl_aud)}">{fmt_currency(total_pnl_aud)}</div>
      <div class="stat-sub" style="color:{COLORS['muted']}">Closed ASX positions</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Open Positions</div>
      <div class="stat-value">{open_count}</div>
      <div class="stat-sub" style="color:{COLORS['green']}">
        {lt_count} long-term (&gt;12 mo)
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Interest Earned (AUD)</div>
      <div class="stat-value" style="color:{COLORS['green']}">{fmt_currency(aud_interest)}</div>
      <div class="stat-sub" style="color:{COLORS['muted']}">+ USD {usd_interest:,.2f} (Cash Plus)</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Dividends Received</div>
      <div class="stat-value" style="color:{COLORS['yellow']}">{fmt_currency(total_dividends_usd, 'USD ')}</div>
      <div class="stat-sub" style="color:{COLORS['muted']}">+ {fmt_currency(total_dividends_aud, 'AUD ')} net</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Deposited</div>
      <div class="stat-value">{fmt_currency(deposits, 'AUD ')}</div>
      <div class="stat-sub" style="color:{COLORS['red']}">— Withdrawn: {fmt_currency(withdrawals, 'AUD ')}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Transactions</div>
      <div class="stat-value">{len(transactions)}</div>
      <div class="stat-sub" style="color:{COLORS['muted']}">Across AU + US markets</div>
    </div>
  </div>

  <!-- ── LONG-TERM ALERT ───────────────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">⏱ Long-Term Holdings Status (CGT 50% Discount)</div>
    {lt_msg}
  </div>

  <!-- ── CHARTS ROW 1 ─────────────────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">📈 Portfolio Performance</div>
    <div class="chart-grid full">
      <div class="chart-card">
        <h3>Net Asset Value Over Time (AUD)</h3>
        <div id="chartNAV" style="height:300px"></div>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-card">
        <h3>Monthly Trading Volume (AUD equiv.)</h3>
        <div id="chartVolume" style="height:280px"></div>
      </div>
      <div class="chart-card">
        <h3>Open Position Allocation (cost basis)</h3>
        <div id="chartAlloc" style="height:280px"></div>
      </div>
    </div>
  </div>

  <!-- ── OPEN POSITIONS ───────────────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">📂 Current Open Positions</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th><th>Name</th><th>CCY</th>
            <th>Qty</th><th>Avg Buy Price</th><th>Cost Basis</th>
            <th>First Buy</th><th>Held</th><th>Term</th>
          </tr>
        </thead>
        <tbody>{open_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- ── P&L CHARTS ────────────────────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">💰 Realised P&L Analysis</div>
    <div class="chart-grid full">
      <div class="chart-card">
        <h3>P&L by Security (Closed Positions)</h3>
        <div id="chartPnL" style="height:320px"></div>
      </div>
    </div>
  </div>

  <!-- ── CLOSED TRADES TABLE ──────────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">📋 Closed Trade History</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th><th>Name</th><th>CCY</th>
            <th>Qty</th><th>Buy Price</th><th>Sell Price</th>
            <th>P&L</th><th>Buy Date</th><th>Sell Date</th><th>Held</th>
          </tr>
        </thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- ── INCOME CHARTS ─────────────────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">💵 Income: Dividends &amp; Interest</div>
    <div class="chart-grid">
      <div class="chart-card">
        <h3>Dividends Received by Stock (net, original CCY)</h3>
        <div id="chartDiv" style="height:280px"></div>
      </div>
      <div class="chart-card">
        <h3>Monthly Interest Income (AUD equiv.)</h3>
        <div id="chartInterest" style="height:280px"></div>
      </div>
    </div>

    <!-- Dividend detail table -->
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Symbol</th><th>Name</th><th>CCY</th><th>Gross</th><th>Tax</th><th>Net</th></tr>
        </thead>
        <tbody>{div_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- ── STRATEGY TIMELINE ─────────────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">🗓 Trading Strategy Evolution</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Period</th><th>Style</th><th>Key Holdings</th><th>Notes</th></tr></thead>
        <tbody>
          <tr>
            <td>Jan–Jun 2024</td>
            <td><span class="badge badge-blue">Speculative</span></td>
            <td>AMC, SABR, TSLA, GOOGL</td>
            <td>Early exploration with small US positions. Large AUD cash balance earning interest.</td>
          </tr>
          <tr>
            <td>Jul–Dec 2024</td>
            <td><span class="badge badge-yellow">Core US Tech</span></td>
            <td>DELL, NVDA, MSFT, CRWD, PLTR, NTNX</td>
            <td>Built quality tech positions. Deployed AUD cash into USD equities.</td>
          </tr>
          <tr>
            <td>Apr–Jun 2025</td>
            <td><span class="badge badge-muted">ASX Short-Term</span></td>
            <td>WDS, ANZ, BRG, JHX, RWC, BEN, EVN, PME</td>
            <td>Active ASX trading – most positions held days to weeks. Mixed results.</td>
          </tr>
          <tr>
            <td>Jul–Nov 2025</td>
            <td><span class="badge badge-red">Leveraged ETFs</span></td>
            <td>NVDU, SOXL, METU, CWVX, OKLL, CURE</td>
            <td>Shifted to 2-3× leveraged US ETFs. High risk/reward. Portfolio value volatile.</td>
          </tr>
          <tr>
            <td>Dec 2025–Feb 2026</td>
            <td><span class="badge badge-red">Leveraged + ASX</span></td>
            <td>NVDU, SOXL, METU + AAI, ALQ, MND</td>
            <td>Continued leveraged ETF strategy. Added ASX quality stocks alongside.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ── FOOTER ────────────────────────────────────────────────────────────── -->
  <div style="border-top:1px solid {COLORS['border']};padding-top:24px;color:{COLORS['muted']};font-size:12px;text-align:center;margin-top:40px">
    <p>⚠️ <strong>Disclaimer:</strong> This report is auto-generated from statement data for informational purposes only. It does not constitute financial advice. P&L figures are approximate (FIFO matching). Please consult a licensed financial adviser for investment or tax decisions.</p>
    <p style="margin-top:8px">Moomoo Securities Australia Ltd (ACN 095 920 648) (AFSL No. 224663) · Report generated {generation_time}</p>
  </div>

</div><!-- /container -->

<!-- ── PLOTLY CHARTS ──────────────────────────────────────────────────────── -->
<script>
const PLOTLY_CONFIG = {{layout: {{paper_bgcolor:'transparent', plot_bgcolor:'transparent',
  font:{{color:'{COLORS["text"]}',family:'Segoe UI,system-ui,sans-serif',size:12}},
  margin:{{t:10,r:10,b:40,l:60}},
  xaxis:{{gridcolor:'{COLORS["border"]}',tickcolor:'{COLORS["muted"]}'}},
  yaxis:{{gridcolor:'{COLORS["border"]}',tickcolor:'{COLORS["muted"]}'}},
  showlegend:true,
  legend:{{bgcolor:'transparent',font:{{size:11}}}},
}}, config:{{responsive:true,displayModeBar:false}}}};

// NAV Line Chart
Plotly.newPlot('chartNAV', [{{
  x: {json.dumps(nav_labels)},
  y: {json.dumps(nav_values)},
  type: 'scatter', mode: 'lines+markers',
  line: {{color:'{COLORS["accent"]}', width:3}},
  marker: {{color:'{COLORS["accent"]}', size:7}},
  fill: 'tozeroy', fillcolor: 'rgba(79,142,247,0.08)',
  name: 'NAV (AUD)',
  hovertemplate: '%{{x}}<br>AUD %{{y:,.0f}}<extra></extra>',
}}], {{...PLOTLY_CONFIG.layout, margin:{{t:10,r:20,b:50,l:80}}}}, PLOTLY_CONFIG.config);

// Volume Bar Chart
Plotly.newPlot('chartVolume', [
  {{x:{json.dumps(vol_labels)}, y:{json.dumps(vol_buys)},  name:'Buys',  type:'bar', marker:{{color:'{COLORS["green"]}',opacity:0.8}}}},
  {{x:{json.dumps(vol_labels)}, y:{json.dumps(vol_sells)}, name:'Sells', type:'bar', marker:{{color:'{COLORS["red"]}',opacity:0.8}}}},
], {{...PLOTLY_CONFIG.layout, barmode:'group', margin:{{t:10,r:20,b:70,l:60}}, xaxis:{{...PLOTLY_CONFIG.layout.xaxis, tickangle:-45}} }}, PLOTLY_CONFIG.config);

// Open Allocation Donut
Plotly.newPlot('chartAlloc', [{{
  labels: {json.dumps(alloc_labels)},
  values: {json.dumps(alloc_values)},
  type: 'pie', hole: 0.5,
  textinfo: 'label+percent',
  textfont: {{size:11}},
  marker: {{colors: ['#4f8ef7','#00c896','#f5c518','#f05a5a','#a78bfa','#fb923c','#34d399','#60a5fa','#f472b6','#facc15','#38bdf8','#4ade80']}},
  hovertemplate: '%{{label}}<br>Cost: %{{value:,.2f}}<br>%{{percent}}<extra></extra>',
}}], {{...PLOTLY_CONFIG.layout, margin:{{t:20,r:20,b:20,l:20}}, showlegend:true, legend:{{orientation:'v',x:1.02,y:0.5}} }}, PLOTLY_CONFIG.config);

// P&L Bar Chart
Plotly.newPlot('chartPnL', [{{
  x: {json.dumps(pnl_codes)},
  y: {json.dumps(pnl_values)},
  type: 'bar',
  marker: {{ color: {json.dumps(pnl_colors)} }},
  hovertemplate: '%{{x}}<br>P&L: %{{y:,.2f}}<extra></extra>',
  name: 'P&L',
}}], {{...PLOTLY_CONFIG.layout, margin:{{t:10,r:20,b:60,l:80}}, showlegend:false }}, PLOTLY_CONFIG.config);

// Dividend Bar Chart
Plotly.newPlot('chartDiv', [{{
  x: {json.dumps(div_labels)},
  y: {json.dumps(div_nets)},
  type: 'bar',
  marker: {{color:'{COLORS["yellow"]}', opacity:0.85}},
  text: {json.dumps(div_currencies)},
  hovertemplate: '%{{x}} (%{{text}})<br>Net: %{{y:.2f}}<extra></extra>',
  name: 'Net Dividend',
}}], {{...PLOTLY_CONFIG.layout, margin:{{t:10,r:20,b:60,l:60}}, showlegend:false}}, PLOTLY_CONFIG.config);

// Interest Line Chart
Plotly.newPlot('chartInterest', [{{
  x: {json.dumps(int_labels)},
  y: {json.dumps(int_values)},
  type: 'bar',
  marker: {{color:'{COLORS["green"]}', opacity:0.8}},
  name: 'Interest (AUD equiv.)',
  hovertemplate: '%{{x}}<br>AUD %{{y:,.2f}}<extra></extra>',
}}], {{...PLOTLY_CONFIG.layout, margin:{{t:10,r:20,b:70,l:60}}, xaxis:{{...PLOTLY_CONFIG.layout.xaxis, tickangle:-45}} }}, PLOTLY_CONFIG.config);
</script>
</body>
</html>
"""
    return html


# ── PDF Generation ─────────────────────────────────────────────────────────────
def generate_pdf(closed_trades, open_positions, nav_series, div_summary,
                 aud_interest, usd_interest, deposits, withdrawals):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, HRFlowable, KeepTogether)
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    doc = SimpleDocTemplate(
        PDF_OUTPUT,
        pagesize=A4,
        topMargin=18*mm, bottomMargin=18*mm,
        leftMargin=18*mm, rightMargin=18*mm,
        title="Portfolio Analysis — Priyadharshini Kalyanaraman",
        author="Moomoo Portfolio Analyzer",
    )

    W, H = A4
    content_w = W - 36*mm

    # Color palette
    C_PRIMARY   = colors.HexColor("#1a1f3a")
    C_ACCENT    = colors.HexColor("#4f8ef7")
    C_GREEN     = colors.HexColor("#00a87a")
    C_RED       = colors.HexColor("#d94040")
    C_YELLOW    = colors.HexColor("#d4a800")
    C_MUTED     = colors.HexColor("#6b7280")
    C_BG_LIGHT  = colors.HexColor("#f0f4ff")
    C_BG_DARK   = colors.HexColor("#dde4f5")
    C_WHITE     = colors.white
    C_BLACK     = colors.HexColor("#1a1a2e")
    C_BORDER    = colors.HexColor("#c8d0e8")

    # Styles
    ss   = getSampleStyleSheet()
    h1   = ParagraphStyle("H1",  parent=ss["Heading1"], fontSize=18, textColor=C_PRIMARY,  spaceAfter=4)
    h2   = ParagraphStyle("H2",  parent=ss["Heading2"], fontSize=13, textColor=C_ACCENT,   spaceAfter=6, spaceBefore=14, borderPadding=(0,0,4,0))
    body = ParagraphStyle("Body",parent=ss["Normal"],   fontSize=9,  textColor=C_BLACK,    leading=14)
    small= ParagraphStyle("Small",parent=ss["Normal"],  fontSize=8,  textColor=C_MUTED,    leading=12)
    bold = ParagraphStyle("Bold", parent=body,           fontName="Helvetica-Bold")
    warn = ParagraphStyle("Warn", parent=body, backColor=colors.HexColor("#fff8dc"),
                           borderColor=C_YELLOW, borderWidth=1, borderPadding=6, leading=14)
    ok   = ParagraphStyle("OK",   parent=body, backColor=colors.HexColor("#dcffe8"),
                           borderColor=C_GREEN,  borderWidth=1, borderPadding=6, leading=14)

    story = []

    def section(title, icon=""):
        story.append(Spacer(1, 4*mm))
        story.append(HRFlowable(width="100%", thickness=1, color=C_BORDER))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(f"{icon} {title}", h2))

    def table_style(header_bg=C_PRIMARY, alt=True):
        ts = [
            ("BACKGROUND", (0,0), (-1,0), header_bg),
            ("TEXTCOLOR",  (0,0), (-1,0), C_WHITE),
            ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",   (0,0), (-1,0), 8),
            ("ALIGN",      (0,0), (-1,0), "CENTER"),
            ("BOTTOMPADDING", (0,0), (-1,0), 6),
            ("TOPPADDING",    (0,0), (-1,0), 6),
            ("FONTSIZE",   (0,1), (-1,-1), 8),
            ("BOTTOMPADDING", (0,1), (-1,-1), 4),
            ("TOPPADDING",    (0,1), (-1,-1), 4),
            ("GRID",       (0,0), (-1,-1), 0.4, C_BORDER),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_WHITE, C_BG_LIGHT] if alt else [C_WHITE]),
        ]
        return TableStyle(ts)

    def money(v, sym="", dec=2):
        if v is None: return "—"
        sign = "-" if v < 0 else ""
        return f"{sign}{sym}{abs(v):,.{dec}f}"

    # ── Cover ─────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 10*mm))
    story.append(Paragraph("Portfolio Analysis Report", h1))
    story.append(Paragraph(
        "Priyadharshini Kalyanaraman · Moomoo Securities Australia · Account 1009298585383608",
        ParagraphStyle("Sub", parent=body, textColor=C_MUTED, fontSize=10, spaceAfter=4)
    ))
    story.append(Paragraph(
        f"Period: January 2024 – February 2026  ·  Generated: {datetime.now().strftime('%d %b %Y %H:%M')}",
        small
    ))
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=C_ACCENT))
    story.append(Spacer(1, 4*mm))

    # ── Portfolio Summary ─────────────────────────────────────────────────────
    section("Portfolio Summary", "📊")
    current_nav = nav_series[-1]["nav_aud"] if nav_series else 0
    total_pnl_usd = sum(t["pnl"] for t in closed_trades if t["currency"] == "USD")
    total_pnl_aud = sum(t["pnl"] for t in closed_trades if t["currency"] == "AUD")
    lt_count   = sum(1 for p in open_positions.values() if p["long_term"])
    open_count = len(open_positions)
    total_div_usd = sum(d["net"] for d in dividends if d["currency"] == "USD")
    total_div_aud = sum(d["net"] for d in dividends if d["currency"] == "AUD")

    summary_data = [
        ["Metric", "Value", "Metric", "Value"],
        ["Current NAV (AUD)", f"AUD {current_nav:,.2f}", "Open Positions", str(open_count)],
        ["Realised P&L (USD)", money(total_pnl_usd, "USD "), "Long-Term Holdings (>12mo)", str(lt_count)],
        ["Realised P&L (AUD)", money(total_pnl_aud, "AUD "), "Total Transactions", str(len(transactions))],
        ["Interest Earned (AUD)", f"AUD {aud_interest:,.2f}", "Interest Earned (USD)", f"USD {usd_interest:,.2f}"],
        ["Total Deposited", f"AUD {deposits:,.2f}", "Total Withdrawn", f"AUD {withdrawals:,.2f}"],
        ["Dividends Net (USD)", f"USD {total_div_usd:,.2f}", "Dividends Net (AUD)", f"AUD {total_div_aud:,.2f}"],
    ]
    t = Table(summary_data, colWidths=[content_w*0.28, content_w*0.22, content_w*0.28, content_w*0.22])
    t.setStyle(table_style())
    story.append(t)

    # ── Long-term holdings ────────────────────────────────────────────────────
    section("Long-Term Holdings Analysis (CGT 50% Discount)", "⏱")
    lt_open = [p for p in open_positions.values() if p["long_term"]]
    if lt_open:
        story.append(Paragraph(
            f"✅ {len(lt_open)} position(s) held for more than 12 months and qualify for the 50% CGT discount: "
            + ", ".join(p["code"] for p in lt_open),
            ok
        ))
    else:
        story.append(Paragraph(
            "⚠️  No currently open positions have been held for more than 12 months as of today. "
            "All current holdings were initiated between May 2025 and early 2026.",
            warn
        ))

    approaching = [p for p in open_positions.values() if 240 <= p["held_days"] < 365]
    if approaching:
        story.append(Spacer(1, 2*mm))
        lines = [f"  • {p['code']} — bought {p['first_buy'].strftime('%d %b %Y')}, "
                 f"{p['held_days']}d held, {365-p['held_days']}d until long-term"
                 for p in sorted(approaching, key=lambda x: x["held_days"], reverse=True)]
        story.append(Paragraph(
            "🕐 Positions approaching 12-month mark:<br/>" + "<br/>".join(lines),
            ParagraphStyle("Approach", parent=body, backColor=colors.HexColor("#e8f4ff"),
                           borderColor=C_ACCENT, borderWidth=1, borderPadding=6, leading=15)
        ))

    # ── NAV History ───────────────────────────────────────────────────────────
    section("Portfolio Value History (NAV)", "📈")
    nav_data = [["Period", "NAV (AUD)", "Change"]]
    prev = None
    for n in nav_series:
        chg = ""
        if prev is not None:
            delta = n["nav_aud"] - prev
            chg = f"{'↑' if delta>=0 else '↓'} AUD {abs(delta):,.0f} ({delta/prev*100:+.1f}%)"
        nav_data.append([n["period"], f"AUD {n['nav_aud']:,.2f}", chg])
        prev = n["nav_aud"]
    t = Table(nav_data, colWidths=[content_w*0.25, content_w*0.35, content_w*0.40])
    t.setStyle(table_style())
    story.append(t)

    # ── Open Positions ────────────────────────────────────────────────────────
    section("Current Open Positions", "📂")
    open_list = sorted(open_positions.values(), key=lambda x: x["qty"]*x["avg_price"], reverse=True)
    op_data = [["Symbol", "Name", "CCY", "Qty", "Avg Price", "Cost Basis", "First Buy", "Held", "Status"]]
    for p in open_list:
        cost = p["qty"] * p["avg_price"]
        status = "Long-Term ✅" if p["long_term"] else "Short-Term"
        op_data.append([
            p["code"], p["name"][:24], p["currency"],
            f"{p['qty']:,.0f}", money(p["avg_price"]), money(cost),
            p["first_buy"].strftime("%d %b %Y") if p["first_buy"] else "—",
            f"{p['held_days']}d", status,
        ])
    t = Table(op_data, colWidths=[content_w*v for v in [.07,.22,.05,.06,.09,.10,.11,.07,.10]])
    ts2 = table_style()
    # Colour long-term rows
    for i, p in enumerate(open_list, start=1):
        if p["long_term"]:
            ts2.add("BACKGROUND", (0,i), (-1,i), colors.HexColor("#dcffe8"))
    t.setStyle(ts2)
    story.append(t)

    # ── Closed Trades ─────────────────────────────────────────────────────────
    section("Closed Trade History (FIFO Matching)", "📋")
    ct_sorted = sorted(closed_trades, key=lambda x: x["sell_date"] or date.min, reverse=True)
    ct_data = [["Symbol", "CCY", "Qty", "Buy Price", "Sell Price", "P&L", "Buy Date", "Sell Date", "Held"]]
    for t2 in ct_sorted:
        pnl_str = money(t2["pnl"])
        ct_data.append([
            t2["code"], t2["currency"], f"{t2['qty']:,.0f}",
            money(t2["buy_price"]), money(t2["sell_price"]),
            pnl_str,
            t2["buy_date"].strftime("%d %b %Y") if t2["buy_date"] else "—",
            t2["sell_date"].strftime("%d %b %Y") if t2["sell_date"] else "—",
            f"{t2['held_days']}d",
        ])
    t = Table(ct_data, colWidths=[content_w*v for v in [.07,.05,.05,.09,.09,.10,.12,.12,.08]])
    ts3 = table_style()
    for i, tr in enumerate(ct_sorted, start=1):
        if tr["pnl"] > 0:
            ts3.add("TEXTCOLOR", (5, i), (5, i), C_GREEN)
            ts3.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
        elif tr["pnl"] < 0:
            ts3.add("TEXTCOLOR", (5, i), (5, i), C_RED)
            ts3.add("FONTNAME",  (5, i), (5, i), "Helvetica-Bold")
    t.setStyle(ts3)
    story.append(t)

    # ── Dividends ─────────────────────────────────────────────────────────────
    section("Dividends Received", "💵")
    div_data = [["Symbol", "Name", "CCY", "Gross", "Tax", "Net"]]
    for k, v in div_summary.items():
        div_data.append([k, v.get("name","")[:28], v["currency"],
                         money(v["gross"]), money(v["tax"]), money(v["net"])])
    t = Table(div_data, colWidths=[content_w*v for v in [.08,.30,.06,.14,.14,.14]])
    t.setStyle(table_style())
    story.append(t)

    # ── Interest ──────────────────────────────────────────────────────────────
    section("Interest Income Summary (Cash Plus Program)", "🏦")
    aud_yr = defaultdict(float)
    usd_yr = defaultdict(float)
    for i in interest:
        yr = i["date"].year
        if i["currency"] == "AUD": aud_yr[yr] += i["amount"]
        else: usd_yr[yr] += i["amount"]

    int_data = [["Year", "AUD Interest", "USD Interest", "Notes"]]
    for yr in sorted(set(list(aud_yr.keys()) + list(usd_yr.keys()))):
        int_data.append([str(yr), f"AUD {aud_yr[yr]:,.2f}", f"USD {usd_yr[yr]:,.2f}", "Cash Plus program"])
    int_data.append(["TOTAL", f"AUD {aud_interest:,.2f}", f"USD {usd_interest:,.2f}", ""])
    t = Table(int_data, colWidths=[content_w*v for v in [.12,.25,.25,.38]])
    ts4 = table_style()
    ts4.add("BACKGROUND", (0, len(int_data)-1), (-1, len(int_data)-1), C_PRIMARY)
    ts4.add("TEXTCOLOR",  (0, len(int_data)-1), (-1, len(int_data)-1), C_WHITE)
    ts4.add("FONTNAME",   (0, len(int_data)-1), (-1, len(int_data)-1), "Helvetica-Bold")
    t.setStyle(ts4)
    story.append(t)

    # ── Strategy ──────────────────────────────────────────────────────────────
    section("Trading Strategy Timeline", "🗓")
    strat_data = [
        ["Period", "Style", "Key Securities", "Notes"],
        ["Jan–Jun 2024", "Speculative", "AMC, SABR, TSLA, GOOGL",
         "Early stage. Small US positions. ~AUD 100K in Cash Plus earning interest."],
        ["Jul–Dec 2024", "Core US Tech","DELL, NVDA, MSFT, CRWD, PLTR, NTNX",
         "Built quality tech positions. Strong performers (CRWD +USD 344, PLTR +USD 1,270)."],
        ["Apr–Jun 2025", "ASX Active",  "WDS, ANZ, BRG, JHX, RWC, BEN, EVN, PME",
         "Short-term ASX trading, mostly days to weeks. Mixed P&L outcomes."],
        ["Jul–Nov 2025", "Leveraged ETFs","NVDU, SOXL, METU, OKLL, CWVX, CURE",
         "2-3× leveraged ETFs. High volatility, significant drawdown in Aug 2025."],
        ["Dec 2025–Feb 2026","Leveraged+ASX","NVDU, SOXL, METU, AAI, ALQ, MND",
         "Dual approach: leveraged US ETFs + ASX quality value stocks."],
    ]
    t = Table(strat_data, colWidths=[content_w*v for v in [.14,.14,.28,.44]])
    t.setStyle(table_style())
    story.append(t)

    # ── Disclaimer ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=C_BORDER))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "⚠️ Disclaimer: This report is auto-generated from Moomoo statement data for informational purposes only. "
        "P&L figures use FIFO matching and are approximate. This is not financial advice. "
        "Please consult a licensed financial adviser (AFSL holder) for investment or tax decisions. "
        "Moomoo Securities Australia Ltd (ACN 095 920 648) (AFSL No. 224663).",
        small
    ))

    doc.build(story)
    print(f"  [PDF] Saved → {PDF_OUTPUT}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Moomoo Portfolio Analyzer")
    print("=" * 60)
    print(f"\nReading statements from: {STATEMENT_DIR}\n")

    load_all()

    # Deduplicate transactions (Excel + PDF may overlap)
    global transactions
    transactions = dedupe_transactions(transactions)
    print(f"\n  Transactions loaded: {len(transactions)}")
    print(f"  Dividends loaded:    {len(dividends)}")
    print(f"  Interest records:    {len(interest)}")
    print(f"  Monthly NAV points:  {len(monthly_nav)}")

    # Analysis
    closed_trades, open_positions = build_trade_summary()
    nav_series    = build_nav_series()
    div_summary   = compute_dividend_summary()
    aud_int, usd_int = compute_interest_summary()
    deposits, withdrawals = compute_cash_deposits_withdrawals()

    print(f"\n  Closed trades:  {len(closed_trades)}")
    print(f"  Open positions: {len(open_positions)}")
    lt_pos = [p for p in open_positions.values() if p["long_term"]]
    print(f"  Long-term (>12mo open): {len(lt_pos)}")
    if lt_pos:
        for p in lt_pos:
            print(f"    → {p['code']} ({p['name']}) held {p['held_days']} days since {p['first_buy']}")
    else:
        print("    → None qualify as long-term as of today")

    # Generate HTML
    print("\n  Generating HTML report...")
    html = generate_html(closed_trades, open_positions, nav_series, div_summary,
                         aud_int, usd_int, deposits, withdrawals)
    with open(HTML_OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [HTML] Saved → {HTML_OUTPUT}")

    # Generate PDF
    print("  Generating PDF report...")
    generate_pdf(closed_trades, open_positions, nav_series, div_summary,
                 aud_int, usd_int, deposits, withdrawals)

    print(f"\n{'='*60}")
    print("  ✅ Done!")
    print(f"  HTML → {HTML_OUTPUT}")
    print(f"  PDF  → {PDF_OUTPUT}")
    print(f"{'='*60}\n")

    # Auto-open HTML in browser
    if not os.environ.get("NO_BROWSER_OPEN"):
        import webbrowser
        webbrowser.open(f"file:///{HTML_OUTPUT.replace(os.sep, '/')}")


if __name__ == "__main__":
    main()
