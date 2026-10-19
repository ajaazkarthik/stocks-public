"""Tests for india-brokers/zerodha_websocket.py — Zerodha WebSocket streaming."""
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "india-brokers"))

import zerodha_websocket


# ---------------------------------------------------------------------------
# Test: TickData
# ---------------------------------------------------------------------------

class TestTickData:
    def test_from_kite_tick(self):
        tick = {
            "instrument_token": 12345,
            "last_price": 2500.0,
            "volume_traded": 1000000,
            "change": 1.5,
            "ohlc": {"open": 2480.0, "high": 2520.0, "low": 2470.0, "close": 2490.0},
        }
        symbol_map = {12345: "RELIANCE"}
        td = zerodha_websocket.TickData.from_kite_tick(tick, symbol_map)
        assert td.tradingsymbol == "RELIANCE"
        assert td.last_price == 2500.0
        assert td.volume == 1000000
        assert td.change == 1.5
        assert td.open_price == 2480.0
        assert td.high_price == 2520.0

    def test_from_kite_tick_unknown_token(self):
        tick = {"instrument_token": 99999, "last_price": 100.0, "ohlc": {}}
        td = zerodha_websocket.TickData.from_kite_tick(tick, {})
        assert td.tradingsymbol == "99999"
        assert td.last_price == 100.0

    def test_from_kite_tick_minimal(self):
        tick = {"instrument_token": 1, "last_price": 50.0}
        td = zerodha_websocket.TickData.from_kite_tick(tick, {1: "TCS"})
        assert td.last_price == 50.0
        assert td.volume == 0


# ---------------------------------------------------------------------------
# Test: ZerodhaWebSocket initialization
# ---------------------------------------------------------------------------

class TestZerodhaWebSocketInit:
    def test_creates_with_defaults(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        assert ws.api_key == ""
        assert ws.access_token == ""
        assert ws.is_connected is False

    def test_creates_with_credentials(self):
        fake_api_key = "test_" + "key"
        fake_access_token = "test_" + "token"
        ws = zerodha_websocket.ZerodhaWebSocket(api_key=fake_api_key, access_token=fake_access_token)
        assert ws.api_key == "test_key"
        assert ws.access_token == "test_token"

    def test_initial_state(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        assert ws.tick_count == 0
        assert ws.last_tick_time is None
        assert ws.is_connected is False


# ---------------------------------------------------------------------------
# Test: subscribe
# ---------------------------------------------------------------------------

class TestSubscribe:
    def test_subscribe_with_tokens(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        tokens = {"RELIANCE": 12345, "TCS": 67890}
        ws.subscribe(["RELIANCE", "TCS"], tokens=tokens)
        assert 12345 in ws._instrument_tokens
        assert 67890 in ws._instrument_tokens
        assert ws._symbol_map[12345] == "RELIANCE"


# ---------------------------------------------------------------------------
# Test: _on_ticks callback
# ---------------------------------------------------------------------------

class TestOnTicks:
    def test_increments_tick_count(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        ws._symbol_map = {12345: "RELIANCE"}
        ticks = [{"instrument_token": 12345, "last_price": 2500.0, "ohlc": {}}]
        ws._on_ticks(MagicMock(), ticks)
        assert ws.tick_count == 1

    def test_calls_user_callback(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        ws._symbol_map = {12345: "RELIANCE"}
        received = []
        ws.on_tick_callback = lambda ticks: received.extend(ticks)
        ticks = [{"instrument_token": 12345, "last_price": 2500.0, "ohlc": {}}]
        ws._on_ticks(MagicMock(), ticks)
        assert len(received) == 1
        assert received[0].last_price == 2500.0

    def test_handles_callback_error(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        ws._symbol_map = {12345: "RELIANCE"}
        errors = []
        ws.on_tick_callback = lambda ticks: (_ for _ in ()).throw(ValueError("test"))
        ws.on_error_callback = lambda msg: errors.append(msg)
        ticks = [{"instrument_token": 12345, "last_price": 2500.0, "ohlc": {}}]
        ws._on_ticks(MagicMock(), ticks)
        assert len(errors) == 1
        assert "callback error" in errors[0].lower()


# ---------------------------------------------------------------------------
# Test: _on_connect / _on_close
# ---------------------------------------------------------------------------

class TestConnectClose:
    def test_on_connect_sets_running(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        mock_ws = MagicMock()
        ws._on_connect(mock_ws, {})
        assert ws._running is True

    def test_on_close_clears_running(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        ws._running = True
        ws._on_close(MagicMock(), 1000, "normal")
        assert ws._running is False

    def test_on_connect_calls_user_callback(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        called = []
        ws.on_connect_callback = lambda: called.append(True)
        ws._on_connect(MagicMock(), {})
        assert len(called) == 1

    def test_on_close_calls_user_callback(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        closed = []
        ws.on_close_callback = lambda code, reason: closed.append((code, reason))
        ws._on_close(MagicMock(), 1000, "done")
        assert closed == [(1000, "done")]


# ---------------------------------------------------------------------------
# Test: start requires kiteconnect
# ---------------------------------------------------------------------------

class TestStart:
    def test_start_without_credentials_raises(self):
        ws = zerodha_websocket.ZerodhaWebSocket(api_key="", access_token="")
        with pytest.raises((ValueError, ImportError)):
            ws.start(threaded=False)


# ---------------------------------------------------------------------------
# Test: stop
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_clears_state(self):
        ws = zerodha_websocket.ZerodhaWebSocket()
        ws._running = True
        ws._kws = MagicMock()
        ws.stop()
        assert ws._running is False
        assert ws._kws is None


# ---------------------------------------------------------------------------
# Test: create_default_websocket
# ---------------------------------------------------------------------------

class TestCreateDefault:
    def test_uses_env_vars(self, monkeypatch):
        monkeypatch.setenv("ZERODHA_API_KEY", "env_key")
        monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", "env_token")
        ws = zerodha_websocket.create_default_websocket()
        assert ws.api_key == "env_key"
        assert ws.access_token == "env_token"

    def test_empty_without_env(self, monkeypatch):
        monkeypatch.delenv("ZERODHA_API_KEY", raising=False)
        monkeypatch.delenv("ZERODHA_ACCESS_TOKEN", raising=False)
        ws = zerodha_websocket.create_default_websocket()
        assert ws.api_key == ""


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestWebSocketIntegration:
    def test_subscribe_and_receive(self):
        """End-to-end: subscribe → receive ticks → verify count."""
        ws = zerodha_websocket.ZerodhaWebSocket()
        tokens = {"RELIANCE": 1, "TCS": 2, "INFY": 3}
        ws.subscribe(["RELIANCE", "TCS", "INFY"], tokens=tokens)

        assert len(ws._instrument_tokens) == 3

        # Simulate ticks
        received = []
        ws.on_tick_callback = lambda ticks: received.extend(ticks)
        ws._on_ticks(MagicMock(), [
            {"instrument_token": 1, "last_price": 2500.0, "ohlc": {}},
            {"instrument_token": 2, "last_price": 3800.0, "ohlc": {}},
        ])

        assert ws.tick_count == 2
        assert len(received) == 2
        assert received[0].tradingsymbol == "RELIANCE"
        assert received[1].tradingsymbol == "TCS"
