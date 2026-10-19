"""
Unit tests for TradabilityFilter.

Tests cover:
- Known tradable exchange:    NSE:TITAN   → tradable=True
- Known non-tradable:         TYO:7203    → tradable=False
- Forex exception:            FX:EURUSD   → tradable_with_limitations=True, api_execution=False
- Crypto exception:           BINANCE:BTCUSDT → tradable_with_limitations=True, api_execution=False
- Bare ticker (no prefix):    AAPL        → tradable=True (US assumption)
- filter_results() batch:     mixed list  → correct split
- is_forex_or_crypto():        FX/BINANCE  → True, NSE → False
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Adjust sys.path so we can import tradability_filter directly ──────────────
_TV_DIR = Path(__file__).parent.parent.parent   # tradingview/
sys.path.insert(0, str(_TV_DIR))

from tradability_filter import TradabilityFilter, TradabilityResult, MOOMOO_SG_FX_CRYPTO_API_EXPOSED  # noqa: E402


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tf() -> TradabilityFilter:
    """TradabilityFilter with buy_advisor importable from the real workspace."""
    return TradabilityFilter()


# ─── Basic tradable exchanges ─────────────────────────────────────────────────

class TestTradableExchanges:

    def test_nse_titan_is_tradable(self, tf: TradabilityFilter):
        r = tf.check("NSE:TITAN")
        assert r.tradable is True
        assert r.api_execution_available is True
        assert r.tradable_with_limitations is False
        assert r.reason == ""
        assert "Zerodha (INR)" in r.brokers or len(r.brokers) > 0

    def test_bse_is_tradable(self, tf: TradabilityFilter):
        r = tf.check("BSE:500325")
        assert r.tradable is True

    def test_nyse_is_tradable(self, tf: TradabilityFilter):
        r = tf.check("NYSE:AAPL")
        assert r.tradable is True
        assert "IBKR (USD)" in r.brokers

    def test_nasdaq_is_tradable(self, tf: TradabilityFilter):
        r = tf.check("NASDAQ:NVDA")
        assert r.tradable is True

    def test_asx_is_tradable(self, tf: TradabilityFilter):
        r = tf.check("ASX:CBA")
        assert r.tradable is True
        assert "Moomoo AU (AUD)" in r.brokers

    def test_hkex_is_tradable(self, tf: TradabilityFilter):
        r = tf.check("HKEX:700")
        assert r.tradable is True

    def test_sgx_is_tradable(self, tf: TradabilityFilter):
        r = tf.check("SGX:D05")
        assert r.tradable is True
        assert "Moomoo SG (SGD)" in r.brokers

    def test_tyo_is_tradable(self, tf: TradabilityFilter):
        # TYO (Tokyo) was added to _EXCHANGE_ACCOUNTS — tradable via Moomoo SG
        r = tf.check("TYO:7203")
        assert r.tradable is True
        assert r.api_execution_available is True
        assert r.brokers == ["Moomoo SG (SGD)"]


# ─── Non-tradable exchanges ───────────────────────────────────────────────────

class TestNonTradableExchanges:

    def test_lse_not_tradable(self, tf: TradabilityFilter):
        r = tf.check("LSE:HSBA")
        assert r.tradable is False
        assert "LSE" in r.reason

    def test_xetr_not_tradable(self, tf: TradabilityFilter):
        r = tf.check("XETR:SAP")
        assert r.tradable is False

    def test_unknown_exchange_not_tradable(self, tf: TradabilityFilter):
        r = tf.check("UNKNOWNEXCH:FOOBAR")
        assert r.tradable is False
        assert r.tradable_with_limitations is False


# ─── Forex exceptions ─────────────────────────────────────────────────────────

class TestForexExceptions:

    @pytest.mark.parametrize("sym", [
        "FX:EURUSD",
        "FX_IDC:GBPUSD",
        "OANDA:USDJPY",
        "FXCM:AUDUSD",
        "FOREXCOM:EURCAD",
    ])
    def test_forex_tradable_with_limitations(self, tf: TradabilityFilter, sym: str):
        r = tf.check(sym)
        assert r.tradable is False
        assert r.tradable_with_limitations is True
        assert r.api_execution_available is MOOMOO_SG_FX_CRYPTO_API_EXPOSED  # False by default
        assert "Moomoo SG (SGD)" in r.brokers
        assert "API not exposed" in r.reason

    def test_forex_api_exposed_config(self, tf: TradabilityFilter):
        """If MOOMOO_SG_FX_CRYPTO_API_EXPOSED is set to True, api_execution becomes True."""
        import tradability_filter as _tf_module
        original = _tf_module.MOOMOO_SG_FX_CRYPTO_API_EXPOSED
        _tf_module.MOOMOO_SG_FX_CRYPTO_API_EXPOSED = True
        try:
            r = tf.check("FX:EURUSD")
            assert r.api_execution_available is True
        finally:
            _tf_module.MOOMOO_SG_FX_CRYPTO_API_EXPOSED = original


# ─── Crypto exceptions ────────────────────────────────────────────────────────

class TestCryptoExceptions:

    @pytest.mark.parametrize("sym", [
        "BINANCE:BTCUSDT",
        "COINBASE:ETHUSD",
        "KRAKEN:XBTUSD",
        "BITSTAMP:BTCUSD",
    ])
    def test_crypto_tradable_with_limitations(self, tf: TradabilityFilter, sym: str):
        r = tf.check(sym)
        assert r.tradable is False
        assert r.tradable_with_limitations is True
        assert r.api_execution_available is MOOMOO_SG_FX_CRYPTO_API_EXPOSED
        assert "Moomoo SG (SGD)" in r.brokers


# ─── Bare ticker (no exchange prefix) ────────────────────────────────────────

class TestBareTicker:

    def test_bare_ticker_assumed_us_stock(self, tf: TradabilityFilter):
        r = tf.check("AAPL")
        assert r.tradable is True
        assert r.api_execution_available is True
        assert r.exchange == ""
        assert r.ticker == "AAPL"

    def test_bare_ticker_lowercase(self, tf: TradabilityFilter):
        r = tf.check("nvda")
        assert r.tradable is True

    def test_empty_symbol_returns_safe_result(self, tf: TradabilityFilter):
        r = tf.check("")
        assert r.tradable is False
        assert "Empty" in r.reason


# ─── filter_results() batch processing ───────────────────────────────────────

class TestFilterResults:

    def _make_result(self, tv_symbol: str, source: str = "watchlist") -> dict:
        return {
            "tv_symbol":    tv_symbol,
            "_source":      source,
            "composite":    60.0,
            "scores":       {"l1": {"total": 20}, "l2": {"total": 20},
                             "l3": {"total": 10}, "l4": {"total": 10}},
        }

    def test_tradable_symbols_kept(self, tf: TradabilityFilter):
        results = [
            self._make_result("NSE:TITAN"),
            self._make_result("NYSE:AAPL"),
        ]
        kept, skipped = tf.filter_results(results)
        assert len(kept)    == 2
        assert len(skipped) == 0

    def test_non_tradable_symbols_skipped(self, tf: TradabilityFilter):
        results = [
            self._make_result("XETR:SAP"),
            self._make_result("LSE:HSBA"),
        ]
        kept, skipped = tf.filter_results(results)
        assert len(kept)    == 0
        assert len(skipped) == 2
        assert all("symbol" in s for s in skipped)
        assert all("reason" in s for s in skipped)

    def test_forex_kept_with_limitation_flag(self, tf: TradabilityFilter):
        results = [self._make_result("FX:EURUSD")]
        kept, skipped = tf.filter_results(results)
        assert len(kept)    == 1
        assert len(skipped) == 0
        assert kept[0].get("_tradable_with_limitations") is True

    def test_mixed_results_split_correctly(self, tf: TradabilityFilter):
        results = [
            self._make_result("NSE:TITAN"),      # tradable
            self._make_result("XETR:SAP"),        # not tradable
            self._make_result("BINANCE:BTCUSDT"), # limitations
        ]
        kept, skipped = tf.filter_results(results)
        assert len(kept)    == 2   # NSE + BINANCE
        assert len(skipped) == 1   # XETR
        assert skipped[0]["symbol"] == "XETR:SAP"

    def test_skipped_carries_source(self, tf: TradabilityFilter):
        results = [self._make_result("XETR:SAP", source="screener")]
        _, skipped = tf.filter_results(results)
        assert skipped[0]["source"] == "screener"


# ─── is_forex_or_crypto() ─────────────────────────────────────────────────────

class TestIsForexOrCrypto:

    def test_fx_is_forex(self, tf: TradabilityFilter):
        assert tf.is_forex_or_crypto("FX:EURUSD") is True

    def test_binance_is_crypto(self, tf: TradabilityFilter):
        assert tf.is_forex_or_crypto("BINANCE:BTCUSDT") is True

    def test_nse_is_not_forex_crypto(self, tf: TradabilityFilter):
        assert tf.is_forex_or_crypto("NSE:TITAN") is False

    def test_bare_ticker_not_forex_crypto(self, tf: TradabilityFilter):
        assert tf.is_forex_or_crypto("AAPL") is False


# ─── TradabilityResult.as_dict() ─────────────────────────────────────────────

class TestResultAsDict:

    def test_as_dict_keys(self, tf: TradabilityFilter):
        r = tf.check("NSE:TITAN")
        d = r.as_dict()
        for key in ("tradable", "tradable_with_limitations", "api_execution_available",
                    "reason", "brokers", "exchange", "ticker"):
            assert key in d

    def test_as_dict_values_match_fields(self, tf: TradabilityFilter):
        r = tf.check("FX:EURUSD")
        d = r.as_dict()
        assert d["tradable"]                  == r.tradable
        assert d["tradable_with_limitations"] == r.tradable_with_limitations
        assert d["api_execution_available"]   == r.api_execution_available


# ─── Fallback behaviour when buy_advisor is unavailable ──────────────────────

class TestFallbackBehaviour:

    def test_fallback_when_buy_advisor_missing(self):
        """Simulate buy_advisor not installed — filter must use hardcoded fallback."""
        with patch.dict("sys.modules", {"buy_advisor": None}):
            tf2 = TradabilityFilter()
            # NSE should still be tradable via fallback
            r = tf2.check("NSE:TITAN")
            assert r.tradable is True

        # Also test non-tradable still works
        with patch.dict("sys.modules", {"buy_advisor": None}):
            tf3 = TradabilityFilter()
            r = tf3.check("TYO:7203")
            assert r.tradable is False
