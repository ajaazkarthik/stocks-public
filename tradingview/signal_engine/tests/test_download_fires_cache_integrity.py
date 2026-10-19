"""
Regression tests for download-cache integrity (review 2026-10-13, Issue 2).

BEFORE the fix: a TradingView response whose ``s`` field was not ``ok`` became
``[]``, the unconditional ``open(..., 'w')`` overwrote the populated cache with
empty ``all``/``win2h``/``win1h`` arrays, and the script exited 0 -- so the next
command produced an empty "completed" report instead of reporting an upstream
failure.

AFTER: HTTP failures, unsuccessful API responses, invalid JSON, and malformed
result structures raise ``DownloadError``; the CLI exits nonzero; the cache is
replaced atomically and only after validation; a valid empty response is a
documented, distinguishable empty result.

download_fires.py performs network calls and ``sys.exit()`` at module level, so
it cannot be imported. The pure helpers and the module-level download block are
extracted via AST and executed in an isolated namespace, matching the existing
``test_download_fires_cookie_auth.py`` pattern.

All tests are network-free.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

TV_DIR = Path(__file__).resolve().parents[2]
SCRIPT_PATH = TV_DIR / "tv_data" / "download_fires.py"

WIN_2H_ID = 2381963449
WIN_1H_ID = 2381961922


class _ExitCalled(Exception):
    def __init__(self, code):
        super().__init__(f"sys.exit({code})")
        self.code = code


class _FakeSys:
    def exit(self, code=0):
        raise _ExitCalled(code)


def _extract_defs(tree):
    names = {"DownloadError", "_validate_fire_records",
             "_validate_fires_response", "_publish_fires_cache"}
    return [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names
    ]


def _extract_download_block(tree):
    """Return the module-level statements from the list_fires Try through the
    ledger Try (win2h/win1h derivation, atomic publish, ledger recording)."""
    start = end = None
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.Try) and "list_fires" in ast.unparse(node):
            start = i
        if isinstance(node, ast.Try) and "record_fires" in ast.unparse(node):
            end = i
    assert start is not None, "list_fires download block not found"
    assert end is not None, "ledger recording block not found"
    return tree.body[start:end + 1]


def _run_download_block(tmp_path, list_fires_factory, ledger_calls, monkeypatch,
                        payload=None):
    """Execute the real production download block with controlled seams.

    ``list_fires_factory`` receives the namespace's own ``DownloadError`` class
    (so a raised error is caught by the block's ``except DownloadError``) and
    returns the ``list_fires`` callable to install.

    When ``payload`` is given, ``list_fires`` runs the namespace's REAL
    validator over that raw response body instead, so malformed-record handling
    is exercised end to end with the matching exception class.
    """
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    defs = _extract_defs(tree)
    block = _extract_download_block(tree)

    ledger_mod = ModuleType("database.fires_ledger")
    ledger_mod.count = lambda: 0
    ledger_mod.record_fires = lambda fires, source="download": (
        ledger_calls.append(list(fires)) or len(fires)
    )
    database_pkg = ModuleType("database")
    monkeypatch.setitem(sys.modules, "database", database_pkg)
    monkeypatch.setitem(sys.modules, "database.fires_ledger", ledger_mod)

    ns = {
        "json": json,
        "os": os,
        "Path": Path,
        "ROOT": tmp_path,
        "WIN_2H_ID": WIN_2H_ID,
        "WIN_1H_ID": WIN_1H_ID,
        "sys": _FakeSys(),
        "print": lambda *a, **k: None,
    }
    exec(compile(ast.Module(body=defs, type_ignores=[]), "download_fires", "exec"), ns)
    if payload is not None:
        _validate = ns["_validate_fires_response"]
        ns["list_fires"] = lambda params=None: _validate(200, json.dumps(payload))
    else:
        ns["list_fires"] = list_fires_factory(ns["DownloadError"])
    exec(compile(ast.Module(body=block, type_ignores=[]), "download_fires", "exec"), ns)
    return ns


def _populated_cache(tmp_path):
    cache = tmp_path / "fires_raw.json"
    payload = {
        "all": [{"fire_id": 1, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:A"}],
        "win2h": [{"fire_id": 1, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:A"}],
        "win1h": [],
    }
    cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return cache


def _validate_with(DownloadError, payload):
    """Run the real validator over *payload* using the namespace's own
    ``DownloadError`` so the raised class matches the block's except clause."""
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    ns = {"json": json}
    exec(compile(ast.Module(body=_extract_defs(tree), type_ignores=[]),
                 "download_fires", "exec"), ns)
    assert ns["DownloadError"] is not None
    return ns["_validate_fires_response"](200, json.dumps(payload))


MALFORMED_EMPTY_RECORD = {"s": "ok", "r": [{}]}
MALFORMED_NO_ALERT_ID = {"s": "ok", "r": [{"pro_symbol": "NASDAQ:A"}]}


# ─── Pure validator ───────────────────────────────────────────────────────────

class TestValidateFiresResponse:

    def _validate(self):
        tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
        ns = {"json": json}
        exec(compile(ast.Module(body=_extract_defs(tree), type_ignores=[]),
                     "download_fires", "exec"), ns)
        return ns["_validate_fires_response"], ns["DownloadError"]

    def test_http_error_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(500, "{}")

    def test_invalid_json_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, "<html>not json</html>")

    def test_unsuccessful_status_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, json.dumps({"s": "error", "r": []}))

    def test_malformed_result_structure_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, json.dumps({"s": "ok", "r": {"not": "a list"}}))

    def test_non_object_payload_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, json.dumps([1, 2, 3]))

    def test_valid_empty_result_returns_empty_list(self):
        validate, _ = self._validate()
        assert validate(200, json.dumps({"s": "ok", "r": []})) == []

    def test_valid_nonempty_result_returns_fires(self):
        validate, _ = self._validate()
        fires = [{"fire_id": 1, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:A"}]
        assert validate(200, json.dumps({"s": "ok", "r": fires})) == fires

    def test_empty_object_record_raises(self):
        """Issue 4: ``{"s":"ok","r":[{}]}`` must not pass validation."""
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, json.dumps({"s": "ok", "r": [{}]}))

    def test_record_missing_alert_id_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, json.dumps({"s": "ok", "r": [{"pro_symbol": "NASDAQ:A"}]}))

    def test_record_missing_symbol_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, json.dumps({"s": "ok", "r": [{"alert_id": WIN_2H_ID}]}))

    def test_non_object_record_raises(self):
        validate, DownloadError = self._validate()
        with pytest.raises(DownloadError):
            validate(200, json.dumps({"s": "ok", "r": ["not-a-record"]}))

    def test_one_bad_record_among_good_ones_raises(self):
        validate, DownloadError = self._validate()
        fires = [
            {"fire_id": 1, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:A"},
            {},
        ]
        with pytest.raises(DownloadError):
            validate(200, json.dumps({"s": "ok", "r": fires}))

    def test_symbol_key_accepted_as_alternative(self):
        validate, _ = self._validate()
        fires = [{"fire_id": 1, "alert_id": WIN_2H_ID, "symbol": "NASDAQ:A"}]
        assert validate(200, json.dumps({"s": "ok", "r": fires})) == fires


# ─── Cache preservation on failure ────────────────────────────────────────────

class TestFailurePreservesCache:

    def test_api_error_leaves_cache_byte_for_byte_unchanged(self, tmp_path, monkeypatch):
        cache = _populated_cache(tmp_path)
        before = cache.read_bytes()

        def factory(DownloadError):
            def failing(params=None):
                raise DownloadError("unsuccessful /list_fires response (s='error')")
            return failing

        with pytest.raises(_ExitCalled) as exc:
            _run_download_block(tmp_path, factory, [], monkeypatch)
        assert exc.value.code == 1
        assert cache.read_bytes() == before

    @pytest.mark.parametrize("kind", ["http", "timeout", "invalid_json", "malformed"])
    def test_each_failure_kind_preserves_cache(self, tmp_path, monkeypatch, kind):
        cache = _populated_cache(tmp_path)
        before = cache.read_bytes()

        def factory(DownloadError):
            def failing(params=None):
                raise DownloadError(f"simulated {kind} failure")
            return failing

        with pytest.raises(_ExitCalled):
            _run_download_block(tmp_path, factory, [], monkeypatch)
        assert cache.read_bytes() == before

    def test_failure_produces_nonzero_exit(self, tmp_path, monkeypatch):
        _populated_cache(tmp_path)

        def factory(DownloadError):
            def failing(params=None):
                raise DownloadError("boom")
            return failing

        with pytest.raises(_ExitCalled) as exc:
            _run_download_block(tmp_path, factory, [], monkeypatch)
        assert exc.value.code != 0

    def test_failed_download_does_not_record_ledger(self, tmp_path, monkeypatch):
        _populated_cache(tmp_path)
        ledger_calls = []

        def factory(DownloadError):
            def failing(params=None):
                raise DownloadError("boom")
            return failing

        with pytest.raises(_ExitCalled):
            _run_download_block(tmp_path, factory, ledger_calls, monkeypatch)
        assert ledger_calls == []

    def test_failure_before_replacement_leaves_previous_cache_intact(self, tmp_path, monkeypatch):
        cache = _populated_cache(tmp_path)
        before = cache.read_bytes()

        def boom_replace(src, dst):
            raise OSError("simulated crash before atomic replace")

        monkeypatch.setattr(os, "replace", boom_replace)

        def factory(DownloadError):
            return lambda params=None: [
                {"fire_id": 2, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:B"}
            ]

        with pytest.raises(OSError):
            _run_download_block(tmp_path, factory, [], monkeypatch)
        assert cache.read_bytes() == before


# ─── Successful publication ───────────────────────────────────────────────────

class TestSuccessfulPublication:

    def test_valid_nonempty_response_publishes_all_three_arrays(self, tmp_path, monkeypatch):
        cache = tmp_path / "fires_raw.json"
        fires = [
            {"fire_id": 1, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:A"},
            {"fire_id": 2, "alert_id": WIN_1H_ID, "pro_symbol": "NASDAQ:B"},
        ]
        _run_download_block(tmp_path, lambda _e: (lambda params=None: fires), [], monkeypatch)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert set(payload) == {"all", "win2h", "win1h"}
        assert payload["all"] == fires
        assert payload["win2h"] == [fires[0]]
        assert payload["win1h"] == [fires[1]]

    def test_valid_empty_response_follows_documented_empty_policy(self, tmp_path, monkeypatch):
        cache = tmp_path / "fires_raw.json"
        ledger_calls = []
        _run_download_block(tmp_path, lambda _e: (lambda params=None: []), ledger_calls, monkeypatch)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert payload == {"all": [], "win2h": [], "win1h": []}
        # A valid empty result is recorded (it is a real, successful download).
        assert ledger_calls == [[]]

    def test_successful_publication_records_downloaded_fires(self, tmp_path, monkeypatch):
        fires = [{"fire_id": 1, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:A"}]
        ledger_calls = []
        _run_download_block(tmp_path, lambda _e: (lambda params=None: fires), ledger_calls, monkeypatch)
        assert ledger_calls == [fires]

    def test_no_temporary_file_left_behind(self, tmp_path, monkeypatch):
        fires = [{"fire_id": 1, "alert_id": WIN_2H_ID, "pro_symbol": "NASDAQ:A"}]
        _run_download_block(tmp_path, lambda _e: (lambda params=None: fires), [], monkeypatch)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


# ─── Malformed records must not replace a valid cache ─────────────────────────

class TestMalformedRecordsPreserveCache:
    """Issue 4: a structurally valid envelope with malformed records must not
    replace the previous cache, because downstream processing would then raise
    ``KeyError('alert_id')`` after the good cache was already gone."""

    def test_empty_object_record_preserves_cache(self, tmp_path, monkeypatch):
        cache = _populated_cache(tmp_path)
        before = cache.read_bytes()

        with pytest.raises(_ExitCalled) as exc:
            _run_download_block(tmp_path, None, [], monkeypatch,
                                payload=MALFORMED_EMPTY_RECORD)
        assert exc.value.code == 1
        assert cache.read_bytes() == before

    def test_record_missing_alert_id_preserves_cache(self, tmp_path, monkeypatch):
        cache = _populated_cache(tmp_path)
        before = cache.read_bytes()

        with pytest.raises(_ExitCalled):
            _run_download_block(tmp_path, None, [], monkeypatch,
                                payload=MALFORMED_NO_ALERT_ID)
        assert cache.read_bytes() == before

    def test_malformed_records_do_not_reach_ledger(self, tmp_path, monkeypatch):
        _populated_cache(tmp_path)
        ledger_calls = []

        with pytest.raises(_ExitCalled):
            _run_download_block(tmp_path, None, ledger_calls, monkeypatch,
                                payload=MALFORMED_EMPTY_RECORD)
        assert ledger_calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
