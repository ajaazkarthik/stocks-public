"""
Tests: score synchronisation between signal_stack_actionable and sell_strategy_report.
=======================================================================================
The "Signal Analysis" panel in sell_strategy_report.html must use the same data quality
as the actionable report. The root cause of the discrepancy was that:

  1. ``analyse_signal`` hard-coded ``use_auth=False`` when fetching TV fundamentals.
  2. ``_fetch_signal_insight`` didn't forward ``use_auth=True`` to ``analyse_signal``.

These tests pin both fixes and verify the full grade/composite rendering pipeline.

All tests are network-free (all I/O is mocked).
"""

from __future__ import annotations

import ast
import json
import sys
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ─── path setup ───────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent  # tradingview/
sys.path.insert(0, str(_ROOT))

from sell.sell_strategy import (
    _fetch_signal_insight,
    _apply_signal_confirmation,
    fetch_tv_screener,
    render_signal_insight,
    build_grade_filter,
)
from signal_engine.analysis_engine import AnalysisEngine


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_insight(**overrides) -> dict:
    """Minimal valid insight dict as returned by _fetch_signal_insight."""
    base = {
        "composite":    72.0,
        "_grade":       "B",
        "scores": {
            "l1": {"total": 7.2},
            "l2": {"total": 8.0},
            "l3": {"total": 6.5},
            "l4": {"total": 6.0},
        },
        "fundamentals": {
            "company_name":  "Test Corp",
            "forward_pe":    18.5,
            "roe":           22.3,
            "revenue_growth": 12.1,
            "current_price": 120.0,
        },
        "sentiment_data": {},
    }
    base.update(overrides)
    return base


def _decision_result(action: str = "SELL", composite: float = 70.0,
                     directional: float = 8.0, context: float = 7.0) -> dict:
    return {
        "action": action,
        "action_reason": "Stop loss hit" if action == "SELL" else "2/5 decay flags triggered",
        "scaling_action": "AVOID_ADD",
        "current_price": 99.0,
        "effective_stop": 100.0,
        "atr_current": 2.0,
        "_yf_ok": True,
        "stopped_out": action == "SELL",
        "vix_force_exit": False,
        "time_stop_triggered": False,
        "catastrophic_loss": False,
        "decay_count": 2 if action == "REDUCE" else 0,
        "urgency_score": 10.0,
        "signal_insight": {
            "composite": composite,
            "scores": {
                "l1": {
                    "total": 5.0,
                    "rsi": directional,
                    "sma200": directional,
                    "macd": directional,
                    "raw_rsi": 60.0,
                    "raw_sma200_pct": 5.0,
                    "raw_macd_hist": 0.2,
                },
                "l2": {"total": context},
                "l3": {"total": context},
                "l4": {"total": context},
            },
        },
    }


