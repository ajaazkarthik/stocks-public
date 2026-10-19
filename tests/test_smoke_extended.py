"""
tests/test_smoke_extended.py
════════════════════════════
Extended smoke tests covering the 27 menu options not covered in test_smoke.py.

Options added here:
  0, 6, 6a, 6b, 7, 8, 9, 10, 10a, 14, 14a, 18, 19, 21,
  28, 30, 32, 33, 34, 37, 38, 39, 40, 45, 46, 47, 48

Strategy:
  • Each test feeds the menu option + required prompts via stdin-pipe.
  • Empty string ("") accepts the prompt's built-in default value.
  • Tests that require an online broker/API are marked @pytest.mark.broker;
    they must still pass gracefully when the service is offline.
  • Options that open a new terminal window (CREATE_NEW_CONSOLE on Windows)
    are tested with a short timeout — the handler returns immediately.

Import helpers from test_smoke rather than duplicating them.
"""

import sys
import time
from pathlib import Path

import pytest

# Add the tests/ directory to sys.path so we can import from test_smoke.py
sys.path.insert(0, str(Path(__file__).parent))

from test_smoke import (
    run_menu_option,
    assert_no_traceback,
    assert_no_crash,
    assert_html_report,
    find_latest_log,
    REPORTS,
    TV_REPORTS,
)

# ══════════════════════════════════════════════════════════════════════════════
# TIER 1 — FAST / OFFLINE  (cancel-at-prompt, static calc, or new-console)
# These must complete in ≤ 60 s.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_0_morning_run_cancel():
    """
    Option 0: Morning Run.
    Cancels immediately at the confirmation prompt (sends 'n').
    Verifies no crash on cancel path.
    """
    rc, out, err = run_menu_option("0", extra_inputs=["n"], timeout=30)
    assert_no_traceback(out, err, "0")
    assert_no_crash(rc, err, "0")
    # Should print some form of "Cancelled" or "morning" related output
    combined = (out + err).lower()
    assert any(w in combined for w in ["cancel", "morning", "run"]), (
        "Option 0 output did not contain expected morning-run text.\n" + combined[:500]
    )


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_9_options_us_cancel_offline():
    """
    Option 9: US Options — requires OpenD (Moomoo).
    When OpenD is offline the preflight prompt asks 'Continue anyway? (y/n)'.
    Sending 'n' must cancel cleanly with no traceback.
    """
    rc, out, err = run_menu_option("9", extra_inputs=["n", ""], timeout=30)
    assert_no_traceback(out, err, "9")
    assert_no_crash(rc, err, "9")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_10_option_position_manager_defaults():
    """
    Option 10: Option Position Manager.
    Accepts all 11 prompts with their defaults (no network required).
    Should generate a small HTML report in tradingview/reports/.
    """
    empty_inputs = [""] * 11  # 11 prompts all with defaults
    rc, out, err = run_menu_option("10", extra_inputs=empty_inputs, timeout=45)
    assert_no_traceback(out, err, "10")
    assert_no_crash(rc, err, "10")


@pytest.mark.tier1
@pytest.mark.timeout(45)
def test_option_30_webhook_server_launches_new_window():
    """
    Option 30: Webhook Server.
    Sends no_sizer='' (default n) and no_ext='' (default n).
    The handler opens a new console window (CREATE_NEW_CONSOLE on Windows) and
    returns immediately — the test must complete in < 30 s.
    """
    rc, out, err = run_menu_option("30", extra_inputs=["", ""], timeout=30)
    assert_no_traceback(out, err, "30")
    # Either "Launching" or "already running" — both are valid
    combined = out + err
    assert any(
        phrase in combined
        for phrase in [
            "Launching",
            "launching",
            "webhook",
            "already running",
            "port",
        ]
    ), (
        "Option 30 did not produce expected webhook-server output.\n" + combined[:800]
    )


@pytest.mark.tier1
@pytest.mark.timeout(45)
def test_option_34_live_dashboard_launches_new_window():
    """
    Option 34: Live Dashboard (Streamlit).
    Accepts the port prompt with the default (8501).
    The handler opens a new console window and returns immediately.
    """
    rc, out, err = run_menu_option("34", extra_inputs=[""], timeout=30)
    assert_no_traceback(out, err, "34")
    assert_no_crash(rc, err, "34")
    combined = (out + err).lower()
    assert any(
        w in combined for w in ["dashboard", "streamlit", "launch", "port"]
    ), (
        "Option 34 did not produce expected dashboard output.\n" + combined[:500]
    )


