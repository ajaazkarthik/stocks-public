#!/usr/bin/env python3
"""
Rolling Signal Quality Monitor
================================
Detects regime shifts where the signal stack loses predictive power.

Compares 30-day win rates vs 90-day win rates per score bucket.
If the 30-day rate drops ≥10 points below the 90-day baseline, it prints
a WARNING — indicating the signal model may need recalibration.

Usage:
    python database/signal_quality_monitor.py [--db <path>] [--out <html>]

Can also be imported and called from main.py / morning_run().
"""
import sys
import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent.resolve()
_DEFAULT_DB = ROOT / "database" / "alert_tracker.db"

try:
    from database.queries import WIN_THRESHOLD_PCT
except ImportError:  # direct script run puts database/ on sys.path, not ROOT
    from queries import WIN_THRESHOLD_PCT

# ── Score bucketing ──────────────────────────────────────────────────────────
# composite_score is persisted on a 0-100 scale; bounds mirror backtest_vectorbt.py.
_BUCKETS = [
    ("APEX", 75.0, 100.1),
    ("HIGH", 65.0, 75.0),
    ("MED",  55.0, 65.0),
    ("LOW",   0.0, 55.0),
]


def _query_win_rates(conn: sqlite3.Connection, days: int) -> dict[str, dict]:
    """
    Compute win rates per score bucket over the last `days` days.
    Uses the canonical win definition from database.queries (return_7d >= 3%)
    so this monitor cannot diverge from alert_tracker / performance_analyzer.
    Returns {bucket_label: {n, wins, win_rate}}.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn.cursor()
    results = {}
    for label, lo, hi in _BUCKETS:
        cur.execute(
            """
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN pm.return_7d >= ? THEN 1 ELSE 0 END) as wins
            FROM   alerts a
            JOIN   performance_metrics pm ON a.alert_uuid = pm.alert_uuid
            WHERE  a.created_at >= ?
              AND  a.composite_score >= ?
              AND  a.composite_score < ?
              AND  pm.status IN ('success', 'failure', 'expired')
              AND  pm.return_7d IS NOT NULL
            """,
            (WIN_THRESHOLD_PCT, cutoff, lo, hi),
        )
        row = cur.fetchone()
        n    = row[0] or 0
        wins = row[1] or 0
        results[label] = {
            "n":        n,
            "wins":     wins,
            "win_rate": round(wins / n * 100, 1) if n >= 5 else None,
        }
    return results


def run_signal_quality_check(db_path: Path | None = None) -> list[str]:
    """
    Compare 30-day vs 90-day win rates.  Prints results and returns a list of
    WARNING strings (empty list = no degradation detected).

    Parameters
    ----------
    db_path : Path | None
        Path to alert_tracker.db.  Defaults to the standard location.

    Returns
    -------
    list[str]
        Warning messages (empty = healthy signal).
    """
    db_path = db_path or _DEFAULT_DB
    if not db_path.exists():
        print(f"[WARN] signal_quality_monitor: DB not found at {db_path}", flush=True)
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        r30 = _query_win_rates(conn, 30)
        r90 = _query_win_rates(conn, 90)
    finally:
        conn.close()

    warnings: list[str] = []
    print("\n  ╔══════════════════════════════════════════════════════╗")
    print("  ║   Rolling Signal Quality Monitor (30d vs 90d)       ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    print(f"  {'Bucket':<6}  {'30d n':>5}  {'30d %':>6}  {'90d n':>5}  {'90d %':>6}  {'Delta':>7}  Status")
    print("  " + "─" * 60)

    for label, _, _ in _BUCKETS:
        w30 = r30[label]
        w90 = r90[label]
        wr30 = w30["win_rate"]
        wr90 = w90["win_rate"]

        if wr30 is None or wr90 is None:
            status = "insufficient data"
            delta_str = "N/A"
        else:
            delta = wr30 - wr90
            delta_str = f"{delta:+.1f}%"
            if delta <= -10:
                status = "!! DEGRADED"
                msg = (f"[SIGNAL QUALITY] Bucket {label}: 30d win rate {wr30:.1f}% "
                       f"vs 90d {wr90:.1f}% — delta {delta:.1f}% — model may need recalibration")
                warnings.append(msg)
            elif delta <= -5:
                status = "~ softening"
            else:
                status = "OK"

        wr30_str = f"{wr30:.1f}%" if wr30 is not None else "n/a"
        wr90_str = f"{wr90:.1f}%" if wr90 is not None else "n/a"
        print(f"  {label:<6}  {w30['n']:>5}  {wr30_str:>6}  {w90['n']:>5}  {wr90_str:>6}  {delta_str:>7}  {status}")

    print()
    if warnings:
        for w in warnings:
            print(f"  !! {w}")
    else:
        print("  [OK] No significant signal degradation detected.")
    print()
    return warnings


def _write_html(out_path: Path, r30: dict, r90: dict, warnings: list[str]) -> None:
    """Write a minimal HTML report with the signal quality table."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    status_cls = "warn" if warnings else "ok"
    status_txt = "⚠ Degraded" if warnings else "✓ Healthy"
    rows_html = ""
    for label, _, _ in _BUCKETS:
        w30 = r30[label]
        w90 = r90[label]
        wr30 = w30["win_rate"]
        wr90 = w90["win_rate"]
        if wr30 is None or wr90 is None:
            delta_str, row_cls = "N/A", "dim"
        else:
            delta = wr30 - wr90
            delta_str = f"{delta:+.1f}%"
            row_cls = "degraded" if delta <= -10 else ("soft" if delta <= -5 else "ok")
        wr30_str = f"{wr30:.1f}%" if wr30 is not None else "—"
        wr90_str = f"{wr90:.1f}%" if wr90 is not None else "—"
        rows_html += (
            f"<tr class='{row_cls}'><td>{label}</td>"
            f"<td class='num'>{w30['n']}</td><td class='num'>{wr30_str}</td>"
            f"<td class='num'>{w90['n']}</td><td class='num'>{wr90_str}</td>"
            f"<td class='num'>{delta_str}</td></tr>\n"
        )
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warn_html = f"<div class='warnings'><ul>{items}</ul></div>"

    html = f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Quality Monitor — {now_str}</title>
