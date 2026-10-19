import json
import sqlite3
import sys
from datetime import datetime, timezone

from tradingview.outcome_reconcile import main, reconcile, write_outputs


def _databases(tmp_path):
    tracker_db = tmp_path / "tracker.db"
    fires_db = tmp_path / "fires.db"
    with sqlite3.connect(tracker_db) as conn:
        conn.executescript("""
            CREATE TABLE alerts (alert_uuid TEXT, tv_symbol TEXT, fire_time TEXT, resolution TEXT);
            CREATE TABLE outcome_entries (alert_uuid TEXT, measurement_json TEXT, validation_error TEXT);
            CREATE TABLE outcome_points (alert_uuid TEXT);
            CREATE TABLE outcome_resolution_map (alert_uuid TEXT, pro_symbol TEXT, fire_time TEXT, resolution TEXT, source TEXT);
        """)
        conn.execute("INSERT INTO alerts VALUES ('a','NASDAQ:A','2026-01-01T00:00:00+00:00','60')")
        conn.execute("INSERT INTO outcome_entries VALUES ('a','{}',NULL)")
        conn.execute("INSERT INTO outcome_resolution_map VALUES ('a','NASDAQ:A','2026-01-01T00:00:00+00:00','60','download')")
    with sqlite3.connect(fires_db) as conn:
        conn.executescript("""
            CREATE TABLE downloaded_fires (pro_symbol TEXT, fire_time TEXT, resolution TEXT, scored_at TEXT);
            CREATE TABLE fire_attempt_evidence (pro_symbol TEXT, fire_time TEXT, resolution TEXT, state TEXT, completion_ref TEXT);
        """)
        conn.execute("INSERT INTO downloaded_fires VALUES ('NASDAQ:A','2026-01-01T00:00:00+00:00','60','legacy')")
        conn.execute("INSERT INTO fire_attempt_evidence VALUES ('NASDAQ:A','2026-01-01T00:00:00+00:00','60','completed','run:a')")
    return tracker_db, fires_db


def test_reconcile_is_read_only_and_separates_completion(tmp_path):
    tracker_db, fires_db = _databases(tmp_path)
    before_tracker = tracker_db.read_bytes()
    before_fires = fires_db.read_bytes()
    result = reconcile(
        str(tracker_db),
        str(fires_db),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert result["metadata"]["read_only"] is True
    assert result["counts"]["confirmed_completion"] == 1
    assert result["counts"]["legacy_scored_at_without_evidence"] == 0
    assert result["counts"]["eligible_needing_observations"] == 1
    assert tracker_db.read_bytes() == before_tracker
    assert fires_db.read_bytes() == before_fires


def test_reconcile_outputs_json_and_csv(tmp_path):
    tracker_db, fires_db = _databases(tmp_path)
    result = reconcile(str(tracker_db), str(fires_db))
    json_out = tmp_path / "reconcile.json"
    csv_out = tmp_path / "reconcile.csv"
    write_outputs(result, str(json_out), str(csv_out))
    payload = json.loads(json_out.read_text())
    assert payload["metadata"]["read_only"] is True
    assert "category,record" in csv_out.read_text()


def test_reconcile_cli_is_read_only_and_writes_all_outputs(tmp_path, monkeypatch):
    tracker_db, fires_db = _databases(tmp_path)
    tracker_before = tracker_db.read_bytes()
    fires_before = fires_db.read_bytes()
    json_out = tmp_path / "cli-reconcile.json"
    csv_out = tmp_path / "cli-reconcile.csv"
    monkeypatch.setattr(sys, "argv", [
        "outcome_reconcile.py",
        "--tracker-db", str(tracker_db),
        "--fires-db", str(fires_db),
        "--json-out", str(json_out),
        "--csv-out", str(csv_out),
        "--as-of", "2026-01-02T00:00:00+00:00",
    ])
    assert main() == 0
    assert tracker_db.read_bytes() == tracker_before
    assert fires_db.read_bytes() == fires_before
    assert json.loads(json_out.read_text())["metadata"]["read_only"] is True
    assert "category,record" in csv_out.read_text()


def test_reconcile_separates_retry_dedup_and_legacy_states(tmp_path):
    tracker_db, fires_db = _databases(tmp_path)
    with sqlite3.connect(fires_db) as conn:
        for index in range(3):
            conn.execute(
                "INSERT INTO fire_attempt_evidence VALUES (?,?,?,?,?)",
                ("NASDAQ:B", "2026-01-01T00:00:00+00:00", "60", "filtered", f"run:{index}"),
            )
        conn.execute(
            "INSERT INTO fire_attempt_evidence VALUES (?,?,?,?,?)",
            ("NASDAQ:C", "2026-01-01T00:00:00+00:00", "60", "deduplicated", "run:c"),
        )
    result = reconcile(str(tracker_db), str(fires_db))
    assert result["counts"]["confirmed_completion"] == 1
    assert result["counts"]["retry_exhausted"] == 1
    assert result["counts"]["deduplicated"] == 1
    assert result["counts"]["legacy_scored_at_without_evidence"] == 0