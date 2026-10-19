"""Tests for F8: Price Target Tracker."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.price_target_tracker import (
    load_positions,
    calculate_upside,
    classify_recommendation,
    build_targets,
    generate_html,
    PriceTarget,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. calculate_upside
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateUpside:
    def test_positive(self):
        assert calculate_upside(100, 120) == pytest.approx(20.0)

    def test_negative(self):
        assert calculate_upside(100, 80) == pytest.approx(-20.0)

    def test_zero_current(self):
        assert calculate_upside(0, 100) == 0.0

    def test_equal(self):
        assert calculate_upside(100, 100) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. classify_recommendation
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifyRecommendation:
    def test_strong_buy(self):
        assert classify_recommendation(1.0) == "strong buy"

    def test_buy(self):
        assert classify_recommendation(2.0) == "buy"

    def test_hold(self):
        assert classify_recommendation(3.0) == "hold"

    def test_sell(self):
        assert classify_recommendation(4.0) == "sell"

    def test_strong_sell(self):
        assert classify_recommendation(5.0) == "strong sell"

    def test_none(self):
        assert classify_recommendation(None) == "unknown"

    def test_string_input(self):
        assert classify_recommendation("Buy") == "buy"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. build_targets
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildTargets:
    def test_with_screener_data(self):
        pos = [{"symbol": "NVDA", "broker": "IBKR", "current_price": 100}]
        tv = {"NVDA": {"target_price": 150, "number_of_analysts": 30, "recommendation_mark": 2.0}}
        targets = build_targets(pos, tv)
        assert len(targets) == 1
        assert targets[0].upside_pct == pytest.approx(50.0)
        assert targets[0].analyst_count == 30
        assert targets[0].recommendation == "buy"

    def test_no_target_excluded(self):
        pos = [{"symbol": "X", "broker": "IBKR", "current_price": 100}]
        targets = build_targets(pos, {})
        assert len(targets) == 0  # no screener data → no target

    def test_deduplicate(self):
        pos = [
            {"symbol": "NVDA", "broker": "IBKR", "current_price": 100},
            {"symbol": "NVDA", "broker": "Moomoo-AU", "current_price": 100},
        ]
        tv = {"NVDA": {"target_price": 120}}
        targets = build_targets(pos, tv)
        assert len(targets) == 1

    def test_sorted_by_upside(self):
        pos = [
            {"symbol": "LOW", "broker": "IBKR", "current_price": 100},
            {"symbol": "HIGH", "broker": "IBKR", "current_price": 100},
        ]
        tv = {
            "LOW": {"target_price": 110},
            "HIGH": {"target_price": 200},
        }
        targets = build_targets(pos, tv)
        assert targets[0].symbol == "HIGH"  # bigger upside

    def test_empty(self):
        assert build_targets([]) == []

    def test_invalid_target_skipped(self):
        pos = [{"symbol": "BAD", "broker": "IBKR", "current_price": 100}]
        tv = {"BAD": {"target_price": "N/A"}}
        targets = build_targets(pos, tv)
        assert len(targets) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_targets(self):
        t = PriceTarget(symbol="NVDA", current_price=100, target_price=150,
                        upside_pct=50.0, analyst_count=30, recommendation="buy", broker="IBKR")
        html = generate_html([t])
        assert "NVDA" in html
        assert "+50.0%" in html
        assert "buy" in html

    def test_empty(self):
        html = generate_html([])
        assert "No price targets" in html

    def test_written(self, tmp_path):
        out = tmp_path / "targets.html"
        generate_html([], out)
        assert out.exists()

    def test_negative_upside(self):
        t = PriceTarget(symbol="X", current_price=100, target_price=80,
                        upside_pct=-20.0, analyst_count=10, recommendation="sell")
        html = generate_html([t])
        assert "-20.0%" in html


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
