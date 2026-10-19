from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import report_scheduler as rs
from tradingview.scheduler.config import SchedulerConfig, load_config
from tradingview.scheduler.cycle import CycleResult, enqueue_cycle
from tradingview.scheduler.dispatch import dispatch_pending, reconcile_cycle_results
from tradingview.scheduler.identity import stock_delivery_key
from tradingview.scheduler.scanner import market_session, produce_scanner_cycle
from tradingview.scheduler.freshness import validate_scanner_fires, yahoo_symbol
from tradingview.scheduler.market_calendar import (
    CALENDAR_DATA_SUPPORTED_THROUGH, CalendarCoverageError, market_session_bounds,
)
from tradingview.scheduler.steps import StepResult
from tradingview.scheduler.stock_results import (
    StockResult,
    build_current_run_payload,
    load_current_run_stocks,
    write_current_run_payload,
)
from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory


RECIPIENT = 111
SG_OPEN = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)  # 10:00 Singapore
SG_FIRST_OPEN = datetime(2026, 10, 15, 1, 30, tzinfo=timezone.utc)  # first bar + 15m delay


def _row(env: dict, *, symbol="SGX:BS6", source="scanner", actionable=True):
    observed = datetime.fromisoformat(env["SIGNAL_SCANNER_TIMESTAMP"].replace("Z", "+00:00"))
    bar_start = observed.replace(second=0, microsecond=0) - timedelta(minutes=15)
    bar_end = bar_start + timedelta(minutes=15)
    return {
        "pro_symbol": symbol, "symbol": symbol.split(":", 1)[1],
        "exchange": symbol.split(":", 1)[0], "score": 81.5, "signal": "BUY",
        "fire_time": env["SIGNAL_SCANNER_TIMESTAMP"], "resolution": "D",
        "source": source, "market": env["SIGNAL_SCANNER_MARKET"],
        "session_date": env["SIGNAL_SCANNER_SESSION_DATE"],
        "timestamp": env["SIGNAL_SCANNER_TIMESTAMP"], "direction": "BUY",
        "actionable": actionable,
        "quote_source": "Yahoo Finance 15m chart",
        "quote_symbol": "BS6.SI",
        "bar_time": bar_start.isoformat(), "bar_end_time": bar_end.isoformat(),
        "bar_interval_minutes": 15, "provider_close": 100.0,
        "configured_provider_delay_minutes": 15,
        "source_price_delta_pct": 0.0,
        "freshness_assurance": "independent_chart_crosscheck",
    }


def _config(tmp_path, markets=("SG",), **kwargs):
    base = dict(
        scanner_markets=markets, scanner_candidates=7, scanner_every_hours=2,
        step_timeout=2, display_timezone="Australia/Sydney",
        run_root=tmp_path / "runs", outbox_path=tmp_path / "outbox.db",
    )
    base.update(kwargs)
    return SchedulerConfig(**base)


def test_scanner_environment_defaults_and_overrides():
    config = load_config({
        "SCHEDULER_SCANNER_MARKETS": "us, SG,IN,US",
        "SCHEDULER_SCANNER_CANDIDATES": "13",
        "SCHEDULER_SCANNER_EVERY_HOURS": "3",
        "SIGNAL_SCANNER_FETCH_LIMIT": "600",
        "SIGNAL_SCANNER_PROFILE": "broad",
    })
    assert config.scanner_markets == ("US", "SG", "IN")
    assert config.scanner_candidates == 13
    assert config.scanner_every_hours == 3
    assert config.scanner_fetch_limit == 600
    assert config.scanner_profile == "broad"
    assert config.scanner_scope == "production"
    assert config.scanner_us_options_quote_mode == "strict"
    assert load_config({"SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE": "reference"}).scanner_us_options_quote_mode == "reference"
    with pytest.raises(ValueError, match="quote_mode"):
        load_config({"SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE": "fresh-ish"})
    assert load_config({"SIGNAL_SCANNER_SCOPE": "TEST"}).scanner_scope == "test"
    assert load_config({}).scanner_markets == ("US", "AU", "IN", "SG")
    assert load_config({"SCHEDULER_SCANNER_MARKETS": ""}).scanner_markets == ()
    with pytest.raises(ValueError, match="SCHEDULER_STOCK_THRESHOLD"):
        load_config({"SCHEDULER_STOCK_THRESHOLD": "nan"})
    with pytest.raises(ValueError, match="SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES"):
        load_config({
            "SIGNAL_SCANNER_PROVIDER_DELAY_MINUTES": "30",
            "SIGNAL_SCANNER_MAX_BAR_AGE_MINUTES": "30",
        })
    with pytest.raises(ValueError, match="SIGNAL_SCANNER_SCOPE"):
        load_config({"SIGNAL_SCANNER_SCOPE": "typo"})


def test_market_hours_follow_exchange_timezone_dst_and_sg_lunch():
    assert market_session("US", datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc))[0]
    assert market_session("US", datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))[0]
    assert not market_session("US", datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))[0]
    assert market_session("SG", datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc))[0]
    assert not market_session("SG", datetime(2026, 10, 15, 4, 30, tzinfo=timezone.utc))[0]
    assert market_session("SG", datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc))[0]
    assert market_session("US", datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc))[0] is False


def test_exchange_calendars_cover_holidays_early_closes_and_special_sessions():
    # Package calendars include exchange-specific holidays and scheduled closes.
    assert not market_session("US", datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc))[0]
    assert not market_session("AU", datetime(2026, 12, 25, 1, 0, tzinfo=timezone.utc))[0]
    assert not market_session("IN", datetime(2026, 1, 26, 5, 0, tzinfo=timezone.utc))[0]
    assert market_session("US", datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc))[0]
    assert not market_session("US", datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc))[0]
    # Diwali special-session date is not silently treated as an ordinary session.
    assert not market_session("IN", datetime(2026, 11, 8, 5, 0, tzinfo=timezone.utc))[0]
    # SGX holiday and fixed lunch break use the exchange schedule/session split.
    assert not market_session("SG", datetime(2026, 2, 17, 2, 0, tzinfo=timezone.utc))[0]
    assert not market_session("SG", datetime(2026, 10, 15, 4, 30, tzinfo=timezone.utc))[0]
    # ASX's Christmas Eve early close is supplied by its package calendar.
    assert not market_session("AU", datetime(2026, 12, 24, 4, 0, tzinfo=timezone.utc))[0]
    with pytest.raises(CalendarCoverageError, match=CALENDAR_DATA_SUPPORTED_THROUGH["US"]):
        market_session("US", datetime(2028, 1, 3, 15, 0, tzinfo=timezone.utc))
    with pytest.raises(CalendarCoverageError, match=CALENDAR_DATA_SUPPORTED_THROUGH["SG"]):
        market_session("SG", datetime(2027, 1, 4, 2, 0, tzinfo=timezone.utc))


