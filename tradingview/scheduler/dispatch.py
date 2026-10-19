"""Pending-outbox dispatch and durable-result reconciliation.

Two production gaps this module closes:

1. **Nothing drained the outbox.** ``deliver_cycle`` only claimed the current
   run's keys, so a failed send stayed pending forever and no registered job
   retried it. :func:`dispatch_pending` claims *any* due row and sends the
   **persisted** message text — not a freshly reconstructed cycle.

2. **Durable scoring results had no production consumer.** A crash after the
   completion/result commit but before the stock payload was published left the
   fire suppressed by ``--new-only`` with no notification work. :func:`reconcile`
   discovers durable results that have no per-recipient delivery record and
   enqueues them idempotently.

Both are independent of the weekday analysis gate: a queued alert is still
retried on an excluded analysis day, subject to its own freshness deadline.
"""

from __future__ import annotations

import json
import html
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from tradingview.scheduler import formatting, identity
from tradingview.scheduler.config import redact
from tradingview.scheduler.outbox import (
    STATUS_AMBIGUOUS,
    STATUS_FAILED,
    NotificationOutbox,
    new_owner_id,
)
from tradingview.scheduler.timeutil import try_parse_iso


@dataclass
class DispatchReport:
    """Outcome of one dispatch pass."""

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    ambiguous: int = 0
    expired: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "sent": self.sent,
            "failed": self.failed,
            "ambiguous": self.ambiguous,
            "expired": self.expired,
            "errors": [redact(error) for error in self.errors],
        }


async def dispatch_pending(
    outbox: NotificationOutbox,
    *,
    bot: Any = None,
    send: Callable[..., Any] | None = None,
    limit: int = 20,
    owner: str | None = None,
) -> DispatchReport:
    """Claim and send due outbox rows, including rows from earlier runs.

    Sends the **persisted** ``message_text`` so a retry reproduces exactly what
    was queued. Honours the row's own ``parse_mode`` and falls back to plain
    text on a confirmed formatting rejection.
    """
    owner = owner or new_owner_id("dispatch")
    report = DispatchReport()

    # Expire overdue rows first so a stale alert is never sent as current.
    report.expired = outbox.expire_overdue()

    items = outbox.claim_due(owner, limit=limit)
    report.claimed = len(items)
    if not items:
        return report

    sender = send
    if sender is None:
        if bot is None:
            report.errors.append("no sender available")
            for item in items:
                outbox.release_claim(
                    item.id, error="no sender available", owner=owner,
                    claim_token=item.claim_token,
                )
            return report
        sender = lambda **kwargs: bot.send_message(**kwargs)  # noqa: E731

    for item in items:
        if not outbox.renew_claim(
            item.id, owner=owner, claim_token=item.claim_token,
        ):
            report.errors.append(f"outbox:{item.id}: send lost claim ownership")
            continue
        part = formatting.MessagePart(
            html_text=item.message_text,
            plain_text=item.payload.get("plain_text") or html.unescape(
                re.sub(r"</?(?:pre|b)>", "", item.message_text),
            ),
            label=f"outbox:{item.id}",
        )
        parse_mode = item.parse_mode or formatting.PARSE_MODE_HTML
        receipt = await formatting._send_one(sender, item.recipient_id, part, parse_mode)
        if not receipt.delivered and receipt.error_class == formatting.ERROR_FORMATTING:
            if not outbox.renew_claim(
                item.id, owner=owner, claim_token=item.claim_token,
            ):
                report.errors.append(f"outbox:{item.id}: fallback lost claim ownership")
                continue
            receipt = await formatting._send_one(
                sender, item.recipient_id, part, formatting.PARSE_MODE_PLAIN
            )

        if receipt.delivered:
            if outbox.mark_sent(
                item.id, receipt.message_id, owner=owner,
                claim_token=item.claim_token,
            ):
                report.sent += 1
            else:
                report.errors.append(f"outbox:{item.id}: acknowledgement lost claim ownership")
        elif receipt.ambiguous:
            status = outbox.mark_ambiguous(
                item.id, receipt.error or "no acknowledgement", owner=owner,
                claim_token=item.claim_token,
            )
            if status == STATUS_AMBIGUOUS:
                report.ambiguous += 1
            elif status == STATUS_FAILED:
                report.failed += 1
            elif status is None:
                report.errors.append(f"outbox:{item.id}: ambiguous outcome lost claim ownership")
        else:
            status = outbox.mark_failed(
                item.id,
                receipt.error or "send failed",
                retry_after=receipt.retry_after,
                terminal=receipt.error_class == formatting.ERROR_PERMISSION,
                owner=owner,
                claim_token=item.claim_token,
            )
            if status == STATUS_FAILED:
                report.failed += 1
            elif status is None:
                report.errors.append(f"outbox:{item.id}: failure lost claim ownership")
            else:
                report.errors.append(redact(receipt.error or "send failed"))
    return report


