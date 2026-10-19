"""
BENCHMARK 06 — vectorbt: backtest signal score performance
===========================================================
BEFORE : No backtesting — we score signals but never validate historical returns
AFTER  : vectorbt tests if high-score events beat low-score events historically

Measures:
  - Portfolio returns for a sample of "high signal" stocks
  - Sharpe ratio, max drawdown, CAGR
  - Comparison: buy-and-hold SPY vs signal-timed entries
"""
import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIVIDER = "=" * 70

print(DIVIDER)
print("BENCHMARK 06 — vectorbt: historical signal validation")
print(DIVIDER)

# ── BEFORE ────────────────────────────────────────────────────────────────────
print("\n[BEFORE] Current state: signals fired but never backtested")
print("  - L1-L4 composite score fired when conditions align")
print("  - No historical test: 'do score>7 signals outperform score<3?'")
print("  - Trader relies on intuition about signal quality")

# ── Write vectorbt backtest script ─────────────────────────────────────────────
vbt_script = ROOT / "benchmarks/score_validation_backtest.py"
script_content = '''\
"""
benchmarks/score_validation_backtest.py — Validate signal scoring with vectorbt.

Downloads 1-year OHLCV data, simulates:
  - HIGH-SCORE strategy: enter on days where momentum/RSI conditions align (proxy for L1 score > 7)
  - LOW-SCORE strategy: random-entry control group (proxy for L1 score < 3)
  - BENCHMARK: simple buy-and-hold SPY

Compares Sharpe, CAGR, max drawdown to prove (or disprove) signal value.

Usage:
    python benchmarks/score_validation_backtest.py
"""
import sys
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

# Sample of liquid stocks similar to what our signals fire on
SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
           "JPM", "V", "XOM", "UNH", "BRK-B", "LLY", "AVGO", "WMT"]

print("Downloading 2-year OHLCV data...")
raw = yf.download(" ".join(SYMBOLS), period="2y", progress=False, auto_adjust=True)

if isinstance(raw.columns, pd.MultiIndex):
    close = raw["Close"].dropna(how="all", axis=1)
    high  = raw["High"].dropna(how="all", axis=1)
    low   = raw["Low"].dropna(how="all", axis=1)
    vol   = raw["Volume"].dropna(how="all", axis=1)
else:
    close = raw[["Close"]].rename(columns={"Close": SYMBOLS[0]})
    high  = raw[["High"]].rename(columns={"High": SYMBOLS[0]})
    low   = raw[["Low"]].rename(columns={"Low": SYMBOLS[0]})
    vol   = raw[["Volume"]].rename(columns={"Volume": SYMBOLS[0]})

# Available symbols after download
symbols = list(close.columns)
print(f"Got data for {len(symbols)} symbols, {len(close)} trading days")

try:
    import vectorbt as vbt

    # ── STRATEGY 1: High-score proxy (strong momentum + volume spike) ──────────
    # Proxy for L1 score > 7:  20-day SMA cross + relative volume > 1.5
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    rel_vol = vol / vol.rolling(20).mean()

    # Entry: price crosses above 20-SMA AND relative volume > 1.5 (volume confirmation)
    # This mimics what our signal stack's L1 technical score rewards
    high_score_entries = (
        (close > sma20) &
        (close.shift(1) <= sma20.shift(1)) &  # crossover
        (rel_vol > 1.5)                          # volume confirmation
    ).fillna(False)

    # Exit after 20 trading days (hold period)
    high_score_exits = high_score_entries.shift(20).fillna(False)

    # ── STRATEGY 2: Low-score proxy (no momentum alignment) ────────────────────
    # Proxy for L1 score < 3: price below SMA50 (downtrend)
    np.random.seed(42)
    random_entries = pd.DataFrame(
        np.random.rand(*close.shape) > 0.97,  # ~3% of days
        index=close.index, columns=close.columns
    )
    random_exits = random_entries.shift(20).fillna(False)

    # ── RUN VECTORBT PORTFOLIOS ──────────────────────────────────────────────
    print("\\nRunning vectorbt portfolio simulations...")

    pf_high = vbt.Portfolio.from_signals(
        close=close,
        entries=high_score_entries,
        exits=high_score_exits,
        fees=0.001,       # 0.1% commission
        init_cash=100_000,
        size=0.1,         # 10% of portfolio per signal
        size_type="value_percent",
    )

    pf_random = vbt.Portfolio.from_signals(
        close=close,
        entries=random_entries,
        exits=random_exits,
        fees=0.001,
        init_cash=100_000,
        size=0.1,
        size_type="value_percent",
    )

    # SPY benchmark
    spy_close = yf.download("SPY", period="2y", progress=False, auto_adjust=True)["Close"]
    spy_returns = spy_close.pct_change().dropna()

    # ── RESULTS ──────────────────────────────────────────────────────────────
    stats_high   = pf_high.stats()
    stats_random = pf_random.stats()

    print("\\n=== STRATEGY COMPARISON ===")
    print(f"{'Metric':<30} {'High-Score':>12} {'Random-Entry':>12}")
    print("-" * 56)

    metrics = [
        ("Total Return [%]",   "Total Return [%]"),
        ("Annualized Return [%]", "Annualized Return [%]"),
        ("Sharpe Ratio",       "Sharpe Ratio"),
        ("Sortino Ratio",      "Sortino Ratio"),
        ("Max Drawdown [%]",   "Max Drawdown [%]"),
        ("Win Rate [%]",       "Win Rate [%]"),
        ("Avg Winning Trade [%]", "Avg Winning Trade [%]"),
        ("Avg Losing Trade [%]", "Avg Losing Trade [%]"),
        ("Total Trades",       "Total Trades"),
    ]

    for label, key in metrics:
        try:
            v_high   = stats_high.get(key, "N/A")
            v_random = stats_random.get(key, "N/A")
            if isinstance(v_high, float):
                print(f"  {label:<28} {v_high:>12.2f} {v_random:>12.2f}")
            else:
                print(f"  {label:<28} {str(v_high):>12} {str(v_random):>12}")
        except Exception:
            pass

    # SPY comparison
    import quantstats as qs
    spy_total_return = (spy_returns + 1).prod() - 1
    spy_sharpe = qs.stats.sharpe(spy_returns)
    print(f"\\n  SPY Benchmark:")
    print(f"    Total Return: {spy_total_return:.1%}")
    print(f"    Sharpe Ratio: {spy_sharpe:.3f}")

    print("\\n✓ vectorbt simulation complete")

except ImportError as e:
    print(f"\\nvectorbt import failed: {e}")
    print("Install: pip install vectorbt")
    print("Note: vectorbt requires numba (may take a few minutes to JIT compile)")

    # Fallback: show what WOULD be computed
    import quantstats as qs
    print("\\n[Fallback] Using quantstats for basic validation...")
    close_single = close.iloc[:, 0]
    returns = close_single.pct_change().dropna()
    sharpe = qs.stats.sharpe(returns)
    max_dd = qs.stats.max_drawdown(returns)
    cagr   = qs.stats.cagr(returns)
    print(f"  {symbols[0]} metrics (sample):")
    print(f"  Sharpe: {sharpe:.3f}, Max DD: {max_dd:.1%}, CAGR: {cagr:.1%}")
'''

