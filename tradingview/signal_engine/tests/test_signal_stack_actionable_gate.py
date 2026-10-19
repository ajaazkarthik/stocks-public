"""
Regression tests for actionable-report gate policy (review 2026-10-13, Issue 10).

BEFORE the fix: per-symbol eligibility honored ``--require-trigger`` and
``--require-buy-flag``, but ``_select_actionable_report_items`` recomputed
eligibility with BOTH strict options disabled. A row rejected by the strict
gate could therefore still appear in the actionable report, its counts, and the
options candidates.

AFTER: report selection prefers the authoritative per-symbol ``actionable``
flag (computed with the same threshold and strict options) and propagates the
gate policy when recomputing. WATCH fallback rows are never marked actionable.

All tests are network-free.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import signal_stack as ss

THRESHOLD = 62.5


def _item(symbol="NASDAQ:AAA", composite=80.0, price=100.0, trigger=90.0,
          buy_flag="true", industry="Software", earnings_days=None):
    return {
        "tv_symbol": symbol,
        "composite": composite,
        "fundamentals": {"current_price": price, "industry": industry},
        "meta": {"trigger_price": trigger, "BUY": buy_flag},
        "sentiment_data": {"earnings_days_away": earnings_days},
    }


def _apply_gate(item, require_trigger=False, require_buy_flag=False):
    """Mirror main(): attach the authoritative eligibility flag."""
    item["actionability_reasons"] = ss._actionability_reasons(
        item, THRESHOLD,
        require_trigger=require_trigger,
        require_buy_flag=require_buy_flag,
    )
    item["actionable"] = not item["actionability_reasons"]
    return item


class TestStrictGatePropagation:

    def test_below_trigger_excluded_when_require_trigger(self):
        below = _apply_gate(_item(price=80.0, trigger=90.0), require_trigger=True)
        assert below["actionable"] is False
        selected, is_watch = ss._select_actionable_report_items(
            [below], THRESHOLD, require_trigger=True)
        assert is_watch is True
        assert all(r.get("_watch_candidate") for r in selected)

    def test_false_buy_flag_excluded_when_require_buy_flag(self):
        flagged = _apply_gate(_item(buy_flag="false"), require_buy_flag=True)
        assert flagged["actionable"] is False
        selected, is_watch = ss._select_actionable_report_items(
            [flagged], THRESHOLD, require_buy_flag=True)
        assert is_watch is True

    def test_row_rejected_by_both_stays_rejected_everywhere(self):
        bad = _apply_gate(_item(price=80.0, trigger=90.0, buy_flag="false"),
                          require_trigger=True, require_buy_flag=True)
        assert bad["actionable"] is False
        selected, is_watch = ss._select_actionable_report_items(
            [bad], THRESHOLD, require_trigger=True, require_buy_flag=True)
        assert is_watch is True
        assert bad not in selected
        # Options candidates also require the authoritative flag.
        assert ss._select_options_badge_candidates([bad], top_k=5) == []

    def test_default_behavior_unchanged_with_strict_disabled(self):
        row = _apply_gate(_item(price=80.0, trigger=90.0, buy_flag="false"))
        assert row["actionable"] is True
        selected, is_watch = ss._select_actionable_report_items([row], THRESHOLD)
        assert is_watch is False
        assert selected == [row]

    def test_recompute_path_uses_propagated_policy(self):
        # No authoritative flag present → recompute must honor the strict options.
        raw = _item(price=80.0, trigger=90.0)
        selected, is_watch = ss._select_actionable_report_items(
            [raw], THRESHOLD, require_trigger=True)
        assert is_watch is True
        assert raw not in selected


class TestConsistency:

    def test_every_actionable_row_is_marked_actionable(self):
        rows = [
            _apply_gate(_item("NASDAQ:A", composite=80.0)),
            _apply_gate(_item("NASDAQ:B", composite=70.0)),
        ]
        selected, is_watch = ss._select_actionable_report_items(rows, THRESHOLD)
        assert is_watch is False
        assert all(r["actionable"] for r in selected)

    def test_n_actionable_equals_eligible_count(self):
        rows = [
            _apply_gate(_item("NASDAQ:A", composite=80.0)),
            _apply_gate(_item("NASDAQ:B", composite=50.0)),   # below threshold
            _apply_gate(_item("NASDAQ:C", composite=75.0)),
        ]
        selected, is_watch = ss._select_actionable_report_items(rows, THRESHOLD)
        eligible = [r for r in rows if r["actionable"]]
        assert is_watch is False
        assert len(selected) == len(eligible) == 2

    def test_all_signals_actionable_and_options_agree(self):
        rows = [
            _apply_gate(_item("NASDAQ:A", composite=80.0)),
            _apply_gate(_item("NASDAQ:B", composite=50.0)),
        ]
        selected, _ = ss._select_actionable_report_items(rows, THRESHOLD)
        options = ss._select_options_badge_candidates(rows, top_k=5)
        assert {r["tv_symbol"] for r in selected} == {"NASDAQ:A"}
        assert {r["tv_symbol"] for r in options} == {"NASDAQ:A"}

    def test_watch_has_zero_actionable_and_stays_out(self):
        rows = [_apply_gate(_item("NASDAQ:A", composite=50.0))]
        selected, is_watch = ss._select_actionable_report_items(rows, THRESHOLD)
        assert is_watch is True
        # WATCH rows are copies marked as candidates, never actionable.
        assert all(r.get("_watch_candidate") for r in selected)
        assert not any(r.get("actionable") for r in selected)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
