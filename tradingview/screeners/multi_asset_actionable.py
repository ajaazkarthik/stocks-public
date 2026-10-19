"""
Multi-Asset Actionable Buy Report
==================================
Pipes multi_asset_screener results through adapted signal_stack scoring layers
and produces multi_asset_actionable.html — same card-based layout, per-asset
scoring logic that's meaningful for each class.

Scoring adaptations by asset class:
  Stocks  (screener-sourced) : L1 screener-technical + L2 fundamentals +
                                 L3 news/analyst + L4 PCR/insider/52W
  Crypto                      : L1 + L3 news-only + L4 52W-position
  Forex                       : L1 + L3 macro news + L4 52W-position
  Futures/Commodities         : L1 + L3 commodity news + L4 52W-position
  Bonds                       : no scoring — benchmark yield context panel only

Composite weights (all scale 0–100):
  Stocks  : L1×25% + L2×25% + L3×30% + L4×20%
  Crypto  : L1×45% + L3×35% + L4×20%
  Forex   : L1×50% + L3×25% + L4×25%
  Futures : L1×45% + L3×30% + L4×25%

Usage:
  python multi_asset_actionable.py                  # all classes, top 20 per class
  python multi_asset_actionable.py --max 15         # top 15 per class
  python multi_asset_actionable.py --class crypto forex  # subset
  python multi_asset_actionable.py --rsi-min 45 --adx-min 15
  python multi_asset_actionable.py --threshold 45   # only show composite ≥ 45
  python multi_asset_actionable.py --no-news        # skip news (faster)
"""
import json, re, argparse, sys, math, time, traceback
import urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Import shared functions from signal_stack ──────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    import signal_stack as _ss
    from signal_stack import (
        fetch_google_news, compute_news_sentiment, vader_compound,
        fetch_fundamentals, score_fundamental,
        fetch_sentiment_data, score_sentiment,
        fetch_market_internals, score_market_internals,
        fetch_market_regime, fetch_yf_news,
        grade, bar, hcap, fmt, colour_cell, _regime_banner,
    )
    from tv_screener_fundamentals import fetch_yield_curve
    import yfinance as yf
except ImportError as e:
    sys.exit(f"ERROR: {e}\nRun from the tradingview/ directory with all deps installed.")

# ── Import screener ────────────────────────────────────────────────────────────
from multi_asset_screener import SCAN_DEFS, run_all_scans

ROOT        = Path(__file__).parent.parent
OUTPUT_HTML = ROOT.parent / "reports" / "multi_asset_actionable.html"

# ── Asset class metadata ───────────────────────────────────────────────────────
ASSET_CLASS_META = {
    "stocks_us": {"label": "🇺🇸 US Stocks",    "type": "stock",   "emoji": "📈"},
    "stocks_au": {"label": "🇦🇺 AU Stocks",    "type": "stock",   "emoji": "📈"},
    "stocks_in": {"label": "🇮🇳 India Stocks", "type": "stock",   "emoji": "📈"},
    "stocks_hk": {"label": "🇭🇰 HK Stocks",   "type": "stock",   "emoji": "📈"},
    "stocks_sg": {"label": "🇸🇬 SG Stocks",   "type": "stock",   "emoji": "📈"},
    "stocks_jp": {"label": "🇯🇵 Japan Stocks", "type": "stock",   "emoji": "📈"},
    "stocks_kr": {"label": "🇰🇷 Korea Stocks", "type": "stock",   "emoji": "📈"},
    "crypto":    {"label": "₿ Crypto",        "type": "crypto",  "emoji": "🪙"},
    "forex":     {"label": "💱 Forex",         "type": "forex",   "emoji": "💱"},
    "futures":   {"label": "🛢 Futures",       "type": "futures", "emoji": "🛢"},
    "bonds":     {"label": "📊 Bonds",         "type": "bonds",   "emoji": "📊"},
}

# ── yfinance symbol mapping for non-stock assets ──────────────────────────────
_CRYPTO_YF_MAP = {
    "BTCUSDT": "BTC-USD",  "ETHUSDT": "ETH-USD",  "SOLUSDT": "SOL-USD",
    "BNBUSDT": "BNB-USD",  "XRPUSDT": "XRP-USD",  "ADAUSDT": "ADA-USD",
    "AVAXUSDT": "AVAX-USD","DOGEUSDT": "DOGE-USD", "DOTUSDT": "DOT-USD",
    "LINKUSDT": "LINK-USD","MATICUSDT": "MATIC-USD","LTCUSDT":"LTC-USD",
    "ATOMUSDT": "ATOM-USD","UNIUSDT": "UNI-USD",
}
_FUTURES_YF_MAP = {
    "CL1!": "CL=F",  "BRN1!": "BZ=F",  "GC1!": "GC=F",  "SI1!": "SI=F",
    "NG1!": "NG=F",  "NQ1!": "NQ=F",   "ES1!": "ES=F",  "ZW1!": "ZW=F",
    "ZC1!": "ZC=F",  "HG1!": "HG=F",   "RB1!": "RB=F",  "MCL1!": "CL=F",
}
_FOREX_YF_MAP = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X", "USDCHF": "CHF=X",
    "USDSGD": "SGD=X",    "AUDNZD": "AUDNZD=X", "AUDJPY": "AUDJPY=X",
    "XAUUSD": "GC=F",     "XAGUSD": "SI=F",
}

def _tv_to_yf_nonstock(tv_sym: str, asset_type: str) -> str | None:
    """Convert a non-stock TV symbol to yfinance equivalent for 52W range."""
    ticker = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
    if asset_type == "crypto":
        return _CRYPTO_YF_MAP.get(ticker)
    if asset_type == "futures":
        return _FUTURES_YF_MAP.get(ticker)
    if asset_type == "forex":
        return _FOREX_YF_MAP.get(ticker)
    return None

