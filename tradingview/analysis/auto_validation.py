"""
Auto-Validation Loop — Weekly signal quality validator.
========================================================
Runs weekly (or on-demand) to validate signal scoring accuracy by:
1. Computing Spearman correlation between composite_score and return_7d
2. Checking per-bucket win rates (LOW/MED/HIGH/APEX)
3. Detecting score drift (recent accuracy vs historical)
4. Generating an HTML quality dashboard

Data source: alert_tracker.db
Output:      tradingview/reports/auto_validation_report.html

Usage:
    python tradingview/analysis/auto_validation.py
    python tradingview/analysis/auto_validation.py --weeks 4 --min-samples 100
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_THIS_DIR = Path(__file__).resolve().parent
_TV = _THIS_DIR.parent
_ROOT = _TV.parent
sys.path.insert(0, str(_ROOT))

try:
    from path_config import DATABASE, REPORTS
except ImportError:
    DATABASE = _ROOT / "database"
    REPORTS = _ROOT / "reports"

DB_PATH = DATABASE / "alert_tracker.db"
DEFAULT_OUT = REPORTS / "auto_validation_report.html"

# Score bucket definitions
BUCKETS = {
    "LOW": (0, 45),
    "MED": (45, 55),
    "HIGH": (55, 70),
    "APEX": (70, 100),
}


def _spearman_rank(x: list[float], y: list[float]) -> float:
    """Compute Spearman rank correlation coefficient (pure Python)."""
    n = len(x)
    if n < 3:
        return 0.0

    def rank(arr: list[float]) -> list[float]:
        sorted_indices = sorted(range(n), key=lambda i: arr[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and arr[sorted_indices[j + 1]] == arr[sorted_indices[j]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[sorted_indices[k]] = avg_rank
            i = j + 1
        return ranks

    rx = rank(x)
    ry = rank(y)

    d_sq_sum = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - (6 * d_sq_sum) / (n * (n ** 2 - 1))


def load_validation_data(
    db_path: Path = DB_PATH,
    weeks: int = 12,
    min_samples: int = 30,
) -> tuple[list[dict], list[dict]]:
    """
    Load alert data for validation.

    Returns:
        (all_data, recent_data) - all rows, and rows from the last `weeks` weeks
    """
    if not db_path.exists():
        return [], []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Check columns
    cursor = conn.execute("PRAGMA table_info(alerts)")
    columns = {row["name"] for row in cursor.fetchall()}

    score_col = "composite_score" if "composite_score" in columns else None
    return_col = "return_7d" if "return_7d" in columns else None
    date_col = "timestamp" if "timestamp" in columns else "date" if "date" in columns else None

    if not score_col or not return_col:
        conn.close()
        return [], []

    query = f"""
        SELECT *
        FROM alerts
        WHERE {score_col} IS NOT NULL
          AND {return_col} IS NOT NULL
    """

    all_rows = [dict(row) for row in conn.execute(query).fetchall()]
    conn.close()

    if len(all_rows) < min_samples:
        return all_rows, []

    # Split into recent vs all
    cutoff = datetime.now() - timedelta(weeks=weeks)
    recent = []
    for row in all_rows:
        if date_col and row.get(date_col):
            try:
                ts = row[date_col]
                if isinstance(ts, str):
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            dt = datetime.strptime(ts, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                elif isinstance(ts, (int, float)):
                    dt = datetime.fromtimestamp(ts)
                else:
                    continue

                if dt >= cutoff:
                    recent.append(row)
            except Exception:
                continue
        else:
            recent.append(row)

    return all_rows, recent


def compute_bucket_stats(rows: list[dict]) -> dict:
    """Compute per-bucket win rates and stats."""
    score_col = "composite_score"
    return_col = "return_7d"

    bucket_data: dict = {}
    for name, (lo, hi) in BUCKETS.items():
        bucket_rows = [r for r in rows if lo <= float(r.get(score_col, 0) or 0) < hi]
        wins = [r for r in bucket_rows if float(r.get(return_col, 0) or 0) > 0]
        losses = [r for r in bucket_rows if float(r.get(return_col, 0) or 0) <= 0]

        returns = [float(r.get(return_col, 0) or 0) for r in bucket_rows]
        avg_return = sum(returns) / len(returns) if returns else 0
        total = len(bucket_rows)

        bucket_data[name] = {
            "count": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / total * 100 if total > 0 else 0,
            "avg_return": avg_return,
        }

    return bucket_data


def compute_spearman(rows: list[dict]) -> float:
    """Compute Spearman correlation between score and return."""
    scores = [float(r.get("composite_score", 0) or 0) for r in rows]
    returns = [float(r.get("return_7d", 0) or 0) for r in rows]
    return _spearman_rank(scores, returns)


def detect_drift(all_data: list[dict], recent_data: list[dict]) -> dict:
    """Detect score quality drift between all-time and recent."""
    all_stats = compute_bucket_stats(all_data) if all_data else {}
    recent_stats = compute_bucket_stats(recent_data) if recent_data else {}
    all_spearman = compute_spearman(all_data) if all_data else 0
    recent_spearman = compute_spearman(recent_data) if recent_data else 0

    drift = {
        "all_spearman": all_spearman,
        "recent_spearman": recent_spearman,
        "spearman_delta": recent_spearman - all_spearman,
        "bucket_drift": {},
    }

    for bucket in BUCKETS:
        all_wr = all_stats.get(bucket, {}).get("win_rate", 0)
        recent_wr = recent_stats.get(bucket, {}).get("win_rate", 0)
        drift["bucket_drift"][bucket] = {
            "all_win_rate": all_wr,
            "recent_win_rate": recent_wr,
            "delta": recent_wr - all_wr,
        }

    return drift


def assess_health(all_spearman: float, recent_spearman: float, apex_wr: float) -> tuple[str, str]:
    """
    Assess overall signal quality health.

    Returns:
        (status, message) - status is 'HEALTHY', 'WARNING', or 'DEGRADED'
    """
    issues = []

    if all_spearman < 0.05:
        issues.append("All-time Spearman < 0.05 (scores have no predictive value)")
    if recent_spearman < 0.0:
        issues.append("Recent Spearman is NEGATIVE (scores are inversely predictive!)")
    if apex_wr < 50:
        issues.append(f"APEX win rate is {apex_wr:.0f}% (should be >60%)")

    if len(issues) >= 2:
        return "DEGRADED", "Signal quality is degraded. " + " | ".join(issues)
    elif len(issues) == 1:
        return "WARNING", issues[0]
    else:
        return "HEALTHY", "Signal scoring is performing within expected parameters."


def generate_validation_html(
    all_stats: dict,
    recent_stats: dict,
    drift: dict,
    health_status: str,
    health_message: str,
    n_all: int,
    n_recent: int,
    output_path: Path = DEFAULT_OUT,
) -> Path:
    """Generate validation HTML report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    status_color = {"HEALTHY": "#3fb950", "WARNING": "#d29922", "DEGRADED": "#f85149"}.get(health_status, "#8b949e")
    status_icon = {"HEALTHY": "✅", "WARNING": "⚠️", "DEGRADED": "🔴"}.get(health_status, "❓")

    # Build bucket comparison table
    bucket_rows = ""
    for bucket in BUCKETS:
        a = all_stats.get(bucket, {})
        r = recent_stats.get(bucket, {})
        d = drift.get("bucket_drift", {}).get(bucket, {})
        delta = d.get("delta", 0)
        delta_color = "#3fb950" if delta > 0 else "#f85149" if delta < -5 else "#8b949e"

        bucket_rows += f"""<tr>
  <td><strong>{bucket}</strong></td>
  <td>{a.get('count', 0)}</td>
  <td>{a.get('win_rate', 0):.1f}%</td>
  <td>{a.get('avg_return', 0):.2f}%</td>
  <td>{r.get('count', 0)}</td>
  <td>{r.get('win_rate', 0):.1f}%</td>
  <td style="color:{delta_color}">{delta:+.1f}%</td>
</tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Signal Auto-Validation</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; background: #0d1117; color: #c9d1d9; }}
  h1,h2 {{ color: #58a6ff; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 20px 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }}
  .card .value {{ font-size: 1.8em; font-weight: bold; }}
  .card .label {{ font-size: 0.85em; color: #8b949e; }}
  .ok {{ color: #3fb950; }}
  .warn {{ color: #d29922; }}
  .danger {{ color: #f85149; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th,td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: center; }}
  th {{ background: #161b22; color: #58a6ff; }}
  .status {{ font-size: 1.2em; padding: 12px; border-radius: 8px; background: #161b22; border: 2px solid {status_color}; }}
  footer {{ margin-top: 20px; color: #484f58; font-size: 0.85em; }}
</style>
</head><body>
<h1>📊 Signal Auto-Validation</h1>

<div class="status">
  {status_icon} <span style="color:{status_color}; font-weight:bold">{health_status}</span> — {health_message}
</div>

<div class="grid">
  <div class="card"><div class="value">{n_all}</div><div class="label">Total Samples</div></div>
  <div class="card"><div class="value">{n_recent}</div><div class="label">Recent Samples</div></div>
  <div class="card"><div class="value {'ok' if drift.get('all_spearman',0) > 0.1 else 'warn'}">{drift.get('all_spearman', 0):.3f}</div><div class="label">All-Time Spearman</div></div>
  <div class="card"><div class="value {'ok' if drift.get('recent_spearman',0) > 0.1 else 'danger' if drift.get('recent_spearman',0) < 0 else 'warn'}">{drift.get('recent_spearman', 0):.3f}</div><div class="label">Recent Spearman</div></div>
</div>

<h2>Bucket Performance (All-Time vs Recent)</h2>
<table>
<thead><tr>
  <th>Bucket</th>
  <th>All Count</th><th>All Win%</th><th>All Avg Return</th>
  <th>Recent Count</th><th>Recent Win%</th><th>Win% Δ</th>
</tr></thead>
<tbody>{bucket_rows}</tbody>
</table>

<h2>Drift Analysis</h2>
<p>Spearman correlation delta: <span style="color:{'#3fb950' if drift.get('spearman_delta',0) > 0 else '#f85149'}">{drift.get('spearman_delta', 0):+.3f}</span></p>
<p>{'📈 Signal quality is improving' if drift.get('spearman_delta', 0) > 0.02 else '📉 Signal quality is declining' if drift.get('spearman_delta', 0) < -0.02 else '➡️ Signal quality is stable'}</p>

<footer>Auto-Validation v1.0 | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</footer>
</body></html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"✅ Validation report saved to {output_path}")
    return output_path


def run_validation(
    db_path: Path = DB_PATH,
    weeks: int = 12,
    min_samples: int = 30,
    output_path: Path = DEFAULT_OUT,
) -> dict:
    """
    Run the complete validation pipeline.

    Returns:
        Summary dict with health status and key metrics.
    """
    all_data, recent_data = load_validation_data(db_path, weeks, min_samples)

    if not all_data:
        print("❌ No validation data available")
        return {"status": "NO_DATA", "message": "No data in alert_tracker.db"}

    all_stats = compute_bucket_stats(all_data)
    recent_stats = compute_bucket_stats(recent_data) if recent_data else {}
    drift = detect_drift(all_data, recent_data)
    apex_wr = all_stats.get("APEX", {}).get("win_rate", 0)
    health_status, health_message = assess_health(
        drift["all_spearman"],
        drift["recent_spearman"],
        apex_wr,
    )

    generate_validation_html(
        all_stats, recent_stats, drift,
        health_status, health_message,
        len(all_data), len(recent_data),
        output_path,
    )

    return {
        "status": health_status,
        "message": health_message,
        "n_all": len(all_data),
        "n_recent": len(recent_data),
        "all_spearman": drift["all_spearman"],
        "recent_spearman": drift["recent_spearman"],
        "apex_win_rate": apex_wr,
    }


def main(args: Optional[argparse.Namespace] = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Signal Auto-Validation")
        parser.add_argument("--weeks", type=int, default=12, help="Recent window in weeks")
        parser.add_argument("--min-samples", type=int, default=30, help="Minimum samples required")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output HTML path")
        args = parser.parse_args()

    print("📊 Running signal auto-validation...")
    result = run_validation(DB_PATH, args.weeks, args.min_samples, Path(args.out))
    print(f"  Status: {result.get('status', 'UNKNOWN')}")
    print(f"  {result.get('message', '')}")


if __name__ == "__main__":
    main()
