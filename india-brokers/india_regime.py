"""
India Regime Engine — AngelOne-powered market regime classifier
================================================================
Derives an intraday market regime for NSE/BSE trades from live AngelOne data:

  • NIFTY/BANKNIFTY PCR (Put-Call Ratio) — option-market sentiment
  • F&O OI Buildup distribution           — smart-money flow direction
  • NSE F&O breadth (gainers vs losers)   — market-wide momentum confirmation

Regime:
  BULLISH   — conditions favour entries; lower threshold, larger size
  NEUTRAL   — normal conditions; no adjustment
  BEARISH   — conditions hostile to entries; higher threshold, smaller size
  UNKNOWN   — data unavailable; no adjustment (fail-open for analysis-only mode)

Output of get_regime():
  {
    "regime":           "BULLISH" | "NEUTRAL" | "BEARISH" | "UNKNOWN",
    "score":            7.4,           # 0–10 composite (10 = most bullish)
    "threshold_adj":   -3,             # add to composite threshold before gating order
    "size_multiplier":  1.2,           # multiply position units
    "nifty_pcr":        0.74,          # NIFTY Put-Call Ratio
    "oi_bullish_pct":   62.3,          # % of F&O stocks showing bullish OI buildup
    "gainers_pct":      58.0,          # % breadth gainers (if available)
    "data_age_s":       42,            # seconds since last cache refresh
    "source":           "angelone",
    "errors":           [],
  }

CACHE TTL: 10 minutes (F&O PCR + OI buildup don't meaningfully change sub-minute).

Usage (in webhook_server.py India order flow):
    from india_regime import get_regime

    regime_info = get_regime()   # uses module-level cached singleton
    threshold_for_india = base_threshold + regime_info["threshold_adj"]
    effective_units = max(0, round(units * regime_info["size_multiplier"]))

    if regime_info["regime"] == "BEARISH" and effective_units > 0:
        print(f"  ⚠️  India regime BEARISH (score={regime_info['score']:.1f}) — "
              f"threshold raised by {regime_info['threshold_adj']}")

Standalone:
  python india_regime.py
  python india_regime.py --verbose
  python india_regime.py --json
"""

from __future__ import annotations

import json
import sys
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────── path shim for standalone run ─────
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ─── Thresholds ────────────────────────────────────────────────────────────────

CACHE_TTL = 600  # 10 minutes

# PCR → PCR-score mapping (0–10; higher = more bullish)
# PCR < 0.5  → 9.5 (Very Bullish — heavy call buying, market confident)
# PCR 0.5–0.8 → 7.5 (Bullish)
# PCR 0.8–1.0 → 6.0 (Mildly Bullish)
# PCR 1.0–1.2 → 4.5 (Neutral)
# PCR 1.2–1.5 → 2.5 (Bearish)
# PCR > 1.5  → 1.0 (Very Bearish — heavy put buying)
def _pcr_to_score(pcr: float) -> float:
    if pcr < 0.5:  return 9.5
    if pcr < 0.8:  return 7.5
    if pcr < 1.0:  return 6.0
    if pcr < 1.2:  return 4.5
    if pcr < 1.5:  return 2.5
    return 1.0


# OI bullish-pct thresholds for scoring
def _oi_pct_to_score(bullish_pct: float) -> float:
    """bullish_pct: 0–100 percent of F&O stocks showing bullish OI buildup."""
    if bullish_pct >= 70: return 9.0
    if bullish_pct >= 55: return 7.0
    if bullish_pct >= 45: return 5.0
    if bullish_pct >= 35: return 3.5
    return 2.0


# Gainers breadth thresholds
def _breadth_to_score(gainers_pct: float) -> float:
    if gainers_pct >= 65: return 9.0
    if gainers_pct >= 55: return 7.0
    if gainers_pct >= 45: return 5.0
    if gainers_pct >= 35: return 3.5
    return 2.0


# Composite score → regime + adjustments
_REGIME_TABLE = [
    # (min_score, regime, threshold_adj, size_multiplier)
    (7.5, "BULLISH",  -3, 1.2),
    (5.5, "NEUTRAL",   0, 1.0),
    (3.5, "BEARISH",  +5, 0.6),
    (0.0, "BEARISH",  +8, 0.4),   # very bearish — lean hard against entries
]


