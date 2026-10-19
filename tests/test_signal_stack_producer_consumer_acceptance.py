"""Producer-consumer acceptance for the committed strict handoff.

The test calls signal_stack.main() directly.  It disables every enrichment,
network, broker, tracker, archive, and database seam, while retaining the real
report-writing block and the real strict options reader.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from test_signal_stack_handoff_acceptance import SafetyBoundary, RUN_ID


@pytest.fixture
def safety(tmp_path, monkeypatch):
    boundary = SafetyBoundary(tmp_path)
    boundary.install(monkeypatch)
    try:
        yield boundary
    finally:
        boundary.assert_clean()


def _fake_fundamentals_module(current_price=100.0, market_cap=10_000_000_000):
    module = ModuleType("tv_screener_fundamentals")
    module.fetch_tv_fundamentals = lambda symbols, use_auth=True: {
        symbol: {
            "current_price": current_price,
            "currency": "USD",
            "market_cap": market_cap,
            "company_name": "Acceptance Fixture",
            "sector": "Technology",
        }
        for symbol in symbols
    }
    module.tv_fund_to_compat = lambda data, pro_symbol="": dict(data)
    module.fetch_yield_curve = lambda: {"spread_bps": 10, "source": "fixture"}
    module.fetch_vix_level = lambda: 18.0
    module.fetch_vix3m_level = lambda: 20.0
    module.fetch_sector_etf_momentum = lambda: {}
    module.get_sector_etf_detail = lambda sector: {"ticker": "XLK", "pct": 0.0}
    module.get_sector_etf_score = lambda sector: None
    return module


def _fake_macro_module():
    module = ModuleType("fred_macro")
    module.fetch_macro_signals = lambda: {"spread_bps": 10, "source": "fixture"}
    return module


def _fake_report_db_module():
    module = ModuleType("report_db")
    module.archive_before_write = lambda *args, **kwargs: None
    module.save_signal_stack_snapshot = lambda *args, **kwargs: None
    return module


def _fake_fires_ledger_module():
    module = ModuleType("database.fires_ledger")
    module.fire_key = lambda fire: (
        fire.get("pro_symbol", ""), fire.get("fire_time", ""), fire.get("resolution", "")
    )
    module.persist_run = lambda *args, **kwargs: None
    module.mark_scored = lambda fires: len(fires)

    class NoopConnection:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    module._conn = lambda: NoopConnection()
    return module


def _configure_signal_stack(monkeypatch, safety, fires_path, output_path, actionable_path,
                            real_actionability=False, fundamentals_price=100.0,
                            market_cap=10_000_000_000):
    monkeypatch.setitem(sys.modules, "tv_screener_fundamentals", _fake_fundamentals_module(fundamentals_price, market_cap))
    monkeypatch.setitem(sys.modules, "fred_macro", _fake_macro_module())
    monkeypatch.setitem(sys.modules, "report_db", _fake_report_db_module())
    monkeypatch.setitem(sys.modules, "database.fires_ledger", _fake_fires_ledger_module())
    tradingview_root = Path(__file__).resolve().parents[1] / "tradingview"
    monkeypatch.syspath_prepend(str(tradingview_root))
    signal_stack = importlib.reload(importlib.import_module("tradingview.signal_stack"))
    monkeypatch.setenv("SIGNAL_RUN_ID", RUN_ID)
    monkeypatch.setattr(signal_stack, "FIRES_FILE", str(fires_path))
    monkeypatch.setattr(signal_stack, "OUTPUT_HTML", str(output_path))
    monkeypatch.setattr(signal_stack, "_OPTIONS_SNAPSHOT_PATH", safety.reports / "options_signals.json")
    monkeypatch.setattr(signal_stack, "_MOOMOO_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_INDIA_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_IBKR_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_EXTERNAL_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_AI_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_EDGAR_OK", False)
    monkeypatch.setattr(signal_stack, "_TRACKER_OK", False)
    monkeypatch.setattr(signal_stack, "_ADVISOR_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_SIZER_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_OPTIONS_AVAILABLE", False)
    monkeypatch.setattr(signal_stack, "_TRADABLE_EXCHS", {"NASDAQ"})
    monkeypatch.setattr(signal_stack, "_ensure_paths", lambda *args, **kwargs: None)
    monkeypatch.setattr(signal_stack, "_archive_reports", lambda *args, **kwargs: None)
    monkeypatch.setattr(signal_stack, "_publish_options_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(signal_stack, "build_html", lambda *args, **kwargs: "<!doctype html>\n<html></html>")
    monkeypatch.setattr(signal_stack, "fetch_tv_screener", lambda symbols: {
        symbol: {"rel_vol": 1.0} for symbol in symbols
    })
    monkeypatch.setattr(signal_stack, "tv_to_yf", lambda symbol: symbol.split(":")[-1])
    monkeypatch.setattr(signal_stack, "parse_fire_meta", lambda message: {})
    monkeypatch.setattr(signal_stack, "resolve_etf_underlying", lambda symbol: (None, None))
    monkeypatch.setattr(signal_stack, "score_technical", lambda *args, **kwargs: {"total": 8.0})
    monkeypatch.setattr(signal_stack, "score_fundamental", lambda *args, **kwargs: {"total": 8.0})
    monkeypatch.setattr(signal_stack, "score_sentiment", lambda *args, **kwargs: {"total": 8.0})
    monkeypatch.setattr(signal_stack, "score_market_internals", lambda *args, **kwargs: {"total": 8.0})
    monkeypatch.setattr(signal_stack, "fetch_sentiment_data", lambda *args, **kwargs: {})
    monkeypatch.setattr(signal_stack, "fetch_market_internals", lambda *args, **kwargs: {})
    monkeypatch.setattr(signal_stack, "_prefetch_yf", lambda *args, **kwargs: {})
    monkeypatch.setattr(signal_stack, "_yf_has_quotesummary", lambda *args, **kwargs: False)
    monkeypatch.setattr(signal_stack, "_data_confidence", lambda *args, **kwargs: {"overall": 1.0})
    monkeypatch.setattr(signal_stack, "_get_regime_weights", lambda *args, **kwargs: {})
    monkeypatch.setattr(signal_stack, "_classify_market_regime", lambda *args, **kwargs: "NORMAL")
    monkeypatch.setattr(signal_stack, "_compute_decision_scores", lambda *args, **kwargs: (88.5, 88.5))
    monkeypatch.setattr(signal_stack, "_score_breakdown", lambda *args, **kwargs: {"fixture": True})
    if not real_actionability:
        monkeypatch.setattr(signal_stack, "_actionability_reasons", lambda *args, **kwargs: [])
    monkeypatch.setattr(signal_stack, "_compute_tradable_with_limitations", lambda *args, **kwargs: {})
    monkeypatch.setattr(signal_stack, "_set_sentiment_backend", lambda *args, **kwargs: None)
    monkeypatch.setattr(signal_stack.time, "sleep", lambda *args, **kwargs: None)

    class FakeTicker:
        ticker = "TEST"

        def history(self, *args, **kwargs):
            return []

    monkeypatch.setattr(signal_stack.yf, "Ticker", lambda symbol: FakeTicker())
    monkeypatch.setattr(signal_stack.yf, "download", lambda *args, **kwargs: SimpleNamespace(empty=True))
    return signal_stack


def test_actual_writer_output_is_accepted_by_actual_strict_reader(
    monkeypatch, safety
):
    fires_path = safety.tmp_path / "fires_raw.json"
    output_path = safety.reports / f"signal_stack_report_{RUN_ID}.html"
    actionable_path = safety.reports / f"actionable_report_{RUN_ID}.html"
    fires_path.write_text(json.dumps({
        "all": [{
            "pro_symbol": "NASDAQ:TEST",
            "symbol": "NASDAQ:TEST",
            "alert_id": 2381963449,
            "fire_time": "2020-01-01T00:00:00Z",
            "resolution": "1h",
            "message": "BUY price: 100.0",
        }]
    }), encoding="utf-8")

    signal_stack = _configure_signal_stack(
        monkeypatch, safety, fires_path, output_path, actionable_path
    )
    monkeypatch.setattr(sys, "argv", [
        "signal_stack.py",
        "--n", "1",
        "--threshold", "62.5",
        "--no-moomoo",
        "--no-india",
        "--no-external",
        "--no-options",
        "--no-book-context",
        "--min-market-cap", "0",
        "--out", str(output_path),
        "--actionable-out", str(actionable_path),
    ])
    signal_stack.main()

    actionable_json = actionable_path.with_suffix(".json")
    payload = json.loads(actionable_json.read_text(encoding="utf-8"))
    assert payload["status"] == "complete"
    assert payload["run_id"] == RUN_ID
    parsed = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    assert payload["n_actionable"] == 1
    candidate = payload["actionable_signals"][0]
    assert candidate["symbol"] == "NASDAQ:TEST"
    assert candidate["decision_score"] == 88.5
    assert candidate["score"] == 88.5
    assert actionable_json == safety.reports / f"actionable_report_{RUN_ID}.json"
    assert actionable_json.exists()
    assert sorted(path.name for path in safety.reports.iterdir()) == sorted([
        f"signal_stack_report_{RUN_ID}.html",
        f"signal_stack_report_{RUN_ID}.json",
        f"actionable_report_{RUN_ID}.html",
        f"actionable_report_{RUN_ID}.json",
    ])

    options = importlib.import_module("tradingview.options.run_options")
    report, reason = options._read_strict_actionable_report(actionable_json, RUN_ID)
    assert reason == "valid"
    assert report["actionable_signals"][0]["score"] == 88.5
    signals = options._load_actionable_signals(
        actionable_file=actionable_json,
        strict=True,
        expected_run_id=RUN_ID,
    )
    assert signals[0]["tv_symbol"] == "NASDAQ:TEST"
    assert signals[0]["composite"] == 88.5
    safety.assert_clean()


def test_strict_gate_policy_agrees_across_all_artifacts(monkeypatch, safety):
    """Issue 10: report selection must honor --require-trigger end to end.

    The fire's trigger price is above the fixture price, so the strict gate
    rejects the row. The all-signals report still lists it, but the actionable
    report must fall back to WATCH with zero actionables, and the options
    handoff must see no candidates.
    """
    fires_path = safety.tmp_path / "fires_raw.json"
    output_path = safety.reports / f"signal_stack_report_{RUN_ID}.html"
    actionable_path = safety.reports / f"actionable_report_{RUN_ID}.html"
    fires_path.write_text(json.dumps({
        "all": [{
            "pro_symbol": "NASDAQ:TEST",
            "symbol": "NASDAQ:TEST",
            "alert_id": 2381963449,
            "fire_time": "2020-01-01T00:00:00Z",
            "resolution": "1h",
            "message": "BUY price: 100.0, trigger: 500.0",
        }]
    }), encoding="utf-8")

    signal_stack = _configure_signal_stack(
        monkeypatch, safety, fires_path, output_path, actionable_path,
        real_actionability=True,
    )
    monkeypatch.setattr(signal_stack, "parse_fire_meta",
                        lambda message: {"trigger_price": 500.0})
    monkeypatch.setattr(sys, "argv", [
        "signal_stack.py",
        "--n", "1",
        "--threshold", "62.5",
        "--require-trigger",
        "--no-moomoo",
        "--no-india",
        "--no-external",
        "--no-options",
        "--no-book-context",
        "--min-market-cap", "0",
        "--out", str(output_path),
        "--actionable-out", str(actionable_path),
    ])
    signal_stack.main()

    # All-signals report still contains the scored row.
    all_json = output_path.with_suffix(".json")
    all_payload = json.loads(all_json.read_text(encoding="utf-8"))
    assert all_payload["n_total"] == 1

    # Actionable report falls back to WATCH with zero actionables.
    act_payload = json.loads(actionable_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert act_payload["is_watch"] is True
    assert act_payload["n_actionable"] == 0
    assert act_payload["actionable_signals"] == []
    assert len(act_payload["watch_signals"]) == 1
    assert act_payload["watch_signals"][0]["symbol"] == "NASDAQ:TEST"
    safety.assert_clean()


def _run_actual_scanner_stack(monkeypatch, safety, *, adapter, actionability=None,
                              database_path=None, run_id=RUN_ID, scope="production",
                              freshness=None, fail_scoring=False,
                              session_date="2026-10-15",
                              observed_at="2026-10-15T02:00:00+00:00",
                              tracker=None, fundamentals_price=100.0,
                              score_override=None, market="SG", market_cap=10_000_000_000,
                              min_market_cap=0, fail_persistence=False,
                              options_enabled=False, options_analyzer=None):
    """Exercise signal_stack.main's real scanner handoff with fake externals."""
    import_sources_root = Path(__file__).resolve().parents[1] / "tradingview"
    monkeypatch.syspath_prepend(str(import_sources_root))
    import input_sources
    monkeypatch.setattr(input_sources, "tv_screener_to_fires", adapter)
    corrupt_cache = safety.tmp_path / "fires_raw.json"
    corrupt_cache.write_text("{broken cache", encoding="utf-8")
    output_path = safety.reports / f"scanner_{RUN_ID}.html"
    actionable_path = safety.reports / f"scanner_actionable_{RUN_ID}.html"
    signal_stack = _configure_signal_stack(
        monkeypatch, safety, corrupt_cache, output_path, actionable_path,
        fundamentals_price=fundamentals_price,
        market_cap=market_cap,
    )
    ledger_module = sys.modules["database.fires_ledger"]
    database_path = database_path or (safety.tmp_path / "scanner.sqlite")

    @contextmanager
    def temporary_connection():
        connection = sqlite3.connect(database_path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    ledger_module._conn = temporary_connection
    payload_path = safety.reports / "scanner_stock_results.json"
    monkeypatch.setattr(signal_stack, "_STOCK_PAYLOAD_PATH", payload_path)
    screener_class = {"SG": "stocks_sg", "AU": "stocks_au", "US": "stocks_us", "IN": "stocks_in"}[market]
    monkeypatch.setenv("SIGNAL_SCANNER_MARKET", market)
    monkeypatch.setenv("SIGNAL_RUN_ID", run_id)
    monkeypatch.setenv("SIGNAL_SCANNER_SESSION_DATE", session_date)
    monkeypatch.setenv("SIGNAL_SCANNER_TIMESTAMP", observed_at)
    monkeypatch.setenv("SIGNAL_SCANNER_SCOPE", scope)
    monkeypatch.setenv("SIGNAL_REPORT_DIR", str(safety.reports))
    monkeypatch.setenv("SCHEDULER_SCANNER_US_OPTIONS_ENABLED", "true" if options_enabled else "false")
    monkeypatch.setenv("SCHEDULER_SCORE_THRESHOLD", "70")
    if options_enabled:
        from tradingview.options import options_badge
        monkeypatch.setattr(options_badge, "_analyzer", options_analyzer)
    freshness_module = importlib.import_module("tradingview.scheduler.freshness")
    monkeypatch.setattr(
        freshness_module, "validate_scanner_fires",
        freshness or (lambda fires, **kwargs: ([{
            **fire, "bar_time": observed_at,
            "bar_end_time": observed_at,
            "bar_interval_minutes": 15,
            "quote_source": "Yahoo Finance 15m chart",
        } for fire in fires], {})),
    )
    if fail_persistence:
        scanner_store = importlib.import_module("tradingview.scanner_store")
        def fail_scanner_persistence(*args, **kwargs):
            raise sqlite3.OperationalError("fixture disk full")
        monkeypatch.setattr(scanner_store, "record_observation", fail_scanner_persistence)
    if fail_scoring:
        monkeypatch.setattr(
            signal_stack, "_compute_decision_scores",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fixture scoring failure")),
        )
    if tracker is not None:
        monkeypatch.setattr(signal_stack, "_TRACKER_OK", True)
        monkeypatch.setattr(signal_stack, "_get_tracker", lambda: tracker)
    if score_override is not None:
        monkeypatch.setattr(signal_stack, "_compute_decision_scores", lambda *a, **k: score_override)
    command = [
        "signal_stack.py", "--screener", screener_class, "--n", "7",
        "--threshold", "62.5", "--no-moomoo", "--no-india", "--no-external",
        "--no-book-context", "--min-market-cap", str(min_market_cap),
        "--out", str(output_path), "--actionable-out", str(actionable_path),
    ]
    if not options_enabled:
        command.append("--no-options")
    monkeypatch.setattr(sys, "argv", command)
    if actionability is not None:
        monkeypatch.setattr(signal_stack, "_actionability_reasons", actionability)
    signal_stack.main()
    return signal_stack, payload_path, database_path


def test_actual_scanner_mode_ignores_corrupt_fires_cache_and_writes_valid_empty(
    monkeypatch, safety,
):
    _stack, payload_path, _db_path = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([], {"classes_scanned": ["stocks_sg"]}),
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["status"] == "no_new"
    assert payload["stocks"] == []


@pytest.mark.parametrize("scope,fail_persistence", [
    ("production", False), ("production", True), ("test", False),
])
def test_actual_scanner_options_entrypoint_requires_committed_production_parent(
    monkeypatch, safety, scope, fail_persistence,
):
    from datetime import timedelta
    from tradingview.options.options_analyzer import OptionsAnalyzer
    from tradingview.options import us_scanner_producer
    from tradingview.options.options_snapshot import read_snapshot
    now = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(us_scanner_producer, "datetime", FrozenDatetime)
    analyzer = OptionsAnalyzer()
    calls = []
    def fetch(symbol):
        calls.append(symbol)
        return [{"option_type": "CALL", "strike": 100, "expiry": "2026-11-06",
                 "dte": 18, "bid": 4.1, "ask": 4.3, "mid_price": 4.2,
                 "spread_pct": 4.76, "oi": 1500, "volume": 900, "iv": 25,
                 "delta": 0.5, "spot_price": 100, "moneyness_pct": 0,
                 "market": "US", "source": "fixture", "quote_provider": "fixture",
                 "volume_session_date": "2026-10-19",
                 "bid_quote_at": (now-timedelta(seconds=10)).isoformat(),
                 "ask_quote_at": (now-timedelta(seconds=20)).isoformat()}]
    monkeypatch.setattr(analyzer, "_fetch_us_scanner", fetch)
    fire = {"pro_symbol": "NASDAQ:TEST", "fire_time": now.isoformat(),
            "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
            "_screener_class": "stocks_us", "_screener_data": {"type": "stock"}}
    stack, payload_path, database_path = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([fire], {}), market="US",
        session_date="2026-10-19", observed_at=now.isoformat(), scope=scope,
        fail_persistence=fail_persistence, options_enabled=True, options_analyzer=analyzer,
        freshness=lambda fires, **kwargs: ([{
            **f, "bar_time": (now-timedelta(minutes=15)).isoformat(),
            "bar_end_time": now.isoformat(), "bar_interval_minutes": 15,
            "quote_source": "Yahoo Finance 15m chart",
            "freshness_assurance": "independent_chart_crosscheck",
            "configured_provider_delay_minutes": 15,
        } for f in fires], {}),
    )
    payload = json.loads(payload_path.read_text())
    if scope == "test":
        assert calls == [] and payload["stocks"] == []
        assert not database_path.exists()
    else:
        snapshot, errors = read_snapshot(stack._OPTIONS_SNAPSHOT_PATH, expected_run_id=RUN_ID, now=now)
        assert not errors, errors
        if fail_persistence:
            assert calls == [] and snapshot["contracts"] == []
        else:
            assert calls == ["NASDAQ:TEST"]
            assert len(snapshot["contracts"]) == 1
            contract = snapshot["contracts"][0]
            assert contract["parent_signal_score"] == 88.5
            with sqlite3.connect(database_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM scanner_setups WHERE setup_id=?",
                                    (contract["parent_setup_id"],)).fetchone()[0] == 1
            assert contract["scope"] == "production"
            assert payload["stocks"][0]["source"] == "scanner"
    safety.assert_clean()


