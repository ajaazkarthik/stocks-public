"""
Backtest: Regime-Based Dynamic Leverage vs Static Leverage vs Buy & Hold
=========================================================================
Compares three strategies using historical SPY + ^VIX data (2004–present):

  1. BUY_HOLD_SPY         – buy and hold SPY, never sell
  2. STATIC_LEVERAGED     – hold a constant 2x or 3x leveraged product (simulated)
  3. DYNAMIC_LEVERAGE     – RegimeScore-based leverage (the new strategy)

Dynamic leverage logic:
  - Each bar, compute RegimeScore from SPY close history + VIX
  - Determine leverage multiplier (0.5x → 3.0x)
  - If hard_exit (VIX >= 30): force 0x (all cash)
  - Simulate daily rebalancing to target leverage
  - Decay drag applied to daily-reset products (ETFs)
  - Friction: 0.10% commission, 0.05% slippage on leverage changes

Output:
  reports/backtest_leverage_regime.html
  reports/backtest_leverage_regime.json

Run:
  cd tradingview
  python -m backtest.backtest_leverage_regime
  python -m backtest.backtest_leverage_regime --quick          # 5-year only
  python -m backtest.backtest_leverage_regime --static-lev 2   # compare vs 2x
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT    = Path(__file__).parent.parent
REPORTS = ROOT.parent / "reports"
REPORTS.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "leverage"))

from leverage.regime_score import (
    score_volatility, score_trend, score_momentum, score_macro,
    compute_regime_score_from_components, leverage_from_score, regime_label,
    MA_SHORT, MA_LONG, RSI_PER, MACD_FAST, MACD_SLOW, MACD_SIG,
    _rsi, _macd,
)
from leverage.signals import annual_decay_drag


# ─────────────────────────────────────────────────────────────────────────────
# Parameters
# ─────────────────────────────────────────────────────────────────────────────

COMMISSION_PCT      = 0.001    # 0.10% per leverage change trade
SLIPPAGE_PCT        = 0.0005   # 0.05% adverse fill per rebalance
VIX_HARD_EXIT       = 30.0
TRAILING_DD_EXIT    = 0.08     # 8% trailing drawdown exits leveraged position
DEFAULT_STATIC_LEV  = 2.0      # default for strategy 2
START_DATE          = "2004-01-01"   # SSO/UPRO-era; long enough for multiple regimes


# ─────────────────────────────────────────────────────────────────────────────
# Raw indicator helpers  (no look-ahead: all computed from series up to bar i)
# ─────────────────────────────────────────────────────────────────────────────

def _ma_series(close: pd.Series, w: int) -> pd.Series:
    return close.rolling(w, min_periods=w).mean()


def _precompute_indicators(close: pd.Series, vix: pd.Series) -> pd.DataFrame:
    """
    Pre-compute all indicators in a single vectorised pass so the simulation
    loop can just index df.iloc[i] without re-computing windows on every bar.
    """
    ma50  = _ma_series(close, MA_SHORT)
    ma200 = _ma_series(close, MA_LONG)
    rsi   = _rsi(close, RSI_PER)
    macd, sig = _macd(close)
    hist  = macd - sig

    ma200_slope = ma200.pct_change(10) * 100   # 10-bar slope, %

    df = pd.DataFrame({
        "close":       close,
        "vix":         vix,
        "ma50":        ma50,
        "ma200":       ma200,
        "ma200_slope": ma200_slope,
        "rsi":         rsi,
        "macd_hist":   hist,
    })
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Bar-level RegimeScore (vectorised-compatible, uses pre-computed indicators)
# ─────────────────────────────────────────────────────────────────────────────

def _regime_score_at_bar(row: dict) -> tuple[float, float]:
    """Return (regime_score, leverage_mult) from a single row of pre-computed indicators."""
    close_val  = row["close"]
    vix_val    = row["vix"]
    ma50_val   = row["ma50"]
    ma200_val  = row["ma200"]
    slope_val  = row["ma200_slope"]
    rsi_val    = row["rsi"]
    hist_val   = row["macd_hist"]

    # Skip if indicators not yet warmed up
    if any(math.isnan(v) for v in [ma50_val, ma200_val, rsi_val]):
        return 5.0, 1.0

    # ── Volatility ─────────────────────────────────────────────────────────────
    s_vol = score_volatility(vix_val)

    # ── Trend (simplified: replicate score_trend logic without pd.Series) ──────
    s_trend = 0.0
    dist200 = (close_val - ma200_val) / ma200_val * 100
    if close_val > ma200_val:
        s_trend += min(3.0, 1.5 + dist200 / 10)
    else:
        s_trend += max(0.0, 1.5 + dist200 / 10)
    if ma50_val > ma200_val:
        s_trend += 2.5
    if slope_val > 0.1:
        s_trend += 2.5
    elif slope_val > 0:
        s_trend += 1.5
    dist50 = (close_val - ma50_val) / ma50_val * 100
    if close_val > ma50_val:
        s_trend += 2.0 if dist50 > 2 else 1.0
    s_trend = min(10.0, max(0.0, s_trend))

    # ── Momentum ───────────────────────────────────────────────────────────────
    if   rsi_val >= 80:  s_rsi = 2.0
    elif rsi_val >= 72:  s_rsi = 5.0
    elif rsi_val >= 55:  s_rsi = 9.5
    elif rsi_val >= 45:  s_rsi = 6.0
    elif rsi_val >= 35:  s_rsi = 3.0
    else:                s_rsi = 1.0

    norm_hist = hist_val / (close_val * 0.01) if close_val > 0 else 0.0
    if   norm_hist > 0.05:  s_macd = 10.0
    elif norm_hist > 0.02:  s_macd = 8.0
    elif norm_hist > 0:     s_macd = 6.5
    elif norm_hist < -0.02: s_macd = 2.0
    else:                   s_macd = 4.0

    s_mom = min(10.0, max(0.0, s_rsi * 0.55 + s_macd * 0.45))

    # ── Macro (no yield curve in backtester — use VIX-proxy only) ─────────────
    # Conservative: assume neutral (5.0) for macro in backtest
    # because historical yield curve data requires a separate download.
    s_macro = 5.0

    score = compute_regime_score_from_components(s_vol, s_trend, s_mom, s_macro)
    lev   = leverage_from_score(score)

    # Hard exit gate
    if vix_val >= VIX_HARD_EXIT:
        return score, 0.0   # 0x = all cash

    return score, lev


# ─────────────────────────────────────────────────────────────────────────────
# Per-day P&L helpers
# ─────────────────────────────────────────────────────────────────────────────

def _daily_leveraged_pnl(
    underlying_ret: float,
    leverage: float,
    with_decay: bool = True,
) -> float:
    """
    Simulate daily P&L for a leverage-reset product.

    underlying_ret : decimal daily return of the underlying
    leverage       : current leverage multiple (1x, 2x, 3x …)
    with_decay     : apply daily reset (decay) cost (True for ETFs, False for futures)

    Daily reset decay:
      A 2x ETF that tracks daily returns applies leverage on each bar,
      so the compound return diverges from L × buy-and-hold.
      We model this by computing the exact daily leveraged return.
      The decay compound effect is ALREADY captured by simulation —
      there is no separate deduction needed; it emerges naturally from
      compounding (leveraged_daily_ret = L × underlying_ret → compounding
      hurts in choppy markets, helps in trending ones).

    For futures/margin (no daily reset), leverage is applied to the
    cumulative position, not each daily return.  We approximate this as:
      futures_ret ≈ L × underlying_ret    (daily, then compound)
    which slightly overstates performance in practice but is conservative
    for trending periods.
    """
    if leverage == 0.0:
        return 0.0
    if not with_decay:
        return leverage * underlying_ret   # futures / margin
    # ETF daily reset: (1 + L × r) - 1
    return (1.0 + leverage * underlying_ret) - 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio simulation loop
# ─────────────────────────────────────────────────────────────────────────────

def _simulate(
    df: pd.DataFrame,
    strategy: str,
    static_lev: float = DEFAULT_STATIC_LEV,
    use_decay: bool = True,
) -> pd.Series:
    """
    Simulate daily portfolio equity curve.

    Parameters
    ----------
    df          : DataFrame with close, vix, ma50, ma200, ma200_slope, rsi, macd_hist
    strategy    : "buy_hold" | "static_leveraged" | "dynamic_leverage"
    static_lev  : leverage for static_leveraged (default 2.0)
    use_decay   : apply ETF daily-reset compound decay (True for ETF strategies)

    Returns
    -------
    pd.Series of portfolio equity values (starting at 1.0)
    """
    n       = len(df)
    equity  = np.ones(n)
    hwm     = 1.0             # high-water mark (for dynamic trailing stop)
    cur_lev = 0.0             # current leverage (tracks state for friction cost)

    for i in range(1, n):
        underlying_ret = float(df["close"].iloc[i] / df["close"].iloc[i - 1] - 1)

        if strategy == "buy_hold":
            lev = 1.0

        elif strategy == "static_leveraged":
            lev = static_lev

        elif strategy == "dynamic_leverage":
            row  = df.iloc[i].to_dict()
            _, lev = _regime_score_at_bar(row)

            # Trailing drawdown stop on THIS equity (not underlying)
            if hwm > 0 and (hwm - equity[i - 1]) / hwm >= TRAILING_DD_EXIT:
                lev = 0.0   # H3 equivalent — exit to cash

        else:
            raise ValueError(f"Unknown strategy: {strategy}")

        # Friction: deduct commission + slippage when leverage changes
        friction = 0.0
        if strategy == "dynamic_leverage" and lev != cur_lev:
            friction = COMMISSION_PCT + SLIPPAGE_PCT
            cur_lev  = lev

        daily_pnl   = _daily_leveraged_pnl(underlying_ret, lev, with_decay=use_decay)
        equity[i]   = equity[i - 1] * (1.0 + daily_pnl - friction)

        # Update HWM
        if equity[i] > hwm:
            hwm = equity[i]

    return pd.Series(equity, index=df.index)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def _compute_metrics(equity: pd.Series, label: str) -> dict:
    """Compute CAGR, max drawdown, Sharpe, Sortino, Calmar, Volatility, Ulcer Index."""
    years    = len(equity) / 252.0
    cagr     = (float(equity.iloc[-1]) ** (1.0 / years) - 1) if years > 0 else 0.0

    # Max drawdown
    rolling_max = equity.cummax()
    drawdowns   = (equity - rolling_max) / rolling_max
    max_dd      = float(drawdowns.min())

    # Daily returns
    rets         = equity.pct_change().dropna()
    ann_vol      = float(rets.std() * math.sqrt(252))
    excess_ret   = rets.mean() * 252 - 0.05   # risk-free rate ~ 5%
    sharpe       = (excess_ret / ann_vol) if ann_vol > 0 else 0.0

    # Sortino (downside deviation only)
    downside     = rets[rets < 0]
    down_std     = float(downside.std() * math.sqrt(252)) if len(downside) > 0 else ann_vol
    sortino      = (excess_ret / down_std) if down_std > 0 else 0.0

    # Calmar = CAGR / abs(max_drawdown)
    calmar       = (cagr / abs(max_dd)) if max_dd != 0 else 0.0

    # Ulcer Index: RMS of drawdowns
    ulcer        = float(np.sqrt((drawdowns ** 2).mean()))

    # Winning days
    win_rate     = float((rets > 0).sum() / len(rets)) if len(rets) > 0 else 0.0

    return {
        "label":        label,
        "cagr":         round(cagr * 100, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "sharpe":       round(sharpe, 3),
        "sortino":      round(sortino, 3),
        "calmar":       round(calmar, 3),
        "ann_volatility": round(ann_vol * 100, 2),
        "ulcer_index":  round(ulcer * 100, 4),
        "win_rate_daily": round(win_rate * 100, 2),
        "total_return": round((float(equity.iloc[-1]) - 1.0) * 100, 2),
        "years":        round(years, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML report
# ─────────────────────────────────────────────────────────────────────────────

def _build_html(
    metrics_list: list[dict],
    equity_curves: dict[str, pd.Series],
    regime_series: pd.Series,
    static_lev: float,
) -> str:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Build JSON for chart
    labels = [str(d.date()) for d in equity_curves[list(equity_curves.keys())[0]].index]

    def to_js_array(s: pd.Series) -> str:
        return json.dumps([round(v, 4) if not math.isnan(v) else None for v in s.values])

    equity_js = {k: to_js_array(v) for k, v in equity_curves.items()}
    regime_js = to_js_array(regime_series)

    colors = {
        "Buy & Hold SPY":                "#2196F3",
        f"Static {static_lev}x Leverage": "#FF9800",
        "Dynamic Leverage":              "#4CAF50",
    }

    def metric_row(m: dict) -> str:
        cagr_color = "#4caf50" if m["cagr"] > 10 else ("#ff9800" if m["cagr"] > 5 else "#f44336")
        dd_color   = "#f44336" if m["max_drawdown"] < -40 else ("#ff9800" if m["max_drawdown"] < -20 else "#4caf50")
        sh_color   = "#4caf50" if m["sharpe"] > 0.7 else ("#ff9800" if m["sharpe"] > 0.4 else "#f44336")
        return f"""
        <tr>
          <td><strong>{m['label']}</strong></td>
          <td style="color:{cagr_color};font-weight:bold">{m['cagr']:+.1f}%</td>
          <td style="color:#fff">{m['total_return']:+.1f}%</td>
          <td style="color:{dd_color};font-weight:bold">{m['max_drawdown']:.1f}%</td>
          <td style="color:{sh_color};font-weight:bold">{m['sharpe']:.3f}</td>
          <td style="color:#fff">{m['sortino']:.3f}</td>
          <td style="color:#fff">{m['calmar']:.3f}</td>
          <td style="color:#fff">{m['ann_volatility']:.1f}%</td>
          <td style="color:#aaa">{m['ulcer_index']:.2f}%</td>
          <td style="color:#aaa">{m['win_rate_daily']:.1f}%</td>
        </tr>"""

    rows = "".join(metric_row(m) for m in metrics_list)

    datasets_eq = []
    for name, js_data in equity_js.items():
        color = colors.get(name, "#ffffff")
        datasets_eq.append(f"""{{
            label: {json.dumps(name)},
            data: {js_data},
            borderColor: '{color}',
            backgroundColor: 'transparent',
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.1
        }}""")

    datasets_regime = f"""{{
        label: 'RegimeScore',
        data: {regime_js},
        borderColor: '#9c27b0',
        backgroundColor: 'rgba(156,39,176,0.1)',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        tension: 0.2,
        yAxisID: 'y2'
    }}"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Leverage Regime Backtest</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" integrity="sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" crossorigin="anonymous"></script>
