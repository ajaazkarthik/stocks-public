"""
Unit tests for ibkr_async.models

Tests all domain model classes with no I/O, no mocking required.
These are pure-function tests: given inputs → expected outputs.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from ibkr_async.models import (
    AccountSummary,
    AssetClass,
    ConnectionStatus,
    HealthStatus,
    Position,
    Quote,
)


# ── Position ──────────────────────────────────────────────────────────────────

class TestPosition:
    """Tests for Position domain model."""

    def _make(self, **overrides) -> Position:
        defaults = dict(
            symbol="AAPL", exchange="SMART", currency="USD",
            asset_class=AssetClass.STOCK,
            quantity=100.0, avg_cost=150.0,
            market_price=175.0, market_value=17_500.0,
            unrealised_pnl=2_500.0, realised_pnl=0.0,
        )
        defaults.update(overrides)
        return Position(**defaults)

    def test_pnl_pct_long_position(self):
        pos = self._make(quantity=100, avg_cost=150.0, unrealised_pnl=2500.0)
        # cost = 100 * 150 = 15,000; pnl% = 2500/15000 * 100 = 16.67%
        assert abs(pos.pnl_pct - 16.666) < 0.01

    def test_pnl_pct_zero_cost_returns_zero(self):
        pos = self._make(quantity=0, avg_cost=0, unrealised_pnl=100)
        assert pos.pnl_pct == 0.0

    def test_pnl_pct_negative_pnl(self):
        pos = self._make(quantity=100, avg_cost=200.0, unrealised_pnl=-1000.0)
        # cost = 20000; pnl% = -1000/20000 * 100 = -5%
        assert abs(pos.pnl_pct - (-5.0)) < 0.01

    def test_is_long_positive_quantity(self):
        assert self._make(quantity=10).is_long is True

    def test_is_long_negative_quantity(self):
        assert self._make(quantity=-10).is_long is False

    def test_to_dict_keys(self):
        d = self._make().to_dict()
        expected_keys = {
            "symbol", "exchange", "currency", "qty", "avg_cost",
            "last_price", "market_value", "unrealised_pnl", "realised_pnl",
            "pnl_pct", "broker", "account_id", "timestamp",
        }
        assert expected_keys == set(d.keys())

    def test_to_dict_values(self):
        pos = self._make(symbol="TSLA", quantity=50, avg_cost=200.0,
                         market_price=250.0, market_value=12_500.0,
                         unrealised_pnl=2_500.0, broker="IBKR",
                         account_id="DU12345")
        d = pos.to_dict()
        assert d["symbol"]      == "TSLA"
        assert d["qty"]         == 50.0
        assert d["avg_cost"]    == 200.0
        assert d["last_price"]  == 250.0
        assert d["broker"]      == "IBKR"
        assert d["account_id"]  == "DU12345"

    def test_to_dict_pnl_pct_rounded(self):
        pos = self._make(quantity=100, avg_cost=150.0, unrealised_pnl=2500.0)
        d = pos.to_dict()
        # pnl_pct should be rounded to 2 decimal places
        assert d["pnl_pct"] == round(pos.pnl_pct, 2)

    def test_to_dict_timestamp_is_iso_string(self):
        d = self._make().to_dict()
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(d["timestamp"])
        assert dt.tzinfo is not None  # UTC-aware

    def test_frozen_immutability(self):
        pos = self._make()
        with pytest.raises(Exception):
            pos.symbol = "NEW"  # type: ignore

    def test_asset_class_option(self):
        pos = self._make(asset_class=AssetClass.OPTION)
        assert pos.asset_class == AssetClass.OPTION

    def test_default_broker_is_ibkr(self):
        pos = self._make()
        assert pos.broker == "IBKR"


# ── AccountSummary ────────────────────────────────────────────────────────────

class TestAccountSummary:

    def _make(self, **overrides) -> AccountSummary:
        defaults = dict(
            account_id="DU12345",
            net_liquidation=100_000.0,
            total_cash=20_000.0,
            unrealised_pnl=8_500.0,
            realised_pnl=0.0,
        )
        defaults.update(overrides)
        return AccountSummary(**defaults)

    def test_to_dict_keys(self):
        d = self._make().to_dict()
        expected = {
            "account_id", "net_liquidation", "total_cash",
            "unrealised_pnl", "realised_pnl", "equity_with_loan",
            "available_funds", "excess_liquidity", "currency", "timestamp",
        }
        assert expected == set(d.keys())

    def test_to_dict_values(self):
        acc = self._make(account_id="U999", net_liquidation=50_000.0)
        d = acc.to_dict()
        assert d["account_id"]      == "U999"
        assert d["net_liquidation"] == 50_000.0

    def test_frozen_immutability(self):
        acc = self._make()
        with pytest.raises(Exception):
            acc.account_id = "MODIFIED"  # type: ignore

    def test_default_currency_usd(self):
        acc = self._make()
        assert acc.currency == "USD"

    def test_timestamp_is_utc_aware(self):
        acc = self._make()
        assert acc.timestamp.tzinfo is not None


# ── Quote ─────────────────────────────────────────────────────────────────────

class TestQuote:

    def _make(self, **overrides) -> Quote:
        defaults = dict(
            symbol="AAPL", exchange="SMART", currency="USD",
            bid=174.95, ask=175.05, last=175.00,
        )
        defaults.update(overrides)
        return Quote(**defaults)

    def test_mid_with_valid_bid_ask(self):
        q = self._make(bid=100.0, ask=101.0)
        assert q.mid == 100.5

    def test_mid_falls_back_to_last_when_bid_zero(self):
        q = self._make(bid=0.0, ask=0.0, last=99.5)
        assert q.mid == 99.5

    def test_spread_bps(self):
        q = self._make(bid=100.0, ask=101.0)  # mid=100.5, spread=1.0
        # spread_bps = (1.0 / 100.5) * 10000 ≈ 99.5 bps
        assert 90 < q.spread_bps < 110

    def test_spread_bps_zero_mid_returns_zero(self):
        q = self._make(bid=0.0, ask=0.0, last=0.0)
        assert q.spread_bps == 0.0

    def test_frozen_immutability(self):
        q = self._make()
        with pytest.raises(Exception):
            q.bid = 999.0  # type: ignore


# ── HealthStatus ──────────────────────────────────────────────────────────────

class TestHealthStatus:

    def _make(self, connected=True, status=ConnectionStatus.CONNECTED) -> HealthStatus:
        return HealthStatus(
            is_connected=connected,
            status=status,
            host="127.0.0.1",
            port=7496,
            client_id=10,
            latency_ms=0.5,
        )

    def test_is_healthy_when_connected(self):
        assert self._make(connected=True, status=ConnectionStatus.CONNECTED).is_healthy

    def test_not_healthy_when_disconnected(self):
        h = self._make(connected=False, status=ConnectionStatus.DISCONNECTED)
        assert not h.is_healthy

    def test_not_healthy_when_connected_status_mismatch(self):
        # is_connected=True but status=RECONNECTING → not considered healthy
        h = self._make(connected=True, status=ConnectionStatus.RECONNECTING)
        assert not h.is_healthy

    def test_to_dict_contains_expected_keys(self):
        d = self._make().to_dict()
        expected = {
            "is_connected", "status", "host", "port",
            "latency_ms", "account_id", "error_message", "checked_at",
        }
        assert expected == set(d.keys())

    def test_to_dict_status_is_string(self):
        d = self._make().to_dict()
        assert isinstance(d["status"], str)  # Enum serialised to its .value


# ── AssetClass ────────────────────────────────────────────────────────────────

class TestAssetClass:

    def test_stock_value(self):
        assert AssetClass.STOCK.value == "STK"

    def test_option_value(self):
        assert AssetClass.OPTION.value == "OPT"

    def test_unknown_value(self):
        assert AssetClass.UNKNOWN.value == "UNKNOWN"

    def test_str_enum_comparable_to_string(self):
        assert AssetClass.STOCK == "STK"


# ── ConnectionStatus ──────────────────────────────────────────────────────────

class TestConnectionStatus:

    def test_connected_value(self):
        assert ConnectionStatus.CONNECTED.value == "CONNECTED"

    def test_str_enum_comparable(self):
        assert ConnectionStatus.DISCONNECTED == "DISCONNECTED"
