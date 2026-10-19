"""
Regression tests for analyst rating-event classification (review 2026-10-13, Issue 7).

BEFORE the fix: the upgrade/downgrade loops classified any event whose
``ToGrade`` contained "Buy"/"Outperform"/"Overweight" as an upgrade — BEFORE
checking the downgrade condition. A maintained rating ("Action=main",
"ToGrade=Buy") and even a downgrade *to* a Buy grade were counted as upgrades,
inflating ``recent_upgrade`` and ``revision_velocity``.

AFTER: ``_classify_rating_event`` treats the provider's explicit Action as
authoritative, excludes maintenance/reiteration, tracks initiations separately,
and uses grade comparison only as a documented fallback. Both the 30d and 60d
windows share the same helper.

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


def _ud_frame(rows):
    """rows: list of (date_str, firm, action, from_grade, to_grade)."""
    data = [{"Firm": r[1], "Action": r[2], "FromGrade": r[3], "ToGrade": r[4]}
            for r in rows]
    return pd.DataFrame(data, index=pd.to_datetime([r[0] for r in rows]))


class TestClassification:

    def test_downgrade_to_buy_grade_is_downgrade(self):
        assert ss._classify_rating_event("down", "Strong Buy", "Buy") == "downgrade"

    def test_maintained_buy_is_maintain(self):
        assert ss._classify_rating_event("main", "Buy", "Buy") == "maintain"

    def test_reiterated_is_maintain(self):
        assert ss._classify_rating_event("reit", "Outperform", "Outperform") == "maintain"

    def test_upgrade_to_hold_is_upgrade(self):
        assert ss._classify_rating_event("up", "Sell", "Hold") == "upgrade"

    def test_initiation_is_separate(self):
        assert ss._classify_rating_event("init", "", "Buy") == "initiation"

    def test_unknown_action_with_grade_improvement_is_upgrade(self):
        assert ss._classify_rating_event("", "Hold", "Buy") == "upgrade"

    def test_unknown_action_with_grade_deterioration_is_downgrade(self):
        assert ss._classify_rating_event("", "Buy", "Hold") == "downgrade"

    def test_unknown_action_same_grade_is_unknown(self):
        assert ss._classify_rating_event("", "Buy", "Buy") == "unknown"

    def test_underperform_not_matched_as_bullish(self):
        assert ss._grade_direction("Underperform") == -1
        assert ss._grade_direction("Outperform") == 1


class TestWindowSummary:

    def test_original_repro_maintained_buy_plus_downgrade_to_buy(self):
        # One maintained Buy + one downgrade-to-Buy: no upgrade, net velocity -1.
        frame = _ud_frame([
            ("2026-10-01", "FirmA", "main", "Buy", "Buy"),
            ("2026-10-02", "FirmB", "down", "Strong Buy", "Buy"),
        ])
        has_up, has_down, ups, downs, _ = ss._summarize_rating_events(frame)
        assert has_up is False
        assert has_down is True
        assert ups - downs == -1

    def test_one_up_one_down_is_net_zero(self):
        frame = _ud_frame([
            ("2026-10-01", "FirmA", "up", "Hold", "Buy"),
            ("2026-10-02", "FirmB", "down", "Buy", "Hold"),
        ])
        _, _, ups, downs, _ = ss._summarize_rating_events(frame)
        assert ups == 1 and downs == 1
        assert ups - downs == 0

    def test_initiation_not_counted_as_revision(self):
        frame = _ud_frame([("2026-10-01", "FirmA", "init", "", "Buy")])
        has_up, has_down, ups, downs, _ = ss._summarize_rating_events(frame)
        assert (has_up, has_down, ups, downs) == (False, False, 0, 0)

    def test_maintenance_not_counted(self):
        frame = _ud_frame([("2026-10-01", "FirmA", "main", "Buy", "Buy")])
        _, _, ups, downs, _ = ss._summarize_rating_events(frame)
        assert ups == 0 and downs == 0

    def test_30d_and_60d_windows_consistent(self):
        # Event 45 days old: in 60d window, out of 30d window.
        now = pd.Timestamp.now()
        old = (now - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
        recent = (now - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        frame = _ud_frame([
            (old, "FirmA", "up", "Hold", "Buy"),
            (recent, "FirmB", "down", "Buy", "Hold"),
        ])
        w60 = frame[frame.index > now - pd.Timedelta(days=60)]
        w30 = frame[frame.index > now - pd.Timedelta(days=30)]
        _, _, ups60, downs60, _ = ss._summarize_rating_events(w60)
        _, _, ups30, downs30, _ = ss._summarize_rating_events(w30)
        assert (ups60, downs60) == (1, 1)
        assert (ups30, downs30) == (0, 1)

    def test_events_outside_window_excluded(self):
        now = pd.Timestamp.now()
        old = (now - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
        frame = _ud_frame([(old, "FirmA", "up", "Hold", "Buy")])
        w60 = frame[frame.index > now - pd.Timedelta(days=60)]
        _, _, ups, downs, _ = ss._summarize_rating_events(w60)
        assert ups == 0 and downs == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
