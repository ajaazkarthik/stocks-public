"""
options_hedger_india.py — India-specific hedge recommendation engine.

Provides:
  _next_expiry_us / _next_expiry_nse  — option expiry date helpers
  _calc_beta                          — 1-year rolling beta vs NIFTY (yfinance)
  _recommend_hedge                    — India holding hedge rules (EXIT/COLLAR/PP/CC/HOLD)
  _add_nifty_beta_hedge               — per-stock NIFTY beta hedge note
  _build_nifty_portfolio_hedge        — aggregated portfolio-level NIFTY hedge
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import structlog

from .options_hedger_constants import (
    BIG_GAIN_PCT,
    MODERATE_GAIN_PCT,
    LOSS_PCT,
    LARGE_POS_INR,
    INDIA_VIX_FEAR,
    NIFTY_LOT_SIZE,
)

_log = structlog.get_logger(__name__)

# ── Beta calculation cache ─────────────────────────────────────────────────────
_BETA_CACHE: dict[tuple[str, str], Optional[float]] = {}

# Default sector betas to NIFTY when yfinance data is unavailable
_SECTOR_BETA_DEFAULTS: dict[str, float] = {
    # Large-cap F&O stocks (generally lower beta)
    "MCX": 0.85, "BEL": 1.1, "TITAN": 0.9, "CDSL": 1.3, "CAMS": 0.95,
    # Mid-cap: slightly higher
    "CAPACITE": 1.4, "SWSOLAR": 1.5, "GANECOS": 1.3, "MARKSANS": 1.2,
    "GREAVESCOT": 1.2, "NAZARA": 1.4, "CIGNITITEC": 1.2, "IFBIND": 1.1,
    "ASTRAMICRO": 1.1, "BLUESTARCO": 0.9, "TCPLPACK": 1.1, "SHARDACROP": 0.8,
    "PREMEXPLN": 0.9, "IMFA": 1.0, "INOXGREEN": 1.5, "LALPATHLAB": 0.85,
    "SANSERA": 1.0,
}

_HEDGE_PRIORITY = {
    "EXIT":           0,
    "PROTECTIVE PUT": 1,
    "COLLAR":         2,
    "COVERED CALL":   3,
    "HOLD":           4,
}


# ─────────────────────────────────────────────────────────────────────────────
# Expiry date helpers (shared with global module via import)
# ─────────────────────────────────────────────────────────────────────────────

def _next_expiry_us() -> str:
    """Next 3rd Friday of nearest future month (US monthly options expiry)."""
    today = datetime.now()
    for offset in range(4):
        month = today.month + offset
        year  = today.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        first = datetime(year, month, 1)
        days_to_fri = (4 - first.weekday()) % 7    # 4 = Friday
        third_fri = first + timedelta(days=days_to_fri + 14)
        if third_fri.date() > today.date():
            return third_fri.strftime("%d %b %Y")
    return "next month"


def _next_expiry_nse() -> str:
    """Last Thursday of nearest future month (NSE monthly options expiry)."""
    today = datetime.now()
    for offset in range(4):
        month = today.month + offset
        year  = today.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        if month == 12:
            last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = datetime(year, month + 1, 1) - timedelta(days=1)
        days_back = (last_day.weekday() - 3) % 7   # 3 = Thursday
        last_thu  = last_day - timedelta(days=days_back)
        if last_thu.date() > today.date():
            return last_thu.strftime("%d %b %Y")
    return "next month"


# ─────────────────────────────────────────────────────────────────────────────
# Beta calculation
# ─────────────────────────────────────────────────────────────────────────────

def _calc_beta(
    yf_ticker: str,
    index_ticker: str = "^NSEI",
    period: str = "1y",
) -> Optional[float]:
    """
    Calculate beta of a stock vs. an index using 1-year daily returns (yfinance).
    Returns None when data is unavailable (<20 data points or download failure).
    Cached by (yf_ticker, index_ticker) to avoid repeated downloads.
    """
    key = (yf_ticker.upper(), index_ticker.upper())
    if key in _BETA_CACHE:
        return _BETA_CACHE[key]

    result: Optional[float] = None
    try:
        import yfinance as yf
        import numpy as np

        raw = yf.download(
            [yf_ticker, index_ticker],
            period=period,
            auto_adjust=True,
            progress=False,
            threads=False,
        )

        if raw is None or raw.empty:
            _BETA_CACHE[key] = None
            return None

        close = raw["Close"] if "Close" in raw else raw.get("close")
        if close is None or close.empty:
            _BETA_CACHE[key] = None
            return None

        # Handle both MultiIndex column frames and single frames
        if hasattr(close.columns, "levels"):
            cols = [str(c[0]) if isinstance(c, tuple) else str(c) for c in close.columns]
            close.columns = cols
        else:
            close.columns = [str(c) for c in close.columns]

        if yf_ticker not in close.columns or index_ticker not in close.columns:
            _BETA_CACHE[key] = None
            return None

        ret = close[[yf_ticker, index_ticker]].pct_change(fill_method=None).dropna()
        if len(ret) < 20:
            _BETA_CACHE[key] = None
            return None

        cov  = np.cov(ret[yf_ticker].values, ret[index_ticker].values)
        beta = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] != 0 else None
        result = round(beta, 2) if beta is not None else None

    except Exception as exc:
        _log.warning("_calc_beta failed", ticker=yf_ticker, index=index_ticker, exc_info=exc)
        result = None

    _BETA_CACHE[key] = result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# India hedge recommendation engine
# ─────────────────────────────────────────────────────────────────────────────

def _recommend_hedge(
    holding: dict,
    pcr:     Optional[float],
    oi_type: Optional[str],
    regime:  dict,
    big_gain_pct: float = BIG_GAIN_PCT,
    loss_pct:     float = LOSS_PCT,
    india_vix:    Optional[float] = None,
    nifty_price:  Optional[float] = None,
) -> dict:
    """
    Apply rule-based logic to recommend a hedging strategy.
    Returns dict with: strategy, urgency, rationale (list of strings), details.
    """
    pnl_pct  = holding.get("combined_pnl_pct", 0)
    val      = holding.get("combined_val", 0)
    sym      = holding.get("symbol", "?")
    qty      = holding.get("total_qty", 0)
    avg      = holding.get("combined_avg", 0)
    ltp      = holding.get("combined_ltp", 0)

    regime_name   = (regime.get("regime") or "").upper()
    is_bear_regime = "BEAR" in regime_name
    is_bullish_oi  = oi_type in ("Long Built Up", "Short Covering")
    is_bearish_oi  = oi_type in ("Short Built Up", "Long Unwinding")
    pcr_bearish    = pcr is not None and pcr > 1.2
    pcr_bullish    = pcr is not None and pcr < 0.8
    vix_fear       = india_vix is not None and india_vix > INDIA_VIX_FEAR

    strategy  = "HOLD"
    urgency   = "LOW"
    rationale = []
    details   = {}

    # ── Rule 1: EXIT flag — confirmed by bearish OI or bear regime ────────────
    if pnl_pct < loss_pct and (is_bearish_oi or is_bear_regime):
        strategy  = "EXIT"
        urgency   = "HIGH"
        rationale.append(f"P&L = {pnl_pct:.1f}% (below {loss_pct}%)")
        if is_bearish_oi:
            rationale.append(f"OI signal: {oi_type} (bearish)")
        if is_bear_regime:
            rationale.append(f"Market regime: {regime_name}")
        details["stop_price"] = round(avg * (1 + loss_pct / 100), 2)
        details["stop_loss_note"] = f"Consider stop at ₹{details['stop_price']:,.2f}"

    # ── Rule 1b: EXIT — severe drawdown ───────────────────────────────────────
    elif pnl_pct < -20.0:
        strategy  = "EXIT"
        urgency   = "HIGH" if pnl_pct < -30.0 else "MEDIUM"
        rationale.append(f"Severe drawdown: {pnl_pct:.1f}% — stop-loss territory")
        if not oi_type:
            rationale.append("No F&O OI signal available (non-F&O stock)")
        if not is_bear_regime and regime_name:
            rationale.append(f"Note: regime = {regime_name} (not bear-confirmed, but loss severe)")
        elif not regime_name:
            rationale.append("Market regime: Unknown — assume caution")
        if vix_fear and india_vix is not None:
            rationale.append(f"India VIX = {india_vix:.1f} (elevated fear)")
        details["stop_price"] = round(avg * (1 + loss_pct / 100), 2)
        details["stop_loss_note"] = (
            f"Original -10% stop was ₹{details['stop_price']:,.2f}. "
            f"Now at {pnl_pct:.1f}% — consider exiting to free capital."
        )

    # ── Rule 2: COLLAR ─────────────────────────────────────────────────────────
    elif (pnl_pct >= big_gain_pct
          and val >= LARGE_POS_INR
          and (is_bearish_oi or pcr_bearish or is_bear_regime)):
        strategy  = "COLLAR"
        urgency   = "HIGH" if pnl_pct >= 50 else "MEDIUM"
        rationale.append(f"Large gain: {pnl_pct:.1f}% (>{big_gain_pct}%)")
        rationale.append(f"Position size: ₹{val:,.0f} (>{LARGE_POS_INR:,.0f})")
        if is_bearish_oi:
            rationale.append(f"OI signal: {oi_type} (bearish)")
        if pcr_bearish:
            rationale.append(f"PCR = {pcr:.2f} (bearish, >1.2)")
        if is_bear_regime:
            rationale.append(f"Market in {regime_name} regime")
        put_strike  = round(ltp * 0.95 / 50) * 50
        call_strike = round(ltp * 1.10 / 50) * 50
        details["put_strike"]  = put_strike
        details["call_strike"] = call_strike
        details["collar_note"] = (f"Buy {sym} {put_strike}PE (5% OTM put) + "
                                   f"Sell {sym} {call_strike}CE (10% OTM call)")
        details["lot_note"]    = f"Adjust qty to nearest lot size multiple"

    # ── Rule 3: PROTECTIVE PUT ─────────────────────────────────────────────────
    elif (pnl_pct >= big_gain_pct
          or (pnl_pct >= MODERATE_GAIN_PCT and is_bear_regime)
          or (vix_fear and pnl_pct >= 10.0)):
        strategy  = "PROTECTIVE PUT"
        urgency   = "MEDIUM"
        if pnl_pct >= big_gain_pct:
            rationale.append(f"Large gain: {pnl_pct:.1f}% — protect profits")
        if is_bear_regime:
            rationale.append(f"Market regime: {regime_name}")
        if vix_fear and india_vix is not None:
            rationale.append(f"India VIX = {india_vix:.1f} (fear zone >{INDIA_VIX_FEAR})")
        atm_strike = round(ltp / 50) * 50
        otm_strike = round(ltp * 0.95 / 50) * 50
        details["atm_strike"]    = atm_strike
        details["otm_strike"]    = otm_strike
        details["put_note"]      = (f"ATM put: {sym} {atm_strike}PE (~1–2% cost) "
                                    f"or OTM: {otm_strike}PE (~0.5–1% cost)")
        details["cost_estimate"] = f"~₹{val * 0.01:,.0f} – ₹{val * 0.025:,.0f} per month"

    # ── Rule 4: COVERED CALL ───────────────────────────────────────────────────
    elif (pnl_pct >= MODERATE_GAIN_PCT
          and not is_bearish_oi
          and not is_bear_regime):
        strategy  = "COVERED CALL"
        urgency   = "LOW"
        rationale.append(f"Moderate gain: {pnl_pct:.1f}% — generate income")
        if is_bullish_oi:
            rationale.append(f"OI: {oi_type} — stock not under pressure")
        call_strike = round(ltp * 1.10 / 50) * 50
        details["call_strike"] = call_strike
        details["call_note"]   = (f"Sell {sym} {call_strike}CE (10% OTM call) "
                                   f"~1–2% premium income")
        details["income_est"]  = f"~₹{val * 0.01:,.0f} – ₹{val * 0.02:,.0f} per month"

    # ── Rule 5: HOLD ──────────────────────────────────────────────────────────
    else:
        if pnl_pct < 0:
            rationale.append(f"P&L = {pnl_pct:.1f}% — within acceptable loss range; monitor closely")
        else:
            rationale.append(f"P&L = {pnl_pct:.1f}% — no immediate hedge needed")
        if is_bullish_oi:
            rationale.append(f"OI: {oi_type} — bullish signal")
        elif oi_type:
            rationale.append(f"OI signal: {oi_type}")
        if pcr_bullish:
            rationale.append(f"PCR = {pcr:.2f} (bullish)")

    return {
        "strategy":  strategy,
        "urgency":   urgency,
        "rationale": rationale,
        "details":   details,
        "pcr":       pcr,
        "oi_type":   oi_type,
        "regime":    regime.get("regime", "Unknown"),
        "india_vix": india_vix,
    }


def _add_nifty_beta_hedge(
    sym: str,
    val: float,
    details: dict,
    nifty_price: Optional[float] = None,
) -> None:
    """Appends NIFTY beta-based hedge recommendation to the `details` dict."""
    if val <= 0:
        return

    raw = sym.split(":")[-1].upper()
    yf_ticker = f"{raw}.NS"

    beta = _calc_beta(yf_ticker, "^NSEI", "1y")
    if beta is None or not (0.1 <= abs(beta) <= 5.0):
        beta     = _SECTOR_BETA_DEFAULTS.get(raw, 1.2)
        beta_src = "estimated"
    else:
        beta_src = "calculated"

    if nifty_price is None or nifty_price <= 0:
        try:
            import yfinance as yf
            nd = yf.download("^NSEI", period="2d", auto_adjust=True, progress=False)
            nifty_price = float(nd["Close"].iloc[-1].item()) if not nd.empty else 22_000.0
        except Exception:
            nifty_price = 22_000.0

    nifty_lot_val = nifty_price * NIFTY_LOT_SIZE
    lots = max(0, round(val * abs(beta) / nifty_lot_val))
    expiry    = _next_expiry_nse()
    put_strike = round(nifty_price * 0.97 / 50) * 50

    if lots == 0:
        partial = val * abs(beta) / nifty_lot_val
        hedge_note = (
            f"Position too small for 1 NIFTY lot "
            f"({partial:.2f} lots required at beta={beta:.2f} [{beta_src}]). "
            f"Combine with other India positions before hedging."
        )
    else:
        one_lot_approx_cost = nifty_price * 0.005 * NIFTY_LOT_SIZE
        total_cost_lo = one_lot_approx_cost * lots
        total_cost_hi = total_cost_lo * 2
        hedge_note = (
            f"Buy {lots}× NIFTY {put_strike}PE {expiry} "
            f"(beta={beta:.2f} [{beta_src}] → "
            f"{val * abs(beta):,.0f} / {nifty_lot_val:,.0f} per lot = {lots} lots)"
        )
        details["nifty_prem_est"] = (
            f"Est. cost: ~₹{total_cost_lo:,.0f} – ₹{total_cost_hi:,.0f} per month "
            f"(~{total_cost_lo/val*100:.1f}% of position)"
        )

    details["nifty_hedge"] = hedge_note


def _build_nifty_portfolio_hedge(
    india_recs: list[dict],
    oi_map: dict,
    nifty_price: float,
) -> Optional[dict]:
    """
    Aggregate ALL India non-F&O positions into a single portfolio-level
    NIFTY beta-hedge recommendation.
    """
    if nifty_price <= 0:
        return None

    total_beta_weighted = 0.0
    positions_info: list[dict] = []

    for item in india_recs:
        h   = item["holding"]
        sym = h.get("symbol", "").upper()
        raw = sym.split(":")[-1]

        if raw in oi_map or sym in oi_map:
            continue

        val = h.get("combined_val", 0)
        if val <= 0:
            continue

        yf_ticker = f"{raw}.NS"
        beta = _calc_beta(yf_ticker, "^NSEI", "1y")
        if beta is None or not (0.1 <= abs(beta) <= 5.0):
            beta     = _SECTOR_BETA_DEFAULTS.get(raw, 1.2)
            beta_src = "est"
        else:
            beta_src = "calc"

        bw = val * abs(beta)
        total_beta_weighted += bw
        positions_info.append({
            "symbol":        raw,
            "val":           val,
            "beta":          beta,
            "beta_src":      beta_src,
            "beta_weighted": bw,
        })

    if not positions_info:
        return None

    nifty_lot_val = nifty_price * NIFTY_LOT_SIZE
    lots          = round(total_beta_weighted / nifty_lot_val)

    if lots == 0:
        partial = total_beta_weighted / nifty_lot_val
        return {
            "lots":      0,
            "partial":   partial,
            "positions": positions_info,
            "total_val": sum(p["val"] for p in positions_info),
            "nifty_price": nifty_price,
        }

    expiry     = _next_expiry_nse()
    put_strike = round(nifty_price * 0.97 / 50) * 50
    total_val  = sum(p["val"] for p in positions_info)
    one_lot_cost = nifty_price * 0.005 * NIFTY_LOT_SIZE
    cost_lo      = round(one_lot_cost * lots)
    cost_hi      = round(one_lot_cost * lots * 2)

    return {
        "lots":          lots,
        "put_strike":    put_strike,
        "expiry":        expiry,
        "nifty_price":   nifty_price,
        "total_val":     total_val,
        "total_beta_wt": total_beta_weighted,
        "cost_lo":       cost_lo,
        "cost_hi":       cost_hi,
        "positions":     positions_info,
    }
