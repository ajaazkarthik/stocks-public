"""
Twitter/X Expert Enricher — L7c signal layer for signal_stack.py
================================================================

⚠️  STATUS: DISABLED (2026-04-07)
---------------------------------
Reason for disabling:
  • Kiyosaki covers gold/BTC/USD — zero relevance to NSE/BSE Indian stocks.
  • @nishkumar77 handle unverified; no confirmed NSE:TICKER cashtag usage.
  • twscrape session breaks silently on X/Twitter anti-scraping measures.
  • Composite impact was only 0.75% — even perfect signal wouldn't matter.
  • Async overhead on every alert check with guaranteed return-nothing result.

Re-enable ONLY when you add Indian market expert accounts that actively post
NSE:TICKER or $TICKER format cashtags.  Set TWITTER_ENABLED = True and
update EXPERT_HANDLES accordingly.

Original design docs:
  Accounts: Robert Kiyosaki (@theRealKiyosaki), Nishant Kumar (@nishkumar77).
  Useful for commodity symbols: $GOLD, $SLV, $GLD, $BTC (if ever re-enabled).

Weak-signal detection:
  If total score impact < SIGNAL_THRESHOLD against a broad test set, the enricher
  returns `twitter_score = None`  which external_signal_enricher.py treats as
  "no signal available" rather than 0 (penalising) or 5 (neutral).

Requirements:
    pip install twscrape vaderSentiment

Usage (standalone):
    python twitter_enricher.py --symbols NVDA NSE:RELIANCE
    python twitter_enricher.py --setup-accounts  # first-time auth

Wire-in:
    from twitter_enricher import get_twitter_mentions, score_twitter
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
EXPERT_HANDLES: list[str] = [
    "theRealKiyosaki",   # Robert Kiyosaki
    "nishkumar77",       # Nishant Kumar (verify handle; twscrape will skip if wrong)
]

LOOKBACK_HOURS     = 72
MAX_TWEETS_PER_ACC = 30   # per account per symbol check

# If the delta in composite score is less than this, warn that signal is weak
SIGNAL_THRESHOLD = 0.5   # composite score units (0–100 scale)

# ─── Kill-switch ─────────────────────────────────────────────────────────────────────
# Set TWITTER_ENABLED = True to re-activate once Indian expert accounts with
# NSE:TICKER / $TICKER format cashtags are configured in EXPERT_HANDLES.
TWITTER_ENABLED: bool = False

# twscrape accounts file (for authentication state)
_ACCOUNTS_DB = Path(__file__).parent.parent / "database" / ".twscrape_accounts.db"

# In-memory cache: handle → (ts, tweets_list)
_TWEET_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 1800   # 30 min (tweets more volatile than YouTube)


# ─── twscrape async helpers ──────────────────────────────────────────────────────
async def _async_fetch_user_tweets(handle: str, max_tweets: int = MAX_TWEETS_PER_ACC) -> list[dict]:
    """
    Async helper to fetch recent tweets from *handle* using twscrape.
    Returns list of {tweet_id, text, date, likes, retweets}.
    """
    now_ts = time.time()
    cached = _TWEET_CACHE.get(handle)
    if cached and (now_ts - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        import twscrape  # type: ignore
    except ImportError:
        logger.warning("twitter_enricher: twscrape not installed — pip install twscrape")
        return []

    tweets: list[dict] = []
    try:
        api = twscrape.API(str(_ACCOUNTS_DB))
        # Check if we have any logged-in accounts
        accounts = await api.pool.accounts_info()
        if not accounts:
            logger.warning(
                "twitter_enricher: no twscrape accounts configured. "
                "Run: python twitter_enricher.py --setup-accounts"
            )
            return []

        async for tw in api.user_tweets_and_replies(handle, limit=max_tweets):
            tweet_date = tw.date
            # Filter by lookback window
            age_hours = (time.time() - tweet_date.timestamp()) / 3600
            if age_hours > LOOKBACK_HOURS:
                continue
            tweets.append({
                "tweet_id":  str(tw.id),
                "text":      tw.rawContent or tw.renderedContent or "",
                "date":      tweet_date.isoformat(),
                "likes":     tw.likeCount or 0,
                "retweets":  tw.retweetCount or 0,
            })

        _TWEET_CACHE[handle] = (now_ts, tweets)
    except Exception as exc:
        logger.warning("twitter_enricher: error fetching @%s — %s", handle, exc)

    return tweets


def _fetch_user_tweets(handle: str) -> list[dict]:
    """Sync wrapper for async tweet fetch."""
    try:
        # asyncio.get_running_loop() raises RuntimeError when no loop is running.
        # In Python 3.10+ get_event_loop() raises DeprecationWarning/RuntimeError
        # in a main thread with no existing loop, so we never call it.
        asyncio.get_running_loop()
        # Already inside a running loop (Jupyter, async server) — use a thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _async_fetch_user_tweets(handle))
            return future.result(timeout=30)
    except RuntimeError:
        # No running loop — asyncio.run() is safe
        try:
            return asyncio.run(_async_fetch_user_tweets(handle))
        except Exception as exc:
            logger.warning("twitter_enricher: fetch error for @%s — %s", handle, exc)
            return []
    except Exception as exc:
        logger.warning("twitter_enricher: event loop error for @%s — %s", handle, exc)
        return []


# ─── Ticker extraction (same pattern as youtube_enricher) ──────────────────────
_CASHTAG_RE  = re.compile(r'\$([A-Z]{2,12})')
_EXCHANGE_RE = re.compile(r'\b(?:NSE|BSE|NYSE|NASDAQ)[:.]([A-Z0-9]{2,12})\b')

def _extract_cashtags(text: str) -> set[str]:
    text_upper = text.upper()
    found = set()
    for m in _CASHTAG_RE.finditer(text_upper):
        found.add(m.group(1))
    for m in _EXCHANGE_RE.finditer(text_upper):
        found.add(m.group(1))
    return found


# ─── VADER sentiment ─────────────────────────────────────────────────────────────
def _vader_compound(text: str) -> float:
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        return SentimentIntensityAnalyzer().polarity_scores(text)["compound"]
    except ImportError:
        return 0.0


# ─── Public API ──────────────────────────────────────────────────────────────────
def get_twitter_mentions(
    symbol: str,
    handles: list[str] = None,
) -> dict:
    """
    Scan recent tweets from *handles* for mentions of *symbol* via cashtags.

    Currently disabled (TWITTER_ENABLED=False). Returns an empty result
    immediately without making any network calls or async overhead.
    Set TWITTER_ENABLED=True in config and populate EXPERT_HANDLES with
    Indian expert accounts to re-activate.
    """
    if not TWITTER_ENABLED:
        bare = symbol.split(":")[-1].upper() if ":" in symbol else symbol.upper()
        logger.debug("twitter_enricher: disabled — returning empty result for %s", bare)
        return {
            "mention_count": 0,
            "sentiment":     0.0,
            "tweets":        [],
            "symbol_checked": bare,
            "disabled":       True,
        }

    handles = handles or EXPERT_HANDLES
    bare    = symbol.split(":")[-1].upper() if ":" in symbol else symbol.upper()

    all_mentions: list[dict] = []

    for handle in handles:
        try:
            tweets = _fetch_user_tweets(handle)
        except Exception as exc:
            logger.warning("twitter_enricher: fetch error for @%s — %s", handle, exc)
            continue
        for tw in tweets:
            tickers = _extract_cashtags(tw["text"])
            if bare in tickers:
                sent = _vader_compound(tw["text"])
                all_mentions.append({
                    "tweet_id":  tw["tweet_id"],
                    "text":      tw["text"][:280],
                    "handle":    handle,
                    "date":      tw["date"],
                    "sentiment": round(sent, 4),
                    "likes":     tw["likes"],
                    "retweets":  tw["retweets"],
                })

    mean_sent = (
        sum(m["sentiment"] for m in all_mentions) / len(all_mentions)
        if all_mentions else 0.0
    )

    return {
        "mention_count":  len(all_mentions),
        "sentiment":      round(mean_sent, 4),
        "tweets":         all_mentions,
        "symbol_checked": bare,
    }


def score_twitter(mention_data: dict) -> Optional[float]:
    """
    Convert Twitter mention data to L7c sub-score (0–10) or None if no signal.

    Returns None when TWITTER_ENABLED is False (enricher is disabled) so that
    external_signal_enricher.py treats it as "no signal" rather than 0.
    """
    if not TWITTER_ENABLED or mention_data.get("disabled"):
        return None   # Enricher is disabled — abstain cleanly

    n    = mention_data.get("mention_count", 0)
    sent = mention_data.get("sentiment", 0.0)
    tws  = mention_data.get("tweets", [])

    if n == 0:
        return None   # Abstain — no twitter signal for this symbol

    max_engagement = max((t.get("likes", 0) + t.get("retweets", 0) * 2 for t in tws), default=0)
    viral_bonus    = 1.0 if max_engagement >= 100 else 0.0

    if n >= 3:
        base = 7.0
    elif n == 2:
        base = 6.0
    else:
        base = 4.5

    if sent > 0.35:
        sent_boost = +1.5
    elif sent > 0.10:
        sent_boost = +0.8
    elif sent < -0.35:
        sent_boost = -1.5
    elif sent < -0.10:
        sent_boost = -0.8
    else:
        sent_boost = 0.0

    return round(min(10.0, max(0.0, base + sent_boost + viral_bonus)), 2)


# ─── Account setup helper ────────────────────────────────────────────────────────
def _interactive_setup() -> None:
    """Interactively add a Twitter account to twscrape's pool."""
    print("\n── twscrape Account Setup ───────────────────────────────────────────")
    print("twscrape needs a logged-in Twitter/X session to scrape public tweets.")
    print("You can add an existing account (username + password + email).\n")

    username = input("  Twitter username (without @): ").strip()
    password = input("  Password: ").strip()
    email    = input("  Email (used during login): ").strip()

    async def _add():
        try:
            import twscrape
            api = twscrape.API(str(_ACCOUNTS_DB))
            await api.pool.add_account(username, password, email, password)
            await api.pool.login_all()
            accounts = await api.pool.accounts_info()
            ok = [a for a in accounts if getattr(a, "active", False)]
            print(f"\n  ✓ Added @{username}. Active accounts: {len(ok)}")
        except ImportError:
            print("  ✗ twscrape not installed. pip install twscrape")

    asyncio.run(_add())


# ─── Standalone probe ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    ap = argparse.ArgumentParser(description="Twitter enricher standalone probe")
    ap.add_argument("--symbols", nargs="+", default=["NVDA", "GOLD", "NSE:RELIANCE"],
                    help="Symbols to check")
    ap.add_argument("--setup-accounts", action="store_true",
                    help="Interactively add a Twitter account for twscrape")
    args = ap.parse_args()

    if args.setup_accounts:
        _interactive_setup()
    else:
        print(f"\nTwitter Enricher — checking @{', @'.join(EXPERT_HANDLES)}")
        print(f"Lookback: {LOOKBACK_HOURS}h  |  Max tweets/acc: {MAX_TWEETS_PER_ACC}\n")

        for sym in args.symbols:
            data  = get_twitter_mentions(sym)
            score = score_twitter(data)
            score_str = f"{score:.1f}" if score is not None else "None (abstain)"
            print(f"  {sym:<25}  mentions={data['mention_count']}  "
                  f"sentiment={data['sentiment']:+.3f}  score={score_str}/10")
            for t in data["tweets"][:3]:
                print(f"      @{t['handle']}: {t['text'][:80].strip()}")
        print()
