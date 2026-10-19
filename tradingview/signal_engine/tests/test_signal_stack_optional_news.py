"""
Regression tests for optional TV-news import failure (review 2026-10-13, Issue 11).

BEFORE the fix: ``tv_story_snippets`` was initialized INSIDE the
``if _TV_NEWS_OK:`` block but referenced in the merge below it. When the
optional ``tv_data.tv_news`` module was unavailable (``_TV_NEWS_OK = False``),
``fetch_sentiment_data`` raised ``UnboundLocalError`` and the symbol could not
be scored at all.

AFTER: optional-source accumulators are initialized before the availability
branch, so a missing optional module degrades gracefully to the yfinance/Google
fallbacks instead of crashing.

All tests are network-free.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import signal_stack as ss


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test in this module attempts a live HTTP request.

    The non-US path reaches the TV earnings-scanner fallback
    (``_tv_fetch_earnings_date``), which POSTs to TradingView's India scanner.
    That dependency is stubbed below and every HTTP entry point is poisoned so
    an accidental live call is an error rather than a silent network hit
    (review 2026-10-13, Issue 7 follow-up).
    """
    attempts: list[str] = []

    def _blocked(*args, **kwargs):
        attempts.append(repr(args[:1]))
        raise AssertionError(
            f"unexpected network attempt during test: {args[:1]!r}")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        monkeypatch.setattr(requests, "get", _blocked)
        monkeypatch.setattr(requests, "post", _blocked)

    # Stub the earnings-scanner fallback explicitly (it is the one dependency
    # the non-US path would otherwise reach over the network).
    monkeypatch.setattr(ss, "_tv_fetch_earnings_date",
                        lambda symbol, **kw: (None, None))
    yield attempts
    assert attempts == [], f"network was attempted: {attempts}"


class _FakeTicker:
    def __init__(self, news=None):
        self.news = news or []
        self.recommendations = None
        self.upgrades_downgrades = None
        self.earnings_history = None
        self.calendar = None
        self.info = {}


def _call_sentiment(monkeypatch, tv_symbol, tv_fund=None, ticker=None,
                    yf_news=None, google_news=None):
    """Call fetch_sentiment_data with all network paths stubbed."""
    monkeypatch.setattr(ss, "fetch_yf_news", lambda t: list(yf_news or []))
    monkeypatch.setattr(ss, "fetch_google_news",
                        lambda s, c="": list(google_news or []))
    monkeypatch.setattr(ss, "_pf_get", lambda pf, field, getter: None)
    return ss.fetch_sentiment_data(
        ticker or _FakeTicker(), "Test Co", tv_symbol, tv_fund=tv_fund or {})


class TestTvNewsUnavailable:

    def test_tv_news_disabled_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(ss, "_TV_NEWS_OK", False)
        out = _call_sentiment(monkeypatch, "NASDAQ:AAPL")
        assert isinstance(out, dict)
        assert "news" in out

    def test_us_symbol_uses_yahoo_fallback(self, monkeypatch):
        monkeypatch.setattr(ss, "_TV_NEWS_OK", False)
        out = _call_sentiment(monkeypatch, "NASDAQ:AAPL",
                              yf_news=["Yahoo headline one"])
        assert out["news"]["source"] == "yfinance"
        assert out["news"]["count"] >= 1

    def test_non_us_symbol_uses_google_fallback(self, monkeypatch):
        monkeypatch.setattr(ss, "_TV_NEWS_OK", False)
        out = _call_sentiment(monkeypatch, "NSE:TCS",
                              google_news=["Google headline one"])
        assert out["news"]["source"] == "Google-RSS"
        assert out["news"]["count"] >= 1

    def test_non_us_symbol_does_not_reach_network(self, monkeypatch):
        """The non-US path reaches the earnings-scanner fallback, which must be
        stubbed so no live HTTP request is made (see the autouse guard)."""
        monkeypatch.setattr(ss, "_TV_NEWS_OK", False)
        out = _call_sentiment(monkeypatch, "NSE:TCS",
                              google_news=["Google headline"])
        assert out["news"]["source"] == "Google-RSS"
        assert out["earnings_days_away"] is None

    def test_all_sources_unavailable_returns_valid_structure(self, monkeypatch):
        monkeypatch.setattr(ss, "_TV_NEWS_OK", False)
        out = _call_sentiment(monkeypatch, "NASDAQ:AAPL")
        news = out["news"]
        assert news["source"] == "none"
        assert news["count"] == 0
        assert news["score"] == 0.0
        assert news["article_velocity"] == 0

    def test_tv_fetch_exception_behaves_like_unavailable(self, monkeypatch):
        monkeypatch.setattr(ss, "_TV_NEWS_OK", True)

        def _boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(ss, "_tv_fetch_headlines", _boom)
        monkeypatch.setattr(ss, "_tv_fetch_symbol_news_items", _boom)
        monkeypatch.setattr(ss, "_tv_fetch_story_snippets", _boom)
        out = _call_sentiment(monkeypatch, "NASDAQ:AAPL",
                              yf_news=["Yahoo fallback headline"])
        assert out["news"]["source"] == "yfinance"


class TestScoringStillWorks:

    def test_scored_row_produced_when_tv_news_absent(self, monkeypatch):
        monkeypatch.setattr(ss, "_TV_NEWS_OK", False)
        sent = _call_sentiment(monkeypatch, "NASDAQ:AAPL",
                               yf_news=["Company beats estimates"])
        scored = ss.score_sentiment(sent)
        assert "total" in scored
        assert 0.0 <= scored["total"] <= 10.0

    def test_sentiment_structure_has_required_keys(self, monkeypatch):
        monkeypatch.setattr(ss, "_TV_NEWS_OK", False)
        out = _call_sentiment(monkeypatch, "NASDAQ:AAPL")
        for key in ("analyst", "news", "recent_upgrade", "recent_downgrade",
                    "revision_velocity", "earnings_beats"):
            assert key in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
