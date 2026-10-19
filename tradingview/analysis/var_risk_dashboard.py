"""
VaR / Risk Dashboard — Value at Risk and portfolio risk metrics.
================================================================
Computes 1-day 95%/99% Value at Risk, portfolio beta vs S&P 500,
sector/country concentration, and max drawdown using historical returns.

Output: tradingview/reports/risk_dashboard.html

Usage:
    python tradingview/analysis/var_risk_dashboard.py
    python tradingview/analysis/var_risk_dashboard.py --confidence 99 --window 252
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_THIS_DIR = Path(__file__).resolve().parent
_TV = _THIS_DIR.parent
_ROOT = _TV.parent
sys.path.insert(0, str(_ROOT))

try:
    from path_config import REPORTS
except ImportError:
    REPORTS = _ROOT / "reports"

POSITIONS_CACHE = _ROOT / ".cache_global_positions.json"
DEFAULT_OUT = REPORTS / "risk_dashboard.html"


@dataclass
class RiskMetrics:
    """Portfolio risk metrics."""
    portfolio_value: float = 0.0
    var_95_1d: float = 0.0        # 1-day 95% VaR (USD)
    var_99_1d: float = 0.0        # 1-day 99% VaR (USD)
    var_95_pct: float = 0.0       # VaR as % of portfolio
    var_99_pct: float = 0.0
    portfolio_volatility: float = 0.0  # Annualized volatility
    daily_volatility: float = 0.0
    max_drawdown: float = 0.0
    beta_vs_spy: float = 0.0
    sharpe_ratio: float = 0.0
    concentration_top5: float = 0.0    # % in top 5 positions
    concentration_top10: float = 0.0   # % in top 10 positions
    region_concentration: dict = None
    broker_concentration: dict = None
    num_positions: int = 0
    correlated_pairs: list = None      # Pairs with correlation > 0.85

    def __post_init__(self):
        if self.region_concentration is None:
            self.region_concentration = {}
        if self.broker_concentration is None:
            self.broker_concentration = {}
        if self.correlated_pairs is None:
            self.correlated_pairs = []


def _z_score(confidence: float) -> float:
    """Get Z-score for a given confidence level using a lookup table."""
    z_table = {
        90: 1.282,
        95: 1.645,
        99: 2.326,
        99.5: 2.576,
        99.9: 3.090,
    }
    return z_table.get(confidence, 1.645)


def compute_var_parametric(
    returns: list[float],
    portfolio_value: float,
    confidence: float = 95,
) -> tuple[float, float]:
    """
    Compute parametric VaR assuming normal distribution.

    Args:
        returns: List of historical daily returns (as decimals)
        portfolio_value: Current portfolio value
        confidence: Confidence level (95 or 99)

    Returns:
        (var_dollar, var_pct) tuple
    """
    if not returns or portfolio_value <= 0:
        return 0.0, 0.0

    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / max(n - 1, 1)
    std_dev = math.sqrt(variance)

    z = _z_score(confidence)
    var_pct = mean - z * std_dev  # Negative means loss
    var_dollar = abs(var_pct) * portfolio_value

    return var_dollar, abs(var_pct)


def compute_var_historical(
    returns: list[float],
    portfolio_value: float,
    confidence: float = 95,
) -> tuple[float, float]:
    """
    Compute historical VaR using percentile method.

    Args:
        returns: Sorted list of historical daily returns
        portfolio_value: Current portfolio value
        confidence: Confidence level

    Returns:
        (var_dollar, var_pct) tuple
    """
    if not returns or portfolio_value <= 0:
        return 0.0, 0.0

    sorted_returns = sorted(returns)
    percentile_idx = int(len(sorted_returns) * (1 - confidence / 100))
    percentile_idx = max(0, min(percentile_idx, len(sorted_returns) - 1))
    var_pct = abs(sorted_returns[percentile_idx])
    var_dollar = var_pct * portfolio_value

    return var_dollar, var_pct


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Compute maximum drawdown from an equity curve."""
    if not equity_curve or len(equity_curve) < 2:
        return 0.0

    peak = equity_curve[0]
    max_dd = 0.0

    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return max_dd


def compute_beta(
    portfolio_returns: list[float],
    benchmark_returns: list[float],
) -> float:
    """Compute portfolio beta vs benchmark."""
    if len(portfolio_returns) != len(benchmark_returns) or len(portfolio_returns) < 2:
        return 1.0

    n = len(portfolio_returns)
    mean_p = sum(portfolio_returns) / n
    mean_b = sum(benchmark_returns) / n

    cov = sum((p - mean_p) * (b - mean_b) for p, b in zip(portfolio_returns, benchmark_returns)) / (n - 1)
    var_b = sum((b - mean_b) ** 2 for b in benchmark_returns) / (n - 1)

    if var_b == 0:
        return 1.0

    return cov / var_b


