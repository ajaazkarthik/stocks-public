"""Phase 3: independent, deterministic arithmetic reconciliation for Signal Stack.

Executes production scorer definitions from the source AST (no runtime imports,
brokers, network calls or synthetic copies of the implementations). Expected
numbers come from scoring_reference_oracle, which imports *no production code*.

Run:
    python3 -m unittest discover -s tests -p 'test_signal_scoring_phase3.py' -v
"""
from __future__ import annotations

import ast
import math
import random
import unittest
from pathlib import Path

import scoring_reference_oracle as ref


SOURCE = Path(__file__).resolve().parents[1] / "tradingview" / "signal_stack.py"
TEXT = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(TEXT, filename=str(SOURCE))
NEEDED = {
    "_score_number", "_score_observed",
    "score_technical", "score_fundamental", "score_sentiment",
    "score_market_internals", "_data_confidence", "_composite_components",
    "composite", "_compute_decision_scores", "_score_breakdown",
}
DEFINITIONS = [node for node in TREE.body
               if isinstance(node, ast.FunctionDef) and node.name in NEEDED]
assert {node.name for node in DEFINITIONS} == NEEDED, "Production scoring contract changed"
ENV = {"math": math, "CONFIDENCE_DEMOTION_FLOOR": 0.85,
       "SCORE_MODEL_VERSION": "signal-stack-2026.10.19.phase12"}
exec(compile(ast.Module(body=DEFINITIONS, type_ignores=[]),
             str(SOURCE), "exec"), ENV)
class Production:
    pass
for name in NEEDED:
    setattr(Production, name, staticmethod(ENV[name]))


def regime_dict(name):
    return dict(zip(("l1", "l2", "l3", "l4"), ref.REGIMES[name]))


