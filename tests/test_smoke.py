"""
tests/test_smoke.py
═══════════════════
Smoke test harness for main.py menu options.
Uses subprocess.Popen(stdin=PIPE) to drive the interactive terminal menu
and verify that:
  1. The script does not crash (no Traceback / exit code 1)
  2. Expected HTML reports are generated with non-trivial file sizes
  3. Broker-offline options handle missing connections gracefully

Architecture:
  Layer 1 (this file) — stdin pipe automation, stdout/stderr capture
  Layer 2 (ux-tester agent) — Playwright MCP HTML report validation

Usage:
  pytest tests/test_smoke.py -v               # run all smoke tests
  pytest tests/test_smoke.py -v -k "tier1"    # run only safe/fast tests
  pytest tests/test_smoke.py -v -k "broker"   # run broker-dependent tests
  pytest tests/test_smoke.py -v -k "report"   # run report-validation tests
  pytest tests/test_smoke.py --timeout=300    # increase timeout for slow tests
"""

import os
import sys
import subprocess
import time
import py_compile
from pathlib import Path

import pytest

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
REPORTS = ROOT / "reports"
TV_REPORTS = ROOT / "tradingview" / "reports"
LOGS = ROOT / "logs"

# ── Base env — mirrors main.py BASE_ENV ───────────────────────────────────────
_BASE_ENV = {
    **os.environ,
    "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
    "PYTHONIOENCODING": "utf-8",
    "OPENBLAS_NUM_THREADS": "1",
    "SMOKE_TEST": "1",           # signals scripts to limit data processing
    "NO_BROWSER_OPEN": "1",      # prevent webbrowser.open() during tests
}

# ── Core helper ───────────────────────────────────────────────────────────────

def run_menu_option(
    option: str,
    extra_inputs: list[str] | None = None,
    timeout: int = 90,
) -> tuple[int, str, str]:
    """
    Launch ``python main.py``, feed the menu option + any extra prompt inputs via
    stdin, and return ``(returncode, stdout, stderr)``.

    ``extra_inputs`` is a list of strings to send after the menu option, one per
    prompt.  Each empty string accepts the current default.

    Example — option 25 (IBKR CGT) accepts the "Use Flex Query defaults?" prompt::

        run_menu_option("25", extra_inputs=[""])   # "y" default accepted

    The final ``"q"`` quits the menu after the handler returns.
    """
    inputs = [option] + (extra_inputs or []) + ["q"]
    stdin_data = "\n".join(inputs) + "\n"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        env=_BASE_ENV,
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr


def assert_no_traceback(stdout: str, stderr: str, option: str) -> None:
    """Fail the test if a Python Traceback appears in the output."""
    combined = stdout + stderr
    assert "Traceback (most recent call last)" not in combined, (
        f"Option {option!r} produced a Python traceback:\n"
        + combined[-2000:]
    )


def assert_no_crash(returncode: int, stderr: str, option: str) -> None:
    """Fail if the process exited with code 1 and stderr has content."""
    if returncode == 1 and stderr.strip():
        pytest.fail(
            f"Option {option!r} exited with code 1.\nStderr:\n{stderr[-1500:]}"
        )


def assert_html_report(path: Path, min_bytes: int = 5_000, option: str = "") -> None:
    """Fail if the HTML report does not exist or is suspiciously small."""
    assert path.exists(), (
        f"Option {option!r}: expected HTML report not found at {path}"
    )
    size = path.stat().st_size
    assert size >= min_bytes, (
        f"Option {option!r}: report {path.name} is only {size} bytes "
        f"(expected >= {min_bytes}). Likely empty or errored."
    )


# ── Log reading helpers (Layer 1 — log validation) ───────────────────────────

