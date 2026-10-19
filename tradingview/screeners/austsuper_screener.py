#!/usr/bin/env python3
"""
AustralianSuper Member Direct — ASX 300 Screener
==================================================
Downloads the tradeable share universe for AustralianSuper Member Direct:
  • ASX 300  : official S&P/ASX 300 constituents (TV index symbolset SYML:ASX;XKO)

Applies a standard technical signal pass:
  BUY    — RSI in [rsi_min, rsi_max] AND ADX > adx_min AND close > SMA200
  WATCH  — RSI in [30, rsi_min) AND ADX > adx_min AND close > SMA200
  AVOID  — close < SMA200  OR  RSI > 75  OR  ADX < 15

Caches the universe in SQLite (tradingview/austsuper_member_direct.db) and
stores every signal run so you can review history.

Optionally appends BUY signals to fires_raw.json so signal_stack.py can
score them through the full L1-L8 pipeline.

Usage:
  python austsuper_screener.py                          # full run (ASX 300)
  python austsuper_screener.py --refresh                # force re-download universe
  python austsuper_screener.py --bullish                # default: RSI 45-72, ADX>20
  python austsuper_screener.py --bearish                # RSI 25-45, close<SMA200
  python austsuper_screener.py                          # full run (pipeline on by default)
  python austsuper_screener.py --no-pipeline            # screener only, skip signal_stack.py
  python austsuper_screener.py --rsi-min 50 --adx-min 25
  python austsuper_screener.py --out /path/to/out.html
"""
import sys
import json
import time
import sqlite3
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Windows: force UTF-8 console ──────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ──────────────────────────────────────────────────────────────────────
TV   = Path(__file__).parent.parent          # tradingview/
ROOT = TV.parent                             # repo root
# NOTE: If a legacy austsuper_member_direct.db exists in tradingview/,
# copy it to database/ before first run: shutil.copy("tradingview/austsuper_member_direct.db", DB_PATH)
DB_PATH     = ROOT / "database" / "austsuper_member_direct.db"
FIRES_FILE  = TV / "fires_raw.json"
DEFAULT_OUT = ROOT / "reports" / "austsuper_report.html"

# ── TV Scanner ─────────────────────────────────────────────────────────────────
_TV_AU_URL = "https://scanner.tradingview.com/australia/scan"

# Official S&P/ASX 300 index constituent set (XKO) — not a market-cap proxy
_ASX300_SYMBOLSET = "SYML:ASX;XKO"

# Columns fetched from TV scanner
_STOCK_COLS = [
    "name", "description", "close", "change",
    "volume", "RSI", "ADX", "SMA200",
    "relative_volume_10d_calc", "recommendation_mark",
    "market_cap_basic", "type", "subtype",
]

# ── Database helpers ───────────────────────────────────────────────────────────

