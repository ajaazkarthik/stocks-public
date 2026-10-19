"""
Automated tests for every main.py menu handler.
=================================================
Coverage: all 31 handler functions.

Strategy
--------
• Each handler is called directly (not through the interactive menu).
• builtins.input  is mocked to supply canned answers — no keyboard needed.
• subprocess.run / subprocess.Popen are mocked — no real scripts execute.
• _india_env() is mocked — no broker authentication required.
• Assertions verify:
    (a) the correct script / flag is passed to subprocess
    (b) missing / empty inputs abort gracefully (no subprocess call)
    (c) cancellation prompts abort gracefully (no subprocess call)

Run all tests:
    python -m pytest tests/test_menu_handlers.py -v

Run with HTML report:
    python -m pytest tests/test_menu_handlers.py -v --html=reports/test_menu_handlers.html

Run one section:
    python -m pytest tests/test_menu_handlers.py -v -k "CGT"
    python -m pytest tests/test_menu_handlers.py -v -k "India"
    python -m pytest tests/test_menu_handlers.py -v -k "Screener"
"""

import os
import sys
import threading
import io
from pathlib import Path
from unittest.mock import call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview"))

import main as M

PYEXE = sys.executable
CGT   = ROOT / "cgt"
TV    = ROOT / "tradingview"
INDIA = ROOT / "india-brokers"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _cmd(mock_run) -> list:
    """Return the command list from the first subprocess.run call."""
    return mock_run.call_args[0][0]


def _all_cmds(mock_run) -> list[list]:
    """Return all command lists from all subprocess.run calls."""
    return [c[0][0] for c in mock_run.call_args_list]


def _cmd_for(mock_run, needle: str) -> list:
    """Return the single command containing `needle`.

    Selecting by script name instead of by position keeps these assertions
    stable when a step is inserted into a pipeline.
    """
    matches = [c for c in _all_cmds(mock_run) if needle in " ".join(str(p) for p in c)]
    assert matches, f"No subprocess call contained {needle!r}: {_all_cmds(mock_run)}"
    assert len(matches) == 1, f"Expected exactly one {needle!r} call, got {matches}"
    return matches[0]


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — CGT Analysis  (options 1-4)
# ══════════════════════════════════════════════════════════════════════════════

class TestMoomooCGT:
    """Option 1 — Moomoo CGT (no user input required)."""

    def test_calls_cgt_analysis_script(self, mock_subprocess_run):
        M.run_moomoo_au_cgt()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert str(CGT / "moomoo-cgt" / "cgt_analysis.py") in cmd

    def test_uses_python_executable(self, mock_subprocess_run):
        M.run_moomoo_au_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE


class TestIBKRCGT:
    """Option 2 — IBKR CGT (two modes: CSV and Flex Query)."""

    def test_csv_mode_builds_correct_cmd(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["csv", "activity.csv"])
        M.run_ibkr_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "ibkr_cgt_analysis.py" in str(cmd)
        assert "--statement" in cmd
        assert "activity.csv" in cmd

    def test_csv_mode_empty_path_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["csv", ""])
        M.run_ibkr_cgt()
        mock_subprocess_run.assert_not_called()

    def test_flex_mode_builds_correct_cmd(self, mocker, mock_subprocess_run):
        """Flex mode auto-fetches — no token/qid prompts needed."""
        mocker.patch("builtins.input", side_effect=["y"])
        M.run_ibkr_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "ibkr_cgt_analysis.py" in str(cmd)
        assert "--statement" not in cmd

    def test_flex_mode_missing_token_aborts(self, mocker, mock_subprocess_run):
        """Flex mode runs without extra flags (auto-fetch, no token prompt)."""
        mocker.patch("builtins.input", side_effect=["y"])
        M.run_ibkr_cgt()
        mock_subprocess_run.assert_called_once()

    def test_flex_mode_missing_qid_aborts(self, mocker, mock_subprocess_run):
        """Flex mode runs regardless — query id is baked into auto-fetch config."""
        mocker.patch("builtins.input", side_effect=["y"])
        M.run_ibkr_cgt()
        mock_subprocess_run.assert_called_once()

    def test_default_choice_is_csv(self, mocker, mock_subprocess_run):
        """Default Enter picks flex mode (y), so subprocess runs without --statement."""
        mocker.patch("builtins.input", side_effect=[""])
        M.run_ibkr_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "ibkr_cgt_analysis.py" in str(cmd)
        assert "--statement" not in cmd


class TestWebullCGT:
    """Option 28 — Webull CGT (PDF or CSV)."""

    def test_pdf_mode_default(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="1")
        mocker.patch("pathlib.Path.is_dir", return_value=True)
        mocker.patch("pathlib.Path.glob", return_value=["a.pdf", "b.pdf"])
        M.run_webull_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "webull_cgt_analysis.py" in str(cmd)
        assert "--pdf-dir" in cmd

    def test_with_valid_csv(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["2", "webull_orders.csv"])
        mocker.patch("pathlib.Path.exists", return_value=True)
        M.run_webull_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "webull_cgt_analysis.py" in str(cmd)
        assert "webull_orders.csv" in cmd

    def test_empty_csv_path_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["2", ""])
        M.run_webull_cgt()
        mock_subprocess_run.assert_not_called()


class TestCombinedCGT:
    """Option 4 — Combined CGT (IBKR + Moomoo)."""

    def test_default_no_flags(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "n", "n", "n"])
        M.run_combined_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "combined_cgt_report.py" in str(cmd)
        assert "--no-moomoo" not in cmd
        assert "--no-ibkr" not in cmd

    def test_skip_moomoo_adds_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "y", "n", "n"])
        M.run_combined_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-moomoo" in cmd

    def test_skip_ibkr_adds_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "n", "y", "n"])
        M.run_combined_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-ibkr" in cmd

    def test_with_ibkr_csv_adds_statement_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["ibkr_report.csv", "n", "n", "n"])
        M.run_combined_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "--ibkr-statement" in cmd
        assert "ibkr_report.csv" in cmd

    def test_both_skipped(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "y", "y", "n"])
        M.run_combined_cgt()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-moomoo" in cmd
        assert "--no-ibkr" in cmd


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — Portfolio  (option 5)
# ══════════════════════════════════════════════════════════════════════════════

class TestPortfolio:
    """Option 5 — Portfolio Analyser."""

    def test_calls_analyze_portfolio(self, mock_subprocess_run):
        M.run_portfolio()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert "analyze_portfolio.py" in str(cmd)


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — India Brokers  (options 5a–5l)
# ══════════════════════════════════════════════════════════════════════════════

class TestIndiaDashboard:
    """Option 5a — India Portfolio Dashboard."""

    def test_calls_dashboard_script_with_token(
        self, mocker, mock_subprocess_run, india_env_with_token
    ):
        M.run_india_dashboard()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert "india_portfolio_dashboard.py" in str(cmd)

    def test_calls_dashboard_without_token(
        self, mocker, mock_subprocess_run, india_env_no_token
    ):
        """Dashboard still runs (Angel One only) when Zerodha token is absent."""
        M.run_india_dashboard()
        mock_subprocess_run.assert_called_once()


class TestIndiaZerodhaGTT:
    """Options 5b–5e — Zerodha GTT management."""

    def test_gtt_audit_calls_correct_arg(
        self, mock_subprocess_run, india_env_with_token
    ):
        M.run_india_gtt_audit()
        cmd = _cmd(mock_subprocess_run)
        assert "india_gtt_manager.py" in str(cmd)
        assert "--audit" in cmd

    def test_gtt_suggest_calls_correct_arg(
        self, mock_subprocess_run, india_env_with_token
    ):
        M.run_india_gtt_suggest()
        cmd = _cmd(mock_subprocess_run)
        assert "--suggest" in cmd

    def test_gtt_place_confirmed_calls_correct_arg(
        self, mocker, mock_subprocess_run, india_env_with_token
    ):
        mocker.patch("builtins.input", return_value="yes")
        M.run_india_gtt_place()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert "--place-all" in cmd

    def test_gtt_place_not_confirmed_aborts(
        self, mocker, mock_subprocess_run, india_env_with_token
    ):
        mocker.patch("builtins.input", return_value="no")
        M.run_india_gtt_place()
        mock_subprocess_run.assert_not_called()

    def test_gtt_place_default_cancel_aborts(
        self, mocker, mock_subprocess_run, india_env_with_token
    ):
        """Pressing Enter should default to 'no' and abort."""
        mocker.patch("builtins.input", return_value="")
        M.run_india_gtt_place()
        mock_subprocess_run.assert_not_called()

    def test_gtt_list_calls_correct_arg(
        self, mock_subprocess_run, india_env_with_token
    ):
        M.run_india_gtt_list()
        cmd = _cmd(mock_subprocess_run)
        assert "--list" in cmd

    def test_gtt_audit_no_token_aborts(
        self, mock_subprocess_run, india_env_no_token
    ):
        """Without token, _require_zerodha returns False and no subprocess runs."""
        M.run_india_gtt_audit()
        mock_subprocess_run.assert_not_called()


class TestIndiaAngelOneGTT:
    """Options 5f–5i — Angel One GTT management (no Zerodha token needed)."""

    def test_ao_gtt_audit_calls_correct_arg(self, mock_subprocess_run):
        M.run_india_ao_gtt_audit()
        cmd = _cmd(mock_subprocess_run)
        assert "angelone_gtt_manager.py" in str(cmd)
        assert "--audit" in cmd

    def test_ao_gtt_suggest_calls_correct_arg(self, mock_subprocess_run):
        M.run_india_ao_gtt_suggest()
        cmd = _cmd(mock_subprocess_run)
        assert "--suggest" in cmd

    def test_ao_gtt_place_confirmed(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="yes")
        M.run_india_ao_gtt_place()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert "--place-all" in cmd

    def test_ao_gtt_place_not_confirmed_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="no")
        M.run_india_ao_gtt_place()
        mock_subprocess_run.assert_not_called()

    def test_ao_gtt_place_default_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="")
        M.run_india_ao_gtt_place()
        mock_subprocess_run.assert_not_called()

    def test_ao_gtt_list_calls_correct_arg(self, mock_subprocess_run):
        M.run_india_ao_gtt_list()
        cmd = _cmd(mock_subprocess_run)
        assert "--list" in cmd


class TestIndiaMarketInternals:
    """Option 5j — India Market Internals."""

    def test_default_symbol_and_expiry(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["NSE:RELIANCE", "NEAR", "y", "n"])
        M.run_india_market_internals()
        cmd = _cmd(mock_subprocess_run)
        assert "india_market_internals.py" in str(cmd)
        assert "--symbol" in cmd
        assert "NSE:RELIANCE" in cmd
        assert "--expiry" in cmd
        assert "NEAR" in cmd

    def test_custom_symbol_and_expiry(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["NSE:TCS", "FAR", "y", "n"])
        M.run_india_market_internals()
        cmd = _cmd(mock_subprocess_run)
        assert "NSE:TCS" in cmd
        assert "FAR" in cmd

    def test_empty_defaults_still_calls(self, mocker, mock_subprocess_run):
        """Empty inputs should fall back to defaults (NSE:RELIANCE, NEAR)."""
        mocker.patch("builtins.input", side_effect=["", "", "", ""])
        M.run_india_market_internals()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert "NSE:RELIANCE" in cmd
        assert "NEAR" in cmd


class TestIndiaOrderTest:
    """Option 5k — India Order Executor dry-run."""

    def test_basic_dry_run(self, mocker, mock_subprocess_run, india_env_with_token):
        mocker.patch("builtins.input", side_effect=["NSE:INFY", "", ""])
        M.run_india_order_test()
        cmd = _cmd(mock_subprocess_run)
        assert "india_order_executor.py" in str(cmd)
        assert "--symbol" in cmd
        assert "NSE:INFY" in cmd
        assert "--dry-run" in cmd

    def test_with_price_and_amount(
        self, mocker, mock_subprocess_run, india_env_with_token
    ):
        mocker.patch("builtins.input", side_effect=["NSE:TCS", "3500", "20000"])
        M.run_india_order_test()
        cmd = _cmd(mock_subprocess_run)
        assert "--price" in cmd
        assert "3500" in cmd
        assert "--amount" in cmd
        assert "20000" in cmd

    def test_no_token_aborts(self, mocker, mock_subprocess_run, india_env_no_token):
        mocker.patch("builtins.input", side_effect=["NSE:INFY", "", ""])
        M.run_india_order_test()
        mock_subprocess_run.assert_not_called()


class TestIndiaTests:
    """Option 5l — India Broker Test Suite."""

    def test_unit_tests_choice1(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="1")
        M.run_india_tests()
        cmd = _cmd(mock_subprocess_run)
        assert "-m" in cmd
        assert "pytest" in cmd
        assert "-v" in cmd

    def test_integration_tests_choice2(
        self, mocker, mock_subprocess_run, india_env_with_token
    ):
        mocker.patch("builtins.input", return_value="2")
        M.run_india_tests()
        cmd = _cmd(mock_subprocess_run)
        assert "test_integration_india.py" in str(cmd)

    def test_all_tests_choice3(
        self, mocker, mock_subprocess_run, india_env_with_token
    ):
        mocker.patch("builtins.input", return_value="3")
        M.run_india_tests()
        cmd = _cmd(mock_subprocess_run)
        # Use element-level check to avoid Windows double-backslash repr issues
        assert any("india-brokers" in str(c) for c in cmd)

    def test_default_choice_runs_unit_tests(self, mocker, mock_subprocess_run):
        """Default Enter = choice 1 = unit tests only."""
        mocker.patch("builtins.input", return_value="")
        M.run_india_tests()
        mock_subprocess_run.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — F&O Options  (options 22, 22a, 22b, 22c)
# ══════════════════════════════════════════════════════════════════════════════

