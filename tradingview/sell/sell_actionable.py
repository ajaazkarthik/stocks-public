#!/usr/bin/env python3
"""
Sell Actionable Report
======================
For every open position in your Moomoo account, scores SELL URGENCY across
4 layers (mirroring the buy signal_stack.py architecture) and produces an
HTML card report sorted by highest sell priority.

Layers:
  L1 Sell Technical   (40%): ATR hard stop, trailing stop, MACD < Signal,
                              RSI decay, MA slope breakdown, P&L%, time stop
  L2 Fundamental Risk (20%): Overvaluation decay, negative earnings growth,
                              analyst target vs current, debt/equity, FCF
  L3 Sentiment Risk   (25%): Negative news VADER, analyst downgrades,
                              earnings miss streak, upcoming earnings risk
  L4 Market Internals (15%): Relative volume bearish, 52W range position,
                              VIX regime

Composite Sell Urgency = L1×0.40 + L2×0.20 + L3×0.25 + L4×0.15  (0–10)
Recommendation levels:
  ≥ 7.0  → 🔴 SELL NOW
  ≥ 5.0  → 🟡 REDUCE / WATCH
  ≥ 3.5  → 🟠 MONITOR
  < 3.5  → 🟢 HOLD

Usage:
    python sell_actionable.py
    python sell_actionable.py --out my_sell_report.html

Requirements:
    pip install moomoo-api yfinance pandas numpy vaderSentiment
"""

import sys, os, argparse, json, math, re, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

try:
    from moomoo import (
        OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK
    )
    _MOOMOO_OK = True
except ImportError:
    _MOOMOO_OK = False

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("ERROR: pip install yfinance pandas numpy")

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
    def vader_score(text: str) -> float:
        return _vader.polarity_scores(text)["compound"]
except ImportError:
    sys.exit("ERROR: pip install vaderSentiment")

import urllib.request, urllib.parse

# ─── Config ───────────────────────────────────────────────────────────────────
ACCOUNT_ID  = int(os.environ.get("MOOMOO_ACCOUNT_ID", ""))
HOST        = "127.0.0.1"
PORT        = 11111
FIRM        = SecurityFirm.FUTUAU if _MOOMOO_OK else None
TRD_ENV     = TrdEnv.REAL if _MOOMOO_OK else None
ROOT        = Path(__file__).parent.parent
OUTPUT_HTML = ROOT.parent / "reports" / "sell_actionable_report.html"

EXCHANGE_MAP = {
    "ASX": ".AX",  "ASX_DLY": ".AX",
    "NSE": ".NS",  "BSE": ".BO",
    "SGX": ".SI",  "HKEX": ".HK",
    "NYSE": "",    "NASDAQ": "", "AMEX": "", "BATS": "",
}
_TV_SCANNER = {
    "NYSE":    "https://scanner.tradingview.com/america/scan",
    "NASDAQ":  "https://scanner.tradingview.com/america/scan",
    "AMEX":    "https://scanner.tradingview.com/america/scan",
    "BATS":    "https://scanner.tradingview.com/america/scan",
    "ASX":     "https://scanner.tradingview.com/australia/scan",
    "ASX_DLY": "https://scanner.tradingview.com/australia/scan",
    "NSE":     "https://scanner.tradingview.com/india/scan",
    "BSE":     "https://scanner.tradingview.com/india/scan",
    "HKEX":    "https://scanner.tradingview.com/hongkong/scan",
    "SGX":     "https://scanner.tradingview.com/singapore/scan",
}
_NYSE_SET = {"XLV","SOXL","CURE","LRCX","XOM","JPM","GS","V","MA","WMT",
             "HD","UNH","JNJ","PFE","KO","PEP","BA","CAT","HON","GE","CVX"}

# ─── Single-stock leveraged ETF → underlying equity ───────────────────────────
# These ETFs track a single company, so we can use the underlying's fundamentals.
# Broad-index leveraged ETFs (SOXL, CURE, XLV, TQQQ, UPRO etc.) are NOT listed here.
# Sources: Direxion, GraniteShares, T-Rex/REX Shares, SGX DLCs (SocGen)
LEVERAGED_ETF_UNDERLYING: dict[str, str] = {
    # ── Direxion Bull 2X single-stock ETFs ─────────────────────────────────────
    "AAPU":  "AAPL",   # Direxion Daily AAPL Bull 2X
    "ADBU":  "ADBE",   # Direxion Daily ADBE Bull 2X
    "AMUU":  "AMD",    # Direxion Daily AMD Bull 2X
    "AMZU":  "AMZN",   # Direxion Daily AMZN Bull 2X
    "ASMU":  "ASML",   # Direxion Daily ASML Bull 2X
    "AVL":   "AVGO",   # Direxion Daily AVGO Bull 2X
    "BABU":  "BABA",   # Direxion Daily BABA Bull 2X
    "BOEU":  "BA",     # Direxion Daily BA Bull 2X
    "BRKU":  "BRK-B",  # Direxion Daily BRKB Bull 2X
    "CONX":  "COIN",   # Direxion Daily COIN Bull 2X
    "CSCL":  "CSCO",   # Direxion Daily CSCO Bull 2X
    "ELIL":  "LLY",    # Direxion Daily LLY Bull 2X
    "FRDU":  "F",      # Direxion Daily F Bull 2X
    "GGLL":  "GOOGL",  # Direxion Daily GOOGL Bull 2X
    "HODU":  "HOOD",   # Direxion Daily HOOD Bull 2X
    "LINT":  "INTC",   # Direxion Daily INTC Bull 2X
    "LMTL":  "LMT",    # Direxion Daily LMT Bull 2X
    "METU":  "META",   # Direxion Daily META Bull 2X
    "MRVU":  "MRVL",   # Direxion Daily MRVL Bull 2X
    "MSFU":  "MSFT",   # Direxion Daily MSFT Bull 2X
    "MUU":   "MU",     # Direxion Daily MU Bull 2X
    "NFXL":  "NFLX",   # Direxion Daily NFLX Bull 2X
    "NVDU":  "NVDA",   # Direxion Daily NVDA Bull 2X
    "ORCU":  "ORCL",   # Direxion Daily ORCL Bull 2X
    "PALU":  "PANW",   # Direxion Daily PANW Bull 2X
    "PLTU":  "PLTR",   # Direxion Daily PLTR Bull 2X
    "PYPU":  "PYPL",   # Direxion Daily PYPL Bull 2X
    "QCMU":  "QCOM",   # Direxion Daily QCOM Bull 2X
    "SHPU":  "SHOP",   # Direxion Daily SHOP Bull 2X
    "SOFA":  "SOFI",   # Direxion Daily SOFI Bull 2X
    "TSLL":  "TSLA",   # Direxion Daily TSLA Bull 2X
    "TSMX":  "TSM",    # Direxion Daily TSM Bull 2X
    "TXNU":  "TXN",    # Direxion Daily TXN Bull 2X
    "UNHU":  "UNH",    # Direxion Daily UNH Bull 2X
    "XOMX":  "XOM",    # Direxion Daily XOM Bull 2X
    # ── Direxion Bear 1X single-stock ETFs (inverse) ──────────────────────────
    "AAPD":  "AAPL",   # Direxion Daily AAPL Bear 1X
    "AMDD":  "AMD",    # Direxion Daily AMD Bear 1X
    "AMZD":  "AMZN",   # Direxion Daily AMZN Bear 1X
    "AVS":   "AVGO",   # Direxion Daily AVGO Bear 1X
    "BOED":  "BA",     # Direxion Daily BA Bear 1X
    "BRKD":  "BRK-B",  # Direxion Daily BRKB Bear 1X
    "CSCS":  "CSCO",   # Direxion Daily CSCO Bear 1X
    "ELIS":  "LLY",    # Direxion Daily LLY Bear 1X
    "FRDD":  "F",      # Direxion Daily F Bear 1X
    "GGLS":  "GOOGL",  # Direxion Daily GOOGL Bear 1X
    "LMTS":  "LMT",    # Direxion Daily LMT Bear 1X
    "METD":  "META",   # Direxion Daily META Bear 1X
    "MSFD":  "MSFT",   # Direxion Daily MSFT Bear 1X
    "MUD":   "MU",     # Direxion Daily MU Bear 1X
    "NFXS":  "NFLX",   # Direxion Daily NFLX Bear 1X
    "NVDD":  "NVDA",   # Direxion Daily NVDA Bear 1X
    "ORCS":  "ORCL",   # Direxion Daily ORCL Bear 1X
    "PALD":  "PANW",   # Direxion Daily PANW Bear 1X
    "PLTD":  "PLTR",   # Direxion Daily PLTR Bear 1X
    "QCMD":  "QCOM",   # Direxion Daily QCOM Bear 1X
    "SHPD":  "SHOP",   # Direxion Daily SHOP Bear 1X
    "TSLS":  "TSLA",   # Direxion Daily TSLA Bear 1X
    "TSMZ":  "TSM",    # Direxion Daily TSM Bear 1X
    "XOMZ":  "XOM",    # Direxion Daily XOM Bear 1X
    # ── GraniteShares single-stock ETFs ────────────────────────────────────────
    "NVDL":  "NVDA",   # GraniteShares 1.5x / 2x Long NVIDIA
    "CONL":  "COIN",   # GraniteShares 2x Long Coinbase
    "SMCX":  "SMCI",   # GraniteShares 2x Long Super Micro Computer
    "GOGU":  "GOOGL",  # GraniteShares 2x Long Alphabet
    "AMDL":  "AMD",    # GraniteShares 2x Long AMD (legacy ticker)
    "MULL":  "MU",     # GraniteShares 2x Long Micron
    # ── T-Rex / REX Shares 2X Long single-stock ETFs ──────────────────────────
    "MSTU":  "MSTR",   # T-Rex 2X Long MicroStrategy
    "MSTX":  "MSTR",   # T-Rex 2X Long MicroStrategy (alt)
    "NVDX":  "NVDA",   # T-Rex 2X Long NVIDIA
    "TSLT":  "TSLA",   # T-Rex 2X Long Tesla
    "AAPX":  "AAPL",   # T-Rex 2X Long Apple
    "GOOX":  "GOOGL",  # T-Rex 2X Long Alphabet
    "MSFO":  "MSFT",   # T-Rex 2X Long Microsoft
    "MSFX":  "MSFT",   # T-Rex 2X Long Microsoft (alt)
    "PTIR":  "PLTR",   # T-Rex 2X Long Palantir
    "OKLL":  "OKLO",   # T-Rex 2X Long OKLO
    "NFLU":  "NFLX",   # T-Rex 2X Long Netflix
    "ROBN":  "HOOD",   # T-Rex 2X Long Robinhood
    "DKUP":  "DKNG",   # T-Rex 2X Long DraftKings
    "ARMU":  "ARM",    # T-Rex 2X Long Arm Holdings
    "DJTU":  "DJT",    # T-Rex 2X Long Trump Media
    "RBLU":  "RBLX",   # T-Rex 2X Long Roblox
    "GMEU":  "GME",    # T-Rex 2X Long GameStop
    "SNOU":  "SNOW",   # T-Rex 2X Long Snowflake
    "SMUP":  "SMR",    # T-Rex 2X Long NuScale Power
    "AXUP":  "AXON",   # T-Rex 2X Long Axon Enterprise
    "KTUP":  "KTOS",   # T-Rex 2X Long Kratos Defense
    "TTDU":  "TTD",    # T-Rex 2X Long Trade Desk
    "BKNU":  "BKNG",   # T-Rex 2X Long Booking Holdings
    # ── T-Rex / REX Shares 2X Inverse single-stock ETFs ───────────────────────
    "MSTZ":  "MSTR",   # T-Rex 2X Inverse MicroStrategy
    "NVDQ":  "NVDA",   # T-Rex 2X Inverse NVIDIA
    "TSLZ":  "TSLA",   # T-Rex 2X Inverse Tesla
    # ── Tradr / Defiance / Leverage Shares 2X single-stock ETFs ───────────────
    # Names confirmed against TradingView symbol data, not inferred from tickers:
    # APPX is AppLovin (APP), not Apple.
    "APPX":  "APP",    # Tradr 2X Long APP Daily
    "AVGX":  "AVGO",   # Defiance Daily Target 2X Long AVGO
    "CWVX":  "CRWV",   # Tradr 2X Long CRWV Daily
    "KLAG":  "KLAC",   # Leverage Shares 2x Long KLAC Daily
    "LRCU":  "LRCX",   # Tradr 2X Long LRCX Daily
    "NOWL":  "NOW",    # GraniteShares 2x Long NOW Daily
    "NVOX":  "NVO",    # Defiance Daily Target 2X Long NVO
    # ── SGX DLCs (SocGen daily-leveraged certificates, Moomoo SG) ─────────────
    # Legacy placeholder tickers (kept for backwards-compat; may not match live portfolio)
    "NVDDU": "NVDA",   # 3x NVDA Bull DLC (SGX — placeholder ticker)
    "AMDDU": "AMD",    # 3x AMD Bull DLC (SGX — placeholder ticker)
    "TSLDBU":"TSLA",   # 3x TSLA Bull DLC (SGX — placeholder ticker)
    "METDU": "META",   # 3x META Bull DLC (SGX — placeholder ticker)
    "AAPDU": "AAPL",   # 3x AAPL Bull DLC (SGX — placeholder ticker)
    "AMZDU": "AMZN",   # 3x AMZN Bull DLC (SGX — placeholder ticker)
    "GGLDU": "GOOGL",  # 3x GOOGL Bull DLC (SGX — placeholder ticker)
    "MSFDU": "MSFT",   # 3x MSFT Bull DLC (SGX — placeholder ticker)
    "BDIDBU":"BIDU",   # 3x Baidu Bull DLC (SGX — placeholder ticker)
    # Live portfolio SGX DLC tickers (SocGen series, expiry-coded names)
    # ZOGW (HSI 7x) and UMCW (HSTECH 7x) are INDEX DLCs — no single-stock underlying
    "NVIW": "NVDA",    # NVDA 3xLongSG261006 — 3× NVDA Bull DLC
    "Z8TW": "AVGO",    # AVGO 3xLongSG280420 — 3× AVGO Bull DLC  ← user-reported bug
    "Z4WW": "AMD",     # AMD  3xLongSG280420 — 3× AMD  Bull DLC
    "USDW": "META",    # META 3xLongSG261006 — 3× META Bull DLC
    "USAW": "AMZN",    # AMZN 3xLongSG261006 — 3× AMZN Bull DLC
}

