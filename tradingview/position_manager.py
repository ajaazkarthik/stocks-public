"""
Position Manager — Scaling, Profit-Taking, and Position Quality Engine
=======================================================================
Answers three questions for every live position that sell_engine already scored:

  1. PROFIT LADDER  — Graduated profit-taking at TP1/2/3/4 R-multiples.
                      How many shares to sell now, and what remains?

  2. SCALING IN     — Should we add to this winner (pyramid), average down
                      on this loser, or do nothing?

  3. POSITION QUALITY — Was the initial sizing optimal? If you held too few
                         units through a big winner, what was the opportunity cost?

All logic is pure (no network calls, no yfinance, no TV screener I/O).
Input:  result dict from SellEngine.apply_exit_rules() + optional portfolio_context.
Output: same dict with profit/scaling/quality fields merged in.

Profit ladder framework
-----------------------
  TP1 @ 1.5R → sell 25 % of position  (lock in gains, reduce risk)
  TP2 @ 3.0R → sell another 25 %      (book half of original; trail rest free-ride)
  TP3 @ 5.0R → sell another 25 %      (very profitable — big haircut)
  TP4 @ 7.0R → sell final 25 %        (trail a residual or cash out)

  Cumulative cap: 75 % (always keep ≥ 1 share trailing for full upside capture).

Pyramid-add framework
---------------------
  Condition: gain 5–50 %, RSI 50–72, ADX ≥ 22, VIX < 25, decay_count == 0,
             not past TP2, position value ≤ 20 % of portfolio.
  Quantity:  50 % of current holding size, capped by available cash.

Average-down framework
----------------------
  Condition: loss 5–20 %, not already averaged down, backtest conviction ≥ 2 units,
             VIX ≤ 28, decay_count ≤ 1.
  Quantity:  50 % of current holding size, capped by available cash.
  Never average down more than ONCE (risk-management discipline).

Position quality
----------------
  Compare current holding size to what backtest_sizer recommended (or heuristic).
  Report UNDERSIZED / OVERSIZED / OPTIMAL + dollar opportunity cost for UNDERSIZED.
"""

from __future__ import annotations

import math
from typing import Optional


# ─── Conviction Scorer ────────────────────────────────────────────────────────
# Multi-factor conviction scoring for smarter take-profit and buy-more decisions.
# Pure logic — no network calls.  Expects pre-fetched data in a dict.

# Factor weights (must sum to 1.0)
_CONVICTION_WEIGHTS = {
    "fundamental":  0.30,
    "technical":    0.25,
    "sentiment":    0.20,
    "macro":        0.15,
    "valuation":    0.10,
}

