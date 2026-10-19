"""
F&O / Forex Arbitrage Engine
==============================
Detects basis spread opportunities across two dimensions:

  1. NSE Index Arbitrage  (Nifty / BankNifty / FinNifty)
     ─────────────────────────────────────────────────────
     Compares the NSE index spot price (yfinance: ^NSEI, ^NSEBANK, ^CNXFIN)
     with the *near-month* futures price (TV Pro: NSE:NIFTY1!, NSE:BANKNIFTY1!, NSE:FINNIFTY1!).
     Shows:
       • Basis = (Futures − Spot) / Spot × 100   [%]
       • Fair-value basis = risk-free rate × DTE/365
       • Mispricing = Actual Basis − Fair-Value Basis   [bps]
       • Signal: Rich (sell futures, buy spot) | Cheap (buy futures, sell spot) | Fair

  2. G10 Forex Basis  (spot vs CME futures)
     ─────────────────────────────────────
     Compares TV Pro canonical FX spot prices with CME FX continuous futures
     (yfinance: 6E=F EURUSD, 6J=F USDJPY (inverted), 6B=F GBPUSD, 6A=F AUDUSD,
      6C=F USDCAD (inverted), 6N=F NZDUSD)
     Shows:
       • Forward basis = (Futures − Spot) / Spot × 10000  [pips]
       • Signal: Contango (futures > spot) | Backwardation | Fair

  3. NSE F&O Stock Basis  (Angel One OI Buildup crosscheck)
     ─────────────────────────────────────────────────────
     Uses AngelOne OI Buildup API to find stocks where:
       • Price rising + OI rising  → Long Buildup  (trend continuation)
       • Price rising + OI falling → Short Covering (explosive reversal)
     Filters for highest absolute OI change as arbitrage signal quality.

Outputs HTML report to reports/fo_arbitrage_report.html
Also prints a concise summary to stdout.

Usage:
  python tradingview/screeners/fo_arbitrage.py
  python tradingview/screeners/fo_arbitrage.py --rfi 6.5  # India risk-free rate %
  python tradingview/screeners/fo_arbitrage.py --no-india  # skip AngelOne calls
  python tradingview/screeners/fo_arbitrage.py --no-forex  # skip forex basis
  python tradingview/screeners/fo_arbitrage.py --out path/to/report.html
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Windows: force UTF-8 console so emoji/arrows render correctly ──────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Output path ────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent.parent
OUTPUT_HTML = ROOT.parent / "reports" / "fo_arbitrage_report.html"

# ── India risk-free rate (RBI repo rate, ~6.5% as of Apr 2026) ────────────────
DEFAULT_RFI = 6.5   # percent per annum

# ── NSE index contracts ────────────────────────────────────────────────────────
NSE_INDEX_CONTRACTS = [
    {
        "name":      "NIFTY 50",
        "spot_yf":   "^NSEI",
        "tv_fut":    "NSE:NIFTY1!",
        "lot_size":  25,
        "spot_etf":  "NIFTYBEES",       # NSE ETF that tracks Nifty 50 (used as the "spot" leg)
        "etf_yf":    "NIFTYBEES.NS",    # yfinance ticker for ETF price
        "comment":   "Nifty 50 index — most liquid NSE futures",
    },
    {
        "name":      "BANKNIFTY",
        "spot_yf":   "^NSEBANK",
        "tv_fut":    "NSE:BANKNIFTY1!",
        "lot_size":  15,
        "spot_etf":  "BANKBEES",        # NSE Bank ETF
        "etf_yf":    "BANKBEES.NS",     # yfinance ticker
        "comment":   "Bank Nifty — high-beta banking index",
    },
    {
        "name":      "FINNIFTY",
        "spot_yf":   "^CNXFIN",
        "tv_fut":    "NSE:FINNIFTY1!",
        "lot_size":  40,
        "spot_etf":  "JUNIORBEES",      # Closest liquid ETF (no dedicated FinNifty ETF)
        "etf_yf":    "JUNIORBEES.NS",   # yfinance ticker
        "comment":   "Nifty Financial Services — smaller, faster mover",
        "spot_yf_note": "⚠ ^CNXFIN (yfinance) may not match FINNIFTY1! underlying exactly — basis unreliable if >2%",
        "basis_unreliable_above": 2.0,   # abs basis % above which to flag as unreliable
    },
]

# ── Global / US + ASX index contracts ─────────────────────────────────────────
# E-mini futures; spot from yfinance; futures from TV Pro
GLOBAL_INDEX_CONTRACTS = [
    {
        "name":       "S&P 500",
        "spot_yf":    "^GSPC",
        "tv_fut":     "CME:ES1!",
        "multiplier": 50,           # 1 E-mini ES contract = $50 × index price (~$122k)
        "spot_etf":   "SPY",
        "etf_yf":     "SPY",           # yfinance ticker
        "rfi_pct":    4.5,          # US 3-month T-bill rate (approx Apr 2026)
        "currency":   "USD",
        "comment":    "E-mini S&P 500 — sell SPY ETF as spot hedge",
    },
    {
        "name":       "NASDAQ 100",
        "spot_yf":    "^NDX",
        "tv_fut":     "CME:NQ1!",
        "multiplier": 20,           # 1 E-mini NQ contract = $20 × index price (~$375k)
        "spot_etf":   "QQQ",
        "etf_yf":     "QQQ",           # yfinance ticker
        "rfi_pct":    4.5,
        "currency":   "USD",
        "comment":    "E-mini NASDAQ 100 — sell QQQ ETF as spot hedge",
    },
    {
        "name":       "ASX 200",
        "spot_yf":    "^AXJO",
        "tv_fut":     "ASX:SPI200!",
        "multiplier": 25,           # 1 SPI200 contract = A$25 × index price (~A$190k)
        "spot_etf":   "IOZ",
        "etf_yf":     "IOZ.AX",        # yfinance ticker (ASX-listed)
        "rfi_pct":    4.1,          # RBA cash rate (approx Apr 2026)
        "currency":   "AUD",
        "comment":    "SPI 200 futures — sell IOZ ETF as spot hedge",
    },
    {
        "name":       "Hang Seng (HK)",
        "spot_yf":    "^HSI",
        "tv_fut":     "HKEX:HSI1!",
        "multiplier": 50,           # 1 Hang Seng contract = HK$50 × index (~HK$1.0M)
        "spot_etf":   "2800.HK",    # Tracker Fund of Hong Kong (HKEX:2800) — tracks HSI
        "etf_yf":     "2800.HK",       # yfinance ticker
        "rfi_pct":    5.25,         # HKMA base rate (linked to US Fed)
        "currency":   "HKD",
        "comment":    "Hang Seng Index futures — sell 2800.HK ETF as spot hedge",
    },
    {
        "name":       "Nikkei 225 (JP)",
        "spot_yf":    "^N225",
        "tv_fut":     "CME:NKD1!",  # CME dollar-denominated Nikkei futures (most liquid outside Japan)
        "multiplier": 5,            # NKD = $5 × Nikkei index (~$190k per contract)
        "spot_etf":   "EWJ",        # iShares MSCI Japan ETF (USD-traded, best proxy for non-JPY accounts)
        "etf_yf":     "EWJ",           # yfinance ticker
        "rfi_pct":    0.5,          # Bank of Japan rate (near zero; using 0.5% for 2026)
        "currency":   "USD",        # NKD futures are USD-denominated
        "comment":    "Nikkei 225 — CME dollar futures; sell EWJ ETF as spot hedge",
        "spot_yf_note": "ℹ ^N225 is JPY-quoted; NKD1! is USD-based — FX-adjusted comparison",
    },
    {
        "name":       "STI (Singapore)",
        "spot_yf":    "^STI",
        "tv_fut":     "SGX:STF1!",   # SGX Straits Times Index futures (STF contract)
        "multiplier": 10,            # 1 STI futures = S$10 × index (~S$36k at 3,600)
        "spot_etf":   "ES3.SI",      # SPDR STI ETF traded on SGX
        "etf_yf":     "ES3.SI",        # yfinance ticker (SGX-listed, .SI suffix)
        "rfi_pct":    3.7,           # MAS (Singapore) overnight rate (approx Apr 2026)
        "currency":   "SGD",
        "comment":    "Singapore STI — SGX STF futures; sell ES3 STI ETF as spot hedge",
        "spot_yf_note": "ℹ SGX STI futures volume is low vs Nifty/SNP — arb trades may face slippage",
        "basis_unreliable_above": 5.0,
    },
]

# ── G10 Forex pairs ────────────────────────────────────────────────────────────
# tv_symbol: TV Pro canonical spot  |  yf_fut: yfinance CME FX futures
# invert_fut: when CME quotes currency per USD (JPY, CAD) vs USD per currency
FOREX_PAIRS = [
    {"pair": "EUR/USD", "tv_symbol": "FX:EURUSD",  "yf_fut": "6E=F",  "invert_fut": False},
    {"pair": "GBP/USD", "tv_symbol": "FX:GBPUSD",  "yf_fut": "6B=F",  "invert_fut": False},
    {"pair": "AUD/USD", "tv_symbol": "FX:AUDUSD",  "yf_fut": "6A=F",  "invert_fut": False},
    {"pair": "NZD/USD", "tv_symbol": "FX:NZDUSD",  "yf_fut": "6N=F",  "invert_fut": False},
    {"pair": "USD/JPY", "tv_symbol": "FX:USDJPY",  "yf_fut": "6J=F",  "invert_fut": True },
    {"pair": "USD/CAD", "tv_symbol": "FX:USDCAD",  "yf_fut": "6C=F",  "invert_fut": True },
    {"pair": "USD/CHF", "tv_symbol": "FX:USDCHF",  "yf_fut": "6S=F",  "invert_fut": True },
]

# ── Global market momentum watchlist ──────────────────────────────────────────
# Price change + RSI snapshot across major indices and sector benchmarks.
# Source: yfinance (free, no subscription needed).
# These signals parallel the NSE OI buildup section but for global markets.
GLOBAL_MOMENTUM_TICKERS = [
    # US broad market
    {"label": "S&P 500",          "ticker": "^GSPC",  "region": "US",        "type": "Index"},
    {"label": "NASDAQ 100",       "ticker": "^NDX",   "region": "US",        "type": "Index"},
    {"label": "Dow Jones",        "ticker": "^DJI",   "region": "US",        "type": "Index"},
    {"label": "Russell 2000",     "ticker": "^RUT",   "region": "US",        "type": "Index"},
    # US Fear gauges
    {"label": "VIX (US Fear)",    "ticker": "^VIX",   "region": "US",        "type": "Volatility"},
    # Asia-Pacific
    {"label": "Nikkei 225",       "ticker": "^N225",  "region": "Japan",     "type": "Index"},
    {"label": "Hang Seng",        "ticker": "^HSI",   "region": "Hong Kong", "type": "Index"},
    {"label": "ASX 200",          "ticker": "^AXJO",  "region": "Australia", "type": "Index"},
    {"label": "STI (Singapore)",  "ticker": "^STI",   "region": "Singapore", "type": "Index"},
    # Europe
    {"label": "DAX (Germany)",    "ticker": "^GDAXI", "region": "Europe",    "type": "Index"},
    {"label": "FTSE 100 (UK)",    "ticker": "^FTSE",  "region": "Europe",    "type": "Index"},
    {"label": "CAC 40 (France)",  "ticker": "^FCHI",  "region": "Europe",    "type": "Index"},
    # Commodities / FX
    {"label": "Gold",             "ticker": "GC=F",   "region": "Global",    "type": "Commodity"},
    {"label": "Crude Oil (WTI)",  "ticker": "CL=F",   "region": "Global",    "type": "Commodity"},
    {"label": "DXY (USD Index)",  "ticker": "DX-Y.NYB","region": "FX",       "type": "FX"},
]

_TV_BASE = "https://scanner.tradingview.com"
_TV_COOKIE_PATH = ROOT / "cookie"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tv_cookie() -> str:
    """Load saved TradingView session cookie, or return empty string."""
    try:
        return _TV_COOKIE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _tv_fetch_price(tv_symbol: str) -> Optional[float]:
    """
    Fetch the latest close price for a TV symbol via the screener API.
    Works for futures (NSE:NIFTY1!) and forex (FX:EURUSD).
    """
    exchange, sym = (tv_symbol.split(":", 1) + [""])[:2]
    if not sym:
        sym, exchange = exchange, ""

    # Determine the right scan endpoint based on prefix
    # Continuous futures (NIFTY1!, BANKNIFTY1!, FINNIFTY1!) live on futures/scan
    if sym.endswith("1!") or sym.endswith("2!"):
        url = f"{_TV_BASE}/futures/scan"
    elif exchange in ("NSE", "BSE", "NFO"):
        url = f"{_TV_BASE}/india/scan"
    elif exchange == "FX":
        url = f"{_TV_BASE}/forex/scan"
    elif exchange in ("CME", "COMEX", "NYMEX", "CBOT", "ICEEUR"):
        url = f"{_TV_BASE}/futures/scan"
    else:
        url = f"{_TV_BASE}/global/scan"

    payload = {
        "columns": ["close", "change"],
        "filter": [{"left": "name", "operation": "equal", "right": sym}],
        "sort":   {"sortBy": "close", "sortOrder": "desc"},
        "range":  [0, 1],
    }
    cookie = _tv_cookie()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            **({"Cookie": cookie} if cookie else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        rows = data.get("data", [])
        if rows:
            d = rows[0].get("d", [])
            return float(d[0]) if d and d[0] is not None else None
    except Exception:
        pass
    return None


def _yf_latest_close(ticker: str) -> Optional[float]:
    """Fetch the most recent close from yfinance. Returns None on error."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", interval="1d", auto_adjust=True)
        if hist.empty:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception:
        return None


