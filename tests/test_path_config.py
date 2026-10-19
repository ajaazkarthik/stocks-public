"""
Tests for path_config — centralised path management.

[1/N] — RESEARCH [DONE], VERIFICATION [IN PROGRESS], SAVE TO BRAIN [PENDING]

Validates:
  • All directory constants are pathlib.Path instances
  • ROOT points to the actual project root
  • ensure_paths() adds directories to sys.path idempotently
  • ensure_paths() accepts both Path objects and raw strings
  • Critical sub-directories actually exist on disk
"""
from __future__ import annotations

import sys
import importlib
import tempfile
from pathlib import Path

import pytest


# ── Constant correctness ──────────────────────────────────────────────────────

class TestPathConstants:
    """All exported constants must be Path objects pointing to real dirs."""

    def test_root_exists(self):
        from path_config import ROOT
        assert ROOT.is_dir(), f"ROOT {ROOT} is not a directory"

    def test_root_contains_main_py(self):
        from path_config import ROOT
        assert (ROOT / "main.py").is_file(), "ROOT should contain main.py"

    def test_all_constants_are_paths(self):
        import path_config
        expected = [
            "ROOT", "TV", "CGT", "INDIA", "REPORTS", "LOGS",
            "TV_SELL", "TV_SCREENERS", "TV_BACKTEST", "TV_OPTIONS",
            "TV_DATA", "TV_SIGNAL_ENGINE",
            "CGT_MOOMOO", "CGT_IBKR", "CGT_WEBULL",
            "IBKR_ASYNC", "MOOMOO_SCRIPTS", "BENCHMARKS",
        ]
        for name in expected:
            val = getattr(path_config, name, None)
            assert val is not None, f"{name} not found in path_config"
            assert isinstance(val, Path), f"{name} should be Path, got {type(val)}"

    def test_tv_is_subdir_of_root(self):
        from path_config import ROOT, TV
        assert TV == ROOT / "tradingview"

    def test_india_is_subdir_of_root(self):
        from path_config import ROOT, INDIA
        assert INDIA == ROOT / "india-brokers"

    def test_cgt_is_subdir_of_root(self):
        from path_config import ROOT, CGT
        assert CGT == ROOT / "cgt"

    def test_critical_dirs_exist(self):
        """Verify that the most-imported directories exist on disk."""
        from path_config import ROOT, TV, INDIA, CGT
        for d in (ROOT, TV, INDIA, CGT):
            assert d.is_dir(), f"{d} does not exist"


# ── ensure_paths() behaviour ──────────────────────────────────────────────────

class TestEnsurePaths:
    """ensure_paths() must be idempotent and accept both Path and str."""

    def test_adds_directory_to_sys_path(self, tmp_path):
        from path_config import ensure_paths
        target = str(tmp_path.resolve())
        # Remove if already there (test isolation)
        while target in sys.path:
            sys.path.remove(target)
        ensure_paths(tmp_path)
        assert target in sys.path

    def test_idempotent(self, tmp_path):
        from path_config import ensure_paths
        target = str(tmp_path.resolve())
        while target in sys.path:
            sys.path.remove(target)
        ensure_paths(tmp_path)
        ensure_paths(tmp_path)
        ensure_paths(tmp_path)
        assert sys.path.count(target) == 1, "ensure_paths must not add duplicates"

    def test_accepts_string(self, tmp_path):
        from path_config import ensure_paths
        target = str(tmp_path.resolve())
        while target in sys.path:
            sys.path.remove(target)
        ensure_paths(target)
        assert target in sys.path

    def test_multiple_dirs_at_once(self, tmp_path):
        from path_config import ensure_paths
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        s1, s2 = str(d1.resolve()), str(d2.resolve())
        for s in (s1, s2):
            while s in sys.path:
                sys.path.remove(s)
        ensure_paths(d1, d2)
        assert s1 in sys.path
        assert s2 in sys.path

    def test_returns_none(self, tmp_path):
        from path_config import ensure_paths
        result = ensure_paths(tmp_path)
        assert result is None
