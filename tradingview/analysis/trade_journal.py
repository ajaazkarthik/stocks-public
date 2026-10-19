"""
Trade Journal — Auto-log every signal with outcome tracking.
=============================================================
Records every signal that reaches the actionable threshold (≥55 composite)
with full L1–L4 breakdown, price at alert time, broker routing, and tracks
7/14/30-day outcomes for performance analysis.

Data source: alert_tracker.db
Output:      tradingview/reports/trade_journal.html

Usage:
    python tradingview/analysis/trade_journal.py
    python tradingview/analysis/trade_journal.py --days 30 --out reports/trade_journal.html
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

# ── Windows: force UTF-8 console ───────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Path setup ─────────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_TV = _THIS_DIR.parent
_ROOT = _TV.parent

sys.path.insert(0, str(_ROOT))
try:
    from path_config import DATABASE, REPORTS, TV, ensure_data_dirs
except ImportError:
    DATABASE = _ROOT / "database"
    REPORTS = _ROOT / "reports"
    TV = _ROOT / "tradingview"

    def ensure_data_dirs():
        DATABASE.mkdir(parents=True, exist_ok=True)
        REPORTS.mkdir(parents=True, exist_ok=True)

DB_PATH = DATABASE / "alert_tracker.db"
DEFAULT_OUT = REPORTS / "trade_journal.html"

# ── Journal entry model ───────────────────────────────────────────────────────

class JournalEntry:
    """Represents a single trade journal entry."""

    __slots__ = (
        "symbol", "alert_time", "composite_score", "l1_technical", "l2_fundamental",
        "l3_sentiment", "l4_market", "price_at_alert", "broker_routed",
        "action_taken", "return_7d", "return_14d", "return_30d", "notes",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @property
    def outcome_7d(self) -> str:
        if self.return_7d is None:
            return "pending"
        return "win" if self.return_7d > 0 else "loss"


# ── Database queries ──────────────────────────────────────────────────────────

def _get_db_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Get a read-only database connection."""
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_journal_entries(
    db_path: Path = DB_PATH,
    days: int = 90,
    min_score: float = 0.0,
) -> list[JournalEntry]:
    """
    Fetch trade journal entries from alert_tracker.db.

    Args:
        db_path: Path to the SQLite database
        days: Number of days to look back
        min_score: Minimum composite score filter

    Returns:
        List of JournalEntry objects sorted by date descending
    """
    conn = _get_db_connection(db_path)
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        # Try to get column info first
        cursor = conn.execute("PRAGMA table_info(alerts)")
        columns = {row["name"] for row in cursor.fetchall()}

        # Build query based on available columns
        select_cols = ["symbol"]
        if "timestamp" in columns:
            select_cols.append("timestamp as alert_time")
        elif "alert_time" in columns:
            select_cols.append("alert_time")
        else:
            select_cols.append("'' as alert_time")

        if "composite_score" in columns:
            select_cols.append("composite_score")
        elif "score" in columns:
            select_cols.append("score as composite_score")
        else:
            select_cols.append("0 as composite_score")

        # Score components
        for col, alias in [
            ("l1_technical", "l1_technical"),
            ("l2_fundamental", "l2_fundamental"),
            ("l3_sentiment", "l3_sentiment"),
            ("l4_market", "l4_market"),
            ("L1_score", "l1_technical"),
            ("L2_score", "l2_fundamental"),
            ("L3_score", "l3_sentiment"),
            ("L4_score", "l4_market"),
        ]:
            if col in columns:
                select_cols.append(f'"{col}" as {alias}')

        # Price at alert
        if "close" in columns:
            select_cols.append("close as price_at_alert")
        elif "price" in columns:
            select_cols.append("price as price_at_alert")
        else:
            select_cols.append("NULL as price_at_alert")

        # Return columns
        for col in ["return_7d", "return_14d", "return_30d"]:
            if col in columns:
                select_cols.append(col)
            else:
                select_cols.append(f"NULL as {col}")

        # Broker column
        if "broker" in columns:
            select_cols.append("broker as broker_routed")
        elif "buy_vehicle" in columns:
            select_cols.append("buy_vehicle as broker_routed")
        else:
            select_cols.append("NULL as broker_routed")

        time_col = "timestamp" if "timestamp" in columns else "alert_time" if "alert_time" in columns else None
        score_col = "composite_score" if "composite_score" in columns else "score" if "score" in columns else None

        # Remove duplicates from select_cols (keep first occurrence of each alias)
        seen_aliases = set()
        deduped = []
        for c in select_cols:
            alias = c.split(" as ")[-1].strip().strip('"') if " as " in c.lower() else c
            if alias not in seen_aliases:
                seen_aliases.add(alias)
                deduped.append(c)
        select_cols = deduped

        query = f"SELECT {', '.join(select_cols)} FROM alerts"
        wheres = []
        params = []

        if time_col:
            wheres.append(f"{time_col} >= ?")
            params.append(cutoff)
        if score_col:
            wheres.append(f"{score_col} >= ?")
            params.append(min_score)

        if wheres:
            query += " WHERE " + " AND ".join(wheres)

        if time_col:
            query += f" ORDER BY {time_col} DESC"

        rows = conn.execute(query, params).fetchall()

        entries = []
        for row in rows:
            d = dict(row)  # sqlite3.Row → dict for safe .get()
            entry = JournalEntry(
                symbol=d["symbol"],
                alert_time=d.get("alert_time"),
                composite_score=d.get("composite_score"),
                l1_technical=d.get("l1_technical"),
                l2_fundamental=d.get("l2_fundamental"),
                l3_sentiment=d.get("l3_sentiment"),
                l4_market=d.get("l4_market"),
                price_at_alert=d.get("price_at_alert"),
                broker_routed=d.get("broker_routed"),
                return_7d=d.get("return_7d"),
                return_14d=d.get("return_14d"),
                return_30d=d.get("return_30d"),
            )
            entries.append(entry)

        return entries
    finally:
        conn.close()