<style>
body{{font-family:monospace;background:#111;color:#ccc;padding:20px}}
h2{{color:#eee}}table{{border-collapse:collapse;margin-top:12px}}
th,td{{padding:6px 12px;border:1px solid #333;text-align:left}}
th{{background:#1e1e1e;color:#888}}
.num{{text-align:right}}
.ok td{{color:#4caf50}}.degraded td{{color:#f44336}}.soft td{{color:#ff9800}}.dim td{{color:#666}}
.badge-ok{{color:#4caf50}}.badge-warn{{color:#f44336}}
.warnings{{background:#1e1e1e;border:1px solid #f44336;padding:10px;margin-top:12px;border-radius:4px}}
</style></head><body>
<h2>Rolling Signal Quality Monitor (30d vs 90d)</h2>
<p>Generated: {now_str} | Status: <strong class='badge-{status_cls}'>{status_txt}</strong></p>
<table>
<tr><th>Bucket</th><th>30d n</th><th>30d WR%</th><th>90d n</th><th>90d WR%</th><th>Delta</th></tr>
{rows_html}</table>
{warn_html}
</body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rolling Signal Quality Monitor")
    parser.add_argument("--db",  default=str(_DEFAULT_DB), help="Path to alert_tracker.db")
    parser.add_argument("--out", default="",               help="Output HTML report path")
    args = parser.parse_args()
    db_path = Path(args.db)
    conn_ = sqlite3.connect(str(db_path)) if db_path.exists() else None
    if conn_:
        r30_ = _query_win_rates(conn_, 30)
        r90_ = _query_win_rates(conn_, 90)
        conn_.close()
    else:
        r30_ = r90_ = {label: {"n": 0, "wins": 0, "win_rate": None} for label, _, _ in _BUCKETS}
    warns_ = run_signal_quality_check(db_path)
    if args.out:
        _write_html(Path(args.out), r30_, r90_, warns_)
