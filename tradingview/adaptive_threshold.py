"""
Adaptive Threshold Advisor (Idea 8)
=====================================
ADVISORY ONLY — does NOT modify the live composite threshold.

Analyzes sell_history.db trade outcomes across different hypothetical
composite score thresholds and reports projected win rates.

Limitations:
  - sell_history.db stores SELL-side data (gain_pct per position exit)
  - No direct link to the composite_score at entry time
  - This module uses conviction_score (0-100 range from sell engine)
    as a proxy for trade quality
  - For accurate threshold analysis, need to cross-reference with
    alert_tracker.db composite_score — future enhancement

Usage:
    python adaptive_threshold.py
    python adaptive_threshold.py --db path/to/sell_history.db
"""

import sys
import sqlite3
import argparse
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ─── Configuration ────────────────────────────────────────────────────────────
# Primary analysis database -- uses actual composite buy scores (H-03 fix)
_ALERTS_DB = Path(__file__).parent.parent / "database" / "alert_tracker.db"

_MIN_THRESHOLD = 40
_MAX_THRESHOLD = 70
_STEP = 5

# Guardrails
_MAX_CHANGE_PER_DAY = 5  # max threshold adjustment per day
_ABSOLUTE_MIN = 40       # never go below this
_ABSOLUTE_MAX = 70       # never go above this


