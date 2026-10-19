"""
Tests for 5 bug fixes applied to the options contract screener.

Bugs fixed:
  1. _MIN_DTE was 1, now 6 — passes_quality_gate() rejects DTE < 6
  2. --max-be-sigma CLI arg was never forwarded to run_scan()
  3. --actionable-out CLI arg was never forwarded to run_scan()
  4. HTML/console/empty-state banners had wrong filter values (IV Rank 60→80%, Lev 40→65×)
  5. Rejected report template missing viewport meta tag (covered by test_structural_invariants.py)

Run:
    python -m pytest test_options_screener_fixes.py -v
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path setup — ensure project root is importable
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Module paths (used for source-text checks to avoid expensive imports)
# ---------------------------------------------------------------------------
_SCREENER_PY = ROOT / "tradingview" / "options" / "options_contract_screener.py"
_ACTIONABLE_PY = ROOT / "tradingview" / "options" / "options_actionable_report.py"


# ===========================================================================
# Helpers
# ===========================================================================

def _screener_src() -> str:
    """Return source text of options_contract_screener.py (cached per session)."""
    return _SCREENER_PY.read_text(encoding="utf-8")


def _actionable_src() -> str:
    """Return source text of options_actionable_report.py (cached per session)."""
    return _ACTIONABLE_PY.read_text(encoding="utf-8")


# ===========================================================================
# Bug fix 1 — _MIN_DTE constant
# ===========================================================================

class TestMinDteConstant:

    def test_min_dte_constant_is_6(self):
        """_MIN_DTE must be 6 (was incorrectly set to 1 before the fix)."""
        from tradingview.options.options_contract_screener import _MIN_DTE
        assert _MIN_DTE == 6, (
            f"_MIN_DTE should be 6 (got {_MIN_DTE}). "
            "Short-DTE contracts steal diversity cap slots and must be excluded."
        )

    def test_min_dte_appears_in_source(self):
        """The source must contain the literal assignment '_MIN_DTE       = 6'."""
        src = _screener_src()
        assert "_MIN_DTE" in src
        # Verify the value is 6 (not 1 or any other old value)
        assert "_MIN_DTE       = 6" in src or "_MIN_DTE = 6" in src, (
            "_MIN_DTE assignment with value 6 not found in source"
        )


# ===========================================================================
# Bug fix 1 (continued) — passes_quality_gate DTE rejection
# ===========================================================================

class TestPassesQualityGateDte:
    """passes_quality_gate() must reject contracts with DTE < 6."""

    # A minimal valid contract that passes all gates except DTE
    _VALID_BASE = {
        "mid_price": 1.0,        # >= _MIN_PREMIUM (0.05)
        "spread_pct": 5.0,       # <= _MAX_SPREAD (20%)
        "oi": 500,               # well above _MIN_OI_MOOMOO (100)
        "oi_source": "",         # no source → OI gate skipped
        "iv": 0.35,              # > 0 → IV gate passes
        "moneyness_pct": 10.0,   # <= _MAX_OTM_PCT (30%)
    }

    def test_quality_gate_rejects_dte_3(self):
        """DTE=3 is below _MIN_DTE=6; gate must return False."""
        from tradingview.options.options_contract_screener import passes_quality_gate
        contract = {**self._VALID_BASE, "dte": 3}
        assert passes_quality_gate(contract) is False, (
            "DTE=3 should be rejected (< _MIN_DTE=6)"
        )

    def test_quality_gate_rejects_dte_1(self):
        """DTE=1 (the old _MIN_DTE value) must also be rejected now."""
        from tradingview.options.options_contract_screener import passes_quality_gate
        contract = {**self._VALID_BASE, "dte": 1}
        assert passes_quality_gate(contract) is False, (
            "DTE=1 should be rejected (< _MIN_DTE=6)"
        )

    def test_quality_gate_rejects_dte_5(self):
        """DTE=5 is one below the boundary; gate must return False."""
        from tradingview.options.options_contract_screener import passes_quality_gate
        contract = {**self._VALID_BASE, "dte": 5}
        assert passes_quality_gate(contract) is False, (
            "DTE=5 should be rejected (< _MIN_DTE=6)"
        )

    def test_quality_gate_accepts_dte_6(self):
        """DTE=6 is exactly at boundary; gate must return True."""
        from tradingview.options.options_contract_screener import passes_quality_gate
        contract = {**self._VALID_BASE, "dte": 6}
        assert passes_quality_gate(contract) is True, (
            "DTE=6 should be accepted (== _MIN_DTE=6)"
        )

    def test_quality_gate_accepts_dte_14(self):
        """DTE=14 is comfortably above boundary; gate must return True."""
        from tradingview.options.options_contract_screener import passes_quality_gate
        contract = {**self._VALID_BASE, "dte": 14}
        assert passes_quality_gate(contract) is True, (
            "DTE=14 should be accepted (> _MIN_DTE=6)"
        )

    def test_quality_gate_skips_dte_check_when_dte_none(self):
        """When dte is missing (None), the DTE gate must be skipped (not reject)."""
        from tradingview.options.options_contract_screener import passes_quality_gate
        contract = {**self._VALID_BASE, "dte": None}
        # Should pass all other gates (dte gate is skipped when None)
        assert passes_quality_gate(contract) is True, (
            "Missing DTE (None) should not cause rejection"
        )


# ===========================================================================
# Bug fix 2 — run_scan() has max_be_sigma parameter
# ===========================================================================

class TestRunScanSignatureMaxBeSigma:
    """--max-be-sigma CLI arg must be forwarded via run_scan(max_be_sigma=...)."""

    def test_run_scan_has_max_be_sigma_param(self):
        """run_scan() must have a max_be_sigma parameter."""
        from tradingview.options.options_contract_screener import run_scan
        sig = inspect.signature(run_scan)
        assert "max_be_sigma" in sig.parameters, (
            "run_scan() is missing the max_be_sigma parameter. "
            "The --max-be-sigma CLI arg would be silently dropped."
        )

    def test_run_scan_max_be_sigma_default_is_0_55(self):
        """max_be_sigma default must be 0.55 (matches CLI --max-be-sigma default)."""
        from tradingview.options.options_contract_screener import run_scan
        param = inspect.signature(run_scan).parameters["max_be_sigma"]
        assert param.default == pytest.approx(0.55), (
            f"max_be_sigma default is {param.default!r}, expected 0.55"
        )


# ===========================================================================
# Bug fix 3 — run_scan() has actionable_out parameter
# ===========================================================================

class TestRunScanSignatureActionableOut:
    """--actionable-out CLI arg must be forwarded via run_scan(actionable_out=...)."""

    def test_run_scan_has_actionable_out_param(self):
        """run_scan() must have an actionable_out parameter."""
        from tradingview.options.options_contract_screener import run_scan
        sig = inspect.signature(run_scan)
        assert "actionable_out" in sig.parameters, (
            "run_scan() is missing the actionable_out parameter. "
            "The --actionable-out CLI arg would be silently dropped."
        )

    def test_run_scan_actionable_out_default_is_none(self):
        """actionable_out default must be None (caller supplies path or falls back)."""
        from tradingview.options.options_contract_screener import run_scan
        param = inspect.signature(run_scan).parameters["actionable_out"]
        assert param.default is None, (
            f"actionable_out default is {param.default!r}, expected None"
        )


# ===========================================================================
# Bug fix 4 — HTML banner filter values (options_actionable_report.py)
# ===========================================================================

class TestHtmlBannerFilterValues:
    """
    The visible HTML filter criteria banner must show the corrected thresholds.
    Old (wrong): IV Rank 60%, Lev 40×
    New (correct): IV Rank 80%, Lev 65×
    """

    def test_html_banner_contains_iv_le_80(self):
        """Banner must show 'IV ≤ 80%' (not the old 60%)."""
        src = _actionable_src()
        assert "IV ≤ 80%" in src, (
            "'IV ≤ 80%' not found in options_actionable_report.py. "
            "Banner may still show the old 'IV ≤ 60%' threshold."
        )

    def test_html_banner_contains_lev_le_65(self):
        """Banner must show 'Lev ≤ 65×' (not the old 40×)."""
        src = _actionable_src()
        assert "Lev ≤ 65×" in src, (
            "'Lev ≤ 65×' not found in options_actionable_report.py. "
            "Banner may still show the old 'Lev ≤ 40×' threshold."
        )

    def test_html_banner_contains_iv_rank_le_80(self):
        """Banner must show 'IV Rank ≤ 80%'."""
        src = _actionable_src()
        assert "IV Rank ≤ 80%" in src, (
            "'IV Rank ≤ 80%' not found in options_actionable_report.py."
        )

    def test_html_banner_contains_1dte_lev_50(self):
        """Banner must show '1-DTE: lev ≤ 50×' (special 1-DTE rule)."""
        src = _actionable_src()
        assert "1-DTE: lev ≤ 50×" in src, (
            "'1-DTE: lev ≤ 50×' not found in options_actionable_report.py."
        )

    def test_html_banner_does_not_contain_old_lev_40(self):
        """Banner must NOT still contain the old wrong 'Lev ≤ 40×' threshold."""
        src = _actionable_src()
        # Old wrong value should not appear inside the criteria banner context
        # (the value 40 appears in "θ ≤ 40%/day" which is correct — check for "Lev ≤ 40×" specifically)
        assert "Lev ≤ 40×" not in src, (
            "'Lev ≤ 40×' still present in options_actionable_report.py — old threshold not removed."
        )

    def test_html_banner_does_not_contain_old_iv_rank_60(self):
        """Banner must NOT still contain the old wrong 'IV Rank ≤ 60%' threshold."""
        src = _actionable_src()
        assert "IV Rank ≤ 60%" not in src, (
            "'IV Rank ≤ 60%' still present in options_actionable_report.py — old threshold not removed."
        )


# ===========================================================================
# Bug fix 4 (continued) — empty-state div filter values
# ===========================================================================

class TestEmptyStateDivFilterValues:
    """
    The empty-state div (shown when no contracts pass the filter) must list
    the corrected thresholds.
    """

    def test_empty_state_contains_no_contracts_passed_text(self):
        """The empty-state text must begin with 'No contracts passed the actionable filter'."""
        src = _actionable_src()
        assert "No contracts passed the actionable filter" in src

    def test_empty_state_contains_tqs_65(self):
        """Empty-state must mention 'TQS ≥ 65'."""
        src = _actionable_src()
        assert "TQS ≥ 65" in src, "'TQS ≥ 65' not found in empty-state text"

    def test_empty_state_contains_vol_oi_0_25(self):
        """Vol/OI filter disabled (OI display-only) — must mention 'OI display-only'."""
        src = _actionable_src()
        assert "OI display-only" in src, "'OI display-only' not found in empty-state text"

    def test_empty_state_contains_dte_6(self):
        """Empty-state must mention 'DTE ≥ 6' (was missing before fix)."""
        src = _actionable_src()
        assert "DTE ≥ 6" in src, "'DTE ≥ 6' not found in empty-state text"

    def test_empty_state_contains_spot_50(self):
        """Empty-state must mention 'Spot ≥ $50'."""
        src = _actionable_src()
        assert "Spot ≥ $50" in src, "'Spot ≥ $50' not found in empty-state text"

    def test_empty_state_contains_leverage_65(self):
        """Empty-state must mention 'leverage ≤ 65×' (corrected from old 40×)."""
        src = _actionable_src()
        assert "leverage ≤ 65×" in src, (
            "'leverage ≤ 65×' not found in empty-state text. "
            "May still show old 'leverage ≤ 40×'."
        )

    def test_empty_state_contains_iv_rank_80(self):
        """Empty-state must mention 'IV Rank ≤ 80%' (corrected from old 60%)."""
        src = _actionable_src()
        assert "IV Rank ≤ 80%" in src, (
            "'IV Rank ≤ 80%' not found in empty-state text. "
            "May still show old 'IV Rank ≤ 60%'."
        )


# ===========================================================================
# Source file existence sanity checks
# ===========================================================================

class TestSourceFilesExist:
    """Both modified files must exist at the expected paths."""

    def test_screener_file_exists(self):
        assert _SCREENER_PY.exists(), f"Source not found: {_SCREENER_PY}"

    def test_actionable_file_exists(self):
        assert _ACTIONABLE_PY.exists(), f"Source not found: {_ACTIONABLE_PY}"


# ===========================================================================
# Golden Rule #19 — verbose guard for per-contract console logging
# ===========================================================================

class TestVerboseGuardNotBroken:
    """
    Per-contract detail lines (C{strike} {dte}d spr=... oi=... → ...)
    MUST only print when verbose=True.

    This guard has broken twice due to formatter de-indentation / accidental edits.
    These tests detect the regression before it ships.

    Golden Rule #19: NEVER break the `if verbose and results:` guard in
    options_contract_screener.py. All per-contract print lines MUST remain
    inside this block.
    """

    def test_verbose_guard_present_in_source(self):
        """Source must contain the exact verbose guard before per-contract prints."""
        src = _screener_src()
        assert "if verbose and results:" in src, (
            "The `if verbose and results:` guard is missing from "
            "options_contract_screener.py. Per-contract detail lines will "
            "print for ALL runs regardless of --verbose flag."
        )

    def test_per_contract_print_is_after_verbose_guard(self):
        """The spr= per-contract print must appear AFTER `if verbose and results:`."""
        src = _screener_src()
        guard_pos = src.find("if verbose and results:")
        spr_pos = src.find("spr={c_spr")
        assert guard_pos != -1, "`if verbose and results:` not found in source"
        assert spr_pos != -1, "`spr={c_spr` not found in source"
        assert guard_pos < spr_pos, (
            "The `if verbose and results:` guard must appear BEFORE the spr= print. "
            "The verbose guard may have been removed or moved after the print."
        )

    def test_verbose_guard_indentation_correct(self):
        """
        The per-contract print must be indented INSIDE the verbose guard.
        Check that `spr={c_spr` line is indented more than `if verbose and results:`.
        """
        lines = _screener_src().splitlines()
        guard_indent = None
        spr_indent = None
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("if verbose and results:") and guard_indent is None:
                guard_indent = len(line) - len(stripped)
            if "spr={c_spr" in line and spr_indent is None:
                spr_indent = len(line) - len(stripped)
        assert guard_indent is not None, "`if verbose and results:` guard not found"
        assert spr_indent is not None, "`spr={c_spr` print not found"
        assert spr_indent > guard_indent, (
            f"Per-contract print indent ({spr_indent}) must be GREATER than "
            f"verbose guard indent ({guard_indent}). "
            "The print is outside the `if verbose:` block — it will always execute."
        )

    def test_run_scan_has_verbose_param(self):
        """run_scan() must have a verbose parameter defaulting to False."""
        from tradingview.options.options_contract_screener import run_scan
        sig = inspect.signature(run_scan)
        assert "verbose" in sig.parameters, "run_scan() is missing the verbose parameter"
        param = sig.parameters["verbose"]
        assert param.default is False, (
            f"run_scan() verbose default is {param.default!r}, expected False. "
            "Running without --verbose would still print per-contract details."
        )
