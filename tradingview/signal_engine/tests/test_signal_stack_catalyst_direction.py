"""
Regression tests for catalyst direction (review 2026-10-13, Issue 8).

BEFORE the fix: the Quartr catalyst check treated ANY earnings/guidance keyword
as a positive catalyst. The keyword list included "miss", "misses",
"lowered guidance", and "lowers outlook", so "Company misses earnings and
lowered guidance" earned the same +0.5 L3 bonus as a genuine beat.

AFTER: ``_detect_catalyst`` separates catalyst presence from direction. Only
clearly positive evidence earns the bonus; negative, negated, or mixed evidence
earns nothing. Matching uses word/phrase boundaries, not substrings.

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


class TestCatalystDirection:

    def test_negative_earnings_headline_is_bearish(self):
        assert ss._catalyst_direction(
            "Company misses earnings and lowered guidance") == "bearish"

    def test_positive_earnings_headline_is_bullish(self):
        assert ss._catalyst_direction(
            "Company beats estimates and raises guidance") == "bullish"

    def test_negated_beat_is_not_bullish(self):
        assert ss._catalyst_direction("Company did not beat estimates") != "bullish"

    def test_neutral_eps_mention_is_neutral(self):
        assert ss._catalyst_direction("Company reports EPS of $1.20") == "neutral"

    def test_mixed_evidence_is_neutral(self):
        assert ss._catalyst_direction(
            "Company beats on revenue but misses on earnings") == "neutral"

    def test_unrelated_substring_does_not_trigger(self):
        # "beatnik" contains "beat"; "missile" contains "miss".
        assert ss._catalyst_direction("Beatnik coffee chain opens store") == "neutral"
        assert ss._catalyst_direction("Missile defense contract awarded") == "neutral"

    def test_empty_and_none_are_neutral(self):
        assert ss._catalyst_direction("") == "neutral"
        assert ss._catalyst_direction(None) == "neutral"


class TestDetectCatalyst:

    def test_negative_headline_earns_no_bonus(self):
        is_cat, direction = ss._detect_catalyst(
            ["Company misses earnings and lowered guidance"])
        assert is_cat is False
        assert direction == "bearish"

    def test_positive_headline_earns_bonus(self):
        is_cat, direction = ss._detect_catalyst(
            ["Company beats estimates and raises guidance"])
        assert is_cat is True
        assert direction == "bullish"

    def test_negated_headline_earns_no_bonus(self):
        is_cat, _ = ss._detect_catalyst(["Company did not beat estimates"])
        assert is_cat is False

    def test_mixed_list_earns_no_bonus(self):
        is_cat, direction = ss._detect_catalyst([
            "Company beats estimates",
            "Company lowers outlook",
        ])
        assert is_cat is False
        assert direction == "mixed"

    def test_empty_list_earns_no_bonus(self):
        assert ss._detect_catalyst([]) == (False, "neutral")
        assert ss._detect_catalyst(None) == (False, "neutral")


class TestMixedEvidenceAcrossHeadlines:
    """Issue 5 follow-up: mixed evidence must stay conservative.

    A bullish headline plus a headline that is itself mixed (bullish AND
    bearish) previously returned ``(True, "bullish")`` because the mixed
    headline collapsed to neutral and its bearish evidence was lost.
    """

    def test_bullish_plus_mixed_headline_is_not_bullish(self):
        is_cat, direction = ss._detect_catalyst([
            "Company beats estimates",
            "Company beats estimates but lowers guidance",
        ])
        assert is_cat is False
        assert direction == "mixed"

    def test_bullish_plus_bearish_headline_is_mixed(self):
        is_cat, direction = ss._detect_catalyst([
            "Company beats estimates",
            "Company lowers guidance",
        ])
        assert is_cat is False
        assert direction == "mixed"

    def test_pure_bearish_still_reported_bearish(self):
        is_cat, direction = ss._detect_catalyst(["Company misses earnings"])
        assert is_cat is False
        assert direction == "bearish"

    def test_pure_bullish_still_earns_bonus(self):
        is_cat, direction = ss._detect_catalyst(["Company beats estimates"])
        assert is_cat is True
        assert direction == "bullish"

    def test_mixed_evidence_earns_no_bonus_in_scoring(self):
        sent = {"news": {"quartr_catalyst": False,
                         "quartr_catalyst_direction": "mixed",
                         "count": 0, "score": 0.0}}
        out = ss.score_sentiment(sent)
        assert out["quartr_catalyst"] is False
        assert out["quartr_catalyst_direction"] == "mixed"


class TestBonusIntegration:

    def test_bonus_only_for_bullish_catalyst(self):
        bullish = {"news": {"quartr_catalyst": True, "count": 0, "score": 0.0}}
        bearish = {"news": {"quartr_catalyst": False, "count": 0, "score": 0.0}}
        s_bull = ss.score_sentiment(bullish)
        s_bear = ss.score_sentiment(bearish)
        assert s_bull["quartr_catalyst"] is True
        assert s_bear["quartr_catalyst"] is False
        assert s_bull["total"] > s_bear["total"]

    def test_report_representation_compatible(self):
        out = ss.score_sentiment({"news": {"quartr_catalyst": True,
                                           "quartr_catalyst_direction": "bullish",
                                           "count": 0, "score": 0.0}})
        assert out["quartr_catalyst"] is True
        assert out["quartr_catalyst_direction"] == "bullish"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
