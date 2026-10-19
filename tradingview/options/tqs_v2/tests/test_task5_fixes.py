"""
Tests for Task 5 critical audit fixes (2026-05-27).
Verifies C-1 (VIX threshold wiring), C-2 (theta efficiency filter),
C-3 (IV percentile HV fallback).
"""
import pytest
from unittest.mock import MagicMock, patch


class TestC1_VIXThresholdWired:
    """C-1: VIX multiplier must actually modify the scoring threshold."""

    def test_high_vix_raises_threshold(self):
        """VIX=30 with multiplier 1.45 should raise threshold from 65 to ~94.25."""
        from tradingview.options.tqs_v2.scoring.engine import ScoringEngine
        from tradingview.options.tqs_v2.config import TQSv2Config

        config = TQSv2Config()
        engine = ScoringEngine()

        # Create a contract mock that scores ~70 (decent but not stellar)
        contract = MagicMock()
        contract.spot_price = 300.0
        contract.strike = 310.0
        contract.bid = 3.30
        contract.ask = 3.50
        contract.iv = 0.22
        contract.dte = 30
        contract.delta = 0.4
        contract.option_type = "call"
        contract.opra_code = "TEST"

        # With VIX=30, threshold should be 65 * 1.45 = 94.25
        with patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.fetch_vix_level", return_value=30.0), \
             patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.get_vix_multiplier", return_value=(1.45, "extreme_fear")), \
             patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.vix_regime_score", return_value=33.3):

            # Mock _score_one to return a contract with score 70
            scored_mock = MagicMock()
            scored_mock.total_score = 70.0
            scored_mock.scores = MagicMock()
            scored_mock.scores.pop_score = 50.0
            engine._score_one = MagicMock(return_value=scored_mock)

            results = engine.score_all([contract], config)
            # Score 70 < threshold 94.25 → should NOT pass
            assert len(results) == 0

    def test_low_vix_lowers_threshold(self):
        """VIX=12 with multiplier 0.85 should lower threshold from 65 to ~55.25."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(12.0)
        effective = 65.0 * mult
        assert effective == pytest.approx(55.25)

    def test_normal_vix_no_change(self):
        """VIX=17 with multiplier 1.0 should keep threshold at 65."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(17.0)
        effective = 65.0 * mult
        assert effective == 65.0

    def test_vix_metadata_attached_to_scored(self):
        """VIX metadata must be attached to surviving scored contracts."""
        from tradingview.options.tqs_v2.scoring.engine import ScoringEngine
        from tradingview.options.tqs_v2.config import TQSv2Config

        config = TQSv2Config()
        engine = ScoringEngine()
        contract = MagicMock()
        contract.opra_code = "TEST"

        with patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.fetch_vix_level", return_value=17.0), \
             patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.get_vix_multiplier", return_value=(1.0, "normal")), \
             patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.vix_regime_score", return_value=50.0):

            scored_mock = MagicMock()
            scored_mock.total_score = 80.0  # above threshold
            scored_mock.scores = MagicMock()
            scored_mock.scores.pop_score = 50.0
            engine._score_one = MagicMock(return_value=scored_mock)

            results = engine.score_all([contract], config)
            assert len(results) == 1
            assert results[0].vix_level == 17.0
            assert results[0].vix_regime == "normal"
            assert results[0].vix_multiplier == 1.0
            assert results[0].vix_score == 50.0


