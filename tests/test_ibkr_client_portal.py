"""Tests for F10: IBKR Client Portal REST adapter + dashboard fallback chain."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))

from tradingview.ibkr_client_portal import IBKRClientPortal


# ═══════════════════════════════════════════════════════════════════════════════
# 1. IBKRClientPortal class — unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestIBKRClientPortalUnit:
    """IBKRClientPortal without network — patch _health_check_direct."""

    def _make_cp(self, available: bool = False) -> IBKRClientPortal:
        """Build an IBKRClientPortal with mocked availability."""
        with patch.object(IBKRClientPortal, "_health_check_direct", return_value=available):
            cp = IBKRClientPortal(use_ibind=False)
        return cp

    # ── availability ──────────────────────────────────────────────────────

    def test_unavailable_by_default(self):
        cp = self._make_cp(False)
        assert cp.is_available is False

    def test_available_when_gateway_reachable(self):
        cp = self._make_cp(True)
        assert cp.is_available is True

    # ── get_positions empty when unavailable ──────────────────────────────

    def test_get_positions_empty_when_unavailable(self):
        cp = self._make_cp(False)
        assert cp.get_positions() == []

    def test_get_portfolio_accounts_empty_when_unavailable(self):
        cp = self._make_cp(False)
        assert cp.get_portfolio_accounts() == []

    def test_get_live_orders_empty_when_unavailable(self):
        cp = self._make_cp(False)
        assert cp.get_live_orders() == []

    def test_get_market_data_empty_when_unavailable(self):
        cp = self._make_cp(False)
        assert cp.get_market_data([12345]) == []

    def test_get_market_data_empty_no_conids(self):
        cp = self._make_cp(True)
        assert cp.get_market_data([]) == []

    # ── _normalize_position ───────────────────────────────────────────────

    def test_normalize_position_full(self):
        raw = {
            "contractDesc": "AAPL",
            "position": 100,
            "avgCost": 150.0,
            "mktPrice": 155.0,
            "mktValue": 15500.0,
            "unrealizedPnl": 500.0,
            "currency": "USD",
            "assetClass": "STK",
            "acctId": "U123",
        }
        result = IBKRClientPortal._normalize_position(raw)
        assert result["symbol"] == "AAPL"
        assert result["qty"] == 100
        assert result["avg_cost"] == 150.0
        assert result["market_price"] == 155.0
        assert result["unrealized_pnl"] == 500.0
        assert result["broker"] == "IBKR"
        assert result["currency"] == "USD"
        assert result["account"] == "U123"

    def test_normalize_position_alt_keys(self):
        """Handles alternative key names (ticker, pos, unrealizedPL)."""
        raw = {
            "ticker": "MSFT",
            "pos": 50,
            "avgCost": 300.0,
            "mktPrice": 310.0,
            "mktValue": 15500.0,
            "unrealizedPL": 500.0,
        }
        result = IBKRClientPortal._normalize_position(raw)
        assert result["symbol"] == "MSFT"
        assert result["qty"] == 50
        assert result["unrealized_pnl"] == 500.0

    def test_normalize_position_missing_fields(self):
        """Missing fields default gracefully."""
        result = IBKRClientPortal._normalize_position({})
        assert result["symbol"] == ""
        assert result["qty"] == 0
        assert result["avg_cost"] == 0.0
        assert result["broker"] == "IBKR"

    # ── get_positions with mocked REST ────────────────────────────────────

    def test_get_positions_direct_rest(self):
        cp = self._make_cp(True)
        mock_accounts = [{"accountId": "U123"}]
        mock_positions = [
            {"contractDesc": "AAPL", "position": 100, "avgCost": 150.0,
             "mktPrice": 155.0, "mktValue": 15500.0, "unrealizedPnl": 500.0,
             "currency": "USD"},
            {"contractDesc": "MSFT", "position": 50, "avgCost": 300.0,
             "mktPrice": 310.0, "mktValue": 15500.0, "unrealizedPnl": 500.0,
             "currency": "USD"},
        ]
        def mock_request(path, *args, **kwargs):
            if "accounts" in path:
                return mock_accounts
            if "positions" in path:
                return mock_positions
            return None

        with patch.object(cp, "_make_request", side_effect=mock_request):
            positions = cp.get_positions()

        assert len(positions) == 2
        assert positions[0]["symbol"] == "AAPL"
        assert positions[1]["symbol"] == "MSFT"

    def test_get_positions_multiple_pages(self):
        """Handles pagination (30 positions per page)."""
        cp = self._make_cp(True)
        page0 = [{"contractDesc": f"SYM{i}", "position": 10, "avgCost": 1.0,
                   "mktPrice": 1.0, "mktValue": 10.0, "unrealizedPnl": 0.0}
                  for i in range(30)]
        page1 = [{"contractDesc": "LAST", "position": 5, "avgCost": 1.0,
                   "mktPrice": 1.0, "mktValue": 5.0, "unrealizedPnl": 0.0}]

        call_count = {"n": 0}
        def mock_request(path, *args, **kwargs):
            if "accounts" in path:
                return [{"accountId": "U123"}]
            if "positions/0" in path:
                return page0
            if "positions/1" in path:
                return page1
            return None

        with patch.object(cp, "_make_request", side_effect=mock_request):
            positions = cp.get_positions()

        assert len(positions) == 31
        assert positions[-1]["symbol"] == "LAST"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Dashboard _fetch_ibkr_client_portal integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestDashboardClientPortalFallback:
    """Tests for _fetch_ibkr_client_portal in global_portfolio_dashboard.py."""

    def test_fetch_ibkr_client_portal_exists(self):
        """The function exists in global_portfolio_dashboard."""
        import global_portfolio_dashboard as gpd
        assert hasattr(gpd, "_fetch_ibkr_client_portal")
        assert callable(gpd._fetch_ibkr_client_portal)

    def test_fetch_ibkr_has_three_tier_fallback(self):
        """_fetch_ibkr() should call TWS → Flex → Client Portal."""
        import global_portfolio_dashboard as gpd
        import inspect
        src = inspect.getsource(gpd._fetch_ibkr)
        # Verify all three fallback stages are referenced
        assert "_fetch_ibkr_live_tws" in src
        assert "_fetch_ibkr_flex" in src
        assert "_fetch_ibkr_client_portal" in src

    def test_fetch_ibkr_skip_returns_empty(self):
        """_fetch_ibkr(skip=True) should return empty immediately."""
        import global_portfolio_dashboard as gpd
        positions, msg = gpd._fetch_ibkr(skip=True)
        assert positions == []
        assert "Skipped" in msg

    def test_fetch_ibkr_client_portal_unavailable(self):
        """When Client Portal is unavailable, returns empty list."""
        import global_portfolio_dashboard as gpd

        # Patch the IBKRClientPortal class at its source module
        mock_cp_cls = MagicMock()
        mock_cp_inst = MagicMock()
        mock_cp_inst.is_available = False
        mock_cp_cls.return_value = mock_cp_inst

        with patch.dict("sys.modules", {}):  # don't interfere with cached modules
            with patch("ibkr_client_portal.IBKRClientPortal", mock_cp_cls):
                positions, msg = gpd._fetch_ibkr_client_portal()
        # Should return empty — Client Portal is not reachable
        assert isinstance(positions, list)
        assert len(positions) == 0
        assert "unavailable" in msg.lower() or "error" in msg.lower()

    def test_fetch_ibkr_client_portal_returns_positions(self):
        """When Client Portal is available, returns normalized positions."""
        import global_portfolio_dashboard as gpd

        mock_cp_cls = MagicMock()
        mock_cp_inst = MagicMock()
        mock_cp_inst.is_available = True
        mock_cp_inst.get_positions.return_value = [
            {"symbol": "AAPL", "qty": 100, "avg_cost": 150.0,
             "market_price": 155.0, "market_value": 15500.0,
             "unrealized_pnl": 500.0, "currency": "USD"},
        ]
        mock_cp_cls.return_value = mock_cp_inst

        with patch("ibkr_client_portal.IBKRClientPortal", mock_cp_cls):
            positions, msg = gpd._fetch_ibkr_client_portal()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["broker"] == "IBKR"
        assert positions[0]["shares"] == 100
        assert "Client Portal" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# 3. IBKRClientPortal — ibind mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestIBKRClientPortalIbind:
    """Tests for ibind-based operation (mocked)."""

    def test_ibind_positions(self):
        """When ibind client is available, positions come from ibind."""
        cp = IBKRClientPortal.__new__(IBKRClientPortal)
        cp._base_url = "https://localhost:5000/v1/api"
        cp._timeout = 15
        cp._available = True

        mock_client = MagicMock()
        resp = MagicMock()
        resp.data = [
            {"contractDesc": "TSLA", "position": 200, "avgCost": 180.0,
             "mktPrice": 195.0, "mktValue": 39000.0, "unrealizedPnl": 3000.0,
             "currency": "USD", "acctId": "U999"},
        ]
        mock_client.portfolio_positions.return_value = resp
        cp._ibind_client = mock_client

        positions = cp.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "TSLA"
        assert positions[0]["qty"] == 200

    def test_ibind_positions_error_returns_empty(self):
        """ibind error → graceful empty return."""
        cp = IBKRClientPortal.__new__(IBKRClientPortal)
        cp._base_url = "https://localhost:5000/v1/api"
        cp._timeout = 15
        cp._available = True

        mock_client = MagicMock()
        mock_client.portfolio_positions.side_effect = RuntimeError("connection lost")
        cp._ibind_client = mock_client

        positions = cp.get_positions()
        assert positions == []

    def test_ibind_accounts(self):
        """ibind portfolio_accounts returns account list."""
        cp = IBKRClientPortal.__new__(IBKRClientPortal)
        cp._base_url = "https://localhost:5000/v1/api"
        cp._timeout = 15
        cp._available = True

        mock_client = MagicMock()
        resp = MagicMock()
        resp.data = [{"accountId": "U123", "type": "INDIVIDUAL"}]
        mock_client.portfolio_accounts.return_value = resp
        cp._ibind_client = mock_client

        accounts = cp.get_portfolio_accounts()
        assert len(accounts) == 1
        assert accounts[0]["accountId"] == "U123"

    def test_ibind_live_orders(self):
        """ibind live_orders returns orders list."""
        cp = IBKRClientPortal.__new__(IBKRClientPortal)
        cp._base_url = "https://localhost:5000/v1/api"
        cp._timeout = 15
        cp._available = True

        mock_client = MagicMock()
        resp = MagicMock()
        resp.data = {"orders": [{"orderId": 1, "status": "Filled"}]}
        mock_client.live_orders.return_value = resp
        cp._ibind_client = mock_client

        orders = cp.get_live_orders()
        assert len(orders) == 1
        assert orders[0]["orderId"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
