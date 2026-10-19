"""
Lightweight TCP reachability check for TWS/IB Gateway.

This module provides a quick, non-blocking way to test whether
TWS or IB Gateway is accepting connections — without instantiating
the full ib_async IB object.

This is useful for:
- Health checks in the ConnectionManager
- Benchmark comparisons (pre-flight TWS availability check)
- CI/CD gates that skip integration tests when TWS is offline
"""

from __future__ import annotations

import asyncio
import socket
from typing import Optional

from ..logger import get_logger

log = get_logger(__name__)


async def is_tws_reachable(
    host: str = "127.0.0.1",
    port: int = 7496,
    timeout: float = 3.0,
) -> bool:
    """
    Attempt a TCP connection to TWS/IB Gateway and return True if successful.

    This is a lightweight probe that doesn't require ib_async to be installed.
    It opens a socket, checks that the port accepts connections, then closes
    immediately — no IBKR API handshake is performed.

    Args:
        host:    Hostname where TWS/IB Gateway is running (default: 127.0.0.1)
        port:    Port to probe (default: 7496 live TWS)
        timeout: Socket connect timeout in seconds (default: 3.0)

    Returns:
        True  — port is open and accepting connections.
        False — connection refused, host unreachable, or timed out.
    """
    try:
        # Use asyncio for non-blocking TCP connection check
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        log.debug("tws_tcp_reachable", host=host, port=port)
        return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as exc:
        log.debug("tws_tcp_unreachable", host=host, port=port, reason=str(exc))
        return False


def is_tws_reachable_sync(
    host: str = "127.0.0.1",
    port: int = 7496,
    timeout: float = 3.0,
) -> bool:
    """
    Synchronous version of is_tws_reachable() for use in non-async contexts
    (e.g. pytest fixtures, benchmark scripts).

    Uses stdlib socket directly (no asyncio required).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        log.debug("tws_tcp_reachable_sync", host=host, port=port)
        return True
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        log.debug("tws_tcp_unreachable_sync", host=host, port=port, reason=str(exc))
        return False
