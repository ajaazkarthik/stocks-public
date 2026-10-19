"""Collection-time safety plugin for offline Signal Stack acceptance tests.

Load with ``-p tests.acceptance_safety_plugin``.  It runs before acceptance
session fixtures, disables the repository report-stub writer, and blocks writes
to production paths and connections to non-temporary SQLite databases.
"""

from __future__ import annotations

import asyncio
import builtins
import http.client
import inspect
import os
import socket
import sqlite3
import subprocess
import sys
import traceback
import urllib.request
from urllib.parse import unquote
from collections import Counter
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
_ACCEPTANCE_ROOT = Path(os.environ["SIGNAL_STACK_ACCEPTANCE_ROOT"]).resolve()
if not _ACCEPTANCE_ROOT.exists():
    raise RuntimeError(f"SIGNAL_STACK_ACCEPTANCE_ROOT does not exist: {_ACCEPTANCE_ROOT}")
_STATE = Counter()
_ORIGINALS = {}
_INSTALLED = False
_STUBS_NEUTRALIZED = {}
_PATH_SETUP_NEUTRALIZED = False
_CLEANUP_DONE = False

# Patch path setup before pytest imports the repository conftest module.  That
# module calls ensure_paths at import time, before pytest fixtures exist.
try:
    import path_config as _path_config
except ImportError:
    _path_config = None
else:
    _ORIGINALS[(_path_config, "ensure_paths")] = _path_config.ensure_paths
    _path_config.ensure_paths = lambda *args, **kwargs: None
    _PATH_SETUP_NEUTRALIZED = True


def _production_path(value) -> Path | None:
    if value is None or value == ":memory:":
        return None
    try:
        raw = os.fspath(value)
        if isinstance(raw, str) and raw.startswith("file:"):
            raw = raw[5:].split("?", 1)[0]
            if raw.startswith("//"):
                raw = raw[2:]
            raw = unquote(raw)
        path = Path(raw).resolve()
    except (TypeError, ValueError):
        return None
    if path == _ACCEPTANCE_ROOT or _ACCEPTANCE_ROOT in path.parents:
        return None
    return path


def _is_write_mode(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _blocked(name: str):
    def guard(*args, **kwargs):
        _STATE[name] += 1
        raise AssertionError(
            f"PROHIBITED COLLECTION/SESSION PATH: {name}\n"
            + "".join(traceback.format_stack())
        )

    return guard


def _guard_open(original, name):
    def guarded(file, mode="r", *args, **kwargs):
        if isinstance(file, int) and _is_write_mode(mode):
            _STATE[name] += 1
            raise AssertionError(f"WRITE DESCRIPTOR BLOCKED: {file}")
        path = _production_path(file)
        if path is not None and _is_write_mode(mode):
            _STATE[name] += 1
            raise AssertionError(f"PRODUCTION WRITE BLOCKED: {path}")
        return original(file, mode, *args, **kwargs)

    return guarded


def _guard_path_open(original, name):
    def guarded(path, mode="r", *args, **kwargs):
        resolved = _production_path(path)
        if resolved is not None and _is_write_mode(mode):
            _STATE[name] += 1
            raise AssertionError(f"PRODUCTION PATH WRITE BLOCKED: {resolved}")
        return original(path, mode, *args, **kwargs)

    return guarded


def _guard_path_write(original, name):
    def guarded(path, *args, **kwargs):
        resolved = _production_path(path)
        if resolved is not None:
            _STATE[name] += 1
            raise AssertionError(f"PRODUCTION PATH WRITE BLOCKED: {resolved}")
        return original(path, *args, **kwargs)

    return guarded


def _guard_sqlite(original, name):
    def guarded(database, *args, **kwargs):
        path = _production_path(database)
        if path is not None:
            _STATE[name] += 1
            raise AssertionError(f"PRODUCTION DATABASE CONNECTION BLOCKED: {path}")
        return original(database, *args, **kwargs)

    return guarded


def _remember(owner, name):
    key = (owner, name)
    if key not in _ORIGINALS:
        _ORIGINALS[key] = getattr(owner, name)
    return _ORIGINALS[key]


def _restore():
    global _INSTALLED, _STUBS_NEUTRALIZED, _PATH_SETUP_NEUTRALIZED
    failures = []
    for (owner, name), original in reversed(list(_ORIGINALS.items())):
        try:
            setattr(owner, name, original)
        except BaseException as exc:
            failures.append((owner, name, exc))
    _ORIGINALS.clear()
    _INSTALLED = False
    _STUBS_NEUTRALIZED.clear()
    _PATH_SETUP_NEUTRALIZED = False
    if failures:
        details = "; ".join(
            f"{owner!r}.{name}: {type(error).__name__}: {error}"
            for owner, name, error in failures
        )
        raise RuntimeError(f"Acceptance safety restoration failed: {details}") from failures[0][2]


def _binding_specs():
    targets = [
        (builtins, "open", _guard_open, "builtins.open"),
        (Path, "open", _guard_path_open, "pathlib.Path.open"),
        (Path, "write_text", _guard_path_write, "pathlib.Path.write_text"),
        (Path, "write_bytes", _guard_path_write, "pathlib.Path.write_bytes"),
        (sqlite3, "connect", _guard_sqlite, "sqlite3.connect"),
    ]
    for owner, name in [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (urllib.request, "urlopen"),
        (http.client.HTTPConnection, "connect"),
        (http.client.HTTPSConnection, "connect"),
        (asyncio, "open_connection"),
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "call"),
        (subprocess, "check_call"),
        (subprocess, "check_output"),
    ]:
        targets.append((owner, name, None, f"{owner.__name__}.{name}"))
    return targets


