"""
F&O Chain Screener — Options-first standalone scanner.
=====================================================
Scans option chains across US (yfinance) and India (NSE API) markets,
scores each contract 0-100 using S1-S5 scoring, reports contracts ≥ threshold.

Scoring:
  S1 — IV Rank      (0-30 pts): IV percentile vs 52-week range
  S2 — Directional  (0-25 pts): Underlying composite alignment with option type
  S3 — Liquidity    (0-20 pts): Open interest + bid-ask spread
  S4 — Time Value   (0-15 pts): DTE sweet spot (15-45 days optimal)
  S5 — Greek Profile (0-10 pts): Delta zone + theta efficiency

Usage:
  python fo_chain_screener.py --market us --min-score 70
  python fo_chain_screener.py --market india --out reports/fo_chain_report.html
  python fo_chain_screener.py --market all --symbols AAPL,MSFT,NIFTY
"""
from __future__ import annotations

import sys
import json
import math
import argparse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Paths ────────────────────────────────────────────────────────────────────
TV = Path(__file__).parent.parent          # tradingview/
OUT_DEFAULT = TV.parent / "reports" / "fo_chain_report.html"

# ─── yfinance import ─────────────────────────────────────────────────────────
try:
    import yfinance as yf  # type: ignore
    _YF_OK = True
except ImportError:
    _YF_OK = False

# ─── Risk-free rate for BS delta ──────────────────────────────────────────────
_RISK_FREE_RATE = 0.045

# ─── TV scanner helpers ──────────────────────────────────────────────────────

_TV_SCANNER_URL = "https://scanner.tradingview.com/{market}/scan"
_TV_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}


def _tv_scan_top_symbols(market: str = "america", n: int = 100) -> list[str]:
    """Fetch top N US stocks by market cap from TV scanner."""
    payload = {
        "columns": ["name", "market_cap_basic"],
        "filter": [
            {"left": "market_cap_basic", "operation": "greater", "right": 1e9},
            {"left": "is_primary", "operation": "equal", "right": True},
            {"left": "type", "operation": "equal", "right": "stock"},
        ],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, n],
        "options": {"lang": "en"},
    }
    url = _TV_SCANNER_URL.format(market=market)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=_TV_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        symbols = []
        for item in data.get("data", []):
            s = item.get("s", "")
            # Extract ticker from "NASDAQ:AAPL" → "AAPL"
            ticker = s.split(":")[-1] if ":" in s else s
            if ticker:
                symbols.append(ticker)
        return symbols
    except Exception as exc:
        print(f"  [warn] TV scanner failed: {exc}")
        return []


def _get_us_symbols(n: int = 100) -> list[str]:
    """Get top N US optionable symbols."""
    symbols = _tv_scan_top_symbols("america", n)
    if not symbols:
        # Fallback: hardcoded top 20
        symbols = [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B",
            "JPM", "V", "UNH", "MA", "JNJ", "HD", "PG", "XOM", "AVGO", "COST",
            "MRK", "ABBV",
        ]
    return symbols


# ─── NSE India helpers ────────────────────────────────────────────────────────

_NSE_BASE = "https://www.nseindia.com"
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def _nse_fetch_chain(symbol: str = "NIFTY", is_index: bool = True) -> list[dict]:
    """Fetch option chain from NSE India API."""
    endpoint = "option-chain-indices" if is_index else "option-chain-equities"
    url = f"{_NSE_BASE}/api/{endpoint}?symbol={symbol}"

    # NSE requires cookies from homepage visit — use CookieJar to persist them
    import http.cookiejar
    _cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))
    opener.addheaders = list(_NSE_HEADERS.items())
    try:
        # Visit homepage first to get cookies (now persisted via CookieJar)
        opener.open(_NSE_BASE, timeout=10)
        resp = opener.open(url, timeout=15)
        data = json.loads(resp.read())
    except Exception as exc:
        print(f"  [warn] NSE chain fetch failed for {symbol}: {exc}")
        return []

    contracts = []
    records = data.get("records", {}).get("data", [])
    underlying_price = data.get("records", {}).get("underlyingValue", 0)

    for rec in records:
        for opt_type, key in [("call", "CE"), ("put", "PE")]:
            opt = rec.get(key)
            if not opt:
                continue
            expiry_str = opt.get("expiryDate", "")
            try:
                expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y")
                dte = (expiry_dt - datetime.now()).days
            except Exception:
                dte = 0

            iv_raw = opt.get("impliedVolatility", 0)
            contracts.append({
                "symbol": symbol,
                "underlying": symbol,
                "underlying_price": float(underlying_price),
                "option_type": opt_type,
                "strike": float(opt.get("strikePrice", 0)),
                "expiry": expiry_str,
                "dte": max(0, dte),
                "bid": float(opt.get("bidprice", 0)),
                "ask": float(opt.get("askPrice", 0)),
                "last": float(opt.get("lastPrice", 0)),
                "volume": int(opt.get("totalTradedVolume", 0)),
                "open_interest": int(opt.get("openInterest", 0)),
                "iv": float(iv_raw) / 100 if iv_raw > 1 else float(iv_raw),
                "change_oi": int(opt.get("changeinOpenInterest", 0)),
                "market": "india",
                "broker_note": "NSE API (direct)",
            })

    return contracts