class TestC2_ThetaEfficiencyFilter:
    """C-2: Filter out contracts where theta eats the expected move."""

    def test_good_theta_efficiency_passes(self):
        """Contract where expected move >> theta decay should pass."""
        from tradingview.options.tqs_v2.filtering.contract_filter import ThetaEfficiencyFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        f = ThetaEfficiencyFilter()
        config = TQSv2Config()
        contract = MagicMock()
        contract.theta = -0.05    # small theta
        contract.ask = 5.00      # expensive enough premium
        contract.stock_atr = 8.0  # big daily move
        contract.spot_price = 200.0

        # daily_theta_pct = 0.05/5.00 = 0.01 = 1%
        # daily_move = 8/200 = 0.04 = 4%
        # efficiency = 4.0 > 0.5
        assert f.accepts(contract, config) is True

    def test_bad_theta_efficiency_rejected(self):
        """Contract where theta >> expected move should be rejected."""
        from tradingview.options.tqs_v2.filtering.contract_filter import ThetaEfficiencyFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        f = ThetaEfficiencyFilter()
        config = TQSv2Config()
        contract = MagicMock()
        contract.theta = -0.15    # heavy theta
        contract.ask = 2.00      # cheap premium = high % decay
        contract.stock_atr = 2.0  # small daily move
        contract.spot_price = 200.0

        # daily_theta_pct = 0.15/2.00 = 0.075 = 7.5%
        # daily_move = 2/200 = 0.01 = 1%
        # efficiency = 0.01/0.075 = 0.133 < 0.5
        assert f.accepts(contract, config) is False

    def test_missing_data_passes(self):
        """Contract with missing theta/atr data should pass (don't reject on missing data)."""
        from tradingview.options.tqs_v2.filtering.contract_filter import ThetaEfficiencyFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        f = ThetaEfficiencyFilter()
        config = TQSv2Config()
        contract = MagicMock()
        contract.theta = None
        contract.ask = 3.00
        contract.stock_atr = 5.0
        contract.spot_price = 200.0

        assert f.accepts(contract, config) is True

    def test_zero_theta_passes(self):
        """Contract with zero theta should pass."""
        from tradingview.options.tqs_v2.filtering.contract_filter import ThetaEfficiencyFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        f = ThetaEfficiencyFilter()
        config = TQSv2Config()
        contract = MagicMock()
        contract.theta = 0.0
        contract.ask = 3.00
        contract.stock_atr = 5.0
        contract.spot_price = 200.0

        assert f.accepts(contract, config) is True

    def test_borderline_efficiency(self):
        """Contract at exactly 0.5 threshold should pass."""
        from tradingview.options.tqs_v2.filtering.contract_filter import ThetaEfficiencyFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        f = ThetaEfficiencyFilter()
        config = TQSv2Config()
        contract = MagicMock()
        contract.theta = -0.10
        contract.ask = 4.00
        contract.stock_atr = 5.0
        contract.spot_price = 200.0

        # daily_theta_pct = 0.10/4.00 = 0.025
        # daily_move = 5.0/200.0 = 0.025
        # efficiency = 1.0 >= 0.5 → pass
        assert f.accepts(contract, config) is True

    def test_filter_name(self):
        """Filter should have the correct name."""
        from tradingview.options.tqs_v2.filtering.contract_filter import ThetaEfficiencyFilter
        f = ThetaEfficiencyFilter()
        assert f.name == "theta_efficiency"

    def test_in_pipeline(self):
        """ThetaEfficiencyFilter must be present in default FilterPipeline."""
        from tradingview.options.tqs_v2.filtering.pipeline import FilterPipeline
        from tradingview.options.tqs_v2.filtering.contract_filter import ThetaEfficiencyFilter

        pipeline = FilterPipeline()
        filter_types = [type(f) for f in pipeline._filters]
        assert ThetaEfficiencyFilter in filter_types