def test_market_session_bounds_follow_dst_early_close_and_holidays():
    assert market_session_bounds("US", "2026-01-15") == (
        datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc),
        datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc),
    )
    assert market_session_bounds("US", "2026-07-15") == (
        datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc),
    )
    assert market_session_bounds("US", "2026-11-27")[1] == datetime(
        2026, 11, 27, 18, 0, tzinfo=timezone.utc,
    )
    with pytest.raises(CalendarCoverageError, match="closed"):
        market_session_bounds("US", "2026-11-26")


def _fresh_scanner_option(now: datetime, session_date: str, run_id: str) -> dict:
    from datetime import date, time

    from tradingview.options.options_snapshot import build_snapshot, publish_snapshot

    ny = ZoneInfo("America/New_York")
    local_date = date.fromisoformat(session_date)
    close = datetime.combine(local_date, time(23, 59, 59), tzinfo=ny)
    expiry = (local_date + timedelta(days=30)).isoformat()
    quote_time = now.isoformat()
    contract = {
        "source": "scanner", "market": "US", "scope": "production",
        "session_date": session_date, "exchange": "NASDAQ", "symbol": "AAA",
        "option_type": "CALL", "contract_identifier": "AAA261116C00100000",
        "strike": 100, "expiry": expiry, "dte": 30,
        "option_score": 82.0, "parent_signal_score": 75.0,
        "strategy_modes": ["BALANCED"], "parent_setup_id": "setup-1",
        "parent_run_id": run_id, "analysis_run_id": run_id,
        "bid": 1.10, "ask": 1.20, "mid_price": 1.15,
        "premium_currency": "USD", "quote_provider": "fixture-provider",
        "bid_quote_at": quote_time, "ask_quote_at": quote_time,
        "fetch_completed_at": quote_time, "freshness_status": "verified_bid_ask",
        "quote_max_age_seconds": 120, "max_spread_pct": 10.0,
        "min_open_interest": 100, "min_volume": 10,
        "min_dte": 14, "max_dte": 45,
        "session_close_at": close.isoformat(),
        "recommendation_expires_at": (now + timedelta(seconds=90)).isoformat(),
        "oi": 500, "volume": 20, "volume_session_date": session_date,
    }
    return build_snapshot(run_id, "success", [contract], generated_at=quote_time)


def test_enabled_us_consumes_matching_snapshot_and_keeps_other_flags(tmp_path, monkeypatch):
    from tradingview.options.options_snapshot import publish_snapshot

    from tradingview.scheduler import scanner

    ny = ZoneInfo("America/New_York")
    session_date = datetime.now(timezone.utc).astimezone(ny).date().isoformat()
    now = datetime.combine(
        datetime.fromisoformat(session_date).date(), datetime.min.time().replace(hour=12), tzinfo=ny,
    ).astimezone(timezone.utc)
    config = _config(tmp_path, markets=("US",), scanner_us_options_enabled=True)
    monkeypatch.setattr(scanner, "market_session", lambda market, moment, force=False: (True, session_date))
    calls = []

    def runner(label, cmd, *, cwd, env, timeout):
        calls.append((cmd, env))
        row = _row(env, symbol="NASDAQ:AAA")
        write_current_run_payload(
            env["SIGNAL_STOCK_PAYLOAD"],
            build_current_run_payload(env["SIGNAL_RUN_ID"], [row]),
        )
        publish_snapshot(Path(env["SIGNAL_OPTIONS_SNAPSHOT"]),
                         _fresh_scanner_option(now, session_date, env["SIGNAL_RUN_ID"]))
        return StepResult(label=label, status="ok")

    result = produce_scanner_cycle(
        run_id="us-options", config=config, project_root=Path(__file__).parents[1],
        python_executable="python", runner=runner, now=now, clock=lambda: now,
    )
    command, env = calls[0]
    assert "--no-options" not in command
    assert "--no-external" in command and "--no-moomoo" in command and "--no-india" in command
    assert env["SCHEDULER_SCANNER_US_OPTIONS_ENABLED"] == "true"
    assert env["SCHEDULER_SCANNER_US_OPTIONS_MAX_UNDERLYINGS"] == "5"
    assert env["SCHEDULER_SCORE_THRESHOLD"] == "70.0"
    assert result.option_result["status"] == "success"
    option = result.options[0]
    assert option["source"] == "scanner" and option["market"] == "US"
    assert option["session_date"] == session_date
    assert option["parent_setup_id"] == "setup-1"
    assert option["bid_quote_at"] == option["ask_quote_at"]
    assert option["quote_provider"] == "fixture-provider"
    assert option["scanner_option_cap"] == 1
    assert result.status == "ok"


def test_us_options_flag_never_enables_non_us_scanner(tmp_path, monkeypatch):
    from tradingview.scheduler import scanner

    config = _config(tmp_path, markets=("SG",), scanner_us_options_enabled=True)
    monkeypatch.setattr(scanner, "market_session", lambda market, moment, force=False: (True, "2026-10-15"))
    captured = []

    def runner(label, cmd, *, cwd, env, timeout):
        captured.append((cmd, env))
        write_current_run_payload(
            env["SIGNAL_STOCK_PAYLOAD"],
            build_current_run_payload(env["SIGNAL_RUN_ID"], [], status="no_new"),
        )
        return StepResult(label=label, status="ok")

    result = produce_scanner_cycle(
        run_id="sg-options-remain-disabled", config=config,
        project_root=Path(__file__).parents[1], python_executable="python",
        runner=runner, now=SG_OPEN, clock=lambda: SG_OPEN,
    )
    command, env = captured[0]
    assert "--no-options" in command
    assert env["SCHEDULER_SCANNER_US_OPTIONS_ENABLED"] == "false"
    assert result.option_result["status"] == "disabled"


