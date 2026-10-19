#!/usr/bin/env python3
"""
Combined CGT Report — IBKR + Moomoo + India
=============================================
Generates three HTML reports from a single run:

  1.  ibkr_cgt_report.html     — IBKR only   (existing generator)
  2.  moomoo_cgt_report.html   — Moomoo only  (existing generator)
  3.  combined_cgt_report.html — Both brokers merged

Usage:
    # Default: IBKR credentials are built-in (MyFlexQuery), Moomoo via OpenD
    python combined_cgt_report.py

    # Custom Flex Query credentials
    python combined_cgt_report.py \\
        --ibkr-flex-token TOKEN --ibkr-flex-query-id 123456

    # With Moomoo + IBKR manual CSV
    python combined_cgt_report.py --ibkr-statement activity.csv

    # Skip Moomoo (OpenD not running) – IBKR only + combined
    python combined_cgt_report.py --no-moomoo

    # Skip IBKR – Moomoo only + combined
    python combined_cgt_report.py --no-ibkr

    # Specify output directory
    python combined_cgt_report.py --out-dir reports/
"""

import os
import sys
import pathlib
import argparse
import importlib.util
from datetime import datetime, date
from collections import defaultdict

import pandas as pd

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Load broker modules ─────────────────────────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))
INDIA_BROKERS_DIR = os.path.join(os.path.dirname(HERE), "india-brokers")


