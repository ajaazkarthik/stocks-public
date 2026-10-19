"""
Live Price Analysis for WIN Alert Fires
Fetches current prices via yfinance and compares with trigger prices.
Generates an interactive HTML report with in-depth analysis.
"""

import json
import time
import datetime
from pathlib import Path
ROOT = Path(__file__).parent.parent
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# ─── Symbol mapping: TradingView → yfinance ────────────────────────────────────

def tv_to_yf(tv_symbol: str) -> str:
    """Convert TradingView symbol to yfinance ticker."""
    exchange, ticker = tv_symbol.split(":", 1)
    if exchange == "ASX":
        return f"{ticker}.AX"
    elif exchange == "NSE":
        return f"{ticker}.NS"
    elif exchange == "SGX":
        return f"{ticker}.SI"
    elif exchange == "HKEX":
        # Zero-pad to 4 digits
        return f"{ticker.zfill(4)}.HK"
    elif exchange in ("NYSE", "NASDAQ"):
        return ticker
    else:
        return ticker


# ─── Load fires data ────────────────────────────────────────────────────────────

print("Loading fires data...")
fires = json.load(open(ROOT / "fires_analysed.json"))
print(f"Loaded {len(fires)} fire records")

# Deduplicate symbols for batch download
unique_tv_symbols = list({f["symbol"] for f in fires})
tv_to_yf_map = {sym: tv_to_yf(sym) for sym in unique_tv_symbols}
unique_yf_tickers = list(set(tv_to_yf_map.values()))

print(f"Unique symbols: {len(unique_tv_symbols)}")
print(f"Fetching prices for {len(unique_yf_tickers)} tickers...")


# ─── Fetch current prices via yfinance ─────────────────────────────────────────

def fetch_prices_batch(tickers: list, batch_size: int = 50) -> dict:
    """Fetch latest closing prices in batches. Returns {yf_ticker: price}."""
    prices = {}
    currency_map = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        batch_str = " ".join(batch)
        try:
            data = yf.download(batch_str, period="5d", interval="1d",
                               progress=False, auto_adjust=True, threads=True)
            close = data["Close"] if "Close" in data else data.get("Adj Close", pd.DataFrame())
            if close.empty:
                continue
            last_prices = close.ffill().iloc[-1]
            if isinstance(last_prices, pd.Series):
                for tick, price in last_prices.items():
                    if pd.notna(price):
                        prices[tick] = float(price)
            else:
                # Single ticker
                if pd.notna(last_prices):
                    prices[batch[0]] = float(last_prices)
        except Exception as e:
            print(f"  Batch error: {e}")
        time.sleep(0.3)

    # For any missing, try individually
    missing = [t for t in tickers if t not in prices]
    if missing:
        print(f"  Retrying {len(missing)} missing tickers individually...")
        for tick in missing:
            try:
                tkr = yf.Ticker(tick)
                hist = tkr.history(period="5d", interval="1d", auto_adjust=True)
                if not hist.empty:
                    prices[tick] = float(hist["Close"].dropna().iloc[-1])
            except Exception:
                pass
            time.sleep(0.1)
    return prices


prices = fetch_prices_batch(unique_yf_tickers)
print(f"Got prices for {len(prices)}/{len(unique_yf_tickers)} tickers")

# Report missing
missing_syms = [sym for sym, yft in tv_to_yf_map.items() if yft not in prices]
if missing_syms:
    print(f"No price data: {missing_syms}")


# ─── Build enriched records ─────────────────────────────────────────────────────