def test_us_options_provider_failure_is_partial_and_preserves_stock(tmp_path, monkeypatch):
    from tradingview.options.options_snapshot import build_snapshot, publish_snapshot

    from tradingview.scheduler import scanner

    ny = ZoneInfo("America/New_York")
    session_date = datetime.now(timezone.utc).astimezone(ny).date().isoformat()
    now = datetime.combine(
        datetime.fromisoformat(session_date).date(), datetime.min.time().replace(hour=12), tzinfo=ny,
    ).astimezone(timezone.utc)
    config = _config(tmp_path, markets=("US",), scanner_us_options_enabled=True)
    monkeypatch.setattr(scanner, "market_session", lambda market, moment, force=False: (True, session_date))

    def runner(label, cmd, *, cwd, env, timeout):
        row = _row(env, symbol="NASDAQ:AAA")
        write_current_run_payload(
            env["SIGNAL_STOCK_PAYLOAD"],
            build_current_run_payload(env["SIGNAL_RUN_ID"], [row]),
        )
        publish_snapshot(Path(env["SIGNAL_OPTIONS_SNAPSHOT"]), build_snapshot(
            env["SIGNAL_RUN_ID"], "unavailable", errors=[{"symbol": "AAA", "message": "provider timeout"}],
            generated_at=now.isoformat(),
        ))
        return StepResult(label=label, status="ok")

    result = produce_scanner_cycle(
        run_id="us-options-failure", config=config, project_root=Path(__file__).parents[1],
        python_executable="python", runner=runner, now=now, clock=lambda: now,
    )
    assert len(result.stocks) == 1
    assert result.option_result["status"] == "unavailable"
    assert result.options_errors == ["provider timeout"]
    assert result.scanner_markets["US"]["status"] == "partial"
    assert result.status == "partial"


def test_options_only_failure_reaches_scheduler_delivery_predicate(tmp_path, monkeypatch):
    from tradingview.scheduler import scanner

    ny = ZoneInfo("America/New_York")
    session_date = datetime.now(timezone.utc).astimezone(ny).date().isoformat()
    now = datetime.combine(
        datetime.fromisoformat(session_date).date(), datetime.min.time().replace(hour=12), tzinfo=ny,
    ).astimezone(timezone.utc)
    monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "US")
    monkeypatch.setenv("SCHEDULER_SCANNER_US_OPTIONS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("SCHEDULER_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(scanner, "market_session", lambda market, moment, force=False: (True, session_date))
    monkeypatch.setattr(rs, "_allowed_users", lambda: {RECIPIENT})
    monkeypatch.setattr(rs, "_now_utc", lambda: now)
    delivered = []
    async def record_delivery(bot, result, recipients):
        delivered.append((result, recipients))
    monkeypatch.setattr(rs, "_deliver_cycle_result", record_delivery)

    def fake_run_step(label, cmd, *, cwd, env, timeout):
        write_current_run_payload(
            env["SIGNAL_STOCK_PAYLOAD"],
            build_current_run_payload(env["SIGNAL_RUN_ID"], [], status="no_new"),
        )
        return StepResult(label=label, status="ok", command=tuple(cmd))
    monkeypatch.setattr("tradingview.scheduler.steps.run_step", fake_run_step)

    result = asyncio.run(rs._run_scanner_cycle(object(), ("US",)))
    assert result is not None
    assert result.options_status == "unavailable"
    assert result.scanner_markets["US"]["status"] == "partial"
    assert len(delivered) == 1
    assert delivered[0][0].run_id == result.run_id
    assert delivered[0][1] == [RECIPIENT]


def test_timeout_after_durable_stock_handoff_keeps_stock_and_fails_options(tmp_path, monkeypatch):
    from tradingview.scheduler import scanner

    ny = ZoneInfo("America/New_York")
    session_date = datetime.now(timezone.utc).astimezone(ny).date().isoformat()
    now = datetime.combine(
        datetime.fromisoformat(session_date).date(), datetime.min.time().replace(hour=12), tzinfo=ny,
    ).astimezone(timezone.utc)
    config = _config(tmp_path, markets=("US",), scanner_us_options_enabled=True)
    monkeypatch.setattr(scanner, "market_session", lambda market, moment, force=False: (True, session_date))

    def runner(label, cmd, *, cwd, env, timeout):
        row = _row(env, symbol="NASDAQ:AAA")
        write_current_run_payload(
            env["SIGNAL_STOCK_PAYLOAD"],
            build_current_run_payload(env["SIGNAL_RUN_ID"], [row]),
        )
        return StepResult(label=label, status="timeout", error="bounded child timeout")

    result = produce_scanner_cycle(
        run_id="us-timeout-after-stocks", config=config, project_root=Path(__file__).parents[1],
        python_executable="python", runner=runner, now=now, clock=lambda: now,
    )
    assert len(result.stocks) == 1
    assert result.scanner_markets["US"]["stock_status"] == "partial"
    assert result.option_result["status"] == "unavailable"
    assert result.status == "partial"


def test_short_scanner_quote_expiry_does_not_expire_stock_part(tmp_path):
    from types import SimpleNamespace

    from tradingview.options.options_snapshot import build_snapshot
    from tradingview.scheduler.cycle import CYCLE_OK

    now = datetime.now(timezone.utc)
    session_date = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    option = build_snapshot(
        "cycle_us", "success", [_fresh_scanner_option(now, session_date, "cycle_us")["contracts"][0]],
        generated_at=now.isoformat(),
    )["contracts"][0]
    option.update({
        "opt_type": "CALL", "score": option["option_score"],
        "parent_score": option["parent_signal_score"], "scanner_option_cap": 1,
    })
    stock = {
        "pro_symbol": "NASDAQ:BBB", "exchange": "NASDAQ", "symbol": "BBB",
        "score": 72.0, "signal": "BUY", "actionable": True,
    }
    cycle = CycleResult(
        run_id="mixed-expiry", started_at=now.isoformat(), status=CYCLE_OK,
        stock_result=StockResult(status="success", stocks=[stock]),
        option_result={"status": "success", "errors": [], "options": [option]},
    )
    clock = [now]
    outbox = NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"), clock=lambda: clock[0], alert_ttl_hours=12,
    )
    assert enqueue_cycle(outbox, cycle, [RECIPIENT], limit=3500, scanner_option_cap=1) == 2
    queued = outbox.all_items()
    assert len(queued) == 2
    assert sorted(len(item.payload["events"]) for item in queued) == [1, 1]
    option_item = next(item for item in queued if item.payload["events"][0].get("scanner_option_reservation"))
    stock_item = next(item for item in queued if item is not option_item)
    assert datetime.fromisoformat(option_item.expires_at) == datetime.fromisoformat(
        option["recommendation_expires_at"],
    )
    assert datetime.fromisoformat(stock_item.expires_at) > datetime.fromisoformat(option_item.expires_at)

    clock[0] += timedelta(minutes=3)
    sent = []
    async def transport(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=7)
    report = asyncio.run(dispatch_pending(outbox, send=transport))
    assert report.sent == 1 and report.expired == 1
    assert len(sent) == 1 and "BBB" in sent[0]["text"]


