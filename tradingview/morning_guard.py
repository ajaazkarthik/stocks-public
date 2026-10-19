"""
morning_guard.py — Cooldown-based guard to prevent concurrent morning runs.

Prevents flooding caused by multiple systems (main.py, telegram_bot.py,
report_scheduler.py) triggering independent morning run Telegram notifications
within the same 2-hour window.

Design rationale (from Stocks Critic review 2026-05-19):
- The root cause of 60+ Telegram messages is THREE independent systems firing
  morning run notifications simultaneously, not the number of messages each sends.
- A lock FILE is crash-unsafe on Windows (file remains if process crashes).
- This module uses SQLite + PID liveness check: if the claiming process dies,
  the next caller can reclaim the slot safely.

Usage:
    from tradingview.morning_guard import try_claim_morning_run, release_morning_run

    if not try_claim_morning_run("my_caller"):
        print("Morning run already in progress — skipping")
        return

    try:
        run_morning_pipeline()
    finally:
        release_morning_run()
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

# Shared DB — same as telegram_queue.py so we don't scatter .db files
_DB = Path(__file__).parent.parent / "telegram_alert_queue.db"
_COOLDOWN_SECS: int = 7200  # 2 hours between morning runs


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_DB), timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=3000")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS morning_run_claim (
            id         INTEGER PRIMARY KEY CHECK(id = 1),
            claimed_at REAL    NOT NULL,
            caller     TEXT    NOT NULL,
            pid        INTEGER NOT NULL
        )
        """
    )
    con.commit()
    return con


def _pid_alive(pid: int) -> bool:
    """
    Return True if the given PID is still running on this machine.
    Windows-safe: uses ctypes.windll on Windows (os.kill with signal 0
    calls TerminateProcess on Windows and must not be used for liveness checks).
    """
    if pid == os.getpid():
        return True  # fast path: we're definitely alive
    if sys.platform == "win32":
        # Windows: use OpenProcess to probe existence without killing anything
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False  # process does not exist or access denied (treat as dead)
    else:
        # POSIX: signal 0 is a safe existence check
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True   # exists, but we can't signal it
        except OSError:
            return False  # no such process


def try_claim_morning_run(caller: str) -> bool:
    """
    Atomically claim the morning run slot.

    Returns True  — this process successfully claimed the slot; proceed with run.
    Returns False — another process ran within the cooldown window and is still alive.

    Crash-safe: if the previous claiming process no longer exists (PID check fails),
    the slot is released and the new caller can claim it.
    """
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT claimed_at, pid, caller FROM morning_run_claim WHERE id = 1"
            ).fetchone()

            if row is not None:
                claimed_at, pid, prev_caller = row
                age_secs = time.time() - claimed_at
                if age_secs < _COOLDOWN_SECS and _pid_alive(pid):
                    # Active claim — skip
                    return False
                # Stale claim (cooldown expired OR process dead) — allow re-claim

            con.execute(
                "INSERT OR REPLACE INTO morning_run_claim VALUES (1, ?, ?, ?)",
                (time.time(), caller, os.getpid()),
            )
            return True
    except Exception:
        # If the guard itself fails, allow the run to proceed (fail-open).
        # Better to have occasional duplicates than to permanently block runs.
        return True


def release_morning_run() -> None:
    """
    Release the morning run claim so the next caller can proceed earlier.

    Call this in a finally block after the morning run completes.
    Only releases if the PID matches this process (prevents race conditions).
    """
    try:
        with _connect() as con:
            con.execute(
                "DELETE FROM morning_run_claim WHERE id = 1 AND pid = ?",
                (os.getpid(),),
            )
    except Exception:
        pass  # Release is best-effort — cooldown TTL handles the rest


def get_claim_status() -> dict | None:
    """
    Return current claim info or None if no active claim.
    Useful for diagnostics / tests.
    """
    con = None
    try:
        con = _connect()
        row = con.execute(
            "SELECT claimed_at, caller, pid FROM morning_run_claim WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        claimed_at, caller, pid = row
        return {
            "claimed_at": claimed_at,
            "caller": caller,
            "pid": pid,
            "age_secs": time.time() - claimed_at,
            "pid_alive": _pid_alive(pid),
        }
    except Exception:
        return None
    finally:
        if con:
            con.close()