records = []
for fire in fires:
    tv_sym = fire["symbol"]
    yf_ticker = tv_to_yf_map[tv_sym]
    trigger_price = fire["metrics"].get("price")
    current_price = prices.get(yf_ticker)

    pct_change = None
    if trigger_price and current_price and trigger_price > 0:
        pct_change = (current_price - trigger_price) / trigger_price * 100

    records.append({
        "symbol": tv_sym,
        "exchange": fire["exchange"],
        "timeframe": fire["timeframe"],
        "fire_id": fire["fire_id"],
        "trigger_price": trigger_price,
        "current_price": current_price,
        "pct_change": round(pct_change, 2) if pct_change is not None else None,
        "direction": "UP" if pct_change and pct_change > 0 else ("DOWN" if pct_change and pct_change < 0 else "FLAT"),
        "WinRate": fire["metrics"].get("WinRate"),
        "NetProfit%": fire["metrics"].get("NetProfit%"),
        "rsi": fire["metrics"].get("rsi"),
        "ADX": fire["metrics"].get("ADX"),
        "sqzOff": fire["metrics"].get("sqzOff") == "true",
        "ma_up": fire["metrics"].get("ma_up") == "true",
        "LongTrade": fire["metrics"].get("LongTrade") == "true",
        "srBuySignal": fire["metrics"].get("srBuySignal") == "true",
    })

df = pd.DataFrame(records)
df_with_price = df[df["pct_change"].notna()].copy()
df_missing = df[df["current_price"].isna()].copy()

# Save enriched JSON
df.to_json(ROOT / "fires_live_prices.json", orient="records", indent=2)

print(f"\n=== PRICE FETCH SUMMARY ===")
print(f"Total fires:      {len(df)}")
print(f"With live price:  {len(df_with_price)}")
print(f"Missing price:    {len(df_missing)}")
print(f"\n=== PERFORMANCE OVERVIEW ===")
gainers = df_with_price[df_with_price["pct_change"] > 0]
losers = df_with_price[df_with_price["pct_change"] < 0]
flat = df_with_price[df_with_price["pct_change"] == 0]
print(f"UP:   {len(gainers)} ({len(gainers)/len(df_with_price)*100:.1f}%)")
print(f"DOWN: {len(losers)} ({len(losers)/len(df_with_price)*100:.1f}%)")
print(f"FLAT: {len(flat)}")
print(f"Avg % change: {df_with_price['pct_change'].mean():.2f}%")
print(f"Median % change: {df_with_price['pct_change'].median():.2f}%")
print(f"Best:  {df_with_price.loc[df_with_price['pct_change'].idxmax(), 'symbol']} +{df_with_price['pct_change'].max():.2f}%")
print(f"Worst: {df_with_price.loc[df_with_price['pct_change'].idxmin(), 'symbol']} {df_with_price['pct_change'].min():.2f}%")


# ─── Build HTML Report ──────────────────────────────────────────────────────────

def color_pct(val):
    if val is None:
        return "gray"
    if val > 5:
        return "#00c853"
    if val > 0:
        return "#69f0ae"
    if val < -5:
        return "#d50000"
    if val < 0:
        return "#ff5252"
    return "#888"


def build_table_html(df_sub: pd.DataFrame, title: str, limit: int = None) -> str:
    rows_df = df_sub.sort_values("pct_change", ascending=False)
    if limit:
        rows_df = rows_df.head(limit)
    rows_html = ""
    for _, r in rows_df.iterrows():
        pc = r["pct_change"]
        pc_str = f"+{pc:.2f}%" if pc and pc > 0 else (f"{pc:.2f}%" if pc is not None else "N/A")
        pc_color = color_pct(pc)
        cur = f"{r['current_price']:.3f}" if r['current_price'] else "N/A"
        trig = f"{r['trigger_price']:.3f}" if r['trigger_price'] else "N/A"
        wr = f"{r['WinRate']:.1f}%" if r['WinRate'] else "N/A"
        np_ = f"{r['NetProfit%']:.1f}%" if r['NetProfit%'] else "N/A"
        rsi = f"{r['rsi']:.1f}" if r['rsi'] else "N/A"
        adx = f"{r['ADX']:.1f}" if r['ADX'] else "N/A"
        sqz = "✅" if r['sqzOff'] else "❌"
        ma = "✅" if r['ma_up'] else "❌"
        rows_html += f"""
        <tr>
          <td><strong>{r['symbol']}</strong></td>
          <td>{r['timeframe']}</td>
          <td>{trig}</td>
          <td>{cur}</td>
          <td style="color:{pc_color};font-weight:bold">{pc_str}</td>
          <td>{wr}</td>
          <td>{np_}</td>
          <td>{rsi}</td>
          <td>{adx}</td>
          <td>{sqz}</td>
          <td>{ma}</td>
        </tr>"""
    return f"""
    <h2>{title}</h2>
    <table>
      <tr>
        <th>Symbol</th><th>TF</th><th>Trigger $</th><th>Current $</th>
        <th>% Change</th><th>WinRate</th><th>NetProfit%</th>
        <th>RSI</th><th>ADX</th><th>SqzOff</th><th>MA↑</th>
      </tr>
      {rows_html}
    </table>"""