def find_latest_log(option: str, after_time: float) -> str | None:
    """
    Find and read the log file written by main.py for the given menu option.

    main.py names logs: logs/{option}_{safe_label}_{YYYYMMDD_HHMMSS}.log
    Returns file contents (str) or None if no matching log found.
    """
    if not LOGS.exists():
        return None
    matches = list(LOGS.glob(f"{option}_*.log"))
    # Accept files modified within 2 s before after_time (clock skew tolerance)
    recent = [f for f in matches if f.stat().st_mtime >= after_time - 2]
    if not recent:
        return None
    latest = max(recent, key=lambda f: f.stat().st_mtime)
    try:
        return latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def assert_log_no_severe_errors(log_content: str, option: str) -> None:
    """
    Fail if the log contains patterns that indicate a severe script crash.
    Connection errors and missing-broker warnings are NOT in this list.
    """
    if not log_content:
        return
    severe = [
        "Traceback (most recent call last)",
        "SyntaxError:",
        "ModuleNotFoundError:",
        "NameError:",
        "AttributeError:",
    ]
    for pattern in severe:
        assert pattern not in log_content, (
            f"Option {option!r} log contains {pattern!r}:\n" + log_content[-2000:]
        )


# ══════════════════════════════════════════════════════════════════════════════
# TIER 1 — SAFE (read-only, no broker required)
# All of these generate HTML reports and should complete in < 3 minutes.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_3_portfolio_analytics():
    """Option 3: Portfolio Analytics — reads portfolio_history.db → HTML chart."""
    rc, out, err = run_menu_option("3")
    assert_no_traceback(out, err, "3")
    # If DB not populated yet, the script should still produce an empty report
    # rather than crash.
    assert_no_crash(rc, err, "3")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_4_report_analytics():
    """Option 4: Report Analytics — reads report_analytics.db → HTML chart."""
    rc, out, err = run_menu_option("4")
    assert_no_traceback(out, err, "4")
    assert_no_crash(rc, err, "4")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_4a_pnl_attribution():
    """Option 4a: P&L Attribution — reads .cache_global_positions.json."""
    rc, out, err = run_menu_option("4a")
    assert_no_traceback(out, err, "4a")
    assert_no_crash(rc, err, "4a")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_31_broker_api_audit():
    """Option 31: Broker API Audit — 2 prompts: skip_prb + test_sym (offline-safe)."""
    t0 = time.time()
    rc, out, err = run_menu_option("31", extra_inputs=["", ""])
    assert_no_traceback(out, err, "31")
    assert_no_crash(rc, err, "31")
    log = find_latest_log("31", t0)
    if log is not None:
        assert_log_no_severe_errors(log, "31")
    # Report might be in REPORTS or TV_REPORTS depending on version
    for html in [REPORTS / "broker_api_audit.html", TV_REPORTS / "broker_api_audit.html"]:
        if html.exists() and (time.time() - html.stat().st_mtime) < 300:
            assert_html_report(html, min_bytes=3_000, option="31")
            break


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_36_sector_heatmap():
    """Option 36: Sector Heatmap — 2 prompts (exchange, n). TV API needed (graceful fail OK)."""
    t0 = time.time()
    rc, out, err = run_menu_option("36", extra_inputs=["", ""])
    assert_no_traceback(out, err, "36")
    log = find_latest_log("36", t0)
    if log is not None:
        assert_log_no_severe_errors(log, "36")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_43_alert_tracker_stats():
    """Option 43: Alert tracker stats summary — reads alert_tracker.db."""
    rc, out, err = run_menu_option("43")
    assert_no_traceback(out, err, "43")
    assert_no_crash(rc, err, "43")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_50_performance_analyzer():
    """Option 50: Performance Analyzer — 3 prompts (min_samples, since, tradable_only)."""
    t0 = time.time()
    rc, out, err = run_menu_option("50", extra_inputs=["", "", ""])
    assert_no_traceback(out, err, "50")
    assert_no_crash(rc, err, "50")
    log = find_latest_log("50", t0)
    if log is not None:
        assert_log_no_severe_errors(log, "50")
    # Check report if freshly generated
    html = TV_REPORTS / "performance_analysis.html"
    if html.exists() and (time.time() - html.stat().st_mtime) < 300:
        assert_html_report(html, min_bytes=5_000, option="50")


