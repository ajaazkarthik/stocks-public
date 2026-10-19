"""
TQS v2 Scoring Unit Tests
===========================
Tests all 7 scorers and the ScoringEngine with known inputs.
All tests are deterministic — no network calls.
"""

from __future__ import annotations

import pytest
from datetime import date, timedelta

from ..options_data.models import RawContract
from ..scoring.metrics.liquidity import LiquidityScorer
from ..scoring.metrics.iv_analysis import IVScorer
from ..scoring.metrics.probability import ProbabilityScorer
from ..scoring.metrics.risk_reward import RiskRewardScorer
from ..scoring.metrics.win_rate import WinRateScorer
from ..scoring.metrics.volatility_conditions import VolatilityConditionsScorer
from ..scoring.metrics.premium_efficiency import PremiumEfficiencyScorer
from ..scoring.metrics.stock_trend import StockTrendScorer
from ..scoring.models import ScoreBreakdown
from ..scoring.engine import ScoringEngine
from ..filtering.contract_filter import ATRReachabilityFilter, SMA200AlignmentFilter
from ..config import DEFAULT_CONFIG


def _make_call(
    strike=200.0,
    spot=195.0,
    bid=3.0,
    ask=3.20,
    iv=0.35,
    delta=0.35,
    dte=45,
    oi=2000,
    volume=300,
    theo_price=3.10,
    bid_iv=0.32,
    ask_iv=0.38,
    option_type="call",
    expiration=None,
) -> RawContract:
    """Build a test RawContract using the real constructor."""
    if expiration is None:
        exp_date = date.today() + timedelta(days=dte + 1)
        expiration = int(exp_date.strftime("%Y%m%d"))
    else:
        exp_date = None

    return RawContract(
        opra_code=f"OPRA:TEST{expiration}C{strike}",
        underlying_symbol="NASDAQ:TEST",
        root="TEST",
        exchange="NASDAQ",
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        expiry_date=exp_date,
        bid=bid,
        ask=ask,
        theo_price=theo_price,
        iv=iv,
        delta=delta,
        gamma=0.01,
        theta=-0.05,
        vega=0.1,
        rho=0.02,
        bid_iv=bid_iv,
        ask_iv=ask_iv,
        volume=volume,
        open_interest=oi,
        spot_price=spot,
    )


class TestLiquidityScorer:
    def setup_method(self):
        self.scorer = LiquidityScorer()

    def test_name(self):
        assert self.scorer.name == "liquidity"

    def test_high_oi_high_score(self):
        # Very tight spread (ask=2.02, ~0.99%) ensures spread_score stays high under /12 formula
        c = _make_call(oi=5000, bid=2.0, ask=2.02, volume=500)
        score = self.scorer.score(c)
        assert score >= 90

    def test_zero_oi_low_score(self):
        c = _make_call(oi=0, bid=2.0, ask=2.10, volume=500)
        score = self.scorer.score(c)
        assert score < 60  # 40% weight on OI pulls it down

    def test_wide_spread_penalized(self):
        c_tight = _make_call(bid=3.0, ask=3.06, oi=2000, volume=300)
        c_wide = _make_call(bid=3.0, ask=4.50, oi=2000, volume=300)
        assert self.scorer.score(c_tight) > self.scorer.score(c_wide)

    def test_none_volume_returns_neutral(self):
        c = _make_call(volume=None, oi=2000)
        score = self.scorer.score(c)
        # volume=None → 50 (neutral), should still work
        assert 0 <= score <= 100

    def test_never_raises(self):
        # Pathological input: all zeros
        c = _make_call(bid=0.0, ask=0.0, oi=0, volume=0)
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0


class TestIVScorer:
    def setup_method(self):
        self.scorer = IVScorer()

    def test_name(self):
        assert self.scorer.name == "iv_analysis"

    def test_40_pct_iv_moderate_score(self):
        # Fix 2026-05-27 (C-1): IV scorer now monotonically decreasing for buyers.
        # IV=40% scores ~50 iv_level (not 100 peak), tight spread ~84, term 60
        # → total ≈ 0.40×50 + 0.40×84 + 0.20×60 ≈ 65.6
        c = _make_call(iv=0.40, bid_iv=0.38, ask_iv=0.42)
        score = self.scorer.score(c)
        assert score >= 60

    def test_very_high_iv_penalized(self):
        c_normal = _make_call(iv=0.40, bid_iv=0.38, ask_iv=0.42)
        c_high = _make_call(iv=0.90, bid_iv=0.88, ask_iv=0.92)
        assert self.scorer.score(c_normal) > self.scorer.score(c_high)

    def test_tight_iv_spread_rewarded(self):
        c_tight = _make_call(bid_iv=0.35, ask_iv=0.36)
        c_wide = _make_call(bid_iv=0.25, ask_iv=0.55)
        assert self.scorer.score(c_tight) > self.scorer.score(c_wide)

    def test_score_range(self):
        c = _make_call(iv=0.35, bid_iv=0.33, ask_iv=0.37)
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0


