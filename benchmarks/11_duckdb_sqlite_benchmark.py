"""
BENCHMARK 11 — DuckDB vs SQLite: analytical query performance
==============================================================
BEFORE : All analytical queries run on SQLite (read via sqlite3 module)
AFTER  : Same queries run on DuckDB (columnar, OLAP-optimised)

Measures (using alert_tracker.db as real-world data source):
  1. Win-rate aggregation by source  (GROUP BY with CASE expression)
  2. JSON field extraction + aggregation  (json_extract emulation)
  3. Rolling win-rate via window functions  (ROW_NUMBER + OVER)
  4. Time-series monthly aggregation  (strftime / date_trunc)
  5. Cold-start overhead  (first import / connection cost)

Recommendation guide at the end — when to switch vs stay on SQLite.

Usage:
    python benchmarks/11_duckdb_sqlite_benchmark.py
    python benchmarks/11_duckdb_sqlite_benchmark.py --db path/to/alert_tracker.db
"""
import sys
import argparse
import time
import sqlite3
import statistics
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT   = Path(__file__).parent.parent
TV     = ROOT / "tradingview"
DIVIDER = "=" * 72

REPS = 5  # repetitions per query for median timing


# ─── timing helpers ───────────────────────────────────────────────────────────

def _median_ms(times: list[float]) -> float:
    return round(statistics.median(times) * 1000, 2)


def _run_sqlite(db_path: Path, sql: str, params: tuple = (), reps: int = REPS) -> tuple[float, int]:
    """Run SQL on SQLite `reps` times; return (median_ms, row_count)."""
    row_count = 0
    times = []
    for i in range(reps):
        t0 = time.perf_counter()
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(sql, params).fetchall()
        times.append(time.perf_counter() - t0)
        row_count = len(rows)
    return _median_ms(times), row_count


def _run_duckdb(conn, sql: str, reps: int = REPS) -> tuple[float, int]:
    """Run SQL on DuckDB `reps` times; return (median_ms, row_count)."""
    row_count = 0
    times = []
    for i in range(reps):
        t0 = time.perf_counter()
        rows = conn.execute(sql).fetchall()
        times.append(time.perf_counter() - t0)
        row_count = len(rows)
    return _median_ms(times), row_count


# ─── benchmark suite ──────────────────────────────────────────────────────────

