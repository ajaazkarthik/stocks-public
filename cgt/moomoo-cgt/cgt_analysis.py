#!/usr/bin/env python3
"""
Australian Capital Gains Tax (CGT) Analysis
============================================
Fetches all historical trades from your Moomoo account and produces a
per-Australian-Financial-Year CGT summary with FIFO cost-basis matching.

Australian FY:  1 July – 30 June
CGT discount:   50% off gains for assets held ≥ 12 months (individuals)
Capital losses: Offset gains (applied to short-term first, then long-term)

Usage:
    python cgt_analysis.py [--start YYYY-MM-DD]

Requirements:
    pip install moomoo-api pandas jinja2

NB: Trades denominated in foreign currencies (USD, HKD) are reported in
    their native currencies. To determine your AUD CGT liability you must
    convert using the AUD/USD (or AUD/HKD) rates on the trade date.
    AU-listed stocks are reported in AUD directly.
"""

import re
import sys
import os
import json
import argparse
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path
import pandas as pd

# Must be set before importing moomoo to avoid protobuf descriptor error
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

# ─── moomoo imports ──────────────────────────────────────────────────────────
from moomoo import (
    OpenSecTradeContext,
    TrdMarket,
    SecurityFirm,
    TrdEnv,
    RET_OK,
)

# ─── Config (defaults from moomoo_config; overridable via CLI args) ───────────
import sys as _sys
_THIS = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_THIS, "..", ".."))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
from moomoo_config import (
    OPEND_HOST as _DEF_HOST,
    OPEND_AU_PORT as _DEF_PORT,
    AU_FIRM as _DEF_FIRM,
    AU_ACCOUNT_ID as _DEF_ACCOUNT_ID,
)

ACCOUNT_ID   = _DEF_ACCOUNT_ID
HOST         = _DEF_HOST
PORT         = _DEF_PORT
FIRM         = _DEF_FIRM
TRD_ENV      = TrdEnv.REAL
FETCH_START  = "2018-01-01"   # adjust if your account is older

# ─── Australian FY helpers ────────────────────────────────────────────────────

def au_fy(d: date) -> str:
    """Return 'FY2024' label for a given date (FY ends 30 Jun)."""
    year = d.year if d.month >= 7 else d.year - 1
    return f"FY{year + 1}"


def fy_range(label: str):
    """Return (start_date, end_date) for a FY label like 'FY2024'."""
    year = int(label[2:])
    return date(year - 1, 7, 1), date(year, 6, 30)


# ─── FIFO matching engine ─────────────────────────────────────────────────────

class FifoLedger:
    """
    Maintains a per-ticker FIFO queue of buy lots.
    When a sell arrives, lots are consumed oldest-first and matched trades
    are emitted.
    """

    def __init__(self):
        # {code: [(buy_date, price, qty, currency), ...]}
        self._lots: dict[str, list] = defaultdict(list)
        self.matched: list[dict] = []
        self.warnings: list[str] = []

    def buy(self, code: str, dt: date, price: float, qty: float, currency: str):
        self._lots[code].append([dt, price, qty, currency])

    def sell(self, code: str, dt: date, price: float, qty: float, currency: str):
        remaining = qty
        lots = self._lots[code]

        if not lots:
            self.warnings.append(
                f"SELL {qty} {code} on {dt} but no BUY lots found – skipping."
            )
            return

        while remaining > 0 and lots:
            lot_dt, lot_price, lot_qty, lot_currency = lots[0]

            matched_qty = min(remaining, lot_qty)
            hold_days   = (dt - lot_dt).days
            long_term   = hold_days >= 365

            gross_gain  = (price - lot_price) * matched_qty
            # 50% CGT discount only applies to individuals *and* gains (not losses)
            if long_term and gross_gain > 0:
                taxable_gain = gross_gain * 0.5
                discount     = gross_gain * 0.5
            else:
                taxable_gain = gross_gain
                discount     = 0.0

            self.matched.append({
                "code":         code,
                "currency":     currency or lot_currency,
                "buy_date":     lot_dt,
                "sell_date":    dt,
                "hold_days":    hold_days,
                "term":         "Long" if long_term else "Short",
                "qty":          matched_qty,
                "buy_price":    lot_price,
                "sell_price":   price,
                "gross_gain":   round(gross_gain, 4),
                "cgt_discount": round(discount, 4),
                "taxable_gain": round(taxable_gain, 4),
                "fy":           au_fy(dt),
            })

            lots[0][2] -= matched_qty
            if lots[0][2] <= 1e-9:
                lots.pop(0)
            remaining -= matched_qty

        if remaining > 1e-9:
            self.warnings.append(
                f"SELL {qty} {code} on {dt}: still {remaining:.4f} units unmatched "
                f"(partial sale of a position opened before {FETCH_START}?)."
            )

    def open_lots(self) -> list[dict]:
        """Return all unmatched (still-held) buy lots."""
        result = []
        for code, lots in self._lots.items():
            for lot_dt, lot_price, lot_qty, currency in lots:
                result.append({
                    "code":      code,
                    "currency":  currency,
                    "buy_date":  lot_dt,
                    "qty":       lot_qty,
                    "buy_price": lot_price,
                    "hold_days": (date.today() - lot_dt).days,
                    "term":      "Long" if (date.today() - lot_dt).days >= 365 else "Short",
                })
        return result


# ─── API helpers ─────────────────────────────────────────────────────────────

