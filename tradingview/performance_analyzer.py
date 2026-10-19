"""
Performance Analyzer — Data-driven signal improvement via alert_tracker.db
===========================================================================
Queries the alert_tracker SQLite database and produces:

1. Win rate by parameter bucket (L1–L4, composite, win_rate_at_fire, VIX)
2. Source reliability (win_rate, avg_return, avg_MAE, avg_MFE per input_source)
3. Market regime filter (bull/bear/neutral/vix-spike success rates)
4. Failure pattern detection (low L2 + high VIX → failure predictors)
5. HTML report with: parameter importance table, source quality, regime breakdown,
   suggested threshold adjustments, rolling 30-day win rate time-series

CLI:
    python performance_analyzer.py --min-samples 20
    python performance_analyzer.py --since 2026-01-01
    python performance_analyzer.py --out reports/performance_analysis.html
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote as _url_quote

# ── Windows: force UTF-8 console ──────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ─────────────────────────────────────────────────────────────────────
_THIS    = Path(__file__).parent                     # tradingview/
_ROOT    = _THIS.parent
_DB_PATH = _ROOT / "database" / "alert_tracker.db"
_OUT_DEFAULT = _THIS / "reports" / "performance_analysis.html"

try:
    from path_config import ensure_paths as _ensure_paths
    _ensure_paths(_THIS)
except ImportError:
    sys.path.insert(0, str(_THIS))

try:
    from log_config import get_logger as _get_logger
except ImportError:
    import logging as _logging_fb
    _get_logger = lambda n: _logging_fb.getLogger(n)
_log = _get_logger("performance_analyzer")

# ── scipy (optional — graceful fallback) ──────────────────────────────────────
try:
    from scipy.stats import spearmanr as _spearmanr  # type: ignore
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False

# ── alert_tracker (optional — for SUCCESS_RETURN_PCT constant) ───────────────
try:
    from alert_tracker import SUCCESS_RETURN_PCT, FAILURE_DRAWDOWN_PCT
except ImportError:
    SUCCESS_RETURN_PCT   = 5.0
    FAILURE_DRAWDOWN_PCT = -5.0

# ── canonical win rate (shared definition — database/queries.py) ─────────────
try:
    sys.path.insert(0, str(_ROOT))
    from database.queries import canonical_win_rate as _canonical_win_rate
    _CANONICAL_OK = True
except ImportError:
    _CANONICAL_OK = False

# Human-readable names for alerts.source values
_SOURCE_NAMES: dict[str, str] = {
    "screener": "TV Screener Alerts",
    "download": "TV Alerts Download",
    "news":     "News Screener Alerts",
    "manual":   "Manual Entry",
}

_WRAPPED_SYMBOL_RE = re.compile(r'"symbol"\s*:\s*"([^"]+)"')


def _normalise_tv_symbol(value: Any) -> str:
    """Extract a plain TV symbol from wrapped scanner payload strings."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    candidate = raw[1:].strip() if raw.startswith("=") else raw
    if candidate.startswith("{"):
        try:
            parsed = json.loads(candidate)
            symbol = parsed.get("symbol")
            if symbol:
                return str(symbol).strip()
        except Exception:
            pass

    match = _WRAPPED_SYMBOL_RE.search(candidate)
    if match:
        return match.group(1).strip()

    return raw.strip().strip('"')


def _clean_yf_ticker(value: Any) -> str:
    """Return stored yfinance ticker unless it is a wrapped TV payload fragment."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw[1:].strip() if raw.startswith("=") else raw
    if candidate.startswith("{") or _WRAPPED_SYMBOL_RE.search(candidate):
        return ""
    return raw.strip('"')


def _normalise_row_symbols(rows: list[dict]) -> list[dict]:
    """Clean legacy wrapped TV symbols before report analysis/rendering."""
    for row in rows:
        display_symbol = _normalise_tv_symbol(row.get("tv_symbol") or row.get("ticker") or "")
        if display_symbol:
            row["tv_symbol"] = display_symbol
            row["ticker"] = display_symbol
        row["yf_ticker"] = _clean_yf_ticker(row.get("yf_ticker"))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def _db_rows(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a read-only query, return list of row dicts."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=20, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        print(f"[perf_analyzer] DB error: {exc}", flush=True)
        return []
    finally:
        conn.close()


def _table_exists(db_path: Path, table: str) -> bool:
    rows = _db_rows(db_path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return bool(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def _is_priceable(tv_symbol: str, yf_ticker: str = "") -> bool:
    """
    Return True if we expect to be able to fetch a price for this symbol via
    yfinance OR the TV scanner fallback (_TV_SCANNER_AT in alert_tracker).
    """
    tv_symbol = _normalise_tv_symbol(tv_symbol)
    yf_ticker = _clean_yf_ticker(yf_ticker)
    if yf_ticker:
        return True  # stored mapping — assume priceable
    if ":" not in tv_symbol:
        return True  # bare ticker — assume US
    exchange = tv_symbol.split(":", 1)[0].upper()
    # Import the exchange maps from alert_tracker if available, else use inline copies.
    try:
        import sys
        _tv_dir = Path(__file__).parent
        if str(_tv_dir) not in sys.path:
            sys.path.insert(0, str(_tv_dir))
        from alert_tracker import _EXCHANGE_YF_SUFFIX, _TV_SCANNER_AT  # type: ignore
        suffix = _EXCHANGE_YF_SUFFIX.get(exchange)
        if suffix is not None:  # yfinance supports this exchange
            return True
        # TV scanner is a secondary price source for non-yfinance exchanges
        return exchange in _TV_SCANNER_AT
    except Exception:
        _FALLBACK_YF: dict[str, str | None] = {
            "NASDAQ": "", "NYSE": "", "AMEX": "", "BATS": "", "OTC": "",
            "CBOE": "", "NYSE MKT": "", "NYSEARCA": "",
            "TSX": ".TO", "TSXV": ".V", "CSE": ".CN",
            "ASX": ".AX", "LSE": ".L", "HKEX": ".HK", "TSE": ".T",
            "NSE": None, "BSE": None,
            "CME": None, "CBOT": None, "NYMEX": None, "MCX": None,
            "HSI": None, "SZSE": None, "SSE": None,
            "EURONEXT": ".PA", "XETR": ".DE", "XETRA": ".DE",
            "SIX": ".SW", "BME": ".MC", "BMFBOVESPA": ".SA",
            "TASE": None, "MYX": None,
        }
        # _FALLBACK_TV: only tradable exchanges with no yfinance suffix
        _FALLBACK_TV = {
            "NSE", "BSE",   # India — TV scanner is the price source
        }
        suffix = _FALLBACK_YF.get(exchange)
        if suffix is not None:
            return True
        return exchange in _FALLBACK_TV
    # If exchange is not in any map, we don't know — treat as not priceable.
    return False



def _load_outcomes(
    db_path: Path,
    since: str | None = None,
    tradable_only: bool = False,
) -> list[dict]:
    """
    Join signal_engine_outputs with price_evolution to get outcomes per signal.
    Falls back to alerts + performance_metrics if signal_engine_outputs is empty.

    Parameters
    ----------
    tradable_only : if True, restrict to rows where tradable = 1
                   (filters out futures, unsupported foreign exchanges, etc.)
    """
    tradable_clause = "AND seo.tradable = 1" if tradable_only else ""
    if _table_exists(db_path, "signal_engine_outputs"):
        date_filter = f"AND seo.timestamp >= '{since}'" if since else ""
        rows = _db_rows(db_path, f"""
            WITH latest_model AS (
                SELECT json_extract(parameters_json, '$.model_version') AS model_version
                FROM signal_engine_outputs
                WHERE json_extract(parameters_json, '$.model_version') IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 1
            ),
            ranked_evolution AS (
                SELECT pe.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY pe.output_id
                           ORDER BY ABS(pe.hours_elapsed - 168.0)
                       ) AS rn
                FROM price_evolution pe
                WHERE pe.hours_elapsed BETWEEN 144.0 AND 192.0
                  AND pe.return_pct IS NOT NULL
            )
            SELECT
                seo.id                AS output_id,
                seo.ticker,
                seo.tv_symbol,
                seo.timestamp,
                seo.price_at_signal   AS entry_price,
                seo.composite_score   AS composite,
                seo.l1_score          AS l1,
                seo.l2_score          AS l2,
                seo.l3_score          AS l3,
                seo.l4_score          AS l4,
                seo.win_rate          AS win_rate_at_fire,
                seo.vix_at_signal     AS vix_at_fire,
                seo.market_regime,
                seo.input_source,
                seo.tradable,
                pe.return_pct,
                pe.max_favorable      AS mfe,
                pe.max_adverse        AS mae,
                CASE
                    WHEN pe.return_pct >= {SUCCESS_RETURN_PCT} THEN 'success'
                    WHEN pe.return_pct <= {FAILURE_DRAWDOWN_PCT} THEN 'failure'
                    WHEN pe.return_pct IS NOT NULL THEN 'open'
                    ELSE NULL
                END AS outcome,
                json_extract(seo.parameters_json, '$.model_version') AS model_version,
                json_extract(seo.parameters_json, '$.quality_score') AS quality_score
            FROM signal_engine_outputs seo
            LEFT JOIN ranked_evolution pe ON pe.output_id = seo.id AND pe.rn = 1
            WHERE json_extract(seo.parameters_json, '$.model_version') =
                  (SELECT model_version FROM latest_model)
              {date_filter} {tradable_clause}
            ORDER BY seo.timestamp DESC
        """)
        if rows:
            return _normalise_row_symbols(rows)

    # Fallback: use existing alerts + performance_metrics tables.
    # BUG NOTE (2026-04-22): pm.status='success'/'failure' rows are frequently
    # ORPHANED — their alert_uuid no longer exists in the alerts table after
    # old records are pruned, so the JOIN returns 0 rows for those statuses.
    # FIX: classify outcome from return thresholds directly in SQL so any row
    # with actual return data (return_7d != NULL) gets a meaningful outcome.
    tradable_clause_a = "AND a.tradable = 1" if tradable_only else ""
    date_filter = f"AND a.fire_time >= '{since}'" if since else ""
    # Build the COALESCE return expression once to avoid repetition in the CASE
    _ret = "COALESCE(pm.return_7d, pm.return_3d, pm.return_1d, pm.return_4h, pm.return_1h)"
    _vix = "COALESCE(a.vix_at_fire, CAST(json_extract(a.result_json, '$.vix_level') AS REAL))"
    rows = _db_rows(db_path, f"""
        SELECT
            a.id              AS output_id,
            a.tv_symbol       AS ticker,
            a.tv_symbol,
            a.fire_time       AS timestamp,
            a.fire_time       AS fire_time,
            a.entry_price     AS entry_price,
            a.yf_ticker       AS yf_ticker,
            a.composite_score AS composite,
            a.l1_score        AS l1,
            a.l2_score        AS l2,
            a.l3_score        AS l3,
            a.l4_score        AS l4,
            a.win_rate        AS win_rate_at_fire,
            -- vix_at_fire: use column first, fall back to result_json field
            {_vix}            AS vix_at_fire,
            -- market_regime: use column first; derive from VIX if NULL
            COALESCE(
                a.market_regime,
                CASE
                    WHEN CAST(json_extract(a.result_json,'$.vix_level') AS REAL) > 30
                        THEN 'High-Vol (VIX>30)'
                    WHEN CAST(json_extract(a.result_json,'$.vix_level') AS REAL) > 20
                        THEN 'Normal (VIX 20-30)'
                    WHEN CAST(json_extract(a.result_json,'$.vix_level') AS REAL) IS NOT NULL
                        THEN 'Low-Vol (VIX<20)'
                    ELSE 'Unknown'
                END
            )                 AS market_regime,
            a.source          AS input_source,
            COALESCE(a.tradable, 1) AS tradable,
            -- return_pct = most recent snapshot return (T+7d → T+3d → T+1d → T+4h → T+1h)
            {_ret}            AS return_pct,
            pm.max_return     AS mfe,
            pm.max_drawdown   AS mae,
            pm.return_1h      AS ret_1h,
            pm.return_4h      AS ret_4h,
            pm.return_1d      AS ret_1d,
            pm.return_3d      AS ret_3d,
            pm.return_7d      AS ret_7d,
            pm.computed_at    AS computed_at,
            pm.hit_target     AS hit_target,
            pm.hit_stop       AS hit_stop,
            -- outcome: classify from return thresholds — avoids orphaned pm.status rows
            CASE
                WHEN {_ret} >= {SUCCESS_RETURN_PCT}   THEN 'success'
                WHEN {_ret} <= {FAILURE_DRAWDOWN_PCT} THEN 'failure'
                WHEN {_ret} IS NOT NULL               THEN 'open'
                ELSE 'open'
            END               AS outcome
        FROM   alerts a
        LEFT JOIN performance_metrics pm ON pm.alert_uuid = a.alert_uuid
        WHERE  1=1 {date_filter} {tradable_clause_a}
        ORDER  BY a.fire_time DESC
    """)
    return _normalise_row_symbols(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Analysis routines
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _quintile_buckets(values: list[float]) -> list[tuple[float, float]]:
    """Return 5 (lo, hi] bucket boundaries for the given values."""
    if not values:
        return []
    s = sorted(values)
    n = len(s)
    idx = [int(n * p / 5) for p in range(6)]
    idx[-1] = n - 1
    return [(s[idx[i]], s[idx[i + 1]]) for i in range(5)]


def _success_rate(rows: list[dict]) -> float | None:
    closed = [r for r in rows if r.get("outcome") in ("success", "failure")]
    if not closed:
        return None
    wins = sum(1 for r in closed if r.get("outcome") == "success")
    return round(wins / len(closed) * 100, 1)


def _avg(vals: list[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 2) if v else None


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman correlation — uses scipy if available, else fallback."""
    if len(xs) < 5:
        return None
    if _SCIPY_OK:
        try:
            r, _ = _spearmanr(xs, ys)
            return round(float(r), 4) if not math.isnan(r) else None
        except Exception as exc:
            _log.debug("scipy_spearman_failed", error=str(exc))
    # Fallback: rank-based Pearson (simple)
    def _rank(arr: list[float]) -> list[float]:
        s = sorted(enumerate(arr), key=lambda x: x[1])
        ranks = [0.0] * len(arr)
        for rank, (i, _) in enumerate(s, 1):
            ranks[i] = float(rank)
        return ranks
    try:
        rx = _rank(xs)
        ry = _rank(ys)
        mx = sum(rx) / len(rx)
        my = sum(ry) / len(ry)
        num   = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        denom = (sum((a - mx) ** 2 for a in rx) ** 0.5 *
                 sum((b - my) ** 2 for b in ry) ** 0.5)
        return round(num / denom, 4) if denom else None
    except Exception:
        return None


