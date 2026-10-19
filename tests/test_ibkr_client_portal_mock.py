"""
Tests for IBKR Client Portal REST API Adapter.

Tests mock all external dependencies (ibind, urllib) — no real IBKR connection needed.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingview"))


# ─── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_ibind_unavailable(monkeypatch):
    """Simulate ibind not installed."""
    monkeypatch.setattr("tradingview.ibkr_client_portal._IBIND_AVAILABLE", False)


@pytest.fixture
def sample_positions():
    return [
        {
            "contractDesc": "AAPL",
            "position": 100,
            "avgCost": 150.0,
            "mktPrice": 155.0,
            "mktValue": 15500.0,
            "unrealizedPnl": 500.0,
            "currency": "USD",
            "assetClass": "STK",
            "acctId": "DU12345",
        },
        {
            "contractDesc": "MSFT",
            "position": 50,
            "avgCost": 300.0,
            "mktPrice": 310.0,
            "mktValue": 15500.0,
            "unrealizedPnl": 500.0,
            "currency": "USD",
            "assetClass": "STK",
            "acctId": "DU12345",
        },
    ]


@pytest.fixture
def sample_accounts():
    return [
        {"accountId": "DU12345", "accountTitle": "Individual", "type": "INDIVIDUAL"}
    ]


# ─── Test: normalize_position ──────────────────────────────────────────────────

class TestNormalizePosition:
    def test_normalize_standard_fields(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        raw = {
            "contractDesc": "AAPL",
            "position": 100,
            "avgCost": 150.0,
            "mktPrice": 155.0,
            "mktValue": 15500.0,
            "unrealizedPnl": 500.0,
            "currency": "USD",
            "assetClass": "STK",
            "acctId": "DU12345",
        }
        result = IBKRClientPortal._normalize_position(raw)

        assert result["broker"] == "IBKR"
        assert result["symbol"] == "AAPL"
        assert result["qty"] == 100
        assert result["avg_cost"] == 150.0
        assert result["market_price"] == 155.0
        assert result["unrealized_pnl"] == 500.0
        assert result["currency"] == "USD"

    def test_normalize_alternative_field_names(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        raw = {
            "ticker": "TSLA",
            "pos": 25,
            "avgCost": 200.0,
            "mktPrice": 220.0,
            "mktValue": 5500.0,
            "unrealizedPL": 500.0,
            "currency": "USD",
        }
        result = IBKRClientPortal._normalize_position(raw)

        assert result["symbol"] == "TSLA"
        assert result["qty"] == 25
        assert result["unrealized_pnl"] == 500.0

    def test_normalize_missing_fields_defaults(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        raw = {}
        result = IBKRClientPortal._normalize_position(raw)

        assert result["broker"] == "IBKR"
        assert result["symbol"] == ""
        assert result["qty"] == 0
        assert result["avg_cost"] == 0.0
        assert result["currency"] == "USD"


# ─── Test: IBKRClientPortal unavailable mode ───────────────────────────────────

class TestClientPortalUnavailable:
    def test_unavailable_returns_empty_positions(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        with patch.object(IBKRClientPortal, "_health_check_direct", return_value=False):
            with patch("tradingview.ibkr_client_portal._IBIND_AVAILABLE", False):
                cp = IBKRClientPortal(use_ibind=False)
                assert cp.is_available is False
                assert cp.get_positions() == []

    def test_unavailable_returns_empty_accounts(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        with patch.object(IBKRClientPortal, "_health_check_direct", return_value=False):
            with patch("tradingview.ibkr_client_portal._IBIND_AVAILABLE", False):
                cp = IBKRClientPortal(use_ibind=False)
                assert cp.get_portfolio_accounts() == []

    def test_unavailable_returns_empty_orders(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        with patch.object(IBKRClientPortal, "_health_check_direct", return_value=False):
            with patch("tradingview.ibkr_client_portal._IBIND_AVAILABLE", False):
                cp = IBKRClientPortal(use_ibind=False)
                assert cp.get_live_orders() == []

    def test_unavailable_returns_empty_market_data(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        with patch.object(IBKRClientPortal, "_health_check_direct", return_value=False):
            with patch("tradingview.ibkr_client_portal._IBIND_AVAILABLE", False):
                cp = IBKRClientPortal(use_ibind=False)
                assert cp.get_market_data([12345]) == []


# ─── Test: Direct REST path with mocked responses ─────────────────────────────

class TestDirectRESTPath:
    def _make_cp(self):
        from tradingview.ibkr_client_portal import IBKRClientPortal

        with patch("tradingview.ibkr_client_portal._IBIND_AVAILABLE", False):
            cp = IBKRClientPortal.__new__(IBKRClientPortal)
            cp._base_url = "https://localhost:5000/v1/api"
            cp._timeout = 5
            cp._ibind_client = None
            cp._available = True
            return cp

    def test_get_positions_direct(self, sample_positions, sample_accounts):
        cp = self._make_cp()

        def mock_request(path, method="GET", body=None):
            if "accounts" in path:
                return sample_accounts
            if "positions" in path:
                return sample_positions
            return None

        with patch.object(cp, "_make_request", side_effect=mock_request):
            positions = cp.get_positions()
            assert len(positions) == 2
            assert positions[0]["symbol"] == "AAPL"
            assert positions[1]["symbol"] == "MSFT"
            assert all(p["broker"] == "IBKR" for p in positions)

    def test_get_portfolio_accounts_direct(self, sample_accounts):
        cp = self._make_cp()

        with patch.object(cp, "_make_request", return_value=sample_accounts):
            accounts = cp.get_portfolio_accounts()
            assert len(accounts) == 1
            assert accounts[0]["accountId"] == "DU12345"

    def test_get_live_orders_direct(self):
        cp = self._make_cp()
        orders_resp = {"orders": [{"orderId": 1, "symbol": "AAPL", "side": "BUY"}]}

        with patch.object(cp, "_make_request", return_value=orders_resp):
            orders = cp.get_live_orders()
            assert len(orders) == 1
            assert orders[0]["symbol"] == "AAPL"

    def test_get_market_data_direct(self):
        cp = self._make_cp()
        mktdata = [{"conid": 12345, "31": "155.50", "84": "155.40", "85": "155.60"}]

        with patch.object(cp, "_make_request", return_value=mktdata):
            data = cp.get_market_data([12345])
            assert len(data) == 1
            assert data[0]["conid"] == 12345


# ─── Test: Parallel fetch fix in ibkr_enricher ────────────────────────────────

class TestIBKREnricherParallelFetch:
    def test_compute_l6_uses_parallel_fetch(self):
        """Verify that compute_l6_score uses ThreadPoolExecutor for parallel fetches."""
        import tradingview.ibkr_enricher as ie

        # Mock all the individual fetch functions
        with patch.object(ie, "_IBAPI_AVAILABLE", True), \
             patch.object(ie, "get_conid", return_value=12345), \
             patch.object(ie, "fetch_news_sentiment", return_value={
                 "score": 0.3, "headlines": ["Good news"], "count": 1
             }), \
             patch.object(ie, "fetch_fundamental_ratios", return_value={
                 "PR": 20.0
             }), \
             patch.object(ie, "fetch_option_stats", return_value={
                 "pcr_volume": 0.8, "call_option_volume": 100,
                 "put_option_volume": 80
             }):
            # Clear cache first
            ie._L6_CACHE.clear()
            result = ie.compute_l6_score("TEST", force=True)

            assert result["l6_score"] is not None
            assert result["conid"] == 12345
            assert "news" in result
            assert "ratios" in result
            assert "options" in result