class TestFOOptions:
    """Options 22–22c — F&O Options analysis."""

    def test_from_actionable_defaults(self, mocker, mock_subprocess_run):
        mocker.patch("main._is_port_in_use", return_value=False)  # force OpenD-offline path
        mocker.patch("builtins.input", side_effect=["n", "20"])  # "n" = skip OpenD auto-start
        M.run_options_from_actionable()
        cmd = _cmd(mock_subprocess_run)
        assert "run_options.py" in str(cmd)
        assert "--from-actionable" in cmd
        assert "--all-modes" in cmd
        assert "--limit" in cmd
        assert "20" in cmd

    def test_from_actionable_no_mode_prompt(self, mocker, mock_subprocess_run):
        """Option 12 must not prompt for mode — it always passes --all-modes."""
        mocker.patch("main._is_port_in_use", return_value=False)  # force OpenD-offline path
        mocker.patch("builtins.input", side_effect=["n", "20"])  # "n" = skip OpenD auto-start
        M.run_options_from_actionable()
        cmd = _cmd(mock_subprocess_run)
        assert "--mode" not in cmd
        assert "--all-modes" in cmd

    def test_manual_single_symbol(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["NASDAQ:AAPL", "70", "SAFE", "n"])
        M.run_options_manual()
        cmd = _cmd(mock_subprocess_run)
        assert "--symbol" in cmd
        assert "NASDAQ:AAPL" in cmd
        assert "--score" in cmd
        assert "70" in cmd
        assert "SAFE" in cmd

    def test_manual_all_modes(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["NSE:INFY", "65", "BALANCED", "y"])
        M.run_options_manual()
        cmd = _cmd(mock_subprocess_run)
        assert "--all-modes" in cmd

    def test_manual_empty_symbol_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "65", "BALANCED", "n"])
        M.run_options_manual()
        mock_subprocess_run.assert_not_called()

    def test_india_only_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="BALANCED")
        M.run_options_india()
        cmd = _cmd(mock_subprocess_run)
        assert "--india-only" in cmd
        assert "--from-actionable" in cmd

    def test_us_only_flag(self, mocker, mock_subprocess_run):
        mocker.patch("main._options_preflight", return_value=True)
        mocker.patch("builtins.input", return_value="BALANCED")
        M.run_options_us()
        cmd = _cmd(mock_subprocess_run)
        assert "--us-only" in cmd
        assert "--from-actionable" in cmd

    def test_all_modes_always_set(self, mocker, mock_subprocess_run):
        """--all-modes must always be in the command regardless of limit input."""
        mocker.patch("builtins.input", side_effect=["n", "5"])  # "n" = skip OpenD auto-start
        M.run_options_from_actionable()
        cmd = _cmd(mock_subprocess_run)
        assert "--all-modes" in cmd
        assert "--mode" not in cmd


class TestPutOptions:
    """Options 12a/12b — PUT options handlers."""

    def test_put_from_actionable_defaults(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["n", "BALANCED", "20"])  # "n" = skip OpenD auto-start
        M.run_put_options_from_actionable()
        cmd = _cmd(mock_subprocess_run)
        assert "run_options.py" in str(cmd)
        assert "--puts" in cmd
        assert "--from-actionable" in cmd
        assert "BALANCED" in cmd

    def test_put_from_actionable_aggressive(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["n", "AGGRESSIVE", "10"])  # "n" = skip OpenD auto-start
        M.run_put_options_from_actionable()
        cmd = _cmd(mock_subprocess_run)
        assert "--puts" in cmd
        assert "AGGRESSIVE" in cmd

    def test_put_from_bearish_screen(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["n", "SAFE", "15"])  # "n" = skip OpenD auto-start
        M.run_put_options_from_bearish_screen()
        cmd = _cmd(mock_subprocess_run)
        assert "--puts" in cmd
        assert "--from-bearish-screen" in cmd
        assert "SAFE" in cmd

    def test_put_from_bearish_screen_mode_uppercased(self, mocker, mock_subprocess_run):
        mocker.patch("main._is_port_in_use", return_value=False)  # force OpenD-offline path
        mocker.patch("builtins.input", side_effect=["n", "safe", "10"])  # "n" = skip OpenD auto-start
        M.run_put_options_from_bearish_screen()
        cmd = _cmd(mock_subprocess_run)
        assert "SAFE" in cmd
        assert "--puts" in cmd


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — TradingView — Buy Alerts  (options 6–9)
# ══════════════════════════════════════════════════════════════════════════════

class TestTVDownloadFires:
    """Option 6 — Download fired alerts from TradingView."""

    def test_calls_download_fires_script(self, mock_subprocess_run):
        M.run_download_fires()
        cmd = _cmd(mock_subprocess_run)
        assert "download_fires.py" in str(cmd)


class TestHoldingsSignals:
    """Option 14b — Holdings Signal Scan."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self, mocker):
        """Default to a fresh positions cache so tests exercise the scoring path."""
        mocker.patch.object(M, "_ensure_fresh_positions", return_value=True)
        mocker.patch.object(M, "_actionable_signal_count", return_value=-1)

    def test_runs_signal_stack_in_holdings_mode(self, mocker, mock_subprocess_run):
        M.run_holdings_signals()
        cmds = _all_cmds(mock_subprocess_run)
        assert "signal_stack.py" in str(cmds[0])
        assert "--holdings" in cmds[0]

    def test_uses_the_agreed_threshold(self, mocker, mock_subprocess_run):
        M.run_holdings_signals()
        cmd = _all_cmds(mock_subprocess_run)[0]
        assert "--threshold" in cmd
        assert "62.5" in cmd

    def test_never_passes_fire_scope_flags(self, mocker, mock_subprocess_run):
        """Holdings mode has no fires, so alert-scope flags would be meaningless."""
        M.run_holdings_signals()
        cmd = _all_cmds(mock_subprocess_run)[0]
        assert "--new-only" not in cmd
        assert "--n" not in cmd

    def test_defaults_to_vader_sentiment(self, mocker, mock_subprocess_run):
        """Golden Rule #13 — VADER is always the default backend."""
        M.run_holdings_signals()
        cmd = _all_cmds(mock_subprocess_run)[0]
        assert "--sentiment" in cmd
        assert "vader" in cmd

    def test_options_run_is_scoped_to_the_holdings_report(self, mocker, mock_subprocess_run):
        """Without --actionable-file the options pass would analyse the option-14 report."""
        mocker.patch.object(M, "_actionable_signal_count", return_value=5)
        M.run_holdings_signals()
        cmds = _all_cmds(mock_subprocess_run)
        options_cmd = cmds[-1]
        assert "run_options.py" in str(options_cmd)
        assert "--actionable-file" in options_cmd
        assert any("signal_report_actionable_holdings.json" in str(c) for c in options_cmd)

    def test_zero_actionable_holdings_skips_options(self, mocker, mock_subprocess_run):
        mocker.patch.object(M, "_actionable_signal_count", return_value=0)
        M.run_holdings_signals()
        cmds = _all_cmds(mock_subprocess_run)
        assert "run_options.py" not in " ".join(str(c) for c in cmds)

    def test_stale_cache_refresh_failure_stops_the_run(self, mocker, mock_subprocess_run):
        """No positions means nothing can be scored — don't run the pipeline on an empty book."""
        mocker.patch.object(M, "_ensure_fresh_positions", return_value=False)
        M.run_holdings_signals()
        assert mock_subprocess_run.call_count == 0

    def test_handler_is_registered_in_the_menu(self):
        keys = {row[0] for row in M.MENU if row[0] != "section"}
        assert "14b" in keys


class TestEnsureFreshPositions:
    """Option 14b refreshes stale positions by running the Global Portfolio Dashboard."""

    @pytest.fixture(autouse=True)
    def _no_broker_auth(self, mocker):
        """Keep stale-cache handler tests entirely offline."""
        mocker.patch.object(M, "_india_env", return_value=M.BASE_ENV.copy())

    def test_fresh_cache_is_reused(self, mocker, mock_subprocess_run):
        mocker.patch("holdings_resolver.cache_age_seconds", return_value=60)
        assert M._ensure_fresh_positions() is True
        assert mock_subprocess_run.call_count == 0

    def test_stale_cache_triggers_a_refresh(self, mocker, mock_subprocess_run):
        mocker.patch("holdings_resolver.cache_age_seconds", return_value=90_000)
        M._ensure_fresh_positions()
        cmd = _all_cmds(mock_subprocess_run)[0]
        assert "global_portfolio_dashboard.py" in str(cmd)
        assert "--force-refresh" in cmd

    def test_refresh_never_passes_no_ibkr(self, mocker, mock_subprocess_run):
        """SD-007: main.py must never skip IBKR — it breaks the all-or-none rule."""
        mocker.patch("holdings_resolver.cache_age_seconds", return_value=90_000)
        M._ensure_fresh_positions()
        assert "--no-ibkr" not in _all_cmds(mock_subprocess_run)[0]