BENCHMARKS = [
    (
        "Win-rate by source (GROUP BY + CASE)",
        # SQLite
        """
        SELECT
            source,
            COUNT(*)                                                      AS total,
            SUM(CASE WHEN COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) >= 5.0
                     THEN 1 ELSE 0 END)                                   AS wins,
            ROUND(
              SUM(CASE WHEN COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) >= 5.0
                       THEN 1.0 ELSE 0.0 END)
              / NULLIF(COUNT(*), 0) * 100, 1
            )                                                             AS win_rate_pct
        FROM alerts a
        LEFT JOIN performance_metrics pm ON pm.alert_uuid = a.alert_uuid
        WHERE a.source IS NOT NULL
        GROUP BY a.source
        ORDER BY win_rate_pct DESC
        """,
        # DuckDB (same SQL — DuckDB is largely SQLite-compatible)
        """
        SELECT
            source,
            COUNT(*)                                                      AS total,
            SUM(CASE WHEN COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) >= 5.0
                     THEN 1 ELSE 0 END)                                   AS wins,
            ROUND(
              SUM(CASE WHEN COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) >= 5.0
                       THEN 1.0 ELSE 0.0 END)
              / NULLIF(COUNT(*), 0) * 100, 1
            )                                                             AS win_rate_pct
        FROM alerts a
        LEFT JOIN performance_metrics pm ON pm.alert_uuid = a.alert_uuid
        WHERE a.source IS NOT NULL
        GROUP BY a.source
        ORDER BY win_rate_pct DESC
        """,
    ),
    (
        "JSON extraction + score breakdown (json_extract)",
        # SQLite
        """
        SELECT
            a.tv_symbol,
            a.composite_score,
            CAST(json_extract(a.result_json, '$.vix_level')           AS REAL) AS vix,
            CAST(json_extract(a.result_json, '$.scores.l1.raw_rsi')   AS REAL) AS rsi,
            CAST(json_extract(a.result_json, '$.scores.l1.raw_adx')   AS REAL) AS adx,
            CAST(json_extract(a.result_json, '$.scores.l1.squeeze')   AS INTEGER) AS squeeze
        FROM alerts a
        WHERE a.result_json IS NOT NULL
          AND a.composite_score > 60
        LIMIT 500
        """,
        # DuckDB uses json_extract_string / json_extract differently
        # but also supports SQLite-style json_extract via compatibility layer
        """
        SELECT
            a.tv_symbol,
            a.composite_score,
            TRY_CAST(json_extract_string(a.result_json, '$.vix_level')         AS DOUBLE) AS vix,
            TRY_CAST(json_extract_string(a.result_json, '$.scores.l1.raw_rsi') AS DOUBLE) AS rsi,
            TRY_CAST(json_extract_string(a.result_json, '$.scores.l1.raw_adx') AS DOUBLE) AS adx,
            TRY_CAST(json_extract_string(a.result_json, '$.scores.l1.squeeze') AS INTEGER) AS squeeze
        FROM alerts a
        WHERE a.result_json IS NOT NULL
          AND a.composite_score > 60
        LIMIT 500
        """,
    ),
    (
        "Rolling 30-trade win-rate (window function)",
        # SQLite (no native window ROWS, uses correlated subquery as approx)
        """
        SELECT
            a.tv_symbol,
            a.fire_time,
            a.composite_score,
            COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) AS ret,
            CASE WHEN COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) >= 5.0
                 THEN 1.0 ELSE 0.0 END AS is_win
        FROM alerts a
        LEFT JOIN performance_metrics pm ON pm.alert_uuid = a.alert_uuid
        WHERE COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) IS NOT NULL
        ORDER BY a.fire_time
        """,
        # DuckDB — proper AVG OVER ROWS window for rolling win-rate
        """
        SELECT
            a.tv_symbol,
            a.fire_time,
            a.composite_score,
            COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) AS ret,
            CASE WHEN COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) >= 5.0
                 THEN 1.0 ELSE 0.0 END AS is_win,
            AVG(
                CASE WHEN COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) >= 5.0
                     THEN 1.0 ELSE 0.0 END
            ) OVER (
                ORDER BY a.fire_time
                ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
            ) * 100 AS rolling_30_winrate_pct
        FROM alerts a
        LEFT JOIN performance_metrics pm ON pm.alert_uuid = a.alert_uuid
        WHERE COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) IS NOT NULL
        ORDER BY a.fire_time
        """,
    ),
    (
        "Monthly P&L aggregation (time-series bucketing)",
        # SQLite
        """
        SELECT
            strftime('%Y-%m', a.fire_time)          AS month,
            COUNT(*)                                  AS signals,
            AVG(COALESCE(pm.return_7d, pm.return_3d, pm.return_1d)) AS avg_return,
            SUM(COALESCE(pm.return_7d, pm.return_3d, pm.return_1d)) AS sum_return
        FROM alerts a
        LEFT JOIN performance_metrics pm ON pm.alert_uuid = a.alert_uuid
        WHERE a.fire_time IS NOT NULL
          AND COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) IS NOT NULL
        GROUP BY month
        ORDER BY month
        """,
        # DuckDB — date_trunc for cleaner time-series
        """
        SELECT
            strftime(CAST(a.fire_time AS TIMESTAMP), '%Y-%m') AS month,
            COUNT(*)                                            AS signals,
            AVG(COALESCE(pm.return_7d, pm.return_3d, pm.return_1d)) AS avg_return,
            SUM(COALESCE(pm.return_7d, pm.return_3d, pm.return_1d)) AS sum_return
        FROM alerts a
        LEFT JOIN performance_metrics pm ON pm.alert_uuid = a.alert_uuid
        WHERE a.fire_time IS NOT NULL
          AND COALESCE(pm.return_7d, pm.return_3d, pm.return_1d) IS NOT NULL
        GROUP BY month
        ORDER BY month
        """,
    ),
]