def _tv_to_yf_stock(tv_sym: str) -> str:
    """Convert stock TV symbol to yfinance."""
    if ":" not in tv_sym:
        return tv_sym
    exch, ticker = tv_sym.split(":", 1)
    ticker = ticker.replace("-", ".").strip()
    suffix_map = {
        "ASX": ".AX", "ASX_DLY": ".AX", "NSE": ".NS", "BSE": ".BO",
        "SGX": ".SI", "HKEX": ".HK", "TSE": ".T",
    }
    if exch.upper() == "HKEX" and ticker.isdigit():
        ticker = ticker.zfill(4)
    return ticker + suffix_map.get(exch.upper(), "")


# ── News query helpers ─────────────────────────────────────────────────────────
def _news_query(tv_sym: str, desc: str, asset_type: str) -> str:
    """Build a Google News search query appropriate for the asset class."""
    ticker = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
    if asset_type == "stock":
        # Use description (company name) for better results
        name = re.sub(r"\b(Corp|Inc|Ltd|plc|Group|Holdings|Co\.|Limited)\b", "", desc, flags=re.I).strip()
        return name if name else ticker
    if asset_type == "crypto":
        # Map to readable coin name
        coin_map = {
            "BTCUSDT": "Bitcoin", "ETHUSDT": "Ethereum", "SOLUSDT": "Solana",
            "BNBUSDT": "BNB Binance", "XRPUSDT": "XRP Ripple",
            "ADAUSDT": "Cardano ADA", "AVAXUSDT": "Avalanche AVAX",
            "DOGEUSDT": "Dogecoin", "DOTUSDT": "Polkadot",
            "LINKUSDT": "Chainlink LINK",
        }
        return coin_map.get(ticker, desc[:40] if desc else ticker)
    if asset_type == "forex":
        # Clean pair
        pair_map = {
            "EURUSD": "EUR/USD euro dollar",  "GBPUSD": "GBP/USD pound dollar",
            "USDJPY": "USD/JPY dollar yen",   "AUDUSD": "AUD/USD aussie dollar",
            "AUDNZD": "AUD/NZD",              "XAUUSD": "gold price XAU",
            "XAGUSD": "silver price XAG",     "USDCHF": "USD/CHF franc",
        }
        return pair_map.get(ticker, ticker)
    if asset_type == "futures":
        futures_map = {
            "CL1!": "WTI crude oil price", "BRN1!": "Brent crude oil price",
            "MCL1!": "WTI crude oil",      "GC1!": "gold futures price",
            "SI1!": "silver futures",      "NG1!": "natural gas price",
            "NQ1!": "Nasdaq 100 futures",  "ES1!": "S&P 500 futures",
            "ZW1!": "wheat futures",       "ZC1!": "corn futures",
            "HG1!": "copper futures",
        }
        return futures_map.get(ticker, desc[:40] if desc else ticker)
    return desc[:40] if desc else tv_sym


# ── Layer 1: Technical scoring from screener data ─────────────────────────────
def _norm10(v, good, bad):
    if v is None: return 5.0
    if good > bad:  # higher is better
        if v >= good: return 10.0
        if v <= bad:  return 0.0
        return round(10 * (v - bad) / (good - bad), 2)
    else:           # lower is better
        if v <= good: return 10.0
        if v >= bad:  return 0.0
        return round(10 * (bad - v) / (bad - good), 2)

def score_l1_screener(r: dict, asset_type: str) -> dict:
    """
    Layer 1 from screener fields — no WinRate/NetProfit (those are alert-only).
    RSI zone (30%) + ADX strength (25%) + SMA200 alignment (20%) + Volume/RelVol (25%)
    """
    rsi    = r.get("RSI")
    adx    = r.get("ADX")
    close  = r.get("close")
    sma200 = r.get("SMA200")
    change = r.get("change", 0) or 0
    relvol = r.get("relative_volume_10d_calc")
    volume = r.get("volume")

    # RSI zone (ideal 55–70 for trend continuation)
    if rsi is None:
        s_rsi = 5.0
    elif 60 <= rsi <= 70: s_rsi = 10.0
    elif 70 < rsi <= 75:  s_rsi = 7.5
    elif 55 <= rsi < 60:  s_rsi = 7.0
    elif 50 <= rsi < 55:  s_rsi = 5.0
    else:                  s_rsi = 2.0

    # ADX: trend strength ≥ 30 is strong
    s_adx = _norm10(adx, 40, 20)

    # SMA200 alignment: above = good, well above = extended, below = filtered already
    if close and sma200 and sma200 > 0:
        gap_pct = (close - sma200) / sma200 * 100
        if   gap_pct > 30:  s_sma = 4.0   # too extended, reversal risk
        elif gap_pct > 10:  s_sma = 9.0   # healthy momentum
        elif gap_pct > 2:   s_sma = 10.0  # just above — best entry zone
        elif gap_pct >= 0:  s_sma = 7.0   # just crossed above
        else:               s_sma = 0.0   # below (shouldn't hit due to filter)
    elif asset_type == "crypto":
        s_sma = 6.0   # crypto: no SMA200 in TV screener → neutral
    else:
        s_sma = 5.0

    # Volume / relative volume conviction
    if relvol is not None:
        if   relvol >= 3.0: s_vol = 10.0
        elif relvol >= 2.0: s_vol = 8.0
        elif relvol >= 1.5: s_vol = 6.5
        elif relvol >= 1.0: s_vol = 5.0
        elif relvol >= 0.7: s_vol = 3.0
        else:               s_vol = 1.0
    elif volume is not None and volume > 0:
        # For crypto/futures: change% as momentum proxy
        s_vol = min(10.0, max(0.0, 5.0 + float(change) * 0.4))
    else:
        s_vol = 5.0

    total = round(s_rsi * 0.30 + s_adx * 0.25 + s_sma * 0.20 + s_vol * 0.25, 2)
    return {
        "total":   total,
        "rsi":     s_rsi,  "adx": s_adx, "sma": s_sma, "vol": s_vol,
        "raw_rsi": rsi,    "raw_adx": adx, "raw_rv": relvol, "raw_change": change,
    }


