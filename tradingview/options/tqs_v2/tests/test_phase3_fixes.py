"""
Tests for Phase 3 TQS v2 fixes (2026-05-27).
Verifies C-6 (VIX regime), C-3 (real IV percentile).
"""
import pytest
from unittest.mock import MagicMock, patch


class TestFix06_VIXRegime:
    """C-6: VIX regime detection for market-wide volatility awareness."""

    def test_low_vix_multiplier(self):
        """VIX 0-15 should lower threshold (multiplier < 1.0)."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(12.0)
        assert mult == 0.85
        assert label == "low_vol"

    def test_normal_vix_multiplier(self):
        """VIX 15-20 should be neutral (multiplier = 1.0)."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(17.0)
        assert mult == 1.0
        assert label == "normal"

    def test_elevated_vix_multiplier(self):
        """VIX 20-25 should raise threshold slightly."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(22.0)
        assert mult == 1.10
        assert label == "elevated"

    def test_high_vix_multiplier(self):
        """VIX 25-30 should raise threshold significantly."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(27.0)
        assert mult == 1.25
        assert label == "high"

    def test_extreme_vix_multiplier(self):
        """VIX 30+ should raise threshold to near-prohibitive."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(45.0)
        assert mult == 1.45
        assert label == "extreme_fear"

    @patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.fetch_vix_level", return_value=None)
    def test_none_vix_returns_neutral(self, _mock_fetch):
        """When VIX unavailable, return neutral multiplier."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(None)
        assert mult == 1.0
        assert label == "unknown"

    def test_boundary_15_is_normal(self):
        """VIX=15.0 falls into normal band (lower bound inclusive)."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(15.0)
        assert mult == 1.0
        assert label == "normal"

    def test_boundary_30_is_extreme(self):
        """VIX=30.0 falls into extreme_fear band."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import get_vix_multiplier
        mult, label = get_vix_multiplier(30.0)
        assert mult == 1.45
        assert label == "extreme_fear"

    def test_vix_regime_score_low(self):
        """Low VIX should score high (good for buyers)."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import vix_regime_score
        score = vix_regime_score(10.0)
        assert score == 100.0

    def test_vix_regime_score_high(self):
        """High VIX should score low (bad for buyers)."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import vix_regime_score
        score = vix_regime_score(35.0)
        assert score < 20.0

    def test_vix_regime_score_mid(self):
        """VIX=25 → score = (40-25)/30 * 100 = 50."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import vix_regime_score
        score = vix_regime_score(25.0)
        assert score == 50.0

    @patch("tradingview.options.tqs_v2.scoring.metrics.vix_regime.fetch_vix_level", return_value=None)
    def test_vix_regime_score_none_is_neutral(self, _mock_fetch):
        """No VIX data should return neutral 50."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import vix_regime_score
        score = vix_regime_score(None)
        assert score == 50.0

    def test_vix_regime_score_clamped_at_zero(self):
        """VIX=50 should clamp score to 0 (not negative)."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import vix_regime_score
        score = vix_regime_score(50.0)
        assert score == 0.0

    def test_vix_cache_works(self):
        """Cache should prevent repeated yfinance calls."""
        from tradingview.options.tqs_v2.scoring.metrics.vix_regime import (
            _vix_cache, clear_cache, get_vix_multiplier,
        )
        clear_cache()
        import time
        _vix_cache["^VIX"] = (18.5, time.time())  # inject cache entry
        mult, label = get_vix_multiplier()  # should read from cache, not call yfinance
        assert mult == 1.0  # 18.5 is in normal band
        assert label == "normal"
        clear_cache()


class TestFix03_RealIVPercentile:
    """C-3: IV percentile should use real 52-week rank, not contango proxy."""

    def test_win_rate_uses_real_percentile_when_available(self):
        """When iv_percentile_real is set on contract, win_rate should use it."""
        from tradingview.options.tqs_v2.scoring.metrics.win_rate import WinRateScorer

        scorer = WinRateScorer()

        contract = MagicMock()
        contract.spot_price = 300.0
        contract.strike = 310.0
        contract.bid = 3.30
        contract.ask = 3.50
        contract.iv = 0.22
        contract.dte = 30
        contract.delta = 0.4
        contract.option_type = "call"
        contract.stock_rsi = 55.0
        contract.iv_contango_score = 50.0
        contract.iv_percentile_real = 85.0  # real percentile says IV is HIGH

        result = scorer.score_detailed(contract)
        assert result is not None
        assert "total" in result
        assert "iv_rank_proxy" in result
        # iv_rank_proxy should reflect the real percentile, not contango
        assert result["iv_rank_proxy"] == 85.0

    def test_win_rate_falls_back_to_contango_proxy(self):
        """When iv_percentile_real is None, fall back to contango proxy."""
        from tradingview.options.tqs_v2.scoring.metrics.win_rate import WinRateScorer

        scorer = WinRateScorer()

        contract = MagicMock()
        contract.spot_price = 300.0
        contract.strike = 310.0
        contract.bid = 3.30
        contract.ask = 3.50
        contract.iv = 0.22
        contract.dte = 30
        contract.delta = 0.4
        contract.option_type = "call"
        contract.stock_rsi = 55.0
        contract.iv_contango_score = 30.0  # contango proxy
        contract.iv_percentile_real = None  # no real data

        result = scorer.score_detailed(contract)
        assert result is not None
        # Contango=30 → iv_percentile = 100-30 = 70
        assert result["iv_rank_proxy"] == 70.0

    def test_win_rate_real_percentile_affects_iv_pct_adj(self):
        """High real IV percentile should result in lower iv_pct_adj (penalty)."""
        from tradingview.options.tqs_v2.scoring.metrics.win_rate import WinRateScorer

        scorer = WinRateScorer()

        # Contract with LOW iv_percentile_real (cheap IV = good)
        contract_cheap = MagicMock()
        contract_cheap.spot_price = 300.0
        contract_cheap.strike = 310.0
        contract_cheap.bid = 3.30
        contract_cheap.ask = 3.50
        contract_cheap.iv = 0.22
        contract_cheap.dte = 30
        contract_cheap.delta = 0.4
        contract_cheap.option_type = "call"
        contract_cheap.iv_contango_score = 50.0
        contract_cheap.iv_percentile_real = 10.0  # IV is cheap

        # Contract with HIGH iv_percentile_real (expensive IV = bad)
        contract_expensive = MagicMock()
        contract_expensive.spot_price = 300.0
        contract_expensive.strike = 310.0
        contract_expensive.bid = 3.30
        contract_expensive.ask = 3.50
        contract_expensive.iv = 0.22
        contract_expensive.dte = 30
        contract_expensive.delta = 0.4
        contract_expensive.option_type = "call"
        contract_expensive.iv_contango_score = 50.0
        contract_expensive.iv_percentile_real = 90.0  # IV is expensive

        cheap_result = scorer.score_detailed(contract_cheap)
        expensive_result = scorer.score_detailed(contract_expensive)

        # Cheap IV should get higher iv_pct_adj (bonus) than expensive IV
        assert cheap_result["iv_pct_adj"] > expensive_result["iv_pct_adj"]

    def test_fetch_iv_percentile_returns_value(self):
        """fetch_iv_percentile should return a 0-100 value when API succeeds."""
        from tradingview.options.tqs_v2.options_data.iv_structure import (
            fetch_iv_percentile, _iv_history_cache,
        )

        # Clear cache
        _iv_history_cache.clear()

        # Mock response with standard-ivs containing percentile field
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "real-ivs": [{"value": 0.30}],
            "standard-ivs": [
                {"span": {"value": 1, "unit": "w"}, "value": 0.28, "percentile": 45.0},
                {"span": {"value": 1, "unit": "m"}, "value": 0.30, "percentile": None},
            ],
        }

        mock_sess = MagicMock()
        mock_sess.get.return_value = mock_resp

        result = fetch_iv_percentile("AAPL", 0.30, session=mock_sess)

        assert result is not None
        assert result == 45.0  # from TV percentile field

        _iv_history_cache.clear()

    def test_fetch_iv_percentile_computes_from_values(self):
        """When no percentile field, fall back to yfinance HV percentile (C-3 fix 2026-05-27)."""
        from tradingview.options.tqs_v2.options_data.iv_structure import (
            fetch_iv_percentile, _iv_history_cache,
        )

        _iv_history_cache.clear()

        # No percentile field on any item → yfinance HV fallback
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "real-ivs": [{"value": 0.30}],
            "standard-ivs": [
                {"span": "1w", "value": 0.20, "percentile": None},
                {"span": "1m", "value": 0.25, "percentile": None},
                {"span": "3m", "value": 0.30, "percentile": None},
                {"span": "6m", "value": 0.35, "percentile": None},
            ],
        }

        mock_sess = MagicMock()
        mock_sess.get.return_value = mock_resp

        import pandas as pd
        import numpy as np

        # Build mock yfinance history with known volatility distribution
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
            # yfinance HV fallback should return a valid percentile
            assert result is not None
            assert 0.0 <= result <= 100.0

        _iv_history_cache.clear()

    def test_fetch_iv_percentile_handles_failure(self):
        """fetch_iv_percentile should return None when API fails."""
        from tradingview.options.tqs_v2.options_data.iv_structure import (
            fetch_iv_percentile, _iv_history_cache,
        )

        _iv_history_cache.clear()

        mock_sess = MagicMock()
        mock_sess.get.side_effect = ConnectionError("Network error")

        result = fetch_iv_percentile("AAPL", 0.30, session=mock_sess)
        assert result is None

        _iv_history_cache.clear()

    def test_fetch_iv_percentile_http_error(self):
        """fetch_iv_percentile should return None on HTTP error."""
        from tradingview.options.tqs_v2.options_data.iv_structure import (
            fetch_iv_percentile, _iv_history_cache,
        )

        _iv_history_cache.clear()

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_sess = MagicMock()
        mock_sess.get.return_value = mock_resp

        result = fetch_iv_percentile("AAPL", 0.30, session=mock_sess)
        assert result is None

        _iv_history_cache.clear()
