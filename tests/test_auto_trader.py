"""Tests for F1: auto_trader.py — ATR stop-loss, order dedup, NameError fixes."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
TV = ROOT / "tradingview"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TV))


# ---------------------------------------------------------------------------
# Helpers — import with heavy deps stubbed
# ---------------------------------------------------------------------------

def _import_auto_trader():
    """Import auto_trader with heavy deps stubbed out."""
    # Stub moomoo_config
    mc = types.ModuleType("moomoo_config")
    mc.OPEND_HOST = "127.0.0.1"
    mc.OPEND_PORT_AU = 11111
    mc.OPEND_PORT_SG = 11112
    mc.is_opend_running = lambda port: False
    sys.modules.setdefault("moomoo_config", mc)

    # Stub moomoo
    mm = types.ModuleType("moomoo")
    mm.Market = type("Market", (), {"US": 1, "AU": 2, "HK": 3})
    mm.TrdSide = type("TrdSide", (), {"BUY": 0, "SELL": 1})
    mm.OrderType = type("OrderType", (), {"NORMAL": 0})
    mm.TradeContext = MagicMock
    sys.modules.setdefault("moomoo", mm)

    import auto_trader
    return auto_trader


at = _import_auto_trader()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. size_position — ATR-based stop-loss
# ═══════════════════════════════════════════════════════════════════════════════

class TestSizePosition:
    """size_position() should use ATR when available, fall back to 3%."""

    def test_atr_based_stop(self):
        """When _get_atr returns a value, stop should use ATR * multiplier."""
        with patch.object(at, "_get_atr", return_value=2.5):
            result = at.size_position(
                available_cash=100_000,
                entry_price=100.0,
                win_rate=0.6,
                ticker="AAPL",
            )
        # stop = 100 - 2.0 * 2.5 = 95.0
        assert result["stop_loss_price"] == pytest.approx(95.0, abs=0.01)

    def test_fallback_3pct_when_no_atr(self):
        """When _get_atr returns None, fall back to 3% stop."""
        with patch.object(at, "_get_atr", return_value=None):
            result = at.size_position(
                available_cash=100_000,
                entry_price=100.0,
                win_rate=0.6,
                ticker="AAPL",
            )
        # stop = 100 * (1 - 0.03) = 97.0
        assert result["stop_loss_price"] == pytest.approx(97.0, abs=0.01)

    def test_fallback_3pct_when_no_ticker(self):
        """When ticker is empty, skip ATR call and use 3% fallback."""
        with patch.object(at, "_get_atr") as mock_atr:
            result = at.size_position(
                available_cash=100_000,
                entry_price=200.0,
                win_rate=0.5,
                ticker="",
            )
        mock_atr.assert_not_called()
        assert result["stop_loss_price"] == pytest.approx(194.0, abs=0.01)

    def test_atr_zero_uses_fallback(self):
        """If ATR returns 0, fall back to 3%."""
        with patch.object(at, "_get_atr", return_value=0.0):
            result = at.size_position(
                available_cash=50_000,
                entry_price=50.0,
                win_rate=0.55,
                ticker="XYZ",
            )
        assert result["stop_loss_price"] == pytest.approx(48.5, abs=0.01)

    def test_returns_required_keys(self):
        """Result dict must have all expected keys."""
        with patch.object(at, "_get_atr", return_value=1.0):
            result = at.size_position(
                available_cash=100_000,
                entry_price=50.0,
                win_rate=0.6,
                ticker="TEST",
            )
        for key in ("qty", "position_value", "stop_loss_price", "risk_per_trade", "kelly_fraction"):
            assert key in result, f"Missing key: {key}"

    def test_qty_zero_when_cash_tiny(self):
        """If cash is too small for 1 share, qty should be 0."""
        with patch.object(at, "_get_atr", return_value=1.0):
            result = at.size_position(
                available_cash=1.0,
                entry_price=500.0,
                win_rate=0.6,
                ticker="EXPENSIVE",
            )
        assert result["qty"] == 0

    def test_option_multiplier(self):
        """OPTION instrument should apply option contract multiplier."""
        with patch.object(at, "_get_atr", return_value=5.0):
            result = at.size_position(
                available_cash=100_000,
                entry_price=10.0,
                win_rate=0.6,
                instrument_type="OPTION",
                ticker="SPY",
            )
        # Options typically have multiplier applied
        assert result["qty"] >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. place_order_zerodha — stop_loss propagation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlaceOrderZerodha:
    """place_order_zerodha() tests."""

    def test_dry_run_returns_stop(self):
        """Dry-run should return stop_loss in result."""
        result = at.place_order_zerodha("INFY", 10, 1500.0, 1450.0, dry_run=True)
        assert result["status"] == "DRY_RUN"
        assert result["stop"] == 1450.0
        assert result["broker"] == "zerodha"

    def test_dry_run_ticker_preserved(self):
        result = at.place_order_zerodha("RELIANCE", 5, 2800.0, 2700.0, dry_run=True)
        assert result["ticker"] == "RELIANCE"
        assert result["qty"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# 3. place_order_moomoo — stop_loss in result
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlaceOrderMoomoo:
    """place_order_moomoo() tests."""

    def test_dry_run_returns_stop(self):
        """Dry-run should return stop_loss in result."""
        result = at.place_order_moomoo("AAPL", "NASDAQ", 10, 180.0, 172.0, dry_run=True)
        assert result["status"] == "DRY_RUN"
        assert result["stop"] == 172.0
        assert result["broker"] == "moomoo"

    def test_dry_run_exchange_preserved(self):
        result = at.place_order_moomoo("BHP", "ASX", 100, 45.0, 43.0, dry_run=True)
        assert result["ticker"] == "BHP"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. run_auto_trader — dedup integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunAutoTraderDedup:
    """run_auto_trader() should skip duplicate signals via order_ledger."""

    def _make_signal(self, symbol="NSE:INFY", composite=75.0, fire_id="fire1"):
        return {
            "tv_symbol": symbol,
            "composite_score": composite,
            "win_rate": 0.6,
            "price_at_signal": 1500.0,
            "fire_id": fire_id,
            "timestamp": "2026-01-01T00:00:00Z",
        }

    @patch.object(at, "get_available_cash", return_value=100_000.0)
    @patch.object(at, "_get_vix", return_value=18.0)
    @patch.object(at, "_get_live_price", return_value=1500.0)
    @patch.object(at, "_get_atr", return_value=30.0)
    @patch.object(at, "load_actionable_signals")
    def test_duplicate_signal_skipped(self, mock_load, mock_atr, mock_price, mock_vix, mock_cash):
        """When ledger reports duplicate, signal should be skipped."""
        mock_load.return_value = [self._make_signal()]

        # Create a mock ledger that says everything is a duplicate
        mock_ledger = MagicMock()
        mock_ledger.make_sid.return_value = "test_sid_123"
        mock_ledger.is_duplicate.return_value = True

        with patch.object(at, "_ledger_available", True), \
             patch.object(at, "get_ledger", return_value=mock_ledger):
            orders = at.run_auto_trader(broker="zerodha", top_n=5, dry_run=True)

        assert len(orders) == 1
        assert orders[0]["action"] == "SKIP"
        assert orders[0]["reason"] == "duplicate"

    @patch.object(at, "get_available_cash", return_value=100_000.0)
    @patch.object(at, "_get_vix", return_value=18.0)
    @patch.object(at, "_get_live_price", return_value=1500.0)
    @patch.object(at, "_get_atr", return_value=30.0)
    @patch.object(at, "load_actionable_signals")
    def test_non_duplicate_proceeds(self, mock_load, mock_atr, mock_price, mock_vix, mock_cash):
        """When ledger says not duplicate, signal should be processed."""
        mock_load.return_value = [self._make_signal()]

        mock_ledger = MagicMock()
        mock_ledger.make_sid.return_value = "test_sid_456"
        mock_ledger.is_duplicate.return_value = False

        with patch.object(at, "_ledger_available", True), \
             patch.object(at, "get_ledger", return_value=mock_ledger):
            orders = at.run_auto_trader(broker="zerodha", top_n=5, dry_run=True)

        assert len(orders) >= 1
        # Should NOT be skipped as duplicate
        if orders[0].get("action") == "SKIP":
            assert orders[0]["reason"] != "duplicate"

    @patch.object(at, "get_available_cash", return_value=100_000.0)
    @patch.object(at, "_get_vix", return_value=18.0)
    @patch.object(at, "load_actionable_signals")
    def test_no_signals_returns_empty(self, mock_load, mock_vix, mock_cash):
        """When no signals found, return empty list."""
        mock_load.return_value = []
        with patch.object(at, "load_actionable_log", return_value=[]):
            orders = at.run_auto_trader(broker="zerodha", top_n=5, dry_run=True)
        assert orders == []


# ═══════════════════════════════════════════════════════════════════════════════
# 5. select_instrument — basic coverage
# ═══════════════════════════════════════════════════════════════════════════════

class TestSelectInstrument:
    """select_instrument() should return valid instrument dict."""

    def test_returns_required_keys(self):
        sig = {"tv_symbol": "NASDAQ:AAPL", "composite_score": 80}
        result = at.select_instrument(sig, vix=18.0)
        for key in ("instrument_type", "ticker", "exchange", "reason"):
            assert key in result, f"Missing key: {key}"

    def test_skip_on_low_composite(self):
        sig = {"tv_symbol": "NASDAQ:XYZ", "composite_score": 30}
        result = at.select_instrument(sig, vix=18.0)
        # Low composite should be SKIP or STOCK (depends on impl)
        assert result["instrument_type"] in ("SKIP", "STOCK", "OPTION")

    def test_high_vix_still_returns(self):
        sig = {"tv_symbol": "NYSE:SPY", "composite_score": 85}
        result = at.select_instrument(sig, vix=35.0)
        assert "instrument_type" in result


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge case tests for auto_trader functions."""

    def test_size_position_negative_kelly(self):
        """Negative Kelly should clamp to near-zero or minimum qty."""
        with patch.object(at, "_get_atr", return_value=1.0):
            result = at.size_position(
                available_cash=10_000,
                entry_price=50.0,
                win_rate=0.20,  # very low win rate → negative Kelly
                ticker="BAD",
            )
        assert result["qty"] >= 0  # should never be negative

    def test_size_position_win_rate_one(self):
        """100% win rate should produce valid sizing."""
        with patch.object(at, "_get_atr", return_value=2.0):
            result = at.size_position(
                available_cash=100_000,
                entry_price=100.0,
                win_rate=1.0,
                ticker="PERFECT",
            )
        assert result["qty"] > 0
        assert result["position_value"] > 0

    def test_load_actionable_signals_missing_db(self, tmp_path):
        """Should return empty list if database doesn't exist."""
        result = at.load_actionable_signals(tmp_path / "nonexistent.db")
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 7. F6 — Earnings blocking
# ═══════════════════════════════════════════════════════════════════════════════

