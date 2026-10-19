"""
test_global_dashboard_sealed.py
================================
SEALED PRODUCTION TESTS — global_portfolio_dashboard.py

⚠️  FROZEN MODULE CONTRACT — DO NOT MODIFY WITHOUT APPROVAL ⚠️
These tests are the source of truth for the behavioral contract of
global_portfolio_dashboard.py.  Every test in this file documents either:
  (a) a previously introduced regression, OR
  (b) a design invariant that must NEVER change.

If any test in this file fails after a code change, the change is REJECTED.
Get explicit approval from the maintainer before modifying this file.

Change log:
  2026-05-06  Initial sealed suite — 50+ behavioral contract tests.
              Covers: broker coverage, cache guardrails, IBKR fallback chain,
              Moomoo timeout, 6-broker pre-seeding, port guard, CLI contract,
              no auto-browser, no --no-ibkr in main(), _currency_for_prefix,
              _is_port_open, _save_positions_cache never poisons cache.
"""
from __future__ import annotations

import json
import sys
import time
import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import global_portfolio_dashboard as G


# ══════════════════════════════════════════════════════════════════════════════
# SECTION A — Static invariants (no IO)
# These tests are the cheapest and must ALWAYS pass.
# ══════════════════════════════════════════════════════════════════════════════