def test_actual_scanner_mode_writes_actionable_source_metadata(monkeypatch, safety):
    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "_screener_class": "stocks_sg", "_screener_data": {},
    }
    _stack, payload_path, _db_path = _run_actual_scanner_stack(
        monkeypatch, safety,
        adapter=lambda **kwargs: ([fire], {"classes_scanned": ["stocks_sg"]}),
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["stocks"] == [{
        "pro_symbol": "SGX:BS6", "exchange": "SGX", "symbol": "BS6",
        "score": 88.5, "signal": "SCR", "fire_time": fire["fire_time"],
        "resolution": "D", "actionable": True, "source": "scanner",
        "market": "SG", "session_date": "2026-10-15",
        "timestamp": "2026-10-15T02:00:00+00:00", "direction": "BUY",
        "bar_time": "2026-10-15T02:00:00+00:00",
        "bar_end_time": "2026-10-15T02:00:00+00:00",
        "bar_interval_minutes": 15,
        "quote_source": "Yahoo Finance 15m chart",
    }]
    safety.assert_clean()


def test_actual_scanner_mode_does_not_emit_engine_veto_as_actionable(monkeypatch, safety):
    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "_screener_class": "stocks_sg", "_screener_data": {},
    }
    _stack, payload_path, _db_path = _run_actual_scanner_stack(
        monkeypatch, safety,
        adapter=lambda **kwargs: ([fire], {"classes_scanned": ["stocks_sg"]}),
        actionability=lambda *args, **kwargs: ["earnings blackout"],
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["stocks"] == []
    assert payload["status"] == "no_new"
    safety.assert_clean()


def test_actual_scanner_entrypoint_keeps_observations_and_one_immutable_setup(
    monkeypatch, safety,
):
    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "_screener_class": "stocks_sg", "_screener_data": {},
    }
    db_path = safety.tmp_path / "setup-observations.sqlite"
    adapter = lambda **kwargs: ([fire], {"classes_scanned": ["stocks_sg"]})

    # A vetoed first observation must not reserve the daily setup identity.
    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, actionability=lambda *a, **k: ["veto"],
        database_path=db_path, run_id="scan_observation_1",
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scanner_setups").fetchone()[0] == 0

    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, database_path=db_path,
        run_id="scan_observation_2",
    )
    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, database_path=db_path,
        run_id="scan_observation_3",
    )
    with sqlite3.connect(db_path) as conn:
        setup_rows = conn.execute(
            "SELECT first_run_id, first_result_json FROM scanner_setups"
        ).fetchall()
        observations = conn.execute(
            "SELECT run_id, actionable FROM scanner_observations ORDER BY run_id"
        ).fetchall()
        recoverable = conn.execute("SELECT COUNT(*) FROM scanner_scoring_results").fetchone()[0]
    assert len(setup_rows) == 1
    assert setup_rows[0][0] == "scan_observation_2"
    assert json.loads(setup_rows[0][1])["run_id"] == "scan_observation_2"
    assert len(observations) == 3
    assert sum(actionable for _, actionable in observations) == 2
    assert recoverable == 1
    safety.assert_clean()