# ─── Plotly Charts ──────────────────────────────────────────────────────────────

def fig_to_html(fig) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=False)


# 1. Distribution of % change
fig_dist = go.Figure()
fig_dist.add_trace(go.Histogram(
    x=df_with_price["pct_change"],
    nbinsx=40,
    marker_color=["#00c853" if v >= 0 else "#d50000" for v in df_with_price["pct_change"]],
    name="% Change Distribution"
))
fig_dist.update_layout(
    title="Distribution of % Change from Trigger Price",
    xaxis_title="% Change",
    yaxis_title="Count",
    plot_bgcolor="#1a1a2e",
    paper_bgcolor="#16213e",
    font_color="white",
    height=350
)

# 2. Gainers vs Losers by Exchange
exc_perf = df_with_price.groupby(["exchange", "direction"]).size().reset_index(name="count")
fig_exc = px.bar(
    exc_perf, x="exchange", y="count", color="direction",
    color_discrete_map={"UP": "#00c853", "DOWN": "#d50000", "FLAT": "#888888"},
    title="Gainers vs Losers by Exchange",
    barmode="group"
)
fig_exc.update_layout(
    plot_bgcolor="#1a1a2e", paper_bgcolor="#16213e", font_color="white", height=350
)

# 3. WinRate vs % Change scatter
df_scatter = df_with_price[df_with_price["WinRate"].notna()].copy()
fig_scatter = px.scatter(
    df_scatter,
    x="WinRate",
    y="pct_change",
    color="pct_change",
    color_continuous_scale=["#d50000", "#888888", "#00c853"],
    range_color=[-20, 20],
    size="ADX",
    text="symbol",
    hover_data=["symbol", "timeframe", "NetProfit%", "rsi", "trigger_price", "current_price"],
    title="WinRate vs Price Change (size = ADX strength)"
)
fig_scatter.update_traces(textposition="top center", textfont_size=8)
fig_scatter.update_layout(
    plot_bgcolor="#1a1a2e", paper_bgcolor="#16213e", font_color="white", height=500
)
fig_scatter.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.4)

# 4. Top 20 gainers bar chart
top20 = df_with_price.nlargest(20, "pct_change")
colors_top = ["#00c853" if v >= 0 else "#d50000" for v in top20["pct_change"]]
fig_top20 = go.Figure(go.Bar(
    x=top20["symbol"] + " [" + top20["timeframe"] + "]",
    y=top20["pct_change"],
    marker_color=colors_top,
    text=[f"{v:+.2f}%" for v in top20["pct_change"]],
    textposition="outside"
))
fig_top20.update_layout(
    title="Top 20 Price Movers (from Trigger)",
    xaxis_tickangle=-45,
    plot_bgcolor="#1a1a2e", paper_bgcolor="#16213e", font_color="white", height=450
)

