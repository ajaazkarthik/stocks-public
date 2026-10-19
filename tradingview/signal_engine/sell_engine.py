"""
SellEngine — Pure-logic position exit decision engine.

All computation is pure (no network calls, no yfinance, no TV screener I/O).
Network-dependent orchestration lives in sell/sell_strategy.py which imports
these helpers and delegates to them.

Rules implemented in apply_exit_rules():
  1. Hard Stop        — entry − 2×ATR_at_entry  (floor: entry × 0.92)
  2. Break-Even Stop  — once HWM ≥ entry + 1×ATR, stop moves to entry
  3. Trailing Stop    — HWM − 1.5×ATR_current (activates always, wins if above floor)
  4. R-Multiple TPs   — TP1 @+1.5R | TP2 @+3.0R
  5. Time Stop        — ≥20 trading days with < +3% gain → EXIT
  6. Signal Decay     — RSI<45, ADX<20, RelVol<0.5 → 2+ flags = REDUCE
  7. VIX Regime       — ≥35 force exit losers; ≥25 tighten to 1×ATR trail
  8. Fundamental Drift— PE>60 or ROE<5% → REVIEW trigger
"""

from __future__ import annotations

import math
from datetime import datetime

try:
    import numpy as np
    import pandas as pd
except ImportError:
    raise ImportError("pip install numpy pandas")

# ─── Constants (also exposed as class attributes on SellEngine) ───────────────
ATR_PERIOD      = 14
HARD_STOP_MULT  = 2.0
HARD_STOP_FLOOR = 0.08        # minimum % loss before hard stop can trigger
BREAKEVEN_MULT  = 1.0
TRAIL_MULT      = 1.5
TP1_R           = 1.5
TP2_R           = 3.0
TIME_STOP_DAYS  = 20
TIME_STOP_GAIN  = 0.03
MIN_HOLD_DAYS              = 14    # freshly bought positions below this threshold get REVIEW instead of SELL
MOMENTUM_STRONG_THRESHOLD  = 0.05  # +5%+ over 5d = momentum strong override
MOMENTUM_MODERATE_THRESHOLD = 0.03 # +3–5% over 5d = momentum moderate override
CATASTROPHIC_LOSS_MULT     = 1.25  # losses > MULT×HARD_STOP_FLOOR (>10%) bypass MIN_HOLD — beyond 1.25× stop floor = thesis failure
DECAY_FACTOR_COUNT         = 5     # RSI + ADX + RelVol + Darvas breakdown + VPA trap-up (see decay_count below)

# ── Conviction-aware exit guards ──────────────────────────────────────────────
# CGT Proximity Guard: if within this many days of the 12-month CGT discount threshold
# AND the position is profitable, downgrade a stop-triggered SELL → REVIEW so that
# the trader can make an informed tax-aware decision rather than auto-exiting.
CGT_PROXIMITY_DAYS     = 60    # within 60 calendar days of 12-month CGT discount window
CGT_MIN_PROFIT_PCT     = 0.10  # only protect positions > 10% in profit

# Quality Stop Guard: a mechanical trailing-stop hit on a fundamentally sound, profitable
# position is often a pullback rather than a thesis failure.  Instead of SELL, issue
# REVIEW so the trader can assess whether the business case has actually changed.
QUALITY_MAX_PE         = 40    # PE ≤ 40 → "not overvalued" for quality classification
QUALITY_MIN_ROE        = 10    # ROE ≥ 10% → healthy-quality classification
QUALITY_MIN_PROFIT_PCT = 0.20  # require > 20% embedded gain before softening the exit

EXCHANGE_MAP: dict[str, str] = {
    "ASX": ".AX", "ASX_DLY": ".AX",
    "NSE": ".NS", "BSE": ".BO",
    "SGX": ".SI", "HKEX": ".HK",
    "TYO": ".T",                     # Tokyo Stock Exchange (e.g. 2865.T)
    "KRX": ".KS",                    # Korea Stock Exchange (e.g. 005930.KS)
    "TSX": ".TO",                    # Toronto Stock Exchange (e.g. SHOP.TO)
    "LON": ".L",                     # London Stock Exchange (e.g. BARC.L)
    "NYSE": "", "NASDAQ": "", "AMEX": "", "BATS": "",
}


