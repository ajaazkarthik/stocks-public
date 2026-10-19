"""
O'Neil Relative Strength (RS) Rating — Book-sourced signal.

Source: William O'Neil, "How to Make Money in Stocks" (CANSLIM system).
O'Neil ranks every stock's weighted multi-period return against all other
stocks in the universe on a 1–99 percentile scale.  Stocks with RS ≥ 80
are "leaders" — his research shows the biggest winners had RS ≥ 80 BEFORE
their major moves.

This is NOT RSI (Relative Strength Index).  It is a cross-sectional
percentile ranking of price performance.

Usage
-----
    from tradingview.analysis.rs_rating import compute_rs_rating, score_rs

    # returns dict  ticker → rs_value (1-99)
    ratings = compute_rs_rating(price_histories)

    # returns L1 score bonus for a single ticker
    bonus = score_rs(rs_value)
"""

from __future__ import annotations

import sys
from typing import Dict, Optional

import numpy as np
import pandas as pd

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def weighted_return(closes: pd.Series) -> Optional[float]:
    """Compute O'Neil weighted return from a price series (most recent last).

    Formula (O'Neil empirical weighting):
        weighted = 2 * ret_63d  +  ret_126d  +  0.5 * ret_252d

    The 2× weight on the most recent quarter captures momentum acceleration.
    Returns None if the series is too short (< 64 bars).
    """
    n = len(closes)
    if n < 64:
        return None

    last = closes.iloc[-1]
    if last <= 0:
        return None

    ret_63 = (last / closes.iloc[-64] - 1) if (n >= 64 and closes.iloc[-64] > 0) else 0.0
    ret_126 = (last / closes.iloc[-127] - 1) if (n >= 127 and closes.iloc[-127] > 0) else ret_63
    ret_252 = (last / closes.iloc[-253] - 1) if (n >= 253 and closes.iloc[-253] > 0) else ret_126

    result = 2.0 * ret_63 + ret_126 + 0.5 * ret_252
    if not np.isfinite(result):
        return None
    return result


def compute_rs_rating(
    price_histories: Dict[str, pd.Series],
) -> Dict[str, int]:
    """Compute RS rating (1-99) for every ticker in the universe.

    Parameters
    ----------
    price_histories : dict
        Mapping of ticker → pd.Series of daily close prices (oldest first).

    Returns
    -------
    dict  ticker → int (1-99)
    """
    raw: Dict[str, float] = {}
    for ticker, closes in price_histories.items():
        wr = weighted_return(closes)
        if wr is not None:
            raw[ticker] = wr

    if not raw:
        return {}

    tickers = list(raw.keys())
    values = np.array([raw[t] for t in tickers])

    # Percentile rank: fraction of values that are ≤ this value
    # Scale to 1-99 (O'Neil convention — no 0 or 100)
    ranks = np.array([
        np.sum(values <= v) / len(values) * 98 + 1
        for v in values
    ]).astype(int)
    ranks = np.clip(ranks, 1, 99)

    return dict(zip(tickers, ranks.tolist()))


# ---------------------------------------------------------------------------
# Scoring interface for signal_stack L1
# ---------------------------------------------------------------------------

def score_rs(rs_value: int) -> dict:
    """Return an L1 score adjustment based on the RS rating.

    Returns
    -------
    dict with keys: score (int), signal ("rs_rating"), detail (str)
    """
    if rs_value >= 80:
        return {"score": 6, "signal": "rs_rating", "detail": f"RS {rs_value} — leader (≥80)"}
    if rs_value >= 70:
        return {"score": 3, "signal": "rs_rating", "detail": f"RS {rs_value} — strong (≥70)"}
    if rs_value < 30:
        return {"score": -4, "signal": "rs_rating", "detail": f"RS {rs_value} — laggard (<30)"}
    return {"score": 0, "signal": "rs_rating", "detail": f"RS {rs_value} — neutral"}


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute O'Neil RS Rating for a universe of tickers")
    parser.add_argument("--tickers", nargs="+", default=["AAPL", "MSFT", "NVDA", "TSLA", "META"],
                        help="Tickers to evaluate")
    args = parser.parse_args()

    try:
        import yfinance as yf  # noqa: E402

        print(f"Fetching 1-year price history for {len(args.tickers)} tickers...")
        data = yf.download(args.tickers, period="1y", progress=False)
        if "Close" not in data.columns if hasattr(data.columns, "__iter__") else True:
            print("No data returned")
            sys.exit(1)

        close_df = data["Close"] if len(args.tickers) > 1 else data[["Close"]].rename(columns={"Close": args.tickers[0]})
        histories = {t: close_df[t].dropna() for t in close_df.columns}
        ratings = compute_rs_rating(histories)

        print("\n=== O'Neil RS Ratings ===")
        for t, rs in sorted(ratings.items(), key=lambda x: -x[1]):
            bonus = score_rs(rs)
            print(f"  {t:8s}  RS={rs:3d}  L1 bonus={bonus['score']:+d}  ({bonus['detail']})")
    except ImportError:
        print("yfinance not installed — run with synthetic data only")
