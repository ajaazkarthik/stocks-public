"""
Tests for signal_engine.sell_engine — pure-logic sell decision engine.

All tests are network-free. Synthetic OHLC DataFrames and hand-crafted
result dicts drive the tests.
"""

import sys
import math
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ─── path setup so signal_engine is importable ────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent   # tradingview/
sys.path.insert(0, str(_ROOT))

from signal_engine.sell_engine import (
    SellEngine,
    tv_to_yf,
    compute_atr,
    compute_rsi,
    compute_adx,
    trading_days_between,
    fmt_price,
    fmt_pct,
    apply_exit_rules,
    HARD_STOP_MULT,
    HARD_STOP_FLOOR,
    BREAKEVEN_MULT,
    TRAIL_MULT,
    TP1_R,
    TP2_R,
    TIME_STOP_DAYS,
    TIME_STOP_GAIN,
    CGT_PROXIMITY_DAYS,
    CGT_MIN_PROFIT_PCT,
    QUALITY_MAX_PE,
    QUALITY_MIN_ROE,
    QUALITY_MIN_PROFIT_PCT,
)


# ─── OHLC helper ──────────────────────────────────────────────────────────────

def _make_ohlc(n: int, start: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    """Synthetic OHLC with constant H-L spread of 1.0 around each close."""
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame({
        "High":  [c + 0.5 for c in closes],
        "Low":   [c - 0.5 for c in closes],
        "Close": closes,
        "Open":  closes,
    })


def _make_result(**overrides) -> dict:
    """Minimal result dict for apply_exit_rules with safe defaults."""
    base = {
        "symbol":          "NYSE:TEST",
        "entry_price":     100.0,
        "entry_date":      "2024-01-01",
        "high_water_mark": 100.0,
        "current_price":   100.0,
        "atr_entry":       2.0,
        "atr_current":     2.0,
        "rsi":             55.0,
        "adx":             30.0,
        "rel_vol":         1.2,
        "pe":              None,
        "roe":             None,
        "errors":          [],
    }
    base.update(overrides)
    return base


# Fixed "now" datetime — 30 days after 2024-01-01 → ~21 trading days
_NOW_HOLD = datetime(2024, 1, 15)     # 14 calendar days → ~10 trading days (< 20)
_NOW_TIME  = datetime(2024, 2, 10)    # 40 calendar days → ~29 trading days (> 20)


# ─── tv_to_yf ─────────────────────────────────────────────────────────────────

class TestTvToYf(unittest.TestCase):

    def test_asx_suffix(self):
        self.assertEqual(tv_to_yf("ASX:BHP"), "BHP.AX")

    def test_nse_suffix(self):
        self.assertEqual(tv_to_yf("NSE:RELIANCE"), "RELIANCE.NS")

    def test_bse_suffix(self):
        self.assertEqual(tv_to_yf("BSE:INFY"), "INFY.BO")

    def test_nyse_no_suffix(self):
        self.assertEqual(tv_to_yf("NYSE:RCL"), "RCL")

    def test_nasdaq_no_suffix(self):
        self.assertEqual(tv_to_yf("NASDAQ:AAPL"), "AAPL")

    def test_hkex_zero_pads(self):
        self.assertEqual(tv_to_yf("HKEX:700"), "0700.HK")

    def test_hkex_four_digits_unchanged(self):
        self.assertEqual(tv_to_yf("HKEX:2318"), "2318.HK")

    def test_bare_ticker_unchanged(self):
        self.assertEqual(tv_to_yf("AAPL"), "AAPL")

    def test_dash_replaced_by_dot(self):
        self.assertEqual(tv_to_yf("NYSE:BRK-B"), "BRK.B")

    def test_class_method_alias(self):
        self.assertEqual(SellEngine.tv_to_yf("ASX:CBA"), "CBA.AX")


# ─── compute_atr ──────────────────────────────────────────────────────────────

class TestComputeAtr(unittest.TestCase):

    def test_returns_float_for_sufficient_data(self):
        hist = _make_ohlc(30)
        result = compute_atr(hist, period=14)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, float)

    def test_returns_none_for_insufficient_data(self):
        hist = _make_ohlc(10)    # needs 15 for period=14
        self.assertIsNone(compute_atr(hist, period=14))

    def test_trending_atr_positive(self):
        hist = _make_ohlc(30, step=1.0)
        atr = compute_atr(hist, period=14)
        self.assertGreater(atr, 0)

    def test_constant_candles_atr(self):
        # With step=1.0; H-C_prev = 1.5 always → ATR should converge near 1.5
        hist = _make_ohlc(50, step=1.0)
        atr = compute_atr(hist, period=14)
        self.assertAlmostEqual(atr, 1.5, delta=0.1)

    def test_class_method_alias(self):
        hist = _make_ohlc(30)
        self.assertEqual(SellEngine.compute_atr(hist), compute_atr(hist))


# ─── compute_rsi ──────────────────────────────────────────────────────────────

class TestComputeRsi(unittest.TestCase):

    def test_all_gains_returns_100(self):
        hist = _make_ohlc(30, step=1.0)   # strictly rising
        rsi = compute_rsi(hist, period=14)
        self.assertAlmostEqual(rsi, 100.0, delta=0.01)

    def test_all_losses_returns_0(self):
        hist = _make_ohlc(30, step=-1.0)  # strictly falling
        rsi = compute_rsi(hist, period=14)
        self.assertAlmostEqual(rsi, 0.0, delta=0.01)

    def test_returns_none_for_insufficient_data(self):
        hist = _make_ohlc(10)
        self.assertIsNone(compute_rsi(hist, period=14))

    def test_rsi_in_valid_range(self):
        closes = [100 + 5*math.sin(i * 0.5) for i in range(40)]
        hist = pd.DataFrame({
            "High": [c + 1 for c in closes],
            "Low":  [c - 1 for c in closes],
            "Close": closes,
            "Open":  closes,
        })
        rsi = compute_rsi(hist, period=14)
        self.assertIsNotNone(rsi)
        self.assertGreaterEqual(rsi, 0.0)
        self.assertLessEqual(rsi, 100.0)


# ─── compute_adx ──────────────────────────────────────────────────────────────

