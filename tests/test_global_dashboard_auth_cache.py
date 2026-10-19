"""
Tests for the global_portfolio_dashboard positions-cache helpers.
=================================================================
These guards ensure:
  1. _status_has_auth_error() correctly identifies auth-failure strings
  2. _load_positions_cache() rejects stale or auth-error-poisoned caches
  3. _save_positions_cache() never writes auth-error states to disk

Bug history: Without these guards, a failed Zerodha auth would write
"Login skipped" into the cache, and every subsequent run would load stale
data and never trigger a re-authentication (cache poisoning).
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import global_portfolio_dashboard as G


# ══════════════════════════════════════════════════════════════════════════════
# _status_has_auth_error() — sentinel detection
# ══════════════════════════════════════════════════════════════════════════════

class TestStatusHasAuthError:
    """Verify the auth-error sentinel detection covers all known failure messages."""

    def test_empty_statuses_is_not_auth_error(self):
        assert G._status_has_auth_error({}) is False

    def test_none_is_not_auth_error(self):
        assert G._status_has_auth_error(None) is False

    def test_all_ok_statuses_are_not_auth_error(self):
        statuses = {
            "Zerodha": "OK — 12 holdings",
            "IBKR":    "OK — 5 positions",
            "Moomoo":  "OK",
        }
        assert G._status_has_auth_error(statuses) is False

    def test_insufficient_permission_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "Insufficient permission"}) is True

    def test_tokenexception_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "TokenException: Invalid token"}) is True

    def test_permissionexception_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "PermissionException: invalid access"}) is True

    def test_token_expired_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "Token expired"}) is True

    def test_incorrect_access_token_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "Incorrect access_token"}) is True

    def test_login_skipped_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "Login skipped — token unavailable"}) is True

    def test_holdings_missing_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "Holdings missing"}) is True

    def test_login_failed_is_auth_error(self):
        assert G._status_has_auth_error({"Zerodha": "Login failed"}) is True

    def test_auth_failed_is_auth_error(self):
        assert G._status_has_auth_error({"IBKR": "Auth failed"}) is True

    def test_case_insensitive_detection(self):
        assert G._status_has_auth_error({"Zerodha": "TOKEN EXPIRED"}) is True
        assert G._status_has_auth_error({"Zerodha": "PERMISSIONEXCEPTION: xyz"}) is True

    def test_only_one_bad_broker_triggers(self):
        statuses = {
            "AngelOne": "OK — 8 holdings",
            "Zerodha":  "TokenException: Invalid",
            "IBKR":     "OK — 3 positions",
        }
        assert G._status_has_auth_error(statuses) is True

    def test_error_message_without_auth_keyword(self):
        """Network or timeout errors are NOT auth errors."""
        statuses = {"Moomoo": "Error: connection refused", "IBKR": "Timeout after 30s"}
        assert G._status_has_auth_error(statuses) is False


# ══════════════════════════════════════════════════════════════════════════════
# _load_positions_cache() — rejection logic
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadPositionsCache:
    """_load_positions_cache() must reject absent / stale / auth-poisoned caches."""

    def test_returns_none_none_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", tmp_path / "cache.json")
        positions, statuses = G._load_positions_cache()
        assert positions is None
        assert statuses is None

    def test_returns_data_when_cache_is_fresh(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        cache_file.write_text(json.dumps({
            "saved_at":  time.time(),
            "positions": [{"symbol": "TEST"}],
            "statuses":  {"IBKR": "OK"},
        }), encoding="utf-8")
        positions, statuses = G._load_positions_cache()
        assert positions == [{"symbol": "TEST"}]
        assert statuses == {"IBKR": "OK"}

    def test_rejects_cache_older_than_1_hour(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        cache_file.write_text(json.dumps({
            "saved_at":  time.time() - 3601,
            "positions": [{"symbol": "TEST"}],
            "statuses":  {"IBKR": "OK"},
        }), encoding="utf-8")
        positions, statuses = G._load_positions_cache()
        assert positions is None

    def test_rejects_auth_error_poisoned_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        cache_file.write_text(json.dumps({
            "saved_at":  time.time(),
            "positions": [{"symbol": "TEST"}],
            "statuses":  {"Zerodha": "Login skipped — token unavailable"},
        }), encoding="utf-8")
        positions, statuses = G._load_positions_cache()
        assert positions is None, (
            "_load_positions_cache() must reject caches containing auth-error statuses. "
            "A 'Login skipped' status means Zerodha data is missing; loading this cache "
            "would silently suppress the missing holdings indefinitely."
        )

    def test_handles_malformed_json_gracefully(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        cache_file.write_text("{ NOT VALID JSON }", encoding="utf-8")
        positions, statuses = G._load_positions_cache()
        assert positions is None
        assert statuses is None


# ══════════════════════════════════════════════════════════════════════════════
# _save_positions_cache() — never save auth-error states
# ══════════════════════════════════════════════════════════════════════════════

class TestSavePositionsCache:
    """_save_positions_cache() must refuse to write auth-error states."""

    def test_saves_clean_statuses(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        G._save_positions_cache([{"symbol": "TEST"}], {"IBKR": "OK"})
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert data["positions"] == [{"symbol": "TEST"}]

    def test_skips_save_on_auth_error_statuses(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        G._save_positions_cache(
            [{"symbol": "TEST"}],
            {"Zerodha": "TokenException: Invalid token"}
        )
        assert not cache_file.exists(), (
            "_save_positions_cache() must NOT write to disk when statuses contain "
            "auth errors. Writing would poison the cache and suppress auth retries."
        )

    def test_existing_good_cache_not_overwritten_by_auth_error(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(G, "_POSITIONS_CACHE_FILE", cache_file)
        # Write good data first
        G._save_positions_cache([{"symbol": "GOOD"}], {"IBKR": "OK"})
        good_mtime = cache_file.stat().st_mtime
        # Now try to overwrite with bad auth status
        G._save_positions_cache([{"symbol": "BAD"}], {"Zerodha": "Login skipped"})
        # File should not have changed
        assert cache_file.stat().st_mtime == good_mtime, (
            "Cache file was modified when it should have been left intact."
        )
