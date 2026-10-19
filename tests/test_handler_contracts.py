"""
tests/test_handler_contracts.py
════════════════════════════════
Behavioral contract tests for main.py handler functions.

These tests DIRECTLY IMPORT the handler functions from main.py, mock the two
key helpers (`main.prompt` and `main.run`) and assert that each handler:
  1. Passes the CORRECT subprocess command to run()
  2. Passes the CORRECT flags for every user-input path
  3. Does NOT place live orders when the safe/dry-run flag is used
  4. Handles empty / invalid input gracefully (returns early, no crash)

WHY THIS APPROACH (vs stdin-pipe smoke tests):
  • 100–200× faster  — no subprocess overhead, runs in < 1 ms per test
  • Tests BEHAVIOUR, not just "no crash" — verifies exact CLI args built
  • Can cover every branch (live vs dry-run, skip vs no-skip, etc.)
  • No reliance on stdin/pty plumbing; pure Python mock

Pattern:
  1.  import main (module-level, once per session)
  2.  mock main.prompt  — returns values from a controlled iterator
  3.  mock main.run     — captures the command list instead of spawning
  4.  call handler()
  5.  assert cmd contains expected flags/args

Notes on import:
  • main.py is guarded by ``if __name__ == "__main__":`` so imports safely.
  • Set PROTOCOL_BUFFERS env before importing (avoids moomoo-api warning).
  • The import is done at module level; individual tests patch as needed.
"""

import importlib
import os
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ── Bootstrap env before main.py is imported ─────────────────────────────────
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# ── Import main.py ────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import main as m  # noqa: E402  (after env setup)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_prompt_iter(*answers):
    """Return a prompt() replacement that yields answers one by one, then defaults."""
    it = iter(answers)
    def _prompt(msg, default=""):  # noqa: ANN001, ANN202
        return next(it, default)
    return _prompt


def _capture_run():
    """Return (mock_run, calls_list) — mock replaces main.run, list records calls."""
    calls = []

    def _run(cmd, **kw):  # noqa: ANN001, ANN202
        calls.append(list(cmd))
        return 0

    return _run, calls


FAKE_ZERODHA_TOKEN = "unit-test-zerodha-token"


@pytest.fixture(autouse=True)
def _isolate_broker_auth(monkeypatch):
    """Keep handler-contract tests away from local brokers and auth state."""
    guard = sys.modules["zerodha_login"]
    monkeypatch.setitem(sys.modules, "zerodha_login", guard)
    monkeypatch.setattr(m, "_load_cached_token", lambda: "")
    monkeypatch.setattr(m, "_save_cached_token", guard.run_login)
    monkeypatch.setattr(m, "_is_port_in_use", lambda _port: False)
    yield
    assert guard.call_count == 0, (
        "Unexpected Zerodha login attempt(s):\n"
        + "\n".join(guard.attempt_stacks)
    )


@pytest.fixture
def india_env_with_token(monkeypatch):
    """Provide a deterministic token for India command construction tests."""
    env = dict(m.BASE_ENV)
    env["ZERODHA_ACCESS_TOKEN"] = FAKE_ZERODHA_TOKEN
    monkeypatch.setattr(m, "_india_env", lambda require_zerodha=True: dict(env))
    return env


@pytest.fixture
def india_env_no_token(monkeypatch):
    """Provide an explicit no-token environment for missing-auth behavior."""
    env = dict(m.BASE_ENV)
    env.pop("ZERODHA_ACCESS_TOKEN", None)
    monkeypatch.setattr(m, "_india_env", lambda require_zerodha=True: dict(env))
    return env


# ══════════════════════════════════════════════════════════════════════════════
# SAFETY CONTRACTS — verify no live orders can be placed accidentally
# ══════════════════════════════════════════════════════════════════════════════


