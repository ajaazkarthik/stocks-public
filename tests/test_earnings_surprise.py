"""Tests for F4: Earnings Surprise Tracker."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.earnings_surprise import (
    load_positions,
    classify_surprise,
    calculate_l2_bonus,
    build_earnings_data,
    generate_html,
    EarningsRecord,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. classify_surprise
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifySurprise:
    def test_beat(self):
        assert classify_surprise(5.0) == "beat"

    def test_miss(self):
        assert classify_surprise(-3.0) == "miss"

    def test_inline(self):
        assert classify_surprise(0.0) == "inline"

    def test_none(self):
        assert classify_surprise(None) == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. calculate_l2_bonus
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateL2Bonus:
    def test_no_data(self):
        assert calculate_l2_bonus(0, None) == 0

    def test_single_beat(self):
        assert calculate_l2_bonus(1, 5.0) == 1

    def test_two_beats(self):
        assert calculate_l2_bonus(2, 5.0) == 2

    def test_three_beats(self):
        assert calculate_l2_bonus(3, 5.0) == 3

    def test_four_beats(self):
        assert calculate_l2_bonus(4, 5.0) == 3  # capped at 3

    def test_big_beat_bonus(self):
        """Surprise > 10% gets extra +1."""
        assert calculate_l2_bonus(1, 15.0) == 2  # 1 + 1 big beat

    def test_miss_penalty(self):
        assert calculate_l2_bonus(0, -5.0) == -2

    def test_big_miss_still_minus_two(self):
        assert calculate_l2_bonus(0, -20.0) == -2

    def test_zero_consecutive_positive_surprise(self):
        """Positive surprise but 0 consecutive → bonus 0 (no beat streak confirmed)."""
        assert calculate_l2_bonus(0, 5.0) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. build_earnings_data
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildEarningsData:
    def test_basic(self):
        pos = [{"symbol": "NVDA", "broker": "IBKR"}]
        tv = {"NVDA": {"earnings_per_share_surprise_fq": 12.0, "consecutive_beats": 4}}
        records = build_earnings_data(pos, tv)
        assert len(records) == 1
        assert records[0].symbol == "NVDA"
        assert records[0].surprise_pct == 12.0
        assert records[0].l2_bonus == 4  # 3 (4+ consec) + 1 (>10% surprise)

    def test_no_screener_data(self):
        pos = [{"symbol": "X", "broker": "IBKR"}]
        records = build_earnings_data(pos, {})
        assert len(records) == 1
        assert records[0].surprise_pct is None
        assert records[0].l2_bonus == 0

    def test_deduplicate(self):
        pos = [
            {"symbol": "NVDA", "broker": "IBKR"},
            {"symbol": "NVDA", "broker": "Moomoo-AU"},
        ]
        records = build_earnings_data(pos)
        assert len(records) == 1

    def test_sorted_by_bonus(self):
        pos = [
            {"symbol": "MISS", "broker": "IBKR"},
            {"symbol": "BEAT", "broker": "IBKR"},
        ]
        tv = {
            "MISS": {"earnings_per_share_surprise_fq": -5.0, "consecutive_beats": 0},
            "BEAT": {"earnings_per_share_surprise_fq": 8.0, "consecutive_beats": 3},
        }
        records = build_earnings_data(pos, tv)
        assert records[0].symbol == "BEAT"
        assert records[1].symbol == "MISS"

    def test_empty(self):
        assert build_earnings_data([]) == []

    def test_invalid_surprise_value(self):
        pos = [{"symbol": "BAD", "broker": "IBKR"}]
        tv = {"BAD": {"earnings_per_share_surprise_fq": "N/A"}}
        records = build_earnings_data(pos, tv)
        assert records[0].surprise_pct is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_data(self):
        r = EarningsRecord(symbol="NVDA", surprise_pct=12.0, consecutive_beats=4,
                           streak_type="beat", l2_bonus=4, broker="IBKR")
        html = generate_html([r])
        assert "NVDA" in html
        assert "4Q streak" in html

    def test_empty(self):
        html = generate_html([])
        assert "No earnings data" in html

    def test_written(self, tmp_path):
        out = tmp_path / "earnings.html"
        generate_html([], out)
        assert out.exists()

    def test_miss_shown(self):
        r = EarningsRecord(symbol="LOSE", surprise_pct=-5.0, streak_type="miss",
                           l2_bonus=-2, broker="Zerodha")
        html = generate_html([r])
        assert "miss" in html
        assert "LOSE" in html


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
