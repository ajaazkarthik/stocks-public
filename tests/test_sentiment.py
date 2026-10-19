"""Tests for tradingview/sentiment.py — centralized sentiment engine."""
import sys
from pathlib import Path

# Ensure tradingview/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingview"))

import pytest


class TestGetScorer:
    """Test the get_scorer() factory function."""

    def test_vader_returns_callable(self):
        from tradingview.sentiment import get_scorer
        scorer = get_scorer("vader")
        assert callable(scorer)

    def test_vader_positive_headline(self):
        from tradingview.sentiment import get_scorer
        scorer = get_scorer("vader")
        score = scorer("Stock surges 15% after strong earnings beat")
        assert isinstance(score, float)
        assert -1.0 <= score <= 1.0
        assert score > 0  # should be positive

    def test_vader_negative_headline(self):
        from tradingview.sentiment import get_scorer
        scorer = get_scorer("vader")
        score = scorer("Company misses earnings estimates by wide margin")
        assert score < 0  # should be negative

    def test_vader_compound_range(self):
        from tradingview.sentiment import get_scorer
        scorer = get_scorer("vader")
        for text in ["good", "bad", "neutral statement about market"]:
            score = scorer(text)
            assert -1.0 <= score <= 1.0

    def test_unknown_backend_raises(self):
        from tradingview.sentiment import get_scorer
        with pytest.raises(ValueError, match="Unknown sentiment backend"):
            get_scorer("nonexistent")

    def test_scorer_is_cached(self):
        from tradingview.sentiment import get_scorer, _cached_scorers
        _cached_scorers.clear()
        s1 = get_scorer("vader")
        s2 = get_scorer("vader")
        assert s1 is s2  # same instance (cached)

    def test_case_insensitive_backend(self):
        from tradingview.sentiment import get_scorer
        scorer = get_scorer("VADER")
        assert callable(scorer)


class TestGetBatchScorer:
    """Test batch scoring."""

    def test_vader_batch_returns_list(self):
        from tradingview.sentiment import get_batch_scorer
        batch = get_batch_scorer("vader")
        results = batch(["good news", "bad news", "neutral"])
        assert isinstance(results, list)
        assert len(results) == 3

    def test_vader_batch_empty(self):
        from tradingview.sentiment import get_batch_scorer
        batch = get_batch_scorer("vader")
        assert batch([]) == []

    def test_unknown_batch_backend_raises(self):
        from tradingview.sentiment import get_batch_scorer
        with pytest.raises(ValueError, match="Unknown backend"):
            get_batch_scorer("nonexistent")


class TestFinBERTIntegration:
    """Test FinBERT backend (requires transformers + torch)."""

    @pytest.fixture(autouse=True)
    def _check_deps(self):
        pytest.importorskip("transformers")
        pytest.importorskip("torch")

    def test_finbert_returns_callable(self):
        from tradingview.sentiment import get_scorer
        scorer = get_scorer("finbert")
        assert callable(scorer)

    def test_finbert_score_range(self):
        from tradingview.sentiment import get_scorer
        scorer = get_scorer("finbert")
        score = scorer("Apple reports record revenue beating expectations")
        assert isinstance(score, float)
        assert -1.0 <= score <= 1.0

    def test_finbert_more_accurate_than_vader_on_financial(self):
        """FinBERT should correctly classify headlines that VADER gets wrong."""
        from tradingview.sentiment import get_scorer
        vader = get_scorer("vader")
        finbert = get_scorer("finbert")

        # Headlines where VADER historically fails:
        test_cases = [
            ("Revenue declines for third consecutive quarter", "negative"),
            ("Guidance lowered due to macroeconomic headwinds", "negative"),
            ("Profit warning issued ahead of quarterly results", "negative"),
        ]

        vader_correct = 0
        finbert_correct = 0
        for text, expected in test_cases:
            v_score = vader(text)
            f_score = finbert(text)
            if (expected == "negative" and v_score < -0.05) or \
               (expected == "positive" and v_score > 0.05):
                vader_correct += 1
            if (expected == "negative" and f_score < -0.05) or \
               (expected == "positive" and f_score > 0.05):
                finbert_correct += 1

        # FinBERT should get at least as many right as VADER
        assert finbert_correct >= vader_correct

    def test_finbert_batch_scorer(self):
        from tradingview.sentiment import get_batch_scorer
        batch = get_batch_scorer("finbert")
        results = batch(["good earnings", "bad outlook"])
        assert len(results) == 2
        assert all(isinstance(s, float) for s in results)


class TestScoreBoth:
    """Test dual scoring (VADER + FinBERT together)."""

    @pytest.fixture(autouse=True)
    def _check_deps(self):
        pytest.importorskip("transformers")
        pytest.importorskip("torch")

    def test_score_both_returns_both_keys(self):
        from tradingview.sentiment import score_both
        result = score_both("Amazon earnings beat expectations")
        assert "vader" in result
        assert "finbert" in result
        assert isinstance(result["vader"], float)
        assert isinstance(result["finbert"], float)

    def test_score_both_positive_headline(self):
        from tradingview.sentiment import score_both
        result = score_both("Record revenue growth drives stock higher")
        assert result["finbert"] > 0
        # VADER may or may not agree — just check it's a valid score
        assert -1.0 <= result["vader"] <= 1.0

    def test_score_both_empty_text(self):
        from tradingview.sentiment import score_both
        result = score_both("")
        assert result["vader"] == 0.0
        assert result["finbert"] == 0.0


class TestSignalStackSentimentFlag:
    """Verify signal_stack.py respects --sentiment flag."""

    @pytest.fixture(autouse=True)
    def _restore_backend(self):
        """Restore signal_stack module state after each test to prevent pollution."""
        from tradingview import signal_stack
        orig_backend = signal_stack._SENTIMENT_BACKEND
        orig_dual = signal_stack._USE_DUAL_SCORES
        yield
        signal_stack._SENTIMENT_BACKEND = orig_backend
        signal_stack._USE_DUAL_SCORES = orig_dual

    def test_set_sentiment_backend_vader(self):
        from tradingview.signal_stack import _set_sentiment_backend
        _set_sentiment_backend("vader", dual_scores=False)
        from tradingview import signal_stack
        assert signal_stack._SENTIMENT_BACKEND == "vader"
        assert signal_stack._USE_DUAL_SCORES is False

    def test_set_sentiment_backend_both(self):
        from tradingview.signal_stack import _set_sentiment_backend
        _set_sentiment_backend("both", dual_scores=True)
        from tradingview import signal_stack
        assert signal_stack._SENTIMENT_BACKEND == "both"
        assert signal_stack._USE_DUAL_SCORES is True

    def test_set_sentiment_backend_finbert(self):
        from tradingview.signal_stack import _set_sentiment_backend
        _set_sentiment_backend("finbert", dual_scores=False)
        from tradingview import signal_stack
        assert signal_stack._SENTIMENT_BACKEND == "finbert"
        assert signal_stack._USE_DUAL_SCORES is False
