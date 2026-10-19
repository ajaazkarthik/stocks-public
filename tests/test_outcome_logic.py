from datetime import datetime, timedelta, timezone

import pytest

from tradingview.outcome_logic import (
    FireAttempt,
    FireDisposition,
    FireIdentity,
    MeasurementOrigin,
    OutcomeStatus,
    PointObservation,
    SignalMeasurement,
    classify_fire_batch,
    evaluate_outcome,
    summarize_outcomes,
)


UTC = timezone.utc
FIRE = datetime(2026, 1, 2, 9, tzinfo=UTC)
ORIGIN = datetime(2026, 1, 2, 10, tzinfo=UTC)
EXPIRY = ORIGIN + timedelta(hours=24)


def signal(**overrides):
    values = {
        "signal_id": "signal-1",
        "origin": MeasurementOrigin.ANALYSIS,
        "fire_at": FIRE,
        "registered_at": ORIGIN,
        "analysis_at": ORIGIN,
        "entry_price": 100.0,
        "entry_price_source": "synthetic-close",
        "entry_price_at": ORIGIN,
        "entry_available_at": ORIGIN,
        "target_return_pct": 10.0,
        "stop_return_pct": -5.0,
        "horizon": timedelta(hours=24),
        "horizon_tolerance": timedelta(hours=1),
    }
    values.update(overrides)
    return SignalMeasurement(**values)


def point(hours, price, *, available_at=None, start=ORIGIN):
    observed_at = start + timedelta(hours=hours)
    return PointObservation(observed_at, price, available_at or observed_at)


def fire(symbol="NASDAQ:A", time="2026-01-02T09:00:00+00:00", resolution="60"):
    return FireIdentity(symbol, time, resolution)


def attempt(
    attempt_id="attempt-1",
    *,
    fire_identity=None,
    state=FireDisposition.COMPLETED,
    reason=None,
    completion_ref="result-1",
):
    if state is FireDisposition.COMPLETED:
        if completion_ref is not None:
            reason = None
    else:
        completion_ref = None
    if state in (FireDisposition.FAILED, FireDisposition.FILTERED) and reason is None:
        reason = state.value
    return FireAttempt(
        attempt_id=attempt_id,
        fire=fire_identity or fire(),
        state=state,
        considered_at=ORIGIN,
        reason=reason,
        completion_ref=completion_ref,
    )


def test_fire_origin_and_analysis_origin_have_distinct_expiry():
    fire_origin = signal(
        origin=MeasurementOrigin.FIRE,
        entry_price_at=FIRE,
        entry_available_at=FIRE,
        analysis_at=None,
    )
    analysis_origin = signal()
    later = ORIGIN + timedelta(hours=25)
    fire_result = evaluate_outcome(fire_origin, [point(24, 110, start=FIRE)], as_of=later)
    analysis_result = evaluate_outcome(analysis_origin, [point(24, 101)], as_of=later)
    assert fire_result.expiry_at == FIRE + timedelta(hours=24)
    assert analysis_result.expiry_at == EXPIRY
    assert fire_result.status is OutcomeStatus.OBSERVED_TARGET_FIRST
    assert analysis_result.status is OutcomeStatus.MATURE_NO_OBSERVED_THRESHOLD_HIT


def test_registration_is_not_an_implicit_analysis_time():
    item = signal(analysis_at=ORIGIN + timedelta(hours=2), entry_price_at=ORIGIN + timedelta(hours=2), entry_available_at=ORIGIN + timedelta(hours=2))
    assert item.measurement_origin_at == ORIGIN + timedelta(hours=2)
    assert item.expiry_at == ORIGIN + timedelta(hours=26)


def test_retrospective_fire_origin_with_known_provenance_is_eligible():
    item = signal(origin=MeasurementOrigin.FIRE, analysis_at=None, registered_at=ORIGIN, entry_price_at=FIRE, entry_available_at=FIRE)
    result = evaluate_outcome(item, [point(2, 101, start=FIRE)], as_of=FIRE + timedelta(hours=3))
    assert result.eligible is True
    assert result.status is OutcomeStatus.IMMATURE


def test_missing_and_unavailable_entry_provenance_are_not_eligible():
    missing = signal(entry_price=None, entry_price_source=None, entry_price_at=None, entry_available_at=None)
    unavailable = signal(entry_available_at=ORIGIN + timedelta(hours=3))
    later = ORIGIN + timedelta(hours=2)
    missing_result = evaluate_outcome(missing, [point(1, 101)], as_of=later)
    unavailable_result = evaluate_outcome(unavailable, [point(1, 101)], as_of=later)
    assert missing_result.status is OutcomeStatus.NOT_ELIGIBLE
    assert unavailable_result.status is OutcomeStatus.NOT_ELIGIBLE


