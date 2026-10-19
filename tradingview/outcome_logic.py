"""Pure observational outcome and scoring-ledger semantics.

This module has no database, network, broker, or filesystem dependencies.
It evaluates long-only *observed* price behavior from point observations;
it is not a trade simulator and does not model fills, actual first-passage
order, intrabar ordering, fees, corporate-action adjustment, quote revisions,
market calendars, or short-side performance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Mapping


class MeasurementOrigin(str, Enum):
    FIRE = "fire"
    ANALYSIS = "analysis"


class OutcomeStatus(str, Enum):
    NOT_ELIGIBLE = "not_eligible"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    IMMATURE = "immature"
    OBSERVED_TARGET_FIRST = "observed_target_first"
    OBSERVED_STOP_FIRST = "observed_stop_first"
    MATURE_NO_OBSERVED_THRESHOLD_HIT = "mature_no_observed_threshold_hit"


class FireDisposition(str, Enum):
    CONSIDERED_NOT_ATTEMPTED = "considered_not_attempted"
    FILTERED = "filtered"
    DEDUPLICATED = "deduplicated"
    FAILED = "failed"
    COMPLETED = "completed"


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: float, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must not be boolean")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


@dataclass(frozen=True)
class PointObservation:
    observed_at: datetime
    price: float
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        price = _finite(self.price, "price")
        if price <= 0:
            raise ValueError("price must be positive")
        object.__setattr__(self, "price", price)
        if self.available_at < self.observed_at:
            raise ValueError("available_at cannot precede observed_at")


# Backward-compatible alias retained for import-time clarity. High/low range
# observations are intentionally unsupported in this version.
PriceObservation = PointObservation


@dataclass(frozen=True)
class SignalMeasurement:
    signal_id: str
    origin: MeasurementOrigin
    fire_at: datetime
    registered_at: datetime
    analysis_at: datetime | None
    entry_price: float | None
    entry_price_source: str | None
    entry_price_at: datetime | None
    entry_available_at: datetime | None
    target_return_pct: float
    stop_return_pct: float
    horizon: timedelta
    horizon_tolerance: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if not isinstance(self.signal_id, str) or not self.signal_id.strip():
            raise ValueError("signal_id must be nonempty")
        if not isinstance(self.origin, MeasurementOrigin):
            raise ValueError("origin must be a MeasurementOrigin")
        for name in ("fire_at", "registered_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.analysis_at is not None:
            object.__setattr__(self, "analysis_at", _utc(self.analysis_at, "analysis_at"))
        for name in ("entry_price_at", "entry_available_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))
        if self.entry_price is not None:
            price = _finite(self.entry_price, "entry_price")
            if price <= 0:
                raise ValueError("entry_price must be positive")
            object.__setattr__(self, "entry_price", price)
        source = _text(self.entry_price_source, "entry_price_source")
        if source is not None and not source.strip():
            raise ValueError("entry_price_source cannot be blank")
        target = _finite(self.target_return_pct, "target_return_pct")
        stop = _finite(self.stop_return_pct, "stop_return_pct")
        object.__setattr__(self, "target_return_pct", target)
        object.__setattr__(self, "stop_return_pct", stop)
        if target <= 0:
            raise ValueError("target_return_pct must be positive")
        if stop <= -100 or stop >= 0:
            raise ValueError("stop_return_pct must be strictly between -100 and 0")
        if not isinstance(self.horizon, timedelta) or self.horizon <= timedelta(0):
            raise ValueError("horizon must be a positive timedelta")
        if (
            not isinstance(self.horizon_tolerance, timedelta)
            or self.horizon_tolerance < timedelta(0)
        ):
            raise ValueError("horizon_tolerance must be a nonnegative timedelta")
        if self.horizon_tolerance >= self.horizon:
            raise ValueError("horizon_tolerance must be smaller than horizon")
        if self.origin is MeasurementOrigin.ANALYSIS and self.analysis_at is None:
            raise ValueError("analysis origin requires analysis_at")
        if self.origin is MeasurementOrigin.FIRE and self.analysis_at is not None:
            # Analysis time can coexist with a fire-origin measurement when kept
            # only as context; the origin rule below selects fire_at.
            pass
        # Partial historical provenance is unknown, not contradictory.
        # Present timestamps are still validated individually below.
        if self.entry_price_at is not None and self.entry_price_at > self.measurement_origin_at:
            raise ValueError("entry_price_at cannot be after the selected measurement origin")
        if (
            self.entry_price_at is not None
            and self.entry_available_at is not None
            and self.entry_available_at < self.entry_price_at
        ):
            raise ValueError("entry_available_at cannot precede entry_price_at")

    @property
    def measurement_origin_at(self) -> datetime:
        if self.origin is MeasurementOrigin.FIRE:
            return self.fire_at
        assert self.analysis_at is not None
        return self.analysis_at

    @property
    def expiry_at(self) -> datetime:
        return self.measurement_origin_at + self.horizon

    def entry_eligible(self, as_of: datetime) -> bool:
        return bool(
            self.entry_price is not None
            and self.entry_price_source
            and self.entry_price_at is not None
            and self.entry_available_at is not None
            and self.entry_available_at <= as_of
        )


@dataclass(frozen=True)
class OutcomeEvaluation:
    signal_id: str
    status: OutcomeStatus
    expiry_at: datetime
    eligible: bool
    observed: bool
    mature: bool
    measured: bool
    target_touched: bool
    target_touch_at: datetime | None
    max_observed_return_pct: float | None
    fixed_horizon_return_pct: float | None
    horizon_observed_at: datetime | None
    horizon_offset: timedelta | None
    ineligibility_reason: str | None = None


@dataclass(frozen=True)
class OutcomeDenominators:
    registered: int
    eligible: int
    observed: int
    mature: int
    measured: int
    target_touched: int


@dataclass(frozen=True)
class OutcomeSummary:
    denominators: OutcomeDenominators
    status_counts: Mapping[OutcomeStatus, int]
    max_observed_returns_pct: tuple[float, ...]
    fixed_horizon_returns_pct: tuple[float, ...]


def _outcome(
    signal: SignalMeasurement,
    status: OutcomeStatus,
    *,
    eligible: bool = False,
    observed: bool = False,
    mature: bool = False,
    measured: bool = False,
    target_touched: bool = False,
    target_touch_at: datetime | None = None,
    max_return: float | None = None,
    fixed_return: float | None = None,
    horizon_observed_at: datetime | None = None,
    horizon_offset: timedelta | None = None,
    ineligibility_reason: str | None = None,
) -> OutcomeEvaluation:
    return OutcomeEvaluation(
        signal.signal_id,
        status,
        signal.expiry_at,
        eligible,
        observed,
        mature,
        measured,
        target_touched,
        target_touch_at,
        max_return,
        fixed_return,
        horizon_observed_at,
        horizon_offset,
        ineligibility_reason,
    )


def _deduplicated_points(points: Iterable[PointObservation]) -> tuple[PointObservation, ...]:
    by_time: dict[datetime, PointObservation] = {}
    for point in points:
        if not isinstance(point, PointObservation):
            raise ValueError("observations must be PointObservation values")
        existing = by_time.get(point.observed_at)
        if existing is None:
            by_time[point.observed_at] = point
            continue
        if existing.price != point.price or existing.available_at != point.available_at:
            raise ValueError("conflicting observations for the same timestamp")
    return tuple(sorted(by_time.values(), key=lambda item: item.observed_at))


def evaluate_outcome(
    signal: SignalMeasurement,
    observations: Iterable[PointObservation],
    *,
    as_of: datetime,
) -> OutcomeEvaluation:
    """Evaluate one long-only observational signal.

    Threshold classifications describe the first threshold detected in the
    supplied point samples, not actual market first-passage or execution order.
    """
    as_of = _utc(as_of, "as_of")
    origin = signal.measurement_origin_at
    if as_of < origin:
        raise ValueError("as_of cannot precede measurement origin")

    expiry = signal.expiry_at
    mature = as_of >= expiry
    if not signal.entry_eligible(as_of):
        fields = (signal.entry_price, signal.entry_price_source,
                  signal.entry_price_at, signal.entry_available_at)
        reason = "missing_entry_provenance" if any(value is None for value in fields) else "entry_not_available"
        return _outcome(signal, OutcomeStatus.NOT_ELIGIBLE, mature=mature,
                        ineligibility_reason=reason)

    points = _deduplicated_points(observations)
    available = tuple(
        point
        for point in points
        if point.observed_at >= origin and point.available_at <= as_of
    )
    event_points = tuple(point for point in available if point.observed_at <= expiry)

    fixed_return = None
    horizon_observed_at = None
    horizon_offset = None
    if mature:
        horizon_start = expiry - signal.horizon_tolerance
        horizon_end = expiry + signal.horizon_tolerance
        horizon_points = tuple(
            point
            for point in available
            if horizon_start <= point.observed_at <= horizon_end
        )
        if horizon_points:
            selected = min(
                horizon_points,
                key=lambda point: (abs(point.observed_at - expiry), point.observed_at),
            )
            horizon_observed_at = selected.observed_at
            horizon_offset = selected.observed_at - expiry
            entry_for_horizon = signal.entry_price
            assert entry_for_horizon is not None
            fixed_return = (selected.price / entry_for_horizon - 1) * 100

    if not event_points:
        return _outcome(
            signal,
            OutcomeStatus.INSUFFICIENT_OBSERVATIONS,
            eligible=True,
            mature=mature,
            measured=fixed_return is not None,
            fixed_return=fixed_return,
            horizon_observed_at=horizon_observed_at,
            horizon_offset=horizon_offset,
        )

    entry_price = signal.entry_price
    assert entry_price is not None
    target_level = entry_price * (1 + signal.target_return_pct / 100)
    stop_level = entry_price * (1 + signal.stop_return_pct / 100)
    if not math.isfinite(target_level) or not math.isfinite(stop_level):
        raise ValueError("threshold calculation overflow")
    # One representational ULP at each threshold, not a market-price tolerance.
    target_tolerance = math.ulp(target_level)
    stop_tolerance = math.ulp(stop_level)

    target_touched_at = next(
        (point.observed_at for point in event_points if point.price >= target_level - target_tolerance),
        None,
    )
    stop_touched_at = next(
        (point.observed_at for point in event_points if point.price <= stop_level + stop_tolerance),
        None,
    )
    max_return = max((point.price / entry_price - 1) * 100 for point in event_points)

    if target_touched_at is not None and (
        stop_touched_at is None or target_touched_at <= stop_touched_at
    ):
        status = OutcomeStatus.OBSERVED_TARGET_FIRST
    elif stop_touched_at is not None:
        status = OutcomeStatus.OBSERVED_STOP_FIRST
    elif mature:
        status = OutcomeStatus.MATURE_NO_OBSERVED_THRESHOLD_HIT
    else:
        status = OutcomeStatus.IMMATURE

    return _outcome(
        signal,
        status,
        eligible=True,
        observed=True,
        mature=mature,
        measured=fixed_return is not None,
        target_touched=target_touched_at is not None,
        target_touch_at=target_touched_at,
        max_return=max_return,
        fixed_return=fixed_return,
        horizon_observed_at=horizon_observed_at,
        horizon_offset=horizon_offset,
    )


def summarize_outcomes(evaluations: Iterable[OutcomeEvaluation]) -> OutcomeSummary:
    values = list(evaluations)
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, OutcomeEvaluation):
            raise ValueError("evaluations must be OutcomeEvaluation values")
        if value.signal_id in seen:
            raise ValueError("duplicate signal_id in summary")
        if not isinstance(value.signal_id, str) or not value.signal_id.strip():
            raise ValueError("signal_id must be nonempty")
        if not isinstance(value.status, OutcomeStatus):
            raise ValueError("invalid outcome status")
        if any(type(getattr(value, name)) is not bool for name in
               ("eligible", "observed", "mature", "measured", "target_touched")):
            raise ValueError("outcome flags must be booleans")
        if value.measured != (value.fixed_horizon_return_pct is not None):
            raise ValueError("measured flag must match fixed-horizon return")
        if value.measured and not (value.eligible and value.mature):
            raise ValueError("measured requires eligible and mature")
        if (value.horizon_observed_at is not None) != value.measured or (value.horizon_offset is not None) != value.measured:
            raise ValueError("horizon metadata must match measured flag")
        if value.measured and value.horizon_observed_at - value.expiry_at != value.horizon_offset:
            raise ValueError("inconsistent horizon offset")
        if value.observed and not value.eligible:
            raise ValueError("observed requires eligible")
        if value.observed != (value.max_observed_return_pct is not None):
            raise ValueError("observed flag must match maximum return")
        if value.target_touched != (value.target_touch_at is not None):
            raise ValueError("target touch metadata mismatch")
        if value.target_touched and not value.observed:
            raise ValueError("target touch requires event observations")
        if value.target_touch_at is not None and value.target_touch_at > value.expiry_at:
            raise ValueError("target touch after expiry")
        for number in (value.fixed_horizon_return_pct, value.max_observed_return_pct):
            if number is not None:
                _finite(number, "return")
        if (value.status is OutcomeStatus.NOT_ELIGIBLE) != (not value.eligible):
            raise ValueError("status inconsistent with eligibility")
        if value.eligible and (value.status is OutcomeStatus.INSUFFICIENT_OBSERVATIONS) != (not value.observed):
            raise ValueError("status inconsistent with event observations")
        if value.status is OutcomeStatus.MATURE_NO_OBSERVED_THRESHOLD_HIT and (not value.mature or value.target_touched):
            raise ValueError("invalid mature no-hit status")
        if value.status is OutcomeStatus.IMMATURE and (value.mature or value.target_touched):
            raise ValueError("invalid immature status")
        if value.status is OutcomeStatus.OBSERVED_TARGET_FIRST and not value.target_touched:
            raise ValueError("target-first requires target touch")
        seen.add(value.signal_id)
    counts = {status: sum(item.status is status for item in values) for status in OutcomeStatus}
    denominators = OutcomeDenominators(
        registered=len(values),
        eligible=sum(item.eligible for item in values),
        observed=sum(item.observed for item in values),
        mature=sum(item.mature for item in values),
        measured=sum(item.measured for item in values),
        target_touched=sum(item.target_touched for item in values),
    )
    fixed_returns = tuple(
        item.fixed_horizon_return_pct
        for item in values
        if item.fixed_horizon_return_pct is not None
    )
    if len(fixed_returns) != denominators.measured:
        raise ValueError("fixed-horizon distribution does not match measured denominator")
    return OutcomeSummary(
        denominators,
        counts,
        tuple(
            item.max_observed_return_pct
            for item in values
            if item.max_observed_return_pct is not None
        ),
        fixed_returns,
    )


@dataclass(frozen=True)
class FireIdentity:
    pro_symbol: str
    fire_time: str
    resolution: str

    def __post_init__(self) -> None:
        for name in ("pro_symbol", "fire_time", "resolution"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty")


@dataclass(frozen=True)
class FireAttempt:
    attempt_id: str
    fire: FireIdentity
    state: FireDisposition
    considered_at: datetime | None = None
    reason: str | None = None
    completion_ref: str | None = None
    selection_dedup_key: str | None = None
    deduplicated_against: FireIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("attempt_id must be nonempty")
        if not isinstance(self.fire, FireIdentity):
            raise ValueError("fire must be a FireIdentity")
        if not isinstance(self.state, FireDisposition):
            raise ValueError("state must be a FireDisposition")
        if self.considered_at is not None:
            object.__setattr__(
                self, "considered_at", _utc(self.considered_at, "considered_at")
            )
        reason = _text(self.reason, "reason")
        completion_ref = _text(self.completion_ref, "completion_ref")
        if reason is not None and not reason.strip():
            raise ValueError("reason cannot be blank")
        if completion_ref is not None and not completion_ref.strip():
            raise ValueError("completion_ref cannot be blank")
        if self.state is FireDisposition.DEDUPLICATED:
            if not isinstance(self.selection_dedup_key, str) or not self.selection_dedup_key.strip() or not isinstance(self.deduplicated_against, FireIdentity):
                raise ValueError("selection deduplication requires key and referenced fire")
        elif self.selection_dedup_key is not None or self.deduplicated_against is not None:
            raise ValueError("deduplication metadata only belongs to deduplicated attempts")
        if self.state is FireDisposition.CONSIDERED_NOT_ATTEMPTED and reason is not None:
            raise ValueError("not-attempted records cannot have a failure/filter reason")
        if self.state is FireDisposition.COMPLETED:
            if not completion_ref:
                raise ValueError("completed attempts require completion_ref")
            if reason is not None:
                raise ValueError("completed attempts cannot have a failure/filter reason")
        elif completion_ref is not None:
            raise ValueError("only completed attempts can have completion_ref")
        if self.state is FireDisposition.FAILED and not reason:
            raise ValueError("failed attempts require a reason")
        if self.state is FireDisposition.FILTERED and not reason:
            raise ValueError("filtered attempts require a reason")


@dataclass(frozen=True)
class FireLedgerEntry:
    fire: FireIdentity
    disposition: FireDisposition
    attempt_ids: tuple[str, ...]
    considered: bool = True
    attempts: tuple[FireAttempt, ...] = ()


def classify_fire_batch(attempts: Iterable[FireAttempt]) -> tuple[FireLedgerEntry, ...]:
    """Classify explicit attempts without inferring success from no error."""
    values = list(attempts)
    seen_attempts: dict[str, FireAttempt] = {}
    for attempt in values:
        if not isinstance(attempt, FireAttempt):
            raise ValueError("attempts must be FireAttempt values")
        existing = seen_attempts.get(attempt.attempt_id)
        if existing is not None:
            if existing != attempt:
                raise ValueError("conflicting duplicate attempt_id")
            continue
        seen_attempts[attempt.attempt_id] = attempt

    grouped: dict[FireIdentity, list[FireAttempt]] = {}
    for attempt in seen_attempts.values():
        grouped.setdefault(attempt.fire, []).append(attempt)

    entries: list[FireLedgerEntry] = []
    # Rollup means "ever completed", NOT latest attempt or chronological state.
    # Preserve every unique attempt as audit evidence; IDs only sort presentation.
    priority = (FireDisposition.COMPLETED, FireDisposition.FAILED,
                FireDisposition.FILTERED, FireDisposition.CONSIDERED_NOT_ATTEMPTED,
                FireDisposition.DEDUPLICATED)
    for fire in sorted(grouped, key=lambda f: (f.pro_symbol, f.fire_time, f.resolution)):
        ordered = tuple(sorted(grouped[fire], key=lambda a: a.attempt_id))
        states = {attempt.state for attempt in ordered}
        disposition = next(state for state in priority if state in states)
        entries.append(FireLedgerEntry(fire, disposition,
                       tuple(a.attempt_id for a in ordered), attempts=ordered))
    return tuple(entries)
