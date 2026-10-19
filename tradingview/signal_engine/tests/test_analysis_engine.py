"""
Unit tests for AnalysisEngine (L1-L4 scoring, TV symbol mapping, meta parsing).

All tests are pure-math / string-parsing.  NO external network calls.
Run with:  python -m pytest signal_engine/tests/ -v
        or python -m unittest signal_engine/tests/test_analysis_engine.py
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Make sure tradingview/ is on the path so the package imports work
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from signal_engine.analysis_engine import AnalysisEngine


class TestTvToYf(unittest.TestCase):
    """AnalysisEngine.tv_to_yf — exchange:ticker → yfinance ticker string."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_nse_suffix(self):
        self.assertEqual(self.eng.tv_to_yf("NSE:TITAN"), "TITAN.NS")

    def test_bse_suffix(self):
        self.assertEqual(self.eng.tv_to_yf("BSE:RELIANCE"), "RELIANCE.BO")

    def test_asx_suffix(self):
        self.assertEqual(self.eng.tv_to_yf("ASX:CBA"), "CBA.AX")

    def test_no_exchange(self):
        """Bare ticker without 'EXCHANGE:' prefix is returned unchanged."""
        self.assertEqual(self.eng.tv_to_yf("AAPL"), "AAPL")

    def test_nasdaq(self):
        self.assertEqual(self.eng.tv_to_yf("NASDAQ:AAPL"), "AAPL")


class TestParseFireMeta(unittest.TestCase):
    """AnalysisEngine.parse_fire_meta — extract numeric fields from alert text."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_standard_message(self):
        msg = ("BUY TITAN price: 3450.50, WinRate: 63.50%, NetProfit%: 22.10, "
               "sqzOff: 1.0, rsi: 66.20, ADX: 28.50")
        meta = self.eng.parse_fire_meta(msg)
        self.assertAlmostEqual(meta["WinRate"],     63.5)
        self.assertAlmostEqual(meta["NetProfit%"],  22.1)
        self.assertAlmostEqual(meta["rsi"],         66.2)
        self.assertAlmostEqual(meta["ADX"],         28.5)
        self.assertAlmostEqual(meta["sqzOff"],      1.0)
        self.assertAlmostEqual(meta["trigger_price"], 3450.50)

    def test_missing_fields_give_empty_dict(self):
        """A message with no matching patterns should return {}."""
        meta = self.eng.parse_fire_meta("This is just a plain string.")
        self.assertNotIn("WinRate",  meta)
        self.assertNotIn("rsi",      meta)
        self.assertNotIn("trigger_price", meta)

    def test_percent_stripped(self):
        """Trailing '%' must be removed before float conversion."""
        meta = self.eng.parse_fire_meta("WinRate: 45.00%, NetProfit%: 12%")
        self.assertAlmostEqual(meta["WinRate"],    45.0)
        self.assertAlmostEqual(meta["NetProfit%"], 12.0)


class TestScoreTechnical(unittest.TestCase):
    """AnalysisEngine.score_technical — L1 score correctness."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_output_keys(self):
        meta  = {"WinRate": 65, "NetProfit%": 25, "rsi": 65, "sqzOff": 1.0, "ADX": 30}
        score = self.eng.score_technical(meta)
        for key in ("total", "wr", "np", "rsi", "squeeze", "adx"):
            self.assertIn(key, score)

    def test_total_in_range(self):
        """Score must always be 0–10."""
        meta  = {"WinRate": 45, "NetProfit%": 10, "rsi": 68, "sqzOff": 0, "ADX": 20}
        score = self.eng.score_technical(meta)
        self.assertGreaterEqual(score["total"], 0.0)
        self.assertLessEqual(   score["total"], 10.0)

    def test_high_quality_signal(self):
        """WinRate 80%, NP 50%, RSI 65, sqzOff, ADX 35 → should exceed 7.0."""
        meta  = {"WinRate": 80, "NetProfit%": 50, "rsi": 65, "sqzOff": 1.0, "ADX": 35}
        score = self.eng.score_technical(meta)
        self.assertGreater(score["total"], 7.0,
                           f"Expected > 7.0, got {score['total']}")

    def test_low_quality_signal(self):
        """WinRate 20%, NP 0%, RSI < 55, no squeeze, low ADX → should be below 3.0."""
        meta  = {"WinRate": 20, "NetProfit%": 0, "rsi": 50, "sqzOff": 0, "ADX": 14}
        score = self.eng.score_technical(meta)
        self.assertLess(score["total"], 3.0,
                        f"Expected < 3.0, got {score['total']}")

    def test_rel_vol_changes_total(self):
        """When screener supplies rel_vol, the total should differ from without."""
        meta  = {"WinRate": 55, "NetProfit%": 18, "rsi": 64, "sqzOff": 1.0, "ADX": 25}
        s_no  = self.eng.score_technical(meta, screener=None)
        s_yes = self.eng.score_technical(meta, screener={"rel_vol": 3.5})
        self.assertNotAlmostEqual(s_no["total"], s_yes["total"], places=2)