# Map yfinance underlying tickers to their TV pro_symbol (for TV fundamentals batch)
UNDERLYING_TO_TV: dict[str, str] = {
    "AAPL":  "NASDAQ:AAPL",
    "ADBE":  "NASDAQ:ADBE",
    "AMD":   "NASDAQ:AMD",
    "AMZN":  "NASDAQ:AMZN",
    "APP":   "NASDAQ:APP",
    "ARM":   "NASDAQ:ARM",
    "ASML":  "NASDAQ:ASML",
    "AVGO":  "NASDAQ:AVGO",
    "AXON":  "NASDAQ:AXON",
    "BA":    "NYSE:BA",
    "BABA":  "NYSE:BABA",
    "BIDU":  "NASDAQ:BIDU",
    "BKNG":  "NASDAQ:BKNG",
    "BRK-B": "NYSE:BRK.B",
    "COIN":  "NASDAQ:COIN",
    "CRWV":  "NASDAQ:CRWV",
    "CSCO":  "NASDAQ:CSCO",
    "DJT":   "NASDAQ:DJT",
    "DKNG":  "NASDAQ:DKNG",
    "F":     "NYSE:F",
    "GME":   "NYSE:GME",
    "GOOGL": "NASDAQ:GOOGL",
    "HOOD":  "NASDAQ:HOOD",
    "INTC":  "NASDAQ:INTC",
    "KLAC":  "NASDAQ:KLAC",
    "KTOS":  "NASDAQ:KTOS",
    "LLY":   "NYSE:LLY",
    "LMT":   "NYSE:LMT",
    "LRCX":  "NASDAQ:LRCX",
    "META":  "NASDAQ:META",
    "MRVL":  "NASDAQ:MRVL",
    "MSFT":  "NASDAQ:MSFT",
    "MSTR":  "NASDAQ:MSTR",
    "MU":    "NASDAQ:MU",
    "NFLX":  "NASDAQ:NFLX",
    "NOW":   "NYSE:NOW",
    "NVDA":  "NASDAQ:NVDA",
    "NVO":   "NYSE:NVO",
    "OKLO":  "NYSE:OKLO",
    "ORCL":  "NYSE:ORCL",
    "PANW":  "NASDAQ:PANW",
    "PLTR":  "NASDAQ:PLTR",   # moved NYSE → NASDAQ; NYSE:PLTR no longer resolves
    "PYPL":  "NASDAQ:PYPL",
    "QCOM":  "NASDAQ:QCOM",
    "RBLX":  "NYSE:RBLX",
    "SHOP":  "NYSE:SHOP",
    "SMCI":  "NASDAQ:SMCI",
    "SMR":   "NYSE:SMR",
    "SNOW":  "NYSE:SNOW",
    "SOFI":  "NASDAQ:SOFI",
    "TSLA":  "NASDAQ:TSLA",
    "TSM":   "NYSE:TSM",
    "TTD":   "NASDAQ:TTD",
    "TXN":   "NASDAQ:TXN",
    "UNH":   "NYSE:UNH",
    "XOM":   "NYSE:XOM",
}

# ─── Index / sector / commodity funds — no single-stock underlying ───────────
# Equity fundamentals do not apply to these, so scoring them as ordinary stocks
# produces neutral-fallback sub-scores that look like real analysis.  Tickers are
# stored unpadded; HK numerics are zero-stripped before lookup.
INDEX_FUND_TICKERS: frozenset[str] = frozenset({
    # Broad market / index
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VAS", "IVV",
    "TQQQ", "SQQQ", "UPRO", "SPXL", "SPXS", "TNA", "TZA",
    # Sector / thematic
    "XLV", "XLK", "XLF", "XLE", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
    "SOXL", "SOXS", "SMH", "TECL", "FAS", "LABU", "CURE", "ARKK", "YINN",
    # Commodity / crypto
    "GLD", "SLV", "GDX", "GDXJ", "USO", "UNG", "SHNY", "VBTC", "IBIT",
    # Geared index funds (ASX)
    "GEAR", "GGUS", "GNDQ", "G200", "GHHF", "LEVR",
    # HK index products (zero-stripped form)
    "7200", "7226", "3115", "2801", "2840", "2828", "2800",
    # SGX index DLCs — no single-stock underlying, unlike the SocGen stock DLCs
    "ZOGW", "UMCW",
    # Leveraged wrapper over a private company — underlying is not listed
    "SPAL",
})

# ─── Helpers ─────────────────────────────────────────────────────────────────

def tv_to_yf(symbol: str) -> str:
    if ":" not in symbol:
        return symbol
    exch, ticker = symbol.split(":", 1)
    ticker = ticker.replace("-", ".").strip()
    suffix = EXCHANGE_MAP.get(exch.upper(), "")
    if exch.upper() == "HKEX" and ticker.isdigit():
        ticker = ticker.zfill(4)
    return ticker + suffix

