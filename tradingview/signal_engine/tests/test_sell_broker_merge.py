"""
Tests for sell_strategy.py broker-position merging, broker badges, and the
_finalise() SELL->REVIEW softener consistency fix.

All tests are network-free.

Test groups
-----------
  TestCombineBrokerPositions        -- _combine_broker_positions() never drops
                                        a position for an overlapping symbol
  TestBrokerBadge                   -- broker_badge() color lookup incl. the
                                        "Webull AU" / "Moomoo-AU" / "Moomoo-SG"
                                        naming fix (2026-07-06)
  TestFinaliseSoftenerConsistency   -- _finalise() re-runs PositionManager
                                        after the SELL->REVIEW softener so
                                        profit/scaling/quality fields never
                                        go stale (2026-07-06)
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# ─── path setup ──────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent   # tradingview/
sys.path.insert(0, str(_ROOT))

import sell.sell_strategy as ss
from sell.sell_strategy import _combine_broker_positions, broker_badge


# ─── _combine_broker_positions ────────────────────────────────────────────────

class TestCombineBrokerPositions(unittest.TestCase):
    """
    Every broker's position must survive the merge -- no silent drops.

    Prior versions used a Moomoo > IBKR > India > Webull priority order that
    silently DISCARDED the lower-priority broker's entire position (shares,
    cost basis, exit signal -- everything) for any overlapping symbol, with
    only a one-line console log and nothing in the HTML report. Fixed 2026-07-06.
    """

    def test_no_positions_dropped_for_symbol_in_all_four_brokers(self):
        moomoo = [{"symbol": "NASDAQ:AAPL", "broker": "Moomoo-AU", "shares": 10}]
        ibkr   = [{"symbol": "NASDAQ:AAPL", "broker": "IBKR", "shares": 5}]
        india  = [{"symbol": "NASDAQ:AAPL", "broker": "Zerodha", "shares": 3}]
        webull = [{"symbol": "NASDAQ:AAPL", "broker": "Webull AU", "shares": 2}]

        combined = _combine_broker_positions(moomoo, ibkr, india, webull)

        self.assertEqual(len(combined), 4, "All 4 broker positions must be kept, not deduped away")
        brokers = sorted(p["broker"] for p in combined)
        self.assertEqual(brokers, ["IBKR", "Moomoo-AU", "Webull AU", "Zerodha"])

    def test_each_brokers_own_shares_preserved(self):
        moomoo = [{"symbol": "NASDAQ:AAPL", "broker": "Moomoo-AU", "shares": 10}]
        ibkr   = [{"symbol": "NASDAQ:AAPL", "broker": "IBKR", "shares": 5}]
        combined = _combine_broker_positions(moomoo, ibkr, [], [])
        shares_by_broker = {p["broker"]: p["shares"] for p in combined}
        self.assertEqual(shares_by_broker["Moomoo-AU"], 10)
        self.assertEqual(shares_by_broker["IBKR"], 5)

    def test_no_overlap_returns_all_positions(self):
        moomoo = [{"symbol": "NASDAQ:AAPL", "broker": "Moomoo-AU", "shares": 10}]
        ibkr   = [{"symbol": "NYSE:KO", "broker": "IBKR", "shares": 5}]
        india  = [{"symbol": "NSE:TCS", "broker": "Zerodha", "shares": 3}]
        webull = [{"symbol": "ASX:CBA", "broker": "Webull AU", "shares": 2}]

        combined = _combine_broker_positions(moomoo, ibkr, india, webull)
        self.assertEqual(len(combined), 4)

    def test_all_empty_lists_return_empty(self):
        self.assertEqual(_combine_broker_positions([], [], [], []), [])

    def test_same_symbol_across_two_moomoo_accounts_both_kept(self):
        # Same US stock held in both Moomoo-AU and Moomoo-SG accounts --
        # genuinely different shares/cost-basis, must NOT be deduped.
        moomoo = [
            {"symbol": "NASDAQ:AAPL", "broker": "Moomoo-AU", "shares": 10},
            {"symbol": "NASDAQ:AAPL", "broker": "Moomoo-SG", "shares": 20},
        ]
        combined = _combine_broker_positions(moomoo, [], [], [])
        self.assertEqual(len(combined), 2)

    def test_return_order_is_moomoo_ibkr_india_webull(self):
        moomoo = [{"symbol": "A", "broker": "Moomoo-AU", "shares": 1}]
        ibkr   = [{"symbol": "B", "broker": "IBKR", "shares": 1}]
        india  = [{"symbol": "C", "broker": "Zerodha", "shares": 1}]
        webull = [{"symbol": "D", "broker": "Webull AU", "shares": 1}]
        combined = _combine_broker_positions(moomoo, ibkr, india, webull)
        self.assertEqual([p["symbol"] for p in combined], ["A", "B", "C", "D"])


# ─── broker_badge ──────────────────────────────────────────────────────────────

class TestBrokerBadge(unittest.TestCase):
    """
    broker_badge() color lookup. "Webull AU" must resolve via the same
    .startswith() pattern already used for "Moomoo-AU"/"Moomoo-SG" (fixed
    2026-07-06 -- previously the colors dict key was bare "Webull" with no
    startswith fallback, so it happened to still match by exact equality,
    but was inconsistent with the Moomoo pattern and broke if any Webull
    sub-account naming was ever introduced).
    """

    def test_webull_au_shows_broker_name_and_webull_color(self):
        badge = broker_badge("Webull AU")
        self.assertIn("Webull AU", badge)
        self.assertIn("#00c9a7", badge)

    def test_moomoo_au_uses_moomoo_color(self):
        badge = broker_badge("Moomoo-AU")
        self.assertIn("#f5a623", badge)

    def test_moomoo_sg_uses_moomoo_color(self):
        badge = broker_badge("Moomoo-SG")
        self.assertIn("#f5a623", badge)

    def test_ibkr_uses_ibkr_color(self):
        badge = broker_badge("IBKR")
        self.assertIn("#00d4ff", badge)

    def test_unknown_broker_uses_default_gray(self):
        badge = broker_badge("SomeFutureBroker")
        self.assertIn("#888", badge)

    def test_empty_or_none_broker_returns_empty_string(self):
        self.assertEqual(broker_badge(""), "")
        self.assertEqual(broker_badge(None), "")


# ─── _finalise() softener consistency ────────────────────────────────────────

class TestFinaliseSoftenerConsistency(unittest.TestCase):
    """
    When the post-conviction SELL->REVIEW softener fires, PositionManager
    must be re-run so profit/scaling/quality fields reflect the FINAL action.
    Fixed 2026-07-06 -- a card could previously show REVIEW (from the
    softener) while scaling_reason still said "flagged SELL by exit engine",
    a contradictory pair of statements on the same card.
    """

    def test_positionmanager_rerun_when_softened(self):
        calls: list[str] = []

        def fake_apply_exit_rules(result, vix, now):
            result = dict(result)
            result["action"] = "SELL"
            result["action_reason"] = "Stop loss hit"
            result["current_gain_pct"] = 25.0   # percentage units, > 10
            return result

        def fake_evaluate(result, conviction_data=None):
            calls.append(result.get("action"))
            result = dict(result)
            result["conviction_score"] = 8.0    # >= 7.5 -> softener eligible
            if result.get("action") == "SELL":
                result["scaling_action"] = "AVOID_ADD"
                result["scaling_reason"] = "Position flagged SELL by exit engine — no scaling."
            else:
                result["scaling_action"] = "HOLD_SIZE"
                result["scaling_reason"] = "Normal hold."
            return result

        with patch.object(ss.SellEngine, "apply_exit_rules", side_effect=fake_apply_exit_rules), \
             patch.object(ss.PositionManager, "evaluate", side_effect=fake_evaluate), \
             patch.object(ss, "_PM_AVAILABLE", True):
            result = ss._finalise({"symbol": "TEST"}, vix=20.0, now=datetime(2025, 1, 1))

        self.assertEqual(result["action"], "REVIEW")
        # PositionManager.evaluate must run twice: once for the original SELL,
        # once more so downstream fields match the softened REVIEW action.
        self.assertEqual(calls, ["SELL", "REVIEW"])
        self.assertEqual(result["scaling_action"], "HOLD_SIZE")
        self.assertNotIn("flagged SELL", result["scaling_reason"])

    def test_positionmanager_not_rerun_when_action_not_sell(self):
        calls: list[str] = []

        def fake_apply_exit_rules(result, vix, now):
            result = dict(result)
            result["action"] = "HOLD"
            result["action_reason"] = "All systems normal"
            result["current_gain_pct"] = 2.0
            return result

        def fake_evaluate(result, conviction_data=None):
            calls.append(result.get("action"))
            result = dict(result)
            result["conviction_score"] = 8.0
            return result

        with patch.object(ss.SellEngine, "apply_exit_rules", side_effect=fake_apply_exit_rules), \
             patch.object(ss.PositionManager, "evaluate", side_effect=fake_evaluate), \
             patch.object(ss, "_PM_AVAILABLE", True):
            result = ss._finalise({"symbol": "TEST"}, vix=20.0, now=datetime(2025, 1, 1))

        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(calls, ["HOLD"])   # called once -- no re-run needed

    def test_positionmanager_not_rerun_when_conviction_too_low(self):
        # SELL + big gain, but conviction below 7.5 threshold -> no softening
        calls: list[str] = []

        def fake_apply_exit_rules(result, vix, now):
            result = dict(result)
            result["action"] = "SELL"
            result["action_reason"] = "Stop loss hit"
            result["current_gain_pct"] = 25.0
            return result

        def fake_evaluate(result, conviction_data=None):
            calls.append(result.get("action"))
            result = dict(result)
            result["conviction_score"] = 3.0   # too low to soften
            return result

        with patch.object(ss.SellEngine, "apply_exit_rules", side_effect=fake_apply_exit_rules), \
             patch.object(ss.PositionManager, "evaluate", side_effect=fake_evaluate), \
             patch.object(ss, "_PM_AVAILABLE", True):
            result = ss._finalise({"symbol": "TEST"}, vix=20.0, now=datetime(2025, 1, 1))

        self.assertEqual(result["action"], "SELL")
        self.assertEqual(calls, ["SELL"])

    def test_positionmanager_not_rerun_when_gain_too_small(self):
        # SELL + high conviction, but gain <= 10% -> no softening
        calls: list[str] = []

        def fake_apply_exit_rules(result, vix, now):
            result = dict(result)
            result["action"] = "SELL"
            result["action_reason"] = "Stop loss hit"
            result["current_gain_pct"] = 4.0   # too small to soften (needs > 10)
            return result

        def fake_evaluate(result, conviction_data=None):
            calls.append(result.get("action"))
            result = dict(result)
            result["conviction_score"] = 9.0
            return result

        with patch.object(ss.SellEngine, "apply_exit_rules", side_effect=fake_apply_exit_rules), \
             patch.object(ss.PositionManager, "evaluate", side_effect=fake_evaluate), \
             patch.object(ss, "_PM_AVAILABLE", True):
            result = ss._finalise({"symbol": "TEST"}, vix=20.0, now=datetime(2025, 1, 1))

        self.assertEqual(result["action"], "SELL")
        self.assertEqual(calls, ["SELL"])


if __name__ == "__main__":
    unittest.main()