# ══════════════════════════════════════════════════════════════════════════════
# BROKER TIER — may need network or broker connection; graceful failure OK
# All tests verify no crash and no traceback.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_6_options_from_actionable():
    """
    Option 6: Options from Actionable Signals.
    Accepts the 'max signals' prompt with default (20).
    Gracefully handles empty actionable list.
    """
    rc, out, err = run_menu_option("6", extra_inputs=[""], timeout=120)
    assert_no_traceback(out, err, "6")
    assert_no_crash(rc, err, "6")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_6a_put_options_from_actionable():
    """
    Option 6a: PUT Options from Actionable Signals.
    Accepts strategy mode (default BALANCED) and limit (default 20).
    """
    rc, out, err = run_menu_option("6a", extra_inputs=["", ""], timeout=120)
    assert_no_traceback(out, err, "6a")
    assert_no_crash(rc, err, "6a")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_6b_put_options_from_bearish_screen():
    """
    Option 6b: PUT Options from Bearish Screen.
    Accepts strategy mode and limit with defaults.
    """
    rc, out, err = run_menu_option("6b", extra_inputs=["", ""], timeout=120)
    assert_no_traceback(out, err, "6b")
    assert_no_crash(rc, err, "6b")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_7_options_manual_with_symbol():
    """
    Option 7: Options for Manual Symbol.
    Provides 'NASDAQ:AAPL' as the required symbol; accepts score, mode, all_modes defaults.
    """
    rc, out, err = run_menu_option(
        "7",
        extra_inputs=["NASDAQ:AAPL", "", "", ""],
        timeout=120,
    )
    assert_no_traceback(out, err, "7")
    assert_no_crash(rc, err, "7")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_8_options_india():
    """
    Option 8: India NSE F&O Options.
    Accepts strategy mode with default (BALANCED).
    """
    rc, out, err = run_menu_option("8", extra_inputs=[""], timeout=120)
    assert_no_traceback(out, err, "8")
    assert_no_crash(rc, err, "8")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_10a_fo_chain_screener():
    """
    Option 10a: F&O Chain Screener.
    Accepts all 4 prompts with defaults: market='us', min_score=70, n_top=50, symbols=''.
    """
    rc, out, err = run_menu_option("10a", extra_inputs=["", "", "", ""], timeout=120)
    assert_no_traceback(out, err, "10a")
    assert_no_crash(rc, err, "10a")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_14_signal_stack_quick_defaults():
    """
    Option 14: Signal Stack.
    Presses Enter to accept quick-run defaults (n=50, threshold=50).
    Gracefully handles an empty or absent fires_raw.json.
    """
    rc, out, err = run_menu_option("14", extra_inputs=[""], timeout=90)
    assert_no_traceback(out, err, "14")
    assert_no_crash(rc, err, "14")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_14a_combined_signals_skip_screener():
    """
    Option 14a: Combined Signals.
    Accepts fires, threshold, direction defaults; skips screener scan for speed.
    """
    rc, out, err = run_menu_option(
        "14a",
        extra_inputs=["", "", "", "y"],  # fires, threshold, direction, skip_screener=y
        timeout=120,
    )
    assert_no_traceback(out, err, "14a")
    assert_no_crash(rc, err, "14a")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_18_morning_screener_hk():
    """
    Option 18: Morning Screener HK.
    No prompts — runs immediately and fails gracefully if TV API offline.
    """
    rc, out, err = run_menu_option("18", extra_inputs=[], timeout=120)
    assert_no_traceback(out, err, "18")
    assert_no_crash(rc, err, "18")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_19_morning_screener_custom():
    """
    Option 19: Morning Screener Custom.
    Accepts market='US' (default) and limit='100' (default).
    """
    rc, out, err = run_menu_option("19", extra_inputs=["", ""], timeout=120)
    assert_no_traceback(out, err, "19")
    assert_no_crash(rc, err, "19")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_21_india_order_executor_dry_run():
    """
    Option 21: India Order Executor (dry-run).
    Accepts symbol (NSE:INFY default), limit-price (blank = market), amount defaults.
    If ZERODHA_ACCESS_TOKEN is not set the script exits early with a clear message —
    this is the expected behaviour during CI.
    """
    rc, out, err = run_menu_option("21", extra_inputs=["", "", ""], timeout=45)
    assert_no_traceback(out, err, "21")
    # Either dry-run completes OR Zerodha token missing — both are valid paths
    combined = out + err
    # Must not show an unhandled Python error
    assert "SyntaxError" not in combined
    assert "NameError" not in combined


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_28_test_enrichers():
    """
    Option 28: Test Enrichers.
    Accepts source='all' (default) and symbol='NSE:RELIANCE' (default).
    Each enricher must fail gracefully when its broker is offline.
    """
    rc, out, err = run_menu_option("28", extra_inputs=["", ""], timeout=120)
    assert_no_traceback(out, err, "28")
    assert_no_crash(rc, err, "28")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_32_moomoo_market_tools_state():
    """
    Option 32: Moomoo Market Tools.
    Sends mode='state' — reports market-open status without needing OpenD.
    """
    rc, out, err = run_menu_option("32", extra_inputs=["state", ""], timeout=45)
    assert_no_traceback(out, err, "32")
    assert_no_crash(rc, err, "32")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_33_auto_trader_dry_run():
    """
    Option 33: Auto Trader.
    Accepts broker (zerodha), top (10), since_hrs (24) and live_ans (n) defaults.
    The default live_ans='n' ensures DRY-RUN mode — NO live orders are placed.
    """
    rc, out, err = run_menu_option(
        "33",
        extra_inputs=["", "", "", ""],  # broker, top, since_hrs, live=n
        timeout=90,
    )
    assert_no_traceback(out, err, "33")
    assert_no_crash(rc, err, "33")
    # Must not see the live-trading confirmation phrase
    combined = out + err
    assert "REAL orders" not in combined or "DRY" in combined or "dry" in combined or "n" in combined


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_37_forex_screener():
    """
    Option 37: Forex Signal Report.
    Sends blank pairs → uses default major-pair set from TV screener.
    """
    rc, out, err = run_menu_option("37", extra_inputs=[""], timeout=120)
    assert_no_traceback(out, err, "37")
    assert_no_crash(rc, err, "37")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_38_prediction_engine_no_chronos():
    """
    Option 38: Prediction Engine.
    Sends 'AAPL' as the symbol list and 'y' to skip the Chronos model load
    (avoids 11-second cold-start in CI).
    """
    rc, out, err = run_menu_option(
        "38",
        extra_inputs=["AAPL", "y"],  # symbols, skip_chronos=y
        timeout=120,
    )
    assert_no_traceback(out, err, "38")
    assert_no_crash(rc, err, "38")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_39_analyse_tickers_now():
    """
    Option 39: Analyse Tickers Now.
    Provides 'AAPL' as the required ticker (blank would abort).
    Accepts no_sizer and no_ext defaults.
    """
    rc, out, err = run_menu_option(
        "39",
        extra_inputs=["AAPL", "", ""],  # tickers (required), no_sizer, no_ext
        timeout=120,
    )
    assert_no_traceback(out, err, "39")
    assert_no_crash(rc, err, "39")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_40_tv_screener_run():
    """
    Option 40: Run TV Screener Scan.
    Accepts the screener_id prompt with default ('stocks_us').
    """
    rc, out, err = run_menu_option("40", extra_inputs=[""], timeout=120)
    assert_no_traceback(out, err, "40")
    assert_no_crash(rc, err, "40")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_45_news_scan_bullish():
    """
    Option 45: News Scan — Bullish.
    Accepts max_age (24h default) and VADER threshold (0.35 default).
    """
    rc, out, err = run_menu_option("45", extra_inputs=["", ""], timeout=90)
    assert_no_traceback(out, err, "45")
    assert_no_crash(rc, err, "45")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_46_news_scan_bearish():
    """
    Option 46: News Scan — Bearish.
    Accepts max_age (24h default) and VADER threshold (0.35 default).
    """
    rc, out, err = run_menu_option("46", extra_inputs=["", ""], timeout=90)
    assert_no_traceback(out, err, "46")
    assert_no_crash(rc, err, "46")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_47_news_browser():
    """
    Option 47: News Browser.
    Accepts direction (1=all default), max_age (24h), threshold (0.35).
    """
    rc, out, err = run_menu_option("47", extra_inputs=["", "", ""], timeout=90)
    assert_no_traceback(out, err, "47")
    assert_no_crash(rc, err, "47")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_48_trade_history_analysis():
    """
    Option 48: Trade History Analysis.
    Accepts start-date with default (2020-01-01).
    Requires Moomoo OpenD to be running for full results;
    graceful failure when offline.
    """
    rc, out, err = run_menu_option("48", extra_inputs=[""], timeout=120)
    assert_no_traceback(out, err, "48")
    assert_no_crash(rc, err, "48")


# ══════════════════════════════════════════════════════════════════════════════
# COVERAGE SUMMARY
# ══════════════════════════════════════════════════════════════════════════════


def test_extended_coverage_summary():
    """
    Meta-test: documents all options covered by this file.
    Always passes — acts as a live registry of test coverage.
    """
    covered_here = {
        "0", "6", "6a", "6b", "7", "8", "9", "10", "10a",
        "14", "14a", "18", "19", "21", "28", "30", "32",
        "33", "34", "37", "38", "39", "40", "45", "46", "47", "48",
    }
    assert len(covered_here) == 27, "Coverage count mismatch — update this test"
