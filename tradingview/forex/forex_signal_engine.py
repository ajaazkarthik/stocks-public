"""
forex_signal_engine.py
======================
Forex pair scoring engine — L1 Technical (30%), L2 Macro/Carry (25%),
L3 Sentiment (25%), L4 Consensus (20%).

Uses:
  - TV forex scanner for technical data (RSI, MACD, ADX, SMA50/200, ATR)
  - Hardcoded central-bank rates for carry-trade scoring
  - TV Recommend.All / Recommend.MA / Recommend.Other for consensus

Usage:
  python forex_signal_engine.py
  python forex_signal_engine.py --pairs EURUSD,GBPUSD --out tradingview/reports/forex_signal_report.html

As library:
  from tradingview.forex.forex_signal_engine import ForexSignalEngine
  engine = ForexSignalEngine()
  result = engine.score_pair("FX:EURUSD")
"""
from __future__ import annotations

import sys
import json
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Paths ────────────────────────────────────────────────────────────────────
TV = Path(__file__).parent.parent          # tradingview/
OUT_DEFAULT = TV.parent / "reports" / "forex_signal_report.html"

# ─── Cookie auth (optional) ──────────────────────────────────────────────────
_COOKIE_PATH = TV / "cookie"
_TV_COOKIE: Optional[str] = (
    _COOKIE_PATH.read_text(encoding="utf-8").strip()
    if _COOKIE_PATH.exists() else None
)

# ─── Central bank rates (updated periodically) ───────────────────────────────
# Source: https://www.investing.com/central-banks/
# These should be updated when rates change; they shift slowly.
_CB_RATES = {
    "USD": 5.25, "EUR": 4.25, "GBP": 5.00, "JPY": 0.25, "AUD": 4.35,
    "NZD": 5.50, "CAD": 4.50, "CHF": 1.50, "SEK": 3.75, "NOK": 4.50,
    "SGD": 3.50, "HKD": 5.25, "CNY": 3.45, "INR": 6.50, "ZAR": 8.25,
    "MXN": 11.0, "TRY": 50.0, "BRL": 10.75,
}

# ─── Default forex pairs to score ────────────────────────────────────────────
_DEFAULT_PAIRS = [
    "FX:EURUSD", "FX:GBPUSD", "FX:USDJPY", "FX:AUDUSD",
    "FX:USDCAD", "FX:NZDUSD", "FX:USDCHF", "FX:EURGBP",
    "FX:EURJPY", "FX:GBPJPY", "FX:AUDJPY", "FX:AUDNZD",
    "FX:USDSGD", "FX:USDINR", "FX:USDHKD",
]

# TV forex scanner fields
_COLUMNS = [
    "name", "close", "change", "RSI", "MACD.macd", "MACD.signal",
    "SMA50", "SMA200", "ADX", "ATR", "volume",
    "Recommend.All", "Recommend.MA", "Recommend.Other",
]


# ─── TV Scanner fetch ────────────────────────────────────────────────────────