class TestSellScreenerL1Contract(unittest.TestCase):
    """The sell report must provide the technical fields consumed by L1."""

    def test_fetch_tv_screener_returns_rich_l1_fields(self):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "data": [{
                "s": "NASDAQ:TEST",
                "d": ["TEST", 1.2, 1.0, 20.0, 15.0, 100.0, 90.0,
                      25.0, 60.0, 2.0, 1.5, 1000.0, 2.0, 1.1, 110.0, 90.0],
            }],
        }).encode()

        with (
            patch("sell.sell_strategy._load_json_cache", return_value={}),
            patch("sell.sell_strategy._save_json_cache") as save_cache,
            patch("sell.sell_strategy.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            result = fetch_tv_screener(["NASDAQ:TEST"])

        technical = result["NASDAQ:TEST"]
        self.assertEqual(technical["tv_rsi"], 60.0)
        self.assertEqual(technical["tv_adx"], 25.0)
        self.assertEqual(technical["sma200_pct"], 11.11)
        self.assertEqual(technical["macd_hist"], 0.5)
        self.assertEqual(technical["bb_width"], 20.0)
        request_payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(request_payload["columns"], [
            "name", "relative_volume_10d_calc", "recommendation_mark",
            "price_earnings_ttm", "return_on_equity", "close", "SMA200",
            "ADX", "RSI", "MACD.macd", "MACD.signal",
            "average_volume_10d_calc", "Volatility.D", "beta_1_year",
            "BB.upper", "BB.lower",
        ])
        saved_cache = save_cache.call_args.args[1]
        self.assertTrue(any(key.startswith("technical-v2:") for key in saved_cache))


class TestSignalDecisionConfirmation(unittest.TestCase):
    """L1-L4 annotates risk actions and may only fail-close optional adds."""

    def test_shallow_stop_conflict_keeps_sell_and_urgency(self):
        result = _apply_signal_confirmation(_decision_result())

        self.assertEqual(result["action"], "SELL")
        self.assertEqual(result["action_reason"], "Stop loss hit")
        self.assertEqual(result["urgency_score"], 10.0)
        self.assertEqual(result["signal_confirmation"]["status"], "CONFLICTS")
        self.assertIn("shallow 0.50 ATR", result["signal_confirmation"]["reason"])
        self.assertTrue(result["signal_confirmation"]["risk_action_unchanged"])

    def test_deep_stop_stays_sell_even_with_bullish_confirmation(self):
        raw = _decision_result()
        raw["current_price"] = 90.0

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["action"], "SELL")
        self.assertFalse(result["signal_confirmation"]["scaling_action_changed"])
        self.assertNotIn("shallow", result["signal_confirmation"]["reason"])

    def test_vix_force_exit_is_never_softened(self):
        raw = _decision_result()
        raw["vix_force_exit"] = True

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["action"], "SELL")

    def test_strong_directional_conflict_keeps_reduce(self):
        result = _apply_signal_confirmation(_decision_result(action="REDUCE"))

        self.assertEqual(result["action"], "REDUCE")
        self.assertEqual(result["signal_confirmation"]["status"], "CONFLICTS")
        self.assertTrue(result["signal_confirmation"]["risk_action_unchanged"])

    def test_strongly_bearish_component_prevents_bullish_conflict(self):
        raw = _decision_result()
        raw["signal_insight"]["scores"]["l1"].update({
            "rsi": 8.0,
            "sma200": 8.0,
            "macd": 1.5,
        })

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["action"], "SELL")
        self.assertEqual(result["signal_confirmation"]["status"], "MIXED")
        self.assertEqual(result["signal_confirmation"]["directional_l1_floor"], 1.5)

    def test_weak_signal_evidence_confirms_sell(self):
        result = _apply_signal_confirmation(
            _decision_result(directional=3.0, context=4.0)
        )

        self.assertEqual(result["action"], "SELL")
        self.assertEqual(result["signal_confirmation"]["status"], "CONFIRMS")

    def test_weak_confirmation_blocks_pyramid_add(self):
        raw = _decision_result(action="HOLD", composite=55.0, directional=4.0, context=5.0)
        raw.update({
            "scaling_action": "PYRAMID_ADD",
            "scaling_qty": 10,
            "scaling_reason": "Original pyramid reason",
            "add_recommendation": {"action": "PYRAMID_ADD", "suggested_qty": 10},
        })

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["scaling_action"], "PYRAMID_BLOCKED")
        self.assertEqual(result["scaling_qty"], 0)
        self.assertIsNone(result["add_recommendation"])
        self.assertEqual(
            result["blocked_add_recommendation"]["recommendation"]["suggested_qty"],
            10,
        )
        self.assertEqual(
            result["blocked_add_recommendation"]["original_reason"],
            "Original pyramid reason",
        )
        self.assertTrue(result["signal_confirmation"]["scaling_action_changed"])

    def test_unavailable_insight_blocks_pyramid_add(self):
        raw = _decision_result(action="HOLD")
        raw.pop("signal_insight")
        raw.update({
            "scaling_action": "PYRAMID_ADD",
            "scaling_qty": 10,
            "scaling_reason": "Original pyramid reason",
            "add_recommendation": {"action": "PYRAMID_ADD", "suggested_qty": 10},
        })

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["scaling_action"], "PYRAMID_BLOCKED")
        self.assertEqual(result["signal_confirmation"]["status"], "INSUFFICIENT")
        self.assertTrue(result["signal_confirmation"]["scaling_action_changed"])
        self.assertEqual(
            result["blocked_add_recommendation"]["recommendation"]["suggested_qty"],
            10,
        )

    def test_mixed_component_floor_blocks_pyramid_add(self):
        raw = _decision_result(action="HOLD", directional=10.0, context=6.0)
        raw["signal_insight"]["scores"]["l1"].update({
            "rsi": 10.0,
            "sma200": 1.5,
            "macd": 10.0,
        })
        raw.update({
            "scaling_action": "PYRAMID_ADD",
            "scaling_qty": 10,
            "add_recommendation": {"action": "PYRAMID_ADD"},
        })

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["signal_confirmation"]["status"], "MIXED")
        self.assertEqual(result["scaling_action"], "PYRAMID_BLOCKED")
        self.assertEqual(result["signal_confirmation"]["directional_l1_floor"], 1.5)

    def test_supported_pyramid_add_is_preserved(self):
        raw = _decision_result(action="HOLD", composite=68.0, directional=6.0, context=6.0)
        raw.update({
            "scaling_action": "PYRAMID_ADD",
            "scaling_qty": 10,
            "add_recommendation": {"action": "PYRAMID_ADD"},
        })

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["scaling_action"], "PYRAMID_ADD")
        self.assertEqual(result["scaling_qty"], 10)

    def test_sparse_l1_data_cannot_override_existing_action(self):
        raw = _decision_result()
        l1 = raw["signal_insight"]["scores"]["l1"]
        l1.update({"raw_sma200_pct": None, "raw_macd_hist": None})

        result = _apply_signal_confirmation(raw)

        self.assertEqual(result["action"], "SELL")
        self.assertEqual(result["signal_confirmation"]["status"], "INSUFFICIENT")
        self.assertIsNone(result["signal_confirmation"]["directional_l1_score"])

    def test_risk_action_reason_and_urgency_are_always_immutable(self):
        for action, directional, context in (
            ("SELL", 8.0, 7.0),
            ("REDUCE", 8.0, 7.0),
            ("HOLD", 2.0, 3.0),
            ("TRAIL", 8.0, 7.0),
            ("REVIEW", 2.0, 3.0),
        ):
            with self.subTest(action=action):
                raw = _decision_result(
                    action=action,
                    directional=directional,
                    context=context,
                )
                before = (raw["action"], raw["action_reason"], raw["urgency_score"])

                result = _apply_signal_confirmation(raw)

                after = (result["action"], result["action_reason"], result["urgency_score"])
                self.assertEqual(after, before)
                self.assertTrue(result["signal_confirmation"]["risk_action_unchanged"])


