"""Tests for F10: 13F Filing Tracker."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.sec_13f_tracker import (
    load_positions,
    build_institutional_data,
    summarize_by_symbol,
    summarize_by_institution,
    generate_html,
    InstitutionalHolding,
    TRACKED_INSTITUTIONS,
)


class TestBuildInstitutionalData:
    def test_match(self):
        pos = [{"symbol": "NVDA", "broker": "IBKR"}]
        filings = {
            "Berkshire Hathaway": [
                {"symbol": "NVDA", "shares": 1000000, "value_usd": 150000000,
                 "filing_date": "2026-02-14", "change_type": "increased", "change_pct": 25.0},
            ]
        }
        result = build_institutional_data(pos, filings)
        assert len(result) == 1
        assert result[0].institution == "Berkshire Hathaway"
        assert result[0].symbol == "NVDA"

    def test_no_match(self):
        pos = [{"symbol": "BHP", "broker": "Moomoo-AU"}]
        filings = {
            "ARK Invest": [
                {"symbol": "TSLA", "shares": 500000, "value_usd": 80000000,
                 "filing_date": "2026-02-14"},
            ]
        }
        result = build_institutional_data(pos, filings)
        assert len(result) == 0

    def test_multiple_institutions(self):
        pos = [{"symbol": "AAPL", "broker": "IBKR"}]
        filings = {
            "Berkshire": [{"symbol": "AAPL", "shares": 900000000, "value_usd": 135000000000, "filing_date": "2026-02-14"}],
            "Vanguard": [{"symbol": "AAPL", "shares": 1200000000, "value_usd": 180000000000, "filing_date": "2026-02-14"}],
        }
        result = build_institutional_data(pos, filings)
        assert len(result) == 2

    def test_sorted_by_value(self):
        pos = [{"symbol": "NVDA"}, {"symbol": "AAPL"}]
        filings = {
            "Inst": [
                {"symbol": "AAPL", "shares": 100, "value_usd": 100},
                {"symbol": "NVDA", "shares": 100, "value_usd": 500},
            ]
        }
        result = build_institutional_data(pos, filings)
        assert result[0].symbol == "NVDA"

    def test_empty(self):
        assert build_institutional_data([]) == []

    def test_no_filings(self):
        pos = [{"symbol": "NVDA"}]
        assert build_institutional_data(pos) == []


class TestSummarizeBySymbol:
    def test_basic(self):
        holdings = [
            InstitutionalHolding(institution="A", symbol="NVDA", shares=100, value_usd=1000, filing_date=""),
            InstitutionalHolding(institution="B", symbol="NVDA", shares=200, value_usd=2000, filing_date=""),
            InstitutionalHolding(institution="A", symbol="AAPL", shares=50, value_usd=500, filing_date=""),
        ]
        by_sym = summarize_by_symbol(holdings)
        assert len(by_sym["NVDA"]) == 2
        assert len(by_sym["AAPL"]) == 1


class TestSummarizeByInstitution:
    def test_basic(self):
        holdings = [
            InstitutionalHolding(institution="Big Fund", symbol="NVDA", shares=100, value_usd=1000, filing_date=""),
            InstitutionalHolding(institution="Big Fund", symbol="AAPL", shares=50, value_usd=500, filing_date=""),
            InstitutionalHolding(institution="Small Fund", symbol="NVDA", shares=10, value_usd=100, filing_date=""),
        ]
        counts = summarize_by_institution(holdings)
        assert counts["Big Fund"] == 2
        assert counts["Small Fund"] == 1


class TestGenerateHtml:
    def test_with_data(self):
        h = InstitutionalHolding(
            institution="Berkshire Hathaway", symbol="NVDA", shares=1000000,
            value_usd=150000000, filing_date="2026-02-14",
            change_type="increased", change_pct=25.0,
        )
        html = generate_html([h])
        assert "NVDA" in html
        assert "Berkshire" in html
        assert "increased" in html

    def test_empty(self):
        html = generate_html([])
        assert "No matching" in html

    def test_written(self, tmp_path):
        out = tmp_path / "13f.html"
        generate_html([], out)
        assert out.exists()


class TestTrackedInstitutions:
    def test_has_berkshire(self):
        assert "Berkshire Hathaway" in TRACKED_INSTITUTIONS

    def test_has_ark(self):
        assert "ARK Invest" in TRACKED_INSTITUTIONS


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
