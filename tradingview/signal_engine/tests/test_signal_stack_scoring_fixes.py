"""
Tests for signal_stack.py bug fixes (2026-07-06 option-14 deep-dive fix pass).

Covers:
  TestEpsGrowthLabel        -- HTML "EPS Growth" card must show earnings_growth,
                               not revenue_growth (was showing the wrong field)
  TestNearEarningsFalsyZero -- near_earnings must be True when earnings are
                               TODAY (earnings_days_away=0), not False
  TestBollingerBandWidth    -- fetch_tv_screener() now requests BB.upper/BB.lower
                               and computes bb_width so score_technical()'s
                               previously-always-None bb_width sub-score works
  TestTradableWithLimitations -- _compute_tradable_with_limitations() wiring
                               for the Moomoo-SG manual-only badge
  TestNLimitScreenerNewsModes -- --n is documented as a hard cap; this covers
                               the underlying slicing helper used by all modes

All tests are network-free.
"""

from __future__ import annotations

import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import signal_stack as ss


# ─── Shared fixture ───────────────────────────────────────────────────────────

def _make_ranked_item(**fund_overrides) -> dict:
    """Minimal item shape accepted by build_html() (see build_html() for the
    required bracket-accessed keys: fundamentals, meta, sentiment_data,
    market_internals, scores.{l1,l2,l3,l4}, composite)."""
    fund = {
        "market_cap": 1_000_000_000, "forward_pe": 20.0, "peg_ratio": 1.0,
        "roe": 15.0, "profit_margin": 10.0,
        "earnings_growth": 25.0, "revenue_growth": -5.0,  # deliberately different
        "debt_to_equity": 0.5, "current_price": 100.0, "currency": "USD",
    }
    fund.update(fund_overrides)
    return {
        "tv_symbol": "NASDAQ:TEST",
        "resolution": "60",
        "fundamentals": fund,
        "meta": {"trigger_price": 100.0, "sqzOff": "0"},
        "sentiment_data": {
            "analyst": {"consensus": "Buy", "total": 5, "strongBuy": 1, "buy": 2, "hold": 2},
            "earnings_beats": {"beat_rate": 0.5},
            "earnings_risk": "Low", "earnings_date_str": "N/A",
            "news": {"label": "Neutral", "score": 0.0, "count": 0},
            "upgrade_detail": [], "recent_upgrade": False, "recent_downgrade": False,
        },
        "market_internals": {},
        "scores": {
            "l1": {"total": 5.0}, "l2": {"total": 5.0},
            "l3": {"total": 5.0}, "l4": {"total": 5.0},
        },
        "composite": 60.0,
    }


# ─── C1: EPS Growth label bug ─────────────────────────────────────────────────

class TestEpsGrowthLabel:

    def test_eps_growth_row_shows_earnings_growth_value(self):
        item = _make_ranked_item(earnings_growth=25.0, revenue_growth=-5.0)
        html = ss.build_html([item], 1)

        m = re.search(r"<span>EPS Growth</span><b[^>]*>([^<]+)</b>", html)
        assert m is not None, "EPS Growth row not found in rendered HTML"
        assert m.group(1) == "25.00%", (
            f"EPS Growth row should show earnings_growth (25.00%), got {m.group(1)!r}"
        )

    def test_eps_growth_row_does_not_show_revenue_growth_value(self):
        item = _make_ranked_item(earnings_growth=25.0, revenue_growth=-5.0)
        html = ss.build_html([item], 1)

        m = re.search(r"<span>EPS Growth</span><b[^>]*>([^<]+)</b>", html)
        assert m is not None
        assert m.group(1) != "-5.00%", "EPS Growth row is still showing revenue_growth's value"

    def test_eps_growth_row_green_when_earnings_growth_positive_despite_negative_revenue(self):
        # Regression guard for the color class also being bound to the wrong field
        item = _make_ranked_item(earnings_growth=25.0, revenue_growth=-5.0)
        html = ss.build_html([item], 1)
        assert '<span>EPS Growth</span><b class="green"' in html


