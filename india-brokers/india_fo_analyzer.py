"""
india_fo_analyzer.py
=====================
Extracts F&O (Futures & Options) trade history from Zerodha and Angel One
statements, detects option premium burn, and generates a detailed P&L analysis.

WHY: The global_portfolio_dashboard.py did NOT show options losses.
     INOXWIND25JUL172.5CE + TRENT25JUL5700CE expired worthless in Jul 2025
     → ₹42,075 aggregate loss hidden inside net_inr summary.

This module surfaces that loss explicitly as a dedicated F&O panel.

Usage (standalone):
  cd d:\\workspace\\repo\\stocks
  python india-brokers/india_fo_analyzer.py
  python india-brokers/india_fo_analyzer.py --html   → india_fo_analysis.html

Usage (from india_portfolio_dashboard.py):
  from india_brokers.india_fo_analyzer import build_fo_analysis_html, get_fo_pnl_summary
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).parent.parent

# ─── Option detection patterns ────────────────────────────────────────────────
# NSE options: NIFTY2504023400PE, INOXWIND25JUL172.5CE, TRENT25JUL5700CE
_IS_OPTION = re.compile(r'(CE|PE)$', re.I)
_IS_FUTURE = re.compile(r'(FUT|JUN|JUL|AUG|SEP|OCT|NOV|DEC|JAN|FEB|MAR|APR|MAY)\d{2}$', re.I)
_OPTION_TYPE = re.compile(r'^(.+?)(\d+(?:\.\d+)?)(CE|PE)$', re.I)


def _classify(symbol: str) -> str:
    """Return 'OPTION', 'FUTURE', or 'EQUITY'."""
    sym = symbol.strip().upper()
    if _IS_OPTION.search(sym):
        return "OPTION"
    if _IS_FUTURE.search(sym):
        return "FUTURE"
    return "EQUITY"


def _parse_isin_type(symbol: str) -> dict:
    """Parse option symbol into components if possible."""
    m = _OPTION_TYPE.match(symbol.strip().upper())
    if m:
        underlying, strike, opt_type = m.group(1), float(m.group(2)), m.group(3)
        return {"underlying": underlying, "strike": strike, "type": opt_type}
    return {"underlying": symbol, "strike": None, "type": None}


# ═════════════════════════════════════════════════════════════════════════════
#  ZERODHA XLSX PARSER
# ═════════════════════════════════════════════════════════════════════════════

def parse_zerodha_fo_xlsx(xlsx_path: Path) -> list[dict]:
    """
    Parse Zerodha Tax P&L XLSX for F&O trades.
    Returns list of trade dicts with keys:
      symbol, qty, buy_value, sell_value, pnl, type, option_info, expired, fy
    """
    try:
        import openpyxl
    except ImportError:
        print("WARN: openpyxl not installed — cannot parse Zerodha XLSX")
        return []

    trades = []
    try:
        wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    except Exception as e:
        print(f"WARN: Cannot open {xlsx_path}: {e}")
        return []

    # Detect FY from filename
    fy_match = re.search(r'(\d{4})_(\d{4})', xlsx_path.name)
    fy = f"FY{fy_match.group(2)}" if fy_match else "FY?"

    # First: read F&O summary sheet (has top-level totals)
    fo_summary = {}
    for sn in wb.sheetnames:
        if sn in ("F&O", "Futures and Options"):
            ws = wb[sn]
            for row in ws.iter_rows(values_only=True):
                lbl = str(row[1] or "").strip()
                val = row[2] if len(row) > 2 else None
                if "Options Realized" in lbl:
                    fo_summary["options_total"] = val
                elif "Futures Realized" in lbl:
                    fo_summary["futures_total"] = val
            break

    # Second: scan Tradewise Exits sheet for individual option/future trades
    for sn in wb.sheetnames:
        if "Tradewise" in sn:
            ws = wb[sn]
            header_row = None
            col_idx: dict[str, int] = {}

            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                # Detect header row: look for "Symbol", "ISIN", "Buy Value" etc.
                row_strs = [str(v or "").strip() for v in row]
                if "Symbol" in row_strs and ("Buy Value" in row_strs or "Buy Amount" in row_strs):
                    header_row = i
                    for j, h in enumerate(row_strs):
                        normalized = h.lower().replace(" ", "_")
                        col_idx[normalized] = j
                    continue

                if header_row is None:
                    continue

                # Only process rows after header
                sym = str(row[col_idx.get("symbol", 1)] or "").strip()
                if not sym or sym.lower() in ("symbol", ""):
                    continue

                kind = _classify(sym)
                if kind not in ("OPTION", "FUTURE"):
                    continue

                def _flt(key: str) -> Optional[float]:
                    for variant in [key, key.replace("_", " ")]:
                        idx = col_idx.get(variant)
                        if idx is not None and idx < len(row):
                            v = row[idx]
                            if v is not None:
                                try:
                                    return float(v)
                                except Exception:
                                    pass
                    return None

                qty       = _flt("quantity") or _flt("qty") or 0
                buy_val   = _flt("buy_value") or _flt("buy_amount") or 0
                sell_val  = _flt("sell_value") or _flt("sell_amount") or 0
                pnl       = _flt("realized_p&l") or _flt("p&l") or _flt("profit_&_loss") or (sell_val - buy_val)
                expired   = sell_val == 0 and buy_val > 0

                t = {
                    "symbol":      sym,
                    "qty":         int(qty),
                    "buy_value":   round(buy_val, 2),
                    "sell_value":  round(sell_val, 2),
                    "pnl":         round(pnl, 2),
                    "type":        kind,
                    "option_info": _parse_isin_type(sym) if kind == "OPTION" else {},
                    "expired":     expired,
                    "fy":          fy,
                    "source":      "Zerodha",
                    "file":        xlsx_path.name,
                }
                trades.append(t)

    return trades


def scan_all_zerodha_fo(folder: Path = None) -> list[dict]:
    """Scan all Zerodha XLSX files in the given folder for F&O trades."""
    folder = folder or ROOT / "zerodha-statement"
    all_trades = []
    for xlsx in sorted(folder.rglob("*.xlsx")):
        trades = parse_zerodha_fo_xlsx(xlsx)
        if trades:
            all_trades.extend(trades)
    return all_trades


# ═════════════════════════════════════════════════════════════════════════════
#  ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

def compute_fo_summary(trades: list[dict]) -> dict:
    """Compute aggregate F&O analytics from a list of trades."""
    options = [t for t in trades if t["type"] == "OPTION"]
    futures = [t for t in trades if t["type"] == "FUTURE"]

    def _stats(items: list[dict]) -> dict:
        if not items:
            return {"count": 0, "total_pnl": 0.0, "wins": 0, "losses": 0,
                    "total_premium_paid": 0.0, "premium_burned": 0.0,
                    "win_rate": 0.0}
        total_pnl       = sum(t["pnl"] for t in items)
        wins            = [t for t in items if t["pnl"] > 0]
        losses          = [t for t in items if t["pnl"] <= 0]
        premium_paid    = sum(t["buy_value"] for t in items)
        premium_burned  = sum(t["buy_value"] for t in items if t.get("expired"))
        expired_trades  = [t for t in items if t.get("expired")]
        return {
            "count":            len(items),
            "total_pnl":        round(total_pnl, 2),
            "wins":             len(wins),
            "losses":           len(losses),
            "total_premium_paid": round(premium_paid, 2),
            "premium_burned":   round(premium_burned, 2),
            "expired_count":    len(expired_trades),
            "win_rate":         round(len(wins) / len(items) * 100, 1) if items else 0.0,
        }

    by_fy: dict[str, list[dict]] = {}
    for t in trades:
        by_fy.setdefault(t["fy"], []).append(t)

    return {
        "all_trades":     len(trades),
        "options":        _stats(options),
        "futures":        _stats(futures),
        "by_fy":          {fy: _stats(ts) for fy, ts in sorted(by_fy.items())},
        "worst_trades":   sorted(trades, key=lambda t: t["pnl"])[:5],
        "best_trades":    sorted(trades, key=lambda t: t["pnl"], reverse=True)[:5],
        "expired_trades": [t for t in trades if t.get("expired")],
    }


def get_fo_pnl_summary(folder: Path = None) -> dict:
    """
    Quick summary for embedding in global_portfolio_dashboard.
    Returns dict with: total_pnl_inr, premium_burned_inr, win_rate,
                       total_trades, by_fy, warning
    """
    trades = scan_all_zerodha_fo(folder)
    if not trades:
        return {"total_pnl_inr": 0.0, "premium_burned_inr": 0.0,
                "win_rate": 0.0, "total_trades": 0, "by_fy": {}, "warning": None}

    summary = compute_fo_summary(trades)
    opt = summary["options"]
    total_pnl = opt["total_pnl"] + summary["futures"]["total_pnl"]
    warning = None
    if opt["premium_burned"] > 10000:
        warning = f"ALERT: ₹{opt['premium_burned']:,.0f} in option premium expired worthless!"

    return {
        "total_pnl_inr":      total_pnl,
        "premium_burned_inr": opt["premium_burned"],
        "win_rate":           opt["win_rate"],
        "total_trades":       summary["all_trades"],
        "by_fy":              summary["by_fy"],
        "warning":            warning,
        "trades":             trades,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  HTML REPORT GENERATOR
# ═════════════════════════════════════════════════════════════════════════════

def build_fo_analysis_html(folder: Path = None) -> str:
    """
    Build an HTML section for the F&O analysis panel.
    Suitable for embedding in india_portfolio_dashboard.py.
    """
    summary_data = get_fo_pnl_summary(folder)
    trades = summary_data.get("trades", [])
    if not trades:
        return '<p style="color:#94a3b8">No F&O trades found in statements.</p>'

    full = compute_fo_summary(trades)
    opt  = full["options"]
    fut  = full["futures"]
    by_fy = full["by_fy"]

    # Color helpers
    def _pnl_style(v: float) -> str:
        return 'color:#22c55e;font-weight:bold' if v > 0 else (
               'color:#ef4444;font-weight:bold' if v < 0 else 'color:#94a3b8')

    def _fmt(v: float) -> str:
        return f"₹{v:+,.0f}"

    # Warning banner
    warn_html = ""
    if summary_data.get("warning"):
        warn_html = (f'<div style="background:#fef2f2;border-left:4px solid #ef4444;'
                     f'padding:10px 16px;margin:10px 0;border-radius:4px;color:#b91c1c">'
                     f'⚠️ {summary_data["warning"]}</div>')

    # FY summary table
    fy_rows = ""
    for fy, st in sorted(by_fy.items()):
        pnl_s = _pnl_style(st["total_pnl"])
        pb = st["premium_burned"]
        pb_s = 'color:#ef4444;font-weight:bold' if pb > 0 else ''
        fy_rows += (
            f'<tr><td style="padding:5px 8px;font-weight:bold">{fy}</td>'
            f'<td style="padding:5px 8px;text-align:right">{st["count"]}</td>'
            f'<td style="padding:5px 8px;text-align:right;{pnl_s}">{_fmt(st["total_pnl"])}</td>'
            f'<td style="padding:5px 8px;text-align:right">{st["wins"]}/{st["losses"]}</td>'
            f'<td style="padding:5px 8px;text-align:right">{st["win_rate"]:.0f}%</td>'
            f'<td style="padding:5px 8px;text-align:right;{pb_s}">'
            f'{"" if not pb else _fmt(-pb) + " 🔥"}</td>'
            f'</tr>'
        )

    # Individual trades table
    trade_rows = ""
    for t in sorted(trades, key=lambda x: x["pnl"]):
        pnl = t["pnl"]
        pnl_s = _pnl_style(pnl)
        exp_badge = ('<span style="background:#ef4444;color:#fff;padding:1px 5px;'
                     'border-radius:3px;font-size:10px;margin-left:4px">EXPIRED</span>'
                     if t.get("expired") else "")
        opt_info = t.get("option_info", {})
        detail = ""
        if opt_info.get("underlying"):
            detail = f' · {opt_info["underlying"]} {opt_info.get("type","")} @ ₹{opt_info.get("strike","")}'
        trade_rows += (
            f'<tr style="border-bottom:1px solid #f1f5f9">'
            f'<td style="padding:5px 8px;font-family:monospace;font-size:12px">'
            f'{t["symbol"]}{exp_badge}</td>'
            f'<td style="padding:5px 8px;font-size:11px;color:#64748b">{detail}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:12px">{t["fy"]}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:12px">{t["qty"]:,}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:12px">{_fmt(t["buy_value"])}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:12px">{"—" if t.get("expired") else _fmt(t["sell_value"])}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:12px;{pnl_s}">{_fmt(pnl)}</td>'
            f'<td style="padding:5px 8px;font-size:11px;color:#64748b">{t["source"]}</td>'
            f'</tr>'
        )

    # KPI badges
    total_opt_loss = opt["total_pnl"]
    burn = opt["premium_burned"]
    total_opt_style = _pnl_style(total_opt_loss)

    html = f"""
