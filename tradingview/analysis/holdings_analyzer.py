"""
Holdings Analyzer — track broker holdings over time and find winners/losers.

Reads .cache_global_positions.json (produced by global_portfolio_dashboard.py)
and persists daily snapshots to database/holdings.db for historical analysis.

Output: reports/holdings_analysis.html
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TV   = Path(__file__).parent.parent
ROOT = TV.parent

DEFAULT_CACHE = ROOT / ".cache_global_positions.json"
DEFAULT_DB    = ROOT / "database" / "holdings.db"
DEFAULT_OUT   = ROOT / "reports" / "holdings_analysis.html"

_DDL = """
CREATE TABLE IF NOT EXISTS holdings_snapshots (
    snapshot_date TEXT    NOT NULL,
    broker        TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    shares        REAL    NOT NULL DEFAULT 0,
    avg_cost      REAL    NOT NULL DEFAULT 0,
    current_price REAL    NOT NULL DEFAULT 0,
    market_val    REAL    NOT NULL DEFAULT 0,
    pnl           REAL    NOT NULL DEFAULT 0,
    pnl_pct       REAL    NOT NULL DEFAULT 0,
    currency      TEXT    NOT NULL DEFAULT 'USD',
    is_option     INTEGER NOT NULL DEFAULT 0,
    exchange      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_date, broker, symbol)
);
CREATE INDEX IF NOT EXISTS idx_hs_date   ON holdings_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_hs_broker ON holdings_snapshots(broker);
CREATE INDEX IF NOT EXISTS idx_hs_symbol ON holdings_snapshots(symbol);
"""


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    conn.commit()
    return conn


def _load_cache(cache_path: Path) -> tuple[str, list[dict]]:
    if not cache_path.exists():
        raise FileNotFoundError(
            f"Cache file not found: {cache_path}\n"
            "Run 'Global Portfolio Dashboard' first to populate it."
        )
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    saved_at = data.get("saved_at")
    if isinstance(saved_at, (int, float)):
        snap_date = datetime.fromtimestamp(saved_at, tz=timezone.utc).strftime("%Y-%m-%d")
    elif isinstance(saved_at, str):
        snap_date = saved_at[:10]
    else:
        snap_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return snap_date, data.get("positions", [])


def _save_snapshot(conn: sqlite3.Connection, snap_date: str, positions: list[dict]) -> int:
    rows = []
    for p in positions:
        symbol = (p.get("symbol") or "").strip()
        if not symbol:
            continue
        rows.append((
            snap_date,
            (p.get("broker") or "").strip(),
            symbol,
            float(p.get("shares", 0) or 0),
            float(p.get("avg_cost", 0) or 0),
            float(p.get("current_price", 0) or 0),
            float(p.get("market_val", 0) or 0),
            float(p.get("pnl", 0) or 0),
            float(p.get("pnl_pct", 0) or 0),
            (p.get("currency") or "USD").strip(),
            int(bool(p.get("is_option", False))),
            (p.get("exchange") or "").strip(),
        ))
    if rows:
        conn.executemany(
            """INSERT OR IGNORE INTO holdings_snapshots
               (snapshot_date, broker, symbol, shares, avg_cost, current_price,
                market_val, pnl, pnl_pct, currency, is_option, exchange)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
    return len(rows)


def _fetch_latest(conn: sqlite3.Connection) -> list[dict]:
    latest_date = conn.execute(
        "SELECT MAX(snapshot_date) FROM holdings_snapshots"
    ).fetchone()[0]
    if not latest_date:
        return []
    cols = ["snapshot_date", "broker", "symbol", "shares", "avg_cost", "current_price",
            "market_val", "pnl", "pnl_pct", "currency", "is_option", "exchange"]
    rows = conn.execute(
        """SELECT snapshot_date, broker, symbol, shares, avg_cost, current_price,
                  market_val, pnl, pnl_pct, currency, is_option, exchange
           FROM holdings_snapshots WHERE snapshot_date=? ORDER BY market_val DESC""",
        (latest_date,),
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _available_dates(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM holdings_snapshots ORDER BY snapshot_date DESC"
    ).fetchall()]


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _pnl_cls(v: float) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _perf_rows(items: list[dict]) -> str:
    html = ""
    for p in items:
        badge = " <span class='opt'>OPT</span>" if p["is_option"] else ""
        html += (
            f"<tr><td><strong>{p['symbol']}</strong>{badge}</td>"
            f"<td class='dim'>{p['broker']}</td>"
        f"<td class='num {_pnl_cls(p['pnl_pct'])}'>{_fmt_pct(p['pnl_pct'])}</td>"
        f"<td class='num {_pnl_cls(p['pnl'])}'>${p['pnl']:,.0f}</td>"
            f"<td class='num'>${p['market_val']:,.0f}</td></tr>\n"
        )
    return html


