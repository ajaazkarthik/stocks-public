"""
Unit tests for backtest indicator helpers.

All tests are deterministic (synthetic data, no network access).
Run with:  python -m pytest backtest/tests/ -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Path bootstrap — works whether run as module or directly
# ---------------------------------------------------------------------------
_REPO = Path(__file__).parent.parent.parent  # tradingview/
sys.path.insert(0, str(_REPO))

from backtest.backtest_buy_sell_v2 import (
    calc_adx_dmi,
    calc_atr,
    calc_macd,
    calc_rsi,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _series(values: list[float], start: str = "2020-01-01") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


def _ohlcv(
    n: int,
    price_fn=None,
    spread_pct: float = 0.005,
    vol: float = 1_000_000,
    start: str = "2020-01-01",
) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame."""
    idx   = pd.date_range(start, periods=n, freq="B")
    price = price_fn(n) if price_fn is not None else np.linspace(100.0, 200.0, n)
    h     = price * (1 + spread_pct)
    lo    = price * (1 - spread_pct)
    op    = price * (1 - spread_pct * 0.5)
    volume = np.full(n, vol)
    return pd.DataFrame(
        {"Open": op, "High": h, "Low": lo, "Close": price, "Volume": volume},
        index=idx,
    )


# ===========================================================================
class TestCalcRSI(unittest.TestCase):

    def test_first_n_minus_one_are_nan(self):
        """RSI requires `period` bars — first period-1 values must be NaN."""
        close = _series([100.0] * 30)
        rsi = calc_rsi(close, period=14)
        self.assertTrue(rsi.iloc[:13].isna().all(),
                        "RSI values before period are expected to be NaN")

    def test_all_gains_rsi_approaches_100(self):
        """
        Consistently rising prices → RSI must be very high (≥ 90).
        NOTE: A perfectly flat loss series (avg_l = 0) is replaced with NaN by
        the implementation, so we use a mostly-gains series with one tiny dip to
        keep avg_l > 0 and RSI computable.
        """
        prices = [100.0 + i for i in range(60)]
        prices[30] = prices[29] - 0.5   # price at bar 30 < bar 29 → one negative diff so avg_l > 0
        close  = _series(prices)
        rsi    = calc_rsi(close, period=14)
        valid  = rsi.dropna()
        self.assertGreater(len(valid), 0, "Expected non-NaN RSI values")
        last = float(valid.iloc[-1])
        self.assertGreater(last, 90.0,
                           f"Expected RSI > 90 for mostly-up series, got {last:.2f}")

    def test_all_losses_rsi_approaches_0(self):
        """Consistently falling prices → RSI must be very low (≤ 10)."""
        prices = [200.0 - i for i in range(60)]
        close  = _series(prices)
        rsi    = calc_rsi(close, period=14)
        last   = rsi.dropna().iloc[-1]
        self.assertLess(last, 10.0,
                        f"Expected RSI < 10 for pure downtrend, got {last:.2f}")

    def test_rsi_bounds(self):
        """RSI must always be within [0, 100]."""
        rng    = np.random.default_rng(0)
        prices = 100 + np.cumsum(rng.normal(0, 1, 200))
        close  = _series(prices.tolist())
        rsi    = calc_rsi(close, period=14).dropna()
        self.assertTrue((rsi >= 0).all() and (rsi <= 100).all(),
                        "RSI must be within [0, 100]")

    def test_period_parameter_respected(self):
        """Different period values must produce different results on the same data."""
        # Use a noisy uptrend so both RSI series have valid (non-NaN) values
        rng    = np.random.default_rng(5)
        prices = list(100.0 + np.cumsum(rng.normal(0.3, 0.8, 80)))
        close  = _series(prices)
        rsi7   = calc_rsi(close, period=7).dropna()
        rsi14  = calc_rsi(close, period=14).dropna()
        self.assertGreater(len(rsi7),  0, "RSI(7) produced no valid values")
        self.assertGreater(len(rsi14), 0, "RSI(14) produced no valid values")
        # Align to same length (rsi14 is shorter due to larger period)
        aligned7  = rsi7.values[-len(rsi14):]
        aligned14 = rsi14.values
        self.assertFalse(
            np.allclose(aligned7, aligned14, atol=0.01),
            "RSI with period=7 and period=14 should differ on noisy data",
        )

    def test_returns_series_same_index(self):
        close = _series([100.0] * 50)
        rsi   = calc_rsi(close)
        self.assertEqual(list(rsi.index), list(close.index))


# ===========================================================================
class TestCalcMACD(unittest.TestCase):

    def test_constant_price_macd_is_zero(self):
        """Constant price → all EMAs equal → MACD = 0."""
        close       = _series([100.0] * 100)
        macd, signal = calc_macd(close)
        # After the EMA has converged, MACD must be ~0
        self.assertAlmostEqual(float(macd.iloc[-1]), 0.0, places=6)
        self.assertAlmostEqual(float(signal.iloc[-1]), 0.0, places=6)

    def test_returns_two_series(self):
        close        = _series([100.0 + i * 0.5 for i in range(80)])
        result       = calc_macd(close)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        macd, sig = result
        self.assertIsInstance(macd, pd.Series)
        self.assertIsInstance(sig,  pd.Series)

    def test_uptrend_macd_positive(self):
        """In a consistent uptrend the faster EMA must eventually exceed the slower."""
        prices      = [100.0 + i * 0.4 for i in range(120)]
        close       = _series(prices)
        macd, _     = calc_macd(close)
        # After initial convergence (>50 bars) MACD should be positive
        self.assertGreater(float(macd.iloc[-1]), 0.0)

    def test_downtrend_macd_negative(self):
        prices  = [200.0 - i * 0.4 for i in range(120)]
        close   = _series(prices)
        macd, _ = calc_macd(close)
        self.assertLess(float(macd.iloc[-1]), 0.0)

    def test_same_index_as_input(self):
        close        = _series([100.0] * 60)
        macd, signal = calc_macd(close)
        self.assertEqual(list(macd.index),   list(close.index))
        self.assertEqual(list(signal.index), list(close.index))


