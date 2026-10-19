"""Fail-closed policy for scanner-origin US CALL recommendations.

This module deliberately does not fetch quotes, send alerts, or touch databases.
Callers must prove that their input parents were durably retained by the stock
scanner and that quote timestamps describe executable bid/ask observations.
A recent request-completion timestamp or a Yahoo last trade is NOT sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Iterable
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
_SUPPORTED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "BATS", "CBOE"})


class InvalidOptionEvidence(ValueError):
    """A contract is unsafe to publish as an actionable option quote."""


@dataclass(frozen=True)
class USOptionsPolicy:
    max_underlyings: int = 5
    max_contracts_per_symbol: int = 1
    min_dte: int = 14
    max_dte: int = 45
    min_open_interest: int = 100
    min_volume: int = 10
    max_spread_pct: float = 10.0
    max_quote_age_seconds: int = 120
    quote_mode: str = "strict"

    def __post_init__(self) -> None:
        for field in ("max_underlyings", "max_contracts_per_symbol", "min_dte", "max_dte",
                      "min_open_interest", "min_volume", "max_quote_age_seconds"):
            value = getattr(self, field)
            if type(value) is not int:
                raise ValueError(f"{field} must be an integer")
        for field in ("max_underlyings", "max_contracts_per_symbol", "max_quote_age_seconds"):
            if getattr(self, field) <= 0:
                raise ValueError(f"{field} must be positive")
        if self.max_underlyings > 100 or self.max_contracts_per_symbol > 20:
            raise ValueError("scanner workload limits exceed safe bounds")
        if not 0 <= self.min_dte <= self.max_dte <= 365:
            raise ValueError("DTE range must be ordered and within 0..365")
        if self.min_open_interest < 0 or self.min_volume < 0:
            raise ValueError("OI and volume minimums must not be negative")
        if self.max_quote_age_seconds > 900:
            raise ValueError("quote age must be <= 900 seconds")
        if self.quote_mode not in {"strict", "reference"}:
            raise ValueError("quote_mode must be strict or reference")
        if isinstance(self.max_spread_pct, bool) or not isinstance(self.max_spread_pct, (float, int)):
            raise ValueError("maximum spread must be numeric")
        if not isfinite(float(self.max_spread_pct)) or not 0 < self.max_spread_pct <= 100:
            raise ValueError("maximum spread must be finite and within (0,100]")


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise InvalidOptionEvidence(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidOptionEvidence(f"{label} must be numeric") from exc
    if not isfinite(result) or result <= 0:
        raise InvalidOptionEvidence(f"{label} must be finite and positive")
    return result


def _nonnegative_integral(value: Any, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise InvalidOptionEvidence(f"{label} must be reported as an integer")
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d != d.to_integral_value() or d < 0:
            raise ValueError()
        return int(d)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise InvalidOptionEvidence(f"{label} must be reported as a nonnegative integer") from exc


def _aware_timestamp(raw: Any, label: str, now: datetime, max_age: int) -> datetime:
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidOptionEvidence(f"{label} is malformed") from exc
    else:
        raise InvalidOptionEvidence(f"{label} is missing")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidOptionEvidence(f"{label} must include timezone")
    utc = parsed.astimezone(timezone.utc)
    age = (now - utc).total_seconds()
    if age < -120:
        raise InvalidOptionEvidence(f"{label} is materially future-dated")
    if age > max_age:
        raise InvalidOptionEvidence(f"{label} is stale")
    return utc


def _canonical_strike(strike: Any) -> str:
    price = _positive_finite(strike, "strike")
    try:
        val = Decimal(str(price)).normalize()
    except InvalidOperation as exc:
        raise InvalidOptionEvidence("strike is malformed") from exc
    return format(val, "f")


def canonical_contract(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Canonical tuple, independent of provider contract-code aliases.

    Avoid preferring `contract_id` at one boundary and `contract_identifier` at
    another, which would make restart recovery deliver a duplicate.
    """
    exchange = str(row.get("exchange") or "").strip().upper()
    symbol = str(row.get("symbol") or "").strip().upper()
    opt_type = str(row.get("option_type") or row.get("opt_type") or "").strip().upper()
    if exchange not in _SUPPORTED_EXCHANGES or not symbol or ":" in symbol:
        raise InvalidOptionEvidence("unsupported US contract symbol")
    if opt_type not in {"CALL", "PUT"}:
        raise InvalidOptionEvidence("unknown contract option type")
    try:
        expiry = date.fromisoformat(str(row.get("expiry") or ""))
    except ValueError as exc:
        raise InvalidOptionEvidence("invalid contract expiry") from exc
    return exchange, symbol, expiry.isoformat(), _canonical_strike(row.get("strike")), opt_type