def _get_india_symbols() -> list[tuple[str, bool]]:
    """Return India index/equity symbols for chain scanning."""
    return [
        ("NIFTY", True),
        ("BANKNIFTY", True),
    ]


# ─── yfinance chain fetcher ──────────────────────────────────────────────────

def _fetch_chain_yf(symbol: str, n_expiries: int = 2) -> list[dict]:
    """Fetch option chain for a US symbol via yfinance."""
    if not _YF_OK:
        return []
    try:
        ticker = yf.Ticker(symbol)
        expiry_dates = ticker.options
        if not expiry_dates:
            return []

        spot = None
        try:
            hist = ticker.history(period="5d")
            if hist is not None and not hist.empty:
                spot = float(hist["Close"].iloc[-1])
        except Exception:
            pass

        contracts = []
        for expiry in expiry_dates[:n_expiries]:
            try:
                chain = ticker.option_chain(expiry)
            except Exception:
                continue

            expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
            dte = max(0, (expiry_dt - datetime.now()).days)

            for opt_type, df in [("call", chain.calls), ("put", chain.puts)]:
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    iv = float(row.get("impliedVolatility", 0) or 0)
                    strike = float(row.get("strike", 0) or 0)
                    bid = float(row.get("bid", 0) or 0)
                    ask = float(row.get("ask", 0) or 0)
                    oi = int(row.get("openInterest", 0) or 0)
                    vol = int(row.get("volume", 0) or 0)
                    last = float(row.get("lastPrice", row.get("lastTradePrice", 0)) or 0)

                    # Compute delta via Black-Scholes if spot available
                    delta = _bs_call_delta(spot, strike, dte, iv) if spot and spot > 0 else 0.5
                    if opt_type == "put":
                        delta = delta - 1.0  # put delta = call delta - 1

                    # Compute theta estimate
                    mid = (bid + ask) / 2 if (bid + ask) > 0 else last
                    theta = -mid / max(dte, 1) * 0.05 if mid > 0 else 0

                    contracts.append({
                        "symbol": symbol,
                        "underlying": symbol,
                        "underlying_price": spot or 0,
                        "option_type": opt_type,
                        "strike": strike,
                        "expiry": expiry,
                        "dte": dte,
                        "bid": bid,
                        "ask": ask,
                        "last": last,
                        "volume": vol,
                        "open_interest": oi,
                        "iv": iv,
                        "delta": round(delta, 4),
                        "theta": round(theta, 4),
                        "market": "us",
                        "broker_note": "IBKR (full options)",
                    })
        return contracts
    except Exception as exc:
        print(f"  [warn] yfinance chain failed for {symbol}: {exc}")
        return []


def _bs_call_delta(spot: float, strike: float, dte: int, sigma: float) -> float:
    """Black-Scholes call delta."""
    if dte <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.50
    T = dte / 365.0
    d1 = (math.log(spot / strike) + (_RISK_FREE_RATE + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    return 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))


