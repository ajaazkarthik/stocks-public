"""
Options Hedger — Strategy Logic (re-export shim)
=================================================
Split completed 2026-06-05. Functions now live in dedicated sub-modules:

  India hedge rules  → options_hedger_india.py
  Global hedge rules → options_hedger_global.py

This module re-exports the public API for backward compatibility with any
existing callers that import from options_hedger_strategies.
"""
from __future__ import annotations

# Re-export India hedge functions
from .options_hedger_india import (   # noqa: F401
    _recommend_hedge,
    _add_nifty_beta_hedge,
    _build_nifty_portfolio_hedge,
)

# Re-export global hedge functions
from .options_hedger_global import (   # noqa: F401
    _recommend_hedge_global,
    _recommend_hedge_long_option,
)
