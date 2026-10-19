"""
portfolio_score_tracker.py — Daily L1-L4 score capture + trend HTML report

Reads live positions from .cache_global_positions.json, runs the full
AnalysisEngine pipeline (same as signal_stack.py) on each holding, saves
scores to database/portfolio_score_history.db, and generates an HTML report
with Chart.js time-series trend graphs.

Usage
─────
  python portfolio_score_tracker.py --capture         # score today + write report
  python portfolio_score_tracker.py                   # view-only: read DB, write report
  python portfolio_score_tracker.py --days 60         # longer history window
  python portfolio_score_tracker.py --limit 10        # score only 10 symbols (dev test)

Output: reports/portfolio_score_history.html
Database: database/portfolio_score_history.db
"""

from __future__ import annotations

import sys
import os
import json
import sqlite3
import argparse
import traceback
from pathlib import Path
from datetime import date, datetime

# ── Windows UTF-8 fix (MANDATORY per copilot-instructions.md) ─────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent       # tradingview/
_ROOT = _HERE.parent                # stocks/

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from path_config import ensure_paths  # noqa: E402
ensure_paths(_HERE)

# Default paths
_POSITIONS_CACHE = _ROOT / ".cache_global_positions.json"
_DB_PATH = _ROOT / "database" / "portfolio_score_history.db"
_REPORT_OUT = _ROOT / "reports" / "portfolio_score_history.html"