def parameter_importance(rows: list[dict], min_samples: int = 5) -> list[dict]:
    """
    Compute Spearman correlation between each L-score / composite and success outcome.
    Returns ranked list: [{param, correlation, direction, sample_count}]
    """
    params = ["l1", "l2", "l3", "l4", "composite", "win_rate_at_fire", "vix_at_fire"]
    results = []
    for p in params:
        xs = []
        ys = []
        for r in rows:
            v = _safe_float(r.get(p))
            outcome_v = 1.0 if r.get("outcome") == "success" else 0.0 if r.get("outcome") == "failure" else None
            if v is not None and outcome_v is not None:
                xs.append(v)
                ys.append(outcome_v)
        if len(xs) < min_samples:
            continue
        corr = _spearman(xs, ys)
        results.append({
            "param":        p,
            "correlation":  corr,
            "direction":    "positive" if (corr or 0) > 0 else "negative",
            "sample_count": len(xs),
        })
    results.sort(key=lambda x: abs(x.get("correlation") or 0), reverse=True)
    return results


def bucket_analysis(rows: list[dict], param: str, min_samples: int = 5) -> list[dict]:
    """
    Split rows into quintile buckets for ``param``, compute success rate per bucket.
    Returns list of bucket dicts sorted by bucket low value.
    """
    vals = [_safe_float(r.get(param)) for r in rows]
    vals_notnone = [v for v in vals if v is not None]
    if len(vals_notnone) < min_samples:
        return []

    buckets = _quintile_buckets(vals_notnone)
    out = []
    for lo, hi in buckets:
        bucket_rows = [
            r for r, v in zip(rows, vals)
            if v is not None and lo <= v <= hi
        ]
        if len(bucket_rows) < min_samples:
            continue
        wr = _success_rate(bucket_rows)
        closed_b = [r for r in bucket_rows if r.get("outcome") in ("success", "failure")]
        avg_mfe, _ = _avg_capped([_safe_float(r.get("mfe")) for r in closed_b])
        avg_mae, _ = _avg_capped([_safe_float(r.get("mae")) for r in closed_b])
        out.append({
            "range":        f"{lo:.1f}–{hi:.1f}",
            "lo":           lo,
            "hi":           hi,
            "count":        len(bucket_rows),
            "success_rate": wr,
            "avg_mfe":      avg_mfe,
            "avg_mae":      avg_mae,
        })
    return out


# Max return magnitude considered plausible for display/avg purposes.
# Values beyond this indicate a wrong-ticker artefact (e.g. TSX:IAU entry price
# was the TSX stock price but yfinance fetched AMEX:IAU instead).
_MAX_SANE_DISPLAY_PCT = 200.0


def _avg_capped(vals: list[float | None], cap: float = _MAX_SANE_DISPLAY_PCT) -> tuple[float | None, int]:
    """
    Average of values, ignoring None and capping outliers.
    Returns (average, outliers_excluded_count).
    """
    clean = [x for x in vals if x is not None and abs(x) <= cap]
    skipped = sum(1 for x in vals if x is not None and abs(x) > cap)
    return (round(sum(clean) / len(clean), 2) if clean else None), skipped


def source_reliability(rows: list[dict], min_samples: int = 3) -> list[dict]:
    """Group by input_source, compute win_rate, avg_return, avg_MAE, avg_MFE."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        src = r.get("input_source") or "unknown"
        groups.setdefault(src, []).append(r)

    out = []
    for src, g in sorted(groups.items()):
        if len(g) < min_samples:
            continue
        # Use only CLOSED signals for return/mfe/mae averages — open signals
        # only have tiny 1h/4h snapshots which dilute the true performance signal.
        closed_g = [r for r in g if r.get("outcome") in ("success", "failure")]
        avg_ret, outliers = _avg_capped([_safe_float(r.get("return_pct")) for r in closed_g])
        avg_mfe, _        = _avg_capped([_safe_float(r.get("mfe")) for r in closed_g])
        avg_mae, _        = _avg_capped([_safe_float(r.get("mae")) for r in closed_g])
        out.append({
            "source":          src,                               # raw key for row lookups
            "source_display":  _SOURCE_NAMES.get(src, src),      # human-readable label
            "count":           len(g),
            "closed_count":    len(closed_g),
            "win_rate":        _success_rate(g),
            "avg_return":      avg_ret,
            "avg_mfe":         avg_mfe,
            "avg_mae":         avg_mae,
            "outliers_skipped": outliers,
        })
    out.sort(key=lambda x: x.get("win_rate") or 0, reverse=True)
    return out


def regime_analysis(rows: list[dict], min_samples: int = 3) -> list[dict]:
    """Group by market_regime, compare success rates."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        reg = r.get("market_regime") or "unknown"
        groups.setdefault(reg, []).append(r)

    out = []
    for reg, g in sorted(groups.items()):
        if len(g) < min_samples:
            continue
        # Avg return from closed trades only — open snapshots are tiny/misleading
        closed_g = [r for r in g if r.get("outcome") in ("success", "failure")]
        avg_ret, _ = _avg_capped([_safe_float(r.get("return_pct")) for r in closed_g])
        out.append({
            "regime":       reg,
            "count":        len(g),
            "closed_count": len(closed_g),
            "win_rate":     _success_rate(g),
            "avg_return":   avg_ret,
        })
    out.sort(key=lambda x: x.get("win_rate") or 0, reverse=True)
    return out