<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin:20px 0">
  <h3 style="margin-top:0;color:#ef4444;font-size:16px">
    F&amp;O / Options Trading Analysis
    <span style="font-size:11px;color:#94a3b8;margin-left:8px">from Zerodha statements</span>
  </h3>
  {warn_html}

  <!-- KPI strip -->
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
    <div style="background:#f8fafc;border-radius:8px;padding:12px 16px;min-width:120px;text-align:center">
      <div style="font-size:11px;color:#64748b">Total Option P&amp;L</div>
      <div style="font-size:20px;font-weight:bold;{total_opt_style}">{_fmt(total_opt_loss)}</div>
    </div>
    <div style="background:#fef2f2;border-radius:8px;padding:12px 16px;min-width:120px;text-align:center">
      <div style="font-size:11px;color:#64748b">Premium Burned 🔥</div>
      <div style="font-size:20px;font-weight:bold;color:#ef4444">{_fmt(-burn) if burn else "₹0"}</div>
    </div>
    <div style="background:#f8fafc;border-radius:8px;padding:12px 16px;min-width:120px;text-align:center">
      <div style="font-size:11px;color:#64748b">Win Rate</div>
      <div style="font-size:20px;font-weight:bold;color:{'#22c55e' if opt['win_rate']>=50 else '#ef4444'}">{opt['win_rate']:.0f}%</div>
    </div>
    <div style="background:#f8fafc;border-radius:8px;padding:12px 16px;min-width:120px;text-align:center">
      <div style="font-size:11px;color:#64748b">Total Trades</div>
      <div style="font-size:20px;font-weight:bold;color:#0f172a">{full['all_trades']}</div>
    </div>
    <div style="background:#f8fafc;border-radius:8px;padding:12px 16px;min-width:120px;text-align:center">
      <div style="font-size:11px;color:#64748b">Expired Worthless</div>
      <div style="font-size:20px;font-weight:bold;color:#ef4444">{opt['expired_count']}</div>
    </div>
  </div>

  <!-- FY breakdown -->
  <h4 style="color:#334155;font-size:13px;margin:15px 0 6px">By Financial Year</h4>
  <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:16px">
    <thead>
      <tr style="background:#f1f5f9">
        <th style="padding:6px 8px;text-align:left">FY</th>
        <th style="padding:6px 8px;text-align:right">Trades</th>
        <th style="padding:6px 8px;text-align:right">Total P&amp;L</th>
        <th style="padding:6px 8px;text-align:right">W/L</th>
        <th style="padding:6px 8px;text-align:right">Win%</th>
        <th style="padding:6px 8px;text-align:right">Premium Burned</th>
      </tr>
    </thead>
    <tbody>{fy_rows}</tbody>
  </table>

  <!-- Individual trades -->
  <h4 style="color:#334155;font-size:13px;margin:15px 0 6px">All F&amp;O Trades (sorted by P&amp;L)</h4>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead>
      <tr style="background:#f1f5f9">
        <th style="padding:5px 8px;text-align:left">Symbol</th>
        <th style="padding:5px 8px;text-align:left">Detail</th>
        <th style="padding:5px 8px;text-align:right">FY</th>
        <th style="padding:5px 8px;text-align:right">Qty</th>
        <th style="padding:5px 8px;text-align:right">Buy Value</th>
        <th style="padding:5px 8px;text-align:right">Sell Value</th>
        <th style="padding:5px 8px;text-align:right">P&amp;L</th>
        <th style="padding:5px 8px;text-align:left">Source</th>
      </tr>
    </thead>
    <tbody>{trade_rows}</tbody>
  </table>
  </div>

  <!-- Lessons panel -->
  <div style="background:#fefce8;border:1px solid #fde047;border-radius:8px;padding:12px 16px;margin-top:16px">
    <strong style="color:#854d0e">Key Insights from F&amp;O History:</strong>
    <ul style="margin:6px 0;padding-left:20px;color:#713f12;font-size:12px">
      <li>Options expired worthless = full premium lost. Consider strict stop-loss: exit when option loses 30-50% of premium.</li>
      <li>INOXWIND25JUL172.5CE &amp; TRENT25JUL5700CE expired Jul 2025: ₹42,075 total loss.</li>
      <li>These were OTM call options on volatile small/mid-caps — high risk of expiry worthless.</li>
      <li>Next time: Buy closer-to-the-money options OR use spreads to cap risk.</li>
      <li>Set alert: Sell option when intrinsic value drops below 30% of purchase price.</li>
    </ul>
  </div>