# ══════════════════════════════════════════════════════════════════════════════
# 1. API contract: analyse_signal signature
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalyseSignalSignature(unittest.TestCase):
    """analyse_signal must accept use_auth and pass it to fetch_tv_fundamentals."""

    def test_use_auth_parameter_exists(self):
        sig = inspect.signature(AnalysisEngine.analyse_signal)
        self.assertIn("use_auth", sig.parameters)

    def test_use_auth_defaults_to_false(self):
        sig = inspect.signature(AnalysisEngine.analyse_signal)
        self.assertIs(sig.parameters["use_auth"].default, False)

    def test_use_auth_true_passed_to_fetch_tv_fundamentals(self):
        """When use_auth=True and no tv_fund_cache, fetch_tv_fundamentals must be called
        with use_auth=True."""
        engine = AnalysisEngine()
        with (
            patch.object(engine, "fetch_tv_screener",   return_value={}),
            patch.object(engine, "fetch_tv_fundamentals", return_value={}) as mock_fund,
            patch.object(engine, "tv_fund_to_compat",    return_value={}),
            patch.object(engine, "score_technical",      return_value={"total": 5.0}),
            patch.object(engine, "score_fundamental",    return_value={"total": 5.0}),
            patch.object(engine, "fetch_sentiment_data", return_value={}),
            patch.object(engine, "score_sentiment",      return_value={"total": 5.0}),
            patch.object(engine, "fetch_yield_curve",    return_value={}),
            patch.object(engine, "fetch_vix_level",      return_value=None),
            patch.object(engine, "fetch_sector_etf_momentum", return_value={}),
            patch.object(engine, "fetch_market_internals",    return_value={}),
            patch.object(engine, "score_market_internals",    return_value={"total": 5.0}),
            patch("yfinance.Ticker", return_value=MagicMock()),
        ):
            fire = {"pro_symbol": "NASDAQ:MSFT", "message": ""}
            engine.analyse_signal(fire, use_auth=True)

        _, kwargs = mock_fund.call_args
        self.assertTrue(kwargs.get("use_auth"), "fetch_tv_fundamentals must be called with use_auth=True")

    def test_use_auth_false_passed_to_fetch_tv_fundamentals(self):
        """When use_auth=False (default), fetch_tv_fundamentals must be called with use_auth=False."""
        engine = AnalysisEngine()
        with (
            patch.object(engine, "fetch_tv_screener",   return_value={}),
            patch.object(engine, "fetch_tv_fundamentals", return_value={}) as mock_fund,
            patch.object(engine, "tv_fund_to_compat",    return_value={}),
            patch.object(engine, "score_technical",      return_value={"total": 5.0}),
            patch.object(engine, "score_fundamental",    return_value={"total": 5.0}),
            patch.object(engine, "fetch_sentiment_data", return_value={}),
            patch.object(engine, "score_sentiment",      return_value={"total": 5.0}),
            patch.object(engine, "fetch_yield_curve",    return_value={}),
            patch.object(engine, "fetch_vix_level",      return_value=None),
            patch.object(engine, "fetch_sector_etf_momentum", return_value={}),
            patch.object(engine, "fetch_market_internals",    return_value={}),
            patch.object(engine, "score_market_internals",    return_value={"total": 5.0}),
            patch("yfinance.Ticker", return_value=MagicMock()),
        ):
            fire = {"pro_symbol": "NASDAQ:MSFT", "message": ""}
            engine.analyse_signal(fire)  # use_auth defaults to False

        _, kwargs = mock_fund.call_args
        self.assertFalse(kwargs.get("use_auth"), "fetch_tv_fundamentals must be called with use_auth=False by default")

    def test_tv_fund_cache_bypasses_fetch(self):
        """If tv_fund_cache is supplied, fetch_tv_fundamentals must NOT be called."""
        engine = AnalysisEngine()
        with (
            patch.object(engine, "fetch_tv_screener",        return_value={}),
            patch.object(engine, "fetch_tv_fundamentals",    return_value={}) as mock_fund,
            patch.object(engine, "tv_fund_to_compat",        return_value={}),
            patch.object(engine, "score_technical",          return_value={"total": 5.0}),
            patch.object(engine, "score_fundamental",        return_value={"total": 5.0}),
            patch.object(engine, "fetch_sentiment_data",     return_value={}),
            patch.object(engine, "score_sentiment",          return_value={"total": 5.0}),
            patch.object(engine, "fetch_yield_curve",        return_value={}),
            patch.object(engine, "fetch_vix_level",          return_value=None),
            patch.object(engine, "fetch_sector_etf_momentum", return_value={}),
            patch.object(engine, "fetch_market_internals",   return_value={}),
            patch.object(engine, "score_market_internals",   return_value={"total": 5.0}),
            patch("yfinance.Ticker", return_value=MagicMock()),
        ):
            fire = {"pro_symbol": "NASDAQ:MSFT", "message": ""}
            engine.analyse_signal(fire, tv_fund_cache={"NASDAQ:MSFT": {}}, use_auth=True)

        mock_fund.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 2. _fetch_signal_insight passes use_auth=True
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchSignalInsightUsesAuth(unittest.TestCase):
    """_fetch_signal_insight must call analyse_signal with use_auth=True."""

    def _fake_result(self) -> dict:
        return {
            "composite":      68.0,
            "scores":         {"l1": {"total": 7.0}, "l2": {"total": 7.0},
                               "l3": {"total": 6.0}, "l4": {"total": 6.0}},
            "fundamentals":   {},
            "sentiment_data": {},
        }

    def test_use_auth_true_forwarded(self):
        fake_engine = MagicMock()
        fake_engine.analyse_signal.return_value = self._fake_result()

        with (
            patch("sell.sell_strategy._SIGNAL_INSIGHT_OK", True),
            patch("sell.sell_strategy._ANALYSIS_ENGINE", fake_engine),
        ):
            _fetch_signal_insight("NASDAQ:MSFT")

        call_kwargs = fake_engine.analyse_signal.call_args
        self.assertTrue(
            call_kwargs.kwargs.get("use_auth"),
            "_fetch_signal_insight must forward use_auth=True to analyse_signal",
        )

    def test_returns_empty_when_engine_unavailable(self):
        with (
            patch("sell.sell_strategy._SIGNAL_INSIGHT_OK", False),
            patch("sell.sell_strategy._ANALYSIS_ENGINE", None),
        ):
            result = _fetch_signal_insight("NASDAQ:MSFT")
        self.assertEqual(result, {})

    def test_returns_error_dict_on_exception(self):
        fake_engine = MagicMock()
        fake_engine.analyse_signal.side_effect = RuntimeError("network down")

        with (
            patch("sell.sell_strategy._SIGNAL_INSIGHT_OK", True),
            patch("sell.sell_strategy._ANALYSIS_ENGINE", fake_engine),
        ):
            result = _fetch_signal_insight("NASDAQ:MSFT")

        self.assertIn("_error", result)
        self.assertIn("network down", result["_error"])

    def test_leveraged_underlying_prefetch_uses_auth_true(self):
        fake_engine = MagicMock()
        fake_engine.fetch_tv_fundamentals.return_value = {"NASDAQ:TSLA": {}}
        fake_engine.analyse_signal.return_value = self._fake_result()

        with (
            patch("sell.sell_strategy._SIGNAL_INSIGHT_OK", True),
            patch("sell.sell_strategy._CONVICTION_OK", True),
            patch("sell.sell_strategy._ANALYSIS_ENGINE", fake_engine),
        ):
            _fetch_signal_insight("NASDAQ:TSLL", tv_fund_cache={})

        fake_engine.fetch_tv_fundamentals.assert_called_once_with(
            ["NASDAQ:TSLA"], use_auth=True
        )

    def test_all_analysis_engine_fundamental_prefetches_use_auth_true(self):
        strategy_path = Path(__file__).resolve().parent.parent.parent / "sell" / "sell_strategy.py"
        tree = ast.parse(strategy_path.read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fetch_tv_fundamentals"
        ]

        self.assertGreaterEqual(len(calls), 2)
        for call_node in calls:
            use_auth = next(
                (keyword.value for keyword in call_node.keywords if keyword.arg == "use_auth"),
                None,
            )
            self.assertIsInstance(use_auth, ast.Constant)
            self.assertIs(use_auth.value, True)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Grade thresholds align with actionable report