def _scoring_result_to_stock(payload: dict) -> dict | None:
    """Convert one durable scoring payload into a stock delivery row."""
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        return None
    if str(result.get("source") or "").lower() == "scanner":
        session_date = str(result.get("session_date") or "")
        timestamp = str(result.get("timestamp") or "")
        direction = str(result.get("direction") or "").upper()
        try:
            valid_date = date.fromisoformat(session_date).isoformat() == session_date
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            valid_timestamp = parsed_timestamp.tzinfo is not None and parsed_timestamp.utcoffset() is not None
            from tradingview.scheduler.scanner import MARKETS
            market_key = str(result.get("market") or "").upper()
            valid_timestamp = valid_timestamp and parsed_timestamp.astimezone(
                ZoneInfo(MARKETS[market_key][1])
            ).date().isoformat() == session_date if market_key in MARKETS else False
        except (TypeError, ValueError):
            valid_date, valid_timestamp = False, False
        if (result.get("actionable") is not True
                or str(result.get("market") or "").upper() not in {"US", "AU", "IN", "SG"}
                or direction not in {"BUY", "SELL"}
                or not valid_date or not valid_timestamp):
            return None
    elif not result.get("actionable"):
        # A vetoed or ineligible fire is not an actionable alert.
        return None
    pro_symbol = str(payload.get("pro_symbol") or "")
    if not pro_symbol:
        return None
    try:
        score = float(result.get("composite"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    if str(result.get("source") or "").lower() == "scanner" and not 0 <= score <= 100:
        return None
    exchange, _, symbol = pro_symbol.partition(":")
    return {
        "pro_symbol": pro_symbol,
        "exchange": exchange or "US",
        "symbol": symbol or pro_symbol,
        "score": round(score, 1),
        "signal": str(result.get("signal") or "SCR")[:4],
        "fire_time": str(payload.get("fire_time") or ""),
        "resolution": str(payload.get("resolution") or ""),
        **({key: result[key] for key in (
            "source", "market", "session_date", "timestamp", "direction",
            "bar_time", "bar_end_time", "bar_interval_minutes", "quote_source",
            "quote_symbol", "provider_close", "source_price_delta_pct",
            "freshness_assurance", "configured_provider_delay_minutes",
        ) if key in result}),
        "actionable": True,
    }


def _original_expiry(
    outbox: NotificationOutbox,
    timestamp: Any,
    ttl_hours: float | None,
    explicit_expiry: Any = None,
) -> datetime | None:
    """Derive recovery freshness from durable creation, never recovery time.

    Missing or invalid creation timestamps cannot establish freshness. Explicit
    persisted deadlines can shorten the original TTL, but cannot extend it.
    """
    created = try_parse_iso(timestamp)
    if created is None:
        return None
    ttl = outbox.alert_ttl_hours if ttl_hours is None else float(ttl_hours)
    # Recovery always requires a finite freshness bound, including when a
    # malformed configuration would otherwise disable the ordinary TTL.
    if ttl <= 0:
        return None
    expiry = created + timedelta(hours=ttl)
    persisted = try_parse_iso(explicit_expiry)
    if persisted is not None:
        expiry = min(expiry, persisted)
    return expiry


def _enqueue_recovered_event(
    outbox: NotificationOutbox,
    recipient: int,
    row: dict,
    kind: str,
    run_id: str,
    expiry: datetime,
) -> int:
    """Reserve one original event and enqueue its exact rendered parts."""
    row = dict(row)
    scanner_reservation = None
    scanner_option_cap = None
    if kind == identity.KIND_OPTION and str(row.get("source") or "").lower() == "scanner":
        # Recovery uses the same immutable contract fields and quote deadline
        # as normal delivery. Never derive a new deadline from recovery time.
        if not row.get("option_type") and row.get("opt_type"):
            row["option_type"] = row["opt_type"]
        scanner_option_cap = row.get("scanner_option_cap") or row.get("max_contracts_per_symbol") or 1
        scanner_reservation = {
            "market": row.get("market"),
            "symbol": row.get("symbol"),
            "session_date": row.get("session_date"),
            "cap": scanner_option_cap,
            "expires_at": row.get("recommendation_expires_at"),
            "recommendation": row,
        }
    try:
        event_key = identity.delivery_key_for(recipient, kind, row)
    except (TypeError, ValueError, KeyError):
        return 0  # One corrupt recovered row must not discard valid events.
    event = {
        "delivery_key": event_key,
        "recipient": recipient,
        "kind": kind,
        "run_id": run_id,
    }
    if scanner_reservation is not None:
        event["scanner_option_reservation"] = scanner_reservation

    def build_items(events: list[dict]) -> list[dict]:
        if not events:
            return []
        parts = _render_recovery_parts(row, run_id, kind)
        return [
            {
                "delivery_key": identity.summary_delivery_key(
                    recipient, f"recover:{run_id}:{event_key}", index,
                ),
                "recipient": recipient,
                "kind": identity.KIND_SUMMARY,
                "payload": {
                    "run_id": run_id,
                    "recovered": True,
                    "part": index,
                    "events": events,
                    "plain_text": part.plain_text,
                },
                "message_text": part.html_text,
                "run_id": run_id,
                "parse_mode": formatting.PARSE_MODE_HTML,
            }
            for index, part in enumerate(parts)
        ]

    return outbox.enqueue_event_parts(
        recipient=recipient, events=[event], build_items=build_items,
        expires_at=expiry, scanner_option_cap=scanner_option_cap,
    )


def reconcile(
    outbox: NotificationOutbox,
    scoring_conn: sqlite3.Connection,
    recipients: Sequence[int],
    *,
    limit: int = 200,
    ttl_hours: float | None = None,
) -> int:
    """Enqueue notification work for durable results that lack a delivery record.

    Idempotent: the per-fire delivery identity is unique, so running this on
    every startup and every cycle cannot produce duplicate sends. Historical
    completed fires are not re-queued because their events are either already in
    ``delivered_events`` or were never actionable.
    """
    from tradingview import outcome_store

    recipients = identity.normalize_recipients(recipients)
    if not recipients:
        return 0

    try:
        payloads = outcome_store.load_scoring_results(scoring_conn, limit=limit)
        # New scanner records are setup-scoped and production-only. Keep the
        # legacy ledger reader during rollout so pending old daily events remain
        # recoverable; the unchanged delivery key suppresses acknowledged days.
        from tradingview.scanner_store import load_scoring_results as _load_scanner_results
        scanner_payloads = _load_scanner_results(scoring_conn, scope="production", limit=limit)
        payloads.extend(scanner_payloads)
    except sqlite3.Error:
        return 0

    queued = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        stock = _scoring_result_to_stock(payload)
        if stock is None:
            continue
        expiry = _original_expiry(
            outbox, payload.get("scored_at"), ttl_hours, payload.get("expires_at"),
        )
        if expiry is None:
            continue
        run_id = str(payload.get("run_id") or "")
        for recipient in recipients:
            queued += _enqueue_recovered_event(
                outbox, recipient, stock, identity.KIND_STOCK, run_id, expiry,
            )
    return queued


def _render_recovery_parts(row: dict, run_id: str, kind: str) -> list[formatting.MessagePart]:
    """Render a recovered alert as safe HTML."""
    card = formatting.option_card(row) if kind == identity.KIND_OPTION else formatting.stock_card(row)
    return formatting.build_parts(
        [formatting.Block(lines=["Recommendation · delayed delivery", card])],
        limit=formatting.TELEGRAM_SAFE_LIMIT, label_parts=False,
    )


def reconcile_cycle_results(
    outbox: NotificationOutbox,
    run_root: Path | str,
    recipients: Sequence[int],
    *,
    limit: int = 50,
    ttl_hours: float | None = None,
) -> int:
    """Enqueue work for persisted cycle results whose events were never sent.

    Covers the crash-after-commit-before-enqueue window: the cycle result file
    is durable, so its actionable rows can be re-enqueued idempotently.
    """
    root = Path(run_root)
    if not root.exists():
        return 0
    recipients = identity.normalize_recipients(recipients)
    if not recipients:
        return 0

    queued = 0
    run_dirs = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    for run_dir in run_dirs:
        result_path = run_dir / "cycle_result.json"
        if not result_path.exists():
            continue
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        run_id = str(payload.get("run_id") or run_dir.name)
        expiry = _original_expiry(
            outbox, payload.get("started_at"), ttl_hours, payload.get("expires_at"),
        )
        if expiry is None:
            continue
        for field, rows_field, kind in (
            ("stock_result", "stocks", identity.KIND_STOCK),
            ("option_result", "options", identity.KIND_OPTION),
        ):
            result = payload.get(field) or {}
            if not isinstance(result, dict):
                continue
            rows = result.get(rows_field) or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for recipient in recipients:
                    try:
                        queued += _enqueue_recovered_event(
                            outbox, recipient, row, kind, run_id, expiry,
                        )
                    except (TypeError, ValueError, KeyError, OverflowError):
                        # Bad scanner option evidence is isolated to that row;
                        # recovery must continue with other options and stocks.
                        if kind != identity.KIND_OPTION or str(row.get("source") or "").lower() != "scanner":
                            raise
    return queued


__all__ = [
    "DispatchReport",
    "dispatch_pending",
    "reconcile",
    "reconcile_cycle_results",
]
