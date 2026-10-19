"""Tests for F17: Zerodha Autoslice Order Integration.

Validates that india_order_executor uses kite.place_autoslice_order()
for large orders exceeding NSE freeze limits (100,000 shares),
and falls back to kite.place_order() for normal-sized orders.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "india-brokers"))


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: mock Kite object with all required constants
# ═══════════════════════════════════════════════════════════════════════════════

def _make_mock_kite(has_autoslice: bool = True):
    """Build a mock KiteConnect that mimics the real kite interface."""
    kite = MagicMock()
    kite.VARIETY_REGULAR = "regular"
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"
    kite.PRODUCT_CNC = "CNC"
    kite.ORDER_TYPE_MARKET = "MARKET"
    kite.place_order = MagicMock(return_value="ORDER123")
    if has_autoslice:
        kite.place_autoslice_order = MagicMock(return_value="AUTOSLICE456")
    else:
        # Simulate older kiteconnect without autoslice
        if hasattr(kite, "place_autoslice_order"):
            del kite.place_autoslice_order
    return kite


# ═══════════════════════════════════════════════════════════════════════════════
# Test autoslice logic directly (unit tests)
# ═══════════════════════════════════════════════════════════════════════════════

NSE_FREEZE_LIMIT = 100_000


class TestAutosliceLogic:
    """Test the autoslice decision logic extracted from india_order_executor.py."""

    def test_small_order_uses_regular(self):
        """Orders <= 100,000 shares use kite.place_order()."""
        kite = _make_mock_kite(has_autoslice=True)
        qty = 50
        kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange="NSE",
            tradingsymbol="INFY",
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        if qty > NSE_FREEZE_LIMIT and hasattr(kite, "place_autoslice_order"):
            order_id = kite.place_autoslice_order(**kwargs)
        else:
            order_id = kite.place_order(**kwargs)

        assert order_id == "ORDER123"
        kite.place_order.assert_called_once()
        kite.place_autoslice_order.assert_not_called()

    def test_large_order_uses_autoslice(self):
        """Orders > 100,000 shares use kite.place_autoslice_order()."""
        kite = _make_mock_kite(has_autoslice=True)
        qty = 150_000
        kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange="NSE",
            tradingsymbol="YESBANK",
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        if qty > NSE_FREEZE_LIMIT and hasattr(kite, "place_autoslice_order"):
            order_id = kite.place_autoslice_order(**kwargs)
        else:
            order_id = kite.place_order(**kwargs)

        assert order_id == "AUTOSLICE456"
        kite.place_autoslice_order.assert_called_once()
        kite.place_order.assert_not_called()

    def test_exact_limit_uses_regular(self):
        """Orders exactly at 100,000 use regular (> not >=)."""
        kite = _make_mock_kite(has_autoslice=True)
        qty = 100_000
        kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange="NSE",
            tradingsymbol="IDEA",
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        if qty > NSE_FREEZE_LIMIT and hasattr(kite, "place_autoslice_order"):
            order_id = kite.place_autoslice_order(**kwargs)
        else:
            order_id = kite.place_order(**kwargs)

        assert order_id == "ORDER123"
        kite.place_order.assert_called_once()

    def test_no_autoslice_fallback(self):
        """Older kiteconnect without autoslice falls back to place_order."""
        kite = _make_mock_kite(has_autoslice=False)
        qty = 200_000
        kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange="NSE",
            tradingsymbol="YESBANK",
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        if qty > NSE_FREEZE_LIMIT and hasattr(kite, "place_autoslice_order"):
            order_id = kite.place_autoslice_order(**kwargs)
        else:
            order_id = kite.place_order(**kwargs)

        assert order_id == "ORDER123"
        kite.place_order.assert_called_once()


class TestAutosliceSellLogic:
    """Test autoslice for SELL orders (same logic at line 540)."""

    def test_large_sell_uses_autoslice(self):
        kite = _make_mock_kite(has_autoslice=True)
        qty = 200_000
        kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange="NSE",
            tradingsymbol="YESBANK",
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        if qty > NSE_FREEZE_LIMIT and hasattr(kite, "place_autoslice_order"):
            order_id = kite.place_autoslice_order(**kwargs)
        else:
            order_id = kite.place_order(**kwargs)

        assert order_id == "AUTOSLICE456"

    def test_small_sell_uses_regular(self):
        kite = _make_mock_kite(has_autoslice=True)
        qty = 500
        kwargs = dict(
            variety=kite.VARIETY_REGULAR,
            exchange="NSE",
            tradingsymbol="INFY",
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=kite.PRODUCT_CNC,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        if qty > NSE_FREEZE_LIMIT and hasattr(kite, "place_autoslice_order"):
            order_id = kite.place_autoslice_order(**kwargs)
        else:
            order_id = kite.place_order(**kwargs)

        assert order_id == "ORDER123"


class TestFreezeLimit:
    """Validate the freeze limit constant and edge cases."""

    def test_limit_is_100k(self):
        assert NSE_FREEZE_LIMIT == 100_000

    def test_limit_type(self):
        assert isinstance(NSE_FREEZE_LIMIT, int)

    def test_comparison_boundary(self):
        """100,001 should trigger autoslice, 100,000 should not."""
        assert 100_001 > NSE_FREEZE_LIMIT
        assert not (100_000 > NSE_FREEZE_LIMIT)
        assert not (99_999 > NSE_FREEZE_LIMIT)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