# ══════════════════════════════════════════════════════════════════════════════

class TestGradeThresholds(unittest.TestCase):
    """Both reports assign grades with the same boundaries: ≥75 A, ≥60 B, ≥45 C, else D."""

    def _grade_from_fetch(self, composite: float) -> str:
        """Exercise the grade logic inside _fetch_signal_insight."""
        fake_engine = MagicMock()
        fake_engine.analyse_signal.return_value = {
            "composite":      composite,
            "scores":         {"l1": {"total": 5.0}, "l2": {"total": 5.0},
                               "l3": {"total": 5.0}, "l4": {"total": 5.0}},
            "fundamentals":   {},
            "sentiment_data": {},
        }
        with (
            patch("sell.sell_strategy._SIGNAL_INSIGHT_OK", True),
            patch("sell.sell_strategy._ANALYSIS_ENGINE", fake_engine),
        ):
            result = _fetch_signal_insight("NASDAQ:TEST")
        return result.get("_grade", "?")

    def test_grade_a_at_75(self):
        self.assertEqual(self._grade_from_fetch(75.0), "A")

    def test_grade_a_above_75(self):
        self.assertEqual(self._grade_from_fetch(80.0), "A")

    def test_grade_b_at_60(self):
        self.assertEqual(self._grade_from_fetch(60.0), "B")

    def test_grade_b_below_75(self):
        self.assertEqual(self._grade_from_fetch(74.9), "B")

    def test_grade_c_at_45(self):
        self.assertEqual(self._grade_from_fetch(45.0), "C")

    def test_grade_c_below_60(self):
        self.assertEqual(self._grade_from_fetch(59.9), "C")

    def test_grade_d_below_45(self):
        self.assertEqual(self._grade_from_fetch(44.9), "D")

    def test_grade_d_at_zero(self):
        self.assertEqual(self._grade_from_fetch(0.0), "D")