# ── Layer 2: Fundamentals (stocks only) ──────────────────────────────────────
def score_l2_neutral() -> dict:
    return {"total": 5.0, "note": "N/A", "_neutral": True}


# ── Layer 3: Sentiment — adapted per class ────────────────────────────────────
def score_l3_news_only(tv_sym: str, desc: str, asset_type: str) -> dict:
    """News-only sentiment — used for non-stock assets."""
    query    = _news_query(tv_sym, desc, asset_type)
    headlines = fetch_google_news(tv_sym, query)
    news     = compute_news_sentiment(headlines)
    # -1→0, 0→5, +1→10
    score = round(max(0.0, min(10.0, (news["score"] + 1) * 5)), 2)
    return {"total": score, "news": news, "_news_only": True}


# ── Layer 4: Market internals — adapted per class ─────────────────────────────
def _fetch_52w_range_yf(yf_sym: str) -> tuple[float | None, float | None]:
    """Return (52W_high, 52W_low) from yfinance for any supported symbol."""
    try:
        info = yf.Ticker(yf_sym).fast_info
        hi   = getattr(info, "year_high",  None) or getattr(info, "fiftyTwoWeekHigh", None)
        lo   = getattr(info, "year_low",   None) or getattr(info, "fiftyTwoWeekLow",  None)
        return (float(hi) if hi else None, float(lo) if lo else None)
    except Exception:
        return None, None

def score_l4_position_only(close: float, yf_sym: str | None, yc: dict) -> dict:
    """
    Simplified L4 for non-stock assets:
    52W position (55%) + yield curve macro (45%).
    No PCR, no insider (not applicable).
    """
    s_w52 = 5.0
    w52_pos = None
    w52_label = "N/A"
    if close and yf_sym:
        hi52, lo52 = _fetch_52w_range_yf(yf_sym)
        if hi52 and lo52 and hi52 > lo52:
            w52_pos = round((close - lo52) / (hi52 - lo52), 3)
            if   w52_pos > 0.90: s_w52, w52_label = 3.0, "Near High (extension risk)"
            elif w52_pos > 0.80: s_w52, w52_label = 6.0, "Upper Range"
            elif w52_pos > 0.60: s_w52, w52_label = 8.0, "Upper Range (momentum)"
            elif w52_pos > 0.40: s_w52, w52_label = 9.0, "Mid Range (ideal entry)"
            elif w52_pos > 0.20: s_w52, w52_label = 7.0, "Lower Range"
            else:                s_w52, w52_label = 4.0, "Near Low"

    spread = (yc or {}).get("spread_bps")
    if spread is None:     s_yc = 5.0
    elif spread < -25:     s_yc = 1.0
    elif spread < 0:       s_yc = 3.0
    elif spread < 25:      s_yc = 6.0
    elif spread < 75:      s_yc = 8.0
    else:                  s_yc = 10.0

    total = round(s_w52 * 0.55 + s_yc * 0.45, 2)
    return {
        "total": total, "w52": s_w52, "yc": s_yc,
        "w52_position": w52_pos, "w52_label": w52_label,
        "pcr": None, "pcr_label": "N/A", "insider_net": "N/A",
        "_simplified": True,
    }


# ── Composite weights by asset type ──────────────────────────────────────────
_WEIGHTS = {
    "stock":   (0.25, 0.25, 0.30, 0.20),
    "crypto":  (0.45, 0.00, 0.35, 0.20),
    "forex":   (0.50, 0.00, 0.25, 0.25),
    "futures": (0.45, 0.00, 0.30, 0.25),
}

def composite_asset(l1, l2, l3, l4, asset_type: str) -> float:
    w1, w2, w3, w4 = _WEIGHTS.get(asset_type, (0.25, 0.25, 0.30, 0.20))
    raw = l1 * w1 + l2 * w2 + l3 * w3 + l4 * w4
    return round(raw * 10, 2)   # scale 0–100


