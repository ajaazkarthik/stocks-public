"""Tests for F3: Currency Exposure Report."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingview.analysis.currency_exposure import (
    load_positions,
    infer_currency,
    build_exposure,
    compute_percentages,
    generate_html,
    CurrencyBucket,
    BROKER_DEFAULT_CURRENCY,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. infer_currency
# ═══════════════════════════════════════════════════════════════════════════════

class TestInferCurrency:
    def test_explicit_currency(self):
        assert infer_currency({"currency": "USD"}) == "USD"

    def test_lowercase_normalized(self):
        assert infer_currency({"currency": "usd"}) == "USD"

    def test_from_broker_moomoo_au(self):
        assert infer_currency({"broker": "Moomoo-AU"}) == "AUD"

    def test_from_broker_zerodha(self):
        assert infer_currency({"broker": "Zerodha"}) == "INR"

    def test_from_broker_moomoo_sg(self):
        assert infer_currency({"broker": "Moomoo-SG"}) == "SGD"

    def test_unknown_broker_defaults_usd(self):
        assert infer_currency({"broker": "UnknownBroker"}) == "USD"

    def test_empty_currency_falls_to_broker(self):
        assert infer_currency({"currency": "", "broker": "AngelOne"}) == "INR"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. build_exposure
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildExposure:
    def test_single_currency(self):
        pos = [
            {"symbol": "BHP", "currency": "AUD", "broker": "Moomoo-AU",
             "market_val": 10000, "pnl": 500},
        ]
        buckets = build_exposure(pos)
        assert len(buckets) == 1
        assert buckets[0].currency == "AUD"
        assert buckets[0].value_aud == 10000  # AUD rate = 1.0

    def test_multiple_currencies(self):
        pos = [
            {"symbol": "BHP", "currency": "AUD", "broker": "Moomoo-AU",
             "market_val": 10000, "pnl": 500},
            {"symbol": "NVDA", "currency": "USD", "broker": "IBKR",
             "market_val": 10000, "pnl": 1000},
        ]
        buckets = build_exposure(pos)
        assert len(buckets) == 2
        # USD should be first (10000 * 1.55 = 15500 > AUD 10000)
        assert buckets[0].currency == "USD"

    def test_custom_fx_rates(self):
        pos = [{"symbol": "X", "currency": "USD", "market_val": 1000, "pnl": 100, "broker": "IBKR"}]
        buckets = build_exposure(pos, fx_rates={"USD": 2.0})
        assert buckets[0].value_aud == 2000

    def test_empty_positions(self):
        assert build_exposure([]) == []

    def test_aggregation_same_currency(self):
        pos = [
            {"currency": "USD", "market_val": 5000, "pnl": 100, "broker": "IBKR"},
            {"currency": "USD", "market_val": 3000, "pnl": -50, "broker": "Moomoo-AU"},
        ]
        buckets = build_exposure(pos)
        assert len(buckets) == 1
        assert buckets[0].value_native == 8000
        assert buckets[0].position_count == 2
        assert buckets[0].brokers == {"IBKR", "Moomoo-AU"}

    def test_sorted_by_value_aud(self):
        pos = [
            {"currency": "INR", "market_val": 1000000, "pnl": 0, "broker": "Zerodha"},  # 1M * 0.019 = 19K
            {"currency": "USD", "market_val": 10000, "pnl": 0, "broker": "IBKR"},       # 10K * 1.55 = 15.5K
            {"currency": "AUD", "market_val": 20000, "pnl": 0, "broker": "Moomoo-AU"},  # 20K * 1.0 = 20K
        ]
        buckets = build_exposure(pos)
        assert [b.currency for b in buckets] == ["AUD", "INR", "USD"]


# ═══════════════════════════════════════════════════════════════════════════════
# 3. compute_percentages
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputePercentages:
    def test_basic(self):
        b1 = CurrencyBucket(currency="USD", value_aud=7000)
        b2 = CurrencyBucket(currency="AUD", value_aud=3000)
        result = compute_percentages([b1, b2])
        assert len(result) == 2
        assert abs(result[0][1] - 70.0) < 0.1
        assert abs(result[1][1] - 30.0) < 0.1

    def test_empty(self):
        assert compute_percentages([]) == []

    def test_zero_total(self):
        b = CurrencyBucket(currency="X", value_aud=0)
        result = compute_percentages([b])
        assert result[0][1] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_contains_currencies(self):
        b = CurrencyBucket(currency="USD", value_aud=15500, pnl_aud=500,
                           position_count=5, brokers={"IBKR"})
        html = generate_html([b])
        assert "USD" in html
        assert "IBKR" in html

    def test_empty(self):
        html = generate_html([])
        assert "No positions" in html

    def test_written(self, tmp_path):
        out = tmp_path / "currency.html"
        generate_html([], out)
        assert out.exists()

    def test_percentage_shown(self):
        b = CurrencyBucket(currency="AUD", value_aud=10000, pnl_aud=0,
                           position_count=3, brokers={"Moomoo-AU"})
        html = generate_html([b])
        assert "100.0%" in html


# ═══════════════════════════════════════════════════════════════════════════════
# 5. load_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPositions:
    def test_valid(self, tmp_path):
        f = tmp_path / "pos.json"
        f.write_text('[{"symbol":"X","currency":"AUD"}]', encoding="utf-8")
        assert len(load_positions(f)) == 1

    def test_missing(self, tmp_path):
        assert load_positions(tmp_path / "nope.json") == []

    def test_invalid(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("broken", encoding="utf-8")
        assert load_positions(f) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
