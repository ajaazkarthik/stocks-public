"""
Per-Market Expectancy Calculator — Book-sourced risk metric.

Source: Van Tharp, "Trade Your Way to Financial Freedom"

Calculates per-market (broker/exchange) expectancy to identify which
markets are profitable and which are dragging returns down.

    Expectancy = (Win% × Avg Win) - (Loss% × Avg Loss)

A positive expectancy means the market is profitable over time;
negative means you should reduce exposure or change strategy.

Usage
-----
    from tradingview.analysis.per_market_expectancy import compute_expectancy

    trades = [
        {"broker": "Moomoo-AU", "pnl_pct": 12.5},
        {"broker": "Moomoo-AU", "pnl_pct": -3.2},
        {"broker": "Zerodha", "pnl_pct": 8.0},
    ]
    results = compute_expectancy(trades)
    # results = {
    #     "Moomoo-AU": {"win_rate": 50.0, "avg_win": 12.5, "avg_loss": 3.2,
    #                   "expectancy": 4.65, "total_trades": 2, "detail": "..."},
    #     "Zerodha": {"win_rate": 100.0, ...},
    # }
"""

from __future__ import annotations

import sys
from collections import defaultdict

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def compute_expectancy(
    trades: list[dict],
    group_key: str = "broker",
) -> dict[str, dict]:
    """Compute per-group expectancy from a list of closed trades.

    Parameters
    ----------
    trades : list[dict]
        Each trade must have at minimum:
        - ``group_key`` field (default "broker") — the grouping dimension
        - ``pnl_pct`` (float) — the % return of the trade (positive = win)
    group_key : str
        Which field to group by (default "broker"; could be "exchange", "market", etc.)

    Returns
    -------
    dict mapping group name → stats dict with keys:
        win_rate (float), avg_win (float), avg_loss (float),
        expectancy (float), total_trades (int), wins (int), losses (int),
        detail (str)
    """
    if not trades:
        return {}

    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        grp = t.get(group_key, "Unknown")
        pnl = t.get("pnl_pct")
        if pnl is None:
            continue
        groups[grp].append(float(pnl))

    results: dict[str, dict] = {}
    for grp, pnl_list in sorted(groups.items()):
        wins = [p for p in pnl_list if p > 0]
        losses = [p for p in pnl_list if p <= 0]
        total = len(pnl_list)

        win_rate = len(wins) / total * 100.0 if total > 0 else 0.0
        loss_rate = 100.0 - win_rate
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

        # Van Tharp expectancy formula
        expectancy = (win_rate / 100.0 * avg_win) - (loss_rate / 100.0 * avg_loss)

        verdict = "PROFITABLE" if expectancy > 0 else "UNPROFITABLE" if expectancy < 0 else "BREAKEVEN"

        results[grp] = {
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "detail": f"{grp}: {verdict} — E={expectancy:+.2f}%, WR={win_rate:.0f}%, "
                      f"avg_win={avg_win:+.1f}%, avg_loss=-{avg_loss:.1f}% ({total} trades)",
        }

    return results


def score_expectancy(expectancy: float) -> dict:
    """Convert expectancy to a scoring signal for pipeline integration.

    Parameters
    ----------
    expectancy : float
        The expectancy value from :func:`compute_expectancy`.

    Returns
    -------
    dict with keys: signal (str), score (int), detail (str)
    """
    if expectancy > 5.0:
        score = 3
        label = "STRONG"
    elif expectancy > 0:
        score = 1
        label = "POSITIVE"
    elif expectancy == 0:
        score = 0
        label = "BREAKEVEN"
    elif expectancy > -5.0:
        score = -1
        label = "WEAK"
    else:
        score = -3
        label = "NEGATIVE"

    return {
        "signal": "market_expectancy",
        "score": score,
        "detail": f"Expectancy {expectancy:+.2f}% — {label}",
    }
