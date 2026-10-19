"""
Darvas Box Breakout Detection — Book-sourced signal.

Source: Nicholas Darvas, "How I Made $2 Million in the Stock Market"

The Darvas Box method identifies consolidation ranges ("boxes") and
generates signals when price breaks out of the box on above-average
volume.  The box low provides a natural stop-loss level.

Usage
-----
    from tradingview.analysis.darvas_box import detect_darvas_box

    result = detect_darvas_box(ohlcv_df)
    # result = {"signal": "darvas", "score": 8, "detail": "BREAKOUT ...",
    #           "box_high": 150.0, "box_low": 140.0}
"""

from __future__ import annotations

import sys

import pandas as pd

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def detect_darvas_box(
    df: pd.DataFrame,
    box_period: int = 15,
) -> dict:
    """Detect a Darvas Box and classify the current bar as breakout/breakdown/inside.

    Parameters
    ----------
    df : pd.DataFrame
        Must have lowercase columns: open, high, low, close, volume.
        At least ``box_period + 1`` rows required.
    box_period : int
        Number of bars to define the box (default 15).

    Returns
    -------
    dict with keys:
        signal (str), score (int), detail (str),
        box_high (float | None), box_low (float | None)
    """
    if len(df) < box_period + 1:
        return {
            "signal": "darvas",
            "score": 0,
            "detail": "insufficient data",
            "box_high": None,
            "box_low": None,
        }

    # The box is defined by the preceding box_period bars (excluding today)
    box_data = df.iloc[-(box_period + 1) : -1]
    last = df.iloc[-1]

    box_high = float(box_data["high"].max())
    box_low = float(box_data["low"].min())

    # Volume context
    vol_avg = float(box_data["volume"].mean())
    vol_ratio = float(last["volume"]) / vol_avg if vol_avg > 0 else 1.0
    high_vol = vol_ratio > 1.5

    close = float(last["close"])

    result: dict = {
        "signal": "darvas",
        "box_high": round(box_high, 4),
        "box_low": round(box_low, 4),
    }

    # ── Classification ──

    if close > box_high and high_vol:
        result.update({
            "score": 8,
            "detail": f"BREAKOUT above ${box_high:.2f} on {vol_ratio:.1f}x volume",
        })
    elif close < box_low and high_vol:
        result.update({
            "score": -5,
            "detail": f"BREAKDOWN below ${box_low:.2f} on {vol_ratio:.1f}x volume",
        })
    elif close > box_high:
        result.update({
            "score": 3,
            "detail": f"Above box ${box_high:.2f} (low volume — {vol_ratio:.1f}x)",
        })
    elif close < box_low:
        result.update({
            "score": -2,
            "detail": f"Below box ${box_low:.2f} (low volume — {vol_ratio:.1f}x)",
        })
    else:
        result.update({
            "score": 0,
            "detail": f"Inside box ${box_low:.2f}–${box_high:.2f}",
        })

    return result
