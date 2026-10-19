"""
NSE Options Fallback — Black-Scholes Greeks via yfinance historical volatility
==============================================================================
Used when AngelOne optionGreeks endpoint returns AB9019 "No Data Available"
(off-market hours) or when Angel One auth fails entirely.

Two-tier approach:
  Tier 2: NSE Option Chain API (nseindia.com) — returns real IV, OI, volume,
          LTP per strike. Requires Playwright to pass Akamai bot-protection.
          If Playwright is not installed or the request fails, skip to Tier 3.

  Tier 3: Pure Black-Scholes from yfinance historical volatility.
          Always available. Returns synthetic contracts with theoretical Greeks.
          Marked with source="black_scholes".

Black-Scholes inputs:
  S     — spot price (yfinance intraday or last close)
  K     — strike prices (ATM ± strikes_around_atm)
  T     — DTE / 365  (calendar days)
  r     — 0.065  (RBI repo rate; overrideable via BS_RISK_FREE_RATE env var)
  sigma — annualised historical vol (60-day log-return std * √252)

Greeks formulae:
  d1    = (ln(S/K) + (r + σ²/2) * T) / (σ * √T)
  d2    = d1 - σ * √T
  delta = N(d1)                        [CE]  | N(d1) - 1  [PE]
  gamma = N'(d1) / (S * σ * √T)
  theta = -(S*N'(d1)*σ/(2√T) + r*K*e^(-rT)*N(d2)) / 365   [CE]
  vega  = S * N'(d1) * √T / 100
  price = S*N(d1) - K*e^(-rT)*N(d2)   [CE]  | K*e^(-rT)*N(-d2) - S*N(-d1)  [PE]
"""

from __future__ import annotations

import math
import os
import time
from datetime import date, timedelta
from typing import Optional

_RISK_FREE_RATE = float(os.environ.get("BS_RISK_FREE_RATE", "0.065"))
_HV_CACHE: dict[str, tuple[float, float]] = {}   # ticker -> (sigma, ts)
_HV_TTL   = 3600   # 1 hour cache TTL for historical vol