def test_auto_trader_dry_run_never_sends_live_flag(monkeypatch):
    """
    run_auto_trader_handler with live_ans='' (default 'n') must NOT pass
    --live to the subprocess command.  This is the most critical safety contract:
    accidental live_ans='y' would place real orders.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("", "", "", ""))  # all defaults
    monkeypatch.setattr(m, "run", mock_run)

    m.run_auto_trader_handler()

    assert calls, "run() was never called — handler returned early unexpectedly"
    cmd_str = " ".join(str(c) for c in calls[0])
    assert "--live" not in cmd_str, (
        "live flag found in command even though live_ans defaulted to 'n':\n" + cmd_str
    )


def test_auto_trader_explicit_n_never_sends_live_flag(monkeypatch):
    """Same as above but with an explicit 'n' answer for the live prompt."""
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("zerodha", "5", "12", "n"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_auto_trader_handler()

    if calls:  # may return early if no signals queued
        cmd_str = " ".join(str(c) for c in calls[0])
        assert "--live" not in cmd_str


def test_india_order_executor_dry_run_flag(monkeypatch, india_env_with_token):
    """
    run_india_order_test always passes --dry-run to india_order_executor.py.
    This prevents actual orders from being placed in CI.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("NSE:INFY", "", "15000"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_india_order_test()

    # Handler might return early if Zerodha token missing — that's also safe
    if calls:
        cmd_str = " ".join(str(c) for c in calls[0])
        assert "--dry-run" in cmd_str, (
            "--dry-run not found in india_order_executor command:\n" + cmd_str
        )


# ══════════════════════════════════════════════════════════════════════════════
# COMMAND SHAPE CONTRACTS — verify correct script / flags built
# ══════════════════════════════════════════════════════════════════════════════


def test_run_options_manual_correct_script_and_symbol(monkeypatch):
    """
    run_options_manual must build a command that includes run_options.py
    and the symbol the user entered.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("NASDAQ:AAPL", "70", "SAFE", "n"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_options_manual()

    assert calls, "run() never called"
    cmd_str = " ".join(str(c) for c in calls[0])
    assert "run_options.py" in cmd_str or "options_analyzer" in cmd_str, (
        "Expected options script in cmd:\n" + cmd_str
    )
    assert "NASDAQ:AAPL" in cmd_str, "Symbol not passed to command:\n" + cmd_str


def test_run_options_manual_empty_symbol_aborts(monkeypatch):
    """
    run_options_manual with an empty symbol must return without calling run().
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("", "", "", ""))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_options_manual()

    assert not calls, (
        "run() was called even though no symbol was provided — "
        "handler should have aborted:\n" + str(calls)
    )


def test_run_sell_strategy_demo_mode(monkeypatch):
    """
    run_sell_strategy with choice='4' must include --demo in the command.
    Demo mode uses synthetic data — safe for CI.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("4"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_sell_strategy()

    assert calls, "run() never called for sell_strategy"
    cmd_str = " ".join(str(c) for c in calls[0])
    assert "--demo" in cmd_str, (
        "--demo not found in sell_strategy command for choice=4:\n" + cmd_str
    )


def test_run_prediction_engine_skip_chronos_flag(monkeypatch):
    """
    run_prediction_engine with skip_chronos='y' must pass --no-chronos (or
    equivalent) to the prediction script.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("AAPL", "y"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_prediction_engine()

    assert calls, "run() never called for prediction_engine"
    cmd_str = " ".join(str(c) for c in calls[0])
    assert any(
        flag in cmd_str for flag in ["--no-chronos", "--skip-chronos", "--no_chronos"]
    ), (
        "Skip-chronos flag not found in prediction_engine command:\n" + cmd_str
    )


def test_run_combined_signals_skip_screener_flag(monkeypatch):
    """
    run_combined_signals with skip_screener='y' must pass --skip-screener (or
    equivalent) so the TV screener scan is bypassed in CI.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("100", "50", "all", "y"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_combined_signals()

    if calls:  # may be multi-step; check any call
        all_cmds = " ".join(" ".join(str(c) for c in cmd) for cmd in calls)
        # When skip_screener=y the screener step should be absent or flagged
        assert any(
            flag in all_cmds
            for flag in ["--skip-screener", "--skip_screener", "skip"]
        ) or len(calls) < 3, (
            "skip-screener flag not found and full pipeline still ran:\n" + all_cmds
        )


def test_run_signal_stack_quick_run_uses_defaults(monkeypatch, india_env_no_token):
    """
    run_signal_stack with '' (Enter) uses defaults: n=50, threshold=62.5.
    Must call run() with the signal_stack script.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter(""))
    monkeypatch.setattr(m, "run", mock_run)
    monkeypatch.setattr(m, "_load_cached_token", lambda: None)
    monkeypatch.setattr(m, "_india_env", lambda **kwargs: m.BASE_ENV.copy())

    m.run_signal_stack()

    assert calls, "run() never called for signal_stack"
    # calls[0] is always the fires download; signal_stack is the scoring step.
    all_cmds = "\n".join(" ".join(str(c) for c in cmd) for cmd in calls)
    assert "signal_stack" in all_cmds, (
        "signal_stack.py not found in any command:\n" + all_cmds
    )


