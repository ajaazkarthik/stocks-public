"""Centralised SQLite connection factory.

Every module that opens a database connection SHOULD use ``get_conn()``
so that WAL mode, busy-timeout, and foreign-keys are applied uniformly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DB_DIR = Path(__file__).parent  # database/

_DB_REGISTRY: dict[str, Path] = {
    "alert_tracker":          _DB_DIR / "alert_tracker.db",
    "order_ledger":           _DB_DIR / "order_ledger.db",
    "sell_history":           _DB_DIR / "sell_history.db",
    "portfolio_history":      _DB_DIR / "portfolio_history.db",
    "portfolio_score_history": _DB_DIR / "portfolio_score_history.db",
    "report_analytics":       _DB_DIR / "report_analytics.db",
    "fires_ledger":           _DB_DIR / "fires_ledger.db",
}


def get_conn(db_name: str) -> sqlite3.Connection:
    """Return a configured SQLite connection with WAL, busy_timeout, foreign_keys."""
    path = _DB_REGISTRY.get(db_name)
    if path is None:
        raise ValueError(f"Unknown database: {db_name!r}. Known: {sorted(_DB_REGISTRY)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_path(db_name: str) -> Path:
    """Return the canonical path for a named database."""
    path = _DB_REGISTRY.get(db_name)
    if path is None:
        raise ValueError(f"Unknown database: {db_name!r}. Known: {sorted(_DB_REGISTRY)}")
    return path
