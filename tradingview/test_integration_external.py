"""
Integration Tests — External Expert Signal Enrichers (L7)
=========================================================
These tests make REAL network calls to YouTube Data API v3, Smallcase.com,
and optionally Twitter/X via twscrape.

They verify:
  1. YouTube API key is valid and channel search works
  2. Akshat Shrivastava channel returns videos in the lookback window
  3. Smallcase cookie is accepted and subscriptions page is reachable
  4. Subscription holdings can be fetched for at least one basket
  5. Twitter cashtag search works for expert handles (if twscrape configured)
  6. L7 compute_l7_score() runs end-to-end without error

Tests are automatically SKIPPED when:
  • Network is unreachable
  • API key/cookie is absent
  • twscrape has no configured accounts

Run:
    pytest test_integration_external.py -v
    pytest test_integration_external.py -v -k "youtube"     # YouTube only
    pytest test_integration_external.py -v -k "smallcase"   # Smallcase only
    pytest test_integration_external.py -v --tb=short -s    # see print output
"""

import os
import socket
import sys
import time
from pathlib import Path

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
TV_DIR   = Path(__file__).parent
ROOT     = TV_DIR.parent
sys.path.insert(0, str(TV_DIR))
sys.path.insert(0, str(ROOT))

_SMALLCASE_COOKIE = ROOT / "smallcase" / "smallcase-cookie"
_YOUTUBE_API_KEY  = os.environ.get(
    "YOUTUBE_API_KEY",
    ""
)
_AKSHAT_CHANNEL_ID = "UCqW8jxh4tH1Z1sWPbkGWL4g"


