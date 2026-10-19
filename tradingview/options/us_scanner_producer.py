"""Bounded producer for scanner-origin US equity CALL recommendations.

The producer consumes completed signal-stack rows and delegates option ranking
to the existing OptionsAnalyzer badge pipeline. It never fetches a chain unless
the parent is an explicitly retained, actionable US BUY equity setup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tradingview.options.options_snapshot import build_snapshot, publish_snapshot
from tradingview.scheduler.us_options_policy import (
    USOptionsPolicy,
    extract_validated_badge_calls,
    select_actionable_parents,
    validate_persisted_scanner_record,
)


EquityBadgeCall = Callable[[str, float], dict[str, Any]]


def _is_explicit_equity(row: dict[str, Any]) -> bool:
    """Require affirmative discovery metadata; unknown instruments fail closed."""
    marker = row.get("instrument_type", row.get("asset_class"))
    if marker is None:
        return False
    return str(marker).strip().lower() in {"equity", "stock"}


def _parent_row(row: dict[str, Any], *, session_date: str) -> dict[str, Any]:
    return {
        "source": row.get("source"),
        "market": row.get("market"),
        "scope": row.get("scope"),
        "session_date": row.get("session_date"),
        "direction": row.get("direction"),
        "actionable": row.get("actionable"),
        "durably_persisted": row.get("durably_persisted"),
        "scanner_setup_id": row.get("scanner_setup_id"),
        "pro_symbol": row.get("tv_symbol") or row.get("pro_symbol"),
        "score": row.get("composite", row.get("score")),
        "run_id": row.get("run_id"),
        "near_earnings": row.get("near_earnings"),
        "_source_row": row,
    }


def produce_us_scanner_options(
    rows: list[dict[str, Any]],
    *,
    snapshot_path: Path,
    run_id: str,
    session_date: str,
    session_open: datetime,
    session_close: datetime,
    policy: USOptionsPolicy,
    parent_threshold: float,
    option_threshold: float,
    badge_call: EquityBadgeCall,
    clock: Callable[[], datetime] | None = None,
    max_parent_bar_age_minutes: int = 45,
    configured_provider_delay_minutes: int = 15,
) -> dict[str, Any]:
    """Analyze eligible parents, validate scored calls, and publish one snapshot.

    ``badge_call`` must invoke the existing one-chain/all-modes analyzer with
    the real parent score and return its completed badge structure. The caller
    supplies a bounded provider adapter; no timeout thread is created here.
    """
    if session_open.tzinfo is None or session_open.utcoffset() is None:
        raise ValueError("session_open must be timezone-aware")
    if session_close.tzinfo is None or session_close.utcoffset() is None:
        raise ValueError("session_close must be timezone-aware")

    clock = clock or (lambda: datetime.now(timezone.utc))
    if not session_date:
        snapshot = build_snapshot(run_id, "unavailable", [], [{
            "symbol": "US", "message": "US session date is missing",
        }], generated_at=clock().astimezone(timezone.utc).isoformat())
        publish_snapshot(Path(snapshot_path), snapshot)
        return snapshot

    from tradingview.scheduler.scanner import _valid_freshness_contract
    selection_time = clock()
    if selection_time.tzinfo is None or selection_time.utcoffset() is None:
        raise ValueError("clock must return timezone-aware values")
    eligible_rows = [row for row in rows if isinstance(row, dict) and _is_explicit_equity(row)
                     and _valid_freshness_contract(
                         row, "US", session_date, selection_time,
                         max_parent_bar_age_minutes, configured_provider_delay_minutes)]
    parents = select_actionable_parents(
        [_parent_row(row, session_date=session_date) for row in eligible_rows],
        session_date=session_date,
        min_score=parent_threshold,
        policy=policy,
    )
    contracts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for parent in parents:
        before_fetch = clock()
        if before_fetch.tzinfo is None or before_fetch.utcoffset() is None:
            raise ValueError("clock must return timezone-aware values")
        if not (session_open <= before_fetch.astimezone(timezone.utc) < session_close.astimezone(timezone.utc)):
            continue
        symbol = str(parent["pro_symbol"]).upper()
        source_row = dict(parent["_source_row"])
        if not _valid_freshness_contract(
            source_row, "US", session_date, before_fetch,
            max_parent_bar_age_minutes, configured_provider_delay_minutes,
        ):
            continue
        # Adapt field aliases only; eligibility and durability remain the
        # original completed producer evidence selected above.
        source_row["tv_symbol"] = symbol
        source_row["composite"] = float(parent["score"])
        try:
            # Existing analyzer fetches the chain once and ranks all three
            # strategy modes from that same chain.
            source_row["options_data"] = badge_call(symbol, float(parent["score"]))
        except Exception as exc:
            source_row["options_data"] = {
                "error": f"options provider failed: {type(exc).__name__}",
                "meta": {"market": "US", "data_source": "none"},
            }
        completed_at = clock()
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            raise ValueError("clock must return timezone-aware values")
        badge = source_row.get("options_data")
        if isinstance(badge, dict):
            # Fetch completion is captured locally as its own event. It never
            # fills bid_quote_at or ask_quote_at, whose provider evidence must
            # be independently supplied.
            for mode in ("BALANCED", "AGGRESSIVE", "SAFE"):
                mode_data = badge.get(mode) or {}
                if not isinstance(mode_data, dict):
                    continue
                for contract in mode_data.get("top_contracts") or []:
                    if isinstance(contract, dict):
                        contract.setdefault("fetch_completed_at", completed_at.isoformat())
        rows_contracts, row_errors, _row_status = extract_validated_badge_calls(
            [source_row],
            session_date=session_date,
            now=completed_at,
            session_close=session_close,
            policy=policy,
            parent_threshold=parent_threshold,
            option_threshold=option_threshold,
            run_id=run_id,
        )
        contracts.extend(rows_contracts)
        errors.extend(row_errors)

    if contracts and errors:
        status = "partial"
    elif contracts:
        status = "success"
    elif errors:
        status = "unavailable"
    else:
        # No eligible parent and closed-session scans are successful silent
        # outcomes; importantly, they do not call the provider adapter.
        status = "empty"

    generated_at = clock()
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("clock must return timezone-aware values")
    generated_at = generated_at.astimezone(timezone.utc)
    still_valid: list[dict[str, Any]] = []
    for contract in contracts:
        try:
            validate_persisted_scanner_record(contract, generated_at)
            still_valid.append(contract)
        except (TypeError, ValueError) as exc:
            errors.append({
                "symbol": f"{contract.get('exchange', '')}:{contract.get('symbol', '')}",
                "message": f"quote evidence expired before snapshot publication: {exc}",
            })
    contracts = still_valid
    if contracts and errors:
        status = "partial"
    elif contracts:
        status = "success"
    elif errors:
        status = "unavailable"
    else:
        status = "empty"
    snapshot = build_snapshot(run_id, status, contracts, errors,
                              generated_at=generated_at.isoformat())
    publish_snapshot(Path(snapshot_path), snapshot)
    return snapshot