def analyze_thresholds_from_alerts(db_path=None) -> dict:
    """
    Analyze composite score thresholds using actual buy signal outcomes from alert_tracker.db.

    This is the PRIMARY threshold analysis function (H-03 fix).
    Uses composite_score at entry and return_7d (actual return) to compute win rates.

    Win = return_7d >= 3.0 (3% profitable threshold).
    """
    db = db_path or _ALERTS_DB
    result = {
        "total_trades": 0,
        "win_rate": 0.0,
        "avg_gain": 0.0,
        "thresholds": [],
        "recommendation": "",
        "current_threshold": 55,
        "data_source": "alert_tracker.db",
    }

    if not db.exists():
        result["recommendation"] = f"alert_tracker.db not found at {db}. Using sell_history.db fallback."
        return result

    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        row = conn.execute("""
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN p.return_7d >= 3.0 THEN 1 ELSE 0 END) as wins,
                   AVG(p.return_7d) as avg_gain,
                   AVG(CASE WHEN p.return_7d >= 3.0 THEN p.return_7d END) as avg_win,
                   AVG(CASE WHEN p.return_7d < 3.0 THEN p.return_7d END) as avg_loss
            FROM alerts a
            JOIN performance_metrics p ON p.alert_uuid = a.alert_uuid
            WHERE a.composite_score IS NOT NULL AND p.return_7d IS NOT NULL
        """).fetchone()

        if not row or row["n"] == 0:
            result["recommendation"] = (
                "No alert data with composite_score + return_7d in alert_tracker.db. "
                "Add outcome tracking."
            )
            conn.close()
            return result

        total = row["n"]
        wins = row["wins"] or 0
        result["total_trades"] = total
        result["win_rate"] = round(wins / total * 100, 1) if total > 0 else 0.0
        result["avg_gain"] = round(row["avg_gain"], 2) if row["avg_gain"] else 0.0

        thresholds = []
        for t in range(_MIN_THRESHOLD, _MAX_THRESHOLD + 1, _STEP):
            row_t = conn.execute("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN p.return_7d >= 3.0 THEN 1 ELSE 0 END) as wins,
                       AVG(p.return_7d) as avg_gain,
                       AVG(CASE WHEN p.return_7d >= 3.0 THEN p.return_7d END) as avg_win,
                       AVG(CASE WHEN p.return_7d < 3.0 THEN p.return_7d END) as avg_loss
                FROM alerts a
                JOIN performance_metrics p ON p.alert_uuid = a.alert_uuid
                WHERE a.composite_score >= ? AND p.return_7d IS NOT NULL
            """, (t,)).fetchone()

            n_t    = (row_t["n"]    or 0) if row_t else 0
            wins_t = (row_t["wins"] or 0) if row_t else 0
            avg_g  = round(row_t["avg_gain"], 2) if row_t and row_t["avg_gain"] else 0.0
            avg_w  = round(row_t["avg_win"], 2) if row_t and row_t["avg_win"] else 0.0
            avg_l  = round(row_t["avg_loss"], 2) if row_t and row_t["avg_loss"] else 0.0

            thresholds.append({
                "threshold": t,
                "trades": n_t,
                "wins": wins_t,
                "win_rate": round(wins_t / n_t * 100, 1) if n_t > 0 else 0.0,
                "avg_gain": avg_g,
                "avg_win": avg_w,
                "avg_loss": avg_l,
            })

        result["thresholds"] = thresholds

        best = max(
            [t for t in thresholds if t["trades"] >= 10],
            key=lambda x: x["win_rate"],
            default=None,
        )
        if best:
            if best["threshold"] != 55:
                delta = best["threshold"] - 55
                capped = max(-_MAX_CHANGE_PER_DAY, min(_MAX_CHANGE_PER_DAY, delta))
                suggested = max(_ABSOLUTE_MIN, min(_ABSOLUTE_MAX, 55 + capped))
                result["recommendation"] = (
                    f"Advisory: Composite score analysis suggests threshold {best['threshold']} "
                    f"(WR={best['win_rate']:.1f}%, n={best['trades']}). "
                    f"Current=55. Capped adjustment: {suggested}."
                )
            else:
                result["recommendation"] = (
                    f"Current threshold 55 appears optimal "
                    f"(WR={best['win_rate']:.1f}%, n={best['trades']})."
                )
        else:
            result["recommendation"] = (
                f"Insufficient data for threshold analysis ({total} outcomes). "
                "Need >= 10 per bucket."
            )

        conn.close()
    except Exception as e:
        result["recommendation"] = f"Error querying alert_tracker.db: {e}"

    return result

def analyze_thresholds(db_path: Path | None = None) -> dict:
    """
    Analyze trade outcomes across conviction_score thresholds.

    Returns:
        dict with:
            total_trades:  int
            win_rate:      float (overall)
            avg_gain:      float (overall)
            thresholds:    list of dicts with threshold, trades, wins, win_rate, avg_gain
            recommendation: str (advisory text)
            current_threshold: int (55 — hardcoded baseline)
    """
    # Primary DB: alert_tracker.db composite_score (H-03 fix — sell_history.db is semantically wrong)
    if db_path is None:
        return analyze_thresholds_from_alerts()
    # Legacy path: explicit db_path provided by caller
    db = db_path
    result = {
        "total_trades": 0,
        "win_rate": 0.0,
        "avg_gain": 0.0,
        "thresholds": [],
        "recommendation": "",
        "current_threshold": 55,
        "data_source": "sell_history.db",
    }

    if not db.exists():
        result["recommendation"] = f"sell_history.db not found at {db}"
        return result

    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        # Overall stats
        row = conn.execute("""
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN gain_pct > 0 THEN 1 ELSE 0 END) as wins,
                   AVG(gain_pct) as avg_gain
            FROM sell_events
            WHERE gain_pct IS NOT NULL
        """).fetchone()

        if not row or row["n"] == 0:
            result["recommendation"] = "No trade data with gain_pct in sell_history.db"
            conn.close()
            return result

        total = row["n"]
        wins = row["wins"]
        result["total_trades"] = total
        result["win_rate"] = round(wins / total * 100, 1) if total > 0 else 0.0
        result["avg_gain"] = round(row["avg_gain"], 2) if row["avg_gain"] else 0.0

        # Threshold analysis using conviction_score as proxy.
        # conviction_score in sell_history.db is on 0-10 scale (sell urgency).
        # Normalize by ×10 so thresholds 40-70 map to conviction 4.0-7.0.
        thresholds = []
        for t in range(_MIN_THRESHOLD, _MAX_THRESHOLD + 1, _STEP):
            row_t = conn.execute("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN gain_pct > 0 THEN 1 ELSE 0 END) as wins,
                       AVG(gain_pct) as avg_gain,
                       AVG(CASE WHEN gain_pct > 0 THEN gain_pct END) as avg_win,
                       AVG(CASE WHEN gain_pct <= 0 THEN gain_pct END) as avg_loss
                FROM sell_events
                WHERE conviction_score * 10 >= ? AND gain_pct IS NOT NULL
            """, (t,)).fetchone()

            n_t    = (row_t["n"]    or 0) if row_t else 0
            wins_t = (row_t["wins"] or 0) if row_t else 0
            avg_g = round(row_t["avg_gain"], 2) if row_t and row_t["avg_gain"] else 0.0
            avg_w = round(row_t["avg_win"], 2) if row_t and row_t["avg_win"] else 0.0
            avg_l = round(row_t["avg_loss"], 2) if row_t and row_t["avg_loss"] else 0.0

            thresholds.append({
                "threshold": t,
                "trades": n_t,
                "wins": wins_t,
                "win_rate": round(wins_t / n_t * 100, 1) if n_t > 0 else 0.0,
                "avg_gain": avg_g,
                "avg_win": avg_w,
                "avg_loss": avg_l,
            })

        result["thresholds"] = thresholds

        # Find optimal threshold (max win_rate with sufficient samples)
        best = max(
            [t for t in thresholds if t["trades"] >= 20],
            key=lambda x: x["win_rate"],
            default=None,
        )
        if best:
            if best["threshold"] != 55:
                delta = best["threshold"] - 55
                capped = max(-_MAX_CHANGE_PER_DAY, min(_MAX_CHANGE_PER_DAY, delta))
                suggested = max(_ABSOLUTE_MIN, min(_ABSOLUTE_MAX, 55 + capped))
                result["recommendation"] = (
                    f"Advisory: Conviction-based analysis suggests threshold {best['threshold']} "
                    f"(WR={best['win_rate']:.1f}%, n={best['trades']}). "
                    f"Current=55. Capped adjustment: {suggested}. "
                    f"⚠ Uses conviction_score proxy, not composite_score at entry. "
                    f"Review before manual adjustment."
                )
            else:
                result["recommendation"] = (
                    f"Current threshold 55 appears optimal "
                    f"(WR={best['win_rate']:.1f}%, n={best['trades']}). No change needed."
                )
        else:
            result["recommendation"] = (
                "Insufficient data (need ≥20 trades per threshold level) for recommendation."
            )

        conn.close()
    except Exception as exc:
        result["recommendation"] = f"Error analyzing thresholds: {exc}"

    return result