def _tv_forex_scan(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch forex data from TV scanner for given symbols.
    Returns {symbol: {field: value, ...}, ...}.
    """
    url = "https://scanner.tradingview.com/forex/scan"
    payload = {
        "columns": _COLUMNS,
        "symbols": {"tickers": symbols},
        "options": {"lang": "en"},
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    }
    if _TV_COOKIE:
        headers["Cookie"] = _TV_COOKIE

    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"  [error] TV forex scan failed: {exc}")
        return {}

    result = {}
    for item in data.get("data", []):
        sym = item.get("s", "")
        vals = item.get("d", [])
        rec = {}
        for i, col in enumerate(_COLUMNS):
            key = col.replace(".", "_").lower()
            rec[key] = vals[i] if i < len(vals) else None
        result[sym] = rec
    return result


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ─── ForexSignalEngine ───────────────────────────────────────────────────────

class ForexSignalEngine:
    """Score forex pairs on a 0-100 scale using L1-L4 layers."""

    def __init__(self, cb_rates: Optional[dict] = None):
        self.cb_rates = cb_rates or _CB_RATES

    def _parse_currencies(self, symbol: str) -> tuple[str, str]:
        """Extract base/quote from 'FX:EURUSD' → ('EUR', 'USD')."""
        pair = symbol.split(":")[-1] if ":" in symbol else symbol
        pair = pair.upper()
        return pair[:3], pair[3:]

    def _l1_technical(self, d: dict) -> tuple[float, dict]:
        """
        L1 Technical Score (0-30).
        Components: RSI zone, MACD crossover, ADX trend strength, SMA position.
        """
        rsi = _safe_float(d.get("rsi"))
        macd = _safe_float(d.get("macd_macd"))
        macd_signal = _safe_float(d.get("macd_signal"))
        adx = _safe_float(d.get("adx"))
        close = _safe_float(d.get("close"))
        sma50 = _safe_float(d.get("sma50"))
        sma200 = _safe_float(d.get("sma200"))

        score = 0.0
        detail = {}

        # RSI — 9 pts max
        if rsi > 0:
            if 30 <= rsi <= 45:
                rsi_pts = 9.0  # oversold bounce zone
            elif 45 < rsi <= 55:
                rsi_pts = 6.0  # neutral
            elif 55 < rsi <= 70:
                rsi_pts = 7.5  # bullish momentum
            elif rsi < 30:
                rsi_pts = 4.0  # deeply oversold (risky)
            else:
                rsi_pts = 2.0  # overbought
            score += rsi_pts
            detail["rsi"] = round(rsi, 1)
            detail["rsi_pts"] = round(rsi_pts, 1)

        # MACD crossover — 7 pts max
        macd_diff = macd - macd_signal
        if abs(macd_diff) > 0:
            if macd_diff > 0:
                macd_pts = min(7.0, 3.5 + abs(macd_diff) * 100)
            else:
                macd_pts = max(0.0, 3.5 - abs(macd_diff) * 100)
        else:
            macd_pts = 3.5
        score += macd_pts
        detail["macd_pts"] = round(macd_pts, 1)

        # ADX trend strength — 7 pts max
        if adx > 25:
            adx_pts = min(7.0, 3.5 + (adx - 25) * 0.15)
        elif adx > 15:
            adx_pts = 3.5
        else:
            adx_pts = 1.5  # no clear trend
        score += adx_pts
        detail["adx"] = round(adx, 1)
        detail["adx_pts"] = round(adx_pts, 1)

        # SMA position — 7 pts max
        sma_pts = 3.5
        if close > 0 and sma50 > 0:
            if close > sma50:
                sma_pts += 1.75
            else:
                sma_pts -= 1.75
        if close > 0 and sma200 > 0:
            if close > sma200:
                sma_pts += 1.75
            else:
                sma_pts -= 1.75
        score += _clamp(sma_pts, 0, 7)
        detail["sma_pts"] = round(_clamp(sma_pts, 0, 7), 1)

        return _clamp(score, 0, 30), detail

    def _l2_macro(self, base: str, quote: str, d: dict) -> tuple[float, dict]:
        """
        L2 Macro/Carry Score (0-25).
        Components: carry trade (interest rate differential), price vs SMA200 trend.
        """
        detail = {}

        # Carry trade: base rate - quote rate
        base_rate = self.cb_rates.get(base, 3.0)
        quote_rate = self.cb_rates.get(quote, 3.0)
        carry_diff = base_rate - quote_rate
        # Max ±10 range → scale to 0-15 pts
        carry_pts = _clamp((carry_diff + 10) / 20 * 15, 0, 15)
        detail["base_rate"] = base_rate
        detail["quote_rate"] = quote_rate
        detail["carry_diff"] = round(carry_diff, 2)
        detail["carry_pts"] = round(carry_pts, 1)

        # Trend alignment: price vs SMA200 deviation
        close = _safe_float(d.get("close"))
        sma200 = _safe_float(d.get("sma200"))
        trend_pts = 5.0  # neutral
        if close > 0 and sma200 > 0:
            deviation = (close - sma200) / sma200 * 100
            trend_pts = _clamp(5.0 + deviation * 0.5, 0, 10)
            detail["trend_dev_pct"] = round(deviation, 2)
        detail["trend_pts"] = round(trend_pts, 1)

        return _clamp(carry_pts + trend_pts, 0, 25), detail

    def _l3_sentiment(self, d: dict) -> tuple[float, dict]:
        """
        L3 Sentiment Score (0-25).
        Uses TV Recommend.Other as proxy for market sentiment.
        Range: -1.0 (strong sell) to +1.0 (strong buy) → scale to 0-25.
        """
        rec_other = _safe_float(d.get("recommend_other"))
        # Map -1..+1 → 0..25
        score = _clamp((rec_other + 1) / 2 * 25, 0, 25)
        return score, {"recommend_other": round(rec_other, 3), "pts": round(score, 1)}

    def _l4_consensus(self, d: dict) -> tuple[float, dict]:
        """
        L4 Consensus Score (0-20).
        Uses TV Recommend.All (composite) and Recommend.MA (moving average).
        """
        rec_all = _safe_float(d.get("recommend_all"))
        rec_ma = _safe_float(d.get("recommend_ma"))
        # Weighted: 60% All + 40% MA → -1..+1 → scale to 0-20
        composite = rec_all * 0.6 + rec_ma * 0.4
        score = _clamp((composite + 1) / 2 * 20, 0, 20)
        return score, {
            "recommend_all": round(rec_all, 3),
            "recommend_ma": round(rec_ma, 3),
            "pts": round(score, 1),
        }

    def score_pair(self, symbol: str, tv_data: Optional[dict] = None) -> dict:
        """
        Score a single forex pair.

        Args:
            symbol: TV symbol like "FX:EURUSD"
            tv_data: Pre-fetched TV scanner data dict (optional; fetched if None)

        Returns dict with:
            symbol, base, quote, score, direction, recommendation,
            l1_score, l2_score, l3_score, l4_score, l1_detail, l2_detail, l3_detail, l4_detail,
            close, change, atr, broker_note
        """
        base, quote = self._parse_currencies(symbol)

        if tv_data is None:
            scan = _tv_forex_scan([symbol])
            tv_data = scan.get(symbol, {})

        l1, l1d = self._l1_technical(tv_data)
        l2, l2d = self._l2_macro(base, quote, tv_data)
        l3, l3d = self._l3_sentiment(tv_data)
        l4, l4d = self._l4_consensus(tv_data)

        total = round(l1 + l2 + l3 + l4, 1)

        if total >= 65:
            direction = f"BUY {base} / SELL {quote}"
            recommendation = "BUY"
        elif total <= 35:
            direction = f"SELL {base} / BUY {quote}"
            recommendation = "SELL"
        else:
            direction = "NEUTRAL"
            recommendation = "HOLD"

        # Broker routing note
        broker_note = "IBKR (full forex)"
        if "INR" in (base, quote):
            broker_note = "Zerodha / AngelOne (INR pairs)"
        elif "SGD" in (base, quote):
            broker_note = "IBKR or Moomoo-SG"

        return {
            "symbol": symbol,
            "pair": f"{base}/{quote}",
            "base": base,
            "quote": quote,
            "close": _safe_float(tv_data.get("close")),
            "change": round(_safe_float(tv_data.get("change")), 2),
            "atr": round(_safe_float(tv_data.get("atr")), 5),
            "score": total,
            "direction": direction,
            "recommendation": recommendation,
            "l1_score": round(l1, 1),
            "l2_score": round(l2, 1),
            "l3_score": round(l3, 1),
            "l4_score": round(l4, 1),
            "l1_detail": l1d,
            "l2_detail": l2d,
            "l3_detail": l3d,
            "l4_detail": l4d,
            "broker_note": broker_note,
        }

    def score_all(self, symbols: Optional[list[str]] = None) -> list[dict]:
        """Score multiple pairs in one batch scan."""
        symbols = symbols or _DEFAULT_PAIRS
        print(f"  Scanning {len(symbols)} forex pairs...")
        scan = _tv_forex_scan(symbols)
        print(f"  Got data for {len(scan)} pairs")

        results = []
        for sym in symbols:
            tv_data = scan.get(sym, {})
            results.append(self.score_pair(sym, tv_data))

        results.sort(key=lambda r: r["score"], reverse=True)
        return results


# ─── HTML Report Builder ─────────────────────────────────────────────────────

def build_html(results: list[dict]) -> str:
    """Build HTML report for scored forex pairs."""
    if not results:
        return "<html><body><h2>No forex data available</h2></body></html>"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = ""
    for i, r in enumerate(results):
        score = r["score"]
        rec = r["recommendation"]
        if rec == "BUY":
            badge = '<span style="background:#16a34a;color:#fff;padding:2px 8px;border-radius:4px;font-size:0.85rem">BUY</span>'
            row_bg = "#0f2d1a" if i < 5 else "#1a1a2e"
        elif rec == "SELL":
            badge = '<span style="background:#dc2626;color:#fff;padding:2px 8px;border-radius:4px;font-size:0.85rem">SELL</span>'
            row_bg = "#2d0f0f" if i < 5 else "#1a1a2e"
        else:
            badge = '<span style="background:#525252;color:#e5e5e5;padding:2px 8px;border-radius:4px;font-size:0.85rem">HOLD</span>'
            row_bg = "#1a1a2e"

        chg_color = "#4ade80" if r["change"] >= 0 else "#f87171"

        rows += f"""<tr style="background:{row_bg}">
  <td><b>{r['pair']}</b></td>
  <td class="num">{r['close']:.5f}</td>
  <td class="num" style="color:{chg_color}">{r['change']:+.2f}%</td>
  <td class="num" style="font-weight:700">{score:.0f}</td>
  <td class="num">{r['l1_score']:.0f}</td>
  <td class="num">{r['l2_score']:.0f}</td>
  <td class="num">{r['l3_score']:.0f}</td>
  <td class="num">{r['l4_score']:.0f}</td>
  <td>{badge}</td>
  <td>{r['direction']}</td>
  <td style="font-size:0.8rem;color:#94a3b8">{r['broker_note']}</td>
</tr>\n"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forex Signal Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 16px; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .meta {{ font-size: 0.85rem; color: #94a3b8; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ color: #94a3b8; font-weight: 600; position: sticky; top: 0;
       background: #0f172a; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr:hover {{ background: #1e293b !important; }}
  .legend {{ margin-top: 16px; font-size: 0.8rem; color: #94a3b8; }}
  .legend span {{ display: inline-block; margin-right: 16px; }}
</style>
</head>
<body>
<h1>Forex Signal Report</h1>
<p class="meta">Generated {now} | {len(results)} pairs scored | L1 Tech (30) + L2 Macro (25) + L3 Sentiment (25) + L4 Consensus (20) = 100</p>

<table>
<thead>
<tr>
  <th>Pair</th><th class="num">Rate</th><th class="num">Chg</th>
  <th class="num">Score</th><th class="num">L1</th><th class="num">L2</th>
  <th class="num">L3</th><th class="num">L4</th>
  <th>Action</th><th>Direction</th><th>Broker</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>

<div class="legend">
  <span>L1 = Technical (RSI, MACD, ADX, SMA)</span>
  <span>L2 = Macro/Carry (interest rate diff, trend)</span>
  <span>L3 = Sentiment (TV oscillators)</span>
  <span>L4 = Consensus (TV Recommend.All + MA)</span>
  <span>BUY ≥ 65 | SELL ≤ 35 | HOLD 35-65</span>
</div>
</body>
</html>"""


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    engine = ForexSignalEngine()

    pairs: Optional[list[str]] = None
    if args.pairs:
        pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
        # Auto-prefix FX: if missing
        pairs = [p if ":" in p else f"FX:{p}" for p in pairs]

    results = engine.score_all(pairs)
    html = build_html(results)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  Forex report written to {out}  ({len(results)} pairs)")

    # Print top 5 to console
    print("\n  Top 5 Forex Signals:")
    for r in results[:5]:
        print(f"    {r['pair']:10s}  Score={r['score']:5.1f}  {r['recommendation']:4s}  {r['direction']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Forex Signal Engine — L1-L4 scoring for currency pairs"
    )
    parser.add_argument("--pairs", default="",
                        help="Comma-separated pair list (e.g., EURUSD,GBPUSD). Default: 15 major pairs")
    parser.add_argument("--out", default=str(OUT_DEFAULT),
                        help="Output HTML path")
    main(parser.parse_args())
