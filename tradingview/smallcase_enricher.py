"""
Smallcase Enricher — L7b signal layer for signal_stack.py
=========================================================
Checks if a ticker is in any of the user's subscribed Smallcase baskets.

Data source: smallcase/sc_baskets.json  (manual update after each rebalance)
  - Go to https://www.smallcase.com/subscriptions
  - Copy the current constituent tickers and their weightage into sc_baskets.json

Scoring (weight-proportional):
  score = min(10.0, 4.0 + max_weight / 4.0)
  Examples:
    25% weight → 10.0   (capped)
    16% weight →  8.0
     9% weight →  6.25
     6% weight →  5.5
     3% weight →  4.75
     0% / not in basket → 0.0

Wire-in:
    from smallcase_enricher import get_smallcase_holdings, score_smallcase
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SC_BASKETS_FILE = Path(__file__).parent.parent / "smallcase" / "sc_baskets.json"
_CACHE_TTL = 3600  # 1 hour in-memory cache

_cache: Optional[tuple[float, set[str], dict[str, dict[str, float]]]] = None


def _load_baskets() -> tuple[set[str], dict[str, dict[str, float]]]:
    """Load basket data from sc_baskets.json.

    Returns:
        (all_tickers, basket_weights) where basket_weights maps
        basket_name -> {ticker: weightage_pct}.
    """
    global _cache
    if _cache and (time.time() - _cache[0]) < _CACHE_TTL:
        return _cache[1], _cache[2]

    if not _SC_BASKETS_FILE.exists():
        logger.warning(
            "smallcase_enricher: %s not found. "
            "Add basket stocks to that file to enable Smallcase scoring.",
            _SC_BASKETS_FILE,
        )
        return set(), {}

    try:
        data = json.loads(_SC_BASKETS_FILE.read_text(encoding="utf-8"))
        all_tickers: set[str] = set()
        basket_weights: dict[str, dict[str, float]] = {}

        for iscid, info in data.get("baskets", {}).items():
            raw_stocks = info.get("stocks", {})
            name = info.get("name", iscid)
            # Support both dict {ticker: weight} and legacy list [ticker]
            if isinstance(raw_stocks, dict):
                weights = {t.upper(): float(w) for t, w in raw_stocks.items() if t}
            else:
                weights = {t.upper(): 0.0 for t in raw_stocks if t}
            if weights:
                basket_weights[name] = weights
                all_tickers.update(weights)

        if basket_weights:
            logger.debug(
                "smallcase_enricher: loaded %d basket(s), %d tickers from %s",
                len(basket_weights), len(all_tickers), _SC_BASKETS_FILE.name,
            )
        else:
            logger.warning(
                "smallcase_enricher: %s has no baskets with stocks. "
                "Edit that file to add tickers.",
                _SC_BASKETS_FILE,
            )

        _cache = (time.time(), all_tickers, basket_weights)
        return all_tickers, basket_weights

    except Exception as exc:
        logger.warning("smallcase_enricher: failed to load %s — %s", _SC_BASKETS_FILE, exc)
        return set(), {}


# ─── Public API ─────────────────────────────────────────────────────────────────

def get_smallcase_holdings(
    force_refresh: bool = False,
    **_kwargs,  # absorb legacy cookie_file param
) -> tuple[set[str], dict[str, dict[str, float]]]:
    """Return (all_tickers_set, basket_weights). In-memory cached for 1 hour."""
    global _cache
    if force_refresh:
        _cache = None
    return _load_baskets()


def score_smallcase(symbol: str, **_kwargs) -> dict:
    """
    Check whether *symbol* is in any subscribed Smallcase basket.

    Score is weight-proportional:  min(10.0, 4.0 + max_weight / 4.0)
      25% → 10.0  |  16% → 8.0  |  9% → 6.25  |  3% → 4.75  |  absent → 0.0

    Returns dict with keys:
        smallcase_score  float   0.0 – 10.0
        in_subscription  bool
        basket_names     list[str]
        max_weight       float   highest constituent weight across all baskets
        all_fetched      int
        error            str
    """
    bare = symbol.split(":")[-1].upper() if ":" in symbol else symbol.upper()

    try:
        all_tickers, basket_weights = get_smallcase_holdings()
    except Exception as exc:
        return {
            "smallcase_score": 5.0,
            "in_subscription": False,
            "basket_names": [],
            "max_weight": 0.0,
            "all_fetched": 0,
            "error": str(exc),
        }

    containing = [
        name for name, weights in basket_weights.items() if bare in weights
    ]
    in_sub = bare in all_tickers

    if in_sub:
        max_weight = max(
            basket_weights[name][bare]
            for name in containing
        )
        score = min(10.0, 4.0 + max_weight / 4.0)
    else:
        max_weight = 0.0
        score = 0.0

    return {
        "smallcase_score": round(score, 2),
        "in_subscription": in_sub,
        "basket_names": containing,
        "max_weight": max_weight,
        "all_fetched": len(all_tickers),
        "error": "",
    }


# ─── Standalone probe ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    ap = argparse.ArgumentParser(description="Smallcase enricher probe")
    ap.add_argument("--symbols", nargs="+", default=["NSE:THERMAX", "NSE:KAYNES", "NSE:INFY"])
    ap.add_argument("--dump", action="store_true", help="Print all loaded tickers")
    args = ap.parse_args()

    all_t, bmap = get_smallcase_holdings()
    print(f"\nLoaded {len(all_t)} tickers across {len(bmap)} basket(s)  [{_SC_BASKETS_FILE}]\n")

    if args.dump:
        for bname, tickers in bmap.items():
            print(f"  {bname}: {', '.join(sorted(tickers))}")
        print()

    for sym in args.symbols:
        r = score_smallcase(sym)
        flag = "✅" if r["in_subscription"] else "–"
        print(f"  {flag}  {sym:<30}  score={r['smallcase_score']:.1f}  baskets={r['basket_names']}")
