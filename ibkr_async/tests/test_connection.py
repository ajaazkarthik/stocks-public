"""
Unit tests for ConnectionManager and health utilities.

Tests connection lifecycle, retry logic, health monitoring,
and the TCP reachability probe.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from ibkr_async.config import IBKRConfig
from ibkr_async.connection.manager import ConnectionManager
from ibkr_async.connection.health import is_tws_reachable, is_tws_reachable_sync
from ibkr_async.exceptions import IBKRConnectionError
from ibkr_async.models import ConnectionStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_tws_adapter_mock(connected: bool = True) -> MagicMock:
    """Create a MagicMock TWSAdapter with async connect/disconnect."""
    adapter = MagicMock(name="TWSAdapter")
    adapter.connect    = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.health_check = AsyncMock()

    health = MagicMock()
    health.is_healthy    = connected
    health.is_connected  = connected
    health.status        = ConnectionStatus.CONNECTED if connected else ConnectionStatus.DISCONNECTED
    health.latency_ms    = 0.5
    health.account_id    = "DU12345"
    health.error_message = ""
    adapter.health_check.return_value = health
    return adapter


# ── ConnectionManager: start / stop ──────────────────────────────────────────

class TestConnectionManagerStartStop:

    @pytest.mark.asyncio
    async def test_start_creates_and_connects_adapter(self, ibkr_config):
        """start() creates a TWSAdapter and calls connect()."""
        mock_adapter = _make_tws_adapter_mock()

        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            mgr = ConnectionManager(ibkr_config, health_check_interval=0)
            await mgr.start()

        mock_adapter.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_exposes_adapter_property(self, ibkr_config):
        mock_adapter = _make_tws_adapter_mock()
        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            async with ConnectionManager(ibkr_config, health_check_interval=0) as mgr:
                assert mgr.adapter is mock_adapter

    @pytest.mark.asyncio
    async def test_stop_calls_disconnect(self, ibkr_config):
        mock_adapter = _make_tws_adapter_mock()
        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            mgr = ConnectionManager(ibkr_config, health_check_interval=0)
            await mgr.start()
            await mgr.stop()

        mock_adapter.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_context_manager_start_stop(self, ibkr_config):
        mock_adapter = _make_tws_adapter_mock()
        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            async with ConnectionManager(ibkr_config, health_check_interval=0) as mgr:
                assert mgr.adapter is mock_adapter

        mock_adapter.connect.assert_awaited_once()
        mock_adapter.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_adapter_property_raises_when_not_started(self, ibkr_config):
        mgr = ConnectionManager(ibkr_config)
        with pytest.raises(IBKRConnectionError, match="not started"):
            _ = mgr.adapter


# ── ConnectionManager: retry logic ───────────────────────────────────────────

class TestConnectionManagerRetry:

    @pytest.mark.asyncio
    async def test_retry_on_initial_connect_failure(self, ibkr_config):
        """Retries up to max_reconnect_attempts before raising."""
        cfg = IBKRConfig.for_testing()
        # for_testing has max_reconnect_attempts=1, reconnect_delay=0.1

        mock_adapter = _make_tws_adapter_mock()
        mock_adapter.connect = AsyncMock(side_effect=IBKRConnectionError("refused"))

        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            mgr = ConnectionManager(cfg, health_check_interval=0)
            with pytest.raises(IBKRConnectionError, match="Failed to connect"):
                await mgr.start()

        assert mock_adapter.connect.await_count == cfg.max_reconnect_attempts

    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self, ibkr_config):
        """If first attempt fails but second succeeds, start() returns normally."""
        cfg = IBKRConfig(
            host="127.0.0.1", port=7497, client_id=99,
            timeout=5, readonly=True, paper_trading=True,
            flex_token="t", flex_query_id="q",
            max_reconnect_attempts=3, reconnect_delay_seconds=0.01,
        )

        call_count = {"n": 0}

        async def _connect():
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise IBKRConnectionError("first attempt fails")

        mock_adapter = _make_tws_adapter_mock()
        mock_adapter.connect = AsyncMock(side_effect=_connect)

        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            mgr = ConnectionManager(cfg, health_check_interval=0)
            await mgr.start()  # Should not raise

        assert call_count["n"] == 2
        await mgr.stop()


# ── ConnectionManager: health check ──────────────────────────────────────────

class TestConnectionManagerHealthCheck:

    @pytest.mark.asyncio
    async def test_health_check_when_connected(self, ibkr_config):
        mock_adapter = _make_tws_adapter_mock(connected=True)
        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            async with ConnectionManager(ibkr_config, health_check_interval=0) as mgr:
                health = await mgr.health_check()

        assert health.is_connected is True
        mock_adapter.health_check.assert_awaited()

    @pytest.mark.asyncio
    async def test_health_check_not_started_returns_disconnected(self, ibkr_config):
        mgr = ConnectionManager(ibkr_config)
        health = await mgr.health_check()
        assert health.is_connected is False
        assert "not started" in health.error_message


# ── TCP health probe ──────────────────────────────────────────────────────────

class TestTCPHealthProbe:

    @pytest.mark.asyncio
    async def test_is_tws_reachable_when_port_open(self):
        """Simulate port open — asyncio.open_connection succeeds."""
        reader = MagicMock()
        writer = MagicMock()                    # sync mock: close() is sync
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        async def _fake_open(*args, **kwargs):
            return reader, writer

        with patch(
            "ibkr_async.connection.health.asyncio.open_connection",
            side_effect=_fake_open,
        ):
            result = await __import__(
                "ibkr_async.connection.health", fromlist=["is_tws_reachable"]
            ).is_tws_reachable("127.0.0.1", 7496, timeout=2.0)
            assert result is True

    def test_is_tws_reachable_sync_refused(self):
        """is_tws_reachable_sync returns False on connection refused."""
        # Port 1 is almost certainly closed/refused
        result = is_tws_reachable_sync("127.0.0.1", port=1, timeout=0.5)
        assert result is False

    def test_is_tws_reachable_sync_unreachable_host(self):
        """is_tws_reachable_sync returns False on unreachable host."""
        result = is_tws_reachable_sync("192.0.2.1", port=7496, timeout=0.5)
        assert result is False


# ── Stop after disconnect called on exception ──────────────────────────────────

class TestConnectionManagerContextException:

    @pytest.mark.asyncio
    async def test_stop_called_on_body_exception(self, ibkr_config):
        mock_adapter = _make_tws_adapter_mock()
        with patch("ibkr_async.connection.manager.TWSAdapter", return_value=mock_adapter):
            with pytest.raises(ValueError):
                async with ConnectionManager(ibkr_config, health_check_interval=0):
                    raise ValueError("test")

        mock_adapter.disconnect.assert_awaited_once()
