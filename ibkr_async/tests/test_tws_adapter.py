"""
Unit tests for TWSAdapter.

All tests mock the ib_async library — no real TWS connection required.
The mock_ib fixture (from conftest.py) provides a MagicMock mimicking
the ib_async.IB API surface.

Strategy:
- Patch `ibkr_async.adapters.tws_adapter._get_ib_class` to return a callable
  that returns mock_ib when called (IB() call inside TWSAdapter.__init__).
- Use pytest-asyncio for async test functions.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from ibkr_async.adapters.tws_adapter import TWSAdapter
from ibkr_async.config import IBKRConfig
from ibkr_async.exceptions import (
    IBKRConnectionError,
    IBKRDataError,
    IBKRNotAvailableError,
    IBKRTimeoutError,
)
from ibkr_async.models import (
    AccountSummary,
    AssetClass,
    ConnectionStatus,
    HealthStatus,
    Position,
)


# ── Helper to build a connected adapter with injected mock IB ────────────────

def _make_adapter(mock_ib: MagicMock, config: IBKRConfig) -> TWSAdapter:
    """
    Create a TWSAdapter with its internal _ib already set to mock_ib,
    bypassing the real connectAsync call.
    """
    adapter = TWSAdapter(config)
    adapter._ib          = mock_ib
    adapter._status      = ConnectionStatus.CONNECTED
    adapter._account_id  = "DU12345"
    return adapter


# ── Connection tests ──────────────────────────────────────────────────────────

class TestTWSAdapterConnect:

    @pytest.mark.asyncio
    async def test_connect_success(self, ibkr_config, mock_ib):
        """connect() sets status=CONNECTED and extracts account_id."""
        with patch(
            "ibkr_async.adapters.tws_adapter._get_ib_class",
            return_value=lambda: mock_ib,
        ):
            adapter = TWSAdapter(ibkr_config)
            await adapter.connect()

        assert adapter.is_connected is True
        assert adapter._status == ConnectionStatus.CONNECTED
        assert adapter._account_id == "DU12345"
        mock_ib.connectAsync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_timeout_raises_ibkr_timeout_error(self, ibkr_config):
        """When connectAsync times out, IBKRTimeoutError is raised."""
        ib = MagicMock()
        ib.connectAsync = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch(
            "ibkr_async.adapters.tws_adapter._get_ib_class",
            return_value=lambda: ib,
        ):
            with pytest.raises(IBKRTimeoutError, match="timed out"):
                await TWSAdapter(ibkr_config).connect()

    @pytest.mark.asyncio
    async def test_connect_refused_raises_ibkr_connection_error(self, ibkr_config):
        """When connectAsync raises ConnectionRefusedError, IBKRConnectionError is raised."""
        ib = MagicMock()
        ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError())

        with patch(
            "ibkr_async.adapters.tws_adapter._get_ib_class",
            return_value=lambda: ib,
        ):
            with pytest.raises(IBKRConnectionError, match="refused"):
                await TWSAdapter(ibkr_config).connect()

    @pytest.mark.asyncio
    async def test_connect_sets_market_data_type(self, ibkr_config, mock_ib):
        """connect() calls reqMarketDataType with the configured value."""
        with patch(
            "ibkr_async.adapters.tws_adapter._get_ib_class",
            return_value=lambda: mock_ib,
        ):
            adapter = TWSAdapter(ibkr_config)
            await adapter.connect()

        mock_ib.reqMarketDataType.assert_called_once_with(ibkr_config.market_data_type)

    @pytest.mark.asyncio
    async def test_ib_async_not_installed_raises(self, ibkr_config):
        """If ib_async is missing, IBKRNotAvailableError is raised on connect()."""
        with patch(
            "ibkr_async.adapters.tws_adapter._get_ib_class",
            side_effect=IBKRNotAvailableError("not installed"),
        ):
            with pytest.raises(IBKRNotAvailableError):
                await TWSAdapter(ibkr_config).connect()


class TestTWSAdapterDisconnect:

    @pytest.mark.asyncio
    async def test_disconnect_calls_ib_disconnect(self, ibkr_config, mock_ib):
        adapter = _make_adapter(mock_ib, ibkr_config)
        await adapter.disconnect()
        mock_ib.disconnect.assert_called_once()
        assert adapter._ib is None
        assert adapter._status == ConnectionStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_idempotent_when_already_disconnected(self, ibkr_config):
        adapter = TWSAdapter(ibkr_config)
        # Calling disconnect on a never-connected adapter should not raise
        await adapter.disconnect()
        assert adapter._ib is None


# ── fetch_positions ───────────────────────────────────────────────────────────

class TestTWSAdapterFetchPositions:

    @pytest.mark.asyncio
    async def test_returns_list_of_positions(self, ibkr_config, mock_ib):
        """fetch_positions returns a list of Position domain objects."""
        adapter = _make_adapter(mock_ib, ibkr_config)
        positions = await adapter.fetch_positions()

        assert isinstance(positions, list)
        assert len(positions) == 2  # fixture has AAPL + MSFT
        for p in positions:
            assert isinstance(p, Position)

    @pytest.mark.asyncio
    async def test_position_fields_correct(self, ibkr_config, mock_ib):
        """Position objects have correct field values from mock portfolio items."""
        adapter = _make_adapter(mock_ib, ibkr_config)
        positions = await adapter.fetch_positions()
        aapl = next(p for p in positions if p.symbol == "AAPL")

        assert aapl.symbol         == "AAPL"
        assert aapl.quantity       == 100.0
        assert aapl.avg_cost       == 150.0
        assert aapl.market_price   == 175.0
        assert aapl.market_value   == 17_500.0
        assert aapl.unrealised_pnl == 2_500.0
        assert aapl.broker         == "IBKR"
        assert aapl.account_id     == "DU12345"

    @pytest.mark.asyncio
    async def test_position_asset_class_mapped(self, ibkr_config, mock_ib):
        adapter = _make_adapter(mock_ib, ibkr_config)
        positions = await adapter.fetch_positions()
        # All fixture items are "STK"
        for p in positions:
            assert p.asset_class == AssetClass.STOCK

    @pytest.mark.asyncio
    async def test_empty_portfolio_returns_empty_list(self, ibkr_config, mock_ib):
        mock_ib.portfolio.return_value = []
        adapter = _make_adapter(mock_ib, ibkr_config)
        positions = await adapter.fetch_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_fetch_positions_not_connected_raises(self, ibkr_config):
        """fetch_positions raises IBKRConnectionError when not connected."""
        adapter = TWSAdapter(ibkr_config)  # No connect()
        with pytest.raises(IBKRConnectionError):
            await adapter.fetch_positions()

    @pytest.mark.asyncio
    async def test_bad_portfolio_item_skipped_not_raised(self, ibkr_config, mock_ib):
        """Malformed PortfolioItems are skipped with a warning, not raised."""
        bad_item = MagicMock()
        bad_item.contract.symbol = "BAD"
        bad_item.position        = "not_a_float"  # This will fail float() conversion
        bad_item.averageCost     = None
        bad_item.marketPrice     = None
        bad_item.marketValue     = None
        bad_item.unrealizedPNL   = None
        bad_item.realizedPNL     = None

        # Mix one bad item with one good item
        good_item = mock_ib.portfolio.return_value[0]
        mock_ib.portfolio.return_value = [bad_item, good_item]

        adapter = _make_adapter(mock_ib, ibkr_config)
        positions = await adapter.fetch_positions()
        # Bad item is skipped; good item should still appear
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"


# ── fetch_account_summary ─────────────────────────────────────────────────────

class TestTWSAdapterFetchAccountSummary:

    @pytest.mark.asyncio
    async def test_returns_account_summary(self, ibkr_config, mock_ib):
        adapter = _make_adapter(mock_ib, ibkr_config)
        account = await adapter.fetch_account_summary()

        assert isinstance(account, AccountSummary)
        assert account.account_id      == "DU12345"
        assert account.net_liquidation == 100_000.0
        assert account.total_cash      == 20_000.0
        assert account.unrealised_pnl  == 8_500.0

    @pytest.mark.asyncio
    async def test_not_connected_raises(self, ibkr_config):
        with pytest.raises(IBKRConnectionError):
            await TWSAdapter(ibkr_config).fetch_account_summary()

    @pytest.mark.asyncio
    async def test_missing_tags_default_to_zero(self, ibkr_config, mock_ib):
        """When account summary returns empty list, values default to 0."""
        mock_ib.accountValues.return_value = []
        adapter = _make_adapter(mock_ib, ibkr_config)
        account = await adapter.fetch_account_summary()
        assert account.net_liquidation == 0.0
        assert account.total_cash      == 0.0


# ── health_check ──────────────────────────────────────────────────────────────

class TestTWSAdapterHealthCheck:

    @pytest.mark.asyncio
    async def test_health_connected(self, ibkr_config, mock_ib):
        """health_check returns CONNECTED status when ib.isConnected() is True."""
        mock_ib.isConnected.return_value = True
        adapter = _make_adapter(mock_ib, ibkr_config)
        health = await adapter.health_check()

        assert health.is_connected is True
        assert health.is_healthy   is True
        assert health.status       == ConnectionStatus.CONNECTED
        assert health.host         == ibkr_config.host
        assert health.port         == ibkr_config.port

    @pytest.mark.asyncio
    async def test_health_disconnected(self, ibkr_config, mock_ib):
        mock_ib.isConnected.return_value = False
        adapter = _make_adapter(mock_ib, ibkr_config)
        health = await adapter.health_check()

        assert health.is_connected is False
        assert health.is_healthy   is False

    @pytest.mark.asyncio
    async def test_health_never_raises(self, ibkr_config, mock_ib):
        """health_check must never raise even if _ib throws."""
        mock_ib.isConnected.side_effect = RuntimeError("unexpected!")
        adapter = _make_adapter(mock_ib, ibkr_config)
        health = await adapter.health_check()  # Should not raise
        assert health.is_connected is False

    @pytest.mark.asyncio
    async def test_health_when_never_connected(self, ibkr_config):
        """health_check on a brand-new adapter returns DISCONNECTED."""
        adapter = TWSAdapter(ibkr_config)
        health = await adapter.health_check()
        assert health.is_connected is False
        assert health.status == ConnectionStatus.DISCONNECTED


# ── is_connected property ─────────────────────────────────────────────────────

class TestTWSAdapterIsConnected:

    def test_is_connected_false_when_ib_none(self, ibkr_config):
        adapter = TWSAdapter(ibkr_config)
        assert adapter.is_connected is False

    def test_is_connected_true_when_ib_reports_connected(self, ibkr_config, mock_ib):
        mock_ib.isConnected.return_value = True
        adapter = _make_adapter(mock_ib, ibkr_config)
        assert adapter.is_connected is True

    def test_is_connected_false_when_ib_reports_disconnected(self, ibkr_config, mock_ib):
        mock_ib.isConnected.return_value = False
        adapter = _make_adapter(mock_ib, ibkr_config)
        assert adapter.is_connected is False

    def test_is_connected_false_when_ib_raises(self, ibkr_config, mock_ib):
        mock_ib.isConnected.side_effect = RuntimeError("boom")
        adapter = _make_adapter(mock_ib, ibkr_config)
        assert adapter.is_connected is False


# ── supports_live_quotes ──────────────────────────────────────────────────────

def test_tws_adapter_supports_live_quotes(ibkr_config):
    assert TWSAdapter(ibkr_config).supports_live_quotes is True


# ── fetch_open_orders ─────────────────────────────────────────────────────────

class TestTWSAdapterFetchOrders:

    @pytest.mark.asyncio
    async def test_fetch_open_orders_empty(self, ibkr_config, mock_ib):
        mock_ib.openTrades.return_value = []
        adapter = _make_adapter(mock_ib, ibkr_config)
        orders = await adapter.fetch_open_orders()
        assert orders == []

    @pytest.mark.asyncio
    async def test_fetch_open_orders_returns_dicts(self, ibkr_config, mock_ib):
        trade = MagicMock()
        trade.order.orderId        = 101
        trade.contract.symbol      = "AAPL"
        trade.order.action         = "BUY"
        trade.order.totalQuantity  = 50
        trade.order.orderType      = "LMT"
        trade.order.lmtPrice       = 170.0
        trade.orderStatus.status   = "Submitted"

        mock_ib.openTrades.return_value = [trade]
        adapter = _make_adapter(mock_ib, ibkr_config)
        orders = await adapter.fetch_open_orders()

        assert len(orders) == 1
        o = orders[0]
        assert o["order_id"]   == 101
        assert o["symbol"]     == "AAPL"
        assert o["action"]     == "BUY"
        assert o["quantity"]   == 50.0
        assert o["order_type"] == "LMT"
        assert o["status"]     == "Submitted"


# ── cancel_order ──────────────────────────────────────────────────────────────

class TestTWSAdapterCancelOrder:

    @pytest.mark.asyncio
    async def test_cancel_blocked_in_readonly_mode(self, ibkr_config, mock_ib):
        """cancel_order returns False (and doesn't call cancelOrder) in readonly mode."""
        readonly_cfg = IBKRConfig.for_testing()
        assert readonly_cfg.readonly is True
        adapter = _make_adapter(mock_ib, readonly_cfg)
        result = await adapter.cancel_order(101)
        assert result is False
        mock_ib.cancelOrder.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_order_not_found_returns_false(self, ibkr_config):
        cfg = IBKRConfig.for_testing()
        # Make non-readonly for this test
        cfg2 = IBKRConfig(
            host=cfg.host, port=cfg.port, client_id=cfg.client_id,
            timeout=cfg.timeout, readonly=False, paper_trading=True,
            flex_token=cfg.flex_token, flex_query_id=cfg.flex_query_id,
        )
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.openTrades.return_value  = []  # No open orders

        adapter = _make_adapter(ib, cfg2)
        result = await adapter.cancel_order(999)
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_order_not_connected_raises(self, ibkr_config):
        adapter = TWSAdapter(ibkr_config)
        with pytest.raises(IBKRConnectionError):
            await adapter.cancel_order(1)


# ── Context manager ───────────────────────────────────────────────────────────

class TestTWSAdapterContextManager:

    @pytest.mark.asyncio
    async def test_context_manager_calls_connect_and_disconnect(self, ibkr_config, mock_ib):
        with patch(
            "ibkr_async.adapters.tws_adapter._get_ib_class",
            return_value=lambda: mock_ib,
        ):
            async with TWSAdapter(ibkr_config) as adapter:
                assert adapter.is_connected is True
            # After exiting context
            mock_ib.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_disconnects_on_exception(self, ibkr_config, mock_ib):
        with patch(
            "ibkr_async.adapters.tws_adapter._get_ib_class",
            return_value=lambda: mock_ib,
        ):
            with pytest.raises(RuntimeError):
                async with TWSAdapter(ibkr_config):
                    raise RuntimeError("test error")
            # Must still disconnect
            mock_ib.disconnect.assert_called_once()