# ─── Black-Scholes core ───────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_greeks(
    S: float,
    K: float,
    T: float,          # years (DTE / 365)
    r: float,
    sigma: float,
    option_type: str = "CE",
) -> dict[str, float]:
    """
    Return Black-Scholes price + Greeks for a European option.

    Returns dict with keys: price, delta, gamma, theta, vega.
    All values 0.0 on degenerate inputs.
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return {"price": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if option_type.upper() == "CE":
        price = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta = (
            -(S * _norm_pdf(d1) * sigma / (2 * sqrt_T))
            - r * K * math.exp(-r * T) * _norm_cdf(d2)
        ) / 365
    else:   # PE
        price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1
        theta = (
            -(S * _norm_pdf(d1) * sigma / (2 * sqrt_T))
            + r * K * math.exp(-r * T) * _norm_cdf(-d2)
        ) / 365

    gamma = _norm_pdf(d1) / (S * sigma * sqrt_T)
    vega  = S * _norm_pdf(d1) * sqrt_T / 100   # per 1% change in vol

    return {
        "price": round(price, 4),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega, 4),
    }


# ─── Historical volatility from yfinance ────────────────────────────────────

def _historical_vol_yf(yf_ticker: str, days: int = 60) -> float:
    """
    Annualised historical volatility from yfinance close prices.

    Uses 60-day log-return std × √252.
    Returns 0.0 on any error.
    """
    cached = _HV_CACHE.get(yf_ticker)
    if cached and (time.time() - cached[1]) < _HV_TTL:
        return cached[0]
    try:
        import yfinance as yf   # type: ignore[import]
        ticker  = yf.Ticker(yf_ticker)
        hist    = ticker.history(period="60d")
        closes  = hist["Close"].dropna()
        if len(closes) < 10:
            return 0.0
        log_ret = closes.pct_change().dropna().apply(lambda x: math.log(1 + x))
        sigma   = float(log_ret.std() * math.sqrt(252))
        _HV_CACHE[yf_ticker] = (sigma, time.time())
        return max(sigma, 0.01)   # floor at 1%
    except Exception:
        return 0.0


def _spot_price_yf(yf_ticker: str) -> float:
    """Get the latest close (or last known) price from yfinance."""
    try:
        import math as _math
        import yfinance as yf   # type: ignore[import]
        t = yf.Ticker(yf_ticker)
        hist = t.history(period="5d")
        if not hist.empty:
            closes = hist["Close"].dropna()
            if not closes.empty:
                val = float(closes.iloc[-1])
                if not _math.isnan(val) and val > 0:
                    return val
    except Exception:
        pass
    return 0.0


# ─── Strike generation ───────────────────────────────────────────────────────

def _atm_strikes(spot: float, n: int = 8, step_pct: float = 0.01) -> list[float]:
    """
    Generate n strike prices on each side of ATM, rounded to nearest round number.

    Step is 1% of spot by default, rounded to a "clean" value.
    """
    if spot <= 0:
        return []

    # Determine a clean step: e.g. for INFY@1500 → step=20, for NIFTY@22000 → step=250
    raw_step = spot * step_pct
    # Round to nearest "order of magnitude" step
    magnitude = 10 ** math.floor(math.log10(raw_step))
    clean_step = round(raw_step / magnitude) * magnitude
    clean_step = max(clean_step, 1.0)

    atm = round(spot / clean_step) * clean_step
    return sorted({
        round(atm + i * clean_step, 2)
        for i in range(-n, n + 1)
    })


# ─── NSE Option Chain via requests (Tier 2 — may be Akamai-blocked) ──────────

class _NseOcRequester:
    """Lightweight NSE OC requester using requests + header warm-up.

    NSE protects its API with Akamai Bot Manager. A plain requests session
    usually fails after the first one or two calls because the _abck / bm_sz
    cookies become stale. Tier 2 is a best-effort attempt; Tier 3 (BS) is the
    reliable fallback.
    """

    _HEADERS = {
        "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-US,en;q=0.9",
        "Referer":          "https://www.nseindia.com/option-chain",
        "X-Requested-With": "XMLHttpRequest",
    }
    _session: "requests.Session | None" = None   # type: ignore[name-defined]
    _last_init: float = 0.0
    _INIT_TTL  = 1800   # 30-minute session TTL

    @classmethod
    def _ensure_session(cls) -> "requests.Session | None":   # type: ignore[name-defined]
        if cls._session and (time.time() - cls._last_init) < cls._INIT_TTL:
            return cls._session
        try:
            import requests as _req   # type: ignore[import]
            s = _req.Session()
            s.headers.update(cls._HEADERS)
            # Warm-up: fetch derivatives page to get valid NSE cookies
            r = s.get("https://www.nseindia.com/market-data/equity-derivatives-watch",
                       timeout=8)
            if r.status_code not in (200, 302):
                return None
            cls._session   = s
            cls._last_init = time.time()
            return s
        except Exception:
            return None

    @classmethod
    def invalidate(cls) -> None:
        cls._session   = None
        cls._last_init = 0.0

    @classmethod
    def fetch_oc(cls, symbol: str) -> dict:
        """Fetch option chain for `symbol` (e.g. 'INFY'). Returns raw JSON dict or {}."""
        s = cls._ensure_session()
        if s is None:
            return {}
        try:
            url = f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
            r   = s.get(url, timeout=10)
            if r.status_code != 200 or len(r.content) < 50:
                return {}
            data = r.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _parse_nse_oc(
    oc_data: dict,
    base_symbol: str,
    expiry_date: date,
    lot_size: int,
    strikes_around_atm: int = 8,
) -> list[dict]:
    """
    Parse NSE Option Chain API response into standard contract dicts.

    NSE OC format:
      records.underlyingValue
      records.data[].CE / .PE with fields:
        strikePrice, impliedVolatility, lastPrice, openInterest,
        bidPrice, askPrice, changeinOpenInterest, totalBuyQuantity, totalSellQuantity
    """
    records = (oc_data.get("records") or oc_data.get("filtered") or {})
    spot    = float(records.get("underlyingValue") or 0)
    expiry_fmt_nse = expiry_date.strftime("%d-%b-%Y").upper()   # 29-APR-2026

    rows = records.get("data") or []
    dte  = (expiry_date - date.today()).days
    if dte <= 0:
        return []
    T = dte / 365.0

    all_contracts: list[dict] = []
    r = _RISK_FREE_RATE

    for row in rows:
        if not isinstance(row, dict):
            continue
        ce = row.get("CE")
        if not isinstance(ce, dict):
            continue
        # Filter by expiry
        row_expiry = (ce.get("expiryDate") or "").upper()
        if expiry_fmt_nse not in row_expiry and expiry_date.strftime("%Y-%m-%d") not in row_expiry:
            continue

        strike = float(ce.get("strikePrice") or 0)
        if strike <= 0:
            continue

        iv_pct  = float(ce.get("impliedVolatility") or 0)  # NSE gives IV as percent (e.g. 22.5)
        sigma   = iv_pct / 100.0 if iv_pct > 0 else 0.0
        ltp     = float(ce.get("lastPrice") or 0)
        bid     = float(ce.get("bidPrice") or 0)
        ask     = float(ce.get("askPrice") or 0)
        oi      = int(float(ce.get("openInterest") or 0))
        volume  = int(float(
            ce.get("totalBuyQuantity") or ce.get("totalSellQuantity") or 0
        ))

        # Compute Greeks via Black-Scholes using NSE IV
        if sigma > 0 and spot > 0:
            greeks = bs_greeks(spot, strike, T, r, sigma)
        else:
            greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "price": ltp}

        mid_price  = (bid + ask) / 2 if bid > 0 and ask > 0 else ltp
        spread_pct = ((ask - bid) / mid_price * 100) if mid_price > 0 else 0.0
        moneyness  = ((strike - spot) / spot * 100) if spot > 0 else 0.0

        from .india_options import _expiry_str
        all_contracts.append({
            "tradingsymbol": f"{base_symbol}{_expiry_str(expiry_date)}CE{int(strike)}",
            "strike":        strike,
            "expiry":        _expiry_str(expiry_date),
            "option_type":   "CE",
            "bid":           bid,
            "ask":           ask,
            "last_price":    ltp,
            "mid_price":     mid_price,
            "volume":        volume,
            "oi":            oi,
            "iv":            sigma if sigma > 0 else None,
            "delta":         greeks["delta"],
            "gamma":         greeks["gamma"],
            "theta":         greeks["theta"],
            "vega":          greeks["vega"],
            "dte":           dte,
            "spread_pct":    spread_pct,
            "moneyness_pct": moneyness,
            "spot_price":    spot,
            "lot_size":      lot_size,
            "lot_cost_inr":  round(mid_price * lot_size, 2),
            "market":        "NSE",
            "source":        "nse_oc",
        })

    # Narrow to ±strikes_around_atm from ATM
    if spot > 0 and all_contracts:
        strikes = sorted({c["strike"] for c in all_contracts})
        atm = min(strikes, key=lambda s: abs(s - spot))
        try:
            idx = strikes.index(atm)
        except ValueError:
            return all_contracts
        lo, hi = max(0, idx - strikes_around_atm), min(len(strikes) - 1, idx + strikes_around_atm)
        valid = set(strikes[lo: hi + 1])
        all_contracts = [c for c in all_contracts if c["strike"] in valid]

    all_contracts.sort(key=lambda c: c["strike"])
    return all_contracts


# ─── Tier 3: Pure Black-Scholes fallback ─────────────────────────────────────

def get_bs_contracts(
    base_symbol: str,
    yf_ticker: str,
    expiry_dates: "list[date]",
    lot_size: int,
    strikes_around_atm: int = 8,
    verbose: bool = False,
) -> list[dict]:
    """
    Generate synthetic option contracts using Black-Scholes + yfinance historical vol.

    Returns contracts with source="black_scholes".
    iv = annualised historical vol (not per-strike IV from market).
    OI and volume = 0 (marks after_hours=True in options_analyzer.py).
    """
    import math as _math
    spot  = _spot_price_yf(yf_ticker)
    sigma = _historical_vol_yf(yf_ticker)

    if spot <= 0 or _math.isnan(spot) or sigma <= 0 or _math.isnan(sigma):
        if verbose:
            print(f"[BS] {base_symbol}: spot={spot}, sigma={sigma} — cannot generate contracts")
        return []

    if verbose:
        print(f"[BS] {base_symbol}: spot={spot:.2f}, HV={sigma*100:.1f}%, yf={yf_ticker}")

    r = _RISK_FREE_RATE
    strikes = _atm_strikes(spot, n=strikes_around_atm)

    from .india_options import _expiry_str
    all_contracts: list[dict] = []

    for expiry_date in expiry_dates:
        dte = (expiry_date - date.today()).days
        if dte <= 0:
            continue
        T = dte / 365.0

        for K in strikes:
            greeks = bs_greeks(spot, K, T, r, sigma, option_type="CE")
            if greeks["delta"] < 0.02 or greeks["delta"] > 0.98:
                continue   # deep OTM / deep ITM — skip
            price = greeks["price"]
            if price <= 0:
                continue

            # Synthetic bid/ask with 1% spread
            bid = round(price * 0.99, 2)
            ask = round(price * 1.01, 2)
            moneyness = (K - spot) / spot * 100

            all_contracts.append({
                "tradingsymbol": f"{base_symbol}{_expiry_str(expiry_date)}CE{int(K)}",
                "strike":        K,
                "expiry":        _expiry_str(expiry_date),
                "option_type":   "CE",
                "bid":           bid,
                "ask":           ask,
                "last_price":    price,
                "mid_price":     price,
                "volume":        0,
                "oi":            0,
                "iv":            round(sigma, 4),
                "delta":         greeks["delta"],
                "gamma":         greeks["gamma"],
                "theta":         greeks["theta"],
                "vega":          greeks["vega"],
                "dte":           dte,
                "spread_pct":    round((ask - bid) / price * 100, 2),
                "moneyness_pct": round(moneyness, 2),
                "spot_price":    spot,
                "lot_size":      lot_size,
                "lot_cost_inr":  round(price * lot_size, 2),
                "market":        "NSE",
                "source":        "black_scholes",
            })

    all_contracts.sort(key=lambda c: (c["dte"], c["strike"]))
    return all_contracts


# ─── Public entrypoint (Tier 2 → Tier 3) ────────────────────────────────────

def get_nse_fallback_contracts(
    base_symbol: str,
    yf_ticker: str,
    expiry_dates: "list[date]",
    lot_size: int,
    strikes_around_atm: int = 8,
    verbose: bool = False,
) -> list[dict]:
    """
    Attempt Tier 2 (NSE OC) then fall back to Tier 3 (Black-Scholes).

    Returns a list of contract dicts in the standard india_options format.
    Contracts always have a "source" key: "nse_oc" or "black_scholes".
    """
    # Tier 2: NSE Option Chain API
    all_tier2: list[dict] = []
    try:
        nse_data = _NseOcRequester.fetch_oc(base_symbol)
        if nse_data:
            for expiry_date in expiry_dates:
                contracts = _parse_nse_oc(
                    nse_data, base_symbol, expiry_date,
                    lot_size, strikes_around_atm,
                )
                all_tier2.extend(contracts)
            if all_tier2:
                if verbose:
                    print(f"[Tier2/NSE-OC] {base_symbol}: {len(all_tier2)} contracts")
                return all_tier2
    except Exception as exc:
        if verbose:
            print(f"[Tier2/NSE-OC] {base_symbol}: {exc}")

    # Tier 3: Black-Scholes via yfinance
    contracts = get_bs_contracts(
        base_symbol, yf_ticker, expiry_dates, lot_size,
        strikes_around_atm=strikes_around_atm, verbose=verbose,
    )
    if verbose and contracts:
        print(f"[Tier3/BS] {base_symbol}: {len(contracts)} synthetic contracts")
    return contracts
