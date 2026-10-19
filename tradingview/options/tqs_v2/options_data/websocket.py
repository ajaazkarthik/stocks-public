"""
TQS v2 OPRA WebSocket Client
==============================
Fetches live bid/ask/bid_size/ask_size for OPRA option contracts
via TradingView WebSocket.

This is OPTIONAL enrichment — 15-minute delay (TV Pro subscription).
The orchestrator uses this if websocket is enabled in config.
If WebSocket fails, contracts proceed without this data.

Protocol: Custom TV WS framing: ~m~LENGTH~m~{json}
Auth: TV cookie (same session as REST endpoints)
Provider: ICE (Intercontinental Exchange)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import TQSv2Config
from ..exceptions import WebSocketError
from .models import RawContract

_log = logging.getLogger("tqs_v2.options_data.websocket")

# Regex to parse TV WS framing: ~m~LENGTH~m~PAYLOAD
_TV_FRAME_RE = re.compile(r"~m~\d+~m~(.+)")


def _tv_frame(msg: str) -> str:
    """Wrap a JSON string in TV WebSocket framing."""
    return f"~m~{len(msg)}~m~{msg}"


class TVOPRAWebSocket:
    """
    Subscribes to OPRA option quotes via TradingView WebSocket.

    Usage:
        ws = TVOPRAWebSocket()
        quotes = ws.fetch_quotes(opra_codes, config, timeout=30.0)
        # quotes: {opra_code: {bid, ask, bid_size, ask_size}}
    """

    def __init__(self) -> None:
        self._session_id = f"qs_{int(time.time())}"

    def fetch_quotes(
        self,
        opra_codes: list[str],
        config: TQSv2Config,
        timeout: float = 25.0,
    ) -> dict[str, dict]:
        """
        Fetch bid/ask quotes for the given OPRA codes.

        Returns dict: {opra_code: {bid, ask, bid_size, ask_size}}
        Returns partial results on partial failure.
        Returns {} if websocket is entirely unavailable.
        """
        if not opra_codes:
            return {}

        results: dict[str, dict] = {}
        error: list[str] = []

        def _run():
            try:
                import websocket as ws_lib
                self._connect_and_subscribe(
                    opra_codes=opra_codes,
                    results=results,
                    config=config,
                    timeout=timeout,
                )
            except ImportError:
                _log.debug("websocket-client not installed — OPRA WS skipped")
                error.append("import_error")
            except Exception as exc:
                _log.debug("OPRA WS error: %s", exc)
                error.append(str(exc))

        # Run in a thread with SD-004 pattern
        _ex_thread = threading.Thread(target=_run, daemon=True)
        _ex_thread.start()
        _ex_thread.join(timeout=timeout + 5)  # extra 5s buffer

        if error:
            _log.debug("OPRA WS not available: %s", error[0])

        return results

    def _connect_and_subscribe(
        self,
        opra_codes: list[str],
        results: dict[str, dict],
        config: TQSv2Config,
        timeout: float,
    ) -> None:
        """Internal: connect, subscribe, collect quotes up to timeout."""
        import websocket

        received_events: threading.Event = threading.Event()
        collected: dict[str, dict] = {}
        expected = set(opra_codes)
        stop_event = threading.Event()

        def on_message(ws, message):
            matches = _TV_FRAME_RE.findall(message)
            for payload_str in matches:
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue

                m = payload.get("m", "")
                p = payload.get("p", [])

                if m == "qsd" and len(p) >= 2:
                    # Quote update: p[0] = session, p[1] = {n: symbol, v: {fields}}
                    data = p[1]
                    sym = data.get("n", "")
                    if sym in expected:
                        v = data.get("v", {})
                        if "bid" in v or "ask" in v or "volume" in v:
                            existing = collected.get(sym, {})
                            existing.update({
                                k: v.get(k)
                                for k in ("bid", "ask", "bid_size", "ask_size", "volume")
                                if k in v
                            })
                            collected[sym] = existing
                            # TV WS sends volume in a separate qsd message after bid/ask.
                            # Wait until BOTH bid AND volume are present for every
                            # expected contract before stopping; the timeout handles
                            # contracts where volume never arrives.
                            if len(collected) >= len(expected) and all(
                                "bid" in collected.get(c, {})
                                and "volume" in collected.get(c, {})
                                for c in expected
                            ):
                                stop_event.set()

        def on_error(ws, error):
            _log.debug("WS error: %s", error)
            stop_event.set()

        def on_close(ws, *args):
            stop_event.set()

        def on_open(ws):
            # 1. Create quote session
            create_msg = json.dumps({
                "m": "quote_create_session",
                "p": [self._session_id],
            })
            ws.send(_tv_frame(create_msg))

            # 2. Subscribe to all OPRA codes
            for code in opra_codes:
                add_msg = json.dumps({
                    "m": "quote_add_symbols",
                    "p": [self._session_id, code],
                })
                ws.send(_tv_frame(add_msg))

        ws_app = websocket.WebSocketApp(
            config.tv_websocket_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        ws_thread = threading.Thread(
            target=ws_app.run_forever,
            kwargs={"ping_interval": 10},
            daemon=True,
        )
        ws_thread.start()

        # Wait for all quotes or timeout
        stop_event.wait(timeout=timeout)
        ws_app.close()

        results.update(collected)
        _log.debug("OPRA WS: collected %d/%d quotes", len(collected), len(opra_codes))


def enrich_contracts_with_ws(
    contracts: list[RawContract],
    config: TQSv2Config,
    timeout: float = 25.0,
) -> list[RawContract]:
    """
    Convenience function: fetch WS quotes for all contracts and attach results.
    Mutates ws_bid, ws_ask, ws_bid_size, ws_ask_size on each contract.
    Non-blocking on failure — returns contracts unchanged on error.
    """
    ws = TVOPRAWebSocket()
    opra_codes = [c.opra_code for c in contracts if c.opra_code]
    if not opra_codes:
        return contracts

    quotes = ws.fetch_quotes(opra_codes, config, timeout=timeout)

    for c in contracts:
        q = quotes.get(c.opra_code, {})
        if not q:
            continue
        # Use setattr for duck-typing — works on both RawContract and OptionContract
        # (neither is frozen=True, so direct attribute assignment is safe)
        ws_fields = {
            "ws_bid": q.get("bid"),
            "ws_ask": q.get("ask"),
            "ws_bid_size": q.get("bid_size"),
            "ws_ask_size": q.get("ask_size"),
            "ws_volume": q.get("volume"),  # last-session cached volume (fills scan2 None)
        }
        for attr, val in ws_fields.items():
            setattr(c, attr, val)

    return contracts