def failure_patterns(rows: list[dict]) -> list[str]:
    """
    Identify parameter combinations that predict failure.
    Returns human-readable strings like "l2_score < 3 AND vix > 30 → high failure".
    """
    patterns: list[str] = []

    # L2 low + high VIX
    for l2_thr in [3, 5]:
        for vix_thr in [25, 30, 35]:
            bucket = [
                r for r in rows
                if (_safe_float(r.get("l2")) or 99) < l2_thr
                and (_safe_float(r.get("vix_at_fire")) or 0) > vix_thr
            ]
            if len(bucket) >= 5:
                wr = _success_rate(bucket)
                if wr is not None and wr < 30:
                    patterns.append(
                        f"l2_score < {l2_thr} AND vix > {vix_thr} → "
                        f"{wr:.0f}% win rate ({len(bucket)} samples)"
                    )

    # Low composite
    for comp_thr in [40, 50, 60]:
        bucket = [r for r in rows if (_safe_float(r.get("composite")) or 999) < comp_thr]
        if len(bucket) >= 5:
            wr = _success_rate(bucket)
            if wr is not None and wr < 35:
                patterns.append(
                    f"composite < {comp_thr} → {wr:.0f}% win rate ({len(bucket)} samples)"
                )

    return sorted(set(patterns))


def suggested_thresholds(rows: list[dict]) -> list[str]:
    """
    Suggest threshold adjustments based on bucket win rates.
    """
    suggestions: list[str] = []

    # Find best composite threshold where win rate jumps
    comp_vals = [_safe_float(r.get("composite")) for r in rows if _safe_float(r.get("composite")) is not None]
    if len(comp_vals) >= 10:
        for thr in [55, 60, 65, 70, 75]:
            above = [r for r in rows if (_safe_float(r.get("composite")) or 0) >= thr]
            below = [r for r in rows if (_safe_float(r.get("composite")) or 0) < thr]
            wr_above = _success_rate(above)
            wr_below = _success_rate(below)
            if wr_above and wr_below and len(above) >= 5 and len(below) >= 5:
                diff = wr_above - wr_below
                if diff >= 10:
                    suggestions.append(
                        f"Raise composite threshold to {thr} — signals above {thr} "
                        f"have {wr_above:.0f}% win rate vs {wr_below:.0f}% below "
                        f"({len(above)} / {len(below)} samples)"
                    )
                    break   # report first meaningful threshold

    # Source with poor win rate
    for s in source_reliability(rows, min_samples=5):
        wr = s.get("win_rate")
        if wr is not None and wr < 30:
            suggestions.append(
                f"Consider disabling source '{s['source']}' — "
                f"{wr:.0f}% win rate over {s['count']} samples"
            )

    return suggestions


def rolling_winrate(rows: list[dict], window_days: int = 30) -> list[dict]:
    """
    Compute rolling 30-day win rate, sorted by date.
    Returns list of {date, win_rate, count}.
    """
    closed = [
        r for r in rows
        if r.get("outcome") in ("success", "failure") and r.get("timestamp")
    ]
    if len(closed) < 5:
        return []

    closed.sort(key=lambda r: r["timestamp"])
    out = []
    for i, r in enumerate(closed):
        ts = r["timestamp"]
        try:
            dt = datetime.fromisoformat(ts[:19])
        except Exception:
            continue
        # Window: look back window_days from this row
        cutoff_ts = (dt - __import__("datetime").timedelta(days=window_days)).isoformat()
        window = [
            x for x in closed[:i + 1]
            if (x.get("timestamp") or "") >= cutoff_ts
        ]
        wr = _success_rate(window)
        out.append({
            "date":     ts[:10],
            "win_rate": wr,
            "count":    len(window),
        })
    return out


# ─── 6 New Analysis Sections ─────────────────────────────────────────────────

def resolution_analysis(rows: list[dict], min_samples: int = 3) -> list[dict]:
    """Win rate broken down by chart resolution (D / W / 4h / 1h etc.)."""
    groups: dict[str, list] = {}
    for r in rows:
        res = (r.get("resolution") or "unknown").strip()
        groups.setdefault(res, []).append(r)
    out = []
    for res, grp in sorted(groups.items()):
        closed = [x for x in grp if x.get("outcome") in ("success", "failure")]
        if len(closed) < min_samples:
            continue
        wr = _success_rate(closed)
        out.append({"resolution": res, "win_rate": wr, "total": len(grp), "closed": len(closed)})
    return sorted(out, key=lambda x: (x["win_rate"] is None, -(x["win_rate"] or 0)))


def holding_period_analysis(rows: list[dict], min_samples: int = 3) -> list[dict]:
    """Win rate by holding-period bucket (days held until outcome)."""
    buckets = {"< 3 days": [], "3–7 days": [], "1–2 weeks": [], "2–4 weeks": [], "> 4 weeks": []}
    for r in rows:
        if r.get("outcome") not in ("success", "failure"):
            continue
        try:
            ts_open  = r.get("created_at") or r.get("timestamp") or ""
            ts_close = r.get("closed_at") or ""
            if not ts_open or not ts_close:
                continue
            days = (datetime.fromisoformat(ts_close[:19]) - datetime.fromisoformat(ts_open[:19])).days
        except Exception:
            continue
        if days < 3:
            buckets["< 3 days"].append(r)
        elif days < 8:
            buckets["3–7 days"].append(r)
        elif days < 15:
            buckets["1–2 weeks"].append(r)
        elif days < 29:
            buckets["2–4 weeks"].append(r)
        else:
            buckets["> 4 weeks"].append(r)
    out = []
    for label, grp in buckets.items():
        if len(grp) < min_samples:
            continue
        wr = _success_rate(grp)
        out.append({"bucket": label, "win_rate": wr, "count": len(grp)})
    return out


def enricher_impact(rows: list[dict], min_samples: int = 3) -> list[dict]:
    """Compare win rate with vs without key enricher signals (AI, volume, news etc.)."""
    checks = [
        ("AI growth", lambda r: bool(r.get("growth_ai_outlook"))),
        ("High conviction", lambda r: (_safe_float(r.get("composite_score")) or 0) >= 70),
        ("Tradable", lambda r: bool(r.get("tradable"))),
        ("Has news", lambda r: bool(r.get("news_sentiment"))),
        ("ADX > 25", lambda r: (_safe_float(r.get("adx")) or 0) > 25),
        ("RSI 40–65", lambda r: 40 <= (_safe_float(r.get("rsi")) or -1) <= 65),
    ]
    out = []
    closed = [r for r in rows if r.get("outcome") in ("success", "failure")]
    for label, fn in checks:
        yes = [r for r in closed if fn(r)]
        no  = [r for r in closed if not fn(r)]
        if len(yes) < min_samples:
            continue
        out.append({
            "label":      label,
            "with_wr":    _success_rate(yes),
            "without_wr": _success_rate(no) if len(no) >= min_samples else None,
            "with_n":     len(yes),
            "without_n":  len(no),
        })
    return out


def signal_type_analysis(rows: list[dict], min_samples: int = 3) -> list[dict]:
    """Win rate by signal type / alert name."""
    groups: dict[str, list] = {}
    for r in rows:
        sig = (r.get("signal") or r.get("signal_type") or "unknown").strip()
        groups.setdefault(sig, []).append(r)
    out = []
    for sig, grp in groups.items():
        closed = [x for x in grp if x.get("outcome") in ("success", "failure")]
        if len(closed) < min_samples:
            continue
        wr = _success_rate(closed)
        out.append({"signal": sig, "win_rate": wr, "total": len(grp), "closed": len(closed)})
    return sorted(out, key=lambda x: (x["win_rate"] is None, -(x["win_rate"] or 0)))


def actionable_vs_all(rows: list[dict]) -> dict:
    """Compare win rate for actionable signals vs all signals."""
    closed      = [r for r in rows if r.get("outcome") in ("success", "failure")]
    actionable  = [r for r in closed if r.get("actionable")]
    non_action  = [r for r in closed if not r.get("actionable")]
    return {
        "actionable_wr":      _success_rate(actionable),
        "actionable_n":       len(actionable),
        "non_actionable_wr":  _success_rate(non_action),
        "non_actionable_n":   len(non_action),
        "total_closed":       len(closed),
    }


def input_indicators_analysis(rows: list[dict], min_samples: int = 3) -> list[dict]:
    """Correlation of individual L1-L4 component scores with trade outcome."""
    params = ["l1_score", "l2_score", "l3_score", "l4_score", "l5_score",
              "adx_at_entry", "rsi_at_entry"]
    closed = [r for r in rows if r.get("outcome") in ("success", "failure")]
    out = []
    for param in params:
        xs = [_safe_float(r.get(param)) for r in closed]
        ys = [1.0 if r.get("outcome") == "success" else 0.0 for r in closed]
        valid = [(x, y) for x, y in zip(xs, ys) if x is not None]
        if len(valid) < min_samples:
            continue
        vx, vy = zip(*valid)
        corr = _spearman(list(vx), list(vy))
        out.append({"param": param, "correlation": corr, "n": len(valid)})
    return sorted(out, key=lambda x: (x["correlation"] is None, -(abs(x["correlation"] or 0))))


# ─────────────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────────────

def _esc(v: Any) -> str:
    return str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _color_wr(wr: float | None) -> str:
    if wr is None: return "#94a3b8"
    if wr >= 55:   return "#22c55e"
    if wr < 40:    return "#ef4444"
    return "#f59e0b"


def _color_corr(c: float | None) -> str:
    if c is None: return "#94a3b8"
    if c > 0.2:   return "#22c55e"
    if c < -0.1:  return "#ef4444"
    return "#f59e0b"


