"""Tests for broker_schemas — Pydantic v2 position validation."""
import math
import pytest
from broker_schemas import BrokerPosition, validate_positions


# ── Valid position fixture ────────────────────────────────────────────────────

def _valid_pos(**overrides) -> dict:
    base = {
        "symbol": "AAPL",
        "shares": 100,
        "avg_cost": 150.0,
        "current_price": 155.0,
        "market_val": 15500.0,
        "pnl": 500.0,
        "pnl_pct": 3.3,
        "currency": "USD",
        "broker": "Moomoo-AU",
        "exchange": "US",
    }
    base.update(overrides)
    return base


# ── BrokerPosition model tests ───────────────────────────────────────────────

class TestBrokerPosition:
    def test_valid_position(self):
        pos = BrokerPosition(**_valid_pos())
        assert pos.symbol == "AAPL"
        assert pos.shares == 100
        assert pos.currency == "USD"

    def test_with_option_flag(self):
        pos = BrokerPosition(**_valid_pos(is_option=True))
        assert pos.is_option is True

    def test_default_currency(self):
        d = _valid_pos()
        del d["currency"]
        pos = BrokerPosition(**d)
        assert pos.currency == "USD"

    def test_empty_symbol_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(symbol=""))

    def test_whitespace_symbol_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(symbol="   "))

    def test_zero_shares_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(shares=0))

    def test_negative_shares_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(shares=-10))

    def test_nan_avg_cost_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(avg_cost=float("nan")))

    def test_inf_market_val_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(market_val=float("inf")))

    def test_negative_inf_price_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(current_price=float("-inf")))

    def test_empty_currency_rejected(self):
        with pytest.raises(Exception):
            BrokerPosition(**_valid_pos(currency=""))

    def test_float_shares_accepted(self):
        """Fractional shares (e.g. crypto, Webull) should be accepted."""
        pos = BrokerPosition(**_valid_pos(shares=0.5))
        assert pos.shares == 0.5

    def test_zero_pnl_accepted(self):
        pos = BrokerPosition(**_valid_pos(pnl=0.0, pnl_pct=0.0))
        assert pos.pnl == 0.0

    def test_negative_pnl_accepted(self):
        pos = BrokerPosition(**_valid_pos(pnl=-500.0, pnl_pct=-3.3))
        assert pos.pnl == -500.0

    def test_zero_price_accepted(self):
        """Delisted stocks may have 0 price."""
        pos = BrokerPosition(**_valid_pos(current_price=0.0))
        assert pos.current_price == 0.0


# ── validate_positions tests ─────────────────────────────────────────────────

class TestValidatePositions:
    def test_all_valid(self):
        raw = [_valid_pos(symbol="AAPL"), _valid_pos(symbol="GOOG")]
        result = validate_positions(raw, broker="test")
        assert len(result) == 2

    def test_drops_invalid(self):
        raw = [
            _valid_pos(symbol="AAPL"),
            _valid_pos(symbol=""),  # invalid
            _valid_pos(symbol="GOOG"),
        ]
        result = validate_positions(raw, broker="test")
        assert len(result) == 2
        assert result[0]["symbol"] == "AAPL"
        assert result[1]["symbol"] == "GOOG"

    def test_empty_list(self):
        assert validate_positions([], broker="test") == []

    def test_all_invalid(self):
        raw = [
            _valid_pos(shares=0),
            _valid_pos(avg_cost=float("nan")),
        ]
        result = validate_positions(raw, broker="test")
        assert len(result) == 0

    def test_preserves_extra_fields(self):
        """Extra dict keys not in schema should pass through."""
        raw = [_valid_pos(custom_field="hello")]
        result = validate_positions(raw, broker="test")
        assert len(result) == 1
        assert result[0]["custom_field"] == "hello"

    def test_logger_called_on_invalid(self):
        """When logger is provided, it should be called for invalid positions."""

        class MockLogger:
            def __init__(self):
                self.warnings = []
            def warning(self, msg, **kw):
                self.warnings.append((msg, kw))

        ml = MockLogger()
        raw = [_valid_pos(symbol="")]
        validate_positions(raw, broker="test", logger=ml)
        assert len(ml.warnings) == 1
        assert ml.warnings[0][0] == "position_validation_failed"

    def test_realistic_moomoo_position(self):
        """Test with a realistic Moomoo position dict."""
        pos = {
            "symbol": "ASX:BHP",
            "shares": 200,
            "avg_cost": 45.50,
            "current_price": 48.20,
            "market_val": 9640.0,
            "pnl": 540.0,
            "pnl_pct": 5.9,
            "currency": "AUD",
            "broker": "Moomoo-AU",
            "exchange": "AU",
        }
        result = validate_positions([pos], broker="Moomoo-AU")
        assert len(result) == 1

    def test_realistic_ibkr_position(self):
        """Test with a realistic IBKR position dict."""
        pos = {
            "symbol": "NVDA",
            "shares": 50,
            "avg_cost": 120.0,
            "current_price": 135.50,
            "market_val": 6775.0,
            "pnl": 775.0,
            "pnl_pct": 12.9,
            "currency": "USD",
            "broker": "IBKR",
            "exchange": "US",
        }
        result = validate_positions([pos], broker="IBKR")
        assert len(result) == 1

    def test_realistic_india_position(self):
        """Test with a realistic Zerodha position dict."""
        pos = {
            "symbol": "NSE:RELIANCE",
            "shares": 10,
            "avg_cost": 2500.0,
            "current_price": 2650.0,
            "market_val": 26500.0,
            "pnl": 1500.0,
            "pnl_pct": 6.0,
            "currency": "INR",
            "broker": "Zerodha",
            "exchange": "NSE",
        }
        result = validate_positions([pos], broker="Zerodha")
        assert len(result) == 1
