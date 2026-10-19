"""
Unit tests for india_order_executor.py.
No live Zerodha connection required.
Run: pytest test_india_order_executor.py -v
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))

from india_order_executor import _calc_qty, _calc_gtt_levels, _resolve_exchange_and_symbol


class TestCalcQty(unittest.TestCase):
    """_calc_qty() is pure arithmetic — no mocks needed."""

    def test_basic_calculation(self):
        # ₹10,000 at ₹500/share = 20 shares
        self.assertEqual(_calc_qty(10_000, 500.0), 20)

    def test_fractional_rounds_down(self):
        # ₹10,000 at ₹333.33/share → 30 (floor of 30.0003)
        self.assertEqual(_calc_qty(10_000, 333.33), 30)

    def test_minimum_one_share(self):
        # Even if the amount is less than one share price, buy at least 1
        self.assertEqual(_calc_qty(100, 500.0), 1)

    def test_zero_ltp_returns_zero(self):
        self.assertEqual(_calc_qty(10_000, 0.0), 0)

    def test_negative_ltp_returns_zero(self):
        self.assertEqual(_calc_qty(10_000, -1.0), 0)

    def test_large_order(self):
        # ₹5,00,000 at ₹2500 = 200 shares
        self.assertEqual(_calc_qty(500_000, 2500.0), 200)


class TestCalcGttLevels(unittest.TestCase):
    """_calc_gtt_levels() is pure math."""

    def test_stop_below_ltp(self):
        levels = _calc_gtt_levels(avg_price=1000.0, ltp=1000.0)
        self.assertLess(levels["stop_price"], 1000.0)

    def test_target_above_ltp(self):
        levels = _calc_gtt_levels(avg_price=1000.0, ltp=1000.0)
        self.assertGreater(levels["target_price"], 1000.0)

    def test_atr_stop_dominates_percentage(self):
        # 2*ATR=100 > 8% of 500 = 40 → stop = ltp - 100
        levels = _calc_gtt_levels(avg_price=500.0, ltp=500.0, atr=50.0)
        self.assertAlmostEqual(levels["stop_price"], 400.0, places=0)

    def test_pct_floor_applies_without_atr(self):
        # 8% of 1000 = 80 → stop = 1000 - 80 = 920
        levels = _calc_gtt_levels(avg_price=1000.0, ltp=1000.0)
        self.assertAlmostEqual(levels["stop_price"], 920.0, places=0)

    def test_stop_limit_below_stop(self):
        levels = _calc_gtt_levels(avg_price=1000.0, ltp=1000.0, atr=20.0)
        self.assertLess(levels["stop_limit"], levels["stop_price"])

    def test_target_limit_above_target(self):
        levels = _calc_gtt_levels(avg_price=1000.0, ltp=1000.0, atr=20.0)
        self.assertGreater(levels["target_limit"], levels["target_price"])

    def test_rr_greater_than_one(self):
        """Default risk-reward should be at least 1:1 (3*ATR vs 2*ATR)."""
        levels = _calc_gtt_levels(avg_price=1000.0, ltp=1000.0, atr=50.0)
        self.assertGreater(levels["rr_ratio"], 1.0)

    def test_stop_capped_at_five_pct_below_ltp(self):
        """stop_price must be ≤ 0.95 * ltp (5% safety cap)."""
        # Even if ATR is tiny, stop should not be > 0.95 * ltp
        levels = _calc_gtt_levels(avg_price=1000.0, ltp=1000.0, atr=1.0)
        self.assertLessEqual(levels["stop_price"], 1000.0 * 0.95)


class TestResolveExchangeAndSymbol(unittest.TestCase):
    """_resolve_exchange_and_symbol() maps TV pro_symbol → (exchange, symbol)."""

    def test_nse_symbol(self):
        exch, sym = _resolve_exchange_and_symbol("NSE:INFY")
        self.assertEqual(exch, "NSE")
        self.assertEqual(sym, "INFY")

    def test_bse_symbol(self):
        exch, sym = _resolve_exchange_and_symbol("BSE:500209")
        self.assertEqual(exch, "BSE")
        self.assertEqual(sym, "500209")

    def test_no_colon_defaults_to_nse(self):
        exch, sym = _resolve_exchange_and_symbol("RELIANCE")
        self.assertEqual(exch, "NSE")
        self.assertEqual(sym, "RELIANCE")

    def test_case_insensitive_exchange(self):
        exch, sym = _resolve_exchange_and_symbol("nse:tcs")
        self.assertEqual(exch, "NSE")
        self.assertEqual(sym, "TCS")


class TestPlaceIndiaOrderDryRun(unittest.TestCase):
    """
    Test place_india_order with mocked kite to avoid live trading.
    """

    def setUp(self):
        # Reset the module-level executor singleton between tests
        import india_order_executor as ioe
        ioe._executor = None

    @patch("india_order_executor._get_kite")
    def test_dry_run_no_order_placed(self, mock_get_kite):
        """With dry_run=True, no actual order should be placed."""
        kite = MagicMock()
        kite.ltp.return_value = {"NSE:INFY": {"last_price": 1800.0}}
        kite.margins.return_value = {"available": {"cash": 100_000.0}}
        mock_get_kite.return_value = kite

        from india_order_executor import place_india_order
        result = place_india_order("NSE:INFY", 1800.0, 10_000, dry_run=True)

        # Dry run must not call place_order or place_gtt
        kite.place_order.assert_not_called()
        kite.place_gtt.assert_not_called()
        # Should return a structured result dict
        self.assertIn("success", result)

    @patch("india_order_executor._get_kite")
    def test_zero_amount_returns_error(self, mock_get_kite):
        """Zero or negative amount should return an error without placing an order."""
        kite = MagicMock()
        mock_get_kite.return_value = kite

        from india_order_executor import place_india_order
        result = place_india_order("NSE:INFY", 1800.0, 0, dry_run=True)
        self.assertFalse(result.get("success", True))

    @patch("india_order_executor._get_kite")
    def test_invalid_symbol_returns_error(self, mock_get_kite):
        """Non-India symbol should return an error."""
        kite = MagicMock()
        mock_get_kite.return_value = kite

        from india_order_executor import place_india_order
        result = place_india_order("NYSE:AAPL", 180.0, 10_000, dry_run=True)
        self.assertFalse(result.get("success", True))


if __name__ == "__main__":
    unittest.main()
