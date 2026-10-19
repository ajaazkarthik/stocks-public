"""
moomoo_community_intel.py
==========================
Moomoo Community Intelligence Engine — turns raw community posts into
actionable stock signals with VADER sentiment, ticker hotness ranking,
and optional cookie-authenticated access to personalised following feeds.

CAPABILITIES:
  1. Public feed   — fetch latest community posts (no login needed)
  2. VADER scoring — sentiment score per post (-1 bearish → +1 bullish)
  3. Hot Stocks    — aggregate by ticker: mentions × sentiment → ranked table
  4. Cookie auth   — inject moomoo_cookie → access /community/following feed
  5. API probe     — intercept ALL XHR to discover authenticated endpoints
  6. HTML report   — comprehensive intelligence report

COOKIE SETUP (one-time):
  1. Log in at www.moomoo.com/au  in your browser
  2. DevTools → Application → Cookies → www.moomoo.com
  3. Copy ALL cookies as a single semicolon-separated line into:
       d:/workspace/repo/stocks/moomoo_cookie
  Minimum required cookies:  futu_user_info  OR  user_token  OR  _auth

USAGE:
  python moomoo_community_intel.py                          # public feed only
  python moomoo_community_intel.py --auth                   # + following feed
  python moomoo_community_intel.py --auth --n 50            # 50 posts
  python moomoo_community_intel.py --out reports/mm.html    # save HTML
  python moomoo_community_intel.py --auth --probe           # discover API endpoints
"""

import sys, os, re, json, html, datetime, textwrap, argparse, time
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
COOKIE_FILE = ROOT / "moomoo_cookie"
MOOMOO_BASE = "https://www.moomoo.com"
COMMUNITY_NNQ      = f"{MOOMOO_BASE}/community/nnq"
COMMUNITY_FOLLOWING = f"{MOOMOO_BASE}/community/following"
COMMUNITY_HOT      = f"{MOOMOO_BASE}/community/nnq/topic-square"

# Moomoo Insights official profile (user ID 71146480)
INSIGHTS_USER_ID    = "71146480"
INSIGHTS_PROFILE_URL = f"{MOOMOO_BASE}/community/profile/moomoo-insights-{INSIGHTS_USER_ID}"


# ── VADER sentiment ────────────────────────────────────────────────────────────

def _get_vader():
    """Lazily load VADER analyser. Graceful degradation if not installed."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        return SentimentIntensityAnalyzer()
    except ImportError:
        return None


def score_post(text: str, vader) -> float:
    """Return VADER compound score [-1, +1]. Returns 0 if VADER not available."""
    if not vader or not text:
        return 0.0
    return vader.polarity_scores(text)["compound"]


# ── Cookie utilities ───────────────────────────────────────────────────────────

def load_moomoo_cookies() -> list[dict]:
    """
    Read the moomoo_cookie file (single line of semicolon-separated key=value)
    and return a list of Playwright cookie dicts.
    Returns [] if file not found.
    """
    if not COOKIE_FILE.exists():
        return []
    raw = COOKIE_FILE.read_text(encoding="utf-8", errors="replace").strip()
    # Strip any URL line at bottom (like tradingview/cookie has)
    # Skip standalone URL lines (lines that START with http:// or https://)
    # Do NOT skip cookie lines that merely contain URLs in their values
    raw_lines = [l for l in raw.splitlines() if l.strip() and not re.match(r'https?://', l.strip())]
    if not raw_lines:
        return []
    cookie_str = raw_lines[0]
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        cookies.append({
            "name": k.strip(),
            "value": v.strip(),
            "domain": ".moomoo.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
        })
    return cookies


def _cookies_available() -> bool:
    cookies = load_moomoo_cookies()
    # Check if any auth-related cookie is present
    auth_keys = {"futu_user_info", "user_token", "_auth", "futu_token",
                 "futu_uid", "u", "auth_token", "accessToken", "jwt"}
    found_keys = {c["name"] for c in cookies}
    has_auth = bool(auth_keys & found_keys)
    # Also accept if we have at least 3 cookies (session is likely present)
    return has_auth or len(cookies) >= 3


# ── Page fetch via Playwright ──────────────────────────────────────────────────

def _make_browser_ctx(pw, cookies: list[dict]):
    """Create a Playwright browser context with optional cookies injected."""
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-AU",
        timezone_id="Australia/Sydney",
        viewport={"width": 1280, "height": 800},
    )
    if cookies:
        # FIX 2026-04-25: sameSite must be "Lax" (not "None").
        # "None" prevents JavaScript from executing on the page — JS skips
        # cookie-setup entirely when SameSite=None without proper CORS context.
        enriched: list[dict] = [
            dict(c, domain=".moomoo.com", sameSite="Lax", secure=True)
            for c in cookies
        ]
        try:
            ctx.add_cookies(enriched)
        except Exception:
            ctx.add_cookies(cookies)  # Fallback: add without modifications
    return browser, ctx


def _goto_wait(page, url: str, timeout: int = 35_000):
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout)
    except Exception:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception:
            pass
    page.wait_for_timeout(3500)


def _extract_posts_from_dom(page, n: int, base_url: str) -> list[dict]:
    """Extract posts via Moomoo's Vue.js DOM structure (confirmed selectors)."""
    posts = []
    seen: set[str] = set()
    wrappers = page.query_selector_all("div[data-rep-key]")
    for wrapper in wrappers[:n]:
        try:
            feed_id = wrapper.get_attribute("data-rep-key") or wrapper.get_attribute("feed-id") or ""
            if not feed_id or feed_id in seen:
                continue
            seen.add(feed_id)
            post_url = f"{MOOMOO_BASE}/community/feed/{feed_id}"

            # Body text
            body_el = (wrapper.query_selector('[data-feed-type="rich-text"]')
                       or wrapper.query_selector('[data-feed-type="wrapper"]'))
            raw_text = body_el.inner_text().strip() if body_el else wrapper.inner_text().strip()
            cleaned = _clean(raw_text)
            if not cleaned or len(cleaned) < 10:
                continue

            # Author
            author = "Community"
            try:
                a_el = wrapper.evaluate_handle(
                    "el => el.closest('.nnq-list-item-content')?.parentElement?.querySelector('.username a')"
                    " || el.closest('.nnq-list-item-content')?.previousElementSibling?.querySelector('a[href*=\"/community/profile/\"]')"
                )
                if a_el and a_el.as_element():
                    author = (a_el.as_element().inner_text() or "Community").strip()
            except Exception:
                pass

            # Timestamp
            ts = "—"
            try:
                ts_el = wrapper.evaluate_handle(
                    "el => el.closest('.nnq-list-item-content')?.previousElementSibling?.querySelector('.publish-time-text')"
                )
                if ts_el and ts_el.as_element():
                    ts = (ts_el.as_element().inner_text() or "—").strip()
            except Exception:
                pass

            # Tickers from $TICKER-US$ format and stock page links
            # Filter out options contracts: pattern like TSLA260424C370000-US
            raw_tickers = re.findall(r"\$([A-Z]{1,6}(?:\.[A-Z]{2})?)\$", raw_text)
            tickers = [t for t in raw_tickers if not re.match(r'^[A-Z]{1,6}\d{6}[CP]\d+', t)]
            for sl in wrapper.query_selector_all("a[href*='/stock/']")[:6]:
                href = sl.get_attribute("href") or ""
                m = re.search(r"/stock/([A-Z0-9\.\-]+)", href)
                sym = m.group(1) if m else ""
                # Skip options-like symbols (e.g. TSLA260424C370000-US)
                if sym and sym not in tickers and not re.match(r'^[A-Z]{1,6}\d{6}[CP]\d+', sym):
                    tickers.append(sym)

            posts.append({
                "author":   author,
                "official": any(o in author for o in ["Moomoo", "Insights", "Jessica", "Tina", "Greg"]),
                "time":     ts,
                "text":     cleaned,
                "url":      post_url,
                "tickers":  tickers,
                "source":   base_url,
            })
        except Exception:
            pass
    return posts