# ── Per-signal scoring orchestrator ──────────────────────────────────────────
def score_signal(r: dict, scan_key: str, yc: dict, use_news: bool,
                 verbose: bool = False) -> dict | None:
    """
    Score one screener row through the appropriate layer stack.
    Returns a result dict or None if scoring fails critically.
    """
    meta = ASSET_CLASS_META.get(scan_key, {"type": "stock", "label": scan_key, "emoji": "📈"})
    asset_type = meta["type"]
    tv_sym = r.get("tv_symbol", "")
    desc   = r.get("description", "") or ""

    if not tv_sym:
        return None

    result = {
        "tv_sym":       tv_sym,
        "scan_key":     scan_key,
        "asset_type":   asset_type,
        "asset_label":  meta["label"],
        "asset_emoji":  meta["emoji"],
        "description":  desc,
        "close":        r.get("close"),
        "change":       r.get("change"),
        "rsi":          r.get("RSI"),
        "adx":          r.get("ADX"),
        "sma200":       r.get("SMA200"),
        "relvol":       r.get("relative_volume_10d_calc"),
        "volume":       r.get("volume"),
        "subtype":      r.get("subtype"),
        "pinned":       r.get("_pinned", False),
    }

    # ── L1 always from screener data ──────────────────────────────────────────
    l1 = score_l1_screener(r, asset_type)
    result["l1"] = l1

    # ── L2 : fundamentals (stocks) or neutral ────────────────────────────────
    if asset_type == "stock":
        yf_sym = _tv_to_yf_stock(tv_sym)
        try:
            ticker_obj = yf.Ticker(yf_sym)
            fund_raw   = fetch_fundamentals(yf_sym)
        except Exception:
            fund_raw   = {}
            ticker_obj = None
        l2 = score_fundamental(fund_raw)
        result["l2"]       = l2
        result["fund_raw"] = fund_raw
        result["yf_sym"]   = yf_sym
        result["ticker_obj"] = ticker_obj
    else:
        l2 = score_l2_neutral()
        result["l2"]       = l2
        result["fund_raw"] = {}
        yf_sym             = _tv_to_yf_nonstock(tv_sym, asset_type)
        result["yf_sym"]   = yf_sym
        result["ticker_obj"] = None

    # ── L3 : sentiment ────────────────────────────────────────────────────────
    if not use_news:
        l3 = {"total": 5.0, "news": {"score": 0.0, "label": "Skipped", "count": 0},
               "_skipped": True}
    elif asset_type == "stock" and result["ticker_obj"] is not None:
        company = fund_raw.get("company_name", desc)
        sent_raw = fetch_sentiment_data(result["ticker_obj"], company, tv_sym)
        l3 = score_sentiment(sent_raw)
        l3["_sent_raw"] = sent_raw
    else:
        l3 = score_l3_news_only(tv_sym, desc, asset_type)

    result["l3"] = l3

    # ── L4 : market internals ─────────────────────────────────────────────────
    if asset_type == "stock" and result["ticker_obj"] is not None:
        internals = fetch_market_internals(result["ticker_obj"], fund_raw, tv_sym)
        l4 = score_market_internals(internals, yc)
        l4["_internals_raw"] = internals
    else:
        l4 = score_l4_position_only(r.get("close"), result["yf_sym"], yc)

    result["l4"] = l4

    # ── Composite ─────────────────────────────────────────────────────────────
    result["composite"] = composite_asset(
        l1["total"], l2["total"], l3["total"], l4["total"], asset_type
    )
    result["grade"], result["grade_color"] = grade(result["composite"])
    return result


# ── HTML helpers ──────────────────────────────────────────────────────────────
def _w52_bar(pos) -> str:
    if pos is None:
        return '<span style="color:#64748b">N/A</span>'
    pct = min(100, max(0, pos * 100))
    return (
        f'<div class="w52-outer">'
        f'<div class="w52-inner" style="width:100%"></div>'
        f'<div class="w52-thumb" style="left:calc({pct:.0f}% - 5px)"></div>'
        f'</div>'
        f'<span class="w52-label">{pct:.0f}% of 52W range</span>'
    )

def _news_html(news: dict) -> str:
    if not news or not news.get("headlines"):
        return ""
    lines = "".join(
        f'<div class="news-item">• {h[:120]}</div>'
        for h in news.get("headlines", [])[:5]
    )
    score = news.get("score", 0)
    col   = "#4ade80" if score > 0.05 else ("#f87171" if score < -0.05 else "#94a3b8")
    label = news.get("label", "Neutral")
    return (
        f'<div class="news-block">'
        f'<div style="font-size:11px;color:#64748b;margin-bottom:6px">'
        f'📰 News sentiment: <b style="color:{col}">{label}</b> '
        f'({news.get("count",0)} headlines)</div>'
        f'{lines}'
        f'</div>'
    )

_ASSET_WEIGHTS_HTML = {
    "stock":   "L1×25 L2×25 L3×30 L4×20",
    "crypto":  "L1×45 L3×35 L4×20 (no fundamentals)",
    "forex":   "L1×50 L3×25 L4×25 (no fundamentals)",
    "futures": "L1×45 L3×30 L4×25 (no fundamentals)",
}