vbt_script.write_text(script_content, encoding="utf-8")
print(f"[CONFIG] Written {vbt_script}")

# ── AFTER : run vectorbt backtest ─────────────────────────────────────────────
print("\n[AFTER]  Running vectorbt backtest (may take 30-60s for JIT compile)...")
t0 = time.perf_counter()
result = subprocess.run(
    [sys.executable, str(vbt_script)],
    capture_output=True, text=True, cwd=ROOT, timeout=300
)
t_vbt = time.perf_counter() - t0

out = result.stdout + result.stderr
print(out[:4000])
print(f"\n  Run time: {t_vbt:.1f}s")

# ── RESULT SUMMARY ────────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("BENCHMARK RESULT SUMMARY")
print(DIVIDER)
print(f"  BEFORE: No way to validate if signal criteria predict returns")
print(f"  AFTER:  vectorbt/quantstats comparison runs in {t_vbt:.1f}s")
print(f"")
print(f"  WHAT YOU GET:")
print(f"  - High-score proxy (momentum + volume spike) vs random entries")
print(f"  - Sharpe ratio comparison: signal-timed vs untimed")
print(f"  - Max drawdown with vs without signal filtering")
print(f"  - If Sharpe(high-score) > Sharpe(random): scoring has edge")
print(f"  - If they're equal: scoring criteria need rethinking")
print(f"")
print(f"  BUSINESS VALUE: Data-driven evidence that the scoring system works")

# ── SAVE PATCH ────────────────────────────────────────────────────────────────
patches_dir = Path(__file__).parent / "patches"
patches_dir.mkdir(exist_ok=True)
patch_file = patches_dir / "06_vectorbt.patch"
patch_file.write_text(f"# New file: benchmarks/score_validation_backtest.py\n{script_content}", encoding="utf-8")
print(f"\n[PATCH]  Saved to {patch_file}")

# ── REVERT ────────────────────────────────────────────────────────────────────
vbt_script.unlink(missing_ok=True)
print(f"[REVERT] Removed benchmarks/score_validation_backtest.py — clean state restored")
print(f"\n{'='*70}")
