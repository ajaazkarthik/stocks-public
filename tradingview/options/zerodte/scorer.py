"""
ZeroDTE Signal Scorer
======================
Computes signal scores for momentum, IV backwardation, and VOI surge.
All scoring logic is in one place for easy tuning.

Max possible scores:
  Momentum:         40 pts
  IV Backwardation: 30 pts
  VOI Surge:        30 pts
  Composite:       100 pts
"""

from __future__ import annotations

import logging

from .config import ZeroDTEConfig, DEFAULT_CONFIG
from .models import MoverCandidate, IVStructure, VOIData, SignalScore

_log = logging.getLogger("zerodte.scorer")


# ── Momentum Scorer ─────────────────────────────────────────────────────────

def score_momentum(
    candidate: MoverCandidate,
    direction: str,
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> tuple[float, float, float, float]:
    """
    Score momentum signal. Returns (total, prev_day_pts, vol_pts, rsi_pts).
    direction: "call" or "put"
    """
    change = candidate.prev_day_change_pct
    vol_ratio = candidate.volume_ratio
    rsi = candidate.rsi

    prev_day_pts = 0.0
    vol_pts = 0.0
    rsi_pts = 0.0

    if direction == "call":
        # CALL setup: need bullish momentum
        # Tiered: +15 for ≥2%, +20 for ≥4%, +20 for ≥7% (gap-up / continuation)
        # May 28 MSFT: +3.4% → 15 pts. A +7%+ day (NVDA earnings style) → 20 pts.
        if change >= 7.0:
            prev_day_pts = 20.0     # gap-up / explosive move → strong continuation candidate
        elif change >= 4.0:
            prev_day_pts = 20.0     # strong move
        elif change >= config.momentum_min_pct_call:
            prev_day_pts = 15.0     # threshold move (May 28 MSFT 3.4% → gets this)

        if vol_ratio >= 2.0:
            vol_pts = 10.0          # very high volume surge
        elif vol_ratio >= config.momentum_vol_ratio_min:
            vol_pts = 7.0           # above average volume

        # RSI in bullish range: 45-75
        if config.momentum_rsi_call_min <= rsi <= config.momentum_rsi_call_max:
            if rsi >= 60:
                rsi_pts = 10.0      # strong bullish RSI
            elif rsi >= 50:
                rsi_pts = 7.0       # moderate bullish RSI
            else:
                rsi_pts = 3.0       # borderline

    else:  # put
        # PUT setup: need bearish momentum
        if change <= -7.0:
            prev_day_pts = 20.0     # gap-down / explosive down move
        elif change <= -4.0:
            prev_day_pts = 20.0     # strong down move
        elif change <= -config.momentum_min_pct_put:
            prev_day_pts = 15.0     # threshold down move

        if vol_ratio >= 2.0:
            vol_pts = 10.0
        elif vol_ratio >= config.momentum_vol_ratio_min:
            vol_pts = 7.0

        # RSI in bearish range: 25-55
        if config.momentum_rsi_put_min <= rsi <= config.momentum_rsi_put_max:
            if rsi <= 40:
                rsi_pts = 10.0      # strong bearish RSI
            elif rsi <= 50:
                rsi_pts = 7.0
            else:
                rsi_pts = 3.0

    total = prev_day_pts + vol_pts + rsi_pts
    total = min(total, 40.0)
    return total, prev_day_pts, vol_pts, rsi_pts


# ── IV Backwardation Scorer ──────────────────────────────────────────────────

def score_iv_backwardation(
    iv: IVStructure,
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> tuple[float, float, float, float]:
    """
    Score IV backwardation (iv_1w vs iv_1m).
    Returns (total, base_pts, mid_pts, high_pts).
    """
    if not iv.available:
        return 0.0, 0.0, 0.0, 0.0

    # GAP-2 fix (2026-05-31): use RELATIVE slope = (iv_1w / iv_1m) - 1.0.
    # MSFT example: iv_1w=0.3232, iv_1m=0.3033 → absolute=0.0199 (missed mid at 0.02)
    #                                           → relative=6.57%  (comfortably above 4%)
    if iv.iv_1m <= 0:
        return 0.0, 0.0, 0.0, 0.0
    slope = (iv.iv_1w / iv.iv_1m) - 1.0  # relative backwardation ratio
    base_pts = 0.0
    mid_pts = 0.0
    high_pts = 0.0

    if slope > config.iv_back_min_slope:
        base_pts = 15.0             # any backwardation

    if slope >= config.iv_back_mid_slope:
        mid_pts = 10.0              # meaningful backwardation

    if slope >= config.iv_back_high_slope:
        high_pts = 5.0              # extreme backwardation

    total = min(base_pts + mid_pts + high_pts, 30.0)
    return total, base_pts, mid_pts, high_pts


# ── VOI Surge Scorer ─────────────────────────────────────────────────────────

def score_voi_surge(
    voi: VOIData,
    direction: str,
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> tuple[float, float, float, float]:
    """
    Score VOI (volume/OI) directional surge.
    direction: "call" or "put"
    Returns (total, base_pts, mid_pts, high_pts).
    """
    if not voi.available:
        return 0.0, 0.0, 0.0, 0.0

    # Pick the relevant ratio based on direction
    if direction == "call":
        ratio = voi.call_put_ratio
    else:
        ratio = voi.put_call_ratio

    base_pts = 0.0
    mid_pts = 0.0
    high_pts = 0.0

    if ratio >= config.voi_base_ratio:
        base_pts = 10.0            # basic surge (5x)

    if ratio >= config.voi_mid_ratio:
        mid_pts = 15.0             # strong surge (10x)

    if ratio >= config.voi_high_ratio:
        high_pts = 5.0             # extreme surge (50x — institutional)

    total = min(base_pts + mid_pts + high_pts, 30.0)
    return total, base_pts, mid_pts, high_pts


# ── Composite Scorer ─────────────────────────────────────────────────────────

def compute_signal_score(
    candidate: MoverCandidate,
    iv: IVStructure,
    voi: VOIData,
    direction: str,
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> SignalScore:
    """
    Compute full SignalScore for a candidate in one direction.
    direction: "call" | "put"
    """
    # Signal 1 — Momentum
    mom_total, mom_prev, mom_vol, mom_rsi = score_momentum(candidate, direction, config)

    # Signal 2 — IV Backwardation
    iv_total, iv_base, iv_mid, iv_high = score_iv_backwardation(iv, config)

    # Signal 3 — VOI Surge
    voi_total, voi_base, voi_mid, voi_high = score_voi_surge(voi, direction, config)

    sig = SignalScore(
        symbol=candidate.symbol,
        direction=direction,
        # Momentum
        momentum_score=mom_total,
        momentum_prev_day_pts=mom_prev,
        momentum_vol_pts=mom_vol,
        momentum_rsi_pts=mom_rsi,
        # IV
        iv_back_score=iv_total,
        iv_back_slope=((iv.iv_1w / iv.iv_1m) - 1.0) if (iv.available and iv.iv_1m > 0) else 0.0,
        iv_back_pts_base=iv_base,
        iv_back_pts_mid=iv_mid,
        iv_back_pts_high=iv_high,
        iv_available=iv.available,
        # VOI
        voi_score=voi_total,
        voi_ratio=(voi.call_put_ratio if direction == "call" else voi.put_call_ratio),
        voi_pts_base=voi_base,
        voi_pts_mid=voi_mid,
        voi_pts_high=voi_high,
        voi_available=voi.available,
        # Metadata
        price=candidate.price,
        prev_day_change_pct=candidate.prev_day_change_pct,
        volume_ratio=candidate.volume_ratio,
        rsi=candidate.rsi,
        iv_1w=iv.iv_1w if iv.available else 0.0,
        iv_1m=iv.iv_1m if iv.available else 0.0,
        sector=candidate.sector,
        industry=candidate.industry,
    )
    sig.compute_composite()

    _log.debug(
        "%s %s → composite=%.1f (mom=%.0f, iv=%.0f, voi=%.0f)",
        candidate.symbol, direction.upper(), sig.composite_score,
        mom_total, iv_total, voi_total,
    )
    return sig
