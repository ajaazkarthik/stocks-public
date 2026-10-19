"""
Regression tests for default downloaded-WIN selection (review 2026-10-13, Issue 3).

BEFORE the fix: the default selection loop consumed every fire in
fires_raw.json's ``all`` array and filtered only duplicate symbols. It did not
restrict alert IDs or message direction, so an unrelated alert or a SELL event
could enter the long pipeline and receive ATM call badges and buy advice.

AFTER: ``_select_default_win_fires()`` restricts to the configured WIN alert IDs
and actual BUY direction, applies eligibility before deduplication and the --n
limit, and preserves direction for auditing.

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
from tv_data.win_alerts import WIN_2H_ID, WIN_1H_ID, WIN_ALERT_IDS


def _fire(alert_id, message, symbol="NASDAQ:A", **extra):
    fire = {"alert_id": alert_id, "message": message,
            "pro_symbol": symbol, "symbol": symbol}
    fire.update(extra)
    return fire


class TestSharedWinAlertIds:

    def test_signal_stack_uses_shared_ids(self):
        assert ss.WIN_ALERT_IDS == WIN_ALERT_IDS

    def test_both_configured_ids_present(self):
        assert WIN_2H_ID in ss.WIN_ALERT_IDS
        assert WIN_1H_ID in ss.WIN_ALERT_IDS


class TestDirectionParsing:

    @pytest.mark.parametrize("message", ["BUY price: 1", "  BUY price: 1", "buy price: 1"])
    def test_buy_variants(self, message):
        assert ss._fire_direction(message) == "BUY"

    @pytest.mark.parametrize("message", ["SELL price: 1", "  SELL price: 1", "sell price: 1"])
    def test_sell_variants(self, message):
        assert ss._fire_direction(message) == "SELL"

    @pytest.mark.parametrize("message", ["", "hello", "HOLD price: 1", "price: 1"])
    def test_unknown_direction(self, message):
        assert ss._fire_direction(message) == "UNKNOWN"


class TestEligibility:

    def test_buy_event_from_each_win_alert_is_eligible(self):
        for aid in (WIN_2H_ID, WIN_1H_ID):
            assert ss._is_eligible_win_fire(_fire(aid, "BUY price: 1"))

    def test_unrelated_alert_is_excluded(self):
        assert not ss._is_eligible_win_fire(_fire(999999, "BUY price: 1"))

    def test_sell_event_is_excluded_even_with_strong_metrics(self):
        fire = _fire(WIN_2H_ID, "SELL price: 1, WinRate: 99, NetProfit%: 500")
        assert not ss._is_eligible_win_fire(fire)

    def test_missing_direction_does_not_become_buy(self):
        assert not ss._is_eligible_win_fire(_fire(WIN_2H_ID, "price: 1"))

    def test_unrecognized_direction_does_not_become_buy(self):
        assert not ss._is_eligible_win_fire(_fire(WIN_2H_ID, "HOLD price: 1"))

    def test_leading_whitespace_and_case_normalized(self):
        assert ss._is_eligible_win_fire(_fire(WIN_2H_ID, "   buy price: 1"))

    def test_buy_with_false_flag_remains_eligible_by_default(self):
        fire = _fire(WIN_2H_ID, "BUY price: 1, BUY: false")
        assert ss._is_eligible_win_fire(fire)

    def test_buy_with_false_flag_rejected_when_strict(self):
        fire = _fire(WIN_2H_ID, "BUY price: 1, BUY: false")
        assert not ss._is_eligible_win_fire(fire, strict_buy_flag=True)


class TestSelection:

    def test_excluded_events_do_not_consume_n(self):
        fires = [
            _fire(999999, "BUY price: 1", symbol="NASDAQ:OTHER"),   # unrelated alert
            _fire(WIN_2H_ID, "SELL price: 1", symbol="NASDAQ:SELL"),  # SELL
            _fire(WIN_2H_ID, "BUY price: 1", symbol="NASDAQ:A"),
            _fire(WIN_1H_ID, "BUY price: 1", symbol="NASDAQ:B"),
        ]
        selected = ss._select_default_win_fires(fires, n=2)
        assert [f["pro_symbol"] for f in selected] == ["NASDAQ:A", "NASDAQ:B"]

    def test_duplicate_eligible_symbols_collapse(self):
        fires = [
            _fire(WIN_2H_ID, "BUY price: 1", symbol="NASDAQ:A"),
            _fire(WIN_1H_ID, "BUY price: 2", symbol="NASDAQ:A"),
            _fire(WIN_2H_ID, "BUY price: 3", symbol="NASDAQ:B"),
        ]
        selected = ss._select_default_win_fires(fires, n=10)
        assert [f["pro_symbol"] for f in selected] == ["NASDAQ:A", "NASDAQ:B"]

    def test_no_dedup_keeps_duplicates(self):
        fires = [
            _fire(WIN_2H_ID, "BUY price: 1", symbol="NASDAQ:A"),
            _fire(WIN_1H_ID, "BUY price: 2", symbol="NASDAQ:A"),
        ]
        selected = ss._select_default_win_fires(fires, n=10, no_dedup=True)
        assert len(selected) == 2

    def test_direction_preserved_on_selected_fires(self):
        fires = [_fire(WIN_2H_ID, "BUY price: 1", symbol="NASDAQ:A")]
        selected = ss._select_default_win_fires(fires, n=1)
        assert selected[0]["_direction"] == "BUY"

    def test_selection_does_not_mutate_input(self):
        fires = [_fire(WIN_2H_ID, "BUY price: 1", symbol="NASDAQ:A")]
        ss._select_default_win_fires(fires, n=1)
        assert "_direction" not in fires[0]

    def test_n_limit_applies_after_eligibility(self):
        fires = [_fire(WIN_2H_ID, "BUY price: 1", symbol=f"NASDAQ:{i}") for i in range(5)]
        assert len(ss._select_default_win_fires(fires, n=3)) == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