def _spearman(xs: list, ys: list) -> float | None:
    """Simple Spearman rank correlation (no scipy required)."""
    n = len(xs)
    if n < 3:
        return None
    try:
        import warnings
        from scipy.stats import spearmanr
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r, _ = spearmanr(xs, ys)
        if r is None or (hasattr(r, "__float__") and __import__("math").isnan(float(r))):
            return None
        return round(float(r), 3)
    except Exception as exc:
        _log.debug("scipy_spearman_corr_failed", error=str(exc))
    # Fallback: Pearson on ranks
    def _rank(lst):
        s = sorted(enumerate(lst), key=lambda t: t[1])
        r = [0.0] * len(lst)
        for rank, (idx, _) in enumerate(s, 1):
            r[idx] = float(rank)
        return r
    rx, ry = _rank(xs), _rank(ys)
    mx = sum(rx) / n;  my = sum(ry) / n
    num   = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denom = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(num / denom, 3) if denom else None


def _html_table_2col(rows: list[dict], *, key_col: str, wr_col: str,
                     n_col: str, label: str) -> str:
    if not rows:
        return '<p class="note">Not enough data (min 3 closed signals per bucket).</p>'
    hdr = f"<th>{label}</th><th>Win Rate</th><th>Closed</th>"
    body = ""
    for r in rows:
        wr   = r.get(wr_col)
        wr_s = f"{wr:.1f}%" if wr is not None else "—"
        col  = "#22c55e" if (wr or 0) >= 60 else "#ef4444" if (wr or 100) < 45 else "#94a3b8"
        body += (f"<tr><td>{r.get(key_col,'?')}</td>"
                 f"<td style='color:{col};font-weight:600'>{wr_s}</td>"
                 f"<td>{r.get(n_col,0)}</td></tr>")
    return f"<table><thead><tr>{hdr}</tr></thead><tbody>{body}</tbody></table>"


def _html_enricher_table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="note">Not enough data for enricher comparison.</p>'
    hdr = "<th>Enricher Signal</th><th>With WR</th><th>N</th><th>Without WR</th><th>N</th>"
    body = ""
    for r in rows:
        wwr  = r.get("with_wr");    wwr_s = f"{wwr:.1f}%"  if wwr is not None else "—"
        nwr  = r.get("without_wr"); nwr_s = f"{nwr:.1f}%"  if nwr is not None else "—"
        wcol = "#22c55e" if (wwr or 0) >= 60 else "#ef4444" if (wwr or 100) < 45 else "#94a3b8"
        body += (f"<tr><td>{r['label']}</td>"
                 f"<td style='color:{wcol};font-weight:600'>{wwr_s}</td>"
                 f"<td>{r.get('with_n',0)}</td>"
                 f"<td>{nwr_s}</td><td>{r.get('without_n',0)}</td></tr>")
    return f"<table><thead><tr>{hdr}</tr></thead><tbody>{body}</tbody></table>"


def _html_actionable_card(d: dict) -> str:
    awr = d.get("actionable_wr");    awr_s = f"{awr:.1f}%" if awr is not None else "—"
    nwr = d.get("non_actionable_wr"); nwr_s = f"{nwr:.1f}%" if nwr is not None else "—"
    an  = d.get("actionable_n", 0)
    nn  = d.get("non_actionable_n", 0)
    return (f"<div class='card' style='display:flex;gap:2rem'>"
            f"<div><strong>Actionable signals</strong><br>"
            f"<span style='font-size:1.4rem;font-weight:700;color:#22c55e'>{awr_s}</span>"
            f"<br><small>n = {an}</small></div>"
            f"<div><strong>All other signals</strong><br>"
            f"<span style='font-size:1.4rem;font-weight:700;color:#94a3b8'>{nwr_s}</span>"
            f"<br><small>n = {nn}</small></div>"
            f"</div>")


def _html_correlations_table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="note">Not enough data for correlation analysis.</p>'
    hdr = "<th>Indicator</th><th>Spearman ρ</th><th>n</th>"
    body = ""
    for r in rows:
        c   = r.get("correlation")
        c_s = f"{c:+.3f}" if c is not None else "—"
        col = "#22c55e" if (c or 0) > 0.15 else "#ef4444" if (c or 0) < -0.15 else "#94a3b8"
        body += (f"<tr><td>{r['param']}</td>"
                 f"<td style='color:{col};font-weight:600'>{c_s}</td>"
                 f"<td>{r['n']}</td></tr>")
    return f"<table><thead><tr>{hdr}</tr></thead><tbody>{body}</tbody></table>"


