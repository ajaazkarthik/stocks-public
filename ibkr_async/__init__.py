"""
ibkr_async — Live IBKR data layer via ib_async TWS socket
==========================================================

This module is intentionally isolated from the rest of the trading system.
It must NOT import from any other module in this repository.

It provides clean, testable adapters over the ib_async library, implementing
the IBKRDataPort and IBKROrderPort hexagonal-architecture interfaces.

Quick start (async)::

    from ibkr_async import TWSAdapter, IBKRConfig

    config = IBKRConfig.from_env()
    adapter = TWSAdapter(config)

    async def main():
        async with adapter:
            positions = await adapter.fetch_positions()
            account   = await adapter.fetch_account_summary()

Quick start (Flex Query — no TWS required)::

    from ibkr_async import FlexAdapter, IBKRConfig

    config = IBKRConfig.from_env()
    async with FlexAdapter(config) as adapter:
        positions = await adapter.fetch_positions()

Adapters
--------
- TWSAdapter  : Real-time data via ib_async live socket (~100-500ms latency)
- FlexAdapter : Batch data via IBKR Flex Query HTTPS (~30-300s, T+1)

See SPEC.md for full architecture documentation.
"""

from .config import IBKRConfig
from .exceptions import (
    IBKRAuthError,
    IBKRConnectionError,
    IBKRDataError,
    IBKRError,
    IBKRNotAvailableError,
    IBKRTimeoutError,
)
from .models import AccountSummary, AssetClass, ConnectionStatus, HealthStatus, Position, Quote
from .ports import IBKRDataPort, IBKROrderPort
from .adapters.tws_adapter import TWSAdapter
from .adapters.flex_adapter import FlexAdapter

__version__ = "1.0.0"
__author__ = "stocks trading system"

__all__ = [
    # Configuration
    "IBKRConfig",
    # Exceptions
    "IBKRError",
    "IBKRConnectionError",
    "IBKRTimeoutError",
    "IBKRDataError",
    "IBKRNotAvailableError",
    "IBKRAuthError",
    # Domain models
    "Position",
    "AccountSummary",
    "Quote",
    "HealthStatus",
    "AssetClass",
    "ConnectionStatus",
    # Ports (interfaces)
    "IBKRDataPort",
    "IBKROrderPort",
    # Adapters
    "TWSAdapter",
    "FlexAdapter",
]