def test_actual_scanner_entrypoint_failed_score_retries_and_test_scope_is_separate(
    monkeypatch, safety,
):
    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "_screener_class": "stocks_sg", "_screener_data": {},
    }
    db_path = safety.tmp_path / "setup-recovery.sqlite"
    adapter = lambda **kwargs: ([fire], {"classes_scanned": ["stocks_sg"]})
    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, database_path=db_path,
        run_id="scan_failure", fail_scoring=True,
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scanner_setups").fetchone()[0] == 0
    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, database_path=db_path,
        run_id="scan_recovered",
    )
    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, database_path=db_path,
        run_id="scan_next_session", session_date="2026-10-16",
        observed_at="2026-10-16T02:00:00+00:00",
    )
    _test_stack, test_payload_path, _test_db_path = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, database_path=db_path,
        run_id="scan_test_scope", scope="test",
    )
    assert json.loads(test_payload_path.read_text(encoding="utf-8"))["stocks"] == []
    with sqlite3.connect(db_path) as conn:
        scopes = conn.execute("SELECT scope, COUNT(*) FROM scanner_setups GROUP BY scope").fetchall()
        production_payloads = conn.execute(
            "SELECT COUNT(*) FROM scanner_scoring_results WHERE scope='production'"
        ).fetchone()[0]
        test_payloads = conn.execute(
            "SELECT COUNT(*) FROM scanner_scoring_results WHERE scope='test'"
        ).fetchone()[0]
    assert scopes == [("production", 2)]
    assert (production_payloads, test_payloads) == (2, 0)
    safety.assert_clean()


