#!/usr/bin/env python3
"""
Australian Capital Gains Tax (CGT) Analysis — IBKR
====================================================
Produces a per-Australian-Financial-Year CGT summary with FIFO cost-basis
matching from your IBKR trade history.  Token and Query ID are pre-configured
so the script can run without any arguments.

── Mode 1: Auto-fetch via IBKR Flex Query (default, no downloads needed) ────
  Credentials are stored in FLEX_TOKEN / FLEX_QUERY_ID constants.

  Run with no arguments:
    python ibkr_cgt_analysis.py

  Or override credentials:
    python ibkr_cgt_analysis.py --flex-token TOKEN --flex-query-id QUERYID

── Mode 2: Manual CSV (fallback) ────────────────────────────────────────────
  1. Log in to IBKR Client Portal → Reports → Activity → Activity Statement
  2. Choose custom date range, CSV format, enable Trades + Open Positions
  3. Download the .csv file, then run:
       python ibkr_cgt_analysis.py --statement activity.csv
─────────────────────────────────────────────────────────────────────────────

Performance note on "enable everything" Flex Queries:
  Extra sections (dividends, interest, corporate actions) are parsed and shown
  in the report but do NOT affect trade/FIFO performance — the XML parser
  only iterates the specific element tags it needs (O(n) in total nodes).

NB: Trades in USD/HKD are reported in their *native currency*.  Under Australian
    tax law you must convert to AUD using the RBA rate on each trade date.
    Consult a registered tax agent for your actual CGT position.
    This tool uses FIFO cost-basis matching (standard ATO method).
    The 50% CGT discount applies only to *gains* (not losses) held ≥ 12 months.
"""

import sys
import os
import csv
import argparse
from datetime import datetime, date
from collections import defaultdict
from pathlib import Path

# html_utils.py lives at the project root (two levels up from cgt/ibkr-cgt/)
_ROOT_DIR = str(Path(__file__).resolve().parent.parent.parent)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

import pandas as pd

# ─── Config defaults ──────────────────────────────────────────────────────────

FETCH_START   = "2018-01-01"
FLEX_TOKEN    = "581369287293302825272191"   # IBKR Flex Web Service token
FLEX_QUERY_ID = "1462304"                   # MyFlexQuery

# ─── Australian FY helpers ────────────────────────────────────────────────────

def au_fy(d: date) -> str:
    """Return 'FY2024' for a date (AU FY ends 30 Jun)."""
    year = d.year if d.month >= 7 else d.year - 1
    return f"FY{year + 1}"


# ─── Code / currency helpers ─────────────────────────────────────────────────

def make_code(symbol: str, currency: str) -> str:
    """Construct a display code like AU.CBA, US.AAPL, HK.0700."""
    prefix = {"AUD": "AU", "USD": "US", "HKD": "HK"}.get(currency.upper(), currency[:2])
    return f"{prefix}.{symbol}"


# ─── FIFO matching engine ─────────────────────────────────────────────────────

class FifoLedger:
    """
    Per-ticker FIFO queue of buy lots.  When a sell arrives, lots are consumed
    oldest-first and matched trades (CGT events) are recorded.
    """

    def __init__(self):
        # {code: [[buy_date, price, qty, currency], ...]}
        self._lots: dict[str, list] = defaultdict(list)
        self.matched:  list[dict] = []
        self.warnings: list[str]  = []

    def buy(self, code: str, dt: date, price: float, qty: float, currency: str):
        self._lots[code].append([dt, price, qty, currency])

    def sell(self, code: str, dt: date, price: float, qty: float, currency: str):
        remaining = qty
        lots = self._lots[code]

        if not lots:
            self.warnings.append(
                f"SELL {qty} {code} on {dt} — no BUY lots found. "
                f"Position was likely opened before the Flex Query date range "
                f"(e.g. transferred in via ACAT from Webull). "
                f"Fix: in IBKR Portal → Flex Queries → Edit → add 'Prior Period Positions' section."
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
                f"SELL {qty} {code} on {dt}: {remaining:.4f} units unmatched — "
                f"partial position opened before Flex Query start date "
                f"(or transferred in via ACAT). "
                f"Fix: add 'Prior Period Positions' section in IBKR Flex Query config."
            )

    def open_lots(self) -> list[dict]:
        """Return all unmatched buy lots (still-held positions)."""
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


# ─── IBKR Flex Query Web Service ─────────────────────────────────────────────

_FLEX_REQUEST_URL  = ("https://gdcdyn.interactivebrokers.com"
                      "/Universal/servlet/FlexStatementService.SendRequest")
_FLEX_DOWNLOAD_URL = ("https://gdcdyn.interactivebrokers.com"
                      "/Universal/servlet/FlexStatementService.GetStatement")


