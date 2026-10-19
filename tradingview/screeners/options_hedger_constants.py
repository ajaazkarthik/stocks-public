"""
Shared constants for the options hedger module family.

All hedge threshold values, lot sizes, and contract sizes are defined
here so every sub-module imports from a single source of truth.
"""
from __future__ import annotations

# ── Hedge decision thresholds ──────────────────────────────────────────────
BIG_GAIN_PCT       = 30.0    # %PnL above which to consider Protective Put / Collar
MODERATE_GAIN_PCT  = 15.0    # %PnL above which to consider Covered Call
LOSS_PCT           = -10.0   # %PnL below which to flag for EXIT review
LARGE_POS_INR      = 100_000 # position value above which to prioritise hedging
INDIA_VIX_FEAR     = 20.0    # India VIX above this → elevate hedges (fear zone)

# NSE NIFTY lot size (post-2024 change)
NIFTY_LOT_SIZE = 75

# SPY and US options contract size
US_OPTION_CONTRACT = 100   # 1 options contract = 100 shares