# ===========================================================================
class TestCalcATR(unittest.TestCase):

    def test_zero_spread_gives_near_zero_atr(self):
        """OHLC all equal → True Range = 0 → ATR must converge to ~0."""
        n  = 60
        px = np.full(n, 100.0)
        df = pd.DataFrame({
            "Open": px, "High": px, "Low": px, "Close": px
        }, index=pd.date_range("2020-01-01", periods=n))
        atr = calc_atr(df["High"], df["Low"], df["Close"], period=14)
        self.assertAlmostEqual(float(atr.iloc[-1]), 0.0, places=6)

    def test_atr_positive_for_volatile_data(self):
        rng = np.random.default_rng(7)
        n   = 100
        c   = 100.0 + np.cumsum(rng.normal(0, 1, n))
        h   = c + abs(rng.normal(1, 0.5, n))
        lo  = c - abs(rng.normal(1, 0.5, n))
        atr = calc_atr(
            pd.Series(h), pd.Series(lo), pd.Series(c), period=14
        )
        self.assertGreater(float(atr.dropna().iloc[-1]), 0.0)

    def test_atr_non_negative(self):
        """ATR must never be negative."""
        rng = np.random.default_rng(99)
        n   = 200
        c   = 50.0 + np.cumsum(rng.normal(0, 2, n))
        h   = c + abs(rng.normal(0.5, 0.2, n))
        lo  = c - abs(rng.normal(0.5, 0.2, n))
        atr = calc_atr(
            pd.Series(h), pd.Series(lo), pd.Series(c), period=14
        ).dropna()
        self.assertTrue((atr >= 0).all(), "ATR must be non-negative")

    def test_atr_increases_with_wider_bars(self):
        """Wider bar spreads should produce higher ATR."""
        n   = 100
        c   = np.linspace(100, 110, n)
        h_narrow = c + 0.1
        lo_narrow = c - 0.1
        h_wide   = c + 2.0
        lo_wide  = c - 2.0
        atr_n = calc_atr(pd.Series(h_narrow), pd.Series(lo_narrow), pd.Series(c))
        atr_w = calc_atr(pd.Series(h_wide),   pd.Series(lo_wide),   pd.Series(c))
        self.assertLess(float(atr_n.dropna().mean()), float(atr_w.dropna().mean()))

    def test_returns_series_same_length(self):
        n   = 80
        c   = pd.Series(np.ones(n) * 100)
        h   = c + 1
        lo  = c - 1
        atr = calc_atr(h, lo, c)
        self.assertEqual(len(atr), n)


# ===========================================================================
class TestCalcADXDMI(unittest.TestCase):

    def test_returns_three_components(self):
        df     = _ohlcv(100)
        result = calc_adx_dmi(df["High"], df["Low"], df["Close"])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    def test_adx_positive_in_trend(self):
        """Strong consistent uptrend → ADX should be > 0 after warmup."""
        df       = _ohlcv(150, price_fn=lambda n: 100.0 + np.arange(n) * 0.5)
        adx, pdi, mdi = calc_adx_dmi(df["High"], df["Low"], df["Close"])
        self.assertGreater(float(adx.dropna().iloc[-1]), 0.0)

    def test_plus_di_dominates_in_uptrend(self):
        """In an uptrend, +DI should exceed −DI."""
        df       = _ohlcv(200, price_fn=lambda n: 100.0 + np.arange(n) * 0.5)
        _, pdi, mdi = calc_adx_dmi(df["High"], df["Low"], df["Close"])
        self.assertGreater(float(pdi.dropna().iloc[-1]),
                           float(mdi.dropna().iloc[-1]),
                           "+DI must exceed -DI in a consistent uptrend")

    def test_minus_di_dominates_in_downtrend(self):
        """In a downtrend, −DI should exceed +DI."""
        df = _ohlcv(200, price_fn=lambda n: 200.0 - np.arange(n) * 0.5)
        _, pdi, mdi = calc_adx_dmi(df["High"], df["Low"], df["Close"])
        self.assertGreater(float(mdi.dropna().iloc[-1]),
                           float(pdi.dropna().iloc[-1]),
                           "-DI must exceed +DI in a consistent downtrend")

    def test_adx_non_negative(self):
        rng = np.random.default_rng(55)
        n   = 200
        c   = 100.0 + np.cumsum(rng.normal(0, 1, n))
        h   = c + abs(rng.normal(0.5, 0.2, n))
        lo  = c - abs(rng.normal(0.5, 0.2, n))
        df  = pd.DataFrame(
            {"High": h, "Low": lo, "Close": c},
            index=pd.date_range("2020-01-01", periods=n),
        )
        adx, _, _ = calc_adx_dmi(df["High"], df["Low"], df["Close"])
        self.assertTrue((adx.dropna() >= 0).all(), "ADX must be non-negative")

    def test_all_series_same_length_as_input(self):
        df    = _ohlcv(80)
        adx, pdi, mdi = calc_adx_dmi(df["High"], df["Low"], df["Close"])
        for s in (adx, pdi, mdi):
            self.assertEqual(len(s), 80)


if __name__ == "__main__":
    unittest.main()