def test_all_calendar_schedules_are_populated_through_declared_boundary():
    import pandas_market_calendars as mcal
    for name, market in (("XNYS", "US"), ("XASX", "AU"), ("NSE", "IN"), ("XSES", "SG")):
        calendar = mcal.get_calendar(name)
        through = CALENDAR_DATA_SUPPORTED_THROUGH[market]
        schedule = calendar.schedule(
            start_date=through,
            end_date=through,
        )
        assert not schedule.empty, name


def _provider_history(index, close=100.0):
    import pandas as pd
    return pd.DataFrame({"Close": [close]}, index=pd.DatetimeIndex([index]))


def test_yahoo_bar_freshness_accepts_in_progress_bar_without_claiming_future_time():
    observed = datetime(2026, 6, 1, 9, 30, 30, tzinfo=ZoneInfo("America/New_York"))
    bar_start = datetime(2026, 6, 1, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    row = {"tv_symbol": "NASDAQ:AAPL", "close": 100.0}
    accepted, rejected = validate_scanner_fires(
        [row], market="US", session_date="2026-06-01", observed_at=observed,
        provider_fetcher=lambda symbol: _provider_history(bar_start, 100.5),
        provider_delay_minutes=15,
    )
    assert rejected == {}
    assert len(accepted) == 1
    assert accepted[0]["bar_time"] == bar_start.astimezone(timezone.utc).isoformat()
    assert accepted[0]["bar_end_time"] == (bar_start + timedelta(minutes=15)).astimezone(timezone.utc).isoformat()
    assert accepted[0]["quote_source"] == "Yahoo Finance 15m chart"
    assert "timestamp" not in accepted[0]


@pytest.mark.parametrize("provider,price,reason", [
    (lambda _s: (_ for _ in ()).throw(RuntimeError("network unavailable")), 100.0, "provider unavailable"),
    (lambda _s: _provider_history(datetime(2026, 6, 1, 8, 0, tzinfo=ZoneInfo("America/New_York"))), 100.0, "stale"),
    (lambda _s: _provider_history(datetime(2026, 6, 1, 9, 30, tzinfo=ZoneInfo("America/New_York")), 100.0), 110.0, "differs"),
    (lambda _s: _provider_history(datetime(2026, 6, 1, 9, 30)), 100.0, "timezone"),
])
def test_yahoo_bar_freshness_fails_closed(provider, price, reason):
    observed = datetime(2026, 6, 1, 9, 30, 30, tzinfo=ZoneInfo("America/New_York"))
    accepted, rejected = validate_scanner_fires(
        [{"tv_symbol": "NASDAQ:AAPL", "close": price}], market="US",
        session_date="2026-06-01", observed_at=observed, provider_fetcher=provider,
        provider_delay_minutes=15,
    )
    assert accepted == []
    assert "AAPL" in rejected
    assert reason in rejected["AAPL"]


def test_yahoo_symbol_mapping_checks_market_exchange():
    assert yahoo_symbol("NASDAQ:AAPL", "US") == "AAPL"
    assert yahoo_symbol("ASX:BHP", "AU") == "BHP.AX"
    assert yahoo_symbol("NSE:INFY", "IN") == "INFY.NS"
    assert yahoo_symbol("BSE:500325", "IN") == "500325.BO"
    assert yahoo_symbol("SGX:BS6", "SG") == "BS6.SI"
    with pytest.raises(ValueError, match="does not match"):
        yahoo_symbol("BSE:500325", "US")


def test_corrupt_persisted_scanner_cadence_is_sanitized(tmp_path):
    from tradingview.scheduler.scanner_state import load_cadence

    path = tmp_path / "scanner_cadence.json"
    path.write_text(json.dumps({
        "version": 1,
        "markets": {"SG": {
            "last_success": "NaN", "last_attempt": float("inf"),
            "retry_at": -1, "failure_count": "bogus",
        }},
    }))
    assert load_cadence(path) == {
        "SG": {"last_success": 0.0, "last_attempt": 0.0, "retry_at": 0.0, "failure_count": 0}
    }
    assert market_session("US", datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc), force=True)[0]


