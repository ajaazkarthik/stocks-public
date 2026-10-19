"""Tests for rba_fx_rates.py — RBA FX rate lookup for CGT compliance."""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rba_fx_rates


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Minimal RBA f11-style CSV with known rates
_SAMPLE_CSV = """\
Title,Exchange Rates – Daily – F11
Description,"Units of foreign currencies per Australian dollar"
Frequency,Daily
Type,Original
Series ID,FXRUSD,FXRGBP,FXREUR,FXRJPY,FXRHKD,FXRSGD,FXRNZD
Unit,$,$,$,JPY,$,$,$
02-Jan-2025,0.6250,0.5010,0.5980,95.1200,4.8750,0.8390,1.0980
03-Jan-2025,0.6280,0.5030,0.5995,95.4500,4.8900,0.8410,1.1010
06-Jan-2025,0.6310,0.5050,0.6010,95.8000,4.9100,0.8430,1.1050
07-Jan-2025,0.6295,0.5040,0.6000,95.6000,4.9000,0.8420,1.1030
"""


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset module-level cache between tests."""
    rba_fx_rates._rates = {}
    rba_fx_rates._cache_loaded_at = None
    yield
    rba_fx_rates._rates = {}
    rba_fx_rates._cache_loaded_at = None


@pytest.fixture()
def loaded_rates():
    """Pre-load rates from sample CSV without hitting the network."""
    rates = rba_fx_rates._parse_rba_csv(_SAMPLE_CSV)
    rba_fx_rates._rates = rates
    rba_fx_rates._cache_loaded_at = datetime.now()
    return rates


# ---------------------------------------------------------------------------
# Test: CSV parsing
# ---------------------------------------------------------------------------

class TestParseRbaCsv:
    def test_parses_all_currencies(self):
        rates = rba_fx_rates._parse_rba_csv(_SAMPLE_CSV)
        assert set(rates.keys()) == {"USD", "GBP", "EUR", "JPY", "HKD", "SGD", "NZD"}

    def test_parses_known_usd_rate(self):
        rates = rba_fx_rates._parse_rba_csv(_SAMPLE_CSV)
        assert rates["USD"]["2025-01-02"] == pytest.approx(0.6250)
        assert rates["USD"]["2025-01-03"] == pytest.approx(0.6280)

    def test_parses_jpy_rate(self):
        rates = rba_fx_rates._parse_rba_csv(_SAMPLE_CSV)
        assert rates["JPY"]["2025-01-02"] == pytest.approx(95.12)

    def test_correct_number_of_dates(self):
        rates = rba_fx_rates._parse_rba_csv(_SAMPLE_CSV)
        assert len(rates["USD"]) == 4

    def test_raises_on_missing_header(self):
        with pytest.raises(ValueError, match="Could not find RBA series header"):
            rba_fx_rates._parse_rba_csv("some random text\nno headers here\n")


# ---------------------------------------------------------------------------
# Test: get_rba_rate — exact date
# ---------------------------------------------------------------------------

class TestGetRbaRateExact:
    def test_returns_exact_rate(self, loaded_rates):
        rate = rba_fx_rates.get_rba_rate("USD", date(2025, 1, 3))
        assert rate == pytest.approx(0.6280)

    def test_returns_gbp_rate(self, loaded_rates):
        rate = rba_fx_rates.get_rba_rate("GBP", date(2025, 1, 2))
        assert rate == pytest.approx(0.5010)

    def test_aud_returns_one(self, loaded_rates):
        rate = rba_fx_rates.get_rba_rate("AUD", date(2025, 1, 2))
        assert rate == 1.0

    def test_case_insensitive(self, loaded_rates):
        rate = rba_fx_rates.get_rba_rate("usd", date(2025, 1, 3))
        assert rate == pytest.approx(0.6280)


# ---------------------------------------------------------------------------
# Test: weekend / holiday fallback (uses prior business day)
# ---------------------------------------------------------------------------

class TestWeekendFallback:
    def test_saturday_falls_back_to_friday(self, loaded_rates):
        # 2025-01-04 is Saturday, 2025-01-03 (Friday) has data
        rate = rba_fx_rates.get_rba_rate("USD", date(2025, 1, 4))
        assert rate == pytest.approx(0.6280)  # Friday's rate

    def test_sunday_falls_back_to_friday(self, loaded_rates):
        # 2025-01-05 is Sunday, 2025-01-03 (Friday) has data
        rate = rba_fx_rates.get_rba_rate("USD", date(2025, 1, 5))
        assert rate == pytest.approx(0.6280)  # Friday's rate


# ---------------------------------------------------------------------------
# Test: unsupported currency
# ---------------------------------------------------------------------------

class TestUnsupportedCurrency:
    def test_unknown_currency_returns_none(self, loaded_rates):
        rate = rba_fx_rates.get_rba_rate("XYZ", date(2025, 1, 2))
        assert rate is None

    def test_empty_string_returns_none(self, loaded_rates):
        rate = rba_fx_rates.get_rba_rate("", date(2025, 1, 2))
        assert rate is None


# ---------------------------------------------------------------------------
# Test: cache round-trip
# ---------------------------------------------------------------------------

class TestCacheRoundTrip:
    def test_save_and_load_cache(self, tmp_path):
        rates = rba_fx_rates._parse_rba_csv(_SAMPLE_CSV)

        cache_file = tmp_path / "rba_fx_rates.json"
        # Temporarily redirect cache file
        orig_cache = rba_fx_rates._CACHE_FILE
        orig_dir = rba_fx_rates._CACHE_DIR
        try:
            rba_fx_rates._CACHE_FILE = cache_file
            rba_fx_rates._CACHE_DIR = tmp_path

            rba_fx_rates._save_cache(rates)
            assert cache_file.exists()

            loaded = rba_fx_rates._load_cache()
            assert loaded is not None
            assert loaded["USD"]["2025-01-02"] == pytest.approx(0.6250)
            assert set(loaded.keys()) == {"USD", "GBP", "EUR", "JPY", "HKD", "SGD", "NZD"}
        finally:
            rba_fx_rates._CACHE_FILE = orig_cache
            rba_fx_rates._CACHE_DIR = orig_dir

    def test_stale_cache_returns_none(self, tmp_path):
        cache_file = tmp_path / "rba_fx_rates.json"
        # Write cache with an old timestamp
        payload = {
            "fetched_at": (datetime.now() - timedelta(hours=48)).isoformat(),
            "rates": {"USD": {"2025-01-02": 0.625}},
        }
        cache_file.write_text(json.dumps(payload), encoding="utf-8")

        orig_cache = rba_fx_rates._CACHE_FILE
        try:
            rba_fx_rates._CACHE_FILE = cache_file
            loaded = rba_fx_rates._load_cache()
            assert loaded is None  # too old
        finally:
            rba_fx_rates._CACHE_FILE = orig_cache

    def test_corrupt_cache_returns_none(self, tmp_path):
        cache_file = tmp_path / "rba_fx_rates.json"
        cache_file.write_text("NOT VALID JSON!!!", encoding="utf-8")

        orig_cache = rba_fx_rates._CACHE_FILE
        try:
            rba_fx_rates._CACHE_FILE = cache_file
            loaded = rba_fx_rates._load_cache()
            assert loaded is None
        finally:
            rba_fx_rates._CACHE_FILE = orig_cache


# ---------------------------------------------------------------------------
# Test: yfinance fallback
# ---------------------------------------------------------------------------

class TestYfinanceFallback:
    @patch("rba_fx_rates._download_csv", side_effect=Exception("Network error"))
    @patch("rba_fx_rates._load_cache", return_value=None)
    @patch("rba_fx_rates._yfinance_fallback", return_value=0.6500)
    def test_falls_back_to_yfinance_when_rba_unavailable(
        self, mock_yf, mock_cache, mock_dl
    ):
        # _ensure_loaded will fail, then get_rba_rate falls back to yfinance
        rate = rba_fx_rates.get_rba_rate("USD", date(2025, 6, 15))
        assert rate == pytest.approx(0.6500)
        mock_yf.assert_called_once_with("USD", date(2025, 6, 15))

    @patch("rba_fx_rates._download_csv", side_effect=Exception("Network error"))
    @patch("rba_fx_rates._load_cache", return_value=None)
    @patch("rba_fx_rates._yfinance_fallback", return_value=None)
    def test_returns_none_when_both_fail(self, mock_yf, mock_cache, mock_dl):
        rate = rba_fx_rates.get_rba_rate("USD", date(2025, 6, 15))
        assert rate is None


# ---------------------------------------------------------------------------
# Test: _ensure_loaded with mocked download
# ---------------------------------------------------------------------------

class TestEnsureLoaded:
    @patch("rba_fx_rates._load_cache", return_value=None)
    @patch("rba_fx_rates._download_csv", return_value=_SAMPLE_CSV)
    def test_downloads_and_populates_rates(self, mock_dl, mock_cache):
        rba_fx_rates._ensure_loaded()
        assert "USD" in rba_fx_rates._rates
        assert len(rba_fx_rates._rates["USD"]) == 4
        mock_dl.assert_called_once()

    @patch("rba_fx_rates._load_cache", return_value=None)
    @patch("rba_fx_rates._download_csv", return_value=_SAMPLE_CSV)
    def test_does_not_redownload_if_fresh(self, mock_dl, mock_cache):
        rba_fx_rates._ensure_loaded()
        assert mock_dl.call_count == 1
        rba_fx_rates._ensure_loaded()  # second call should be no-op
        assert mock_dl.call_count == 1


# ---------------------------------------------------------------------------
# Test: get_available_dates
# ---------------------------------------------------------------------------

class TestGetAvailableDates:
    def test_returns_sorted_dates(self, loaded_rates):
        dates = rba_fx_rates.get_available_dates("USD")
        assert dates == [
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
            date(2025, 1, 7),
        ]

    def test_unknown_currency_returns_empty(self, loaded_rates):
        dates = rba_fx_rates.get_available_dates("XYZ")
        assert dates == []