def select_actionable_parents(
    rows: Iterable[dict[str, Any]], *, session_date: str,
    min_score: float, policy: USOptionsPolicy,
) -> list[dict[str, Any]]:
    """Choose only explicitly retained, actionable US scanner BUY setups.

    `durably_persisted` and `scanner_setup_id` must be set by the producer after
    successful transactional persistence; never infer durability from score.
    """
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (row.get("source") != "scanner" or row.get("market") != "US"
                or row.get("session_date") != session_date or row.get("scope") != "production"
                or row.get("direction") != "BUY" or row.get("actionable") is not True
                or row.get("durably_persisted") is not True
                or not row.get("scanner_setup_id")):
            continue
        pro = str(row.get("pro_symbol") or "").strip().upper()
        exchange, sep, symbol = pro.partition(":")
        if not sep or exchange not in _SUPPORTED_EXCHANGES or not symbol:
            continue
        try:
            score = float(row.get("score"))
        except (TypeError, ValueError, OverflowError):
            continue
        if isinstance(row.get("score"), bool) or not isfinite(score) or not min_score <= score <= 100:
            continue
        prev = selected.get(pro)
        if prev is None or score > float(prev["score"]):
            selected[pro] = row
    return sorted(selected.values(), key=lambda row: (-float(row["score"]), row["pro_symbol"].upper()))[:policy.max_underlyings]


