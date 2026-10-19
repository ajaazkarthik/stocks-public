"""
Unit tests for WatchlistCleaner with mocked TV API calls.

Tests cover:
- process_skipped() skips FX/Crypto symbols (never auto-remove)
- process_skipped() skips symbols whose source is not a watchlist source
- process_skipped() returns manual_required when API call fails
- remove_from_watchlist() returns True when symbol not in list (idempotent)
- remove_from_watchlist() returns False when auth fails
- _print_manual_required() produces formatted block (smoke test)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_TV_DIR = Path(__file__).parent.parent.parent   # tradingview/
sys.path.insert(0, str(_TV_DIR))

from watchlist_cleaner import WatchlistCleaner, _WATCHLIST_SOURCES  # noqa: E402


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _skipped(symbol: str, source: str = "watchlist", reason: str = "not tradable") -> dict:
    return {"symbol": symbol, "source": source, "reason": reason}


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def cleaner(tmp_path):
    """WatchlistCleaner with no real cookie file."""
    return WatchlistCleaner(cookie_file=tmp_path / "no_cookie", dry_run=False)


@pytest.fixture
def cleaner_with_auth(tmp_path):
    """WatchlistCleaner with a fake but parseable cookie."""
    cookie = tmp_path / "cookie"
    cookie.write_text("sessionid=abc123; sessionid_sign=xyz789", encoding="utf-8")
    return WatchlistCleaner(cookie_file=cookie, dry_run=False)


# ─── process_skipped: FX/Crypto bypass ───────────────────────────────────────

class TestProcessSkippedForexCrypto:

    @pytest.mark.parametrize("symbol", [
        "FX:EURUSD", "FX_IDC:GBPUSD", "OANDA:USDJPY",
        "BINANCE:BTCUSDT", "COINBASE:ETHUSD", "KRAKEN:XBTUSD",
    ])
    def test_forex_crypto_never_removed(self, cleaner_with_auth, symbol):
        """FX/Crypto symbols must be left untouched regardless of source."""
        skipped = [_skipped(symbol, source="watchlist")]
        result  = cleaner_with_auth.process_skipped(skipped)
        assert result["cleaned"] == []
        assert result["manual_required"] == []

    def test_mixed_list_skips_forex_only(self, cleaner_with_auth):
        """Non-FX symbol still processed; FX symbol skipped."""
        non_fx = _skipped("TYO:7203", source="watchlist")
        fx     = _skipped("FX:EURUSD", source="watchlist")
        # Mock the actual remove call so we don't need a real TV API
        with patch.object(cleaner_with_auth, "remove_from_watchlist", return_value=True) as mock_rm:
            result = cleaner_with_auth.process_skipped([non_fx, fx])
        # remove_from_watchlist should only be called for TYO:7203
        mock_rm.assert_called_once_with("TYO:7203", None)
        assert len(result["cleaned"]) == 1
        assert result["cleaned"][0]["symbol"] == "TYO:7203"


# ─── process_skipped: source filtering ───────────────────────────────────────

class TestProcessSkippedSource:

    @pytest.mark.parametrize("source", ["webhook", "screener", "manual", "news", "unknown"])
    def test_non_watchlist_source_skipped(self, cleaner_with_auth, source):
        """Symbols from non-watchlist sources must not be removed."""
        skipped = [_skipped("TYO:7203", source=source)]
        with patch.object(cleaner_with_auth, "remove_from_watchlist") as mock_rm:
            result = cleaner_with_auth.process_skipped(skipped)
        mock_rm.assert_not_called()
        assert result["cleaned"] == []
        assert result["manual_required"] == []

    @pytest.mark.parametrize("source", list(_WATCHLIST_SOURCES))
    def test_watchlist_source_processed(self, cleaner_with_auth, source):
        """Symbols from watchlist sources should attempt removal."""
        skipped = [_skipped("TYO:7203", source=source)]
        with patch.object(cleaner_with_auth, "remove_from_watchlist", return_value=True):
            result = cleaner_with_auth.process_skipped(skipped)
        assert len(result["cleaned"]) == 1


# ─── process_skipped: manual_required on API failure ─────────────────────────

class TestProcessSkippedFailure:

    def test_api_failure_returns_manual_required(self, cleaner_with_auth, capsys):
        """When remove_from_watchlist returns False, symbol goes to manual_required."""
        skipped = [_skipped("LSE:HSBA", source="watchlist")]
        with patch.object(cleaner_with_auth, "remove_from_watchlist", return_value=False):
            result = cleaner_with_auth.process_skipped(skipped)
        assert result["cleaned"] == []
        assert len(result["manual_required"]) == 1
        assert result["manual_required"][0]["symbol"] == "LSE:HSBA"

    def test_manual_required_prints_formatted_block(self, cleaner_with_auth, capsys):
        """_print_manual_required must produce the ╔═╗ formatted block."""
        skipped = [_skipped("LSE:HSBA", source="watchlist")]
        with patch.object(cleaner_with_auth, "remove_from_watchlist", return_value=False):
            cleaner_with_auth.process_skipped(skipped)
        out = capsys.readouterr().out
        assert "WATCHLIST CLEANUP REQUIRED" in out
        assert "LSE:HSBA" in out
        # Box drawing characters present
        assert "╔" in out or "\u2554" in out

    def test_empty_skipped_list_returns_empty(self, cleaner_with_auth):
        result = cleaner_with_auth.process_skipped([])
        assert result == {"cleaned": [], "manual_required": []}


# ─── remove_from_watchlist: auth failure ─────────────────────────────────────

class TestRemoveFromWatchlist:

    def test_no_cookie_returns_false(self, cleaner):
        """With no cookie file, remove must fail gracefully."""
        ok = cleaner.remove_from_watchlist("TYO:7203")
        assert ok is False

    def test_symbol_not_in_watchlist_is_idempotent(self, cleaner_with_auth):
        """
        If symbol is not in the current list, return True (already clean).
        """
        with patch("watchlist_cleaner._tv_get") as mock_get, \
             patch("watchlist_cleaner._tv_put") as mock_put:
            # Simulate watchlists list response
            mock_get.side_effect = [
                [{"name": "ALL_NEW", "id": "99"}],   # list watchlists
                ["NSE:TITAN", "NYSE:AAPL"],            # get symbols (no TYO:7203)
            ]
            ok = cleaner_with_auth.remove_from_watchlist("TYO:7203", "ALL_NEW")
        assert ok is True
        mock_put.assert_not_called()   # no PUT needed

    def test_symbol_present_triggers_put(self, cleaner_with_auth):
        """If symbol is in list, PUT is called with symbol removed."""
        with patch("watchlist_cleaner._tv_get") as mock_get, \
             patch("watchlist_cleaner._tv_put") as mock_put:
            mock_get.side_effect = [
                [{"name": "ALL_NEW", "id": "99"}],
                ["NSE:TITAN", "TYO:7203", "NYSE:AAPL"],
            ]
            mock_put.return_value = {"status": "ok"}
            ok = cleaner_with_auth.remove_from_watchlist("TYO:7203", "ALL_NEW")
        assert ok is True
        # Verify PUT was called with TYO:7203 removed
        call_args = mock_put.call_args
        pushed_symbols = call_args[0][1]["symbols"]
        assert "TYO:7203" not in pushed_symbols
        assert "NSE:TITAN" in pushed_symbols
        assert "NYSE:AAPL" in pushed_symbols

    def test_put_failure_returns_false(self, cleaner_with_auth):
        """If PUT returns None, remove_from_watchlist returns False."""
        with patch("watchlist_cleaner._tv_get") as mock_get, \
             patch("watchlist_cleaner._tv_put") as mock_put:
            mock_get.side_effect = [
                [{"name": "ALL_NEW", "id": "99"}],
                ["TYO:7203", "NSE:TITAN"],
            ]
            mock_put.return_value = None   # PUT failed
            ok = cleaner_with_auth.remove_from_watchlist("TYO:7203", "ALL_NEW")
        assert ok is False

    def test_watchlist_not_found_returns_false(self, cleaner_with_auth):
        """If named watchlist doesn't exist in API response, return False."""
        with patch("watchlist_cleaner._tv_get") as mock_get:
            mock_get.return_value = [{"name": "OTHER_LIST", "id": "1"}]
            ok = cleaner_with_auth.remove_from_watchlist("TYO:7203", "ALL_NEW")
        assert ok is False

    def test_dry_run_does_not_call_put(self, tmp_path):
        """dry_run=True must not call PUT even if symbol is present."""
        cookie = tmp_path / "cookie"
        cookie.write_text("sessionid=abc; sessionid_sign=xyz", encoding="utf-8")
        cleaner = WatchlistCleaner(cookie_file=cookie, dry_run=True)
        with patch("watchlist_cleaner._tv_get") as mock_get, \
             patch("watchlist_cleaner._tv_put") as mock_put:
            mock_get.side_effect = [
                [{"name": "ALL_NEW", "id": "99"}],
                ["TYO:7203"],
            ]
            ok = cleaner.remove_from_watchlist("TYO:7203", "ALL_NEW")
        assert ok is True
        mock_put.assert_not_called()


# ─── _print_manual_required: smoke test ──────────────────────────────────────

class TestPrintManualRequired:

    def test_output_format_contains_box_chars(self, capsys):
        WatchlistCleaner._print_manual_required([
            {"symbol": "LSE:HSBA", "reason": "LSE not supported"},
            {"symbol": "TYO:7203", "reason": "TYO not supported"},
        ])
        out = capsys.readouterr().out
        assert "WATCHLIST CLEANUP REQUIRED" in out
        assert "LSE:HSBA" in out
        assert "TYO:7203" in out
        assert "LSE not supported" in out