def _nearest_expiry_dte() -> tuple[int, str]:
    """
    Days-to-expiry and label for the near-month NSE futures.
    NSE expiry is the last Thursday of each month.
    Returns (dte_days, label) e.g. (16, "24 Apr 2026 (NSE near-month)").
    """
    from datetime import date, timedelta
    today = date.today()
    # Find last Thursday of current month
    import calendar
    year, month = today.year, today.month
    _, last_day = calendar.monthrange(year, month)
    # Walk backward from last_day to find Thursday (weekday=3)
    last_thu = date(year, month, last_day)
    while last_thu.weekday() != 3:
        last_thu -= timedelta(days=1)
    # If today is past this month's last Thursday, use next month's
    if today >= last_thu:
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        _, last_day = calendar.monthrange(year, month)
        last_thu = date(year, month, last_day)
        while last_thu.weekday() != 3:
            last_thu -= timedelta(days=1)
    dte = (last_thu - today).days
    label = last_thu.strftime("%d %b %Y") + " (NSE near-month)"
    return dte, label


# ─────────────────────────────────────────────────────────────────────────────
# Section 1: NSE Index Arbitrage
# ─────────────────────────────────────────────────────────────────────────────

def fetch_nse_index_arb(rfi_pct: float = DEFAULT_RFI) -> list[dict]:
    """
    For each NSE index contract, fetch spot (yfinance) + futures (TV Pro),
    compute basis, fair value, and mispricing.

    Returns list of dicts:
      {name, spot, futures, basis_pct, fair_basis_pct, mispricing_bps,
       dte, expiry_label, signal, lot_size, etf_price, etf_units, comment, error}
    """
    dte, expiry_label = _nearest_expiry_dte()
    rows = []
    for c in NSE_INDEX_CONTRACTS:
        row: dict = {
            "name":     c["name"],
            "lot_size": c["lot_size"],
            "spot_etf": c.get("spot_etf", ""),
            "tv_fut":   c["tv_fut"],
            "comment":  c["comment"],
            "dte":      dte,
            "expiry_label": expiry_label,
            "error":    None,
        }
        # Fetch spot from yfinance
        spot = _yf_latest_close(c["spot_yf"])
        if spot is None:
            row["error"] = f"yfinance: no data for {c['spot_yf']}"
            rows.append(row)
            continue
        row["spot"] = round(spot, 2)

        # Fetch futures from TV Pro
        fut = _tv_fetch_price(c["tv_fut"])
        if fut is None:
            row["error"] = f"TV: no data for {c['tv_fut']}"
            rows.append(row)
            continue
        row["futures"] = round(fut, 2)

        # Basis calculations
        basis_pct       = (fut - spot) / spot * 100
        fair_basis_pct  = rfi_pct * (dte / 365.0)      # carry cost = r × DTE/365
        mispricing_bps  = (basis_pct - fair_basis_pct) * 100   # in bps

        row["basis_pct"]      = round(basis_pct, 3)
        row["fair_basis_pct"] = round(fair_basis_pct, 3)
        row["mispricing_bps"] = round(mispricing_bps, 1)

        # Check for unreliable basis (e.g. FINNIFTY ^CNXFIN mismatch)
        unreliable_threshold = c.get("basis_unreliable_above")
        if unreliable_threshold and abs(basis_pct) > unreliable_threshold:
            row["basis_warning"] = c.get("spot_yf_note", f"Basis {basis_pct:+.2f}% may be unreliable")

        # Signal thresholds: ±10 bps considered noise
        if mispricing_bps > 10:
            row["signal"] = "RICH"          # futures overpriced → sell fut / buy spot
            row["signal_detail"] = "Sell futures, buy spot"
        elif mispricing_bps < -10:
            row["signal"] = "CHEAP"         # futures underpriced → buy fut / sell spot
            row["signal_detail"] = "Buy futures, sell spot"
        else:
            row["signal"] = "FAIR"
            row["signal_detail"] = "No clear mispricing"

        # Trade guide data (used for new-user-friendly Notes column)
        lot_value = c["lot_size"] * fut
        row["lot_value"]      = lot_value
        row["profit_per_lot"] = abs(mispricing_bps) / 100 / 100 * lot_value

        # Fetch ETF price for trade quantity display
        etf_yf = c.get("etf_yf", "")
        if etf_yf:
            etf_price = _yf_latest_close(etf_yf)
            if etf_price and etf_price > 0:
                row["etf_price"] = round(etf_price, 2)
                # ETF units needed to replicate 1 futures contract (spot leg)
                row["etf_units"] = int(round((c["lot_size"] * spot) / etf_price))

        rows.append(row)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Section 1b: Global / US + ASX Index Arbitrage