# ─── Pure helpers ─────────────────────────────────────────────────────────────

def tv_to_yf(pro_symbol: str) -> str:
    """Convert TradingView pro_symbol (e.g. 'ASX:BHP') to yfinance ticker ('BHP.AX')."""
    if ":" not in pro_symbol:
        return pro_symbol
    exchange, ticker = pro_symbol.split(":", 1)
    ticker = ticker.replace("-", ".").strip()
    suffix = EXCHANGE_MAP.get(exchange.upper(), "")
    if exchange.upper() == "HKEX" and ticker.isdigit():
        ticker = ticker.zfill(4)
    return ticker + suffix


def compute_atr(hist: "pd.DataFrame", period: int = ATR_PERIOD) -> float | None:
    """Wilder 14-period ATR from a yfinance OHLC history DataFrame."""
    if len(hist) < period + 1:
        return None
    high  = hist["High"].values
    low   = hist["Low"].values
    close = hist["Close"].values
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])),
    )
    atr = float(np.mean(tr[:period]))
    for t in tr[period:]:
        atr = (atr * (period - 1) + t) / period
    return round(atr, 4)


def compute_rsi(hist: "pd.DataFrame", period: int = 14) -> float | None:
    """Wilder RSI from yfinance close column."""
    closes = hist["Close"].values
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = float(np.mean(gains[:period]))
    avg_l  = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)


def compute_adx(hist: "pd.DataFrame", period: int = 14) -> float | None:
    """ADX directional-movement strength from yfinance OHLC history."""
    if len(hist) < period * 2:
        return None
    high  = hist["High"].values.astype(float)
    low   = hist["Low"].values.astype(float)
    close = hist["Close"].values.astype(float)

    plus_dm  = np.where(
        (high[1:] - high[:-1]) > (low[:-1] - low[1:]),
        np.maximum(high[1:] - high[:-1], 0), 0,
    )
    minus_dm = np.where(
        (low[:-1] - low[1:]) > (high[1:] - high[:-1]),
        np.maximum(low[:-1] - low[1:], 0), 0,
    )
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])),
    )

    def wilder(arr: "np.ndarray", p: int) -> "np.ndarray":
        out = [float(np.mean(arr[:p]))]   # correct Wilder init: average, not sum
        for v in arr[p:]:
            out.append(out[-1] - out[-1] / p + v / p)  # Wilder: (old*(p-1)+v)/p
        return np.array(out)

    atr14 = wilder(tr, period)
    pdm14 = wilder(plus_dm, period)
    mdm14 = wilder(minus_dm, period)
    pdi   = 100 * pdm14 / np.where(atr14 == 0, 1, atr14)
    mdi   = 100 * mdm14 / np.where(atr14 == 0, 1, atr14)
    dx    = 100 * abs(pdi - mdi) / np.where((pdi + mdi) == 0, 1, (pdi + mdi))
    adx_arr = wilder(dx, period)
    return round(float(adx_arr[-1]), 2) if len(adx_arr) else None


def trading_days_between(start_date: str | None, end_date: datetime) -> int | None:
    """Return business-day count between an ISO date string and a datetime.

    Returns None when start_date is missing/invalid — this is deliberately
    DISTINCT from 0 (a position bought today). Callers MUST treat None as
    "holding period unknown, skip duration-based rules" rather than defaulting
    to 0, which would silently assume a freshly-bought position. This matters
    for India (Zerodha/AngelOne) positions, which never have a known entry_date
    from the holdings API — treating that as "0 days old" previously caused
    every India stop-loss signal to be wrongly suppressed by the MIN_HOLD guard
    with a false "position only 0d old" message.
    """
    if not start_date:
        return None
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        return max(0, int(np.busday_count(start.date(), end_date.date())))
    except Exception:
        return None