def _build_card(e: dict) -> str:
    tv   = e["tv_sym"]
    comp = e["composite"]
    gr, gc = e["grade"], e["grade_color"]
    asset_type  = e["asset_type"]
    asset_label = e["asset_label"]
    fund   = e.get("fund_raw", {})
    l1     = e["l1"]
    l2     = e["l2"]
    l3     = e["l3"]
    l4     = e["l4"]

    change     = e.get("change", 0) or 0
    change_col = "#4ade80" if change >= 0 else "#f87171"
    pinned_badge = ' <span title="Key commodity pinned" style="font-size:11px">📌</span>' if e.get("pinned") else ""

    # Header
    company_name = fund.get("company_name") or e.get("description", "")
    sector       = fund.get("sector", "")
    market_cap   = hcap(fund.get("market_cap"))
    tv_url       = f"https://www.tradingview.com/chart/?symbol={tv}"

    cap_str  = f'<span style="color:#64748b;font-size:11px">{market_cap}</span>' if fund.get("market_cap") else ""
    sect_str = f'<span style="color:#64748b;font-size:10px">{sector}</span>' if sector else ""

    header = f"""
<div class="card-header">
  <div class="sym-block">
    <div class="sym-line">
      <a class="sym" href="{tv_url}" target="_blank">{tv}</a>{pinned_badge}
      <span class="asset-badge" style="background:{_asset_badge_color(asset_type)}">{asset_label}</span>
    </div>
    <div class="company">{company_name[:50] if company_name else ""} {cap_str} {sect_str}</div>
  </div>
  <div class="composite-block">
    <div class="comp-score" style="color:{gc}">{comp:.0f}</div>
    <div class="comp-label">{gr} — {_ASSET_WEIGHTS_HTML.get(asset_type,'')}</div>
  </div>
</div>"""

    # Price strip
    close_s = fmt(e.get("close"), d=4 if (e.get("close") or 1) < 10 else 2)
    price_strip = f"""
<div class="price-strip">
  <span>Price: <b>{close_s}</b></span>
  <span style="color:{change_col}">Chg: {fmt(change)}%</span>
  <span>RSI: <b style="color:#93c5fd">{fmt(e.get('rsi'),d=1)}</b></span>
  <span>ADX: <b>{fmt(e.get('adx'),d=1)}</b></span>
  {f'<span>SMA200: {fmt(e.get("sma200"),d=2)}</span>' if e.get('sma200') else ''}
  {f'<span>RelVol: <b>{fmt(e.get("relvol"))}x</b></span>' if e.get('relvol') else ''}
</div>"""

    # Layer bars
    l2_bar = bar(l2["total"]) if not l2.get("_neutral") else '<span style="color:#4b5563;font-size:11px">N/A (not applicable)</span>'
    layers = f"""
<div class="layers">
  <div class="layer-item">
    <div class="layer-title">L1 Technical</div>
    {bar(l1["total"])}
    <div style="font-size:10px;color:#4b5563;margin-top:3px">
      RSI:{fmt(l1.get('raw_rsi'),d=1)}  ADX:{fmt(l1.get('raw_adx'),d=1)}
      {'  RV:' + fmt(l1.get('raw_rv'))+'x' if l1.get('raw_rv') else ''}
    </div>
  </div>
  <div class="layer-item">
    <div class="layer-title">L2 Fundamental</div>
    {l2_bar}
    {'<div style="font-size:10px;color:#4b5563;margin-top:3px">Val:' + fmt(l2.get("valuation"),d=1) + '  Prof:' + fmt(l2.get("profitability"),d=1) + '  Gr:' + fmt(l2.get("growth"),d=1) + '</div>' if not l2.get('_neutral') else ''}
  </div>
  <div class="layer-item">
    <div class="layer-title">L3 Sentiment</div>
    {bar(l3["total"])}
    <div style="font-size:10px;color:#4b5563;margin-top:3px">
      {_l3_sub(l3, asset_type)}
    </div>
  </div>
  <div class="layer-item">
    <div class="layer-title">L4 Internals</div>
    {bar(l4["total"])}
    <div style="font-size:10px;color:#4b5563;margin-top:3px">
      {_l4_sub(l4, asset_type)}
    </div>
  </div>
</div>"""

    # Detail columns
    l2_col = _l2_detail_col(l2, fund, asset_type)
    l3_col = _l3_detail_col(l3, asset_type)
    l4_col = _l4_detail_col(l4, asset_type)
    l1_col = _l1_detail_col(l1, e)

    details = f"""
<div class="detail-grid">
  {l1_col}
  {l2_col}
  {l3_col}
  {l4_col}
</div>"""

    # News
    news_d  = l3.get("news", {}) or l3.get("_sent_raw", {}).get("news", {}) if not l3.get("_skipped") else {}
    news_html = _news_html(news_d)

    return f'<div class="card">{header}{price_strip}{layers}{details}{news_html}</div>'


def _asset_badge_color(asset_type: str) -> str:
    return {"stock": "#1e3a5f", "crypto": "#3b1f5e", "forex": "#1a3a2a",
            "futures": "#3a2a0a", "bonds": "#1a2a3a"}.get(asset_type, "#1e293b")

def _l3_sub(l3: dict, asset_type: str) -> str:
    if l3.get("_skipped"):  return "Skipped"
    if l3.get("_news_only"):
        news = l3.get("news", {})
        return f'News: {news.get("label","N/A")} ({news.get("count",0)} hdls)'
    sent_raw = l3.get("_sent_raw", {})
    cons     = (sent_raw.get("analyst", {}) or {}).get("consensus", "N/A")
    news     = sent_raw.get("news", {})
    return f'Analyst: {cons}  News: {(news or {}).get("label","N/A")}'

def _l4_sub(l4: dict, asset_type: str) -> str:
    if l4.get("_simplified"):
        return f'52W: {l4.get("w52_label","N/A")}'
    return f'PCR: {l4.get("pcr_label","N/A")}  Insider: {l4.get("_internals_raw",{}).get("insider_net","N/A")}'

def _l1_detail_col(l1: dict, e: dict) -> str:
    kv_rows = [
        ("RSI",    fmt(l1.get("raw_rsi"), d=1)),
        ("ADX",    fmt(l1.get("raw_adx"), d=1)),
        ("SMA200", fmt(e.get("sma200"),   d=2)),
        ("Close",  fmt(e.get("close"),    d=4 if (e.get('close') or 1) < 10 else 2)),
        ("Chg%",   fmt(e.get("change"),   d=2) + "%"),
        ("RelVol", fmt(l1.get("raw_rv")) + "x" if l1.get("raw_rv") else "—"),
    ]
    rows = "".join(f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in kv_rows)
    return f'<div class="col"><div class="col-head">L1 Technical</div>{rows}</div>'

def _l2_detail_col(l2: dict, fund: dict, asset_type: str) -> str:
    if l2.get("_neutral"):
        reasons = {
            "crypto":  "No standardised fundamentals for crypto assets.",
            "forex":   "Forex pairs driven by macro / technical, not company fundamentals.",
            "futures": "Futures / commodities: supply-demand driven, not PE/ROE.",
        }
        note = reasons.get(asset_type, "Not applicable.")
        return (f'<div class="col"><div class="col-head">L2 Fundamental</div>'
                f'<div style="font-size:11px;color:#4b5563;padding-top:8px">{note}</div></div>')

    kv_rows = [
        ("Forward PE",   fmt(fund.get("forward_pe"),    d=1)),
        ("PEG",          fmt(fund.get("peg_ratio"),     d=2)),
        ("ROE",          fmt(fund.get("roe"),           d=1) + "%"),
        ("Profit Margin",fmt(fund.get("profit_margin"), d=1) + "%"),
        ("Rev Growth",   fmt(fund.get("revenue_growth"), d=1) + "%"),
        ("D/E",          fmt(fund.get("debt_to_equity"), d=1)),
        ("Beta",         fmt(fund.get("beta"),           d=2)),
        ("Analyst Tgt",  fmt(fund.get("analyst_target"), d=2)),
    ]
    rows = "".join(f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in kv_rows)
    return f'<div class="col"><div class="col-head">L2 Fundamental</div>{rows}</div>'

