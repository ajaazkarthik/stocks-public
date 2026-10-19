"""
Zerodha WebSocket — Live tick streaming via KiteConnect Ticker.
===============================================================
Wraps kiteconnect.KiteTicker to stream live prices for Indian stocks.
Provides callbacks for on_tick, on_connect, on_close events, and
integrates with the signal pipeline for real-time price updates.

Usage:
    python india-brokers/zerodha_websocket.py --symbols RELIANCE,TCS,INFY
    python india-brokers/zerodha_websocket.py --watchlist india_watchlist.txt
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from path_config import INDIA
except ImportError:
    INDIA = _ROOT / "india-brokers"


@dataclass
class TickData:
    """Normalised tick data from KiteConnect WebSocket."""
    instrument_token: int
    tradingsymbol: str
    last_price: float
    volume: int = 0
    change: float = 0.0  # % change from open
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    timestamp: Optional[datetime] = None

    @classmethod
    def from_kite_tick(cls, tick: dict, symbol_map: dict[int, str]) -> "TickData":
        """Create TickData from a raw KiteConnect tick dict."""
        token = tick.get("instrument_token", 0)
        last = tick.get("last_price", 0.0)
        ohlc = tick.get("ohlc", {})
        return cls(
            instrument_token=token,
            tradingsymbol=symbol_map.get(token, str(token)),
            last_price=last,
            volume=tick.get("volume_traded", tick.get("volume", 0)),
            change=tick.get("change", 0.0),
            open_price=ohlc.get("open", 0.0),
            high_price=ohlc.get("high", 0.0),
            low_price=ohlc.get("low", 0.0),
            close_price=ohlc.get("close", 0.0),
            timestamp=tick.get("exchange_timestamp") or datetime.now(),
        )


@dataclass
class ZerodhaWebSocket:
    """
    Manages a KiteConnect WebSocket connection for live tick streaming.

    Usage:
        ws = ZerodhaWebSocket(api_key="...", access_token="...")
        ws.on_tick_callback = my_handler
        ws.subscribe(["RELIANCE", "TCS"])
        ws.start()
        ...
        ws.stop()
    """
    api_key: str = ""
    access_token: str = ""
    on_tick_callback: Optional[Callable[[list[TickData]], None]] = None
    on_connect_callback: Optional[Callable[[], None]] = None
    on_close_callback: Optional[Callable[[int, str], None]] = None
    on_error_callback: Optional[Callable[[str], None]] = None

    # Internal state
    _kws: object = field(default=None, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)
    _instrument_tokens: list[int] = field(default_factory=list, repr=False)
    _symbol_map: dict[int, str] = field(default_factory=dict, repr=False)
    _tick_count: int = field(default=0, repr=False)
    _last_tick_time: Optional[datetime] = field(default=None, repr=False)

    @property
    def is_connected(self) -> bool:
        return self._running and self._kws is not None

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def last_tick_time(self) -> Optional[datetime]:
        return self._last_tick_time

    def resolve_symbols(self, symbols: list[str]) -> dict[str, int]:
        """
        Resolve trading symbols to instrument tokens using KiteConnect instruments list.

        Returns dict of symbol → token.
        """
        try:
            from zerodha_auth import get_kite
            kite = get_kite()
            instruments = kite.instruments("NSE")

            token_map = {}
            for sym in symbols:
                for inst in instruments:
                    if inst["tradingsymbol"] == sym:
                        token_map[sym] = inst["instrument_token"]
                        break

            return token_map
        except Exception as e:
            if self.on_error_callback:
                self.on_error_callback(f"Failed to resolve symbols: {e}")
            return {}

    def subscribe(self, symbols: list[str], tokens: Optional[dict[str, int]] = None) -> None:
        """
        Subscribe to symbols. Resolves tokens if not provided.

        Args:
            symbols: List of trading symbols (e.g. ["RELIANCE", "TCS"])
            tokens: Optional pre-resolved symbol→token map
        """
        if tokens is None:
            tokens = self.resolve_symbols(symbols)

        self._instrument_tokens = list(tokens.values())
        self._symbol_map = {v: k for k, v in tokens.items()}

    def _on_ticks(self, ws: object, ticks: list[dict]) -> None:
        """Internal tick handler."""
        tick_data = [TickData.from_kite_tick(t, self._symbol_map) for t in ticks]
        self._tick_count += len(tick_data)
        self._last_tick_time = datetime.now()

        if self.on_tick_callback:
            try:
                self.on_tick_callback(tick_data)
            except Exception as e:
                if self.on_error_callback:
                    self.on_error_callback(f"Tick callback error: {e}")

    def _on_connect(self, ws: object, response: dict) -> None:
        """Internal connect handler."""
        self._running = True
        if self._instrument_tokens:
            ws.subscribe(self._instrument_tokens)
            ws.set_mode(ws.MODE_FULL, self._instrument_tokens)
        if self.on_connect_callback:
            self.on_connect_callback()

    def _on_close(self, ws: object, code: int, reason: str) -> None:
        """Internal close handler."""
        self._running = False
        if self.on_close_callback:
            self.on_close_callback(code, reason)

    def _on_error(self, ws: object, code: int, reason: str) -> None:
        """Internal error handler."""
        if self.on_error_callback:
            self.on_error_callback(f"WebSocket error {code}: {reason}")

    def start(self, threaded: bool = True) -> None:
        """
        Start the WebSocket connection.

        Args:
            threaded: If True, run in a background thread
        """
        try:
            from kiteconnect import KiteTicker
        except ImportError:
            raise ImportError("kiteconnect not installed. Run: pip install kiteconnect")

        api_key = self.api_key or os.environ.get("ZERODHA_API_KEY", "")
        access_token = self.access_token or os.environ.get("ZERODHA_ACCESS_TOKEN", "")

        if not api_key or not access_token:
            raise ValueError("api_key and access_token required")

        self._kws = KiteTicker(api_key, access_token)
        self._kws.on_ticks = self._on_ticks
        self._kws.on_connect = self._on_connect
        self._kws.on_close = self._on_close
        self._kws.on_error = self._on_error

        if threaded:
            self._thread = threading.Thread(target=self._kws.connect, kwargs={"threaded": True}, daemon=True)
            self._thread.start()
        else:
            self._kws.connect()

    def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._kws:
            try:
                self._kws.close()
            except Exception:
                pass
        self._kws = None


def create_default_websocket() -> ZerodhaWebSocket:
    """Create a ZerodhaWebSocket with env vars."""
    return ZerodhaWebSocket(
        api_key=os.environ.get("ZERODHA_API_KEY", ""),
        access_token=os.environ.get("ZERODHA_ACCESS_TOKEN", ""),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Zerodha WebSocket Live Ticker")
    parser.add_argument("--symbols", help="Comma-separated symbols (e.g. RELIANCE,TCS)")
    parser.add_argument("--watchlist", help="File with one symbol per line")
    parser.add_argument("--duration", type=int, default=60, help="Streaming duration in seconds")
    args = parser.parse_args()

    symbols = []
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.watchlist:
        wl_path = Path(args.watchlist)
        if wl_path.exists():
            symbols = [s.strip() for s in wl_path.read_text().splitlines() if s.strip()]

    if not symbols:
        print("❌ No symbols provided. Use --symbols or --watchlist")
        return

    print(f"📡 Starting Zerodha WebSocket for {len(symbols)} symbols: {', '.join(symbols[:10])}")

    ticks_received = []

    def on_tick(ticks: list[TickData]) -> None:
        for t in ticks:
            ticks_received.append(t)
            print(f"  {t.tradingsymbol}: ₹{t.last_price:.2f} ({t.change:+.2f}%) vol={t.volume:,}")

    def on_connect() -> None:
        print("  ✅ Connected to Zerodha WebSocket")

    def on_close(code: int, reason: str) -> None:
        print(f"  ❌ Disconnected: {code} {reason}")

    ws = create_default_websocket()
    ws.on_tick_callback = on_tick
    ws.on_connect_callback = on_connect
    ws.on_close_callback = on_close
    ws.subscribe(symbols)
    ws.start(threaded=True)

    print(f"  Streaming for {args.duration}s...")
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        print("  Interrupted by user")
    finally:
        ws.stop()

    print(f"\n📊 Total ticks received: {len(ticks_received)}")


if __name__ == "__main__":
    main()