def test_contradictory_entry_provenance_fails_validation():
    assert signal(entry_price_at=None).entry_eligible(ORIGIN) is False
    with pytest.raises(ValueError, match="measurement origin"):
        signal(entry_price_at=ORIGIN + timedelta(seconds=1))


def test_evaluation_before_origin_is_rejected():
    with pytest.raises(ValueError, match="precede"):
        evaluate_outcome(signal(), [], as_of=ORIGIN - timedelta(seconds=1))


def test_post_expiry_threshold_does_not_affect_event_status():
    result = evaluate_outcome(
        signal(horizon_tolerance=timedelta(hours=2)),
        [point(25, 115)],
        as_of=ORIGIN + timedelta(hours=30),
    )
    assert result.status is OutcomeStatus.INSUFFICIENT_OBSERVATIONS
    assert result.target_touched is False
    assert result.measured is True
    assert result.fixed_horizon_return_pct == pytest.approx(15.0)


def test_delayed_availability_is_excluded_at_as_of():
    future_available = point(1, 111, available_at=ORIGIN + timedelta(hours=5))
    result = evaluate_outcome(signal(), [future_available], as_of=ORIGIN + timedelta(hours=2))
    assert result.status is OutcomeStatus.INSUFFICIENT_OBSERVATIONS


def test_exact_expiry_and_tolerance_boundaries_are_inclusive():
    at_expiry = point(24, 110)
    result = evaluate_outcome(signal(), [at_expiry], as_of=EXPIRY)
    assert result.status is OutcomeStatus.OBSERVED_TARGET_FIRST
    assert result.measured is True
    assert result.horizon_offset == timedelta(0)


def test_post_expiry_horizon_point_measures_without_events():
    result = evaluate_outcome(
        signal(horizon_tolerance=timedelta(hours=2)),
        [point(25, 104)],
        as_of=ORIGIN + timedelta(hours=30),
    )
    assert result.status is OutcomeStatus.INSUFFICIENT_OBSERVATIONS
    assert result.observed is False
    assert result.measured is True
    assert result.fixed_horizon_return_pct == pytest.approx(4.0)
    assert result.horizon_offset == timedelta(hours=1)
    summary = summarize_outcomes([result])
    assert summary.denominators.measured == 1
    assert summary.denominators.observed == 0


def test_nearest_horizon_tie_chooses_earlier_point():
    result = evaluate_outcome(
        signal(),
        [point(24.5, 102), point(23.5, 103)],
        as_of=ORIGIN + timedelta(hours=30),
    )
    assert result.horizon_observed_at == ORIGIN + timedelta(hours=23.5)
    assert result.fixed_horizon_return_pct == pytest.approx(3.0)


def test_missing_horizon_measurement_is_none_and_not_summarized():
    result = evaluate_outcome(signal(), [point(2, 101)], as_of=ORIGIN + timedelta(hours=48))
    assert result.status is OutcomeStatus.MATURE_NO_OBSERVED_THRESHOLD_HIT
    assert result.measured is False
    assert result.fixed_horizon_return_pct is None
    summary = summarize_outcomes([result])
    assert summary.denominators.measured == 0
    assert summary.fixed_horizon_returns_pct == ()


def test_sparse_mature_sample_is_not_complete_coverage():
    result = evaluate_outcome(signal(), [point(2, 101)], as_of=ORIGIN + timedelta(hours=48))
    assert result.status is OutcomeStatus.MATURE_NO_OBSERVED_THRESHOLD_HIT


def test_stop_observation_followed_by_target_touch_preserves_stop_first():
    result = evaluate_outcome(signal(), [point(2, 94), point(4, 111)], as_of=ORIGIN + timedelta(hours=5))
    assert result.status is OutcomeStatus.OBSERVED_STOP_FIRST
    assert result.target_touched is True
    assert result.target_touch_at == ORIGIN + timedelta(hours=4)


def test_max_observed_return_can_be_negative_and_stays_in_event_window():
    result = evaluate_outcome(
        signal(horizon_tolerance=timedelta(hours=2)),
        [point(2, 90), point(25, 130)],
        as_of=ORIGIN + timedelta(hours=30),
    )
    assert result.max_observed_return_pct == pytest.approx(-10.0)


def test_exact_duplicate_points_are_deduplicated_and_input_permutation_is_stable():
    observations = [point(2, 101), point(4, 111)]
    duplicate = observations + [point(4, 111)]
    reversed_order = list(reversed(duplicate))
    first = evaluate_outcome(signal(), duplicate, as_of=ORIGIN + timedelta(hours=5))
    second = evaluate_outcome(signal(), reversed_order, as_of=ORIGIN + timedelta(hours=5))
    assert first == second