def fetch_deals(start: str) -> pd.DataFrame:
    end = date.today().strftime("%Y-%m-%d")
    print(f"  Fetching history deals {start} to {end} ...")
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.NONE,
        host=HOST,
        port=PORT,
        security_firm=FIRM,
    )
    try:
        ret, data = ctx.history_deal_list_query(
            trd_env=TRD_ENV,
            acc_id=ACCOUNT_ID,
            start=start,
            end=end,
        )
        if ret != RET_OK:
            print(f"  ERROR: {data}")
            sys.exit(1)
        return data
    finally:
        ctx.close()


def fetch_positions() -> pd.DataFrame:
    print("  Fetching current open positions ...")
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.NONE,
        host=HOST,
        port=PORT,
        security_firm=FIRM,
    )
    try:
        ret, data = ctx.position_list_query(
            trd_env=TRD_ENV,
            acc_id=ACCOUNT_ID,
        )
        if ret != RET_OK:
            print(f"  ERROR: {data}")
            sys.exit(1)
        return data
    finally:
        ctx.close()


# ─── Helpers ──────────────────────────────────────────────────────────────────

# Moomoo market code → ISO currency
_CURRENCY_MAP = {
    "AU": "AUD",  # ASX
    "US": "USD",  # NYSE / NASDAQ / AMEX
    "HK": "HKD",  # HKEX
    "SG": "SGD",  # SGX
    "JP": "JPY",  # TSE / Osaka
    "KR": "KRW",  # KRX
    "UK": "GBP",  # LSE
    "CA": "CAD",  # TSX
    "CN": "CNH",  # Shanghai / Shenzhen (offshore)
}


def infer_currency(code: str) -> str:
    """Derive ISO currency from Moomoo stock code prefix (e.g. SG.* → SGD, JP.* → JPY)."""
    prefix = code.split(".")[0].upper() if "." in code else ""
    return _CURRENCY_MAP.get(prefix, "???")


# ─── Build CGT summary tables ─────────────────────────────────────────────────

def fy_summary(matched: list[dict]) -> pd.DataFrame:
    """Per-FY CGT summary grouped by currency."""
    rows = []
    df = pd.DataFrame(matched)
    if df.empty:
        return pd.DataFrame()

    for (fy, currency), grp in df.groupby(["fy", "currency"]):
        short  = grp[grp.term == "Short"]
        long_  = grp[grp.term == "Long"]

        st_gains  = short[short.gross_gain > 0].gross_gain.sum()
        st_losses = short[short.gross_gain < 0].gross_gain.sum()
        lt_gains  = long_[long_.gross_gain > 0].gross_gain.sum()
        lt_losses = long_[long_.gross_gain < 0].gross_gain.sum()
        discount  = long_[long_.gross_gain > 0].cgt_discount.sum()
        net_taxable = short.taxable_gain.sum() + long_.taxable_gain.sum()

        rows.append({
            "FY":              fy,
            "Currency":        currency,
            "ST Gains":        round(st_gains, 2),
            "ST Losses":       round(st_losses, 2),
            "LT Gross Gains":  round(lt_gains, 2),
            "LT Losses":       round(lt_losses, 2),
            "CGT Discount":    round(discount, 2),
            "Net Taxable":     round(net_taxable, 2),
            "# Trades":        len(grp),
        })

    return pd.DataFrame(rows).sort_values(["FY", "Currency"]).reset_index(drop=True)