def compute_journal_stats(entries: list[JournalEntry]) -> dict:
    """Compute summary statistics from journal entries."""
    if not entries:
        return {
            "total_signals": 0, "win_rate_7d": 0.0, "avg_return_7d": 0.0,
            "avg_score": 0.0, "best_symbol": "", "worst_symbol": "",
            "total_wins": 0, "total_losses": 0, "pending": 0,
        }

    scores = [e.composite_score for e in entries if e.composite_score is not None]
    returns_7d = [e.return_7d for e in entries if e.return_7d is not None]
    wins = [r for r in returns_7d if r > 0]
    losses = [r for r in returns_7d if r <= 0]
    pending = len(entries) - len(returns_7d)

    # Best and worst
    with_returns = [(e.symbol, e.return_7d) for e in entries if e.return_7d is not None]
    best = max(with_returns, key=lambda x: x[1]) if with_returns else ("", 0)
    worst = min(with_returns, key=lambda x: x[1]) if with_returns else ("", 0)

    return {
        "total_signals": len(entries),
        "win_rate_7d": len(wins) / len(returns_7d) * 100 if returns_7d else 0.0,
        "avg_return_7d": sum(returns_7d) / len(returns_7d) if returns_7d else 0.0,
        "avg_score": sum(scores) / len(scores) if scores else 0.0,
        "best_symbol": f"{best[0]} (+{best[1]:.1f}%)" if best[0] else "",
        "worst_symbol": f"{worst[0]} ({worst[1]:.1f}%)" if worst[0] else "",
        "total_wins": len(wins),
        "total_losses": len(losses),
        "pending": pending,
    }


# ── HTML Report ───────────────────────────────────────────────────────────────

def generate_journal_html(
    entries: list[JournalEntry],
    stats: dict,
    output_path: Path = DEFAULT_OUT,
) -> Path:
    """Generate an HTML trade journal report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_html = ""
    for e in entries:
        score_class = "apex" if (e.composite_score or 0) >= 75 else \
                      "high" if (e.composite_score or 0) >= 65 else \
                      "med" if (e.composite_score or 0) >= 55 else "low"

        outcome_class = ""
        outcome_text = "⏳"
        if e.return_7d is not None:
            if e.return_7d > 0:
                outcome_class = "win"
                outcome_text = f"+{e.return_7d:.1f}%"
            else:
                outcome_class = "loss"
                outcome_text = f"{e.return_7d:.1f}%"

        rows_html += f"""<tr>
  <td>{e.symbol or ''}</td>
  <td>{(e.alert_time or '')[:19]}</td>
  <td class='{score_class}'>{e.composite_score or 0:.1f}</td>
  <td>{e.l1_technical or 0:.1f}</td>
  <td>{e.l2_fundamental or 0:.1f}</td>
  <td>{e.l3_sentiment or 0:.1f}</td>
  <td>{e.l4_market or 0:.1f}</td>
  <td>{f'${e.price_at_alert:.2f}' if e.price_at_alert else '—'}</td>
  <td>{e.broker_routed or '—'}</td>
  <td class='{outcome_class}'>{outcome_text}</td>
