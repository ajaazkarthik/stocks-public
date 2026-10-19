"""
Fundamental Analysis for Last N Fired Alerts
=============================================
Picks the last N unique symbols from fires_raw.json, fetches fundamental
data via yfinance, scores & ranks them, then produces an HTML report.

Usage:
    python fundamental_analysis.py [--n 10] [--out fundamental_report.html]
"""
import json, re, argparse, sys, math, io, contextlib
from datetime import datetime, timezone

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

# ─── Config ────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent.parent
FIRES_FILE   = ROOT / "fires_raw.json"
OUTPUT_HTML  = ROOT.parent / "reports" / "fundamental_report.html"

# TradingView exchange prefix → yfinance suffix
EXCHANGE_MAP = {
    "ASX":      ".AX",
    "ASX_DLY":  ".AX",
    "NSE":      ".NS",
    "BSE":      ".BO",
    "SGX":      ".SI",
    "HKEX":     ".HK",
    "NYSE":     "",
    "NASDAQ":   "",
    "AMEX":     "",
    "BATS":     "",
}

def tv_to_yfinance(pro_symbol: str) -> str:
    """Convert TradingView pro_symbol (e.g. ASX:CBA) to yfinance ticker."""
    if ":" not in pro_symbol:
        return pro_symbol
    exchange, ticker = pro_symbol.split(":", 1)
    ticker = ticker.replace("-", ".").strip()
    suffix = EXCHANGE_MAP.get(exchange.upper(), "")
    # HKEX zero-pad to 4 digits
    if exchange.upper() == "HKEX" and ticker.isdigit():
        ticker = ticker.zfill(4)
    return ticker + suffix

def parse_fire_meta(message: str) -> dict:
    """Extract signal metadata from fire message string."""
    result = {}
    for key in ["WinRate", "NetProfit%", "rsi", "sqzOff", "ma_up", "ADX",
                "BUY", "LongTrade", "srBuySignal", "prev_change_percent"]:
        m = re.search(rf"{re.escape(key)}:\s*([^,\n]+)", message)
        if m:
            val = m.group(1).strip().rstrip("%")
            try:
                result[key] = float(val)
            except ValueError:
                result[key] = val
    m = re.search(r"price:\s*([\d.]+)", message)
    if m:
        result["trigger_price"] = float(m.group(1))
    return result

