#!/usr/bin/env python3
"""
Market Screener — Shared Base Module
=====================================
Reusable signal classification, DB persistence, HTML report generation, and
pipeline integration for all market-specific screeners (AU, US, IND, SG).

This module follows the Single Responsibility Principle: each function handles
one concern (classify, persist, render, integrate). Market-specific stock
fetching is NOT here — that lives in each market's screener or fetcher.
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
TV = Path(__file__).parent.parent          # tradingview/
ROOT = TV.parent                           # repo root
FIRES_FILE = TV / "fires_raw.json"
SIGNAL_STACK = TV / "signal_stack.py"


# ── Signal classifier ──────────────────────────────────────────────────────────

def classify_signal(row: dict, rsi_min: float, rsi_max: float,
                    adx_min: float, bearish: bool) -> str:
    """
    Classify a row as BUY / WATCH / AVOID / N/A.

    Bullish mode:
      BUY    — RSI in [rsi_min, rsi_max] AND ADX > adx_min AND close > SMA200
      WATCH  — RSI in [30, rsi_min) AND ADX > (adx_min-5) AND close > SMA200
      AVOID  — everything else

    Bearish mode (inverse):
      BUY    — RSI in [100-rsi_max, 100-rsi_min] AND ADX > adx_min AND close < SMA200
      WATCH  — RSI in (100-rsi_min, 75] AND ADX > (adx_min-5) AND close < SMA200
      AVOID  — everything else
    """
    rsi = row.get("RSI")
    adx = row.get("ADX")
    close = row.get("close")
    sma = row.get("SMA200")

    if rsi is None or adx is None:
        return "N/A"

    if bearish:
        bear_min = max(0, 100 - rsi_max)
        bear_max = max(0, 100 - rsi_min)
        rsi_ok = bear_min <= rsi <= bear_max
        adx_ok = adx > adx_min
        sma_ok = (close is None or sma is None) or (close < sma)
        watch_rsi = rsi > bear_max and rsi <= 75
        watch_adx = adx > max(0, adx_min - 5)
        watch_sma = (close is None or sma is None) or (close < sma)
        if rsi_ok and adx_ok and sma_ok:
            return "BUY"
        if watch_rsi and watch_adx and watch_sma:
            return "WATCH"
        return "AVOID"
    else:
        rsi_ok = rsi_min <= rsi <= rsi_max
        adx_ok = adx > adx_min
        sma_ok = (close is None or sma is None) or (close > sma)
        watch_rsi = 30 <= rsi < rsi_min
        watch_adx = adx > max(0, adx_min - 5)
        watch_sma = (close is None or sma is None) or (close > sma)
        if rsi_ok and adx_ok and sma_ok:
            return "BUY"
        if watch_rsi and watch_adx and watch_sma:
            return "WATCH"
        return "AVOID"


# ── Database helpers ───────────────────────────────────────────────────────────

def get_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database with required schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS universe_cache (
            ticker       TEXT PRIMARY KEY,
            name         TEXT,
            universe     TEXT,
            market_cap   REAL,
            last_seen    TEXT
        );

        CREATE TABLE IF NOT EXISTS signal_runs (
            run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT NOT NULL,
            mode         TEXT,
            n_buy        INTEGER DEFAULT 0,
            n_watch      INTEGER DEFAULT 0,
            n_avoid      INTEGER DEFAULT 0,
            n_total      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER REFERENCES signal_runs(run_id),
            symbol       TEXT,
            name         TEXT,
            universe     TEXT,
            signal       TEXT,
            close        REAL,
            change_pct   REAL,
            rsi          REAL,
            adx          REAL,
            sma200       REAL,
            rel_vol      REAL,
            rec_mark     TEXT,
            market_cap   REAL,
            run_at       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_signals_symbol   ON signals(symbol);
        CREATE INDEX IF NOT EXISTS idx_signals_run_id   ON signals(run_id);
        CREATE INDEX IF NOT EXISTS idx_signals_signal   ON signals(signal);
    """)
    conn.commit()
    return conn


