"""
Hybrid Options Fetcher — Three-Source Data Integration
=======================================================
Merges live data from three independent sources:

  1. TradingView scan2 REST  → primary: IV, Greeks, theoPrice, bid_iv/ask_iv
  2. Moomoo OpenD            → OI only  (option_open_interest, offline-tolerant)
  3. TradingView WebSocket   → live: OHLC, live last_price, bid_size/ask_size

Graceful degradation:
  - Moomoo offline → oi=0 kept from TV, source tagged "unavailable"
  - WebSocket closed/market closed → neutral TQS_v2 sub-scores used
  - TV cookie expired → raises (unrecoverable; all data from TV)

Merge keys:
  TV → Moomoo:    (round(strike,2), expiry_str, "CALL") — different code formats
  TV → WebSocket: OPRA code ("OPRA:NVDA260821C230.0")  — same code in both

TQS_v2 fields computed from hybrid data:
  iv_spread      = abs(ask_iv - bid_iv) in percentage points (from TV)
  ohlc_range_pct = (lp - low) / (high - low) × 100 (from WebSocket)
  pressure       = (bid_size - ask_size) / (bid_size + ask_size) (from WebSocket)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random
import re
import string
import sys
import time
from pathlib import Path
from typing import Optional, TypedDict


class OptionContractData(TypedDict, total=False):
    """Typed contract record returned by get_calls(), get_puts(), batch_enrich_ws().

    Fields from three sources (see module docstring for merge strategy):
      TV scan2 source: symbol, strike, expiry, bid_iv, ask_iv, iv, theoPrice, delta,
                        gamma, theta, vega, openInterest, volume, type
      Moomoo source:   option_open_interest (real OI, lobby 24/7)
      WS source:       last_price, open_price, high_price, low_price, bid_size, ask_size
      Computed TQS_v2: iv_spread, ohlc_range_pct, pressure, tqs_score, tqs_v2_score
    """
    # Core identification
    symbol:      str    # OPRA code, e.g. "OPRA:NVDA260515C200.0"
    type:        str    # "call" or "put"
    strike:      float
    expiry:      int    # YYYYMMDD integer
    # Pricing
    bid:         float
    ask:         float
    last_price:  float
    theoPrice:   float
    # IV
    iv:          float
    bid_iv:      float
    ask_iv:      float
    iv_spread:   float  # abs(ask_iv - bid_iv) in pp (computed)
    # Greeks
    delta:       float
    gamma:       float
    theta:       float
    vega:        float
    # Size / volume
    openInterest:       int
    option_open_interest: int  # Moomoo real OI
    volume:             int
    bid_size:           int
    ask_size:           int
    # OHLC (from WS)
    open_price:  float
    high_price:  float
    low_price:   float
    # Computed TQS_v2 sub-scores
    ohlc_range_pct: float
    pressure:        float
    tqs_score:       float
    tqs_v2_score:    float
    # Metadata
    data_source:     str   # "tv+moomoo+ws", "tv+moomoo", "tv+ws", "tv"


if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE     = Path(__file__).parent.resolve()   # tradingview/options/
_TV_DIR   = _HERE.parent.resolve()            # tradingview/
_ROOT_DIR = _TV_DIR.parent.resolve()          # project root
_COOKIE_FILE = _TV_DIR / "cookie"

# ── Market hours ─────────────────────────────────────────────────────────────

def _is_us_market_hours() -> bool:
    """Return True if US regular trading hours are active (09:30–16:00 ET Mon-Fri)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if now_utc.weekday() >= 5:       # Sat/Sun
        return False
    h_utc = now_utc.hour + now_utc.minute / 60.0
    # 09:30 ET (EDT UTC-4) = 13:30 UTC; 16:00 ET = 20:00 UTC
    return 13.5 <= h_utc <= 20.0


# ── Expiry normalization ──────────────────────────────────────────────────────

def _norm_expiry(exp) -> int:
    """
    Normalize any expiry representation to integer YYYYMMDD.

    TV scan2 returns integers (20260515).
    Moomoo returns strings ("2026-05-18") or datetime.date objects.

    US options may differ by 1-3 days between sources because Moomoo uses
    settlement date (T+1 or T+3 for weeklies) while TV uses expiry date.
    """
    if isinstance(exp, int) and exp > 10000000:
        return exp
    if isinstance(exp, datetime.date):
        return int(exp.strftime("%Y%m%d"))
    if isinstance(exp, str) and exp:
        return int(exp.replace("-", ""))
    return 0


