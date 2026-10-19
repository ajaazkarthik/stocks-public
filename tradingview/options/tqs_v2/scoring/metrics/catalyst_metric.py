"""
Catalyst Metric — scoring/metrics/catalyst_metric.py
======================================================

Computes catalyst_score (0-100) as a weighted aggregate of 6 sub-signals.
Only active when config.catalyst_mode == True.

Sub-signal weights (CALL preset — GAP-5 updated 2026-05-31):
  iv_hump_score              21.25%  — IV term structure hump
  vol_oi_ratio_score         21.25%  — fresh OI accumulation vs daily volume
  call_vol_skew_score        17.00%  — call vs put volume direction
  price_breakout_score       12.75%  — recent % move
  analyst_gap_score          12.75%  — analyst mean target vs current price
  post_earnings_drift_score  15.00%  — post-earnings continuation (GAP-5)

Sub-signal weights (PUT preset):
  Same but uses put_vol_skew_score instead of call_vol_skew_score.

Scoring thresholds:
  iv_hump:              > 0.15 → 100 | > 0.08 → 75 | > 0.03 → 45 | else → 0
  vol_oi_ratio:         > 2.00 → 100 | > 1.00 → 80 | > 0.50 → 60 | > 0.20 → 30 | else → 0
  call_vol_skew:        > 0.70 → 100 | > 0.60 → 75 | > 0.50 → 50 | > 0.40 → 25 | else → 0
  price_breakout:       > 0.20 → 100 | > 0.10 → 80 | > 0.05 → 60 | > 0.02 → 35 | else → 0
  analyst_gap:          > 0.30 → 100 | > 0.20 → 75 | > 0.10 → 50 | > 0.05 → 25 | else → 0
  post_earnings_drift:  ≤14d+≥5% → 100 | ≤14d+≥3% → 75 | ≤21d+≥5% → 50 | else → 0
                        None (data unavailable) → weight rescaled out

If a sub-signal is None (data unavailable), it is excluded and remaining
weights are rescaled to sum to 1.0. Returns 0.0 if ALL signals are None.
"""

from __future__ import annotations

from ..models import ScoreBreakdown
from ...options_data.models import RawContract
from ...config import TQSv2Config

# GAP-5 (2026-05-31): Rescaled to include post_earnings_drift_score (15%).
# Old weights \u00d7 0.85 to make room; sum still = 1.0.
_CALL_WEIGHTS: dict[str, float] = {
    "iv_hump_score":             0.2125,  # was 0.25
    "vol_oi_ratio_score":        0.2125,  # was 0.25
    "call_vol_skew_score":       0.1700,  # was 0.20
    "price_breakout_score":      0.1275,  # was 0.15
    "analyst_gap_score":         0.1275,  # was 0.15
    "post_earnings_drift_score": 0.1500,  # NEW \u2014 post-earnings drift momentum
}

_PUT_WEIGHTS: dict[str, float] = {
    "iv_hump_score":             0.2125,
    "vol_oi_ratio_score":        0.2125,
    "put_vol_skew_score":        0.1700,  # PUT: reward put_vol_skew instead
    "price_breakout_score":      0.1275,
    "analyst_gap_score":         0.1275,
    "post_earnings_drift_score": 0.1500,
}


def _score_iv_hump(iv_hump: float | None) -> float | None:
    if iv_hump is None:
        return None
    if iv_hump > 0.15:
        return 100.0
    if iv_hump > 0.08:
        return 75.0
    if iv_hump > 0.03:
        return 45.0
    return 0.0


def _score_vol_oi_ratio(ratio: float | None) -> float | None:
    if ratio is None:
        return None
    if ratio > 2.00:
        return 100.0
    if ratio > 1.00:
        return 80.0
    if ratio > 0.50:
        return 60.0
    if ratio > 0.20:
        return 30.0
    return 0.0


