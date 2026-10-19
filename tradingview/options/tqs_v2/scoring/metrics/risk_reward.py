"""
TQS v2 Risk/Reward Scorer
==========================
Scores a contract's risk/reward profile (0-100).

Formula:
  breakeven_dist_pct = |breakeven - spot| / spot × 100
  rr_breakeven_score = max(0, 100 - breakeven_dist_pct × 4)

  rv_proxy   = (stock_atr / spot_price) × √252     (annualised ATR-based realized vol)
  iv_rv_ratio = iv / rv_proxy
  iv_rv_score:
    ratio ≤ 0.75              → 85-100  (IV well below RV = cheap options for buyers)
    ratio 0.75..1.0           → 55-85   (IV near RV = fair pricing)
    ratio 1.0..1.5            → 0-55    (IV above RV = expensive premiums)
    ratio > 1.5               → 0       (IV far above RV = very expensive)
    fallback (no ATR/spot)    → 50.0

  risk_reward_score = 0.45 × rr_breakeven_score + 0.55 × iv_rv_score
  (theo_score key retained for DB/report compatibility; maps to iv_rv_score)

Rationale for replacing theo_price/ask ratio (2026-05-25):
  theo_price is computed by TV scan2 using Black-Scholes with IV as input.
  Therefore theo_price/ask ≈ f(IV)/ask — scoring based on an IV-derived value
  to evaluate IV is circular. IV/RV ratio is a direct measure of premium richness
  from realized vs implied perspective, with no circularity.
"""

from __future__ import annotations

import math

from ...interfaces.contracts import IContractScorer
from ...options_data.models import RawContract


class RiskRewardScorer(IContractScorer):
    """Scores risk/reward profile of an option contract."""

    @property
    def name(self) -> str:
        return "risk_reward"

    def score(self, contract: RawContract) -> float:
        """Return risk/reward score 0-100. Never raises."""
        try:
            return self._compute(contract)
        except Exception:
            return 0.0

    def score_detailed(self, contract: RawContract) -> dict[str, float]:
        try:
            be_s = self._breakeven_score(contract)
            iv_rv_s = self._iv_rv_ratio_score(contract)
            total = 0.45 * be_s + 0.55 * iv_rv_s
            return {
                "breakeven_score": round(be_s, 2),
                "theo_score": round(iv_rv_s, 2),   # key kept for DB/report compat
                "total": round(total, 2),
            }
        except Exception:
            return {"breakeven_score": 0.0, "theo_score": 0.0, "total": 0.0}

    def _compute(self, contract: RawContract) -> float:
        be_s = self._breakeven_score(contract)
        iv_rv_s = self._iv_rv_ratio_score(contract)
        return 0.45 * be_s + 0.55 * iv_rv_s

    @staticmethod
    def _breakeven_score(contract: RawContract) -> float:
        """
        Lower breakeven distance = easier to reach profitability.
        breakeven_dist_pct = |breakeven - spot| / spot × 100

        0% distance = 100 score; 25%+ distance = 0 score.
        """
        spot = float(getattr(contract, "spot_price", 0.0) or 0.0)
        if spot <= 0:
            return 50.0  # neutral if no spot price
        breakeven = float(getattr(contract, "breakeven", 0.0) or 0.0)
        dist = abs(breakeven - spot)
        dist_pct = dist / spot * 100.0
        return max(0.0, 100.0 - dist_pct * 4.0)

    @staticmethod
    def _iv_rv_ratio_score(contract: RawContract) -> float:
        """
        IV / Realized-Volatility ratio score for option buyers.

        RV proxy = (ATR / spot) × √252  — annualised ATR-based realized volatility.
        Using stock_atr (Average True Range in price units) normalised by spot price
        converts to a percentage move scale comparable to decimal IV.

        Scoring:
          ratio ≤ 0.75  → 85-100   options very cheap vs recent realized vol
          ratio 0.75-1.0 → 55-85   fair pricing zone
          ratio 1.0-1.5  → 0-55    options getting expensive
          ratio > 1.5    → 0       very expensive, avoid buying
          fallback       → 50.0    neutral when ATR or spot unavailable
        """
        iv = float(getattr(contract, "iv", 0.0) or 0.0)
        atr = float(getattr(contract, "stock_atr", 0.0) or 0.0)
        spot = float(getattr(contract, "spot_price", 0.0) or 0.0)

        if iv <= 0 or atr <= 0 or spot <= 0:
            return 50.0

        rv_proxy = (atr / spot) * math.sqrt(252.0)
        if rv_proxy <= 0:
            return 50.0

        ratio = iv / rv_proxy

        if ratio <= 0.75:
            # Very cheap: 85 + bonus up to 100 as ratio → 0
            return min(100.0, 85.0 + (0.75 - ratio) / 0.75 * 15.0)
        elif ratio <= 1.0:
            # Fair zone: linearly 85 → 55
            return 85.0 - (ratio - 0.75) / 0.25 * 30.0
        elif ratio <= 1.5:
            # Getting expensive: 55 → 0
            return max(0.0, 55.0 - (ratio - 1.0) / 0.5 * 55.0)
        else:
            # Very expensive: clamp to 0
            return 0.0
