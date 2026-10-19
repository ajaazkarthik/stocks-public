"""
Tests for Task 3: Hedging Recommendations.

Tests cover:
  TestComputeHedgeRecommendation — pure logic for hedge urgency / vehicle / sizing
  TestBuildPortfolioHedgeBanner  — HTML banner renders without crashing
"""

import sys
import unittest
from pathlib import Path

# ─── path setup ──────────────────────────────────────────────────────────────
_TRADINGVIEW = Path(__file__).resolve().parent.parent.parent
_SELL_DIR    = _TRADINGVIEW / "sell"
sys.path.insert(0, str(_TRADINGVIEW))
sys.path.insert(0, str(_SELL_DIR))

from sell_strategy import (
    _compute_hedge_recommendation,
    build_portfolio_hedge_banner,
    _SECTOR_HEDGE_MAP,
    _VIX_PORTFOLIO_HEDGE,
)


def _make_result(**kwargs) -> dict:
    """Minimal result dict with all fields used by _compute_hedge_recommendation."""
    base = {
        "symbol":           "AAPL",
        "action":           "HOLD",
        "current_gain_pct": 25.0,
        "vix":              20.0,
        "vix_regime":       "NORMAL",
        "tp1_hit":          False,
        "decay_count":      0,
        "conviction_score": 7.0,
        "cgt_eligible":     False,
        "broker":           "",
        "sector":           "Technology",
    }
    base.update(kwargs)
    return base


