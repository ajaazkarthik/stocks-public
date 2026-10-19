"""Tests for F9: Multi-Timeframe Confluence Scorer."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.multi_timeframe import (
    TimeframeData,
    ConfluenceResult,
    compute_confluence,
    compute_l1_mtf_bonus,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. compute_confluence — RSI confluence
# ═══════════════════════════════════════════════════════════════════════════════

class TestRSIConfluence:
    def test_both_oversold(self):
        d = TimeframeData(timeframe="daily", rsi=35)
        w = TimeframeData(timeframe="weekly", rsi=38)
        r = compute_confluence("NVDA", d, w)
        assert r.rsi_confluence is True
        assert r.bonus >= 8

    def test_daily_only_oversold(self):
        d = TimeframeData(timeframe="daily", rsi=35)
        w = TimeframeData(timeframe="weekly", rsi=55)
        r = compute_confluence("NVDA", d, w)
        assert r.rsi_confluence is False
        assert r.bonus == 2  # daily only bonus

    def test_neither_oversold(self):
        d = TimeframeData(timeframe="daily", rsi=55)
        w = TimeframeData(timeframe="weekly", rsi=60)
        r = compute_confluence("NVDA", d, w)
        assert r.rsi_confluence is False
        assert r.bonus == 0

    def test_weekly_only_oversold(self):
        d = TimeframeData(timeframe="daily", rsi=55)
        w = TimeframeData(timeframe="weekly", rsi=35)
        r = compute_confluence("NVDA", d, w)
        # No bonus for weekly-only oversold
        assert r.rsi_confluence is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. compute_confluence — SMA200 confluence
# ═══════════════════════════════════════════════════════════════════════════════

class TestSMAConfluence:
    def test_both_above(self):
        d = TimeframeData(timeframe="daily", sma200_pct=5.0)
        w = TimeframeData(timeframe="weekly", sma200_pct=3.0)
        r = compute_confluence("AAPL", d, w)
        assert r.sma_confluence is True
        assert r.bonus >= 4

    def test_daily_above_weekly_below(self):
        d = TimeframeData(timeframe="daily", sma200_pct=5.0)
        w = TimeframeData(timeframe="weekly", sma200_pct=-2.0)
        r = compute_confluence("AAPL", d, w)
        assert r.sma_confluence is False

    def test_both_below(self):
        d = TimeframeData(timeframe="daily", sma200_pct=-5.0)
        w = TimeframeData(timeframe="weekly", sma200_pct=-10.0)
        r = compute_confluence("AAPL", d, w)
        assert r.sma_confluence is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. compute_confluence — ADX confluence
# ═══════════════════════════════════════════════════════════════════════════════

class TestADXConfluence:
    def test_both_trending(self):
        d = TimeframeData(timeframe="daily", adx=30)
        w = TimeframeData(timeframe="weekly", adx=28)
        r = compute_confluence("X", d, w)
        assert r.bonus >= 2

    def test_one_weak(self):
        d = TimeframeData(timeframe="daily", adx=30)
        w = TimeframeData(timeframe="weekly", adx=15)
        r = compute_confluence("X", d, w)
        assert r.bonus == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. compute_confluence — combined
# ═══════════════════════════════════════════════════════════════════════════════

class TestCombined:
    def test_full_confluence(self):
        """RSI oversold + SMA above + ADX trending = 8 + 4 + 2 = 14."""
        d = TimeframeData(timeframe="daily", rsi=35, sma200_pct=5.0, adx=30)
        w = TimeframeData(timeframe="weekly", rsi=38, sma200_pct=3.0, adx=28)
        r = compute_confluence("NVDA", d, w)
        assert r.bonus == 14
        assert r.rsi_confluence is True
        assert r.sma_confluence is True

    def test_missing_weekly(self):
        d = TimeframeData(timeframe="daily", rsi=35)
        r = compute_confluence("X", d, None)
        assert r.bonus == 0
        assert "Missing" in r.details

    def test_missing_daily(self):
        w = TimeframeData(timeframe="weekly", rsi=35)
        r = compute_confluence("X", None, w)
        assert r.bonus == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. compute_l1_mtf_bonus — integration wrapper
# ═══════════════════════════════════════════════════════════════════════════════

class TestL1MTFBonus:
    def test_basic(self):
        daily = {"tv_rsi": 35, "sma200_pct": 5.0, "tv_adx": 30}
        weekly = {"tv_rsi": 38, "sma200_pct": 3.0, "tv_adx": 28}
        assert compute_l1_mtf_bonus(daily, weekly) == 14

    def test_no_weekly(self):
        daily = {"tv_rsi": 35}
        assert compute_l1_mtf_bonus(daily, None) == 0

    def test_no_data(self):
        assert compute_l1_mtf_bonus({}, {}) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
