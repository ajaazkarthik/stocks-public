"""
Volume Price Analysis (VPA) Anomaly Scoring — Book-sourced signal.

Source: Anna Coulling, "A Complete Guide to Volume Price Analysis"

Detects institutional accumulation/distribution by analyzing the
relationship between candle spread (body size vs range) and volume.
Key anomalies:

- **Trap Up**: Narrow spread + high volume at resistance → distribution (bearish)
- **Stopping Volume**: Narrow spread + high volume at support → accumulation (bullish)
- **No Demand**: Narrow up-close + low volume → weak rally, likely to reverse
- **No Supply**: Narrow down-close + low volume → pullback exhausting, likely to continue

Usage
-----
    from tradingview.analysis.vpa_anomaly import vpa_signals

    result = vpa_signals(ohlcv_df)
    # result = {"signal": "vpa", "score": -5, "detail": "TRAP UP — ..."}
"""

from __future__ import annotations

import sys

import pandas as pd

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def vpa_signals(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Analyze OHLCV DataFrame for VPA anomalies on the most recent bar.

    Parameters
    ----------
    df : pd.DataFrame
        Must have lowercase columns: open, high, low, close, volume.
        At least ``lookback + 1`` rows required.
    lookback : int
        Number of bars for average volume and high/low context (default 20).

    Returns
    -------
    dict with keys: signal (str), score (int), detail (str)
    """
    if len(df) < lookback + 1:
        return {"signal": "vpa", "score": 0, "detail": "insufficient data"}

    recent = df.iloc[-(lookback + 1):]
    last = recent.iloc[-1]
    history = recent.iloc[:-1]

    o = float(last["open"])
    h = float(last["high"])
    low = float(last["low"])
    c = float(last["close"])
    v = float(last["volume"])

    candle_range = h - low
    if candle_range <= 0:
        return {"signal": "vpa", "score": 0, "detail": "zero range candle"}

    # Spread ratio: body size relative to full range
    spread_ratio = abs(c - o) / candle_range
    narrow = spread_ratio < 0.3

    # Volume relative to 20-bar average
    vol_avg = float(history["volume"].mean())
    vol_ratio = v / vol_avg if vol_avg > 0 else 1.0
    high_vol = vol_ratio > 1.5
    low_vol = vol_ratio < 0.5

    # Context: is the bar near recent high/low?
    recent_high = float(history["high"].max())
    recent_low = float(history["low"].min())
    near_high = h >= recent_high * 0.98
    near_low = low <= recent_low * 1.02

    # ── VPA Anomaly Classification ──

    # Trap Up: narrow spread + high volume near resistance
    if narrow and high_vol and near_high:
        return {
            "signal": "vpa",
            "score": -5,
            "detail": f"TRAP UP — narrow spread ({spread_ratio:.2f}) + high vol ({vol_ratio:.1f}x) at resistance",
        }

    # Stopping Volume: narrow spread + high volume near support
    if narrow and high_vol and near_low:
        return {
            "signal": "vpa",
            "score": 5,
            "detail": f"STOPPING VOLUME — narrow spread ({spread_ratio:.2f}) + high vol ({vol_ratio:.1f}x) at support",
        }

    # No Demand: narrow up-close on low volume
    if narrow and low_vol and c > o:
        return {
            "signal": "vpa",
            "score": -3,
            "detail": f"NO DEMAND — narrow up ({spread_ratio:.2f}) + low vol ({vol_ratio:.1f}x)",
        }

    # No Supply: narrow down-close on low volume
    if narrow and low_vol and c <= o:
        return {
            "signal": "vpa",
            "score": 3,
            "detail": f"NO SUPPLY — narrow down ({spread_ratio:.2f}) + low vol ({vol_ratio:.1f}x)",
        }

    return {
        "signal": "vpa",
        "score": 0,
        "detail": f"no anomaly (spread={spread_ratio:.2f}, vol={vol_ratio:.1f}x)",
    }