def _l3_detail_col(l3: dict, asset_type: str) -> str:
    if l3.get("_skipped"):
        return '<div class="col"><div class="col-head">L3 Sentiment</div><div style="font-size:11px;color:#4b5563;padding-top:8px">Skipped (--no-news)</div></div>'

    if l3.get("_news_only"):
        news = l3.get("news", {})
        score_col = "#4ade80" if (news.get("score",0) or 0) > 0.05 else "#f87171" if (news.get("score",0) or 0) < -0.05 else "#94a3b8"
        kv_rows = [
            ("News Sentiment", f'<span style="color:{score_col}">{news.get("label","N/A")}</span>'),
            ("Compound Score", fmt(news.get("score"), d=3)),
            ("Headlines",      str(news.get("count", 0))),
            ("Positive",       str(news.get("pos_count", 0))),
            ("Negative",       str(news.get("neg_count", 0))),
            ("Analyst",        "N/A (not applicable)"),
        ]
        rows = "".join(f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in kv_rows)
        return f'<div class="col"><div class="col-head">L3 Sentiment</div>{rows}</div>'

    # Full stock sentiment
    sent_raw = l3.get("_sent_raw", {})
    analyst  = (sent_raw.get("analyst") or {})
    news     = (sent_raw.get("news") or {})
    earn     = (sent_raw.get("earnings_beats") or {})
    upgrades = sent_raw.get("upgrade_detail", [])
    news_col = "#4ade80" if (news.get("score",0) or 0) > 0.05 else "#f87171" if (news.get("score",0) or 0) < -0.05 else "#94a3b8"

    kv_rows = [
        ("Consensus",   analyst.get("consensus", "N/A")),
        ("Analysts",    str(analyst.get("total", 0))),
        ("News",        f'<span style="color:{news_col}">{news.get("label","N/A")}</span>'),
        ("Beat Rate",   fmt(earn.get("beat_rate",0)*100, d=0) + "%"),
        ("Earn Risk",   sent_raw.get("earnings_risk", "N/A")),
    ]
    rows = "".join(f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in kv_rows)
    if upgrades:
        rows += '<div class="upgrades">' + "".join(f'<div class="up-item">↑ {u}</div>' for u in upgrades[:3]) + '</div>'
    return f'<div class="col"><div class="col-head">L3 Sentiment</div>{rows}</div>'

def _l4_detail_col(l4: dict, asset_type: str) -> str:
    w52_bar_html = _w52_bar(l4.get("w52_position"))
    kv_rows = [
        ("52W Position", l4.get("w52_label", "N/A")),
        ("Yield Curve",  "See macro panel"),
    ]
    if l4.get("_simplified"):
        kv_rows += [
            ("Options PCR",  "N/A (no options)"),
            ("Insider",      "N/A (not applicable)"),
        ]
    else:
        internals = l4.get("_internals_raw", {})
        kv_rows += [
            ("PCR",          l4.get("pcr_label", "N/A")),
            ("Insider",      internals.get("insider_net", "N/A")),
        ]
    rows = "".join(f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in kv_rows)
    return f'<div class="col"><div class="col-head">L4 Internals</div>{w52_bar_html}{rows}</div>'


# ── Bond yield context panel (no scoring) ─────────────────────────────────────
def _bonds_panel(bond_rows: list) -> str:
    if not bond_rows:
        return ""
    rows_html = ""
    for r in bond_rows[:12]:
        sym   = r.get("tv_symbol", "")
        desc  = (r.get("description") or "")[:45]
        close = fmt(r.get("close"), d=3)
        chg   = r.get("change", 0) or 0
        chg_c = "#4ade80" if chg >= 0 else "#f87171"
        url   = f"https://www.tradingview.com/chart/?symbol={sym}"
        rows_html += (
            f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
            f'border-bottom:1px solid #1e293b;font-size:12px">'
            f'<a href="{url}" target="_blank" style="color:#90caf9;text-decoration:none">{sym}</a>'
            f'<span style="color:#64748b;flex:1;padding:0 12px">{desc}</span>'
            f'<b>{close}</b>'
            f'<span style="color:{chg_c};margin-left:10px">{chg:+.2f}%</span>'
            f'</div>'
        )
    return f"""
<div class="card" style="border-color:#1e3a5f">
  <div class="card-header">
    <div class="sym-block">
      <div class="sym-line"><span class="sym">📊 Bond Yield Context</span>
        <span class="asset-badge" style="background:#1a2a3a">Benchmark Yields (TVC)</span>
      </div>
      <div class="company" style="color:#64748b">No buy/sell scoring — reference only</div>
    </div>
  </div>
  <div style="padding:14px 20px">{rows_html}</div>
</div>"""