# ══════════════════════════════════════════════════════════════════════════════
# Database
# ══════════════════════════════════════════════════════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS score_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at       TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    broker            TEXT    DEFAULT '',
    l1_technical      REAL    DEFAULT 0,
    l2_fundamental    REAL    DEFAULT 0,
    l3_sentiment      REAL    DEFAULT 0,
    l4_market         REAL    DEFAULT 0,
    composite         REAL    DEFAULT 0,
    price             REAL    DEFAULT 0,
    underlying_symbol TEXT    DEFAULT '',
    error_flag        INTEGER DEFAULT 0,
    UNIQUE(captured_at, symbol)
);
CREATE INDEX IF NOT EXISTS idx_score_history_date   ON score_history(captured_at);
CREATE INDEX IF NOT EXISTS idx_score_history_symbol ON score_history(symbol);
"""


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(_SCHEMA)
    con.commit()
    _flag_fabricated_rows(con)
    # Wire into versioned migration system for future schema evolution
    try:
        from database.migrations import run_migrations
        run_migrations("portfolio_score_history", con)
    except Exception:
        pass  # migrations module unavailable (standalone use)
    return con


#: Sub-scores the engine returns when it has no data for a symbol. Before broker
#: symbols were resolved to TradingView symbols, unmapped holdings landed here and
#: were stored as if they were real analysis. Both values matching to 2dp is a
#: reliable fingerprint for those rows.
_NEUTRAL_FALLBACK_L1 = 4.75
_NEUTRAL_FALLBACK_L2 = 4.55


def _flag_fabricated_rows(con: sqlite3.Connection) -> int:
    """Mark historical neutral-fallback rows as errors. Idempotent."""
    try:
        cur = con.execute(
            """UPDATE score_history SET error_flag = 1
               WHERE error_flag = 0
                 AND ROUND(l1_technical, 2) = ?
                 AND ROUND(l2_fundamental, 2) = ?""",
            (_NEUTRAL_FALLBACK_L1, _NEUTRAL_FALLBACK_L2),
        )
        con.commit()
        if cur.rowcount:
            print(f"  [DB] Flagged {cur.rowcount} historical rows that were "
                  f"stored on neutral-fallback scores")
        return cur.rowcount or 0
    except sqlite3.Error:
        return 0


def _upsert_row(con: sqlite3.Connection, row: dict) -> None:
    """Insert or replace a score row (ignores duplicates on same date+symbol)."""
    con.execute(
        """INSERT INTO score_history
           (captured_at, symbol, broker, l1_technical, l2_fundamental,
            l3_sentiment, l4_market, composite, price, underlying_symbol, error_flag)
           VALUES (:captured_at, :symbol, :broker, :l1, :l2, :l3, :l4,
                   :composite, :price, :underlying_symbol, :error_flag)
           ON CONFLICT(captured_at, symbol) DO UPDATE SET
             l1_technical=excluded.l1_technical,
             l2_fundamental=excluded.l2_fundamental,
             l3_sentiment=excluded.l3_sentiment,
             l4_market=excluded.l4_market,
             composite=excluded.composite,
             price=excluded.price,
             underlying_symbol=excluded.underlying_symbol,
             error_flag=excluded.error_flag
        """,
        row,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Capture
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_zerodha_live_positions() -> list[dict]:
    """
    Fetch live Zerodha holdings via zerodha_auth SSOT (india-brokers/zerodha_auth.py).
    Zerodha should never be 'offline' — auth is handled once per day by the singleton.
    Returns positions in .cache_global_positions.json format, or [] when unavailable.
    """
    india_path = _ROOT / "india-brokers"
    if str(india_path) not in sys.path:
        sys.path.insert(0, str(india_path))
    try:
        from zerodha_auth import get_kite, is_token_valid  # type: ignore
    except ImportError:
        print("  [Zerodha] zerodha_auth import failed — kiteconnect installed?")
        return []
    if not is_token_valid():
        print("  [Zerodha] No valid token — set ZERODHA_ACCESS_TOKEN or run zerodha_login.py")
        return []
    try:
        kite = get_kite(silent=True)
        raw = kite.holdings()
        positions = []
        for h in raw:
            qty = h.get("quantity", 0) or 0
            if not qty:
                continue
            avg = float(h.get("average_price", 0) or 0)
            price = float(h.get("last_price", 0) or 0)
            pnl = (price - avg) * qty
            pnl_pct = ((price / avg) - 1) * 100 if avg else 0.0
            positions.append({
                "symbol": h.get("tradingsymbol", ""),
                "broker": "Zerodha",
                "shares": qty,
                "avg_cost": avg,
                "current_price": price,
                "market_val": price * qty,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "currency": "INR",
            })
        if positions:
            print(f"  [Zerodha] Fetched {len(positions)} live holdings via KiteConnect ✓")
        return positions
    except Exception as exc:
        print(f"  [Zerodha] Live fetch error: {exc}")
        return []


def _load_positions(positions_file: Path) -> list[dict]:
    """Load positions from cache; auto-fetches Zerodha live if missing (Zerodha never offline)."""
    if not positions_file.exists():
        print(f"  [WARN] Positions cache not found: {positions_file}")
        print("  Run 'Global Portfolio Dashboard' (option 1) first to populate it.")
        return []
    try:
        data = json.loads(positions_file.read_text(encoding="utf-8"))
        positions = list(data.get("positions", []))
        saved_at = data.get("saved_at", 0)
        if saved_at:
            age_h = (datetime.now().timestamp() - saved_at) / 3600
            if age_h > 24:
                print(f"  [WARN] Positions cache is {age_h:.1f}h old — run Global Dashboard to refresh")
    except Exception as exc:
        print(f"  [ERROR] Failed to load positions: {exc}")
        return []

    # Zerodha SSOT rule: auth is handled once per day by zerodha_auth singleton.
    # Never show Zerodha as 'offline' — auto-fetch live holdings if missing from cache.
    has_zerodha = any(p.get("broker") == "Zerodha" for p in positions)
    if not has_zerodha:
        print("  [Zerodha] Not in positions cache — fetching live via zerodha_auth ...")
        z_pos = _fetch_zerodha_live_positions()
        if z_pos:
            positions.extend(z_pos)
    return positions


def _score_symbol(engine, symbol: str, broker: str,
                  screener_cache: dict, tv_fund_cache: dict, yc: dict) -> dict:
    """Run the L1-L4 pipeline for one symbol and return a row dict."""
    try:
        result = engine.analyse_signal(
            fire={"pro_symbol": symbol, "message": ""},
            screener_cache=screener_cache,
            tv_fund_cache=tv_fund_cache,
            yc=yc,
        )
        scores = result.get("scores", {})
        l1 = scores.get("l1", {}).get("total", 0) or 0
        l2 = scores.get("l2", {}).get("total", 0) or 0
        l3 = scores.get("l3", {}).get("total", 0) or 0
        l4 = scores.get("l4", {}).get("total", 0) or 0
        comp = result.get("composite", 0) or 0
        # Price: from screener > fundamentals > 0
        price = (
            (screener_cache.get(symbol) or {}).get("close")
            or (result.get("fundamentals") or {}).get("current_price")
            or 0
        )
        underlying = ""
        if result.get("is_etf") and result.get("etf_label", ""):
            underlying = str(result.get("etf_label", ""))
        return {
            "captured_at": str(date.today()),
            "symbol": symbol,
            "broker": broker,
            "l1": float(l1),
            "l2": float(l2),
            "l3": float(l3),
            "l4": float(l4),
            "composite": float(comp),
            "price": float(price or 0),
            "underlying_symbol": underlying,
            "error_flag": 0,
        }
    except Exception as exc:
        print(f"    [ERR] {symbol}: {exc}")
        return {
            "captured_at": str(date.today()),
            "symbol": symbol,
            "broker": broker,
            "l1": 0.0, "l2": 0.0, "l3": 0.0, "l4": 0.0,
            "composite": 0.0,
            "price": 0.0,
            "underlying_symbol": "",
            "error_flag": 1,
        }


def capture_scores(positions_file: Path, db_path: Path, limit: int | None = None) -> int:
    """
    Score all holdings and upsert today's rows into the DB.
    Returns the number of symbols scored.
    """
    positions = _load_positions(positions_file)
    if not positions:
        print("  No positions found — nothing to capture.")
        return 0

    # Broker symbols are not TradingView symbols. Resolving them here is what
    # stops unmapped holdings being stored as neutral-fallback scores that look real.
    from holdings_resolver import resolve_holdings

    seen: dict[str, str] = {}
    for res in resolve_holdings(positions):
        if not res.scoreable:
            print(f"    [SKIP] {res.source_symbol}: {res.detail}")
            continue
        seen.setdefault(res.pro_symbol, res.broker)

    symbols = list(seen.keys())
    if limit:
        symbols = symbols[:limit]

    print(f"  Capturing scores for {len(symbols)} symbols (today: {date.today()}) ...")

    # ── Import AnalysisEngine ──────────────────────────────────────────────
    try:
        from signal_engine.analysis_engine import AnalysisEngine
        engine = AnalysisEngine()
    except ImportError as exc:
        print(f"  [ERROR] Cannot import AnalysisEngine: {exc}")
        return 0

    # ── Batch pre-fetches (one API call per batch instead of per symbol) ───
    print(f"  [1/4] Fetching TV screener data ...")
    try:
        screener_cache = engine.fetch_tv_screener(symbols)
    except Exception as exc:
        print(f"  [WARN] Screener fetch failed: {exc} — using empty cache")
        screener_cache = {}

    print(f"  [2/4] Fetching TV fundamentals ...")
    try:
        tv_fund_cache = engine.fetch_tv_fundamentals(symbols)
    except Exception as exc:
        print(f"  [WARN] Fundamentals fetch failed: {exc} — using empty cache")
        tv_fund_cache = {}

    print(f"  [3/4] Fetching yield curve ...")
    try:
        yc = engine.fetch_yield_curve()
    except Exception as exc:
        print(f"  [WARN] Yield curve fetch failed: {exc} — using empty dict")
        yc = {}

    # ── Score each symbol ──────────────────────────────────────────────────
    print(f"  [4/4] Scoring {len(symbols)} symbols ...")
    con = _open_db(db_path)
    scored = 0
    for i, sym in enumerate(symbols, 1):
        broker = seen[sym]
        print(f"    [{i:>3}/{len(symbols)}] {sym:<30}", end=" ", flush=True)
        row = _score_symbol(engine, sym, broker, screener_cache, tv_fund_cache, yc)
        _upsert_row(con, row)
        status = "ERR" if row["error_flag"] else f"{row['composite']:.0f}"
        print(f"composite={status}")
        scored += 1

    con.commit()
    con.close()
    print(f"\n  Saved {scored} score rows to {db_path}")
    return scored


# ══════════════════════════════════════════════════════════════════════════════
# Report generation
# ══════════════════════════════════════════════════════════════════════════════

def _grade(composite: float) -> str:
    if composite >= 70:
        return "A"
    if composite >= 55:
        return "B"
    if composite >= 40:
        return "C"
    return "D"


def _grade_color(grade: str) -> str:
    return {"A": "#22c55e", "B": "#3b82f6", "C": "#f59e0b", "D": "#ef4444"}.get(grade, "#888")


def _load_history(db_path: Path, days: int) -> list[dict]:
    """Return rows from DB for the last `days` calendar days."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT captured_at, symbol, broker,
                  l1_technical, l2_fundamental, l3_sentiment, l4_market,
                  composite, price, underlying_symbol, error_flag
           FROM score_history
           WHERE captured_at >= date('now', ? || ' days')
             AND error_flag = 0
           ORDER BY captured_at DESC, composite DESC
        """,
        (f"-{days}",),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]



def build_html(rows: list[dict], days: int) -> str:
    """Build portfolio score history HTML — 5 tabs with per-stock 5-line trend charts."""
    if not rows:
        return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Portfolio Score History</title></head>
<body style="font-family:sans-serif;padding:2rem;background:#0f0f0f;color:#e2e8f0">
<h1 style="margin-bottom:1rem">Portfolio Score History</h1>
<div style="background:#1a2440;border:1px solid #2a3a60;border-radius:10px;padding:1.5rem">
<h3 style="color:#f59e0b;margin-bottom:.75rem">No Data Found</h3>
<p style="color:#94a3b8;margin-bottom:.5rem">No scores captured yet. To generate this report:</p>
<ol style="color:#94a3b8;padding-left:1.5rem;line-height:1.8">
  <li>Run <strong style="color:#7dd3fc">Option 1: Global Portfolio Dashboard</strong> to refresh live positions</li>
  <li>Return to main menu and select <strong style="color:#7dd3fc">Option 4a: Portfolio Score History</strong></li>
  <li>Answer <code style="background:#2a2a3e;padding:1px 6px;border-radius:4px">y</code> when asked to capture today&#39;s scores</li>
  <li>Wait ~5-10 minutes for the first full capture (scores all holdings via TradingView screener)</li>
  <li>Subsequent daily captures take ~2 min (screener data cached for 24h)</li>
</ol>
</div></body></html>"""

    # ── Data prep ──────────────────────────────────────────────────────────
    today = str(date.today())
    latest_date = max(r["captured_at"] for r in rows)
    latest_rows = [r for r in rows if r["captured_at"] == latest_date]
    latest_rows.sort(key=lambda r: r["composite"], reverse=True)

    all_dates = sorted({r["captured_at"] for r in rows})

    # Build per-symbol data: {sym: {date: {l1,l2,l3,l4,c}}}
    sym_data: dict[str, dict] = {}
    sym_broker: dict[str, str] = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in sym_data:
            sym_data[sym] = {}
            sym_broker[sym] = r.get("broker", "")
        sym_data[sym][r["captured_at"]] = {
            "l1": r["l1_technical"],
            "l2": r["l2_fundamental"],
            "l3": r["l3_sentiment"],
            "l4": r["l4_market"],
            "c":  r["composite"],
        }

    # Sort symbols by latest composite descending
    symbols_sorted = sorted(
        sym_data.keys(),
        key=lambda s: sym_data[s].get(latest_date, {}).get("c", 0),
        reverse=True,
    )

    # Compact JS data: symbol -> {l1:[],l2:[],l3:[],l4:[],c:[],b:str}
    js_data: dict[str, dict] = {}
    for sym in symbols_sorted:
        js_data[sym] = {
            "l1": [sym_data[sym].get(d, {}).get("l1") for d in all_dates],
            "l2": [sym_data[sym].get(d, {}).get("l2") for d in all_dates],
            "l3": [sym_data[sym].get(d, {}).get("l3") for d in all_dates],
            "l4": [sym_data[sym].get(d, {}).get("l4") for d in all_dates],
            "c":  [sym_data[sym].get(d, {}).get("c")  for d in all_dates],
            "b":  sym_broker.get(sym, ""),
        }

    # Grade distribution
    grades = [_grade(r["composite"]) for r in latest_rows]
    grade_counts = {g: grades.count(g) for g in "ABCD"}
    avg_composite = (
        sum(r["composite"] for r in latest_rows) / len(latest_rows) if latest_rows else 0
    )
    total_positions = len(latest_rows)

    # ── Table rows ─────────────────────────────────────────────────────────
    def _bar(val: float, max_val: float = 30.0) -> str:
        pct = min(100, max(0, val / max_val * 100))
        clr = "#22c55e" if pct >= 70 else "#f59e0b" if pct >= 40 else "#ef4444"
        return (
            f"<div style='display:inline-block;width:60px;height:8px;"
            f"background:#2a2a2a;border-radius:4px;vertical-align:middle'>"
            f"<div style='width:{pct:.0f}%;height:8px;background:{clr};"
            f"border-radius:4px'></div></div>"
            f"<span style='margin-left:4px;font-size:11px'>{val:.1f}</span>"
        )

    table_rows = ""
    for r in latest_rows:
        sym = r["symbol"]
        g = _grade(r["composite"]); gc = _grade_color(g)
        l1 = r["l1_technical"]; l2 = r["l2_fundamental"]
        l3 = r["l3_sentiment"];  l4 = r["l4_market"]
        comp = r["composite"];   price = r.get("price", 0)
        broker = r.get("broker", "")
        table_rows += (
            f"<tr data-broker='{broker}' data-grade='{g}'>"
            f"<td style='padding:6px 10px;font-weight:600'>"
            f"<a href='#' onclick='selectStock(\"{sym}\");return false;'"
            f" style='color:#7dd3fc;text-decoration:none'>{sym}</a></td>"
            f"<td style='padding:6px 10px;font-size:12px;color:#aaa'>{broker}</td>"
            f"<td style='padding:6px 10px;text-align:center'>"
            f"<span style='background:{gc};color:#000;padding:2px 8px;border-radius:12px;"
            f"font-weight:700;font-size:13px'>{g}</span></td>"
            f"<td style='padding:6px 10px'>{_bar(l1, 30)}</td>"
            f"<td style='padding:6px 10px'>{_bar(l2, 30)}</td>"
            f"<td style='padding:6px 10px'>{_bar(l3, 20)}</td>"
            f"<td style='padding:6px 10px'>{_bar(l4, 20)}</td>"
            f"<td style='padding:6px 10px;text-align:right;font-weight:700;"
            f"color:{'#22c55e' if comp>=55 else '#f59e0b' if comp>=40 else '#ef4444'}'>"
            f"{comp:.1f}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#aaa;font-size:12px'>"
            f"{f'{price:,.2f}' if price else chr(8212)}</td>"
            f"</tr>\n"
        )

    # ── Broker breakdown ────────────────────────────────────────────────────
    _KNOWN_BROKERS = ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]
    broker_stats: dict[str, dict] = {
        b: {"count": 0, "sum_comp": 0.0, "a": 0, "b": 0, "c": 0, "d": 0}
        for b in _KNOWN_BROKERS
    }
    for r in latest_rows:
        b = r.get("broker", "Unknown")
        if b not in broker_stats:
            broker_stats[b] = {"count": 0, "sum_comp": 0.0, "a": 0, "b": 0, "c": 0, "d": 0}
        broker_stats[b]["count"] += 1
        broker_stats[b]["sum_comp"] += r["composite"]
        g2 = _grade(r["composite"]).lower()
        broker_stats[b][g2] = broker_stats[b].get(g2, 0) + 1

    broker_rows = ""
    for b, s in broker_stats.items():
        if s["count"] == 0:
            broker_rows += (
                f"<tr style='opacity:.4'><td style='padding:6px 10px'><strong>{b}</strong></td>"
                f"<td style='padding:6px 10px;color:#888'>—</td>"
                f"<td style='padding:6px 10px;color:#888'>—</td>"
                f"<td style='padding:6px 10px;color:#888'>—</td></tr>\n"
            )
        else:
            avg = s["sum_comp"] / s["count"]
            dist = (
                f"<span style='color:#22c55e'>A:{s['a']}</span> "
                f"<span style='color:#3b82f6'>B:{s['b']}</span> "
                f"<span style='color:#f59e0b'>C:{s['c']}</span> "
                f"<span style='color:#ef4444'>D:{s['d']}</span>"
            )
            broker_rows += (
                f"<tr><td style='padding:6px 10px'><strong>{b}</strong></td>"
                f"<td style='padding:6px 10px;text-align:center'>{s['count']}</td>"
                f"<td style='padding:6px 10px;text-align:right;font-weight:700;"
                f"color:{'#22c55e' if avg>=55 else '#f59e0b' if avg>=40 else '#ef4444'}'>"
                f"{avg:.1f}</td>"
                f"<td style='padding:6px 10px'>{dist}</td></tr>\n"
            )

    # ── Broker chips HTML (all 6 always visible) ────────────────────────────
    broker_chips_html = ""
    for b in _KNOWN_BROKERS:
        s = broker_stats.get(b, {"count": 0})
        cnt = s["count"]
        if cnt > 0:
            broker_chips_html += (
                f"  <span class='bchip bchip-online' "
                f"title='{b}: {cnt} positions captured'>"
                f"{b}: {cnt}</span>\n"
            )
        else:
            broker_chips_html += (
                f"  <span class='bchip bchip-offline' "
                f"title='{b}: 0 holdings in positions cache — "
                f"run Global Dashboard (option 1) to refresh, or check broker auth.'>"
                f"{b}: 0 holdings</span>\n"
            )
    # Also add any unknown brokers found in data
    for b, s in broker_stats.items():
        if b not in _KNOWN_BROKERS and s.get("count", 0) > 0:
            broker_chips_html += (
                f"  <span class='bchip bchip-online'>{b}: {s['count']}</span>\n"
            )

    # ── Capture history ─────────────────────────────────────────────────────
    all_date_rows = ""
    dates_in_db = sorted({r["captured_at"] for r in rows}, reverse=True)
    for d in dates_in_db[:30]:
        d_rows = [r for r in rows if r["captured_at"] == d]
        avg = sum(r["composite"] for r in d_rows) / len(d_rows) if d_rows else 0
        g_dist = {g: sum(1 for r in d_rows if _grade(r["composite"]) == g) for g in "ABCD"}
        all_date_rows += (
            f"<tr><td style='padding:6px 10px'>{d}</td>"
            f"<td style='padding:6px 10px;text-align:center'>{len(d_rows)}</td>"
            f"<td style='padding:6px 10px;text-align:right;font-weight:700;color:#22c55e'>"
            f"{avg:.1f}</td>"
            f"<td style='padding:6px 10px'>"
            f"<span style='color:#22c55e'>A:{g_dist['A']}</span> "
            f"<span style='color:#3b82f6'>B:{g_dist['B']}</span> "
            f"<span style='color:#f59e0b'>C:{g_dist['C']}</span> "
            f"<span style='color:#ef4444'>D:{g_dist['D']}</span>"
            f"</td></tr>\n"
        )

    # ── Symbols dropdown for individual chart ────────────────────────────────
    sym_options = "\n".join(
        f"      <option value='{s}'>{s}  ({sym_broker.get(s, '')})</option>"
        for s in symbols_sorted
    )

    # ── Stock cards HTML for the grid view (one mini-chart card per holding) ─
    stock_cards_html = ""
    for sym in symbols_sorted:
        r_latest = next((row for row in latest_rows if row["symbol"] == sym), None)
        comp  = r_latest["composite"]      if r_latest else 0.0
        l1v   = r_latest["l1_technical"]   if r_latest else 0.0
        l2v   = r_latest["l2_fundamental"] if r_latest else 0.0
        l3v   = r_latest["l3_sentiment"]   if r_latest else 0.0
        l4v   = r_latest["l4_market"]      if r_latest else 0.0
        g = _grade(comp); gc = _grade_color(g)
        broker = sym_broker.get(sym, "")
        comp_col = "#22c55e" if comp >= 55 else "#f59e0b" if comp >= 40 else "#ef4444"
        stock_cards_html += (
            f"<div class='stock-card' data-sym='{sym}'"
            f" data-broker='{broker}' data-grade='{g}'>"
            f"<div class='card-hdr'>"
            f"<span class='card-sym' title='{sym}'>{sym}</span>"
            f"<span class='card-grade' style='background:{gc}'>{g}</span>"
            f"<span class='card-comp' style='color:{comp_col}'>{comp:.0f}</span>"
            f"</div>"
            f"<div class='card-broker'>{broker}</div>"
            f"<div class='canvas-wrap'>"
            f"<canvas class='mini-chart' data-sym='{sym}'></canvas>"
            f"</div>"
            f"<div class='card-scores'>"
            f"<span style='color:#3b82f6'>L1:{l1v:.0f}</span> "
            f"<span style='color:#22c55e'>L2:{l2v:.0f}</span> "
            f"<span style='color:#f59e0b'>L3:{l3v:.0f}</span> "
            f"<span style='color:#a78bfa'>L4:{l4v:.0f}</span>"
            f"</div>"
            f"</div>\n"
        )

    # ── Serialise for JavaScript ─────────────────────────────────────────────
    dates_json    = json.dumps(all_dates)
    sym_data_json = json.dumps(js_data)
    symbols_json  = json.dumps(symbols_sorted)
    avg_color = "#22c55e" if avg_composite >= 55 else "#f59e0b" if avg_composite >= 40 else "#ef4444"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio Score History</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" integrity="sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" crossorigin="anonymous"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0f0f0f;color:#e2e8f0;padding:1.5rem}}
  h1{{font-size:1.6rem;margin-bottom:.5rem}}
  .subtitle{{color:#888;font-size:.9rem;margin-bottom:1rem}}
  .howto{{background:#1a2440;border:1px solid #2a3a60;border-radius:8px;
          padding:.75rem 1rem;margin-bottom:1.5rem;font-size:.8rem;color:#94a3b8;line-height:1.7}}
  .howto strong{{color:#7dd3fc}}
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
              gap:1rem;margin-bottom:1.5rem}}
  .stat-card{{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:10px;
              padding:1rem;text-align:center}}
  .stat-val{{font-size:2rem;font-weight:700}}
  .stat-label{{font-size:.75rem;color:#888;margin-top:.25rem;text-transform:uppercase}}
  .tabs{{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}}
  .tab{{padding:.5rem 1.2rem;border:1px solid #333;border-radius:6px;cursor:pointer;
        background:#1a1a1a;color:#aaa;font-size:.9rem;transition:.15s;font-family:inherit;font-weight:400}}
  .tab.active{{background:#3b82f6;border-color:#3b82f6;color:#fff;font-weight:600}}
  .tab:hover:not(.active){{background:#2a2a2a;color:#ddd}}
  .panel{{display:none}}.panel.active{{display:block}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;padding:8px 10px;font-size:.75rem;text-transform:uppercase;
      color:#888;border-bottom:1px solid #333;white-space:nowrap}}
  tr:hover td{{background:rgba(255,255,255,.03)}}
  td{{border-bottom:1px solid #1a1a1a}}
  .chart-wrap{{background:#1a1a1a;border-radius:10px;padding:1rem;
               position:relative;margin-bottom:1rem}}
  .chart-wrap-tall{{height:520px}}
  .chart-wrap-full{{height:600px}}
  .filter-bar{{display:flex;gap:.75rem;margin-bottom:1rem;flex-wrap:wrap;align-items:center}}
  select{{background:#1a1a2e;color:#e2e8f0;border:1px solid #333;border-radius:6px;
          padding:.4rem .8rem;font-size:.85rem;font-family:inherit}}
  .nav-btn{{background:#2a2a3e;border:1px solid #3a3a5e;color:#e2e8f0;border-radius:6px;
            padding:.4rem .9rem;font-size:.85rem;cursor:pointer;font-family:inherit}}
  .nav-btn:hover{{background:#3a3a5e}}
  .chart-caption{{font-size:.75rem;color:#555;margin-top:.25rem;text-align:right}}
  .score-legend{{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem;
                 font-size:.8rem;color:#94a3b8}}
  .score-legend span{{background:#1a1a2e;border-radius:6px;padding:.2rem .6rem}}
  .generated{{font-size:.75rem;color:#555;margin-top:1.5rem;text-align:right}}
  .broker-chips{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.25rem}}
  .bchip{{padding:.35rem .9rem;border-radius:20px;font-size:.82rem;font-weight:600;
           border:1px solid #2a2a3e;cursor:default}}
  .bchip-online{{background:#1a2440;border-color:#3b5bdb;color:#93c5fd}}
  .bchip-offline{{background:#1a1a1a;border-color:#333;color:#666;font-style:italic}}
  /* ── Stock chart grid ── */
  .stock-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
               gap:.65rem;margin-top:.5rem}}
  .stock-card{{background:#1a1a2e;border:1px solid #2a2a3e;border-radius:10px;
               padding:.7rem;cursor:pointer;transition:border-color .15s,background .15s;
               user-select:none}}
  .stock-card:hover{{border-color:#3b82f6;background:#1a1f38}}
  .card-hdr{{display:flex;align-items:center;gap:.3rem;margin-bottom:.1rem}}
  .card-sym{{font-size:.9rem;font-weight:700;color:#e2e8f0;flex:1;overflow:hidden;
             text-overflow:ellipsis;white-space:nowrap}}
  .card-grade{{padding:1px 5px;border-radius:7px;font-size:.68rem;font-weight:700;
               color:#000;flex-shrink:0}}
  .card-comp{{font-size:.88rem;font-weight:700;flex-shrink:0}}
  .card-broker{{font-size:.68rem;color:#555;margin-bottom:.25rem;
                overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .canvas-wrap{{position:relative;height:95px;margin:.2rem 0}}
  .mini-chart{{width:100%!important;height:100%!important;display:block}}
  .card-scores{{display:flex;gap:.35rem;font-size:.68rem;color:#888;flex-wrap:wrap}}
  .grid-legend{{display:flex;gap:.5rem;flex-wrap:wrap;font-size:.75rem;
                color:#94a3b8;margin-bottom:.5rem;align-items:center}}
  .grid-legend span{{background:#1a1a2e;padding:.15rem .45rem;border-radius:5px;
                     border:1px solid #2a2a3e}}
</style>
</head>
<body>

<h1>Portfolio Score History</h1>
<p class="subtitle">Daily L1-L4 scores per holding &mdash; {total_positions} holdings tracked &mdash; latest: {latest_date} &mdash; {days}-day window</p>

<div class="howto">
  <strong>How to populate:</strong>
  1. Run <strong>Option 1 (Global Portfolio Dashboard)</strong> to refresh live positions &nbsp;&rarr;&nbsp;
  2. Run <strong>Option 4a (Portfolio Score History)</strong> and answer <code>y</code> to capture today&rsquo;s scores &nbsp;&rarr;&nbsp;
  3. First capture: ~5-10 min (scores all holdings via TradingView screener).
  Daily captures thereafter: ~2 min (screener data cached 24h).
  &nbsp;|&nbsp; DB: <code>database/portfolio_score_history.db</code>
</div>

<!-- Broker chips — always all 6, offline shown dimmed -->
<div class="broker-chips">
{broker_chips_html}</div>

<!-- Summary stats -->
<div class="stat-grid">
  <div class="stat-card">
    <div class="stat-val" style="color:#e2e8f0">{total_positions}</div>
    <div class="stat-label">Holdings tracked</div>
  </div>
  <div class="stat-card">
    <div class="stat-val" style="color:{avg_color}">{avg_composite:.1f}</div>
    <div class="stat-label">Avg composite</div>
  </div>
  <div class="stat-card">
    <div class="stat-val" style="color:#22c55e">{grade_counts['A']}</div>
    <div class="stat-label">Grade A (&ge;70)</div>
  </div>
  <div class="stat-card">
    <div class="stat-val" style="color:#3b82f6">{grade_counts['B']}</div>
    <div class="stat-label">Grade B (55-69)</div>
  </div>
  <div class="stat-card">
    <div class="stat-val" style="color:#f59e0b">{grade_counts['C']}</div>
    <div class="stat-label">Grade C (40-54)</div>
  </div>
  <div class="stat-card">
    <div class="stat-val" style="color:#ef4444">{grade_counts['D']}</div>
    <div class="stat-label">Grade D (&lt;40)</div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <button type="button" class="tab active" onclick="showTab('grid')">&#128202; All {total_positions} Holdings</button>
  <button type="button" class="tab" onclick="showTab('individual')">&#128269; Individual Detail</button>
  <button type="button" class="tab" onclick="showTab('overall')">&#128200; Overall Composite</button>
  <button type="button" class="tab" onclick="showTab('table')">&#128203; Score Table</button>
  <button type="button" class="tab" onclick="showTab('brokers')">By Broker</button>
  <button type="button" class="tab" onclick="showTab('history')">Capture History</button>
</div>

<!-- Panel 1: All Stock Charts Grid (default view — all {total_positions} holdings) -->
<div id="panel-grid" class="panel active">
  <div class="grid-legend">
    <span style="color:#3b82f6">&#9632; L1 Technical</span>
    <span style="color:#22c55e">&#9632; L2 Fundamental</span>
    <span style="color:#f59e0b">&#9632; L3 Sentiment</span>
    <span style="color:#a78bfa">&#9632; L4 Market</span>
    <span style="color:#f87171">&#9632; Composite</span>
    <small style="color:#555">(all lines normalized 0&ndash;100 for mini view &mdash; click any card for full 5-line detail)</small>
  </div>
  <div class="filter-bar">
    <select id="gridBroker" onchange="filterGrid()">
      <option value="">All brokers</option>
      <option>AngelOne</option><option>IBKR</option><option>Moomoo-AU</option>
      <option>Moomoo-SG</option><option>Webull AU</option><option>Zerodha</option>
    </select>
    <select id="gridGrade" onchange="filterGrid()">
      <option value="">All grades</option>
      <option>A</option><option>B</option><option>C</option><option>D</option>
    </select>
    <input type="text" id="gridSearch" placeholder="Search symbol&hellip;" oninput="filterGrid()"
           style="background:#1a1a2e;color:#e2e8f0;border:1px solid #333;border-radius:6px;
                  padding:.4rem .8rem;font-size:.85rem;width:180px">
  </div>
  <div class="stock-grid" id="stockGrid">
{stock_cards_html}  </div>
</div>

<!-- Panel 2: Score Table (latest snapshot) -->
<div id="panel-table" class="panel">
  <div class="filter-bar">
    <select id="filterBroker" onchange="filterTable()">
      <option value="">All brokers</option>
      <option>AngelOne</option><option>IBKR</option><option>Moomoo-AU</option>
      <option>Moomoo-SG</option><option>Webull AU</option><option>Zerodha</option>
    </select>
    <select id="filterGrade" onchange="filterTable()">
      <option value="">All grades</option>
      <option>A</option><option>B</option><option>C</option><option>D</option>
    </select>
  </div>
  <div class="score-legend">
    <span>L1 Technical: max 30</span>
    <span>L2 Fundamental: max 30</span>
    <span>L3 Sentiment: max 20</span>
    <span>L4 Market: max 20</span>
    <span>Composite: max 100</span>
    <span style="color:#888;font-size:.75rem">Click a symbol name to open its 5-line trend chart</span>
  </div>
  <table id="scoreTable">
    <thead>
      <tr>
        <th>Symbol</th><th>Broker</th><th>Grade</th>
        <th>L1 Technical</th><th>L2 Fundamental</th>
        <th>L3 Sentiment</th><th>L4 Market</th>
        <th style="text-align:right">Composite</th>
        <th style="text-align:right">Price</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>

<!-- Panel 2: Individual Stock Charts (5-line per stock) -->
<div id="panel-individual" class="panel">
  <p style="color:#888;font-size:.85rem;margin-bottom:.75rem">
    Select a stock to view its daily score history (5 lines per chart):
    <strong style="color:#3b82f6">L1 Technical</strong> (0-30),
    <strong style="color:#22c55e">L2 Fundamental</strong> (0-30),
    <strong style="color:#f59e0b">L3 Sentiment</strong> (0-20),
    <strong style="color:#a78bfa">L4 Market</strong> (0-20),
    <strong style="color:#f87171">Composite</strong> (0-100, right axis).
    Each dot = one capture day.
  </p>
  <div class="filter-bar">
    <select id="stockSelect" onchange="updateIndividualChart(this.value)" style="min-width:260px">
{sym_options}
    </select>
    <button type="button" class="nav-btn" onclick="navStock(-1)">&#9664; Prev</button>
    <button type="button" class="nav-btn" onclick="navStock(1)">Next &#9654;</button>
  </div>
  <div class="chart-wrap chart-wrap-tall">
    <canvas id="individualChart"></canvas>
  </div>
  <p class="chart-caption">
    Scores via TradingView screener + yfinance &mdash; L1-L4 pipeline (same as signal_stack.py).
    Gap in line = not captured that day.
  </p>
</div>

<!-- Panel 3: Overall Comparison -->
<div id="panel-overall" class="panel">
  <p style="color:#888;font-size:.85rem;margin-bottom:.75rem">
    Composite score trends for all holdings on one chart.
    Filter by broker or limit to top N by latest composite score.
    Toggle individual lines by clicking the legend.
  </p>
  <div class="filter-bar">
    <select id="overallBroker" onchange="rebuildOverall()">
      <option value="">All brokers</option>
      <option>AngelOne</option><option>IBKR</option><option>Moomoo-AU</option>
      <option>Moomoo-SG</option><option>Webull AU</option><option>Zerodha</option>
    </select>
    <select id="overallTopN" onchange="rebuildOverall()">
      <option value="10">Top 10 by score</option>
      <option value="20" selected>Top 20 by score</option>
      <option value="50">Top 50 by score</option>
      <option value="9999">All holdings</option>
    </select>
  </div>
  <div class="chart-wrap chart-wrap-full">
    <canvas id="overallChart"></canvas>
  </div>
  <p class="chart-caption">Each line = composite score (0-100) over time for one holding. Click legend to toggle visibility.</p>
</div>

<!-- Panel 4: By Broker -->
<div id="panel-brokers" class="panel">
  <table>
    <thead>
      <tr><th>Broker</th><th style="text-align:center">Positions</th>
          <th style="text-align:right">Avg Composite</th><th>Grade Distribution</th></tr>
    </thead>
    <tbody>{broker_rows}</tbody>
  </table>
</div>

<!-- Panel 5: Capture History -->
<div id="panel-history" class="panel">
  <table>
    <thead>
      <tr><th>Date</th><th style="text-align:center">Symbols</th>
          <th style="text-align:right">Avg Composite</th><th>Grade Distribution</th></tr>
    </thead>
    <tbody>{all_date_rows if all_date_rows else "<tr><td colspan='4' style='padding:1rem;color:#888'>No capture history yet.</td></tr>"}</tbody>
  </table>
</div>

<p class="generated">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} &mdash; {days}-day lookback &mdash; {total_positions} holdings</p>

<script>
// ── Data ─────────────────────────────────────────────────────────────────────
const ALL_DATES    = {dates_json};
const ALL_SYM_DATA = {sym_data_json};
const SYMBOLS      = {symbols_json};

// Golden-angle palette for N distinct colours
function mkPalette(n) {{
  return Array.from({{length: n}}, (_, i) => `hsl(${{Math.round((i * 137.508) % 360)}},70%,55%)`);
}}
const PALETTE = mkPalette(Math.max(SYMBOLS.length, 20));

// ── Mini chart (grid view — one canvas per holding, lazy-rendered) ────────────
const _miniCharts = {{}};

function _createMiniChart(canvas, sym) {{
  const d = ALL_SYM_DATA[sym];
  if (!d) return null;
  const norm = (arr, max) => arr.map(v => v != null ? +(v / max * 100).toFixed(1) : null);
  return new Chart(canvas, {{
    type: 'line',
    data: {{
      labels: ALL_DATES,
      datasets: [
        {{ label:'L1', data:norm(d.l1,30), borderColor:'#3b82f6', borderWidth:1.5, pointRadius:2, spanGaps:true, tension:.3, fill:false }},
        {{ label:'L2', data:norm(d.l2,30), borderColor:'#22c55e', borderWidth:1.5, pointRadius:2, spanGaps:true, tension:.3, fill:false }},
        {{ label:'L3', data:norm(d.l3,20), borderColor:'#f59e0b', borderWidth:1.5, pointRadius:2, spanGaps:true, tension:.3, fill:false }},
        {{ label:'L4', data:norm(d.l4,20), borderColor:'#a78bfa', borderWidth:1.5, pointRadius:2, spanGaps:true, tension:.3, fill:false }},
        {{ label:'Comp', data:d.c,         borderColor:'#f87171', borderWidth:2,   pointRadius:2, spanGaps:true, tension:.3, fill:false }},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false, animation:false,
      plugins:{{ legend:{{display:false}}, tooltip:{{enabled:false}} }},
      scales:{{ x:{{display:false}}, y:{{min:0,max:105,display:false}} }},
    }},
  }});
}}

// IntersectionObserver — render mini charts only when scrolled into view
const _chartObserver = new IntersectionObserver((entries) => {{
  entries.forEach(e => {{
    if (e.isIntersecting) {{
      const c = e.target, sym = c.dataset.sym;
      if (!_miniCharts[sym]) _miniCharts[sym] = _createMiniChart(c, sym);
    }}
  }});
}}, {{ rootMargin:'400px', threshold:0.01 }});

// Event delegation — click any grid card → open Individual Detail for that stock
document.getElementById('stockGrid').addEventListener('click', function(e) {{
  const card = e.target.closest('.stock-card');
  if (card) openDetail(card.dataset.sym);
}});

function openDetail(sym) {{
  showTab('individual');
  const sel = document.getElementById('stockSelect');
  if (sel) {{ sel.value = sym; updateIndividualChart(sym); }}
}}

function filterGrid() {{
  const broker = document.getElementById('gridBroker').value;
  const grade  = document.getElementById('gridGrade').value;
  const q = (document.getElementById('gridSearch').value || '').toLowerCase();
  document.querySelectorAll('.stock-card').forEach(card => {{
    const show = (!broker || card.dataset.broker === broker)
              && (!grade  || card.dataset.grade  === grade)
              && (!q      || (card.dataset.sym || '').toLowerCase().includes(q));
    card.style.display = show ? '' : 'none';
  }});
}}

// Start observing all mini-chart canvases on page load
document.querySelectorAll('.mini-chart').forEach(c => _chartObserver.observe(c));

// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(name) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  document.querySelectorAll('.tab').forEach(t => {{
    if ((t.getAttribute('onclick') || '').includes("'" + name + "'")) t.classList.add('active');
  }});
  if (name === 'individual' && !_indChart) {{
    const sel = document.getElementById('stockSelect');
    updateIndividualChart(sel ? sel.value : SYMBOLS[0]);
  }}
  if (name === 'overall' && !_overallChart) rebuildOverall();
  if (name === 'grid') {{
    document.querySelectorAll('.mini-chart').forEach(c => {{
      if (!_miniCharts[c.dataset.sym]) _chartObserver.observe(c);
    }});
  }}
}}

// ── Table filter ──────────────────────────────────────────────────────────────
function filterTable() {{
  const broker = document.getElementById('filterBroker').value.toLowerCase();
  const grade  = document.getElementById('filterGrade').value.toUpperCase();
  document.querySelectorAll('#scoreTable tbody tr').forEach(row => {{
    const tds = row.querySelectorAll('td');
    const rowBroker = tds[1] ? tds[1].textContent.trim().toLowerCase() : '';
    const rowGrade  = tds[2] ? tds[2].textContent.trim().toUpperCase() : '';
    row.style.display = (!broker || rowBroker === broker) && (!grade || rowGrade === grade) ? '' : 'none';
  }});
}}

// ── Navigate prev/next stock ──────────────────────────────────────────────────
function navStock(dir) {{
  const sel = document.getElementById('stockSelect');
  const opts = Array.from(sel.options);
  let idx = opts.findIndex(o => o.value === sel.value) + dir;
  if (idx < 0) idx = opts.length - 1;
  if (idx >= opts.length) idx = 0;
  sel.value = opts[idx].value;
  updateIndividualChart(sel.value);
}}

// ── Click from Latest Scores table ───────────────────────────────────────────
function selectStock(sym) {{
  showTab('individual');
  document.getElementById('stockSelect').value = sym;
  updateIndividualChart(sym);
}}

// ── Individual 5-line chart ───────────────────────────────────────────────────
let _indChart = null;

function updateIndividualChart(sym) {{
  const d = ALL_SYM_DATA[sym];
  if (!d) return;
  const datasets = [
    {{ label: 'L1 Technical (max 30)',   data: d.l1, borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,.08)',  yAxisID: 'ySub',  tension: .3, pointRadius: 4, borderWidth: 2, spanGaps: true }},
    {{ label: 'L2 Fundamental (max 30)', data: d.l2, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,.08)',   yAxisID: 'ySub',  tension: .3, pointRadius: 4, borderWidth: 2, spanGaps: true }},
    {{ label: 'L3 Sentiment (max 20)',   data: d.l3, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,.08)',  yAxisID: 'ySub',  tension: .3, pointRadius: 4, borderWidth: 2, spanGaps: true }},
    {{ label: 'L4 Market (max 20)',      data: d.l4, borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,.08)', yAxisID: 'ySub',  tension: .3, pointRadius: 4, borderWidth: 2, spanGaps: true }},
    {{ label: 'Composite (max 100)',     data: d.c,  borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,.08)', yAxisID: 'yComp', tension: .3, pointRadius: 5, borderWidth: 3, spanGaps: true }},
  ];
  const titleText = sym + '  \u2014  L1-L4 Score Trend  (' + (d.b || 'unknown broker') + ')';
  const chartOpts = {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ labels: {{ color: '#ccc', font: {{ size: 11 }} }} }},
      title: {{ display: true, text: titleText, color: '#e2e8f0', font: {{ size: 14 }} }},
      tooltip: {{ backgroundColor: '#1a1a2e', titleColor: '#fff', bodyColor: '#ccc' }},
    }},
    scales: {{
      x: {{ ticks: {{ color: '#888', maxRotation: 45 }}, grid: {{ color: '#1e1e1e' }},
            title: {{ display: true, text: 'Capture Date', color: '#666' }} }},
      ySub:  {{ type: 'linear', position: 'left',  min: 0, max: 35,
                ticks: {{ color: '#888' }}, grid: {{ color: '#1e1e1e' }},
                title: {{ display: true, text: 'Sub-scores (L1-L4)', color: '#888' }} }},
      yComp: {{ type: 'linear', position: 'right', min: 0, max: 100,
                ticks: {{ color: '#f87171' }}, grid: {{ drawOnChartArea: false }},
                title: {{ display: true, text: 'Composite (0-100)', color: '#f87171' }} }},
    }},
  }};
  if (_indChart) {{
    _indChart.data.labels = ALL_DATES;
    _indChart.data.datasets = datasets;
    _indChart.options.plugins.title.text = titleText;
    _indChart.update('none');
  }} else {{
    _indChart = new Chart(document.getElementById('individualChart'), {{
      type: 'line', data: {{ labels: ALL_DATES, datasets }}, options: chartOpts,
    }});
  }}
}}

// ── Overall comparison chart ──────────────────────────────────────────────────
let _overallChart = null;

function rebuildOverall() {{
  const brokerFilter = (document.getElementById('overallBroker') || {{}}).value || '';
  const topN = parseInt((document.getElementById('overallTopN') || {{}}).value || '20', 10);
  let syms = SYMBOLS.filter(s => !brokerFilter || (ALL_SYM_DATA[s] && ALL_SYM_DATA[s].b === brokerFilter));
  syms = syms.slice(0, topN);
  const pal = mkPalette(syms.length);
  const datasets = syms.map((s, i) => ({{
    label: s, data: (ALL_SYM_DATA[s] || {{}}).c || [],
    borderColor: pal[i], backgroundColor: 'transparent',
    tension: .3, pointRadius: 3, borderWidth: 1.5, spanGaps: true,
  }}));
  const titleText = 'Composite Score Comparison \u2014 ' + syms.length + ' holdings';
  const chartOpts = {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: syms.length <= 20, labels: {{ color: '#ccc', font: {{ size: 10 }}, boxWidth: 14 }} }},
      title: {{ display: true, text: titleText, color: '#e2e8f0', font: {{ size: 13 }} }},
      tooltip: {{
        backgroundColor: '#1a1a2e', titleColor: '#fff', bodyColor: '#ccc',
        callbacks: {{ label: ctx => ctx.dataset.label + ': ' + (ctx.parsed.y != null ? ctx.parsed.y.toFixed(1) : '\u2014') }},
      }},
    }},
    scales: {{
      x: {{ ticks: {{ color: '#888', maxRotation: 45 }}, grid: {{ color: '#1e1e1e' }} }},
      y: {{ min: 0, max: 100, ticks: {{ color: '#888' }}, grid: {{ color: '#1e1e1e' }},
            title: {{ display: true, text: 'Composite Score (0-100)', color: '#888' }} }},
    }},
  }};
  if (_overallChart) {{
    _overallChart.data.labels = ALL_DATES;
    _overallChart.data.datasets = datasets;
    _overallChart.options.plugins.title.text = titleText;
    _overallChart.options.plugins.legend.display = syms.length <= 20;
    _overallChart.update('none');
  }} else {{
    _overallChart = new Chart(document.getElementById('overallChart'), {{
      type: 'line', data: {{ labels: ALL_DATES, datasets }}, options: chartOpts,
    }});
  }}
}}
</script>
</body>
</html>"""



def generate_report(db_path: Path, out_path: Path, days: int) -> bool:
    """Load history from DB and write HTML report. Returns True on success."""
    rows = _load_history(db_path, days)
    html = build_html(rows, days)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    out_path = Path(args.out)
    positions_file = Path(args.positions_file)

    if args.capture:
        scored = capture_scores(
            positions_file=positions_file,
            db_path=db_path,
            limit=args.limit,
        )
        if scored == 0:
            print("  Nothing was captured.")
            return

    print(f"  Generating report ({args.days}-day window) ...")
    ok = generate_report(db_path, out_path, args.days)
    if ok:
        print(f"  Report: {out_path}")
        if sys.platform == "win32":
            try:
                os.startfile(str(out_path))  # noqa: S606 — user-initiated, localhost only
            except Exception:
                pass
        elif sys.platform == "darwin":
            try:
                import subprocess
                subprocess.run(["open", str(out_path)], check=False)
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Portfolio Score Tracker — capture daily L1-L4 scores and generate HTML report"
    )
    parser.add_argument(
        "--capture",
        action="store_true",
        default=False,
        help="Score all holdings from positions cache and save to DB (default: view-only)",
    )
    parser.add_argument(
        "--out",
        default=str(_REPORT_OUT),
        help=f"Output HTML path (default: {_REPORT_OUT})",
    )
    parser.add_argument(
        "--db",
        default=str(_DB_PATH),
        help=f"SQLite database path (default: {_DB_PATH})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of calendar days to show in chart (default: 30)",
    )
    parser.add_argument(
        "--positions-file",
        default=str(_POSITIONS_CACHE),
        help=f"Path to positions JSON cache (default: {_POSITIONS_CACHE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of symbols to score (default: all; use small value for testing)",
    )
    main(parser.parse_args())