# ─────────────────────────────────────────────────────────────────────────────

def _cme_quarterly_expiry_dte() -> int:
    """
    Days-to-expiry for the next CME quarterly contract.
    CME E-mini (ES, NQ) expire on the 3rd Friday of Mar / Jun / Sep / Dec.
    """
    import calendar
    from datetime import date, timedelta
    today = date.today()
    quarterly = [3, 6, 9, 12]
    # Find the next quarterly month at or after today's month
    expiry_year = today.year
    expiry_month = next((m for m in quarterly if m >= today.month), None)
    if expiry_month is None:
        expiry_month = 3
        expiry_year += 1

    def _third_friday(yr: int, mo: int) -> date:
        count = 0
        for day in range(1, calendar.monthrange(yr, mo)[1] + 1):
            d = date(yr, mo, day)
            if d.weekday() == 4:  # Friday
                count += 1
                if count == 3:
                    return d
        raise ValueError(f"No third Friday found for {yr}-{mo}")

    third_fri = _third_friday(expiry_year, expiry_month)
    dte = (third_fri - today).days
    # If today is on or past the expiry, roll to the next quarterly month
    if dte <= 0:
        idx = quarterly.index(expiry_month)
        next_idx = (idx + 1) % 4
        expiry_month = quarterly[next_idx]
        if next_idx == 0:
            expiry_year += 1
        third_fri = _third_friday(expiry_year, expiry_month)
        dte = (third_fri - today).days
    label = third_fri.strftime("%d %b %Y") + " (CME quarterly)"
    return max(1, dte), label


def fetch_global_index_arb() -> list[dict]:
    """
    For each global index contract (US S&P500, NASDAQ, ASX200), fetch spot (yfinance)
    + futures (TV Pro), compute basis vs carry-cost fair value.
    Returns same structure as fetch_nse_index_arb().
    """
    dte, expiry_label = _cme_quarterly_expiry_dte()
    rows = []
    for c in GLOBAL_INDEX_CONTRACTS:
        row: dict = {
            "name":         c["name"],
            "multiplier":   c["multiplier"],
            "spot_etf":     c.get("spot_etf", ""),
            "tv_fut":       c["tv_fut"],
            "currency":     c.get("currency", "USD"),
            "rfi_pct":      c.get("rfi_pct", 4.5),
            "comment":      c["comment"],
            "dte":          dte,
            "expiry_label": expiry_label,
            "error":        None,
        }
        spot = _yf_latest_close(c["spot_yf"])
        if spot is None:
            row["error"] = f"yfinance: no data for {c['spot_yf']}"
            rows.append(row)
            continue
        row["spot"] = round(spot, 2)

        fut = _tv_fetch_price(c["tv_fut"])
        if fut is None:
            row["error"] = f"TV: no data for {c['tv_fut']}"
            rows.append(row)
            continue
        row["futures"] = round(fut, 2)

        rfi_pct        = c.get("rfi_pct", 4.5)
        basis_pct      = (fut - spot) / spot * 100
        fair_basis_pct = rfi_pct * (dte / 365.0)
        mispricing_bps = (basis_pct - fair_basis_pct) * 100

        row["basis_pct"]      = round(basis_pct, 3)
        row["fair_basis_pct"] = round(fair_basis_pct, 3)
        row["mispricing_bps"] = round(mispricing_bps, 1)

        # Check if basis is suspiciously large (e.g. wrong futures symbol)
        unreliable_threshold = c.get("basis_unreliable_above")
        if unreliable_threshold and abs(basis_pct) > unreliable_threshold:
            row["basis_warning"] = c.get("spot_yf_note", f"Basis {basis_pct:+.2f}% — futures/spot mismatch")

        if mispricing_bps > 10:
            row["signal"] = "RICH"
            row["signal_detail"] = "Sell futures, buy spot"
        elif mispricing_bps < -10:
            row["signal"] = "CHEAP"
            row["signal_detail"] = "Buy futures, sell spot"
        else:
            row["signal"] = "FAIR"
            row["signal_detail"] = "No clear mispricing"

        lot_value            = c["multiplier"] * fut
        row["lot_value"]      = lot_value
        row["profit_per_lot"] = abs(mispricing_bps) / 100 / 100 * lot_value

        # Fetch ETF price for trade quantity display
        etf_yf = c.get("etf_yf", "")
        if etf_yf:
            etf_price = _yf_latest_close(etf_yf)
            if etf_price and etf_price > 0:
                row["etf_price"] = round(etf_price, 2)
                # ETF units needed to replicate 1 futures contract (spot leg)
                row["etf_units"] = int(round((c["multiplier"] * spot) / etf_price))

        rows.append(row)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Section 2: Forex Basis
# ─────────────────────────────────────────────────────────────────────────────

