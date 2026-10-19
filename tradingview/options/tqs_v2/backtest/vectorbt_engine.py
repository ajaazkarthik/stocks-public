"""
VectorBTEngine — portfolio-level backtest analysis for TQS v2 results.

Loads all completed contracts from the DB (is_winner IS NOT NULL) and
computes win-rate statistics, factor Spearman correlations, and
grade-level breakdowns.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Resolve _apply_migrations regardless of how this module is imported
try:
    from ..persistence.db import _apply_migrations as _apply_db_migrations
except ImportError:
    from tradingview.options.tqs_v2.persistence.db import _apply_migrations as _apply_db_migrations

_log = logging.getLogger(__name__)

# Optional: scipy for Spearman rank correlation
try:
    from scipy.stats import spearmanr  # type: ignore
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False
    _log.warning("scipy not installed — Spearman correlations unavailable")

# Optional: vectorbt for equity-curve stats
try:
    import vectorbt as vbt  # type: ignore  # noqa: F401
    import numpy as np  # type: ignore
    _VBT_OK = True
except ImportError:
    _VBT_OK = False
    _log.warning("vectorbt not installed — equity curve stats unavailable")

# Factor columns to correlate with outcome (pnl_pct / is_winner)
FACTOR_FIELDS = [
    "total_score",
    "liquidity_score",
    "iv_score",
    "probability_score",
    "risk_reward_score",
    "oi_score",
    "spread_score",
    "volume_score",
    "iv_level_score",
    "iv_spread_score",
    "term_slope_score",
    "delta_score",
    "dte_score",
    "moneyness_score",
    "breakeven_score",
    "theo_score",
    "win_rate_score",
    "pop_score",
    "iv_pct_adj",
    "iv_rank_proxy",
    "iv_analysis_score",
    "volatility_conditions_score",
    "pcr_score",
    "skew_score",
    "premium_efficiency_score",
    "tv_score",
    "spread_eff_score",
    "be_score",
    "be_1sigma_score",
    "delta_iv_score",
    "leverage_score",
    "stock_trend_score",
    "stock_adx_score",
]


@dataclass
class BacktestResult:
    """Summary of backtest analysis."""

    total_contracts: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    median_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0   # average P&L % for winning contracts (positive)
    avg_loss_pct: float = 0.0  # average P&L % for losing contracts (typically negative)
    total_calls: int = 0
    calls_win_rate: float = 0.0
    total_puts: int = 0
    puts_win_rate: float = 0.0

    # factor → (spearman_r, p_value)
    factor_correlations: dict[str, tuple[float, float]] = field(default_factory=dict)

    # grade → {"count": N, "winners": M, "win_rate": pct, "avg_pnl": x}
    grade_stats: dict[str, dict] = field(default_factory=dict)

    # Raw rows for reporter
    rows: list[dict] = field(default_factory=list)

    # vectorbt equity curve stats (optional)
    vbt_stats: dict = field(default_factory=dict)


class VectorBTEngine:
    """Runs the backtest analysis against completed TQS v2 contracts.

    Usage::

        engine = VectorBTEngine(db_path)
        result = engine.run()
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Ensure all new columns exist in pre-Task-2 databases (idempotent)
        _apply_db_migrations(self._conn)

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> BacktestResult:
        """Load completed rows and produce a BacktestResult."""
        rows = self._load_completed_rows()
        if not rows:
            _log.info("VectorBTEngine: no completed contracts found")
            return BacktestResult()

        result = BacktestResult()
        result.rows = rows
        result.total_contracts = len(rows)

        winners = [r for r in rows if r.get("is_winner") == 1]
        losers  = [r for r in rows if r.get("is_winner") == 0]
        result.winners = len(winners)
        result.losers  = len(losers)
        result.win_rate = (
            round(len(winners) / result.total_contracts * 100, 1)
            if result.total_contracts else 0.0
        )

        pnl_vals = [r["pnl_pct"] for r in rows if r.get("pnl_pct") is not None]
        if pnl_vals:
            result.avg_pnl_pct    = round(sum(pnl_vals) / len(pnl_vals), 2)
            result.median_pnl_pct = round(self._median(pnl_vals), 2)

        # Avg P&L for winners vs losers separately (key diagnostic metrics)
        win_pnls  = [r["pnl_pct"] for r in winners if r.get("pnl_pct") is not None]
        loss_pnls = [r["pnl_pct"] for r in losers  if r.get("pnl_pct") is not None]
        if win_pnls:
            result.avg_win_pct  = round(sum(win_pnls)  / len(win_pnls),  2)
        if loss_pnls:
            result.avg_loss_pct = round(sum(loss_pnls) / len(loss_pnls), 2)

        # Calls vs puts breakdown
        calls = [r for r in rows if (r.get("option_type") or "").upper().startswith("C")]
        puts  = [r for r in rows if (r.get("option_type") or "").upper().startswith("P")]
        result.total_calls    = len(calls)
        result.total_puts     = len(puts)
        result.calls_win_rate = self._win_rate(calls)
        result.puts_win_rate  = self._win_rate(puts)

        # Grade stats
        result.grade_stats = self._compute_grade_stats(rows)

        # Factor correlations
        if _SCIPY_OK:
            result.factor_correlations = self._compute_correlations(rows)
        else:
            result.factor_correlations = {}

        # vectorbt equity curve
        if _VBT_OK and pnl_vals:
            result.vbt_stats = self._run_vbt(pnl_vals)

        _log.info(
            "VectorBTEngine: %d contracts | WR=%.1f%% | avg_pnl=%.2f%%",
            result.total_contracts, result.win_rate, result.avg_pnl_pct,
        )
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_completed_rows(self) -> list[dict]:
        """Return all rows where is_winner IS NOT NULL as dicts.

        Also back-fills null sub-score fields from raw_contract_json for rows
        that were stored before the sub-score columns were added to the schema.
        """
        cols = (
            ["id", "symbol", "option_type", "expiration", "strike", "mid", "dte",
             "iv", "delta", "grade", "total_score", "is_winner", "pnl_pct",
             "expiry_stock_price", "intrinsic_value_at_expiry",
             "liquidity_score", "iv_score", "probability_score", "risk_reward_score",
             "current_premium", "outcome_fetched_at", "raw_contract_json"]
            + FACTOR_FIELDS
        )
        # Build a deduplicated column list preserving order
        seen: set[str] = set()
        uniq_cols = []
        for c in cols:
            if c not in seen:
                uniq_cols.append(c)
                seen.add(c)

        # Only select columns that actually exist in the DB
        existing = self._existing_columns()
        select_cols = [c for c in uniq_cols if c in existing]
        if not select_cols:
            return []

        sql = (
            f"SELECT {', '.join(select_cols)} FROM tqs_v2_results "
            f"WHERE is_winner IS NOT NULL "
            f"ORDER BY total_score DESC"
        )
        raw = self._conn.execute(sql).fetchall()
        rows = [dict(r) for r in raw]

        # Back-fill null sub-score columns from raw_contract_json for legacy rows
        # (rows inserted before sub-score columns existed have NULL for all of them)
        _sub_score_fields = set(FACTOR_FIELDS) - {"total_score"}
        for row in rows:
            # Skip rows that already have at least some sub-scores populated
            if any(row.get(f) is not None for f in _sub_score_fields):
                continue
            raw_json = row.get("raw_contract_json")
            if not raw_json:
                continue
            try:
                import json as _json
                blob = _json.loads(raw_json)
                for f in _sub_score_fields:
                    if row.get(f) is None and blob.get(f) is not None:
                        row[f] = blob[f]
            except (ValueError, TypeError):
                pass

        return rows

    def _existing_columns(self) -> set[str]:
        """Return set of column names in tqs_v2_results."""
        rows = self._conn.execute(
            "PRAGMA table_info(tqs_v2_results)"
        ).fetchall()
        return {r["name"] for r in rows}

    def _compute_grade_stats(self, rows: list[dict]) -> dict[str, dict]:
        from collections import defaultdict
        by_grade: dict[str, list] = defaultdict(list)
        for r in rows:
            by_grade[r.get("grade") or "?"].append(r)

        stats: dict[str, dict] = {}
        for grade in sorted(by_grade):
            group = by_grade[grade]
            wins = [r for r in group if r.get("is_winner") == 1]
            pnls = [r["pnl_pct"] for r in group if r.get("pnl_pct") is not None]
            stats[grade] = {
                "count":    len(group),
                "winners":  len(wins),
                "win_rate": round(len(wins) / len(group) * 100, 1) if group else 0.0,
                "avg_pnl":  round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
            }
        return stats

    def _compute_correlations(
        self, rows: list[dict]
    ) -> dict[str, tuple[float, float]]:
        """Compute Spearman r between each factor and is_winner."""
        outcomes = [r.get("is_winner") for r in rows]
        if all(v is None for v in outcomes):
            return {}

        result: dict[str, tuple[float, float]] = {}
        for factor in FACTOR_FIELDS:
            factor_vals = [r.get(factor) for r in rows]
            # Pair-wise drop None
            pairs = [
                (f, o)
                for f, o in zip(factor_vals, outcomes)
                if f is not None and o is not None
            ]
            if len(pairs) < 5:
                continue
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            try:
                r_val, p_val = spearmanr(xs, ys)
                result[factor] = (round(float(r_val), 4), round(float(p_val), 4))
            except Exception as exc:
                _log.debug("Spearman error for %s: %s", factor, exc)
        return result

    def _run_vbt(self, pnl_pcts: list[float]) -> dict:
        """Run a simple vectorbt equity curve on pnl_pct values."""
        try:
            # Simulate: start with 1.0, apply each trade's return
            equity = [1.0]
            for p in pnl_pcts:
                equity.append(equity[-1] * (1 + p / 100))
            equity_series = np.array(equity[1:], dtype=float)
            pf = vbt.Portfolio.from_holding(
                equity_series,
                freq="1 day",
            )
            stats = pf.stats()
            return {k: (float(v) if hasattr(v, "__float__") else str(v))
                    for k, v in stats.items()}
        except Exception as exc:
            _log.debug("vectorbt stats error: %s", exc)
            return {}

    @staticmethod
    def _win_rate(rows: list[dict]) -> float:
        if not rows:
            return 0.0
        wins = sum(1 for r in rows if r.get("is_winner") == 1)
        return round(wins / len(rows) * 100, 1)

    @staticmethod
    def _median(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "VectorBTEngine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
