"""Tests for F15: Social Media Mention Spike Detector."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.social_mention_tracker import (
    MentionData,
    MentionSpike,
    compute_velocity,
    detect_spikes,
    build_mention_data,
    generate_html,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. compute_velocity
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeVelocity:
    def test_spike_up(self):
        m = MentionData(symbol="NVDA", daily_counts=[10, 10, 10, 10, 10, 10, 10, 10, 10, 25])
        v = compute_velocity(m, window=10)
        assert v.velocity_pct > 0
        assert v.current_count == 25

    def test_spike_down(self):
        m = MentionData(symbol="NVDA", daily_counts=[50, 50, 50, 50, 50, 50, 50, 50, 50, 10])
        v = compute_velocity(m, window=10)
        assert v.velocity_pct < 0

    def test_stable(self):
        m = MentionData(symbol="AAPL", daily_counts=[10] * 10)
        v = compute_velocity(m, window=10)
        assert abs(v.velocity_pct) < 1

    def test_from_zero(self):
        m = MentionData(symbol="GME", daily_counts=[0, 0, 0, 0, 0, 0, 0, 0, 0, 5])
        v = compute_velocity(m, window=10)
        # avg_10d = 0.5 (includes the 5), so velocity = (5-0.5)/0.5*100 = 900%
        assert v.velocity_pct == pytest.approx(900.0, rel=0.01)

    def test_empty(self):
        m = MentionData(symbol="X", daily_counts=[])
        v = compute_velocity(m)
        assert v.signal == "no_data"

    def test_short_series(self):
        m = MentionData(symbol="X", daily_counts=[10, 20])
        v = compute_velocity(m, window=10)
        assert v.avg_10d > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. detect_spikes
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectSpikes:
    def test_finds_spike(self):
        mentions = [
            MentionData(symbol="GME", daily_counts=[10] * 9 + [20]),  # 100% up
        ]
        result = detect_spikes(mentions, spike_threshold=40)
        assert result[0].is_spike is True
        assert result[0].signal == "spike_up"

    def test_finds_drop(self):
        mentions = [
            MentionData(symbol="X", daily_counts=[100] * 9 + [30]),  # -70%
        ]
        result = detect_spikes(mentions, spike_threshold=40)
        assert result[0].is_spike is True
        assert result[0].signal == "spike_down"

    def test_no_spike(self):
        mentions = [
            MentionData(symbol="AAPL", daily_counts=[10] * 10),
        ]
        result = detect_spikes(mentions, spike_threshold=40)
        assert result[0].is_spike is False
        assert result[0].signal == "normal"

    def test_sorted_by_abs_velocity(self):
        mentions = [
            MentionData(symbol="A", daily_counts=[10] * 9 + [12]),   # +20%
            MentionData(symbol="B", daily_counts=[10] * 9 + [20]),   # +100%
        ]
        result = detect_spikes(mentions)
        assert result[0].symbol == "B"

    def test_empty(self):
        assert detect_spikes([]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. build_mention_data
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildMentionData:
    def test_basic(self):
        pos = [{"symbol": "NVDA"}]
        raw = {"NVDA": [10, 20, 30]}
        result = build_mention_data(pos, raw)
        assert len(result) == 1
        assert result[0].symbol == "NVDA"

    def test_no_data(self):
        pos = [{"symbol": "BHP"}]
        result = build_mention_data(pos, {})
        assert len(result) == 0

    def test_filters_to_portfolio(self):
        pos = [{"symbol": "AAPL"}]
        raw = {"AAPL": [10], "MSFT": [20]}
        result = build_mention_data(pos, raw)
        assert len(result) == 1  # only AAPL


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_spike(self):
        s = MentionSpike(
            symbol="GME", current_count=1000, avg_10d=100, velocity_pct=900,
            is_spike=True, source="reddit", signal="spike_up",
        )
        html = generate_html([s])
        assert "GME" in html
        assert "SPIKE" in html

    def test_empty(self):
        html = generate_html([])
        assert "No mention data" in html

    def test_written(self, tmp_path):
        out = tmp_path / "social.html"
        generate_html([], out)
        assert out.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
