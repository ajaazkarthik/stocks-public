"""
Leverage module — Dynamic leverage strategy for the tactical 20% bucket.

Modules
-------
  regime_score  : RegimeScore engine (0–10), leverage multiplier
  signals       : Multi-asset scanner, opportunity ranking
  sell_strategy : Tiered exit rules (H1–H4 hard, S1–S4 soft)
  portfolio     : Position sizing, allocation plan

Quick usage
-----------
  from leverage import RegimeEngine, LeverageSellEngine, PortfolioAllocator

  regime  = RegimeEngine().compute("SPY")
  print(regime.score, regime.leverage_multiplier, regime.regime_label)
"""

from leverage.regime_score  import RegimeEngine, RegimeResult, leverage_from_score, regime_label
from leverage.sell_strategy import LeverageSellEngine, LeveragePosition, ExitDecision
from leverage.portfolio      import PortfolioAllocator, PortfolioContext, AllocationPlan
from leverage.signals        import MultiAssetScanner, AssetSignal

__all__ = [
    "RegimeEngine", "RegimeResult", "leverage_from_score", "regime_label",
    "LeverageSellEngine", "LeveragePosition", "ExitDecision",
    "PortfolioAllocator", "PortfolioContext", "AllocationPlan",
    "MultiAssetScanner", "AssetSignal",
]
