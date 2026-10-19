"""
tv_screener_fundamentals.py
===========================
Replaces yfinance for fundamental data in signal_stack.py and sell_actionable.py.

WHY THIS BEATS yfinance:
  • yfinance returns HTTP 404 for ASX stocks (CBA, MND, APE, ALQ) → all fields null
  • yfinance returns no fundamental data for leveraged ETFs (SOXL, MSFU, METU)
  • yfinance makes 1 HTTP call PER symbol (sequential, slow)
  • TV Screener fetches ALL symbols in ONE call per exchange (batch, fast)
  • TV Screener adds fields yfinance doesn't have: ROIC, operating_margin, 
    earnings dates, TV recommendation signal, analyst price target range

VERDICT (from field probe on 2026-04-05):
  TV Screener wins for:  fundamentals, batch capability, ASX coverage, ETF data
  Keep yfinance for:     OHLC price history (ATR/RSI calc), news headlines, VIX

VALID TV SCREENER FUNDAMENTAL FIELDS (confirmed via probe):
  price_earnings_ttm, enterprise_value_ebitda_ttm, market_cap_basic,
  return_on_equity, return_on_assets, return_on_invested_capital,
  net_margin, gross_margin, operating_margin,
  debt_to_equity, current_ratio, quick_ratio,
  net_debt, cash_n_equivalents_fq, free_cash_flow, free_cash_flow_ttm,
  dividends_yield, dividend_yield_recent,
  beta_1_year, beta_5_year,
  price_52_week_high, price_52_week_low,
  price_target_median, price_target_high, price_target_low,
  earnings_release_next_date, earnings_release_date,
  earnings_per_share_diluted_yoy_growth_ttm, earnings_per_share_diluted_ttm,
  total_revenue, net_income, sector, industry, description,
  recommendation_mark, Recommend.All, RSI, MACD.macd, SMA200, ATR,
  relative_volume_10d_calc, average_volume_10d_calc, change, volume,
  float_shares_outstanding, total_shares_outstanding

INVALID (HTTP 400 - do NOT use):
  peg_ratio, price_to_book_fwd, price_earnings_fwd, price_to_sales_ttm,
  ebitda_margin, free_cash_flow_margin, revenue_growth_yoy_fwd,
  earnings_growth_yoy_fwd, earnings_growth_ttm, payout_ratio,
  beta, 52_week_high, 52_week_low, short_percent_of_float,
  institutional_holdings_percent, number_of_analysts

UNIT NOTES (TV Screener values are already in % for margin/yield fields):
  return_on_equity         → 101.5  means 101.5%  (yfinance: 1.015)
  net_margin               → 27.04  means 27.04%  (yfinance: 0.2704)
  gross_margin             → 4.85   means 4.85%   (yfinance: 0.0485)
  dividends_yield          → 0.418  means 0.418%  (yfinance: 0.00418)
  earnings_per_share_diluted_yoy_growth_ttm → 25.65 means 25.65% growth
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ─── Scanner URL table ────────────────────────────────────────────────────────
# Tradable markets only: US, Australia, India, Hong Kong, Singapore, Japan, Korea.
# Non-tradable exchanges (Canada, UK, Europe, futures, Brazil, Israel, Malaysia etc.)
# are intentionally excluded — we don't trade there.
_TV_SCANNER = {
    # United States
    "NYSE":       "https://scanner.tradingview.com/america/scan",
    "NASDAQ":     "https://scanner.tradingview.com/america/scan",
    "AMEX":       "https://scanner.tradingview.com/america/scan",
    "BATS":       "https://scanner.tradingview.com/america/scan",
    "CBOE":       "https://scanner.tradingview.com/america/scan",
    # Australia
    "ASX":        "https://scanner.tradingview.com/australia/scan",
    "ASX_DLY":    "https://scanner.tradingview.com/australia/scan",
    # India (equities only — MCX is commodity exchange, excluded)
    "NSE":        "https://scanner.tradingview.com/india/scan",
    "BSE":        "https://scanner.tradingview.com/india/scan",
    # Hong Kong
    "HKEX":       "https://scanner.tradingview.com/hongkong/scan",
    # Singapore
    "SGX":        "https://scanner.tradingview.com/singapore/scan",
    # Japan
    "TYO":        "https://scanner.tradingview.com/japan/scan",
    "TSE":        "https://scanner.tradingview.com/japan/scan",
    # Korea
    "KRX":        "https://scanner.tradingview.com/korea/scan",
    "KOSDAQ":     "https://scanner.tradingview.com/korea/scan",
}

# ─── Confirmed-valid field list (tested 2026-04-05) ──────────────────────────
# "name" is always included as index[0]; don't add it here.
FUND_FIELDS = [
    # Valuation
    "price_earnings_ttm",                        # P/E TTM
    "enterprise_value_ebitda_ttm",               # EV/EBITDA
    "market_cap_basic",                          # Market cap (USD)
    # Profitability (all returned as %, already multiplied by 100)
    "return_on_equity",                          # ROE %
    "return_on_assets",                          # ROA %
    "return_on_invested_capital",                # ROIC % (not in yfinance)
    "net_margin",                                # Net margin %
    "gross_margin",                              # Gross margin %
    "operating_margin",                          # Operating margin %
    # Health
    "debt_to_equity",                            # D/E ratio
    "current_ratio",                             # Current ratio
    "quick_ratio",                               # Quick ratio
    "net_debt",                                  # Net debt ($)
    "cash_n_equivalents_fq",                     # Cash ($)
    "free_cash_flow",                            # FCF annual ($)
    "free_cash_flow_ttm",                        # FCF TTM ($)
    # Valuation (cont.)
    "price_earnings_growth_ttm",                 # PEG ratio TTM (P/E ÷ EPS-growth)
    # Growth (% already)
    "earnings_per_share_diluted_yoy_growth_ttm", # EPS YoY growth TTM %
    "earnings_per_share_diluted_ttm",            # EPS diluted TTM
    # Dividends
    "dividends_yield",                           # Dividend yield %
    "dividend_yield_recent",                     # Dividend yield recent %
    # Risk
    "beta_1_year",                               # Beta 1Y
    # Analyst
    "price_target_median",                       # Consensus price target
    "price_target_high",                         # Bull case target
    "price_target_low",                          # Bear case target
    "recommendation_mark",                       # 1=Strong Buy, 5=Strong Sell
    # Price stats
    "price_52_week_high",                        # 52-week high
    "price_52_week_low",                         # 52-week low
    # Earnings calendar
    "earnings_release_next_date",                # Next earnings (unix ts)
    "earnings_release_date",                     # Last earnings (unix ts)
    # Fundamentals extras
    "total_revenue",                             # Annual revenue ($)
    "net_income",                                # Net income ($)
    # Classification
    "sector",
    "industry",
    "description",                               # Company name / description
    # TV technical signals (bonus — not in yfinance)
    "Recommend.All",                             # TV overall: -1=Strong Sell, 1=Strong Buy
    "Recommend.MA",                              # MA recommendation
    "RSI",                                       # RSI 14
    "SMA200",                                    # 200-day SMA
    "ATR",                                       # ATR 14 (absolute)
    # Volume
    "relative_volume_10d_calc",
    "average_volume_10d_calc",
    "change",                                    # % change today
    "volume",
    "close",                                     # Current/last close price (native currency)
    "currency",                                  # Native currency code (INR, AUD, USD, HKD...)
]

# ─── Cookie for Pro auth (optional) ──────────────────────────────────────────
# ─── Exchange → native currency fallback (used when TV doesn't return 'currency') ─
_EXCHANGE_CURRENCY = {
    "NSE": "INR", "BSE": "INR",
    "ASX": "AUD", "ASX_DLY": "AUD",
    "HKEX": "HKD",
    "SGX": "SGD",
    "NYSE": "USD", "NASDAQ": "USD", "AMEX": "USD", "BATS": "USD", "CBOE": "USD",
    "LSE": "GBP",
    "TSX": "CAD",
    "BINANCE": "USD", "COINBASE": "USD",
}

_COOKIE_PATH = Path(__file__).parent / "cookie"
_TV_COOKIE   = _COOKIE_PATH.read_text(encoding="utf-8").strip() if _COOKIE_PATH.exists() else None


def _resolve_response_symbol(raw_sym: str, sym_map: dict, ticker_candidates: dict, url: str):
    """
    Map a scanner response symbol back to the requested pro_symbol.

    Returns ``(pro_symbol, is_exact)`` or ``(None, False)``.

    Exact exchange-qualified matches win (e.g. response "NYSE:ALL" → requested
    "NYSE:ALL"). A bare-ticker fallback is allowed only when the ticker is
    unambiguous (exactly one requested pro_symbol) and that candidate belongs to
    the same scanner. Unsolicited or ambiguous responses return None so they are
    never assigned to the wrong symbol (e.g. NYSE:ALL vs ASX:ALL).
    """
    if not raw_sym:
        return None, False
    key = raw_sym.upper()
    if key in sym_map:
        return sym_map[key], True
    # An exchange-qualified response that did not exactly match a requested
    # symbol must never be reassigned to a different exchange. Without this
    # guard a response of "BSE:ABC" was accepted for a request of "NSE:ABC"
    # because both share the india scanner (review 2026-10-13, Issue 3).
    if ":" in key:
        return None, False
    candidates = ticker_candidates.get(key, [])
    if len(candidates) == 1:
        cand = candidates[0]
        exch = cand.split(":", 1)[0].upper()
        if _TV_SCANNER.get(exch) == url:
            return cand, False
    return None, False


def fetch_tv_fundamentals(pro_symbols: list, use_auth: bool = True) -> dict:
    """
    Batch-fetch all fundamental + technical fields for a list of TV pro_symbols
    (e.g. ["NYSE:NVDA", "ASX:CBA", "NYSE:SOXL"]).

    Groups by exchange → one HTTP call per exchange scanner.

    Returns:
        {
            "NYSE:NVDA": {
                "price_earnings_ttm": 32.4,
                "return_on_equity":   101.5,  # already in %
                "net_margin":         27.0,   # already in %
                "sector":             "Technology",
                "recommendation_mark": 1.3,
                "RSI":                55.2,
                "earnings_release_next_date": 1777550400,  # unix ts
                ...
            },
            "ASX:CBA": { ... },   # real data — no 404!
            "NYSE:SOXL": { ... }, # at least vol/RSI/ATR data
        }

    Gracefully returns empty {} for symbols whose exchange isn't recognized.
    """
    # Group tickers by scanner URL
    by_url: dict[str, list[str]] = {}
    sym_map: dict[str, str] = {}          # exact "EXCH:TICKER" (upper) → pro_symbol
    ticker_candidates: dict[str, list] = {}  # bare ticker (upper) → [pro_symbol, ...]

    for ps in pro_symbols:
        if ":" not in ps:
            continue
        exch, ticker = ps.split(":", 1)
        url = _TV_SCANNER.get(exch.upper())
        if not url:
            continue
        by_url.setdefault(url, []).append(ticker)
        sym_map[ps.upper()] = ps
        ticker_candidates.setdefault(ticker.upper(), []).append(ps)

    if not by_url:
        return {}

    all_columns = ["name"] + FUND_FIELDS
    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/",
    }
    if use_auth and _TV_COOKIE:
        headers["Cookie"] = _TV_COOKIE

    results: dict[str, dict] = {}

    for url, tickers in by_url.items():
        payload = {
            "columns": all_columns,
            "filter":  [{"left": "name", "operation": "in_range", "right": tickers}],
            "range":   [0, len(tickers) + 10],
        }
        try:
            body = json.dumps(payload).encode()
            req  = urllib.request.Request(url, data=body, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            for item in data.get("data", []):
                raw_sym = item.get("s", "")                  # e.g. "ASX:CBA"
                d       = item.get("d", [])
                # Map response symbol back to the requested pro_symbol. Exact
                # exchange-qualified matches win; ambiguous/unsolicited
                # responses are skipped rather than mis-assigned.
                ps, is_exact = _resolve_response_symbol(
                    raw_sym, sym_map, ticker_candidates, url
                )
                if ps is None:
                    continue
                # Never let a bare-ticker fallback overwrite an exact match.
                if not is_exact and ps in results:
                    continue
                row: dict = {}
                for i, col in enumerate(all_columns):
                    if i < len(d):
                        row[col] = d[i]
                results[ps] = row
        except Exception:
            pass   # non-fatal; caller's score_fundamental handles missing keys

    return results


def score_fundamental(fund: dict) -> dict:
    """
    Score fundamental quality 0–10 per sub-category.
    Identical interface to the old yfinance-based version, 
    but uses TV Screener field names + correct units (TV returns % directly).

    Returns:
        {
          "total":         0–10,
          "valuation":     0–10,
          "profitability": 0–10,
          "growth":        0–10,
          "health":        0–10,
        }
    Returns neutral {"total": 5.0, ...} when fund is empty (ETFs, unknowns).
    """
    if not fund or fund.get("error"):
        return {"total": 5.0, "valuation": 5.0, "profitability": 5.0,
                "growth": 5.0, "health": 5.0}

    def g(key, default=None):
        v = fund.get(key)
        return v if v not in (None, "N/A") else default

    def val(v, good, bad):
        """Lower is better. v=None → neutral 5."""
        if v is None or v <= 0: return 5.0
        if v <= good:           return 10.0
        if v >= bad:            return 0.0
        return round(10.0 * (bad - v) / max(bad - good, 0.001), 2)

    def hi(v, good, bad):
        """Higher is better. v=None → neutral 5."""
        if v is None:   return 5.0
        if v >= good:   return 10.0
        if v <= bad:    return 0.0
        return round(10.0 * (v - bad) / max(good - bad, 0.001), 2)

    # ── Valuation (lower = better) ─────────────────────────────────────────────
    # TV: price_earnings_ttm is TTM P/E (use instead of forward P/E — still valid signal)
    pe    = g("price_earnings_ttm")
    evebi = g("enterprise_value_ebitda_ttm")
    s_pe   = val(pe,    15, 50)           # PE <15 = great, >50 = expensive
    s_ev   = val(evebi, 10, 40)           # EV/EBITDA <10 = great
    s_val  = round(s_pe * 0.6 + s_ev * 0.4, 2) if evebi else s_pe

    # ── Profitability ──────────────────────────────────────────────────────────
    # TV fields already in % (e.g. 27.0 = 27%), thresholds are in same % units
    roe  = g("return_on_equity")           # % (101.5 = 101.5%)
    roic = g("return_on_invested_capital") # % — bonus field vs yfinance
    nm   = g("net_margin")                 # %
    gm   = g("gross_margin")               # %
    om   = g("operating_margin")           # %
    s_roe  = hi(roe,   15, -5)             # >15% good, <-5% bad
    s_roic = hi(roic,  12, -5)             # >12% good
    s_nm   = hi(nm,    15, -3)             # >15% good, <0% bad
    s_gm   = hi(gm,    40,  5)             # >40% good
    s_om   = hi(om,    20, -5)             # >20% good
    # Weighted: ROE most important, then NM/OM, then ROIC
    s_prof = round(s_roe*0.30 + s_nm*0.30 + s_gm*0.15 + s_om*0.15 + s_roic*0.10, 2)

    # ── Growth ────────────────────────────────────────────────────────────────
    # TV: earnings_per_share_diluted_yoy_growth_ttm is EPS YoY % growth
    # Using EPS growth as proxy for both earnings and revenue growth
    eps_g = g("earnings_per_share_diluted_yoy_growth_ttm")  # %
    s_g   = hi(eps_g, 20, -20) if eps_g is not None else 5.0

    # ── Health ────────────────────────────────────────────────────────────────
    de  = g("debt_to_equity")
    cr  = g("current_ratio")
    fcf = g("free_cash_flow") or g("free_cash_flow_ttm")
    # TV debt_to_equity is actual ratio (AAPL≈1.0, Boeing≈10.3, NVDA≈0.07)
    # Thresholds: ≤0.5 = great (low leverage), ≥3.0 = bad (highly leveraged)
    s_de   = val(de, 0.5, 3.0)            # ratio-based (not ×100)
    s_cr   = hi(cr, 2.0, 0.5) if cr else 5.0
    s_fcf  = 10.0 if (fcf and fcf > 0) else (0.0 if (fcf and fcf < 0) else 5.0)
    s_hlth = round(s_de*0.4 + s_cr*0.3 + s_fcf*0.3, 2)

    total = round(s_val*0.25 + s_prof*0.30 + s_g*0.25 + s_hlth*0.20, 2)
    return {
        "total":         total,
        "valuation":     s_val,
        "profitability": s_prof,
        "growth":        s_g,
        "health":        s_hlth,
    }


def get_tv_recommendation_score(fund: dict) -> float:
    """
    Extract TV's own technical recommendation as a 0-10 score.
    Recommend.All: -1.0 (Strong Sell) to +1.0 (Strong Buy)
    → scaled to 0-10.

    This is a FREE bonus signal from TV Screener (L1 enhancement).
    """
    rec = fund.get("Recommend.All")
    if rec is None:
        return 5.0   # neutral
    # Scale from [-1, 1] → [0, 10]
    return round((rec + 1.0) * 5.0, 2)


def get_earnings_proximity_risk(fund: dict) -> float:
    """
    Returns 0.0–1.0 risk factor based on how close the next earnings are.
    0.0 = earnings far away (safe)
    1.0 = earnings within 7 days (high risk)
    """
    ts = fund.get("earnings_release_next_date")
    if not ts:
        return 0.0
    try:
        next_dt  = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        now_dt   = datetime.now(tz=timezone.utc)
        days_out = (next_dt - now_dt).days
        if days_out < 0:
            return 0.0      # past
        if days_out <= 7:
            return 1.0      # imminent risk
        if days_out <= 14:
            return 0.6
        if days_out <= 30:
            return 0.3
        return 0.0
    except Exception:
        return 0.0


def analyst_upside(fund: dict, current_price: float = None) -> float | None:
    """
    Calculate % upside to analyst median price target.
    Returns None if data unavailable.
    """
    target = fund.get("price_target_median")
    price  = current_price or fund.get("current_price")
    if not target or not price or price <= 0:
        return None
    return round((target - price) / price * 100, 1)


def fund_summary_dict(fund: dict) -> dict:
    """
    Build a clean summary dict (for HTML rendering) with proper labels.
    TV units: margins already in %, yields already in %.
    """
    def fmt_pct(v):
        return f"{v:.1f}%" if v is not None else "N/A"

    def fmt_x(v):
        return f"{v:.1f}x" if v is not None else "N/A"

    def fmt_m(v):
        if v is None: return "N/A"
        if abs(v) >= 1e12: return f"${v/1e12:.1f}T"
        if abs(v) >= 1e9:  return f"${v/1e9:.1f}B"
        if abs(v) >= 1e6:  return f"${v/1e6:.1f}M"
        return f"${v:,.0f}"

    next_earn_str = "N/A"
    ts = fund.get("earnings_release_next_date")
    if ts:
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            days_out = (dt - datetime.now(tz=timezone.utc)).days
            next_earn_str = f"{dt.strftime('%b %d, %Y')} ({days_out}d)"
        except Exception:
            pass

    return {
        "Company":        fund.get("description", fund.get("name", "N/A")),
        "Sector":         fund.get("sector", "N/A"),
        "Industry":       fund.get("industry", "N/A"),
        "Market Cap":     fmt_m(fund.get("market_cap_basic")),
        "P/E (TTM)":      fmt_x(fund.get("price_earnings_ttm")),
        "EV/EBITDA":      fmt_x(fund.get("enterprise_value_ebitda_ttm")),
        "ROE":            fmt_pct(fund.get("return_on_equity")),
        "ROIC":           fmt_pct(fund.get("return_on_invested_capital")),
        "Net Margin":     fmt_pct(fund.get("net_margin")),
        "Gross Margin":   fmt_pct(fund.get("gross_margin")),
        "Oper Margin":    fmt_pct(fund.get("operating_margin")),
        "EPS Growth":     fmt_pct(fund.get("earnings_per_share_diluted_yoy_growth_ttm")),
        "D/E Ratio":      fmt_x(fund.get("debt_to_equity")),
        "Current Ratio":  fmt_x(fund.get("current_ratio")),
        "Free Cash Flow": fmt_m(fund.get("free_cash_flow")),
        "Div Yield":      fmt_pct(fund.get("dividends_yield")),
        "Beta 1Y":        f"{fund.get('beta_1_year', 'N/A'):.2f}" if fund.get("beta_1_year") else "N/A",
        "52W High":       f"${fund.get('price_52_week_high', 'N/A'):.2f}" if fund.get("price_52_week_high") else "N/A",
        "52W Low":        f"${fund.get('price_52_week_low', 'N/A'):.2f}" if fund.get("price_52_week_low") else "N/A",
        "Analyst Target": f"${fund.get('price_target_median', 'N/A'):.2f}" if fund.get("price_target_median") else "N/A",
        "TV Rating":      _rec_mark_label(fund.get("recommendation_mark")),
        "TV Rec.All":     f"{fund.get('Recommend.All', 'N/A'):.3f}" if fund.get("Recommend.All") is not None else "N/A",
        "RSI":            f"{fund.get('RSI', 'N/A'):.1f}" if fund.get("RSI") is not None else "N/A",
        "Next Earnings":  next_earn_str,
    }


def _rec_mark_label(v) -> str:
    """Convert recommendation_mark (1=Strong Buy, 5=Strong Sell) to label."""
    if v is None: return "N/A"
    if v <= 1.5:  return "Strong Buy"
    if v <= 2.5:  return "Buy"
    if v <= 3.5:  return "Hold"
    if v <= 4.5:  return "Sell"
    return "Strong Sell"


def tv_fund_to_compat(tv_fund: dict, current_price: float = None, pro_symbol: str = "") -> dict:
    """
    Convert TV Screener field names to a dict that is COMPATIBLE with the
    old yfinance-based `fund` dict format used throughout signal_stack.py,
    sell_actionable.py, and sell_strategy.py.

    Also injects new TV-only fields so callers can use them too.
    TV margin/yield fields are already in %; this function keeps them as-is
    but adds the old yfinance decimal versions under the old keys for backward compat.
    (e.g., `return_on_equity` stays in %; `roe` is set to the same TV value since
    the scoring functions in signal_stack/sell_actionable already normalise.)

    Usage:
        tv_fund_cache = fetch_tv_fundamentals(symbols)
        for sym, tv_fund in tv_fund_cache.items():
            fund = tv_fund_to_compat(tv_fund, current_price=...)
            # Now fund works with existing score_fundamental(), score_fundamental_risk(), etc.
    """
    if not tv_fund:
        # Still return currency (and optionally price) even when TV has no data
        exch     = pro_symbol.split(":")[0].upper() if pro_symbol else ""
        currency = _EXCHANGE_CURRENCY.get(exch, "USD") if exch else None
        base: dict = {}
        if current_price is not None:
            base["current_price"] = current_price
        if currency:
            base["currency"] = currency
        return base

    def g(k):
        v = tv_fund.get(k)
        return v if v is not None else None

    def pick(*vals):
        """First non-None value.

        Aliased compat keys (e.g. ``market_cap`` ← ``market_cap_basic``) must
        not erase a populated fallback value that a caller already placed under
        the compat key name. The Moomoo basicinfo fallback injects
        ``pe_ratio``/``market_cap``/``current_price`` directly into the raw TV
        dict, so a missing TV field previously overwrote them with None
        (review 2026-10-13, Issue 1 follow-up).
        """
        for v in vals:
            if v is not None:
                return v
        return None

    # TV margins are already in % (e.g. net_margin=27.0 means 27%)
    # Old yfinance scoring used decimal (0.27). We pass TV % value directly
    # because the updated score_fundamental() in this module handles % units.
    # For BACKWARD COMPAT with old codebase functions that multiply by 100,
    # we also provide legacy keys with the /100 decimal form.

    compat: dict = {
        # Raw TV fields first so the normalized/aliased keys below always win.
        # This spread previously came LAST and silently overwrote the converted
        # debt_to_equity (×100) with the raw ratio, so the scorer read a
        # leveraged company as low-debt (review 2026-10-13, Issue 1).
        **tv_fund,
        # ── Key name aliases (old name → TV field) ───────────────────────
        "company_name":    tv_fund.get("description") or tv_fund.get("name", ""),
        "company":         tv_fund.get("description") or tv_fund.get("name", ""),
        "sector":          g("sector"),
        "industry":        g("industry"),
        "market_cap":      pick(g("market_cap_basic"), tv_fund.get("market_cap")),
        "currency":        g("currency") or _EXCHANGE_CURRENCY.get(
                               pro_symbol.split(":")[0].upper() if pro_symbol else
                               (tv_fund.get("name", "") or "").split(":")[0].upper(),
                               "USD"
                           ),
        # ↑ TV screener returns 'currency' directly (INR/AUD/HKD…).
        #   If blank, derive from exchange in pro_symbol (NSE→INR, ASX→AUD, etc.)
        "ev_ebitda":       pick(g("enterprise_value_ebitda_ttm"), tv_fund.get("ev_ebitda")),

        # Valuation ratios — template reads these exact keys
        "forward_pe":      pick(g("price_earnings_ttm"), tv_fund.get("forward_pe")),  # TTM P/E (best we have; TV has no forwardPE)
        "pe_ratio":        pick(g("price_earnings_ttm"), tv_fund.get("pe_ratio")),
        "peg_ratio":       pick(g("price_earnings_growth_ttm"), tv_fund.get("peg_ratio")),  # confirmed working for NSE+US

        # Profitability — TV field values are already %
        # Old code: fund.get("roe") expected value like 12.5 (the % already)
        "roe":             pick(g("return_on_equity"), tv_fund.get("roe")),        # TV: already %
        "roa":             pick(g("return_on_assets"), tv_fund.get("roa")),
        "profit_margin":   pick(g("net_margin"), tv_fund.get("profit_margin")),     # TV: already %
        "gross_margin":    pick(g("gross_margin"), tv_fund.get("gross_margin")),   # TV: already %

        # Growth — TV: already %
        # TV exposes no revenue_yoy field on any screener (all attempts → 400).
        # revenue_growth stays None rather than aliasing EPS growth: the alias made
        # every consumer double-count one series and read it as two agreeing signals.
        "revenue_growth":  None,
        "earnings_growth": pick(g("earnings_per_share_diluted_yoy_growth_ttm"),
                                tv_fund.get("earnings_growth")),  # TV: already %

        # Health
        # UNIT NOTE: TV debt_to_equity is the actual ratio (AAPL=1.026)
        # yfinance debtToEquity is ratio × 100 (AAPL=102.63)
        # Multiply by 100 so existing thresholds (val(de, 30, 200)) work correctly
        "debt_to_equity":  g("debt_to_equity") * 100 if g("debt_to_equity") is not None else None,
        "current_ratio":   pick(g("current_ratio"), tv_fund.get("current_ratio")),
        "free_cashflow":   pick(g("free_cash_flow"), g("free_cash_flow_ttm"),
                                tv_fund.get("free_cashflow")),

        # Dividends
        "dividend_yield":  pick(g("dividends_yield"), tv_fund.get("dividend_yield")),  # TV: already %

        # Risk
        "beta":            pick(g("beta_1_year"), tv_fund.get("beta")),

        # Analyst
        "analyst_target":  pick(g("price_target_median"), tv_fund.get("analyst_target")),
        "52w_high":        pick(g("price_52_week_high"), tv_fund.get("52w_high")),
        "52w_low":         pick(g("price_52_week_low"), tv_fund.get("52w_low")),

        # Price — caller-supplied value first, then TV 'close', then any
        # fallback value already present under the compat key.
        "current_price":   pick(current_price, g("close"), tv_fund.get("current_price")),

        # ── NEW TV-only fields (bonus) ────────────────────────────────────
        "return_on_invested_capital": g("return_on_invested_capital"),
        "operating_margin":           g("operating_margin"),
        "earnings_release_next_date": g("earnings_release_next_date"),
        "earnings_release_date":      g("earnings_release_date"),
        "price_target_high":          g("price_target_high"),
        "price_target_low":           g("price_target_low"),
        "recommendation_mark":        g("recommendation_mark"),
        "Recommend.All":              g("Recommend.All"),
        "RSI_tv":                     pick(g("RSI"), tv_fund.get("RSI_tv")),   # TV's RSI (different name to avoid clash)
        "SMA200_tv":                  pick(g("SMA200"), tv_fund.get("SMA200_tv")),
        "ATR_tv":                     pick(g("ATR"), tv_fund.get("ATR_tv")),
        "rel_vol":                    pick(g("relative_volume_10d_calc"), tv_fund.get("rel_vol")),
        "chg_pct":                    pick(g("change"), tv_fund.get("chg_pct")),
        "tv_rec_label":               _rec_mark_label(g("recommendation_mark")),
    }
    return compat


# ─── Sector ETF momentum (SPDR suite) ────────────────────────────────────────
# Canonical sector names → SPDR Select Sector ETF ticker. These are the keys
# returned by fetch_sector_etf_momentum() and stored in _SECTOR_ETF_DETAIL.
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Healthcare":             "XLV",
    "Energy":                 "XLE",
    "Basic Materials":        "XLB",
    "Industrials":            "XLI",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
}

# TradingView's "sector" field uses its own taxonomy (e.g. "Technology Services",
# "Electronic Technology", "Finance", "Health Technology") which does NOT match
# the canonical SPDR names above. This alias table maps every known TV sector
# name — plus legacy/yfinance-style names — to a canonical sector. Anything not
# listed resolves to None so unknown sectors never receive an arbitrary ETF.
_SECTOR_ALIASES: dict[str, str] = {
    # ── TradingView taxonomy ──────────────────────────────────────────────
    "technology services":     "Technology",
    "electronic technology":   "Technology",
    "finance":                 "Financial Services",
    "health technology":       "Healthcare",
    "health services":         "Healthcare",
    "energy minerals":         "Energy",
    "non-energy minerals":     "Basic Materials",
    "process industries":      "Basic Materials",
    "producer manufacturing":  "Industrials",
    "industrial services":     "Industrials",
    "commercial services":     "Industrials",
    "transportation":          "Industrials",
    "consumer durables":       "Consumer Cyclical",
    "consumer services":       "Consumer Cyclical",
    "retail trade":            "Consumer Cyclical",
    "distribution services":   "Consumer Cyclical",
    "consumer non-durables":   "Consumer Defensive",
    "communications":          "Communication Services",
    # ── Legacy / yfinance-style names ─────────────────────────────────────
    "technology":              "Technology",
    "financial services":      "Financial Services",
    "healthcare":              "Healthcare",
    "energy":                  "Energy",
    "basic materials":         "Basic Materials",
    "industrials":             "Industrials",
    "consumer cyclical":       "Consumer Cyclical",
    "consumer defensive":      "Consumer Defensive",
    "utilities":               "Utilities",
    "real estate":             "Real Estate",
    "communication services":  "Communication Services",
}


def normalize_sector(sector) -> str | None:
    """Normalize a TradingView/yfinance sector name to a canonical SPDR sector.

    Returns a key of ``SECTOR_ETF_MAP`` (e.g. "Technology") or ``None`` when the
    sector is missing, empty, or unrecognized. Never returns an arbitrary ETF.
    """
    if not sector or not isinstance(sector, str):
        return None
    key = sector.strip().lower()
    if not key:
        return None
    if key in _SECTOR_ALIASES:
        return _SECTOR_ALIASES[key]
    for canon in SECTOR_ETF_MAP:
        if canon.lower() == key:
            return canon
    return None


def sector_etf_ticker(sector) -> str | None:
    """Return the SPDR ETF ticker for *sector*, or None when unknown."""
    canon = normalize_sector(sector)
    return SECTOR_ETF_MAP.get(canon) if canon else None


_SECTOR_ETF_CACHE: dict = {"data": None, "ts": 0.0}
_SECTOR_ETF_DETAIL: dict[str, dict] = {}   # {canonical_sector: {ticker, pct}} updated by fetch_sector_etf_momentum


def get_sector_etf_detail(sector: str) -> dict:
    """Return {ticker, pct, score, roc} for the SPDR sector ETF mapped to *sector*.

    ``sector`` may be a raw TradingView sector name; it is normalized first so
    the displayed ETF always matches the ETF used for scoring. ``pct`` is the
    ETF's percentage above/below its 200-day SMA (positive = above). Returns an
    empty dict when the sector is unknown or the fetch has not yet run.
    """
    canon = normalize_sector(sector)
    if canon is None:
        return {}
    return _SECTOR_ETF_DETAIL.get(canon, {})


def get_sector_etf_score(sector: str):
    """Return the cached sector ETF momentum score for *sector*, or None.

    Uses the same normalization as :func:`get_sector_etf_detail` so the score
    lookup and the displayed ETF detail always agree.
    """
    canon = normalize_sector(sector)
    if canon is None:
        return None
    data = _SECTOR_ETF_CACHE.get("data") or {}
    return data.get(canon)

def fetch_sector_etf_momentum() -> dict[str, float]:
    """
    Fetch SMA-200 momentum scores for the 11 SPDR sector ETFs via TV america scanner.
    Cached for 30 minutes — sector ETF momentum is market-wide context, not per-symbol.

    Returns {sector_name: score_0_to_10} where sector_name matches TV's "sector" field.
    Non-US symbols (NSE, ASX) won't match and naturally fall back to 5.0 (neutral)
    in score_market_internals().

    Scoring (based on distance from SMA-200):
        > +10%   → 9.0  (strong sector bull trend)
        +3–10%   → 7.5  (healthy above-SMA momentum)
        0–+3%    → 6.0  (just above SMA — neutral-positive)
        -5–0%    → 4.0  (below SMA — mild caution)
        -15–-5%  → 2.5  (sector in downtrend)
        < -15%   → 1.0  (sector in deep downtrend)
    """
    import time as _time
    now = _time.time()
    if _SECTOR_ETF_CACHE["ts"] > 0 and (now - _SECTOR_ETF_CACHE["ts"]) < 1800:
        return _SECTOR_ETF_CACHE["data"]   # type: ignore[return-value]

    url     = "https://scanner.tradingview.com/america/scan"
    tickers = list(SECTOR_ETF_MAP.values())          # ["XLK", "XLF", ..., "XLC"]
    columns = ["name", "close", "SMA200", "SMA5"]
    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/",
    }
    if _TV_COOKIE:
        headers["Cookie"] = _TV_COOKIE

    payload = {
        "columns": columns,
        "filter":  [{"left": "name", "operation": "in_range", "right": tickers}],
        "range":   [0, len(tickers) + 5],
    }

    def _score_sma200(close: float | None, sma: float | None) -> float:
        if close is None or sma is None or sma <= 0:
            return 5.0   # neutral when data unavailable
        pct = (close - sma) / sma * 100.0
        if pct > 10:    return 9.0
        if pct > 3:     return 7.5
        if pct >= 0:    return 6.0
        if pct >= -5:   return 4.0
        if pct >= -15:  return 2.5
        return 1.0

    def _roc_bonus(close: float | None, sma5: float | None) -> float:
        """Rate-of-change bonus from 5-day momentum (detects sector rotation early)."""
        if close is None or sma5 is None or sma5 <= 0:
            return 0.0   # backward compatible: no change when SMA5 unavailable
        roc = (close / sma5 - 1.0) * 100.0
        if roc >  3.0:  return  1.5
        if roc >  1.5:  return  0.8
        if roc > -1.5:  return  0.0
        if roc > -3.0:  return -0.8
        return -1.5

    # Build reverse map: ETF ticker → sector name
    etf_to_sector = {v: k for k, v in SECTOR_ETF_MAP.items()}
    scores: dict[str, float] = {}

    try:
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(url, data=body, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        for item in data.get("data", []):
            raw_sym = item.get("s", "")                  # e.g. "AMEX:XLK"
            d       = item.get("d", [])
            ticker  = raw_sym.split(":")[-1] if ":" in raw_sym else raw_sym
            # d[0]=name, d[1]=close, d[2]=SMA200, d[3]=SMA5
            close  = d[1] if len(d) > 1 else None
            sma200 = d[2] if len(d) > 2 else None
            sma5   = d[3] if len(d) > 3 else None
            sector = etf_to_sector.get(ticker)
            if sector:
                base_score = _score_sma200(close, sma200)
                bonus      = _roc_bonus(close, sma5)
                s = round(max(1.0, min(10.0, base_score + bonus)), 2)
                scores[sector] = s
                # Store detail for display in signal stack card
                _pct = round((close - sma200) / sma200 * 100.0, 1) if (close and sma200 and sma200 > 0) else None
                _roc = round((close / sma5 - 1.0) * 100.0, 2) if (close and sma5 and sma5 > 0) else None
                _SECTOR_ETF_DETAIL[sector] = {"ticker": ticker, "pct": _pct, "score": s, "roc": _roc}
    except Exception:
        pass   # return whatever partial results we have; score_market_internals handles None

    _SECTOR_ETF_CACHE.update({"data": scores, "ts": now})
    return scores


# ─── Yield curve fetch (2s10s spread via TV economics scan) ──────────────────

def fetch_yield_curve() -> dict:
    """
    Fetch US Treasury 2Y and 10Y yields from TV global screener (TVC symbols).
    Returns {us2y, us10y, spread_bps} where spread_bps = (10Y - 2Y) * 100.
    Positive spread = normal curve.  Negative = inverted = recession risk.
    No auth needed.  Single fast HTTP call.
    """
    try:
        payload = {
            "columns": ["close"],
            "symbols": {"tickers": ["TVC:US02Y", "TVC:US10Y"]},
            "range": [0, 2],
        }
        req = urllib.request.Request(
            "https://scanner.tradingview.com/global/scan",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=8).read())
        yields = {item["s"]: item["d"][0] for item in data.get("data", []) if item["d"][0] is not None}
        us2y  = yields.get("TVC:US02Y")
        us10y = yields.get("TVC:US10Y")
        if us2y is not None and us10y is not None:
            spread_bps = round((us10y - us2y) * 100, 1)
            return {"us2y": round(us2y, 3), "us10y": round(us10y, 3), "spread_bps": spread_bps}
    except Exception:
        pass
    return {"us2y": None, "us10y": None, "spread_bps": None}


# ─── VIX market-regime level ─────────────────────────────────────────────────
_VIX_CACHE: dict = {"val": None, "ts": 0.0}

def fetch_vix_level() -> float | None:
    """
    Fetch current CBOE VIX from yfinance.  Cached for 30 minutes — VIX is
    market-wide, not per-symbol, so one fetch covers the whole batch run.

    Regime guide (for score_market_internals):
        < 15  → extreme calm   (strong bull backdrop)
        15–20 → calm / normal  (favourable)
        20–25 → slightly elevated (neutral-caution)
        25–30 → elevated fear  (reduce exposure)
        30–40 → high fear      (risk-off)
        > 40  → crisis         (avoid new longs)
    """
    import time as _time
    now = _time.time()
    if _VIX_CACHE["ts"] > 0 and (now - _VIX_CACHE["ts"]) < 1800:
        return _VIX_CACHE["val"]
    try:
        import yfinance as yf
        vix_price = yf.Ticker("^VIX").fast_info.get("lastPrice")
        _VIX_CACHE.update({
            "val": float(vix_price) if vix_price is not None else None,
            "ts":  now,
        })
        return _VIX_CACHE["val"]
    except Exception:
        return None


# ─── VIX3M / VIX term structure ratio ────────────────────────────────────────
_VIX3M_CACHE: dict = {"val": None, "ts": 0.0}

def fetch_vix3m_level() -> float | None:
    """
    Fetch current CBOE 3-month VIX (^VIX3M) from yfinance.  Cached for 30
    minutes — same rationale as fetch_vix_level(): market-wide, not per-symbol.

    Combine with fetch_vix_level() to get the VIX3M/VIX term-structure ratio:
        > 1.0 → contango      (normal, calm markets)
        < 1.0 → backwardation (near-term fear exceeds long-term — often a
                                leading indicator of sell-offs)
    """
    import time as _time
    now = _time.time()
    if _VIX3M_CACHE["ts"] > 0 and (now - _VIX3M_CACHE["ts"]) < 1800:
        return _VIX3M_CACHE["val"]
    try:
        import yfinance as yf
        vix3m_price = yf.Ticker("^VIX3M").fast_info.get("lastPrice")
        _VIX3M_CACHE.update({
            "val": float(vix3m_price) if vix3m_price is not None else None,
            "ts":  now,
        })
        return _VIX3M_CACHE["val"]
    except Exception:
        return None


# ─── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    TEST_SYMS = [
        "NYSE:NVDA", "NASDAQ:AAPL", "NYSE:BA",
        "NYSE:SOXL", "NYSE:MSFU",              # leveraged ETFs
        "ASX:CBA", "ASX:MND", "ASX:APE",       # ASX — yfinance 404
    ]

    print("Fetching fundamentals from TV Screener (batch, 1 call per exchange)...")
    t0   = time.time()
    data = fetch_tv_fundamentals(TEST_SYMS, use_auth=True)
    elapsed = time.time() - t0
    print(f"Fetched {len(data)} symbols in {elapsed:.2f}s\n")

    for sym, fund in data.items():
        score = score_fundamental(fund)
        earn_risk  = get_earnings_proximity_risk(fund)
        tv_rec     = get_tv_recommendation_score(fund)
        summary    = fund_summary_dict(fund)

        print(f"{'='*60}")
        print(f"  {sym}  —  Fundamental Score: {score['total']:.1f}/10")
        print(f"{'='*60}")
        for k, v in summary.items():
            if v not in ("N/A", None):
                print(f"    {k:<20} {v}")
        print(f"    {'Score breakdown':<20} Val={score['valuation']:.1f}  Prof={score['profitability']:.1f}  "
              f"Growth={score['growth']:.1f}  Health={score['health']:.1f}")
        print(f"    {'TV rec score':<20} {tv_rec:.1f}/10  (Recommend.All={fund.get('Recommend.All', 'N/A')})")
        print(f"    {'Earnings risk':<20} {earn_risk:.1f}")
        print()
