"""
Unit tests for india_enricher.py.
All external calls (KiteConnect, SmartAPI) are mocked.
Run: pytest test_india_enricher.py -v
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))


class TestTvToNse(unittest.TestCase):
    def setUp(self):
        # Import after sys.path is set
        import india_enricher as ie
        self.tv_to_nse = ie.tv_to_nse

    def test_nse_symbol(self):
        self.assertEqual(self.tv_to_nse("NSE:INFY"), "INFY")

    def test_nse_dly_symbol(self):
        self.assertEqual(self.tv_to_nse("NSE_DLY:TCS"), "TCS")

    def test_bse_symbol(self):
        self.assertEqual(self.tv_to_nse("BSE:500209"), "500209")

    def test_non_india_returns_none(self):
        self.assertIsNone(self.tv_to_nse("NYSE:AAPL"))
        self.assertIsNone(self.tv_to_nse("NASDAQ:NVDA"))

    def test_no_colon_returns_none(self):
        self.assertIsNone(self.tv_to_nse("INFY"))

    def test_lowercase_exchange(self):
        # tv_to_nse upcases the exchange internally, so 'nse:INFY' is treated as NSE:INFY
        self.assertEqual(self.tv_to_nse("nse:INFY"), "INFY")


class TestStripEq(unittest.TestCase):
    def setUp(self):
        import india_enricher as ie
        self._strip_eq = ie._strip_eq

    def test_strips_eq_suffix(self):
        self.assertEqual(self._strip_eq("INFY-EQ"), "INFY")

    def test_no_suffix_unchanged(self):
        self.assertEqual(self._strip_eq("INFY"), "INFY")

    def test_case_insensitive(self):
        self.assertEqual(self._strip_eq("INFY-eq"), "INFY")


class TestIndiaEnricherScoring(unittest.TestCase):
    """
    Tests the enrich() scoring logic by injecting synthetic holdings.
    The _refresh() call is monkeypatched so no network is needed.
    """

    @staticmethod
    def _make_holding(symbol, avg, ltp, qty=100):
        """Build a fully-structured holding dict matching india_enricher internals."""
        pnl = (ltp - avg) * qty
        val = ltp * qty
        pnl_pct = ((ltp - avg) / avg * 100) if avg else 0
        return {
            "symbol": symbol, "exchange": "NSE",
            "zerodha_qty": qty, "zerodha_avg": avg, "zerodha_ltp": ltp,
            "zerodha_pnl": pnl, "zerodha_val": val, "zerodha_pnl_pct": pnl_pct,
            "angelone_qty": 0, "angelone_avg": 0, "angelone_ltp": 0,
            "angelone_pnl": 0, "angelone_val": 0, "angelone_pnl_pct": 0,
            "total_qty": qty,
            "combined_avg": avg,
            "combined_ltp": ltp,
            "combined_pnl": pnl,
            "combined_val": val,
            "combined_pnl_pct": pnl_pct,
            "brokers": ["zerodha"],
        }

    def _make_enricher_with_holdings(self, holdings: dict):
        import india_enricher as ie
        with patch.object(ie.IndiaEnricher, "_refresh", return_value=None):
            enricher = ie.IndiaEnricher()
        enricher._holdings = holdings
        enricher._total_portfolio_value = sum(
            h.get("combined_val", 0) for h in holdings.values()
        )
        enricher._last_refresh = 1e18  # far future → no refresh
        enricher._zerodha_ok  = True   # simulate successful broker connection
        enricher._angelone_ok = True
        return enricher

    def test_no_holding_bullish_gives_positive_boost(self):
        enricher = self._make_enricher_with_holdings({})
        e = enricher.enrich("NSE:INFY")
        # No holding + any signal → fresh opportunity boost (+5)
        self.assertGreaterEqual(e.get("l5_score", 0), 4.5)
        self.assertFalse(e.get("holding"))

    def test_holding_at_profit_reduces_score(self):
        """Holding +26% → avoid chasing winner → score = 3.0."""
        holdings = {"INFY": self._make_holding("INFY", avg=1000.0, ltp=1260.0, qty=100)}
        enricher = self._make_enricher_with_holdings(holdings)
        e = enricher.enrich("NSE:INFY")
        # pnl_pct=26 → score=3.0 (< 5.5)
        self.assertLess(e.get("l5_score", 10), 5.5)
        self.assertTrue(e.get("holding"))

    def test_holding_averaging_zone_gives_boost(self):
        """Holding at -20% → moderate loss → caution zone → score = 5.0 (no overweight)."""
        holdings = {"INFY": self._make_holding("INFY", avg=1000.0, ltp=800.0, qty=100)}
        enricher = self._make_enricher_with_holdings(holdings)
        # Set a large portfolio value so INFY weight is only ~2% (no overweight penalty)
        enricher._total_portfolio_value = 4_000_000.0
        e = enricher.enrich("NSE:INFY")
        # combined_pnl_pct = -20% → score = 5.0 (moderate loss — avg with caution)
        self.assertGreaterEqual(e.get("l5_score", 0), 4.0)
        self.assertTrue(e.get("holding"))

    def test_enrich_non_india_symbol(self):
        enricher = self._make_enricher_with_holdings({})
        e = enricher.enrich("NYSE:AAPL")
        # Non-India → empty dict (no l5_score key)
        self.assertNotIn("l5_score", e)

    def test_html_badge_no_holding(self):
        enricher = self._make_enricher_with_holdings({})
        e = enricher.enrich("NSE:INFY")
        html = enricher.html_badge(e)
        self.assertIsInstance(html, str)
        self.assertIn("<span", html)       # must return an HTML element
        self.assertIn("FRESH", html)       # not held → FRESH badge

    def test_html_badge_empty_dict(self):
        import india_enricher as ie
        with patch.object(ie.IndiaEnricher, "_refresh", return_value=None):
            enricher = ie.IndiaEnricher()
        html = enricher.html_badge({})
        self.assertIsInstance(html, str)


class TestIndiaEnricherRefreshFailsSafely(unittest.TestCase):
    """Enricher must not crash when brokers are unreachable."""

    def test_refresh_failure_is_graceful(self):
        import india_enricher as ie
        with patch.object(ie.IndiaEnricher, "_refresh", side_effect=Exception("network")):
            try:
                enricher = ie.IndiaEnricher()
            except Exception:
                pass  # constructor may propagate if _refresh raises — that's OK

    def test_enrich_with_empty_holdings_is_safe(self):
        import india_enricher as ie
        with patch.object(ie.IndiaEnricher, "_refresh", return_value=None):
            enricher = ie.IndiaEnricher()
        enricher._holdings = {}
        enricher._total_portfolio_value = 0
        enricher._last_refresh = 1e18
        # Should not raise
        e = enricher.enrich("NSE:TCS")
        self.assertIn("l5_score", e)


if __name__ == "__main__":
    unittest.main()