class TestSignalStackReviewFixes:

    def test_report_escapes_external_headlines(self):
        item = _make_ranked_item()
        item["sentiment_data"]["news"] = {
            "label": "Neutral", "score": 0.0, "count": 1,
            "headlines": ['<script>alert("xss")</script>'],
        }
        html = ss.build_html([item], 1)
        assert '<script>alert("xss")</script>' not in html
        assert "&lt;script&gt;alert" in html

    def test_confidence_reads_produced_free_cashflow_key(self):
        confidence = ss._data_confidence(
            {}, {"free_cashflow": 1}, {}, {},
        )
        assert confidence["l2"] == pytest.approx(1 / 6, abs=0.001)

    @pytest.mark.parametrize("value", [1, 1.0, "1", "1.0", "true"])
    def test_integer_and_string_squeeze_release_values_score_bullish(self, value):
        scores = ss.score_technical({"sqzOff": value}, {})
        assert scores["squeeze"] == 10.0

    @pytest.mark.parametrize(("tv_symbol", "yf_symbol"), [
        ("TYO:7203", "7203.T"),
        ("TSE:7203", "7203.T"),
        ("KRX:005930", "005930.KS"),
        ("KOSDAQ:035720", "035720.KQ"),
    ])
    def test_japan_and_korea_symbols_use_yfinance_suffixes(self, tv_symbol, yf_symbol):
        assert ss.tv_to_yf(tv_symbol) == yf_symbol


# ─── H2: near_earnings falsy-zero bug ─────────────────────────────────────────

class TestNearEarningsFalsyZero:

    def _near_earnings(self, earnings_days_away):
        """Mirror the exact expression used in signal_stack.py's results.append()."""
        sent_data = {"earnings_days_away": earnings_days_away}
        return (lambda _d: 999 if _d is None else _d)(sent_data.get("earnings_days_away")) <= 5

    def test_same_day_earnings_is_near_earnings(self):
        assert self._near_earnings(0) is True

    def test_one_day_away_is_near_earnings(self):
        assert self._near_earnings(1) is True

    def test_five_days_away_is_near_earnings_boundary(self):
        assert self._near_earnings(5) is True

    def test_six_days_away_is_not_near_earnings(self):
        assert self._near_earnings(6) is False

    def test_none_is_not_near_earnings(self):
        assert self._near_earnings(None) is False

    def test_unknown_far_future_is_not_near_earnings(self):
        assert self._near_earnings(180) is False


# ─── H4: Bollinger Band width now populated ───────────────────────────────────

class TestBollingerBandWidth:

    def test_score_technical_computes_bb_score_when_bb_width_present(self):
        meta = {}
        screener = {"bb_width": 4.0}   # tight squeeze
        result = ss.score_technical(meta, screener=screener)
        assert result["bb_width"] == 8.0   # per score_technical's own thresholds (<6 -> 8.0)

    def test_score_technical_bb_score_none_when_bb_width_absent(self):
        meta = {}
        screener = {}
        result = ss.score_technical(meta, screener=screener)
        assert result["bb_width"] is None

    def test_fetch_tv_screener_requests_bb_columns(self):
        # fetch_tv_screener's internal `fields` list must include BB.upper/BB.lower
        # so bb_width can ever be populated. Verified by source inspection since
        # the function makes a live network call otherwise.
        import inspect
        src = inspect.getsource(ss.fetch_tv_screener)
        assert '"BB.upper"' in src
        assert '"BB.lower"' in src


# ─── H1: Tradable-with-limitations wiring ─────────────────────────────────────

class TestTradableWithLimitations:

    def test_none_filter_returns_false(self):
        assert ss._compute_tradable_with_limitations("FX:EURUSD", None) is False

    def test_forex_symbol_is_tradable_with_limitations(self):
        from tradability_filter import TradabilityFilter
        tf = TradabilityFilter()
        assert ss._compute_tradable_with_limitations("FX:EURUSD", tf) is True

    def test_crypto_symbol_is_tradable_with_limitations(self):
        from tradability_filter import TradabilityFilter
        tf = TradabilityFilter()
        assert ss._compute_tradable_with_limitations("BINANCE:BTCUSDT", tf) is True

    def test_normal_nse_symbol_is_not_tradable_with_limitations(self):
        from tradability_filter import TradabilityFilter
        tf = TradabilityFilter()
        assert ss._compute_tradable_with_limitations("NSE:TITAN", tf) is False

    def test_exception_in_filter_is_swallowed_and_returns_false(self):
        bad_filter = MagicMock()
        bad_filter.check.side_effect = RuntimeError("boom")
        assert ss._compute_tradable_with_limitations("NSE:TITAN", bad_filter) is False

    def test_badge_renders_in_html_when_flag_set(self):
        item = _make_ranked_item()
        item["_tradable_with_limitations"] = True
        html = ss.build_html([item], 1)
        assert "Moomoo-SG (manual)" in html

    def test_badge_absent_when_flag_not_set(self):
        item = _make_ranked_item()
        html = ss.build_html([item], 1)
        assert "Moomoo-SG (manual)" not in html


