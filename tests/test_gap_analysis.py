"""Tests for Gap Analysis (tradingview/analysis/gap_analysis.py)."""

import pandas as pd
import pytest

from tradingview.analysis.gap_analysis import classify_gap


def _make_ohlcv(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _base_history(n: int = 50, base_vol: float = 1_000_000) -> list[dict]:
    """N bars of normal trading around 100."""
    return [
        {"open": 100, "high": 105, "low": 95, "close": 102, "volume": base_vol}
        for _ in range(n)
    ]


class TestGapClassification:
    def test_insufficient_data(self):
        df = _make_ohlcv([
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1e6},
        ])
        r = classify_gap(df)
        assert r["score"] == 0
        assert "insufficient" in r["detail"]

    def test_zero_prev_close(self):
        rows = [
            {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 1e6},
            {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 1e6},
            {"open": 100, "high": 110, "low": 95, "close": 105, "volume": 1e6},
        ]
        r = classify_gap(_make_ohlcv(rows))
        assert r["score"] == 0
        assert "zero prev close" in r["detail"]

    def test_continuation_gap_up(self):
        """Large gap up + high volume (not at 52w high) → +6."""
        rows = _base_history(50, base_vol=1_000_000)
        # Previous close = 102
        rows.append({
            "open": 105,  # +2.94% gap
            "high": 108, "low": 104,
            "close": 107,
            "volume": 1_600_000,  # 1.6x prev bar
        })
        r = classify_gap(_make_ohlcv(rows))
        assert r["score"] == 6
        assert "CONTINUATION" in r["detail"]
        assert r["gap_pct"] > 0

    def test_continuation_gap_down(self):
        """Large gap down + high volume → -6."""
        rows = _base_history(50, base_vol=1_000_000)
        rows.append({
            "open": 97,  # ≈ -4.9% gap
            "high": 98, "low": 95,
            "close": 96,
            "volume": 1_600_000,
        })
        r = classify_gap(_make_ohlcv(rows))
        assert r["score"] == -6
        assert "CONTINUATION" in r["detail"]
        assert r["gap_pct"] < 0

    def test_exhaustion_gap_up_at_52w_high(self):
        """Large gap up at 52w high + very high volume → -4 (exhaustion = reversal)."""
        # Build history where the bar reaches 52w high
        rows = _base_history(50, base_vol=1_000_000)
        # Previous bar near 52w high
        rows[-1] = {"open": 104, "high": 105, "low": 103, "close": 104, "volume": 1_000_000}
        # Gap up to new 52w high with massive volume
        rows.append({
            "open": 107,  # ~+2.9% gap
            "high": 110, "low": 106,  # ≥105*0.98 → at_52w_high
            "close": 108,
            "volume": 2_500_000,  # 2.5x
        })
        r = classify_gap(_make_ohlcv(rows))
        assert r["score"] == -4
        assert "EXHAUSTION" in r["detail"]

    def test_exhaustion_gap_down_at_52w_low(self):
        """Large gap down at 52w low + very high volume → +4 (exhaustion = reversal)."""
        rows = _base_history(50, base_vol=1_000_000)
        # Previous bar at 52w low area
        rows[-1] = {"open": 96, "high": 97, "low": 95, "close": 96, "volume": 1_000_000}
        # Gap down to new 52w low with massive volume
        rows.append({
            "open": 93,  # ≈ -3.1% gap
            "high": 94, "low": 92,  # ≤95*1.02=96.9 → at_52w_low
            "close": 93,
            "volume": 2_500_000,
        })
        r = classify_gap(_make_ohlcv(rows))
        assert r["score"] == 4
        assert "EXHAUSTION" in r["detail"]

    def test_common_gap_small(self):
        """Small gap < 1% → score 0."""
        rows = _base_history(50, base_vol=1_000_000)
        rows.append({
            "open": 102.5,  # +0.49% gap from prev close 102
            "high": 103, "low": 101,
            "close": 102, "volume": 1_000_000,
        })
        r = classify_gap(_make_ohlcv(rows))
        assert r["score"] == 0
        assert "COMMON" in r["detail"]

    def test_common_gap_low_volume(self):
        """Large gap but low volume → common gap."""
        rows = _base_history(50, base_vol=1_000_000)
        rows.append({
            "open": 105,  # +2.94% gap
            "high": 106, "low": 104,
            "close": 105,
            "volume": 800_000,  # 0.8x — not enough
        })
        r = classify_gap(_make_ohlcv(rows))
        assert r["score"] == 0
        assert "COMMON" in r["detail"]

    def test_signal_name(self):
        rows = _base_history(5)
        rows.append({"open": 103, "high": 105, "low": 101, "close": 104, "volume": 1e6})
        r = classify_gap(_make_ohlcv(rows))
        assert r["signal"] == "gap"

    def test_gap_pct_returned(self):
        rows = _base_history(5)
        rows.append({"open": 105, "high": 106, "low": 104, "close": 105, "volume": 1e6})
        r = classify_gap(_make_ohlcv(rows))
        assert isinstance(r["gap_pct"], float)
        assert r["gap_pct"] > 0

    def test_short_history_still_works(self):
        """Even with only 3 bars (no 52w context), should not crash."""
        rows = [
            {"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1e6},
            {"open": 102, "high": 106, "low": 100, "close": 104, "volume": 1e6},
            {"open": 108, "high": 110, "low": 107, "close": 109, "volume": 2e6},
        ]
        r = classify_gap(_make_ohlcv(rows))
        assert r["signal"] == "gap"
        assert isinstance(r["score"], int)