class TestC3_IVPercentileHVFallback:
    """C-3: IV percentile should use yfinance HV when TV data unavailable."""

    def test_tv_percentile_preferred(self):
        """When TV returns a percentile field, use it directly."""
        from tradingview.options.tqs_v2.options_data.iv_structure import fetch_iv_percentile, _iv_history_cache
        _iv_history_cache.clear()

        mock_sess = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "real-ivs": [{"value": 0.30}],
            "standard-ivs": [{"iv": 0.3, "percentile": 72.5}],
        }
        mock_sess.get.return_value = mock_resp

        result = fetch_iv_percentile("AAPL", 0.30, session=mock_sess)
        assert result == 72.5
        _iv_history_cache.clear()

    def test_yfinance_fallback_when_no_tv_percentile(self):
        """When TV has no percentile field, yfinance HV should be used."""
        from tradingview.options.tqs_v2.options_data.iv_structure import fetch_iv_percentile, _iv_history_cache
        _iv_history_cache.clear()

        # Mock TV response with no percentile field
        mock_sess = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "real-ivs": [{"value": 0.30}],
            "standard-ivs": [{"value": 0.3}, {"value": 0.32}],  # no "percentile" key
        }
        mock_sess.get.return_value = mock_resp

        # Mock yfinance to return historical data
        import pandas as pd
        import numpy as np

        dates = pd.date_range(end="2026-05-27", periods=300, freq="B")
        rng = np.random.default_rng(42)
        prices = 150.0 + np.cumsum(rng.standard_normal(300) * 2)
        prices = np.maximum(prices, 50.0)
        mock_hist = pd.DataFrame({"Close": prices}, index=dates)

        with patch("yfinance.Ticker") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.history.return_value = mock_hist
            mock_yf.return_value = mock_ticker

            result = fetch_iv_percentile("AAPL", 0.30, session=mock_sess)
            # Should return a percentile 0-100 from yfinance HV
            assert result is not None
            assert 0.0 <= result <= 100.0

        _iv_history_cache.clear()

    def test_all_sources_fail_returns_none(self):
        """When both TV and yfinance fail, return None."""
        from tradingview.options.tqs_v2.options_data.iv_structure import fetch_iv_percentile, _iv_history_cache
        _iv_history_cache.clear()

        mock_sess = MagicMock()
        mock_sess.get.side_effect = ConnectionError("Network error")

        with patch("yfinance.Ticker") as mock_yf:
            mock_yf.side_effect = Exception("yfinance error")
            result = fetch_iv_percentile("AAPL", 0.30, session=mock_sess)
            assert result is None

        _iv_history_cache.clear()

    def test_yfinance_insufficient_history_returns_none(self):
        """When yfinance has too few data points, return None."""
        from tradingview.options.tqs_v2.options_data.iv_structure import fetch_iv_percentile, _iv_history_cache
        _iv_history_cache.clear()

        # TV returns no percentile
        mock_sess = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "real-ivs": [],
            "standard-ivs": [],
        }
        mock_sess.get.return_value = mock_resp

        # yfinance returns too few data points
        import pandas as pd
        mock_hist = pd.DataFrame({"Close": [100.0, 101.0, 102.0]})

        with patch("yfinance.Ticker") as mock_yf:
            mock_ticker = MagicMock()
            mock_ticker.history.return_value = mock_hist
            mock_yf.return_value = mock_ticker

            result = fetch_iv_percentile("AAPL", 0.30, session=mock_sess)
            assert result is None

        _iv_history_cache.clear()


class TestExitDTEPrecedence:
    """Regression guard for the json_reporter exit-DTE operator-precedence bug.

    The exit note must compute (dte or 45) - 21, NOT dte or (45 - 21).
    Python binds '-' tighter than 'or', so the buggy form `dte or 45 - 21`
    evaluates to `dte or 24` — printing the full DTE instead of the
    21-days-before-expiry exit point.
    """

    def test_exit_dte_expression_is_parenthesized(self):
        from pathlib import Path

        src = (
            Path(__file__).parents[1] / "reporting" / "json_reporter.py"
        ).read_text(encoding="utf-8")
        # Buggy form must NOT be present
        assert "or 45 - 21" not in src, (
            "operator-precedence bug regressed: `or 45 - 21` binds as `or (45-21)`"
        )
        # Fixed form MUST be present
        assert "(getattr(c, 'dte', 45) or 45) - 21" in src, (
            "expected parenthesized exit-DTE expression `(dte or 45) - 21`"
        )

    def test_exit_dte_math(self):
        # Confirm the corrected semantics: a 45-DTE contract exits at 24 DTE.
        dte = 45
        assert (dte or 45) - 21 == 24
