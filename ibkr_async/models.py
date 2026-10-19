"""
Pure domain models for the ibkr_async module.

These are framework-agnostic frozen dataclasses — they carry data and have
NO business logic, NO external dependencies, and NO I/O.

They form the data contract between the adapters and the application layer.

Design principles:
- Frozen (immutable) where fields should not change after construction
- All optional fields have sensible zero-value defaults
- Timestamps are always UTC-aware datetime objects
- Currency is always uppercase ISO 4217 (e.g. "USD", "AUD", "HKD")
- to_dict() mirrors the shape expected by global_portfolio_dashboard.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


# ── Enumerations ──────────────────────────────────────────────────────────────

class AssetClass(str, Enum):
    """Enumeration of IBKR-supported asset classes."""
    STOCK   = "STK"
    OPTION  = "OPT"
    FUTURE  = "FUT"
    FOREX   = "CASH"
    INDEX   = "IND"
    BOND    = "BOND"
    FUND    = "FUND"
    UNKNOWN = "UNKNOWN"


class ConnectionStatus(str, Enum):
    """Connection state of an IBKR adapter."""
    CONNECTED     = "CONNECTED"
    DISCONNECTED  = "DISCONNECTED"
    CONNECTING    = "CONNECTING"
    RECONNECTING  = "RECONNECTING"
    FAILED        = "FAILED"


# ── Domain models ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Position:
    """
    A single open position held at IBKR.

    Populated from ib_async PortfolioItem (live, real-time) or from
    Flex Query OpenPosition XML elements (T+1 batch).

    The ``to_dict()`` output is compatible with the position dicts consumed
    by global_portfolio_dashboard.py (``broker``, ``symbol``, ``qty``, etc.)
    """
    symbol:          str
    exchange:        str
    currency:        str
    asset_class:     AssetClass
    quantity:        float
    avg_cost:        float
    market_price:    float
    market_value:    float
    unrealised_pnl:  float
    realised_pnl:    float
    timestamp:       datetime    = field(default_factory=_utcnow)
    account_id:      str         = ""
    broker:          str         = "IBKR"

    @property
    def pnl_pct(self) -> float:
        """Unrealised P&L as a percentage of cost basis."""
        cost_basis = abs(self.avg_cost * self.quantity)
        if cost_basis < 1e-9:
            return 0.0
        return (self.unrealised_pnl / cost_basis) * 100.0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    def to_dict(self) -> dict:
        """
        Return a dict compatible with the global_portfolio_dashboard position format.
        """
        return {
            "symbol":         self.symbol,
            "exchange":       self.exchange,
            "currency":       self.currency,
            "qty":            self.quantity,
            "avg_cost":       self.avg_cost,
            "last_price":     self.market_price,
            "market_value":   self.market_value,
            "unrealised_pnl": self.unrealised_pnl,
            "realised_pnl":   self.realised_pnl,
            "pnl_pct":        round(self.pnl_pct, 2),
            "broker":         self.broker,
            "account_id":     self.account_id,
            "timestamp":      self.timestamp.isoformat(),
        }


@dataclass(frozen=True)
class AccountSummary:
    """
    Account-level financial summary from IBKR.

    Retrieved via ib_async IB.accountSummary() (live, near-real-time) or
    from EquitySummaryInBase XML in a Flex Query statement (T+1).
    """
    account_id:       str
    net_liquidation:  float
    total_cash:       float
    unrealised_pnl:   float
    realised_pnl:     float
    equity_with_loan: float    = 0.0
    available_funds:  float    = 0.0
    excess_liquidity: float    = 0.0
    currency:         str      = "USD"
    timestamp:        datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return {
            "account_id":        self.account_id,
            "net_liquidation":   self.net_liquidation,
            "total_cash":        self.total_cash,
            "unrealised_pnl":    self.unrealised_pnl,
            "realised_pnl":      self.realised_pnl,
            "equity_with_loan":  self.equity_with_loan,
            "available_funds":   self.available_funds,
            "excess_liquidity":  self.excess_liquidity,
            "currency":          self.currency,
            "timestamp":         self.timestamp.isoformat(),
        }


@dataclass(frozen=True)
class Quote:
    """
    Real-time market quote for a single instrument.

    Only available via TWSAdapter (live socket streaming).
    Not available via FlexAdapter — Flex Query is batch-only.
    """
    symbol:    str
    exchange:  str
    currency:  str
    bid:       float
    ask:       float
    last:      float
    bid_size:  float    = 0.0
    ask_size:  float    = 0.0
    volume:    float    = 0.0
    timestamp: datetime = field(default_factory=_utcnow)

    @property
    def mid(self) -> float:
        """Mid-point of bid/ask spread."""
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_bps(self) -> float:
        """Bid-ask spread in basis points (relative to mid price)."""
        if self.mid < 1e-9:
            return 0.0
        return ((self.ask - self.bid) / self.mid) * 10_000.0


@dataclass(frozen=True)
class HealthStatus:
    """Connectivity health report produced by an IBKRDataPort.health_check() call."""
    is_connected:  bool
    status:        ConnectionStatus
    host:          str
    port:          int
    client_id:     int
    latency_ms:    Optional[float] = None
    account_id:    str             = ""
    error_message: str             = ""
    checked_at:    datetime        = field(default_factory=_utcnow)

    @property
    def is_healthy(self) -> bool:
        return self.is_connected and self.status == ConnectionStatus.CONNECTED

    def to_dict(self) -> dict:
        return {
            "is_connected":  self.is_connected,
            "status":        self.status.value,
            "host":          self.host,
            "port":          self.port,
            "latency_ms":    self.latency_ms,
            "account_id":    self.account_id,
            "error_message": self.error_message,
            "checked_at":    self.checked_at.isoformat(),
        }
