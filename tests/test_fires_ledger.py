"""Tests for database/fires_ledger.py — the durable downloaded-fires record."""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import fires_ledger as fl  # noqa: E402


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the ledger at a throwaway database."""
    db = tmp_path / "fires_ledger.db"

    def _conn():
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(fl._SCHEMA)
        return conn

    monkeypatch.setattr(fl, "_conn", _conn)
    monkeypatch.setattr(fl, "_HERE", tmp_path)   # no alert_tracker.db here
    return fl


def _fire(symbol="NASDAQ:AAPL", fire_time="2026-08-18T05:20:02Z", resolution="60", **kw):
    base = {
        "fire_id": 55340298060,
        "alert_id": 2381963449,
        "symbol": symbol.split(":")[-1],
        "pro_symbol": symbol,
        "resolution": resolution,
        "fire_time": fire_time,
        "name": "WIN 2H",
    }
    base.update(kw)
    return base


class TestRecordFires:

    def test_records_and_counts_new_fires(self, ledger):
        assert ledger.record_fires([_fire(), _fire("NYSE:GE")]) == 2
        assert ledger.count() == 2

    def test_reinserting_the_same_fires_adds_nothing(self, ledger):
        fires = [_fire(), _fire("NYSE:GE")]
        ledger.record_fires(fires)
        assert ledger.record_fires(fires) == 0
        assert ledger.count() == 2

    def test_same_symbol_at_a_different_time_is_a_different_fire(self, ledger):
        ledger.record_fires([_fire()])
        assert ledger.record_fires([_fire(fire_time="2026-08-19T05:20:02Z")]) == 1

    def test_same_symbol_and_time_on_another_timeframe_is_distinct(self, ledger):
        ledger.record_fires([_fire(resolution="60")])
        assert ledger.record_fires([_fire(resolution="120")]) == 1

    def test_fires_without_an_identity_are_skipped(self, ledger):
        assert ledger.record_fires([{"symbol": "", "fire_time": ""}]) == 0
        assert ledger.record_fires([_fire(fire_time="")]) == 0

    def test_empty_input_is_a_no_op(self, ledger):
        assert ledger.record_fires([]) == 0

    def test_symbol_is_used_when_pro_symbol_is_absent(self, ledger):
        fire = _fire()
        del fire["pro_symbol"]
        assert ledger.record_fires([fire]) == 1


class TestNewOnlySelection:

    def test_reset_transient_attempts_preserves_completed_evidence(self, ledger):
        fires = [_fire(), _fire("NYSE:GE")]
        ledger.record_fires(fires)
        with ledger._conn() as conn:
            from tradingview.outcome_store import ensure_schema
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO fire_attempt_evidence "
                "(attempt_id,pro_symbol,fire_time,resolution,state,considered_at) "
                "VALUES ('failed','NASDAQ:AAPL',?,?, 'failed', 'now')",
                (fires[0]["fire_time"], fires[0]["resolution"]),
            )
            conn.execute(
                "INSERT INTO fire_attempt_evidence "
                "(attempt_id,pro_symbol,fire_time,resolution,state,considered_at) "
                "VALUES ('done','NYSE:GE',?,?, 'completed', 'now')",
                (fires[1]["fire_time"], fires[1]["resolution"]),
            )
        assert ledger.reset_transient_attempts(reason="retry_after_data_fix", reset_by="test") == 1
        assert [fire["pro_symbol"] for fire in ledger.unseen_fires(fires)] == [
            fires[0]["pro_symbol"]
        ]
        with ledger._conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM fire_attempt_evidence").fetchone()[0] == 2
            reset = conn.execute(
                "SELECT reason, reset_by FROM fire_attempt_resets"
            ).fetchone()
        assert tuple(reset) == ("retry_after_data_fix", "test")

    def test_dedup_is_not_suppressed_by_retry_exhaustion(self, ledger):
        replacement = _fire("NASDAQ:MSFT")
        duplicate = _fire("NASDAQ:AAPL")
        ledger.record_fires([replacement, duplicate])
        with ledger._conn() as conn:
            from tradingview.outcome_store import ensure_schema
            ensure_schema(conn)
            for index in range(fl.MAX_FAILED_ATTEMPTS):
                conn.execute(
                    "INSERT INTO fire_attempt_evidence "
                    "(attempt_id,pro_symbol,fire_time,resolution,state,considered_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"failed-{index}", replacement["pro_symbol"], replacement["fire_time"], replacement["resolution"], "failed", f"2026-01-01T00:00:0{index}+00:00"),
                )
            conn.execute(
                "INSERT INTO fire_attempt_evidence "
                "(attempt_id,pro_symbol,fire_time,resolution,state,deduplicated_against,considered_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("dedup", duplicate["pro_symbol"], duplicate["fire_time"], duplicate["resolution"], "deduplicated", '{"pro_symbol":"NASDAQ:MSFT","fire_time":"2026-08-18T05:20:02Z","resolution":"60"}', "2026-01-02T00:00:00+00:00"),
            )
        assert [fire["pro_symbol"] for fire in ledger.unseen_fires([replacement, duplicate])] == [
            duplicate["pro_symbol"]
        ]

    def test_dedup_chain_is_order_independent_and_completion_only(self, ledger):
        first = _fire("NASDAQ:AAPL")
        second = _fire("NASDAQ:MSFT")
        third = _fire("NYSE:IBM")
        ledger.record_fires([first, second, third])
        with ledger._conn() as conn:
            from tradingview.outcome_store import ensure_schema
            ensure_schema(conn)
            conn.executemany(
                "INSERT INTO fire_attempt_evidence "
                "(attempt_id,pro_symbol,fire_time,resolution,state,deduplicated_against,considered_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    ("first", first["pro_symbol"], first["fire_time"], first["resolution"], "deduplicated", '{"pro_symbol":"NASDAQ:MSFT","fire_time":"2026-08-18T05:20:02Z","resolution":"60"}', "2026-01-02T00:00:00+00:00"),
                    ("second", second["pro_symbol"], second["fire_time"], second["resolution"], "deduplicated", '{"pro_symbol":"NYSE:IBM","fire_time":"2026-08-18T05:20:02Z","resolution":"60"}', "2026-01-02T00:00:01+00:00"),
                    ("third", third["pro_symbol"], third["fire_time"], third["resolution"], "completed", None, "2026-01-02T00:00:02+00:00"),
                ],
            )
        assert ledger.unseen_fires([third, first, second]) == []

    def test_legacy_scored_at_does_not_complete_a_dedup_replacement(self, ledger):
        replacement = _fire("NASDAQ:MSFT")
        duplicate = _fire("NASDAQ:AAPL")
        ledger.record_fires([replacement, duplicate])
        assert ledger.mark_scored([replacement]) == 1
        with ledger._conn() as conn:
            from tradingview.outcome_store import ensure_schema
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO fire_attempt_evidence "
                "(attempt_id,pro_symbol,fire_time,resolution,state,deduplicated_against,considered_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("dedup", duplicate["pro_symbol"], duplicate["fire_time"], duplicate["resolution"], "deduplicated", '{"pro_symbol":"NASDAQ:MSFT","fire_time":"2026-08-18T05:20:02Z","resolution":"60"}', "2026-01-02T00:00:00+00:00"),
            )
        assert [fire["pro_symbol"] for fire in ledger.unseen_fires([replacement, duplicate])] == [
            duplicate["pro_symbol"]
        ]

    def test_everything_is_new_before_anything_is_scored(self, ledger):
        fires = [_fire(), _fire("NYSE:GE")]
        ledger.record_fires(fires)
        assert len(ledger.unseen_fires(fires)) == 2

    def test_recording_alone_does_not_make_a_fire_old(self, ledger):
        """The downloader runs before the scorer; presence must not mean 'seen'."""
        fires = [_fire()]
        ledger.record_fires(fires)
        ledger.record_fires(fires)
        assert len(ledger.unseen_fires(fires)) == 1

    def test_scored_fires_drop_out(self, ledger):
        fires = [_fire(), _fire("NYSE:GE")]
        ledger.record_fires(fires)
        assert ledger.mark_scored([fires[0]]) == 1
        remaining = ledger.unseen_fires(fires)
        assert [f["pro_symbol"] for f in remaining] == ["NYSE:GE"]

    def test_marking_twice_does_not_double_count(self, ledger):
        fires = [_fire()]
        ledger.record_fires(fires)
        assert ledger.mark_scored(fires) == 1
        assert ledger.mark_scored(fires) == 0

    def test_duplicates_within_one_batch_collapse(self, ledger):
        fires = [_fire(), _fire(), _fire("NYSE:GE")]
        ledger.record_fires(fires)
        assert len(ledger.unseen_fires(fires)) == 2

    def test_order_is_preserved(self, ledger):
        fires = [_fire("NYSE:GE"), _fire("NASDAQ:AAPL"), _fire("ASX:BHP")]
        ledger.record_fires(fires)
        assert [f["pro_symbol"] for f in ledger.unseen_fires(fires)] == [
            "NYSE:GE", "NASDAQ:AAPL", "ASX:BHP",
        ]

    def test_rejected_fires_stay_marked_so_they_stop_reappearing(self, ledger):
        """A fire filtered out mid-pipeline never reaches alert_tracker, but it
        was still considered — re-offering it every run is the noise this kills."""
        fires = [_fire("OTC:PENNY")]
        ledger.record_fires(fires)
        ledger.mark_scored(fires)
        assert ledger.unseen_fires(fires) == []


class TestAlertTrackerSeeding:

    def _tracker(self, tmp_path, rows):
        db = tmp_path / "alert_tracker.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE alerts (tv_symbol TEXT, fire_time TEXT)"
            )
            conn.executemany("INSERT INTO alerts VALUES (?,?)", rows)
        return db

    def test_tracker_rows_do_not_fabricate_scoring_evidence(self, ledger, tmp_path):
        self._tracker(tmp_path, [("NASDAQ:AAPL", "2026-08-18T05:20:02Z")])
        fires = [_fire(), _fire("NYSE:GE")]
        ledger.record_fires(fires)
        assert [f["pro_symbol"] for f in ledger.unseen_fires(fires)] == [
            "NASDAQ:AAPL", "NYSE:GE"
        ]

    def test_seeding_is_idempotent(self, ledger, tmp_path):
        self._tracker(tmp_path, [("NASDAQ:AAPL", "2026-08-18T05:20:02Z")])
        fires = [_fire()]
        ledger.record_fires(fires)
        assert ledger.sync_scored_from_alert_tracker() == 0

    def test_missing_tracker_database_is_tolerated(self, ledger):
        fires = [_fire()]
        ledger.record_fires(fires)
        assert ledger.sync_scored_from_alert_tracker() == 0
        assert len(ledger.unseen_fires(fires)) == 1
