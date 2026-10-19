"""Offline acceptance tests for the committed option-14 handoff.

These tests never launch a subprocess.  They replace main.run with a controlled
orchestrator that writes only temporary run-scoped reports.  The production
parent validator remains active in every parent test.
"""

from __future__ import annotations

import importlib
import asyncio
import builtins
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import traceback
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest


RUN_ID = "acceptance-run-001"
UTC_TIMESTAMP = "2020-01-01T00:00:00Z"
MISSING = object()
ACCEPTANCE_ROOT = Path(os.environ["SIGNAL_STACK_ACCEPTANCE_ROOT"]).resolve()


class ForbiddenCall(AssertionError):
    """Raised when a prohibited external or trading path is reached."""


class SafetyBoundary:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path.resolve()
        assert self.tmp_path.is_relative_to(ACCEPTANCE_ROOT)
        self.reports = self.tmp_path / "reports"
        self.reports.mkdir()
        self.calls: Counter[str] = Counter()

    def guard(self, name: str):
        def blocked(*args, **kwargs):
            self.calls[name] += 1
            raise ForbiddenCall(
                f"PROHIBITED PATH REACHED: {name}\n"
                + "".join(traceback.format_stack())
            )

        return blocked

    def install(self, monkeypatch):
        """Install guards before importing production modules in each test."""
        monkeypatch.setattr(socket.socket, "connect", self.guard("socket.connect"))
        monkeypatch.setattr(socket.socket, "connect_ex", self.guard("socket.connect_ex"))
        monkeypatch.setattr(socket, "create_connection", self.guard("socket.create_connection"))
        monkeypatch.setattr(urllib.request, "urlopen", self.guard("urllib.request.urlopen"))
        monkeypatch.setattr(http.client.HTTPConnection, "connect", self.guard("HTTPConnection.connect"))
        monkeypatch.setattr(http.client.HTTPSConnection, "connect", self.guard("HTTPSConnection.connect"))
        monkeypatch.setattr(asyncio, "open_connection", self.guard("asyncio.open_connection"))
        monkeypatch.setattr(subprocess, "run", self.guard("subprocess.run"))
        monkeypatch.setattr(subprocess, "Popen", self.guard("subprocess.Popen"))
        monkeypatch.setattr(subprocess, "call", self.guard("subprocess.call"))
        monkeypatch.setattr(subprocess, "check_call", self.guard("subprocess.check_call"))
        monkeypatch.setattr(subprocess, "check_output", self.guard("subprocess.check_output"))
        monkeypatch.setitem(sys.modules, "moomoo", None)
        monkeypatch.setitem(sys.modules, "ib_insync", None)
        monkeypatch.setitem(sys.modules, "zerodha_login", None)
        monkeypatch.setitem(sys.modules, "angelone_login", None)
        monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", "")
        monkeypatch.delenv("ANGELONE_API_KEY", raising=False)
        monkeypatch.delenv("ANGELONE_CLIENT_CODE", raising=False)
        monkeypatch.delenv("ANGELONE_TOTP_SECRET", raising=False)

        original_write_text = Path.write_text
        original_write_bytes = Path.write_bytes
        original_open = builtins.open
        original_path_open = Path.open
        original_sqlite_connect = sqlite3.connect
        original_mkdir = Path.mkdir
        original_replace = os.replace

        def temporary_write(path, *args, **kwargs):
            resolved = Path(path).resolve()
            if not _is_test_infrastructure_path(resolved, self.tmp_path):
                self.calls["production.write_text"] += 1
                raise ForbiddenCall(f"PRODUCTION WRITE REACHED: {resolved}")
            return original_write_text(path, *args, **kwargs)

        def temporary_write_bytes(path, *args, **kwargs):
            resolved = Path(path).resolve()
            if not _is_test_infrastructure_path(resolved, self.tmp_path):
                self.calls["production.write_bytes"] += 1
                raise ForbiddenCall(f"PRODUCTION WRITE REACHED: {resolved}")
            return original_write_bytes(path, *args, **kwargs)

        def temporary_open(path, mode="r", *args, **kwargs):
            if isinstance(path, int) and _is_write_mode(mode):
                self.calls["builtins.open"] += 1
                raise ForbiddenCall(f"WRITE DESCRIPTOR REACHED: {path}")
            resolved = Path(path).resolve()
            if not _is_test_infrastructure_path(resolved, self.tmp_path) and _is_write_mode(mode):
                self.calls["builtins.open"] += 1
                raise ForbiddenCall(f"PRODUCTION OPEN WRITE REACHED: {resolved}")
            return original_open(path, mode, *args, **kwargs)

        def temporary_path_open(path, mode="r", *args, **kwargs):
            resolved = Path(path).resolve()
            if not _is_test_infrastructure_path(resolved, self.tmp_path) and _is_write_mode(mode):
                self.calls["pathlib.Path.open"] += 1
                raise ForbiddenCall(f"PRODUCTION PATH.OPEN WRITE REACHED: {resolved}")
            return original_path_open(path, mode, *args, **kwargs)

        def temporary_sqlite_connect(database, *args, **kwargs):
            if database != ":memory:":
                resolved = Path(database).resolve()
                if not _is_test_infrastructure_path(resolved, self.tmp_path):
                    self.calls["sqlite3.connect"] += 1
                    raise ForbiddenCall(f"PRODUCTION DATABASE CONNECTION REACHED: {resolved}")
            return original_sqlite_connect(database, *args, **kwargs)

        def temporary_replace(source, destination):
            resolved = Path(destination).resolve()
            if not resolved.is_relative_to(self.tmp_path):
                self.calls["production.os.replace"] += 1
                raise ForbiddenCall(f"PRODUCTION REPLACE REACHED: {resolved}")
            return original_replace(source, destination)

        def temporary_mkdir(path, *args, **kwargs):
            resolved = Path(path).resolve()
            if not _is_test_infrastructure_path(resolved, self.tmp_path) and not resolved.exists():
                self.calls["production.mkdir"] += 1
                raise ForbiddenCall(f"PRODUCTION DIRECTORY CREATE REACHED: {resolved}")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", temporary_write)
        monkeypatch.setattr(Path, "write_bytes", temporary_write_bytes)
        monkeypatch.setattr(builtins, "open", temporary_open)
        monkeypatch.setattr(Path, "open", temporary_path_open)
        monkeypatch.setattr(sqlite3, "connect", temporary_sqlite_connect)
        monkeypatch.setattr(Path, "mkdir", temporary_mkdir)
        monkeypatch.setattr(os, "replace", temporary_replace)

    def assert_clean(self):
        assert not self.calls, f"prohibited calls were reached: {self.calls}"