class TestProbabilityScorer:
    def setup_method(self):
        self.scorer = ProbabilityScorer()

    def test_name(self):
        assert self.scorer.name == "probability"

    def test_sweet_zone_delta_call(self):
        c_sweet = _make_call(delta=0.30, dte=45)
        c_deep = _make_call(delta=0.70, dte=45)
        assert self.scorer.score(c_sweet) > self.scorer.score(c_deep)

    def test_dte_peak_at_45(self):
        c_45 = _make_call(delta=0.30, dte=45)
        c_7 = _make_call(delta=0.30, dte=7)
        c_90 = _make_call(delta=0.30, dte=90)
        assert self.scorer.score(c_45) > self.scorer.score(c_7)
        assert self.scorer.score(c_45) > self.scorer.score(c_90)

    def test_put_delta_sign(self):
        c_put = _make_call(delta=-0.30, option_type="put")
        score = self.scorer.score(c_put)
        assert score >= 60   # abs(delta)=0.30 is in sweet zone

    def test_score_range(self):
        c = _make_call(delta=0.30, dte=45)
        assert 0.0 <= self.scorer.score(c) <= 100.0


class TestRiskRewardScorer:
    def setup_method(self):
        self.scorer = RiskRewardScorer()

    def test_name(self):
        assert self.scorer.name == "risk_reward"

    def test_close_breakeven_high_score(self):
        # Spot=195, strike=200, ask=1.0 → breakeven=201 (3.1% above)
        c = _make_call(spot=195.0, strike=200.0, ask=1.0, bid=0.90, theo_price=1.10)
        score = self.scorer.score(c)
        assert score >= 60

    def test_far_breakeven_low_score(self):
        # Spot=100, strike=150, ask=5.0 → breakeven=155 (55% above spot)
        c = _make_call(spot=100.0, strike=150.0, ask=5.0, bid=4.80, theo_price=5.20)
        score = self.scorer.score(c)
        assert score < 40

    def test_iv_rv_cheap_options_score_higher(self):
        """Low IV / RV ratio (cheap options for buyers) scores higher than high IV / RV ratio."""
        # cheap: IV=0.20, ATR=8.0, spot=100 → RV_proxy = (8/100)×√252 ≈ 1.27 → ratio ≈ 0.16
        c_cheap = _make_call(spot=100.0, strike=100.0, bid=1.0, ask=1.20)
        c_cheap.iv = 0.20
        c_cheap.stock_atr = 8.0
        # expensive: IV=1.60, ATR=8.0, spot=100 → ratio ≈ 1.26 > 1.5 → score ≈ 0
        c_exp = _make_call(spot=100.0, strike=100.0, bid=1.0, ask=1.20)
        c_exp.iv = 1.60
        c_exp.stock_atr = 8.0
        assert self.scorer.score(c_cheap) > self.scorer.score(c_exp)

    def test_score_range(self):
        c = _make_call()
        assert 0.0 <= self.scorer.score(c) <= 100.0


class TestScoringEngine:
    def setup_method(self):
        self.engine = ScoringEngine()
        self.config = DEFAULT_CONFIG

    def test_score_all_returns_only_above_threshold(self):
        contracts = [_make_call() for _ in range(5)]
        scored = self.engine.score_all(contracts, self.config)
        assert all(s.total_score >= self.config.min_score_for_report for s in scored)

    def test_score_breakdown_fields(self):
        c = _make_call(iv=0.40, delta=0.30, dte=45, oi=2000, bid=3.0, ask=3.20)
        sc = self.engine.score_one(c)
        assert sc is not None
        assert 0.0 <= sc.scores.liquidity <= 100.0
        assert 0.0 <= sc.scores.iv_analysis <= 100.0
        assert 0.0 <= sc.scores.probability <= 100.0
        assert 0.0 <= sc.scores.risk_reward <= 100.0
        assert 0.0 <= sc.total_score <= 100.0

    def test_grade_assignment(self):
        c = _make_call(iv=0.40, delta=0.30, dte=45, oi=5000, bid=2.0, ask=2.10)
        sc = self.engine.score_one(c)
        assert sc is not None
        assert sc.grade in ("A+", "A", "B+", "B", "C+", "C", "F")

    def test_higher_quality_contract_scores_higher(self):
        c_good = _make_call(oi=4000, iv=0.40, delta=0.35, dte=45, bid=2.0, ask=2.10)
        c_bad = _make_call(oi=50, iv=0.90, delta=0.05, dte=5, bid=0.05, ask=0.50)
        sc_good = self.engine.score_one(c_good)
        sc_bad = self.engine.score_one(c_bad)
        if sc_good and sc_bad:
            assert sc_good.total_score > sc_bad.total_score


