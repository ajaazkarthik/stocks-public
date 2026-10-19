"""
Leverage Portfolio Manager — Position Sizing and Allocation Engine
==================================================================
Manages how the 20% tactical (high-risk/high-reward) allocation is deployed
across leveraged positions.

Key responsibilities:
  1. Compute target allocation per asset within the 20% bucket
  2. Scale leverage based on RegimeScore (more confidence = more size)
  3. Enforce hard concentration limits (max per asset, per instrument type)
  4. Estimate decay drag cost for the full portfolio
  5. Produce an actionable allocation table

Allocation model
----------------
The 20% tactical bucket is divided as follows based on RegimeScore:

  RegimeScore >= 8.0  (STRONG BULL):
    15% → primary leveraged instrument (UPRO/TQQQ/DLC)
     5% → secondary sector/commodity leveraged instrument
     0% → cash reserve within bucket

  RegimeScore 6.0–7.9 (BULL / MILD BULL):
    12% → primary leveraged (2x only)
     5% → secondary (2x)
     3% → cash reserve

  RegimeScore 4.0–5.9 (NEUTRAL):
    20% → direct 1x (no leverage products)
     0% → cash reserve

  RegimeScore < 4.0 (BEAR):
    10% → TLT (long duration bonds) or cash
    10% → SH (inverse S&P) or 100% cash
     0% → any leverage

Usage
-----
  from leverage.portfolio import PortfolioAllocator, PortfolioContext

  ctx = PortfolioContext(
      total_portfolio_value=100_000,
      tactical_pct=0.20,
      base_currency="USD",
  )
  allocator = PortfolioAllocator(ctx)
  plan      = allocator.compute_allocation()
  print(plan.summary())
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

_PARENT = Path(__file__).parent.parent
sys.path.insert(0, str(_PARENT))

from leverage.regime_score import RegimeEngine, RegimeResult, leverage_from_score
from leverage.signals import MultiAssetScanner, AssetSignal, annual_decay_drag


# ─────────────────────────────────────────────────────────────────────────────
# Hard concentration limits (non-negotiable)
# ─────────────────────────────────────────────────────────────────────────────

MAX_SINGLE_LEVERAGED_POSITION_PCT = 0.15   # max 15% of total portfolio in one leveraged position
MAX_SINGLE_DLC_PCT                = 0.05   # DLCs capped at 5% due to extreme decay risk
MAX_TOTAL_LEVERAGED_PCT           = 0.20   # never exceed 20% total in leveraged products
MAX_GROSS_LEVERAGE_EXPOSURE       = 3.0    # maximum multiple on the 20% bucket
                                           # (e.g. 20% × 3x = 60% gross exposure)

# Per-instrument type max leverage (within the tactical bucket)
MAX_LEVERAGE_BY_TYPE = {
    "etf_2x":  2.0,
    "etf_3x":  3.0,
    "dlc_5x":  5.0,
    "dlc_7x":  7.0,
    "futures": 2.0,   # conservative for futures (margin discipline)
    "margin":  1.5,
    "options": 2.0,   # implicit leverage
    "direct":  1.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PortfolioContext:
    total_portfolio_value: float        # AUD/USD/SGD — your entire portfolio NLV
    tactical_pct:          float = 0.20 # fraction allocated to high-risk bucket
    base_currency:         str   = "USD"
    available_markets:     list[str] = field(default_factory=lambda: ["US", "SG", "JP"])

    @property
    def tactical_capital(self) -> float:
        return self.total_portfolio_value * self.tactical_pct

    @property
    def core_capital(self) -> float:
        return self.total_portfolio_value * (1.0 - self.tactical_pct)


@dataclass
class AllocationSlot:
    """A single line in the allocation plan."""
    rank:         int
    asset_signal: AssetSignal
    pct_of_total: float           # e.g. 0.12 = 12% of total portfolio
    dollar_value: float
    leverage:     float
    instrument:   str
    gross_exposure: float         # dollar_value × leverage
    decay_drag_annual: float      # estimated $ decay cost per year
    rationale:    str = ""


@dataclass
class AllocationPlan:
    regime_score:          float
    regime_label:          str
    vix:                   float
    total_portfolio_value: float
    tactical_capital:      float
    slots:                 list[AllocationSlot] = field(default_factory=list)
    cash_reserve_pct:      float = 0.0
    de_risk_mode:          bool  = False
    timestamp:             str   = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def total_allocated_pct(self) -> float:
        return sum(s.pct_of_total for s in self.slots)

    @property
    def total_gross_exposure(self) -> float:
        return sum(s.gross_exposure for s in self.slots)

    @property
    def total_decay_drag_annual(self) -> float:
        return sum(s.decay_drag_annual for s in self.slots)

    def summary(self) -> str:
        lines = [
            f"\n{'='*70}",
            f"LEVERAGE ALLOCATION PLAN  |  {self.timestamp[:10]}",
            f"{'='*70}",
            f"Portfolio: ${self.total_portfolio_value:,.0f}  |  "
            f"Tactical bucket: ${self.tactical_capital:,.0f} ({self.tactical_capital/self.total_portfolio_value:.0%})",
            f"RegimeScore: {self.regime_score:.1f}  |  {self.regime_label}  |  VIX: {self.vix:.1f}",
            "",
        ]
        if self.de_risk_mode:
            lines.append("⚠️  DE-RISK MODE ACTIVE — all leverage exited, holding defensive assets")
            lines.append("")

        if self.slots:
            lines.append(f"  {'#':>2} {'TICKER':8s} {'INSTR':10s} {'LEV':>4} "
                          f"{'%PORT':>6} {'$VALUE':>9} {'GROSS':>9} {'DECAY/YR':>9}  RATIONALE")
            lines.append(f"  {'-'*85}")
            for s in self.slots:
                lines.append(
                    f"  {s.rank:>2} {s.asset_signal.yf_ticker:8s} {s.instrument:10s} "
                    f"{s.leverage:>3.1f}x "
                    f"{s.pct_of_total:>5.1%} "
                    f"${s.dollar_value:>8,.0f} "
                    f"${s.gross_exposure:>8,.0f} "
                    f"${s.decay_drag_annual:>8,.0f}  "
                    f"{s.rationale}"
                )
        else:
            lines.append("  No leveraged positions allocated.")

        lines.append("")
        lines.append(f"  Total allocated: {self.total_allocated_pct:.1%} | "
                     f"Gross exposure: ${self.total_gross_exposure:,.0f} | "
                     f"Est. decay drag/yr: ${self.total_decay_drag_annual:,.0f}")
        lines.append('='*70)
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PortfolioAllocator
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioAllocator:
    """
    Compute a full leverage allocation plan based on current regime.

    Integrates with RegimeEngine + MultiAssetScanner from your existing stack.
    """

    def __init__(
        self,
        context: PortfolioContext,
        regime_engine: Optional[RegimeEngine] = None,
        scanner: Optional[MultiAssetScanner] = None,
    ):
        self.ctx     = context
        self.engine  = regime_engine or RegimeEngine()
        self.scanner = scanner or MultiAssetScanner(regime_engine=self.engine)

    def compute_allocation(
        self,
        top_n: int = 3,
        asset_classes: Optional[list[str]] = None,
    ) -> AllocationPlan:
        """
        Compute the optimal leverage allocation for the current regime.

        Parameters
        ----------
        top_n         : maximum number of leveraged positions to hold simultaneously
        asset_classes : restrict to these asset classes (None = all)
        """
        regime = self.engine.compute("SPY")
        from leverage.regime_score import _fetch_vix
        vix    = _fetch_vix()

        plan = AllocationPlan(
            regime_score          = regime.score,
            regime_label          = regime.regime_label,
            vix                   = vix,
            total_portfolio_value = self.ctx.total_portfolio_value,
            tactical_capital      = self.ctx.tactical_capital,
        )

        # ── De-risk mode ──────────────────────────────────────────────────────
        if regime.hard_exit_triggered or regime.score < 4.0:
            plan.de_risk_mode = True
            plan.cash_reserve_pct = 1.0
            # Add defensive slot (TLT or cash)
            if regime.score < 4.0:
                plan.slots.append(self._defensive_slot())
            return plan

        # ── Get ranked opportunities ──────────────────────────────────────────
        opportunities = self.scanner.scan(
            min_composite_score = 5.5,
            asset_classes       = asset_classes,
        )

        # Take top N
        selected = opportunities[:top_n]
        if not selected:
            plan.cash_reserve_pct = 1.0
            return plan

        # ── Compute target allocations based on regime score ──────────────────
        base_allocs = self._target_allocations(regime.score, len(selected))

        total_allocated_pct = 0.0
        for rank, (sig, alloc_pct) in enumerate(zip(selected, base_allocs), start=1):
            # Adjust allocation down for DLCs (extreme decay risk)
            if sig.recommended_instrument in ("dlc_5x", "dlc_7x"):
                alloc_pct = min(alloc_pct, MAX_SINGLE_DLC_PCT)

            # Enforce single-position hard cap
            alloc_pct = min(alloc_pct, MAX_SINGLE_LEVERAGED_POSITION_PCT)

            # Ensure total leveraged exposure stays within 20% of portfolio
            remaining = MAX_TOTAL_LEVERAGED_PCT - total_allocated_pct
            alloc_pct = min(alloc_pct, remaining)

            if alloc_pct <= 0:
                break

            dollar_val     = self.ctx.total_portfolio_value * alloc_pct
            lev            = min(sig.recommended_leverage,
                                 MAX_LEVERAGE_BY_TYPE.get(sig.recommended_instrument, 1.0))
            gross_exposure = dollar_val * lev
            decay_annual   = annual_decay_drag(lev, sig.annual_vol) * dollar_val

            slot = AllocationSlot(
                rank         = rank,
                asset_signal = sig,
                pct_of_total = alloc_pct,
                dollar_value = dollar_val,
                leverage     = lev,
                instrument   = sig.recommended_instrument,
                gross_exposure    = gross_exposure,
                decay_drag_annual = decay_annual,
                rationale    = self._rationale(regime.score, sig),
            )
            plan.slots.append(slot)
            total_allocated_pct += alloc_pct

        plan.cash_reserve_pct = max(0.0, self.ctx.tactical_pct - total_allocated_pct)
        return plan

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _target_allocations(self, score: float, n_assets: int) -> list[float]:
        """
        Target % of total portfolio per slot.
        Primary slot gets the most, each subsequent slot gets progressively less.
        """
        if score >= 8.0:
            base = 0.15    # STRONG BULL
        elif score >= 7.0:
            base = 0.12    # BULL
        elif score >= 6.0:
            base = 0.10    # MILD BULL
        else:
            base = 0.0     # NEUTRAL or worse — no leverage

        # Pyramid: slot 1 = base, slot 2 = base × 0.4, slot 3 = base × 0.25
        multipliers = [1.0, 0.40, 0.25]
        return [base * multipliers[i] for i in range(min(n_assets, len(multipliers)))]

    def _rationale(self, score: float, sig: AssetSignal) -> str:
        parts = []
        if sig.trend_score >= 7:
            parts.append("strong trend")
        if sig.momentum_score >= 7:
            parts.append("momentum")
        if sig.annual_vol < 0.20:
            parts.append("low decay")
        parts.append(f"score={sig.composite_score:.1f}")
        return ", ".join(parts)

    def _defensive_slot(self) -> AllocationSlot:
        """Return a TLT defensive slot for de-risk mode."""
        from leverage.signals import AssetSignal
        dummy_sig = AssetSignal(
            yf_ticker="TLT", tv_symbol="NASDAQ:TLT",
            description="20Y Treasury (defensive)",
            asset_class="bond", currency="USD",
            available_instruments=["direct"],
            composite_score=5.0, annual_vol=0.12,
        )
        pct = self.ctx.tactical_pct * 0.5   # keep half in TLT, half cash
        return AllocationSlot(
            rank=1, asset_signal=dummy_sig,
            pct_of_total=pct,
            dollar_value=self.ctx.total_portfolio_value * pct,
            leverage=1.0, instrument="direct",
            gross_exposure=self.ctx.total_portfolio_value * pct,
            decay_drag_annual=0.0,
            rationale="de-risk rotation to bonds",
        )

    def sizing_for_single(
        self,
        ticker: str,
        price: float,
        allocation_pct: float,
        leverage: float,
    ) -> dict:
        """
        Compute units to buy for a single position.

        Parameters
        ----------
        ticker         : instrument ticker
        price          : current price
        allocation_pct : fraction of total portfolio to allocate (e.g. 0.10)
        leverage       : intended leverage multiple

        Returns
        -------
        dict with units, notional, gross_exposure, max_loss_8pct
        """
        notional        = self.ctx.total_portfolio_value * allocation_pct
        units           = int(notional / price)
        gross_exposure  = notional * leverage
        max_loss        = notional * 0.08   # 8% trailing stop = max loss
        decay_drag_yr   = annual_decay_drag(leverage, 0.20) * notional  # assume 20% vol
        return {
            "ticker":          ticker,
            "units":           units,
            "notional":        round(notional, 2),
            "price":           price,
            "allocation_pct":  allocation_pct,
            "leverage":        leverage,
            "gross_exposure":  round(gross_exposure, 2),
            "max_loss_at_8pct_dd": round(max_loss, 2),
            "est_decay_drag_yr":   round(decay_drag_yr, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ctx = PortfolioContext(
        total_portfolio_value=200_000,
        tactical_pct=0.20,
        base_currency="USD",
    )
    allocator = PortfolioAllocator(ctx)
    plan      = allocator.compute_allocation(top_n=3)
    print(plan.summary())
