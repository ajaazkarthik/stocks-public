"""Canonical queries shared across alert_tracker, performance_analyzer, etc.

Single source of truth for win-rate calculation so different tools
cannot diverge on definitions.
"""

from __future__ import annotations

import sqlite3

WIN_THRESHOLD_PCT: float = 3.0


def canonical_win_rate(conn: sqlite3.Connection) -> dict:
    """Return the canonical win rate for all closed trades.

    Uses ``return_7d >= WIN_THRESHOLD_PCT`` as the win definition and
    excludes OPEN trades.  Both ``alert_tracker --stats`` and
    ``performance_analyzer`` should call this function.
    """
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                              AS total_closed,
            SUM(CASE WHEN pm.return_7d >= ? THEN 1 ELSE 0 END)   AS wins,
            ROUND(
                100.0 * SUM(CASE WHEN pm.return_7d >= ? THEN 1 ELSE 0 END)
                / MAX(COUNT(*), 1), 1
            )                                                     AS win_rate_pct
        FROM alerts a
        JOIN performance_metrics pm ON a.alert_uuid = pm.alert_uuid
        WHERE pm.status IN ('success', 'failure', 'expired')
        """,
        (WIN_THRESHOLD_PCT, WIN_THRESHOLD_PCT),
    ).fetchone()

    if row is None:
        return {"total_closed": 0, "wins": 0, "win_rate_pct": 0.0}

    return {
        "total_closed": row[0] or 0,
        "wins": row[1] or 0,
        "win_rate_pct": row[2] or 0.0,
    }


def canonical_pnl(conn: sqlite3.Connection) -> float:
    """Return the total realised P&L (%) summed across all closed trades.

    Uses ``return_7d`` as the P&L proxy (percentage return on each trade).
    Excludes OPEN trades.  Returns 0.0 when there are no closed trades.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pm.return_7d), 0.0)
        FROM alerts a
        JOIN performance_metrics pm ON a.alert_uuid = pm.alert_uuid
        WHERE pm.status IN ('success', 'failure', 'expired')
          AND pm.return_7d IS NOT NULL
        """
    ).fetchone()
    return float(row[0]) if row else 0.0


def trade_outcome_stats(conn: sqlite3.Connection) -> dict:
    """Return granular per-outcome statistics for closed trades.

    Returns:
        {
            "total_closed":   int,    # all closed trades
            "wins":           int,    # return_7d >= WIN_THRESHOLD_PCT
            "losses":         int,    # return_7d <  0
            "breakeven":      int,    # 0 <= return_7d < WIN_THRESHOLD_PCT
            "win_rate_pct":   float,  # wins / total_closed * 100
            "avg_win_pct":    float,  # mean return_7d on winning trades
            "avg_loss_pct":   float,  # mean return_7d on losing trades (negative)
            "profit_factor":  float,  # sum of wins / abs(sum of losses); 0 when no losses
        }
    """
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                                           AS total_closed,
            SUM(CASE WHEN pm.return_7d >= ?  THEN 1 ELSE 0 END)               AS wins,
            SUM(CASE WHEN pm.return_7d <  0  THEN 1 ELSE 0 END)               AS losses,
            SUM(CASE WHEN pm.return_7d >= 0 AND pm.return_7d < ? THEN 1 ELSE 0 END) AS breakeven,
            ROUND(
                100.0 * SUM(CASE WHEN pm.return_7d >= ? THEN 1 ELSE 0 END)
                / MAX(COUNT(*), 1), 1
            )                                                                  AS win_rate_pct,
            ROUND(
                AVG(CASE WHEN pm.return_7d >= ? THEN pm.return_7d END), 2
            )                                                                  AS avg_win_pct,
            ROUND(
                AVG(CASE WHEN pm.return_7d <  0 THEN pm.return_7d END), 2
            )                                                                  AS avg_loss_pct,
            ROUND(
                SUM(CASE WHEN pm.return_7d >= ? THEN pm.return_7d ELSE 0 END)
                / NULLIF(ABS(SUM(CASE WHEN pm.return_7d < 0 THEN pm.return_7d ELSE 0 END)), 0),
                2
            )                                                                  AS profit_factor
        FROM alerts a
        JOIN performance_metrics pm ON a.alert_uuid = pm.alert_uuid
        WHERE pm.status IN ('success', 'failure', 'expired')
          AND pm.return_7d IS NOT NULL
        """,
        (
            WIN_THRESHOLD_PCT,
            WIN_THRESHOLD_PCT,
            WIN_THRESHOLD_PCT,
            WIN_THRESHOLD_PCT,
            WIN_THRESHOLD_PCT,
        ),
    ).fetchone()

    if row is None or row[0] == 0:
        return {
            "total_closed": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate_pct": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "profit_factor": 0.0,
        }

    return {
        "total_closed": row[0] or 0,
        "wins":         row[1] or 0,
        "losses":       row[2] or 0,
        "breakeven":    row[3] or 0,
        "win_rate_pct": row[4] or 0.0,
        "avg_win_pct":  row[5] or 0.0,
        "avg_loss_pct": row[6] or 0.0,
        "profit_factor": row[7] or 0.0,
    }
