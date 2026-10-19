"""Tests for tradingview/analysis/portfolio_rebalancer.py — Portfolio rebalancing."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tradingview.analysis.portfolio_rebalancer as rebalancer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_positions():
    """Sample position dicts as found in .cache_global_positions.json."""
    return [
        {"symbol": "AAPL", "shares": 100, "market_val": 15000, "current_price": 150, "broker": "IBKR", "exchange": "NASDAQ"},
        {"symbol": "MSFT", "shares": 50, "market_val": 20000, "current_price": 400, "broker": "IBKR", "exchange": "NASDAQ"},
        {"symbol": "ASX:BHP", "shares": 200, "market_val": 8000, "current_price": 40, "broker": "Moomoo-AU", "exchange": "ASX"},
        {"symbol": "ASX:CBA", "shares": 30, "market_val": 3000, "current_price": 100, "broker": "Moomoo-AU", "exchange": "ASX"},
        {"symbol": "NSE:RELIANCE", "shares": 10, "market_val": 5000, "current_price": 500, "broker": "Zerodha", "exchange": "NSE"},
        {"symbol": "SGX:DBS", "shares": 50, "market_val": 4000, "current_price": 80, "broker": "Moomoo-SG", "exchange": "SGX"},
    ]


@pytest.fixture()
def positions_cache(tmp_path, sample_positions):
    """Create a temp .cache_global_positions.json."""
    cache_file = tmp_path / ".cache_global_positions.json"
    cache_file.write_text(json.dumps(sample_positions), encoding="utf-8")
    return cache_file


@pytest.fixture()
def target_allocation():
    """Default target allocation (percentages)."""
    return {"US": 40, "AU": 25, "IN": 15, "SG": 10, "HK": 10}


# ---------------------------------------------------------------------------
# Test: classify_region — takes a dict, not a string
# ---------------------------------------------------------------------------

class TestClassifyRegion:
    def test_us_stocks_by_broker(self):
        assert rebalancer.classify_region({"symbol": "AAPL", "broker": "IBKR"}) == "US"

    def test_au_stocks_by_exchange(self):
        assert rebalancer.classify_region({"symbol": "BHP", "broker": "Moomoo-AU", "exchange": "ASX"}) == "AU"

    def test_india_stocks_by_broker(self):
        assert rebalancer.classify_region({"symbol": "RELIANCE", "broker": "Zerodha"}) == "IN"
        assert rebalancer.classify_region({"symbol": "TCS", "broker": "AngelOne"}) == "IN"

    def test_sg_stocks_by_broker(self):
        assert rebalancer.classify_region({"symbol": "DBS", "broker": "Moomoo-SG"}) == "SG"

    def test_hk_stocks_by_exchange(self):
        assert rebalancer.classify_region({"symbol": "0700", "broker": "Moomoo-AU", "exchange": "HKEX"}) == "HK"

    def test_unknown_defaults_to_other(self):
        assert rebalancer.classify_region({"symbol": "TSLA", "broker": "UnknownBroker"}) == "OTHER"


# ---------------------------------------------------------------------------
# Test: load_positions
# ---------------------------------------------------------------------------

class TestLoadPositions:
    def test_loads_from_cache(self, positions_cache):
        positions = rebalancer.load_positions(positions_cache)
        assert len(positions) == 6

    def test_position_has_region(self, positions_cache):
        positions = rebalancer.load_positions(positions_cache)
        regions = {p.region for p in positions}
        assert "US" in regions
        assert "AU" in regions

    def test_returns_empty_for_missing_file(self, tmp_path):
        positions = rebalancer.load_positions(tmp_path / "missing.json")
        assert positions == []

    def test_position_market_val(self, positions_cache):
        positions = rebalancer.load_positions(positions_cache)
        us_positions = [p for p in positions if p.region == "US"]
        assert len(us_positions) == 2
        assert us_positions[0].market_val > 0


# ---------------------------------------------------------------------------
# Test: compute_rebalance — returns (actions, stats) tuple
# ---------------------------------------------------------------------------

class TestComputeRebalance:
    def test_produces_actions_and_stats(self, positions_cache, target_allocation):
        positions = rebalancer.load_positions(positions_cache)
        result = rebalancer.compute_rebalance(positions, target_allocation)
        assert isinstance(result, tuple)
        actions, stats = result
        assert isinstance(actions, list)
        assert isinstance(stats, dict)
        assert len(actions) > 0

    def test_actions_have_action_field(self, positions_cache, target_allocation):
        positions = rebalancer.load_positions(positions_cache)
        actions, _ = rebalancer.compute_rebalance(positions, target_allocation)
        for a in actions:
            assert a.action in ("BUY", "SELL")

    def test_actions_have_region(self, positions_cache, target_allocation):
        positions = rebalancer.load_positions(positions_cache)
        actions, _ = rebalancer.compute_rebalance(positions, target_allocation)
        for a in actions:
            assert a.region in ("US", "AU", "IN", "SG", "HK", "OTHER")

    def test_stats_has_total(self, positions_cache, target_allocation):
        positions = rebalancer.load_positions(positions_cache)
        _, stats = rebalancer.compute_rebalance(positions, target_allocation)
        assert "total" in stats
        assert stats["total"] > 0
        assert "regions" in stats

    def test_empty_positions(self, target_allocation):
        actions, stats = rebalancer.compute_rebalance([], target_allocation)
        assert actions == []
        assert stats["total"] == 0

    def test_single_region_over_allocated(self):
        positions = [rebalancer.Position(
            symbol="AAPL", shares=100, market_val=50000,
            current_price=500, broker="IBKR", region="US",
        )]
        target = {"US": 40, "AU": 25, "IN": 15, "SG": 10, "HK": 10}
        actions, stats = rebalancer.compute_rebalance(positions, target)
        us_actions = [a for a in actions if a.region == "US"]
        assert len(us_actions) >= 1
        assert us_actions[0].action == "SELL"


# ---------------------------------------------------------------------------
# Test: HTML report generation — generate_rebalance_html(actions, stats, target_alloc, out)
# ---------------------------------------------------------------------------

class TestRebalanceHtmlReport:
    def test_generates_html(self, positions_cache, target_allocation, tmp_path):
        positions = rebalancer.load_positions(positions_cache)
        actions, stats = rebalancer.compute_rebalance(positions, target_allocation)
        out = tmp_path / "rebalance.html"
        result = rebalancer.generate_rebalance_html(actions, stats, target_allocation, out)
        assert result.exists()
        html = result.read_text(encoding="utf-8")
        assert "Rebalance" in html or "rebalance" in html

    def test_html_contains_regions(self, positions_cache, target_allocation, tmp_path):
        positions = rebalancer.load_positions(positions_cache)
        actions, stats = rebalancer.compute_rebalance(positions, target_allocation)
        out = tmp_path / "rebalance.html"
        result = rebalancer.generate_rebalance_html(actions, stats, target_allocation, out)
        html = result.read_text(encoding="utf-8")
        assert "US" in html


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestRebalanceIntegration:
    def test_full_pipeline(self, positions_cache, target_allocation, tmp_path):
        positions = rebalancer.load_positions(positions_cache)
        assert len(positions) > 0
        actions, stats = rebalancer.compute_rebalance(positions, target_allocation)
        assert len(actions) > 0
        out = tmp_path / "rebalance.html"
        result = rebalancer.generate_rebalance_html(actions, stats, target_allocation, out)
        assert result.exists()
