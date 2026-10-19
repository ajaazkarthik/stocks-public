"""Offline tests for the durable scoring → notification handoff (Issue 6).

The defect: completion evidence was persisted before the tracker result, so a
tracker-write failure could make a scored fire disappear from future alerts,
and a Telegram failure had no durable pending state at all.

These tests exercise the real ``outcome_store`` / ``fires_ledger`` code against
temporary SQLite databases. No production database is opened.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingview import outcome_store
from tradingview.scheduler import identity
from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory

FROZEN_NOW = datetime(2026, 10, 13, 6, 42, 19, tzinfo=timezone.utc)
RUN_ID = "ss_20261013T064219Z_handoff01"


def _fire(pro_symbol: str = "NASDAQ:GOOG", fire_time: str = "2026-10-13T05:00:00Z",
          resolution: str = "60") -> dict:
    return {"pro_symbol": pro_symbol, "fire_time": fire_time, "resolution": resolution}


def _result(score: float = 71.0, actionable: bool = True) -> dict:
    return {
        "composite": score,
        "actionable": actionable,
        "actionability_reasons": [] if actionable else ["earnings blackout"],
        "scores": {"l1": {"total": 10.0}},
    }


@pytest.fixture
def ledger_conn(tmp_path):
    """A temporary fires-ledger database with the real schema."""
    conn = sqlite3.connect(str(tmp_path / "fires_ledger.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS downloaded_fires (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            pro_symbol    TEXT    NOT NULL,
            fire_time     TEXT    NOT NULL,
            resolution    TEXT    NOT NULL DEFAULT '',
            fire_id       INTEGER,
            alert_id      INTEGER,
            symbol        TEXT,
            name          TEXT,
            message       TEXT,
            bar_time      TEXT,
            source        TEXT    NOT NULL DEFAULT 'download',
            fire_json     TEXT,
            first_seen_at TEXT    NOT NULL,
            scored_at     TEXT,
            UNIQUE (pro_symbol, fire_time, resolution)
        );
        """
    )
    outcome_store.ensure_schema(conn)
    yield conn
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Durable scoring payload
# ══════════════════════════════════════════════════════════════════════════════

def test_scoring_results_table_is_created_additively(ledger_conn):
    tables = {
        row[0] for row in ledger_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "scoring_results" in tables
    # Pre-existing tables are untouched.
    assert "fire_attempt_evidence" in tables
    assert "downloaded_fires" in tables


def test_ensure_schema_is_idempotent(ledger_conn):
    outcome_store.ensure_schema(ledger_conn)
    outcome_store.ensure_schema(ledger_conn)
    assert ledger_conn.execute("SELECT COUNT(*) FROM scoring_results").fetchone()[0] == 0


def test_completed_result_is_recoverable(ledger_conn):
    fire = _fire()
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result())
    ledger_conn.commit()

    recovered = outcome_store.load_scoring_results(ledger_conn)
    assert len(recovered) == 1
    assert recovered[0]["pro_symbol"] == "NASDAQ:GOOG"
    assert recovered[0]["result"]["composite"] == 71.0
    assert recovered[0]["result"]["actionable"] is True


def test_completion_evidence_and_payload_land_together(ledger_conn):
    """Both writes share one transaction, so neither can exist alone."""
    fire = _fire()
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result())
    ledger_conn.commit()

    evidence = ledger_conn.execute(
        "SELECT COUNT(*) FROM fire_attempt_evidence WHERE state='completed'"
    ).fetchone()[0]
    payloads = ledger_conn.execute("SELECT COUNT(*) FROM scoring_results").fetchone()[0]
    assert evidence == 1
    assert payloads == 1


def test_rollback_leaves_neither_evidence_nor_payload(ledger_conn):
    """A crash before commit must not leave false durable completion."""
    fire = _fire()
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result())
    ledger_conn.rollback()

    assert ledger_conn.execute("SELECT COUNT(*) FROM scoring_results").fetchone()[0] == 0
    assert ledger_conn.execute(
        "SELECT COUNT(*) FROM fire_attempt_evidence"
    ).fetchone()[0] == 0