def fetch_flex_statement(token: str, query_id: str, start_date: str) -> tuple:
    """
    Auto-fetch IBKR trade history via the Flex Query Web Service (no manual
    CSV download needed).

    Returns the same (trades_df, positions_df) tuple as parse_activity_statement.
    """
    import urllib.request
    import urllib.parse
    import time
    import xml.etree.ElementTree as ET

    # ── Step 1: Request generation of the statement ───────────────────────────
    req_params = urllib.parse.urlencode({
        "v": "3", "t": token, "q": query_id, "type": "AF"
    })
    req_url = f"{_FLEX_REQUEST_URL}?{req_params}"
    print("  Requesting Flex statement from IBKR...")

    # Outer loop: retry on IBKR 1001 ("try again shortly") — up to 5 times, 60s apart
    # Inner loop: retry on transient network errors (DNS glitches, timeouts) — up to 3 times
    xml_str = None
    root = None
    ref_code = err_code = status = dl_base = None
    _MAX_1001_RETRIES = 5

    for _outer in range(_MAX_1001_RETRIES):
        # -- inner: network-level retry --
        for _net in range(3):
            try:
                with urllib.request.urlopen(req_url, timeout=30) as resp:
                    xml_str = resp.read().decode()
                break
            except Exception as _e:
                if _net < 2:
                    _nwait = 5 * (_net + 1)
                    print(f"  Network error (attempt {_net + 1}/3), "
                          f"retrying in {_nwait}s: {_e}")
                    time.sleep(_nwait)
                else:
                    raise RuntimeError(f"Flex request failed: {_e}") from _e

        root      = ET.fromstring(xml_str)
        status    = (root.findtext("Status")        or "").strip()
        ref_code  = (root.findtext("ReferenceCode") or "").strip()
        dl_base   = (root.findtext("Url")           or _FLEX_DOWNLOAD_URL).strip()
        err_code  = (root.findtext("ErrorCode")     or "").strip()

        # ErrorCode=1001: "Statement could not be generated at this time. Please try again shortly."
        if err_code == "1001" and not ref_code:
            if _outer < _MAX_1001_RETRIES - 1:
                print(f"  IBKR not ready (1001), waiting 60s before retry "
                      f"({_outer + 1}/{_MAX_1001_RETRIES})...")
                time.sleep(60)
                continue
            raise RuntimeError(
                "IBKR Flex: ErrorCode=1001 persisted after 5 retries (~5 min). "
                "Check the IBKR Account Management portal — the Flex Query may need "
                "to be re-saved, or the service may be temporarily down."
            )
        break   # got a valid (non-1001) response

    if not ref_code or status.lower() not in ("success", "processing", "generating", "queued"):
        raise RuntimeError(
            f"Flex SendRequest returned unexpected status={status!r}. "
            f"Full response: {ET.tostring(root, encoding='unicode')}"
        )

    print(f"  Statement queued (ref={ref_code}).  Polling for completion...")

    # ── Step 2: Poll until statement is ready ─────────────────────────────────
    dl_params = urllib.parse.urlencode({"v": "3", "t": token, "q": ref_code})
    dl_url    = f"{dl_base}?{dl_params}"

    for attempt in range(30):      # up to ~5 minutes
        time.sleep(10)
        print(f"    Attempt {attempt + 1}/30 ...")
        try:
            with urllib.request.urlopen(dl_url, timeout=60) as resp:
                result = resp.read().decode()
        except Exception as e:
            raise RuntimeError(f"Flex download request failed: {e}") from e

        try:
            r2 = ET.fromstring(result)
        except ET.ParseError:
            # Non-XML response → this is the actual Flex Statement
            break

        # If the root is not a status/error wrapper, the statement is ready
        if r2.tag != "FlexStatementResponse":
            break   # result contains the actual Flex Statement XML

        st2 = (r2.findtext("Status") or "").strip().lower()
        err2 = (r2.findtext("ErrorCode") or "").strip()
        if attempt == 0 or attempt % 5 == 4:
            print(f"      (server status: {st2!r}, code: {err2!r})")
        # 1019 = "Statement generation in progress"; 1001 = "Try again shortly" — keep polling
        if st2 in ("processing", "generating", "queued", "") or err2 in ("1019", "1001"):
            continue
        raise RuntimeError(
            f"Flex GetStatement error (status={st2!r}): "
            f"{ET.tostring(r2, encoding='unicode')}"
        )
    else:
        raise RuntimeError("Flex statement never became ready after 30 attempts (~5 min).")

    print("  Statement received.  Parsing...")
    print(f"  Statement size: {len(result):,} bytes")
    # Save raw XML for diagnostics
    _debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_last_flex.xml")
    try:
        with open(_debug_path, "w", encoding="utf-8") as _f:
            _f.write(result)
    except Exception:
        pass
    return parse_flex_xml(result, start_date)  # (trades_df, positions_df, cash_transactions_df, prior_positions_dict)


