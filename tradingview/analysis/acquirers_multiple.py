"""
Acquirer's Multiple (EV/EBIT) Scoring — Book-sourced L2 signal.

Source: Tobias Carlisle, "The Acquirer's Multiple"

The Acquirer's Multiple = Enterprise Value / EBIT.
Screens for deeply undervalued companies (AM < 7) that institutional
acquirers target.  Lower is better.

Usage
-----
    from tradingview.analysis.acquirers_multiple import (
        compute_acquirers_multiple,
        score_acquirers_multiple,
    )

    am = compute_acquirers_multiple(ev=50_000_000_000, ebit=8_000_000_000)
    result = score_acquirers_multiple(am)
    # result = {"signal": "acquirers_multiple", "score": 2, "detail": "AM 6.25 ..."}
"""

from __future__ import annotations

import math
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def compute_acquirers_multiple(ev: float | None, ebit: float | None) -> float | None:
    """Compute EV / EBIT.

    Parameters
    ----------
    ev : float | None
        Enterprise value.
    ebit : float | None
        Earnings before interest and taxes.

    Returns
    -------
    float | None
        The multiple, or None if inputs are missing or EBIT ≤ 0.
    """
    if ev is None or ebit is None:
        return None
    if not math.isfinite(ev) or not math.isfinite(ebit):
        return None
    if ebit <= 0:
        return None
    return ev / ebit


def score_acquirers_multiple(am: float | None) -> dict:
    """Score an Acquirer's Multiple for L2 integration.

    Thresholds (Carlisle-derived):
        AM <  7  → +4  (deep value)
        AM  7-12 → +2  (undervalued)
        AM 12-20 →  0  (fair value)
        AM 20-30 → -1  (overvalued)
        AM > 30  → -3  (expensive)

    Parameters
    ----------
    am : float | None
        The Acquirer's Multiple from :func:`compute_acquirers_multiple`.

    Returns
    -------
    dict with keys: signal (str), score (int), detail (str)
    """
    if am is None:
        return {"signal": "acquirers_multiple", "score": 0, "detail": "no data"}

    if am < 7:
        score = 4
        label = "DEEP VALUE"
    elif am < 12:
        score = 2
        label = "UNDERVALUED"
    elif am < 20:
        score = 0
        label = "FAIR VALUE"
    elif am < 30:
        score = -1
        label = "OVERVALUED"
    else:
        score = -3
        label = "EXPENSIVE"

    return {
        "signal": "acquirers_multiple",
        "score": score,
        "detail": f"AM {am:.2f} — {label}",
    }
