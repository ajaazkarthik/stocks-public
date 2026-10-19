"""Isolated, deterministic policy tests. No DB, network or Telegram access."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import pytest

from tradingview.scheduler.us_options_policy import (
    USOptionsPolicy, InvalidOptionEvidence, canonical_contract,
    scanner_option_identity, select_actionable_parents, validate_scanner_call, select_contracts,
)

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)  # NY 11am, regular US weekday
DATE = "2026-10-19"
SESSION_CLOSE = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)


def parent(**kw):
    return {"source": "scanner", "market": "US", "session_date": DATE, "direction": "BUY",
            "actionable": True, "scope": "production", "durably_persisted": True, "scanner_setup_id": "s1",
            "pro_symbol": "NYSE:RTX", "score": 75.5, "run_id": "original", **kw}


def option(**kw):
    result = {"exchange": "NYSE", "symbol": "RTX", "option_type": "CALL", "strike": 195,
              "expiry": "2026-11-06", "dte": 18, "option_score": 80, "bid": 4.10,
              "ask": 4.40, "oi": 145, "volume": 30, "quote_provider": "fixture",
              "bid_quote_at": (NOW - timedelta(seconds=20)).isoformat(),
              "ask_quote_at": (NOW - timedelta(seconds=30)).isoformat(),
              "strategy_modes": ["BALANCED"], "analysis_run_id": "current",
              "volume_session_date": DATE}
    result.update(kw)
    return result


def test_happy_path_provenance_and_expiry():
    got = validate_scanner_call(option(), parent(), now=NOW, session_close=SESSION_CLOSE, session_date=DATE,
                                policy=USOptionsPolicy(), option_threshold=70)
    assert got["mid_price"] == pytest.approx(4.25)
    assert got["premium_currency"] == "USD"
    assert got["source"] == "scanner"
    assert got["parent_run_id"] == "original"
    assert got["recommendation_expires_at"] == (NOW + timedelta(seconds=90)).isoformat()


@pytest.mark.parametrize("invalid", [
    {"bid": 0}, {"ask": -1}, {"bid": 5, "ask": 4}, {"ask": float("nan")},
    {"bid": float("inf")}, {"bid": "not-a-price"}, {"bid_quote_at": None},
    {"ask_quote_at": "2026-10-19T11:00:00"},
    {"bid_quote_at": (NOW - timedelta(minutes=3)).isoformat()},
    {"ask_quote_at": (NOW + timedelta(minutes=3)).isoformat()},
    {"quote_provider": ""}, {"volume": None}, {"oi": None}, {"oi": 10},
    {"volume": 1}, {"volume": float("nan")}, {"volume_session_date": None}, {"ask": 5.5},
    {"option_type": "PUT"}, {"symbol": "WRONG"}, {"strike": 0},
    {"expiry": "2026-10-20", "dte": 1},
    {"expiry": "2027-01-01", "dte": 74},
    {"expiry": "2026-11-06", "dte": 9},
    {"option_score": 69.99}, {"option_score": float("inf")},
    {"strategy_modes": []}, {"earnings_risk": True},
])
def test_fail_closed(invalid):
    with pytest.raises((InvalidOptionEvidence, ValueError)):
        validate_scanner_call(option(**invalid), parent(), now=NOW, session_close=SESSION_CLOSE,
                              session_date=DATE, policy=USOptionsPolicy(), option_threshold=70)


def test_fetch_time_does_not_substitute_quote_side_time():
    with pytest.raises(InvalidOptionEvidence, match="bid_quote_at"):
        validate_scanner_call(option(bid_quote_at=None, lastTradeDate=NOW.isoformat(),
                                    fetch_completed_at=NOW.isoformat(), update_time=NOW.isoformat()),
                              parent(), now=NOW, session_close=SESSION_CLOSE, session_date=DATE,
                              policy=USOptionsPolicy(), option_threshold=70)


def test_reference_mode_uses_generic_tv_update_without_faking_side_quote_times():
    from tradingview.scheduler.us_options_policy import validate_persisted_scanner_record
    got = validate_scanner_call(
        option(bid_quote_at=None, ask_quote_at=None, tv_update_time=(NOW - timedelta(seconds=30)).timestamp(),
               fetch_completed_at=(NOW - timedelta(seconds=5)).isoformat(),
               volume_session_provenance="inferred_from_current_tv_update", volume_session_date=None,
               oi_source="yfinance_exact_expiry_strike"),
        parent(), now=NOW, session_close=SESSION_CLOSE, session_date=DATE,
        policy=USOptionsPolicy(quote_mode="reference"), option_threshold=70,
    )
    assert got["freshness_status"] == "reference_unverified"
    assert got["premium_basis"] == "reference · freshness unverified"
    assert got["bid_quote_at"] is None and got["ask_quote_at"] is None
    assert got["volume_session_date"] == DATE
    assert got["recommendation_expires_at"] == (NOW + timedelta(seconds=115)).isoformat()
    validate_persisted_scanner_record(got, NOW + timedelta(seconds=30))
    with pytest.raises(InvalidOptionEvidence, match="expired"):
        validate_persisted_scanner_record(got, NOW + timedelta(seconds=121))


def test_reference_mode_still_requires_reported_oi_and_current_session_volume():
    policy = USOptionsPolicy(quote_mode="reference")
    base = option(bid_quote_at=None, ask_quote_at=None,
                  tv_update_time=(NOW - timedelta(seconds=30)).timestamp(),
                  fetch_completed_at=(NOW - timedelta(seconds=5)).isoformat(),
                  volume_session_provenance="inferred_from_current_tv_update",
                  volume_session_date=None)
    with pytest.raises(InvalidOptionEvidence, match="open interest"):
        validate_scanner_call({**base, "oi": None}, parent(), now=NOW,
                              session_close=SESSION_CLOSE, session_date=DATE,
                              policy=policy, option_threshold=70)
    with pytest.raises(InvalidOptionEvidence, match="current session"):
        validate_scanner_call({**base, "volume_session_provenance": None,
                               "oi_source": "yfinance_exact_expiry_strike"}, parent(), now=NOW,
                              session_close=SESSION_CLOSE, session_date=DATE,
                              policy=policy, option_threshold=70)


def test_reference_rejects_missing_oi_source_and_holiday_session():
    from tradingview.scheduler.us_options_policy import validate_scanner_call
    ref = option(bid_quote_at=None, ask_quote_at=None,
                 tv_update_time=(NOW - timedelta(seconds=30)).timestamp(),
                 fetch_completed_at=(NOW - timedelta(seconds=5)).isoformat(),
                 volume_session_provenance="provider_session_date", oi_source=None)
    with pytest.raises(InvalidOptionEvidence, match="open interest source"):
        validate_scanner_call(ref, parent(), now=NOW, session_close=SESSION_CLOSE,
                              session_date=DATE, policy=USOptionsPolicy(quote_mode="reference"),
                              option_threshold=70)
    holiday_now = datetime(2026, 11, 26, 15, tzinfo=timezone.utc)
    holiday = {**ref, "tv_update_time": (holiday_now - timedelta(seconds=30)).timestamp(),
               "fetch_completed_at": (holiday_now - timedelta(seconds=5)).isoformat(),
               "oi_source": "yfinance_exact_expiry_strike", "expiry": "2026-12-18", "dte": 22}
    with pytest.raises(InvalidOptionEvidence, match="trading session"):
        validate_scanner_call(holiday, parent(session_date="2026-11-26"), now=holiday_now,
                              session_close=datetime(2026, 11, 26, 20, tzinfo=timezone.utc),
                              session_date="2026-11-26",
                              policy=USOptionsPolicy(quote_mode="reference"), option_threshold=70)


def test_reference_recovery_rejects_future_fetch_timestamp():
    from tradingview.scheduler.us_options_policy import validate_persisted_scanner_record
    row = validate_scanner_call(
        option(bid_quote_at=None, ask_quote_at=None,
               tv_update_time=(NOW - timedelta(seconds=30)).timestamp(),
               fetch_completed_at=(NOW - timedelta(seconds=5)).isoformat(),
               volume_session_provenance="provider_session_date",
               oi_source="yfinance_exact_expiry_strike"),
        parent(), now=NOW, session_close=SESSION_CLOSE, session_date=DATE,
        policy=USOptionsPolicy(quote_mode="reference"), option_threshold=70,
    )
    row["fetch_completed_at"] = (NOW + timedelta(seconds=30)).isoformat()
    with pytest.raises(InvalidOptionEvidence, match="future-dated"):
        validate_persisted_scanner_record(row, NOW)


@pytest.mark.parametrize("changes", [{"direction": "SELL"}, {"actionable": False},
                                     {"session_date": "2026-10-16"}])
def test_direct_contract_validation_rechecks_parent(changes):
    with pytest.raises(InvalidOptionEvidence, match="current actionable BUY"):
        validate_scanner_call(option(), parent(**changes), now=NOW,
                              session_close=SESSION_CLOSE, session_date=DATE,
                              policy=USOptionsPolicy(), option_threshold=70)


@pytest.mark.parametrize("field", ["parent_setup_id", "parent_run_id", "analysis_run_id"])
def test_recovery_requires_durable_lineage(field):
    from tradingview.scheduler.us_options_policy import validate_persisted_scanner_record
    row = validate_scanner_call(option(), parent(), now=NOW,
                               session_close=SESSION_CLOSE, session_date=DATE,
                               policy=USOptionsPolicy(), option_threshold=70)
    row.pop(field)
    with pytest.raises(InvalidOptionEvidence, match=field):
        validate_persisted_scanner_record(row, NOW)


def test_recovery_rejects_future_side_quotes_even_with_short_deadline():
    from tradingview.scheduler.us_options_policy import validate_persisted_scanner_record
    row = validate_scanner_call(option(), parent(), now=NOW,
                               session_close=SESSION_CLOSE, session_date=DATE,
                               policy=USOptionsPolicy(), option_threshold=70)
    row["bid_quote_at"] = (NOW + timedelta(minutes=10)).isoformat()
    with pytest.raises(InvalidOptionEvidence, match="future"):
        validate_persisted_scanner_record(row, NOW)


def test_badge_adapter_does_not_relabel_another_underlying_contract():
    from tradingview.scheduler.us_options_policy import extract_validated_badge_calls
    row = {**parent(), "tv_symbol": "NYSE:RTX", "composite": 75.5,
           "options_data": {"meta": {"data_source": "fixture"},
                            "BALANCED": {"top_contracts": [option(symbol="NEM")]}}}
    contracts, _, _ = extract_validated_badge_calls(
        [row], now=NOW, session_date=DATE, session_close=SESSION_CLOSE,
        policy=USOptionsPolicy(), parent_threshold=62.5, option_threshold=70, run_id="run")
    assert contracts == []


@pytest.mark.parametrize("changes", [
    {"max_underlyings": 0}, {"max_underlyings": True},
    {"max_contracts_per_symbol": 0}, {"max_dte": 7},
    {"max_quote_age_seconds": 0}, {"max_quote_age_seconds": 10000},
    {"min_volume": -1}, {"min_open_interest": -10},
    {"max_spread_pct": 0}, {"max_spread_pct": float("nan")},
    {"max_spread_pct": float("inf")},
])
def test_invalid_policy_rejected(changes):
    with pytest.raises(ValueError):
        replace(USOptionsPolicy(), **changes)


def test_canonical_numeric_identity_unchanged_by_mutable_fields():
    a = option(strike=195, source="scanner", market="US", session_date=DATE)
    b = option(strike=195.0, source="scanner", market="US", session_date=DATE,
               bid=4.25, option_score=90, strategy_modes=["SAFE", "AGGRESSIVE"],
               contract_identifier="provider_alias")
    assert canonical_contract(a) == canonical_contract(b)
    assert scanner_option_identity(123, a) == scanner_option_identity(123, b)
    assert scanner_option_identity(456, a) != scanner_option_identity(123, a)
    b["session_date"] = "2026-10-20"
    assert scanner_option_identity(123, a) != scanner_option_identity(123, b)
    b["session_date"] = DATE
    b["strike"] = 200
    assert scanner_option_identity(123, a) != scanner_option_identity(123, b)


def test_dedup_modes_and_deterministic_ranking():
    rows = [option(option_score=81, strategy_modes=["SAFE"]),
            option(option_score=80, strategy_modes=["BALANCED"]),
            option(option_score=90, strategy_modes=["AGGRESSIVE"], strike=200)]
    chosen, rejected = select_contracts(rows, parent(), now=NOW, session_close=SESSION_CLOSE, session_date=DATE,
                                         policy=USOptionsPolicy(max_contracts_per_symbol=2),
                                         option_threshold=70)
    assert rejected == {}
    assert len(chosen) == 2
    assert chosen[0]["strike"] == 200
    assert chosen[1]["strategy_modes"] == ["BALANCED", "SAFE"]


def test_parent_selection_stable_and_requires_durable_setup():
    rows = [parent(), parent(pro_symbol="NASDAQ:AAPL", score=76, scanner_setup_id="s2"),
            parent(pro_symbol="NASDAQ:NVDA", score=79, scanner_setup_id="s3", durably_persisted=False),
            parent(pro_symbol="NASDAQ:AMD", score=90, scanner_setup_id="s4", direction="SELL"),
            parent(pro_symbol="NASDAQ:MSFT", score=82, scanner_setup_id="s5", actionable=False)]
    chosen = select_actionable_parents(rows, session_date=DATE, min_score=62.5,
                                       policy=USOptionsPolicy(max_underlyings=2))
    assert [r["pro_symbol"] for r in chosen] == ["NASDAQ:AAPL", "NYSE:RTX"]


def test_new_york_date_checked():
    with pytest.raises(InvalidOptionEvidence, match="session date"):
        validate_scanner_call(option(), parent(), now=NOW, session_close=SESSION_CLOSE, session_date="2026-10-20",
                              policy=USOptionsPolicy(), option_threshold=70)


def test_legacy_and_missing_provenance_not_implicitly_accepted():
    with pytest.raises(InvalidOptionEvidence):
        validate_scanner_call(option(), parent(near_earnings=True), now=NOW, session_close=SESSION_CLOSE,
                              session_date=DATE, policy=USOptionsPolicy(), option_threshold=70)


def test_early_close_bounds_quote_expiry():
    early_close = NOW + timedelta(seconds=15)
    got = validate_scanner_call(option(), parent(), now=NOW, session_close=early_close,
                                session_date=DATE, policy=USOptionsPolicy(),
                                option_threshold=70)
    assert got["recommendation_expires_at"] == early_close.isoformat()
    with pytest.raises(InvalidOptionEvidence, match="closed"):
        validate_scanner_call(option(), parent(), now=early_close, session_close=early_close,
                              session_date=DATE, policy=USOptionsPolicy(), option_threshold=70)

def test_future_quote_does_not_extend_validity():
    got = validate_scanner_call(option(bid_quote_at=(NOW + timedelta(seconds=30)).isoformat(),
                                       ask_quote_at=(NOW + timedelta(seconds=30)).isoformat()),
                                parent(), now=NOW, session_close=SESSION_CLOSE,
                                session_date=DATE, policy=USOptionsPolicy(), option_threshold=70)
    assert got["recommendation_expires_at"] == (NOW + timedelta(seconds=120)).isoformat()

from tradingview.scheduler.us_options_policy import extract_validated_badge_calls


def badge_item(**extra):
    output = dict(parent())
    output.update({"tv_symbol": "NYSE:RTX", "composite": 75.5,
                   "options_data": {"meta": {"data_source": "fixture", "earnings_risk": False},
                                    "BALANCED": {"top_contracts": [option()]}}})
    output.update(extra)
    return output


def test_badge_producer_to_snapshot_contract_boundary():
    items, errors, status = extract_validated_badge_calls(
        [badge_item()], session_date=DATE, now=NOW, session_close=SESSION_CLOSE,
        policy=USOptionsPolicy(), parent_threshold=62.5, option_threshold=70,
        run_id="scanner_cycle_a")
    assert status == "success" and not errors
    assert len(items) == 1
    assert items[0]["analysis_run_id"] == "scanner_cycle_a"
    assert items[0]["parent_setup_id"] == "s1"
    assert items[0]["bid"] == pytest.approx(4.10)


def test_badge_moomoo_missing_quote_sides_is_an_operational_failure():
    upstream = option(bid_quote_at=None, ask_quote_at=None, source="moomoo", update_time=NOW.isoformat())
    itm = badge_item(options_data={"meta": {"data_source": "moomoo"},
                                       "BALANCED": {"top_contracts": [upstream]}})
    rows, errors, status = extract_validated_badge_calls(
        [itm], session_date=DATE, now=NOW, session_close=SESSION_CLOSE,
        policy=USOptionsPolicy(), parent_threshold=62.5, option_threshold=70,
        run_id="scanner_cycle_b")
    assert rows == [] and status == "unavailable"
    assert len(errors) == 1 and errors[0]["symbol"] == "NYSE:RTX"


def test_no_persisted_parent_never_enters_badge_processing():
    item = badge_item(durably_persisted=False, options_data={"error": "expensive provider failure"})
    result = extract_validated_badge_calls(
        [item], session_date=DATE, now=NOW, session_close=SESSION_CLOSE,
        policy=USOptionsPolicy(), parent_threshold=62.5, option_threshold=70,
        run_id="scanner_cycle_c")
    assert result == ([], [], "empty")


def test_partial_badge_failures_preserve_good_contracts():
    item = badge_item()
    other = badge_item(tv_symbol="NASDAQ:AAPL", pro_symbol="NASDAQ:AAPL",
                       scanner_setup_id="s2", composite=85.0,
                       options_data={"error": "provider timeout"})
    rows, errors, status = extract_validated_badge_calls(
        [other, item], session_date=DATE, now=NOW, session_close=SESSION_CLOSE,
        policy=USOptionsPolicy(), parent_threshold=62.5, option_threshold=70,
        run_id="scanner_cycle_d")
    assert len(rows) == 1 and len(errors) == 1 and status == "partial"


def test_valid_chain_no_strategy_matches_is_silent():
    itm = badge_item(options_data={"meta": {"data_source": "moomoo"},
                                       "SAFE": {"top_contracts": []}})
    rows, errors, status = extract_validated_badge_calls(
        [itm], session_date=DATE, now=NOW, session_close=SESSION_CLOSE,
        policy=USOptionsPolicy(), parent_threshold=62.5, option_threshold=70,
        run_id="scanner_cycle_e")
    assert (rows, errors, status) == ([], [], "empty")

from tradingview.scheduler.us_options_policy import validate_persisted_scanner_record


def test_persisted_row_revalidates_and_expires_without_refresh():
    row = validate_scanner_call(option(), parent(), now=NOW,
                                session_close=SESSION_CLOSE, session_date=DATE,
                                policy=USOptionsPolicy(), option_threshold=70)
    validate_persisted_scanner_record(row, NOW)
    with pytest.raises(InvalidOptionEvidence, match="expired"):
        validate_persisted_scanner_record(row, NOW + timedelta(minutes=3))
    row["recommendation_expires_at"] = (NOW + timedelta(minutes=4)).isoformat()
    with pytest.raises(InvalidOptionEvidence, match="extended"):
        validate_persisted_scanner_record(row, NOW)


def test_stored_midpoint_crossed_or_missing_provenance_rejected():
    source = validate_scanner_call(option(), parent(), now=NOW,
                                   session_close=SESSION_CLOSE, session_date=DATE,
                                   policy=USOptionsPolicy(), option_threshold=70)
    for mutation in ({"mid_price": 8}, {"bid": 10}, {"scope": "test"},
                     {"bid_quote_at": NOW.isoformat().split("+")[0]},
                     {"volume_session_date": "2026-10-16"},
                     {"min_open_interest": 1000}, {"min_volume": 1000},
                     {"min_dte": 30}):
        corrupted = {**source, **mutation}
        with pytest.raises(InvalidOptionEvidence):
            validate_persisted_scanner_record(corrupted, NOW)


def test_test_scope_cannot_create_production_recommendation():
    rows = select_actionable_parents([parent(scope="test")], session_date=DATE,
                                     min_score=62.5, policy=USOptionsPolicy())
    assert rows == []
    with pytest.raises(InvalidOptionEvidence, match="production"):
        validate_scanner_call(option(), parent(scope="test"), now=NOW,
                              session_close=SESSION_CLOSE, session_date=DATE,
                              policy=USOptionsPolicy(), option_threshold=70)
