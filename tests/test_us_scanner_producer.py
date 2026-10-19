from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tradingview.options import options_badge
from tradingview.options.options_analyzer import OptionsAnalyzer
from tradingview.options.options_analyzer import USScannerOptionsUnavailable
from tradingview.options.options_snapshot import validate_snapshot
from tradingview.options.us_scanner_producer import produce_us_scanner_options
from tradingview.scheduler.us_options_policy import USOptionsPolicy


NOW = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)
SESSION_DATE = "2026-10-16"
SESSION_OPEN = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
SESSION_CLOSE = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)


def _parent(symbol: str, score: float = 85.7, **overrides) -> dict:
    row = {
        "tv_symbol": symbol,
        "source": "scanner",
        "market": "US",
        "scope": "production",
        "session_date": SESSION_DATE,
        "direction": "BUY",
        "actionable": True,
        "durably_persisted": True,
        "scanner_setup_id": f"setup-{symbol}",
        "run_id": "scanner-run-1",
        "composite": score,
        "instrument_type": "equity",
        "bar_time": (NOW - timedelta(minutes=15)).isoformat(),
        "bar_end_time": NOW.isoformat(), "bar_interval_minutes": 15,
        "quote_source": "Yahoo Finance 15m chart",
        "freshness_assurance": "independent_chart_crosscheck",
        "configured_provider_delay_minutes": 15,
    }
    row.update(overrides)
    return row


def _policy(**overrides) -> USOptionsPolicy:
    values = {
        "max_underlyings": 5,
        "max_contracts_per_symbol": 1,
        "min_dte": 14,
        "max_dte": 45,
        "min_open_interest": 100,
        "min_volume": 10,
        "max_spread_pct": 10,
        "max_quote_age_seconds": 120,
    }
    values.update(overrides)
    return USOptionsPolicy(**values)


def _contract(*, quote_times: bool = True, strike: float = 100.0) -> dict:
    expiry = date.fromisoformat(SESSION_DATE) + timedelta(days=28)
    row = {
        "strike": strike,
        "expiry": expiry.isoformat(),
        "dte": 28,
        "option_type": "CALL",
        "market": "US",
        "spot_price": 100.0,
        "bid": 4.90,
        "ask": 5.10,
        "mid_price": 5.0,
        "spread_pct": 4.0,
        "volume": 500,
        "oi": 1200,
        "iv": 35.0,
        "delta": 0.55,
        "moneyness_pct": 0.0,
        "source": "fixture-provider",
        "quote_provider": "fixture-provider",
        "volume_session_date": SESSION_DATE,
        "fetch_completed_at": NOW.isoformat(),
    }
    if quote_times:
        row["bid_quote_at"] = (NOW - timedelta(seconds=10)).isoformat()
        row["ask_quote_at"] = (NOW - timedelta(seconds=8)).isoformat()
    return row


def test_real_analyzer_adapter_scores_once_and_publishes_strict_snapshot(tmp_path, monkeypatch):
    analyzer = OptionsAnalyzer()
    fetches = []

    def fake_fetch(symbol):
        fetches.append(symbol)
        return [_contract(), _contract(strike=105.0)]

    analyzer._fetch_us_scanner = fake_fetch
    monkeypatch.setattr(options_badge, "_analyzer", analyzer)
    snapshot_path = tmp_path / "options.json"

    snapshot = produce_us_scanner_options(
        [_parent("NASDAQ:ACME")],
        snapshot_path=snapshot_path,
        run_id="scanner-run-1_us",
        session_date=SESSION_DATE,
        session_open=SESSION_OPEN,
        session_close=SESSION_CLOSE,
        policy=_policy(),
        parent_threshold=62.5,
        option_threshold=70.0,
        clock=lambda: NOW,
        badge_call=lambda symbol, score: options_badge.compute_options_badge_data(
            symbol, signal_score=score, scanner_all_candidates=True,
        ),
    )

    assert validate_snapshot(snapshot, expected_run_id="scanner-run-1_us", now=NOW) == []
    assert fetches == ["NASDAQ:ACME"]  # one chain shared by all three modes
    assert snapshot["status"] == "success"
    assert len(snapshot["contracts"]) == 1  # same contract across modes collapsed
    selected = snapshot["contracts"][0]
    assert selected["source"] == "scanner"
    assert selected["parent_signal_score"] == 85.7
    assert selected["option_score"] >= 70.0
    assert selected["parent_setup_id"] == "setup-NASDAQ:ACME"
    assert selected["parent_run_id"] == "scanner-run-1"
    assert selected["analysis_run_id"] == "scanner-run-1_us"
    assert selected["quote_provider"] == "fixture-provider"