class ConvictionScorer:
    """
    Scores a position's conviction 0–10 using five factors.

    Input ``conviction_data`` dict keys (all optional, safe defaults used):
      fundamental:
        earnings_growth  (float, %)
        revenue_growth   (float, %)
        profit_margin    (float, %)
        roe              (float, %)
        debt_to_equity   (float)
        free_cashflow    (float, raw $)
        analyst_target   (float, mean target price)
        current_price    (float)
      technical:
        rsi              (float, 0–100)
        adx              (float, 0–100)
        macd_bullish     (bool)
        ema_bullish      (bool)
      sentiment:
        news_score       (float, –1 to +1, VADER compound)
        analyst_bull     (float, 0–2, bull_ratio from analyst consensus)
        recent_downgrade (bool)
        earnings_beat_rate (float, 0–1)
      macro:
        vix              (float)
        yield_curve_inv  (bool)
      valuation:
        forward_pe       (float)
        peg_ratio        (float)

    Returns dict with:
        conviction_score   float 0–10
        conviction_label   str (STRONG_BUY / BUY / NEUTRAL / WEAK / VERY_WEAK)
        factor_scores      dict[str, float]  per-factor 0–10
        macro_crisis       bool  (VIX>35 or news_score < −0.5)
        earnings_imminent  bool
    """

    # ----- helpers -----
    @staticmethod
    def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
        return max(lo, min(hi, v))

    # ----- individual factor scorers -----

    @staticmethod
    def _score_fundamental(d: dict) -> float:
        """Score 0–10 from fundamental data."""
        pts = 0.0
        n = 0

        eg = d.get("earnings_growth")
        if eg is not None:
            n += 1
            if eg > 25:   pts += 10
            elif eg > 15: pts += 8
            elif eg > 5:  pts += 6
            elif eg > 0:  pts += 4
            else:         pts += 2

        rg = d.get("revenue_growth")
        if rg is not None:
            n += 1
            if rg > 20:   pts += 10
            elif rg > 10: pts += 8
            elif rg > 5:  pts += 6
            elif rg > 0:  pts += 4
            else:         pts += 2

        pm = d.get("profit_margin")
        if pm is not None:
            n += 1
            if pm > 20:   pts += 10
            elif pm > 10: pts += 7
            elif pm > 5:  pts += 5
            else:         pts += 3

        roe = d.get("roe")
        if roe is not None:
            n += 1
            if roe > 20:   pts += 10
            elif roe > 15: pts += 8
            elif roe > 10: pts += 6
            else:          pts += 3

        dte = d.get("debt_to_equity")
        if dte is not None:
            n += 1
            if dte < 30:    pts += 10
            elif dte < 80:  pts += 7
            elif dte < 150: pts += 4
            else:           pts += 2

        fcf = d.get("free_cashflow")
        if fcf is not None:
            n += 1
            pts += 8 if fcf > 0 else 3

        # Analyst target upside
        target = d.get("analyst_target")
        price = d.get("current_price")
        if target and price and price > 0:
            n += 1
            upside = (target - price) / price * 100
            if upside > 30:   pts += 10
            elif upside > 15: pts += 8
            elif upside > 5:  pts += 6
            elif upside > 0:  pts += 4
            else:             pts += 2

        return ConvictionScorer._clamp(pts / max(n, 1))

    @staticmethod
    def _score_technical(d: dict) -> float:
        """Score 0–10 from technical indicators."""
        pts = 0.0

        rsi = d.get("rsi")
        if rsi is not None:
            if 50 <= rsi <= 70:  pts += 3.0  # healthy momentum
            elif 40 <= rsi < 50: pts += 2.0  # weakening
            elif rsi > 70:       pts += 1.5  # overbought risk
            else:                pts += 0.5  # oversold / broken

        adx = d.get("adx")
        if adx is not None:
            if adx >= 30:   pts += 3.0  # strong trend
            elif adx >= 22: pts += 2.0  # trending
            else:           pts += 0.5  # no trend

        if d.get("macd_bullish"):
            pts += 2.0

        if d.get("ema_bullish"):
            pts += 2.0

        return ConvictionScorer._clamp(pts)

    @staticmethod
    def _score_sentiment(d: dict) -> float:
        """Score 0–10 from sentiment data."""
        pts = 0.0

        ns = d.get("news_score")
        if ns is not None:
            # VADER compound: -1 to +1
            pts += ConvictionScorer._clamp((ns + 1) * 3.5, 0, 7)  # maps -1→0, 0→3.5, +1→7

        ab = d.get("analyst_bull")
        if ab is not None:
            pts += min(ab, 2.0)  # 0-2 scale → 0-2 pts

        if d.get("recent_downgrade"):
            pts -= 2.0

        ebr = d.get("earnings_beat_rate")
        if ebr is not None:
            pts += ebr * 1.5  # 0-1 → 0-1.5 pts

        return ConvictionScorer._clamp(pts)

    @staticmethod
    def _score_macro(d: dict) -> float:
        """Score 0–10 from macro environment.  Lower VIX = higher score."""
        vix = d.get("vix", 20.0)
        if vix is None:
            vix = 20.0

        # VIX mapping: <15 → 10, 15-20 → 8, 20-25 → 6, 25-30 → 4, 30-35 → 2, >35 → 0
        if vix < 15:    score = 10.0
        elif vix < 20:  score = 8.0
        elif vix < 25:  score = 6.0
        elif vix < 30:  score = 4.0
        elif vix < 35:  score = 2.0
        else:           score = 0.0

        if d.get("yield_curve_inv"):
            score -= 2.0

        return ConvictionScorer._clamp(score)

    @staticmethod
    def _score_valuation(d: dict) -> float:
        """Score 0–10 from valuation metrics.  Low PE + low PEG = high score."""
        pts = 0.0
        n = 0

        pe = d.get("forward_pe")
        if pe is not None and pe > 0:
            n += 1
            if pe < 15:    pts += 10
            elif pe < 20:  pts += 8
            elif pe < 30:  pts += 5
            elif pe < 50:  pts += 3
            else:          pts += 1

        peg = d.get("peg_ratio")
        if peg is not None and peg > 0:
            n += 1
            if peg < 1.0:    pts += 10
            elif peg < 1.5:  pts += 7
            elif peg < 2.0:  pts += 5
            elif peg < 3.0:  pts += 3
            else:            pts += 1

        return ConvictionScorer._clamp(pts / max(n, 1)) if n > 0 else 5.0

    # ----- main entry -----

    @staticmethod
    def score(conviction_data: dict | None) -> dict:
        """
        Compute conviction score from pre-fetched data.
        Returns dict with conviction_score, conviction_label, factor_scores,
        macro_crisis, earnings_imminent.
        """
        if not conviction_data:
            return {
                "conviction_score":  5.0,
                "conviction_label":  "NEUTRAL",
                "factor_scores":     {},
                "macro_crisis":      False,
                "earnings_imminent": False,
            }

        fund_data = conviction_data.get("fundamental", {})
        tech_data = conviction_data.get("technical", {})
        sent_data = conviction_data.get("sentiment", {})
        macro_data = conviction_data.get("macro", {})
        val_data = conviction_data.get("valuation", {})

        factors = {
            "fundamental": ConvictionScorer._score_fundamental(fund_data),
            "technical":   ConvictionScorer._score_technical(tech_data),
            "sentiment":   ConvictionScorer._score_sentiment(sent_data),
            "macro":       ConvictionScorer._score_macro(macro_data),
            "valuation":   ConvictionScorer._score_valuation(val_data),
        }

        weighted = sum(
            factors[k] * _CONVICTION_WEIGHTS[k] for k in _CONVICTION_WEIGHTS
        )
        score = ConvictionScorer._clamp(weighted)

        # Detect macro crisis
        vix = macro_data.get("vix", 20.0) or 20.0
        news_score = sent_data.get("news_score")
        macro_crisis = vix > 35 or (news_score is not None and news_score < -0.5)

        # Earnings imminent flag
        earnings_days = conviction_data.get("earnings_days_away")
        earnings_imminent = earnings_days is not None and 0 <= earnings_days < 7

        # Override score in crisis
        if macro_crisis:
            score = min(score, 3.0)

        # Label
        if score >= 8:     label = "STRONG_BUY"
        elif score >= 6.5: label = "BUY"
        elif score >= 4.5: label = "NEUTRAL"
        elif score >= 3:   label = "WEAK"
        else:              label = "VERY_WEAK"

        return {
            "conviction_score":  round(score, 1),
            "conviction_label":  label,
            "factor_scores":     {k: round(v, 1) for k, v in factors.items()},
            "macro_crisis":      macro_crisis,
            "earnings_imminent": earnings_imminent,
        }


# ─── Profit ladder ────────────────────────────────────────────────────────────
# (r_level, label, sell_fraction_of_original)
PROFIT_LADDER: list[tuple[float, str, float]] = [
    (1.5, "TP1", 0.25),
    (3.0, "TP2", 0.25),
    (5.0, "TP3", 0.25),
    (7.0, "TP4", 0.25),
]
# Never sell more than 75 % cumulatively — keep a 25 % trailing position
LADDER_MAX_SELL_PCT: float = 75.0

# ─── Pyramid-add thresholds ───────────────────────────────────────────────────
PYRAMID_MIN_GAIN_PCT: float   = 5.0    # must be ≥ 5 % profitable
PYRAMID_MAX_GAIN_PCT: float   = 50.0   # don't chase above 50 %
PYRAMID_MIN_RSI: float        = 50.0   # momentum is healthy
PYRAMID_MAX_RSI: float        = 72.0   # not overbought
PYRAMID_MIN_ADX: float        = 22.0   # market is trending
PYRAMID_ADD_FRACTION: float   = 0.50   # add 50 % of current qty
PYRAMID_MAX_VIX: float        = 25.0   # only add in calm markets
PYRAMID_MAX_TP_R: float       = 3.0    # don't pyramid once at TP2+
PYRAMID_MAX_POS_PCT: float    = 0.20   # max 20 % of portfolio in one stock

