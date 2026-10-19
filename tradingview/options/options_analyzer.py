"""
Options Analyzer Engine — F&O Contract Recommendation
======================================================
Takes a TV buy signal + optional signal score and recommends the best
option contract to trade.

Architecture:
  TradingView Alert
    → Signal Engine (L1-L4 scoring, composite 0-100)
    → OptionsAnalyzer.analyze(symbol, signal_score, mode)
        Step 1: Eligibility check (US = always; India = NSE F&O list)
        Step 2: Fetch option chain (Moomoo for US, Angel One for India)
        Step 3: Filter contracts (liquidity, expiry, earnings)
        Step 4: Strike selection (ITM/ATM/OTM based on mode)
        Step 5: Score each contract (5-factor formula)
        Step 6: Return top-3 ranked contracts with full reasoning

Scoring formula (0-100):
  score = (trend_strength  × 0.30)   ← signal composite from L1-L4
        + (liquidity_score × 0.20)   ← volume, OI, bid-ask spread
        + (iv_score        × 0.20)   ← IV level vs ideal range
        + (strike_score    × 0.15)   ← moneyness alignment with mode
        + (dte_score       × 0.15)   ← DTE alignment with mode

Strategy modes (configurable):
  SAFE       → ITM (delta 0.60-0.75), DTE 30-60, tightest spread filter
  BALANCED   → ATM (delta 0.45-0.60), DTE 14-35, standard filters
  AGGRESSIVE → OTM (delta 0.25-0.45), DTE 7-21,  wider filters (high reward)

Edge cases handled:
  • Earnings within 5 days      → "EARNINGS RISK" warning + score penalty
  • Bid-ask spread > 10%        → reject (illiquid)
  • Volume < 10 or OI < 100    → reject (US) / stricter India filters
  • IV > 80% (absolute)         → "IV HIGH" warning + penalty (overpriced)
  • IV < 5% (absolute)          → flag as potential data error
  • Premium < $0.05 (US) / ₹2  → flag "CHEAP TRAP" (likely OTM garbage)
  • Delta > 0.90                → probably too deep ITM (no leverage)

Usage:
  from options.options_analyzer import OptionsAnalyzer, StrategyMode

  analyzer = OptionsAnalyzer()
  result   = analyzer.analyze(
      pro_symbol    = "NASDAQ:AAPL",
      signal_score  = 72.5,
      mode          = StrategyMode.BALANCED,
      earnings_days = 30,            # days until next earnings (optional)
  )
  if result["options_available"]:
      for contract in result["top_contracts"]:
          print(contract["recommendation_reason"])
"""

from __future__ import annotations

import concurrent.futures
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .nse_fo_list import is_fo_eligible


class USScannerOptionsUnavailable(RuntimeError):
    """Scanner-only signal that no provider completed an interpretable chain fetch."""


# ══════════════════════════════════════════════════════════════════════════════
# Strategy modes
# ══════════════════════════════════════════════════════════════════════════════

class StrategyMode(str, Enum):
    SAFE       = "SAFE"
    BALANCED   = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


@dataclass
class ModeConfig:
    """Per-mode scoring + filtering parameters."""
    name:               str
    # Strike selection (moneyness %)
    # Negative = ITM (strike < spot), Positive = OTM (strike > spot)
    target_moneyness_min: float   # e.g. -10.0 for ITM
    target_moneyness_max: float   # e.g.  +2.0 for ATM/slight OTM
    # Delta target range (approx)
    delta_min:          float
    delta_max:          float
    # DTE window (days to expiry)
    dte_min:            int
    dte_max:            int
    # Liquidity gates (reject below these)
    min_volume_us:      int
    min_oi_us:          int
    min_volume_in:      int
    min_oi_in:          int
    max_spread_pct:     float
    # IV gates (warn/reject)
    max_iv_warn:        float     # IV above this → "HIGH IV" warning
    # Premium floor (reject below)
    min_premium_usd:    float
    min_premium_inr:    float
    # Emoji
    emoji:              str
    description:        str


MODE_CONFIGS: dict[StrategyMode, ModeConfig] = {
    StrategyMode.SAFE: ModeConfig(
        name               = "SAFE",
        target_moneyness_min = -10.0,
        target_moneyness_max =  -1.0,
        delta_min          = 0.60,
        delta_max          = 0.80,
        dte_min            = 28,
        dte_max            = 60,
        min_volume_us      = 50,
        min_oi_us          = 200,
        min_volume_in      = 100,
        min_oi_in          = 1000,
        max_spread_pct     = 5.0,
        max_iv_warn        = 70.0,
        min_premium_usd    = 0.50,
        min_premium_inr    = 5.0,
        emoji              = "🛡️",
        description        = "ITM call — high delta, lower leverage, higher probability",
    ),
    StrategyMode.BALANCED: ModeConfig(
        name               = "BALANCED",
        target_moneyness_min = -3.0,
        target_moneyness_max = +3.0,
        delta_min          = 0.40,
        delta_max          = 0.65,
        dte_min            = 14,
        dte_max            = 35,
        min_volume_us      = 20,
        min_oi_us          = 100,
        min_volume_in      = 50,
        min_oi_in          = 500,
        max_spread_pct     = 8.0,
        max_iv_warn        = 80.0,
        min_premium_usd    = 0.20,
        min_premium_inr    = 2.0,
        emoji              = "⚖️",
        description        = "ATM call — balanced risk/reward, moderate leverage",
    ),
    StrategyMode.AGGRESSIVE: ModeConfig(
        name               = "AGGRESSIVE",
        target_moneyness_min = +1.0,
        target_moneyness_max = +8.0,
        delta_min          = 0.20,
        delta_max          = 0.48,
        dte_min            = 7,
        dte_max            = 21,
        min_volume_us      = 10,
        min_oi_us          = 50,
        min_volume_in      = 20,
        min_oi_in          = 200,
        max_spread_pct     = 12.0,
        max_iv_warn        = 90.0,
        min_premium_usd    = 0.05,
        min_premium_inr    = 0.50,
        emoji              = "🚀",
        description        = "OTM call — high leverage, lower probability, requires strong conviction",
    ),
}