def moomoo_to_tv(code: str) -> str:
    """Convert Moomoo OpenD code to TradingView symbol (e.g. US.AAPL, AU.CBA, HK.00700)."""
    if "." not in code:
        return code
    prefix, ticker = code.split(".", 1)
    prefix = prefix.upper()
    if prefix == "AU":  return f"ASX:{ticker}"
    if prefix == "HK":  return f"HKEX:{ticker}"
    if prefix == "SG":  return f"SGX:{ticker}"
    if prefix in ("US", ""):
        return f"NYSE:{ticker}" if ticker in _NYSE_SET else f"NASDAQ:{ticker}"
    return f"NASDAQ:{ticker}"

def trading_days_between(entry_date_str: str, now: datetime) -> int:
    try:
        start = datetime.strptime(entry_date_str, "%Y-%m-%d")
        delta = (now - start).days
        return max(0, round(delta * 5 / 7))
    except Exception:
        return 0

def fmt_pct(v, decimals=1):
    if v is None: return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.{decimals}f}%"

def fmt_price(p):
    if p is None: return "N/A"
    return f"{p:,.3f}" if abs(p) < 1000 else f"{p:,.2f}"

def fmt_money(v):
    if v is None: return "N/A"
    sign = "+" if v >= 0 else ""
    if abs(v) >= 1_000_000: return f"{sign}{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:     return f"{sign}{v:,.2f}"
    return f"{sign}{v:.2f}"

# ─── Moomoo: fetch current positions ─────────────────────────────────────────

def fetch_positions() -> list[dict]:
    """Fetch open positions from Moomoo OpenD and return list of position dicts."""
    if not _MOOMOO_OK:
        print("  ⚠️ moomoo-api not available; using positions.json fallback")
        return _load_positions_json()

    print("  Connecting to Moomoo OpenD ...")
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.NONE, host=HOST, port=PORT,
        security_firm=FIRM
    )
    try:
        ret, df = ctx.position_list_query(trd_env=TRD_ENV, acc_id=ACCOUNT_ID)
        if ret != RET_OK:
            print(f"  ⚠️ OpenD error: {df}  →  falling back to positions.json")
            return _load_positions_json()
        positions = []
        for _, row in df.iterrows():
            code        = str(row.get("code", ""))
            qty         = float(row.get("qty", 0))
            avg_cost    = float(row.get("average_cost", 0))
            market_val  = float(row.get("market_val", 0))
            curr_price  = market_val / qty if qty > 0 else None
            pl_ratio    = float(row.get("pl_ratio_avg_cost", 0) or 0) * 100
            nom_price   = float(row.get("nominal_price", curr_price or avg_cost) or avg_cost)
            nom_price   = nom_price if nom_price > 0 else curr_price

            tv_sym = moomoo_to_tv(code)
            positions.append({
                "symbol":     tv_sym,
                "entry_price": round(avg_cost, 4),
                "entry_date": None,  # OpenD doesn't expose first buy date; time-stop skipped
                "shares":     int(qty),
                "current_price": round(nom_price, 4) if nom_price else None,
                "pl_ratio":   round(pl_ratio, 2),
                "market_val": round(market_val, 2),
            })
        print(f"  Fetched {len(positions)} live positions from OpenD")
        return positions
    finally:
        ctx.close()

def _load_positions_json() -> list[dict]:
    """Fallback: load from positions.json in same folder."""
    pf = Path(__file__).parent.parent / "positions.json"
    if pf.exists():
        raw = json.loads(pf.read_text())
        return raw
    return []

# ─── TV Screener ─────────────────────────────────────────────────────────────

def fetch_tv_screener(symbols: list) -> dict:
    """Batch fetch rel_vol + recommendation_mark from TV Screener."""
    by_url: dict = {}
    sym_to_ps: dict = {}
    for ps in symbols:
        if ":" not in ps: continue
        exch, ticker = ps.split(":", 1)
        url = _TV_SCANNER.get(exch.upper())
        if not url: continue
        by_url.setdefault(url, []).append(ticker)
        sym_to_ps[ticker] = ps

    results: dict = {}
    fields = ["name", "relative_volume_10d_calc", "recommendation_mark",
              "change", "volume", "average_volume_10d_calc"]
    for url, tickers in by_url.items():
        try:
            payload = {
                "columns": fields,
                "filter": [{"left": "name", "operation": "in_range", "right": tickers}],
                "range": [0, len(tickers) + 5],
            }
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            for item in data.get("data", []):
                s    = item.get("s", "")
                d    = item.get("d", [])
                tok  = s.split(":")[-1] if ":" in s else s
                ps_  = sym_to_ps.get(tok) or s
                results[ps_] = {
                    "rel_vol":  d[1] if len(d) > 1 else None,
                    "rec_mark": d[2] if len(d) > 2 else None,
                    "chg_pct":  d[3] if len(d) > 3 else None,  # % change today
                }
        except Exception:
            pass
    return results

# ─── VIX ─────────────────────────────────────────────────────────────────────

def fetch_vix() -> float:
    try:
        hist = yf.Ticker("^VIX").history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return 20.0

# ─── Technical indicators (from yfinance OHLC) ───────────────────────────────

def compute_indicators(hist: pd.DataFrame) -> dict:
    """Compute ATR14, RSI14, MACD, EMA20, EMA50 from OHLC history."""
    if len(hist) < 30:
        return {}
    high  = hist["High"].values.astype(float)
    low   = hist["Low"].values.astype(float)
    close = hist["Close"].values.astype(float)

    # ATR 14
    tr = np.maximum(high[1:] - low[1:],
         np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])))
    atr = float(np.mean(tr[-14:]))
    if len(tr) > 14:
        atr_val = float(np.mean(tr[:14]))
        for v in tr[14:]:
            atr_val = (atr_val * 13 + v) / 14
        atr = atr_val

    # RSI 14
    deltas = np.diff(close)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = float(np.mean(gains[:14]))
    avg_l  = float(np.mean(losses[:14]))
    for i in range(14, len(deltas)):
        avg_g = (avg_g * 13 + gains[i]) / 14
        avg_l = (avg_l * 13 + losses[i]) / 14
    rsi = 100.0 if avg_l == 0 else round(100 - (100 / (1 + avg_g / avg_l)), 2)

    # RSI direction (compare last 3 bars)
    rsi_arr = []
    for i in range(max(0, len(close) - 20), len(close)):
        sl = close[max(0, i-14):i+1]
        if len(sl) < 5: continue
        d_ = np.diff(sl)
        g_ = np.where(d_ > 0, d_, 0.0)
        l_ = np.where(d_ < 0, -d_, 0.0)
        ag = float(np.mean(g_)); al = float(np.mean(l_))
        rsi_arr.append(100.0 if al == 0 else 100 - (100 / (1 + ag / max(al, 1e-9))))
    rsi_falling = (len(rsi_arr) >= 3 and rsi_arr[-1] < rsi_arr[-3]) if rsi_arr else False

    # MACD (12,26,9)
    def ema(arr, p):
        m = float(np.mean(arr[:p]))
        k = 2 / (p + 1)
        for v in arr[p:]:
            m = m * (1 - k) + v * k
        return m
    macd_line = ema(close, 12) - ema(close, 26)
    # Signal (9-period EMA of MACD) — approximate using last 20 bars
    macd_hist  = []
    for i in range(26, len(close)):
        sl = close[max(0,i-26):i+1]
        if len(sl) < 26: continue
        ml = ema(sl, 12) - ema(sl, 26)
        macd_hist.append(ml)
    signal_line = float(np.mean(macd_hist[-9:])) if len(macd_hist) >= 9 else macd_line
    macd_bearish = macd_line < signal_line

    # EMA 20 and EMA 50
    ema20 = ema(close, 20)
    ema50 = ema(close, 50) if len(close) >= 50 else ema20
    ema_breakdown = close[-1] < ema20 and ema20 < ema50   # price below both MAs

    # High water mark (max close over look-back period, up to 90 bars)
    hwm = float(np.max(close[-90:]))

    # Trailing stop: hwm - 1.5×ATR
    trail_stop = hwm - 1.5 * atr

    return {
        "current_price": round(float(close[-1]), 4),
        "atr14":         round(atr, 4),
        "rsi":           rsi,
        "rsi_falling":   rsi_falling,
        "macd_line":     round(macd_line, 4),
        "signal_line":   round(signal_line, 4),
        "macd_bearish":  macd_bearish,
        "ema20":         round(ema20, 4),
        "ema50":         round(ema50, 4),
        "ema_breakdown": ema_breakdown,
        "hwm":           round(hwm, 4),
        "trail_stop":    round(trail_stop, 4),
    }

# ─── News + Sentiment ────────────────────────────────────────────────────────

def fetch_yf_news(t_obj) -> list[str]:
    headlines = []
    try:
        for item in (t_obj.news or []):
            content = item.get("content", item)
            title   = content.get("title", "")
            summary = content.get("summary", "")
            if title: headlines.append(title)
            if summary: headlines.append(summary[:200])
    except Exception:
        pass
    return headlines[:16]

def fetch_google_news(tv_sym: str, company: str = "") -> list[str]:
    query = company if company else tv_sym.split(":")[-1]
    q = urllib.parse.quote(f"{query} stock")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            txt = r.read().decode("utf-8", errors="ignore")
        titles = re.findall(r"<title>(?!Google News)(.*?)</title>", txt)
        return [re.sub(r"<[^>]+>", "", t).strip() for t in titles if t.strip()][:12]
    except Exception:
        return []