def print_analysis(db_path: Path | None = None) -> None:
    """Print threshold analysis to console."""
    analysis = analyze_thresholds(db_path)

    print(f"\n{'='*70}")
    print(f"  📊 Adaptive Threshold Analysis (ADVISORY ONLY)")
    print(f"{'='*70}")
    if analysis.get("data_source", "sell_history.db") != "alert_tracker.db":
        print(f"  ⚠ METRIC MISMATCH: This tool uses conviction_score (sell urgency,")
        print(f"    0-10 scale) as a proxy for composite_score (buy quality, 0-100).")
        print(f"    Results may not reflect actual buy-signal quality.")
        print(f"{'='*70}")
    print(f"  Total trades: {analysis['total_trades']}")
    print(f"  Overall WR:   {analysis['win_rate']:.1f}%")
    print(f"  Avg gain:     {analysis['avg_gain']:+.2f}%")
    print(f"  Current threshold: {analysis['current_threshold']}")
    print()

    if analysis["thresholds"]:
        print(f"  {'Threshold':>10}  {'Trades':>8}  {'Wins':>6}  {'WR%':>7}  {'AvgGain':>9}  {'AvgWin':>8}  {'AvgLoss':>8}")
        print(f"  {'-'*10}  {'-'*8}  {'-'*6}  {'-'*7}  {'-'*9}  {'-'*8}  {'-'*8}")
        for t in analysis["thresholds"]:
            marker = " ◀ current" if t["threshold"] == 55 else ""
            print(f"  {t['threshold']:>10}  {t['trades']:>8}  {t['wins']:>6}  {t['win_rate']:>6.1f}%  "
                  f"{t['avg_gain']:>+8.2f}%  {t.get('avg_win', 0.0):>+7.2f}%  {t.get('avg_loss', 0.0):>+7.2f}%{marker}")

    print()
    print(f"  💡 {analysis['recommendation']}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Adaptive Threshold Advisor (ADVISORY ONLY)")
    parser.add_argument("--db", type=Path, default=None,
                        help="Path to sell_history.db (default: auto-detect)")
    args = parser.parse_args()
    print_analysis(args.db)