def _is_write_mode(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _is_test_infrastructure_path(path: Path, tmp_path: Path) -> bool:
    resolved = path.resolve()
    return resolved.is_relative_to(tmp_path)


@pytest.fixture
def safety(tmp_path, monkeypatch):
    boundary = SafetyBoundary(tmp_path)
    boundary.install(monkeypatch)
    try:
        yield boundary
    finally:
        boundary.assert_clean()


def _main(monkeypatch, safety):
    module = importlib.reload(importlib.import_module("main"))
    monkeypatch.setattr(module, "REPORTS", safety.reports)
    monkeypatch.setattr(module, "new_run_id", lambda: RUN_ID)
    monkeypatch.setattr(module, "_is_port_in_use", lambda port: False)
    monkeypatch.setattr(module, "_load_cached_token", lambda: None)
    monkeypatch.setattr(module, "_india_env", lambda **kwargs: module.BASE_ENV.copy())
    monkeypatch.setattr(module, "_signal_stack_win_rates", lambda *args, **kwargs: "offline")
    return module


def _strict_payload(run_id=RUN_ID, *, signals=None, watch=False):
    signals = ([{"symbol": "NASDAQ:TEST", "score": 88.5, "decision_score": 88.5}]
               if signals is None else signals)
    return {
        "status": "complete",
        "generated_at": UTC_TIMESTAMP,
        "run_id": run_id,
        "is_watch": watch,
        "n_actionable": 0 if watch else len(signals),
        "actionable_signals": [] if watch else signals,
    }


def _write_actionable(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run_parent(monkeypatch, safety, *, payload=None, options_rc=0,
                failed_script=None):
    module = _main(monkeypatch, safety)
    monkeypatch.setattr("builtins.input", lambda *args: "")
    calls = []
    payload = _strict_payload() if payload is None else payload

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        script = Path(command[1]).name if len(command) > 1 else ""
        if script == "download_fires.py":
            return 1 if failed_script == script else 0
        if script == "alert_tracker.py":
            return 2 if failed_script == script else 0
        if script == "signal_stack.py":
            if failed_script == script:
                return 3
            if payload is not MISSING:
                report_path = Path(command[command.index("--actionable-out") + 1])
                if isinstance(payload, str):
                    report_path.with_suffix(".json").write_text(payload, encoding="utf-8")
                else:
                    _write_actionable(report_path.with_suffix(".json"), payload)
            return 0
        if script == "run_options.py":
            if options_rc == 0:
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text("<html>temporary options report</html>", encoding="utf-8")
            return options_rc
        raise AssertionError(f"unexpected child command: {command}")

    monkeypatch.setattr(module, "run", fake_run)
    module.run_signal_stack()
    return module, calls


def test_safety_guard_self_test(safety):
    with pytest.raises(ForbiddenCall, match="socket.connect"):
        socket.socket().connect(("127.0.0.1", 9))
    assert safety.calls["socket.connect"] == 1

    with pytest.raises(ForbiddenCall, match="OPEN WRITE"):
        open(Path.cwd() / "reports" / "acceptance-forbidden.txt", "w", encoding="utf-8")
    assert safety.calls["builtins.open"] == 1

    with pytest.raises(ForbiddenCall, match="DATABASE CONNECTION"):
        sqlite3.connect(str(Path.cwd() / "database" / "acceptance-forbidden.db"))
    assert safety.calls["sqlite3.connect"] == 1
    safety.calls.clear()


def test_valid_candidates_use_real_parent_validator_and_current_paths(
    monkeypatch, safety, capsys
):
    module, calls = _run_parent(monkeypatch, safety)
    output = capsys.readouterr().out
    options = next(command for command, _ in calls if "run_options.py" in str(command))
    actionable = next(command for command, _ in calls if "signal_stack.py" in str(command))

    assert module._validate_actionable_report(
        safety.reports / f"actionable_report_{RUN_ID}.json", RUN_ID
    ) == (True, "valid", 1)
    assert "--strict-actionable" in options
    assert "--expected-run-id" in options
    assert any(f"options_report_{RUN_ID}" in str(value) for value in options)
    assert any(f"actionable_report_{RUN_ID}" in str(value) for value in actionable)
    assert f"actionable_report_{RUN_ID}.html" in output
    assert "F&O Options Analysis complete" in output
    safety.assert_clean()


def test_valid_empty_output_skips_options(monkeypatch, safety, capsys):
    _, calls = _run_parent(
        monkeypatch, safety,
        payload=_strict_payload(signals=[]),
    )
    output = capsys.readouterr().out
    assert not any("run_options.py" in str(command) for command, _ in calls)
    assert "skipping the F&O options analysis" in output
    safety.assert_clean()


def test_failed_options_run_reports_exit_code_and_ignores_old_report(
    monkeypatch, safety, capsys
):
    old_report = safety.reports / "options_report.html"
    old_report.write_text("old options report", encoding="utf-8")
    _, calls = _run_parent(monkeypatch, safety, options_rc=7)
    output = capsys.readouterr().out
    options_calls = [command for command, _ in calls if "run_options.py" in str(command)]

    assert len(options_calls) == 1
    assert "F&O options analysis failed with exit code 7." in output
    assert "F&O Options Analysis complete" not in output
    assert "options_report.html" not in output
    safety.assert_clean()


@pytest.mark.parametrize("failed_script, exit_code", [
    ("download_fires.py", 1),
    ("alert_tracker.py", 2),
    ("signal_stack.py", 3),
])
def test_each_upstream_failure_stops_before_options(
    monkeypatch, safety, capsys, failed_script, exit_code
):
    _, calls = _run_parent(
        monkeypatch, safety,
        failed_script=failed_script,
    )
    output = capsys.readouterr().out
    assert not any("run_options.py" in str(command) for command, _ in calls)
    assert str(exit_code) in output
    safety.assert_clean()


@pytest.mark.parametrize("payload, expected_message", [
    (None, "actionable report is missing"),
    ("{bad", "actionable report is malformed"),
    (_strict_payload(run_id="different-run"), "run_id does not match"),
    (_strict_payload(watch=True), "WATCH-only"),
])
def test_invalid_current_report_is_rejected_by_real_validator(
    monkeypatch, safety, capsys, payload, expected_message
):
    if payload is None:
        payload = MISSING
    module, calls = _run_parent(monkeypatch, safety, payload=payload)
    output = capsys.readouterr().out
    assert not any("run_options.py" in str(command) for command, _ in calls)
    assert expected_message in output
    safety.assert_clean()


def test_rejection_does_not_use_old_fallback(monkeypatch, safety):
    options = importlib.import_module("tradingview.options.run_options")
    current = safety.reports / "current.json"
    fallback = safety.reports / "actionable_log.json"
    fallback.write_text(json.dumps([{"symbol": "NASDAQ:OLD", "score": 99}]), encoding="utf-8")
    monkeypatch.setattr(options, "ACTIONABLE_LOG", fallback)
    monkeypatch.setattr(options, "ACTIONABLE_LOG_ALT", fallback)
    current.write_text("{bad", encoding="utf-8")
    fallback_loader = safety.guard("actionable fallback loader")
    monkeypatch.setattr(options, "_augment_from_fires", fallback_loader)

    with pytest.raises(options.StrictActionableReportError):
        options._load_actionable_signals(
            actionable_file=current,
            strict=True,
            expected_run_id=RUN_ID,
        )
    assert safety.calls["actionable fallback loader"] == 0
    safety.assert_clean()


def test_old_timestamp_is_not_rejected_when_run_id_matches(monkeypatch, safety):
    options = importlib.import_module("tradingview.options.run_options")
    current = safety.reports / "old-but-matching.json"
    _write_actionable(current, _strict_payload())
    report, reason = options._read_strict_actionable_report(current, RUN_ID)
    assert reason == "valid"
    parsed = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    assert report["run_id"] == RUN_ID
    safety.assert_clean()
