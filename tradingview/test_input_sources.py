"""
Tests for input_sources.py
Run: pytest test_input_sources.py -v
All tests are offline — no network calls (resolve_exchange=False or mocked).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from input_sources import (
    TICKER_ALIASES,
    ValidationResult,
    SOURCE_MANUAL,
    SOURCE_SCREENER,
    SOURCE_WEBHOOK,
    SOURCE_DOWNLOAD,
    _make_fire,
    manual_to_fires,
    validate_and_normalise,
)

try:
    from input_sources import _parse_signal_from_message
    _HAS_PARSE_SIGNAL = True
except ImportError:
    _parse_signal_from_message = None  # type: ignore[assignment]
    _HAS_PARSE_SIGNAL = False


# ─────────────────────────────────────────────────────────────────────────────
# Source constants
# ─────────────────────────────────────────────────────────────────────────────
class TestSourceConstants(unittest.TestCase):
    def test_source_values(self):
        self.assertEqual(SOURCE_WEBHOOK,  "webhook")
        self.assertEqual(SOURCE_DOWNLOAD, "download")
        self.assertEqual(SOURCE_SCREENER, "screener")
        self.assertEqual(SOURCE_MANUAL,   "manual")

    def test_all_sources_distinct(self):
        sources = [SOURCE_WEBHOOK, SOURCE_DOWNLOAD, SOURCE_SCREENER, SOURCE_MANUAL]
        self.assertEqual(len(set(sources)), 4)


# ─────────────────────────────────────────────────────────────────────────────
# TICKER_ALIASES
# ─────────────────────────────────────────────────────────────────────────────
class TestTickerAliases(unittest.TestCase):
    def test_minimum_alias_count(self):
        self.assertGreater(len(TICKER_ALIASES), 50,
                           "TICKER_ALIASES should contain at least 50 entries")

    def test_known_aliases_present(self):
        expected = {
            "oracle":   "NYSE:ORCL",
            "nvidia":   "NASDAQ:NVDA",
            "reliance": "NSE:RELIANCE",
            "tcs":      "NSE:TCS",
            "bhp":      "ASX:BHP",
        }
        for name, sym in expected.items():
            with self.subTest(name=name):
                self.assertEqual(TICKER_ALIASES[name], sym)

    def test_all_values_contain_colon(self):
        for name, sym in TICKER_ALIASES.items():
            with self.subTest(name=name):
                self.assertIn(":", sym, f"Alias value {sym!r} must be EXCHANGE:TICKER")

    def test_all_keys_lowercase(self):
        for key in TICKER_ALIASES:
            self.assertEqual(key, key.lower(), f"Key {key!r} must be lowercase")


# ─────────────────────────────────────────────────────────────────────────────
# validate_and_normalise
# ─────────────────────────────────────────────────────────────────────────────
class TestValidateAndNormalise(unittest.TestCase):

    # ── Company name (alias) ──────────────────────────────────────────────────
    def test_company_name_resolves_via_alias(self):
        result = validate_and_normalise("oracle")
        self.assertEqual(result.pro_symbol, "NYSE:ORCL")
        self.assertEqual(result.error, "")

    def test_company_name_case_insensitive(self):
        result = validate_and_normalise("Oracle")
        self.assertEqual(result.pro_symbol, "NYSE:ORCL")

    def test_nvidia_alias(self):
        result = validate_and_normalise("nvidia")
        self.assertEqual(result.pro_symbol, "NASDAQ:NVDA")

    def test_india_alias(self):
        result = validate_and_normalise("reliance")
        self.assertEqual(result.pro_symbol, "NSE:RELIANCE")

    # ── Already-qualified EXCHANGE:TICKER ────────────────────────────────────
    def test_qualified_symbol_returned_uppercase(self):
        result = validate_and_normalise("NASDAQ:NVDA")
        self.assertEqual(result.pro_symbol, "NASDAQ:NVDA")
        self.assertEqual(result.error, "")

    def test_qualified_symbol_lowercased_input(self):
        result = validate_and_normalise("nasdaq:nvda")
        self.assertEqual(result.pro_symbol, "NASDAQ:NVDA")

    def test_qualified_nse_symbol(self):
        result = validate_and_normalise("NSE:MRF")
        self.assertEqual(result.pro_symbol, "NSE:MRF")

    def test_malformed_exchange_colon_only(self):
        result = validate_and_normalise(":")
        self.assertNotEqual(result.error, "")
        self.assertEqual(result.pro_symbol, "")

    def test_malformed_missing_ticker(self):
        result = validate_and_normalise("NASDAQ:")
        self.assertNotEqual(result.error, "")

    def test_empty_string(self):
        result = validate_and_normalise("")
        self.assertNotEqual(result.error, "")

    def test_whitespace_only(self):
        result = validate_and_normalise("   ")
        self.assertNotEqual(result.error, "")

    def test_non_string_input(self):
        result = validate_and_normalise(123)  # type: ignore[arg-type]
        self.assertNotEqual(result.error, "")

    # ── Bare ticker — offline (network disabled) ──────────────────────────────
    def test_bare_ticker_not_in_cache_no_network_returns_error(self):
        """Bare ticker with no cache and network disabled should fail gracefully."""
        with patch("input_sources._resolve_exchange_via_fires_cache", return_value=""), \
             patch("input_sources._resolve_exchange_via_tv_search", return_value=""):
            result = validate_and_normalise("NVDAXYZ_FAKE")
            self.assertEqual(result.pro_symbol, "")
            self.assertIn("could not resolve", result.error)

    def test_bare_ticker_resolved_via_fires_cache(self):
        with patch("input_sources._resolve_exchange_via_fires_cache", return_value="NASDAQ"), \
             patch("input_sources._resolve_exchange_via_tv_search", return_value=""):
            result = validate_and_normalise("NVDA")
            self.assertEqual(result.pro_symbol, "NASDAQ:NVDA")
            self.assertEqual(result.error, "")

    def test_bare_ticker_resolved_via_tv_search_fallback(self):
        with patch("input_sources._resolve_exchange_via_fires_cache", return_value=""), \
             patch("input_sources._resolve_exchange_via_tv_search", return_value="NYSE"):
            result = validate_and_normalise("ORCL")
            self.assertEqual(result.pro_symbol, "NYSE:ORCL")

    def test_bare_ticker_invalid_chars(self):
        result = validate_and_normalise("nv da!@#")
        self.assertNotEqual(result.error, "")
        self.assertEqual(result.pro_symbol, "")

    # ── ValidationResult is a NamedTuple ────────────────────────────────────
    def test_returns_validation_result_type(self):
        result = validate_and_normalise("oracle")
        self.assertIsInstance(result, ValidationResult)

    def test_successful_result_has_empty_error(self):
        result = validate_and_normalise("oracle")
        self.assertEqual(result.error, "")

    def test_failed_result_has_empty_pro_symbol(self):
        with patch("input_sources._resolve_exchange_via_fires_cache", return_value=""), \
             patch("input_sources._resolve_exchange_via_tv_search", return_value=""):
            result = validate_and_normalise("ZZZNOFOUND")
            self.assertEqual(result.pro_symbol, "")


# ─────────────────────────────────────────────────────────────────────────────
# _make_fire
# ─────────────────────────────────────────────────────────────────────────────
class TestMakeFire(unittest.TestCase):

    def _fire(self, source=SOURCE_MANUAL):
        return _make_fire("NASDAQ:NVDA", source)

    def test_required_fields_present(self):
        fire = self._fire()
        for field in ("fire_id", "pro_symbol", "symbol", "resolution",
                      "message", "fire_time", "bar_time", "_source"):
            with self.subTest(field=field):
                self.assertIn(field, fire)

    def test_source_tag_set(self):
        fire = _make_fire("NSE:RELIANCE", SOURCE_SCREENER)
        self.assertEqual(fire["_source"], SOURCE_SCREENER)

    def test_pro_symbol_preserved(self):
        fire = _make_fire("ASX:BHP", SOURCE_MANUAL)
        self.assertEqual(fire["pro_symbol"], "ASX:BHP")
        self.assertEqual(fire["symbol"], "ASX:BHP")

    def test_custom_message(self):
        fire = _make_fire("NYSE:ORCL", SOURCE_SCREENER, message="SCREENER BUY:1, rsi:65")
        self.assertIn("SCREENER", fire["message"])

    def test_fire_id_is_int(self):
        fire = self._fire()
        self.assertIsInstance(fire["fire_id"], int)

    def test_fire_time_is_iso_string(self):
        from datetime import datetime, timezone
        fire = self._fire()
        dt = datetime.fromisoformat(fire["fire_time"].replace("Z", "+00:00"))
        self.assertIsNotNone(dt)

    def test_close_stored(self):
        fire = _make_fire("NASDAQ:AAPL", SOURCE_MANUAL, close=175.5)
        self.assertAlmostEqual(fire["close"], 175.5)

    def test_close_none_by_default(self):
        fire = _make_fire("NASDAQ:AAPL", SOURCE_MANUAL)
        self.assertIsNone(fire["close"])


# ─────────────────────────────────────────────────────────────────────────────
# manual_to_fires
# ─────────────────────────────────────────────────────────────────────────────
class TestManualToFires(unittest.TestCase):

    def test_qualified_tickers_need_no_exchange_resolution(self):
        fires, errors = manual_to_fires(["NASDAQ:NVDA", "NSE:RELIANCE"],
                                        resolve_exchange=False)
        self.assertEqual(len(fires), 2)
        self.assertEqual(errors, {})

    def test_company_name_resolved_via_alias(self):
        fires, errors = manual_to_fires(["oracle"], resolve_exchange=False)
        self.assertEqual(len(fires), 1)
        self.assertEqual(fires[0]["pro_symbol"], "NYSE:ORCL")
        self.assertEqual(errors, {})

    def test_duplicate_tickers_deduped(self):
        fires, errors = manual_to_fires(
            ["NASDAQ:NVDA", "NASDAQ:NVDA", "NASDAQ:NVDA"],
            resolve_exchange=False,
        )
        self.assertEqual(len(fires), 1)

    def test_comma_separated_string_input(self):
        fires, errors = manual_to_fires("NASDAQ:NVDA, NSE:MRF, NYSE:ORCL",
                                        resolve_exchange=False)
        self.assertEqual(len(fires), 3)
        symbols = {f["pro_symbol"] for f in fires}
        self.assertIn("NASDAQ:NVDA", symbols)
        self.assertIn("NSE:MRF", symbols)
        self.assertIn("NYSE:ORCL", symbols)

    def test_unresolvable_ticker_goes_to_errors(self):
        with patch("input_sources._resolve_exchange_via_fires_cache", return_value=""), \
             patch("input_sources._resolve_exchange_via_tv_search", return_value=""):
            fires, errors = manual_to_fires(["ZZZNOFOUND_FAKE"])
            self.assertEqual(len(fires), 0)
            self.assertIn("ZZZNOFOUND_FAKE", errors)

    def test_mixed_valid_invalid(self):
        with patch("input_sources._resolve_exchange_via_fires_cache", return_value=""), \
             patch("input_sources._resolve_exchange_via_tv_search", return_value=""):
            fires, errors = manual_to_fires(["NYSE:ORCL", "BADFAKE_XYZ"])
            self.assertEqual(len(fires), 1)
            self.assertEqual(fires[0]["pro_symbol"], "NYSE:ORCL")
            self.assertIn("BADFAKE_XYZ", errors)

    def test_fires_have_source_tag_manual(self):
        fires, _ = manual_to_fires(["NSE:INFY"], resolve_exchange=False)
        self.assertEqual(fires[0]["_source"], SOURCE_MANUAL)

    def test_empty_list_returns_empty(self):
        fires, errors = manual_to_fires([])
        self.assertEqual(fires, [])
        self.assertEqual(errors, {})

    def test_returns_tuple_of_list_and_dict(self):
        result = manual_to_fires(["NASDAQ:NVDA"], resolve_exchange=False)
        fires, errors = result
        self.assertIsInstance(fires, list)
        self.assertIsInstance(errors, dict)

    def test_fires_contain_required_fields(self):
        fires, _ = manual_to_fires(["NASDAQ:AAPL"], resolve_exchange=False)
        fire = fires[0]
        for field in ("fire_id", "pro_symbol", "symbol", "message",
                      "fire_time", "_source", "resolution"):
            with self.subTest(field=field):
                self.assertIn(field, fire)

    def test_whitespace_stripped_from_string_input(self):
        fires, errors = manual_to_fires("  NASDAQ:NVDA  ,  NSE:MRF  ",
                                        resolve_exchange=False)
        syms = {f["pro_symbol"] for f in fires}
        self.assertIn("NASDAQ:NVDA", syms)
        self.assertIn("NSE:MRF", syms)


# ─────────────────────────────────────────────────────────────────────────────
# _parse_signal_from_message (if exported — skip if not)
# ─────────────────────────────────────────────────────────────────────────────
class TestParseSignalFromMessage(unittest.TestCase):

    def _check_available(self):
        if not _HAS_PARSE_SIGNAL:
            self.skipTest("_parse_signal_from_message not exported from input_sources")

    def test_buy_signal(self):
        self._check_available()
        self.assertEqual(_parse_signal_from_message("BUY:1, rsi:60"), "BUY")  # type: ignore[misc]

    def test_sell_signal(self):
        self._check_available()
        self.assertEqual(_parse_signal_from_message("SELL SIGNAL"), "SELL")  # type: ignore[misc]

    def test_screener_signal(self):
        self._check_available()
        self.assertEqual(_parse_signal_from_message("SCREENER BUY:1"), "SCREENER")  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main(verbosity=2)
