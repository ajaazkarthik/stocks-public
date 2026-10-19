"""Tests for Per-Market Expectancy (tradingview/analysis/per_market_expectancy.py)."""

import pytest

from tradingview.analysis.per_market_expectancy import compute_expectancy, score_expectancy


# ── compute_expectancy ───────────────────────────────────────────────

class TestComputeExpectancy:
    def test_empty_input(self):
        assert compute_expectancy([]) == {}

    def test_single_winning_trade(self):
        trades = [{"broker": "Moomoo-AU", "pnl_pct": 10.0}]
        r = compute_expectancy(trades)
        assert "Moomoo-AU" in r
        stats = r["Moomoo-AU"]
        assert stats["win_rate"] == 100.0
        assert stats["avg_win"] == 10.0
        assert stats["avg_loss"] == 0.0
        assert stats["expectancy"] == 10.0
        assert stats["total_trades"] == 1

    def test_single_losing_trade(self):
        trades = [{"broker": "Zerodha", "pnl_pct": -5.0}]
        r = compute_expectancy(trades)
        stats = r["Zerodha"]
        assert stats["win_rate"] == 0.0
        assert stats["avg_loss"] == 5.0
        assert stats["expectancy"] == -5.0

    def test_mixed_trades_one_broker(self):
        trades = [
            {"broker": "IBKR", "pnl_pct": 20.0},
            {"broker": "IBKR", "pnl_pct": -10.0},
            {"broker": "IBKR", "pnl_pct": 15.0},
            {"broker": "IBKR", "pnl_pct": -5.0},
        ]
        r = compute_expectancy(trades)
        stats = r["IBKR"]
        assert stats["wins"] == 2
        assert stats["losses"] == 2
        assert stats["win_rate"] == 50.0
        assert stats["avg_win"] == 17.5  # (20+15)/2
        assert stats["avg_loss"] == 7.5  # (10+5)/2
        # E = 0.5 * 17.5 - 0.5 * 7.5 = 5.0
        assert stats["expectancy"] == 5.0

    def test_multiple_brokers(self):
        trades = [
            {"broker": "Moomoo-AU", "pnl_pct": 10.0},
            {"broker": "Zerodha", "pnl_pct": -3.0},
            {"broker": "Moomoo-AU", "pnl_pct": -2.0},
            {"broker": "Zerodha", "pnl_pct": -4.0},
        ]
        r = compute_expectancy(trades)
        assert "Moomoo-AU" in r
        assert "Zerodha" in r
        assert r["Moomoo-AU"]["total_trades"] == 2
        assert r["Zerodha"]["total_trades"] == 2

    def test_custom_group_key(self):
        trades = [
            {"exchange": "NYSE", "pnl_pct": 5.0},
            {"exchange": "ASX", "pnl_pct": -2.0},
        ]
        r = compute_expectancy(trades, group_key="exchange")
        assert "NYSE" in r
        assert "ASX" in r

    def test_missing_pnl_skipped(self):
        trades = [
            {"broker": "IBKR", "pnl_pct": 10.0},
            {"broker": "IBKR"},  # no pnl_pct
        ]
        r = compute_expectancy(trades)
        assert r["IBKR"]["total_trades"] == 1

    def test_missing_group_key(self):
        trades = [{"pnl_pct": 5.0}]  # no broker key
        r = compute_expectancy(trades)
        assert "Unknown" in r

    def test_zero_pnl_is_loss(self):
        """pnl_pct = 0 should be counted as a loss (≤ 0)."""
        trades = [{"broker": "Test", "pnl_pct": 0.0}]
        r = compute_expectancy(trades)
        assert r["Test"]["wins"] == 0
        assert r["Test"]["losses"] == 1

    def test_all_winners(self):
        trades = [
            {"broker": "X", "pnl_pct": 5.0},
            {"broker": "X", "pnl_pct": 10.0},
            {"broker": "X", "pnl_pct": 15.0},
        ]
        r = compute_expectancy(trades)
        assert r["X"]["win_rate"] == 100.0
        assert r["X"]["expectancy"] == 10.0  # avg of 5+10+15 = 10

    def test_all_losers(self):
        trades = [
            {"broker": "Y", "pnl_pct": -3.0},
            {"broker": "Y", "pnl_pct": -7.0},
        ]
        r = compute_expectancy(trades)
        assert r["Y"]["win_rate"] == 0.0
        assert r["Y"]["expectancy"] == -5.0  # -(100% * 5.0)

    def test_detail_contains_broker(self):
        trades = [{"broker": "Moomoo-SG", "pnl_pct": 8.0}]
        r = compute_expectancy(trades)
        assert "Moomoo-SG" in r["Moomoo-SG"]["detail"]

    def test_detail_contains_verdict(self):
        trades = [{"broker": "A", "pnl_pct": 10.0}]
        r = compute_expectancy(trades)
        assert "PROFITABLE" in r["A"]["detail"]


# ── score_expectancy ─────────────────────────────────────────────────

class TestScoreExpectancy:
    def test_strong_positive(self):
        r = score_expectancy(8.0)
        assert r["score"] == 3
        assert "STRONG" in r["detail"]

    def test_positive(self):
        r = score_expectancy(2.5)
        assert r["score"] == 1
        assert "POSITIVE" in r["detail"]

    def test_breakeven(self):
        r = score_expectancy(0.0)
        assert r["score"] == 0
        assert "BREAKEVEN" in r["detail"]

    def test_weak(self):
        r = score_expectancy(-3.0)
        assert r["score"] == -1
        assert "WEAK" in r["detail"]

    def test_negative(self):
        r = score_expectancy(-8.0)
        assert r["score"] == -3
        assert "NEGATIVE" in r["detail"]

    def test_signal_name(self):
        r = score_expectancy(5.0)
        assert r["signal"] == "market_expectancy"

    def test_boundary_5(self):
        r = score_expectancy(5.0)
        assert r["score"] == 1  # 5.0 is not > 5.0

    def test_boundary_above_5(self):
        r = score_expectancy(5.01)
        assert r["score"] == 3

    def test_boundary_minus_5(self):
        r = score_expectancy(-5.0)
        assert r["score"] == -3  # -5.0 is NOT > -5.0, falls to else → -3

    def test_boundary_below_minus_5(self):
        r = score_expectancy(-5.01)
        assert r["score"] == -3