def test_producer_keeps_markets_isolated_and_classifies_empty_plus_failure(tmp_path):
    config = _config(tmp_path, markets=("IN", "SG"))
    now = datetime(2026, 10, 15, 3, 45, tzinfo=timezone.utc)  # IN + SG open
    calls = []

    def runner(label, cmd, *, cwd, env, timeout):
        calls.append((label, cmd, env))
        if label == "scanner_sg":
            return StepResult(label=label, status="failed", error="network unavailable")
        payload = build_current_run_payload(env["SIGNAL_RUN_ID"], [], status="no_new")
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], payload)
        return StepResult(label=label, status="ok")

    result = produce_scanner_cycle(
        run_id="scan-a", config=config, project_root=Path(__file__).parents[1],
        python_executable="python", runner=runner, now=now,
    )
    assert [call[0] for call in calls] == ["scanner_in", "scanner_sg"]
    assert all("--screener" in call[1] and "--no-options" in call[1] for call in calls)
    assert result.status == "partial"
    assert result.scanner_markets["IN"]["status"] == "no_new"
    assert result.scanner_markets["SG"]["status"] == "failed"
    persisted = json.loads((config.run_root / "scanner_scan-a" / "cycle_result.json").read_text())
    assert persisted["scanner_markets"] == result.scanner_markets


def test_producer_uses_four_market_classes_and_configured_candidate_cap(tmp_path):
    config = _config(tmp_path, markets=("US", "AU", "IN", "SG"), scanner_candidates=11)
    commands = []

    def runner(label, cmd, *, cwd, env, timeout):
        commands.append((label, cmd, env))
        payload = build_current_run_payload(env["SIGNAL_RUN_ID"], [], status="no_new")
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], payload)
        return StepResult(label=label, status="ok")

    result = produce_scanner_cycle(
        run_id="all-markets", config=config, project_root=Path(__file__).parents[1],
        python_executable="python", runner=runner, now=SG_OPEN, force=True,
    )
    classes = [cmd[cmd.index("--screener") + 1] for _, cmd, _ in commands]
    assert classes == ["stocks_us", "stocks_au", "stocks_in", "stocks_sg"]
    assert all(env["SIGNAL_SCANNER_MARKET"] == label.split("_")[-1].upper()
               for (label, _cmd, env) in commands)
    assert all(cmd[cmd.index("--n") + 1] == "11" for _, cmd, _ in commands)
    for _label, cmd, env in commands:
        market_dir = Path(env["SIGNAL_STOCK_PAYLOAD"]).parent
        assert Path(cmd[cmd.index("--out") + 1]) == market_dir / "signal_stack_report.html"
        assert Path(cmd[cmd.index("--actionable-out") + 1]) == market_dir / "signal_report_actionable.html"
    assert result.status == "no_new"


def test_producer_rejects_untrusted_payload_provenance(tmp_path):
    config = _config(tmp_path)

    def runner(label, cmd, *, cwd, env, timeout):
        payload = build_current_run_payload(env["SIGNAL_RUN_ID"], [_row(env, source="WIN")])
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], payload)
        return StepResult(label=label, status="ok")

    result = produce_scanner_cycle(
        run_id="bad-provenance", config=config, project_root=Path(__file__).parents[1],
        python_executable="python", runner=runner, now=SG_OPEN,
    )
    assert result.scanner_markets["SG"]["status"] == "failed"
    assert result.stocks == []


def test_market_session_is_resampled_after_prior_market_work(tmp_path):
    config = _config(tmp_path, markets=("IN", "SG"))
    clock = [datetime(2026, 10, 15, 3, 45, tzinfo=timezone.utc)]  # SG 11:45, IN 09:15
    calls = []

    def runner(label, cmd, *, cwd, env, timeout):
        calls.append(label)
        payload = build_current_run_payload(env["SIGNAL_RUN_ID"], [], status="no_new")
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], payload)
        clock[0] = datetime(2026, 10, 15, 4, 5, tzinfo=timezone.utc)  # SG lunch begins
        return StepResult(label=label, status="ok")

    result = produce_scanner_cycle(
        run_id="market-clock", config=config, project_root=Path(__file__).parents[1],
        python_executable="python", runner=runner, now=clock[0], clock=lambda: clock[0],
    )
    assert calls == ["scanner_in"]
    assert result.scanner_markets["SG"]["status"] == "off_hours"


def test_stock_loader_requires_scanner_contract_and_finite_score(tmp_path):
    path = tmp_path / "stock.json"
    payload = build_current_run_payload("r", [
        {**_row({
            "SIGNAL_SCANNER_MARKET": "SG", "SIGNAL_SCANNER_SESSION_DATE": "2026-10-15",
            "SIGNAL_SCANNER_TIMESTAMP": SG_OPEN.isoformat(),
        }), "score": float("nan")},
        {**_row({
            "SIGNAL_SCANNER_MARKET": "SG", "SIGNAL_SCANNER_SESSION_DATE": "2026-10-15",
            "SIGNAL_SCANNER_TIMESTAMP": SG_OPEN.isoformat(),
        }), "actionable": False},
        _row({
            "SIGNAL_SCANNER_MARKET": "SG", "SIGNAL_SCANNER_SESSION_DATE": "2026-10-15",
            "SIGNAL_SCANNER_TIMESTAMP": SG_OPEN.isoformat(),
        }),
    ])
    write_current_run_payload(path, payload)
    result = load_current_run_stocks(path, "r")
    assert len(result.stocks) == 1
    assert result.stocks[0]["source"] == "scanner"
    assert result.malformed_timestamps == 2  # non-finite and missing-validity rows are malformed


def test_signal_stack_scanner_payload_builder_preserves_explicit_contract(tmp_path, monkeypatch):
    # Import-time broker logging must not touch the user's OpenD directory.
    import sys
    monkeypatch.setitem(sys.modules, "moomoo", None)
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "tradingview"))
    try:
        import signal_stack
        rows = signal_stack._scheduler_stock_rows([
            {"tv_symbol": "SGX:BS6", "composite": 81.5, "signal": "BUY", "actionable": True,
             "fire_time": "scan-time", "resolution": "D"},
            {"tv_symbol": "SGX:VETO", "composite": 99, "signal": "BUY", "actionable": False},
            {"tv_symbol": "SGX:NAN", "composite": float("nan"), "actionable": True},
        ], screener=True, env={
            "SIGNAL_SCANNER_MARKET": "SG",
            "SIGNAL_SCANNER_SESSION_DATE": "2026-10-15",
            "SIGNAL_SCANNER_TIMESTAMP": SG_OPEN.isoformat(),
        })
    finally:
        sys.path.pop(0)
    path = tmp_path / "scanner-producer-test.json"
    try:
        payload = build_current_run_payload("r", rows)
        write_current_run_payload(path, payload)
        loaded = load_current_run_stocks(path, "r")
    finally:
        path.unlink(missing_ok=True)
    assert payload["status"] == "success"
    assert len(loaded.stocks) == 1
    assert loaded.stocks[0]["source"] == "scanner"
    assert loaded.stocks[0]["market"] == "SG"
    assert loaded.stocks[0]["session_date"] == "2026-10-15"


