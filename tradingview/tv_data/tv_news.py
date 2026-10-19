"""
TradingView News API — Reuters, Dow Jones, MarketWatch, and more.

Authenticated via session cookies in tradingview/cookie file.
Verified working as of 2026-04-09 (pro_premium account).

Key APIs:
  GET  news-headlines.tradingview.com/v2/headlines  → headline list
  GET  news-headlines.tradingview.com/v2/story      → full article text
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Config ────────────────────────────────────────────────────────────────────

_TV_NEWS_URL  = "https://news-headlines.tradingview.com/v2/headlines"
_TV_STORY_URL = "https://news-headlines.tradingview.com/v2/story"
_COOKIE_FILE  = Path(__file__).parent.parent / "cookie"

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """Build a requests.Session with TradingView cookies loaded from file."""
    global _session
    if _session is not None:
        return _session

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
        "Referer": "https://www.tradingview.com/",
        "Accept": "application/json",
    })

    cookie_path = _COOKIE_FILE
    if cookie_path.exists():
        cookie_str = cookie_path.read_text(encoding="utf-8").strip()
        for part in cookie_str.split("; "):
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=".tradingview.com")

    _session = s
    return s


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_headlines(
    symbol: Optional[str] = None,
    provider: Optional[str] = None,
    streaming: bool = True,
    limit: int = 200,
    timeout: int = 10,
) -> list[dict]:
    """
    Fetch TradingView news headlines.

    Args:
        symbol:    TradingView symbol, e.g. "NASDAQ:AAPL", "NSE:TCS", "ASX:CBA".
                   When given, returns only news related to that symbol.
        provider:  Filter by news provider.
                   Ranked by signal quality (all verified 2026-04-09):
                   HIGH  : "reuters" (200/200 global, 18/20 symbol-tagged)
                   HIGH  : "dow-jones" (200/200 global, 19/20 tagged)
                   HIGH  : "quartr" (IR reports, earnings summaries, 19/20 tagged, all markets)
                   MED   : "market-watch" (US financial media, 15/20 tagged — US stocks only)
                   MED   : "invezz" (global finance, 16/20 tagged)
                   MED   : "globenewswire" (press releases, 20/20 tagged but lower quality)
                   MED   : "tmx_newsfile" (Canadian TSX press releases)
                   MED   : "trading-economics" (macro data, 17/20 tagged)
                   LOW   : "sharecast" (UK/LSE only), "dpa_afx" (German/XETRA, 6/20),
                           "zawya" (MENA, 3/20), "acceswire" (US press releases),
                           "thenewswire" (Canadian), "modular_finance" (Nordic),
                           "stocktwits" (social, 20/20 tagged but noisy)
                   None  : all providers.
        streaming: True → broader category feed. False → same result size.
        limit:     Not enforced server-side (always returns ~200). Slice here.
        timeout:   HTTP timeout in seconds.

    Returns:
        List of headline dicts with keys:
            id, title, provider, source, published (unix), storyPath,
            relatedSymbols (list of {symbol, logoid}).
    """
    params: dict[str, str] = {"client": "web", "lang": "en"}
    if symbol:
        params["symbol"] = symbol
    if provider:
        params["provider"] = provider
    if streaming:
        params["streaming"] = "true"

    s = _get_session()
    try:
        r = s.get(_TV_NEWS_URL, params=params, timeout=timeout)
        r.raise_for_status()
        items = r.json().get("items", [])
        return items[:limit]
    except Exception:
        return []


def fetch_story(story_id: str, timeout: int = 10) -> dict:
    """
    Fetch the full text of a TradingView news article.

    Args:
        story_id: The article id from fetch_headlines(), e.g.
                  "tag:reuters.com,2026:newsml_L4N40S0Q0:0"

    Returns:
        dict with keys:
            shortDescription (str): ~300-char plain text excerpt.
            astDescription (dict):  Full article as AST tree.
            full_text (str):        Extracted plain text from AST (added by us).
        Empty dict on failure.
    """
    params = {
        "client": "web",
        "lang": "en",
        "id": story_id,   # requests handles URL-encoding via params dict
    }
    s = _get_session()
    try:
        r = s.get(_TV_STORY_URL, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        # Extract plain text from AST
        data["full_text"] = _ast_to_text(data.get("astDescription", {}))
        return data
    except Exception:
        return {}


def fetch_story_snippets(items: list[dict], n: int = 3, timeout: int = 8) -> list[str]:
    """
    Fetch story body text (shortDescription) for the top N headline items.

    Args:
        items:   List of headline dicts from fetch_headlines() (must have "id" key).
        n:       Max number of articles to fetch bodies for.
        timeout: HTTP timeout per request.

    Returns:
        List of shortDescription strings (~300 chars each), skipping empties.
        These are much richer than headlines alone for VADER sentiment scoring.
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [it for it in items[:n] if it.get("id")]
    if not targets:
        return []

    def _fetch_one(it: dict) -> str:
        s = fetch_story(it["id"], timeout=timeout)
        return s.get("shortDescription", "").strip()

    snippets: list[str] = []
    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
        futs = [pool.submit(_fetch_one, it) for it in targets]
        for f in futs:
            try:
                desc = f.result(timeout=timeout + 2)
                if desc:
                    snippets.append(desc)
            except Exception:
                pass
    return snippets


