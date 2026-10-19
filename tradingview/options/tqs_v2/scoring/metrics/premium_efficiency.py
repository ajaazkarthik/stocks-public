"""
Premium Efficiency Scorer
==========================
Evaluates cost-efficiency of the option from the buyer's perspective.

Dimensions:
  time_value_score   – intrinsic/extrinsic ratio; more intrinsic = better
  spread_eff_score   – bid/ask quality relative to premium size
  breakeven_score    – expected days to break even vs DTE
  be_1sigma_score    – breakeven distance in units of 1σ move
  delta_iv_score     – |δ| × IV risk-exposure proxy
  leverage_score     – spot / ask leverage multiplier (OTM)
  gamma_score        – dollar gamma convexity premium (Fix 2026-05-27 M-2)

Formula:
  premium_eff = 0.70 × inner + 0.30 × theta_score
  inner = 0.25 × tv_score + 0.20 × spread_eff_score + 0.18 × be_score
        + 0.15 × be_1sigma_score + 0.10 × leverage_score + 0.05 × delta_iv_score
        + 0.07 × gamma_score

Mathematical derivation:
  tv_pct     = (mid - intrinsic) / mid  (0 = all intrinsic; 1 = all extrinsic)
  For OTM (intrinsic=0): tv_score = leverage_ratio score (spot/mid, scaled 0-100)
                           Prior: constant 50.0 — provided zero discrimination between contracts.
                           Fix 2026-05-26: leverage ratio discriminates efficiently — AAPL
                           (spot=305, mid=$2.51, leverage=121×) scores 100 vs CSCO ATM
                           (spot=122, mid=$4.83, leverage=25×) scores 42.8.
  For ITM: tv_score = max(0, 100 - tv_pct * 50)  (lower extrinsic % = higher score)

  spread_eff = (bid / ask) × 100   (90% = tight; 30% = wide)

  Breakeven % = mid / spot
  Daily move  = IV / sqrt(252)          (1-sigma daily expected move)
  be_days     = breakeven_pct / daily_move_pct
  be_score    = clamp(100 × (1 - be_days / (dte + 1)), 0, 100)

  BE/1σ: sigma_move = spot × iv × sqrt(dte/365)  (total expected move over DTE)
         be_1sigma  = (breakeven - spot) / sigma_move
         score: ≤0.5σ→100, 1σ→70, 1.5σ→40, 2σ→10, >2.5σ→0 (linear between breakpoints)

  |δ|·IV: product = abs(delta) × iv  (lower = less risk per dollar paid)
          score: 0→100, 0.05→80, 0.10→60, 0.20→30, 0.30→10, ≥0.40→0 (linear)

  Leverage: L = spot / ask  (how many $ of stock per $ of premium)
            score: 50×→100, 20×→70, 10×→50, 5×→20, <5×→0 (linear between breakpoints)
"""

from __future__ import annotations

import math

from ...interfaces.contracts import IContractScorer
from ...options_data.models import RawContract