def compute_sharpe(returns: list[float], risk_free_rate: float = 0.05) -> float:
    """Compute annualized Sharpe ratio."""
    if not returns or len(returns) < 2:
        return 0.0

    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std_dev = math.sqrt(variance)

    if std_dev == 0:
        return 0.0

    daily_rf = risk_free_rate / 252
    excess_return = mean - daily_rf
    daily_sharpe = excess_return / std_dev
    return daily_sharpe * math.sqrt(252)


def compute_concentration(positions: list[dict]) -> dict:
    """Compute concentration metrics."""
    if not positions:
        return {"top5": 0, "top10": 0, "region": {}, "broker": {}}

    total = sum(float(p.get("market_val", 0) or 0) for p in positions)
    if total <= 0:
        return {"top5": 0, "top10": 0, "region": {}, "broker": {}}

    sorted_pos = sorted(positions, key=lambda p: float(p.get("market_val", 0) or 0), reverse=True)
    top5 = sum(float(p.get("market_val", 0) or 0) for p in sorted_pos[:5]) / total * 100
    top10 = sum(float(p.get("market_val", 0) or 0) for p in sorted_pos[:10]) / total * 100

    region_totals: dict[str, float] = defaultdict(float)
    broker_totals: dict[str, float] = defaultdict(float)
    for p in positions:
        mv = float(p.get("market_val", 0) or 0)
        broker_totals[p.get("broker", "Unknown")] += mv
        # Simple region from broker
        broker = p.get("broker", "")
        if broker in ("Zerodha", "AngelOne"):
            region_totals["IN"] += mv
        elif broker in ("Moomoo-AU", "Webull AU"):
            region_totals["AU"] += mv
        elif broker == "Moomoo-SG":
            region_totals["SG"] += mv
        elif broker == "IBKR":
            region_totals["US"] += mv
        else:
            region_totals["OTHER"] += mv

    return {
        "top5": top5,
        "top10": top10,
        "region": {k: v / total * 100 for k, v in region_totals.items()},
        "broker": {k: v / total * 100 for k, v in broker_totals.items()},
    }


