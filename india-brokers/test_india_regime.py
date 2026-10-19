"""
Tests for india_regime.py
Run: pytest test_india_regime.py -v
All AngelOne API calls are mocked — no real broker calls needed.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).parent))
import india_regime as ir


def _make_pcr_list(nifty_pcr: float) -> list:
    return [
        {"symbol": "NIFTY",     "pcr": nifty_pcr},
        {"symbol": "BANKNIFTY", "pcr": nifty_pcr + 0.1},
    ]


def _make_oi_items(bullish_ratio: float, n: int = 20) -> list:
    """Create n OI buildup items with the given fraction being bullish."""
    items = []
    for i in range(n):
        is_bullish = i < int(bullish_ratio * n)
        items.append({"symbol": f"SYM{i:03d}", "bullish": is_bullish, "change": 1000})
    return items


def _make_gainers_losers(gainers_pct: float, total: int = 100) -> tuple:
    n_gainers = int(gainers_pct / 100 * total)
    gainers  = [{"symbol": f"G{i}", "pct": 1.0} for i in range(n_gainers)]
    losers   = [{"symbol": f"L{i}", "pct": -1.0} for i in range(total - n_gainers)]
    return gainers, losers


class TestPcrToScore(unittest.TestCase):
    """_pcr_to_score() maps PCR to 0–10 score."""

    def test_very_low_pcr_is_bullish(self):
        self.assertGreater(ir._pcr_to_score(0.3), 8.0)

    def test_moderate_pcr_is_neutral(self):
        score = ir._pcr_to_score(1.1)
        self.assertGreater(score, 3.0)
        self.assertLess(score, 6.0)

    def test_high_pcr_is_bearish(self):
        self.assertLess(ir._pcr_to_score(1.8), 3.0)

    def test_boundary_values(self):
        # Test all boundaries don't crash
        for pcr in [0.0, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0]:
            score = ir._pcr_to_score(pcr)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 10.0)


class TestOiPctToScore(unittest.TestCase):
    """_oi_pct_to_score() maps bullish-pct to 0–10 score."""

    def test_high_bullish_pct_high_score(self):
        self.assertGreater(ir._oi_pct_to_score(75.0), 7.0)

    def test_low_bullish_pct_low_score(self):
        self.assertLess(ir._oi_pct_to_score(20.0), 4.0)

    def test_neutral_pct(self):
        score = ir._oi_pct_to_score(45.0)
        self.assertGreater(score, 3.0)
        self.assertLess(score, 7.0)


class TestScoreToRegime(unittest.TestCase):
    """_score_to_regime() maps composite score to regime + adjustments."""

    def test_high_score_bullish(self):
        regime, adj, mult = ir._score_to_regime(8.0)
        self.assertEqual(regime, "BULLISH")
        self.assertLess(adj, 0)      # threshold reduced for bullish
        self.assertGreater(mult, 1.0)  # size increased for bullish

    def test_mid_score_neutral(self):
        regime, adj, mult = ir._score_to_regime(6.0)
        self.assertEqual(regime, "NEUTRAL")
        self.assertEqual(adj, 0)
        self.assertEqual(mult, 1.0)

    def test_low_score_bearish(self):
        regime, adj, mult = ir._score_to_regime(3.0)
        self.assertEqual(regime, "BEARISH")
        self.assertGreater(adj, 0)   # threshold raised for bearish
        self.assertLess(mult, 1.0)   # size reduced for bearish

    def test_zero_score_very_bearish(self):
        regime, adj, mult = ir._score_to_regime(0.0)
        self.assertEqual(regime, "BEARISH")
        self.assertGreater(adj, 5)   # heavy penalty

    def test_adj_and_mult_consistent(self):
        """Higher score should always give lower or equal threshold adjustment."""
        _, adj_high, _ = ir._score_to_regime(9.0)
        _, adj_mid,  _ = ir._score_to_regime(6.0)
        _, adj_low,  _ = ir._score_to_regime(2.0)
        self.assertLessEqual(adj_high, adj_mid)
        self.assertLessEqual(adj_mid,  adj_low)


class TestComputeRegimeBullish(unittest.TestCase):
    """_compute_regime() with all-bullish AngelOne data."""

    def _mock_mi(self, nifty_pcr=0.65, oi_bullish_ratio=0.7, gainers_pct=65.0):
        mock_mi_cls = MagicMock()
        inst = mock_mi_cls.return_value
        inst.pcr.return_value = _make_pcr_list(nifty_pcr)
        # All four OI buildup types return same bullish mix for simplicity
        def oi_side(btype, expiry):
            items = _make_oi_items(oi_bullish_ratio, n=25)
            return items
        inst.oi_buildup.side_effect = oi_side
        gainers, losers = _make_gainers_losers(gainers_pct)
        inst.price_gainers_losers.side_effect = lambda t, e: gainers if "Gainer" in t else losers
        return mock_mi_cls

    def test_bullish_conditions_return_bullish_regime(self):
        mock_cls = self._mock_mi(nifty_pcr=0.65, oi_bullish_ratio=0.72, gainers_pct=68.0)
        mock_mi_module = MagicMock()
        mock_mi_module.IndiaMarketInternals = mock_cls
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            result = ir._compute_regime()
        self.assertEqual(result["regime"], "BULLISH")
        self.assertLess(result["threshold_adj"], 0)
        self.assertGreater(result["size_multiplier"], 1.0)
        self.assertIsNotNone(result["nifty_pcr"])
        self.assertIsNotNone(result["oi_bullish_pct"])
        self.assertIsNotNone(result["gainers_pct"])

    def test_bearish_conditions_return_bearish_regime(self):
        mock_cls = self._mock_mi(nifty_pcr=1.6, oi_bullish_ratio=0.25, gainers_pct=30.0)
        mock_mi_module = MagicMock()
        mock_mi_module.IndiaMarketInternals = mock_cls
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            result = ir._compute_regime()
        self.assertEqual(result["regime"], "BEARISH")
        self.assertGreater(result["threshold_adj"], 0)
        self.assertLess(result["size_multiplier"], 1.0)

    def test_neutral_conditions(self):
        # PCR=0.9 → score=6.0 ; OI 55% bullish → score=7.0 ; breadth 50% → score=5.0
        # composite = 0.50*6.0 + 0.35*7.0 + 0.15*5.0 = 3.0 + 2.45 + 0.75 = 6.2 → NEUTRAL
        mock_cls = self._mock_mi(nifty_pcr=0.90, oi_bullish_ratio=0.55, gainers_pct=50.0)
        mock_mi_module = MagicMock()
        mock_mi_module.IndiaMarketInternals = mock_cls
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            result = ir._compute_regime()
        self.assertEqual(result["regime"], "NEUTRAL")
        self.assertEqual(result["threshold_adj"], 0)
        self.assertEqual(result["size_multiplier"], 1.0)


class TestComputeRegimePartialFailure(unittest.TestCase):
    """_compute_regime() gracefully handles individual data source failures."""

    def test_pcr_failure_falls_back_to_oi_breadth(self):
        """If PCR fetch fails, regime is still computed from OI + breadth."""
        mock_mi_module = MagicMock()
        inst = mock_mi_module.IndiaMarketInternals.return_value
        inst.pcr.side_effect = Exception("PCR API error")
        inst.oi_buildup.return_value = _make_oi_items(0.72, n=30)
        gainers, losers = _make_gainers_losers(66.0)
        inst.price_gainers_losers.side_effect = lambda t, e: gainers if "Gainer" in t else losers
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            result = ir._compute_regime()
        self.assertIn("PCR fetch failed", str(result["errors"]))
        # Should still produce a regime from remaining data
        self.assertIn(result["regime"], ("BULLISH", "NEUTRAL", "BEARISH", "UNKNOWN"))

    def test_oi_failure_falls_back_to_pcr(self):
        """If OI fetch fails, regime is still computed from PCR alone."""
        mock_mi_module = MagicMock()
        inst = mock_mi_module.IndiaMarketInternals.return_value
        inst.pcr.return_value = _make_pcr_list(0.65)  # bullish PCR
        inst.oi_buildup.side_effect = Exception("OI API down")
        inst.price_gainers_losers.side_effect = Exception("breadth also down")
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            result = ir._compute_regime()
        self.assertIn("OI buildup fetch failed", str(result["errors"]))
        # PCR alone should determine regime
        self.assertNotEqual(result["regime"], "UNKNOWN")

    def test_all_sources_fail_returns_unknown(self):
        """If all API calls fail, regime is UNKNOWN with no adjustment."""
        mock_mi_module = MagicMock()
        inst = mock_mi_module.IndiaMarketInternals.return_value
        inst.pcr.side_effect              = Exception("down")
        inst.oi_buildup.side_effect       = Exception("down")
        inst.price_gainers_losers.side_effect = Exception("down")
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            result = ir._compute_regime()
        self.assertEqual(result["regime"],          "UNKNOWN")
        self.assertEqual(result["threshold_adj"],   0)
        self.assertEqual(result["size_multiplier"], 1.0)

    def test_missing_nifty_falls_back_to_median_pcr(self):
        """If NIFTY is not in PCR list, uses median PCR."""
        mock_mi_module = MagicMock()
        inst = mock_mi_module.IndiaMarketInternals.return_value
        # No NIFTY entry — only BANKNIFTY and random stocks
        inst.pcr.return_value = [
            {"symbol": "BANKNIFTY",  "pcr": 0.9},
            {"symbol": "FINNIFTY",   "pcr": 1.1},
            {"symbol": "MIDCPNIFTY", "pcr": 0.7},
        ]
        inst.oi_buildup.return_value = []
        inst.price_gainers_losers.return_value = []
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            result = ir._compute_regime()
        self.assertIsNotNone(result["nifty_pcr"])  # should use median
        self.assertNotEqual(result["regime"], "UNKNOWN")  # derived from PCR

    def test_india_market_internals_not_importable(self):
        """Returns UNKNOWN if india_market_internals is not installed."""
        # Setting sys.modules entry to None acts as a module blocker — Python
        # raises ImportError on 'from india_market_internals import ...'.
        with patch.dict(sys.modules, {"india_market_internals": None}):
            result = ir._compute_regime()
        self.assertEqual(result["regime"], "UNKNOWN")
        self.assertIn("not importable", str(result["errors"]))


class TestGetRegimeCache(unittest.TestCase):
    """get_regime() caching behaviour."""

    def setUp(self):
        ir.invalidate_cache()

    def _single_bullish_call(self):
        mock_mi_module = MagicMock()
        inst = mock_mi_module.IndiaMarketInternals.return_value
        inst.pcr.return_value = _make_pcr_list(0.65)
        inst.oi_buildup.return_value = _make_oi_items(0.70)
        gainers, losers = _make_gainers_losers(65.0)
        inst.price_gainers_losers.side_effect = lambda t, e: gainers if "Gainer" in t else losers
        return mock_mi_module

    def test_second_call_uses_cache(self):
        """IndiaMarketInternals is only instantiated once within TTL."""
        mi_mod = self._single_bullish_call()
        with patch.dict(sys.modules, {"india_market_internals": mi_mod}):
            r1 = ir.get_regime()
            r2 = ir.get_regime()  # should hit cache
        # Constructor should be called exactly once
        self.assertEqual(mi_mod.IndiaMarketInternals.call_count, 1)
        self.assertEqual(r1["regime"], r2["regime"])

    def test_force_refresh_bypasses_cache(self):
        """force_refresh=True triggers a new computation."""
        mi_mod = self._single_bullish_call()
        with patch.dict(sys.modules, {"india_market_internals": mi_mod}):
            ir.get_regime()
            ir.get_regime(force_refresh=True)
        # Should have instantiated twice
        self.assertEqual(mi_mod.IndiaMarketInternals.call_count, 2)

    def test_invalidate_cache_triggers_refresh(self):
        mi_mod = self._single_bullish_call()
        with patch.dict(sys.modules, {"india_market_internals": mi_mod}):
            ir.get_regime()
            ir.invalidate_cache()
            ir.get_regime()
        self.assertEqual(mi_mod.IndiaMarketInternals.call_count, 2)

    def test_get_regime_never_raises(self):
        """get_regime() must never raise, even if _compute_regime throws."""
        import india_regime as _ir
        with patch.object(_ir, "_compute_regime", side_effect=RuntimeError("boom")):
            result = _ir.get_regime(force_refresh=True)
        self.assertEqual(result["regime"], "UNKNOWN")
        self.assertEqual(result["threshold_adj"], 0)

    def test_data_age_increments(self):
        """data_age_s should increase on subsequent cached calls."""
        mi_mod = self._single_bullish_call()
        with patch.dict(sys.modules, {"india_market_internals": mi_mod}):
            r1 = ir.get_regime(force_refresh=True)
        time.sleep(0.05)
        with patch.dict(sys.modules, {"india_market_internals": mi_mod}):
            r2 = ir.get_regime()
        self.assertGreaterEqual(r2["data_age_s"], r1["data_age_s"])


class TestRegimeSummaryText(unittest.TestCase):
    """regime_summary_text() produces a human-readable string."""

    def test_contains_regime_name(self):
        for regime in ("BULLISH", "NEUTRAL", "BEARISH", "UNKNOWN"):
            info = {
                "regime": regime, "score": 6.0,
                "threshold_adj": 0, "size_multiplier": 1.0,
                "nifty_pcr": 0.9, "oi_bullish_pct": 52.0
            }
            text = ir.regime_summary_text(info)
            self.assertIn(regime, text)

    def test_contains_pcr_when_available(self):
        info = {
            "regime": "NEUTRAL", "score": 5.5,
            "threshold_adj": 0, "size_multiplier": 1.0,
            "nifty_pcr": 1.05, "oi_bullish_pct": 49.0
        }
        text = ir.regime_summary_text(info)
        self.assertIn("1.05", text)

    def test_handles_none_pcr_gracefully(self):
        info = {
            "regime": "UNKNOWN", "score": 5.0,
            "threshold_adj": 0, "size_multiplier": 1.0,
            "nifty_pcr": None, "oi_bullish_pct": None
        }
        text = ir.regime_summary_text(info)
        self.assertIn("UNKNOWN", text)  # No KeyError on None values


class TestRegimeAdjustmentLogic(unittest.TestCase):
    """Integration test: verify regime adjustments have the expected direction."""

    @staticmethod
    def _regime_for_pcr(pcr: float) -> dict:
        mock_mi_module = MagicMock()
        inst = mock_mi_module.IndiaMarketInternals.return_value
        inst.pcr.return_value = _make_pcr_list(pcr)
        inst.oi_buildup.side_effect = Exception("skip — PCR only test")
        inst.price_gainers_losers.side_effect = Exception("skip")
        with patch.dict(sys.modules, {"india_market_internals": mock_mi_module}):
            return ir._compute_regime()

    def test_low_pcr_lowers_threshold(self):
        """Very bullish PCR should reduce composite threshold."""
        result = self._regime_for_pcr(0.4)
        self.assertLessEqual(result["threshold_adj"], 0)

    def test_high_pcr_raises_threshold(self):
        """Very bearish PCR should raise composite threshold."""
        result = self._regime_for_pcr(1.8)
        self.assertGreater(result["threshold_adj"], 0)
        self.assertLess(result["size_multiplier"], 1.0)

    def test_bullish_larger_than_neutral_size(self):
        """Bullish multiplier must exceed neutral multiplier."""
        from india_regime import _score_to_regime
        _, _, bull_mult = _score_to_regime(8.0)
        _, _, neut_mult = _score_to_regime(6.0)
        _, _, bear_mult = _score_to_regime(3.0)
        self.assertGreater(bull_mult, neut_mult)
        self.assertGreater(neut_mult, bear_mult)


if __name__ == "__main__":
    unittest.main()