# ─── Average-down thresholds ──────────────────────────────────────────────────
AVG_DOWN_MIN_LOSS_PCT: float         = 5.0    # at least 5 % down before considering
AVG_DOWN_MAX_LOSS_PCT: float         = 20.0   # never average deeper than 20 %
AVG_DOWN_ADD_FRACTION: float         = 0.50   # add 50 % of current qty
AVG_DOWN_MAX_VIX: float              = 28.0   # avoid adding in fear regimes
AVG_DOWN_MIN_BACKTEST_UNITS: int     = 2      # only avg-down high-conviction signals
AVG_DOWN_MAX_DECAY_COUNT: int        = 1      # 2+ decay flags → don't average down

# ─── Position quality ─────────────────────────────────────────────────────────
QUALITY_UNDERSIZED_GAIN_PCT: float   = 30.0   # flag UNDERSIZED above this gain
QUALITY_OVERSIZED_LOSS_PCT: float    = 15.0   # flag OVERSIZED below this loss


# ─── Main facade ──────────────────────────────────────────────────────────────

class PositionManager:
    """
    Pure-logic position management engine.

    Uses only the fields already present in a SellEngine result dict —
    no network calls, no imports of yfinance or moomoo-api.

    Usage
    -----
    >>> result = SellEngine.apply_exit_rules(raw_result, vix, now)
    >>> result = PositionManager.evaluate(result, portfolio_context)
    # result now contains profit_ladder, scaling_action, position_quality, etc.
    """

    # Expose constants for tests
    PROFIT_LADDER           = PROFIT_LADDER
    LADDER_MAX_SELL_PCT     = LADDER_MAX_SELL_PCT
    PYRAMID_MIN_GAIN_PCT    = PYRAMID_MIN_GAIN_PCT
    PYRAMID_MAX_GAIN_PCT    = PYRAMID_MAX_GAIN_PCT
    PYRAMID_MIN_RSI         = PYRAMID_MIN_RSI
    PYRAMID_MAX_RSI         = PYRAMID_MAX_RSI
    PYRAMID_MIN_ADX         = PYRAMID_MIN_ADX
    PYRAMID_ADD_FRACTION    = PYRAMID_ADD_FRACTION
    PYRAMID_MAX_VIX         = PYRAMID_MAX_VIX
    PYRAMID_MAX_TP_R        = PYRAMID_MAX_TP_R
    PYRAMID_MAX_POS_PCT     = PYRAMID_MAX_POS_PCT
    AVG_DOWN_MIN_LOSS_PCT   = AVG_DOWN_MIN_LOSS_PCT
    AVG_DOWN_MAX_LOSS_PCT   = AVG_DOWN_MAX_LOSS_PCT
    AVG_DOWN_ADD_FRACTION   = AVG_DOWN_ADD_FRACTION
    AVG_DOWN_MAX_VIX        = AVG_DOWN_MAX_VIX
    AVG_DOWN_MIN_BACKTEST_UNITS  = AVG_DOWN_MIN_BACKTEST_UNITS
    AVG_DOWN_MAX_DECAY_COUNT     = AVG_DOWN_MAX_DECAY_COUNT
    QUALITY_UNDERSIZED_GAIN_PCT  = QUALITY_UNDERSIZED_GAIN_PCT
    QUALITY_OVERSIZED_LOSS_PCT   = QUALITY_OVERSIZED_LOSS_PCT

    @staticmethod
    def evaluate(result: dict, portfolio_context: Optional[dict] = None,
                 conviction_data: Optional[dict] = None) -> dict:
        """
        Main entry point.  Extends the SellEngine result dict with:

        Conviction fields (new):
          conviction_score    float 0–10  — multi-factor conviction
          conviction_label    str         — STRONG_BUY / BUY / NEUTRAL / WEAK / VERY_WEAK
          factor_scores       dict        — per-factor breakdown
          macro_crisis        bool        — VIX>35 or severe negative news
          earnings_imminent   bool        — earnings announcement within 7 days

        Profit ladder fields:
          profit_ladder       list[dict]  — one entry per TP tier
          profit_action       str         — TAKE_PROFIT_MAJOR / TAKE_PROFIT_PARTIAL / HOLD_GAINS / ACCUMULATE
          profit_qty          int         — shares to sell right now (0 if none)
          profit_pct          int         — % of original position to sell (0 if none)
          profit_reason       str         — human-readable explanation

        Scaling fields:
          scaling_action      str         — PYRAMID_ADD / AVG_DOWN / HOLD_SIZE /
                                           PYRAMID_BLOCKED / AVG_DOWN_BLOCKED / AVOID_ADD
          scaling_qty         int         — shares to add (> 0) or 0 if no action
          scaling_reason      str         — explanation
          add_recommendation  dict|None   — full pyramid or avg-down details
          avg_down_recommendation dict|None

        Position quality fields:
          position_quality    str         — OPTIMAL / UNDERSIZED / OVERSIZED / UNKNOWN
          quality_reason      str         — explanation
          opportunity_cost    float|None  — estimated missed $ if UNDERSIZED
          optimal_units       int         — backtest or heuristic optimal unit count
        """
        ctx = portfolio_context or {}

        # ── Conviction scoring ─────────────────────────────────────────────
        cv = ConvictionScorer.score(conviction_data)
        result.update(cv)

        result = PositionManager._profit_ladder_eval(result)
        result = PositionManager._scaling_eval(result, ctx)
        result = PositionManager._quality_eval(result, ctx)
        return result

    # ─── Profit Ladder ────────────────────────────────────────────────────────

    @staticmethod
    def _profit_ladder_eval(result: dict) -> dict:
        shares    = int(result.get("shares", 0) or 0)
        current_r = result.get("current_r")           # float or None
        R_value   = float(result.get("R_value", 0) or 0)
        entry     = float(result.get("entry_price", 0) or 0)

        # ── Conviction-aware adjustments ──────────────────────────────────
        conv_score       = result.get("conviction_score", 5.0)
        macro_crisis     = result.get("macro_crisis", False)
        earnings_imminent = result.get("earnings_imminent", False)

        # In macro crisis → aggressive take-profit at any hit tier
        # High conviction (≥7) → reduce sell fractions (hold winners longer)
        # Low conviction (<4) → increase sell fractions (take profits early)
        if macro_crisis:
            sell_frac_mult = 1.5  # 50 % more aggressive
        elif earnings_imminent:
            sell_frac_mult = 1.3  # de-risk before earnings
        elif conv_score >= 8:
            sell_frac_mult = 0.0  # DON'T sell — accumulate instead
        elif conv_score >= 7:
            sell_frac_mult = 0.5  # halve the sell fraction — strong stock
        elif conv_score >= 5:
            sell_frac_mult = 1.0  # standard
        elif conv_score >= 3:
            sell_frac_mult = 1.3  # slightly more aggressive
        else:
            sell_frac_mult = 1.5  # very weak — take profits aggressively

        ladder: list[dict] = []
        for (r_level, label, sell_frac) in PROFIT_LADDER:
            hit          = current_r is not None and current_r >= r_level
            target_price = round(entry + r_level * R_value, 4) if R_value > 0 else None
            adj_frac     = min(sell_frac * sell_frac_mult, 0.50)  # cap single tier at 50%
            qty_at_tier  = max(1, round(shares * adj_frac)) if hit and shares > 0 and adj_frac > 0 else 0
            ladder.append({
                "r_level":      r_level,
                "label":        label,
                "sell_pct":     int(adj_frac * 100),
                "target_price": target_price,
                "hit":          hit,
                "qty_at_tier":  qty_at_tier,
                "status":       "HIT" if hit else "PENDING",
            })

        # Cumulative profit-taking: sum all hit tiers, cap at 75 %
        hit_tiers         = [t for t in ladder if t["hit"]]
        cum_pct           = min(LADDER_MAX_SELL_PCT, sum(t["sell_pct"] for t in hit_tiers))
        cum_qty           = max(0, round(shares * cum_pct / 100)) if shares > 0 else 0

        # Always keep at least 1 share as trailing position
        if shares > 0 and cum_qty >= shares:
            cum_qty = shares - 1

        # ── Conviction-aware action labelling ─────────────────────────────
        conv_label = result.get("conviction_label", "NEUTRAL")

        if macro_crisis and hit_tiers:
            # Macro crisis override — sell aggressively with re-entry note
            highest       = hit_tiers[-1]["label"]
            profit_r_str  = f"{current_r:.1f}R" if current_r is not None else "N/A"
            profit_action = "TAKE_PROFIT_MAJOR"
            profit_reason = (
                f"⚠️ MACRO CRISIS — {highest} hit ({profit_r_str}). "
                f"Sell {cum_pct}% ({cum_qty} share(s)). "
                f"Re-entry when VIX < 25 and fundamentals intact."
            )
        elif conv_score >= 8 and hit_tiers:
            # Very high conviction — DON'T take profit, recommend accumulate
            profit_action = "ACCUMULATE"
            cum_qty       = 0
            cum_pct       = 0
            profit_r_str  = f"{current_r:.1f}R" if current_r is not None else "N/A"
            profit_reason = (
                f"🟢 STRONG conviction ({conv_score}/10, {conv_label}). "
                f"Profit at {profit_r_str} but fundamentals excellent — "
                f"HOLD and accumulate on pullbacks. No sell."
            )
        elif hit_tiers:
            highest       = hit_tiers[-1]["label"]
            profit_r_str  = f"{current_r:.1f}R" if current_r is not None else "N/A"
            remaining_qty = shares - cum_qty
            trailing_note = f"Trail {remaining_qty} share(s) with ATR stop." if remaining_qty > 0 else ""
            conv_note     = f" Conviction: {conv_score}/10 ({conv_label})."
            profit_reason = (
                f"{highest} reached ({profit_r_str}). "
                f"Sell {cum_pct}% ({cum_qty} share(s)). {trailing_note}{conv_note}"
            ).strip()
            profit_action = "TAKE_PROFIT_MAJOR" if cum_pct >= 50 else "TAKE_PROFIT_PARTIAL"
        else:
            profit_action   = "HOLD_GAINS"
            cum_pct         = 0
            cum_qty         = 0
            if current_r is not None and R_value > 0:
                profit_reason = (
                    f"No tier hit yet (current {current_r:.1f}R). "
                    f"TP1 (@{PROFIT_LADDER[0][0]:.1f}R) "
                    f"target: {round(entry + PROFIT_LADDER[0][0] * R_value, 4)}. "
                    f"Conviction: {conv_score}/10 ({conv_label})."
                )
            else:
                profit_reason = "Profit levels not calculable (ATR data unavailable)."

        result.update({
            "profit_ladder":  ladder,
            "profit_action":  profit_action,
            "profit_qty":     cum_qty,
            "profit_pct":     cum_pct,
            "profit_reason":  profit_reason,
        })
        return result

    # ─── Scaling Evaluation ───────────────────────────────────────────────────

    @staticmethod
    def _scaling_eval(result: dict, ctx: dict) -> dict:
        shares        = int(result.get("shares", 0) or 0)
        gain_pct      = float(result.get("current_gain_pct", 0) or 0)
        current_r     = result.get("current_r")                     # float or None
        rsi           = result.get("rsi")                           # float or None
        adx           = result.get("adx")                           # float or None
        vix           = float(result.get("vix", 20.0) or 20.0)
        decay_count   = int(result.get("decay_count", 0) or 0)
        entry_price   = float(result.get("entry_price", 0) or 0)
        current_p     = result.get("current_price")                 # float or None
        action        = result.get("action", "HOLD")                # from sell_engine
        backtest_data = result.get("backtest_data")                 # dict or None

        # Conviction-aware fields
        conv_score        = result.get("conviction_score", 5.0)
        macro_crisis      = result.get("macro_crisis", False)
        earnings_imminent = result.get("earnings_imminent", False)

        # Portfolio context (optional; safe defaults if not provided)
        available_cash    = float(ctx.get("available_cash", 0) or 0)
        portfolio_value   = float(ctx.get("total_portfolio_value", 0) or 0)
        max_pos_pct       = float(ctx.get("max_position_pct", PYRAMID_MAX_POS_PCT))
        already_avg_down  = bool(ctx.get("already_averaged_down", False))

        add_price         = current_p if current_p and current_p > 0 else entry_price
        current_pos_value = add_price * shares if add_price else 0

        # ── Macro crisis → block ALL adds ─────────────────────────────────────
        if macro_crisis:
            result.update({
                "scaling_action":          "AVOID_ADD",
                "scaling_qty":             0,
                "scaling_reason":          (
                    f"⚠️ MACRO CRISIS detected (VIX={vix:.1f}, conviction={conv_score}/10). "
                    f"No new capital deployment until crisis abates."
                ),
                "add_recommendation":      None,
                "avg_down_recommendation": None,
            })
            return result

        # ── Earnings imminent → block adds ────────────────────────────────────
        if earnings_imminent:
            result.update({
                "scaling_action":          "AVOID_ADD",
                "scaling_qty":             0,
                "scaling_reason":          (
                    "Earnings within 7 days — no scaling until post-earnings clarity."
                ),
                "add_recommendation":      None,
                "avg_down_recommendation": None,
            })
            return result

        # ── Sell engine triggered a hard exit → never add ─────────────────────
        # NOTE: "TRAIL" is intentionally NOT included here — it is the normal
        # post-TP1/TP2 profit-taking state (see sell_engine.apply_exit_rules),
        # not an exit alarm, and pyramid-add must still be evaluated for it
        # (subject to its own RSI/ADX/decay/gain-range guards below). "REDUCE"
        # is already independently blocked from avg-down further down
        # ("Exit engine flagged REDUCE — do NOT average down.") and is blocked
        # from pyramid-add via `decay_ok = decay_count == 0` (REDUCE requires
        # decay_count >= 2). Confirmed via full test_position_manager.py run
        # (2026-07-06) after an earlier attempt to add TRAIL/REDUCE here broke
        # 6 passing tests that rely on TRAIL remaining pyramid-eligible.
        if action == "SELL":
            result.update({
                "scaling_action":          "AVOID_ADD",
                "scaling_qty":             0,
                "scaling_reason":          "Position flagged SELL by exit engine — no scaling.",
                "add_recommendation":      None,
                "avg_down_recommendation": None,
            })
            return result

        # ── Conviction-adjusted pyramid fraction ──────────────────────────────
        # High conviction → add more aggressively; low conviction → add less
        if conv_score >= 8:
            pyramid_frac = 0.75  # very high conviction: add 75%
        elif conv_score >= 7:
            pyramid_frac = 0.60  # high conviction: add 60%
        elif conv_score >= 5:
            pyramid_frac = PYRAMID_ADD_FRACTION  # standard 50%
        elif conv_score >= 4:
            pyramid_frac = 0.25  # weak conviction: add only 25%
        else:
            pyramid_frac = 0.0  # very weak: don't pyramid at all

        if gain_pct > 0:
            # ── Pyramid add (add to winners) ──────────────────────────────────
            rsi_ok      = rsi is None or (PYRAMID_MIN_RSI <= rsi <= PYRAMID_MAX_RSI)
            adx_ok      = adx is None or adx >= PYRAMID_MIN_ADX
            tp_ok       = current_r is None or current_r < PYRAMID_MAX_TP_R
            vix_ok      = vix < PYRAMID_MAX_VIX
            gain_range  = PYRAMID_MIN_GAIN_PCT <= gain_pct <= PYRAMID_MAX_GAIN_PCT
            decay_ok    = decay_count == 0

            pyramid_eligible = rsi_ok and adx_ok and tp_ok and vix_ok and gain_range and decay_ok
            # Block pyramid entirely for very weak conviction
            if pyramid_frac <= 0:
                pyramid_eligible = False

            if pyramid_eligible:
                desired_add = max(1, round(shares * pyramid_frac))
                # Cap by portfolio concentration
                if portfolio_value > 0 and add_price and add_price > 0:
                    max_extra_value  = max(0.0, portfolio_value * max_pos_pct - current_pos_value)
                    max_by_conc      = int(max_extra_value / add_price)
                else:
                    max_by_conc      = desired_add  # no portfolio data → use desired

                # Cap by available cash
                if available_cash > 0 and add_price and add_price > 0:
                    max_by_cash = int(available_cash / add_price)
                else:
                    max_by_cash = desired_add  # no cash data → use desired

                add_qty = min(desired_add, max_by_conc, max_by_cash)
                add_qty = max(0, add_qty)

                if add_qty > 0:
                    new_avg = round(
                        (entry_price * shares + add_price * add_qty) / (shares + add_qty), 4
                    )
                    rsi_str = f"RSI {rsi:.0f}" if rsi is not None else "RSI N/A"
                    adx_str = f"ADX {adx:.0f}" if adx is not None else "ADX N/A"
                    conv_str = f"Conviction {conv_score}/10"
                    scaling_reason = (
                        f"Pyramid add: +{gain_pct:.1f}% gain, {rsi_str}, {adx_str}, {conv_str}. "
                        f"Add {add_qty} share(s) @ {add_price:.2f} "
                        f"(frac={pyramid_frac:.0%}). "
                        f"New avg cost: {new_avg:.2f}."
                    )
                    add_rec = {
                        "action":           "PYRAMID_ADD",
                        "suggested_qty":    add_qty,
                        "add_price":        round(add_price, 4),
                        "capital_required": round(add_qty * add_price, 2),
                        "new_avg_cost":     new_avg,
                        "reason":           scaling_reason,
                    }
                    result.update({
                        "scaling_action":          "PYRAMID_ADD",
                        "scaling_qty":             add_qty,
                        "scaling_reason":          scaling_reason,
                        "add_recommendation":      add_rec,
                        "avg_down_recommendation": None,
                    })
                    return result
                else:
                    # Conditions passed but blocked by capital/concentration
                    block_reason = []
                    if portfolio_value > 0 and max_by_conc == 0:
                        block_reason.append("concentration limit reached")
                    if available_cash > 0 and max_by_cash == 0:
                        block_reason.append("insufficient capital")
                    result.update({
                        "scaling_action":          "PYRAMID_BLOCKED",
                        "scaling_qty":             0,
                        "scaling_reason":          (
                            "Pyramid conditions met but blocked: "
                            + (", ".join(block_reason) or "qty rounds to 0")
                            + "."
                        ),
                        "add_recommendation":      None,
                        "avg_down_recommendation": None,
                    })
                    return result
            else:
                # Pyramid ineligible — explain why
                reasons = []
                if not gain_range:
                    if gain_pct < PYRAMID_MIN_GAIN_PCT:
                        reasons.append(f"gain too small ({gain_pct:.1f}%)")
                    else:
                        reasons.append(f"gain too large to add ({gain_pct:.1f}%)")
                if not rsi_ok and rsi is not None:
                    if rsi > PYRAMID_MAX_RSI:
                        reasons.append(f"RSI overbought ({rsi:.0f})")
                    else:
                        reasons.append(f"RSI weak ({rsi:.0f})")
                if not adx_ok and adx is not None:
                    reasons.append(f"ADX weak ({adx:.0f})")
                if not vix_ok:
                    reasons.append(f"VIX elevated ({vix:.1f})")
                if not tp_ok and current_r is not None:
                    reasons.append(f"past TP2 ({current_r:.1f}R) — protect gains instead")
                if not decay_ok:
                    reasons.append(f"{decay_count} decay flags — wait for momentum recovery")
                if pyramid_frac <= 0:
                    reasons.append(f"conviction too low ({conv_score}/10) — don't add to weak positions")
                result.update({
                    "scaling_action":          "HOLD_SIZE",
                    "scaling_qty":             0,
                    "scaling_reason":          (
                        "No pyramid: "
                        + ("; ".join(reasons) if reasons else "conditions not fully met")
                        + "."
                    ),
                    "add_recommendation":      None,
                    "avg_down_recommendation": None,
                })
                return result

        else:
            # gain_pct <= 0 → evaluate average-down
            loss_pct = abs(gain_pct)

            # Hard disqualifiers
            if already_avg_down:
                result.update({
                    "scaling_action":          "AVOID_ADD",
                    "scaling_qty":             0,
                    "scaling_reason":          "Already averaged down once — discipline: no second avg-down.",
                    "add_recommendation":      None,
                    "avg_down_recommendation": None,
                })
                return result

            if action == "REDUCE":
                result.update({
                    "scaling_action":          "AVOID_ADD",
                    "scaling_qty":             0,
                    "scaling_reason":          "Exit engine flagged REDUCE — do NOT average down.",
                    "add_recommendation":      None,
                    "avg_down_recommendation": None,
                })
                return result

            if loss_pct > AVG_DOWN_MAX_LOSS_PCT:
                result.update({
                    "scaling_action":          "AVOID_ADD",
                    "scaling_qty":             0,
                    "scaling_reason":          (
                        f"Loss too deep ({loss_pct:.1f}%) — never avg-down beyond "
                        f"{AVG_DOWN_MAX_LOSS_PCT:.0f}%."
                    ),
                    "add_recommendation":      None,
                    "avg_down_recommendation": None,
                })
                return result

            if loss_pct < AVG_DOWN_MIN_LOSS_PCT:
                # Not yet at the entry step — just hold
                result.update({
                    "scaling_action":          "HOLD_SIZE",
                    "scaling_qty":             0,
                    "scaling_reason":          f"Loss only {loss_pct:.1f}% — within normal noise. Hold.",
                    "add_recommendation":      None,
                    "avg_down_recommendation": None,
                })
                return result

            # Check remaining conditions
            backtest_units = (
                backtest_data.get("size_units", 1) if isinstance(backtest_data, dict) else 1
            )
            conviction_ok   = backtest_units >= AVG_DOWN_MIN_BACKTEST_UNITS
            vix_ok          = vix <= AVG_DOWN_MAX_VIX
            decay_ok        = decay_count <= AVG_DOWN_MAX_DECAY_COUNT

            # Conviction-aware avg-down: require conviction ≥ 5 to avg-down
            conv_ok_for_avg  = conv_score >= 5.0

            # Adjust avg-down fraction by conviction
            if conv_score >= 7:
                avg_frac = 0.75   # high conviction = aggressive avg-down ("buy the dip")
            elif conv_score >= 5:
                avg_frac = AVG_DOWN_ADD_FRACTION  # standard 50%
            else:
                avg_frac = 0.0    # don't avg-down weak fundamentals

            if conviction_ok and vix_ok and decay_ok and conv_ok_for_avg and avg_frac > 0:
                desired_add = max(1, round(shares * avg_frac))
                if available_cash > 0 and add_price and add_price > 0:
                    max_by_cash = int(available_cash / add_price)
                else:
                    max_by_cash = desired_add

                avg_down_qty = min(desired_add, max_by_cash)
                avg_down_qty = max(0, avg_down_qty)

                if avg_down_qty > 0:
                    new_avg = round(
                        (entry_price * shares + add_price * avg_down_qty) / (shares + avg_down_qty), 4
                    )
                    scaling_reason = (
                        f"Avg-down candidate: -{loss_pct:.1f}% loss, "
                        f"backtest conviction={backtest_units}u, VIX={vix:.1f}, "
                        f"multi-factor conviction={conv_score}/10. "
                        f"Add {avg_down_qty} share(s) @ {add_price:.2f} "
                        f"(frac={avg_frac:.0%}). "
                        f"New avg cost: {new_avg:.2f}. "
                        f"CAUTION: only do this ONCE."
                    )
                    avg_rec = {
                        "action":           "AVG_DOWN",
                        "suggested_qty":    avg_down_qty,
                        "add_price":        round(add_price, 4),
                        "capital_required": round(avg_down_qty * add_price, 2),
                        "new_avg_cost":     new_avg,
                        "reason":           scaling_reason,
                    }
                    result.update({
                        "scaling_action":          "AVG_DOWN",
                        "scaling_qty":             avg_down_qty,
                        "scaling_reason":          scaling_reason,
                        "add_recommendation":      None,
                        "avg_down_recommendation": avg_rec,
                    })
                    return result
                else:
                    result.update({
                        "scaling_action":          "AVG_DOWN_BLOCKED",
                        "scaling_qty":             0,
                        "scaling_reason":          (
                            f"Avg-down conditions met but blocked: insufficient capital "
                            f"(need {add_price:.2f}/share, available {available_cash:.2f})."
                        ),
                        "add_recommendation":      None,
                        "avg_down_recommendation": None,
                    })
                    return result
            else:
                disqualifiers = []
                if not conviction_ok:
                    disqualifiers.append(
                        f"low conviction (backtest={backtest_units}u, need ≥{AVG_DOWN_MIN_BACKTEST_UNITS})"
                    )
                if not vix_ok:
                    disqualifiers.append(f"VIX={vix:.1f} > {AVG_DOWN_MAX_VIX} (fear regime)")
                if not decay_ok:
                    disqualifiers.append(f"{decay_count} signal decay flags")
                if not conv_ok_for_avg:
                    disqualifiers.append(
                        f"multi-factor conviction too low ({conv_score}/10, need ≥5)"
                    )
                result.update({
                    "scaling_action":          "AVOID_ADD",
                    "scaling_qty":             0,
                    "scaling_reason":          (
                        f"No avg-down: {'; '.join(disqualifiers)}."
                    ),
                    "add_recommendation":      None,
                    "avg_down_recommendation": None,
                })
                return result

        # Fallback (gain_pct == 0 exactly)
        result.update({
            "scaling_action":          "HOLD_SIZE",
            "scaling_qty":             0,
            "scaling_reason":          "Position at breakeven — hold, no scaling action.",
            "add_recommendation":      None,
            "avg_down_recommendation": None,
        })
        return result

    # ─── Position Quality ─────────────────────────────────────────────────────

    @staticmethod
    def _quality_eval(result: dict, ctx: dict) -> dict:
        shares        = int(result.get("shares", 0) or 0)
        gain_pct      = float(result.get("current_gain_pct", 0) or 0)
        current_p     = result.get("current_price")
        entry_price   = float(result.get("entry_price", 0) or 0)
        backtest_data = result.get("backtest_data")   # dict from backtest_sizer, or None

        # Determine optimal unit count
        optimal_units: int
        _is_heuristic_sizing = not (isinstance(backtest_data, dict) and "size_units" in backtest_data)
        if not _is_heuristic_sizing:
            optimal_units = int(backtest_data["size_units"] or 1)
        else:
            # Heuristic fallback based on final gain -- this is a RETROSPECTIVE,
            # hindsight-based guess ("this position did well, so in hindsight a
            # bigger size would have captured more"), not a signal-quality or
            # backtest-derived recommendation. Labelled explicitly below so the
            # HTML never implies backtest validation that doesn't exist
            # (2026-07-06 fix -- see sell-strategy critique finding B3).
            if gain_pct > 50:
                optimal_units = 3
            elif gain_pct > 20:
                optimal_units = 2
            else:
                optimal_units = 1
        _sizing_basis = ("backtest-derived" if not _is_heuristic_sizing
                          else "retrospective gain-based heuristic, NOT backtest-validated")

        # We define "1 unit" = current share count.
        # If backtest said 3 units and you only bought 1 unit, you're undersized.
        # Opportunity cost = (optimal_units - 1) × share_count × price_gain
        price_gain = ((current_p or entry_price) - entry_price)

        # Context from earlier evaluation steps (profit ladder + scaling run before quality)
        scaling_action = result.get("scaling_action", "")
        profit_action  = result.get("profit_action", "HOLD_GAINS")

        # Pyramid-add window: gain must be between MIN and MAX thresholds
        _pyramid_window_open = (PYRAMID_MIN_GAIN_PCT <= gain_pct <= PYRAMID_MAX_GAIN_PCT)
        # In profit-taking mode: priority is selling, not adding
        _taking_profit = profit_action in ("TAKE_PROFIT_MAJOR", "TAKE_PROFIT_PARTIAL")

        if gain_pct >= QUALITY_UNDERSIZED_GAIN_PCT and optimal_units >= 2:
            quality = "UNDERSIZED"
            missed_shares = shares * (optimal_units - 1)
            opp_cost = round(price_gain * missed_shares, 2) if price_gain > 0 else 0.0

            if not _pyramid_window_open or _taking_profit:
                # Pyramid window closed OR in profit-taking mode:
                # Show educational/retrospective note ONLY — do NOT suggest adding now.
                _window_reason = (
                    f"gain {gain_pct:.1f}% > {int(PYRAMID_MAX_GAIN_PCT)}% max add threshold"
                    if gain_pct > PYRAMID_MAX_GAIN_PCT
                    else f"gain {gain_pct:.1f}% < {int(PYRAMID_MIN_GAIN_PCT)}% min add threshold"
                )
                _prefix = "Profit-taking mode active. " if _taking_profit else ""
                quality_reason = (
                    f"\U0001f4ca Sizing note: 1 unit held vs {optimal_units}\u00d7 optimal "
                    f"({_sizing_basis}). "
                    f"Missed gain est.: {'+' if opp_cost >= 0 else ''}{opp_cost:.2f}. "
                    f"{_prefix}Add window closed ({_window_reason}). "
                    f"For your NEXT entry in this setup, start with {optimal_units}\u00d7 size "
                    f"({shares * optimal_units:,.0f} shares). "
                    f"Live add signals appear in Scaling Decision \u2191 above."
                )
            else:
                # Pyramid window IS open and not in sell mode — live suggestion
                quality_reason = (
                    f"Up {gain_pct:.1f}% but holding only 1 unit. "
                    f"Optimal was {optimal_units}\u00d7 ({_sizing_basis}). "
                    f"Estimated missed gain: {'+' if opp_cost >= 0 else ''}{opp_cost:.2f} "
                    f"(if {optimal_units}\u00d7 size at entry). "
                    f"\U0001f449 Scaling Decision \u2191 shows live add signal. "
                    f"Target size: {shares * optimal_units:,.0f} shares at {optimal_units}\u00d7."
                )
        elif gain_pct <= -QUALITY_OVERSIZED_LOSS_PCT:
            quality = "OVERSIZED"
            opp_cost = None
            quality_reason = (
                f"Down {abs(gain_pct):.1f}% with current sizing. "
                f"Consider reducing position size to limit further drawdown."
            )
        else:
            quality = "OPTIMAL"
            opp_cost = None
            quality_reason = "Position sizing appears appropriate for current P&L."

        result.update({
            "position_quality": quality,
            "quality_reason":   quality_reason,
            "opportunity_cost": opp_cost,
            "optimal_units":    optimal_units,
        })
        return result


