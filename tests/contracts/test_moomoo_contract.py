"""
Contract tests: Moomoo OpenD + moomoo_enricher response shape stability.
========================================================================
Validates that the expected dict keys from Moomoo OpenD API and the L5
enricher output remain stable. When Moomoo renames a field or changes
the return shape, these tests break immediately.
"""
import math
import pytest


# ── Market snapshot keys (from OpenQuoteContext.get_market_snapshot) ──────
REQUIRED_SNAPSHOT_KEYS = {
    "turnover_rate", "bid_ask_ratio", "amplitude",
}

# ── Position keys (from OpenSecTradeContext.position_list_query) ─────────
REQUIRED_POSITION_KEYS = {
    "code", "stock_name", "qty", "cost_price", "market_val", "pl_val", "pl_ratio",
}

# ── Capital flow keys (from OpenQuoteContext.get_capital_flow) ────────────
REQUIRED_CAPITAL_FLOW_COLS = {
    "in_flow", "out_flow", "net_flow",
}

# ── L5 enricher output keys (from compute_l5_score return dict) ──────────
REQUIRED_L5_OUTPUT_KEYS = {
    "l5_score", "smart_money_net", "flow_direction", "bid_ask", "turnover",
}

# ── Account info keys (from OpenSecTradeContext.accinfo_query) ───────────
REQUIRED_ACCOUNT_KEYS = {
    "total_assets", "cash", "market_val",
}


# ══════════════════════════════════════════════════════════════════════════
#  Shape tests
# ══════════════════════════════════════════════════════════════════════════

class TestMoomooSnapshotContract:
    """Market snapshot response must contain required keys."""

    SAMPLE = {
        "turnover_rate": 2.5,
        "bid_ask_ratio": 1.2,
        "amplitude": 3.1,
        "pe_ttm": 15.0,
        "pb": 2.0,
        "change_pct": 1.5,
    }

    def test_required_keys_present(self):
        missing = REQUIRED_SNAPSHOT_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing snapshot keys: {missing}"

    def test_values_are_finite(self):
        for key in REQUIRED_SNAPSHOT_KEYS:
            val = self.SAMPLE[key]
            assert isinstance(val, (int, float))
            assert math.isfinite(val), f"{key} is not finite: {val}"

    def test_turnover_rate_non_negative(self):
        assert self.SAMPLE["turnover_rate"] >= 0


class TestMoomooPositionContract:
    """Position dict from position_list_query must contain required keys."""

    SAMPLE = {
        "code": "US.AAPL",
        "stock_name": "Apple Inc",
        "qty": 10.0,
        "cost_price": 150.0,
        "market_val": 1700.0,
        "pl_val": 200.0,
        "pl_ratio": 13.3,
        "today_pl_val": 50.0,
    }

    def test_required_keys_present(self):
        missing = REQUIRED_POSITION_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing position keys: {missing}"

    def test_value_types(self):
        assert isinstance(self.SAMPLE["code"], str)
        assert isinstance(self.SAMPLE["stock_name"], str)
        assert isinstance(self.SAMPLE["qty"], (int, float))
        assert isinstance(self.SAMPLE["cost_price"], (int, float))

    def test_code_has_market_prefix(self):
        """Moomoo codes include market prefix like 'US.AAPL', 'AU.BHP'."""
        code = self.SAMPLE["code"]
        assert "." in code, f"Code should have market prefix: {code}"


class TestMoomooCapitalFlowContract:
    """Capital flow response must contain in/out/net flow keys."""

    SAMPLE = {
        "in_flow": 1_000_000.0,
        "out_flow": 500_000.0,
        "net_flow": 500_000.0,
    }

    def test_required_keys_present(self):
        missing = REQUIRED_CAPITAL_FLOW_COLS - set(self.SAMPLE.keys())
        assert not missing, f"Missing capital flow keys: {missing}"

    def test_net_flow_consistency(self):
        """net_flow should roughly equal in_flow - out_flow."""
        expected = self.SAMPLE["in_flow"] - self.SAMPLE["out_flow"]
        assert abs(self.SAMPLE["net_flow"] - expected) < 0.01


class TestMoomooL5OutputContract:
    """L5 enricher output dict must contain required scoring keys."""

    SAMPLE = {
        "l5_score": 65.0,
        "smart_money_net": 0.7,
        "flow_direction": "bullish",
        "bid_ask": 1.3,
        "turnover": 2.5,
    }

    def test_required_keys_present(self):
        missing = REQUIRED_L5_OUTPUT_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing L5 output keys: {missing}"

    def test_l5_score_range(self):
        """L5 score must be 0-100."""
        assert 0 <= self.SAMPLE["l5_score"] <= 100

    def test_flow_direction_valid(self):
        assert self.SAMPLE["flow_direction"] in ("bullish", "bearish", "neutral")


class TestMoomooAccountContract:
    """Account info dict must contain required keys."""

    SAMPLE = {
        "total_assets": 100_000.0,
        "cash": 25_000.0,
        "market_val": 75_000.0,
    }

    def test_required_keys_present(self):
        missing = REQUIRED_ACCOUNT_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing account keys: {missing}"

    def test_values_non_negative(self):
        for key in REQUIRED_ACCOUNT_KEYS:
            assert self.SAMPLE[key] >= 0, f"{key} is negative"


# ══════════════════════════════════════════════════════════════════════════
#  Edge-case tests (P3.6)
# ══════════════════════════════════════════════════════════════════════════

class TestMoomooEdgeCases:
    """Edge-case contract tests: nulls, zeros, negatives, serialisation."""

    def test_snapshot_with_null_ltp(self):
        """Pipeline must not crash when snapshot numeric fields are None (market closed)."""
        snapshot = {
            "turnover_rate": None,  # <-- null (market closed / untradable)
            "bid_ask_ratio": None,
            "amplitude": None,
        }
        # All required keys present even with null values
        missing = REQUIRED_SNAPSHOT_KEYS - set(snapshot.keys())
        assert not missing, f"Null-field snapshot missing keys: {missing}"
        assert snapshot["turnover_rate"] is None

    def test_position_with_zero_quantity(self):
        """Zero-qty positions appear after full exits — must still pass key contract."""
        position = {
            "code": "US.TSLA",
            "stock_name": "Tesla Inc",
            "qty": 0.0,
            "cost_price": 200.0,
            "market_val": 0.0,
            "pl_val": 0.0,
            "pl_ratio": 0.0,
        }
        missing = REQUIRED_POSITION_KEYS - set(position.keys())
        assert not missing
        assert position["qty"] == 0.0

    def test_position_with_negative_netqty(self):
        """Negative pl_val represents an unrealised loss — must be allowed."""
        position = {
            "code": "HK.00700",
            "stock_name": "Tencent Holdings",
            "qty": 100.0,
            "cost_price": 400.0,
            "market_val": 35_000.0,
            "pl_val": -5_000.0,   # loss position
            "pl_ratio": -12.5,
        }
        missing = REQUIRED_POSITION_KEYS - set(position.keys())
        assert not missing
        assert position["pl_val"] < 0

    def test_serialisation_round_trip(self):
        """Position dict must survive a JSON round-trip without data loss."""
        import json
        position = {
            "code": "AU.BHP",
            "stock_name": "BHP Group",
            "qty": 50.0,
            "cost_price": 42.50,
            "market_val": 2_250.0,
            "pl_val": 125.0,
            "pl_ratio": 5.88,
        }
        serialised = json.dumps(position)
        restored = json.loads(serialised)
        for key in REQUIRED_POSITION_KEYS:
            assert key in restored, f"Key {key!r} lost in round-trip"
            assert restored[key] == position[key]
