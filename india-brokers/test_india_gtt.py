"""
Unit tests for india_gtt_manager.py pure functions.
No live Zerodha connection required.
Run: pytest test_india_gtt.py -v
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

from india_gtt_manager import _suggest_levels, _gtts_by_symbol, list_gtts, delete_gtt


class TestSuggestLevels(unittest.TestCase):
    """_suggest_levels() is pure math — no mocks needed."""

    def test_atr_stop_dominates_pct(self):
        """When ATR is large, ATR-derived stop should dominate."""
        levels = _suggest_levels(avg_price=1000.0, ltp=950.0, atr=100.0)
        # 2*ATR = 200 >> 8% = 80 → stop = 950 - 200 = 750
        self.assertEqual(levels["stop_price"], 750.0)

    def test_pct_stop_dominates_small_atr(self):
        """When ATR is tiny, percentage floor should dominate."""
        levels = _suggest_levels(avg_price=1000.0, ltp=1000.0, atr=1.0)
        # 8% of 1000 = 80 >> 2*ATR = 2 → stop = 1000 - 80 = 920
        self.assertAlmostEqual(levels["stop_price"], 920.0, places=0)

    def test_no_atr_uses_pct(self):
        """When atr=None, falls back to percentage stops."""
        levels = _suggest_levels(avg_price=500.0, ltp=500.0, atr=None)
        # stop = 500 - 8% of 500 = 500 - 40 = 460
        self.assertAlmostEqual(levels["stop_price"], 460.0, places=0)
        # target = 500 + 12% of 500 = 500 + 60 = 560
        self.assertAlmostEqual(levels["target_price"], 560.0, places=0)

    def test_stop_below_ltp(self):
        """Stop must always be below current LTP."""
        for ltp in [100.0, 500.0, 2000.0]:
            levels = _suggest_levels(avg_price=ltp, ltp=ltp, atr=None)
            self.assertLess(levels["stop_price"], ltp)

    def test_target_above_ltp(self):
        """Target must always be above current LTP."""
        for ltp in [100.0, 500.0, 2000.0]:
            levels = _suggest_levels(avg_price=ltp, ltp=ltp, atr=None)
            self.assertGreater(levels["target_price"], ltp)

    def test_stop_limit_below_stop(self):
        """Stop limit (slippage buffer) must be below stop trigger."""
        levels = _suggest_levels(avg_price=1000.0, ltp=1000.0, atr=20.0)
        self.assertLess(levels["stop_limit"], levels["stop_price"])

    def test_target_limit_above_target(self):
        """Target limit must be above target trigger."""
        levels = _suggest_levels(avg_price=1000.0, ltp=1000.0, atr=20.0)
        self.assertGreater(levels["target_limit"], levels["target_price"])

    def test_rr_ratio_positive(self):
        """Risk-reward ratio must be positive (target further than stop)."""
        levels = _suggest_levels(avg_price=1000.0, ltp=1000.0, atr=20.0)
        self.assertGreater(levels["rr_ratio"], 0)

    def test_deeply_losing_position_stop_still_below_ltp(self):
        """Even for -40% loser, stop must be below LTP."""
        # avg=1000, ltp=600 (−40%)
        levels = _suggest_levels(avg_price=1000.0, ltp=600.0, atr=30.0)
        self.assertLess(levels["stop_price"], 600.0)
        self.assertGreater(levels["target_price"], 600.0)

    def test_atr_used_captured_in_result(self):
        atr = 45.5
        levels = _suggest_levels(avg_price=1000.0, ltp=1000.0, atr=atr)
        self.assertEqual(levels["atr_used"], 45.5)

    def test_atr_none_returns_none_in_result(self):
        levels = _suggest_levels(avg_price=1000.0, ltp=1000.0, atr=None)
        self.assertIsNone(levels["atr_used"])


class TestGttsBySymbol(unittest.TestCase):
    """_gtts_by_symbol() groups GTT list by trading symbol."""

    def test_empty_list(self):
        self.assertEqual(_gtts_by_symbol([]), {})

    def test_single_gtt(self):
        gtts = [{"condition": {"tradingsymbol": "INFY"}, "id": 1}]
        result = _gtts_by_symbol(gtts)
        self.assertIn("INFY", result)
        self.assertEqual(len(result["INFY"]), 1)

    def test_multiple_gtts_same_symbol(self):
        gtts = [
            {"condition": {"tradingsymbol": "TCS"}, "id": 1},
            {"condition": {"tradingsymbol": "TCS"}, "id": 2},
        ]
        result = _gtts_by_symbol(gtts)
        self.assertEqual(len(result["TCS"]), 2)

    def test_multiple_symbols(self):
        gtts = [
            {"condition": {"tradingsymbol": "INFY"}, "id": 1},
            {"condition": {"tradingsymbol": "TCS"},  "id": 2},
        ]
        result = _gtts_by_symbol(gtts)
        self.assertIn("INFY", result)
        self.assertIn("TCS", result)


class TestListGttsAndDeleteGtt(unittest.TestCase):
    """Tests that list_gtts and delete_gtt delegate correctly to kite."""

    def test_list_gtts_returns_kite_result(self):
        kite = MagicMock()
        kite.get_gtts.return_value = [{"id": 42}]
        result = list_gtts(kite)
        self.assertEqual(result[0]["id"], 42)
        kite.get_gtts.assert_called_once()

    def test_list_gtts_empty_returns_empty_list(self):
        kite = MagicMock()
        kite.get_gtts.return_value = None
        result = list_gtts(kite)
        self.assertEqual(result, [])

    def test_delete_gtt_calls_kite(self):
        kite = MagicMock()
        kite.delete_gtt.return_value = {"id": 101}
        result = delete_gtt(kite, 101)
        kite.delete_gtt.assert_called_once_with(101)
        self.assertEqual(result.get("status"), "ok")

    def test_delete_gtt_error_returns_error_dict(self):
        kite = MagicMock()
        kite.delete_gtt.side_effect = Exception("GTT not found")
        result = delete_gtt(kite, 999)
        self.assertEqual(result.get("status"), "error")
        self.assertIn("msg", result)


if __name__ == "__main__":
    unittest.main()
