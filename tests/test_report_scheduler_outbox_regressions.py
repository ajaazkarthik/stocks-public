"""Offline regression coverage for event reservations and claim fencing."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

import pytest

from tradingview.scheduler import outbox as outbox_module
from tradingview.scheduler.outbox import (
    NotificationOutbox, SCHEMA, STATUS_FAILED, STATUS_SENT, sqlite_factory,
)


@pytest.fixture
def queue(tmp_path):
    now = [datetime(2026, 10, 14, tzinfo=timezone.utc)]
    path = tmp_path / 'outbox.db'
    outbox = NotificationOutbox(sqlite_factory(path), clock=lambda: now[0],
                                claim_lease_seconds=10, backoff_base_seconds=1)
    return outbox, now, path


def event(key='event', recipient=1):
    return dict(delivery_key=key, recipient=recipient, kind='stock', run_id='run')


def part(key, events, recipient=1):
    return dict(delivery_key=key, recipient=recipient, kind='summary', run_id='run',
                payload={'events': events}, message_text=','.join(e['delivery_key'] for e in events))


def reserve(outbox, key='part', events=None, **kwargs):
    events = [event()] if events is None else events
    return outbox.enqueue_event_parts(recipient=1, events=events,
                                     build_items=lambda remaining: [part(key, remaining)], **kwargs)


def fence(item):
    return dict(owner=item.claim_owner, claim_token=item.claim_token)


def test_sqlite_factory_retries_busy_during_wal_initialization(monkeypatch, tmp_path):
    class InjectedBusy(sqlite3.OperationalError):
        sqlite_errorcode = sqlite3.SQLITE_BUSY

    class FakeConnection:
        def __init__(self, fail_count):
            self.fail_count = fail_count
            self.closed = False

        def execute(self, sql):
            if sql == "PRAGMA journal_mode=WAL" and self.fail_count:
                self.fail_count -= 1
                raise InjectedBusy("database is locked")
            return None

        def close(self):
            self.closed = True

    connections = []

    def fake_connect(*_args, **_kwargs):
        conn = FakeConnection(1 if len(connections) < 2 else 0)
        connections.append(conn)
        return conn

    sleeps = []
    monkeypatch.setattr(outbox_module.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(outbox_module.time, "sleep", sleeps.append)

    result = sqlite_factory(tmp_path / "retry.db")()

    assert result is connections[2]
    assert [conn.closed for conn in connections] == [True, True, False]
    assert sleeps == [0.05, 0.05]
    result.close()


def test_sqlite_factory_preserves_nonbusy_setup_error_and_closes(monkeypatch, tmp_path):
    class FakeConnection:
        closed = False

        def execute(self, _sql):
            raise sqlite3.OperationalError("no such pragma")

        def close(self):
            self.closed = True

    conn = FakeConnection()
    monkeypatch.setattr(outbox_module.sqlite3, "connect", lambda *_a, **_k: conn)

    with pytest.raises(sqlite3.OperationalError, match="no such pragma"):
        sqlite_factory(tmp_path / "nonbusy.db")()

    assert conn.closed


def test_sqlite_factory_deadline_exhaustion_closes_each_attempt(monkeypatch, tmp_path):
    class InjectedBusy(sqlite3.OperationalError):
        sqlite_errorcode = sqlite3.SQLITE_BUSY

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execute(self, sql):
            if sql == "PRAGMA journal_mode=WAL":
                raise InjectedBusy("database is locked")

        def close(self):
            self.closed = True

    connections = []

    def fake_connect(*_args, **_kwargs):
        conn = FakeConnection()
        connections.append(conn)
        return conn

    clock = iter([100.0, 101.0, 131.0])
    monkeypatch.setattr(outbox_module.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(outbox_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(outbox_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(InjectedBusy, match="database is locked"):
        sqlite_factory(tmp_path / "deadline.db")()

    assert len(connections) == 2
    assert all(conn.closed for conn in connections)


def test_repeated_concurrent_fresh_database_wal_initialization(tmp_path):
    worker_count = 8
    for iteration in range(8):
        path = tmp_path / f"fresh-{iteration}.db"
        barrier = threading.Barrier(worker_count)

        def first_open():
            barrier.wait()
            conn = sqlite_factory(path)()
            try:
                return conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=worker_count) as workers:
            futures = [workers.submit(first_open) for _ in range(worker_count)]
            assert [future.result() for future in futures] == ["wal"] * worker_count


@pytest.mark.parametrize('mutation', ['failed', 'ambiguous', 'release', 'sent'])
@pytest.mark.parametrize('newer_sent', [False, True])
def test_stale_worker_cannot_mutate_newer_claim_or_ack(queue, mutation, newer_sent):
    outbox, now, _ = queue
    assert reserve(outbox) == 1
    first = outbox.claim_due('worker')[0]
    now[0] += timedelta(seconds=11)
    second = outbox.claim_due('worker')[0]  # same owner, distinct claim token
    assert first.claim_token != second.claim_token
    if newer_sent:
        assert outbox.mark_sent(second.id, 222, **fence(second))
    before = outbox.get(second.id)
    if mutation == 'failed':
        assert outbox.mark_failed(first.id, 'late failure', **fence(first)) is None
    elif mutation == 'ambiguous':
        assert outbox.mark_ambiguous(first.id, 'late timeout', **fence(first)) is None
    elif mutation == 'release':
        assert not outbox.release_claim(first.id, **fence(first))
    else:
        assert not outbox.mark_sent(first.id, 111, **fence(first))
    assert outbox.get(second.id) == before
    assert outbox.event_delivered('event') is newer_sent


def test_expired_lease_cannot_complete_even_before_reclaim(queue):
    outbox, now, _ = queue
    reserve(outbox)
    item = outbox.claim_due('A')[0]
    now[0] += timedelta(seconds=11)
    assert not outbox.mark_sent(item.id, 1, **fence(item))
    assert outbox.mark_failed(item.id, 'late', **fence(item)) is None
    assert not outbox.event_delivered('event')


def test_missing_owner_or_token_is_rejected(queue):
    outbox, _, _ = queue
    reserve(outbox)
    item = outbox.claim_due('A')[0]
    assert not outbox.mark_sent(item.id, 1, owner='A')
    assert outbox.mark_failed(item.id, 'unsafe') is None
    assert outbox.mark_ambiguous(item.id, 'unsafe') is None
    assert not outbox.release_claim(item.id)
    assert outbox.mark_sent(item.id, 1, **fence(item))


def test_multipart_event_ack_requires_every_carrier(queue):
    outbox, _, _ = queue
    a, b = event('a'), event('b')
    assert outbox.enqueue_event_parts(recipient=1, events=[a, b],
        build_items=lambda _: [part('one', [a]), part('two', [a, b])]) == 2
    one, two = outbox.claim_due('A')
    assert outbox.mark_sent(one.id, 1, **fence(one))
    assert not outbox.event_delivered('a')
    assert not outbox.event_delivered('b')
    assert outbox.mark_sent(two.id, 2, **fence(two))
    assert outbox.event_delivered('a')
    assert outbox.event_delivered('b')


def test_ack_transaction_rolls_back_row_if_ledger_write_fails(queue):
    outbox, _, path = queue
    reserve(outbox)
    item = outbox.claim_due('A')[0]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TRIGGER break_ack BEFORE INSERT ON delivered_events "
                     "BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match='ledger unavailable'):
        outbox.mark_sent(item.id, 1, **fence(item))
    assert outbox.get(item.id).status == 'claimed'
    assert not outbox.event_delivered('event')


def test_concurrent_normal_and_recovery_reserve_only_one_message(queue):
    outbox, _, _ = queue
    barrier = threading.Barrier(2)
    def producer(name):
        barrier.wait()
        return reserve(outbox, name)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(producer, name) for name in ['ordinary', 'recover']]
        assert sorted(f.result() for f in futures) == [0, 1]
    assert len(outbox.all_items()) == 1


def test_reservation_filters_body_and_distinct_recipient(queue):
    outbox, _, _ = queue
    assert reserve(outbox, events=[event('old')]) == 1
    assert reserve(outbox, 'new', [event('old'), event('new')]) == 1
    assert outbox.get_by_key('new').message_text == 'new'
    other = event('other-recipient', 2)
    assert outbox.enqueue_event_parts(recipient=2, events=[other],
                                     build_items=lambda remaining: [part('other', remaining, 2)]) == 1


def test_partial_batch_key_conflict_rolls_back_rows_and_reservations(queue):
    outbox, _, _ = queue
    assert outbox.enqueue(**part('collision', []))
    a, b = event('a'), event('b')
    assert outbox.enqueue_event_parts(recipient=1, events=[a, b],
        build_items=lambda _: [part('inserted-first', [a]), part('collision', [b])]) == 0
    assert outbox.get_by_key('inserted-first') is None
    assert reserve(outbox, 'retry', [a, b]) == 1


def test_callback_failure_rolls_back_reservation(queue):
    outbox, _, _ = queue
    def broken(_):
        raise RuntimeError('render failed')
    with pytest.raises(RuntimeError, match='render failed'):
        outbox.enqueue_event_parts(recipient=1, events=[event()], build_items=broken)
    assert reserve(outbox) == 1


def test_expired_or_terminal_work_cannot_gain_new_retry_budget(queue):
    outbox, now, _ = queue
    deadline = now[0] + timedelta(hours=1)
    assert reserve(outbox, expires_at=deadline) == 1
    item = outbox.claim_due('A')[0]
    assert outbox.mark_failed(item.id, 'forbidden', terminal=True, **fence(item)) == STATUS_FAILED
    assert reserve(outbox, 'recover', expires_at=deadline + timedelta(days=1)) == 0
    now[0] = deadline + timedelta(seconds=1)
    assert reserve(outbox, 'later', expires_at=now[0] + timedelta(hours=12)) == 0
    assert outbox.get(item.id).expires_at == deadline.isoformat()


def test_expired_ordinary_row_cannot_be_recovered_with_fresh_ttl(queue):
    outbox, now, _ = queue
    deadline = now[0] + timedelta(seconds=5)
    reserve(outbox, expires_at=deadline)
    now[0] += timedelta(seconds=6)
    assert outbox.expire_overdue() == 1
    assert reserve(outbox, 'recover') == 0
    assert outbox.all_items()[0].status == 'expired'


def test_expired_new_work_is_rejected(queue):
    outbox, now, _ = queue
    assert reserve(outbox, expires_at=now[0] - timedelta(seconds=1)) == 0
    assert outbox.all_items() == []


def test_legacy_schema_pending_parts_backfill_conservative_membership(tmp_path):
    path = tmp_path / 'legacy.db'
    old_schema = SCHEMA.split('CREATE TABLE IF NOT EXISTS notification_events')[0]
    old_schema = old_schema.replace('    claim_token        TEXT,\n', '')
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    deadline = now + timedelta(hours=1)
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        for key in ['one', 'two']:
            conn.execute('INSERT INTO notification_outbox '
                         '(delivery_key, recipient, kind, run_id, payload_json, message_text, '
                         'next_attempt_at, created_at, updated_at, expires_at) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (key, '1', 'summary', 'old', json.dumps({'events': [event()]}),
                          key, now.isoformat(), now.isoformat(), now.isoformat(), deadline.isoformat()))
    outbox = NotificationOutbox(sqlite_factory(path), clock=lambda: now)
    assert reserve(outbox, 'recovered') == 0
    one, two = outbox.claim_due('A')
    assert outbox.mark_sent(one.id, 1, **fence(one))
    assert not outbox.event_delivered('event')
    assert outbox.mark_failed(two.id, 'forbidden', terminal=True, **fence(two)) == STATUS_FAILED
    assert not outbox.event_delivered('event')
    assert outbox.items_for_run('old', recipient=1, kind='summary')[0].status == STATUS_SENT


def test_renew_checks_owner_token_lease_and_freshness(queue):
    outbox, now, _ = queue
    reserve(outbox, expires_at=now[0] + timedelta(seconds=18))
    item = outbox.claim_due('A')[0]
    assert not outbox.renew_claim(item.id, owner='B', claim_token=item.claim_token)
    now[0] += timedelta(seconds=9)
    assert outbox.renew_claim(item.id, **fence(item))
    assert outbox.get(item.id).claim_expires_at == outbox.get(item.id).expires_at
    now[0] += timedelta(seconds=9)
    assert not outbox.renew_claim(item.id, **fence(item))


def test_renew_cannot_revive_expired_lease_or_overwrite_reclaimer(queue):
    outbox, now, _ = queue
    reserve(outbox)
    first = outbox.claim_due('A')[0]
    now[0] += timedelta(seconds=11)
    assert not outbox.renew_claim(first.id, **fence(first))
    second = outbox.claim_due('B')[0]
    before = outbox.get(second.id)
    assert not outbox.renew_claim(first.id, **fence(first))
    assert outbox.get(second.id) == before


def test_concurrent_first_open_migrates_legacy_schema_once(tmp_path):
    path = tmp_path / 'legacy-race.db'
    legacy_schema = SCHEMA.split('CREATE TABLE IF NOT EXISTS notification_events')[0]
    legacy_schema = legacy_schema.replace('    claim_token        TEXT,\n', '')
    with sqlite3.connect(path) as conn:
        conn.executescript(legacy_schema)
    barrier = threading.Barrier(4)
    def migrate():
        barrier.wait()
        return NotificationOutbox(sqlite_factory(path)).all_items()
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(migrate) for _ in range(4)]
        assert [future.result() for future in futures] == [[], [], [], []]
    with sqlite3.connect(path) as conn:
        assert [row[1] for row in conn.execute('PRAGMA table_info(notification_outbox)')].count('claim_token') == 1
        assert conn.execute('SELECT COUNT(*) FROM notification_migrations').fetchone()[0] == 1


@pytest.mark.parametrize('mutation', ['sent', 'failed', 'ambiguous', 'release', 'renew'])
def test_fence_uses_time_after_connection_wait(queue, mutation):
    outbox, now, _ = queue
    reserve(outbox)
    item = outbox.claim_due('A')[0]
    original_factory = outbox._conn_factory
    def delayed_connection():
        now[0] += timedelta(seconds=11)
        return original_factory()
    outbox._conn_factory = delayed_connection
    if mutation == 'sent':
        accepted = outbox.mark_sent(item.id, 1, **fence(item))
    elif mutation == 'failed':
        accepted = outbox.mark_failed(item.id, 'late', **fence(item))
    elif mutation == 'ambiguous':
        accepted = outbox.mark_ambiguous(item.id, 'late', **fence(item))
    elif mutation == 'release':
        accepted = outbox.release_claim(item.id, **fence(item))
    else:
        accepted = outbox.renew_claim(item.id, **fence(item))
    assert not accepted
    outbox._conn_factory = original_factory
    assert outbox.get(item.id).status == 'claimed'
    assert not outbox.event_delivered('event')


def test_claim_deadline_checked_after_connection_wait(queue):
    outbox, now, _ = queue
    reserve(outbox, expires_at=now[0] + timedelta(seconds=5))
    original_factory = outbox._conn_factory
    def delayed_connection():
        now[0] += timedelta(seconds=6)
        return original_factory()
    outbox._conn_factory = delayed_connection
    assert outbox.claim_due('A') == []
    outbox._conn_factory = original_factory
    assert outbox.all_items()[0].status == 'expired'
