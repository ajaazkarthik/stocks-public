"""
Tests for Phase 1 TQS v2 scoring fixes (2026-05-27).
Verifies C-1, C-2, C-5, M-1, M-4 fixes.
"""
import pytest
import math


class TestFix01_IVLevelScore:
    """C-1: IV level scorer should be monotonically decreasing for buyers."""

    def test_low_iv_scores_highest(self):
        """IV <= 15% should score 100."""
        from tradingview.options.tqs_v2.scoring.metrics.iv_analysis import IVScorer
        assert IVScorer._iv_level_score(0.10) == 100.0
        assert IVScorer._iv_level_score(0.15) == 100.0

    def test_mid_iv_scores_moderate(self):
        """IV = 40% should score ~50."""
        from tradingview.options.tqs_v2.scoring.metrics.iv_analysis import IVScorer
        score = IVScorer._iv_level_score(0.40)
        assert 45.0 <= score <= 55.0

    def test_high_iv_scores_zero(self):
        """IV >= 65% should score 0."""
        from tradingview.options.tqs_v2.scoring.metrics.iv_analysis import IVScorer
        assert IVScorer._iv_level_score(0.65) == 0.0
        assert IVScorer._iv_level_score(0.80) == 0.0

    def test_monotonically_decreasing(self):
        """Score should decrease (or stay flat) as IV increases."""
        from tradingview.options.tqs_v2.scoring.metrics.iv_analysis import IVScorer
        scores = [IVScorer._iv_level_score(iv / 100.0) for iv in range(10, 80, 5)]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], (
                f"Not monotonic at IV={10 + i*5}%: {scores[i]} < {scores[i+1]}"
            )

    def test_low_iv_better_than_high_iv(self):
        """IV=20% should score higher than IV=60% (key buyer behavior)."""
        from tradingview.options.tqs_v2.scoring.metrics.iv_analysis import IVScorer
        assert IVScorer._iv_level_score(0.20) > IVScorer._iv_level_score(0.60)


class TestFix02_PremiumCap:
    """C-2: Premium cap should be $8.00 to include ATM options."""

    def test_default_premium_cap(self):
        from tradingview.options.tqs_v2.config import TQSv2Config
        cfg = TQSv2Config()
        assert cfg.contract_max_premium == 8.00


class TestFix07_ATRReachability:
    """M-1: ATR multiplier should be 1.8, not 2.5."""

    def test_atr_reachability_uses_1_8_sigma(self):
        from tradingview.options.tqs_v2.filtering.contract_filter import ATRReachabilityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config
        from unittest.mock import MagicMock

        filt = ATRReachabilityFilter()
        config = TQSv2Config()

        # Contract at 2.0sigma should be REJECTED (2.0 > 1.8)
        contract = MagicMock()
        contract.stock_atr = 5.0
        contract.spot_price = 100.0
        contract.dte = 30
        trading_days = 30 * 5.0 / 7.0
        # Breakeven just beyond 1.8sigma range
        projected_1_8 = 5.0 * (trading_days ** 0.5) * 1.8
        contract.breakeven = 100.0 + projected_1_8 + 1.0
        assert not filt.accepts(contract, config)

    def test_atr_reachability_accepts_within_range(self):
        from tradingview.options.tqs_v2.filtering.contract_filter import ATRReachabilityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config
        from unittest.mock import MagicMock

        filt = ATRReachabilityFilter()
        config = TQSv2Config()

        contract = MagicMock()
        contract.stock_atr = 5.0
        contract.spot_price = 100.0
        contract.dte = 30
        trading_days = 30 * 5.0 / 7.0
        projected_1_8 = 5.0 * (trading_days ** 0.5) * 1.8
        contract.breakeven = 100.0 + projected_1_8 - 1.0  # within 1.8sigma
        assert filt.accepts(contract, config)


class TestFix10_IVSpreadClosed:
    """M-4: IV spread should return 50 (neutral) when market closed, not 60."""

    def test_closed_market_returns_50(self):
        from tradingview.options.tqs_v2.scoring.metrics.iv_analysis import IVScorer
        assert IVScorer._iv_spread_score(0.0, 0.0) == 50.0

    def test_closed_market_not_above_neutral(self):
        from tradingview.options.tqs_v2.scoring.metrics.iv_analysis import IVScorer
        score = IVScorer._iv_spread_score(0.0, 0.0)
        assert score <= 50.0, f"Closed market should not score above neutral 50, got {score}"


class TestFix05_BreakevenUsesAsk:
    """C-5: Breakeven computations should use ask (true cost), not mid."""

    def test_win_rate_uses_ask_for_pop(self):
        """_compute_pop_n_d2 should use premium=ask, not premium=mid."""
        from tradingview.options.tqs_v2.scoring.metrics.win_rate import _compute_pop_n_d2
        # For a CALL: effective_strike = strike + premium
        # Higher premium (ask) -> higher effective_strike -> lower PoP
        pop_mid = _compute_pop_n_d2(
            spot=300.0, strike=310.0, premium=3.40,
            iv=0.22, T=30 / 365, delta_fallback=0.4, option_type="call",
        )
        pop_ask = _compute_pop_n_d2(
            spot=300.0, strike=310.0, premium=3.50,
            iv=0.22, T=30 / 365, delta_fallback=0.4, option_type="call",
        )
        assert pop_ask < pop_mid, "Higher premium (ask) should produce lower PoP"

    def test_premium_efficiency_breakeven_uses_ask(self):
        """PremiumEfficiencyScorer should use ask for breakeven, not mid."""
        from tradingview.options.tqs_v2.scoring.metrics.premium_efficiency import PremiumEfficiencyScorer
        from unittest.mock import MagicMock

        scorer = PremiumEfficiencyScorer()

        # Create two contracts: same mid, different ask
        c1 = MagicMock()
        c1.bid = 3.30; c1.ask = 3.50; c1.spot_price = 300.0; c1.strike = 310.0
        c1.option_type = "call"; c1.iv = 0.22; c1.dte = 30; c1.delta = 0.4; c1.theta = -0.10

        c2 = MagicMock()
        c2.bid = 3.30; c2.ask = 4.00; c2.spot_price = 300.0; c2.strike = 310.0
        c2.option_type = "call"; c2.iv = 0.22; c2.dte = 30; c2.delta = 0.4; c2.theta = -0.10

        d1 = scorer.score_detailed(c1)
        d2 = scorer.score_detailed(c2)
        # Higher ask = further breakeven = lower be_score
        assert d1["be_score"] >= d2["be_score"], "Higher ask should produce lower breakeven score"
