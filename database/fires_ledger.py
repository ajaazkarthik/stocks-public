#!/usr/bin/env python3
"""Durable record of every TradingView fire we have ever downloaded.

TradingView's ``/list_fires`` endpoint is hard-capped server-side at 100 fires
per alert and supports no pagination (probed 2026-08-19: ``limit`` only caps
downward, and ``offset``/cursor/time-window parameters are silently ignored).
``fires_raw.json`` is rewritten wholesale on every download. Together those mean
a fire that falls out of TradingView's rolling window is gone forever.

This ledger is the only durable record of what has been *downloaded*, as
distinct from ``alert_tracker.alerts`` which records what was successfully
*scored*. It is what makes a "new alerts only" run possible: a fire filtered out
before scoring is still remembered here, so it is not re-offered every run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from database.db_factory import get_conn  # noqa: E402

DB_NAME = "fires_ledger"

# fire_id is not a safe primary key on its own: screener and webhook fires mint
# their own ids from int(time.time() * 1000), a different namespace from
# TradingView's. The natural key carries identity; fire_id is only metadata.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloaded_fires (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pro_symbol    TEXT    NOT NULL,
    fire_time     TEXT    NOT NULL,
    resolution    TEXT    NOT NULL DEFAULT '',
    fire_id       INTEGER,
    alert_id      INTEGER,
    symbol        TEXT,
    name          TEXT,
    message       TEXT,
    bar_time      TEXT,
    source        TEXT    NOT NULL DEFAULT 'download',
    fire_json     TEXT,
    first_seen_at TEXT    NOT NULL,
    scored_at     TEXT,
    UNIQUE (pro_symbol, fire_time, resolution)
);
CREATE INDEX IF NOT EXISTS idx_fires_first_seen ON downloaded_fires(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_fires_pro_symbol ON downloaded_fires(pro_symbol);
CREATE INDEX IF NOT EXISTS idx_fires_scored_at  ON downloaded_fires(scored_at);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = get_conn(DB_NAME)
    conn.executescript(_SCHEMA)
    from tradingview.outcome_store import ensure_schema
    ensure_schema(conn)
    return conn


def fire_key(fire: dict) -> tuple[str, str, str]:
    """Return the natural identity of a fire: (pro_symbol, fire_time, resolution)."""
    pro = str(fire.get("pro_symbol") or fire.get("symbol") or "").strip()
    return pro, str(fire.get("fire_time") or "").strip(), str(fire.get("resolution") or "").strip()


def sync_scored_from_alert_tracker() -> int:
    """Legacy compatibility no-op: tracker existence is not scoring evidence.

    Previously matched only symbol/time (not resolution) and fabricated scored
    timestamps. Historical scored_at is retained for audit, not trusted by the
    new unseen policy. Explicit completion attempts are authoritative.
    """
    return 0


def record_fires(fires: list[dict], source: str = "download") -> int:
    """Insert *fires* into the ledger. Returns how many were not already present."""
    if not fires:
        return 0
    seen_at = _now_iso()
    rows = []
    for fire in fires:
        pro, fire_time, resolution = fire_key(fire)
        if not pro or not fire_time:
            continue
        rows.append((
            pro, fire_time, resolution,
            fire.get("fire_id"), fire.get("alert_id"),
            fire.get("symbol"), fire.get("name"), fire.get("message"),
            fire.get("bar_time"), fire.get("_source") or source,
            json.dumps(fire, default=str), seen_at,
        ))
    if not rows:
        return 0
    with _conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM downloaded_fires").fetchone()[0]
        conn.executemany(
            """INSERT OR IGNORE INTO downloaded_fires
               (pro_symbol, fire_time, resolution, fire_id, alert_id, symbol,
                name, message, bar_time, source, fire_json, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        after = conn.execute("SELECT COUNT(*) FROM downloaded_fires").fetchone()[0]
    sync_scored_from_alert_tracker()
    return after - before


def known_keys() -> set[tuple[str, str, str]]:
    """Return every fire identity currently in the ledger."""
    with _conn() as conn:
        return {
            (r["pro_symbol"], r["fire_time"], r["resolution"])
            for r in conn.execute(
                "SELECT pro_symbol, fire_time, resolution FROM downloaded_fires"
            )
        }


# Retry policy for fires that were considered but produced no scoring result.
MAX_FILTERED_ATTEMPTS = 3
MAX_FAILED_ATTEMPTS = 5


def scored_keys() -> set[tuple[str, str, str]]:
    """Identities that must NOT be offered again by ``--new-only``.

    Suppressed when any of:
      * ``scored_at`` is set. Legacy rows keep their old (whole-batch) meaning so
        a rollout does not requeue history; since the completed-only fix,
        ``mark_scored`` is only called for fires with a real scoring result.
      * a ``completed`` attempt exists (survives an interrupted run);
      * it was ``deduplicated`` against a fire whose own scoring is suppressed;
      * it was ``filtered`` / ``failed`` at least MAX_* times (bounded retry).
    Fires that were merely filtered or failed fewer times, and those that were
    deduplicated against a fire that never completed, are offered again.
    """
    with _conn() as conn:
        # Legacy/test connections may create only the fires ledger schema.
        from tradingview.outcome_store import ensure_schema
        ensure_schema(conn)
        suppressed = {
            (r["pro_symbol"], r["fire_time"], r["resolution"])
            for r in conn.execute(
                "SELECT pro_symbol, fire_time, resolution FROM downloaded_fires "
                "WHERE scored_at IS NOT NULL"
            )
        }
        resets = {
            (r[0], r[1], r[2]): r[3]
            for r in conn.execute(
                "SELECT pro_symbol, fire_time, resolution, MAX(reset_at) "
                "FROM fire_attempt_resets GROUP BY pro_symbol, fire_time, resolution"
            )
        }
        counts: dict[tuple[str, str, str], dict[str, int]] = {}
        dedup: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        completed: set[tuple[str, str, str]] = set()

        def is_after_reset(value, reset_at):
            try:
                return value > reset_at
            except TypeError:
                return False

        for row in conn.execute(
            "SELECT pro_symbol, fire_time, resolution, state, "
            "deduplicated_against, considered_at FROM fire_attempt_evidence"
        ):
            key = (row[0], row[1], row[2])
            reset_at = resets.get(key)
            if reset_at and not is_after_reset(row[5], reset_at) and row[3] in {"filtered", "failed", "deduplicated"}:
                continue
            if row[3] == "completed":
                completed.add(key)
            counts.setdefault(key, {}).setdefault(row[3], 0)
            counts[key][row[3]] += 1
            if row[3] == "deduplicated" and row[4]:
                try:
                    target = json.loads(row[4])
                    dedup[key] = (target["pro_symbol"], target["fire_time"], target["resolution"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue
        for key, state_counts in counts.items():
            if (
                state_counts.get("filtered", 0) >= MAX_FILTERED_ATTEMPTS
                or state_counts.get("failed", 0) >= MAX_FAILED_ATTEMPTS
            ):
                suppressed.add(key)

            suppressed.update(completed)
        def replacement_completed(key, visiting=frozenset()):
            if key in completed:
                return True
            if key in visiting:
                return False
            target = dedup.get(key)
            return target is not None and replacement_completed(target, visiting | {key})

        suppressed.update(key for key in dedup if replacement_completed(key))
        return suppressed


def mark_scored(fires: list[dict]) -> int:
    """Mark explicitly completed scoring results. Returns rows updated.

    Callers must supply only fires that produced a scoring result. Considered,
    filtered, failed, selection-deduplicated and unselected fires are not scored.
    This does not certify actionability or downstream tracker persistence.
    Existing historical scored_at values are not reinterpreted or cleared here.
    """
    keys = [fire_key(f) for f in fires]
    keys = [k for k in keys if k[0] and k[1]]
    if not keys:
        return 0
    stamp = _now_iso()
    with _conn() as conn:
        cur = conn.executemany(
            "UPDATE downloaded_fires SET scored_at = ? "
            "WHERE pro_symbol = ? AND fire_time = ? AND resolution = ? AND scored_at IS NULL",
            [(stamp, *k) for k in keys],
        )
        return cur.rowcount


def reset_transient_attempts(
    keys: set[tuple[str, str, str]] | None = None,
    *,
    reason: str = "operator_requested",
    reset_by: str = "manual",
) -> int:
    """Record an auditable retry reset without deleting attempt history."""
    if not reason.strip() or not reset_by.strip():
        raise ValueError("reset reason and reset_by are required")
    reset_at = _now_iso()
    with _conn() as conn:
        from tradingview.outcome_store import ensure_schema
        ensure_schema(conn)
        if keys is None:
            keys = {
                (row[0], row[1], row[2])
                for row in conn.execute(
                    "SELECT DISTINCT pro_symbol, fire_time, resolution "
                    "FROM fire_attempt_evidence WHERE state IN ('filtered','failed')"
                )
            }
        rows = [
            (
                f"reset:{uuid.uuid4().hex}", pro_symbol, fire_time, resolution,
                reason, reset_at, reset_by,
            )
            for pro_symbol, fire_time, resolution in keys
            if pro_symbol and fire_time and resolution
        ]
        conn.executemany(
            "INSERT INTO fire_attempt_resets "
            "(reset_id,pro_symbol,fire_time,resolution,reason,reset_at,reset_by) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)


def unseen_fires(fires: list[dict]) -> list[dict]:
    """Return the subset of *fires* that has not yet been scored.

    "New" cannot mean "absent from the ledger": the downloader records fires
    before the scorer runs, so everything would already be known. It means
    downloaded but never put through the pipeline.

    Order is preserved, and duplicates within *fires* itself collapse to the
    first occurrence.
    """
    already = scored_keys()
    out, batch_seen = [], set()
    for fire in fires:
        key = fire_key(fire)
        if not key[0] or not key[1]:
            continue
        if key in already or key in batch_seen:
            continue
        batch_seen.add(key)
        out.append(fire)
    return out


def count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM downloaded_fires").fetchone()[0]


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the downloaded-fires ledger")
    parser.add_argument("--stats", action="store_true", help="Show ledger summary")
    args = parser.parse_args()

    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM downloaded_fires").fetchone()[0]
        unscored = conn.execute(
            "SELECT COUNT(*) FROM downloaded_fires WHERE scored_at IS NULL"
        ).fetchone()[0]
        span = conn.execute(
            "SELECT MIN(fire_time), MAX(fire_time) FROM downloaded_fires"
        ).fetchone()
        by_source = conn.execute(
            "SELECT source, COUNT(*) c FROM downloaded_fires GROUP BY source ORDER BY c DESC"
        ).fetchall()

    print(f"Ledger: {total} fires ({unscored} never scored)")
    if total:
        print(f"  fire_time span: {span[0]} .. {span[1]}")
        for row in by_source:
            print(f"  {row['source']:<12} {row['c']}")
    if not args.stats:
        print("\n(use --stats for the same summary; this module is mainly a library)")


if __name__ == "__main__":
    _main()