def test_scanner_adapter_does_not_turn_ambiguous_empty_provider_results_into_success(mocker):
    analyzer = OptionsAnalyzer()
    tv = mocker.patch("tradingview.options.tv_options.TVOptionsFetcher")
    tv.return_value.is_available.return_value = True
    tv.return_value.get_calls.return_value = []

    # The TV adapter's legacy list API collapses an empty chain and a fetch
    # failure into []; scanner delivery must classify that as unavailable.
    with pytest.raises(USScannerOptionsUnavailable, match="returned no chain"):
        analyzer._fetch_us_scanner("NASDAQ:ACME")


def test_scanner_adapter_uses_tv_chain_once_and_keeps_generic_timestamp(mocker):
    analyzer = OptionsAnalyzer()
    rows = [_contract()]
    rows[0].update(oi=0, volume=0, source="tv", tv_update_time=1_792_175_170)
    rows[0].pop("bid_quote_at")
    rows[0].pop("ask_quote_at")
    rows[0].pop("volume_session_date")
    tv = mocker.patch("tradingview.options.tv_options.TVOptionsFetcher")
    tv.return_value.is_available.return_value = True
    tv.return_value.get_calls.return_value = rows

    contracts = analyzer._fetch_us_scanner("NASDAQ:ACME")
    assert tv.return_value.get_calls.call_count == 1
    assert contracts[0]["tv_update_time"] == 1_792_175_170
    assert contracts[0]["scanner_provider_capabilities"] == {
        "provider": "tradingview_scan2",
        "independent_bid_ask_timestamps": False,
        "open_interest_with_provenance": False,
        "session_volume_with_date": False,
    }
    assert "bid_quote_at" not in contracts[0]
    assert "ask_quote_at" not in contracts[0]


def test_scanner_adapter_normalizes_dte_to_new_york_session_date(mocker, monkeypatch):
    analyzer = OptionsAnalyzer()
    row = _contract()
    row["expiry"] = "2026-11-13"
    tv = mocker.patch("tradingview.options.tv_options.TVOptionsFetcher")
    tv.return_value.is_available.return_value = True
    tv.return_value.get_calls.return_value = [row]
    # Sydney has rolled into Monday while New York remains Sunday.
    monkeypatch.setenv("SIGNAL_SCANNER_TIMESTAMP", "2026-10-19T01:00:00+00:00")
    monkeypatch.setenv("SCHEDULER_SCANNER_US_OPTIONS_MIN_DTE", "13")
    monkeypatch.setenv("SCHEDULER_SCANNER_US_OPTIONS_MAX_DTE", "50")
    contracts = analyzer._fetch_us_scanner("NASDAQ:ACME")
    assert contracts[0]["dte"] == (date(2026, 11, 13) - date(2026, 10, 18)).days
    assert tv.return_value.get_calls.call_args.kwargs == {
        "n_expiries": 3, "min_dte": 13, "max_dte": 50,
        "reference_date": date(2026, 10, 18),
    }


def test_reference_adapter_enriches_only_exact_reported_yahoo_oi(mocker, monkeypatch):
    analyzer = OptionsAnalyzer()
    rows = [_contract(strike=100.0), _contract(strike=105.0)]
    expiry = rows[0]["expiry"]
    tv = mocker.patch("tradingview.options.tv_options.TVOptionsFetcher")
    tv.return_value.is_available.return_value = True
    tv.return_value.get_calls.return_value = rows
    yf = mocker.patch("tradingview.options.yf_options.YFOptionsFetcher")
    yf.return_value.get_option_chain_for_date.return_value = [
        {"strike": 100.0, "expiry": expiry, "option_type": "CALL", "oi": 123},
        {"strike": 105.0, "expiry": expiry, "option_type": "CALL", "oi": True},
        {"strike": 105.0, "expiry": "2026-12-31", "option_type": "CALL", "oi": 999},
    ]
    monkeypatch.setenv("SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE", "reference")
    monkeypatch.setenv("SIGNAL_SCANNER_TIMESTAMP", "2026-10-16T17:00:00+00:00")
    contracts = analyzer._fetch_us_scanner("NASDAQ:ACME")
    assert contracts[0]["oi"] == 123
    assert contracts[0]["oi_source"] == "yfinance_exact_expiry_strike"
    assert "oi_source" not in contracts[1]