class TestComputeHedgeRecommendation(unittest.TestCase):

    # ── NONE cases ───────────────────────────────────────────────────────────

    def test_no_triggers_returns_none(self):
        """Small gain, normal VIX, good conviction → NONE."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=5.0, vix=18.0, vix_regime="NORMAL"),
            vix=18.0,
        )
        self.assertEqual(r["hedge_urgency"], "NONE")
        self.assertIsNone(r["hedge_vehicle"])
        self.assertEqual(r["hedge_sizing_pct"], 0.0)
        self.assertEqual(r["hedge_triggers"], [])

    def test_loss_position_no_hedge(self):
        """Losing position in normal market → no hedge needed."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=-10.0, vix=20.0, vix_regime="NORMAL"),
            vix=20.0,
        )
        self.assertEqual(r["hedge_urgency"], "NONE")

    # ── URGENT cases ─────────────────────────────────────────────────────────

    def test_extreme_fear_large_gain_urgent(self):
        """VIX EXTREME_FEAR + gain >= 10% → URGENT hedge."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=20.0, vix=38.0, vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_urgency"], "URGENT")
        self.assertIsNotNone(r["hedge_vehicle"])
        self.assertGreaterEqual(r["hedge_sizing_pct"], 40.0)

    def test_extreme_fear_small_gain_no_urgent(self):
        """VIX EXTREME_FEAR but gain < 10% → not URGENT from T1."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=5.0, vix=38.0, vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        # T1 requires gain >= 10; should still be NONE unless T2 or others fire
        self.assertNotEqual(r["hedge_urgency"], "URGENT")

    def test_fear_plus_tp1_urgent(self):
        """VIX FEAR + TP1 hit → URGENT (or RECOMMEND) hedge."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=15.0, vix=28.0,
                         vix_regime="FEAR", tp1_hit=True),
            vix=28.0,
        )
        self.assertIn(r["hedge_urgency"], ("URGENT", "RECOMMEND"))
        self.assertGreaterEqual(r["hedge_sizing_pct"], 25.0)

    # ── RECOMMEND cases ───────────────────────────────────────────────────────

    def test_low_conviction_large_gain_recommend(self):
        """Low conviction < 4 + gain >= 15% → RECOMMEND hedge."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=18.0, conviction_score=3.0,
                         vix=20.0, vix_regime="NORMAL"),
            vix=20.0,
        )
        self.assertIn(r["hedge_urgency"], ("RECOMMEND", "URGENT"))
        self.assertGreaterEqual(r["hedge_sizing_pct"], 30.0)

    def test_decay_large_gain_recommend(self):
        """Signal decay 2/3 + gain >= 20% → RECOMMEND hedge."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=25.0, decay_count=2,
                         vix=20.0, vix_regime="NORMAL"),
            vix=20.0,
        )
        self.assertIn(r["hedge_urgency"], ("RECOMMEND", "URGENT"))
        self.assertGreaterEqual(r["hedge_sizing_pct"], 20.0)

    def test_reduce_action_gain_consider(self):
        """REDUCE action + gain 10%+ → CONSIDER hedge."""
        r = _compute_hedge_recommendation(
            _make_result(action="REDUCE", current_gain_pct=12.0,
                         vix=20.0, vix_regime="NORMAL"),
            vix=20.0,
        )
        self.assertNotEqual(r["hedge_urgency"], "NONE")

    # ── CONSIDER cases ────────────────────────────────────────────────────────

    def test_cgt_eligible_large_gain_elevated_vix_consider(self):
        """CGT eligible + 30%+ gain + VIX 20+ → CONSIDER."""
        r = _compute_hedge_recommendation(
            _make_result(cgt_eligible=True, current_gain_pct=35.0,
                         vix=21.0, vix_regime="NORMAL"),
            vix=21.0,
        )
        self.assertNotEqual(r["hedge_urgency"], "NONE")
        self.assertGreaterEqual(r["hedge_sizing_pct"], 15.0)

    def test_cgt_eligible_but_low_vix_no_trigger(self):
        """CGT eligible + large gain but VIX < 20 → T5 should not fire."""
        r = _compute_hedge_recommendation(
            _make_result(cgt_eligible=True, current_gain_pct=35.0,
                         vix=18.0, vix_regime="NORMAL"),
            vix=18.0,
        )
        # T5 requires vix >= 20 — should be NONE (no other triggers)
        self.assertEqual(r["hedge_urgency"], "NONE")

    # ── Vehicle selection ─────────────────────────────────────────────────────

    def test_tech_sector_uses_sqqq(self):
        """Technology sector → SQQQ hedge vehicle."""
        r = _compute_hedge_recommendation(
            _make_result(sector="Technology", current_gain_pct=20.0,
                         vix=38.0, vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"], "SQQQ")

    def test_financials_sector_uses_faz(self):
        """Financial Services → FAZ hedge vehicle."""
        r = _compute_hedge_recommendation(
            _make_result(sector="Financial Services", current_gain_pct=20.0,
                         vix=38.0, vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"], "FAZ")

    def test_energy_sector_uses_dug(self):
        """Energy sector → DUG hedge vehicle."""
        r = _compute_hedge_recommendation(
            _make_result(sector="Energy", current_gain_pct=20.0,
                         vix=38.0, vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"], "DUG")

    def test_unknown_sector_uses_default(self):
        """Unknown sector → _default hedge vehicle (SH)."""
        r = _compute_hedge_recommendation(
            _make_result(sector="Aerospace", current_gain_pct=20.0,
                         vix=38.0, vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"],
                         _SECTOR_HEDGE_MAP["_default"]["ticker"])

    def test_angelone_broker_uses_india_hedge(self):
        """AngelOne broker → India hedge vehicle regardless of sector."""
        r = _compute_hedge_recommendation(
            _make_result(broker="AngelOne", sector="Technology",
                         current_gain_pct=20.0, vix=38.0,
                         vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"],
                         _SECTOR_HEDGE_MAP["_IN"]["ticker"])

    def test_zerodha_broker_uses_india_hedge(self):
        """Zerodha broker → India hedge vehicle."""
        r = _compute_hedge_recommendation(
            _make_result(broker="Zerodha", sector="Healthcare",
                         current_gain_pct=20.0, vix=38.0,
                         vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"],
                         _SECTOR_HEDGE_MAP["_IN"]["ticker"])

    def test_moomoo_unknown_sector_uses_au_hedge(self):
        """Moomoo-AU broker with unknown sector -> AU hedge (BBOZ).

        Real fetched positions always carry the account-suffixed broker value
        f"Moomoo-{label}" (see _fetch_live_positions()) -- bare "Moomoo" is
        never actually produced. This test previously used broker="Moomoo",
        which only passed because the (buggy) implementation checked for
        that exact bare string; the check never matched a real broker value
        and this branch was silently dead code. Fixed 2026-07-06 -- both the
        implementation and this test now use realistic "Moomoo-AU"/"Moomoo-SG"
        values (see broker_badge() below, which already used the correct
        .startswith("Moomoo") pattern this copied).
        """
        r = _compute_hedge_recommendation(
            _make_result(broker="Moomoo-AU", sector="",
                         current_gain_pct=20.0, vix=38.0,
                         vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"],
                         _SECTOR_HEDGE_MAP["_AU"]["ticker"])

    def test_moomoo_sg_unknown_sector_falls_through_to_default(self):
        """Moomoo-SG (Singapore account) intentionally does NOT get the AU hedge.

        No single regional inverse ETF reliably covers the mixed SGX/TYO/KRX
        exposure a Moomoo-SG account can hold, so it falls through to
        _SECTOR_HEDGE_MAP["_default"] instead of "_AU".
        """
        r = _compute_hedge_recommendation(
            _make_result(broker="Moomoo-SG", sector="",
                         current_gain_pct=20.0, vix=38.0,
                         vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"],
                         _SECTOR_HEDGE_MAP["_default"]["ticker"])

    def test_moomoo_au_known_sector_uses_sector_hedge_not_au(self):
        """Moomoo-AU with a KNOWN sector uses the sector hedge, not the AU default."""
        r = _compute_hedge_recommendation(
            _make_result(broker="Moomoo-AU", sector="Technology",
                         current_gain_pct=20.0, vix=38.0,
                         vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertEqual(r["hedge_vehicle"]["ticker"],
                         _SECTOR_HEDGE_MAP["Technology"]["ticker"])

    # ── Return schema ─────────────────────────────────────────────────────────

    def test_all_keys_present_in_result(self):
        """All four expected keys are present in every return dict."""
        r = _compute_hedge_recommendation(_make_result(), vix=18.0)
        for key in ("hedge_urgency", "hedge_vehicle", "hedge_sizing_pct", "hedge_triggers"):
            self.assertIn(key, r)

    def test_triggers_list_on_urgent(self):
        """URGENT assessment populates at least one trigger string."""
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=20.0, vix=38.0, vix_regime="EXTREME_FEAR"),
            vix=38.0,
        )
        self.assertIsInstance(r["hedge_triggers"], list)
        self.assertGreater(len(r["hedge_triggers"]), 0)

    def test_sizing_pct_non_negative(self):
        """hedge_sizing_pct is always >= 0."""
        for gain in (-20.0, 0.0, 5.0, 50.0):
            r = _compute_hedge_recommendation(
                _make_result(current_gain_pct=gain, vix=20.0),
                vix=20.0,
            )
            self.assertGreaterEqual(r["hedge_sizing_pct"], 0.0)

    def test_multiple_triggers_take_highest_sizing(self):
        """When multiple triggers fire, sizing is the MAX of all triggered sizes."""
        # T1 (40%) + T3 (30%) → sizing should be 40%
        r = _compute_hedge_recommendation(
            _make_result(current_gain_pct=20.0, vix=38.0, vix_regime="EXTREME_FEAR",
                         conviction_score=3.0),
            vix=38.0,
        )
        self.assertGreaterEqual(r["hedge_sizing_pct"], 40.0)


class TestBuildPortfolioHedgeBanner(unittest.TestCase):
    """Tests that build_portfolio_hedge_banner() renders correctly."""

    def _make_results(self, hedge_urgencies: list[str]) -> list[dict]:
        return [{"hedge_urgency": u} for u in hedge_urgencies]

    def test_empty_results_no_hedge_normal_vix_returns_empty(self):
        """No hedge alerts + normal VIX → green "All Clear" banner (never empty)."""
        # sell_strategy.py always renders the hedge section so users can see the
        # full picture. With no triggers it shows a green "All Clear" panel.
        html = build_portfolio_hedge_banner(
            self._make_results([]),
            vix=18.0,
        )
        self.assertIn("All Clear", html)
        self.assertIn("HEDGE STATUS", html)
        self.assertNotIn("HEDGE ALERTS", html)

    def test_urgent_hedges_present_returns_html(self):
        """One URGENT position → banner HTML with URGENT text."""
        html = build_portfolio_hedge_banner(
            self._make_results(["URGENT", "NONE"]),
            vix=20.0,
        )
        self.assertIn("URGENT", html)
        self.assertIn("HEDGE ALERTS", html)

    def test_fear_vix_includes_portfolio_hedge_suggestion(self):
        """VIX >= 25 (FEAR) → portfolio hedge vehicle shown."""
        html = build_portfolio_hedge_banner(
            self._make_results(["NONE"]),
            vix=26.0,
        )
        # Portfolio hedge for FEAR is GLD
        self.assertIn("GLD", html)

    def test_extreme_fear_vix_includes_vixy(self):
        """VIX >= 35 (EXTREME_FEAR) → VIXY portfolio hedge."""
        html = build_portfolio_hedge_banner(
            self._make_results(["NONE"]),
            vix=36.0,
        )
        self.assertIn("VIXY", html)

    def test_normal_vix_no_hedges_no_banner(self):
        """All NONE hedges + normal VIX → green "All Clear" banner (never empty)."""
        html = build_portfolio_hedge_banner(
            self._make_results(["NONE", "NONE", "NONE"]),
            vix=18.0,
        )
        self.assertIn("All Clear", html)
        self.assertIn("HEDGE STATUS", html)
        self.assertNotIn("HEDGE ALERTS", html)

    def test_counts_all_urgency_levels(self):
        """URGENT + RECOMMEND + CONSIDER all reflected in banner."""
        html = build_portfolio_hedge_banner(
            self._make_results(["URGENT", "RECOMMEND", "CONSIDER", "NONE"]),
            vix=20.0,
        )
        self.assertIn("URGENT", html)
        self.assertIn("RECOMMEND", html)
        self.assertIn("CONSIDER", html)

    def test_renders_without_crash_all_none(self):
        """All NONE but elevated VIX → valid HTML, no exception."""
        html = build_portfolio_hedge_banner(
            self._make_results(["NONE", "NONE"]),
            vix=28.0,
        )
        self.assertIsInstance(html, str)


if __name__ == "__main__":
    unittest.main()