class TestComputeAdx(unittest.TestCase):

    def test_returns_none_for_insufficient_data(self):
        hist = _make_ohlc(20)   # needs 28 for period=14
        self.assertIsNone(compute_adx(hist, period=14))

    def test_strong_trend_gives_positive_adx(self):
        hist = _make_ohlc(60, step=1.0)
        adx = compute_adx(hist, period=14)
        self.assertIsNotNone(adx)
        self.assertGreater(adx, 0)

    def test_adx_in_valid_range(self):
        hist = _make_ohlc(60, step=1.0)
        adx = compute_adx(hist, period=14)
        self.assertIsNotNone(adx)
        self.assertGreaterEqual(adx, 0.0)
        self.assertLessEqual(adx, 100.0)   # ADX must be in 0-100 (Wilder mean init)


# ─── trading_days_between ─────────────────────────────────────────────────────

class TestTradingDaysBetween(unittest.TestCase):

    def test_seven_calendar_days_gives_five(self):
        start = "2024-01-01"
        end   = datetime(2024, 1, 8)   # 7 calendar days
        result = trading_days_between(start, end)
        self.assertEqual(result, 5)

    def test_zero_days(self):
        start = "2024-01-01"
        end   = datetime(2024, 1, 1)
        self.assertEqual(trading_days_between(start, end), 0)

    def test_invalid_date_returns_none(self):
        self.assertIsNone(trading_days_between("bad-date", datetime.now()))

    def test_twenty_eight_calendar_gives_twenty(self):
        start = "2024-01-01"
        end   = datetime(2024, 1, 29)   # 28 calendar days → round(28*5/7) = 20
        result = trading_days_between(start, end)
        self.assertEqual(result, 20)


# ─── fmt_price ────────────────────────────────────────────────────────────────

class TestFmtPrice(unittest.TestCase):

    def test_none_returns_na(self):
        self.assertEqual(fmt_price(None), "N/A")

    def test_large_price_comma_format(self):
        self.assertEqual(fmt_price(1500.0), "1,500.00")

    def test_medium_price_two_decimals(self):
        self.assertEqual(fmt_price(25.5), "25.50")

    def test_small_price_three_decimals(self):
        self.assertEqual(fmt_price(0.123), "0.123")

    def test_currency_prefix(self):
        self.assertIn("$", fmt_price(100.5, "$"))


# ─── fmt_pct ──────────────────────────────────────────────────────────────────

class TestFmtPct(unittest.TestCase):

    def test_none_returns_na(self):
        self.assertEqual(fmt_pct(None), "N/A")

    def test_positive_has_plus(self):
        self.assertTrue(fmt_pct(5.0).startswith("+"))

    def test_negative_has_minus(self):
        self.assertTrue(fmt_pct(-3.5).startswith("-"))

    def test_zero_has_plus(self):
        self.assertTrue(fmt_pct(0.0).startswith("+"))

    def test_one_decimal_place(self):
        self.assertEqual(fmt_pct(3.14159), "+3.1%")


# ─── apply_exit_rules (the core 8-rule engine) ────────────────────────────────

