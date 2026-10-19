"""Tests for F7: ETF Holdings Overlap Analyzer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.etf_overlap import (
    load_positions,
    is_etf,
    identify_etfs,
    identify_stocks,
    compute_overlap,
    filter_overlapping,
    generate_html,
    OverlapEntry,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. is_etf
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsEtf:
    def test_known_etfs(self):
        for sym in ["SPY", "QQQ", "ARKK", "VOO", "VTI", "IOZ", "NDQ"]:
            assert is_etf(sym), f"{sym} should be ETF"

    def test_stocks(self):
        for sym in ["NVDA", "AAPL", "BHP", "RELIANCE"]:
            assert not is_etf(sym), f"{sym} should NOT be ETF"

    def test_case_insensitive(self):
        assert is_etf("spy") is True


# ═══════════════════════════════════════════════════════════════════════════════
# 2. identify_etfs / identify_stocks
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentify:
    def test_split(self):
        pos = [
            {"symbol": "SPY", "market_val": 10000},
            {"symbol": "NVDA", "market_val": 5000},
            {"symbol": "QQQ", "market_val": 8000},
        ]
        etfs = identify_etfs(pos)
        stocks = identify_stocks(pos)
        assert len(etfs) == 2
        assert len(stocks) == 1
        assert stocks[0]["symbol"] == "NVDA"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. compute_overlap
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeOverlap:
    def test_basic_overlap(self):
        pos = [
            {"symbol": "SPY", "market_val": 10000, "broker": "IBKR"},
            {"symbol": "NVDA", "market_val": 5000, "broker": "IBKR"},
        ]
        etf_holdings = {
            "SPY": [{"symbol": "NVDA", "weight": 6.5}]
        }
        entries = compute_overlap(pos, etf_holdings)
        # NVDA appears: direct 5000 + indirect (10000 * 0.065 = 650)
        nvda = next(e for e in entries if e.stock_symbol == "NVDA")
        assert nvda.direct_value == 5000
        assert nvda.indirect_value == pytest.approx(650, rel=0.01)

    def test_no_overlap(self):
        pos = [
            {"symbol": "SPY", "market_val": 10000},
            {"symbol": "BHP", "market_val": 5000},  # not in SPY
        ]
        etf_holdings = {
            "SPY": [{"symbol": "AAPL", "weight": 7.0}]
        }
        entries = compute_overlap(pos, etf_holdings)
        overlapping = filter_overlapping(entries)
        assert len(overlapping) == 0

    def test_multiple_etfs(self):
        pos = [
            {"symbol": "SPY", "market_val": 10000},
            {"symbol": "QQQ", "market_val": 8000},
            {"symbol": "AAPL", "market_val": 3000},
        ]
        etf_holdings = {
            "SPY": [{"symbol": "AAPL", "weight": 7.0}],
            "QQQ": [{"symbol": "AAPL", "weight": 12.0}],
        }
        entries = compute_overlap(pos, etf_holdings)
        aapl = next(e for e in entries if e.stock_symbol == "AAPL")
        # indirect = 10000*0.07 + 8000*0.12 = 700 + 960 = 1660
        assert aapl.indirect_value == pytest.approx(1660, rel=0.01)

    def test_empty_positions(self):
        assert compute_overlap([]) == []

    def test_zero_portfolio(self):
        pos = [{"symbol": "SPY", "market_val": 0}]
        assert compute_overlap(pos) == []

    def test_sorted_by_total_pct(self):
        pos = [
            {"symbol": "SPY", "market_val": 100000},
            {"symbol": "AAPL", "market_val": 1000},
            {"symbol": "NVDA", "market_val": 5000},
        ]
        etf_holdings = {
            "SPY": [
                {"symbol": "AAPL", "weight": 7.0},
                {"symbol": "NVDA", "weight": 6.0},
            ]
        }
        entries = compute_overlap(pos, etf_holdings)
        # AAPL: direct 1000 + indirect 7000 = 8000, pct ~7.5%
        # NVDA: direct 5000 + indirect 6000 = 11000, pct ~10.4%
        assert entries[0].stock_symbol == "NVDA"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. filter_overlapping
# ═══════════════════════════════════════════════════════════════════════════════

class TestFilterOverlapping:
    def test_both(self):
        e = OverlapEntry(stock_symbol="X", direct_value=100, indirect_value=50)
        assert len(filter_overlapping([e])) == 1

    def test_direct_only(self):
        e = OverlapEntry(stock_symbol="X", direct_value=100, indirect_value=0)
        assert len(filter_overlapping([e])) == 0

    def test_indirect_only(self):
        e = OverlapEntry(stock_symbol="X", direct_value=0, indirect_value=50)
        assert len(filter_overlapping([e])) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_overlap(self):
        e = OverlapEntry(stock_symbol="NVDA", direct_value=5000, direct_pct=3.0,
                         indirect_value=650, indirect_pct=0.5, total_pct=3.5,
                         etf_sources=[("SPY", 6.5)])
        html = generate_html([e])
        assert "NVDA" in html
        assert "SPY" in html
        assert "overlap" in html

    def test_empty(self):
        html = generate_html([])
        assert "No overlap data" in html

    def test_written(self, tmp_path):
        out = tmp_path / "overlap.html"
        generate_html([], out)
        assert out.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. load_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPositions:
    def test_valid(self, tmp_path):
        f = tmp_path / "pos.json"
        f.write_text('[{"symbol":"X"}]', encoding="utf-8")
        assert len(load_positions(f)) == 1

    def test_missing(self, tmp_path):
        assert load_positions(tmp_path / "nope.json") == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