def fetch_symbol_news(
    tv_symbol: str,
    providers: Optional[list[str]] = None,
    limit: int = 20,
) -> list[str]:
    """
    Fetch news headlines for a symbol as plain strings (for VADER sentiment).

    Args:
        tv_symbol: TradingView symbol, e.g. "NASDAQ:AAPL".
        providers: List of providers to try, e.g. ["reuters", "dow-jones"].
                   If None, fetches from all providers.
        limit:     Max headlines to return per provider.

    Returns:
        List of headline strings, deduplicated.
    """
    seen: set[str] = set()
    result: list[str] = []

    if providers:
        for prov in providers:
            items = fetch_headlines(symbol=tv_symbol, provider=prov, limit=limit)
            for it in items:
                title = it.get("title", "").strip()
                if title and title not in seen:
                    seen.add(title)
                    result.append(title)
                    if len(result) >= limit * len(providers):
                        return result
    else:
        items = fetch_headlines(symbol=tv_symbol, limit=limit)
        for it in items:
            title = it.get("title", "").strip()
            if title and title not in seen:
                seen.add(title)
                result.append(title)

    return result


def fetch_reuters_headlines(limit: int = 50) -> list[dict]:
    """Convenience: fetch the latest global Reuters headlines."""
    return fetch_headlines(provider="reuters", limit=limit)


def fetch_reuters_for_symbol(tv_symbol: str, limit: int = 20) -> list[dict]:
    """Convenience: fetch Reuters + Dow Jones news for a specific symbol."""
    items: list[dict] = []
    seen_ids: set[str] = set()
    for prov in ("reuters", "dow-jones"):
        for it in fetch_headlines(symbol=tv_symbol, provider=prov, limit=limit):
            if it.get("id") not in seen_ids:
                seen_ids.add(it.get("id", ""))
                items.append(it)
    return items[:limit]


def news_to_headlines(items: list[dict]) -> list[str]:
    """Extract title strings from a list of headline dicts."""
    return [it["title"] for it in items if it.get("title")]


def fetch_symbol_news_items(
    tv_symbol: str,
    providers: Optional[list[str]] = None,
    limit: int = 20,
) -> list[dict]:
    """Like :func:`fetch_symbol_news` but returns the raw headline dicts.

    Preserves ``id``, ``title``, ``provider``, ``source``, and ``published``
    (unix timestamp) so callers can compute article velocity. Deduplicated by
    article id (falling back to title when no id is present).
    """
    seen: set[str] = set()
    result: list[dict] = []

    def _add(items):
        for it in items:
            key = str(it.get("id") or it.get("title", "")).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(it)

    if providers:
        for prov in providers:
            _add(fetch_headlines(symbol=tv_symbol, provider=prov, limit=limit))
    else:
        _add(fetch_headlines(symbol=tv_symbol, limit=limit))
    return result


def article_published_ts(item: dict, now=None):
    """Return a timezone-aware UTC datetime for an article, or ``None``.

    Accepts a unix timestamp (int/float) or an ISO-8601 string. Missing,
    invalid, or future timestamps return ``None`` so they never inflate a
    recency count. ``now`` is the reference clock (defaults to the current UTC
    time); a timestamp later than ``now`` is rejected, so callers that supply
    a reference time are not compared against real wall time
    (review 2026-10-13, Issue 6 follow-up).
    """
    if not isinstance(item, dict):
        return None
    raw = item.get("published")
    if raw is None:
        raw = item.get("publishedAt") or item.get("date")
    if raw is None:
        return None
    dt = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.fromtimestamp(float(s), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if dt > now:
        return None   # future timestamps are not "recent"
    return dt


def article_velocity(items: list[dict], now=None, window_hours: int = 24) -> int:
    """Count distinct articles published within the trailing window.

    ``now`` is a timezone-aware reference time (defaults to the current UTC
    time) so tests can control the clock. Articles without a usable timestamp
    are excluded — they keep their sentiment role but do not count as recent.
    Multiple representations of the same article (same id, or same title when
    no id) count once.
    """
    if not items:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    seen: set[str] = set()
    count = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        key = str(it.get("id") or it.get("title", "")).strip()
        if not key or key in seen:
            continue
        ts = article_published_ts(it, now=now)
        if ts is None:
            continue
        if cutoff <= ts <= now:
            seen.add(key)
            count += 1
    return count


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ast_to_text(node) -> str:
    """Recursively extract plain text from TradingView AST article structure."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(_ast_to_text(n) for n in node if n).strip()
    if isinstance(node, dict):
        children = node.get("children", [])
        return " ".join(_ast_to_text(c) for c in children if c).strip()
    return ""


# ── CLI self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else None

    if symbol:
        print(f"\n=== Reuters + Dow Jones news for: {symbol} ===")
        items = fetch_reuters_for_symbol(symbol, limit=10)
    else:
        print("\n=== Latest Reuters headlines (global) ===")
        items = fetch_reuters_headlines(limit=10)

    if not items:
        print("No items returned — check cookie file or network.")
        sys.exit(1)

    for i, it in enumerate(items, 1):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("published", 0)))
        syms = [s["symbol"] for s in it.get("relatedSymbols", [])]
        print(f"\n[{i}] {it['title']}")
        print(f"     Source: {it.get('source')} | {ts}")
        if syms:
            print(f"     Symbols: {', '.join(syms)}")
        if it.get("storyPath"):
            print(f"     Path: {it['storyPath']}")

    # Demo: fetch full story text for first item
    if items and items[0].get("id"):
        print("\n=== Full text (first article) ===")
        story = fetch_story(items[0]["id"])
        short = story.get("shortDescription", "")
        full  = story.get("full_text", "")
        print("Short:", short[:300])
        print("Full text chars:", len(full))
        if full:
            print("Preview:", full[:400])