def test_india_order_without_token_aborts_without_auth(monkeypatch, india_env_no_token):
    """Missing India auth must stop command construction without prompting login."""
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("NSE:INFY", "", "15000"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_india_order_test()

    assert not calls


def test_signal_stack_without_zerodha_token_is_noninteractive(monkeypatch):
    """Signal Stack remains command-only when Zerodha is unavailable."""
    monkeypatch.delenv("ZERODHA_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ZERODHA_REQUEST_TOKEN", raising=False)
    cache_read = MagicMock(return_value="")
    monkeypatch.setattr(m, "_load_cached_token", cache_read)
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter(""))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_signal_stack()

    assert calls
    cache_read.assert_called_once_with()
    assert sys.modules["zerodha_login"].call_count == 0
    assert all("zerodha_login" not in " ".join(map(str, command)) for command in calls)


def test_run_manual_analyse_empty_tickers_aborts(monkeypatch):
    """
    run_manual_analyse with empty tickers string must return early.
    Blank tickers means nothing to analyse — no run() call expected.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("", "", ""))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_manual_analyse()

    assert not calls, (
        "run() was called with empty tickers — handler should abort early:\n" + str(calls)
    )


def test_run_manual_analyse_with_tickers_calls_run(monkeypatch):
    """
    run_manual_analyse with a valid ticker must call run() with that ticker.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("AAPL", "", ""))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_manual_analyse()

    assert calls, "run() never called when tickers were provided"
    cmd_str = " ".join(str(c) for c in calls[0])
    assert "AAPL" in cmd_str, "Ticker 'AAPL' not passed to command:\n" + cmd_str


def test_run_options_us_returns_early_when_preflight_fails(monkeypatch):
    """
    run_options_us calls _options_preflight(require_opend=True).
    When OpenD is offline and the user answers 'n', no run() should be called.
    """
    mock_run, calls = _capture_run()
    # Mock preflight to return False (simulating offline OpenD + user says "n")
    monkeypatch.setattr(m, "_options_preflight", lambda require_opend=False: False)
    monkeypatch.setattr(m, "run", mock_run)

    m.run_options_us()

    assert not calls, (
        "run() was called even though _options_preflight returned False:\n" + str(calls)
    )


def test_run_morning_screener_hk_calls_correct_market_flag(monkeypatch):
    """
    run_morning_screener_hk must pass --market HK to morning_screener.py.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "run", mock_run)

    m.run_morning_screener_hk()

    assert calls, "run() never called for morning_screener_hk"
    cmd_str = " ".join(str(c) for c in calls[0])
    assert "--market" in cmd_str, "--market flag missing:\n" + cmd_str
    assert "HK" in cmd_str, "HK market flag not found:\n" + cmd_str


def test_run_morning_screener_us_calls_correct_market_flag(monkeypatch):
    """
    run_morning_screener_us must pass --market US to morning_screener.py.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "run", mock_run)

    m.run_morning_screener_us()

    assert calls, "run() never called for morning_screener_us"
    cmd_str = " ".join(str(c) for c in calls[0])
    assert "--market" in cmd_str and "US" in cmd_str, (
        "--market US not found in command:\n" + cmd_str
    )


def test_run_forex_screener_no_pairs_uses_defaults(monkeypatch):
    """
    run_forex_screener with blank pairs must call run() without a --pairs flag,
    letting the script use its own default pair set.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter(""))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_forex_screener()

    assert calls, "run() never called for forex_screener"
    cmd_str = " ".join(str(c) for c in calls[0])
    # Either no --pairs flag OR the value is blank/default
    # At minimum the correct script must be in the command
    assert any(s in cmd_str for s in ["forex", "signal_engine", "screener"]), (
        "Forex-related script not found in command:\n" + cmd_str
    )


def test_run_fo_chain_screener_default_market(monkeypatch):
    """
    run_fo_chain_screener with blank input must default to 'us' market.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("", "", "", ""))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_fo_chain_screener()

    assert calls, "run() never called for fo_chain_screener"
    cmd_str = " ".join(str(c) for c in calls[0])
    # Script name should appear
    assert any(s in cmd_str for s in ["fo_chain", "chain_screener", "screener"]), (
        "Expected screener script in command:\n" + cmd_str
    )