class TestScoreBreakdown:
    def test_total_uses_8d_weights(self):
        """ScoreBreakdown.total must match the 8-dimension weighted formula."""
        b = ScoreBreakdown(
            liquidity=80.0,
            iv_analysis=70.0,
            probability=60.0,
            risk_reward=50.0,
            win_rate=40.0,
            volatility_conditions=30.0,
            premium_efficiency=20.0,
            stock_trend=10.0,
        )
        # Actual weights: prob=15, liq=15, rr=16, wr=11, iv=11, trend=18, vc=6, pe=8
        expected = (
            0.15 * 60.0   # probability
            + 0.15 * 80.0   # liquidity
            + 0.16 * 50.0   # risk_reward
            + 0.11 * 40.0   # win_rate
            + 0.11 * 70.0   # iv_analysis
            + 0.18 * 10.0   # stock_trend (raised 12%→18%)
            + 0.06 * 30.0   # volatility_conditions
            + 0.08 * 20.0   # premium_efficiency
        )  # = 9.0+12.0+8+4.4+7.7+1.8+1.8+1.6 = 46.3
        assert abs(b.total - expected) < 0.001

    def test_grade_a_plus_at_90(self):
        """All 8 dimensions at 90 → weighted total == 90 → A+."""
        b = ScoreBreakdown(
            liquidity=90.0, iv_analysis=90.0, probability=90.0, risk_reward=90.0,
            win_rate=90.0, volatility_conditions=90.0, premium_efficiency=90.0,
            stock_trend=90.0,
        )
        # sum of weights == 1.0, so total = 90.0
        assert b.grade in ("A+", "A")

    def test_grade_f_below_60(self):
        b = ScoreBreakdown(
            liquidity=40.0, iv_analysis=40.0, probability=40.0, risk_reward=40.0
        )
        assert b.grade == "F"


class TestWinRateScorer:
    """Tests for WinRateScorer — PoP proxy + IV percentile + DTE zone."""

    def setup_method(self):
        self.scorer = WinRateScorer()

    def test_name(self):
        assert self.scorer.name == "win_rate"

    def test_peak_zone_dte_30_60(self):
        """DTE in 30-60 range gives full DTE factor (1.0)."""
        c_peak = _make_call(delta=0.30, dte=45)
        c_short = _make_call(delta=0.30, dte=10)
        assert self.scorer.score(c_peak) >= self.scorer.score(c_short)

    def test_higher_delta_higher_pop_score(self):
        """When iv=0 (N(d2) fallback), larger abs(delta) → higher PoP → higher score."""
        c_high = _make_call(delta=0.65, dte=45)
        c_low = _make_call(delta=0.10, dte=45)
        # Null out iv so _compute_pop_n_d2 hits ZeroDivisionError and falls back to delta
        c_high.iv = 0.0
        c_low.iv = 0.0
        assert self.scorer.score(c_high) > self.scorer.score(c_low)

    def test_iv_percentile_penalty_for_buyers(self):
        """HIGH iv_rank = expensive premiums = penalises option BUYERS' win rate."""
        c = _make_call(delta=0.30, dte=45)
        c.iv_contango_score = 20.0  # low contango → iv_rank_proxy=80% (expensive) → small adj
        score_expensive = self.scorer.score(c)
        c.iv_contango_score = 80.0  # high contango → iv_rank_proxy=20% (cheap) → large adj
        score_cheap = self.scorer.score(c)
        # Buyers want CHEAP options (low IV rank) — cheap should score higher
        assert score_cheap > score_expensive

    def test_score_range(self):
        c = _make_call(delta=0.35, dte=45)
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0

    def test_never_raises_on_none_delta(self):
        c = _make_call()
        c.delta = None
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0


class TestVolatilityConditionsScorer:
    """Tests for VolatilityConditionsScorer — PCR + skew directional analysis."""

    def setup_method(self):
        self.scorer = VolatilityConditionsScorer()

    def test_name(self):
        assert self.scorer.name == "volatility_conditions"

    def test_none_pcr_and_skew_gives_midpoint(self):
        """Without enrichment data, should return a neutral mid-range score."""
        c = _make_call()
        c.voi_pcr = None
        c.iv_skew_25d = None
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0

    def test_low_pcr_favors_calls(self):
        """PCR < 0.7 is bullish (good for calls)."""
        c = _make_call(option_type="call")
        c.voi_pcr = 0.4
        c.iv_skew_25d = None
        score_call = self.scorer.score(c)
        c.option_type = "put"
        score_put = self.scorer.score(c)
        assert score_call > score_put

    def test_high_pcr_favors_puts(self):
        """PCR > 1.5 is bearish (good for puts)."""
        c = _make_call(option_type="put")
        c.voi_pcr = 2.0
        c.iv_skew_25d = None
        score_put = self.scorer.score(c)
        c.option_type = "call"
        score_call = self.scorer.score(c)
        assert score_put > score_call

    def test_score_range(self):
        c = _make_call()
        c.voi_pcr = 1.0
        c.iv_skew_25d = 0.02
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0


