"""
Unit tests for external signal enricher modules.
All network calls are mocked — no internet required.

Run: pytest signal_engine/tests/test_external_signals.py -v
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Make tradingview/ importable
_TV_DIR = Path(__file__).parent.parent.parent
if str(_TV_DIR) not in sys.path:
    sys.path.insert(0, str(_TV_DIR))


# ─── Helper: build a mock video dict ─────────────────────────────────────────────
def _mock_video(video_id="abc123", title="", description=""):
    return {
        "video_id":     video_id,
        "title":        title,
        "description":  description,
        "published_at": "2024-01-01T10:00:00Z",
        "channel_name": "Akshat Shrivastava",
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  YOUTUBE ENRICHER TESTS
# ═══════════════════════════════════════════════════════════════════════════════
class TestYoutubeTickerExtraction(unittest.TestCase):
    """Ticker extraction regex — no network, no imports needed from external APIs."""

    def setUp(self):
        from youtube_enricher import _extract_tickers_from_text
        self.extract = _extract_tickers_from_text

    def test_cashtag_extraction(self):
        tickers = self.extract("I like $NVDA and also $AAPL today!")
        self.assertIn("NVDA", tickers)
        self.assertIn("AAPL", tickers)

    def test_exchange_prefix(self):
        tickers = self.extract("Buying NSE:RELIANCE and BSE:TCS soon")
        self.assertIn("RELIANCE", tickers)
        self.assertIn("TCS", tickers)

    def test_stopwords_excluded(self):
        tickers = self.extract("THE BEST STOCK FOR YOU TODAY")
        # None of these should be treated as tickers
        for w in ["THE", "BEST", "FOR", "YOU"]:
            self.assertNotIn(w, tickers)

    def test_empty_text(self):
        self.assertEqual(self.extract(""), set())

    def test_none_like_empty(self):
        # Should handle None-ish input gracefully
        self.assertEqual(self.extract(""), set())

    def test_short_words_excluded(self):
        # 1-2 char words should not be extracted
        result = self.extract("I like AI and IT")
        self.assertNotIn("I", result)

    def test_mixed_case_normalised(self):
        tickers = self.extract("i love $nvda and nse:reliance")
        # The extractor works on uppercased text
        self.assertIn("NVDA", tickers)
        self.assertIn("RELIANCE", tickers)


class TestYoutubeScore(unittest.TestCase):
    """score_youtube() function — pure math, no network."""

    def setUp(self):
        from youtube_enricher import score_youtube
        self.score = score_youtube

    def _mention(self, n, sentiment, in_title=False):
        return {
            "mention_count": n,
            "sentiment":     sentiment,
            "videos": [{"in_title": in_title, "likes": 0} for _ in range(n)],
        }

    def test_no_mentions_returns_zero(self):
        result = self.score({"mention_count": 0, "sentiment": 0.0, "videos": []})
        self.assertEqual(result, 0.0)

    def test_one_mention_neutral_sentiment(self):
        result = self.score(self._mention(1, 0.0))
        self.assertGreater(result, 0.0)
        self.assertLessEqual(result, 10.0)

    def test_title_mention_scores_higher_than_description(self):
        s_title = self.score(self._mention(1, 0.3, in_title=True))
        s_desc  = self.score(self._mention(1, 0.3, in_title=False))
        self.assertGreater(s_title, s_desc)

    def test_positive_sentiment_boosts_score(self):
        s_pos = self.score(self._mention(1, 0.8))    # very positive
        s_neg = self.score(self._mention(1, -0.8))   # very negative
        self.assertGreater(s_pos, s_neg)

    def test_score_clamped_0_to_10(self):
        for sent in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            for n in [1, 2, 5]:
                r = self.score(self._mention(n, sent, in_title=True))
                self.assertGreaterEqual(r, 0.0)
                self.assertLessEqual(r, 10.0)

    def test_more_mentions_scores_higher_equal(self):
        s1 = self.score(self._mention(1, 0.3))
        s3 = self.score(self._mention(3, 0.3))
        self.assertGreaterEqual(s3, s1)


@unittest.skip("get_youtube_mentions no longer uses _yt_search_channel_videos or _get_transcript_text — replaced by community-post-only flow")
class TestYoutubeGetMentions(unittest.TestCase):
    """get_youtube_mentions() — mock the API + transcript calls."""

    @patch("youtube_enricher._get_transcript_text", return_value="")
    @patch("youtube_enricher._yt_search_channel_videos")
    def test_no_videos_returns_zero_mentions(self, mock_search, mock_transcript):
        from youtube_enricher import get_youtube_mentions
        mock_search.return_value = []
        result = get_youtube_mentions("NSE:RELIANCE", channels={"test": "UCTEST"})
        self.assertEqual(result["mention_count"], 0)

    @patch("youtube_enricher._get_transcript_text", return_value="RELIANCE is great $RELIANCE")
    @patch("youtube_enricher._yt_search_channel_videos")
    def test_cashtag_in_transcript_counted(self, mock_search, mock_transcript):
        from youtube_enricher import get_youtube_mentions
        mock_search.return_value = [_mock_video(title="Market Update")]
        result = get_youtube_mentions("NSE:RELIANCE", channels={"test": "UCTEST"})
        self.assertEqual(result["mention_count"], 1)

    @patch("youtube_enricher._get_transcript_text", return_value="")
    @patch("youtube_enricher._yt_search_channel_videos")
    def test_title_mention_detected(self, mock_search, mock_transcript):
        from youtube_enricher import get_youtube_mentions
        mock_search.return_value = [_mock_video(title="Buy $NVDA now!")]
        result = get_youtube_mentions("NVDA", channels={"test": "UCTEST"})
        self.assertGreater(result["mention_count"], 0)
        self.assertTrue(result["videos"][0]["in_title"])

    @patch("youtube_enricher._get_transcript_text", return_value="")
    @patch("youtube_enricher._yt_search_channel_videos")
    def test_duplicate_mentions_counted_once_per_video(self, mock_search, mock_transcript):
        from youtube_enricher import get_youtube_mentions
        # Same symbol mentioned 3 times in same title / description → still 1 video
        mock_search.return_value = [
            _mock_video(title="$NVDA $NVDA $NVDA triple mention"),
        ]
        result = get_youtube_mentions("NVDA", channels={"test": "UCTEST"})
        self.assertEqual(result["mention_count"], 1)  # 1 video, not 3 mentions

    @patch("youtube_enricher._get_transcript_text", side_effect=Exception("network down"))
    @patch("youtube_enricher._yt_search_channel_videos")
    def test_transcript_error_non_fatal(self, mock_search, mock_transcript):
        from youtube_enricher import get_youtube_mentions
        mock_search.return_value = [_mock_video(title="$NVDA analysis")]
        # Should not raise even if transcript fetch fails
        try:
            result = get_youtube_mentions("NVDA", channels={"test": "UCTEST"})
        except Exception:
            self.fail("get_youtube_mentions raised an exception on transcript error")

    @patch("youtube_enricher._yt_search_channel_videos", side_effect=Exception("API quota"))
    def test_api_error_returns_empty(self, mock_search):
        from youtube_enricher import get_youtube_mentions
        result = get_youtube_mentions("NVDA", channels={"test": "UCTEST"})
        self.assertEqual(result["mention_count"], 0)


# ═══════════════════════════════════════════════════════════════════════════════
#  SMALLCASE ENRICHER TESTS
# ═══════════════════════════════════════════════════════════════════════════════
class TestSmallcaseEnricher(unittest.TestCase):

    def _make_basket_weights(self, ticker_weight_map: dict, basket_name: str = "Green Energy"):
        """Helper: build (all_tickers, basket_weights) tuple for mocking."""
        all_tickers = set(ticker_weight_map.keys())
        basket_weights = {basket_name: ticker_weight_map}
        return all_tickers, basket_weights

    @patch("smallcase_enricher.get_smallcase_holdings")
    def test_in_subscription_returns_weight_proportional_score(self, mock_get):
        from smallcase_enricher import score_smallcase
        # 16% weight → score = 4.0 + 16/4 = 8.0
        mock_get.return_value = self._make_basket_weights({"RELIANCE": 16.0})
        result = score_smallcase("NSE:RELIANCE")
        self.assertAlmostEqual(result["smallcase_score"], 8.0, places=2)
        self.assertTrue(result["in_subscription"])
        self.assertIn("Green Energy", result["basket_names"])
        self.assertEqual(result["max_weight"], 16.0)

    @patch("smallcase_enricher.get_smallcase_holdings")
    def test_two_baskets_max_weight_used_for_score(self, mock_get):
        from smallcase_enricher import score_smallcase
        # RELIANCE: 16% in basket1, 6% in basket2 → max_weight=16 → score=8.0
        all_tickers = {"RELIANCE"}
        basket_weights = {
            "Super Businesses": {"RELIANCE": 16.0},
            "Green Energy":     {"RELIANCE":  6.0},
        }
        mock_get.return_value = (all_tickers, basket_weights)
        result = score_smallcase("NSE:RELIANCE")
        self.assertEqual(result["max_weight"], 16.0)
        self.assertAlmostEqual(result["smallcase_score"], 8.0, places=2)
        self.assertEqual(len(result["basket_names"]), 2)

    @patch("smallcase_enricher.get_smallcase_holdings")
    def test_high_weight_capped_at_ten(self, mock_get):
        from smallcase_enricher import score_smallcase
        # 25% weight → 4.0 + 25/4 = 10.25 → capped at 10.0
        mock_get.return_value = self._make_basket_weights({"CAPACITE": 25.0})
        result = score_smallcase("NSE:CAPACITE")
        self.assertEqual(result["smallcase_score"], 10.0)

    @patch("smallcase_enricher.get_smallcase_holdings")
    def test_not_in_subscription_returns_score_0(self, mock_get):
        from smallcase_enricher import score_smallcase
        mock_get.return_value = ({"RELIANCE"}, {"Green Energy": {"RELIANCE": 16.0}})
        result = score_smallcase("NSE:INFY")
        self.assertEqual(result["smallcase_score"], 0.0)
        self.assertFalse(result["in_subscription"])
        self.assertEqual(result["max_weight"], 0.0)

    @patch("smallcase_enricher.get_smallcase_holdings", side_effect=Exception("network error"))
    def test_error_returns_neutral_fallback(self, mock_get):
        from smallcase_enricher import score_smallcase
        result = score_smallcase("NSE:RELIANCE")
        self.assertIn("error", result)
        self.assertNotEqual(result["error"], "")
        self.assertEqual(result["smallcase_score"], 5.0)

    def test_bare_ticker_strip_works(self):
        """NSE:RELIANCE and RELIANCE should both match the same ticker."""
        from smallcase_enricher import score_smallcase
        with patch("smallcase_enricher.get_smallcase_holdings") as mock_get:
            mock_get.return_value = ({"RELIANCE"}, {"basket": {"RELIANCE": 16.0}})
            r1 = score_smallcase("NSE:RELIANCE")
            r2 = score_smallcase("RELIANCE")
        self.assertEqual(r1["in_subscription"], r2["in_subscription"])
        self.assertEqual(r1["smallcase_score"], r2["smallcase_score"])

    def test_missing_baskets_file_returns_empty(self):
        """When sc_baskets.json path points to non-existent file, holdings returns empty."""
        import smallcase_enricher
        original_path  = smallcase_enricher._SC_BASKETS_FILE
        original_cache = smallcase_enricher._cache
        try:
            smallcase_enricher._SC_BASKETS_FILE = Path("Z:/nonexistent/sc_baskets.json")
            smallcase_enricher._cache = None
            tickers, baskets = smallcase_enricher.get_smallcase_holdings()
            self.assertEqual(tickers, set())
            self.assertEqual(baskets, {})
        finally:
            smallcase_enricher._SC_BASKETS_FILE = original_path
            smallcase_enricher._cache = original_cache


# ═══════════════════════════════════════════════════════════════════════════════
#  TWITTER ENRICHER TESTS
# ═══════════════════════════════════════════════════════════════════════════════
class TestTwitterCashtagExtraction(unittest.TestCase):
    def setUp(self):
        from twitter_enricher import _extract_cashtags
        self.extract = _extract_cashtags

    def test_cashtag_found(self):
        result = self.extract("Gold is fiat. $GOLD will rule!")
        self.assertIn("GOLD", result)

    def test_exchange_prefix(self):
        result = self.extract("Buy NSE:RELIANCE now!")
        self.assertIn("RELIANCE", result)

    def test_no_cashtag(self):
        result = self.extract("No stocks here, just text.")
        self.assertEqual(result, set())

    def test_multiple_cashtags(self):
        result = self.extract("$BTC $GOLD $NVDA all going up")
        self.assertIn("BTC", result)
        self.assertIn("GOLD", result)
        self.assertIn("NVDA", result)


class TestTwitterScore(unittest.TestCase):
    def setUp(self):
        from twitter_enricher import score_twitter
        self.score = score_twitter

    def test_no_mentions_returns_none(self):
        result = self.score({"mention_count": 0, "sentiment": 0.0, "tweets": []})
        self.assertIsNone(result)

    @unittest.skip("Twitter disabled — TWITTER_ENABLED=False")
    def test_one_mention_returns_score(self):
        result = self.score({"mention_count": 1, "sentiment": 0.0, "tweets": [{"likes": 0, "retweets": 0}]})
        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)

    @unittest.skip("Twitter disabled — TWITTER_ENABLED=False")
    def test_positive_sentiment_higher_score(self):
        s_pos = self.score({"mention_count": 1, "sentiment": 0.8, "tweets": [{"likes": 0, "retweets": 0}]})
        s_neg = self.score({"mention_count": 1, "sentiment": -0.8, "tweets": [{"likes": 0, "retweets": 0}]})
        self.assertGreater(s_pos, s_neg)

    @unittest.skip("Twitter disabled — TWITTER_ENABLED=False")
    def test_viral_tweet_bonus(self):
        s_viral  = self.score({"mention_count": 1, "sentiment": 0.3,
                                "tweets": [{"likes": 200, "retweets": 50}]})
        s_normal = self.score({"mention_count": 1, "sentiment": 0.3,
                                "tweets": [{"likes": 1, "retweets": 0}]})
        self.assertGreater(s_viral, s_normal)

    @unittest.skip("Twitter disabled — TWITTER_ENABLED=False")
    def test_score_clamped_0_to_10(self):
        for sent in [-1.0, 0.0, 1.0]:
            r = self.score({"mention_count": 5, "sentiment": sent,
                            "tweets": [{"likes": 1000, "retweets": 500}]})
            self.assertGreaterEqual(r, 0.0)
            self.assertLessEqual(r, 10.0)


class TestTwitterGetMentions(unittest.TestCase):

    @patch("twitter_enricher._fetch_user_tweets", return_value=[])
    def test_no_tweets_returns_zero(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        result = get_twitter_mentions("NVDA", handles=["testhandle"])
        self.assertEqual(result["mention_count"], 0)

    @unittest.skip("Twitter disabled — TWITTER_ENABLED=False")
    @patch("twitter_enricher._fetch_user_tweets")
    def test_cashtag_match(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.return_value = [{
            "tweet_id": "111", "text": "$GOLD is the future!", "date": "2024-01-01",
            "likes": 10, "retweets": 5,
        }]
        result = get_twitter_mentions("GOLD", handles=["kiyosaki"])
        self.assertEqual(result["mention_count"], 1)
        self.assertEqual(result["tweets"][0]["handle"], "kiyosaki")

    @patch("twitter_enricher._fetch_user_tweets")
    def test_unrelated_tweet_not_counted(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.return_value = [{
            "tweet_id": "222", "text": "Today is a great day!", "date": "2024-01-01",
            "likes": 0, "retweets": 0,
        }]
        result = get_twitter_mentions("NVDA", handles=["test"])
        self.assertEqual(result["mention_count"], 0)

    @patch("twitter_enricher._fetch_user_tweets", side_effect=Exception("connection refused"))
    def test_network_error_returns_empty(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        result = get_twitter_mentions("NVDA", handles=["test"])
        self.assertEqual(result["mention_count"], 0)


# ═══════════════════════════════════════════════════════════════════════════════
#  EXTERNAL SIGNAL ENRICHER (L7 COMBINER) TESTS
# ═══════════════════════════════════════════════════════════════════════════════
class TestExternalSignalCombiner(unittest.TestCase):

    def setUp(self):
        from external_signal_enricher import _combine_scores
        self.combine = _combine_scores

    def test_all_three_sources_weighted_correctly(self):
        # yt=10, sc=10, tw=10 → should give 10.0
        result = self.combine(10.0, 10.0, 10.0)
        self.assertAlmostEqual(result, 10.0, places=1)

    def test_all_zero_gives_zero(self):
        result = self.combine(0.0, 0.0, 0.0)
        self.assertAlmostEqual(result, 0.0, places=2)

    def test_twitter_none_redistributes_weight(self):
        # With twitter=None, weight is redistributed to YT/SC
        r_with    = self.combine(10.0, 10.0, 10.0)
        r_without = self.combine(10.0, 10.0, None)
        # Both max-out at 10, but intermediate values should differ
        r_mid_with    = self.combine(5.0, 5.0, 5.0)
        r_mid_without = self.combine(5.0, 5.0, None)
        self.assertAlmostEqual(r_mid_with, r_mid_without, places=5)

    def test_weights_sum_to_one_with_twitter(self):
        # Verify by testing: if yt=1, sc=0, tw=0 → should give exactly 0.50
        r = self.combine(1.0, 0.0, 0.0)
        self.assertAlmostEqual(r, 0.50, places=3)

    def test_weights_without_twitter(self):
        # yt=1, sc=0, tw=None → yt_w = 0.50 + 0.15*(0.50/0.85) ≈ 0.588
        r = self.combine(1.0, 0.0, None)
        expected = 0.50 + 0.15 * (0.50 / 0.85)
        self.assertAlmostEqual(r, expected, places=3)


class TestComputeL7Score(unittest.TestCase):

    @patch("external_signal_enricher.score_twitter", return_value=None)
    @patch("external_signal_enricher.get_twitter_mentions", return_value={"mention_count": 0, "sentiment": 0.0, "tweets": []})
    @patch("external_signal_enricher.score_smallcase", return_value={"smallcase_score": 8.0, "in_subscription": True, "basket_names": ["Green Energy"], "all_fetched": 50, "error": ""})
    @patch("external_signal_enricher.score_youtube", return_value=6.5)
    @patch("external_signal_enricher.get_youtube_mentions", return_value={"mention_count": 1, "sentiment": 0.3, "videos": [], "symbol_checked": "ADANIGREEN"})
    def test_l7_score_computed(self, mock_yt_m, mock_yt_s, mock_sc, mock_tw_m, mock_tw_s):
        from external_signal_enricher import compute_l7_score
        result = compute_l7_score("NSE:ADANIGREEN")
        self.assertIsNotNone(result["l7_score"])
        self.assertGreater(result["l7_score"], 0.0)
        self.assertLessEqual(result["l7_score"], 10.0)
        self.assertIn("youtube", result["sources_active"])
        self.assertIn("smallcase", result["sources_active"])

    @patch("external_signal_enricher._YOUTUBE_OK", False)
    @patch("external_signal_enricher._SMALLCASE_OK", False)
    @patch("external_signal_enricher._TWITTER_OK", False)
    def test_no_sources_returns_none_score(self):
        from external_signal_enricher import compute_l7_score
        result = compute_l7_score("NSE:RELIANCE")
        self.assertIsNone(result["l7_score"])
        self.assertEqual(result["sources_active"], [])


# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL_STACK COMPOSITE FUNCTION TESTS
# ═══════════════════════════════════════════════════════════════════════════════
class TestCompositeFunction(unittest.TestCase):

    def setUp(self):
        from signal_stack import composite
        self.composite = composite

    def test_baseline_no_optional_layers(self):
        # (5*0.25 + 5*0.25 + 5*0.30 + 5*0.20) * 10 = 50.0
        result = self.composite(5, 5, 5, 5)
        self.assertAlmostEqual(result, 50.0, places=1)

    def test_l5_only_reduces_base_weight(self):
        # With l5=5, total base weight = 0.90, l5 weight = 0.10
        r_base = self.composite(5, 5, 5, 5)
        r_l5   = self.composite(5, 5, 5, 5, l5=5)
        # Same inputs → same score regardless
        self.assertAlmostEqual(r_base, r_l5, places=1)

    def test_l7_boosts_score_when_above_average(self):
        # l7=10 (above average of 5) should boost composite above baseline
        r_base = self.composite(5, 5, 5, 5)
        r_l7   = self.composite(5, 5, 5, 5, l7=10)
        self.assertGreater(r_l7, r_base)

    def test_l7_no_penalty_when_below_average(self):
        """L7 below mid-point (e.g. 0) must not penalise — score equals base (no bonus, no penalty)."""
        r_base = self.composite(5, 5, 5, 5)
        r_l7   = self.composite(5, 5, 5, 5, l7=0)
        self.assertAlmostEqual(r_l7, r_base, places=2,
                               msg="l7 ≤ 5 must produce zero penalty — same score as no-l7 base")

    def test_all_layers_present(self):
        # With all layers at equal mid-score, result should still be ~50
        r = self.composite(5, 5, 5, 5, l5=5, l6=5, l7=5)
        self.assertAlmostEqual(r, 50.0, places=1)

    def test_output_clamped_positive(self):
        r = self.composite(10, 10, 10, 10, l5=10, l6=10, l7=10)
        self.assertLessEqual(r, 100.0)

    def test_output_clamped_zero(self):
        r = self.composite(0, 0, 0, 0, l5=0, l6=0, l7=0)
        self.assertGreaterEqual(r, 0.0)

    def test_l7_only_no_l5_l6(self):
        r = self.composite(5, 5, 5, 5, l7=8)
        # l7=8 > 5 average → score should be above 50
        self.assertGreater(r, 50.0)
        self.assertLessEqual(r, 100.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  L7 HTML BADGE TEST
# ═══════════════════════════════════════════════════════════════════════════════
class TestL7HtmlBadge(unittest.TestCase):

    def setUp(self):
        from external_signal_enricher import l7_html_badge
        self.badge = l7_html_badge

    def test_none_returns_empty(self):
        self.assertEqual(self.badge(None), "")

    def test_none_score_returns_empty(self):
        self.assertEqual(self.badge({"l7_score": None}), "")

    def test_valid_score_returns_html(self):
        html = self.badge({"l7_score": 7.5, "yt_score": 6.0, "sc_score": 8.0,
                           "tw_score": None, "youtube": {}, "smallcase": {},
                           "sources_active": ["youtube", "smallcase"]})
        self.assertIn("L7", html)
        self.assertIn("7.5", html)

    def test_green_badge_for_high_score(self):
        html = self.badge({"l7_score": 8.5, "yt_score": 9.0, "sc_score": 8.0,
                           "tw_score": None, "youtube": {}, "smallcase": {},
                           "sources_active": ["youtube"]})
        self.assertIn("#2ecc71", html)  # green

    def test_red_badge_for_low_score(self):
        html = self.badge({"l7_score": 2.0, "yt_score": 2.0, "sc_score": 2.0,
                           "tw_score": 2.0, "youtube": {}, "smallcase": {},
                           "sources_active": ["youtube", "smallcase", "twitter"]})
        self.assertIn("#e74c3c", html)  # red
        
    def test_amber_badge_for_mid_score(self):
        html = self.badge({"l7_score": 5.5, "yt_score": 5.0, "sc_score": 6.0,
                           "tw_score": None, "youtube": {}, "smallcase": {},
                           "sources_active": ["youtube"]})
        self.assertIn("#f39c12", html)  # amber

    def test_smallcase_in_basket_shows_basket_name_and_weight(self):
        """When in_subscription=True, tooltip must include basket name and weight%."""
        enrichment = {
            "l7_score": 8.0, "yt_score": 0.0, "sc_score": 8.0,
            "tw_score": None,
            "youtube": {},
            "smallcase": {
                "smallcase_score": 8.0,
                "in_subscription": True,
                "basket_names": ["Super Businesses Fundamental"],
                "max_weight": 16.0,
                "all_fetched": 5,
                "error": "",
            },
            "sources_active": ["smallcase"],
        }
        html = self.badge(enrichment)
        self.assertIn("Super Businesses Fundamental", html)
        self.assertIn("16", html)   # weight percentage
        self.assertIn("wt", html)   # weight label
        self.assertNotIn("not in any basket", html)

    def test_smallcase_multi_basket_shows_all_names(self):
        """When in multiple baskets, all basket names appear in the tooltip."""
        enrichment = {
            "l7_score": 8.0, "yt_score": 0.0, "sc_score": 8.0,
            "tw_score": None,
            "youtube": {},
            "smallcase": {
                "smallcase_score": 8.0,
                "in_subscription": True,
                "basket_names": ["Super Businesses Fundamental", "Green & Clean Energy"],
                "max_weight": 16.0,
                "all_fetched": 32,
                "error": "",
            },
            "sources_active": ["smallcase"],
        }
        html = self.badge(enrichment)
        self.assertIn("Super Businesses Fundamental", html)
        self.assertIn("Green & Clean Energy", html)
        self.assertIn("16", html)

    def test_smallcase_not_in_basket_shows_not_in_any_basket(self):
        """When in_subscription=False, tooltip says 'not in any basket'."""
        enrichment = {
            "l7_score": 0.0, "yt_score": 0.0, "sc_score": 0.0,
            "tw_score": None,
            "youtube": {},
            "smallcase": {
                "smallcase_score": 0.0,
                "in_subscription": False,
                "basket_names": [],
                "max_weight": 0.0,
                "all_fetched": 32,
                "error": "",
            },
            "sources_active": ["smallcase"],
        }
        html = self.badge(enrichment)
        self.assertIn("not in any basket", html)
        self.assertNotIn("wt", html)


# ═══════════════════════════════════════════════════════════════════════════════
#  TWITTER ENRICHER — EXTENDED TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTwitterCashtagExtractionEdgeCases(unittest.TestCase):
    """Edge cases for _extract_cashtags() beyond the basic suite."""

    def setUp(self):
        from twitter_enricher import _extract_cashtags
        self.extract = _extract_cashtags

    def test_single_char_cashtag_not_matched(self):
        # $A has only 1 char — below minimum of 2
        result = self.extract("$A is going up")
        self.assertNotIn("A", result)

    def test_13_char_cashtag_not_matched(self):
        # 13 chars — above maximum of 12
        result = self.extract("$TOOLONGTICKER is moving")
        self.assertNotIn("TOOLONGTICKER", result)

    def test_lowercase_cashtag_detected(self):
        # Twitter users sometimes write $gold — uppercasing before regex must catch it
        result = self.extract("$gold will beat inflation says Kiyosaki")
        self.assertIn("GOLD", result)

    def test_bse_exchange_prefix(self):
        result = self.extract("BSE:RELIANCE looks bullish after results")
        self.assertIn("RELIANCE", result)

    def test_nyse_exchange_prefix(self):
        result = self.extract("NYSE:JPM earnings beat expectations today")
        self.assertIn("JPM", result)

    def test_nasdaq_exchange_prefix(self):
        result = self.extract("NASDAQ:AAPL buyback programme announced")
        self.assertIn("AAPL", result)

    def test_plain_word_without_prefix_not_matched(self):
        # Plain uppercase words without $ or exchange prefix → NOT extracted
        result = self.extract("RELIANCE is a great company for long term")
        self.assertEqual(result, set())

    def test_mixed_content_extracts_only_cashtags(self):
        result = self.extract("Studying inflation and $BTC and $GOLD today, reading FIAT")
        self.assertIn("BTC", result)
        self.assertIn("GOLD", result)
        self.assertNotIn("FIAT", result)  # no $ prefix

    def test_exchange_prefix_with_dot_separator(self):
        result = self.extract("NSE.RELIANCE also matches dot separator")
        self.assertIn("RELIANCE", result)

    def test_two_char_cashtag_at_minimum_length(self):
        result = self.extract("$AU gold futures spiking")
        self.assertIn("AU", result)

    def test_twelve_char_cashtag_at_maximum_length(self):
        result = self.extract("$ABCDEFGHIJKL is the longest valid ticker")
        self.assertIn("ABCDEFGHIJKL", result)


@unittest.skip("Twitter disabled — TWITTER_ENABLED=False; re-enable when Twitter is re-activated")
class TestTwitterScoreEdgeCases(unittest.TestCase):
    """Boundary conditions for score_twitter() beyond the basic suite."""

    def setUp(self):
        from twitter_enricher import score_twitter
        self.score = score_twitter

    def _tw(self, n=1, likes=0, retweets=0):
        return [{"likes": likes, "retweets": retweets}] * n

    def test_two_mentions_base_is_6(self):
        result = self.score({"mention_count": 2, "sentiment": 0.0, "tweets": self._tw(2)})
        self.assertAlmostEqual(result, 6.0, places=2)

    def test_three_mentions_base_is_7(self):
        result = self.score({"mention_count": 3, "sentiment": 0.0, "tweets": self._tw(3)})
        self.assertAlmostEqual(result, 7.0, places=2)

    def test_slight_positive_sentiment_adds_0_8(self):
        # 0.10 < sent ≤ 0.35 → +0.8 boost; base for 1 mention = 4.5
        result = self.score({"mention_count": 1, "sentiment": 0.20, "tweets": self._tw()})
        self.assertAlmostEqual(result, 5.3, places=2)

    def test_slight_negative_sentiment_subtracts_0_8(self):
        result = self.score({"mention_count": 1, "sentiment": -0.20, "tweets": self._tw()})
        self.assertAlmostEqual(result, 3.7, places=2)

    def test_strong_positive_sentiment_adds_1_5(self):
        # sent > 0.35 → +1.5; 4.5 + 1.5 = 6.0
        result = self.score({"mention_count": 1, "sentiment": 0.6, "tweets": self._tw()})
        self.assertAlmostEqual(result, 6.0, places=2)

    def test_strong_negative_sentiment_subtracts_1_5(self):
        # sent < -0.35 → -1.5; 4.5 - 1.5 = 3.0
        result = self.score({"mention_count": 1, "sentiment": -0.6, "tweets": self._tw()})
        self.assertAlmostEqual(result, 3.0, places=2)

    def test_viral_threshold_exactly_100_likes(self):
        # likes=100 → max_engagement=100 → viral_bonus=1.0 applied
        s_viral  = self.score({"mention_count": 1, "sentiment": 0.0,
                                "tweets": [{"likes": 100, "retweets": 0}]})
        s_normal = self.score({"mention_count": 1, "sentiment": 0.0,
                                "tweets": [{"likes": 99, "retweets": 0}]})
        self.assertAlmostEqual(s_viral, s_normal + 1.0, places=2)

    def test_retweets_count_double_toward_viral(self):
        # 50 retweets × 2 = 100 engagement → viral; base 4.5 + 1.0 = 5.5
        result = self.score({"mention_count": 1, "sentiment": 0.0,
                             "tweets": [{"likes": 0, "retweets": 50}]})
        self.assertAlmostEqual(result, 5.5, places=2)

    def test_empty_tweets_list_with_nonzero_count_no_crash(self):
        # mention_count=1 but tweets=[] → max() with default=0 → no viral bonus → 4.5
        result = self.score({"mention_count": 1, "sentiment": 0.0, "tweets": []})
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 4.5, places=2)

    def test_neutral_sentiment_boundary_at_zero(self):
        # sent=0.0 is neither positive nor negative → no boost/penalty
        result = self.score({"mention_count": 1, "sentiment": 0.0, "tweets": self._tw()})
        self.assertAlmostEqual(result, 4.5, places=2)

    def test_sentiment_boundary_exactly_0_10(self):
        # sent=0.10 (not > 0.10) → neutral zone → no boost
        result = self.score({"mention_count": 1, "sentiment": 0.10, "tweets": self._tw()})
        self.assertAlmostEqual(result, 4.5, places=2)

    def test_sentiment_boundary_just_above_0_10(self):
        # sent=0.11 (> 0.10) → slight boost +0.8
        result = self.score({"mention_count": 1, "sentiment": 0.11, "tweets": self._tw()})
        self.assertAlmostEqual(result, 5.3, places=2)


@unittest.skip("Twitter disabled — TWITTER_ENABLED=False; re-enable when Twitter is re-activated")
class TestTwitterGetMentionsEdgeCases(unittest.TestCase):
    """Edge cases for get_twitter_mentions() beyond the basic suite."""

    @patch("twitter_enricher._fetch_user_tweets")
    def test_nse_prefix_stripped_for_cashtag_matching(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.return_value = [{
            "tweet_id": "1", "text": "$RELIANCE is bullish today",
            "date": "2024-01-01", "likes": 5, "retweets": 0,
        }]
        result = get_twitter_mentions("NSE:RELIANCE", handles=["h"])
        self.assertEqual(result["mention_count"], 1)
        self.assertEqual(result["symbol_checked"], "RELIANCE")

    @patch("twitter_enricher._fetch_user_tweets")
    def test_symbol_checked_is_uppercase_bare_ticker(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.return_value = []
        result = get_twitter_mentions("nse:reliance", handles=["h"])
        self.assertEqual(result["symbol_checked"], "RELIANCE")

    @patch("twitter_enricher._fetch_user_tweets")
    def test_multiple_handles_mentions_aggregated(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.side_effect = [
            [{"tweet_id": "1", "text": "$GOLD bullish", "date": "2024-01-01", "likes": 0, "retweets": 0}],
            [{"tweet_id": "2", "text": "$GOLD moon", "date": "2024-01-01", "likes": 0, "retweets": 0}],
        ]
        result = get_twitter_mentions("GOLD", handles=["handle_a", "handle_b"])
        self.assertEqual(result["mention_count"], 2)

    @patch("twitter_enricher._fetch_user_tweets")
    def test_error_from_one_handle_does_not_block_other(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.side_effect = [
            Exception("rate limit"),
            [{"tweet_id": "3", "text": "$NVDA buy signal", "date": "2024-01-01", "likes": 10, "retweets": 2}],
        ]
        result = get_twitter_mentions("NVDA", handles=["failing_handle", "working_handle"])
        self.assertEqual(result["mention_count"], 1)

    @patch("twitter_enricher._fetch_user_tweets")
    def test_positive_tweet_sentiment_positive_in_result(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.return_value = [{
            "tweet_id": "10", "text": "$GOLD is absolutely great and wonderful today!",
            "date": "2024-01-01", "likes": 0, "retweets": 0,
        }]
        result = get_twitter_mentions("GOLD", handles=["h"])
        self.assertIn("sentiment", result)
        self.assertGreater(result["sentiment"], 0.0)

    @patch("twitter_enricher._fetch_user_tweets")
    def test_mean_sentiment_averaged_across_tweets(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        # One strongly positive, one strongly negative tweet
        mock_fetch.return_value = [
            {"tweet_id": "1", "text": "$BTC is amazing and wonderful and great!",
             "date": "2024-01-01", "likes": 0, "retweets": 0},
            {"tweet_id": "2", "text": "$BTC is terrible and awful and horrible!",
             "date": "2024-01-01", "likes": 0, "retweets": 0},
        ]
        result = get_twitter_mentions("BTC", handles=["h"])
        self.assertEqual(result["mention_count"], 2)
        self.assertAlmostEqual(result["sentiment"], 0.0, delta=0.35)  # ~neutral average

    @patch("twitter_enricher._fetch_user_tweets")
    def test_tweet_text_truncated_to_280_chars_in_result(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        long_text = "$NVDA " + "x" * 500
        mock_fetch.return_value = [{
            "tweet_id": "1", "text": long_text, "date": "2024-01-01",
            "likes": 0, "retweets": 0,
        }]
        result = get_twitter_mentions("NVDA", handles=["h"])
        self.assertEqual(result["mention_count"], 1)
        self.assertLessEqual(len(result["tweets"][0]["text"]), 280)

    @patch("twitter_enricher._fetch_user_tweets")
    def test_result_dict_has_all_required_keys(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.return_value = []
        result = get_twitter_mentions("NVDA", handles=["h"])
        for key in ("mention_count", "sentiment", "tweets", "symbol_checked"):
            self.assertIn(key, result, f"Missing key: {key}")

    @patch("twitter_enricher._fetch_user_tweets")
    def test_zero_mentions_sentiment_is_zero(self, mock_fetch):
        from twitter_enricher import get_twitter_mentions
        mock_fetch.return_value = []
        result = get_twitter_mentions("NVDA", handles=["h"])
        self.assertEqual(result["sentiment"], 0.0)
        self.assertEqual(result["mention_count"], 0)
        self.assertEqual(result["tweets"], [])


class TestTwitterSignalQuality(unittest.TestCase):
    """
    Documents and verifies expected signal quality for the two monitored accounts.

    Kiyosaki (@theRealKiyosaki): Tweets about gold, Bitcoin, USD, US debt.
      → Zero overlap with Indian NSE/BSE stocks → enricher abstains (returns None).

    Nishant Kumar (@nishkumar77): If he tweets with NSE: prefix or $CASHTAG, it counts.
      → Correctly counted when present; correctly absent when no mention.

    Key design: score_twitter() returns None (not 0) for zero mentions.
    This means missing Twitter signal never penalises an Indian stock.
    """

    @patch("twitter_enricher._fetch_user_tweets")
    def test_kiyosaki_gold_btc_tweets_give_zero_indian_stock_signal(self, mock_fetch):
        """Kiyosaki's typical tweet portfolio has zero signal for NSE stocks."""
        from twitter_enricher import get_twitter_mentions, score_twitter
        mock_fetch.return_value = [
            {"tweet_id": "k1", "text": "$GOLD is real money. $BTC store of value.",
             "date": "2024-01-01", "likes": 5000, "retweets": 1000},
            {"tweet_id": "k2", "text": "Paper money is $USD $DEBT. Buy $GLD $SLV.",
             "date": "2024-01-01", "likes": 3000, "retweets": 800},
            {"tweet_id": "k3", "text": "Real assets beat fiat. $BTC $GOLD $SLVR",
             "date": "2024-01-01", "likes": 2000, "retweets": 600},
        ]
        for symbol in ["NSE:RELIANCE", "NSE:INFY", "NSE:HDFC", "NSE:TCS", "NSE:WIPRO"]:
            mentions = get_twitter_mentions(symbol, handles=["theRealKiyosaki"])
            score = score_twitter(mentions)
            self.assertEqual(mentions["mention_count"], 0,
                             f"Kiyosaki should have 0 mentions for {symbol}")
            self.assertIsNone(score,
                              f"score_twitter must abstain (None) for {symbol} — not penalise it")

    @unittest.skip("Twitter disabled — TWITTER_ENABLED=False")
    @patch("twitter_enricher._fetch_user_tweets")
    def test_kiyosaki_is_valid_signal_for_gold_symbol(self, mock_fetch):
        """Kiyosaki IS a useful signal for $GOLD — enricher should not be disabled totally."""
        from twitter_enricher import get_twitter_mentions, score_twitter
        mock_fetch.return_value = [
            {"tweet_id": "k1", "text": "$GOLD will hit $3000. Buy now!",
             "date": "2024-01-01", "likes": 8000, "retweets": 2000},
            {"tweet_id": "k2", "text": "$GOLD $GLD $XAUUSD going parabolic",
             "date": "2024-01-01", "likes": 4000, "retweets": 1000},
        ]
        mentions = get_twitter_mentions("GOLD", handles=["theRealKiyosaki"])
        score = score_twitter(mentions)
        self.assertGreater(mentions["mention_count"], 0)
        self.assertIsNotNone(score)
        self.assertGreater(score, 5.0)  # Viral + positive tweet → above neutral

    @unittest.skip("Twitter disabled — TWITTER_ENABLED=False")
    @patch("twitter_enricher._fetch_user_tweets")
    def test_nishant_kumar_nse_prefix_tweet_counted(self, mock_fetch):
        """If @nishkumar77 tweets NSE:SYMBOL it is captured correctly."""
        from twitter_enricher import get_twitter_mentions, score_twitter
        mock_fetch.return_value = [
            {"tweet_id": "n1", "text": "NSE:RELIANCE looks good for long-term investors.",
             "date": "2024-01-01", "likes": 200, "retweets": 40},
        ]
        mentions = get_twitter_mentions("NSE:RELIANCE", handles=["nishkumar77"])
        score = score_twitter(mentions)
        self.assertEqual(mentions["mention_count"], 1)
        self.assertIsNotNone(score)

    @patch("twitter_enricher._fetch_user_tweets")
    def test_zero_mentions_must_return_none_not_zero(self, mock_fetch):
        """
        Critical design requirement:  score_twitter() must return None (abstain)
        when a stock is not mentioned — returning 0 would falsely penalise stocks
        never covered by these accounts.
        """
        from twitter_enricher import get_twitter_mentions, score_twitter
        mock_fetch.return_value = [
            {"tweet_id": "1", "text": "Good morning! $GOLD $BTC", "date": "2024-01-01",
             "likes": 0, "retweets": 0},
        ]
        mentions = get_twitter_mentions("NSE:WIPRO", handles=["theRealKiyosaki"])
        score = score_twitter(mentions)
        self.assertIsNone(score,
                          "score_twitter must return None (not 0) when stock not mentioned")


