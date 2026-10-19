"""
OI Snapshot Store
==================

Persists daily OI snapshots to tqs_v2.db table daily_oi_snapshots.
Used to compute day-over-day OI change (S4: OI accumulation signal).

Table schema (see db.py _DDL migration):
  daily_oi_snapshots (symbol, strike, expiry, option_type, oi, volume,
                      snapshot_date, source)
  UNIQUE (symbol, strike, expiry, option_type, snapshot_date) ON CONFLICT REPLACE
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

_log = logging.getLogger("tqs_v2.persistence.oi_snapshots")


class OISnapshotStore:
    """Read/write daily OI snapshots for day-over-day comparison."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def save_snapshot(
        self,
        symbol: str,
        contracts: list[dict],  # list of {strike, expiry, option_type, oi, volume}
        source: str = "cboe",
        snapshot_date: date | None = None,
    ) -> int:
        """
        Persist today's OI snapshot. Returns rows inserted.
        Upsert semantics: ON CONFLICT REPLACE.
        """
        today = (snapshot_date or date.today()).isoformat()
        rows = [
            (
                symbol,
                c["strike"],
                c["expiry"],
                c["option_type"],
                c.get("oi", 0),
                c.get("volume", 0),
                today,
                source,
            )
            for c in contracts
        ]

        sql = """
            INSERT OR REPLACE INTO daily_oi_snapshots
                (symbol, strike, expiry, option_type, oi, volume, snapshot_date, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.executemany(sql, rows)
            conn.commit()
        _log.debug("OISnapshotStore: saved %d rows for %s on %s", len(rows), symbol, today)
        return len(rows)

    def get_previous_oi(
        self,
        symbol: str,
        strike: float,
        expiry: str,
        option_type: str,
        days_back: int = 1,
    ) -> int | None:
        """Return OI from N trading days ago for a specific contract."""
        sql = """
            SELECT oi FROM daily_oi_snapshots
            WHERE symbol = ? AND strike = ? AND expiry = ?
              AND option_type = ?
            ORDER BY snapshot_date DESC
            LIMIT 1 OFFSET ?
        """
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, (symbol, strike, expiry, option_type, days_back - 1)).fetchone()
        return row[0] if row else None

    def get_oi_change_pct(
        self,
        symbol: str,
        strike: float,
        expiry: str,
        option_type: str,
    ) -> float | None:
        """Return (today_oi - yesterday_oi) / yesterday_oi as a fraction. None if unavailable."""
        sql = """
            SELECT oi, snapshot_date FROM daily_oi_snapshots
            WHERE symbol = ? AND strike = ? AND expiry = ?
              AND option_type = ?
            ORDER BY snapshot_date DESC
            LIMIT 2
        """
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, (symbol, strike, expiry, option_type)).fetchall()

        if len(rows) < 2:
            return None
        today_oi, yesterday_oi = rows[0][0], rows[1][0]
        if yesterday_oi == 0:
            return None
        return (today_oi - yesterday_oi) / yesterday_oi
