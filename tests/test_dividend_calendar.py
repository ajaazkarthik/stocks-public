"""Tests for F6: Dividend Income Calendar."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.dividend_calendar import (
    load_positions,
    estimate_frequency,
    build_calendar,
    total_projected_income,
    generate_html,
    DividendEntry,
)


def _future(days: int = 30) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def _past(days: int = 30) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. estimate_frequency
# ═══════════════════════════════════════════════════════════════════════════════

class TestEstimateFrequency:
    def test_monthly(self):
        assert estimate_frequency(12) == "monthly"

    def test_quarterly(self):
        assert estimate_frequency(4) == "quarterly"

    def test_semi_annual(self):
        assert estimate_frequency(2) == "semi-annual"

    def test_annual(self):
        assert estimate_frequency(1) == "annual"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. build_calendar
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildCalendar:
    def test_basic(self):
        pos = [{"symbol": "AAPL", "broker": "IBKR", "shares": 100, "currency": "USD"}]
        div = {"AAPL": {"next_ex_date": _future(30), "amount_per_share": 0.25,
                        "dividends_per_year": 4, "dividend_yield": 0.55}}
        entries = build_calendar(pos, div)
        assert len(entries) == 1
        assert entries[0].symbol == "AAPL"
        assert entries[0].projected_income == 25.0

    def test_past_date_excluded(self):
        pos = [{"symbol": "OLD", "broker": "IBKR", "shares": 100}]
        div = {"OLD": {"next_ex_date": _past(10), "amount_per_share": 0.5}}
        assert build_calendar(pos, div) == []

    def test_beyond_horizon_excluded(self):
        pos = [{"symbol": "FAR", "broker": "IBKR", "shares": 100}]
        div = {"FAR": {"next_ex_date": _future(100), "amount_per_share": 0.5}}
        assert build_calendar(pos, div, horizon_days=90) == []

    def test_no_div_data(self):
        pos = [{"symbol": "NONE", "broker": "IBKR", "shares": 100}]
        assert build_calendar(pos) == []

    def test_sorted_by_date(self):
        pos = [
            {"symbol": "B", "broker": "IBKR", "shares": 10},
            {"symbol": "A", "broker": "IBKR", "shares": 10},
        ]
        div = {
            "B": {"next_ex_date": _future(60), "amount_per_share": 1.0},
            "A": {"next_ex_date": _future(20), "amount_per_share": 1.0},
        }
        entries = build_calendar(pos, div)
        assert entries[0].symbol == "A"
        assert entries[1].symbol == "B"

    def test_deduplicate(self):
        pos = [
            {"symbol": "X", "broker": "IBKR", "shares": 10},
            {"symbol": "X", "broker": "Moomoo-AU", "shares": 5},
        ]
        div = {"X": {"next_ex_date": _future(30), "amount_per_share": 1.0}}
        entries = build_calendar(pos, div)
        assert len(entries) == 1

    def test_empty(self):
        assert build_calendar([]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. total_projected_income
# ═══════════════════════════════════════════════════════════════════════════════

class TestTotalProjectedIncome:
    def test_sum(self):
        entries = [
            DividendEntry(symbol="A", broker="X", ex_date="2026-06-01",
                          amount_per_share=1.0, shares=100, projected_income=100),
            DividendEntry(symbol="B", broker="Y", ex_date="2026-06-15",
                          amount_per_share=0.5, shares=200, projected_income=100),
        ]
        assert total_projected_income(entries) == 200

    def test_empty(self):
        assert total_projected_income([]) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_entries(self):
        e = DividendEntry(symbol="AAPL", broker="IBKR", ex_date="2026-06-15",
                          amount_per_share=0.25, shares=100, projected_income=25.0,
                          yield_pct=0.55, frequency="quarterly")
        html = generate_html([e])
        assert "AAPL" in html
        assert "0.55%" in html
        assert "25.00" in html

    def test_empty(self):
        html = generate_html([])
        assert "No upcoming dividends" in html

    def test_written(self, tmp_path):
        out = tmp_path / "div.html"
        generate_html([], out)
        assert out.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. load_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPositions:
    def test_valid(self, tmp_path):
        import json
        f = tmp_path / "pos.json"
        f.write_text('[{"symbol":"X"}]', encoding="utf-8")
        assert len(load_positions(f)) == 1

    def test_missing(self, tmp_path):
        assert load_positions(tmp_path / "nope.json") == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