# 5. NetProfit% vs Actual % Change (quality vs reality)
df_np = df_with_price[df_with_price["NetProfit%"].notna()].copy()
fig_np = px.scatter(
    df_np,
    x="NetProfit%",
    y="pct_change",
    color="exchange",
    hover_data=["symbol", "WinRate", "rsi", "timeframe"],
    title="Strategy NetProfit% vs Actual Price Change Since Alert",
    labels={"NetProfit%": "Strategy Backtest NetProfit%", "pct_change": "Actual % Change Since Alert"}
)
fig_np.update_layout(
    plot_bgcolor="#1a1a2e", paper_bgcolor="#16213e", font_color="white", height=400
)
fig_np.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.4)
fig_np.add_vline(x=15, line_dash="dash", line_color="yellow", opacity=0.4)

# 6. Performance by Timeframe box plot
fig_tf = px.box(
    df_with_price, x="timeframe", y="pct_change", color="timeframe",
    color_discrete_map={"1H": "#5c6bc0", "2H": "#e65100"},
    title="Price Change Distribution by Timeframe",
    points="all"
)
fig_tf.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.4)
fig_tf.update_layout(
    plot_bgcolor="#1a1a2e", paper_bgcolor="#16213e", font_color="white", height=400
)

# 7. High quality signals (WinRate>50 AND NetProfit>15) performance
hq = df_with_price[(df_with_price["WinRate"] > 50) & (df_with_price["NetProfit%"] > 15)].copy()
hq_sorted = hq.sort_values("pct_change", ascending=True)
colors_hq = ["#00c853" if v >= 0 else "#d50000" for v in hq_sorted["pct_change"]]
fig_hq = go.Figure(go.Bar(
    x=hq_sorted["pct_change"],
    y=hq_sorted["symbol"] + " [" + hq_sorted["timeframe"] + "]",
    orientation="h",
    marker_color=colors_hq,
    text=[f"{v:+.2f}%" for v in hq_sorted["pct_change"]],
    textposition="outside"
))
fig_hq.update_layout(
    title=f"High-Quality Signals (WinRate>50% & NetProfit>15%) — {len(hq)} signals",
    plot_bgcolor="#1a1a2e", paper_bgcolor="#16213e", font_color="white",
    height=max(400, len(hq) * 30)
)
fig_hq.add_vline(x=0, line_dash="dash", line_color="white", opacity=0.4)


# ─── Stats by Exchange ──────────────────────────────────────────────────────────

exc_stats = df_with_price.groupby("exchange")["pct_change"].agg(
    count="count",
    mean=lambda x: round(x.mean(), 2),
    median=lambda x: round(x.median(), 2),
    pct_up=lambda x: round((x > 0).mean() * 100, 1),
    best="max",
    worst="min"
).reset_index()


def exc_table_html(exc_df) -> str:
    rows = ""
    for _, r in exc_df.iterrows():
        c = color_pct(r["mean"])
        rows += f"""<tr>
          <td><strong>{r['exchange']}</strong></td>
          <td>{int(r['count'])}</td>
          <td style="color:{color_pct(r['mean'])}">{r['mean']:+.2f}%</td>
          <td style="color:{color_pct(r['median'])}">{r['median']:+.2f}%</td>
          <td>{r['pct_up']:.1f}%</td>
          <td style="color:#00c853">{r['best']:+.2f}%</td>
          <td style="color:#d50000">{r['worst']:+.2f}%</td>
        </tr>"""
    return f"""
    <table>
      <tr><th>Exchange</th><th>Signals</th><th>Avg Δ%</th><th>Median Δ%</th>
          <th>% Positive</th><th>Best</th><th>Worst</th></tr>
      {rows}
    </table>"""


# ─── Assemble Full HTML ─────────────────────────────────────────────────────────

now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
up_pct = len(gainers) / len(df_with_price) * 100
avg_chg = df_with_price["pct_change"].mean()
med_chg = df_with_price["pct_change"].median()
best_sym = df_with_price.loc[df_with_price["pct_change"].idxmax()]
worst_sym = df_with_price.loc[df_with_price["pct_change"].idxmin()]

