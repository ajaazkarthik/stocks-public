#!/usr/bin/env python3
"""
Portfolio Correlation Matrix
=============================
Detects concentration risk by computing Pearson return correlations
across all open positions, using 90-day price history from yfinance.

High correlation (>0.7) between two holdings = hidden concentration risk —
a single macro event can hurt both positions simultaneously.

Reads from:  .cache_global_positions.json  (from global_portfolio_dashboard.py)
Outputs:     reports/portfolio_correlation.html

Usage:
    python tradingview/analysis/portfolio_correlation.py
    python tradingview/analysis/portfolio_correlation.py --positions-file .cache_global_positions.json
    python tradingview/analysis/portfolio_correlation.py --days 60 --out reports/portfolio_correlation.html
"""
import sys
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent.parent.resolve()

_DEFAULT_POSITIONS = ROOT / ".cache_global_positions.json"
_DEFAULT_OUT       = ROOT / "reports" / "portfolio_correlation.html"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_positions(path: Path) -> list[dict]:
    if not path.exists():
        print(f"[WARN] Positions cache not found: {path}")
        print("       Run 'Global Portfolio Dashboard' (option 1) first.")
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else raw.get("positions", [])


def _extract_tickers(positions: list[dict], markets: str = "all") -> list[str]:
    """Pull yf_ticker from positions.

    Parameters
    ----------
    markets : "us" | "all"
        "us"  — only US tickers (no suffix)
        "all" — include AU (.AX), IN (.NS/.BO), SG (.SI), HK (.HK) tickers
    """
    _SUFFIX_MAP = {
        "ASX": ".AX", "NSE": ".NS", "BSE": ".BO",
        "HKEX": ".HK", "SGX": ".SI",
    }
    seen: set[str] = set()
    tickers = []
    for p in positions:
        t = (p.get("yf_ticker") or p.get("ticker") or "").strip()
        if not t or t in seen:
            continue
        if markets == "us":
            # Skip non-US (contains dot = ASX/NSE/HK/SG suffix)
            if "." in t:
                continue
        else:
            # For non-US tickers without a suffix, try to add one from exchange
            exchange = (p.get("exchange") or "").upper()
            if "." not in t and exchange in _SUFFIX_MAP:
                t = t + _SUFFIX_MAP[exchange]
        seen.add(t)
        tickers.append(t)
    return tickers[:60]   # cap at 60 to keep download manageable