def test_scanner_identity_is_daily_source_and_market_specific():
    row = {
        "source": "scanner", "pro_symbol": "NASDAQ:AAA", "market": "US",
        "direction": "BUY", "session_date": "2026-10-15", "fire_time": "t1",
    }
    key = stock_delivery_key(RECIPIENT, row)
    assert key == stock_delivery_key(RECIPIENT, {**row, "fire_time": "t2"})
    assert key != stock_delivery_key(RECIPIENT, {**row, "session_date": "2026-10-16"})
    assert key != stock_delivery_key(RECIPIENT, {**row, "market": "SG"})
    assert key != stock_delivery_key(RECIPIENT, {**row, "source": "WIN"})


def test_scoring_result_recovery_uses_same_scanner_daily_identity():
    from tradingview.scheduler.dispatch import _scoring_result_to_stock

    durable = {
        "pro_symbol": "SGX:BS6", "fire_time": "run-1", "resolution": "D",
        "result": {
            "composite": 81.5, "signal": "BUY", "actionable": True,
            "source": "scanner", "market": "SG", "session_date": "2026-10-15",
            "timestamp": SG_OPEN.isoformat(), "direction": "BUY",
        },
    }
    recovered = _scoring_result_to_stock(durable)
    assert recovered["source"] == "scanner"
    assert stock_delivery_key(RECIPIENT, recovered) == stock_delivery_key(RECIPIENT, {
        "pro_symbol": "SGX:BS6", "fire_time": "later-run", "resolution": "D",
        "source": "scanner", "market": "SG", "session_date": "2026-10-15",
        "timestamp": SG_OPEN.isoformat(), "actionable": True, "direction": "BUY",
    })


class _Bot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return type("Message", (), {"message_id": len(self.messages)})()


def test_registered_scanner_job_runs_producer_and_durable_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "SG")
    monkeypatch.setenv("SCHEDULER_SCANNER_EVERY_HOURS", "2")
    monkeypatch.setenv("SCHEDULER_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("SCHEDULER_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("SCHEDULER_ALERT_TTL_HOURS", "12")
    monkeypatch.setattr(rs, "_allowed_users", lambda: {RECIPIENT})
    now = [SG_FIRST_OPEN]
    monkeypatch.setattr(rs, "_now_utc", lambda: now[0])
    monkeypatch.setattr(rs, "_scanner_last_attempted", {})
    outbox = NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"), clock=lambda: now[0],
    )
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    calls = []

    def fake_run_step(label, cmd, *, cwd, env, timeout):
        calls.append((label, cmd))
        row = _row(env)
        payload = build_current_run_payload(env["SIGNAL_RUN_ID"], [row])
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], payload)
        return StepResult(label=label, status="ok", command=tuple(cmd))

    monkeypatch.setattr("tradingview.scheduler.steps.run_step", fake_run_step)

    class JobQueue:
        def __init__(self):
            self.jobs = []
        def run_repeating(self, callback, **kwargs):
            self.jobs.append((callback, kwargs))
        def run_daily(self, callback, **kwargs):
            self.jobs.append((callback, kwargs))
        def run_once(self, callback, **kwargs):
            self.jobs.append((callback, kwargs))

    class App:
        def __init__(self):
            self.job_queue = JobQueue()

    app = App()
    rs._setup_job_queue(app)
    callback, job = next((cb, kw) for cb, kw in app.job_queue.jobs if kw.get("name") == "tradingview_scanner")
    assert job["interval"] == 300
    assert callback is rs._job_scanner_tick

    bot = _Bot()
    context = type("Context", (), {"bot": bot})()
    asyncio.run(callback(context))
    assert [label for label, _ in calls] == ["scanner_sg"]
    assert "--screener" in calls[0][1]
    assert "--new-only" not in calls[0][1]
    assert len(bot.messages) == 1
    assert "SCANNER/SG" in bot.messages[0]["text"]
    assert "SGX:BS6 · BUY" in bot.messages[0]["text"]

    assert outbox.counts()["sent"] == 1
    cadence_path = tmp_path / "runs" / "scanner_cadence.json"
    cadence = json.loads(cadence_path.read_text())["markets"]["SG"]
    assert cadence["last_success"] == SG_FIRST_OPEN.timestamp()
    assert cadence["failure_count"] == 0
    now[0] = SG_FIRST_OPEN.replace(minute=20)
    asyncio.run(callback(context))
    assert len(calls) == 1
    # A second real registered tick two hours later sees the same market-local
    # date and symbol. It must leave the one acknowledged event intact.
    now[0] = SG_FIRST_OPEN + timedelta(hours=2)
    asyncio.run(callback(context))
    assert len(calls) == 2
    assert len(bot.messages) == 1
    assert outbox.counts()["sent"] == 1
    # Reconciliation sees both scanner's scoring result source and the cycle
    # result on restart; same stable key is reserved and acknowledged once.
    queued = reconcile_cycle_results(outbox, tmp_path / "runs", [RECIPIENT])
    assert queued == 0
    assert outbox.counts()["sent"] == 1