def _build_html(latest: list[dict], snap_date: str, dates: list[str], db_path: Path) -> str:
    total_mv  = sum(p["market_val"] for p in latest)
    total_pnl = sum(p["pnl"] for p in latest)

    # Broker breakdown
    broker_map: dict[str, dict] = {}
    for p in latest:
        b = p["broker"] or "Unknown"
        broker_map.setdefault(b, {"count": 0, "mv": 0.0, "pnl": 0.0})
        broker_map[b]["count"] += 1
        broker_map[b]["mv"]    += p["market_val"]
        broker_map[b]["pnl"]   += p["pnl"]

    broker_rows = ""
    for b, st in sorted(broker_map.items(), key=lambda x: -x[1]["mv"]):
        pct = (st["mv"] / total_mv * 100) if total_mv else 0
        broker_rows += (
            f"<tr><td><strong>{b}</strong></td>"
            f"<td class='num'>{st['count']}</td>"
            f"<td class='num'>${st['mv']:,.0f}</td>"
            f"<td class='num'>{pct:.1f}%</td>"
        f"<td class='num {_pnl_cls(st['pnl'])}'>${st['pnl']:,.0f}</td></tr>\n"
        )

    # Currency breakdown
    ccy_map: dict[str, float] = {}
    for p in latest:
        ccy_map[p["currency"]] = ccy_map.get(p["currency"], 0) + p["market_val"]
    ccy_rows = "".join(
        f"<tr><td>{ccy}</td>"
        f"<td class='num'>${mv:,.0f}</td>"
        f"<td class='num'>{(mv/total_mv*100) if total_mv else 0:.1f}%</td></tr>\n"
        for ccy, mv in sorted(ccy_map.items(), key=lambda x: -x[1])
    )

    # Top 10
    top10_rows = ""
    for i, p in enumerate(latest[:10], 1):
        pct = (p["market_val"] / total_mv * 100) if total_mv else 0
        badge = " <span class='opt'>OPT</span>" if p["is_option"] else ""
        top10_rows += (
            f"<tr><td class='num dim'>{i}</td>"
            f"<td><strong>{p['symbol']}</strong>{badge}</td>"
            f"<td class='dim'>{p['broker']}</td>"
            f"<td class='num'>${p['market_val']:,.0f}</td>"
            f"<td class='num'>{pct:.1f}%</td>"
          f"<td class='num {_pnl_cls(p['pnl_pct'])}'>{_fmt_pct(p['pnl_pct'])}</td></tr>\n"
        )

    # Best/Worst (exclude options)
    stocks = [p for p in latest if not p["is_option"] and p["market_val"] > 0]
    best5  = sorted(stocks, key=lambda p: -p["pnl_pct"])[:5]
    worst5 = sorted(stocks, key=lambda p:  p["pnl_pct"])[:5]

    # All positions table
    all_rows = ""
    for p in latest:
        badge = " <span class='opt'>OPT</span>" if p["is_option"] else ""
        cost_basis = p["shares"] * p["avg_cost"]
        all_rows += (
            f"<tr><td><strong>{p['symbol']}</strong>{badge}</td>"
            f"<td class='dim'>{p['broker']}</td>"
            f"<td class='dim'>{p['currency']}</td>"
            f"<td class='num'>{p['shares']:,.2f}</td>"
            f"<td class='num'>${p['avg_cost']:,.4f}</td>"
            f"<td class='num'>${p['current_price']:,.4f}</td>"
            f"<td class='num'>${cost_basis:,.0f}</td>"
            f"<td class='num'>${p['market_val']:,.0f}</td>"
          f"<td class='num {_pnl_cls(p['pnl'])}'>${p['pnl']:,.0f}</td>"
          f"<td class='num {_pnl_cls(p['pnl_pct'])}'>{_fmt_pct(p['pnl_pct'])}</td></tr>\n"
        )

    # Options section
    options_pos = [p for p in latest if p["is_option"]]
    opt_section = ""
    if options_pos:
        opt_rows = "".join(
            f"<tr><td><strong>{p['symbol']}</strong></td>"
            f"<td class='dim'>{p['broker']}</td>"
            f"<td class='num'>{p['shares']:,.0f}</td>"
            f"<td class='num'>${p['current_price']:,.4f}</td>"
            f"<td class='num'>${p['market_val']:,.0f}</td>"
          f"<td class='num {_pnl_cls(p['pnl_pct'])}'>{_fmt_pct(p['pnl_pct'])}</td></tr>\n"
            for p in options_pos
        )
        opt_section = f"""
<section class="card">
  <h2>🎯 Options Positions ({len(options_pos)})</h2>
  <table id="tbl-opt" class="sortable">
    <thead><tr>
      <th onclick="s('tbl-opt',0)">Symbol</th>
      <th onclick="s('tbl-opt',1)">Broker</th>
      <th onclick="s('tbl-opt',2)" class="num">Contracts</th>
      <th onclick="s('tbl-opt',3)" class="num">Price</th>
      <th onclick="s('tbl-opt',4)" class="num">Mkt Val</th>
      <th onclick="s('tbl-opt',5)" class="num">P&amp;L %</th>
    </tr></thead>
    <tbody>{opt_rows}</tbody>
  </table>
</section>"""

    history_note = (
        f"<p class='note'>📅 <strong>{len(dates)}</strong> snapshots available "
        f"({dates[-1] if dates else '—'} → {dates[0] if dates else '—'}). "
        "Run Global Portfolio Dashboard daily to build trends.</p>"
    ) if len(dates) > 1 else (
        "<p class='note'>ℹ️ Only 1 snapshot. Run again tomorrow to see holding-period trends.</p>"
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pnl_sign = "+" if total_pnl >= 0 else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Holdings Analysis — {snap_date}</title>
<style>
:root{{--bg:#0f1117;--card:#1a1f2e;--border:#2a2f40;--text:#e8eaed;--dim:#8b9299;
      --acc:#4a9eff;--pos:#4caf50;--neg:#f44336;--opt:#9c27b0}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;
     font-size:14px;line-height:1.5;padding:20px}}
h1{{font-size:1.6rem;color:var(--acc);margin-bottom:4px}}
h2{{font-size:1.1rem;color:var(--acc);margin-bottom:12px}}
.subtitle{{color:var(--dim);font-size:.85rem;margin-bottom:24px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;
       padding:20px;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin-bottom:20px}}
.metric{{background:var(--card);border:1px solid var(--border);border-radius:8px;
         padding:16px;text-align:center}}
.metric .val{{font-size:1.6rem;font-weight:700}}
.metric .lbl{{font-size:.78rem;color:var(--dim);margin-top:4px}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}}
th{{color:var(--dim);font-weight:600;font-size:.82rem;cursor:pointer;user-select:none}}
th:hover{{color:var(--acc)}}
td{{font-size:.88rem}}
.num{{text-align:right}}
.dim{{color:var(--dim)}}
.pos{{color:var(--pos);font-weight:600}}
.neg{{color:var(--neg);font-weight:600}}
.opt{{background:var(--opt);color:#fff;font-size:.65rem;padding:1px 5px;
      border-radius:3px;margin-left:4px;vertical-align:middle}}
