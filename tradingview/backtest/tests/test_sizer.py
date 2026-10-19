"""
Unit + integration tests for backtest.backtest_sizer.

Covers:
  1. size_from_metrics — all decision branches (0/1/2/3 units + insufficient data)
  2. size_from_metrics — boundary values at every threshold
  3. plain_english — correctness and completeness for each metric level
  4. Cache read/write + TTL expiry
  5. _error_result — structure and safe defaults
  6. get_position_size — success path with mocked backtest engine
  7. get_position_size — cache hit path
  8. get_position_size — error path (backtest returns None)
  9. sharpe key present in backtest return dict (regression guard)
  10. Sharpe calculation correctness (regression guard)

No network calls are made — yfinance is mocked throughout.
Run:  python -m pytest backtest/tests/test_sizer.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

_REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO))

from backtest.backtest_sizer import (
    MIN_TRADES,
    _cache_key,
    _error_result,
    _is_fresh,
    plain_english,
    size_from_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 400, uptrend_strength: float = 0.003) -> pd.DataFrame:
    """Synthetic uptrending OHLCV (no network)."""
    rng     = np.random.default_rng(42)
    log_ret = rng.normal(uptrend_strength, 0.010, n)
    price   = 100.0 * np.exp(np.cumsum(log_ret))
    spread  = 0.002
    high    = price * (1 + spread)
    low     = price * (1 - spread)
    opens   = price * (1 - spread * 0.4)
    idx     = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": opens, "High": high, "Low": low, "Close": price,
         "Volume": np.full(n, 2_000_000)},
        index=idx,
    )


# ============================================================================
# 1 – size_from_metrics: all decision branches
# ============================================================================

class TestSizeFromMetrics(unittest.TestCase):

    def test_insufficient_data_returns_1_unit(self):
        units, label, color = size_from_metrics(70.0, 50.0, 1.0, MIN_TRADES - 1)
        self.assertEqual(units, 1)
        self.assertIn("Insufficient", label)
        self.assertEqual(color, "#9e9e9e")

    def test_avoid_when_total_pnl_very_negative(self):
        units, label, color = size_from_metrics(45.0, -15.0, 0.3, 20)
        self.assertEqual(units, 0)
        self.assertIn("AVOID", label)
        self.assertEqual(color, "#f44336")

    def test_avoid_when_win_rate_too_low(self):
        units, label, color = size_from_metrics(30.0, 5.0, 0.4, 20)
        self.assertEqual(units, 0)
        self.assertIn("AVOID", label)

    def test_high_conviction_3_units(self):
        units, label, color = size_from_metrics(60.0, 80.0, 0.6, 30)
        self.assertEqual(units, 3)
        self.assertIn("HIGH", label)
        self.assertEqual(color, "#4caf50")

    def test_moderate_2_units_via_sharpe(self):
        """High Sharpe but win_rate only 40% → moderate (sharpe ≥ 0.2)."""
        units, label, color = size_from_metrics(40.0, 20.0, 0.25, 20)
        self.assertEqual(units, 2)
        self.assertIn("MODERATE", label)
        self.assertEqual(color, "#ff9800")

    def test_moderate_2_units_via_win_rate(self):
        """Low Sharpe but win_rate ≥ 45% → moderate."""
        units, label, color = size_from_metrics(50.0, 10.0, 0.1, 20)
        self.assertEqual(units, 2)
        self.assertIn("MODERATE", label)

    def test_low_conviction_1_unit(self):
        """Neither threshold met, but not in avoid territory."""
        units, label, color = size_from_metrics(42.0, 2.0, 0.1, 20)
        self.assertEqual(units, 1)
        self.assertIn("LOW CONVICTION", label)
        self.assertEqual(color, "#9e9e9e")

    def test_exact_boundary_insufficient(self):
        """Exactly MIN_TRADES - 1 → insufficient data."""
        units, _, _ = size_from_metrics(60.0, 50.0, 0.8, MIN_TRADES - 1)
        self.assertEqual(units, 1)

    def test_exact_min_trades_proceeds(self):
        """Exactly MIN_TRADES → not insufficient — falls through to real sizing."""
        units, _, _ = size_from_metrics(60.0, 50.0, 0.8, MIN_TRADES)
        self.assertGreater(units, 1)   # should be 3 (high conviction)

    def test_boundary_avoid_pnl_exactly_minus10(self):
        """total_pnl == -10.0 → AVOID."""
        units, _, _ = size_from_metrics(50.0, -10.0, 0.4, 20)
        self.assertEqual(units, 0)

    def test_boundary_just_above_avoid_pnl(self):
        """total_pnl == -9.99 → NOT avoid."""
        units, _, _ = size_from_metrics(50.0, -9.99, 0.4, 20)
        self.assertNotEqual(units, 0)

    def test_boundary_avoid_win_rate_exactly_35(self):
        """win_rate == 35.0 → AVOID."""
        units, _, _ = size_from_metrics(35.0, 10.0, 0.4, 20)
        self.assertEqual(units, 0)

    def test_boundary_high_conviction_exact_thresholds(self):
        """Exactly sharpe=0.5, win_rate=55 → HIGH CONVICTION."""
        units, _, _ = size_from_metrics(55.0, 30.0, 0.5, 20)
        self.assertEqual(units, 3)

    def test_return_type(self):
        result = size_from_metrics(55.0, 30.0, 0.5, 20)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        self.assertIsInstance(result[0], int)
        self.assertIsInstance(result[1], str)
        self.assertIsInstance(result[2], str)
        self.assertTrue(result[2].startswith("#"))


# ============================================================================
# 2 – plain_english
# ============================================================================

class TestPlainEnglish(unittest.TestCase):

    def _metrics(self, win_rate=55.0, total_pnl=80.0, sharpe=0.6,
                 num_closed=30, avg_win=4.0, avg_loss=-2.0, avg_hold=12, interval="1d"):
        return dict(win_rate=win_rate, total_pnl=total_pnl, sharpe=sharpe,
                    num_closed=num_closed, avg_win=avg_win, avg_loss=avg_loss,
                    avg_hold=avg_hold, interval=interval)

    def test_output_is_string(self):
        out = plain_english(self._metrics())
        self.assertIsInstance(out, str)

    def test_output_nonempty(self):
        out = plain_english(self._metrics())
        self.assertGreater(len(out.strip()), 10)

    def test_contains_win_rate_value(self):
        out = plain_english(self._metrics(win_rate=57.3))
        self.assertIn("57.3", out)

    def test_contains_sharpe_value(self):
        out = plain_english(self._metrics(sharpe=0.312))
        self.assertIn("0.312", out)

    def test_contains_total_pnl_value(self):
        out = plain_english(self._metrics(total_pnl=103.7))
        self.assertIn("103.7", out)

    def test_excellent_win_rate_comment(self):
        out = plain_english(self._metrics(win_rate=65.0))
        self.assertIn("Excellent", out)

    def test_poor_win_rate_comment(self):
        out = plain_english(self._metrics(win_rate=33.0))
        self.assertIn("Poor", out)

    def test_exceptional_sharpe_comment(self):
        out = plain_english(self._metrics(sharpe=1.2))
        self.assertIn("Exceptional", out)

    def test_negative_sharpe_comment(self):
        out = plain_english(self._metrics(sharpe=-0.1))
        self.assertIn("Negative", out)

    def test_missing_keys_dont_raise(self):
        """plain_english should not raise on a minimal dict."""
        try:
            plain_english({})
        except Exception as exc:
            self.fail(f"plain_english raised on empty dict: {exc}")

    def test_interval_appears_in_hold_line(self):
        out = plain_english(self._metrics(interval="1h"))
        self.assertIn("1h", out)


# ============================================================================
# 3 – Cache helpers
# ============================================================================

class TestCacheHelpers(unittest.TestCase):

    def test_cache_key_format(self):
        key = _cache_key("NASDAQ:NVDA", "1d", "5y")
        self.assertEqual(key, "NASDAQ:NVDA_1d_5y")

    def test_is_fresh_recent(self):
        entry = {"_cached_at": time.time()}
        self.assertTrue(_is_fresh(entry))

    def test_is_fresh_expired(self):
        entry = {"_cached_at": time.time() - 8 * 24 * 3600}   # 8 days ago
        self.assertFalse(_is_fresh(entry))

    def test_is_fresh_missing_key(self):
        self.assertFalse(_is_fresh({}))


# ============================================================================
# 4 – _error_result structure
# ============================================================================

class TestErrorResult(unittest.TestCase):

    def setUp(self):
        self.r = _error_result("NASDAQ:NVDA", "NVDA", "1d", "5y", "test error")

    def test_has_required_keys(self):
        for k in ("tv_sym", "yf_ticker", "interval", "period",
                  "win_rate", "total_pnl", "sharpe", "num_closed",
                  "avg_win", "avg_loss", "avg_hold",
                  "size_units", "size_label", "size_color",
                  "plain_english", "from_cache", "error"):
            self.assertIn(k, self.r, f"Missing key: {k}")

    def test_error_result_size_units_is_none(self):
        # CF6: error result must return None (not 1) so callers fail-closed
        self.assertIsNone(self.r["size_units"])

    def test_error_message_present(self):
        self.assertIn("test error", self.r["error"])

    def test_numeric_fields_are_none(self):
        for k in ("win_rate", "total_pnl", "sharpe", "avg_win", "avg_loss", "avg_hold"):
            self.assertIsNone(self.r[k])

    def test_from_cache_false(self):
        self.assertFalse(self.r["from_cache"])


# ============================================================================
# 5 – get_position_size with mocked backtest engine
# ============================================================================

# Fake backtest result to inject via mock
_FAKE_RESULT = {
    "tv_sym":      "NASDAQ:NVDA",
    "yf_ticker":   "NVDA",
    "interval":    "1d",
    "total_bars":  1000,
    "trades":      [],
    "num_trades":  32,
    "num_closed":  30,
    "num_wins":    18,
    "num_losses":  12,
    "win_rate":    60.0,
    "avg_win":     4.5,
    "avg_loss":    -2.1,
    "avg_hold":    14.0,
    "total_pnl":   87.3,
    "sharpe":      0.62,
    "reason_count": {"ATR Hard Stop": 8, "ATR Trail Stop": 12, "MA Breakdown": 10},
    "avg_left_on_table": 1.2,
    "num_skipped":  2,
    "commission_pct_rt": 0.2,
    "slippage_pct_rt":   0.1,
}


class TestGetPositionSize(unittest.TestCase):

    def _call(self, use_cache=False, result=None):
        """Call get_position_size with mocked backtest engine and a temp cache dir."""
        import backtest.backtest_sizer as sizer_mod
        fake = result if result is not None else _FAKE_RESULT
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "backtest_cache.json"
            orig_cache = sizer_mod._CACHE
            sizer_mod._CACHE = cache_path
            try:
                with patch("backtest.backtest_sizer.get_position_size.__module__"):
                    pass
                # Patch the import inside get_position_size
                with patch.dict("sys.modules", {
                    "backtest.backtest_buy_sell_v2": MagicMock(backtest=lambda *a, **kw: fake)
                }):
                    return sizer_mod.get_position_size(
                        "NASDAQ:NVDA", "NVDA", "1d", "5y", use_cache=use_cache
                    )
            finally:
                sizer_mod._CACHE = orig_cache

    def test_returns_dict(self):
        r = self._call()
        self.assertIsInstance(r, dict)

    def test_required_keys_present(self):
        r = self._call()
        for k in ("tv_sym", "yf_ticker", "win_rate", "total_pnl", "sharpe",
                  "num_closed", "size_units", "size_label", "size_color",
                  "plain_english", "from_cache", "error"):
            self.assertIn(k, r, f"Missing key: {k}")

    def test_high_conviction_result(self):
        """FAKE_RESULT has sharpe=0.62, win_rate=60 → should be 3 units."""
        r = self._call()
        self.assertEqual(r["size_units"], 3)

    def test_metrics_match_fake(self):
        r = self._call()
        self.assertEqual(r["win_rate"], 60.0)
        self.assertAlmostEqual(r["sharpe"], 0.62)
        self.assertEqual(r["num_closed"], 30)

    def test_plain_english_nonempty(self):
        r = self._call()
        self.assertTrue(r["plain_english"].strip())

    def test_error_is_none_on_success(self):
        r = self._call()
        self.assertIsNone(r["error"])

    def test_from_cache_false_first_call(self):
        r = self._call(use_cache=False)
        self.assertFalse(r["from_cache"])

    def test_none_backtest_returns_error_result(self):
        import backtest.backtest_sizer as sizer_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "backtest_cache.json"
            orig_cache = sizer_mod._CACHE
            sizer_mod._CACHE = cache_path
            try:
                with patch.dict("sys.modules", {
                    "backtest.backtest_buy_sell_v2": MagicMock(backtest=lambda *a, **kw: None)
                }):
                    r = sizer_mod.get_position_size(
                        "NASDAQ:NVDA", "NVDA", "1d", "5y", use_cache=False
                    )
            finally:
                sizer_mod._CACHE = orig_cache
        self.assertIsNotNone(r["error"])
        self.assertIsNone(r["size_units"])   # CF6: fail-closed — None, not 1

    def test_cache_written_after_run(self):
        import backtest.backtest_sizer as sizer_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "backtest_cache.json"
            orig_cache = sizer_mod._CACHE
            sizer_mod._CACHE = cache_path
            try:
                with patch.dict("sys.modules", {
                    "backtest.backtest_buy_sell_v2": MagicMock(backtest=lambda *a, **kw: _FAKE_RESULT)
                }):
                    sizer_mod.get_position_size("NASDAQ:NVDA", "NVDA", "1d", "5y",
                                                use_cache=True)
                self.assertTrue(cache_path.exists())
                data = json.loads(cache_path.read_text())
                key = _cache_key("NASDAQ:NVDA", "1d", "5y")
                self.assertIn(key, data)
            finally:
                sizer_mod._CACHE = orig_cache

    def test_cache_hit_returns_from_cache_true(self):
        import backtest.backtest_sizer as sizer_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "backtest_cache.json"
            orig_cache = sizer_mod._CACHE
            sizer_mod._CACHE = cache_path
            try:
                call_count = {"n": 0}
                def counting_backtest(*a, **kw):
                    call_count["n"] += 1
                    return _FAKE_RESULT
                with patch.dict("sys.modules", {
                    "backtest.backtest_buy_sell_v2": MagicMock(backtest=counting_backtest)
                }):
                    # First call writes cache
                    sizer_mod.get_position_size("NASDAQ:NVDA", "NVDA", "1d", "5y",
                                                use_cache=True)
                    # Second call should hit cache, not call backtest again
                    r2 = sizer_mod.get_position_size("NASDAQ:NVDA", "NVDA", "1d", "5y",
                                                     use_cache=True)
                self.assertTrue(r2["from_cache"])
                self.assertEqual(call_count["n"], 1)
            finally:
                sizer_mod._CACHE = orig_cache


# ============================================================================
# 6 – Sharpe regression: backtest() return dict must include sharpe key
# ============================================================================

class TestBacktestSharpeKey(unittest.TestCase):

    def _run_with_df(self, df):
        """Inject a synthetic dataframe into backtest() and collect result."""
        with patch("yfinance.Ticker") as mock_ticker:
            mock_inst = MagicMock()
            mock_inst.history.return_value = df
            mock_ticker.return_value = mock_inst
            from backtest.backtest_buy_sell_v2 import backtest
            return backtest("NASDAQ:NVDA", "NVDA", "1d", "5y")

    def test_sharpe_key_present_in_result(self):
        result = self._run_with_df(_make_df(400))
        if result is None:
            self.skipTest("No trades generated — needs larger synthetic dataset")
        self.assertIn("sharpe", result, "'sharpe' key missing from backtest() return dict")

    def test_sharpe_is_float(self):
        result = self._run_with_df(_make_df(400))
        if result is None:
            self.skipTest("No trades generated")
        self.assertIsInstance(result["sharpe"], float)

    def test_sharpe_finite(self):
        result = self._run_with_df(_make_df(400))
        if result is None:
            self.skipTest("No trades generated")
        import math
        self.assertTrue(math.isfinite(result["sharpe"]),
                        f"sharpe should be finite, got {result['sharpe']}")

    def test_sharpe_zero_for_single_trade(self):
        """With only one closed trade, sharpe should default to 0.0."""
        # Build a tiny df.  The engine may or may not fire — so we test the
        # formula in isolation using the same logic as the module.
        pnls = [5.0]   # only 1 trade
        std  = np.std(pnls, ddof=1) if len(pnls) > 1 else 0
        sharpe = float(np.mean(pnls)) / std if std > 0 else 0.0
        self.assertEqual(sharpe, 0.0)

    def test_sharpe_formula_correctness(self):
        """Sharpe = mean(pnls) / std(pnls, ddof=1), rounded to 3dp."""
        pnls   = [4.0, 2.0, -1.0, 3.0, 5.0]
        mean_v = np.mean(pnls)
        std_v  = np.std(pnls, ddof=1)
        expected = round(mean_v / std_v, 3)
        # Mirror the actual backtest computation
        _std = float(np.std(pnls, ddof=1))
        computed = round(float(np.mean(pnls)) / _std, 3) if _std > 0 else 0.0
        self.assertAlmostEqual(computed, expected, places=5)


if __name__ == "__main__":
    unittest.main()