@pytest.mark.parametrize("options_only", [False, True])
def test_scanner_failure_retries_with_persisted_bounded_backoff(tmp_path, monkeypatch, options_only):
    monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "SG")
    monkeypatch.setenv("SCHEDULER_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("SCHEDULER_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("SCHEDULER_SCANNER_RETRY_BASE_SECONDS", "300")
    monkeypatch.setenv("SCHEDULER_SCANNER_RETRY_CAP_SECONDS", "600")
    monkeypatch.setattr(rs, "_allowed_users", lambda: {RECIPIENT})
    now = [SG_OPEN]
    monkeypatch.setattr(rs, "_now_utc", lambda: now[0])
    monkeypatch.setattr(rs, "_scanner_last_attempted", {})
    retry_outbox = NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"), clock=lambda: now[0],
    )
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: retry_outbox)
    calls = []
    behavior = ["failed"]
    if options_only:
        monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "US")
        monkeypatch.setenv("SCHEDULER_SCANNER_US_OPTIONS_ENABLED", "true")
        now[0] = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)
    origin = now[0]
    market = "US" if options_only else "SG"
    label = "scanner_us" if options_only else "scanner_sg"

    def failed_step(label, cmd, *, cwd, env, timeout):
        calls.append(label)
        if behavior[0] == "failed" and not options_only:
            return StepResult(label=label, status="failed", error="provider timeout")
        row = _row(env, symbol="NYSE:RTX" if options_only else "SGX:BS6")
        if options_only:
            row["quote_symbol"] = "RTX"
            from tradingview.options.options_snapshot import build_snapshot, publish_snapshot
            publish_snapshot(Path(env["SIGNAL_OPTIONS_SNAPSHOT"]), build_snapshot(
                env["SIGNAL_RUN_ID"], "unavailable" if behavior[0] == "failed" else "empty",
                errors=[{"message": "options provider unavailable"}] if behavior[0] == "failed" else [],
                generated_at=now[0].isoformat()))
        write_current_run_payload(
            env["SIGNAL_STOCK_PAYLOAD"],
            build_current_run_payload(env["SIGNAL_RUN_ID"], [row]),
        )
        return StepResult(label=label, status="ok")

    monkeypatch.setattr("tradingview.scheduler.steps.run_step", failed_step)
    bot = _Bot()
    context = type("Context", (), {"bot": bot})()
    asyncio.run(rs._job_scanner_tick(context))
    assert calls == [label]
    assert len(bot.messages) == 1
    state_path = tmp_path / "runs" / "scanner_cadence.json"
    first_state = json.loads(state_path.read_text())["markets"][market]
    assert first_state["failure_count"] == 1
    assert first_state["retry_at"] == origin.timestamp() + 300
    assert not first_state.get("last_success")

    now[0] += timedelta(minutes=4)
    asyncio.run(rs._job_scanner_tick(context))
    assert calls == [label]

    now[0] += timedelta(minutes=2)
    asyncio.run(rs._job_scanner_tick(context))
    assert calls == [label, label]
    second_state = json.loads(state_path.read_text())["markets"][market]
    assert second_state["failure_count"] == 2
    assert second_state["retry_at"] == origin.timestamp() + 6 * 60 + 600
    assert len(bot.messages) == (2 if options_only else 1)

    behavior[0] = "success"
    now[0] = origin + timedelta(minutes=16)
    asyncio.run(rs._job_scanner_tick(context))
    success_state = json.loads(state_path.read_text())["markets"][market]
    assert success_state["failure_count"] == 0
    assert success_state["retry_at"] == 0
    assert success_state["last_success"] == now[0].timestamp()


def test_calendar_failure_is_not_quiet_and_backoff_does_not_starve_other_markets(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "SG,AU")
    monkeypatch.setenv("SCHEDULER_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("SCHEDULER_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(rs, "_allowed_users", lambda: {RECIPIENT})
    now = [SG_OPEN]
    monkeypatch.setattr(rs, "_now_utc", lambda: now[0])
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: now[0])
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    calls = []

    def calendar(market, moment, *, force=False):
        if market == "SG":
            raise RuntimeError("calendar coverage unavailable")
        return True, "2026-10-15"

    monkeypatch.setattr("tradingview.scheduler.scanner.market_session", calendar)

    def runner(label, cmd, *, cwd, env, timeout):
        calls.append(label)
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], build_current_run_payload(env["SIGNAL_RUN_ID"], []))
        return StepResult(label=label, status="ok")

    monkeypatch.setattr("tradingview.scheduler.steps.run_step", runner)
    bot = _Bot()
    context = type("Context", (), {"bot": bot})()

    asyncio.run(rs._job_scanner_tick(context))
    assert calls == []  # calendar failure occurs before starting any subprocess
    assert len(bot.messages) == 1
    state_path = tmp_path / "runs" / "scanner_cadence.json"
    first_state = json.loads(state_path.read_text())["markets"]["SG"]
    assert first_state["failure_count"] == 1
    assert first_state["retry_at"] == SG_OPEN.timestamp() + 300

    # SG is cooling down, so the independent AU market gets the next tick.
    now[0] += timedelta(minutes=4)
    asyncio.run(rs._job_scanner_tick(context))
    assert calls == ["scanner_au"]
    assert len(bot.messages) == 1  # a successful empty market scan stays silent


def test_scanner_retries_after_shared_lock_contention(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "SG")
    monkeypatch.setenv("SCHEDULER_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("SCHEDULER_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(rs, "_allowed_users", lambda: {RECIPIENT})
    monkeypatch.setattr(rs, "_now_utc", lambda: SG_OPEN)
    monkeypatch.setattr(rs, "_scanner_last_attempted", {})
    # Another cycle owns the shared lock; the next five-minute tick should
    # retry because no per-market attempt time was recorded.
    from tradingview.scheduler.cycle import CycleLock
    config = load_config()
    lock = CycleLock(config.run_root / ".cycle.lock")
    assert lock.acquire("test-owner")
    calls = []

    def fake_run_step(label, cmd, *, cwd, env, timeout):
        calls.append(label)
        payload = build_current_run_payload(env["SIGNAL_RUN_ID"], [_row(env)])
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], payload)
        return StepResult(label=label, status="ok")

    monkeypatch.setattr("tradingview.scheduler.steps.run_step", fake_run_step)
    try:
        bot = _Bot()
        context = type("Context", (), {"bot": bot})()
        asyncio.run(rs._job_scanner_tick(context))
        assert rs._scanner_last_attempted == {}
    finally:
        lock.release()
    asyncio.run(rs._job_scanner_tick(context))
    assert calls == ["scanner_sg"]
    assert rs._scanner_last_attempted == {"SG": SG_OPEN.timestamp()}