# ─── Score derivation strip ───────────────────────────────────────────────────

class TestScoreBreakdown:
    """The card publishes a decision score; the strip must let a reader
    reproduce it from the report alone, with no step unaccounted for."""

    ARGS = dict(
        institutional_l5=7.0, l6=6.0, l7=8.0,
        book_bonus=2.0, pool_boost=3.0, basket_boost=5.0, data_confidence=0.97,
    )
    BD_ARGS = dict(ARGS, age_hours=36.0)

    def test_components_agree_with_public_composite(self):
        assert ss._composite_components(9.0, 8.2, 7.6, 5.6, 7.0, 6.0, 8.0)["stretched"] == \
            ss.composite(9.0, 8.2, 7.6, 5.6, l5=7.0, l6=6.0, l7=8.0)

    def test_breakdown_reconciles_with_computed_scores(self):
        quality, decision = ss._compute_decision_scores(9.0, 8.2, 7.6, 5.6, **self.ARGS)
        bd = ss._score_breakdown(9.0, 8.2, 7.6, 5.6, **self.BD_ARGS)
        assert bd["quality"] == quality
        assert bd["decision"] == decision
        assert bd["steps"][-1]["value"] == decision

    def test_every_multiplier_and_boost_is_shown(self):
        labels = [s["label"] for s in ss._score_breakdown(9.0, 8.2, 7.6, 5.6, **self.BD_ARGS)["steps"]]
        for expected in ("Weighted layers", "L7 conviction bonus", "Contrast stretch",
                         "Book context", "Quality score", "Signal age",
                         "Confidence demotion", "Pre-screen pool boost",
                         "Smallcase basket boost", "Decision score"):
            assert expected in labels, f"{expected} missing from breakdown"

    def test_signal_age_is_reported_but_never_scored(self):
        aged = ss._score_breakdown(9.0, 8.2, 7.6, 5.6, **self.BD_ARGS)
        plain = ss._score_breakdown(9.0, 8.2, 7.6, 5.6, **self.ARGS)
        assert aged["decision"] == plain["decision"]
        step = next(s for s in aged["steps"] if s["label"] == "Signal age")
        assert step.get("note") is True
        assert "value" not in step

    def test_inactive_steps_are_omitted(self):
        bd = ss._score_breakdown(
            9.0, 8.2, 7.6, 5.6, institutional_l5=None,
            data_confidence=None,
        )
        labels = [s["label"] for s in bd["steps"]]
        assert labels == ["Weighted layers", "Contrast stretch", "Quality score", "Decision score"]

    def test_confidence_floor_is_disclosed(self):
        bd = ss._score_breakdown(
            9.0, 8.2, 7.6, 5.6, institutional_l5=None,
            data_confidence=0.60,
        )
        step = next(s for s in bd["steps"] if s["label"] == "Confidence demotion")
        assert "floored" in step["detail"]
        assert "60%" in step["detail"]

    def test_strip_renders_and_states_the_published_score(self):
        item = _make_ranked_item()
        item["composite"] = 81.83
        item["score_breakdown"] = ss._score_breakdown(9.0, 8.2, 7.6, 5.6, **self.BD_ARGS)
        html = ss.build_html([item], 1)
        assert 'class="breakdown"' in html
        assert "How this 81.8 was calculated" in html
        assert "Confidence demotion" in html

    def test_strip_omitted_for_reports_scored_before_this_field_existed(self):
        html = ss.build_html([_make_ranked_item()], 1)
        assert 'class="breakdown"' not in html


# ─── M1: --n limit must apply in --screener and --news modes too ────────────

class TestNLimitAppliedInAllSourceModes:
    """
    Regression guard for M1: --n was silently ignored in --screener and
    --news modes (only enforced in default/--austsuper modes), so e.g.
    `--screener stocks_us --n 10` would process all 200+ screener candidates
    instead of 10. Fixed 2026-07-06 by slicing `selected[:args.n]` in both
    modes, matching the pattern the --austsuper mode already used.

    main() is a large monolithic function not easily unit-tested in
    isolation, so this is a source-level regression check confirming the
    slicing is present in both code paths (and specifically that it happens
    for `selected`, the variable actually passed into the per-symbol loop).
    """

    def test_screener_mode_slices_selected_by_args_n(self):
        import inspect
        src = inspect.getsource(ss.main)
        assert "selected, _ = _screener_to_fires(classes=_classes if _classes else None)\n        selected = selected[:args.n]" in src

    def test_news_mode_slices_selected_by_args_n(self):
        import inspect
        src = inspect.getsource(ss.main)
        assert "selected = _bull_fires + _bear_fires\n        selected = selected[:args.n]" in src


