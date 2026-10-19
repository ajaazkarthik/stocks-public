"""
TQS v2 Ranking Models
======================
"""
from __future__ import annotations
from dataclasses import dataclass
from ..scoring.models import ScoredContract


@dataclass
class RankedContract:
    """A scored contract with ranking metadata."""
    scored: ScoredContract
    rank_global: int     # 1 = best globally
    rank_per_stock: int  # 1 = best for this symbol

    @property
    def symbol(self) -> str:
        return self.scored.symbol

    @property
    def option_type(self) -> str:
        return self.scored.option_type

    @property
    def total_score(self) -> float:
        return self.scored.total_score

    @property
    def grade(self) -> str:
        return self.scored.grade

    @property
    def contract(self):
        return self.scored.contract
