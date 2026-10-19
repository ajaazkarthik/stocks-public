"""
signal_backtest.py — Portfolio return analytics for signal validation.

Downloads 1-year historical data for a configurable set of symbols,
computes equal-weight portfolio returns, and generates a full HTML
tearsheet using quantstats.

Run standalone:
    python benchmarks/signal_backtest.py
    python benchmarks/signal_backtest.py --symbols AAPL,MSFT,NVDA --period 2y
    python benchmarks/signal_backtest.py --out reports/my_backtest.html

Outputs:
    reports/signal_backtest.html   — interactive HTML tearsheet
    Console metrics: Sharpe, Sortino, Max Drawdown, CAGR, Win Rate, Kelly

Tearsheet sections:
  - Cumulative returns vs SPY benchmark
  - Monthly return heatmap
  - Rolling Sharpe / Sortino / Beta
  - Drawdown chart with recovery timeline
  - Win/loss distribution histogram
  - Kelly criterion for optimal position sizing
"""
import sys
import argparse
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import warnings
warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM",
    "V", "XOM", "UNH", "AVGO", "LLY", "WMT", "MA",
]

_FIRES_PATH = ROOT / "tradingview" / "tv_data" / "fires_raw.json"


def _load_symbols() -> list[str]:
    """Load fired symbols from fires_raw.json or fall back to large-caps."""
    if _FIRES_PATH.exists():
        import json
        try:
            fires = json.loads(_FIRES_PATH.read_text(encoding="utf-8"))
            syms = list({f.get("symbol") or f.get("ticker") for f in fires
                         if f.get("symbol") or f.get("ticker")})
            syms = [s for s in syms if s]
            if syms:
                print(f"Using {len(syms)} symbols from fires_raw.json")
                return syms[:50]  # cap at 50 for reasonable download time
        except Exception:
            pass
    print(f"Using {len(_DEFAULT_SYMBOLS)} default large-cap symbols")
    return _DEFAULT_SYMBOLS


def run(symbols: list[str], period: str = "1y", out: str | None = None) -> str:
    """
    Run the signal backtest and generate HTML tearsheet.

    Returns:
        Path to the generated HTML file.
    """
    import yfinance as yf
    import pandas as pd
    import quantstats as qs

    out_path = Path(out) if out else ROOT / "reports" / "signal_backtest.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {period} history for {len(symbols)} symbols...")
    raw = yf.download(symbols, period=period, progress=False, auto_adjust=True)

    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"].dropna(how="all", axis=1)
    else:
        sym = symbols[0] if len(symbols) == 1 else "PORTFOLIO"
        close = raw[["Close"]].rename(columns={"Close": sym})

    available = list(close.columns)
    print(f"Got {len(close)} trading days for {len(available)} symbols")

    # Equal-weight portfolio returns
    returns_by_sym = close.pct_change().dropna()
    portfolio_returns = returns_by_sym.mean(axis=1)

    # Print console metrics
    print(f"\n=== SIGNAL PORTFOLIO METRICS ({period}) ===")
    sharpe  = qs.stats.sharpe(portfolio_returns)
    sortino = qs.stats.sortino(portfolio_returns)
    max_dd  = qs.stats.max_drawdown(portfolio_returns)
    cagr    = qs.stats.cagr(portfolio_returns)
    wr      = qs.stats.win_rate(portfolio_returns)
    kelly   = qs.stats.kelly_criterion(portfolio_returns)

    print(f"  Sharpe Ratio    : {sharpe:.3f}  (>1.0 is good, >2.0 is excellent)")
    print(f"  Sortino Ratio   : {sortino:.3f}  (downside risk adjusted)")
    print(f"  Max Drawdown    : {max_dd:.1%}")
    print(f"  CAGR            : {cagr:.1%}")
    print(f"  Win Rate        : {wr:.1%}")
    print(f"  Kelly Criterion : {kelly:.1%}  (optimal position sizing)")

    # Generate HTML tearsheet vs SPY
    print("\nGenerating HTML tearsheet vs SPY benchmark...")
    qs.reports.html(
        portfolio_returns,
        benchmark="SPY",
        output=str(out_path),
        title=f"Signal Portfolio ({len(available)} symbols, {period})",
    )

    size_kb = out_path.stat().st_size // 1024
    print(f"Tearsheet saved: {out_path} ({size_kb:,} KB)")
    return str(out_path)


def main(args=None):
    parser = argparse.ArgumentParser(
        description="Generate quantstats HTML tearsheet for signal portfolio"
    )
    parser.add_argument(
        "--symbols", default="", help="Comma-separated symbols (default: fires_raw.json or large-caps)"
    )
    parser.add_argument(
        "--period", default="1y", help="History period: 1y, 2y, 6mo (default: 1y)"
    )
    parser.add_argument(
        "--out", default="", help="Output HTML path (default: reports/signal_backtest.html)"
    )
    args = parser.parse_args(args)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or _load_symbols()
    out = args.out or None
    run(symbols=symbols, period=args.period, out=out)


if __name__ == "__main__":
    main()
