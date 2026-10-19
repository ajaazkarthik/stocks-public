"""F9 — Multi-Timeframe Confluence Scorer.

Adds weekly SMA200/RSI confirmation to daily signals.
When RSI<40 on both daily AND weekly timeframes → +8 bonus (deep oversold confluence).
When price above SMA200 on both daily AND weekly → +4 bonus (trend confirmed).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class TimeframeData:
    """Technical data for a single timeframe."""
    timeframe: str       # "daily", "weekly"
    rsi: float | None = None
    sma200_pct: float | None = None   # % above/below SMA200
    adx: float | None = None
    macd_hist: float | None = None


@dataclass
class ConfluenceResult:
    """Multi-timeframe confluence scoring result."""
    symbol: str
    daily: TimeframeData | None = None
    weekly: TimeframeData | None = None
    rsi_confluence: bool = False      # RSI<40 on BOTH timeframes
    sma_confluence: bool = False      # above SMA200 on BOTH timeframes
    bonus: int = 0                    # total bonus points
    details: str = ""


def compute_confluence(
    symbol: str,
    daily: TimeframeData | None = None,
    weekly: TimeframeData | None = None,
) -> ConfluenceResult:
    """Compute multi-timeframe confluence bonus.

    Rules:
        - RSI < 40 on both daily AND weekly → +8 (deep oversold bounce)
        - RSI < 40 daily only → +2 (daily oversold without weekly confirmation)
        - Price > SMA200 on both timeframes → +4 (strong uptrend confirmation)
        - ADX > 25 on both → +2 (trending on both timeframes)
    """
    result = ConfluenceResult(symbol=symbol, daily=daily, weekly=weekly)

    if daily is None or weekly is None:
        result.details = "Missing timeframe data"
        return result

    bonus = 0
    details_parts: list[str] = []

    # RSI oversold confluence
    d_rsi = daily.rsi
    w_rsi = weekly.rsi
    if d_rsi is not None and w_rsi is not None:
        if d_rsi < 40 and w_rsi < 40:
            bonus += 8
            result.rsi_confluence = True
            details_parts.append(f"RSI confluence: D={d_rsi:.1f} W={w_rsi:.1f} → +8")
        elif d_rsi < 40:
            bonus += 2
            details_parts.append(f"Daily RSI oversold: {d_rsi:.1f} → +2")

    # SMA200 trend confluence
    d_sma = daily.sma200_pct
    w_sma = weekly.sma200_pct
    if d_sma is not None and w_sma is not None:
        if d_sma > 0 and w_sma > 0:
            bonus += 4
            result.sma_confluence = True
            details_parts.append(f"SMA200 uptrend: D={d_sma:+.1f}% W={w_sma:+.1f}% → +4")

    # ADX trending confluence
    d_adx = daily.adx
    w_adx = weekly.adx
    if d_adx is not None and w_adx is not None:
        if d_adx > 25 and w_adx > 25:
            bonus += 2
            details_parts.append(f"ADX trend: D={d_adx:.0f} W={w_adx:.0f} → +2")

    result.bonus = bonus
    result.details = "; ".join(details_parts) if details_parts else "No confluence signals"
    return result


def compute_l1_mtf_bonus(screener_daily: dict[str, Any], screener_weekly: dict[str, Any] | None = None) -> int:
    """Convenience wrapper for signal_stack.py integration.

    Args:
        screener_daily: dict with tv_rsi, sma200_pct, tv_adx keys.
        screener_weekly: dict with same keys for weekly timeframe.

    Returns:
        bonus points (0-14 range) to add to L1 score.
    """
    if not screener_weekly:
        return 0

    daily = TimeframeData(
        timeframe="daily",
        rsi=screener_daily.get("tv_rsi"),
        sma200_pct=screener_daily.get("sma200_pct"),
        adx=screener_daily.get("tv_adx"),
    )
    weekly = TimeframeData(
        timeframe="weekly",
        rsi=screener_weekly.get("tv_rsi"),
        sma200_pct=screener_weekly.get("sma200_pct"),
        adx=screener_weekly.get("tv_adx"),
    )

    result = compute_confluence("", daily, weekly)
    return result.bonus