def news_sentiment(headlines: list[str]) -> dict:
    if not headlines:
        return {"score": 0.0, "label": "Neutral", "count": 0, "headlines": []}
    scores = [vader_score(h) for h in headlines]
    mean   = sum(scores) / len(scores)
    label  = ("Very Bearish" if mean < -0.35 else
              "Bearish"      if mean < -0.10 else
              "Slightly Bearish" if mean < -0.02 else
              "Neutral"      if mean < 0.02 else
              "Slightly Bullish" if mean < 0.10 else
              "Bullish"      if mean < 0.35 else "Very Bullish")
    return {"score": round(mean, 4), "label": label,
            "count": len(headlines), "headlines": headlines[:5]}

# ─── Fundamentals ────────────────────────────────────────────────────────────

def fetch_fundamentals(ticker: str) -> dict:
    # Silence yfinance / urllib3 404 errors — these are expected for non-US tickers.
    # yfinance writes HTTP 404 directly to stderr (not via logging), so we also
    # redirect stderr to suppress the noise.
    import io, contextlib
    _yf_log  = logging.getLogger("yfinance")
    _ul_log  = logging.getLogger("urllib3")
    _prev_yf = _yf_log.level
    _prev_ul = _ul_log.level
    _yf_log.setLevel(logging.CRITICAL)
    _ul_log.setLevel(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            t   = yf.Ticker(ticker)
            inf = t.info or {}
    except Exception as e:
        _yf_log.setLevel(_prev_yf)
        _ul_log.setLevel(_prev_ul)
        return {"error": str(e)}
    finally:
        _yf_log.setLevel(_prev_yf)
        _ul_log.setLevel(_prev_ul)

    def g(k, d=None):
        v = inf.get(k)
        return v if v not in (None, "N/A") else d

    def pct(k):
        v = g(k)
        return round(v * 100, 2) if v is not None else None

    return {
        "company":        g("longName", g("shortName", ticker)),
        "sector":         g("sector", "N/A"),
        "market_cap":     g("marketCap"),
        "currency":       g("currency", ""),
        "forward_pe":     g("forwardPE"),
        "peg_ratio":      g("pegRatio"),
        "earnings_growth":pct("earningsGrowth"),
        "revenue_growth": pct("revenueGrowth"),
        "roe":            pct("returnOnEquity"),
        "profit_margin":  pct("profitMargins"),
        "debt_to_equity": g("debtToEquity"),
        "current_ratio":  g("currentRatio"),
        "free_cashflow":  g("freeCashflow"),
        "analyst_target": g("targetMeanPrice"),
        "current_price":  g("currentPrice", g("regularMarketPrice")),
        "52w_high":       g("fiftyTwoWeekHigh"),
        "52w_low":        g("fiftyTwoWeekLow"),
        "beta":           g("beta"),
        "dividend_yield": pct("dividendYield"),
    }

def fetch_sentiment_data(t_obj, company: str, tv_sym: str) -> dict:
    import io, contextlib
    result = {}
    # Analyst consensus — yfinance writes HTTP 404 directly to stderr for
    # non-US tickers / ETFs, so redirect stderr to suppress the noise.
    _stderr_trap = io.StringIO()
    _stdout_trap = io.StringIO()
    try:
        with contextlib.redirect_stdout(_stdout_trap), contextlib.redirect_stderr(_stderr_trap):
            rec = t_obj.recommendations
        if rec is not None and not rec.empty:
            row = rec.iloc[-1]
            sb  = int(row.get("strongBuy",  0))
            b_  = int(row.get("buy",        0))
            h   = int(row.get("hold",       0))
            s   = int(row.get("sell",       0))
            ss  = int(row.get("strongSell", 0))
            tot = sb + b_ + h + s + ss
            bear_score = (s * 1 + ss * 2) / max(tot, 1)
            bull_score = (sb * 2 + b_ * 1) / max(tot, 1)
            result["analyst"] = {
                "strongBuy": sb, "buy": b_, "hold": h, "sell": s, "strongSell": ss,
                "total": tot,
                "bull_ratio": round(bull_score, 3),
                "bear_ratio": round(bear_score, 3),
                "consensus": ("Strong Buy" if bull_score > 1.5 else
                              "Buy"        if bull_score > 0.9 else
                              "Hold"       if bear_score < 0.5 else
                              "Sell"       if bear_score < 1.0 else "Strong Sell"),
            }
        else:
            result["analyst"] = {"bull_ratio": 0.5, "bear_ratio": 0.2,
                                  "consensus": "N/A", "total": 0}
    except Exception:
        result["analyst"] = {"bull_ratio": 0.5, "bear_ratio": 0.2,
                              "consensus": "N/A", "total": 0}

    # Downgrades (last 30 days)
    result["recent_downgrade"] = False
    result["downgrade_detail"] = []
    try:
        with contextlib.redirect_stdout(_stdout_trap), contextlib.redirect_stderr(_stderr_trap):
            ud = t_obj.upgrades_downgrades
        if ud is not None and not ud.empty:
            ud = ud.copy()
            ud.index = ud.index.tz_localize(None) if ud.index.tzinfo else ud.index
            cutoff = datetime.now() - timedelta(days=30)
            recent = ud[ud.index > cutoff]
            for _, row in recent.iterrows():
                action = str(row.get("Action","")).lower()
                to_g   = str(row.get("ToGrade",""))
                if action == "down" or any(g in to_g for g in ["Sell","Underperform","Underweight"]):
                    result["recent_downgrade"] = True
                    result["downgrade_detail"].append(
                        f"{row.name.strftime('%m/%d')} {row.get('Firm','')} → {to_g}")
    except Exception:
        pass

    # Earnings beat rate
    result["earnings_beats"] = {"beat_rate": 0.5, "beats": 0, "quarters": 0, "detail": []}
    try:
        with contextlib.redirect_stdout(_stdout_trap), contextlib.redirect_stderr(_stderr_trap):
            eh = t_obj.earnings_history
        if eh is not None and not eh.empty:
            beats  = (eh["surprisePercent"] > 0).sum()
            total  = len(eh)
            detail = []
            for q, row in eh.tail(4).iterrows():
                sp = row.get("surprisePercent", 0)
                detail.append(f"{q}: {'✓' if sp > 0 else '✗'} {sp*100:+.1f}%")
            result["earnings_beats"] = {
                "beat_rate": round(beats / max(total, 1), 3),
                "beats": int(beats), "quarters": int(total),
                "detail": detail,
            }
    except Exception:
        pass

    # Earnings calendar
    result["earnings_days_away"] = None
    result["earnings_risk"] = "Unknown"
    result["earnings_date_str"] = None
    try:
        with contextlib.redirect_stdout(_stdout_trap), contextlib.redirect_stderr(_stderr_trap):
            cal = t_obj.calendar
        if cal and "Earnings Date" in cal:
            ed = cal["Earnings Date"]
            if isinstance(ed, list): ed = ed[0]
            if hasattr(ed, "strftime"):
                days = (ed - datetime.now().date()).days
                result["earnings_days_away"] = days
                result["earnings_date_str"]  = ed.strftime("%Y-%m-%d")
                result["earnings_risk"] = (
                    "🔴 Imminent (<7D)"  if days < 7  else
                    "🟡 Soon (<14D)"     if days < 14 else
                    "🟠 Coming (<28D)"   if days < 28 else
                    "🟢 Distant (>28D)")
    except Exception:
        pass

    # News sentiment
    yf_h  = fetch_yf_news(t_obj)
    gg_h  = fetch_google_news(tv_sym, company)
    all_h = list(dict.fromkeys(yf_h + gg_h))
    result["news"] = news_sentiment(all_h)
    return result

# ─── LAYER 1: Sell Technical Score ───────────────────────────────────────────

def score_sell_technical(pos: dict, ind: dict, vix: float, screener: dict) -> dict:
    """
    Returns a 0–10 urgency score. Higher = stronger sell signal.
    Components:
      hard_stop_hit   (0 or 10 each)
      trail_stop_hit  (0 or 10)
      macd_bearish    (0 or 6)
      rsi_decay       (0–8)
      ema_breakdown   (0 or 5)
      deep_loss       (0–8)  profit very negative → stop loss urgency
      big_profit_risk (0–4)  large open profit → consider taking
      time_stop       (0 or 5)
    """
    if not ind:
        return {"total": 5.0, "triggers": ["No indicator data"], "details": {}}

    entry = float(pos.get("entry_price", 0))
    curr  = ind.get("current_price") or float(pos.get("current_price") or entry)
    atr   = ind.get("atr14", 0)
    trail = ind.get("trail_stop", curr)
    hwm   = ind.get("hwm", curr)
    rsi   = ind.get("rsi", 50)
    shares= int(pos.get("shares", 1))
    td    = trading_days_between(pos.get("entry_date"), datetime.now())

    signals  = []
    detail   = {}
    urgency  = 0.0

    # Hard stop: entry − 2×ATR
    if atr > 0:
        hard_stop = entry - 2.0 * atr
        detail["hard_stop"] = round(hard_stop, 3)
        if curr < hard_stop:
            urgency += 10.0
            signals.append(f"🛑 HARD STOP hit (curr={curr:.3f} < stop={hard_stop:.3f})")

    # Trailing stop: HWM − 1.5×ATR
    detail["trail_stop"] = round(trail, 3)
    detail["hwm"]        = round(hwm, 3)
    if curr < trail and curr < hwm * 0.97:
        urgency += 8.0
        signals.append(f"📉 TRAILING STOP hit (curr={curr:.3f} < trail={trail:.3f})")

    # MACD bearish cross
    detail["macd_bearish"] = ind.get("macd_bearish", False)
    if ind.get("macd_bearish"):
        urgency += 5.0
        signals.append(f"⚡ MACD below signal (bearish momentum)")

    # RSI decay
    detail["rsi"] = rsi
    detail["rsi_falling"] = ind.get("rsi_falling", False)
    if rsi < 40:
        urgency += 7.0
        signals.append(f"📛 RSI severely weak: {rsi:.1f}")
    elif rsi < 50 and ind.get("rsi_falling"):
        urgency += 5.0
        signals.append(f"📛 RSI decaying: {rsi:.1f} and falling")
    elif rsi < 50:
        urgency += 3.0
        signals.append(f"📛 RSI below 50: {rsi:.1f}")

    # EMA breakdown (price below both EMA20 and EMA50)
    detail["ema_breakdown"] = ind.get("ema_breakdown", False)
    if ind.get("ema_breakdown"):
        urgency += 5.0
        signals.append(f"📊 MA Breakdown: price below EMA20 & EMA50")

    # P&L context
    if entry > 0:
        pnl_pct = (curr - entry) / entry * 100
        detail["pnl_pct"] = round(pnl_pct, 2)
        if pnl_pct < -15:
            urgency += 7.0
            signals.append(f"🩸 Deep loss: {pnl_pct:.1f}%")
        elif pnl_pct < -8:
            urgency += 4.0
            signals.append(f"🩸 Significant loss: {pnl_pct:.1f}%")
        elif pnl_pct > 40:
            urgency += 3.0
            signals.append(f"💰 Large profit ({pnl_pct:.1f}%) — consider locking in gains")
        elif pnl_pct > 20:
            urgency += 1.5
            signals.append(f"💰 Good profit ({pnl_pct:.1f}%) — monitor trail stop")

    # Time stop (held long with little gain)
    if entry > 0 and td and td > 20:
        if pnl_pct < 3.0:
            urgency += 4.0
            signals.append(f"⏰ Time stop: {td}d held, only {pnl_pct:.1f}% gain (dead money)")

    # VIX regime modifier (amplify if high volatility)
    detail["vix"] = vix
    if vix > 35:
        urgency = min(10.0, urgency * 1.2)
        signals.append(f"🌪️ VIX EXTREME ({vix:.1f}) — tighten all stops")
    elif vix > 25:
        urgency = min(10.0, urgency * 1.1)

    # Cap at 10
    # Rescale: max realistic raw urgency ~25; scale to 0-10
    total = round(min(10.0, urgency / 2.5), 2)

    return {"total": total, "triggers": signals, "details": detail}

# ─── LAYER 2: Fundamental Risk ────────────────────────────────────────────────

def score_fundamental_risk(fund: dict, ind: dict) -> dict:
    """
    Score fundamental deterioration risk (higher = more reason to sell).
    Note: flipped from buy scoring — high PE+poor growth = sell risk.
    Returns neutral (3.0) for ETFs/symbols with no fundamentals data.
    """
    if "error" in fund:
        return {"total": 3.0, "signals": ["No fundamental data (ETF or unavailable)"], "details": {}}

    # Detect ETF / no-data case: most fields will be None
    non_null = sum(1 for k in ["forward_pe","earnings_growth","revenue_growth","roe","analyst_target"]
                   if fund.get(k) is not None)
    if non_null == 0:
        return {"total": 3.0, "signals": ["No fundamental data (ETF or unavailable)"], "details": {}}

    curr  = (ind or {}).get("current_price") or fund.get("current_price", 0) or 0
    # Skip analyst target comparison for leveraged ETFs: curr is ETF price, tgt is underlying price
    tgt   = None if fund.get("_underlying_for") else fund.get("analyst_target")
    pe    = fund.get("forward_pe")
    peg   = fund.get("peg_ratio")
    eg    = fund.get("earnings_growth")  # % already
    rg    = fund.get("revenue_growth")
    roe   = fund.get("roe")
    pm    = fund.get("profit_margin")
    de    = fund.get("debt_to_equity")
    cr    = fund.get("current_ratio")
    fcf   = fund.get("free_cashflow")
    beta  = fund.get("beta")

    signals = []
    risk    = 0.0

    # Analyst target below current (bearish)
    if tgt and curr > 0:
        downside = (curr - tgt) / curr * 100
        if downside > 15:
            risk += 8.0
            signals.append(f"🎯 {downside:.1f}% above analyst target (target={tgt:.2f})")
        elif downside > 5:
            risk += 5.0
            signals.append(f"🎯 Above analyst target by {downside:.1f}%")
        elif downside < -20:
            # Target well above current = bullish, reduce risk
            risk -= 2.0

    # Overvalued + deteriorating fundamentals
    if pe and pe > 40:
        risk += 4.0
        signals.append(f"💹 High P/E: {pe:.1f}")
    if peg and peg > 4:
        risk += 4.0
        signals.append(f"💹 High PEG: {peg:.2f}")

    # Negative earnings/revenue growth
    if eg is not None and eg < -10:
        risk += 6.0
        signals.append(f"📉 Earnings growth deeply negative: {eg:.1f}%")
    elif eg is not None and eg < 0:
        risk += 3.0
        signals.append(f"📉 Negative earnings growth: {eg:.1f}%")

    if rg is not None and rg < -5:
        risk += 4.0
        signals.append(f"📉 Revenue shrinking: {rg:.1f}%")

    # High debt (risk) + poor liquidity
    if de and de > 200:
        risk += 4.0
        signals.append(f"🏦 Very high D/E: {de:.0f}")
    elif de and de > 100:
        risk += 2.0

    if cr and cr < 1.0:
        risk += 3.0
        signals.append(f"⚠️ Current ratio < 1 ({cr:.2f})")

    if fcf is not None and fcf < 0:
        risk += 3.0
        signals.append(f"💸 Negative free cashflow")

    # High beta = volatile = tighten stops
    if beta and beta > 2.5:
        risk += 2.0
        signals.append(f"🎢 High beta: {beta:.1f}")

    total = round(min(10.0, risk / 3.5), 2)
    return {
        "total": total,
        "signals": signals,
        "details": {
            "forward_pe": pe, "peg_ratio": peg,
            "earnings_growth": eg, "revenue_growth": rg,
            "analyst_target": tgt, "current_price": curr,
            "de": de, "cr": cr, "fcf": fcf, "beta": beta,
            "roe": roe, "profit_margin": pm,
        },
    }

# ─── LAYER 3: Sentiment Risk ─────────────────────────────────────────────────

def score_sentiment_risk(sent: dict) -> dict:
    """Higher score = more bearish sentiment = stronger sell signal."""
    signals = []
    risk    = 0.0

    # News negativity
    news_score = sent.get("news", {}).get("score", 0.0)
    if news_score < -0.35:
        risk += 7.0
        signals.append(f"📰 Very bearish news ({news_score:.2f})")
    elif news_score < -0.10:
        risk += 4.0
        signals.append(f"📰 Bearish news ({news_score:.2f})")
    elif news_score < 0.0:
        risk += 2.0
    elif news_score > 0.35:
        risk -= 3.0   # very bullish news reduces sell urgency

    # Analyst downgrade
    if sent.get("recent_downgrade"):
        risk += 5.0
        for d in sent.get("downgrade_detail", [])[:2]:
            signals.append(f"⬇️ Downgrade: {d}")

    # Bear analyst ratio
    bear_r = sent.get("analyst", {}).get("bear_ratio", 0)
    if bear_r > 1.0:
        risk += 5.0
        signals.append(f"🐻 Bear analyst ratio: {bear_r:.2f}")
    elif bear_r > 0.5:
        risk += 2.5

    # Low earnings beat rate
    beat_r = sent.get("earnings_beats", {}).get("beat_rate", 0.5)
    if beat_r < 0.25:
        risk += 5.0
        signals.append(f"📊 Very low earnings beat rate: {beat_r*100:.0f}%")
    elif beat_r < 0.4:
        risk += 2.5
        signals.append(f"📊 Below-avg earnings beat: {beat_r*100:.0f}%")

    # Earnings risk (sell before earnings uncertainty)
    days = sent.get("earnings_days_away")
    if days is not None and 0 <= days <= 7:
        risk += 6.0
        signals.append(f"📅 Earnings in {days}d — high event risk")
    elif days is not None and 0 <= days <= 14:
        risk += 3.0
        signals.append(f"📅 Earnings in {days}d — moderate risk")

    total = round(min(10.0, risk / 3.0), 2)
    return {"total": total, "signals": signals}

# ─── LAYER 4: Market Internals ────────────────────────────────────────────────

def score_market_internals(fund: dict, screener: dict, ind: dict, vix: float, yc: dict = None) -> dict:
    """Relative volume (sell pressure), 52W position, VIX, yield curve."""
    signals = []
    risk    = 0.0

    # Relative volume: high vol + down day = distribution
    rv = (screener or {}).get("rel_vol")
    chg= (screener or {}).get("chg_pct")
    if rv and chg is not None:
        if rv >= 2.0 and chg < 0:
            risk += 7.0
            signals.append(f"📊 High rel vol ({rv:.1f}x) with negative day ({chg:.1f}%) — distribution")
        elif rv >= 1.5 and chg < -2:
            risk += 5.0
            signals.append(f"📊 Elevated vol ({rv:.1f}x) + sell-off ({chg:.1f}%)")
    elif rv and rv < 0.5:
        risk += 1.0  # low liquidity = harder to exit

    # 52W position: near 52W high = consider profit-take
    high52 = fund.get("52w_high")
    low52  = fund.get("52w_low")
    curr   = (ind or {}).get("current_price") or fund.get("current_price", 0) or 0
    if high52 and low52 and curr > 0 and high52 > low52:
        pos52 = (curr - low52) / (high52 - low52) * 100
        if pos52 >= 90:
            risk += 5.0
            signals.append(f"🔝 Near 52W high ({pos52:.0f}% of range) — extended")
        elif pos52 >= 75:
            risk += 2.5
            signals.append(f"🔝 In upper 52W range ({pos52:.0f}%)")
        elif pos52 <= 15:
            risk += 3.0
            signals.append(f"🔻 Near 52W low ({pos52:.0f}%) — breakdown risk")

    # VIX regime
    if vix > 35:
        risk += 5.0
        signals.append(f"🌪️ VIX extreme: {vix:.1f}")
    elif vix > 25:
        risk += 2.5
        signals.append(f"⚠️ Elevated VIX: {vix:.1f}")

    # Yield curve (2s10s spread) — inversion = macro recession risk
    spread = (yc or {}).get("spread_bps")
    if spread is not None:
        if spread < -25:
            risk += 4.0
            signals.append(f"📉 Yield curve deeply inverted: {spread:+.0f}bps — recession risk")
        elif spread < 0:
            risk += 2.5
            signals.append(f"📉 Yield curve inverted: {spread:+.0f}bps (2Y > 10Y)")
        elif spread < 25:
            risk += 1.0
            signals.append(f"📊 Yield curve flat: {spread:+.0f}bps")

    total = round(min(10.0, risk / 2.5), 2)
    return {"total": total, "signals": signals, "yield_curve": yc or {}}

# ─── Composite scorer ────────────────────────────────────────────────────────

def composite_score(l1: dict, l2: dict, l3: dict, l4: dict) -> float:
    # Floor each layer at 0 — good fundamentals/sentiment should NOT reduce sell urgency
    # when technicals are screaming exit.
    l1v = max(0.0, l1["total"])
    l2v = max(0.0, l2["total"])
    l3v = max(0.0, l3["total"])
    l4v = max(0.0, l4["total"])
    comp = round(l1v * 0.40 + l2v * 0.20 + l3v * 0.25 + l4v * 0.15, 2)

    # L1 override: if hard stop OR trailing stop is triggered (L1 >= 8.0),
    # enforce a minimum composite of REDUCE (5.0)
    if l1v >= 8.0:
        comp = max(comp, 5.0)

    # Extra override: if L1 is maxed (10.0) AND deep loss in triggers → SELL NOW
    triggers = " ".join(l1.get("triggers", []))
    if l1v >= 9.5 and ("HARD STOP" in triggers or "TRAILING STOP" in triggers):
        comp = max(comp, 7.0)

    return round(comp, 2)

def recommendation(score: float) -> dict:
    if score >= 7.0:
        return {"label": "SELL NOW",  "color": "#ef4444", "badge_bg": "#450a0a", "icon": "🔴"}
    if score >= 5.0:
        return {"label": "REDUCE",    "color": "#f97316", "badge_bg": "#431407", "icon": "🟡"}
    if score >= 3.5:
        return {"label": "MONITOR",   "color": "#eab308", "badge_bg": "#422006", "icon": "🟠"}
    return     {"label": "HOLD",      "color": "#4ade80", "badge_bg": "#14532d", "icon": "🟢"}

# ─── HTML builder ─────────────────────────────────────────────────────────────

def _bar(score: float, color: str) -> str:
    pct = min(100, max(0, score * 10))
    return f'<div style="display:flex;align-items:center;gap:6px"><div style="flex:1;height:8px;background:#1e293b;border-radius:4px;overflow:hidden"><div style="width:{pct:.0f}%;height:100%;border-radius:4px;background:{color}"></div></div><span style="font-size:11px;color:#94a3b8;min-width:24px">{score:.1f}</span></div>'

def _signal_list(items: list) -> str:
    if not items:
        return '<div style="color:#64748b;font-size:11px">No signals</div>'
    return "".join(f'<div style="font-size:11px;color:#fca5a5;padding:2px 0">{s}</div>' for s in items[:4])

def build_card(pos: dict, result: dict, rank: int) -> str:
    sym    = pos["symbol"]
    entry  = float(pos.get("entry_price", 0))
    shares = int(pos.get("shares", 1))
    ind    = result["indicators"]
    curr   = ind.get("current_price") or pos.get("current_price") or entry
    pnl_v  = (curr - entry) * shares
    pnl_p  = (curr - entry) / entry * 100 if entry else 0
    pnl_c  = "#4ade80" if pnl_v >= 0 else "#f87171"
    fund   = result["fundamentals"]
    sent   = result["sentiment"]
    rec    = result["recommendation"]
    comp   = result["composite"]
    l1, l2, l3, l4 = result["l1"], result["l2"], result["l3"], result["l4"]

    # Moneyball: rank sell urgency vs recent buy-signal population (scaled ×10 for /100 display)
    _comp_100 = round(float(comp) * 10.0, 1)   # scale 0-10 → 0-100 for display + MB computation
    try:
        import sys as _sys, os as _os
        _tv_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _tv_dir not in _sys.path:
            _sys.path.insert(0, _tv_dir)
        from alert_tracker import compute_signal_moneyball as _csm_a
        _mb_a, _fin_a = _csm_a(_comp_100)   # _mb_a=percentile rank 0-100, _fin_a=blended 0-100
    except Exception:
        _mb_a, _fin_a = 50.0, _comp_100

    news_h = sent.get("news", {})
    news_score_c = "#f87171" if news_h.get("score", 0) < -0.1 else ("#4ade80" if news_h.get("score", 0) > 0.1 else "#94a3b8")

    upside_to_tgt = ""
    tgt = fund.get("analyst_target")
    if tgt and curr > 0:
        upside = (tgt - curr) / curr * 100
        upside_to_tgt = f'<div class="kv"><span>Analyst Target</span><b style="color:{"#4ade80" if upside > 0 else "#f87171"}">{tgt:.2f} ({fmt_pct(upside)})</b></div>'

    # 52W bar
    h52   = fund.get("52w_high")
    l52   = fund.get("52w_low")
    pos52_pct = 0
    pos52_lbl = "N/A"
    if h52 and l52 and curr > 0 and h52 > l52:
        pos52_pct = round((curr - l52) / (h52 - l52) * 100, 1)
        pos52_lbl = f"{pos52_pct:.0f}% of 52W range"

    signals_all = l1.get("triggers", []) + l2.get("signals", []) + l3.get("signals", []) + l4.get("signals", [])

    news_items = "".join(
        f'<div style="font-size:11px;color:#94a3b8;padding:4px 8px;background:#0a0f1e;border-radius:4px;margin-bottom:3px;border-left:3px solid {news_score_c}">{h}</div>'
        for h in news_h.get("headlines", [])[:4]
    )

    eb = sent.get("earnings_beats", {})
    analyst = sent.get("analyst", {})
    cons_c = "#4ade80" if analyst.get("bull_ratio", 0) > 1.0 else ("#f87171" if analyst.get("bear_ratio", 0) > 0.5 else "#94a3b8")

    return f"""
<div class="card" style="border-top:3px solid {rec['color']}">
  <div class="card-header">
    <div class="rank-badge" style="background:{rec['badge_bg']};border:1px solid {rec['color']};color:{rec['color']}">{rank}</div>
    <div class="card-title">
      <div class="ticker">{sym}</div>
      <div class="company">{fund.get('company','') or ''}</div>
      <div class="tags">
        <span style="background:{rec['badge_bg']};color:{rec['color']};border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700">{rec['icon']} {rec['label']}</span>
        <span style="background:#1a2e1a;color:#4ade80;border-radius:4px;padding:2px 7px;font-size:10px">{fund.get('sector','N/A')}</span>
        <span style="background:#1e293b;color:#94a3b8;border-radius:4px;padding:2px 7px;font-size:10px">{shares} shares @ {fmt_price(entry)}</span>
        {'<span style="background:#1e3a5f;color:#60a5fa;border-radius:4px;padding:2px 7px;font-size:10px">📌 Underlying: ' + fund['_underlying_for'] + '</span>' if fund.get('_underlying_for') else ''}
      </div>
    </div>
    <div style="text-align:right">
      <div style="display:inline-flex;align-items:flex-end;gap:10px">
        <div style="text-align:center">
          <div style="font-size:9px;color:#9ca3af;letter-spacing:.07em;text-transform:uppercase;margin-bottom:2px">CURRENT</div>
          <div style="font-size:38px;font-weight:800;line-height:1;color:{rec['color']}">{_comp_100:.0f}</div>
          <div style="font-size:10px;color:#64748b">Sell Urgency /100</div>
        </div>
        <div style="font-size:1.5rem;color:#6b7280;font-weight:300;padding-bottom:14px">+</div>
        <div style="text-align:center">
          <div style="font-size:9px;color:#9ca3af;letter-spacing:.07em;text-transform:uppercase;margin-bottom:2px">MONEYBALL</div>
          <div style="font-size:38px;font-weight:800;line-height:1;color:#60a5fa">{_mb_a:.0f}</div>
          <div style="font-size:10px;color:#64748b">MB Percentile /100</div>
        </div>
        <div style="font-size:1.5rem;color:#6b7280;font-weight:300;padding-bottom:14px">=</div>
        <div style="text-align:center">
          <div style="font-size:9px;color:#9ca3af;letter-spacing:.07em;text-transform:uppercase;margin-bottom:2px">Blended (60/40)</div>
          <div style="font-size:38px;font-weight:800;line-height:1;color:#22c55e">{_fin_a:.0f}</div>
          <div style="font-size:10px;color:#64748b">Final Score /100</div>
        </div>
      </div>
      <div style="font-size:14px;font-weight:700;color:{pnl_c};margin-top:4px">{fmt_pct(pnl_p)} ({fmt_money(pnl_v)} {fund.get('currency','')})</div>
      <div style="font-size:11px;color:#64748b">Unrealised P&L</div>
    </div>
  </div>

  <!-- Layer bar strip -->
  <div class="layers">
    <div class="layer-item"><div style="font-size:10px;color:#64748b;margin-bottom:4px;text-transform:uppercase">L1 Technical</div>{_bar(l1['total'],"#ef4444")}</div>
    <div class="layer-item"><div style="font-size:10px;color:#64748b;margin-bottom:4px;text-transform:uppercase">L2 Fundamental</div>{_bar(l2['total'],"#f97316")}</div>
    <div class="layer-item"><div style="font-size:10px;color:#64748b;margin-bottom:4px;text-transform:uppercase">L3 Sentiment</div>{_bar(l3['total'],"#eab308")}</div>
    <div class="layer-item"><div style="font-size:10px;color:#64748b;margin-bottom:4px;text-transform:uppercase">L4 Internals</div>{_bar(l4['total'],"#f59e0b")}</div>
  </div>

  <!-- Triggered sell signals -->
  <div style="padding:10px 20px;background:#0a0f1e;border-bottom:1px solid #1e293b">
    <div style="font-size:11px;font-weight:600;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">⚠️ Sell Triggers</div>
    {_signal_list(signals_all[:6]) if signals_all else '<div style="color:#64748b;font-size:11px">No sell signals — position looks healthy</div>'}
  </div>

  <!-- Detail grid -->
  <div class="detail-grid">
    <!-- COL1 Technical -->
    <div class="col">
      <div class="col-head">⚡ Technical (L1)</div>
      <div class="kv"><span>Current Price</span><b>{fmt_price(curr)}</b></div>
      <div class="kv"><span>Entry Price</span><b>{fmt_price(entry)}</b></div>
      <div class="kv"><span>ATR(14)</span><b>{fmt_price(ind.get('atr14'))}</b></div>
      <div class="kv"><span>Hard Stop</span><b style="color:#f87171">{fmt_price(l1['details'].get('hard_stop'))}</b></div>
      <div class="kv"><span>Trail Stop</span><b style="color:#f97316">{fmt_price(ind.get('trail_stop'))}</b></div>
      <div class="kv"><span>HWM</span><b>{fmt_price(ind.get('hwm'))}</b></div>
      <div class="kv"><span>RSI(14)</span><b style="color:{'#f87171' if (ind.get('rsi') or 50) < 45 else '#94a3b8'}">{ind.get('rsi','N/A')}</b></div>
      <div class="kv"><span>RSI Falling</span><b>{"Yes ⬇️" if ind.get('rsi_falling') else "No"}</b></div>
      <div class="kv"><span>MACD Bearish</span><b>{"Yes ⚡" if ind.get('macd_bearish') else "No"}</b></div>
      <div class="kv"><span>MA Breakdown</span><b>{"Yes 📊" if ind.get('ema_breakdown') else "No"}</b></div>
    </div>

    <!-- COL2 Fundamental -->
    <div class="col">
      <div class="col-head">📊 Fundamental Risk (L2){'&nbsp;<span style="font-size:9px;color:#60a5fa;font-weight:500;text-transform:none">(via ' + fund['_underlying_for'] + ')</span>' if fund.get('_underlying_for') else ''}</div>
      <div class="kv"><span>Forward P/E</span><b style="color:{'#f87171' if fund.get('forward_pe') and fund.get('forward_pe') > 40 else '#e2e8f0'}">{fund.get('forward_pe') or '–'}</b></div>
      <div class="kv"><span>PEG</span><b>{fund.get('peg_ratio') or '–'}</b></div>
      <div class="kv"><span>Earn Growth</span><b style="color:{'#f87171' if fund.get('earnings_growth') is not None and fund.get('earnings_growth') < 0 else '#4ade80'}">{fmt_pct(fund.get('earnings_growth'))}</b></div>
      <div class="kv"><span>Rev Growth</span><b style="color:{'#f87171' if fund.get('revenue_growth') is not None and fund.get('revenue_growth') < 0 else '#4ade80'}">{fmt_pct(fund.get('revenue_growth'))}</b></div>
      <div class="kv"><span>ROE</span><b>{fmt_pct(fund.get('roe'))}</b></div>
      <div class="kv"><span>Net Margin</span><b>{fmt_pct(fund.get('profit_margin'))}</b></div>
      <div class="kv"><span>D/E Ratio</span><b style="color:{'#f87171' if fund.get('debt_to_equity') and fund.get('debt_to_equity') > 200 else '#e2e8f0'}">{fund.get('debt_to_equity') or '–'}</b></div>
      {upside_to_tgt}
      <div class="kv"><span>Beta</span><b style="color:{'#f87171' if fund.get('beta') and fund.get('beta') > 2.5 else '#94a3b8'}">{fund.get('beta') or '–'}</b></div>
    </div>

    <!-- COL3 Sentiment -->
    <div class="col">
      <div class="col-head">🎯 Sentiment Risk (L3)</div>
      <div class="kv"><span>Analyst</span><b style="color:{cons_c}">{analyst.get('consensus','N/A')} ({analyst.get('total','?')} analysts)</b></div>
      <div class="kv"><span>SB/B/H/S/SS</span><b>{analyst.get('strongBuy',0)}/{analyst.get('buy',0)}/{analyst.get('hold',0)}/{analyst.get('sell',0)}/{analyst.get('strongSell',0)}</b></div>
      <div class="kv"><span>Beat Rate</span><b style="color:{'#f87171' if eb.get('beat_rate',0.5) < 0.4 else '#4ade80'}">{eb.get('beat_rate',0)*100:.0f}%</b></div>
      <div class="kv"><span>Earnings</span><b style="color:#eab308">{sent.get('earnings_risk','Unknown')}</b></div>
      {'<div class="kv"><span>Earn Date</span><b>' + str(sent.get('earnings_date_str','—')) + '</b></div>' if sent.get('earnings_date_str') else ''}
      <div class="kv"><span>News Tone</span><b style="color:{news_score_c}">{news_h.get('label','Neutral')} ({news_h.get('score',0):.3f})</b></div>
      <div class="kv"><span>Headlines</span><b>{news_h.get('count',0)} analysed</b></div>
      {''.join(f'<div style="font-size:10px;color:#f87171;padding:2px 0">⬇️ {d}</div>' for d in sent.get('downgrade_detail',[])[:2])}
    </div>

    <!-- COL4 Internals -->
    <div class="col">
      <div class="col-head">🔬 Market Internals (L4)</div>
      <div class="kv"><span>Rel Volume</span><b>{f"{result['screener'].get('rel_vol',0):.2f}x" if result['screener'].get('rel_vol') else '—'}</b></div>
      <div class="kv"><span>Today Change</span><b style="color:{'#f87171' if (result['screener'].get('chg_pct') or 0) < 0 else '#4ade80'}">{fmt_pct(result['screener'].get('chg_pct'))}</b></div>
      <div class="kv"><span>VIX</span><b style="color:{'#ef4444' if result['vix'] > 35 else ('#eab308' if result['vix'] > 25 else '#4ade80')}">{result['vix']:.1f}</b></div>
      <div class="kv"><span>52W High</span><b>{fmt_price(fund.get('52w_high'))}</b></div>
      <div class="kv"><span>52W Low</span><b>{fmt_price(fund.get('52w_low'))}</b></div>
      <div style="position:relative;height:8px;background:#1e293b;border-radius:4px;overflow:hidden;margin:8px 0 4px">
        <div style="height:100%;background:linear-gradient(90deg,#ef4444,#eab308,#22c55e);opacity:.5"></div>
        <div style="position:absolute;top:-2px;left:calc({pos52_pct}% - 5px);width:10px;height:12px;background:#fff;border-radius:2px"></div>
      </div>
      <div style="font-size:10px;color:#64748b">{pos52_lbl}</div>
      {''.join(f'<div class="kv"><span>{k.replace("_"," ").capitalize()}</span><b>{v}</b></div>' for k,v in (result.get('l1_details_extra') or {}).items())}
    </div>
  </div>

  <!-- News -->
  <div style="padding:10px 20px;border-top:1px solid #1e293b">
    <div style="font-size:11px;font-weight:600;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">📰 Recent News</div>
    {news_items or '<div style="color:#64748b;font-size:11px">No recent news</div>'}
  </div>
</div>"""

def build_html(ranked: list, vix: float, generated_at: str) -> str:
    sell_now  = [r for r in ranked if r["composite"] >= 7.0]
    reduce    = [r for r in ranked if 5.0 <= r["composite"] < 7.0]
    monitor   = [r for r in ranked if 3.5 <= r["composite"] < 5.0]
    hold      = [r for r in ranked if r["composite"] < 3.5]

    alert_bar = ""
    if sell_now:
        alert_bar = f'<div style="background:#7c3aed;color:#fff;padding:12px 28px;font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px"><span style="font-size:18px">🚨</span><span>SELL ACTIONABLE — {len(sell_now)} position(s) require IMMEDIATE action | VIX: {vix:.1f} | {generated_at}</span></div>'
    elif reduce:
        alert_bar = f'<div style="background:#b45309;color:#fff;padding:12px 28px;font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px"><span style="font-size:18px">⚠️</span><span>REDUCE WATCH — {len(reduce)} position(s) flagged for reduction | VIX: {vix:.1f} | {generated_at}</span></div>'
    else:
        alert_bar = f'<div style="background:#14532d;color:#4ade80;padding:12px 28px;font-weight:600;font-size:13px;display:flex;align-items:center;gap:12px"><span style="font-size:18px">✅</span><span>All positions HOLD status | VIX: {vix:.1f} | {generated_at}</span></div>'

    cards_html = ""
    for r in ranked:
        cards_html += build_card(r["position"], r, r["rank"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sell Actionable Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0f1e;color:#e2e8f0;font-size:13px;line-height:1.5}}
.header{{padding:24px 28px 8px;border-bottom:1px solid #1e293b}}
h1{{font-size:22px;color:#f8fafc;font-weight:700}}
.sub{{color:#64748b;font-size:12px;margin-top:4px}}
.legend{{display:flex;gap:12px;flex-wrap:wrap;padding:12px 28px;background:#0f172a;border-bottom:1px solid #1e293b;font-size:11px;color:#64748b}}
.legend b{{color:#94a3b8}}
.cards{{padding:20px 24px;display:flex;flex-direction:column;gap:18px}}
.card{{background:#0f172a;border:1px solid #1e293b;border-radius:12px;overflow:hidden}}
.card:hover{{border-color:#334155}}
.card-header{{display:flex;align-items:flex-start;gap:16px;padding:16px 20px;background:#111827;border-bottom:1px solid #1e293b}}
.rank-badge{{width:48px;height:48px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:800;flex-shrink:0}}
.card-title{{flex:1}}
.ticker{{font-size:18px;font-weight:700;color:#f87171}}
.company{{color:#94a3b8;font-size:12px;margin-top:2px}}
.tags{{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}}
.layers{{display:flex;gap:12px;padding:12px 20px;background:#0a0f1e;border-bottom:1px solid #1e293b}}
.layer-item{{flex:1;min-width:100px}}
.detail-grid{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:0;padding:0 20px}}
@media(max-width:1100px){{.detail-grid{{grid-template-columns:1fr 1fr}}}}
.col{{padding:14px 10px;border-right:1px solid #1e293b}}
.col:last-child{{border-right:none}}
.col-head{{font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #1e293b}}
.kv{{display:flex;justify-content:space-between;padding:3px 0;font-size:12px}}
.kv span{{color:#64748b}}
.kv b{{color:#e2e8f0;font-weight:600}}
.footer{{padding:16px 28px;color:#374151;font-size:11px;border-top:1px solid #1e293b}}
</style>
</head>
<body>
{alert_bar}
<div class="header">
  <h1>🔔 Sell Actionable Report — Open Positions Ranked by Exit Urgency</h1>
  <div class="sub">4-Layer Sell Scoring: Technical (40%) + Fundamental Risk (20%) + Sentiment Risk (25%) + Market Internals (15%) | Generated: {generated_at}</div>
</div>
<div class="legend">
  <b>Urgency guide:</b>
  <span>🔴 ≥7.0 SELL NOW</span><span>🟡 ≥5.0 REDUCE</span><span>🟠 ≥3.5 MONITOR</span><span>🟢 &lt;3.5 HOLD</span>
  <b>|</b>
  <span>L1 Technical: hard stop / trail stop / MACD / RSI / MA / P&L / time</span>
  <span>L2 Fundamental: PE/PEG decay / earnings growth / analyst target / debt / FCF</span>
  <span>L3 Sentiment: news VADER / analyst downgrades / beat rate / earnings calendar</span>
  <span>L4 Internals: rel vol + down day (distribution) / 52W range / VIX</span>
  <b>Sum:</b>
  <span>🔴 {len(sell_now)} SELL</span><span>🟡 {len(reduce)} REDUCE</span><span>🟠 {len(monitor)} MONITOR</span><span>🟢 {len(hold)} HOLD</span>
</div>
<div class="cards">
{cards_html}
</div>
<div class="footer">
  ⚠️ Research only — not financial advice. Data: Moomoo OpenD + yfinance + Google News RSS + VADER. Past performance ≠ future results. Always DYOR.
</div>
</body>
</html>"""

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Sell Actionable Report — positions ranked by exit urgency")
    ap.add_argument("--out", default=str(OUTPUT_HTML), help="Output HTML path")
    args = ap.parse_args()

    now     = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*60}")
    print(f"  Sell Actionable Report")
    print(f"{'='*60}\n")

    print("[1/5] Loading positions ...")
    positions = fetch_positions()
    if not positions:
        print("  No positions found.")
        return
    symbols = [p["symbol"] for p in positions]
    print(f"  {len(positions)} positions: {', '.join(symbols)}")

    print("[2/5] Fetching VIX, yield curve and TV Screener data ...")
    from tv_screener_fundamentals import (
        fetch_tv_fundamentals as _tv_fund_fetch,
        tv_fund_to_compat     as _tv_fund_compat,
        fetch_yield_curve,
    )
    vix        = fetch_vix()
    yc         = fetch_yield_curve()
    screener   = fetch_tv_screener(symbols)
    _spread_str = (f"{yc['spread_bps']:+.0f}bps (INVERTED)" if (yc['spread_bps'] is not None and yc['spread_bps'] < 0)
                   else f"{yc['spread_bps']:+.0f}bps" if yc['spread_bps'] is not None else "N/A")
    print(f"  VIX: {vix:.1f} | 2s10s: {_spread_str} | TV Screener: {len(screener)} symbols")

    # Batch-fetch fundamentals from TV Screener (replaces sequential yfinance .info calls)
    # Includes underlying symbols for single-stock leveraged ETFs
    symbols_for_fund = list(symbols)
    for _pos in positions:
        _und = LEVERAGED_ETF_UNDERLYING.get(_pos["symbol"].split(":", 1)[-1])
        if _und:
            _tv_und = UNDERLYING_TO_TV.get(_und)
            if _tv_und and _tv_und not in symbols_for_fund:
                symbols_for_fund.append(_tv_und)
    tv_fund_cache = _tv_fund_fetch(symbols_for_fund, use_auth=True)
    print(f"  TV Fundamentals: {len(tv_fund_cache)}/{len(symbols_for_fund)} symbols")

    print("[3/5] Fetching technical indicators, fundamentals, sentiment ...")
    results = []
    for i, pos in enumerate(positions, 1):
        sym    = pos["symbol"]
        yf_sym = tv_to_yf(sym)
        # For single-stock leveraged ETFs, fetch fundamentals from the underlying equity
        raw_ticker = sym.split(":", 1)[-1]   # strip exchange prefix
        underlying = LEVERAGED_ETF_UNDERLYING.get(raw_ticker)
        fund_ticker = underlying if underlying else yf_sym
        print(f"  [{i}/{len(positions)}] {sym} ({yf_sym})"
              + (f"  [underlying → {underlying}]" if underlying else ""))

        # yfinance — history/indicators from the ETF itself, fundamentals from underlying
        t_obj = yf.Ticker(yf_sym)
        try:
            hist = t_obj.history(period="6mo")
        except Exception:
            hist = pd.DataFrame()
        ind  = compute_indicators(hist) if not hist.empty else {}

        # TV Screener replacement for yfinance fundamentals (batch, no ASX/ETF 404)
        if underlying:
            _tv_und_sym = UNDERLYING_TO_TV.get(underlying, "")
            _tv_raw = tv_fund_cache.get(_tv_und_sym, {})
        else:
            _tv_raw = tv_fund_cache.get(sym, {})
        fund = _tv_fund_compat(_tv_raw, current_price=ind.get("current_price")) if _tv_raw else {}
        # keep _underlying_for tag for HTML display
        if underlying:
            fund["_underlying_for"] = underlying
        # Sentiment also from the underlying (news, analyst coverage are for the stock, not the ETF)
        sent_t_obj = yf.Ticker(fund_ticker) if underlying else t_obj
        sent = fetch_sentiment_data(sent_t_obj, fund.get("company",""), sym)
        sc   = screener.get(sym, {})

        # Score all 4 layers
        l1 = score_sell_technical(pos, ind, vix, sc)
        l2 = score_fundamental_risk(fund, ind)
        l3 = score_sentiment_risk(sent)
        l4 = score_market_internals(fund, sc, ind, vix, yc)
        comp = composite_score(l1, l2, l3, l4)
        rec  = recommendation(comp)

        entry  = float(pos.get("entry_price", 0))
        curr   = ind.get("current_price") or pos.get("current_price") or entry
        pnl_p  = (curr - entry) / entry * 100 if entry else 0
        print(f"       Urgency={comp}/10 → {rec['label']} | P&L {fmt_pct(pnl_p)} | L1={l1['total']} L2={l2['total']} L3={l3['total']} L4={l4['total']}")

        results.append({
            "position":       pos,
            "indicators":     ind,
            "fundamentals":   fund,
            "sentiment":      {**sent},
            "screener":       sc,
            "vix":            vix,
            "l1":             l1, "l2": l2, "l3": l3, "l4": l4,
            "composite":      comp,
            "recommendation": rec,
        })

    print("[4/5] Ranking and building report ...")
    ranked = sorted(results, key=lambda r: r["composite"], reverse=True)
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    print("[5/5] Writing HTML ...")
    html = build_html(ranked, vix, now_str)
    out  = Path(args.out)
    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out)
    except Exception:
        pass
    out.write_text(html, encoding="utf-8")
    print(f"\n  ✅ Written: {out} ({out.stat().st_size // 1024}KB)")

    # Summary
    print("\n  ─── Summary ───────────────────────────────────────")
    for r in ranked:
        sym  = r["position"]["symbol"]
        comp = r["composite"]
        rec  = r["recommendation"]["label"]
        print(f"  {r['rank']:>2}. {sym:<20}  {comp:.1f}/10  →  {rec}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