class TestApplyExitRules(unittest.TestCase):
    """
    All tests use hand-crafted result dicts and a fixed 'now'.
    entry=100, atr_entry=2 → R=4, hard_stop=96, tp1=106, tp2=112.
    """

    def _apply(self, **kwargs):
        result = _make_result(**kwargs)
        return apply_exit_rules(result, vix=kwargs.pop("_vix", 20.0), now=kwargs.pop("_now", _NOW_HOLD))

    def _run(self, vix=20.0, now=_NOW_HOLD, **kwargs):
        result = _make_result(**kwargs)
        return apply_exit_rules(result, vix=vix, now=now)

    # ── Rule 1: Hard Stop ────────────────────────────────────────────────────

    def test_hard_stop_triggers_sell(self):
        """current below hard_stop → SELL"""
        # hard_stop = max(100 - 2*2, 100*0.92) = max(96, 92) = 96
        result = self._run(current_price=95.0)   # 95 <= 96 → stopped_out
        self.assertEqual(result["action"], "REVIEW")  # min_hold_active: _NOW_HOLD is ~10 days < MIN_HOLD_DAYS=14
        self.assertTrue(result["stopped_out"])
        self.assertEqual(result["hard_stop"], 96.0)

    def test_hard_stop_not_triggered_above(self):
        """current just above hard_stop → NOT stopped out by rule 1"""
        result = self._run(current_price=97.0)   # 97 > 96
        self.assertFalse(result["stopped_out"])

    def test_hard_stop_floor_applies_for_large_atr(self):
        """When ATR is large, floor caps the hard stop at entry*0.92"""
        # atr_entry=15 → hard_stop_atr = 100-30 = 70; floor = 92 → hard_stop = 92
        result = self._run(atr_entry=15.0, current_price=91.0)
        self.assertEqual(result["hard_stop"], 92.0)   # floor dominates
        self.assertTrue(result["stopped_out"])         # 91 <= 92

    def test_hard_stop_atr_dominates_when_small(self):
        """When ATR is small, ATR-based stop is above floor"""
        # atr_entry=2 → hard_stop_atr=96; floor=92 → hard_stop=96 (ATR dominates)
        result = self._run(atr_entry=2.0, current_price=99.0)
        self.assertEqual(result["hard_stop"], 96.0)

    # ── Rule 2: Break-Even Stop ──────────────────────────────────────────────

    def test_breakeven_not_triggered_before_trigger(self):
        """HWM below break-even trigger → breakeven_hit=False"""
        # breakeven_trigger = 100 + 1*2 = 102; hwm=101 < 102
        result = self._run(high_water_mark=101.0, current_price=101.0)
        self.assertFalse(result["breakeven_hit"])

    def test_breakeven_triggered(self):
        """HWM ≥ break-even trigger → breakeven_hit=True, stop moves to entry"""
        # hwm=103 >= 102 → breakeven_hit=True
        result = self._run(high_water_mark=103.0, current_price=101.0)
        self.assertTrue(result["breakeven_hit"])
        # effective_stop >= entry (100)
        self.assertGreaterEqual(result["effective_stop"], 100.0)

    # ── Rule 3: Trailing Stop ────────────────────────────────────────────────

    def test_trailing_stop_triggers_sell(self):
        """price drops below HWM - 1.5*ATR → stopped_out=True"""
        # hwm=110, atr_now=2 → trailing=110-3=107; breakeven_hit (110>=102)
        # effective_stop = max(100, 107) = 107
        # current=106 <= 107 → stopped_out=True
        result = self._run(
            high_water_mark=110.0,
            current_price=106.0,
            atr_current=2.0,
        )
        self.assertEqual(result["trailing_stop"], 107.0)
        self.assertTrue(result["stopped_out"])
        self.assertEqual(result["action"], "REVIEW")  # min_hold_active: _NOW_HOLD is ~10 days < MIN_HOLD_DAYS=14

    # ── Rule 4: R-Multiple Targets ───────────────────────────────────────────

    def test_tp1_hit_gives_trail_action(self):
        """HWM ≥ TP1 → tp1_hit=True, action=TRAIL"""
        # R=4, tp1=100+1.5*4=106; hwm=107 >= 106
        result = self._run(high_water_mark=107.0, current_price=107.0)
        self.assertTrue(result["tp1_hit"])
        self.assertFalse(result["tp2_hit"])
        self.assertEqual(result["action"], "TRAIL")

    def test_tp2_hit_gives_trail_action(self):
        """HWM ≥ TP2 → tp2_hit=True, action=TRAIL (TP2 priority)"""
        # R=4, tp2=100+3.0*4=112; hwm=113 >= 112
        result = self._run(high_water_mark=113.0, current_price=113.0)
        self.assertTrue(result["tp2_hit"])
        self.assertEqual(result["action"], "TRAIL")

    def test_r_multiple_calculation(self):
        """R = HARD_STOP_MULT * atr_entry; current_r reflects current position"""
        # R = 2.0 * 2.0 = 4.0; current=104 → current_r=(104-100)/4=1.0
        result = self._run(current_price=104.0)
        self.assertEqual(result["R_value"], 4.0)
        self.assertEqual(result["current_r"], 1.0)

    # ── Rule 5: Time Stop ────────────────────────────────────────────────────

    def test_time_stop_triggers_sell(self):
        """≥20 trading days with < +3% gain → SELL"""
        # now = _NOW_TIME (Feb 10 = ~29 trading days from Jan 1); gain = 0% → triggered
        result = _make_result(current_price=100.0)
        result = apply_exit_rules(result, vix=20.0, now=_NOW_TIME)
        self.assertTrue(result["time_stop_triggered"])
        self.assertEqual(result["action"], "SELL")

    def test_time_stop_not_triggered_when_sufficient_gain(self):
        """≥20 trading days BUT gain ≥ +3% → time stop NOT triggered"""
        result = _make_result(current_price=104.0)   # 4% gain
        result = apply_exit_rules(result, vix=20.0, now=_NOW_TIME)
        self.assertFalse(result["time_stop_triggered"])

    def test_time_stop_not_triggered_for_recent_entry(self):
        """< 20 trading days → time stop never fires"""
        result = _make_result(current_price=100.0)
        result = apply_exit_rules(result, vix=20.0, now=_NOW_HOLD)
        self.assertFalse(result["time_stop_triggered"])

    # ── Rule 6: Signal Decay ─────────────────────────────────────────────────

    def test_two_decay_flags_gives_reduce(self):
        """RSI<45 AND ADX<20 → 2 decay flags → REDUCE"""
        result = self._run(rsi=40.0, adx=15.0, rel_vol=1.2)
        self.assertEqual(result["decay_count"], 2)
        self.assertEqual(result["action"], "REDUCE")

    def test_three_decay_flags_gives_reduce(self):
        """All 3 decay flags → REDUCE"""
        result = self._run(rsi=40.0, adx=15.0, rel_vol=0.3)
        self.assertEqual(result["decay_count"], 3)
        self.assertEqual(result["action"], "REDUCE")

    def test_one_decay_flag_no_action(self):
        """Only 1 decay flag → insufficient for REDUCE"""
        result = self._run(rsi=40.0, adx=25.0, rel_vol=1.2)
        self.assertEqual(result["decay_count"], 1)
        self.assertEqual(result["action"], "HOLD")

    def test_tp1_and_decay_gives_reduce(self):
        """TP1 hit + 2 decay flags → REDUCE (not TRAIL)"""
        result = self._run(
            high_water_mark=107.0, current_price=107.0,
            rsi=40.0, adx=15.0,
        )
        self.assertTrue(result["tp1_hit"])
        self.assertEqual(result["decay_count"], 2)
        self.assertEqual(result["action"], "REDUCE")

    # ── Rule 7: VIX Regime ───────────────────────────────────────────────────

    def test_vix_normal(self):
        result = self._run(vix=18.0)
        self.assertEqual(result["vix_regime"], "NORMAL")
        self.assertFalse(result["vix_force_exit"])

    def test_vix_fear_tightens_stop(self):
        """VIX 25–34 → FEAR regime; tightened_stop = HWM - 1×ATR"""
        # hwm=110, atr_now=2 → tightened_stop=108
        result = _make_result(high_water_mark=110.0, current_price=107.0, atr_current=2.0)
        result = apply_exit_rules(result, vix=28.0, now=_NOW_HOLD)
        self.assertEqual(result["vix_regime"], "FEAR")
        # effective_stop must be at least tightened (108 > trailing 107)
        self.assertGreaterEqual(result["effective_stop"], 108.0)

    def test_vix_extreme_fear_force_exit_loser(self):
        """VIX ≥ 35 + current < entry → vix_force_exit=True, SELL"""
        result = _make_result(current_price=95.0, high_water_mark=95.0)
        result = apply_exit_rules(result, vix=40.0, now=_NOW_HOLD)
        self.assertEqual(result["vix_regime"], "EXTREME_FEAR")
        self.assertTrue(result["vix_force_exit"])
        self.assertEqual(result["action"], "SELL")

    def test_vix_extreme_fear_no_exit_winner(self):
        """VIX ≥ 35 but current > entry → vix_force_exit=False"""
        result = _make_result(current_price=110.0, high_water_mark=110.0)
        result = apply_exit_rules(result, vix=40.0, now=_NOW_HOLD)
        self.assertFalse(result["vix_force_exit"])

    # ── Rule 8: Fundamental Check ────────────────────────────────────────────

    def test_pe_and_roe_flags_give_review(self):
        """PE>60 AND ROE<5 → fund_flag_count=2 → REVIEW"""
        result = self._run(pe=75.0, roe=2.0)
        self.assertTrue(result["fund_pe_flag"])
        self.assertTrue(result["fund_roe_flag"])
        self.assertEqual(result["action"], "REVIEW")

    def test_only_pe_flag_no_review(self):
        """Only PE flag → 1 fund flag, no REVIEW alone"""
        result = self._run(pe=75.0, roe=None)
        self.assertEqual(result["action"], "HOLD")

    def test_no_fund_data_no_flag(self):
        result = self._run(pe=None, roe=None)
        self.assertFalse(result["fund_pe_flag"])
        self.assertFalse(result["fund_roe_flag"])

    # ── Action Priority ──────────────────────────────────────────────────────

    def test_hold_when_all_clear(self):
        """No flags → HOLD"""
        result = self._run(
            current_price=102.0,
            high_water_mark=102.0,
            rsi=55.0, adx=30.0, rel_vol=1.2,
            pe=None, roe=None,
        )
        self.assertEqual(result["action"], "HOLD")

    def test_stopped_out_overrides_tp2(self):
        """If both stopped_out and TP2 hit (due to HWM), stopped_out wins → SELL"""
        # This can't happen with realistic prices but tests priority ordering
        # hwm=113 → tp2=112 hit; but set effective_stop above current to force stop
        # We do this by setting breakeven (hwm=103) + trailing (103-3=100) = 100
        # current=100 → stopped_out (==100) and tp1 hit (hwm=107)
        # Actually let's just ensure stopped_out takes priority
        # current=95 (stopped) + hwm=117 (tp2 hit)
        result = _make_result(current_price=95.0, high_water_mark=117.0)
        result = apply_exit_rules(result, vix=20.0, now=_NOW_HOLD)
        self.assertTrue(result["tp2_hit"])
        self.assertTrue(result["stopped_out"])
        self.assertEqual(result["action"], "REVIEW")  # min_hold_active: _NOW_HOLD is ~10 days < MIN_HOLD_DAYS=14

    # ── ATR fallback ─────────────────────────────────────────────────────────

    def test_atr_entry_none_uses_current_atr_fallback(self):
        """atr_entry=None → falls back to atr_current"""
        result = _make_result(atr_entry=None, atr_current=3.0)
        result = apply_exit_rules(result, vix=20.0, now=_NOW_HOLD)
        # hard_stop = max(100 - 2*3, 100*0.92) = max(94, 92) = 94
        self.assertEqual(result["atr_entry"], 3.0)
        self.assertEqual(result["hard_stop"], 94.0)

    def test_both_atr_none_uses_percentage_fallback(self):
        """Both atr values None → uses entry * 0.02 as fallback"""
        result = _make_result(atr_entry=None, atr_current=None)
        result = apply_exit_rules(result, vix=20.0, now=_NOW_HOLD)
        expected_atr = 100.0 * 0.02   # 2.0
        self.assertAlmostEqual(result["atr_entry"], expected_atr, places=4)

    # ── current_price None (error fetching) ──────────────────────────────────

    def test_null_current_price_gives_hold(self):
        """current_price=None (fetch error) → should not crash, returns HOLD"""
        result = _make_result(current_price=None)
        result = apply_exit_rules(result, vix=20.0, now=_NOW_HOLD)
        self.assertFalse(result["stopped_out"])
        # No TP hit, no time-stop (current_r=None), no VIX force exit
        self.assertEqual(result["action"], "HOLD")

    # ── Computed fields present ───────────────────────────────────────────────

    def test_result_has_all_expected_keys(self):
        result = _make_result()
        result = apply_exit_rules(result, vix=20.0, now=_NOW_HOLD)
        expected_keys = [
            "hard_stop", "breakeven_trigger", "breakeven_hit",
            "trailing_stop", "effective_stop",
            "tp1_price", "tp2_price", "current_r", "R_value", "tp1_hit", "tp2_hit",
            "trading_days", "time_stop_triggered",
            "decay_rsi", "decay_adx", "decay_rv", "decay_count",
            "vix", "vix_regime", "vix_force_exit",
            "fund_pe_flag", "fund_roe_flag",
            "stopped_out", "action", "action_reason", "current_gain_pct",
        ]
        for key in expected_keys:
            self.assertIn(key, result, f"Missing key: {key}")

    # ── SellEngine class facade ───────────────────────────────────────────────

    def test_sellengine_exposes_constants(self):
        """SellEngine class should expose constants as class attributes."""
        self.assertEqual(SellEngine.HARD_STOP_MULT, HARD_STOP_MULT)
        self.assertEqual(SellEngine.TRAIL_MULT, TRAIL_MULT)
        self.assertEqual(SellEngine.TP1_R, TP1_R)
        self.assertEqual(SellEngine.TP2_R, TP2_R)

    def test_sellengine_apply_exit_rules_alias(self):
        """SellEngine.apply_exit_rules is the same function as apply_exit_rules."""
        r1 = _make_result(current_price=95.0)
        r2 = _make_result(current_price=95.0)
        r1 = apply_exit_rules(r1, vix=20.0, now=_NOW_HOLD)
        r2 = SellEngine.apply_exit_rules(r2, vix=20.0, now=_NOW_HOLD)
        self.assertEqual(r1["action"], r2["action"])
        self.assertEqual(r1["hard_stop"], r2["hard_stop"])