def test_actual_scanner_tracker_is_one_setup_row_and_repairs_with_original_entry(
    monkeypatch, safety,
):
    from tradingview.alert_tracker import AlertTracker

    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "close": 77.0, "_screener_class": "stocks_sg", "_screener_data": {},
    }
    scanner_db = safety.tmp_path / "setup-tracker-scanner.sqlite"
    tracker = AlertTracker(db_path=safety.tmp_path / "setup-alert-tracker.sqlite")
    adapter = lambda **kwargs: ([fire], {"classes_scanned": ["stocks_sg"]})
    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=adapter, database_path=scanner_db,
        run_id="scan_tracker_first", tracker=tracker, fundamentals_price=None,
    )
    changed_fire = {**fire, "close": 99.0}
    _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([changed_fire], {}),
        database_path=scanner_db, run_id="scan_tracker_repeat", tracker=tracker,
        fundamentals_price=None, score_override=(99.0, 99.0),
    )
    with sqlite3.connect(safety.tmp_path / "setup-alert-tracker.sqlite") as conn:
        alert_rows = conn.execute(
            "SELECT COUNT(*), MIN(entry_price), MAX(entry_price) FROM alerts WHERE tv_symbol='SGX:BS6'"
        ).fetchone()
        output_rows = conn.execute(
            "SELECT COUNT(*), MIN(composite_score), MAX(composite_score), "
            "MIN(run_id), MAX(run_id) "
            "FROM signal_engine_outputs WHERE tv_symbol='SGX:BS6'"
        ).fetchone()
    assert alert_rows == (1, 77.0, 77.0)
    assert output_rows == (1, 88.5, 88.5, "scan_tracker_first", "scan_tracker_first")
    safety.assert_clean()


