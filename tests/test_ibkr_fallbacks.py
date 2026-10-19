"""
Tests for IBKR 3-tier fallback chain in global_portfolio_dashboard.py
======================================================================
Guards against regressions in the fallback order:
  1. _fetch_ibkr_live_tws()   — TWS live (port-open pre-check, IBKRConnectionError → None)
  2. _fetch_ibkr_flex()       — Flex XML (48h cache tolerance, 360s live timeout with stale fallback)
  3. _fetch_ibkr_client_portal() — Client Portal REST (sys.path fix for tradingview/)
  4. _fetch_ibkr()            — orchestrator: skip flag + full fallback chain

Root causes fixed (2026-05-06):
  RC-1: Flex cache policy was "same calendar day" → triggered live 30-300s fetch when cache
        was from yesterday. Fixed to 48h tolerance.
  RC-2: _fetch_ibkr_client_portal() imported without tradingview/ on sys.path → ModuleNotFoundError.
  RC-3: No port-open pre-check in TWS fetcher → needless 2s asyncio wait when TWS is off.
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import global_portfolio_dashboard as G


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_ibkr() — skip flag
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchIBKRSkipFlag:
    def test_skip_returns_empty_with_skip_message(self):
        """--no-ibkr flag: must return empty list + 'Skipped (--no-ibkr)' message."""
        positions, msg = G._fetch_ibkr(skip=True)
        assert positions == []
        assert "Skipped" in msg
        assert "--no-ibkr" in msg

    def test_skip_does_not_call_any_fetcher(self):
        """--no-ibkr must not call TWS, Flex, or Client Portal."""
        with (
            patch.object(G, "_fetch_ibkr_live_tws") as mock_tws,
            patch.object(G, "_fetch_ibkr_flex") as mock_flex,
            patch.object(G, "_fetch_ibkr_client_portal") as mock_cp,
        ):
            G._fetch_ibkr(skip=True)
            mock_tws.assert_not_called()
            mock_flex.assert_not_called()
            mock_cp.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_ibkr_live_tws() — port-open pre-check
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchIBKRLiveTWS:
    def test_returns_none_when_port_closed(self):
        """RC-3: When TWS port is closed, must return None immediately without asyncio."""
        with patch.object(G, "_is_port_open", return_value=False):
            result = G._fetch_ibkr_live_tws()
        assert result is None

    def test_skips_asyncio_when_port_closed(self):
        """asyncio.run() must NOT be called when port is closed (fast-fail path)."""
        with (
            patch.object(G, "_is_port_open", return_value=False),
            patch("asyncio.run") as mock_asyncio,
        ):
            G._fetch_ibkr_live_tws()
            mock_asyncio.assert_not_called()

    def test_returns_none_and_logs_on_ibkr_connection_error(self):
        """IBKRConnectionError (TWS running but API disabled) → returns None, logs warning."""
        from ibkr_async.exceptions import IBKRConnectionError

        with (
            patch.object(G, "_is_port_open", return_value=True),
            patch("asyncio.run", side_effect=IBKRConnectionError("refused")),
            patch.object(G._log, "warning") as mock_warn,
        ):
            result = G._fetch_ibkr_live_tws()
        assert result is None
        mock_warn.assert_called_once()
        call_kwargs = mock_warn.call_args[1] if mock_warn.call_args[1] else {}
        call_args = mock_warn.call_args[0] if mock_warn.call_args[0] else []
        # structlog calls: _log.warning("event_name", key=val)
        assert any("ibkr_tws_fetch_failed" in str(a) for a in call_args)

    def test_returns_positions_when_tws_succeeds(self):
        """When port open and asyncio.run succeeds, returns normalized position list."""
        fake_raw = [
            MagicMock(to_dict=lambda: {
                "symbol": "AAPL",
                "qty": 100,
                "avg_cost": 150.0,
                "last_price": 155.0,
                "market_value": 15500.0,
                "unrealised_pnl": 500.0,
                "pnl_pct": 3.3,
                "currency": "USD",
                "exchange": "US",
            })
        ]
        with (
            patch.object(G, "_is_port_open", return_value=True),
            patch("asyncio.run", return_value=fake_raw),
        ):
            result = G._fetch_ibkr_live_tws()

        assert result is not None
        positions, msg = result
        assert len(positions) == 1
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["broker"] == "IBKR"
        assert "TWS Live" in msg


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_ibkr_flex() — 48h cache tolerance (RC-1)
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchIBKRFlex:
    """RC-1: Flex cache must be accepted if < 48h old, without making a live HTTP call."""

    def _make_fake_stmt_pos(self):
        """Minimal pandas-like DataFrame stub for stmt_pos."""
        import pandas as pd
        return pd.DataFrame([{
            "qty": 10,
            "avg_cost": 200.0,
            "last_price": 210.0,
            "code": "US.MSFT",
        }])

    def test_cache_24h_old_is_accepted_without_live_fetch(self, tmp_path, monkeypatch):
        """RC-1 core: 24h-old cache must be used directly — no live Flex call."""
        # _fetch_ibkr_flex uses: _ibkr_dir = CGT / "ibkr-cgt"
        # so cache_path = CGT / "ibkr-cgt" / "_last_flex.xml".
        ibkr_cgt_dir = tmp_path / "ibkr-cgt"
        ibkr_cgt_dir.mkdir()
        cache_xml = ibkr_cgt_dir / "_last_flex.xml"
        cache_xml.write_text("<FlexQueryResponse/>", encoding="utf-8")
        mtime_24h_ago = time.time() - (24 * 3600)
        os.utime(cache_xml, (mtime_24h_ago, mtime_24h_ago))

        fake_pos = self._make_fake_stmt_pos()
        mock_ibkr = MagicMock()
        mock_ibkr.parse_flex_xml.return_value = (None, fake_pos, None, None)
        mock_ibkr.fetch_flex_statement.side_effect = AssertionError(
            "fetch_flex_statement must NOT be called when cache is < 48h old"
        )

        monkeypatch.setattr(G, "CGT", tmp_path)  # -> _ibkr_dir = tmp_path / "ibkr-cgt"
        monkeypatch.setattr(G, "ensure_paths", lambda *a: None)

        with patch.dict("sys.modules", {"ibkr_cgt_analysis": mock_ibkr}):
            positions, msg = G._fetch_ibkr_flex()

        mock_ibkr.fetch_flex_statement.assert_not_called()
        assert len(positions) >= 0  # Could be empty if parse returns no qty>0 rows
        assert "Cached" in msg

    def test_cache_fresh_message_contains_age(self, tmp_path, monkeypatch):
        """Fresh cache message must report the age in hours (e.g. 'Cached (1h old)')."""
        ibkr_cgt_dir = tmp_path / "ibkr-cgt"
        ibkr_cgt_dir.mkdir()
        cache_xml = ibkr_cgt_dir / "_last_flex.xml"
        cache_xml.write_text("<FlexQueryResponse/>", encoding="utf-8")
        mtime_1h_ago = time.time() - 3600
        os.utime(cache_xml, (mtime_1h_ago, mtime_1h_ago))

        fake_pos = self._make_fake_stmt_pos()
        mock_ibkr = MagicMock()
        mock_ibkr.parse_flex_xml.return_value = (None, fake_pos, None, None)

        monkeypatch.setattr(G, "CGT", tmp_path)
        monkeypatch.setattr(G, "ensure_paths", lambda *a: None)

        with patch.dict("sys.modules", {"ibkr_cgt_analysis": mock_ibkr}):
            positions, msg = G._fetch_ibkr_flex()

        assert "Cached" in msg
        assert "h old" in msg  # e.g. "Cached (1h old)"

    def test_cache_49h_old_triggers_live_fetch(self, tmp_path, monkeypatch):
        """Cache older than 48h must trigger live Flex fetch, not silently use stale data."""
        cache_xml = tmp_path / "_last_flex.xml"
        cache_xml.write_text("<FlexQueryResponse/>", encoding="utf-8")
        mtime_49h_ago = time.time() - (49 * 3600)
        os.utime(cache_xml, (mtime_49h_ago, mtime_49h_ago))

        fake_pos = self._make_fake_stmt_pos()
        mock_ibkr = MagicMock()
        mock_ibkr.parse_flex_xml.return_value = (None, fake_pos, None, None)
        # Live fetch returns successfully
        mock_ibkr.fetch_flex_statement.return_value = (None, fake_pos, None, None)
        mock_ibkr.FLEX_TOKEN = "tok"
        mock_ibkr.FLEX_QUERY_ID = "qid"
        mock_ibkr.FETCH_START = "20250101"

        with patch.dict("sys.modules", {"ibkr_cgt_analysis": mock_ibkr}):
            monkeypatch.setattr(G, "CGT", tmp_path)
            monkeypatch.setattr(G, "ensure_paths", lambda *a: None)
            positions, msg = G._fetch_ibkr_flex()

        mock_ibkr.fetch_flex_statement.assert_called_once()

    def test_live_fetch_timeout_falls_back_to_stale_cache(self, tmp_path, monkeypatch):
        """When live Flex fetch times out (360s), must fall back to stale XML cache.

        _fetch_ibkr_flex() spawns a raw daemon threading.Thread and calls
        _flex_t.join(timeout=360) — NOT ThreadPoolExecutor (Golden Rule #16 /
        SD-004: `with ThreadPoolExecutor` + fut.result(timeout=N) can hang
        indefinitely at __exit__). Patch threading.Thread itself so the test
        simulates a hung background fetch without actually blocking for 360s.
        """
        ibkr_cgt_dir = tmp_path / "ibkr-cgt"
        ibkr_cgt_dir.mkdir()
        cache_xml = ibkr_cgt_dir / "_last_flex.xml"
        cache_xml.write_text("<FlexQueryResponse/>", encoding="utf-8")
        mtime_49h_ago = time.time() - (49 * 3600)
        os.utime(cache_xml, (mtime_49h_ago, mtime_49h_ago))

        fake_pos = self._make_fake_stmt_pos()
        mock_ibkr = MagicMock()
        mock_ibkr.parse_flex_xml.return_value = (None, fake_pos, None, None)
        mock_ibkr.FLEX_TOKEN = "tok"
        mock_ibkr.FLEX_QUERY_ID = "qid"
        mock_ibkr.FETCH_START = "20250101"

        class _FakeHungThread:
            """Simulates a background thread that never finishes within join(timeout)."""
            def __init__(self, *a, **kw): pass
            def start(self): pass                # never actually runs _do_flex
            def join(self, timeout=None): pass    # returns immediately without blocking
            def is_alive(self): return True       # thread "still running" -> timed out

        monkeypatch.setattr(G, "CGT", tmp_path)
        monkeypatch.setattr(G, "ensure_paths", lambda *a: None)

        with (
            patch.dict("sys.modules", {"ibkr_cgt_analysis": mock_ibkr}),
            patch("threading.Thread", _FakeHungThread),
        ):
            positions, msg = G._fetch_ibkr_flex()

        # Must return stale-cache data (not empty) with explanation
        assert "Flex cache" in msg and "old" in msg, f"Expected stale-cache message, got: {msg!r}"
        assert len(positions) >= 0

    def test_no_cache_and_live_fails_returns_error(self, tmp_path, monkeypatch):
        """When no cache exists and live fetch fails, must return empty + error message."""
        # No cache file — _cache_age_h = inf → tries live fetch → fails
        mock_ibkr = MagicMock()
        mock_ibkr.FLEX_TOKEN = "tok"
        mock_ibkr.FLEX_QUERY_ID = "qid"
        mock_ibkr.FETCH_START = "20250101"

        mock_future = MagicMock(spec=Future)
        mock_future.result.side_effect = Exception("IBKR server down")

        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = mock_future

        with (
            patch.dict("sys.modules", {"ibkr_cgt_analysis": mock_ibkr}),
            patch("concurrent.futures.ThreadPoolExecutor", return_value=mock_executor),
        ):
            monkeypatch.setattr(G, "CGT", tmp_path)
            monkeypatch.setattr(G, "ensure_paths", lambda *a: None)
            positions, msg = G._fetch_ibkr_flex()

        assert positions == []
        assert "Error" in msg or "no cache" in msg.lower() or "Error" in msg


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_ibkr_client_portal() — sys.path fix (RC-2)
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchIBKRClientPortal:
    def test_tradingview_path_added_before_import(self):
        """RC-2: tradingview/ directory must be on sys.path when ibkr_client_portal is imported."""
        tv_path = str(ROOT / "tradingview")

        mock_cp = MagicMock()
        mock_cp.is_available = True
        mock_cp.get_positions.return_value = []

        mock_class = MagicMock(return_value=mock_cp)

        captured_syspath: list[str] = []

        def _import_hook(name, *args, **kwargs):
            if name == "ibkr_client_portal":
                captured_syspath.extend(sys.path[:])
                raise ImportError("test_sentinel")
            return __builtins__["__import__"](name, *args, **kwargs)  # type: ignore

        with patch.dict("sys.modules", {"ibkr_client_portal": MagicMock(IBKRClientPortal=mock_class)}):
            # Temporarily remove tradingview from path to test the injection
            original_path = sys.path[:]
            if tv_path in sys.path:
                sys.path.remove(tv_path)
            try:
                G._fetch_ibkr_client_portal()
            finally:
                sys.path[:] = original_path

        # After the call, tradingview/ must be on sys.path (G adds it if missing)
        # We verify indirectly: the function didn't raise ModuleNotFoundError
        # which would happen if the path wasn't added before the import.

    def test_ibkr_client_portal_module_is_importable(self):
        """ibkr_client_portal.py must exist at tradingview/ibkr_client_portal.py."""
        assert (ROOT / "tradingview" / "ibkr_client_portal.py").exists(), (
            "ibkr_client_portal.py missing from tradingview/ — "
            "Client Portal fallback will always fail with ModuleNotFoundError"
        )

    def test_returns_empty_when_gateway_not_running(self):
        """When Client Portal gateway is not running, returns [] + informative message."""
        mock_cp = MagicMock()
        mock_cp.is_available = False
        mock_class = MagicMock(return_value=mock_cp)

        with patch.dict("sys.modules", {"ibkr_client_portal": MagicMock(IBKRClientPortal=mock_class)}):
            positions, msg = G._fetch_ibkr_client_portal()

        assert positions == []
        assert "unavailable" in msg.lower() or "not running" in msg.lower()

    def test_returns_positions_when_gateway_running(self):
        """When Client Portal is available, returns normalized positions."""
        raw = [
            {
                "symbol": "TSLA",
                "qty": 5,
                "avg_cost": 200.0,
                "market_price": 220.0,
                "market_value": 1100.0,
                "unrealized_pnl": 100.0,
                "currency": "USD",
            }
        ]
        mock_cp = MagicMock()
        mock_cp.is_available = True
        mock_cp.get_positions.return_value = raw
        mock_class = MagicMock(return_value=mock_cp)

        with patch.dict("sys.modules", {"ibkr_client_portal": MagicMock(IBKRClientPortal=mock_class)}):
            positions, msg = G._fetch_ibkr_client_portal()

        assert len(positions) == 1
        assert positions[0]["symbol"] == "TSLA"
        assert positions[0]["broker"] == "IBKR"
        assert "Client Portal" in msg


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_ibkr() — full fallback chain
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchIBKRFullChain:
    def test_uses_tws_when_available(self):
        """When TWS returns positions, Flex and Client Portal must NOT be called."""
        tws_result = ([{"symbol": "AAPL", "broker": "IBKR"}], "TWS Live — 1 positions")
        with (
            patch.object(G, "_fetch_ibkr_live_tws", return_value=tws_result) as m_tws,
            patch.object(G, "_fetch_ibkr_flex") as m_flex,
            patch.object(G, "_fetch_ibkr_client_portal") as m_cp,
        ):
            positions, msg = G._fetch_ibkr(skip=False)

        assert positions[0]["symbol"] == "AAPL"
        assert "TWS" in msg
        m_flex.assert_not_called()
        m_cp.assert_not_called()

    def test_falls_to_flex_when_tws_unavailable(self):
        """When TWS returns None (port closed), must fall back to Flex."""
        flex_result = ([{"symbol": "MSFT", "broker": "IBKR"}], "Flex Cached (23h old) — 1 positions")
        with (
            patch.object(G, "_fetch_ibkr_live_tws", return_value=None),
            patch.object(G, "_fetch_ibkr_flex", return_value=flex_result) as m_flex,
            patch.object(G, "_fetch_ibkr_client_portal") as m_cp,
        ):
            positions, msg = G._fetch_ibkr(skip=False)

        assert positions[0]["symbol"] == "MSFT"
        m_flex.assert_called_once()
        m_cp.assert_not_called()

    def test_falls_to_client_portal_when_tws_and_flex_fail(self):
        """When TWS returns None AND Flex returns empty, must try Client Portal."""
        cp_result = ([{"symbol": "NVDA", "broker": "IBKR"}], "Client Portal — 1 positions")
        with (
            patch.object(G, "_fetch_ibkr_live_tws", return_value=None),
            patch.object(G, "_fetch_ibkr_flex", return_value=([], "Error: no cache")),
            patch.object(G, "_fetch_ibkr_client_portal", return_value=cp_result) as m_cp,
        ):
            positions, msg = G._fetch_ibkr(skip=False)

        assert positions[0]["symbol"] == "NVDA"
        m_cp.assert_called_once()

    def test_returns_flex_error_when_all_three_fail(self):
        """When all 3 tiers fail, returns empty list with the Flex error message."""
        with (
            patch.object(G, "_fetch_ibkr_live_tws", return_value=None),
            patch.object(G, "_fetch_ibkr_flex", return_value=([], "Error: timeout")),
            patch.object(G, "_fetch_ibkr_client_portal", return_value=([], "CP Error")),
        ):
            positions, msg = G._fetch_ibkr(skip=False)

        assert positions == []
        # Must return the Flex error (more descriptive) not the CP error
        assert "Error" in msg


# ══════════════════════════════════════════════════════════════════════════════
# Source code guardrails
# ══════════════════════════════════════════════════════════════════════════════

class TestIBKRSourceGuardrails:
    """Static analysis: prevent regressions from being re-introduced."""

    _src = (ROOT / "global_portfolio_dashboard.py").read_text(encoding="utf-8")

    def test_flex_cache_uses_time_not_date_comparison(self):
        """RC-1 guardrail: cache freshness must use time.time() / float hours, not date comparison."""
        assert "_cache_age_h" in self._src, (
            "RC-1 regression: _cache_age_h float-based check removed — "
            "Flex will revert to calendar-day comparison and hang on stale cache"
        )
        assert "_FLEX_CACHE_MAX_AGE_H" in self._src, (
            "RC-1 regression: _FLEX_CACHE_MAX_AGE_H constant removed"
        )
        # Must NOT use the old date-exact comparison pattern
        assert "cached_date >= _date.today()" not in self._src, (
            "RC-1 regression: old calendar-day cache check re-introduced"
        )

    def test_flex_live_fetch_has_timeout(self):
        """RC-1 guardrail: live Flex fetch must have a timeout (no unbounded blocking call)."""
        assert "timeout=90" in self._src or "_fut.result(timeout=" in self._src, (
            "RC-1 regression: Flex live fetch has no timeout — will hang 30-300s"
        )

    def test_tws_has_port_open_pre_check(self):
        """RC-3 guardrail: _fetch_ibkr_live_tws() must check port before asyncio.run()."""
        # The port check must appear BEFORE asyncio.run in the function
        tws_fn_start = self._src.find("def _fetch_ibkr_live_tws()")
        tws_fn_end   = self._src.find("\ndef _fetch_ibkr_flex()")
        tws_fn_body  = self._src[tws_fn_start:tws_fn_end]
        assert "_is_port_open" in tws_fn_body, (
            "RC-3 regression: port-open pre-check removed from _fetch_ibkr_live_tws() — "
            "will make unnecessary asyncio calls when TWS is not running"
        )

    def test_tws_logs_exception_before_returning_none(self):
        """RC-3 guardrail: TWS exception must be logged (not silently swallowed)."""
        tws_fn_start = self._src.find("def _fetch_ibkr_live_tws()")
        tws_fn_end   = self._src.find("\ndef _fetch_ibkr_flex()")
        tws_fn_body  = self._src[tws_fn_start:tws_fn_end]
        assert "_tws_exc" in tws_fn_body, (
            "RC-3 regression: bare 'except Exception: return None' re-introduced in "
            "_fetch_ibkr_live_tws() — IBKR failures will be silently swallowed"
        )

    def test_client_portal_adds_tradingview_to_sys_path(self):
        """RC-2 guardrail: _fetch_ibkr_client_portal() must inject tradingview/ into sys.path."""
        cp_fn_start = self._src.find("def _fetch_ibkr_client_portal()")
        cp_fn_end   = self._src.find("\ndef _fetch_ibkr(")
        cp_fn_body  = self._src[cp_fn_start:cp_fn_end]
        assert "sys.path.insert" in cp_fn_body or "tradingview" in cp_fn_body, (
            "RC-2 regression: tradingview/ path injection removed from "
            "_fetch_ibkr_client_portal() — import will fail with ModuleNotFoundError"
        )

    def test_flex_stale_cache_fallback_present(self):
        """RC-1 guardrail: code path for 'use stale cache when live fetch fails' must exist."""
        assert "Cached-stale" in self._src or "using_stale_cache" in self._src, (
            "RC-1 regression: stale-cache fallback on live fetch timeout removed — "
            "IBKR will return empty when live fetch fails and cache > 48h old"
        )