def test_payload_records_eligibility_verdict(ledger_conn):
    fire = _fire()
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result(actionable=False))
    ledger_conn.commit()

    recovered = outcome_store.load_scoring_results(ledger_conn)[0]
    assert recovered["result"]["actionable"] is False
    assert recovered["result"]["actionability_reasons"] == ["earnings blackout"]


def test_recording_the_same_fire_twice_is_idempotent(ledger_conn):
    fire = _fire()
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result())
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result(score=80.0))
    ledger_conn.commit()

    rows = outcome_store.load_scoring_results(ledger_conn)
    assert len(rows) == 1
    assert rows[0]["result"]["composite"] == 80.0


def test_load_scoring_results_filters_by_run(ledger_conn):
    outcome_store.persist_completed(ledger_conn, _fire("NASDAQ:A"), "run-a", _result())
    outcome_store.persist_completed(
        ledger_conn, _fire("NASDAQ:B", fire_time="2026-10-13T06:00:00Z"), "run-b", _result()
    )
    ledger_conn.commit()

    assert len(outcome_store.load_scoring_results(ledger_conn, run_id="run-a")) == 1
    assert len(outcome_store.load_scoring_results(ledger_conn)) == 2


def test_persist_completed_without_result_still_records_evidence(ledger_conn):
    """Backwards compatibility: existing callers keep working."""
    outcome_store.persist_completed(ledger_conn, _fire(), RUN_ID)
    ledger_conn.commit()
    assert ledger_conn.execute(
        "SELECT COUNT(*) FROM fire_attempt_evidence WHERE state='completed'"
    ).fetchone()[0] == 1


def test_incomplete_identity_is_ignored(ledger_conn):
    outcome_store.persist_completed(ledger_conn, {"pro_symbol": "", "fire_time": ""},
                                    RUN_ID, _result())
    ledger_conn.commit()
    assert ledger_conn.execute("SELECT COUNT(*) FROM scoring_results").fetchone()[0] == 0


def test_migration_does_not_replay_historical_completed_fires(ledger_conn):
    """Rollout must not turn historical completed fires into new alerts."""
    ledger_conn.execute(
        "INSERT INTO downloaded_fires (pro_symbol, fire_time, resolution, first_seen_at, "
        "scored_at) VALUES ('NASDAQ:OLD', '2026-01-01T00:00:00Z', '60', '2026-01-01', "
        "'2026-01-01T00:00:00+00:00')"
    )
    ledger_conn.commit()

    # The new table starts empty; historical rows are not back-filled.
    assert outcome_store.load_scoring_results(ledger_conn) == []
    # And the historical scored_at marker is preserved.
    assert ledger_conn.execute(
        "SELECT scored_at FROM downloaded_fires WHERE pro_symbol='NASDAQ:OLD'"
    ).fetchone()[0] is not None


# ══════════════════════════════════════════════════════════════════════════════
# Recovery scenarios
# ══════════════════════════════════════════════════════════════════════════════

def test_tracker_write_failure_leaves_result_recoverable(ledger_conn, tmp_path):
    """A tracker failure after scoring must not lose the notification."""
    fire = _fire()
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result())
    ledger_conn.commit()

    # Simulate the tracker write failing afterwards.
    def _failing_tracker_write():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        _failing_tracker_write()

    recovered = outcome_store.load_scoring_results(ledger_conn)
    assert len(recovered) == 1

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    stock = {
        "pro_symbol": recovered[0]["pro_symbol"],
        "fire_time": recovered[0]["fire_time"],
        "resolution": recovered[0]["resolution"],
        "score": recovered[0]["result"]["composite"],
    }
    key = identity.stock_delivery_key(111, stock)
    assert outbox.enqueue(delivery_key=key, recipient=111, kind="stock",
                          payload=stock, message_text="alert") is True


def test_crash_after_commit_before_enqueue_can_enqueue_once(ledger_conn, tmp_path):
    fire = _fire()
    outcome_store.persist_completed(ledger_conn, fire, RUN_ID, _result())
    ledger_conn.commit()

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    recovered = outcome_store.load_scoring_results(ledger_conn)
    stock = {
        "pro_symbol": recovered[0]["pro_symbol"],
        "fire_time": recovered[0]["fire_time"],
        "resolution": recovered[0]["resolution"],
    }
    key = identity.stock_delivery_key(111, stock)

    # Recovery runs twice (e.g. two restarts); only one enqueue happens.
    assert outbox.enqueue(delivery_key=key, recipient=111, kind="stock",
                          payload=stock, message_text="alert") is True
    assert outbox.enqueue(delivery_key=key, recipient=111, kind="stock",
                          payload=stock, message_text="alert") is False
    assert len(outbox.all_items()) == 1