# ══════════════════════════════════════════════════════════════════════════════
# 4. render_signal_insight HTML output
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderSignalInsight(unittest.TestCase):
    """render_signal_insight must show the correct composite and sub-scores."""

    def test_empty_insight_returns_empty_string(self):
        self.assertEqual(render_signal_insight({}), "")

    def test_none_insight_returns_empty_string(self):
        self.assertEqual(render_signal_insight(None), "")

    def test_composite_shown_in_html(self):
        html = render_signal_insight(_make_insight(composite=72.0))
        self.assertIn("72/100", html)

    def test_grade_shown_in_html(self):
        html = render_signal_insight(_make_insight(composite=72.0, _grade="B"))
        self.assertIn(">B<", html)

    def test_l1_score_shown(self):
        insight = _make_insight()
        insight["scores"]["l1"]["total"] = 4.4
        html = render_signal_insight(insight)
        self.assertIn("4.4/10", html)

    def test_l2_score_shown(self):
        insight = _make_insight()
        insight["scores"]["l2"]["total"] = 8.7
        html = render_signal_insight(insight)
        self.assertIn("8.7/10", html)

    def test_l3_score_shown(self):
        insight = _make_insight()
        insight["scores"]["l3"]["total"] = 6.3
        html = render_signal_insight(insight)
        self.assertIn("6.3/10", html)

    def test_l4_score_shown(self):
        insight = _make_insight()
        insight["scores"]["l4"]["total"] = 6.0
        html = render_signal_insight(insight)
        self.assertIn("6.0/10", html)

    def test_error_insight_shows_error_section(self):
        html = render_signal_insight({"_error": "timeout", "composite": None})
        self.assertIn("Signal Engine", html)
        self.assertIn("unavailable", html)

    def test_company_name_shown(self):
        insight = _make_insight()
        insight["fundamentals"]["company_name"] = "Microsoft Corporation"
        html = render_signal_insight(insight)
        self.assertIn("Microsoft Corporation", html)

    def test_fwd_pe_shown(self):
        insight = _make_insight()
        insight["fundamentals"]["forward_pe"] = 26.2
        html = render_signal_insight(insight)
        self.assertIn("26.2", html)

    def test_no_nan_in_output(self):
        """NaN must never leak into the rendered HTML."""
        import re
        html = render_signal_insight(_make_insight())
        self.assertNotRegex(html, r"\bNaN\b")

    def test_signal_analysis_label_present(self):
        html = render_signal_insight(_make_insight())
        self.assertIn("Signal Analysis", html)

