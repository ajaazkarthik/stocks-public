"""
tests/test_parallel_fetch.py
═════════════════════════════
Tests for parallel API fetching in signal_stack.py and tv_news.py.

Validates that:
  1. fetch_story_snippets() uses ThreadPoolExecutor for parallel fetching
  2. L3 news sources (Reuters, DJ, Quartr, MarketWatch) are fetched in parallel
  3. Options badge post-rank uses ThreadPoolExecutor
  4. Individual fetch failures don't crash the parallel pipeline
  5. Results are correct regardless of parallel execution order
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Bootstrap ─────────────────────────────────────────────────────────────────
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

ROOT = Path(__file__).parent.parent
TV   = ROOT / "tradingview"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TV))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. fetch_story_snippets — parallel story body fetching
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchStorySnippetsParallel:
    """Tests for tv_data/tv_news.py::fetch_story_snippets()."""

    def test_parallel_fetch_returns_correct_snippets(self):
        """All 3 stories fetched and returned."""
        from tv_data.tv_news import fetch_story_snippets

        items = [
            {"id": "story_1", "title": "Headline 1"},
            {"id": "story_2", "title": "Headline 2"},
            {"id": "story_3", "title": "Headline 3"},
        ]

        def _mock_fetch_story(story_id, timeout=8):
            return {"shortDescription": f"Description for {story_id}"}

        with patch("tv_data.tv_news.fetch_story", side_effect=_mock_fetch_story):
            snippets = fetch_story_snippets(items, n=3, timeout=8)

        assert len(snippets) == 3
        assert all("Description for story_" in s for s in snippets)

    def test_parallel_fetch_skips_empty_ids(self):
        """Items without 'id' key are skipped."""
        from tv_data.tv_news import fetch_story_snippets

        items = [
            {"id": "story_1"},
            {"title": "No ID"},
            {"id": "", "title": "Empty ID"},
            {"id": "story_4"},
        ]

        def _mock_fetch_story(story_id, timeout=8):
            return {"shortDescription": f"Desc {story_id}"}

        with patch("tv_data.tv_news.fetch_story", side_effect=_mock_fetch_story):
            snippets = fetch_story_snippets(items, n=4, timeout=8)

        # Only story_1 and story_4 have valid IDs
        assert len(snippets) == 2

    def test_parallel_fetch_handles_individual_failures(self):
        """One story fetch failing doesn't crash others."""
        from tv_data.tv_news import fetch_story_snippets

        items = [
            {"id": "ok_1"},
            {"id": "fail_2"},
            {"id": "ok_3"},
        ]

        def _mock_fetch_story(story_id, timeout=8):
            if "fail" in story_id:
                raise ConnectionError("Network error")
            return {"shortDescription": f"Good {story_id}"}

        with patch("tv_data.tv_news.fetch_story", side_effect=_mock_fetch_story):
            snippets = fetch_story_snippets(items, n=3, timeout=8)

        # 2 of 3 should succeed
        assert len(snippets) == 2
        assert all("Good" in s for s in snippets)

    def test_parallel_fetch_empty_input_returns_empty(self):
        """Empty input returns empty list without spawning threads."""
        from tv_data.tv_news import fetch_story_snippets

        result = fetch_story_snippets([], n=3, timeout=8)
        assert result == []

    def test_parallel_fetch_skips_empty_descriptions(self):
        """Stories with empty shortDescription are filtered out."""
        from tv_data.tv_news import fetch_story_snippets

        items = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]

        def _mock_fetch_story(story_id, timeout=8):
            if story_id == "s2":
                return {"shortDescription": ""}
            return {"shortDescription": f"Content {story_id}"}

        with patch("tv_data.tv_news.fetch_story", side_effect=_mock_fetch_story):
            snippets = fetch_story_snippets(items, n=3, timeout=8)

        assert len(snippets) == 2

    def test_parallel_fetch_is_actually_parallel(self):
        """Verify parallelism by checking wall-clock time is less than serial."""
        from tv_data.tv_news import fetch_story_snippets

        items = [{"id": f"s{i}"} for i in range(3)]

        def _slow_fetch(story_id, timeout=8):
            time.sleep(0.2)  # 200ms per story
            return {"shortDescription": f"Content {story_id}"}

        with patch("tv_data.tv_news.fetch_story", side_effect=_slow_fetch):
            t0 = time.perf_counter()
            snippets = fetch_story_snippets(items, n=3, timeout=8)
            elapsed = time.perf_counter() - t0

        assert len(snippets) == 3
        # Serial would take ~600ms; parallel should take ~200-300ms
        assert elapsed < 0.5, f"Expected parallel execution < 500ms, got {elapsed:.3f}s"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. L3 News — parallel headline fetching (signal_stack.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestL3NewsParallelFetch:
    """Tests for parallel L3 news headline fetching in fetch_sentiment_data()."""

    def test_parallel_news_fetch_returns_all_sources(self):
        """All 4 news sources (Reuters, DJ, Quartr, MarketWatch) are fetched."""
        from signal_stack import fetch_sentiment_data

        mock_ticker = MagicMock()
        mock_ticker.recommendations = None
        mock_ticker.upgrades_downgrades = None
        mock_ticker.earnings_history = None
        mock_ticker.calendar = {}

        tv_fund = {
            "recommendation_mark": 2.0,
            "earnings_release_next_date": None,
        }

        def _mock_headlines(symbol="", provider="", limit=15):
            return [{"title": f"{provider} headline {i}", "id": f"{provider}_{i}"}
                    for i in range(3)]

        def _mock_symbol_news(symbol, providers=None, limit=10):
            prov = providers[0] if providers else "unknown"
            return [f"{prov} item {i}" for i in range(2)]

        def _mock_story_snippets(items, n=3):
            return [f"story {i}" for i in range(min(n, len(items)))]

        with patch("signal_stack._tv_fetch_headlines", side_effect=_mock_headlines), \
             patch("signal_stack._tv_fetch_symbol_news", side_effect=_mock_symbol_news), \
             patch("signal_stack._tv_fetch_story_snippets", side_effect=_mock_story_snippets), \
             patch("signal_stack._TV_NEWS_OK", True), \
             patch("signal_stack._yf_has_quotesummary", return_value=True), \
             patch("signal_stack.fetch_google_news", return_value=[]), \
             patch("signal_stack.fetch_yf_news", return_value=[]), \
             patch("signal_stack.compute_news_sentiment", return_value={"score": 0.3}):
            result = fetch_sentiment_data(
                ticker_obj=mock_ticker,
                tv_fund=tv_fund,
                tv_symbol="NASDAQ:AAPL",
                company_name="Apple Inc",
            )

        news = result.get("news", {})
        assert news.get("wire_count", 0) > 0, "Wire headlines (Reuters+DJ) should be fetched"
        assert news.get("quartr_count", 0) > 0, "Quartr headlines should be fetched"
        assert news.get("mwatch_count", 0) > 0, "MarketWatch headlines should be fetched"

    def test_parallel_news_reuters_failure_doesnt_crash(self):
        """If Reuters fails, DJ + Quartr + MarketWatch still work."""
        from signal_stack import fetch_sentiment_data

        mock_ticker = MagicMock()
        mock_ticker.recommendations = None
        mock_ticker.upgrades_downgrades = None
        mock_ticker.earnings_history = None
        mock_ticker.calendar = {}

        tv_fund = {"recommendation_mark": 2.0, "earnings_release_next_date": None}

        call_count = {"reuters": 0, "dow-jones": 0}

        def _mock_headlines(symbol="", provider="", limit=15):
            call_count[provider] = call_count.get(provider, 0) + 1
            if provider == "reuters":
                raise ConnectionError("Reuters down")
            return [{"title": f"{provider} ok", "id": f"{provider}_1"}]

        def _mock_symbol_news(symbol, providers=None, limit=10):
            return [f"{providers[0]} item"]

        with patch("signal_stack._tv_fetch_headlines", side_effect=_mock_headlines), \
             patch("signal_stack._tv_fetch_symbol_news", side_effect=_mock_symbol_news), \
             patch("signal_stack._tv_fetch_story_snippets", return_value=[]), \
             patch("signal_stack._TV_NEWS_OK", True), \
             patch("signal_stack._yf_has_quotesummary", return_value=True), \
             patch("signal_stack.fetch_google_news", return_value=[]), \
             patch("signal_stack.fetch_yf_news", return_value=[]), \
             patch("signal_stack.compute_news_sentiment", return_value={"score": 0.1}):
            result = fetch_sentiment_data(
                ticker_obj=mock_ticker,
                tv_fund=tv_fund,
                tv_symbol="NASDAQ:AAPL",
                company_name="Apple Inc",
            )

        # Should not crash — wire_count may be 1 (dow-jones only)
        news = result.get("news", {})
        assert "score" in news, "Sentiment should still compute despite Reuters failure"

    def test_parallel_news_non_us_skips_marketwatch(self):
        """Non-US stocks should NOT fetch MarketWatch (is_us=False)."""
        from signal_stack import fetch_sentiment_data

        mock_ticker = MagicMock()
        mock_ticker.recommendations = None
        mock_ticker.upgrades_downgrades = None
        mock_ticker.earnings_history = None
        mock_ticker.calendar = {}

        tv_fund = {"recommendation_mark": 2.0, "earnings_release_next_date": None}

        mwatch_called = {"called": False}

        def _mock_headlines(symbol="", provider="", limit=15):
            return [{"title": f"{provider} hl", "id": f"{provider}_1"}]

        def _mock_symbol_news(symbol, providers=None, limit=10):
            if providers and "market-watch" in providers:
                mwatch_called["called"] = True
            return [f"{providers[0]} item"]

        with patch("signal_stack._tv_fetch_headlines", side_effect=_mock_headlines), \
             patch("signal_stack._tv_fetch_symbol_news", side_effect=_mock_symbol_news), \
             patch("signal_stack._tv_fetch_story_snippets", return_value=[]), \
             patch("signal_stack._TV_NEWS_OK", True), \
             patch("signal_stack._yf_has_quotesummary", return_value=False), \
             patch("signal_stack.fetch_google_news", return_value=[]), \
             patch("signal_stack.fetch_yf_news", return_value=[]), \
             patch("signal_stack.compute_news_sentiment", return_value={"score": 0.0}):
            result = fetch_sentiment_data(
                ticker_obj=mock_ticker,
                tv_fund=tv_fund,
                tv_symbol="ASX:BHP",
                company_name="BHP Group",
            )

        news = result.get("news", {})
        assert news.get("mwatch_count", 0) == 0, "MarketWatch should NOT be called for non-US"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Options Badge — parallel post-rank fetching
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionsBadgeParallel:
    """Tests for parallel options badge computation in post-rank loop."""

    def test_options_badge_uses_threadpool(self):
        """Verify ThreadPoolExecutor is used in the options badge code."""
        import inspect
        from signal_stack import main as signal_main

        source = inspect.getsource(signal_main)
        # The options badge section should contain ThreadPoolExecutor
        assert "ThreadPoolExecutor" in source, \
            "main() should use ThreadPoolExecutor for options badge computation"

    def test_options_badge_section_has_parallel_pattern(self):
        """Verify the options badge section uses submit/result pattern."""
        import inspect
        from signal_stack import main as signal_main

        source = inspect.getsource(signal_main)
        # Check for the parallel pattern markers
        assert "_badge_pool" in source or "_news_pool" in source, \
            "main() should have named thread pools for parallel operations"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Sleep Reduction
# ═══════════════════════════════════════════════════════════════════════════════

class TestSleepReduction:
    """Verify the inter-symbol sleep was reduced."""

    def test_sleep_reduced_from_half_second(self):
        """time.sleep(0.5) should be reduced to time.sleep(0.1)."""
        import inspect
        from signal_stack import main as signal_main

        source = inspect.getsource(signal_main)
        assert "time.sleep(0.5)" not in source, \
            "time.sleep(0.5) should be removed/reduced"
        assert "time.sleep(0.1)" in source, \
            "time.sleep(0.1) should be present as reduced delay"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Parallel Execution Correctness
# ═══════════════════════════════════════════════════════════════════════════════

class TestParallelCorrectness:
    """Verify parallel execution produces correct results."""

    def test_threadpool_exception_isolation(self):
        """One future failing doesn't affect others in ThreadPoolExecutor."""
        results = {}

        def _task_ok(key):
            return f"ok_{key}"

        def _task_fail(key):
            raise ValueError(f"fail_{key}")

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(_task_ok, "a"): "a",
                pool.submit(_task_fail, "b"): "b",
                pool.submit(_task_ok, "c"): "c",
            }
            for f in futures:
                key = futures[f]
                try:
                    results[key] = f.result(timeout=5)
                except Exception as e:
                    results[key] = f"error: {e}"

        assert results["a"] == "ok_a"
        assert "error" in results["b"]
        assert results["c"] == "ok_c"

    def test_threadpool_respects_max_workers(self):
        """Verify max_workers limits concurrent execution."""
        import threading

        max_concurrent = {"value": 0}
        current = {"value": 0}
        lock = threading.Lock()

        def _tracked_task(i):
            with lock:
                current["value"] += 1
                max_concurrent["value"] = max(max_concurrent["value"], current["value"])
            time.sleep(0.1)
            with lock:
                current["value"] -= 1
            return i

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_tracked_task, i) for i in range(6)]
            for f in futs:
                f.result(timeout=5)

        assert max_concurrent["value"] <= 2, \
            f"max_workers=2 but saw {max_concurrent['value']} concurrent"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Per-symbol yfinance prefetch (_prefetch_yf / _pf_get)