def fetch_forex_basis() -> list[dict]:
    """
    For each G10 forex pair, compare TV Pro spot vs CME futures (yfinance).
    Returns:
      {pair, spot, futures, basis_pips, signal, tv_symbol, yf_fut, error}
    """
    rows = []
    for fp in FOREX_PAIRS:
        row: dict = {
            "pair":      fp["pair"],
            "tv_symbol": fp["tv_symbol"],
            "yf_fut":    fp["yf_fut"],
            "error":     None,
        }
        # TV Pro spot
        spot = _tv_fetch_price(fp["tv_symbol"])
        if spot is None:
            row["error"] = f"TV: no data for {fp['tv_symbol']}"
            rows.append(row)
            continue
        row["spot"] = round(spot, 5)

        # CME futures via yfinance
        fut_raw = _yf_latest_close(fp["yf_fut"])
        if fut_raw is None:
            row["error"] = f"yfinance: no data for {fp['yf_fut']}"
            rows.append(row)
            continue

        # CME 6J/6C/6S quote currency-per-USD (inverse of direct quote)
        # e.g. 6J=F is JPY per USD quoted as USD per JPY → need 1/fut_raw for USDJPY
        if fp["invert_fut"]:
            fut = round(1.0 / fut_raw, 5) if fut_raw != 0 else None
        else:
            fut = round(fut_raw, 5)

        if fut is None:
            row["error"] = "Division by zero in futures price"
            rows.append(row)
            continue
        row["futures"] = fut

        # Basis in pips (1 pip = 0.0001 for most pairs, 0.01 for JPY)
        pip_size = 0.01 if "JPY" in fp["pair"] else 0.0001
        basis_pips = (fut - spot) / pip_size

        row["basis_pips"] = round(basis_pips, 1)

        # Signal
        if basis_pips > 5:
            row["signal"] = "CONTANGO"
            row["signal_detail"] = "Futures at premium to spot"
        elif basis_pips < -5:
            row["signal"] = "BACKWARDATION"
            row["signal_detail"] = "Futures at discount to spot"
        else:
            row["signal"] = "FLAT"
            row["signal_detail"] = "Spot and futures aligned"

        rows.append(row)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Section 3: NSE F&O OI Buildup Signals (via AngelOne)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_oi_signals() -> dict:
    """
    Fetch OI buildup for all four types (Long Built Up / Short Built Up /
    Short Covering / Long Unwinding) via AngelOne, for near-month expiry.

    Returns:
      {
        "long_buildup":   [rows sorted by OI change desc],
        "short_buildup":  [rows sorted by OI change desc],
        "short_covering": [rows sorted by OI change desc],
        "long_unwinding": [rows sorted by OI change desc],
        "error": str | None
      }
    """
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "india-brokers"))
        from india_market_internals import IndiaMarketInternals  # type: ignore
    except ImportError as e:
        return {"error": f"Cannot import IndiaMarketInternals: {e}",
                "long_buildup": [], "short_buildup": [],
                "short_covering": [], "long_unwinding": []}

    mi = IndiaMarketInternals()
    result: dict = {
        "long_buildup":   [],
        "short_buildup":  [],
        "short_covering": [],
        "long_unwinding": [],
        "error":          None,
    }
    type_map = {
        "long_buildup":   "Long Built Up",
        "short_buildup":  "Short Built Up",
        "short_covering": "Short Covering",
        "long_unwinding": "Long Unwinding",
    }
    for key, datatype in type_map.items():
        try:
            rows = mi.oi_buildup(datatype, "NEAR")
            # Sort by abs OI change descending, take top 10
            rows.sort(key=lambda r: abs(r.get("oi_change") or 0), reverse=True)
            result[key] = rows[:10]
        except Exception as ex:
            result["error"] = str(ex)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Section 4: Global Market Momentum (yfinance — no subscription)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_global_momentum() -> list[dict]:
    """
    Fetch price snapshot (close, 1-day change %, RSI-14, 52w high/low) for
    major global indices, commodities, and FX via yfinance.
    Signals are derived purely from price action — no OI data required.

    Returns list of dicts:
      {label, ticker, region, type, close, chg_pct, rsi, from_52w_high_pct,
       signal, signal_detail, error}
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return [{"label": t["label"], "ticker": t["ticker"], "region": t["region"],
                 "type": t["type"], "error": "yfinance not installed"} for t in GLOBAL_MOMENTUM_TICKERS]

    def _rsi(series, period=14) -> Optional[float]:
        """Wilder's RSI."""
        try:
            delta = series.diff()
            gain  = delta.clip(lower=0).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
            loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
            rs    = gain / loss.replace(0, float("nan"))
            rsi   = (100 - 100 / (1 + rs)).iloc[-1]
            return round(float(rsi), 1) if not pd.isna(rsi) else None
        except Exception:
            return None

    rows = []
    for t in GLOBAL_MOMENTUM_TICKERS:
        row: dict = {
            "label":  t["label"],
            "ticker": t["ticker"],
            "region": t["region"],
            "type":   t["type"],
            "error":  None,
        }
        try:
            hist = yf.Ticker(t["ticker"]).history(period="60d", interval="1d", auto_adjust=True)
            if hist.empty or len(hist) < 2:
                row["error"] = "No data"
                rows.append(row)
                continue
            close      = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2])
            chg_pct    = (close - prev_close) / prev_close * 100 if prev_close else 0
            high_52w   = float(hist["Close"].max())
            low_52w    = float(hist["Close"].min())
            from_high  = (close - high_52w) / high_52w * 100 if high_52w else 0
            rsi_val    = _rsi(hist["Close"])

            row["close"]           = round(close, 2)
            row["chg_pct"]         = round(chg_pct, 2)
            row["rsi"]             = rsi_val
            row["from_52w_high"]   = round(from_high, 1)
            row["low_52w"]         = round(low_52w, 2)
            row["high_52w"]        = round(high_52w, 2)

            # ── Derive signal ─────────────────────────────────────────────
            if t["type"] == "Volatility":
                # VIX: high = fear, low = complacency
                if close > 30:
                    row["signal"] = "FEAR"
                    row["signal_detail"] = f"VIX {close:.1f} — high fear; expect volatility"
                elif close < 15:
                    row["signal"] = "COMPLACENCY"
                    row["signal_detail"] = f"VIX {close:.1f} — low fear; market overconfident"
                else:
                    row["signal"] = "NEUTRAL"
                    row["signal_detail"] = f"VIX {close:.1f} — moderate volatility (normal)"
            elif rsi_val is not None:
                if chg_pct > 1.5 and rsi_val < 70:
                    row["signal"] = "LONG BUILDUP"
                    row["signal_detail"] = f"+{chg_pct:.1f}% today; RSI {rsi_val} (room to run)"
                elif chg_pct > 0.5 and rsi_val >= 70:
                    row["signal"] = "OVERBOUGHT"
                    row["signal_detail"] = f"+{chg_pct:.1f}% today; RSI {rsi_val} (overbought caution)"
                elif chg_pct < -1.5 and rsi_val > 30:
                    row["signal"] = "SHORT BUILDUP"
                    row["signal_detail"] = f"{chg_pct:.1f}% today; RSI {rsi_val} (bearish pressure)"
                elif chg_pct < -0.5 and rsi_val <= 30:
                    row["signal"] = "OVERSOLD"
                    row["signal_detail"] = f"{chg_pct:.1f}% today; RSI {rsi_val} (watch for bounce)"
                elif abs(chg_pct) <= 0.5:
                    row["signal"] = "FLAT"
                    row["signal_detail"] = f"{chg_pct:+.1f}% today; RSI {rsi_val}"
                else:
                    row["signal"] = "WATCH"
                    row["signal_detail"] = f"{chg_pct:+.1f}% today; RSI {rsi_val}"
            else:
                row["signal"] = "FLAT" if abs(chg_pct) <= 0.5 else ("LONG BUILDUP" if chg_pct > 0 else "SHORT BUILDUP")
                row["signal_detail"] = f"{chg_pct:+.1f}% today"
        except Exception as ex:
            row["error"] = str(ex)
        rows.append(row)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# HTML Report Builder
# ─────────────────────────────────────────────────────────────────────────────

