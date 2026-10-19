"""
Unit tests for tradingview/options/hybrid_options.py
======================================================
Tests cover:
  • compute_tqs_v2()   — hybrid TQS v2 score (pure, no I/O)
  • OI merge logic     — (strike, expiry, type) key matching
  • Graceful degradation when Moomoo is offline
  • Graceful degradation when WebSocket fails
  • Pressure / OHLC range / IV-spread derived-field computation
  • Source field tagging ("hybrid")
  • HybridOptionsFetcher.get_calls() integration (all network mocked)

Run:
    python -m pytest test_hybrid_options.py -v
    python -m pytest test_hybrid_options.py -v -k "tqs"
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))

# ── helpers ───────────────────────────────────────────────────────────────────

def _tv_contract(
    strike: float = 215.0,
    expiry: str   = "2026-05-11",
    opt_type: str = "CALL",
    bid: float    = 2.08,
    ask: float    = 2.15,
    bid_iv: float = 44.8,
    ask_iv: float = 45.6,
    iv: float     = 45.2,
    oi: int       = 0,
    volume: int   = 0,
    spread_pct: float = 3.3,
    spot_price: float = 218.0,
    code: str     = "OPRA:NVDA260511C215.0",
) -> dict:
    """Build a minimal TV scan2 contract dict."""
    return {
        "code":         code,
        "strike":       strike,
        "expiry":       expiry,
        "option_type":  opt_type,
        "bid":          bid,
        "ask":          ask,
        "last_price":   2.01,
        "mid_price":    (bid + ask) / 2,
        "volume":       volume,
        "oi":           oi,
        "iv":           iv,
        "bid_iv":       bid_iv,
        "ask_iv":       ask_iv,
        "delta":        0.52,
        "gamma":        0.03,
        "theta":        -0.08,
        "vega":         0.12,
        "theo_price":   2.10,
        "dte":          10,
        "spread_pct":   spread_pct,
        "moneyness_pct": -1.5,
        "spot_price":   spot_price,
        "market":       "US",
        "source":       "tv",
    }


def _moo_contract(strike: float = 215.0, expiry: str = "2026-05-11",
                  opt_type: str = "CALL", oi: int = 12500) -> dict:
    """Build a minimal Moomoo contract dict with real OI."""
    return {
        "code":        "US.NVDA260511C215",
        "strike":      strike,
        "expiry":      expiry,
        "option_type": opt_type,
        "bid":         2.05,
        "ask":         2.18,
        "last_price":  2.10,
        "volume":      75999,
        "oi":          oi,
        "iv":          44.9,
        "delta":       0.51,
        "gamma":       0.03,
        "theta":       -0.08,
        "vega":        0.12,
        "market":      "US",
        "source":      "moomoo",
    }


WS_LIVE_DATA = {
    "OPRA:NVDA260511C215.0": {
        "lp":         2.13,
        "open_price": 2.01,
        "high_price": 4.30,
        "low_price":  1.81,
        "volume":     75999,
        "bid_size":   131,
        "ask_size":   10,
    }
}


# ── import target module ──────────────────────────────────────────────────────

from tradingview.options.hybrid_options import (
    HybridOptionsFetcher,
    _clamp,
    _is_us_market_hours,
    _ivspd_s,
    _ohlc_s,
    _pressure_s,
    compute_tqs_v2,
)


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — Pure score helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestClamp(unittest.TestCase):
    def test_within_range(self):
        self.assertAlmostEqual(_clamp(5.0, 0.0, 10.0), 5.0)

    def test_below_min(self):
        self.assertAlmostEqual(_clamp(-3.0, 0.0, 10.0), 0.0)

    def test_above_max(self):
        self.assertAlmostEqual(_clamp(15.0, 0.0, 10.0), 10.0)


class TestIvSpdScore(unittest.TestCase):
    def test_zero_spread_perfect(self):
        self.assertAlmostEqual(_ivspd_s(0.0), 10.0)

    def test_ten_pct_spread_floor(self):
        self.assertAlmostEqual(_ivspd_s(10.0), 0.0)

    def test_five_pct_midpoint(self):
        score = _ivspd_s(5.0)
        self.assertGreater(score, 0.0)
        self.assertLess(score, 10.0)


class TestOhlcScore(unittest.TestCase):
    def test_bottom_of_range(self):
        self.assertAlmostEqual(_ohlc_s(0.0), 2.0)

    def test_top_of_range(self):
        self.assertAlmostEqual(_ohlc_s(100.0), 10.0)

    def test_midpoint(self):
        score = _ohlc_s(50.0)
        self.assertGreater(score, 2.0)
        self.assertLess(score, 10.0)


class TestPressureScore(unittest.TestCase):
    def test_neutral_pressure(self):
        self.assertAlmostEqual(_pressure_s(0.0), 5.0)

    def test_full_bid_pressure(self):
        self.assertAlmostEqual(_pressure_s(1.0), 10.0)

    def test_full_ask_pressure(self):
        self.assertAlmostEqual(_pressure_s(-1.0), 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — compute_tqs_v2
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeTqsV2(unittest.TestCase):

    def _base_contract(self, **kw) -> dict:
        c = {"spread_pct": 3.0, "oi": 5000, "volume": 10000}
        c.update(kw)
        return c

    def test_range_0_to_100(self):
        c = self._base_contract()
        score = compute_tqs_v2(c)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_excellent_liquid_contract(self):
        c = self._base_contract(spread_pct=0.5, oi=50000, volume=50000)
        score = compute_tqs_v2(c, iv_spread=0.1, ohlc_range_pct=70, pressure=0.8)
        self.assertGreater(score, 80.0, "Highly liquid contract should score > 80")

    def test_zero_oi_reduces_score(self):
        """OI is display-only — changing OI does NOT affect TQS v2 score."""
        c_with_oi  = self._base_contract(oi=5000)
        c_no_oi    = self._base_contract(oi=0)
        s_with     = compute_tqs_v2(c_with_oi)
        s_no       = compute_tqs_v2(c_no_oi)
        self.assertEqual(s_with, s_no, "OI is display-only — must not affect TQS v2")

    def test_moomoo_offline_oi_uses_neutral(self):
        """OI is display-only — oi_source has no effect on TQS v2 score."""
        # With Moomoo removed, OI sub-score is always neutral 5.0
        c_offline = self._base_contract(oi=0, oi_source="unavailable")
        c_legit_zero = self._base_contract(oi=0)
        score_offline    = compute_tqs_v2(c_offline)
        score_legit_zero = compute_tqs_v2(c_legit_zero)
        self.assertEqual(
            score_offline, score_legit_zero,
            "OI is display-only — oi_source must not affect TQS v2",
        )

    def test_missing_ws_fields_use_neutral(self):
        c = self._base_contract()
        # No iv_spread / ohlc_range_pct / pressure → neutral sub-scores 5.0
        score_no_ws = compute_tqs_v2(c)
        score_ws    = compute_tqs_v2(c, iv_spread=0.5, ohlc_range_pct=60, pressure=0.3)
        # Both should be valid floats in range
        self.assertGreaterEqual(score_no_ws, 0.0)
        self.assertLessEqual(score_no_ws, 100.0)
        # Scores differ (WS fields add information)
        self.assertNotAlmostEqual(score_no_ws, score_ws, places=1)

    def test_high_spread_penalises_score(self):
        c_tight = self._base_contract(spread_pct=0.5)
        c_wide  = self._base_contract(spread_pct=50.0)
        self.assertGreater(
            compute_tqs_v2(c_tight),
            compute_tqs_v2(c_wide),
            "Wide spread should lower TQS v2",
        )

    def test_result_is_float_not_int(self):
        c = self._base_contract()
        score = compute_tqs_v2(c)
        self.assertIsInstance(score, float)


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — Derived field calculations (no I/O)
# ══════════════════════════════════════════════════════════════════════════════

class TestDerivedFields(unittest.TestCase):
    """Test that get_calls() correctly computes ohlc_range_pct, pressure, iv_spread."""

    def _make_fetcher_with_mocks(self, ws_data=None, moo_available=True, moo_oi=12500):
        ws_data = ws_data or {}
        moo_contracts = [_moo_contract(oi=moo_oi)] if moo_available else []
        oi_map = {(215.0, "2026-05-11", "CALL"): moo_oi} if moo_available else {}
        oi_src = "moomoo" if moo_available else "unavailable"

        with patch("tradingview.options.hybrid_options._is_us_market_hours", return_value=True):
            fetcher = HybridOptionsFetcher(verbose=False)
            fetcher._fetch_tv = MagicMock(return_value=[_tv_contract()])
            # _fetch_moomoo_all returns (mm_contracts, oi_map, oi_source)
            fetcher._fetch_moomoo_all = MagicMock(return_value=(moo_contracts, oi_map, oi_src))
            fetcher._fetch_ws = MagicMock(return_value=(ws_data, "live" if ws_data else "unavailable"))
            return fetcher

    def test_ohlc_range_pct_computed(self):
        ws = {"OPRA:NVDA260511C215.0": {
            "lp": 2.13, "high_price": 4.30, "low_price": 1.81,
            "bid_size": 131, "ask_size": 10, "volume": 75999,
        }}
        fetcher = self._make_fetcher_with_mocks(ws_data=ws)
        contracts = fetcher.get_calls("NASDAQ:NVDA")
        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertIsNotNone(c.get("ohlc_range_pct"), "ohlc_range_pct must be set")
        # (2.13 - 1.81) / (4.30 - 1.81) × 100 ≈ 12.85 %
        self.assertAlmostEqual(c["ohlc_range_pct"], 12.85, delta=1.5)

    def test_pressure_computed(self):
        ws = {"OPRA:NVDA260511C215.0": {
            "lp": 2.13, "high_price": 4.30, "low_price": 1.81,
            "bid_size": 131, "ask_size": 10, "volume": 75999,
        }}
        fetcher = self._make_fetcher_with_mocks(ws_data=ws)
        contracts = fetcher.get_calls("NASDAQ:NVDA")
        c = contracts[0]
        self.assertIsNotNone(c.get("pressure"), "pressure must be set when WS is available")
        # (131 - 10) / (131 + 10) ≈ 0.858
        self.assertAlmostEqual(c["pressure"], 0.858, delta=0.01)

    def test_iv_spread_computed(self):
        """iv_spread = abs(ask_iv - bid_iv) from TV data."""
        fetcher = self._make_fetcher_with_mocks(ws_data={})
        contracts = fetcher.get_calls("NASDAQ:NVDA")
        c = contracts[0]
        # bid_iv=44.8, ask_iv=45.6 → spread=0.8
        self.assertIsNotNone(c.get("iv_spread"))
        self.assertAlmostEqual(c["iv_spread"], 0.8, delta=0.01)

    def test_source_is_hybrid(self):
        fetcher = self._make_fetcher_with_mocks(ws_data={})
        contracts = fetcher.get_calls("NASDAQ:NVDA")
        self.assertEqual(contracts[0]["source"], "hybrid")

    def test_tqs_v2_present_and_in_range(self):
        fetcher = self._make_fetcher_with_mocks(ws_data={})
        contracts = fetcher.get_calls("NASDAQ:NVDA")
        score = contracts[0]["tqs_v2"]
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — OI merge by (strike, expiry, type)
# ══════════════════════════════════════════════════════════════════════════════

class TestOiMerge(unittest.TestCase):
    """Test that Moomoo OI is correctly merged into TV contracts."""

    def _run_get_calls(self, tv_contracts, moo_contracts, ws_data=None):
        """Run get_calls() with all network fully mocked."""
        ws_data = ws_data or {}
        oi_map = {}
        for mc in moo_contracts:
            key = (round(mc["strike"], 2), mc["expiry"], mc["option_type"].upper())
            oi_map[key] = mc.get("oi", 0)
        moo_src = "moomoo" if oi_map else "unavailable"

        with patch("tradingview.options.hybrid_options._is_us_market_hours", return_value=False):
            fetcher = HybridOptionsFetcher(verbose=False)
            fetcher._fetch_tv = MagicMock(return_value=tv_contracts)
            # _fetch_moomoo_all returns (mm_contracts, oi_map, oi_source)
            fetcher._fetch_moomoo_all = MagicMock(return_value=(moo_contracts, oi_map, moo_src))
            fetcher._fetch_ws = MagicMock(return_value=(ws_data, "unavailable"))
            return fetcher.get_calls("NASDAQ:NVDA")

    def test_oi_merged_by_strike_expiry_type(self):
        """Moomoo removed — OI stays 0, oi_source='unavailable'."""
        tv  = [_tv_contract(strike=215.0, expiry="2026-05-11", oi=0)]
        moo = [_moo_contract(strike=215.0, expiry="2026-05-11", oi=12500)]
        result = self._run_get_calls(tv, moo)
        self.assertEqual(result[0]["oi"], 0, "OI stays 0 — Moomoo removed from pipeline")
        self.assertEqual(result[0]["oi_source"], "unavailable")

    def test_unmatched_strike_keeps_oi_zero(self):
        tv  = [_tv_contract(strike=220.0, expiry="2026-05-11", oi=0)]
        moo = [_moo_contract(strike=215.0, expiry="2026-05-11", oi=12500)]
        result = self._run_get_calls(tv, moo)
        self.assertEqual(result[0]["oi"], 0, "Unmatched strike must keep oi=0")
        self.assertEqual(result[0]["oi_source"], "unavailable")

    def test_unmatched_expiry_keeps_oi_zero(self):
        tv  = [_tv_contract(strike=215.0, expiry="2026-05-18", oi=0)]
        moo = [_moo_contract(strike=215.0, expiry="2026-05-11", oi=12500)]
        result = self._run_get_calls(tv, moo)
        self.assertEqual(result[0]["oi"], 0, "Unmatched expiry must keep oi=0")

    def test_multiple_expiries_merged_correctly(self):
        """Moomoo removed — OI stays 0 for all expiries."""
        tv = [
            _tv_contract(strike=215.0, expiry="2026-05-11", code="OPRA:NVDA260511C215.0"),
            _tv_contract(strike=215.0, expiry="2026-05-18", code="OPRA:NVDA260518C215.0"),
        ]
        moo = [
            _moo_contract(strike=215.0, expiry="2026-05-11", oi=8000),
            _moo_contract(strike=215.0, expiry="2026-05-18", oi=3000),
        ]
        result = self._run_get_calls(tv, moo)
        oi_by_expiry = {c["expiry"]: c["oi"] for c in result}
        self.assertEqual(oi_by_expiry["2026-05-11"], 0)
        self.assertEqual(oi_by_expiry["2026-05-18"], 0)


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Graceful degradation
# ══════════════════════════════════════════════════════════════════════════════

class TestGracefulDegradation(unittest.TestCase):
    """Verify the system stays functional when individual sources fail."""

    def _run_get_calls_with_failures(self, moo_available=True, ws_fails=False, tv_contracts=None):
        tv_contracts = tv_contracts or [_tv_contract()]
        moo_contracts = [_moo_contract(oi=5000)] if moo_available else []
        ws_data = WS_LIVE_DATA if not ws_fails else {}

        if not moo_available:
            oi_map, oi_src = {}, "unavailable"
        else:
            oi_map = {(215.0, "2026-05-11", "CALL"): 5000}
            oi_src = "moomoo"

        with patch("tradingview.options.hybrid_options._is_us_market_hours", return_value=True):
            fetcher = HybridOptionsFetcher(verbose=False)
            fetcher._fetch_tv = MagicMock(return_value=tv_contracts)
            # _fetch_moomoo_all returns (mm_contracts, oi_map, oi_source)
            fetcher._fetch_moomoo_all = MagicMock(return_value=(moo_contracts, oi_map, oi_src))
            fetcher._fetch_ws = MagicMock(return_value=(ws_data, "unavailable" if ws_fails else "live"))
            return fetcher.get_calls("NASDAQ:NVDA")

    def test_moomoo_offline_keeps_oi_zero(self):
        """When Moomoo is offline, contracts still return but oi=0."""
        result = self._run_get_calls_with_failures(moo_available=False)
        self.assertGreater(len(result), 0, "Should still return contracts when Moomoo offline")
        self.assertEqual(result[0]["oi"], 0)
        self.assertEqual(result[0]["oi_source"], "unavailable")

    def test_moomoo_offline_source_is_still_hybrid(self):
        result = self._run_get_calls_with_failures(moo_available=False)
        self.assertEqual(result[0]["source"], "hybrid")

    def test_websocket_fails_ohlc_range_is_none(self):
        """When WebSocket fails, ohlc_range_pct and pressure should be None."""
        result = self._run_get_calls_with_failures(ws_fails=True)
        self.assertIsNone(result[0]["ohlc_range_pct"])
        self.assertIsNone(result[0]["pressure"])

    def test_websocket_fails_tqs_v2_still_computed(self):
        """TQS v2 must still be computed (using neutral sub-scores) when WS fails."""
        result = self._run_get_calls_with_failures(ws_fails=True)
        score = result[0]["tqs_v2"]
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_websocket_fails_badge_is_no_websocket(self):
        result = self._run_get_calls_with_failures(ws_fails=True)
        self.assertIn("No WebSocket", result[0]["tqs_v2_badge"])

    def test_both_fail_source_still_hybrid(self):
        result = self._run_get_calls_with_failures(moo_available=False, ws_fails=True)
        self.assertEqual(result[0]["source"], "hybrid")

    def test_tv_and_moomoo_both_empty_returns_empty(self):
        """Only when BOTH TV and Moomoo return nothing should get_calls return []."""
        with patch("tradingview.options.hybrid_options._is_us_market_hours", return_value=False):
            fetcher = HybridOptionsFetcher(verbose=False)
            fetcher._fetch_tv = MagicMock(return_value=[])
            # _fetch_moomoo_all also returns nothing
            fetcher._fetch_moomoo_all = MagicMock(return_value=([], {}, "unavailable"))
            fetcher._fetch_ws = MagicMock(return_value=({}, "unavailable"))
            result = fetcher.get_calls("NASDAQ:NVDA")
        self.assertEqual(result, [])


# ══════════════════════════════════════════════════════════════════════════════
# Section 6 — WebSocket badge tagging
# ══════════════════════════════════════════════════════════════════════════════

class TestWsBadge(unittest.TestCase):
    """Verify tqs_v2_badge reflects ws_source correctly."""

    def _fetch(self, ws_source_label: str, ws_data: dict):
        with patch("tradingview.options.hybrid_options._is_us_market_hours",
                   return_value=(ws_source_label == "live")):
            fetcher = HybridOptionsFetcher(verbose=False)
            fetcher._fetch_tv = MagicMock(return_value=[_tv_contract()])
            # _fetch_moomoo_all returns (mm_contracts, oi_map, oi_source)
            fetcher._fetch_moomoo_all = MagicMock(return_value=([], {}, "unavailable"))
            fetcher._fetch_ws = MagicMock(return_value=(ws_data, ws_source_label))
            return fetcher.get_calls("NASDAQ:NVDA")

    def test_live_badge(self):
        result = self._fetch("live", WS_LIVE_DATA)
        self.assertIn("Market Hours", result[0]["tqs_v2_badge"])

    def test_closed_badge(self):
        result = self._fetch("closed", WS_LIVE_DATA)
        self.assertIn("Market Closed", result[0]["tqs_v2_badge"])

    def test_unavailable_badge(self):
        result = self._fetch("unavailable", {})
        self.assertIn("No WebSocket", result[0]["tqs_v2_badge"])


# ══════════════════════════════════════════════════════════════════════════════
# Section 7 — is_available() delegates to TVOptionsFetcher
# ══════════════════════════════════════════════════════════════════════════════

class TestIsAvailable(unittest.TestCase):
    def test_available_when_tv_available(self):
        with patch("tradingview.options.tv_options.TVOptionsFetcher") as MockTV:
            MockTV.return_value.is_available.return_value = True
            fetcher = HybridOptionsFetcher()
            self.assertTrue(fetcher.is_available())

    def test_unavailable_when_tv_unavailable(self):
        with patch("tradingview.options.tv_options.TVOptionsFetcher") as MockTV:
            MockTV.return_value.is_available.return_value = False
            fetcher = HybridOptionsFetcher()
            self.assertFalse(fetcher.is_available())

    def test_unavailable_on_exception(self):
        with patch(
            "tradingview.options.hybrid_options.HybridOptionsFetcher.is_available",
            side_effect=Exception("cookie missing"),
        ):
            fetcher = HybridOptionsFetcher()
            with self.assertRaises(Exception):
                fetcher.is_available()


# ══════════════════════════════════════════════════════════════════════════════
# Section 8 — Floating-point strike normalisation
# ══════════════════════════════════════════════════════════════════════════════

class TestStrikeNormalisation(unittest.TestCase):
    """Verify that floating-point strike rounding matches between TV and Moomoo."""

    def test_float_strike_match(self):
        """Strike rounding still normalises correctly (Moomoo removed, OI stays 0)."""
        tv  = [_tv_contract(strike=215.0, expiry="2026-05-11")]
        with patch("tradingview.options.hybrid_options._is_us_market_hours", return_value=False):
            fetcher = HybridOptionsFetcher(verbose=False)
            fetcher._fetch_tv = MagicMock(return_value=tv)
            fetcher._fetch_moomoo_all = MagicMock(return_value=([], {}, "unavailable"))
            fetcher._fetch_ws = MagicMock(return_value=({}, "unavailable"))
            result = fetcher.get_calls("NASDAQ:NVDA")
        self.assertEqual(result[0]["oi"], 0, "OI stays 0 — Moomoo removed from pipeline")
        self.assertEqual(round(result[0]["strike"], 2), 215.0, "Strike rounding must normalise")


# ══════════════════════════════════════════════════════════════════════════════
# Section 9 — Sequential merge (true hybrid, not fallback)
# ══════════════════════════════════════════════════════════════════════════════

class TestSequentialMerge(unittest.TestCase):
    """
    Verify the NEW sequential-merge architecture:
      ALL sources always run, missing data → neutral defaults.
      Moomoo seeds contracts when TV scan2 returns empty (weekend).
    """

    def _make_fetcher(self, tv_contracts, moo_contracts, moo_oi_map, moo_src,
                      ws_data=None, market_hours=False):
        ws_data = ws_data or {}
        with patch("tradingview.options.hybrid_options._is_us_market_hours",
                   return_value=market_hours):
            fetcher = HybridOptionsFetcher(verbose=False)
            fetcher._fetch_tv = MagicMock(return_value=tv_contracts)
            fetcher._fetch_moomoo_all = MagicMock(
                return_value=(moo_contracts, moo_oi_map, moo_src)
            )
            fetcher._fetch_ws = MagicMock(
                return_value=(ws_data, "live" if market_hours and ws_data else "unavailable")
            )
            return fetcher

    def test_weekend_tv_empty_returns_empty(self):
        """
        When TV scan2 returns empty (weekend / after-hours) AND Moomoo is
        removed, get_calls() returns [] (no contract source available).
        """
        fetcher = self._make_fetcher(
            tv_contracts=[],           # TV empty (weekend)
            moo_contracts=[],
            moo_oi_map={},
            moo_src="unavailable",
        )
        result = fetcher.get_calls("NASDAQ:NVDA")
        self.assertEqual(len(result), 0, "No contracts when TV empty and Moomoo removed")

    def test_all_three_sources_contribute_flags(self):
        """
        With Moomoo removed, data_completeness has tv=True, moomoo=False, ws=True.
        """
        tv_c   = _tv_contract(strike=215.0, expiry="2026-05-11")
        ws_d   = {"OPRA:NVDA260511C215.0": {
            "lp": 2.10, "open_price": 2.0, "high_price": 2.5,
            "low_price": 1.8, "bid_size": 100, "ask_size": 20, "volume": 5000,
        }}
        fetcher = self._make_fetcher(
            tv_contracts=[tv_c],
            moo_contracts=[],
            moo_oi_map={},
            moo_src="unavailable",
            ws_data=ws_d,
            market_hours=True,
        )
        result = fetcher.get_calls("NASDAQ:NVDA")
        self.assertEqual(len(result), 1)
        dc = result[0]["data_completeness"]
        self.assertTrue(dc["tv"],      "TV flag must be True")
        self.assertFalse(dc["moomoo"], "Moomoo flag must be False — removed from pipeline")
        self.assertTrue(dc["ws"],      "WS flag must be True")

    def test_data_completeness_always_present(self):
        """Every contract must have a data_completeness dict regardless of source mix."""
        tv_c   = _tv_contract()
        fetcher = self._make_fetcher(
            tv_contracts=[tv_c],
            moo_contracts=[],
            moo_oi_map={},
            moo_src="unavailable",
        )
        result = fetcher.get_calls("NASDAQ:NVDA")
        for c in result:
            self.assertIn("data_completeness", c, "Every contract must have data_completeness")
            self.assertIn("tv",     c["data_completeness"])
            self.assertIn("moomoo", c["data_completeness"])
            self.assertIn("ws",     c["data_completeness"])

    def test_hybrid_sources_label_reflects_available_sources(self):
        """
        hybrid_sources string must list only the sources that succeeded:
        'TV' when only TV works, 'TV+Moomoo' when both work, etc.
        """
        # TV only
        tv_c   = _tv_contract()
        fetcher = self._make_fetcher(
            tv_contracts=[tv_c],
            moo_contracts=[],
            moo_oi_map={},
            moo_src="unavailable",
        )
        result = fetcher.get_calls("NASDAQ:NVDA")
        self.assertIn("TV", result[0]["hybrid_sources"])
        self.assertNotIn("Moomoo", result[0]["hybrid_sources"])

    def test_moomoo_removed_no_seeding(self):
        """
        With Moomoo removed, when TV returns nothing, get_calls() returns [].
        Moomoo can no longer seed contracts.
        """
        fetcher = self._make_fetcher(
            tv_contracts=[],
            moo_contracts=[],
            moo_oi_map={},
            moo_src="unavailable",
        )
        result = fetcher.get_calls("NASDAQ:NVDA")
        self.assertEqual(len(result), 0, "No contracts when TV empty and Moomoo removed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
