"""F16 — Portfolio Snapshot Archiver.

Saves timestamped portfolio snapshots after each morning run.
Enables queries like "What was my portfolio worth on March 1?"
Uses SQLite for storage (report_db compatible).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = ROOT / ".cache_global_positions.json"
DB_PATH = ROOT / "reports" / "portfolio_snapshots.db"

_KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]

# ─── DB schema ────────────────────────────────────────────────────────────────

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts   TEXT    NOT NULL,       -- ISO-8601 UTC
    total_value   REAL    NOT NULL,
    total_pnl     REAL    NOT NULL,
    position_count INTEGER NOT NULL,
    broker_json   TEXT    NOT NULL,       -- JSON: {broker: {count, value, pnl}}
    positions_json TEXT   NOT NULL        -- full position list JSON
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(snapshot_ts);
"""


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or create) the snapshots database."""
    p = Path(db_path) if db_path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(CREATE_SQL)
    return conn


def load_positions(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load cached positions JSON."""
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


# ─── core logic ───────────────────────────────────────────────────────────────


def build_broker_breakdown(positions: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Aggregate positions by broker."""
    breakdown: dict[str, dict[str, float]] = {
        b: {"count": 0, "value": 0.0, "pnl": 0.0} for b in _KNOWN_BROKERS
    }
    for pos in positions:
        broker = pos.get("broker", "Unknown")
        if broker not in breakdown:
            breakdown[broker] = {"count": 0, "value": 0.0, "pnl": 0.0}
        breakdown[broker]["count"] += 1
        breakdown[broker]["value"] += float(pos.get("market_val", 0))
        breakdown[broker]["pnl"] += float(pos.get("pnl", 0))
    return breakdown


def save_snapshot(
    positions: list[dict[str, Any]],
    conn: sqlite3.Connection,
    timestamp: str | None = None,
) -> int:
    """Save a portfolio snapshot to the database.

    Returns:
        snapshot row id.
    """
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_value = sum(float(p.get("market_val", 0)) for p in positions)
    total_pnl = sum(float(p.get("pnl", 0)) for p in positions)
    broker_data = build_broker_breakdown(positions)

    cur = conn.execute(
        "INSERT INTO snapshots (snapshot_ts, total_value, total_pnl, position_count, broker_json, positions_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            ts,
            round(total_value, 2),
            round(total_pnl, 2),
            len(positions),
            json.dumps(broker_data),
            json.dumps(positions),
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_snapshot_on_date(
    conn: sqlite3.Connection,
    date_str: str,
) -> dict[str, Any] | None:
    """Get the closest snapshot to a given date (YYYY-MM-DD).

    Returns dict with keys: snapshot_ts, total_value, total_pnl,
    position_count, broker_breakdown, positions.
    Returns None if no snapshot found.
    """
    row = conn.execute(
        "SELECT id, snapshot_ts, total_value, total_pnl, position_count, broker_json, positions_json "
        "FROM snapshots WHERE snapshot_ts >= ? ORDER BY snapshot_ts ASC LIMIT 1",
        (date_str,),
    ).fetchone()

    if row is None:
        # Try the most recent snapshot before the date
        row = conn.execute(
            "SELECT id, snapshot_ts, total_value, total_pnl, position_count, broker_json, positions_json "
            "FROM snapshots WHERE snapshot_ts < ? ORDER BY snapshot_ts DESC LIMIT 1",
            (date_str,),
        ).fetchone()

    if row is None:
        return None

    return {
        "id": row[0],
        "snapshot_ts": row[1],
        "total_value": row[2],
        "total_pnl": row[3],
        "position_count": row[4],
        "broker_breakdown": json.loads(row[5]),
        "positions": json.loads(row[6]),
    }


def list_snapshots(
    conn: sqlite3.Connection,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """List recent snapshots (newest first)."""
    rows = conn.execute(
        "SELECT id, snapshot_ts, total_value, total_pnl, position_count "
        "FROM snapshots ORDER BY snapshot_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "snapshot_ts": r[1], "total_value": r[2],
         "total_pnl": r[3], "position_count": r[4]}
        for r in rows
    ]


def compare_snapshots(
    conn: sqlite3.Connection,
    ts1: str,
    ts2: str,
) -> dict[str, Any] | None:
    """Compare two snapshots by date strings.

    Returns dict with value_change, pnl_change, count_change.
    """
    s1 = get_snapshot_on_date(conn, ts1)
    s2 = get_snapshot_on_date(conn, ts2)
    if s1 is None or s2 is None:
        return None
    return {
        "from": s1["snapshot_ts"],
        "to": s2["snapshot_ts"],
        "value_change": round(s2["total_value"] - s1["total_value"], 2),
        "pnl_change": round(s2["total_pnl"] - s1["total_pnl"], 2),
        "count_change": s2["position_count"] - s1["position_count"],
    }


# ─── HTML report ──────────────────────────────────────────────────────────────

def generate_html(
    snapshots: list[dict[str, Any]],
    out: Path | str | None = None,
) -> str:
    """Build an HTML snapshot history report."""
    rows = ""
    for s in snapshots:
        pnl_cls = "pos" if s["total_pnl"] >= 0 else "neg"
        rows += (
            f"<tr>"
            f"<td>{s['snapshot_ts']}</td>"
            f"<td class='num'>${s['total_value']:,.0f}</td>"
            f"<td class='num {pnl_cls}'>${s['total_pnl']:,.0f}</td>"
            f"<td class='num'>{s['position_count']}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Portfolio Snapshot History</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 900px; margin: 20px auto; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
th {{ background: #161b22; color: #8b949e; text-align: left; padding: 8px 12px;
      border-bottom: 1px solid #30363d; font-size: .85em; text-transform: uppercase; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #3fb950; }}
.neg {{ color: #f85149; }}
.summary {{ background: #161b22; border: 1px solid #30363d; padding: 16px;
            border-radius: 6px; margin: 16px 0; }}
</style></head><body>
<h1>Portfolio Snapshot History</h1>
<div class="summary">
  <strong>{len(snapshots)}</strong> snapshots archived
</div>
<table>
<thead><tr><th>Timestamp</th><th>Total Value</th><th>Total P&L</th>
<th>Positions</th></tr></thead>
<tbody>
{rows if rows else '<tr><td colspan="4" style="text-align:center;color:#8b949e">No snapshots yet</td></tr>'}
</tbody></table>
</body></html>"""

    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        print(f"Report saved → {p}")
    return html


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="Portfolio Snapshot Archiver")
        parser.add_argument("--cache", default=str(CACHE_PATH))
        parser.add_argument("--db", default=str(DB_PATH))
        parser.add_argument("--out", default=str(ROOT / "reports" / "snapshot_history.html"))
        sub = parser.add_subparsers(dest="command")

        snap = sub.add_parser("save", help="Save current snapshot")
        query = sub.add_parser("query", help="Query portfolio on a date")
        query.add_argument("date", help="Date to query (YYYY-MM-DD)")
        sub.add_parser("list", help="List recent snapshots")
        cmp = sub.add_parser("compare", help="Compare two dates")
        cmp.add_argument("date1")
        cmp.add_argument("date2")

        args = parser.parse_args()

    conn = init_db(args.db)

    if args.command == "query":
        snap = get_snapshot_on_date(conn, args.date)
        if snap:
            print(f"Snapshot: {snap['snapshot_ts']}")
            print(f"  Total value: ${snap['total_value']:,.0f}")
            print(f"  Total P&L:   ${snap['total_pnl']:,.0f}")
            print(f"  Positions:   {snap['position_count']}")
        else:
            print(f"No snapshot found for {args.date}")

    elif args.command == "compare":
        result = compare_snapshots(conn, args.date1, args.date2)
        if result:
            print(f"From {result['from']} → {result['to']}")
            print(f"  Value change: ${result['value_change']:+,.0f}")
            print(f"  P&L change:   ${result['pnl_change']:+,.0f}")
            print(f"  Count change: {result['count_change']:+d}")
        else:
            print("Could not find snapshots for comparison.")

    elif args.command == "list":
        snaps = list_snapshots(conn)
        for s in snaps:
            print(f"  {s['snapshot_ts']}  ${s['total_value']:>12,.0f}  "
                  f"P&L ${s['total_pnl']:>10,.0f}  ({s['position_count']} pos)")

    else:
        # Default: save snapshot
        positions = load_positions(args.cache)
        if not positions:
            print("No positions found. Run global_portfolio_dashboard.py first.")
            conn.close()
            return
        row_id = save_snapshot(positions, conn)
        print(f"Snapshot #{row_id} saved ({len(positions)} positions)")

        # Also generate HTML report
        snaps = list_snapshots(conn)
        generate_html(snaps, args.out)

    conn.close()


if __name__ == "__main__":
    main()