# ─── HTML report ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_flag} Moomoo CGT Analysis – {account_label}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#0f1117; color:#e0e0e0; margin:0; padding:20px; }}
  h1   {{ color:#00d4ff; letter-spacing:1px; }}
  h2   {{ color:#7ecbf5; border-bottom:1px solid #333; padding-bottom:6px; }}
  h3   {{ color:#aad4f5; }}
  p.note {{ background:#1e2330; border-left:3px solid #f5a623; padding:10px 14px; font-size:0.88em; color:#ccc; }}
  .meta {{ font-size:0.9em; color:#9bb;  margin-bottom:16px; }}
  .meta span {{ margin-right:16px; }}
  .meta .badge {{ background:#1a2540; border:1px solid #2c3a55; border-radius:4px;
                  padding:2px 8px; font-size:0.82em; color:#7ecbf5; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:24px; font-size:0.86em; }}
  th {{ background:#1a2540; color:#7ecbf5; padding:8px 10px; text-align:left; border-bottom:2px solid #2c3a55; }}
  td {{ padding:7px 10px; border-bottom:1px solid #22293a; }}
  tr:hover td {{ background:#1a2236; }}
  .pos {{ color:#4caf50; }}
  .neg {{ color:#f44336; }}
  .disc {{ color:#f5a623; }}
  .dim  {{ color:#666; font-style:italic; }}
  .fy-section {{ background:#12181f; border-radius:8px; padding:16px 20px; margin-bottom:28px; border:1px solid #1c2540; }}
  details {{ margin-bottom:20px; }}
  .fy-summary {{ cursor:pointer; background:#1a2540; color:#7ecbf5; border-radius:6px; padding:10px 16px; list-style:none; font-size:0.92rem; font-weight:600; border:1px solid #2c3a55; }}
  .fy-summary:hover {{ background:#243050; }}
  .fy-summary::marker, .fy-summary::-webkit-details-marker {{ display:none; }}
  .fy-summary::before {{ content:"▶  "; font-size:0.8em; }}
  details[open] > .fy-summary::before {{ content:"▼  "; }}
  .summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }}
  .card {{ background:#1a2236; border-radius:6px; padding:12px 16px; }}
  .card .label {{ font-size:0.75em; color:#888; }}
  .card .value {{ font-size:1.3em; font-weight:bold; margin-top:4px; }}
  .warn {{ background:#2a1f10; border-left:3px solid #e07000; padding:6px 12px; font-size:0.82em; color:#e0c080; margin-bottom:4px; }}
</style>
</head>
<body>
<header>
<h1>{title_flag} Moomoo CGT Analysis — {account_label}</h1>
<p class="meta">
  <span>Generated: {generated}</span>
  <span class="badge">Account: {account_id}</span>
  <span class="badge">Firm: {firm_label}</span>
  <span class="badge">History from: {fetch_start}</span>
</p>

<p class="note">
  <strong>Disclaimer:</strong> Each market’s trades are reported in their <em>native currency</em>
  (SGD for SGX, JPY for TSE, USD for US markets, etc.).
  Foreign-currency gains must be converted to your home currency using the applicable exchange rate
  on each trade date for local tax purposes. Consult a registered tax agent for your CGT position.
  This tool uses <strong>FIFO</strong> cost-basis matching.
  The 50% CGT discount (Australian-account holders only) applies to gains held ≥ 12 months.
</p></header>
<main>
{warnings_html}

{kpi_cards}

<h2>Per-Financial-Year Summary</h2>
{fy_summary_table}

{current_fy_panel}

{aud_equivalent_section}

<details class="fy-section" style="border-color:#2d5a3d">
<summary style="cursor:pointer;color:#06d6a0;font-weight:600;font-size:1rem;padding:4px 0">&#x1F4B0; Open Positions (Unrealised CGT) &#x2014; click to expand</summary>
<div style="margin-top:12px">
<p class="note">These positions are not yet CGT events. They become taxable when sold.</p>
{open_positions_table}
</div>
</details>

{fy_sections}

</main>
</body>
</html>"""


def _fmt(val, decimals=2):
    val = round(float(val), decimals)  # avoid -0.00
    cls = "pos" if val > 0 else ("neg" if val < 0 else "")
    prefix = "+" if val > 0 else ""
    return f'<span class="{cls}">{prefix}{val:,.{decimals}f}</span>'


def _build_kpi_cards(matched: list[dict]) -> str:
    """Build 3 KPI summary cards: Total Realized, Net Taxable, Trade Split."""
    if not matched:
        return ""
    total_gain = sum(m.get("gross_gain", 0) for m in matched)
    net_taxable = sum(m.get("taxable_gain", 0) for m in matched)
    n_short = sum(1 for m in matched if m.get("term") == "Short")
    n_long = sum(1 for m in matched if m.get("term") == "Long")
    gain_color = "#4caf50" if total_gain >= 0 else "#f44336"
    tax_color = "#4caf50" if net_taxable >= 0 else "#f44336"
    sign_g = "+" if total_gain > 0 else ""
    sign_t = "+" if net_taxable > 0 else ""
    return f"""<div style="display:flex;gap:18px;margin:20px 0;flex-wrap:wrap">
  <div style="flex:1;min-width:200px;background:#1e293b;border-radius:10px;padding:16px 20px">
    <div style="color:#94a3b8;font-size:12px;text-transform:uppercase">Total Realized Gain / Loss</div>
    <div style="font-size:24px;font-weight:700;color:{gain_color}">{sign_g}{total_gain:,.2f}</div>
  </div>
  <div style="flex:1;min-width:200px;background:#1e293b;border-radius:10px;padding:16px 20px">
    <div style="color:#94a3b8;font-size:12px;text-transform:uppercase">Net Taxable Gain</div>
    <div style="font-size:24px;font-weight:700;color:{tax_color}">{sign_t}{net_taxable:,.2f}</div>
  </div>
  <div style="flex:1;min-width:200px;background:#1e293b;border-radius:10px;padding:16px 20px">
    <div style="color:#94a3b8;font-size:12px;text-transform:uppercase">Trade Split</div>
    <div style="font-size:24px;font-weight:700;color:#7ecbf5">{n_short} ST / {n_long} LT</div>
    <div style="color:#94a3b8;font-size:11px">{len(matched)} total trades</div>
  </div>
</div>"""


def df_to_html(df: pd.DataFrame, numeric_cols=None, discount_cols=None) -> str:
    if df.empty:
        return "<p><em>No data</em></p>"
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            val = row[col]
            if numeric_cols and col in numeric_cols:
                cells.append(f"<td>{_fmt(float(val))}</td>")
            elif discount_cols and col in discount_cols:
                cells.append(f'<td><span class="disc">{float(val):,.2f}</span></td>')
            else:
                cells.append(f"<td>{val}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    headers = "".join(f"<th>{c}</th>" for c in df.columns)
    return f"<table id='cgt-table' data-sortable><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def build_fy_sections(matched: list[dict]) -> str:
    if not matched:
        return ""
    df = pd.DataFrame(matched)
    sections = []
    for fy in sorted(df.fy.unique()):
        grp = df[df.fy == fy].copy()
        grp["buy_date"]  = grp["buy_date"].astype(str)
        grp["sell_date"] = grp["sell_date"].astype(str)
        display = grp[[
            "code","currency","term","buy_date","sell_date","hold_days",
            "qty","buy_price","sell_price","gross_gain","cgt_discount","taxable_gain"
        ]].rename(columns={
            "cgt_discount": "50% Discount",
            "taxable_gain": "Taxable Gain",
            "gross_gain":   "Gross Gain",
        })
        tbl = df_to_html(
            display,
            numeric_cols=["Gross Gain", "Taxable Gain"],
            discount_cols=["50% Discount"],
        )
        n_trades = len(grp)
        sections.append(
            f'<details class="fy-section">'
            f'<summary class="fy-summary" style="cursor:pointer;font-size:1rem;font-weight:600;'
            f'color:#7ecbf5;padding:4px 0">&#x1F4CB; {fy} \u2014 Trade Detail ({n_trades} trades) \u2014 click to expand</summary>'
            f'<div style="margin-top:10px">{tbl}</div>'
            f'</details>'
        )
    return "\n".join(sections)


_YFINANCE_SUFFIX = {
    "AU": ".AX", "SG": ".SI", "HK": ".HK", "JP": ".T",
    "KR": ".KS", "UK": ".L", "CA": ".TO",
}


def _moomoo_to_yfinance(code: str) -> str:
    """Convert Moomoo code (e.g. AU.MND, US.SOXL, SG.H12) to yfinance ticker."""
    if "." in code:
        pfx, sym = code.split(".", 1)
        return sym + _YFINANCE_SUFFIX.get(pfx.upper(), "")
    return code


_RE_SGX_WARRANT  = re.compile(r"^.+W\.SI$", re.IGNORECASE)   # e.g. Z4WW.SI, USAW.SI
_RE_US_OPTION    = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d+$")      # e.g. NEE260501C95000


def _is_unfetchable_yfticker(ticker: str) -> bool:
    """Return True for instrument types yfinance cannot fetch (warrants, options)."""
    return bool(_RE_SGX_WARRANT.match(ticker) or _RE_US_OPTION.match(ticker))


def _fetch_yfinance_prices(codes: list[str]) -> dict[str, float]:
    """Fetch last-close prices from yfinance for a list of moomoo codes.
    Returns {moomoo_code: price}. Silent on errors — caller uses 0 as fallback.
    SGX warrants (*W.SI) and US options (e.g. NEE260501C95000) are skipped
    silently — yfinance cannot price these instruments."""
    if not codes:
        return {}
    code_to_yticker = {
        c: _moomoo_to_yfinance(c) for c in codes
        if not _is_unfetchable_yfticker(_moomoo_to_yfinance(c))
    }
    if not code_to_yticker:
        return {}
    try:
        import yfinance as yf
        tickers = list(set(code_to_yticker.values()))
        raw = yf.download(tickers, period="5d", progress=False, auto_adjust=True, threads=False)
        if raw.empty:
            return {}
        close = raw["Close"] if "Close" in raw.columns else raw
        result: dict[str, float] = {}
        for mcode, yticker in code_to_yticker.items():
            try:
                series = close[yticker] if hasattr(close, "columns") and yticker in close.columns else close
                val = float(series.dropna().iloc[-1])
                if val > 0:
                    result[mcode] = val
            except Exception:
                pass
        return result
    except Exception:
        return {}


def build_open_positions_table(positions_df: pd.DataFrame, open_lots: list[dict]) -> str:
    if positions_df is None or positions_df.empty:
        return "<p><em>No open positions</em></p>"

    # Index open lots by code for quick lookup
    lots_by_code: dict[str, list] = defaultdict(list)
    for lot in open_lots:
        lots_by_code[lot["code"]].append(lot)

    # Pre-fetch yfinance prices for positions where Moomoo returns 0
    zero_price_codes = [
        str(row["code"]) for _, row in positions_df.iterrows()
        if float(row.get("last_price", 0) or 0) == 0
    ]
    yfin_prices = _fetch_yfinance_prices(zero_price_codes)

    rows_out = []
    for _, pos in positions_df.iterrows():
        code = pos["code"]
        # Fix currency: always infer from code prefix (API returns wrong values like MYR for AU)
        currency = infer_currency(code)

        last_price  = float(pos.get("last_price", 0) or 0)
        if last_price == 0 and code in yfin_prices:
            last_price = yfin_prices[code]
        api_upl     = float(pos.get("unrealized_pl", 0) or 0)

        # Compute hold_days + ST/LT unrealized P&L from FIFO lots
        lots = lots_by_code.get(code, [])
        if lots:
            earliest_buy = min(lot["buy_date"] for lot in lots)
            max_hold     = max(lot["hold_days"] for lot in lots)
            if last_price > 0:
                # Market open: compute directly from FIFO lots
                st_upl = sum(
                    (last_price - lot["buy_price"]) * lot["qty"]
                    for lot in lots if lot["hold_days"] < 365
                )
                lt_upl = sum(
                    (last_price - lot["buy_price"]) * lot["qty"]
                    for lot in lots if lot["hold_days"] >= 365
                )
            else:
                # Market closed: use API total, split proportionally by ST/LT qty
                total_qty = sum(lot["qty"] for lot in lots) or 1
                st_qty    = sum(lot["qty"] for lot in lots if lot["hold_days"] < 365)
                lt_qty    = sum(lot["qty"] for lot in lots if lot["hold_days"] >= 365)
                st_upl    = api_upl * (st_qty / total_qty)
                lt_upl    = api_upl * (lt_qty / total_qty)
        else:
            # No FIFO data – fall back to API total
            earliest_buy = "—"
            max_hold     = "—"
            st_upl       = api_upl
            lt_upl       = 0.0

        last_price_display = f"{last_price:,.4g}" if last_price > 0 else "—"
        avg_cost_raw = float(pos.get("average_cost", 0) or 0)
        rows_out.append({
            "code":          code,
            "name":          pos.get("stock_name", ""),
            "qty":           pos.get("qty", ""),
            "avg_cost":      f"{avg_cost_raw:,.4g}" if avg_cost_raw else "—",
            "last_price":    last_price_display,
            "ccy":           currency,
            "earliest_buy":  str(earliest_buy),
            "hold_days":     max_hold if max_hold != "—" else "—",
            "term":          "Long" if isinstance(max_hold, int) and max_hold >= 365 else "Short",
            "ST Unrealized": round(st_upl, 2),
            "LT Unrealized": round(lt_upl, 2),
            "Total Unrealized": round(st_upl + lt_upl, 2),
        })

    df_out = pd.DataFrame(rows_out)
    if df_out.empty:
        return ("<details><summary class=\"fy-summary\">&#x1F4CB; Open Positions (Unrealised CGT) "
                "\u2014 no open positions</summary><p><em>No open positions</em></p></details>")
    n_hold     = len(df_out)
    ccy_totals = df_out.groupby('ccy')['Total Unrealized'].sum()
    total_parts = [f"{ccy}: {'+' if v>=0 else ''}{v:,.2f}" for ccy, v in ccy_totals.items()]
    total_str   = " | ".join(total_parts) if total_parts else "\u2014"
    table_html  = df_to_html(df_out, numeric_cols=["ST Unrealized", "LT Unrealized", "Total Unrealized"])
    return (
        f'<details><summary class="fy-summary">&#x1F4CB; Open Positions (Unrealised CGT)'
        f' \u2014 {n_hold} holding{"s" if n_hold != 1 else ""} | Unrealized: {total_str}</summary>'
        f'<p class="note" style="margin-top:12px">These positions have unrealised gains/losses. '
        f'They become CGT events when you sell.</p>'
        f'{table_html}</details>'
    )


# ─── FX Rate + new CGT panels ──────────────────────────────────────────────────

def _fetch_fx_rate(
    from_ccy: str,
    to_ccy: str,
    trade_date: "str | None" = None,
) -> "float | None":
    """Fetch FX rate (from_ccy → to_ccy) via yfinance.

    BUG-NEW-2 fix (2026-05-01): ATO requires the FX rate as of the
    trade settlement date, NOT today's live rate.  When ``trade_date``
    is supplied (ISO format "YYYY-MM-DD") the function fetches the
    historical closing rate for that date and caches the result in
    ``database/fx_rate_cache.json`` to avoid repeated network calls.
    When ``trade_date`` is ``None`` the live fast_info rate is returned
    (acceptable only for display/summary sections that clearly label
    themselves "live rate").

    Returns None on failure; callers should fall back gracefully.
    """
    try:
        import yfinance as yf  # optional dep; imported locally
        ticker = f"{from_ccy}{to_ccy}=X"

        if trade_date:
            # ── Historical rate for ATO-compliant CGT ──────────────────────
            _cache_path = (
                Path(__file__).resolve().parent.parent.parent
                / "database" / "fx_rate_cache.json"
            )
            _cache_key = f"{from_ccy}_{to_ccy}_{trade_date}"
            # Try on-disk cache first
            if _cache_path.exists():
                try:
                    with open(_cache_path, "r", encoding="utf-8") as _fh:
                        _cache = json.load(_fh)
                except Exception:
                    _cache = {}
            else:
                _cache = {}

            if _cache_key in _cache:
                return float(_cache[_cache_key])

            # Fetch from yfinance — download 1-week window to handle weekends/holidays
            from datetime import datetime as _dt, timedelta as _td
            _start = _dt.strptime(trade_date, "%Y-%m-%d").date()
            _end   = _start + _td(days=5)
            df = yf.download(
                ticker,
                start=str(_start),
                end=str(_end),
                progress=False,
                auto_adjust=True,
            )
            if df.empty:
                # Fallback: live rate (acceptable for very old dates)
                info = yf.Ticker(ticker).fast_info
                rate = getattr(info, "last_price", None)
            else:
                # Use the closest available closing price
                col = "Close" if "Close" in df.columns else df.columns[0]
                rate = float(df[col].dropna().iloc[0])

            if rate and float(rate) > 0:
                _cache[_cache_key] = round(float(rate), 8)
                try:
                    _cache_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(_cache_path, "w", encoding="utf-8") as _fh:
                        json.dump(_cache, _fh, indent=2)
                except Exception:
                    pass
                return float(rate)
            return None

        # ── Live rate (display/summary use only) ───────────────────────────
        info = yf.Ticker(ticker).fast_info
        rate = getattr(info, "last_price", None)
        if rate and float(rate) > 0:
            return float(rate)
    except Exception:
        pass
    return None


def build_aud_equivalent_section(fy_sum_df: pd.DataFrame) -> str:
    """Shows USD/SGD/HKD FY totals converted to AUD at live exchange rates."""
    if fy_sum_df.empty:
        return ""
    foreign_ccys = [c for c in fy_sum_df["Currency"].unique() if c != "AUD"]
    if not foreign_ccys:
        return ""
    rates: dict[str, float] = {}
    for ccy in foreign_ccys:
        r = _fetch_fx_rate(ccy, "AUD")
        if r:
            rates[ccy] = r
    if not rates:
        return "<p class='note'>&#x26A0; Could not fetch live FX rates for AUD conversion.</p>"
    rows = []
    for _, row in fy_sum_df.iterrows():
        ccy = row["Currency"]
        if ccy not in rates:
            continue
        rate = rates[ccy]
        rows.append({
            "FY":                   row["FY"],
            "Currency":             ccy,
            "Rate (to AUD)":        round(rate, 5),
            "Net Taxable (orig)":   row["Net Taxable"],
            "Net Taxable (AUD)":    round(float(row["Net Taxable"]) * rate, 2),
            "ST Gains (AUD)":       round(float(row["ST Gains"]) * rate, 2),
            "LT Gross Gains (AUD)": round(float(row["LT Gross Gains"]) * rate, 2),
        })
    if not rows:
        return ""
    df = pd.DataFrame(rows)
    table_html = df_to_html(
        df,
        numeric_cols=["Net Taxable (orig)", "Net Taxable (AUD)", "ST Gains (AUD)", "LT Gross Gains (AUD)"],
    )
    rates_str = " | ".join(f"1 {c} = {r:.4f} AUD" for c, r in rates.items())
    return (
        f'<details class="fy-section" style="border-color:#3d5a2d" open>'
        f'<summary style="cursor:pointer;color:#adff2f;font-weight:600;font-size:1rem;padding:4px 0">'
        f'&#x1F4B1; AUD-Equivalent Summary (live rate) &#x2014; click to collapse</summary>'
        f'<div style="margin-top:12px">'
        f'<p class="note">Live rates: {rates_str} &middot; Fetched from Yahoo Finance at report generation time.</p>'
        f'{table_html}</div></details>'
    )


def build_current_fy_panel(
    matched: list[dict],
    positions_df: pd.DataFrame,
    open_lots: list[dict],
) -> str:
    """Shows current-FY realized (closed trades) + unrealized (open positions) P&L."""
    current_fy = au_fy(date.today())
    realized_rows = [m for m in matched if m.get("fy") == current_fy]
    realized_by_ccy: dict[str, float] = {}
    for m in realized_rows:
        ccy = m.get("currency", "???")
        realized_by_ccy[ccy] = realized_by_ccy.get(ccy, 0.0) + float(m.get("taxable_gain", 0))
    unrealized_by_ccy: dict[str, float] = {}
    if positions_df is not None and not positions_df.empty:
        lots_by_code: dict[str, list] = defaultdict(list)
        for lot in (open_lots or []):
            lots_by_code[lot["code"]].append(lot)
        for _, pos in positions_df.iterrows():
            code     = pos["code"]
            currency = infer_currency(code)
            last_px  = float(pos.get("last_price", 0) or 0)
            api_upl  = float(pos.get("unrealized_pl", 0) or 0)
            lots     = lots_by_code.get(code, [])
            if lots and last_px > 0:
                upl = sum((last_px - lot["buy_price"]) * lot["qty"] for lot in lots)
            else:
                upl = api_upl
            unrealized_by_ccy[currency] = unrealized_by_ccy.get(currency, 0.0) + upl
    all_ccys = sorted(set(list(realized_by_ccy) + list(unrealized_by_ccy)))
    if not all_ccys:
        return ""
    rows = []
    for ccy in all_ccys:
        real   = realized_by_ccy.get(ccy, 0.0)
        unreal = unrealized_by_ccy.get(ccy, 0.0)
        rows.append({
            "Currency":           ccy,
            "Realized (Taxable)": round(real, 2),
            "Unrealized":         round(unreal, 2),
            "Combined":           round(real + unreal, 2),
        })
    df = pd.DataFrame(rows)
    table_html = df_to_html(df, numeric_cols=["Realized (Taxable)", "Unrealized", "Combined"])
    n_realized = len(realized_rows)
    n_open = len(positions_df) if positions_df is not None and not positions_df.empty else 0
    return (
        f'<details class="fy-section" style="border-color:#5a3d2d" open>'
        f'<summary style="cursor:pointer;color:#ffd700;font-weight:600;font-size:1rem;padding:4px 0">'
        f'&#x1F4C5; {current_fy} P&amp;L Snapshot ({n_realized} realized trades + {n_open} open) &#x2014; click to collapse</summary>'
        f'<div style="margin-top:12px">'
        f'<p class="note">Realized = taxable gain on closed trades in {current_fy}. '
        f'Unrealized = current mark-to-market P&amp;L on open positions (not a CGT event until sold).</p>'
        f'{table_html}</div></details>'
    )


# Moomoo firm → description used in the report header
_FIRM_LABEL = {
    "FUTUAU":         "🇦🇺 Moomoo AU  (FUTUAU)",
    "FUTUSG":         "🇸🇬 Moomoo SG  (FUTUSG)",
    "FUTUSECURITIES": "🇭🇰 Moomoo HK  (FUTUSECURITIES)",
    "FUTUINC":        "🇺🇸 Moomoo US  (FUTUINC)",
}


def generate_html(
    fy_sum_df: pd.DataFrame,
    matched: list[dict],
    positions_df: pd.DataFrame,
    open_lots: list[dict],
    warnings: list[str],
    fetch_start: str,
    *,
    account_label: str = "AU",
    firm_label: str = "FUTUAU",
) -> str:
    # Derive flag emoji + readable label for the header/title
    _flag_map = {
        "AU": "🇦🇺", "SG": "🇸🇬", "HK": "🇭🇰", "US": "🇺🇸",
        "AU + SG": "🇦🇺🇸🇬", "SG + AU": "🇸🇬🇦🇺",
    }
    title_flag = _flag_map.get(account_label.split()[0].rstrip("+").strip(), "🌐")
    firm_display = _FIRM_LABEL.get(firm_label.split()[0], firm_label)  # first token if combined

    warnings_html = ""
    if warnings:
        items = "\n".join(f'<div class="warn">⚠ {w}</div>' for w in warnings)
        warnings_html = f"<h2>Warnings</h2>{items}"

    # FY summary table – colour numeric cols
    fy_summary_html = df_to_html(
        fy_sum_df,
        numeric_cols=["ST Gains", "ST Losses", "LT Gross Gains", "LT Losses", "Net Taxable"],
        discount_cols=["CGT Discount"],
    ) if not fy_sum_df.empty else "<p><em>No closed trades found.</em></p>"

    html = HTML_TEMPLATE.format(
        title_flag=title_flag,
        account_label=f"Moomoo {account_label}",
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        account_id=ACCOUNT_ID,
        firm_label=firm_display,
        fetch_start=fetch_start,
        warnings_html=warnings_html,
        kpi_cards=_build_kpi_cards(matched),
        fy_summary_table=fy_summary_html,
        fy_sections=build_fy_sections(matched),
        open_positions_table=build_open_positions_table(positions_df, open_lots),
        current_fy_panel=build_current_fy_panel(matched, positions_df, open_lots),
        aud_equivalent_section=build_aud_equivalent_section(fy_sum_df),
    )
    from html_utils import JS_TABLE_SORT, JS_CSV_EXPORT  # C-6, C-11
    csv_btn = '<div data-no-print style="text-align:right;margin:12px 0"><button onclick="exportTableCSV(\'cgt-table\',\'moomoo_cgt.csv\')" style="padding:6px 14px;cursor:pointer">Export CSV</button></div>'
    return html.replace("</main>", f"{csv_btn}<script>{JS_CSV_EXPORT}\n{JS_TABLE_SORT}</script>\n</main>")


# ─── Public data-extraction API (used by combined_cgt_report.py) ────────────

def get_data(start: str = FETCH_START, *, port: int = None, firm=None, acc_id: int = None) -> dict:
    """
    Run the full moomoo CGT analysis and return results as a dict.
    Called by combined_cgt_report.py — does not write any files.
    Requires OpenD to be running.

    Optional keyword args override the module-level account config temporarily:
      port    — OpenD port (e.g. 11111 for AU, 11112 for SG)
      firm    — SecurityFirm instance (e.g. SecurityFirm.FUTUSG)
      acc_id  — Moomoo account ID integer
    """
    global PORT, FIRM, ACCOUNT_ID
    _orig_port, _orig_firm, _orig_acc = PORT, FIRM, ACCOUNT_ID
    try:
        if port    is not None: PORT       = port
        if firm    is not None: FIRM       = firm
        if acc_id  is not None: ACCOUNT_ID = acc_id
        deals_df     = fetch_deals(start)
        positions_df = fetch_positions()

        if deals_df is None or deals_df.empty:
            return {
                "matched": [], "open_lots": [], "fy_sum_df": pd.DataFrame(),
                "positions_df": pd.DataFrame(), "warnings": [],
                "trades_df": pd.DataFrame(), "fetch_start": start,
            }

        deals_df = deals_df.copy()
        deals_df.columns = [c.lower() for c in deals_df.columns]
        deals_df["dt"]       = pd.to_datetime(deals_df["create_time"]).dt.date
        deals_df["side"]     = deals_df["trd_side"].astype(str).str.upper().str.split(".").str[-1]
        deals_df["price"]    = pd.to_numeric(deals_df["price"], errors="coerce")
        deals_df["qty"]      = pd.to_numeric(deals_df["qty"],   errors="coerce")
        deals_df["currency"] = deals_df["code"].apply(infer_currency)
        deals_df = deals_df.sort_values("create_time").reset_index(drop=True)

        ledger = FifoLedger()
        for _, row in deals_df.iterrows():
            if row["side"] == "BUY":
                ledger.buy(row["code"], row["dt"], row["price"], row["qty"], row["currency"])
            elif row["side"] == "SELL":
                ledger.sell(row["code"], row["dt"], row["price"], row["qty"], row["currency"])

        return {
            "matched":      ledger.matched,
            "open_lots":    ledger.open_lots(),
            "fy_sum_df":    fy_summary(ledger.matched),
            "positions_df": positions_df,
            "warnings":     ledger.warnings,
            "trades_df":    deals_df,
            "fetch_start":  start,
        }
    finally:
        PORT, FIRM, ACCOUNT_ID = _orig_port, _orig_firm, _orig_acc


# ─── Main ────────────────────────────────────────────────────────────────────

_FIRM_MAP = {
    "AU": SecurityFirm.FUTUAU,
    "SG": SecurityFirm.FUTUSG,
    "HK": SecurityFirm.FUTUSECURITIES,
    "US": SecurityFirm.FUTUINC,
}


def main():
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # cp1252 fix
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description="Moomoo CGT analysis (AU/SG accounts)")
    parser.add_argument("--start", default=FETCH_START,
                        help=f"Earliest trade date to fetch (default: {FETCH_START})")
    parser.add_argument("--out", default="cgt_report.html",
                        help="Output HTML file (default: cgt_report.html)")
    parser.add_argument("--port", type=int, default=None,
                        help="OpenD port (default: AU=11111, SG=11112 per moomoo_config)")
    parser.add_argument("--firm", default=None, choices=list(_FIRM_MAP),
                        help="Account region/firm: AU (default), SG, HK, US")
    parser.add_argument("--acc-id", type=int, default=None,
                        help="Override account ID (default from moomoo_config)")
    args = parser.parse_args()

    # Apply CLI overrides to module-level config used by fetch_* functions
    global HOST, PORT, FIRM, ACCOUNT_ID
    if args.firm:
        FIRM = _FIRM_MAP[args.firm]
        # Auto-select port based on firm unless --port is also given
        if args.firm == "SG" and args.port is None:
            from moomoo_config import OPEND_SG_PORT
            PORT = OPEND_SG_PORT
    if args.port is not None:
        PORT = args.port
    if args.acc_id is not None:
        ACCOUNT_ID = args.acc_id

    print("=" * 60)
    print("Australian CGT Analysis — Moomoo")
    print("=" * 60)

    # ── Fetch data ──────────────────────────────────────────────
    deals_df   = fetch_deals(args.start)
    positions_df = fetch_positions()

    if deals_df is None or deals_df.empty:
        print("No historical deals returned. Nothing to analyse.")
        sys.exit(0)

    print(f"  Got {len(deals_df)} deal rows.")

    # ── Normalise deal rows ──────────────────────────────────────
    # Expected columns: deal_id, order_id, code, trd_side, price, qty, create_time
    # trd_side may be string "BUY"/"SELL" or enum repr
    deals_df = deals_df.copy()
    deals_df.columns = [c.lower() for c in deals_df.columns]

    # Parse create_time to date
    deals_df["dt"] = pd.to_datetime(deals_df["create_time"]).dt.date

    # Normalise trd_side to uppercase string
    deals_df["side"] = deals_df["trd_side"].astype(str).str.upper()
    # Handles "TrdSide.BUY" style repr
    deals_df["side"] = deals_df["side"].str.split(".").str[-1]

    # Ensure price and qty are numeric
    deals_df["price"] = pd.to_numeric(deals_df["price"], errors="coerce")
    deals_df["qty"]   = pd.to_numeric(deals_df["qty"],   errors="coerce")

    # Currency: derive from market prefix (AU.* = AUD, US.* = USD, HK.* = HKD)
    deals_df["currency"] = deals_df["code"].apply(infer_currency)

    # Sort chronologically so FIFO processes oldest first
    deals_df = deals_df.sort_values("create_time").reset_index(drop=True)

    print(f"  Date range: {deals_df['dt'].min()} to {deals_df['dt'].max()}")
    print(f"  Tickers involved: {sorted(deals_df['code'].unique())}")

    # ── FIFO matching ────────────────────────────────────────────
    ledger = FifoLedger()
    for _, row in deals_df.iterrows():
        if row["side"] == "BUY":
            ledger.buy(row["code"], row["dt"], row["price"], row["qty"], row["currency"])
        elif row["side"] == "SELL":
            ledger.sell(row["code"], row["dt"], row["price"], row["qty"], row["currency"])
        else:
            ledger.warnings.append(f"Unknown trd_side '{row['trd_side']}' for {row['code']} on {row['dt']} – skipped.")

    matched  = ledger.matched
    warnings = ledger.warnings

    print(f"  Matched {len(matched)} sell events across CGT history.")
    if warnings:
        print(f"  {len(warnings)} warnings (see report).")

    # ── FY summary ───────────────────────────────────────────────
    fy_sum_df = fy_summary(matched)

    if not fy_sum_df.empty:
        print("\n  FY Summary:  (ST = Short Term < 12 months | LT = Long Term \u2265 12 months, 50% CGT discount eligible)")
        print(fy_sum_df.to_string(index=False))
    else:
        print("  No closed (sell) trades found — CGT summary is empty.")

    # ── Open lots (still held) ────────────────────────────────────
    open_lots = ledger.open_lots()
    if open_lots:
        print(f"\n  {len(open_lots)} open FIFO buy-lots remaining (unrealised CGT).")

    # ── Generate HTML ────────────────────────────────────────────
    # Determine account_label and firm display from CLI args
    _acct_label  = args.firm or "AU"
    _firm_label  = str(FIRM).split(".")[-1]  # e.g. "FUTUSG" from SecurityFirm.FUTUSG
    html = generate_html(
        fy_sum_df, matched, positions_df, open_lots, warnings, args.start,
        account_label=_acct_label, firm_label=_firm_label,
    )
    out_path = os.path.join(os.path.dirname(__file__), args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  Report written: {out_path}")

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        _rdb_dir = str(Path(__file__).parent.parent.parent / "reports")
        _sys.path.insert(0, _rdb_dir)
        from report_db import save_cgt_snapshot, archive_report as _arc
        broker_label = f"moomoo_{(args.firm or 'AU').lower()}"
        save_cgt_snapshot(fy_sum_df, broker_label)
        _arc(Path(out_path))
    except Exception as _e:
        print(f"  [report_db] cgt_analysis snapshot skipped: {_e}")

    print("=" * 60)


if __name__ == "__main__":
    main()