# ─── HWM inflation (the CSCL / freshly-bought false-SELL bug) ─────────────────

class TestFreshlyBoughtPositionHwm(unittest.TestCase):
    """
    Reproduces the exact bug seen for NASDAQ:CSCL (Cisco leveraged ETF):

      Just bought at 31.06.  Stock was at 38.67 three months ago.
      If pre-purchase 3-month history is used as HWM the engine fires:
        breakeven_hit  = True  (38.67 >= entry + 1×ATR = 32.40)
        trailing_stop  = 38.67 − 1.5×1.3438 ≈ 36.65
        effective_stop = max(31.06, 36.65) = 36.65
        current=31.05 <= 36.65  →  SELL    ← false alarm

      After the fix (entry_date=None → HWM stays at max(entry, current)):
        hwm            = 31.06
        breakeven_hit  = False  (31.06 < 32.40)
        effective_stop = hard_stop = 28.37
        current=31.05 > 28.37  →  HOLD  ✓
    """

    # Exact CSCL values from the screenshot
    _ENTRY   = 31.06
    _CURRENT = 31.05
    _ATR     = 1.3438

    def _make_cscl(self, hwm: float, entry_date=None) -> dict:
        return {
            "symbol":          "NASDAQ:CSCL",
            "entry_price":     self._ENTRY,
            "entry_date":      entry_date,
            "shares":          13,
            "high_water_mark": hwm,
            "current_price":   self._CURRENT,
            "atr_entry":       self._ATR,
            "atr_current":     self._ATR,
            "rsi":             54.0,
            "adx":             196.0,   # value from screenshot
            "rel_vol":         0.19,
            "pe":              None,
            "roe":             None,
            "errors":          [],
        }

    def test_inflated_hwm_reproduces_false_sell(self):
        """Pre-purchase HWM=38.67 → trailing stop above current → SELL (bug confirmed).

        entry_date=None means holding duration is unknown, NOT "0 days old" —
        min_hold_active must stay False here so the genuine stop fires as SELL
        (2026-07-06 fix: unknown duration is no longer treated as freshly-bought).
        """
        result = apply_exit_rules(self._make_cscl(hwm=38.67), vix=24.2, now=datetime.now())
        self.assertTrue(result["breakeven_hit"],
                        "breakeven should be hit when HWM is inflated to 38.67")
        self.assertGreater(result["effective_stop"], self._CURRENT,
                           "effective_stop must exceed current price to trigger stop")
        self.assertTrue(result["stopped_out"])
        self.assertFalse(result["min_hold_active"])
        self.assertEqual(result["action"], "SELL")

    def test_correct_hwm_gives_hold_for_freshly_bought(self):
        """Correct HWM=entry_price → no false trailing-stop → HOLD."""
        # This is the expected state after the analyse_position fix:
        # entry_date=None → hwm = max(entry_price, current_price) = 31.06
        result = apply_exit_rules(self._make_cscl(hwm=self._ENTRY), vix=24.2, now=datetime.now())
        self.assertFalse(result["breakeven_hit"],
                         "breakeven must NOT fire for a freshly bought position")
        # effective_stop is the hard_stop (≈28.37 for entry−2×ATR)
        self.assertLess(result["effective_stop"], self._CURRENT,
                        "stop must be below current price so position is not stopped out")
        self.assertFalse(result["stopped_out"])
        self.assertNotEqual(result["action"], "SELL")

    def test_hard_stop_computation_cscl(self):
        """Hard stop = max(entry−2×ATR, entry×0.92)."""
        result = apply_exit_rules(self._make_cscl(hwm=self._ENTRY), vix=20.0, now=datetime.now())
        expected_hard_atr   = round(self._ENTRY - 2.0 * self._ATR, 4)   # ≈28.37
        expected_hard_floor = round(self._ENTRY * 0.92, 4)               # ≈28.58
        expected_hard       = round(max(expected_hard_atr, expected_hard_floor), 4)
        self.assertEqual(result["hard_stop"], expected_hard)

    def test_zero_days_held_when_entry_date_none(self):
        """entry_date=None → trading_days_between returns None (unknown, not 0) → time stop never fires."""
        result = apply_exit_rules(self._make_cscl(hwm=self._ENTRY, entry_date=None),
                                  vix=20.0, now=datetime.now())
        self.assertIsNone(result["trading_days"])
        self.assertFalse(result["time_stop_triggered"])

    def test_freshly_bought_at_entry_is_hold(self):
        """Stock bought today at entry, current==entry → safest outcome is HOLD."""
        base = {
            "symbol": "NASDAQ:TEST", "entry_price": 50.0, "entry_date": None,
            "shares": 10, "high_water_mark": 50.0, "current_price": 50.0,
            "atr_entry": 1.0, "atr_current": 1.0,
            "rsi": 60.0, "adx": 30.0, "rel_vol": 1.0, "pe": None, "roe": None, "errors": [],
        }
        result = apply_exit_rules(base, vix=20.0, now=datetime.now())
        self.assertEqual(result["action"], "HOLD")

    def test_slight_loss_freshly_bought_is_hold(self):
        """Small loss immediately after buy — not yet at hard stop → HOLD."""
        base = {
            "symbol": "NASDAQ:TEST", "entry_price": 100.0, "entry_date": None,
            "shares": 5, "high_water_mark": 100.0, "current_price": 97.0,
            "atr_entry": 2.0, "atr_current": 2.0,
            "rsi": 50.0, "adx": 25.0, "rel_vol": 0.9, "pe": None, "roe": None, "errors": [],
        }
        result = apply_exit_rules(base, vix=20.0, now=datetime.now())
        # hard_stop = max(100-4, 92) = 96; current=97 > 96 → not stopped
        self.assertFalse(result["stopped_out"])
        self.assertNotEqual(result["action"], "SELL")