# ── Full HTML report ──────────────────────────────────────────────────────────
def build_html(scored: list, bond_rows: list, args, regime: dict, yc: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Sort by composite score desc
    scored_sorted = sorted(scored, key=lambda x: x.get("composite", 0), reverse=True)

    # Group by asset_type for tab counts
    counts = {}
    for e in scored_sorted:
        k = e["scan_key"]
        counts[k] = counts.get(k, 0) + 1

    # Tab navigation
    tab_btns = '<button type="button" class="tab-btn active" onclick="showTab(\'all\',this)">All (%d)</button>\n' % len(scored_sorted)
    tab_btns += '<button type="button" class="tab-btn" onclick="showTab(\'stocks\',this)">📈 Stocks (%d)</button>\n' % sum(v for k, v in counts.items() if "stock" in k)
    for ak in ["crypto", "forex", "futures"]:
        n = sum(v for k, v in counts.items() if k == ak)
        if n:
            meta = ASSET_CLASS_META.get(ak, {})
            tab_btns += f'<button type="button" class="tab-btn" onclick="showTab(\'{ak}\',this)">{meta.get("label","?")} ({n})</button>\n'

    cards_all   = "".join(_build_card(e) for e in scored_sorted)
    cards_stock = "".join(_build_card(e) for e in scored_sorted if "stock" in e["scan_key"])
    cards_crypto  = "".join(_build_card(e) for e in scored_sorted if e["scan_key"] == "crypto")
    cards_forex   = "".join(_build_card(e) for e in scored_sorted if e["scan_key"] == "forex")
    cards_futures = "".join(_build_card(e) for e in scored_sorted if e["scan_key"] == "futures")

    bonds_html = _bonds_panel(bond_rows)

    spread = yc.get("spread_bps")
    spread_s = (f'{spread:+.0f}bps{"  ⚠️ INVERTED" if spread < 0 else ""}') if spread is not None else "N/A"
    yc_strip = f'2s10s Yield Curve: <b>{spread_s}</b> &nbsp;|&nbsp; US10Y: {yc.get("us10y","N/A")} &nbsp;|&nbsp; US2Y: {yc.get("us2y","N/A")}'

    w_str = _ASSET_WEIGHTS_HTML
    threshold_note = f"Showing composite ≥ {args.threshold}" if args.threshold else "All signals shown"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Multi-Asset Actionable Buy — {now}</title>
<style>
:root{{--bg:#0d1117;--card:#0f1923;--border:#1e293b;--text:#e2e8f0;--sub:#64748b;--accent:#1d4ed8}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif}}
.header{{padding:20px 28px;border-bottom:1px solid var(--border)}}
.header h1{{font-size:1.35em;color:#60a5fa}}
.sub{{font-size:0.8em;color:var(--sub);margin-top:4px}}
.yc-strip{{padding:8px 28px;background:#0a0f1e;font-size:12px;color:var(--sub);border-bottom:1px solid var(--border)}}
.legend{{padding:10px 28px;font-size:11px;color:var(--sub);display:flex;flex-wrap:wrap;gap:12px;border-bottom:1px solid var(--border)}}
.legend b{{color:var(--text)}}
.tab-nav{{display:flex;flex-wrap:wrap;gap:4px;padding:14px 28px;border-bottom:1px solid var(--border)}}
.tab-btn{{background:#161b22;border:1px solid var(--border);color:var(--sub);padding:6px 16px;border-radius:4px;cursor:pointer;font-size:0.84em;transition:all .15s}}
.tab-btn:hover{{background:#21262d;color:var(--text)}}
.tab-btn.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
.tab-pane{{display:none}}.tab-pane.active{{display:block}}
.cards{{padding:20px 28px;display:flex;flex-direction:column;gap:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
.card-header{{display:flex;justify-content:space-between;align-items:flex-start;padding:16px 20px 12px}}
.sym-block{{flex:1}}
.sym-line{{display:flex;align-items:center;gap:8px;margin-bottom:4px}}
.sym{{font-size:1.15em;font-weight:700;color:#60a5fa;text-decoration:none}}
.sym:hover{{text-decoration:underline}}
.asset-badge{{font-size:10px;padding:2px 8px;border-radius:10px;color:#94a3b8;font-weight:500}}
.company{{font-size:12px;color:var(--sub);display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.composite-block{{text-align:right;min-width:80px}}
.comp-score{{font-size:36px;font-weight:800;line-height:1}}
.comp-label{{font-size:10px;color:var(--sub);margin-top:2px;white-space:nowrap}}
.price-strip{{padding:6px 20px;background:#0a0f1e;font-size:12px;display:flex;gap:18px;flex-wrap:wrap;color:var(--sub)}}
.price-strip b{{color:var(--text)}}
.layers{{display:flex;gap:12px;padding:12px 20px;background:#0a0f1e;border-bottom:1px solid var(--border)}}
.layer-item{{flex:1;min-width:90px}}
.layer-title{{font-size:10px;color:var(--sub);margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em}}
.bar-row{{display:flex;align-items:center;gap:6px}}
.bar-bg{{flex:1;height:8px;background:#1e293b;border-radius:4px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px}}
.bar-val{{font-size:11px;color:#94a3b8;min-width:24px;font-family:monospace}}
.detail-grid{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:0;padding:0 20px}}
@media(max-width:1100px){{.detail-grid{{grid-template-columns:1fr 1fr}}}}
.col{{padding:14px 10px;border-right:1px solid var(--border)}}
.col:last-child{{border-right:none}}
.col-head{{font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)}}
.kv{{display:flex;justify-content:space-between;padding:3px 0;font-size:12px}}
.kv span{{color:var(--sub)}}.kv b{{color:var(--text);font-weight:600}}
.green{{color:#4ade80!important}}.red{{color:#f87171!important}}
.upgrades{{margin-top:6px}}.up-item{{font-size:11px;color:#4ade80;padding:2px 0}}
.w52-outer{{position:relative;height:8px;background:#1e293b;border-radius:4px;overflow:hidden;margin:8px 0 4px}}
.w52-inner{{height:100%;background:linear-gradient(90deg,#ef4444,#eab308,#22c55e);opacity:.5}}
.w52-thumb{{position:absolute;top:-2px;width:10px;height:12px;background:#fff;border-radius:2px}}
.w52-label{{font-size:10px;color:var(--sub)}}
.news-block{{padding:12px 20px;border-top:1px solid var(--border)}}
.news-item{{font-size:11px;color:#94a3b8;padding:5px 8px;margin-bottom:4px;background:#0a0f1e;border-radius:4px;line-height:1.4}}
.footer{{padding:16px 28px;color:#374151;font-size:11px;border-top:1px solid var(--border)}}
</style>
</head>
<body>
<div class="header">
  <h1>🎯 Multi-Asset Actionable Buy</h1>
  <div class="sub">4-Layer Confluence (adapted per asset class) | {now}</div>
</div>
<div class="yc-strip">{yc_strip}</div>
<div class="legend">
  <b>Weight logic:</b>
  <span>Stocks: L1×25 + L2×25 + L3×30 + L4×20</span>
  <span>Crypto: L1×45 + L3×35 + L4×20</span>
  <span>Forex: L1×50 + L3×25 + L4×25</span>
  <span>Futures: L1×45 + L3×30 + L4×25</span>
  <b>|</b> <span>{threshold_note}</span>
</div>
{_regime_banner(regime)}
<div class="tab-nav">
{tab_btns}
</div>
<div class="tab-pane active" id="tab-all">
  <div class="cards">{cards_all}{bonds_html}</div>
</div>
<div class="tab-pane" id="tab-stocks">
  <div class="cards">{cards_stock}</div>
</div>
<div class="tab-pane" id="tab-crypto">
  <div class="cards">{cards_crypto}</div>
</div>
<div class="tab-pane" id="tab-forex">
  <div class="cards">{cards_forex}</div>
</div>
<div class="tab-pane" id="tab-futures">
  <div class="cards">{cards_futures}</div>
</div>
<div class="footer">
  ⚠️ Research only — not financial advice. Data: TV Screener (L1) + yfinance (L2/L4) + Google News RSS + VADER sentiment (L3).
</div>
<script>
function showTab(key, btn) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + key).classList.add('active');
  btn.classList.add('active');
}}
</script>
</body></html>"""


# ── CLI entry point ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Multi-asset actionable buy report with 4-layer scoring",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--rsi-min",       type=float, default=50.0)
    parser.add_argument("--rsi-max",       type=float, default=75.0)
    parser.add_argument("--adx-min",       type=float, default=20.0)
    parser.add_argument("--max",           type=int,   default=20,
                        help="Max signals per asset class to score")
    parser.add_argument("--threshold",     type=float, default=None,
                        help="Only include signals with composite >= threshold")
    parser.add_argument("--class",         dest="scan_class", nargs="*",
                        choices=[d[0] for d in SCAN_DEFS],
                        help="Limit to these asset classes")
    parser.add_argument("--no-news",       action="store_true",
                        help="Skip news fetching (much faster, no L3 news score)")
    parser.add_argument("--out",           type=str, default=str(OUTPUT_HTML))
    args = parser.parse_args()
    # Expose max_per_class to run_all_scans (it uses args.max_per_class)
    args.max_per_class = args.max
    args.bearish       = False

    print(f"\n{'='*62}")
    print(f"  Multi-Asset Actionable Buy  |  RSI [{args.rsi_min}–{args.rsi_max}]  ADX > {args.adx_min}")
    print(f"  Max per class: {args.max}  |  News: {'OFF' if args.no_news else 'ON'}")
    print(f"{'='*62}\n")

    # ── Step 1: Run screener ──────────────────────────────────────────────────
    print("─── Step 1: Screener ────────────────────────────────────────")
    t0 = time.time()
    screener_results = run_all_scans(args)
    print(f"  Screener done in {time.time()-t0:.1f}s\n")

    # ── Step 2: Fetch macro context ───────────────────────────────────────────
    print("─── Step 2: Macro context ───────────────────────────────────")
    yc = {}
    try:
        yc = fetch_yield_curve()
        spread = yc.get("spread_bps")
        sp_s = f"{spread:+.0f}bps" if spread is not None else "N/A"
        print(f"  Yield curve 2s10s: {sp_s}  US10Y={yc.get('us10y','?')}  US2Y={yc.get('us2y','?')}")
    except Exception as e:
        print(f"  Yield curve error: {e}")

    regime = {}
    try:
        regime = fetch_market_regime()
        if regime:
            print(f"  Regime: {regime.get('regime')}  VIX={regime.get('vix')}  SPY_dd={regime.get('spy_drawdown')}%")
    except Exception as e:
        print(f"  Regime error: {e}")
    print()

    # ── Step 3: Score each signal ─────────────────────────────────────────────
    print("─── Step 3: Scoring signals ─────────────────────────────────")
    scored_all   = []
    bond_rows    = screener_results.pop("bonds", [])

    total_signals = sum(len(v) for v in screener_results.values())
    print(f"  Scoring {total_signals} signals across {len(screener_results)} asset classes …\n")

    for scan_key, rows in screener_results.items():
        meta = ASSET_CLASS_META.get(scan_key, {})
        asset_type = meta.get("type", "stock")
        print(f"  [{scan_key}]  {len(rows)} signals …", flush=True)
        class_scored = []
        for r in rows:
            if "_error" in r:
                continue
            try:
                result = score_signal(
                    r, scan_key, yc,
                    use_news=(not args.no_news),
                )
                if result:
                    class_scored.append(result)
            except Exception:
                if args.max > 5:   # only show errors in non-quiet mode
                    traceback.print_exc()

        # Sort by composite within class, take top N
        class_scored.sort(key=lambda x: x.get("composite", 0), reverse=True)
        if args.threshold:
            class_scored = [x for x in class_scored if x.get("composite", 0) >= args.threshold]

        scored_all.extend(class_scored)
        scores = [x["composite"] for x in class_scored]
        if scores:
            print(f"    → {len(class_scored)} scored  "
                  f"avg={sum(scores)/len(scores):.0f}  "
                  f"max={max(scores):.0f}  "
                  f"min={min(scores):.0f}")
        else:
            print(f"    → 0 signals passed threshold")

    print(f"\n  Total scored: {len(scored_all)}")
    print()

    # ── Step 4: Build + save HTML ─────────────────────────────────────────────
    print("─── Step 4: Building report ─────────────────────────────────")
    # Restore bonds for display
    screener_results["bonds"] = bond_rows

    html = build_html(scored_all, bond_rows, args, regime, yc)
    out_path = Path(args.out)
    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out_path)
    except Exception:
        pass
    out_path.write_text(html, encoding="utf-8")
    print(f"  Report saved → {out_path}")
    print(f"  Total time: {time.time()-t0:.1f}s")
    print()


if __name__ == "__main__":
    main()
