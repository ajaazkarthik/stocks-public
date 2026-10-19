"""
book_features_scorer.py — Central wrapper for all 6 book-sourced signal features.

Combines: VPA Anomaly (Coulling), Darvas Box (Darvas), Gap Analysis (Bulkowski),
Acquirer's Multiple (Carlisle), RS Rating (O'Neil), Per-Market Expectancy (Van Tharp).

Each feature can be individually disabled via the ``flags`` dict.
Returns a combined result dict with per-feature scores and a total_bonus value.
"""

from __future__ import annotations

import sys
import os

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow importing sibling modules when called from within tradingview/
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from typing import Any

try:
    import pandas as pd
    _PD_OK = True
except ImportError:
    _PD_OK = False


# ─── Flag helpers ────────────────────────────────────────────────────────────

_DEFAULT_FLAGS: dict[str, bool] = {
    "disable_vpa":    False,
    "disable_darvas": False,
    "disable_gap":    False,
    "disable_am":     False,
    "disable_rs":     False,
}

_EMPTY_RESULT: dict[str, Any] = {
    "signal": "NEUTRAL",
    "score": 0,
    "detail": "disabled",
}


def _mk_flags(flags: dict | None) -> dict[str, bool]:
    """Merge user-supplied flags with defaults (all enabled by default)."""
    merged = dict(_DEFAULT_FLAGS)
    if flags:
        merged.update(flags)
    return merged


# ─── OHLCV column normalisation ──────────────────────────────────────────────

def _normalise_ohlcv(df: "pd.DataFrame") -> "pd.DataFrame":
    """Ensure lowercase column names: open, high, low, close, volume."""
    if df is None or df.empty:
        return df
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    return df


# ─── Main entry point ────────────────────────────────────────────────────────

