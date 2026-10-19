"""Tests for F5: Broker Fee/Cost Tracker."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.broker_fee_tracker import (
    TradeFee,
    BrokerFeeStats,
    parse_trade_fees,
    aggregate_by_broker,
    aggregate_by_month,
    cheapest_broker_by_market,
    generate_html,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TradeFee
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradeFee:
    def test_total_fee(self):
        f = TradeFee(broker="IBKR", symbol="NVDA", date="2026-01-15",
                     trade_value=10000, commission=5.0, other_fees=2.0)
        assert f.total_fee == 7.0

    def test_fee_pct(self):
        f = TradeFee(broker="IBKR", symbol="NVDA", date="2026-01-15",
                     trade_value=10000, commission=10.0, other_fees=0.0)
        assert f.fee_pct == pytest.approx(0.1)

    def test_fee_pct_zero_value(self):
        f = TradeFee(broker="IBKR", symbol="X", date="", trade_value=0, commission=5.0)
        assert f.fee_pct == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. BrokerFeeStats
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerFeeStats:
    def test_fee_pct(self):
        s = BrokerFeeStats(broker="IBKR", total_fees=50, total_trade_value=100000)
        assert s.fee_pct == pytest.approx(0.05)

    def test_fee_pct_zero(self):
        s = BrokerFeeStats(broker="IBKR")
        assert s.fee_pct == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. parse_trade_fees
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseTradeFees:
    def test_basic(self):
        raw = [{"broker": "IBKR", "symbol": "NVDA", "date": "2026-01-15",
                "trade_value": 10000, "commission": 5.0, "market": "US"}]
        fees = parse_trade_fees(raw)
        assert len(fees) == 1
        assert fees[0].broker == "IBKR"
        assert fees[0].commission == 5.0

    def test_negative_values_abs(self):
        raw = [{"broker": "X", "symbol": "Y", "date": "", "trade_value": -5000, "commission": -3}]
        fees = parse_trade_fees(raw)
        assert fees[0].trade_value == 5000
        assert fees[0].commission == 3

    def test_empty(self):
        assert parse_trade_fees([]) == []

    def test_missing_fields(self):
        raw = [{"broker": "X"}]
        fees = parse_trade_fees(raw)
        assert len(fees) == 1
        assert fees[0].trade_value == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. aggregate_by_broker
# ═══════════════════════════════════════════════════════════════════════════════

class TestAggregateByBroker:
    def test_pre_seeds_all_six(self):
        stats = aggregate_by_broker([])
        assert set(stats.keys()) == {"AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"}

    def test_aggregation(self):
        fees = [
            TradeFee(broker="IBKR", symbol="A", date="2026-01-01", trade_value=10000, commission=5),
            TradeFee(broker="IBKR", symbol="B", date="2026-01-02", trade_value=5000, commission=3),
        ]
        stats = aggregate_by_broker(fees)
        assert stats["IBKR"].trade_count == 2
        assert stats["IBKR"].total_fees == 8
        assert stats["IBKR"].total_trade_value == 15000

    def test_unknown_broker(self):
        fees = [TradeFee(broker="NewBroker", symbol="X", date="", trade_value=100, commission=1)]
        stats = aggregate_by_broker(fees)
        assert "NewBroker" in stats


# ═══════════════════════════════════════════════════════════════════════════════
# 5. aggregate_by_month
# ═══════════════════════════════════════════════════════════════════════════════

class TestAggregateByMonth:
    def test_basic(self):
        fees = [
            TradeFee(broker="IBKR", symbol="A", date="2026-01-15", trade_value=10000, commission=5),
            TradeFee(broker="IBKR", symbol="B", date="2026-01-20", trade_value=5000, commission=3),
            TradeFee(broker="IBKR", symbol="C", date="2026-02-10", trade_value=8000, commission=4),
        ]
        monthly = aggregate_by_month(fees)
        assert "2026-01" in monthly
        assert "2026-02" in monthly
        assert monthly["2026-01"]["IBKR"] == 8  # 5+3

    def test_empty(self):
        assert aggregate_by_month([]) == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 6. cheapest_broker_by_market
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheapestBrokerByMarket:
    def test_basic(self):
        fees = [
            TradeFee(broker="IBKR", symbol="A", date="", trade_value=10000,
                     commission=5, market="US"),
            TradeFee(broker="Moomoo-AU", symbol="B", date="", trade_value=10000,
                     commission=10, market="US"),
        ]
        result = cheapest_broker_by_market(fees)
        assert result["US"][0] == "IBKR"  # cheaper

    def test_empty(self):
        assert cheapest_broker_by_market([]) == {}

    def test_no_market_excluded(self):
        fees = [TradeFee(broker="X", symbol="Y", date="", trade_value=100, commission=1, market="")]
        assert cheapest_broker_by_market(fees) == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 7. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def _make_stats(self):
        return {b: BrokerFeeStats(broker=b) for b in
                ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]}

    def test_all_brokers_present(self):
        html = generate_html(self._make_stats(), {}, {})
        for b in ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]:
            assert b in html

    def test_with_cheapest(self):
        cheapest = {"US": ("IBKR", 0.05), "AU": ("Moomoo-AU", 0.1)}
        html = generate_html(self._make_stats(), {}, cheapest)
        assert "US" in html
        assert "IBKR" in html

    def test_written(self, tmp_path):
        out = tmp_path / "fees.html"
        generate_html(self._make_stats(), {}, {}, out)
        assert out.exists()

    def test_empty_cheapest(self):
        html = generate_html(self._make_stats(), {}, {})
        assert "No market data" in html


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
