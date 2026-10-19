"""Tests for F12: Covered Call Scanner."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.covered_call_scanner import (
    estimate_premium,
    suggest_strike,
    scan_covered_calls,
    generate_html,
    CoveredCallCandidate,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. estimate_premium
# ═══════════════════════════════════════════════════════════════════════════════

class TestEstimatePremium:
    def test_positive(self):
        p = estimate_premium(150.0, 70.0, 30)
        assert p > 0

    def test_higher_iv_higher_premium(self):
        p1 = estimate_premium(100.0, 40.0, 30)
        p2 = estimate_premium(100.0, 80.0, 30)
        assert p2 > p1

    def test_zero_price(self):
        assert estimate_premium(0, 50, 30) == 0.0

    def test_zero_iv(self):
        assert estimate_premium(100, 0, 30) == 0.0

    def test_zero_dte(self):
        assert estimate_premium(100, 50, 0) == 0.0

    def test_longer_dte_higher_premium(self):
        p1 = estimate_premium(100, 50, 15)
        p2 = estimate_premium(100, 50, 45)
        assert p2 > p1


# ═══════════════════════════════════════════════════════════════════════════════
# 2. suggest_strike
# ═══════════════════════════════════════════════════════════════════════════════

class TestSuggestStrike:
    def test_otm(self):
        strike = suggest_strike(100.0, 70.0)
        assert strike > 100.0

    def test_high_iv_further_otm(self):
        s1 = suggest_strike(100.0, 50.0)
        s2 = suggest_strike(100.0, 85.0)
        assert s2 > s1

    def test_zero_price(self):
        assert suggest_strike(0, 50) == 0.0

    def test_rounded(self):
        strike = suggest_strike(100.0, 70.0)
        assert (strike * 2) == int(strike * 2)  # multiple of 0.50


# ═══════════════════════════════════════════════════════════════════════════════
# 3. scan_covered_calls
# ═══════════════════════════════════════════════════════════════════════════════

class TestScanCoveredCalls:
    def test_eligible(self):
        pos = [{"symbol": "NVDA", "shares": 200, "current_price": 150,
                "avg_cost": 120, "pnl_pct": 25.0, "broker": "IBKR"}]
        opts = {"NVDA": {"iv_rank": 70, "days_to_earnings": 45}}
        result = scan_covered_calls(pos, opts)
        assert len(result) == 1
        assert result[0].eligible is True
        assert result[0].contracts == 2

    def test_too_few_shares(self):
        pos = [{"symbol": "NVDA", "shares": 50, "current_price": 150,
                "avg_cost": 120, "pnl_pct": 25.0, "broker": "IBKR"}]
        opts = {"NVDA": {"iv_rank": 70, "days_to_earnings": 45}}
        result = scan_covered_calls(pos, opts)
        assert result[0].eligible is False
        assert "shares" in result[0].reason.lower()

    def test_low_iv(self):
        pos = [{"symbol": "NVDA", "shares": 200, "current_price": 150,
                "avg_cost": 120, "pnl_pct": 25.0, "broker": "IBKR"}]
        opts = {"NVDA": {"iv_rank": 20, "days_to_earnings": 45}}
        result = scan_covered_calls(pos, opts)
        assert result[0].eligible is False
        assert "iv rank" in result[0].reason.lower()

    def test_near_earnings(self):
        pos = [{"symbol": "NVDA", "shares": 200, "current_price": 150,
                "avg_cost": 120, "pnl_pct": 25.0, "broker": "IBKR"}]
        opts = {"NVDA": {"iv_rank": 70, "days_to_earnings": 5}}
        result = scan_covered_calls(pos, opts)
        assert result[0].eligible is False
        assert "earnings" in result[0].reason.lower()

    def test_losing_position(self):
        pos = [{"symbol": "NVDA", "shares": 200, "current_price": 100,
                "avg_cost": 150, "pnl_pct": -33.0, "broker": "IBKR"}]
        opts = {"NVDA": {"iv_rank": 70, "days_to_earnings": 45}}
        result = scan_covered_calls(pos, opts)
        assert result[0].eligible is False
        assert "down" in result[0].reason.lower()

    def test_sorted_eligible_first(self):
        pos = [
            {"symbol": "AAPL", "shares": 50, "current_price": 180,
             "avg_cost": 150, "pnl_pct": 20.0, "broker": "IBKR"},
            {"symbol": "NVDA", "shares": 200, "current_price": 150,
             "avg_cost": 120, "pnl_pct": 25.0, "broker": "IBKR"},
        ]
        opts = {"NVDA": {"iv_rank": 70, "days_to_earnings": 45}}
        result = scan_covered_calls(pos, opts)
        assert result[0].eligible is True

    def test_empty(self):
        assert scan_covered_calls([]) == []

    def test_skip_options(self):
        pos = [{"symbol": "NVDA", "shares": 200, "current_price": 150,
                "avg_cost": 120, "pnl_pct": 25.0, "broker": "IBKR",
                "is_option": True}]
        result = scan_covered_calls(pos)
        assert len(result) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_data(self):
        c = CoveredCallCandidate(
            symbol="NVDA", broker="IBKR", shares=200, current_price=150,
            avg_cost=120, pnl_pct=25.0, iv_rank=70, days_to_earnings=45,
            eligible=True, contracts=2, strike_price=160.5, est_premium=340,
        )
        html = generate_html([c])
        assert "NVDA" in html
        assert "160.50" in html

    def test_empty(self):
        html = generate_html([])
        assert "No positions" in html

    def test_written(self, tmp_path):
        out = tmp_path / "cc.html"
        generate_html([], out)
        assert out.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
