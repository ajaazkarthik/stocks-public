"""Subprocess execution with real deadlines and structured step results.

The previous implementation iterated ``proc.stdout`` to EOF *before* calling
``proc.wait(timeout=...)``. A child that stopped printing but kept running — or
one that printed forever — never reached the timed wait, so the 900-second
deadline was unenforceable.

Here the deadline starts when the process is launched. Output is drained by a
separate reader thread into a bounded buffer, so timeout enforcement never
depends on EOF, and the captured text stays bounded no matter how chatty the
child is.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from tradingview.scheduler.config import redact

#: Statuses a step can report.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_LAUNCH_ERROR = "launch_error"
STATUS_CANCELLED = "cancelled"
STATUS_SKIPPED = "skipped"

#: Statuses that mean "this step did not produce a fresh result".
UNSUCCESSFUL_STATUSES = frozenset({
    STATUS_FAILED, STATUS_TIMEOUT, STATUS_LAUNCH_ERROR, STATUS_CANCELLED,
})

#: How long to wait for a terminated child before escalating to SIGKILL.
DEFAULT_GRACE_SECONDS = 5.0

#: Maximum characters retained from a child's combined output.
DEFAULT_CAPTURE_LIMIT = 8000

#: Characters of captured output kept for the sanitized error summary.
ERROR_SUMMARY_LIMIT = 400


@dataclass
class StepResult:
    """Outcome of one pipeline step."""

    label: str
    status: str
    exit_code: int | None = None
    duration_seconds: float = 0.0
    output: str = ""
    error: str = ""
    command: tuple[str, ...] = ()
    started_at: str = ""
    completed_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def produced_fresh_result(self) -> bool:
        """True only for a clean exit — a timeout or failure is never fresh."""
        return self.status == STATUS_OK

    def summary(self) -> str:
        """Return a short, secret-free one-line description."""
        if self.status == STATUS_OK:
            return f"{self.label}: ok ({self.duration_seconds:.1f}s)"
        detail = self.error or f"exit {self.exit_code}"
        return f"{self.label}: {self.status} — {redact(detail)}"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": redact(self.error),
            "command": list(self.command),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class _BoundedCapture:
    """Thread-safe bounded text buffer fed by a reader thread."""

    def __init__(self, limit: int = DEFAULT_CAPTURE_LIMIT) -> None:
        self._limit = max(1, int(limit))
        self._chunks: list[str] = []
        self._size = 0
        self._lock = threading.Lock()
        self._truncated = False

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._size += len(text)
            while self._size > self._limit and len(self._chunks) > 1:
                removed = self._chunks.pop(0)
                self._size -= len(removed)
                self._truncated = True
            if self._size > self._limit:
                overflow = self._size - self._limit
                self._chunks[0] = self._chunks[0][overflow:]
                self._size = self._limit
                self._truncated = True

    def value(self) -> str:
        with self._lock:
            text = "".join(self._chunks)
            if self._truncated:
                return "[…earlier output truncated…]\n" + text
            return text


def _start_reader(stream, capture: _BoundedCapture) -> threading.Thread:
    def _pump() -> None:
        try:
            for line in iter(stream.readline, ""):
                capture.append(line)
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except (ValueError, OSError):
                pass

    thread = threading.Thread(target=_pump, name="sched-step-reader", daemon=True)
    thread.start()
    return thread


def _popen_kwargs() -> dict:
    """Platform-specific process-group isolation for reliable cleanup."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _group_alive(pgid: int | None) -> bool:
    """Return True when any process in *pgid* still exists."""
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_tree(proc: subprocess.Popen, grace: float) -> None:
    """Terminate *proc* and its descendants, escalating to SIGKILL.

    The group's lifetime is managed **independently of the parent's exit**. A
    descendant that ignores SIGTERM (or that outlives its parent) would
    otherwise keep the inherited stdout pipe open, and the reader thread would
    stay blocked — defeating the deadline entirely.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        return

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    def _signal(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # SIGTERM the whole group, then wait for the *group* — not just the parent.
    _signal(signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if proc.poll() is None:
            try:
                proc.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        if not _group_alive(pgid):
            break
        time.sleep(0.02)

    # Escalate for anything still alive in the group.
    if _group_alive(pgid) or proc.poll() is None:
        _signal(signal.SIGKILL)
        kill_deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < kill_deadline:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if not _group_alive(pgid):
                break
            time.sleep(0.02)

    # Reap the direct child so it cannot linger as a zombie.
    if proc.poll() is None:
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def run_step(
    label: str,
    cmd: Sequence[str],
    cwd: Path | str | None = None,
    env: dict | None = None,
    timeout: float = 900,
    *,
    capture_limit: int = DEFAULT_CAPTURE_LIMIT,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    popen: Callable[..., subprocess.Popen] | None = None,
    clock: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> StepResult:
    """Run *cmd* with a deadline that starts at launch.

    Returns a :class:`StepResult` for every outcome — including launch failure
    and cancellation — so a caller never has to guess whether a step ran.
    """
    from tradingview.scheduler.timeutil import utc_now_iso

    command = tuple(str(part) for part in cmd)
    started_iso = utc_now_iso()
    started = clock()
    capture = _BoundedCapture(capture_limit)
    proc: subprocess.Popen | None = None
    reader: threading.Thread | None = None
    status = STATUS_OK
    exit_code: int | None = None
    error = ""

    spawn = popen or subprocess.Popen
    try:
        try:
            proc = spawn(
                list(command),
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_popen_kwargs(),
            )
        except (OSError, ValueError) as exc:
            status = STATUS_LAUNCH_ERROR
            error = f"could not launch: {type(exc).__name__}: {exc}"
            return StepResult(
                label=label, status=status, exit_code=None,
                duration_seconds=clock() - started, output="",
                error=error, command=command,
                started_at=started_iso, completed_at=utc_now_iso(),
            )

        reader = _start_reader(proc.stdout, capture)

        deadline = started + max(0.0, float(timeout))
        while True:
            if cancelled is not None and cancelled():
                status = STATUS_CANCELLED
                error = "cancelled"
                _terminate_tree(proc, grace_seconds)
                break
            remaining = deadline - clock()
            if remaining <= 0:
                status = STATUS_TIMEOUT
                error = f"exceeded {timeout:g}s deadline"
                _terminate_tree(proc, grace_seconds)
                break
            try:
                exit_code = proc.wait(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        if status == STATUS_OK:
            exit_code = proc.returncode
            if exit_code != 0:
                status = STATUS_FAILED
                error = f"exit code {exit_code}"
    finally:
        if proc is not None:
            if proc.poll() is None:
                _terminate_tree(proc, grace_seconds)
            if reader is not None:
                reader.join(timeout=grace_seconds)
            # Closing a pipe whose reader thread is still blocked would block on
            # the stream lock. Only close once the reader has actually finished;
            # otherwise leave the daemon thread to drain and exit on its own.
            if reader is None or not reader.is_alive():
                for stream in (proc.stdout, proc.stderr, proc.stdin):
                    if stream is not None:
                        try:
                            stream.close()
                        except (ValueError, OSError):
                            pass

    output = capture.value()
    if status == STATUS_TIMEOUT:
        output = (output + f"\n[TIMEOUT] step exceeded {timeout:g}s — terminated.").strip()
    if status in UNSUCCESSFUL_STATUSES and not error:
        error = f"exit code {exit_code}"

    return StepResult(
        label=label,
        status=status,
        exit_code=exit_code,
        duration_seconds=clock() - started,
        output=output,
        error=error,
        command=command,
        started_at=started_iso,
        completed_at=utc_now_iso(),
    )


def error_summary(result: StepResult, limit: int = ERROR_SUMMARY_LIMIT) -> str:
    """Return a bounded, redacted error summary for logs and messages."""
    detail = result.error or ""
    tail = result.output[-limit:] if result.output else ""
    combined = f"{detail}\n{tail}".strip() if tail else detail
    return redact(combined)[:limit]


__all__ = [
    "DEFAULT_CAPTURE_LIMIT",
    "DEFAULT_GRACE_SECONDS",
    "ERROR_SUMMARY_LIMIT",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_LAUNCH_ERROR",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_TIMEOUT",
    "UNSUCCESSFUL_STATUSES",
    "StepResult",
    "error_summary",
    "run_step",
]