def build_html(
    rows:          list[dict],
    min_samples:   int = 5,
    db_path:       Path = _DB_PATH,
    since:         str | None = None,
    tradable_only: bool = False,
) -> str:
    rows = _normalise_row_symbols(rows)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    importance   = parameter_importance(rows, min_samples)
    src_rel      = source_reliability(rows, min_samples)
    regime_data  = regime_analysis(rows, min_samples)
    fail_pats    = failure_patterns(rows)
    suggestions  = suggested_thresholds(rows)
    rolling      = rolling_winrate(rows)
    comp_buckets = bucket_analysis(rows, "composite", min_samples)

    # ── 6 New analysis sections ─────────────────────────────────────────────
    res_analysis      = resolution_analysis(rows, min_samples)
    holding_buckets   = holding_period_analysis(rows, min_samples)
    enricher_data     = enricher_impact(rows, min_samples)
    signal_types      = signal_type_analysis(rows, min_samples)
    actionable_data   = actionable_vs_all(rows)
    input_indicators  = input_indicators_analysis(rows, min_samples)

    total   = len(rows)
    closed  = [r for r in rows if r.get("outcome") in ("success", "failure")]
    wins    = sum(1 for r in closed if r.get("outcome") == "success")
    overall = round(wins / len(closed) * 100, 1) if closed else None

    # ── Canonical win rate (from alerts table, shared definition) ───────────
    canonical_wr: float | None = None
    canonical_n: int = 0
    if _CANONICAL_OK and _DB_PATH.exists():
        try:
            with sqlite3.connect(str(_DB_PATH), timeout=10) as _c:
                _c.execute("PRAGMA journal_mode=WAL")
                _c.execute("PRAGMA busy_timeout=30000")
                _cwr = _canonical_win_rate(_c)
                canonical_wr = _cwr.get("win_rate_pct")
                canonical_n  = _cwr.get("total_closed", 0)
        except Exception:
            pass

    # ── Price coverage stats ────────────────────────────────────────────────
    no_entry   = sum(1 for r in rows if _safe_float(r.get("entry_price")) is None)
    no_return  = sum(1 for r in rows if _safe_float(r.get("return_pct")) is None)
    no_price   = sum(1 for r in rows
                     if _safe_float(r.get("entry_price")) is None
                     and _safe_float(r.get("return_pct")) is None)
    pct_no_price = round(no_price / total * 100, 1) if total else 0

    # ── Group raw rows for drill-down detail ────────────────────────────────
    rows_by_source: dict = {}
    rows_by_regime: dict = {}
    for r in rows:
        rows_by_source.setdefault(r.get("input_source") or "unknown", []).append(r)
        rows_by_regime.setdefault(r.get("market_regime") or "unknown", []).append(r)

    # ── Mini signal table helper for accordion panels ─────────────────────────
    def _mini_signals(signal_rows: list, section_id: str) -> str:
        from datetime import datetime, timezone

        if not signal_rows:
            return "<p style='color:#94a3b8;padding:8px'>No signals</p>"

        # Summary stats — use only closed signals for win-rate; all for avg return
        closed_r  = [r for r in signal_rows if r.get("outcome") in ("success", "failure")]
        wins_r    = [r for r in closed_r if r.get("outcome") == "success"]
        open_r    = [r for r in signal_rows if r.get("outcome") not in ("success", "failure")]
        wr_s      = f"{len(wins_r)/len(closed_r)*100:.0f}%" if closed_r else "—"
        wr_c      = "#22c55e" if closed_r and len(wins_r) / len(closed_r) >= 0.5 else "#f59e0b"

        # avg return = CLOSED trades only (latest snapshot return, capped)
        # Open trades only have tiny 1h/4h snapshots that would dilute the signal.
        ret_vals  = [_safe_float(r.get("return_pct")) for r in closed_r
                     if _safe_float(r.get("return_pct")) is not None
                     and abs(_safe_float(r.get("return_pct")) or 0) <= _MAX_SANE_DISPLAY_PCT]
        avg_ret   = sum(ret_vals) / len(ret_vals) if ret_vals else None
        avg_s     = f"{avg_ret:+.1f}%" if avg_ret is not None else "—"
        avg_c     = "#22c55e" if (avg_ret or 0) >= 0 else "#ef4444"
        sid       = section_id.replace("-", "_")

        now_utc = datetime.now(timezone.utc)

        # Cap rendered rows to most recent 200 for DOM performance
        # (summary stats above already computed from ALL signal_rows)
        _MAX_RENDER_ROWS = 200
        render_rows = signal_rows
        truncated = len(signal_rows) > _MAX_RENDER_ROWS
        if truncated:
            # Sort by timestamp descending, take most recent
            render_rows = sorted(
                signal_rows,
                key=lambda r: r.get("timestamp") or r.get("fire_time") or "",
                reverse=True,
            )[:_MAX_RENDER_ROWS]

        tbody = ""
        for r in render_rows:
            display_symbol = _normalise_tv_symbol(r.get("tv_symbol") or r.get("ticker") or "")
            ticker   = _esc(display_symbol)
            tv_url   = f"https://www.tradingview.com/chart/?symbol={_url_quote(display_symbol, safe='')}"

            # ── Fire date & days open ──────────────────────────────────────
            ts_raw   = r.get("timestamp") or r.get("fire_time") or ""
            ts_disp  = ts_raw[:16]
            days_s   = "—"
            days_col = "#64748b"
            try:
                ft = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ft.tzinfo is None:
                    ft = ft.replace(tzinfo=timezone.utc)
                days_n = (now_utc - ft).days
                days_s = f"{days_n}d"
                days_col = "#94a3b8" if days_n <= 3 else "#64748b"
            except Exception as exc:
                _log.debug("fire_time_parse_failed", raw=ts_raw[:50], error=str(exc))

            # ── Entry price (trigger price) ────────────────────────────────
            entry    = _safe_float(r.get("entry_price"))
            entry_s  = (f"${entry:,.4f}" if entry is not None and entry < 1
                        else f"${entry:,.2f}" if entry is not None else "—")

            # ── Latest return (COALESCE of T+7d→T+3d→T+1d→T+4h→T+1h) ────
            ret      = _safe_float(r.get("return_pct"))
            ret_warn = ret is not None and abs(ret) > _MAX_SANE_DISPLAY_PCT
            if ret_warn:
                ret_s = f"{ret:+.0f}% ⚠"
                r_col = "#f59e0b"
            elif ret is not None:
                ret_s = f"{ret:+.1f}%"
                r_col = "#22c55e" if ret > 2 else "#ef4444" if ret < -2 else "#94a3b8"
            else:
                ret_s = "pending"
                r_col = "#475569"

            # ── Last price = entry * (1 + return%/100) ────────────────────
            last_s   = "—"
            if entry and ret is not None and not ret_warn:
                lp     = entry * (1 + ret / 100.0)
                last_s = (f"${lp:,.4f}" if lp < 1 else f"${lp:,.2f}")

            # ── MFE (peak return ever) ─────────────────────────────────────
            mfe      = _safe_float(r.get("mfe"))
            mfe_warn = mfe is not None and abs(mfe) > _MAX_SANE_DISPLAY_PCT
            if mfe_warn:
                mfe_s  = f"{mfe:+.0f}% ⚠"
                mfe_c  = "#f59e0b"
            elif mfe is not None:
                mfe_s  = f"{mfe:+.1f}%"
                mfe_c  = "#22c55e" if mfe > 0 else "#ef4444"
            else:
                mfe_s  = "—"
                mfe_c  = "#64748b"

            # ── MAE (max drawdown) ─────────────────────────────────────────
            mae     = _safe_float(r.get("mae"))
            mae_s   = f"{mae:+.1f}%" if mae is not None else "—"
            mae_c   = "#ef4444" if (mae or 0) < -5 else "#64748b"

            # ── Score & Outcome ────────────────────────────────────────────
            comp    = _safe_float(r.get("composite"))
            comp_s  = f"{comp:.0f}" if comp is not None else "—"
            comp_c  = "#22c55e" if (comp or 0) >= 70 else ("#f59e0b" if (comp or 0) >= 50 else "#ef4444")
            outcome = r.get("outcome") or "open"
            o_col   = ("#22c55e" if outcome == "success" else
                       "#ef4444" if outcome == "failure" else "#94a3b8")

            # ── No-price badge: both entry AND return are missing ──────────
            no_price_row = entry is None and ret is None
            if no_price_row:
                row_opacity = "opacity:.55;"
                # Determine reason for no price
                tv_sym_raw = display_symbol
                if ":" in tv_sym_raw:
                    exch = tv_sym_raw.split(":", 1)[0]
                    priceable = _is_priceable(tv_sym_raw, r.get("yf_ticker") or "")
                    if not priceable:
                        no_price_reason = (
                            f"<span title='{exch}: no yfinance or TV scanner coverage — "
                            f"price tracking permanently unavailable for this exchange.' "
                            f"style='background:#1e293b;color:#f59e0b;padding:1px 5px;"
                            f"border-radius:3px;font-size:10px;margin-left:4px'>no-price</span>"
                        )
                    else:
                        no_price_reason = (
                            "<span title='Entry price not captured yet. "
                            "Will be fetched on next update_price_evolution() run "
                            "(via TV scanner for futures/EU/EM or yfinance for US/AU/LSE).' "
                            "style='background:#1e293b;color:#64748b;padding:1px 5px;"
                            "border-radius:3px;font-size:10px;margin-left:4px'>pending-entry</span>"
                        )
                else:
                    no_price_reason = ""
            else:
                row_opacity = ""
                no_price_reason = ""

            tbody  += (
                f'<tr data-sym="{ticker.lower()}" '
                f'style="border-bottom:1px solid #1e293b;{row_opacity}">'
                f'<td class="nw">'
                f'  <a href="{tv_url}" target="_blank" class="sym">{ticker}</a>'
                f'{no_price_reason}'
                f'</td>'
                f'<td class="nw ts" style="color:#94a3b8">{ts_disp}</td>'
                f'<td class="nw" style="color:{days_col}">{days_s}</td>'
                f'<td class="mono" style="color:#cbd5e1">{entry_s}</td>'
                f'<td class="mono" style="color:#94a3b8">{last_s}</td>'
                f'<td class="fw7" style="color:{r_col}">{ret_s}</td>'
                f'<td style="color:{mfe_c}">{mfe_s}</td>'
                f'<td style="color:{mae_c}">{mae_s}</td>'
                f'<td class="fw6" style="color:{comp_c}">{comp_s}</td>'
                f'<td style="color:{o_col}">{outcome}</td>'
                f'</tr>'
            )

        # ── Count no-price rows for summary bar ───────────────────────────
        no_price_r = sum(
            1 for r in signal_rows
            if _safe_float(r.get("entry_price")) is None
            and _safe_float(r.get("return_pct")) is None
        )
        no_price_badge = (
            f'<span title="{no_price_r} signals with no entry price or return data — '
            f'either price not captured yet (pending-entry) or exchange has no price source. '
            f'Run update_price_evolution() to fill in TV-scanner-covered exchanges." '
            f'style="color:#f59e0b">'
            f'No-price: <b>{no_price_r}</b></span>'
            if no_price_r > 0 else ""
        )

        truncated_badge = (
            f'<span style="color:#60a5fa;font-size:11px" '
            f'title="Showing most recent {_MAX_RENDER_ROWS} of {len(signal_rows)} signals for performance. '
            f'Stats above reflect ALL {len(signal_rows)} signals.">'
            f'Showing: <b>{len(render_rows)}/{len(signal_rows)}</b></span>'
            if truncated else ""
        )

        return (
            f'<div style="padding:4px 0 8px">'
            f'<div style="display:flex;gap:16px;flex-wrap:wrap;padding:6px 10px;'
            f'background:#0f172a;border-radius:6px;margin-bottom:6px;font-size:12px;align-items:center">'
            f'<span style="color:#64748b">Total: <b style="color:#e2e8f0">{len(signal_rows)}</b></span>'
            f'<span style="color:#64748b">Closed: <b style="color:#e2e8f0">{len(closed_r)}</b></span>'
            f'<span style="color:#64748b">Open: <b style="color:#94a3b8">{len(open_r)}</b></span>'
            f'<span style="color:#64748b">Win Rate: <b style="color:{wr_c}">{wr_s}</b></span>'
            f'<span style="color:#64748b">Avg Return: <b style="color:{avg_c}">{avg_s}</b></span>'
            f'{no_price_badge}'
            f'{truncated_badge}'
            f'<input id="filter-{sid}" placeholder="🔍 filter symbol…" '
            f'style="margin-left:auto;background:#1e293b;border:1px solid #334155;'
            f'color:#e2e8f0;padding:3px 8px;border-radius:4px;font-size:11px" '
            f'oninput="filterSigs(\'{sid}\', this.value)">'
            f'</div>'
            f'<div style="max-height:380px;overflow-y:auto;overflow-x:auto;background:#070d1a;border-radius:6px">'
            f'<table class="sig-tbl" style="font-size:11px;width:100%;border-collapse:collapse;min-width:700px">'
            f'<thead style="position:sticky;top:0;background:#1a2540;z-index:2"><tr>'
            f'<th>Symbol</th>'
            f'<th title="Signal fire time">Fired</th>'
            f'<th title="Days since signal fired">Days</th>'
            f'<th title="Entry/trigger price when signal fired">Entry $</th>'
            f'<th title="Latest price (entry × (1 + latest return%))">Last $</th>'
            f'<th title="Most recent return snapshot (T+7d→T+3d→T+1d→T+4h→T+1h). ⚠=wrong ticker artefact">Return%</th>'
            f'<th title="Peak (max favorable excursion) return ever recorded">MFE%</th>'
            f'<th title="Max adverse excursion (worst drawdown recorded)">MAE%</th>'
            f'<th>Score</th>'
            f'<th>Outcome</th>'
            f'</tr></thead>'
            f'<tbody id="tbody-{sid}">{tbody}</tbody>'
            f'</table></div>'
            f'</div>'
        )

    # ── Parameter importance table (with horizontal bar chart data) ───────────
    imp_rows = ""
    imp_params  = []
    imp_corrs   = []
    imp_colors  = []
    for i_row in importance:
        corr     = i_row.get("correlation")
        corr_str = f"{corr:.4f}" if corr is not None else "n/a"
        color    = _color_corr(corr)
        imp_rows += (
            f"<tr>"
            f"<td>{'★ ' if abs(corr or 0) > 0.2 else ''}{_esc(i_row['param'])}</td>"
            f"<td style='color:{color};font-weight:600'>{corr_str}</td>"
            f"<td>{_esc(i_row['direction'])}</td>"
            f"<td>{i_row['sample_count']}</td>"
            f"</tr>"
        )
        imp_params.append(i_row["param"])
        imp_corrs.append(round(corr, 4) if corr is not None else 0)
        imp_colors.append("#22c55e" if (corr or 0) > 0 else "#ef4444")
    if not imp_rows:
        imp_rows = "<tr><td colspan='4' style='color:#94a3b8'>Not enough closed samples yet</td></tr>"

    # ── Source accordion rows ─────────────────────────────────────────────────
    src_rows = ""
    for s in src_rel:
        wr       = s.get("win_rate")
        src_key  = s["source"]
        safe_k   = src_key.replace("'", "\\'")
        mini     = _mini_signals(rows_by_source.get(src_key, []), f"src-{src_key}")
        wr_str   = f"{wr:.1f}%" if wr is not None else "n/a"
        avg_ret  = s.get("avg_return")
        outliers = s.get("outliers_skipped", 0)
        if avg_ret is not None:
            ret_str = f"{avg_ret:+.1f}%"
            if outliers:
                ret_str += f' <span title="{outliers} outlier(s) &gt;±{_MAX_SANE_DISPLAY_PCT:.0f}% excluded" style="color:#f59e0b;font-size:10px">⚠ {outliers} excluded</span>'
        else:
            ret_str = "n/a"
        closed_n = s.get("closed_count", 0)
        mfe_val  = s.get("avg_mfe")
        mae_val  = s.get("avg_mae")
        mfe_str  = f"{mfe_val:+.1f}%" if mfe_val is not None else "n/a"
        mae_str  = f"{mae_val:+.1f}%" if mae_val is not None else "n/a"
        src_rows += (
            f'<tr class="acc-row" onclick="toggleAcc(\'acc-src-{safe_k}\')" style="cursor:pointer">'
            f'<td style="padding:10px 12px">'
            f'<span id="arrow-acc-src-{safe_k}" '
            f'style="display:inline-block;transition:transform .2s;margin-right:6px;color:#64748b">▶</span>'
            f'<b style="color:#e2e8f0">{_esc(s.get("source_display", src_key))}</b>'
            f'<span style="background:#1e3a5f;color:#7dd3fc;border-radius:10px;'
            f'padding:1px 8px;font-size:11px;margin-left:6px">{s["count"]}</span>'
            f'<span title="{closed_n} closed out of {s["count"]} total" '
            f'style="color:#64748b;font-size:10px;margin-left:6px">{closed_n} closed</span>'
            f'</td>'
            f'<td style="padding:10px 12px;color:{_color_wr(wr)};font-weight:700">{wr_str}</td>'
            f'<td style="padding:10px 12px" title="Avg return of {closed_n} closed signals (latest snapshot)">{ret_str}</td>'
            f'<td style="padding:10px 12px;color:#64748b" title="Avg peak return (MFE) of closed signals">{mfe_str}</td>'
            f'<td style="padding:10px 12px;color:#ef4444" title="Avg worst drawdown (MAE) of closed signals">{mae_str}</td>'
            f'</tr>'
            f'<tr id="acc-src-{safe_k}" style="display:none">'
            f'<td colspan="5" style="padding:4px 12px 16px;background:#0b1221;'
            f'border-left:3px solid #334155">{mini}</td>'
            f'</tr>'
        )
    if not src_rows:
        src_rows = "<tr><td colspan='5' style='color:#94a3b8;padding:10px 12px'>No source data yet</td></tr>"

    # ── Regime accordion rows ─────────────────────────────────────────────────
    reg_rows = ""
    for reg in regime_data:
        wr       = reg.get("win_rate")
        reg_key  = reg["regime"]
        safe_rk  = reg_key.replace("'", "\\'").replace(" ", "_")
        mini     = _mini_signals(rows_by_regime.get(reg_key, []), f"reg-{safe_rk}")
        wr_str   = f"{wr:.1f}%" if wr is not None else "n/a"
        reg_rows += (
            f'<tr class="acc-row" onclick="toggleAcc(\'acc-reg-{safe_rk}\')" style="cursor:pointer">'
            f'<td style="padding:10px 12px">'
            f'<span id="arrow-acc-reg-{safe_rk}" '
            f'style="display:inline-block;transition:transform .2s;margin-right:6px;color:#64748b">▶</span>'
            f'<b style="color:#e2e8f0">{_esc(reg_key)}</b>'
            f'<span style="background:#1e3a5f;color:#7dd3fc;border-radius:10px;'
            f'padding:1px 8px;font-size:11px;margin-left:6px">{reg["count"]}</span>'
            f'</td>'
            f'<td style="padding:10px 12px;color:{_color_wr(wr)};font-weight:700">{wr_str}</td>'
            f'<td style="padding:10px 12px">{reg.get("avg_return") or "n/a"}%</td>'
            f'</tr>'
            f'<tr id="acc-reg-{safe_rk}" style="display:none">'
            f'<td colspan="3" style="padding:4px 12px 16px;background:#0b1221;'
            f'border-left:3px solid #334155">{mini}</td>'
            f'</tr>'
        )
    if not reg_rows:
        reg_rows = "<tr><td colspan='3' style='color:#94a3b8;padding:10px 12px'>No regime data yet</td></tr>"

    # ── Composite bucket accordion rows ───────────────────────────────────────
    bkt_rows  = ""
    bkt_labels = []
    bkt_wr     = []
    bkt_colors = []
    for b in comp_buckets:
        wr       = b.get("success_rate")
        rng      = b["range"]
        bkt_id   = rng.replace(".", "_").replace("–", "_")
        lo, hi   = b.get("lo", 0), b.get("hi", 100)
        mini     = _mini_signals(
            [r for r in rows
             if _safe_float(r.get("composite")) is not None
             and lo <= (_safe_float(r.get("composite")) or 0) <= hi],
            f"bkt-{bkt_id}"
        )
        wr_str   = f"{wr:.1f}%" if wr is not None else "n/a"
        bkt_labels.append(rng)
        bkt_wr.append(wr or 0)
        bkt_colors.append("#22c55e" if (wr or 0) >= 55 else ("#ef4444" if (wr or 0) < 40 else "#f59e0b"))
        bkt_mfe_val = b.get("avg_mfe")
        bkt_mae_val = b.get("avg_mae")
        bkt_mfe_s   = f"{bkt_mfe_val:+.1f}%" if bkt_mfe_val is not None else "n/a"
        bkt_mae_s   = f"{bkt_mae_val:+.1f}%" if bkt_mae_val is not None else "n/a"
        bkt_rows += (
            f'<tr class="acc-row" onclick="toggleAcc(\'acc-bkt-{bkt_id}\')" style="cursor:pointer">'
            f'<td style="padding:10px 12px">'
            f'<span id="arrow-acc-bkt-{bkt_id}" '
            f'style="display:inline-block;transition:transform .2s;margin-right:6px;color:#64748b">▶</span>'
            f'<b style="color:#e2e8f0">{_esc(rng)}</b>'
            f'<span style="background:#1e3a5f;color:#7dd3fc;border-radius:10px;'
            f'padding:1px 8px;font-size:11px;margin-left:6px">{b["count"]}</span>'
            f'</td>'
            f'<td style="padding:10px 12px;color:{_color_wr(wr)};font-weight:700">{wr_str}</td>'
            f'<td style="padding:10px 12px;color:#64748b" title="Avg peak return (MFE) of closed signals in bucket">{bkt_mfe_s}</td>'
            f'<td style="padding:10px 12px;color:#ef4444" title="Avg worst drawdown (MAE) of closed signals in bucket">{bkt_mae_s}</td>'
            f'</tr>'
            f'<tr id="acc-bkt-{bkt_id}" style="display:none">'
            f'<td colspan="4" style="padding:4px 12px 16px;background:#0b1221;'
            f'border-left:3px solid #334155">{mini}</td>'
            f'</tr>'
        )
    if not bkt_rows:
        bkt_rows = "<tr><td colspan='4' style='color:#94a3b8;padding:10px 12px'>Not enough data</td></tr>"

    # ── Failure pattern bullets ───────────────────────────────────────────────
    fail_html = ""
    if fail_pats:
        fail_html = ("<ul style='padding-left:1.2em'>" +
                     "".join(f"<li style='margin:4px 0;color:#fca5a5'>{_esc(p)}</li>" for p in fail_pats) +
                     "</ul>")
    else:
        fail_html = "<p style='color:#94a3b8'>No failure patterns detected yet.</p>"

    # ── Suggestion bullets ────────────────────────────────────────────────────
    sug_html = ""
    if suggestions:
        sug_html = ("<ul style='padding-left:1.2em'>" +
                    "".join(f"<li style='margin:4px 0;color:#86efac'>{_esc(s)}</li>" for s in suggestions) +
                    "</ul>")
    else:
        sug_html = "<p style='color:#94a3b8'>No threshold suggestions yet (need more closed trades).</p>"

    # ── Rolling win-rate chart data ───────────────────────────────────────────
    spark_labels = json.dumps([r["date"]    for r in rolling[-60:]])
    spark_data   = json.dumps([r["win_rate"] for r in rolling[-60:]])

    # ── Chart.js data ─────────────────────────────────────────────────────────
    imp_params_js  = json.dumps(imp_params)
    imp_corrs_js   = json.dumps(imp_corrs)
    imp_colors_js  = json.dumps(imp_colors)
    bkt_labels_js  = json.dumps(bkt_labels)
    bkt_wr_js      = json.dumps(bkt_wr)
    bkt_colors_js  = json.dumps(bkt_colors)

    scipy_note  = "Spearman r via scipy.stats" if _SCIPY_OK else "fallback (no scipy)"
    since_note  = f" | Since: {since}" if since else ""
    filter_note = " | Filter: tradable-only" if tradable_only else ""
    overall_str = f"{overall}%" if overall is not None else "n/a"
    wr_color    = _color_wr(overall)
    canonical_wr_str = f"{canonical_wr:.1f}%" if canonical_wr is not None else "—"
    open_count  = total - len(closed)
    losses      = len(closed) - wins

    # ── Price coverage warning card ──────────────────────────────────────────
    if no_price > 0:
        no_price_pct_str = f"{pct_no_price:.0f}%"
        if tradable_only:
            first_note = (
                "These rows passed the tradable/priceable filter, so they usually need a fresh "
                "price update or an entry-price backfill rather than another exchange filter."
            )
            action_note = (
                "Run <code>python tradingview/alert_tracker.py --update</code> during market hours, "
                "then regenerate this report."
            )
            badge_note = (
                "Symbols with <span style='background:#1e293b;padding:1px 5px;"
                "border-radius:3px;color:#f59e0b'>no-price</span> badge still have no "
                "current price snapshot or no entry price captured."
            )
        else:
            first_note = (
                "Some rows may be from exchanges without yfinance or TV scanner coverage; "
                "others may simply be waiting for their first price snapshot."
            )
            action_note = "Use <code>--tradable-only</code> or run a price update to separate the two cases."
            badge_note = (
                "Symbols with <span style='background:#1e293b;padding:1px 5px;"
                "border-radius:3px;color:#f59e0b'>no-price</span> badge have no price "
                "source at all (exchange not in yfinance or TV scanner). These will never "
                "resolve. Use <code>--tradable-only</code> to exclude them from this report."
            )
        price_warn_html = (
            f"<div class='card' style='margin-top:12px;border-left:3px solid #f59e0b;"
            f"padding:12px 16px;font-size:13px'>"
            f"<b style='color:#f59e0b'>⚠ Price Data Gap: {no_price} signal(s) ({no_price_pct_str}) have no entry price or return data.</b>"
            f"<ul style='margin:6px 0 0 16px;line-height:2;color:#94a3b8'>"
            f"<li>{first_note}</li>"
            f"<li><b>Why Return% shows 'pending'</b>: <code>update_price_evolution()</code> "
            f"has not run since the signal fired, OR the exchange has no price source "
            f"(e.g. MYX equity futures, NSE/BSE). Most futures and European exchanges "
            f"now use the TV scanner as fallback — run an update pass to populate these.</li>"
            f"<li>{action_note}</li>"
            f"<li>{badge_note}</li>"
            f"</ul></div>"
        )
    else:
        price_warn_html = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Performance Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" integrity="sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" crossorigin="anonymous"></script>
