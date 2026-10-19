"""
ZeroDTE SQLite Database
========================
Writes scan results to a persistent SQLite database for tracking
and backtesting the quality of ZeroDTE signals over time.

Schema tracks:
  - Stock-level signal scores
  - Contract details (strike, expiry, DTE, premium, Greeks)
  - Outcome tracking (actual_move_pct, contract_return_pct) — filled later
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import ZeroDTEConfig, DEFAULT_CONFIG
from .models import ZeroDTEContract

_log = logging.getLogger("zerodte.db")

# ── DDL ──────────────────────────────────────────────────────────────────────

_CREATE_SCANS_TABLE = """
CREATE TABLE IF NOT EXISTS zerodte_scans (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts            TEXT NOT NULL,
    scan_date          TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    option_type        TEXT NOT NULL,
    direction          TEXT NOT NULL,
    contract_code      TEXT,
    expiry             TEXT,
    dte                INTEGER,
    strike             REAL,
    spot_price         REAL,
    otm_pct            REAL,
    bid                REAL,
    ask                REAL,
    mid_price          REAL,
    premium            REAL,
    iv                 REAL,
    delta              REAL,
    gamma              REAL,
    theta              REAL,
    open_interest      INTEGER,
    volume             INTEGER,
    spread_pct         REAL,
    momentum_score     REAL,
    iv_back_score      REAL,
    voi_score          REAL,
    composite_score    REAL,
    confidence         TEXT,
    prev_day_change_pct REAL,
    volume_ratio       REAL,
    rsi                REAL,
    iv_1w              REAL,
    iv_1m              REAL,
    iv_slope           REAL,
    voi_ratio          REAL,
    sector             TEXT,
    industry           TEXT,
    actual_move_pct    REAL,
    contract_return_pct REAL,
    UNIQUE(scan_ts, contract_code)
);
"""

_CREATE_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS zerodte_signals (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts            TEXT NOT NULL,
    scan_date          TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    direction          TEXT NOT NULL,
    composite_score    REAL,
    momentum_score     REAL,
    iv_back_score      REAL,
    voi_score          REAL,
    prev_day_change_pct REAL,
    volume_ratio       REAL,
    rsi                REAL,
    iv_1w              REAL,
    iv_1m              REAL,
    iv_slope           REAL,
    voi_ratio          REAL,
    iv_available       INTEGER,
    voi_available      INTEGER,
    sector             TEXT,
    industry           TEXT,
    contracts_found    INTEGER DEFAULT 0,
    UNIQUE(scan_ts, symbol, direction)
);
"""


def init_db(db_path: Path) -> None:
    """Ensure the database and tables exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(_CREATE_SCANS_TABLE)
            conn.execute(_CREATE_SIGNALS_TABLE)
            conn.commit()
        _log.debug("DB initialized at %s", db_path)
    except sqlite3.Error as exc:
        _log.error("DB init error: %s", exc)
        raise


def write_contracts(
    contracts: list[ZeroDTEContract],
    direction: str,
    config: ZeroDTEConfig = DEFAULT_CONFIG,
) -> int:
    """
    Write a list of ZeroDTEContracts to the database.
    Returns number of rows inserted.
    """
    if not contracts:
        return 0

    db_path = config.database_dir / config.database_name
    scan_ts = datetime.now(timezone.utc).isoformat()
    scan_date = scan_ts[:10]

    rows = []
    for c in contracts:
        sig = c.signal
        rows.append((
            scan_ts, scan_date, c.symbol, c.option_type, direction,
            c.contract_code, c.expiry, c.dte,
            c.strike, c.spot_price, c.otm_pct,
            c.bid, c.ask, c.mid_price, c.ask,  # premium = ask
            c.iv, c.delta, c.gamma, c.theta,
            c.open_interest, c.volume, c.spread_pct,
            sig.momentum_score if sig else 0.0,
            sig.iv_back_score if sig else 0.0,
            sig.voi_score if sig else 0.0,
            sig.composite_score if sig else 0.0,
            c.confidence,
            sig.prev_day_change_pct if sig else 0.0,
            sig.volume_ratio if sig else 0.0,
            sig.rsi if sig else 0.0,
            sig.iv_1w if sig else 0.0,
            sig.iv_1m if sig else 0.0,
            sig.iv_back_slope if sig else 0.0,
            sig.voi_ratio if sig else 0.0,
            sig.sector if sig else "",
            sig.industry if sig else "",
            None, None,  # actual_move_pct, contract_return_pct — filled later
        ))

    sql = """
    INSERT OR IGNORE INTO zerodte_scans (
        scan_ts, scan_date, symbol, option_type, direction,
        contract_code, expiry, dte,
        strike, spot_price, otm_pct,
        bid, ask, mid_price, premium,
        iv, delta, gamma, theta,
        open_interest, volume, spread_pct,
        momentum_score, iv_back_score, voi_score, composite_score, confidence,
        prev_day_change_pct, volume_ratio, rsi,
        iv_1w, iv_1m, iv_slope, voi_ratio,
        sector, industry,
        actual_move_pct, contract_return_pct
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.executemany(sql, rows)
            conn.commit()
        _log.info("Wrote %d contracts to DB (%s direction)", len(rows), direction)
        return len(rows)
    except sqlite3.Error as exc:
        _log.error("DB write error: %s", exc)
        return 0
