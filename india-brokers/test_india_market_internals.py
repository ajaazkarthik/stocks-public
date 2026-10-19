"""
Unit tests for india_market_internals.py.
All HTTP calls via SmartAPI are mocked.
Run: pytest test_india_market_internals.py -v
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))


class TestMiScore(unittest.TestCase):
    """
    mi_score() is pure math — no network calls needed.
    Build the IndiaMarketInternals instance with SmartConnect mocked.
    """

    def _make_mi(self):
        import india_market_internals as im
        with patch("india_market_internals.SmartConnect") as MockSC:
            inst = MockSC.return_value
            inst.generateSession.return_value = {"status": True, "data": {"jwtToken": "tok"}}
            mi = im.IndiaMarketInternals.__new__(im.IndiaMarketInternals)
            mi._session = inst
            mi._jwt = "tok"
        return mi

    def _mi_score(self, **kwargs):
        import india_market_internals as im
        with patch.object(im.IndiaMarketInternals, "__init__", return_value=None):
            mi = im.IndiaMarketInternals.__new__(im.IndiaMarketInternals)
        return mi.mi_score(kwargs)

    # ── PCR component ────────────────────────────────────────────────────────
    def test_pcr_none_neutral(self):
        score = self._mi_score()
        self.assertAlmostEqual(score, 5.0, places=0)

    def test_pcr_below_05_bullish(self):
        score = self._mi_score(pcr=0.3)
        # PCR=10 * 0.30 + others all neutral 5 → 3 + 1.25 + 1.25 + 1 = 6.5
        self.assertGreater(score, 6.0)

    def test_pcr_above_15_bearish(self):
        score_bear = self._mi_score(pcr=1.8)
        score_bull = self._mi_score(pcr=0.3)
        self.assertLess(score_bear, score_bull)

    # ── 52W position component ───────────────────────────────────────────────
    def test_52w_upper_range_high_score(self):
        score = self._mi_score(**{"52w_position": 0.70})
        # 52w score = 10 at sweet spot 0.50–0.85
        self.assertGreater(score, 5.0)

    def test_52w_below_15_low_score(self):
        score = self._mi_score(**{"52w_position": 0.05})
        self.assertLess(score, 5.0)

    # ── Buy/sell ratio component ─────────────────────────────────────────────
    def test_high_buy_sell_ratio_bullish(self):
        score = self._mi_score(buy_sell_ratio=2.0)
        self.assertGreater(score, 5.0)

    def test_low_buy_sell_ratio_bearish(self):
        score_low = self._mi_score(buy_sell_ratio=0.3)
        score_high = self._mi_score(buy_sell_ratio=2.0)
        self.assertLess(score_low, score_high)

    # ── OI buildup component ─────────────────────────────────────────────────
    def test_oi_bullish_true_boosts_score(self):
        score_bull = self._mi_score(oi_bullish=True)
        score_bear = self._mi_score(oi_bullish=False)
        self.assertGreater(score_bull, score_bear)

    # ── Full bullish scenario ────────────────────────────────────────────────
    def test_all_bullish_gives_high_score(self):
        score = self._mi_score(
            pcr=0.4,
            **{"52w_position": 0.70},
            buy_sell_ratio=2.0,
            oi_bullish=True,
        )
        # PCR=10*0.30=3 + 52w=10*0.25=2.5 + bsr=10*0.25=2.5 + oi=8*0.20=1.6 = 9.6
        self.assertGreater(score, 9.0)

    def test_all_bearish_gives_low_score(self):
        score = self._mi_score(
            pcr=2.0,
            **{"52w_position": 0.05},
            buy_sell_ratio=0.3,
            oi_bullish=False,
        )
        # PCR=1*0.30=0.3 + 52w=2*0.25=0.5 + bsr=1*0.25=0.25 + oi=3*0.20=0.6 = 1.65
        self.assertLess(score, 3.0)

    def test_score_in_range_0_to_10(self):
        for pcr in [0.3, 0.8, 1.5, 2.5]:
            for w52 in [0.1, 0.5, 0.9]:
                score = self._mi_score(pcr=pcr, **{"52w_position": w52})
                self.assertGreaterEqual(score, 0)
                self.assertLessEqual(score, 10)


class TestNseTokenMap(unittest.TestCase):
    """Validate the NSE_TOKEN_MAP is populated and well-formed."""

    def test_map_not_empty(self):
        from india_market_internals import NSE_TOKEN_MAP
        self.assertGreater(len(NSE_TOKEN_MAP), 10)

    def test_all_values_are_strings(self):
        from india_market_internals import NSE_TOKEN_MAP
        for key, val in NSE_TOKEN_MAP.items():
            self.assertIsInstance(val, str, f"{key} token should be a string")

    def test_reliance_present(self):
        from india_market_internals import NSE_TOKEN_MAP
        self.assertIn("RELIANCE", NSE_TOKEN_MAP)

    def test_infy_present(self):
        from india_market_internals import NSE_TOKEN_MAP
        self.assertIn("INFY", NSE_TOKEN_MAP)


class TestPcrFor(unittest.TestCase):
    """
    pcr_for() looks up PCR data from the cached self._pcr_cache.
    We inject synthetic data to test lookup behaviour.
    """

    def _make_mi_with_pcr_cache(self, cache: list):
        import india_market_internals as im
        with patch.object(im.IndiaMarketInternals, "__init__", return_value=None):
            mi = im.IndiaMarketInternals.__new__(im.IndiaMarketInternals)
        mi._pcr_cache = cache
        mi._pcr_cache_ts = 1e18  # never expire
        return mi

    def test_exact_symbol_found(self):
        cache = [{"symbol": "NIFTY", "pcr": 0.75}]
        import india_market_internals as im
        with patch.object(im.IndiaMarketInternals, "__init__", return_value=None):
            mi = im.IndiaMarketInternals.__new__(im.IndiaMarketInternals)
        # Directly call pcr_for using the mocked pcr() method
        with patch.object(mi, "pcr", return_value=cache):
            result = mi.pcr_for("NIFTY")
        self.assertIsNotNone(result)
        self.assertEqual(result["pcr"], 0.75)

    def test_symbol_not_in_cache_returns_none(self):
        cache = [{"symbol": "NIFTY", "pcr": 0.75}]
        import india_market_internals as im
        with patch.object(im.IndiaMarketInternals, "__init__", return_value=None):
            mi = im.IndiaMarketInternals.__new__(im.IndiaMarketInternals)
        with patch.object(mi, "pcr", return_value=cache):
            result = mi.pcr_for("AAPL")
        self.assertIsNone(result)


class TestFiiDiiFlow(unittest.TestCase):
    """Tests for fetch_fii_dii_flow() and fii_dii_modifier()."""

    def _make_mi(self):
        import india_market_internals as im
        with patch.object(im.IndiaMarketInternals, "__init__", return_value=None):
            mi = im.IndiaMarketInternals.__new__(im.IndiaMarketInternals)
        mi.verbose = False
        return mi

    def test_fii_net_positive_bullish(self):
        """FII net buy > 100 → regime 'bullish'."""
        mi = self._make_mi()
        nse_data = [
            {"category": "FII/FPI", "buyValue": 5000.0, "sellValue": 3000.0},
            {"category": "DII", "buyValue": 4000.0, "sellValue": 4500.0},
        ]
        with patch("urllib.request.build_opener") as mock_opener:
            opener_inst = MagicMock()
            mock_opener.return_value = opener_inst
            resp = MagicMock()
            import json as _j
            resp.read.return_value = _j.dumps(nse_data).encode("utf-8")
            opener_inst.open.side_effect = [MagicMock(), resp]  # homepage + api
            result = mi.fetch_fii_dii_flow()
        self.assertIsNotNone(result)
        self.assertEqual(result["regime"], "bullish")
        self.assertAlmostEqual(result["fii_net"], 2000.0)
        self.assertAlmostEqual(result["dii_net"], -500.0)

    def test_fii_net_negative_bearish(self):
        """FII net sell → regime 'bearish'."""
        mi = self._make_mi()
        nse_data = [
            {"category": "FII/FPI", "buyValue": 1000.0, "sellValue": 3000.0},
            {"category": "DII", "buyValue": 5000.0, "sellValue": 4000.0},
        ]
        with patch("urllib.request.build_opener") as mock_opener:
            opener_inst = MagicMock()
            mock_opener.return_value = opener_inst
            resp = MagicMock()
            import json as _j
            resp.read.return_value = _j.dumps(nse_data).encode("utf-8")
            opener_inst.open.side_effect = [MagicMock(), resp]
            result = mi.fetch_fii_dii_flow()
        self.assertEqual(result["regime"], "bearish")
        self.assertAlmostEqual(result["fii_net"], -2000.0)

    def test_fii_dii_neutral_near_zero(self):
        """FII net near zero → regime 'neutral'."""
        mi = self._make_mi()
        nse_data = [
            {"category": "FII/FPI", "buyValue": 3050.0, "sellValue": 3000.0},
            {"category": "DII", "buyValue": 4000.0, "sellValue": 4000.0},
        ]
        with patch("urllib.request.build_opener") as mock_opener:
            opener_inst = MagicMock()
            mock_opener.return_value = opener_inst
            resp = MagicMock()
            import json as _j
            resp.read.return_value = _j.dumps(nse_data).encode("utf-8")
            opener_inst.open.side_effect = [MagicMock(), resp]
            result = mi.fetch_fii_dii_flow()
        self.assertEqual(result["regime"], "neutral")

    def test_fetch_fii_dii_returns_none_on_error(self):
        """Network error → returns None, no crash."""
        mi = self._make_mi()
        with patch("urllib.request.build_opener") as mock_opener:
            opener_inst = MagicMock()
            mock_opener.return_value = opener_inst
            opener_inst.open.side_effect = Exception("connection refused")
            result = mi.fetch_fii_dii_flow()
        self.assertIsNone(result)

    def test_fii_dii_modifier_bullish(self):
        """fii_dii_modifier() returns +0.5 when bullish."""
        mi = self._make_mi()
        with patch.object(mi, "fetch_fii_dii_flow", return_value={
            "fii_net": 2000, "dii_net": -500, "regime": "bullish",
            "fii_buy": 5000, "fii_sell": 3000, "dii_buy": 4000, "dii_sell": 4500,
        }):
            self.assertAlmostEqual(mi.fii_dii_modifier(), 0.5)

    def test_fii_dii_modifier_bearish(self):
        """fii_dii_modifier() returns -0.5 when bearish."""
        mi = self._make_mi()
        with patch.object(mi, "fetch_fii_dii_flow", return_value={
            "fii_net": -2000, "dii_net": 1000, "regime": "bearish",
            "fii_buy": 1000, "fii_sell": 3000, "dii_buy": 5000, "dii_sell": 4000,
        }):
            self.assertAlmostEqual(mi.fii_dii_modifier(), -0.5)

    def test_fii_dii_modifier_unavailable(self):
        """fii_dii_modifier() returns 0.0 when data unavailable."""
        mi = self._make_mi()
        with patch.object(mi, "fetch_fii_dii_flow", return_value=None):
            self.assertAlmostEqual(mi.fii_dii_modifier(), 0.0)


class TestMwpl(unittest.TestCase):
    """Tests for fetch_mwpl() and mwpl_check()."""

    def _make_mi(self):
        import india_market_internals as im
        with patch.object(im.IndiaMarketInternals, "__init__", return_value=None):
            mi = im.IndiaMarketInternals.__new__(im.IndiaMarketInternals)
        mi.verbose = False
        return mi

    def test_mwpl_approaching_ban(self):
        """MWPL ≥ 85% → status 'approaching_ban', penalty -5."""
        mi = self._make_mi()
        mwpl_data = [{"symbol": "INFY", "mwpl_pct": 88.5, "status": "approaching_ban"}]
        with patch.object(mi, "fetch_mwpl", return_value=mwpl_data):
            result = mi.mwpl_check("NSE:INFY")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "approaching_ban")
        self.assertAlmostEqual(result["penalty"], -5.0)

    def test_mwpl_normal_no_penalty(self):
        """MWPL < 70% → status 'normal', penalty 0."""
        mi = self._make_mi()
        mwpl_data = [{"symbol": "RELIANCE", "mwpl_pct": 45.0, "status": "normal"}]
        with patch.object(mi, "fetch_mwpl", return_value=mwpl_data):
            result = mi.mwpl_check("NSE:RELIANCE")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "normal")
        self.assertAlmostEqual(result["penalty"], 0.0)

    def test_mwpl_caution(self):
        """MWPL 70-85% → status 'caution', penalty -2."""
        mi = self._make_mi()
        mwpl_data = [{"symbol": "SBIN", "mwpl_pct": 75.0, "status": "caution"}]
        with patch.object(mi, "fetch_mwpl", return_value=mwpl_data):
            result = mi.mwpl_check("NSE:SBIN")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "caution")
        self.assertAlmostEqual(result["penalty"], -2.0)

    def test_mwpl_symbol_not_found(self):
        """Symbol not in MWPL list → returns None."""
        mi = self._make_mi()
        with patch.object(mi, "fetch_mwpl", return_value=[]):
            result = mi.mwpl_check("NSE:AAPL")
        self.assertIsNone(result)

    def test_fetch_mwpl_returns_empty_on_error(self):
        """Network error → returns empty list, no crash."""
        mi = self._make_mi()
        with patch("urllib.request.build_opener") as mock_opener:
            opener_inst = MagicMock()
            mock_opener.return_value = opener_inst
            opener_inst.open.side_effect = Exception("timeout")
            result = mi.fetch_mwpl()
        self.assertEqual(result, [])

    def test_fetch_mwpl_parses_nse_response(self):
        """Verify MWPL status classification from raw NSE data."""
        mi = self._make_mi()
        nse_data = {"data": [
            {"tradingSymbol": "INFY", "mwpl": 90.0},
            {"tradingSymbol": "TCS", "mwpl": 75.0},
            {"tradingSymbol": "RELIANCE", "mwpl": 40.0},
            {"tradingSymbol": "SBIN", "mwpl": 96.0},
        ]}
        with patch("urllib.request.build_opener") as mock_opener:
            opener_inst = MagicMock()
            mock_opener.return_value = opener_inst
            resp = MagicMock()
            import json as _j
            resp.read.return_value = _j.dumps(nse_data).encode("utf-8")
            opener_inst.open.side_effect = [MagicMock(), resp]
            results = mi.fetch_mwpl()
        self.assertEqual(len(results), 4)
        statuses = {r["symbol"]: r["status"] for r in results}
        self.assertEqual(statuses["INFY"], "approaching_ban")
        self.assertEqual(statuses["TCS"], "caution")
        self.assertEqual(statuses["RELIANCE"], "normal")
        self.assertEqual(statuses["SBIN"], "banned")


if __name__ == "__main__":
    unittest.main()
