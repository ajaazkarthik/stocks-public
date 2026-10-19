"""Offline integration: real analyzer, snapshot, consumer, SQLite and recovery.

Run with tests.acceptance_safety_plugin and a temporary acceptance root.
Only chain I/O and Telegram transport are replaced; the ranking/selection,
parent persistence, normalization, rendering and delivery machinery are real.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradingview.options import options_badge
from tradingview.options import options_snapshot
from tradingview.options.options_analyzer import OptionsAnalyzer
from tradingview.options.us_scanner_producer import produce_us_scanner_options
from tradingview.scanner_store import record_observation
from tradingview.scheduler.cycle import CycleResult, _options_result_from_snapshot, enqueue_cycle
from tradingview.scheduler.dispatch import dispatch_pending, reconcile_cycle_results
from tradingview.scheduler.outbox import NotificationOutbox
from tradingview.scheduler.stock_results import StockResult
from tradingview.scheduler.us_options_policy import USOptionsPolicy

NOW = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)


@pytest.fixture(params=("strict", "reference"))
def pipeline(tmp_path, monkeypatch, mocker, request):
    parent = dict(source="scanner", market="US", scope="production",
                  session_date="2026-10-19", direction="BUY", actionable=True,
                  tv_symbol="NYSE:RTX", composite=88.5, instrument_type="equity",
                  run_id="parent-run", timestamp=NOW.isoformat())
    parent.update(bar_time=(NOW-timedelta(minutes=15)).isoformat(),
                  bar_end_time=NOW.isoformat(), bar_interval_minutes=15,
                  quote_source="Yahoo Finance 15m chart",
                  freshness_assurance="independent_chart_crosscheck",
                  configured_provider_delay_minutes=15)
    with sqlite3.connect(tmp_path / "parents.db") as conn:
        setup, _, _ = record_observation(conn, parent, run_id="parent-run")
    # Durability marker is assigned only after the transaction has committed.
    parent.update(scanner_setup_id=setup, durably_persisted=True)
    raw = dict(exchange="NYSE", symbol="RTX", option_type="CALL", strike=195,
               expiry="2026-11-06", dte=18, bid=4.1, ask=4.3, mid_price=4.2,
               spread_pct=4.7619, oi=1500, volume=900, iv=25, delta=0.5,
               spot_price=195, moneyness_pct=0, market="US", source="fixture",
               quote_provider="fixture", volume_session_date="2026-10-19",
               fetch_completed_at=NOW.isoformat(), contract_identifier="RTX261106C00195000")
    if request.param == "strict":
        raw.update(bid_quote_at=(NOW-timedelta(seconds=10)).isoformat(),
                   ask_quote_at=(NOW-timedelta(seconds=20)).isoformat())
    else:
        raw.update(tv_update_time=(NOW-timedelta(seconds=10)).timestamp(),
                   oi_source="yfinance_exact_expiry_strike",
                   volume_session_provenance="provider_session_date")
    if request.param == "strict":
        raw.update(source="tv", bid_quote_at=(NOW-timedelta(seconds=10)).isoformat(),
                   ask_quote_at=(NOW-timedelta(seconds=20)).isoformat(),
                   oi_source="fixture_reported_oi")
    else:
        raw.update(source="tv", oi=0, tv_update_time=(NOW-timedelta(seconds=10)).timestamp())
        monkeypatch.setenv("SCHEDULER_SCANNER_US_OPTIONS_QUOTE_MODE", "reference")
        yf = mocker.patch("tradingview.options.yf_options.YFOptionsFetcher")
        yf.return_value.get_option_chain_for_date.return_value = [
            {"strike": 195, "expiry": "2026-11-06", "option_type": "CALL", "oi": 1500}
        ]
    monkeypatch.setenv("SIGNAL_SCANNER_TIMESTAMP", NOW.isoformat())
    tv = mocker.patch("tradingview.options.tv_options.TVOptionsFetcher")
    tv.return_value.is_available.return_value = True
    tv.return_value.get_calls.return_value = [dict(raw)]
    monkeypatch.setattr(options_badge, "_analyzer", OptionsAnalyzer())
    snapshot_path = tmp_path / "options.json"
    snapshot = produce_us_scanner_options(
        [parent], snapshot_path=snapshot_path, run_id="cycle_us",
        session_date="2026-10-19", clock=lambda: NOW,
        session_open=NOW.replace(hour=13, minute=30), session_close=NOW.replace(hour=20),
        policy=USOptionsPolicy(quote_mode=request.param), parent_threshold=62.5, option_threshold=70,
        badge_call=lambda symbol, score: options_badge.compute_options_badge_data(
            symbol, signal_score=score, scanner_all_candidates=True),
    )
    assert tv.return_value.get_calls.call_count == 1
    assert tv.return_value.get_calls.call_args.args[0] == "NYSE:RTX"
    assert snapshot["status"] == "success", json.dumps(snapshot, indent=2)
    assert len(snapshot["contracts"]) == 1
    result = _options_result_from_snapshot(snapshot_path, "cycle_us", 70, now=NOW)
    assert len(result["options"]) == 1, result
    assert result["options"][0]["parent_signal_score"] == 88.5
    assert result["options"][0]["option_score"] == snapshot["contracts"][0]["option_score"]
    if request.param == "reference":
        assert result["options"][0]["freshness_status"] == "reference_unverified"
        assert result["options"][0]["bid_quote_at"] is None
        assert "Freshness unverified" in __import__("tradingview.scheduler.formatting", fromlist=["option_card"]).option_card(result["options"][0])
    # Freeze the real reader's clock for the legacy scheduler consumer too.
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(options_snapshot, "datetime", FrozenDatetime)
    import report_scheduler
    monkeypatch.setattr(report_scheduler, "OPTIONS_SNAPSHOT", snapshot_path)
    legacy = report_scheduler._extract_options_alerts(70, "cycle_us")
    assert legacy["options"] == result["options"]
    cycle = CycleResult("cycle", NOW.isoformat(), completed_at=NOW.isoformat(),
                        stock_result=StockResult("empty"), option_result=result)
    root = tmp_path / "runs"
    cycle.write(root / "cycle" / "cycle_result.json")
    clock = [NOW]
    def box():
        return NotificationOutbox(lambda: sqlite3.connect(tmp_path / "outbox.db"),
                                  clock=lambda: clock[0])
    return cycle, root, box, clock, snapshot


@pytest.mark.parametrize("crash_before_enqueue", [True, False])
def test_real_pipeline_recovers_and_acknowledges_once(pipeline, crash_before_enqueue):
    cycle, root, box, clock, snapshot = pipeline
    first = box()
    if not crash_before_enqueue:
        assert enqueue_cycle(first, cycle, [101], limit=3500, scanner_option_cap=1) == 1
    restarted = box()
    assert reconcile_cycle_results(restarted, root, [101]) == int(crash_before_enqueue)
    assert reconcile_cycle_results(restarted, root, [101]) == 0
    pending = restarted.all_items()
    assert len(pending) == 1
    deadline = snapshot["contracts"][0]["recommendation_expires_at"]
    assert datetime.fromisoformat(pending[0].expires_at) == datetime.fromisoformat(deadline)
    sends = []
    async def transport(**kwargs):
        sends.append(kwargs)
        return SimpleNamespace(message_id=123)
    report = asyncio.run(dispatch_pending(restarted, send=transport))
    assert report.sent == 1, report
    assert len(sends) == 1
    assert "USD" in sends[0]["text"] and "Scanner US" in sends[0]["text"]
    if snapshot["contracts"][0]["freshness_status"] == "reference_unverified":
        assert "Freshness unverified" in sends[0]["text"]
        assert "not a bid/ask timestamp" in sends[0]["text"]
    assert reconcile_cycle_results(box(), root, [101]) == 0
    assert asyncio.run(dispatch_pending(box(), send=transport)).sent == 0
    assert len(sends) == 1


def test_real_pipeline_expiry_cannot_be_refreshed_by_restart(pipeline):
    cycle, root, box, clock, snapshot = pipeline
    assert enqueue_cycle(box(), cycle, [101], limit=3500, scanner_option_cap=1) == 1
    clock[0] += timedelta(minutes=3)
    async def forbidden(**kwargs):
        pytest.fail("expired option reached transport")
    assert asyncio.run(dispatch_pending(box(), send=forbidden)).sent == 0
    assert reconcile_cycle_results(box(), root, [101]) == 0
    persisted = json.loads((root / "cycle" / "cycle_result.json").read_text())
    expected_time_field = "tv_update_time" if snapshot["contracts"][0]["freshness_status"] == "reference_unverified" else "bid_quote_at"
    assert persisted["option_result"]["options"][0][expected_time_field] == snapshot["contracts"][0][expected_time_field]
