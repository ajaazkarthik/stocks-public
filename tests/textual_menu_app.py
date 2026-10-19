"""
tests/textual_menu_app.py
══════════════════════════
Minimal Textual TUI wrapper around 3 stocks-system menu options.
Used to demonstrate and benchmark the Textual Pilot API against the
stdin-pipe harness (tests/test_smoke.py).

Architecture decision:
  main.py uses plain input() calls — NOT a Textual App.
  The Textual Pilot API only works with textual.app.App subclasses.
  This module creates a thin Textual wrapper around 3 scripts to enable
  a fair head-to-head benchmark.

  Subprocess calls use @work(thread=True) so they never block Textual's
  asyncio event loop — the Pilot API stays fully responsive during runs.

Usage:
  python tests/textual_menu_app.py            # run interactively
  pytest tests/test_textual_pilot.py -v      # run Pilot benchmark tests
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Button, Footer, Header, Label, RichLog, Static

ROOT = Path(__file__).parent.parent

# Menu options exposed in this demo TUI (same 3 options covered in tier1 tests)
DEMO_ACTIONS: dict[str, dict] = {
    "3": {
        "label": "Portfolio Analytics",
        "cmd": [
            sys.executable,
            str(ROOT / "tradingview" / "analysis" / "portfolio_score_tracker.py"),
            "--out",
            str(ROOT / "reports" / "portfolio_score_tracker.html"),
        ],
    },
    "43": {
        "label": "Alert Tracker Stats",
        "cmd": [
            sys.executable,
            str(ROOT / "tradingview" / "alert_stats.py"),
        ],
    },
    "4a": {
        "label": "P&L Attribution",
        "cmd": [
            sys.executable,
            str(ROOT / "tradingview" / "analysis" / "pnl_attribution.py"),
            "--out",
            str(ROOT / "reports" / "pnl_attribution.html"),
        ],
    },
}

_BASE_ENV = {
    **os.environ,
    "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
    "PYTHONIOENCODING": "utf-8",
    "OPENBLAS_NUM_THREADS": "1",
}


class StocksMenuApp(App):
    """
    Minimal Textual demo of the stocks menu.

    Public attributes set by the worker (read from tests after run):
      _last_option (str):  last option ID whose worker completed
      _last_rc (int):      exit code of last subprocess
      _last_output (str):  stdout+stderr of last subprocess
      _run_time_s (float): wall-clock seconds consumed by subprocess
    """

    TITLE = "Stocks Menu (Textual Demo)"
    SUB_TITLE = "Pilot API Benchmark Sample"
    CSS = """
    Vertical { padding: 1 2; }
    Label { color: $text-muted; margin-bottom: 1; }
    Button { margin-bottom: 1; width: 100%; }
    RichLog { height: 20; border: solid $primary; }
    """
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_option: str = ""
        self._last_rc: int = -1
        self._last_output: str = ""
        self._run_time_s: float = 0.0

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Label("Select an action:")
            for key, info in DEMO_ACTIONS.items():
                yield Button(f"[{key}] {info['label']}", id=f"btn_{key}", variant="primary")
            yield Static("", id="status")
            yield RichLog(id="output_log", markup=True, highlight=True)
        yield Footer()

    # ── Event handlers ────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        option = btn_id.replace("btn_", "")
        if option in DEMO_ACTIONS:
            # Reset marker so tests can detect when THIS run completes
            self._last_option = ""
            self._run_option_worker(option)

    # ── Worker ────────────────────────────────────────────────────────────────

    @work(thread=True)
    def _run_option_worker(self, option: str) -> None:
        """
        Run the selected script in a background thread.

        @work(thread=True) keeps the asyncio event loop free, so the Pilot
        API can still process events while the subprocess is running.
        """
        info = DEMO_ACTIONS[option]
        t0 = time.perf_counter()
        try:
            result = subprocess.run(
                info["cmd"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                env=_BASE_ENV,
                timeout=120,
            )
            elapsed = time.perf_counter() - t0
            combined = result.stdout + result.stderr
            rc = result.returncode
        except subprocess.TimeoutExpired:
            elapsed = time.perf_counter() - t0
            combined = "TIMEOUT after 120s"
            rc = -1

        # Store results — simple attribute writes are thread-safe here
        self._last_rc = rc
        self._last_output = combined
        self._run_time_s = elapsed
        # Set _last_option LAST: tests poll this to detect completion
        self._last_option = option

        # Schedule UI update on the main event loop
        self.call_from_thread(self._update_display, option, rc, elapsed, combined)

    def _update_display(
        self, option: str, rc: int, elapsed: float, combined: str
    ) -> None:
        """Update UI widgets from the main thread (called via call_from_thread)."""
        try:
            log = self.query_one("#output_log", RichLog)
            status = self.query_one("#status", Static)
            tail = "\n".join(combined.splitlines()[-40:])
            log.write(tail)
            rc_style = "green" if rc == 0 else "red"
            status.update(
                f"[{rc_style}]Option {option} done in {elapsed:.2f}s (exit {rc})[/]"
            )
        except Exception:  # noqa: BLE001 — widget may be unmounted during tests
            pass


if __name__ == "__main__":
    StocksMenuApp().run()
