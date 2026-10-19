#!/usr/bin/env python3
"""
Australian Capital Gains Tax (CGT) Analysis — Webull AU
=========================================================
Reads your Webull AU order-history CSV export and produces a
per-Australian-Financial-Year CGT summary with FIFO cost-basis matching
(same logic as cgt_analysis.py for Moomoo).

Australian FY:  1 July – 30 June
CGT discount:   50% off gains for assets held >= 12 months (individuals)
Capital losses: Offset gains (no 50% discount on losses)
Cost basis:     FIFO (First In, First Out) — standard ATO method

── How to export your transaction history from Webull AU ────────────────────
  Desktop app (Windows/Mac):
    1. Open Webull → click your account icon → "My Portfolio"
    2. Select "Orders" tab → "History" → set date range → "Export"
    3. Save the CSV file

  Web browser (app.webull.com.au):
    1. Log in → "Orders" → "Order History"
    2. Set date range → click the export/download icon

  The script auto-detects common Webull CSV column layouts.
─────────────────────────────────────────────────────────────────────────────

Usage:
    python webull_cgt_analysis.py orders.csv
    python webull_cgt_analysis.py orders.csv --out cgt_report.html

Requirements:
    pip install pandas

NB: USD/HKD-denominated trades need AUD FX conversion (RBA rate on trade date)
    for your actual ATO lodgement. Consult a registered tax agent.
"""

import sys
import os
import argparse
from datetime import datetime, date
from collections import defaultdict
import pandas as pd

# ─── Australian FY helpers ────────────────────────────────────────────────────

def au_fy(d: date) -> str:
    """Return 'FY2024' label for a given date (FY ends 30 Jun)."""
    year = d.year if d.month >= 7 else d.year - 1
    return f"FY{year + 1}"


def infer_currency(symbol: str, exchange: str = "") -> str:
    """
    Infer currency from exchange code or symbol suffix.
    Webull AU returns exchangeCode like 'ASX', 'NYSE', 'NASDAQ', 'CBOE', etc.
    """
    exc = (exchange or "").upper()
    if exc in ("ASX", "ASX2", "CHI-X AU"):
        return "AUD"
    if exc in ("NYSE", "NASDAQ", "AMEX", "CBOE", "ARCA", "BATS"):
        return "USD"
    if exc in ("HKEX", "SEHK"):
        return "HKD"
    # Fallback: Australian symbols often have no dot; US symbols tend to be pure alpha
    return "AUD" if exc.startswith("ASX") else "USD"


# ─── FIFO matching engine ─────────────────────────────────────────────────────

class FifoLedger:
    """
    Maintains a per-ticker FIFO queue of buy lots.
    Consumes oldest lots first when a sell arrives.
    """

    def __init__(self):
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
                f"SELL {qty} {code} on {dt} but no BUY lots found — skipping."
            )
            return

        while remaining > 0 and lots:
            lot_dt, lot_price, lot_qty, lot_currency = lots[0]
            matched_qty = min(remaining, lot_qty)
            hold_days   = (dt - lot_dt).days
            long_term   = hold_days >= 365
            gross_gain  = (price - lot_price) * matched_qty

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
                f"SELL {qty} {code} on {dt}: {remaining:.4f} units unmatched "
                f"(bought before earliest fetched history?)."
            )

    def open_lots(self) -> list[dict]:
        result = []
        for code, lots in self._lots.items():
            for lot_dt, lot_price, lot_qty, currency in lots:
                hold_days = (date.today() - lot_dt).days
                result.append({
                    "code":      code,
                    "currency":  currency,
                    "buy_date":  lot_dt,
                    "qty":       lot_qty,
                    "buy_price": lot_price,
                    "hold_days": hold_days,
                    "term":      "Long" if hold_days >= 365 else "Short",
                })
        return result


# ─── CSV loader — Webull AU order history ────────────────────────────────────

