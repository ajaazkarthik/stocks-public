"""Offline tests for the produce-once cycle pipeline.

Covers Issues 1, 2, 3, 7, and 10. Every subprocess is replaced by an injected
runner, every path is a temporary directory, and no network call is possible.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingview.scheduler import stock_results
from tradingview.scheduler.config import SchedulerConfig
from tradingview.scheduler.cycle import (
    CYCLE_FAILED,
    CYCLE_NO_NEW,
    CYCLE_OK,
    CyclePaths,
    CycleResult,
    build_summary_parts,
    produce_cycle,
)
from tradingview.scheduler.steps import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TIMEOUT,
    StepResult,
)

RUN_ID = "ss_20261013T064219Z_test0001"
FROZEN_NOW = datetime(2026, 10, 13, 6, 42, 19, tzinfo=timezone.utc)


def _config(**overrides) -> SchedulerConfig:
    base = dict(
        stock_threshold=62.5,
        option_threshold=70.0,
        step_timeout=30,
        display_timezone="Australia/Sydney",
        schedule_timezone="Asia/Kolkata",
    )
    base.update(overrides)
    return SchedulerConfig(**base)


class RecordingRunner:
    """Injected subprocess runner that records invocations."""

    def __init__(self, outcomes: dict[str, StepResult] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, label, cmd, cwd=None, env=None, timeout=900, **kwargs):
        self.calls.append((label, tuple(cmd)))
        outcome = self.outcomes.get(label)
        if outcome is not None:
            return outcome
        return StepResult(
            label=label, status=STATUS_OK, exit_code=0, duration_seconds=0.01,
            output="ok", command=tuple(cmd),
        )

    @property
    def labels(self) -> list[str]:
        return [label for label, _ in self.calls]


def _step(label: str, status: str, exit_code: int | None = 0) -> StepResult:
    return StepResult(
        label=label, status=status, exit_code=exit_code, duration_seconds=0.01,
        output="", error="" if status == STATUS_OK else f"{label} {status}",
    )


def _write_stock_payload(paths: CyclePaths, stocks: list[dict], status: str = "success") -> None:
    paths.ensure()
    stock_results.write_current_run_payload(
        paths.stock_payload,
        stock_results.build_current_run_payload(RUN_ID, stocks, status=status),
    )


def _write_options_snapshot(paths: CyclePaths, contracts: list[dict], status: str = "success") -> None:
    from tradingview.options.options_snapshot import build_snapshot, publish_snapshot

    paths.ensure()
    publish_snapshot(paths.options_snapshot, build_snapshot(RUN_ID, status, contracts))


def _contract(score: float = 79.0, symbol: str = "MSFT") -> dict:
    return {
        "market": "US", "exchange": "NASDAQ", "symbol": symbol,
        "option_type": "CALL", "strike": 530.0, "expiry": "2030-06-19",
        "dte": 45, "option_score": score, "parent_signal_score": 91.0,
        "strategy_modes": ["BALANCED"],
    }


def _stock(pro_symbol: str = "NASDAQ:GOOG", score: float = 71.0,
           fire_time: str = "2026-10-13T05:00:00Z") -> dict:
    exchange, _, symbol = pro_symbol.partition(":")
    return {
        "pro_symbol": pro_symbol, "exchange": exchange, "symbol": symbol,
        "score": score, "signal": "BUY", "fire_time": fire_time, "resolution": "60",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Issue 3 — dependency-aware failure handling
# ══════════════════════════════════════════════════════════════════════════════

def test_download_failure_stops_scoring_and_options(tmp_path):
    """A failed download must not let the preserved cache be scored as fresh."""
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner({"download_fires": _step("download_fires", STATUS_FAILED, 1)})

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.status == CYCLE_FAILED
    assert runner.labels == ["download_fires"], "dependent steps ran after a failed download"
    assert result.stock_status == stock_results.STATUS_UNAVAILABLE
    assert result.options_status == "unavailable"
    assert result.stocks == []
    assert result.options == []


def test_download_timeout_stops_dependent_work(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner({"download_fires": _step("download_fires", STATUS_TIMEOUT, None)})

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.status == CYCLE_FAILED
    assert "signal_stack" not in runner.labels


def test_settlement_failure_allows_scoring_with_warning(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner({"settle_outcomes": _step("settle_outcomes", STATUS_FAILED, 2)})
    _write_stock_payload(paths, [_stock()])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert "signal_stack" in runner.labels
    assert any("settlement" in warning for warning in result.warnings)
    assert result.status in {CYCLE_OK, "partial"}


def test_scorer_failure_publishes_no_stale_options(tmp_path):
    """A failed scorer must not present an old snapshot as the current result."""
    paths = CyclePaths(tmp_path, RUN_ID)
    _write_options_snapshot(paths, [_contract()])
    _write_stock_payload(paths, [_stock()])
    runner = RecordingRunner({"signal_stack": _step("signal_stack", STATUS_FAILED, 1)})

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.status == CYCLE_FAILED
    assert result.options == []
    assert result.stocks == []
    assert result.options_status == "unavailable"


def test_genuine_empty_result_is_distinct_from_unavailable(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()
    _write_stock_payload(paths, [], status="no_new")
    _write_options_snapshot(paths, [], status="no_new_signals")

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.status == CYCLE_NO_NEW
    assert result.stock_status == stock_results.STATUS_NO_NEW
    assert result.stock_status not in stock_results.UNAVAILABLE_STATUSES


def test_launch_failure_is_persisted_as_a_cycle_failure(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner({
        "download_fires": _step("download_fires", "launch_error", None),
    })

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.status == CYCLE_FAILED
    assert paths.cycle_result.exists()
    persisted = json.loads(paths.cycle_result.read_text())
    assert persisted["status"] == CYCLE_FAILED
    assert persisted["steps"][0]["status"] == "launch_error"


def test_no_step_runs_after_a_dependency_failure(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner({"download_fires": _step("download_fires", STATUS_FAILED, 1)})

    produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert runner.labels == ["download_fires"]


# ══════════════════════════════════════════════════════════════════════════════
# Issue 7 — unavailable vs empty
# ══════════════════════════════════════════════════════════════════════════════

def test_missing_stock_payload_is_unavailable_not_empty(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.stock_status == stock_results.STATUS_UNAVAILABLE
    assert result.stock_status not in stock_results.EMPTY_STATUSES


def test_mismatched_run_id_payload_is_unavailable(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    paths.ensure()
    stock_results.write_current_run_payload(
        paths.stock_payload,
        stock_results.build_current_run_payload("some-other-run", [_stock()]),
    )
    runner = RecordingRunner()

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.stock_status == stock_results.STATUS_UNAVAILABLE
    assert result.stocks == []


def test_malformed_payload_is_unavailable(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    paths.ensure()
    paths.stock_payload.write_text("{not json", encoding="utf-8")
    runner = RecordingRunner()

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.stock_status == stock_results.STATUS_UNAVAILABLE


def test_stock_unavailable_while_options_succeed(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()
    _write_options_snapshot(paths, [_contract()])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.stock_status == stock_results.STATUS_UNAVAILABLE
    assert result.options_status == "success"
    assert len(result.options) == 1


def test_options_unavailable_while_stocks_succeed(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()
    _write_stock_payload(paths, [_stock()])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.stock_status == stock_results.STATUS_SUCCESS
    assert result.options_status == "unavailable"


def test_missing_database_is_not_created_by_reading(tmp_path):
    db_path = tmp_path / "does-not-exist.db"
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert result.status == stock_results.STATUS_UNAVAILABLE
    assert not db_path.exists(), "reading a missing database created it"


def test_missing_table_is_reported_as_failure(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert result.status == stock_results.STATUS_FAILED
    assert any("alerts" in error for error in result.errors)


def test_malformed_database_is_reported_as_failure(tmp_path):
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is definitely not a sqlite database" * 10)
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert result.status == stock_results.STATUS_FAILED


def test_valid_empty_database_is_a_genuine_empty_result(tmp_path):
    db_path = tmp_path / "alerts.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE alerts (tv_symbol TEXT, composite_score REAL, signal TEXT, "
        "fire_time TEXT, resolution TEXT, created_at TEXT, actionable INTEGER)"
    )
    conn.commit()
    conn.close()

    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert result.status == stock_results.STATUS_EMPTY
    assert result.available is True


# ══════════════════════════════════════════════════════════════════════════════
# Issue 2 — 24-hour SQL boundary
# ══════════════════════════════════════════════════════════════════════════════

def _alerts_db(tmp_path: Path, rows: list[tuple]) -> Path:
    db_path = tmp_path / "alerts.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE alerts (tv_symbol TEXT, composite_score REAL, signal TEXT, "
        "fire_time TEXT, resolution TEXT, created_at TEXT, actionable INTEGER)"
    )
    conn.executemany(
        "INSERT INTO alerts VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


def test_stale_row_outside_window_is_excluded(tmp_path):
    """The observed defect: a ~29h44m old row was still included."""
    db_path = _alerts_db(tmp_path, [
        ("ASX:CDA", 80.0, "BUY", "2026-10-12T00:58:00Z", "60",
         "2026-10-12T00:58:00.786494+00:00", 1),
        ("NASDAQ:GOOG", 75.0, "BUY", "2026-10-13T05:00:00Z", "60",
         "2026-10-13T05:00:00+00:00", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    symbols = {stock["symbol"] for stock in result.stocks}
    assert "CDA" not in symbols, "stale row was included"
    assert "GOOG" in symbols


def test_exact_lower_boundary_is_excluded_and_one_second_later_included(tmp_path):
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:EXACT", 70.0, "BUY", "t", "60", "2026-10-12T06:42:19Z", 1),
        ("NASDAQ:AFTER", 70.0, "BUY", "t", "60", "2026-10-12T06:42:20Z", 1),
        ("NASDAQ:BEFORE", 70.0, "BUY", "t", "60", "2026-10-12T06:42:18Z", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    symbols = {stock["symbol"] for stock in result.stocks}
    assert symbols == {"AFTER"}


def test_equivalent_instants_across_formats_are_consistent(tmp_path):
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:Z", 70.0, "BUY", "t", "60", "2026-10-13T06:00:00Z", 1),
        ("NASDAQ:OFFSET", 70.0, "BUY", "t", "60", "2026-10-13T06:00:00+00:00", 1),
        ("NASDAQ:OTHER", 70.0, "BUY", "t", "60", "2026-10-13T17:00:00+11:00", 1),
        ("NASDAQ:FRACTION", 70.0, "BUY", "t", "60", "2026-10-13T06:00:00.500000+00:00", 1),
        ("NASDAQ:LEGACY", 70.0, "BUY", "t", "60", "2026-10-13 06:00:00", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    symbols = {stock["symbol"] for stock in result.stocks}
    assert symbols == {"Z", "OFFSET", "OTHER", "FRACTION", "LEGACY"}


def test_future_records_are_excluded(tmp_path):
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:FUTURE", 90.0, "BUY", "t", "60", "2026-10-14T00:00:00Z", 1),
        ("NASDAQ:NOW", 70.0, "BUY", "t", "60", "2026-10-13T06:00:00Z", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    symbols = {stock["symbol"] for stock in result.stocks}
    assert symbols == {"NOW"}


def test_invalid_timestamps_are_reported_not_silently_fresh(tmp_path):
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:BAD", 99.0, "BUY", "t", "60", "not-a-timestamp", 1),
        ("NASDAQ:GOOD", 70.0, "BUY", "t", "60", "2026-10-13T06:00:00Z", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    symbols = {stock["symbol"] for stock in result.stocks}
    assert symbols == {"GOOD"}
    assert result.malformed_timestamps == 1
    assert any("unparseable" in error for error in result.errors)


def test_latest_assessment_wins_over_old_high_score(tmp_path):
    """An old high-scoring eligible row must not survive a newer ineligible one."""
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:GOOG", 95.0, "BUY", "t1", "60", "2026-10-13T01:00:00Z", 1),
        ("NASDAQ:GOOG", 40.0, "BUY", "t2", "60", "2026-10-13T05:00:00Z", 0),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert result.stocks == [], "an old maximum-score row was resurrected"


def test_latest_eligible_assessment_is_returned(tmp_path):
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:GOOG", 95.0, "BUY", "t1", "60", "2026-10-13T01:00:00Z", 1),
        ("NASDAQ:GOOG", 66.0, "BUY", "t2", "60", "2026-10-13T05:00:00Z", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert len(result.stocks) == 1
    assert result.stocks[0]["score"] == 66.0


def test_twenty_historical_high_scores_do_not_crowd_out_a_new_lower_fire(tmp_path):
    rows = [
        (f"NASDAQ:HIST{i}", 99.0 - i, "BUY", "t", "60", "2026-10-13T01:00:00Z", 1)
        for i in range(20)
    ]
    rows.append(("NASDAQ:NEW", 65.0, "BUY", "t", "60", "2026-10-13T06:00:00Z", 1))
    db_path = _alerts_db(tmp_path, rows)

    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW, limit=20)
    symbols = {stock["symbol"] for stock in result.stocks}
    assert "NEW" in symbols, "the new lower-scoring fire was crowded out"
    assert result.total_eligible == 21
    assert result.remainder == 1, "the remainder was not reported"


def test_ordering_is_most_recent_first(tmp_path):
    """Recency-first ordering is what stops new fires being crowded out."""
    db_path = _alerts_db(tmp_path, [
        ("NASDAQ:OLD", 90.0, "BUY", "t", "60", "2026-10-13T01:00:00Z", 1),
        ("NASDAQ:NEW", 65.0, "BUY", "t", "60", "2026-10-13T05:00:00Z", 1),
    ])
    result = stock_results.query_latest_assessment_stocks(db_path, now=FROZEN_NOW)
    assert [stock["symbol"] for stock in result.stocks] == ["NEW", "OLD"]


# ══════════════════════════════════════════════════════════════════════════════
# Issue 1 / 10 — current-run consumption and produce-once
# ══════════════════════════════════════════════════════════════════════════════

def test_cycle_runs_each_step_exactly_once(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()
    _write_stock_payload(paths, [_stock()])

    produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert runner.labels.count("download_fires") == 1
    assert runner.labels.count("settle_outcomes") == 1
    assert runner.labels.count("signal_stack") == 1


def test_cycle_consumes_only_current_run_stocks(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()
    _write_stock_payload(paths, [_stock("NASDAQ:GOOG", 71.0)])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert [stock["symbol"] for stock in result.stocks] == ["GOOG"]
    assert result.stock_result.source == "current_run"


def test_engine_actionability_is_preserved_not_recomputed(tmp_path):
    """A score below the option threshold is still delivered when actionable."""
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()
    _write_stock_payload(paths, [_stock("NASDAQ:LOW", 65.0)])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert len(result.stocks) == 1
    assert result.stocks[0]["score"] == 65.0


def test_run_scoped_paths_are_isolated_per_run(tmp_path):
    first = CyclePaths(tmp_path, "run-a")
    second = CyclePaths(tmp_path, "run-b")
    assert first.stock_payload != second.stock_payload
    assert first.options_snapshot != second.options_snapshot
    assert first.run_dir != second.run_dir


def test_cycle_result_is_persisted_and_reloadable(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    runner = RecordingRunner()
    _write_stock_payload(paths, [_stock()])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    persisted = json.loads(paths.cycle_result.read_text())
    assert persisted["run_id"] == RUN_ID
    assert persisted["schema_version"] == result.schema_version
    assert persisted["stock_result"]["status"] == stock_results.STATUS_SUCCESS


def test_summary_reports_unavailable_stock_data_explicitly(tmp_path):
    result = CycleResult(
        run_id=RUN_ID,
        started_at=FROZEN_NOW.isoformat(),
        display_timezone="Australia/Sydney",
        stock_result=stock_results.StockResult(
            status=stock_results.STATUS_UNAVAILABLE,
            errors=["stock database is missing"],
        ),
        option_result={"options": [], "status": "empty", "errors": []},
    )
    parts = build_summary_parts(result, limit=3500, analysis_time=FROZEN_NOW)
    text = "\n".join(part.plain_text for part in parts)
    assert "Scan error" in text
    assert "stock database is missing" in text
    assert "No new actionable stock signals" not in text


def test_summary_reports_genuine_no_new_cycle(tmp_path):
    result = CycleResult(
        run_id=RUN_ID,
        started_at=FROZEN_NOW.isoformat(),
        display_timezone="Australia/Sydney",
        stock_result=stock_results.StockResult(status=stock_results.STATUS_NO_NEW),
        option_result={"options": [], "status": "no_new_signals", "errors": []},
    )
    parts = build_summary_parts(result, limit=3500, analysis_time=FROZEN_NOW)
    text = "\n".join(part.plain_text for part in parts)
    assert parts == []


def test_summary_shows_sydney_time_not_ist(tmp_path):
    result = CycleResult(
        run_id=RUN_ID,
        started_at=FROZEN_NOW.isoformat(),
        display_timezone="Australia/Sydney",
        stock_result=stock_results.StockResult(status=stock_results.STATUS_NO_NEW),
        option_result={"options": [], "status": "failed", "errors": ["fetch failed"]},
    )
    parts = build_summary_parts(result, limit=3500, analysis_time=FROZEN_NOW)
    text = "\n".join(part.plain_text for part in parts)
    assert "17:42" in text
    assert "AEDT" in text
    assert "IST" not in text
