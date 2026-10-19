"""
TQS v2 Interfaces — Abstract contracts for all major components.
================================================================
Following the Interface Segregation Principle — each interface is small
and focused. Implementations can be swapped without touching callers.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..market_data.models import StockRecord
    from ..options_data.models import RawContract, OptionContract
    from ..scoring.models import ScoredContract
    from ..ranking.models import RankedContract
    from ..config import TQSv2Config


# ── Market Data Interface ─────────────────────────────────────────────────────

class IStockScreener(ABC):
    """Fetches the quality stock universe to scan options for."""

    @abstractmethod
    def fetch_quality_stocks(
        self,
        config: "TQSv2Config",
    ) -> list["StockRecord"]:
        """
        Return a list of quality stocks meeting the configured criteria.

        Raises:
            TVScanError: if the screener API call fails.
            TVCookieError: if the TV session cookie is missing/expired.
        """


# ── Options Fetcher Interface ─────────────────────────────────────────────────

class IOptionsFetcher(ABC):
    """Fetches raw option contracts for a given underlying stock."""

    @abstractmethod
    def fetch_contracts(
        self,
        stock: "StockRecord",
        option_type: str,    # "call" or "put"
        config: "TQSv2Config",
    ) -> list["RawContract"]:
        """
        Return raw contracts for the stock from the options chain.

        Args:
            stock: the underlying stock record (provides symbol + close price)
            option_type: "call" or "put"
            config: pipeline configuration

        Raises:
            TVOptionsScan2Error: if the scan2 API call fails.
        """


# ── OI Enricher Interface ─────────────────────────────────────────────────────

class IOIEnricher(ABC):
    """Enriches contracts with real Open Interest from yfinance."""

    @abstractmethod
    def enrich(
        self,
        contracts: list["RawContract"],
        config: "TQSv2Config",
    ) -> list["RawContract"]:
        """
        Attach `open_interest` to each contract in-place and return the list.
        Contracts for which OI is unavailable receive open_interest=None.

        Raises:
            YFinanceError: on unrecoverable yfinance failure.
        """


# ── IV Enricher Interface ─────────────────────────────────────────────────────

class IIVEnricher(ABC):
    """Enriches contracts with IV term structure / smile data."""

    @abstractmethod
    def enrich(
        self,
        symbol: str,
        exchange: str,
        config: "TQSv2Config",
    ) -> dict:
        """
        Return a dict of enrichment data for the given symbol.
        Keys depend on the enricher type (e.g. term_slope, smile_skew).
        Returns {} on failure (non-blocking — enrichment is optional).
        """


# ── Contract Scorer Interface ────────────────────────────────────────────────

class IContractScorer(ABC):
    """Scores a single OptionContract on a specific dimension (0-100)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this scorer (e.g. 'liquidity')."""

    @abstractmethod
    def score(
        self,
        contract: "OptionContract",
    ) -> float:
        """
        Return a score in the range [0, 100].
        Must never raise — return 0.0 on any error.
        """


# ── Ranker Interface ─────────────────────────────────────────────────────────

class IRanker(ABC):
    """Ranks a list of scored contracts and returns ordered results."""

    @abstractmethod
    def rank(
        self,
        scored: list["ScoredContract"],
        top_per_stock: int,
        top_global: int,
    ) -> list["RankedContract"]:
        """
        Return globally ranked list of contracts.

        Args:
            scored: all scored contracts
            top_per_stock: max contracts per underlying to keep
            top_global: final global top-N to return
        """


# ── Filter Interface ─────────────────────────────────────────────────────────

class IContractFilter(ABC):
    """A single filter gate in the filter pipeline."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for logging."""

    @abstractmethod
    def accepts(self, contract: "RawContract", config: "TQSv2Config") -> bool:
        """Return True if the contract passes this filter."""


# ── Reporter Interface ───────────────────────────────────────────────────────

class IReporter(ABC):
    """Generates a report from ranked contracts."""

    @abstractmethod
    def generate(
        self,
        ranked: list["RankedContract"],
        scan_type: str,          # "CALL" or "PUT"
        run_id: str,
        config: "TQSv2Config",
    ) -> str:
        """
        Generate the report and return the output path as a string.

        Raises:
            ReportingError: if report generation fails.
        """
