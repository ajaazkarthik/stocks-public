"""
TQS v2 Ranking Engine
======================
Selects the top N contracts per stock and top M globally.

Step 1: Sort all scored contracts by total_score descending.
Step 2: Pick top_per_stock per unique symbol.
Step 3: Apply expiry diversification: max_per_expiry contracts per expiry date.
Step 3.5: Apply sector diversification: max_per_sector contracts per sector (Fix 2026-05-27 M-3).
Step 4: From those, pick top_global overall.
Step 5: Assign rank_per_stock and rank_global numbers.

Fix 2026-05-26 (CRITICAL-4): Added max_per_expiry cap to prevent expiry concentration.
Without it, all results expiring on the same monthly OPEX date creates a single concentrated
gamma event — systematic risk invisible from individual contract scores.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ..config import TQSv2Config
from ..scoring.models import ScoredContract
from .models import RankedContract

_log = logging.getLogger("tqs_v2.ranking.engine")


class RankingEngine:
    """Selects top contracts per stock and globally."""

    def rank(
        self,
        scored: list[ScoredContract],
        config: TQSv2Config,
    ) -> list[RankedContract]:
        """
        Returns ranked contracts.

        Process:
          1. Sort all contracts by score descending.
          2. For each symbol, keep top `top_n_per_stock`.
          3. Apply expiry diversification cap: max `top_n_per_expiry` contracts per expiry date.
          4. From survivors, pick top `top_n_global`.
          5. Assign rank numbers (1 = best).
        """
        if not scored:
            return []

        # Sort globally by score
        sorted_all = sorted(scored, key=lambda s: s.total_score, reverse=True)

        # Per-stock selection: keep top N per symbol
        per_stock_rank: dict[str, int] = defaultdict(int)
        per_stock_selections: list[tuple[ScoredContract, int]] = []  # (sc, rank_per_stock)

        for sc in sorted_all:
            sym = sc.symbol
            per_stock_rank[sym] += 1
            if per_stock_rank[sym] <= config.top_n_per_stock:
                per_stock_selections.append((sc, per_stock_rank[sym]))

        # ── Expiry diversification cap (CRITICAL-4 fix 2026-05-26) ──────────
        # Prevent all results from clustering at the same monthly OPEX date.
        # When the DTE sweet-spot peak falls on one expiry cycle, every stock's
        # best-scoring contract has the same expiration, creating correlated gamma risk.
        max_per_expiry = getattr(config, "top_n_per_expiry", 5)  # default cap: 5 per expiry
        expiry_count: dict[str, int] = defaultdict(int)
        expiry_filtered: list[tuple[ScoredContract, int]] = []

        for sc, stock_rank in sorted(
            per_stock_selections,
            key=lambda x: x[0].total_score,
            reverse=True,
        ):
            expiry_key = str(getattr(sc, "expiry", "") or "")
            if expiry_count[expiry_key] < max_per_expiry:
                expiry_filtered.append((sc, stock_rank))
                expiry_count[expiry_key] += 1
            else:
                _log.debug(
                    "expiry cap rejected %s (expiry=%s, cap=%d)",
                    getattr(sc, "opra_code", "?"),
                    expiry_key,
                    max_per_expiry,
                )

        expiry_counts_str = ", ".join(
            f"{k}={v}" for k, v in sorted(expiry_count.items())
        )
        _log.info("Expiry distribution after cap: %s", expiry_counts_str)

        # ── Sector diversification cap (M-3 fix 2026-05-27) ───────────────
        # Prevent all top contracts from clustering in one sector (e.g., 100% tech).
        max_per_sector = config.max_per_sector  # default 3 per sector
        sector_count: dict[str, int] = defaultdict(int)
        sector_filtered: list[tuple[ScoredContract, int]] = []

        for sc, stock_rank in sorted(
            expiry_filtered,
            key=lambda x: x[0].total_score,
            reverse=True,
        ):
            sector = str(getattr(sc, "sector", "Unknown") or "Unknown")
            if sector_count[sector] < max_per_sector:
                sector_filtered.append((sc, stock_rank))
                sector_count[sector] += 1
            else:
                _log.debug(
                    "sector cap rejected %s (sector=%s, cap=%d)",
                    getattr(sc, "opra_code", "?"),
                    sector,
                    max_per_sector,
                )

        sector_counts_str = ", ".join(
            f"{k}={v}" for k, v in sorted(sector_count.items())
        )
        _log.info("Sector distribution after cap: %s", sector_counts_str)

        # Sort survivors by score and take global top N
        survivors_sorted = sorted(
            sector_filtered,
            key=lambda x: x[0].total_score,
            reverse=True,
        )
        top_global = survivors_sorted[: config.top_n_global]

        # Build ranked list
        ranked: list[RankedContract] = []
        for global_rank, (sc, stock_rank) in enumerate(top_global, start=1):
            ranked.append(
                RankedContract(
                    scored=sc,
                    rank_global=global_rank,
                    rank_per_stock=stock_rank,
                )
            )

        _log.info(
            "Ranking: %d scored → %d after per-stock filter → %d after expiry cap → %d after sector cap → %d final",
            len(scored),
            len(per_stock_selections),
            len(expiry_filtered),
            len(sector_filtered),
            len(ranked),
        )
        return ranked