class PremiumEfficiencyScorer(IContractScorer):
    """Scores option premium cost-efficiency for buyers."""

    @property
    def name(self) -> str:
        return "premium_efficiency"

    def score(self, contract: RawContract) -> float:
        return self.score_detailed(contract)["total"]

    def score_detailed(self, contract: RawContract) -> dict:
        bid = float(getattr(contract, "bid", 0.0) or 0.0)
        ask = float(getattr(contract, "ask", 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0

        spot = float(getattr(contract, "spot_price", 0.0) or 0.0)
        strike = float(getattr(contract, "strike", 0.0) or 0.0)
        option_type = getattr(contract, "option_type", "call").lower()
        iv = float(getattr(contract, "iv", 0.0) or 0.0)
        dte = max(1, int(getattr(contract, "dte", 45) or 45))
        delta = float(getattr(contract, "delta", 0.0) or 0.0)

        # ── Intrinsic value ──────────────────────────────────────────────────
        if option_type == "call":
            intrinsic = max(0.0, spot - strike)
        else:
            intrinsic = max(0.0, strike - spot)

        # ── Time value score ─────────────────────────────────────────────────
        if mid > 0:
            tv_pct = min(1.0, max(0.0, (mid - intrinsic) / mid))
        else:
            tv_pct = 1.0

        if intrinsic > 0:
            # ITM: lower extrinsic fraction = more intrinsic = higher score
            tv_score = max(0.0, 100.0 - tv_pct * 50.0)
        else:
            # OTM: all premium is time value — prior constant 50.0 provided zero signal.
            # Fix 2026-05-26: use leverage ratio (spot / mid) as directional efficiency metric.
            # Higher leverage = more stock exposure per dollar of premium paid.
            # Scoring curve: 0−30× leverage = 0–50pts; 30−100× leverage = 50−100pts;
            # >100× leverage capped at 100. AAPL 121× scores 100; INTC 20× scores 33.
            if mid > 0.0 and spot > 0.0:
                leverage = spot / mid
                if leverage >= 100.0:
                    tv_score = 100.0
                elif leverage >= 30.0:
                    tv_score = 50.0 + (leverage - 30.0) / 70.0 * 50.0
                else:
                    tv_score = max(0.0, leverage / 30.0 * 50.0)
            else:
                tv_score = 50.0  # no data fallback

        # ── Spread efficiency score ──────────────────────────────────────────
        if ask > 0:
            spread_ratio = bid / ask
            spread_eff_score = min(100.0, spread_ratio * 100.0)
        else:
            spread_eff_score = 0.0

        # ── Breakeven score ──────────────────────────────────────────────────
        # Fix 2026-05-27 (C-5): breakeven uses ask (true cost to buyer), not mid
        if iv > 0 and spot > 0 and ask > 0:
            daily_move_pct = iv / math.sqrt(252)
            breakeven_pct = ask / spot
            be_days = breakeven_pct / daily_move_pct if daily_move_pct > 0 else float(dte)
            be_score = max(0.0, min(100.0, 100.0 * (1.0 - be_days / (dte + 1))))
        else:
            be_score = 50.0

        # ── BE / 1σ score (how many standard deviations to breakeven) ────────
        # sigma_move = spot × iv × sqrt(dte/365)  — 1σ expected move over DTE
        # be_dist    = breakeven price - spot  (positive for CALL, positive for PUT)
        # be_1sigma  = be_dist / sigma_move  (ratio of breakeven to 1σ)
        # Score breakpoints: ≤0.5σ→100, 1σ→70, 1.5σ→40, 2σ→10, ≥2.5σ→0
        # Fix 2026-05-27 (C-5): breakeven uses ask (true cost to buyer), not mid
        if iv > 0 and spot > 0 and ask > 0:
            sigma_move = spot * iv * math.sqrt(dte / 365.0)
            if option_type == "call":
                breakeven_price = strike + ask   # need to move above strike+premium
                be_dist = max(0.0, breakeven_price - spot)
            else:
                breakeven_price = strike - ask   # need to fall below strike-premium
                be_dist = max(0.0, spot - breakeven_price)
            if sigma_move > 0:
                be_sigma = be_dist / sigma_move
            else:
                be_sigma = 2.5  # default to worst case
            # Piecewise linear scoring
            if be_sigma <= 0.5:
                be_1sigma_score = 100.0
            elif be_sigma <= 1.0:
                be_1sigma_score = 100.0 - (be_sigma - 0.5) / 0.5 * 30.0   # 100→70
            elif be_sigma <= 1.5:
                be_1sigma_score = 70.0 - (be_sigma - 1.0) / 0.5 * 30.0    # 70→40
            elif be_sigma <= 2.0:
                be_1sigma_score = 40.0 - (be_sigma - 1.5) / 0.5 * 30.0    # 40→10
            elif be_sigma <= 2.5:
                be_1sigma_score = 10.0 - (be_sigma - 2.0) / 0.5 * 10.0    # 10→0
            else:
                be_1sigma_score = 0.0
        else:
            be_1sigma_score = 50.0
            be_sigma = 0.0

        # ── |δ| × IV score (risk-exposure proxy) ─────────────────────────────
        # Lower product → less risk per dollar paid → better for buyer
        # abs(delta) × iv: score breakpoints (lower = better):
        # 0.00→100, 0.05→80, 0.10→60, 0.20→30, 0.30→10, ≥0.40→0
        delta_iv_product = abs(delta) * iv
        if delta_iv_product <= 0.0:
            delta_iv_score = 100.0
        elif delta_iv_product <= 0.05:
            delta_iv_score = 100.0 - delta_iv_product / 0.05 * 20.0      # 100→80
        elif delta_iv_product <= 0.10:
            delta_iv_score = 80.0 - (delta_iv_product - 0.05) / 0.05 * 20.0  # 80→60
        elif delta_iv_product <= 0.20:
            delta_iv_score = 60.0 - (delta_iv_product - 0.10) / 0.10 * 30.0  # 60→30
        elif delta_iv_product <= 0.30:
            delta_iv_score = 30.0 - (delta_iv_product - 0.20) / 0.10 * 20.0  # 30→10
        elif delta_iv_product <= 0.40:
            delta_iv_score = 10.0 - (delta_iv_product - 0.30) / 0.10 * 10.0  # 10→0
        else:
            delta_iv_score = 0.0

        # ── Leverage score (spot / ask) ───────────────────────────────────────
        # Higher leverage = more stock exposure per dollar of premium = better for buyer
        # Score breakpoints: <5×→0, 5×→20, 10×→50, 20×→70, 50×→100
        if ask > 0 and spot > 0:
            leverage = spot / ask
        else:
            leverage = 0.0
        if leverage >= 50.0:
            leverage_score = 100.0
        elif leverage >= 20.0:
            leverage_score = 70.0 + (leverage - 20.0) / 30.0 * 30.0      # 70→100
        elif leverage >= 10.0:
            leverage_score = 50.0 + (leverage - 10.0) / 10.0 * 20.0      # 50→70
        elif leverage >= 5.0:
            leverage_score = 20.0 + (leverage - 5.0) / 5.0 * 30.0        # 20→50
        else:
            leverage_score = leverage / 5.0 * 20.0 if leverage > 0 else 0.0  # 0→20

        # ── Theta decay sub-score (TASK 5i) ──────────────────────────────────
        # theta is negative (daily cost); as % of ask premium:
        #   0%   daily cost → 100  (no decay — ideal, though unrealistic)
        #   0.02 (2%)  → ~0        (losing 2% of premium per day → bad)
        # clamp: 100 - (|theta| / ask) × 5000
        theta = float(getattr(contract, "theta", 0.0) or 0.0)
        if ask > 0 and theta != 0.0:
            theta_daily_cost_pct = abs(theta) / ask
            theta_score = min(100.0, max(0.0, 100.0 - theta_daily_cost_pct * 5000.0))
        else:
            theta_score = 50.0  # neutral when data unavailable

        # ── Gamma score (convexity premium) ───────────────────────────────────
        # Higher gamma = faster delta acceleration = more convexity profit potential.
        # Fix 2026-05-27 (M-2): Gamma was fetched but never scored.
        # normalized_gamma = gamma × spot × 100 (dollar gamma per 1% move)
        # Score: 0→0, 5→50, 10+→100 (linear, capped)
        gamma = float(getattr(contract, "gamma", 0.0) or 0.0)
        if gamma > 0 and spot > 0:
            normalized_gamma = gamma * spot * 100.0
            gamma_score = min(100.0, normalized_gamma * 10.0)
        else:
            gamma_score = 50.0  # neutral when no gamma data

        # ── Composite (inner 7 sub-scores blend) ─────────────────────────────
        inner_total = (
            0.25 * tv_score
            + 0.20 * spread_eff_score     # was 0.25; -0.05 to gamma
            + 0.18 * be_score             # was 0.20; -0.02 to gamma
            + 0.15 * be_1sigma_score
            + 0.10 * leverage_score
            + 0.05 * delta_iv_score
            + 0.07 * gamma_score          # Fix 2026-05-27 (M-2): convexity signal
        )
        # Blend theta score: 70% inner composite + 30% theta
        total = 0.70 * inner_total + 0.30 * theta_score

        return {
            "total": round(total, 2),
            "tv_score": tv_score,
            "spread_eff_score": spread_eff_score,
            "be_score": be_score,
            "be_1sigma_score": round(be_1sigma_score, 2),
            "delta_iv_score": round(delta_iv_score, 2),
            "leverage_score": round(leverage_score, 2),
            "theta_score": round(theta_score, 2),
            "gamma_score": round(gamma_score, 2),
            "tv_pct": tv_pct,
            "be_sigma": round(be_sigma, 3),
            "delta_iv_product": round(delta_iv_product, 4),
            "leverage": round(leverage, 1),
        }