def _score_to_regime(score: float) -> tuple[str, int, float]:
    for min_s, regime, adj, mult in _REGIME_TABLE:
        if score >= min_s:
            return regime, adj, mult
    return "BEARISH", +8, 0.4


# ─── Cache ────────────────────────────────────────────────────────────────────

_cache_lock      = threading.Lock()
_cached_regime: Optional[dict] = None
_cached_at: float = 0.0

# ─── Core computation ─────────────────────────────────────────────────────────

def _compute_regime(verbose: bool = False) -> dict:
    """
    Query AngelOne for PCR, OI buildup, and optionally breadth; return regime dict.
    Never raises — returns UNKNOWN on any failure.
    """
    result: dict = {
        "regime":          "UNKNOWN",
        "score":           5.0,
        "threshold_adj":   0,
        "size_multiplier": 1.0,
        "nifty_pcr":       None,
        "oi_bullish_pct":  None,
        "gainers_pct":     None,
        "data_age_s":      0,
        "source":          "angelone",
        "errors":          [],
    }

    try:
        from india_market_internals import IndiaMarketInternals
    except ImportError:
        result["errors"].append("india_market_internals not importable")
        return result

    mi = IndiaMarketInternals(verbose=verbose)

    # ── 1. NIFTY PCR ─────────────────────────────────────────────────────────
    pcr_score: Optional[float] = None
    try:
        pcr_list = mi.pcr()
        nifty_pcr_items = [p for p in pcr_list if p["symbol"].upper() in ("NIFTY", "NIFTY50")]
        if nifty_pcr_items:
            nifty_pcr = nifty_pcr_items[0]["pcr"]
            result["nifty_pcr"] = nifty_pcr
            pcr_score = _pcr_to_score(nifty_pcr)
            if verbose:
                print(f"  [regime] NIFTY PCR={nifty_pcr:.3f} → pcr_score={pcr_score:.1f}")
        else:
            # Fall back to market-wide median PCR if NIFTY not in list
            all_pcrs = [p["pcr"] for p in pcr_list if p["pcr"] is not None]
            if all_pcrs:
                all_pcrs.sort()
                median_pcr = all_pcrs[len(all_pcrs) // 2]
                result["nifty_pcr"] = median_pcr
                pcr_score = _pcr_to_score(median_pcr)
                if verbose:
                    print(f"  [regime] Median PCR={median_pcr:.3f} (NIFTY not found) "
                          f"→ pcr_score={pcr_score:.1f}")
            else:
                result["errors"].append("PCR list empty")
    except Exception as exc:
        result["errors"].append(f"PCR fetch failed: {exc}")

    # ── 2. OI Buildup distribution ────────────────────────────────────────────
    oi_score: Optional[float] = None
    try:
        bullish_count  = 0
        bearish_count  = 0
        seen_symbols: set = set()

        for btype in ("Long Built Up", "Short Covering", "Short Built Up", "Long Unwinding"):
            items = mi.oi_buildup(btype, "NEAR")
            for item in items:
                sym = item["symbol"]
                if sym in seen_symbols:
                    continue
                seen_symbols.add(sym)
                if item["bullish"]:
                    bullish_count += 1
                else:
                    bearish_count += 1

        total = bullish_count + bearish_count
        if total > 0:
            bullish_pct = (bullish_count / total) * 100.0
            result["oi_bullish_pct"] = round(bullish_pct, 1)
            oi_score = _oi_pct_to_score(bullish_pct)
            if verbose:
                print(f"  [regime] OI bullish={bullish_count}/{total} "
                      f"({bullish_pct:.1f}%) → oi_score={oi_score:.1f}")
        else:
            result["errors"].append("OI buildup list empty")
    except Exception as exc:
        result["errors"].append(f"OI buildup fetch failed: {exc}")

    # ── 3. Breadth (optional — non-blocking; use if available) ───────────────
    breadth_score: Optional[float] = None
    try:
        gainers = mi.price_gainers_losers("PercPriceGainers", "NEAR")
        losers  = mi.price_gainers_losers("PercPriceLosers",  "NEAR")
        total_breadth = len(gainers) + len(losers)
        if total_breadth > 0:
            gainers_pct = (len(gainers) / total_breadth) * 100.0
            result["gainers_pct"] = round(gainers_pct, 1)
            breadth_score = _breadth_to_score(gainers_pct)
            if verbose:
                print(f"  [regime] Breadth gainers={len(gainers)}/{total_breadth} "
                      f"({gainers_pct:.1f}%) → breadth_score={breadth_score:.1f}")
    except Exception as exc:
        result["errors"].append(f"Breadth fetch failed (non-critical): {exc}")

    # ── 4. Composite score ────────────────────────────────────────────────────
    available_scores: list[tuple[float, float]] = []  # (score, weight)
    if pcr_score    is not None: available_scores.append((pcr_score,    0.50))
    if oi_score     is not None: available_scores.append((oi_score,     0.35))
    if breadth_score is not None: available_scores.append((breadth_score, 0.15))

    if available_scores:
        total_weight = sum(w for _, w in available_scores)
        composite    = sum(s * w for s, w in available_scores) / total_weight
        result["score"] = round(composite, 2)
        regime, adj, mult = _score_to_regime(composite)
        result["regime"]          = regime
        result["threshold_adj"]   = adj
        result["size_multiplier"] = mult
        if verbose:
            print(f"  [regime] Composite score={composite:.2f} → {regime} "
                  f"(threshold_adj={adj:+d}, size_mult={mult})")
    else:
        # All data sources failed — UNKNOWN
        result["errors"].append("All data sources failed — regime UNKNOWN")

    return result


# ─── Public API ───────────────────────────────────────────────────────────────

def get_regime(force_refresh: bool = False, verbose: bool = False) -> dict:
    """
    Return the current India market regime (cached for CACHE_TTL seconds).

    Args:
        force_refresh — bypass cache and recompute immediately
        verbose       — print debug output during computation

    Returns regime dict (see module docstring for schema).
    Never raises.
    """
    global _cached_regime, _cached_at

    with _cache_lock:
        age = time.time() - _cached_at
        if _cached_regime is not None and not force_refresh and age < CACHE_TTL:
            # Return cached result with updated data_age_s
            fresh = dict(_cached_regime)
            fresh["data_age_s"] = int(age)
            return fresh

    # Compute outside the lock to avoid blocking callers
    try:
        regime = _compute_regime(verbose=verbose)
    except Exception as exc:
        regime = {
            "regime":          "UNKNOWN",
            "score":           5.0,
            "threshold_adj":   0,
            "size_multiplier": 1.0,
            "nifty_pcr":       None,
            "oi_bullish_pct":  None,
            "gainers_pct":     None,
            "data_age_s":      0,
            "source":          "angelone",
            "errors":          [f"compute_regime raised: {exc}"],
        }

    now = time.time()
    with _cache_lock:
        _cached_regime = regime
        _cached_at     = now

    result = dict(regime)
    result["data_age_s"] = 0
    return result


def invalidate_cache() -> None:
    """Force next get_regime() call to recompute."""
    global _cached_at
    with _cache_lock:
        _cached_at = 0.0


def regime_summary_text(regime_info: dict) -> str:
    """Return a human-readable one-liner for logging."""
    r   = regime_info.get("regime", "UNKNOWN")
    s   = regime_info.get("score", 0.0)
    adj = regime_info.get("threshold_adj", 0)
    mul = regime_info.get("size_multiplier", 1.0)
    pcr = regime_info.get("nifty_pcr")
    oi  = regime_info.get("oi_bullish_pct")
    icon = {"BULLISH": "📈", "NEUTRAL": "➡️", "BEARISH": "📉", "UNKNOWN": "❓"}.get(r, "?")
    parts = [f"{icon} India regime={r} score={s:.1f}",
             f"adj={adj:+d} size_mult={mul}×"]
    if pcr  is not None: parts.append(f"NIFTY_PCR={pcr:.2f}")
    if oi   is not None: parts.append(f"OI_bull={oi:.0f}%")
    return "  ".join(parts)


# ─── Standalone CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="India market regime from AngelOne")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json",    "-j", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    print("Fetching India regime data from AngelOne…")
    info = get_regime(force_refresh=True, verbose=args.verbose)

    if args.json:
        print(json.dumps(info, indent=2, default=str))
    else:
        print(regime_summary_text(info))
        if info["errors"]:
            for e in info["errors"]:
                print(f"  ⚠️  {e}")
