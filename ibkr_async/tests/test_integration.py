"""
Integration tests for ibkr_async.

These tests require a live TWS or IB Gateway connection.
They are automatically skipped when TWS is not reachable.

Markers:
    @pytest.mark.integration — on every test in this file

Run integration tests only:
    pytest ibkr_async/tests/test_integration.py -m integration -v

Skip integration tests (default behaviour):
    pytest ibkr_async/tests/          <- these tests are auto-skipped

Requirements:
    - TWS or IB Gateway running on 127.0.0.1:7496 (live) or 7497 (paper)
    - API enabled: Configure → API → Settings → Enable Socket Clients
    - Client ID 98 must be free (not in use by another connection)
"""

from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from ibkr_async.adapters.tws_adapter import TWSAdapter
from ibkr_async.adapters.flex_adapter import FlexAdapter
from ibkr_async.config import IBKRConfig, PORT_TWS_PAPER, PORT_TWS_LIVE
from ibkr_async.connection.health import is_tws_reachable_sync
from ibkr_async.connection.manager import ConnectionManager
from ibkr_async.models import AccountSummary, HealthStatus, Position


# ── Skip logic ────────────────────────────────────────────────────────────────
# Auto-skip all tests in this file if TWS is not reachable

def _find_tws_port() -> int | None:
    """Check paper port first, then live. Return first reachable port or None."""
    for port in [PORT_TWS_PAPER, PORT_TWS_LIVE]:
        if is_tws_reachable_sync("127.0.0.1", port, timeout=2.0):
            return port
    return None


_TWS_PORT = _find_tws_port()
_SKIP_REASON = "TWS/IB Gateway not reachable on port 7496 or 7497 — skipping integration tests"

pytestmark = pytest.mark.integration

_SKIP_IF_NO_TWS = pytest.mark.skipif(_TWS_PORT is None, reason=_SKIP_REASON)


@pytest.fixture
def live_config() -> IBKRConfig:
    """IBKRConfig pointing to whichever TWS port is reachable."""
    port = _TWS_PORT or PORT_TWS_PAPER
    return IBKRConfig(
        host="127.0.0.1",
        port=port,
        client_id=98,          # Use a dedicated client_id for integration tests
        timeout=30,
        readonly=True,         # Never place orders in integration tests
        paper_trading=(port == PORT_TWS_PAPER),
        market_data_type=3,    # Delayed data, no subscription needed
    )


# ── TWSAdapter integration tests ──────────────────────────────────────────────

@_SKIP_IF_NO_TWS
@pytest.mark.asyncio
async def test_tws_connect_and_health(live_config):
    """Connect to TWS, verify health_check returns CONNECTED."""
    async with TWSAdapter(live_config) as adapter:
        health = await adapter.health_check()

    assert health.is_connected is True
    assert health.is_healthy   is True
    assert health.latency_ms   is not None
    assert health.latency_ms   < 1000  # Under 1 second


@_SKIP_IF_NO_TWS
@pytest.mark.asyncio
async def test_tws_fetch_positions_returns_list(live_config):
    """fetch_positions returns a list of Position objects (may be empty)."""
    async with TWSAdapter(live_config) as adapter:
        positions = await adapter.fetch_positions()

    assert isinstance(positions, list)
    for p in positions:
        assert isinstance(p, Position)
        assert p.symbol
        assert p.broker == "IBKR"


@_SKIP_IF_NO_TWS
@pytest.mark.asyncio
async def test_tws_fetch_account_summary(live_config):
    """fetch_account_summary returns an AccountSummary with net_liquidation >= 0."""
    async with TWSAdapter(live_config) as adapter:
        account = await adapter.fetch_account_summary()

    assert isinstance(account, AccountSummary)
    assert account.net_liquidation >= 0.0


@_SKIP_IF_NO_TWS
@pytest.mark.asyncio
async def test_tws_fetch_open_orders_returns_list(live_config):
    """fetch_open_orders returns a list (possibly empty)."""
    async with TWSAdapter(live_config) as adapter:
        orders = await adapter.fetch_open_orders()

    assert isinstance(orders, list)
    for o in orders:
        assert "order_id" in o
        assert "symbol"   in o
        assert "action"   in o


@_SKIP_IF_NO_TWS
@pytest.mark.asyncio
async def test_tws_cancel_readonly_returns_false(live_config):
    """cancel_order in readonly mode always returns False."""
    async with TWSAdapter(live_config) as adapter:
        result = await adapter.cancel_order(99999)

    assert result is False  # readonly=True from live_config


@_SKIP_IF_NO_TWS
@pytest.mark.asyncio
async def test_connection_manager_lifecycle(live_config):
    """ConnectionManager connects, returns adapter, disconnects cleanly."""
    async with ConnectionManager(live_config, health_check_interval=0) as mgr:
        adapter = mgr.adapter
        health  = await mgr.health_check()

    assert health.is_connected is True


@_SKIP_IF_NO_TWS
@pytest.mark.asyncio
async def test_tws_position_to_dict_format(live_config):
    """Position.to_dict() output has the keys expected by global_portfolio_dashboard."""
    async with TWSAdapter(live_config) as adapter:
        positions = await adapter.fetch_positions()

    required_keys = {
        "symbol", "exchange", "currency", "qty", "avg_cost",
        "last_price", "market_value", "unrealised_pnl", "realised_pnl",
        "pnl_pct", "broker", "account_id", "timestamp",
    }
    for p in positions:
        d = p.to_dict()
        assert required_keys.issubset(set(d.keys())), (
            f"Position.to_dict() is missing keys: {required_keys - set(d.keys())}"
        )


# ── FlexAdapter integration test ──────────────────────────────────────────────

@pytest.mark.skipif(True, reason=(
    "Flex Query integration test disabled by default — it makes a live HTTP request "
    "to IBKR servers and takes 30-300 seconds. "
    "Enable manually: remove skipif and run with --no-header."
))
@pytest.mark.asyncio
async def test_flex_fetch_positions_live(live_config):
    """
    Live Flex Query integration test — fetches real data from IBKR.
    Disabled by default to avoid slow CI runs and IBKR rate limiting.
    """
    async with FlexAdapter(live_config) as adapter:
        positions = await adapter.fetch_positions()

    assert isinstance(positions, list)
    print(f"\nFlex positions: {len(positions)}")
    for p in positions:
        print(f"  {p.symbol}: qty={p.quantity}, pnl={p.unrealised_pnl:.2f}")