def upsert_universe(conn: sqlite3.Connection, ticker: str, name: str,
                    universe: str, market_cap: float) -> None:
    conn.execute("""
        INSERT INTO universe_cache (ticker, name, universe, market_cap, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name       = excluded.name,
            universe   = excluded.universe,
            market_cap = excluded.market_cap,
            last_seen  = excluded.last_seen
    """, (ticker, name, universe, market_cap, datetime.now(timezone.utc).isoformat()))


def save_signal(conn: sqlite3.Connection, run_id: int, row: dict, signal: str) -> None:
    conn.execute("""
        INSERT INTO signals
          (run_id, symbol, name, universe, signal, close, change_pct,
           rsi, adx, sma200, rel_vol, rec_mark, market_cap, run_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        row.get("tv_symbol", ""),
        (row.get("description") or row.get("name") or "")[:60],
        row.get("_universe", ""),
        signal,
        row.get("close"),
        row.get("change"),
        row.get("RSI"),
        row.get("ADX"),
        row.get("SMA200"),
        row.get("relative_volume_10d_calc"),
        row.get("recommendation_mark"),
        row.get("market_cap_basic"),
        datetime.now(timezone.utc).isoformat(),
    ))


# ── TV Scanner helpers ─────────────────────────────────────────────────────────

import urllib.request
import urllib.error

STOCK_COLS = [
    "name", "description", "close", "change",
    "volume", "RSI", "ADX", "SMA200",
    "relative_volume_10d_calc", "recommendation_mark",
    "market_cap_basic", "type", "subtype",
]


def tv_scan(url: str, filters: list, sort_by: str, count: int,
            retries: int = 2) -> list[dict]:
    """POST to TV scanner. Returns list of record dicts."""
    payload = {
        "columns": STOCK_COLS,
        "filter": filters,
        "sort": {"sortBy": sort_by, "sortOrder": "desc"},
        "range": [0, count],
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://www.tradingview.com",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            rows = []
            for item in data.get("data", []):
                sym = item.get("s", "")
                d = item.get("d", [])
                rec = {"tv_symbol": sym}
                for i, col in enumerate(STOCK_COLS):
                    rec[col] = d[i] if i < len(d) else None
                rows.append(rec)
            return rows
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(1.5)
            else:
                print(f"  ✗ TV scanner error: {e.reason}")
                return []
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5)
            else:
                print(f"  ✗ TV scanner error: {e}")
                return []
    return []


# ── Pipeline integration ───────────────────────────────────────────────────────

def append_to_fires(buy_rows: list[dict], source_tag: str) -> int:
    """Append BUY-signal rows to fires_raw.json for signal_stack.py processing."""
    if not buy_rows:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    new_fires = []
    for i, r in enumerate(buy_rows):
        sym = r.get("tv_symbol", "")
        if not sym or ":" not in sym:
            continue
        rsi = r.get("RSI")
        adx = r.get("ADX")
        msg = f"{source_tag} BUY:1"
        if rsi is not None:
            msg += f", rsi:{rsi:.1f}"
        if adx is not None:
            msg += f", ADX:{adx:.1f}"
        rel_vol = r.get("relative_volume_10d_calc")
        if rel_vol is not None:
            msg += f", relVol:{rel_vol:.2f}"
        universe = r.get("_universe", "")
        if universe:
            msg += f", src:{universe}"

        new_fires.append({
            "fire_id": int(time.time() * 1000) + i,
            "alert_id": 0,
            "symbol": sym,
            "pro_symbol": sym,
            "resolution": "D",
            "sound_file": None,
            "sound_duration": 0,
            "popup": False,
            "cross_interval": False,
            "message": msg,
            "fire_time": now_iso,
            "bar_time": now_iso,
            "name": f"{source_tag}_{sym}",
            "kinds": [source_tag],
            "close": r.get("close"),
            "source": source_tag,
        })

    if not new_fires:
        return 0

    if FIRES_FILE.exists():
        try:
            with open(FIRES_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {"all": []}
    else:
        data = {"all": []}

    if not isinstance(data.get("all"), list):
        data["all"] = []

    # Remove stale fires from the same source to prevent duplicates on re-run
    data["all"] = [f for f in data["all"] if f.get("source") != source_tag]
    data["all"] = new_fires + data["all"]
    with open(FIRES_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)

    return len(new_fires)


def run_signal_pipeline(source_tag: str, skip_moomoo: bool = False,
                        skip_india: bool = False) -> None:
    """Invoke signal_stack.py to score all fires through the full L1-L8 pipeline."""
    script = SIGNAL_STACK
    if not script.exists():
        script = Path(__file__).parent.parent / "signal_stack.py"
    if not script.exists():
        print("  [WARN] signal_stack.py not found — skipping pipeline run")
        return

    skip_flags = []
    if skip_moomoo:
        skip_flags.append("--no-moomoo")
    if skip_india:
        skip_flags.append("--no-india")

    cmd = [
        sys.executable, str(script),
        "--n", "200",
        "--threshold", "55",
        "--no-external",
        "--source-filter", source_tag,
        *skip_flags,
    ]

    print("\n" + "=" * 70)
    print("  Scoring through signal_stack.py full pipeline (L1-L8)…")
    print("=" * 70)

    result = subprocess.run(cmd, cwd=str(script.parent))
    if result.returncode == 0:
        print(f"\n  Pipeline complete.")
    else:
        print(f"  [WARN] signal_stack.py exited with code {result.returncode}")


# ── Screener runner (shared logic) ─────────────────────────────────────────────

def run_screener_core(
    *,
    market_label: str,
    sections: dict[str, list[dict]],
    db_path: Path,
    rsi_min: float,
    rsi_max: float,
    adx_min: float,
    bearish: bool,
    pipeline: bool,
    source_tag: str,
    skip_moomoo: bool = False,
    skip_india: bool = False,
) -> dict[str, list[dict]]:
    """
    Core screener logic: classify signals, persist to DB, run pipeline.
    `sections` is a dict like {"large_cap": [rows], "mid_cap": [rows]}.
    Returns the same dict with 'signal' key added to each row.
    """
    conn = get_db(db_path)
    run_at = datetime.now(timezone.utc).isoformat()

    # Persist universe
    for section_key, rows in sections.items():
        for r in rows:
            ticker = (r.get("tv_symbol", "").split(":")[-1]
                      if ":" in r.get("tv_symbol", "") else r.get("tv_symbol", ""))
            upsert_universe(conn, ticker,
                            r.get("description") or r.get("name") or ticker,
                            section_key, r.get("market_cap_basic") or 0)
    conn.commit()

    # Classify signals
    print("\n  Applying technical signals...")
    cur = conn.execute(
        "INSERT INTO signal_runs (run_at, mode, n_buy, n_watch, n_avoid, n_total) "
        "VALUES (?, ?, 0, 0, 0, 0)",
        (run_at, "bearish" if bearish else "bullish"),
    )
    run_id = cur.lastrowid

    totals = {"BUY": 0, "WATCH": 0, "AVOID": 0, "N/A": 0}
    all_buy_rows: list[dict] = []
    total_rows = sum(len(v) for v in sections.values())
    progress = 0

    for section_key, rows in sections.items():
        for r in rows:
            progress += 1
            if progress % 20 == 0 or progress == total_rows:
                print(f"  Classifying signals: {progress}/{total_rows} ...",
                      end="\r", flush=True)
            sig = classify_signal(r, rsi_min, rsi_max, adx_min, bearish)
            r["signal"] = sig
            totals[sig] = totals.get(sig, 0) + 1
            save_signal(conn, run_id, r, sig)
            if sig == "BUY":
                all_buy_rows.append(r)

    conn.execute(
        "UPDATE signal_runs SET n_buy=?, n_watch=?, n_avoid=?, n_total=? WHERE run_id=?",
        (totals["BUY"], totals["WATCH"], totals.get("AVOID", 0),
         sum(totals.values()), run_id),
    )
    conn.commit()
    conn.close()

    # Summary
    total = sum(totals.values())
    print(f"\n  Results: {total} total  |  "
          f"🟢 {totals['BUY']} BUY  |  "
          f"👁️ {totals['WATCH']} WATCH  |  "
          f"🔴 {totals.get('AVOID', 0)} AVOID")

    # Print top BUY signals
    if all_buy_rows:
        buy_sorted = sorted(all_buy_rows, key=lambda r: -(r.get("ADX") or 0))
        print(f"\n  Top BUY signals (sorted by ADX):")
        print(f"  {'Ticker':<10} {'Name':<35} {'Close':>8} {'RSI':>6} {'ADX':>6} "
              f"{'RelVol':>7} {'Universe':<8}")
        print("  " + "-" * 85)
        for r in buy_sorted[:20]:
            sym = r.get("tv_symbol", "")
            ticker = sym.split(":")[-1] if ":" in sym else sym
            name = (r.get("description") or r.get("name") or ticker)[:33]
            close = r.get("close")
            rsi = r.get("RSI")
            adx = r.get("ADX")
            rv = r.get("relative_volume_10d_calc")
            uni = r.get("_universe", "")
            print(f"  {ticker:<10} {name:<35} "
                  f"{_fmt_num(close):>8}  {_fmt_num(rsi, '.1f'):>6}  "
                  f"{_fmt_num(adx, '.1f'):>6}  {_fmt_num(rv, '.2f'):>6}x  {uni:<8}")
        if len(buy_sorted) > 20:
            print(f"  ... and {len(buy_sorted) - 20} more in the HTML report")

    # Pipeline
    if pipeline:
        n_appended = append_to_fires(all_buy_rows, source_tag)
        print(f"\n  Appended {n_appended} BUY signals to fires_raw.json")
        run_signal_pipeline(source_tag, skip_moomoo=skip_moomoo,
                            skip_india=skip_india)

    return sections


# ── HTML report builder ────────────────────────────────────────────────────────

_SIGNAL_COLORS = {
    "BUY":   ("#00c853", "#e8f5e9"),
    "WATCH": ("#ff8f00", "#fff3e0"),
    "AVOID": ("#d32f2f", "#ffebee"),
    "N/A":   ("#78909c", "#eceff1"),
}


def _signal_badge(signal: str) -> str:
    fg, _ = _SIGNAL_COLORS.get(signal, ("#78909c", "#eceff1"))
    icons = {"BUY": "🟢", "WATCH": "👁️", "AVOID": "🔴", "N/A": "⬜"}
    icon = icons.get(signal, "")
    return (f'<span style="background:{fg};color:#fff;padding:2px 8px;'
            f'border-radius:4px;font-weight:bold;font-size:0.85em">'
            f'{icon} {signal}</span>')


def _fmt_num(v, fmt=".2f", fallback="—") -> str:
    try:
        return format(float(v), fmt) if v is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _fmt_cap(v, currency_prefix: str = "$") -> str:
    """Format market cap in human-readable form."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1e12:
        return f"{currency_prefix}{v/1e12:.2f}T"
    if v >= 1e9:
        return f"{currency_prefix}{v/1e9:.1f}B"
    if v >= 1e6:
        return f"{currency_prefix}{v/1e6:.0f}M"
    return f"{currency_prefix}{v:,.0f}"


def build_report_html(
    *,
    title: str,
    subtitle: str,
    sections: dict[str, tuple[str, str, list[dict]]],
    rsi_min: float,
    rsi_max: float,
    adx_min: float,
    bearish: bool,
    run_at: str,
    header_color: str = "#003087",
    currency_prefix: str = "$",
    chart_base_url: str = "https://www.tradingview.com/chart/?symbol=",
) -> str:
    """
    Build an HTML signal report.

    sections: dict mapping key -> (title, icon_html, rows_list)
    Each row must have a 'signal' key from classify_signal().
    """
    all_rows = []
    for _, (_, _, rows) in sections.items():
        all_rows.extend(rows)

    n_buy = sum(1 for r in all_rows if r.get("signal") == "BUY")
    n_watch = sum(1 for r in all_rows if r.get("signal") == "WATCH")
    n_avoid = sum(1 for r in all_rows if r.get("signal") == "AVOID")
    n_na = sum(1 for r in all_rows if r.get("signal") == "N/A")
    mode_label = "Bearish" if bearish else "Bullish"

    def _section_table(rows: list, sec_title: str, icon: str) -> str:
        if not rows:
            return f"<h2>{icon} {sec_title}</h2><p style='color:#888'>No data.</p>"

        order = {"BUY": 0, "WATCH": 1, "AVOID": 2, "N/A": 3}
        rows = sorted(rows, key=lambda r: (
            order.get(r.get("signal", "N/A"), 9),
            -(r.get("ADX") or 0),
        ))

        trs = ""
        for r in rows:
            sig = r.get("signal", "N/A")
            _, bg = _SIGNAL_COLORS.get(sig, ("#78909c", "#eceff1"))
            sym = r.get("tv_symbol", "")
            ticker = sym.split(":")[-1] if ":" in sym else sym
            name = (r.get("description") or r.get("name") or ticker)[:45]
            close = _fmt_num(r.get("close"))
            chg = r.get("change")
            chg_color = "#00c853" if (chg or 0) >= 0 else "#d32f2f"
            chg_str = (f'<span style="color:{chg_color}">{"+" if chg >= 0 else ""}{chg:.2f}%</span>'
                       ) if chg is not None else "&mdash;"
            rsi = _fmt_num(r.get("RSI"), ".1f")
            adx = _fmt_num(r.get("ADX"), ".1f")
            sma200 = _fmt_num(r.get("SMA200"))
            relvol = _fmt_num(r.get("relative_volume_10d_calc"), ".2f")
            cap = _fmt_cap(r.get("market_cap_basic"), currency_prefix)
            rec = r.get("recommendation_mark") or "—"
            tv_link = f"{chart_base_url}{sym}"
            trs += (
                f'<tr style="background:{bg}">'
                f'<td><a href="{tv_link}" target="_blank" style="font-weight:bold;color:#1565c0">'
                f'{ticker}</a></td>'
                f'<td style="font-size:0.85em;max-width:180px">{name}</td>'
                f'<td>{_signal_badge(sig)}</td>'
                f'<td class="num">{currency_prefix}{close}</td>'
                f'<td class="num">{chg_str}</td>'
                f'<td class="num">{rsi}</td>'
                f'<td class="num">{adx}</td>'
                f'<td class="num">{currency_prefix}{sma200}</td>'
                f'<td class="num">{relvol}x</td>'
                f'<td class="num">{cap}</td>'
                f'<td style="font-size:0.8em">{rec}</td>'
                f'</tr>\n'
            )

        n_buy_sec = sum(1 for r in rows if r.get("signal") == "BUY")
        n_watch_sec = sum(1 for r in rows if r.get("signal") == "WATCH")

        return f"""
<h2 style="margin-top:2em">{icon} {sec_title}
  <span style="font-size:0.7em;font-weight:normal;color:#555">
    {len(rows)} securities · {n_buy_sec} BUY · {n_watch_sec} WATCH
  </span>
</h2>
<div style="overflow-x:auto">
<table>
<thead>
<tr>
  <th>Ticker</th><th>Name</th><th>Signal</th>
  <th class="num">Price</th><th class="num">Chg%</th>
  <th class="num">RSI</th><th class="num">ADX</th>
  <th class="num">SMA200</th><th class="num">RelVol</th>
  <th class="num">Mkt Cap</th><th>Analyst</th>
</tr>
</thead>
<tbody>
{trs}
</tbody>
</table>
</div>
"""

    sections_html = ""
    for key, (sec_title, icon, rows) in sections.items():
        sections_html += _section_table(rows, sec_title, icon)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f5; color: #212121; padding: 20px; }}
  .header {{ background: {header_color}; color: white; padding: 20px 24px;
             border-radius: 8px; margin-bottom: 20px; }}
  .header h1 {{ font-size: 1.5em; font-weight: 700; }}
  .header .sub {{ font-size: 0.9em; opacity: 0.8; margin-top: 4px; }}
  .kpi-bar {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  .kpi {{ background: white; border-radius: 8px; padding: 14px 20px;
          box-shadow: 0 1px 4px rgba(0,0,0,.1); flex: 1; min-width: 100px; text-align:center; }}
  .kpi .v {{ font-size: 2em; font-weight: 700; }}
  .kpi .l {{ font-size: 0.8em; color: #555; margin-top: 2px; }}
  .kpi.buy   .v {{ color: #00c853; }}
  .kpi.watch .v {{ color: #ff8f00; }}
  .kpi.avoid .v {{ color: #d32f2f; }}
  .kpi.na    .v {{ color: #78909c; }}
  h2 {{ font-size: 1.15em; margin-bottom: 10px; color: {header_color};
        border-bottom: 2px solid {header_color}; padding-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border-radius: 8px; overflow: hidden;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); font-size:0.88em; }}
  th {{ background: {header_color}; color: white; padding: 8px 10px;
        font-size: 0.82em; text-transform: uppercase; letter-spacing: .04em; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #e0e0e0; }}
  tr:last-child td {{ border-bottom: none; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .footer {{ text-align:center; font-size:0.8em; color:#888; margin-top:30px; }}
  .legend {{ background: white; border-radius: 8px; padding: 12px 16px;
             box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 16px;
             font-size: 0.85em; }}
  .legend span {{ margin-right: 16px; }}
</style>
</head>
<body>
<div class="header">
  <h1>{title}</h1>
  <div class="sub">
    {subtitle} &nbsp;|&nbsp;
    Mode: {mode_label} &nbsp;|&nbsp;
    RSI {rsi_min}–{rsi_max} &nbsp;|&nbsp;
    ADX &gt; {adx_min} &nbsp;|&nbsp;
    Generated: {run_at}
  </div>
</div>

<div class="kpi-bar">
  <div class="kpi buy">  <div class="v">{n_buy}</div>  <div class="l">🟢 BUY</div></div>
  <div class="kpi watch"><div class="v">{n_watch}</div><div class="l">👁️ WATCH</div></div>
  <div class="kpi avoid"><div class="v">{n_avoid}</div><div class="l">🔴 AVOID</div></div>
  <div class="kpi na">   <div class="v">{n_na}</div>   <div class="l">⬜ N/A</div></div>
  <div class="kpi">      <div class="v">{len(all_rows)}</div><div class="l">Total</div></div>
</div>

<div class="legend">
  <strong>Signal key:</strong>
  <span>🟢 <strong>BUY</strong> — RSI {rsi_min}–{rsi_max}, ADX&gt;{adx_min},
    {'close&lt;SMA200' if bearish else 'close&gt;SMA200'}</span>
  <span>👁️ <strong>WATCH</strong> — building momentum, near threshold</span>
  <span>🔴 <strong>AVOID</strong> — does not pass filters</span>
  <span>⬜ <strong>N/A</strong> — insufficient data</span>
</div>

{sections_html}

<div class="footer">
  {title} · Data: TradingView ·
  Generated {run_at} · Not financial advice
</div>
</body>
</html>"""