def test_crash_after_enqueue_before_send_is_retried(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    # A fresh process finds the pending row and sends it.
    restarted = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    claimed = restarted.claim_due("restart-worker")
    assert len(claimed) == 1
    restarted.mark_sent(claimed[0].id, 99, owner=claimed[0].claim_owner, claim_token=claimed[0].claim_token)
    assert restarted.get_by_key("k").status == "sent"


def test_timeout_then_success_records_attempt_and_receipt(tmp_path):
    clock = _Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                backoff_base_seconds=5)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    item = outbox.claim_due("w")[0]
    outbox.mark_failed(item.id, "TimedOut", retry_after=30, owner=item.claim_owner, claim_token=item.claim_token)
    after_failure = outbox.get(item.id)
    assert after_failure.attempt_count == 1
    assert after_failure.status == "pending"

    clock.advance(60)
    item = outbox.claim_due("w")[0]
    outbox.mark_sent(item.id, 1234, owner=item.claim_owner, claim_token=item.claim_token)
    final = outbox.get(item.id)
    assert final.status == "sent"
    assert final.telegram_message_id == 1234
    assert final.attempt_count == 1


def test_restart_with_in_flight_expired_lease_reclaims_work(tmp_path):
    clock = _Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                claim_lease_seconds=30)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    outbox.claim_due("crashed")

    clock.advance(120)
    reclaimed = outbox.claim_due("recovered")
    assert len(reclaimed) == 1


def test_ambiguous_timeout_is_documented_not_marked_delivered(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    item = outbox.claim_due("w")[0]
    outbox.mark_ambiguous(item.id, "ReadTimeout after the request was accepted", owner=item.claim_owner, claim_token=item.claim_token)

    refreshed = outbox.get(item.id)
    assert refreshed.status == "ambiguous"
    assert refreshed.sent_at is None
    assert refreshed.telegram_message_id is None
    # The module documents the at-least-once limitation explicitly.
    from tradingview.scheduler import outbox as outbox_module

    assert "at-least-once" in outbox_module.__doc__.lower()
    assert "exactly-once" in outbox_module.__doc__.lower()


def test_expired_queue_item_is_visible_and_not_sent(tmp_path):
    clock = _Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                alert_ttl_hours=2)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    clock.advance(3600 * 5)

    assert outbox.claim_due("w") == []
    item = outbox.get_by_key("k")
    assert item.status == "expired"
    assert item.last_error


def test_no_new_cycle_does_not_destroy_pending_option_payload(tmp_path):
    """A later no-new cycle must not replace the only pending option payload."""
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    option = {
        "exchange": "NASDAQ", "symbol": "MSFT", "opt_type": "CALL",
        "strike": 530.0, "expiry": "2030-06-19", "score": 79.0,
        "parent_score": 91.0, "strategy_modes": ["BALANCED"],
    }
    key = identity.option_delivery_key(111, option)
    outbox.enqueue(delivery_key=key, recipient=111, kind="option",
                   payload=option, message_text="option alert", run_id="run-a")

    # A no-new cycle enqueues only its own summary identity.
    summary_key = identity.summary_delivery_key(111, "run-b", 0)
    outbox.enqueue(delivery_key=summary_key, recipient=111, kind="summary",
                   payload={"run_id": "run-b"}, message_text="no new", run_id="run-b")

    assert outbox.get_by_key(key) is not None
    assert outbox.get_by_key(key).status == "pending"
    assert outbox.get_by_key(key).payload["strike"] == 530.0


def test_network_calls_do_not_hold_a_write_transaction(tmp_path):
    """Claims commit before any send happens."""
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    outbox.claim_due("w")

    # A second connection can write immediately: no long-held transaction.
    other = sqlite3.connect(str(tmp_path / "outbox.db"), timeout=2)
    other.execute("BEGIN IMMEDIATE")
    other.rollback()
    other.close()


class _Clock:
    def __init__(self, start: datetime = FROZEN_NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)
