"""Read-only reconciliation for tracker outcomes and downloaded fires."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from database.fires_ledger import MAX_FAILED_ATTEMPTS, MAX_FILTERED_ATTEMPTS

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _open_read_only(path: str) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{quote(str(resolved))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def reconcile(tracker_db: str, fires_db: str, as_of: datetime | None = None) -> dict:
    """Return reconciliation findings without writing either input database."""
    as_of = as_of or datetime.now(timezone.utc)
    tracker = _open_read_only(tracker_db)
    fires = _open_read_only(fires_db)
    try:
        findings = {key: [] for key in (
            "confirmed_completion", "legacy_scored_at_without_evidence",
            "retry_exhausted", "deduplicated",
            "tracker_fire_identity_mismatch", "missing_entry_provenance",
            "snapshots_without_trusted_timestamps", "eligible_needing_observations",
            "conflicting_or_invalid_records",
        )}
        alerts = tracker.execute(
            "SELECT alert_uuid, tv_symbol, fire_time, resolution FROM alerts"
        ).fetchall() if _has_table(tracker, "alerts") else []
        entries = {
            row["alert_uuid"]: row
            for row in tracker.execute(
                "SELECT alert_uuid, measurement_json, validation_error FROM outcome_entries"
            ).fetchall()
        } if _has_table(tracker, "outcome_entries") else {}
        points = {
            row["alert_uuid"]
            for row in tracker.execute(
                "SELECT DISTINCT alert_uuid FROM outcome_points"
            ).fetchall()
        } if _has_table(tracker, "outcome_points") else set()
        mappings = tracker.execute(
            "SELECT alert_uuid, pro_symbol, fire_time, resolution, source FROM outcome_resolution_map"
        ).fetchall() if _has_table(tracker, "outcome_resolution_map") else []
        mapping_keys = {
            row["alert_uuid"]: (row["pro_symbol"], row["fire_time"], row["resolution"])
            for row in mappings
        }
        for row in alerts:
            uuid = row["alert_uuid"]
            if uuid not in entries:
                findings["missing_entry_provenance"].append({"alert_uuid": uuid})
            elif entries[uuid]["validation_error"]:
                findings["conflicting_or_invalid_records"].append({
                    "alert_uuid": uuid,
                    "reason": entries[uuid]["validation_error"],
                })
            if uuid not in points:
                findings["snapshots_without_trusted_timestamps"].append({"alert_uuid": uuid})
            if uuid in entries and not entries[uuid]["validation_error"] and uuid not in points:
                findings["eligible_needing_observations"].append({"alert_uuid": uuid})
            mapped = mapping_keys.get(uuid)
            if mapped and mapped != (row["tv_symbol"], row["fire_time"], row["resolution"]):
                findings["tracker_fire_identity_mismatch"].append({
                    "alert_uuid": uuid,
                    "tracker": dict(row),
                    "mapping": mapped,
                })

        attempts = fires.execute(
            "SELECT pro_symbol, fire_time, resolution, state, completion_ref "
            "FROM fire_attempt_evidence"
        ).fetchall() if _has_table(fires, "fire_attempt_evidence") else []
        completed_keys = set()
        attempt_counts = {}
        for row in attempts:
            key = (row["pro_symbol"], row["fire_time"], row["resolution"])
            attempt_counts.setdefault(key, {}).setdefault(row["state"], 0)
            attempt_counts[key][row["state"]] += 1
            if row["state"] == "completed":
                completed_keys.add(key)
                findings["confirmed_completion"].append({**dict(row), "key": key})
            elif row["state"] == "deduplicated":
                findings["deduplicated"].append({**dict(row), "key": key})
        for key, counts in attempt_counts.items():
            if (
                counts.get("filtered", 0) >= MAX_FILTERED_ATTEMPTS
                or counts.get("failed", 0) >= MAX_FAILED_ATTEMPTS
            ):
                findings["retry_exhausted"].append({"key": key, "states": counts})
        if _has_table(fires, "downloaded_fires"):
            for row in fires.execute(
                "SELECT pro_symbol, fire_time, resolution FROM downloaded_fires WHERE scored_at IS NOT NULL"
            ):
                key = (row["pro_symbol"], row["fire_time"], row["resolution"])
                if key not in completed_keys:
                    findings["legacy_scored_at_without_evidence"].append(dict(row))
        return {
            "metadata": {
                "as_of": as_of.astimezone(timezone.utc).isoformat(),
                "tracker_db": str(Path(tracker_db).expanduser()),
                "fires_db": str(Path(fires_db).expanduser()),
                "read_only": True,
            },
            "findings": findings,
            "counts": {key: len(value) for key, value in findings.items()},
        }
    finally:
        tracker.close()
        fires.close()


def write_outputs(result: dict, json_out: str, csv_out: str) -> None:
    Path(json_out).expanduser().write_text(
        json.dumps(result, sort_keys=True, indent=2, default=str), encoding="utf-8"
    )
    with Path(csv_out).expanduser().open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("category", "record"))
        writer.writeheader()
        for category, rows in result["findings"].items():
            for row in rows:
                writer.writerow({"category": category, "record": json.dumps(row, sort_keys=True, default=str)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only outcome reconciliation")
    parser.add_argument("--tracker-db", required=True)
    parser.add_argument("--fires-db", required=True)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--csv-out", required=True)
    parser.add_argument("--as-of")
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        parser.error("--as-of must be timezone-aware")
    result = reconcile(args.tracker_db, args.fires_db, as_of)
    write_outputs(result, args.json_out, args.csv_out)
    print("Outcome reconciliation (read-only)")
    for category, count in result["counts"].items():
        print(f"  {category}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())