def score_book_features(
    ohlcv_df: "pd.DataFrame | None",
    fund_dict: dict | None = None,
    rs_value: int | None = None,
    trades: list[dict] | None = None,
    group_key: str = "broker",
    flags: dict | None = None,
) -> dict[str, Any]:
    """
    Compute all book-sourced signal scores for one ticker.

    Parameters
    ----------
    ohlcv_df  : DataFrame with open/high/low/close/volume columns.
                Pass None or empty DataFrame when OHLCV is unavailable.
    fund_dict : Fundamental data dict from the TV screener / yfinance.
                Keys used: market_cap, total_debt, total_cash, ebit, ebitda.
    rs_value  : Pre-computed RS Rating (1-99 percentile) for this ticker.
                Pass None to skip the RS score.
    trades    : List of trade dicts for per-market expectancy computation.
                Each dict needs: pnl (float), broker (str, or whatever group_key).
    group_key : Key to group trades by (default "broker").
    flags     : Dict with disable flags.  Keys:
                  disable_vpa, disable_darvas, disable_gap, disable_am, disable_rs

    Returns
    -------
    dict with keys:
        vpa        — VPA result dict (signal, score, detail)
        darvas     — Darvas Box result dict
        gap        — Gap Analysis result dict
        am         — Acquirer's Multiple result dict
        rs         — RS Rating result dict
        expectancy — Per-Market Expectancy result dict (summary only)
        total_bonus — int, sum of all active bonus scores (capped at ±10)
        active_features — list of enabled feature names
        disabled_features — list of disabled feature names
    """
    flags = _mk_flags(flags)

    results: dict[str, Any] = {}
    active: list[str] = []
    disabled: list[str] = []
    total_bonus = 0

    # Normalise OHLCV once
    if _PD_OK and ohlcv_df is not None and not ohlcv_df.empty:
        df = _normalise_ohlcv(ohlcv_df)
    else:
        df = None

    # ── 1. VPA Anomaly ───────────────────────────────────────────────────────
    if flags.get("disable_vpa"):
        results["vpa"] = dict(_EMPTY_RESULT)
        disabled.append("VPA")
    else:
        try:
            from vpa_anomaly import vpa_signals
            if df is not None and len(df) >= 20:
                vpa_res = vpa_signals(df)
            else:
                vpa_res = {"signal": "NEUTRAL", "score": 0,
                           "detail": "Insufficient OHLCV data (<20 bars)"}
        except Exception as exc:
            vpa_res = {"signal": "ERROR", "score": 0, "detail": str(exc)}
        results["vpa"] = vpa_res
        total_bonus += vpa_res.get("score", 0)
        active.append("VPA")

    # ── 2. Darvas Box ────────────────────────────────────────────────────────
    if flags.get("disable_darvas"):
        results["darvas"] = dict(_EMPTY_RESULT)
        disabled.append("Darvas")
    else:
        try:
            from darvas_box import detect_darvas_box
            if df is not None and len(df) >= 20:
                darvas_res = detect_darvas_box(df)
            else:
                darvas_res = {"signal": "NEUTRAL", "score": 0,
                              "detail": "Insufficient OHLCV data (<20 bars)",
                              "box_high": None, "box_low": None}
        except Exception as exc:
            darvas_res = {"signal": "ERROR", "score": 0, "detail": str(exc),
                          "box_high": None, "box_low": None}
        results["darvas"] = darvas_res
        total_bonus += darvas_res.get("score", 0)
        active.append("Darvas")

    # ── 3. Gap Analysis ──────────────────────────────────────────────────────
    if flags.get("disable_gap"):
        results["gap"] = dict(_EMPTY_RESULT)
        disabled.append("Gap")
    else:
        try:
            from gap_analysis import classify_gap
            if df is not None and len(df) >= 5:
                gap_res = classify_gap(df)
            else:
                gap_res = {"signal": "NEUTRAL", "score": 0,
                           "detail": "Insufficient OHLCV data (<5 bars)",
                           "gap_pct": 0.0}
        except Exception as exc:
            gap_res = {"signal": "ERROR", "score": 0, "detail": str(exc),
                       "gap_pct": 0.0}
        results["gap"] = gap_res
        total_bonus += gap_res.get("score", 0)
        active.append("Gap")

    # ── 4. Acquirer's Multiple ───────────────────────────────────────────────
    if flags.get("disable_am"):
        results["am"] = dict(_EMPTY_RESULT)
        disabled.append("AM")
    else:
        try:
            from acquirers_multiple import compute_acquirers_multiple, score_acquirers_multiple
            fd = fund_dict or {}
            market_cap  = fd.get("market_cap")
            total_debt  = fd.get("total_debt") or 0.0
            total_cash  = fd.get("total_cash") or 0.0
            ebit        = fd.get("ebit") or fd.get("net_income")
            if market_cap is not None and ebit is not None:
                try:
                    market_cap  = float(market_cap)
                    total_debt  = float(total_debt) if total_debt else 0.0
                    total_cash  = float(total_cash) if total_cash else 0.0
                    ebit        = float(ebit)
                    ev = market_cap + total_debt - total_cash
                    am = compute_acquirers_multiple(ev, ebit)
                    am_res = score_acquirers_multiple(am)
                except (TypeError, ValueError):
                    am_res = {"signal": "NEUTRAL", "score": 0,
                              "detail": "Fundamental data type error"}
            else:
                am_res = {"signal": "NEUTRAL", "score": 0,
                          "detail": "Fundamental data unavailable (market_cap or ebit missing)"}
        except Exception as exc:
            am_res = {"signal": "ERROR", "score": 0, "detail": str(exc)}
        results["am"] = am_res
        total_bonus += am_res.get("score", 0)
        active.append("AM")

    # ── 5. RS Rating ────────────────────────────────────────────────────────
    if flags.get("disable_rs"):
        results["rs"] = dict(_EMPTY_RESULT)
        disabled.append("RS")
    else:
        try:
            from rs_rating import score_rs
            if rs_value is not None:
                rs_res = score_rs(int(rs_value))
            else:
                rs_res = {"signal": "NEUTRAL", "score": 0,
                          "detail": "RS Rating not available (no universe data)"}
        except Exception as exc:
            rs_res = {"signal": "ERROR", "score": 0, "detail": str(exc)}
        results["rs"] = rs_res
        total_bonus += rs_res.get("score", 0)
        active.append("RS")

    # ── 6. Per-Market Expectancy (summary — not per-ticker) ─────────────────
    # Expectancy is a portfolio-level metric (trades list required).
    # When trades are provided, compute per-group and store the best group's score.
    # When no trades provided, store neutral (0) without disabling.
    if trades:
        try:
            from per_market_expectancy import compute_expectancy, score_expectancy
            exp_data = compute_expectancy(trades, group_key=group_key)
            # Summarise across all groups: pick group with most trades as representative
            if exp_data:
                best = max(exp_data.items(), key=lambda kv: kv[1].get("trade_count", 0))
                best_key, best_data = best
                exp_res = score_expectancy(best_data.get("expectancy", 0))
                exp_res["per_group"] = {
                    k: {
                        "expectancy": round(v.get("expectancy", 0), 4),
                        "trade_count": v.get("trade_count", 0),
                        "win_rate": round(v.get("win_rate", 0), 4),
                    }
                    for k, v in exp_data.items()
                }
                exp_res["best_group"] = best_key
            else:
                exp_res = {"signal": "NEUTRAL", "score": 0,
                           "detail": "No trades with matching group key"}
        except Exception as exc:
            exp_res = {"signal": "ERROR", "score": 0, "detail": str(exc)}
    else:
        exp_res = {"signal": "NEUTRAL", "score": 0,
                   "detail": "No trade history provided"}

    results["expectancy"] = exp_res
    # Expectancy bonus is NOT included in total_bonus (portfolio-level, not per-ticker)
    active.append("Expectancy")

    # ── Clamp total bonus to ±10 to avoid overwhelming L1-L4 scores ─────────
    total_bonus = max(-10, min(10, int(total_bonus)))

    return {
        **results,
        "total_bonus":        total_bonus,
        "active_features":    active,
        "disabled_features":  disabled,
    }


if __name__ == "__main__":  # pragma: no cover
    import json
    sample = score_all_features("AAPL", {})
    print(json.dumps(sample, indent=2, default=str))
