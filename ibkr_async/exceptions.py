"""
Custom exception hierarchy for the ibkr_async module.

Design principles:
- Domain-level exceptions only — ib_async internals never leak through
- All ib_async/socket exceptions are caught at the adapter boundary and
  re-raised as one of these typed domain exceptions
- Use the most specific exception; callers can catch IBKRError for a broad net
"""


class IBKRError(Exception):
    """Base exception for all ibkr_async errors."""


class IBKRConnectionError(IBKRError):
    """
    Raised when a connection to TWS/IB Gateway cannot be established
    or is lost during operation.

    Common causes:
    - TWS/IB Gateway not running on the expected host:port
    - API not enabled: Configure → API → Settings → Enable Socket Clients
    - Client ID already in use by another process
    - Read-only mode preventing requested operation
    """


class IBKRTimeoutError(IBKRError):
    """
    Raised when an IBKR API call does not complete within the timeout.

    Common causes:
    - Market data subscription inactive (use reqMarketDataType(3) for delayed)
    - IBKR server overload during peak hours
    - Flex Query statement taking longer than usual to generate
    """


class IBKRDataError(IBKRError):
    """
    Raised when IBKR returns data that cannot be parsed or is structurally
    invalid (e.g. malformed XML, unexpected field values).
    """


class IBKRNotAvailableError(IBKRError):
    """
    Raised when the ib_async library is not installed.

    Resolution::

        pip install ib_async
    """


class IBKRAuthError(IBKRError):
    """
    Raised when authentication fails, such as when TWS requires
    two-factor re-authentication or the Flex token is invalid.
    """