def validate_scanner_call(
    contract: dict[str, Any], parent: dict[str, Any], *,
    now: datetime, session_date: str, policy: USOptionsPolicy,
    option_threshold: float,
    session_close: datetime,
) -> dict[str, Any]:
    """Return a provenance-rich, strictly actionable snapshot row.

    Provider adapters MUST pass `bid_quote_at` and `ask_quote_at` as actual
    side-quote timestamps. Provider `update_time`, `lastTradeDate`, last trade
    price and local fetch time are not accepted as quote evidence.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if session_close.tzinfo is None or session_close.utcoffset() is None:
        raise InvalidOptionEvidence("US session close requires an exchange-aware timestamp")
    session_close = session_close.astimezone(timezone.utc)
    if now >= session_close:
        raise InvalidOptionEvidence("US regular session is closed")
    if now.astimezone(NY).date().isoformat() != session_date:
        raise InvalidOptionEvidence("not the expected New York session date")
    contract_key = canonical_contract(contract)
    exchange, symbol, expiry, strike, opt_type = contract_key
    if parent.get("source") != "scanner" or parent.get("market") != "US" or parent.get("scope") != "production":
        raise InvalidOptionEvidence("invalid scanner production parent")
    if parent.get("durably_persisted") is not True or not parent.get("scanner_setup_id"):
        raise InvalidOptionEvidence("underlying setup is not durably persisted")
    if (parent.get("actionable") is not True or parent.get("direction") != "BUY"
            or parent.get("session_date") != session_date):
        raise InvalidOptionEvidence("parent is not a current actionable BUY")
    pro_symbol = str(parent.get("pro_symbol") or "").upper()
    if pro_symbol != f"{exchange}:{symbol}":
        raise InvalidOptionEvidence("contract does not match its parent")
    if opt_type != "CALL":
        raise InvalidOptionEvidence("only CALL purchases are supported")
    dte = (date.fromisoformat(expiry) - now.astimezone(NY).date()).days
    if dte < policy.min_dte or dte > policy.max_dte:
        raise InvalidOptionEvidence("DTE outside scanner bounds")
    dte_reported = contract.get("dte")
    if dte_reported is not None and _nonnegative_integral(dte_reported, "dte") != dte:
        raise InvalidOptionEvidence("reported DTE disagrees with expiry")
    score = _positive_finite(contract.get("option_score"), "option_score")
    if score > 100 or score < option_threshold:
        raise InvalidOptionEvidence("score below threshold or invalid")
    parent_score = _positive_finite(parent.get("score"), "parent_score")
    if parent_score > 100:
        raise InvalidOptionEvidence("parent score must be <= 100")
    bid = _positive_finite(contract.get("bid"), "bid")
    ask = _positive_finite(contract.get("ask"), "ask")
    if ask < bid:
        raise InvalidOptionEvidence("crossed bid/ask market")
    mid = (ask + bid) / 2
    spread = (ask - bid) / mid * 100
    if spread > policy.max_spread_pct:
        raise InvalidOptionEvidence("spread too wide")
    oi = _nonnegative_integral(contract.get("oi"), "open interest")
    if policy.quote_mode == "reference" and contract.get("oi_source") != "yfinance_exact_expiry_strike":
        raise InvalidOptionEvidence("open interest source is missing or unverified")
    volume = _nonnegative_integral(contract.get("volume"), "session volume")
    min_oi = max(policy.min_open_interest, 100) if policy.quote_mode == "reference" else policy.min_open_interest
    min_volume = max(policy.min_volume, 10) if policy.quote_mode == "reference" else policy.min_volume
    if oi < min_oi or volume < min_volume:
        raise InvalidOptionEvidence("liquidity below minimum")
    if policy.quote_mode == "strict" and contract.get("volume_session_date") != session_date:
        raise InvalidOptionEvidence("volume is not confirmed for the current session")
    provider = str(contract.get("quote_provider") or "").strip()
    if not provider or provider == "none":
        raise InvalidOptionEvidence("bid/ask provider not identified")
    volume_date = contract.get("volume_session_date")
    if policy.quote_mode == "reference":
        raw_update = contract.get("tv_update_time")
        if isinstance(raw_update, (int, float)) and not isinstance(raw_update, bool):
            try:
                update_at = datetime.fromtimestamp(float(raw_update), timezone.utc)
            except (ValueError, OverflowError, OSError) as exc:
                raise InvalidOptionEvidence("tv_update_time is malformed") from exc
            age = (now - update_at).total_seconds()
            if age < -120 or age > 900:
                raise InvalidOptionEvidence("tv_update_time is stale or future-dated")
        else:
            update_at = _aware_timestamp(raw_update, "tv_update_time", now, 900)
        if update_at.astimezone(NY).date().isoformat() != session_date:
            raise InvalidOptionEvidence("TradingView update is not from the current session")
        from tradingview.scheduler.market_calendar import CalendarCoverageError, market_session_bounds
        try:
            expected_open, expected_close = market_session_bounds("US", session_date)
        except (ValueError, CalendarCoverageError) as exc:
            raise InvalidOptionEvidence("reference option is not from a US trading session") from exc
        if session_close != expected_close.astimezone(timezone.utc):
            raise InvalidOptionEvidence("session close does not match US trading calendar")
        if not expected_open.astimezone(timezone.utc) <= update_at <= session_close:
            raise InvalidOptionEvidence("TradingView update is outside regular session")
        if (volume_date != session_date and volume > 0
                and contract.get("volume_session_provenance") == "inferred_from_current_tv_update"):
            volume_date = session_date
        if volume_date != session_date:
            raise InvalidOptionEvidence("volume is not confirmed for the current session")
        if (contract.get("volume_session_provenance") != "inferred_from_current_tv_update"
                and contract.get("volume_session_provenance") != "provider_session_date"):
            raise InvalidOptionEvidence("current-session volume provenance is missing")
        # No side-quote times are asserted in reference mode. Expiry is anchored
        # to original provider update and original fetch, never recovery time.
        fetched_at = _aware_timestamp(contract.get("fetch_completed_at"), "fetch_completed_at", now, 120)
        if fetched_at > now:
            raise InvalidOptionEvidence("fetch completion is future-dated")
        expiry_at = min(fetched_at + timedelta(seconds=120), update_at + timedelta(minutes=15), session_close)
        bid_at = ask_at = None
        freshness = "reference_unverified"
    else:
        bid_at = _aware_timestamp(contract.get("bid_quote_at"), "bid_quote_at", now,
                                  policy.max_quote_age_seconds)
        ask_at = _aware_timestamp(contract.get("ask_quote_at"), "ask_quote_at", now,
                                  policy.max_quote_age_seconds)
        expiry_at = min(min(bid_at, ask_at, now) + timedelta(seconds=policy.max_quote_age_seconds),
                        session_close)
        freshness = "verified_bid_ask"
    if expiry_at <= now:
        raise InvalidOptionEvidence("quote validity already expired")
    modes = contract.get("strategy_modes") or []
    if not isinstance(modes, list) or not modes or any(not isinstance(v, str) or not v for v in modes):
        raise InvalidOptionEvidence("strategy modes missing")
    if parent.get("near_earnings") is True or contract.get("earnings_risk") is True:
        raise InvalidOptionEvidence("earnings risk")
    return {
        "source": "scanner", "market": "US", "scope": parent["scope"],
        "session_date": session_date, "exchange": exchange, "symbol": symbol,
        "option_type": "CALL", "contract_identifier": contract.get("contract_identifier"),
        "strike": float(strike), "expiry": expiry, "dte": dte,
        "option_score": score, "parent_signal_score": parent_score,
        "strategy_modes": sorted(set(mode.upper() for mode in modes)),
        "parent_setup_id": str(parent["scanner_setup_id"]),
        "parent_run_id": str(parent.get("run_id") or ""),
        "analysis_run_id": str(contract.get("analysis_run_id") or ""),
        "bid": bid, "ask": ask, "mid_price": mid, "premium_currency": "USD",
        "quote_provider": provider, "bid_quote_at": bid_at.isoformat() if bid_at else None,
        "ask_quote_at": ask_at.isoformat() if ask_at else None,
        "fetch_completed_at": str(contract.get("fetch_completed_at") or ""),
        "freshness_status": freshness, "quote_mode": policy.quote_mode,
        "tv_update_time": update_at.isoformat() if freshness == "reference_unverified" else None,
        "premium_basis": "reference · freshness unverified" if freshness == "reference_unverified" else "mid",
        "quote_max_age_seconds": policy.max_quote_age_seconds,
        "min_open_interest": min_oi,
        "min_volume": min_volume,
        "min_dte": policy.min_dte, "max_dte": policy.max_dte,
        "max_spread_pct": policy.max_spread_pct,
        "session_close_at": session_close.isoformat(),
        "recommendation_expires_at": expiry_at.isoformat(),
        "oi": oi, "volume": volume,
        "oi_asof": contract.get("oi_asof"),
        "volume_session_date": volume_date if freshness == "reference_unverified" else contract.get("volume_session_date"),
        "volume_session_provenance": (contract.get("volume_session_provenance") or
                                       ("provider_session_date" if volume_date == session_date else None)),
        "oi_source": contract.get("oi_source"),
    }


def select_contracts(
    scored_contracts: Iterable[dict[str, Any]], parent: dict[str, Any], *,
    now: datetime, session_date: str, policy: USOptionsPolicy,
    option_threshold: float, session_close: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Deterministic canonical dedup and rank; fail-closed rejection breakdown."""
    candidates: dict[tuple[str, ...], dict[str, Any]] = {}
    rejected: dict[str, int] = {}
    for raw in scored_contracts:
        try:
            item = validate_scanner_call(raw, parent, now=now, session_date=session_date,
                                         policy=policy, option_threshold=option_threshold,
                                         session_close=session_close)
            key = canonical_contract(item)
            existing = candidates.get(key)
            if existing is None or item["option_score"] > existing["option_score"]:
                item["strategy_modes"] = sorted(set(item["strategy_modes"] + (existing or {}).get("strategy_modes", [])))
                candidates[key] = item
            else:
                existing["strategy_modes"] = sorted(set(existing["strategy_modes"] + item["strategy_modes"]))
        except (InvalidOptionEvidence, TypeError, ValueError) as exc:
            reason = str(exc)
            rejected[reason] = rejected.get(reason, 0) + 1
    ordered = sorted(candidates.values(), key=lambda r: (-r["option_score"], canonical_contract(r)))
    return ordered[:policy.max_contracts_per_symbol], rejected


