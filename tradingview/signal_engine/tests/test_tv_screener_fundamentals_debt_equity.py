"""
Regression tests for the debt-to-equity normalization overwrite
(review 2026-10-13, Issue 1).

BEFORE the fix: ``tv_fund_to_compat()`` multiplied TV's raw debt_to_equity
ratio by 100 to match the scorer's thresholds, but the trailing ``**tv_fund``
spread re-inserted the raw ratio under the same key, so the converted value was
discarded. A raw ratio of 2.0 came back as 2.0 instead of 200.0, and the scorer
(val(de, 30, 200)) then read a leveraged company as very low leverage.

AFTER: raw TV fields are merged first and the normalized/aliased keys win.

All tests are network-free — no HTTP calls are made.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
if str(TV_DIR) not in sys.path:
    sys.path.insert(0, str(TV_DIR))

import tv_screener_fundamentals as tvf
import signal_stack as ss


def _raw_tv(debt_to_equity):
    """Minimal raw TV screener row as returned by fetch_tv_fundamentals()."""
    return {
        "name": "NASDAQ:TEST",
        "debt_to_equity": debt_to_equity,
        "return_on_equity": 15.0,
        "net_margin": 10.0,
        "gross_margin": 40.0,
        "current_ratio": 2.0,
        "free_cash_flow": 1_000_000.0,
        "market_cap_basic": 1_000_000_000.0,
        "price_earnings_ttm": 20.0,
        "earnings_per_share_diluted_yoy_growth_ttm": 12.0,
        "sector": "Technology Services",
    }


class TestDebtToEquityNormalization:

    @pytest.mark.parametrize(("raw", "expected"), [
        (0.3, 30.0),
        (1.0, 100.0),
        (2.0, 200.0),
    ])
    def test_raw_ratio_is_scaled_by_100(self, raw, expected):
        compat = tvf.tv_fund_to_compat(_raw_tv(raw), pro_symbol="NASDAQ:TEST")
        assert compat["debt_to_equity"] == pytest.approx(expected)

    def test_none_ratio_stays_unavailable(self):
        compat = tvf.tv_fund_to_compat(_raw_tv(None), pro_symbol="NASDAQ:TEST")
        assert compat["debt_to_equity"] is None

    def test_missing_ratio_stays_unavailable(self):
        raw = _raw_tv(1.0)
        del raw["debt_to_equity"]
        compat = tvf.tv_fund_to_compat(raw, pro_symbol="NASDAQ:TEST")
        assert compat["debt_to_equity"] is None

    def test_zero_ratio_remains_zero(self):
        compat = tvf.tv_fund_to_compat(_raw_tv(0.0), pro_symbol="NASDAQ:TEST")
        assert compat["debt_to_equity"] == 0.0

    def test_raw_field_is_not_left_in_normalized_units(self):
        """The raw ratio must not survive under the normalized key."""
        compat = tvf.tv_fund_to_compat(_raw_tv(2.0), pro_symbol="NASDAQ:TEST")
        assert compat["debt_to_equity"] != 2.0


class TestAdapterToScorerHealthDirection:

    def _health(self, raw_de):
        compat = tvf.tv_fund_to_compat(_raw_tv(raw_de), pro_symbol="NASDAQ:TEST")
        return ss.score_fundamental(compat)["health"]

    def test_low_leverage_scores_higher_health_than_high_leverage(self):
        low  = self._health(0.3)   # → 30.0  → s_de = 10.0
        high = self._health(2.0)   # → 200.0 → s_de = 0.0
        assert low > high, (
            f"low-leverage health ({low}) must exceed high-leverage health ({high})"
        )

    def test_otherwise_identical_companies_differ_only_by_leverage(self):
        assert self._health(0.3) != self._health(2.0)


class TestAdapterAliasesAndRawFieldsPreserved:

    def test_aliases_still_present(self):
        compat = tvf.tv_fund_to_compat(_raw_tv(1.0), pro_symbol="NASDAQ:TEST")
        assert compat["roe"] == 15.0
        assert compat["profit_margin"] == 10.0
        assert compat["gross_margin"] == 40.0
        assert compat["forward_pe"] == 20.0
        assert compat["earnings_growth"] == 12.0

    def test_unrelated_raw_fields_remain_available(self):
        compat = tvf.tv_fund_to_compat(_raw_tv(1.0), pro_symbol="NASDAQ:TEST")
        # Raw TV keys that have no alias must still be reachable.
        assert compat["return_on_equity"] == 15.0
        assert compat["net_margin"] == 10.0
        assert compat["market_cap_basic"] == 1_000_000_000.0

    def test_currency_derived_from_exchange(self):
        compat = tvf.tv_fund_to_compat(_raw_tv(1.0), pro_symbol="ASX:CBA")
        assert compat["currency"] == "AUD"


class TestFallbackAliasPreservation:
    """Issue 1 follow-up: populated fallback aliases must not be erased.

    The Moomoo basicinfo fallback injects ``pe_ratio``/``market_cap``/
    ``current_price`` directly into the raw TV dict. Merging ``**tv_fund``
    first fixed debt normalization but let a missing TV field overwrite those
    populated fallback values with ``None``.
    """

    def _fallback_raw(self):
        return {
            "name": "HKEX:0700",
            "current_price": 380.0,
            "market_cap": 3_500_000_000_000.0,
            "pe_ratio": 18.5,
        }

    def test_populated_fallback_values_survive(self):
        compat = tvf.tv_fund_to_compat(self._fallback_raw(), pro_symbol="HKEX:0700")
        assert compat["current_price"] == 380.0
        assert compat["market_cap"] == 3_500_000_000_000.0
        assert compat["pe_ratio"] == 18.5

    def test_tv_value_still_wins_when_present(self):
        raw = self._fallback_raw()
        raw["price_earnings_ttm"] = 22.0
        raw["market_cap_basic"] = 9.0
        raw["close"] = 400.0
        compat = tvf.tv_fund_to_compat(raw, pro_symbol="HKEX:0700")
        assert compat["pe_ratio"] == 22.0
        assert compat["market_cap"] == 9.0
        assert compat["current_price"] == 400.0

    def test_caller_supplied_price_beats_fallback(self):
        compat = tvf.tv_fund_to_compat(self._fallback_raw(), current_price=999.0,
                                       pro_symbol="HKEX:0700")
        assert compat["current_price"] == 999.0

    def test_absent_everywhere_stays_none(self):
        compat = tvf.tv_fund_to_compat({"name": "X"}, pro_symbol="HKEX:0700")
        assert compat["market_cap"] is None
        assert compat["pe_ratio"] is None
        assert compat["current_price"] is None

    def test_debt_normalization_still_applies_with_fallback_present(self):
        raw = self._fallback_raw()
        raw["debt_to_equity"] = 2.0
        compat = tvf.tv_fund_to_compat(raw, pro_symbol="HKEX:0700")
        assert compat["debt_to_equity"] == pytest.approx(200.0)
        assert compat["pe_ratio"] == 18.5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
