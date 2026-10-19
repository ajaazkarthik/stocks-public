"""
Backtest-driven Position Sizer
================================
Given a symbol, runs (or loads from cache) the buy_sell_v2 backtest and returns
a position-sizing recommendation: 0, 1, 2, or 3 units.

Decision rules
--------------
  ≥ 3 units  :  Sharpe ≥ 0.5  AND  win_rate ≥ 55 %   → HIGH CONVICTION
  2 units    :  Sharpe ≥ 0.2  OR   win_rate ≥ 45 %   → MODERATE
  1 unit     :  Anything else that isn't a hard avoid  → LOW CONVICTION
  0 units    :  total_pnl < −10  OR  win_rate < 35 %  → AVOID
  1 unit*    :  fewer than MIN_TRADES closed trades    → INSUFFICIENT DATA (default safe)

Plain-English explanations let a human instantly understand what the numbers mean
without knowing trading jargon.

Public API
----------
  size_from_metrics(win_rate, total_pnl, sharpe, n_trades) -> (int, str, str)
  plain_english(metrics: dict) -> str
  get_position_size(tv_sym, yf_ticker, interval, period, use_cache) -> dict
  run_backtest_menu(tv_sym_or_all)   — interactive terminal report
"""

import json, time
from datetime import datetime, timezone
from pathlib import Path

_HERE  = Path(__file__).parent
_CACHE = _HERE / "backtest_cache.json"

MIN_TRADES = 5      # fewer closed trades → "insufficient data" → 1 unit (safe default)
CACHE_TTL  = 7 * 24 * 3600   # seconds — 7 days

# ---------------------------------------------------------------------------
# Pure logic (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def size_from_metrics(win_rate: float, total_pnl: float, sharpe: float,
                      n_trades: int) -> tuple[int, str, str]:
    """Return (units, label, hex_color).

    Parameters are all numeric; no network or file I/O.
    """
    if n_trades < MIN_TRADES:
        return (1, "Insufficient data — using 1 unit (safe default)", "#9e9e9e")

    # Hard avoid
    if total_pnl <= -10.0 or win_rate <= 35.0:
        return (0, "AVOID — strategy does not work on this symbol", "#f44336")

    # High conviction: both Sharpe AND win_rate strong
    if sharpe >= 0.5 and win_rate >= 55.0:
        return (3, "HIGH CONVICTION — size up (3 units)", "#4caf50")

    # Moderate: at least one of Sharpe or win_rate is acceptable
    if sharpe >= 0.2 or win_rate >= 45.0:
        return (2, "MODERATE — standard size (2 units)", "#ff9800")

    # Low conviction
    return (1, "LOW CONVICTION — minimum size (1 unit)", "#9e9e9e")