def _get_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database with required schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS universe_cache (
            ticker       TEXT PRIMARY KEY,
            name         TEXT,
            universe     TEXT,    -- 'asx300'
            market_cap   REAL,
            last_seen    TEXT
        );

        CREATE TABLE IF NOT EXISTS signal_runs (
            run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT NOT NULL,
            mode         TEXT,    -- 'bullish' | 'bearish'
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
            universe     TEXT,    -- 'asx300'
            signal       TEXT,    -- 'BUY' | 'WATCH' | 'AVOID' | 'N/A'
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


def _upsert_universe(conn: sqlite3.Connection, ticker: str, name: str,
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


def _save_signal(conn: sqlite3.Connection, run_id: int, row: dict, signal: str) -> None:
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

def _tv_scan(filters: list, sort_by: str, count: int, retries: int = 2,
             symbolset: str | None = None) -> list[dict]:
    """POST to TV Australia scanner. Returns list of record dicts."""
    payload = {
        "columns": _STOCK_COLS,
        "filter":  filters,
        "sort":    {"sortBy": sort_by, "sortOrder": "desc"},
        "range":   [0, count],
    }
    if symbolset:
        payload["symbols"] = {"symbolset": [symbolset]}
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        _TV_AU_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "Mozilla/5.0",
            "Origin":       "https://www.tradingview.com",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            rows = []
            for item in data.get("data", []):
                sym = item.get("s", "")
                d   = item.get("d", [])
                rec = {"tv_symbol": sym}
                for i, col in enumerate(_STOCK_COLS):
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


# ── Universe downloaders ───────────────────────────────────────────────────────

def download_asx300(verbose: bool = True) -> list[dict]:
    """
    Fetch the official S&P/ASX 300 constituents (index symbolset XKO) from TV.
    Returns records with _universe='asx300'.
    Excludes funds (ETFs, LICs, REIT trusts) — Member Direct permits the ASX 300
    shares only. CDIs/depositary receipts (type='dr') ARE index members and ARE
    tradeable on ASX, so they are kept.
    """
    if verbose:
        print("  Fetching S&P/ASX 300 constituents from TradingView...", end=" ", flush=True)

    rows = _tv_scan(
        filters=[],
        sort_by="market_cap_basic",
        count=400,
        symbolset=_ASX300_SYMBOLSET,
    )

    asx300 = []
    for r in rows:
        if r.get("type") == "fund":
            continue
        r["_universe"] = "asx300"
        asx300.append(r)

    if verbose:
        print(f"{len(asx300)} stocks")
    return asx300


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
    rsi   = row.get("RSI")
    adx   = row.get("ADX")
    close = row.get("close")
    sma   = row.get("SMA200")

    if rsi is None or adx is None:
        return "N/A"

    if bearish:
        bear_min = max(0, 100 - rsi_max)
        bear_max = max(0, 100 - rsi_min)
        rsi_ok  = bear_min <= rsi <= bear_max
        adx_ok  = adx > adx_min
        sma_ok  = (close is None or sma is None) or (close < sma)
        watch_rsi = rsi > bear_max and rsi <= 75
        watch_adx = adx > max(0, adx_min - 5)
        watch_sma = (close is None or sma is None) or (close < sma)
        if rsi_ok and adx_ok and sma_ok:
            return "BUY"
        if watch_rsi and watch_adx and watch_sma:
            return "WATCH"
        return "AVOID"
    else:
        rsi_ok  = rsi_min <= rsi <= rsi_max
        adx_ok  = adx > adx_min
        sma_ok  = (close is None or sma is None) or (close > sma)
        watch_rsi = 30 <= rsi < rsi_min
        watch_adx = adx > max(0, adx_min - 5)
        watch_sma = (close is None or sma is None) or (close > sma)
        if rsi_ok and adx_ok and sma_ok:
            return "BUY"
        if watch_rsi and watch_adx and watch_sma:
            return "WATCH"
        return "AVOID"


# ── Pipeline integration ───────────────────────────────────────────────────────
SIGNAL_STACK = TV / "signal_stack.py"

def append_to_fires(buy_rows: list[dict]) -> int:
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
        msg = "AUSTSUPER BUY:1"
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
            "fire_id":        int(time.time() * 1000) + i,  # +i avoids collision within same ms
            "alert_id":       0,
            "symbol":         sym,
            "pro_symbol":     sym,
            "resolution":     "D",
            "sound_file":     None,
            "sound_duration": 0,
            "popup":          False,
            "cross_interval": False,
            "message":        msg,
            "fire_time":      now_iso,
            "bar_time":       now_iso,
            "name":           f"austsuper_{sym}",
            "kinds":          ["austsuper_screener"],
            "close":          r.get("close"),
            "source":         "austsuper_screener",
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

    # Every run re-screens the whole ASX 300, so any earlier austsuper fire is a
    # stale snapshot — keeping it would re-score the same stock a second time
    # (and orphaned ETF fires from the pre-ASX300-only universe would never expire).
    data["all"] = [f for f in data["all"] if f.get("source") != "austsuper_screener"]

    # Prepend (signal_stack reads newest first)
    data["all"] = new_fires + data["all"]
    with open(FIRES_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)

    return len(new_fires)


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


def _fmt_cap(v) -> str:
    """Format market cap (raw AUD) in human-readable form."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1e12:
        return f"A${v/1e12:.2f}T"
    if v >= 1e9:
        return f"A${v/1e9:.1f}B"
    if v >= 1e6:
        return f"A${v/1e6:.0f}M"
    return f"A${v:,.0f}"


def build_html(results: dict, args, run_at: str) -> str:
    """
    Build the HTML report.
    results = {"asx300": [rows]}  — each row already has 'signal' key.
    """
    all_rows = list(results.get("asx300", []))
    n_buy   = sum(1 for r in all_rows if r.get("signal") == "BUY")
    n_watch = sum(1 for r in all_rows if r.get("signal") == "WATCH")
    n_avoid = sum(1 for r in all_rows if r.get("signal") == "AVOID")
    n_na    = sum(1 for r in all_rows if r.get("signal") == "N/A")
    mode_label = "Bearish" if args.bearish else "Bullish"

    def _section_table(rows: list, title: str, icon: str) -> str:
        if not rows:
            return f"<h2>{icon} {title}</h2><p style='color:#888'>No data.</p>"

        # Sort: BUY first, then WATCH, then rest; within BUY sort by ADX desc
        order = {"BUY": 0, "WATCH": 1, "AVOID": 2, "N/A": 3}
        rows = sorted(rows, key=lambda r: (
            order.get(r.get("signal", "N/A"), 9),
            -(r.get("ADX") or 0),
        ))

        trs = ""
        for r in rows:
            sig = r.get("signal", "N/A")
            _, bg = _SIGNAL_COLORS.get(sig, ("#78909c", "#eceff1"))
            sym      = r.get("tv_symbol", "")
            ticker   = sym.split(":")[-1] if ":" in sym else sym
            name     = (r.get("description") or r.get("name") or ticker)[:45]
            close    = _fmt_num(r.get("close"))
            chg      = r.get("change")
            chg_color = "#00c853" if (chg or 0) >= 0 else "#d32f2f"
            chg_str   = (f'<span style="color:{chg_color}">{"+" if chg >= 0 else ""}{chg:.2f}%</span>'
                         ) if chg is not None else "&mdash;"
            rsi      = _fmt_num(r.get("RSI"), ".1f")
            adx      = _fmt_num(r.get("ADX"), ".1f")
            sma200   = _fmt_num(r.get("SMA200"))
            relvol   = _fmt_num(r.get("relative_volume_10d_calc"), ".2f")
            cap      = _fmt_cap(r.get("market_cap_basic"))
            rec      = r.get("recommendation_mark") or "—"
            tv_link  = f"https://www.tradingview.com/chart/?symbol=ASX:{ticker}"
            trs += (
                f'<tr style="background:{bg}">'
                f'<td><a href="{tv_link}" target="_blank" style="font-weight:bold;color:#1565c0">'
                f'{ticker}</a></td>'
                f'<td style="font-size:0.85em;max-width:180px">{name}</td>'
                f'<td>{_signal_badge(sig)}</td>'
                f'<td class="num">A${close}</td>'
                f'<td class="num">{chg_str}</td>'
                f'<td class="num">{rsi}</td>'
                f'<td class="num">{adx}</td>'
                f'<td class="num">A${sma200}</td>'
                f'<td class="num">{relvol}x</td>'
                f'<td class="num">{cap}</td>'
                f'<td style="font-size:0.8em">{rec}</td>'
                f'</tr>\n'
            )

        n_buy_sec   = sum(1 for r in rows if r.get("signal") == "BUY")
        n_watch_sec = sum(1 for r in rows if r.get("signal") == "WATCH")

        return f"""
<h2 style="margin-top:2em">{icon} {title}
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

    asx300_section = _section_table(results.get("asx300", []),
                                    "ASX 300 Stocks", "&#127462;&#127482;")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AustralianSuper Member Direct — Signal Screener</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f5; color: #212121; padding: 20px; }}
  .header {{ background: #003087; color: white; padding: 20px 24px;
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
  h2 {{ font-size: 1.15em; margin-bottom: 10px; color: #003087;
        border-bottom: 2px solid #003087; padding-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border-radius: 8px; overflow: hidden;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); font-size:0.88em; }}
  th {{ background: #003087; color: white; padding: 8px 10px;
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
  <h1>🏦 AustralianSuper Member Direct — Signal Screener</h1>
  <div class="sub">
    Mode: {mode_label} &nbsp;|&nbsp;
    RSI {args.rsi_min}–{args.rsi_max} &nbsp;|&nbsp;
    ADX &gt; {args.adx_min} &nbsp;|&nbsp;
    Generated: {run_at} AEST
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
  <span>🟢 <strong>BUY</strong> — RSI {args.rsi_min}–{args.rsi_max}, ADX&gt;{args.adx_min},
    {'close&lt;SMA200' if args.bearish else 'close&gt;SMA200'}</span>
  <span>👁️ <strong>WATCH</strong> — building momentum, near threshold</span>
  <span>🔴 <strong>AVOID</strong> — does not pass filters</span>
  <span>⬜ <strong>N/A</strong> — insufficient data</span>
</div>

{asx300_section}

<div class="footer">
  AustralianSuper Member Direct Screener · Data: TradingView ·
  Generated {run_at} · Not financial advice
</div>
</body>
</html>"""


# ── Main runner ────────────────────────────────────────────────────────────────

def run_screener(args) -> dict:
    """
    Full screener run. Returns dict with key 'asx300' (list of rows + signal).
    Also saves to DB and optionally appends to fires_raw.json.
    """
    conn = _get_db(args.db)
    run_at = datetime.now(timezone.utc).isoformat()

    print("\n" + "=" * 70)
    print("  AustralianSuper Member Direct — ASX 300 Signal Screener")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
          f"Mode: {'Bearish' if args.bearish else 'Bullish'}  |  "
          f"RSI {args.rsi_min}-{args.rsi_max}  ADX>{args.adx_min}")
    print("=" * 70)

    # ── Download universe ────────────────────────────────────────────────────
    results: dict[str, list] = {}

    asx300 = download_asx300(verbose=True)
    # Persist universe to DB
    for r in asx300:
        ticker = (r.get("tv_symbol", "").split(":")[-1]
                  if ":" in r.get("tv_symbol", "") else r.get("tv_symbol", ""))
        _upsert_universe(conn, ticker,
                         r.get("description") or r.get("name") or ticker,
                         "asx300", r.get("market_cap_basic") or 0)
    results["asx300"] = asx300

    conn.commit()

    # ── Apply signals ────────────────────────────────────────────────────────
    print("\n  Applying technical signals...")

    # Create signal run record
    cur = conn.execute(
        "INSERT INTO signal_runs (run_at, mode, n_buy, n_watch, n_avoid, n_total) "
        "VALUES (?, ?, 0, 0, 0, 0)",
        (run_at, "bearish" if args.bearish else "bullish"),
    )
    run_id = cur.lastrowid

    totals = {"BUY": 0, "WATCH": 0, "AVOID": 0, "N/A": 0}
    all_buy_rows: list[dict] = []

    _total_rows = sum(len(v) for v in results.values())
    _progress = 0
    for universe_key, rows in results.items():
        for r in rows:
            _progress += 1
            if _progress % 20 == 0 or _progress == _total_rows:
                print(f"  Classifying signals: {_progress}/{_total_rows} ...", end="\r", flush=True)
            sig = classify_signal(r, args.rsi_min, args.rsi_max,
                                  args.adx_min, args.bearish)
            r["signal"] = sig
            totals[sig] = totals.get(sig, 0) + 1
            _save_signal(conn, run_id, r, sig)
            if sig == "BUY":
                all_buy_rows.append(r)

    # Update run totals
    conn.execute(
        "UPDATE signal_runs SET n_buy=?, n_watch=?, n_avoid=?, n_total=? WHERE run_id=?",
        (totals["BUY"], totals["WATCH"], totals.get("AVOID", 0),
         sum(totals.values()), run_id),
    )
    conn.commit()
    conn.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    total = sum(totals.values())
    print(f"\n  Results: {total} total  |  "
          f"🟢 {totals['BUY']} BUY  |  "
          f"👁️ {totals['WATCH']} WATCH  |  "
          f"🔴 {totals.get('AVOID', 0)} AVOID")

    # ── Print top BUY signals ─────────────────────────────────────────────────
    if all_buy_rows:
        buy_sorted = sorted(all_buy_rows, key=lambda r: -(r.get("ADX") or 0))
        print(f"\n  Top BUY signals (sorted by ADX):")
        print(f"  {'Ticker':<10} {'Name':<35} {'Close':>8} {'RSI':>6} {'ADX':>6} "
              f"{'RelVol':>7} {'Universe':<8}")
        print("  " + "-" * 85)
        for r in buy_sorted[:20]:
            sym    = r.get("tv_symbol", "")
            ticker = sym.split(":")[-1] if ":" in sym else sym
            name   = (r.get("description") or r.get("name") or ticker)[:33]
            close  = r.get("close")
            rsi    = r.get("RSI")
            adx    = r.get("ADX")
            rv     = r.get("relative_volume_10d_calc")
            uni    = r.get("_universe", "")
            print(f"  {ticker:<10} {name:<35} "
                  f"{_fmt_num(close):>8}  {_fmt_num(rsi, '.1f'):>6}  "
                  f"{_fmt_num(adx, '.1f'):>6}  {_fmt_num(rv, '.2f'):>6}x  {uni:<8}")
        if len(buy_sorted) > 20:
            print(f"  ... and {len(buy_sorted) - 20} more in the HTML report")

    # ── Append to fires & run pipeline ──────────────────────────────────────
    if getattr(args, "pipeline", True):
        n_appended = append_to_fires(all_buy_rows)
        print(f"\n  Appended {n_appended} BUY signals to fires_raw.json")
        _run_signal_pipeline(args)

    return results


def _run_signal_pipeline(args) -> None:
    """Invoke signal_stack.py to score all fires through the full L1-L8 pipeline."""
    import subprocess

    script = SIGNAL_STACK
    if not script.exists():
        # Try one level up (when called from inside tradingview/)
        script = Path(__file__).parent.parent / "signal_stack.py"
    if not script.exists():
        print("  [WARN] signal_stack.py not found — skipping pipeline run")
        return

    pipeline_out = script.parent.parent / "reports" / "signal_report_actionable_austsuper.html"
    skip_flags = []
    if getattr(args, "skip_moomoo", False):
        skip_flags.append("--no-moomoo")
    if getattr(args, "skip_india", False):
        skip_flags.append("--no-india")

    cmd = [
        sys.executable, str(script),
        "--n",         "200",
        "--threshold", "55",
        "--no-external",
        "--austsuper",   # filter to source='austsuper_screener' fires only + austsuper output naming
        *skip_flags,
    ]

    print("\n" + "=" * 70)
    print("  Scoring through signal_stack.py full pipeline (L1-L8)…")
    print("=" * 70)

    result = subprocess.run(cmd, cwd=str(script.parent))

    if result.returncode == 0:
        print(f"\n  Pipeline complete.")
        print(f"  Actionable report → {pipeline_out}")
    else:
        print(f"  [WARN] signal_stack.py exited with code {result.returncode}")


def main(args) -> None:
    # Run screener
    results = run_screener(args)

    # Build HTML report
    run_at_local = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = build_html(results, args, run_at_local)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"\n  📊 Report written → {out}")

    # Note: webbrowser.open() is intentionally absent — main.py handles report display
    # (Rule 9.3: dashboard/report scripts must NEVER call webbrowser.open() automatically)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AustralianSuper Member Direct — ASX 300 Signal Screener",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Signal thresholds
    parser.add_argument("--rsi-min", type=float, default=45.0,
                        help="Minimum RSI for BUY signal (default: 45)")
    parser.add_argument("--rsi-max", type=float, default=72.0,
                        help="Maximum RSI for BUY signal (default: 72)")
    parser.add_argument("--adx-min", type=float, default=20.0,
                        help="Minimum ADX for BUY signal (default: 20)")
    parser.add_argument("--bearish", action="store_true",
                        help="Bearish mode (inverse RSI filter, close < SMA200)")
    # Output
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="Output HTML report path")
    parser.add_argument("--db",  default=str(DB_PATH), type=Path,
                        help="SQLite database path")
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-open the HTML report in browser")
    # Pipeline (enabled by default)
    parser.add_argument("--pipeline", action="store_true", default=True,
                        help="Run full signal_stack.py pipeline after screener (default: on)")
    parser.add_argument("--no-pipeline", dest="pipeline", action="store_false",
                        help="Skip signal_stack.py pipeline run")
    parser.add_argument("--skip-moomoo", action="store_true",
                        help="Pass --skip-moomoo to signal_stack.py")
    parser.add_argument("--skip-india", action="store_true",
                        help="Pass --skip-india to signal_stack.py")
    # Misc
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-download even if universe was recently cached")

    args = parser.parse_args()
    main(args)
