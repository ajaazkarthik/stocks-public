"""Tests for O'Neil RS Rating (tradingview/analysis/rs_rating.py)."""

import numpy as np
import pandas as pd
import pytest

from tradingview.analysis.rs_rating import compute_rs_rating, score_rs, weighted_return


# ── weighted_return ──────────────────────────────────────────────────

class TestWeightedReturn:
    def test_basic_uptrend(self):
        closes = pd.Series(np.linspace(100, 150, 253))
        wr = weighted_return(closes)
        assert wr is not None
        assert wr > 0

    def test_basic_downtrend(self):
        closes = pd.Series(np.linspace(150, 100, 253))
        wr = weighted_return(closes)
        assert wr is not None
        assert wr < 0

    def test_too_short(self):
        closes = pd.Series([100.0] * 50)
        assert weighted_return(closes) is None

    def test_minimum_length_64(self):
        closes = pd.Series(np.linspace(100, 110, 64))
        wr = weighted_return(closes)
        assert wr is not None
        assert wr > 0

    def test_zero_last_price(self):
        closes = pd.Series([0.0] * 64)
        assert weighted_return(closes) is None

    def test_flat_prices(self):
        closes = pd.Series([100.0] * 253)
        wr = weighted_return(closes)
        assert wr is not None
        assert abs(wr) < 0.001

    def test_recent_acceleration(self):
        """If the most recent quarter is strong, weighted return should be higher."""
        # Flat then rise
        late_mover = pd.Series([100.0] * 190 + list(np.linspace(100, 150, 63)))
        # Rise then flat
        early_mover = pd.Series(list(np.linspace(100, 150, 63)) + [150.0] * 190)
        wr_late = weighted_return(late_mover)
        wr_early = weighted_return(early_mover)
        assert wr_late > wr_early, "O'Neil RS weights recent performance more heavily"


# ── compute_rs_rating ────────────────────────────────────────────────

class TestComputeRsRating:
    def test_all_same_return(self):
        histories = {f"T{i}": pd.Series([100.0] * 64) for i in range(10)}
        ratings = compute_rs_rating(histories)
        assert len(ratings) == 10
        for v in ratings.values():
            assert 1 <= v <= 99

    def test_clear_leader(self):
        histories = {}
        for i in range(20):
            histories[f"T{i}"] = pd.Series(np.linspace(100, 100 + i * 5, 64))
        ratings = compute_rs_rating(histories)
        assert ratings["T19"] > ratings["T0"]
        assert ratings["T19"] >= 80

    def test_clear_laggard(self):
        histories = {}
        for i in range(20):
            histories[f"T{i}"] = pd.Series(np.linspace(100, 100 + i * 5, 64))
        ratings = compute_rs_rating(histories)
        assert ratings["T0"] < 30

    def test_empty_input(self):
        assert compute_rs_rating({}) == {}

    def test_all_too_short(self):
        histories = {f"T{i}": pd.Series([100.0] * 10) for i in range(5)}
        assert compute_rs_rating(histories) == {}

    def test_single_ticker(self):
        histories = {"ONLY": pd.Series(np.linspace(100, 150, 64))}
        ratings = compute_rs_rating(histories)
        assert len(ratings) == 1
        assert 1 <= ratings["ONLY"] <= 99

    def test_range_bounds(self):
        histories = {}
        for i in range(100):
            histories[f"T{i}"] = pd.Series(np.linspace(100, 100 + i, 64))
        ratings = compute_rs_rating(histories)
        for v in ratings.values():
            assert 1 <= v <= 99


# ── score_rs ─────────────────────────────────────────────────────────

class TestScoreRs:
    def test_leader_80(self):
        r = score_rs(80)
        assert r["score"] == 6
        assert r["signal"] == "rs_rating"

    def test_leader_99(self):
        assert score_rs(99)["score"] == 6

    def test_strong_70(self):
        assert score_rs(70)["score"] == 3

    def test_strong_79(self):
        assert score_rs(79)["score"] == 3

    def test_laggard_1(self):
        assert score_rs(1)["score"] == -4

    def test_laggard_29(self):
        assert score_rs(29)["score"] == -4

    def test_neutral_30(self):
        assert score_rs(30)["score"] == 0

    def test_neutral_50(self):
        assert score_rs(50)["score"] == 0

    def test_neutral_69(self):
        assert score_rs(69)["score"] == 0

    def test_detail_contains_value(self):
        r = score_rs(85)
        assert "85" in r["detail"]
