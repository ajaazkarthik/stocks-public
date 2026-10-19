"""Tests for F2: Duplicate Position Detector (Cross-Broker)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview" / "analysis"))

from tradingview.analysis.duplicate_position_detector import (
    normalize_symbol,
    load_positions,
    detect_duplicates,
    portfolio_total_value,
    generate_html,
    DuplicateGroup,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. normalize_symbol
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeSymbol:
    def test_plain_ticker(self):
        assert normalize_symbol("NVDA") == "NVDA"

    def test_asx_suffix(self):
        assert normalize_symbol("BHP.AX") == "BHP"

    def test_nse_suffix(self):
        assert normalize_symbol("INFY.NS") == "INFY"

    def test_bse_suffix(self):
        assert normalize_symbol("RELIANCE.BO") == "RELIANCE"

    def test_hk_suffix(self):
        assert normalize_symbol("0700.HK") == "0700"

    def test_sgx_suffix(self):
        assert normalize_symbol("D05.SI") == "D05"

    def test_exchange_prefix(self):
        assert normalize_symbol("ASX:BHP") == "BHP"

    def test_nasdaq_prefix(self):
        assert normalize_symbol("NASDAQ:AAPL") == "AAPL"

    def test_lowercase_normalized(self):
        assert normalize_symbol("nvda") == "NVDA"

    def test_whitespace_stripped(self):
        assert normalize_symbol("  AAPL  ") == "AAPL"

    def test_empty_string(self):
        assert normalize_symbol("") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. load_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPositions:
    def test_valid_json(self, tmp_path):
        f = tmp_path / "pos.json"
        data = [{"symbol": "NVDA", "broker": "IBKR", "market_val": 10000}]
        f.write_text(json.dumps(data), encoding="utf-8")
        result = load_positions(f)
        assert len(result) == 1
        assert result[0]["symbol"] == "NVDA"

    def test_missing_file(self, tmp_path):
        result = load_positions(tmp_path / "nope.json")
        assert result == []

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{{{invalid", encoding="utf-8")
        result = load_positions(f)
        assert result == []

    def test_non_list_json(self, tmp_path):
        f = tmp_path / "obj.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        result = load_positions(f)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. detect_duplicates
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectDuplicates:
    def test_no_duplicates(self):
        positions = [
            {"symbol": "NVDA", "broker": "IBKR", "market_val": 10000},
            {"symbol": "AAPL", "broker": "Moomoo-AU", "market_val": 5000},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 0

    def test_single_duplicate(self):
        positions = [
            {"symbol": "NVDA", "broker": "IBKR", "market_val": 10000, "shares": 50},
            {"symbol": "NVDA", "broker": "Moomoo-AU", "market_val": 8000, "shares": 40},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 1
        assert dupes[0].symbol == "NVDA"
        assert dupes[0].broker_count == 2
        assert dupes[0].total_value == 18000

    def test_three_broker_duplicate(self):
        positions = [
            {"symbol": "NVDA", "broker": "IBKR", "market_val": 10000, "shares": 50},
            {"symbol": "NVDA", "broker": "Moomoo-AU", "market_val": 8000, "shares": 40},
            {"symbol": "NVDA", "broker": "Webull AU", "market_val": 5000, "shares": 25},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 1
        assert dupes[0].broker_count == 3
        assert set(dupes[0].brokers) == {"IBKR", "Moomoo-AU", "Webull AU"}

    def test_suffix_normalization_catches_dupe(self):
        """BHP and BHP.AX should be detected as the same symbol."""
        positions = [
            {"symbol": "BHP.AX", "broker": "Moomoo-AU", "market_val": 5000, "shares": 100},
            {"symbol": "BHP", "broker": "IBKR", "market_val": 5100, "shares": 100},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 1
        assert dupes[0].symbol == "BHP"

    def test_prefix_normalization_catches_dupe(self):
        """ASX:BHP and BHP should be detected as the same symbol."""
        positions = [
            {"symbol": "ASX:BHP", "broker": "Moomoo-AU", "market_val": 5000, "shares": 100},
            {"symbol": "BHP", "broker": "IBKR", "market_val": 5100, "shares": 100},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 1

    def test_same_broker_not_duplicate(self):
        """Same symbol in same broker is NOT a cross-broker duplicate."""
        positions = [
            {"symbol": "NVDA", "broker": "IBKR", "market_val": 10000, "shares": 50},
            {"symbol": "NVDA", "broker": "IBKR", "market_val": 8000, "shares": 40},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 0

    def test_sorted_by_total_value(self):
        positions = [
            {"symbol": "AAPL", "broker": "IBKR", "market_val": 1000, "shares": 5},
            {"symbol": "AAPL", "broker": "Moomoo-AU", "market_val": 1000, "shares": 5},
            {"symbol": "NVDA", "broker": "IBKR", "market_val": 10000, "shares": 50},
            {"symbol": "NVDA", "broker": "Webull AU", "market_val": 8000, "shares": 40},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 2
        assert dupes[0].symbol == "NVDA"  # higher value first
        assert dupes[1].symbol == "AAPL"

    def test_empty_positions(self):
        assert detect_duplicates([]) == []

    def test_empty_symbol_skipped(self):
        positions = [
            {"symbol": "", "broker": "IBKR", "market_val": 100},
            {"symbol": "", "broker": "Moomoo-AU", "market_val": 200},
        ]
        dupes = detect_duplicates(positions)
        assert len(dupes) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. portfolio_total_value
# ═══════════════════════════════════════════════════════════════════════════════

class TestPortfolioTotalValue:
    def test_sum(self):
        positions = [
            {"market_val": 10000},
            {"market_val": 5000},
            {"market_val": 3000},
        ]
        assert portfolio_total_value(positions) == 18000

    def test_empty(self):
        assert portfolio_total_value([]) == 0

    def test_missing_market_val(self):
        assert portfolio_total_value([{"symbol": "X"}]) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_html_contains_symbol(self):
        g = DuplicateGroup(
            symbol="NVDA",
            positions=[
                {"symbol": "NVDA", "broker": "IBKR", "market_val": 10000, "shares": 50},
                {"symbol": "NVDA", "broker": "Moomoo-AU", "market_val": 8000, "shares": 40},
            ],
        )
        html = generate_html([g], 100000)
        assert "NVDA" in html
        assert "IBKR" in html
        assert "Moomoo-AU" in html

    def test_html_written_to_file(self, tmp_path):
        g = DuplicateGroup(
            symbol="AAPL",
            positions=[
                {"symbol": "AAPL", "broker": "IBKR", "market_val": 5000, "shares": 25},
                {"symbol": "AAPL", "broker": "Zerodha", "market_val": 5000, "shares": 25},
            ],
        )
        out = tmp_path / "report.html"
        generate_html([g], 50000, out)
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "AAPL" in content
        assert "Zerodha" in content

    def test_html_empty_groups(self):
        html = generate_html([], 100000)
        assert "No cross-broker duplicates found" in html

    def test_html_percentage(self):
        g = DuplicateGroup(
            symbol="NVDA",
            positions=[
                {"symbol": "NVDA", "broker": "IBKR", "market_val": 5000, "shares": 25},
                {"symbol": "NVDA", "broker": "Moomoo-AU", "market_val": 5000, "shares": 25},
            ],
        )
        html = generate_html([g], 100000)
        assert "10.0%" in html  # 10000/100000 = 10%

    def test_html_zero_portfolio(self):
        """Should not crash on zero total portfolio."""
        g = DuplicateGroup(
            symbol="X",
            positions=[
                {"symbol": "X", "broker": "A", "market_val": 0, "shares": 0},
                {"symbol": "X", "broker": "B", "market_val": 0, "shares": 0},
            ],
        )
        html = generate_html([g], 0)
        assert "X" in html


# ═══════════════════════════════════════════════════════════════════════════════
# 6. DuplicateGroup properties
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicateGroup:
    def test_broker_count(self):
        g = DuplicateGroup(
            symbol="TEST",
            positions=[
                {"broker": "A", "market_val": 100, "shares": 1},
                {"broker": "B", "market_val": 200, "shares": 2},
                {"broker": "A", "market_val": 300, "shares": 3},  # same broker
            ],
        )
        assert g.broker_count == 2  # unique brokers

    def test_total_value(self):
        g = DuplicateGroup(
            symbol="TEST",
            positions=[
                {"market_val": 100, "shares": 1},
                {"market_val": 200, "shares": 2},
            ],
        )
        assert g.total_value == 300

    def test_total_shares(self):
        g = DuplicateGroup(
            symbol="TEST",
            positions=[
                {"shares": 10, "market_val": 100},
                {"shares": 20, "market_val": 200},
            ],
        )
        assert g.total_shares == 30


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
