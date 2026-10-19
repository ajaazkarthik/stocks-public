"""Phase 1–2 scoring regressions.

Tests the actual production scoring function AST in isolation, requiring only
Python's standard library (no broker credentials, networking or vendor APIs).
They deliberately avoid importing signal_stack's heavyweight orchestration.
Run: python -m unittest discover -s tests -p test_signal_scoring_phase12.py -v
"""
from __future__ import annotations

import ast
import math
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "tradingview" / "signal_stack.py"
TEXT = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(TEXT, filename=str(SOURCE))
NAMES = {
    "_score_number", "_score_observed",
    "score_technical", "score_fundamental", "score_sentiment",
    "score_market_internals", "_data_confidence",
    "_composite_components", "composite", "_compute_decision_scores",
}
FUNCS = [node for node in TREE.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name in NAMES]
assert {node.name for node in FUNCS} == NAMES, "One of the production scorers is missing"
NAMESPACE = {
    "math": math,
    "CONFIDENCE_DEMOTION_FLOOR": 0.85,
}
exec(compile(ast.Module(body=FUNCS, type_ignores=[]), str(SOURCE), "exec"), NAMESPACE)
ss = type("ScoringUnderTest", (), {k: staticmethod(NAMESPACE[k]) for k in NAMES})


class TestFiniteInputs(unittest.TestCase):
    def test_nonfinite_and_malformed_inputs_are_unknown(self):
        for value in (float("nan"), float("inf"), -float("inf"),
                      "nan", "inf", "-inf", "invalid", None, True):
            with self.subTest(value=str(value)):
                self.assertIsNone(ss._score_number(value))

    def test_numeric_strings_and_zero_still_work(self):
        self.assertEqual(ss._score_number("0"), 0.0)
        self.assertEqual(ss._score_number("32.5"), 32.5)
        self.assertIsNone(ss._score_number(101, low=0, high=100))


class TestTechnical(unittest.TestCase):
    def test_all_missing_is_neutral_without_fake_evidence(self):
        output = ss.score_technical({})
        self.assertEqual(output["total"], 5.0)
        self.assertEqual(output["coverage"], 0.0)
        self.assertEqual(output["adx"], 5.0)
        self.assertEqual(output["squeeze"], 5.0)
        self.assertIsNone(output["raw_adx"])

    def test_zero_squeeze_is_observed_but_absence_is_not(self):
        absent = ss.score_technical({})
        off = ss.score_technical({"sqzOff": 0})
        self.assertEqual(off["squeeze"], 4.0)
        self.assertLess(off["total"], absent["total"])
        self.assertGreater(off["coverage"], absent["coverage"])

    def test_adx_zero_is_bearish_not_missing(self):
        absent = ss.score_technical({})
        weak = ss.score_technical({"ADX": 0})
        self.assertEqual(weak["adx"], 0.0)
        self.assertLess(weak["total"], absent["total"])

    def test_invalid_win_rate_cannot_make_layer_perfect(self):
        poor = {"WinRate": 20, "NetProfit%": -25, "rsi": 28, "ADX": 15, "sqzOff": 0}
        for bad in (float("nan"), float("inf"), "-inf", -4, 101, "garbage"):
            with self.subTest(bad=str(bad)):
                invalid = ss.score_technical(dict(poor, WinRate=bad))
                unknown = ss.score_technical(dict(poor, WinRate=None))
                self.assertEqual(invalid["total"], unknown["total"])
                self.assertEqual(invalid["wr"], 5.0)
                self.assertLess(invalid["total"], 10.0)

    def test_tv_fallback_counts_as_real_coverage(self):
        src = {"tv_rsi": 60, "tv_adx": 35}
        result = ss.score_technical({}, src)
        self.assertEqual(result["rsi"], 10.0)
        self.assertEqual(result["adx"], 10.0)
        self.assertGreater(result["coverage"], 0)

    def test_optional_nonfinite_inputs_are_missing(self):
        r = ss.score_technical({}, {"macd_hist": float("nan"),
                                     "bb_width": float("inf"),
                                     "rel_vol": float("nan")})
        self.assertEqual(r["total"], 5.0)
        self.assertEqual(r["coverage"], 0.0)


class TestFundamental(unittest.TestCase):
    def test_all_missing_is_neutral(self):
        r = ss.score_fundamental({})
        self.assertEqual(r["total"], 5.0)
        self.assertEqual(r["piotroski"], 5.0)
        self.assertEqual(r["piotroski_coverage"], 0.0)

    def test_zero_debt_is_good_not_bad(self):
        healthy = {"debt_to_equity": 0, "current_ratio": 2.5,
                   "free_cashflow": 1000}
        result = ss.score_fundamental(healthy)
        self.assertEqual(result["health"], 10.0)
        self.assertEqual(result["health"],
                         ss.score_fundamental(dict(healthy, debt_to_equity=1))["health"])

    def test_higher_debt_cannot_improve_health_with_other_inputs_fixed(self):
        base = {"current_ratio": 2.5, "free_cashflow": 1000}
        observed = [ss.score_fundamental(dict(base, debt_to_equity=x))["health"]
                    for x in (0, 1, 30, 100, 200)]
        self.assertEqual(observed, sorted(observed, reverse=True))

    def test_current_ratio_zero_is_observed_bad(self):
        r = ss.score_fundamental({"debt_to_equity": 0,
                                  "current_ratio": 0,
                                  "free_cashflow": 1000})
        self.assertEqual(r["health"], 7.0)

    def test_sparse_piotroski_is_shrunk_to_neutral(self):
        one_good = ss.score_fundamental({"free_cashflow": 100})
        one_bad = ss.score_fundamental({"free_cashflow": -100})
        self.assertEqual(one_good["piotroski_observed"], 1)
        self.assertAlmostEqual(one_good["piotroski"], 5.83)
        self.assertAlmostEqual(one_bad["piotroski"], 4.17)

    def test_complete_piotroski_evidence_gets_original_maximum(self):
        fields = {"free_cashflow": 100, "roa": 10, "roe": 25,
                  "current_ratio": 2, "gross_margin": 50,
                  "revenue_growth": 15}
        result = ss.score_fundamental(fields)
        self.assertEqual(result["piotroski"], 10.0)
        self.assertEqual(result["piotroski_coverage"], 1.0)

    def test_invalid_fundamentals_cannot_escape_to_nan(self):
        r = ss.score_fundamental({"debt_to_equity": float("nan"),
                                  "current_ratio": float("-inf"),
                                  "roe": float("nan")})
        self.assertTrue(math.isfinite(r["total"]))
        self.assertEqual(r["total"], 5.0)


