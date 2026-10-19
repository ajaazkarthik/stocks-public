"""Versioned schema migration system for the stocks trading system.

Replaces the per-import ALTER TABLE approach with a tracked migration
system using a ``schema_version`` table.  Each database's migrations are
numbered starting at 1.  ``run_migrations()`` is idempotent — it only
runs migrations newer than the recorded version.
"""

from __future__ import annotations

import sqlite3


# ── Schema version tracking ───────────────────────────────────────────────────

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    db_name    TEXT PRIMARY KEY,
    version    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    DEFAULT (datetime('now'))
);
"""


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_VERSION_DDL)


def _get_version(conn: sqlite3.Connection, db_name: str) -> int:
    row = conn.execute(
        "SELECT version FROM schema_version WHERE db_name = ?", (db_name,)
    ).fetchone()
    return row[0] if row else 0


def _set_version(conn: sqlite3.Connection, db_name: str, version: int) -> None:
    conn.execute(
        """INSERT INTO schema_version (db_name, version, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(db_name) DO UPDATE SET version = excluded.version,
                                              updated_at = excluded.updated_at""",
        (db_name, version),
    )
    conn.commit()


# ── alert_tracker migrations ─────────────────────────────────────────────────
# Each entry is (version, sql).
# Numbering matches the historical _MIGRATION_COLUMNS order.

_ALERT_TRACKER_MIGRATIONS: list[tuple[int, str]] = [
    # v1–v19: alerts table columns
    (1,  "ALTER TABLE alerts ADD COLUMN signal_composite       REAL"),
    (2,  "ALTER TABLE alerts ADD COLUMN signal_threshold       REAL"),
    (3,  "ALTER TABLE alerts ADD COLUMN tradable               INTEGER DEFAULT 1"),
    (4,  "ALTER TABLE alerts ADD COLUMN tradable_limitations   INTEGER DEFAULT 0"),
    (5,  "ALTER TABLE alerts ADD COLUMN watchlist_cleaned      INTEGER DEFAULT 0"),
    (6,  "ALTER TABLE alerts ADD COLUMN input_source           TEXT"),
    (7,  "ALTER TABLE alerts ADD COLUMN win_rate_at_fire       REAL"),
    (8,  "ALTER TABLE alerts ADD COLUMN vix_at_fire            REAL"),
    (9,  "ALTER TABLE alerts ADD COLUMN market_regime          TEXT"),
    (10, "ALTER TABLE alerts ADD COLUMN net_profit_pct         REAL"),
    (11, "ALTER TABLE alerts ADD COLUMN squeeze_off            INTEGER DEFAULT 0"),
    (12, "ALTER TABLE alerts ADD COLUMN adx_at_entry           REAL"),
    (13, "ALTER TABLE alerts ADD COLUMN rsi_at_entry           REAL"),
    (14, "ALTER TABLE alerts ADD COLUMN ma_cross_at_entry      INTEGER DEFAULT 0"),
    (15, "ALTER TABLE alerts ADD COLUMN win_rate_pct           REAL"),
    (16, "ALTER TABLE alerts ADD COLUMN l5_score               REAL"),
    (17, "ALTER TABLE alerts ADD COLUMN plus_di                REAL"),
    (18, "ALTER TABLE alerts ADD COLUMN minus_di               REAL"),
    (19, "ALTER TABLE alerts ADD COLUMN trigger_price          REAL"),
    # v20–v27: options_positions table columns
    (20, "ALTER TABLE options_positions ADD COLUMN dte_at_entry            INTEGER"),
    (21, "ALTER TABLE options_positions ADD COLUMN moneyness               TEXT"),
    (22, "ALTER TABLE options_positions ADD COLUMN underlying_price_at_open REAL"),
    (23, "ALTER TABLE options_positions ADD COLUMN pnl_pct                 REAL"),
    (24, "ALTER TABLE options_positions ADD COLUMN hold_days               INTEGER"),
    (25, "ALTER TABLE options_positions ADD COLUMN outcome                 TEXT DEFAULT 'OPEN'"),
    (26, "ALTER TABLE options_positions ADD COLUMN first_seen_at           TEXT"),
    (27, "ALTER TABLE options_positions ADD COLUMN last_seen_at            TEXT"),
    # v28: delisted ticker detection (P2-B)
    (28, "ALTER TABLE alerts ADD COLUMN is_delisted             INTEGER DEFAULT 0"),
    # v29–v35: book-sourced signals (rs_rating, vpa_anomaly, darvas_box,
    #           gap_analysis, acquirers_multiple, per_market_expectancy, total_bonus)
    (29, "ALTER TABLE alerts ADD COLUMN book_vpa_score          REAL"),
    (30, "ALTER TABLE alerts ADD COLUMN book_darvas_score       REAL"),
    (31, "ALTER TABLE alerts ADD COLUMN book_gap_score          REAL"),
    (32, "ALTER TABLE alerts ADD COLUMN book_am_score           REAL"),
    (33, "ALTER TABLE alerts ADD COLUMN book_rs_score           REAL"),
    (34, "ALTER TABLE alerts ADD COLUMN book_expectancy_score   REAL"),
    (35, "ALTER TABLE alerts ADD COLUMN book_total_bonus        REAL"),
    # v36–v37: Moneyball options scoring columns
    (36, "ALTER TABLE options_recommendations ADD COLUMN moneyball_score REAL"),
    (37, "ALTER TABLE options_recommendations ADD COLUMN final_score     REAL"),
    # v38–v39: current premium tracking for OPEN options recommendations
    (38, "ALTER TABLE options_recommendations ADD COLUMN current_premium    REAL"),
    (39, "ALTER TABLE options_recommendations ADD COLUMN current_premium_at TEXT"),
    (40, "ALTER TABLE signal_engine_outputs ADD COLUMN scanner_setup_id TEXT"),
]

_MIGRATION_REGISTRY: dict[str, list[tuple[int, str]]] = {
    "alert_tracker":          _ALERT_TRACKER_MIGRATIONS,
    # These DBs start with all columns in their CREATE TABLE DDL.
    # Register them here so future schema evolution can use versioned migrations
    # instead of ad-hoc ALTER TABLE statements scattered across source files.
    "order_ledger":           [],
    "sell_history":           [],
    "portfolio_score_history": [],
    "fires_ledger":           [],
}


def run_migrations(db_name: str, conn: sqlite3.Connection) -> int:
    """Run pending migrations for *db_name* on *conn*.

    Returns the number of migrations applied.  Each ALTER is wrapped in
    its own try/except so that columns which already exist (from the old
    per-import approach) are silently skipped.
    """
    _ensure_version_table(conn)
    current = _get_version(conn, db_name)
    migrations = _MIGRATION_REGISTRY.get(db_name, [])
    applied = 0

    for version, sql in migrations:
        if version <= current:
            continue
        try:
            conn.execute(sql)
            applied += 1
        except sqlite3.OperationalError:
            # Column already exists (idempotent with old approach)
            pass
        current = version

    if current != _get_version(conn, db_name):
        _set_version(conn, db_name, current)
    return applied