def _clean(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return textwrap.shorten(text, width=300, placeholder=" …")


# ── Main fetch functions ───────────────────────────────────────────────────────

def fetch_public_feed(n: int = 30) -> list[dict]:
    """Fetch public community posts from /community/nnq (no auth needed)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] playwright not installed. Run: pip install playwright && playwright install chromium")
        return []

    captured_xhr: list[tuple[str, str]] = []

    def on_response(resp):
        rurl = resp.url
        if any(rurl.endswith(e) for e in [".js", ".css", ".png", ".svg", ".ico", ".woff", ".woff2"]):
            return
        if "json" not in resp.headers.get("content-type", ""):
            return
        try:
            body = resp.text()
            if any(k in body for k in ['"feed_id"', '"feedId"', '"feedList"', '"nickName"', '"userId"']):
                captured_xhr.append((rurl, body))
        except Exception:
            pass

    posts: list[dict] = []
    with sync_playwright() as pw:
        browser, ctx = _make_browser_ctx(pw, [])
        page = ctx.new_page()
        page.on("response", on_response)
        _goto_wait(page, COMMUNITY_NNQ)

        # Try XHR first
        for rurl, body in captured_xhr:
            try:
                data = json.loads(body)
                for path_fn in [
                    lambda d: d.get("data", {}).get("list"),
                    lambda d: d.get("result", {}).get("list"),
                    lambda d: d.get("list"),
                ]:
                    try:
                        items = path_fn(data)
                        if isinstance(items, list) and items:
                            from moomoo_community_scraper import _normalise_item
                            for item in items[:n]:
                                try:
                                    p = _normalise_item(item)
                                    p["source"] = "public"
                                    if p["text"]:
                                        posts.append(p)
                                except Exception:
                                    pass
                            break
                    except Exception:
                        pass
            except Exception:
                pass

        # DOM fallback
        if len(posts) < 5:
            dom_posts = _extract_posts_from_dom(page, n, "public")
            posts.extend(dom_posts)

        ctx.close()
        browser.close()
    result = posts[:n]
    if result:
        save_community_cache(result)
    return result


# ── Cache layer for pipeline integration ──────────────────────────────────────
_COMMUNITY_CACHE = ROOT / ".cache_moomoo_community.json"
_CACHE_MAX_AGE_SECS = 4 * 3600  # 4 hours

# Keyed by (cache mtime, max_age_secs) so a refreshed cache invalidates it.
_HEADLINES_MEMO: dict = {}


def save_community_cache(posts: list[dict]) -> None:
    """Save fetched community posts to JSON cache for pipeline consumption."""
    payload = {
        "ts": time.time(),
        "count": len(posts),
        "posts": posts,
    }
    _COMMUNITY_CACHE.write_text(json.dumps(payload, default=str), encoding="utf-8")


def load_community_cache(max_age_secs: float = _CACHE_MAX_AGE_SECS) -> list[dict]:
    """Load cached community posts if fresh enough. Returns [] if stale/missing."""
    if not _COMMUNITY_CACHE.exists():
        return []
    try:
        data = json.loads(_COMMUNITY_CACHE.read_text(encoding="utf-8"))
        age = time.time() - data.get("ts", 0)
        if age > max_age_secs:
            return []
        return data.get("posts", [])
    except Exception:
        return []


def get_community_headlines(max_age_secs: float = _CACHE_MAX_AGE_SECS) -> list[str]:
    """Return headline strings from live or cached moomoo community posts.

    Auto-refreshes via Playwright when cache is stale/missing (public feed, no
    auth needed). Falls back to stale cache if Playwright is unavailable.
    Called by signal_stack.py and tv_news_screener.py.

    Ticker metadata from each post's ``tickers`` field is prepended as
    ``$SYMBOL$`` tags so that downstream regex (both in tv_news_screener.py
    and signal_stack.py L3 Tier 4) can reliably match the stock symbol even
    when the post title only uses the company name ("Amazon…" not "AMZN").

    Example output:
        "$AMZN$ Amazon Earnings Preview: Investors Bet on Anthropic…"
    """
    # Called once per symbol by signal_stack; without this memo a 130-symbol run
    # re-reads and re-parses the cache file 130 times, and a failed Playwright
    # refresh is retried 130 times because nothing gets written to change it.
    try:
        _stamp = _COMMUNITY_CACHE.stat().st_mtime
    except OSError:
        _stamp = None
    _memo_key = (_stamp, max_age_secs)
    if _memo_key in _HEADLINES_MEMO:
        return _HEADLINES_MEMO[_memo_key]

    posts = load_community_cache(max_age_secs)
    if not posts:
        # Cache stale or missing — try live fetch via Playwright (public feed)
        try:
            fresh = fetch_public_feed(n=30)
            if fresh:
                posts = fresh
        except Exception:
            # Playwright not available or network error — use stale cache as fallback
            posts = load_community_cache(max_age_secs=float("inf"))

    headlines: list[str] = []
    for p in posts:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        # Take first line or first 200 chars as headline
        first_line = text.split("\n")[0][:200]

        # ── BUG11 fix: prepend structured $TICKER$ tags from post metadata ──
        # Moomoo posts carry a "tickers" list (e.g. ["AMZN-US", "GOOGL-US"]).
        # The post title often uses the company name ("Amazon …"), NOT the
        # bare symbol, so the regex can't find "AMZN" in plain text.
        # Prepending "$AMZN$" makes both tv_news_screener.py and
        # signal_stack.py L3 find the symbol correctly.
        raw_tickers = p.get("tickers") or []
        ticker_tags: list[str] = []
        for t in raw_tickers:
            # Normalise: "AMZN-US" → "AMZN", "AMZN.US" → "AMZN", "AMZN" → "AMZN"
            bare = re.sub(r'[\.\-][A-Z]{2}$', '', str(t).strip().upper())
            if bare and 1 <= len(bare) <= 6 and bare.isalpha():
                ticker_tags.append(f"${bare}$")

        if ticker_tags:
            headline = " ".join(ticker_tags) + " " + first_line
        else:
            headline = first_line
        headlines.append(headline)

    _HEADLINES_MEMO[_memo_key] = headlines
    return headlines


# ── Moomoo Insights dedicated feed ────────────────────────────────────────────
# Fetches posts ONLY from the official Moomoo Insights profile (ID 71146480).
# No auth required — the profile is public.

_INSIGHTS_CACHE = ROOT / ".cache_moomoo_insights.json"
_INSIGHTS_CACHE_MAX_AGE_SECS = 4 * 3600  # 4 hours


def save_insights_cache(posts: list[dict]) -> None:
    """Persist Moomoo Insights posts to a dedicated JSON cache."""
    payload = {
        "ts": time.time(),
        "count": len(posts),
        "source": "moomoo_insights",
        "user_id": INSIGHTS_USER_ID,
        "posts": posts,
    }
    _INSIGHTS_CACHE.write_text(json.dumps(payload, default=str), encoding="utf-8")


def load_insights_cache(max_age_secs: float = _INSIGHTS_CACHE_MAX_AGE_SECS) -> list[dict]:
    """Load cached Insights posts if fresh enough. Returns [] if stale/missing."""
    if not _INSIGHTS_CACHE.exists():
        return []
    try:
        data = json.loads(_INSIGHTS_CACHE.read_text(encoding="utf-8"))
        age = time.time() - data.get("ts", 0)
        if age > max_age_secs:
            return []
        return data.get("posts", [])
    except Exception:
        return []


def fetch_insights_feed(n: int = 20) -> list[dict]:
    """
    Fetch posts specifically from the Moomoo Insights profile page using Playwright.

    Targets: https://www.moomoo.com/community/profile/moomoo-insights-71146480

    No authentication required (public profile).  Uses XHR intercept first,
    then DOM parse fallback — same strategy as fetch_public_feed().

    Returns:
        list[dict] with keys: author, official, time, text, url, tickers, source.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] playwright not installed. Run: pip install playwright && playwright install chromium")
        return []

    captured_xhr: list[tuple[str, str]] = []

    def on_response(resp):
        rurl = resp.url
        if any(rurl.endswith(e) for e in [".js", ".css", ".png", ".svg", ".ico", ".woff", ".woff2"]):
            return
        if "json" not in resp.headers.get("content-type", ""):
            return
        try:
            body = resp.text()
            # Look for post-list payloads — Insights profile XHR carries feedList or similar
            if any(k in body for k in ['"feed_id"', '"feedId"', '"feedList"', '"nickName"',
                                        '"userId"', '"postContent"', '"content"']):
                captured_xhr.append((rurl, body))
        except Exception:
            pass

    posts: list[dict] = []
    with sync_playwright() as pw:
        browser, ctx = _make_browser_ctx(pw, [])  # no cookies needed for public profile
        page = ctx.new_page()
        page.on("response", on_response)
        _goto_wait(page, INSIGHTS_PROFILE_URL)

        # ── Strategy 1: XHR intercept ─────────────────────────────────────
        for rurl, body in captured_xhr:
            try:
                data = json.loads(body)
                for path_fn in [
                    lambda d: d.get("data", {}).get("list"),
                    lambda d: d.get("data", {}).get("feedList"),
                    lambda d: d.get("result", {}).get("list"),
                    lambda d: d.get("list"),
                    lambda d: d.get("data"),
                ]:
                    try:
                        items = path_fn(data)
                        if isinstance(items, list) and items:
                            from moomoo_community_scraper import _normalise_item
                            for item in items[:n]:
                                try:
                                    p = _normalise_item(item)
                                    p["source"] = "insights"
                                    p["official"] = True
                                    if p["text"]:
                                        posts.append(p)
                                except Exception:
                                    pass
                            if posts:
                                break
                    except Exception:
                        pass
                if posts:
                    break
            except Exception:
                pass

        # ── Strategy 2: DOM parse fallback ───────────────────────────────
        if len(posts) < 3:
            dom_posts = _extract_posts_from_dom(page, n, INSIGHTS_PROFILE_URL)
            for p in dom_posts:
                p["source"] = "insights"
                p["official"] = True
            posts.extend(dom_posts)

        ctx.close()
        browser.close()

    result = posts[:n]
    if result:
        save_insights_cache(result)
    return result


