"""
Opening Gap Classification — Book-sourced signal.

Source: Thomas Bulkowski, "Encyclopedia of Chart Patterns"

Classifies opening gaps as Exhaustion, Continuation, or Common based
on gap size, proximity to 52-week high/low, and volume context.

Usage
-----
    from tradingview.analysis.gap_analysis import classify_gap

    result = classify_gap(ohlcv_df)
    # result = {"signal": "gap", "score": 6, "gap_pct": 3.2,
    #           "detail": "CONTINUATION GAP UP +3.2% on 2.1x volume"}
"""

from __future__ import annotations

import sys

import pandas as pd

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def classify_gap(df: pd.DataFrame, lookback_52w: int = 252) -> dict:
    """Classify the most recent opening gap.

    Parameters
    ----------
    df : pd.DataFrame
        Must have lowercase columns: open, high, low, close, volume.
        At least 3 rows required; ideally ``lookback_52w + 1`` rows for
        52-week high/low context.
    lookback_52w : int
        Number of bars for 52-week high/low (default 252 trading days).

    Returns
    -------
    dict with keys: signal (str), score (int), detail (str), gap_pct (float)
    """
    if len(df) < 3:
        return {"signal": "gap", "score": 0, "detail": "insufficient data", "gap_pct": 0.0}

    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_close = float(prev["close"])

    if prev_close == 0:
        return {"signal": "gap", "score": 0, "detail": "zero prev close", "gap_pct": 0.0}

    curr_open = float(curr["open"])
    gap_pct = (curr_open - prev_close) / prev_close * 100.0
    gap_up = gap_pct > 0

    # Volume context (relative to prior bar)
    prev_vol = float(prev["volume"])
    curr_vol = float(curr["volume"])
    vol_ratio = curr_vol / prev_vol if prev_vol > 0 else 1.0

    # 52-week high/low context
    hist_window = df.iloc[-min(lookback_52w, len(df)) :]
    high_52w = float(hist_window["high"].max())
    low_52w = float(hist_window["low"].min())
    at_52w_high = float(curr["high"]) >= high_52w * 0.98
    at_52w_low = float(curr["low"]) <= low_52w * 1.02

    abs_gap = abs(gap_pct)

    # ── Gap Classification ──

    # Exhaustion gap: large gap at 52w extreme with high volume
    if abs_gap >= 2.0 and vol_ratio >= 2.0:
        if (gap_up and at_52w_high) or (not gap_up and at_52w_low):
            score = -4 if gap_up else 4
            direction = "UP" if gap_up else "DOWN"
            return {
                "signal": "gap",
                "score": score,
                "gap_pct": round(gap_pct, 2),
                "detail": f"EXHAUSTION GAP {direction} {gap_pct:+.1f}% on {vol_ratio:.1f}x vol at 52w {'high' if gap_up else 'low'}",
            }

    # Continuation gap: significant gap with elevated volume (mid-trend)
    if abs_gap >= 2.0 and vol_ratio >= 1.5:
        score = 6 if gap_up else -6
        direction = "UP" if gap_up else "DOWN"
        return {
            "signal": "gap",
            "score": score,
            "gap_pct": round(gap_pct, 2),
            "detail": f"CONTINUATION GAP {direction} {gap_pct:+.1f}% on {vol_ratio:.1f}x vol",
        }

    # Common gap: small or unsupported by volume
    return {
        "signal": "gap",
        "score": 0,
        "gap_pct": round(gap_pct, 2),
        "detail": f"COMMON GAP {gap_pct:+.1f}% (vol {vol_ratio:.1f}x — not significant)",
    }
