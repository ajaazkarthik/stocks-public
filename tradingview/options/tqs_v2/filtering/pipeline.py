"""
TQS v2 Filter Pipeline
=======================
Chains all IContractFilter implementations.
Logs how many contracts each filter rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import TQSv2Config
from ..interfaces.contracts import IContractFilter
from ..options_data.models import RawContract
from .contract_filter import (
    IntrinsicValueFilter,
    BidFilter,
    PremiumFilter,
    SpreadFilter,
    DeltaFilter,
    OpenInterestFilter,
    DTEFilter,
    ATRReachabilityFilter,
    SMA200AlignmentFilter,
    DirectionalBiasFilter,
    VolumeFilter,
    IVExtremesFilter,
    ThetaEfficiencyFilter,
    EarningsProximityFilter,
)

_log = logging.getLogger("tqs_v2.filtering.pipeline")


@dataclass
class FilterResult:
    """Statistics from a filter run."""
    total_in: int
    total_out: int
    rejections: dict[str, int]  # filter_name → count rejected

    @property
    def pass_rate(self) -> float:
        if self.total_in == 0:
            return 0.0
        return self.total_out / self.total_in * 100.0


class FilterPipeline:
    """
    Sequential contract filter pipeline.

    Default filter order:
      0. Intrinsic value sanity (catches split-unadjusted / bad data first)
      1. DTE window      (fast, reduces search space early)
      2. Bid floor       (fast)
      3. Premium cap     (budget gate — default $4 max ask)
      4. Spread          (fast)
      5. Delta range     (fast)
      6. OI floor        (after yfinance enrichment)

    Custom filters can be injected via constructor.
    """

    def __init__(self, filters: list[IContractFilter] | None = None) -> None:
        if filters is not None:
            self._filters = filters
        else:
            self._filters: list[IContractFilter] = [
                IntrinsicValueFilter(),
                DTEFilter(),
                BidFilter(),
                PremiumFilter(),
                SpreadFilter(),
                DeltaFilter(),
                OpenInterestFilter(),
                VolumeFilter(),            # hard gate: rejects near-zero volume (e.g. CMCSA P25 vol=48)
                ATRReachabilityFilter(),
                SMA200AlignmentFilter(),   # hard gate: rejects directional mismatches
                DirectionalBiasFilter(),   # hard gate: SMA50 directional bias — prevents same stock in both calls+puts
                IVExtremesFilter(),        # hard gate: rejects extreme IV + backwardation (vol crush trap; Fix 2026-05-26)
                ThetaEfficiencyFilter(),   # hard gate: rejects contracts where theta eats expected move (C-2 fix 2026-05-27)
                EarningsProximityFilter(),  # hard gate: rejects contracts spanning earnings dates (IV crush risk; Fix 2026-05-28)
            ]

    def apply(
        self,
        contracts: list[RawContract],
        config: TQSv2Config,
    ) -> tuple[list[RawContract], FilterResult]:
        """
        Apply all filters sequentially.
        Returns (surviving_contracts, stats).
        """
        rejected_counts: dict[str, int] = {}
        current = contracts

        for f in self._filters:
            before = len(current)
            current = [c for c in current if f.accepts(c, config)]
            rejected = before - len(current)
            rejected_counts[f.name] = rejected
            if rejected > 0:
                _log.debug(
                    "Filter '%s' rejected %d contracts (%d remaining)",
                    f.name,
                    rejected,
                    len(current),
                )

        result = FilterResult(
            total_in=len(contracts),
            total_out=len(current),
            rejections=rejected_counts,
        )

        _log.info(
            "Filter pipeline: %d → %d contracts (%.1f%% pass rate) | %s",
            result.total_in,
            result.total_out,
            result.pass_rate,
            " | ".join(f"{k}:{v}" for k, v in rejected_counts.items() if v > 0),
        )

        return current, result
