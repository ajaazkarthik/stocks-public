"""
Backtest simulator for buy_sell_v2.pine strategy.

Mirrors the exact BUY / SELL logic from the Pine Script:
  BUY : MACD>signal, RSI>=60, MA rising, ADX>10, +DI>-DI, +DI>30  (on a new cross / signal edge)
  SELL: ATR Hard Stop  (2×ATR below entry)
        ATR Trail Stop (1.5×ATR below HWM)
        MA Breakdown   (ma_down: MA declining, close<prev_MA)
        MACD Crossunder
        RSI Decay      (RSI<45 AND RSI[0]<RSI[3])

Outputs:  backtest_report.html
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json, warnings, datetime, textwrap
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STOCKS = [
    # (TV_symbol,   yf_ticker,   interval,  period)
    # Note: yfinance max for 1h = 730d, 1d = any period, 2h = 60d (too short for backtesting)
    # Using 1h (1.5 years) for your 2H-fired stocks to stay on short TF, and 1d (5yr) for broad view
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

ATR_STOP_MULT  = 2.0
ATR_TRAIL_MULT = 1.5
RSI_EXIT       = 45
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
RSI_PERIOD     = 14
MA_PERIOD      = 20
ATR_PERIOD     = 14
ADX_PERIOD     = 14

# ---------------------------------------------------------------------------
# Execution realism (friction — applied to all simulations)
# ---------------------------------------------------------------------------
COMMISSION_PCT = 0.001   # 0.10% per side
SLIPPAGE_PCT   = 0.0005  # 0.05% per side (adverse fill at execution)
VOL_CAP_PCT    = 0.05    # reject entry if shares needed > 5% of bar volume
NOTIONAL       = 10_000  # assumed position size in currency units (for vol cap)


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------
def calc_rsi(close: pd.Series, period=14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def calc_macd(close: pd.Series):
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd     = ema_fast - ema_slow
    signal   = macd.ewm(span=MACD_SIG, adjust=False).mean()
    return macd, signal


def calc_atr(high, low, close, period=14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    # Wilder smoothing
    atr = tr.ewm(com=period - 1, min_periods=period).mean()
    return atr


def calc_adx_dmi(high, low, close, period=14):
    """Returns (adx, plus_di, minus_di)"""
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    up_move   = high - prev_high
    down_move = prev_low - low   # "down" is prev_low minus current low

    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm  = pd.Series(plus_dm,  index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr       = calc_atr(high, low, close, period)  # Wilder-smoothed ATR (already /period)
    plus_di  = 100 * plus_dm.ewm(com=period-1, min_periods=period).mean() / tr
    minus_di = 100 * minus_dm.ewm(com=period-1, min_periods=period).mean() / tr

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(com=period-1, min_periods=period).mean()
    return adx, plus_di, minus_di


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------
def backtest(tv_sym, yf_ticker, interval, period):
    print(f"  Fetching {tv_sym} ({yf_ticker}) interval={interval} period={period} …", end=" ", flush=True)
    try:
        df = yf.download(yf_ticker, interval=interval, period=period,
                         auto_adjust=True, progress=False, multi_level_index=False)
    except Exception as e:
        print(f"FAILED: {e}")
        return None

    if df is None or len(df) < 60:
        print(f"insufficient data ({len(df) if df is not None else 0} bars)")
        return None

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.dropna(inplace=True)

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    opens  = df["Open"]
    volume = df["Volume"]

    # --- indicators ---
    rsi              = calc_rsi(close, RSI_PERIOD)
    macd, macd_sig   = calc_macd(close)
    atr              = calc_atr(high, low, close, ATR_PERIOD)
    ma               = close.rolling(MA_PERIOD).mean()
    adx, plus_di, minus_di = calc_adx_dmi(high, low, close, ADX_PERIOD)

    # ma_up: current MA > MA 3 bars ago (slope positive)
    ma_up = ma > ma.shift(3)

    # cond_down (MA breakdown): MA declining AND close < prev MA
    ma_down = (ma < ma.shift(1)) & (close < ma.shift(1))

    # macd above signal
    macd_above = macd > macd_sig
    # macd crossunder (was above, now below)
    macd_crossunder = (macd < macd_sig) & (macd.shift(1) >= macd_sig)
    # macd crossover (was below, now above)
    macd_crossover  = (macd > macd_sig) & (macd.shift(1) <= macd_sig)

    # BUY condition — require fresh edge: triggerBuy = true AND triggerBuy[1] = false
    buy_raw = (
        macd_above &
        (rsi >= 60) &
        ma_up &
        (adx > 10) &
        (plus_di > minus_di) &
        (plus_di > 30)
    )
    buy_edge = buy_raw & ~buy_raw.shift(1).fillna(False).astype(bool)  # rising edge

    # --- state machine ---
    n           = len(df)
    idx         = df.index

    in_trade    = False
    entry_price = np.nan
    hwm         = np.nan
    entry_bar   = -1

    trades      = []

    for i in range(ATR_PERIOD + MACD_SLOW + 5, n):
        cur_rsi   = rsi.iloc[i]
        cur_atr   = atr.iloc[i]
        cur_close = close.iloc[i]
        cur_date  = idx[i]

        if not in_trade:
            if buy_edge.iloc[i]:
                # --- next-bar fill + volume cap ---
                if i + 1 >= n:
                    continue  # no next bar to fill on — skip signal
                fill_bar   = i + 1
                fill_open  = float(opens.iloc[fill_bar])
                fill_vol   = float(volume.iloc[fill_bar])
                shares_needed = NOTIONAL / fill_open if fill_open > 0 else 0
                if fill_vol > 0 and shares_needed > VOL_CAP_PCT * fill_vol:
                    trades.append({
                        "entry_date":  idx[i].strftime("%Y-%m-%d %H:%M"),
                        "exit_date":   "SKIPPED",
                        "entry_price": 0.0,
                        "exit_price":  0.0,
                        "hwm":         0.0,
                        "hold_bars":   0,
                        "pnl_pct":     0.0,
                        "exit_reason": "SKIPPED_VOL_CAP",
                        "win":         False,
                    })
                    continue
                # Entry = next-bar open + adverse slippage
                entry_price = fill_open * (1 + SLIPPAGE_PCT)
                entry_bar   = fill_bar
                in_trade    = True
                hwm         = entry_price
        else:
            # Update HWM
            hwm = max(hwm, cur_close)

            # Evaluate sell conditions (priority order)
            sell_reason = None

            # 1. ATR Hard Stop
            if cur_close < entry_price - ATR_STOP_MULT * cur_atr:
                sell_reason = "ATR Hard Stop"

            # 2. ATR Trailing Stop
            elif cur_close < hwm - ATR_TRAIL_MULT * cur_atr:
                sell_reason = "ATR Trail Stop"

            # 3. MA Breakdown
            elif ma_down.iloc[i] and not ma_down.iloc[i-1]:
                sell_reason = "MA Breakdown"

            # 4. MACD Crossunder
            elif macd_crossunder.iloc[i]:
                sell_reason = "MACD Crossunder"

            # 5. RSI Decay
            elif cur_rsi < RSI_EXIT and (i >= 3) and cur_rsi < rsi.iloc[i-3]:
                sell_reason = "RSI Decay"

            if sell_reason:
                # --- next-bar fill + round-trip commission ---
                if i + 1 < n:
                    exit_fill = float(opens.iloc[i + 1]) * (1 - SLIPPAGE_PCT)
                    exit_date = idx[i + 1].strftime("%Y-%m-%d %H:%M")
                else:
                    exit_fill = cur_close * (1 - SLIPPAGE_PCT)  # last bar
                    exit_date = cur_date.strftime("%Y-%m-%d %H:%M")
                rt_cost   = COMMISSION_PCT * 2 * 100  # round-trip cost in %
                raw_pnl   = (exit_fill - entry_price) / entry_price * 100
                pnl_pct   = raw_pnl - rt_cost
                hold_bars = (i + 1) - entry_bar
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
                })
                in_trade    = False
                entry_price = np.nan
                hwm         = np.nan

    # If still in trade at end, record as open (deduct fees at last close)
    if in_trade:
        last_close = float(close.iloc[-1])
        raw_pnl    = (last_close - entry_price) / entry_price * 100
        pnl_pct    = raw_pnl - COMMISSION_PCT * 2 * 100
        trades.append({
            "entry_date":  idx[entry_bar].strftime("%Y-%m-%d %H:%M"),
            "exit_date":   "OPEN",
            "entry_price": round(float(entry_price), 4),
            "exit_price":  round(float(last_close), 4),
            "hwm":         round(float(hwm), 4),
            "hold_bars":   n - 1 - entry_bar,
            "pnl_pct":     round(float(pnl_pct), 2),
            "exit_reason": "OPEN",
            "win":         pnl_pct > 0,
        })

    print(f"OK  {len(df)} bars → {len(trades)} trades")

    if not trades:
        return None

    closed  = [t for t in trades if t["exit_reason"] not in ("OPEN", "SKIPPED_VOL_CAP")]
    skipped = [t for t in trades if t["exit_reason"] == "SKIPPED_VOL_CAP"]
    wins    = [t for t in closed if t["win"]]
    losses  = [t for t in closed if not t["win"]]
    win_rate     = len(wins) / len(closed) * 100 if closed else 0
    avg_win      = np.mean([t["pnl_pct"] for t in wins])    if wins   else 0
    avg_loss     = np.mean([t["pnl_pct"] for t in losses])  if losses else 0
    avg_hold     = np.mean([t["hold_bars"] for t in closed]) if closed else 0
    total_pnl    = sum(t["pnl_pct"] for t in closed)
    reason_count = {}
    for t in closed:
        reason_count[t["exit_reason"]] = reason_count.get(t["exit_reason"], 0) + 1

    # Most price was left on table (HWM vs exit price) for winners
    left_on_table = []
    for t in wins:
        left_pct = (t["hwm"] - t["exit_price"]) / t["entry_price"] * 100
        left_on_table.append(left_pct)
    avg_left = float(np.mean(left_on_table)) if left_on_table else 0.0

    pnls   = [t["pnl_pct"] for t in closed]
    if len(pnls) > 1:
        _std = float(np.std(pnls, ddof=1))
        sharpe = round(float(np.mean(pnls)) / _std, 3) if _std > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "tv_sym":      tv_sym,
        "yf_ticker":   yf_ticker,
        "interval":    interval,
        "total_bars":  n,
        "trades":      trades,
        "num_trades":  len(trades),
        "num_closed":  len(closed),
        "num_wins":    len(wins),
        "num_losses":  len(losses),
        "win_rate":    round(win_rate, 1),
        "avg_win":     round(float(avg_win), 2),
        "avg_loss":    round(float(avg_loss), 2),
        "avg_hold":    round(float(avg_hold), 1),
        "total_pnl":         round(float(total_pnl), 2),
        "sharpe":            sharpe,
        "reason_count":      reason_count,
        "avg_left_on_table": round(avg_left, 2),
        "num_skipped":       len(skipped),
        "commission_pct_rt": COMMISSION_PCT * 2 * 100,
        "slippage_pct_rt":   SLIPPAGE_PCT * 2 * 100,
    }


# ---------------------------------------------------------------------------
# HTML report generator
# ---------------------------------------------------------------------------
COLORS = {
    "ATR Hard Stop":  "#e74c3c",
    "ATR Trail Stop": "#e67e22",
    "MA Breakdown":   "#3498db",
    "MACD Crossunder":"#9b59b6",
    "RSI Decay":      "#1abc9c",
    "OPEN":           "#95a5a6",
}

def reason_badge(reason):
    c = COLORS.get(reason, "#7f8c8d")
    return f'<span style="background:{c};color:#fff;padding:2px 7px;border-radius:4px;font-size:0.78em">{reason}</span>'

def pnl_cell(pnl):
    if pnl > 0:
        return f'<td style="color:#2ecc71;font-weight:700">+{pnl:.2f}%</td>'
    elif pnl < 0:
        return f'<td style="color:#e74c3c;font-weight:700">{pnl:.2f}%</td>'
    else:
        return f'<td>0.00%</td>'

def build_reason_bar(reason_count):
    total = sum(reason_count.values())
    if total == 0:
        return ""
    html = '<div style="display:flex;gap:4px;flex-wrap:wrap;margin:6px 0">'
    for r, cnt in sorted(reason_count.items(), key=lambda x: -x[1]):
        pct = cnt / total * 100
        c   = COLORS.get(r, "#7f8c8d")
        html += (f'<div style="background:{c};color:#fff;padding:3px 10px;'
                 f'border-radius:12px;font-size:0.8em">'
                 f'{r}: {cnt} ({pct:.0f}%)</div>')
    html += "</div>"
    return html

def build_summary_table(results):
    rows = ""
    for r in results:
        wr_col = ("#2ecc71" if r["win_rate"] >= 60 else
                  "#e67e22" if r["win_rate"] >= 45 else "#e74c3c")
        pnl_col = "#2ecc71" if r["total_pnl"] >= 0 else "#e74c3c"
        reason_str = ", ".join(f"{k}:{v}" for k, v in
                               sorted(r["reason_count"].items(), key=lambda x: -x[1]))
        rows += f"""
        <tr>
          <td><b>{r['tv_sym']}</b><br><small style="color:#aaa">{r['yf_ticker']} {r['interval']}</small></td>
          <td>{r['num_closed']}</td>
          <td style="color:{wr_col};font-weight:700">{r['win_rate']}%</td>
          <td style="color:#2ecc71">+{r['avg_win']}%</td>
          <td style="color:#e74c3c">{r['avg_loss']}%</td>
          <td style="color:{pnl_col};font-weight:700">{r['total_pnl']:+.1f}%</td>
          <td>{r['avg_hold']:.0f} bars</td>
          <td style="color:#e67e22">{r['avg_left_on_table']:+.2f}%</td>
          <td style="color:#888">{r.get('num_skipped', 0)}</td>
          <td><small>{reason_str}</small></td>
        </tr>"""
    return f"""
    <table class="tbl">
      <thead><tr>
        <th>Symbol</th><th>Trades</th><th>Win%</th>
        <th>Avg Win</th><th>Avg Loss</th><th>Total P&L</th>
        <th>Avg Hold</th><th>Left on Table¹</th><th>Skipped</th><th>Exit Reasons</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="color:#888;font-size:0.82em">¹ HWM reached vs price at exit (avg for winning trades). 
       Positive = sell fired before peak. Negative = sold above eventual retracement (good!).</p>
    """

def build_trades_section(r):
    rows = ""
    for t in sorted([x for x in r["trades"] if x["exit_reason"] != "SKIPPED_VOL_CAP"],
                    key=lambda x: x["entry_date"], reverse=True):
        win_bg = "rgba(46,204,113,0.05)" if t["win"] else "rgba(231,76,60,0.05)"
        rows += f"""
        <tr style="background:{win_bg}">
          <td>{t['entry_date']}</td>
          <td>{t['exit_date']}</td>
          <td>{t['entry_price']}</td>
          <td>{t['exit_price']}</td>
          <td style="color:#3498db">{t['hwm']}</td>
          {pnl_cell(t['pnl_pct'])}
          <td>{reason_badge(t['exit_reason'])}</td>
          <td>{t['hold_bars']}</td>
        </tr>"""
    return f"""
    <div class="section">
      <h2>{r['tv_sym']} <small style="color:#aaa;font-weight:400">{r['yf_ticker']} · {r['interval']}</small></h2>
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-num" style="color:{'#2ecc71' if r['win_rate']>=60 else '#e67e22'}">{r['win_rate']}%</div>
          <div class="stat-lbl">Win Rate</div>
        </div>
        <div class="stat-card">
          <div class="stat-num">{r['num_closed']}</div>
          <div class="stat-lbl">Closed Trades</div>
        </div>
        <div class="stat-card">
          <div class="stat-num" style="color:#2ecc71">+{r['avg_win']}%</div>
          <div class="stat-lbl">Avg Win</div>
        </div>
        <div class="stat-card">
          <div class="stat-num" style="color:#e74c3c">{r['avg_loss']}%</div>
          <div class="stat-lbl">Avg Loss</div>
        </div>
        <div class="stat-card">
          <div class="stat-num" style="color:{'#2ecc71' if r['total_pnl']>=0 else '#e74c3c'}">{r['total_pnl']:+.1f}%</div>
          <div class="stat-lbl">Total P&L</div>
        </div>
        <div class="stat-card">
          <div class="stat-num" style="color:#e67e22">{r['avg_left_on_table']:+.2f}%</div>
          <div class="stat-lbl">Left on Table¹</div>
        </div>
      </div>
      <div><b>Exit Reason Distribution:</b> {build_reason_bar(r['reason_count'])}</div>
      <table class="tbl" style="margin-top:12px">
        <thead><tr>
          <th>Entry Date</th><th>Exit Date</th>
          <th>Entry</th><th>Exit</th><th>HWM</th>
          <th>P&L %</th><th>Exit Reason</th><th>Hold Bars</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""