def _download_returns(tickers: list[str], days: int) -> "dict[str, list[float]] | None":
    """Download adjusted close and compute daily returns.  Returns {ticker: [returns]}."""
    try:
        import yfinance as yf
        import pandas as pd
        end   = datetime.now()
        start = end - timedelta(days=days + 5)
        raw   = yf.download(tickers, start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"),
                             auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return None
        # yfinance ≥0.2 returns MultiIndex columns; handle both layouts
        if hasattr(raw.columns, "levels"):
            # MultiIndex: (Price, Ticker)
            if "Close" in raw.columns.get_level_values(0):
                closes = raw["Close"]
            else:
                return None
        else:
            closes = raw if raw.shape[1] == len(tickers) else raw.get("Close", raw)

        closes = closes.dropna(how="all")
        rets   = closes.pct_change().dropna(how="all")
        result = {}
        for t in tickers:
            if t in rets.columns:
                col = rets[t].dropna()
                if len(col) >= 20:
                    result[t] = col.tolist()
        return result if result else None
    except Exception as exc:
        print(f"[WARN] yfinance download failed: {exc}")
        return None


def _pearson(a: list[float], b: list[float]) -> float:
    """Compute Pearson correlation between two series (equal length, min 10)."""
    n = min(len(a), len(b))
    if n < 10:
        return float("nan")
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num  = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    dena = (sum((x - ma) ** 2 for x in a)) ** 0.5
    denb = (sum((y - mb) ** 2 for y in b)) ** 0.5
    if dena < 1e-10 or denb < 1e-10:
        return float("nan")
    return round(num / (dena * denb), 3)


def _build_matrix(returns: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    tickers = sorted(returns.keys())
    matrix  = {}
    for t1 in tickers:
        matrix[t1] = {}
        for t2 in tickers:
            matrix[t1][t2] = _pearson(returns[t1], returns[t2]) if t1 != t2 else 1.0
    return matrix


# ── HTML generation ──────────────────────────────────────────────────────────

def _cell_color(v: float) -> str:
    if v != v:   return "#eee"          # NaN
    if v >= 0.8: return "#d73027"       # very high — red
    if v >= 0.6: return "#fc8d59"       # high — orange
    if v >= 0.4: return "#fee090"       # moderate — yellow
    if v >= 0.0: return "#e0f3f8"       # low — light blue
    return "#91bfdb"                    # negative — blue (diversifying)


def _build_html(matrix: dict[str, dict[str, float]], tickers: list[str],
                days: int, generated_at: str, warnings: list[str]) -> str:
    tickers_sorted = sorted(tickers)
    header_cells = "".join(f"<th>{t}</th>" for t in tickers_sorted)
    rows = []
    for t1 in tickers_sorted:
        cells = "".join(
            f'<td style="background:{_cell_color(matrix[t1].get(t2, float("nan")))};">'
            f'{matrix[t1].get(t2, float("nan")):.2f}</td>'
            for t2 in tickers_sorted
        )
        rows.append(f"<tr><th>{t1}</th>{cells}</tr>")

    warn_html = "".join(
        f'<div class="warn">&#9888; {w}</div>' for w in warnings
    ) if warnings else '<div class="ok">&#10003; No high-correlation pairs detected (threshold: 0.7).</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Portfolio Correlation Matrix — {generated_at}</title>
  <style>
    body {{ font-family: Arial, sans-serif; background: #1a1a2e; color: #eee; padding: 20px; }}
    h1   {{ color: #e0e0ff; }}
    p    {{ color: #aaa; font-size: 0.9em; }}
    table {{ border-collapse: collapse; margin-top: 16px; font-size: 0.82em; }}
    th, td {{ border: 1px solid #444; padding: 6px 8px; text-align: center; min-width: 54px; }}
    th   {{ background: #2a2a4e; color: #c8c8ff; }}
    .warn {{ background: #5c1a1a; border: 1px solid #c00; border-radius: 4px;
             padding: 8px 12px; margin: 6px 0; color: #ffaaaa; }}
    .ok   {{ background: #1a3a1a; border: 1px solid #0a0; border-radius: 4px;
             padding: 8px 12px; margin: 6px 0; color: #aaffaa; }}
    .legend {{ display:flex; gap:12px; margin:12px 0; font-size:0.8em; }}
    .lbox {{ padding:4px 10px; border-radius:3px; }}
  </style>
</head>
<body>
  <h1>Portfolio Correlation Matrix</h1>
  <p>Generated: {generated_at} &nbsp;|&nbsp; Lookback: {days} days &nbsp;|&nbsp; {len(tickers_sorted)} tickers (multi-market)</p>
  <div class="legend">
    <span class="lbox" style="background:#d73027">≥ 0.80 Very High</span>
    <span class="lbox" style="background:#fc8d59">≥ 0.60 High</span>
    <span class="lbox" style="background:#fee090">≥ 0.40 Moderate</span>
    <span class="lbox" style="background:#e0f3f8; color:#333">≥ 0.00 Low</span>
    <span class="lbox" style="background:#91bfdb; color:#333">&lt; 0.00 Negative (diversifying)</span>
  </div>
  <h2>Concentration Risk Alerts</h2>
  {warn_html}
  <h2>Pairwise Correlation Table</h2>
  <table>
    <thead><tr><th></th>{header_cells}</tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  <p style="margin-top:16px;font-size:0.78em;color:#888;">
    Pearson correlation on daily returns. Values &gt;0.7 indicate shared factor exposure —
    consider hedging or rebalancing to reduce hidden concentration risk.
  </p>
</body>
</html>"""


def main(args) -> None:
    pos_file = Path(args.positions_file)
    out_file = Path(args.out)
    days     = int(args.days)
    markets  = getattr(args, "markets", "all")

    print(f"[INFO] Loading positions from {pos_file}", flush=True)
    positions = _load_positions(pos_file)
    if not positions:
        print("[WARN] No positions loaded — exiting.")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("<html><head><title>Portfolio Correlation</title></head><body><h1>Portfolio Correlation</h1><p>No positions data available. Run the global portfolio dashboard first.</p></body></html>", encoding="utf-8")
        return

    tickers = _extract_tickers(positions, markets=markets)
    if len(tickers) < 2:
        print("[WARN] Fewer than 2 US tickers found — correlation requires ≥2 symbols.")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("<html><head><title>Portfolio Correlation</title></head><body><h1>Portfolio Correlation</h1><p>Insufficient data: fewer than 2 tradeable tickers found. Correlation matrix requires at least 2 symbols.</p></body></html>", encoding="utf-8")
        return

    print(f"[INFO] Downloading {len(tickers)} tickers × {days}d from yfinance...", flush=True)
    import time as _time
    t0      = _time.time()
    returns = _download_returns(tickers, days)
    elapsed = round((_time.time() - t0) * 1000)
    if not returns:
        print(f"[WARN] Download failed after {elapsed} ms. Cannot build correlation matrix.")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("<html><head><title>Portfolio Correlation</title></head><body><h1>Portfolio Correlation</h1><p>Unable to download price data. Check internet connection and retry.</p></body></html>", encoding="utf-8")
        return
    print(f"[OK]   Downloaded {len(returns)} tickers in {elapsed} ms")

    matrix = _build_matrix(returns)

    # Find high-correlation pairs
    tickers_ok = sorted(returns.keys())
    warnings: list[str] = []
    for i, t1 in enumerate(tickers_ok):
        for t2 in tickers_ok[i + 1:]:
            v = matrix[t1].get(t2, float("nan"))
            if v == v and v >= 0.7:
                warnings.append(f"{t1} ↔ {t2}: correlation {v:.2f} — hidden concentration risk")

    if warnings:
        print(f"\n[!!] {len(warnings)} high-correlation pairs found:")
        for w in warnings:
            print(f"     {w}")
    else:
        print("[OK] No high-correlation pairs (>0.7) detected.")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = _build_html(matrix, tickers_ok, days, generated_at, warnings)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")
    print(f"\n[OK] Report written to {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio Correlation Matrix")
    parser.add_argument("--positions-file", default=str(_DEFAULT_POSITIONS),
                        help="Path to .cache_global_positions.json")
    parser.add_argument("--days",    default="90",           help="Lookback days (default 90)")
    parser.add_argument("--markets", default="all", choices=["us", "all"],
                        help="Market scope: 'us' (US only) or 'all' (AU/IN/SG/HK too, default: all)")
    parser.add_argument("--out",     default=str(_DEFAULT_OUT), help="Output HTML path")
    main(parser.parse_args())