all_table_html = build_table_html(df_with_price, "All Fires — Performance Since Alert")
top_gainers_html = build_table_html(df_with_price[df_with_price["pct_change"] > 0], "Top Gainers", limit=25)
high_quality_html = build_table_html(
    df_with_price[(df_with_price["WinRate"] > 50) & (df_with_price["NetProfit%"] > 15)],
    "High-Quality Signals (WinRate>50% & NetProfit>15%)"
)
biggest_movers_html = build_table_html(
    pd.concat([df_with_price.nlargest(30, "pct_change"), df_with_price.nsmallest(15, "pct_change")]).drop_duplicates(),
    "Biggest Movers (Top 30 Gainers + Worst 15)"
)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WIN Alert Fires — Live Price Analysis</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" integrity="sha384-cCVCZkAjYNxaYKbM8lsArLznDF/SvMFr1jcZrvOpSTCa0W40ZAdLzHCEulnUa5i7" crossorigin="anonymous"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0f0f1a; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; padding: 20px; }}
  h1 {{ color: #00e5ff; font-size: 2em; margin-bottom: 6px; }}
  h2 {{ color: #64b5f6; font-size: 1.3em; margin: 30px 0 10px; border-bottom: 1px solid #333; padding-bottom: 6px; }}
  .meta {{ color: #888; font-size: 0.9em; margin-bottom: 20px; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 20px 0; }}
  .kpi {{ background: #1a1a2e; border-radius: 10px; padding: 16px; text-align: center; border: 1px solid #2a2a4a; }}
  .kpi .val {{ font-size: 1.8em; font-weight: bold; }}
  .kpi .lbl {{ font-size: 0.75em; color: #888; margin-top: 4px; }}
  .green {{ color: #00c853; }}
  .red {{ color: #d50000; }}
  .blue {{ color: #40c4ff; }}
  .yellow {{ color: #ffd740; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82em; margin-bottom: 20px; }}
  th {{ background: #1a1a2e; color: #90caf9; padding: 8px 10px; text-align: left; position: sticky; top: 0; z-index: 1; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #1a1a2e; }}
  tr:hover {{ background: #1c1c30; }}
  .chart-wrap {{ background: #16213e; border-radius: 10px; padding: 10px; margin: 10px 0; }}
  .tabs {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 20px 0 10px; }}
  .tab {{ background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 6px; padding: 7px 16px;
           cursor: pointer; color: #90caf9; font-size: 0.85em; transition: background 0.2s; }}
  .tab.active {{ background: #0d47a1; color: white; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}
  .notice {{ background: #1a1a2e; border-left: 3px solid #ffd740; padding: 10px 16px;
              border-radius: 4px; margin: 12px 0; font-size: 0.85em; color: #ccc; }}
  .section {{ background: #12122a; border-radius: 10px; padding: 20px; margin: 16px 0; }}
</style>
</head>
<body>

<h1>📈 WIN Alert Fires — Live Price Analysis</h1>
<p class="meta">Generated: {now_str} &nbsp;|&nbsp; {len(df_with_price)} fires with live data &nbsp;|&nbsp; Data via yfinance (may be delayed)</p>

<div class="notice">
  ⚠️ Prices are as of market close (or last available). NSE/SGX/HKEX prices may be in local currency. 
  % change is calculated from the alert trigger price to the most recent available close.
</div>

<!-- KPIs -->
<div class="kpi-grid">
  <div class="kpi"><div class="val blue">{len(df_with_price)}</div><div class="lbl">Fires with Price Data</div></div>
  <div class="kpi"><div class="val green">{len(gainers)}</div><div class="lbl">Positive ({up_pct:.1f}%)</div></div>
  <div class="kpi"><div class="val red">{len(losers)}</div><div class="lbl">Negative ({100-up_pct:.1f}%)</div></div>
  <div class="kpi"><div class="val {'green' if avg_chg>=0 else 'red'}">{avg_chg:+.2f}%</div><div class="lbl">Avg % Change</div></div>
  <div class="kpi"><div class="val {'green' if med_chg>=0 else 'red'}">{med_chg:+.2f}%</div><div class="lbl">Median % Change</div></div>
  <div class="kpi"><div class="val green">+{best_sym['pct_change']:.2f}%</div><div class="lbl">Best: {best_sym['symbol']}</div></div>
  <div class="kpi"><div class="val red">{worst_sym['pct_change']:.2f}%</div><div class="lbl">Worst: {worst_sym['symbol']}</div></div>
  <div class="kpi"><div class="val yellow">{len(hq)}</div><div class="lbl">High-Quality Signals</div></div>
</div>

<!-- Exchange Summary -->
<div class="section">
  <h2>Performance by Exchange</h2>
  {exc_table_html(exc_stats)}
</div>

<!-- Charts -->
<div class="section">
  <h2>Charts</h2>
  <div class="chart-wrap">{fig_to_html(fig_dist)}</div>
  <div class="chart-wrap">{fig_to_html(fig_exc)}</div>
  <div class="chart-wrap">{fig_to_html(fig_top20)}</div>
  <div class="chart-wrap">{fig_to_html(fig_scatter)}</div>
  <div class="chart-wrap">{fig_to_html(fig_np)}</div>
  <div class="chart-wrap">{fig_to_html(fig_tf)}</div>
  <div class="chart-wrap">{fig_to_html(fig_hq)}</div>
</div>

<!-- Tabbed Tables -->
<div class="section">
  <h2>Detailed Tables</h2>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('hq')">⭐ High Quality</div>
    <div class="tab" onclick="switchTab('gainers')">🟢 Gainers</div>
    <div class="tab" onclick="switchTab('movers')">🏆 Top Movers</div>
    <div class="tab" onclick="switchTab('all')">📋 All ({len(df_with_price)})</div>
    <div class="tab" onclick="switchTab('missing')">❓ Missing Price ({len(df_missing)})</div>
  </div>
  <div id="hq" class="tab-content active">{high_quality_html}</div>
  <div id="gainers" class="tab-content">{top_gainers_html}</div>
  <div id="movers" class="tab-content">{biggest_movers_html}</div>
  <div id="all" class="tab-content">{all_table_html}</div>
  <div id="missing" class="tab-content">
    <h2>Symbols Without Live Price Data</h2>
    <table><tr><th>Symbol</th><th>Exchange</th><th>yfinance Ticker</th><th>Trigger Price</th></tr>
    {''.join(f"<tr><td>{r['symbol']}</td><td>{r['exchange']}</td><td>{tv_to_yf_map[r['symbol']]}</td><td>{r['trigger_price']}</td></tr>" for _, r in df_missing.iterrows())}
    </table>
  </div>
</div>

<script>
function switchTab(id) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body>
</html>"""

out_path = ROOT.parent / "reports" / "fires_live_analysis.html"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"\n✅ Report saved → {out_path}")
print(f"\nTop 10 Gainers:")
for _, r in df_with_price.nlargest(10, "pct_change").iterrows():
    print(f"  {r['symbol']:25s} [{r['timeframe']}]  trigger={r['trigger_price']:.3f}  now={r['current_price']:.3f}  {r['pct_change']:+.2f}%  WR={r['WinRate']}%")

print(f"\nTop 10 Losers:")
for _, r in df_with_price.nsmallest(10, "pct_change").iterrows():
    print(f"  {r['symbol']:25s} [{r['timeframe']}]  trigger={r['trigger_price']:.3f}  now={r['current_price']:.3f}  {r['pct_change']:+.2f}%  WR={r['WinRate']}%")