# ═══════════════════════════════════════════════════════════════════════════════

class TestYfPrefetch:
    """The prefetch must overlap waiting without changing which endpoints run."""

    def test_non_us_without_history_fetches_nothing(self):
        """Non-US symbols have no quoteSummary endpoints — prefetch must be a no-op."""
        import signal_stack

        with patch.object(signal_stack.yf, "Ticker") as mock_ticker:
            out = signal_stack._prefetch_yf("BHP.AX", is_us=False, want_history=False)

        assert out == {}
        mock_ticker.assert_not_called()

    def test_non_us_with_history_fetches_only_history(self):
        """Book context needs OHLCV for every market, but nothing else off-US."""
        import signal_stack

        with patch.object(signal_stack.yf, "Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = "ohlcv"
            out = signal_stack._prefetch_yf("BHP.AX", is_us=False, want_history=True)

        assert set(out) == {"history"}
        assert out["history"] == "ohlcv"

    def test_us_prefetches_exactly_the_unconditional_fields(self):
        """Guards against silently adding a request that today is lazy/conditional."""
        import signal_stack

        with patch.object(signal_stack.yf, "Ticker"):
            with patch.object(signal_stack, "_fetch_pcr_option_chain", return_value=None):
                out = signal_stack._prefetch_yf("AAPL", is_us=True, want_history=True)

        assert set(out) == {
            "recommendations", "upgrades_downgrades", "earnings_history",
            "insider_transactions", "info", "option_chain", "history",
        }
        # .calendar and .news stay lazy — prefetching them would add requests.
        assert "calendar" not in out
        assert "news" not in out

    def test_failing_field_becomes_none_without_killing_the_rest(self):
        """A dead endpoint must degrade to 'no data', not abort the symbol."""
        import signal_stack

        def _boom(*a, **kw):
            raise RuntimeError("yahoo said no")

        with patch.object(signal_stack.yf, "Ticker") as mock_ticker:
            mock_ticker.return_value.history.side_effect = _boom
            out = signal_stack._prefetch_yf("BHP.AX", is_us=False, want_history=True)

        assert out == {"history": None}


class TestPfGet:
    """_pf_get is the seam that keeps every external caller working unchanged."""

    def test_prefers_prefetched_value(self):
        import signal_stack

        called = {"n": 0}

        def _getter():
            called["n"] += 1
            return "live"

        assert signal_stack._pf_get({"info": "cached"}, "info", _getter) == "cached"
        assert called["n"] == 0, "prefetched hit must not issue a request"

    def test_prefetched_none_is_honoured_not_refetched(self):
        """A failed prefetch already logged the failure — don't retry it serially."""
        import signal_stack

        called = {"n": 0}

        def _getter():
            called["n"] += 1
            return "live"

        assert signal_stack._pf_get({"info": None}, "info", _getter) is None
        assert called["n"] == 0

    def test_falls_back_when_key_absent(self):
        import signal_stack

        assert signal_stack._pf_get({}, "info", lambda: "live") == "live"

    def test_falls_back_when_prefetch_is_none(self):
        """External callers (webhook_server, analysis_engine) pass no prefetch."""
        import signal_stack

        assert signal_stack._pf_get(None, "info", lambda: "live") == "live"


class TestPrefetchCallerCompatibility:
    """The new kwarg must be optional so existing call sites keep working."""

    @pytest.mark.parametrize("fn_name", ["fetch_sentiment_data", "fetch_market_internals"])
    def test_prefetched_kwarg_is_optional(self, fn_name):
        import inspect
        import signal_stack

        sig = inspect.signature(getattr(signal_stack, fn_name))
        assert "prefetched" in sig.parameters, f"{fn_name}() missing prefetched kwarg"
        assert sig.parameters["prefetched"].default is None, \
            f"{fn_name}(prefetched=) must default to None for existing callers"

    def test_market_internals_matches_with_and_without_prefetch(self):
        """Same inputs must score identically whether prefetched or fetched live."""
        import signal_stack

        ticker = MagicMock()
        ticker.options = []
        ticker.insider_transactions = None
        ticker.info = {}
        fund = {"sector": "Technology"}

        live = signal_stack.fetch_market_internals(ticker, fund, "NASDAQ:AAPL")
        pre = signal_stack.fetch_market_internals(
            ticker, fund, "NASDAQ:AAPL",
            prefetched={"option_chain": None, "insider_transactions": None, "info": {}},
        )

        assert live == pre

    def test_pcr_fallback_reuses_callers_ticker(self):
        """No-prefetch callers must not pay for a second Ticker/.options request."""
        import signal_stack

        ticker = MagicMock()
        ticker.options = ["2026-09-18"]

        with patch.object(signal_stack.yf, "Ticker") as mock_new_ticker:
            signal_stack.fetch_market_internals(ticker, {}, "NASDAQ:AAPL")

        mock_new_ticker.assert_not_called()
        ticker.option_chain.assert_called_once_with("2026-09-18")