# ─── HTML rendering helpers (used by sell_strategy.py) ───────────────────────

def render_profit_ladder_html(result: dict) -> str:
    """Render a compact profit ladder table for the sell_strategy HTML card."""
    ladder = result.get("profit_ladder", [])
    if not ladder:
        return ""

    rows = ""
    for t in ladder:
        status_col = "#1db954" if t["hit"] else "#555"
        status_icon = "&#10003;" if t["hit"] else "&#9679;"
        target_str = f"${t['target_price']:.2f}" if t["target_price"] else "N/A"
        qty_str = f"sell {t['qty_at_tier']}" if t["hit"] and t["qty_at_tier"] > 0 else "—"
        rows += (
            f"<tr>"
            f"<td style='color:{status_col};padding:3px 8px 3px 0;font-weight:700'>"
            f"{status_icon} {t['label']}</td>"
            f"<td style='color:#aaa;padding:3px 8px'>@ {t['r_level']:.1f}R ({t['sell_pct']}%)</td>"
            f"<td style='color:#ccc;padding:3px 8px;font-family:monospace'>{target_str}</td>"
            f"<td style='color:{status_col};padding:3px 0;font-style:italic'>{qty_str}</td>"
            f"</tr>"
        )

    profit_action  = result.get("profit_action", "HOLD_GAINS")
    profit_qty     = result.get("profit_qty", 0)
    profit_pct     = result.get("profit_pct", 0)
    profit_reason  = result.get("profit_reason", "")
    action_colors  = {
        "TAKE_PROFIT_MAJOR":   "#f9c74f",
        "TAKE_PROFIT_PARTIAL": "#06d6a0",
        "HOLD_GAINS":          "#3a86ff",
    }
    action_col = action_colors.get(profit_action, "#888")
    action_label = {
        "TAKE_PROFIT_MAJOR":   "TAKE PROFIT (MAJOR)",
        "TAKE_PROFIT_PARTIAL": "TAKE PROFIT (PARTIAL)",
        "HOLD_GAINS":          "HOLD — NO TIER HIT YET",
    }.get(profit_action, profit_action)

    summary = (
        f"<div style='margin-bottom:6px'>"
        f"<span style='background:{action_col};color:#0f0f1a;font-weight:700;"
        f"padding:3px 12px;border-radius:6px;font-size:0.8rem'>{action_label}</span>"
        + (
            f"<span style='margin-left:12px;color:{action_col};font-weight:600'>"
            f"Sell {profit_qty} share(s) ({profit_pct}%)</span>"
            if profit_qty > 0 else ""
        )
        + f"</div>"
        f"<div style='color:#999;font-size:0.78rem;margin-bottom:8px'>{profit_reason}</div>"
    )

    return (
        f"<details style='margin-top:10px'>"
        f"<summary style='color:#06d6a0;cursor:pointer;font-size:0.82rem;font-weight:600'>"
        f"Profit Ladder</summary>"
        f"{summary}"
        f"<table style='border-collapse:collapse;font-size:0.82rem;margin-top:4px'>"
        f"{rows}"
        f"</table>"
        f"</details>"
    )