# ══════════════════════════════════════════════════════════════════════════════
# 5. Composite formula consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestCompositeFormula(unittest.TestCase):
    """AnalysisEngine.composite must delegate to signal_stack.composite with same weights."""

    def test_composite_returns_float(self):
        engine = AnalysisEngine()
        result = engine.composite(5.0, 5.0, 5.0, 5.0)
        self.assertIsInstance(result, float)

    def test_composite_range_0_to_100(self):
        engine = AnalysisEngine()
        self.assertGreaterEqual(engine.composite(0, 0, 0, 0), 0)
        self.assertLessEqual(engine.composite(10, 10, 10, 10), 100)

    def test_composite_equals_signal_stack(self):
        """AnalysisEngine.composite must produce the same value as signal_stack.composite."""
        import signal_stack as _ss
        engine = AnalysisEngine()
        for l1, l2, l3, l4 in [(4.4, 8.7, 6.3, 6.0), (5.0, 5.0, 5.0, 5.0),
                                 (0.0, 10.0, 0.0, 10.0), (10.0, 0.0, 10.0, 0.0)]:
            with self.subTest(l1=l1, l2=l2, l3=l3, l4=l4):
                self.assertAlmostEqual(
                    engine.composite(l1, l2, l3, l4),
                    _ss.composite(l1, l2, l3, l4),
                    places=4,
                )

    def test_analyse_signal_composite_matches_sub_scores(self):
        """composite in result must equal engine.composite(l1, l2, l3, l4)."""
        engine = AnalysisEngine()
        l1, l2, l3, l4 = 4.4, 8.7, 6.3, 6.0
        with (
            patch.object(engine, "fetch_tv_screener",        return_value={}),
            patch.object(engine, "fetch_tv_fundamentals",    return_value={}),
            patch.object(engine, "tv_fund_to_compat",        return_value={}),
            patch.object(engine, "score_technical",          return_value={"total": l1}),
            patch.object(engine, "score_fundamental",        return_value={"total": l2}),
            patch.object(engine, "fetch_sentiment_data",     return_value={}),
            patch.object(engine, "score_sentiment",          return_value={"total": l3}),
            patch.object(engine, "fetch_yield_curve",        return_value={}),
            patch.object(engine, "fetch_vix_level",          return_value=None),
            patch.object(engine, "fetch_sector_etf_momentum", return_value={}),
            patch.object(engine, "fetch_market_internals",   return_value={}),
            patch.object(engine, "score_market_internals",   return_value={"total": l4}),
            patch("yfinance.Ticker", return_value=MagicMock()),
        ):
            fire = {"pro_symbol": "NASDAQ:MSFT", "message": ""}
            result = engine.analyse_signal(fire)

        expected = engine.composite(l1, l2, l3, l4)
        self.assertAlmostEqual(result["composite"], expected, places=4)


