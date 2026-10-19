"""
Volatility Conditions Scorer
=============================
Uses voi-lens PCR data and volatility-chart skew to score the macro
volatility environment for a contract.

PCR signal (from voi-lens):
  Low PCR (<0.7) = more calls traded = bullish → good for CALL buyers
  High PCR (>1.2) = more puts traded = bearish → good for PUT buyers

Skew signal (from volatility-chart):
  Positive skew (25D put IV > 25D call IV) = bearish tilt → PUT favorable
  Negative skew (25D call IV > 25D put IV) = bullish tilt → CALL favorable

Formula:
  vol_score = 0.6 × pcr_score + 0.4 × skew_score

Input contract attributes (enriched by orchestrator Stage 4b):
  voi_pcr     – put/call volume ratio (float | None); sourced from voi-lens API
  iv_skew_25d – 25D put IV minus 25D call IV (float | None); from volatility-chart
"""

from __future__ import annotations

from ...interfaces.contracts import IContractScorer
from ...options_data.models import RawContract


class VolatilityConditionsScorer(IContractScorer):
    """Scores volatility environment using voi-lens PCR + volatility-chart skew."""

    @property
    def name(self) -> str:
        return "volatility_conditions"

    def score(self, contract: RawContract) -> float:
        return self.score_detailed(contract)["total"]

    def score_detailed(self, contract: RawContract) -> dict:
        option_type = getattr(contract, "option_type", "call").lower()
        pcr = getattr(contract, "voi_pcr", None)        # enriched by Stage 4b
        skew = getattr(contract, "iv_skew_25d", None)   # enriched by Stage 4b

        pcr_score = self._pcr_score(pcr, option_type)
        skew_score = self._skew_score(skew, option_type)

        total = 0.6 * pcr_score + 0.4 * skew_score

        return {
            "total": round(total, 2),
            "pcr_score": pcr_score,
            "skew_score": skew_score,
            "pcr": pcr,
            "skew_25d": skew,
        }

    @staticmethod
    def _pcr_score(pcr: float | None, option_type: str) -> float:
        """Map PCR to 0-100 score.  Lower PCR → better for calls; higher → better for puts."""
        if pcr is None:
            return 50.0

        if option_type == "call":
            if pcr < 0.5:
                return 90.0
            elif pcr < 0.7:
                return 75.0
            elif pcr < 1.0:
                return 55.0
            elif pcr < 1.3:
                return 35.0
            else:
                return 15.0
        else:  # put
            if pcr > 1.5:
                return 90.0
            elif pcr > 1.2:
                return 75.0
            elif pcr > 0.9:
                return 55.0
            elif pcr > 0.7:
                return 35.0
            else:
                return 15.0

    @staticmethod
    def _skew_score(skew: float | None, option_type: str) -> float:
        """Map 25D put-call skew to 0-100 score.  Positive skew = put skew = bearish."""
        if skew is None:
            return 50.0

        if option_type == "call":
            # Negative skew (calls bid up) = bullish env = good for calls
            if skew < -0.03:
                return 85.0
            elif skew < 0.0:
                return 65.0
            elif skew < 0.03:
                return 45.0
            else:
                return 25.0
        else:  # put
            if skew > 0.05:
                return 85.0
            elif skew > 0.02:
                return 70.0
            elif skew > 0.0:
                return 55.0
            else:
                return 30.0
