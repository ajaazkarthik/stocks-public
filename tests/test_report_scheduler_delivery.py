"""Offline tests for durable delivery, deduplication, and safe formatting.

Covers Issues 1 (delivery dedup), 5 (formatting/fallback), 6 (durable handoff
and outbox), and 10 (independent per-recipient delivery).

No real Telegram client is constructed and no network call is possible: the
"bot" is a local stub and the outbox is a temporary SQLite database.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingview.scheduler import formatting, identity
from tradingview.scheduler.config import TELEGRAM_MESSAGE_LIMIT
from tradingview.scheduler.cycle import CycleResult, build_summary_parts, deliver_cycle, enqueue_cycle
from tradingview.scheduler.outbox import (
    STATUS_AMBIGUOUS,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    NotificationOutbox,
    sqlite_factory,
)
from tradingview.scheduler import stock_results

RUN_ID = "ss_20261013T064219Z_delivery1"
FROZEN_NOW = datetime(2026, 10, 13, 6, 42, 19, tzinfo=timezone.utc)

#: A synthetic token used only to prove redaction.
SYNTHETIC_TOKEN = "1234567890:AAFakeTokenValueForRedactionTestsOnly"


class FakeClock:
    """Injected clock so retry/backoff tests never sleep."""

    def __init__(self, start: datetime = FROZEN_NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeBot:
    """Local Telegram stub. Records every call; never touches the network."""

    def __init__(self, *, reject_html: bool = False, reject_all: bool = False,
                 fail_labels: set[str] | None = None) -> None:
        self.reject_html = reject_html
        self.reject_all = reject_all
        self.fail_labels = fail_labels or set()
        self.calls: list[dict] = []
        self._next_id = 1000

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs.get("text", "")
        if self.reject_all:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        if self.reject_html and kwargs.get("parse_mode") == "HTML":
            raise RuntimeError(
                "Bad Request: can't parse entities: can't find end of the entity"
            )
        for label in self.fail_labels:
            if label in text:
                raise RuntimeError("Bad Request: can't parse entities")
        self._next_id += 1
        return type("Msg", (), {"message_id": self._next_id})()


def _outbox(tmp_path: Path, clock: FakeClock | None = None, **kwargs) -> NotificationOutbox:
    return NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"),
        clock=clock or FakeClock(),
        **kwargs,
    )


def _stock(pro_symbol: str = "NASDAQ:GOOG", score: float = 71.0,
           fire_time: str = "2026-10-13T05:00:00Z", resolution: str = "60") -> dict:
    exchange, _, symbol = pro_symbol.partition(":")
    return {
        "pro_symbol": pro_symbol, "exchange": exchange, "symbol": symbol,
        "score": score, "signal": "BUY", "fire_time": fire_time,
        "resolution": resolution,
    }


def _option(symbol: str = "MSFT", strike: float = 530.0, expiry: str = "2030-06-19",
            modes: list[str] | None = None) -> dict:
    return {
        "exchange": "NASDAQ", "symbol": symbol, "opt_type": "CALL",
        "strike": strike, "expiry": expiry, "dte": 45, "score": 79.0,
        "parent_score": 91.0, "strategy_modes": modes or ["BALANCED"],
    }


def _cycle_result(stocks: list[dict] | None = None, options: list[dict] | None = None,
                  stock_status: str = stock_results.STATUS_SUCCESS,
                  options_status: str = "success") -> CycleResult:
    return CycleResult(
        run_id=RUN_ID,
        started_at=FROZEN_NOW.isoformat(),
        completed_at=FROZEN_NOW.isoformat(),
        display_timezone="Australia/Sydney",
        stock_result=stock_results.StockResult(status=stock_status, stocks=stocks or []),
        option_result={"options": options or [], "status": options_status, "errors": []},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Issue 1 — stable identities and delivery deduplication
# ══════════════════════════════════════════════════════════════════════════════

def test_same_fire_same_recipient_has_stable_identity():
    stock = _stock()
    assert identity.stock_delivery_key(111, stock) == identity.stock_delivery_key(111, dict(stock))


def test_identity_is_per_recipient():
    stock = _stock()
    assert identity.stock_delivery_key(111, stock) != identity.stock_delivery_key(222, stock)


def test_new_fire_time_is_a_new_identity():
    first = _stock(fire_time="2026-10-13T05:00:00Z")
    second = _stock(fire_time="2026-10-13T07:00:00Z")
    assert identity.stock_delivery_key(111, first) != identity.stock_delivery_key(111, second)


def test_new_resolution_is_a_new_identity():
    first = _stock(resolution="60")
    second = _stock(resolution="D")
    assert identity.stock_delivery_key(111, first) != identity.stock_delivery_key(111, second)


def test_run_id_does_not_change_fire_identity():
    """A restart or re-score with another run ID must not resend the same fire."""
    stock = _stock()
    key = identity.stock_delivery_key(111, stock)
    assert key == identity.stock_delivery_key(111, stock)


def test_option_identity_distinguishes_contracts_on_same_ticker():
    first = _option(strike=530.0)
    second = _option(strike=540.0)
    assert identity.option_delivery_key(111, first) != identity.option_delivery_key(111, second)


def test_option_identity_distinguishes_policy():
    first = _option(modes=["BALANCED"])
    second = _option(modes=["AGGRESSIVE"])
    assert identity.option_delivery_key(111, first) != identity.option_delivery_key(111, second)


def test_option_identity_prefers_validated_contract_id():
    first = {"contract_id": "AAPL-2030-06-19-C-100", "exchange": "NASDAQ", "symbol": "AAPL"}
    second = {"contract_id": "AAPL-2030-06-19-C-110", "exchange": "NASDAQ", "symbol": "AAPL"}
    assert identity.option_delivery_key(111, first) != identity.option_delivery_key(111, second)


def test_duplicate_recipients_are_normalized():
    assert identity.normalize_recipients([222, 111, 222, "111"]) == [111, 222]


def test_enqueue_is_idempotent_for_the_same_delivery_key(tmp_path):
    outbox = _outbox(tmp_path)
    key = identity.stock_delivery_key(111, _stock())
    assert outbox.enqueue(
        delivery_key=key, recipient=111, kind="stock", payload=_stock(),
        message_text="hello",
    ) is True
    assert outbox.enqueue(
        delivery_key=key, recipient=111, kind="stock", payload=_stock(),
        message_text="hello",
    ) is False
    assert len(outbox.all_items()) == 1


def test_run_a_sends_once_and_run_b_does_not_replay(tmp_path):
    """Run A has an actionable fire; run B has none. B must not replay A."""
    outbox = _outbox(tmp_path)
    stock = _stock()
    key = identity.stock_delivery_key(111, stock)

    assert outbox.enqueue(
        delivery_key=key, recipient=111, kind="stock", payload=stock,
        message_text="alert", run_id="run-a",
    ) is True
    item = outbox.claim_due("worker")[0]
    outbox.mark_sent(item.id, 42, owner=item.claim_owner, claim_token=item.claim_token)

    # Run B re-derives the same identity for the same fire.
    assert outbox.enqueue(
        delivery_key=key, recipient=111, kind="stock", payload=stock,
        message_text="alert", run_id="run-b",
    ) is False
    assert outbox.counts().get(STATUS_SENT) == 1


def test_restart_does_not_resend_an_acknowledged_fire(tmp_path):
    outbox = _outbox(tmp_path)
    stock = _stock()
    key = identity.stock_delivery_key(111, stock)
    outbox.enqueue(delivery_key=key, recipient=111, kind="stock",
                   payload=stock, message_text="alert")
    item = outbox.claim_due("worker-1")[0]
    outbox.mark_sent(item.id, 7, owner=item.claim_owner, claim_token=item.claim_token)

    # A fresh dispatcher process re-reads the same durable state.
    restarted = _outbox(tmp_path)
    assert restarted.get_by_key(key).status == STATUS_SENT
    assert restarted.pending_count() == 0


def test_summary_identity_includes_run_id(tmp_path):
    """Each cycle may send one no-new summary; older cycles are not replayed."""
    first = identity.summary_delivery_key(111, "run-a", 0)
    second = identity.summary_delivery_key(111, "run-b", 0)
    assert first != second


# ══════════════════════════════════════════════════════════════════════════════
# Issue 6 — durable outbox: claims, leases, retries, expiry
# ══════════════════════════════════════════════════════════════════════════════

def test_claim_is_atomic_and_exclusive(tmp_path):
    outbox = _outbox(tmp_path)
    for index in range(3):
        outbox.enqueue(
            delivery_key=f"key-{index}", recipient=111, kind="stock",
            payload={}, message_text=f"m{index}",
        )
    first = outbox.claim_due("worker-a", limit=2)
    second = outbox.claim_due("worker-b", limit=2)
    assert len(first) == 2
    assert len(second) == 1
    assert {item.id for item in first}.isdisjoint({item.id for item in second})


def test_expired_lease_is_reclaimed(tmp_path):
    clock = FakeClock()
    outbox = _outbox(tmp_path, clock, claim_lease_seconds=60)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    claimed = outbox.claim_due("crashed-worker")
    assert len(claimed) == 1
    assert outbox.claim_due("other-worker") == []

    clock.advance(120)  # the crashed worker's lease expires
    reclaimed = outbox.claim_due("recovery-worker")
    assert len(reclaimed) == 1
    assert reclaimed[0].id == claimed[0].id


def test_retry_backoff_grows_and_is_bounded(tmp_path):
    clock = FakeClock()
    outbox = _outbox(tmp_path, clock, backoff_base_seconds=10, backoff_cap_seconds=40)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    delays: list[float] = []
    for _ in range(3):
        item = outbox.claim_due("w")[0]
        outbox.mark_failed(item.id, "transient", owner=item.claim_owner, claim_token=item.claim_token)
        refreshed = outbox.get(item.id)
        delays.append(
            (datetime.fromisoformat(refreshed.next_attempt_at) - clock.now).total_seconds()
        )
        clock.advance(delays[-1] + 1)

    assert delays[0] < delays[1] < delays[2]
    assert all(delay <= 40 for delay in delays)


def test_retry_after_hint_is_honoured(tmp_path):
    clock = FakeClock()
    outbox = _outbox(tmp_path, clock, backoff_base_seconds=1, backoff_cap_seconds=5)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    item = outbox.claim_due("w")[0]
    outbox.mark_failed(item.id, "flood", retry_after=120, owner=item.claim_owner, claim_token=item.claim_token)
    refreshed = outbox.get(item.id)
    delay = (datetime.fromisoformat(refreshed.next_attempt_at) - clock.now).total_seconds()
    assert delay >= 120


def test_max_attempts_moves_item_to_terminal_failure(tmp_path):
    clock = FakeClock()
    outbox = _outbox(tmp_path, clock, max_attempts=2, backoff_base_seconds=1)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    item = outbox.claim_due("w")[0]
    assert outbox.mark_failed(item.id, "boom", owner=item.claim_owner, claim_token=item.claim_token) == STATUS_PENDING
    clock.advance(10)
    item = outbox.claim_due("w")[0]
    assert outbox.mark_failed(item.id, "boom", owner=item.claim_owner, claim_token=item.claim_token) == STATUS_FAILED
    assert outbox.pending_count() == 0


def test_expired_item_is_not_delivered_as_current(tmp_path):
    clock = FakeClock()
    outbox = _outbox(tmp_path, clock, alert_ttl_hours=1)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    clock.advance(3600 * 3)
    assert outbox.claim_due("w") == []
    assert outbox.get_by_key("k").status == STATUS_EXPIRED


def test_ambiguous_send_is_not_marked_delivered(tmp_path):
    outbox = _outbox(tmp_path)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    item = outbox.claim_due("w")[0]
    status = outbox.mark_ambiguous(item.id, "ReadTimeout after request was sent", owner=item.claim_owner, claim_token=item.claim_token)
    assert status == STATUS_AMBIGUOUS
    refreshed = outbox.get(item.id)
    assert refreshed.status == STATUS_AMBIGUOUS
    assert refreshed.sent_at is None
    assert refreshed.telegram_message_id is None


def test_release_claim_does_not_count_an_attempt(tmp_path):
    outbox = _outbox(tmp_path)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    item = outbox.claim_due("w")[0]
    outbox.release_claim(item.id, error="shutting down", owner=item.claim_owner, claim_token=item.claim_token)
    refreshed = outbox.get(item.id)
    assert refreshed.status == STATUS_PENDING
    assert refreshed.attempt_count == 0


def test_one_recipient_failure_does_not_block_another(tmp_path):
    outbox = _outbox(tmp_path)
    result = _cycle_result(stocks=[_stock()])
    enqueue_cycle(outbox, result, [111, 222], limit=3500, analysis_time=FROZEN_NOW)

    async def _send(**kwargs):
        if kwargs["chat_id"] == 222:
            raise RuntimeError("Bad Request: can't parse entities")
        return type("Msg", (), {"message_id": 5})()

    receipts = asyncio.run(
        deliver_cycle(outbox, result, [111, 222], send=_send, limit=3500,
                      analysis_time=FROZEN_NOW)
    )
    assert all(receipt.delivered for receipt in receipts[111])
    assert all(not receipt.delivered for receipt in receipts[222])


def test_recipient_states_are_independent(tmp_path):
    outbox = _outbox(tmp_path)
    result = _cycle_result(stocks=[_stock()])
    enqueue_cycle(outbox, result, [111, 222], limit=3500, analysis_time=FROZEN_NOW)

    sent_to: list[int] = []

    async def _send(**kwargs):
        sent_to.append(kwargs["chat_id"])
        if kwargs["chat_id"] == 222:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return type("Msg", (), {"message_id": 5})()

    receipts = asyncio.run(
        deliver_cycle(outbox, result, [111, 222], send=_send, limit=3500,
                      analysis_time=FROZEN_NOW)
    )
    assert all(receipt.delivered for receipt in receipts[111])
    assert all(not receipt.delivered for receipt in receipts[222])

    key_111 = identity.summary_delivery_key(111, RUN_ID, 0)
    key_222 = identity.summary_delivery_key(222, RUN_ID, 0)
    assert outbox.get_by_key(key_111).status == STATUS_SENT
    assert outbox.get_by_key(key_222).status == STATUS_FAILED


def test_retry_only_resends_the_failed_part(tmp_path):
    outbox = _outbox(tmp_path)
    result = _cycle_result(stocks=[_stock()])
    enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)

    parts = build_summary_parts(result, limit=3500, analysis_time=FROZEN_NOW)
    assert len(parts) == 1

    bot = FakeBot()
    asyncio.run(deliver_cycle(outbox, result, [111], bot=bot, limit=3500,
                              analysis_time=FROZEN_NOW))
    first_call_count = len(bot.calls)

    # A second delivery pass must not resend the acknowledged part.
    asyncio.run(deliver_cycle(outbox, result, [111], bot=bot, limit=3500,
                              analysis_time=FROZEN_NOW))
    assert len(bot.calls) == first_call_count


def test_two_dispatchers_produce_one_ordinary_send(tmp_path):
    outbox = _outbox(tmp_path)
    result = _cycle_result(stocks=[_stock()])
    enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)

    sent: list[str] = []

    async def _send(**kwargs):
        sent.append(kwargs["text"])
        return type("Msg", (), {"message_id": 1})()

    asyncio.run(deliver_cycle(outbox, result, [111], send=_send, limit=3500,
                              analysis_time=FROZEN_NOW))
    asyncio.run(deliver_cycle(outbox, result, [111], send=_send, limit=3500,
                              analysis_time=FROZEN_NOW))
    assert len(sent) == 1


def test_no_new_cycle_does_not_overwrite_pending_option_payload(tmp_path):
    """A later no-new cycle must not destroy an earlier pending payload."""
    outbox = _outbox(tmp_path)
    option = _option()
    option_key = identity.option_delivery_key(111, option)
    outbox.enqueue(delivery_key=option_key, recipient=111, kind="option",
                   payload=option, message_text="option alert", run_id="run-a")

    no_new = _cycle_result(stocks=[], options=[], stock_status=stock_results.STATUS_NO_NEW,
                           options_status="no_new_signals")
    enqueue_cycle(outbox, no_new, [111], limit=3500, analysis_time=FROZEN_NOW)

    assert outbox.get_by_key(option_key) is not None
    assert outbox.get_by_key(option_key).status == STATUS_PENDING


def test_outbox_schema_is_additive_and_rerunnable(tmp_path):
    path = tmp_path / "outbox.db"
    factory = sqlite_factory(path)
    conn = factory()
    from tradingview.scheduler.outbox import ensure_schema

    ensure_schema(conn)
    ensure_schema(conn)  # idempotent
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "notification_outbox" in tables


def test_outbox_migration_preserves_existing_rows(tmp_path):
    path = tmp_path / "outbox.db"
    outbox = NotificationOutbox(sqlite_factory(path), clock=FakeClock())
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    # Re-opening runs ensure_schema again; the row must survive.
    reopened = NotificationOutbox(sqlite_factory(path), clock=FakeClock())
    assert len(reopened.all_items()) == 1


def test_counts_and_pending_count_are_reported(tmp_path):
    outbox = _outbox(tmp_path)
    outbox.enqueue(delivery_key="a", recipient=111, kind="stock", payload={}, message_text="m")
    outbox.enqueue(delivery_key="b", recipient=111, kind="stock", payload={}, message_text="m")
    item = outbox.claim_due("w", limit=1)[0]
    outbox.mark_sent(item.id, 1, owner=item.claim_owner, claim_token=item.claim_token)
    counts = outbox.counts()
    assert counts.get(STATUS_SENT) == 1
    assert counts.get(STATUS_PENDING) == 1
    assert outbox.pending_count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# Issue 5 — safe formatting and fallback
# ══════════════════════════════════════════════════════════════════════════════

HOSTILE_TEXT = (
    "run_id does not match current run_ [brackets] `backticks` & <angle> "
    "under_scores /Users/priya/stocks/reports/options_signals.json "
    "emoji 🚀 and 日本語"
)


def test_dynamic_text_is_html_escaped():
    escaped = formatting.escape_html(HOSTILE_TEXT)
    assert "&amp;" in escaped
    assert "&lt;angle&gt;" in escaped
    assert "<angle>" not in escaped


def test_error_note_with_run_id_renders_safely():
    parts = formatting.render_summary(
        header="ALERT SUMMARY",
        stock_lines=["no stocks"],
        option_lines=["no options"],
        notes=[HOSTILE_TEXT],
    )
    html_text = "\n".join(part.html_text for part in parts)
    assert "run_id" in html_text
    assert "&lt;angle&gt;" in html_text
    # No unescaped angle brackets from dynamic content.
    assert "<angle>" not in html_text


def test_every_part_is_within_the_safe_limit():
    long_note = "x" * 20_000
    parts = formatting.render_summary(
        header="ALERT SUMMARY",
        stock_lines=["line " + "y" * 200 for _ in range(200)],
        option_lines=["opt " + "z" * 200 for _ in range(200)],
        notes=[long_note],
        limit=3500,
    )
    assert len(parts) > 1
    for part in parts:
        assert part.length(formatting.PARSE_MODE_HTML) <= 3500
        assert part.length(formatting.PARSE_MODE_PLAIN) <= 3500


def test_single_oversized_line_is_split():
    parts = formatting.render_summary(
        header="H",
        stock_lines=["q" * 30_000],
        option_lines=[],
        limit=3500,
    )
    assert len(parts) > 1
    for part in parts:
        assert part.length(formatting.PARSE_MODE_HTML) <= 3500


def test_preformatted_blocks_stay_balanced_per_part():
    parts = formatting.render_summary(
        header="H",
        stock_lines=["row " + "a" * 300 for _ in range(100)],
        option_lines=[],
        limit=3500,
    )
    for part in parts:
        assert part.html_text.count("<pre>") == part.html_text.count("</pre>")


def test_telegram_length_counts_utf16_units():
    assert formatting.telegram_length("abc") == 3
    assert formatting.telegram_length("🚀") == 2


def test_formatting_rejection_falls_back_to_plain_text():
    bot = FakeBot(reject_html=True)
    parts = formatting.render_summary(
        header="H", stock_lines=["s"], option_lines=["o"], limit=3500,
    )
    receipts = asyncio.run(formatting.send_parts(bot, 111, parts))
    assert all(receipt.delivered for receipt in receipts)
    assert any(call.get("parse_mode") is None for call in bot.calls)


def test_permission_rejection_does_not_retry_as_plain_text():
    bot = FakeBot(reject_all=True)
    parts = formatting.render_summary(
        header="H", stock_lines=["s"], option_lines=["o"], limit=3500,
    )
    receipts = asyncio.run(formatting.send_parts(bot, 111, parts))
    assert all(not receipt.delivered for receipt in receipts)
    assert all(receipt.error_class == formatting.ERROR_PERMISSION for receipt in receipts)
    # Exactly one attempt per part: no plain-text retry for a permission error.
    assert len(bot.calls) == len(parts)


def test_error_classification():
    assert formatting.classify_send_error(
        RuntimeError("Bad Request: can't parse entities")
    ) == formatting.ERROR_FORMATTING
    assert formatting.classify_send_error(
        RuntimeError("Forbidden: bot was blocked by the user")
    ) == formatting.ERROR_PERMISSION
    assert formatting.classify_send_error(
        RuntimeError("TimedOut")
    ) == formatting.ERROR_TRANSIENT


def test_retry_after_is_extracted():
    class _Exc(Exception):
        retry_after = 42

    assert formatting.retry_after_seconds(_Exc()) == 42.0
    assert formatting.retry_after_seconds(RuntimeError("Retry after 17")) == 17.0
    assert formatting.retry_after_seconds(RuntimeError("nope")) is None


def test_partial_multi_part_success_retries_only_the_failed_part():
    parts = formatting.build_parts(
        [formatting.Block(lines=["a" * 3000]), formatting.Block(lines=["b" * 3000])],
        limit=3500,
    )
    assert len(parts) == 2

    calls: list[str] = []

    async def _send(**kwargs):
        calls.append(kwargs["text"])
        if "b" * 100 in kwargs["text"]:
            raise RuntimeError("Bad Request: can't parse entities")
        return type("Msg", (), {"message_id": 1})()

    receipts = asyncio.run(formatting.send_parts(None, 111, parts, send=_send))
    assert receipts[0].delivered
    assert not receipts[1].delivered


def test_synthetic_token_is_redacted_in_error_text():
    parts = formatting.render_summary(
        header="H", stock_lines=[], option_lines=[],
        notes=[f"auth failed for {SYNTHETIC_TOKEN}"],
    )
    text = "\n".join(part.plain_text for part in parts)
    assert SYNTHETIC_TOKEN not in text
    assert "<redacted-token>" in text


def test_message_limit_constant_matches_telegram():
    assert TELEGRAM_MESSAGE_LIMIT == 4096
    assert formatting.TELEGRAM_SAFE_LIMIT < TELEGRAM_MESSAGE_LIMIT


# ══════════════════════════════════════════════════════════════════════════════
# Issue 10 — produce once, deliver to all recipients
# ══════════════════════════════════════════════════════════════════════════════

def test_two_recipients_get_identical_payloads(tmp_path):
    outbox = _outbox(tmp_path)
    result = _cycle_result(stocks=[_stock()], options=[_option()])
    enqueue_cycle(outbox, result, [111, 222], limit=3500, analysis_time=FROZEN_NOW)

    bot = FakeBot()
    asyncio.run(deliver_cycle(outbox, result, [111, 222], bot=bot, limit=3500,
                              analysis_time=FROZEN_NOW))

    texts_by_chat: dict[int, list[str]] = {}
    for call in bot.calls:
        texts_by_chat.setdefault(call["chat_id"], []).append(call["text"])
    assert set(texts_by_chat) == {111, 222}
    assert texts_by_chat[111] == texts_by_chat[222]


def test_zero_recipients_enqueues_nothing(tmp_path):
    outbox = _outbox(tmp_path)
    result = _cycle_result(stocks=[_stock()])
    assert enqueue_cycle(outbox, result, [], limit=3500, analysis_time=FROZEN_NOW) == 0
    assert outbox.all_items() == []


def test_duplicate_recipients_do_not_duplicate_messages(tmp_path):
    outbox = _outbox(tmp_path)
    result = _cycle_result(stocks=[_stock()])
    enqueue_cycle(outbox, result, [111, 111, 111], limit=3500, analysis_time=FROZEN_NOW)
    assert len(outbox.all_items()) == 1


def test_cycle_lock_releases_after_failure(tmp_path):
    from tradingview.scheduler.cycle import CycleLock

    lock = CycleLock(tmp_path / "cycle.lock")
    assert lock.acquire("owner-a") is True
    lock.release()
    assert not (tmp_path / "cycle.lock").exists()

    assert lock.acquire("owner-b") is True
    lock.release()


def test_cycle_lock_blocks_a_concurrent_owner(tmp_path):
    from tradingview.scheduler.cycle import CycleLock

    first = CycleLock(tmp_path / "cycle.lock")
    second = CycleLock(tmp_path / "cycle.lock")
    assert first.acquire("owner-a") is True
    assert second.acquire("owner-b") is False
    first.release()
    assert second.acquire("owner-b") is True
    second.release()


def test_stale_cycle_lock_is_reclaimed(tmp_path):
    from tradingview.scheduler.cycle import CycleLock

    path = tmp_path / "cycle.lock"
    path.write_text(
        json.dumps({"owner": "dead-process", "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    lock = CycleLock(path, stale_seconds=60)
    assert lock.acquire("new-owner") is True
    lock.release()