# ── PUT options mode configs ──────────────────────────────────────────────────
# For protective puts / bearish directional plays — OTM puts want
# positive moneyness (above underlying), so moneyness sign is inverted.
PUT_MODE_CONFIGS: dict[StrategyMode, ModeConfig] = {
    StrategyMode.SAFE: ModeConfig(
        name               = "SAFE (PUT)",
        target_moneyness_min = -1.0,
        target_moneyness_max = +10.0,
        delta_min          = 0.55,
        delta_max          = 0.80,   # high-delta ITM puts (abs delta)
        dte_min            = 28,
        dte_max            = 60,
        min_volume_us      = 50,
        min_oi_us          = 200,
        min_volume_in      = 100,
        min_oi_in          = 1000,
        max_spread_pct     = 5.0,
        max_iv_warn        = 70.0,
        min_premium_usd    = 0.50,
        min_premium_inr    = 5.0,
        emoji              = "🛡️",
        description        = "ITM put — high delta, protective hedge or bearish directional",
    ),
    StrategyMode.BALANCED: ModeConfig(
        name               = "BALANCED (PUT)",
        target_moneyness_min = -3.0,
        target_moneyness_max = +3.0,
        delta_min          = 0.35,
        delta_max          = 0.60,
        dte_min            = 14,
        dte_max            = 35,
        min_volume_us      = 20,
        min_oi_us          = 100,
        min_volume_in      = 50,
        min_oi_in          = 500,
        max_spread_pct     = 8.0,
        max_iv_warn        = 80.0,
        min_premium_usd    = 0.20,
        min_premium_inr    = 2.0,
        emoji              = "⚖️",
        description        = "ATM put — balanced bearish hedge, moderate leverage",
    ),
    StrategyMode.AGGRESSIVE: ModeConfig(
        name               = "AGGRESSIVE (PUT)",
        target_moneyness_min = -8.0,
        target_moneyness_max = -1.0,
        delta_min          = 0.15,
        delta_max          = 0.40,
        dte_min            = 7,
        dte_max            = 21,
        min_volume_us      = 10,
        min_oi_us          = 50,
        min_volume_in      = 20,
        min_oi_in          = 200,
        max_spread_pct     = 12.0,
        max_iv_warn        = 90.0,
        min_premium_usd    = 0.05,
        min_premium_inr    = 0.50,
        emoji              = "🚀",
        description        = "OTM put — high leverage bearish play, requires strong conviction",
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# Scoring helpers
# ══════════════════════════════════════════════════════════════════════════════

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def expected_move(spot: float, iv: float, dte: int) -> dict:
    """
    Calculate ±1σ expected move for an option position.

    Formula: EM = spot × IV × sqrt(DTE / 365)
    This gives the 1-standard-deviation expected price range by expiry.
    ~68% of the time, the stock should stay within ±EM of spot.

    Parameters
    ----------
    spot : float
        Current stock price.
    iv : float
        Implied volatility as a percentage (e.g. 35.0 for 35%).
    dte : int
        Days to expiry.

    Returns
    -------
    dict with keys:
        em_dollars: float  — absolute expected move in dollars/currency
        em_pct:     float  — expected move as percentage of spot
        range_low:  float  — spot - EM (lower bound of 1σ range)
        range_high: float  — spot + EM (upper bound of 1σ range)
        dte:        int    — days to expiry used
    """
    if spot <= 0 or iv <= 0 or dte <= 0:
        return {"em_dollars": 0.0, "em_pct": 0.0,
                "range_low": spot, "range_high": spot, "dte": dte}
    iv_dec = iv / 100.0  # convert from percentage to decimal
    em = spot * iv_dec * math.sqrt(dte / 365.0)
    em_pct = (em / spot) * 100.0
    return {
        "em_dollars": round(em, 2),
        "em_pct":     round(em_pct, 2),
        "range_low":  round(spot - em, 2),
        "range_high": round(spot + em, 2),
        "dte":        dte,
    }


def iv_percentile(
    symbol: str,
    current_iv: float,
    window_days: int = 252,
) -> Optional[float]:
    """
    Compute IV percentile rank: where *current_iv* sits within the past
    *window_days* of historical (realized) volatility for *symbol*.

    Uses yfinance close prices to compute rolling 21-day annualised
    historical volatility, then returns the percentile of *current_iv*
    within that distribution.

    Returns
    -------
    float 0–100   if sufficient data exists
    None           if price history is unavailable or too short
    """
    if current_iv is None or current_iv <= 0:
        return None
    try:
        import numpy as np
        import yfinance as yf

        # Normalise symbol: "NASDAQ:AAPL" → "AAPL", "NSE:INFY" → "INFY.NS"
        if ":" in symbol:
            exch, ticker = symbol.split(":", 1)
            exch = exch.upper()
            if exch in ("NSE", "BSE"):
                yf_sym = f"{ticker}.NS"
            elif exch in ("ASX", "ASX_DLY"):
                yf_sym = f"{ticker}.AX"
            elif exch in ("HKEX", "HKEX_DLY"):
                yf_sym = f"{ticker}.HK"
            else:
                yf_sym = ticker
        else:
            yf_sym = symbol

        hist = yf.Ticker(yf_sym).history(period=f"{window_days + 30}d")
        if hist is None or len(hist) < 30:
            return None

        closes = hist["Close"].dropna()
        if len(closes) < 30:
            return None

        # 21-day annualised realised volatility (proxy for IV history)
        log_ret = np.log(closes / closes.shift(1)).dropna()
        rv_series = log_ret.rolling(21).std() * np.sqrt(252) * 100  # annualise & to %
        rv_series = rv_series.dropna()
        if len(rv_series) < 10:
            return None

        # Percentile rank: % of historical RV values below current_iv
        pct = float(np.sum(rv_series < current_iv) / len(rv_series) * 100)
        return round(_clamp(pct, 0, 100), 1)
    except Exception:
        return None


def put_call_skew(option_chain: list[dict]) -> float:
    """
    Compute directional skew from put vs call implied volatility.

    Formula: (avg_put_IV − avg_call_IV) / avg_call_IV × 100

    Interpretation:
      Positive → bearish (puts more expensive than calls)
      Negative → bullish (calls more expensive than puts)

    Returns 0.0 if fewer than 5 contracts on either side.
    """
    if not option_chain:
        return 0.0

    call_ivs: list[float] = []
    put_ivs: list[float] = []

    for c in option_chain:
        iv = c.get("iv") or c.get("implied_volatility")
        opt_type = (c.get("option_type") or c.get("type") or "").upper()
        if iv is not None and iv > 0:
            if "CALL" in opt_type or opt_type == "C":
                call_ivs.append(float(iv))
            elif "PUT" in opt_type or opt_type == "P":
                put_ivs.append(float(iv))

    if len(call_ivs) < 5 or len(put_ivs) < 5:
        return 0.0

    avg_call = sum(call_ivs) / len(call_ivs)
    avg_put = sum(put_ivs) / len(put_ivs)

    if avg_call <= 0:
        return 0.0

    return round((avg_put - avg_call) / avg_call * 100, 2)


def max_pain(option_chain: list[dict]) -> Optional[float]:
    """
    Compute the *max pain* strike — the price at which total dollar loss
    to **option holders** (= total payout from writers) is maximised.

    Equivalently, this is the strike at which the total intrinsic value of
    all outstanding options is **minimised** for holders, which is the same
    strike that is "max pain" for them.

    For each candidate strike K, sum:
      calls: sum over all call strikes Kc  of  max(0, K − Kc) × call_OI × 100
      puts:  sum over all put strikes Kp   of  max(0, Kp − K) × put_OI × 100
    Return the K that **minimises** that total (max pain to holders).

    Returns None if no OI data.
    """
    if not option_chain:
        return None

    calls: list[tuple[float, int]] = []   # (strike, OI)
    puts: list[tuple[float, int]] = []

    for c in option_chain:
        strike_val = c.get("strike") or c.get("strike_price")
        oi_val = c.get("oi") or c.get("open_interest") or 0
        opt_type = (c.get("option_type") or c.get("type") or "").upper()
        if strike_val is None or strike_val <= 0:
            continue
        if "CALL" in opt_type or opt_type == "C":
            calls.append((float(strike_val), int(oi_val)))
        elif "PUT" in opt_type or opt_type == "P":
            puts.append((float(strike_val), int(oi_val)))

    if not calls and not puts:
        return None

    # Gather all unique strikes as candidate settlement prices
    all_strikes = sorted({s for s, _ in calls} | {s for s, _ in puts})
    if not all_strikes:
        return None

    best_strike = all_strikes[0]
    best_pain = float("inf")

    for k in all_strikes:
        total = 0.0
        for kc, c_oi in calls:
            total += max(0.0, k - kc) * c_oi * 100
        for kp, p_oi in puts:
            total += max(0.0, kp - k) * p_oi * 100
        if total < best_pain:
            best_pain = total
            best_strike = k

    return round(best_strike, 2)


def _liquidity_score(volume: int, oi: int, spread_pct: float,
                     market: str, cfg: ModeConfig) -> float:
    """
    Liquidity score 0–10.
    Three factors: volume, open-interest, bid-ask spread (lowest wins).
    """
    min_vol = cfg.min_volume_us if market == "US" else cfg.min_volume_in
    min_oi  = cfg.min_oi_us     if market == "US" else cfg.min_oi_in

    after_hours = (volume == 0)
    # OI may be unavailable (=0) for Indian markets via Angel One optionGreek API
    oi_unavailable = (market in ("NSE", "BSE") and oi == 0)

    # Hard gates: reject if below floor (but skip volume gate when market is closed)
    if (not after_hours and volume < min_vol) or (not oi_unavailable and oi < min_oi):
        return 0.0

    # Spread: 0% = perfect (10pts), threshold = 0 pts
    spread_score = _clamp(10 * (1 - spread_pct / cfg.max_spread_pct), 0, 10)

    # Volume score — 0 when market closed (no penalty, just no reward)
    if after_hours or volume == 0:
        vol_score = 0.0
    else:
        vol_ratio = volume / max(min_vol, 1)
        vol_score = _clamp(10 * math.log10(max(vol_ratio, 1)) / math.log10(10), 0, 10)

    # OI score (log scale — 10× threshold = 10 pts); skip when unavailable
    if oi_unavailable:
        oi_score = 5.0   # neutral score — no data to penalise
    else:
        oi_ratio = oi / max(min_oi, 1)
        oi_score = _clamp(10 * math.log10(max(oi_ratio, 1)) / math.log10(10), 0, 10)

    # Weighted: spread 35%, OI 40%, volume 25%
    score = spread_score * 0.35 + oi_score * 0.40 + vol_score * 0.25
    return round(score, 2)


def _iv_score(iv: Optional[float]) -> tuple[float, list[str]]:
    """
    IV score 0–10 + list of warnings.
    Lower IV inside a reasonable band is better for buys (cheaper premium).
    IV bands (US equity typical range 15-60%):
      < 10%         → possibly stale data, score 5.0 + warning
      10-20%        → cheap, good entry,         score 8-10
      20-35%        → moderate,                  score 7-9
      35-60%        → elevated, use caution,     score 5-7
      60-80%        → high, penalise,            score 2-5 + warning
      > 80%         → extreme, avoid,            score 0-2 + warning
    India options tend to have higher IV (25-80% typical).
    """
    warnings: list[str] = []
    if iv is None:
        return 5.0, []   # unknown → neutral

    if iv < 5:
        warnings.append("⚠️ IV very low (<5%) — may be stale data")
        return 5.0, warnings

    if iv <= 20:
        score = _clamp(8.0 + (20 - iv) / 10, 0, 10)   # 20%→8, 10%→9, <10%→9-10
    elif iv <= 35:
        score = _clamp(10.0 - (iv - 20) / 15 * 2, 0, 10)  # 20%→10, 35%→8
    elif iv <= 60:
        score = _clamp(8.0 - (iv - 35) / 25 * 3, 0, 10)   # 35%→8, 60%→5
    elif iv <= 80:
        score = _clamp(5.0 - (iv - 60) / 20 * 4, 0, 10)   # 60%→5, 80%→1
        warnings.append(f"⚠️ IV HIGH ({iv:.0f}%) — premium overpriced, reduce size")
    else:
        score = _clamp(2.0 - (iv - 80) / 20 * 2, 0, 10)   # 80%→2, 100%→0
        warnings.append(f"🔴 IV EXTREME ({iv:.0f}%) — likely event-driven spike, AVOID")

    return round(score, 2), warnings


def _strike_score(moneyness_pct: float, cfg: ModeConfig) -> float:
    """
    Strike alignment score 0–10 based on how well moneyness fits the mode's target band.
    moneyness_pct: negative = ITM, positive = OTM, 0 = exactly ATM.
    """
    lo, hi = cfg.target_moneyness_min, cfg.target_moneyness_max
    mid = (lo + hi) / 2.0
    width = (hi - lo) / 2.0 if hi != lo else 2.0

    if lo <= moneyness_pct <= hi:
        # Inside target band: distance from center → inversely proportional
        dist = abs(moneyness_pct - mid)
        score = 10.0 - (dist / max(width, 0.1)) * 4.0   # center=10, edge=6
    else:
        dist_outside = min(abs(moneyness_pct - lo), abs(moneyness_pct - hi))
        # 0% outside: 6pts, 5% outside: 1pt, 10+ outside: 0
        score = max(0.0, 6.0 - dist_outside * 1.0)

    return round(_clamp(score, 0, 10), 2)


def _dte_score(dte: int, cfg: ModeConfig) -> float:
    """
    DTE alignment score 0–10.
    Inside window is good (score 7-10), sweet spot at ~2/3 of max.
    """
    lo, hi = cfg.dte_min, cfg.dte_max
    sweet = lo + (hi - lo) * 0.55   # sweet spot: slightly below midpoint

    if lo <= dte <= hi:
        dist = abs(dte - sweet)
        window_half = (hi - lo) / 2.0
        score = 10.0 - (dist / max(window_half, 1)) * 3.0   # sweet=10, edge=7
    elif dte > hi:
        excess = dte - hi
        score = max(0.0, 7.0 - excess / 10 * 2.0)   # grace to 40d over, then drops
    else:
        deficit = lo - dte
        score = max(0.0, 5.0 - deficit * 1.5)        # penalise short DTE hard

    return round(_clamp(score, 0, 10), 2)


def _composite_option_score(
    signal_score: float,
    liquidity: float,
    iv: float,
    strike: float,
    dte_s: float,
) -> float:
    """
    Final weighted composite option score (0–100).

    Weights (as specified in architecture):
      trend_strength   30%  ← signal composite (already 0-10 normalised here)
      liquidity        20%
      iv_quality       20%
      strike_alignment 15%
      dte_alignment    15%
    """
    return round(
        (signal_score * 0.30
         + liquidity    * 0.20
         + iv           * 0.20
         + strike       * 0.15
         + dte_s        * 0.15) * 10,    # × 10 to convert 0-10 → 0-100
        1
    )


# ══════════════════════════════════════════════════════════════════════════════
# Moneyball — Population-Relative Scoring
# ══════════════════════════════════════════════════════════════════════════════
#
# The current engine uses context-blind absolute thresholds: volume ≥ 50 is
# "good" whether the chain has 5 contracts or 500. The Moneyball approach asks:
# "Is THIS contract good RELATIVE to the other contracts in this chain?"
#
# Hybrid formula (final_score):
#   final_score = 0.60 × current_score (absolute guards) + 0.40 × moneyball_score
#
# This preserves hard guards (IV cap, premium floor) while adding population
# context. Contracts are ultimately ranked by final_score.
# ══════════════════════════════════════════════════════════════════════════════


def _mb_percentile_rank(value: float, population: list[float]) -> float:
    """Return percentile rank (0–100) of value within population."""
    if not population or len(population) < 2:
        return 50.0
    rank = sum(1.0 for v in population if v < value) + 0.5 * sum(1.0 for v in population if v == value)
    return round(100.0 * rank / len(population), 1)


def _mb_chain_stats(contracts: list[dict]) -> dict:
    """Extract population vectors from all passing contracts for percentile ranking."""
    return {
        "vols":    [c.get("volume",    0)   for c in contracts if (c.get("volume",    0)   or 0) > 0],
        "ois":     [c.get("oi",        0)   for c in contracts if (c.get("oi",        0)   or 0) > 0],
        "ivs":     [c.get("iv")             for c in contracts if c.get("iv") is not None],
        "spreads": [c.get("spread_pct", 0)  for c in contracts if (c.get("spread_pct", 0) or 0) > 0],
    }


def _mb_score_contract(c: dict, chain_stats: dict) -> float:
    """
    Moneyball population-relative score 0–100.

    Sub-scores (each 0–10, then scaled to 0–100):
      • Volume rank vs chain   25% — higher volume = better liquidity signal
      • OI rank vs chain       25% — higher OI = deeper interest, tighter spreads
      • IV rank vs chain       25% — LOWER IV = cheaper premium (inverted)
      • Spread rank vs chain   25% — LOWER spread = better fill quality (inverted)

    Each sub-score uses percentile rank within the chain population, so a
    contract with volume in the 90th percentile gets 9.0/10 — regardless of
    whether the chain's median volume is 50 or 50,000.
    """
    vol    = c.get("volume",    0) or 0
    oi     = c.get("oi",        0) or 0
    iv     = c.get("iv")
    spread = c.get("spread_pct", 0) or 0

    # Volume — higher is better
    vols = chain_stats.get("vols", [])
    vol_s = _mb_percentile_rank(vol, vols) / 10.0 if len(vols) >= 2 else 5.0

    # OI — higher is better
    ois = chain_stats.get("ois", [])
    oi_s = _mb_percentile_rank(oi, ois) / 10.0 if len(ois) >= 2 else 5.0

    # IV — lower is better for buys (inverted percentile)
    ivs = chain_stats.get("ivs", [])
    if len(ivs) >= 2 and iv is not None:
        iv_pct = _mb_percentile_rank(iv, ivs)
        iv_s = (100.0 - iv_pct) / 10.0     # 0th percentile = 10 pts, 100th = 0 pts
    else:
        iv_s = 5.0

    # Spread — lower is better (inverted)
    spreads = chain_stats.get("spreads", [])
    if len(spreads) >= 2 and spread > 0:
        sp_pct = _mb_percentile_rank(spread, spreads)
        spread_s = (100.0 - sp_pct) / 10.0
    else:
        spread_s = 5.0

    raw = vol_s * 0.25 + oi_s * 0.25 + iv_s * 0.25 + spread_s * 0.25   # 0–10
    return round(_clamp(raw * 10.0, 0.0, 100.0), 1)                     # → 0–100


# ══════════════════════════════════════════════════════════════════════════════
# Main analyzer
# ══════════════════════════════════════════════════════════════════════════════

class OptionsAnalyzer:
    """
    End-to-end options recommendation engine.

    Stateless — safe to call repeatedly. Each call creates a new data-fetch
    context internally (Moomoo / Zerodha). No persistent connections held.

    Parameters
    ----------
    verbose : bool
        Print debug output if True.

    Example
    -------
    >>> analyzer = OptionsAnalyzer()
    >>> result = analyzer.analyze("NASDAQ:AAPL", signal_score=72.5, mode=StrategyMode.BALANCED)
    >>> print(result["options_available"], len(result["top_contracts"]))
    True 3
    """

    MAX_CONTRACTS = 3   # number of recommended contracts in output

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    # ─── Public API ────────────────────────────────────────────────────────────

    def analyze(
        self,
        pro_symbol: str,
        signal_score: float = 50.0,
        mode: StrategyMode = StrategyMode.BALANCED,
        earnings_days: Optional[int] = None,
        access_token: Optional[str] = None,
        puts_mode: bool = False,
    ) -> dict:
        """
        Analyze options for *pro_symbol* given a buy signal.

        Parameters
        ----------
        pro_symbol : str
            TradingView pro_symbol, e.g. "NASDAQ:AAPL" or "NSE:INFY".
        signal_score : float
            Composite signal score 0-100 from L1-L4 analysis.
        mode : StrategyMode
            SAFE / BALANCED / AGGRESSIVE (default BALANCED).
        earnings_days : int, optional
            Days until next earnings. If <5, earnings risk warning is added.
            Fetched from the signal result dict if available.
        access_token : str, optional
            Zerodha access token (India only). Falls back to env var.

        Returns
        -------
        dict with keys:
          symbol, market, mode, options_available, fo_eligible (India),
          spot_price, warnings, earnings_risk, top_contracts (list),
          no_options_reason (if unavailable), signal_score
        """
        exchange = pro_symbol.split(":")[0].upper() if ":" in pro_symbol else "NYSE"
        if exchange in ("NSE", "BSE"):
            market = "NSE"
        elif exchange in ("HKEX", "HKEX_DLY"):
            market = "HK"
        else:
            market = "US"

        result: dict[str, Any] = {
            "symbol":            pro_symbol,
            "market":            market,
            "mode":              mode.value,
            "signal_score":      signal_score,
            "options_available": False,
            "fo_eligible":       True,
            "spot_price":        None,
            "warnings":          [],
            "earnings_risk":     False,
            "top_contracts":     [],
            "no_options_reason": None,
        }

        # ── Earnings risk check (global — applies to US and India) ──────────────
        if earnings_days is not None and earnings_days < 5:
            result["earnings_risk"] = True
            result["warnings"].append(
                f"🔴 EARNINGS IN {earnings_days} DAYS — IV spike risk, options overpriced. "
                "Consider waiting until after the report or trade stock instead."
            )

        # ── Market-specific eligibility & data fetch ───────────────────────────
        if market == "NSE":
            symbol_clean = pro_symbol.split(":")[-1].upper().replace("-EQ", "")
            if not is_fo_eligible(symbol_clean):
                result["fo_eligible"]       = False
                result["no_options_reason"] = (
                    f"{symbol_clean} is NOT in NSE F&O eligible list. "
                    "Only ~225 stocks have NSE derivatives."
                )
                return result
            contracts = self._fetch_india(pro_symbol, access_token)
        elif market == "HK":
            contracts = self._fetch_hk(pro_symbol)
        else:
            contracts = self._fetch_us(pro_symbol)

        # Track which data source was used (for badge display)
        result["data_source"] = (
            contracts[0].get("source", "moomoo") if contracts else "none"
        )

        if not contracts:
            result["no_options_reason"] = (
                "Could not fetch option chain. "
                "Moomoo OpenD offline and yfinance returned no data (US/HK). "
                "Angel One API unavailable (India)."
            )
            return result

        result["options_available"] = True
        if contracts:
            result["spot_price"] = contracts[0].get("spot_price")

        # ── Filter + score ────────────────────────────────────────────────────
        cfg_map = PUT_MODE_CONFIGS if puts_mode else MODE_CONFIGS
        cfg = cfg_map[mode]
        result["puts_mode"] = puts_mode
        ranked = self._filter_and_score(contracts, signal_score, cfg)

        if not ranked:
            result["options_available"] = False
            # Mode-aware suggestion: don't tell a SAFE user to "try BALANCED"
            _mode_suggest = {
                "AGGRESSIVE": "Try BALANCED or SAFE mode to widen filters.",
                "BALANCED": "Try SAFE mode — it uses the widest liquidity filter.",
                "SAFE": (
                    "No contracts passed even in SAFE (widest-filter) mode. "
                    "Symbol may not have listed options, or market is closed."
                ),
            }
            result["no_options_reason"] = (
                f"No contracts passed liquidity + mode ({mode.value}) filters. "
                + _mode_suggest.get(mode.value, "Widen strategy mode or retry at market open.")
            )
            return result

        # ── Top-N results ─────────────────────────────────────────────────────
        top = ranked[: self.MAX_CONTRACTS]
        for c in top:
            c["recommendation_reason"] = self._build_reason(c, cfg, mode)
            # Add expected move for each contract
            _spot = c.get("spot_price") or result.get("spot_price") or 0
            _iv = c.get("iv") or 0
            _dte = c.get("dte") or 0
            c["expected_move"] = expected_move(_spot, _iv, _dte)
        result["top_contracts"] = top

        # Aggregate warnings from all top contracts
        for c in top:
            result["warnings"].extend(c.pop("_contract_warnings", []))
        result["warnings"] = list(dict.fromkeys(result["warnings"]))  # dedupe

        return result

    def analyze_all_modes(
        self,
        pro_symbol: str,
        signal_score: float = 50.0,
        earnings_days: Optional[int] = None,
        access_token: Optional[str] = None,
        include_all_contracts: bool = False,
    ) -> dict:
        """
        Run analysis for ALL THREE modes (SAFE, BALANCED, AGGRESSIVE) using a
        single option chain fetch.  Returns a dict keyed by mode name.

        Returns
        -------
        dict with keys:
          "SAFE"       -> result dict (same shape as analyze())
          "BALANCED"   -> result dict
          "AGGRESSIVE" -> result dict
          "meta"       -> {"symbol", "market", "spot_price", "earnings_risk",
                           "data_source", "signal_score"}
        """
        exchange = pro_symbol.split(":")[0].upper() if ":" in pro_symbol else "NYSE"
        if exchange in ("NSE", "BSE"):
            market = "NSE"
        elif exchange in ("HKEX", "HKEX_DLY"):
            market = "HK"
        else:
            market = "US"

        # ── Shared earnings check ────────────────────────────────────────────
        earnings_risk = earnings_days is not None and earnings_days < 5
        shared_warnings: list[str] = []
        if earnings_risk:
            shared_warnings.append(
                f"🔴 EARNINGS IN {earnings_days} DAYS — IV spike risk, options overpriced. "
                "Consider waiting until after the report or trade stock instead."
            )

        # ── India eligibility (checked once) ────────────────────────────────
        if market == "NSE":
            symbol_clean = pro_symbol.split(":")[-1].upper().replace("-EQ", "")
            if not is_fo_eligible(symbol_clean):
                _not_fo = {
                    "symbol":            pro_symbol,
                    "market":            market,
                    "signal_score":      signal_score,
                    "options_available": False,
                    "fo_eligible":       False,
                    "spot_price":        None,
                    "warnings":          [],
                    "earnings_risk":     earnings_risk,
                    "top_contracts":     [],
                    "no_options_reason": (
                        f"{symbol_clean} is NOT in NSE F&O eligible list. "
                        "Only ~225 stocks have NSE derivatives."
                    ),
                }
                return {
                    "SAFE":       {**_not_fo, "mode": "SAFE"},
                    "BALANCED":   {**_not_fo, "mode": "BALANCED"},
                    "AGGRESSIVE": {**_not_fo, "mode": "AGGRESSIVE"},
                    "meta": {"symbol": pro_symbol, "market": market, "spot_price": None,
                             "earnings_risk": earnings_risk, "data_source": "none",
                             "signal_score": signal_score},
                }

        # ── Single chain fetch ───────────────────────────────────────────────
        if market == "NSE":
            contracts = self._fetch_india(pro_symbol, access_token)
        elif market == "HK":
            contracts = self._fetch_hk(pro_symbol)
        else:
            # Scanner work already runs in a scheduler-bounded child process.
            # Fetch synchronously there so no timed-out worker can outlive an
            # abandoned result; legacy callers keep their timed worker path.
            contracts = (self._fetch_us_scanner(pro_symbol) if include_all_contracts
                         else self._fetch_us(pro_symbol))

        scanner_capabilities = None
        if market == "US" and include_all_contracts:
            scanner_capabilities = self._scanner_contract_capabilities(contracts)

        data_source = contracts[0].get("source", "none") if contracts else "none"
        spot_price = contracts[0].get("spot_price") if contracts else None

        _base = {
            "symbol":        pro_symbol,
            "market":        market,
            "signal_score":  signal_score,
            "warnings":      list(shared_warnings),
            "earnings_risk": earnings_risk,
            "data_source":   data_source,
            "spot_price":    spot_price,
        }

        if not contracts:
            _no = {
                **_base,
                "options_available": False,
                "top_contracts":     [],
                "no_options_reason": (
                    "Could not fetch option chain. "
                    "Moomoo OpenD offline and yfinance returned no data."
                ),
            }
            return {
                "SAFE":       {**_no, "mode": "SAFE",       "fo_eligible": True},
                "BALANCED":   {**_no, "mode": "BALANCED",   "fo_eligible": True},
                "AGGRESSIVE": {**_no, "mode": "AGGRESSIVE", "fo_eligible": True},
                "meta": {**_base, "data_source": "none"},
            }

        results = {}
        for mode in (StrategyMode.SAFE, StrategyMode.BALANCED, StrategyMode.AGGRESSIVE):
            cfg    = MODE_CONFIGS[mode]
            ranked = self._filter_and_score(list(contracts), signal_score, cfg)

            r: dict = {
                **_base,
                "mode":              mode.value,
                "fo_eligible":       True,
                "options_available": bool(ranked),
                "top_contracts":     [],
                "no_options_reason": None,
                "warnings":          list(shared_warnings),
            }

            if ranked:
                # Scanner-origin strict quote/liquidity gates run after scoring.
                # Let that caller inspect the full ranked result so an eligible
                # lower-ranked candidate is not hidden by the legacy display cap.
                top = ranked if include_all_contracts else ranked[: self.MAX_CONTRACTS]
                for c in top:
                    c["recommendation_reason"] = self._build_reason(c, cfg, mode)
                r["top_contracts"] = top
                for c in top:
                    r["warnings"].extend(c.pop("_contract_warnings", []))
                r["warnings"] = list(dict.fromkeys(r["warnings"]))
            else:
                _mode_suggest = {
                    "AGGRESSIVE": "Try BALANCED or SAFE mode to widen filters.",
                    "BALANCED":   "Try SAFE mode — it uses the widest liquidity filter.",
                    "SAFE":       (
                        "No contracts passed even in SAFE mode. "
                        "Symbol may not have listed options, or market is closed."
                    ),
                }
                r["no_options_reason"] = (
                    f"No contracts passed liquidity + {mode.value} filters. "
                    + _mode_suggest.get(mode.value, "")
                )

            results[mode.value] = r

        meta = {
            "symbol":        pro_symbol,
            "market":        market,
            "spot_price":    spot_price,
            "earnings_risk": earnings_risk,
            "data_source":   data_source,
            "signal_score":  signal_score,
        }
        if scanner_capabilities is not None:
            meta["scanner_provider_capabilities"] = scanner_capabilities
        results["meta"] = meta
        return results

    # ─── Data fetchers ────────────────────────────────────────────────────────

    def _fetch_us(self, pro_symbol: str) -> list[dict]:
        """Fetch US options via Moomoo → yfinance fallback, with 30s timeout."""
        def _inner() -> list[dict]:
            # ── Try Moomoo OpenD first (real-time, with full Greeks) ──────────────
            try:
                from .us_options import USOptionsFetcher, tv_to_mm
                mm_code = tv_to_mm(pro_symbol)
                if mm_code:
                    fetcher = USOptionsFetcher(verbose=self.verbose)
                    if fetcher.is_available():
                        contracts = fetcher.get_calls(mm_code, n_expiries=3)
                        # Guard: Moomoo sometimes returns all-zero data (weekend /
                        # after-hours cache). Fall through to yfinance if so.
                        if contracts and any(c.get("oi", 0) > 0 for c in contracts):
                            for c in contracts:
                                c.setdefault("source", "moomoo")
                            return contracts
                        if contracts and self.verbose:
                            print(f"[OptionsAnalyzer] _fetch_us Moomoo data all-zero OI for {pro_symbol}; falling back to yfinance")
            except Exception as exc:
                if self.verbose:
                    print(f"[OptionsAnalyzer] _fetch_us Moomoo({pro_symbol}): {exc}")

            # ── Fall back to yfinance (free, no auth, B-S delta) ──────────────────
            # Also fall back if Moomoo returned all-zero data (weekend / stale cache)
            try:
                from .yf_options import YFOptionsFetcher, is_us_symbol
                if is_us_symbol(pro_symbol):
                    yf_fetcher = YFOptionsFetcher(verbose=self.verbose)
                    if yf_fetcher.is_available():
                        contracts = yf_fetcher.get_calls(pro_symbol, n_expiries=5)
                        if contracts:
                            return contracts
            except Exception as exc:
                if self.verbose:
                    print(f"[OptionsAnalyzer] _fetch_us yfinance({pro_symbol}): {exc}")

            return []

        # Golden Rule #16: ThreadPoolExecutor + shutdown(wait=False)
        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _fut = _ex.submit(_inner)
        try:
            return _fut.result(timeout=30)
        except (concurrent.futures.TimeoutError, Exception) as exc:
            if self.verbose:
                print(f"[OptionsAnalyzer] _fetch_us({pro_symbol}) timeout/error: {exc}")
            return []
        finally:
            _ex.shutdown(wait=False)

    def _fetch_us_scanner(self, pro_symbol: str) -> list[dict]:
        """Fetch a single TradingView scan2 chain for the bounded scanner child.

        This scanner-only path deliberately bypasses HybridOptionsFetcher: its
        WebSocket and TQS enrichment are separate scoring inputs and its Moomoo
        fallback can substitute last-session prices. Existing WIN callers keep
        their Moomoo/yfinance path in ``_fetch_us``.
        """
        try:
            from .tv_options import TVOptionsFetcher
            fetcher = TVOptionsFetcher(verbose=self.verbose)
            if not fetcher.is_available():
                raise USScannerOptionsUnavailable("TradingView options provider unavailable")
            scan_timestamp = os.environ.get("SIGNAL_SCANNER_TIMESTAMP")
            if scan_timestamp:
                raw_now = datetime.fromisoformat(scan_timestamp.replace("Z", "+00:00"))
                if raw_now.tzinfo is None:
                    raise USScannerOptionsUnavailable("scanner timestamp must include timezone")
                ny_date = raw_now.astimezone(ZoneInfo("America/New_York")).date()
            else:
                ny_date = datetime.now(ZoneInfo("America/New_York")).date()
            try:
                min_dte = int(os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MIN_DTE", "14"))
                max_dte = int(os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_MAX_DTE", "45"))
            except ValueError as exc:
                raise USScannerOptionsUnavailable("scanner DTE window is invalid") from exc
            contracts = fetcher.get_calls(
                pro_symbol, n_expiries=3, min_dte=min_dte, max_dte=max_dte,
                reference_date=ny_date,
            )
            if not contracts:
                raise USScannerOptionsUnavailable("TradingView options provider returned no chain")
            # The provider adapter calculates DTE against host-local date.
            # Scanner scoring and expiry policy use the New York session date.
            for contract in contracts:
                contract.setdefault("source", "tv")
                contract["quote_provider"] = "tradingview_scan2"
                try:
                    contract["dte"] = (date.fromisoformat(str(contract["expiry"])) - ny_date).days
                except (KeyError, TypeError, ValueError):
                    contract["dte"] = None
            # scan2 does not report OI. In the explicit reference mode, enrich
            # only exact expiry/strike matches from Yahoo's reported chain OI.
            # No zero/default is promoted to evidence and this never uses TQS.
            if os.environ.get("SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE", "strict").strip().lower() == "reference":
                from .yf_options import YFOptionsFetcher
                yf_fetcher = YFOptionsFetcher(verbose=self.verbose)
                expiry_cache: dict[str, dict[str, int]] = {}
                for contract in contracts:
                    expiry = str(contract.get("expiry") or "")
                    if not expiry:
                        continue
                    if expiry not in expiry_cache and len(expiry_cache) < 3:
                        oi_by_strike: dict[str, int] = {}
                        for yf_contract in yf_fetcher.get_option_chain_for_date(pro_symbol, expiry, option_type="CALL"):
                            try:
                                if (str(yf_contract.get("expiry") or "") != expiry
                                        or str(yf_contract.get("option_type") or "").upper() != "CALL"):
                                    continue
                                oi_raw = yf_contract.get("oi")
                                if isinstance(oi_raw, bool) or oi_raw is None:
                                    continue
                                oi_decimal = Decimal(str(oi_raw))
                                if not oi_decimal.is_finite() or oi_decimal != oi_decimal.to_integral_value() or oi_decimal < 0:
                                    continue
                                oi_value = int(oi_decimal)
                                strike_decimal = Decimal(str(yf_contract.get("strike")))
                                if not strike_decimal.is_finite() or strike_decimal <= 0:
                                    continue
                                strike_value = float(strike_decimal)
                                if oi_value >= 0 and strike_value > 0:
                                    oi_by_strike[format(strike_decimal.normalize(), "f")] = oi_value
                            except (InvalidOperation, TypeError, ValueError, OverflowError):
                                continue
                        expiry_cache[expiry] = oi_by_strike
                    try:
                        strike_key = format(Decimal(str(contract.get("strike"))).normalize(), "f")
                    except (InvalidOperation, TypeError, ValueError, OverflowError):
                        continue
                    matched_oi = expiry_cache.get(expiry, {}).get(strike_key)
                    if matched_oi is not None:
                        contract["oi"] = matched_oi
                        contract["oi_source"] = "yfinance_exact_expiry_strike"
                # Infer the volume session only when the update and scanner
                # run both fall inside that date's actual US regular session.
                for contract in contracts:
                    try:
                        raw_update = contract["tv_update_time"]
                        tv_update = (datetime.fromtimestamp(float(raw_update), timezone.utc)
                                     if isinstance(raw_update, (int, float)) else
                                     datetime.fromisoformat(str(raw_update).replace("Z", "+00:00")))
                        scan_now = datetime.fromisoformat(scan_timestamp.replace("Z", "+00:00")) if scan_timestamp else None
                        if tv_update.tzinfo is None or scan_now is None or scan_now.tzinfo is None:
                            continue
                        from tradingview.scheduler.market_calendar import market_session_bounds
                        update_date = tv_update.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                        session_open, session_close = market_session_bounds("US", update_date)
                        if (float(contract.get("volume") or 0) > 0
                                and scan_now.astimezone(ZoneInfo("America/New_York")).date().isoformat() == update_date
                                and session_open <= tv_update.astimezone(timezone.utc) <= session_close
                                and session_open <= scan_now.astimezone(timezone.utc) < session_close):
                            contract["volume_session_provenance"] = "inferred_from_current_tv_update"
                    except (KeyError, TypeError, ValueError, OverflowError, OSError):
                        pass
            # Keep generic update_time separate from side-quote provenance.
            for contract in contracts:
                contract["scanner_provider_capabilities"] = {
                    "provider": "tradingview_scan2",
                    "independent_bid_ask_timestamps": bool(contract.get("bid_quote_at") and contract.get("ask_quote_at")),
                    "open_interest_with_provenance": bool(contract.get("oi_source") and contract.get("oi") is not None),
                    "session_volume_with_date": bool(contract.get("volume_session_date") or contract.get("volume_session_provenance")),
                }
            return contracts
        except Exception as exc:
            if isinstance(exc, USScannerOptionsUnavailable):
                raise
            if self.verbose:
                print(f"[OptionsAnalyzer] TradingView scanner fetch failed: {type(exc).__name__}")
            raise USScannerOptionsUnavailable(
                f"TradingView scanner options fetch failed ({type(exc).__name__})"
            ) from None

    @staticmethod
    def _scanner_contract_capabilities(contracts: list[dict]) -> dict[str, Any] | None:
        """Summarize evidence capabilities independently of scored survivors."""
        tagged = [c.get("scanner_provider_capabilities") for c in contracts
                  if isinstance(c.get("scanner_provider_capabilities"), dict)]
        if not tagged:
            return None
        capabilities = [c.get("scanner_provider_capabilities")
                        if isinstance(c.get("scanner_provider_capabilities"), dict)
                        else {} for c in contracts]
        first = tagged[0]
        return {
            "provider": first.get("provider", "unknown"),
            "independent_bid_ask_timestamps": all(c.get("independent_bid_ask_timestamps") is True for c in capabilities),
            "open_interest_with_provenance": all(c.get("open_interest_with_provenance") is True for c in capabilities),
            "session_volume_with_date": all(c.get("session_volume_with_date") is True for c in capabilities),
        }

    def _fetch_hk(self, pro_symbol: str) -> list[dict]:
        """Fetch HK options via Moomoo OpenD (HK.XXXXX codes), with 30s timeout."""
        def _inner() -> list[dict]:
            try:
                from .hk_options import HKOptionsFetcher, tv_to_mm_hk, is_hk_optionable
                if not is_hk_optionable(pro_symbol):
                    return []
                mm_code = tv_to_mm_hk(pro_symbol)
                if not mm_code:
                    return []
                fetcher = HKOptionsFetcher(verbose=self.verbose)
                if not fetcher.is_available():
                    return []
                contracts = fetcher.get_calls(mm_code, n_expiries=2)
                for c in contracts:
                    c.setdefault("source", "moomoo")
                return contracts
            except Exception as exc:
                if self.verbose:
                    print(f"[OptionsAnalyzer] _fetch_hk({pro_symbol}): {exc}")
                return []

        # Golden Rule #16: ThreadPoolExecutor + shutdown(wait=False)
        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _fut = _ex.submit(_inner)
        try:
            return _fut.result(timeout=30)
        except (concurrent.futures.TimeoutError, Exception) as exc:
            if self.verbose:
                print(f"[OptionsAnalyzer] _fetch_hk({pro_symbol}) timeout/error: {exc}")
            return []
        finally:
            _ex.shutdown(wait=False)

    def _fetch_india(self, pro_symbol: str, access_token: Optional[str]) -> list[dict]:
        """Fetch India options via AngelOne SmartConnect, with 30s timeout."""
        def _inner() -> list[dict]:
            try:
                from .india_options import IndiaOptionsFetcher
                fetcher = IndiaOptionsFetcher(verbose=self.verbose)
                if not fetcher.is_available():
                    return []
                return fetcher.get_calls(pro_symbol, access_token=access_token, n_expiries=3)
            except Exception as exc:
                if self.verbose:
                    print(f"[OptionsAnalyzer] _fetch_india({pro_symbol}): {exc}")
                return []

        # Golden Rule #16: ThreadPoolExecutor + shutdown(wait=False)
        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _fut = _ex.submit(_inner)
        try:
            return _fut.result(timeout=30)
        except (concurrent.futures.TimeoutError, Exception) as exc:
            if self.verbose:
                print(f"[OptionsAnalyzer] _fetch_india({pro_symbol}) timeout/error: {exc}")
            return []
        finally:
            _ex.shutdown(wait=False)

    # ─── Filter + score pipeline ──────────────────────────────────────────────

    def _filter_and_score(
        self,
        contracts: list[dict],
        signal_score: float,
        cfg: ModeConfig,
    ) -> list[dict]:
        """
        Apply hard filters then compute composite score for each contract.
        Returns list sorted by score descending.
        """
        scored: list[dict] = []
        # Normalise signal score 0-100 → 0-10 for the formula
        trend_s = _clamp(signal_score / 100.0 * 10.0, 0, 10)

        for c in contracts:
            market       = c.get("market", "US")
            volume       = c.get("volume", 0)
            oi           = c.get("oi", 0)
            spread_pct   = c.get("spread_pct", 999)
            dte          = c.get("dte", 0)
            mid_price    = c.get("mid_price", 0)
            iv           = c.get("iv")
            moneyness    = c.get("moneyness_pct", 0)

            contract_warnings: list[str] = []

            # ── Hard liquidity filter ──────────────────────────────────────
            min_vol = cfg.min_volume_us if market == "US" else cfg.min_volume_in
            min_oi  = cfg.min_oi_us     if market == "US" else cfg.min_oi_in

            after_hours = (volume == 0)
            # OI may be 0 when the data source doesn't provide OI (e.g. Angel One
            # optionGreek API returns Greeks + volume but not OI).  Treat oi=0
            # as "data unavailable" for Indian markets rather than a hard reject.
            oi_unavailable = (market in ("NSE", "BSE") and oi == 0)
            if (not after_hours and volume < min_vol) or (not oi_unavailable and oi < min_oi) or (not after_hours and spread_pct > cfg.max_spread_pct):
                continue   # reject illiquid
            if after_hours:
                contract_warnings.append(
                    "⚠️ AFTER HOURS — volume=0, OI-only analysis (verify before trading)"
                )
            if oi_unavailable:
                contract_warnings.append("ℹ️ OI data not available from this source")

            # ── DTE range filter ──────────────────────────────────────────
            if dte < max(3, cfg.dte_min - 5) or dte > cfg.dte_max + 20:
                continue   # outside window (with grace margin)

            # ── Premium floor (cheap trap filter) ─────────────────────────
            min_prem = cfg.min_premium_usd if market == "US" else cfg.min_premium_inr
            if mid_price < min_prem:
                contract_warnings.append(f"⚠️ LOW PREMIUM ({mid_price:.2f}) — cheap trap risk")

            # ── IV check ──────────────────────────────────────────────────
            iv_s, iv_warns = _iv_score(iv)
            contract_warnings.extend(iv_warns)

            # Earnings risk penalty (if provided at contract level)
            earnings_penalty = 0.0

            # ── Score components ──────────────────────────────────────────
            liq_s    = _liquidity_score(volume, oi, spread_pct, market, cfg)
            strike_s = _strike_score(moneyness, cfg)
            dte_s    = _dte_score(dte, cfg)

            composite = _composite_option_score(
                trend_s, liq_s, iv_s, strike_s, dte_s
            )

            # Apply earnings penalty (heavy IV decay risk)
            # Already warned in analyze()  — just reduce score
            # (This would be set per-contract if earnings_days were per-symbol)

            scored.append({
                **c,
                "_contract_warnings": contract_warnings,
                "liq_score":     liq_s,
                "iv_score":      iv_s,
                "strike_score":  strike_s,
                "dte_score":     dte_s,
                "option_score":  composite,
                "current_score": composite,  # absolute score — 60% component in Moneyball final_score
            })

        # ── Moneyball population-relative scoring ──────────────────────────
        # Compute after filtering so chain_stats reflect only eligible contracts.
        # Requires ≥2 contracts to have a meaningful population; single-contract
        # chains skip Moneyball and preserve the raw absolute score.
        chain_stats = _mb_chain_stats(scored)
        _use_mb = len(scored) >= 2
        for c in scored:
            if _use_mb:
                mb = _mb_score_contract(c, chain_stats)
                c["moneyball_score"] = mb
                # Hybrid: 60% absolute guards + 40% population context
                c["final_score"] = round(0.60 * c["current_score"] + 0.40 * mb, 1)
            else:
                # Single-contract chain: no population to rank against.
                c["moneyball_score"] = c["current_score"]
                c["final_score"]     = c["current_score"]
            # GUARDRAIL: option_score is the canonical ranking key used by ALL
            # consumers (run_options.py sort, display, DB save, badge, report).
            # Always update it here so no consumer ever silently uses the pre-Moneyball score.
            c["option_score"] = c["final_score"]
            # mb_active flag: True when ≥2 contracts in chain → Moneyball ran with real population.
            # False for single-contract chains where moneyball_score == current_score.
            # Used by display layers (options_report.py, options_badge.py) to conditionally
            # show the score breakdown without rendering redundant identical values.
            c["mb_active"] = _use_mb

        scored.sort(key=lambda x: -x["option_score"])
        return scored

    # ─── Recommendation reason builder ────────────────────────────────────────

    def _build_reason(self, c: dict, cfg: ModeConfig, mode: StrategyMode) -> str:
        """Build a human-readable reason string for a recommended contract."""
        market     = c.get("market", "US")
        currency   = "₹" if market in ("NSE", "BSE") else "$"
        strike     = c.get("strike", 0)
        expiry     = c.get("expiry", "?")
        mid        = c.get("mid_price", 0)
        iv         = c.get("iv")
        dte        = c.get("dte", 0)
        delta      = c.get("delta", 0)
        moneyness  = c.get("moneyness_pct", 0)
        liq_s      = c.get("liq_score", 0)
        iv_s       = c.get("iv_score", 0)
        strike_s   = c.get("strike_score", 0)
        dte_s      = c.get("dte_score", 0)
        opt_score  = c.get("option_score", 0)

        # Moneyness label
        if moneyness < -3:
            m_label = f"ITM ({moneyness:+.1f}%)"
        elif moneyness > 3:
            m_label = f"OTM ({moneyness:+.1f}%)"
        else:
            m_label = f"ATM ({moneyness:+.1f}%)"

        iv_str = f"{iv:.0f}%" if iv else "N/A"

        # Score bar (visual)
        filled = round(opt_score / 10)
        bar    = "█" * filled + "░" * (10 - filled)

        reason_parts = [f"{m_label} | IV {iv_str}"]
        if delta:
            reason_parts.append(f"Δ {delta:.2f}")
        if liq_s >= 7:
            reason_parts.append("✅ Liquid")
        elif liq_s < 3:
            reason_parts.append("⚠️ Low Liquidity")
        if dte_s >= 7:
            reason_parts.append(f"⏰ {dte}d DTE (ideal)")
        else:
            reason_parts.append(f"⏰ {dte}d DTE")
        if iv_s < 3:
            reason_parts.append("🔴 IV HIGH")
        elif iv_s >= 8:
            reason_parts.append("💚 IV Cheap")

        mode_cfg = MODE_CONFIGS[mode]
        reason_parts.append(mode_cfg.emoji + " " + mode_cfg.name)

        return " | ".join(reason_parts)
