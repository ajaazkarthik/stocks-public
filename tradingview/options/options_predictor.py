"""
Options Prediction Engine
==========================
Predicts win probability and target premium for option recommendations.

Two WIN definitions:
1. ITM Win  : Option expires in-the-money (spot > strike for CALL).
2. Premium Win: Option reaches target-premium multiple before expiry —
               even early exit counts as a WIN.

Model Architecture
------------------
P(WIN) = N(d2)                [Black-Scholes risk-neutral P(ITM)]
       + signal_alpha          [directional conviction from signal_score]
       + moneyball_alpha        [chain-relative quality from moneyball_score]

Target Premium = BS_price(target_spot, half_DTE)
  where  target_spot = spot × (1 + expected_move)
         expected_move derived from signal_score via empirical sigmoid mapping
         half_DTE is conservative (assumes move happens midway through contract life)

Expected Value
--------------
EV = P(WIN) × (target_premium − entry) − P(LOSE) × entry
EV_ratio = EV / entry    (positive = expected profit per dollar invested)

No external dependencies — uses Python stdlib only (math.erfc for exact normal CDF).
"""

from __future__ import annotations

import math
from typing import Optional


# ── Risk-free rate ────────────────────────────────────────────────────────────
_DEFAULT_RISK_FREE_RATE = 0.05   # 5% (US Fed Funds, 2026 approximation)