# ─── entry_date=None edge cases ───────────────────────────────────────────────

class TestEntryDateNone(unittest.TestCase):
    """
    When positions come from live Moomoo (no buy date stored), entry_date=None.
    All rules that depend on entry_date must behave safely.
    """

    def test_trading_days_between_none_returns_none(self):
        """trading_days_between(None, now) must return None (unknown), not raise
        and not silently claim 0 (which would mean "freshly bought today")."""
        self.assertIsNone(trading_days_between(None, datetime.now()))

    def test_trading_days_between_empty_string_returns_none(self):
        self.assertIsNone(trading_days_between("", datetime.now()))

    def test_trading_days_between_bad_string_returns_none(self):
        self.assertIsNone(trading_days_between("not-a-date", datetime.now()))

    def test_time_stop_never_fires_when_entry_date_none(self):
        """With entry_date=None, holding period is unknown (not 0) → time_stop_triggered=False."""
        result = _make_result(entry_date=None, current_price=100.0)
        # Use a far-future 'now' to stress-test
        far_future = datetime(2050, 1, 1)
        result = apply_exit_rules(result, vix=20.0, now=far_future)
        self.assertIsNone(result["trading_days"])
        self.assertFalse(result["time_stop_triggered"])

    def test_apply_exit_rules_none_entry_date_no_crash(self):
        """apply_exit_rules must not raise when entry_date=None."""
        result = _make_result(entry_date=None, current_price=105.0)
        try:
            result = apply_exit_rules(result, vix=20.0, now=datetime.now())
        except Exception as exc:
            self.fail(f"apply_exit_rules raised with entry_date=None: {exc}")
        self.assertIn("action", result)

    def test_stopped_out_still_works_when_entry_date_none(self):
        """Hard stop still fires correctly even when entry_date=None.

        Regression guard (2026-07-06 fix): entry_date=None must NOT be treated
        as "freshly bought today" — MIN_HOLD must NOT suppress a genuine stop
        signal just because the holding period is unknown (this previously
        caused every India position's stop-loss to be silently downgraded to
        REVIEW with a false "position only 0d old" message).
        """
        # entry=100, atr=2 → hard_stop=96; current=94 → stopped_out
        result = _make_result(entry_date=None, current_price=94.0)
        result = apply_exit_rules(result, vix=20.0, now=datetime.now())
        self.assertTrue(result["stopped_out"])
        self.assertFalse(result["min_hold_active"], "min_hold must NOT activate when duration is unknown")
        self.assertEqual(result["action"], "SELL")


# ─── moomoo_to_tv symbol mapping ──────────────────────────────────────────────

# Import the helper from sell_strategy (closest file to the one we fixed).
_SELL_DIR = Path(__file__).resolve().parent.parent.parent / "sell"
sys.path.insert(0, str(_SELL_DIR))

