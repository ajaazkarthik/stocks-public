"""Offline durable cycle regressions, with temporary DBs and process locks only."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingview.scheduler import formatting, identity
from tradingview.scheduler.cycle import CycleLock, CycleResult, deliver_cycle, enqueue_cycle
from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory
from tradingview.scheduler.stock_results import StockResult

NOW = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)


def stock(symbol="ONE", *, fire_time=None):
    return dict(pro_symbol=f"NASDAQ:{symbol}", exchange="NASDAQ", symbol=symbol,
                score=80.0, signal="BUY", fire_time=fire_time or NOW.isoformat(), resolution="60")


def result(run_id, rows):
    return CycleResult(run_id=run_id, started_at=NOW.isoformat(), completed_at=NOW.isoformat(),
                       display_timezone="Australia/Sydney",
                       stock_result=StockResult(status="success", stocks=rows),
                       option_result={"status": "empty", "options": []})


@pytest.fixture
def queue(tmp_path):
    clock = [NOW]
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: clock[0])
    return outbox, clock


@pytest.mark.parametrize("error", ["Forbidden: bot was blocked", "NetworkError: temporarily unavailable"])
def test_first_of_multiple_parts_does_not_acknowledge_failed_stock_parts(queue, error):
    outbox, _ = queue
    rows = [stock(f"STOCK{i:03d}") for i in range(300)]
    cycle = result("partial", rows)
    assert enqueue_cycle(outbox, cycle, [111], limit=3500, analysis_time=NOW) > 1
    items = outbox.all_items()
    calls = []

    async def send(**kwargs):
        calls.append(kwargs["text"])
        if len(calls) > 1:
            raise RuntimeError(error)
        return SimpleNamespace(message_id=42)

    receipts = asyncio.run(deliver_cycle(outbox, cycle, [111], limit=3500, send=send))
    assert sum(receipt.delivered for receipt in receipts[111]) == 1
    expected = {event["delivery_key"] for event in items[0].payload["events"]}
    assert set(outbox.delivered_event_keys(111, "stock")) == expected
    assert not outbox.event_delivered(identity.stock_delivery_key(111, rows[-1]))
    assert rows[-1]["symbol"] not in calls[0]
    # Pending and terminal failures preserve their reservation/attempt policy.
    # Explicit intervention is required to retry an exhausted terminal failure.
    assert enqueue_cycle(outbox, replace(cycle, run_id="repeated"), [111], limit=3500) == 0


def test_transient_failed_parts_retry_without_resending_acknowledged_parts(queue):
    outbox, clock = queue
    rows = [stock(f"STOCK{i:03d}") for i in range(300)]
    cycle = result("retry", rows)
    enqueue_cycle(outbox, cycle, [111], limit=3500)
    calls = []

    async def partial(**kwargs):
        calls.append(kwargs["text"])
        if len(calls) > 2:
            raise RuntimeError("NetworkError: temporarily unavailable")
        return SimpleNamespace(message_id=len(calls))

    asyncio.run(deliver_cycle(outbox, cycle, [111], limit=3500, send=partial))
    assert outbox.delivered_event_keys(111, "stock")
    assert not outbox.event_delivered(identity.stock_delivery_key(111, rows[-1]))
    clock[0] += timedelta(seconds=1000)
    retried = []

    async def success(**kwargs):
        retried.append(kwargs["text"])
        return SimpleNamespace(message_id=100 + len(retried))

    asyncio.run(deliver_cycle(outbox, cycle, [111], limit=64, send=success))
    assert retried == calls[2:]
    assert len(outbox.delivered_event_keys(111, "stock")) == 300


def test_expired_failed_parts_never_become_delivered_or_get_new_deadline(queue):
    outbox, clock = queue
    rows = [stock(f"STOCK{i:03d}") for i in range(300)]
    cycle = result("expiry", rows)
    enqueue_cycle(outbox, cycle, [111], limit=3500)
    calls = []

    async def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("NetworkError: temporarily unavailable")

    asyncio.run(deliver_cycle(outbox, cycle, [111], limit=3500, send=fail))
    original = [item.expires_at for item in outbox.all_items()]
    clock[0] += timedelta(hours=13)
    asyncio.run(deliver_cycle(outbox, cycle, [111], limit=3500, send=fail))
    assert len(calls) == len(original)
    assert all(item.status == "expired" for item in outbox.all_items())
    assert [item.expires_at for item in outbox.all_items()] == original
    assert not outbox.delivered_event_keys(111, "stock")
    assert enqueue_cycle(outbox, result("later", rows), [111], limit=3500) == 0


def test_recipient_bodies_filter_delivered_and_pending_events(queue):
    outbox, _ = queue
    old, new = stock("OLD"), stock("NEW")
    first = result("first", [old])
    enqueue_cycle(outbox, first, [111], limit=3500)

    async def send(**kwargs):
        return SimpleNamespace(message_id=1)

    asyncio.run(deliver_cycle(outbox, first, [111], limit=3500, send=send))
    second = result("second", [old, new])
    assert enqueue_cycle(outbox, second, [111, 222], limit=3500) == 2
    body111 = outbox.items_for_run("second", recipient=111)[0].message_text
    body222 = outbox.items_for_run("second", recipient=222)[0].message_text
    assert "NEW" in body111 and "OLD" not in body111
    assert "NEW" in body222 and "OLD" in body222
    assert enqueue_cycle(outbox, result("pending-again", [old, new]), [111, 222], limit=3500) == 0


def test_direct_delivery_uses_original_persisted_parts_after_input_changes(queue):
    outbox, _ = queue
    cycle = result("persisted", [stock("ORIGINAL")])
    enqueue_cycle(outbox, cycle, [111], limit=3500, analysis_time=NOW)
    expected = [item.message_text for item in outbox.all_items()]
    sent = []

    async def send(**kwargs):
        sent.append(kwargs["text"])
        return SimpleNamespace(message_id=1)

    changed = replace(cycle, stock_result=StockResult(status="success", stocks=[stock("CHANGED")]))
    asyncio.run(deliver_cycle(outbox, changed, [111], limit=64, send=send,
                              analysis_time=NOW + timedelta(hours=1)))
    assert sent == expected
    assert outbox.event_delivered(identity.stock_delivery_key(111, stock("ORIGINAL")))
    assert not outbox.event_delivered(identity.stock_delivery_key(111, stock("CHANGED")))


def test_identical_rendered_rows_keep_distinct_fire_memberships(queue):
    outbox, _ = queue
    rows = [stock("SAME&😀", fire_time=(NOW + timedelta(minutes=i)).isoformat()) for i in range(50)]
    enqueue_cycle(outbox, result("lookalikes", rows), [111], limit=200)
    observed = []
    for item in outbox.all_items():
        assert formatting.telegram_length(item.message_text) <= 200
        observed.extend(event["delivery_key"] for event in item.payload["events"])
    expected = [identity.stock_delivery_key(111, row) for row in rows]
    assert list(dict.fromkeys(observed)) == expected
    carriers = [item for item in outbox.all_items() if item.payload["events"]]
    first = outbox.claim_by_keys("first", [carriers[0].delivery_key])[0]
    assert outbox.mark_sent(first.id, 7, owner=first.claim_owner, claim_token=first.claim_token)
    completed = {event["delivery_key"] for event in first.payload["events"]}
    for item in carriers[1:]:
        completed -= {event["delivery_key"] for event in item.payload["events"]}
    assert completed
    assert set(outbox.delivered_event_keys(111, "stock")) == completed


def test_oversized_event_acknowledges_only_when_every_fragment_succeeds(queue):
    outbox, _ = queue
    row = stock("&😀" * 200)
    enqueue_cycle(outbox, result("oversized", [row]), [111], limit=200)
    key = identity.stock_delivery_key(111, row)
    carriers = [item for item in outbox.all_items() if item.payload["events"]]
    assert len(carriers) > 2
    for item in outbox.all_items():
        assert formatting.telegram_length(item.message_text) <= 200
    for carrier in carriers[:-1]:
        item = outbox.claim_by_keys("fragments", [carrier.delivery_key])[0]
        assert outbox.mark_sent(item.id, 7, owner=item.claim_owner, claim_token=item.claim_token)
        assert not outbox.event_delivered(key)
    last = outbox.claim_by_keys("fragments", [carriers[-1].delivery_key])[0]
    assert outbox.mark_sent(last.id, 8, owner=last.claim_owner, claim_token=last.claim_token)
    assert outbox.event_delivered(key)


def test_lock_contender_denied_during_empty_metadata_initialization(tmp_path, monkeypatch):
    path = tmp_path / "cycle.lock"
    first, second = CycleLock(path), CycleLock(path)
    paused, resume = threading.Event(), threading.Event()
    original_write = os.write
    acquired = []

    def write(fd, payload):
        if threading.current_thread().name == "initializing-owner" and b'"owner"' in bytes(payload):
            paused.set()
            assert resume.wait(5)
        return original_write(fd, payload)

    monkeypatch.setattr(os, "write", write)
    thread = threading.Thread(name="initializing-owner", target=lambda: acquired.append(first.acquire("a")))
    thread.start()
    try:
        assert paused.wait(5)
        assert path.read_bytes() == b""
        assert second.acquire("b") is False
    finally:
        resume.set()
        thread.join(timeout=5)
        first.release()
        second.release()
    assert not thread.is_alive()
    assert acquired == [True]


def test_simultaneous_stale_metadata_reclaimers_have_one_owner(tmp_path):
    path = tmp_path / "cycle.lock"
    path.write_text(json.dumps({"owner": "dead", "started_at": "2020-01-01T00:00:00Z"}))
    start = threading.Barrier(3)
    attempted = threading.Barrier(3)
    release = threading.Event()
    outcomes = []

    def contender(owner):
        lock = CycleLock(path)
        try:
            start.wait(timeout=5)
            outcomes.append(lock.acquire(owner))
            attempted.wait(timeout=5)
            assert release.wait(5)
        finally:
            lock.release()

    threads = [threading.Thread(target=contender, args=(owner,)) for owner in ["a", "b"]]
    for thread in threads:
        thread.start()
    try:
        start.wait(timeout=5)
        attempted.wait(timeout=5)
        assert sorted(outcomes) == [False, True]
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert path.with_name(path.name + ".guard").exists()


def test_dead_process_releases_kernel_lock_and_live_process_cannot_be_reclaimed(tmp_path):
    path = tmp_path / "cycle.lock"
    source = "\n".join([
        "import sys", "from tradingview.scheduler.cycle import CycleLock",
        f"lock = CycleLock({str(path)!r})", "assert lock.acquire('child')",
        "print('held', flush=True)", "sys.stdin.read()",
    ])
    child = subprocess.Popen([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
                             stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
    contender = CycleLock(path, stale_seconds=0)
    try:
        assert child.stdout.readline().strip() == "held"
        assert contender.acquire("parent") is False
        child.kill()
        child.wait(timeout=5)
        assert contender.acquire("parent") is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        contender.release()
        child.stdout.close()
        child.stdin.close()


def test_releasing_nonowner_and_old_owner_cannot_unlock_current_owner(tmp_path):
    path = tmp_path / "cycle.lock"
    first, second = CycleLock(path), CycleLock(path)
    assert first.acquire("a")
    second.release()
    assert not second.acquire("a")  # Matching text is not ownership.
    first.release()
    assert second.acquire("b")
    first.release()
    assert not CycleLock(path).acquire("c")
    second.release()
    third = CycleLock(path)
    assert third.acquire("c")
    third.release()


def test_direct_stale_ack_is_skipped_after_new_worker_acknowledges(queue):
    outbox, clock = queue
    row = stock()
    cycle = result("stale-ack", [row])
    enqueue_cycle(outbox, cycle, [111], limit=3500)

    async def late_ack(**kwargs):
        clock[0] += timedelta(seconds=outbox.claim_lease_seconds + 1)
        newer = outbox.claim_due("newer")[0]
        assert outbox.mark_sent(newer.id, 222, owner=newer.claim_owner, claim_token=newer.claim_token)
        return SimpleNamespace(message_id=111)

    receipts = asyncio.run(deliver_cycle(outbox, cycle, [111], limit=3500, send=late_ack))
    assert receipts[111][0].status == "skipped"
    assert outbox.all_items()[0].telegram_message_id == 222
    assert outbox.event_delivered(identity.stock_delivery_key(111, row))


def test_direct_lost_lease_blocks_plain_fallback_and_keeps_newer_claim(queue):
    outbox, clock = queue
    cycle = result("stale-fallback", [stock()])
    enqueue_cycle(outbox, cycle, [111], limit=3500)
    calls = []
    newer = []

    async def formatting_failure(**kwargs):
        calls.append(kwargs)
        clock[0] += timedelta(seconds=outbox.claim_lease_seconds + 1)
        newer.extend(outbox.claim_due("newer"))
        raise RuntimeError("Bad Request: can't parse entities")

    receipts = asyncio.run(deliver_cycle(outbox, cycle, [111], limit=3500, send=formatting_failure))
    assert len(calls) == 1
    assert receipts[111][0].status == "skipped"
    current = outbox.get(newer[0].id)
    assert current.status == "claimed"
    assert current.claim_owner == "newer"
    assert current.claim_token == newer[0].claim_token
    assert current.attempt_count == 0