def scanner_option_identity(recipient: Any, row: dict[str, Any]) -> tuple[Any, ...]:
    """Source-specific stable identity ingredients; hash with existing _digest.

    Never includes run ID, mutable scoring, premium or quote timestamp.
    """
    if row.get("source") != "scanner" or row.get("market") != "US":
        raise InvalidOptionEvidence("not a scanner US contract")
    date.fromisoformat(str(row.get("session_date")))
    return (str(recipient), "option", "scanner", "US", row["session_date"], *canonical_contract(row))


def extract_validated_badge_calls(
    ranked: Iterable[dict[str, Any]], *, session_date: str, now: datetime,
    session_close: datetime, policy: USOptionsPolicy, parent_threshold: float,
    option_threshold: float, run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    """Convert completed producer badge data into scanner-origin options.

    Expects completed producer rows, NOT bare scanner candidates. The producer
    MUST set `scanner_setup_id` and `durably_persisted` after database success.
    No option fetched, scored or created here without an actionable parent.
    Actual chain access remains via existing OptionsAnalyzer.analyze_all_modes.
    """
    normalized_parents: list[dict[str, Any]] = []
    badges: dict[str, dict[str, Any]] = {}
    for item in ranked:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("tv_symbol") or "").strip().upper()
        parent = {
            "source": item.get("source"), "market": item.get("market"),
            "scope": item.get("scope"),
            "session_date": item.get("session_date"),
            "direction": item.get("direction"), "actionable": item.get("actionable"),
            "durably_persisted": item.get("durably_persisted"),
            "scanner_setup_id": item.get("scanner_setup_id"),
            "pro_symbol": sym, "score": item.get("composite"),
            "run_id": item.get("run_id"), "near_earnings": item.get("near_earnings"),
        }
        normalized_parents.append(parent)
        if sym:
            badges[sym] = item
    parents = select_actionable_parents(normalized_parents, session_date=session_date,
                                         min_score=parent_threshold, policy=policy)
    contracts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for parent in parents:
        symbol = parent["pro_symbol"]
        badge = badges[symbol].get("options_data")
        if not isinstance(badge, dict) or badge.get("error"):
            # Avoid persisting arbitrary error strings from adapters. Positive
            # capability gaps are reported separately from structured metadata.
            errors.append({"symbol": symbol, "message": "options provider did not complete successfully"})
            continue
        metadata = badge.get("meta") or {}
        if metadata.get("earnings_risk") is True:
            # This is a strategy-based suppression, not a provider failure.
            continue
        capabilities = metadata.get("scanner_provider_capabilities")
        if isinstance(capabilities, dict):
            missing = [
                label for field, label in (
                    ("independent_bid_ask_timestamps", "independent bid/ask timestamps"),
                    ("open_interest_with_provenance", "open interest provenance"),
                    ("session_volume_with_date", "current-session volume date"),
                )
                if (field != "independent_bid_ask_timestamps" or policy.quote_mode != "reference")
                and capabilities.get(field) is not True
            ]
            if missing:
                provider = str(capabilities.get("provider") or "options provider")
                errors.append({
                    "symbol": symbol,
                    "message": f"{provider} lacks required evidence: {', '.join(missing)}",
                })
        raw_contracts: list[dict[str, Any]] = []
        for mode in ("BALANCED", "AGGRESSIVE", "SAFE"):
            data = badge.get(mode) or {}
            if not isinstance(data, dict):
                errors.append({"symbol": symbol, "message": "options mode payload malformed"})
                continue
            for raw in data.get("top_contracts") or []:
                if isinstance(raw, dict):
                    item = dict(raw)
                    item["strategy_modes"] = [mode]
                    exchange, _, ticker = symbol.partition(":")
                    # Fill absent context, but never hide a provider mismatch.
                    item.setdefault("exchange", exchange)
                    item.setdefault("symbol", ticker)
                    item["analysis_run_id"] = run_id
                    # Provider adapter must explicitly identify bid/ask quote
                    # provenance; do not convert generic update_time to it.
                    if not item.get("quote_provider"):
                        item["quote_provider"] = item.get("source")
                    raw_contracts.append(item)
                else:
                    errors.append({"symbol": symbol, "message": "options contract payload malformed"})
        if not raw_contracts:
            # An absent chain cannot be treated as a proven successful no-match.
            if str(metadata.get("data_source") or "none").lower() == "none":
                errors.append({"symbol": symbol, "message": "options chain unavailable"})
            continue
        matches, rejected = select_contracts(
            raw_contracts, parent, now=now, session_date=session_date,
            session_close=session_close, policy=policy, option_threshold=option_threshold,
        )
        contracts.extend(matches)
        if not matches and any(("quote_at" in reason and policy.quote_mode == "strict")
                               or "provider" in reason or "volume is not confirmed" in reason
                               or (policy.quote_mode == "reference" and any(token in reason for token in (
                                   "tv_update_time", "TradingView update", "fetch_completed_at",
                                   "fetch completion", "open interest source", "quote validity",
                               )))
                               for reason in rejected):
            message = ("options provider lacks verified live quote evidence" if policy.quote_mode == "strict"
                       else "options provider lacks required quote or liquidity evidence")
            errors.append({"symbol": symbol, "message": message})
    status = ("partial" if contracts else "unavailable") if errors else (
        "success" if contracts else "empty")
    return contracts, errors, status


