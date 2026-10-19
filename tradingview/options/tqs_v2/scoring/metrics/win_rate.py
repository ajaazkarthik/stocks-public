"""
Expected Win Rate Scorer
========================
Estimates probability of profit at expiry using N(d2) with breakeven-adjusted
effective strike as the true PoP proxy, adjusted for IV rank context and DTE efficiency zone.

Formula:
  premium         = ask  (true cost to buyer; Fix 2026-05-27 C-5: was mid)
  effective_K     = strike + premium  (CALL)  /  max(0.01, strike - premium)  (PUT)
  d2              = [ln(spot / effective_K) + (−0.5·IV²)·T] / (IV·√T)
  pop_score       = N(d2) × 100  (buyer profits only when stock clears breakeven)

  iv_rank_proxy   = clamp(100 - iv_contango_score, 0, 100)
  iv_pct_adj      = (1 − iv_rank_proxy/100) × 10  (penalty for HIGH IV = expensive premiums)
  dte_factor      = 1.0 for 30–60 DTE, 0.9 for 21–29 or 61–90, 0.7 otherwise
  win_rate        = clamp(pop_score × dte_factor + iv_pct_adj, 0, 100)

Falls back to |delta|×100 when spot/IV/mid are unavailable (graceful degradation).

Sub-scores returned:
  pop_score, iv_pct_adj, iv_rank_proxy, dte_factor → all for observability
"""

from __future__ import annotations

import math

try:
    from scipy.stats import norm as _scipy_norm  # type: ignore
    _SCIPY_OK = True
except ImportError:  # pragma: no cover
    _SCIPY_OK = False

from ...interfaces.contracts import IContractScorer
from ...options_data.models import RawContract


class WinRateScorer(IContractScorer):
    """
    Expected win rate scorer using N(d2) with breakeven-adjusted effective strike.

    Input fields consumed from RawContract (duck-typed):
      delta              – fallback PoP proxy when IV/spot data unavailable
      dte                – days to expiry
      spot_price         – current underlying price
      strike             – option strike price
      bid, ask           – option bid/ask for mid-price calculation
      iv                 – implied volatility (decimal, e.g. 0.35 = 35%)
      option_type        – "call" or "put"
      iv_contango_score  – IV term structure contango score 0..100 (set by _apply_iv_enrichment;
                           high contango = calm market = low IV rank; defaults to 50)
    """

    @property
    def name(self) -> str:
        return "win_rate"

    def score(self, contract: RawContract) -> float:
        return self.score_detailed(contract)["total"]

    def score_detailed(self, contract: RawContract) -> dict:
        delta = abs(getattr(contract, "delta", 0.0) or 0.0)
        dte = max(1, int(getattr(contract, "dte", 45) or 45))

        # IV rank proxy — prefer real 52-week percentile when available (C-3 fix 2026-05-27).
        # Falls back to contango proxy when real data unavailable.
        _real_iv_pctile = getattr(contract, "iv_percentile_real", None)
        if _real_iv_pctile is not None:
            iv_percentile = max(0.0, min(100.0, float(_real_iv_pctile)))
        else:
            # Fallback: contango proxy (set by _apply_iv_enrichment).
            # High contango (79) → calm market → low IV rank (21).
            # Backwardation (10) → stressed market → high IV rank (90).
            iv_contango = float(getattr(contract, "iv_contango_score", 50.0) or 50.0)
            iv_percentile = max(0.0, min(100.0, 100.0 - iv_contango))

        # ── PoP via N(d2) with breakeven-adjusted effective strike ──────────
        # Delta ≈ P(spot > strike) but buyer profits only when spot > strike + mid.
        # Using effective_strike = strike + mid adjusts for the premium paid.
        spot = float(getattr(contract, "spot_price", 0.0) or 0.0)
        strike = float(getattr(contract, "strike", 0.0) or 0.0)
        bid = float(getattr(contract, "bid", 0.0) or 0.0)
        ask = float(getattr(contract, "ask", 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
        iv = float(getattr(contract, "iv", 0.0) or 0.0)
        option_type = (getattr(contract, "option_type", "call") or "call").lower()

        T = dte / 365.0
        pop_score = _compute_pop_n_d2(spot, strike, ask, iv, T, delta, option_type)

        # ── IV rank penalty for buyers ────────────────────────────────────
        # HIGH IV rank = expensive premiums = buyers overpay → penalise.
        # LOW IV rank = cheap premiums = good entry for buyers → reward.
        # Range: 0.0 (iv_rank=100, max penalty) to +10.0 (iv_rank=0, max bonus).
        iv_pct_adj = ((100.0 - iv_percentile) / 100.0) * 10.0

        # DTE efficiency zone factor
        if 30 <= dte <= 60:
            dte_factor = 1.0    # sweet spot
        elif 21 <= dte < 30 or 60 < dte <= 90:
            dte_factor = 0.9    # acceptable
        else:
            dte_factor = 0.7    # suboptimal (too short or too long)

        win_rate = min(100.0, max(0.0, pop_score * dte_factor + iv_pct_adj))

        return {
            "total": win_rate,
            "pop_score": pop_score,
            "iv_pct_adj": iv_pct_adj,
            "iv_rank_proxy": round(iv_percentile, 1),
            "dte_factor": dte_factor,
        }


# ── Module-level helpers (testable without class instantiation) ──────────────

def _compute_pop_n_d2(
    spot: float,
    strike: float,
    premium: float,   # was 'mid'; Fix 2026-05-27 (C-5): callers now pass ask (true cost)
    iv: float,
    T: float,
    delta_fallback: float,
    option_type: str,
) -> float:
    """
    Compute P(profit at expiry) using N(d2) with breakeven-adjusted effective strike.
    Falls back to |delta|×100 when inputs are invalid or incomplete.
    """
    fallback = min(100.0, delta_fallback * 100.0)
    if not (spot > 0 and strike > 0 and iv > 0 and T > 0 and premium > 0):
        return fallback

    if option_type == "call":
        effective_strike = strike + premium          # must clear breakeven to profit
    else:
        effective_strike = max(0.01, strike - premium)  # must drop below breakeven

    if effective_strike <= 0:
        return fallback

    try:
        d2 = (math.log(spot / effective_strike) + (-0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
        if _SCIPY_OK:
            from scipy.stats import norm  # already imported at top; re-alias for clarity
            cdf = _scipy_norm.cdf
        else:
            cdf = _norm_cdf_approx  # type: ignore[assignment]

        if option_type == "call":
            pop = cdf(d2) * 100.0
        else:
            pop = cdf(-d2) * 100.0
        return float(min(100.0, max(0.0, pop)))
    except (ValueError, ZeroDivisionError, OverflowError):
        return fallback


def _norm_cdf_approx(x: float) -> float:
    """Normal CDF approximation without scipy (Abramowitz & Stegun 26.2.17)."""
    sign = 1.0 if x >= 0 else -1.0
    xa = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * xa)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    return 0.5 * (1.0 + sign * (1.0 - poly * math.exp(-xa * xa / 2.0)))
