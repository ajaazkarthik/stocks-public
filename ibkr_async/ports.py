"""
Abstract interface definitions (Ports) for the ibkr_async module.

Hexagonal Architecture — Ports define WHAT the application needs;
Adapters define HOW to deliver it.

Interface Segregation (SOLID-I):
- IBKRDataPort  — read-only: positions, account info, quotes, health
- IBKROrderPort — write: place orders, cancel, query open orders

Not all adapters implement both ports:
- TWSAdapter  : implements IBKRDataPort + IBKROrderPort (full)
- FlexAdapter : implements IBKRDataPort only (batch, no live orders)

The application layer depends ONLY on these abstract ports, never on
TWSAdapter or FlexAdapter directly.  This enables:
- Swapping adapters without changing application code
- Mocking in tests without ib_async installed
- Future adapters (e.g. IBKR Client Portal REST) with zero app changes
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import AccountSummary, HealthStatus, Position, Quote


class IBKRDataPort(ABC):
    """
    Port for reading market data and portfolio state from IBKR.

    Implementations: TWSAdapter (live), FlexAdapter (batch T+1).
    """

    @abstractmethod
    async def connect(self) -> None:
        """
        Establish connection to the data source.

        Raises:
            IBKRConnectionError: If connection is refused or cannot be established.
            IBKRTimeoutError:    If connection times out.
            IBKRNotAvailableError: If ib_async library is not installed (TWSAdapter only).
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the connection and release resources."""

    @abstractmethod
    async def fetch_positions(self) -> list[Position]:
        """
        Fetch all currently open positions.

        Returns:
            List of Position objects. Empty list if no positions are held.

        Raises:
            IBKRConnectionError: If not connected.
            IBKRDataError:       If data cannot be parsed.
        """

    @abstractmethod
    async def fetch_account_summary(self) -> AccountSummary:
        """
        Fetch account-level financial summary (cash, equity, P&L).

        Returns:
            AccountSummary with current balances.

        Raises:
            IBKRConnectionError: If not connected.
            IBKRDataError:       If data cannot be parsed.
        """

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """
        Return current connectivity health status.

        This method MUST NOT raise — it should always return a HealthStatus
        even if the connection is down. Errors are reported within the object.
        """

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True if the adapter currently has an active connection."""

    @property
    @abstractmethod
    def supports_live_quotes(self) -> bool:
        """
        True if this adapter can stream real-time market quotes.
        False for FlexAdapter (batch-only, no quote streaming).
        """

    # ── Context manager protocol ──────────────────────────────────────────────

    async def __aenter__(self) -> "IBKRDataPort":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()


class IBKROrderPort(ABC):
    """
    Port for placing and managing orders at IBKR.

    Only TWSAdapter implements this — FlexAdapter is read-only.

    Security notes:
    - Implementations MUST check IBKRConfig.readonly before any write operation
    - The interface is intentionally minimal: only open_orders and cancel_order
      are exposed here. Extend only when there is an explicit business requirement.
    """

    @abstractmethod
    async def fetch_open_orders(self) -> list[dict]:
        """
        Return currently open (unfilled) orders.

        Returns:
            List of dicts with keys:
            ``order_id``, ``symbol``, ``action``, ``quantity``,
            ``order_type``, ``lmt_price``, ``status``

        Raises:
            IBKRConnectionError: If not connected.
            IBKRDataError:       If order data cannot be retrieved.
        """

    @abstractmethod
    async def cancel_order(self, order_id: int) -> bool:
        """
        Cancel an open order.

        Args:
            order_id: IBKR order ID (integer) to cancel.

        Returns:
            True  — cancellation was acknowledged by IBKR.
            False — order not found, or readonly mode prevented it.

        Note:
            Does NOT block until the order is confirmed cancelled.
            Monitor via fetch_open_orders() for final confirmation.
        """