# ─── Connectivity guards ───────────────────────────────────────────────────────
def _host_reachable(host: str, port: int = 443, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _youtube_reachable() -> bool:
    return _host_reachable("www.googleapis.com")


def _smallcase_reachable() -> bool:
    return _host_reachable("www.smallcase.com")


def _twitter_reachable() -> bool:
    return _host_reachable("twitter.com")


def _have_youtube_key() -> bool:
    return bool(_YOUTUBE_API_KEY) and not _YOUTUBE_API_KEY.startswith("YOUR_")


def _have_smallcase_cookie() -> bool:
    return _SMALLCASE_COOKIE.exists() and _SMALLCASE_COOKIE.stat().st_size > 20


def _have_twscrape_accounts() -> bool:
    """True if at least one active twscrape account is configured."""
    try:
        import asyncio
        import twscrape
        db_file = TV_DIR.parent / "database" / ".twscrape_accounts.db"
        if not db_file.exists():
            return False
        async def _check():
            api = twscrape.API(str(db_file))
            return await api.pool.accounts_info()
        accounts = asyncio.run(_check())
        return bool([a for a in accounts if getattr(a, "active", False)])
    except Exception:
        return False


# ─── Pytest skip markers (evaluated lazily to avoid import overhead) ────────────
_skip_no_youtube = pytest.mark.skipif(
    not (_have_youtube_key() and _youtube_reachable()),
    reason="YouTube API key absent or googleapis.com unreachable",
)
_skip_no_smallcase = pytest.mark.skipif(
    not (_have_smallcase_cookie() and _smallcase_reachable()),
    reason="Smallcase cookie absent or smallcase.com unreachable",
)
_skip_no_twitter = pytest.mark.skipif(
    not (_have_twscrape_accounts() and _twitter_reachable()),
    reason="twscrape accounts not configured or twitter.com unreachable",
)


# ═════════════════════════════════════════════════════════════════════════════
#  YOUTUBE INTEGRATION TESTS
# ═════════════════════════════════════════════════════════════════════════════
class TestYoutubeIntegration:

    @_skip_no_youtube
    def test_channel_videos_fetch_returns_list(self):
        """_fetch_community_posts should return a list (may be empty if no recent posts)."""
        from youtube_enricher import _fetch_community_posts, _load_youtube_cookie
        cookie = _load_youtube_cookie()
        posts = _fetch_community_posts(_AKSHAT_CHANNEL_ID, cookie, lookback_hours=720)  # 30 days
        assert isinstance(posts, list), "Expected list, got something else"

    @_skip_no_youtube
    def test_channel_video_dict_structure(self):
        """Each post dict must have the expected keys."""
        from youtube_enricher import _fetch_community_posts, _load_youtube_cookie
        cookie = _load_youtube_cookie()
        posts = _fetch_community_posts(_AKSHAT_CHANNEL_ID, cookie, lookback_hours=720)
        for p in posts:
            for key in ("post_id", "text", "published_time", "is_member_only"):
                assert key in p, f"Missing key '{key}' in post dict"

    @_skip_no_youtube
    def test_get_youtube_mentions_no_crash(self):
        """get_youtube_mentions should complete without raising for a known NSE symbol."""
        from youtube_enricher import get_youtube_mentions, score_youtube
        # Use a symbol Akshat commonly covers
        data  = get_youtube_mentions("NSE:RELIANCE", use_transcript=False)
        score = score_youtube(data)
        assert isinstance(data["mention_count"], int)
        assert 0 <= score <= 10, f"Score out of range: {score}"

    @_skip_no_youtube
    def test_score_is_zero_for_unlikely_symbol(self):
        """A US micro-cap ticker is unlikely to appear in Akshat's India-focused channel."""
        from youtube_enricher import get_youtube_mentions, score_youtube
        data  = get_youtube_mentions("XYZNOTREAL", use_transcript=False)
        score = score_youtube(data)
        assert data["mention_count"] == 0
        assert score == 0.0

    @_skip_no_youtube
    def test_youtube_cache_avoids_double_api_call(self):
        """Second call for same channel should use in-memory cache."""
        from youtube_enricher import _fetch_community_posts, _load_youtube_cookie, _MENTION_CACHE
        _MENTION_CACHE.clear()
        cookie = _load_youtube_cookie()
        t0 = time.time()
        _fetch_community_posts(_AKSHAT_CHANNEL_ID, cookie, lookback_hours=720)
        t1 = time.time()
        _fetch_community_posts(_AKSHAT_CHANNEL_ID, cookie, lookback_hours=720)
        t2 = time.time()
        first_call_ms  = (t1 - t0) * 1000
        second_call_ms = (t2 - t1) * 1000
        # Second call should be orders of magnitude faster (< 10ms vs network latency)
        assert second_call_ms < first_call_ms * 0.1 or second_call_ms < 5, (
            f"Cache not working: 1st={first_call_ms:.0f}ms, 2nd={second_call_ms:.0f}ms"
        )


# ═════════════════════════════════════════════════════════════════════════════
#  SMALLCASE INTEGRATION TESTS
#  These tests read from sc_baskets.json (no network, no cookie required).
# ═════════════════════════════════════════════════════════════════════════════
class TestSmallcaseIntegration:

    def test_holdings_returns_set_and_dict(self):
        """get_smallcase_holdings should return (set, dict) without error."""
        from smallcase_enricher import get_smallcase_holdings
        all_tickers, basket_weights = get_smallcase_holdings()
        assert isinstance(all_tickers, set), "Expected set for all_tickers"
        assert isinstance(basket_weights, dict), "Expected dict for basket_weights"
        assert len(all_tickers) > 0, "No tickers loaded — check sc_baskets.json"
        assert len(basket_weights) > 0, "No baskets loaded — check sc_baskets.json"

    def test_fetched_tickers_are_strings(self):
        """All tickers in the returned set must be non-empty strings."""
        from smallcase_enricher import get_smallcase_holdings
        all_tickers, _ = get_smallcase_holdings()
        for t in all_tickers:
            assert isinstance(t, str) and len(t) > 0, f"Bad ticker: {t!r}"

    def test_score_not_in_basket_is_zero(self):
        """Symbol not in any basket should get score 0.0."""
        from smallcase_enricher import score_smallcase
        result = score_smallcase("NSE:INFY")
        assert result["smallcase_score"] == 0.0
        assert result["in_subscription"] is False
        assert result["basket_names"] == []

    def test_score_proportional_to_weight(self):
        """THERMAX is 16% in PTDMO_0005 → score should be 4.0 + 16/4 = 8.0."""
        from smallcase_enricher import score_smallcase
        result = score_smallcase("NSE:THERMAX")
        assert result["in_subscription"] is True
        # THERMAX appears in both baskets: 16% (PTDMO_0005) beats 3% (GREEN_ENERGY)
        assert result["max_weight"] == 16.0
        assert result["smallcase_score"] == pytest.approx(8.0, abs=0.01)

    def test_score_capped_at_ten(self):
        """CAPACITE is 25% in PTDMO_0005 → raw score 10.25, capped at 10.0."""
        from smallcase_enricher import score_smallcase
        result = score_smallcase("NSE:CAPACITE")
        assert result["in_subscription"] is True
        assert result["smallcase_score"] == 10.0

    def test_green_energy_basket_loaded(self):
        """Green & Clean Energy basket must be present and contain renewable stocks."""
        from smallcase_enricher import get_smallcase_holdings
        _, basket_weights = get_smallcase_holdings()
        green_baskets = {k: v for k, v in basket_weights.items()
                         if "green" in k.lower() or "energy" in k.lower()}
        assert green_baskets, "Green Energy basket not found — check sc_baskets.json"
        total_stocks = sum(len(v) for v in green_baskets.values())
        assert total_stocks >= 10, f"Expected ≥10 stocks in green basket, got {total_stocks}"

    def test_cache_returns_quickly(self):
        """Second call to get_smallcase_holdings should be < 5 ms (cache hit)."""
        from smallcase_enricher import get_smallcase_holdings, _cache
        # Warm the cache
        get_smallcase_holdings()
        t0 = time.time()
        get_smallcase_holdings()
        elapsed_ms = (time.time() - t0) * 1000
        assert elapsed_ms < 5, f"Cache not working: elapsed={elapsed_ms:.1f}ms"


# ═════════════════════════════════════════════════════════════════════════════
#  L7 SMALLCASE PIPELINE TESTS
#  End-to-end: compute_l7_score → l7_html_badge — no network required.
# ═════════════════════════════════════════════════════════════════════════════
class TestL7SmallcasePipeline:
    """Verify that basket name and weightage flow all the way from sc_baskets.json
    through compute_l7_score() and into the l7_html_badge() tooltip HTML."""

    def test_compute_l7_for_nse_basket_stock_has_basket_info(self):
        """compute_l7_score for NSE:THERMAX must expose basket name + max_weight."""
        from external_signal_enricher import compute_l7_score
        result = compute_l7_score("NSE:THERMAX")
        sc = result.get("smallcase", {})
        assert sc.get("in_subscription") is True, "THERMAX should be in subscription"
        assert sc.get("max_weight", 0.0) == 16.0, f"Expected 16.0%, got {sc.get('max_weight')}"
        assert any("Super Businesses" in b for b in sc.get("basket_names", [])), (
            f"Expected 'Super Businesses...' in basket_names, got {sc.get('basket_names')}"
        )

    def test_compute_l7_for_nse_stock_not_in_basket(self):
        """compute_l7_score for NSE:INFY must show not_in_subscription, weight 0."""
        from external_signal_enricher import compute_l7_score
        result = compute_l7_score("NSE:INFY")
        sc = result.get("smallcase", {})
        assert sc.get("in_subscription") is False
        assert sc.get("max_weight", -1) == 0.0
        assert sc.get("basket_names") == []

    def test_badge_tooltip_shows_basket_name_and_weight_for_basket_stock(self):
        """l7_html_badge must emit basket name and % weight in the title attribute."""
        from external_signal_enricher import compute_l7_score, l7_html_badge
        result = compute_l7_score("NSE:THERMAX")
        html   = l7_html_badge(result)
        assert html, "Badge should not be empty for active L7 result"
        assert "Super Businesses Fundamental" in html, (
            f"Basket name missing from badge tooltip. Badge: {html[:300]}"
        )
        assert "16" in html, f"Weight (16%) missing from badge tooltip. Badge: {html[:300]}"
        assert "wt" in html, f"'wt' label missing from badge tooltip. Badge: {html[:300]}"
        assert "not in any basket" not in html

    def test_badge_tooltip_shows_not_in_any_basket_for_unknown_stock(self):
        """l7_html_badge must say 'not in any basket' for a stock not in sc_baskets.json."""
        from external_signal_enricher import compute_l7_score, l7_html_badge
        result = compute_l7_score("NSE:INFY")
        html   = l7_html_badge(result)
        assert html, "Badge should not be empty (smallcase source is always active)"
        assert "not in any basket" in html, (
            f"Expected 'not in any basket' for INFY. Badge: {html[:300]}"
        )

    def test_badge_tooltip_for_high_weight_stock_shows_ten(self):
        """NSE:CAPACITE (25%) → score capped at 10.0 — badge must reflect that."""
        from external_signal_enricher import compute_l7_score, l7_html_badge
        result = compute_l7_score("NSE:CAPACITE")
        sc = result.get("smallcase", {})
        assert sc.get("smallcase_score") == 10.0, f"Expected 10.0, got {sc.get('smallcase_score')}"
        html = l7_html_badge(result)
        assert "25" in html, f"Weight 25% not found in badge: {html[:300]}"

    def test_thermax_in_two_baskets(self):
        """THERMAX is in both PTDMO_0005 (16%) and GREEN_ENERGY (3%) —
        badge must show both basket names and pick the max weight (16%)."""
        from external_signal_enricher import compute_l7_score, l7_html_badge
        result = compute_l7_score("NSE:THERMAX")
        sc     = result.get("smallcase", {})
        assert len(sc.get("basket_names", [])) == 2, (
            f"THERMAX should be in 2 baskets, got {sc.get('basket_names')}"
        )
        assert sc.get("max_weight") == 16.0   # takes max of 16% and 3%
        html = l7_html_badge(result)
        assert "Green" in html, "Green & Clean Energy basket name missing from badge"
        assert "Super Businesses" in html, "Super Businesses basket name missing from badge"


# ═════════════════════════════════════════════════════════════════════════════
#  TWITTER INTEGRATION TESTS
# ═════════════════════════════════════════════════════════════════════════════
class TestTwitterIntegration:

    @_skip_no_twitter
    def test_kiyosaki_tweets_fetched(self):
        """Should fetch tweets from Robert Kiyosaki's account without error."""
        from twitter_enricher import _fetch_user_tweets
        tweets = _fetch_user_tweets("theRealKiyosaki")
        assert isinstance(tweets, list)
        # May be empty if no tweets in lookback, but should not raise

    @_skip_no_twitter
    def test_tweet_dict_structure(self):
        """Each tweet dict must have standard keys."""
        from twitter_enricher import _fetch_user_tweets
        tweets = _fetch_user_tweets("theRealKiyosaki")
        for tw in tweets[:3]:  # Check first 3 only
            for key in ("tweet_id", "text", "date", "likes", "retweets"):
                assert key in tw, f"Missing key '{key}' in tweet dict"

    @_skip_no_twitter
    def test_get_twitter_mentions_no_crash(self):
        """get_twitter_mentions should run without error for GOLD (Kiyosaki's favourite)."""
        from twitter_enricher import get_twitter_mentions, score_twitter
        data  = get_twitter_mentions("GOLD", handles=["theRealKiyosaki"])
        score = score_twitter(data)
        assert isinstance(data["mention_count"], int)
        if score is not None:
            assert 0 <= score <= 10, f"Score out of range: {score}"


# ═════════════════════════════════════════════════════════════════════════════
#  END-TO-END L7 INTEGRATION TEST
# ═════════════════════════════════════════════════════════════════════════════
class TestL7EndToEnd:

    @pytest.mark.skipif(
        not (_have_youtube_key() and _youtube_reachable()) and
        not (_have_smallcase_cookie() and _smallcase_reachable()),
        reason="Neither YouTube nor Smallcase is reachable — L7 would produce no signal",
    )
    def test_compute_l7_score_nse_reliance(self):
        """compute_l7_score for NSE:RELIANCE should return valid-shaped result."""
        from external_signal_enricher import compute_l7_score
        result = compute_l7_score("NSE:RELIANCE", "RELIANCE.NS")
        # Must have required keys
        for key in ("l7_score", "youtube", "smallcase", "twitter", "sources_active"):
            assert key in result, f"Missing key '{key}'"
        # Score must be None or valid float in range
        if result["l7_score"] is not None:
            assert 0.0 <= result["l7_score"] <= 10.0, f"L7 score out of range: {result['l7_score']}"

    @pytest.mark.skipif(
        not (_have_youtube_key() and _youtube_reachable()) and
        not (_have_smallcase_cookie() and _smallcase_reachable()),
        reason="Neither YouTube nor Smallcase is reachable",
    )
    def test_compute_l7_us_stock_no_crash(self):
        """compute_l7_score for a US stock (YouTube/Twitter relevant, Smallcase neutral)."""
        from external_signal_enricher import compute_l7_score
        result = compute_l7_score("NASDAQ:NVDA", "NVDA")
        assert "l7_score" in result

    @pytest.mark.skipif(
        not (_have_youtube_key() and _youtube_reachable()) and
        not (_have_smallcase_cookie() and _smallcase_reachable()),
        reason="Neither YouTube nor Smallcase is reachable",
    )
    def test_l7_html_badge_valid_html(self):
        """l7_html_badge should return a non-empty HTML string for a known result."""
        from external_signal_enricher import compute_l7_score, l7_html_badge
        result = compute_l7_score("NSE:TATAPOWER", "TATAPOWER.NS")
        if result.get("l7_score") is None:
            pytest.skip("L7 score was None — no external signal for this symbol")
        html = l7_html_badge(result)
        assert html, "Expected non-empty HTML badge"
        assert "L7" in html, "Badge should contain 'L7'"
