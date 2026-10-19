"""
Shared pytest fixtures for ibkr_async test suite.

Fixtures
--------
ibkr_config         : IBKRConfig.for_testing() — paper mode, short timeout
mock_ib             : MagicMock mimicking ib_async.IB API surface
mock_portfolio_items: Realistic PortfolioItem-like objects
mock_account_items  : Realistic AccountValue-like objects
skip_if_no_tws      : autouse=False mark to skip integration tests when TWS unavailable
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Adjust path so we can import ibkr_async from workspace root
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from ibkr_async.config import IBKRConfig
from ibkr_async.connection.health import is_tws_reachable_sync


# ── Config fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def ibkr_config() -> IBKRConfig:
    """Return test-mode IBKRConfig (paper port, 5s timeout, readonly)."""
    return IBKRConfig.for_testing()


# ── Mock ib_async.IB ──────────────────────────────────────────────────────────

def _make_portfolio_item(
    symbol: str = "AAPL",
    sectype: str = "STK",
    exchange: str = "SMART",
    currency: str = "USD",
    quantity: float = 100.0,
    avg_cost: float = 150.0,
    market_price: float = 175.0,
    market_value: float = 17500.0,
    unrealised_pnl: float = 2500.0,
    realised_pnl: float = 0.0,
) -> MagicMock:
    """Create a MagicMock that mimics an ib_async PortfolioItem."""
    item = MagicMock()
    item.contract.symbol     = symbol
    item.contract.secType    = sectype
    item.contract.exchange   = exchange
    item.contract.primaryExch = exchange
    item.contract.currency   = currency
    item.position            = quantity
    item.averageCost         = avg_cost
    item.marketPrice         = market_price
    item.marketValue         = market_value
    item.unrealizedPNL       = unrealised_pnl
    item.realizedPNL         = realised_pnl
    return item


def _make_account_value(tag: str, value: str, currency: str = "USD") -> MagicMock:
    """Create a MagicMock that mimics an ib_async AccountValue."""
    av = MagicMock()
    av.tag      = tag
    av.value    = value
    av.currency = currency
    return av


@pytest.fixture
def mock_portfolio_items() -> list[MagicMock]:
    """Return two realistic mock PortfolioItem objects."""
    return [
        _make_portfolio_item("AAPL", quantity=100, avg_cost=150.0,
                             market_price=175.0, market_value=17500.0,
                             unrealised_pnl=2500.0),
        _make_portfolio_item("MSFT", quantity=50,  avg_cost=300.0,
                             market_price=420.0, market_value=21000.0,
                             unrealised_pnl=6000.0),
    ]


@pytest.fixture
def mock_account_values() -> list[MagicMock]:
    """Return realistic mock AccountValue objects."""
    return [
        _make_account_value("NetLiquidation",     "100000.00"),
        _make_account_value("TotalCashValue",      "20000.00"),
        _make_account_value("UnrealizedPnL",        "8500.00"),
        _make_account_value("RealizedPnL",              "0.0"),
        _make_account_value("EquityWithLoanValue", "100000.00"),
        _make_account_value("AvailableFunds",       "80000.00"),
        _make_account_value("ExcessLiquidity",      "80000.00"),
        _make_account_value("Currency",                  "USD"),
    ]


@pytest.fixture
def mock_ib(mock_portfolio_items, mock_account_values) -> MagicMock:
    """
    Return a MagicMock that closely mimics the ib_async.IB API surface.
    connectAsync is an AsyncMock (coroutine).
    All other methods are synchronous MagicMocks returning realistic data.
    """
    ib = MagicMock(name="IB")

    # Async methods
    ib.connectAsync = AsyncMock()

    # Synchronous query methods
    ib.managedAccounts.return_value = ["DU12345"]
    ib.isConnected.return_value     = True
    ib.portfolio.return_value       = mock_portfolio_items
    ib.accountSummary.return_value  = mock_account_values
    ib.accountValues.return_value   = mock_account_values
    ib.openTrades.return_value      = []
    ib.reqMarketDataType.return_value = None
    ib.disconnect.return_value      = None

    return ib


# ── Skip marker for integration tests ────────────────────────────────────────

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring a live TWS/IB Gateway connection",
    )


@pytest.fixture
def skip_if_no_tws(ibkr_config):
    """
    Skip the test if TWS/IB Gateway is not reachable on the configured port.
    Use on integration tests:

        def test_live(skip_if_no_tws, ibkr_config):
            ...
    """
    if not is_tws_reachable_sync(ibkr_config.host, ibkr_config.port, timeout=2.0):
        pytest.skip(
            f"TWS/IB Gateway not reachable at "
            f"{ibkr_config.host}:{ibkr_config.port} — skipping integration test"
        )