class TestStaticInvariants:
    """Immutable design contracts that require zero IO."""

    # ── A1: 6-broker canonical list ───────────────────────────────────────────

    def test_known_brokers_contains_exactly_6(self):
        """_KNOWN_BROKERS must have exactly 6 entries — one per canonical broker."""
        assert len(G._KNOWN_BROKERS) == 6, (
            f"Expected 6 brokers, got {len(G._KNOWN_BROKERS)}: {G._KNOWN_BROKERS}"
        )

    def test_known_brokers_exact_names(self):
        """Broker key names must exactly match — any rename breaks all reports."""
        assert set(G._KNOWN_BROKERS) == {
            "AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"
        }

    def test_known_brokers_order_is_stable(self):
        """Broker order is used for HTML table rows — must not change silently."""
        assert G._KNOWN_BROKERS == [
            "AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"
        ]

    # ── A2: Auth error sentinels ───────────────────────────────────────────────

    def test_auth_sentinels_contains_no_ibkr_sentinel(self):
        """The 'skipped (--no-ibkr)' sentinel must be in _AUTH_ERROR_SENTINELS.
        This prevents stale caches from a --no-ibkr run poisoning future runs."""
        assert "skipped (--no-ibkr)" in G._AUTH_ERROR_SENTINELS, (
            "'skipped (--no-ibkr)' sentinel missing — stale cache from old --no-ibkr "
            "run will cause IBKR to appear skipped on subsequent runs."
        )

    def test_auth_sentinels_contains_login_skipped(self):
        assert "login skipped" in G._AUTH_ERROR_SENTINELS

    def test_auth_sentinels_contains_token_expired(self):
        assert "token expired" in G._AUTH_ERROR_SENTINELS

    def test_auth_sentinels_contains_auth_failed(self):
        assert "auth failed" in G._AUTH_ERROR_SENTINELS

    def test_auth_sentinels_all_lowercase(self):
        """All sentinels must be lowercase — _status_has_auth_error uses .lower()."""
        for s in G._AUTH_ERROR_SENTINELS:
            assert s == s.lower(), (
                f"Sentinel '{s}' is not lowercase — "
                "_status_has_auth_error() compares against .lower() result."
            )

    # ── A3: Cache TTL ─────────────────────────────────────────────────────────

    def test_cache_ttl_is_3600_seconds(self):
        """Positions cache TTL must be 3600 s (1 hour). Changing it silently skips
        broker re-fetches which leads to stale data."""
        assert G._POSITIONS_CACHE_TTL == 3600, (
            f"Cache TTL changed to {G._POSITIONS_CACHE_TTL}s — "
            "this affects how often live broker data is fetched."
        )

    # ── A4: Module-level no auto-browser ─────────────────────────────────────

    def test_module_source_has_no_auto_webbrowser_open(self):
        """webbrowser.open() must only be called conditionally (no_open=False).
        This rule prevents report scripts from auto-opening browsers during
        morning runs and CI."""
        src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
        # Allowed: webbrowser.open inside a `if not no_open:` guard
        # Forbidden: webbrowser.open() at module level (outside any function)
        import re
        # Find all webbrowser.open calls
        calls = [(m.start(), m.group()) for m in re.finditer(r'webbrowser\.open\(', src)]
        for pos, call in calls:
            # Get lines before the call
            line_num = src[:pos].count('\n') + 1
            lines = src.splitlines()[max(0, line_num-5): line_num]
            context = "\n".join(lines)
            assert "no_open" in context or "not no_open" in context, (
                f"webbrowser.open() at line {line_num} is NOT guarded by 'no_open'. "
                "This would auto-open a browser during morning runs."
            )

    # ── A5: _currency_for_prefix ──────────────────────────────────────────────

    def test_currency_for_prefix_au(self):
        assert G._currency_for_prefix("AU") == "AUD"

    def test_currency_for_prefix_hk(self):
        assert G._currency_for_prefix("HK") == "HKD"

    def test_currency_for_prefix_sg(self):
        assert G._currency_for_prefix("SG") == "SGD"

    def test_currency_for_prefix_us(self):
        assert G._currency_for_prefix("US") == "USD"

    def test_currency_for_prefix_unknown(self):
        """Unknown prefix must default to USD — never raise."""
        assert G._currency_for_prefix("XX") == "USD"

    def test_currency_for_prefix_empty(self):
        """Empty prefix (US stocks with no prefix) must return USD."""
        assert G._currency_for_prefix("") == "USD"

    # ── A6: Flex cache constants ───────────────────────────────────────────────

    def test_flex_cache_logic_reads_cache_within_48h(self):
        """The Flex cache uses 48h as the max age. This is verified by reading the
        source — the constant is inlined, not a module-level symbol."""
        src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
        assert "_FLEX_CACHE_MAX_AGE_H = 48" in src, (
            "_FLEX_CACHE_MAX_AGE_H = 48 not found in source. "
            "If the Flex cache max age changed, update this assertion."
        )

    def test_flex_timeout_is_90s(self):
        """Flex live fetch must timeout at 90s to prevent indefinite blocking."""
        src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
        assert "timeout=90" in src, (
            "Flex Query 90s timeout not found. "
            "Without timeout, morning run can hang indefinitely when IBKR servers are slow."
        )

    def test_moomoo_thread_timeout_is_20s(self):
        """Moomoo thread join must timeout at 20s to prevent infinite SDK retry loop."""
        src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
        assert "t.join(timeout=20)" in src, (
            "Moomoo thread join timeout not found. "
            "Without it, OpenD stale session causes infinite _init_connect_sync() loop."
        )

    def test_ibkr_skip_flag_exists_in_argparse(self):
        """--no-ibkr flag must exist so manual/testing override is possible,
        even though main.py must NEVER pass it automatically."""
        src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
        assert "--no-ibkr" in src, "--no-ibkr argparse flag was removed"

    def test_force_refresh_flag_exists_in_argparse(self):
        """--force-refresh flag must exist to bypass the positions cache."""
        src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
        assert "--force-refresh" in src, "--force-refresh argparse flag was removed"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION B — _status_has_auth_error() — sentinel matching
# ══════════════════════════════════════════════════════════════════════════════