class _FakeUrlResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class TestFetchTvScreener(unittest.TestCase):
    """AnalysisEngine.fetch_tv_screener — preserve exchange-specific symbol mapping."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_same_ticker_on_different_exchanges_keeps_both_results(self):
        def _fake_urlopen(req, timeout=10):
            url = req.full_url
            if "america/scan" in url:
                return _FakeUrlResponse({
                    "data": [{
                        "s": "NYSE:ABC",
                        "d": ["ABC", 1.5, 2.0, 100.0, 105.0, 20.0, 60.0, 1.0, 0.5, 100000, 2.0, 1.1],
                    }]
                })
            if "australia/scan" in url:
                return _FakeUrlResponse({
                    "data": [{
                        "s": "ASX:ABC",
                        "d": ["ABC", 3.0, 1.5, 50.0, 52.0, 25.0, 65.0, 0.4, 0.1, 200000, 1.0, 0.8],
                    }]
                })
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("signal_stack.urllib.request.urlopen", side_effect=_fake_urlopen):
            data = self.eng.fetch_tv_screener(["NYSE:ABC", "ASX:ABC"])

        self.assertEqual(set(data), {"NYSE:ABC", "ASX:ABC"})
        self.assertEqual(data["NYSE:ABC"]["rel_vol"], 1.5)
        self.assertEqual(data["ASX:ABC"]["rel_vol"], 3.0)


class TestScoreFundamental(unittest.TestCase):
    """AnalysisEngine.score_fundamental — L2 score correctness."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_empty_fund_returns_neutral(self):
        """Empty dict → all sub-scores default to neutral (5.0) → total ≈ 5.0."""
        s = self.eng.score_fundamental({})
        # All unknowns default to 5.0; composite is ~5.0
        self.assertIn("total", s)
        self.assertAlmostEqual(s["total"], 5.0, delta=0.5)

    def test_strong_fundamentals_score_high(self):
        fund = {
            "forward_pe":   12,    # good: low P/E
            "peg_ratio":    0.8,   # good: PEG < 1
            "roe":          22,    # good: high ROE
            "profit_margin": 18,   # good: high margin
            "revenue_growth": 20,  # good: strong growth
            "debt_to_equity": 20,  # good: low debt
        }
        s = self.eng.score_fundamental(fund)
        self.assertGreater(s["total"], 6.5,
                           f"Expected > 6.5, got {s['total']}")

    def test_weak_fundamentals_score_low(self):
        fund = {
            "forward_pe":    80,   # bad: very high P/E
            "peg_ratio":     5.0,  # bad: very high PEG
            "roe":          -10,   # bad: negative ROE
            "profit_margin": -5,   # bad: negative margin
            "revenue_growth": -20, # bad: revenue shrinking
            "debt_to_equity": 400, # bad: very high debt
        }
        s = self.eng.score_fundamental(fund)
        self.assertLess(s["total"], 3.5,
                        f"Expected < 3.5, got {s['total']}")


