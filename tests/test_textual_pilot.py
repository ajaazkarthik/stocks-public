"""
tests/test_textual_pilot.py
════════════════════════════
Textual Pilot API benchmark tests for the StocksMenuApp demo.

Textual's Pilot API simulates user interactions (button clicks, key presses)
inside a real TUI app running headlessly.  Compare vs test_smoke.py (stdin-pipe):

  stdin-pipe approach (test_smoke.py):
    - Spawns a NEW subprocess per test
    - Sends all inputs at once via stdin pipe
    - Works with plain-input() main.py (no rewrite needed)
    - Process startup cost: ~300-500ms per test

  Textual Pilot approach (this file):
    - Creates a Textual App object inside the test process
    - Simulates button clicks + key presses via async Pilot
    - Requires app rewritten as textual.app.App (~300 lines)
    - App startup cost: ~50-150ms per test
    - Can simulate multi-action sessions in ONE app lifecycle

Verdict evidence is collected by test_pilot_timing_overhead() and
test_pilot_sequential_two_options() and reported in BENCHMARK_REPORT.md.

Usage:
  pytest tests/test_textual_pilot.py -v --durations=0
  pytest tests/test_textual_pilot.py -v -k "timing"  # just benchmark
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# ── Guard: textual must be installed ──────────────────────────────────────────
try:
    from textual.pilot import Pilot  # noqa: F401
    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not TEXTUAL_AVAILABLE,
    reason="textual not installed — pip install textual",
)

# ── Local import (tests/ has no __init__.py; pytest adds dir to sys.path) ────
# Guard: only import when textual is available — pytestmark skips test functions
# but does NOT prevent module-level imports from running during collection.
sys.path.insert(0, str(Path(__file__).parent))
if TEXTUAL_AVAILABLE:
    from textual_menu_app import DEMO_ACTIONS, StocksMenuApp  # noqa: E402
else:
    DEMO_ACTIONS = None  # type: ignore[assignment]
    StocksMenuApp = None  # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════════════════════
# Pilot helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _wait_for_option(
    pilot: "Pilot", app: StocksMenuApp, option_id: str, timeout: float = 130.0
) -> None:
    """
    Poll ``app._last_option`` until the worker for *option_id* has finished.

    The worker sets ``_last_option`` as its very last action, so when it
    matches we know subprocess + UI update have both completed.
    """
    deadline = time.monotonic() + timeout
    while app._last_option != option_id:
        await pilot.pause(0.3)
        if time.monotonic() > deadline:
            pytest.fail(
                f"Option {option_id!r} worker did not complete within {timeout:.0f}s"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Test 1 — App renders with all three buttons
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_app_renders_all_buttons():
    """Pilot: headless app starts and exposes one Button per DEMO_ACTIONS entry."""
    app = StocksMenuApp()
    async with app.run_test(headless=True) as pilot:
        for option_id in DEMO_ACTIONS:
            btn = app.query_one(f"#btn_{option_id}")
            assert btn is not None, f"Button for option {option_id!r} not found"


# ══════════════════════════════════════════════════════════════════════════════
# Test 2 — Quit key exits cleanly
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_quit_key():
    """Pilot: pressing 'q' quits the app without exception."""
    app = StocksMenuApp()
    async with app.run_test(headless=True) as pilot:
        await pilot.press("q")
    # No exception = pass (run_test context manager calls app.exit() on __aexit__)


# ══════════════════════════════════════════════════════════════════════════════
# Test 3 — Option 43: Alert Tracker Stats
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_pilot_option_43_alert_stats():
    """
    Pilot: click [43] Alert Tracker Stats → worker completes → no traceback.

    Empty alert_tracker.db is OK (graceful empty output), crash is not.
    """
    app = StocksMenuApp()
    async with app.run_test(headless=True) as pilot:
        await pilot.click("#btn_43")
        await _wait_for_option(pilot, app, "43")

    assert app._last_option == "43"
    assert "Traceback (most recent call last)" not in app._last_output, (
        f"Option 43 produced a Python traceback:\n{app._last_output[-2000:]}"
    )
    assert app._run_time_s > 0.0, "Run time was not recorded"


# ══════════════════════════════════════════════════════════════════════════════
# Test 4 — Option 3: Portfolio Analytics
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_pilot_option_3_portfolio_analytics():
    """Pilot: click [3] Portfolio Analytics → no traceback."""
    app = StocksMenuApp()
    async with app.run_test(headless=True) as pilot:
        await pilot.click("#btn_3")
        await _wait_for_option(pilot, app, "3")

    assert app._last_option == "3"
    assert "Traceback (most recent call last)" not in app._last_output


# ══════════════════════════════════════════════════════════════════════════════
# Test 5 — Option 4a: P&L Attribution
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_pilot_option_4a_pnl_attribution():
    """Pilot: click [4a] P&L Attribution → no traceback."""
    app = StocksMenuApp()
    async with app.run_test(headless=True) as pilot:
        await pilot.click("#btn_4a")
        await _wait_for_option(pilot, app, "4a")

    assert app._last_option == "4a"
    assert "Traceback (most recent call last)" not in app._last_output


# ══════════════════════════════════════════════════════════════════════════════
# Test 6 — Sequential multi-option session (unique Textual capability)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.timeout(200)
async def test_pilot_sequential_two_options():
    """
    Pilot: run two options in ONE app session without restarting.

    This is impossible with the stdin-pipe approach (which spawns a fresh
    process per test).  Textual Pilot maintains persistent app state across
    multiple interactions — closer to real user behaviour.
    """
    app = StocksMenuApp()
    async with app.run_test(headless=True) as pilot:
        # First: option 43 (fast)
        await pilot.click("#btn_43")
        await _wait_for_option(pilot, app, "43")
        first_rc = app._last_rc
        first_output = app._last_output

        # Second: option 4a (in same session)
        await pilot.click("#btn_4a")
        await _wait_for_option(pilot, app, "4a")

    # Both must complete without traceback
    assert "Traceback" not in first_output, "Option 43 crashed"
    assert "Traceback" not in app._last_output, "Option 4a crashed"


# ══════════════════════════════════════════════════════════════════════════════
# Test 7 — Timing benchmark: measures Pilot API overhead
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_pilot_timing_overhead():
    """
    Benchmarks Textual Pilot startup and teardown overhead.

    Compares to stdin-pipe startup (measured via --durations=0 on test_smoke.py).
    Prints timing breakdown to stdout — captured by pytest -s.
    """
    t_start = time.perf_counter()

    app = StocksMenuApp()
    async with app.run_test(headless=True) as pilot:
        t_app_ready = time.perf_counter()

        await pilot.click("#btn_43")
        await _wait_for_option(pilot, app, "43")

        t_after_run = time.perf_counter()

    t_total = time.perf_counter()

    startup_ms = (t_app_ready - t_start) * 1000
    subprocess_ms = app._run_time_s * 1000
    teardown_ms = (t_total - t_after_run) * 1000
    total_ms = (t_total - t_start) * 1000

    print(f"\n[Textual Pilot timing — option 43]")
    print(f"  App startup overhead : {startup_ms:7.1f} ms")
    print(f"  Subprocess run time  : {subprocess_ms:7.1f} ms  (same work as stdin-pipe)")
    print(f"  App teardown         : {teardown_ms:7.1f} ms")
    print(f"  Total wall clock     : {total_ms:7.1f} ms")
    print(f"  Subprocess RC        : {app._last_rc}")

    # Startup should be < 5 s on any reasonable machine
    assert startup_ms < 5_000, f"App startup too slow: {startup_ms:.0f}ms > 5000ms"
