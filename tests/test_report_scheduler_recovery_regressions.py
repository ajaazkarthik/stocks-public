"""Durable recovery through the real startup callback, with no external effects."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from threading import Barrier

import pytest

import report_scheduler as rs
from tradingview import outcome_store
from tradingview.scanner_store import record_observation as record_scanner_observation
from tradingview.scheduler import identity, stock_results
from tradingview.scheduler.config import SchedulerConfig
from tradingview.scheduler.cycle import CyclePaths, CycleResult, enqueue_cycle
from tradingview.scheduler.dispatch import dispatch_pending, reconcile, reconcile_cycle_results
from tradingview.scheduler.outbox import (
    STATUS_CLAIMED, STATUS_EXPIRED, STATUS_SENT, NotificationOutbox, sqlite_factory,
)
from tradingview.scheduler.timeutil import parse_iso


NOW = datetime(2026, 10, 14, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Bot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=len(self.calls))


def stock(symbol="GOOG"):
    return {
        "exchange": "NASDAQ", "symbol": symbol, "pro_symbol": f"NASDAQ:{symbol}",
        "score": 80.0, "signal": "BUY", "resolution": "60",
        "fire_time": (NOW - timedelta(hours=2)).isoformat(),
    }


def option(strike=530.0):
    return {
        "exchange": "NASDAQ", "symbol": "MSFT", "opt_type": "CALL",
        "strike": strike, "expiry": "2026-11-20", "score": 85.0,
        "parent_score": 91.0, "strategy_modes": ["BALANCED"],
        "contract_id": f"MSFT-20261120-C-{strike}",
    }


def cycle(run_id="run-1", *, stocks=(), options=(), started=NOW):
    return CycleResult(
        run_id=run_id, started_at=started.isoformat(), completed_at=started.isoformat(),
        stock_result=stock_results.StockResult(
            status="success", stocks=list(stocks), run_id=run_id,
        ),
        option_result={"status": "success", "options": list(options)},
    )


def persist_cycle(root, result):
    paths = CyclePaths(root, result.run_id).ensure()
    result.write(paths.cycle_result)


def seed_scoring(conn, row, *, run_id="run-1", scored=NOW):
    outcome_store.persist_completed(
        conn, row, run_id, {"composite": row["score"], "actionable": True},
    )
    payload = outcome_store.load_scoring_results(conn, run_id=run_id)[0]
    payload["scored_at"] = scored.isoformat()
    conn.execute(
        "UPDATE scoring_results SET scored_at=?, payload_json=? WHERE run_id=?",
        (scored.isoformat(), json.dumps(payload), run_id),
    )
    conn.commit()


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """Replace only database/config/bot boundaries used by registered startup."""
    clock = Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock)
    ledger = tmp_path / "ledger.db"
    conn = sqlite3.connect(ledger)
    outcome_store.ensure_schema(conn)
    conn.commit()
    conn.close()
    run_root = tmp_path / "runs"
    config = SchedulerConfig(run_root=run_root, alert_ttl_hours=12)
    monkeypatch.setattr("tradingview.scheduler.config.load_config", lambda: config)
    monkeypatch.setattr("database.fires_ledger._conn", lambda: sqlite3.connect(ledger))
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    monkeypatch.setattr(rs, "_allowed_users", lambda: {111, 222})
    monkeypatch.setattr(rs, "_scheduler_paused", False)
    bot = Bot()
    return SimpleNamespace(
        clock=clock, outbox=outbox, ledger=ledger, root=run_root, bot=bot,
        run=lambda: asyncio.run(rs._job_startup_recovery(SimpleNamespace(bot=bot))),
    )


def test_startup_pending_normal_and_both_recovery_sources_send_once(startup):
    row = stock()
    result = cycle(stocks=[row])
    persist_cycle(startup.root, result)
    with sqlite3.connect(startup.ledger) as conn:
        seed_scoring(conn, row)
    assert enqueue_cycle(startup.outbox, result, [111, 222], limit=3500) == 2

    startup.run()
    startup.run()

    assert len(startup.bot.calls) == 2
    assert {call["chat_id"] for call in startup.bot.calls} == {111, 222}
    assert len(startup.outbox.all_items()) == 2
    assert startup.outbox.counts() == {STATUS_SENT: 2}
    for recipient in [111, 222]:
        assert startup.outbox.event_delivered(identity.stock_delivery_key(recipient, row))


def test_startup_old_scoring_and_cycle_do_not_receive_fresh_ttl(startup):
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row = stock()
    persist_cycle(startup.root, cycle(stocks=[row], options=[option()], started=stale))
    with sqlite3.connect(startup.ledger) as conn:
        seed_scoring(conn, row, scored=stale)

    startup.run()
    assert startup.bot.calls == []
    assert startup.outbox.all_items() == []


def test_startup_expired_normal_alert_is_not_resurrected(startup):
    row = stock()
    result = cycle(stocks=[row])
    persist_cycle(startup.root, result)
    assert enqueue_cycle(startup.outbox, result, [111, 222], limit=3500) == 2
    original_expiry = startup.outbox.all_items()[0].expires_at
    startup.clock.now += timedelta(hours=13)

    startup.run()
    assert startup.bot.calls == []
    items = startup.outbox.all_items()
    assert len(items) == 2
    assert all(item.status == STATUS_EXPIRED for item in items)
    assert all(item.expires_at == original_expiry for item in items)

    # Even a later persisted cycle cannot refresh the first alert deadline.
    persist_cycle(startup.root, cycle("later-run", stocks=[row], started=startup.clock.now))
    startup.run()
    assert startup.bot.calls == []
    assert len(startup.outbox.all_items()) == 2


def test_startup_recovers_option_only_cycle_with_original_contracts(startup):
    rows = [option(530), option(535)]
    persist_cycle(startup.root, cycle(options=rows))
    startup.run()
    startup.run()

    assert len(startup.bot.calls) == 4
    for recipient in [111, 222]:
        for row in rows:
            key = identity.option_delivery_key(recipient, row)
            assert startup.outbox.event_delivered(key)
            item = next(
                item for item in startup.outbox.all_items()
                if item.payload["events"][0]["delivery_key"] == key
            )
            assert row["contract_id"] in item.message_text
            assert str(row["strike"]) in item.message_text
            assert row["expiry"] in item.message_text
            assert parse_iso(item.expires_at) == NOW + timedelta(hours=12)


def test_startup_mixed_cycle_respects_individual_recipient_histories(startup):
    old, new = stock("OLD"), stock("NEW")
    call = option()
    persist_cycle(startup.root, cycle(stocks=[old, new], options=[call]))
    startup.outbox.record_events_delivered([
        {"delivery_key": identity.stock_delivery_key(111, old),
         "recipient": 111, "kind": identity.KIND_STOCK},
        {"delivery_key": identity.option_delivery_key(222, call),
         "recipient": 222, "kind": identity.KIND_OPTION},
    ], run_id="previous-run", message_id=7)

    startup.run()
    startup.run()

    assert len(startup.bot.calls) == 4
    recipient_111 = [call["text"] for call in startup.bot.calls if call["chat_id"] == 111]
    recipient_222 = [call["text"] for call in startup.bot.calls if call["chat_id"] == 222]
    assert all("OLD" not in text for text in recipient_111)
    assert any("NEW" in text for text in recipient_111)
    assert any("MSFT" in text for text in recipient_111)
    assert all("MSFT" not in text for text in recipient_222)
    for recipient in [111, 222]:
        assert startup.outbox.event_delivered(identity.stock_delivery_key(recipient, old))
        assert startup.outbox.event_delivered(identity.stock_delivery_key(recipient, new))
        assert startup.outbox.event_delivered(identity.option_delivery_key(recipient, call))


def test_scoring_recovery_retains_original_deadline(startup):
    scored = NOW - timedelta(hours=11)
    with sqlite3.connect(startup.ledger) as conn:
        seed_scoring(conn, stock(), scored=scored)
        assert reconcile(startup.outbox, conn, [111]) == 1
    assert parse_iso(startup.outbox.all_items()[0].expires_at) == scored + timedelta(hours=12)
    startup.clock.now += timedelta(hours=2)
    assert asyncio.run(dispatch_pending(startup.outbox, bot=startup.bot)).expired == 1
    assert startup.bot.calls == []


def test_scanner_setup_recovery_is_production_scoped_and_daily_key_compatible(tmp_path):
    ledger = tmp_path / "scanner-ledger.db"
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "scanner-outbox.db"), clock=Clock())
    first = {
        "source": "scanner", "market": "SG", "session_date": "2026-10-14",
        "timestamp": NOW.isoformat(), "direction": "BUY", "actionable": True,
        "tv_symbol": "SGX:BS6", "fire_time": NOW.isoformat(), "resolution": "D",
        "composite": 80.0, "signal": "BUY", "bar_time": NOW.isoformat(),
        "quote_source": "Yahoo Finance 15m chart",
    }
    repeated = {**first, "composite": 84.0, "timestamp": (NOW + timedelta(minutes=10)).isoformat()}
    with sqlite3.connect(ledger) as conn:
        # Pre-migration scanner scoring rows remain readable and share the
        # unchanged daily delivery key with setup-scoped recovery.
        outcome_store.ensure_schema(conn)
        outcome_store.record_scoring_result(
            conn,
            {"pro_symbol": "SGX:BS6", "fire_time": NOW.isoformat(), "resolution": "D"},
            {"source": "scanner", "market": "SG", "session_date": "2026-10-14",
             "timestamp": NOW.isoformat(), "direction": "BUY", "actionable": True,
             "composite": 80.0, "signal": "BUY"},
            "legacy-scan",
        )
        record_scanner_observation(conn, first, run_id="scan-a", scope="production")
        record_scanner_observation(conn, repeated, run_id="scan-b", scope="production")
        record_scanner_observation(conn, first, run_id="test-a", scope="test")
        assert reconcile(outbox, conn, [111]) == 1
    rows = outbox.all_items()
    assert len(rows) == 1
    event = rows[0].payload["events"][0]
    expected = identity.scanner_delivery_key(111, {
        **first, "pro_symbol": "SGX:BS6", "symbol": "BS6",
    })
    assert event["delivery_key"] == expected
    with sqlite3.connect(ledger) as conn:
        stored = conn.execute(
            "SELECT first_result_json FROM scanner_setups WHERE scope='production'"
        ).fetchone()[0]
        assert json.loads(stored)["composite"] == 80.0


def test_concurrent_normal_and_cycle_recovery_reserve_one_send(tmp_path):
    clock = Clock()
    factory = sqlite_factory(tmp_path / "outbox.db")
    normal = NotificationOutbox(factory, clock=clock)
    recovering = NotificationOutbox(factory, clock=clock)
    # Initialize only the temporary schema before the controlled race.
    assert normal.all_items() == []
    result = cycle(stocks=[stock()])
    root = tmp_path / "runs"
    persist_cycle(root, result)
    barrier = Barrier(2)

    def enqueue_normal():
        barrier.wait()
        return enqueue_cycle(normal, result, [111], limit=3500)

    def enqueue_recovery():
        barrier.wait()
        return reconcile_cycle_results(recovering, root, [111])

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(enqueue_normal)
        second = executor.submit(enqueue_recovery)
        assert first.result() + second.result() == 1
    bot = Bot()
    assert asyncio.run(dispatch_pending(normal, bot=bot)).sent == 1
    assert len(bot.calls) == len(normal.all_items()) == 1


@pytest.mark.parametrize("timestamp", [None, "", "bad-date"])
def test_cycle_recovery_cannot_establish_freshness_without_original_time(startup, timestamp):
    payload = cycle(options=[option()]).to_dict()
    payload["started_at"] = timestamp
    paths = CyclePaths(startup.root, "run-1").ensure()
    paths.cycle_result.write_text(json.dumps(payload))
    assert reconcile_cycle_results(startup.outbox, startup.root, [111]) == 0
    assert startup.outbox.all_items() == []


@pytest.mark.parametrize("late_outcome", ["sent", "failure", "ambiguous"])
@pytest.mark.parametrize("new_owner_sent", [False, True])
def test_dispatcher_late_completion_cannot_mutate_reclaimed_work(
    tmp_path, late_outcome, new_owner_sent,
):
    clock = Clock()
    outbox = NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"), clock=clock, claim_lease_seconds=1,
    )
    row = stock()
    event_key = identity.stock_delivery_key(111, row)
    enqueue_cycle(outbox, cycle(stocks=[row]), [111], limit=3500)

    async def sender(**kwargs):
        clock.now += timedelta(seconds=2)
        replacement = outbox.claim_due("worker-b")[0]
        if new_owner_sent:
            assert outbox.mark_sent(
                replacement.id, 222, owner="worker-b", claim_token=replacement.claim_token,
            )
        if late_outcome == "sent":
            return SimpleNamespace(message_id=111)
        if late_outcome == "ambiguous":
            return SimpleNamespace()
        raise RuntimeError("Forbidden: bot was blocked by the user")

    report = asyncio.run(dispatch_pending(outbox, send=sender, owner="worker-a"))
    item = outbox.all_items()[0]
    assert report.sent == report.failed == report.ambiguous == 0
    assert report.errors and "lost claim ownership" in report.errors[0]
    if new_owner_sent:
        assert item.status == STATUS_SENT
        assert item.telegram_message_id == 222
        assert outbox.event_delivered(event_key)
    else:
        assert item.status == STATUS_CLAIMED
        assert item.claim_owner == "worker-b"
        assert not outbox.event_delivered(event_key)


def test_dispatcher_missing_sender_releases_only_its_current_claim(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    enqueue_cycle(outbox, cycle(stocks=[stock()]), [111], limit=3500)
    report = asyncio.run(dispatch_pending(outbox, owner="worker-a"))
    assert report.sent == 0
    assert report.errors == ["no sender available"]
    assert outbox.all_items()[0].claim_owner is None


def test_dispatcher_does_not_send_later_parts_after_their_bulk_lease_expires(tmp_path):
    clock = Clock()
    outbox = NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"), clock=clock, claim_lease_seconds=1,
    )
    for key in ["first", "second"]:
        outbox.enqueue(delivery_key=key, recipient=111, kind="summary",
                       payload={}, message_text=key)
    calls = []

    async def sender(**kwargs):
        calls.append(kwargs["text"])
        clock.now += timedelta(seconds=2)
        return SimpleNamespace(message_id=1)

    report = asyncio.run(dispatch_pending(outbox, send=sender, owner="worker-a"))
    assert calls == ["first"]
    assert report.sent == 0
    assert any("send lost claim ownership" in error for error in report.errors)
    # The expired leases remain reclaimable on the next pass.
    assert len(outbox.claim_due("worker-b")) == 2


def test_dispatcher_does_not_start_plain_fallback_after_lease_expires(tmp_path):
    clock = Clock()
    outbox = NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"), clock=clock, claim_lease_seconds=1,
    )
    outbox.enqueue(delivery_key="part", recipient=111, kind="summary",
                   payload={}, message_text="body")
    calls = []

    async def sender(**kwargs):
        calls.append(kwargs)
        clock.now += timedelta(seconds=2)
        raise RuntimeError("BadRequest: can't parse entities")

    report = asyncio.run(dispatch_pending(outbox, send=sender, owner="worker-a"))
    assert len(calls) == 1
    assert report.sent == 0
    assert report.errors == ["outbox:1: fallback lost claim ownership"]
