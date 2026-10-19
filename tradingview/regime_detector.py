"""
Regime Detector — Market regime detection using VIX level
==========================================================
Detects 4 market regimes based on CBOE VIX level and returns
adjusted L1-L4 weight vectors for the composite scoring system.

Regimes:
  BULL_QUIET  — VIX < 15:   Strong bull, calm market. Tech/momentum favoured.
  NORMAL      — 15 ≤ VIX < 25: Normal conditions. Balanced weights (baseline).
  ELEVATED    — 25 ≤ VIX < 35: Fear rising. Shift to fundamentals + sentiment.
  CRISIS      — VIX ≥ 35:   Market panic. Heavy on sentiment + internals.

Benchmark evidence (2026-04-29, sell_history.db):
  - NORMAL VIX WR = 45.2% vs ELEVATED VIX WR = 38.8% (6.4pp spread)
  - Regime-aware weighting shifts emphasis from momentum (L1) to
    fundamentals (L2) and sentiment (L3) during elevated volatility.

Usage:
    from regime_detector import detect_regime, get_regime_weights

    regime = detect_regime(vix_level=28.5)
    # → {"regime": "ELEVATED", "vix": 28.5, "weights": {...}}

    w = get_regime_weights(vix_level=28.5)
    # → {"l1": 0.25, "l2": 0.30, "l3": 0.25, "l4": 0.20}
"""

import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ─── Regime Definitions ──────────────────────────────────────────────────────

# Baseline weights (current signal_stack.py defaults)
_BASELINE_WEIGHTS = {"l1": 0.30, "l2": 0.30, "l3": 0.20, "l4": 0.20}

# Regime-specific weight overrides
# Rationale:
#   BULL_QUIET: Technical momentum (L1) matters most; sentiment (L3) is noise.
#   NORMAL:     Baseline — balanced across all layers.
#   ELEVATED:   Fundamentals (L2) + sentiment (L3) become critical safety nets.
#   CRISIS:     Market internals (L4: VIX, PCR) + sentiment dominate; technicals unreliable.
_REGIME_WEIGHTS = {
    "BULL_QUIET": {"l1": 0.35, "l2": 0.30, "l3": 0.15, "l4": 0.20},
    "NORMAL":     {"l1": 0.30, "l2": 0.30, "l3": 0.20, "l4": 0.20},
    "ELEVATED":   {"l1": 0.25, "l2": 0.30, "l3": 0.25, "l4": 0.20},
    "CRISIS":     {"l1": 0.20, "l2": 0.25, "l3": 0.25, "l4": 0.30},
}

# VIX thresholds for regime classification
_VIX_THRESHOLDS = {
    "BULL_QUIET": 15.0,
    "NORMAL":     25.0,
    "ELEVATED":   35.0,
    # CRISIS: VIX >= 35
}

# Module-level cache: last successfully observed VIX value
_last_vix: float | None = None


def classify_regime(vix_level: float | None) -> str:
    """Classify market regime from VIX level. Returns regime string."""
    global _last_vix
    if vix_level is not None:
        _last_vix = vix_level
    else:
        # Use cached VIX if available; otherwise default to NORMAL
        vix_level = _last_vix
        if vix_level is None:
            return "NORMAL"
    if vix_level < _VIX_THRESHOLDS["BULL_QUIET"]:
        return "BULL_QUIET"
    if vix_level < _VIX_THRESHOLDS["NORMAL"]:
        return "NORMAL"
    if vix_level < _VIX_THRESHOLDS["ELEVATED"]:
        return "ELEVATED"
    return "CRISIS"


def get_regime_weights(vix_level: float | None) -> dict:
    """Return L1-L4 weights for the current VIX regime."""
    regime = classify_regime(vix_level)
    return dict(_REGIME_WEIGHTS[regime])


def detect_regime(vix_level: float | None) -> dict:
    """
    Full regime detection result.

    Returns:
        dict with keys:
            regime:  str — one of BULL_QUIET, NORMAL, ELEVATED, CRISIS
            vix:     float | None — current VIX level
            weights: dict — L1-L4 weight vector
            label:   str — human-readable label
    """
    regime = classify_regime(vix_level)
    weights = dict(_REGIME_WEIGHTS[regime])

    labels = {
        "BULL_QUIET": "🟢 Bull Quiet (VIX < 15)",
        "NORMAL":     "🔵 Normal (VIX 15–25)",
        "ELEVATED":   "🟡 Elevated (VIX 25–35)",
        "CRISIS":     "🔴 Crisis (VIX ≥ 35)",
    }

    return {
        "regime":  regime,
        "vix":     vix_level,
        "weights": weights,
        "label":   labels.get(regime, "Unknown"),
    }


if __name__ == "__main__":
    # Quick self-test
    for v in [None, 12.5, 18.0, 28.0, 42.0]:
        r = detect_regime(v)
        print(f"VIX={v!s:>5} → {r['regime']:<12} {r['label']:<30}  weights={r['weights']}")