def test_conflicting_point_timestamps_are_rejected():
    with pytest.raises(ValueError, match="conflicting"):
        evaluate_outcome(signal(), [point(2, 101), point(2, 102)], as_of=ORIGIN + timedelta(hours=3))


def test_numeric_and_timestamp_validation():
    with pytest.raises(ValueError, match="finite"):
        point(1, float("nan"))
    with pytest.raises(ValueError, match="positive"):
        point(1, 0)
    with pytest.raises(ValueError, match="between -100"):
        signal(stop_return_pct=-100)
    with pytest.raises(ValueError, match="smaller than horizon"):
        signal(horizon_tolerance=timedelta(hours=24))
    with pytest.raises(ValueError, match="signal_id"):
        signal(signal_id=" ")
    with pytest.raises(ValueError, match="available_at"):
        PointObservation(ORIGIN, 100, ORIGIN - timedelta(seconds=1))


def test_summary_denominators_and_invariants():
    measured = evaluate_outcome(signal(), [point(2, 101), point(24, 102)], as_of=ORIGIN + timedelta(hours=30))
    not_eligible = evaluate_outcome(
        signal(signal_id="unknown", entry_price=None, entry_price_source=None, entry_price_at=None, entry_available_at=None),
        [point(2, 115)],
        as_of=ORIGIN + timedelta(hours=30),
    )
    summary = summarize_outcomes([measured, not_eligible])
    assert summary.denominators.registered == 2
    assert summary.denominators.eligible == 1
    assert summary.denominators.observed == 1
    assert summary.denominators.mature == 2
    assert summary.denominators.measured == 1
    assert summary.fixed_horizon_returns_pct == pytest.approx((2.0,))
    assert sum(summary.status_counts.values()) == 2
    assert summarize_outcomes([]).denominators.registered == 0


def test_duplicate_signal_ids_are_rejected():
    result = evaluate_outcome(signal(), [point(2, 101)], as_of=ORIGIN + timedelta(hours=3))
    with pytest.raises(ValueError, match="duplicate signal_id"):
        summarize_outcomes([result, result])


def test_not_attempted_is_not_completed_scoring():
    entries = classify_fire_batch([attempt(state=FireDisposition.CONSIDERED_NOT_ATTEMPTED)])
    assert entries[0].disposition is FireDisposition.CONSIDERED_NOT_ATTEMPTED


def test_filtered_and_failed_records_are_not_marked_completed():
    entries = classify_fire_batch(
        [
            attempt("a1", fire_identity=fire("NASDAQ:A"), state=FireDisposition.FILTERED, reason="exchange"),
            attempt("a2", fire_identity=fire("NASDAQ:B"), state=FireDisposition.FAILED, reason="exception"),
        ]
    )
    assert [entry.disposition for entry in entries] == [FireDisposition.FILTERED, FireDisposition.FAILED]


def test_failed_attempt_with_explicit_retry_completes():
    identity = fire()
    entries = classify_fire_batch(
        [
            attempt("a1", fire_identity=identity, state=FireDisposition.FAILED, reason="first failure"),
            attempt("a2", fire_identity=identity, state=FireDisposition.COMPLETED, completion_ref="result-2"),
        ]
    )
    assert entries[0].disposition is FireDisposition.COMPLETED
    assert entries[0].attempt_ids == ("a1", "a2")


def test_conflicting_attempts_are_rejected():
    identity = fire()
    with pytest.raises(ValueError, match="conflicting duplicate attempt_id"):
        classify_fire_batch(
            [
                attempt("same", fire_identity=identity, state=FireDisposition.FAILED, reason="a"),
                attempt("same", fire_identity=identity, state=FireDisposition.COMPLETED),
            ]
        )


def test_distinct_same_symbol_fires_are_not_selection_duplicates():
    identity_1 = fire("NASDAQ:A", "t1", "60")
    identity_2 = fire("NASDAQ:A", "t2", "60")
    entries = classify_fire_batch(
        [
            attempt("a1", fire_identity=identity_1),
            attempt("a2", fire_identity=identity_2),
        ]
    )
    assert len(entries) == 2
    assert all(entry.disposition is FireDisposition.COMPLETED for entry in entries)


def test_attempt_validation_is_explicit():
    with pytest.raises(ValueError, match="completion_ref"):
        attempt(state=FireDisposition.COMPLETED, completion_ref=None)
    with pytest.raises(ValueError, match="reason"):
        attempt(state=FireDisposition.FAILED, reason=" ")
    with pytest.raises(ValueError, match="only completed"):
        FireAttempt(
            attempt_id="bad",
            fire=fire(),
            state=FireDisposition.FAILED,
            reason="failure",
            completion_ref="wrong",
        )