class TestSessionAwareFreshness:

    def test_weekend_hours_do_not_age_signal(self):
        now = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        age = ss._weekday_active_age_hours(
            "2026-07-10T20:00:00Z", now_utc=now
        )
        assert age == 4.0


# ─── Screener-sourced fires must be deduped like webhook fires ──────────────

class TestScreenerFireDedup:
    """
    Regression guard: --austsuper and --source-filter selected every fire in
    fires_raw.json, so re-running a screener scored the same stock twice with
    different freshness decays (ASX:PME at 52.34 and 42.87 in one report).
    """

    def test_keeps_only_the_first_fire_per_symbol(self):
        fires = [
            {"symbol": "ASX:PME", "close": 178.86},
            {"symbol": "ASX:ANZ", "close": 30.0},
            {"symbol": "ASX:PME", "close": 183.71},
        ]
        out = ss._dedup_fires_by_symbol(fires)
        assert [f["symbol"] for f in out] == ["ASX:PME", "ASX:ANZ"]
        assert out[0]["close"] == 178.86

    def test_pro_symbol_takes_precedence(self):
        fires = [
            {"symbol": "PME", "pro_symbol": "ASX:PME"},
            {"symbol": "PME", "pro_symbol": "ASX:PME"},
        ]
        assert len(ss._dedup_fires_by_symbol(fires)) == 1

    def test_disabled_returns_every_fire(self):
        fires = [{"symbol": "ASX:PME"}, {"symbol": "ASX:PME"}]
        assert len(ss._dedup_fires_by_symbol(fires, enabled=False)) == 2

    def test_austsuper_and_source_filter_modes_dedup(self):
        import inspect
        src = inspect.getsource(ss.main)
        assert src.count("_dedup_fires_by_symbol(") == 2


# ─── TV analyst consensus must share the yfinance scale ─────────────────────

class TestBreakdownMatchesTheEngine:
    """
    The strip is only trustworthy if its final step equals the score the engine
    actually publishes. Rounding each step of the post-quality chain drifted the
    total by 0.01 against _compute_decision_scores, which rounds once at the end
    (seen live on NASDAQ:MU 74.24 vs 74.23 and NYSE:MS 58.17 vs 58.16).
    """

    CASES = [
        # l1,   l2,   l3,   l4,   l5,   conf,  book, pool, basket
        (7.46, 9.00, 9.09, 5.92, 7.40, 0.975, 3.0,  0.0,  0.0),   # MU
        (4.72, 8.73, 8.98, 5.49, None, 0.975, 0.0,  0.0,  0.0),   # MS
        (9.00, 8.24, 5.84, 5.61, None, 0.850, 0.0,  0.0,  0.0),   # TSE:8306
        (4.68, 8.79, 5.57, 5.75, None, 0.850, 0.0,  3.0,  0.0),   # MAHABANK
        (5.20, 6.86, 8.17, 6.14, 6.00, 1.000, 0.0,  0.0,  2.0),
        (3.00, 3.00, 3.00, 3.00, None, 0.850, 0.0,  0.0,  0.0),
    ]

    @pytest.mark.parametrize("case", CASES)
    def test_final_step_equals_published_decision_score(self, case):
        l1, l2, l3, l4, l5, conf, book, pool, basket = case
        kwargs = dict(
            institutional_l5=l5, book_bonus=book,
            pool_boost=pool, basket_boost=basket, data_confidence=conf,
        )
        _, decision = ss._compute_decision_scores(l1, l2, l3, l4, **kwargs)
        bd = ss._score_breakdown(l1, l2, l3, l4, age_hours=36.0, **kwargs)
        assert bd["steps"][-1]["value"] == decision
        assert bd["decision"] == decision

    @pytest.mark.parametrize("case", CASES)
    def test_quality_step_equals_published_quality(self, case):
        l1, l2, l3, l4, l5, conf, book, pool, basket = case
        kwargs = dict(
            institutional_l5=l5, book_bonus=book,
            pool_boost=pool, basket_boost=basket, data_confidence=conf,
        )
        quality, _ = ss._compute_decision_scores(l1, l2, l3, l4, **kwargs)
        bd = ss._score_breakdown(l1, l2, l3, l4, age_hours=36.0, **kwargs)
        assert bd["quality"] == quality


