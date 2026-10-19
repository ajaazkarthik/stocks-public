"""Tests for F3: portfolio_correlation.py — multi-market support."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TV = ROOT / "tradingview"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TV))
sys.path.insert(0, str(TV / "analysis"))

import portfolio_correlation as pc


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _extract_tickers — multi-market
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTickers:
    """_extract_tickers() should handle US and multi-market modes."""

    def test_us_only_filters_non_us(self):
        positions = [
            {"ticker": "AAPL"},
            {"ticker": "BHP.AX"},
            {"ticker": "INFY.NS"},
        ]
        result = pc._extract_tickers(positions, markets="us")
        assert "AAPL" in result
        assert "BHP.AX" not in result
        assert "INFY.NS" not in result

    def test_all_markets_includes_suffixed(self):
        positions = [
            {"ticker": "AAPL"},
            {"ticker": "BHP.AX"},
            {"ticker": "INFY.NS"},
        ]
        result = pc._extract_tickers(positions, markets="all")
        assert "AAPL" in result
        assert "BHP.AX" in result
        assert "INFY.NS" in result

    def test_all_markets_adds_suffix_from_exchange(self):
        """Non-suffixed tickers with exchange should get suffix added."""
        positions = [
            {"ticker": "BHP", "exchange": "ASX"},
            {"ticker": "INFY", "exchange": "NSE"},
            {"ticker": "D05", "exchange": "SGX"},
        ]
        result = pc._extract_tickers(positions, markets="all")
        assert "BHP.AX" in result
        assert "INFY.NS" in result
        assert "D05.SI" in result

    def test_us_mode_ignores_exchange_suffix(self):
        positions = [{"ticker": "BHP", "exchange": "ASX"}]
        result = pc._extract_tickers(positions, markets="us")
        # BHP without dot passes in US mode, but with exchange it shouldn't add suffix
        assert "BHP" in result  # no dot → passes US filter
        assert "BHP.AX" not in result

    def test_dedup(self):
        positions = [
            {"ticker": "AAPL"},
            {"ticker": "AAPL"},
        ]
        result = pc._extract_tickers(positions, markets="us")
        assert result.count("AAPL") == 1

    def test_empty_positions(self):
        assert pc._extract_tickers([], markets="all") == []

    def test_cap_at_60(self):
        positions = [{"ticker": f"T{i}"} for i in range(100)]
        result = pc._extract_tickers(positions, markets="us")
        assert len(result) <= 60

    def test_yf_ticker_preferred(self):
        positions = [{"yf_ticker": "MSFT", "ticker": "WRONG"}]
        result = pc._extract_tickers(positions, markets="us")
        assert "MSFT" in result
        assert "WRONG" not in result

    def test_hk_suffix(self):
        positions = [{"ticker": "0005", "exchange": "HKEX"}]
        result = pc._extract_tickers(positions, markets="all")
        assert "0005.HK" in result

    def test_bse_suffix(self):
        positions = [{"ticker": "RELIANCE", "exchange": "BSE"}]
        result = pc._extract_tickers(positions, markets="all")
        assert "RELIANCE.BO" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _pearson — correlation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPearson:
    """_pearson() correlation tests."""

    def test_perfect_positive(self):
        a = list(range(20))
        b = list(range(20))
        assert pc._pearson(a, b) == pytest.approx(1.0, abs=0.001)

    def test_perfect_negative(self):
        a = list(range(20))
        b = list(range(19, -1, -1))
        assert pc._pearson(a, b) == pytest.approx(-1.0, abs=0.001)

    def test_uncorrelated(self):
        a = [1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1]
        b = [1, 1, -1, -1, 1, 1, -1, -1, 1, 1, -1, -1]
        # Not perfectly uncorrelated but close to 0
        r = pc._pearson(a, b)
        assert -0.5 < r < 0.5

    def test_too_short(self):
        result = pc._pearson([1, 2, 3], [4, 5, 6])
        assert math.isnan(result)

    def test_constant_series(self):
        result = pc._pearson([5.0] * 20, list(range(20)))
        assert math.isnan(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _build_matrix
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildMatrix:
    """_build_matrix() should produce symmetric correlation matrix."""

    def test_diagonal_is_one(self):
        returns = {
            "AAPL": list(range(30)),
            "MSFT": list(range(30)),
        }
        matrix = pc._build_matrix(returns)
        assert matrix["AAPL"]["AAPL"] == 1.0
        assert matrix["MSFT"]["MSFT"] == 1.0

    def test_symmetric(self):
        returns = {
            "A": [float(x) for x in range(30)],
            "B": [float(x) * 0.5 + 1 for x in range(30)],
        }
        matrix = pc._build_matrix(returns)
        assert matrix["A"]["B"] == pytest.approx(matrix["B"]["A"], abs=0.001)

    def test_single_ticker(self):
        returns = {"ONLY": list(range(30))}
        matrix = pc._build_matrix(returns)
        assert matrix["ONLY"]["ONLY"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _cell_color
# ═══════════════════════════════════════════════════════════════════════════════

class TestCellColor:
    def test_high_red(self):
        assert pc._cell_color(0.85) == "#d73027"

    def test_moderate_yellow(self):
        assert pc._cell_color(0.45) == "#fee090"

    def test_negative_blue(self):
        assert pc._cell_color(-0.3) == "#91bfdb"

    def test_nan_grey(self):
        assert pc._cell_color(float("nan")) == "#eee"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _build_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildHtml:
    def test_html_contains_tickers(self):
        matrix = {"AAPL": {"AAPL": 1.0, "MSFT": 0.65}, "MSFT": {"AAPL": 0.65, "MSFT": 1.0}}
        html = pc._build_html(matrix, ["AAPL", "MSFT"], 90, "2026-01-01", [])
        assert "AAPL" in html
        assert "MSFT" in html

    def test_html_contains_warnings(self):
        matrix = {"A": {"A": 1.0, "B": 0.85}, "B": {"A": 0.85, "B": 1.0}}
        html = pc._build_html(matrix, ["A", "B"], 90, "2026-01-01",
                              ["A ↔ B: correlation 0.85"])
        assert "correlation 0.85" in html

    def test_html_no_warnings_ok(self):
        matrix = {"X": {"X": 1.0}}
        html = pc._build_html(matrix, ["X"], 90, "2026-01-01", [])
        assert "No high-correlation pairs" in html

    def test_html_multi_market_label(self):
        matrix = {"A": {"A": 1.0}}
        html = pc._build_html(matrix, ["A"], 90, "2026-01-01", [])
        assert "multi-market" in html


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _load_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPositions:
    def test_missing_file(self, tmp_path):
        result = pc._load_positions(tmp_path / "missing.json")
        assert result == []

    def test_list_format(self, tmp_path):
        f = tmp_path / "pos.json"
        f.write_text('[{"ticker":"AAPL"}]', encoding="utf-8")
        result = pc._load_positions(f)
        assert len(result) == 1

    def test_dict_format(self, tmp_path):
        f = tmp_path / "pos.json"
        f.write_text('{"positions":[{"ticker":"MSFT"}]}', encoding="utf-8")
        result = pc._load_positions(f)
        assert len(result) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
