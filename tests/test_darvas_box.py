"""Tests for Darvas Box Breakout (tradingview/analysis/darvas_box.py)."""

import pandas as pd
import pytest

from tradingview.analysis.darvas_box import detect_darvas_box


def _make_ohlcv(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _box_history(n: int = 15, base_vol: float = 1_000_000) -> list[dict]:
    """Generate N bars forming a consolidation box at 100-110."""
    return [
        {"open": 103, "high": 110, "low": 100, "close": 107, "volume": base_vol}
        for _ in range(n)
    ]


class TestDarvasBox:
    def test_insufficient_data(self):
        df = _make_ohlcv([
            {"open": 100, "high": 110, "low": 100, "close": 105, "volume": 1e6}
        ])
        r = detect_darvas_box(df)
        assert r["score"] == 0
        assert r["box_high"] is None
        assert "insufficient" in r["detail"]

    def test_breakout_high_volume(self):
        """Close above box high on high volume → +8."""
        rows = _box_history(15, base_vol=1_000_000)
        # Breakout bar
        rows.append({
            "open": 111, "high": 115, "low": 110,
            "close": 114, "volume": 2_000_000,  # 2x avg
        })
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["score"] == 8
        assert "BREAKOUT" in r["detail"]
        assert r["box_high"] == 110.0
        assert r["box_low"] == 100.0

    def test_breakdown_high_volume(self):
        """Close below box low on high volume → -5."""
        rows = _box_history(15, base_vol=1_000_000)
        rows.append({
            "open": 99, "high": 100, "low": 95,
            "close": 96, "volume": 2_000_000,
        })
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["score"] == -5
        assert "BREAKDOWN" in r["detail"]

    def test_above_box_low_volume(self):
        """Close above box high but low volume → +3 (tepid breakout)."""
        rows = _box_history(15, base_vol=1_000_000)
        rows.append({
            "open": 111, "high": 112, "low": 110,
            "close": 112, "volume": 800_000,  # below avg
        })
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["score"] == 3
        assert "Above box" in r["detail"]

    def test_below_box_low_volume(self):
        """Close below box low but low volume → -2 (tepid breakdown)."""
        rows = _box_history(15, base_vol=1_000_000)
        rows.append({
            "open": 99, "high": 100, "low": 97,
            "close": 98, "volume": 800_000,
        })
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["score"] == -2
        assert "Below box" in r["detail"]

    def test_inside_box(self):
        """Close inside the box → 0."""
        rows = _box_history(15, base_vol=1_000_000)
        rows.append({
            "open": 103, "high": 108, "low": 101,
            "close": 105, "volume": 1_000_000,
        })
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["score"] == 0
        assert "Inside box" in r["detail"]

    def test_box_high_and_low_values(self):
        """box_high and box_low reflect actual history."""
        rows = [
            {"open": 50, "high": 60, "low": 40, "close": 55, "volume": 1e6}
            for _ in range(15)
        ]
        rows.append({"open": 55, "high": 58, "low": 52, "close": 55, "volume": 1e6})
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["box_high"] == 60.0
        assert r["box_low"] == 40.0

    def test_signal_name(self):
        rows = _box_history(15)
        rows.append({"open": 105, "high": 108, "low": 102, "close": 105, "volume": 1e6})
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["signal"] == "darvas"

    def test_custom_box_period(self):
        rows = _box_history(10, base_vol=1e6)
        rows.append({
            "open": 111, "high": 115, "low": 110,
            "close": 114, "volume": 2e6,
        })
        r = detect_darvas_box(_make_ohlcv(rows), box_period=10)
        assert r["score"] == 8

    def test_stop_loss_is_box_low(self):
        """Practical test: box_low should serve as stop-loss for breakout trades."""
        rows = _box_history(15, base_vol=1e6)
        rows.append({"open": 112, "high": 115, "low": 111, "close": 114, "volume": 2e6})
        r = detect_darvas_box(_make_ohlcv(rows))
        assert r["box_low"] == 100.0  # natural stop-loss level
        assert r["score"] == 8  # confirmed breakout