class TestComposite(unittest.TestCase):
    """AnalysisEngine.composite — weighted 0-100 composite score."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_all_ten_gives_hundred(self):
        """When all layers are perfect (10), composite should be 100."""
        self.assertAlmostEqual(
            self.eng.composite(10, 10, 10, 10), 100.0, places=1
        )

    def test_all_zero_gives_zero(self):
        self.assertAlmostEqual(
            self.eng.composite(0, 0, 0, 0), 0.0, places=1
        )

    def test_baseline_weights(self):
        # weights 0.30/0.30/0.20/0.20 + score stretch: 50+(linear-50)*1.5
        raw = (0.30 * 8 + 0.30 * 6 + 0.20 * 5 + 0.20 * 4) * 10
        expected = round(min(100.0, max(0.0, 50.0 + (raw - 50.0) * 1.5)), 2)
        self.assertAlmostEqual(
            self.eng.composite(8, 6, 5, 4), expected, places=1
        )

    def test_result_in_range(self):
        import random
        for _ in range(20):
            l1, l2, l3, l4 = (random.uniform(0, 10) for _ in range(4))
            c = self.eng.composite(l1, l2, l3, l4)
            self.assertGreaterEqual(c,   0.0)
            self.assertLessEqual(   c, 100.0)


class TestGrade(unittest.TestCase):
    """AnalysisEngine.grade — A/B/C/D/F + hex colour for a 0-100 score."""

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_boundaries(self):
        cases = [
            (75, "A"),
            (60, "B"),
            (45, "C"),
            (30, "D"),
            (10, "F"),
        ]
        for score, expected_grade in cases:
            letter, colour = self.eng.grade(score)
            self.assertEqual(letter, expected_grade,
                             f"grade({score}) → expected {expected_grade}, got {letter}")

    def test_colour_is_hex(self):
        for score in (10, 35, 50, 65, 80):
            letter, colour = self.eng.grade(score)
            self.assertTrue(colour.startswith("#"),
                            f"grade({score}) colour {colour!r} is not a hex string")


# ─── resolve_etf_underlying ───────────────────────────────────────────────────

class TestResolveEtfUnderlying(unittest.TestCase):
    """
    AnalysisEngine.resolve_etf_underlying — identifies index ETFs vs ordinary stocks.
    Returns ("INDEX", "") for Indian BEES ETFs; (None, None) for everything else.
    """

    def setUp(self):
        self.eng = AnalysisEngine()

    def test_nifty_bees_is_index_etf(self):
        result = self.eng.resolve_etf_underlying("NSE:NIFTYBEES")
        self.assertEqual(result, ("INDEX", ""))

    def test_bank_bees_is_index_etf(self):
        result = self.eng.resolve_etf_underlying("NSE:BANKBEES")
        self.assertEqual(result, ("INDEX", ""))

    def test_arbitrary_bees_suffix_is_index_etf(self):
        """Any ticker ending in BEES should be treated as an index ETF."""
        result = self.eng.resolve_etf_underlying("NSE:PSUBNKBEES")
        self.assertEqual(result, ("INDEX", ""))

    def test_ordinary_stock_returns_none_none(self):
        result = self.eng.resolve_etf_underlying("NASDAQ:AAPL")
        self.assertEqual(result, (None, None))

    def test_spy_returns_none_none(self):
        """SPY (S&P 500 tracker) is not in the BEES list → (None, None)."""
        result = self.eng.resolve_etf_underlying("AMEX:SPY")
        self.assertEqual(result, (None, None))

    def test_nse_regular_stock(self):
        result = self.eng.resolve_etf_underlying("NSE:TITAN")
        self.assertEqual(result, (None, None))

    def test_returns_tuple(self):
        result = self.eng.resolve_etf_underlying("NASDAQ:TQQQ")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)


# ─── analyse_signal (integration — all network mocked) ────────────────────────

class TestAnalyseSignalMocked(unittest.TestCase):
    """
    Full analyse_signal pipeline with all external I/O patched.
    Tests orchestration, output shape, and composite range.
    """

    def _fire(self, sym: str = "NASDAQ:AAPL") -> dict:
        return {
            "pro_symbol": sym,
            "symbol":     sym,
            "resolution": "60",
            "message":    f"BUY {sym.split(':')[-1]} price: 150.00, WinRate: 70.00%",
            "fire_time":  "2026-01-01T10:00:00+00:00",
        }

    def _run_with_mocks(self, fire: dict) -> dict:
        engine = AnalysisEngine()
        with (
            patch.object(engine, "fetch_tv_screener",    return_value={}),
            patch.object(engine, "fetch_tv_fundamentals", return_value={}),
            patch.object(engine, "tv_fund_to_compat",    return_value={}),
            patch.object(engine, "fetch_sentiment_data", return_value={}),
            patch.object(engine, "fetch_yield_curve",    return_value={}),
            patch.object(engine, "fetch_market_internals", return_value={}),
            patch.object(engine, "score_technical",      return_value={"total": 6.0}),
            patch.object(engine, "score_fundamental",    return_value={"total": 5.0}),
            patch.object(engine, "score_sentiment",      return_value={"total": 5.0}),
            patch.object(engine, "score_market_internals", return_value={"total": 5.0}),
        ):
            import unittest.mock as _mock
            with _mock.patch("signal_engine.analysis_engine.yf") as mock_yf:
                mock_yf.Ticker.return_value = _mock.MagicMock()
                return engine.analyse_signal(fire)

    def test_result_has_tv_symbol(self):
        result = self._run_with_mocks(self._fire())
        self.assertEqual(result["tv_symbol"], "NASDAQ:AAPL")

    def test_result_has_composite_in_range(self):
        result = self._run_with_mocks(self._fire())
        self.assertGreaterEqual(result["composite"], 0)
        self.assertLessEqual(result["composite"], 100)

    def test_result_has_required_keys(self):
        result = self._run_with_mocks(self._fire())
        for key in ("tv_symbol", "yf_ticker", "composite", "scores",
                    "fundamentals", "meta", "is_etf"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_scores_dict_has_l1_l2_l3_l4(self):
        result = self._run_with_mocks(self._fire())
        for layer in ("l1", "l2", "l3", "l4"):
            self.assertIn(layer, result["scores"])

    def test_nse_bees_etf_detected(self):
        result = self._run_with_mocks(self._fire("NSE:NIFTYBEES"))
        self.assertTrue(result["is_etf"])

    def test_non_etf_is_etf_false(self):
        result = self._run_with_mocks(self._fire("NASDAQ:AAPL"))
        self.assertFalse(result["is_etf"])

    def test_fetch_sentiment_data_delegates_tv_fund(self):
        engine = AnalysisEngine()
        ticker_obj = MagicMock()
        tv_fund = {"recommendation_mark": 1.4}

        with patch("signal_engine.analysis_engine._ss.fetch_sentiment_data", return_value={}) as mock_fetch:
            engine.fetch_sentiment_data(ticker_obj, "CBA", "ASX:CBA", tv_fund=tv_fund)

        mock_fetch.assert_called_once_with(ticker_obj, "CBA", "ASX:CBA", tv_fund=tv_fund)

    def test_fetch_market_internals_delegates_tv_symbol(self):
        engine = AnalysisEngine()
        ticker_obj = MagicMock()
        fund = {"current_price": 100}

        with patch("signal_engine.analysis_engine._ss.fetch_market_internals", return_value={}) as mock_fetch:
            engine.fetch_market_internals(ticker_obj, fund, "ASX:CBA")

        mock_fetch.assert_called_once_with(ticker_obj, fund, "ASX:CBA")

    def test_analyse_signal_passes_tv_fund_and_symbol_context(self):
        engine = AnalysisEngine()
        fire = self._fire("ASX:CBA")
        raw_tv = {"ASX:CBA": {"recommendation_mark": 1.4, "earnings_release_next_date": 1_800_000_000}}
        fund = {"company_name": "Commonwealth Bank", "sector": "Financial Services", "current_price": 150.0}

        with (
            patch.object(engine, "fetch_tv_screener", return_value={}),
            patch.object(engine, "fetch_tv_fundamentals", return_value=raw_tv),
            patch.object(engine, "tv_fund_to_compat", return_value=fund),
            patch.object(engine, "fetch_sentiment_data", return_value={}) as mock_sent,
            patch.object(engine, "fetch_yield_curve", return_value={}),
            patch.object(engine, "fetch_vix_level", return_value=None),
            patch.object(engine, "fetch_sector_etf_momentum", return_value={}),
            patch.object(engine, "fetch_market_internals", return_value={}) as mock_ints,
            patch.object(engine, "score_technical", return_value={"total": 6.0}),
            patch.object(engine, "score_fundamental", return_value={"total": 5.0}),
            patch.object(engine, "score_sentiment", return_value={"total": 5.0}),
            patch.object(engine, "score_market_internals", return_value={"total": 5.0}),
        ):
            import unittest.mock as _mock
            with _mock.patch("signal_engine.analysis_engine.yf") as mock_yf:
                mock_yf.Ticker.return_value = _mock.MagicMock()
                engine.analyse_signal(fire)

        self.assertEqual(mock_sent.call_args.kwargs["tv_fund"], raw_tv["ASX:CBA"])
        self.assertEqual(mock_ints.call_args.args[2], "ASX:CBA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
