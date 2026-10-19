"""
TQS v2 Database Persistence
=============================
SQLite persistence for scan results.

Database: database/tqs_v2.db

Tables:
  tqs_v2_scans         — one row per scan run
  tqs_v2_results       — one row per ranked contract
  tqs_v2_stock_universe — stocks scanned in each run

All writes are idempotent (upsert by run_id + opra_code).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from ..config import TQSv2Config
from ..market_data.models import StockRecord
from ..ranking.models import RankedContract

_log = logging.getLogger("tqs_v2.persistence.db")

_DDL = """
CREATE TABLE IF NOT EXISTS tqs_v2_scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL UNIQUE,
    scan_type       TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    stocks_scanned  INTEGER DEFAULT 0,
    contracts_fetched INTEGER DEFAULT 0,
    contracts_after_filter INTEGER DEFAULT 0,
    contracts_scored INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS tqs_v2_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id             INTEGER REFERENCES tqs_v2_scans(id),
    run_id              TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    exchange            TEXT,
    opra_code           TEXT NOT NULL,
    option_type         TEXT NOT NULL,
    expiration          INTEGER,
    strike              REAL,
    dte                 INTEGER,
    bid                 REAL,
    ask                 REAL,
    mid                 REAL,
    iv                  REAL,
    delta               REAL,
    gamma               REAL,
    theta               REAL,
    vega                REAL,
    theo_price          REAL,
    open_interest       INTEGER,
    volume              INTEGER,
    spread_pct          REAL,
    moneyness_pct       REAL,
    breakeven           REAL,
    spot_price          REAL,
    total_score         REAL,
    liquidity_score     REAL,
    iv_score            REAL,
    probability_score   REAL,
    risk_reward_score   REAL,
    grade               TEXT,
    rank_global         INTEGER,
    rank_per_stock      INTEGER,
    raw_contract_json   TEXT,
    created_at          TEXT NOT NULL,
    -- Greeks extended
    bid_iv              REAL,
    ask_iv              REAL,
    breakeven_pct       REAL,
    -- IV Term Structure
    iv_term_slope       REAL,
    iv_1w               REAL,
    iv_1m               REAL,
    iv_3m               REAL,
    slope_3m_1m         REAL,
    slope_6m_3m         REAL,
    iv_contango_score   REAL,
    -- IV Smile
    iv_skew_25d         REAL,
    iv_skew_score       REAL,
    -- Stock Technicals
    stock_rsi           REAL,
    stock_sma200        REAL,
    stock_sma50         REAL,
    stock_macd          REAL,
    stock_atr           REAL,
    stock_adx           REAL,
    -- Dimension sub-scores
    delta_score         REAL,
    dte_score           REAL,
    moneyness_score     REAL,
    oi_score            REAL,
    spread_score        REAL,
    volume_score        REAL,
    breakeven_score     REAL,
    theo_score          REAL,
    win_rate_score      REAL,
    pop_score           REAL,
    iv_pct_adj          REAL,
    iv_rank_proxy       REAL,
    iv_analysis_score   REAL,
    iv_level_score      REAL,
    iv_spread_score     REAL,
    term_slope_score    REAL,
    volatility_conditions_score REAL,
    pcr_score           REAL,
    skew_score          REAL,
    premium_efficiency_score REAL,
    tv_score            REAL,
    spread_eff_score    REAL,
    be_score            REAL,
    be_1sigma_score     REAL,
    delta_iv_score      REAL,
    leverage_score      REAL,
    stock_trend_score   REAL,
    stock_adx_score     REAL,
    -- Outcome tracking (filled later by outcome_tracker)
    expiry_stock_price          REAL,
    intrinsic_value_at_expiry   REAL,
    pnl_pct                     REAL,
    outcome_fetched_at          TEXT,
    is_winner                   INTEGER,
    current_premium             REAL,
    UNIQUE(run_id, opra_code)
);

