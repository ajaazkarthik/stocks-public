"""
Tests for tv_screener_fundamentals.py VIX3M term-structure fetch (H3 fix,
2026-07-06 option-14 deep-dive fix pass).

fetch_vix3m_level() mirrors fetch_vix_level() exactly (same cache pattern,
same yfinance fast_info lookup) so score_market_internals()'s previously
always-None vix3m_vix_ratio sub-score can actually receive data.

All tests are network-free — yfinance is mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import tv_screener_fundamentals as tvf


class TestFetchVix3mLevel:

    def setup_method(self):
        # Reset module-level cache before each test so tests don't leak state
        tvf._VIX3M_CACHE.update({"val": None, "ts": 0.0})

    def test_returns_float_on_success(self):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.get.return_value = 19.04
        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = tvf.fetch_vix3m_level()
        assert result == 19.04
        mock_ticker.fast_info.get.assert_called_with("lastPrice")

    def test_uses_vix3m_ticker_symbol(self):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.get.return_value = 20.0
        with patch("yfinance.Ticker", return_value=mock_ticker) as mock_cls:
            tvf.fetch_vix3m_level()
        mock_cls.assert_called_with("^VIX3M")

    def test_returns_none_on_exception(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("network down")):
            result = tvf.fetch_vix3m_level()
        assert result is None

    def test_returns_none_when_lastprice_missing(self):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.get.return_value = None
        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = tvf.fetch_vix3m_level()
        assert result is None

    def test_second_call_within_30min_uses_cache_not_new_fetch(self):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.get.return_value = 19.04
        with patch("yfinance.Ticker", return_value=mock_ticker) as mock_cls:
            first = tvf.fetch_vix3m_level()
            second = tvf.fetch_vix3m_level()
        assert first == second == 19.04
        assert mock_cls.call_count == 1   # second call served from cache


class TestVix3mVixRatioIntegration:
    """
    Mirrors the ratio computation used in signal_stack.py's main() right after
    fetch_vix_level()/fetch_vix3m_level() are called.
    """

    def _ratio(self, vix3m, vix):
        return (round(vix3m / vix, 4) if (vix3m is not None and vix not in (None, 0)) else None)

    def test_contango_ratio_above_one(self):
        assert self._ratio(23.0, 20.0) == 1.15

    def test_backwardation_ratio_below_one(self):
        assert self._ratio(18.0, 20.0) == 0.9

    def test_none_when_vix3m_missing(self):
        assert self._ratio(None, 20.0) is None

    def test_none_when_vix_missing(self):
        assert self._ratio(23.0, None) is None

    def test_none_when_vix_is_zero(self):
        assert self._ratio(23.0, 0.0) is None

    def test_score_market_internals_uses_ratio_when_present(self):
        result = tvf  # noqa: keep import used
        import signal_stack as ss
        internals = {"vix3m_vix_ratio": 1.20}
        scores = ss.score_market_internals(internals, yc={"spread_bps": 50}, vix_level=18.0)
        assert scores.get("vix_term_structure") == 9.0   # strong contango per score_market_internals thresholds

    def test_score_market_internals_none_when_ratio_absent(self):
        import signal_stack as ss
        internals = {}
        scores = ss.score_market_internals(internals, yc={"spread_bps": 50}, vix_level=18.0)
        assert scores.get("vix_term_structure") is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