def _load_module(name: str, rel_path: str):
    path = os.path.join(HERE, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── Combined FY summary ─────────────────────────────────────────────────────

def combine_fy_summaries(
    ibkr_df: pd.DataFrame,
    moomoo_df: pd.DataFrame,
    webull_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Sum all brokers' per-FY CGT data into a single combined table."""
    frames = []
    if ibkr_df is not None and not ibkr_df.empty:
        frames.append(ibkr_df.copy())
    if moomoo_df is not None and not moomoo_df.empty:
        frames.append(moomoo_df.copy())
    if webull_df is not None and not webull_df.empty:
        frames.append(webull_df.copy())
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    num_cols = ["ST Gains", "ST Losses", "LT Gross Gains", "LT Losses",
                "CGT Discount", "Net Taxable", "# Trades"]
    agg = (combined
           .groupby(["FY", "Currency"])[num_cols]
           .sum()
           .reset_index()
           .sort_values(["FY", "Currency"])
           .reset_index(drop=True))
    return agg


# ─── Combined matched trades ──────────────────────────────────────────────────

def combine_matched_trades(
    ibkr_matched: list,
    moomoo_matched: list,
    webull_matched: list | None = None,
) -> list:
    """Merge all broker trade lists, adding a 'broker' column."""
    rows = []
    for t in ibkr_matched:
        rows.append({**t, "broker": "IBKR"})
    for t in moomoo_matched:
        rows.append({**t, "broker": "Moomoo"})
    for t in (webull_matched or []):
        rows.append({**t, "broker": "Webull"})
    return rows


# ─── India positions ─────────────────────────────────────────────────────────

def build_india_positions_html(india_data: dict) -> str:
    """
    Build an HTML table for India (Zerodha + Angel One) holdings.
    Returns empty string if no positions.
    """
    positions = india_data.get("positions", [])
    if not positions:
        return ""
    rows_html = []
    for p in sorted(positions, key=lambda x: x["symbol"]):
        avg  = float(p.get("avg_cost", 0))
        ltp  = float(p.get("last_price", 0))
        pnl  = float(p.get("unrealized_pnl", 0))
        qty  = int(p.get("qty", 0))
        pct  = ((ltp - avg) / avg * 100) if avg else 0.0
        pct_sign  = "+" if pct  >= 0 else ""
        pnl_sign  = "+" if pnl  >= 0 else ""
        pct_cls   = "pos" if pct  >= 0 else "neg"
        pnl_cls   = "pos" if pnl  >= 0 else "neg"
        broker_badge = (
            '<span class="badge badge-zerodha">Zerodha</span>'
            if p.get("broker") == "Zerodha"
            else '<span class="badge badge-angelone">AngelOne</span>'
        )
        rows_html.append(
            f"<tr>"
            f"<td>{broker_badge}</td>"
            f"<td>{p['symbol']}</td>"
            f"<td>{qty}</td>"
            f"<td>₹{avg:,.2f}</td>"
            f"<td>₹{ltp:,.2f}</td>"
            f'<td><span class="{pct_cls}">{pct_sign}{pct:.1f}%</span></td>'
            f'<td><span class="{pnl_cls}">{pnl_sign}₹{abs(pnl):,.0f}</span></td>'
            f"</tr>"
        )
    header = (
        "<table><thead><tr>"
        "<th>Broker</th><th>Symbol</th><th>Qty</th>"
        "<th>Avg Cost</th><th>LTP</th><th>Change %</th><th>Unrealised P&amp;L</th>"
        "</tr></thead><tbody>"
    )
    footer = (
        f"</tbody></table>"
        f'<p style="font-size:0.84em;color:#aaa;">'
        f"Zerodha: {india_data.get('zerodha_count',0)} &nbsp;|"
        f" AngelOne: {india_data.get('angelone_count',0)} &nbsp;|"
        f" Total value: ₹{india_data.get('total_value',0):,.0f} &nbsp;|"
        f" Total P&amp;L: ₹{india_data.get('total_pnl',0):+,.0f}"
        f"</p>"
    )
    return header + "\n".join(rows_html) + footer


# ─── Combined open positions ──────────────────────────────────────────────────

def _infer_currency(code: str) -> str:
    prefix = code.split(".")[0].upper() if "." in code else ""
    return {
        "AU": "AUD", "US": "USD", "HK": "HKD",
        "SG": "SGD", "JP": "JPY", "KR": "KRW",
        "UK": "GBP", "CA": "CAD", "CN": "CNH",
    }.get(prefix, "???")


def build_combined_positions_rows(ibkr_data: dict, moomoo_data: dict) -> list[dict]:
    """
    Build a flat list of open position rows from both brokers for the combined table.
    Each row has a 'broker' field set to 'IBKR' or 'Moomoo'.
    Mirrors the per-broker open-position logic but labels each row.
    """
    today = date.today()
    rows  = []

    # ── IBKR positions ────────────────────────────────────────────────────────
    ibkr_lots_by_code: dict[str, list] = defaultdict(list)
    for lot in ibkr_data.get("open_lots", []):
        ibkr_lots_by_code[lot["code"]].append(lot)

    live_portfolio = ibkr_data.get("live_portfolio", {})
    stmt_positions = ibkr_data.get("statement_positions", pd.DataFrame())

    if live_portfolio:
        ibkr_pos_codes = sorted(live_portfolio)
        def _ibkr_pos(code):
            lp = live_portfolio[code]
            return lp["currency"], float(lp["position"]), float(lp["market_price"]), \
                   float(lp.get("unrealized_pnl", 0)), float(lp.get("avg_cost", 0))
    elif stmt_positions is not None and not stmt_positions.empty:
        stmt_by_code   = {r["code"]: r for _, r in stmt_positions.iterrows()}
        ibkr_pos_codes = sorted(stmt_by_code)
        def _ibkr_pos(code):
            lp = stmt_by_code[code]
            return (str(lp.get("currency", "USD")), float(lp.get("qty", 0)),
                    float(lp.get("last_price", 0) or 0),
                    float(lp.get("unrealized_pnl", 0) or 0),
                    float(lp.get("avg_cost", 0) or 0))
    else:
        # FIFO only
        ibkr_agg: dict[str, dict] = {}
        for lot in ibkr_data.get("open_lots", []):
            c = lot["code"]
            if c not in ibkr_agg:
                ibkr_agg[c] = {"currency": lot["currency"], "lots": []}
            ibkr_agg[c]["lots"].append(lot)
        ibkr_pos_codes = sorted(ibkr_agg)
        def _ibkr_pos(code):
            lots = ibkr_agg[code]["lots"]
            qty  = sum(l["qty"] for l in lots)
            avg  = sum(l["buy_price"] * l["qty"] for l in lots) / (qty or 1)
            return ibkr_agg[code]["currency"], qty, avg, 0.0, avg

    for code in ibkr_pos_codes:
        currency, qty, last_price, api_upl, pos_avg_cost = _ibkr_pos(code)
        lots = ibkr_lots_by_code.get(code, [])
        if lots:
            earliest_buy = min(l["buy_date"] for l in lots)
            max_hold     = max(l["hold_days"] for l in lots)
            term         = "Long" if max_hold >= 365 else "Short"
            lot_qty      = sum(l["qty"] for l in lots)
            avg_cost     = sum(l["buy_price"] * l["qty"] for l in lots) / (lot_qty or 1)
            if last_price > 0:
                st_upl = sum((last_price - l["buy_price"]) * l["qty"] for l in lots if l["hold_days"] < 365)
                lt_upl = sum((last_price - l["buy_price"]) * l["qty"] for l in lots if l["hold_days"] >= 365)
            elif api_upl != 0:
                st_qty  = sum(l["qty"] for l in lots if l["hold_days"] < 365)
                lt_qty  = sum(l["qty"] for l in lots if l["hold_days"] >= 365)
                total_q = (st_qty + lt_qty) or 1
                st_upl  = api_upl * (st_qty / total_q)
                lt_upl  = api_upl * (lt_qty / total_q)
            else:
                last_price = avg_cost
                st_upl = lt_upl = 0.0
        else:
            earliest_buy = "—";  max_hold = "—";  term = "—"
            avg_cost     = pos_avg_cost
            st_upl       = api_upl;  lt_upl = 0.0

        rows.append({
            "broker": "IBKR", "code": code, "qty": qty,
            "avg_cost": round(float(avg_cost), 4), "last_price": round(float(last_price), 4),
            "currency": currency, "earliest_buy": str(earliest_buy),
            "hold_days": max_hold, "term": term,
            "ST Unrealized": round(st_upl, 2), "LT Unrealized": round(lt_upl, 2),
            "Total Unrealized": round(st_upl + lt_upl, 2),
        })

    # ── Moomoo positions ──────────────────────────────────────────────────────
    mm_lots_by_code: dict[str, list] = defaultdict(list)
    for lot in moomoo_data.get("open_lots", []):
        mm_lots_by_code[lot["code"]].append(lot)

    positions_df = moomoo_data.get("positions_df", pd.DataFrame())
    if positions_df is not None and not positions_df.empty:
        for _, pos in positions_df.iterrows():
            code     = pos["code"]
            currency = _infer_currency(code)
            last_price  = float(pos.get("last_price", 0) or 0)
            api_upl     = float(pos.get("unrealized_pl", 0) or 0)
            lots = mm_lots_by_code.get(code, [])
            if lots:
                earliest_buy = min(l["buy_date"] for l in lots)
                max_hold     = max(l["hold_days"] for l in lots)
                term         = "Long" if max_hold >= 365 else "Short"
                if last_price > 0:
                    st_upl = sum((last_price - l["buy_price"]) * l["qty"] for l in lots if l["hold_days"] < 365)
                    lt_upl = sum((last_price - l["buy_price"]) * l["qty"] for l in lots if l["hold_days"] >= 365)
                else:
                    total_q = sum(l["qty"] for l in lots) or 1
                    st_qty  = sum(l["qty"] for l in lots if l["hold_days"] < 365)
                    lt_qty  = sum(l["qty"] for l in lots if l["hold_days"] >= 365)
                    st_upl  = api_upl * (st_qty / total_q)
                    lt_upl  = api_upl * (lt_qty / total_q)
                avg_cost     = sum(l["buy_price"] * l["qty"] for l in lots) / (sum(l["qty"] for l in lots) or 1)
            else:
                earliest_buy = "—";  max_hold = "—";  term = "—"
                avg_cost     = float(pos.get("average_cost", 0) or 0)
                st_upl       = api_upl;  lt_upl = 0.0

            rows.append({
                "broker": "Moomoo", "code": code,
                "qty": float(pos.get("qty", 0)),
                "avg_cost": round(float(avg_cost), 4),
                "last_price": round(float(last_price), 4),
                "currency": currency,
                "earliest_buy": str(earliest_buy),
                "hold_days": max_hold, "term": term,
                "ST Unrealized": round(st_upl, 2), "LT Unrealized": round(lt_upl, 2),
                "Total Unrealized": round(st_upl + lt_upl, 2),
            })

    return rows


# ─── HTML helpers (shared) ────────────────────────────────────────────────────

def _fmt(val, decimals=2):
    val = round(float(val), decimals)
    cls = "pos" if val > 0 else ("neg" if val < 0 else "")
    prefix = "+" if val > 0 else ""
    return f'<span class="{cls}">{prefix}{val:,.{decimals}f}</span>'


def _df_to_html(df: pd.DataFrame, numeric_cols=None, discount_cols=None) -> str:
    if df is None or df.empty:
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
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _build_fy_sections_combined(combined_matched: list) -> str:
    """Per-FY trade tables with 'broker' column for the combined report."""
    if not combined_matched:
        return ""
    df = pd.DataFrame(combined_matched)
    df["buy_date"]  = df["buy_date"].astype(str)
    df["sell_date"] = df["sell_date"].astype(str)
    sections = []
    for fy in sorted(df.fy.unique()):
        grp = df[df.fy == fy].copy()
        display = grp[[
            "broker", "code", "currency", "term", "buy_date", "sell_date", "hold_days",
            "qty", "buy_price", "sell_price", "gross_gain", "cgt_discount", "taxable_gain",
        ]].rename(columns={"cgt_discount": "50% Discount", "taxable_gain": "Taxable Gain",
                            "gross_gain": "Gross Gain"})
        tbl = _df_to_html(display,
                          numeric_cols=["Gross Gain", "Taxable Gain"],
                          discount_cols=["50% Discount"])
        n_trades = len(grp)
        sections.append(
            f'<details class="fy-section">'
            f'<summary class="fy-summary" style="cursor:pointer;font-size:1rem;font-weight:600;'
            f'color:#7ecbf5;padding:4px 0">&#x1F4CB; {fy} \u2014 Trade Detail ({n_trades} trades) \u2014 click to expand</summary>'
            f'<div style="margin-top:10px">{tbl}</div>'
            f'</details>'
        )
    return "\n".join(sections)


# ─── Combined HTML template ───────────────────────────────────────────────────

COMBINED_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Combined CGT Analysis – {title_sources}</title>
<style>
  body {{ font-family:'Segoe UI',Arial,sans-serif; background:#0f1117; color:#e0e0e0; margin:0; padding:20px; }}
  h1   {{ color:#00d4ff; letter-spacing:1px; }}
  h2   {{ color:#7ecbf5; border-bottom:1px solid #333; padding-bottom:6px; }}
  h3   {{ color:#aad4f5; }}
  p.note {{ background:#1e2330; border-left:3px solid #f5a623; padding:10px 14px; font-size:0.88em; color:#ccc; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:24px; font-size:0.86em; }}
  th {{ background:#1a2540; color:#7ecbf5; padding:8px 10px; text-align:left; border-bottom:2px solid #2c3a55; }}
  td {{ padding:7px 10px; border-bottom:1px solid #22293a; }}
  tr:hover td {{ background:#1a2236; }}
  .pos {{ color:#4caf50; }}
  .neg {{ color:#f44336; }}
  .disc {{ color:#f5a623; }}
  .fy-section {{ background:#12181f; border-radius:8px; padding:16px 20px; margin-bottom:28px; border:1px solid #1c2540; }}
  .warn {{ background:#2a1f10; border-left:3px solid #e07000; padding:6px 12px; font-size:0.82em; color:#e0c080; margin-bottom:4px; }}
  .broker-ibkr   {{ background:#0d2035; border-left:4px solid #00d4ff; padding:16px 20px; margin-bottom:28px; border-radius:8px; }}
  .broker-moomoo {{ background:#1a1a0d; border-left:4px solid #f5a623; padding:16px 20px; margin-bottom:28px; border-radius:8px; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:0.75em; font-weight:bold; }}
  .badge-ibkr     {{ background:#00d4ff22; color:#00d4ff; border:1px solid #00d4ff55; }}
  .badge-moomoo   {{ background:#f5a62322; color:#f5a623; border:1px solid #f5a62355; }}
  .badge-zerodha  {{ background:#5c2a9d22; color:#b07fff; border:1px solid #8a50dd55; }}
  .badge-angelone {{ background:#e8232322; color:#ff7070; border:1px solid #e8232355; }}
  .broker-india   {{ background:#1a0d1a; border-left:4px solid #b07fff; padding:16px 20px; margin-bottom:28px; border-radius:8px; }}
  .broker-webull  {{ background:#1a110d; border-left:4px solid #ff6b35; padding:16px 20px; margin-bottom:28px; border-radius:8px; }}
  .badge-webull   {{ background:#ff6b3522; color:#ff9a65; border:1px solid #ff6b3555; }}
  .stat-badge     {{ display:inline-block; padding:4px 14px; border-radius:16px; font-size:0.88em; margin:2px 4px; }}
  .stat-badge.pos {{ background:#06d6a022; color:#06d6a0; border:1px solid #06d6a055; }}
  .stat-badge.neg {{ background:#ff6b6b22; color:#ff6b6b; border:1px solid #ff6b6b55; }}
</style>
</head>
<body>
<h1>🇦🇺 Combined CGT Analysis — IBKR &amp; Moomoo &amp; Webull &amp; 🇮🇳 India</h1>
<p>Generated: {generated}</p>

<p class="note">
  <strong>Disclaimer:</strong> Foreign currency amounts (USD/HKD) are reported in their
  native currency and must be converted to AUD using the RBA rate on each trade date.
  This tool uses <strong>FIFO</strong> cost-basis matching (standard ATO method).
  The 50% CGT discount applies only to <em>gains</em> held ≥ 12 months by individuals.
  Consult a registered tax agent for your CGT position.
</p>

{warnings_html}

{cgt_snapshot}
<h2>Combined Per-FY Summary (Both Brokers)</h2>
{combined_fy_table}

<h2>Per-Broker FY Summary</h2>
<div class="broker-ibkr">
  <h3><span class="badge badge-ibkr">IBKR</span> &nbsp; FY Summary</h3>
  {ibkr_fy_table}
</div>
{moomoo_section}
{webull_section}

<details class="fy-section" style="border-color:#2d5a3d">
<summary style="cursor:pointer;color:#06d6a0;font-weight:600;font-size:1rem;padding:4px 0">&#x1F4B0; Combined Open Positions (US / HK) &#x2014; click to expand</summary>
<div style="margin-top:12px">
<p class="note">All current holdings across both brokers. {price_note}</p>
{open_positions_table}
</div>
</details>

<h2>Combined Trade Details (All FYs)</h2>
{fy_sections}

{webull_holdings_section}
{india_section}

</body>
</html>"""


# ─── Consolidated stats helper ────────────────────────────────────────────────

def _build_consolidated_stats_html(
    combined_matched: list,
    combined_fy_df: pd.DataFrame,
) -> str:
    """
    Build a compact "stats banner" above the FY table:
    - Net taxable per FY headline (badge per FY)
    - Best and worst contributors across ALL FYs
    """
    if combined_fy_df is None or combined_fy_df.empty:
        return ""

    # ── Per-FY net taxable badges ─────────────────────────────────────────────
    fy_badges = []
    fy_data = combined_fy_df.groupby("FY")["Net Taxable"].sum().reset_index()
    for _, row in fy_data.sort_values("FY").iterrows():
        fy    = row["FY"]
        net   = row["Net Taxable"]
        sign  = "+" if net >= 0 else ""
        cls   = "pos" if net >= 0 else "neg"
        fy_badges.append(
            f'<span class="stat-badge {cls}">'
            f'<strong>{fy}</strong> Net Taxable: {sign}{net:,.2f}'
            f'</span>'
        )

    # ── Best / worst contributors from matched trade list ─────────────────────
    by_symbol: dict[str, float] = defaultdict(float)
    for t in combined_matched:
        gain = float(t.get("gross_gain", t.get("gain_loss", t.get("gain", 0))))
        sym  = t.get("code", t.get("symbol", "?"))
        by_symbol[sym] += gain

    best_worst_html = ""
    if by_symbol:
        sorted_syms = sorted(by_symbol.items(), key=lambda x: x[1], reverse=True)
        top5    = sorted_syms[:5]
        bottom5 = sorted_syms[-5:][::-1]

        def _sym_row(sym, val):
            sign = "+" if val >= 0 else ""
            cls  = "pos" if val >= 0 else "neg"
            return (
                f'<tr><td style="font-weight:600">{sym}</td>'
                f'<td class="{cls}" style="text-align:right">{sign}{val:,.2f}</td></tr>'
            )

        best_rows  = "\n".join(_sym_row(s, v) for s, v in top5)
        worst_rows = "\n".join(_sym_row(s, v) for s, v in bottom5)
        best_worst_html = (
            '<div style="display:flex;gap:32px;flex-wrap:wrap;margin-top:12px">'
            '<div><strong style="color:#06d6a0">🏆 Best Contributors (All Time)</strong>'
            f'<table style="min-width:220px;margin-top:6px"><tbody>{best_rows}</tbody></table></div>'
            '<div><strong style="color:#ff6b6b">📉 Worst Contributors (All Time)</strong>'
            f'<table style="min-width:220px;margin-top:6px"><tbody>{worst_rows}</tbody></table></div>'
            '</div>'
        )

    badges_html = " &nbsp; ".join(fy_badges) if fy_badges else ""
    return (
        '<div class="stat-banner" style="'
        'background:#1a1a2e;border:1px solid #2a2a4e;'
        'border-radius:8px;padding:16px 20px;margin-bottom:24px">'
        f'<div style="margin-bottom:8px">{badges_html}</div>'
        f'{best_worst_html}'
        '</div>'
    )


def generate_combined_html(
    ibkr_data: dict,
    moomoo_data: dict,
    combined_matched: list,
    ibkr_fy_df: pd.DataFrame,
    moomoo_fy_df: pd.DataFrame,
    all_warnings: list,
    india_data: dict = None,
    webull_data: dict = None,
    india_fy_rows: list = None,
) -> str:
    webull_fy_df = webull_data.get("fy_sum_df") if webull_data else None
    combined_fy_df = combine_fy_summaries(ibkr_fy_df, moomoo_fy_df, webull_fy_df)

    num_cols  = ["ST Gains", "ST Losses", "LT Gross Gains", "LT Losses", "Net Taxable"]
    disc_cols = ["CGT Discount"]

    combined_fy_html = _df_to_html(combined_fy_df, numeric_cols=num_cols, discount_cols=disc_cols)
    cgt_snapshot_html = _build_consolidated_stats_html(combined_matched, combined_fy_df)
    ibkr_fy_html     = _df_to_html(ibkr_fy_df,    numeric_cols=num_cols, discount_cols=disc_cols)
    moomoo_fy_html   = _df_to_html(moomoo_fy_df,  numeric_cols=num_cols, discount_cols=disc_cols)
    if not moomoo_fy_df.empty:
        moomoo_section = (
            '<div class="broker-moomoo">'
            '<h3><span class="badge badge-moomoo">Moomoo</span> &nbsp; FY Summary</h3>'
            f'{moomoo_fy_html}</div>'
        )
    else:
        moomoo_section = ""

    warnings_html = ""
    if all_warnings:
        items = "\n".join(f'<div class="warn">⚠ {w}</div>' for w in all_warnings)
        warnings_html = f"<h2>Warnings</h2>{items}"

    pos_rows = build_combined_positions_rows(ibkr_data, moomoo_data)
    if pos_rows:
        pos_df        = pd.DataFrame(pos_rows)
        open_pos_html = _df_to_html(pos_df,
                                    numeric_cols=["ST Unrealized", "LT Unrealized", "Total Unrealized"])
        # Determine price note
        if ibkr_data.get("live_portfolio"):
            price_note = "IBKR prices: live (IB Gateway). Moomoo prices: live (OpenD API)."
        else:
            price_note = "Live prices from Moomoo OpenD API; IBKR prices from statement."
    else:
        open_pos_html = "<p><em>No open positions found.</em></p>"
        price_note    = ""

    india_section = ""
    if india_data and india_data.get("positions"):
        india_pos_html = build_india_positions_html(india_data)
        india_total_pnl = india_data.get('total_pnl', 0)
        india_section  = (
            '<div class="broker-india">'
            '<h2>🇮🇳 India Holdings — Zerodha + Angel One</h2>'
            '<div class="warn" style="margin-bottom:12px">'
            '⚠ <strong>This is UNREALISED P&amp;L — NOT Capital Gains Tax.</strong> '
            'The P&amp;L figures below reflect current open positions valued at today\'s market price. '
            'Unrealised losses/gains do NOT create a CGT obligation until positions are sold. '
            'For actual realised CGT, run the India annual statement (option 29 includes India FY rows from statement files).</div>'
            '<p class="note">India CGT rates when you DO sell: STCG @ 20% (held &lt;1 year), LTCG @ 12.5% above ₹1.25L (held ≥1 year). '
            'Purchase dates not available via live API — refer to your broker console for exact holding periods.</p>'
            f'{india_pos_html}</div>'
        )
        india_warnings = india_data.get("warnings", [])
        if india_warnings:
            items = "\n".join(
                f'<div class="warn">⚠ [India] {w}</div>' for w in india_warnings
            )
            if warnings_html:
                warnings_html += "\n" + items
            else:
                warnings_html = f"<h2>Warnings</h2>\n{items}"

    # India FY P&L summary (AngelOne + Zerodha from statement PDFs/XLSXs)
    if india_fy_rows:
        _fy_table_rows = ""
        for r in sorted(india_fy_rows, key=lambda x: (x.get("fy", ""), x.get("broker", ""))):
            fy = r.get("fy", "")
            broker = r.get("broker", "")
            st = r.get("st_inr", 0.0) or 0.0
            lt = r.get("lt_inr", 0.0) or 0.0
            intra = r.get("intraday_inr", 0.0) or 0.0
            net = r.get("net_inr", 0.0) or 0.0
            st_cls = "pos" if st >= 0 else "neg"
            lt_cls = "pos" if lt >= 0 else "neg"
            intra_cls = "pos" if intra >= 0 else "neg"
            net_cls = "pos" if net >= 0 else "neg"
            _fy_table_rows += (
                f'<tr><td>{fy}</td><td>{broker}</td>'
                f'<td class="{st_cls}">₹{st:>12,.2f}</td>'
                f'<td class="{lt_cls}">₹{lt:>12,.2f}</td>'
                f'<td class="{intra_cls}">₹{intra:>12,.2f}</td>'
                f'<td class="{net_cls}"><strong>₹{net:>12,.2f}</strong></td>'
                f'</tr>\n'
            )
        india_fy_section = (
            '<div class="broker-india" style="margin-bottom:1rem">'
            '<h2>🇮🇳 India FY P&amp;L Summary (AngelOne + Zerodha)</h2>'
            '<p class="note">Values in INR from annual P&amp;L statements. '
            'Indian capital gains: Delivery STCG @ 20%, LTCG @ 12.5% (held ≥1 yr, above ₹1.25L exemption); '
            'Intraday &amp; F&amp;O @ income tax slab rate. This is Indian tax, not Australian CGT.</p>'
            '<table><thead><tr>'
            '<th>FY</th><th>Broker</th>'
            '<th>Delivery ST (₹)</th><th>Delivery LT (₹)</th>'
            '<th>Intraday/F&amp;O (₹)</th><th>Net P&amp;L (₹)</th>'
            '</tr></thead><tbody>'
            f'{_fy_table_rows}'
            '</tbody></table></div>'
        )
        india_section = india_fy_section + india_section

    # Webull FY + holdings section
    webull_section = ""
    webull_holdings_section = ""
    if webull_data:
        wd_fy = webull_data.get("fy_sum_df")
        if wd_fy is not None and not wd_fy.empty:
            webull_section = (
                '<div class="broker-webull">'
                '<h3><span class="badge badge-webull">Webull AU</span> &nbsp; FY Summary</h3>'
                f'{_df_to_html(wd_fy, numeric_cols=num_cols, discount_cols=disc_cols)}</div>'
            )
        holdings = webull_data.get("holdings", [])
        live_prices = webull_data.get("live_prices", {})
        if holdings:
            rows_html = []
            total_cost = 0.0
            total_mval = 0.0
            for h in sorted(holdings, key=lambda x: x.get("symbol", "")):
                sym   = h.get("symbol", "")
                qty   = h.get("qty", 0)
                avg   = float(h.get("avg_price", 0) or 0)
                stmt_close = float(h.get("close_price", 0) or 0)
                cost  = float(h.get("cost_basis", 0) or 0)
                curr  = h.get("currency", "USD")
                # Use live price if available
                lp = live_prices.get(sym, {})
                cur_price = lp.get("price", 0.0) or stmt_close
                price_src = "Live" if lp.get("price", 0) > 0 else "Stmt"
                mval = round(cur_price * qty, 2)
                upl  = round(mval - cost, 2)
                pct  = ((cur_price - avg) / avg * 100) if avg else 0.0
                pct_sign = "+" if pct >= 0 else ""
                upl_sign = "+" if upl >= 0 else ""
                pct_cls  = "pos" if pct >= 0 else "neg"
                upl_cls  = "pos" if upl >= 0 else "neg"
                total_cost += cost
                total_mval += mval
                rows_html.append(
                    f"<tr>"
                    f"<td>{sym}</td>"
                    f"<td>{h.get('name','')[:30]}</td>"
                    f"<td>{qty}</td>"
                    f"<td>{curr} {avg:,.4f}</td>"
                    f"<td>{curr} {cur_price:,.4f}</td>"
                    f'<td><span class="{pct_cls}">{pct_sign}{pct:.1f}%</span></td>'
                    f"<td>{curr} {mval:,.2f}</td>"
                    f'<td><span class="{upl_cls}">{upl_sign}{curr} {abs(upl):,.2f}</span></td>'
                    f"<td>{price_src}</td>"
                    f"</tr>"
                )
            as_of = webull_data.get("holdings", [{}])
            as_of_date = holdings[0].get("as_of_date", "") if holdings else ""
            has_live = any(live_prices.get(h.get("symbol", ""), {}).get("price", 0) > 0 for h in holdings)
            price_note = "Live prices via yfinance" if has_live else "Statement closing prices"
            header = (
                "<table><thead><tr>"
                "<th>Symbol</th><th>Name</th><th>Qty</th>"
                "<th>Avg Cost</th><th>Price</th><th>Change %</th>"
                "<th>Market Val</th><th>Unrealised P&amp;L</th><th>Source</th>"
                "</tr></thead><tbody>"
            )
            footer = (
                f"</tbody></table>"
                f'<p style="font-size:0.84em;color:#aaa;">As of {as_of_date} &nbsp;|&nbsp; '
                f'{len(holdings)} positions &nbsp;|&nbsp; {price_note}</p>'
            )
            webull_warnings = webull_data.get("warnings", [])
            warn_html = ""
            if webull_warnings:
                warn_html = "".join(f'<div class="warn">⚠ [Webull] {w}</div>' for w in webull_warnings)
                if warnings_html:
                    warnings_html += "\n" + warn_html
                else:
                    warnings_html = f"<h2>Warnings</h2>\n{warn_html}"
            webull_holdings_section = (
                '<div class="broker-webull">'
                '<h2><span class="badge badge-webull">Webull AU</span> &nbsp; Current Holdings</h2>'
                + header + "\n".join(rows_html) + footer +
                '</div>'
            )

    # Build dynamic title from active data sources (BUG-012 fix)
    _srcs = []
    if ibkr_data:   _srcs.append("IBKR")
    if moomoo_data: _srcs.append("Moomoo")
    if webull_data:  _srcs.append("Webull")
    if india_data or india_fy_rows:  _srcs.append("India")
    title_sources = " + ".join(_srcs) if _srcs else "All Sources"

    return COMBINED_TEMPLATE.format(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        warnings_html=warnings_html,
        cgt_snapshot=cgt_snapshot_html,
        combined_fy_table=combined_fy_html,
        ibkr_fy_table=ibkr_fy_html,
        moomoo_section=moomoo_section,
        webull_section=webull_section,
        fy_sections=_build_fy_sections_combined(combined_matched),
        open_positions_table=open_pos_html,
        price_note=price_note,
        india_section=india_section,
        webull_holdings_section=webull_holdings_section,
        title_sources=title_sources,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # cp1252 fix
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        description="Generate individual + combined CGT reports for IBKR, Moomoo, and India (Zerodha + Angel One)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── IBKR args ──────────────────────────────────────────────────────────────
    g = parser.add_argument_group("IBKR")
    g.add_argument("--ibkr-statement",   default=None, dest="ibkr_statement",
                   help="Path to IBKR Activity Statement CSV")
    g.add_argument("--ibkr-flex-token",  default=None, dest="ibkr_flex_token",
                   help="IBKR Flex Web Service token")
    g.add_argument("--ibkr-flex-query-id", default=None, dest="ibkr_flex_query_id",
                   help="IBKR Flex Query ID")
    g.add_argument("--ibkr-start",  default="2018-01-01", dest="ibkr_start",
                   help="Earliest IBKR trade date (default: 2018-01-01)")
    g.add_argument("--no-ibkr",     action="store_true", dest="no_ibkr",
                   help="Skip IBKR (generate Moomoo-only and combined reports)")

    # ── Moomoo args ────────────────────────────────────────────────────────────
    g2 = parser.add_argument_group("Moomoo")
    g2.add_argument("--moomoo-start", default="2018-01-01", dest="moomoo_start",
                    help="Earliest Moomoo trade date (default: 2018-01-01)")
    g2.add_argument("--no-moomoo",    action="store_true", dest="no_moomoo",
                    help="Skip Moomoo (generate IBKR-only and combined report)")

    # ── India args ─────────────────────────────────────────────────────────────
    g3 = parser.add_argument_group("India (Zerodha + Angel One)")
    g3.add_argument("--no-india", action="store_true", dest="no_india",
                    help="Skip India holdings section")

    # ── Webull args ────────────────────────────────────────────────────────────
    g4 = parser.add_argument_group("Webull AU")
    default_wb_dir = os.path.normpath(os.path.join(HERE, "..", "webull-statement"))
    g4.add_argument("--webull-dir", default=default_wb_dir, dest="webull_dir",
                    metavar="DIR",
                    help=f"Folder containing Webull AU PDF statements (default: {default_wb_dir})")
    g4.add_argument("--no-webull", action="store_true", dest="no_webull",
                    help="Skip Webull")

    # ── Output ─────────────────────────────────────────────────────────────────
    parser.add_argument("--out-dir", default=None, dest="out_dir",
                        help="Output directory for HTML files (default: this script's directory)")

    args = parser.parse_args()

    _default_out = os.path.normpath(os.path.join(HERE, "..", "reports", "cgt"))
    out_dir = args.out_dir or _default_out
    os.makedirs(out_dir, exist_ok=True)

    print("=" * 60)
    print("Combined CGT Report — IBKR + Moomoo + India (Zerodha + Angel One)")
    print("=" * 60)

    # ── Load modules ───────────────────────────────────────────────────────────
    ibkr_mod = _load_module("cgt_ibkr", "ibkr-cgt/ibkr_cgt_analysis.py")

    moomoo_mod = None
    if not args.no_moomoo:
        try:
            moomoo_mod = _load_module("cgt_moomoo", "moomoo-cgt/cgt_analysis.py")
        except Exception as e:
            print(f"\n[Moomoo] Cannot load module: {e}")
            print("  Skipping Moomoo. Is moomoo-api installed and compatible?")
            args.no_moomoo = True

    # ── Run IBKR ───────────────────────────────────────────────────────────────
    ibkr_data: dict = {
        "matched": [], "open_lots": [], "fy_sum_df": pd.DataFrame(),
        "statement_positions": pd.DataFrame(), "live_portfolio": {},
        "warnings": [], "trades_df": pd.DataFrame(),
        "cash_transactions": pd.DataFrame(),
        "fetch_start": args.ibkr_start, "statement_label": "",
    }

    if not args.no_ibkr:
        print("\n[IBKR] Fetching data ...")
        try:
            ibkr_data = ibkr_mod.get_data(
                statement=args.ibkr_statement,
                flex_token=args.ibkr_flex_token,
                flex_query_id=args.ibkr_flex_query_id,
                start=args.ibkr_start,
            )
            print(f"  {len(ibkr_data['matched'])} IBKR CGT events found.")
        except Exception as e:
            print(f"  [IBKR] ERROR: {e}")

        # Supplement IBKR fy_sum_df with PDF annual statements for FYs not in Flex data
        try:
            _ibkr_stmt_dir = os.path.normpath(os.path.join(HERE, "..", "ibkr-statement"))
            if os.path.isdir(_ibkr_stmt_dir):
                _stmt_mod = _load_module(
                    "ibkr_stmt_reader",
                    os.path.join(_ibkr_stmt_dir, "ibkr_statement_reader.py"),
                )
                _pdf_summary = _stmt_mod.build_realised_summary_for_dashboard(_ibkr_stmt_dir)
                _flex_fys = (
                    set(ibkr_data["fy_sum_df"]["FY"].tolist())
                    if not ibkr_data["fy_sum_df"].empty else set()
                )
                _supp_rows = []
                for _pdf in _pdf_summary.get("ibkr", []):
                    _fy = _pdf["fy"]
                    if _fy not in _flex_fys:
                        _supp_rows.append({
                            "FY":             _fy,
                            "Currency":       "USD",
                            "ST Gains":       max(0.0, float(_pdf.get("st_profit_usd", 0) or 0)),
                            "ST Losses":      min(0.0, -abs(float(_pdf.get("st_loss_usd", 0) or 0))),
                            "LT Gross Gains": max(0.0, float(_pdf.get("lt_profit_usd", 0) or 0)),
                            "LT Losses":      min(0.0, -abs(float(_pdf.get("lt_loss_usd", 0) or 0))),
                            "CGT Discount":   0.0,
                            "Net Taxable":    round(float(_pdf.get("realized_usd", 0) or 0), 2),
                            "# Trades":       0,
                        })
                        ibkr_data.setdefault("warnings", []).append(
                            f"{_fy}: P&L from PDF annual statement (Flex API returned no events "
                            f"for this FY — stocks likely transferred via ACAT from Webull)."
                        )
                if _supp_rows:
                    _supp_df = pd.DataFrame(_supp_rows)
                    if ibkr_data["fy_sum_df"].empty:
                        ibkr_data["fy_sum_df"] = _supp_df
                    else:
                        ibkr_data["fy_sum_df"] = pd.concat(
                            [ibkr_data["fy_sum_df"], _supp_df], ignore_index=True
                        ).sort_values(["FY", "Currency"]).reset_index(drop=True)
                    print(f"  Supplemented IBKR with {len(_supp_rows)} FY(s) from PDF statement(s).")
        except Exception as _stmt_err:
            print(f"  [IBKR PDF] Could not read annual statements: {_stmt_err}")

        # Write individual IBKR report
        if ibkr_data.get("matched") or ibkr_data.get("open_lots") or ibkr_data.get("live_portfolio"):
            ibkr_html = ibkr_mod.generate_html(
                ibkr_data["fy_sum_df"],
                ibkr_data["matched"],
                ibkr_data["open_lots"],
                ibkr_data["live_portfolio"],
                ibkr_data["statement_positions"],
                ibkr_data["warnings"],
                ibkr_data["fetch_start"],
                ibkr_data.get("statement_label", ""),
                cash_transactions=ibkr_data.get("cash_transactions"),
                prior_positions=ibkr_data.get("prior_positions"),
            )
            ibkr_out = os.path.join(out_dir, "ibkr_cgt_report.html")
            with open(ibkr_out, "w", encoding="utf-8") as fh:
                fh.write(ibkr_html)
            print(f"  Individual IBKR report: {ibkr_out}")

    # ── Run Moomoo ─────────────────────────────────────────────────────────────
    moomoo_data: dict = {
        "matched": [], "open_lots": [], "fy_sum_df": pd.DataFrame(),
        "positions_df": pd.DataFrame(), "warnings": [],
        "trades_df": pd.DataFrame(), "fetch_start": args.moomoo_start,
    }

    if not args.no_moomoo and moomoo_mod is not None:
        # Import account list so we fetch from BOTH AU (11111) and SG (11112)
        _root_dir = os.path.normpath(os.path.join(HERE, ".."))
        if _root_dir not in sys.path:
            sys.path.insert(0, _root_dir)
        from moomoo_config import ACCOUNTS as _MM_ACCOUNTS  # [(label, port, firm, acc_id)]

        _all_matched: list = []
        _all_open_lots: list = []
        _all_pos_dfs: list = []
        _all_warnings: list = []
        _all_trades_dfs: list = []

        for _label, _port, _firm, _acc_id in _MM_ACCOUNTS:
            print(f"\n[Moomoo-{_label}] Fetching data (OpenD port {_port}) ...")
            try:
                _acct_data = moomoo_mod.get_data(
                    start=args.moomoo_start,
                    port=_port, firm=_firm, acc_id=_acc_id,
                )
                print(f"  {len(_acct_data['matched'])} Moomoo-{_label} CGT events found.")
                for _t in _acct_data["matched"]:
                    _t.setdefault("account", f"Moomoo-{_label}")
                _all_matched.extend(_acct_data["matched"])
                _all_open_lots.extend(_acct_data["open_lots"])
                if _acct_data.get("positions_df") is not None and not _acct_data["positions_df"].empty:
                    _all_pos_dfs.append(_acct_data["positions_df"])
                _all_warnings.extend([f"[{_label}] {w}" for w in _acct_data.get("warnings", [])])
                if not _acct_data["trades_df"].empty:
                    _all_trades_dfs.append(_acct_data["trades_df"])
            except Exception as e:
                print(f"  [Moomoo-{_label}] ERROR: {e}")
                print(f"  Skipping {_label} (is OpenD port {_port} running?)")

        moomoo_data = {
            "matched":     _all_matched,
            "open_lots":   _all_open_lots,
            "fy_sum_df":   moomoo_mod.fy_summary(_all_matched) if _all_matched else pd.DataFrame(),
            "positions_df": pd.concat(_all_pos_dfs, ignore_index=True) if _all_pos_dfs else pd.DataFrame(),
            "warnings":    _all_warnings,
            "trades_df":   pd.concat(_all_trades_dfs, ignore_index=True) if _all_trades_dfs else pd.DataFrame(),
            "fetch_start": args.moomoo_start,
        }

        # Write individual Moomoo report (combined AU + SG)
        if moomoo_data.get("matched") or (moomoo_data.get("positions_df") is not None
                                           and not moomoo_data["positions_df"].empty):
            mm_html = moomoo_mod.generate_html(
                moomoo_data["fy_sum_df"],
                moomoo_data["matched"],
                moomoo_data["positions_df"],
                moomoo_data["open_lots"],
                moomoo_data["warnings"],
                moomoo_data["fetch_start"],
                account_label="AU + SG",
                firm_label="FUTUAU + FUTUSG",
            )
            moomoo_out = os.path.join(out_dir, "moomoo_cgt_report.html")
            with open(moomoo_out, "w", encoding="utf-8") as fh:
                fh.write(mm_html)
            print(f"  Individual Moomoo report (AU+SG): {moomoo_out}")

    # ── Run India holdings ─────────────────────────────────────────────────────
    india_data: dict = {"positions": [], "zerodha_count": 0, "angelone_count": 0,
                        "total_value": 0.0, "total_pnl": 0.0, "warnings": []}
    if not args.no_india:
        print("\n[India] Fetching Zerodha + Angel One holdings ...")
        try:
            if INDIA_BROKERS_DIR not in sys.path:
                sys.path.insert(0, INDIA_BROKERS_DIR)
            import india_cgt as india_cgt_mod  # type: ignore
            india_data = india_cgt_mod.get_data()
        except Exception as e:
            print(f"  [India] WARNING: {e}")
            india_data["warnings"].append(str(e))

    # ── Run Webull ─────────────────────────────────────────────────────────────
    webull_data: dict = {
        "matched": [], "open_lots": [], "fy_sum_df": pd.DataFrame(),
        "holdings": [], "warnings": [], "fetch_start": "n/a",
    }
    if not args.no_webull:
        webull_dir = os.path.abspath(args.webull_dir)
        if os.path.isdir(webull_dir):
            print(f"\n[Webull AU] Reading PDFs from: {webull_dir}")
            try:
                _webull_cgt_dir = os.path.join(HERE, "webull-cgt")
                if _webull_cgt_dir not in sys.path:
                    sys.path.insert(0, _webull_cgt_dir)
                webull_mod = _load_module("cgt_webull", "webull-cgt/webull_cgt_analysis.py")
                webull_data = webull_mod.get_data(webull_dir)
                print(f"  {len(webull_data['matched'])} Webull CGT events found.")

                # Write individual Webull report
                if webull_data.get("matched") or webull_data.get("holdings") or webull_data.get("open_lots"):
                    wb_html = webull_mod.generate_html(
                        webull_data["fy_sum_df"],
                        webull_data["matched"],
                        webull_data["open_lots"],
                        webull_data["warnings"],
                        csv_path="PDF statements",
                        holdings=webull_data.get("holdings"),
                        live_prices=webull_data.get("live_prices"),
                    )
                    webull_out = os.path.join(out_dir, "webull_cgt_report.html")
                    with open(webull_out, "w", encoding="utf-8") as fh:
                        fh.write(wb_html)
                    print(f"  Individual Webull report: {webull_out}")
            except Exception as e:
                print(f"  [Webull] ERROR: {e}")
                webull_data["warnings"].append(str(e))
        else:
            print(f"\n[Webull AU] PDF folder not found ({webull_dir}) — skipping.")

    # ── Generate combined report ───────────────────────────────────────────────
    print("\n[Combined] Generating combined report ...")
    combined_matched = combine_matched_trades(
        ibkr_data.get("matched", []),
        moomoo_data.get("matched", []),
        webull_data.get("matched", []),
    )
    all_warnings = (
        [f"[IBKR] {w}" for w in ibkr_data.get("warnings", [])] +
        [f"[Moomoo] {w}" for w in moomoo_data.get("warnings", [])] +
        [f"[Webull] {w}" for w in webull_data.get("warnings", [])]
    )

    # ── Load India FY P&L summaries from AngelOne + Zerodha statements ────────
    _india_fy_rows: list = []
    if not args.no_india:
      _ao_stmt_dir  = os.path.normpath(os.path.join(HERE, "..", "angelone-statement"))
      _zr_stmt_dir  = os.path.normpath(os.path.join(HERE, "..", "zerodha-statement"))
      for _stmt_dir, _mod_name, _mod_file in [
          (_ao_stmt_dir, "ao_stmt_reader",  "angelone_statement_reader.py"),
          (_zr_stmt_dir, "zr_stmt_reader",  "zerodha_statement_reader.py"),
      ]:
          if os.path.isdir(_stmt_dir):
              try:
                  _mod_path = os.path.join(_stmt_dir, _mod_file)
                  _spec = importlib.util.spec_from_file_location(_mod_name, _mod_path)
                  _smod = importlib.util.module_from_spec(_spec)
                  _spec.loader.exec_module(_smod)
                  _rows = _smod.build_summary_for_dashboard(pathlib.Path(_stmt_dir))
                  _india_fy_rows.extend(_rows)
                  print(f"  [India FY] {_mod_name}: {len(_rows)} FY row(s) loaded from {_stmt_dir}")
              except Exception as _ie:
                  print(f"  [India FY] Could not load {_mod_file}: {_ie}")

    combined_html = generate_combined_html(
        ibkr_data   = ibkr_data,
        moomoo_data = moomoo_data,
        combined_matched = combined_matched,
        ibkr_fy_df  = ibkr_data["fy_sum_df"],
        moomoo_fy_df = moomoo_data["fy_sum_df"],
        all_warnings = all_warnings,
        india_data   = india_data,
        webull_data  = webull_data,
        india_fy_rows = _india_fy_rows,
    )
    combined_out = os.path.join(out_dir, "combined_cgt_report.html")
    with open(combined_out, "w", encoding="utf-8") as fh:
        fh.write(combined_html)
    print(f"  Combined report: {combined_out}")

    # ── Terminal CGT summary ───────────────────────────────────────────────────
    _cgt_summary_df = combine_fy_summaries(
        ibkr_data["fy_sum_df"], moomoo_data["fy_sum_df"], webull_data.get("fy_sum_df")
    )
    if not _cgt_summary_df.empty:
        print("\n" + "=" * 60)
        print("  CGT SUMMARY — Net Taxable by FY")
        print("=" * 60)
        for _, _row in _cgt_summary_df.iterrows():
            _net = float(_row["Net Taxable"])
            _sign = "+" if _net >= 0 else ""
            print(f"  {_row['FY']}  {str(_row['Currency']):<4}  "
                  f"Net Taxable: {_sign}{_net:>12,.2f}  ({int(_row['# Trades'])} trades)")

    print("\n" + "=" * 60)
    print("Reports generated:")
    print(f"  IBKR:     {os.path.join(out_dir, 'ibkr_cgt_report.html')}")
    if not args.no_moomoo:
        print(f"  Moomoo:   {os.path.join(out_dir, 'moomoo_cgt_report.html')}")
    if not args.no_webull:
        print(f"  Webull:   {os.path.join(out_dir, 'webull_cgt_report.html')}")
    print(f"  Combined: {combined_out}")
    print("=" * 60)

    # ── Persist CGT snapshots to report analytics DB ──────────────────────────
    try:
        _rdb_dir = str(pathlib.Path(__file__).parent.parent / "reports")
        if _rdb_dir not in sys.path:
            sys.path.insert(0, _rdb_dir)
        from report_db import save_cgt_snapshot, archive_report as _arc
        if not ibkr_data["fy_sum_df"].empty:
            save_cgt_snapshot(ibkr_data["fy_sum_df"], "IBKR")
        if not moomoo_data["fy_sum_df"].empty:
            save_cgt_snapshot(moomoo_data["fy_sum_df"], "Moomoo")
        if not _cgt_summary_df.empty:
            save_cgt_snapshot(_cgt_summary_df, "Combined")
        _arc(combined_out, "combined_cgt_report")
    except Exception as _rdb_exc:
        print(f"  [report_db] CGT snapshot not saved: {_rdb_exc}")


if __name__ == "__main__":
    main()