# ══════════════════════════════════════════════════════════════════════════════
# TIER 2 — BROKER_REQUIRED (test graceful offline handling)
# These need live broker connections.  We verify the script starts correctly
# and handles missing connections with a warning rather than a crash.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_23_moomoo_au_cgt_offline():
    """Option 23: Moomoo AU CGT — OpenD offline should warn, not crash."""
    rc, out, err = run_menu_option("23")
    assert_no_traceback(out, err, "23")
    # May produce an "OpenD not running" warning — that is acceptable
    combined = (out + err).lower()
    crash_indicators = ["traceback", "exception", "attributeerror", "nameerror"]
    for indicator in crash_indicators:
        assert indicator not in combined, (
            f"Option 23 produced unexpected error indicator {indicator!r}:\n"
            + (out + err)[-1500:]
        )


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_24_moomoo_sg_cgt_offline():
    """Option 24: Moomoo SG CGT — OpenD offline should warn, not crash."""
    rc, out, err = run_menu_option("24")
    assert_no_traceback(out, err, "24")
    combined = (out + err).lower()
    assert "traceback" not in combined


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_25_ibkr_cgt_flex_defaults():
    """Option 25: IBKR CGT — accept Flex Query defaults (y)."""
    rc, out, err = run_menu_option("25", extra_inputs=[""])  # accept "y" default
    assert_no_traceback(out, err, "25")
    assert_no_crash(rc, err, "25")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_26_webull_cgt_pdf_mode():
    """Option 26: Webull CGT — PDF mode (default 1), handles missing PDFs gracefully."""
    rc, out, err = run_menu_option("26", extra_inputs=[""])  # accept "1" (PDF) default
    assert_no_traceback(out, err, "26")
    combined = (out + err).lower()
    assert "traceback" not in combined


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_27_combined_cgt_skip_all():
    """Option 27: Combined CGT — skip all brokers (quick smoke test)."""
    # Inputs: ibkr csv (blank), skip moomoo (y), skip ibkr (y), skip india (y)
    rc, out, err = run_menu_option("27", extra_inputs=["", "y", "y", "y"])
    assert_no_traceback(out, err, "27")
    combined = (out + err).lower()
    assert "traceback" not in combined


# ══════════════════════════════════════════════════════════════════════════════
# TIER 2 — India broker options (graceful offline)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_2_india_dashboard_offline():
    """Option 2: India Portfolio Dashboard — handles offline Zerodha gracefully."""
    rc, out, err = run_menu_option("2")
    assert_no_traceback(out, err, "2")
    combined = (out + err).lower()
    assert "traceback" not in combined


# ══════════════════════════════════════════════════════════════════════════════
# NEW TIER 1 — Additional offline-safe tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.tier1
@pytest.mark.timeout(30)
def test_option_5_gtt_submenu_back():
    """Option 5: GTT submenu — send 'b' immediately to go back (no crash)."""
    rc, out, err = run_menu_option("5", extra_inputs=["b"])
    assert_no_traceback(out, err, "5")


@pytest.mark.tier1
@pytest.mark.timeout(30)
def test_option_51_tests_submenu_back():
    """Option 51: Tests submenu — send 'b' to exit immediately (no crash)."""
    rc, out, err = run_menu_option("51", extra_inputs=["b"])
    assert_no_traceback(out, err, "51")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_15_sell_strategy_demo():
    """Option 15: Sell Strategy — demo mode (choice=4), no live broker required."""
    rc, out, err = run_menu_option("15", extra_inputs=["4"])
    assert_no_traceback(out, err, "15")
    assert_no_crash(rc, err, "15")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_29_strategy_backtest():
    """Option 29: Strategy Backtest — accept 'all' default symbol (1 prompt)."""
    rc, out, err = run_menu_option("29", extra_inputs=[""])
    assert_no_traceback(out, err, "29")
    assert_no_crash(rc, err, "29")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_41_alert_tracker_update():
    """Option 41: Alert Tracker update — refreshes prices, reads alert_tracker.db."""
    t0 = time.time()
    rc, out, err = run_menu_option("41")
    assert_no_traceback(out, err, "41")
    log = find_latest_log("41", t0)
    if log is not None:
        assert_log_no_severe_errors(log, "41")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_option_42_alert_tracker_report():
    """Option 42: Alert Tracker report — generates HTML from DB."""
    rc, out, err = run_menu_option("42")
    assert_no_traceback(out, err, "42")
    assert_no_crash(rc, err, "42")