def _install(bindings=None):
    global _INSTALLED
    if _INSTALLED:
        return
    bindings = _binding_specs() if bindings is None else bindings
    try:
        for owner, name, wrapper, label in bindings:
            original = _remember(owner, name)
            if wrapper is None:
                setattr(owner, name, _blocked(label))
            else:
                setattr(owner, name, wrapper(original, label))
        _INSTALLED = True
    except BaseException:
        _restore()
        raise


def _loaded_conftest_modules(config):
    plugin_manager = getattr(config, "pluginmanager", None)
    if plugin_manager is None:
        return []
    modules = []
    expected_path = (ROOT / "conftest.py").resolve()
    for candidate in plugin_manager.get_plugins():
        if not isinstance(candidate, ModuleType):
            continue
        source = getattr(candidate, "__file__", None)
        if source is not None and Path(source).resolve() == expected_path:
            modules.append(candidate)
    return modules


def _unwrap_fixture_function(fixture):
    wrapped = getattr(fixture, "__pytest_wrapped__", None)
    if wrapped is not None:
        fixture = getattr(wrapped, "obj", fixture)
    fixture_function = getattr(fixture, "_fixture_function", None)
    if fixture_function is not None:
        fixture = fixture_function
    return inspect.unwrap(fixture)


def _disable_repository_report_stubs(config):
    for conftest in _loaded_conftest_modules(config):
        if conftest in _STUBS_NEUTRALIZED:
            continue
        if not hasattr(conftest, "_write_stub"):
            continue
        _remember(conftest, "_write_stub")
        conftest._write_stub = lambda *args, **kwargs: None
        ensure_reports = getattr(conftest, "_ensure_reports_exist", None)
        underlying = (
            _unwrap_fixture_function(ensure_reports)
            if ensure_reports is not None
            else None
        )
        if (
            underlying is not None
            and underlying.__globals__.get("_write_stub") is not conftest._write_stub
        ):
            raise RuntimeError(
                f"{conftest.__name__}._ensure_reports_exist does not resolve patched _write_stub"
            )
        _STUBS_NEUTRALIZED[conftest] = True


def _disable_path_setup(path_module=None):
    global _PATH_SETUP_NEUTRALIZED
    if _PATH_SETUP_NEUTRALIZED:
        return
    if path_module is None:
        import path_config as path_module
    _remember(path_module, "ensure_paths")
    path_module.ensure_paths = lambda *args, **kwargs: None
    _PATH_SETUP_NEUTRALIZED = True


def pytest_configure(config):
    global _CLEANUP_DONE
    _CLEANUP_DONE = False
    config.add_cleanup(_cleanup)
    try:
        _install()
        _disable_repository_report_stubs(config)
        _disable_path_setup()
    except BaseException:
        _restore()
        raise


def pytest_sessionstart(session):
    # Conftest plugins are registered by this point, before session fixtures run.
    _disable_repository_report_stubs(session.config)


def pytest_sessionfinish(session, exitstatus):
    if _STATE:
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            f"Acceptance safety violations: {_STATE}"
        )
        session.exitstatus = 1


def pytest_unconfigure(config):
    _cleanup()


def _cleanup():
    global _CLEANUP_DONE
    if _CLEANUP_DONE:
        return
    restore_error = None
    try:
        _restore()
    except BaseException as exc:
        restore_error = exc

    counters = dict(_STATE)
    restored = restore_error is None and not _ORIGINALS and not _INSTALLED
    sys.stderr.write(
        "Acceptance safety cleanup: "
        f"restored={restored} counters={counters}"
        + (f" error={restore_error!r}" if restore_error else "")
        + "\n"
    )
    _STATE.clear()
    _CLEANUP_DONE = True
    if restore_error is not None:
        raise restore_error