def test_legacy_analyzer_call_keeps_existing_us_fetch_path(monkeypatch):
    analyzer = OptionsAnalyzer()
    calls = []
    monkeypatch.setattr(analyzer, "_fetch_us", lambda symbol: calls.append(symbol) or [])
    monkeypatch.setattr(
        analyzer, "_fetch_us_scanner",
        lambda _symbol: (_ for _ in ()).throw(AssertionError("scanner path used")),
    )

    result = analyzer.analyze_all_modes("NASDAQ:ACME")

    assert calls == ["NASDAQ:ACME"]
    assert "scanner_provider_capabilities" not in result["meta"]


def test_provider_failure_flows_through_badge_to_unavailable_snapshot(tmp_path, monkeypatch, mocker):
    analyzer = OptionsAnalyzer()
    monkeypatch.setattr(options_badge, "_analyzer", analyzer)
    tv = mocker.patch("tradingview.options.tv_options.TVOptionsFetcher")
    tv.return_value.is_available.return_value = True
    tv.return_value.get_calls.side_effect = RuntimeError("provider internal detail")

    snapshot = produce_us_scanner_options(
        [_parent("NASDAQ:ACME")],
        snapshot_path=tmp_path / "provider-error.json",
        run_id="provider-error-run",
        session_date=SESSION_DATE,
        session_open=SESSION_OPEN,
        session_close=SESSION_CLOSE,
        policy=_policy(),
        parent_threshold=62.5,
        option_threshold=70.0,
        clock=lambda: NOW,
        badge_call=lambda symbol, score: options_badge.compute_options_badge_data(
            symbol, signal_score=score, scanner_all_candidates=True,
        ),
    )

    assert snapshot["status"] == "unavailable"
    assert snapshot["contracts"] == []
    assert snapshot["errors"]
    assert snapshot["errors"][0]["message"] == "options provider did not complete successfully"
    assert "provider internal detail" not in str(snapshot)


def test_tv_provider_capability_error_survives_analyzer_filtering(tmp_path, monkeypatch, mocker):
    analyzer = OptionsAnalyzer()
    monkeypatch.setattr(options_badge, "_analyzer", analyzer)
    tv = mocker.patch("tradingview.options.tv_options.TVOptionsFetcher")
    tv.return_value.is_available.return_value = True
    # TV's observed schema supplies cached bid/ask and generic update_time,
    # while OI/session volume are zero and side timestamps are absent.
    row = _contract()
    row.update(oi=0, volume=0, source="tv", tv_update_time=1_792_175_170)
    row.pop("bid_quote_at")
    row.pop("ask_quote_at")
    row.pop("volume_session_date")
    tv.return_value.get_calls.return_value = [row]

    snapshot = produce_us_scanner_options(
        [_parent("NASDAQ:ACME")], snapshot_path=tmp_path / "tv-limits.json",
        run_id="tv-limits-run", session_date=SESSION_DATE,
        session_open=SESSION_OPEN, session_close=SESSION_CLOSE,
        policy=_policy(), parent_threshold=62.5, option_threshold=70.0,
        clock=lambda: NOW,
        badge_call=lambda symbol, score: options_badge.compute_options_badge_data(
            symbol, signal_score=score, scanner_all_candidates=True,
        ),
    )

    assert snapshot["status"] == "unavailable"
    assert snapshot["contracts"] == []
    messages = " ".join(error["message"] for error in snapshot["errors"])
    assert "independent bid/ask timestamps" in messages
    assert "open interest provenance" in messages
    assert "current-session volume date" in messages
    assert tv.return_value.get_calls.call_count == 1


def test_no_eligible_parent_or_explicit_equity_proof_means_no_chain_fetch(tmp_path):
    calls = []
    callback = lambda *args: calls.append(args) or {}
    rows = [
        _parent("NASDAQ:LOW", score=60),
        _parent("NYSE:SELL", direction="SELL"),
        _parent("AMEX:ETF", instrument_type=None),
        _parent("NASDAQ:UNPERSISTED", durably_persisted=False),
        _parent("NASDAQ:STALE", bar_time=(NOW-timedelta(minutes=75)).isoformat(),
                bar_end_time=(NOW-timedelta(minutes=60)).isoformat()),
    ]
    snapshot = produce_us_scanner_options(
        rows,
        snapshot_path=tmp_path / "empty.json",
        run_id="empty-run",
        session_date=SESSION_DATE,
        session_open=SESSION_OPEN,
        session_close=SESSION_CLOSE,
        policy=_policy(),
        parent_threshold=62.5,
        option_threshold=70,
        badge_call=callback,
        clock=lambda: NOW,
    )
    assert calls == []
    assert snapshot["status"] == "empty"
    assert snapshot["contracts"] == []