.note{{color:var(--dim);font-size:.83rem;margin-bottom:16px}}
tr:hover td{{background:rgba(74,158,255,.05)}}
@media(max-width:768px){{.two-col{{grid-template-columns:1fr}}}}
</style>
<script>
function s(id,col){{
  var t=document.getElementById(id);
  var rows=Array.from(t.tBodies[0].rows);
  var asc=t.dataset.sort==col?t.dataset.asc!='1':true;
  t.dataset.sort=col;t.dataset.asc=asc?'1':'0';
  rows.sort(function(a,b){{
    var va=a.cells[col].innerText.replace(/[+$,% ]/g,'');
    var vb=b.cells[col].innerText.replace(/[+$,% ]/g,'');
    var na=parseFloat(va),nb=parseFloat(vb);
    if(!isNaN(na)&&!isNaN(nb))return asc?na-nb:nb-na;
    return asc?va.localeCompare(vb):vb.localeCompare(va);
  }});
  rows.forEach(function(r){{t.tBodies[0].appendChild(r)}});
}}
</script>
</head>
<body>
<h1>📊 Holdings Analysis</h1>
<p class="subtitle">Snapshot: <strong>{snap_date}</strong> &nbsp;|&nbsp; {now} &nbsp;|&nbsp; DB: {db_path.name}</p>
{history_note}
<div class="grid">
  <div class="metric"><div class="val">${total_mv:,.0f}</div><div class="lbl">Total Market Value</div></div>
  <div class="metric"><div class="val {_pnl_cls(total_pnl)}">{pnl_sign}${total_pnl:,.0f}</div><div class="lbl">Unrealised P&amp;L</div></div>
  <div class="metric"><div class="val">{len(latest)}</div><div class="lbl">Open Positions</div></div>
  <div class="metric"><div class="val">{len(broker_map)}</div><div class="lbl">Active Brokers</div></div>
  <div class="metric"><div class="val">{len(dates)}</div><div class="lbl">Historical Snapshots</div></div>
