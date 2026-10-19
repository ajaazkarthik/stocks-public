"""Offline checks across the application's enqueue, startup and dispatch callbacks."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import report_scheduler as scheduler
from tradingview import outcome_store
from tradingview.scheduler import identity
from tradingview.scheduler.cycle import CycleResult, enqueue_cycle
from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory
from tradingview.scheduler.stock_results import StockResult

NOW = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)


def stock(symbol="ONE"):
    return dict(pro_symbol=f"NASDAQ:{symbol}", exchange="NASDAQ", symbol=symbol,
                score=80.0, signal="BUY", fire_time=NOW.isoformat(), resolution="60")


def option():
    return dict(exchange="NASDAQ", symbol="MSFT", opt_type="CALL", strike="530",
                expiry="2030-06-19", score=79.0, parent_score=91.0,
                strategy_modes=["BALANCED"])


def result(run_id, stocks=None, options=None):
    return CycleResult(
        run_id=run_id, started_at=NOW.isoformat(), completed_at=NOW.isoformat(),
        display_timezone="Australia/Sydney",
        stock_result=StockResult(status="success", stocks=stocks or []),
        option_result={"status": "success", "options": options or []},
    )


class Bot:
    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return SimpleNamespace(message_id=len(self.calls))


@pytest.fixture
def app(tmp_path, monkeypatch):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: NOW)
    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(ledger) as conn:
        outcome_store.ensure_schema(conn)
    config = SimpleNamespace(run_root=tmp_path / "runs", alert_ttl_hours=12, message_limit=3500)
    monkeypatch.setattr(scheduler, "_scheduler_outbox", lambda: outbox)
    monkeypatch.setattr(scheduler, "_allowed_users", lambda: {111, 222})
    monkeypatch.setattr(scheduler, "_scheduler_paused", False)
    monkeypatch.setattr("tradingview.scheduler.config.load_config", lambda: config)
    monkeypatch.setattr("database.fires_ledger._conn", lambda: sqlite3.connect(ledger))
    return outbox, config


def test_startup_reuses_pending_mixed_cycle_for_each_recipient(app):
    outbox, config = app
    cycle = result("pending", [stock()], [option()])
    cycle.write(config.run_root / cycle.run_id / "cycle_result.json")
    enqueue_cycle(outbox, cycle, [111, 222], limit=3500)
    before = len(outbox.all_items())
    bot = Bot()
    asyncio.run(scheduler._job_startup_recovery(SimpleNamespace(bot=bot)))
    assert len(outbox.all_items()) == before
    assert len(bot.calls) == before
    for recipient in [111, 222]:
        assert outbox.event_delivered(identity.stock_delivery_key(recipient, stock()))
        assert outbox.event_delivered(identity.option_delivery_key(recipient, option()))
    # An additional restart must not create any more work or sends.
    asyncio.run(scheduler._job_startup_recovery(SimpleNamespace(bot=bot)))
    assert len(bot.calls) == before


def test_startup_recovers_option_only_crash_before_enqueue(app):
    outbox, config = app
    cycle = result("option-only", options=[option()])
    cycle.write(config.run_root / cycle.run_id / "cycle_result.json")
    bot = Bot()
    asyncio.run(scheduler._job_startup_recovery(SimpleNamespace(bot=bot)))
    assert len(bot.calls) == 2
    assert all("MSFT" in call["text"] and "CALL" in call["text"] for call in bot.calls)
    for recipient in [111, 222]:
        assert outbox.event_delivered(identity.option_delivery_key(recipient, option()))


def test_application_partial_delivery_does_not_acknowledge_failed_parts(app):
    outbox, _ = app
    rows = [stock(f"STOCK{i:03d}") for i in range(300)]
    bot = Bot(fail_after=1)
    asyncio.run(scheduler._deliver_cycle_result(bot, result("multipart", rows), [111]))
    assert len(bot.calls) > 1
    assert rows[-1]["symbol"] not in bot.calls[0]["text"]
    assert not outbox.event_delivered(identity.stock_delivery_key(111, rows[-1]))
    sent_events = [event for item in outbox.all_items() if item.status == "sent"
                   for event in item.payload.get("events", [])]
    # A header-only first part correctly acknowledges no stock events.
    assert all(outbox.event_delivered(event["delivery_key"]) for event in sent_events)


def test_application_filters_delivered_rows_independently_per_recipient(app):
    outbox, _ = app
    bot = Bot()
    asyncio.run(scheduler._deliver_cycle_result(bot, result("old", [stock("OLD")]), [111]))
    bot.calls.clear()
    asyncio.run(scheduler._deliver_cycle_result(
        bot, result("mixed", [stock("OLD"), stock("NEW")]), [111, 222]))
    recipient_a = "\n".join(call["text"] for call in bot.calls if call["chat_id"] == 111)
    recipient_b = "\n".join(call["text"] for call in bot.calls if call["chat_id"] == 222)
    assert "OLD" not in recipient_a and "NEW" in recipient_a
    assert "OLD" in recipient_b and "NEW" in recipient_b


def test_startup_never_revives_an_expired_cycle(app):
    outbox, config = app
    cycle = result("expired", [stock()], [option()])
    cycle.started_at = cycle.completed_at = "2020-01-01T00:00:00+00:00"
    cycle.write(config.run_root / cycle.run_id / "cycle_result.json")
    bot = Bot()
    asyncio.run(scheduler._job_startup_recovery(SimpleNamespace(bot=bot)))
    assert bot.calls == []
    assert outbox.pending_count() == 0