def test_actual_scanner_persistence_failure_blocks_actionable_handoff(
    monkeypatch, safety,
):
    from tradingview.alert_tracker import AlertTracker

    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "close": 77.0, "_screener_class": "stocks_sg", "_screener_data": {},
    }
    db_path = safety.tmp_path / "setup-persistence-failure.sqlite"
    tracker = AlertTracker(db_path=safety.tmp_path / "setup-persistence-failure-tracker.sqlite")
    _stack, payload_path, _ = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([fire], {}),
        database_path=db_path, run_id="scan_persistence_failure", tracker=tracker,
        fail_persistence=True,
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["stocks"] == []
    assert any("persistence failed" in error for error in payload["errors"])
    with sqlite3.connect(safety.tmp_path / "setup-persistence-failure-tracker.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM signal_engine_outputs").fetchone()[0] == 0
    safety.assert_clean()


def test_actual_scanner_invalid_scope_fails_before_persistence(monkeypatch, safety):
    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "_screener_class": "stocks_sg", "_screener_data": {},
    }
    _stack, payload_path, db_path = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([fire], {}), scope="unknown",
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["stocks"] == []
    assert not db_path.exists()
    safety.assert_clean()


def test_actual_scanner_fetch_error_does_not_publish_valid_empty(monkeypatch, safety):
    def failed_adapter(**kwargs):
        raise RuntimeError("TradingView request failed")

    import pytest
    with pytest.raises(RuntimeError, match="TradingView request failed"):
        _run_actual_scanner_stack(monkeypatch, safety, adapter=failed_adapter)
    assert not (safety.reports / "scanner_stock_results.json").exists()
    safety.assert_clean()


