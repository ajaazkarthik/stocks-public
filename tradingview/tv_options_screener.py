"""
TradingView Options Screener
============================
Reverse-engineered from TV Pro options chain page.

API endpoint: POST scanner.tradingview.com/options/scan2
Auth: sessionid cookie (no JWT needed)

Usage:
  python tradingview/tv_options_screener.py --symbol "NASDAQ:AAPL"
  python tradingview/tv_options_screener.py --symbol "NYSE:NEE" --type call --min-iv 0.30 --max-dte 45
  python tradingview/tv_options_screener.py --symbol "NASDAQ:NVDA" --out reports/nvda_options.html
  python tradingview/tv_options_screener.py --symbols "NASDAQ:AAPL,NYSE:NEE,NASDAQ:NVDA" --min-oi 100

Screener mode (scan multiple symbols, rank by criteria):
  python tradingview/tv_options_screener.py --screener --min-iv 0.40 --min-oi 500 --out reports/options_screener.html
"""

import sys
import os
import re
import json
import argparse
import datetime
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
TV   = Path(__file__).parent
ROOT = TV.parent
COOKIE_FILE = TV / "cookie"

# ── Confirmed valid columns for scanner.tradingview.com/options/scan2 ────────
# (verified by exhaustive probe 2026-05-09 — options/scan2 has a different
#  field set than the regular screener; ignore_unknown_fields=False used)
ALL_COLUMNS = [
    # Price
    "ask", "bid", "close", "open", "high", "low", "change", "theoPrice",
    # Volume
    "volume",
    # Greeks
    "delta", "gamma", "theta", "vega", "rho",
    # IV
    "iv", "bid_iv", "ask_iv",
    # Contract identity
    "expiration", "root", "strike", "option-type", "underlying_symbol",
    # Meta
    "pricescale", "currency",
]

# Default columns shown in the HTML report table
DEFAULT_COLUMNS = [
    "underlying_symbol", "root", "strike", "option-type", "expiration",
    "bid", "ask", "close", "change",
    "iv", "bid_iv", "ask_iv",
    "delta", "gamma", "theta", "vega", "rho",
    "volume", "theoPrice",
    "pricescale", "currency",
]


# ── Auth helper ───────────────────────────────────────────────────────────────
def _load_session() -> tuple[str, str]:
    """Load TV sessionid cookie."""
    if not COOKIE_FILE.exists():
        raise FileNotFoundError(f"Cookie file not found: {COOKIE_FILE}")
    text = COOKIE_FILE.read_text(encoding="utf-8")
    m1 = re.search(r"sessionid=([^;\s]+)", text)
    m2 = re.search(r"sessionid_sign=([^;\s]+)", text)
    if not m1:
        raise ValueError("sessionid not found in cookie file")
    return m1.group(1).strip(), (m2.group(1).strip() if m2 else "")


def _make_session() -> requests.Session:
    s = requests.Session()
    sid, ssign = _load_session()
    s.cookies.set("sessionid",      sid,   domain=".tradingview.com")
    s.cookies.set("sessionid_sign", ssign, domain=".tradingview.com")
    s.headers.update({
        "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
        "Referer":      "https://www.tradingview.com/",
        "Origin":       "https://www.tradingview.com",
        "Accept":       "application/json",
        # TV uses text/plain;charset=UTF-8 for scan2 (not application/json)
        "Content-Type": "text/plain;charset=UTF-8",
    })
    return s


# ── Core API functions ─────────────────────────────────────────────────────────

