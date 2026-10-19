"""Tests for Acquirer's Multiple (tradingview/analysis/acquirers_multiple.py)."""

import math

import pytest

from tradingview.analysis.acquirers_multiple import (
    compute_acquirers_multiple,
    score_acquirers_multiple,
)


# ── compute_acquirers_multiple ───────────────────────────────────────

class TestComputeAcquirersMultiple:
    def test_basic(self):
        assert compute_acquirers_multiple(100, 10) == 10.0

    def test_deep_value(self):
        am = compute_acquirers_multiple(50_000, 10_000)
        assert am == 5.0

    def test_none_ev(self):
        assert compute_acquirers_multiple(None, 10) is None

    def test_none_ebit(self):
        assert compute_acquirers_multiple(100, None) is None

    def test_both_none(self):
        assert compute_acquirers_multiple(None, None) is None

    def test_zero_ebit(self):
        assert compute_acquirers_multiple(100, 0) is None

    def test_negative_ebit(self):
        assert compute_acquirers_multiple(100, -5) is None

    def test_inf_ev(self):
        assert compute_acquirers_multiple(float("inf"), 10) is None

    def test_nan_ebit(self):
        assert compute_acquirers_multiple(100, float("nan")) is None

    def test_large_values(self):
        am = compute_acquirers_multiple(500_000_000_000, 25_000_000_000)
        assert am == 20.0


# ── score_acquirers_multiple ─────────────────────────────────────────

class TestScoreAcquirersMultiple:
    def test_deep_value(self):
        r = score_acquirers_multiple(5.0)
        assert r["score"] == 4
        assert "DEEP VALUE" in r["detail"]

    def test_undervalued(self):
        r = score_acquirers_multiple(10.0)
        assert r["score"] == 2
        assert "UNDERVALUED" in r["detail"]

    def test_fair_value(self):
        r = score_acquirers_multiple(15.0)
        assert r["score"] == 0
        assert "FAIR VALUE" in r["detail"]

    def test_overvalued(self):
        r = score_acquirers_multiple(25.0)
        assert r["score"] == -1
        assert "OVERVALUED" in r["detail"]

    def test_expensive(self):
        r = score_acquirers_multiple(35.0)
        assert r["score"] == -3
        assert "EXPENSIVE" in r["detail"]

    def test_none_input(self):
        r = score_acquirers_multiple(None)
        assert r["score"] == 0
        assert "no data" in r["detail"]

    def test_signal_name(self):
        r = score_acquirers_multiple(10.0)
        assert r["signal"] == "acquirers_multiple"

    def test_boundary_7(self):
        r = score_acquirers_multiple(7.0)
        assert r["score"] == 2  # 7 is in [7, 12) → undervalued

    def test_boundary_12(self):
        r = score_acquirers_multiple(12.0)
        assert r["score"] == 0  # 12 is in [12, 20) → fair

    def test_boundary_20(self):
        r = score_acquirers_multiple(20.0)
        assert r["score"] == -1  # 20 is in [20, 30) → overvalued

    def test_boundary_30(self):
        r = score_acquirers_multiple(30.0)
        assert r["score"] == -3  # 30 is >= 30 → expensive

    def test_detail_contains_am_value(self):
        r = score_acquirers_multiple(15.5)
        assert "15.50" in r["detail"]