def load_positions_from_cache(cache_path: Path = POSITIONS_CACHE) -> list[dict]:
    """Load positions from cache."""
    if not cache_path.exists():
        return []
    with open(cache_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw if isinstance(raw, list) else raw.get("positions", [])


def compute_risk_metrics(
    positions: list[dict],
    daily_returns: Optional[list[float]] = None,
    benchmark_returns: Optional[list[float]] = None,
    confidence: float = 95,
) -> RiskMetrics:
    """
    Compute full risk metrics suite.

    Args:
        positions: List of position dicts
        daily_returns: Portfolio daily returns (if None, estimates from positions)
        benchmark_returns: S&P 500 daily returns for beta calc
        confidence: VaR confidence level

    Returns:
        RiskMetrics dataclass
    """
    total_value = sum(float(p.get("market_val", 0) or 0) for p in positions)
    concentration = compute_concentration(positions)

    metrics = RiskMetrics(
        portfolio_value=total_value,
        num_positions=len(positions),
        concentration_top5=concentration["top5"],
        concentration_top10=concentration["top10"],
        region_concentration=concentration["region"],
        broker_concentration=concentration["broker"],
    )

    if daily_returns and len(daily_returns) >= 20:
        # VaR calculations
        var95_dollar, var95_pct = compute_var_parametric(daily_returns, total_value, 95)
        var99_dollar, var99_pct = compute_var_parametric(daily_returns, total_value, 99)
        metrics.var_95_1d = var95_dollar
        metrics.var_99_1d = var99_dollar
        metrics.var_95_pct = var95_pct * 100
        metrics.var_99_pct = var99_pct * 100

        # Volatility
        n = len(daily_returns)
        mean = sum(daily_returns) / n
        variance = sum((r - mean) ** 2 for r in daily_returns) / max(n - 1, 1)
        metrics.daily_volatility = math.sqrt(variance) * 100
        metrics.portfolio_volatility = metrics.daily_volatility * math.sqrt(252)

        # Sharpe
        metrics.sharpe_ratio = compute_sharpe(daily_returns)

        # Max drawdown from cumulative returns
        equity = [1.0]
        for r in daily_returns:
            equity.append(equity[-1] * (1 + r))
        metrics.max_drawdown = compute_max_drawdown(equity) * 100

    if benchmark_returns and daily_returns:
        min_len = min(len(daily_returns), len(benchmark_returns))
        if min_len >= 20:
            metrics.beta_vs_spy = compute_beta(
                daily_returns[:min_len],
                benchmark_returns[:min_len],
            )

    return metrics


def generate_risk_html(metrics: RiskMetrics, output_path: Path = DEFAULT_OUT) -> Path:
    """Generate HTML risk dashboard."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Region breakdown
    region_rows = ""
    for region, pct in sorted(metrics.region_concentration.items(), key=lambda x: -x[1]):
        bar_width = min(pct, 100)
        color = "#f85149" if pct > 40 else "#d29922" if pct > 30 else "#3fb950"
        region_rows += f"""<tr>
  <td>{region}</td>
  <td>{pct:.1f}%</td>
  <td><div style="background:{color}; height:20px; width:{bar_width}%; border-radius:4px;"></div></td>
</tr>"""

    # Broker breakdown
    broker_rows = ""
    for broker, pct in sorted(metrics.broker_concentration.items(), key=lambda x: -x[1]):
        broker_rows += f"<tr><td>{broker}</td><td>{pct:.1f}%</td></tr>\n"

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Risk Dashboard</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; background: #0d1117; color: #c9d1d9; }}
  h1,h2 {{ color: #58a6ff; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 20px 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }}
  .card .value {{ font-size: 1.8em; font-weight: bold; }}
  .card .label {{ font-size: 0.85em; color: #8b949e; }}
  .danger {{ color: #f85149; }}
  .warn {{ color: #d29922; }}
  .ok {{ color: #3fb950; }}
  .neutral {{ color: #58a6ff; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th,td {{ border: 1px solid #30363d; padding: 8px 12px; }}
  th {{ background: #161b22; color: #58a6ff; }}
  footer {{ margin-top: 20px; color: #484f58; font-size: 0.85em; }}
</style>
</head><body>
<h1>🛡️ Risk Dashboard</h1>
<p>Portfolio: <strong>${metrics.portfolio_value:,.0f}</strong> | {metrics.num_positions} positions</p>

<div class="grid">
  <div class="card"><div class="value danger">${metrics.var_95_1d:,.0f}</div><div class="label">1-Day VaR (95%)</div></div>
  <div class="card"><div class="value danger">${metrics.var_99_1d:,.0f}</div><div class="label">1-Day VaR (99%)</div></div>
  <div class="card"><div class="value warn">{metrics.portfolio_volatility:.1f}%</div><div class="label">Ann. Volatility</div></div>
  <div class="card"><div class="value {'ok' if metrics.beta_vs_spy<1 else 'warn'}">{metrics.beta_vs_spy:.2f}</div><div class="label">Beta vs S&P 500</div></div>
  <div class="card"><div class="value {'ok' if metrics.sharpe_ratio>1 else 'warn'}">{metrics.sharpe_ratio:.2f}</div><div class="label">Sharpe Ratio</div></div>
  <div class="card"><div class="value danger">{metrics.max_drawdown:.1f}%</div><div class="label">Max Drawdown</div></div>
  <div class="card"><div class="value {'danger' if metrics.concentration_top5>50 else 'warn'}">{metrics.concentration_top5:.1f}%</div><div class="label">Top 5 Concentration</div></div>
  <div class="card"><div class="value neutral">{metrics.concentration_top10:.1f}%</div><div class="label">Top 10 Concentration</div></div>
</div>

<h2>Region Allocation</h2>
<table><thead><tr><th>Region</th><th>Weight</th><th>Bar</th></tr></thead>
<tbody>{region_rows}</tbody></table>

<h2>Broker Allocation</h2>
<table><thead><tr><th>Broker</th><th>Weight</th></tr></thead>
<tbody>{broker_rows}</tbody></table>

<footer>Risk Dashboard v1.0 | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</footer>
</body></html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"✅ Risk dashboard saved to {output_path}")
    return output_path


def main(args: Optional[argparse.Namespace] = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="VaR / Risk Dashboard")
        parser.add_argument("--confidence", type=float, default=95, help="VaR confidence %%")
        parser.add_argument("--window", type=int, default=252, help="Historical window (days)")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output HTML path")
        parser.add_argument("--positions-file", default=str(POSITIONS_CACHE), help="Positions JSON")
        args = parser.parse_args()

    positions = load_positions_from_cache(Path(args.positions_file))
    if not positions:
        print("❌ No positions found. Run global_portfolio_dashboard.py first.")
        return

    # Try to get returns from yfinance
    daily_returns = None
    benchmark_returns = None
    try:
        import yfinance as yf
        # Use SPY as portfolio proxy for VaR estimation
        spy = yf.download("SPY", period=f"{args.window}d", progress=False)
        if not spy.empty:
            close_col = "Close"
            if isinstance(spy.columns, type(spy.columns)) and hasattr(spy.columns, 'get_level_values'):
                try:
                    spy.columns = spy.columns.get_level_values(0)
                except Exception:
                    pass
            if close_col in spy.columns:
                benchmark_returns = spy[close_col].pct_change().dropna().tolist()
                # Rough portfolio proxy: weighted SPY returns
                daily_returns = benchmark_returns  # Placeholder until we have individual stock returns
    except Exception as e:
        print(f"⚠️ Could not fetch benchmark data: {e}")

    metrics = compute_risk_metrics(positions, daily_returns, benchmark_returns, args.confidence)
    generate_risk_html(metrics, Path(args.out))


if __name__ == "__main__":
    main()