def fmt_price(p: float | None, currency_symbol: str = "") -> str:
    """Format a price for display."""
    if p is None:
        return "N/A"
    if abs(p) >= 1000:
        return f"{currency_symbol}{p:,.2f}"
    elif abs(p) >= 10:
        return f"{currency_symbol}{p:.2f}"
    return f"{currency_symbol}{p:.3f}"


def fmt_pct(v: float | None) -> str:
    """Format a decimal fraction (e.g. 0.03) as a percentage string."""
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _price_dp(price: float) -> int:
    """Return decimal places needed to represent price movements for penny stocks.

    Normal stocks ($0.01+): 4dp is sufficient.
    Ultra-pennies (<$0.01): use enough places so that a 2% ATR is not rounded to zero.
    Example: $0.0003 → 2% ATR = 0.000006 → needs 6dp minimum.
    """
    if price <= 0:
        return 4
    # Number of significant decimal places: -log10(price) + 3 safety margin, min 4
    magnitude = -math.floor(math.log10(abs(price))) if price > 0 else 0
    return max(4, magnitude + 3)


def _compute_urgency_score(action: str, stopped_out: bool, vix_force_exit: bool,
                            time_stop_triggered: bool, decay_count: int,
                            fund_flag_count: int) -> float:
    """
    Pure 0-10 "how urgently should I consider exiting NOW" score.

    Deliberately independent of conviction_score (which measures stock
    quality/long-term attractiveness, not exit timing) — a high-conviction
    stock sitting right at its hard stop should show HIGH urgency, and a
    low-conviction stock nowhere near any exit trigger should show LOW
    urgency, regardless of how "good" the business is.
    """
    base = {"SELL": 9.0, "REDUCE": 6.5, "TRAIL": 5.0, "REVIEW": 4.0, "HOLD": 1.5}.get(action, 1.5)
    modifier = 0.0
    if stopped_out:
        modifier += 1.0
    if vix_force_exit:
        modifier += 1.0
    if time_stop_triggered:
        modifier += 0.5
    modifier += min(decay_count, DECAY_FACTOR_COUNT) * 0.3
    modifier += min(fund_flag_count, 2) * 0.3
    return round(min(10.0, base + modifier), 1)