def _score_vol_skew(skew: float | None) -> float | None:
    """Scores call_vol_skew (CALL) or put_vol_skew (PUT). Same thresholds."""
    if skew is None:
        return None
    if skew > 0.70:
        return 100.0
    if skew > 0.60:
        return 75.0
    if skew > 0.50:
        return 50.0
    if skew > 0.40:
        return 25.0
    return 0.0


def _score_price_breakout(pct: float | None) -> float | None:
    if pct is None:
        return None
    if pct > 0.20:
        return 100.0
    if pct > 0.10:
        return 80.0
    if pct > 0.05:
        return 60.0
    if pct > 0.02:
        return 35.0
    return 0.0


def _score_analyst_gap(gap: float | None) -> float | None:
    if gap is None:
        return None
    if gap > 0.30:
        return 100.0
    if gap > 0.20:
        return 75.0
    if gap > 0.10:
        return 50.0
    if gap > 0.05:
        return 25.0
    return 0.0


def _score_post_earnings_drift(
    days_since: int | None,
    move_pct: float | None,
) -> float | None:
    """
    GAP-5 (2026-05-31): Score post-earnings continuation momentum.

    High score: earnings \u226414d ago with large move (\u22655%) \u2192 drift still active.
    Medium:     15-21d window or smaller initial move.
    Zero:       >21 days since earnings \u2014 drift window closed.
    None:       data unavailable \u2192 weight is rescaled out of the aggregate.
    """
    if days_since is None or move_pct is None:
        return None  # Graceful: weight gets rescaled out

    if 0 <= days_since <= 14:
        if move_pct >= 5.0:
            return 100.0
        if move_pct >= 3.0:
            return 75.0
        if move_pct >= 1.0:
            return 50.0
        return 20.0
    if 15 <= days_since <= 21:
        if move_pct >= 5.0:
            return 50.0
        if move_pct >= 3.0:
            return 30.0
        return 10.0
    return 0.0  # > 21 days since earnings


def compute_catalyst_score(
    contract: RawContract,
    config: TQSv2Config,
) -> float:
    """
    Compute catalyst_score (0-100) for a contract.
    Called from ScoringEngine when config.catalyst_mode == True.

    Sub-signals that are None (unavailable) are excluded, and the remaining
    weights are rescaled so they still sum to 1.0.
    """
    is_put = getattr(contract, "option_type", "call").lower() == "put"
    weights = _PUT_WEIGHTS if is_put else _CALL_WEIGHTS

    # Compute raw sub-scores
    sub_scores: dict[str, float | None] = {
        "iv_hump_score":             _score_iv_hump(getattr(contract, "iv_hump", None)),
        "vol_oi_ratio_score":        _score_vol_oi_ratio(getattr(contract, "vol_oi_ratio", None)),
        "call_vol_skew_score":       _score_vol_skew(getattr(contract, "call_vol_skew", None)),
        "put_vol_skew_score":        _score_vol_skew(getattr(contract, "put_vol_skew", None)),
        "price_breakout_score":      _score_price_breakout(getattr(contract, "price_breakout_pct", None)),
        "analyst_gap_score":         _score_analyst_gap(getattr(contract, "analyst_target_gap", None)),
        # GAP-5 (2026-05-31): Post-earnings drift
        "post_earnings_drift_score": _score_post_earnings_drift(
            getattr(contract, "post_earnings_days", None),
            getattr(contract, "post_earnings_move_pct", None),
        ),
    }

    # Only include keys that are in the selected weight dict AND have a non-None score
    active: dict[str, float] = {
        k: sub_scores[k]  # type: ignore[assignment]
        for k in weights
        if sub_scores.get(k) is not None
    }

    total_weight = sum(weights[k] for k in active)
    if total_weight == 0.0:
        return 0.0

    weighted_sum = sum(sub_scores[k] * weights[k] for k in active)  # type: ignore[operator]
    return min(100.0, weighted_sum / total_weight)
