"""Tests for morning_run.ps1 — Scheduled morning run script validation."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "morning_run.ps1"


# ---------------------------------------------------------------------------
# Test: Script exists and has correct structure
# ---------------------------------------------------------------------------

class TestMorningRunStructure:
    def test_script_exists(self):
        assert SCRIPT.exists(), "morning_run.ps1 not found"

    def test_script_is_utf8_readable(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert len(content) > 100

    def test_has_param_block(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "param(" in content or "[CmdletBinding()]" in content

    def test_has_dry_run_flag(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "DryRun" in content

    def test_has_skip_india_flag(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "SkipIndia" in content

    def test_has_skip_screener_flag(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "SkipScreener" in content

    def test_has_logging(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "Write-Log" in content

    def test_has_error_handling(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "catch" in content.lower() or "ErrorActionPreference" in content


# ---------------------------------------------------------------------------
# Test: Script references correct files
# ---------------------------------------------------------------------------

class TestMorningRunReferences:
    def test_references_global_dashboard(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "global_portfolio_dashboard.py" in content

    def test_references_india_dashboard(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "india_portfolio_dashboard.py" in content

    def test_references_morning_screener(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "morning_screener.py" in content

    def test_references_signal_stack(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "signal_stack.py" in content

    def test_references_auto_validation(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "auto_validation.py" in content


# ---------------------------------------------------------------------------
# Test: Script sets correct env vars
# ---------------------------------------------------------------------------

class TestMorningRunEnv:
    def test_sets_protobuf_env(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION" in content

    def test_sets_openblas_env(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "OPENBLAS_NUM_THREADS" in content


# ---------------------------------------------------------------------------
# Test: Script exit codes
# ---------------------------------------------------------------------------

class TestMorningRunExitCodes:
    def test_exits_with_failure_on_error(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "exit 1" in content

    def test_exits_with_success(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "exit 0" in content


# ---------------------------------------------------------------------------
# Test: Script has summary section
# ---------------------------------------------------------------------------

class TestMorningRunSummary:
    def test_has_results_summary(self):
        content = SCRIPT.read_text(encoding="utf-8")
        assert "Results:" in content or "Summary" in content or "passed" in content