@pytest.mark.tier1
@pytest.mark.timeout(90)
def test_option_44_alert_tracker_all():
    """Option 44: Alert Tracker full run (update + report + stats)."""
    rc, out, err = run_menu_option("44")
    assert_no_traceback(out, err, "44")


@pytest.mark.tier1
@pytest.mark.timeout(60)
def test_main_menu_renders():
    """Sending 'q' immediately should show the menu and exit cleanly."""
    rc, out, err = run_menu_option("q", extra_inputs=[])
    assert_no_traceback(out, err, "q")
    # Menu should contain recognisable section headers
    assert any(kw in out for kw in ["Portfolio", "Signal", "Option", "CGT", "---", "═"]), (
        f"Menu did not render any expected section headers.\nstdout:\n{out[:2000]}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# NEW BROKER TESTS — additional graceful-offline checks
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_1_global_dashboard_allow_partial():
    """
    Option 1: Global Dashboard — 4 inputs:
      1. Zerodha redirect URL (empty = skip login)
      2. Skip IBKR? → y
      3. Allow partial report? → y  (so HARD STOP is bypassed)
      4. pause() Enter before returning to menu
    """
    rc, out, err = run_menu_option("1", extra_inputs=["", "y", "y", ""])
    assert_no_traceback(out, err, "1")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_11_fo_arbitrage_defaults():
    """Option 11: F&O Arbitrage — 3 prompts (rfi, skip_ao, skip_fx), defaults."""
    rc, out, err = run_menu_option("11", extra_inputs=["", "", ""])
    assert_no_traceback(out, err, "11")
    assert_no_crash(rc, err, "11")


@pytest.mark.broker
@pytest.mark.timeout(90)
def test_option_12_options_hedger_defaults():
    """Option 12: Options Hedger — 3 prompts (min_val, skip_ao, skip_reg), defaults."""
    rc, out, err = run_menu_option("12", extra_inputs=["", "", ""])
    assert_no_traceback(out, err, "12")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_13_download_fires():
    """Option 13: Download Fired Alerts — no prompts, needs TV API (graceful fail OK)."""
    rc, out, err = run_menu_option("13")
    assert_no_traceback(out, err, "13")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_16_multi_asset_screener_no_feed():
    """Option 16: Multi-Asset Screener — 1 prompt (feed=n)."""
    rc, out, err = run_menu_option("16", extra_inputs=[""])
    assert_no_traceback(out, err, "16")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_17_morning_screener_us():
    """Option 17: Morning Screener US — no prompts, needs TV API."""
    rc, out, err = run_menu_option("17")
    assert_no_traceback(out, err, "17")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_20_india_market_internals():
    """Option 20: India Market Internals — 4 prompts (symbol, expiry, run_all, show_gain)."""
    rc, out, err = run_menu_option("20", extra_inputs=["", "", "", ""])
    assert_no_traceback(out, err, "20")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_22_india_fo_analyzer():
    """Option 22: India F&O Analyzer — graceful offline."""
    rc, out, err = run_menu_option("22")
    assert_no_traceback(out, err, "22")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_35_vectorbt_backtest():
    """Option 35: Signal Backtest (VectorBT) — no prompts, reads alert_tracker.db."""
    t0 = time.time()
    rc, out, err = run_menu_option("35")
    assert_no_traceback(out, err, "35")
    assert_no_crash(rc, err, "35")
    log = find_latest_log("35", t0)
    if log is not None:
        assert_log_no_severe_errors(log, "35")


@pytest.mark.broker
@pytest.mark.timeout(60)
def test_option_49_fundamental_analysis():
    """Option 49: Fundamental Analysis — 1 prompt (n_symbols), reads fires_raw.json."""
    rc, out, err = run_menu_option("49", extra_inputs=[""])
    assert_no_traceback(out, err, "49")


# ══════════════════════════════════════════════════════════════════════════════
# TIER 3 — IMPORT SMOKE (scripts must at least import without error)
# We don't run these through the menu — we just verify the scripts can be
# imported / syntax-checked without crashing.
# ══════════════════════════════════════════════════════════════════════════════

_SYNTAX_CHECK_SCRIPTS = [
    ROOT / "main.py",
    ROOT / "global_portfolio_dashboard.py",
    ROOT / "tradingview" / "signal_stack.py",
    ROOT / "tradingview" / "alert_tracker.py",
    ROOT / "tradingview" / "sell" / "sell_strategy.py",
    ROOT / "cgt" / "combined_cgt_report.py",
    ROOT / "tradingview" / "screeners" / "multi_asset_screener.py",
    ROOT / "tradingview" / "screeners" / "morning_screener.py",
    ROOT / "tradingview" / "performance_analyzer.py",
    ROOT / "tradingview" / "backtest" / "backtest_vectorbt.py",
    ROOT / "broker_schemas.py",
    ROOT / "log_config.py",
    ROOT / "path_config.py",
]


@pytest.mark.tier3
@pytest.mark.parametrize("script_path", _SYNTAX_CHECK_SCRIPTS, ids=lambda p: p.name)
def test_script_syntax(script_path: Path):
    """Verify Python syntax of critical scripts using py_compile."""
    if not script_path.exists():
        pytest.skip(f"Script not found: {script_path}")
    try:
        py_compile.compile(str(script_path), doraise=True)
    except py_compile.PyCompileError as exc:
        pytest.fail(f"Syntax error in {script_path.name}:\n{exc}")


# ══════════════════════════════════════════════════════════════════════════════
# REPORT VALIDATION (separate from smoke — run after option 1 generates report)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.report
def test_global_dashboard_report_has_all_six_brokers():
    """
    If the global dashboard HTML exists, all 6 brokers must appear in it.
    This catches the 'offline broker silently omitted from PnL table' bug.

    Run after option 1 has generated the report (manually or in CI).
    """
    html = REPORTS / "global_portfolio_dashboard.html"
    if not html.exists():
        pytest.skip("global_portfolio_dashboard.html not yet generated — run option 1 first")

    content = html.read_text(encoding="utf-8", errors="replace")
    required = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]
    missing = [b for b in required if b not in content]
    assert not missing, (
        f"Missing brokers in global_portfolio_dashboard.html: {missing}\n"
        "This is a broker coverage bug — see broker-coverage.instructions.md"
    )