def parse_flex_xml(xml_str: str, start_date: str) -> tuple:
    """
    Parse a Flex Statement XML string.

    Returns (trades_df, positions_df, cash_transactions_df):
      trades_df             — stock trades for FIFO matching
      positions_df          — open positions with prices (from OpenPosition section)
      cash_transactions_df  — interest charges/income and dividends (from CashTransaction)

    Extra sections enabled in the Flex Query (dividends, corporate actions, etc.)
    are handled gracefully — the parser only looks at the element tags it needs.
    """
    import xml.etree.ElementTree as ET

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    root     = ET.fromstring(xml_str)

    trade_rows, position_rows, cash_rows, skipped = [], [], [], 0

    for trade in root.iter("Trade"):
        if trade.get("assetCategory") != "STK":
            continue
        try:
            dt = _parse_date(trade.get("tradeDate", ""))
        except ValueError:
            skipped += 1
            continue
        if dt < start_dt:
            skipped += 1
            continue
        currency = trade.get("currency", "USD")
        symbol   = trade.get("symbol", "")
        code     = make_code(symbol, currency)
        try:
            qty   = float(trade.get("quantity", 0))
            price = float(trade.get("tradePrice", 0))
        except ValueError:
            skipped += 1
            continue
        if qty == 0 or price <= 0:
            skipped += 1
            continue
        trade_rows.append({
            "code":     code,
            "symbol":   symbol,
            "currency": currency,
            "dt":       dt,
            "side":     "BUY" if qty > 0 else "SELL",
            "price":    abs(price),
            "qty":      abs(qty),
        })

    for pos in root.iter("OpenPosition"):
        if pos.get("assetCategory") != "STK":
            continue
        currency = pos.get("currency", "USD")
        symbol   = pos.get("symbol", "")
        code     = make_code(symbol, currency)
        try:
            qty        = float(pos.get("position", 0))   # NB: "position" not "quantity"
            cost_price = float(pos.get("costBasisPrice", 0))
            mark_price = float(pos.get("markPrice", 0))
            unreal_pnl = float(pos.get("fifoPnlUnrealized", pos.get("unrealizedPnL", 0)))
        except ValueError:
            continue
        if qty == 0:
            continue
        position_rows.append({
            "code":          code,
            "symbol":        symbol,
            "currency":      currency,
            "qty":           qty,
            "avg_cost":      cost_price,
            "last_price":    mark_price,
            "unrealized_pnl": unreal_pnl,
        })

    # ── PriorPeriodPositions: positions opened before the query date range ─────
    # These appear when the Flex Query period doesn't cover the full history of a
    # position.  They provide cost basis and open date for enriching the report.
    prior_positions_dict: dict = {}  # code → {qty, avg_cost, open_date}
    for ppos in root.iter("PriorPeriodPosition"):
        if ppos.get("assetCategory") != "STK":
            continue
        currency = ppos.get("currency", "USD")
        symbol   = ppos.get("symbol", "")
        code     = make_code(symbol, currency)
        try:
            qty        = float(ppos.get("position", ppos.get("quantity", 0)))
            cost_price = float(ppos.get("costBasisPrice", 0))
        except ValueError:
            continue
        if qty == 0:
            continue
        raw_date = (
            ppos.get("openDateTime")
            or ppos.get("holdingPeriodDateTime")
            or ppos.get("openDate")
            or ppos.get("reportDate")
            or ""
        )
        try:
            open_dt = (
                _parse_date(raw_date.split(";")[0].split(",")[0].strip())
                if raw_date else None
            )
        except ValueError:
            open_dt = None
        prior_positions_dict[code] = {
            "qty":       qty,
            "avg_cost":  cost_price,
            "open_date": open_dt,
        }
    if prior_positions_dict:
        print(f"  Prior period positions: {len(prior_positions_dict)} "
              f"({sorted(prior_positions_dict)})")

    # ── CashTransaction: interest paid/received + dividends ──────────────────
    # These appear when you enable Interest Accruals or Cash Transactions in
    # the Flex Query.  Extra sections do not slow parsing — we just skip
    # elements whose tag we don't recognise.
    INTEREST_TYPES = {
        "Broker Interest Paid",
        "Broker Interest Received",
        "Bond Interest Paid",
        "Bond Interest Received",
    }
    DIVIDEND_TYPES = {"Dividends", "Payment In Lieu Of Dividends", "Withholding Tax"}
    for ct in root.iter("CashTransaction"):
        ct_type = ct.get("type", "").strip()
        if ct_type not in INTEREST_TYPES and ct_type not in DIVIDEND_TYPES:
            continue
        try:
            raw_date = ct.get("dateTime") or ct.get("reportDate") or ct.get("settleDate") or ""
            dt = _parse_date(raw_date.split(",")[0].strip()) if raw_date else None
        except ValueError:
            dt = None
        try:
            amount = float(ct.get("amount", 0))
        except ValueError:
            continue
        if amount == 0:
            continue
        cash_rows.append({
            "date":        str(dt) if dt else "—",
            "type":        ct_type,
            "description": ct.get("description", ""),
            "currency":    ct.get("currency", ""),
            "amount":      amount,
        })

    if skipped:
        print(f"  Skipped {skipped} trade row(s) (before start date or bad data).")

    # Report what was found (helps diagnose Flex Query date range issues)
    fs_elem = root.find(".//FlexStatement")
    if fs_elem is not None:
        from_d = fs_elem.get("fromDate", "?")
        to_d   = fs_elem.get("toDate", "?")
        print(f"  Statement date range: {from_d} → {to_d}")
    print(f"  Parsed: {len(trade_rows)} trade(s), {len(position_rows)} open position(s), "
          f"{len(cash_rows)} cash transaction(s).")

    trades_df = (
        pd.DataFrame(trade_rows).sort_values("dt").reset_index(drop=True)
        if trade_rows else pd.DataFrame()
    )
    positions_df      = pd.DataFrame(position_rows) if position_rows else pd.DataFrame()
    cash_transactions = pd.DataFrame(cash_rows) if cash_rows else pd.DataFrame()
    return trades_df, positions_df, cash_transactions, prior_positions_dict


# ─── Parse IBKR Activity Statement CSV ───────────────────────────────────────