def render_scaling_html(result: dict) -> str:
    """Render scaling recommendation section for the sell_strategy HTML card."""
    scaling_action  = result.get("scaling_action", "HOLD_SIZE")
    scaling_reason  = result.get("scaling_reason", "")
    add_rec         = result.get("add_recommendation")
    avg_down_rec    = result.get("avg_down_recommendation")

    colors = {
        "PYRAMID_ADD":       "#4ade80",
        "AVG_DOWN":          "#f59e0b",
        "PYRAMID_BLOCKED":   "#64748b",
        "AVG_DOWN_BLOCKED":  "#64748b",
        "HOLD_SIZE":         "#3a86ff",
        "AVOID_ADD":         "#ef4444",
    }
    col = colors.get(scaling_action, "#888")

    badge = (
        f"<span style='background:{col};color:#0f0f1a;font-weight:700;"
        f"padding:3px 12px;border-radius:6px;font-size:0.8rem'>{scaling_action}</span>"
    )

    detail_rows = ""
    rec = add_rec or avg_down_rec
    if rec:
        detail_rows = (
            f"<table style='border-collapse:collapse;font-size:0.8rem;margin-top:8px'>"
            f"<tr><td style='color:#888;padding:2px 10px 2px 0'>Suggested qty</td>"
            f"<td style='color:#eee'>{rec.get('suggested_qty', '?')}</td></tr>"
            f"<tr><td style='color:#888;padding:2px 10px 2px 0'>Add price</td>"
            f"<td style='color:#eee'>${rec.get('add_price', 0):.2f}</td></tr>"
            f"<tr><td style='color:#888;padding:2px 10px 2px 0'>Capital needed</td>"
            f"<td style='color:#eee'>${rec.get('capital_required', 0):.2f}</td></tr>"
            f"<tr><td style='color:#888;padding:2px 10px 2px 0'>New avg cost</td>"
            f"<td style='color:#eee'>${rec.get('new_avg_cost', 0):.2f}</td></tr>"
            f"</table>"
        )

    # Auto-open the details when there is an actionable signal (PYRAMID_ADD or AVG_DOWN)
    _open_attr = " open" if scaling_action in ("PYRAMID_ADD", "AVG_DOWN") else ""

    return (
        f"<details style='margin-top:10px'{_open_attr}>"
        f"<summary style='color:#a78bfa;cursor:pointer;font-size:0.82rem;font-weight:600'>"
        f"Scaling Decision {chr(8593) if scaling_action in ('PYRAMID_ADD','AVG_DOWN') else ''}</summary>"
        f"<div style='margin-top:6px'>{badge}"
        f"<span style='margin-left:10px;color:#999;font-size:0.78rem'>{scaling_reason}</span>"
        f"</div>"
        f"{detail_rows}"
        f"</details>"
    )