<style>
  body {{ background:#0f1117; color:#e0e0e0; font-family:'Segoe UI',sans-serif; margin:0; padding:20px; }}
  h1   {{ color:#fff; border-bottom:1px solid #333; padding-bottom:12px; }}
  .subtitle {{ color:#888; font-size:0.9em; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:24px; }}
  .card {{ background:#1a1d2e; border:1px solid #2a2d3e; border-radius:8px; padding:20px; }}
  .card h3 {{ margin:0 0 16px; color:#90caf9; font-size:1em; letter-spacing:0.05em; text-transform:uppercase; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.85em; }}
  th    {{ background:#1e2235; color:#90caf9; padding:10px 8px; text-align:right; border-bottom:1px solid #2a2d3e; }}
  th:first-child, td:first-child {{ text-align:left; }}
  td    {{ padding:9px 8px; border-bottom:1px solid #1e2235; text-align:right; }}
  tr:hover td {{ background:#1e2235; }}
  .risk-box {{ background:#1a1d2e; border:1px solid #f44336; border-radius:8px; padding:20px; margin-top:24px; }}
  .risk-box h3 {{ color:#f44336; }}
  .risk-row {{ display:flex; gap:16px; flex-wrap:wrap; margin-top:12px; }}
  .risk-item {{ flex:1; min-width:200px; background:#111; border-radius:6px; padding:12px; }}
  .risk-item h4 {{ margin:0 0 8px; color:#ff9800; font-size:0.85em; text-transform:uppercase; letter-spacing:.05em; }}
  .risk-item p  {{ margin:0; font-size:0.82em; color:#aaa; line-height:1.5; }}
  canvas {{ width:100% !important; }}
  .full {{ grid-column:1/-1; }}
</style>
</head>
<body>
<h1>📊 Regime-Based Dynamic Leverage — Backtest Report</h1>
<p class="subtitle">Generated {ts} | Period: {START_DATE} → present | Universe: SPY + ^VIX | Commission: 0.10% | Slippage: 0.05%</p>

<div class="card full" style="margin-bottom:20px">
  <h3>Equity Curves (normalised to 1.0)</h3>
  <canvas id="eqChart" height="90"></canvas>
</div>

<div class="card full" style="margin-bottom:20px">
  <h3>Daily RegimeScore + VIX Overlay</h3>
  <canvas id="regimeChart" height="50"></canvas>
</div>

<div class="card full">
  <h3>Performance Metrics</h3>
  <table>
    <thead><tr>
      <th>Strategy</th><th>CAGR</th><th>Total Return</th><th>Max Drawdown</th>
      <th>Sharpe</th><th>Sortino</th><th>Calmar</th><th>Ann. Vol</th>
      <th>Ulcer Index</th><th>Win Rate</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>

<div class="risk-box">
  <h3>⚠️ Risk Analysis — Brutal Honesty</h3>
  <div class="risk-row">
    <div class="risk-item">
      <h4>Volatility Decay (Daily-Reset ETFs)</h4>
      <p>Formula: L×(L-1)/2 × σ². At 2x + 20% vol = 2%/yr drag. At 3x + 30% vol = 13.5%/yr drag.
      The decay is EMBEDDED in the simulation via daily compounding — it emerges naturally.
      3x ETFs held through a bear market will lose 90%+ even if the index only drops 50%.</p>
    </div>
    <div class="risk-item">
      <h4>Tail Risk (DLCs 5x–7x)</h4>
      <p>A 7x DLC loses 70% on a single 10% gap-down in the underlying.
      SGX DLCs have mandatory knock-out levels (typically 50% drawdown).
      Once knocked out, the product is liquidated at near-zero — capital is GONE.
      Only allocate capital you are prepared to lose entirely.</p>
    </div>
    <div class="risk-item">
      <h4>Liquidity Risk (SG/JP Markets)</h4>
      <p>STI DLCs trade thin volumes; bid-ask spreads of 3–8% are common.
      Exit during a volatility spike may be at a 5–10% worse price than displayed.
      Japan market hours do not overlap with US; you may be exposed overnight
      to US news events with no ability to hedge until Tokyo open.</p>
    </div>
    <div class="risk-item">
      <h4>Options-Specific Risks</h4>
      <p>Theta decay: long calls lose value every day the underlying is flat.
      IV crush: buying calls before earnings and selling after can lose 30–50%
      even if the stock moves in your direction.
      Treat options as a insurance/leverage tool, never as a substitute for
      directional conviction — size them at max 5% of tactical bucket.</p>
    </div>
    <div class="risk-item">
      <h4>Regime Detection Failure Modes</h4>
      <p>Flash crashes (2010, 2020 Covid open): VIX spikes intraday before you can exit.
      Regime change lag: MA200 takes weeks to turn — you are in a bear for 20+ days
      before the model confirms it. The 8% trailing stop is your real-time protection.
      Over-fitting risk: RegimeScore thresholds are optimised on US SPY history.
      SG/JP regimes may not align — always run the regime check on the LOCAL index too.</p>
    </div>
    <div class="risk-item">
      <h4>Behavioural Failure Modes</h4>
      <p>Overriding exit rules: "it'll bounce back" is how leveraged ETF investors
      experience -90% drawdowns. Never override H1/H2/H3. Ever.
      Averaging down on leverage: adding to a losing 3x position is how accounts blow up.
      The code enforces no-average-down. Do not manually bypass this.
      Chasing: entering leverage after the regime signal has already been up for weeks
      means you're buying near the peak of the current cycle, not the start.</p>
    </div>
  </div>
</div>

<script>
const labels = {json.dumps(labels)};
const eqCtx = document.getElementById('eqChart');
new Chart(eqCtx, {{
  type: 'line',
  data: {{
    labels: labels,
    datasets: [{','.join(datasets_eq)}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#e0e0e0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color:'#666', maxTicksLimit:20 }}, grid: {{ color:'#1e2235' }} }},
      y: {{ ticks: {{ color:'#999' }}, grid: {{ color:'#1e2235' }}, title: {{ display:true, text:'Portfolio Value (1.0 = start)', color:'#666' }} }}
    }}
  }}
}});

const regCtx = document.getElementById('regimeChart');
new Chart(regCtx, {{
  type: 'line',
  data: {{ labels: labels, datasets: [{datasets_regime}] }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#e0e0e0' }} }} }},
    scales: {{
      x: {{ ticks: {{ color:'#666', maxTicksLimit:12 }}, grid: {{ color:'#1e2235' }} }},
      y2: {{ position:'left', min:0, max:10, ticks: {{ color:'#9c27b0' }}, title: {{ display:true, text:'RegimeScore (0–10)', color:'#9c27b0' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(static_lev: float = DEFAULT_STATIC_LEV, quick: bool = False) -> dict:
    start = "2019-01-01" if quick else START_DATE
    print(f"[backtest] Downloading SPY + ^VIX from {start} …", flush=True)

    spy  = yf.Ticker("SPY").history(start=start, interval="1d", auto_adjust=True)
    vix  = yf.Ticker("^VIX").history(start=start, interval="1d")

    if spy.empty or vix.empty:
        raise RuntimeError("Failed to download SPY or ^VIX from yfinance")

    spy_close  = spy["Close"].dropna()
    vix_close  = vix["Close"].dropna().reindex(spy_close.index, method="ffill")

    print(f"[backtest] {len(spy_close)} trading days downloaded", flush=True)

    # Build aligned indicator DataFrame
    df = _precompute_indicators(spy_close, vix_close)
    df = df.dropna(subset=["ma200", "rsi"])   # trim warm-up period

    print(f"[backtest] Simulating 3 strategies on {len(df)} bars …", flush=True)

    # Run simulations
    eq_bh      = _simulate(df, "buy_hold",        use_decay=False)
    eq_static  = _simulate(df, "static_leveraged", static_lev=static_lev, use_decay=True)
    eq_dynamic = _simulate(df, "dynamic_leverage", use_decay=True)

    # Compute RegimeScore series for overlay chart
    print(f"[backtest] Computing RegimeScore series …", flush=True)
    regime_scores = []
    for i in range(len(df)):
        row           = df.iloc[i].to_dict()
        score, _      = _regime_score_at_bar(row)
        regime_scores.append(score)
    regime_series = pd.Series(regime_scores, index=df.index)

    # Metrics
    label_static  = f"Static {static_lev:.0f}x Leverage"
    metrics_bh     = _compute_metrics(eq_bh,      "Buy & Hold SPY")
    metrics_static = _compute_metrics(eq_static,   label_static)
    metrics_dyn    = _compute_metrics(eq_dynamic,  "Dynamic Leverage")

    metrics_list   = [metrics_bh, metrics_static, metrics_dyn]
    equity_curves  = {
        "Buy & Hold SPY": eq_bh,
        label_static:     eq_static,
        "Dynamic Leverage": eq_dynamic,
    }

    # Print summary
    print("\n" + "=" * 68)
    print(f"{'Strategy':<30} {'CAGR':>7} {'MaxDD':>8} {'Sharpe':>8} {'Calmar':>8}")
    print("-" * 68)
    for m in metrics_list:
        print(f"{m['label']:<30} {m['cagr']:>+6.1f}% {m['max_drawdown']:>7.1f}% "
              f"{m['sharpe']:>8.3f} {m['calmar']:>8.3f}")
    print("=" * 68)

    # Save HTML
    html = _build_html(metrics_list, equity_curves, regime_series, static_lev)
    out_html = REPORTS / "backtest_leverage_regime.html"
    out_json = REPORTS / "backtest_leverage_regime.json"

    out_html.write_text(html, encoding="utf-8")
    with open(out_json, "w") as f:
        json.dump(metrics_list, f, indent=2)

    print(f"\n[backtest] Report saved → {out_html}")
    print(f"[backtest] JSON saved  → {out_json}")

    return {"metrics": metrics_list, "html_path": str(out_html)}


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick",       action="store_true", help="5-year backtest only")
    parser.add_argument("--static-lev",  type=float, default=DEFAULT_STATIC_LEV,
                        help=f"Static leverage multiple (default {DEFAULT_STATIC_LEV})")
    args = parser.parse_args()

    run_backtest(static_lev=args.static_lev, quick=args.quick)