def _expiry_match(tv_exp: int, mm_exp: int, tolerance_days: int = 3) -> bool:
    """
    True if |tv_exp - mm_exp| ≤ tolerance_days in calendar days.

    Handles the settlement-date offset: Moomoo returns settlement (Mon)
    for weekly options that expire on Friday → 3-day difference.
    """
    if tv_exp == mm_exp:
        return True
    try:
        tv_d = datetime.date(tv_exp // 10000, (tv_exp % 10000) // 100, tv_exp % 100)
        mm_d = datetime.date(mm_exp // 10000, (mm_exp % 10000) // 100, mm_exp % 100)
        return abs((mm_d - tv_d).days) <= tolerance_days
    except (ValueError, ZeroDivisionError):
        return False


# ── TQS v2 sub-score helpers ─────────────────────────────────────────────────

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _ivspd_s(iv_spread: float) -> float:
    """IV spread score: smaller IV spread → higher score (0–10)."""
    return _clamp(10.0 * (1.0 - iv_spread / 10.0), 0.0, 10.0)


def _ohlc_s(ohlc_range_pct: float) -> float:
    """OHLC position score: where in the day's range is the current price? (2–10)."""
    return _clamp(2.0 + ohlc_range_pct / 100.0 * 8.0, 2.0, 10.0)


def _pressure_s(pressure: float) -> float:
    """Order-pressure score: bid pressure → higher score (0–10)."""
    return _clamp(5.0 + pressure * 5.0, 0.0, 10.0)


def inject_screener_subscores(c: dict) -> None:
    """Compute and inject liq_score, spd_s, oi_s, vol_s into contract dict *c* in-place.

    Weekend spread fix (Behaviour 1):
    When bid=ask=0 the market is closed and spread_pct=0.0 is meaningless
    (spread appears 0% but there is no real quote).  Apply OI-based rule:
        oi > 0  → spd_s = 5.0  (neutral — OI proves the contract is real)
        oi == 0 → spd_s = 0.0  (penalty — no proof of liquidity)

    liq_score weights: spd_s × 0.35 + oi_s × 0.40 + vol_s × 0.25
    """
    oi  = c.get("oi",  0) or 0
    bid = c.get("bid", None)
    ask = c.get("ask", None)

    # ── Spread score (with weekend fix) ──────────────────────────────────
    if bid == 0 and ask == 0:
        # Market closed: spread_pct=0.0 is not a valid tightness signal
        spd_s = 5.0 if oi > 0 else 0.0
    else:
        spread_pct = c.get("spread_pct") or 999.0
        if spread_pct > 100:
            spd_s = 0.0
        elif spread_pct > 10:
            spd_s = _clamp(10.0 * (1.0 - (spread_pct - 10.0) / 90.0), 0.0, 5.0)
        else:
            spd_s = _clamp(10.0 * (1.0 - spread_pct / 10.0), 5.0, 10.0)

    # ── OI score (display only — NOT used for filtering/scoring decisions)
    oi_s = 5.0  # neutral: OI is unreliable across sources, kept for display only

    # ── Volume score ─────────────────────────────────────────────────────
    vol = c.get("volume", 0) or 0
    if vol >= 10_000:
        vol_s = 10.0
    elif vol >= 1_000:
        vol_s = 6.0 + (vol - 1_000) / 9_000.0 * 4.0
    elif vol >= 100:
        vol_s = 2.0 + (vol - 100) / 900.0 * 4.0
    elif vol > 0:
        vol_s = vol / 100.0 * 2.0
    else:
        vol_s = 0.0

    # ── Inject subscores ─────────────────────────────────────────────────
    c["spd_s"]     = round(spd_s, 4)
    c["oi_s"]      = round(oi_s,  4)  # neutral 5.0 — display only
    c["vol_s"]     = round(vol_s, 4)
    c["liq_score"] = round(
        _clamp(spd_s * 0.55 + vol_s * 0.45, 0.0, 10.0), 4
    )  # OI removed from liq_score — only spread + volume matter


def compute_tqs_v2(
    c: dict,
    iv_spread: Optional[float] = None,
    ohlc_range_pct: Optional[float] = None,
    pressure: Optional[float] = None,
) -> float:
    """
    Comprehensive Trade Quality Score (0–100).

    PRIMARY path — uses pre-computed sub-scores from options_analyzer (preferred):
      liq_score    (0–10)  · weight 0.30
      iv_score     (0–10)  · weight 0.25
      strike_score (0–10)  · weight 0.25
      dte_score    (0–10)  · weight 0.20
    Plus ±8-pt hybrid boost from iv_spread tightness and buying pressure.

    FALLBACK path — no sub-scores available, uses raw liquidity fields only:
      spread 0.20 | OI 0.35 | volume 0.15 | IV-spread 0.15 | pressure 0.15
    Missing WebSocket or Moomoo data falls back to neutral sub-score 5.0.

    Result is CONTRACT quality only — no trend component.
    Should be ≤ final_score on well-structured contracts where trend boosts the composite.
    """
    # ── PRIMARY: use pre-computed sub-scores if available ────────────────────
    liq_s_raw    = c.get("liq_score")
    iv_s_raw     = c.get("iv_score")
    strike_s_raw = c.get("strike_score")
    dte_s_raw    = c.get("dte_score")

    if liq_s_raw is not None and any(
        x is not None for x in (iv_s_raw, strike_s_raw, dte_s_raw)
    ):
        _liq  = float(liq_s_raw    or 0.0)
        _iv   = float(iv_s_raw     or 0.0)
        _k    = float(strike_s_raw or 0.0)
        _dte  = float(dte_s_raw    or 0.0)
        # Weighted: liq 30% | iv 25% | strike 25% | dte 20%
        base = _liq * 0.30 + _iv * 0.25 + _k * 0.25 + _dte * 0.20
        # Small hybrid boost from live WebSocket / Moomoo data (±8 max)
        _ivs = c.get("iv_spread") if c.get("iv_spread") is not None else iv_spread
        _prs = c.get("pressure")  if c.get("pressure")  is not None else pressure
        boost = 0.0
        if _ivs is not None and float(_ivs) >= 0:
            boost += _clamp((2.0 - float(_ivs)) * 2.0, -5.0, 5.0)
        if _prs is not None and abs(float(_prs)) > 0.05:
            boost += _clamp(float(_prs) * 3.0, -3.0, 3.0)
        return round(_clamp(base * 10.0 + boost, 0.0, 100.0), 1)

    # ── FALLBACK: liquidity-only from raw fields ────────────────────────────
    spread_pct = c.get("spread_pct") or 999.0
    if spread_pct > 100:
        spd_s = 0.0
    elif spread_pct > 10:
        spd_s = _clamp(10.0 * (1.0 - (spread_pct - 10.0) / 90.0), 0.0, 5.0)
    else:
        spd_s = _clamp(10.0 * (1.0 - spread_pct / 10.0), 5.0, 10.0)

    # ── OI score (display only — NOT used for scoring decisions) ────────
    oi_s = 5.0  # neutral: OI is unreliable across sources

    # ── Volume score ─────────────────────────────────────────────────────
    vol = c.get("volume", 0) or 0
    if vol >= 10_000:
        vol_s = 10.0
    elif vol >= 1_000:
        vol_s = 6.0 + (vol - 1_000) / 9_000.0 * 4.0
    elif vol >= 100:
        vol_s = 2.0 + (vol - 100) / 900.0 * 4.0
    elif vol > 0:
        vol_s = vol / 100.0 * 2.0
    else:
        vol_s = 0.0

    # ── IV-spread score (from TV bid_iv/ask_iv) ───────────────────────────
    ivs_s = _ivspd_s(iv_spread) if iv_spread is not None else 5.0

    # ── Pressure score (from WebSocket bid_size/ask_size) ─────────────────
    prs_s = _pressure_s(pressure) if pressure is not None else 5.0

    # ── Weighted sum (×10 to scale component averages 0–10 → 0–100) ──────
    # OI excluded from scoring — only spread, volume, IV-spread, pressure
    raw = spd_s * 0.35 + vol_s * 0.25 + ivs_s * 0.20 + prs_s * 0.20
    return round(_clamp(raw * 10.0, 0.0, 100.0), 1)


def compute_wrs(c: dict, sig: float = 65.0) -> float:
    """Win Rate Score — probability (0–100) that this CALL option is profitable at expiry.

    Uses a lognormal break-even model from Black-Scholes theory:
        P(profit) = P(stock closes above strike + premium paid)

    All inputs come from the hybrid contract dict (TV scan2 + Moomoo OI + WebSocket).
    No if/else fallback chains — every field is used as-is from the accumulator.

    Args:
        c:   Hybrid contract dict from HybridOptionsFetcher.get_calls()
        sig: Parent-stock signal score 0–100 (TV oscillator). Default 65 = neutral.

    Returns:
        wrs: float 0–100
    """
    import math

    spot  = c.get("spot_price", 0) or 0          # underlying last price
    K     = c.get("strike",     0) or 0           # option strike
    mid   = c.get("mid_price",  0) or 0           # option mid-price (premium paid)
    dte   = c.get("dte",        0) or 0           # days to expiry
    iv    = c.get("iv",         0) or 0           # implied vol in % form (e.g. 38 = 38 %)
    delta = abs(c.get("delta",  0.3) or 0.3)
    rv    = c.get("rv",         iv) or iv         # realized vol; fallback to IV

    def _clamp_wrs(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    # ── Lognormal break-even probability ────────────────────────────────
    # Break-even price for a call = strike + premium paid
    be_price = K + mid
    use_be = False
    if spot > 0 and be_price > 0 and iv > 0 and dte > 0:
        sigma_T = (iv / 100.0) * math.sqrt(max(dte, 1) / 252.0)
        if sigma_T > 0:
            z = math.log(be_price / spot) / sigma_T
            p_be = 0.5 * math.erfc(z / math.sqrt(2))   # P(stock > be_price at expiry)
            base = _clamp_wrs(p_be * 100.0 - 2.0, 5.0, 88.0)
            use_be = True
        else:
            base = _clamp_wrs(delta * 100.0 - 2.0, 5.0, 90.0)
    else:
        base = _clamp_wrs(delta * 100.0 - 2.0, 5.0, 90.0)

    # ── Signal-quality adjustment (parent stock momentum) ───────────────
    if   sig >= 85: sa = +12
    elif sig >= 75: sa = +6
    elif sig >= 65: sa = +2
    elif sig >= 55: sa = -2
    elif sig >= 45: sa = -8
    else:           sa = -14

    # ── DTE decay penalty (time = risk) ─────────────────────────────────
    if   dte <= 5:  dp = -18
    elif dte <= 7:  dp = -14
    elif dte <= 10: dp = -10
    elif dte <= 14: dp = -7
    elif dte <= 21: dp = -4
    elif dte <= 30: dp = -2
    elif dte <= 45: dp = -1
    else:           dp = 0

    # Soften DTE penalty when we have a real lognormal estimate
    if use_be and dte <= 14:
        dp = int(dp * 0.40)

    # ── IV / RV ratio adjustment (IV premium = overpaying for volatility) ─
    ratio = (iv / rv) if rv > 0 else 1.0
    if   ratio <= 0.75: iv_adj = +6
    elif ratio <= 0.85: iv_adj = +4
    elif ratio <= 0.95: iv_adj = +2
    elif ratio <= 1.05: iv_adj = 0
    elif ratio <= 1.15: iv_adj = -2
    elif ratio <= 1.30: iv_adj = -4
    else:               iv_adj = -6

    raw = base + sa + dp + iv_adj
    return round(_clamp_wrs(raw, 5.0, 95.0), 1)


def compute_premium_win(c: dict, sig: float = 65.0) -> float:
    """Premium Win Score — P(option value reaches 3× entry premium at any point before expiry).

    Inspired by real trades: NEE +100%, GOOGL +600%, NVDA peak +800% (expired worthless), AAPL +200%.
    Models: P(max_{0≤t≤T} Option_Value_t ≥ 3 × entry_premium).

    Method:
    1. Finds target spot where option reaches 3× premium via delta+gamma quadratic.
    2. Lognormal reflection principle: P(max S_t ≥ H) = erfc(log(H/S) / (σ√T × √2))
    3. Adjusts for: signal quality, theta decay %, gamma leverage, momentum (WebSocket pressure).

    Fields used:
        spot_price, strike, mid_price, dte, iv (%), delta, gamma, theta, pressure (WS)

    Returns:
        float 3–80  (premium wins are harder to achieve than held-to-expiry)
    """
    import math

    spot     = c.get("spot_price", 0) or 0
    K        = c.get("strike",     0) or 0
    mid      = c.get("mid_price",  0) or 0
    dte      = c.get("dte",        0) or 0
    iv       = c.get("iv",         0) or 0        # % form (e.g. 38 = 38 %)
    delta    = abs(c.get("delta",  0.3) or 0.3)
    gamma    = c.get("gamma",      0) or 0
    theta    = abs(c.get("theta",  0) or 0)        # daily $-decay (abs value)
    pressure = c.get("pressure",   0) or 0         # WebSocket bid/ask pressure

    def _cl(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    if spot <= 0 or mid <= 0 or iv <= 0 or dte <= 0 or delta <= 0:
        return 15.0

    # ── Step 1: Target spot via delta+gamma quadratic ──────────────────
    # To reach 2× premium (option doubles): ΔV = delta×x + 0.5×gamma×x² = 1×mid
    additional_needed = 1.0 * mid  # target: option value doubles (100% gain on premium)
    if gamma > 0.001:
        disc = delta ** 2 + 2.0 * gamma * additional_needed
        x = (-delta + math.sqrt(disc)) / gamma if disc > 0 else additional_needed / delta
    else:
        x = additional_needed / delta
    x = max(x, 0.01 * spot)           # at least 1 % move
    target_spot = spot + x

    # ── Step 2: Barrier probability (reflection principle, μ=0) ────────
    sigma_T = (iv / 100.0) * math.sqrt(max(dte, 1) / 365.0)
    if sigma_T <= 0:
        return 15.0
    z = math.log(target_spot / spot) / sigma_T
    # P(max lognormal >= H before T) = erfc(z / sqrt(2))
    p_touch = 0.5 * math.erfc(z / math.sqrt(2))
    base = _cl(p_touch * 100.0 - 3.0, 3.0, 75.0)

    # ── Step 3: Signal quality ──────────────────────────────────────────
    if   sig >= 85: sa = +15
    elif sig >= 75: sa = +8
    elif sig >= 65: sa = +3
    elif sig >= 55: sa = -3
    elif sig >= 45: sa = -10
    else:           sa = -18

    # ── Step 4: DTE (very short → fast decay; very long → too much wait) ─
    if   dte <= 3:  dte_adj = -10
    elif dte <= 7:  dte_adj = -5
    elif dte <= 14: dte_adj = 0       # sweet spot
    elif dte <= 21: dte_adj = -2
    elif dte <= 45: dte_adj = -8
    else:           dte_adj = -15

    # ── Step 5: Theta decay risk (theta/mid = daily decay %) ───────────
    theta_pct = (theta / mid) if (mid > 0 and theta > 0) else 0.0
    if   theta_pct > 0.10: theta_adj = -12   # >10 %/day — very dangerous
    elif theta_pct > 0.05: theta_adj = -7
    elif theta_pct > 0.02: theta_adj = -3
    elif theta_pct > 0.01: theta_adj = 0
    else:                  theta_adj = +3

    # ── Step 6: Momentum (WebSocket bid-ask pressure) ──────────────────
    if   pressure > 0.3:  mom_adj = +8
    elif pressure > 0.1:  mom_adj = +4
    elif pressure < -0.3: mom_adj = -8
    elif pressure < -0.1: mom_adj = -4
    else:                 mom_adj = 0

    # ── Step 7: Gamma leverage (high γ/δ = explosive near-strike moves) ─
    g_ratio = (gamma / delta) if (delta > 0 and gamma > 0) else 0.0
    if   g_ratio > 0.04: gamma_adj = +8
    elif g_ratio > 0.03: gamma_adj = +6
    elif g_ratio > 0.01: gamma_adj = +3
    else:                gamma_adj = 0

    raw = base + sa + dte_adj + theta_adj + mom_adj + gamma_adj
    return round(_cl(raw, 3.0, 80.0), 1)


# ── TV WebSocket protocol helpers ─────────────────────────────────────────────

def _tv_encode(msg: dict) -> str:
    body = json.dumps(msg, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"


def _tv_decode(raw: str) -> list:
    results: list = []
    parts = re.compile(r"~m~(\d+)~m~").split(raw)
    i = 1
    while i + 1 < len(parts):
        payload = parts[i + 1]
        if payload.startswith("{"):
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
        i += 2
    return results


def _rand_session_id(prefix: str = "qs_hybrid") -> str:
    return prefix + "_" + "".join(random.choices(string.ascii_letters + string.digits, k=12))


def _load_tv_cookie() -> tuple[str, str]:
    text = _COOKIE_FILE.read_text("utf-8")
    m1   = re.search(r"sessionid=([^;\s]+)", text)
    m2   = re.search(r"sessionid_sign=([^;\s]+)", text)
    if not m1:
        raise ValueError("No sessionid found in tradingview/cookie")
    return m1.group(1).strip(), (m2.group(1).strip() if m2 else "")


# ── Async WebSocket fetcher ───────────────────────────────────────────────────

async def _fetch_ws_fields(
    contract_symbols: list[str],
    timeout_s: float = 12.0,
    verbose: bool = False,
) -> dict[str, dict]:
    """
    Fetch live option fields from TV WebSocket for the given OPRA contract symbols.

    Fields requested: lp, open_price, high_price, low_price, ask_size, bid_size, volume

    Returns dict mapping OPRA_symbol → {field: value, ...}.
    Returns {} on any connection failure (graceful degradation).
    """
    try:
        import websockets  # type: ignore
    except ImportError:
        if verbose:
            print("[Hybrid] websockets not installed — pip install websockets")
        return {}

    try:
        sid, ssig = _load_tv_cookie()
    except Exception as exc:
        if verbose:
            print(f"[Hybrid] Cookie load failed: {exc}")
        return {}

    session_name = _rand_session_id("qs_hybrid_opts")
    ws_fields    = [
        "ask", "bid", "lp", "volume",
        "open_price", "high_price", "low_price",
        "ask_size", "bid_size",
    ]
    _now   = datetime.datetime.now(datetime.timezone.utc)
    _date  = _now.strftime("%Y-%m-%dT%H%%3A%M%%3A%S")
    ws_url = (
        "wss://data.tradingview.com/socket.io/websocket"
        "?from=options%2Fchain%2F"
        f"&date={_date}"
        "&auth=sessionid"
    )
    headers = {
        "Origin":     "https://www.tradingview.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        ),
        "Cookie":   f"sessionid={sid}; sessionid_sign={ssig}",
        "Referer":  "https://www.tradingview.com/options/chain/",
    }

    result: dict[str, dict] = {}
    completed: set[str]     = set()
    sym_set                  = set(contract_symbols)

    try:
        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            async def _send(msg: dict) -> None:
                await ws.send(_tv_encode(msg))

            async def _recv(timeout: float = 3.0) -> list:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                raw_str = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                if raw_str.startswith("~h~"):
                    await ws.send(raw_str)   # echo heartbeat
                    return []
                return _tv_decode(raw_str)

            # Server hello
            try:
                await _recv(timeout=8.0)
            except asyncio.TimeoutError:
                pass

            # Create session, set fields, subscribe, request snapshot
            await _send({"m": "quote_create_session", "p": [session_name]})
            await asyncio.sleep(0.2)
            await _send({"m": "quote_set_fields", "p": [session_name] + ws_fields})
            await asyncio.sleep(0.2)
            await _send({"m": "quote_add_symbols", "p": [session_name] + contract_symbols})
            await asyncio.sleep(0.3)
            await _send({"m": "quote_fast_symbols", "p": [session_name] + contract_symbols})

            # Collect qsd messages until all symbols completed or timeout
            deadline    = time.time() + timeout_s
            idle_streak = 0   # consecutive recv-timeouts with no new data
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                # Early-exit when all symbols have at least one field
                if len(result) >= len(contract_symbols):
                    break
                try:
                    msgs = await _recv(timeout=min(remaining, 2.0))
                    idle_streak = 0   # data arrived — reset streak
                except asyncio.TimeoutError:
                    idle_streak += 1
                    if idle_streak >= 2:
                        break   # 2 consecutive dead recv cycles → give up early
                    continue
                except Exception:
                    break

                for m in msgs:
                    if m.get("m") == "qsd":
                        params = m.get("p", [])
                        if len(params) >= 2 and params[0] == session_name:
                            data   = params[1]
                            symbol = data.get("n", "")
                            values = data.get("v", {})
                            if symbol in sym_set:
                                if symbol not in result:
                                    result[symbol] = {}
                                result[symbol].update(
                                    {k: v for k, v in values.items() if v is not None}
                                )
                    elif m.get("m") == "quote_completed":
                        p = m.get("p", [])
                        if len(p) >= 2 and p[0] == session_name:
                            completed.add(p[1])

                if len(completed) >= len(contract_symbols):
                    break

    except Exception as exc:
        if verbose:
            print(f"[Hybrid] WebSocket error: {exc}")

    if verbose:
        filled = sum(1 for v in result.values() if v)
        print(f"[Hybrid] WebSocket: {filled}/{len(contract_symbols)} contracts enriched")

    return result


# ── Hybrid fetcher ────────────────────────────────────────────────────────────

class HybridOptionsFetcher:
    """
    Three-source hybrid CALL-options fetcher for US symbols.

    Sources (in order of priority):
      1. TradingView scan2 REST  → IV, Greeks, theoPrice, bid_iv/ask_iv, oi=0
      2. Moomoo OpenD            → real open_interest merged by (strike, expiry, type)
      3. TradingView WebSocket   → live last_price, OHLC, bid_size/ask_size

    Extra fields added to each contract versus TV-only:
      oi             : int   — real OI from Moomoo (0 if unavailable)
      oi_source      : str   — "moomoo" | "unavailable"
      ws_lp          : float — live last price from WebSocket
      ws_open        : float — session open
      ws_high        : float — session high
      ws_low         : float — session low
      ws_volume      : int   — last-session volume from WebSocket
      ohlc_range_pct : float — (lp−low)/(high−low)×100, position in day range
      bid_size        : int   — bid size from WebSocket
      ask_size        : int   — ask size from WebSocket
      pressure        : float — (bid_size−ask_size)/(bid_size+ask_size), −1..+1
      iv_spread       : float — abs(ask_iv−bid_iv) in % (from TV bid_iv/ask_iv)
      ws_source       : str   — "live" | "closed" | "unavailable"
      tqs_v2          : float — hybrid TQS v2 score 0–100
      tqs_v2_badge    : str   — "[Market Hours]" | "[Market Closed]" | "[No WebSocket]"
      source          : str   — "hybrid"
    """

    def __init__(self, verbose: bool = False, ws_timeout: float = 12.0) -> None:
        self.verbose    = verbose
        self.ws_timeout = ws_timeout

    # ── Public API ────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if TV scan2 (the primary source) is reachable."""
        try:
            from .tv_options import TVOptionsFetcher
            return TVOptionsFetcher(verbose=False).is_available()
        except Exception:
            return False

    def get_calls(self, pro_symbol: str, n_expiries: int = 3, max_dte: int | None = None) -> list[OptionContractData]:
        """
        Sequential-merge CALL contracts for *pro_symbol* from ALL three sources.

        Architecture: ALL sources are always attempted.  Missing data → neutral
        defaults (never skip a contract because one source is offline).

        Step 1 — TradingView scan2: IV, Greeks, bid_iv/ask_iv, theoPrice
        Step 2 — Moomoo OpenD:      Real open_interest (merged by key)
        Step 3 — TradingView WS:    Live lp, OHLC, bid_size/ask_size
        Step 4 — Compute:           ohlc_range_pct, pressure, iv_spread, TQS v2
        Step 5 — Flag:              data_completeness dict on every contract

        Returns a list of enriched dicts even when TV scan2 returns nothing
        (weekend / after-hours) — Moomoo acts as the contract-list seed in that case.
        """
        # ── Step 1: TV scan2 ─────────────────────────────────────────────
        tv_contracts = self._fetch_tv(pro_symbol, n_expiries)
        tv_ok = bool(tv_contracts)

        # Extract spot_price from TV contracts (falls back to yfinance inside tv_options).
        # Store it here so Moomoo-seeded contracts (which have no spot_price) can
        # inherit it and display correct ATM% / Spot in the report.
        tv_spot_price: float = 0.0
        for c in tv_contracts:
            sp = c.get("spot_price") or 0.0
            if sp > 0.0:
                tv_spot_price = sp
                break

        # Build working dict keyed by (strike, norm_expiry, type)
        # Normalize TV expiry to int YYYYMMDD so it's compatible with Moomoo keys
        merged: dict[tuple, dict] = {}
        for c in tv_contracts:
            tv_exp_norm = _norm_expiry(c["expiry"])
            key = (round(c["strike"], 2), tv_exp_norm, c.get("option_type", "CALL").upper())
            merged[key] = dict(c)
            merged[key]["source_tv"]     = True
            merged[key]["source_moomoo"] = False
            merged[key]["source_ws"]     = False
            merged[key]["_tv_exp_norm"]  = tv_exp_norm

        # ── Step 2: OI enrichment ────────────────────────────────────────────
        # Moomoo OpenD removed from options pipeline (2026-05-17).
        # Stability issues: 419s hangs, 400-call limit, connection dependency.
        # yfinance OI fallback (Step 2b below) provides display-only OI.
        mm_contracts: list[dict] = []
        oi_map: dict[tuple, int] = {}
        oi_source = "unavailable"
        mm_ok = False

        # Build a fast lookup from Moomoo: (strike, mm_exp_norm, type) → contract
        mm_by_key: dict[tuple, dict] = {}
        for mc in mm_contracts:
            mm_exp_norm = mc.get("_mm_exp_norm") or _norm_expiry(mc.get("expiry", 0))
            key = (round(mc["strike"], 2), mm_exp_norm, mc.get("option_type", "CALL").upper())
            mm_by_key[key] = mc

        # ── Nearest-expiry pairing ────────────────────────────────────────
        # TV expiry dates use option-expiry convention (e.g. Fri May 22 → 20260522).
        # Moomoo expiry dates use settlement convention (e.g. Mon May 26 → 20260526).
        # The offset is 1-4 days depending on the exchange/contract type.
        # Strategy: for each TV expiry, find the closest Moomoo expiry within 7 days.
        # Then all (strike, TV-expiry) keys are matched to the paired Moomoo expiry.

        tv_expiries_set = sorted({k[1] for k in merged})  # sorted int list
        mm_expiries_set = sorted({k[1] for k in oi_map})  # sorted int list

        def _days_diff(exp_a: int, exp_b: int) -> int:
            try:
                da = datetime.date(exp_a // 10000, (exp_a % 10000) // 100, exp_a % 100)
                db = datetime.date(exp_b // 10000, (exp_b % 10000) // 100, exp_b % 100)
                return abs((db - da).days)
            except (ValueError, ZeroDivisionError):
                return 999

        # tv_to_mm_expiry[tv_exp_int] = best mm_exp_int (within 7 days)
        tv_to_mm_expiry: dict[int, int] = {}
        for tv_exp in tv_expiries_set:
            best_mm, best_diff = None, 999
            for mm_exp in mm_expiries_set:
                d = _days_diff(tv_exp, mm_exp)
                if d < best_diff:
                    best_diff = d
                    best_mm = mm_exp
            if best_diff <= 7 and best_mm is not None:
                tv_to_mm_expiry[tv_exp] = best_mm

        # mm_to_tv_expiry[mm_exp_int] = paired tv_exp_int
        mm_to_tv_expiry: dict[int, int] = {v: k for k, v in tv_to_mm_expiry.items()}

        for mm_key, oi_val in oi_map.items():
            mm_strike, mm_exp, mm_type = mm_key
            paired_tv_exp = mm_to_tv_expiry.get(mm_exp)

            # Find the matching TV key using paired expiry
            matched_tv_key = None
            if mm_key in merged:
                matched_tv_key = mm_key
            elif paired_tv_exp is not None:
                candidate = (mm_strike, paired_tv_exp, mm_type)
                if candidate in merged:
                    matched_tv_key = candidate

            if matched_tv_key is not None:
                # OI merge into existing TV contract
                merged[matched_tv_key]["oi"]            = oi_val
                merged[matched_tv_key]["oi_source"]     = oi_source
                merged[matched_tv_key]["source_moomoo"] = True

                # Weekend bid/ask fill: use Moomoo's last-session bid/ask when TV has zeros
                c = merged[matched_tv_key]
                if (c.get("bid") or 0.0) == 0.0 and mm_key in mm_by_key:
                    mm_c   = mm_by_key[mm_key]
                    mm_bid = mm_c.get("_mm_bid") or mm_c.get("bid") or 0.0
                    mm_ask = mm_c.get("_mm_ask") or mm_c.get("ask") or 0.0
                    if mm_bid > 0.0:
                        c["bid"]        = mm_bid
                        c["ask"]        = mm_ask
                        c["mid_price"]  = round((mm_bid + mm_ask) / 2.0, 4)
                        c["spread_pct"] = (
                            round((mm_ask - mm_bid) / c["mid_price"] * 100, 2)
                            if c["mid_price"] > 0 else 0.0
                        )
                        c["bid_source"] = "moomoo_last_session"
            else:
                # Moomoo has contract TV doesn't — seed from Moomoo
                mc = mm_by_key.get(mm_key, {})
                new_c = dict(mc)
                new_c["source_tv"]     = False
                new_c["source_moomoo"] = True
                new_c["source_ws"]     = False
                new_c["oi"]            = oi_val
                new_c["oi_source"]     = oi_source
                # Inherit spot_price from TV so Spot/$ATM% display correctly
                new_c["spot_price"]    = tv_spot_price
                if tv_spot_price > 0 and new_c.get("strike"):
                    new_c["moneyness_pct"] = round(
                        (new_c["strike"] - tv_spot_price) / tv_spot_price * 100, 2
                    )
                # Compute DTE from expiry (Moomoo contracts don't carry this)
                if not new_c.get("dte"):
                    try:
                        exp_i = mm_exp
                        exp_d = datetime.date(exp_i // 10000, (exp_i % 10000) // 100, exp_i % 100)
                        new_c["dte"] = max(0, (exp_d - datetime.date.today()).days)
                    except Exception:
                        new_c["dte"] = 0
                # Compute mid_price / spread_pct if not present
                if "bid" in new_c and "ask" in new_c:
                    mm_bid = new_c.get("bid") or 0.0
                    mm_ask = new_c.get("ask") or 0.0
                    new_c["mid_price"]  = round((mm_bid + mm_ask) / 2.0, 4)
                    new_c["spread_pct"] = (
                        round((mm_ask - mm_bid) / new_c["mid_price"] * 100, 2)
                        if new_c.get("mid_price", 0) > 0 else 0.0
                    )
                if new_c.get("strike"):
                    merged[mm_key] = new_c

        # Mark TV-only contracts as moomoo-unavailable
        for key, c in merged.items():
            if not c.get("source_moomoo"):
                c["oi"]        = 0
                c["oi_source"] = "unavailable"

        # If both TV and Moomoo empty → nothing to score
        if not merged:
            if self.verbose:
                print(f"[Hybrid] {pro_symbol}: TV and Moomoo both returned nothing")
            return []

        # ── Step 3: TV WebSocket ─────────────────────────────────────────
        # Always attempt regardless of TV/Moomoo result
        contracts_list = list(merged.values())
        ws_data, ws_source = self._fetch_ws(contracts_list)
        ws_ok = bool(ws_data)

        # ── Step 4: Compute derived fields and TQS v2 ────────────────────
        market_hours = _is_us_market_hours()
        tqs_v2_badge = (
            "[Market Hours]"  if ws_source == "live"    else
            "[Market Closed]" if ws_source == "closed"  else
            "[No WebSocket]"
        )

        output: list[dict] = []
        for key, c in merged.items():
            code = c.get("code", "")
            ws   = ws_data.get(code, {})

            # Live price and OHLC from WebSocket
            ws_lp   = _safe(ws.get("lp"))
            ws_open = _safe(ws.get("open_price"))
            ws_high = _safe(ws.get("high_price"))
            ws_low  = _safe(ws.get("low_price"))
            ws_vol  = ws.get("volume")

            c["ws_lp"]   = ws_lp
            c["ws_open"] = ws_open
            c["ws_high"] = ws_high
            c["ws_low"]  = ws_low
            c["source_ws"] = bool(ws)

            # Volume: prefer WebSocket when TV=0 (weekend/after-hours)
            if ws_vol is not None and (not c.get("volume") or not market_hours):
                c["ws_volume"] = int(float(ws_vol))
                c["volume"]    = c["ws_volume"]
            else:
                c["ws_volume"] = None

            # OHLC position in day range (0–100 %)
            ohlc_range_pct: Optional[float] = None
            if ws_lp is not None and ws_high is not None and ws_low is not None:
                h, lo, lp_f = float(ws_high), float(ws_low), float(ws_lp)
                if h > lo:
                    ohlc_range_pct = max(0.0, min(100.0, (lp_f - lo) / (h - lo) * 100.0))

            # Order pressure: (bid_size - ask_size) / (bid_size + ask_size), -1..+1
            bid_sz   = ws.get("bid_size")
            ask_sz   = ws.get("ask_size")
            pressure: Optional[float] = None
            if bid_sz is not None and ask_sz is not None:
                total = float(bid_sz) + float(ask_sz)
                if total > 0:
                    pressure = (float(bid_sz) - float(ask_sz)) / total

            # IV spread from TV bid_iv/ask_iv (available from scan2; null when Moomoo-seeded)
            bid_iv   = c.get("bid_iv") or 0.0
            ask_iv   = c.get("ask_iv") or 0.0
            iv_spread: Optional[float] = abs(ask_iv - bid_iv) if (bid_iv > 0 or ask_iv > 0) else None

            # TQS v2 — neutral defaults (5.0) for missing inputs
            tqs_v2 = compute_tqs_v2(
                c,
                iv_spread=iv_spread,
                ohlc_range_pct=ohlc_range_pct,
                pressure=pressure,
            )

            # ── Step 5: data completeness flags ──────────────────────────
            data_completeness = {
                "tv":     c.get("source_tv",     False),
                "moomoo": c.get("source_moomoo", False),
                "ws":     c.get("source_ws",     False),
            }

            # Win Rate Score — lognormal P(profit at expiry)
            wrs = compute_wrs(c)

            # Premium Win Score — P(3× premium at any point before expiry)
            premium_win = compute_premium_win(c, sig=65.0)

            c.update({
                "ohlc_range_pct":    ohlc_range_pct,
                "bid_size":          bid_sz,
                "ask_size":          ask_sz,
                "pressure":          pressure,
                "iv_spread":         iv_spread,
                "ws_source":         ws_source,
                "tqs_v2":            tqs_v2,
                "tqs_v2_badge":      tqs_v2_badge,
                "wrs":               wrs,
                "wrs_badge":         f"{wrs:.0f}%",
                "premium_win":       premium_win,
                "premium_win_badge": f"{premium_win:.0f}%",
                "source":            "hybrid",
                "data_completeness": data_completeness,
            })

            # Human-readable data quality header for the report
            sources_used = [s for s, ok in [("TV", tv_ok), ("Moomoo", mm_ok), ("WS", ws_ok)] if ok]
            c["hybrid_sources"] = "+".join(sources_used) if sources_used else "none"

            output.append(c)

        if self.verbose:
            print(
                f"[Hybrid] {pro_symbol}: {len(output)} contracts "
                f"| TV={'✓' if tv_ok else '✗'} "
                f"| Moomoo={'✓' if mm_ok else '✗'} "
                f"| WS={'✓' if ws_ok else '✗'} "
                f"| sources={output[0]['hybrid_sources'] if output else 'none'}"
            )

        if max_dte is not None:
            output = [c for c in output if c.get("dte", 0) <= max_dte]

        return output

    def get_puts(self, pro_symbol: str, n_expiries: int = 3, max_dte: int | None = None) -> list[OptionContractData]:
        """Fetch PUT contracts.  Mirrors get_calls() but filters to PUT option_type."""
        all_contracts = self.get_calls(pro_symbol, n_expiries=n_expiries, max_dte=max_dte)
        return [c for c in all_contracts if str(c.get("option_type", "C")).upper() in ("P", "PUT")]

    @classmethod
    def batch_enrich_ws(
        cls,
        contracts: list[dict],
        ws_timeout: float = 15.0,
        verbose: bool = False,
    ) -> None:
        """Phase-2 batch WebSocket enrichment — enrich all contracts in one WS session.

        The per-stock Phase-1 scan (with skip_ws=True) already populated every
        contract with bid/ask/vol/IV data from TV scan2.  This Phase-2 pass would
        open a single Moomoo WS session to layer in live tick data.

        Current status: No-op — contracts are already fully enriched by TV scan2 in
        Phase 1.  If Moomoo WS streaming is added later, implement it here.
        """
        if verbose:
            print(f"[WS-batch] batch_enrich_ws: {len(contracts)} contracts already enriched by scan2 — skipping WS layer")

    # ── Private helpers ───────────────────────────────────────────────────

    def _fetch_tv(self, pro_symbol: str, n_expiries: int) -> list[dict]:
        """Fetch base contracts from TV scan2."""
        try:
            from .tv_options import TVOptionsFetcher
            tv = TVOptionsFetcher(verbose=self.verbose)
            if not tv.is_available():
                if self.verbose:
                    print(f"[Hybrid] TV not available for {pro_symbol}")
                return []
            return tv.get_calls(pro_symbol, n_expiries=n_expiries)
        except Exception as exc:
            if self.verbose:
                print(f"[Hybrid] TV fetch failed for {pro_symbol}: {exc}")
            return []

    def _fetch_moomoo_all(
        self,
        pro_symbol: str,
        n_expiries: int,
        timeout_s: float = 15.0,
    ) -> tuple[list[dict], dict[tuple, int], str]:
        """
        Fetch full Moomoo contract list AND OI map.

        Returns (mm_contracts, oi_map, oi_source_label).
        mm_contracts: full contract dicts from Moomoo (used to seed when TV empty)
        oi_map:       {(strike, expiry, type): oi_value}
        oi_source:    "moomoo" | "unavailable"

        Uses ThreadPoolExecutor with timeout to prevent hangs (Golden Rule #16).
        """
        import concurrent.futures

        def _worker() -> tuple[list[dict], dict[tuple, int], str]:
            oi_map:       dict[tuple, int]  = {}
            mm_contracts: list[dict]        = []
            try:
                from .us_options import USOptionsFetcher, tv_to_mm
                mm_code = tv_to_mm(pro_symbol)
                if not mm_code:
                    return [], {}, "unavailable"
                moo = USOptionsFetcher(verbose=self.verbose)
                if not moo.is_available():
                    return [], {}, "unavailable"

                # Get n_expiries expirations from Moomoo
                expiry_dates: list[str] = []
                try:
                    expiry_dates = moo.get_expiry_dates(mm_code)[:n_expiries]
                except Exception:
                    pass

                for expiry_date in expiry_dates:
                    chain = moo.get_option_chain_for_date(mm_code, expiry_date)
                    for mc in chain:
                        if mc.get("option_type", "CALL").upper() != "CALL":
                            continue
                        mm_exp_norm = _norm_expiry(mc.get("expiry", expiry_date))
                        key = (round(mc["strike"], 2), mm_exp_norm, "CALL")
                        oi_map[key]  = mc.get("oi", 0)
                        mc["_mm_bid"]      = mc.get("bid", 0.0) or 0.0
                        mc["_mm_ask"]      = mc.get("ask", 0.0) or 0.0
                        mc["_mm_exp_norm"] = mm_exp_norm
                        mm_contracts.append(mc)

                if self.verbose:
                    print(
                        f"[Hybrid] Moomoo: {len(mm_contracts)} contracts, "
                        f"{len(oi_map)} OI keys across {len(expiry_dates)} expiries"
                    )
            except Exception as exc:
                if self.verbose:
                    print(f"[Hybrid] Moomoo fetch failed: {exc}")
                return [], {}, "unavailable"

            return mm_contracts, oi_map, ("moomoo" if oi_map else "unavailable")

        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _fut = _ex.submit(_worker)
        try:
            return _fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            if self.verbose:
                print(f"[Hybrid] Moomoo fetch timed out after {timeout_s}s")
            return [], {}, "unavailable"
        finally:
            _ex.shutdown(wait=False)

    def _fetch_moomoo_oi(
        self,
        pro_symbol: str,
        contracts: list[dict],
    ) -> tuple[dict[tuple, int], str]:
        """
        Fetch OI from Moomoo for the expiry dates in *contracts*.

        Returns (oi_map, oi_source_label).
        oi_map keys: (round(strike, 2), expiry_str, "CALL" or "PUT")

        NOTE: For new code use _fetch_moomoo_all() which also returns contract
        dicts so Moomoo can seed contracts when TV scan2 is empty.
        """
        oi_map: dict[tuple, int] = {}
        try:
            from .us_options import USOptionsFetcher, tv_to_mm
            mm_code = tv_to_mm(pro_symbol)
            if not mm_code:
                return {}, "unavailable"
            moo = USOptionsFetcher(verbose=self.verbose)
            if not moo.is_available():
                return {}, "unavailable"
            expiries = sorted({c["expiry"] for c in contracts})
            for expiry_date in expiries:
                chain = moo.get_option_chain_for_date(mm_code, expiry_date)
                for mc in chain:
                    key = (round(mc["strike"], 2), mc["expiry"], mc.get("option_type", "CALL").upper())
                    oi_map[key] = mc.get("oi", 0)
            if self.verbose:
                print(f"[Hybrid] Moomoo OI: {len(oi_map)} contracts across {len(expiries)} expiries")
        except Exception as exc:
            if self.verbose:
                print(f"[Hybrid] Moomoo OI fetch failed: {exc}")
            return {}, "unavailable"

        return oi_map, ("moomoo" if oi_map else "unavailable")

    def _fetch_ws(
        self,
        contracts: list[dict],
    ) -> tuple[dict[str, dict], str]:
        """
        Fetch live WebSocket fields for *contracts*.

        Returns (ws_data, ws_source_label).
        ws_data keys: OPRA code strings.
        """
        opra_symbols = [c["code"] for c in contracts if c.get("code")]
        if not opra_symbols:
            return {}, "unavailable"
        try:
            ws_data = asyncio.run(
                _fetch_ws_fields(opra_symbols, timeout_s=self.ws_timeout, verbose=self.verbose)
            )
        except RuntimeError:
            # asyncio.run() fails if a loop is already running (e.g. Jupyter).
            # Fall back to a new thread-based event loop.
            # Golden Rule #16: NEVER use `with ThreadPoolExecutor` — use shutdown(wait=False)
            try:
                import concurrent.futures
                _ws_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                _ws_fut = _ws_ex.submit(
                    asyncio.run,
                    _fetch_ws_fields(opra_symbols, timeout_s=self.ws_timeout, verbose=self.verbose),
                )
                try:
                    ws_data = _ws_fut.result(timeout=self.ws_timeout + 5)
                except concurrent.futures.TimeoutError:
                    if self.verbose:
                        print(f"[Hybrid] WebSocket thread fallback timed out")
                    return {}, "unavailable"
                finally:
                    _ws_ex.shutdown(wait=False)
            except Exception as exc:
                if self.verbose:
                    print(f"[Hybrid] WebSocket thread fallback failed: {exc}")
                return {}, "unavailable"
        except Exception as exc:
            if self.verbose:
                print(f"[Hybrid] WebSocket fetch failed: {exc}")
            return {}, "unavailable"

        if not ws_data:
            return {}, "unavailable"

        market_hours = _is_us_market_hours()
        ws_source    = "live" if market_hours else "closed"
        return ws_data, ws_source


# ── Utility ───────────────────────────────────────────────────────────────────

def _safe(val: object) -> Optional[float]:
    """Return float(val) or None if val is None/invalid."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