try:
    from sell_strategy import _moomoo_to_tv as _ss_moomoo_to_tv   # from sell_strategy.py
    _HAS_SS = True
except Exception:
    _HAS_SS = False

try:
    from sell_actionable import moomoo_to_tv as _sa_moomoo_to_tv   # from sell_actionable.py
    _NYSE_SET_SA = getattr(sys.modules.get("sell_actionable"), "_NYSE_SET", set())
    _HAS_SA = True
except Exception:
    _HAS_SA = False


@unittest.skipUnless(_HAS_SS, "sell_strategy not importable")
class TestMoomooToTvSellStrategy(unittest.TestCase):
    """Tests for _moomoo_to_tv() in sell_strategy.py."""

    def test_au_maps_to_asx(self):
        self.assertEqual(_ss_moomoo_to_tv("AU.CBA"), "ASX:CBA")

    def test_hk_maps_to_hkex(self):
        """HK prefix must map to HKEX, not NASDAQ (bug that was fixed)."""
        self.assertEqual(_ss_moomoo_to_tv("HK.00700"), "HKEX:00700")

    def test_hk_tencent(self):
        self.assertEqual(_ss_moomoo_to_tv("HK.00700"), "HKEX:00700")

    def test_sg_maps_to_sgx(self):
        """SG prefix must map to SGX, not NASDAQ (bug that was fixed)."""
        self.assertEqual(_ss_moomoo_to_tv("SG.D05"), "SGX:D05")

    def test_us_known_nyse_maps_to_nyse(self):
        self.assertEqual(_ss_moomoo_to_tv("US.XLV"), "NYSE:XLV")

    def test_us_unknown_maps_to_nasdaq(self):
        self.assertEqual(_ss_moomoo_to_tv("US.AAPL"), "NASDAQ:AAPL")

    def test_no_dot_returns_unchanged(self):
        self.assertEqual(_ss_moomoo_to_tv("AAPL"), "AAPL")

    def test_cscl_us_maps_to_nasdaq(self):
        """CSCL is a leveraged ETF not in NYSE set → NASDAQ."""
        self.assertEqual(_ss_moomoo_to_tv("US.CSCL"), "NASDAQ:CSCL")


@unittest.skipUnless(_HAS_SA, "sell_actionable not importable")
class TestMoomooToTvSellActionable(unittest.TestCase):
    """Tests for moomoo_to_tv() in sell_actionable.py — same fix applied there."""

    def test_au_maps_to_asx(self):
        self.assertEqual(_sa_moomoo_to_tv("AU.CBA"), "ASX:CBA")

    def test_hk_maps_to_hkex(self):
        self.assertEqual(_sa_moomoo_to_tv("HK.00700"), "HKEX:00700")

    def test_sg_maps_to_sgx(self):
        self.assertEqual(_sa_moomoo_to_tv("SG.D05"), "SGX:D05")

    def test_us_known_nyse(self):
        self.assertEqual(_sa_moomoo_to_tv("US.XLV"), "NYSE:XLV")

    def test_us_generic_nasdaq(self):
        self.assertEqual(_sa_moomoo_to_tv("US.TSLA"), "NASDAQ:TSLA")

    def test_no_dot_unchanged(self):
        self.assertEqual(_sa_moomoo_to_tv("AAPL"), "AAPL")


# ─── CGT Proximity Guard ──────────────────────────────────────────────────────

