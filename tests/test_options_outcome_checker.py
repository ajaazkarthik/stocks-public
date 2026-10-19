"""Tests for tradingview/options/options_outcome_checker.py."""

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))
sys.path.insert(0, str(ROOT / "tradingview" / "options"))

import options_outcome_checker as ooc


class TestBareSymbol:

    def test_bare_symbol_strips_exchange_prefix(self):
        assert ooc._bare_symbol("NASDAQ:AAPL") == "AAPL"

    def test_bare_symbol_passthrough(self):
        assert ooc._bare_symbol("AAPL") == "AAPL"

    def test_bare_symbol_strips_asx_prefix(self):
        assert ooc._bare_symbol("ASX:BHP") == "BHP"

    def test_bare_symbol_strips_nse_prefix(self):
        assert ooc._bare_symbol("NSE:INFY") == "INFY"

    def test_bare_symbol_uses_first_colon(self):
        # Edge: symbol with multiple colons takes only the part after first ":"
        assert ooc._bare_symbol("A:B:C") == "B:C"


class TestDetermineOutcome:

    def test_determine_outcome_call_win(self):
        assert ooc._determine_outcome("CALL", 150.0, 155.0) == "WIN"

    def test_determine_outcome_call_loss(self):
        assert ooc._determine_outcome("CALL", 150.0, 145.0) == "LOSS"

    def test_determine_outcome_put_win(self):
        assert ooc._determine_outcome("PUT", 150.0, 145.0) == "WIN"

    def test_determine_outcome_put_loss(self):
        assert ooc._determine_outcome("PUT", 150.0, 155.0) == "LOSS"

    def test_determine_outcome_invalid_type(self):
        assert ooc._determine_outcome("", 150.0, 155.0) == "EXPIRED"

    def test_determine_outcome_nan_strike(self):
        assert ooc._determine_outcome("CALL", float("nan"), 155.0) == "EXPIRED"

    def test_determine_outcome_case_insensitive(self):
        assert ooc._determine_outcome("call", 150.0, 155.0) == "WIN"
        assert ooc._determine_outcome("put", 150.0, 145.0) == "WIN"

    def test_determine_outcome_unknown_type_returns_expired(self):
        assert ooc._determine_outcome("SPREAD", 150.0, 155.0) == "EXPIRED"

    def test_determine_outcome_call_at_the_money(self):
        # spot == strike: not above strike, so LOSS for CALL
        assert ooc._determine_outcome("CALL", 150.0, 150.0) == "LOSS"

    def test_determine_outcome_put_at_the_money(self):
        # spot == strike: not below strike, so LOSS for PUT
        assert ooc._determine_outcome("PUT", 150.0, 150.0) == "LOSS"


class TestGetSpotAtExpiry:

    def test_get_spot_yfinance_returns_none_on_empty_history(self, mocker):
        mock_ticker = mocker.MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mocker.patch("yfinance.Ticker", return_value=mock_ticker)
        result = ooc._get_spot_at_expiry("AAPL", date(2026, 1, 15))
        assert result is None

    def test_get_spot_yfinance_returns_none_on_exception(self, mocker):
        mocker.patch("yfinance.Ticker", side_effect=RuntimeError("network error"))
        result = ooc._get_spot_at_expiry("AAPL", date(2026, 1, 15))
        assert result is None

    def test_get_spot_yfinance_returns_first_close_price(self, mocker):
        hist = pd.DataFrame(
            {"Close": [155.0, 156.5]},
            index=pd.to_datetime(["2026-01-15", "2026-01-16"]),
        )
        mock_ticker = mocker.MagicMock()
        mock_ticker.history.return_value = hist
        mocker.patch("yfinance.Ticker", return_value=mock_ticker)
        result = ooc._get_spot_at_expiry("AAPL", date(2026, 1, 15))
        assert result == 155.0

    def test_get_spot_strips_exchange_prefix_before_yfinance(self, mocker):
        mock_ticker = mocker.MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_yf_ticker = mocker.patch("yfinance.Ticker", return_value=mock_ticker)
        ooc._get_spot_at_expiry("NASDAQ:AAPL", date(2026, 1, 15))
        mock_yf_ticker.assert_called_once_with("AAPL")

    def test_get_spot_returns_none_for_zero_close(self, mocker):
        hist = pd.DataFrame(
            {"Close": [0.0]},
            index=pd.to_datetime(["2026-01-15"]),
        )
        mock_ticker = mocker.MagicMock()
        mock_ticker.history.return_value = hist
        mocker.patch("yfinance.Ticker", return_value=mock_ticker)
        result = ooc._get_spot_at_expiry("AAPL", date(2026, 1, 15))
        assert result is None


class TestRunDryRun:

    def test_run_dry_run_no_db_writes(self, mocker):
        """dry_run=True must not call update_options_rec_outcome."""

        class _Row(dict):
            pass

        row = _Row(
            rec_uuid="uuid-abc-123",
            tv_symbol="AAPL",
            option_type="CALL",
            strike=150.0,
            expiry=(date.today() - timedelta(days=1)).isoformat(),
        )

        mock_conn = mocker.MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [row]
        mocker.patch("sqlite3.connect", return_value=mock_conn)

        mock_tracker_cls = mocker.patch("alert_tracker.AlertTracker")
        mock_tracker = mock_tracker_cls.return_value
        mocker.patch("alert_tracker.DB_PATH", "fake.db")

        mocker.patch.object(ooc, "_get_spot_at_expiry", return_value=155.0)

        ooc.run(dry_run=True)

        mock_tracker.update_options_rec_outcome.assert_not_called()

    def test_run_returns_zero_when_no_open_rows(self, mocker):
        mock_conn = mocker.MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mocker.patch("sqlite3.connect", return_value=mock_conn)
        mocker.patch("alert_tracker.AlertTracker")
        mocker.patch("alert_tracker.DB_PATH", "fake.db")

        result = ooc.run(dry_run=True)
        assert result == 0

    def test_run_non_dry_run_calls_update(self, mocker):
        """dry_run=False MUST call update_options_rec_outcome."""

        class _Row(dict):
            pass

        row = _Row(
            rec_uuid="uuid-xyz-999",
            tv_symbol="MSFT",
            option_type="PUT",
            strike=200.0,
            expiry=(date.today() - timedelta(days=1)).isoformat(),
        )

        mock_conn = mocker.MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [row]
        mocker.patch("sqlite3.connect", return_value=mock_conn)

        mock_tracker_cls = mocker.patch("alert_tracker.AlertTracker")
        mock_tracker = mock_tracker_cls.return_value
        mocker.patch("alert_tracker.DB_PATH", "fake.db")

        mocker.patch.object(ooc, "_get_spot_at_expiry", return_value=195.0)

        result = ooc.run(dry_run=False)

        mock_tracker.update_options_rec_outcome.assert_called_once()
        assert result == 1