def plain_english(metrics: dict) -> str:
    """Return a multi-line human-readable explanation of what the metrics mean.

    ``metrics`` must have keys: win_rate, total_pnl, sharpe, num_closed, avg_win,
    avg_loss, avg_hold, interval.
    """
    win_rate  = metrics.get("win_rate",  0.0)
    total_pnl = metrics.get("total_pnl", 0.0)
    sharpe    = metrics.get("sharpe",    0.0)
    n         = metrics.get("num_closed", 0)
    avg_win   = metrics.get("avg_win",   0.0)
    avg_loss  = metrics.get("avg_loss",  0.0)
    avg_hold  = metrics.get("avg_hold",  0.0)
    interval  = metrics.get("interval",  "?")

    # Win rate commentary
    if win_rate >= 60:
        wr_comment = "Excellent — the strategy was right more often than not."
    elif win_rate >= 50:
        wr_comment = "Good — slightly more winners than losers."
    elif win_rate >= 40:
        wr_comment = "Mediocre — fewer than half of trades were winners."
    else:
        wr_comment = "Poor — most trades lost money."

    # Sharpe commentary
    if sharpe >= 1.0:
        sh_comment = "Exceptional risk-adjusted return. Professional-grade."
    elif sharpe >= 0.5:
        sh_comment = "Good. You earned decent return for every unit of risk taken."
    elif sharpe >= 0.2:
        sh_comment = "Borderline. Returns compensate for risk, but only just."
    elif sharpe >= 0.0:
        sh_comment = "Weak. Returns barely cover the volatility of outcomes."
    else:
        sh_comment = "Negative. On average losses exceeded gains — avoid."

    # Total P&L commentary
    pnl_sign = "+" if total_pnl >= 0 else ""
    if total_pnl >= 50:
        pnl_comment = "Strong cumulative performance across the test period."
    elif total_pnl >= 0:
        pnl_comment = "Positive but modest cumulative result."
    else:
        pnl_comment = "Cumulative losses — strategy destroyed value on this symbol."

    lines = [
        f"  Win Rate      {win_rate:.1f}%  — won {int(round(n * win_rate / 100))} of {n} closed trades. "
        f"{wr_comment}",
        f"",
        f"  Total P&L     {pnl_sign}{total_pnl:.1f}%  — sum of every closed trade's gain/loss "
        f"(NOT compound). {pnl_comment}",
        f"",
        f"  Sharpe Ratio  {sharpe:.3f}  — for every 1% of volatility (spread of outcomes), "
        f"you earned {sharpe:.2f}% average return. {sh_comment}",
        f"",
        f"  Avg Win/Loss  +{avg_win:.1f}% per winning trade vs {avg_loss:.1f}% per losing trade "
        f"(higher is better for both).",
        f"",
        f"  Avg Hold      {avg_hold:.0f} {interval} bars per trade.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _cache_key(tv_sym: str, interval: str, period: str) -> str:
    return f"{tv_sym}_{interval}_{period}"


def _is_fresh(entry: dict) -> bool:
    ts = entry.get("_cached_at", 0)
    return (time.time() - ts) < CACHE_TTL


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def get_position_size(
    tv_sym:    str,
    yf_ticker: str,
    interval:  str = "1d",
    period:    str = "5y",
    use_cache: bool = True,
) -> dict:
    """Run backtest (or load from cache) and return full sizing recommendation.

    Returns a dict with keys:
        tv_sym, yf_ticker, interval, period,
        win_rate, total_pnl, sharpe, num_closed, avg_win, avg_loss, avg_hold,
        size_units (0/1/2/3),
        size_label (human label),
        size_color (hex),
        plain_english (multi-line string),
        from_cache (bool),
        error (str or None)
    """
    cache = _load_cache() if use_cache else {}
    key   = _cache_key(tv_sym, interval, period)

    if use_cache and key in cache and _is_fresh(cache[key]):
        entry = dict(cache[key])
        entry["from_cache"] = True
        return entry

    # Run backtest (deferred import so sizer is usable without heavy deps)
    try:
        from backtest.backtest_buy_sell_v2 import backtest as _run_backtest
    except ImportError:
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent))
            from backtest.backtest_buy_sell_v2 import backtest as _run_backtest
        except ImportError as exc:
            return _error_result(tv_sym, yf_ticker, interval, period, str(exc))

    try:
        result = _run_backtest(tv_sym, yf_ticker, interval, period)
    except Exception as exc:
        return _error_result(tv_sym, yf_ticker, interval, period, str(exc))

    if result is None:
        return _error_result(tv_sym, yf_ticker, interval, period, "No trades generated")

    win_rate  = result["win_rate"]
    total_pnl = result["total_pnl"]
    sharpe    = result.get("sharpe", 0.0)
    n_closed  = result["num_closed"]

    units, label, color = size_from_metrics(win_rate, total_pnl, sharpe, n_closed)
    pe = plain_english({
        "win_rate":  win_rate,
        "total_pnl": total_pnl,
        "sharpe":    sharpe,
        "num_closed": n_closed,
        "avg_win":   result.get("avg_win", 0.0),
        "avg_loss":  result.get("avg_loss", 0.0),
        "avg_hold":  result.get("avg_hold", 0.0),
        "interval":  interval,
    })

    entry = {
        "tv_sym":       tv_sym,
        "yf_ticker":    yf_ticker,
        "interval":     interval,
        "period":       period,
        "win_rate":     win_rate,
        "total_pnl":    total_pnl,
        "sharpe":       sharpe,
        "num_closed":   n_closed,
        "avg_win":      result.get("avg_win", 0.0),
        "avg_loss":     result.get("avg_loss", 0.0),
        "avg_hold":     result.get("avg_hold", 0.0),
        "size_units":   units,
        "size_label":   label,
        "size_color":   color,
        "plain_english": pe,
        "from_cache":   False,
        "error":        None,
        "_cached_at":   time.time(),
    }

    new_cache = dict(cache)
    new_cache[key] = entry
    try:
        _save_cache(new_cache)
    except Exception:
        pass   # non-fatal — we still return the result

    return entry


