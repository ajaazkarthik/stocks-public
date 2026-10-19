"""Durable scanner-option delivery caps, identity, and recovery evidence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import json

from tradingview.scheduler import identity
from tradingview.scheduler.dispatch import reconcile_cycle_results
from tradingview.scheduler.outbox import (
    SCHEMA, STATUS_AMBIGUOUS, STATUS_EXPIRED, NotificationOutbox, sqlite_factory,
)


NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)


def scanner_option(*, strike=195, symbol="RTX", recipient=11, expires=None, cap=1, session="2026-10-19"):
    quote_time = (expires - timedelta(seconds=120)) if expires else datetime.fromisoformat(f"{session}T15:00:00+00:00")
    row = {
        "source": "scanner", "market": "US", "scope": "production",
        "exchange": "NYSE", "symbol": symbol,
        "option_type": "CALL", "strike": strike, "expiry": "2026-11-20",
        "session_date": session, "option_score": 75, "parent_signal_score": 80,
        "parent_setup_id": "setup-1", "parent_run_id": "parent-run",
        "analysis_run_id": "analysis-run", "strategy_modes": ["BALANCED"],
        "bid": 4.1, "ask": 4.2, "mid_price": 4.15, "premium_currency": "USD",
        "quote_provider": "fixture", "bid_quote_at": quote_time.isoformat(),
        "ask_quote_at": quote_time.isoformat(), "freshness_status": "verified_bid_ask",
        "quote_max_age_seconds": 120, "max_spread_pct": 10,
        "oi": 100, "volume": 10, "min_open_interest": 100, "min_volume": 10,
        "min_dte": 14, "max_dte": 45, "volume_session_date": session,
        "session_close_at": quote_time.replace(hour=20).isoformat(),
        "recommendation_expires_at": (expires or quote_time + timedelta(minutes=2)).isoformat(),
        "contract_identifier": f"quote-provider-id-{strike}",
    }
    return row


def reservation(row, cap=1):
    return {
        "market": "US", "symbol": row["symbol"], "session_date": row["session_date"],
        "cap": cap, "expires_at": row["recommendation_expires_at"], "recommendation": row,
    }


def enqueue(outbox, row, recipient=11, cap=1):
    key = identity.option_delivery_key(recipient, row)
    event = {
        "delivery_key": key, "recipient": recipient, "kind": identity.KIND_OPTION,
        "run_id": "new-run", "scanner_option_reservation": reservation(row, cap),
    }

    def build(remaining):
        if not remaining:
            return []
        return [{
            "delivery_key": f"part:{recipient}:{key}", "recipient": recipient,
            "kind": "summary", "run_id": "new-run",
            "payload": {"events": remaining, "plain_text": "scanner option"},
            "message_text": "scanner option",
        }]

    return outbox.enqueue_event_parts(
        recipient=recipient, events=[event], build_items=build,
        expires_at=outbox._now() + timedelta(hours=12), scanner_option_cap=cap,
    )


def create_outbox(path, clock=lambda: NOW):
    return NotificationOutbox(sqlite_factory(path), clock=clock, alert_ttl_hours=12)


def reservations(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT delivery_key,recipient,market,symbol,session_date,expires_at,released_at "
            "FROM scanner_option_reservations ORDER BY rowid"
        ).fetchall()


def test_scanner_identity_uses_actual_contract_and_ignores_mutable_recommendation_fields():
    one = scanner_option(strike=195)
    two = {
        **one, "strike": 195.0, "contract_identifier": "different-alias",
        "option_score": 93, "parent_signal_score": 70, "strategy_modes": ["FAST", "BALANCED"],
        "recommendation_expires_at": (NOW + timedelta(seconds=30)).isoformat(),
    }
    assert identity.option_delivery_key(11, one) == identity.option_delivery_key(11, two)
    assert identity.option_delivery_key(12, one) != identity.option_delivery_key(11, one)
    assert identity.option_delivery_key(11, one) != identity.option_delivery_key(
        11, {**one, "expiry": "2026-11-27"}
    )


def test_cap_is_atomic_and_idempotent_per_recipient_symbol_session(tmp_path):
    path = tmp_path / "outbox.db"
    outbox = create_outbox(path)
    assert enqueue(outbox, scanner_option()) == 1
    # Same identity across score/premium/run changes is a retry, not a second slot.
    changed = scanner_option()
    changed.update(option_score=99, parent_signal_score=62, strategy_modes=["FAST"])
    assert enqueue(outbox, changed) == 0
    assert enqueue(outbox, scanner_option(strike=200)) == 0
    assert len(outbox.all_items()) == 1
    rows = reservations(path)
    assert len(rows) == 1 and rows[0][1:5] == ("11", "US", "RTX", "2026-10-19")


def test_concurrent_contracts_cannot_both_take_last_symbol_slot(tmp_path):
    path = tmp_path / "outbox.db"
    workers = [create_outbox(path) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(
            lambda pair: enqueue(pair[0], scanner_option(strike=pair[1])),
            [(workers[0], 195), (workers[1], 200)],
        ))
    assert sorted(counts) == [0, 1]
    assert len(reservations(path)) == 1
    assert len(workers[0].all_items()) == 1


def test_cap_conflict_filters_only_that_option_and_keeps_stock_and_other_symbol(tmp_path):
    path = tmp_path / "outbox.db"
    outbox = create_outbox(path)
    assert enqueue(outbox, scanner_option()) == 1

    stock = {"delivery_key": "stock-event", "recipient": 11, "kind": "stock", "run_id": "r2"}
    withheld = scanner_option(strike=200)
    allowed = scanner_option(strike=205, symbol="MSFT")
    option_events = []
    for row in (withheld, allowed):
        option_events.append({
            "delivery_key": identity.option_delivery_key(11, row), "recipient": 11,
            "kind": identity.KIND_OPTION, "run_id": "r2",
            "scanner_option_reservation": reservation(row),
        })
    events = [stock, *option_events]
    captured = []

    def build(available):
        captured.extend(event["delivery_key"] for event in available)
        return [{
            "delivery_key": "mixed", "recipient": 11, "kind": "summary", "run_id": "r2",
            "payload": {"events": available, "plain_text": "valid rows"},
            "message_text": "valid rows",
        }]

    assert outbox.enqueue_event_parts(
        recipient=11, events=events, build_items=build,
        expires_at=NOW + timedelta(hours=12), scanner_option_cap=1,
    ) == 1
    assert "stock-event" in captured
    assert option_events[1]["delivery_key"] in captured
    assert option_events[0]["delivery_key"] not in captured
    assert len(reservations(path)) == 2


def test_invalid_stale_option_does_not_discard_valid_stock_event(tmp_path):
    path = tmp_path / "outbox.db"
    outbox = create_outbox(path)
    stock = {"delivery_key": "stock-valid", "recipient": 11, "kind": "stock", "run_id": "r2"}
    stale = scanner_option(strike=200)
    stale["bid_quote_at"] = (NOW - timedelta(minutes=5)).isoformat()
    stale["ask_quote_at"] = (NOW - timedelta(minutes=5)).isoformat()
    option_event = {
        "delivery_key": identity.option_delivery_key(11, stale), "recipient": 11,
        "kind": identity.KIND_OPTION, "run_id": "r2",
        "scanner_option_reservation": reservation(stale),
    }
    captured = []

    def build(available):
        captured.extend(event["delivery_key"] for event in available)
        return [{
            "delivery_key": "stock-only", "recipient": 11, "kind": "summary", "run_id": "r2",
            "payload": {"events": available, "plain_text": "stock"}, "message_text": "stock",
        }]

    assert outbox.enqueue_event_parts(
        recipient=11, events=[stock, option_event], build_items=build,
        expires_at=NOW + timedelta(hours=12), scanner_option_cap=1,
    ) == 1
    assert captured == ["stock-valid"]
    assert reservations(path) == []


def test_recovery_skips_expired_option_and_keeps_current_other_symbol(tmp_path):
    path = tmp_path / "outbox.db"
    outbox = create_outbox(path)
    stale = scanner_option(symbol="RTX")
    stale["bid_quote_at"] = (NOW - timedelta(minutes=5)).isoformat()
    stale["ask_quote_at"] = (NOW - timedelta(minutes=5)).isoformat()
    stale["recommendation_expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    valid = scanner_option(symbol="MSFT", strike=200)
    malformed = {**scanner_option(), "expiry": "broken"}
    run_dir = tmp_path / "runs" / "recovered-run"
    run_dir.mkdir(parents=True)
    (run_dir / "cycle_result.json").write_text(json.dumps({
        "run_id": "recovered-run", "started_at": NOW.isoformat(),
        "option_result": {"options": [malformed, stale, valid]},
    }), encoding="utf-8")

    assert reconcile_cycle_results(outbox, tmp_path / "runs", [11]) == 1
    assert len(outbox.all_items()) == 1
    assert "MSFT" in outbox.all_items()[0].message_text
    assert len(reservations(path)) == 1
    assert reservations(path)[0][3] == "MSFT"


def test_reservations_are_independent_by_recipient_and_session(tmp_path):
    path = tmp_path / "outbox.db"
    outbox = create_outbox(path)
    assert enqueue(outbox, scanner_option(strike=195), recipient=11) == 1
    assert enqueue(outbox, scanner_option(strike=200), recipient=12) == 1
    assert len(reservations(path)) == 2
    tomorrow = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    next_outbox = create_outbox(path, lambda: tomorrow)
    assert enqueue(next_outbox, scanner_option(strike=200, session="2026-10-20")) == 1
    assert len(reservations(path)) == 3


def test_restart_and_ambiguous_delivery_keep_daily_cap_reserved(tmp_path):
    path = tmp_path / "outbox.db"
    outbox = create_outbox(path)
    row = scanner_option()
    assert enqueue(outbox, row) == 1
    key = identity.option_delivery_key(11, row)
    # Restart/recovery sees the already-enqueued event and cannot create another part.
    restarted = create_outbox(path)
    assert enqueue(restarted, {**row, "option_score": 80}) == 0
    item = restarted.claim_due("worker")[0]
    assert restarted.mark_ambiguous(item.id, "transport timed out", owner="worker",
                                    claim_token=item.claim_token) == STATUS_AMBIGUOUS
    assert enqueue(restarted, scanner_option(strike=200)) == 0
    assert len(reservations(path)) == 1
    assert restarted.event_delivered(key) is False


def test_expired_never_claimed_reservation_is_marked_released_and_reusable(tmp_path):
    path = tmp_path / "outbox.db"
    now = [NOW]
    outbox = create_outbox(path, lambda: now[0])
    row = scanner_option(expires=NOW + timedelta(seconds=30))
    assert enqueue(outbox, row) == 1
    now[0] += timedelta(seconds=31)
    assert outbox.expire_overdue() == 1
    assert outbox.all_items()[0].status == STATUS_EXPIRED
    assert enqueue(outbox, scanner_option(strike=200, expires=now[0] + timedelta(minutes=2))) == 1
    history = reservations(path)
    assert len(history) == 2
    assert history[0][-1] is not None
    assert history[1][-1] is None


def test_expired_claimed_reservation_is_never_released(tmp_path):
    path = tmp_path / "outbox.db"
    now = [NOW]
    outbox = create_outbox(path, lambda: now[0])
    assert enqueue(outbox, scanner_option(expires=NOW + timedelta(seconds=30))) == 1
    item = outbox.claim_due("worker")[0]
    assert outbox.renew_claim(item.id, owner="worker", claim_token=item.claim_token)
    now[0] += timedelta(seconds=31)
    assert outbox.expire_overdue() == 1
    assert enqueue(outbox, scanner_option(strike=200, expires=now[0] + timedelta(minutes=2))) == 0
    assert reservations(path)[0][-1] is None
    assert outbox.get(item.id).first_send_started_at == NOW.isoformat()


def test_additive_schema_upgrade_preserves_existing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        old_schema = SCHEMA.replace("    sent_at            TEXT,\n    first_send_started_at TEXT",
                                    "    sent_at            TEXT")
        for statement in old_schema.split(";"):
            if statement.strip():
                conn.execute(statement)
        assert "first_send_started_at" not in {
            row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)")}
        conn.execute(
            "INSERT INTO notification_outbox "
            "(delivery_key,recipient,kind,run_id,payload_json,message_text,parse_mode,status,"
            "attempt_count,max_attempts,next_attempt_at,created_at,updated_at) "
            "VALUES ('historical-row','11','option','','{}','historical','HTML','sent',0,5,?,?,?)",
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
    outbox = create_outbox(path)
    outbox.counts()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT delivery_key FROM notification_outbox WHERE id=1").fetchone()[0] == "historical-row"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)")}
        assert "first_send_started_at" in columns
        assert conn.execute("SELECT COUNT(*) FROM scanner_option_reservations").fetchone()[0] == 0


def test_claim_crash_before_transport_can_release_expired_capacity(tmp_path):
    path = tmp_path / "outbox.db"
    now = [NOW]
    box = create_outbox(path, lambda: now[0])
    assert enqueue(box, scanner_option(expires=NOW + timedelta(seconds=30))) == 1
    assert box.claim_due("crashed-before-send")
    now[0] += timedelta(seconds=31)
    restarted = create_outbox(path, lambda: now[0])
    assert enqueue(restarted, scanner_option(strike=200, expires=now[0] + timedelta(seconds=120))) == 1
    assert reservations(path)[0][-1] is not None
