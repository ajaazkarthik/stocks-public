"""
Regression tests for analyst-consensus period selection (review 2026-10-13, Issue 6).

BEFORE the fix: ``fetch_sentiment_data`` used ``rec.iloc[-1]`` to pick the
analyst row. yfinance's recommendations frame is indexed by period ("0m",
"-1m", "-2m", "-3m") but row order is not guaranteed, so the last row could be
a stale period (e.g. -3m). A stale bearish row could then override a current
bullish one.

AFTER: ``_select_current_recommendation_row`` selects the current period
explicitly, is order-independent, falls back deterministically, and an invalid
yfinance row never erases a valid TV aggregate.

All tests are network-free.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import signal_stack as ss


def _rec_frame(rows):
    """rows: list of (period_label, strongBuy, buy, hold, sell, strongSell)."""
    data = [{"strongBuy": r[1], "buy": r[2], "hold": r[3],
             "sell": r[4], "strongSell": r[5]} for r in rows]
    return pd.DataFrame(data, index=[r[0] for r in rows])


def _rec_frame_with_period_column(rows):
    """The shape yfinance actually returns: a ``period`` COLUMN and a plain
    RangeIndex, so the index carries no period information."""
    return pd.DataFrame({
        "period":    [r[0] for r in rows],
        "strongBuy": [r[1] for r in rows],
        "buy":       [r[2] for r in rows],
        "hold":      [r[3] for r in rows],
        "sell":      [r[4] for r in rows],
        "strongSell": [r[5] for r in rows],
    })


class TestPeriodColumnShape:
    """Issue 2 follow-up: yfinance supplies ``period`` as a COLUMN with a
    RangeIndex. The helper previously examined the index, so with rows ordered
    ``-3m``, ``0m`` it selected ``-3m`` (index 0 collides with the "0" key).
    """

    def test_selects_current_period_from_column(self):
        rec = _rec_frame_with_period_column([
            ("-3m", 0, 1, 4, 5, 4),   # bearish stale, at index 0
            ("0m", 10, 5, 1, 0, 0),   # bullish current, at index 1
        ])
        row = ss._select_current_recommendation_row(rec)
        assert row["period"] == "0m"
        assert int(row["strongBuy"]) == 10

    def test_shuffled_column_rows_do_not_change_result(self):
        rows = [
            ("-3m", 0, 1, 4, 5, 4),
            ("-1m", 2, 3, 5, 2, 1),
            ("0m", 10, 5, 1, 0, 0),
        ]
        a = ss._select_current_recommendation_row(_rec_frame_with_period_column(rows))
        b = ss._select_current_recommendation_row(
            _rec_frame_with_period_column(list(reversed(rows))))
        assert a["period"] == b["period"] == "0m"
        assert int(a["strongBuy"]) == int(b["strongBuy"]) == 10

    def test_column_missing_current_falls_back_to_newest(self):
        rec = _rec_frame_with_period_column([
            ("-3m", 0, 0, 1, 1, 1),
            ("-1m", 1, 1, 1, 0, 0),
        ])
        assert ss._select_current_recommendation_row(rec)["period"] == "-1m"

    def test_column_unparseable_falls_back_to_last_row(self):
        rec = _rec_frame_with_period_column([
            ("alpha", 1, 0, 0, 0, 0),
            ("beta", 0, 0, 0, 0, 1),
        ])
        assert ss._select_current_recommendation_row(rec)["period"] == "beta"

    def test_range_index_alone_is_not_treated_as_period(self):
        """A plain RangeIndex must not be read as period offsets."""
        rec = pd.DataFrame({
            "strongBuy": [1, 2], "buy": [0, 0], "hold": [0, 0],
            "sell": [0, 0], "strongSell": [0, 0],
        })
        row = ss._select_current_recommendation_row(rec)
        assert int(row["strongBuy"]) == 2   # deterministic last row

    def test_legacy_index_shape_still_supported(self):
        rec = _rec_frame([("0m", 10, 5, 1, 0, 0), ("-3m", 0, 1, 4, 5, 4)])
        assert ss._select_current_recommendation_row(rec).name == "0m"


class TestCurrentPeriodSelection:

    def test_bullish_current_beats_bearish_stale(self):
        # Provider order: 0m, -1m, -2m, -3m. iloc[-1] would pick -3m (bearish).
        rec = _rec_frame([
            ("0m",  10, 5, 1, 0, 0),   # bullish current
            ("-1m",  2, 3, 5, 2, 1),
            ("-2m",  1, 2, 6, 3, 2),
            ("-3m",  0, 1, 4, 5, 4),   # bearish stale
        ])
        row = ss._select_current_recommendation_row(rec)
        assert row.name == "0m"
        assert int(row["strongBuy"]) == 10

    def test_shuffled_rows_do_not_change_result(self):
        rows = [
            ("0m",  10, 5, 1, 0, 0),
            ("-1m",  2, 3, 5, 2, 1),
            ("-3m",  0, 1, 4, 5, 4),
        ]
        a = ss._select_current_recommendation_row(_rec_frame(rows))
        b = ss._select_current_recommendation_row(_rec_frame(list(reversed(rows))))
        assert a.name == b.name == "0m"
        assert int(a["strongBuy"]) == int(b["strongBuy"]) == 10

    def test_single_valid_current_row(self):
        rec = _rec_frame([("0m", 3, 2, 1, 0, 0)])
        assert ss._select_current_recommendation_row(rec).name == "0m"

    def test_missing_current_falls_back_to_newest_period(self):
        rec = _rec_frame([("-1m", 1, 1, 1, 0, 0), ("-3m", 0, 0, 1, 1, 1)])
        assert ss._select_current_recommendation_row(rec).name == "-1m"

    def test_unparseable_index_falls_back_to_last_row(self):
        rec = _rec_frame([("alpha", 1, 0, 0, 0, 0), ("beta", 0, 0, 0, 0, 1)])
        assert ss._select_current_recommendation_row(rec).name == "beta"

    def test_empty_frame_returns_none(self):
        assert ss._select_current_recommendation_row(pd.DataFrame()) is None
        assert ss._select_current_recommendation_row(None) is None


class TestRowValidity:

    def test_zero_analyst_row_is_invalid(self):
        rec = _rec_frame([("0m", 0, 0, 0, 0, 0)])
        assert not ss._analyst_row_is_valid(ss._select_current_recommendation_row(rec))

    def test_populated_row_is_valid(self):
        rec = _rec_frame([("0m", 1, 0, 0, 0, 0)])
        assert ss._analyst_row_is_valid(ss._select_current_recommendation_row(rec))

    def test_none_row_is_invalid(self):
        assert not ss._analyst_row_is_valid(None)


class TestBullRatioUnchangedForSelectedRow:

    def test_bull_ratio_matches_selected_row(self):
        rec = _rec_frame([("0m", 4, 4, 2, 0, 0), ("-3m", 0, 0, 0, 5, 5)])
        row = ss._select_current_recommendation_row(rec)
        sb, b, h, s, ss_ = (int(row[k]) for k in
                            ("strongBuy", "buy", "hold", "sell", "strongSell"))
        total = sb + b + h + s + ss_
        expected = round((sb * 2 + b) / total, 3)
        assert expected == round((4 * 2 + 4) / 10, 3)
        assert ss._consensus_label(expected) == "Buy"


class TestRecordedPeriodMetadata:
    """P3 nit: the recorded ``period`` must be the period LABEL, not the
    numeric RangeIndex position that ``row.name`` yields for the column shape.
    """

    def _recorded_period(self, monkeypatch, rec):
        class _Ticker:
            recommendations = rec
            upgrades_downgrades = None
            earnings_history = None
            calendar = None
            info = {}

        monkeypatch.setattr(ss, "fetch_yf_news", lambda t: [])
        monkeypatch.setattr(ss, "fetch_google_news", lambda s, c="": [])
        monkeypatch.setattr(ss, "_pf_get",
                            lambda pf, field, getter: getter() if field == "recommendations" else None)
        out = ss.fetch_sentiment_data(_Ticker(), "Test Co", "NASDAQ:AAPL", tv_fund={})
        return out["analyst"].get("period")

    def test_column_shape_records_period_label(self, monkeypatch):
        rec = _rec_frame_with_period_column([
            ("-3m", 0, 1, 4, 5, 4),
            ("0m", 10, 5, 1, 0, 0),
        ])
        assert self._recorded_period(monkeypatch, rec) == "0m"

    def test_legacy_index_shape_records_period_label(self, monkeypatch):
        rec = _rec_frame([("0m", 10, 5, 1, 0, 0), ("-3m", 0, 1, 4, 5, 4)])
        assert self._recorded_period(monkeypatch, rec) == "0m"

    def test_recorded_period_is_not_a_numeric_index(self, monkeypatch):
        rec = _rec_frame_with_period_column([
            ("-3m", 0, 1, 4, 5, 4),
            ("0m", 10, 5, 1, 0, 0),
        ])
        assert self._recorded_period(monkeypatch, rec) != "1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
