"""Tests for VPA Anomaly Scoring (tradingview/analysis/vpa_anomaly.py)."""

import numpy as np
import pandas as pd
import pytest

from tradingview.analysis.vpa_anomaly import vpa_signals


def _make_ohlcv(rows: list[dict]) -> pd.DataFrame:
    """Helper to build OHLCV DataFrame from list of dicts."""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _base_history(n: int = 20, base_vol: float = 1_000_000) -> list[dict]:
    """Generate N normal bars for history context."""
    return [
        {"open": 100, "high": 105, "low": 95, "close": 102, "volume": base_vol}
        for _ in range(n)
    ]


class TestVpaSignals:
    def test_insufficient_data(self):
        df = _make_ohlcv([{"open": 100, "high": 105, "low": 95, "close": 102, "volume": 1e6}])
        r = vpa_signals(df, lookback=20)
        assert r["score"] == 0
        assert "insufficient" in r["detail"]

    def test_zero_range_candle(self):
        rows = _base_history(20)
        rows.append({"open": 100, "high": 100, "low": 100, "close": 100, "volume": 1e6})
        r = vpa_signals(_make_ohlcv(rows))
        assert r["score"] == 0
        assert "zero range" in r["detail"]

    def test_trap_up(self):
        """Narrow spread + high volume at resistance → bearish (-5)."""
        rows = _base_history(20, base_vol=1_000_000)
        # Bar at the high of the range with narrow body but big volume
        rows.append({
            "open": 104.5, "high": 106, "low": 103,  # narrow: |104.5-104.8|/(106-103) = 0.1
            "close": 104.8, "volume": 2_000_000,  # 2x avg
        })
        r = vpa_signals(_make_ohlcv(rows))
        assert r["score"] == -5
        assert "TRAP UP" in r["detail"]

    def test_stopping_volume(self):
        """Narrow spread + high volume at support → bullish (+5)."""
        rows = _base_history(20, base_vol=1_000_000)
        # Bar at the low of the range with narrow body but big volume
        rows.append({
            "open": 95.5, "high": 96, "low": 94.5,  # narrow: |95.5-95.2|/(96-94.5) = 0.2
            "close": 95.2, "volume": 2_000_000,  # 2x avg
        })
        r = vpa_signals(_make_ohlcv(rows))
        assert r["score"] == 5
        assert "STOPPING" in r["detail"]

    def test_no_demand(self):
        """Narrow up-close + low volume → weak rally (-3)."""
        rows = _base_history(20, base_vol=1_000_000)
        rows.append({
            "open": 100.0, "high": 104, "low": 99,  # narrow: |100-100.5|/(104-99)=0.1
            "close": 100.5, "volume": 400_000,  # 0.4x avg
        })
        r = vpa_signals(_make_ohlcv(rows))
        assert r["score"] == -3
        assert "NO DEMAND" in r["detail"]

    def test_no_supply(self):
        """Narrow down-close + low volume → continuation (+3)."""
        rows = _base_history(20, base_vol=1_000_000)
        rows.append({
            "open": 101.0, "high": 105, "low": 98,  # narrow: |101-100.5|/(105-98)=0.07
            "close": 100.5, "volume": 400_000,  # 0.4x avg
        })
        r = vpa_signals(_make_ohlcv(rows))
        assert r["score"] == 3
        assert "NO SUPPLY" in r["detail"]

    def test_no_anomaly(self):
        """Wide spread + normal volume → no anomaly."""
        rows = _base_history(20, base_vol=1_000_000)
        rows.append({
            "open": 100, "high": 110, "low": 98,
            "close": 109, "volume": 1_000_000,  # wide spread, normal vol
        })
        r = vpa_signals(_make_ohlcv(rows))
        assert r["score"] == 0
        assert "no anomaly" in r["detail"]

    def test_signal_name(self):
        rows = _base_history(20)
        rows.append({"open": 100, "high": 110, "low": 98, "close": 109, "volume": 1e6})
        r = vpa_signals(_make_ohlcv(rows))
        assert r["signal"] == "vpa"

    def test_custom_lookback(self):
        rows = _base_history(10, base_vol=1e6)
        rows.append({"open": 100, "high": 110, "low": 98, "close": 109, "volume": 1e6})
        r = vpa_signals(_make_ohlcv(rows), lookback=10)
        assert r["signal"] == "vpa"

    def test_equal_open_close_is_down(self):
        """When open == close, treat as down-close (c <= o)."""
        rows = _base_history(20, base_vol=1_000_000)
        rows.append({
            "open": 100.0, "high": 104, "low": 98,
            "close": 100.0, "volume": 400_000,
        })
        r = vpa_signals(_make_ohlcv(rows))
        # Narrow (spread=0, ratio=0 < 0.3) + low vol → should be NO SUPPLY
        assert r["score"] == 3