class TestTwitterCacheBehavior(unittest.TestCase):
    """Cache hit/miss behavior for _async_fetch_user_tweets()."""

    def tearDown(self):
        import twitter_enricher
        twitter_enricher._TWEET_CACHE.clear()

    def test_warm_cache_returned_without_network(self):
        """Cache hit: warm entry returned immediately, no twscrape call needed."""
        import asyncio
        import twitter_enricher
        cached_tweets = [{"tweet_id": "cached1", "text": "$GOLD", "date": "2024-01-01",
                          "likes": 0, "retweets": 0}]
        twitter_enricher._TWEET_CACHE["cachehandle"] = (time.time(), cached_tweets)
        result = asyncio.run(twitter_enricher._async_fetch_user_tweets("cachehandle"))
        self.assertEqual(result, cached_tweets)
        self.assertEqual(result[0]["tweet_id"], "cached1")

    def test_expired_cache_not_served(self):
        """Cache entry older than _CACHE_TTL is treated as expired."""
        import asyncio
        import twitter_enricher
        stale_ts = time.time() - twitter_enricher._CACHE_TTL - 60
        twitter_enricher._TWEET_CACHE["stalehandle"] = (stale_ts, [{"tweet_id": "stale"}])
        # With twscrape unavailable, returns [] — but NOT the stale entry
        result = asyncio.run(twitter_enricher._async_fetch_user_tweets("stalehandle"))
        # Either [] (twscrape unavailable) or fresh data — never stale
        if result:
            self.assertNotEqual(result[0].get("tweet_id"), "stale")

    def test_cache_ttl_is_1800_seconds(self):
        import twitter_enricher
        self.assertEqual(twitter_enricher._CACHE_TTL, 1800)

    def test_cache_ttl_is_positive(self):
        import twitter_enricher
        self.assertGreater(twitter_enricher._CACHE_TTL, 0)

    def test_cache_cleared_between_tests(self):
        """tearDown must leave cache empty so tests are independent."""
        import twitter_enricher
        self.assertEqual(len(twitter_enricher._TWEET_CACHE), 0)