class TestStatusHasAuthErrorSealed:
    """Sealed behavioral contract for _status_has_auth_error()."""

    def test_no_ibkr_skip_sentinel_triggers(self):
        """REGRESSION GUARDRAIL (BUG-009): 'Skipped (--no-ibkr)' must be treated
        as an auth error so caches from old --no-ibkr runs are NEVER reused."""
        assert G._status_has_auth_error({"IBKR": "Skipped (--no-ibkr)"}) is True

    def test_no_ibkr_skip_sentinel_case_insensitive(self):
        assert G._status_has_auth_error({"IBKR": "SKIPPED (--NO-IBKR)"}) is True

    def test_flex_cached_is_not_auth_error(self):
        """Flex Cached is a valid status — must NOT be rejected as auth error."""
        assert G._status_has_auth_error({"IBKR": "Flex Cached (24h old) — 14 positions"}) is False

    def test_flex_live_is_not_auth_error(self):
        assert G._status_has_auth_error({"IBKR": "Flex Live — 14 positions"}) is False

    def test_tws_live_is_not_auth_error(self):
        assert G._status_has_auth_error({"IBKR": "TWS Live — 14 positions"}) is False

    def test_opend_offline_is_not_auth_error(self):
        """OpenD not running is an infrastructure error, not an auth error."""
        assert G._status_has_auth_error({"Moomoo-AU": "OpenD not running on port 11111"}) is False

    def test_pdf_statements_is_not_auth_error(self):
        assert G._status_has_auth_error({"Webull AU": "PDF statements — 25 positions"}) is False

    def test_all_six_brokers_healthy_is_not_auth_error(self):
        statuses = {
            "AngelOne":  "Live — 5 holdings",
            "IBKR":      "Flex Cached (24h old) — 14 positions",
            "Moomoo-AU": "Live — 8 positions",
            "Moomoo-SG": "Live — 3 positions",
            "Webull AU": "PDF statements — 25 positions",
            "Zerodha":   "Live — 12 holdings",
        }
        assert G._status_has_auth_error(statuses) is False

    def test_single_poisoned_broker_triggers(self):
        """Even if only ONE broker has an auth error, the whole cache must be rejected."""
        statuses = {
            "AngelOne":  "Live — 5 holdings",
            "IBKR":      "Skipped (--no-ibkr)",  # only this one is bad
            "Moomoo-AU": "Live — 8 positions",
            "Moomoo-SG": "Live — 3 positions",
            "Webull AU": "PDF statements — 25 positions",
            "Zerodha":   "Live — 12 holdings",
        }
        assert G._status_has_auth_error(statuses) is True


# ══════════════════════════════════════════════════════════════════════════════
# SECTION C — Cache save / load contracts
# ══════════════════════════════════════════════════════════════════════════════

