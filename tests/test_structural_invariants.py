"""
test_structural_invariants.py
─────────────────────────────
STRUCTURAL INVARIANT TESTS  — the "sealed dashboard" pattern applied to the
whole codebase. These tests exist specifically because agents with context loss
have silently deleted handlers/functions that took hours to write.

If ANY of these fail, the codebase has regressed to a broken state.
These tests are intentionally simple: they only check *existence*, not behaviour.

Git history of circular fixes that motivated each guard:
  MENU 4c/4d    — deleted by agent on 2026-05-17 (2nd restore)
  run_signal_stack "idle_streak"   — removed 2026-05-11, restored same day
  _fetch_ws_fields idle-streak     — removed 2026-05-17
  backtest_vectorbt DEEP_LOSS etc  — 277-line silent deletion commit fe78dc5
  run_holdings_analysis            — 1st delete→restore df6548c, 2nd delete→2026-05-17

DO NOT DELETE OR MODIFY THESE TESTS without explicit user instruction.
"""
from __future__ import annotations
import ast
import importlib
import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "main.py"
TV = ROOT / "tradingview"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — main.py handler presence (88 handlers)
# Guards: each run_* handler that was historically deleted and restored.
# ─────────────────────────────────────────────────────────────────────────────
class TestMainHandlerPresence:
    """Every run_* handler in this list MUST exist in main.py.

    If it's missing, an agent silently deleted it. Stop immediately.
    """

    @pytest.fixture(scope="class")
    def main_source(self) -> str:
        return MAIN_PY.read_text(encoding="utf-8", errors="replace")

    # ── Handlers historically deleted/restored most often ──────────────────
    CRITICAL_HANDLERS = [
        "run_holdings_analysis",
        "run_portfolio_risk_optimizer",
        "run_option_position_manager",
        "run_signal_stack",
        "run_global_dashboard",
        "run_options_from_actionable",
        "run_put_options_from_actionable",
        "run_options_scan_us_quality",
        "run_vectorbt_backtest",
        "run_sell_strategy",
        "run_combined_cgt",
        "run_moomoo_au_cgt",
        "run_ibkr_cgt",
    ]

    # ── Full list (all handlers that must exist) ───────────────────────────
    ALL_HANDLERS = [
        "run_moomoo_au_cgt",
        "run_moomoo_sg_cgt",
        "run_ibkr_cgt",
        "run_webull_cgt",
        "run_combined_cgt",
        "run_tax_harvest_advisor",
        "run_global_dashboard",
        "run_portfolio",
        "run_portfolio_analytics",
        "run_report_analytics",
        "run_pnl_attribution",
        "run_holdings_analysis",
        "run_portfolio_risk_optimizer",
        "run_portfolio_correlation",
        "run_india_dashboard",
        "run_india_gtt_audit",
        "run_india_gtt_suggest",
        "run_india_gtt_place",
        "run_india_gtt_list",
        "run_india_ao_gtt_audit",
        "run_india_ao_gtt_suggest",
        "run_india_ao_gtt_place",
        "run_india_ao_gtt_list",
        "run_india_market_internals",
        "run_india_order_test",
        "run_india_tests",
        "run_india_fo_analyzer",
        "run_fo_arbitrage",
        "run_tv_options_screener",
        "run_options_scan_us_quality",
        "run_options_hedger",
        "run_broker_api_audit",
        "run_vectorbt_backtest",
        "run_live_dashboard",
        "run_moomoo_market_tools",
        "run_multi_source_test",
        "run_performance_analyzer",
        "run_adaptive_threshold",
        "run_download_fires",
        "run_combined_signals",
        "run_signal_stack",
        "run_webhook_server",
        "run_telegram_bot",
        "run_report_scheduler",
        "run_test_enrichers",
        "run_strategy_backtest",
        "run_sell_strategy",
        "run_multi_asset_screener",
        "run_sector_heatmap",
        "run_prediction_engine",
        "run_forex_screener",
        "run_morning_screener_us",
        "run_morning_screener_hk",
        "run_morning_screener_custom",
        "run_austsuper_screener",
        "run_manual_analyse",
        "run_screener_run",
        "run_alert_tracker_update",
        "run_alert_tracker_report",
        "run_alert_tracker_stats",
        "run_alert_tracker_all",
        "run_signal_quality_monitor",
        "run_news_scan_bullish",
        "run_news_scan_bearish",
        "run_tests_signal_engine",
        "run_tests_cgt_moomoo",
        "run_tests_integration_moomoo",
        "run_tests_integration_tv",
        "run_tests_integration_external",
        "run_tests_all",
        "run_tests_input_and_tracker",
        "run_tests_news_screener",
        "run_news_browser",
        "run_moomoo_insights_scan",
        "run_trade_history_analysis",
        "run_fundamental_analysis",
        "run_options_from_actionable",
        "run_options_manual",
        "run_put_options_from_actionable",
        "run_put_options_from_bearish_screen",
        "run_options_india",
        "run_option_position_manager",
        "run_fo_chain_screener",
        "run_options_outcome_checker",
        "run_options_rec_report",
        "run_options_us",
        "run_morning_all",
        "run_auto_trader_handler",
        "run_gtt_submenu",
        "run_tests_submenu",
        "run_tqs_v2_backtest",
    ]

    @pytest.mark.parametrize("handler", CRITICAL_HANDLERS)
    def test_critical_handler_present(self, main_source, handler):
        """CRITICAL handlers — historically deleted/restored multiple times."""
        assert f"def {handler}(" in main_source, (
            f"REGRESSION: def {handler}() was DELETED from main.py. "
            f"This handler was previously restored (see git log for 'restore {handler}'). "
            f"Do NOT remove it again."
        )

    def test_all_handlers_present(self, main_source):
        """All 88 handlers must be present in main.py."""
        missing = [h for h in self.ALL_HANDLERS if f"def {h}(" not in main_source]
        assert not missing, (
            f"REGRESSION: {len(missing)} handler(s) DELETED from main.py:\n"
            + "\n".join(f"  - {h}" for h in missing)
        )

    def test_helper_load_held_option_positions(self, main_source):
        """_load_held_option_positions must exist — mocked by TestRunOptionPositionManager."""
        assert "def _load_held_option_positions(" in main_source, (
            "REGRESSION: _load_held_option_positions() DELETED from main.py. "
            "Tests patch this at 'main._load_held_option_positions'."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — MENU structure integrity
# ─────────────────────────────────────────────────────────────────────────────
class TestMenuStructureIntegrity:
    """MENU variable must contain all expected option codes."""

    @pytest.fixture(scope="class")
    def main_source(self) -> str:
        return MAIN_PY.read_text(encoding="utf-8", errors="replace")

    REQUIRED_MENU_KEYS = [
        '"4c"',   # Holdings Snapshot Analysis — restored 2026-05-17
        '"4d"',   # Portfolio Risk Optimizer   — restored 2026-05-17
        '"10d"',  # US Options Scan (Quality 300)
        '"4a"',   # P&L Attribution
        '"4b"',   # Portfolio Correlation
    ]

    @pytest.mark.parametrize("key", REQUIRED_MENU_KEYS)
    def test_menu_key_present(self, main_source, key):
        """Critical MENU entries that agents have removed."""
        assert key in main_source, (
            f"REGRESSION: MENU entry {key} DELETED from main.py MENU variable. "
            f"This was restored in git — do not remove again."
        )

    def test_menu_variable_exists(self, main_source):
        """MENU variable must be defined (it's the entire CLI dispatch table)."""
        assert "MENU = [" in main_source or "MENU=[" in main_source, (
            "REGRESSION: MENU variable not found in main.py. "
            "The entire CLI depends on this variable."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — hybrid_options.py structural guards
# ─────────────────────────────────────────────────────────────────────────────
class TestHybridOptionsStructure:
    """Guards for functions that were silently modified or deleted in hybrid_options.py."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        p = TV / "options" / "hybrid_options.py"
        return p.read_text(encoding="utf-8", errors="replace")

    def test_fetch_ws_fields_exists(self, source):
        """_fetch_ws_fields must exist — primary WS data fetch."""
        assert "_fetch_ws_fields" in source, (
            "REGRESSION: _fetch_ws_fields deleted from hybrid_options.py"
        )

    def test_idle_streak_detector_present(self, source):
        """idle_streak counter must be present to prevent full-timeout hangs.

        Root cause (2026-05-11 + 2026-05-17): the WS loop broke only on
        quote_completed which is inconsistently sent by TV. Without idle_streak,
        each symbol waited the full timeout_s (12 s) regardless of data arrival.
        """
        assert "idle_streak" in source, (
            "REGRESSION: idle_streak detector DELETED from _fetch_ws_fields. "
            "This causes _fetch_ws_fields to wait the full timeout_s even when "
            "no data is arriving. Restore: increment idle_streak on TimeoutError, "
            "break when idle_streak >= 2. See TestWSIdleBreak regression tests."
        )

    def test_get_calls_exists(self, source):
        """get_calls() must exist on HybridOptionsFetcher."""
        assert "def get_calls(" in source, (
            "REGRESSION: HybridOptionsFetcher.get_calls() deleted"
        )

    def test_get_puts_exists(self, source):
        """get_puts() must exist on HybridOptionsFetcher."""
        assert "def get_puts(" in source, (
            "REGRESSION: HybridOptionsFetcher.get_puts() deleted"
        )

    def test_batch_enrich_ws_exists(self, source):
        """batch_enrich_ws() must exist — called by options_contract_screener."""
        assert "def batch_enrich_ws(" in source or "batch_enrich_ws" in source, (
            "REGRESSION: HybridOptionsFetcher.batch_enrich_ws deleted"
        )

    def test_threadpool_uses_shutdown_wait_false(self, source):
        """Golden Rule #16: Never use 'with ThreadPoolExecutor' for timed work.

        The 'with' block calls shutdown(wait=True) on __exit__, blocking
        indefinitely even after TimeoutError. Verified root cause of 419s hang.
        """
        # Check that any ThreadPoolExecutor in hybrid_options uses shutdown(wait=False)
        # rather than the 'with ThreadPoolExecutor() as ex:' pattern.
        if "ThreadPoolExecutor" not in source:
            return  # No executor in file — OK
        bad_pattern_count = source.count("with ThreadPoolExecutor(")
        assert bad_pattern_count == 0, (
            "GOLDEN RULE #16 VIOLATION: 'with ThreadPoolExecutor(' found in hybrid_options.py. "
            "Use: ex = ThreadPoolExecutor(1); fut = ex.submit(fn); fut.result(timeout=N); "
            "ex.shutdown(wait=False). The 'with' pattern blocks on __exit__ after TimeoutError."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — backtest_vectorbt.py structural guards
# ─────────────────────────────────────────────────────────────────────────────
class TestVectorbtStructure:
    """Guards for backtest_vectorbt.py — commit fe78dc5 silently deleted 277 lines."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        p = TV / "backtest_vectorbt.py"
        return p.read_text(encoding="utf-8", errors="replace")

    def test_file_not_corrupted(self, source):
        """File must have at least 200 lines (fe78dc5 left only 23)."""
        line_count = source.count("\n")
        assert line_count >= 200, (
            f"REGRESSION: backtest_vectorbt.py has only {line_count} lines. "
            f"Commit fe78dc5 silently deleted 277 lines. Restore from git."
        )

    def test_deep_loss_constant(self, source):
        """DEEP_LOSS constant must be present — needed for strategy-aware exit."""
        assert "DEEP_LOSS" in source, (
            "REGRESSION: DEEP_LOSS constant deleted from backtest_vectorbt.py"
        )

    def test_itm_win_reference(self, source):
        """ITM_WIN outcome reference must be present."""
        assert "ITM_WIN" in source, (
            "REGRESSION: ITM_WIN outcome reference deleted from backtest_vectorbt.py"
        )

    def test_sort_table_js(self, source):
        """sortTable JS function must be present in the HTML report template."""
        assert "sortTable" in source, (
            "REGRESSION: sortTable() JS function deleted from backtest_vectorbt.py HTML template"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — signal_stack.py structural guards
# ─────────────────────────────────────────────────────────────────────────────
class TestSignalStackStructure:
    """signal_stack.py structural guards — 5 restorations in 6 months."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        p = TV / "signal_stack.py"
        return p.read_text(encoding="utf-8", errors="replace")

    def test_vader_sentiment_present(self, source):
        """VADER must be the default sentiment backend (Golden Rule #13)."""
        assert "vader" in source.lower(), (
            "REGRESSION: VADER sentiment removed from signal_stack.py. "
            "VADER is ALWAYS the default (Golden Rule #13)."
        )

    def test_score_technical_present(self, source):
        """score_technical() must exist — L1-L4 core scoring function."""
        assert "def score_technical(" in source or "score_technical" in source, (
            "REGRESSION: score_technical deleted from signal_stack.py"
        )

    def test_actionable_report_generated(self, source):
        """actionable_report.html must be referenced — primary output of the pipeline."""
        assert "actionable_report.html" in source, (
            "REGRESSION: actionable_report.html output path deleted from signal_stack.py"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — options_actionable_report.py structural guards
# ─────────────────────────────────────────────────────────────────────────────
class TestOptionsActionableReportStructure:
    """Guards for options_actionable_report.py viewport + reject report."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        p = TV / "options" / "options_actionable_report.py"
        return p.read_text(encoding="utf-8", errors="replace")

    def test_viewport_meta_in_rejected_report_template(self, source):
        """options_rejected.html template must have viewport meta.

        Missing viewport causes TestHTMLQualityGates::test_reports_have_viewport
        to fail after every regeneration.
        """
        # Find the rejected report HTML template (look for 'Options Rejected Report')
        idx = source.find("Options Rejected Report")
        assert idx != -1, (
            "REGRESSION: 'Options Rejected Report' HTML template removed from "
            "options_actionable_report.py"
        )
        # Check the 500 chars around the title for viewport
        snippet = source[max(0, idx - 300) : idx + 500]
        assert "viewport" in snippet, (
            "REGRESSION: <meta name='viewport'> DELETED from the Options Rejected "
            "Report HTML template. This causes test_reports_have_viewport to fail "
            "after every report regeneration. Add it back to the <head> section."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — Golden Rule #16 compliance (ThreadPoolExecutor pattern)
# ─────────────────────────────────────────────────────────────────────────────
class TestGoldenRule16Compliance:
    """Check that no module uses 'with ThreadPoolExecutor' + fut.result(timeout=N).

    This is Golden Rule #16: the 'with' block calls shutdown(wait=True) on exit,
    which BLOCKS indefinitely even after TimeoutError. Root cause of 419s hang.
    """

    # Files where GR#16 was verified broken and fixed:
    GUARDED_FILES = [
        TV / "options" / "hybrid_options.py",
        TV / "options" / "options_analyzer.py",
        TV / "options" / "us_options.py",
    ]

    @pytest.mark.parametrize("filepath", GUARDED_FILES, ids=lambda p: p.name)
    def test_no_with_threadpool_executor(self, filepath):
        """No 'with ThreadPoolExecutor(' pattern allowed in broker-calling files."""
        if not filepath.exists():
            pytest.skip(f"{filepath.name} not found")
        source = filepath.read_text(encoding="utf-8", errors="replace")
        bad_count = source.count("with ThreadPoolExecutor(")
        assert bad_count == 0, (
            f"GOLDEN RULE #16 VIOLATION in {filepath.name}: "
            f"Found 'with ThreadPoolExecutor(' {bad_count} time(s). "
            f"Use: ex = ThreadPoolExecutor(1); fut = ex.submit(fn); "
            f"fut.result(timeout=N); ex.shutdown(wait=False)"
        )