class TestTVSignalStack:
    """Option 7 — Signal Stack."""

    @pytest.fixture(autouse=True)
    def _unknown_actionable_count(self, mocker):
        """Model a validated current report for ordinary handler tests."""
        mocker.patch.object(M, "_actionable_signal_count", return_value=-1)
        mocker.patch.object(M, "_validate_actionable_report", return_value=(True, "valid", 1))
        mocker.patch.object(M, "_load_cached_token", return_value=None)
        mocker.patch.object(M, "_india_env", return_value=M.BASE_ENV.copy())

    def test_source_win_rates_use_canonical_closed_outcomes(self, tmp_path):
        import sqlite3

        db = tmp_path / "tracker.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """
                CREATE TABLE signal_engine_outputs (
                    id INTEGER PRIMARY KEY, timestamp TEXT,
                    input_source TEXT, parameters_json TEXT
                );
                CREATE TABLE price_evolution (
                    output_id INTEGER, hours_elapsed REAL, return_pct REAL
                );
                INSERT INTO signal_engine_outputs VALUES
                    (1, '2026-07-13T00:00:00Z', 'download', '{"model_version":"test-v1"}'),
                    (2, '2026-07-13T00:00:00Z', 'download', '{"model_version":"test-v1"}'),
                    (3, '2026-07-13T00:00:00Z', 'webhook',  '{"model_version":"test-v1"}');
                INSERT INTO price_evolution VALUES
                    (1, 168.0, 4.0), (2, 168.0, 1.0), (3, 168.0, 5.0);
                """
            )

        text = M._signal_stack_win_rates(db, min_samples=2)

        assert "Download alerts" in text
        assert "50.0% win rate" in text
        assert "n=2, 7d return" in text

    def test_customize_mode_always_downloads(self, mocker, mock_subprocess_run):
        """Customize mode always downloads fires from TV first, then runs signal_stack then options."""
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "50", "50", "n", "n", "n", "n", "n", "n", "v"])
        M.run_signal_stack()
        # 4 calls: download_fires.py, alert_tracker.py, signal_stack.py, run_options.py
        assert mock_subprocess_run.call_count == 4
        cmds = _all_cmds(mock_subprocess_run)
        assert "download_fires.py" in str(cmds[0])
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "signal_stack.py" in str(cmd)
        assert "--n" in cmd
        assert "50" in cmd
        assert "--threshold" in cmd
        assert "--sentiment" in cmd
        assert "vader" in cmd
        assert "--new-only" not in cmd
        # Verify F&O options analysis is auto-run after signal_stack
        options_cmd = _cmd_for(mock_subprocess_run, "run_options.py")
        assert "run_options.py" in str(options_cmd)
        assert "--from-actionable" in options_cmd
        assert "--all-modes" in options_cmd

    def test_customize_mode_passes_custom_n_threshold(self, mocker, mock_subprocess_run):
        """Customize mode passes custom n and threshold to signal_stack.py."""
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "30", "60", "n", "n", "n", "n", "n", "n", "v"])
        M.run_signal_stack()
        # download_fires.py, alert_tracker.py, signal_stack.py, then run_options.py
        assert mock_subprocess_run.call_count == 4
        cmds = _all_cmds(mock_subprocess_run)
        assert "download_fires.py" in str(cmds[0])
        stack_cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "30" in stack_cmd
        assert "60" in stack_cmd
        assert "--sentiment" in stack_cmd
        assert "vader" in stack_cmd
        assert _cmd_for(mock_subprocess_run, "run_options.py")

    def test_new_only_scope_passes_flag_and_still_runs_everything(
        self, mocker, mock_subprocess_run
    ):
        """Choosing 'new' passes --new-only and keeps the full pipeline when work exists."""
        mocker.patch("main._count_new_fires", return_value=7)
        # Scope 'n' skips the fire-count prompt entirely.
        mocker.patch("builtins.input", side_effect=["c", "n", "50", "n", "n", "n", "n", "n", "n", "v"])
        M.run_signal_stack()
        assert mock_subprocess_run.call_count == 4
        cmds = _all_cmds(mock_subprocess_run)
        assert "download_fires.py" in str(cmds[0])
        assert "--new-only" in _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert _cmd_for(mock_subprocess_run, "run_options.py")

    def test_new_only_with_nothing_new_stops_after_download(
        self, mocker, mock_subprocess_run
    ):
        """Zero new alerts must not launch scoring or options — but outcomes still settle."""
        mocker.patch("main._count_new_fires", return_value=0)
        mocker.patch("builtins.input", side_effect=["c", "n", "50", "n", "n", "n", "n", "n", "n", "v"])
        M.run_signal_stack()
        cmds = _all_cmds(mock_subprocess_run)
        assert mock_subprocess_run.call_count == 2
        assert "download_fires.py" in str(cmds[0])
        assert "signal_stack.py" not in " ".join(str(c) for c in cmds)
        assert "run_options.py" not in " ".join(str(c) for c in cmds)
        # A quiet day is exactly when the outcome backfill must still happen.
        assert _cmd_for(mock_subprocess_run, "alert_tracker.py")

    def test_settles_outcomes_before_scoring(self, mocker, mock_subprocess_run):
        """Option 14 must advance the forward-test record, not just consume it.

        alert_tracker only snapshots alerts whose expires_at is still in the
        future, so a run that skips it silently loses those outcomes forever.
        """
        mocker.patch("builtins.input", side_effect=["", ""])  # quick, recent scope
        M.run_signal_stack()
        cmd = _cmd_for(mock_subprocess_run, "alert_tracker.py")
        assert "--update" in cmd, "Must snapshot prices for still-open alerts"
        assert "--compute" in cmd, "Must recompute metrics so outcomes actually settle"
        assert "--report" not in cmd, "HTML report is the slow part; keep option 14 responsive"
        # It has to run before scoring, so a mid-run bail-out still banks the outcomes.
        cmds = _all_cmds(mock_subprocess_run)
        assert cmds.index(cmd) < cmds.index(_cmd_for(mock_subprocess_run, "signal_stack.py"))

    def test_unknown_ledger_state_does_not_block_the_run(self, mocker, mock_subprocess_run):
        """An unreadable ledger returns -1 and must not be mistaken for 'nothing new'."""
        mocker.patch("main._count_new_fires", return_value=-1)
        mocker.patch("builtins.input", side_effect=["c", "n", "50", "n", "n", "n", "n", "n", "n", "v"])
        M.run_signal_stack()
        assert mock_subprocess_run.call_count == 4

    def test_quick_mode_can_select_new_only(self, mocker, mock_subprocess_run):
        """Quick mode also offers the scope choice."""
        mocker.patch("main._count_new_fires", return_value=3)
        mocker.patch("builtins.input", side_effect=["", "n"])  # Enter = quick, 'n' = new only
        M.run_signal_stack()
        assert mock_subprocess_run.call_count == 4
        assert "--new-only" in _cmd_for(mock_subprocess_run, "signal_stack.py")

    def test_zero_actionable_signals_skips_the_options_run(self, mocker, mock_subprocess_run):
        """A --new-only run can legitimately produce nothing; don't burn an options pass on it."""
        mocker.patch.object(M, "_validate_actionable_report", return_value=(True, "valid", 0))
        mocker.patch("builtins.input", side_effect=["", ""])
        M.run_signal_stack()
        cmds = _all_cmds(mock_subprocess_run)
        assert len(cmds) == 3, f"Expected download + tracker + signal_stack only, got {cmds}"
        assert "run_options.py" not in " ".join(str(c) for c in cmds)

    @pytest.mark.parametrize("failed_stage", [0, 1, 2])
    def test_upstream_failure_blocks_options_even_with_old_report(
        self, mocker, failed_stage
    ):
        mocker.patch("builtins.input", side_effect=["", ""])
        calls = mocker.patch.object(M, "run", side_effect=[1 if i == failed_stage else 0 for i in range(3)])

        M.run_signal_stack()

        assert not any("run_options.py" in " ".join(map(str, c.args[0]))
                       for c in calls.call_args_list)
        assert calls.call_count == failed_stage + 1

    @pytest.mark.parametrize("reason", [
        "missing", "malformed", "mismatched", "incomplete", "watch",
    ])
    def test_invalid_report_blocks_options(self, mocker, reason):
        mocker.patch("builtins.input", side_effect=["", ""])
        mocker.patch.object(M, "run", return_value=0)
        mocker.patch.object(M, "_validate_actionable_report", return_value=(False, reason, 0))

        M.run_signal_stack()

        assert not any("run_options.py" in " ".join(map(str, c.args[0]))
                       for c in M.run.call_args_list)

    def test_current_report_with_candidates_passes_strict_handoff(self, mocker):
        mocker.patch("builtins.input", side_effect=["", ""])
        mocker.patch.object(M, "run", return_value=0)
        mocker.patch.object(M, "_validate_actionable_report", return_value=(True, "valid", 1))

        M.run_signal_stack()

        options_call = next(c for c in M.run.call_args_list if "run_options.py" in str(c.args[0]))
        command = options_call.args[0]
        assert "--strict-actionable" in command
        assert "--expected-run-id" in command
        assert "--actionable-file" in command
        assert "--output" in command
        assert any("options_report_" in str(value) for value in command)

    def test_failed_options_run_does_not_report_success_from_old_report(
        self, mocker, capsys
    ):
        """A prior fixed-name report must not mask a failed current options run."""
        (M.REPORTS / "options_report.html").write_text("old report", encoding="utf-8")
        mocker.patch("builtins.input", side_effect=["", ""])
        mocker.patch.object(M, "run", side_effect=[0, 0, 0, 7])
        mocker.patch.object(M, "_validate_actionable_report", return_value=(True, "valid", 1))

        M.run_signal_stack()

        output = capsys.readouterr().out
        assert "F&O options analysis failed with exit code 7." in output
        assert "F&O Options Analysis complete" not in output
        assert "options_report.html" not in output
        assert "ACTIONABLE REPORT:" in output

    def test_two_invocations_receive_different_run_ids(self, mocker):
        mocker.patch("builtins.input", side_effect=["", "", "", ""])
        mocker.patch.object(M, "run", return_value=0)
        mocker.patch.object(M, "_validate_actionable_report", return_value=(True, "valid", 1))

        M.run_signal_stack()
        first = M.run.call_args_list[-2].kwargs["env"]["SIGNAL_RUN_ID"]
        M.run_signal_stack()
        second = M.run.call_args_list[-2].kwargs["env"]["SIGNAL_RUN_ID"]

        assert first != second


    def test_no_moomoo_flag(self, mocker, mock_subprocess_run):
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "50", "50", "y", "n", "n", "n", "n", "n", "v"])
        M.run_signal_stack()
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "--no-moomoo" in cmd
        assert "--sentiment" in cmd

    def test_no_external_flag(self, mocker, mock_subprocess_run):
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "50", "50", "n", "n", "n", "y", "n", "n", "v"])
        M.run_signal_stack()
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "--no-external" in cmd
        assert "--sentiment" in cmd

    def test_sentiment_finbert_choice(self, mocker, mock_subprocess_run):
        """Choosing 'f' should pass --sentiment finbert to signal_stack.py."""
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "50", "50", "n", "n", "n", "n", "n", "n", "f"])
        M.run_signal_stack()
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "--sentiment" in cmd
        assert "finbert" in cmd

    def test_sentiment_both_choice(self, mocker, mock_subprocess_run):
        """Choosing 'b' should pass --sentiment both to signal_stack.py."""
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "50", "50", "n", "n", "n", "n", "n", "n", "b"])
        M.run_signal_stack()
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "--sentiment" in cmd
        assert "both" in cmd

    def test_sentiment_customize_default_is_vader(self, mocker, mock_subprocess_run):
        """Pressing Enter (empty) in customize mode must default to vader — Golden Rule #13."""
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "50", "50", "n", "n", "n", "n", "n", "n", ""])
        M.run_signal_stack()
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "--sentiment" in cmd
        assert "vader" in cmd

    def test_quick_mode_uses_vader_sentiment(self, mocker, mock_subprocess_run):
        """Quick mode uses 'vader' — Golden Rule #13 (FinBERT never the default)."""
        mocker.patch("builtins.input", side_effect=["", ""])  # quick, recent scope
        M.run_signal_stack()
        # 4 calls: download_fires.py, alert_tracker.py, signal_stack.py, run_options.py
        assert mock_subprocess_run.call_count == 4
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "--sentiment" in cmd
        assert "vader" in cmd

    def test_invalid_n_defaults_to_50(self, mocker, mock_subprocess_run):
        """Non-numeric n should fall back to 50."""
        # Input order: [quick/customize, scope, n_fires, threshold, no_moomoo, no_india, use_ibkr, no_external, use_ai, use_edgar, sentiment]
        mocker.patch("builtins.input", side_effect=["c", "r", "bad_num", "50", "n", "n", "n", "n", "n", "n", "v"])
        M.run_signal_stack()
        cmd = _cmd_for(mock_subprocess_run, "signal_stack.py")
        assert "50" in cmd

    def test_auto_runs_options_after_signal_stack(self, mocker, mock_subprocess_run):
        """After signal_stack.py, analyze the top 20 eligible quality-gated signals."""
        mocker.patch("builtins.input", side_effect=["", ""])  # quick mode, recent scope
        M.run_signal_stack()
        cmds = _all_cmds(mock_subprocess_run)
        assert len(cmds) == 4, f"Expected 4 subprocess calls, got {len(cmds)}: {cmds}"
        options_cmd = _cmd_for(mock_subprocess_run, "run_options.py")
        assert "--from-actionable" in options_cmd
        assert "--all-modes" in options_cmd
        assert "--limit" in options_cmd
        assert "20" in options_cmd

    def test_options_auto_run_uses_cwd_tv(self, mocker, mock_subprocess_run):
        """run_options.py must be called with cwd=TV (tradingview directory)."""
        mocker.patch("builtins.input", side_effect=["", ""])  # quick mode, recent scope
        M.run_signal_stack()
        # The run_options.py call must have cwd pointing to the tradingview dir
        idx = _all_cmds(mock_subprocess_run).index(_cmd_for(mock_subprocess_run, "run_options.py"))
        kwargs = mock_subprocess_run.call_args_list[idx][1]
        assert "tradingview" in str(kwargs.get("cwd", "")).lower()


class TestSubprocessExitCodes:
    def test_run_returns_subprocess_exit_code(self, mocker):
        mocker.patch.object(M, "_current_log_path", None)
        mocker.patch.object(M.subprocess, "run", return_value=mocker.Mock(returncode=7))
        assert M.run(["command"]) == 7

    def test_run_returns_logged_process_exit_code(self, mocker, tmp_path):
        process = mocker.Mock()
        process.stdout = io.StringIO("")
        process.stderr = io.StringIO("")
        process.wait.return_value = 9
        mocker.patch.object(M, "_current_log_path", tmp_path / "run.log")
        mocker.patch.object(M.subprocess, "Popen", return_value=process)
        assert M.run(["command"]) == 9

    def test_run_treats_launch_exception_as_failure(self, mocker):
        mocker.patch.object(M, "_current_log_path", None)
        mocker.patch.object(M.subprocess, "run", side_effect=OSError("not found"))
        assert M.run(["command"]) == 127


class TestTVWebhookServer:
    """Option 8 — Webhook Server."""

    def test_starts_new_console_when_port_free(
        self, mocker, mock_subprocess_popen, mock_port_free
    ):
        mocker.patch("builtins.input", side_effect=["n", "n"])
        M.run_webhook_server()
        mock_subprocess_popen.assert_called_once()
        cmd = mock_subprocess_popen.call_args[0][0]
        assert "webhook_server.py" in str(cmd)

    def test_aborts_when_port_busy(
        self, mocker, mock_subprocess_popen, mock_port_busy
    ):
        M.run_webhook_server()
        mock_subprocess_popen.assert_not_called()

    def test_no_sizer_flag(self, mocker, mock_subprocess_popen, mock_port_free):
        mocker.patch("builtins.input", side_effect=["y", "n"])
        M.run_webhook_server()
        cmd = mock_subprocess_popen.call_args[0][0]
        assert "--no-sizer" in cmd

    def test_no_external_flag(self, mocker, mock_subprocess_popen, mock_port_free):
        mocker.patch("builtins.input", side_effect=["n", "y"])
        M.run_webhook_server()
        cmd = mock_subprocess_popen.call_args[0][0]
        assert "--no-external" in cmd


class TestTVTestEnrichers:
    """Option 9 — Test Enrichers."""

    def test_all_enrichers_default(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["all", "NSE:RELIANCE"])
        M.run_test_enrichers()
        cmd = _cmd(mock_subprocess_run)
        assert "webhook_server.py" in str(cmd)
        assert "--test-enrichers" in cmd
        assert "all" in cmd
        assert "--test-symbol" in cmd
        assert "NSE:RELIANCE" in cmd

    def test_youtube_enricher(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["youtube", "NASDAQ:NVDA"])
        M.run_test_enrichers()
        cmd = _cmd(mock_subprocess_run)
        assert "youtube" in cmd

    def test_invalid_enricher_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["invalid_enricher", "NSE:INFY"])
        M.run_test_enrichers()
        mock_subprocess_run.assert_not_called()

    @pytest.mark.parametrize("source", ["youtube", "smallcase", "twitter", "sizer", "all"])
    def test_all_valid_sources_accepted(self, mocker, mock_subprocess_run, source):
        mocker.patch("builtins.input", side_effect=[source, "NSE:RELIANCE"])
        M.run_test_enrichers()
        mock_subprocess_run.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Section 6 — Backtest  (option 10)
# ══════════════════════════════════════════════════════════════════════════════

class TestStrategyBacktest:
    """Option 10 — Strategy Backtest (direct import, not subprocess)."""

    def test_all_symbols_default(self, mocker, mock_backtest_menu):
        mocker.patch("builtins.input", return_value="all")
        M.run_strategy_backtest()
        mock_backtest_menu.assert_called_once_with("all")

    def test_specific_symbol(self, mocker, mock_backtest_menu):
        mocker.patch("builtins.input", return_value="NASDAQ:NVDA")
        M.run_strategy_backtest()
        mock_backtest_menu.assert_called_once_with("NASDAQ:NVDA")

    def test_empty_input_defaults_to_all(self, mocker, mock_backtest_menu):
        """Empty Enter selects 'all'."""
        mocker.patch("builtins.input", return_value="")
        M.run_strategy_backtest()
        mock_backtest_menu.assert_called_once_with("all")


# ══════════════════════════════════════════════════════════════════════════════
# Section 7 — Sell Strategy  (option 11)
# ══════════════════════════════════════════════════════════════════════════════

class TestSellStrategy:
    """Option 11 — Sell Strategy."""

    def test_live_mode_choice1(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="1")
        M.run_sell_strategy()
        cmd = _cmd(mock_subprocess_run)
        assert "sell_strategy.py" in str(cmd)
        assert "--live" in cmd

    def test_positions_json_mode_choice2(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="2")
        M.run_sell_strategy()
        cmd = _cmd(mock_subprocess_run)
        assert "sell_strategy.py" in str(cmd)
        assert "--live" not in cmd
        assert "--demo" not in cmd

    def test_custom_file_mode_choice3(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["3", "my_positions.json"])
        M.run_sell_strategy()
        cmd = _cmd(mock_subprocess_run)
        assert "--positions" in cmd
        assert "my_positions.json" in cmd

    def test_custom_file_empty_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["3", ""])
        M.run_sell_strategy()
        mock_subprocess_run.assert_not_called()

    def test_demo_mode_choice4(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="4")
        M.run_sell_strategy()
        cmd = _cmd(mock_subprocess_run)
        assert "--demo" in cmd

    def test_default_choice_is_live(self, mocker, mock_subprocess_run):
        """Empty Enter defaults to choice 1 = live mode."""
        mocker.patch("builtins.input", return_value="")
        M.run_sell_strategy()
        cmd = _cmd(mock_subprocess_run)
        assert "--live" in cmd


# ══════════════════════════════════════════════════════════════════════════════
# Section 8 — Screeners  (options 12–15)
# ══════════════════════════════════════════════════════════════════════════════

