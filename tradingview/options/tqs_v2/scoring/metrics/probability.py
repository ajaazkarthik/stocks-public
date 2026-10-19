"""
TQS v2 Probability Scorer
==========================
Scores the probability of an option expiring profitably (0-100).

Uses delta, DTE, and moneyness as inputs.

Formula:
  delta_score:
    For CALL: sweet zone delta 0.25-0.55 → score=100
    For PUT:  sweet zone delta -0.40 to -0.25 (abs) → score=100
    Linear falloff outside sweet zone.
  
  dte_score   = max(0, min(100, 100 - |dte - 45| × 1.5))  peak at 45 DTE
  
  moneyness_score = max(0, 100 - |moneyness_pct - 5| × 8)
    For CALL: moneyness_pct = (spot - strike)/strike × 100
              5% OTM = moneyness_pct = -5 → we want small negative
    Adjust: For CALL, target = -5; For PUT, target = +5

  probability_score = 0.50 × delta + 0.30 × dte + 0.20 × moneyness
"""

from __future__ import annotations
from ...interfaces.contracts import IContractScorer
from ...options_data.models import RawContract


class ProbabilityScorer(IContractScorer):
    """Scores probability of profitable expiry."""

    @property
    def name(self) -> str:
        return "probability"

    def score(self, contract: RawContract) -> float:
        """Return probability score 0-100. Never raises."""
        try:
            return self._compute(contract)
        except Exception:
            return 0.0

    def score_detailed(self, contract: RawContract) -> dict[str, float]:
        try:
            d_s = self._delta_score(contract.delta, contract.option_type)
            dte_s = self._dte_score(contract.dte)
            m_s = self._moneyness_score(contract.moneyness_pct, contract.option_type)
            total = 0.50 * d_s + 0.30 * dte_s + 0.20 * m_s
            return {
                "delta_score": round(d_s, 2),
                "dte_score": round(dte_s, 2),
                "moneyness_score": round(m_s, 2),
                "total": round(total, 2),
            }
        except Exception:
            return {"delta_score": 0.0, "dte_score": 0.0,
                    "moneyness_score": 0.0, "total": 0.0}

    def _compute(self, contract: RawContract) -> float:
        d_s = self._delta_score(contract.delta, contract.option_type)
        dte_s = self._dte_score(contract.dte)
        m_s = self._moneyness_score(contract.moneyness_pct, contract.option_type)
        return 0.50 * d_s + 0.30 * dte_s + 0.20 * m_s

    @staticmethod
    def _delta_score(delta: float, option_type: str) -> float:
        """
        Sweet zone for directional options: 0.25–0.55 absolute delta.
        Score = 100 in sweet zone, linear falloff outside.

        Fix 2026-05-26: Expanded upper bound from 0.40 → 0.55 to include ATM options.
        Prior upper bound penalised ATM options (delta≈0.50) despite them often having the
        best breakeven distance (e.g., CSCO ATM breakeven=3.73% vs OTM=7.76%). The expert
        confirmed ATM with strong momentum is frequently the best risk-reward choice.
        The sweet zone now represents the full useful directional range:
          0.25 = meaningful PoP floor (delta≈25 → ~30% PoP at expiry)
          0.55 = light ITM, full directional exposure with limited theta drag
        """
        d = abs(delta)
        # Perfect zone [0.25, 0.55] — includes both slightly-OTM and ATM options
        if 0.25 <= d <= 0.55:
            return 100.0
        elif d < 0.25:
            # Below 0.25: too far OTM, falling toward 0
            return max(0.0, d / 0.25 * 100.0)
        else:
            # Above 0.55: deeper ITM, falling toward 0 at delta=0.80
            return max(0.0, (1.0 - (d - 0.55) / 0.25) * 100.0)

    @staticmethod
    def _dte_score(dte: int) -> float:
        """Peak at 45 DTE — enough time premium, not too much decay."""
        return max(0.0, min(100.0, 100.0 - abs(dte - 45) * 1.5))

    @staticmethod
    def _moneyness_score(moneyness_pct: float, option_type: str) -> float:
        """
        moneyness_pct = (spot - strike) / strike × 100
        
        For CALL: positive moneyness = ITM. We want slightly OTM (~-5%).
        For PUT:  negative moneyness = ITM. We want slightly OTM (~+5%).
        
        Target: -5% for calls (slightly OTM), +5% for puts (slightly OTM).
        """
        if option_type == "call":
            target = -5.0   # slightly OTM call (spot slightly below strike)
        else:
            target = +7.0   # more OTM for puts → better premium efficiency

        # Coefficient 4.0 (was 8.0): more gradual penalty for off-target moneyness.
        # Prevents over-penalizing contracts that are 1-3% away from the target.
        return max(0.0, 100.0 - abs(moneyness_pct - target) * 4.0)