<style>
  :root{{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--sub:#94a3b8;--accent:#7c3aed;
        --green:#22c55e;--red:#ef4444;--amber:#f59e0b}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px}}
  nav{{background:#0d1424;border-bottom:1px solid var(--border);padding:.6rem 1.5rem;display:flex;gap:.8rem;flex-wrap:wrap;position:sticky;top:0;z-index:100}}
  nav a{{color:#64748b;font-size:.78rem;text-decoration:none;padding:.2rem .5rem;border-radius:4px;white-space:nowrap}}
  nav a:hover{{color:#e2e8f0;background:var(--card)}}
  .hdr{{padding:24px 28px 12px;border-bottom:1px solid var(--border)}}
  h1{{font-size:22px;font-weight:700;color:#f8fafc}}
  h2{{font-size:15px;font-weight:600;color:var(--accent);margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
  .meta{{color:var(--sub);font-size:12px;margin-top:4px}}
  .sec{{padding:0 28px 28px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:8px}}
  .kpi{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}}
  .kpi .val{{font-size:28px;font-weight:700}}
  .kpi .lbl{{color:var(--sub);font-size:12px;margin-top:3px}}
  table.acc{{border-collapse:collapse;width:100%;max-width:820px;margin-top:10px}}
  table.acc th{{color:var(--sub);font-size:11px;font-weight:600;text-align:left;padding:7px 12px;border-bottom:1px solid var(--border)}}
  .acc-row:hover td{{background:rgba(255,255,255,.04)}}
  table.plain{{border-collapse:collapse;width:100%;max-width:760px;font-size:13px;margin-bottom:8px}}
  table.plain th{{color:var(--sub);font-size:11px;font-weight:600;text-align:left;padding:6px 10px;border-bottom:1px solid var(--border)}}
  table.plain td{{padding:6px 10px;border-bottom:1px solid #1e293b}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px;max-width:820px;margin-bottom:16px}}
  canvas{{max-height:220px;max-width:720px;margin-bottom:10px}}
  .note{{color:var(--sub);font-size:12px;margin-top:6px;font-style:italic}}
  .hint{{font-size:11px;color:#475569;font-weight:400;margin-left:8px}}
  footer{{padding:16px 28px;color:#374151;font-size:12px;border-top:1px solid var(--border);margin-top:8px}}
  /* _mini_signals table classes */
  .sig-tbl td{{padding:5px 8px}}
  .sig-tbl .nw{{white-space:nowrap}}
  .sig-tbl .mono{{font-family:monospace}}
  .sig-tbl .fw6{{font-weight:600}}
  .sig-tbl .fw7{{font-weight:700}}
  .sig-tbl .ts{{font-size:10px}}
  .sig-tbl a.sym{{color:#7dd3fc;font-weight:600;text-decoration:none}}
  .sig-tbl th{{padding:5px 8px;color:#64748b;font-weight:600;text-align:left}}
</style>
</head>
<body>
<nav>
  <a href="#overview">📊 Overview</a>
  <a href="#importance">🔑 Importance</a>
  <a href="#buckets">🎯 Buckets</a>
  <a href="#sources">📂 Sources</a>
  <a href="#regime">🌐 Regime</a>
  <a href="#patterns">⚠️ Patterns</a>
  <a href="#rolling">📈 Rolling WR</a>
</nav>
<header>
  <h1>📉 Signal Performance Analysis</h1>
  <p class="meta">Generated: {now}{since_note}{filter_note} | Signals: {total} | Method: {scipy_note}</p>
</header>
<main>
<div class="sec" id="overview">
  <h2>Overview</h2>
  <div class="grid">
    <div class="kpi"><div class="val">{total}</div><div class="lbl">Total Signals</div></div>
    <div class="kpi"><div class="val">{len(closed)}</div><div class="lbl">Closed (Win+Loss)</div></div>
    <div class="kpi"><div class="val" style="color:{wr_color}">{overall_str}</div><div class="lbl">Overall Win Rate</div></div>
    <div class="kpi"><div class="val" style="color:#f59e0b">{canonical_wr_str}</div><div class="lbl">Canonical WR (alerts, n={canonical_n})</div></div>
    <div class="kpi"><div class="val" style="color:#22c55e">{wins}</div><div class="lbl">Winners</div></div>
    <div class="kpi"><div class="val" style="color:#ef4444">{losses}</div><div class="lbl">Losses</div></div>
    <div class="kpi"><div class="val" style="color:#94a3b8">{open_count}</div><div class="lbl">Still Open</div></div>
    <div class="kpi"><div class="val" style="color:{'#f59e0b' if no_price > 0 else '#22c55e'}">{no_price}</div><div class="lbl">No Price Data</div></div>
  </div>
  {price_warn_html}
  <div class="card" style="margin-top:16px;font-size:13px">
    <b style="color:#e2e8f0">How outcomes are determined:</b>
    <ul style="margin:8px 0 0 16px;line-height:1.9;color:#94a3b8">
      <li>✅ <b style="color:#22c55e">Success</b> — price hit <b>+5%</b> above entry at any price snapshot within 7 days of signal fire</li>
      <li>❌ <b style="color:#ef4444">Failure</b> — price hit <b>−5%</b> below entry (stop-loss triggered) at any snapshot within 7 days</li>
      <li>⏳ <b style="color:#94a3b8">Open</b> — signal is still within the 7-day tracking window, or no price snapshot yet recorded</li>
    </ul>
    <p style="margin-top:8px;color:#475569;font-size:12px">
      <b>Return% column</b> = most recent price snapshot return (T+7d → T+3d → T+1d → T+4h → T+1h, first non-null).
      <b>MFE%</b> = peak (max favorable excursion) return ever recorded.
      <b>Entry $</b> = price when signal fired.
      <b>Last $</b> = estimated current price (entry × (1 + return%)).
      <span style="color:#f59e0b">⚠</span> flag = return &gt;±200% — likely wrong-ticker artefact.
    </p>
  </div>
</div>

<div class="sec" id="importance">
  <h2>🔑 Parameter Importance (Spearman ρ with outcome)</h2>
  <canvas id="impChart"></canvas>
  <table class="plain">
    <thead><tr><th>Parameter</th><th>Correlation ρ</th><th>Direction</th><th>Samples</th></tr></thead>
    <tbody>{imp_rows}</tbody>
  </table>
  <p class="note">★ = |ρ| > 0.2 — likely meaningful predictor. Green = positive predictor, Red = negative.</p>
</div>

<div class="sec" id="buckets">
  <h2>🎯 Composite Score Buckets (quintiles)
    <span class="hint">▶ Click any row to see all signals in that range</span>
  </h2>
  <canvas id="bktChart"></canvas>
  <table class="acc">
    <thead><tr>
      <th>Score Range</th><th>Win Rate</th><th>Avg MFE</th><th>Avg MAE</th>
    </tr></thead>
    <tbody>{bkt_rows}</tbody>
  </table>
</div>

<div class="sec" id="sources">
  <h2>📂 Source Reliability
    <span class="hint">▶ Click any row to see all signals from that source</span>
  </h2>
  <table class="acc">
    <thead><tr>
      <th>Source</th><th>Win Rate</th><th>Avg Return</th><th>Avg MFE</th><th>Avg MAE</th>
    </tr></thead>
    <tbody>{src_rows}</tbody>
  </table>
</div>

<div class="sec" id="regime">
  <h2>🌐 Market Regime Breakdown
    <span class="hint">▶ Click any row to see signals in that regime</span>
  </h2>
  <table class="acc">
    <thead><tr>
      <th>Regime</th><th>Win Rate</th><th>Avg Return</th>
    </tr></thead>
    <tbody>{reg_rows}</tbody>
  </table>
</div>

<div class="sec" id="patterns">
  <h2>⚠️ Failure Pattern Detection</h2>
  <div class="card">{fail_html}</div>
  <h2 style="margin-top:20px">💡 Suggested Threshold Adjustments</h2>
  <div class="card">{sug_html}</div>
</div>

<div class="sec" id="rolling">
  <h2>📈 Rolling 30-Day Win Rate</h2>
  <canvas id="sparkChart"></canvas>
  <p class="note">Based on {len(rolling)} closed signals. Win rate = wins / (wins + losses) in rolling 30-day window.</p>
</div>

<div class="sec" id="resolution">
  <h2>🕐 Win Rate by Resolution</h2>
  {_html_table_2col(res_analysis,      key_col="resolution", wr_col="win_rate", n_col="closed", label="Resolution")}
</div>

<div class="sec" id="holding">
  <h2>⏳ Win Rate by Holding Period</h2>
  {_html_table_2col(holding_buckets,   key_col="bucket",     wr_col="win_rate", n_col="count",  label="Period")}
</div>

<div class="sec" id="signal-type">
  <h2>🏷️ Win Rate by Signal Type</h2>
  {_html_table_2col(signal_types,      key_col="signal",     wr_col="win_rate", n_col="closed", label="Signal")}
</div>

<div class="sec" id="enricher">
  <h2>🔬 Enricher Impact Analysis</h2>
  {_html_enricher_table(enricher_data)}
</div>

<div class="sec" id="actionable">
  <h2>✅ Actionable vs All Signals</h2>
  {_html_actionable_card(actionable_data)}
</div>

<div class="sec" id="input-indicators">
  <h2>📊 Input Indicator Correlations</h2>
  {_html_correlations_table(input_indicators)}
</div>

<footer>
  Analysis based on {len(rows)} signal rows. Outcomes: {len(closed)} closed, {total - len(closed)} still open.
  {scipy_note}. Charts require JavaScript.
</footer>

<script>
(function() {{
  // Parameter importance horizontal bar chart
  const impCtx = document.getElementById('impChart');
  if (impCtx && {imp_params_js}.length > 0) {{
    new Chart(impCtx.getContext('2d'), {{
      type: 'bar',
      data: {{
        labels: {imp_params_js},
        datasets: [{{
          label: 'Spearman ρ',
          data: {imp_corrs_js},
          backgroundColor: {imp_colors_js},
          borderRadius: 3,
        }}],
      }},
      options: {{
        indexAxis: 'y',
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#1e293b' }}, suggestedMin: -0.5, suggestedMax: 0.5 }},
          y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#1e293b' }} }},
        }},
      }},
    }});
  }}

  // Composite bucket win rate bar chart
  const bktCtx = document.getElementById('bktChart');
  if (bktCtx && {bkt_labels_js}.length > 0) {{
    new Chart(bktCtx.getContext('2d'), {{
      type: 'bar',
      data: {{
        labels: {bkt_labels_js},
        datasets: [{{
          label: 'Win Rate %',
          data: {bkt_wr_js},
          backgroundColor: {bkt_colors_js},
          borderRadius: 4,
        }}],
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#1e293b' }} }},
          y: {{ min: 0, max: 100, ticks: {{ color: '#94a3b8', callback: v => v + '%' }}, grid: {{ color: '#1e293b' }} }},
        }},
      }},
    }});
  }}

  // Rolling win rate line chart
  const spkCtx = document.getElementById('sparkChart');
  if (spkCtx && {spark_labels}.length > 0) {{
    new Chart(spkCtx.getContext('2d'), {{
      type: 'line',
      data: {{
        labels: {spark_labels},
        datasets: [{{
          label: '30-day win rate %',
          data: {spark_data},
          borderColor: '#7c3aed',
          backgroundColor: 'rgba(124,58,237,0.1)',
          tension: 0.3, fill: true, pointRadius: 2,
        }}],
      }},
      options: {{
        plugins: {{ legend: {{ labels: {{ color: '#e2e8f0', font: {{ size: 12 }} }} }} }},
        scales: {{
          x: {{ ticks: {{ color: '#94a3b8', maxRotation: 45, font: {{ size: 10 }} }} }},
          y: {{ min: 0, max: 100, ticks: {{ color: '#94a3b8', callback: v => v + '%' }} }},
        }},
      }},
    }});
  }}

  // Accordion toggle
  window.toggleAcc = function(id) {{
    const row   = document.getElementById(id);
    const arrow = document.getElementById('arrow-' + id);
    if (!row) return;
    const isOpen = row.style.display !== 'none';
    row.style.display = isOpen ? 'none' : 'table-row';
    if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
  }};

  // Symbol filter inside expanded panels
  window.filterSigs = function(sectionId, query) {{
    const q     = query.toLowerCase();
    const tbody = document.getElementById('tbody-' + sectionId);
    if (!tbody) return;
    tbody.querySelectorAll('tr').forEach(tr => {{
      const sym = tr.getAttribute('data-sym') || '';
      tr.style.display = sym.includes(q) || q === '' ? '' : 'none';
    }});
  }};
}})();
</script>
</main>
</body>
</html>"""

    return html


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceAnalyzer (importable class)
# ─────────────────────────────────────────────────────────────────────────────

class PerformanceAnalyzer:
    """
    Importable interface for programmatic use.

    Example:
        from performance_analyzer import PerformanceAnalyzer
        pa = PerformanceAnalyzer()
        html = pa.build_report()
    """

    def __init__(
        self,
        db_path: Path | str = _DB_PATH,
        min_samples: int = 5,
    ) -> None:
        self.db_path     = Path(db_path)
        self.min_samples = min_samples

    def load_rows(
        self,
        since: str | None = None,
        tradable_only: bool = False,
    ) -> list[dict]:
        return _load_outcomes(self.db_path, since, tradable_only=tradable_only)

    def build_report(
        self,
        since: str | None = None,
        tradable_only: bool = False,
    ) -> str:
        rows = self.load_rows(since, tradable_only=tradable_only)
        return build_html(rows, self.min_samples, self.db_path, since, tradable_only=tradable_only)

    def get_summary(
        self,
        since: str | None = None,
        tradable_only: bool = False,
    ) -> dict:
        rows = self.load_rows(since, tradable_only=tradable_only)
        return {
            "total":                len(rows),
            "closed":               len([r for r in rows if r.get("outcome") in ("success", "failure")]),
            "win_rate":             _success_rate(rows),
            "parameter_importance": parameter_importance(rows, self.min_samples),
            "source_reliability":   source_reliability(rows, self.min_samples),
            "failure_patterns":     failure_patterns(rows),
            "suggestions":          suggested_thresholds(rows),
        }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Signal performance analyzer — queries alert_tracker.db"
    )
    parser.add_argument(
        "--min-samples", type=int, default=5, metavar="N",
        help="Minimum samples per bucket to include in analysis (default 5)"
    )
    parser.add_argument(
        "--since", default=None, metavar="DATE",
        help="Filter signals since YYYY-MM-DD (default: all time)"
    )
    parser.add_argument(
        "--db", default=str(_DB_PATH), metavar="PATH",
        help=f"Path to alert_tracker.db (default: {_DB_PATH})"
    )
    parser.add_argument(
        "--out", default=str(_OUT_DEFAULT), metavar="PATH",
        help=f"Output HTML path (default: {_OUT_DEFAULT})"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print summary JSON to stdout instead of generating HTML"
    )
    parser.add_argument(
        "--tradable-only", action="store_true",
        help=(
            "Exclude signals from exchanges not supported by yfinance (e.g. CBOT futures, "
            "BMFBOVESPA, MYX, TASE, NSE/BSE). These signals can never have prices "
            "tracked and permanently inflate the Open count. "
            "Highly recommended for clean win-rate analysis."
        )
    )
    parser.add_argument(
        "--priceable-only", action="store_true", default=True,
        help=(
            "Exclude signals that have no price source — neither yfinance nor the TV scanner "
            "covers their exchange. Broader than --tradable-only: also filters out exchanges "
            "like NSE/BSE/MYX that are not in either price map. "
            "Recommended for the cleanest win-rate view. (Default: on)"
        )
    )
    parser.add_argument(
        "--include-unpriceable", action="store_true",
        help="Override --priceable-only default and include ALL signals regardless of price coverage."
    )
    args = parser.parse_args()

    db_path        = Path(args.db)
    out_path       = Path(args.out)
    tradable_only  = getattr(args, "tradable_only", False)
    include_all    = getattr(args, "include_unpriceable", False)
    priceable_only = (not include_all) and getattr(args, "priceable_only", True)
    # --priceable-only implies --tradable-only at query level (both use tradable=1 as base)
    if priceable_only:
        tradable_only = True

    if not db_path.exists():
        print(f"[performance_analyzer] DB not found: {db_path}", flush=True)
        print("  Run the signal engine first to populate alert_tracker.db.", flush=True)
        sys.exit(0)

    pa = PerformanceAnalyzer(db_path=db_path, min_samples=args.min_samples)

    if args.json:
        summary = pa.get_summary(since=args.since, tradable_only=tradable_only)
        print(json.dumps(summary, indent=2, default=str))
        return

    rows = pa.load_rows(since=args.since, tradable_only=tradable_only)
    print(f"[performance_analyzer] {len(rows)} signal rows loaded from {db_path.name}", flush=True)
    if tradable_only:
        print("  --tradable-only active: non-priceable exchange signals excluded", flush=True)

    no_price = sum(
        1 for r in rows
        if _safe_float(r.get("entry_price")) is None
        and _safe_float(r.get("return_pct")) is None
    )
    if no_price:
        if tradable_only:
            print(
                f"  ⚠  {no_price} tradable signal(s) still have no price data; "
                f"run alert_tracker.py --update to backfill pending snapshots.",
                flush=True,
            )
        else:
            print(
                f"  ⚠  {no_price} signal(s) have no price data (unsupported exchange or "
                f"missing entry price). Use --tradable-only to filter unsupported exchanges.",
                flush=True,
            )

    closed = [r for r in rows if r.get("outcome") in ("success", "failure")]
    if closed:
        wr = _success_rate(closed)
        print(f"  Overall win rate: {wr}% ({len(closed)} closed trades)", flush=True)
    else:
        print("  No closed trades yet — win rate unavailable", flush=True)

    html = build_html(
        rows,
        min_samples=args.min_samples,
        db_path=db_path,
        since=args.since,
        tradable_only=tradable_only,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import sys as _s
        _rpt = str(Path(__file__).resolve().parent.parent / "reports")
        if _rpt not in _s.path: _s.path.insert(0, _rpt)
        from report_db import archive_before_write as _abw
        _abw(out_path)
    except Exception:
        pass
    out_path.write_text(html, encoding="utf-8")
    print(f"  Report saved: {out_path}", flush=True)

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent / "reports"))
        from report_db import save_performance_analyzer_snapshot, archive_report as _arc
        save_performance_analyzer_snapshot(rows)
        _arc(out_path)
    except Exception as _e:
        print(f"  [report_db] performance_analyzer snapshot skipped: {_e}")


if __name__ == "__main__":
    _main()