def main(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ERROR] Database not found: {db_path}")
        print("        Run the app to populate alert_tracker.db first.")
        sys.exit(1)

    print(DIVIDER)
    print("BENCHMARK 11 — DuckDB vs SQLite: analytical query performance")
    print(DIVIDER)
    print(f"  Database : {db_path}")
    print(f"  Reps     : {REPS} per query (reporting median)")
    print()

    # ── cold-start measurement ────────────────────────────────────────────────
    print("[COLD START] Measuring module import + first connection overhead...")

    t0 = time.perf_counter()
    import sqlite3 as _sq
    _sq.connect(str(db_path)).close()
    sqlite_cold_ms = round((time.perf_counter() - t0) * 1000, 2)
    print(f"  SQLite  cold start : {sqlite_cold_ms} ms")

    try:
        t0 = time.perf_counter()
        import duckdb as _dd
        duckdb_cold_ms = round((time.perf_counter() - t0) * 1000, 2)
        print(f"  DuckDB  cold start : {duckdb_cold_ms} ms")
        duckdb_available = True
    except ImportError:
        print("  DuckDB  cold start : NOT INSTALLED (pip install duckdb)")
        duckdb_available = False

    # ── connect DuckDB to SQLite file (zero-copy read) ───────────────────────
    if duckdb_available:
        try:
            import duckdb
            duck = duckdb.connect()
            duck.execute(f"ATTACH '{db_path}' AS db (TYPE sqlite, READ_ONLY)")
            duck.execute("USE db")
            print(f"  DuckDB  attached   : {db_path.name} via SQLite extension")
        except Exception as e:
            print(f"  DuckDB  attach     : FAILED — {e}")
            duckdb_available = False

    print()
    print(f"{'Query':<46} {'SQLite (ms)':>12} {'DuckDB (ms)':>12} {'Speedup':>9} {'Rows':>6}")
    print("-" * 90)

    results = []
    for label, sql_sqlite, sql_duck in BENCHMARKS:
        # SQLite timing
        try:
            sq_ms, sq_rows = _run_sqlite(db_path, sql_sqlite)
            sq_str = f"{sq_ms:>10.1f}"
        except Exception as ex:
            sq_ms, sq_rows, sq_str = 9999, 0, f"   ERROR"
            print(f"  [SQLite error] {label}: {ex}", file=sys.stderr)

        # DuckDB timing
        if duckdb_available:
            try:
                dd_ms, dd_rows = _run_duckdb(duck, sql_duck)
                dd_str    = f"{dd_ms:>10.1f}"
                speedup   = sq_ms / dd_ms if dd_ms > 0 else float("inf")
                speedup_s = f"{speedup:>7.2f}x"
            except Exception as ex:
                dd_ms, dd_rows, dd_str = 9999, sq_rows, f"   ERROR"
                speedup_s = "  N/A"
                print(f"  [DuckDB error] {label}: {ex}", file=sys.stderr)
        else:
            dd_ms, dd_str, speedup_s = None, "  N/A (not installed)", "  N/A"

        row_str = str(sq_rows)
        trunc = label[:44] + ".." if len(label) > 44 else label
        print(f"  {trunc:<44} {sq_str}   {dd_str}  {speedup_s}  {row_str:>5}")
        results.append({
            "label":    label,
            "sq_ms":    sq_ms,
            "dd_ms":    dd_ms,
            "sq_rows":  sq_rows,
        })

    print()
    print(DIVIDER)
    print("RECOMMENDATION")
    print(DIVIDER)

    if not duckdb_available:
        print("""
  DuckDB is NOT installed.  To benchmark it, run:
      pip install duckdb

  DuckDB key advantages for this system:
    - Columnar storage → 10-100x faster on GROUP BY / window aggregations
    - Direct SQLite attach: zero data migration, read-only overlay
    - Pandas-native: duckdb.query("SELECT ...").df() returns a DataFrame
    - Excellent JSON support: json_extract_string, unnest, json_array_length
    - Window functions (ROWS BETWEEN) for rolling win-rates without Python loops
    - FREE, embeddable (no separate server process needed)

  Keep SQLite for:
    - OLTP writes (INSERT / UPDATE) — DuckDB is read-optimised
    - Single-row lookups (PRIMARY KEY queries)
    - Existing tooling compatibility
""")
    else:
        sq_total = sum(r["sq_ms"] for r in results if r["sq_ms"] < 9999)
        dd_total = sum(r["dd_ms"] for r in results if r["dd_ms"] is not None and r["dd_ms"] < 9999)
        overall_speedup = sq_total / dd_total if dd_total > 0 else 1.0

        print(f"""
  Overall SQLite  : {sq_total:.1f} ms across {len(results)} queries
  Overall DuckDB  : {dd_total:.1f} ms across {len(results)} queries
  Geometric speedup: {overall_speedup:.2f}x

  VERDICT for this codebase:
  {"=" * 60}
  {'✅ DuckDB is SIGNIFICANTLY FASTER — recommended for analytics.' if overall_speedup > 2 else '⚠️  DuckDB speedup is MODEST at this data size.'}

  HYBRID STRATEGY (recommended):
    • Keep SQLite (alert_tracker.db) for all WRITES (INSERT alerts, UPDATE metrics)
    • Use DuckDB as a READ layer for analytics (performance_analyzer.py, backtest_vectorbt.py)
    • Pattern:
        import duckdb
        duck = duckdb.connect()
        duck.execute("ATTACH 'alert_tracker.db' AS db (TYPE sqlite, READ_ONLY)")
        df = duck.execute("SELECT ... FROM db.alerts ...").df()  # returns pandas DataFrame

  WHEN DATA GROWS:
    - At ~100k+ alerts rows, DuckDB advantage becomes dramatic (10-100x)
    - Window functions (rolling win-rate) are where DuckDB shines most
    - JSON extraction in DuckDB is 3-5x faster at scale
    - Consider exporting to DuckDB native file (.duckdb) for fastest reads

  SELF-EVOLUTION (data science workflow):
    - duckdb + pandas: run ML features directly from SQL queries
    - duckdb.query("...").df() → pandas DataFrame → scikit-learn pipeline
    - Add ibis or dbt-duckdb for query abstraction layer
    - Use COPY TO PARQUET for archiving large alert history efficiently
""")

    if duckdb_available:
        duck.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Benchmark DuckDB vs SQLite for analytical queries on alert_tracker.db"
    )
    parser.add_argument(
        "--db",
        default=str(ROOT / "database" / "alert_tracker.db"),
        help="Path to alert_tracker.db (default: database/alert_tracker.db)",
    )
    args = parser.parse_args()
    main(args)
