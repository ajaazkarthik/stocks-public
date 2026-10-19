"""Tests for F11: Community Sentiment Aggregator."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.community_sentiment import (
    CommunityPost,
    SymbolSentiment,
    compute_sentiment_stats,
    build_community_data,
    calculate_l3_bonus,
    generate_html,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. compute_sentiment_stats
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeSentimentStats:
    def test_bullish(self):
        posts = [
            CommunityPost(source="reddit", symbol="NVDA", title="t", score=0.8),
            CommunityPost(source="reddit", symbol="NVDA", title="t", score=0.6),
        ]
        s = compute_sentiment_stats(posts)
        assert s.avg_sentiment == pytest.approx(0.7, rel=0.01)
        assert s.bullish_pct > 50

    def test_bearish(self):
        posts = [
            CommunityPost(source="stocktwits", symbol="META", title="t", score=-0.5),
            CommunityPost(source="stocktwits", symbol="META", title="t", score=-0.7),
        ]
        s = compute_sentiment_stats(posts)
        assert s.avg_sentiment < 0
        assert s.bearish_pct > 50

    def test_mixed(self):
        posts = [
            CommunityPost(source="reddit", symbol="AAPL", title="t", score=0.5),
            CommunityPost(source="stocktwits", symbol="AAPL", title="t", score=-0.5),
        ]
        s = compute_sentiment_stats(posts)
        assert abs(s.avg_sentiment) < 0.01
        assert s.bullish_pct == pytest.approx(50, rel=0.1)

    def test_crowding_high(self):
        """All posts bullish → high crowding risk."""
        posts = [
            CommunityPost(source="reddit", symbol="GME", title="t", score=0.9)
            for _ in range(10)
        ]
        s = compute_sentiment_stats(posts)
        assert s.crowding_risk == 1.0

    def test_crowding_low(self):
        """Balanced sentiment → low crowding."""
        posts = [
            CommunityPost(source="reddit", symbol="AAPL", title="t", score=0.3),
            CommunityPost(source="reddit", symbol="AAPL", title="t", score=-0.3),
            CommunityPost(source="reddit", symbol="AAPL", title="t", score=0.0),
        ]
        s = compute_sentiment_stats(posts)
        assert s.crowding_risk < 0.5

    def test_empty(self):
        s = compute_sentiment_stats([])
        assert s.post_count == 0

    def test_penalty_reduces_score(self):
        """High crowding should reduce adjusted_score magnitude."""
        posts = [
            CommunityPost(source="reddit", symbol="GME", title="t", score=0.9)
            for _ in range(10)
        ]
        s = compute_sentiment_stats(posts)
        assert abs(s.adjusted_score) < abs(s.avg_sentiment)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. build_community_data
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildCommunityData:
    def test_basic(self):
        pos = [{"symbol": "NVDA", "broker": "IBKR"}]
        posts = {"NVDA": [{"source": "reddit", "title": "Bull!", "score": 0.7}]}
        result = build_community_data(pos, posts)
        assert len(result) == 1
        assert result[0].symbol == "NVDA"

    def test_no_data(self):
        pos = [{"symbol": "BHP"}]
        result = build_community_data(pos, {})
        assert len(result) == 0

    def test_sorted_by_abs_score(self):
        pos = [{"symbol": "NVDA"}, {"symbol": "AAPL"}]
        posts = {
            "NVDA": [{"source": "reddit", "title": "t", "score": 0.2}],
            "AAPL": [{"source": "reddit", "title": "t", "score": -0.8}],
        }
        result = build_community_data(pos, posts)
        assert result[0].symbol == "AAPL"  # higher abs score


# ═══════════════════════════════════════════════════════════════════════════════
# 3. calculate_l3_bonus
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateL3Bonus:
    def test_bullish_low_crowd(self):
        s = SymbolSentiment(symbol="X", post_count=10, avg_sentiment=0.5, crowding_risk=0.3)
        assert calculate_l3_bonus(s) == 2

    def test_bearish(self):
        s = SymbolSentiment(symbol="X", post_count=10, avg_sentiment=-0.5, crowding_risk=0.3)
        assert calculate_l3_bonus(s) == -2

    def test_extreme_crowding_penalty(self):
        s = SymbolSentiment(symbol="X", post_count=10, avg_sentiment=0.5, crowding_risk=0.9)
        # Bullish (+2) but crowded → +2 - 1 = +1
        # Wait: avg>0.3 but crowding>0.5 → no bullish bonus → only crowding penalty
        assert calculate_l3_bonus(s) == -1

    def test_empty(self):
        s = SymbolSentiment(symbol="X", post_count=0)
        assert calculate_l3_bonus(s) == 0

    def test_neutral(self):
        s = SymbolSentiment(symbol="X", post_count=10, avg_sentiment=0.1, crowding_risk=0.3)
        assert calculate_l3_bonus(s) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_data(self):
        s = SymbolSentiment(
            symbol="NVDA", post_count=5, avg_sentiment=0.6,
            bullish_pct=80, bearish_pct=10, neutral_pct=10,
            crowding_risk=0.5, adjusted_score=0.42, sources=["reddit"],
        )
        html = generate_html([s])
        assert "NVDA" in html
        assert "reddit" in html

    def test_empty(self):
        html = generate_html([])
        assert "No community data" in html

    def test_written(self, tmp_path):
        out = tmp_path / "community.html"
        generate_html([], out)
        assert out.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
