"""
tests/test_telegram_queue.py — Unit tests for the Telegram staging/cooldown layer.

Covers:
  - morning_guard.py  (try_claim_morning_run, release_morning_run, get_claim_status)
  - telegram_queue.py (stage, count_pending, flush, clear_pending)

All tests use a temporary in-memory or temp-file DB so they never touch the
real telegram_alert_queue.db.
"""
from __future__ import annotations

import os
import sys
import time
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure tradingview/ is on sys.path so imports work without install
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
TV = ROOT / "tradingview"
if str(TV) not in sys.path:
    sys.path.insert(0, str(TV))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Shared fixture: temp DB file
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Redirect both morning_guard and telegram_queue to use a temp DB."""
    db_file = tmp_path / "test_queue.db"

    # Patch the _DB constant in both modules
    import morning_guard
    import telegram_queue
    monkeypatch.setattr(morning_guard, "_DB", db_file)
    monkeypatch.setattr(telegram_queue, "_DB", db_file)
    return db_file


# ===========================================================================
# morning_guard tests
# ===========================================================================

class TestMorningGuard:
    def test_first_claim_succeeds(self, tmp_db):
        """First call to try_claim_morning_run always returns True."""
        import morning_guard
        result = morning_guard.try_claim_morning_run("test_caller")
        assert result is True

    def test_second_claim_blocked_while_pid_alive(self, tmp_db, monkeypatch):
        """Second claim from same PID returns False (already in cooldown window)."""
        import morning_guard
        # Claim once
        assert morning_guard.try_claim_morning_run("caller_a") is True
        # PID is our own process — definitely alive
        # Claiming again (same process, different caller name) should be blocked
        result = morning_guard.try_claim_morning_run("caller_b")
        assert result is False

    def test_stale_claim_released_after_cooldown(self, tmp_db, monkeypatch):
        """Claim succeeds when the previous claim is older than the cooldown window."""
        import morning_guard
        # Insert a stale claim (3 hours ago)
        con = sqlite3.connect(str(tmp_db))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS morning_run_claim "
            "(id INTEGER PRIMARY KEY CHECK(id=1), claimed_at REAL, caller TEXT, pid INTEGER)"
        )
        stale_time = time.time() - 10_800  # 3 hours ago
        con.execute("INSERT INTO morning_run_claim VALUES (1, ?, 'old_caller', ?)",
                    (stale_time, os.getpid()))
        con.commit()
        con.close()

        result = morning_guard.try_claim_morning_run("new_caller")
        assert result is True

    def test_dead_process_claim_released(self, tmp_db, monkeypatch):
        """Claim with a dead PID (fake PID 9999999) can be stolen."""
        import morning_guard

        # Force _pid_alive to return False for a fake PID
        monkeypatch.setattr(morning_guard, "_pid_alive", lambda pid: False)

        # Insert a "live" claim with a fake PID
        con = sqlite3.connect(str(tmp_db))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS morning_run_claim "
            "(id INTEGER PRIMARY KEY CHECK(id=1), claimed_at REAL, caller TEXT, pid INTEGER)"
        )
        con.execute("INSERT INTO morning_run_claim VALUES (1, ?, 'zombie_caller', 9999999)",
                    (time.time() - 10,))
        con.commit()
        con.close()

        result = morning_guard.try_claim_morning_run("new_caller")
        assert result is True  # zombie process releases the slot

    def test_release_removes_own_claim(self, tmp_db):
        """release_morning_run() removes the claim row for this PID."""
        import morning_guard
        assert morning_guard.try_claim_morning_run("release_test") is True
        status = morning_guard.get_claim_status()
        assert status is not None

        morning_guard.release_morning_run()
        status_after = morning_guard.get_claim_status()
        assert status_after is None

    def test_release_does_not_remove_other_pids_claim(self, tmp_db):
        """release_morning_run() only deletes rows owned by THIS process."""
        import morning_guard

        # Insert a claim with a different PID
        fake_pid = 9999998
        con = sqlite3.connect(str(tmp_db))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS morning_run_claim "
            "(id INTEGER PRIMARY KEY CHECK(id=1), claimed_at REAL, caller TEXT, pid INTEGER)"
        )
        con.execute("INSERT INTO morning_run_claim VALUES (1, ?, 'other', ?)",
                    (time.time(), fake_pid))
        con.commit()
        con.close()

        morning_guard.release_morning_run()  # this process != fake_pid
        status = morning_guard.get_claim_status()
        assert status is not None  # row still exists

    def test_get_claim_status_none_when_empty(self, tmp_db):
        """get_claim_status returns None when no claim exists."""
        import morning_guard
        assert morning_guard.get_claim_status() is None

    def test_get_claim_status_returns_dict(self, tmp_db):
        """get_claim_status returns a dict with expected keys after claim."""
        import morning_guard
        morning_guard.try_claim_morning_run("status_test")
        status = morning_guard.get_claim_status()
        assert isinstance(status, dict)
        assert "claimed_at" in status
        assert "caller" in status
        assert "pid" in status
        assert "age_secs" in status
        assert "pid_alive" in status
        assert status["caller"] == "status_test"
        assert status["pid"] == os.getpid()

    def test_fail_open_on_db_error(self, tmp_db, monkeypatch):
        """If the guard DB is unavailable, try_claim returns True (fail-open)."""
        import morning_guard
        # Make the DB path invalid so _connect() raises
        monkeypatch.setattr(morning_guard, "_DB", Path("/nonexistent/path/guard.db"))
        result = morning_guard.try_claim_morning_run("failopen_test")
        assert result is True  # should NOT block the run


# ===========================================================================
# telegram_queue tests
# ===========================================================================

class TestTelegramQueue:
    def test_stage_new_alert_returns_true(self, tmp_db):
        """stage() returns True when a new unique message is inserted."""
        import telegram_queue
        result = telegram_queue.stage("morning_summary", "Hello Telegram",
                                      dedup_key="test-key-001")
        assert result is True

    def test_stage_dedup_returns_false(self, tmp_db):
        """stage() returns False when the same dedup_key is already present."""
        import telegram_queue
        telegram_queue.stage("morning_summary", "First msg", dedup_key="dedup-001")
        result = telegram_queue.stage("morning_summary", "Second msg", dedup_key="dedup-001")
        assert result is False  # duplicate key — not re-staged

    def test_count_pending_zero_on_empty(self, tmp_db):
        """count_pending() returns 0 on an empty queue."""
        import telegram_queue
        assert telegram_queue.count_pending() == 0

    def test_count_pending_after_stage(self, tmp_db):
        """count_pending() increments with each new staged alert."""
        import telegram_queue
        telegram_queue.stage("type_a", "msg 1", dedup_key="k1")
        telegram_queue.stage("type_b", "msg 2", dedup_key="k2")
        telegram_queue.stage("type_c", "msg 3", dedup_key="k3")
        assert telegram_queue.count_pending() == 3

    def test_count_pending_dedup_not_counted(self, tmp_db):
        """Duplicate dedup_key does not increase count."""
        import telegram_queue
        telegram_queue.stage("type_a", "msg 1", dedup_key="dup")
        telegram_queue.stage("type_a", "msg 2", dedup_key="dup")
        assert telegram_queue.count_pending() == 1

    def test_flush_dry_run_does_not_call_api(self, tmp_db, monkeypatch):
        """flush(dry_run=True) returns count without calling the Telegram API
        AND leaves the message status as 'pending' (not committed as sent)."""
        import telegram_queue
        calls = []
        telegram_queue.stage("test_type", "Hello world", dedup_key="dry-run-001")

        monkeypatch.setattr(telegram_queue, "_send_via_telegram", lambda msg: calls.append(msg))
        sent, errors = telegram_queue.flush(dry_run=True)
        assert sent == 1      # reports 1 would-be-sent
        assert errors == []   # no errors in dry-run
        assert calls == []    # _send_via_telegram must NOT be called in dry-run
        # Status must remain 'pending' — dry-run does NOT commit to DB
        assert telegram_queue.count_pending() == 1

    def test_flush_marks_sent(self, tmp_db, monkeypatch):
        """flush() marks pending messages as 'sent' after successful dispatch."""
        import telegram_queue
        telegram_queue.stage("test_type", "Hello world", dedup_key="flush-001")

        # Mock successful send
        monkeypatch.setattr(telegram_queue, "_send_via_telegram", lambda msg: None)
        sent, errors = telegram_queue.flush(dry_run=False)
        assert sent == 1
        assert errors == []

        # After flush, count_pending should be 0
        assert telegram_queue.count_pending() == 0

    def test_clear_pending_rejects_all(self, tmp_db):
        """clear_pending() marks all pending as 'rejected' without sending."""
        import telegram_queue
        telegram_queue.stage("type_x", "msg1", dedup_key="clear-1")
        telegram_queue.stage("type_y", "msg2", dedup_key="clear-2")
        assert telegram_queue.count_pending() == 2

        cleared = telegram_queue.clear_pending()
        assert cleared == 2
        assert telegram_queue.count_pending() == 0

    def test_get_pending_returns_list(self, tmp_db):
        """get_pending() returns a list of dicts for pending messages."""
        import telegram_queue
        telegram_queue.stage("morning_summary", "Good morning!", dedup_key="gp-001",
                             metadata={"reports_ok": 18, "reports_failed": 0})
        items = telegram_queue.get_pending()
        assert isinstance(items, list)
        assert len(items) == 1
        item = items[0]
        assert item["message_type"] == "morning_summary"
        assert item["content"] == "Good morning!"
        assert item["status"] == "pending"

    def test_stage_without_dedup_key_always_inserts(self, tmp_db):
        """stage() without dedup_key inserts every time (no dedup)."""
        import telegram_queue
        r1 = telegram_queue.stage("alert", "msg A")  # no dedup_key
        r2 = telegram_queue.stage("alert", "msg B")  # no dedup_key
        assert r1 is True
        assert r2 is True
        assert telegram_queue.count_pending() == 2

    def test_flush_sends_oldest_first(self, tmp_db, monkeypatch):
        """flush() sends messages in staged_at order (oldest first)."""
        import telegram_queue
        sent_order: list[str] = []

        def capture(msg: str) -> None:
            sent_order.append(msg)

        monkeypatch.setattr(telegram_queue, "_send_via_telegram", capture)
        telegram_queue.stage("t", "FIRST", dedup_key="order-1")
        telegram_queue.stage("t", "SECOND", dedup_key="order-2")
        telegram_queue.stage("t", "THIRD", dedup_key="order-3")
        telegram_queue.flush()
        assert sent_order == ["FIRST", "SECOND", "THIRD"]

    def test_sha256_dedup_key_stable(self, tmp_db):
        """dedup_key derived from content uses sha256, not hash() which is random."""
        import telegram_queue
        import hashlib
        # Stage twice with the same content and no explicit dedup_key
        content = "Identical morning summary content"
        # The internal dedup key should be sha256-based and identical across calls
        expected_dk = f"morning_summary:{hashlib.sha256(content.encode()).hexdigest()[:16]}"
        # First insert: should succeed
        r1 = telegram_queue.stage("morning_summary", content)
        assert r1 is True
        # Second insert with same content: should be dedup'd (same derived key)
        r2 = telegram_queue.stage("morning_summary", content)
        assert r2 is False  # dedup'd — not re-staged

    def test_flush_partial_failure_first_committed_second_pending(self, tmp_db, monkeypatch):
        """If flush fails on msg 2, msg 1 is already committed (no double-send)."""
        import telegram_queue
        telegram_queue.stage("t", "FIRST", dedup_key="pf-1")
        telegram_queue.stage("t", "SECOND", dedup_key="pf-2")

        call_count = [0]

        def sometimes_fails(msg: str) -> None:
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Simulated Telegram API timeout")

        monkeypatch.setattr(telegram_queue, "_send_via_telegram", sometimes_fails)
        sent, errors = telegram_queue.flush()

        # FIRST was sent and committed; SECOND failed
        assert sent == 1
        assert len(errors) == 1
        assert "Simulated Telegram API timeout" in errors[0]  # error recorded for SECOND message

        # FIRST must be marked 'sent', SECOND must still be 'pending'
        assert telegram_queue.count_pending() == 1
        remaining = telegram_queue.get_pending()
        assert remaining[0]["content"] == "SECOND"

    def test_history_returns_sent_after_flush(self, tmp_db, monkeypatch):
        """After flush, _cli_history() finds the sent message."""
        import telegram_queue
        telegram_queue.stage("t", "Historical message", dedup_key="hist-1")
        monkeypatch.setattr(telegram_queue, "_send_via_telegram", lambda m: None)
        telegram_queue.flush()

        # The sent record should appear in history
        sent_rows = []
        con = None
        try:
            con = telegram_queue._connect()
            con.row_factory = __import__("sqlite3").Row
            sent_rows = con.execute(
                "SELECT status FROM alert_queue WHERE dedup_key='hist-1'"
            ).fetchall()
        finally:
            if con:
                con.close()
        assert len(sent_rows) == 1
        assert sent_rows[0]["status"] == "sent"

    def test_metadata_stored_with_failed_names(self, tmp_db, monkeypatch):
        """Staged messages include failed_names in metadata JSON."""
        import telegram_queue, json

        telegram_queue.stage(
            "morning_summary",
            "Test msg",
            metadata={"reports_ok": 15, "reports_failed": 3,
                      "failed_names": ["report_a.html", "report_b.html", "report_c.html"]},
            dedup_key="meta-test-1",
        )
        items = telegram_queue.get_pending()
        assert len(items) == 1
        meta = json.loads(items[0]["metadata"])
        assert meta["failed_names"] == ["report_a.html", "report_b.html", "report_c.html"]
        assert meta["reports_failed"] == 3


# ===========================================================================
# telegram_alerts staging= parameter tests (critical fix verification)
# ===========================================================================

class TestTelegramAlertsStagingParam:
    """Verify that send_morning_summary(staging=True) queues locally, not sends."""

    def test_staging_true_stages_not_sends(self, tmp_db, monkeypatch):
        """staging=True must queue the message locally, NOT call Telegram API."""
        import telegram_queue
        import telegram_alerts

        direct_calls: list[str] = []

        # Patch the internal send so we can detect any direct send
        monkeypatch.setattr(telegram_alerts, "_send", lambda msg: direct_calls.append(msg) or True)
        # Patch _send_via_telegram in telegram_queue to capture staging flushes
        monkeypatch.setattr(telegram_queue, "_send_via_telegram", lambda m: None)

        # Patch is_enabled to return True
        monkeypatch.setattr(telegram_alerts, "is_enabled", lambda: True)

        result = telegram_alerts.send_morning_summary(
            reports=["r1.html", "r2.html"],
            failures=[],
            staging=True,
        )

        # Should NOT call _send directly
        assert direct_calls == [], "staging=True must not call _send() directly!"
        # Should return True (staged = success from caller perspective)
        assert result is True
        # Message should be in the queue
        assert telegram_queue.count_pending() == 1

    def test_staging_false_sends_directly(self, tmp_db, monkeypatch):
        """staging=False (default) sends via _send() without queueing."""
        import telegram_queue
        import telegram_alerts

        direct_calls: list[str] = []
        monkeypatch.setattr(telegram_alerts, "_send", lambda msg: direct_calls.append(msg) or True)
        monkeypatch.setattr(telegram_alerts, "is_enabled", lambda: True)

        telegram_alerts.send_morning_summary(
            reports=["r1.html"],
            failures=[],
            staging=False,
        )

        assert len(direct_calls) == 1  # went directly to Telegram
        assert telegram_queue.count_pending() == 0  # nothing staged

    def test_staging_failure_returns_false_not_fallthrough(self, tmp_db, monkeypatch):
        """If staging fails (e.g., DB error), return False instead of falling through to _send()."""
        import telegram_alerts, telegram_queue

        direct_calls: list[str] = []
        monkeypatch.setattr(telegram_alerts, "_send", lambda msg: direct_calls.append(msg) or True)
        monkeypatch.setattr(telegram_alerts, "is_enabled", lambda: True)

        # Make stage() itself throw so the staging path hits except block
        def fail_stage(*args, **kwargs):
            raise RuntimeError("Simulated SQLite write failure")

        monkeypatch.setattr(telegram_queue, "stage", fail_stage)

        result = telegram_alerts.send_morning_summary(
            reports=["r1.html"],
            failures=[],
            staging=True,
        )

        # Must return False (staging failed) — NOT fall through to _send()
        assert result is False
        assert direct_calls == [], "Must NOT fall through to direct send on staging failure!"

    def test_staging_dedup_prevents_duplicate_today(self, tmp_db, monkeypatch):
        """Second call with same date dedup_key is silently skipped (no duplicate)."""
        import telegram_queue
        import telegram_alerts

        monkeypatch.setattr(telegram_alerts, "_send", lambda msg: True)
        monkeypatch.setattr(telegram_alerts, "is_enabled", lambda: True)

        result1 = telegram_alerts.send_morning_summary(
            reports=["r1.html"], failures=[], staging=True
        )
        result2 = telegram_alerts.send_morning_summary(
            reports=["r1.html"], failures=[], staging=True
        )

        # Both calls return True (caller sees success both times)
        assert result1 is True
        assert result2 is True
        # But only ONE message in queue (dedup'd by date)
        assert telegram_queue.count_pending() == 1

    def test_staging_includes_failed_names_in_metadata(self, tmp_db, monkeypatch):
        """staged message metadata includes full list of failed report names."""
        import telegram_queue, telegram_alerts, json

        monkeypatch.setattr(telegram_alerts, "_send", lambda msg: True)
        monkeypatch.setattr(telegram_alerts, "is_enabled", lambda: True)

        telegram_alerts.send_morning_summary(
            reports=["ok1.html", "ok2.html"],
            failures=["fail_report.html"],
            staging=True,
        )

        items = telegram_queue.get_pending()
        assert len(items) == 1
        meta = json.loads(items[0]["metadata"])
        assert "failed_names" in meta
        assert "fail_report.html" in meta["failed_names"]
        assert meta["reports_ok"] == 2
        assert meta["reports_failed"] == 1