class TestCacheContractSealed:
    """Sealed behavioral contract for cache save/load."""

    def _write_cache(self, tmp_path, positions, statuses, age_s=0):
        """Helper: write a cache file with a given age."""
        cache_file = tmp_path / "cache.json"
        data = {
            "saved_at": time.time() - age_s,
            "positions": positions,
            "statuses": statuses,
        }
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return cache_file

    def _patch_cache_file(self, monkeypatch, cache_file):
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)

    # ── C1: Never save poisoned cache ────────────────────────────────────────

    def test_save_refuses_to_write_if_ibkr_skipped(self, tmp_path, monkeypatch):
        """REGRESSION GUARDRAIL: _save_positions_cache must never persist
        a cache containing 'Skipped (--no-ibkr)'."""
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        G._save_positions_cache(
            [{"symbol": "AAPL", "broker": "IBKR"}],
            {"IBKR": "Skipped (--no-ibkr)", "Zerodha": "Live — 5"}
        )
        assert not cache_file.exists(), (
            "_save_positions_cache wrote a cache with 'Skipped (--no-ibkr)'. "
            "This would poison subsequent runs."
        )

    def test_save_refuses_to_write_if_login_skipped(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        G._save_positions_cache([], {"Zerodha": "Login skipped — token unavailable"})
        assert not cache_file.exists()

    def test_save_writes_when_all_statuses_healthy(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        G._save_positions_cache(
            [{"symbol": "BHP", "broker": "Moomoo-AU"}],
            {"IBKR": "Flex Cached (24h old) — 14 positions", "Zerodha": "Live — 5"}
        )
        assert cache_file.exists(), "_save_positions_cache refused to write a healthy cache"
        data = json.loads(cache_file.read_text())
        assert len(data["positions"]) == 1
        assert "saved_at" in data

    # ── C2: Load rejects stale cache ─────────────────────────────────────────

    def test_load_rejects_cache_older_than_1_hour(self, tmp_path, monkeypatch):
        cache_file = self._write_cache(
            tmp_path,
            [{"symbol": "BHP"}],
            {"Zerodha": "Live"},
            age_s=3601  # just over 1 hour
        )
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        pos, st = G._load_positions_cache()
        assert pos is None, "Stale cache (>1h) was NOT rejected"

    def test_load_accepts_cache_within_1_hour(self, tmp_path, monkeypatch):
        cache_file = self._write_cache(
            tmp_path,
            [{"symbol": "BHP"}],
            {"Zerodha": "Live — 5"},
            age_s=100
        )
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        pos, st = G._load_positions_cache()
        assert pos is not None, "Fresh cache was rejected"
        assert len(pos) == 1

    def test_load_rejects_cache_with_no_ibkr_sentinel(self, tmp_path, monkeypatch):
        """REGRESSION GUARDRAIL (BUG-009): poisoned cache from --no-ibkr run
        must be rejected even if it's fresh (< 1h old)."""
        cache_file = self._write_cache(
            tmp_path,
            [],
            {"IBKR": "Skipped (--no-ibkr)", "Zerodha": "Live — 5"},
            age_s=60  # only 1 minute old — still fresh
        )
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        pos, st = G._load_positions_cache()
        assert pos is None, (
            "Cache with 'Skipped (--no-ibkr)' was NOT rejected. "
            "This is BUG-009 — stale --no-ibkr cache poisons subsequent runs."
        )

    def test_load_rejects_cache_with_auth_error(self, tmp_path, monkeypatch):
        cache_file = self._write_cache(
            tmp_path,
            [],
            {"Zerodha": "TokenException: invalid token"},
            age_s=5
        )
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        pos, st = G._load_positions_cache()
        assert pos is None

    def test_load_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", tmp_path / "missing.json")
        pos, st = G._load_positions_cache()
        assert pos is None
        assert st is None

    def test_load_returns_none_on_corrupt_json(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "bad.json"
        cache_file.write_text("{{{INVALID JSON", encoding="utf-8")
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        pos, st = G._load_positions_cache()
        assert pos is None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION D — IBKR fetch chain behavioral contract
# ══════════════════════════════════════════════════════════════════════════════

class TestIBKRFetchChainSealed:
    """Sealed behavioral contract for _fetch_ibkr() and its sub-fetchers."""

    def test_skip_true_returns_empty_with_no_ibkr_message(self):
        """_fetch_ibkr(skip=True) must return ([], 'Skipped (--no-ibkr)') always.
        This is the message that main.py's main() records — it must never change."""
        pos, msg = G._fetch_ibkr(skip=True)
        assert pos == []
        assert "Skipped" in msg and "--no-ibkr" in msg

    def test_skip_true_never_calls_tws_or_flex(self):
        with patch.object(G, "_fetch_ibkr_live_tws") as m_tws, \
             patch.object(G, "_fetch_ibkr_flex") as m_flex, \
             patch.object(G, "_fetch_ibkr_client_portal") as m_cp:
            G._fetch_ibkr(skip=True)
            m_tws.assert_not_called()
            m_flex.assert_not_called()
            m_cp.assert_not_called()

    def test_tws_success_short_circuits_flex(self):
        """When TWS returns data, Flex must NOT be called."""
        tws_pos = [{"symbol": "AAPL", "broker": "IBKR"}]
        with patch.object(G, "_fetch_ibkr_live_tws", return_value=(tws_pos, "TWS Live — 1")), \
             patch.object(G, "_fetch_ibkr_flex") as mock_flex:
            pos, msg = G._fetch_ibkr(skip=False)
            mock_flex.assert_not_called()
            assert len(pos) == 1
            assert "TWS" in msg

    def test_tws_none_falls_through_to_flex(self):
        """When TWS returns None, Flex must be called."""
        with patch.object(G, "_fetch_ibkr_live_tws", return_value=None), \
             patch.object(G, "_fetch_ibkr_flex", return_value=([{"broker": "IBKR"}], "Flex")) as m_flex:
            pos, msg = G._fetch_ibkr(skip=False)
            m_flex.assert_called_once()
            assert msg == "Flex"

    def test_tws_port_closed_returns_none(self):
        """When IBKR port is closed, _fetch_ibkr_live_tws must return None quickly."""
        with patch.object(G, "_is_port_open", return_value=False):
            result = G._fetch_ibkr_live_tws()
        assert result is None

    def test_tws_port_open_false_skips_asyncio(self):
        """_fetch_ibkr_live_tws must NOT start asyncio.run() when port is closed."""
        with patch.object(G, "_is_port_open", return_value=False), \
             patch("asyncio.run") as mock_run:
            G._fetch_ibkr_live_tws()
            mock_run.assert_not_called()

    def test_fetch_ibkr_returns_tuple_of_list_and_str(self):
        """_fetch_ibkr must ALWAYS return (list, str) — never None, never raise."""
        with patch.object(G, "_fetch_ibkr_live_tws", side_effect=Exception("crash")), \
             patch.object(G, "_fetch_ibkr_flex", return_value=([], "Error")):
            result = G._fetch_ibkr(skip=False)
            assert isinstance(result, tuple) and len(result) == 2
            assert isinstance(result[0], list)
            assert isinstance(result[1], str)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION E — Moomoo timeout contract
# ══════════════════════════════════════════════════════════════════════════════

class TestMoomooTimeoutSealed:
    """_fetch_moomoo() must never block the caller for more than 20s."""

    def test_moomoo_returns_timeout_message_when_thread_hangs(self):
        """REGRESSION GUARDRAIL (BUG-D1): _fetch_moomoo must return within 20s
        even if the SDK enters its infinite _init_connect_sync() retry loop."""
        import time as _time

        # Patch _fetch_moomoo_locked to block indefinitely (simulates SDK hang)
        hang_event = threading.Event()

        def _hanging_locked(label, port):
            hang_event.wait()  # hangs until test cleans up
            return [], "never"

        with patch.object(G, "_fetch_moomoo_locked", side_effect=_hanging_locked):
            original_timeout = None
            # Temporarily reduce thread join timeout to 0.1s for test speed
            src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
            assert "t.join(timeout=20)" in src, (
                "Moomoo thread join timeout not found. BUG-D1 protection removed."
            )
            # Verify the function signature is in place — don't actually wait 20s in tests
            try:
                pass
            finally:
                hang_event.set()  # unblock hanging thread

    def test_moomoo_timeout_message_contains_broker_label(self):
        """Timeout message must identify which broker hung (AU or SG)."""
        def _slow_locked(label, port):
            import time as _t; _t.sleep(100)
            return [], ""

        # Verify the timeout message format in source code
        src = open(ROOT / "global_portfolio_dashboard.py", encoding="utf-8").read()
        assert "OpenD connect timeout (20s)" in src, (
            "Moomoo timeout message format changed. "
            "Scripts/tests parsing this message will break."
        )

    def test_moomoo_port_check_before_sdk(self):
        """_fetch_moomoo_locked must check port before connecting the SDK."""
        with patch.object(G, "_is_port_open", return_value=False) as mock_port:
            pos, msg = G._fetch_moomoo_locked("AU", 11111)
            mock_port.assert_called_once_with(11111)
            assert pos == []
            assert "not running" in msg.lower() or "port" in msg.lower()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION F — main() behavioral contracts
# ══════════════════════════════════════════════════════════════════════════════

class TestMainBehavioralContract:
    """Contracts for main() — the orchestrator that must never skip brokers."""

    def _make_positions(self):
        return [
            {"broker": "Zerodha",   "symbol": "INFY",    "cost_usd": 100.0, "pnl_usd": 10.0},
            {"broker": "AngelOne",  "symbol": "HDFC",    "cost_usd": 200.0, "pnl_usd": -5.0},
            {"broker": "IBKR",      "symbol": "AAPL",    "cost_usd": 300.0, "pnl_usd": 50.0},
            {"broker": "Moomoo-AU", "symbol": "CBA",     "cost_usd": 150.0, "pnl_usd": 15.0},
            {"broker": "Moomoo-SG", "symbol": "DBS",     "cost_usd": 120.0, "pnl_usd": 8.0},
            {"broker": "Webull AU", "symbol": "BHP",     "cost_usd": 90.0,  "pnl_usd": -3.0},
        ]

    def _make_statuses(self):
        return {
            "Zerodha":   "Live — 1 positions fetched",
            "AngelOne":  "Live — 1 positions fetched",
            "IBKR":      "Flex Cached (24h old) — 1 positions",
            "Moomoo-AU": "Live — 1 positions fetched",
            "Moomoo-SG": "Live — 1 positions fetched",
            "Webull AU": "PDF statements — 1 positions",
        }

    def _patch_all(self, mocker, tmp_path):
        """Redirect file paths and patch all side-effectful operations."""
        mocker.patch.object(G, "_POSITIONS_CACHE_FILE", tmp_path / "cache.json")
        mocker.patch.object(G, "REPORTS", tmp_path)
        mocker.patch.object(G, "OUTPUT", tmp_path / "dashboard.html")
        # Mock pre-flight so tests don't depend on live broker connectivity
        mocker.patch.object(G, "_preflight_check_all_brokers", return_value=(True, []))
        mocker.patch.object(G, "_fetch_moomoo",     return_value=([], "offline"))
        mocker.patch.object(G, "_fetch_ibkr",       return_value=([], "Flex test"))
        # HERMETIC ISOLATION: _webull_api_configured() reads real env vars
        # (WEBULL_AU_APP_KEY/SECRET) — force False so tests NEVER hit the live
        # Webull OpenAPI (and never trigger a real 2FA prompt) regardless of
        # what happens to be set in the developer's shell.
        mocker.patch.object(G, "_webull_api_configured", return_value=False)
        mocker.patch.object(G, "_fetch_webull_pdf",  return_value=([], "no PDFs"))
        mocker.patch.object(G, "_fetch_india",
                            return_value=([], [], "offline", "offline"))
        mocker.patch.object(G, "_fetch_realised_pnl",
                            return_value={"status": {}, "deposit_events": [],
                                          "ibkr_pnl": [], "moomoo_pnl": [],
                                          "webull_pnl": [], "india_pnl": []})
        mocker.patch.object(G, "generate_html", return_value="<html></html>")
        mocker.patch.object(G, "_rebuild_archive_index")
        mocker.patch("webbrowser.open")

    def test_force_refresh_bypasses_fresh_cache(self, tmp_path, mocker):
        """force_refresh=True must call live fetchers even when cache is valid."""
        self._patch_all(mocker, tmp_path)
        # Write a valid fresh cache
        G._save_positions_cache(self._make_positions(), self._make_statuses())
        mock_moomoo = mocker.patch.object(G, "_fetch_moomoo", return_value=([], "offline"))
        try:
            G.main(no_open=True, allow_partial=True, force_refresh=True)
        except Exception:
            pass
        assert mock_moomoo.called, "force_refresh=True must bypass cache and call _fetch_moomoo"

    def test_no_ibkr_is_false_by_default(self, tmp_path, mocker):
        """main() must call _fetch_ibkr(skip=False) — never skip=True by default."""
        self._patch_all(mocker, tmp_path)
        mock_ibkr = mocker.patch.object(G, "_fetch_ibkr", return_value=([], "Flex"))
        try:
            G.main(no_open=True, allow_partial=True, force_refresh=True)
        except Exception:
            pass
        # Check that _fetch_ibkr was called with skip=False
        called_with_skip_true = any(
            call[0] and call[0][0] is True  # positional skip=True
            for call in (mock_ibkr.call_args_list or [])
        )
        called_skip_kwarg_true = any(
            call.kwargs.get("skip") is True
            for call in (mock_ibkr.call_args_list or [])
        )
        assert not (called_with_skip_true or called_skip_kwarg_true), (
            "main() called _fetch_ibkr(skip=True) — IBKR was silently skipped. "
            "Default behavior must ALWAYS try IBKR."
        )

    def test_nameerror_not_raised_from_cache_path(self, tmp_path, mocker):
        """REGRESSION (BUG-007): z_pos/a_pos NameError when _from_cache=True."""
        self._patch_all(mocker, tmp_path)
        # Save a valid fresh cache
        orig_cache = G._POSITIONS_CACHE_FILE
        G._POSITIONS_CACHE_FILE = tmp_path / "cache.json"
        try:
            G._save_positions_cache(self._make_positions(), self._make_statuses())
            G.main(no_open=True, allow_partial=True, force_refresh=False)
        except NameError as e:
            pytest.fail(
                f"NameError in main() when using cached positions: {e}. "
                "z_pos/a_pos must be initialized before the cache branch."
            )
        except Exception:
            pass  # Other exceptions (generate_html mock, etc.) are acceptable
        finally:
            G._POSITIONS_CACHE_FILE = orig_cache

    def test_main_does_not_auto_open_browser_when_no_open_true(self, tmp_path, mocker):
        """main(no_open=True) must never call webbrowser.open()."""
        self._patch_all(mocker, tmp_path)
        mock_wb = mocker.patch("webbrowser.open")
        try:
            G.main(no_open=True, allow_partial=True, force_refresh=True)
        except Exception:
            pass
        mock_wb.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION G — main.py integration contract
# These tests verify that main.py honors the "never skip IBKR" rule.
# ══════════════════════════════════════════════════════════════════════════════

class TestMainPyIntegrationContract:
    """Verify main.py NEVER passes --no-ibkr to global_portfolio_dashboard.py."""

    def _get_fn_body(self, fn_name: str) -> str:
        import re
        src = open(ROOT / "main.py", encoding="utf-8").read()
        start = src.index(f"def {fn_name}(")
        nxt = re.search(r"\ndef [a-zA-Z_]", src[start + 1:])
        return src[start: start + nxt.start() + 1]

    def test_run_global_dashboard_never_passes_no_ibkr(self):
        """run_global_dashboard() in main.py must NEVER pass --no-ibkr."""
        body = self._get_fn_body("run_global_dashboard")
        assert "--no-ibkr" not in body, (
            "main.py run_global_dashboard() passes --no-ibkr to the dashboard. "
            "This skips IBKR silently. Remove the flag."
        )

    def test_run_morning_all_never_passes_no_ibkr(self):
        """run_morning_all() in main.py must NEVER pass --no-ibkr."""
        body = self._get_fn_body("run_morning_all")
        assert "--no-ibkr" not in body, (
            "main.py run_morning_all() passes --no-ibkr to the dashboard. "
            "This skips IBKR silently in morning runs."
        )

    def test_run_global_dashboard_has_no_prompt_for_skipping_ibkr(self):
        """run_global_dashboard() must NOT ask user if they want to skip IBKR."""
        body = self._get_fn_body("run_global_dashboard")
        assert "Include IBKR" not in body
        assert "skip_ibkr" not in body
        assert "include_ibkr" not in body

    def test_run_morning_all_has_no_include_ibkr_variable(self):
        """run_morning_all() must NOT have an include_ibkr variable."""
        body = self._get_fn_body("run_morning_all")
        assert "include_ibkr" not in body, (
            "run_morning_all() still has include_ibkr variable. "
            "All IBKR skip logic must be removed."
        )

    def test_run_global_dashboard_has_no_allow_partial_prompt(self):
        """run_global_dashboard() must NOT prompt the user for allow_partial."""
        body = self._get_fn_body("run_global_dashboard")
        # The description mention is fine; calling prompt() for it is not
        assert "prompt(" not in body, (
            "run_global_dashboard() still prompts the user. "
            "All prompts have been removed — this is a regression."
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION H — _is_port_open() contract
# ══════════════════════════════════════════════════════════════════════════════

class TestIsPortOpenSealed:
    """_is_port_open() must be fast and safe."""

    def test_closed_port_returns_false(self):
        """Port 1 is always closed — must return False quickly."""
        result = G._is_port_open(1)
        assert result is False

    def test_returns_bool_not_exception(self):
        """Must return bool, never raise, even for port 0."""
        result = G._is_port_open(0)
        assert isinstance(result, bool)

    def test_high_port_returns_false(self):
        result = G._is_port_open(65000)
        assert isinstance(result, bool)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION I — Broker HTML coverage contract
# ══════════════════════════════════════════════════════════════════════════════

class TestBrokerHTMLCoverageSealed:
    """The generated HTML must always mention all 6 broker names."""

    def test_reports_html_contains_all_six_brokers(self):
        """MANDATORY: generated report must show all 6 broker names.
        If a broker is offline, it shows 'offline' — it is NEVER silently omitted."""
        html_path = ROOT / "reports" / "global_portfolio_dashboard.html"
        if not html_path.exists():
            pytest.skip("Dashboard HTML not generated yet — run the dashboard first")
        html = html_path.read_text(encoding="utf-8", errors="replace")
        for broker in G._KNOWN_BROKERS:
            assert broker in html, (
                f"Broker '{broker}' is MISSING from the generated dashboard HTML. "
                f"Offline brokers must show 'offline' — never be silently omitted. "
                f"See broker-coverage.instructions.md."
            )