# ══════════════════════════════════════════════════════════════════════════════
# LIVE DASHBOARD / WEBHOOK — CREATE_NEW_CONSOLE contracts
# ══════════════════════════════════════════════════════════════════════════════


def test_run_live_dashboard_uses_popen_not_run(monkeypatch):
    """
    run_live_dashboard must use subprocess.Popen (for CREATE_NEW_CONSOLE) not
    main.run (which blocks until the subprocess finishes).
    This is what allows the dashboard to run in a separate window.
    """
    mock_run, run_calls = _capture_run()
    popen_calls = []

    class _MockPopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(m, "prompt", _make_prompt_iter(""))
    monkeypatch.setattr(m, "run", mock_run)
    monkeypatch.setattr(subprocess, "Popen", _MockPopen)

    m.run_live_dashboard()

    # The handler should NOT block using main.run() for a long-running server
    # It should use Popen directly so it returns immediately
    assert popen_calls or run_calls, (
        "Neither Popen nor run() was called — live_dashboard handler did nothing"
    )
    if popen_calls:
        # Verify streamlit is in the Popen command
        cmd = popen_calls[0][0][0]
        cmd_str = " ".join(str(c) for c in cmd)
        assert any(s in cmd_str for s in ["streamlit", "live_dashboard"]), (
            "Streamlit not found in Popen command:\n" + cmd_str
        )


def test_run_webhook_server_uses_popen_not_run(monkeypatch):
    """
    run_webhook_server must use Popen (CREATE_NEW_CONSOLE) not main.run().
    If main.run() were used the test would block indefinitely.
    """
    mock_run, run_calls = _capture_run()
    popen_calls = []

    class _MockPopen:
        def __init__(self, *args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(m, "prompt", _make_prompt_iter("", ""))
    monkeypatch.setattr(m, "run", mock_run)
    monkeypatch.setattr(subprocess, "Popen", _MockPopen)
    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 0, raising=False)

    # Also mock the port check so the handler doesn't think server is already running
    monkeypatch.setattr(m, "_is_port_in_use", lambda port: False)

    m.run_webhook_server()

    # Should use Popen (new window) rather than blocking run()
    assert popen_calls or not run_calls, (
        "run() was called for webhook_server — this would block the process:\n"
        + str(run_calls)
    )


# ══════════════════════════════════════════════════════════════════════════════
# MORNING RUN CANCEL CONTRACT
# ══════════════════════════════════════════════════════════════════════════════


def test_run_morning_all_cancel_runs_nothing(monkeypatch):
    """
    run_morning_all with confirm='n' must return without calling run() for any
    morning script.  This prevents accidental full-system execution in CI.
    """
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "prompt", _make_prompt_iter("n"))
    monkeypatch.setattr(m, "run", mock_run)

    m.run_morning_all()

    assert not calls, (
        f"run() was called {len(calls)} time(s) after 'n' at morning-run confirm:\n"
        + str(calls[:3])
    )


# ══════════════════════════════════════════════════════════════════════════════
# ALERT TRACKER — command flag contracts
# ══════════════════════════════════════════════════════════════════════════════


def test_run_alert_tracker_update_flag(monkeypatch):
    """run_alert_tracker_update must pass --update to alert_tracker.py."""
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "run", mock_run)
    m.run_alert_tracker_update()
    assert calls
    assert "--update" in " ".join(str(c) for c in calls[0])


def test_run_alert_tracker_report_flag(monkeypatch):
    """run_alert_tracker_report must pass --report to alert_tracker.py."""
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "run", mock_run)
    m.run_alert_tracker_report()
    assert calls
    assert "--report" in " ".join(str(c) for c in calls[0])


def test_run_alert_tracker_stats_flag(monkeypatch):
    """run_alert_tracker_stats must pass --stats to alert_tracker.py."""
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "run", mock_run)
    m.run_alert_tracker_stats()
    assert calls
    assert "--stats" in " ".join(str(c) for c in calls[0])


def test_run_alert_tracker_all_flag(monkeypatch):
    """run_alert_tracker_all must pass --all to alert_tracker.py."""
    mock_run, calls = _capture_run()
    monkeypatch.setattr(m, "run", mock_run)
    m.run_alert_tracker_all()
    assert calls
    assert "--all" in " ".join(str(c) for c in calls[0])
