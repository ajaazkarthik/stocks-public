"""
Tests for tv_news_screener.py
Run: pytest test_tv_news_screener.py -v
All tests are offline — no real network calls (fetch_headlines / fetch_story mocked).
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from tv_news_screener import (
    SOURCE_NEWS,
    PROVIDER_WEIGHTS,
    _DEFAULT_PROVIDERS,
    NewsSignal,
    _score_text,
    _is_too_old,
    _extract_symbols,
    _make_news_fire,
    fetch_news_signals,
    news_to_fires,
    news_scan_meta,
)


# ─────────────────────────────────────────────────────────────────────────────
# Source constant
# ─────────────────────────────────────────────────────────────────────────────
class TestSourceConstant(unittest.TestCase):
    def test_source_news_value(self):
        self.assertEqual(SOURCE_NEWS, "news")

    def test_provider_weights_present(self):
        for p in ("quartr", "reuters", "dow-jones", "market-watch", "invezz"):
            self.assertIn(p, PROVIDER_WEIGHTS)

    def test_quartr_highest_weight(self):
        self.assertGreater(PROVIDER_WEIGHTS["quartr"], PROVIDER_WEIGHTS["market-watch"])

    def test_default_providers_not_empty(self):
        self.assertGreater(len(_DEFAULT_PROVIDERS), 0)


# ─────────────────────────────────────────────────────────────────────────────
# _score_text
# ─────────────────────────────────────────────────────────────────────────────
class TestScoreText(unittest.TestCase):
    def test_empty_string_returns_zero(self):
        self.assertEqual(_score_text(""), 0.0)

    def test_returns_float(self):
        result = _score_text("some text")
        self.assertIsInstance(result, float)

    def test_range_valid(self):
        result = _score_text("earnings beat expectations sharply")
        self.assertGreaterEqual(result, -1.0)
        self.assertLessEqual(result, 1.0)

    @patch("tv_news_screener._VADER_OK", False)
    def test_vader_unavailable_returns_zero(self):
        self.assertEqual(_score_text("strong earnings"), 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# _is_too_old
# ─────────────────────────────────────────────────────────────────────────────
class TestIsTooOld(unittest.TestCase):
    def test_zero_published_not_old(self):
        self.assertFalse(_is_too_old(0, 3600))

    def test_none_published_not_old(self):
        self.assertFalse(_is_too_old(None, 3600))  # type: ignore[arg-type]

    def test_recent_article_not_old(self):
        now = time.time()
        self.assertFalse(_is_too_old(now - 100, 3600))

    def test_old_article_flagged(self):
        old_ts = time.time() - 90_000   # 25 hours ago
        self.assertTrue(_is_too_old(old_ts, 86_400))

    def test_exactly_at_boundary_not_old(self):
        boundary_ts = time.time() - 3600 + 10  # just inside window
        self.assertFalse(_is_too_old(boundary_ts, 3600))


# ─────────────────────────────────────────────────────────────────────────────
# _extract_symbols
# ─────────────────────────────────────────────────────────────────────────────
class TestExtractSymbols(unittest.TestCase):
    def test_empty_item(self):
        self.assertEqual(_extract_symbols({}), [])

    def test_none_related(self):
        self.assertEqual(_extract_symbols({"relatedSymbols": None}), [])

    def test_single_symbol(self):
        item = {"relatedSymbols": [{"symbol": "NASDAQ:NVDA", "logoid": "nvidia"}]}
        self.assertEqual(_extract_symbols(item), ["NASDAQ:NVDA"])

    def test_multiple_symbols(self):
        item = {"relatedSymbols": [
            {"symbol": "NASDAQ:NVDA"},
            {"symbol": "NYSE:ORCL"},
        ]}
        syms = _extract_symbols(item)
        self.assertIn("NASDAQ:NVDA", syms)
        self.assertIn("NYSE:ORCL", syms)

    def test_symbol_without_colon_excluded(self):
        item = {"relatedSymbols": [{"symbol": "NVDA"}]}
        self.assertEqual(_extract_symbols(item), [])

    def test_symbols_uppercased(self):
        item = {"relatedSymbols": [{"symbol": "nasdaq:nvda"}]}
        syms = _extract_symbols(item)
        self.assertTrue(all(s == s.upper() for s in syms))

    def test_empty_symbol_excluded(self):
        item = {"relatedSymbols": [{"symbol": ""}, {"symbol": "NYSE:JPM"}]}
        self.assertEqual(_extract_symbols(item), ["NYSE:JPM"])


# ─────────────────────────────────────────────────────────────────────────────
# _make_news_fire
# ─────────────────────────────────────────────────────────────────────────────
class TestMakeNewsFire(unittest.TestCase):
    def _bullish_signal(self) -> NewsSignal:
        return NewsSignal(
            pro_symbol="NASDAQ:NVDA",
            compound=0.55,
            bearish=False,
            articles=3,
            providers=["quartr", "reuters"],
            titles=["NVDA smashes earnings estimates again"],
        )

    def _bearish_signal(self) -> NewsSignal:
        return NewsSignal(
            pro_symbol="NYSE:GS",
            compound=-0.48,
            bearish=True,
            articles=2,
            providers=["dow-jones"],
            titles=["Goldman Sachs faces regulatory probe"],
        )

    def test_bullish_fire_structure(self):
        fire = _make_news_fire(self._bullish_signal())
        self.assertEqual(fire["pro_symbol"],   "NASDAQ:NVDA")
        self.assertEqual(fire["symbol"],       "NASDAQ:NVDA")
        self.assertEqual(fire["_source"],      "news")
        self.assertFalse(fire["_bearish"])
        self.assertEqual(fire["_news_compound"], 0.55)
        self.assertEqual(fire["_news_articles"], 3)

    def test_bearish_fire_structure(self):
        fire = _make_news_fire(self._bearish_signal())
        self.assertEqual(fire["pro_symbol"],   "NYSE:GS")
        self.assertTrue(fire["_bearish"])
        self.assertEqual(fire["_news_compound"], -0.48)

    def test_fire_has_required_pipeline_keys(self):
        fire = _make_news_fire(self._bullish_signal())
        for key in ("fire_id", "alert_id", "symbol", "pro_symbol", "resolution",
                    "message", "fire_time", "bar_time", "name", "kinds",
                    "_source", "_bearish"):
            self.assertIn(key, fire, f"Missing required key: {key}")

    def test_message_contains_sentiment(self):
        fire = _make_news_fire(self._bullish_signal())
        self.assertIn("sentiment:", fire["message"])
        self.assertIn("BUY", fire["message"])

    def test_bearish_message_contains_bear(self):
        fire = _make_news_fire(self._bearish_signal())
        self.assertIn("BEAR", fire["message"])

    def test_kinds_contains_source_news(self):
        fire = _make_news_fire(self._bullish_signal())
        self.assertIn("news", fire["kinds"])

    def test_fire_id_is_int(self):
        fire = _make_news_fire(self._bullish_signal())
        self.assertIsInstance(fire["fire_id"], int)

    def test_news_providers_stored(self):
        fire = _make_news_fire(self._bullish_signal())
        self.assertIn("quartr", fire["_news_providers"])

    def test_news_titles_stored(self):
        fire = _make_news_fire(self._bullish_signal())
        self.assertIn("NVDA smashes earnings estimates again", fire["_news_titles"])

    def test_resolution_daily(self):
        fire = _make_news_fire(self._bullish_signal())
        self.assertEqual(fire["resolution"], "D")


# ─────────────────────────────────────────────────────────────────────────────
# fetch_news_signals — with mocked fetch_headlines and fetch_story
# ─────────────────────────────────────────────────────────────────────────────
_MOCK_HEADLINE_NVDA = {
    "id": "tag:reuters.com,2024:newsml_NVDA001",
    "title": "NVIDIA earnings beat expectations by 40 percent",
    "provider": "reuters",
    "published": int(time.time()) - 3600,   # 1 hour ago → within 24h window
    "relatedSymbols": [{"symbol": "NASDAQ:NVDA", "logoid": "nvidia"}],
    "storyPath": "/news/nvda-earnings",
}

_MOCK_HEADLINE_OLD = {
    "id": "tag:reuters.com,2024:newsml_OLD001",
    "title": "Old news article nobody cares about",
    "provider": "reuters",
    "published": int(time.time()) - 200_000,   # very old — should be filtered
    "relatedSymbols": [{"symbol": "NYSE:AA", "logoid": "alcoa"}],
    "storyPath": "/news/old-article",
}

_MOCK_HEADLINE_BEARISH = {
    "id": "tag:dj.com,2024:newsml_GS001",
    "title": "Goldman Sachs faces massive regulatory investigation",
    "provider": "dow-jones",
    "published": int(time.time()) - 1800,
    "relatedSymbols": [{"symbol": "NYSE:GS", "logoid": "goldman-sachs"}],
    "storyPath": "/news/gs-probe",
}

_MOCK_HEADLINE_NO_SYMBOLS = {
    "id": "tag:reuters.com,2024:newsml_GEN001",
    "title": "Global markets show mixed performance on Monday",
    "provider": "reuters",
    "published": int(time.time()) - 3600,
    "relatedSymbols": [],   # no symbols → should be ignored
    "storyPath": "/news/markets",
}


class TestFetchNewsSignals(unittest.TestCase):

    def setUp(self):
        # Prevent real moomoo-community network calls during offline unit tests
        self._community_patcher = patch(
            "moomoo_community_intel.get_community_headlines", return_value=[]
        )
        self._community_patcher.start()

    def tearDown(self):
        self._community_patcher.stop()

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_bullish_signal_found(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_NVDA]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": 0.8}
            bullish, bearish = fetch_news_signals(
                providers=["reuters"],
                bullish_threshold=0.35,
                bearish_threshold=-0.35,
                fetch_body_n=0,
            )
        self.assertEqual(len(bullish), 1)
        self.assertEqual(bullish[0].pro_symbol, "NASDAQ:NVDA")
        self.assertFalse(bullish[0].bearish)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_bearish_signal_found(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_BEARISH]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": -0.7}
            bullish, bearish = fetch_news_signals(
                providers=["dow-jones"],
                bullish_threshold=0.35,
                bearish_threshold=-0.35,
                fetch_body_n=0,
            )
        self.assertEqual(len(bearish), 1)
        self.assertEqual(bearish[0].pro_symbol, "NYSE:GS")
        self.assertTrue(bearish[0].bearish)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_old_articles_filtered(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_OLD]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": 0.9}
            bullish, bearish = fetch_news_signals(
                providers=["reuters"],
                max_age_hours=24.0,
                fetch_body_n=0,
            )
        # Old article should be excluded → no signals
        self.assertEqual(len(bullish) + len(bearish), 0)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_articles_without_symbols_excluded(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_NO_SYMBOLS]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": 0.9}
            bullish, bearish = fetch_news_signals(
                providers=["reuters"], fetch_body_n=0,
            )
        self.assertEqual(len(bullish) + len(bearish), 0)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_exchange_filter_applied(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_NVDA, _MOCK_HEADLINE_BEARISH]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": 0.8}
            bullish, bearish = fetch_news_signals(
                providers=["reuters", "dow-jones"],
                exchange_filter=["NASDAQ"],   # only NASDAQ
                fetch_body_n=0,
            )
        all_syms = [s.pro_symbol for s in bullish + bearish]
        # NYSE:GS should be excluded by exchange filter
        self.assertNotIn("NYSE:GS", all_syms)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_below_bullish_threshold_excluded(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_NVDA]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": 0.1}  # below 0.35
            bullish, bearish = fetch_news_signals(
                providers=["reuters"], fetch_body_n=0,
                bullish_threshold=0.35, bearish_threshold=-0.35,
            )
        self.assertEqual(len(bullish), 0)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_api_failure_returns_empty(self, mock_hl, mock_story):
        mock_hl.return_value = []   # API returns nothing
        bullish, bearish = fetch_news_signals(providers=["reuters"], fetch_body_n=0)
        self.assertEqual(len(bullish) + len(bearish), 0)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_bullish_sorted_by_compound_descending(self, mock_hl, mock_story):
        high = {**_MOCK_HEADLINE_NVDA, "id": "h1",
                "relatedSymbols": [{"symbol": "NASDAQ:NVDA"}]}
        low  = {**_MOCK_HEADLINE_NVDA, "id": "h2",
                "relatedSymbols": [{"symbol": "NYSE:AAPL"}],
                "title": "Apple mildly positive news"}
        mock_hl.return_value = [low, high]
        compound_map = {
            "NVIDIA earnings beat expectations by 40 percent": 0.9,
            "Apple mildly positive news": 0.4,
        }
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.side_effect = lambda text: {
                "compound": compound_map.get(text, 0.0)
            }
            bullish, _ = fetch_news_signals(
                providers=["reuters"], fetch_body_n=0,
                bullish_threshold=0.35,
            )
        if len(bullish) >= 2:
            self.assertGreaterEqual(bullish[0].compound, bullish[1].compound)


# ─────────────────────────────────────────────────────────────────────────────
# news_to_fires — fire dict slice
# ─────────────────────────────────────────────────────────────────────────────
class TestNewsToFires(unittest.TestCase):

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_returns_tuple_of_two_lists(self, mock_hl, mock_story):
        mock_hl.return_value = []
        result = news_to_fires(providers=["reuters"])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], list)
        self.assertIsInstance(result[1], list)

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_bullish_fire_has_bearish_false(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_NVDA]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": 0.8}
            bullish, _ = news_to_fires(
                providers=["reuters"], fetch_body_n=0,
            )
        if bullish:
            self.assertFalse(bullish[0]["_bearish"])
            self.assertEqual(bullish[0]["_source"], "news")

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_bearish_fire_has_bearish_true(self, mock_hl, mock_story):
        mock_hl.return_value = [_MOCK_HEADLINE_BEARISH]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": -0.7}
            _, bearish = news_to_fires(
                providers=["dow-jones"], fetch_body_n=0,
            )
        if bearish:
            self.assertTrue(bearish[0]["_bearish"])

    @patch("tv_news_screener.fetch_story", return_value={})
    @patch("tv_news_screener.fetch_headlines")
    def test_no_duplicate_symbols(self, mock_hl, mock_story):
        # Same symbol appears twice (same symbol in both bullish and bearish won't
        # happen due to thresholds, but two articles for same symbol → one fire).
        duplicate = {**_MOCK_HEADLINE_NVDA, "id": "dup1"}
        mock_hl.return_value = [_MOCK_HEADLINE_NVDA, duplicate]
        with patch("tv_news_screener._VADER_OK", True), \
             patch("tv_news_screener._vader") as mock_vader:
            mock_vader.polarity_scores.return_value = {"compound": 0.8}
            bullish, _ = news_to_fires(
                providers=["reuters"], fetch_body_n=0,
            )
        symbols = [f["pro_symbol"] for f in bullish]
        self.assertEqual(len(symbols), len(set(symbols)), "Duplicate symbols in output")


# ─────────────────────────────────────────────────────────────────────────────
# news_scan_meta
# ─────────────────────────────────────────────────────────────────────────────
class TestNewsScanMeta(unittest.TestCase):
    def test_meta_structure(self):
        meta = news_scan_meta(
            bullish_fires=[{"pro_symbol": "NASDAQ:NVDA"}],
            bearish_fires=[{"pro_symbol": "NYSE:GS"}, {"pro_symbol": "NASDAQ:TSLA"}],
            providers=["reuters", "quartr"],
            max_age_hours=24.0,
            bullish_threshold=0.35,
            bearish_threshold=-0.35,
        )
        self.assertEqual(meta["bullish_signals"], 1)
        self.assertEqual(meta["bearish_signals"], 2)
        self.assertEqual(meta["total_signals"], 3)
        self.assertIn("generated_at", meta)
        self.assertIn("vader_available", meta)
        self.assertIn("tv_news_available", meta)

    def test_empty_fires(self):
        meta = news_scan_meta([], [], None, 24.0, 0.35, -0.35)
        self.assertEqual(meta["total_signals"], 0)

    def test_default_providers_used_when_none(self):
        meta = news_scan_meta([], [], None, 24.0, 0.35, -0.35)
        self.assertEqual(meta["providers_queried"], _DEFAULT_PROVIDERS)

    def test_explicit_providers_stored(self):
        meta = news_scan_meta([], [], ["reuters"], 24.0, 0.35, -0.35)
        self.assertEqual(meta["providers_queried"], ["reuters"])


# ─────────────────────────────────────────────────────────────────────────────
# NewsSignal namedtuple
# ─────────────────────────────────────────────────────────────────────────────
class TestNewsSignalTuple(unittest.TestCase):
    def test_fields_accessible(self):
        sig = NewsSignal(
            pro_symbol="NASDAQ:NVDA",
            compound=0.55,
            bearish=False,
            articles=3,
            providers=["reuters"],
            titles=["headline"],
        )
        self.assertEqual(sig.pro_symbol, "NASDAQ:NVDA")
        self.assertEqual(sig.compound, 0.55)
        self.assertFalse(sig.bearish)
        self.assertEqual(sig.articles, 3)

    def test_bearish_signal(self):
        sig = NewsSignal("NYSE:GS", -0.4, True, 1, ["dow-jones"], [])
        self.assertTrue(sig.bearish)
        self.assertLess(sig.compound, 0)


# ─────────────────────────────────────────────────────────────────────────────
# input_sources integration — SOURCE_NEWS constant
# ─────────────────────────────────────────────────────────────────────────────
class TestInputSourcesIntegration(unittest.TestCase):
    def test_source_news_imported_in_input_sources(self):
        try:
            from input_sources import SOURCE_NEWS as _sn
            self.assertEqual(_sn, "news")
        except ImportError:
            self.skipTest("input_sources not importable in this env")

    def test_news_to_fires_importable_from_input_sources(self):
        try:
            from input_sources import news_to_fires as _ntf
            self.assertTrue(callable(_ntf))
        except ImportError:
            self.skipTest("input_sources not importable in this env")


if __name__ == "__main__":
    unittest.main()