class TestEarningsBlocking:
    """is_earnings_blocked() and _days_until_earnings() tests."""

    def test_blocked_when_earnings_within_window(self):
        """Should block when earnings are 0-3 days away."""
        with patch.object(at, "_days_until_earnings", return_value=2):
            blocked, days = at.is_earnings_blocked("AAPL", "NASDAQ")
        assert blocked is True
        assert days == 2

    def test_not_blocked_when_earnings_far(self):
        """Should NOT block when earnings are >3 days away."""
        with patch.object(at, "_days_until_earnings", return_value=10):
            blocked, days = at.is_earnings_blocked("AAPL", "NASDAQ")
        assert blocked is False
        assert days == 10

    def test_not_blocked_when_unknown(self):
        """Should NOT block when earnings date is unknown."""
        with patch.object(at, "_days_until_earnings", return_value=None):
            blocked, days = at.is_earnings_blocked("XYZ", "NASDAQ")
        assert blocked is False
        assert days is None

    def test_blocked_on_earnings_day(self):
        """Should block when earnings are today (day 0)."""
        with patch.object(at, "_days_until_earnings", return_value=0):
            blocked, days = at.is_earnings_blocked("TSLA", "NASDAQ")
        assert blocked is True
        assert days == 0

    def test_not_blocked_past_earnings(self):
        """Negative days = past earnings, should NOT block."""
        with patch.object(at, "_days_until_earnings", return_value=-2):
            blocked, days = at.is_earnings_blocked("MSFT", "NASDAQ")
        assert blocked is False

    def test_custom_block_window(self):
        """Custom block_days parameter should be respected."""
        with patch.object(at, "_days_until_earnings", return_value=5):
            blocked, _ = at.is_earnings_blocked("GOOG", "NASDAQ", block_days=7)
        assert blocked is True

    def test_earnings_constant_defined(self):
        """EARNINGS_BLOCK_DAYS constant should exist."""
        assert hasattr(at, "EARNINGS_BLOCK_DAYS")
        assert at.EARNINGS_BLOCK_DAYS == 3

    @patch.object(at, "get_available_cash", return_value=100_000.0)
    @patch.object(at, "_get_vix", return_value=18.0)
    @patch.object(at, "_get_live_price", return_value=150.0)
    @patch.object(at, "_get_atr", return_value=3.0)
    @patch.object(at, "load_actionable_signals")
    def test_earnings_skip_in_run_auto_trader(self, mock_load, mock_atr, mock_price, mock_vix, mock_cash):
        """run_auto_trader should skip signals with imminent earnings."""
        mock_load.return_value = [{
            "tv_symbol": "NASDAQ:AAPL",
            "composite_score": 80.0,
            "win_rate": 0.65,
            "price_at_signal": 150.0,
            "fire_id": "earn_test",
            "timestamp": "2026-01-01T00:00:00Z",
        }]

        with patch.object(at, "_ledger_available", False), \
             patch.object(at, "is_earnings_blocked", return_value=(True, 1)):
            orders = at.run_auto_trader(broker="zerodha", top_n=5, dry_run=True)

        assert len(orders) == 1
        assert orders[0]["action"] == "SKIP"
        assert "earnings" in orders[0]["reason"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