def render_quality_html(result: dict) -> str:
    """Render position quality section for the sell_strategy HTML card."""
    quality       = result.get("position_quality", "UNKNOWN")
    quality_reason = result.get("quality_reason", "")
    opp_cost      = result.get("opportunity_cost")
    optimal_units = result.get("optimal_units", 1)

    colors = {
        "OPTIMAL":    "#1db954",
        "UNDERSIZED": "#f9c74f",
        "OVERSIZED":  "#e63946",
        "UNKNOWN":    "#888",
    }
    col = colors.get(quality, "#888")

    opp_cost_html = ""
    if opp_cost is not None and opp_cost > 0:
        opp_cost_html = (
            f"<div style='margin-top:4px;color:#f9c74f;font-size:0.78rem'>"
            f"Est. missed gain: <strong>{'+' if opp_cost >= 0 else ''}{opp_cost:.2f}</strong> "
            f"(if held {optimal_units}x size)</div>"
        )

    return (
        f"<details style='margin-top:10px'>"
        f"<summary style='color:#f9c74f;cursor:pointer;font-size:0.82rem;font-weight:600'>"
        f"Position Quality</summary>"
        f"<div style='margin-top:6px'>"
        f"<span style='background:{col};color:#0f0f1a;font-weight:700;"
        f"padding:3px 12px;border-radius:6px;font-size:0.8rem'>{quality}</span>"
        f"<span style='margin-left:10px;color:#999;font-size:0.78rem'>{quality_reason}</span>"
        f"</div>"
        f"{opp_cost_html}"
        f"</details>"
    )


def render_position_mgmt_block(result: dict) -> str:
    """
    Render the full position management block for a sell_strategy HTML card.
    Returns empty string if nothing actionable.
    """
    profit_block  = render_profit_ladder_html(result)
    scaling_block = render_scaling_html(result)
    quality_block = render_quality_html(result)

    if not (profit_block or scaling_block or quality_block):
        return ""

    return (
        f"<div style='margin-top:14px;border-top:1px solid #2d2d4e;padding-top:10px'>"
        f"<div style='color:#888;font-size:0.72rem;text-transform:uppercase;"
        f"letter-spacing:1px;margin-bottom:8px'>Position Management</div>"
        f"{profit_block}"
        f"{scaling_block}"
        f"{quality_block}"
        f"</div>"
    )