# Column-name aliases: (canonical_name -> list of possible CSV header names)
_COL_ALIASES = {
    "symbol":   ["symbol", "ticker", "stock", "scrip"],
    "action":   ["side", "action", "type", "order side", "direction"],
    "price":    ["avg price", "average price", "price", "fill price",
                 "filled price", "avg. price", "executed price"],
    "qty":      ["filled qty", "qty", "quantity", "filled quantity",
                 "amount", "shares", "units"],
    "dt":       ["order time", "time", "date", "filled time", "fill time",
                 "filled date", "order date", "transaction date", "trade date"],
    "exchange": ["exchange", "market", "exchange code", "market code"],
    "status":   ["status"],
}


def _find_col(columns_lower: list[str], canonical: str) -> str | None:
    """Return the first CSV column header that matches any alias for `canonical`."""
    for alias in _COL_ALIASES[canonical]:
        if alias in columns_lower:
            return columns_lower.index(alias)
    return None


def load_orders_csv(filepath: str) -> pd.DataFrame:
    """
    Read a Webull AU order-history CSV and return a normalised DataFrame with
    columns: code, side, price, qty, dt, currency.

    The function handles common variations in Webull's CSV column names.
    """
    if not os.path.exists(filepath):
        print(f"ERROR: CSV file not found: {filepath}")
        sys.exit(1)

    # Try to sniff the delimiter (comma or tab)
    with open(filepath, encoding="utf-8-sig") as f:
        sample = f.read(2048)
    sep = "\t" if sample.count("\t") > sample.count(",") else ","

    raw = pd.read_csv(filepath, sep=sep, encoding="utf-8-sig", dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    cols_lower   = [c.lower() for c in raw.columns]

    # ── Locate required columns ───────────────────────────────────────────
    required = ["symbol", "action", "price", "qty", "dt"]
    idx = {}
    missing = []
    for key in required:
        found = _find_col(cols_lower, key)
        if found is None:
            missing.append(key)
        else:
            idx[key] = raw.columns[found]

    # Optional columns
    exc_col = None
    exc_idx = _find_col(cols_lower, "exchange")
    if exc_idx is not None:
        exc_col = raw.columns[exc_idx]

    if missing:
        print(f"\nERROR: Could not find these columns in the CSV: {missing}")
        print(f"       Available columns: {list(raw.columns)}")
        print("\nExpected column names (any variation):")
        for key in missing:
            print(f"  {key:>8}: {_COL_ALIASES[key]}")
        sys.exit(1)

    rows = []
    for _, row in raw.iterrows():
        try:
            action = str(row[idx["action"]]).strip().upper()
            # Accept BUY/SELL and common variants
            if "BUY" in action:
                action = "BUY"
            elif "SELL" in action:
                action = "SELL"
            else:
                continue  # skip non-trade rows (e.g. dividends, fees)

            # Skip if marked as not filled
            status_idx = _find_col(cols_lower, "status")
            if status_idx is not None:
                status_val = str(row[raw.columns[status_idx]]).strip().upper()
                if status_val not in ("", "FILLED", "COMPLETED", "EXECUTED", "NAN"):
                    continue

            symbol   = str(row[idx["symbol"]]).strip().upper()
            if not symbol or symbol in ("", "NAN"):
                continue

            exchange = str(row[exc_col]).strip().upper() if exc_col else ""

            # Build code prefix
            if exchange in ("ASX", "ASX2", "CBOE AU", "CHI-X AU"):
                code = f"AU.{symbol}"
            elif exchange in ("NYSE", "NASDAQ", "AMEX", "CBOE", "ARCA", "BATS", "NYSE ARCA"):
                code = f"US.{symbol}"
            elif exchange in ("HKEX", "SEHK", "HKSE"):
                code = f"HK.{symbol}"
            elif exchange:
                code = f"{exchange}.{symbol}"
            else:
                # Heuristic: ASX symbols are 3-5 alpha chars; US symbols can be longer
                # Default to ASX for an AU account if no exchange info
                code = f"AU.{symbol}"

            price_str = str(row[idx["price"]]).replace(",", "").strip()
            qty_str   = str(row[idx["qty"]]).replace(",", "").strip()
            price = float(price_str)
            qty   = float(qty_str)
            if price <= 0 or qty <= 0:
                continue

            dt_str = str(row[idx["dt"]]).strip()
            # Parse date — Webull AU exports ISO format (YYYY-MM-DD HH:MM:SS)
            # Try ISO8601 first; fall back to dayfirst for localised formats
            try:
                dt = pd.to_datetime(dt_str).date()
            except Exception:
                dt = pd.to_datetime(dt_str, dayfirst=True).date()

            currency = infer_currency(symbol, exchange)

            rows.append({
                "code":     code,
                "side":     action,
                "price":    price,
                "qty":      qty,
                "dt":       dt,
                "currency": currency,
                "raw_time": dt_str,
            })

        except Exception as e:
            print(f"  WARNING: Could not parse row (skipping): {dict(row)} — {e}")
            continue

    if not rows:
        print("ERROR: No valid BUY/SELL rows found in the CSV.")
        print("       Check that the file contains filled order history.")
        sys.exit(1)

    df = pd.DataFrame(rows).sort_values("raw_time").reset_index(drop=True)
    return df


# ─── PDF loader — Webull AU PDF statements ───────────────────────────────────

# Normalise company-name-as-ticker back to real NASDAQ/NYSE tickers
# (pdfplumber sometimes picks up the NVIDIA brand name instead of NVDA)
_SYMBOL_NORMALISE = {
    "NVIDIA": "NVDA",
    "TESLA":  "TSLA",
    "APPLE":  "AAPL",
    "GOOGLE": "GOOGL",
    "AMAZON": "AMZN",
    "META":   "META",   # stays same, listed just for clarity
}


# ─── yfinance live price fetcher ────────────────────────────────────────────

def _yf_ticker(symbol: str, currency: str, exchange: str = "") -> str:
    """Map a Webull symbol to a yfinance ticker string."""
    sym = symbol.upper()
    curr = currency.upper()
    if curr == "AUD" or exchange in ("ASX", "XASX", "CBOE AU", "CHI-X AU"):
        return f"{sym}.AX"
    if curr in ("HKD", "CNH") or exchange in ("HKEX", "SEHK"):
        # Pad HK symbols to 4 digits (e.g. 7200 → 07200 → 7200.HK)
        digits = sym.lstrip("0") or "0"
        padded = digits.zfill(4)
        return f"{padded}.HK"
    return sym  # US stocks: as-is


def fetch_live_prices(holdings: list[dict]) -> dict[str, dict]:
    """
    Fetch current market prices for holdings using yfinance.
    Returns {symbol: {'price': float, 'change_pct': float, 'name': str}}.
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  WARNING: yfinance not installed — live prices unavailable (pip install yfinance)")
        return {}

    ticker_map: dict[str, str] = {}  # yf_ticker -> original_symbol
    for h in holdings:
        yt = _yf_ticker(h["symbol"], h["currency"], h.get("exchange", ""))
        ticker_map[yt] = h["symbol"]

    if not ticker_map:
        return {}

    result: dict[str, dict] = {}
    yf_symbols = list(ticker_map.keys())

    print(f"  Fetching live prices for {len(yf_symbols)} symbols via yfinance …")
    try:
        tickers = yf.Tickers(" ".join(yf_symbols))
        for yt, orig_sym in ticker_map.items():
            try:
                info = tickers.tickers[yt].fast_info
                price = getattr(info, "last_price", None) or 0.0
                prev  = getattr(info, "previous_close", None) or 0.0
                chg   = ((price - prev) / prev * 100) if prev else 0.0
                result[orig_sym] = {
                    "price": round(price, 4),
                    "change_pct": round(chg, 2),
                    "name": "",
                }
            except Exception:
                result[orig_sym] = {"price": 0.0, "change_pct": 0.0, "name": ""}
    except Exception as exc:
        print(f"  WARNING: yfinance batch fetch failed: {exc}")

    fetched_count = sum(1 for v in result.values() if v["price"] > 0)
    print(f"  Live prices fetched: {fetched_count}/{len(yf_symbols)} symbols")
    return result

def load_from_pdfs(pdf_folder: str) -> tuple[pd.DataFrame, list[dict]]:
    """
    Parse all Webull AU PDF statements from `pdf_folder`.

    Returns:
        (deals_df, holdings)
    Where deals_df has columns: code, side, price, qty, dt, currency
    And holdings is a list of current position dicts from the most recent monthly.
    """
    # Late import to avoid hard dependency if only using CSV
    try:
        from webull_statement_reader import parse_all_pdfs
    except ImportError:
        print("ERROR: webull_statement_reader.py not found next to this script.")
        sys.exit(1)

    data     = parse_all_pdfs(pdf_folder)
    trades   = data["trades"]
    holdings = data["holdings"]

    rows = []
    for t in trades:
        sym  = _SYMBOL_NORMALISE.get(t["symbol"], t["symbol"])
        curr = t["currency"]
        exc  = t.get("exchange", "")

        # Determine code prefix
        if curr == "AUD" or exc in ("ASX", "XASX"):
            code = f"AU.{sym}"
        elif curr in ("HKD", "CNH") or exc in ("HKEX", "SEHK"):
            code = f"HK.{sym}"
        else:
            code = f"US.{sym}"

        rows.append({
            "code":     code,
            "side":     t["side"],
            "price":    t["price"],
            "qty":      t["qty"],
            "dt":       t["trade_date"],
            "currency": curr,
            "raw_time": str(t["trade_date"]),
        })

    if not rows:
        print("WARNING: No trades found in PDF statements.")
        return pd.DataFrame(columns=["code","side","price","qty","dt","currency"]), holdings

    df = pd.DataFrame(rows).sort_values("raw_time").reset_index(drop=True)
    return df, holdings


def get_data(pdf_dir: str) -> dict:
    """
    High-level entry point for combined_cgt_report.py.
    Loads PDF statements, runs FIFO matching, and returns a dict
    parallel to the structure that ibkr_cgt_analysis.get_data() returns.

    Returns keys:
      matched       – list[dict] of closed CGT events
      open_lots     – list[dict] of remaining FIFO lots
      fy_sum_df     – pd.DataFrame FY summary
      holdings      – list[dict] current holdings from latest monthly statement
      live_prices   – dict[str, dict] live prices from yfinance
      warnings      – list[str]
      fetch_start   – str date used as range start
    """
    deals_df, holdings = load_from_pdfs(pdf_dir)
    ledger = FifoLedger()
    if not deals_df.empty:
        for _, row in deals_df.iterrows():
            if row["side"] == "BUY":
                ledger.buy(row["code"], row["dt"], row["price"], row["qty"], row["currency"])
            else:
                ledger.sell(row["code"], row["dt"], row["price"], row["qty"], row["currency"])

    # Fetch live prices for holdings
    live_prices = fetch_live_prices(holdings) if holdings else {}

    fetch_start = str(deals_df["dt"].min()) if not deals_df.empty else "n/a"
    return {
        "matched":     ledger.matched,
        "open_lots":   ledger.open_lots(),
        "fy_sum_df":   fy_summary(ledger.matched),
        "holdings":    holdings,
        "live_prices": live_prices,
        "warnings":    ledger.warnings,
        "fetch_start": fetch_start,
    }


# ─── CGT summary ──────────────────────────────────────────────────────────────

def fy_summary(matched: list[dict]) -> pd.DataFrame:
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
            "FY":             fy,
            "Currency":       currency,
            "ST Gains":       round(st_gains, 2),
            "ST Losses":      round(st_losses, 2),
            "LT Gross Gains": round(lt_gains, 2),
            "LT Losses":      round(lt_losses, 2),
            "CGT Discount":   round(discount, 2),
            "Net Taxable":    round(net_taxable, 2),
            "# Trades":       len(grp),
        })

    return pd.DataFrame(rows).sort_values(["FY", "Currency"]).reset_index(drop=True)


# ─── HTML report ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Australian CGT Analysis – Webull AU</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#0f1117; color:#e0e0e0; margin:0; padding:20px; }}
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
</style>
</head>
<body>
<header>
<h1>🇦🇺 Australian CGT Analysis — Webull AU</h1>
<p>Generated: {generated} &nbsp;|&nbsp; Source: <code>{csv_source}</code></p>

<p class="note">
  <strong>Disclaimer:</strong> USD/HKD-denominated trades are reported in their <em>native currency</em>.
  Under Australian tax law, all foreign-currency amounts must be converted to AUD using the RBA rate
  on each trade date. Consult a registered tax agent for your definitive CGT position.
  This tool uses <strong>FIFO</strong> cost-basis matching (standard ATO method).
  The 50% CGT discount applies only to <em>gains</em> (not losses) held &ge; 12 months by individuals.
  Analysis covers all orders in the exported CSV file.
</p>
</header>
<main>

{warnings_html}

{kpi_cards}

<h2>Per-Financial-Year Summary</h2>
{fy_summary_table}

<details class="fy-section" style="border-color:#2d5a3d">
<summary style="cursor:pointer;color:#06d6a0;font-weight:600;font-size:1rem;padding:4px 0">&#x1F4B0; Open Lots (Unrealised CGT) &#x2014; click to expand</summary>
<div style="margin-top:12px">
<p class="note">These become CGT events when sold. Cost basis from FIFO matching. Live prices not available from CSV — check your broker for current market value.</p>
{open_positions_table}
</div>
</details>

{holdings_section}

{fy_sections}

</main>
</body>
</html>"""


def _fmt(val, decimals=2):
    val = round(float(val), decimals)
    cls = "pos" if val > 0 else ("neg" if val < 0 else "")
    prefix = "+" if val > 0 else ""
    return f'<span class="{cls}">{prefix}{val:,.{decimals}f}</span>'


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
    return f"<table data-sortable><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


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
            "code", "currency", "term", "buy_date", "sell_date", "hold_days",
            "qty", "buy_price", "sell_price", "gross_gain", "cgt_discount", "taxable_gain"
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


def build_open_positions_table(open_lots: list[dict]) -> str:
    """Show FIFO open lots from CSV (no live prices — export fresh to get current prices)."""
    if not open_lots:
        return "<p><em>No open lots found in the CSV — all bought shares have been sold.</em></p>"

    rows_out = []
    for lot in open_lots:
        rows_out.append({
            "code":       lot["code"],
            "currency":   lot["currency"],
            "buy_date":   str(lot["buy_date"]),
            "hold_days":  lot["hold_days"],
            "term":       lot["term"],
            "qty":        lot["qty"],
            "buy_price":  round(lot["buy_price"], 4),
            "cost_basis": round(lot["buy_price"] * lot["qty"], 2),
        })

    return df_to_html(pd.DataFrame(rows_out))


def build_holdings_section(holdings: list[dict], live_prices: dict[str, dict] | None = None) -> str:
    """Build HTML section showing current portfolio with live prices and consolidation."""
    if not holdings:
        return ""
    from datetime import date as _date

    as_of = holdings[0].get("as_of_date", "") if holdings else ""
    today = _date.today()
    live_prices = live_prices or {}

    rows_out = []
    # Per-currency totals
    currency_totals: dict[str, dict] = {}  # curr -> {cost, market_val, pnl}
    # FX rates for AUD conversion
    fx = {"USD": 1.58, "HKD": 0.205, "CNH": 0.205}

    for h in sorted(holdings, key=lambda x: x["currency"] + x["symbol"]):
        sym   = h["symbol"]
        name  = h.get("name", "")
        qty   = h["qty"]
        avg   = h["avg_price"]
        cost  = h["cost_basis"]
        stmt_close = h["close_price"]
        curr  = h["currency"]
        exc   = h["exchange"]

        # Use live price if available, else fall back to statement close
        lp = live_prices.get(sym, {})
        current_price = lp.get("price", 0.0) or stmt_close
        day_change    = lp.get("change_pct", 0.0)
        price_source  = "Live" if lp.get("price", 0) > 0 else "Statement"

        market_val = round(current_price * qty, 2)
        pnl = round(market_val - cost, 2)
        pct = (pnl / cost * 100) if cost != 0 else 0.0

        # Accumulate per-currency totals
        if curr not in currency_totals:
            currency_totals[curr] = {"cost": 0.0, "market_val": 0.0, "pnl": 0.0}
        currency_totals[curr]["cost"]       += cost
        currency_totals[curr]["market_val"]  += market_val
        currency_totals[curr]["pnl"]         += pnl

        rows_out.append({
            "Symbol":        sym,
            "Name":          name[:30] if name else "",
            "Qty":           int(qty),
            "Avg Cost":      f"{avg:,.4f}",
            "Cost Basis":    f"{cost:,.2f}",
            "Cur. Price":    f"{current_price:,.4f}",
            "Day %":         f'{day_change:+.1f}%' if price_source == "Live" else "—",
            "Market Val":    f"{market_val:,.2f}",
            "P/L":           f'+{pnl:,.2f}' if pnl >= 0 else f'{pnl:,.2f}',
            "P/L %":         f'+{pct:.1f}%' if pct >= 0 else f'{pct:.1f}%',
            "Ccy":           curr,
            "Src":           price_source,
        })

    # Build header
    headers = "".join(f"<th>{c}</th>" for c in rows_out[0].keys())
    table_rows = ""
    for r in rows_out:
        cells = ""
        for col, val in r.items():
            cls = ""
            if col in ("P/L", "P/L %", "Day %"):
                sval = str(val)
                if sval.startswith("+"):
                    cls = ' class="pos"'
                elif sval.startswith("-"):
                    cls = ' class="neg"'
            cells += f"<td{cls}>{val}</td>"
        table_rows += f"<tr>{cells}</tr>"

    # Consolidation totals per currency
    totals_html = ""
    grand_aud_cost = 0.0
    grand_aud_mval = 0.0
    for curr in sorted(currency_totals.keys()):
        ct = currency_totals[curr]
        pnl_class = "pos" if ct["pnl"] >= 0 else "neg"
        pnl_sign  = "+" if ct["pnl"] >= 0 else ""
        pct       = (ct["pnl"] / ct["cost"] * 100) if ct["cost"] else 0
        pct_sign  = "+" if pct >= 0 else ""
        # Convert to AUD
        rate = fx.get(curr, 1.0) if curr != "AUD" else 1.0
        grand_aud_cost += ct["cost"] * rate
        grand_aud_mval += ct["market_val"] * rate
        totals_html += (
            f'<tr style="font-weight:600;border-top:2px solid #3a4a6a">'
            f'<td colspan="4">{curr} Total</td>'
            f'<td>{ct["cost"]:,.2f}</td>'
            f'<td></td><td></td>'
            f'<td>{ct["market_val"]:,.2f}</td>'
            f'<td class="{pnl_class}">{pnl_sign}{ct["pnl"]:,.2f}</td>'
            f'<td class="{pnl_class}">{pct_sign}{pct:.1f}%</td>'
            f'<td>{curr}</td><td></td></tr>'
        )

    grand_pnl = grand_aud_mval - grand_aud_cost
    grand_pnl_pct = (grand_pnl / grand_aud_cost * 100) if grand_aud_cost else 0
    grand_cls = "pos" if grand_pnl >= 0 else "neg"

    # Summary card
    summary_card = f"""
<div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:16px">
  <div style="background:#1a2540;border-radius:8px;padding:14px 22px;border:1px solid #2c3a55;min-width:200px">
    <div style="color:#7ecbf5;font-size:0.78em;text-transform:uppercase">Total Cost (AUD equiv.)</div>
    <div style="color:#e0e0e0;font-size:1.4em;font-weight:700">A${grand_aud_cost:,.0f}</div>
  </div>
  <div style="background:#1a2540;border-radius:8px;padding:14px 22px;border:1px solid #2c3a55;min-width:200px">
    <div style="color:#7ecbf5;font-size:0.78em;text-transform:uppercase">Market Value (AUD equiv.)</div>
    <div style="color:#e0e0e0;font-size:1.4em;font-weight:700">A${grand_aud_mval:,.0f}</div>
  </div>
  <div style="background:#1a2540;border-radius:8px;padding:14px 22px;border:1px solid #2c3a55;min-width:200px">
    <div style="color:#7ecbf5;font-size:0.78em;text-transform:uppercase">Unrealised P/L (AUD)</div>
    <div class="{grand_cls}" style="font-size:1.4em;font-weight:700">{"+" if grand_pnl >= 0 else ""}A${grand_pnl:,.0f}
      <span style="font-size:0.6em">({("+" if grand_pnl_pct >= 0 else "")}{grand_pnl_pct:.1f}%)</span>
    </div>
  </div>
  <div style="background:#1a2540;border-radius:8px;padding:14px 22px;border:1px solid #2c3a55;min-width:200px">
    <div style="color:#7ecbf5;font-size:0.78em;text-transform:uppercase">Positions</div>
    <div style="color:#e0e0e0;font-size:1.4em;font-weight:700">{len(holdings)}</div>
  </div>
</div>"""

    has_live = any(live_prices.get(h["symbol"], {}).get("price", 0) > 0 for h in holdings)
    price_note = ("Prices are <strong>live</strong> from yfinance."
                  if has_live else
                  "Prices are from statement closing prices (yfinance unavailable).")

    return f"""
<details class="fy-section" style="border-color:#3d5a2d;margin-top:16px" open>
<summary style="cursor:pointer;color:#81c784;font-weight:600;font-size:1rem;padding:4px 0">
  &#x1F4CA; Current Portfolio &mdash; {len(holdings)} positions &mdash; click to expand
</summary>
<div style="margin-top:12px">
{summary_card}
<p class="note">{price_note}
  Holdings from latest monthly statement ({as_of}), adjusted for subsequent trades.
  FX rates: USD/AUD~1.58, HKD/AUD~0.205 (approx.).</p>
<table data-sortable><thead><tr>{headers}</tr></thead><tbody>{table_rows}{totals_html}</tbody></table>
</div>
</details>"""


def generate_html(fy_sum_df, matched, open_lots, warnings, csv_path: str,
                  holdings: list[dict] | None = None,
                  live_prices: dict[str, dict] | None = None) -> str:
    warnings_html = ""
    if warnings:
        items = "\n".join(f'<div class="warn">&#9888; {w}</div>' for w in warnings)
        warnings_html = f"<h2>Warnings</h2>{items}"

    fy_summary_html = df_to_html(
        fy_sum_df,
        numeric_cols=["ST Gains", "ST Losses", "LT Gross Gains", "LT Losses", "Net Taxable"],
        discount_cols=["CGT Discount"],
    ) if not fy_sum_df.empty else "<p><em>No closed (sell) trades found.</em></p>"

    html = HTML_TEMPLATE.format(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        csv_source=os.path.basename(csv_path),
        warnings_html=warnings_html,
        kpi_cards=_build_kpi_cards(matched),
        fy_summary_table=fy_summary_html,
        fy_sections=build_fy_sections(matched),
        open_positions_table=build_open_positions_table(open_lots),
        holdings_section=build_holdings_section(holdings or [], live_prices),
    )
    _project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    from html_utils import JS_TABLE_SORT
    return html.replace("</main>", f"<script>{JS_TABLE_SORT}</script>\n</main>")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # cp1252 fix
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        description="Australian CGT analysis from Webull AU order-history CSV or PDF statements",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python webull_cgt_analysis.py --pdf-dir ../../webull-statement
  python webull_cgt_analysis.py orders.csv
  python webull_cgt_analysis.py orders.csv --out webull_cgt_2026.html
        """
    )
    parser.add_argument("csv", nargs="?", help="Path to Webull AU order history CSV export")
    parser.add_argument("--pdf-dir", metavar="DIR",
                        help="Folder containing Webull AU PDF statements (alternative to CSV)")
    parser.add_argument("--out", default="webull_cgt_report.html",
                        help="Output HTML file (default: webull_cgt_report.html)")
    args = parser.parse_args()

    # Require at least one input
    if not args.csv and not args.pdf_dir:
        # Default: look for PDF statements next to the script
        default_pdf_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "webull-statement"
        )
        if os.path.isdir(default_pdf_dir):
            args.pdf_dir = default_pdf_dir
        else:
            parser.error("Provide either a CSV file or --pdf-dir pointing to PDF statements.")

    print("=" * 60)
    print("Australian CGT Analysis — Webull AU")
    print("=" * 60)

    holdings: list[dict] = []
    source_label = ""

    if args.pdf_dir:
        # ── Load from PDF statements ──────────────────────────────
        pdf_dir = os.path.abspath(args.pdf_dir)
        print(f"  Reading PDFs from: {pdf_dir}")
        deals_df, holdings = load_from_pdfs(pdf_dir)
        source_label = pdf_dir
    else:
        # ── Load from CSV ─────────────────────────────────────────
        print(f"  Reading: {args.csv}")
        deals_df = load_orders_csv(args.csv)
        source_label = args.csv

    print(f"  Parsed {len(deals_df)} valid BUY/SELL rows.")
    if not deals_df.empty:
        print(f"  Date range: {deals_df['dt'].min()} to {deals_df['dt'].max()}")
        print(f"  Tickers: {sorted(deals_df['code'].unique())}")

    # ── FIFO matching ────────────────────────────────────────────
    ledger = FifoLedger()
    for _, row in deals_df.iterrows():
        if row["side"] == "BUY":
            ledger.buy(row["code"], row["dt"], row["price"], row["qty"], row["currency"])
        else:
            ledger.sell(row["code"], row["dt"], row["price"], row["qty"], row["currency"])

    matched  = ledger.matched
    warnings = ledger.warnings

    print(f"  Matched {len(matched)} sell events.")
    if warnings:
        print(f"  {len(warnings)} warnings (see report).")

    # ── FY summary ───────────────────────────────────────────────
    fy_sum_df = fy_summary(matched)

    if not fy_sum_df.empty:
        print("\n  FY Summary:")
        print(fy_sum_df.to_string(index=False))
    else:
        print("  No closed trades found — summary is empty.")

    open_lots = ledger.open_lots()
    if open_lots:
        print(f"  {len(open_lots)} open FIFO lots remaining (unrealised CGT).")
    if holdings:
        print(f"  {len(holdings)} current positions from latest monthly statement.")

    # ── Fetch live prices ────────────────────────────────────────────
    live_prices = fetch_live_prices(holdings) if holdings else {}

    # ── HTML report ──────────────────────────────────────────────────
    html = generate_html(fy_sum_df, matched, open_lots, warnings, source_label, holdings, live_prices)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  Report written: {out_path}")

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent.parent.parent / "reports"))
        from report_db import save_cgt_snapshot, archive_report as _arc
        save_cgt_snapshot(fy_sum_df, "Webull")
        _arc(_Path(out_path))
    except Exception as _e:
        print(f"  [report_db] webull_cgt snapshot skipped: {_e}")

    print("=" * 60)


if __name__ == "__main__":
    main()