def test_registered_outbox_retry_acknowledges_scanner_event(tmp_path, monkeypatch):
    from datetime import timedelta

    monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "SG")
    monkeypatch.setenv("SCHEDULER_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("SCHEDULER_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(rs, "_allowed_users", lambda: {RECIPIENT})
    monkeypatch.setattr(rs, "_now_utc", lambda: SG_OPEN)
    clock = [SG_OPEN]
    outbox = NotificationOutbox(
        sqlite_factory(tmp_path / "outbox.db"), clock=lambda: clock[0],
        backoff_base_seconds=1,
    )
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)

    def fake_run_step(label, cmd, *, cwd, env, timeout):
        payload = build_current_run_payload(env["SIGNAL_RUN_ID"], [_row(env)])
        write_current_run_payload(env["SIGNAL_STOCK_PAYLOAD"], payload)
        return StepResult(label=label, status="ok")

    monkeypatch.setattr("tradingview.scheduler.steps.run_step", fake_run_step)

    class FlakyBot(_Bot):
        async def send_message(self, **kwargs):
            if not self.messages:
                self.messages.append(kwargs)
                raise TimeoutError("network timeout")
            return await super().send_message(**kwargs)

    bot = FlakyBot()
    result = asyncio.run(rs._run_scanner_cycle(bot, ("SG",)))
    assert result.stocks
    assert outbox.pending_count() == 1
    clock[0] += timedelta(seconds=2)
    asyncio.run(rs._job_outbox_dispatch(type("Context", (), {"bot": bot})()))
    assert outbox.pending_count() == 0
    assert outbox.counts()["sent"] == 1


def test_test_scope_failure_never_enqueues_or_sends_and_uses_isolated_run_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_SCANNER_SCOPE", "test")
    monkeypatch.setenv("SCHEDULER_SCANNER_MARKETS", "SG")
    production_root = tmp_path / "runs"
    monkeypatch.setenv("SCHEDULER_RUN_ROOT", str(production_root))
    monkeypatch.setenv("SCHEDULER_OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(rs, "_allowed_users", lambda: {RECIPIENT})
    now = [SG_OPEN]
    monkeypatch.setattr(rs, "_now_utc", lambda: now[0])
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: now[0])
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    observed_env = []

    def failed_step(label, cmd, *, cwd, env, timeout):
        observed_env.append(env)
        return StepResult(label=label, status="failed", error="provider unavailable")

    monkeypatch.setattr("tradingview.scheduler.steps.run_step", failed_step)
    bot = _Bot()
    result = asyncio.run(rs._run_scanner_cycle(bot, ("SG",)))

    assert result.status == "failed"
    assert observed_env[0]["SIGNAL_SCANNER_SCOPE"] == "test"
    assert Path(observed_env[0]["SIGNAL_REPORT_DIR"]).is_relative_to(
        tmp_path / "runs_scanner_test_scope"
    )
    assert not production_root.exists()
    assert not bot.messages
    assert outbox.pending_count() == 0

    # Scheduled test ticks must not update the production cadence file either.
    asyncio.run(rs._job_scanner_tick(type("Context", (), {"bot": bot})()))
    assert not (production_root / "scanner_cadence.json").exists()


def test_manual_scan_always_reports_empty_or_busy_completion(monkeypatch):
    bot = _Bot()
    empty = CycleResult(
        run_id="empty", started_at=SG_OPEN.isoformat(), status="no_new",
        scanner_markets={"SG": {"status": "no_new", "count": 0}},
    )
    async def return_empty(*args, **kwargs):
        return empty
    monkeypatch.setattr(rs, "_run_scanner_cycle", return_empty)
    asyncio.run(rs._manual_scanner_run(bot, RECIPIENT, ("SG",)))
    assert "SG no_new (0 alerts)" in bot.messages[-1]["text"]

    busy = CycleResult(
        run_id="busy", started_at=SG_OPEN.isoformat(), status="failed",
        errors=["another analysis cycle holds the shared execution lock"],
    )
    async def return_busy(*args, **kwargs):
        return busy
    monkeypatch.setattr(rs, "_run_scanner_cycle", return_busy)
    asyncio.run(rs._manual_scanner_run(bot, RECIPIENT, ("SG",)))
    assert "holds the shared lock" in bot.messages[-1]["text"]


def test_tradingview_adapter_accepts_legacy_text_recommendation(monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "tradingview"))
    try:
        import input_sources
        monkeypatch.setattr(input_sources, "_run_all_scans", lambda args: {
            "stocks_sg": [{
                "tv_symbol": "SGX:BS6", "RSI": 58.0, "ADX": 25.0,
                "relative_volume_10d_calc": 1.2, "close": 3.1,
                "recommendation_mark": "Strong Buy",
            }],
        })
        fires, meta = input_sources.tv_screener_to_fires(
            classes=["stocks_sg"], max_per_class=7,
        )
    finally:
        sys.path.pop(0)
    assert len(fires) == 1
    assert "recommendation:Strong Buy" in fires[0]["message"]
    assert meta["classes_scanned"] == ["stocks_sg"]


def test_automatic_scanner_waits_for_first_bar_and_delay(tmp_path, monkeypatch):
    monkeypatch.setenv('SCHEDULER_SCANNER_MARKETS', 'AU')
    monkeypatch.setenv('SCHEDULER_SCANNER_SCOPE', 'production')
    monkeypatch.setenv('SCHEDULER_RUN_ROOT', str(tmp_path / 'runs'))
    monkeypatch.setenv('SCHEDULER_SCANNER_PROVIDER_DELAY_MINUTES', '15')
    monkeypatch.setattr(rs, '_scheduler_paused', False)
    now = [datetime(2026, 10, 18, 23, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(rs, '_now_utc', lambda: now[0])
    calls = []
    async def run(bot, markets):
        calls.append(markets)
        return None
    monkeypatch.setattr(rs, '_run_scanner_cycle', run)
    context = type('Context', (), {'bot': None})()
    asyncio.run(rs._job_scanner_tick(context))
    assert not calls
    assert not (tmp_path / 'runs' / 'scanner_cadence.json').exists()
    now[0] = now[0].replace(minute=30)
    asyncio.run(rs._job_scanner_tick(context))
    assert calls == [('AU',)]