def fetch_fundamentals(ticker: str) -> dict:
    """Fetch key fundamental metrics via yfinance."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            t = yf.Ticker(ticker)
            info = t.info or {}
    except Exception as e:
        return {"error": str(e)}
    if not info or info.get("quoteType") is None:
        return {"error": "no_data"}

    def g(key, default=None):
        v = info.get(key)
        return v if v not in (None, "N/A", 0) else default

    # Revenue growth: compare TTM revenue vs prior year from income statements
    rev_growth = g("revenueGrowth")  # yfinance provides as decimal (0.12 = 12%)
    if rev_growth is not None:
        rev_growth = round(rev_growth * 100, 2)

    earnings_growth = g("earningsGrowth")
    if earnings_growth is not None:
        earnings_growth = round(earnings_growth * 100, 2)

    profit_margin = g("profitMargins")
    if profit_margin is not None:
        profit_margin = round(profit_margin * 100, 2)

    roe = g("returnOnEquity")
    if roe is not None:
        roe = round(roe * 100, 2)

    roa = g("returnOnAssets")
    if roa is not None:
        roa = round(roa * 100, 2)

    dividend_yield = g("dividendYield")
    if dividend_yield is not None:
        dividend_yield = round(dividend_yield * 100, 2)

    return {
        "company_name":     g("longName", g("shortName", ticker)),
        "sector":           g("sector", "N/A"),
        "industry":         g("industry", "N/A"),
        "market_cap":       g("marketCap"),
        "currency":         g("currency", ""),
        # Valuation
        "trailing_pe":      g("trailingPE"),
        "forward_pe":       g("forwardPE"),
        "peg_ratio":        g("pegRatio"),
        "price_to_book":    g("priceToBook"),
        "price_to_sales":   g("priceToSalesTrailing12Months"),
        "ev_to_ebitda":     g("enterpriseToEbitda"),
        # Profitability
        "profit_margin":    profit_margin,
        "roe":              roe,
        "roa":              roa,
        "gross_margin":     round(g("grossMargins", 0) * 100, 2) if g("grossMargins") is not None else None,
        # Growth
        "revenue_growth":   rev_growth,
        "earnings_growth":  earnings_growth,
        "eps_ttm":          g("trailingEps"),
        "eps_forward":      g("forwardEps"),
        # Health
        "debt_to_equity":   g("debtToEquity"),
        "current_ratio":    g("currentRatio"),
        "quick_ratio":      g("quickRatio"),
        "free_cashflow":    g("freeCashflow"),
        # Income
        "dividend_yield":   dividend_yield,
        "payout_ratio":     round(g("payoutRatio", 0) * 100, 2) if g("payoutRatio") is not None else None,
        # Price
        "current_price":    g("currentPrice", g("regularMarketPrice")),
        "52w_high":         g("fiftyTwoWeekHigh"),
        "52w_low":          g("fiftyTwoWeekLow"),
        "beta":             g("beta"),
        "analyst_target":   g("targetMeanPrice"),
    }

def score_fundamental(fund: dict) -> dict:
    """
    Score a stock on fundamentals (0–100). Higher = better.

    Dimensions:
    1. Valuation      (25%): Forward P/E, PEG, P/S, P/B — lower is better
    2. Profitability  (30%): ROE, profit margin, gross margin — higher is better
    3. Growth         (25%): Revenue growth, EPS growth — higher is better
    4. Health         (20%): Debt/equity (lower), current ratio (higher), free cashflow

    Each metric is clamped to a 0–10 sub-score, then weighted.
    """
    scores = {}

    # ── Valuation ──────────────────────────────────────────────────────────────
    fpe = fund.get("forward_pe")
    peg = fund.get("peg_ratio")
    ps  = fund.get("price_to_sales")
    pb  = fund.get("price_to_book")

    def val_score(v, good, bad):
        """Lower is better. good → 10, bad → 0."""
        if v is None or v <= 0:
            return 5  # neutral when missing
        if v <= good: return 10
        if v >= bad:  return 0
        return round(10 * (bad - v) / (bad - good), 2)

    s_fpe = val_score(fpe, 10, 40)
    s_peg = val_score(peg, 1, 4)
    s_ps  = val_score(ps,  1, 10)
    s_pb  = val_score(pb,  1, 5)
    scores["valuation"] = round((s_fpe * 0.4 + s_peg * 0.3 + s_ps * 0.15 + s_pb * 0.15), 2)

    # ── Profitability ──────────────────────────────────────────────────────────
    def pct_score(v, good, bad):
        """Higher is better."""
        if v is None:
            return 5
        if v >= good: return 10
        if v <= bad:  return 0
        return round(10 * (v - bad) / (good - bad), 2)

    s_roe   = pct_score(fund.get("roe"),          20, -10)
    s_pm    = pct_score(fund.get("profit_margin"), 20, -5)
    s_gm    = pct_score(fund.get("gross_margin"),  50, 10)
    scores["profitability"] = round(s_roe * 0.4 + s_pm * 0.4 + s_gm * 0.2, 2)

    # ── Growth ─────────────────────────────────────────────────────────────────
    s_rev  = pct_score(fund.get("revenue_growth"),  20, -10)
    s_eps  = pct_score(fund.get("earnings_growth"), 20, -20)
    scores["growth"] = round(s_rev * 0.5 + s_eps * 0.5, 2)

    # ── Health ─────────────────────────────────────────────────────────────────
    de  = fund.get("debt_to_equity")
    cr  = fund.get("current_ratio")
    fcf = fund.get("free_cashflow")

    s_de  = val_score(de, 30, 200)            # debt/equity: lower is better
    s_cr  = pct_score(cr, 2.5, 0.5) if cr else 5
    s_fcf = 10 if (fcf and fcf > 0) else (0 if (fcf and fcf < 0) else 5)
    scores["health"] = round(s_de * 0.4 + s_cr * 0.3 + s_fcf * 0.3, 2)

    # ── Weighted Total ─────────────────────────────────────────────────────────
    total = (scores["valuation"]    * 0.25
           + scores["profitability"] * 0.30
           + scores["growth"]        * 0.25
           + scores["health"]        * 0.20)
    scores["total"] = round(total, 2)
    return scores

def score_signal(meta: dict) -> float:
    """Score the alert signal quality (0–10)."""
    wr = meta.get("WinRate", 0)
    np = meta.get("NetProfit%", 0)
    # Normalize: WR 50%→5, 100%→10, 0%→0; NP 50%→5, 100%→10, 0%→0
    s_wr = min(10, max(0, wr / 10))
    s_np = min(10, max(0, np / 10))
    return round(s_wr * 0.5 + s_np * 0.5, 2)

def composite_score(fund_score: float, signal_score: float) -> float:
    """60% fundamentals + 40% signal quality → 0–100 composite."""
    return round((fund_score * 0.6 + signal_score * 0.4) * 10, 2)

def human_cap(v):
    if v is None:
        return "N/A"
    if v >= 1e12:
        return f"${v/1e12:.1f}T"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"

def fmt(v, unit="", decimals=2, na="\u2014"):
    if v is None:
        return na
    if isinstance(v, float) and math.isnan(v):
        return na
    return f"{v:.{decimals}f}{unit}"

def score_bar(score, max_score=10):
    pct = min(100, max(0, score / max_score * 100))
    color = "#22c55e" if pct >= 65 else ("#eab308" if pct >= 40 else "#ef4444")
    return f'<div class="bar-wrap"><div class="bar" style="width:{pct:.0f}%;background:{color}"></div><span>{score:.1f}</span></div>'

def build_html(ranked: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = ""
    for rank, item in enumerate(ranked, 1):
        meta = item["meta"]
        fund = item["fundamentals"]
        fs = item["fund_scores"]
        sig = item["signal_score"]
        comp = item["composite"]

        rows += f"""