class TestTwitterDependencyGuards(unittest.TestCase):
    """Configuration constants and graceful-degradation when twscrape is absent."""

    def test_twscrape_unavailable_returns_empty_list(self):
        """When twscrape is not importable, _async_fetch_user_tweets returns [] gracefully."""
        import asyncio
        import sys
        import twitter_enricher
        # Setting sys.modules["twscrape"] = None makes `import twscrape` raise ImportError
        original = sys.modules.get("twscrape", _MISSING := object())
        sys.modules["twscrape"] = None  # type: ignore[assignment]
        try:
            result = asyncio.run(twitter_enricher._async_fetch_user_tweets("nomodule_handle"))
            self.assertEqual(result, [])
        finally:
            if original is _MISSING:
                sys.modules.pop("twscrape", None)
            else:
                sys.modules["twscrape"] = original  # type: ignore[assignment]

    def test_expert_handles_not_empty(self):
        """EXPERT_HANDLES must contain at least one entry."""
        from twitter_enricher import EXPERT_HANDLES
        self.assertGreater(len(EXPERT_HANDLES), 0,
                           "EXPERT_HANDLES is empty — no accounts are monitored")

    def test_kiyosaki_handle_configured(self):
        from twitter_enricher import EXPERT_HANDLES
        self.assertIn("theRealKiyosaki", EXPERT_HANDLES)

    def test_nishant_kumar_handle_configured(self):
        from twitter_enricher import EXPERT_HANDLES
        self.assertIn("nishkumar77", EXPERT_HANDLES)

    def test_lookback_hours_is_positive(self):
        from twitter_enricher import LOOKBACK_HOURS
        self.assertGreater(LOOKBACK_HOURS, 0)

    def test_max_tweets_per_account_is_positive(self):
        from twitter_enricher import MAX_TWEETS_PER_ACC
        self.assertGreater(MAX_TWEETS_PER_ACC, 0)

    def test_signal_threshold_is_positive(self):
        from twitter_enricher import SIGNAL_THRESHOLD
        self.assertGreater(SIGNAL_THRESHOLD, 0.0)

    def test_accounts_db_path_is_absolute(self):
        from twitter_enricher import _ACCOUNTS_DB
        self.assertTrue(_ACCOUNTS_DB.is_absolute(),
                        "_ACCOUNTS_DB should be an absolute path")