</div>
<div class="two-col">
  <section class="card">
    <h2>🏦 Broker Breakdown</h2>
    <table id="tbl-brk">
      <thead><tr>
        <th onclick="s('tbl-brk',0)">Broker</th>
        <th onclick="s('tbl-brk',1)" class="num">Pos</th>
        <th onclick="s('tbl-brk',2)" class="num">Mkt Val</th>
        <th onclick="s('tbl-brk',3)" class="num">Alloc%</th>
        <th onclick="s('tbl-brk',4)" class="num">P&amp;L</th>
      </tr></thead>
      <tbody>{broker_rows}</tbody>
    </table>
  </section>
  <section class="card">
    <h2>💱 Currency Exposure</h2>
    <table>
      <thead><tr><th>CCY</th><th class="num">Mkt Val</th><th class="num">Alloc%</th></tr></thead>
      <tbody>{ccy_rows}</tbody>
    </table>
  </section>
</div>
<section class="card">
  <h2>🏆 Top 10 by Market Value</h2>
  <table id="tbl-t10">
    <thead><tr>
      <th onclick="s('tbl-t10',0)">#</th>
      <th onclick="s('tbl-t10',1)">Symbol</th>
      <th onclick="s('tbl-t10',2)">Broker</th>
      <th onclick="s('tbl-t10',3)" class="num">Mkt Val</th>
      <th onclick="s('tbl-t10',4)" class="num">Alloc%</th>
      <th onclick="s('tbl-t10',5)" class="num">P&amp;L%</th>
    </tr></thead>
    <tbody>{top10_rows}</tbody>
  </table>
</section>
<div class="two-col">
  <section class="card">
    <h2>✅ Best Performers (stocks)</h2>
    <table id="tbl-best">
      <thead><tr>
        <th onclick="s('tbl-best',0)">Symbol</th><th onclick="s('tbl-best',1)">Broker</th>
        <th onclick="s('tbl-best',2)" class="num">P&amp;L%</th>
        <th onclick="s('tbl-best',3)" class="num">P&amp;L$</th>
        <th onclick="s('tbl-best',4)" class="num">Mkt Val</th>
      </tr></thead>
      <tbody>{_perf_rows(best5)}</tbody>
    </table>
  </section>
  <section class="card">
    <h2>⚠️ Worst Performers (stocks)</h2>
    <table id="tbl-wrst">
      <thead><tr>
        <th onclick="s('tbl-wrst',0)">Symbol</th><th onclick="s('tbl-wrst',1)">Broker</th>
        <th onclick="s('tbl-wrst',2)" class="num">P&amp;L%</th>
        <th onclick="s('tbl-wrst',3)" class="num">P&amp;L$</th>
        <th onclick="s('tbl-wrst',4)" class="num">Mkt Val</th>
      </tr></thead>
      <tbody>{_perf_rows(worst5)}</tbody>
    </table>
  </section>
</div>
{opt_section}
<section class="card">
  <h2>📋 All Positions ({len(latest)})</h2>
  <table id="tbl-all">
    <thead><tr>
      <th onclick="s('tbl-all',0)">Symbol</th>
      <th onclick="s('tbl-all',1)">Broker</th>
      <th onclick="s('tbl-all',2)">CCY</th>
      <th onclick="s('tbl-all',3)" class="num">Shares</th>
      <th onclick="s('tbl-all',4)" class="num">Avg Cost</th>
      <th onclick="s('tbl-all',5)" class="num">Current</th>
      <th onclick="s('tbl-all',6)" class="num">Cost Basis</th>
      <th onclick="s('tbl-all',7)" class="num">Mkt Val</th>
      <th onclick="s('tbl-all',8)" class="num">P&amp;L$</th>
      <th onclick="s('tbl-all',9)" class="num">P&amp;L%</th>
    </tr></thead>
    <tbody>{all_rows}</tbody>
  </table>
</section>
</body></html>"""


def main(args: argparse.Namespace) -> None:
    cache_path = Path(args.cache)
    db_path    = Path(args.db)
    out_path   = Path(args.out)

    print(f"  [holdings] reading cache → {cache_path}")
    snap_date, positions = _load_cache(cache_path)
    print(f"  [holdings] snapshot date: {snap_date} | positions: {len(positions)}")

    conn    = _open_db(db_path)
    n_saved = _save_snapshot(conn, snap_date, positions)
    print(f"  [holdings] saved {n_saved} rows to {db_path.name} (date={snap_date})")

    dates   = _available_dates(conn)
    latest  = _fetch_latest(conn)
    conn.close()

    if not latest:
        print("  [holdings] ERROR: no rows found after save — aborting")
        sys.exit(1)

    print(f"  [holdings] building report ({len(latest)} positions, {len(dates)} snapshots)…")
    html = _build_html(latest, snap_date, dates, db_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"  [holdings] report → {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Holdings Analyzer — track positions over time")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="Path to .cache_global_positions.json")
    parser.add_argument("--db",    default=str(DEFAULT_DB),    help="Path to holdings.db")
    parser.add_argument("--out",   default=str(DEFAULT_OUT),   help="Output HTML path")
    parser.add_argument("--days",  type=int, default=30,       help="Days of history (default: 30)")
    main(parser.parse_args())