class TestSentiment(unittest.TestCase):
    def test_no_evidence_is_neutral(self):
        r = ss.score_sentiment({})
        self.assertEqual(r["total"], 5.0)
        self.assertEqual(r["analyst"], 5.0)
        self.assertEqual(r["coverage"], 0.0)

    def test_uncovered_analyst_is_not_bearish(self):
        a = ss.score_sentiment({"analyst": {"total": 0, "bull_ratio": 0.5}})
        self.assertEqual(a["analyst"], 5.0)

    def test_tradingview_aggregate_is_an_observed_analyst_signal(self):
        a = ss.score_sentiment({"analyst": {"source": "TV", "total": 0,
                                             "bull_ratio": 0}})
        self.assertEqual(a["analyst"], 0.0)
        self.assertGreater(a["coverage"], 0.0)

    def test_technical_recommend_all_not_used_as_analyst_proxy(self):
        source = ast.get_source_segment(
            TEXT, next(n for n in TREE.body
                       if isinstance(n, ast.FunctionDef)
                       and n.name == "fetch_sentiment_data"))
        self.assertNotIn("elif rec_all is not None:", source)

    def test_missing_article_count_does_not_crash(self):
        self.assertEqual(ss.score_sentiment({"news": {"count": None}})["total"], 5.0)

    def test_invalid_sentiment_components_do_not_escape(self):
        r = ss.score_sentiment({
            "analyst": {"source": "TV", "bull_ratio": float("nan"), "total": 0},
            "news": {"score": float("inf"), "count": 8},
            "earnings_beats": {"beat_rate": float("nan"), "quarters": 4},
            "revision_velocity": float("nan"),
        })
        self.assertTrue(math.isfinite(r["total"]))
        self.assertEqual(r["total"], 5.0)


class TestInternals(unittest.TestCase):
    def test_all_missing_is_neutral(self):
        r = ss.score_market_internals({})
        self.assertEqual((r["total"], r["coverage"]), (5.0, 0.0))

    def test_nonfinite_vendor_numbers_do_not_create_bullish_macro(self):
        result = ss.score_market_internals({
            "pcr": float("nan"), "insider_score_raw": float("nan"),
            "w52_position": float("nan"), "short_score_raw": float("inf"),
            "sector_etf_score": float("inf"),
            "vix3m_vix_ratio": float("nan"),
        }, yc={"spread_bps": float("nan")}, vix_level=float("nan"))
        self.assertEqual(result["total"], 5.0)
        self.assertEqual(result["coverage"], 0.0)

    def test_coverage_shrink_is_intentionally_unchanged(self):
        r = ss.score_market_internals({"w52_position": 0.7})
        self.assertGreater(r["total"], 5.0)
        self.assertLess(r["total"], r["raw_total"])
        self.assertAlmostEqual(r["coverage"], 0.18)


class TestConfidenceAndBlend(unittest.TestCase):
    def test_fallback_technical_indicators_are_counted(self):
        r = ss._data_confidence({}, {}, {}, {},
                                {"tv_rsi": 60, "tv_adx": 30})
        self.assertAlmostEqual(r["l1"], 2 / 9, places=3)

    def test_tv_aggregate_analyst_is_counted(self):
        r = ss._data_confidence({}, {}, {
            "analyst": {"source": "TV", "bull_ratio": 1.1, "total": 0}
        }, {})
        self.assertEqual(r["l3"], 0.25)

    def test_nan_is_not_counted_as_coverage(self):
        r = ss._data_confidence({"WinRate": float("nan")}, {}, {}, {})
        self.assertEqual(r["l1"], 0.0)

    def test_all_unknown_yields_neutral_composite_before_confidence(self):
        layers = (ss.score_technical({})["total"],
                  ss.score_fundamental({})["total"],
                  ss.score_sentiment({})["total"],
                  ss.score_market_internals({})["total"])
        self.assertEqual(layers, (5.0, 5.0, 5.0, 5.0))
        self.assertEqual(ss.composite(*layers), 50.0)
        quality, decision = ss._compute_decision_scores(
            *layers, institutional_l5=None, data_confidence=0)
        self.assertEqual(quality, 50.0)
        self.assertEqual(decision, 42.5)


if __name__ == "__main__":
    unittest.main()