</tr>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Trade Journal</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; background: #0d1117; color: #c9d1d9; }}
  h1 {{ color: #58a6ff; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
  .stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }}
  .stat-card .value {{ font-size: 1.8em; font-weight: bold; color: #58a6ff; }}
  .stat-card .label {{ font-size: 0.85em; color: #8b949e; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
  th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: center; }}
  th {{ background: #161b22; color: #58a6ff; }}
  tr:hover {{ background: #1c2128; }}
  .apex {{ color: #f0883e; font-weight: bold; }}
  .high {{ color: #3fb950; font-weight: bold; }}
  .med {{ color: #d29922; }}
  .low {{ color: #8b949e; }}
  .win {{ color: #3fb950; font-weight: bold; }}
  .loss {{ color: #f85149; font-weight: bold; }}
  footer {{ margin-top: 20px; color: #484f58; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>📓 Trade Journal</h1>
<p>Signals with outcomes — last {stats['total_signals']} entries</p>

<div class="stats">
  <div class="stat-card"><div class="value">{stats['total_signals']}</div><div class="label">Total Signals</div></div>
  <div class="stat-card"><div class="value">{stats['win_rate_7d']:.1f}%</div><div class="label">Win Rate (7d)</div></div>
  <div class="stat-card"><div class="value">{stats['avg_return_7d']:.2f}%</div><div class="label">Avg Return (7d)</div></div>
  <div class="stat-card"><div class="value">{stats['avg_score']:.1f}</div><div class="label">Avg Score</div></div>
  <div class="stat-card"><div class="value">{stats['total_wins']}</div><div class="label">Wins</div></div>
  <div class="stat-card"><div class="value">{stats['total_losses']}</div><div class="label">Losses</div></div>
  <div class="stat-card"><div class="value">{stats['pending']}</div><div class="label">Pending</div></div>
</div>

<table>
<thead>
<tr>
  <th>Symbol</th><th>Alert Time</th><th>Score</th>
  <th>L1 Tech</th><th>L2 Fund</th><th>L3 Sent</th><th>L4 Mkt</th>
  <th>Price</th><th>Broker</th><th>7d Return</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>

<footer>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | Trade Journal v1.0</footer>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"✅ Trade journal saved to {output_path}")
    return output_path


# ── JSON export ───────────────────────────────────────────────────────────────

def export_journal_json(entries: list[JournalEntry], output_path: Path) -> Path:
    """Export journal entries to JSON for external analysis."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [e.to_dict() for e in entries]
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return output_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main(args: Optional[argparse.Namespace] = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Trade Journal — signal outcome tracker")
        parser.add_argument("--days", type=int, default=90, help="Look-back days (default 90)")
        parser.add_argument("--min-score", type=float, default=0.0, help="Minimum composite score")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output HTML path")
        parser.add_argument("--json", default=None, help="Also export JSON to this path")
        args = parser.parse_args()

    ensure_data_dirs()

    print(f"📓 Fetching journal entries (last {args.days} days, min score {args.min_score})...")

    entries = fetch_journal_entries(
        db_path=DB_PATH,
        days=args.days,
        min_score=args.min_score,
    )

    stats = compute_journal_stats(entries)
    print(f"   Found {stats['total_signals']} signals, win rate {stats['win_rate_7d']:.1f}%")

    generate_journal_html(entries, stats, Path(args.out))

    if args.json:
        export_journal_json(entries, Path(args.json))
        print(f"   JSON exported to {args.json}")


if __name__ == "__main__":
    main()
