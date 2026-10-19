"""
tv_sector_heatmap.py
====================
TradingView Sector Heatmap — Interactive Treemap via Plotly.js (CDN)

Fetches top stocks from the TV scanner grouped by GICS sector, then renders
an interactive treemap coloured by daily % change. No pip dependency for the
chart — uses plotly.js CDN in the HTML output.

Supported markets: america, australia, india, hongkong, singapore, japan, korea

Usage:
  python tv_sector_heatmap.py
  python tv_sector_heatmap.py --exchange australia --n 200
  python tv_sector_heatmap.py --out tradingview/reports/sector_heatmap.html

As library:
  from tradingview.analysis.tv_sector_heatmap import build_sector_data, build_html
  data = build_sector_data("america", n=300)
  html = build_html(data, "america")
"""
from __future__ import annotations

import sys
import json
import argparse
import urllib.request
import urllib.error
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
ROOT = TV.parent                           # stocks/
OUT_DEFAULT = ROOT / "reports" / "sector_heatmap.html"

# ─── Scanner endpoints ───────────────────────────────────────────────────────
_SCANNER_URLS = {
    "america":   "https://scanner.tradingview.com/america/scan",
    "australia":  "https://scanner.tradingview.com/australia/scan",
    "india":      "https://scanner.tradingview.com/india/scan",
    "hongkong":   "https://scanner.tradingview.com/hongkong/scan",
    "singapore":  "https://scanner.tradingview.com/singapore/scan",
    "japan":      "https://scanner.tradingview.com/japan/scan",
    "korea":      "https://scanner.tradingview.com/korea/scan",
}

# Fields to fetch — confirmed valid across all exchange scanners
_COLUMNS = [
    "name", "close", "change", "market_cap_basic",
    "sector", "industry", "description",
    "RSI", "volume", "Recommend.All",
]

# ─── Cookie auth (optional, boosts rate limits) ─────────────────────────────
_COOKIE_PATH = TV / "cookie"
_TV_COOKIE: Optional[str] = (
    _COOKIE_PATH.read_text(encoding="utf-8").strip()
    if _COOKIE_PATH.exists() else None
)


# ─── Scanner fetch ───────────────────────────────────────────────────────────

def _tv_scan(url: str, columns: list, n: int = 300) -> list[dict]:
    """POST to TV scanner and return parsed rows."""
    payload = {
        "columns": columns,
        "filter": [
            {"left": "market_cap_basic", "operation": "greater", "right": 50_000_000},
        ],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, n],
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"  [error] Scanner fetch failed: {exc}")
        return []

    rows = []
    for item in data.get("data", []):
        vals = item.get("d", [])
        rec = {"symbol": item.get("s", "")}
        for i, col in enumerate(columns):
            key = col.replace(".", "_").lower()
            rec[key] = vals[i] if i < len(vals) else None
        rows.append(rec)
    return rows


