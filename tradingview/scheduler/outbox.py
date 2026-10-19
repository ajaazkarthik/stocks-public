"""Durable notification outbox with atomic claims and bounded retries.

Scoring completion and notification delivery are **separate durable states**.
A completed score is not a delivered alert, and a failed Telegram send must not
erase the scoring result. This module owns the delivery half.

Guarantees
----------
* ``delivery_key`` is ``UNIQUE``, so an ordinary duplicate enqueue is a no-op.
* Claims are atomic (``BEGIN IMMEDIATE`` + ``UPDATE … RETURNING``), so two
  dispatchers cannot both send the same row.
* A claim carries a lease. If a worker crashes, the lease expires and the row
  becomes claimable again.
* Retries are bounded with exponential backoff and honour Telegram
  ``retry_after``. Terminal states are explicit.

Documented limitation
---------------------
Delivery is **at-least-once**, not exactly-once. A network timeout after
Telegram accepted a request cannot be resolved by a local unique constraint:
the row is recorded as ``ambiguous`` and may be retried, so a duplicate is
possible. The outbox never claims a message was delivered without an
acknowledgement (a returned ``message_id``).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from tradingview.scheduler.config import (
    DEFAULT_ALERT_TTL_HOURS,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_CLAIM_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    redact,
)
from tradingview.scheduler.timeutil import parse_iso, to_utc, utc_now

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_AMBIGUOUS = "ambiguous"


class _ReservationConflict(Exception):
    pass

#: Statuses that still require work.
OPEN_STATUSES = frozenset({STATUS_PENDING, STATUS_CLAIMED, STATUS_AMBIGUOUS})

#: Statuses that will never be retried.
TERMINAL_STATUSES = frozenset({STATUS_SENT, STATUS_FAILED, STATUS_EXPIRED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_outbox (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_key       TEXT    NOT NULL UNIQUE,
    recipient          TEXT    NOT NULL,
    kind               TEXT    NOT NULL,
    run_id             TEXT    NOT NULL DEFAULT '',
    payload_json       TEXT    NOT NULL,
    message_text       TEXT    NOT NULL,
    parse_mode         TEXT    NOT NULL DEFAULT 'HTML',
    status             TEXT    NOT NULL DEFAULT 'pending',
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL DEFAULT 5,
    next_attempt_at    TEXT    NOT NULL,
    claim_owner        TEXT,
    claim_token        TEXT,
    claim_expires_at   TEXT,
    last_error         TEXT,
    telegram_message_id INTEGER,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    expires_at         TEXT,
    sent_at            TEXT,
    first_send_started_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON notification_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_recipient
    ON notification_outbox(recipient, status);
CREATE INDEX IF NOT EXISTS idx_outbox_run
    ON notification_outbox(run_id);
CREATE TABLE IF NOT EXISTS delivered_events (
    delivery_key        TEXT    PRIMARY KEY,
    recipient           TEXT    NOT NULL,
    kind                TEXT    NOT NULL,
    run_id              TEXT    NOT NULL DEFAULT '',
    delivered_at        TEXT    NOT NULL,
    telegram_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_delivered_recipient
    ON delivered_events(recipient, kind);
CREATE TABLE IF NOT EXISTS notification_events (
    delivery_key TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS notification_event_parts (
    item_id INTEGER NOT NULL,
    delivery_key TEXT NOT NULL,
    PRIMARY KEY (item_id, delivery_key)
);
CREATE INDEX IF NOT EXISTS idx_event_parts_key
    ON notification_event_parts(delivery_key);
CREATE TABLE IF NOT EXISTS notification_migrations (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS scanner_option_reservations (
    delivery_key TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    session_date TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_scanner_option_cap
    ON scanner_option_reservations(recipient, market, symbol, session_date, released_at);

"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive migration, including conservative membership for older rows.

    Legacy payloads attached full-cycle events to every part. Retaining every
    membership requires all those parts to succeed before recording an event.
    Existing false historical ledger entries cannot be inferred or repaired here.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)")}
        if "claim_token" not in columns:
            conn.execute("ALTER TABLE notification_outbox ADD COLUMN claim_token TEXT")
        if "first_send_started_at" not in columns:
            conn.execute("ALTER TABLE notification_outbox ADD COLUMN first_send_started_at TEXT")
        migrated = conn.execute(
            "SELECT 1 FROM notification_migrations WHERE name = 'event-membership-v1'"
        ).fetchone()
        if migrated:
            return
        for row in conn.execute(
            "SELECT id, recipient, run_id, expires_at, payload_json FROM notification_outbox"
        ).fetchall():
            try:
                payload = json.loads(row[4])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            for event in payload.get("events") or []:
                if not isinstance(event, dict) or not event.get("delivery_key"):
                    continue
                key = str(event["delivery_key"])
                conn.execute(
                    "INSERT OR IGNORE INTO notification_events "
                    "(delivery_key, recipient, kind, run_id, expires_at) VALUES (?,?,?,?,?)",
                    (key, str(row[1]), str(event.get("kind") or ""), str(row[2]), row[3]),
                )
                # The earliest deadline wins when legacy parts disagree.
                if row[3]:
                    conn.execute(
                        "UPDATE notification_events SET expires_at = ? WHERE delivery_key = ? "
                        "AND (expires_at IS NULL OR julianday(expires_at) > julianday(?))",
                        (row[3], key, row[3]),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO notification_event_parts VALUES (?,?)", (row[0], key)
                )
        conn.execute("INSERT INTO notification_migrations VALUES ('event-membership-v1')")


def _iso(moment: datetime) -> str:
    return to_utc(moment).isoformat()


@dataclass
class OutboxItem:
    """One queued notification."""

    id: int
    delivery_key: str
    recipient: str
    kind: str
    run_id: str
    payload: dict
    message_text: str
    parse_mode: str
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: str
    claim_owner: str | None
    claim_expires_at: str | None
    last_error: str | None
    telegram_message_id: int | None
    created_at: str
    updated_at: str
    expires_at: str | None
    sent_at: str | None
    claim_token: str | None = None
    first_send_started_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OutboxItem":
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        return cls(
            id=row["id"],
            delivery_key=row["delivery_key"],
            recipient=row["recipient"],
            kind=row["kind"],
            run_id=row["run_id"],
            payload=payload if isinstance(payload, dict) else {},
            message_text=row["message_text"],
            parse_mode=row["parse_mode"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            next_attempt_at=row["next_attempt_at"],
            claim_owner=row["claim_owner"],
            claim_expires_at=row["claim_expires_at"],
            last_error=row["last_error"],
            telegram_message_id=row["telegram_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            sent_at=row["sent_at"],
            claim_token=row["claim_token"],
            first_send_started_at=row["first_send_started_at"],
        )

    @property
    def recipient_id(self) -> int | None:
        text = str(self.recipient).strip()
        return int(text) if text.lstrip("-").isdigit() else None


class NotificationOutbox:
    """SQLite-backed durable notification queue.

    The connection factory is injected so tests can point at a temporary
    database and so production code can reuse the shared ``db_factory``.
    """

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        clock: Callable[[], datetime] = utc_now,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
        claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
        alert_ttl_hours: float = DEFAULT_ALERT_TTL_HOURS,
    ) -> None:
        self._conn_factory = conn_factory
        self._clock = clock
        self.max_attempts = int(max_attempts)
        self.backoff_base_seconds = float(backoff_base_seconds)
        self.backoff_cap_seconds = float(backoff_cap_seconds)
        self.claim_lease_seconds = float(claim_lease_seconds)
        self.alert_ttl_hours = float(alert_ttl_hours)

    # ── connection helpers ────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = self._conn_factory()
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        return conn

    def _now(self) -> datetime:
        return to_utc(self._clock())

    # ── enqueue ───────────────────────────────────────────────────────────
    def _deadline(self, expires_at, ttl_hours=None) -> str | None:
        if expires_at is not None:
            return _iso(expires_at if isinstance(expires_at, datetime) else parse_iso(expires_at))
        ttl = self.alert_ttl_hours if ttl_hours is None else float(ttl_hours)
        return _iso(self._now() + timedelta(hours=ttl)) if ttl > 0 else None

    def _insert_item(self, conn, *, delivery_key, recipient, kind, payload,
                     message_text, run_id="", parse_mode="HTML", expires_at=None,
                     ttl_hours=None, max_attempts=None) -> int | None:
        now_iso = _iso(self._now())
        deadline = self._deadline(expires_at, ttl_hours)
        if deadline is not None and deadline <= now_iso:
            return None
        cursor = conn.execute(
            "INSERT OR IGNORE INTO notification_outbox "
            "(delivery_key, recipient, kind, run_id, payload_json, message_text, parse_mode, "
            "status, attempt_count, max_attempts, next_attempt_at, created_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(delivery_key), str(recipient), str(kind), str(run_id),
             json.dumps(payload, default=str, sort_keys=True), str(message_text), str(parse_mode),
             STATUS_PENDING, 0, int(max_attempts or self.max_attempts), now_iso, now_iso,
             now_iso, deadline),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def enqueue(self, *, delivery_key: str, recipient: Any, kind: str, payload: dict,
                message_text: str, run_id: str = "", parse_mode: str = "HTML",
                ttl_hours: float | None = None, max_attempts: int | None = None,
                expires_at: datetime | str | None = None,
                scanner_option_cap: int | None = None) -> bool:
        """Queue one part, rejecting a duplicate reservation or expired work.

        Multipart producers must use ``enqueue_event_parts`` to reserve all
        carriers together. A pre-rendered body cannot safely filter its events;
        this single-part API rejects the whole body if any event is reserved.
        """
        events = payload.get("events") or []
        item = dict(delivery_key=delivery_key, recipient=recipient, kind=kind,
                    payload=payload, message_text=message_text, run_id=run_id,
                    parse_mode=parse_mode, max_attempts=max_attempts)
        if events:
            def build(available):
                return [item] if len(available) == len(events) else []
            return bool(self.enqueue_event_parts(recipient=recipient, events=events,
                        build_items=build, expires_at=expires_at, ttl_hours=ttl_hours,
                        scanner_option_cap=scanner_option_cap))
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                return self._insert_item(conn, **item, expires_at=expires_at,
                                         ttl_hours=ttl_hours) is not None
        finally:
            conn.close()

    def enqueue_event_parts(self, *, recipient: Any, events: Iterable[dict],
                            build_items: Callable[[list[dict]], Iterable[dict]],
                            expires_at: datetime | str | None = None,
                            ttl_hours: float | None = None,
                            scanner_option_cap: int | None = None) -> int:
        """Filter, render, and reserve recipient/event work in one transaction.

        ``build_items`` receives available identities and returns enqueue kwargs
        whose ``payload.events`` names exactly the events present in each part.
        An event may occur in multiple parts; every carrier must acknowledge it.
        Terminal failures retain reservations so recovery cannot reset attempts.
        Deadlines persist for each event and can never be extended by recovery.
        Callback/insert errors roll back the complete batch.
        """
        recipient = str(recipient)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                deadline = self._deadline(expires_at, ttl_hours)
                now_iso = _iso(self._now())
                available = []
                deadlines = {}
                reservations = {}
                for event in events:
                    key = str(event.get("delivery_key") or "")
                    if not key or key in deadlines:
                        continue
                    if str(event.get("recipient", recipient)) != recipient:
                        raise ValueError("event recipient does not match reservation")
                    if conn.execute("SELECT 1 FROM delivered_events WHERE delivery_key = ?", (key,)).fetchone():
                        continue
                    old = conn.execute("SELECT expires_at FROM notification_events WHERE delivery_key = ?", (key,)).fetchone()
                    event_deadline = old[0] if old is not None else deadline
                    reservation = event.get("scanner_option_reservation")
                    if reservation is not None:
                        try:
                            if not isinstance(reservation, dict):
                                raise ValueError("scanner option reservation must be an object")
                            recommendation = reservation.get("recommendation")
                            if not isinstance(recommendation, dict):
                                raise ValueError("scanner option recommendation evidence is required")
                            recommendation = dict(recommendation)
                            if not recommendation.get("option_type") and recommendation.get("opt_type"):
                                recommendation["option_type"] = recommendation["opt_type"]
                            reservation["recommendation"] = recommendation
                            from tradingview.scheduler.us_options_policy import validate_persisted_scanner_record
                            validate_persisted_scanner_record(recommendation, self._now())
                            market = str(reservation.get("market") or "").strip().upper()
                            symbol = str(reservation.get("symbol") or "").strip().upper()
                            session_date = str(reservation.get("session_date") or "").strip()
                            date.fromisoformat(session_date)
                            cap = reservation.get("cap")
                            if cap is None:
                                cap = scanner_option_cap
                            if type(cap) is not int or cap <= 0 or cap > 20:
                                raise ValueError("scanner option reservation cap must be an integer in 1..20")
                            if market != "US" or not symbol or ":" in symbol:
                                raise ValueError("scanner option reservation requires US market and canonical symbol")
                            if (str(recommendation.get("market") or "").upper() != market
                                    or str(recommendation.get("symbol") or "").upper() != symbol
                                    or str(recommendation.get("session_date") or "") != session_date):
                                raise ValueError("scanner option reservation disagrees with recommendation identity")
                            from tradingview.scheduler.identity import option_delivery_key
                            if option_delivery_key(recipient, recommendation) != key:
                                raise ValueError("scanner option delivery key disagrees with contract identity")
                            raw_expiry = reservation.get("expires_at")
                            if raw_expiry is None or str(raw_expiry).strip() == "":
                                raise ValueError("scanner option reservation requires original quote expiry")
                            original_expiry = _iso(
                                raw_expiry if isinstance(raw_expiry, datetime) else parse_iso(raw_expiry)
                            )
                            recommendation_expiry = _iso(parse_iso(
                                recommendation.get("recommendation_expires_at")
                            ))
                            original_expiry = min(original_expiry, recommendation_expiry)
                        except (TypeError, ValueError):
                            # A bad or expired option must not discard an
                            # independently valid stock or other contract.
                            continue
                        event_deadline = min(
                            [value for value in (event_deadline, deadline, original_expiry) if value is not None]
                        )
                        reservations[key] = {
                            "recipient": recipient, "market": market, "symbol": symbol,
                            "session_date": session_date, "cap": cap,
                            "expires_at": event_deadline,
                        }
                    if event_deadline is not None and event_deadline <= now_iso:
                        continue
                    if conn.execute("SELECT 1 FROM notification_event_parts WHERE delivery_key = ?", (key,)).fetchone():
                        continue
                    available.append(dict(event))
                    deadlines[key] = event_deadline
                if not available:
                    return 0

                # Release eligible historical reservations, then filter only
                # scanner contracts that exceed their symbol/session cap.
                # Other options and valid stock events in the same cycle remain
                # independently deliverable.
                self._release_provably_unsent_reservations(conn, now_iso)
                allowed = []
                allowed_deadlines = {}
                allowed_reservations = {}
                by_group = {}
                for event in available:
                    key = str(event["delivery_key"])
                    reservation = reservations.get(key)
                    if reservation is None:
                        allowed.append(event)
                        allowed_deadlines[key] = deadlines[key]
                        continue
                    group = (reservation["recipient"], reservation["market"], reservation["symbol"],
                             reservation["session_date"])
                    by_group.setdefault(group, []).append((event, reservation))

                for group, candidates in by_group.items():
                    recipient_id, market, symbol, session_date = group
                    active = int(conn.execute(
                        "SELECT COUNT(*) FROM scanner_option_reservations "
                        "WHERE recipient = ? AND market = ? AND symbol = ? AND session_date = ? "
                        "AND released_at IS NULL",
                        group,
                    ).fetchone()[0])
                    cap = min(item[1]["cap"] for item in candidates)
                    remaining = max(0, cap - active)
                    for event, reservation in candidates:
                        key = str(event["delivery_key"])
                        prior = conn.execute(
                            "SELECT released_at FROM scanner_option_reservations WHERE delivery_key = ?",
                            (key,),
                        ).fetchone()
                        if prior is not None:
                            if prior[0] is None:
                                # Existing durable reservation: ordinary retry.
                                allowed.append(event)
                                allowed_deadlines[key] = deadlines[key]
                                allowed_reservations[key] = reservation
                            continue
                        if remaining <= 0:
                            continue
                        remaining -= 1
                        allowed.append(event)
                        allowed_deadlines[key] = deadlines[key]
                        allowed_reservations[key] = reservation

                if not allowed:
                    return 0
                items = list(build_items(allowed))
                if not items:
                    return 0
                expected = set(allowed_deadlines)
                memberships = set()
                for item in items:
                    if str(item.get("recipient")) != recipient:
                        raise ValueError("part recipient does not match reservation")
                    part_events = item.get("payload", {}).get("events") or []
                    keys = {str(event.get("delivery_key") or "") for event in part_events}
                    if not keys <= expected:
                        raise ValueError("part references an unreserved event")
                    memberships.update(keys)
                if memberships != expected:
                    raise ValueError("reserved event missing from rendered parts")

                for key, reservation in allowed_reservations.items():
                    already = conn.execute(
                        "SELECT released_at FROM scanner_option_reservations WHERE delivery_key = ?",
                        (key,),
                    ).fetchone()
                    if already is not None:
                        if already[0] is not None:
                            raise ValueError("scanner option identity was previously released")
                        continue
                    group = (reservation["recipient"], reservation["market"], reservation["symbol"],
                             reservation["session_date"])
                    # Capacity was computed under BEGIN IMMEDIATE. Since this
                    # connection holds the write lock, the checked slot remains
                    # available through event and message insertion.
                    conn.execute(
                        "INSERT INTO scanner_option_reservations "
                        "(delivery_key, recipient, market, symbol, session_date, expires_at, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (key, reservation["recipient"], reservation["market"], reservation["symbol"],
                         reservation["session_date"], reservation["expires_at"], now_iso),
                    )
                memberships = set()
                queued = 0
                for item in items:
                    item = dict(item)
                    if str(item.get("recipient")) != recipient:
                        raise ValueError("part recipient does not match reservation")
                    part_events = item.get("payload", {}).get("events") or []
                    keys = {str(event.get("delivery_key") or "") for event in part_events}
                    if not keys <= allowed_deadlines.keys():
                        raise ValueError("part references an unreserved event")
                    part_deadlines = [deadlines[key] for key in keys if deadlines[key] is not None]
                    item_deadline = item.pop("expires_at", None)
                    if item_deadline is not None:
                        part_deadlines.append(self._deadline(item_deadline))
                    effective = min(part_deadlines) if part_deadlines else deadline
                    item.pop("ttl_hours", None)
                    item_id = self._insert_item(conn, **item, expires_at=effective,
                                                ttl_hours=ttl_hours)
                    if item_id is None:
                        # Never leave a subset of a logical multipart batch.
                        raise _ReservationConflict()
                    queued += 1
                    for event in part_events:
                        key = str(event["delivery_key"])
                        conn.execute("INSERT OR IGNORE INTO notification_events "
                                     "(delivery_key, recipient, kind, run_id, expires_at) VALUES (?,?,?,?,?)",
                                     (key, recipient, str(event.get("kind") or ""),
                                      str(event.get("run_id") or item.get("run_id") or ""), deadlines[key]))
                        conn.execute("INSERT INTO notification_event_parts VALUES (?,?)", (item_id, key))
                        memberships.add(key)
                return queued
        except _ReservationConflict:
            return 0
        finally:
            conn.close()

    def enqueue_many(self, items: Iterable[dict]) -> int:
        """Queue independent notifications; multipart work uses enqueue_event_parts."""
        return sum(self.enqueue(**item) for item in items)

    # ── claims ────────────────────────────────────────────────────────────
    def claim_due(
        self,
        owner: str,
        *,
        limit: int = 10,
        kinds: Iterable[str] | None = None,
    ) -> list[OutboxItem]:
        """Atomically claim up to *limit* due notifications for *owner*.

        Expired leases from crashed workers are reclaimed here. Rows past their
        freshness deadline are moved to ``expired`` instead of being sent.
        """
        kind_filter = tuple(kinds) if kinds else ()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_iso = _iso(now)
            lease_iso = _iso(now + timedelta(seconds=self.claim_lease_seconds))
            self._expire_stale(conn, now_iso)
            placeholders = ""
            if kind_filter:
                placeholders = f" AND kind IN ({','.join('?' for _ in kind_filter)})"
            rows = conn.execute(
                f"""
                UPDATE notification_outbox
                   SET status = ?, claim_owner = ?, claim_token = ?, claim_expires_at = ?,
                       updated_at = ?
                 WHERE id IN (
                       SELECT id FROM notification_outbox
                        WHERE next_attempt_at <= ?
                          AND (
                                status IN (?, ?)
                             OR (status = ? AND claim_expires_at IS NOT NULL
                                 AND claim_expires_at <= ?)
                          )
                          {placeholders}
                        ORDER BY next_attempt_at ASC, id ASC
                        LIMIT ?
                 )
                RETURNING *
                """,
                (
                    STATUS_CLAIMED, owner, uuid.uuid4().hex, lease_iso, now_iso,
                    now_iso,
                    STATUS_PENDING, STATUS_AMBIGUOUS,
                    STATUS_CLAIMED, now_iso,
                    *kind_filter, int(limit),
                ),
            ).fetchall()
            conn.commit()
            return [OutboxItem.from_row(row) for row in rows]
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_by_keys(self, owner: str, keys: Iterable[str]) -> list[OutboxItem]:
        """Atomically claim specific delivery keys for *owner*.

        Used by the direct-delivery path so a second dispatcher cannot send a
        row that is already claimed or acknowledged. Rows whose scheduled
        ``next_attempt_at`` has not yet elapsed are **not** claimed, so a
        backoff is honoured on every claim path.
        """
        key_list = [str(key) for key in keys]
        if not key_list:
            return []
        placeholders = ",".join("?" for _ in key_list)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_iso = _iso(now)
            lease_iso = _iso(now + timedelta(seconds=self.claim_lease_seconds))
            self._expire_stale(conn, now_iso)
            rows = conn.execute(
                f"""
                UPDATE notification_outbox
                   SET status = ?, claim_owner = ?, claim_token = ?, claim_expires_at = ?,
                       updated_at = ?
                 WHERE delivery_key IN ({placeholders})
                   AND next_attempt_at <= ?
                   AND (
                        status IN (?, ?)
                     OR (status = ? AND claim_expires_at IS NOT NULL
                         AND claim_expires_at <= ?)
                   )
                RETURNING *
                """,
                (
                    STATUS_CLAIMED, owner, uuid.uuid4().hex, lease_iso, now_iso,
                    *key_list,
                    now_iso,
                    STATUS_PENDING, STATUS_AMBIGUOUS, STATUS_CLAIMED, now_iso,
                ),
            ).fetchall()
            conn.commit()
            return [OutboxItem.from_row(row) for row in rows]
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_claim(self, item_id: int, *, owner: str | None = None,
                    claim_token: str | None = None) -> bool:
        """Check and extend a live claim immediately before beginning a send.

        This cannot revive an expired lease or freshness deadline. Sequential
        batch senders use it to avoid starting later rows with a stale claim.
        """
        if owner is None or claim_token is None:
            return False
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                now = self._now()
                now_iso = _iso(now)
                lease_iso = _iso(now + timedelta(seconds=self.claim_lease_seconds))
                current = conn.execute(
                    "SELECT payload_json FROM notification_outbox WHERE id = ? AND status = ? "
                    "AND claim_owner = ? AND claim_token = ? AND claim_expires_at > ? "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (int(item_id), STATUS_CLAIMED, str(owner), str(claim_token), now_iso, now_iso),
                ).fetchone()
                if current is None:
                    return False
                try:
                    payload = json.loads(current["payload_json"])
                except (TypeError, ValueError):
                    payload = {}
                try:
                    for event in (payload.get("events") or []) if isinstance(payload, dict) else []:
                        reservation = event.get("scanner_option_reservation") if isinstance(event, dict) else None
                        if reservation is None:
                            continue
                        recommendation = reservation.get("recommendation") if isinstance(reservation, dict) else None
                        if not isinstance(recommendation, dict):
                            raise ValueError("scanner option quote evidence is missing")
                        recommendation = dict(recommendation)
                        if not recommendation.get("option_type") and recommendation.get("opt_type"):
                            recommendation["option_type"] = recommendation["opt_type"]
                        from tradingview.scheduler.us_options_policy import validate_persisted_scanner_record
                        validate_persisted_scanner_record(recommendation, now)
                except (TypeError, ValueError) as exc:
                    conn.execute(
                        "UPDATE notification_outbox SET status = ?, updated_at = ?, "
                        "last_error = ?, claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL "
                        "WHERE id = ? AND status = ? AND claim_owner = ? AND claim_token = ?",
                        (STATUS_EXPIRED, now_iso, redact(str(exc))[:500], int(item_id), STATUS_CLAIMED,
                         str(owner), str(claim_token)),
                    )
                    return False
                cursor = conn.execute(
                    "UPDATE notification_outbox SET claim_expires_at = "
                    "CASE WHEN expires_at IS NOT NULL AND expires_at < ? "
                    "THEN expires_at ELSE ? END, updated_at = ?, "
                    "first_send_started_at = COALESCE(first_send_started_at, ?) "
                    "WHERE id = ? AND status = ? AND claim_owner = ? AND claim_token = ? "
                    "AND claim_expires_at > ? AND (expires_at IS NULL OR expires_at > ?)",
                    (lease_iso, lease_iso, now_iso, now_iso, int(item_id), STATUS_CLAIMED,
                     str(owner), str(claim_token), now_iso, now_iso),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def _expire_stale(self, conn: sqlite3.Connection, now_iso: str) -> int:
        """Mark overdue rows expired so stale alerts are never sent as current."""
        cursor = conn.execute(
            """
            UPDATE notification_outbox
               SET status = ?, updated_at = ?, claim_owner = NULL,
                   claim_token = NULL, claim_expires_at = NULL,
                   last_error = COALESCE(last_error, 'expired before delivery')
             WHERE status IN (?, ?, ?)
               AND expires_at IS NOT NULL
               AND expires_at <= ?
            """,
            (STATUS_EXPIRED, now_iso, STATUS_PENDING, STATUS_CLAIMED,
             STATUS_AMBIGUOUS, now_iso),
        )
        return cursor.rowcount

    def _release_provably_unsent_reservations(self, conn: sqlite3.Connection,
                                               now_iso: str) -> int:
        """Release only expired events whose parts never reached transport.

        A pre-send renewal is durable evidence that transport might have started,
        even if the process died before recording an attempt result. Such reservations
        remain active forever; ambiguity must not silently free daily capacity.
        Reservation rows are historical records and are only marked released.
        """
        conn.execute("UPDATE notification_outbox SET status = ?, updated_at = ?, "
                     "claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL, "
                     "last_error = COALESCE(last_error, 'expired before delivery') "
                     "WHERE status IN (?, ?, ?) AND expires_at IS NOT NULL AND expires_at <= ?",
                     (STATUS_EXPIRED, now_iso, STATUS_PENDING, STATUS_CLAIMED,
                      STATUS_AMBIGUOUS, now_iso))
        cursor = conn.execute(
            "UPDATE scanner_option_reservations SET released_at = ?, "
            "release_reason = 'expired_before_first_send' "
            "WHERE released_at IS NULL AND expires_at <= ? "
            "AND EXISTS (SELECT 1 FROM notification_event_parts p "
            "WHERE p.delivery_key = scanner_option_reservations.delivery_key) "
            "AND NOT EXISTS (SELECT 1 FROM delivered_events d "
            "WHERE d.delivery_key = scanner_option_reservations.delivery_key) "
            "AND NOT EXISTS (SELECT 1 FROM notification_event_parts p "
            "JOIN notification_outbox o ON o.id = p.item_id "
            "WHERE p.delivery_key = scanner_option_reservations.delivery_key "
            "AND (o.first_send_started_at IS NOT NULL OR o.attempt_count > 0 OR o.status != ?))",
            (now_iso, now_iso, STATUS_EXPIRED),
        )
        return cursor.rowcount

    def expire_overdue(self) -> int:
        """Public wrapper: expire overdue rows outside a claim cycle."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                return self._expire_stale(conn, _iso(self._now()))
        finally:
            conn.close()

    # ── completion ────────────────────────────────────────────────────────
    def mark_sent(self, item_id: int, message_id: int | None = None, *,
                  owner: str | None = None, claim_token: str | None = None) -> bool:
        """Atomically acknowledge a current claim and its completed events."""
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise ValueError("mark_sent requires an integer message_id acknowledgement")
        if owner is None or claim_token is None:
            return False
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                now_iso = _iso(self._now())
                cursor = conn.execute(
                    "UPDATE notification_outbox SET status = ?, telegram_message_id = ?, "
                    "sent_at = ?, updated_at = ?, claim_owner = NULL, claim_token = NULL, "
                    "claim_expires_at = NULL, last_error = NULL "
                    "WHERE id = ? AND status = ? AND claim_owner = ? AND claim_token = ? "
                    "AND claim_expires_at > ? AND (expires_at IS NULL OR expires_at > ?)",
                    (STATUS_SENT, message_id, now_iso, now_iso, int(item_id), STATUS_CLAIMED,
                     str(owner), str(claim_token), now_iso, now_iso),
                )
                if not cursor.rowcount:
                    return False
                conn.execute(
                    "INSERT OR IGNORE INTO delivered_events "
                    "(delivery_key, recipient, kind, run_id, delivered_at, telegram_message_id) "
                    "SELECT e.delivery_key, e.recipient, e.kind, e.run_id, ?, ? "
                    "FROM notification_events e JOIN notification_event_parts p "
                    "ON p.delivery_key = e.delivery_key WHERE p.item_id = ? "
                    "AND NOT EXISTS (SELECT 1 FROM notification_event_parts q "
                    "JOIN notification_outbox o ON o.id = q.item_id "
                    "WHERE q.delivery_key = e.delivery_key AND o.status != ?)",
                    (now_iso, message_id, int(item_id), STATUS_SENT),
                )
                return True
        finally:
            conn.close()

    def _mark_attempt(self, item_id, error, *, owner, claim_token,
                      retry_after=None, terminal=False, ambiguous=False) -> str | None:
        if owner is None or claim_token is None:
            return None
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                now = self._now()
                now_iso = _iso(now)
                row = conn.execute(
                    "SELECT attempt_count, max_attempts FROM notification_outbox "
                    "WHERE id = ? AND status = ? AND claim_owner = ? AND claim_token = ? "
                    "AND claim_expires_at > ? AND (expires_at IS NULL OR expires_at > ?)",
                    (int(item_id), STATUS_CLAIMED, str(owner), str(claim_token), now_iso, now_iso),
                ).fetchone()
                if row is None:
                    return None
                attempts = int(row["attempt_count"]) + 1
                status = (STATUS_FAILED if terminal or attempts >= int(row["max_attempts"])
                          else STATUS_AMBIGUOUS if ambiguous else STATUS_PENDING)
                delay = max(self._backoff_seconds(attempts), float(retry_after or 0))
                next_at = now_iso if status == STATUS_FAILED else _iso(now + timedelta(seconds=delay))
                conn.execute(
                    "UPDATE notification_outbox SET status = ?, attempt_count = ?, next_attempt_at = ?, "
                    "last_error = ?, updated_at = ?, claim_owner = NULL, claim_token = NULL, "
                    "claim_expires_at = NULL WHERE id = ?",
                    (status, attempts, next_at, redact(error)[:500], now_iso, int(item_id)),
                )
                return status
        finally:
            conn.close()

    def mark_failed(self, item_id: int, error: str, *, retry_after: float | None = None,
                    terminal: bool = False, owner: str | None = None,
                    claim_token: str | None = None) -> str | None:
        """Count a current claim's failure; ``None`` means the fence rejected it."""
        return self._mark_attempt(item_id, error, owner=owner, claim_token=claim_token,
                                 retry_after=retry_after, terminal=terminal)

    def mark_ambiguous(self, item_id: int, error: str, *, owner: str | None = None,
                       claim_token: str | None = None) -> str | None:
        """Record unknown remote outcome only for the current claim."""
        return self._mark_attempt(item_id, error, owner=owner, claim_token=claim_token,
                                 ambiguous=True)

    def release_claim(self, item_id: int, *, error: str = "", owner: str | None = None,
                      claim_token: str | None = None) -> bool:
        """Release only the current unexpired claim, without counting an attempt."""
        if owner is None or claim_token is None:
            return False
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                now_iso = _iso(self._now())
                cursor = conn.execute(
                    "UPDATE notification_outbox SET status = ?, claim_owner = NULL, claim_token = NULL, "
                    "claim_expires_at = NULL, updated_at = ?, "
                    "last_error = COALESCE(NULLIF(?, ''), last_error) "
                    "WHERE id = ? AND status = ? AND claim_owner = ? AND claim_token = ? "
                    "AND claim_expires_at > ? AND (expires_at IS NULL OR expires_at > ?)",
                    (STATUS_PENDING, now_iso, redact(error)[:500], int(item_id), STATUS_CLAIMED,
                     str(owner), str(claim_token), now_iso, now_iso),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()

    def _backoff_seconds(self, attempt: int) -> float:
        delay = self.backoff_base_seconds * (2 ** max(0, attempt - 1))
        return min(delay, self.backoff_cap_seconds)

    # ── inspection ────────────────────────────────────────────────────────
    def counts(self) -> dict[str, int]:
        """Return a status → count map for logging and health checks."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM notification_outbox GROUP BY status"
            ).fetchall()
            return {row["status"]: int(row["n"]) for row in rows}
        finally:
            conn.close()

    def pending_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE status IN (?, ?, ?)",
                (STATUS_PENDING, STATUS_CLAIMED, STATUS_AMBIGUOUS),
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def get(self, item_id: int) -> OutboxItem | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (int(item_id),)
            ).fetchone()
            return OutboxItem.from_row(row) if row is not None else None
        finally:
            conn.close()

    def get_by_key(self, delivery_key: str) -> OutboxItem | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE delivery_key = ?",
                (str(delivery_key),),
            ).fetchone()
            return OutboxItem.from_row(row) if row is not None else None
        finally:
            conn.close()

    # ── per-event delivery ledger ─────────────────────────────────────────
    def event_delivered(self, delivery_key: str) -> bool:
        """True when this exact event identity was already delivered."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM delivered_events WHERE delivery_key = ?",
                (str(delivery_key),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def delivered_event_keys(self, recipient: Any, kind: str | None = None) -> set[str]:
        """Return every event identity already delivered to *recipient*."""
        conn = self._connect()
        try:
            if kind is None:
                rows = conn.execute(
                    "SELECT delivery_key FROM delivered_events WHERE recipient = ?",
                    (str(recipient),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT delivery_key FROM delivered_events "
                    "WHERE recipient = ? AND kind = ?",
                    (str(recipient), str(kind)),
                ).fetchall()
            return {row[0] for row in rows}
        finally:
            conn.close()

    def record_events_delivered(
        self,
        events: Iterable[dict],
        *,
        run_id: str = "",
        message_id: int | None = None,
    ) -> int:
        """Record that the constituent events of a message part were delivered.

        This is what makes per-fire deduplication real: a later cycle containing
        the same fire consults this ledger and does not send it again.
        """
        rows = []
        now_iso = _iso(self._now())
        for event in events:
            key = str(event.get("delivery_key") or "")
            if not key:
                continue
            rows.append((
                key, str(event.get("recipient") or ""), str(event.get("kind") or ""),
                str(run_id or event.get("run_id") or ""), now_iso, message_id,
            ))
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn:
                cursor = conn.executemany(
                    "INSERT OR IGNORE INTO delivered_events "
                    "(delivery_key, recipient, kind, run_id, delivered_at, "
                    " telegram_message_id) VALUES (?,?,?,?,?,?)",
                    rows,
                )
                return cursor.rowcount
        finally:
            conn.close()

    def items_for_run(self, run_id: str, *, recipient: Any = None,
                      kind: str | None = None) -> list[OutboxItem]:
        """Return persisted run parts, including terminal rows, in insertion order."""
        conn = self._connect()
        try:
            clauses = ["run_id = ?"]
            params = [str(run_id)]
            if recipient is not None:
                clauses.append("recipient = ?")
                params.append(str(recipient))
            if kind is not None:
                clauses.append("kind = ?")
                params.append(str(kind))
            rows = conn.execute("SELECT * FROM notification_outbox WHERE " +
                                " AND ".join(clauses) + " ORDER BY id", params).fetchall()
            return [OutboxItem.from_row(row) for row in rows]
        finally:
            conn.close()

    def all_items(self) -> list[OutboxItem]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM notification_outbox ORDER BY id ASC"
            ).fetchall()
            return [OutboxItem.from_row(row) for row in rows]
        finally:
            conn.close()


def default_outbox_path(root: Path | str) -> Path:
    """Return the default outbox database path under *root*."""
    return Path(root) / "database" / "notification_outbox.db"


def sqlite_factory(path: Path | str) -> Callable[[], sqlite3.Connection]:
    """Return a connection factory for *path* with WAL and a busy timeout.

    SQLite may report SQLITE_BUSY while several processes concurrently switch
    a newly-created database into WAL mode. Retry only that initialization
    race, with a fixed deadline; unrelated setup errors remain visible.
    """
    target = Path(path)

    def _factory() -> sqlite3.Connection:
        target.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        while True:
            conn = None
            try:
                # Keep individual setup attempts short so the monotonic
                # deadline bounds the complete initialization retry loop.
                conn = sqlite3.connect(str(target), timeout=0.1)
                conn.execute("PRAGMA busy_timeout=100")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                return conn
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    conn.close()
                code = getattr(exc, "sqlite_errorcode", None)
                if code is not None:
                    primary_code = code & 0xFF
                    retryable = primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                else:
                    message = str(exc).lower()
                    retryable = message in {
                        "database is locked",
                        "database table is locked",
                    }
                remaining = deadline - time.monotonic()
                if not retryable or remaining <= 0:
                    raise
                time.sleep(min(0.05, remaining))
            except BaseException:
                if conn is not None:
                    conn.close()
                raise

    return _factory


def new_owner_id(prefix: str = "dispatcher") -> str:
    """Return a unique claim-owner identifier."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


__all__ = [
    "OPEN_STATUSES",
    "OutboxItem",
    "NotificationOutbox",
    "SCHEMA",
    "STATUS_AMBIGUOUS",
    "STATUS_CLAIMED",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_SENT",
    "TERMINAL_STATUSES",
    "default_outbox_path",
    "ensure_schema",
    "new_owner_id",
    "sqlite_factory",
]