def get_insights_headlines(max_age_secs: float = _INSIGHTS_CACHE_MAX_AGE_SECS) -> list[str]:
    """
    Return headline strings from the Moomoo Insights profile feed.

    Auto-refreshes via Playwright when cache is stale/missing (public profile,
    no auth needed). Falls back to stale cache if Playwright is unavailable.

    Ticker metadata from each post's ``tickers`` field is prepended as
    ``$SYMBOL$`` tags so signal_stack.py L3 can match the symbol.

    Example output:
        "$AAPL$ Apple Reports Q2 Earnings Beat — Moomoo Insights"
    """
    posts = load_insights_cache(max_age_secs)
    if not posts:
        try:
            fresh = fetch_insights_feed(n=20)
            if fresh:
                posts = fresh
        except Exception:
            posts = load_insights_cache(max_age_secs=float("inf"))

    headlines: list[str] = []
    for p in posts:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        first_line = text.split("\n")[0][:200]

        raw_tickers = p.get("tickers") or []
        ticker_tags: list[str] = []
        for t in raw_tickers:
            bare = re.sub(r'[\.\-][A-Z]{2}$', '', str(t).strip().upper())
            if bare and 1 <= len(bare) <= 6 and bare.isalpha():
                ticker_tags.append(f"${bare}$")

        headline = (" ".join(ticker_tags) + " " + first_line).strip() if ticker_tags else first_line
        headlines.append(headline)
    return headlines