@pytest.mark.report
def test_global_dashboard_report_size():
    """Global dashboard HTML must be at least 50KB (< 50KB = error/empty report)."""
    html = REPORTS / "global_portfolio_dashboard.html"
    if not html.exists():
        pytest.skip("global_portfolio_dashboard.html not yet generated")
    assert_html_report(html, min_bytes=50_000, option="1")


@pytest.mark.report
def test_india_dashboard_report_exists():
    """India dashboard HTML should exist if option 2 ran successfully."""
    html = REPORTS / "india_portfolio_dashboard.html"
    if not html.exists():
        pytest.skip("india_portfolio_dashboard.html not yet generated — run option 2 first")
    assert_html_report(html, min_bytes=20_000, option="2")


@pytest.mark.report
def test_performance_analyzer_report():
    """
    Performance analyzer HTML must contain the key analysis sections.
    The report uses numeric score ranges (e.g. '28.5–43.3'), NOT named
    labels like LOW/MED/HIGH/APEX — check for actual report sections.
    """
    # main.py writes to performance_analysis.html (not performance_analyzer.html)
    html = TV_REPORTS / "performance_analysis.html"
    if not html.exists():
        pytest.skip("performance_analysis.html not yet generated — run option 50 first")
    content = html.read_text(encoding="utf-8", errors="replace")
    # Verify key sections that every performance_analysis.html always contains
    for section in ["Win Rate", "Failure Pattern", "Rolling"]:
        assert section in content, (
            f"Performance analyzer report missing section '{section}' — "
            "report may be truncated or from a different script version"
        )