# ─── Scoring Engine ──────────────────────────────────────────────────────────

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def score_contract(contract: dict, underlying_score: Optional[float] = None) -> dict:
    """
    Score an option contract on 0-100 scale.

    Args:
        contract: Contract dict with strike, dte, iv, bid, ask, open_interest, delta, etc.
        underlying_score: Optional composite score (0-100) of underlying stock.

    Returns:
        Same dict with added: total_score, s1-s5 scores, s1-s5 details.
    """
    iv = _safe_float(contract.get("iv"))
    dte = int(_safe_float(contract.get("dte")))
    oi = int(_safe_float(contract.get("open_interest")))
    bid = _safe_float(contract.get("bid"))
    ask = _safe_float(contract.get("ask"))
    delta = _safe_float(contract.get("delta"), 0.5)
    last = _safe_float(contract.get("last"))
    opt_type = contract.get("option_type", "call")

    # ── S1: IV Rank (0-30 pts) ────────────────────────────────────────────
    # Without 52-week IV history, use raw IV as proxy:
    # IV > 0.40 (40%) = high IV environment = good for selling premium
    # IV 0.20-0.40 = moderate
    # IV < 0.20 = low IV = good for buying options
    if iv > 0.50:
        s1 = 30.0
    elif iv > 0.40:
        s1 = 25.0
    elif iv > 0.30:
        s1 = 20.0
    elif iv > 0.20:
        s1 = 12.0
    elif iv > 0.10:
        s1 = 6.0
    elif iv > 0:
        s1 = 4.0  # very low IV (<10%) — minimal premium opportunity; must exceed neutral floor
    else:
        # iv == 0 means API did not return IV data — use conservative neutral floor
        # Note: any real IV (>0%) always beats this floor (4.0 > 3.0)
        s1 = 3.0
        print(
            f"  [warn] iv=0 for {contract.get('symbol', '?')} "
            f"{contract.get('option_type', '?')} K={contract.get('strike', '?')}"
            " — possible API/auth failure; using neutral S1=3. Refresh TV cookie if India."
        )

    # ── S2: Directional Signal (0-25 pts) ─────────────────────────────────
    if underlying_score is not None:
        if opt_type == "call" and underlying_score >= 70:
            s2 = 25.0
        elif opt_type == "put" and underlying_score <= 30:
            s2 = 25.0
        elif opt_type == "call" and underlying_score >= 55:
            s2 = 18.0
        elif opt_type == "put" and underlying_score <= 45:
            s2 = 18.0
        else:
            s2 = 10.0
    else:
        s2 = 12.5  # neutral when no underlying score

    # ── S3: Liquidity (0-20 pts) ──────────────────────────────────────────
    s3 = 0.0
    if oi > 500:
        s3 += 10.0
    elif oi > 100:
        s3 += 7.0
    elif oi > 10:
        s3 += 3.0

    if bid > 0 and ask > 0:
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        if spread_pct < 2.0:
            s3 += 10.0
        elif spread_pct < 5.0:
            s3 += 7.0
        elif spread_pct < 10.0:
            s3 += 3.0
    elif oi > 0:
        s3 += 2.0  # some liquidity exists

    # ── S4: Time Value (0-15 pts) ─────────────────────────────────────────
    if 15 <= dte <= 45:
        s4 = 15.0
    elif 7 <= dte < 15:
        s4 = 10.0
    elif 46 <= dte <= 90:
        s4 = 8.0
    elif 91 <= dte <= 180:
        s4 = 3.0
    elif 1 <= dte < 7:
        s4 = 5.0  # very short-dated
    else:
        s4 = 0.0

    # ── S5: Greek Profile (0-10 pts) ──────────────────────────────────────
    s5 = 0.0
    abs_delta = abs(delta)
    if 0.25 <= abs_delta <= 0.55:
        s5 += 5.0  # optimal premium zone
    elif 0.15 <= abs_delta <= 0.70:
        s5 += 3.0

    # Theta efficiency: daily theta / premium < 2%
    mid = (bid + ask) / 2 if (bid + ask) > 0 else last
    theta = _safe_float(contract.get("theta"))
    if mid > 0 and theta != 0:
        theta_pct = abs(theta) / mid * 100
        if theta_pct < 2.0:
            s5 += 5.0
        elif theta_pct < 4.0:
            s5 += 3.0

    total = round(s1 + s2 + s3 + s4 + s5, 1)

    contract["total_score"] = total
    contract["s1_iv_rank"] = round(s1, 1)
    contract["s2_direction"] = round(s2, 1)
    contract["s3_liquidity"] = round(s3, 1)
    contract["s4_time"] = round(s4, 1)
    contract["s5_greeks"] = round(s5, 1)
    return contract


# ─── HTML Report ──────────────────────────────────────────────────────────────