class TestCgtProximityGuard(unittest.TestCase):
    """CGT proximity: stopped-out profitable position within 60d of CGT discount → REVIEW."""

    # Entry=100, ATR=2 → effective_stop ≈ 96; price=95 → stopped_out=True
    # 50d to CGT discount, +25% gain → should be REVIEW, not SELL

    def _base(self, **kw) -> dict:
        base = _make_result(
            entry_price=100.0,
            high_water_mark=130.0,
            current_price=95.0,   # below effective stop
            atr_entry=2.0,
            atr_current=2.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=None, roe=None,
            days_to_lt=50,        # 50 days to CGT discount
            entry_date="2023-06-01",
        )
        base.update(kw)
        return base

    def test_cgt_proximity_gives_review_not_sell(self):
        # entry=100, HWM=165, ATR=5 → trailing_stop=157.5; current=145 → stopped_out=True
        # current_gain_pct = (145-100)/100 = 45% > CGT_MIN_PROFIT_PCT → guard should fire
        r = _make_result(
            entry_price=100.0,
            high_water_mark=165.0,
            current_price=145.0,   # profitable (+45%) but below trailing stop (157.5)
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=None, roe=None,
            days_to_lt=50,
            entry_date="2023-06-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertEqual(result["action"], "REVIEW",
                         f"Expected REVIEW (CGT proximity), got {result['action']}: {result.get('action_reason')}")
        self.assertTrue(result.get("cgt_proximity_override"),
                        "cgt_proximity_override should be True")
        self.assertIn("CGT", result.get("action_reason", ""))

    def test_cgt_proximity_at_boundary_60d(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=165.0,
            current_price=145.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=None, roe=None,
            days_to_lt=60,       # exactly at boundary
            entry_date="2023-06-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertEqual(result["action"], "REVIEW")
        self.assertTrue(result.get("cgt_proximity_override"))

    def test_cgt_proximity_beyond_60d_still_sells(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=165.0,
            current_price=145.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=None, roe=None,
            days_to_lt=90,       # 90d > 60d threshold → no CGT guard
            entry_date="2023-06-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("cgt_proximity_override"))

    def test_cgt_proximity_not_fired_when_not_in_profit(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=100.5,
            current_price=90.0,   # loss
            atr_entry=2.0, atr_current=2.0,
            rsi=40.0, adx=18.0, rel_vol=0.4,
            pe=None, roe=None,
            days_to_lt=30,
            entry_date="2023-06-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("cgt_proximity_override"),
                         "CGT guard should not fire when position is at a loss")

    def test_cgt_proximity_vix_force_exit_bypasses_guard(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=110.0,
            current_price=85.0,   # loss + extreme VIX → force exit
            atr_entry=2.0, atr_current=2.0,
            rsi=30.0, adx=10.0, rel_vol=0.2,
            pe=None, roe=None,
            days_to_lt=30,
            entry_date="2023-06-01",
        )
        # VIX=40 → EXTREME_FEAR + current_gain_pct < 0 → vix_force_exit=True
        result = apply_exit_rules(r, vix=40.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("cgt_proximity_override"),
                         "CGT guard must not fire when VIX force exit is active")


# ─── Quality Stop Guard ───────────────────────────────────────────────────────

class TestQualityStopGuard(unittest.TestCase):
    """Quality stop: healthy fundamentals (PE≤40, ROE≥10%) + large gain → REVIEW not SELL."""

    def _stopped_result(self, pe, roe, gain_pct_frac, **kw) -> dict:
        entry = 100.0
        current = entry * (1 + gain_pct_frac)
        hwm = max(entry * 1.5, current + 1)   # HWM above current so trail stop fires
        r = _make_result(
            entry_price=entry,
            high_water_mark=hwm,
            current_price=current * 0.92,    # pull back to force stopped_out
            atr_entry=2.0, atr_current=2.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=pe, roe=roe,
            days_to_lt=None,                # no CGT guard
            entry_date="2022-01-01",        # long ago → no MIN_HOLD guard
        )
        r.update(kw)
        return r

    def test_quality_position_stopped_gives_review(self):
        # entry=100, HWM=170, ATR=5 → trailing_stop=162.5; current=135 → stopped_out=True
        # current_gain_pct = (135-100)/100 = 35% > QUALITY_MIN_PROFIT_PCT (20%)
        r = _make_result(
            entry_price=100.0,
            high_water_mark=170.0,
            current_price=135.0,   # profitable (+35%) but below trailing stop (162.5)
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=28.0, roe=18.0,     # healthy: PE 28 ≤ 40, ROE 18 ≥ 10
            days_to_lt=None,
            entry_date="2022-01-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertEqual(result["action"], "REVIEW",
                         f"Expected REVIEW (quality guard), got {result['action']}: {result.get('action_reason')}")
        self.assertTrue(result.get("quality_stop_override"))
        self.assertIn("PE", result.get("action_reason", ""))
        self.assertIn("ROE", result.get("action_reason", ""))

    def test_quality_guard_not_fired_for_overvalued_pe(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=170.0,
            current_price=135.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=65.0, roe=15.0,    # PE 65 > 40 → overvalued, no quality guard
            days_to_lt=None,
            entry_date="2022-01-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("quality_stop_override"),
                         "Quality guard should not fire for overvalued PE > 40")

    def test_quality_guard_not_fired_for_low_roe(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=170.0,
            current_price=135.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=20.0, roe=6.0,     # ROE 6 < 10 → low-quality, no guard
            days_to_lt=None,
            entry_date="2022-01-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("quality_stop_override"),
                         "Quality guard should not fire for low-quality ROE < 10")

    def test_quality_guard_not_fired_for_small_gain(self):
        # entry=100, HWM=108, ATR=2 → trailing_stop=105; current=104 → stopped_out=True
        # current_gain_pct = (104-100)/100 = +4% < QUALITY_MIN_PROFIT_PCT (20%)
        r = _make_result(
            entry_price=100.0,
            high_water_mark=108.0,
            current_price=104.0,   # small gain only (+4%)
            atr_entry=2.0, atr_current=2.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=22.0, roe=14.0,
            days_to_lt=None,
            entry_date="2022-01-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("quality_stop_override"),
                         "Quality guard requires > 20% gain; small gain should not activate it")

    def test_quality_guard_not_fired_missing_pe(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=170.0,
            current_price=135.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=None, roe=15.0,    # no PE data
            days_to_lt=None,
            entry_date="2022-01-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("quality_stop_override"),
                         "Quality guard requires PE data; None PE should not activate it")

    def test_quality_guard_not_fired_missing_roe(self):
        r = _make_result(
            entry_price=100.0,
            high_water_mark=170.0,
            current_price=135.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=25.0, roe=None,    # no ROE data
            days_to_lt=None,
            entry_date="2022-01-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertFalse(result.get("quality_stop_override"),
                         "Quality guard requires ROE data; None ROE should not activate it")

    def test_mnd_scenario_review_not_sell(self):
        """
        Regression test for ASX:MND case (Apr 2026):
        315d held, PE 27.3, ROE 20.5%, +65% gain, trailing stop hit.
        System was incorrectly issuing SELL — should be REVIEW.
        """
        r = _make_result(
            symbol="ASX:MND",
            entry_price=17.49,
            high_water_mark=36.31,
            current_price=28.89,   # below trailing stop 34.74
            atr_entry=2.093,
            atr_current=1.0466,
            rsi=52.0,
            adx=15.0,              # ADX < 20 → decay flag
            rel_vol=0.61,          # RelVol < 0.5 is the boundary — 0.61 is OK
            pe=27.3,
            roe=20.5,
            days_to_lt=50,         # 50 days to CGT discount
            entry_date="2025-06-10",
        )
        result = apply_exit_rules(r, vix=19.5, now=datetime(2026, 4, 21))
        self.assertEqual(result["action"], "REVIEW",
                         f"MND regression: expected REVIEW, got {result['action']}: {result.get('action_reason')}")
        # At least one of the new guards should have fired
        self.assertTrue(
            result.get("cgt_proximity_override") or result.get("quality_stop_override"),
            "Expected CGT proximity or quality stop guard to fire for MND"
        )


# ─── _compute_urgency_score / SellEngine.compute_urgency_score ───────────────

class TestUrgencyScore(unittest.TestCase):
    """0-10 exit-urgency score — independent of conviction_score."""

    def test_hold_no_modifiers_is_base_value(self):
        score = SellEngine.compute_urgency_score(
            action="HOLD", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=0, fund_flag_count=0,
        )
        self.assertEqual(score, 1.5)

    def test_action_base_values(self):
        bases = {"SELL": 9.0, "REDUCE": 6.5, "TRAIL": 5.0, "REVIEW": 4.0, "HOLD": 1.5}
        for action, expected in bases.items():
            score = SellEngine.compute_urgency_score(
                action=action, stopped_out=False, vix_force_exit=False,
                time_stop_triggered=False, decay_count=0, fund_flag_count=0,
            )
            self.assertEqual(score, expected, f"action={action}")

    def test_unknown_action_falls_back_to_hold_base(self):
        score = SellEngine.compute_urgency_score(
            action="NOT_A_REAL_ACTION", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=0, fund_flag_count=0,
        )
        self.assertEqual(score, 1.5)

    def test_modifiers_add_independently(self):
        # REVIEW base 4.0 + stopped_out 1.0 + time_stop 0.5 = 5.5
        score = SellEngine.compute_urgency_score(
            action="REVIEW", stopped_out=True, vix_force_exit=False,
            time_stop_triggered=True, decay_count=0, fund_flag_count=0,
        )
        self.assertEqual(score, 5.5)

    def test_decay_count_modifier_scales_linearly(self):
        # HOLD base 1.5 + decay_count * 0.3
        score_2 = SellEngine.compute_urgency_score(
            action="HOLD", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=2, fund_flag_count=0,
        )
        score_5 = SellEngine.compute_urgency_score(
            action="HOLD", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=5, fund_flag_count=0,
        )
        self.assertEqual(score_2, 1.5 + 2 * 0.3)
        self.assertEqual(score_5, 1.5 + 5 * 0.3)

    def test_decay_count_capped_at_decay_factor_count(self):
        # decay_count above DECAY_FACTOR_COUNT (5) must not add further modifier —
        # there are only 5 possible decay sub-signals, so >5 is a caller bug, not
        # a legitimately stronger signal.
        score_at_cap  = SellEngine.compute_urgency_score(
            action="HOLD", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=SellEngine.DECAY_FACTOR_COUNT,
            fund_flag_count=0,
        )
        score_over_cap = SellEngine.compute_urgency_score(
            action="HOLD", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=100, fund_flag_count=0,
        )
        self.assertEqual(score_at_cap, score_over_cap)

    def test_fund_flag_count_capped_at_two(self):
        score_at_cap = SellEngine.compute_urgency_score(
            action="HOLD", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=0, fund_flag_count=2,
        )
        score_over_cap = SellEngine.compute_urgency_score(
            action="HOLD", stopped_out=False, vix_force_exit=False,
            time_stop_triggered=False, decay_count=0, fund_flag_count=10,
        )
        self.assertEqual(score_at_cap, score_over_cap)

    def test_clamped_to_ten(self):
        # SELL(9.0) + stopped_out(1.0) + vix_force_exit(1.0) + time_stop(0.5)
        # + decay(5*0.3=1.5) + fund(2*0.3=0.6) = 13.6 → clamp to 10.0
        score = SellEngine.compute_urgency_score(
            action="SELL", stopped_out=True, vix_force_exit=True,
            time_stop_triggered=True, decay_count=5, fund_flag_count=2,
        )
        self.assertEqual(score, 10.0)

    def test_apply_exit_rules_sets_urgency_score_key(self):
        r = _make_result(entry_price=100.0, high_water_mark=100.0, current_price=101.0,
                          atr_entry=2.0, atr_current=2.0, rsi=55.0, adx=30.0, rel_vol=1.2,
                          entry_date="2026-01-01")
        result = apply_exit_rules(r, vix=15.0, now=datetime(2026, 1, 5))
        self.assertIn("urgency_score", result)
        self.assertIsInstance(result["urgency_score"], float)
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(result["urgency_score"], 1.5)


# ─── DECAY_FACTOR_COUNT (single source of truth for "N/5" displays) ──────────

class TestDecayFactorCount(unittest.TestCase):

    def test_decay_factor_count_is_five(self):
        # Must match the actual number of decay sub-signals computed below
        # (RSI + ADX + RelVol + Darvas breakdown + VPA trap-up). If a 6th
        # decay signal is ever added, this constant AND this test must both
        # be updated together, or every "N/5" display silently becomes wrong.
        self.assertEqual(SellEngine.DECAY_FACTOR_COUNT, 5)

    def test_all_five_decay_flags_triggered_reason_uses_constant(self):
        r = _make_result(
            entry_price=100.0, high_water_mark=100.0, current_price=101.0,
            atr_entry=2.0, atr_current=2.0,
            rsi=30.0, adx=15.0, rel_vol=0.3,     # 3 base decay flags
            darvas_score=-5, vpa_score=-5,        # +2 book-sourced decay flags = 5 total
            entry_date="2026-01-01",
        )
        result = apply_exit_rules(r, vix=15.0, now=datetime(2026, 1, 5))
        self.assertEqual(result["decay_count"], 5)
        self.assertEqual(result["action"], "REDUCE")
        self.assertEqual(
            result["action_reason"],
            f"5/{SellEngine.DECAY_FACTOR_COUNT} decay flags triggered",
        )
        self.assertIn("5/5", result["action_reason"])


# ─── Override-note concatenation ("Also applies: ...") ───────────────────────

class TestOverrideNoteConcatenation(unittest.TestCase):
    """
    When a position is stopped out and MULTIPLE independent overrides apply
    (e.g. both CGT-proximity AND quality-stop conditions are true), the
    lower-priority-but-still-true override must not be silently dropped —
    it should be appended via "Also applies: ...".
    """

    def test_quality_note_appended_when_cgt_wins(self):
        # entry=100, ATR=5 → hard_stop=max(90,92)=92; HWM=170 → breakeven_hit
        # (170 >= 105) → trailing_stop=170-1.5*5=162.5 → effective_stop=162.5.
        # current=135 → stopped_out (135 <= 162.5). gain=(135-100)/100=35%.
        r = _make_result(
            entry_price=100.0,
            high_water_mark=170.0,
            current_price=135.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=28.0, roe=18.0,          # quality_stop_override: PE<=40, ROE>=10, gain>20%
            days_to_lt=45,              # cgt_proximity_override: 0<45<=60, gain>10%
            entry_date="2022-01-01",    # long-held -> min_hold_active is False
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertEqual(result["action"], "REVIEW")
        self.assertTrue(result.get("cgt_proximity_override"))
        self.assertTrue(result.get("quality_stop_override"))
        # CGT note wins (checked first) ...
        self.assertIn("CGT", result["action_reason"])
        # ... but the quality-stop consideration must still be visible, not hidden.
        self.assertIn("Also applies:", result["action_reason"])
        self.assertIn("quality position", result["action_reason"])

    def test_no_also_applies_when_only_one_override_active(self):
        # Same as above but PE too high for quality_stop_override to ever apply.
        r = _make_result(
            entry_price=100.0,
            high_water_mark=170.0,
            current_price=135.0,
            atr_entry=5.0, atr_current=5.0,
            rsi=50.0, adx=25.0, rel_vol=1.0,
            pe=90.0, roe=18.0,           # PE 90 > 40 -> quality_stop_override False
            days_to_lt=45,
            entry_date="2022-01-01",
        )
        result = apply_exit_rules(r, vix=19.0, now=datetime(2024, 4, 1))
        self.assertEqual(result["action"], "REVIEW")
        self.assertTrue(result.get("cgt_proximity_override"))
        self.assertFalse(result.get("quality_stop_override"))
        self.assertNotIn("Also applies:", result["action_reason"])


if __name__ == "__main__":
    unittest.main()