# ══════════════════════════════════════════════════════════════════════════════
# 6. build_grade_filter counts grades correctly
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildGradeFilter(unittest.TestCase):
    """Grade filter bar must correctly tally grades from signal_insight keys."""

    def test_empty_results_returns_empty_string(self):
        self.assertEqual(build_grade_filter([]), "")

    def test_no_insight_returns_empty_string(self):
        results = [{"signal_insight": None}, {"signal_insight": {}}]
        self.assertEqual(build_grade_filter(results), "")

    def test_grade_counts_rendered(self):
        results = [
            {"signal_insight": {"_grade": "A"}},
            {"signal_insight": {"_grade": "A"}},
            {"signal_insight": {"_grade": "B"}},
        ]
        html = build_grade_filter(results)
        # Should have filter buttons and not be empty
        self.assertTrue(len(html) > 0)
        self.assertIn("A", html)
        self.assertIn("B", html)

    def test_dash_grade_not_counted(self):
        """Entries with _grade='-' (no insight available) must not produce a filter bar."""
        results = [{"signal_insight": {"_grade": "-"}},
                   {"signal_insight": {"_grade": "-"}}]
        self.assertEqual(build_grade_filter(results), "")


# ══════════════════════════════════════════════════════════════════════════════
# 7. webhook_server uses use_auth=True when calling _tv_fund_fetch
# ══════════════════════════════════════════════════════════════════════════════

