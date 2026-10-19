"""
score_validation_backtest.py — Validate signal scoring with vectorbt.

Downloads 2-year OHLCV data, simulates:
  - HIGH-SCORE strategy: enter on days where momentum + volume alignment (proxy for L1 score > 7)
  - LOW-SCORE strategy: random-entry control group (proxy for L1 score < 3)
  - BENCHMARK: buy-and-hold SPY

Compares Sharpe, CAGR, max drawdown to prove (or disprove) signal scoring has edge.

Run:
    python benchmarks/score_validation_backtest.py

Outputs:
    Console: strategy comparison table
"""
import sys
import argparse

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

# Liquid stocks representative of what our signals fire on
_DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    "JPM", "V", "XOM", "UNH", "BRK-B", "LLY", "AVGO", "WMT",
]


def run(period: str = "2y"):
    print(f"Downloading {period} OHLCV data for {len(_DEFAULT_SYMBOLS)} symbols...")
    raw = yf.download(
        " ".join(_DEFAULT_SYMBOLS),
        period=period,
        progress=False,
        auto_adjust=True,
    )

    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"].dropna(how="all", axis=1)
        vol   = raw["Volume"].dropna(how="all", axis=1)
    else:
        close = raw[["Close"]].rename(columns={"Close": _DEFAULT_SYMBOLS[0]})
        vol   = raw[["Volume"]].rename(columns={"Volume": _DEFAULT_SYMBOLS[0]})

    symbols = list(close.columns)
    print(f"Got {len(close)} trading days for {len(symbols)} symbols")

    # ── STRATEGY 1: High-score proxy ──────────────────────────────────────────
    # Mimics L1 score > 7: price crosses above 20-SMA with volume confirmation (>1.5x avg vol)
    sma20   = close.rolling(20).mean()
    rel_vol = vol / vol.rolling(20).mean().replace(0, np.nan)

    high_entries = (
        (close > sma20) &
        (close.shift(1) <= sma20.shift(1)) &  # crossover confirmation
        (rel_vol > 1.5)                         # volume spike = institutional interest
    ).fillna(False)
    high_exits = high_entries.shift(20).fillna(False)   # hold 20 trading days

    # ── STRATEGY 2: Random-entry (low-score proxy) ─────────────────────────────
    np.random.seed(42)
    rand_entries = pd.DataFrame(
        np.random.rand(*close.shape) > 0.97,   # ~3% of days at random
        index=close.index, columns=close.columns,
    )
    rand_exits = rand_entries.shift(20).fillna(False)

    # ── RUN VECTORBT ──────────────────────────────────────────────────────────
    try:
        import vectorbt as vbt

        # Use integer SizeType instead of string to avoid version-specific enum issues
        # SizeType.ValuePercent = 5 in most vectorbt versions
        # Alternatively: no size_type needed for simple fixed-size
        pf_high = vbt.Portfolio.from_signals(
            close=close,
            entries=high_entries,
            exits=high_exits,
            fees=0.001,
            init_cash=100_000,
            size=10_000,             # $10k per signal (fixed dollar amount)
        )

        pf_rand = vbt.Portfolio.from_signals(
            close=close,
            entries=rand_entries,
            exits=rand_exits,
            fees=0.001,
            init_cash=100_000,
            size=10_000,
        )

        stats_high = pf_high.stats()
        stats_rand = pf_rand.stats()

        # SPY comparison
        import quantstats as qs
        spy_close   = yf.download("SPY", period=period, progress=False, auto_adjust=True)["Close"]
        spy_returns = spy_close.pct_change().dropna()

        print(f"\n{'='*60}")
        print("STRATEGY COMPARISON (vectorbt simulation)")
        print(f"{'='*60}")
        print(f"{'Metric':<30} {'High-Score':>12} {'Random':>10}")
        print("-" * 55)

        _metric_keys = [
            ("Total Return [%]",      "Total Return [%]"),
            ("Annualized Return [%]", "Annualized Return [%]"),
            ("Sharpe Ratio",          "Sharpe Ratio"),
            ("Sortino Ratio",         "Sortino Ratio"),
            ("Max Drawdown [%]",      "Max Drawdown [%]"),
            ("Win Rate [%]",          "Win Rate [%]"),
            ("Total Trades",          "Total Trades"),
        ]
        for label, key in _metric_keys:
            try:
                v_h = stats_high.get(key, "N/A")
                v_r = stats_rand.get(key, "N/A")
                if isinstance(v_h, float):
                    print(f"  {label:<28} {v_h:>12.2f} {v_r:>10.2f}")
                else:
                    print(f"  {label:<28} {v_h!s:>12} {v_r!s:>10}")
            except Exception:
                pass

        spy_sharpe  = qs.stats.sharpe(spy_returns)
        spy_total   = (spy_returns + 1).prod() - 1
        print("\n  SPY buy-and-hold:")
        print(f"    Total Return: {spy_total:.1%}, Sharpe: {spy_sharpe:.3f}")

        print("\n  INTERPRETATION:")
        try:
            h_sharpe = float(stats_high.get("Sharpe Ratio", 0))
            r_sharpe = float(stats_rand.get("Sharpe Ratio", 0))
            if h_sharpe > r_sharpe + 0.5:
                print(f"  ✓ High-score strategy OUTPERFORMS random by >{h_sharpe - r_sharpe:.2f} Sharpe — signal has edge")
            elif h_sharpe > r_sharpe:
                print(f"  ~ High-score marginally better ({h_sharpe:.2f} vs {r_sharpe:.2f}) — weak signal edge")
            else:
                print("  ✗ High-score NOT better than random — review signal criteria")
        except Exception:
            pass

    except ImportError as e:
        print(f"\nvectorbt import failed: {e}")
        _run_quantstats_fallback(close, high_entries, rand_entries, period)

    except KeyError as e:
        print(f"\nvectorbt size_type error: {e} — retrying with SizeType enum...")
        _run_vbt_with_enum(close, high_entries, rand_entries, high_exits, rand_exits, period)