def build_insights(results):
    all_closed = [t for r in results for t in r["trades"]
                  if t["exit_reason"] not in ("OPEN", "SKIPPED_VOL_CAP")]
    total = len(all_closed)
    if not total:
        return ""

    rc_all = {}
    for t in all_closed:
        rc_all[t["exit_reason"]] = rc_all.get(t["exit_reason"], 0) + 1

    # Win rates by exit reason
    wr_by_reason = {}
    for reason in rc_all:
        subset = [t for t in all_closed if t["exit_reason"] == reason]
        wins   = [t for t in subset if t["win"]]
        wr_by_reason[reason] = {
            "count": len(subset),
            "win_rate": len(wins)/len(subset)*100 if subset else 0,
            "avg_pnl":  float(np.mean([t["pnl_pct"] for t in subset])) if subset else 0,
        }

    rows = ""
    for r, d in sorted(wr_by_reason.items(), key=lambda x: -x[1]["count"]):
        c = COLORS.get(r, "#7f8c8d")
        wr_col = "#2ecc71" if d["win_rate"] >= 60 else "#e67e22" if d["win_rate"] >= 40 else "#e74c3c"
        pnl    = d["avg_pnl"]
        pnl_col= "#2ecc71" if pnl >= 0 else "#e74c3c"
        rows += f"""<tr>
          <td>{reason_badge(r)}</td>
          <td>{d['count']} ({d['count']/total*100:.0f}%)</td>
          <td style="color:{wr_col};font-weight:700">{d['win_rate']:.1f}%</td>
          <td style="color:{pnl_col};font-weight:700">{pnl:+.2f}%</td>
        </tr>"""

    # Identify improvement opportunities
    insights = []
    
    # 1. Which condition exits with most profit left on table?
    by_reason_left = {}
    for r in results:
        for t in r["trades"]:
            if t["exit_reason"] == "OPEN":
                continue
            left_pct = (t["hwm"] - t["exit_price"]) / max(t["entry_price"], 0.001) * 100
            if t["exit_reason"] not in by_reason_left:
                by_reason_left[t["exit_reason"]] = []
            by_reason_left[t["exit_reason"]].append(left_pct)

    worst_reason = max(by_reason_left.items(), key=lambda x: np.mean(x[1]), default=(None, [0]))
    if worst_reason[0]:
        insights.append(f"<b>🔍 Early Exit:</b> <b>{worst_reason[0]}</b> leaves most profit on table "
                        f"(avg {float(np.mean(worst_reason[1])):.2f}% below HWM). "
                        f"Consider relaxing its trigger or making it conditional on volume drop.")

    # 2. ATR trail - is 1.5x optimal?
    atr_trail_trades = [t for t in all_closed if t["exit_reason"] == "ATR Trail Stop"]
    if atr_trail_trades:
        wins_at  = [t for t in atr_trail_trades if t["win"]]
        loss_at  = [t for t in atr_trail_trades if not t["win"]]
        insights.append(f"<b>📉 ATR Trail Stop fires {len(atr_trail_trades)}x</b>: "
                        f"{len(wins_at)} wins / {len(loss_at)} losses. "
                        f"{'It tends to cut winners — try 2.0× for more room.' if len(wins_at) > len(loss_at) else 'Mostly protecting from losses — 1.5× seems good.'}")

    # 3. RSI Decay check
    rsi_trades = [t for t in all_closed if t["exit_reason"] == "RSI Decay"]
    if rsi_trades:
        rsi_wins     = [t for t in rsi_trades if t["win"]]
        rsi_avg_pnl  = float(np.mean([t["pnl_pct"] for t in rsi_trades]))
        insights.append(f"<b>📛 RSI Decay ({len(rsi_trades)} exits)</b>: "
                        f"Win% {len(rsi_wins)/len(rsi_trades)*100:.0f}%, avg P&L {rsi_avg_pnl:+.1f}%. "
                        f"{'Strong signal — keep.' if len(rsi_wins)/len(rsi_trades) > 0.5 else 'Low win rate — consider requiring RSI<42 for 2 bars before firing.'}")

    # 4. MACD Crossunder
    macd_trades = [t for t in all_closed if t["exit_reason"] == "MACD Crossunder"]
    if macd_trades:
        macd_wins    = [t for t in macd_trades if t["win"]]
        macd_avg_pnl = float(np.mean([t["pnl_pct"] for t in macd_trades]))
        insights.append(f"<b>⚡ MACD Crossunder ({len(macd_trades)} exits)</b>: "
                        f"Win% {len(macd_wins)/len(macd_trades)*100:.0f}%, avg P&L {macd_avg_pnl:+.1f}%. "
                        f"{'Effective at capturing gains early.' if len(macd_wins)/len(macd_trades) > 0.5 else 'Often exits prematurely. Consider filtering: require RSI<55 too.'}")

    insights_html = "".join(f"<li>{i}</li>" for i in insights)

    return f"""
    <div class="section">
      <h2>📊 Cross-Symbol Insights</h2>

      <h3>Exit Reason Performance (all symbols combined)</h3>
      <table class="tbl">
        <thead><tr><th>Exit Reason</th><th>Count (%)</th><th>Win Rate</th><th>Avg P&L</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>

      <h3>🧠 Key Observations & Recommendations</h3>
      <ul style="line-height:1.9">{insights_html}</ul>

      <h3>💡 Sell Strategy Improvement Ideas</h3>
      <ol style="line-height:1.9">
        <li><b>Volume-Confirmed ATR Trail</b>: Only trigger ATR trail if volume is also below 70% of its 20-bar MA — weak volume during pullbacks may be noise.</li>
        <li><b>Profit-Locked Trail</b>: Once trade is +10%, widen trail to 2.5×ATR to let big winners run.</li>
        <li><b>RSI Decay Guard</b>: Require RSI &lt; {RSI_EXIT} for 2 consecutive bars (not just current) to reduce false decay exits.</li>
        <li><b>MACD Cross + Confirm</b>: Add a rule that MACD-crossunder sell only fires if histogram is negative AND declining — avoids whipsaws.</li>
        <li><b>Earnings Shield</b>: Suppress all sell signals in the 3 bars before an earnings announcement (earnings surprise can reset the signal stack).</li>
        <li><b>Regime Filter</b>: During VIX &gt; 28 regime, tighten ATR trail to 1.0× (capital protection mode).</li>
      </ol>
    </div>
    """