def build_html(contracts: list[dict], min_score: int = 55) -> str:
    """Build HTML report for scored option contracts."""
    scored = [c for c in contracts if c.get("total_score", 0) >= min_score]
    scored.sort(key=lambda c: c.get("total_score", 0), reverse=True)

    if not scored:
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>F&O Chain Screener</title></head><body>"
            f"<h2>No contracts scored above {min_score}</h2>"
            "</body></html>"
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = ""
    for c in scored:
        score = c.get("total_score", 0)
        opt_type = c.get("option_type", "?").upper()
        type_color = "#4ade80" if opt_type == "CALL" else "#f87171"

        badge = (
            '<span style="background:#16a34a;color:#fff;padding:2px 6px;'
            'border-radius:4px;font-size:0.8rem">HIGH</span>'
            if score >= 80 else
            '<span style="background:#f59e0b;color:#000;padding:2px 6px;'
            'border-radius:4px;font-size:0.8rem">GOOD</span>'
            if score >= 70 else
            '<span style="background:#525252;color:#e5e5e5;padding:2px 6px;'
            'border-radius:4px;font-size:0.8rem">OK</span>'
        )

        rows += f"""<tr>
  <td><b>{c.get('symbol','?')}</b></td>
  <td style="color:{type_color}">{opt_type}</td>
  <td class="num">{c.get('strike',0):.2f}</td>
  <td>{c.get('expiry','?')}</td>
  <td class="num">{c.get('dte',0)}</td>
  <td class="num" style="font-weight:700">{score:.0f}</td>
  <td class="num">{c.get('s1_iv_rank',0):.0f}</td>
  <td class="num">{c.get('s2_direction',0):.0f}</td>
  <td class="num">{c.get('s3_liquidity',0):.0f}</td>
  <td class="num">{c.get('s4_time',0):.0f}</td>
  <td class="num">{c.get('s5_greeks',0):.0f}</td>
  <td class="num">{c.get('iv',0)*100:.1f}%</td>
  <td class="num">{c.get('open_interest',0):,}</td>
  <td class="num">{c.get('bid',0):.2f}</td>
  <td class="num">{c.get('ask',0):.2f}</td>
  <td>{badge}</td>
  <td style="font-size:0.8rem;color:#94a3b8">{c.get('broker_note','')}</td>
</tr>\n"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F&O Chain Screener Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 16px; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .meta {{ font-size: 0.85rem; color: #94a3b8; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ color: #94a3b8; font-weight: 600; position: sticky; top: 0;
       background: #0f172a; font-size: 0.8rem; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr:hover {{ background: #1e293b !important; }}
  .legend {{ margin-top: 16px; font-size: 0.8rem; color: #94a3b8; }}
  .legend span {{ display: inline-block; margin-right: 16px; }}
</style>
</head>
<body>
<h1>F&O Chain Screener Report</h1>
<p class="meta">Generated {now} | {len(scored)} contracts above {min_score} | S1 IV (30) + S2 Dir (25) + S3 Liq (20) + S4 Time (15) + S5 Greeks (10) = 100</p>

<table>
<thead>
<tr>
  <th>Symbol</th><th>Type</th><th class="num">Strike</th><th>Expiry</th>
  <th class="num">DTE</th><th class="num">Score</th>
  <th class="num">S1</th><th class="num">S2</th><th class="num">S3</th>
  <th class="num">S4</th><th class="num">S5</th>
  <th class="num">IV%</th><th class="num">OI</th>
  <th class="num">Bid</th><th class="num">Ask</th>
  <th>Grade</th><th>Broker</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>

<div class="legend">
  <span>S1 = IV Rank (premium environment)</span>
  <span>S2 = Directional (underlying alignment)</span>
  <span>S3 = Liquidity (OI + spread)</span>
  <span>S4 = Time Value (DTE sweet spot)</span>
  <span>S5 = Greek Profile (delta + theta efficiency)</span>
</div>
</body>
</html>"""


# ─── Main orchestrator ────────────────────────────────────────────────────────