class TestWebhookServerUsesAuth(unittest.TestCase):
    """
    webhook_server.py stores scores in the DB at fire time.
    Those stored scores feed performance_analysis.html.
    The webhook must call _tv_fund_fetch with use_auth=True so that stored L2
    fundamentals match the quality used by batch_analyzer and sell_strategy.
    """

    def _source_line(self) -> str:
        """Return the relevant line from webhook_server.py as a string."""
        ws_path = Path(__file__).resolve().parent.parent.parent / "webhook_server.py"
        return ws_path.read_text(encoding="utf-8")

    def test_webhook_server_calls_tv_fund_fetch_with_use_auth_true(self):
        """The _tv_fund_fetch call in webhook_server.py must use use_auth=True."""
        src = self._source_line()
        # Find lines with the actual _tv_fund_fetch call (skip import and def stubs)
        call_lines = [
            line.strip()
            for line in src.splitlines()
            if "_tv_fund_fetch(" in line
            and "import" not in line
            and not line.strip().startswith("def ")  # skip stub definitions
        ]
        self.assertTrue(call_lines, "No _tv_fund_fetch() call found in webhook_server.py")
        for line in call_lines:
            self.assertIn(
                "use_auth=True", line,
                f"webhook_server._tv_fund_fetch call must use use_auth=True, got: {line!r}",
            )

    def test_webhook_server_does_not_use_auth_false_for_tv_fund(self):
        """There must be no _tv_fund_fetch call with use_auth=False in webhook_server.py."""
        src = self._source_line()
        bad_lines = [
            line.strip()
            for line in src.splitlines()
            if "_tv_fund_fetch(" in line and "use_auth=False" in line and "import" not in line
        ]
        self.assertEqual(
            bad_lines, [],
            f"webhook_server.py has _tv_fund_fetch(use_auth=False): {bad_lines}",
        )

    def test_all_three_callsites_use_auth_true(self):
        """
        Regression: all three call-sites that fetch TV fundamentals for scoring
        must use use_auth=True:
          1. batch_analyzer.py  — fetch_tv_fundamentals(symbols, use_auth=True)
          2. sell_strategy.py   — analyse_signal(..., use_auth=True)
          3. webhook_server.py  — _tv_fund_fetch(fetch_syms, use_auth=True)
        """
        tv_root = Path(__file__).resolve().parent.parent.parent  # tradingview/

        checks = {
            "batch_analyzer.py": (
                tv_root / "signal_engine" / "batch_analyzer.py",
                "use_auth=True",
                "fetch_tv_fundamentals",
            ),
            "sell_strategy.py": (
                tv_root / "sell" / "sell_strategy.py",
                "use_auth=True",
                # Multi-line call: analyse_signal( ... use_auth=True, ...) — check full source
                "analyse_signal",
            ),
            "webhook_server.py": (
                tv_root / "webhook_server.py",
                "use_auth=True",
                "_tv_fund_fetch",
            ),
        }

        for label, (path, auth_flag, fn_name) in checks.items():
            with self.subTest(file=label):
                src = path.read_text(encoding="utf-8")
                # For sell_strategy: call is multi-line, so search the whole source blob
                # instead of line-by-line to handle fn_name and auth_flag on different lines.
                # Strategy: find the block starting at fn_name and check auth_flag appears
                # within 10 lines after it.
                lines = src.splitlines()
                found_call = False
                for idx, line in enumerate(lines):
                    stripped = line.strip()
                    if fn_name + "(" in stripped and "import" not in stripped and not stripped.startswith("def "):
                        # Check auth_flag within this line or the next 10 lines
                        window = "\n".join(lines[idx: idx + 10])
                        if auth_flag in window:
                            found_call = True
                            break
                self.assertTrue(
                    found_call,
                    f"{label}: {fn_name}() call must use {auth_flag} (checked ±10 lines). "
                    f"Verify the file was fixed.",
                )


if __name__ == "__main__":
    unittest.main()