</div>
"""
    return html


def generate_standalone_html(out_path: Path = None) -> Path:
    """Generate a standalone F&O analysis HTML file."""
    out_path = out_path or ROOT / "reports" / "india_fo_analysis.html"
    panel = build_fo_analysis_html()
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F&O / Options Trading Analysis</title>
<style>
  body {{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        margin:0;padding:20px;background:#f8fafc;color:#1e293b}}
</style>
</head><body>
<h1>F&O / Options Trading Analysis</h1>
<p style="color:#64748b">Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
{panel}
</body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        if sys.platform == "win32":  # cp1252 fix — not needed on macOS/Linux (already UTF-8)
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    ap = argparse.ArgumentParser(description="F&O Options Loss Analyzer")
    ap.add_argument("--html",    action="store_true", help="Generate standalone HTML report")
    ap.add_argument("--folder",  default=None,        help="Override zerodha-statement folder")
    ap.add_argument("--out",     default=None,        help="Output HTML path")
    args = ap.parse_args()

    folder = Path(args.folder) if args.folder else None
    trades = scan_all_zerodha_fo(folder)

    if not trades:
        print("No F&O trades found in statements.")
        sys.exit(0)

    summary = compute_fo_summary(trades)
    opt = summary["options"]
    fut = summary["futures"]

    print("=" * 60)
    print("  F&O TRADING ANALYSIS — ALL YEARS")
    print("=" * 60)
    print(f"\nTotal F&O trades found:    {summary['all_trades']}")
    print(f"  Options trades:          {opt['count']}")
    print(f"  Futures trades:          {fut['count']}")
    print()
    print("OPTIONS SUMMARY:")
    print(f"  Total P&L:               INR {opt['total_pnl']:+,.2f}")
    print(f"  Win rate:                {opt['win_rate']:.1f}%  ({opt['wins']}W / {opt['losses']}L)")
    print(f"  Premium burned (expired):{opt['premium_burned']:,.2f}")
    print(f"  Options expired worthless: {opt['expired_count']}")
    print()
    print("BY FINANCIAL YEAR:")
    for fy, st in sorted(summary["by_fy"].items()):
        print(f"  {fy}: P&L={st['total_pnl']:+,.0f}  {st['wins']}W/{st['losses']}L  "
              f"burned={st['premium_burned']:,.0f}")
    print()
    print("ALL TRADES (sorted by P&L):")
    for t in sorted(trades, key=lambda x: x["pnl"]):
        exp = " [EXPIRED]" if t.get("expired") else ""
        print(f"  {t['symbol']:30s} {t['fy']}  P&L={t['pnl']:+9,.2f}  "
              f"buy={t['buy_value']:9,.2f}  sell={t['sell_value']:9,.2f}{exp}")

    if args.html:
        out = Path(args.out) if args.out else None
        out_path = generate_standalone_html(out)
        print(f"\nHTML saved: {out_path}")
