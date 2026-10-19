"""Failing-before / passing-after evidence for the subprocess deadline fix.

The pre-fix implementation drained ``proc.stdout`` to EOF *before* calling
``proc.wait(timeout=...)``, so a child that stopped printing but kept running
never reached the timed wait. This module reconstructs that exact code from the
pre-fix Git revision and demonstrates the defect, then shows the new runner
enforcing the same deadline.

The pre-fix source is read from Git (``git show <rev>:report_scheduler.py``) and
executed in an isolated namespace. The user's working tree is never reset.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from tradingview.scheduler.steps import STATUS_TIMEOUT, run_step

ROOT = Path(__file__).resolve().parents[1]

#: Revision whose ``_run_cmd`` still had the EOF-before-deadline defect.
# Pin the defective implementation so committing the fix cannot move the baseline.
PREFIX_REVISION = "be08692174367c1a03d5eacd21ff9e3eeefaf681"

#: The deadline is 1s; anything near this means the deadline was not enforced.
CI_BOUND_SECONDS = 25.0


def _prefix_run_cmd_source() -> str:
    """Extract the pre-fix ``_run_cmd`` function source from Git."""
    blob = subprocess.run(
        ["git", "show", f"{PREFIX_REVISION}:report_scheduler.py"],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    ).stdout
    lines = blob.split("\n")
    start = next(
        index for index, line in enumerate(lines) if line.startswith("def _run_cmd(")
    )
    end = start + 1
    while end < len(lines) and not lines[end].startswith("async def "):
        end += 1
    return "\n".join(lines[start:end])


def _load_prefix_run_cmd():
    """Execute the pre-fix function in an isolated namespace."""
    namespace: dict = {
        "subprocess": subprocess,
        "_STEP_TIMEOUT": 900,
        "BASE_ENV": {},
    }
    exec(compile(_prefix_run_cmd_source(), "prefix-report_scheduler", "exec"), namespace)
    return namespace["_run_cmd"]


def test_prefix_source_is_available():
    source = _prefix_run_cmd_source()
    assert "for line in proc.stdout" in source
    assert "proc.wait(timeout=timeout)" in source


def test_prefix_runner_blocks_past_its_deadline():
    """FAILING-BEFORE: the pre-fix runner ignores its own timeout.

    The child sleeps silently for 20s while the configured deadline is 1s. The
    pre-fix loop never reaches ``proc.wait(timeout=...)``, so the call blocks
    for the child's full lifetime and returns a normal exit code instead of
    reporting a timeout.
    """
    prefix_run_cmd = _load_prefix_run_cmd()
    started = time.monotonic()
    rc, output = prefix_run_cmd(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        ROOT,
        None,
        1,
    )
    elapsed = time.monotonic() - started

    assert elapsed > 10, (
        f"pre-fix runner unexpectedly honoured its 1s deadline (took {elapsed:.1f}s)"
    )
    assert rc == 0, "pre-fix runner did not report the child as timed out"
    assert "TIMEOUT" not in output


def test_new_runner_enforces_the_same_deadline():
    """PASSING-AFTER: the new runner stops the identical child at 1s."""
    started = time.monotonic()
    result = run_step(
        "silent-sleeper",
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=ROOT,
        timeout=1,
    )
    elapsed = time.monotonic() - started

    assert result.status == STATUS_TIMEOUT
    assert elapsed < CI_BOUND_SECONDS, f"deadline not enforced (took {elapsed:.1f}s)"
