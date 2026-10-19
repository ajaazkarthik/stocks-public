"""
Regression tests for news velocity (review 2026-10-13, Issue 9).

BEFORE the fix: news velocity used ``news_data["count"]`` — the number of
selected sentiment TEXT inputs. That included story snippets (a second
representation of an already-counted article) and undated fallback headlines,
so a single article could inflate the velocity weight shift.

AFTER: ``article_velocity`` counts distinct articles published within the
trailing 24h using preserved article ids and timestamps. Snippets are part of
their parent article; undated items keep their sentiment role but do not count
as recent. The existing weight-shift formula is unchanged — only its input.

All tests are network-free.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import signal_stack as ss
from tv_data.tv_news import article_velocity, article_published_ts

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _art(article_id, hours_ago=None, title=None, published="auto"):
    item = {"id": article_id, "title": title or f"Headline {article_id}"}
    if published == "auto":
        if hours_ago is not None:
            item["published"] = (NOW - timedelta(hours=hours_ago)).timestamp()
    else:
        item["published"] = published
    return item


class TestArticleVelocity:

    def test_multiple_representations_of_one_article_count_once(self):
        items = [
            _art("a1", 1.0),
            _art("a1", 1.0, title="Different title, same id"),
        ]
        assert article_velocity(items, now=NOW) == 1

    def test_headline_and_snippet_count_once(self):
        # A snippet is not a separate article item; only the article is counted.
        items = [_art("a1", 2.0)]
        assert article_velocity(items, now=NOW) == 1

    def test_23h_old_counts(self):
        assert article_velocity([_art("a1", 23.0)], now=NOW) == 1

    def test_25h_old_does_not_count(self):
        assert article_velocity([_art("a1", 25.0)], now=NOW) == 0

    def test_exact_24h_boundary_counts(self):
        # Documented policy: the window is inclusive of the 24h boundary.
        assert article_velocity([_art("a1", 24.0)], now=NOW) == 1

    def test_missing_timestamp_does_not_count(self):
        assert article_velocity([_art("a1", published=None)], now=NOW) == 0

    def test_invalid_timestamp_does_not_count(self):
        assert article_velocity([_art("a1", published="not-a-date")], now=NOW) == 0

    def test_future_timestamp_does_not_count(self):
        future = (NOW + timedelta(hours=5)).timestamp()
        assert article_velocity([_art("a1", published=future)], now=NOW) == 0

    def test_utc_offsets_normalized(self):
        # 7h before NOW, expressed with a +05:00 offset.
        local = (NOW - timedelta(hours=7)).astimezone(timezone(timedelta(hours=5)))
        assert article_velocity(
            [_art("a1", published=local.isoformat())], now=NOW) == 1

    def test_iso_z_timestamp_parsed(self):
        iso = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert article_velocity([_art("a1", published=iso)], now=NOW) == 1

    def test_distinct_recent_articles_all_count(self):
        items = [_art(f"a{i}", 1.0) for i in range(7)]
        assert article_velocity(items, now=NOW) == 7

    def test_empty_input(self):
        assert article_velocity([], now=NOW) == 0
        assert article_velocity(None, now=NOW) == 0


class TestPublishedTs:

    def test_none_for_missing(self):
        assert article_published_ts({}) is None
        assert article_published_ts(None) is None

    def test_returns_aware_utc(self):
        ts = article_published_ts(_art("a1", 1.0))
        assert ts is not None and ts.tzinfo is not None


class TestReferenceClock:
    """Issue 6 follow-up: the window must use one reference clock.

    With ``now=2020-01-01`` an article dated 2020-01-02 previously counted
    because the window checked only its lower bound while parsing compared
    future dates against real wall time.
    """

    def test_article_after_reference_now_does_not_count(self):
        now = datetime(2020, 1, 1, tzinfo=timezone.utc)
        item = {"id": "a1",
                "published": datetime(2020, 1, 2, tzinfo=timezone.utc).timestamp()}
        assert article_velocity([item], now=now) == 0

    def test_article_before_reference_now_counts(self):
        now = datetime(2020, 1, 2, tzinfo=timezone.utc)
        item = {"id": "a1",
                "published": datetime(2020, 1, 1, 12, tzinfo=timezone.utc).timestamp()}
        assert article_velocity([item], now=now) == 1

    def test_published_ts_rejects_after_reference_now(self):
        now = datetime(2020, 1, 1, tzinfo=timezone.utc)
        item = {"id": "a1",
                "published": datetime(2020, 1, 2, tzinfo=timezone.utc).timestamp()}
        assert article_published_ts(item, now=now) is None

    def test_published_ts_accepts_before_reference_now(self):
        now = datetime(2020, 1, 2, tzinfo=timezone.utc)
        item = {"id": "a1",
                "published": datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()}
        assert article_published_ts(item, now=now) is not None

    def test_one_second_after_reference_now_does_not_count(self):
        now = datetime(2020, 1, 1, tzinfo=timezone.utc)
        item = {"id": "a1",
                "published": (now + timedelta(seconds=1)).timestamp()}
        assert article_velocity([item], now=now) == 0

    def test_iso_after_reference_now_does_not_count(self):
        now = datetime(2020, 1, 1, tzinfo=timezone.utc)
        item = {"id": "a1", "published": "2020-01-02T00:00:00Z"}
        assert article_velocity([item], now=now) == 0


class TestWeightShiftUsesArticleVelocity:

    def _sent(self, **news):
        base = {"analyst": {"bull_ratio": 1.0}, "news": news}
        return base

    def test_equal_unique_counts_give_equal_weight_shift(self):
        # Same 6 distinct articles; one case adds 4 snippets to the text count.
        a = ss.score_sentiment(self._sent(article_velocity=6, count=6, score=0.0))
        b = ss.score_sentiment(self._sent(article_velocity=6, count=10, score=0.0))
        assert a["w_analyst"] == b["w_analyst"]
        assert a["w_news"] == b["w_news"]

    def test_snippet_inflation_no_longer_shifts_weights(self):
        # 3 articles but 20 text inputs (snippets) → no velocity boost.
        out = ss.score_sentiment(self._sent(article_velocity=3, count=20, score=0.0))
        assert out["w_analyst"] == 0.45
        assert out["w_news"] == 0.25

    def test_high_article_velocity_shifts_weights(self):
        out = ss.score_sentiment(self._sent(article_velocity=10, count=10, score=0.0))
        assert out["w_analyst"] == 0.40
        assert out["w_news"] == 0.30

    def test_report_fields_distinguish_velocity_from_text_count(self):
        out = ss.score_sentiment(self._sent(article_velocity=4, count=9, score=0.0))
        assert out["article_velocity"] == 4
        assert out["text_input_count"] == 9

    def test_backward_compatible_when_velocity_absent(self):
        out = ss.score_sentiment(self._sent(count=10, score=0.0))
        assert out["w_analyst"] == 0.40


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
