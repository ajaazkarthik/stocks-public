"""
TQS v2 Exception Hierarchy
============================
Clear, categorised exceptions for every failure mode.
"""

from __future__ import annotations


class TQSv2Error(Exception):
    """Base exception for all TQS v2 errors."""

    def __init__(self, message: str, run_id: str = "") -> None:
        super().__init__(message)
        self.run_id = run_id


# ── Data fetching errors ─────────────────────────────────────────────────────

class DataFetchError(TQSv2Error):
    """Raised when an API call fails after all retries."""


class TVScanError(DataFetchError):
    """TradingView stock scanner call failed."""


class TVOptionsScan2Error(DataFetchError):
    """TradingView options scan2 call failed."""


class TVCookieError(TQSv2Error):
    """TV cookie file missing or malformed."""


class YFinanceError(DataFetchError):
    """yfinance OI enrichment failed."""


class IVStructureError(DataFetchError):
    """in-time-iv API call failed."""


class VoiLensError(DataFetchError):
    """voi-lens API call failed."""


class VolatilityChartError(DataFetchError):
    """volatility-chart API call failed."""


class WebSocketError(DataFetchError):
    """OPRA WebSocket connection or subscription failed."""


# ── Processing errors ────────────────────────────────────────────────────────

class ScoringError(TQSv2Error):
    """Raised when contract scoring fails unexpectedly."""


class RankingError(TQSv2Error):
    """Raised when ranking pipeline fails."""


class FilterError(TQSv2Error):
    """Raised when a filter encounters invalid data."""


# ── Reporting errors ─────────────────────────────────────────────────────────

class ReportingError(TQSv2Error):
    """Raised when report generation fails."""


class PersistenceError(TQSv2Error):
    """Raised when database write/read fails."""


# ── Orchestration errors ─────────────────────────────────────────────────────

class OrchestratorError(TQSv2Error):
    """Raised when the main orchestrator encounters an unrecoverable error."""


class ConfigurationError(TQSv2Error):
    """Raised when configuration is invalid."""