def scan_us(symbols: Optional[list[str]] = None, n_top: int = 50,
            n_expiries: int = 2) -> list[dict]:
    """Scan US option chains and return scored contracts."""
    if not _YF_OK:
        print("  [skip] yfinance not available — US scan disabled")
        return []

    syms = symbols or _get_us_symbols(n_top)
    print(f"  Scanning {len(syms)} US symbols (top by market cap)...")

    all_contracts = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_chain_yf, s, n_expiries): s for s in syms}
        done = 0
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            try:
                contracts = fut.result()
                for c in contracts:
                    score_contract(c)
                all_contracts.extend(contracts)
            except Exception as exc:
                print(f"  [warn] {sym}: {exc}")
            if done % 10 == 0:
                print(f"    ... {done}/{len(syms)} symbols scanned")

    print(f"  US scan complete: {len(all_contracts)} contracts from {len(syms)} symbols")
    return all_contracts


def scan_india() -> list[dict]:
    """Scan India NSE option chains and return scored contracts."""
    india_symbols = _get_india_symbols()
    all_contracts = []

    for symbol, is_index in india_symbols:
        print(f"  Fetching NSE chain: {symbol}...")
        contracts = _nse_fetch_chain(symbol, is_index)
        for c in contracts:
            score_contract(c)
        all_contracts.extend(contracts)

    print(f"  India scan complete: {len(all_contracts)} contracts")
    return all_contracts


def main(args: argparse.Namespace) -> None:
    all_contracts: list[dict] = []

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.market in ("us", "all"):
        us_syms = symbols if symbols and args.market == "us" else None
        all_contracts.extend(scan_us(us_syms, args.n_top, args.n_expiries))

    if args.market in ("india", "all"):
        all_contracts.extend(scan_india())

    # Filter and sort
    scored = [c for c in all_contracts if c.get("total_score", 0) >= args.min_score]
    scored.sort(key=lambda c: c.get("total_score", 0), reverse=True)

    print(f"\n  Total: {len(all_contracts)} contracts scanned, "
          f"{len(scored)} above {args.min_score} threshold")

    # Build and write report
    html = build_html(all_contracts, args.min_score)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Archive existing report before overwriting — SPLIT into 2 separate try blocks
    # Block 1: import only — so _abw() failure in Block 2 cannot null save_fo_chain_snapshot
    _abw = _arc = save_fo_chain_snapshot = None  # type: ignore
    try:
        import sys as _sys
        _rdb_p = str(Path(__file__).parent.parent.parent / "reports")
        if _rdb_p not in _sys.path:
            _sys.path.insert(0, _rdb_p)
        from report_db import archive_before_write as _abw, save_fo_chain_snapshot, archive_report as _arc
    except Exception as _e:
        print(f"  [warn] report_db import failed — snapshot/archive disabled: {_e}")
    # Block 2: call archive_before_write (failure must NOT null save_fo_chain_snapshot/_arc)
    try:
        if _abw is not None:
            _abw(out)
    except Exception as _e:
        print(f"  [warn] archive_before_write failed: {_e}")

    out.write_text(html, encoding="utf-8")
    print(f"  Report written to {out}")

    # Persist to report_analytics.db
    try:
        if save_fo_chain_snapshot is not None:
            save_fo_chain_snapshot(all_contracts, scored, args.min_score, args.market)
            print(f"  [report_db] persisted {len(all_contracts)} contracts to report_analytics.db")
        if _arc is not None:
            _arc(out)
    except Exception as _e:
        print(f"  [report_db] fo_chain snapshot skipped: {_e}")

    # Console summary
    if scored:
        print(f"\n  Top 10 contracts:")
        for c in scored[:10]:
            print(
                f"    {c['symbol']:8s} {c['option_type']:4s} "
                f"K={c['strike']:<10.2f} DTE={c['dte']:3d}  "
                f"Score={c['total_score']:5.1f}  IV={c['iv']*100:5.1f}%  "
                f"OI={c['open_interest']:>8,}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="F&O Chain Screener — options-first scanner with S1-S5 scoring"
    )
    parser.add_argument("--market", choices=["us", "india", "all"], default="us",
                        help="Market to scan (default: us)")
    parser.add_argument("--min-score", type=int, default=55,
                        help="Minimum contract score threshold (default: 55)")
    parser.add_argument("--n-top", type=int, default=50,
                        help="Number of top US symbols by market cap (default: 50)")
    parser.add_argument("--n-expiries", type=int, default=2,
                        help="Number of nearest expiry dates per symbol (default: 2)")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbols to scan (overrides --n-top)")
    parser.add_argument("--out", default=str(OUT_DEFAULT),
                        help="Output HTML path")
    main(parser.parse_args())