def test_selected_parent_budget_is_deterministic_and_actual_score_is_passed(tmp_path):
    calls = []

    def callback(symbol, score):
        calls.append((symbol, score))
        return {
            "meta": {"market": "US", "data_source": "fixture-provider"},
            "BALANCED": {"top_contracts": [{**_contract(quote_times=False), "option_score": 80.0}]},
            "AGGRESSIVE": {"top_contracts": []},
            "SAFE": {"top_contracts": []},
        }

    rows = [_parent(f"NASDAQ:SYM{i}", score=70 + i) for i in range(8)]
    # An invalid higher-ranked duplicate must not replace the retained setup.
    rows.append(_parent("NASDAQ:SYM7", score=99, scanner_setup_id="bad-setup",
                        durably_persisted=False))
    snapshot = produce_us_scanner_options(
        rows,
        snapshot_path=tmp_path / "bounded.json",
        run_id="bounded-run",
        session_date=SESSION_DATE,
        session_open=SESSION_OPEN,
        session_close=SESSION_CLOSE,
        policy=_policy(max_underlyings=3),
        parent_threshold=62.5,
        option_threshold=70,
        badge_call=callback,
        clock=lambda: NOW,
    )
    assert calls == [("NASDAQ:SYM7", 77.0), ("NASDAQ:SYM6", 76.0), ("NASDAQ:SYM5", 75.0)]
    assert snapshot["status"] == "unavailable"
    assert all("verified live quote evidence" in err["message"] for err in snapshot["errors"])


def test_closed_session_does_not_call_provider(tmp_path):
    calls = []
    snapshot = produce_us_scanner_options(
        [_parent("NYSE:ACME")],
        snapshot_path=tmp_path / "closed.json",
        run_id="closed-run",
        session_date=SESSION_DATE,
        session_open=SESSION_OPEN,
        session_close=SESSION_CLOSE,
        policy=_policy(),
        parent_threshold=62.5,
        option_threshold=70,
        badge_call=lambda *args: calls.append(args) or {},
        clock=lambda: SESSION_CLOSE,
    )
    assert calls == []
    assert snapshot["status"] == "empty"


def test_snapshot_publication_discards_quote_that_expired_during_later_work(tmp_path, monkeypatch):
    analyzer = OptionsAnalyzer()
    analyzer._fetch_us_scanner = lambda _symbol: [_contract()]
    monkeypatch.setattr(options_badge, "_analyzer", analyzer)
    moments = iter([NOW, NOW, NOW, NOW + timedelta(seconds=200)])
    snapshot = produce_us_scanner_options(
        [_parent("NYSE:ACME")],
        snapshot_path=tmp_path / "expired.json",
        run_id="expired-run",
        session_date=SESSION_DATE,
        session_open=SESSION_OPEN,
        session_close=SESSION_CLOSE,
        policy=_policy(),
        parent_threshold=62.5,
        option_threshold=0,
        badge_call=lambda symbol, score: options_badge.compute_options_badge_data(
            symbol, signal_score=score, scanner_all_candidates=True,
        ),
        clock=lambda: next(moments),
    )
    assert snapshot["contracts"] == []
    assert snapshot["status"] == "unavailable"
    assert "expired before snapshot publication" in snapshot["errors"][0]["message"]

@pytest.mark.parametrize('evidence', [
    {'tv_update_time': (NOW - timedelta(minutes=16)).timestamp()},
    {'fetch_completed_at': 'invalid'},
    {'oi_source': None},
])
def test_reference_evidence_failures_are_not_silent_empty_results(tmp_path, monkeypatch, evidence):
    analyzer = OptionsAnalyzer()
    raw = _contract(quote_times=False)
    raw.update(tv_update_time=(NOW-timedelta(seconds=10)).timestamp(),
               oi_source='yfinance_exact_expiry_strike',
               volume_session_provenance='provider_session_date')
    raw.update(evidence)
    monkeypatch.setattr(analyzer, '_fetch_us_scanner', lambda symbol: [dict(raw)])
    monkeypatch.setattr(options_badge, '_analyzer', analyzer)
    snapshot = produce_us_scanner_options(
        [_parent('NASDAQ:ACME')], snapshot_path=tmp_path/'reference-error.json',
        run_id='reference-error', session_date=SESSION_DATE, session_open=SESSION_OPEN,
        session_close=SESSION_CLOSE, policy=_policy(quote_mode='reference'),
        parent_threshold=62.5, option_threshold=70, clock=lambda: NOW,
        badge_call=lambda symbol, score: options_badge.compute_options_badge_data(
            symbol, signal_score=score, scanner_all_candidates=True),
    )
    assert snapshot['status'] == 'unavailable', snapshot
    assert snapshot['errors'] and not snapshot['contracts']
