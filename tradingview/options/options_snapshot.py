"""Structured OptionsAnalyzer badge snapshots shared by reports and Telegram."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
SOURCE = "options_badge"
SCORE_MODEL = "OptionsAnalyzer.option_score"
SNAPSHOT_STATUSES = frozenset({
    "success",
    "empty",
    "no_new_signals",
    "disabled",
    "partial",
    "unavailable",
    "failed",
})
MARKET_TIMEZONES = {
    "US": "America/New_York",
    "USA": "America/New_York",
    "AU": "Australia/Sydney",
    "ASX": "Australia/Sydney",
    "HK": "Asia/Hong_Kong",
    "HKEX": "Asia/Hong_Kong",
    "SG": "Asia/Singapore",
    "SGX": "Asia/Singapore",
    "IN": "Asia/Kolkata",
    "NSE": "Asia/Kolkata",
    "BSE": "Asia/Kolkata",
    "JP": "Asia/Tokyo",
    "UK": "Europe/London",
}


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp suitable for JSON metadata."""
    return datetime.now(timezone.utc).isoformat()


def _finite_number(value: Any, field: str, *, minimum: float | None = None,
                   maximum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be <= {maximum}")
    return number


def _option_type(contract: dict[str, Any]) -> str:
    values = []
    for key in ("option_type", "type"):
        if key in contract and contract[key] is not None:
            raw = str(contract[key]).strip().upper()
            if raw in ("CALL", "C"):
                values.append("CALL")
            elif raw in ("PUT", "P"):
                values.append("PUT")
            else:
                raise ValueError(f"unknown option type: {contract[key]!r}")
    if not values:
        raise ValueError("missing option type")
    if len(set(values)) != 1:
        raise ValueError("option_type and type disagree")
    return values[0]


def _symbol_parts(parent_symbol: str) -> tuple[str, str]:
    if not isinstance(parent_symbol, str) or not parent_symbol.strip():
        raise ValueError("missing parent symbol")
    if ":" in parent_symbol:
        exchange, symbol = parent_symbol.split(":", 1)
    else:
        exchange, symbol = "US", parent_symbol
    exchange = exchange.strip().upper()
    symbol = symbol.strip().upper()
    if not exchange or not symbol:
        raise ValueError("invalid parent symbol")
    return exchange, symbol


def _contract_identifier(contract: dict[str, Any]) -> str | None:
    keys = ("contract_id", "option_symbol", "occ_symbol", "contract_symbol")
    present = [contract[key] for key in keys if key in contract and contract[key] is not None]
    if not present:
        return None
    identifier = present[0]
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("invalid contract identifier")
    return identifier.strip()


def _expiry_date(value: Any) -> date:
    """Parse the analyzer expiry as exactly YYYY-MM-DD."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing expiry")
    expiry = value.strip()
    if len(expiry) != 10 or expiry[4] != "-" or expiry[7] != "-":
        raise ValueError("expiry must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(expiry)
    except ValueError as exc:
        raise ValueError("expiry must be a valid ISO date (YYYY-MM-DD)") from exc


def _market_local_date(market: str, reference_now: datetime | None = None) -> date:
    timezone_name = MARKET_TIMEZONES.get(market.upper())
    if timezone_name is None:
        raise ValueError(f"unsupported market timezone: {market}")
    reference = reference_now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("reference_now must include timezone")
    return reference.astimezone(ZoneInfo(timezone_name)).date()


def _validate_snapshot_contract(
    contract: Any,
    index: int,
    reference_now: datetime,
) -> list[str]:
    """Validate every field consumed by the scheduler for one snapshot row."""
    prefix = f"contracts[{index}]"
    if not isinstance(contract, dict):
        return [f"{prefix} must be an object"]
    errors: list[str] = []
    required_fields = (
        "market", "exchange", "symbol", "option_type", "strike", "expiry",
        "dte", "option_score", "parent_signal_score", "strategy_modes",
    )
    for field in required_fields:
        if field not in contract:
            errors.append(f"{prefix} missing {field}")

    for field in ("market", "exchange", "symbol", "option_type", "expiry"):
        value = contract.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}.{field} must be a non-empty string")

    if isinstance(contract.get("market"), str):
        try:
            _market_local_date(contract["market"].upper(), reference_now)
        except ValueError as exc:
            errors.append(f"{prefix}.market invalid: {exc}")

    if isinstance(contract.get("exchange"), str) and not contract["exchange"].strip():
        errors.append(f"{prefix}.exchange must be non-empty")
    if isinstance(contract.get("symbol"), str) and not contract["symbol"].strip():
        errors.append(f"{prefix}.symbol must be non-empty")

    option_type = contract.get("option_type")
    if not isinstance(option_type, str) or option_type not in {"CALL", "PUT"}:
        errors.append(f"{prefix}.option_type must be CALL or PUT")

    try:
        strike = contract["strike"]
        if isinstance(strike, bool):
            raise ValueError("must be a finite positive number")
        strike_value = _finite_number(strike, "strike")
        if strike_value <= 0:
            raise ValueError("must be a finite positive number")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"{prefix}.strike {exc}")

    try:
        expiry_date = _expiry_date(contract["expiry"])
        market = contract.get("market")
        if not isinstance(market, str) or not market.strip():
            raise ValueError("market must be a non-empty string")
        if _market_local_date(market.upper(), reference_now) > expiry_date:
            errors.append(f"{prefix} is expired")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        errors.append(f"{prefix}.expiry invalid: {exc}")

    dte = contract.get("dte")
    if isinstance(dte, bool) or not isinstance(dte, int) or dte < 0:
        errors.append(f"{prefix}.dte must be a non-negative integer")

    for field in ("option_score", "parent_signal_score"):
        try:
            value = contract[field]
            if isinstance(value, bool):
                raise ValueError("must be finite and between 0 and 100")
            _finite_number(value, field, minimum=0.0, maximum=100.0)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{prefix}.{field} {exc}")

    modes = contract.get("strategy_modes")
    if (
        not isinstance(modes, list)
        or not modes
        or any(not isinstance(mode, str) or not mode.strip() for mode in modes)
    ):
        errors.append(f"{prefix}.strategy_modes must be a non-empty string list")

    identifier = contract.get("contract_identifier")
    if identifier is not None and (not isinstance(identifier, str) or not identifier.strip()):
        errors.append(f"{prefix}.contract_identifier must be a string or null")
    if contract.get("source") == "scanner":
        from tradingview.scheduler.us_options_policy import (
            InvalidOptionEvidence, validate_persisted_scanner_record,
        )
        try:
            validate_persisted_scanner_record(contract, reference_now)
        except (InvalidOptionEvidence, TypeError, ValueError) as exc:
            errors.append(f"{prefix}.scanner quote: {exc}")
    return errors


def normalize_contract(
    parent_symbol: str,
    parent_signal_score: Any,
    market: str,
    mode: str,
    contract: dict[str, Any],
    reference_now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize one analyzer contract without changing its option score."""
    if not isinstance(contract, dict):
        raise ValueError("contract must be an object")
    exchange, symbol = _symbol_parts(parent_symbol)
    market_name = str(market or contract.get("market") or "").strip().upper()
    if not market_name:
        raise ValueError("missing market")
    option_type = _option_type(contract)
    strike = _finite_number(contract.get("strike"), "strike", minimum=0.0)
    if strike <= 0:
        raise ValueError("strike must be > 0")
    expiry_date = _expiry_date(contract.get("expiry"))
    if _market_local_date(market_name, reference_now) > expiry_date:
        raise ValueError(f"expired contract: {expiry_date.isoformat()}")
    dte_float = _finite_number(contract.get("dte"), "dte", minimum=0.0)
    if dte_float != int(dte_float):
        raise ValueError("dte must be an integer")
    option_score = _finite_number(
        contract.get("option_score"), "option_score", minimum=0.0, maximum=100.0
    )
    parent_score = _finite_number(
        parent_signal_score, "parent_signal_score", minimum=0.0, maximum=100.0
    )
    return {
        "symbol": symbol,
        "exchange": exchange,
        "market": market_name,
        "contract_identifier": _contract_identifier(contract),
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry_date.isoformat(),
        "dte": int(dte_float),
        "option_score": option_score,
        "parent_signal_score": parent_score,
        "strategy_modes": [str(mode).upper()],
        **option_quote_fields({**contract, "market": market_name}),
    }


def option_quote_fields(contract: dict[str, Any]) -> dict[str, Any]:
    """Keep optional source prices without treating missing quotes as zero."""
    fields: dict[str, Any] = {}
    for key in ("bid", "ask", "last_price", "mid_price"):
        value = contract.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number > 0:
            fields[key] = number
    currency = {"US": "USD", "NSE": "INR", "BSE": "INR", "IN": "INR",
                "HK": "HKD", "HKEX": "HKD", "AU": "AUD", "SG": "SGD"}.get(
                    str(contract.get("market") or "").upper())
    if currency:
        fields["premium_currency"] = currency
    elif contract.get("premium_currency") in {"USD", "INR", "HKD", "AUD", "SGD"}:
        fields["premium_currency"] = contract["premium_currency"]
    if contract.get("source") == "scanner":
        for key in (
            "source", "scope", "market", "session_date", "contract_identifier",
            "parent_setup_id", "parent_run_id", "analysis_run_id",
            "option_score", "parent_signal_score",
            "quote_provider", "bid_quote_at", "ask_quote_at", "fetch_completed_at",
            "freshness_status", "quote_mode", "premium_basis", "tv_update_time",
            "volume_session_provenance", "oi_source", "quote_max_age_seconds", "max_spread_pct",
            "session_close_at", "recommendation_expires_at", "oi", "volume",
            "min_open_interest", "min_volume", "min_dte", "max_dte",
            "oi_asof", "volume_session_date",
        ):
            if key in contract:
                fields[key] = contract[key]
    return fields


def normalize_badge_data(
    parent_symbol: str,
    parent_signal_score: Any,
    badge_data: dict[str, Any],
    reference_now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Normalize all displayed strategy-mode contracts from one badge."""
    if not isinstance(badge_data, dict):
        return [], [{"symbol": parent_symbol, "message": "badge data is not an object"}]
    errors: list[dict[str, str]] = []
    if badge_data.get("error"):
        errors.append({"symbol": parent_symbol, "message": str(badge_data["error"])})
    meta = badge_data.get("meta") or {}
    market = meta.get("market") or badge_data.get("market")
    contracts: list[dict[str, Any]] = []
    for mode in ("BALANCED", "AGGRESSIVE", "SAFE"):
        mode_data = badge_data.get(mode) or {}
        for raw_contract in mode_data.get("top_contracts") or []:
            try:
                contracts.append(
                    normalize_contract(
                        parent_symbol,
                        parent_signal_score,
                        str(market or raw_contract.get("market") or ""),
                        mode,
                        raw_contract,
                        reference_now,
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                errors.append({
                    "symbol": parent_symbol,
                    "mode": mode,
                    "message": str(exc),
                })
    return contracts, errors


def deduplicate_contracts(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate across modes, retaining the highest score and all modes."""
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for contract in contracts:
        key = (
            contract["market"], contract["exchange"], contract["symbol"],
            contract["option_type"], contract["strike"], contract["expiry"],
        )
        existing = grouped.get(key)
        if existing is None or contract["option_score"] > existing["option_score"]:
            replacement = dict(contract)
            replacement["strategy_modes"] = list(contract["strategy_modes"])
            if existing is not None:
                replacement["strategy_modes"] = sorted(set(
                    existing["strategy_modes"] + replacement["strategy_modes"]
                ))
            grouped[key] = replacement
        else:
            existing["strategy_modes"] = sorted(set(
                existing["strategy_modes"] + contract["strategy_modes"]
            ))
    return sorted(
        grouped.values(),
        key=lambda item: (-item["option_score"], item["symbol"], item["expiry"]),
    )


def build_snapshot(
    run_id: str,
    status: str,
    contracts: list[dict[str, Any]] | None = None,
    errors: list[dict[str, str]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if status not in SNAPSHOT_STATUSES:
        raise ValueError(f"unknown snapshot status: {status}")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "generated_at": generated_at or utc_now_iso(),
        "source": SOURCE,
        "score_model": SCORE_MODEL,
        "status": status,
        "policy": {
            "parent_population": "actionable_signals_only",
            "watch_signals": "excluded",
            "strategy_modes": ["BALANCED", "AGGRESSIVE", "SAFE"],
            "duplicate_policy": "highest_option_score_and_union_modes",
        },
        "contracts": deduplicate_contracts(contracts or []),
        "errors": errors or [],
    }


def derive_snapshot_status(
    requested_status: str,
    contracts: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> str:
    """Derive data status after badge rows have been normalized."""
    if requested_status in {"disabled", "no_new_signals", "failed", "unavailable"}:
        return requested_status
    if contracts and errors:
        return "partial"
    if contracts:
        return "success"
    return "unavailable" if errors else "empty"


def publish_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """Publish a complete snapshot atomically in its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, indent=2, sort_keys=True).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def validate_snapshot(
    snapshot: Any,
    *,
    expected_run_id: str | None = None,
    max_age_hours: float | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Return validation errors; an empty list means the snapshot is usable."""
    errors: list[str] = []
    if not isinstance(snapshot, dict):
        return ["snapshot is not an object"]
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    if snapshot.get("source") != SOURCE:
        errors.append("unexpected source")
    if snapshot.get("score_model") != SCORE_MODEL:
        errors.append("unexpected score_model")
    if not isinstance(snapshot.get("run_id"), str) or not snapshot["run_id"]:
        errors.append("missing run_id")
    elif expected_run_id is not None and snapshot["run_id"] != expected_run_id:
        errors.append("snapshot run_id does not match current run")
    generated_at = snapshot.get("generated_at")
    parsed_time: datetime | None = None
    if not isinstance(generated_at, str):
        errors.append("missing generated_at")
    else:
        try:
            parsed_time = datetime.fromisoformat(generated_at)
            if parsed_time.tzinfo is None:
                errors.append("generated_at must include timezone")
        except ValueError:
            errors.append("invalid generated_at")
    current = now or datetime.now(timezone.utc)
    if parsed_time is not None and parsed_time.tzinfo is not None:
        age_hours = (current - parsed_time.astimezone(timezone.utc)).total_seconds() / 3600
        if age_hours < -0.01:
            errors.append("generated_at is in the future")
        elif max_age_hours is not None and age_hours > max_age_hours:
            errors.append("snapshot is stale")
    if snapshot.get("status") not in SNAPSHOT_STATUSES:
        errors.append("invalid status")
    contracts = snapshot.get("contracts")
    if not isinstance(contracts, list):
        errors.append("contracts must be a list")
    if not isinstance(snapshot.get("errors"), list):
        errors.append("errors must be a list")
    if isinstance(contracts, list):
        for index, contract in enumerate(contracts):
            errors.extend(_validate_snapshot_contract(contract, index, current))
    return errors


def read_snapshot(
    path: Path,
    *,
    expected_run_id: str | None = None,
    max_age_hours: float | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, ["options snapshot is missing"]
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"options snapshot is unreadable: {exc}"]
    errors = validate_snapshot(
        snapshot, expected_run_id=expected_run_id, max_age_hours=max_age_hours, now=now
    )
    return (snapshot if not errors else None), errors
