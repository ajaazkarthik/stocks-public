"""
ConnectionManager — full TWSAdapter lifecycle with auto-reconnect.

Wraps TWSAdapter with:
- Configurable retry on initial connect
- Automatic reconnect when TWS drops the connection
- Health monitoring background task
- Clean async context manager interface

Usage
-----
::

    from ibkr_async import IBKRConfig
    from ibkr_async.connection import ConnectionManager

    config = IBKRConfig.from_env()

    async with ConnectionManager(config) as mgr:
        adapter = mgr.adapter
        positions = await adapter.fetch_positions()
        account   = await adapter.fetch_account_summary()

Design
------
- The manager owns the TWSAdapter instance
- On connect(), it retries up to config.max_reconnect_attempts times
- It exposes adapter as a property; caller uses it normally
- On disconnect(), it shuts down gracefully and cancels the health-check task
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Optional

from ..adapters.tws_adapter import TWSAdapter
from ..config import IBKRConfig
from ..exceptions import IBKRConnectionError
from ..logger import get_logger
from ..models import ConnectionStatus, HealthStatus

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

log = get_logger(__name__)


class ConnectionManager:
    """
    Manages the complete lifecycle of a TWSAdapter connection.

    Implements retry on initial connect and optional health-monitor
    that periodically checks if the connection is still alive.

    Args:
        config:             IBKRConfig (default: IBKRConfig.from_env())
        health_check_interval: Seconds between background health checks.
                                Set to 0 to disable the health monitor.
    """

    def __init__(
        self,
        config: Optional[IBKRConfig] = None,
        health_check_interval: float = 60.0,
    ) -> None:
        self._config   = config or IBKRConfig.from_env()
        self._adapter: Optional[TWSAdapter] = None
        self._health_task: Optional[asyncio.Task] = None
        self._health_interval = health_check_interval
        self._last_health: Optional[HealthStatus] = None
        self._shutdown = False

        log.info(
            "connection_manager_created",
            host=self._config.host,
            port=self._config.port,
            max_attempts=self._config.max_reconnect_attempts,
            health_interval_s=health_check_interval,
        )

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "ConnectionManager":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def adapter(self) -> TWSAdapter:
        """Return the live TWSAdapter. Raises if not started."""
        if self._adapter is None:
            raise IBKRConnectionError(
                "ConnectionManager not started. "
                "Use `async with ConnectionManager(...) as mgr:` or call `await mgr.start()`."
            )
        return self._adapter

    @property
    def last_health(self) -> Optional[HealthStatus]:
        """Most recent health status from the background monitor."""
        return self._last_health

    async def start(self) -> None:
        """
        Initialise the TWSAdapter and connect with retry.

        Raises:
            IBKRConnectionError: If all retry attempts fail.
        """
        self._shutdown = False
        self._adapter = TWSAdapter(self._config)

        last_exc: Optional[Exception] = None
        attempts = self._config.max_reconnect_attempts

        for attempt in range(1, attempts + 1):
            try:
                log.info(
                    "manager_connect_attempt",
                    attempt=attempt,
                    max_attempts=attempts,
                )
                await self._adapter.connect()

                # Start health-check task if interval > 0
                if self._health_interval > 0:
                    self._health_task = asyncio.create_task(
                        self._health_monitor(), name="ibkr_health_monitor"
                    )

                log.info("manager_connected", attempt=attempt)
                return

            except Exception as exc:
                last_exc = exc
                log.warning(
                    "manager_connect_failed",
                    attempt=attempt,
                    max_attempts=attempts,
                    error=str(exc),
                )
                if attempt < attempts:
                    delay = self._config.reconnect_delay_seconds
                    log.info("manager_reconnect_wait", delay_s=delay)
                    await asyncio.sleep(delay)

        raise IBKRConnectionError(
            f"Failed to connect after {attempts} attempts. "
            f"Last error: {last_exc}"
        )

    async def stop(self) -> None:
        """Disconnect and clean up the health monitor task."""
        self._shutdown = True

        # Cancel health monitor
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except (asyncio.CancelledError, Exception):
                pass
            self._health_task = None

        # Disconnect adapter
        if self._adapter is not None:
            await self._adapter.disconnect()
            self._adapter = None

        log.info("manager_stopped")

    async def health_check(self) -> HealthStatus:
        """Return current health. Never raises."""
        if self._adapter is None:
            return HealthStatus(
                is_connected  = False,
                status        = ConnectionStatus.DISCONNECTED,
                host          = self._config.host,
                port          = self._config.port,
                client_id     = self._config.client_id,
                error_message = "ConnectionManager not started",
                checked_at    = datetime.now(timezone.utc),
            )
        return await self._adapter.health_check()

    # ── Private: health monitor background task ───────────────────────────────

    async def _health_monitor(self) -> None:
        """
        Background task: periodic health check.
        Logs a warning if the connection is lost (does not auto-reconnect
        in this version — reconnect logic can be added here if needed).
        """
        while not self._shutdown:
            try:
                await asyncio.sleep(self._health_interval)
                if self._shutdown:
                    break

                health = await self.health_check()
                self._last_health = health

                if health.is_healthy:
                    log.debug(
                        "health_check_ok",
                        latency_ms=health.latency_ms,
                        account_id=health.account_id,
                    )
                else:
                    log.warning(
                        "health_check_unhealthy",
                        status=health.status.value,
                        error=health.error_message,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("health_monitor_error", error=str(exc))