CREATE TABLE IF NOT EXISTS tqs_v2_stock_universe (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER REFERENCES tqs_v2_scans(id),
    run_id      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    exchange    TEXT,
    close       REAL,
    market_cap  REAL,
    rsi         REAL,
    sma200      REAL,
    volume      REAL,
    sector      TEXT,
    industry    TEXT,
    UNIQUE(run_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_results_run_id ON tqs_v2_results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_symbol ON tqs_v2_results(symbol);
CREATE INDEX IF NOT EXISTS idx_scans_run_id ON tqs_v2_scans(run_id);

CREATE TABLE IF NOT EXISTS daily_oi_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    strike        REAL NOT NULL,
    expiry        TEXT NOT NULL,
    option_type   TEXT NOT NULL,
    oi            INTEGER DEFAULT 0,
    volume        INTEGER DEFAULT 0,
    snapshot_date TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'cboe',
    UNIQUE(symbol, strike, expiry, option_type, snapshot_date) ON CONFLICT REPLACE
);
"""


_MIGRATION_COLUMNS = [
    ("bid_iv", "REAL"),
    ("ask_iv", "REAL"),
    ("breakeven_pct", "REAL"),
    ("iv_term_slope", "REAL"),
    ("iv_1w", "REAL"),
    ("iv_1m", "REAL"),
    ("iv_3m", "REAL"),
    ("slope_3m_1m", "REAL"),
    ("slope_6m_3m", "REAL"),
    ("iv_contango_score", "REAL"),
    ("iv_skew_25d", "REAL"),
    ("iv_skew_score", "REAL"),
    ("stock_rsi", "REAL"),
    ("stock_sma200", "REAL"),
    ("stock_sma50", "REAL"),
    ("stock_macd", "REAL"),
    ("stock_atr", "REAL"),
    ("stock_adx", "REAL"),
    ("delta_score", "REAL"),
    ("dte_score", "REAL"),
    ("moneyness_score", "REAL"),
    ("oi_score", "REAL"),
    ("spread_score", "REAL"),
    ("volume_score", "REAL"),
    ("breakeven_score", "REAL"),
    ("theo_score", "REAL"),
    ("win_rate_score", "REAL"),
    ("pop_score", "REAL"),
    ("iv_pct_adj", "REAL"),
    ("iv_rank_proxy", "REAL"),
    ("iv_analysis_score", "REAL"),
    ("iv_level_score", "REAL"),
    ("iv_spread_score", "REAL"),
    ("term_slope_score", "REAL"),
    ("volatility_conditions_score", "REAL"),
    ("pcr_score", "REAL"),
    ("skew_score", "REAL"),
    ("premium_efficiency_score", "REAL"),
    ("tv_score", "REAL"),
    ("spread_eff_score", "REAL"),
    ("be_score", "REAL"),
    ("be_1sigma_score", "REAL"),
    ("delta_iv_score", "REAL"),
    ("leverage_score", "REAL"),
    ("stock_trend_score", "REAL"),
    ("stock_adx_score", "REAL"),
    ("expiry_stock_price", "REAL"),
    ("intrinsic_value_at_expiry", "REAL"),
    ("pnl_pct", "REAL"),
    ("outcome_fetched_at", "TEXT"),
    ("is_winner", "INTEGER"),
    ("current_premium", "REAL"),
    # ── Catalyst mode columns ────────────────────────────────────────────────
    ("catalyst_score",       "REAL"),
    ("catalyst_mode",        "INTEGER DEFAULT 0"),
    ("iv_2w",               "REAL"),
    ("iv_hump",              "REAL"),
    ("iv_backwardation",     "REAL"),
    ("vol_oi_ratio",         "REAL"),
    ("call_vol_skew",        "REAL"),
    ("price_breakout_pct",   "REAL"),
    ("analyst_target_gap",   "REAL"),
    ("gex",                  "REAL"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add any missing columns to existing databases (idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tqs_v2_results)").fetchall()}
    for col, col_type in _MIGRATION_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE tqs_v2_results ADD COLUMN {col} {col_type}")
    conn.commit()


def _adx_score_from_contract(adx) -> float:
    """Compute stock_adx_score consistent with json_reporter."""
    if adx is None:
        return 0.0
    adx = float(adx or 0.0)
    if adx > 30.0:
        return 10.0
    if adx >= 20.0:
        return 5.0
    if adx < 14.0:
        return -10.0
    return 0.0


class TQSv2Database:
    """Manages persistence for TQS v2 scan results."""

    def __init__(self, config: TQSv2Config) -> None:
        self._db_path = config.database_dir / config.database_name
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()
        _apply_migrations(self._conn)
        _log.debug("TQS v2 DB ready: %s", self._db_path)

    def create_scan_record(
        self,
        run_id: str,
        scan_type: str,
    ) -> int:
        """Create a scan record and return its id."""
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO tqs_v2_scans
               (run_id, scan_type, started_at, status)
               VALUES (?, ?, ?, 'running')""",
            (run_id, scan_type, datetime.utcnow().isoformat()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM tqs_v2_scans WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row["id"]

    def update_scan_stats(
        self,
        run_id: str,
        stocks_scanned: int = 0,
        contracts_fetched: int = 0,
        contracts_after_filter: int = 0,
        contracts_scored: int = 0,
        status: str = "completed",
    ) -> None:
        """Update scan statistics at completion."""
        self._conn.execute(
            """UPDATE tqs_v2_scans SET
               completed_at = ?,
               stocks_scanned = ?,
               contracts_fetched = ?,
               contracts_after_filter = ?,
               contracts_scored = ?,
               status = ?
               WHERE run_id = ?""",
            (
                datetime.utcnow().isoformat(),
                stocks_scanned,
                contracts_fetched,
                contracts_after_filter,
                contracts_scored,
                status,
                run_id,
            ),
        )
        self._conn.commit()

    def save_results(
        self,
        scan_id: int,
        run_id: str,
        ranked: list[RankedContract],
    ) -> None:
        """Insert ranked contracts with full field set. Ignores duplicates."""
        now = datetime.utcnow().isoformat()
        rows = []
        for rc in ranked:
            c = rc.contract
            s = rc.scored.scores

            raw_json = json.dumps({
                "opra_code": c.opra_code,
                "bid_iv": c.bid_iv,
                "ask_iv": c.ask_iv,
                "rho": c.rho,
                "atm": c.atm,
            })

            # Computed fields
            breakeven_pct = (
                round((c.breakeven - c.spot_price) / c.spot_price * 100, 2)
                if c.spot_price else None
            )
            iv_rank_proxy = round(
                max(0.0, min(100.0, 100.0 - (getattr(c, "iv_contango_score", 50.0) or 50.0))), 1
            )

            rows.append((
                # ── Core ─────────────────────────────────────────────────────
                scan_id, run_id,
                c.root, c.exchange, c.opra_code,
                c.option_type, c.expiration, c.strike, c.dte,
                c.bid, c.ask, c.mid, c.iv, c.delta, c.gamma, c.theta, c.vega,
                c.theo_price, c.open_interest, c.volume,
                c.spread_pct, c.moneyness_pct, c.breakeven, c.spot_price,
                rc.total_score, s.liquidity, s.iv_analysis, s.probability, s.risk_reward,
                rc.grade, rc.rank_global, rc.rank_per_stock,
                raw_json, now,
                # ── Greeks extended ───────────────────────────────────────────
                getattr(c, "bid_iv", None),
                getattr(c, "ask_iv", None),
                breakeven_pct,
                # ── IV Term Structure ─────────────────────────────────────────
                getattr(c, "iv_term_slope", None),
                getattr(c, "iv_1w", None),
                getattr(c, "iv_1m", None),
                getattr(c, "iv_3m", None),
                getattr(c, "slope_3m_1m", None),
                getattr(c, "slope_6m_3m", None),
                getattr(c, "iv_contango_score", None),
                # ── IV Smile ──────────────────────────────────────────────────
                getattr(c, "iv_skew_25d", None),
                getattr(c, "iv_skew_score", None),
                # ── Stock Technicals ──────────────────────────────────────────
                getattr(c, "stock_rsi", None),
                getattr(c, "stock_sma200", None),
                getattr(c, "stock_sma50", None),
                getattr(c, "stock_macd", None),
                getattr(c, "stock_atr", None),
                getattr(c, "stock_adx", None),
                # ── Dimension sub-scores ──────────────────────────────────────
                s.delta_score,
                s.dte_score,
                s.moneyness_score,
                s.oi_score,
                s.spread_score,
                s.volume_score,
                s.breakeven_score,
                s.theo_score,
                s.win_rate,          # win_rate_score column
                s.pop_score,
                s.iv_pct_adj,
                iv_rank_proxy,
                s.iv_analysis,       # iv_analysis_score column
                s.iv_level_score,
                s.iv_spread_score,
                s.term_slope_score,
                s.volatility_conditions,  # volatility_conditions_score column
                s.pcr_score,
                s.skew_score,
                s.premium_efficiency,     # premium_efficiency_score column
                s.tv_score,
                s.spread_eff_score,
                s.be_score,
                s.be_1sigma_score,
                s.delta_iv_score,
                s.leverage_score,
                s.stock_trend,            # stock_trend_score column
                _adx_score_from_contract(getattr(c, "stock_adx", None)),
                # ── Outcome columns (filled later by OutcomeTracker) ──────────
                None,  # expiry_stock_price
                None,  # intrinsic_value_at_expiry
                None,  # pnl_pct
                None,  # outcome_fetched_at
                None,  # is_winner
                # ── Catalyst columns ──────────────────────────────────────────
                getattr(rc.scored.scores, "catalyst_score", None),
                1 if getattr(rc.scored, "catalyst_total", 0.0) > 0.0 else 0,  # catalyst_mode flag
                getattr(c, "iv_2w", None),
                getattr(c, "iv_hump", None),
                getattr(c, "iv_backwardation", None),
                getattr(c, "vol_oi_ratio", None),
                getattr(c, "call_vol_skew", None),
                getattr(c, "price_breakout_pct", None),
                getattr(c, "analyst_target_gap", None),
                getattr(c, "gex", None),
            ))
        self._conn.executemany(
            """INSERT OR IGNORE INTO tqs_v2_results
               (scan_id, run_id, symbol, exchange, opra_code, option_type,
                expiration, strike, dte, bid, ask, mid, iv, delta, gamma, theta, vega,
                theo_price, open_interest, volume, spread_pct, moneyness_pct,
                breakeven, spot_price, total_score, liquidity_score, iv_score,
                probability_score, risk_reward_score, grade, rank_global,
                rank_per_stock, raw_contract_json, created_at,
                bid_iv, ask_iv, breakeven_pct,
                iv_term_slope, iv_1w, iv_1m, iv_3m, slope_3m_1m, slope_6m_3m, iv_contango_score,
                iv_skew_25d, iv_skew_score,
                stock_rsi, stock_sma200, stock_sma50, stock_macd, stock_atr, stock_adx,
                delta_score, dte_score, moneyness_score,
                oi_score, spread_score, volume_score,
                breakeven_score, theo_score,
                win_rate_score, pop_score, iv_pct_adj, iv_rank_proxy,
                iv_analysis_score, iv_level_score, iv_spread_score, term_slope_score,
                volatility_conditions_score, pcr_score, skew_score,
                premium_efficiency_score, tv_score, spread_eff_score,
                be_score, be_1sigma_score, delta_iv_score, leverage_score,
                stock_trend_score, stock_adx_score,
                expiry_stock_price, intrinsic_value_at_expiry, pnl_pct,
                outcome_fetched_at, is_winner,
                catalyst_score, catalyst_mode,
                iv_2w, iv_hump, iv_backwardation, vol_oi_ratio, call_vol_skew,
                price_breakout_pct, analyst_target_gap, gex)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self._conn.commit()
        _log.info("Saved %d results for run_id %s", len(ranked), run_id)

    def save_stock_universe(
        self,
        scan_id: int,
        run_id: str,
        stocks: list[StockRecord],
    ) -> None:
        """Save the stock universe used for this scan."""
        rows = [
            (
                scan_id, run_id,
                s.symbol, s.exchange, s.close, s.market_cap,
                s.rsi, s.sma200, s.volume,
                s.sector, s.industry,
            )
            for s in stocks
        ]
        self._conn.executemany(
            """INSERT OR IGNORE INTO tqs_v2_stock_universe
               (scan_id, run_id, symbol, exchange, close, market_cap,
                rsi, sma200, volume, sector, industry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