class IndependentLayers(unittest.TestCase):
    def test_reference_weight_contracts(self):
        self.assertAlmostEqual(sum(ref.TECHNICAL_BASE.values())
                               + sum(ref.TECHNICAL_OPTIONAL.values()), 1.0)
        for name, weights in ref.REGIMES.items():
            with self.subTest(regime=name):
                self.assertAlmostEqual(sum(weights), 1.0)

    def test_technical_each_optional_combination_and_threshold_edges(self):
        cases = [
            ({}, {}),
            ({"WinRate": 80, "NetProfit%": 50, "rsi": 65,
              "sqzOff": 1, "ADX": 35},
             {"rel_vol": 3, "sma200_pct": 15, "macd_hist": 0.5,
              "close": 100, "volatility": 2,
              "obv_divergence": 0.8, "bb_width": 2}),
            ({"WinRate": 20, "NetProfit%": -50, "rsi": 90, "sqzOff": 0,
              "ADX": 15},
             {"rel_vol": 0.6, "sma200_pct": -5, "macd_hist": -0.5,
              "close": 100, "volatility": 2, "bb_width": 22}),
            ({"WinRate": None, "NetProfit%": None, "sqzOff": None},
             {"rel_vol": 1.0, "sma200_pct": 0.0}),
        ]
        first = Production.score_technical(*cases[1])
        self.assertEqual(first["total"], 9.88)  # independently hand-computed
        for meta, screener in cases:
            with self.subTest(meta=meta, screener=screener):
                got = Production.score_technical(meta, screener)
                self.assertEqual(got["total"], ref.technical(got))
                self.assertGreaterEqual(got["coverage"], 0)
                self.assertLessEqual(got["coverage"], 1)

        rng = random.Random(304)
        optional = ("rel_vol", "sma200_pct", "macd_hist", "obv_divergence", "bb_width")
        for mask in range(32):
            screen = {key: rng.uniform(-0.7, 7) if mask & (1 << i) else None
                      for i, key in enumerate(optional)}
            screen.update(close=100, volatility=2.0)
            got = Production.score_technical(
                {"WinRate": 55, "NetProfit%": 15, "rsi": 65,
                 "sqzOff": True, "ADX": 30}, screen)
            self.assertEqual(got["total"], ref.technical(got),
                             f"Optional field mask={mask:05b}")

    def test_fundamental_independent_reconciliation_and_missing_fcf(self):
        cases = [
            {},
            {"debt_to_equity": 0, "current_ratio": 0, "free_cashflow": -100,
             "roe": 5, "earnings_growth": 5, "market_cap": 10000},
            {"forward_pe": 10, "peg_ratio": 1, "price_to_sales": 1,
             "price_to_book": 1, "roe": 25, "profit_margin": 20,
             "gross_margin": 50, "earnings_growth": 20, "revenue_growth": 20,
             "debt_to_equity": 0, "current_ratio": 2.5,
             "free_cashflow": 100, "market_cap": 1000, "roa": 10},
            {"ev_revenue": 2, "ev_ebitda": 4, "earnings_growth": -20,
             "revenue_growth": None, "gross_margin": None},
        ]
        for fund in cases:
            with self.subTest(fund=fund):
                result = Production.score_fundamental(fund)
                self.assertEqual(result["total"], ref.fundamental(result))
        self.assertEqual(Production.score_fundamental(cases[2])["total"], 10.0)

    def test_sentiment_bonus_stack_reconciles_both_sides_and_cap(self):
        baseline = {"analyst": {"source": "TV", "bull_ratio": 1.2, "total": 0},
                    "earnings_beats": {"beat_rate": 0.75, "quarters": 4},
                    "news": {"score": 0.4, "count": 9}}
        cases = [
            {},
            baseline,
            dict(baseline, recent_upgrade=True, recent_downgrade=False,
                 revision_velocity=4,
                 earnings_beats={"beat_rate": 0.75, "quarters": 4,
                                 "last_surprise_pct": 0.20, "days_since_report": 7},
                 news={"score": 0.95, "count": 15, "article_velocity": 12,
                       "quartr_catalyst": True}),
            dict(baseline, recent_downgrade=True, revision_velocity=-4,
                 earnings_days_away=1,
                 earnings_beats={"beat_rate": 0.0, "quarters": 4,
                                 "last_surprise_pct": -0.20, "days_since_report": 7},
                 news={"score": -0.90, "count": 15, "article_velocity": 9}),
            {"analyst": {"bull_ratio": None, "total": 0},
             "news": {"score": 0, "count": 0}},
        ]
        for sent in cases:
            with self.subTest(sent=str(sent)[:70]):
                got = Production.score_sentiment(sent)
                self.assertEqual(got["total"], ref.sentiment(got))
                self.assertTrue(0 <= got["total"] <= 10)
        high = Production.score_sentiment(cases[2])
        self.assertEqual(high["w_news"], 0.30)
        self.assertEqual(high["w_analyst"], 0.40)

    def test_internals_both_term_structure_weight_sets(self):
        cases = [
            ({}, None, None),
            ({"w52_position": 0.7}, None, None),
            ({"pcr": 0.3, "pcr_available": True,
              "insider_score_raw": 8.0, "insider_available": True,
              "w52_position": 0.75,
              "short_score_raw": 9, "short_ratio": 1,
              "sector_etf_score": 8.0},
             {"spread_bps": 100}, 18),
            ({"pcr": 1.6, "pcr_available": True,
              "insider_score_raw": 2, "insider_available": True,
              "w52_position": 0.10, "short_score_raw": 1.5,
              "short_ratio": 16, "sector_etf_score": 3,
              "vix3m_vix_ratio": 0.85},
             {"spread_bps": -80}, 42),
            ({"vix3m_vix_ratio": 1.16}, None, None),
        ]
        for internals, yc, vix in cases:
            with self.subTest(internals=internals):
                got = Production.score_market_internals(internals, yc, vix)
                raw, total = ref.internals(got)
                self.assertEqual(got["raw_total"], raw)
                self.assertEqual(got["total"], total)