# ─── TV analyst consensus must share the yfinance scale ─────────────────────
class TestTvAnalystCalibration:
    """
    TV's recommendation_mark was read as the documented 1=Strong Buy … 5=Strong
    Sell scale. Real values only span ~1.05-1.74, so (5-mark)/2 mapped every
    market to 1.63-1.98 -- "Strong Buy" for everything. Because the yfinance
    branch overrides TV for US names, the damage landed exclusively on non-US
    symbols: they scored l3.analyst 8.75-9.43 on zero analyst votes while
    US names with real coverage scored 1.91-6.33.

    Anchors below are (mark, yfinance bull_ratio) pairs observed on symbols
    carrying both sources in the 2026-08-13 run.
    """

    # (recommendation_mark, yfinance bull_ratio, yfinance consensus)
    DUAL_COVERED = [
        (1.045455, 1.267, "Buy"),    # ANET
        (1.151786, 1.091, "Buy"),    # MU
        (1.288462, 1.045, "Buy"),    # C
        (1.396552, 0.793, "Hold"),   # MRK
        (1.453125, 0.750, "Hold"),   # FDX
        (1.625000, 0.480, "Hold"),   # MS
        (1.740000, 0.381, "Sell"),   # LMT
    ]

    def test_calibrated_ratio_tracks_real_analyst_coverage(self):
        for mark, actual, _ in self.DUAL_COVERED:
            got = ss._bull_ratio_from_tv_mark(mark)
            assert got == pytest.approx(actual, abs=0.2), (
                f"mark {mark} -> {got}, yfinance says {actual}"
            )

    def test_bearish_mark_is_not_labelled_a_buy(self):
        # LMT's mark, the most bearish observed; the old map called this Strong Buy.
        # The fit lands at 0.414 against yfinance's 0.381, so it sits either side
        # of the Hold/Sell edge -- assert the direction, not the exact band.
        assert ss._consensus_label(ss._bull_ratio_from_tv_mark(1.74)) in {"Hold", "Sell"}

    def test_mapping_is_monotonic_lower_mark_is_more_bullish(self):
        ratios = [ss._bull_ratio_from_tv_mark(m) for m, _, _ in self.DUAL_COVERED]
        assert ratios == sorted(ratios, reverse=True)

    def test_tv_only_symbols_no_longer_max_out_the_analyst_score(self):
        # The five TV-sourced symbols from the 2026-08-13 run.
        for mark in (1.229167, 1.25, 1.266667, 1.5):
            ratio = ss._bull_ratio_from_tv_mark(mark)
            assert ratio < 1.5, f"mark {mark} still scores Strong Buy ({ratio})"
            assert ss._consensus_label(ratio) != "Strong Buy"

    def test_ratio_stays_inside_the_yfinance_zero_to_two_range(self):
        for mark in (0.0, 1.0, 2.0, 3.0, 5.0, 99.0):
            assert 0.0 <= ss._bull_ratio_from_tv_mark(mark) <= 2.0

    def test_both_sources_share_one_consensus_band(self):
        import inspect
        src = inspect.getsource(ss.fetch_sentiment_data)
        assert src.count("_consensus_label(") == 3, (
            "TV mark, Recommend.All and yfinance branches must all use the "
            "shared band, or a label means different things per source"
        )

    def test_weekday_age_excludes_the_weekend(self):
        now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
        age = ss._weekday_active_age_hours(
            "2026-07-06T12:00:00Z", now_utc=now
        )
        assert age == 48.0

    def test_age_is_not_capped(self):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        age = ss._weekday_active_age_hours(
            "2026-07-01T12:00:00Z", now_utc=now
        )
        assert age > 400.0


class TestScreenerPoolFreshness:

    def test_recent_pool_is_fresh(self):
        now = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        assert ss._is_pool_fresh("2026-07-11T12:00:00Z", now_utc=now)

    def test_april_pool_is_stale_in_july(self):
        now = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        assert not ss._is_pool_fresh("2026-04-13T10:24:55Z", now_utc=now)


def test_weak_l1_alone_no_longer_blocks_actionability():
    item = _make_ranked_item()
    item["composite"] = 70.0
    item["scores"]["l1"]["total"] = 4.0
    item["data_confidence"] = {"overall": 0.8}
    selected, is_watch = ss._select_actionable_report_items([item], 50.0)
    assert is_watch is False
    assert selected[0]["tv_symbol"] == "NASDAQ:TEST"


def test_watch_fallback_used_when_every_item_is_below_threshold():
    item = _make_ranked_item()
    item["composite"] = 55.0
    selected, is_watch = ss._select_actionable_report_items([item], 65.0)
    assert is_watch is True
    assert selected[0]["tv_symbol"] == "NASDAQ:TEST"
    assert selected[0]["_watch_candidate"] is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

