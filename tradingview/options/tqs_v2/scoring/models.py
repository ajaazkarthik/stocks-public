"""
TQS v2 Scoring Models
======================
Immutable domain objects for scored and ranked contracts.

8-Dimension scoring weights (sums to 1.00):
  probability          15% — delta-based PoP, DTE zone, moneyness  (was 18%, Gap 4 2026-05-27)
  liquidity            15% — OI, bid-ask spread, volume             (was 18%, Gap 4 2026-05-27)
  risk_reward          16% — breakeven %, theo vs market
  win_rate             11% — PoP proxy + IV percentile + DTE factor
  iv_analysis          11% — IV level, bid/ask IV spread, term slope
  stock_trend          18% — SMA50/200 alignment, RSI, MACD histogram, DI polarity  (was 12%)
  volatility_conditions  6% — market-wide PCR + volatility-chart skew
  premium_efficiency    8% — time value %, spread efficiency, BE/1σ, |δ|×IV, leverage

Specialist tuning 2026-05-23:
  Added stock_trend at 12% to align scoring with independent specialist verdicts.
  Match rate on 8 CALL contracts: 7/8 = 87.5% (target ≥ 80%).

Gap fixes 2026-05-27:
  Gap 1: DI+/DI- polarity (plusDI>minusDI for CALLs, reversed for PUTs) — ±10/−20 adj.
  Gap 2: MACD histogram (macd − macd_signal) replaces raw macd>0 check.
  Gap 3: RSI CALL sweet spot tightened 45–70 → 55–70 (PineScript RSI≥60).
  Gap 4: stock_trend raised 12%→18%; probability+liquidity each reduced 18%→15%.
  Gap 5: Momentum chasing guard — −15 penalty when |change_pct| > 5%.
  Gap 6: score_detailed returns adx_bucket, macd_signal_used, di_polarity_used, momentum_guard_applied.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..options_data.models import RawContract


@dataclass(frozen=True)
class ScoreBreakdown:
    """Individual dimension scores contributing to the total score."""

    # ── Core dimensions (required positional args) ───────────────────────────
    liquidity: float        # 0-100
    iv_analysis: float      # 0-100
    probability: float      # 0-100
    risk_reward: float      # 0-100

    # ── Extended dimensions (default 0.0 for backward compatibility) ─────────
    win_rate: float = 0.0              # Expected win probability [NEW]
    volatility_conditions: float = 0.0 # PCR + skew alignment    [NEW]
    premium_efficiency: float = 0.0    # Cost-efficiency for buyers [NEW]

    # ── Liquidity sub-scores ─────────────────────────────────────────────────
    oi_score: float = 0.0
    spread_score: float = 0.0
    volume_score: float = 0.0

    # ── IV analysis sub-scores ───────────────────────────────────────────────
    iv_level_score: float = 0.0
    iv_spread_score: float = 0.0
    term_slope_score: float = 0.0

    # ── Probability sub-scores ───────────────────────────────────────────────
    delta_score: float = 0.0
    dte_score: float = 0.0
    moneyness_score: float = 0.0

    # ── Risk/reward sub-scores ───────────────────────────────────────────────
    breakeven_score: float = 0.0
    theo_score: float = 0.0

    # ── Win rate sub-scores ──────────────────────────────────────────────────
    pop_score: float = 0.0
    iv_pct_adj: float = 0.0

    # ── Volatility conditions sub-scores ─────────────────────────────────────
    pcr_score: float = 0.0
    skew_score: float = 0.0

    # ── Premium efficiency sub-scores ────────────────────────────────────────
    tv_score: float = 0.0
    spread_eff_score: float = 0.0
    be_score: float = 0.0
    be_1sigma_score: float = 0.0    # breakeven / (spot × iv × √(dte/365)) — how many σ away
    delta_iv_score: float = 0.0     # |δ| × IV — risk-exposure proxy per dollar paid
    leverage_score: float = 0.0     # spot / ask — leverage multiplier (higher = better for buyers)

    # ── Stock trend sub-scores ────────────────────────────────────────────────
    stock_trend: float = 0.0        # 8th dimension: SMA50+RSI+MACD alignment [NEW 2026-05-23]

    # ── Catalyst sub-score (only active when catalyst_mode == True) ──────────
    catalyst_score: float = 0.0     # catalyst signal aggregation (0-100)

    @property
    def total(self) -> float:
        """Weighted total across 8 dimensions (sums to 100%)."""
        # Gap 4: rebalanced weights (2026-05-27).
        # stock_trend raised 12%→18% (now incorporates DI polarity, MACD histogram, tighter RSI).
        # probability + liquidity each reduced 18%→15% to compensate (sum still = 1.00).
        # new sum: 0.15+0.15+0.16+0.11+0.11+0.18+0.06+0.08 = 1.00 ✓
        return (
            0.15 * self.probability
            + 0.15 * self.liquidity
            + 0.16 * self.risk_reward
            + 0.11 * self.win_rate
            + 0.11 * self.iv_analysis
            + 0.18 * self.stock_trend        # raised 12%→18%: DI polarity, MACD histogram, tighter RSI
            + 0.06 * self.volatility_conditions
            + 0.08 * self.premium_efficiency
        )

    @property
    def grade(self) -> str:
        """Letter grade based on total score."""
        t = self.total
        if t >= 85:
            return "A+"
        elif t >= 80:
            return "A"
        elif t >= 75:
            return "B+"
        elif t >= 70:
            return "B"
        elif t >= 65:
            return "C+"
        elif t >= 60:
            return "C"
        else:
            return "F"


@dataclass
class ScoredContract:
    """A RawContract after full scoring — contains original contract + scores."""

    contract: "RawContract"
    scores: ScoreBreakdown
    catalyst_total: float = 0.0    # weighted total using _WEIGHTS_CATALYST; set by engine in catalyst_mode

    @property
    def total_score(self) -> float:
        return self.scores.total

    @property
    def grade(self) -> str:
        return self.scores.grade

    @property
    def symbol(self) -> str:
        return self.contract.root

    @property
    def option_type(self) -> str:
        return self.contract.option_type