def build_sector_data(exchange: str = "america", n: int = 300) -> list[dict]:
    """
    Fetch top N stocks by market cap from TV scanner and return sector-grouped data.

    Returns list of dicts:
      [{symbol, name, sector, industry, market_cap, change, rsi, close}, ...]
    Rows without sector are assigned "Other".

    Special value: exchange="all" fetches from every supported exchange and
    combines results (n is split equally across exchanges).
    """
    # BUG-008 fix: support "all" to combine data from every exchange
    if exchange == "all":
        combined: list[dict] = []
        per_ex = max(50, n // len(_SCANNER_URLS))
        for ex_key in _SCANNER_URLS:
            combined.extend(build_sector_data(ex_key, per_ex))
        return combined

    url = _SCANNER_URLS.get(exchange)
    if not url:
        print(f"  [error] Unknown exchange: {exchange}")
        return []

    print(f"  Fetching top {n} stocks from {exchange} scanner...")
    rows = _tv_scan(url, _COLUMNS, n)
    print(f"  Got {len(rows)} rows")

    results = []
    for r in rows:
        sector = r.get("sector") or "Other"
        industry = r.get("industry") or "Unknown"
        market_cap = r.get("market_cap_basic") or 0
        change = r.get("change") or 0
        if market_cap <= 0:
            continue
        results.append({
            "symbol": r.get("symbol", ""),
            "name": (r.get("description") or r.get("name") or "")[:40],
            "sector": sector,
            "industry": industry,
            "market_cap": market_cap,
            "change": round(change, 2),
            "rsi": r.get("rsi"),
            "close": r.get("close"),
            "recommend": r.get("recommend_all"),
        })
    return results


def _sector_summary(data: list[dict]) -> list[dict]:
    """Aggregate data by sector for top-level treemap labels."""
    sectors: dict[str, dict] = {}
    for item in data:
        s = item["sector"]
        if s not in sectors:
            sectors[s] = {"sector": s, "total_cap": 0, "count": 0,
                          "weighted_change": 0.0}
        sectors[s]["total_cap"] += item["market_cap"]
        sectors[s]["count"] += 1
        sectors[s]["weighted_change"] += item["change"] * item["market_cap"]

    result = []
    for s, info in sectors.items():
        avg_change = (info["weighted_change"] / info["total_cap"]
                      if info["total_cap"] > 0 else 0)
        result.append({
            "sector": s,
            "total_cap": info["total_cap"],
            "count": info["count"],
            "avg_change": round(avg_change, 2),
        })
    return sorted(result, key=lambda x: x["total_cap"], reverse=True)


def build_html(data: list[dict], exchange: str = "america") -> str:
    """
    Build an interactive HTML treemap using Plotly.js CDN.
    Tiles sized by market cap, coloured by daily % change.
    """
    if not data:
        return "<html><body><h2>No data available</h2></body></html>"

    summary = _sector_summary(data)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build plotly treemap data arrays
    ids = []
    labels = []
    parents = []
    values = []
    colors = []
    hover_texts = []

    # Root
    ids.append("Market")
    labels.append(exchange.upper())
    parents.append("")
    values.append(0)
    colors.append(0)
    hover_texts.append(f"Total: {len(data)} stocks")

    # Sector level
    for sec in summary:
        sec_id = sec["sector"]
        ids.append(sec_id)
        labels.append(f"{sec_id}<br>{sec['avg_change']:+.2f}%")
        parents.append("Market")
        values.append(0)
        colors.append(sec["avg_change"])
        hover_texts.append(
            f"<b>{sec_id}</b><br>"
            f"Stocks: {sec['count']}<br>"
            f"Avg Change: {sec['avg_change']:+.2f}%<br>"
            f"Total Cap: ${sec['total_cap']/1e9:.0f}B"
        )

    # Stock level
    for item in data:
        stock_id = f"{item['sector']}_{item['symbol']}"
        ids.append(stock_id)
        short_sym = item["symbol"].split(":")[-1] if ":" in item["symbol"] else item["symbol"]
        labels.append(f"{short_sym}<br>{item['change']:+.1f}%")
        parents.append(item["sector"])
        values.append(int(item["market_cap"]))
        colors.append(item["change"])
        rsi_str = f"RSI: {item['rsi']:.0f}" if item.get("rsi") else ""
        hover_texts.append(
            f"<b>{short_sym}</b> — {item['name']}<br>"
            f"Change: {item['change']:+.2f}%<br>"
            f"Price: {item.get('close', 'N/A')}<br>"
            f"Market Cap: ${item['market_cap']/1e9:.1f}B<br>"
            f"{rsi_str}"
        )

    # Serialize for JS
    j = json.dumps
    ids_json = j(ids)
    labels_json = j(labels)
    parents_json = j(parents)
    values_json = j(values)
    colors_json = j(colors)
    hover_json = j(hover_texts)

    # Summary table rows
    summary_rows = ""
    for sec in summary:
        chg = sec["avg_change"]
        color = "#22c55e" if chg > 0.5 else ("#ef4444" if chg < -0.5 else "#a3a3a3")
        cap_b = sec["total_cap"] / 1e9
        summary_rows += (
            f"<tr>"
            f"<td>{sec['sector']}</td>"
            f"<td class='num'>{sec['count']}</td>"
            f"<td class='num'>${cap_b:,.0f}B</td>"
            f"<td class='num' style='color:{color};font-weight:700'>{chg:+.2f}%</td>"
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sector Heatmap — {exchange.upper()}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" integrity="sha384-cCVCZkAjYNxaYKbM8lsArLznDF/SvMFr1jcZrvOpSTCa0W40ZAdLzHCEulnUa5i7" crossorigin="anonymous"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 16px; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .meta {{ font-size: 0.85rem; color: #94a3b8; margin-bottom: 16px; }}
  #heatmap {{ width: 100%; height: 70vh; min-height: 500px; }}
  .summary {{ margin-top: 20px; }}
  .summary table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  .summary th, .summary td {{ padding: 6px 12px; text-align: left;
                               border-bottom: 1px solid #334155; }}
  .summary th {{ color: #94a3b8; font-weight: 600; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
<h1>Sector Heatmap — {exchange.upper()}</h1>
<p class="meta">Generated {now} | {len(data)} stocks | Tile size = Market Cap | Colour = Daily % Change</p>

<div id="heatmap"></div>

<div class="summary">
<h2 style="font-size:1.1rem; margin-bottom:8px">Sector Summary</h2>
<table>
<thead><tr><th>Sector</th><th class="num">Stocks</th><th class="num">Total Cap</th><th class="num">Avg Change</th></tr></thead>
<tbody>
{summary_rows}
</tbody>
</table>
</div>

<script>
var data = [{{
  type: "treemap",
  ids: {ids_json},
  labels: {labels_json},
  parents: {parents_json},
  values: {values_json},
  marker: {{
    colors: {colors_json},
    colorscale: [
      [0.0, '#dc2626'],
      [0.3, '#f87171'],
      [0.45, '#fca5a5'],
      [0.5, '#404040'],
      [0.55, '#86efac'],
      [0.7, '#4ade80'],
      [1.0, '#16a34a']
    ],
    cmid: 0,
    cmin: -5,
    cmax: 5,
    colorbar: {{
      title: "% Change",
      ticksuffix: "%",
      len: 0.6,
      thickness: 15
    }}
  }},
  text: {hover_json},
  hoverinfo: "text",
  textinfo: "label",
  textfont: {{ size: 11, color: "#fff" }},
  branchvalues: "total",
  pathbar: {{ visible: true }}
}}];

var layout = {{
  margin: {{ t: 10, l: 10, r: 10, b: 10 }},
  paper_bgcolor: '#0f172a',
  font: {{ color: '#e2e8f0' }}
}};

Plotly.newPlot('heatmap', data, layout, {{ responsive: true }});
</script>
</body>
</html>"""


def main(args: argparse.Namespace) -> None:
    data = build_sector_data(args.exchange, args.n)
    if not data:
        print("  No data fetched — check network / exchange name")
        return
    html = build_html(data, args.exchange)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  Heatmap written to {out}  ({len(data)} stocks)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TradingView Sector Heatmap — Interactive Treemap"
    )
    parser.add_argument("--exchange", default="america",
                        choices=list(_SCANNER_URLS.keys()) + ["all"],
                        help="TV exchange/market to scan (use 'all' for every market)")
    parser.add_argument("--n", type=int, default=300,
                        help="Number of stocks to fetch (by market cap)")
    parser.add_argument("--out", default=str(OUT_DEFAULT),
                        help="Output HTML path")
    main(parser.parse_args())
