"""Tests for india_cgt.positions_for_sell_strategy()."""

import math
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "india-brokers"))

from india_cgt import positions_for_sell_strategy


def _make_pos(symbol="NSE:INFY", qty=10, avg_cost=100.0, last_price=120.0, broker="Zerodha"):
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_cost": avg_cost,
        "last_price": last_price,
        "unrealized_pnl": (last_price - avg_cost) * qty if last_price else 0.0,
        "broker": broker,
        "currency": "INR",
    }


def _get_data_ret(positions):
    return {
        "positions": positions,
        "zerodha_count": len(positions),
        "angelone_count": 0,
        "total_value": 0.0,
        "total_pnl": 0.0,
        "warnings": [],
    }


class TestPositionsForSellStrategy:

    def test_positions_for_sell_strategy_returns_correct_keys(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([_make_pos()])):
            result = positions_for_sell_strategy()
        assert len(result) == 1
        required = {"symbol", "entry_price", "entry_date", "shares",
                    "atr_at_entry", "high_water_mark", "current_price", "broker"}
        assert required.issubset(result[0].keys())

    def test_positions_for_sell_strategy_filters_zero_avg_cost(self):
        positions = [_make_pos(avg_cost=0.0), _make_pos(symbol="NSE:TCS", avg_cost=500.0)]
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret(positions)):
            result = positions_for_sell_strategy()
        assert len(result) == 1
        assert result[0]["symbol"] == "NSE:TCS"

    def test_positions_for_sell_strategy_filters_negative_avg_cost(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([_make_pos(avg_cost=-5.0)])):
            result = positions_for_sell_strategy()
        assert result == []

    def test_positions_for_sell_strategy_handles_none_last_price(self):
        pos = _make_pos()
        pos["last_price"] = None
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([pos])):
            result = positions_for_sell_strategy()
        assert result[0]["current_price"] is None

    def test_positions_for_sell_strategy_handles_zero_last_price(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([_make_pos(last_price=0.0)])):
            result = positions_for_sell_strategy()
        assert result[0]["current_price"] is None

    def test_positions_for_sell_strategy_handles_inf_last_price(self):
        pos = _make_pos(last_price=float("inf"))
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([pos])):
            result = positions_for_sell_strategy()
        assert result[0]["current_price"] is None

    def test_positions_for_sell_strategy_handles_nan_last_price(self):
        pos = _make_pos()
        pos["last_price"] = float("nan")
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([pos])):
            result = positions_for_sell_strategy()
        assert result[0]["current_price"] is None

    def test_positions_for_sell_strategy_qty_mapped_to_shares_as_int(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([_make_pos(qty=7)])):
            result = positions_for_sell_strategy()
        assert result[0]["shares"] == 7
        assert isinstance(result[0]["shares"], int)

    def test_positions_for_sell_strategy_returns_empty_on_broker_error(self):
        with mock.patch("india_cgt.get_data", side_effect=Exception("Connection refused")):
            result = positions_for_sell_strategy()
        assert result == []

    def test_positions_for_sell_strategy_entry_date_is_none(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([_make_pos()])):
            result = positions_for_sell_strategy()
        assert result[0]["entry_date"] is None

    def test_positions_for_sell_strategy_atr_at_entry_is_none(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([_make_pos()])):
            result = positions_for_sell_strategy()
        assert result[0]["atr_at_entry"] is None

    def test_positions_for_sell_strategy_valid_price_is_rounded(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([_make_pos(last_price=123.456789)])):
            result = positions_for_sell_strategy()
        # round(ltp, 4) applied
        assert result[0]["current_price"] == round(123.456789, 4)

    def test_positions_for_sell_strategy_empty_positions_returns_empty(self):
        with mock.patch("india_cgt.get_data", return_value=_get_data_ret([])):
            result = positions_for_sell_strategy()
        assert result == []