def apply_exit_rules(result: dict, vix: float, now: datetime) -> dict:
    """
    Apply all 8 exit rules to a pre-populated result dict.

    *result* must contain at minimum:
        symbol, entry_price, entry_date, high_water_mark,
        current_price (may be None), atr_entry (may be None),
        atr_current (may be None), rsi, adx, rel_vol, pe, roe

    Returns the same dict with all rule outputs merged in.
    """
    entry         = result["entry_price"]
    atr_entry_raw = result["atr_entry"]
    current       = result["current_price"]
    atr_now       = result["atr_current"]
    atr_entry     = atr_entry_raw if atr_entry_raw is not None else (atr_now or entry * 0.02)
    atr_now       = atr_now or atr_entry
    hwm           = result["high_water_mark"]
    rsi           = result["rsi"]
    adx           = result["adx"]
    rel_vol       = result["rel_vol"]
    entry_date    = result["entry_date"]

    # Adaptive decimal-place precision: ultra-penny stocks need more than 4dp
    dp = _price_dp(entry)

    # ── Rule 1: Hard Stop ──────────────────────────────────────────────────────
    hard_stop_atr   = entry - HARD_STOP_MULT * atr_entry
    hard_stop_floor = entry * (1 - HARD_STOP_FLOOR)
    hard_stop       = round(max(hard_stop_atr, hard_stop_floor), dp)

    # ── Rule 2: Break-Even Stop ────────────────────────────────────────────────
    breakeven_trigger = entry + BREAKEVEN_MULT * atr_entry
    breakeven_hit     = current is not None and hwm >= breakeven_trigger
    active_floor_stop = entry if breakeven_hit else hard_stop

    # ── Rule 3: Trailing Stop ──────────────────────────────────────────────────
    trailing_stop  = round(hwm - TRAIL_MULT * atr_now, dp)
    effective_stop = max(active_floor_stop, trailing_stop) if breakeven_hit else active_floor_stop

    # ── Rule 4: R-Multiple Targets ─────────────────────────────────────────────
    R             = HARD_STOP_MULT * atr_entry
    tp1_price_raw = entry + TP1_R * R
    tp2_price_raw = entry + TP2_R * R
    tp1_price     = round(tp1_price_raw, dp)
    tp2_price     = round(tp2_price_raw, dp)
    current_r     = round((current - entry) / R, 2) if current is not None and R > 0 else None
    # Bug fix: for ultra-penny stocks R rounds to zero at 4dp, making tp1/tp2 == entry.
    # Use pre-rounded raw values for the hit check AND require the target is above entry.
    _r_significant = R > entry * 0.001   # R must be at least 0.1% of entry to be meaningful
    tp1_hit = (
        current is not None
        and _r_significant
        and hwm >= tp1_price_raw
        and tp1_price_raw > entry
    )
    tp2_hit = (
        current is not None
        and _r_significant
        and hwm >= tp2_price_raw
        and tp2_price_raw > entry
    )

    # ── Rule 5: Time Stop ──────────────────────────────────────────────────────
    trading_days     = trading_days_between(entry_date, now)   # None if entry_date unknown
    # When current price is unknown, gain is indeterminate (not 0%).
    # Use None so time_stop and other PnL-based rules are suppressed.
    current_gain_pct = ((current - entry) / entry) if current is not None else None
    # Price momentum over the last 5 trading days (positive = recovering)
    price_change_5d  = result.get("price_change_5d")  # fraction, e.g. +0.06 = +6%
    # When avg_cost is unreliable (e.g. leveraged ETFs with avg_cost=0 from OpenD),
    # gain_pct ≈ 0% is meaningless — suppress PnL-based rules.
    _cost_unreliable = bool(result.get("avg_cost_unreliable"))
    time_stop_triggered = (
        not _cost_unreliable
        and current_gain_pct is not None
        and trading_days is not None
        and trading_days >= TIME_STOP_DAYS
        and current_gain_pct < TIME_STOP_GAIN
        and (current_r is None or current_r < 0.5)
    )

    # ── Rule 6: Signal Decay ───────────────────────────────────────────────────
    decay_rsi   = rsi is not None and rsi < 45
    decay_adx   = adx is not None and adx < 20
    decay_rv    = rel_vol is not None and rel_vol < 0.5
    # Optional book-sourced decay signals (from Darvas/VPA integration)
    # darvas_score == -5 → Darvas box breakdown (strong bearish)
    # vpa_score    == -5 → VPA Trap Up (distribution at resistance — bearish)
    darvas_breakdown = result.get("darvas_score") == -5
    vpa_trap_up      = result.get("vpa_score") == -5
    decay_count = (int(decay_rsi) + int(decay_adx) + int(decay_rv)
                   + int(darvas_breakdown) + int(vpa_trap_up))

    # ── Rule 7: VIX Regime ─────────────────────────────────────────────────────
    if vix >= 35:
        vix_regime = "EXTREME_FEAR"
    elif vix >= 25:
        vix_regime = "FEAR"
    else:
        vix_regime = "NORMAL"

    vix_force_exit = (
        vix_regime == "EXTREME_FEAR"
        and current_gain_pct is not None
        and current_gain_pct < 0
        and not _cost_unreliable
    )
    tightened_stop = round(hwm - 1.0 * atr_now, dp) if vix_regime == "FEAR" else None
    if tightened_stop is not None:
        effective_stop = max(effective_stop, tightened_stop)

    # ── Rule 8: Fundamental Check ──────────────────────────────────────────────
    pe              = result.get("pe")
    roe             = result.get("roe")
    fund_pe_flag    = pe is not None and pe > 60
    fund_roe_flag   = roe is not None and roe < 5
    fund_flag_count = int(fund_pe_flag) + int(fund_roe_flag)

    # ── Stopped Out? ───────────────────────────────────────────────────────────
    stopped_out = current is not None and current <= effective_stop

    # ── CGT Proximity Guard ─────────────────────────────────────────────────────
    # If within CGT_PROXIMITY_DAYS of the 12-month discount window and profitable,
    # downgrade any stop-triggered SELL to REVIEW so the trader can weigh tax impact.
    cgt_days_remaining = result.get("days_to_lt")      # computed by _compute_holding_period()
    cgt_proximity_override = (
        not vix_force_exit
        and cgt_days_remaining is not None
        and 0 < cgt_days_remaining <= CGT_PROXIMITY_DAYS
        and current_gain_pct is not None
        and current_gain_pct > CGT_MIN_PROFIT_PCT
    )

    # ── Quality Stop Guard ──────────────────────────────────────────────────────
    # Healthy fundamentals (PE ≤ 40, ROE ≥ 10%) + large embedded gain means the
    # business thesis is likely intact — a trailing-stop breach is probably noise.
    # Downgrade to REVIEW so the trader verifies thesis breakdown before selling.
    quality_stop_override = (
        not vix_force_exit
        and pe is not None and pe <= QUALITY_MAX_PE
        and roe is not None and roe >= QUALITY_MIN_ROE
        and current_gain_pct is not None
        and current_gain_pct > QUALITY_MIN_PROFIT_PCT
    )

    # ── Momentum Override: if stopped-out but 5d rally is strong, defer to REVIEW ──
    momentum_override = False
    momentum_note: str | None = None
    if stopped_out and price_change_5d is not None:
        if price_change_5d >= MOMENTUM_STRONG_THRESHOLD:
            momentum_override = True
            momentum_note = (
                f"5d rally +{price_change_5d*100:.1f}% overrides stop — "
                "momentum STRONG; wait for confirmation candle before exiting"
            )
        elif price_change_5d >= MOMENTUM_MODERATE_THRESHOLD:
            momentum_override = True
            momentum_note = (
                f"5d rally +{price_change_5d*100:.1f}% overrides stop — "
                "momentum MODERATE; consider partial exit only"
            )

    # ── MIN_HOLD_DAYS Guard: freshly bought positions should not trigger a SELL ──
    # Exception: losses beyond 1.25×HARD_STOP_FLOOR (>10%) bypass this guard — a position
    # already past its hard-stop floor by 25%+ is a thesis failure, not normal noise.
    catastrophic_loss = (
        current_gain_pct is not None
        and current_gain_pct < -(CATASTROPHIC_LOSS_MULT * HARD_STOP_FLOOR)
    )
    # trading_days is None when entry_date is unknown (e.g. India positions). Treat
    # unknown duration as NOT freshly-bought — defaulting unknown to "protect it"
    # was a silent, incorrect assumption that masked genuine stop-loss signals.
    min_hold_active = (
        trading_days is not None
        and trading_days < MIN_HOLD_DAYS
        and not vix_force_exit
        and not catastrophic_loss
    )

    # ── Action Decision ────────────────────────────────────────────────────────
    if stopped_out or vix_force_exit:
        # Collect every override that independently applies, so a lower-priority
        # (but still true) consideration is never silently hidden from the trader
        # just because a different override happened to win precedence — e.g. a
        # position with strong momentum that is ALSO approaching its CGT discount
        # window previously showed only the momentum note, dropping the CGT timing
        # information entirely.
        _override_notes: list[str] = []
        if momentum_override:
            _override_notes.append(momentum_note or "5d rally active")
        if min_hold_active:
            _override_notes.append(
                f"position only {trading_days}d old (MIN_HOLD={MIN_HOLD_DAYS}d)"
            )
        if cgt_proximity_override and stopped_out:
            _override_notes.append(f"{cgt_days_remaining}d to CGT 50% discount")
        if quality_stop_override and stopped_out:
            _override_notes.append(
                f"quality position (PE {pe:.1f}, ROE {roe:.1f}%)"
                if (pe is not None and roe is not None) else "quality metrics support hold"
            )

        if momentum_override:
            action = "REVIEW"
            action_reason = momentum_note or "Momentum override: stop touched but 5d rally active"
        elif min_hold_active:
            action = "REVIEW"
            action_reason = (
                f"Stop touched but position only {trading_days}d old (MIN_HOLD={MIN_HOLD_DAYS}d), "
                f"loss={current_gain_pct * 100:.1f}% — confirm break before exiting"
            )
        elif cgt_proximity_override and stopped_out:
            action = "REVIEW"
            action_reason = (
                f"{cgt_days_remaining}d to CGT 50% discount — "
                "consider holding through stop for tax efficiency; "
                "confirm with your accountant before selling"
            )
        elif quality_stop_override and stopped_out:
            action = "REVIEW"
            action_reason = (
                f"Stop touched on quality position (PE {pe:.1f}, ROE {roe:.1f}%, "
                f"+{current_gain_pct * 100:.0f}% gain) — "
                "verify thesis breakdown before exiting"
            )
        else:
            action = "SELL"
            action_reason = (
                "VIX force exit (loser in extreme fear)"
                if vix_force_exit and not stopped_out
                else "Stop loss hit"
            )

        # Append any OTHER active override considerations beyond the one that
        # decided the action (simple containment check skips the one already
        # quoted verbatim in action_reason).
        _extra_notes = [n for n in _override_notes if n not in action_reason]
        if _extra_notes:
            action_reason += "  |  Also applies: " + "; ".join(_extra_notes)
    elif time_stop_triggered:
        action = "SELL"
        action_reason = (
            f"Time stop: {trading_days}d in trade, gain={fmt_pct(current_gain_pct * 100)}"
        )
    elif tp2_hit:
        action = "TRAIL"
        action_reason = "TP2 hit – trail remaining with 1.5×ATR stop"
    elif tp1_hit and decay_count >= 2:
        action = "REDUCE"
        action_reason = f"TP1 hit + {decay_count} decay flags"
    elif tp1_hit:
        action = "TRAIL"
        action_reason = "TP1 hit – move to break-even stop, trail rest"
    elif decay_count >= 2:
        action = "REDUCE"
        action_reason = f"{decay_count}/{DECAY_FACTOR_COUNT} decay flags triggered"
    elif fund_flag_count >= 2:
        action = "REVIEW"
        action_reason = "Fundamental drift: PE or ROE concern"
    else:
        action = "HOLD"
        action_reason = "All systems normal"

    # ── Sell-Urgency Score (0-10, independent of conviction/quality) ───────────
    # Replaces a previously dead "urgency_score" field that was read via
    # `.get("urgency_score") or .get("conviction_score")` but never set anywhere
    # — the prominent "Sell Urgency" badge shown on every card was silently just
    # displaying the CONVICTION score (a "is this a good stock" metric) mislabeled
    # as urgency ("should I exit now"). This is computed purely from exit-rule
    # signals already in scope here, orthogonal to conviction.
    urgency_score = _compute_urgency_score(
        action=action,
        stopped_out=stopped_out,
        vix_force_exit=vix_force_exit,
        time_stop_triggered=time_stop_triggered,
        decay_count=decay_count,
        fund_flag_count=fund_flag_count,
    )

    result.update({
        "atr_entry":          round(atr_entry, dp),
        "hard_stop":          hard_stop,
        "breakeven_trigger":  round(breakeven_trigger, dp),
        "breakeven_hit":      breakeven_hit,
        "trailing_stop":      trailing_stop,
        "effective_stop":     round(effective_stop, dp),
        "tp1_price":          tp1_price,
        "tp2_price":          tp2_price,
        "current_r":          current_r,
        "R_value":            round(R, dp),
        "tp1_hit":            tp1_hit,
        "tp2_hit":            tp2_hit,
        "trading_days":       trading_days,
        "time_stop_triggered": time_stop_triggered,
        "decay_rsi":          decay_rsi,
        "decay_adx":          decay_adx,
        "decay_rv":           decay_rv,
        "decay_count":        decay_count,
        "vix":                vix,
        "vix_regime":         vix_regime,
        "vix_force_exit":     vix_force_exit,
        "fund_pe_flag":       fund_pe_flag,
        "fund_roe_flag":      fund_roe_flag,
        "stopped_out":             stopped_out,
        "momentum_override":       momentum_override,
        "momentum_note":           momentum_note,
        "min_hold_active":         min_hold_active,
        "catastrophic_loss":       catastrophic_loss,
        "cgt_proximity_override":  cgt_proximity_override,
        "cgt_days_remaining":      cgt_days_remaining,
        "quality_stop_override":   quality_stop_override,
        "action":                  action,
        "action_reason":           action_reason,
        "urgency_score":           urgency_score,
        "current_gain_pct":        round(current_gain_pct * 100, 2) if current_gain_pct is not None else 0.0,
    })
    return result


