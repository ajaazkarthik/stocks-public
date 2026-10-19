"""
External Signal Enricher — L7 layer for signal_stack.py
========================================================
Aggregates three external expert-signal sources into a single L7 score (0–10):

  L7a  YouTube (50% weight)   — Akshat Shrivastava channel mentions
  L7b  Smallcase (35% weight) — User's subscribed basket holdings
  L7c  Twitter (15% weight)   — Expert cashtag mentions (Robert Kiyosaki, Nishant Kumar)
             ^ If Twitter abstains (no mentions), its 15% is redistributed to YouTube/Smallcase.

Score interpretation (same 0–10 as L1–L6):
  0–2  : No positive external signal (either not mentioned or bearish)
  3–5  : Neutral / low-confidence signal
  6–8  : Moderately positive external endorsement
  8–10 : Strong positive endorsement from multiple expert sources

Requirements:
    pip install google-api-python-client youtube-transcript-api vaderSentiment requests beautifulsoup4

Usage (standalone probe):
    python external_signal_enricher.py --symbols NSE:RELIANCE NVDA NSE:TATAPOWER

Wire-in (signal_stack.py):
    from external_signal_enricher import compute_l7_score, l7_html_badge
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Sub-enricher imports (non-fatal) ──────────────────────────────────────────
try:
    from youtube_enricher import get_youtube_mentions, score_youtube
    _YOUTUBE_OK = True
except ImportError as _e:
    logger.debug("external_signal_enricher: youtube_enricher unavailable — %s", _e)
    _YOUTUBE_OK = False

    def get_youtube_mentions(symbol, **kw):  # type: ignore
        return {"mention_count": 0, "sentiment": 0.0, "videos": [], "symbol_checked": symbol}

    def score_youtube(data) -> float:  # type: ignore
        return 0.0


try:
    from smallcase_enricher import score_smallcase
    _SMALLCASE_OK = True
except ImportError as _e:
    logger.debug("external_signal_enricher: smallcase_enricher unavailable — %s", _e)
    _SMALLCASE_OK = False

    def score_smallcase(symbol, **kw):  # type: ignore
        return {"smallcase_score": 5.0, "in_subscription": False, "basket_names": [], "max_weight": 0.0, "all_fetched": 0, "error": ""}


try:
    from twitter_enricher import get_twitter_mentions, score_twitter
    _TWITTER_OK = True
except ImportError as _e:
    logger.debug("external_signal_enricher: twitter_enricher unavailable — %s", _e)
    _TWITTER_OK = False

    def get_twitter_mentions(symbol, **kw):  # type: ignore
        return {"mention_count": 0, "sentiment": 0.0, "tweets": [], "symbol_checked": symbol}

    def score_twitter(data):  # type: ignore[return-value]
        return None


# ─── Weights ────────────────────────────────────────────────────────────────────
_W_YOUTUBE   = 0.50
_W_SMALLCASE = 0.35
_W_TWITTER   = 0.15


def _combine_scores(
    yt_score: float,
    sc_score: float,
    tw_score: Optional[float],
) -> float:
    """
    Combine sub-scores with weight redistribution when Twitter abstains.

    Parameters
    ----------
    yt_score : float     YouTube sub-score 0–10
    sc_score : float     Smallcase sub-score 0–10
    tw_score : float or None
        Twitter sub-score 0–10, or None (abstain = no tweets found)
    """
    if tw_score is None:
        # Redistribute Twitter's 15% proportionally between YouTube (50%) and Smallcase (35%)
        # Ratio: YT/(YT+SC) = 50/85 ≈ 0.588, SC/(YT+SC) = 35/85 ≈ 0.412
        w_yt = _W_YOUTUBE + _W_TWITTER * (_W_YOUTUBE / (_W_YOUTUBE + _W_SMALLCASE))
        w_sc = _W_SMALLCASE + _W_TWITTER * (_W_SMALLCASE / (_W_YOUTUBE + _W_SMALLCASE))
        w_tw = 0.0
    else:
        w_yt = _W_YOUTUBE
        w_sc = _W_SMALLCASE
        w_tw = _W_TWITTER

    tw_val = tw_score if tw_score is not None else 0.0
    return round(w_yt * yt_score + w_sc * sc_score + w_tw * tw_val, 3)


# ─── Public API ─────────────────────────────────────────────────────────────────
def compute_l7_score(tv_symbol: str, yf_ticker: str = "") -> dict:
    """
    Compute the L7 external signal score for a given symbol.

    Parameters
    ----------
    tv_symbol  : TradingView-style symbol e.g. "NSE:RELIANCE", "NASDAQ:NVDA"
    yf_ticker  : Yahoo Finance ticker (optional, used for cross-referencing)

    Returns
    -------
    dict:
        l7_score        float     Combined L7 score 0–10 (or None if all sources failed)
        youtube         dict      Raw output from youtube_enricher
        smallcase       dict      Raw output from smallcase_enricher
        twitter         dict      Raw output from twitter_enricher
        yt_score        float
        sc_score        float
        tw_score        float|None
        sources_active  list[str] Which sub-enrichers contributed
    """
    result: dict = {
        "l7_score":       None,
        "youtube":        {},
        "smallcase":      {},
        "twitter":        {},
        "yt_score":       0.0,
        "sc_score":       5.0,
        "tw_score":       None,
        "sources_active": [],
    }

    symbol = tv_symbol.strip()

    # ── YouTube ───────────────────────────────────────────────────────────────
    if _YOUTUBE_OK:
        try:
            yt_data  = get_youtube_mentions(symbol)
            yt_score = score_youtube(yt_data)
            result["youtube"]  = yt_data
            result["yt_score"] = yt_score
            result["sources_active"].append("youtube")
        except Exception as exc:
            logger.warning("external_signal_enricher: YouTube error — %s", exc)
            yt_score = 0.0
    else:
        yt_score = 0.0

    # ── Smallcase ─────────────────────────────────────────────────────────────
    if _SMALLCASE_OK:
        try:
            sc_data  = score_smallcase(symbol)
            sc_score = sc_data.get("smallcase_score", 5.0)
            result["smallcase"]  = sc_data
            result["sc_score"]   = sc_score
            result["sources_active"].append("smallcase")
        except Exception as exc:
            logger.warning("external_signal_enricher: Smallcase error — %s", exc)
            sc_score = 5.0
    else:
        sc_score = 5.0

    # ── Twitter ───────────────────────────────────────────────────────────────
    tw_score: Optional[float] = None
    if _TWITTER_OK:
        try:
            tw_data  = get_twitter_mentions(symbol)
            tw_score = score_twitter(tw_data)
            result["twitter"] = tw_data
            result["tw_score"] = tw_score
            if tw_score is not None:
                result["sources_active"].append("twitter")
        except Exception as exc:
            logger.warning("external_signal_enricher: Twitter error — %s", exc)

    # ── Combine ───────────────────────────────────────────────────────────────
    if not result["sources_active"]:
        # No sub-enricher produced output — return None to signal graceful skip
        logger.debug("external_signal_enricher: no sources active for %s", symbol)
        return result

    combined = _combine_scores(yt_score, sc_score, tw_score)
    result["l7_score"] = combined
    result["tw_score"] = tw_score

    return result


def l7_html_badge(enrichment: Optional[dict]) -> str:
    """
    Render a compact HTML badge for the signal_stack.py HTML report.
    Matches the badge style used by moomoo_enricher.py and ibkr_enricher.py.
    """
    if not enrichment:
        return ""

    l7  = enrichment.get("l7_score")
    if l7 is None:
        return ""

    yt_score = enrichment.get("yt_score", 0.0)
    sc_score = enrichment.get("sc_score", 5.0)
    tw_score = enrichment.get("tw_score")
    yt_data  = enrichment.get("youtube", {})
    sc_data  = enrichment.get("smallcase", {})
    sources  = enrichment.get("sources_active", [])

    # Colour band
    if l7 >= 7.5:
        colour = "#2ecc71"   # green
    elif l7 >= 5.0:
        colour = "#f39c12"   # amber
    else:
        colour = "#e74c3c"   # red

    # Build tooltip lines
    tips: list[str] = []
    if "youtube" in sources:
        m_cnt = yt_data.get("mention_count", 0)
        tips.append(f"YouTube: {m_cnt} mention(s) score={yt_score:.1f}")
    if "smallcase" in sources:
        in_sub = sc_data.get("in_subscription", False)
        if in_sub:
            baskets    = sc_data.get("basket_names", [])
            max_weight = sc_data.get("max_weight", 0.0)
            basket_str = " + ".join(baskets) if baskets else "basket"
            tips.append(
                f"Smallcase: {basket_str} ({max_weight:.0f}% wt) score={sc_score:.1f}"
            )
        else:
            tips.append(f"Smallcase: not in any basket score={sc_score:.1f}")
    if "twitter" in sources:
        tw_str = f"{tw_score:.1f}" if tw_score is not None else "—"
        tips.append(f"Twitter: score={tw_str}")

    tip_text = " | ".join(tips)

    return (
        f'<span title="{tip_text}" style="'
        f'background:{colour};color:#fff;padding:1px 5px;'
        f'border-radius:3px;font-size:11px;font-weight:bold;'
        f'margin-left:3px;">L7 {l7:.1f}</span>'
    )


# ─── Standalone probe ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json as _json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    ap = argparse.ArgumentParser(description="External signal enricher (L7) standalone probe")
    ap.add_argument("--symbols", nargs="+",
                    default=["NSE:RELIANCE", "NSE:ADANIGREEN", "NVDA", "AAPL"],
                    help="TradingView symbols to evaluate")
    ap.add_argument("--json", action="store_true", help="Output raw JSON")
    args = ap.parse_args()

    print(f"\nExternal Signal Enricher L7")
    print(f"Active sub-enrichers: "
          f"YouTube={'✓' if _YOUTUBE_OK else '✗'}  "
          f"Smallcase={'✓' if _SMALLCASE_OK else '✗'}  "
          f"Twitter={'✓' if _TWITTER_OK else '✗'}\n")

    for sym in args.symbols:
        data = compute_l7_score(sym)
        l7   = data.get("l7_score")
        badge_html = l7_html_badge(data)
        if args.json:
            print(_json.dumps({sym: data}, indent=2, default=str))
        else:
            score_str = f"{l7:.2f}" if l7 is not None else "N/A"
            srcs = ", ".join(data.get("sources_active", [])) or "none"
            print(f"  {sym:<30}  L7={score_str:<6}  "
                  f"YT={data['yt_score']:.1f}  SC={data['sc_score']:.1f}  "
                  f"TW={data['tw_score'] or '–'!s:>5}  "
                  f"sources=[{srcs}]")
    print()