@pytest.mark.parametrize("missing", ["entry_price", "entry_price_source", "entry_price_at", "entry_available_at"])
def test_partial_provenance_is_unknown(missing):
    result = evaluate_outcome(signal(**{missing: None}), [point(1, 111)], as_of=ORIGIN + timedelta(hours=2))
    assert result.status is OutcomeStatus.NOT_ELIGIBLE
    assert result.ineligibility_reason == "missing_entry_provenance"


def test_numeric_strings_normalized_and_booleans_rejected():
    item = signal(target_return_pct="10", stop_return_pct="-5")
    assert item.target_return_pct == 10.0
    assert evaluate_outcome(item, [point(1, 111)], as_of=ORIGIN + timedelta(hours=2)).target_touched
    with pytest.raises(ValueError):
        signal(target_return_pct=True)


@pytest.mark.parametrize("price", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_prices_rejected(price):
    with pytest.raises(ValueError):
        point(1, price)


def test_range_and_naive_datetime_rejected():
    with pytest.raises(TypeError):
        PointObservation(ORIGIN, 100, ORIGIN, high=111)
    with pytest.raises(ValueError):
        PointObservation(ORIGIN.replace(tzinfo=None), 100, ORIGIN)


def test_conflicting_availability_rejected():
    with pytest.raises(ValueError, match="conflicting"):
        evaluate_outcome(signal(), [point(1, 101), point(1, 101, available_at=ORIGIN + timedelta(hours=2))], as_of=EXPIRY)


@pytest.mark.parametrize("hours, measured", [(23, True), (25, True), (22.999, False), (25.001, False)])
def test_horizon_tolerance_boundaries(hours, measured):
    result = evaluate_outcome(signal(), [point(hours, 101)], as_of=ORIGIN + timedelta(hours=30))
    assert result.measured is measured


def test_unavailable_future_and_preorigin_points_excluded():
    result = evaluate_outcome(signal(), [point(-1, 111), point(3, 111)], as_of=ORIGIN + timedelta(hours=2))
    assert not result.observed and not result.target_touched
    result = evaluate_outcome(signal(), [point(24, 101, available_at=ORIGIN + timedelta(hours=26))], as_of=EXPIRY)
    assert not result.measured


def test_threshold_near_misses_not_rounded_into_hits():
    result = evaluate_outcome(signal(), [point(1, 109.999999), point(2, 95.000001)], as_of=ORIGIN + timedelta(hours=3))
    assert result.status is OutcomeStatus.IMMATURE
    stop = evaluate_outcome(signal(), [point(1, 95)], as_of=ORIGIN + timedelta(hours=2))
    assert stop.status is OutcomeStatus.OBSERVED_STOP_FIRST


def test_summary_rejects_individually_inconsistent_evaluations():
    from dataclasses import replace
    measured = evaluate_outcome(signal(), [point(24, 101)], as_of=EXPIRY)
    unmeasured = evaluate_outcome(signal(signal_id="other"), [point(1, 101)], as_of=EXPIRY)
    with pytest.raises(ValueError, match="measured flag"):
        summarize_outcomes([replace(measured, measured=False), replace(unmeasured, measured=True)])
    with pytest.raises(ValueError, match="target touch"):
        summarize_outcomes([replace(unmeasured, target_touched=True)])


def test_ledger_preserves_evidence_and_has_stable_rollup():
    identity = fire()
    values = [attempt("z-failure", fire_identity=identity, state=FireDisposition.FAILED, reason="timeout"),
              attempt("a-success", fire_identity=identity, completion_ref="result-proof")]
    result = classify_fire_batch(values)
    assert result == classify_fire_batch(reversed(values))
    assert result == classify_fire_batch(values + [values[0]])
    assert {a.reason for a in result[0].attempts} == {None, "timeout"}
    assert result[0].attempts[0].completion_ref == "result-proof"
    assert result[0].disposition is FireDisposition.COMPLETED


def test_selection_dedup_is_explicit_not_fire_identity():
    original = fire(time="t1")
    suppressed = fire(time="t2")
    record = FireAttempt("suppressed", suppressed, FireDisposition.DEDUPLICATED,
                         reason="same-symbol selection", selection_dedup_key="NASDAQ:A",
                         deduplicated_against=original)
    result = classify_fire_batch([record, attempt(fire_identity=original)])
    assert len(result) == 2
    assert result[1].attempts[0].deduplicated_against == original
    with pytest.raises(ValueError, match="requires key"):
        FireAttempt("bad", suppressed, FireDisposition.DEDUPLICATED)