def _error_result(tv_sym, yf_ticker, interval, period, err_msg):
    return {
        "tv_sym":       tv_sym,
        "yf_ticker":    yf_ticker,
        "interval":     interval,
        "period":       period,
        "win_rate":     None,
        "total_pnl":    None,
        "sharpe":       None,
        "num_closed":   0,
        "avg_win":      None,
        "avg_loss":     None,
        "avg_hold":     None,
        "size_units":   None,   # CF6: fail-closed — None signals sizer error to callers
        "size_label":   f"Error — orders BLOCKED ({err_msg[:80]})",
        "size_color":   "#9e9e9e",
        "plain_english": f"Could not run backtest: {err_msg}",
        "from_cache":   False,
        "error":        err_msg,
    }


# ---------------------------------------------------------------------------
# Interactive terminal report (called from main.py menu)
# ---------------------------------------------------------------------------

from html_utils import BOLD, DIM, CYAN, GREEN, YELLOW, RED, RESET  # B-5
GRAY  = "\033[90m"  # not in html_utils

_COLOR_MAP = {
    "#4caf50": GREEN,
    "#ff9800": YELLOW,
    "#f44336": RED,
    "#9e9e9e": GRAY,
}

def _ansi(hex_color: str) -> str:
    return _COLOR_MAP.get(hex_color, "")


def run_backtest_menu(tv_sym_or_all: str = "all") -> None:
    """Interactive terminal backtest report — called from main.py."""
    from backtest.backtest_buy_sell_v2 import STOCKS

    if tv_sym_or_all.lower() == "all":
        targets = [(s[0], s[1], s[2], s[3]) for s in STOCKS]
    else:
        # Try to match partial symbol
        q = tv_sym_or_all.upper()
        targets = [(s[0], s[1], s[2], s[3]) for s in STOCKS
                   if q in s[0].upper() or q in s[1].upper()]
        if not targets:
            print(f"{RED}No matching symbol found for '{tv_sym_or_all}'.{RESET}")
            print(f"{DIM}Known symbols: {', '.join(s[0] for s in STOCKS)}{RESET}")
            return

    print(f"\n{CYAN}{BOLD}{'Symbol':<25} {'Trades':>7} {'Win%':>7} {'P&L':>9} "
          f"{'Sharpe':>8}  Recommendation{RESET}")
    print(f"{DIM}{'─'*80}{RESET}")

    for tv_sym, yf_tick, interval, period in targets:
        print(f"  {DIM}{tv_sym:<23}{RESET}", end=" ", flush=True)
        r = get_position_size(tv_sym, yf_tick, interval, period, use_cache=True)
        if r["error"]:
            print(f"{RED}ERROR: {r['error'][:60]}{RESET}")
            continue
        col = _ansi(r["size_color"])
        if r["from_cache"]:
            _age_secs = max(0.0, time.time() - r.get("_cached_at", time.time()))
            _age_hrs  = _age_secs / 3600
            _age_str  = f"{_age_hrs:.0f}h ago" if _age_hrs < 48 else f"{_age_hrs/24:.0f}d ago"
            cached = f"{GRAY}(cached {_age_str}){RESET}"
        else:
            cached = ""
        print(f"{r['num_closed']:>7d}  {r['win_rate']:>6.1f}%  {r['total_pnl']:>+9.1f}%  "
              f"{r['sharpe']:>7.3f}  {col}{BOLD}{r['size_units']}u — {r['size_label']}{RESET}  {cached}")

    print(f"\n{DIM}{'─'*80}{RESET}")
    print(f"\n{BOLD}Plain-English Guide:{RESET}")
    print(f"  {GREEN}Win Rate{RESET}   — % of closed trades that ended in profit")
    print(f"           ≥55% = reliable  |  45–55% = marginal  |  <40% = unreliable")
    print(f"  {CYAN}Total P&L{RESET}  — cumulative sum of all trade gains/losses (NOT compound)")
    print(f"           Positive = strategy made money  |  Negative = net loss")
    print(f"  {YELLOW}Sharpe{RESET}     — return per unit of risk (spread of trade outcomes)")
    print(f"           ≥0.5 = good  |  0.2–0.5 = OK  |  <0.2 = risky  |  <0 = losing")
    print(f"\n  {BOLD}Position sizes:{RESET}  3u = high conviction  |  2u = standard  "
          f"|  1u = minimum  |  0u = avoid")

    # Optionally show detailed breakdown
    if tv_sym_or_all.lower() != "all":
        r = get_position_size(targets[0][0], targets[0][1], targets[0][2], targets[0][3],
                              use_cache=True)
        if not r["error"]:
            print(f"\n{BOLD}Detailed breakdown for {targets[0][0]}:{RESET}")
            print(r["plain_english"])