# ─── Class facade ─────────────────────────────────────────────────────────────

class SellEngine:
    """
    Facade exposing all pure-logic sell helpers as static methods.

    Network-free. Inject into sell_strategy.py for orchestration,
    or use directly in tests with synthetic data.
    """

    # Constants (mirrors module-level defaults)
    ATR_PERIOD      = ATR_PERIOD
    HARD_STOP_MULT  = HARD_STOP_MULT
    HARD_STOP_FLOOR = HARD_STOP_FLOOR
    BREAKEVEN_MULT  = BREAKEVEN_MULT
    TRAIL_MULT      = TRAIL_MULT
    TP1_R           = TP1_R
    TP2_R           = TP2_R
    MIN_HOLD_DAYS              = MIN_HOLD_DAYS
    CATASTROPHIC_LOSS_MULT     = CATASTROPHIC_LOSS_MULT
    MOMENTUM_STRONG_THRESHOLD  = MOMENTUM_STRONG_THRESHOLD
    MOMENTUM_MODERATE_THRESHOLD = MOMENTUM_MODERATE_THRESHOLD
    TIME_STOP_DAYS  = TIME_STOP_DAYS
    TIME_STOP_GAIN  = TIME_STOP_GAIN
    DECAY_FACTOR_COUNT     = DECAY_FACTOR_COUNT
    EXCHANGE_MAP    = EXCHANGE_MAP
    CGT_PROXIMITY_DAYS     = CGT_PROXIMITY_DAYS
    CGT_MIN_PROFIT_PCT     = CGT_MIN_PROFIT_PCT
    QUALITY_MAX_PE         = QUALITY_MAX_PE
    QUALITY_MIN_ROE        = QUALITY_MIN_ROE
    QUALITY_MIN_PROFIT_PCT = QUALITY_MIN_PROFIT_PCT

    tv_to_yf             = staticmethod(tv_to_yf)
    compute_atr          = staticmethod(compute_atr)
    compute_rsi          = staticmethod(compute_rsi)
    compute_adx          = staticmethod(compute_adx)
    trading_days_between = staticmethod(trading_days_between)
    fmt_price            = staticmethod(fmt_price)
    fmt_pct              = staticmethod(fmt_pct)
    apply_exit_rules     = staticmethod(apply_exit_rules)
    compute_urgency_score = staticmethod(_compute_urgency_score)

    @staticmethod
    def evaluate_option_position(position: dict) -> dict:
        """
        Evaluate strategy options for an existing option holding.

        Delegates to OptionPositionManager — available for Sell Engine
        post-trade decisions (roll, hedge, partial close, etc.).

        Parameters
        ----------
        position : dict
            Required keys: symbol, option_type, strike, expiry,
            entry_premium, current_premium, underlying_price.
            Optional: quantity, contract_size, iv, rsi, adx, vix.

        Returns
        -------
        dict with keys: primary, alternative, all_strategies,
                        action_plan, summary, warnings.
        """
        try:
            from pathlib import Path
            from path_config import ensure_paths as _ep
            _opts = Path(__file__).parent.parent / "options"
            _ep(_opts.parent)
            from options.option_position_manager import OptionPositionManager
            return OptionPositionManager().evaluate(position)
        except Exception as exc:
            return {
                "error":    str(exc),
                "primary":  None,
                "alternative": None,
                "all_strategies": [],
                "summary":  f"OptionPositionManager unavailable: {exc}",
                "warnings": [],
            }
