"""
TQS v2 — Strategy Presets
==========================
Twelve pre-built TQSv2Config variants covering different risk/reward profiles.
6 CALL presets + 6 PUT presets.

Usage:
    from tradingview.options.tqs_v2.presets import STRATEGY_PRESETS

    cfg = STRATEGY_PRESETS["budget_calls"]          # $4 max, 7–21 DTE (shortest theta)
    cfg = STRATEGY_PRESETS["scalp_calls"]           # 3–7 DTE weeklies, high gamma
    cfg = STRATEGY_PRESETS["momentum_calls"]        # RSI 50–68, rel_vol >1.2, 14–30 DTE
    cfg = STRATEGY_PRESETS["standard_calls"]        # balanced default
    cfg = STRATEGY_PRESETS["swing_calls"]           # 45–120 DTE, LEAPS-lite
    cfg = STRATEGY_PRESETS["high_conviction_calls"] # 30–60 DTE, high OI, tight spread
    cfg = STRATEGY_PRESETS["budget_puts"]           # $4 max, 7–21 DTE
    cfg = STRATEGY_PRESETS["scalp_puts"]            # 3–7 DTE weeklies, high gamma
    cfg = STRATEGY_PRESETS["momentum_puts"]         # RSI 30–50, rel_vol >1.2, 14–30 DTE
    cfg = STRATEGY_PRESETS["standard_puts"]         # balanced default
    cfg = STRATEGY_PRESETS["swing_puts"]            # 45–120 DTE, LEAPS-lite
    cfg = STRATEGY_PRESETS["high_conviction_puts"]  # 30–60 DTE, high OI, tight spread

Preset matrix
─────────────────────────────────────────────────────────────────────────────────────────
Name                   Type  DTE       Delta          Premium  OI    RSI      Profile
─────────────────────────────────────────────────────────────────────────────────────────
budget_calls           CALL   7–21  0.20–0.45        ≤$4      1500  any      Low budget, shortest DTE
scalp_calls            CALL   3–7   0.35–0.65        ≤$2.50   2000  any      High-gamma weeklies
momentum_calls         CALL  14–30  0.25–0.50        ≤$4      1500  50–68    Trending/momentum stocks
standard_calls         CALL  21–90  0.20–0.55        ≤$4      1500  any      Balanced (default)
swing_calls            CALL  45–120 0.15–0.35        ≤$4      1000  any      LEAPS-lite swing
high_conviction_calls  CALL  30–60  0.30–0.55        ≤$4      2000  any      High OI + tight spread
budget_puts            PUT    7–21  −0.45–−0.20     ≤$4      1500  any      Low budget, shortest DTE
scalp_puts             PUT    3–7   −0.65–−0.35     ≤$2.50   2000  any      High-gamma weeklies
momentum_puts          PUT   14–30  −0.50–−0.25     ≤$4      1500  30–50    Bearish/oversold stocks
standard_puts          PUT   21–90  −0.55–−0.20     ≤$4      1500  any      Balanced (default)
swing_puts             PUT   45–120 −0.35–−0.15     ≤$4      1000  any      LEAPS-lite swing
high_conviction_puts   PUT   30–60  −0.55–−0.30     ≤$4      2000  any      High OI + tight spread
─────────────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from .config import TQSv2Config


# ── CALL presets ─────────────────────────────────────────────────────────────

budget_calls = TQSv2Config(
    contract_min_dte=7,             # REDUCED from 21: shortest DTE = lowest time premium = fits $4
    contract_max_dte=21,            # REDUCED from 45: 7–21 DTE is the sweet spot for cheap options
    call_min_delta=0.20,
    call_max_delta=0.45,
    contract_max_premium=4.00,
    min_open_interest=1500,
    contract_max_spread_pct=12.0,
    min_score_for_report=65.0,
    stock_max_price=150.0,          # $150 cap: options on cheaper stocks stay within $4 budget
)
"""Near-term CALL within a $4 budget.  DTE 7–21 captures the sharpest theta decay
window while keeping premiums under $4.  Use on $10–$150 stocks (options stay cheap).
Good for short directional bets or pre-earnings plays."""

scalp_calls = TQSv2Config(
    contract_min_dte=3,             # ultra-short: 3–7 DTE weeklies
    contract_max_dte=7,
    call_min_delta=0.35,            # near-ATM for maximum gamma exposure
    call_max_delta=0.65,
    contract_max_premium=2.50,      # tighter budget: weeklies must be cheaper
    min_open_interest=2000,         # strict liquidity: weeklies need high OI for easy exit
    contract_max_spread_pct=10.0,   # tight spread required (weeklies can be illiquid)
    min_score_for_report=68.0,      # higher quality threshold: gamma is ruthless on low-quality
    stock_max_price=150.0,
)
"""High-gamma CALL weeklies (3–7 DTE).  Maximum directional leverage per dollar.
Near-ATM delta (0.35–0.65) maximises gamma; strict OI and spread ensure clean exits.
Best for stocks with a clear catalyst within the week (e.g., FOMC, earnings)."""

momentum_calls = TQSv2Config(
    contract_min_dte=14,
    contract_max_dte=30,
    call_min_delta=0.25,
    call_max_delta=0.50,
    contract_max_premium=4.00,
    min_open_interest=1500,
    contract_max_spread_pct=12.0,
    min_score_for_report=65.0,
    stock_rsi_min=50.0,             # bullish momentum zone: RSI > 50
    stock_rsi_max=68.0,             # not overbought: RSI < 68 (avoids exhaustion fades)
    stock_min_relative_volume=1.2,  # actively trading TODAY (1.2× = 20% above avg volume)
)
"""Momentum CALL scan — targets stocks with bullish RSI (50–68) and elevated
relative volume (>1.2×).  Two-to-four week DTE gives trend time to develop.
Designed for stocks that are moving RIGHT NOW, not just big-cap laggards."""

standard_calls = TQSv2Config(
    contract_min_dte=21,
    contract_max_dte=90,
    call_min_delta=0.20,
    call_max_delta=0.55,
    contract_max_premium=4.00,
    min_open_interest=1500,
    contract_max_spread_pct=12.0,
    min_score_for_report=65.0,
)
"""Balanced CALL scan — the production default.  Covers both near-term catalysts
and medium-term trend trades in a single scan."""

swing_calls = TQSv2Config(
    contract_min_dte=45,
    contract_max_dte=120,
    call_min_delta=0.15,
    call_max_delta=0.35,
    contract_max_premium=4.00,
    min_open_interest=1000,         # relaxed — liquidity thins at longer expiries
    contract_max_spread_pct=15.0,   # slightly relaxed for LEAPS-lite contracts
    min_score_for_report=62.0,
)
"""Longer-dated CALL for swing / multi-week trend trades.  Lower delta range
gives higher leverage; lower OI threshold allows LEAPS-lite contracts."""

high_conviction_calls = TQSv2Config(
    contract_min_dte=30,
    contract_max_dte=60,
    call_min_delta=0.30,
    call_max_delta=0.55,
    contract_max_premium=4.00,
    min_open_interest=2000,         # strict OI: institutional-grade liquidity only
    contract_max_spread_pct=10.0,   # tight spread: quality execution at any size
    min_score_for_report=68.0,      # higher bar: only the best-scoring contracts
)
"""High-conviction CALL for when you have strong thesis confidence.
Strict OI (2000+) and spread (<10%) ensure best-in-class liquidity.
Four to eight weeks gives price action time to unfold without excessive theta."""


# ── PUT presets ──────────────────────────────────────────────────────────────

budget_puts = TQSv2Config(
    contract_min_dte=7,             # REDUCED from 21: matches budget_calls logic
    contract_max_dte=21,            # REDUCED from 45
    put_min_delta=-0.45,
    put_max_delta=-0.20,
    contract_max_premium=4.00,
    min_open_interest=1500,
    contract_max_spread_pct=12.0,
    min_score_for_report=65.0,
    stock_max_price=150.0,
)
"""Near-term PUT within a $4 budget.  DTE 7–21 keeps premium affordable.
Ideal for short-term downside protection or bearish speculative plays."""

scalp_puts = TQSv2Config(
    contract_min_dte=3,
    contract_max_dte=7,
    put_min_delta=-0.65,
    put_max_delta=-0.35,
    contract_max_premium=2.50,
    min_open_interest=2000,
    contract_max_spread_pct=10.0,
    min_score_for_report=68.0,
    stock_max_price=150.0,
)
"""High-gamma PUT weeklies (3–7 DTE).  Near-ATM delta (−0.35–−0.65) maximises
downside gamma.  Best for stocks with imminent bearish catalysts.
Requires strict liquidity (OI 2000+) for clean exits."""

momentum_puts = TQSv2Config(
    contract_min_dte=14,
    contract_max_dte=30,
    put_min_delta=-0.50,
    put_max_delta=-0.25,
    contract_max_premium=4.00,
    min_open_interest=1500,
    contract_max_spread_pct=12.0,
    min_score_for_report=65.0,
    stock_rsi_min=30.0,             # bearish zone: RSI > 30 (avoid extreme oversold)
    stock_rsi_max=50.0,             # bearish momentum: RSI < 50 (below midline)
    stock_min_relative_volume=1.2,  # actively breaking down TODAY
)
"""Bearish momentum PUT scan — targets stocks with RSI 30–50 (below midline)
and elevated relative volume (>1.2× = selling is accelerating).
Two-to-four week DTE lets the breakdown develop without extreme theta burn."""

standard_puts = TQSv2Config(
    contract_min_dte=21,
    contract_max_dte=90,
    put_min_delta=-0.55,
    put_max_delta=-0.20,
    contract_max_premium=4.00,
    min_open_interest=1500,
    contract_max_spread_pct=12.0,
    min_score_for_report=65.0,
)
"""Balanced PUT scan — the production default for bearish scans.  Covers both
short-term downside plays and medium-term hedges."""

swing_puts = TQSv2Config(
    contract_min_dte=45,
    contract_max_dte=120,
    put_min_delta=-0.35,
    put_max_delta=-0.15,
    contract_max_premium=4.00,
    min_open_interest=1000,
    contract_max_spread_pct=15.0,
    min_score_for_report=62.0,
)
"""Longer-dated PUT for multi-week bearish trend or tail-risk hedging.
Wider spread tolerance allows participation in lower-liquidity far expiries."""

high_conviction_puts = TQSv2Config(
    contract_min_dte=30,
    contract_max_dte=60,
    put_min_delta=-0.55,
    put_max_delta=-0.30,
    contract_max_premium=4.00,
    min_open_interest=2000,
    contract_max_spread_pct=10.0,
    min_score_for_report=68.0,
)
"""High-conviction PUT for strong bearish thesis.  Strict OI (2000+) and
tight spread (<10%) ensure institutional-grade liquidity.  Four to eight
weeks gives the bearish move time to develop."""


# ── Catalyst presets ─────────────────────────────────────────────────────────

catalyst_calls = TQSv2Config(
    # Short DTE: 3–21 to capture near-term catalyst moves
    contract_min_dte=3,
    contract_max_dte=21,
    # Near-ATM delta range: high gamma to capture the catalyst pop
    call_min_delta=0.25,
    call_max_delta=0.60,
    contract_max_premium=4.00,
    min_open_interest=1000,         # relaxed — pre-catalyst OI may still be building
    contract_max_spread_pct=15.0,   # relaxed — allow thinner markets before catalyst
    min_score_for_report=62.0,      # lower bar: catalyst_score compensates
    # Catalyst mode flags
    catalyst_mode=True,
    catalyst_max_picks=10,
    catalyst_earnings_window_days=14,
    catalyst_min_rel_volume=1.0,
    catalyst_price_min_pct_change=0.02,
    # Preset metadata for output naming
    preset_name="catalyst_calls",
    preset_option_type="call",
    # Output files
    call_html_report="tqs_v2_catalyst_calls.html",
    call_json_report="tqs_v2_catalyst_calls.json",
)
"""Catalyst CALL scan — 3–21 DTE contracts targeting stocks with active near-term
catalysts (earnings, unusual options flow, breakouts, analyst upgrades).
Bypasses earnings proximity filter; uses catalyst scoring weights."""

catalyst_puts = TQSv2Config(
    contract_min_dte=3,
    contract_max_dte=21,
    put_min_delta=-0.60,
    put_max_delta=-0.25,
    contract_max_premium=4.00,
    min_open_interest=1000,
    contract_max_spread_pct=15.0,
    min_score_for_report=62.0,
    # Catalyst mode flags
    catalyst_mode=True,
    catalyst_max_picks=10,
    catalyst_earnings_window_days=14,
    catalyst_min_rel_volume=1.0,
    catalyst_price_min_pct_change=0.02,
    # Preset metadata
    preset_name="catalyst_puts",
    preset_option_type="put",
    # Output files
    put_html_report="tqs_v2_catalyst_puts.html",
    put_json_report="tqs_v2_catalyst_puts.json",
)
"""Catalyst PUT scan — 3–21 DTE contracts targeting stocks with active near-term
bearish catalysts (earnings disappointment risk, breakdowns, unusual put flow)."""


# ── Master preset registry ────────────────────────────────────────────────────
STRATEGY_PRESETS: dict[str, TQSv2Config] = {
    # ─── CALLS ───
    "budget_calls":          budget_calls,
    "scalp_calls":           scalp_calls,
    "momentum_calls":        momentum_calls,
    "standard_calls":        standard_calls,
    "swing_calls":           swing_calls,
    "high_conviction_calls": high_conviction_calls,
    # ─── PUTS ───
    "budget_puts":           budget_puts,
    "scalp_puts":            scalp_puts,
    "momentum_puts":         momentum_puts,
    "standard_puts":         standard_puts,
    "swing_puts":            swing_puts,
    "high_conviction_puts":  high_conviction_puts,
    # ─── CATALYST ───
    "catalyst_calls": catalyst_calls,
    "catalyst_puts":  catalyst_puts,
}


def get_preset(name: str) -> TQSv2Config:
    """Return a TQSv2Config for the named preset.

    Raises KeyError with a helpful message if the preset is not found.

    Example::

        cfg = get_preset("catalyst_calls")
        cfg = get_preset("standard_puts")
    """
    try:
        return STRATEGY_PRESETS[name]
    except KeyError:
        available = ", ".join(sorted(STRATEGY_PRESETS))
        raise KeyError(
            f"Unknown preset {name!r}. Available presets: {available}"
        ) from None


__all__ = [
    "STRATEGY_PRESETS",
    "get_preset",
    # calls
    "budget_calls", "scalp_calls", "momentum_calls",
    "standard_calls", "swing_calls", "high_conviction_calls",
    # puts
    "budget_puts", "scalp_puts", "momentum_puts",
    "standard_puts", "swing_puts", "high_conviction_puts",
    # catalyst
    "catalyst_calls", "catalyst_puts",
]