def test_actual_scanner_freshness_rejection_precedes_scoring_and_persistence(
    monkeypatch, safety,
):
    fire = {
        "pro_symbol": "SGX:BS6", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "_screener_class": "stocks_sg", "_screener_data": {},
    }
    _stack, payload_path, db_path = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([fire], {}),
        freshness=lambda fires, **kwargs: ([], {"SGX:BS6": "stale provider bar"}),
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["status"] == "no_new"
    assert payload["stocks"] == []
    assert payload["freshness_rejections"] == {"SGX:BS6": "stale provider bar"}
    assert not db_path.exists()
    safety.assert_clean()


def test_actual_au_scanner_skips_legacy_usd_market_cap_floor(monkeypatch, safety):
    fire = {
        "pro_symbol": "ASX:TEST", "fire_time": "2026-10-15T02:00:00+00:00",
        "resolution": "D", "message": "SCREENER BUY:1", "source": "screener",
        "_screener_class": "stocks_au", "_screener_data": {},
    }
    _stack, payload_path, _db_path = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([fire], {}), market="AU",
        market_cap=80_000_000, min_market_cap=100,
    )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["stocks"][0]["pro_symbol"] == "ASX:TEST"
    safety.assert_clean()


def test_insights_mode_is_write_free(monkeypatch, safety):
    fires_path = safety.tmp_path / "fires_raw.json"
    output_path = safety.reports / f"signal_stack_report_{RUN_ID}.html"
    actionable_path = safety.reports / f"actionable_report_{RUN_ID}.html"
    fires_path.write_text(json.dumps({
        "all": [{
            "pro_symbol": "NASDAQ:TEST",
            "symbol": "NASDAQ:TEST",
            "fire_time": "2020-01-01T00:00:00Z",
            "resolution": "1h",
            "message": "",
        }]
    }), encoding="utf-8")

    signal_stack = _configure_signal_stack(
        monkeypatch, safety, fires_path, output_path, actionable_path
    )
    import tradingview.outcome_store as outcome_store
    ledger_module = sys.modules["database.fires_ledger"]
    fail_write = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("insights attempted a persistence call")
    )
    monkeypatch.setattr(outcome_store, "persist_completed", fail_write)
    monkeypatch.setattr(outcome_store, "persist_run", fail_write)
    monkeypatch.setattr(ledger_module, "persist_run", fail_write)
    monkeypatch.setattr(ledger_module, "mark_scored", fail_write)
    monkeypatch.setattr(signal_stack, "_get_tracker", fail_write)
    monkeypatch.setattr(sys, "argv", [
        "signal_stack.py",
        "--insights",
        "--n", "1",
        "--threshold", "62.5",
        "--no-moomoo",
        "--no-india",
        "--no-external",
        "--no-options",
        "--no-book-context",
        "--min-market-cap", "0",
    ])
    signal_stack.main()

    assert list(safety.reports.iterdir()) == []