def _parse_date(raw: str) -> date:
    """
    Parse IBKR date/time strings.
    Formats seen: "2024-01-15 09:30:00", "2024-01-15, 09:30:00", "20240115"
    """
    raw = raw.strip().replace(", ", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse IBKR date: {raw!r}")


def parse_activity_statement(csv_path: str, start_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse an IBKR Activity Statement (multi-section CSV).

    Returns:
        trades_df   — DataFrame of stock trades (columns: code, dt, side, price, qty, currency)
        positions_df — DataFrame of open positions from the statement (may be empty if section absent)
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()

    trade_headers:    list[str] | None = None
    position_headers: list[str] | None = None
    trade_rows:    list[dict] = []
    position_rows: list[dict] = []

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if len(row) < 2:
                continue
            section = row[0].strip()
            row_type = row[1].strip()
            data     = row[2:]

            # ── Trades section ─────────────────────────
            if section == "Trades":
                if row_type == "Header":
                    trade_headers = [c.strip() for c in data]
                elif row_type == "Data" and trade_headers:
                    rec = dict(zip(trade_headers, [c.strip() for c in data]))
                    # Only process individual fills for stocks
                    disc = rec.get("DataDiscriminator", "").lower()
                    if disc not in ("order", "fill"):
                        continue
                    if rec.get("Asset Category", "") != "Stocks":
                        continue
                    trade_rows.append(rec)

            # ── Open Positions section (for fallback — no live prices) ──
            elif section == "Open Positions":
                if row_type == "Header":
                    position_headers = [c.strip() for c in data]
                elif row_type == "Data" and position_headers:
                    rec = dict(zip(position_headers, [c.strip() for c in data]))
                    disc = rec.get("DataDiscriminator", "").lower()
                    if disc not in ("summary",):
                        continue
                    if rec.get("Asset Category", "") != "Stocks":
                        continue
                    position_rows.append(rec)

    if not trade_rows:
        print("  WARNING: No stock trades found in the Trades section.")
        print("  Make sure your statement includes the Trades section and has Stocks.")
        return pd.DataFrame(), pd.DataFrame()

    # ── Normalise trades ──────────────────────────────────────────────────────
    normalised = []
    skipped = 0
    for rec in trade_rows:
        try:
            dt = _parse_date(rec.get("Date/Time", ""))
        except ValueError as e:
            print(f"  Skipping row with bad date: {e}")
            skipped += 1
            continue

        if dt < start_dt:
            skipped += 1
            continue

        currency = rec.get("Currency", "AUD").strip()
        symbol   = rec.get("Symbol", "").strip()
        code     = make_code(symbol, currency)

        try:
            qty   = float(rec.get("Quantity", 0))
            price = float(rec.get("T. Price", 0))
        except ValueError:
            skipped += 1
            continue

        if qty == 0 or price <= 0:
            skipped += 1
            continue

        side = "BUY" if qty > 0 else "SELL"
        normalised.append({
            "code":     code,
            "symbol":   symbol,
            "currency": currency,
            "dt":       dt,
            "side":     side,
            "price":    abs(price),
            "qty":      abs(qty),
        })

    if skipped:
        print(f"  Skipped {skipped} rows (before start date, bad data, or non-stock).")

    trades_df = pd.DataFrame(normalised).sort_values("dt").reset_index(drop=True)

    # ── Normalise positions ───────────────────────────────────────────────────
    pos_rows = []
    for rec in position_rows:
        currency = rec.get("Currency", "AUD").strip()
        symbol   = rec.get("Symbol", "").strip()
        code     = make_code(symbol, currency)
        try:
            qty         = float(rec.get("Quantity", 0))
            cost_price  = float(rec.get("Cost Price", 0))
            close_price = float(rec.get("Close Price", 0))
            unreal_pnl  = float(rec.get("Unrealized P/L", 0))
        except ValueError:
            continue
        if qty == 0:
            continue
        pos_rows.append({
            "code":          code,
            "symbol":        symbol,
            "currency":      currency,
            "qty":           qty,
            "avg_cost":      cost_price,
            "last_price":    close_price,   # as-of statement date, not live
            "unrealized_pnl": unreal_pnl,
        })

    positions_df = pd.DataFrame(pos_rows) if pos_rows else pd.DataFrame()
    return trades_df, positions_df, pd.DataFrame()  # no cash transactions from CSV


# ─── CGT summary ──────────────────────────────────────────────────────────────

def fy_summary(matched: list[dict]) -> pd.DataFrame:
    """Per-FY CGT summary grouped by currency."""
    if not matched:
        return pd.DataFrame()
    df  = pd.DataFrame(matched)
    rows = []
    for (fy, currency), grp in df.groupby(["fy", "currency"]):
        short  = grp[grp.term == "Short"]
        long_  = grp[grp.term == "Long"]
        st_gains  = short[short.gross_gain > 0].gross_gain.sum()
        st_losses = short[short.gross_gain < 0].gross_gain.sum()
        lt_gains  = long_[long_.gross_gain > 0].gross_gain.sum()
        lt_losses = long_[long_.gross_gain < 0].gross_gain.sum()
        discount  = long_[long_.gross_gain > 0].cgt_discount.sum()
        net_tax   = short.taxable_gain.sum() + long_.taxable_gain.sum()
        rows.append({
            "FY":             fy,
            "Currency":       currency,
            "ST Gains":       round(st_gains, 2),
            "ST Losses":      round(st_losses, 2),
            "LT Gross Gains": round(lt_gains, 2),
            "LT Losses":      round(lt_losses, 2),
            "CGT Discount":   round(discount, 2),
            "Net Taxable":    round(net_tax, 2),
            "# Trades":       len(grp),
        })
    return pd.DataFrame(rows).sort_values(["FY", "Currency"]).reset_index(drop=True)


# ─── HTML report ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Australian CGT Analysis – IBKR</title>
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
<h1>🇦🇺 Australian CGT Analysis — IBKR</h1>
<p>Generated: {generated}<br>
Statement: {statement_path} &nbsp;|&nbsp; History from: {fetch_start}</p>

<p class="note">
  <strong>Disclaimer:</strong> Trades in USD / HKD are reported in their <em>native currency</em>.
  Under Australian tax law, foreign-currency amounts must be converted to AUD using the RBA rate
  on each trade date. Consult a registered tax agent for your CGT position.
  This tool uses <strong>FIFO</strong> cost-basis matching (standard ATO method).
  The 50% CGT discount applies only to gains (not losses) held ≥ 12 months by individuals.
</p>
</header>
<main>

{warnings_html}

{kpi_cards}

{account_summary_html}

<h2>Per-Financial-Year Summary</h2>
{fy_summary_table}

{current_fy_panel}

{aud_equivalent_section}

<details><summary class="fy-summary">&#x1F4CB; Open Positions (Unrealised CGT) &#x2014; click to expand</summary>
<p class="note" style="margin-top:12px">These positions have unrealised gains/losses. They become CGT events when you sell.
{price_source_note}</p>
{open_positions_table}
</details>

{fy_sections}

{cash_transactions_html}

</main>
</body>
</html>"""


def _fmt(val, decimals=2):
    val = round(float(val), decimals)      # avoid -0.00
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
    return f"<table id='cgt-table' data-sortable><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


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
            "qty", "buy_price", "sell_price", "gross_gain", "cgt_discount", "taxable_gain",
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


def build_account_summary_html(account_summary: dict) -> str:
    """Build an account overview card grid from ib_insync accountValues data."""
    if not account_summary:
        return ""

    # Key tags to show, in display order
    WANTED = [
        ("NetLiquidation",      "Net Liquidation"),
        ("TotalCashValue",      "Total Cash"),
        ("SettledCash",         "Settled Cash"),
        ("AccruedCash",         "Accrued Interest"),
        ("BuyingPower",         "Buying Power"),
        ("GrossPositionValue",  "Gross Position Value"),
        ("UnrealizedPnL",       "Unrealized P&L"),
        ("RealizedPnL",         "Realized P&L"),
        ("MaintMarginReq",      "Maint. Margin Req."),
        ("InitMarginReq",       "Initial Margin Req."),
    ]

    cards = []
    for tag, label in WANTED:
        entry = account_summary.get(tag)
        if not entry:
            continue
        raw = entry.get("value", "")
        cur = entry.get("currency", "")
        try:
            val  = float(raw)
            disp = f"{val:,.2f}"
            if val > 0:
                cls = "pos"
            elif val < 0:
                cls = "neg"
            else:
                cls = ""
            value_html = f'<span class="{cls}">{disp}</span>'
            if cur and cur not in ("BASE", ""):
                value_html += f' <span style="font-size:0.7em;color:#888">{cur}</span>'
        except (ValueError, TypeError):
            value_html = str(raw)
        cards.append(
            f'<div class="card">'
            f'<div class="label">{label}</div>'
            f'<div class="value">{value_html}</div>'
            f'</div>'
        )

    if not cards:
        return ""
    grid = "\n".join(cards)
    return f'<h2>Account Overview (Live)</h2>\n<div class="summary-grid">\n{grid}\n</div>'


def build_cash_transactions_html(ct_df: "pd.DataFrame") -> str:
    """
    Build an Interest & Cash Transactions section from CashTransaction Flex data.

    Shows interest paid/received, dividends, and withholding tax.
    If the Flex Query has no CashTransaction section enabled, this returns "".
    """
    if ct_df is None or ct_df.empty:
        return ""

    INTEREST_TYPES = {
        "Broker Interest Paid", "Broker Interest Received",
        "Bond Interest Paid",   "Bond Interest Received",
    }
    DIVIDEND_TYPES = {"Dividends", "Payment In Lieu Of Dividends", "Withholding Tax"}

    interest  = ct_df[ct_df["type"].isin(INTEREST_TYPES)].copy()
    dividends = ct_df[ct_df["type"].isin(DIVIDEND_TYPES)].copy()

    sections = []

    # ── Interest summary ───────────────────────────────────────
    if not interest.empty:
        total_paid     = interest[interest["amount"] < 0]["amount"].sum()
        total_received = interest[interest["amount"] > 0]["amount"].sum()
        net            = interest["amount"].sum()

        net_cls = "pos" if net > 0 else "neg"
        paid_cls = "neg"
        recv_cls = "pos"

        summary_cards = f"""
<div class="summary-grid">
  <div class="card"><div class="label">Interest Paid (margin)</div>
    <div class="value"><span class="{paid_cls}">{total_paid:,.2f}</span></div></div>
  <div class="card"><div class="label">Interest Received (cash)</div>
    <div class="value"><span class="{recv_cls}">{total_received:+,.2f}</span></div></div>
  <div class="card"><div class="label">Net Interest</div>
    <div class="value"><span class="{net_cls}">{net:+,.2f}</span></div></div>
</div>"""

        rows = "".join(
            f'<tr><td>{r["date"]}</td><td>{r["type"]}</td>'
            f'<td>{r["description"]}</td><td>{r["currency"]}</td>'
            f'<td class="{"neg" if r["amount"] < 0 else "pos"}">'
            f'{r["amount"]:+,.4f}</td></tr>'
            for _, r in interest.sort_values("date").iterrows()
        )
        tbl = (f"<table data-sortable><thead><tr><th>Date</th><th>Type</th><th>Description</th>"
               f"<th>Currency</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table>")
        sections.append(f"<h2>Interest Charges &amp; Income</h2>{summary_cards}{tbl}")

    # ── Dividends ───────────────────────────────────────────────
    if not dividends.empty:
        total_div = dividends[dividends["type"] == "Dividends"]["amount"].sum()
        total_tax = dividends[dividends["type"] == "Withholding Tax"]["amount"].sum()

        rows = "".join(
            f'<tr><td>{r["date"]}</td><td>{r["type"]}</td>'
            f'<td>{r["description"]}</td><td>{r["currency"]}</td>'
            f'<td class="{"neg" if r["amount"] < 0 else "pos"}">'
            f'{r["amount"]:+,.4f}</td></tr>'
            for _, r in dividends.sort_values("date").iterrows()
        )
        tbl = (f"<table data-sortable><thead><tr><th>Date</th><th>Type</th><th>Description</th>"
               f"<th>Currency</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table>")
        div_html = (f'<p class="note">Total dividends received: '
                    f'<span class="pos">{total_div:+,.2f}</span> &nbsp;|&nbsp; '
                    f'Withholding tax: <span class="neg">{total_tax:,.2f}</span></p>')
        sections.append(f"<h2>Dividends &amp; Withholding Tax</h2>{div_html}{tbl}")

    return "\n".join(sections) if sections else ""


def build_open_positions_table(
    open_lots: list[dict],
    live_portfolio: dict,
    statement_positions: pd.DataFrame,
    prior_positions: dict = None,
) -> tuple[str, str]:
    """
    Build the Open Positions HTML table.

    Position source priority (mirrors moomoo's fetch_positions() approach):
      1. live_portfolio  — ALL positions currently held in IB Gateway/TWS (live)
      2. statement_positions — positions from the activity statement (end-of-day)
      3. FIFO open_lots — computed from trade history only (no price data)

    Each live/statement position is enriched with FIFO buy-date / avg-cost data
    where available.  Returns (html_table, price_source_note).
    """
    # ── Index FIFO lots by code for buy-date / avg-cost enrichment ────────────
    lots_by_code: dict[str, list] = defaultdict(list)
    for lot in open_lots:
        lots_by_code[lot["code"]].append(lot)

    # ── Choose position source ─────────────────────────────────────────────────
    if live_portfolio:
        price_source    = "live (IB Gateway)"
        position_codes  = sorted(live_portfolio)

        def _pos(code):
            lp = live_portfolio[code]
            return (lp["currency"],
                    float(lp["position"]),
                    float(lp["market_price"]),
                    float(lp.get("unrealized_pnl", 0)),
                    float(lp.get("avg_cost", 0)))

    elif statement_positions is not None and not statement_positions.empty:
        price_source   = "end-of-day from statement (not live)"
        stmt_by_code   = {row["code"]: row for _, row in statement_positions.iterrows()}
        position_codes = sorted(stmt_by_code)

        def _pos(code):
            lp = stmt_by_code[code]
            return (str(lp.get("currency", "USD")),
                    float(lp.get("qty", 0)),
                    float(lp.get("last_price", 0) or 0),
                    float(lp.get("unrealized_pnl", 0) or 0),
                    float(lp.get("avg_cost", 0) or 0))

    else:
        # Fall back to FIFO open lots only
        if not open_lots:
            return "<p><em>No open positions found.</em></p>", ""
        price_source = "FIFO avg cost (no live or statement prices)"
        agg: dict[str, dict] = {}
        for lot in open_lots:
            c = lot["code"]
            if c not in agg:
                agg[c] = {"currency": lot["currency"], "lots": []}
            agg[c]["lots"].append(lot)
        position_codes = sorted(agg)

        def _pos(code):
            lots   = agg[code]["lots"]
            cur    = agg[code]["currency"]
            qty    = sum(l["qty"] for l in lots)
            avg    = sum(l["buy_price"] * l["qty"] for l in lots) / (qty or 1)
            return cur, qty, avg, 0.0, avg

    price_source_note = f"Prices: {price_source}."

    # ── Build rows ─────────────────────────────────────────────────────────────
    rows_out = []
    for code in position_codes:
        currency, total_qty, last_price, api_upl, pos_avg_cost = _pos(code)
        lots = lots_by_code.get(code, [])

        if lots:
            earliest_buy = min(l["buy_date"] for l in lots)
            max_hold     = max(l["hold_days"] for l in lots)
            term         = "Long" if max_hold >= 365 else "Short"
            lot_qty      = sum(l["qty"] for l in lots)
            avg_cost     = sum(l["buy_price"] * l["qty"] for l in lots) / (lot_qty or 1)

            if last_price > 0:
                st_upl = sum((last_price - l["buy_price"]) * l["qty"]
                             for l in lots if l["hold_days"] < 365)
                lt_upl = sum((last_price - l["buy_price"]) * l["qty"]
                             for l in lots if l["hold_days"] >= 365)
            elif api_upl != 0:
                st_qty  = sum(l["qty"] for l in lots if l["hold_days"] < 365)
                lt_qty  = sum(l["qty"] for l in lots if l["hold_days"] >= 365)
                total_q = (st_qty + lt_qty) or 1
                st_upl  = api_upl * (st_qty / total_q)
                lt_upl  = api_upl * (lt_qty / total_q)
            else:
                last_price = avg_cost   # display avg cost when no price data
                st_upl     = 0.0
                lt_upl     = 0.0
        else:
            # Position exists in live/statement data but not in FIFO open lots
            # (could be opened before fetch_start or triggered by corporate action)
            # Try to enrich from PriorPeriodPosition data when available.
            pp = (prior_positions or {}).get(code, {})
            if pp.get("open_date"):
                hold_days_pp = (date.today() - pp["open_date"]).days
                earliest_buy = str(pp["open_date"])
                max_hold     = hold_days_pp
                term         = "Long" if hold_days_pp >= 365 else "Short"
                lt_upl       = api_upl if hold_days_pp >= 365 else 0.0
                st_upl       = api_upl if hold_days_pp < 365 else 0.0
            else:
                earliest_buy = "—"
                max_hold     = "—"
                term         = "—"
                st_upl       = api_upl
                lt_upl       = 0.0
            avg_cost = pp.get("avg_cost") or pos_avg_cost

        rows_out.append({
            "code":             code,
            "qty":              total_qty,
            "avg_cost":         round(float(avg_cost), 4),
            "last_price":       round(float(last_price), 4),
            "currency":         currency,
            "earliest_buy":     str(earliest_buy),
            "hold_days":        max_hold,
            "term":             term,
            "ST Unrealized":    round(st_upl, 2),
            "LT Unrealized":    round(lt_upl, 2),
            "Total Unrealized": round(st_upl + lt_upl, 2),
        })

    if not rows_out:
        return "<p><em>No open positions found.</em></p>", ""

    df_out = pd.DataFrame(rows_out)
    html = df_to_html(
        df_out,
        numeric_cols=["ST Unrealized", "LT Unrealized", "Total Unrealized"],
    )
    return html, price_source_note


# ─── FX Rate + new CGT panels ──────────────────────────────────────────────────

def _fetch_fx_rate(from_ccy: str, to_ccy: str) -> float | None:
    """Fetch live FX rate (from_ccy → to_ccy) via yfinance. Returns None on failure."""
    try:
        import yfinance as yf  # optional dep; not imported at module level in ibkr
        ticker = f"{from_ccy}{to_ccy}=X"
        info = yf.Ticker(ticker).fast_info
        rate = getattr(info, "last_price", None)
        if rate and float(rate) > 0:
            return float(rate)
    except Exception:
        pass
    return None


def build_aud_equivalent_section(fy_sum_df: pd.DataFrame) -> str:
    """Shows USD/SGD foreign FY totals converted to AUD at live exchange rates."""
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
        f'<details style="border:1px solid #3d5a2d;border-radius:8px;padding:16px 20px;margin-bottom:28px;background:#12181f" open>'
        f'<summary style="cursor:pointer;color:#adff2f;font-weight:600;font-size:1rem;padding:4px 0">'
        f'&#x1F4B1; AUD-Equivalent Summary (live rate) &#x2014; click to collapse</summary>'
        f'<div style="margin-top:12px">'
        f'<p class="note">Live rates: {rates_str} &middot; Fetched from Yahoo Finance at report generation time.</p>'
        f'{table_html}</div></details>'
    )


def build_current_fy_panel(
    matched: list[dict],
    live_portfolio: dict,
    statement_positions: "pd.DataFrame",
) -> str:
    """Shows current-FY realized (closed trades) + unrealized (open positions) P&L."""
    current_fy = au_fy(date.today())
    realized_rows = [m for m in matched if m.get("fy") == current_fy]
    realized_by_ccy: dict[str, float] = {}
    for m in realized_rows:
        ccy = m.get("currency", "???")
        realized_by_ccy[ccy] = realized_by_ccy.get(ccy, 0.0) + float(m.get("taxable_gain", 0))
    unrealized_by_ccy: dict[str, float] = {}
    if live_portfolio:
        for code, pos in live_portfolio.items():
            ccy = pos.get("currency", "USD")
            upl = float(pos.get("unrealized_pnl", 0) or 0)
            unrealized_by_ccy[ccy] = unrealized_by_ccy.get(ccy, 0.0) + upl
    elif statement_positions is not None and not statement_positions.empty:
        for _, pos in statement_positions.iterrows():
            ccy = str(pos.get("currency", "USD"))
            upl = float(pos.get("unrealized_pnl", 0) or 0)
            unrealized_by_ccy[ccy] = unrealized_by_ccy.get(ccy, 0.0) + upl
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
    n_open = len(live_portfolio) if live_portfolio else (
        len(statement_positions) if statement_positions is not None and not statement_positions.empty else 0
    )
    return (
        f'<details style="border:1px solid #5a3d2d;border-radius:8px;padding:16px 20px;margin-bottom:28px;background:#12181f" open>'
        f'<summary style="cursor:pointer;color:#ffd700;font-weight:600;font-size:1rem;padding:4px 0">'
        f'&#x1F4C5; {current_fy} P&amp;L Snapshot ({n_realized} realized trades + {n_open} open) &#x2014; click to collapse</summary>'
        f'<div style="margin-top:12px">'
        f'<p class="note">Realized = taxable gain on closed trades in {current_fy}. '
        f'Unrealized = current mark-to-market P&amp;L on open positions (not a CGT event until sold).</p>'
        f'{table_html}</div></details>'
    )


def generate_html(
    fy_sum_df: pd.DataFrame,
    matched: list[dict],
    open_lots: list[dict],
    live_portfolio: dict,
    statement_positions: pd.DataFrame,
    warnings: list[str],
    fetch_start: str,
    statement_path: str,
    account_summary: dict = None,
    cash_transactions: "pd.DataFrame" = None,
    prior_positions: dict = None,
) -> str:
    warnings_html = ""
    if warnings:
        items = "\n".join(f'<div class="warn">⚠ {w}</div>' for w in warnings)
        warnings_html = f"<h2>Warnings</h2>{items}"

    fy_summary_html = df_to_html(
        fy_sum_df,
        numeric_cols=["ST Gains", "ST Losses", "LT Gross Gains", "LT Losses", "Net Taxable"],
        discount_cols=["CGT Discount"],
    ) if not fy_sum_df.empty else "<p><em>No closed trades found.</em></p>"

    open_pos_html, price_note = build_open_positions_table(
        open_lots, live_portfolio, statement_positions, prior_positions
    )

    acct_html = build_account_summary_html(account_summary or {})
    cash_html = build_cash_transactions_html(cash_transactions)

    html = HTML_TEMPLATE.format(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        statement_path=os.path.basename(statement_path) if os.sep in statement_path or "/" in statement_path else statement_path,
        fetch_start=fetch_start,
        warnings_html=warnings_html,
        kpi_cards=_build_kpi_cards(matched),
        account_summary_html=acct_html,
        fy_summary_table=fy_summary_html,
        fy_sections=build_fy_sections(matched),
        open_positions_table=open_pos_html,
        price_source_note=price_note,
        cash_transactions_html=cash_html,
        current_fy_panel=build_current_fy_panel(matched, live_portfolio, statement_positions),
        aud_equivalent_section=build_aud_equivalent_section(fy_sum_df),
    )
    from html_utils import JS_TABLE_SORT, JS_CSV_EXPORT  # C-6, C-11
    csv_btn = '<div data-no-print style="text-align:right;margin:12px 0"><button onclick="exportTableCSV(\'cgt-table\',\'ibkr_cgt.csv\')" style="padding:6px 14px;cursor:pointer">Export CSV</button></div>'
    return html.replace("</main>", f"{csv_btn}<script>{JS_CSV_EXPORT}\n{JS_TABLE_SORT}</script>\n</main>")


# ─── Public data-extraction API (used by combined_cgt_report.py) ────────────

def get_data(
    statement: str = None,
    flex_token: str = FLEX_TOKEN,
    flex_query_id: str = FLEX_QUERY_ID,
    start: str = FETCH_START,
) -> dict:
    """
    Run the full IBKR CGT analysis and return results as a dict.
    Called by combined_cgt_report.py — does not write any files.
    """
    # Callers (e.g. combined_cgt_report.py) may pass None to mean "use built-in defaults".
    if flex_token is None:
        flex_token = FLEX_TOKEN
    if flex_query_id is None:
        flex_query_id = FLEX_QUERY_ID

    _empty = {
        "matched": [], "open_lots": [], "fy_sum_df": pd.DataFrame(),
        "statement_positions": pd.DataFrame(), "live_portfolio": {},
        "warnings": [], "trades_df": pd.DataFrame(),
        "cash_transactions": pd.DataFrame(),
        "fetch_start": start, "statement_label": "",
    }
    if flex_token and flex_query_id:
        trades_df, statement_positions, cash_transactions, prior_positions = fetch_flex_statement(
            flex_token, flex_query_id, start
        )
        statement_label = f"Flex Query {flex_query_id}"
    elif statement:
        trades_df, statement_positions, cash_transactions = parse_activity_statement(
            statement, start
        )
        prior_positions = {}
        statement_label = statement
    else:
        return _empty

    if trades_df.empty and statement_positions.empty:
        return _empty

    ledger = FifoLedger()
    # Seed ledger with prior-period lots (positions opened before the Flex Query
    # date range).  These synthetic BUY entries let sells in the current period
    # match correctly instead of generating "no BUY lots found" warnings.
    _CUR_MAP = {"US": "USD", "AU": "AUD", "HK": "HKD"}
    for code, pp in prior_positions.items():
        if pp.get("qty", 0) > 0 and pp.get("avg_cost", 0) > 0:
            lot_date = pp.get("open_date") or date(2000, 1, 1)
            currency = _CUR_MAP.get(code[:2], "USD")
            ledger.buy(code, lot_date, pp["avg_cost"], pp["qty"], currency)
    for _, row in trades_df.iterrows():
        if row["side"] == "BUY":
            ledger.buy(row["code"], row["dt"], row["price"], row["qty"], row["currency"])
        else:
            ledger.sell(row["code"], row["dt"], row["price"], row["qty"], row["currency"])

    return {
        "matched":             ledger.matched,
        "open_lots":           ledger.open_lots(),
        "fy_sum_df":           fy_summary(ledger.matched),
        "statement_positions": statement_positions,
        "live_portfolio":      {},
        "warnings":            ledger.warnings,
        "trades_df":           trades_df,
        "cash_transactions":   cash_transactions,
        "prior_positions":     prior_positions,
        "fetch_start":         start,
        "statement_label":     statement_label,
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # cp1252 fix
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        description="Australian CGT analysis from IBKR trade history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples (Flex Query is the default — credentials are built in):
  python ibkr_cgt_analysis.py
  python ibkr_cgt_analysis.py --start 2020-01-01
  python ibkr_cgt_analysis.py --flex-token TOKEN --flex-query-id 123456

Examples (manual CSV):
  python ibkr_cgt_analysis.py --statement activity.csv
  python ibkr_cgt_analysis.py --statement activity.csv --out my_ibkr_cgt.html

Default Flex Query: token={FLEX_TOKEN[:8]}... id={FLEX_QUERY_ID}
        """,
    )
    parser.add_argument(
        "--flex-token", default=FLEX_TOKEN, dest="flex_token",
        help=f"IBKR Flex Web Service token (default: built-in)",
    )
    parser.add_argument(
        "--flex-query-id", default=FLEX_QUERY_ID, dest="flex_query_id",
        help=f"Flex Query ID (default: {FLEX_QUERY_ID})",
    )
    parser.add_argument(
        "--statement", default=None,
        help="Path to IBKR Activity Statement CSV (overrides Flex Query)",
    )
    parser.add_argument(
        "--start", default=FETCH_START,
        help=f"Earliest trade date to include (default: {FETCH_START})",
    )
    parser.add_argument(
        "--out", default="ibkr_cgt_report.html",
        help="Output HTML file (default: ibkr_cgt_report.html)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Australian CGT Analysis — IBKR")
    print("=" * 60)

    # ── Fetch / parse trade data ─────────────────────────────────
    cash_transactions = pd.DataFrame()
    if args.statement:
        if not os.path.isfile(args.statement):
            print(f"ERROR: Statement file not found: {args.statement}")
            sys.exit(1)
        print(f"\n[1/3] Parsing statement: {args.statement}")
        trades_df, statement_positions, cash_transactions = parse_activity_statement(
            args.statement, args.start
        )
        prior_positions = {}
        statement_label = args.statement
    else:
        print(f"\n[1/3] Fetching statement via IBKR Flex Query (query id={args.flex_query_id}) ...")
        trades_df, statement_positions, cash_transactions, prior_positions = fetch_flex_statement(
            args.flex_token, args.flex_query_id, args.start
        )
        statement_label = f"Flex Query {args.flex_query_id}"

    if trades_df.empty:
        if statement_positions.empty:
            print("No stock trades or open positions found.  Exiting.")
            sys.exit(0)
        print("\n  NOTE: No trade history found in this Flex Query period.")
        print("  Only open positions will be shown — buy dates / FIFO data unavailable.")
        print("  To fix: edit MyFlexQuery in IBKR Portal → change date range from 'Today' to")
        print(f"  'Custom Date Range' with start={args.start} and end=Yesterday.")
    else:
        print(f"  Found {len(trades_df)} trade rows.")
        print(f"  Date range: {trades_df['dt'].min()} → {trades_df['dt'].max()}")
        print(f"  Tickers: {sorted(trades_df['code'].unique())}")

    # ── FIFO matching ────────────────────────────────────────────
    print("\n[2/3] Running FIFO matching ...")
    ledger = FifoLedger()
    # Seed with prior-period positions so sells can match against them
    _CUR_MAP = {"US": "USD", "AU": "AUD", "HK": "HKD"}
    for code, pp in prior_positions.items():
        if pp.get("qty", 0) > 0 and pp.get("avg_cost", 0) > 0:
            lot_date = pp.get("open_date") or date(2000, 1, 1)
            currency = _CUR_MAP.get(code[:2], "USD")
            ledger.buy(code, lot_date, pp["avg_cost"], pp["qty"], currency)
    for _, row in trades_df.iterrows():
        if row["side"] == "BUY":
            ledger.buy(row["code"], row["dt"], row["price"], row["qty"], row["currency"])
        else:
            ledger.sell(row["code"], row["dt"], row["price"], row["qty"], row["currency"])

    matched   = ledger.matched
    warnings  = ledger.warnings
    open_lots = ledger.open_lots()

    print(f"  Matched {len(matched)} sell events as CGT events.")
    if open_lots:
        print(f"  {len(open_lots)} open FIFO lots remaining (unrealised CGT).")
    if warnings:
        print(f"  {len(warnings)} warning(s) — see report.")

    # ── FY Summary ───────────────────────────────────────────────
    print("\n[3/3] Building CGT summary ...")
    fy_sum_df = fy_summary(matched)

    if not fy_sum_df.empty:
        print("\n  FY Summary:  (ST = Short Term < 12 months | LT = Long Term \u2265 12 months, 50% CGT discount eligible)")
        print(fy_sum_df.to_string(index=False))
    else:
        print("  No closed sell trades found — CGT summary empty.")

    # ── Generate HTML ────────────────────────────────────────────
    html = generate_html(
        fy_sum_df, matched, open_lots,
        {}, statement_positions,
        warnings, args.start, statement_label,
        cash_transactions=cash_transactions,
        prior_positions=prior_positions,
    )
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"\n  Report written: {out_path}")

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent.parent.parent / "reports"))
        from report_db import save_cgt_snapshot, archive_report as _arc
        save_cgt_snapshot(fy_sum_df, "IBKR")
        _arc(_Path(out_path))
    except Exception as _e:
        print(f"  [report_db] ibkr_cgt snapshot skipped: {_e}")

    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as _e:
        print(f"\n[ERROR] {_e}")
        sys.exit(1)