def validate_persisted_scanner_record(row: dict[str, Any], now: datetime) -> None:
    """Reject forged, expired, or extended scanner snapshots at consumer edge.

    The original deadline is persisted; it is never calculated relative to the
    current recovery time. A previously eligible row becomes ineligible once
    its original bid/ask validity or session close has passed.
    """
    if not isinstance(row, dict) or row.get("source") != "scanner" or row.get("market") != "US":
        raise InvalidOptionEvidence("not a scanner US snapshot")
    if row.get("scope") != "production" or row.get("freshness_status") not in {"verified_bid_ask", "reference_unverified"}:
        raise InvalidOptionEvidence("missing scanner origin or quote validation")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must include timezone")
    now = now.astimezone(timezone.utc)
    if now.astimezone(NY).date().isoformat() != row.get("session_date"):
        raise InvalidOptionEvidence("not the current New York session")
    for field in ("parent_setup_id", "parent_run_id", "analysis_run_id"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise InvalidOptionEvidence(f"missing {field}")
    for field in ("option_score", "parent_signal_score"):
        if _positive_finite(row.get(field), field) > 100:
            raise InvalidOptionEvidence(f"invalid {field}")
    modes = row.get("strategy_modes")
    if not isinstance(modes, list) or not modes or any(
            not isinstance(mode, str) or not mode.strip() for mode in modes):
        raise InvalidOptionEvidence("missing strategy modes")
    max_age = _nonnegative_integral(row.get("quote_max_age_seconds"), "max quote age")
    if not 0 < max_age <= 900:
        raise InvalidOptionEvidence("invalid quote validity limit")
    spread_cap = _positive_finite(row.get("max_spread_pct"), "max spread")
    if spread_cap > 100:
        raise InvalidOptionEvidence("invalid persisted spread bound")
    bid, ask = _positive_finite(row.get("bid"), "bid"), _positive_finite(row.get("ask"), "ask")
    if bid > ask or ((ask - bid) / ((ask + bid) / 2) * 100) > spread_cap:
        raise InvalidOptionEvidence("persisted quotes are crossed or too wide")
    mid = _positive_finite(row.get("mid_price"), "mid_price")
    if abs(mid - (bid + ask) / 2) > 1e-8:
        raise InvalidOptionEvidence("persisted midpoint disagrees with quote")
    if row.get("premium_currency") != "USD" or not str(row.get("quote_provider") or "").strip():
        raise InvalidOptionEvidence("missing quote currency or provider")
    if str(row.get("option_type") or "").upper() != "CALL":
        raise InvalidOptionEvidence("not a call")
    if row.get("volume_session_date") != row.get("session_date"):
        raise InvalidOptionEvidence("contract volume lacks session provenance")
    if _nonnegative_integral(row.get("oi"), "oi") < _nonnegative_integral(row.get("min_open_interest"), "min open interest"):
        raise InvalidOptionEvidence("OI below recorded policy")
    if _nonnegative_integral(row.get("volume"), "volume") < _nonnegative_integral(row.get("min_volume"), "min volume"):
        raise InvalidOptionEvidence("volume below recorded policy")
    contract_key = canonical_contract(row)
    dte = (date.fromisoformat(contract_key[2]) - date.fromisoformat(row["session_date"])).days
    if not _nonnegative_integral(row.get("min_dte"), "min DTE") <= dte <= _nonnegative_integral(row.get("max_dte"), "max DTE"):
        raise InvalidOptionEvidence("DTE outside recorded policy")
    def parse(raw: Any, label: str) -> datetime:
        if not isinstance(raw, str):
            raise InvalidOptionEvidence(f"missing {label}")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidOptionEvidence(f"malformed {label}") from exc
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise InvalidOptionEvidence(f"{label} must include timezone")
        return dt.astimezone(timezone.utc)
    close = parse(row.get("session_close_at"), "session_close_at")
    expires = parse(row.get("recommendation_expires_at"), "recommendation_expires_at")
    if close.astimezone(NY).date().isoformat() != row.get("session_date"):
        raise InvalidOptionEvidence("session close does not match session date")
    if row.get("freshness_status") == "reference_unverified":
        if row.get("quote_mode") != "reference" or row.get("premium_basis") != "reference · freshness unverified":
            raise InvalidOptionEvidence("reference premium label missing")
        update = _aware_timestamp(row.get("tv_update_time"), "tv_update_time", now, 900)
        fetched = parse(row.get("fetch_completed_at"), "fetch_completed_at")
        if fetched > now:
            raise InvalidOptionEvidence("fetch completion is future-dated")
        if row.get("oi_source") != "yfinance_exact_expiry_strike":
            raise InvalidOptionEvidence("open interest source is missing or unverified")
        from tradingview.scheduler.market_calendar import CalendarCoverageError, market_session_bounds
        try:
            expected_open, expected_close = market_session_bounds("US", row["session_date"])
        except (ValueError, KeyError, CalendarCoverageError) as exc:
            raise InvalidOptionEvidence("reference option is not from a US trading session") from exc
        if close != expected_close.astimezone(timezone.utc):
            raise InvalidOptionEvidence("recorded close does not match US trading calendar")
        if update.astimezone(NY).date().isoformat() != row.get("session_date"):
            raise InvalidOptionEvidence("TradingView update is not from the current session")
        if not expected_open.astimezone(timezone.utc) <= update <= close:
            raise InvalidOptionEvidence("TradingView update is outside regular session")
        if expires > min(fetched + timedelta(seconds=120), update + timedelta(minutes=15), close):
            raise InvalidOptionEvidence("reference recommendation validity was extended")
        if row.get("volume_session_provenance") not in {"provider_session_date", "inferred_from_current_tv_update"}:
            raise InvalidOptionEvidence("reference volume provenance missing")
    else:
        bid_at = parse(row.get("bid_quote_at"), "bid_quote_at")
        ask_at = parse(row.get("ask_quote_at"), "ask_quote_at")
        if expires > min(bid_at + timedelta(seconds=max_age), ask_at + timedelta(seconds=max_age), close):
            raise InvalidOptionEvidence("recommendation validity was extended")
    if expires <= now:
        raise InvalidOptionEvidence("original recommendation expired")
    if row.get("freshness_status") == "verified_bid_ask":
        _aware_timestamp(row.get("bid_quote_at"), "bid_quote_at", now, max_age)
        _aware_timestamp(row.get("ask_quote_at"), "ask_quote_at", now, max_age)
