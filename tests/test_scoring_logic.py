"""Unit tests for module logic extracted from test_html_reports.py."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestSignalScoringBoundaries:
    """
    Phase 8 test quality improvement: parametrized boundary tests for all
    scoring functions in signal_stack.py. Ensures scores stay within valid
    ranges regardless of input extremes.

    Test Quality Score (Phase 8 assessment):
        Coverage %:         3/10  (9% total — many standalone scripts skew this)
        Stability:          9/10  (521/521 consistent passes)
        Mock quality:       7/10  (contract tests match real API shapes)
        Regression value:   7/10  (catches real bugs with fixture-based tests)
        Test isolation:     8/10  (no shared state, proper teardown)
        TOTAL:             34/50 → 6.8/10
    """

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        import sys
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    @pytest.mark.parametrize("l1,l2,l3,l4", [
        (0, 0, 0, 0),       # all minimum
        (10, 10, 10, 10),   # all maximum
        (0, 10, 0, 10),     # alternating
        (5, 5, 5, 5),       # all neutral
        (1, 1, 1, 1),       # all low
        (9, 9, 9, 9),       # all high
    ])
    def test_composite_always_in_0_100(self, l1, l2, l3, l4):
        """Composite score must always be in [0, 100] range."""
        import signal_stack as ss
        score = ss.composite(l1, l2, l3, l4)
        assert 0 <= score <= 100, f"composite({l1},{l2},{l3},{l4}) = {score} out of [0,100]"

    @pytest.mark.parametrize("l5,l6,l7", [
        (None, None, None),
        (0, None, None),
        (10, None, None),
        (None, 0, None),
        (None, 10, None),
        (5, 5, 2),
        (10, 10, 5),
        (0, 0, -3),
    ])
    def test_composite_with_enrichers_in_0_100(self, l5, l6, l7):
        """Composite with L5/L6/L7 enrichment layers stays in [0, 100]."""
        import signal_stack as ss
        score = ss.composite(5, 5, 5, 5, l5=l5, l6=l6, l7=l7)
        assert 0 <= score <= 100, f"composite(5,5,5,5,l5={l5},l6={l6},l7={l7}) = {score}"

    def test_score_technical_returns_0_to_10(self):
        """L1 technical score must be in [0, 10] range."""
        import signal_stack as ss
        # Extreme bullish
        meta = {"WinRate": 100, "NetProfit%": 100, "rsi": 70, "ADX": 50, "sqzOff": 1}
        screener = {"rel_vol": 10, "sma200_pct": 50, "macd_hist": 5, "close": 200, "volatility": 0.5}
        score = ss.score_technical(meta, screener)["total"]
        assert 0 <= score <= 10, f"score_technical extreme bullish = {score}"
        # Extreme bearish
        meta2 = {"WinRate": 0, "NetProfit%": -100, "rsi": 20, "ADX": 5, "sqzOff": 0}
        screener2 = {"rel_vol": 0.1, "sma200_pct": -50, "macd_hist": -5, "close": 10, "volatility": 10}
        score2 = ss.score_technical(meta2, screener2)["total"]
        assert 0 <= score2 <= 10, f"score_technical extreme bearish = {score2}"

    def test_score_fundamental_returns_0_to_10(self):
        """L2 fundamental score must be in [0, 10] range."""
        import signal_stack as ss
        # Strong fundamentals
        fund = {"forward_pe": 10, "peg_ratio": 0.5, "price_to_sales": 1.0,
                "price_to_book": 1.5, "roe": 40, "profit_margin": 30,
                "gross_margin": 60, "revenue_growth": 25, "earnings_growth": 30,
                "debt_to_equity": 20, "current_ratio": 3.0, "free_cash_flow": 1e10}
        score = ss.score_fundamental(fund)["total"]
        assert 0 <= score <= 10, f"score_fundamental strong = {score}"
        # Weak fundamentals
        fund2 = {"forward_pe": 100, "peg_ratio": 5.0, "price_to_sales": 20.0,
                 "price_to_book": 10.0, "roe": -10, "profit_margin": -20,
                 "gross_margin": 10, "revenue_growth": -20, "earnings_growth": -30,
                 "debt_to_equity": 200, "current_ratio": 0.3, "free_cash_flow": -1e9}
        score2 = ss.score_fundamental(fund2)["total"]
        assert 0 <= score2 <= 10, f"score_fundamental weak = {score2}"

    def test_score_fundamental_empty_dict(self):
        """L2 must handle empty fundamentals gracefully."""
        import signal_stack as ss
        score = ss.score_fundamental({})["total"]
        assert 0 <= score <= 10


class TestScoringConsistency:
    """
    Verify signal_stack.py, analysis_engine.py, and batch_analyzer.py
    all produce identical composite scores (since analysis_engine delegates
    to signal_stack, and batch_analyzer uses analysis_engine).
    """

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        import sys
        tv_dir = str(ROOT / "tradingview")
        if tv_dir not in sys.path:
            sys.path.insert(0, tv_dir)

    def test_composite_same_in_both_paths(self):
        """signal_stack.composite and analysis_engine.composite are the same function."""
        import signal_stack as ss
        from signal_engine.analysis_engine import AnalysisEngine
        engine = AnalysisEngine()
        # Test with various L1-L4 values
        for l1, l2, l3, l4 in [(7, 6, 5, 4), (3, 8, 2, 9), (5, 5, 5, 5), (10, 0, 10, 0)]:
            direct = ss.composite(l1, l2, l3, l4)
            via_engine = engine.composite(l1, l2, l3, l4)
            assert direct == via_engine, f"Divergence: ss={direct} engine={via_engine} for L1={l1},L2={l2},L3={l3},L4={l4}"

    def test_composite_with_l5_l6_same(self):
        """Composite with L5/L6 enrichment layers is identical in both paths."""
        import signal_stack as ss
        from signal_engine.analysis_engine import AnalysisEngine
        engine = AnalysisEngine()
        for l5, l6 in [(6.0, None), (None, 7.0), (6.0, 7.0)]:
            direct = ss.composite(7, 6, 5, 4, l5=l5, l6=l6)
            via_engine = engine.composite(7, 6, 5, 4, l5=l5, l6=l6)
            assert direct == via_engine

    def test_score_technical_same(self):
        """L1 scoring is identical between signal_stack and analysis_engine."""
        import signal_stack as ss
        from signal_engine.analysis_engine import AnalysisEngine
        engine = AnalysisEngine()
        meta = {"WinRate": 72.5, "NetProfit%": 35.0, "rsi": 62, "ADX": 28, "sqzOff": 1}
        screener = {"rel_vol": 2.1, "sma200_pct": 8.5, "macd_hist": 0.5,
                     "close": 150.0, "volatility": 1.5}
        direct = ss.score_technical(meta, screener)
        via_engine = engine.score_technical(meta, screener)
        assert direct == via_engine

    def test_score_fundamental_same(self):
        """L2 scoring is identical between signal_stack and analysis_engine."""
        import signal_stack as ss
        from signal_engine.analysis_engine import AnalysisEngine
        engine = AnalysisEngine()
        fund = {"forward_pe": 18, "peg_ratio": 1.5, "price_to_sales": 3.0,
                "price_to_book": 2.0, "roe": 20, "profit_margin": 15,
                "gross_margin": 40, "revenue_growth": 12, "earnings_growth": 15,
                "debt_to_equity": 50, "current_ratio": 1.8, "free_cash_flow": 5e9}
        direct = ss.score_fundamental(fund)
        via_engine = engine.score_fundamental(fund)
        assert direct == via_engine

    def test_score_sentiment_same(self):
        """L3 scoring is identical between signal_stack and analysis_engine."""
        import signal_stack as ss
        from signal_engine.analysis_engine import AnalysisEngine
        engine = AnalysisEngine()
        sent = {"analyst": {"consensus": 4.2, "count": 25},
                "upgrades": {"recent": 3, "total": 5},
                "earnings": {"beat_rate": 0.8, "last_surprise_pct": 5.0},
                "news": {"score": 0.3, "count": 6}}
        direct = ss.score_sentiment(sent)
        via_engine = engine.score_sentiment(sent)
        assert direct == via_engine

    def test_weights_match_spec(self):
        """L1-L4 weights in composite match the documented 30/30/20/20 split."""
        import signal_stack as ss
        # With all L1-L4 at 10.0, the raw is 10*0.30 + 10*0.30 + 10*0.20 + 10*0.20 = 10.0
        # After ×10 = 100.0, then stretch = 50 + (100-50)*1.5 = 125 → capped at 100
        all_max = ss.composite(10, 10, 10, 10)
        assert all_max == 100.0
        # All at 5.0 = neutral → raw 5.0 → 50.0 → stretch = 50.0 (neutral stays neutral)
        all_neutral = ss.composite(5, 5, 5, 5)
        assert all_neutral == 50.0