# ══════════════════════════════════════════════════════════════════════════════
# TIER 1 — MOOMOO COMMUNITY + SENTIMENT INTEGRATION (fast, no network)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.tier1
def test_moomoo_community_import():
    """moomoo_community_intel must import and expose cache API without crash."""
    import importlib, sys
    sys.path.insert(0, str(ROOT))
    mod = importlib.import_module("moomoo_community_intel")
    assert callable(getattr(mod, "get_community_headlines", None)), \
        "get_community_headlines() not found"
    assert callable(getattr(mod, "load_community_cache", None)), \
        "load_community_cache() not found"
    assert callable(getattr(mod, "save_community_cache", None)), \
        "save_community_cache() not found"


@pytest.mark.tier1
def test_moomoo_community_cache_missing_returns_empty():
    """get_community_headlines() must return [] when no cache exists (no Playwright)."""
    import importlib, sys, tempfile
    from pathlib import Path as _Path
    from unittest.mock import patch
    sys.path.insert(0, str(ROOT))
    mod = importlib.import_module("moomoo_community_intel")
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(mod, "_COMMUNITY_CACHE", _Path(tmp) / ".no_cache.json"):
            result = mod.get_community_headlines()
    assert result == [], f"Expected [] with no cache, got {result!r}"


@pytest.mark.tier1
def test_tv_news_screener_sentiment_backend_param():
    """news_to_fires() and fetch_news_signals() must accept sentiment_backend kwarg."""
    import importlib, sys, inspect
    sys.path.insert(0, str(ROOT / "tradingview"))
    mod = importlib.import_module("tv_news_screener")
    for fn_name in ("news_to_fires", "fetch_news_signals", "_score_text"):
        fn = getattr(mod, fn_name, None)
        assert fn is not None, f"{fn_name} not found in tv_news_screener"
        sig = inspect.signature(fn)
        param = "sentiment_backend" if fn_name != "_score_text" else "backend"
        assert param in sig.parameters, \
            f"{fn_name}() missing '{param}' parameter"


@pytest.mark.tier1
def test_tv_news_screener_bare_noise_constant():
    """_MOOMOO_BARE_NOISE must exist and contain common noise words."""
    import importlib, sys
    sys.path.insert(0, str(ROOT / "tradingview"))
    mod = importlib.import_module("tv_news_screener")
    assert hasattr(mod, "_MOOMOO_BARE_NOISE"), \
        "_MOOMOO_BARE_NOISE constant missing from tv_news_screener"
    assert "AI" in mod._MOOMOO_BARE_NOISE, "'AI' must be in _MOOMOO_BARE_NOISE"
    assert "EV" in mod._MOOMOO_BARE_NOISE, "'EV' must be in _MOOMOO_BARE_NOISE"
    assert len(mod._MOOMOO_BARE_NOISE) >= 20, \
        f"Noise filter too small: {len(mod._MOOMOO_BARE_NOISE)} words"


@pytest.mark.tier1
def test_signal_stack_moomoo_count_field():
    """fetch_sentiment_data source must include moomoo_count key."""
    import importlib, sys, inspect
    sys.path.insert(0, str(ROOT / "tradingview"))
    mod = importlib.import_module("signal_stack")
    src = inspect.getsource(mod.fetch_sentiment_data)
    assert "moomoo_count" in src, \
        "moomoo_count key missing from fetch_sentiment_data()"
    assert "Moomoo-Community" in src, \
        "'Moomoo-Community' source label missing from fetch_sentiment_data()"


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY: run a single option and print stdout (for debugging)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run a single main.py menu option and print output"
    )
    parser.add_argument("option", help="Menu option e.g. '1', '25', '4a'")
    parser.add_argument(
        "--inputs", nargs="*", default=[],
        help="Extra stdin inputs (one per prompt, empty string = accept default)"
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    t0 = time.time()
    rc, out, err = run_menu_option(args.option, args.inputs, args.timeout)
    elapsed = time.time() - t0

    print("=" * 70)
    print(f"Option: {args.option}  |  Exit code: {rc}  |  Time: {elapsed:.1f}s")
    print("=" * 70)
    print("STDOUT:")
    print(out)
    if err:
        print("STDERR:")
        print(err)