# ── Normal CDF — exact via complementary error function ──────────────────────
def _norm_cdf(x: float) -> float:
    """Standard normal CDF — exact using Python's built-in math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


# ── Black-Scholes Core ────────────────────────────────────────────────────────
def _bs_d1_d2(S: float, K: float, T: float, sigma: float, r: float):
    """
    Compute Black-Scholes d1, d2 for a European call.

    Parameters
    ----------
    S     : spot price
    K     : strike price
    T     : time to expiry in years  (DTE / 365)
    sigma : annualised volatility     (IV_pct / 100)
    r     : risk-free rate (annual, decimal)

    Returns  (d1, d2) or (None, None) if inputs are degenerate.
    """
    if T <= 0 or sigma <= 0.001 or S <= 0 or K <= 0:
        return None, None
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def _bs_call_price(S: float, K: float, T: float, sigma: float, r: float) -> float:
    """Standard Black-Scholes European call price."""
    d1, d2 = _bs_d1_d2(S, K, T, sigma, r)
    if d1 is None:
        return max(0.0, S - K)   # intrinsic value fallback
    price = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return max(0.0, price)


def _bs_put_price(S: float, K: float, T: float, sigma: float, r: float) -> float:
    """Black-Scholes European put price via put-call parity."""
    call = _bs_call_price(S, K, T, sigma, r)
    return max(0.0, call - S + K * math.exp(-r * T))


def _bs_p_itm(S: float, K: float, T: float, sigma: float, r: float,
              option_type: str = "CALL") -> float:
    """
    Risk-neutral P(ITM at expiry) = N(d2) for CALL, N(-d2) for PUT.

    This is the Black-Scholes *market* estimate of the option ending in-the-money.
    Delta ≈ N(d1) is the hedge ratio; N(d2) is the true risk-neutral probability.
    """
    _, d2 = _bs_d1_d2(S, K, T, sigma, r)
    if d2 is None:
        return 0.5
    if option_type.upper() == "CALL":
        return _norm_cdf(d2)
    return _norm_cdf(-d2)   # PUT: P(ITM) = P(S_T < K) = N(-d2)


# ── Signal → Expected Move ────────────────────────────────────────────────────
def _signal_expected_move(signal_score: float) -> float:
    """
    Map composite signal score → expected underlying % move (decimal).

    Empirical calibration of the L1-L4 scoring system against NSE/US stock
    returns at DTE-length holding period.  Conservative sigmoid segments:

      Score  50-55 → 1-2%  (very mild conviction)
      Score  55-65 → 2-4%  (mild bullish)
      Score  65-75 → 4-7%  (moderate conviction)
      Score  75-85 → 7-10% (strong conviction)
      Score  85-100→ 10-14% (apex)
    """
    s = max(50.0, min(100.0, float(signal_score)))
    if s < 55:
        return 0.01 + (s - 50) / 5.0  * 0.01
    if s < 65:
        return 0.02 + (s - 55) / 10.0 * 0.02
    if s < 75:
        return 0.04 + (s - 65) / 10.0 * 0.03
    if s < 85:
        return 0.07 + (s - 75) / 10.0 * 0.03
    return min(0.14, 0.10 + (s - 85) / 15.0 * 0.04)


# ── Calibration Status ────────────────────────────────────────────────────────
# Empirical validation (2026-05-07):
#
# A) Pearson r (composite_score vs 7d stock return, n≈900):  r = -0.006
#    R-squared = 0.00% — confirms near-zero linear predictive power.
#
# B) Retroactive ATM-call calibration (2426 alerts, yfinance 7d return ≥ VIX-adj
#    breakeven):
#    Overall ATM-call win rate: 51.9% (≈ fair coin — market-efficient baseline)
#    By signal_score bucket (hypothetical ATM CALL win):
#      Score 50: 52.0% (n=1057)  Score 55: 54.4% (n=789)  Score 65: 50.2% (n=294)
#      Score 70: 53.5% (n=109)   Score 75: 57.0% (n=79)   Score 80: 46.2% (n=26, ⚠ small)
#    Key finding: signal provides ~+1-7pp edge at score 55-75; scores 80+ show
#    NEGATIVE edge in this dataset (only 26 obs — inconclusive but not bullish).
#    Interpretation: alpha is tiny and non-monotonic; NOT +12pp as the formula assumes.
#
# C) Options outcome records with settled WIN/LOSS: 0 of 1079 (first expiry 2026-05-08)
#
# CONSEQUENCE: signal_alpha and moneyball_alpha are UNCALIBRATED.
# P(WIN_adjusted) = hypothesis only.
# Run outcome_tracker.py daily after options expire to accumulate real calibration.
# Set _ALPHA_CALIBRATED = True when calibrate_from_db() returns ready=True (≥200 recs).
_ALPHA_CALIBRATED:   bool  = False
_EMPIRICAL_SIGNAL_R: float = -0.006          # Pearson r vs 7d stock return
_RETRO_WIN_RATES:    dict  = {               # hypothetical ATM CALL win rates
    50: 0.520, 55: 0.544, 65: 0.502,         # from 2426 historical alert returns
    70: 0.535, 75: 0.570, 80: 0.462,
}
_CALIBRATION_DATE:   str   = "2026-05-07"
_MIN_OUTCOMES_TO_CALIBRATE: int = 200


# ── Alpha Adjustments ─────────────────────────────────────────────────────────
def _signal_alpha(signal_score: float) -> float:
    """
    Hypothetical probability adjustment from signal score.

    *** UNCALIBRATED *** — empirical r = -0.006 (see _ALPHA_CALIBRATED).
    Do not use this value as a calibrated edge until empirically validated.

    Neutral (score=50) → 0.00 adjustment
    Strong  (score=80) → +0.09 adjustment
    Apex    (score=85+)→ +0.12 adjustment  (hard cap)
    """
    s = float(signal_score or 50.0)
    if s <= 50.0:
        return 0.0
    return min(0.12, (s - 50.0) / 35.0 * 0.12)


def _moneyball_alpha(moneyball_score: Optional[float]) -> float:
    """
    Probability adjustment from moneyball score (chain-relative rank).

    Moneyball ranks this contract vs all contracts in its chain.
    Score 50 = average; 80 = top quintile.

    Score  50 → 0.00
    Score  80 → +0.04
    Score 100 → +0.06  (hard cap)
    """
    if moneyball_score is None or float(moneyball_score) <= 50.0:
        return 0.0
    return min(0.06, (float(moneyball_score) - 50.0) / 50.0 * 0.06)


# ── Main Prediction Function ──────────────────────────────────────────────────
def predict_option_outcome(
    spot:             float,
    strike:           float,
    iv_pct:           float,        # e.g. 22.4  (percent, not decimal)
    dte_at_rec:       int,           # days to expiry at recommendation time
    option_type:      str,           # "CALL" or "PUT"
    signal_score:     float,
    option_score:     float         = 70.0,
    moneyball_score:  Optional[float] = None,
    final_score:      Optional[float] = None,
    current_premium:  Optional[float] = None,
    risk_free_rate:   float           = _DEFAULT_RISK_FREE_RATE,
) -> dict:
    """
    Predict win probability and target premium for an option recommendation.

    WIN is defined as EITHER:
      (a) Option expires ITM at expiry  →  ITM Win
      (b) Premium reaches target multiple before expiry  →  Premium Win

    Parameters
    ----------
    spot            : Current underlying price
    strike          : Option strike price
    iv_pct          : Implied volatility in percent (22.4 = 22.4%)
    dte_at_rec      : Days to expiry at time of recommendation
    option_type     : "CALL" or "PUT"
    signal_score    : Composite L1-L4 signal score (0-100)
    option_score    : Option quality score from analyzer (0-100)
    moneyball_score : Chain-relative rank score (0-100)
    final_score     : Blended score 60/40 current+moneyball (0-100)
    current_premium : Actual market premium (mid price)
    risk_free_rate  : Annual risk-free rate (decimal, default 5%)

    Returns
    -------
    dict with keys:
        p_itm_market       (float) : Black-Scholes N(d2) — market's estimate
        p_win_adjusted     (float) : Our adjusted win probability
        edge               (float) : p_win_adjusted - p_itm_market
        target_premium     (float) : Premium at signal-implied target spot
        current_premium    (float) : Entry premium
        premium_multiple   (float) : target_premium / current_premium
        ev_ratio           (float) : Expected value per dollar invested
        ev_rating          (str)   : 'STRONG' / 'GOOD' / 'NEUTRAL' / 'WEAK'
        breakeven_move_pct (float) : % move in underlying needed to break even
        target_spot        (float) : Signal-implied target spot price
        expected_move_pct  (float) : Expected % move from signal conviction
        confidence         (str)   : 'HIGH' / 'MEDIUM' / 'LOW'
        model_note         (str)   : Human-readable explanation
    """
    # ── Input sanitisation ────────────────────────────────────────────────────
    spot      = float(spot       or 0.0)
    strike    = float(strike     or 0.0)
    iv_pct    = float(iv_pct     or 20.0)
    dte       = max(1, int(dte_at_rec or 15))
    sig_score = float(signal_score or 55.0)
    mb_score  = float(moneyball_score) if moneyball_score is not None else 50.0
    fin_score = float(final_score or option_score)
    curr_prem = float(current_premium or 0.0)

    otype  = option_type.upper() if option_type else "CALL"
    r      = float(risk_free_rate)
    sigma  = iv_pct / 100.0
    # No sigma floor here — IV data quality is guaranteed upstream:
    # yf_options.get_option_chain_for_date() performs ATM IV substitution on any
    # contract whose IV is < 50% of the ATM reference (zero-bid / stale data fix).
    # If sigma arrives as < 0.01 it means the contract is genuinely untradeable;
    # let _bs_d1_d2 return None and propagate the degenerate result naturally.
    T      = dte / 365.0

    # ── Step 1: Market's P(ITM) via Black-Scholes N(d2) ──────────────────────
    p_market = _bs_p_itm(spot, strike, T, sigma, r, otype)

    # ── Step 2: Signal-adjusted P(WIN) ───────────────────────────────────────
    # WARNING: alpha values are UNCALIBRATED (empirical r=-0.006 vs 7d returns,
    # R²=0.00%, 0 settled options outcomes).  Shown as hypothesis only.
    sig_adj = _signal_alpha(sig_score)
    mb_adj  = _moneyball_alpha(mb_score)

    # Bullish-biased system: PUTs get half-weight.
    if otype == "PUT":
        sig_adj *= 0.5
        mb_adj  *= 0.5

    p_win = min(0.95, max(0.05, p_market + sig_adj + mb_adj))
    edge  = p_win - p_market

    # ── Step 3: Target Spot & Premium (scenario math, not a prediction) ───────
    expected_move = _signal_expected_move(sig_score)
    if otype == "CALL":
        target_spot = spot * (1.0 + expected_move)
    else:
        target_spot = spot * (1.0 - expected_move)

    # Target premium: evaluate at HALF the remaining DTE (conservative —
    # assumes the underlying reaches the target midway, not at expiry).
    T_half = max(1.0 / 365.0, T * 0.50)

    # P(underlying reaches target within half-DTE) — MUST use same time window
    # as the premium calculation above.  Using full-DTE vol overstates probability
    # by ~2-3x for weekly options (full-DTE σ > half-DTE σ by sqrt(2)).
    import math as _math
    daily_vol    = sigma / _math.sqrt(252.0)
    period_vol   = daily_vol * _math.sqrt(float(dte) / 2.0)  # half-DTE period
    target_sigma = (expected_move / period_vol) if period_vol > 0 else 0.0
    # P(underlying actually reaches target by half-DTE) — consistent with premium eval
    p_hit_target = max(0.0, 1.0 - _norm_cdf(target_sigma)) if target_sigma > 0 else 0.5

    if otype == "CALL":
        target_prem_bs = _bs_call_price(target_spot, strike, T_half, sigma, r)
    else:
        target_prem_bs = _bs_put_price(target_spot, strike, T_half, sigma, r)

    # ── Step 4: Market fair value & premium multiple ─────────────────────────
    # BS fair value at CURRENT spot — how much is this option worth vs entry?
    if otype == "CALL":
        bs_fair_value = _bs_call_price(spot, strike, T, sigma, r)
    else:
        bs_fair_value = _bs_put_price(spot, strike, T, sigma, r)

    prem_multiple = 0.0
    if curr_prem > 0.001:
        prem_multiple = target_prem_bs / curr_prem
    else:
        curr_prem = max(0.001, bs_fair_value)
        if curr_prem > 0.001:
            prem_multiple = target_prem_bs / curr_prem

    # Market EV: how much are we over/underpaying vs BS fair value
    market_ev      = bs_fair_value - curr_prem if curr_prem > 0.001 else 0.0
    market_ev_pct  = market_ev / curr_prem * 100.0 if curr_prem > 0.001 else 0.0

    # ── Step 5: CORRECTED Expected Value ─────────────────────────────────────
    # CORRECT formula: EV = P(WIN) * E[payoff|ITM] - (1-P(WIN)) * entry
    # E[payoff|ITM] = BS_fair_value / P_market  (conditional expected payoff)
    # This avoids the category error of mixing P(ITM) with target-touch payoff.
    if curr_prem > 0.001 and p_market > 0.01:
        cond_payoff = bs_fair_value / p_market          # E[payoff at expiry | ITM]
        ev          = p_win * cond_payoff - (1.0 - p_win) * curr_prem
        ev_ratio    = ev / curr_prem
    else:
        ev_ratio = 0.0

    # EV rating is ONLY meaningful when alpha is calibrated
    if not _ALPHA_CALIBRATED:
        ev_rating = "UNCAL"   # uncalibrated — do not rely on this
    elif ev_ratio >= 0.5:  ev_rating = "STRONG"
    elif ev_ratio >= 0.2:  ev_rating = "GOOD"
    elif ev_ratio >= 0.0:  ev_rating = "NEUTRAL"
    else:                   ev_rating = "WEAK"

    # ── Step 6: Breakeven move ───────────────────────────────────────────────
    if spot > 0 and curr_prem > 0:
        if otype == "CALL":
            be_spot = strike + curr_prem        # breakeven at expiry
        else:
            be_spot = strike - curr_prem
        breakeven_move_pct = abs(be_spot - spot) / spot * 100.0
    else:
        breakeven_move_pct = 0.0

    # ── Step 7: Confidence ────────────────────────────────────────────────────
    # Confidence = data completeness only; NOT signal conviction.
    # Cannot be HIGH until alpha is empirically calibrated.
    good_data = (spot > 0) and (sigma > 0.05) and (dte >= 3) and (curr_prem > 0)
    if not _ALPHA_CALIBRATED:
        confidence = "UNCALIBRATED"
    elif good_data and sig_score >= 70 and fin_score >= 70:
        confidence = "HIGH"
    elif good_data and sig_score >= 55:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # ── Model note ────────────────────────────────────────────────────────────
    adj_ppt = int(round((p_win - p_market) * 100))
    if not _ALPHA_CALIBRATED:
        model_note = (
            f"⚠️ UNCALIBRATED: signal r={_EMPIRICAL_SIGNAL_R:.3f} vs returns, "
            f"0 settled options outcomes. "
            f"Reliable: P(ITM)={p_market:.1%} (BS). "
            f"Hypothesis: signal adj={adj_ppt:+d}pp → P(WIN)={p_win:.1%}. "
            f"Scenario: +{expected_move:.1%} move ({target_sigma:.1f}σ, "
            f"actual prob≈{p_hit_target:.1%}) → target ${target_prem_bs:.2f} "
            f"({prem_multiple:.1f}×) at half-DTE."
        )
    else:
        model_note = (
            f"BS N(d2)={p_market:.1%} + Signal adj={adj_ppt:+d}pp "
            f"→ P(WIN)={p_win:.1%}. "
            f"Signal implies {expected_move:.1%} move → target {target_spot:.2f}. "
            f"Premium target {target_prem_bs:.2f} ({prem_multiple:.1f}×) at half-DTE."
        )

    return {
        "p_itm_market":       round(p_market,           3),
        "p_win_adjusted":     round(p_win,               3),   # UNCALIBRATED until flagged
        "edge":               round(edge,                3),   # UNCALIBRATED
        "p_hit_target":       round(p_hit_target,        3),   # P(underlying reaches target)
        "target_sigma":       round(target_sigma,        2),   # how many sigma is the target
        "target_premium":     round(target_prem_bs,      2),
        "current_premium":    round(curr_prem,           2),
        "bs_fair_value":      round(bs_fair_value,       2),   # BS value at current spot
        "market_ev":          round(market_ev,           2),   # BS_fair - entry
        "market_ev_pct":      round(market_ev_pct,       1),   # % of entry
        "premium_multiple":   round(prem_multiple,       2),
        "ev_ratio":           round(ev_ratio,            3),   # corrected EV formula
        "ev_rating":          ev_rating,                       # 'UNCAL' until calibrated
        "breakeven_move_pct": round(breakeven_move_pct,  1),
        "target_spot":        round(target_spot,         2),
        "expected_move_pct":  round(expected_move * 100, 1),
        "confidence":         confidence,                      # 'UNCALIBRATED' until flagged
        "alpha_calibrated":   _ALPHA_CALIBRATED,
        "empirical_r":        _EMPIRICAL_SIGNAL_R,
        "model_note":         model_note,
    }


# ── Calibration Utilities ─────────────────────────────────────────────────────

def calibrate_from_db(db_path: str) -> dict:
    """
    Compute fitted alpha values from settled options outcomes in alert_tracker.db.

    Reads outcomes written by outcome_tracker.backfill_outcomes().
    Returns a calibration dict; sets _ALPHA_CALIBRATED module-level flag
    to True when >= MIN_OUTCOMES settled records exist.

    Returns
    -------
    dict with keys:
      n_outcomes      : total settled records
      win_rate        : overall win rate
      by_bucket       : {score_bucket: {actual_wr, bs_baseline, alpha, n}}
      fitted_alphas   : {bucket: alpha}  — for use in _signal_alpha
      ready           : True if >= 200 outcomes
      message         : human-readable status
    """
    global _ALPHA_CALIBRATED
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("""
            SELECT signal_score, option_type, strike, spot_price,
                   iv_pct, dte_at_rec, premium, outcome
            FROM   options_recommendations
            WHERE  outcome IN ('ITM_WIN', 'OTM_LOSS', 'PREMIUM_WIN', 'PREMIUM_LOSS')
              AND  signal_score IS NOT NULL
        """).fetchall()
        conn.close()
    except Exception as ex:
        return {"error": str(ex), "ready": False, "n_outcomes": 0}

    if not rows:
        return {
            "ready": False, "n_outcomes": 0,
            "message": "No settled outcomes yet — run outcome_tracker.py first",
        }

    # ── Back-solve IV from entry premium for accurate BS baseline ─────────────
    def _bsolve(S, K, T, otype, prem):
        if not (S > 0 and K > 0 and T > 0 and prem > 0.001):
            return 0.25
        lo, hi = 0.01, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            d1  = (math.log(S / K) + (_DEFAULT_RISK_FREE_RATE + 0.5 * mid ** 2) * T) / (mid * T ** 0.5)
            d2  = d1 - mid * T ** 0.5
            bs  = max(0.0, S * _norm_cdf(d1) - K * math.exp(-_DEFAULT_RISK_FREE_RATE * T) * _norm_cdf(d2)) \
                  if otype == "CALL" \
                  else max(0.0, K * math.exp(-_DEFAULT_RISK_FREE_RATE * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1))
            if abs(bs - prem) < 1e-4:
                return mid
            if bs < prem:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    _WIN = {"ITM_WIN", "PREMIUM_WIN"}
    buckets: dict[int, list] = {}
    for score, otype, strike, spot, iv_raw, dte, prem, outcome in rows:
        b   = int(max(50, min(90, (int(float(score)) // 5) * 5)))
        win = outcome in _WIN
        T   = max(0.0, int(dte or 21)) / 365.0
        sv  = float(spot or 0)
        kv  = float(strike or 0)
        iv  = _bsolve(sv, kv, T, otype or "CALL", float(prem or 0))
        p_itm = _bs_p_itm(sv, kv, T, iv, _DEFAULT_RISK_FREE_RATE, otype or "CALL") if sv > 0 else 0.5
        buckets.setdefault(b, []).append((win, p_itm))

    by_bucket: dict   = {}
    fitted:    dict   = {}
    tw = tn = 0
    for b in sorted(buckets):
        data = buckets[b]
        if len(data) < 5:
            continue
        actual_wr   = sum(1 for w, _ in data if w) / len(data)
        bs_baseline = sum(p for _, p in data) / len(data)
        alpha       = actual_wr - bs_baseline
        by_bucket[b] = {
            "actual_wr":   round(actual_wr, 4),
            "bs_baseline": round(bs_baseline, 4),
            "alpha":       round(alpha, 4),
            "n":           len(data),
        }
        fitted[b] = alpha
        tw += sum(1 for w, _ in data if w)
        tn += len(data)

    n_total = sum(len(v) for v in buckets.values())
    ready   = n_total >= _MIN_OUTCOMES_TO_CALIBRATE

    if ready:
        _ALPHA_CALIBRATED = True   # unlock calibrated status

    return {
        "n_outcomes":    n_total,
        "win_rate":      round(tw / tn, 4) if tn > 0 else 0.0,
        "by_bucket":     by_bucket,
        "fitted_alphas": fitted,
        "ready":         ready,
        "message":       (f"✅ Calibrated on {n_total} outcomes (set _ALPHA_CALIBRATED=True)"
                          if ready
                          else f"⏳ {n_total} outcomes — need {_MIN_OUTCOMES_TO_CALIBRATE - n_total} more"),
    }


# ── Auto-calibrate on first import if enough settled outcomes exist ──────────
# Fires once per Python process when the module is first imported.
# Safe to call when the DB has zero settled records — calibrate_from_db()
# only sets _ALPHA_CALIBRATED=True when ≥200 resolved outcomes are present.
try:
    _db_default = Path(__file__).resolve().parent.parent / "alert_tracker.db"
    if not _ALPHA_CALIBRATED and _db_default.exists():
        calibrate_from_db(str(_db_default))
except Exception:
    pass