@pytest.mark.parametrize('provider_failure', [False, True])
def test_actual_scanner_retains_valid_stock_when_another_candidate_rejected(monkeypatch, safety, provider_failure):
    from tradingview.scheduler.freshness import FreshnessRejections
    fire = {'pro_symbol': 'SGX:BS6', 'fire_time': '2026-10-15T02:00:00+00:00',
            'resolution': 'D', 'message': 'SCREENER BUY:1', 'source': 'screener',
            '_screener_class': 'stocks_sg', '_screener_data': {}}
    rejected = FreshnessRejections()
    rejected['BAD'] = 'provider unavailable' if provider_failure else 'stale provider bar'
    if provider_failure:
        rejected.provider_errors['BAD'] = rejected['BAD']
    def check(fires, **kwargs):
        row = dict(fires[0])
        row.update(bar_time='2026-10-15T01:45:00+00:00', bar_end_time='2026-10-15T02:00:00+00:00',
                   bar_interval_minutes=15, quote_source='Yahoo Finance 15m chart',
                   freshness_assurance='independent_chart_crosscheck', configured_provider_delay_minutes=15)
        return [row], rejected
    _, path, _ = _run_actual_scanner_stack(
        monkeypatch, safety, adapter=lambda **kwargs: ([fire, {**fire, 'pro_symbol': 'SGX:BAD'}], {}),
        freshness=check)
    payload = json.loads(path.read_text())
    assert payload['status'] == ('partial' if provider_failure else 'success')
    assert [r['pro_symbol'] for r in payload['stocks']] == ['SGX:BS6']
    assert payload['freshness_rejections'] == dict(rejected)
    from tradingview.scheduler.stock_results import load_current_run_stocks
    loaded = load_current_run_stocks(path, payload['run_id'])
    assert loaded.status == payload['status']
    assert len(loaded.stocks) == 1
    safety.assert_clean()
