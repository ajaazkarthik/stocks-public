"""
TQS v2 Scoring Engine
=====================
Orchestrates all 8 scorers and produces ScoredContract objects.

Scoring weights (sum to 100%) — updated 2026-05-27 (Gap 4 rebalance):
  stock_trend          18%   — SMA50/RSI/MACD alignment with option direction
  risk_reward          16%   — breakeven %, theo vs market
  probability          15%   — delta-based PoP, DTE zone, moneyness
  liquidity            15%   — OI, bid-ask spread, volume
  win_rate             11%   — PoP proxy + IV percentile + DTE factor
  iv_analysis          11%   — IV level, bid/ask IV spread, term slope
  premium_efficiency    8%   — time value %, leverage ratio (OTM), be_days
  volatility_conditions  6%  — voi-lens PCR + volatility-chart skew

Minimum score for report inclusion: config.min_score_for_report (default 65.0)
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from ..config import TQSv2Config
from ..options_data.models import RawContract
from .metrics.liquidity import LiquidityScorer
from .metrics.iv_analysis import IVScorer
from .metrics.probability import ProbabilityScorer
from .metrics.risk_reward import RiskRewardScorer
from .metrics.win_rate import WinRateScorer
from .metrics.volatility_conditions import VolatilityConditionsScorer
from .metrics.premium_efficiency import PremiumEfficiencyScorer
from .metrics.stock_trend import StockTrendScorer
from .models import ScoreBreakdown, ScoredContract

_log = logging.getLogger("tqs_v2.scoring.engine")

# Catalyst scoring weights (sum to 1.0)
# win_rate and volatility_conditions removed; replaced by catalyst dimension (17%)
_WEIGHTS_CATALYST = {
    "probability":        0.18,
    "liquidity":          0.13,
    "risk_reward":        0.14,
    "iv_analysis":        0.15,
    "stock_trend":        0.15,
    "premium_efficiency": 0.08,
    "catalyst":           0.17,
}  # sum = 1.00


class ScoringEngine:
    """Scores all contracts using 8 weighted dimensions."""

    def __init__(self) -> None:
        self._liquidity = LiquidityScorer()
        self._iv = IVScorer()
        self._probability = ProbabilityScorer()
        self._risk_reward = RiskRewardScorer()
        self._win_rate = WinRateScorer()
        self._vol_conditions = VolatilityConditionsScorer()
        self._premium_eff = PremiumEfficiencyScorer()
        self._stock_trend = StockTrendScorer()

    def score_all(
        self,
        contracts: list[RawContract],
        config: TQSv2Config,
    ) -> list[ScoredContract]:
        """
        Score all contracts. Returns only contracts scoring >= min_score_for_report.
        Never raises — failures are logged and the contract is skipped.
        """
        # ── VIX Regime Awareness (C-6 fix 2026-05-27, wired C-1 fix) ──────
        # Fetch VIX BEFORE the scoring loop so the multiplier can adjust the
        # threshold.  Previously this ran AFTER the loop — the multiplier was
        # computed but never applied (C-1 audit finding 2026-05-27).
        from .metrics.vix_regime import fetch_vix_level, get_vix_multiplier, vix_regime_score

        vix_level = fetch_vix_level()
        vix_mult, vix_label = get_vix_multiplier(vix_level)
        vix_sc = vix_regime_score(vix_level)
        effective_threshold = config.min_score_for_report * vix_mult
        _log.info(
            "VIX regime: level=%.2f, regime=%s, mult=%.2f, threshold=%.1f→%.1f",
            vix_level or 0.0, vix_label, vix_mult,
            config.min_score_for_report, effective_threshold,
        )

        scored: list[ScoredContract] = []
        failed = 0
        pop_rejected = 0

        for c in contracts:
            try:
                s = self._score_one(c, config=config)

                # ── Catalyst mode: compute catalyst_total and attach ─────────
                if config.catalyst_mode:
                    from .metrics.catalyst_metric import compute_catalyst_score
                    cat_s = compute_catalyst_score(c, config)
                    cat_total = (
                        s.scores.probability        * _WEIGHTS_CATALYST["probability"]
                        + s.scores.liquidity        * _WEIGHTS_CATALYST["liquidity"]
                        + s.scores.risk_reward      * _WEIGHTS_CATALYST["risk_reward"]
                        + s.scores.iv_analysis      * _WEIGHTS_CATALYST["iv_analysis"]
                        + s.scores.stock_trend      * _WEIGHTS_CATALYST["stock_trend"]
                        + s.scores.premium_efficiency * _WEIGHTS_CATALYST["premium_efficiency"]
                        + cat_s                     * _WEIGHTS_CATALYST["catalyst"]
                    )
                    s.catalyst_total = cat_total
                    # Update ScoreBreakdown with catalyst_score via replacement
                    object.__setattr__(s.scores, "catalyst_score", cat_s)

                # ── Earnings IV-crush gate ────────────────────────────────────
                # FIX 2026-05-27 (C-4): Moved to EarningsProximityFilter in the
                # filter pipeline so contracts are rejected BEFORE expensive
                # enrichment/scoring stages. The filter also adds a 14-day IV
                # inflation buffer (reject if 0 <= earnings_dte < dte + 14).
                # See: filtering/contract_filter.py::EarningsProximityFilter

                # Post-scoring pop_score gate (specialist recommendation 2026-05-23)
                if config.pop_score_min > 0 and s.scores.pop_score < config.pop_score_min:
                    pop_rejected += 1
                    _log.debug(
                        "pop_score gate rejected %s: pop_score=%.1f < min=%.1f",
                        getattr(c, "opra_code", "?"),
                        s.scores.pop_score,
                        config.pop_score_min,
                    )
                    continue
                if s.total_score >= effective_threshold:
                    scored.append(s)
                    continue
                # In catalyst_mode, also accept on catalyst_total threshold
                if config.catalyst_mode:
                    actual_score = getattr(s, "catalyst_total", s.total_score)
                    if actual_score >= effective_threshold:
                        scored.append(s)
                        continue
            except Exception as exc:
                _log.debug("Scoring failed for %s: %s", getattr(c, "opra_code", "?"), exc)
                failed += 1

        _log.info(
            "Scored %d/%d contracts (threshold %.1f [VIX-adjusted from %.1f]); %d pop_rejected; %d failed",
            len(scored),
            len(contracts),
            effective_threshold,
            config.min_score_for_report,
            pop_rejected,
            failed,
        )

        # Attach VIX metadata to each scored contract for reporting
        for sc in scored:
            sc.vix_level = vix_level
            sc.vix_regime = vix_label
            sc.vix_multiplier = vix_mult
            sc.vix_score = vix_sc

        return scored

    def score_one(self, contract: RawContract) -> Optional[ScoredContract]:
        """Score a single contract. Returns None on failure."""
        try:
            return self._score_one(contract, config=None)
        except Exception as exc:
            _log.debug("score_one failed for %s: %s",
                       getattr(contract, "opra_code", "?"), exc)
            return None

    def _score_one(self, contract: RawContract, config=None) -> ScoredContract:
        liq_detail = self._liquidity.score_detailed(contract)
        iv_detail = self._iv.score_detailed(contract)
        prob_detail = self._probability.score_detailed(contract)
        rr_detail = self._risk_reward.score_detailed(contract)
        wr_detail = self._win_rate.score_detailed(contract)
        vc_detail = self._vol_conditions.score_detailed(contract)
        pe_detail = self._premium_eff.score_detailed(contract)
        st_detail = self._stock_trend.score_detailed(contract)

        breakdown = ScoreBreakdown(
            # ── Primary dimensions ──────────────────────────────────────────
            liquidity=liq_detail["total"],
            iv_analysis=iv_detail["total"],
            probability=prob_detail["total"],
            risk_reward=rr_detail["total"],
            # ── Extended dimensions ─────────────────────────────────────────
            win_rate=wr_detail["total"],
            volatility_conditions=vc_detail["total"],
            premium_efficiency=pe_detail["total"],            stock_trend=st_detail["total"],            # ── Liquidity sub-scores ────────────────────────────────────────
            oi_score=liq_detail.get("oi_score", 0.0),
            spread_score=liq_detail.get("spread_score", 0.0),
            volume_score=liq_detail.get("volume_score", 0.0),
            # ── IV sub-scores ───────────────────────────────────────────────
            iv_level_score=iv_detail.get("iv_level_score", 0.0),
            iv_spread_score=iv_detail.get("iv_spread_score", 0.0),
            term_slope_score=iv_detail.get("term_slope_score", 0.0),
            # ── Probability sub-scores ──────────────────────────────────────
            delta_score=prob_detail.get("delta_score", 0.0),
            dte_score=prob_detail.get("dte_score", 0.0),
            moneyness_score=prob_detail.get("moneyness_score", 0.0),
            # ── Risk/reward sub-scores ──────────────────────────────────────
            breakeven_score=rr_detail.get("breakeven_score", 0.0),
            theo_score=rr_detail.get("theo_score", 0.0),
            # ── Win rate sub-scores ─────────────────────────────────────────
            pop_score=wr_detail.get("pop_score", 0.0),
            iv_pct_adj=wr_detail.get("iv_pct_adj", 0.0),
            # ── Volatility conditions sub-scores ────────────────────────────
            pcr_score=vc_detail.get("pcr_score", 0.0),
            skew_score=vc_detail.get("skew_score", 0.0),
            # ── Premium efficiency sub-scores ───────────────────────────────
            tv_score=pe_detail.get("tv_score", 0.0),
            spread_eff_score=pe_detail.get("spread_eff_score", 0.0),
            be_score=pe_detail.get("be_score", 0.0),            be_1sigma_score=pe_detail.get("be_1sigma_score", 0.0),
            delta_iv_score=pe_detail.get("delta_iv_score", 0.0),
            leverage_score=pe_detail.get("leverage_score", 0.0),        )

        return ScoredContract(contract=contract, scores=breakdown)

