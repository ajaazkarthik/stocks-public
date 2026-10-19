"""
Configuration for the ibkr_async module.

All settings are driven by environment variables with sensible defaults,
enabling Docker/CI/production deployments without code changes.

Environment variables
---------------------
IBKR_HOST                  TWS/IB Gateway hostname         (default: 127.0.0.1)
IBKR_PORT                  TWS/IB Gateway port             (default: 7496 live)
IBKR_CLIENT_ID             API client ID, unique per conn  (default: 10)
IBKR_TIMEOUT               Connection timeout (seconds)    (default: 30)
IBKR_READONLY              Prevent order placement         (default: 0)
IBKR_PAPER_TRADING         Use paper trading port 7497     (default: 0)
IBKR_MARKET_DATA_TYPE      1=live, 2=frozen, 3=delayed     (default: 3)
IBKR_FLEX_TOKEN            Flex Query Web Service token
IBKR_FLEX_QUERY_ID         Flex Query ID
IBKR_MAX_RECONNECT         Max reconnect attempts          (default: 3)
IBKR_RECONNECT_DELAY       Seconds between reconnects      (default: 5.0)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ── IBKR port conventions ─────────────────────────────────────────────────────
PORT_TWS_LIVE   = 7496
PORT_TWS_PAPER  = 7497
PORT_GW_LIVE    = 4001
PORT_GW_PAPER   = 4002

# ── Default Flex credentials (from existing trading system config) ─────────────
# These match cgt/ibkr-cgt/ibkr_cgt_analysis.py FLEX_TOKEN / FLEX_QUERY_ID.
# Override via IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID environment variables.
_DEFAULT_FLEX_TOKEN    = "581369287293302825272191"
_DEFAULT_FLEX_QUERY_ID = "1462304"


@dataclass
class IBKRConfig:
    """
    All configuration for connecting to IBKR TWS, IB Gateway, or Flex Query.

    Instantiation
    -------------
    Production:  ``IBKRConfig.from_env()``
    Testing:     ``IBKRConfig.for_testing()``
    Explicit:    ``IBKRConfig(host='127.0.0.1', port=7496, client_id=1)``
    """
    # TWS / IB Gateway connection
    host:      str  = "127.0.0.1"
    port:      int  = PORT_TWS_LIVE
    client_id: int  = 10
    timeout:   int  = 30
    readonly:  bool = False

    # Flex Query credentials
    flex_token:    str = _DEFAULT_FLEX_TOKEN
    flex_query_id: str = _DEFAULT_FLEX_QUERY_ID

    # Trading mode
    paper_trading: bool = False

    # Market data: 1=real-time, 2=frozen, 3=delayed, 4=delayed-frozen
    market_data_type: int = 3

    # Reconnect policy
    max_reconnect_attempts: int   = 3
    reconnect_delay_seconds: float = 5.0

    # ── Factories ──────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "IBKRConfig":
        """
        Create config from environment variables.
        Unset variables fall back to their default values.
        """
        paper = os.environ.get("IBKR_PAPER_TRADING", "0") == "1"
        default_port = PORT_TWS_PAPER if paper else PORT_TWS_LIVE

        return cls(
            host=os.environ.get("IBKR_HOST", "127.0.0.1"),
            port=int(os.environ.get("IBKR_PORT", str(default_port))),
            client_id=int(os.environ.get("IBKR_CLIENT_ID", "")),
            timeout=int(os.environ.get("IBKR_TIMEOUT", "30")),
            readonly=os.environ.get("IBKR_READONLY", "0") == "1",
            flex_token=os.environ.get("IBKR_FLEX_TOKEN", _DEFAULT_FLEX_TOKEN),
            flex_query_id=os.environ.get("IBKR_FLEX_QUERY_ID", _DEFAULT_FLEX_QUERY_ID),
            paper_trading=paper,
            market_data_type=int(os.environ.get("IBKR_MARKET_DATA_TYPE", "3")),
            max_reconnect_attempts=int(os.environ.get("IBKR_MAX_RECONNECT", "3")),
            reconnect_delay_seconds=float(os.environ.get("IBKR_RECONNECT_DELAY", "5.0")),
        )

    @classmethod
    def for_testing(cls) -> "IBKRConfig":
        """Convenience factory for unit tests (paper mode, short timeout, client 99)."""
        return cls(
            host="127.0.0.1",
            port=PORT_TWS_PAPER,
            client_id=99,
            timeout=5,
            readonly=True,
            paper_trading=True,
            flex_token="test_flex_token",
            flex_query_id="test_query_id",
            market_data_type=3,
            max_reconnect_attempts=1,
            reconnect_delay_seconds=0.1,
        )

    @classmethod
    def for_paper(cls) -> "IBKRConfig":
        """Factory for paper trading (port 7497, TWS paper)."""
        return cls(
            port=PORT_TWS_PAPER,
            paper_trading=True,
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    @property
    def connection_string(self) -> str:
        """Human-readable connection description."""
        mode = "paper" if self.paper_trading else "live"
        return f"{self.host}:{self.port} (client_id={self.client_id}, {mode})"

    def to_dict(self) -> dict:
        """
        Serialise for structured logging.
        The flex_token is truncated to prevent credential leakage in logs.
        """
        token = self.flex_token
        masked_token = f"...{token[-6:]}" if len(token) > 6 else "***"
        return {
            "host":              self.host,
            "port":              self.port,
            "client_id":         self.client_id,
            "timeout":           self.timeout,
            "readonly":          self.readonly,
            "paper":             self.paper_trading,
            "market_data_type":  self.market_data_type,
            "flex_query_id":     self.flex_query_id,
            "flex_token":        masked_token,
        }
