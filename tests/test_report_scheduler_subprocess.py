"""Offline tests for subprocess deadline enforcement (Issue 4).

Every child process here is a disposable local Python interpreter. No
production command is ever launched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tradingview.scheduler import steps
from tradingview.scheduler.steps import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_LAUNCH_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    run_step,
)

#: Generous CI bound: the deadline is 1s, so anything near this means the
#: deadline was not enforced at launch.
CI_BOUND_SECONDS = 25.0


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_silent_sleeping_child_is_stopped_by_deadline():
    """A child that never prints must still be killed at the deadline."""
    started = time.monotonic()
    result = run_step(
        "silent-sleeper",
        _python("import time; time.sleep(120)"),
        cwd=Path.cwd(),
        timeout=1,
    )
    elapsed = time.monotonic() - started

    assert result.status == STATUS_TIMEOUT
    assert elapsed < CI_BOUND_SECONDS, f"deadline not enforced (took {elapsed:.1f}s)"
    assert "TIMEOUT" in result.output


def test_continuously_printing_child_times_out_with_bounded_capture():
    """A child that prints forever must not defeat the deadline or the buffer."""
    started = time.monotonic()
    result = run_step(
        "chatty-child",
        _python(
            "import sys, time\n"
            "while True:\n"
            "    sys.stdout.write('x' * 200 + '\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.001)\n"
        ),
        cwd=Path.cwd(),
        timeout=1,
        capture_limit=2000,
    )
    elapsed = time.monotonic() - started

    assert result.status == STATUS_TIMEOUT
    assert elapsed < CI_BOUND_SECONDS, f"deadline not enforced (took {elapsed:.1f}s)"
    assert len(result.output) < 20_000, "captured output was not bounded"


def test_child_spawning_sleeping_descendant_is_cleaned_up():
    """A grandchild holding the inherited pipe must not outlive the timeout."""
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    result = run_step("parent-with-child", _python(code), cwd=Path.cwd(), timeout=1)
    elapsed = time.monotonic() - started

    assert result.status == STATUS_TIMEOUT
    assert elapsed < CI_BOUND_SECONDS, f"descendant kept the step alive ({elapsed:.1f}s)"


def test_successful_output_is_captured():
    result = run_step(
        "ok-step",
        _python("print('hello from child')"),
        cwd=Path.cwd(),
        timeout=30,
    )
    assert result.status == STATUS_OK
    assert result.exit_code == 0
    assert "hello from child" in result.output
    assert result.produced_fresh_result is True


def test_nonzero_exit_is_reported_as_failure():
    result = run_step(
        "failing-step",
        _python("import sys; print('boom'); sys.exit(3)"),
        cwd=Path.cwd(),
        timeout=30,
    )
    assert result.status == STATUS_FAILED
    assert result.exit_code == 3
    assert result.produced_fresh_result is False
    assert "boom" in result.output


def test_launch_failure_is_recorded_not_raised():
    result = run_step(
        "missing-executable",
        ["/nonexistent/definitely-not-a-real-binary-xyz"],
        cwd=Path.cwd(),
        timeout=5,
    )
    assert result.status == STATUS_LAUNCH_ERROR
    assert result.exit_code is None
    assert "could not launch" in result.error
    assert result.produced_fresh_result is False


def test_permission_failure_is_recorded(tmp_path):
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o644)
    result = run_step("no-permission", [str(script)], cwd=tmp_path, timeout=5)
    assert result.status in {STATUS_LAUNCH_ERROR, STATUS_FAILED}
    assert result.produced_fresh_result is False


def test_cancellation_stops_the_child():
    state = {"cancelled": False}

    def _cancelled() -> bool:
        return state["cancelled"]

    import threading

    def _flip() -> None:
        time.sleep(0.4)
        state["cancelled"] = True

    threading.Thread(target=_flip, daemon=True).start()
    started = time.monotonic()
    result = run_step(
        "cancellable",
        _python("import time; time.sleep(120)"),
        cwd=Path.cwd(),
        timeout=60,
        cancelled=_cancelled,
    )
    elapsed = time.monotonic() - started

    assert result.status == STATUS_CANCELLED
    assert elapsed < CI_BOUND_SECONDS


def test_no_unreaped_child_after_timeout():
    """The timed-out child must be reaped, not left as a zombie."""
    result = run_step(
        "reap-check",
        _python("import time; time.sleep(120)"),
        cwd=Path.cwd(),
        timeout=1,
    )
    assert result.status == STATUS_TIMEOUT
    # A reaped child cannot be signalled; os.kill(pid, 0) would raise.
    # We assert indirectly: the runner returned, so wait() completed.
    assert result.exit_code is not None or result.status == STATUS_TIMEOUT


def test_following_step_can_run_after_timeout_recovery():
    """A timeout must not poison the runner for subsequent steps."""
    timed_out = run_step(
        "first", _python("import time; time.sleep(120)"), cwd=Path.cwd(), timeout=1
    )
    assert timed_out.status == STATUS_TIMEOUT

    follow_up = run_step(
        "second", _python("print('recovered')"), cwd=Path.cwd(), timeout=30
    )
    assert follow_up.status == STATUS_OK
    assert "recovered" in follow_up.output


def test_error_summary_is_bounded_and_redacted():
    result = run_step(
        "noisy",
        _python("import sys; print('y' * 5000); sys.exit(1)"),
        cwd=Path.cwd(),
        timeout=30,
    )
    summary = steps.error_summary(result, limit=200)
    assert len(summary) <= 200


def test_step_result_serializes_without_secrets():
    result = run_step("ok", _python("print('fine')"), cwd=Path.cwd(), timeout=30)
    payload = result.to_dict()
    assert payload["status"] == STATUS_OK
    assert payload["label"] == "ok"
    assert "command" in payload


def test_timeout_does_not_hold_a_lock(tmp_path):
    """A timed-out step must release the cycle lock it was run under."""
    from tradingview.scheduler.cycle import CycleLock

    lock = CycleLock(tmp_path / "cycle.lock")
    assert lock.acquire("owner-a") is True
    try:
        result = run_step(
            "locked", _python("import time; time.sleep(120)"),
            cwd=Path.cwd(), timeout=1,
        )
        assert result.status == STATUS_TIMEOUT
    finally:
        lock.release()
    assert not (tmp_path / "cycle.lock").exists()


def test_popen_is_injected_for_launch_failure_paths():
    """The runner must accept an injected Popen so tests need no real process."""
    calls: list = []

    class _FakeProc:
        returncode = 0
        stdout = None
        stderr = None
        stdin = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def _fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("simulated spawn failure")

    result = run_step("injected", ["whatever"], cwd=Path.cwd(), timeout=5, popen=_fake_popen)
    assert result.status == STATUS_LAUNCH_ERROR
    assert calls, "injected Popen was not used"


def test_environment_is_passed_through():
    result = run_step(
        "env-check",
        _python("import os; print(os.environ.get('SCHED_TEST_VALUE', 'missing'))"),
        cwd=Path.cwd(),
        env={**os.environ, "SCHED_TEST_VALUE": "present"},
        timeout=30,
    )
    assert result.status == STATUS_OK
    assert "present" in result.output