def fetch_option_chain(
    symbol: str,
    columns: Optional[list] = None,
    option_type: Optional[str] = None,       # "call" or "put"
    expirations: Optional[list] = None,      # list of int YYYYMMDD
    min_iv: Optional[float] = None,
    max_iv: Optional[float] = None,
    min_oi: Optional[int] = None,
    min_volume: Optional[int] = None,
    max_dte: Optional[int] = None,
    min_delta: Optional[float] = None,
    max_delta: Optional[float] = None,
    in_the_money: Optional[bool] = None,
    limit: int = 500,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """
    Fetch options for a single underlying symbol.
    Returns list of dicts with keys matching column names.
    """
    s = session or _make_session()
    cols = columns or DEFAULT_COLUMNS

    # Build filter2 operands
    operands = [
        {"expression": {"left": "type", "operation": "equal", "right": "option"}}
    ]

    if option_type:
        operands.append({"expression": {"left": "option-type", "operation": "equal", "right": option_type.lower()}})

    if expirations:
        exp_ops = []
        root = symbol.split(":")[-1] if ":" in symbol else symbol
        for exp in expirations:
            exp_ops.append({
                "operation": {
                    "operator": "and",
                    "operands": [
                        {"expression": {"left": "expiration", "operation": "equal", "right": exp}},
                        {"expression": {"left": "root", "operation": "equal", "right": root}},
                    ]
                }
            })
        if len(exp_ops) == 1:
            operands.append(exp_ops[0])
        else:
            operands.append({"operation": {"operator": "or", "operands": exp_ops}})

    if min_iv is not None:
        operands.append({"expression": {"left": "iv", "operation": "greater", "right": min_iv}})

    if max_iv is not None:
        operands.append({"expression": {"left": "iv", "operation": "less", "right": max_iv}})

    if min_oi is not None:
        # open_interest is not a valid field in scan2 — filter post-fetch instead
        pass

    if min_volume is not None:
        operands.append({"expression": {"left": "volume", "operation": "greater", "right": min_volume}})

    if max_dte is not None:
        # dte is not a valid field in scan2 — use expiration date math instead
        today = datetime.date.today()
        cutoff = today + datetime.timedelta(days=max_dte)
        cutoff_int = int(cutoff.strftime("%Y%m%d"))
        operands.append({"expression": {"left": "expiration", "operation": "less", "right": cutoff_int}})

    if min_delta is not None:
        operands.append({"expression": {"left": "delta", "operation": "greater", "right": min_delta}})

    if max_delta is not None:
        operands.append({"expression": {"left": "delta", "operation": "less", "right": max_delta}})

    # in_the_money not available in scan2 — skip

    filter2 = {"operator": "and", "operands": operands}

    payload = {
        "columns":              cols,
        "ignore_unknown_fields": False,
        "index_filters":        [{"name": "underlying_symbol", "values": [symbol]}],
        "filter2":              filter2,
        "range":                [0, limit],
    }

    r = s.post(
        "https://scanner.tradingview.com/options/scan2?label-product=options-builder",
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    fields = data.get("fields", [])
    rows = []
    for sym_row in data.get("symbols", []):
        row = {"symbol": sym_row["s"]}
        for i, col in enumerate(fields):
            row[col] = sym_row["f"][i] if i < len(sym_row["f"]) else None
        # Compute DTE from expiration date (not available as a field)
        exp = row.get("expiration")
        if exp:
            try:
                exp_date = datetime.date(
                    int(str(int(exp))[:4]),
                    int(str(int(exp))[4:6]),
                    int(str(int(exp))[6:8]),
                )
                row["dte"] = (exp_date - datetime.date.today()).days
            except Exception:
                row["dte"] = None
        rows.append(row)

    # Post-filter: open_interest is never returned by scan2.
    # Use volume as a liquidity proxy (volume > 0 = traded today during market hours).
    if min_oi is not None:
        rows = [r for r in rows if (r.get("volume") or 0) >= min_oi]

    return rows


def fetch_available_expirations(symbol: str, session: Optional[requests.Session] = None) -> list[int]:
    """
    Fetch all available expiration dates for a symbol.
    Returns list of YYYYMMDD integers.
    """
    s = session or _make_session()
    payload = {
        "columns":               ["expiration", "root"],
        "ignore_unknown_fields": False,
        "index_filters":         [{"name": "underlying_symbol", "values": [symbol]}],
        "filter2": {
            "operator": "and",
            "operands": [{"expression": {"left": "type", "operation": "equal", "right": "option"}}]
        },
        "range": [0, 2000],
    }
    r = s.post(
        "https://scanner.tradingview.com/options/scan2?label-product=options-builder",
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    exps = set()
    fields = data.get("fields", [])
    if "expiration" in fields:
        idx = fields.index("expiration")
        for row in data.get("symbols", []):
            val = row["f"][idx] if idx < len(row["f"]) else None
            if val:
                exps.add(int(val))
    return sorted(exps)


def fetch_volatility_chart(symbol: str, root: str, expiration: int, xaxis: str = "strikes",
                           session: Optional[requests.Session] = None) -> dict:
    """
    Fetch volatility smile chart data for a symbol/expiry.
    expiration: int in YYYYMMDD format
    xaxis: 'strikes' or 'deltas'
    Returns dict with 'x-axis' and 'plots' keys.
    """
    s = session or _make_session()
    s2 = requests.Session()
    s2.cookies.update(s.cookies)
    s2.headers.update({k: v for k, v in s.headers.items() if k != "Content-Type"})
    
    # Format: {SYMBOL};{ROOT};{YYYYMMDD}
    url = f"https://options-charting.tradingview.com/v1/volatility-chart/{symbol};{root};{expiration}?xaxis={xaxis}&label-product=details_widget_volatility_chart"
    r = s2.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_iv_history(symbol: str, session: Optional[requests.Session] = None) -> dict:
    """
    Fetch implied volatility over time for a symbol.
    Returns dict with 'symbol', 'real-ivs', 'standard-ivs'.
    """
    import urllib.parse
    s = session or _make_session()
    s2 = requests.Session()
    s2.cookies.update(s.cookies)
    s2.headers.update({k: v for k, v in s.headers.items() if k != "Content-Type"})
    
    url = f"https://options-charting.tradingview.com/v1/in-time-iv/{urllib.parse.quote(symbol)}?label-product=details_widget_in_time_iv_chart"
    r = s2.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_options_strategies(session: Optional[requests.Session] = None) -> dict:
    """
    Fetch available options strategies from TV.
    Returns dict with 'filter', 'underlyings', 'strategies', 'limits'.
    """
    s = session or _make_session()
    s2 = requests.Session()
    s2.cookies.update(s.cookies)
    s2.headers.update({k: v for k, v in s.headers.items() if k != "Content-Type"})
    
    r = s2.get("https://options-storage.tradingview.com/v1/strategies?", timeout=15)
    r.raise_for_status()
    return r.json()


def search_symbols_with_options(query: str = "", country: str = "US",
                                session: Optional[requests.Session] = None) -> list[dict]:
    """
    Search for symbols that have options available.
    """
    import urllib.parse
    s = session or _make_session()
    s2 = requests.Session()
    s2.cookies.update(s.cookies)
    s2.headers.update({k: v for k, v in s.headers.items() if k != "Content-Type"})
    
    params = {"only_has_options": "true", "lang": "en", "domain": "production",
              "sort_by_country": country}
    if query:
        params["text"] = query
    r = s2.get("https://symbol-search.tradingview.com/symbol_search/v3/", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("symbols", [])


# ── Screener: multi-symbol scan ────────────────────────────────────────────────

SCREENER_SYMBOLS = [
    "NASDAQ:AAPL", "NASDAQ:NVDA", "NASDAQ:MSFT", "NASDAQ:AMZN", "NASDAQ:TSLA",
    "NASDAQ:META", "NASDAQ:GOOGL", "NYSE:SPY", "NYSE:QQQ", "NYSE:SPX",
    "NYSE:NEE", "NYSE:JPM", "NYSE:GS", "NYSE:BAC",
]


def run_screener(
    symbols: Optional[list] = None,
    option_type: Optional[str] = None,
    min_iv: Optional[float] = None,
    max_dte: Optional[int] = None,
    min_oi: Optional[int] = None,
    min_volume: Optional[int] = None,
    min_delta: Optional[float] = None,
    max_delta: Optional[float] = None,
    limit_per_symbol: int = 50,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """Scan multiple symbols and return filtered options."""
    s = session or _make_session()
    symbols = symbols or SCREENER_SYMBOLS
    all_rows = []

    for sym in symbols:
        try:
            rows = fetch_option_chain(
                sym,
                option_type=option_type,
                min_iv=min_iv,
                max_dte=max_dte,
                min_oi=min_oi,
                min_volume=min_volume,
                min_delta=min_delta,
                max_delta=max_delta,
                limit=limit_per_symbol,
                session=s,
            )
            print(f"  {sym}: {len(rows)} contracts")
            all_rows.extend(rows)
        except Exception as e:
            print(f"  {sym}: ERROR — {e}")

    # Sort by volume descending (open_interest is not available from scan2)
    all_rows.sort(key=lambda r: (r.get("volume") or 0), reverse=True)
    return all_rows


# ── HTML Report ────────────────────────────────────────────────────────────────

def fmt(val, col: str) -> str:
    """Format a value for display."""
    if val is None:
        return ""
    if col in ("delta", "gamma", "theta", "vega", "rho"):
        return f"{val:.4f}"
    if col in ("iv", "bid_iv", "ask_iv"):
        return f"{val:.2%}"
    if col in ("strike", "bid", "ask", "close", "open", "high", "low", "theoPrice"):
        return f"${val:.2f}"
    if col in ("volume",):
        return f"{int(val):,}" if val else ""
    if col == "expiration":
        s2 = str(int(val))
        return f"{s2[:4]}-{s2[4:6]}-{s2[6:]}" if len(s2) == 8 else str(val)
    if col == "dte":
        return f"{int(val)}d" if val is not None else ""
    if col == "change":
        return f"{val:+.2f}"
    return str(val)


def _col_label(col: str) -> str:
    labels = {
        "option-type": "Type", "underlying_symbol": "Underlying",
        "expiration": "Expiry", "theoPrice": "Theo",
        "bid_iv": "Bid IV", "ask_iv": "Ask IV", "dte": "DTE",
    }
    return labels.get(col, col.replace("_", " ").title())


def build_html(rows: list[dict], symbol: str, args) -> str:
    show_cols = [c for c in DEFAULT_COLUMNS + ["dte"] if c in (rows[0] if rows else {})]
    if not show_cols:
        show_cols = list(rows[0].keys()) if rows else DEFAULT_COLUMNS

    # Separate calls and puts
    calls = [r for r in rows if r.get("option-type") == "call"]
    puts  = [r for r in rows if r.get("option-type") == "put"]

    now  = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    nrow = len(rows)

    def table(data, title, badge_class):
        if not data:
            return f"<div class='empty'>{title}: no data</div>"
        headers = "".join(f"<th>{_col_label(c)}</th>" for c in show_cols if c not in ("pricescale", "currency"))
        rows_html = ""
        for row in data:
            itm = row.get("in_the_money")
            cls = "itm" if itm else ""
            cells = ""
            for c in show_cols:
                if c in ("pricescale", "currency"):
                    continue
                v = row.get(c)
                cells += f"<td>{fmt(v, c)}</td>"
            rows_html += f"<tr class='{cls}'>{cells}</tr>"
        return f"""
        <h2>{title} <span class='badge {badge_class}'>{len(data)}</span></h2>
        <div class='table-wrap'>
        <table><thead><tr>{headers}</tr></thead><tbody>{rows_html}</tbody></table>
        </div>"""

    filter_summary = []
    if hasattr(args, "type") and args.type:
        filter_summary.append(f"Type: {args.type}")
    if hasattr(args, "min_iv") and args.min_iv:
        filter_summary.append(f"Min IV: {args.min_iv:.0%}")
    if hasattr(args, "max_dte") and args.max_dte:
        filter_summary.append(f"Max DTE: {args.max_dte}d")
    if hasattr(args, "min_oi") and args.min_oi:
        filter_summary.append(f"Min OI: {args.min_oi:,}")

    calls_html = table(calls, "Calls", "badge-call")
    puts_html  = table(puts,  "Puts",  "badge-put")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TV Options Screener — {symbol}</title>
<style>
  :root{{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;
         --call:#10b981;--put:#ef4444;--itm:#1e3a2f;--head:#6366f1}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;font-size:13px}}
  .header{{background:linear-gradient(135deg,#1a1d27,#0f1117);padding:24px 32px;border-bottom:1px solid var(--border)}}
  .header h1{{font-size:20px;font-weight:700;color:#e2e8f0}}
  .header .sub{{color:#94a3b8;font-size:13px;margin-top:4px}}
  .meta{{display:flex;gap:16px;margin-top:12px;flex-wrap:wrap}}
  .meta span{{background:var(--card);padding:4px 12px;border-radius:6px;font-size:12px;color:#94a3b8}}
  .meta span b{{color:#e2e8f0}}
  .content{{padding:24px 32px}}
  h2{{font-size:15px;font-weight:600;color:#e2e8f0;margin:24px 0 10px}}
  .badge{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;margin-left:8px}}
  .badge-call{{background:#064e3b;color:#10b981}}
  .badge-put{{background:#450a0a;color:#ef4444}}
  .table-wrap{{overflow-x:auto;border-radius:8px;border:1px solid var(--border)}}
  table{{width:100%;border-collapse:collapse;background:var(--card)}}
  thead{{background:#1e2130}}
  th{{padding:8px 10px;text-align:right;white-space:nowrap;color:#94a3b8;font-weight:600;font-size:11px;border-bottom:1px solid var(--border)}}
  th:first-child{{text-align:left}}
  td{{padding:6px 10px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}}
  td:first-child{{text-align:left}}
  tr:last-child td{{border-bottom:none}}
  tr.itm{{background:var(--itm)}}
  tr:hover{{background:#1e2130}}
  .empty{{color:#64748b;padding:16px;font-style:italic}}
</style>
</head>
<body>
<div class="header">
  <h1>TradingView Options Screener</h1>
  <div class="sub">Symbol: <b>{symbol}</b> &bull; {nrow} contracts &bull; Generated: {now}</div>
  <div class="meta">
    {''.join(f'<span><b>{f}</b></span>' for f in filter_summary) if filter_summary else '<span>No filters applied</span>'}
  </div>
</div>
<div class="content">
  {calls_html}
  {puts_html}
</div>
</body>
</html>"""


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(args) -> None:
    session = _make_session()

    if args.screener or (hasattr(args, "symbols") and args.symbols and "," in (args.symbols or "")):
        print("Running multi-symbol screener...")
        syms = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        rows = run_screener(
            symbols=syms,
            option_type=args.type,
            min_iv=args.min_iv,
            max_dte=args.max_dte,
            min_oi=args.min_oi,
            min_volume=args.min_volume,
            min_delta=args.min_delta,
            max_delta=args.max_delta,
            limit_per_symbol=100,
            session=session,
        )
        sym_label = "Multi-Symbol Screener"
    else:
        symbol = (args.symbol or "").strip()
        if not symbol:
            print("ERROR: Provide --symbol or --screener")
            sys.exit(1)
        print(f"Fetching options chain: {symbol}")

        if args.expiration:
            exps = [int(e.strip().replace("-", "")) for e in args.expiration.split(",")]
        else:
            exps = None

        rows = fetch_option_chain(
            symbol=symbol,
            option_type=args.type,
            expirations=exps,
            min_iv=args.min_iv,
            max_iv=args.max_iv,
            min_oi=args.min_oi,
            min_volume=args.min_volume,
            max_dte=args.max_dte,
            min_delta=args.min_delta,
            max_delta=args.max_delta,
            limit=args.limit,
            session=session,
        )
        sym_label = symbol

    print(f"Total contracts: {len(rows)}")

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("No contracts found.")
        return

    html = build_html(rows, sym_label, args)
    out  = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Report saved: {out}")

    if not getattr(args, "no_open", False):
        import webbrowser
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradingView Options Screener")

    # Symbol targeting
    parser.add_argument("--symbol",    help="Single underlying, e.g. NASDAQ:AAPL")
    parser.add_argument("--symbols",   help="Comma-separated list for screener mode")
    parser.add_argument("--screener",  action="store_true", help="Scan default symbol universe")

    # Filters
    parser.add_argument("--type",       choices=["call", "put"], help="Option type filter")
    parser.add_argument("--expiration", help="Expiry filter YYYY-MM-DD, comma-separated")
    parser.add_argument("--min-iv",     type=float, help="Min implied volatility (0.0–1.0)")
    parser.add_argument("--max-iv",     type=float, help="Max implied volatility")
    parser.add_argument("--min-oi",     type=int,   help="Min open interest")
    parser.add_argument("--min-volume", type=int,   help="Min daily volume")
    parser.add_argument("--max-dte",    type=int,   help="Max days to expiration")
    parser.add_argument("--min-delta",  type=float, help="Min delta (-1 to 1)")
    parser.add_argument("--max-delta",  type=float, help="Max delta")

    # Output
    parser.add_argument("--limit",    type=int, default=500, help="Max contracts per symbol")
    parser.add_argument("--out",      default=str(ROOT / "reports" / "tv_options_screener.html"),
                        help="HTML output path")
    parser.add_argument("--json",     action="store_true", help="Print JSON to stdout")
    parser.add_argument("--no-open",  action="store_true", help="Don't open browser")

    args = parser.parse_args()
    main(args)