class TestPremiumEfficiencyScorer:
    """Tests for PremiumEfficiencyScorer — time value + spread + breakeven."""

    def setup_method(self):
        self.scorer = PremiumEfficiencyScorer()

    def test_name(self):
        assert self.scorer.name == "premium_efficiency"

    def test_tight_spread_rewarded(self):
        """Bid/ask spread ratio below 5% gives a high spread efficiency score."""
        c_tight = _make_call(bid=3.0, ask=3.06)  # 2% spread
        c_wide = _make_call(bid=3.0, ask=4.50)   # 50% spread
        # Neutralise theta (set to 0) so the comparison is purely about spread quality.
        # Without this, the same absolute theta is a smaller % of the higher ask,
        # which would give c_wide an unintended theta_score advantage.
        c_tight.theta = 0.0
        c_wide.theta = 0.0
        assert self.scorer.score(c_tight) > self.scorer.score(c_wide)

    def test_far_breakeven_penalizes(self):
        """Very far OTM with high premium has a bad breakeven score."""
        c_good = _make_call(spot=195.0, strike=200.0, ask=1.0, iv=0.35, dte=45)
        c_bad = _make_call(spot=100.0, strike=180.0, ask=10.0, iv=0.35, dte=10)
        assert self.scorer.score(c_good) >= self.scorer.score(c_bad)

    def test_score_range(self):
        c = _make_call()
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0

    def test_never_raises_on_zero_bid(self):
        c = _make_call(bid=0.0, ask=0.01)
        score = self.scorer.score(c)
        assert 0.0 <= score <= 100.0