class IndependentComposite(unittest.TestCase):
    def verify_case(self, layers, l5, l6, l7, regime, confidence,
                    book=0.0, pool=0.0, basket=0.0):
        weights = regime_dict(regime)
        parts = Production._composite_components(
            *layers, l5=l5, l6=l6, l7=l7, regime_weights=weights)
        expected = ref.composite(layers, l5=l5, l6=l6, l7=l7, regime=regime)
        for key in ("weighted", "linear", "l7_bonus"):
            self.assertEqual(parts[key], expected[key], (key, layers, regime))
        # Two mathematically identical evaluation orders can straddle an
        # x.xx5 binary-float tie when the contrast stretch is rounded.
        # The reference remains independent; permit at most one display
        # hundredth, and never relax the intermediate component assertions.
        self.assertLessEqual(
            abs(parts["stretched"] - expected["stretched"]),
            0.0100000001, (layers, regime, l5, l6, l7))
        actual_weights = tuple(x["weight"] for x in parts["layers"])
        for observed, target in zip(actual_weights, expected["weights"]):
            self.assertAlmostEqual(observed, target, places=4)
        self.assertAlmostEqual(sum(actual_weights), 1.0, places=4)
        self.assertEqual(Production.composite(
            *layers, l5=l5, l6=l6, l7=l7,
            regime_weights=weights), parts["stretched"])

        want_quality, want_decision = ref.decision(
            layers, l5=l5, l6=l6, l7=l7, regime=regime,
            coverage=confidence, book=book, pool=pool, basket=basket)
        kw = dict(institutional_l5=l5, l6=l6, l7=l7,
                  regime_weights=weights, data_confidence=confidence,
                  book_bonus=book, pool_boost=pool, basket_boost=basket)
        actual_quality, actual_decision = Production._compute_decision_scores(*layers, **kw)
        self.assertLessEqual(abs(actual_quality - want_quality), 0.0100000001)
        self.assertLessEqual(abs(actual_decision - want_decision), 0.0100000001)
        explanation = Production._score_breakdown(
            *layers, age_hours=12.5, **kw)
        self.assertEqual(explanation["quality"], actual_quality)
        self.assertEqual(explanation["decision"], actual_decision)
        self.assertEqual(explanation["steps"][-1]["value"], actual_decision)
        self.assertEqual(
            tuple(round(x["contribution"], 2) for x in parts["layers"]),
            tuple(round(x["score"] * x["weight"] * 10, 2)
                  for x in parts["layers"]),
        )
        self.assertFalse(any(s.get("label") == "Signal age" and
                             "value" in s for s in explanation["steps"]))

    def test_all_regimes_and_optional_layer_combinations(self):
        for regime in ref.REGIMES:
            for l5 in (None, 0, 7):
                for l6 in (None, 0, 8):
                    for l7 in (None, 0, 5, 7.5, 10):
                        with self.subTest(regime=regime,l5=l5,l6=l6,l7=l7):
                            self.verify_case(
                                (6.5, 8.0, 3.0, 9.0), l5, l6, l7,
                                regime, 0.92, book=1.75, pool=2, basket=3)

    def test_deterministic_randomized_score_audit(self):
        rng = random.Random(17019)
        regimes = list(ref.REGIMES)
        for i in range(280):
            layers = tuple(round(rng.uniform(0, 10), 4) for _ in range(4))
            l5 = None if i % 3 == 0 else round(rng.uniform(0, 10), 2)
            l6 = None if i % 5 == 0 else round(rng.uniform(0, 10), 2)
            l7 = None if i % 4 == 0 else round(rng.uniform(0, 10), 2)
            self.verify_case(
                layers, l5, l6, l7, regimes[i % 4],
                rng.choice([None, 0, 0.30, 0.85, 0.97, 1.0]),
                book=rng.choice([0, 0.5, 3]),
                pool=rng.choice([0, 2, 4]),
                basket=rng.choice([0, 1, 5]))

    def test_equal_neutral_layers_stay_neutral_without_modifiers(self):
        for regime in ref.REGIMES:
            self.verify_case((5, 5, 5, 5), None, None, None, regime, 1.0)
            self.assertEqual(ref.composite((5, 5, 5, 5), regime=regime)["stretched"],50)

    def test_monotonic_composite_per_layer_below_saturation(self):
        for regime in ref.REGIMES:
            weights = regime_dict(regime)
            for changed_index in range(4):
                values = []
                for score in (0, 2, 4, 6, 8, 10):
                    base = [5, 5, 5, 5]
                    base[changed_index] = score
                    values.append(Production.composite(*base, regime_weights=weights))
                self.assertEqual(values, sorted(values),
                                 (regime, changed_index, values))

    def test_held_score_confidence_is_never_an_improvement(self):
        layers = (8, 7, 9, 6)
        for conf in (0, 0.5, 0.85, 0.9, 0.99, 1):
            quality, decision = Production._compute_decision_scores(
                *layers, institutional_l5=None, data_confidence=conf)
            self.assertLessEqual(decision, quality)
        self.assertEqual(
            Production._compute_decision_scores(
                *layers, institutional_l5=None, data_confidence=0)[1],
            Production._compute_decision_scores(
                *layers, institutional_l5=None, data_confidence=0.85)[1])

    def test_no_hidden_score_modifiers_are_in_the_explanation(self):
        kw = {"institutional_l5": 7.5, "l6": 8, "l7": 9,
              "book_bonus": 1, "pool_boost": 2, "basket_boost": 3,
              "data_confidence": 0.93}
        explanation = Production._score_breakdown(8, 7, 6, 9, **kw)
        names = [step["label"] for step in explanation["steps"]]
        for required in ("Weighted layers", "L7 conviction bonus",
                         "Contrast stretch", "Book context", "Quality score",
                         "Confidence demotion", "Pre-screen pool boost",
                         "Smallcase basket boost", "Decision score"):
            self.assertIn(required, names)


if __name__ == "__main__":
    unittest.main()
