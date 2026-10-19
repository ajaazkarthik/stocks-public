"""
Benchmark Comparison: NAIVE vs FRICTION-AWARE backtest.

NAIVE  = current behaviour (entry/exit at same-bar close, zero fees, no volume check).
FRICTION = realistic: next-bar open fill, commission 0.10 % per side, slippage 0.05 %
           per side, and a hard volume cap (signal rejected if 1 share would exceed
           VOL_CAP_PCT of that bar's volume — proxy for liquidity).

Run:
    python -m backtest.benchmark_comparison          # uses default symbol list
    python -m backtest.benchmark_comparison --quick  # 3 symbols, fast CI check

Output:  reports/backtest_benchmark.html
         reports/backtest_benchmark.json
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

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Friction parameters (edit here to stress-test)
# ---------------------------------------------------------------------------
COMMISSION_PCT = 0.001   # 0.10 % per side
SLIPPAGE_PCT   = 0.0005  # 0.05 % per side (adverse price move at fill)
VOL_CAP_PCT    = 0.05    # reject entry if trade size > 5 % of bar volume
NOTIONAL       = 10_000  # assumed position size in currency units (for volume cap)

# ---------------------------------------------------------------------------
# Strategy parameters (must mirror backtest_buy_sell_v2.py exactly)
# ---------------------------------------------------------------------------
ATR_STOP_MULT  = 2.0
ATR_TRAIL_MULT = 1.5
RSI_EXIT       = 45
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
RSI_PERIOD     = 14
MA_PERIOD      = 20
ATR_PERIOD     = 14
ADX_PERIOD     = 14

# Symbol list (same as backtest_buy_sell_v2 for apples-to-apples)
STOCKS = [
    ("ASX:APE",     "APE.AX",     "1h",  "730d"),
    ("ASX:WDS",     "WDS.AX",     "1h",  "730d"),
    ("NSE:STLTECH", "STLTECH.NS", "1h",  "730d"),
    ("NYSE:ORCL",   "ORCL",       "1h",  "730d"),
    ("NASDAQ:AMD",  "AMD",        "1h",  "730d"),
    ("NASDAQ:AMAT", "AMAT",       "1h",  "730d"),
    ("NYSE:JNJ",    "JNJ",        "1d",  "5y"),
    ("NASDAQ:NVDA", "NVDA",       "1d",  "5y"),
    ("NYSE:UBER",   "UBER",       "1d",  "5y"),
    ("NASDAQ:AAPL", "AAPL",       "1d",  "5y"),
    ("NASDAQ:AMD",  "AMD",        "1d",  "5y"),
    ("ASX:WDS",     "WDS.AX",     "1d",  "5y"),
]

QUICK_STOCKS = [
    ("NASDAQ:NVDA", "NVDA",       "1d",  "5y"),
    ("NASDAQ:AMD",  "AMD",        "1d",  "5y"),
    ("NYSE:ORCL",   "ORCL",       "1h",  "730d"),
]


# ===========================================================================
# Indicators  (identical to backtest_buy_sell_v2.py — no look-ahead bias here)
# ===========================================================================

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag    = gain.ewm(com=period - 1, min_periods=period).mean()
    al    = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def calc_macd(close: pd.Series):
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd     = ema_fast - ema_slow
    signal   = macd.ewm(span=MACD_SIG, adjust=False).mean()
    return macd, signal


def calc_atr(high, low, close, period: int = 14) -> pd.Series:
    prev_c = close.shift(1)
    tr = pd.concat([high - low,
                    (high - prev_c).abs(),
                    (low  - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def calc_adx_dmi(high, low, close, period: int = 14):
    up   = high - high.shift(1)
    down = low.shift(1) - low
    pdm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    mdm  = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    atr  = calc_atr(high, low, close, period)
    pdi  = 100 * pdm.ewm(com=period - 1, min_periods=period).mean() / atr
    mdi  = 100 * mdm.ewm(com=period - 1, min_periods=period).mean() / atr
    dx   = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx  = dx.ewm(com=period - 1, min_periods=period).mean()
    return adx, pdi, mdi


# ===========================================================================
# Simulation engine
# ===========================================================================

def simulate(df: pd.DataFrame, mode: str) -> list[dict]:
    """
    Parameters
    ----------
    df   : OHLCV dataframe (Open, High, Low, Close, Volume)
    mode : "naive" | "friction"

    In NAIVE mode the simulation mirrors the current backtest_buy_sell_v2.py
    exactly so we get an identical baseline.

    In FRICTION mode:
      - Entry price  = next bar Open  + slippage_pct  (adverse)
      - Exit price   = next bar Open  - slippage_pct  (adverse, assume gap-open fill)
      - Round-trip commission deducted from P&L in %
      - Signal skipped if implied shares > VOL_CAP_PCT * next-bar volume
    """
    use_friction = (mode == "friction")

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]
    opens  = df["Open"]

    rsi                    = calc_rsi(close, RSI_PERIOD)
    macd, macd_sig         = calc_macd(close)
    atr                    = calc_atr(high, low, close, ATR_PERIOD)
    ma                     = close.rolling(MA_PERIOD).mean()
    adx, plus_di, minus_di = calc_adx_dmi(high, low, close, ADX_PERIOD)

    ma_up           = ma > ma.shift(3)
    ma_down         = (ma < ma.shift(1)) & (close < ma.shift(1))
    macd_above      = macd > macd_sig
    macd_crossunder = (macd < macd_sig) & (macd.shift(1) >= macd_sig)

    buy_raw  = (macd_above & (rsi >= 60) & ma_up &
                (adx > 10) & (plus_di > minus_di) & (plus_di > 30))
    buy_edge = buy_raw & ~buy_raw.shift(1).fillna(False).astype(bool)

    n      = len(df)
    idx    = df.index
    warmup = ATR_PERIOD + MACD_SLOW + 5

    in_trade    = False
    entry_price = np.nan
    hwm         = np.nan
    entry_bar   = -1
    trades: list[dict] = []

    for i in range(warmup, n):
        cur_close = close.iloc[i]
        cur_atr   = atr.iloc[i]
        cur_date  = idx[i]

        if not in_trade:
            if buy_edge.iloc[i]:
                if use_friction:
                    # Need data from next bar for fill
                    if i + 1 >= n:
                        continue  # no next bar — skip signal
                    fill_bar = i + 1
                    fill_vol = float(volume.iloc[fill_bar]) if not math.isnan(float(volume.iloc[fill_bar])) else 0
                    fill_open = float(opens.iloc[fill_bar])
                    # Volume cap: if we need more than VOL_CAP_PCT of bar's volume, skip
                    shares_needed = NOTIONAL / fill_open
                    if fill_vol > 0 and shares_needed > VOL_CAP_PCT * fill_vol:
                        trades.append({
                            "entry_date": cur_date.strftime("%Y-%m-%d %H:%M"),
                            "skipped": True,
                            "skip_reason": f"vol_cap ({shares_needed:.0f} > {VOL_CAP_PCT*fill_vol:.0f})",
                        })
                        continue
                    # Entry = next bar open with adverse slippage
                    entry_fill = fill_open * (1 + SLIPPAGE_PCT)
                    entry_price = entry_fill
                    entry_bar   = fill_bar
                else:
                    entry_price = cur_close
                    entry_bar   = i

                in_trade = True
                hwm      = entry_price
        else:
            hwm = max(hwm, cur_close)

            sell_reason = None

            if cur_close < entry_price - ATR_STOP_MULT * cur_atr:
                sell_reason = "ATR Hard Stop"
            elif cur_close < hwm - ATR_TRAIL_MULT * cur_atr:
                sell_reason = "ATR Trail Stop"
            elif ma_down.iloc[i] and not ma_down.iloc[i - 1]:
                sell_reason = "MA Breakdown"
            elif macd_crossunder.iloc[i]:
                sell_reason = "MACD Crossunder"
            elif atr.iloc[i] > 0 and cur_close < entry_price - ATR_STOP_MULT * cur_atr:
                sell_reason = "ATR Hard Stop"
            elif rsi.iloc[i] < RSI_EXIT and (i >= 3) and rsi.iloc[i] < rsi.iloc[i - 3]:
                sell_reason = "RSI Decay"

            if sell_reason:
                if use_friction:
                    if i + 1 < n:
                        exit_fill = float(opens.iloc[i + 1]) * (1 - SLIPPAGE_PCT)
                        exit_date = idx[i + 1].strftime("%Y-%m-%d %H:%M")
                    else:
                        # Last bar — fill at current close  (realistic for end-of-data)
                        exit_fill = cur_close * (1 - SLIPPAGE_PCT)
                        exit_date = cur_date.strftime("%Y-%m-%d %H:%M")
                    # Round-trip commission
                    rt_cost   = (COMMISSION_PCT * 2) * 100  # in %
                    raw_pnl   = (exit_fill - entry_price) / entry_price * 100
                    pnl_pct   = raw_pnl - rt_cost
                    hold_bars = (i + 1) - entry_bar
                else:
                    exit_fill = cur_close
                    exit_date = cur_date.strftime("%Y-%m-%d %H:%M")
                    pnl_pct   = (exit_fill - entry_price) / entry_price * 100
                    hold_bars = i - entry_bar

                trades.append({
                    "entry_date":  idx[entry_bar].strftime("%Y-%m-%d %H:%M"),
                    "exit_date":   exit_date,
                    "entry_price": round(float(entry_price), 4),
                    "exit_price":  round(float(exit_fill), 4),
                    "hwm":         round(float(hwm), 4),
                    "hold_bars":   hold_bars,
                    "pnl_pct":     round(float(pnl_pct), 2),
                    "exit_reason": sell_reason,
                    "win":         pnl_pct > 0,
                    "skipped":     False,
                })
                in_trade    = False
                entry_price = np.nan
                hwm         = np.nan

    if in_trade:
        ep  = entry_price
        lc  = float(close.iloc[-1])
        raw = (lc - ep) / ep * 100
        pnl = raw - (COMMISSION_PCT * 2 * 100) if use_friction else raw
        trades.append({
            "entry_date":  idx[entry_bar].strftime("%Y-%m-%d %H:%M"),
            "exit_date":   "OPEN",
            "entry_price": round(ep, 4),
            "exit_price":  round(lc, 4),
            "hwm":         round(float(hwm), 4),
            "hold_bars":   n - 1 - entry_bar,
            "pnl_pct":     round(pnl, 2),
            "exit_reason": "OPEN",
            "win":         pnl > 0,
            "skipped":     False,
        })

    return trades


# ===========================================================================
# Metrics
# ===========================================================================

def compute_metrics(trades: list[dict]) -> dict:
    """Compute summary metrics from a trades list (skipped entries excluded)."""
    real   = [t for t in trades if not t.get("skipped")]
    closed = [t for t in real if t.get("exit_reason") != "OPEN"]
    wins   = [t for t in closed if t["win"]]
    losses = [t for t in closed if not t["win"]]

    n = len(closed)
    if n == 0:
        return {k: 0 for k in ["n_trades", "n_wins", "n_losses", "win_rate",
                                "avg_win", "avg_loss", "total_pnl", "profit_factor",
                                "expectancy", "sharpe", "max_drawdown", "skipped"]}

    pnls      = [t["pnl_pct"] for t in closed]
    win_pnls  = [t["pnl_pct"] for t in wins]
    loss_pnls = [t["pnl_pct"] for t in losses]

    total_gain = sum(p for p in pnls if p > 0)
    total_loss = abs(sum(p for p in pnls if p < 0))

    # Sharpe on trade-level returns (no annualisation — just signal quality)
    mean_r = np.mean(pnls)
    std_r  = np.std(pnls, ddof=1) if n > 1 else 1e-9
    sharpe = mean_r / std_r if std_r > 1e-9 else 0.0

    # Max drawdown (equity curve based on cumulative sum of pnl_pct)
    equity = np.cumsum(pnls)
    peak   = np.maximum.accumulate(equity)
    dd     = equity - peak
    max_dd = float(np.min(dd)) if len(dd) else 0.0

    skipped = len([t for t in trades if t.get("skipped")])

    return {
        "n_trades":      n,
        "n_wins":        len(wins),
        "n_losses":      len(losses),
        "win_rate":      round(len(wins) / n * 100, 1),
        "avg_win":       round(float(np.mean(win_pnls))  if win_pnls  else 0, 2),
        "avg_loss":      round(float(np.mean(loss_pnls)) if loss_pnls else 0, 2),
        "total_pnl":     round(float(sum(pnls)), 2),
        "profit_factor": round(total_gain / total_loss, 3) if total_loss else float("inf"),
        "expectancy":    round(mean_r, 3),
        "sharpe":        round(float(sharpe), 3),
        "max_drawdown":  round(max_dd, 2),
        "skipped":       skipped,
    }


# ===========================================================================
# Per-symbol runner
# ===========================================================================

def run_symbol(tv_sym: str, yf_ticker: str, interval: str, period: str) -> dict | None:
    print(f"  {tv_sym} ({yf_ticker}) {interval}/{period} … ", end="", flush=True)
    try:
        df = yf.download(yf_ticker, interval=interval, period=period,
                         auto_adjust=True, progress=False, multi_level_index=False)
    except Exception as exc:
        print(f"FAILED: {exc}")
        return None

    if df is None or len(df) < 60:
        print(f"insufficient data ({len(df) if df is not None else 0} bars)")
        return None

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.dropna(inplace=True)

    naive_trades    = simulate(df, mode="naive")
    friction_trades = simulate(df, mode="friction")

    naive_m    = compute_metrics(naive_trades)
    friction_m = compute_metrics(friction_trades)

    print(f"OK  {len(df)} bars | "
          f"naive={naive_m['n_trades']}t {naive_m['win_rate']}%wr  "
          f"friction={friction_m['n_trades']}t {friction_m['win_rate']}%wr "
          f"(skip={friction_m['skipped']})")

    return {
        "tv_sym":    tv_sym,
        "yf_ticker": yf_ticker,
        "interval":  interval,
        "n_bars":    len(df),
        "naive":     naive_m,
        "friction":  friction_m,
        "naive_trades":    naive_trades,
        "friction_trades": friction_trades,
    }


# ===========================================================================
# HTML Report
# ===========================================================================

_CSS = """
body{margin:0;padding:0;background:#0e1117;color:#ddd;font-family:'Segoe UI',sans-serif;font-size:14px}
h1{margin:0;padding:24px 32px;background:linear-gradient(90deg,#1a2035,#23304a);
   font-size:1.6em;border-bottom:1px solid #2a3550}
h2{color:#7ec8e3;margin:0 0 12px;font-size:1.1em}
h3{color:#a8d8ea;margin:14px 0 6px}
.section{padding:20px 32px;border-bottom:1px solid #1e2d42}
.meta{color:#6b7b9a;font-size:0.83em;padding:0 32px 8px;
      background:linear-gradient(90deg,#1a2035,#23304a)}
.tbl{width:100%;border-collapse:collapse;font-size:0.84em}
.tbl th{background:#141c2e;color:#7ec8e3;padding:7px 9px;text-align:left;
        position:sticky;top:0;white-space:nowrap}
.tbl td{padding:5px 9px;border-bottom:1px solid #1c2a3a}
.tbl tr:hover td{background:rgba(126,200,227,0.04)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;
       font-size:0.78em;font-weight:700;color:#fff}
.verdict-box{border:1px solid #2a3550;border-radius:8px;padding:16px 20px;
             margin:14px 0;max-width:860px}
.verdict-box h3{margin:0 0 8px}
.v-good{border-color:#2ecc71}
.v-warn{border-color:#e67e22}
.v-bad {border-color:#e74c3c}
.delta-pos{color:#2ecc71;font-weight:700}
.delta-neg{color:#e74c3c;font-weight:700}
.delta-neu{color:#e67e22;font-weight:700}
"""

def _pct(v: float, pos_good: bool = True) -> str:
    cls = "delta-pos" if (v > 0) == pos_good else "delta-neg"
    if abs(v) < 0.05:
        cls = "delta-neu"
    sign = "+" if v > 0 else ""
    return f'<span class="{cls}">{sign}{v:.2f}%</span>'


def _delta(new: float, old: float, unit: str = "", pos_good: bool = True, fmt: str = ".2f") -> str:
    diff = new - old
    cls  = "delta-pos" if (diff > 0) == pos_good else "delta-neg"
    if abs(diff) < 0.001:
        cls = "delta-neu"
    sign = "+" if diff >= 0 else ""
    return f'<span class="{cls}">{sign}{diff:{fmt}}{unit}</span>'


def build_summary_html(results: list[dict]) -> str:
    """Aggregate + per-symbol summary table."""
    # Aggregate across all symbols
    total_naive = {k: 0.0 for k in ["n_trades","n_wins","total_pnl","skipped"]}
    total_fric  = {k: 0.0 for k in ["n_trades","n_wins","total_pnl","skipped"]}
    all_naive_pnl: list[float] = []
    all_fric_pnl:  list[float] = []

    rows = ""
    for r in results:
        nm, fm = r["naive"], r["friction"]
        for k in total_naive:
            total_naive[k] += nm.get(k, 0)
            total_fric[k]  += fm.get(k, 0)
        all_naive_pnl.extend(t["pnl_pct"] for t in r["naive_trades"]
                             if not t.get("skipped") and t.get("exit_reason") != "OPEN")
        all_fric_pnl.extend( t["pnl_pct"] for t in r["friction_trades"]
                             if not t.get("skipped") and t.get("exit_reason") != "OPEN")

        wr_naive = nm["win_rate"]
        wr_fric  = fm["win_rate"]
        pnl_naive = nm["total_pnl"]
        pnl_fric  = fm["total_pnl"]

        wr_cl  = "#2ecc71" if wr_naive >= 60 else "#e67e22" if wr_naive >= 45 else "#e74c3c"
        wr_cl2 = "#2ecc71" if wr_fric  >= 60 else "#e67e22" if wr_fric  >= 45 else "#e74c3c"

        rows += f"""<tr>
          <td><b>{r['tv_sym']}</b><br><small style="color:#aaa">{r['interval']}</small></td>
          <td>{nm['n_trades']}</td>
          <td style="color:{wr_cl};font-weight:700">{wr_naive}%</td>
          <td style="color:{'#2ecc71' if pnl_naive>=0 else '#e74c3c'};font-weight:700">{pnl_naive:+.1f}%</td>
          <td style="color:#888">{nm['sharpe']:.3f}</td>
          <td style="color:#888">{nm['max_drawdown']:.1f}%</td>
          <td style="width:16px"></td>
          <td>{fm['n_trades']} <small style="color:#888">(skip:{fm['skipped']})</small></td>
          <td style="color:{wr_cl2};font-weight:700">{wr_fric}%
              {_delta(wr_fric, wr_naive, 'pp', True, '.1f')}</td>
          <td style="color:{'#2ecc71' if pnl_fric>=0 else '#e74c3c'};font-weight:700">{pnl_fric:+.1f}%
              {_delta(pnl_fric, pnl_naive, '%', True, '.1f')}</td>
          <td style="color:#888">{fm['sharpe']:.3f}
              {_delta(fm['sharpe'], nm['sharpe'], '', True, '.3f')}</td>
          <td style="color:#888">{fm['max_drawdown']:.1f}%
              {_delta(fm['max_drawdown'], nm['max_drawdown'], '%', False, '.1f')}</td>
        </tr>"""

    # Aggregate Sharpe
    def _sharpe(pnls: list[float]) -> float:
        if len(pnls) < 2:
            return 0.0
        m, s = np.mean(pnls), np.std(pnls, ddof=1)
        return float(m / s) if s > 1e-9 else 0.0

    na_wr  = (total_naive["n_wins"] / total_naive["n_trades"] * 100) if total_naive["n_trades"] else 0
    fr_wr  = (total_fric["n_wins"]  / total_fric["n_trades"]  * 100) if total_fric["n_trades"]  else 0
    na_sh  = _sharpe(all_naive_pnl)
    fr_sh  = _sharpe(all_fric_pnl)

    footer = f"""<tr style="background:#141c2e;font-weight:700;border-top:2px solid #2a3550">
      <td>TOTAL</td>
      <td>{int(total_naive['n_trades'])}</td>
      <td style="color:{'#2ecc71' if na_wr>=60 else '#e67e22'}">{na_wr:.1f}%</td>
      <td style="color:{'#2ecc71' if total_naive['total_pnl']>=0 else '#e74c3c'}">{total_naive['total_pnl']:+.1f}%</td>
      <td>{na_sh:.3f}</td><td>—</td><td></td>
      <td>{int(total_fric['n_trades'])} <small style="color:#888">(skip:{int(total_fric['skipped'])})</small></td>
      <td style="color:{'#2ecc71' if fr_wr>=60 else '#e67e22'}">{fr_wr:.1f}%
          {_delta(fr_wr, na_wr, 'pp', True, '.1f')}</td>
      <td style="color:{'#2ecc71' if total_fric['total_pnl']>=0 else '#e74c3c'}">{total_fric['total_pnl']:+.1f}%
          {_delta(total_fric['total_pnl'], total_naive['total_pnl'], '%', True, '.1f')}</td>
      <td>{fr_sh:.3f} {_delta(fr_sh, na_sh, '', True, '.3f')}</td><td>—</td>
    </tr>"""

    return f"""
    <table class="tbl">
      <thead><tr>
        <th>Symbol</th>
        <th colspan="5" style="background:#1a2a1a;color:#6fcf97">— NAIVE (current backtest) ——————————</th>
        <th style="width:1px"></th>
        <th colspan="5" style="background:#2a1a1a;color:#eb5757">— FRICTION (next-bar + fees + vol-cap) ——</th>
      </tr>
      <tr>
        <th></th>
        <th>Trades</th><th>Win%</th><th>Total P&L</th><th>Sharpe</th><th>MaxDD</th>
        <th></th>
        <th>Trades</th><th>Win%</th><th>Total P&L</th><th>Sharpe</th><th>MaxDD</th>
      </tr></thead>
      <tbody>{rows}{footer}</tbody>
    </table>
    """


def build_verdict_html(results: list[dict]) -> str:
    """High-level verdict summarising the degradation."""
    all_n_pnl = [t["pnl_pct"] for r in results for t in r["naive_trades"]
                 if not t.get("skipped") and t.get("exit_reason") != "OPEN"]
    all_f_pnl = [t["pnl_pct"] for r in results for t in r["friction_trades"]
                 if not t.get("skipped") and t.get("exit_reason") != "OPEN"]

    def agg(pnls: list[float]) -> tuple[float, float, float]:
        if not pnls:
            return 0, 0, 0
        wr  = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        tot = sum(pnls)
        sh  = float(np.mean(pnls) / np.std(pnls, ddof=1)) if len(pnls) > 1 else 0
        return wr, tot, sh

    n_wr, n_tot, n_sh = agg(all_n_pnl)
    f_wr, f_tot, f_sh = agg(all_f_pnl)

    wr_drop  = n_wr  - f_wr
    tot_drop = n_tot - f_tot
    sh_drop  = n_sh  - f_sh

    skipped_total = sum(r["friction"]["skipped"] for r in results)
    entry_reduction = (sum(r["naive"]["n_trades"]    for r in results) -
                       sum(r["friction"]["n_trades"]  for r in results))

    # Verdict colour
    if sh_drop > 0.10 or tot_drop > 20:
        verdict_cls, verdict_txt = "v-bad", "DEGRADED — friction reveals inflated metrics"
    elif sh_drop > 0.03 or tot_drop > 5:
        verdict_cls, verdict_txt = "v-warn", "MODERATE DEGRADATION — real-world performance will differ"
    else:
        verdict_cls, verdict_txt = "v-good", "ROBUST — friction has minimal impact on this strategy"

    rt_cost_pct = (COMMISSION_PCT * 2 + SLIPPAGE_PCT * 2) * 100

    return f"""
    <div class="verdict-box {verdict_cls}">
      <h3>📋 Overall Verdict: {verdict_txt}</h3>
      <table class="tbl" style="max-width:560px">
        <thead><tr><th>Metric</th><th>NAIVE</th><th>FRICTION</th><th>Δ Impact</th></tr></thead>
        <tbody>
          <tr><td>Win Rate (all symbols)</td>
              <td style="color:#6fcf97">{n_wr:.1f}%</td>
              <td style="color:#eb5757">{f_wr:.1f}%</td>
              <td>{_delta(f_wr, n_wr, 'pp', True, '.1f')}</td></tr>
          <tr><td>Total P&L (sum across all trades)</td>
              <td style="color:#6fcf97">{n_tot:+.1f}%</td>
              <td style="color:#eb5757">{f_tot:+.1f}%</td>
              <td>{_delta(f_tot, n_tot, '%', True, '.1f')}</td></tr>
          <tr><td>Trade-level Sharpe</td>
              <td style="color:#6fcf97">{n_sh:.3f}</td>
              <td style="color:#eb5757">{f_sh:.3f}</td>
              <td>{_delta(f_sh, n_sh, '', True, '.3f')}</td></tr>
          <tr><td>Entries rejected by volume cap</td><td>—</td>
              <td>{skipped_total}</td><td>—</td></tr>
          <tr><td>Trade count reduction (next-bar miss)</td><td>—</td>
              <td>{entry_reduction}</td><td>—</td></tr>
          <tr><td>Round-trip friction cost (fees+slip)</td><td>0.00%</td>
              <td>{rt_cost_pct:.2f}%</td><td>—</td></tr>
        </tbody>
      </table>
    </div>

    <div class="verdict-box" style="margin-top:14px">
      <h3>🔬 What this benchmark proves</h3>
      <ul style="line-height:1.8;color:#ccc">
        <li><b>Next-bar fill</b> changes entry price to next bar's Open (±slippage). Gap opens
            eat into P&L and some winners/losers flip. The larger this effect, the more the
            current backtest flatters itself.</li>
        <li><b>Round-trip cost ({rt_cost_pct:.2f}%)</b> is deducted from every trade.
            For short-hold trades this is significant; for multi-day holds it is minor.</li>
        <li><b>Volume cap ({int(VOL_CAP_PCT*100)}% of bar volume)</b> rejects signals on
            thin-volume bars where a real position cannot be filled at quoted price.
            Skipped entries are shown explicitly.</li>
        <li><b>Sharpe drop larger than 0.10</b> is a red flag — the strategy's apparent
            edge mostly came from fill assumptions, not from signal quality.</li>
        <li><b>Win rate drop larger than 5pp</b> means many "wins" only existed because
            entry was taken at a better price than actually available.</li>
      </ul>
    </div>

    <div class="verdict-box" style="margin-top:14px">
      <h3>⚙️ Friction parameters used</h3>
      <ul style="color:#ccc;line-height:1.7">
        <li>Commission: {COMMISSION_PCT*100:.2f}% per side ({COMMISSION_PCT*200:.2f}% round-trip)</li>
        <li>Slippage:   {SLIPPAGE_PCT*100:.3f}% per side (adverse price move at fill)</li>
        <li>Volume cap: {int(VOL_CAP_PCT*100)}% of bar volume (at {NOTIONAL:,.0f} USD notional)</li>
        <li>Fill model:  entry = next-bar Open × (1 + slippage); exit = next-bar Open × (1 − slippage)</li>
        <li>Gap exposure: fully absorbed — no protective limit order modelled</li>
      </ul>
    </div>
    """


def generate_html(results: list[dict]) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Backtest Benchmark — Naive vs Friction — {now}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>🧪 Backtest Benchmark: NAIVE vs FRICTION-AWARE</h1>
<div class="meta">
  Generated: {now} &nbsp;·&nbsp;
  Next-bar fill &nbsp;·&nbsp;
  Commission {COMMISSION_PCT*100:.2f}%/side &nbsp;·&nbsp;
  Slippage {SLIPPAGE_PCT*100:.3f}%/side &nbsp;·&nbsp;
  Volume cap {int(VOL_CAP_PCT*100)}%
</div>

<div class="section">
  <h2>📊 Summary: All Symbols</h2>
  {build_summary_html(results)}
</div>

<div class="section">
  {build_verdict_html(results)}
</div>

</body>
</html>"""


# ===========================================================================
# Main
# ===========================================================================

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backtest benchmark: naive vs friction")
    parser.add_argument("--quick", action="store_true",
                        help="Run only 3 symbols for a fast smoke-test")
    args = parser.parse_args(argv)

    stock_list = QUICK_STOCKS if args.quick else STOCKS

    print("=" * 70)
    print("Backtest Benchmark  —  NAIVE  vs  FRICTION-AWARE")
    print(f"Mode: {'QUICK (3 symbols)' if args.quick else f'FULL ({len(stock_list)} entries)'}")
    print(f"Friction: commission={COMMISSION_PCT*100:.2f}%/side  "
          f"slippage={SLIPPAGE_PCT*100:.3f}%/side  vol_cap={int(VOL_CAP_PCT*100)}%")
    print("=" * 70)

    results: list[dict] = []
    seen: set[str] = set()
    for tv_sym, yf_ticker, interval, period in stock_list:
        key = f"{tv_sym}_{interval}"
        if key in seen:
            continue
        seen.add(key)
        r = run_symbol(tv_sym, yf_ticker, interval, period)
        if r:
            results.append(r)

    if not results:
        print("No results — check network / yfinance availability.")
        sys.exit(1)

    # Save HTML
    out_html = ROOT.parent / "reports" / "backtest_benchmark.html"
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(generate_html(results), encoding="utf-8")
    print(f"\n✅  HTML report → {out_html}")

    # ── Archive report ───────────────────────────────────────────────────────
    try:
        import sys as _sys
        _sys.path.insert(0, str(ROOT.parent / "reports"))
        from report_db import archive_report as _arc
        _arc(out_html, "backtest_benchmark")
    except Exception as _e:
        print(f"  [archive] benchmark archive skipped: {_e}")

    # Save JSON (strip trade lists for brevity)
    summary_json = [
        {k: v for k, v in r.items() if k not in ("naive_trades", "friction_trades")}
        for r in results
    ]
    out_json = ROOT.parent / "reports" / "backtest_benchmark.json"
    out_json.write_text(json.dumps(summary_json, indent=2), encoding="utf-8")
    print(f"    JSON summary  → {out_json}")

    # Console summary
    print("\n── Aggregate Results ────────────────────────────────")
    all_n_pnl = [t["pnl_pct"] for r in results for t in r["naive_trades"]
                 if not t.get("skipped") and t.get("exit_reason") != "OPEN"]
    all_f_pnl = [t["pnl_pct"] for r in results for t in r["friction_trades"]
                 if not t.get("skipped") and t.get("exit_reason") != "OPEN"]

    def _print_row(label: str, pnls: list[float]) -> None:
        if not pnls:
            print(f"  {label:<12}  no closed trades")
            return
        wr  = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        tot = sum(pnls)
        sh  = float(np.mean(pnls) / np.std(pnls, ddof=1)) if len(pnls) > 1 else 0
        print(f"  {label:<12}  trades={len(pnls):3d}  wr={wr:5.1f}%  "
              f"total_pnl={tot:+7.1f}%  sharpe={sh:+.3f}")

    _print_row("NAIVE",    all_n_pnl)
    _print_row("FRICTION", all_f_pnl)

    if all_n_pnl and all_f_pnl:
        n_sh = float(np.mean(all_n_pnl) / np.std(all_n_pnl, ddof=1))
        f_sh = float(np.mean(all_f_pnl) / np.std(all_f_pnl, ddof=1))
        drop = n_sh - f_sh
        label = "🔴 SIGNIFICANT" if drop > 0.10 else "🟡 MODERATE" if drop > 0.03 else "🟢 MINIMAL"
        print(f"\n  Sharpe degradation: {drop:+.3f} ({label})")

    print()


if __name__ == "__main__":
    main()

