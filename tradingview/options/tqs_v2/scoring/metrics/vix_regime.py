"""
VIX Regime Scorer – adjusts score threshold based on market-wide volatility.

Fix 2026-05-27 (C-6): TQS v2 had zero VIX awareness. System would recommend
buying options during VIX=45 panic spikes when ALL premiums are inflated 2-3×.

Regime bands:
  VIX  0-15  → Low vol ("cheap premium")  → threshold multiplier 0.85
  VIX 15-20  → Normal                     → threshold multiplier 1.00
  VIX 20-25  → Elevated                   → threshold multiplier 1.10
  VIX 25-30  → High                       → threshold multiplier 1.25
  VIX 30+    → Extreme fear               → threshold multiplier 1.45

Usage: Multiply the minimum composite score threshold by the VIX multiplier.
Higher multiplier = harder to qualify = fewer contracts pass in fear environments.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

_log = logging.getLogger("tqs_v2.scoring.vix_regime")

# ── VIX regime bands ──────────────────────────────────────────────────────
_VIX_BANDS: list[tuple[float, float, float, str]] = [
    # (lower_bound, upper_bound, multiplier, label)
    (0.0,  15.0, 0.85, "low_vol"),
    (15.0, 20.0, 1.00, "normal"),
    (20.0, 25.0, 1.10, "elevated"),
    (25.0, 30.0, 1.25, "high"),
    (30.0, 999.0, 1.45, "extreme_fear"),
]

# ── Cache ──────────────────────────────────────────────────────────────────
_vix_cache: dict[str, tuple[float, float]] = {}  # key → (vix_level, timestamp)
_VIX_CACHE_TTL = 3600.0  # 1 hour


def fetch_vix_level() -> Optional[float]:
    """Fetch current VIX level from yfinance. Returns None on failure."""
    cache_key = "^VIX"
    now = time.time()

    # Check cache
    if cache_key in _vix_cache:
        cached_vix, cached_ts = _vix_cache[cache_key]
        if now - cached_ts < _VIX_CACHE_TTL:
            _log.debug("VIX cache hit: %.2f (age %.0fs)", cached_vix, now - cached_ts)
            return cached_vix

    try:
        import yfinance as yf
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="1d")
        if hist.empty:
            _log.warning("VIX: yfinance returned empty history")
            return None
        vix_level = float(hist["Close"].iloc[-1])
        _vix_cache[cache_key] = (vix_level, now)
        _log.info("VIX fetched: %.2f", vix_level)
        return vix_level
    except Exception as exc:
        _log.warning("VIX fetch failed: %s", exc)
        return None


def get_vix_multiplier(vix_level: Optional[float] = None) -> tuple[float, str]:
    """
    Get the threshold multiplier for the current VIX regime.

    Returns: (multiplier, regime_label)
    If VIX unavailable, returns (1.0, "unknown") — neutral, no penalty.
    """
    if vix_level is None:
        vix_level = fetch_vix_level()

    if vix_level is None:
        return 1.0, "unknown"

    for lower, upper, mult, label in _VIX_BANDS:
        if lower <= vix_level < upper:
            return mult, label

    # Fallback for extreme values
    return 1.45, "extreme_fear"


def vix_regime_score(vix_level: Optional[float] = None) -> float:
    """
    Convert VIX level to a 0-100 score for composite scoring.

    Low VIX (cheap premiums) = high score (good for buyers).
    High VIX (expensive premiums) = low score (bad for buyers).

    Score: VIX=10 → 100, VIX=20 → 67, VIX=30 → 33, VIX=40+ → 0
    """
    if vix_level is None:
        vix_level = fetch_vix_level()

    if vix_level is None:
        return 50.0  # neutral when unavailable

    # Linear scale: VIX 10 → 100, VIX 40 → 0
    score = max(0.0, min(100.0, (40.0 - vix_level) / 30.0 * 100.0))
    return score


def clear_cache() -> None:
    """Clear the VIX cache (for testing)."""
    _vix_cache.clear()