def generate_html(results):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    summary  = build_summary_table(results)
    insights = build_insights(results)
    sections = "".join(build_trades_section(r) for r in results)

    config_html = f"""
    <div class="section">
      <h2>⚙️ Strategy Config Used</h2>
      <table class="tbl">
        <thead><tr><th>Parameter</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>Buy: RSI threshold</td><td>≥ 60</td></tr>
          <tr><td>Buy: ADX threshold</td><td>&gt; 10</td></tr>
          <tr><td>Buy: +DI threshold</td><td>&gt; 30 AND &gt; −DI</td></tr>
          <tr><td>Buy: MA period</td><td>SMA {MA_PERIOD} (ma_up = MA &gt; MA[3])</td></tr>
          <tr><td>Buy: MACD</td><td>({MACD_FAST},{MACD_SLOW},{MACD_SIG}) — MACD &gt; signal</td></tr>
          <tr><td>ATR period</td><td>{ATR_PERIOD}</td></tr>
          <tr><td>ATR Hard Stop mult</td><td>{ATR_STOP_MULT}×</td></tr>
          <tr><td>ATR Trail Stop mult</td><td>{ATR_TRAIL_MULT}×</td></tr>
          <tr><td>RSI Exit Level</td><td>&lt; {RSI_EXIT} AND declining</td></tr>
          <tr><td>MA Breakdown</td><td>MA declining AND close &lt; prev MA</td></tr>
          <tr><td>MACD Crossunder</td><td>MACD crosses below signal</td></tr>
        </tbody>
      </table>
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BuySell v2 Backtest — {now}</title>
<style>
  body{{margin:0;padding:0;background:#0e1117;color:#ddd;font-family:'Segoe UI',sans-serif;font-size:14px}}
  h1{{margin:0;padding:24px 32px;background:linear-gradient(90deg,#1a2035,#23304a);font-size:1.6em;border-bottom:1px solid #2a3550}}
  h2{{color:#7ec8e3;margin:0 0 14px 0;font-size:1.15em}}
  h3{{color:#a8d8ea;margin:18px 0 8px}}
  .section{{padding:22px 32px;border-bottom:1px solid #1e2d42}}
  .stats-row{{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}}
  .stat-card{{background:#141c2e;border:1px solid #2a3550;border-radius:8px;padding:12px 20px;min-width:100px;text-align:center}}
  .stat-num{{font-size:1.6em;font-weight:700;margin-bottom:4px}}
  .stat-lbl{{color:#778;font-size:0.78em;text-transform:uppercase;letter-spacing:0.05em}}
  .tbl{{width:100%;border-collapse:collapse;font-size:0.87em}}
  .tbl th{{background:#141c2e;color:#7ec8e3;padding:8px 10px;text-align:left;position:sticky;top:0;white-space:nowrap}}
  .tbl td{{padding:6px 10px;border-bottom:1px solid #1c2a3a}}
  .tbl tr:hover td{{background:rgba(126,200,227,0.04)}}
  li,.tbl td{{line-height:1.6}}
  .header-meta{{color:#6b7b9a;font-size:0.85em;padding:0 32px 8px;background:linear-gradient(90deg,#1a2035,#23304a)}}
</style>
</head>
<body>
<h1>🧪 BuySell v2 Backtest Report</h1>
<div class="header-meta">Generated: {now} · Parameters: ATR Stop {ATR_STOP_MULT}× · ATR Trail {ATR_TRAIL_MULT}× · RSI Exit &lt;{RSI_EXIT} · <b>Friction: commission {COMMISSION_PCT*100:.2f}%/side · slippage {SLIPPAGE_PCT*100:.3f}%/side · vol cap {int(VOL_CAP_PCT*100)}%</b> · Fill: next-bar open</div>

<div class="section">
  <h2>📋 Summary — All Symbols</h2>
  {summary}
</div>

{insights}
{config_html}
{sections}
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("BuySell v2 Backtest")
    print("=" * 60)

    results = []
    seen    = set()
    for tv_sym, yf_ticker, interval, period in STOCKS:
        key = f"{tv_sym}_{interval}"
        if key in seen:
            continue
        seen.add(key)
        r = backtest(tv_sym, yf_ticker, interval, period)
        if r:
            results.append(r)

    if not results:
        print("No results — check data availability.")
    else:
        html = generate_html(results)
        out  = ROOT.parent / "reports" / "backtest_report.html"
        out.write_text(html, encoding="utf-8")
        print(f"\n✅  Report saved → {out}")
        print(f"    {len(results)} symbols · {sum(r['num_closed'] for r in results)} closed trades total")

        # Also dump JSON for further analysis
        json_out = ROOT / "backtest_results.json"
        json_out.write_text(json.dumps(results, indent=2, default=lambda o: bool(o) if hasattr(o, 'item') else str(o)), encoding="utf-8")
        print(f"    JSON data → {json_out}")