def insights_to_fires(
    bullish_threshold: float = 0.15,
    bearish_threshold: float = -0.15,
    max_age_secs: float = _INSIGHTS_CACHE_MAX_AGE_SECS,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch Moomoo Insights posts and convert ticker-tagged ones into fire dicts
    for the 4-layer scoring pipeline.

    Returns:
        (bullish_fires, bearish_fires) — lists of fire dicts.
        Each fire has ``_source = "moomoo_insights"`` and ``kinds = ["moomoo_insights"]``.
        Only posts with at least one recognised ticker produce fires.
    """
    posts = load_insights_cache(max_age_secs)
    if not posts:
        try:
            posts = fetch_insights_feed(n=20)
        except Exception:
            posts = load_insights_cache(max_age_secs=float("inf"))

    vader = _get_vader()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    bullish_fires: list[dict] = []
    bearish_fires: list[dict] = []
    seen_symbols: set[str] = set()

    for post in posts:
        raw_tickers = post.get("tickers") or []
        if not raw_tickers:
            continue

        text = (post.get("text") or "").strip()
        score = score_post(text, vader)

        for raw_t in raw_tickers:
            bare = re.sub(r'[\.\-][A-Z]{2}$', '', str(raw_t).strip().upper())
            if not bare or not (1 <= len(bare) <= 6) or not bare.isalpha():
                continue

            # Resolve exchange suffix from the raw moomoo ticker format
            # e.g. "AMZN-US" → try NASDAQ:AMZN; "BHP-AU" → try ASX:BHP
            suffix = raw_t.split("-")[-1].upper() if "-" in raw_t else ""
            _SUFFIX_MAP = {"US": "NASDAQ", "AU": "ASX", "HK": "HKEX", "SG": "SGX",
                           "IN": "NSE", "UK": "LSE"}
            exch = _SUFFIX_MAP.get(suffix, "")
            pro_symbol = f"{exch}:{bare}" if exch else bare

            if pro_symbol in seen_symbols:
                continue
            seen_symbols.add(pro_symbol)

            direction = "BUY" if score >= 0 else "BEAR"
            message = (
                f"MOOMOO_INSIGHTS_{direction}:1, sentiment:{score:+.3f}, "
                f"source:moomoo_insights, author:{post.get('author', 'Moomoo Insights')}"
            )

            fire: dict = {
                "fire_id":        int(time.time() * 1000),
                "alert_id":       0,
                "symbol":         pro_symbol,
                "pro_symbol":     pro_symbol,
                "resolution":     "D",
                "sound_file":     None,
                "sound_duration": 0,
                "popup":          False,
                "cross_interval": False,
                "message":        message,
                "fire_time":      now_iso,
                "bar_time":       now_iso,
                "name":           f"insights_{pro_symbol}",
                "kinds":          ["moomoo_insights"],
                "close":          None,
                "_source":        "moomoo_insights",
                "_bearish":       score < bearish_threshold,
                "_insights_compound":  score,
                "_insights_text":      text[:300],
                "_insights_url":       post.get("url", ""),
                "_insights_author":    post.get("author", "Moomoo Insights"),
            }

            if score >= bullish_threshold:
                bullish_fires.append(fire)
            elif score <= bearish_threshold:
                bearish_fires.append(fire)

    return bullish_fires, bearish_fires


def fetch_following_ids(cookies: list[dict] | None = None) -> list[str]:
    """
    Fetch the list of user IDs the authenticated user is following.
    Uses Moomoo REST endpoint: /community/sns-api/get-user-watch-ids
    Returns a list of user ID strings. Returns [] if auth unavailable.
    """
    if cookies is None:
        cookies = load_moomoo_cookies()
    if not cookies:
        return []
    try:
        import requests
    except ImportError:
        return []

    cd = {c["name"]: c["value"] for c in cookies}
    uid  = cd.get("uid", "")
    csrf = cd.get("csrfToken", "")
    if not uid:
        return []

    sess = requests.Session()
    sess.cookies.update(cd)
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.moomoo.com/community/nnq",
        "X-CSRFToken": csrf,
    })
    try:
        ts = int(time.time() * 1000)
        r = sess.get(
            f"{MOOMOO_BASE}/community/sns-api/get-user-watch-ids",
            params={"uid": uid, "_": ts},
            timeout=10,
        )
        data = r.json()
        ids = data.get("data", [])
        if isinstance(ids, list):
            return [str(i) for i in ids]
    except Exception as e:
        print(f"[WARN] fetch_following_ids failed: {e}")
    return []


def fetch_following_feed(n: int = 30, cookies: list[dict] | None = None) -> list[dict]:
    """
    Fetch personalised following feed from /community/following.
    Requires valid Moomoo cookies.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    if cookies is None:
        cookies = load_moomoo_cookies()
    if not cookies:
        print("[WARN] No moomoo_cookie file found. Following feed unavailable.")
        print(f"       Create {COOKIE_FILE} with your browser cookies to enable.")
        return []

    posts: list[dict] = []
    captured_xhr: list[tuple[str, str]] = []

    def on_response(resp):
        rurl = resp.url
        if any(rurl.endswith(e) for e in [".js", ".css", ".png", ".svg", ".ico", ".woff", ".woff2"]):
            return
        if "json" not in resp.headers.get("content-type", ""):
            return
        try:
            body = resp.text()
            if any(k in body for k in ['"feed_id"', '"feedId"', '"feedList"', '"nickName"', '"userId"']):
                captured_xhr.append((rurl, body))
        except Exception:
            pass

    with sync_playwright() as pw:
        browser, ctx = _make_browser_ctx(pw, cookies)
        page = ctx.new_page()
        page.on("response", on_response)

        # Strategy: navigate to community NNQ first (has tab bar with Following button)
        # Moomoo is a Vue.js SPA — direct nav to /following may fail; tab-click is reliable
        _goto_wait(page, COMMUNITY_NNQ)
        current_url = page.url

        # Verify login worked (logged-in page shows user avatar / profile link)
        page_text = page.inner_text("body") if page.query_selector("body") else ""
        is_logged_in = bool(
            page.query_selector("a[href*='/community/profile/me']") or
            page.query_selector(".header-user-avatar") or
            page.query_selector(".user-avatar") or
            page.query_selector("[class*='avatar']") or
            page.query_selector(".nnq-header-login-info") or
            # uid is in the cookie — if page has our uid or nickname it's logged in
            "logout" in page_text.lower() or
            "profile" in current_url
        )

        if "login" in current_url or "sign-in" in current_url:
            print(f"[WARN] Cookie auth failed — page redirected to: {current_url}")
            print("       Refresh moomoo_cookie from browser DevTools → Application → Cookies")
            ctx.close()
            browser.close()
            return []

        print(f"  Landed on: {current_url}  | Logged-in signals: {is_logged_in}")

        # Try to click the 'Following' tab in the community tab bar
        followed_tab_clicked = False
        for selector in [
            "a[href*='/community/following']",
            "div[class*='tab']:has-text('Following')",
            "span:has-text('Following')",
            "[data-key='following']",
            ".nnq-tabs a:nth-child(2)",
        ]:
            try:
                el = page.query_selector(selector)
                if el:
                    el.click()
                    page.wait_for_timeout(3500)
                    followed_tab_clicked = True
                    print(f"  Clicked 'Following' tab via: {selector}")
                    break
            except Exception:
                pass

        if not followed_tab_clicked:
            # Fallback: navigate directly
            _goto_wait(page, COMMUNITY_FOLLOWING)
            print(f"  Direct nav to /community/following → {page.url}")

        # XHR first
        for rurl, body in captured_xhr:
            try:
                data = json.loads(body)
                for path_fn in [
                    lambda d: d.get("data", {}).get("list"),
                    lambda d: d.get("result", {}).get("list"),
                    lambda d: d.get("list"),
                ]:
                    try:
                        items = path_fn(data)
                        if isinstance(items, list) and items:
                            from moomoo_community_scraper import _normalise_item
                            for item in items[:n]:
                                try:
                                    p = _normalise_item(item)
                                    p["source"] = "following"
                                    if p["text"]:
                                        posts.append(p)
                                except Exception:
                                    pass
                            break
                    except Exception:
                        pass
            except Exception:
                pass
            dom_posts = _extract_posts_from_dom(page, n, "following")
            for p in dom_posts:
                p["source"] = "following"
            posts.extend(dom_posts)

        ctx.close()
        browser.close()
    return posts[:n]


def probe_authenticated_endpoints(cookies: list[dict] | None = None) -> list[dict]:
    """
    Navigate multiple Moomoo pages with auth and intercept ALL JSON API calls.
    Returns list of {url, method, response_keys} for discovered endpoints.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    if cookies is None:
        cookies = load_moomoo_cookies()
    if not cookies:
        return []

    discovered: dict[str, dict] = {}  # url_pattern → info

    PROBE_URLS = [
        (COMMUNITY_NNQ,           "Public feed"),
        (COMMUNITY_FOLLOWING,     "Following feed"),
        (COMMUNITY_HOT,           "Hot/Trending topics"),
        (f"{MOOMOO_BASE}/community/profile/me",   "My Profile"),
        (f"{MOOMOO_BASE}/community/notifications", "Notifications"),
        (f"{MOOMOO_BASE}/watchlist",               "Watchlist"),
        (f"{MOOMOO_BASE}/quotes/us",               "US Quotes"),
        (f"{MOOMOO_BASE}/quotes/au",               "AU Quotes"),
    ]

    def on_response(resp):
        try:
            rurl = resp.url
            # Skip static assets
            if any(ext in rurl for ext in [".js", ".css", ".png", ".svg", ".ico", ".woff",
                                            "fonts.", "static.", "cdn.", "google"]):
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = resp.text()
            if len(body) < 20:
                return
            try:
                data = json.loads(body)
            except Exception:
                return
            # Extract top-level keys as capability hints
            if isinstance(data, dict):
                top_keys = list(data.keys())[:8]
                # Normalise URL to pattern (strip query params for grouping)
                path = re.sub(r"\?.*", "", rurl.replace(MOOMOO_BASE, ""))
                path = re.sub(r"/\d{10,}", "/{id}", path)  # redact numeric IDs
                if path not in discovered and len(path) > 3:
                    discovered[path] = {
                        "url": rurl[:120],
                        "path": path,
                        "keys": top_keys,
                        "size": len(body),
                        "has_data": "data" in data or "result" in data or "list" in data,
                    }
        except Exception:
            pass

    with sync_playwright() as pw:
        browser, ctx = _make_browser_ctx(pw, cookies)
        page = ctx.new_page()
        page.on("response", on_response)

        for url, label in PROBE_URLS:
            try:
                print(f"  Probing: {label}  ({url.replace(MOOMOO_BASE, '')})")
                _goto_wait(page, url, timeout=20_000)
            except Exception:
                pass

        ctx.close()
        browser.close()

    return sorted(discovered.values(), key=lambda x: -x["size"])


# ── Intelligence analysis ──────────────────────────────────────────────────────

def analyse_posts(posts: list[dict], vader=None) -> dict:
    """
    Run VADER sentiment on all posts, aggregate per-ticker stats.
    Returns an analysis dict with:
      - posts_scored: list[dict]  (posts + sentiment_score field)
      - ticker_stats: dict[ticker → TickerStats]
      - overall_bullish: int
      - overall_bearish: int
      - overall_neutral: int
    """
    if vader is None:
        vader = _get_vader()

    posts_scored = []
    ticker_agg: dict[str, dict] = defaultdict(lambda: {
        "mentions": 0, "scores": [], "posts": [], "bullish": 0, "bearish": 0, "neutral": 0
    })

    for post in posts:
        score = score_post(post.get("text", ""), vader)
        p = dict(post, sentiment=score)
        posts_scored.append(p)
        for ticker in post.get("tickers", []):
            t = ticker_agg[ticker]
            t["mentions"] += 1
            t["scores"].append(score)
            t["posts"].append(p)
            if score > 0.1:
                t["bullish"] += 1
            elif score < -0.1:
                t["bearish"] += 1
            else:
                t["neutral"] += 1

    # Compute per-ticker averages and hotness
    ticker_stats = {}
    for ticker, agg in ticker_agg.items():
        avg = sum(agg["scores"]) / len(agg["scores"]) if agg["scores"] else 0.0
        hotness = agg["mentions"] * (0.5 + abs(avg) * 0.5)
        # Bonus for strong sentiment agreement
        if agg["bullish"] > agg["bearish"] and agg["bullish"] / max(agg["mentions"], 1) > 0.6:
            hotness *= 1.25
        elif agg["bearish"] > agg["bullish"] and agg["bearish"] / max(agg["mentions"], 1) > 0.6:
            hotness *= 1.20
        ticker_stats[ticker] = {
            "ticker":   ticker,
            "mentions": agg["mentions"],
            "avg_score": round(avg, 3),
            "hotness":  round(hotness, 2),
            "bullish":  agg["bullish"],
            "bearish":  agg["bearish"],
            "neutral":  agg["neutral"],
            "posts":    agg["posts"][:3],  # keep only 3 sample posts per ticker
        }

    total = len(posts_scored)
    return {
        "posts_scored": posts_scored,
        "ticker_stats": ticker_stats,
        "total":         total,
        "overall_bullish": sum(1 for p in posts_scored if p["sentiment"] > 0.1),
        "overall_bearish": sum(1 for p in posts_scored if p["sentiment"] < -0.1),
        "overall_neutral": sum(1 for p in posts_scored if -0.1 <= p["sentiment"] <= 0.1),
        "avg_sentiment":   round(sum(p["sentiment"] for p in posts_scored) / total, 3) if total else 0,
        "vader_available": vader is not None,
    }


def get_hot_stocks(analysis: dict, top_n: int = 15) -> list[dict]:
    """Return top_n tickers ranked by hotness score (buzz × sentiment strength)."""
    stats = analysis.get("ticker_stats", {})
    ranked = sorted(stats.values(), key=lambda x: -x["hotness"])
    return ranked[:top_n]


# ── HTML report ───────────────────────────────────────────────────────────────

def _sentiment_bar(score: float, width: int = 120) -> str:
    """Return an HTML sentiment bar (red←→green with needle)."""
    pct = int((score + 1) / 2 * 100)
    color = "#ef4444" if score < -0.2 else "#f59e0b" if score < 0.1 else "#22c55e"
    label = "BEARISH" if score < -0.15 else "BULLISH" if score > 0.15 else "NEUTRAL"
    return (
        f"<div style='display:inline-block;vertical-align:middle;width:{width}px;"
        f"height:10px;background:linear-gradient(to right,#ef4444 0%,#f59e0b 40%,#22c55e 70%,#16a34a 100%);"
        f"border-radius:5px;position:relative;'>"
        f"<div style='position:absolute;top:-3px;left:{pct}%;width:3px;height:16px;"
        f"background:#fff;border-radius:2px;transform:translateX(-50%);'></div>"
        f"</div>"
        f"&nbsp;<span style='color:{color};font-size:11px;font-weight:600;'>{label} {score:+.2f}</span>"
    )


def _direction_badge(score: float) -> str:
    if score > 0.15:
        return "<span style='background:#16a34a;color:#fff;padding:2px 7px;border-radius:12px;font-size:11px;'>🐂 BULL</span>"
    elif score < -0.15:
        return "<span style='background:#dc2626;color:#fff;padding:2px 7px;border-radius:12px;font-size:11px;'>🐻 BEAR</span>"
    return "<span style='background:#4b5563;color:#d1d5db;padding:2px 7px;border-radius:12px;font-size:11px;'>〰 NEUTRAL</span>"


def _esc(s: str) -> str:
    return html.escape(str(s) if s else "")


def render_html_report(
    public_analysis: dict,
    hot_stocks: list[dict],
    following_posts: list[dict],
    endpoints: list[dict],
    out_path: Path,
    authenticated: bool = False,
    following_ids: list[str] | None = None,
) -> None:
    following_ids = following_ids or []
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    posts_scored = public_analysis.get("posts_scored", [])
    total = public_analysis.get("total", 0)
    avg_s = public_analysis.get("avg_sentiment", 0)
    bull  = public_analysis.get("overall_bullish", 0)
    bear  = public_analysis.get("overall_bearish", 0)
    neut  = public_analysis.get("overall_neutral", 0)
    vader_ok = public_analysis.get("vader_available", False)

    # ── Hot Stocks table ────────────────────────────────────────────────────
    hot_rows = ""
    for i, s in enumerate(hot_stocks, 1):
        bar = _sentiment_bar(s["avg_score"], 100)
        badge = _direction_badge(s["avg_score"])
        # Link to Moomoo stock page
        code = s["ticker"].replace("-", "").replace(".", "-")
        link = f"https://www.moomoo.com/stock/{s['ticker']}"
        hot_rows += f"""
        <tr>
          <td style='text-align:center;color:#9ca3af;'>{i}</td>
          <td><a href='{link}' target='_blank' style='color:#60a5fa;font-weight:700;font-size:14px;'>{_esc(s['ticker'])}</a></td>
          <td style='text-align:center;'><b>{s['mentions']}</b></td>
          <td>{bar}</td>
          <td style='text-align:center;'>{badge}</td>
          <td style='text-align:center;'>
            <span style='color:#22c55e;'>▲{s['bullish']}</span>
            <span style='color:#6b7280;'>&nbsp;~{s['neutral']}</span>
            <span style='color:#ef4444;'>&nbsp;▼{s['bearish']}</span>
          </td>
          <td style='color:#9ca3af;font-size:12px;'>
            {_esc(s['posts'][0]['text'][:120] + '…' if s['posts'] else '')}
          </td>
        </tr>"""

    # ── Public posts table ───────────────────────────────────────────────────
    pub_rows = ""
    for p in posts_scored[:40]:
        score = p.get("sentiment", 0)
        bar = _sentiment_bar(score, 80)
        tickers_html = " ".join(
            f"<a href='https://www.moomoo.com/stock/{t}' target='_blank' "
            f"style='background:#1e3a5f;color:#60a5fa;padding:1px 6px;border-radius:10px;font-size:11px;"
            f"text-decoration:none;margin-right:3px;'>{_esc(t)}</a>"
            for t in p.get("tickers", [])[:4]
        )
        source_badge = (
            "<span style='color:#a78bfa;font-size:10px;'>★ FOLLOWING</span>"
            if p.get("source") == "following" else ""
        )
        official_badge = (
            " <span style='color:#fbbf24;'>★</span>"
            if p.get("official") else ""
        )
        pub_rows += f"""
        <tr>
          <td style='min-width:110px;'>
            <div style='font-weight:600;font-size:13px;color:#e5e7eb;'>{_esc(p.get('author','?'))}{official_badge}</div>
            <div style='color:#6b7280;font-size:11px;'>{_esc(p.get('time',''))}&nbsp;{source_badge}</div>
          </td>
          <td style='color:#d1d5db;font-size:13px;line-height:1.5;'>
            {_esc(p.get('text',''))}
            <div style='margin-top:4px;'>{tickers_html}</div>
          </td>
          <td style='min-width:190px;'>{bar}</td>
          <td><a href='{_esc(p.get('url',''))}' target='_blank' style='color:#4b5563;font-size:11px;'>→</a></td>
        </tr>"""

    # ── Following posts section ──────────────────────────────────────────────
    following_html = ""
    if following_posts:
        follow_rows = ""
        vader = _get_vader()
        for p in following_posts[:20]:
            score = score_post(p.get("text", ""), vader)
            bar = _sentiment_bar(score, 80)
            follow_rows += f"""
            <tr>
              <td style='min-width:110px;font-weight:600;color:#e5e7eb;font-size:13px;'>{_esc(p.get('author','?'))}</td>
              <td style='color:#d1d5db;font-size:13px;'>{_esc(p.get('text',''))}</td>
              <td>{bar}</td>
              <td><a href='{_esc(p.get('url',''))}' target='_blank' style='color:#4b5563;font-size:11px;'>→</a></td>
            </tr>"""
        # Build following IDs list for the report
        ids_html = ""
        if following_ids:
            links = " ".join(
                f"<a href='https://www.moomoo.com/community/profile/{uid}' target='_blank' "
                f"style='color:#60a5fa;font-size:11px;margin-right:6px;'>{uid}</a>"
                for uid in following_ids[:50]
            )
            ids_html = f"""<p style='color:#9ca3af;font-size:12px;margin:4px 0 12px;'>
                Following <b style='color:#e5e7eb'>{len(following_ids)}</b> users: {links}
                {"… and more" if len(following_ids) > 50 else ""}
              </p>"""
        following_html = f"""
        <div class='section'>
          <h2>📌 Following Feed  <span style='font-size:13px;color:#9ca3af;'>({len(following_posts)} posts)</span></h2>
          {ids_html}
          <table class='data-table'>
            <thead><tr><th>Author</th><th>Post</th><th>Sentiment</th><th></th></tr></thead>
            <tbody>{follow_rows}</tbody>
          </table>
        </div>"""
    elif authenticated:
        ids_html = ""
        if following_ids:
            links = " ".join(
                f"<a href='https://www.moomoo.com/community/profile/{uid}' target='_blank' "
                f"style='color:#60a5fa;font-size:11px;margin-right:6px;'>{uid}</a>"
                for uid in following_ids[:30]
            )
            ids_html = f"""<p style='color:#9ca3af;font-size:12px;margin:6px 0;'>
                Following <b style='color:#e5e7eb'>{len(following_ids)}</b> users: {links}
                {"… and more" if len(following_ids) > 30 else ""}
              </p>"""
        following_html = f"""<div class='section'>
          <h2>📌 Following Feed</h2>
          {ids_html}
          <p style='color:#9ca3af;'>No new posts from followed users in this session.</p>
        </div>"""

    # ── API endpoints section ────────────────────────────────────────────────
    endpoints_html = ""
    if endpoints:
        ep_rows = "".join(
            f"<tr><td style='font-family:monospace;color:#60a5fa;font-size:12px;'>{_esc(e['path'])}</td>"
            f"<td style='font-size:12px;color:#9ca3af;'>{_esc(', '.join(e['keys'][:5]))}</td>"
            f"<td style='text-align:right;color:#6b7280;font-size:12px;'>{e['size']:,}B</td></tr>"
            for e in endpoints[:30]
        )
        endpoints_html = f"""
        <div class='section'>
          <h2>🔍 Discovered API Endpoints  <span style='font-size:13px;color:#9ca3af;'>({len(endpoints)} unique)</span></h2>
          <p style='color:#9ca3af;font-size:12px;'>These are the live API endpoints intercepted during authenticated browsing. High-value endpoints can be called directly for faster data access.</p>
          <table class='data-table'>
            <thead><tr><th>Endpoint Path</th><th>Response Keys</th><th>Size</th></tr></thead>
            <tbody>{ep_rows}</tbody>
          </table>
        </div>"""

    # ── Auth status ──────────────────────────────────────────────────────────
    auth_badge = (
        "<span style='background:#16a34a;color:#fff;padding:3px 12px;border-radius:12px;font-size:12px;'>✓ Authenticated</span>"
        if authenticated else
        "<span style='background:#4b5563;color:#9ca3af;padding:3px 12px;border-radius:12px;font-size:12px;'>Public Only</span>"
    )
    vader_badge = (
        "<span style='background:#2563eb;color:#fff;padding:3px 12px;border-radius:12px;font-size:12px;'>VADER ✓</span>"
        if vader_ok else
        "<span style='background:#4b5563;color:#9ca3af;padding:3px 12px;border-radius:12px;font-size:12px;'>VADER unavailable — run: pip install vaderSentiment</span>"
    )
    bull_pct = int(bull / total * 100) if total else 0
    bear_pct = int(bear / total * 100) if total else 0
    neut_pct = 100 - bull_pct - bear_pct

    css = """
    body { background:#111827; color:#e5e7eb; font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; margin:0; padding:20px; }
    .header { background:linear-gradient(135deg,#1e3a5f 0%,#1a1a2e 100%); border-radius:12px; padding:24px 30px; margin-bottom:24px; border:1px solid #1e3a5f; }
    .section { background:#1f2937; border-radius:10px; padding:20px 24px; margin-bottom:20px; border:1px solid #374151; }
    h1 { margin:0 0 8px; font-size:24px; color:#fff; }
    h2 { margin:0 0 16px; font-size:18px; color:#e5e7eb; border-bottom:1px solid #374151; padding-bottom:8px; }
    .meta { color:#9ca3af; font-size:13px; }
    .stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:16px; margin-bottom:0; }
    .stat-card { background:#111827; border-radius:8px; padding:14px 16px; text-align:center; border:1px solid #374151; }
    .stat-card .num { font-size:28px; font-weight:700; color:#fff; }
    .stat-card .lbl { font-size:12px; color:#9ca3af; margin-top:4px; }
    .green { color:#22c55e !important; }
    .red   { color:#ef4444 !important; }
    .amber { color:#f59e0b !important; }
    table.data-table { width:100%; border-collapse:collapse; font-size:13px; }
    table.data-table th { color:#9ca3af; font-weight:600; padding:8px 10px; border-bottom:1px solid #374151; text-align:left; font-size:12px; }
    table.data-table td { padding:10px 10px; border-bottom:1px solid #1f2937; vertical-align:top; }
    table.data-table tr:hover td { background:#1a2436; }
    .sentiment-bar-bg { height:8px; background:linear-gradient(to right,#ef4444,#f59e0b 40%,#22c55e 70%,#16a34a); border-radius:4px; }
    """

    overall_bar = f"""
    <div style='display:flex;align-items:center;gap:10px;margin-top:12px;'>
      <span style='color:#22c55e;font-size:13px;'>🐂 Bullish</span>
      <div style='flex:1;height:14px;border-radius:7px;overflow:hidden;display:flex;background:#374151;'>
        <div style='width:{bull_pct}%;background:#22c55e;'></div>
        <div style='width:{neut_pct}%;background:#4b5563;'></div>
        <div style='width:{bear_pct}%;background:#ef4444;'></div>
      </div>
      <span style='color:#ef4444;font-size:13px;'>🐻 Bearish</span>
    </div>
    <div style='display:flex;justify-content:space-between;margin-top:4px;font-size:12px;color:#9ca3af;'>
      <span>{bull} ({bull_pct}%)</span><span>{neut} neutral</span><span>{bear} ({bear_pct}%)</span>
    </div>"""

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Moomoo Community Intelligence — {now}</title>
<style>{css}</style>
</head>
<body>

<div class='header'>
  <h1>📊 Moomoo Community Intelligence</h1>
  <div class='meta'>Generated: {now} &nbsp;|&nbsp; {total} posts analysed &nbsp;|&nbsp; {auth_badge} &nbsp;|&nbsp; {vader_badge}</div>
  {overall_bar}
</div>

<div class='section'>
  <h2>📈 Market Sentiment Overview</h2>
  <div class='stats-grid'>
    <div class='stat-card'><div class='num'>{total}</div><div class='lbl'>Posts Analysed</div></div>
    <div class='stat-card'><div class='num green'>{bull}</div><div class='lbl'>🐂 Bullish Posts</div></div>
    <div class='stat-card'><div class='num red'>{bear}</div><div class='lbl'>🐻 Bearish Posts</div></div>
    <div class='stat-card'><div class='num amber'>{neut}</div><div class='lbl'>〰 Neutral Posts</div></div>
    <div class='stat-card'><div class='num {"green" if avg_s > 0.05 else "red" if avg_s < -0.05 else "amber"}'>{avg_s:+.3f}</div><div class='lbl'>Avg Sentiment</div></div>
    <div class='stat-card'><div class='num'>{len(public_analysis.get("ticker_stats", {}))}</div><div class='lbl'>Unique Tickers</div></div>
  </div>
</div>

<div class='section'>
  <h2>🔥 Hot Stocks Right Now  <span style='font-size:13px;color:#9ca3af;'>({len(hot_stocks)} ranked)</span></h2>
  <p style='color:#9ca3af;font-size:12px;margin:0 0 12px;'>Ranked by: <b>mentions × sentiment strength</b>.
     Stocks with strong consensus (all posts agree direction) get a 25% hotness boost.</p>
  <table class='data-table'>
    <thead>
      <tr>
        <th>#</th><th>Ticker</th><th>Mentions</th><th>Community Sentiment</th><th>Direction</th>
        <th>▲/~/▼</th><th>Latest Post Snippet</th>
      </tr>
    </thead>
    <tbody>{hot_rows}</tbody>
  </table>
</div>

<div class='section'>
  <h2>📰 Community Posts  <span style='font-size:13px;color:#9ca3af;'>({len(posts_scored)} posts)</span></h2>
  <table class='data-table'>
    <thead><tr><th>Author</th><th>Post</th><th>Sentiment</th><th></th></tr></thead>
    <tbody>{pub_rows}</tbody>
  </table>
</div>

{following_html}
{endpoints_html}

</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page_html, encoding="utf-8")
    print(f"Report saved → {out_path}")

    # ── Archive report ───────────────────────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent / "reports"))
        from report_db import archive_report as _arc
        _arc(out_path)
    except Exception as _e:
        print(f"  [archive] moomoo_community_intel archive skipped: {_e}")


# ── Console render ─────────────────────────────────────────────────────────────

def render_console_summary(analysis: dict, hot_stocks: list[dict]) -> None:
    total = analysis.get("total", 0)
    bull  = analysis.get("overall_bullish", 0)
    bear  = analysis.get("overall_bearish", 0)
    avg_s = analysis.get("avg_sentiment", 0)
    vader_ok = analysis.get("vader_available", False)

    print("\n" + "═" * 72)
    print("  🔥 MOOMOO COMMUNITY INTELLIGENCE")
    print("═" * 72)
    print(f"  Posts analysed : {total}")
    if vader_ok:
        dir_label = "🐂 BULLISH" if avg_s > 0.05 else "🐻 BEARISH" if avg_s < -0.05 else "〰 NEUTRAL"
        print(f"  Avg sentiment  : {avg_s:+.3f}  {dir_label}")
        print(f"  Bullish posts  : {bull}  |  Bearish: {bear}  |  Neutral: {total-bull-bear}")
    else:
        print("  Sentiment: N/A  (run: pip install vaderSentiment)")

    if hot_stocks:
        print(f"\n  {'RANK':<5}{'TICKER':<14}{'MENTIONS':<10}{'SENTIMENT':<12}{'DIRECTION'}")
        print("  " + "─" * 58)
        for i, s in enumerate(hot_stocks[:10], 1):
            dir_icon = "🐂" if s["avg_score"] > 0.15 else "🐻" if s["avg_score"] < -0.15 else "〰"
            print(f"  {i:<5}{s['ticker']:<14}{s['mentions']:<10}{s['avg_score']:+.3f}       {dir_icon}")

    print("═" * 72 + "\n")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Moomoo Community Intelligence Engine")
    parser.add_argument("--n",     type=int, default=30,
                        help="Number of public posts to fetch (default 30)")
    parser.add_argument("--auth",  action="store_true",
                        help="Use moomoo_cookie to fetch authenticated following feed")
    parser.add_argument("--probe", action="store_true",
                        help="Probe & log discovered API endpoints (requires --auth)")
    parser.add_argument("--out",   default="",
                        help="Save HTML report to this path (default: reports/moomoo_community_intel.html)")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else ROOT / "reports" / "moomoo_community_intel.html"

    vader = _get_vader()
    if not vader:
        print("[WARN] vaderSentiment not installed — sentiment scoring disabled.")
        print("       Run: pip install vaderSentiment")

    # ── Step 1: Public feed ──────────────────────────────────────────────────
    print(f"Fetching {args.n} public community posts…")
    public_posts = fetch_public_feed(n=args.n)
    print(f"  Got {len(public_posts)} posts.")

    # ── Step 2: Sentiment analysis ───────────────────────────────────────────
    print("Running VADER sentiment analysis…")
    analysis = analyse_posts(public_posts, vader)
    hot = get_hot_stocks(analysis, top_n=15)

    # ── Step 3: Following feed (if auth) ─────────────────────────────────────
    following_posts: list[dict] = []
    following_ids: list[str] = []
    if args.auth:
        cookies = load_moomoo_cookies()
        if not cookies:
            print(f"\n[INFO] To enable following feed, create: {COOKIE_FILE}")
            print("  1. Log in at www.moomoo.com in your browser")
            print("  2. DevTools (F12) → Application → Cookies → www.moomoo.com")
            print("  3. Copy all cookie values as one line into the file:")
            print(f"     {COOKIE_FILE}")
        else:
            # Fetch the list of user IDs you follow (via REST — fast, no browser needed)
            print("Fetching following list via REST API…")
            following_ids = fetch_following_ids(cookies)
            print(f"  Following {len(following_ids)} users on Moomoo.")

            print(f"Fetching following feed (authenticated, {len(cookies)} cookies loaded)…")
            following_posts = fetch_following_feed(n=args.n, cookies=cookies)
            if following_posts:
                print(f"  Got {len(following_posts)} following posts.")
                # Add following posts into analysis
                all_posts = public_posts + following_posts
                analysis = analyse_posts(all_posts, vader)
                hot = get_hot_stocks(analysis, top_n=15)

    # ── Step 4: API endpoint probe ───────────────────────────────────────────
    endpoints: list[dict] = []
    if args.probe and args.auth:
        print("Probing authenticated API endpoints…")
        cookies = load_moomoo_cookies()
        if cookies:
            endpoints = probe_authenticated_endpoints(cookies)
            print(f"  Discovered {len(endpoints)} unique API endpoints.")

    # ── Step 5: Console summary ───────────────────────────────────────────────
    render_console_summary(analysis, hot)

    # ── Step 6: HTML report ───────────────────────────────────────────────────
    authenticated = args.auth and bool(load_moomoo_cookies())
    render_html_report(
        public_analysis=analysis,
        hot_stocks=hot,
        following_posts=following_posts,
        endpoints=endpoints,
        out_path=out_path,
        authenticated=authenticated,
        following_ids=following_ids,
    )


if __name__ == "__main__":
    main()