_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:0}
h1{background:linear-gradient(135deg,#1f6feb,#58a6ff);padding:18px 24px;margin:0;font-size:1.4rem}
h2{color:#58a6ff;padding:8px 24px;margin:8px 0 0;font-size:1.05rem;border-bottom:1px solid #30363d}
.section{padding:12px 24px 24px}
table{width:100%;border-collapse:collapse;font-size:.88rem;margin-top:8px}
th{background:#161b22;color:#8b949e;text-align:left;padding:7px 10px;border-bottom:2px solid #30363d}
td{padding:6px 10px;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:#161b22}
.num{text-align:right;font-variant-numeric:tabular-nums}
.rich{color:#f85149;font-weight:600}
.cheap{color:#3fb950;font-weight:600}
.fair{color:#8b949e}
.contango{color:#d29922;font-weight:600}
.backwardation{color:#58a6ff;font-weight:600}
.flat{color:#8b949e}
.bull{color:#3fb950;font-weight:600}
.bear{color:#f85149;font-weight:600}
.neutral{color:#d29922}
.error{color:#f85149;font-style:italic}
.pos{color:#3fb950;font-weight:600}
.neg{color:#f85149;font-weight:600}
.hold{color:#8b949e}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.8rem;font-weight:600}
.tag-rich{background:#3d1515;color:#f85149}
.tag-cheap{background:#152b1e;color:#3fb950}
.tag-fair{background:#21262d;color:#8b949e}
.tag-contango{background:#2d2008;color:#d29922}
.tag-backwardation{background:#0d1f36;color:#58a6ff}
.tag-flat{background:#21262d;color:#8b949e}
.meta{color:#8b949e;font-size:.82rem;padding:4px 24px 12px}
"""

_SIGNAL_CLASS = {
    "RICH": "rich", "CHEAP": "cheap", "FAIR": "fair",
    "CONTANGO": "contango", "BACKWARDATION": "backwardation", "FLAT": "flat",
    "Long Built Up": "bull", "Short Built Up": "bear",
    "Short Covering": "bull", "Long Unwinding": "bear",
}
_SIGNAL_TAG_CLASS = {
    "RICH": "tag-rich", "CHEAP": "tag-cheap", "FAIR": "tag-fair",
    "CONTANGO": "tag-contango", "BACKWARDATION": "tag-backwardation", "FLAT": "tag-flat",
}


def _fmt(v, fmt=".2f", fallback="—") -> str:
    if v is None:
        return fallback
    try:
        return format(float(v), fmt)
    except Exception:
        return str(v)


def _tag(signal: str) -> str:
    css = _SIGNAL_TAG_CLASS.get(signal, "tag-fair")
    return f'<span class="tag {css}">{signal}</span>'


def _trade_guide_html(r: dict, currency: str = "₹") -> str:
    """Build a plain-English trade guide cell for a CHEAP/RICH/FAIR row."""
    sig          = r.get("signal", "FAIR")
    lot_value    = r.get("lot_value", 0)
    profit       = r.get("profit_per_lot", 0)
    tv_fut       = r.get("tv_fut", "")
    fut_sym      = tv_fut.split(":")[-1]            # e.g. NIFTY1!
    exchange     = tv_fut.split(":")[0] if ":" in tv_fut else ""
    lot_label    = r.get("multiplier", r.get("lot_size", 1))  # contracts use multiplier
    spot_etf     = r.get("spot_etf", "")
    dte          = r.get("dte", "?")
    etf_price    = r.get("etf_price")               # live ETF price fetched at runtime
    etf_units    = r.get("etf_units")               # units needed to replicate 1 contract
    expiry_label = r.get("expiry_label", "")

    # Contract identity line (which contract, which expiry)
    if expiry_label:
        contract_note = (
            f"<span style='color:#58a6ff;font-size:.75rem'>"
            f"{exchange}:{fut_sym} &nbsp;·&nbsp; {expiry_label} &nbsp;·&nbsp; {dte}d left"
            f"</span>"
        )
    else:
        contract_note = (
            f"<span style='color:#58a6ff;font-size:.75rem'>"
            f"{fut_sym} ({lot_label}-unit lot, {dte}d to exp.)"
            f"</span>"
        )

    # ETF description with quantity
    if etf_units and etf_price:
        etf_desc = (
            f"<b>{spot_etf}</b> ETF "
            f"<span style='color:#8b949e'>~{etf_units:,} units "
            f"({currency}{etf_price:,.2f}/unit)</span>"
        )
    elif spot_etf:
        etf_desc = f"<b>{spot_etf}</b> ETF"
    else:
        etf_desc = "index basket (spot)"

    # Format lot value
    if currency == "₹":
        lot_str = f"₹{lot_value/1e5:.1f}L" if lot_value >= 1e5 else f"₹{lot_value:,.0f}"
    else:
        lot_str = f"{currency}{lot_value:,.0f}"

    profit_str = f"{currency}{profit:,.0f}" if profit > 0 else "—"

    if sig == "CHEAP":
        action  = f'<span style="color:#3fb950">▲ Buy</span> 1 contract · <b>{fut_sym}</b> ({lot_label}-unit lot)'
        action2 = f'<span style="color:#f85149">▼ Sell</span> {etf_desc}'
        color   = "#3fb950"
    elif sig == "RICH":
        action  = f'<span style="color:#f85149">▼ Sell</span> 1 contract · <b>{fut_sym}</b> ({lot_label}-unit lot)'
        action2 = f'<span style="color:#3fb950">▲ Buy</span> {etf_desc}'
        color   = "#f85149"
    else:
        return "<span style='color:#8b949e;font-size:.8rem'>No clear opportunity — prices are fair, wait</span>"

    return (
        f"<div style='font-size:.8rem;line-height:1.7'>"
        f"{contract_note}<br>"
        f"{action}<br>{action2}<br>"
        f"<span style='color:#8b949e'>Notional ≈ {lot_str}/contract &nbsp;·&nbsp; "
        f"Est. profit <span style='color:{color}'>{profit_str}/contract</span> if gap closes"
        f"</span>"
        f"</div>"
    )


def _nse_arb_section(rows: list) -> str:
    if not rows:
        return "<p class='meta'>No NSE index arbitrage data.</p>"
    ths = ["Index", "Spot", "Futures", "Basis %", "Fair Basis %", "Mispricing (bps)", "DTE", "Signal", "What to do (plain English)"]
    html = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in ths) + "</tr></thead><tbody>"
    for r in rows:
        if r.get("error"):
            html += f"<tr><td>{r['name']}</td><td colspan='8' class='error'>{r['error']}</td></tr>"
            continue
        sig      = r.get("signal", "FAIR")
        sig_cls  = _SIGNAL_CLASS.get(sig, "fair")
        mis_bps  = r.get("mispricing_bps", 0)
        mis_s    = f"{mis_bps:+.1f}" if mis_bps is not None else "—"
        warning  = r.get("basis_warning")
        # If basis is unreliable, override the signal display
        if warning:
            signal_td  = f'<span class="tag tag-fair" style="background:#2d2008;color:#d29922">⚠ UNRELIABLE</span>'
            guide_td   = f"<span style='color:#d29922;font-size:.8rem'>{warning}</span>"
        else:
            signal_td  = _tag(sig)
            guide_td   = _trade_guide_html(r, '₹')
        html += (
            f"<tr>"
            f"<td><strong>{r['name']}</strong></td>"
            f"<td class='num'>{_fmt(r.get('spot'), ',.2f')}</td>"
            f"<td class='num'>{_fmt(r.get('futures'), ',.2f')}</td>"
            f"<td class='num {'neutral' if warning else sig_cls}'>{_fmt(r.get('basis_pct'), '+.3f')}%</td>"
            f"<td class='num'>{_fmt(r.get('fair_basis_pct'), '.3f')}%</td>"
            f"<td class='num {'neutral' if warning else sig_cls}'>{mis_s}</td>"
            f"<td class='num'>{r.get('dte', '—')}</td>"
            f"<td>{signal_td}</td>"
            f"<td>{guide_td}</td>"
            f"</tr>"
        )
    html += "</tbody></table>"
    return html


def _global_arb_section(rows: list) -> str:
    if not rows:
        return "<p class='meta'>No global index arbitrage data.</p>"
    ths = ["Index", "Spot", "Futures", "Basis %", "Fair Basis %", "Mispricing (bps)", "DTE", "Signal", "What to do (plain English)"]
    html = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in ths) + "</tr></thead><tbody>"
    for r in rows:
        if r.get("error"):
            html += f"<tr><td>{r['name']}</td><td colspan='8' class='error'>{r['error']}</td></tr>"
            continue
        sig      = r.get("signal", "FAIR")
        sig_cls  = _SIGNAL_CLASS.get(sig, "fair")
        mis_bps  = r.get("mispricing_bps", 0)
        mis_s    = f"{mis_bps:+.1f}" if mis_bps is not None else "—"
        currency = r.get("currency", "USD")
        cur_sym  = "A$" if currency == "AUD" else "$"

        # Basis warning (e.g. FINNIFTY ^CNXFIN mismatch, STI / SGX mismatch)
        basis_warn = r.get("basis_warning")
        if basis_warn:
            warn_note = r.get("spot_yf_note", basis_warn)
            sig_cell  = f'<span class="tag tag-fair" style="background:#2d2008;color:#d29922">⚠ UNRELIABLE</span>'
            action_cell = f"<span style='color:#d29922;font-size:.8rem'>⚠ {warn_note}</span>"
        else:
            sig_cell    = _tag(sig)
            action_cell = _trade_guide_html(r, cur_sym)
        html += (
            f"<tr>"
            f"<td><strong>{r['name']}</strong> <span style='color:#8b949e;font-size:.78rem'>{currency}</span></td>"
            f"<td class='num'>{_fmt(r.get('spot'), ',.2f')}</td>"
            f"<td class='num'>{_fmt(r.get('futures'), ',.2f')}</td>"
            f"<td class='num {sig_cls}'>{_fmt(r.get('basis_pct'), '+.3f')}%</td>"
            f"<td class='num'>{_fmt(r.get('fair_basis_pct'), '.3f')}%  <span style='color:#8b949e;font-size:.76rem'>({r.get('rfi_pct',4.5)}% RFI)</span></td>"
            f"<td class='num {sig_cls}'>{mis_s}</td>"
            f"<td class='num'>{r.get('dte', '—')}</td>"
            f"<td>{sig_cell}</td>"
            f"<td>{action_cell}</td>"
            f"</tr>"
        )
    html += "</tbody></table>"
    return html


def _forex_basis_section(rows: list) -> str:
    if not rows:
        return "<p class='meta'>No forex basis data.</p>"
    ths = ["Pair", "TV Spot", "CME Futures", "Basis (pips)", "Signal", "Notes"]
    html = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in ths) + "</tr></thead><tbody>"
    for r in rows:
        if r.get("error"):
            html += f"<tr><td>{r['pair']}</td><td colspan='5' class='error'>{r['error']}</td></tr>"
            continue
        sig     = r.get("signal", "FLAT")
        sig_cls = _SIGNAL_CLASS.get(sig, "flat")
        bp      = r.get("basis_pips", 0)
        bp_s    = f"{bp:+.1f}" if bp is not None else "—"
        html += (
            f"<tr>"
            f"<td><strong>{r['pair']}</strong></td>"
            f"<td class='num'>{_fmt(r.get('spot'), '.5f')}</td>"
            f"<td class='num'>{_fmt(r.get('futures'), '.5f')}</td>"
            f"<td class='num {sig_cls}'>{bp_s}</td>"
            f"<td>{_tag(sig)}</td>"
            f"<td class='meta'>{r.get('signal_detail', '')}</td>"
            f"</tr>"
        )
    html += "</tbody></table>"
    return html


def _oi_buildup_section(oi_data: dict) -> str:
    error = oi_data.get("error")
    sections = []
    type_labels = [
        ("long_buildup",   "Long Buildup — Price↑ + OI↑ (Bulls Adding)",   "bull"),
        ("short_covering", "Short Covering — Price↑ + OI↓ (Bears Exiting)", "bull"),
        ("short_buildup",  "Short Buildup — Price↓ + OI↑ (Bears Adding)",  "bear"),
        ("long_unwinding", "Long Unwinding — Price↓ + OI↓ (Bulls Exiting)", "bear"),
    ]
    ths = ["Symbol", "LTP", "Chg %", "OI", "OI Chg", "Signal"]
    for key, label, css in type_labels:
        rows = oi_data.get(key, [])
        if not rows:
            continue
        html = f"<h3 style='color:#8b949e;font-size:.95rem;padding:12px 0 4px'>{label}</h3>"
        html += "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in ths) + "</tr></thead><tbody>"
        for r in rows:
            chg = r.get("pct_change")
            chg_s = f"{chg:+.2f}%" if chg is not None else "—"
            oi   = r.get("oi")
            oi_s = f"{oi/1e5:.2f}L" if oi and oi > 1e5 else (_fmt(oi, ".0f") if oi else "—")
            oi_c = r.get("oi_change")
            oi_c_s = f"{oi_c:+,.0f}" if oi_c is not None else "—"
            html += (
                f"<tr>"
                f"<td><strong>{r.get('symbol', '—')}</strong></td>"
                f"<td class='num'>{_fmt(r.get('ltp'), ',.2f')}</td>"
                f"<td class='num {css}'>{chg_s}</td>"
                f"<td class='num'>{oi_s}</td>"
                f"<td class='num {css}'>{oi_c_s}</td>"
                f"<td class='{css}'>{r.get('buildup_type', '—')}</td>"
                f"</tr>"
            )
        html += "</tbody></table>"
        sections.append(html)
    if error:
        sections.append(f"<p class='error'>⚠ {error}</p>")
    return "\n".join(sections) if sections else "<p class='meta'>No OI buildup data.</p>"


_ARITH_GLOSSARY = """
<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;
            padding:12px 18px;margin:8px 0 14px;font-size:.83rem;line-height:1.7">
  <strong style="color:#58a6ff">📖 What does this table mean? (plain English)</strong><br><br>
  <b>Spot price</b> — the index value <em>right now</em> (e.g. NIFTY = 24,050). This is what you'd pay to buy<br>&nbsp;&nbsp;&nbsp;&nbsp;the entire market basket today.<br>
  <b>Futures price</b> — the <em>agreed price today</em> for buying/selling the same index in a few weeks.
    Futures are usually slightly higher than spot because you are locking in a price for later.<br>
  <b>Basis %</b> — how much higher (or lower) the futures are vs spot, in percentage =
    (Futures − Spot) ÷ Spot × 100.<br>
  <b>Fair Basis %</b> — the difference <em>expected</em> based on the current bank interest rate:
    = Interest Rate × Days Left ÷ 365.
    <span style="color:#8b949e">(Example: 6.5% × 16 days ÷ 365 = 0.285%)</span><br>
  <b>Mispricing (bps)</b> — are futures priced above or below where they should be?<br>
    &nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#8b949e">1 bps = 0.01%. So 21 bps means futures are 0.21% cheap.</span><br>
    &nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#3fb950">Negative = futures are too cheap → profit by buying them + selling the equivalent ETF.</span><br>
    &nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#f85149">Positive = futures are too expensive → profit by selling them + buying the equivalent ETF.</span><br>
  <b>CHEAP</b> — futures are cheaper than fair value → buy the futures contract and simultaneously
    sell the matching ETF. Close both when prices converge.<br>
  <b>RICH</b> — futures are more expensive than fair value → sell the futures contract and buy the ETF.
    Risk-free profit if both sides close.<br>
  <b>⚠ Note</b> — execution needs a <em>margin account</em> for futures + demat for ETF. Prices must move
    faster than brokerage costs (~₹50–200/trade) + SPAN margin (~12–15% of contract value).
</div>
"""

_FOREX_GLOSSARY = """
<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;
            padding:12px 18px;margin:8px 0 14px;font-size:.83rem;line-height:1.7">
  <strong style="color:#58a6ff">📖 What does the Forex Basis table mean? (plain English)</strong><br><br>
  <b>TV Spot</b> — the currency exchange rate <em>right now</em> in the interbank market
    (e.g. EUR/USD = 1.176 means €1 buys $1.176 today).<br>
  <b>CME Futures</b> — the <em>agreed rate today</em> for exchanging the same currencies
    in a few weeks, traded on CME (Chicago Mercantile Exchange).<br>
  <b>Basis (pips)</b> — the gap between spot and futures. One <em>pip</em> = 0.0001 for most pairs
    (0.01 for JPY pairs).<br>
    &nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#d29922">CONTANGO: Futures &gt; Spot → the market expects the base currency to appreciate.</span><br>
    &nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#58a6ff">BACKWARDATION: Futures &lt; Spot → the market expects the base currency to fall.</span><br>
    &nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#8b949e">FLAT (±5 pips): Spot ≈ Futures — no significant directional bet.</span><br>
  <b>Why does this happen?</b> — In theory, the futures price should reflect the <em>interest rate
    differential</em> between the two countries (Covered Interest Rate Parity).
    If the US rate is higher than the Euro rate, USD futures trade at a
    <em>discount</em> to EUR/USD spot (EUR is in contango).<br>
  <b>Trading use</b> — A large basis (e.g. &gt;20 pips) may indicate:<br>
    &nbsp;&nbsp;&nbsp;&nbsp;• Currency flow imbalance (e.g. large institutional buy/sell orders)<br>
    &nbsp;&nbsp;&nbsp;&nbsp;• Rate differential change anticipated (central bank meeting ahead)<br>
    &nbsp;&nbsp;&nbsp;&nbsp;• Low-risk: borrow cheap currency, buy high-rate currency, lock in futures — <em>carry trade</em><br>
  <b>⚠ Note</b> — FX futures require a CME-connected broker (IBKR, Interactive Brokers Forex, etc.).
    Retail spot FX margins are small but spreads eat into arb profits quickly.
</div>
"""

_OI_GLOSSARY = """
<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;
            padding:12px 18px;margin:8px 0 14px;font-size:.83rem;line-height:1.7">
  <strong style="color:#58a6ff">📖 What is OI Buildup? (plain English)</strong><br><br>
  <b>OI (Open Interest)</b> — total number of <em>live futures/options contracts</em> outstanding for a stock.
    When OI rises, new positions are being created. When OI falls, positions are closing.<br><br>
  <b>Four patterns — what they mean:</b><br><br>
  &nbsp;&nbsp;<span style="color:#3fb950">■</span> <b>Long Buildup</b> (Price ↑ + OI ↑) —
    Price is rising AND new contracts are being opened → <em>fresh bulls are entering</em>.
    Strong signal: momentum likely continues. Look to <b>buy</b> or hold long.<br>
  &nbsp;&nbsp;<span style="color:#3fb950">■</span> <b>Short Covering</b> (Price ↑ + OI ↓) —
    Price is rising AND old contracts are closing → <em>bears are giving up and buying back</em>.
    Can be explosive: trapped shorts scrambling to exit can push prices sharply higher.<br>
  &nbsp;&nbsp;<span style="color:#f85149">■</span> <b>Short Buildup</b> (Price ↓ + OI ↑) —
    Price is falling AND new contracts are being opened → <em>fresh bears are entering</em>.
    Bearish signal: momentum down, large players expecting further decline. Avoid or <b>hedge</b> longs.<br>
  &nbsp;&nbsp;<span style="color:#f85149">■</span> <b>Long Unwinding</b> (Price ↓ + OI ↓) —
    Price is falling AND old contracts are closing → <em>bulls are giving up and selling</em>.
    Weak bullish conviction: existing longs exiting in bulk — cautious signal.<br><br>
  <b>Why only NSE India?</b> — OI buildup data requires real-time F&amp;O contract-level feeds.
    This report uses AngelOne SmartAPI which provides NSE F&amp;O OI data.
    US/HK/JP markets require separate exchange-level data subscriptions (CBOE, HKEX, OSE).
    US put/call ratio and sector rotation signals can be added in a future version.<br>
  <b>⚠ Note</b> — OI data has a ~15-min delay on AngelOne. Use for directional bias only,
    not for precise entry/exit timing. Always confirm with live price action.
</div>
"""


_GLOBAL_MOMENTUM_GLOSSARY = """
<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;
            padding:12px 18px;margin:8px 0 14px;font-size:.83rem;line-height:1.7">
  <strong style="color:#58a6ff">📖 What are Global Market Momentum Signals? (plain English)</strong><br><br>
  Unlike NSE F&amp;O OI data (which requires a real-time derivatives exchange feed),
  these signals use <em>publicly available price data</em> from yfinance — so they work for
  every major market worldwide.<br><br>
  <b>How signals are derived:</b><br>
  &nbsp;&nbsp;&nbsp;&nbsp;• <b>1-day Chg%</b>: How much the index/asset moved <em>today</em> vs yesterday's close.<br>
  &nbsp;&nbsp;&nbsp;&nbsp;• <b>RSI-14</b>: Relative Strength Index over 14 days —
    a momentum meter. Above 70 = overbought (potentially tired). Below 30 = oversold (potential bounce).<br>
  &nbsp;&nbsp;&nbsp;&nbsp;• <b>From 52W High</b>: How far the price has fallen from its recent peak — a drawdown measure.<br><br>
  <b>Signal meanings:</b><br>
  &nbsp;&nbsp;<span style="color:#3fb950">■ LONG BUILDUP</span> — Rising strongly today + RSI has room → momentum intact, potential entry.<br>
  &nbsp;&nbsp;<span style="color:#f85149">■ SHORT BUILDUP</span> — Falling today + RSI still above 30 → bearish pressure, protect longs.<br>
  &nbsp;&nbsp;<span style="color:#d29922">■ OVERBOUGHT</span> — Rising but RSI &gt; 70 → extended; pullback risk.<br>
  &nbsp;&nbsp;<span style="color:#58a6ff">■ OVERSOLD</span> — Falling but RSI &lt; 30 → deeply oversold; watch for reversal.<br>
  &nbsp;&nbsp;<span style="color:#8b949e">■ FLAT</span> — Less than ±0.5% move — consolidation or market holiday.<br>
  &nbsp;&nbsp;<span style="color:#f85149">■ FEAR (VIX)</span> — VIX above 30 → high nervousness, large swings expected.<br><br>
  <b>Why this instead of OI?</b><br>
  OI (open interest) data for US/HK/JP/SG markets requires paid exchange subscriptions
  (CBOE, HKEX, OSE). Until those are integrated, price-action RSI signals are the best
  free proxy for global market momentum. India NSE OI data above remains the gold standard
  for F&amp;O participants.
</div>
"""


def _global_momentum_section(rows: list) -> str:
    if not rows:
        return ""
    # Group by region for readability
    from itertools import groupby
    rows_sorted = sorted(rows, key=lambda r: (r.get("region", "z"), r.get("label", "")))
    html = ""
    for region, group in groupby(rows_sorted, key=lambda r: r.get("region", "")):
        group_list = list(group)
        html += f"<h3 style='color:#8b949e;font-size:.95rem;padding:12px 0 4px'>{region}</h3>"
        html += "<table><thead><tr>"
        html += "<th>Asset</th><th>Ticker</th><th>Type</th>"
        html += "<th class='num'>Close</th><th class='num'>1D Chg%</th>"
        html += "<th class='num'>RSI-14</th><th class='num'>From 52W High</th>"
        html += "<th>Signal</th><th>Detail</th>"
        html += "</tr></thead><tbody>"
        for r in group_list:
            if r.get("error"):
                html += f"<tr><td><strong>{r['label']}</strong></td>"
                html += f"<td>{r['ticker']}</td><td>{r.get('type','')}</td>"
                html += f"<td colspan='6' class='error'>{r['error']}</td></tr>"
                continue
            chg     = r.get("chg_pct", 0)
            rsi_v   = r.get("rsi")
            from_hi = r.get("from_52w_high", 0)
            sig     = r.get("signal", "FLAT")
            close   = r.get("close", 0)

            chg_cls  = "pos" if chg >= 0 else "neg"
            chg_str  = f"+{chg:.2f}%" if chg >= 0 else f"{chg:.2f}%"
            rsi_str  = f"{rsi_v:.1f}" if rsi_v is not None else "—"
            rsi_cls  = " style='color:#f85149'" if rsi_v and rsi_v > 70 else \
                       (" style='color:#3fb950'" if rsi_v and rsi_v < 30 else "")
            hi_cls   = "neg" if from_hi < -10 else ("pos" if from_hi > -3 else "hold")
            hi_str   = f"{from_hi:.1f}%"

            sig_map = {
                "LONG BUILDUP":  ("background:#152b1e;color:#3fb950",    "↑ LONG BUILDUP"),
                "SHORT BUILDUP": ("background:#3d1515;color:#f85149",    "↓ SHORT BUILDUP"),
                "OVERBOUGHT":    ("background:#2d2008;color:#d29922",    "⚠ OVERBOUGHT"),
                "OVERSOLD":      ("background:#0d1f36;color:#58a6ff",    "↕ OVERSOLD"),
                "FEAR":          ("background:#3d1515;color:#f85149",    "⚠ FEAR"),
                "COMPLACENCY":   ("background:#2d2008;color:#d29922",    "⚠ COMPLACENCY"),
                "NEUTRAL":       ("background:#21262d;color:#8b949e",    "— NEUTRAL"),
                "FLAT":          ("background:#21262d;color:#8b949e",    "— FLAT"),
                "WATCH":         ("background:#161b22;color:#d29922",    "👀 WATCH"),
            }
            s_style, s_txt = sig_map.get(sig, ("background:#21262d;color:#8b949e", sig))

            html += f"<tr>"
            html += f"<td><strong>{r['label']}</strong></td>"
            html += f"<td style='color:#8b949e;font-size:.8rem'>{r['ticker']}</td>"
            html += f"<td style='color:#8b949e;font-size:.8rem'>{r.get('type','')}</td>"
            html += f"<td class='num'>{close:,.2f}</td>"
            html += f"<td class='num {chg_cls}' style='font-weight:600'>{chg_str}</td>"
            html += f"<td class='num'{rsi_cls}>{rsi_str}</td>"
            html += f"<td class='num {hi_cls}'>{hi_str}</td>"
            html += f"<td><span style='display:inline-block;padding:3px 8px;border-radius:4px;font-size:.82rem;font-weight:600;{s_style}'>{s_txt}</span></td>"
            html += f"<td style='font-size:.8rem;color:#8b949e'>{r.get('signal_detail','')}</td>"
            html += "</tr>"
        html += "</tbody></table>"
    return html


def build_html(
    nse_rows:       list,
    forex_rows:     list,
    oi_data:        dict,
    rfi_pct:        float,
    args,
    global_rows:    list | None = None,
    momentum_rows:  list | None = None,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    has_oi = bool(oi_data.get("long_buildup") or oi_data.get("short_buildup")
                  or oi_data.get("short_covering") or oi_data.get("long_unwinding"))

    nse_section        = _nse_arb_section(nse_rows)             if nse_rows       else ""
    forex_section      = _forex_basis_section(forex_rows)       if forex_rows     else ""
    oi_section         = _oi_buildup_section(oi_data)           if has_oi         else ""
    global_section     = _global_arb_section(global_rows)       if global_rows    else ""
    momentum_section   = _global_momentum_section(momentum_rows) if momentum_rows  else ""

    out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>F&amp;O / Forex Arbitrage Report — {ts}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>⚖ F&amp;O / Forex Arbitrage Engine &nbsp;|&nbsp; {ts}</h1>
<p class="meta">
  India risk-free rate (RBI repo): <strong>{rfi_pct}%</strong> p.a. &nbsp;|&nbsp;
  DTE basis: near-month NSE expiry &nbsp;|&nbsp;
  Spot: yfinance (^NSEI, ^NSEBANK, ^CNXFIN) &nbsp;|&nbsp;
  Futures: TV Pro (NSE:NIFTY1!, NSE:BANKNIFTY1!, NSE:FINNIFTY1!)
</p>
<p class="meta" style="color:#d29922">⚠ <strong>FINNIFTY note:</strong> yfinance <code>^CNXFIN</code> (Nifty Financial Services) may not exactly match
  the FINNIFTY1! futures underlying — large basis values for FINNIFTY are unreliable. Use Nifty 50 / BankNifty for actual arb trading.
</p>
"""
    if nse_section:
        out += f"""
<h2>📊 NSE Index Arbitrage (Spot vs Near-Month Futures)</h2>
<div class="section">
  {_ARITH_GLOSSARY}
  {nse_section}
</div>
"""
    if global_section:
        out += f"""
<h2>🌏 Global Index Arbitrage — US (S&amp;P 500, NASDAQ) + ASX 200 + HK + Japan + Singapore</h2>
<div class="section">
  <p class="meta">
    Spot: yfinance (^GSPC, ^NDX, ^AXJO, ^HSI, ^N225, ^STI) &nbsp;|&nbsp;
    Futures: TV Pro (CME:ES1!, CME:NQ1!, ASX:SPI200!, HKEX:HSI1!, CME:NKD1!, SGX:CN1!) &nbsp;|&nbsp;
    DTE: next CME quarterly expiry (3rd Friday of Mar/Jun/Sep/Dec) &nbsp;|&nbsp;
    US/HK RFI ≈ 4.5%/5.25% (T-bill/HKMA) · ASX/SG RFI ≈ 4.1%/3.7% · JP RFI ≈ 0.5% (BOJ)
  </p>
  {global_section}
</div>
"""
    if forex_section:
        out += f"""
<h2>💱 G10 Forex Basis (TV Spot vs CME Futures)</h2>
<div class="section">
  <p class="meta">
    Source: TV Pro canonical FX spot &nbsp;|&nbsp; CME FX continuous futures (yfinance)
    &nbsp;|&nbsp; Basis &gt;±5 pips flagged
  </p>
  {_FOREX_GLOSSARY}
  {forex_section}
</div>
"""
    if oi_section:
        out += f"""
<h2>🇮🇳 NSE F&amp;O OI Buildup Signals (AngelOne)</h2>
<div class="section">
  <p class="meta">
    Near-month expiry &nbsp;|&nbsp; Top 10 per category by absolute OI change
    &nbsp;|&nbsp; Long Buildup + Short Covering = bullish bets; Short Buildup + Long Unwinding = bearish bets
  </p>
  {_OI_GLOSSARY}
  {oi_section}
</div>
"""
    if momentum_section:
        out += f"""
<h2>🌐 Global Market Momentum (Price-Action Signals)</h2>
<div class="section">
  <p class="meta">
    Source: yfinance 60-day daily data &nbsp;|&nbsp; Signals: Price change % + RSI-14
    &nbsp;|&nbsp; Covers US, Europe, Asia-Pac, Commodities, FX
  </p>
  {_GLOBAL_MOMENTUM_GLOSSARY}
  {momentum_section}
</div>
"""
    out += "</body></html>"
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Reconfigure stdout to UTF-8 so emoji/unicode prints work on Windows cp1252
    if hasattr(sys.stdout, "reconfigure"):
        if sys.platform == "win32":  # cp1252 fix — not needed on macOS/Linux (already UTF-8)
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        description="F&O / Forex Arbitrage Engine",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--rfi",       type=float, default=DEFAULT_RFI,
                        help="India risk-free rate %% p.a. (RBI repo rate)")
    parser.add_argument("--no-india",  action="store_true",
                        help="Skip NSE index arbitrage + OI buildup (no AngelOne needed)")
    parser.add_argument("--no-global", action="store_true",
                        help="Skip US/ASX global index arbitrage section")
    parser.add_argument("--no-forex",  action="store_true",
                        help="Skip G10 forex basis section")
    parser.add_argument("--no-oi",     action="store_true",
                        help="Skip AngelOne OI buildup section")
    parser.add_argument("--no-momentum", action="store_true",
                        help="Skip global market momentum section (yfinance)")
    parser.add_argument("--out",       type=str, default=str(OUTPUT_HTML),
                        help="Output HTML report path")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  F&O / Forex Arbitrage Engine")
    print(f"  RFI={args.rfi}%  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    t0 = time.time()
    nse_rows      = []
    global_rows   = []
    forex_rows    = []
    momentum_rows = []
    oi_data       = {"long_buildup": [], "short_buildup": [],
                     "short_covering": [], "long_unwinding": [], "error": None}

    # ── NSE Index Arbitrage ──────────────────────────────────────────────────
    if not args.no_india:
        print("  📊 NSE index arbitrage (yfinance spot + TV futures) …", end=" ", flush=True)
        nse_rows = fetch_nse_index_arb(args.rfi)
        ok = sum(1 for r in nse_rows if not r.get("error"))
        print(f"{ok}/{len(nse_rows)} contracts")
        for r in nse_rows:
            if r.get("error"):
                print(f"     ⚠ {r['name']}: {r['error']}")
            else:
                sig = r.get("signal", "")
                print(f"     {r['name']:12s}  spot={r['spot']:>10,.2f}  "
                      f"fut={r['futures']:>10,.2f}  "
                      f"basis={r['basis_pct']:+.3f}%  "
                      f"mis={r['mispricing_bps']:+.1f}bps  [{sig}]")

    # ── Global Index Arbitrage ───────────────────────────────────────────────
    if not args.no_global:
        print("  🌏 Global index arbitrage (yfinance spot + TV futures) …", end=" ", flush=True)
        global_rows = fetch_global_index_arb()
        ok = sum(1 for r in global_rows if not r.get("error"))
        print(f"{ok}/{len(global_rows)} contracts")
        for r in global_rows:
            if r.get("error"):
                print(f"     ⚠ {r['name']}: {r['error']}")
            else:
                sig = r.get("signal", "")
                cur = r.get("currency", "USD")
                print(f"     {r['name']:12s}  spot={r['spot']:>10,.2f}  "
                      f"fut={r['futures']:>10,.2f}  "
                      f"basis={r['basis_pct']:+.3f}%  "
                      f"mis={r['mispricing_bps']:+.1f}bps  [{sig}]  {cur}")

    # ── Forex Basis ──────────────────────────────────────────────────────────
    if not args.no_forex:
        print("\n  💱 Forex basis (TV spot vs CME futures) …", end=" ", flush=True)
        forex_rows = fetch_forex_basis()
        ok = sum(1 for r in forex_rows if not r.get("error"))
        print(f"{ok}/{len(forex_rows)} pairs")
        for r in forex_rows:
            if r.get("error"):
                print(f"     ⚠ {r['pair']}: {r['error']}")
            else:
                sig = r.get("signal", "")
                print(f"     {r['pair']:8s}  spot={r['spot']:.5f}  "
                      f"fut={r.get('futures', 0):.5f}  "
                      f"basis={r['basis_pips']:+.1f}pips  [{sig}]")

    # ── OI Buildup (AngelOne) ────────────────────────────────────────────────
    if not args.no_india and not args.no_oi:
        print("\n  🇮🇳 NSE F&O OI buildup (AngelOne) …", end=" ", flush=True)
        oi_data = fetch_oi_signals()
        n = sum(len(oi_data.get(k, [])) for k in
                ("long_buildup", "short_buildup", "short_covering", "long_unwinding"))
        print(f"{n} signals")
        if oi_data.get("error"):
            print(f"     ⚠ {oi_data['error']}")
        else:
            for cat in ("long_buildup", "short_buildup", "short_covering", "long_unwinding"):
                rows = oi_data.get(cat, [])
                if rows:
                    top3 = ", ".join(r["symbol"] for r in rows[:3])
                    print(f"     {cat:16s}: top3 = {top3}")

    # ── Global Market Momentum (yfinance price signals) ─────────────────────
    if not args.no_momentum:
        print("\n  🌐 Global market momentum (yfinance RSI signals) …", end=" ", flush=True)
        momentum_rows = fetch_global_momentum()
        ok = sum(1 for r in momentum_rows if not r.get("error"))
        print(f"{ok}/{len(momentum_rows)} assets")
        for r in momentum_rows:
            if r.get("error"):
                print(f"     ⚠ {r['label']}: {r['error']}")
            else:
                sig = r.get("signal", "")
                chg = r.get("chg_pct", 0)
                print(f"     {r['label']:20s}  {chg:+.2f}%  RSI={r.get('rsi') or '—':<5}  [{sig}]")

    # ── Build and save HTML ───────────────────────────────────────────────────
    print()
    html     = build_html(nse_rows, forex_rows, oi_data, args.rfi, args,
                          global_rows=global_rows if not args.no_global else [],
                          momentum_rows=momentum_rows if not args.no_momentum else [])
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        _rdb_p = str(Path(__file__).parent.parent.parent / "reports")
        if _rdb_p not in _sys.path:
            _sys.path.insert(0, _rdb_p)
        from report_db import save_fo_arbitrage_snapshot, archive_report as _arc, archive_before_write as _abw
        _abw(out_path)  # archive existing report before overwriting
    except Exception as _e:
        print(f"  [report_db] fo_arbitrage archive skipped: {_e}")
        _arc = _abw = save_fo_arbitrage_snapshot = None  # type: ignore

    out_path.write_text(html, encoding="utf-8")
    print(f"📄 Report saved → {out_path}  ({time.time()-t0:.1f}s)")
    print()

    try:
        if save_fo_arbitrage_snapshot is not None:
            save_fo_arbitrage_snapshot(nse_rows, forex_rows, global_rows)
            print(f"  [report_db] persisted fo_arbitrage snapshot to report_analytics.db")
        if _arc is not None:
            _arc(out_path)
    except Exception as _e:
        print(f"  [report_db] fo_arbitrage snapshot skipped: {_e}")


if __name__ == "__main__":
    main()