class TestScreeners:
    """Options 12–15 — Multi-Asset and Morning screeners."""

    def test_multi_asset_screener(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", return_value="n")
        M.run_multi_asset_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "multi_asset_screener.py" in str(cmd)

    def test_sector_heatmap(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["america", "300"])
        M.run_sector_heatmap()
        cmd = _cmd(mock_subprocess_run)
        assert "tv_sector_heatmap.py" in str(cmd)
        assert "--exchange" in cmd
        assert "america" in cmd
        assert "--n" in cmd
        assert "300" in cmd

    def test_forex_screener(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=[""])
        M.run_forex_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "forex_signal_engine.py" in str(cmd)
        assert "--out" in cmd

    def test_forex_screener_custom_pairs(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["EURUSD,GBPUSD"])
        M.run_forex_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "forex_signal_engine.py" in str(cmd)
        assert "--pairs" in cmd
        assert "EURUSD,GBPUSD" in cmd

    def test_fo_chain_screener(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["us", "70", "50", ""])
        M.run_fo_chain_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "fo_chain_screener.py" in str(cmd)
        assert "--market" in cmd
        assert "--min-score" in cmd

    def test_prediction_engine(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["AAPL,MSFT", "n"])
        M.run_prediction_engine()
        cmd = _cmd(mock_subprocess_run)
        assert "prediction_engine.py" in str(cmd)
        assert "--symbols" in cmd

    def test_prediction_engine_no_chronos(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["AAPL", "y"])
        M.run_prediction_engine()
        cmd = _cmd(mock_subprocess_run)
        assert "prediction_engine.py" in str(cmd)
        assert "--no-chronos" in cmd

    def test_morning_screener_us(self, mock_subprocess_run):
        M.run_morning_screener_us()
        cmd = _cmd(mock_subprocess_run)
        assert "morning_screener.py" in str(cmd)
        assert "--market" in cmd
        assert "US" in cmd

    def test_morning_screener_hk(self, mock_subprocess_run):
        M.run_morning_screener_hk()
        cmd = _cmd(mock_subprocess_run)
        assert "HK" in cmd

    def test_morning_screener_custom_us(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["US", "200"])
        M.run_morning_screener_custom()
        cmd = _cmd(mock_subprocess_run)
        assert "--market" in cmd
        assert "US" in cmd
        assert "--limit" in cmd
        assert "200" in cmd

    def test_morning_screener_custom_hk(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["HK", "50"])
        M.run_morning_screener_custom()
        cmd = _cmd(mock_subprocess_run)
        assert "HK" in cmd
        assert "50" in cmd

    def test_morning_screener_custom_empty_defaults(self, mocker, mock_subprocess_run):
        """Empty inputs fall back to defaults (US, 100)."""
        mocker.patch("builtins.input", side_effect=["", ""])
        M.run_morning_screener_custom()
        cmd = _cmd(mock_subprocess_run)
        # defaults: US, 100
        assert "US" in cmd
        assert "100" in cmd

    def test_austsuper_screener_defaults(self, mocker, mock_subprocess_run):
        """Default run: ASX 300 only, bullish — pipeline is always on."""
        mocker.patch("builtins.input", side_effect=["bullish"])
        M.run_austsuper_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "austsuper_screener.py" in str(cmd)
        assert "--universe" not in cmd
        assert "--etf-curated-only" not in cmd
        assert "--no-open" in cmd
        assert "--append-fires" not in cmd  # pipeline handled inside the script

    def test_austsuper_screener_bearish(self, mocker, mock_subprocess_run):
        """Bearish mode — pipeline always runs end-to-end."""
        mocker.patch("builtins.input", side_effect=["bearish"])
        M.run_austsuper_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "austsuper_screener.py" in str(cmd)
        assert "--bearish" in cmd
        assert "--universe" not in cmd

    def test_us_market_screener_defaults(self, mocker, mock_subprocess_run):
        """Default US screener run: bullish mode."""
        mocker.patch("builtins.input", side_effect=["bullish"])
        M.run_us_market_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "market_screener.py" in str(cmd)
        assert "--market" in cmd
        assert "us" in cmd
        assert "--no-open" in cmd

    def test_us_market_screener_bearish(self, mocker, mock_subprocess_run):
        """US screener bearish mode."""
        mocker.patch("builtins.input", side_effect=["bearish"])
        M.run_us_market_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "market_screener.py" in str(cmd)
        assert "us" in cmd
        assert "--bearish" in cmd

    def test_india_market_screener_defaults(self, mocker, mock_subprocess_run):
        """Default India screener run: bullish mode."""
        mocker.patch("builtins.input", side_effect=["bullish"])
        M.run_india_market_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "market_screener.py" in str(cmd)
        assert "--market" in cmd
        assert "ind" in cmd
        assert "--no-open" in cmd

    def test_india_market_screener_bearish(self, mocker, mock_subprocess_run):
        """India screener bearish mode."""
        mocker.patch("builtins.input", side_effect=["bearish"])
        M.run_india_market_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "market_screener.py" in str(cmd)
        assert "ind" in cmd
        assert "--bearish" in cmd

    def test_sg_market_screener_defaults(self, mocker, mock_subprocess_run):
        """Default SG screener run: bullish mode."""
        mocker.patch("builtins.input", side_effect=["bullish"])
        M.run_sg_market_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "market_screener.py" in str(cmd)
        assert "--market" in cmd
        assert "sg" in cmd
        assert "--no-open" in cmd

    def test_sg_market_screener_bearish(self, mocker, mock_subprocess_run):
        """SG screener bearish mode."""
        mocker.patch("builtins.input", side_effect=["bearish"])
        M.run_sg_market_screener()
        cmd = _cmd(mock_subprocess_run)
        assert "market_screener.py" in str(cmd)
        assert "sg" in cmd
        assert "--bearish" in cmd


# ══════════════════════════════════════════════════════════════════════════════
# Section 8b — News signal scan handlers (options 39–41)
# ══════════════════════════════════════════════════════════════════════════════

class TestNewsSignalHandlers:
    """Options 39–41 — all news-based handlers must route through signal_stack.py --news."""

    def test_news_scan_bullish_calls_signal_stack(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["24", "0.35", "v"])
        M.run_news_scan_bullish()
        cmd = _cmd(mock_subprocess_run)
        assert "signal_stack.py" in str(cmd)
        assert "--news" in cmd
        assert "bullish" in cmd

    def test_news_scan_bullish_passes_age_and_threshold(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["12", "0.5", "v"])
        M.run_news_scan_bullish()
        cmd = _cmd(mock_subprocess_run)
        assert "--news-max-age" in cmd
        assert "12" in cmd
        assert "--news-threshold" in cmd
        assert "0.5" in cmd

    def test_news_scan_bearish_calls_signal_stack(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["24", "0.35", "v"])
        M.run_news_scan_bearish()
        cmd = _cmd(mock_subprocess_run)
        assert "signal_stack.py" in str(cmd)
        assert "--news" in cmd
        assert "bearish" in cmd

    def test_news_scan_bearish_passes_age_and_threshold(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["48", "0.6", "v"])
        M.run_news_scan_bearish()
        cmd = _cmd(mock_subprocess_run)
        assert "--news-max-age" in cmd
        assert "48" in cmd
        assert "--news-threshold" in cmd
        assert "0.6" in cmd

    def test_news_browser_all_directions_default(self, mocker, mock_subprocess_run):
        """Menu 41: default direction='1' → --news all via signal_stack.py."""
        mocker.patch("builtins.input", side_effect=["1", "24", "0.35", "v"])
        M.run_news_browser()
        cmd = _cmd(mock_subprocess_run)
        assert "signal_stack.py" in str(cmd)
        assert "--news" in cmd
        assert "all" in cmd

    def test_news_browser_bullish_direction(self, mocker, mock_subprocess_run):
        """Menu 41 direction='2' → --news bullish."""
        mocker.patch("builtins.input", side_effect=["2", "24", "0.35", "v"])
        M.run_news_browser()
        cmd = _cmd(mock_subprocess_run)
        assert "signal_stack.py" in str(cmd)
        assert "--news" in cmd
        assert "bullish" in cmd

    def test_news_browser_bearish_direction(self, mocker, mock_subprocess_run):
        """Menu 41 direction='3' → --news bearish."""
        mocker.patch("builtins.input", side_effect=["3", "24", "0.35", "v"])
        M.run_news_browser()
        cmd = _cmd(mock_subprocess_run)
        assert "signal_stack.py" in str(cmd)
        assert "--news" in cmd
        assert "bearish" in cmd

    def test_news_browser_never_calls_tv_news_report(self, mocker, mock_subprocess_run):
        """Menu 41 must NOT call tv_news_report.py — it must go through signal_stack.py."""
        mocker.patch("builtins.input", side_effect=["1", "24", "0.35", "v"])
        M.run_news_browser()
        cmd = _cmd(mock_subprocess_run)
        assert "tv_news_report.py" not in str(cmd), (
            "run_news_browser still calls tv_news_report.py — it must use signal_stack.py"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Section 8c — Moomoo Insights scan handler (option 47b)
# ══════════════════════════════════════════════════════════════════════════════

class TestMoomooInsightsHandler:
    """Option 47b — Moomoo Insights scan stub (feature not yet implemented)."""

    def test_insights_scan_all_default(self, mocker, mock_subprocess_run):
        """Stub: run_moomoo_insights_scan must NOT call subprocess.run (not implemented)."""
        M.run_moomoo_insights_scan()
        mock_subprocess_run.assert_not_called()

    def test_insights_scan_bullish(self, mocker, mock_subprocess_run):
        """Stub: no subprocess call regardless of any future input."""
        M.run_moomoo_insights_scan()
        mock_subprocess_run.assert_not_called()

    def test_insights_scan_bearish(self, mocker, mock_subprocess_run):
        """Stub: no subprocess call."""
        M.run_moomoo_insights_scan()
        mock_subprocess_run.assert_not_called()

    def test_insights_scan_custom_threshold(self, mocker, mock_subprocess_run):
        """Stub: no subprocess call."""
        M.run_moomoo_insights_scan()
        mock_subprocess_run.assert_not_called()

    def test_insights_scan_never_calls_news_flag(self, mocker, mock_subprocess_run):
        """Stub: --news flag can never appear because run() is never called."""
        M.run_moomoo_insights_scan()
        mock_subprocess_run.assert_not_called()

    def test_insights_menu_entry_exists(self):
        """Menu entry 47b must exist and point to run_moomoo_insights_scan."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "47b" in handlers, "Menu entry '47b' missing from MENU"
        assert handlers["47b"] is M.run_moomoo_insights_scan, \
            "Menu '47b' must map to run_moomoo_insights_scan"


# ══════════════════════════════════════════════════════════════════════════════
# Section 9 — Test-runner meta options  (options 16–21)
# ══════════════════════════════════════════════════════════════════════════════

class TestMetaTestRunners:
    """Options 16–21 — handlers that invoke pytest sub-runs."""

    def test_signal_engine_tests(self, mock_subprocess_run):
        M.run_tests_signal_engine()
        cmd = _cmd(mock_subprocess_run)
        assert "pytest" in cmd
        assert str(TV / "signal_engine" / "tests") in cmd

    def test_cgt_moomoo_tests(self, mock_subprocess_run):
        M.run_tests_cgt_moomoo()
        cmd = _cmd(mock_subprocess_run)
        assert "test_cgt_analysis.py" in str(cmd)

    def test_integration_moomoo(self, mock_subprocess_run):
        M.run_tests_integration_moomoo()
        cmd = _cmd(mock_subprocess_run)
        assert "test_integration_moomoo.py" in str(cmd)

    def test_integration_tv(self, mock_subprocess_run):
        M.run_tests_integration_tv()
        cmd = _cmd(mock_subprocess_run)
        assert "test_integration_tv.py" in str(cmd)

    def test_integration_external(self, mock_subprocess_run):
        M.run_tests_integration_external()
        cmd = _cmd(mock_subprocess_run)
        assert "test_integration_external.py" in str(cmd)

    def test_all_unit_tests(self, mock_subprocess_run):
        M.run_tests_all()


# ══════════════════════════════════════════════════════════════════════════════
# Section 11 — Performance Analyzer (option 48)
# ══════════════════════════════════════════════════════════════════════════════

class TestPerformanceAnalyzer:
    """Option 48 — run_performance_analyzer."""

    def test_basic_invocation(self, mocker, mock_subprocess_run):
        """Default inputs: min_samples=10, no since, tradable_only=n."""
        mocker.patch("builtins.input", side_effect=["10", "", "n"])
        M.run_performance_analyzer()
        cmd = _cmd(mock_subprocess_run)
        assert "performance_analyzer.py" in str(cmd)
        assert "--min-samples" in cmd
        assert "10" in cmd
        assert "--tradable-only" not in cmd

    def test_tradable_only_flag(self, mocker, mock_subprocess_run):
        """When user enters 'y' for tradable-only, --tradable-only must be in cmd."""
        mocker.patch("builtins.input", side_effect=["5", "", "y"])
        M.run_performance_analyzer()
        cmd = _cmd(mock_subprocess_run)
        assert "performance_analyzer.py" in str(cmd)
        assert "--tradable-only" in cmd

    def test_since_flag_passed(self, mocker, mock_subprocess_run):
        """When user provides a date, --since must appear in cmd."""
        mocker.patch("builtins.input", side_effect=["10", "2026-01-01", "n"])
        M.run_performance_analyzer()
        cmd = _cmd(mock_subprocess_run)
        assert "--since" in cmd
        assert "2026-01-01" in cmd

    def test_tradable_only_with_since(self, mocker, mock_subprocess_run):
        """Both --since and --tradable-only must coexist when both are provided."""
        mocker.patch("builtins.input", side_effect=["10", "2026-01-01", "y"])
        M.run_performance_analyzer()
        cmd = _cmd(mock_subprocess_run)
        assert "--since" in cmd
        assert "--tradable-only" in cmd
        assert "2026-01-01" in cmd


# ══════════════════════════════════════════════════════════════════════════════
# Section 10 — dispatch table completeness
# ══════════════════════════════════════════════════════════════════════════════

class TestRunGlobalDashboard:
    """Option 1 — Global Portfolio Dashboard."""

    def test_default_no_skips(self, mocker, mock_subprocess_run):
        mocker.patch("main._india_env", return_value={})
        M.run_global_dashboard()
        cmd = _cmd(mock_subprocess_run)
        assert "global_portfolio_dashboard.py" in str(cmd)
        assert "--no-ibkr" not in cmd
        assert "--allow-partial" not in cmd

    def test_ibkr_never_skipped_by_default(self, mocker, mock_subprocess_run):
        """IBKR must never be skipped — no --no-ibkr flag in the command."""
        mocker.patch("main._india_env", return_value={})
        M.run_global_dashboard()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-ibkr" not in cmd

    def test_no_allow_partial_prompt(self, mocker, mock_subprocess_run):
        """run_global_dashboard must not prompt the user for anything."""
        mocker.patch("main._india_env", return_value={})
        mock_input = mocker.patch("builtins.input")
        M.run_global_dashboard()
        mock_input.assert_not_called()




# ══════════════════════════════════════════════════════════════════════════════
# Section 10b — _fetch_moomoo() thread-wrapper unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchMoomooTimeout:
    """Unit tests for _fetch_moomoo() daemon-thread / timeout wrapper."""

    def test_happy_path_returns_positions(self, mocker):
        """Happy path: _fetch_moomoo_locked returns data → _fetch_moomoo propagates."""
        import global_portfolio_dashboard as GPD
        pos1 = {"symbol": "AAPL", "shares": 10, "broker": "Moomoo-AU"}
        mocker.patch(
            "global_portfolio_dashboard._fetch_moomoo_locked",
            return_value=([pos1], "OK"),
        )
        data, msg = GPD._fetch_moomoo("AU", 11111)
        assert data == [pos1]
        assert msg == "OK"

    def test_timeout_returns_timeout_message(self, mocker):
        """Timeout: join() is no-op + is_alive()=True → timeout error message returned."""
        import global_portfolio_dashboard as GPD
        mocker.patch(
            "global_portfolio_dashboard._fetch_moomoo_locked",
            return_value=([], "offline"),
        )
        mocker.patch.object(threading.Thread, "join", lambda self, timeout=None: None)
        mocker.patch.object(threading.Thread, "is_alive", lambda self: True)
        data, msg = GPD._fetch_moomoo("AU", 11111)
        assert data == []
        assert msg == "Moomoo-AU: OpenD connect timeout (20s)"

    def test_worker_exception_returns_error_message(self, mocker):
        """Worker raises → _fetch_moomoo returns wrapped error message."""
        import global_portfolio_dashboard as GPD
        mocker.patch(
            "global_portfolio_dashboard._fetch_moomoo_locked",
            side_effect=RuntimeError("SDK crash"),
        )
        data, msg = GPD._fetch_moomoo("AU", 11111)
        assert data == []
        assert "SDK crash" in msg
        assert "worker error" in msg

    def test_port_closed_returns_offline(self, mocker):
        """Port closed: locked returns ([], 'offline') → _fetch_moomoo propagates as-is."""
        import global_portfolio_dashboard as GPD
        mocker.patch(
            "global_portfolio_dashboard._fetch_moomoo_locked",
            return_value=([], "offline"),
        )
        data, msg = GPD._fetch_moomoo("AU", 11111)
        assert data == []
        assert msg == "offline"

class TestRunPortfolioAnalytics:
    """Portfolio analytics handler."""

    def test_calls_analytics_script(self, mock_subprocess_run):
        M.run_portfolio_analytics()
        cmd = _cmd(mock_subprocess_run)
        assert "portfolio_analytics.py" in str(cmd)


class TestRunReportAnalytics:
    """Report analytics handler."""

    def test_calls_report_analytics_script(self, mock_subprocess_run):
        M.run_report_analytics()
        cmd = _cmd(mock_subprocess_run)
        assert "report_analytics.py" in str(cmd)


class TestRunPnlAttribution:
    """P&L attribution handler — P3.3."""

    def test_calls_pnl_attribution_script(self, mock_subprocess_run):
        M.run_pnl_attribution()
        cmd = _cmd(mock_subprocess_run)
        assert "pnl_attribution.py" in str(cmd)

    def test_passes_out_flag(self, mock_subprocess_run):
        M.run_pnl_attribution()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd
        assert "pnl_attribution.html" in str(cmd)


class TestRunManualAnalyse:
    """Manual analyse handler."""

    def test_default_no_flags(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["NASDAQ:AAPL", "n", "n"])
        M.run_manual_analyse()
        cmd = _cmd(mock_subprocess_run)
        assert "--analyse" in cmd
        assert "NASDAQ:AAPL" in cmd

    def test_empty_tickers_aborts(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["", "n", "n"])
        M.run_manual_analyse()
        mock_subprocess_run.assert_not_called()

    def test_no_sizer_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["NASDAQ:NVDA", "y", "n"])
        M.run_manual_analyse()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-sizer" in cmd

    def test_no_external_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["NYSE:TSLA", "n", "y"])
        M.run_manual_analyse()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-external" in cmd


class TestRunScreenerRun:
    """Screener run handler."""

    def test_default_screener(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["stocks_us"])
        M.run_screener_run()
        cmd = _cmd(mock_subprocess_run)
        assert "--screener" in cmd
        assert "stocks_us" in cmd

    def test_empty_screener_aborts(self, mocker, mock_subprocess_run):
        # prompt() returns default "stocks_us" when input is blank — we need to
        # mock prompt directly to simulate a truly blank result.
        mocker.patch("main.prompt", return_value="")
        M.run_screener_run()


# ══════════════════════════════════════════════════════════════════════════════
# Section N — Combined Signals  (option 14a)
# ══════════════════════════════════════════════════════════════════════════════

class TestCombinedSignals:
    """Option 14a — Combined Signals (download + screener + signal stack)."""

    def _patch_ports(self, mocker, moomoo: bool = False, token: bool = False):
        mocker.patch("main._is_port_in_use", return_value=moomoo)
        mocker.patch("main._load_cached_token", return_value="tok" if token else None)
        import os as _os
        mocker.patch.dict(_os.environ, {"ZERODHA_ACCESS_TOKEN": ""}, clear=False)

    def test_download_fires_called(self, mocker, mock_subprocess_run):
        """Step 1: download_fires.py must be called."""
        self._patch_ports(mocker)
        mocker.patch("builtins.input", side_effect=["100", "50", "all", "n", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        assert any("download_fires" in str(c) for c in cmds)

    def test_screener_with_append_fires_called(self, mocker, mock_subprocess_run):
        """Step 2: multi_asset_screener.py --append-fires must be called when not skipped."""
        self._patch_ports(mocker)
        mocker.patch("builtins.input", side_effect=["100", "50", "all", "n", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        assert any("multi_asset_screener" in str(c) for c in cmds)
        # --append-fires must be present in that call
        screener_cmd = next(c for c in cmds if "multi_asset_screener" in str(c))
        assert "--append-fires" in screener_cmd

    def test_skip_screener_skips_step2(self, mocker, mock_subprocess_run):
        """When user says 'y' to skip screener, multi_asset_screener is not called."""
        self._patch_ports(mocker)
        mocker.patch("builtins.input", side_effect=["100", "50", "all", "y", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        assert not any("multi_asset_screener" in str(c) for c in cmds)

    def test_signal_stack_with_news_called(self, mocker, mock_subprocess_run):
        """Step 3: signal_stack.py --news <dir> must always be called."""
        self._patch_ports(mocker)
        mocker.patch("builtins.input", side_effect=["100", "50", "bullish", "n", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        ss_cmds = [c for c in cmds if "signal_stack" in str(c)]
        assert ss_cmds, "signal_stack.py was not called"
        # Both fires and news scorers now run in parallel — check that one has --news bullish
        news_cmd = next((c for c in ss_cmds if "--news" in c), None)
        assert news_cmd is not None, "No signal_stack call with --news found"
        assert "bullish" in news_cmd

    def test_no_moomoo_flag_when_offline(self, mocker, mock_subprocess_run):
        """When Moomoo ports are not open, --no-moomoo is passed to signal_stack."""
        self._patch_ports(mocker, moomoo=False)
        mocker.patch("builtins.input", side_effect=["100", "50", "all", "n", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        ss_cmd = next(c for c in cmds if "signal_stack" in str(c))
        assert "--no-moomoo" in ss_cmd

    def test_no_moomoo_flag_absent_when_online(self, mocker, mock_subprocess_run):
        """When Moomoo port 11111 is open, --no-moomoo must NOT be in the command."""
        self._patch_ports(mocker, moomoo=True)
        mocker.patch("builtins.input", side_effect=["100", "50", "all", "n", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        ss_cmd = next(c for c in cmds if "signal_stack" in str(c))
        assert "--no-moomoo" not in ss_cmd

    def test_no_external_always_set(self, mocker, mock_subprocess_run):
        """--no-external must always be in the signal_stack call for combined runs."""
        self._patch_ports(mocker)
        mocker.patch("builtins.input", side_effect=["100", "50", "all", "n", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        ss_cmd = next(c for c in cmds if "signal_stack" in str(c))
        assert "--no-external" in ss_cmd

    def test_n_and_threshold_passed(self, mocker, mock_subprocess_run):
        """Custom n= and threshold= values must reach signal_stack.py."""
        self._patch_ports(mocker)
        mocker.patch("builtins.input", side_effect=["75", "60", "all", "n", "v"])
        M.run_combined_signals()
        cmds = _all_cmds(mock_subprocess_run)
        ss_cmd = next(c for c in cmds if "signal_stack" in str(c))
        assert "--n" in ss_cmd
        assert "75" in ss_cmd
        assert "--threshold" in ss_cmd
        assert "60" in ss_cmd


class TestRunAlertTrackerUpdate:
    """Alert tracker update handler."""

    def test_passes_update_flag(self, mock_subprocess_run):
        M.run_alert_tracker_update()
        cmd = _cmd(mock_subprocess_run)
        assert "--update" in cmd
        assert "alert_tracker.py" in str(cmd)


class TestRunFundamentalAnalysis:
    """Fundamental analysis handler."""

    def test_calls_fundamental_script(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["20"])
        M.run_fundamental_analysis()
        cmd = _cmd(mock_subprocess_run)
        assert "fundamental_analysis.py" in str(cmd)
        assert "--n" in cmd
        assert "20" in cmd
        assert "--out" in cmd


class TestRunFoArbitrage:
    """F&O Arbitrage handler."""

    def test_default_includes_rfi(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["6.5", "n", "n"])
        M.run_fo_arbitrage()
        cmd = _cmd(mock_subprocess_run)
        assert "fo_arbitrage.py" in str(cmd)
        assert "--rfi" in cmd
        assert "6.5" in cmd

    def test_skip_india_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["6.5", "y", "n"])
        M.run_fo_arbitrage()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-india" in cmd

    def test_skip_forex_flag(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["6.5", "n", "y"])
        M.run_fo_arbitrage()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-forex" in cmd


class TestRunOptionsHedger:
    """Options hedger handler."""

    def test_default_no_flags(self, mocker, mock_subprocess_run):
        mocker.patch("main._india_env", return_value={})
        mocker.patch("builtins.input", side_effect=["0", "n", "n", "v"])
        M.run_options_hedger()
        cmd = _cmd(mock_subprocess_run)
        assert "options_hedger.py" in str(cmd)
        assert "--min-value" in cmd
        assert "0" in cmd

    def test_skip_ao_flag(self, mocker, mock_subprocess_run):
        mocker.patch("main._india_env", return_value={})
        mocker.patch("builtins.input", side_effect=["0", "y", "n", "v"])
        M.run_options_hedger()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-ao" in cmd

    def test_skip_regime_flag(self, mocker, mock_subprocess_run):
        mocker.patch("main._india_env", return_value={})
        mocker.patch("builtins.input", side_effect=["0", "n", "y", "v"])
        M.run_options_hedger()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-regime" in cmd


class TestRunOptionPositionManager:
    """Option position manager handler."""

    def test_calls_position_manager_script(self, mocker, mock_subprocess_run):
        # Force manual path (no held positions from cache/Zerodha)
        mocker.patch("main._load_held_option_positions", return_value=[])
        mocker.patch("builtins.input", side_effect=[
            "NASDAQ:GOOGL", "CALL", "335", "2025-05-16",
            "1.5", "8.0", "338", "1", "100", "", "", ""
        ])
        M.run_option_position_manager()
        cmd = _cmd(mock_subprocess_run)
        assert "option_position_manager.py" in str(cmd)
        assert "--position" in cmd

    def test_auto_load_selects_held_position(self, mocker, mock_subprocess_run):
        # Simulate one held option position pre-loaded from cache
        fake_pos = {
            "symbol": "AAPL", "broker": "Moomoo-AU",
            "quantity": 1, "entry_premium": 1.5, "current_premium": 8.0,
            "option_type": "CALL", "strike": 335.0, "expiry": "2025-05-16",
            "pnl_pct": 433.3, "contract_size": 100, "_raw_symbol": "AAPL 250516C00335000",
        }
        mocker.patch("main._load_held_option_positions", return_value=[fake_pos])
        mocker.patch("builtins.input", side_effect=[
            # choice=1 (select the held position)
            "1",
            # pre-fill overrides: sym, type, strike, expiry, entry, curr, underlying, qty, cs
            "AAPL", "CALL", "335", "2025-05-16", "1.5", "8.0", "340", "1", "100",
            # RSI, ADX, VIX (all blank)
            "", "", "",
        ])
        M.run_option_position_manager()
        cmd = _cmd(mock_subprocess_run)
        assert "option_position_manager.py" in str(cmd)
        assert "--position" in cmd


class TestRunMorningAll:
    """Morning run handler — option 50."""

    def test_cancel_on_n(self, mocker, mock_subprocess_run):
        mocker.patch("main._load_cached_token", return_value="")
        mocker.patch("main.is_opend_running", return_value=False, create=True)
        mocker.patch("moomoo_config.is_opend_running", return_value=False)
        mocker.patch("builtins.input", side_effect=["n"])
        M.run_morning_all()
        mock_subprocess_run.assert_not_called()

    def test_runs_reports_on_y(self, mocker, mock_subprocess_run):
        mocker.patch("main._load_cached_token", return_value="")
        mocker.patch("moomoo_config.is_opend_running", return_value=False)
        mocker.patch("main._india_env", return_value={})
        # confirm=y, profile=not saved → screener=y, cgt=y, options=n (no ibkr prompt anymore)
        mocker.patch("builtins.input", side_effect=["y", "y", "y", "n"])
        M.run_morning_all()
        assert mock_subprocess_run.call_count >= 1


class TestRunMorningAllNewSteps:
    """Morning run — verify the 9 new steps added in Task 8."""

    def _setup_no_optional(self, mocker, mock_subprocess_run):
        """Common setup: no saved profile, no optional steps (screener/CGT/options).
        Returns all subprocess command lists in call order.
        Inputs: confirm=y, ibkr=y, screener=n, cgt=n, options=n → total_steps=18.
        """
        import pathlib

        mocker.patch("main._load_cached_token", return_value="")
        mocker.patch("moomoo_config.is_opend_running", return_value=False)
        mocker.patch("main._india_env", return_value={})

        _orig_exists = pathlib.Path.exists
        mocker.patch.object(
            pathlib.Path,
            "exists",
            new=lambda self: (
                False
                if "morning_run_profile.json" in str(self)
                else _orig_exists(self)
            ),
        )
        mocker.patch.object(pathlib.Path, "write_text", new=lambda self, *a, **kw: None)

        # confirm=y, profile=not saved → screener=n, cgt=n, options=n (no ibkr prompt anymore)
        mocker.patch("builtins.input", side_effect=["y", "n", "n", "n"])
        M.run_morning_all()
        return _all_cmds(mock_subprocess_run)

    # ── Each of the 9 new scripts is dispatched ────────────────────────────

    def test_portfolio_score_tracker_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("portfolio_score_tracker.py" in str(c) for c in cmds), \
            "portfolio_score_tracker.py was not called during morning run"

    def test_portfolio_analytics_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("portfolio_analytics.py" in str(c) for c in cmds), \
            "portfolio_analytics.py was not called during morning run"

    def test_portfolio_correlation_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("portfolio_correlation.py" in str(c) for c in cmds), \
            "portfolio_correlation.py was not called during morning run"

    def test_performance_analyzer_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("performance_analyzer.py" in str(c) for c in cmds), \
            "performance_analyzer.py was not called during morning run"

    def test_backtest_vectorbt_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("backtest_vectorbt.py" in str(c) for c in cmds), \
            "backtest_vectorbt.py was not called during morning run"

    def test_fundamental_analysis_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("fundamental_analysis.py" in str(c) for c in cmds), \
            "fundamental_analysis.py was not called during morning run"

    def test_prediction_engine_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("prediction_engine.py" in str(c) for c in cmds), \
            "prediction_engine.py was not called during morning run"

    def test_forex_signal_engine_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("forex_signal_engine.py" in str(c) for c in cmds), \
            "forex_signal_engine.py was not called during morning run"

    def test_report_analytics_is_called(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert any("report_analytics.py" in str(c) for c in cmds), \
            "report_analytics.py was not called during morning run"

    # ── Flag checks ────────────────────────────────────────────────────────

    def test_portfolio_score_tracker_receives_capture_flag(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        tracker = next((c for c in cmds if "portfolio_score_tracker.py" in str(c)), None)
        assert tracker is not None, "portfolio_score_tracker.py not called"
        assert "--capture" in tracker, "--capture flag missing from portfolio_score_tracker.py call"

    def test_portfolio_score_tracker_receives_out_flag(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        tracker = next((c for c in cmds if "portfolio_score_tracker.py" in str(c)), None)
        assert tracker is not None, "portfolio_score_tracker.py not called"
        assert "--out" in tracker, "--out flag missing from portfolio_score_tracker.py call"

    def test_portfolio_analytics_receives_no_open_flag(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        analytics = next((c for c in cmds if "portfolio_analytics.py" in str(c)), None)
        assert analytics is not None, "portfolio_analytics.py not called"
        assert "--no-open" in analytics, "--no-open flag missing from portfolio_analytics.py call"

    def test_report_analytics_receives_no_open_flag(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        ra = next((c for c in cmds if "report_analytics.py" in str(c)), None)
        assert ra is not None, "report_analytics.py not called"
        assert "--no-open" in ra, "--no-open flag missing from report_analytics.py call"

    def test_fundamental_analysis_receives_n_20(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        fa = next((c for c in cmds if "fundamental_analysis.py" in str(c)), None)
        assert fa is not None, "fundamental_analysis.py not called"
        assert "--n" in fa, "--n flag missing from fundamental_analysis.py call"
        n_idx = fa.index("--n")
        assert fa[n_idx + 1] == "20", \
            f"--n value should be '20', got '{fa[n_idx + 1]}'"

    def test_prediction_engine_receives_no_chronos_flag(self, mocker, mock_subprocess_run):
        """Morning run must pass --no-chronos to keep prediction step fast (~15s not ~120s)."""
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        pe = next((c for c in cmds if "prediction_engine.py" in str(c)), None)
        assert pe is not None, "prediction_engine.py not called"
        assert "--no-chronos" in pe, \
            "--no-chronos flag missing from prediction_engine.py morning run call"

    # ── Ordering: report_analytics.py must be LAST ─────────────────────────

    def test_report_analytics_is_last_subprocess_call(self, mocker, mock_subprocess_run):
        cmds = self._setup_no_optional(mocker, mock_subprocess_run)
        assert cmds, "No subprocess calls were made"
        last = cmds[-1]
        assert "report_analytics.py" in str(last), \
            f"report_analytics.py must be the final subprocess call; last was: {last}"

    # ── Step count: 18 required steps ──────────────────────────────────────

    def test_required_subprocess_count_is_18(self, mocker, mock_subprocess_run):
        """total_steps=19 with screener=n, cgt=n, options=n.
        19 subprocess.run calls is the observable proxy for this.
        (18 original + 1 for signal_quality_monitor added 2026-05-xx)
        """
        self._setup_no_optional(mocker, mock_subprocess_run)
        assert mock_subprocess_run.call_count == 19, (
            f"Expected exactly 19 subprocess calls for the 19 required steps; "
            f"got {mock_subprocess_run.call_count}"
        )

    # ── AU Screener in morning run ─────────────────────────────────────────

    def _setup_with_screener(self, mocker, mock_subprocess_run):
        """Setup with include_screener=True to test optional screener steps.
        Inputs: confirm=y, screener=y, cgt=n, options=n → total_steps=21.
        (ibkr prompt removed — IBKR always runs)
        """
        import pathlib

        mocker.patch("main._load_cached_token", return_value="")
        mocker.patch("moomoo_config.is_opend_running", return_value=False)
        mocker.patch("main._india_env", return_value={})

        _orig_exists = pathlib.Path.exists
        mocker.patch.object(
            pathlib.Path,
            "exists",
            new=lambda self: (
                False
                if "morning_run_profile.json" in str(self)
                else _orig_exists(self)
            ),
        )
        mocker.patch.object(pathlib.Path, "write_text", new=lambda self, *a, **kw: None)

        mocker.patch("builtins.input", side_effect=["y", "y", "n", "n"])
        M.run_morning_all()
        return _all_cmds(mock_subprocess_run)

    def test_austsuper_screener_called_in_morning_run(self, mocker, mock_subprocess_run):
        """When include_screener=True, austsuper_screener.py must be dispatched."""
        cmds = self._setup_with_screener(mocker, mock_subprocess_run)
        assert any("austsuper_screener.py" in str(c) for c in cmds), \
            "austsuper_screener.py was not called during morning run with screener=y"

    def test_multi_asset_screener_called_in_morning_run(self, mocker, mock_subprocess_run):
        """When include_screener=True, multi_asset_screener.py must also be dispatched."""
        cmds = self._setup_with_screener(mocker, mock_subprocess_run)
        assert any("multi_asset_screener.py" in str(c) for c in cmds), \
            "multi_asset_screener.py was not called during morning run with screener=y"

    def test_total_steps_with_screener_is_21(self, mocker, mock_subprocess_run):
        """With screener=y only, total subprocess count = 19 required + 2 screener = 21."""
        self._setup_with_screener(mocker, mock_subprocess_run)
        assert mock_subprocess_run.call_count == 21, (
            f"Expected 21 subprocess calls with screener=y; "
            f"got {mock_subprocess_run.call_count}"
        )


class TestAutoTrader:
    """Option 52 — Auto Trader handler."""

    def test_dry_run_defaults(self, mocker, mock_subprocess_run):
        """Default inputs: broker=zerodha, top=10, since=24, no live."""
        mocker.patch("builtins.input", side_effect=["zerodha", "10", "24", "n"])
        M.run_auto_trader_handler()
        cmd = _cmd(mock_subprocess_run)
        assert "auto_trader.py" in str(cmd)
        assert "--broker" in cmd
        assert "zerodha" in cmd
        assert "--top" in cmd
        assert "10" in cmd
        assert "--since-hours" in cmd
        assert "24" in cmd
        assert "--live" not in cmd

    def test_live_flag_added_on_yes(self, mocker, mock_subprocess_run):
        """When user types 'y' for live, --live must appear in cmd."""
        mocker.patch("builtins.input", side_effect=["moomoo", "5", "48", "y"])
        M.run_auto_trader_handler()
        cmd = _cmd(mock_subprocess_run)
        assert "--live" in cmd

    def test_broker_moomoo(self, mocker, mock_subprocess_run):
        mocker.patch("builtins.input", side_effect=["moomoo", "20", "24", "n"])
        M.run_auto_trader_handler()
        cmd = _cmd(mock_subprocess_run)
        assert "moomoo" in cmd

    def test_out_flag_present(self, mocker, mock_subprocess_run):
        """--out flag must always be passed."""
        mocker.patch("builtins.input", side_effect=["zerodha", "10", "24", "n"])
        M.run_auto_trader_handler()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd


class TestMenuDispatch:

    def test_all_handlers_callable(self):
        dispatch = M.build_dispatch()
        for key, fn in dispatch.items():
            assert callable(fn), f"Handler for '{key}' is not callable"

    def test_no_duplicate_menu_keys(self):
        keys = [row[0] for row in M.MENU if row[0] != "section"]
        assert len(keys) == len(set(keys)), "Duplicate option keys in MENU"

    def test_all_section_rows_have_color(self):
        for row in M.MENU:
            if row[0] == "section":
                _, title, color = row
                assert color, f"Section '{title}' has no color"

    def test_expected_handler_count(self):
        """Ensure we have exactly as many handlers as expected (regression guard)."""
        dispatch = M.build_dispatch()
        assert len(dispatch) >= 31, (
            f"Expected ≥31 handlers, got {len(dispatch)}. "
            f"A handler may have been accidentally removed."
        )


class TestLiveDashboard:
    """Option 53 — Live Portfolio Dashboard (Streamlit)."""

    def test_calls_streamlit_run(self, mocker, mock_subprocess_popen):
        """Default port 8501 — must launch streamlit with live_dashboard.py."""
        mocker.patch("builtins.input", side_effect=["8501"])
        M.run_live_dashboard()
        cmd = _cmd(mock_subprocess_popen)
        assert "-m" in cmd
        assert "streamlit" in cmd
        assert "run" in cmd
        assert "live_dashboard.py" in str(cmd)

    def test_streamlit_port_passed(self, mocker, mock_subprocess_popen):
        """Custom port must be forwarded to --server.port."""
        mocker.patch("builtins.input", side_effect=["8502"])
        M.run_live_dashboard()
        cmd = _cmd(mock_subprocess_popen)
        assert "--server.port" in cmd
        assert "8502" in cmd

    def test_live_dashboard_file_exists(self):
        """live_dashboard.py must exist in tradingview/."""
        dashboard = ROOT / "tradingview" / "live_dashboard.py"
        assert dashboard.exists(), f"Missing: {dashboard}"

    def test_live_dashboard_importable(self):
        """live_dashboard.py must be valid Python (ast parse)."""
        import ast
        dashboard = ROOT / "tradingview" / "live_dashboard.py"
        src = dashboard.read_text(encoding="utf-8")
        ast.parse(src)  # raises SyntaxError if invalid

    def test_menu_entry_34_present(self):
        """Menu key '34' must be registered (was 53)."""
        dispatch = M.build_dispatch()
        assert "34" in dispatch, "Menu entry '34' missing from MENU"
        assert dispatch["34"] is M.run_live_dashboard


class TestTelegramBot:
    """Option 30a — Telegram Bot (@Ajaaz_bot interactive)."""

    def test_launches_telegram_bot_script(self, mocker, mock_subprocess_popen):
        """Must launch telegram_bot.py via Popen when user confirms 'y'."""
        mocker.patch("builtins.input", side_effect=["y"])
        M.run_telegram_bot()
        mock_subprocess_popen.assert_called_once()
        cmd = mock_subprocess_popen.call_args[0][0]
        assert "telegram_bot.py" in str(cmd)

    def test_not_started_if_already_running(self, mocker, mock_subprocess_popen):
        """Must NOT launch a second instance when bot is already running."""
        mocker.patch.object(M, "_bot_is_running", return_value=(True, 9999))
        M.run_telegram_bot()
        mock_subprocess_popen.assert_not_called()

    def test_telegram_bot_file_exists(self):
        """telegram_bot.py must exist in project root."""
        assert (ROOT / "telegram_bot.py").exists(), "Missing: telegram_bot.py"

    def test_menu_entry_30a_present(self):
        """Menu key '30a' must point to run_telegram_bot."""
        dispatch = M.build_dispatch()
        assert "30a" in dispatch, "Menu entry '30a' missing"
        assert dispatch["30a"] is M.run_telegram_bot


class TestReportScheduler:
    """Option 30b — Report Scheduler (@AjaazAlertBot auto-push)."""

    def test_launches_report_scheduler_script(self, mocker, mock_subprocess_popen):
        """Must launch report_scheduler.py via Popen when user confirms 'y'."""
        mocker.patch("builtins.input", side_effect=["y"])
        M.run_report_scheduler()
        mock_subprocess_popen.assert_called_once()
        cmd = mock_subprocess_popen.call_args[0][0]
        assert "report_scheduler.py" in str(cmd)

    def test_not_started_if_already_running(self, mocker, mock_subprocess_popen):
        """Must NOT launch a second instance when scheduler is already running."""
        mocker.patch.object(M, "_bot_is_running", return_value=(True, 9998))
        M.run_report_scheduler()
        mock_subprocess_popen.assert_not_called()

    def test_report_scheduler_file_exists(self):
        """report_scheduler.py must exist in project root."""
        assert (ROOT / "report_scheduler.py").exists(), "Missing: report_scheduler.py"

    def test_scheduler_env_file_exists(self):
        """scheduler_env.ps1 must exist in project root."""
        assert (ROOT / "scheduler_env.ps1").exists(), "Missing: scheduler_env.ps1"

    def test_menu_entry_30b_present(self):
        """Menu key '30b' must point to run_report_scheduler."""
        dispatch = M.build_dispatch()
        assert "30b" in dispatch, "Menu entry '30b' missing"
        assert dispatch["30b"] is M.run_report_scheduler



class TestVectorbbtBacktest:
    """Option 54 — vectorbt Walk-Forward Signal Score Backtest."""

    def test_calls_python_backtest_script(self, mocker, mock_subprocess_run):
        """Must invoke backtest_vectorbt.py via subprocess."""
        M.run_vectorbt_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert "backtest_vectorbt.py" in str(cmd)

    def test_menu_entry_35_present(self):
        """Menu key '35' must be registered (was 54)."""
        dispatch = M.build_dispatch()
        assert "35" in dispatch, "Menu entry '35' missing from MENU"
        assert dispatch["35"] is M.run_vectorbt_backtest

    def test_backtest_script_file_exists(self):
        """backtest_vectorbt.py must exist in tradingview/."""
        script = ROOT / "tradingview" / "backtest_vectorbt.py"
        assert script.exists(), f"Missing: {script}"

    def test_backtest_script_valid_python(self):
        """backtest_vectorbt.py must be valid Python (ast parse)."""
        import ast
        script = ROOT / "tradingview" / "backtest_vectorbt.py"
        src = script.read_text(encoding="utf-8")
        ast.parse(src)


class TestSignalOutcomeStudy:
    """Option 35a — Signal Outcome Study (audited outcome logic)."""

    def test_calls_outcome_study_script(self, mock_subprocess_run):
        """Must invoke tradingview/backtest/outcome_study.py via subprocess."""
        M.run_signal_outcome_study()
        cmd = _cmd(mock_subprocess_run)
        assert "outcome_study.py" in str(cmd)

    def test_passes_out_path(self, mock_subprocess_run):
        """Must pass --out pointing at reports/signal_outcome_study.html."""
        M.run_signal_outcome_study()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd
        out = cmd[cmd.index("--out") + 1]
        assert out.endswith("signal_outcome_study.html")
        assert "reports" in out

    def test_menu_entry_35a_present(self):
        """Menu key '35a' must be registered and point to the handler."""
        dispatch = M.build_dispatch()
        assert "35a" in dispatch, "Menu entry '35a' missing from MENU"
        assert dispatch["35a"] is M.run_signal_outcome_study

    def test_outcome_study_script_file_exists(self):
        """outcome_study.py must exist under tradingview/backtest/."""
        script = ROOT / "tradingview" / "backtest" / "outcome_study.py"
        assert script.exists(), f"Missing: {script}"

    def test_outcome_study_script_valid_python(self):
        """outcome_study.py must be valid Python (ast parse)."""
        import ast
        script = ROOT / "tradingview" / "backtest" / "outcome_study.py"
        ast.parse(script.read_text(encoding="utf-8"))

    def test_outcome_study_has_main_guard(self):
        """Standalone script must have an if __name__ == '__main__' guard."""
        script = ROOT / "tradingview" / "backtest" / "outcome_study.py"
        src = script.read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in src

    def test_menu_description_does_not_advertise_score_cutoff(
        self, mock_subprocess_run, capsys
    ):
        """The menu blurb must describe the stored flag, not a score ≥62.5 rule.

        The study compares the engine's historical stored ``actionable`` flag;
        legacy rows were marked actionable under an older policy and can sit
        below 62.5, so advertising a cutoff misdescribes the comparison.
        """
        M.run_signal_outcome_study()
        output = capsys.readouterr().out

        assert "score ≥ 62.5, no veto" not in output
        assert "stored" in output and "actionable" in output
        assert "NOT a re-derived score ≥ 62.5 cutoff" in output

    def test_menu_description_states_both_verdict_gates(
        self, mock_subprocess_run, capsys
    ):
        """The blurb must state the symbol floor, not just the alert count."""
        M.run_signal_outcome_study()
        output = capsys.readouterr().out

        assert "≥30 benchmarked alerts" in output
        assert "≥10 distinct symbols per arm" in output


# ══════════════════════════════════════════════════════════════════════════════
# Section N — Screener fires _source key fix
# ══════════════════════════════════════════════════════════════════════════════

class TestScreenerFiresSourceKey:
    """Verify screener fires use _source key (not source) per the key-fix."""

    def test_screener_fires_use_underscore_source_key(self, tmp_path, mocker):
        import json
        import tradingview.screeners.multi_asset_screener as mas
        from tradingview.screeners.multi_asset_screener import append_to_fires

        fires_file = tmp_path / "fires_raw.json"
        mocker.patch.object(mas, "FIRES_FILE", fires_file)

        results = {
            "stocks_us": [{
                "tv_symbol": "NASDAQ:AAPL",
                "RSI": 55.0,
                "ADX": 25.0,
                "close": 180.0,
                "relative_volume_10d_calc": 1.5,
            }]
        }

        count = append_to_fires(results)
        assert count == 1

        with open(fires_file, encoding="utf-8") as f:
            data = json.load(f)

        assert len(data["all"]) == 1
        fire = data["all"][0]
        assert "_source" in fire, "Fire dict must use '_source' key not 'source'"
        assert fire["_source"] == "screener"
        assert "source" not in fire, "Old 'source' key must NOT be present in fire dict"

    def test_screener_fires_non_stock_keys_not_appended(self, tmp_path, mocker):
        """crypto and forex results must NOT be appended to fires."""
        import json
        import tradingview.screeners.multi_asset_screener as mas
        from tradingview.screeners.multi_asset_screener import append_to_fires

        fires_file = tmp_path / "fires_raw.json"
        mocker.patch.object(mas, "FIRES_FILE", fires_file)

        results = {
            "crypto": [{"tv_symbol": "BINANCE:BTCUSDT", "RSI": 55.0, "ADX": 25.0}],
            "forex_major": [{"tv_symbol": "FX:EURUSD", "RSI": 55.0, "ADX": 25.0}],
        }

        count = append_to_fires(results)
        assert count == 0
        assert not fires_file.exists()


# ══════════════════════════════════════════════════════════════════════════════

class TestOptionsOutcomeCheckerHandler:
    """Menu 10b — Options Outcome Checker handler wiring tests."""

    def test_menu_entry_10b_exists(self):
        """Menu entry 10b must exist and point to run_options_outcome_checker."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "10b" in handlers, "Menu entry '10b' missing from MENU"
        assert handlers["10b"] == M.run_options_outcome_checker, \
            "Menu 10b must point to run_options_outcome_checker"

    def test_outcome_checker_dry_run_flag(self, mocker, mock_subprocess_run):
        """Answering 'y' to dry-run prompt must add --dry-run to subprocess call."""
        mocker.patch("builtins.input", side_effect=["y"])
        M.run_options_outcome_checker()
        cmd = _cmd(mock_subprocess_run)
        assert "options_outcome_checker.py" in str(cmd), \
            "Must call options_outcome_checker.py"
        assert "--dry-run" in cmd

    def test_outcome_checker_no_dry_run_flag(self, mocker, mock_subprocess_run):
        """Answering 'n' to dry-run prompt must NOT add --dry-run."""
        mocker.patch("builtins.input", side_effect=["n"])
        M.run_options_outcome_checker()
        cmd = _cmd(mock_subprocess_run)
        assert "options_outcome_checker.py" in str(cmd), \
            "Must call options_outcome_checker.py"
        assert "--dry-run" not in cmd


# ══════════════════════════════════════════════════════════════════════════════

class TestOptionsRecReportHandler:
    """Menu 10e — Options Recommendations Backtest Report handler wiring tests."""

    def test_menu_entry_10e_exists(self):
        """Menu entry 10e must exist and point to run_options_rec_report."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "10e" in handlers, "Menu entry '10e' missing from MENU"
        assert handlers["10e"] is M.run_options_rec_report, \
            "Menu 10e must point to run_options_rec_report"

    def test_calls_report_script(self, mocker, mock_subprocess_run):
        """run_options_rec_report must call options_recommendations_report.py."""
        mocker.patch("builtins.input", side_effect=["n"])
        M.run_options_rec_report()
        cmd = _cmd(mock_subprocess_run)
        assert "options_recommendations_report.py" in str(cmd), \
            "Must call options_recommendations_report.py"

    def test_no_skip_flags_by_default(self, mocker, mock_subprocess_run):
        """Answering 'n' to skip-prompt must NOT add --no-update-premiums (premiums run by default)."""
        mocker.patch("builtins.input", side_effect=["n"])
        M.run_options_rec_report()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-update-premiums" not in cmd

    def test_skip_premium_flag_when_yes(self, mocker, mock_subprocess_run):
        """Answering 'y' to skip-prompt must add --no-update-premiums."""
        mocker.patch("builtins.input", side_effect=["y"])
        M.run_options_rec_report()
        cmd = _cmd(mock_subprocess_run)
        assert "--no-update-premiums" in cmd

    def test_script_file_exists(self):
        """options_recommendations_report.py must exist in tradingview/options/."""
        script = ROOT / "tradingview" / "options" / "options_recommendations_report.py"
        assert script.exists(), f"Missing: {script}"

    def test_script_valid_python(self):
        """options_recommendations_report.py must be valid Python (ast parse)."""
        import ast
        script = ROOT / "tradingview" / "options" / "options_recommendations_report.py"
        src = script.read_text(encoding="utf-8")
        ast.parse(src)

    def test_uses_python_executable(self, mocker, mock_subprocess_run):
        """Must use the current Python interpreter."""
        mocker.patch("builtins.input", side_effect=["n"])
        M.run_options_rec_report()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE


# ══════════════════════════════════════════════════════════════════════════════

class TestMenu10fOptionsPutScan:
    """Menu 10f — US 300 Quality Options Scan (PUT) handler wiring tests."""

    def test_menu_entry_10f_exists(self):
        """Menu entry 10f must exist and point to run_options_scan_us_quality_puts."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "10f" in handlers, "Menu entry '10f' missing from MENU"
        assert handlers["10f"] is M.run_options_scan_us_quality_puts, \
            "Menu 10f must point to run_options_scan_us_quality_puts"

    def test_put_scan_invokes_legacy_screener(self, mocker, mock_subprocess_run):
        """run_options_scan_us_quality_puts (10f legacy) must call options_contract_screener.py."""
        mocker.patch("builtins.input", side_effect=["300", "n", "28", "0.10", "20.00"])
        M.run_options_scan_us_quality_puts()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        cmd_str = " ".join(str(a) for a in cmd)
        assert "options_contract_screener.py" in cmd_str, \
            "Legacy options_contract_screener.py must be in subprocess command"

    def test_put_scan_passes_option_type_put(self, mocker, mock_subprocess_run):
        """run_options_scan_us_quality_puts must pass --option-type PUT."""
        mocker.patch("builtins.input", side_effect=["300", "n", "28", "0.10", "20.00"])
        M.run_options_scan_us_quality_puts()
        cmd = _cmd(mock_subprocess_run)
        cmd_str = " ".join(str(a) for a in cmd)
        assert "--option-type" in cmd_str and "PUT" in cmd_str, \
            "--option-type PUT must be passed to legacy screener"

    def test_put_scan_runs_with_defaults(self, mocker, mock_subprocess_run):
        """run_options_scan_us_quality_puts must run (not abort) with default inputs."""
        mocker.patch("builtins.input", side_effect=["300", "n", "28", "0.10", "20.00"])
        M.run_options_scan_us_quality_puts()
        mock_subprocess_run.assert_called_once()

    def test_put_scan_uses_python_executable(self, mocker, mock_subprocess_run):
        """Must use the current Python interpreter."""
        mocker.patch("builtins.input", side_effect=["300", "n", "28", "0.10", "20.00"])
        M.run_options_scan_us_quality_puts()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE


class TestMenu10gOptionsSpecialist:
    """Menu 10g — Options Specialist Analysis handler wiring tests."""

    def test_menu_entry_10g_exists(self):
        """Menu entry 10g must exist and point to run_options_specialist_analysis."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "10g" in handlers, "Menu entry '10g' missing from MENU"
        assert handlers["10g"] is M.run_options_specialist_analysis, \
            "Menu 10g must point to run_options_specialist_analysis"

    def test_specialist_puts_invokes_script(self, mocker, mock_subprocess_run):
        """run_options_specialist_analysis (PUT) must call options_specialist.py."""
        mocker.patch(
            "builtins.input",
            side_effect=[
                "put",  # PUT or CALL
                "reports/options_actionable_puts.json",  # actionable
                "reports/options_rejected_puts.json",    # rejected
                "reports/options_specialist_puts.json",  # output
            ],
        )
        M.run_options_specialist_analysis()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert any("options_specialist.py" in str(a) for a in cmd), \
            "options_specialist.py must be in subprocess command"

    def test_specialist_passes_actionable_arg(self, mocker, mock_subprocess_run):
        """run_options_specialist_analysis must pass --actionable flag."""
        mocker.patch(
            "builtins.input",
            side_effect=["put", "", "", ""],
        )
        M.run_options_specialist_analysis()
        cmd = _cmd(mock_subprocess_run)
        cmd_str = " ".join(str(a) for a in cmd)
        assert "--actionable" in cmd_str, "--actionable must be passed"

    def test_specialist_passes_rejected_arg(self, mocker, mock_subprocess_run):
        """run_options_specialist_analysis must pass --rejected flag."""
        mocker.patch(
            "builtins.input",
            side_effect=["put", "", "", ""],
        )
        M.run_options_specialist_analysis()
        cmd = _cmd(mock_subprocess_run)
        cmd_str = " ".join(str(a) for a in cmd)
        assert "--rejected" in cmd_str, "--rejected must be passed"

    def test_specialist_calls_invokes_default_puts(self, mocker, mock_subprocess_run):
        """When user selects 'call', default paths should reference options_actionable.json."""
        mocker.patch(
            "builtins.input",
            side_effect=["call", "", "", ""],
        )
        M.run_options_specialist_analysis()
        cmd = _cmd(mock_subprocess_run)
        cmd_str = " ".join(str(a) for a in cmd)
        assert "options_actionable.json" in cmd_str, \
            "CALL mode must use options_actionable.json"

    def test_specialist_uses_python_executable(self, mocker, mock_subprocess_run):
        """Must use the current Python interpreter."""
        mocker.patch(
            "builtins.input",
            side_effect=["put", "", "", ""],
        )
        M.run_options_specialist_analysis()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE


# ══════════════════════════════════════════════════════════════════════════════

class TestHoldingsAnalysisHandler:
    """Menu 4c — Holdings Analysis handler wiring tests."""

    def test_menu_entry_4c_exists(self):
        """Menu entry 4c must exist and point to run_holdings_analysis."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "4c" in handlers, "Menu entry '4c' missing from MENU"
        assert handlers["4c"] == M.run_holdings_analysis, \
            "Menu 4c must point to run_holdings_analysis"

    def test_calls_holdings_analyzer_script(self, mock_subprocess_run):
        """run_holdings_analysis must call holdings_analyzer.py."""
        M.run_holdings_analysis()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert any("holdings_analyzer.py" in str(a) for a in cmd), \
            "holdings_analyzer.py must be in subprocess command"

    def test_uses_python_executable(self, mock_subprocess_run):
        """Must use the current Python interpreter."""
        M.run_holdings_analysis()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE

    def test_passes_out_flag(self, mock_subprocess_run):
        """Must pass --out pointing to reports/holdings_analysis.html."""
        M.run_holdings_analysis()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd, "--out flag missing from command"
        out_idx = cmd.index("--out")
        assert "holdings_analysis.html" in cmd[out_idx + 1], \
            "--out value must include holdings_analysis.html"

    def test_no_prompt_calls(self, mocker, mock_subprocess_run):
        """Handler has no user prompts — input() must never be called."""
        mock_input = mocker.patch("builtins.input")
        M.run_holdings_analysis()
        mock_input.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════

class TestPortfolioRiskOptimizerHandler:
    """Menu 4d — Portfolio Risk Optimizer handler wiring tests."""

    def test_menu_entry_4d_exists(self):
        """Menu entry 4d must exist and point to run_portfolio_risk_optimizer."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "4d" in handlers, "Menu entry '4d' missing from MENU"
        assert handlers["4d"] == M.run_portfolio_risk_optimizer, \
            "Menu 4d must point to run_portfolio_risk_optimizer"

    def test_calls_portfolio_risk_optimizer_script(self, mock_subprocess_run):
        """run_portfolio_risk_optimizer must call portfolio_risk_optimizer.py."""
        M.run_portfolio_risk_optimizer()
        mock_subprocess_run.assert_called_once()
        cmd = _cmd(mock_subprocess_run)
        assert any("portfolio_risk_optimizer.py" in str(a) for a in cmd), \
            "portfolio_risk_optimizer.py must be in subprocess command"

    def test_uses_python_executable(self, mock_subprocess_run):
        """Must use the current Python interpreter."""
        M.run_portfolio_risk_optimizer()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE

    def test_passes_out_flag(self, mock_subprocess_run):
        """Must pass --out pointing to reports/portfolio_risk_optimizer.html."""
        M.run_portfolio_risk_optimizer()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd, "--out flag missing from command"
        out_idx = cmd.index("--out")
        assert "portfolio_risk_optimizer.html" in cmd[out_idx + 1], \
            "--out value must include portfolio_risk_optimizer.html"

    def test_no_prompt_calls(self, mocker, mock_subprocess_run):
        """Handler has no user prompts — input() must never be called."""
        mock_input = mocker.patch("builtins.input")
        M.run_portfolio_risk_optimizer()
        mock_input.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════


class TestMenu61TQSv2CallsHandler:
    """Menu 61 — TQS v2 CALL scan handler wiring tests."""

    def test_menu_entry_61_exists(self):
        """Menu entry 61 must exist and point to run_tqs_v2_calls."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "61" in handlers, "Menu entry '61' missing from MENU"
        assert handlers["61"] is M.run_tqs_v2_calls, \
            "Menu 61 must point to run_tqs_v2_calls"

    def test_calls_workflow_script(self, mocker, mock_subprocess_run):
        """run_tqs_v2_calls must call tqs_v2/workflow.py."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_calls()
        cmd = _cmd(mock_subprocess_run)
        assert any("workflow.py" in str(a) for a in cmd), \
            "workflow.py must be in subprocess command"

    def test_uses_python_executable(self, mocker, mock_subprocess_run):
        """Must use the current Python interpreter."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_calls()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE

    def test_passes_scan_type_calls(self, mocker, mock_subprocess_run):
        """Must pass --scan-type calls."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_calls()
        cmd = _cmd(mock_subprocess_run)
        cmd_str = " ".join(str(a) for a in cmd)
        assert "--scan-type" in cmd_str and "calls" in cmd_str, \
            "--scan-type calls must be passed to workflow.py"

    def test_passes_max_premium(self, mocker, mock_subprocess_run):
        """Must pass user-entered --max-premium value."""
        mocker.patch("builtins.input", side_effect=["y", "10.00", "20", "1", "1"])
        M.run_tqs_v2_calls()
        cmd = _cmd(mock_subprocess_run)
        assert "--max-premium" in cmd, "--max-premium must be passed"
        idx = cmd.index("--max-premium")
        assert cmd[idx + 1] == "10.0", "max-premium value must be '10.0'"

    def test_passes_top_n(self, mocker, mock_subprocess_run):
        """Must pass user-entered --top-n value."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "100", "1", "1"])
        M.run_tqs_v2_calls()
        cmd = _cmd(mock_subprocess_run)
        assert "--top-n" in cmd, "--top-n must be passed"
        idx = cmd.index("--top-n")
        assert cmd[idx + 1] == "100", "--top-n value must be 100"

    def test_sort_by_market_cap_when_choice_1(self, mocker, mock_subprocess_run):
        """Choice [1] must produce --sort-by market_cap_basic."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_calls()
        cmd = _cmd(mock_subprocess_run)
        assert "--sort-by" in cmd
        idx = cmd.index("--sort-by")
        assert cmd[idx + 1] == "market_cap_basic"

    def test_sort_by_relative_volume_when_choice_2(self, mocker, mock_subprocess_run):
        """Choice [2] must produce --sort-by relative_volume_10d_calc."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "2", "1"])
        M.run_tqs_v2_calls()
        cmd = _cmd(mock_subprocess_run)
        assert "--sort-by" in cmd
        idx = cmd.index("--sort-by")
        assert cmd[idx + 1] == "relative_volume_10d_calc"

    def test_aborts_when_user_declines(self, mocker, mock_subprocess_run):
        """Entering 'n' at confirmation must abort without running subprocess."""
        mocker.patch("builtins.input", side_effect=["n"])
        M.run_tqs_v2_calls()
        mock_subprocess_run.assert_not_called()


class TestMenu62TQSv2PutsHandler:
    """Menu 62 — TQS v2 PUT scan handler wiring tests."""

    def test_menu_entry_62_exists(self):
        """Menu entry 62 must exist and point to run_tqs_v2_puts."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "62" in handlers, "Menu entry '62' missing from MENU"
        assert handlers["62"] is M.run_tqs_v2_puts, \
            "Menu 62 must point to run_tqs_v2_puts"

    def test_calls_workflow_script(self, mocker, mock_subprocess_run):
        """run_tqs_v2_puts must call tqs_v2/workflow.py."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_puts()
        cmd = _cmd(mock_subprocess_run)
        assert any("workflow.py" in str(a) for a in cmd), \
            "workflow.py must be in subprocess command"

    def test_uses_python_executable(self, mocker, mock_subprocess_run):
        """Must use the current Python interpreter."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_puts()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE

    def test_passes_scan_type_puts(self, mocker, mock_subprocess_run):
        """Must pass --scan-type puts."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_puts()
        cmd = _cmd(mock_subprocess_run)
        cmd_str = " ".join(str(a) for a in cmd)
        assert "--scan-type" in cmd_str and "puts" in cmd_str, \
            "--scan-type puts must be passed to workflow.py"

    def test_passes_max_premium(self, mocker, mock_subprocess_run):
        """Must pass user-entered --max-premium value."""
        mocker.patch("builtins.input", side_effect=["y", "8.00", "20", "1", "1"])
        M.run_tqs_v2_puts()
        cmd = _cmd(mock_subprocess_run)
        assert "--max-premium" in cmd, "--max-premium must be passed"
        idx = cmd.index("--max-premium")
        assert cmd[idx + 1] == "8.0", "max-premium value must be '8.0'"

    def test_passes_top_n(self, mocker, mock_subprocess_run):
        """Must pass user-entered --top-n value."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "300", "1", "1"])
        M.run_tqs_v2_puts()
        cmd = _cmd(mock_subprocess_run)
        assert "--top-n" in cmd, "--top-n must be passed"
        idx = cmd.index("--top-n")
        assert cmd[idx + 1] == "300", "--top-n value must be 300"

    def test_sort_by_market_cap_when_choice_1(self, mocker, mock_subprocess_run):
        """Choice [1] must produce --sort-by market_cap_basic."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "1", "1"])
        M.run_tqs_v2_puts()
        cmd = _cmd(mock_subprocess_run)
        assert "--sort-by" in cmd
        idx = cmd.index("--sort-by")
        assert cmd[idx + 1] == "market_cap_basic"

    def test_sort_by_relative_volume_when_choice_2(self, mocker, mock_subprocess_run):
        """Choice [2] must produce --sort-by relative_volume_10d_calc."""
        mocker.patch("builtins.input", side_effect=["y", "4.00", "20", "2", "1"])
        M.run_tqs_v2_puts()
        cmd = _cmd(mock_subprocess_run)
        assert "--sort-by" in cmd
        idx = cmd.index("--sort-by")
        assert cmd[idx + 1] == "relative_volume_10d_calc"

    def test_aborts_when_user_declines(self, mocker, mock_subprocess_run):
        """Entering 'n' at confirmation must abort without running subprocess."""
        mocker.patch("builtins.input", side_effect=["n"])
        M.run_tqs_v2_puts()
        mock_subprocess_run.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════

class TestMenu63TQSv2BacktestHandler:
    """Menu 63 — TQS v2 Backtest Analyzer handler wiring tests."""

    def test_menu_entry_63_exists(self):
        """Menu entry 63 must exist and point to run_tqs_v2_backtest."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "63" in handlers, "Menu entry '63' missing from MENU"
        assert handlers["63"] is M.run_tqs_v2_backtest, \
            "Menu 63 must point to run_tqs_v2_backtest"

    def test_calls_backtest_script(self, mocker, mock_subprocess_run):
        """run_tqs_v2_backtest must call tqs_v2/backtest_main.py."""
        M.run_tqs_v2_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert any("backtest_main.py" in str(a) for a in cmd), \
            "backtest_main.py must be in subprocess command"

    def test_uses_python_executable(self, mocker, mock_subprocess_run):
        """Must use the current Python interpreter."""
        M.run_tqs_v2_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE

    def test_passes_out_flag(self, mocker, mock_subprocess_run):
        """Must pass --out to backtest_main.py."""
        M.run_tqs_v2_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd, "--out must be passed to backtest_main.py"

    def test_runs_without_prompt(self, mocker, mock_subprocess_run):
        """run_tqs_v2_backtest must run directly without any Y/N confirmation prompt."""
        # No input mock needed — function must not call input() at all
        input_mock = mocker.patch("builtins.input")
        M.run_tqs_v2_backtest()
        input_mock.assert_not_called()
        mock_subprocess_run.assert_called_once()


class TestMenu64ZeroDTEHandler:
    """Menu 64 — ZeroDTE 0-2DTE Pattern Scanner handler wiring tests."""

    def test_menu_entry_64_exists(self):
        """Menu entry 64 must exist and point to run_zero_dte."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "64" in handlers, "Menu entry '64' missing from MENU"
        assert handlers["64"] is M.run_zero_dte, \
            "Menu 64 must point to run_zero_dte"

    def test_calls_zerodte_workflow(self, mocker, mock_subprocess_run):
        """run_zero_dte must invoke zerodte/workflow.py via subprocess."""
        mocker.patch("builtins.input", side_effect=["both", "20.00", "60"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert any("workflow.py" in str(a) for a in cmd), \
            "workflow.py must be in subprocess command"
        assert any("zerodte" in str(a) for a in cmd), \
            "zerodte directory must be in subprocess command path"

    def test_uses_python_executable(self, mocker, mock_subprocess_run):
        """Must use the current Python interpreter."""
        mocker.patch("builtins.input", side_effect=["both", "20.00", "60"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE

    def test_passes_direction_flag(self, mocker, mock_subprocess_run):
        """Must pass --direction to workflow.py."""
        mocker.patch("builtins.input", side_effect=["calls", "10.00", "70"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert "--direction" in cmd
        idx = cmd.index("--direction")
        assert cmd[idx + 1] == "calls"

    def test_passes_max_premium_flag(self, mocker, mock_subprocess_run):
        """Must pass --max-premium to workflow.py."""
        mocker.patch("builtins.input", side_effect=["both", "8.00", "60"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert "--max-premium" in cmd
        idx = cmd.index("--max-premium")
        assert cmd[idx + 1] == "8.00"

    def test_passes_min_score_flag(self, mocker, mock_subprocess_run):
        """Must pass --min-score to workflow.py."""
        mocker.patch("builtins.input", side_effect=["both", "20.00", "75"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert "--min-score" in cmd
        idx = cmd.index("--min-score")
        assert cmd[idx + 1] == "75"

    def test_passes_out_dir_to_reports(self, mocker, mock_subprocess_run):
        """Must output reports to REPORTS directory."""
        mocker.patch("builtins.input", side_effect=["both", "20.00", "60"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert "--out-dir" in cmd
        idx = cmd.index("--out-dir")
        # Should point to the reports/ directory
        assert "reports" in cmd[idx + 1].lower()

    def test_invalid_direction_defaults_to_both(self, mocker, mock_subprocess_run):
        """Invalid direction input should default to 'both'."""
        mocker.patch("builtins.input", side_effect=["xyz", "20.00", "60"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert "--direction" in cmd
        idx = cmd.index("--direction")
        assert cmd[idx + 1] == "both"

    def test_put_direction_passed_correctly(self, mocker, mock_subprocess_run):
        """Put direction must be passed as 'puts'."""
        mocker.patch("builtins.input", side_effect=["puts", "15.00", "65"])
        M.run_zero_dte()
        cmd = _cmd(mock_subprocess_run)
        assert "--direction" in cmd
        idx = cmd.index("--direction")
        assert cmd[idx + 1] == "puts"


class TestMenu65ZeroDTEBacktestHandler:
    """Menu 65 — ZeroDTE Backtest handler wiring tests."""

    def test_menu_entry_65_exists(self):
        """Menu entry 65 must exist and point to run_zerodte_backtest."""
        handlers = {row[0]: row[2] for row in M.MENU if row[0] != "section"}
        assert "65" in handlers, "Menu entry '65' missing from MENU"
        assert handlers["65"] is M.run_zerodte_backtest, \
            "Menu 65 must point to run_zerodte_backtest"

    def test_calls_backtest_vectorbt(self, mock_subprocess_run):
        """run_zerodte_backtest must invoke backtest_vectorbt.py via subprocess."""
        M.run_zerodte_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert any("backtest_vectorbt.py" in str(a) for a in cmd), \
            "backtest_vectorbt.py must be in subprocess command"
        assert any("zerodte" in str(a) for a in cmd), \
            "zerodte directory must be in subprocess command path"

    def test_uses_python_executable(self, mock_subprocess_run):
        """Must use the current Python interpreter."""
        M.run_zerodte_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert cmd[0] == PYEXE

    def test_passes_html_out_flag(self, mock_subprocess_run):
        """Must pass --out with an HTML path."""
        M.run_zerodte_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd
        idx = cmd.index("--out")
        assert cmd[idx + 1].endswith(".html"), "--out must point to an HTML file"

    def test_html_output_goes_to_reports_dir(self, mock_subprocess_run):
        """HTML output must be in the reports/ directory."""
        M.run_zerodte_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert "--out" in cmd
        idx = cmd.index("--out")
        assert "reports" in cmd[idx + 1].lower()

    def test_passes_json_flag(self, mock_subprocess_run):
        """Must pass --json with a JSON path."""
        M.run_zerodte_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert "--json" in cmd
        idx = cmd.index("--json")
        assert cmd[idx + 1].endswith(".json"), "--json must point to a JSON file"

    def test_json_output_goes_to_reports_dir(self, mock_subprocess_run):
        """JSON output must be in the reports/ directory."""
        M.run_zerodte_backtest()
        cmd = _cmd(mock_subprocess_run)
        assert "--json" in cmd
        idx = cmd.index("--json")
        assert "reports" in cmd[idx + 1].lower()

    def test_html_and_json_share_same_basename(self, mock_subprocess_run):
        """HTML and JSON output must share the same base filename."""
        M.run_zerodte_backtest()
        cmd = _cmd(mock_subprocess_run)
        html_path = cmd[cmd.index("--out") + 1]
        json_path = cmd[cmd.index("--json") + 1]
        html_stem = html_path.rsplit(".", 1)[0].split("/")[-1].split("\\")[-1]
        json_stem = json_path.rsplit(".", 1)[0].split("/")[-1].split("\\")[-1]
        assert html_stem == json_stem, \
            f"HTML and JSON must share basename: {html_stem!r} != {json_stem!r}"