<tr class="{'top3' if rank <= 3 else ''}">
  <td class="rank">#{rank}</td>
  <td class="sym"><div class="sym-name">{item['tv_symbol']}</div>
      <div class="sub">{fund.get('company_name','')[:30]}</div>
      <div class="badge">{fund.get('sector','')[:20]}</div></td>
  <td>{fmt(meta.get('trigger_price'), ' '+fund.get('currency',''))}</td>
  <td>{fmt(fund.get('current_price'), ' '+fund.get('currency',''))}</td>
  <td class="cap">{human_cap(fund.get('market_cap'))}</td>

  <td class="num">{fmt(fund.get('forward_pe'), 'x')}</td>
  <td class="num">{fmt(fund.get('peg_ratio'), 'x')}</td>
  <td class="num">{fmt(fund.get('price_to_book'), 'x')}</td>

  <td class="num {"green" if (fund.get("roe") or 0) > 15 else "red" if (fund.get("roe") or 0) < 0 else ""}">{fmt(fund.get('roe'), '%')}</td>
  <td class="num {"green" if (fund.get("profit_margin") or 0) > 10 else "red" if (fund.get("profit_margin") or 0) < 0 else ""}">{fmt(fund.get('profit_margin'), '%')}</td>

  <td class="num {"green" if (fund.get("revenue_growth") or 0) > 10 else "red" if (fund.get("revenue_growth") or 0) < 0 else ""}">{fmt(fund.get('revenue_growth'), '%')}</td>
  <td class="num {"green" if (fund.get("earnings_growth") or 0) > 10 else "red" if (fund.get("earnings_growth") or 0) < 0 else ""}">{fmt(fund.get('earnings_growth'), '%')}</td>

  <td class="num">{fmt(fund.get('debt_to_equity'), '')}</td>
  <td class="num {"green" if (fund.get("dividend_yield") or 0) > 0 else ""}">{fmt(fund.get('dividend_yield'), '%')}</td>

  <td>{score_bar(meta.get('WinRate',0), 100)} WR</td>
  <td>{score_bar(meta.get('NetProfit%',0), 100)} NP%</td>

  <td>{score_bar(fs['valuation'])}</td>
  <td>{score_bar(fs['profitability'])}</td>
  <td>{score_bar(fs['growth'])}</td>
  <td>{score_bar(fs['health'])}</td>

  <td class="composite">{score_bar(comp, 100)}</td>
</tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fundamental Analysis – Last {len(ranked)} Fired Alerts</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; font-size: 13px; }}
  h1 {{ padding: 20px 24px 8px; font-size: 20px; color: #f8fafc; }}
  .meta {{ padding: 0 24px 16px; color: #94a3b8; font-size: 12px; }}
  .legend {{ display:flex; gap:16px; padding:0 24px 14px; flex-wrap:wrap; }}
  .legend span {{ background:#1e293b; border:1px solid #334155; border-radius:4px; padding:3px 10px; font-size:11px; }}
  .wrap {{ overflow-x: auto; padding: 0 16px 40px; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 1400px; }}
  th {{ background: #1e293b; padding: 8px 10px; text-align: left; 
        font-size: 11px; color: #94a3b8; position: sticky; top: 0; z-index:10;
        border-bottom: 2px solid #334155; white-space:nowrap; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #1e293b; vertical-align: middle; }}
  tr:hover td {{ background: #1e293b; }}
  tr.top3 td {{ background: #16213e; }}
  tr.top3:hover td {{ background: #1a2a50; }}
  .rank {{ font-size:20px; font-weight:700; color:#fbbf24; text-align:center; }}
  .sym-name {{ font-weight: 600; font-size: 13px; color:#38bdf8; }}
  .sub {{ color: #64748b; font-size: 11px; }}
  .badge {{ display:inline-block; margin-top:2px; background:#1e3a5f; color:#7dd3fc;
            border-radius:3px; padding:1px 5px; font-size:10px; }}
  .cap {{ color:#94a3b8; white-space:nowrap; }}
  .num {{ text-align:right; font-family:monospace; font-size:12px; }}
  .green {{ color:#4ade80; }}
  .red   {{ color:#f87171; }}
  .composite {{ min-width:100px; }}
  .bar-wrap {{ display:flex; align-items:center; gap:5px; min-width:70px; }}
  .bar {{ height:8px; border-radius:4px; transition:width .3s; }}
  .bar-wrap span {{ font-size:11px; color:#94a3b8; min-width:25px; font-family:monospace; }}
  .section-head {{ background:#0f172a !important; color:#475569; font-size:10px; text-align:center; letter-spacing:.05em; }}
  .disclaimer {{ padding:20px 24px; color:#475569; font-size:11px; }}
</style>
</head>
<body>
<h1>📊 Fundamental Analysis — Last {len(ranked)} Fired Alerts</h1>
<div class="meta">Generated: {now} | Ranked by Composite Score (60% fundamentals + 40% signal quality)</div>
<div class="legend">
  <span>🟢 Composite Score: fundamentals + signal quality (0–100, higher = better)</span>
  <span>📈 WR = Backtest Win Rate from fire message</span>
  <span>💰 NP% = Backtest Net Profit % from fire message</span>
  <span>🔴 Red = below-average | 🟡 Yellow = average | 🟢 Green = above-average</span>
</div>
<div class="wrap">
<table>
<thead>
<tr>
  <th rowspan="2">Rank</th>
  <th rowspan="2">Symbol</th>
  <th rowspan="2">Trigger<br>Price</th>
  <th rowspan="2">Current<br>Price</th>
  <th rowspan="2">Mkt Cap</th>
  <th colspan="3" class="section-head">VALUATION</th>
  <th colspan="2" class="section-head">PROFITABILITY</th>
  <th colspan="2" class="section-head">GROWTH</th>
  <th colspan="2" class="section-head">HEALTH &amp; INCOME</th>
  <th colspan="2" class="section-head">SIGNAL (backtest)</th>
  <th colspan="4" class="section-head">FUNDAMENTAL SCORES</th>
  <th rowspan="2">COMPOSITE<br>RANK</th>
</tr>
<tr>
  <th>Fwd P/E</th><th>PEG</th><th>P/B</th>
  <th>ROE</th><th>Net<br>Margin</th>
  <th>Rev<br>Growth</th><th>EPS<br>Growth</th>
  <th>D/E</th><th>Div<br>Yield</th>
  <th>Win<br>Rate</th><th>Net<br>Profit%</th>
  <th>Val</th><th>Prof</th><th>Grwth</th><th>Hlth</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<details style="padding:0 24px 20px">
  <summary style="cursor:pointer;color:#64748b;font-size:12px;padding:8px 0">
    ℹ️ Scoring Methodology (click to expand)
  </summary>
  <div style="color:#475569;font-size:12px;line-height:1.6;padding:10px 0">
    <b>Valuation (25%)</b>: Forward P/E (40%), PEG (30%), P/S (15%), P/B (15%) — lower is better. P/E: &lt;10→10pts, &gt;40→0pts.<br>
    <b>Profitability (30%)</b>: ROE (40%), Net Margin (40%), Gross Margin (20%) — higher is better. ROE: &gt;20%→10pts, &lt;-10%→0pts.<br>
    <b>Growth (25%)</b>: Revenue Growth (50%), EPS Growth (50%) — higher is better. &gt;20%→10pts, &lt;-10%→0pts.<br>
    <b>Health (20%)</b>: D/E (40%), Current Ratio (30%), Free Cashflow positive (30%) — healthier is better.<br>
    <b>Signal Quality (40% of composite)</b>: Backtest Win Rate (50%) + Net Profit% (50%).<br>
    <b>Composite = 60% × Fundamental Total + 40% × Signal Score</b>, scaled to 0–100.
  </div>
</details>
<div class="disclaimer">
  ⚠️ This analysis is for educational/research purposes only. Fundamental data sourced from yfinance (Yahoo Finance). 
  Not financial advice. Past performance does not guarantee future results.
</div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Fundamental analysis of last N fired alerts")
    parser.add_argument("--n",   type=int, default=10, help="Number of most recent fires to analyze")
    parser.add_argument("--out", type=str, default=str(OUTPUT_HTML), help="Output HTML file path")
    parser.add_argument("--no-dedup", action="store_true", help="Allow same symbol multiple times")
    args = parser.parse_args()

    # ── Load fires ─────────────────────────────────────────────────────────────
    print(f"Loading {FIRES_FILE} ...")
    with open(FIRES_FILE) as f:
        data = json.load(f)
    all_fires = data.get("all", [])
    print(f"  Total fires in file: {len(all_fires)}")

    # ── Pick last N unique symbols ──────────────────────────────────────────────
    selected = []
    seen_symbols = set()
    for fire in all_fires:  # already sorted newest-first
        sym = fire.get("pro_symbol") or fire.get("symbol")
        if not args.no_dedup and sym in seen_symbols:
            continue
        seen_symbols.add(sym)
        selected.append(fire)
        if len(selected) >= args.n:
            break

    print(f"\n  Selected {len(selected)} fires for analysis:")
    for i, f in enumerate(selected, 1):
        print(f"    {i}. {f.get('pro_symbol','?')} | {f.get('fire_time','')} | res:{f.get('resolution','')}m")

    # ── Fetch fundamentals & score ─────────────────────────────────────────────
    results = []
    no_data_syms = []
    print(f"\nFetching fundamentals from Yahoo Finance ...")
    for i, fire in enumerate(selected, 1):
        sym = fire.get("pro_symbol") or fire.get("symbol", "")
        yf_ticker = tv_to_yfinance(sym)
        meta = parse_fire_meta(fire.get("message", ""))

        print(f"  [{i}/{len(selected)}] {sym} → {yf_ticker} ...", end=" ", flush=True)
        fund = fetch_fundamentals(yf_ticker)
        fs   = score_fundamental(fund)
        sig  = score_signal(meta)
        comp = composite_score(fs["total"], sig)
        status = "no data — default score" if fund.get("error") == "no_data" else (
                 f"error: {fund['error']}" if fund.get("error") else
                 f"Composite={comp:.1f}  Fund={fs['total']:.1f}  Signal={sig:.1f}")
        print(status)
        if fund.get("error"):
            no_data_syms.append(sym)

        results.append({
            "tv_symbol":    sym,
            "yf_ticker":    yf_ticker,
            "fire_time":    fire.get("fire_time", ""),
            "resolution":   fire.get("resolution", ""),
            "meta":         meta,
            "fundamentals": fund,
            "fund_scores":  fs,
            "signal_score": sig,
            "composite":    comp,
        })

    if no_data_syms:
        print(f"\n  ⚠ No fundamental data for: {', '.join(no_data_syms)}")
        print(f"    These use default scores — rankings for them are not meaningful.")

    # ── Sort by composite ──────────────────────────────────────────────────────
    ranked = sorted(results, key=lambda x: x["composite"], reverse=True)

    print(f"\n{'='*60}")
    print("FINAL RANKING (highest composite score first):")
    print(f"{'='*60}")
    print(f"{'Rank':<5} {'Symbol':<20} {'Composite':>10} {'Fund':>8} {'Signal':>8} {'WinRate':>9} {'NP%':>8}")
    print("-"*60)
    for rank, item in enumerate(ranked, 1):
        wr  = item["meta"].get("WinRate",  0)
        np_ = item["meta"].get("NetProfit%", 0)
        has_data = not item["fundamentals"].get("error")
        data_tag = "" if has_data else "  [no fund data]"
        print(f"#{rank:<4} {item['tv_symbol']:<20} {item['composite']:>10.1f} "
              f"{item['fund_scores']['total']:>8.1f} {item['signal_score']:>8.1f} "
              f"{wr:>8.1f}%  {np_:>6.1f}%{data_tag}")

    # ── Build and save HTML ────────────────────────────────────────────────────
    html = build_html(ranked)
    out_path = Path(args.out)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n✅ HTML report saved → {out_path}")
    print("   Open in browser to view the full fundamental ranking.")

    # ── Persist to report_analytics.db ───────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent.parent / "reports"))
        from report_db import save_fundamental_analysis_snapshot, archive_report as _arc
        save_fundamental_analysis_snapshot(ranked)
        _arc(out_path)
    except Exception as _e:
        print(f"  [report_db] fundamental_analysis snapshot skipped: {_e}")

if __name__ == "__main__":
    main()