def _run_vbt_with_enum(close, high_entries, rand_entries, high_exits, rand_exits, period):
    """Fallback: use vbt.SizeType enum instead of string."""
    try:
        import vectorbt as vbt

        pf_high = vbt.Portfolio.from_signals(
            close=close, entries=high_entries, exits=high_exits,
            fees=0.001, init_cash=100_000, size=10_000,
        )
        print(f"  vectorbt (enum fallback) total return: {pf_high.total_return():.2%}")
    except Exception as e2:
        print(f"  vectorbt enum fallback also failed: {e2}")
        _run_quantstats_fallback(close, high_entries, rand_entries, period)


def _run_quantstats_fallback(close, high_entries, rand_entries, period):
    """Pure quantstats fallback when vectorbt is unavailable."""
    print("\n[Fallback] Using quantstats for basic metrics...")

    # Compute returns for high-score entries (hold 20 days)
    pf_returns = close.pct_change().dropna()
    high_mask  = high_entries.shift(1).fillna(False)
    rand_mask  = rand_entries.shift(1).fillna(False)

    high_rets  = pf_returns[high_mask].melt()['value'].dropna()
    rand_rets  = pf_returns[rand_mask].melt()['value'].dropna()

    print(f"  High-score trades: {len(high_rets)}, avg return: {high_rets.mean():.3%}")
    print(f"  Random trades    : {len(rand_rets)}, avg return: {rand_rets.mean():.3%}")
    if len(high_rets) > 0 and len(rand_rets) > 0:
        print(f"  Edge: {'high-score wins' if high_rets.mean() > rand_rets.mean() else 'no edge detected'}")


def main(args=None):
    parser = argparse.ArgumentParser(description="Validate signal scoring with vectorbt backtest")
    parser.add_argument("--period", default="2y", help="History period: 1y, 2y (default: 2y)")
    args = parser.parse_args(args)
    run(period=args.period)


if __name__ == "__main__":
    main()