class TestStockTrendScorer:
    """Tests for StockTrendScorer ADX adjustment."""

    def setup_method(self):
        self.scorer = StockTrendScorer()

    def test_adx_strong_trend_amplifies_bullish_signal(self):
        """ADX > 30 → multiplier 1.4 amplifies directional signal deviation from neutral."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = 195.0   # above SMA50 → +20 for call
        c.stock_rsi = 55.0      # sweet spot → +10
        c.stock_macd = 0.5      # positive → +10
        c.stock_adx = 35.0      # strong trend → multiplier 1.4
        detail = self.scorer.score_detailed(c)
        # adx_adj now holds the multiplier (1.4) not an additive offset
        assert detail["adx_adj"] == 1.4
        assert detail["adx_used"] is True
        # base=50 + 20 + 10 + 10 = 90; deviation=40; score = 50 + 40*1.4 = 106 → 100
        assert detail["total"] == 100.0

    def test_adx_ranging_dampens_bullish_signal(self):
        """ADX < 14 → multiplier 0.7 dampens directional signal toward neutral."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = 195.0   # above SMA50 → +20
        c.stock_rsi = 55.0      # +10
        c.stock_macd = None
        c.stock_adx = 10.0      # ranging market → multiplier 0.7
        detail = self.scorer.score_detailed(c)
        assert detail["adx_adj"] == 0.7
        # base=50 + 20 + 10 = 80; deviation=30; score = 50 + 30*0.7 = 71
        assert detail["total"] == 71.0

    def test_adx_moderate_trend_no_neutral_effect(self):
        """ADX in [20, 30] → multiplier 1.2; neutral base (50) is unchanged (zero deviation)."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = None
        c.stock_rsi = None
        c.stock_macd = None
        c.stock_adx = 25.0      # moderate trend
        detail = self.scorer.score_detailed(c)
        assert detail["adx_adj"] == 1.2
        # base=50; deviation=0; 50 + 0*1.2 = 50 (amplifying zero deviation = no change)
        assert detail["total"] == 50.0

    def test_adx_none_no_adjustment(self):
        """No ADX data → multiplier 1.0 (identity), score unchanged, no crash."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = None
        c.stock_rsi = None
        c.stock_macd = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        assert detail["adx_adj"] == 1.0   # identity multiplier
        assert detail["adx_used"] is False
        assert detail["total"] == 50.0   # pure neutral

    def test_score_clamped_to_100(self):
        """Score never exceeds 100 even with all bullish signals + strong ADX."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = 100.0   # well above SMA50 → +20
        c.stock_rsi = 55.0      # sweet spot → +10
        c.stock_macd = 1.0      # positive → +10
        c.stock_adx = 50.0      # very strong trend → +10
        score = self.scorer.score(c)
        assert score == 100.0

    def test_score_clamped_to_zero(self):
        """Score never goes below 0."""
        c = _make_call(option_type="put", spot=200.0)
        # stock ABOVE SMA50 = bullish = headwind for puts → -30
        c.stock_sma50 = 150.0   # spot(200) > sma50(150) → -30 for put
        c.stock_rsi = 10.0      # oversold → -50 penalty for put buyers
        c.stock_macd = 1.0      # (suppressed by oversold flag)
        c.stock_adx = 5.0       # ranging → -10
        score = self.scorer.score(c)
        assert score == 0.0

    def test_rsi_adx_exception_call_strong_trend(self):
        """
        REC 4 (2026-05-27): When ADX > 30 AND RSI > 75, CALL penalty reduces from -50 to -20.
        Strong trend regime: RSI overbought is momentum persistence, not reversal signal.
        base=50, sma200=None, sma50=above(+20), RSI>75+ADX>30(-20 not -50), MACD suppressed.
        Expected: 50 + 20 - 20 = 50; ADX 1.4 applied: 50 + (50-50)*1.4 = 50
        Actually: score_before_adx = 50 + 20 - 20 = 50 exactly; neutral → adx has zero effect.
        Let's use a scenario where adx effect is visible: no SMA50, RSI>75+ADX>30.
        score_before_adx = 50 + 0 - 20 = 30; ADX mult 1.4: 50 + (30-50)*1.4 = 50 - 28 = 22.
        """
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = None    # no SMA50
        c.stock_rsi = 80.0      # overbought
        c.stock_adx = 35.0      # strong trend → exception applies
        c.stock_macd = None
        detail = self.scorer.score_detailed(c)
        # With exception: RSI penalty = -20 (not -50)
        # score_before_adx = 50 - 20 = 30; adx_mult = 1.4
        # final = 50 + (30 - 50) * 1.4 = 50 - 28 = 22
        assert detail["total"] == 22.0

    def test_rsi_adx_exception_call_no_exception_weak_trend(self):
        """
        No ADX exception when ADX is weak (< 30): full -50 penalty applies.
        """
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = None    # no SMA50
        c.stock_rsi = 80.0      # overbought
        c.stock_adx = 20.0      # moderate trend → NO exception, full -50 penalty
        c.stock_macd = None
        detail = self.scorer.score_detailed(c)
        # Without exception: RSI penalty = -50
        # score_before_adx = 50 - 50 = 0; adx_mult = 1.2
        # final = 50 + (0 - 50) * 1.2 = 50 - 60 = -10 → clamped to 0
        assert detail["total"] == 0.0

    def test_rsi_adx_exception_call_boundary_adx_exactly_30(self):
        """
        ADX exactly at 30.0: NOT > 30, so full -50 penalty applies (strict > check).
        """
        c = _make_call(option_type="call", spot=200.0)
        c.stock_sma50 = None
        c.stock_rsi = 80.0      # overbought
        c.stock_adx = 30.0      # exactly 30 → NOT > 30 → full -50 penalty
        c.stock_macd = None
        detail = self.scorer.score_detailed(c)
        # Full -50 penalty: score_before_adx = 0; adx_mult = 1.2 (20-30 range)
        # final = 50 + (0 - 50) * 1.2 = -10 → clamped to 0
        assert detail["total"] == 0.0

    def test_rsi_adx_exception_put_strong_downtrend(self):
        """
        REC 4 symmetric for PUTs: When ADX > 30 AND RSI < 25, PUT penalty reduces from -50 to -20.
        Strong downtrend: oversold RSI persists; full -50 penalty would miss real momentum puts.
        """
        c = _make_call(option_type="put", spot=200.0)
        c.stock_sma50 = None    # no SMA50
        c.stock_rsi = 20.0      # oversold (< 25)
        c.stock_adx = 35.0      # strong trend → exception applies
        c.stock_macd = None
        detail = self.scorer.score_detailed(c)
        # With exception: RSI penalty = -20 (not -50)
        # score_before_adx = 50 - 20 = 30; adx_mult = 1.4
        # final = 50 + (30 - 50) * 1.4 = 50 - 28 = 22
        assert detail["total"] == 22.0

    # ─── Gap 3: RSI sweet spot tightened 45.0→55.0 (PineScript RSI>=60) ────────

    def test_rsi_sweet_spot_call_at_55_inclusive(self):
        """RSI exactly 55.0 gets the sweet spot +10 bonus (lower boundary inclusive)."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_rsi = 55.0
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + RSI(+10) = 60; deviation=10; adx_mult=1.0; final=60
        assert detail["total"] == 60.0

    def test_rsi_sweet_spot_call_below_55_excluded(self):
        """RSI at 54.9 does NOT get the sweet spot bonus (gap tightened from 45→55)."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_rsi = 54.9
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # 54.9 is in (35,55) — no RSI bonus, no penalty; final=50
        assert detail["total"] == 50.0

    def test_rsi_old_boundary_44_now_excluded(self):
        """RSI=44.9 was in old [45,70] sweet spot, now excluded after gap fix."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_rsi = 44.9
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        assert detail["total"] == 50.0

    # ─── Gap 2: MACD histogram replaces raw MACD>0 check ───────────────────────

    def test_macd_histogram_call_bullish(self):
        """MACD > signal (positive histogram) confirms bullish momentum → adj +10."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_macd = 0.8
        c.stock_macd_signal = 0.5   # hist = +0.3 → bullish
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + MACD_hist(+10) = 60; final=60
        assert detail["total"] == 60.0
        assert detail["macd_signal_used"] is True

    def test_macd_histogram_call_bearish(self):
        """MACD < signal (negative histogram) is bearish → adj -10 for CALLs."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_macd = 0.5
        c.stock_macd_signal = 0.8   # hist = -0.3 → bearish
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + MACD_hist(-10) = 40; final=40
        assert detail["total"] == 40.0

    def test_macd_histogram_put_bearish_confirmation(self):
        """Negative histogram confirms bearish momentum for PUTs → adj +10."""
        c = _make_call(option_type="put", spot=200.0)
        c.stock_macd = -0.3
        c.stock_macd_signal = 0.2   # hist = -0.5 < 0 → bearish confirmation for put
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + MACD_hist(+10) = 60; final=60
        assert detail["total"] == 60.0

    def test_macd_histogram_fallback_no_signal(self):
        """When macd_signal is None, fallback to raw macd>0 check (prior behaviour)."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_macd = 0.5
        c.stock_macd_signal = None  # no signal → fallback path
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # Fallback: macd > 0 → adj +10; final=60
        assert detail["total"] == 60.0
        assert detail["macd_signal_used"] is False

    # ─── Gap 1: DI+/DI- polarity (PineScript: plusDI > minusDI) ────────────────

    def test_di_polarity_call_bullish(self):
        """DI+ > DI- = buyers dominate → bullish confirmation for CALLs → adj +10."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_plus_di = 35.0
        c.stock_minus_di = 20.0
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + DI(+15×scale) where |35-20|=15, scale=min(1,15/15)=1.0 → adj=+15
        assert detail["total"] == 65.0
        assert detail["di_polarity_used"] is True

    def test_di_polarity_call_bearish(self):
        """DI- >= DI+ = sellers dominate → significant headwind for CALLs → adj -20."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_plus_di = 20.0
        c.stock_minus_di = 35.0
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + DI(-20) = 30; final=30
        assert detail["total"] == 30.0

    def test_di_polarity_put_bearish_confirmation(self):
        """DI- > DI+ for PUTs = sellers dominate → bearish confirmation → adj +15 (scaled)."""
        c = _make_call(option_type="put", spot=200.0)
        c.stock_plus_di = 20.0
        c.stock_minus_di = 35.0
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + DI(+15×scale) where |20-35|=15, scale=min(1,15/15)=1.0 → adj=+15
        assert detail["total"] == 65.0

    def test_di_polarity_put_against_direction(self):
        """DI+ > DI- for PUTs = buyers dominate → significant headwind for puts → adj -20."""
        c = _make_call(option_type="put", spot=200.0)
        c.stock_plus_di = 35.0
        c.stock_minus_di = 20.0
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        # base=50 + DI(-20) = 30; final=30
        assert detail["total"] == 30.0

    def test_di_polarity_none_no_effect(self):
        """No DI data → no adjustment, graceful fallback."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_plus_di = None
        c.stock_minus_di = None
        c.stock_sma50 = None
        c.stock_adx = None
        detail = self.scorer.score_detailed(c)
        assert detail["di_polarity_used"] is False
        assert detail["total"] == 50.0

    # ─── Gap 5: Momentum chasing guard (anti-spike) ──────────────────────────────

    def test_momentum_guard_applied_when_change_over_5pct(self):
        """change_pct > 5% triggers -15 penalty; optimal entry has already passed."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_rsi = 60.0      # sweet spot → +10; pre-guard score=60
        c.stock_sma50 = None
        c.stock_adx = None
        c.stock_change_pct = 6.0  # >5% → penalty applies
        detail = self.scorer.score_detailed(c)
        # pre-guard=60; after guard: 60-15=45; deviation=45-50=-5; mult=1.0; final=45
        assert detail["total"] == 45.0
        assert detail["momentum_guard_applied"] is True

    def test_momentum_guard_not_applied_at_exactly_5pct(self):
        """change_pct == 5.0 does NOT trigger guard (strictly > 5.0 required)."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_rsi = 60.0
        c.stock_sma50 = None
        c.stock_adx = None
        c.stock_change_pct = 5.0  # exactly 5.0 → NOT > 5.0 → no penalty
        detail = self.scorer.score_detailed(c)
        assert detail["total"] == 60.0
        assert detail["momentum_guard_applied"] is False

    def test_momentum_guard_applied_on_negative_spike(self):
        """Negative change_pct also triggers guard (abs value: both directions matter)."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_rsi = 60.0
        c.stock_sma50 = None
        c.stock_adx = None
        c.stock_change_pct = -6.0  # large negative spike → penalty still applies
        detail = self.scorer.score_detailed(c)
        assert detail["total"] == 45.0
        assert detail["momentum_guard_applied"] is True

    def test_momentum_guard_none_no_effect(self):
        """change_pct=None (no data) → no penalty, graceful fallback."""
        c = _make_call(option_type="call", spot=200.0)
        c.stock_rsi = 60.0
        c.stock_sma50 = None
        c.stock_adx = None
        c.stock_change_pct = None
        detail = self.scorer.score_detailed(c)
        assert detail["total"] == 60.0
        assert detail["momentum_guard_applied"] is False


class TestATRReachabilityFilter:
    """Tests for ATRReachabilityFilter."""

    def setup_method(self):
        self.f = ATRReachabilityFilter()
        self.cfg = DEFAULT_CONFIG

    def test_name(self):
        assert self.f.name == "atr_reachability"

    def test_no_atr_passes(self):
        """Missing ATR data → contract passes (non-blocking filter)."""
        c = _make_call(spot=200.0, strike=210.0, dte=30, ask=2.0, bid=1.90)
        # No stock_atr attribute set → should pass
        assert self.f.accepts(c, self.cfg) is True

    def test_zero_atr_passes(self):
        """Zero ATR → passes (guard against division weirdness)."""
        c = _make_call(spot=200.0, strike=210.0, dte=30, ask=2.0, bid=1.90)
        c.stock_atr = 0.0
        assert self.f.accepts(c, self.cfg) is True

    def test_in_range_passes(self):
        """
        breakeven = strike + ask = 210 + 2 = 212.
        breakeven_distance = |212 - 200| = 12.
        projected_range = ATR(5) × sqrt(30×5/7) × 1.8 = 5 × 4.63 × 1.8 ≈ 41.7.
        12 <= 41.7 → passes.
        """
        c = _make_call(spot=200.0, strike=210.0, ask=2.0, bid=1.90, dte=30)
        c.stock_atr = 5.0
        assert self.f.accepts(c, self.cfg) is True

    def test_out_of_range_rejected(self):
        """
        breakeven = 300 + 2 = 302.
        breakeven_distance = |302 - 200| = 102.
        projected_range = ATR(5) × sqrt(30×5/7) × 1.8 ≈ 41.7.
        102 > 41.7 → rejected.
        """
        c = _make_call(spot=200.0, strike=300.0, ask=2.0, bid=1.90, dte=30)
        c.stock_atr = 5.0
        assert self.f.accepts(c, self.cfg) is False

    def test_longer_dte_more_permissive(self):
        """
        Longer DTE = larger projected range = less likely to reject.
        Same breakeven distance, but 90 DTE allows more movement than 7 DTE.
        """
        c_long = _make_call(spot=200.0, strike=215.0, ask=2.0, bid=1.90, dte=90)
        c_short = _make_call(spot=200.0, strike=215.0, ask=2.0, bid=1.90, dte=7)
        c_long.stock_atr = 3.0
        c_short.stock_atr = 3.0
        # long DTE: projected = 3 × sqrt(90×5/7) × 1.8 ≈ 43.3; BE dist = 17 → pass
        # short DTE: projected = 3 × sqrt(5) × 1.8 ≈ 12.1; BE dist = 17 → fail
        assert self.f.accepts(c_long, self.cfg) is True
        assert self.f.accepts(c_short, self.cfg) is False


class TestSMA200AlignmentFilter:
    """Tests for SMA200AlignmentFilter — hard gate on directional alignment."""

    def setup_method(self):
        self.f = SMA200AlignmentFilter()
        self.cfg = DEFAULT_CONFIG  # call_sma200_reject_below_pct=0.03, put_sma200_reject_above_pct=0.10

    def test_name(self):
        assert self.f.name == "sma200_alignment"

    # ── Missing data → pass-through ─────────────────────────────────────────

    def test_no_sma200_passes(self):
        """No SMA200 data → non-blocking, passes."""
        c = _make_call(spot=200.0, strike=210.0, dte=30, ask=2.0, bid=1.90)
        assert self.f.accepts(c, self.cfg) is True

    def test_no_spot_passes(self):
        """No spot data → non-blocking, passes."""
        c = _make_call(spot=200.0, strike=210.0, dte=30, ask=2.0, bid=1.90)
        c.stock_sma200 = 195.0
        c.spot_price = None
        assert self.f.accepts(c, self.cfg) is True

    def test_zero_sma200_passes(self):
        """Zero SMA200 → guard against division error, passes."""
        c = _make_call(spot=200.0, strike=210.0, dte=30, ask=2.0, bid=1.90)
        c.stock_sma200 = 0.0
        assert self.f.accepts(c, self.cfg) is True

    def test_unknown_option_type_passes(self):
        """Unknown option type → non-blocking, passes."""
        c = _make_call(spot=200.0, strike=210.0, dte=30, ask=2.0, bid=1.90)
        c.stock_sma200 = 250.0  # would normally reject a call
        c.option_type = "unknown"
        assert self.f.accepts(c, self.cfg) is True

    # ── CALL logic ────────────────────────────────────────────────────────────

    def test_call_above_sma200_passes(self):
        """
        CALL where stock is above SMA200 → always passes.
        spot=200, sma200=190 → spot > sma200 → passes.
        """
        c = _make_call(spot=200.0, strike=210.0, dte=30, ask=2.0, bid=1.90)
        c.stock_sma200 = 190.0
        assert self.f.accepts(c, self.cfg) is True

    def test_call_just_below_sma200_within_threshold_passes(self):
        """
        CALL with stock 2% below SMA200 (< 3% threshold) → passes.
        spot=196, sma200=200 → 2% below → passes (threshold is 3%).
        """
        c = _make_call(spot=196.0, strike=205.0, dte=30, ask=2.0, bid=1.90)
        c.stock_sma200 = 200.0
        assert self.f.accepts(c, self.cfg) is True

    def test_call_exactly_at_threshold_passes(self):
        """
        CALL with stock exactly 3% below SMA200 → passes (threshold is exclusive).
        spot = 200 × (1 - 0.03) = 194.0 → reject_threshold = 194.0; spot == threshold → NOT < → passes.
        """
        c = _make_call(spot=194.0, strike=200.0, dte=30, ask=2.0, bid=1.90)
        c.stock_sma200 = 200.0
        assert self.f.accepts(c, self.cfg) is True

    def test_call_beyond_threshold_rejected(self):
        """
        CALL where stock is more than 3% below SMA200 → REJECTED.
        spot=190, sma200=200 → 5% below → rejected.
        """
        c = _make_call(spot=190.0, strike=200.0, dte=30, ask=2.0, bid=1.90)
        c.stock_sma200 = 200.0
        assert self.f.accepts(c, self.cfg) is False

    def test_call_msft_scenario(self):
        """
        MSFT scenario from Cycle 2: spot≈388, sma200≈430, 9.8% below → REJECTED.
        """
        c = _make_call(spot=388.0, strike=450.0, dte=28, ask=1.50, bid=1.40)
        c.stock_sma200 = 430.0
        assert self.f.accepts(c, self.cfg) is False  # 388 < 430*(1-0.03)=416.9

    # ── PUT logic ──────────────────────────────────────────────────────────────

    def test_put_below_sma200_passes(self):
        """
        PUT where stock is below SMA200 → always passes.
        spot=180, sma200=200 → spot < sma200 → passes.
        """
        c = _make_call(spot=180.0, strike=170.0, dte=30, ask=2.0, bid=1.90, option_type="put")
        c.stock_sma200 = 200.0
        assert self.f.accepts(c, self.cfg) is True

    def test_put_just_above_sma200_within_threshold_passes(self):
        """
        PUT with stock 3% above SMA200 (< 10% threshold) → passes.
        spot=206, sma200=200 → 3% above → passes (threshold is 10%).
        """
        c = _make_call(spot=206.0, strike=195.0, dte=30, ask=2.0, bid=1.90, option_type="put")
        c.stock_sma200 = 200.0
        assert self.f.accepts(c, self.cfg) is True

    def test_put_beyond_threshold_rejected(self):
        """
        PUT where stock is more than 10% above SMA200 → REJECTED.
        spot=222, sma200=200 → 11% above → rejected.
        """
        c = _make_call(spot=222.0, strike=200.0, dte=30, ask=2.0, bid=1.90, option_type="put")
        c.stock_sma200 = 200.0
        assert self.f.accepts(c, self.cfg) is False

    def test_put_exactly_at_threshold_passes(self):
        """
        PUT with stock exactly 10% above SMA200 → passes (threshold is exclusive).
        spot = 200 × (1 + 0.10) = 220 → reject_threshold = 220.0; spot == threshold → NOT > → passes.
        """
        c = _make_call(spot=220.0, strike=200.0, dte=30, ask=2.0, bid=1.90, option_type="put")
        c.stock_sma200 = 200.0
        assert self.f.accepts(c, self.cfg) is True

    def test_put_sbux_scenario(self):
        """
        SBUX scenario from Cycle 2: stock above BOTH SMAs (uptrend) → PUT should be rejected.
        spot=85 (above sma200=75) by 13.3% → rejected since > 10% threshold.
        """
        c = _make_call(spot=85.0, strike=70.0, dte=25, ask=1.80, bid=1.70, option_type="put")
        c.stock_sma200 = 75.0
        assert self.f.accepts(c, self.cfg) is False

    def test_put_shop_scenario(self):
        """
        SHOP scenario from Cycle 2: stock 36% below SMA200 → PUT passes (correct direction).
        spot=64, sma200=100 → well below SMA200 → passes.
        """
        c = _make_call(spot=64.0, strike=55.0, dte=20, ask=2.10, bid=2.00, option_type="put")
        c.stock_sma200 = 100.0
        assert self.f.accepts(c, self.cfg) is True
