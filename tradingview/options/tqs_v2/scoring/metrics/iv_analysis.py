"""
TQS v2 IV Analysis Scorer
==========================
Scores a contract on its implied volatility characteristics (0-100).

Formula:
  iv_pct = iv × 100  (scan2 returns decimal)
  
  iv_level_score  = max(0, min(100, 100 - max(0, iv_pct - 15) × 2))   monotonic decrease for buyers
  iv_spread_score = max(0, 100 - (ask_iv - bid_iv) × 100 × 4)   tight IV spread
  term_slope_score = normalized iv_term_slope enrichment

  iv_score = 0.40 × iv_level + 0.40 × iv_spread + 0.20 × term_slope
"""

from __future__ import annotations
from typing import Optional

from ...interfaces.contracts import IContractScorer
from ...options_data.models import RawContract


class IVScorer(IContractScorer):
    """Scores implied volatility level, IV spread, and term structure."""

    @property
    def name(self) -> str:
        return "iv_analysis"

    def score(self, contract: RawContract) -> float:
        """Return IV score 0-100. Never raises."""
        try:
            return self._compute(contract)
        except Exception:
            return 0.0

    def score_detailed(self, contract: RawContract) -> dict[str, float]:
        """Return {iv_level_score, iv_spread_score, term_slope_score, total}."""
        try:
            iv_l = self._iv_level_score(contract.iv)
            iv_sp = self._iv_spread_score(contract.bid_iv, contract.ask_iv)
            term = self._term_slope_score(
                getattr(contract, "iv_term_slope", 0.0)
            )
            total = 0.40 * iv_l + 0.40 * iv_sp + 0.20 * term
            return {
                "iv_level_score": round(iv_l, 2),
                "iv_spread_score": round(iv_sp, 2),
                "term_slope_score": round(term, 2),
                "total": round(total, 2),
            }
        except Exception:
            return {"iv_level_score": 0.0, "iv_spread_score": 0.0,
                    "term_slope_score": 0.0, "total": 0.0}

    def _compute(self, contract: RawContract) -> float:
        iv_l = self._iv_level_score(contract.iv)
        iv_sp = self._iv_spread_score(contract.bid_iv, contract.ask_iv)
        term = self._term_slope_score(
            getattr(contract, "iv_term_slope", 0.0)
        )
        return 0.40 * iv_l + 0.40 * iv_sp + 0.20 * term

    @staticmethod
    def _iv_level_score(iv: float) -> float:
        """
        IV level score: monotonically decreasing for option BUYERS.
        Lower IV = cheaper premium = better entry. IV ≤15% scores 100;
        IV=40% scores 50; IV ≥65% scores 0.
        Fix 2026-05-27 (C-1): Prior tent at IV=40% penalised low IV identically
        to high IV — wrong for buyers who benefit from cheap premiums.
        """
        iv_pct = iv * 100.0   # scan2 returns decimal fraction
        return max(0.0, min(100.0, 100.0 - max(0.0, iv_pct - 15.0) * 2.0))

    @staticmethod
    def _iv_spread_score(bid_iv: float, ask_iv: float) -> float:
        """
        Tighter bid/ask IV spread = more liquid options market.
        Score = 100 - (ask_iv - bid_iv) × 100 × 4
        Example: 5% IV spread → score = 100 - 5×4 = 80
        """
        if bid_iv <= 0 and ask_iv <= 0:
            # When market closed, bid_iv/ask_iv may both be 0
            # Return true neutral (50.0), not above-neutral bonus.
            # Fix 2026-05-27 (M-4): Was 60.0 giving a +10pt bonus for after-hours scans.
            return 50.0
        spread_iv = (ask_iv - bid_iv) * 100.0   # convert to percentage points
        return max(0.0, 100.0 - spread_iv * 4.0)

    @staticmethod
    def _term_slope_score(iv_term_slope: float) -> float:
        """
        Contango is normal (positive slope) → higher score.
        Backwardation (negative slope) reduces score slightly.
        
        Slope is in decimal fraction (1m - 1w).
        We cap at ±0.05 (5% absolute change).
        """
        if iv_term_slope == 0.0:
            return 60.0  # neutral when no term structure data
        # Normalize: +0.05 (strong contango) = 100, -0.05 (backwardation) = 20
        normalized = max(-0.05, min(0.05, iv_term_slope))
        # Map [-0.05, 0.05] → [20, 100]
        return 20.0 + (normalized + 0.05) / 0.10 * 80.0
