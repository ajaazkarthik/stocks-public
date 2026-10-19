"""
ZeroDTE Vectorbt Backtest
=========================
Reads historical 0-2DTE option scans from zerodte.db and simulates outcomes
using yfinance to compute intrinsic value at expiry.

Generates:
  reports/zerodte_backtest.html   — visual HTML report
  reports/zerodte_backtest.json   — structured JSON for programmatic access

Usage:
  python -m tradingview.options.zerodte.backtest_vectorbt [--out reports/zerodte_backtest.html]

Reference: Menu option 63 (TQS v2 backtest) for the same pattern.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ── Path setup ───────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
_TRADINGVIEW_DIR = _THIS_DIR.parent.parent
_ROOT_DIR = _TRADINGVIEW_DIR.parent

sys.path.insert(0, str(_ROOT_DIR))

# ── Windows UTF-8 fix ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Third-party imports ───────────────────────────────────────────────────────
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    import vectorbt as vbt
    _HAS_VBT = True
except ImportError:
    _HAS_VBT = False

try:
    import yfinance as yf
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

# ── Constants ─────────────────────────────────────────────────────────────────
DB_PATH = _ROOT_DIR / "database" / "zerodte.db"

# Score bucket definitions (aligned with TQS v2 reference)
BUCKETS = [
    ("LOW",    0.0,  30.0),
    ("MED",   30.0,  45.0),
    ("HIGH",  45.0,  60.0),
    ("APEX",  60.0, 999.0),
]

BUCKET_COLOURS = {
    "LOW":  "#6c757d",   # grey
    "MED":  "#0d6efd",   # blue
    "HIGH": "#fd7e14",   # orange
    "APEX": "#198754",   # green
}


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_scans() -> list[dict]:
    """Return all rows from zerodte_scans as dicts."""
    if not DB_PATH.exists():
        print(f"[WARN] DB not found: {DB_PATH}")
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id, scan_ts, scan_date, symbol, option_type, direction,
            contract_code, expiry, dte, strike, spot_price, otm_pct,
            bid, ask, mid_price, premium,
            iv, delta, gamma, theta,
            open_interest, volume, spread_pct,
            momentum_score, iv_back_score, voi_score,
            composite_score, confidence,
            prev_day_change_pct, volume_ratio, rsi,
            iv_1w, iv_1m, iv_slope, voi_ratio,
            sector, industry,
            actual_move_pct, contract_return_pct
        FROM zerodte_scans
        ORDER BY scan_date ASC, composite_score DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _update_outcomes(outcomes: dict[tuple, float]) -> None:
    """Write computed contract_return_pct back to database."""
    if not DB_PATH.exists() or not outcomes:
        return
    conn = sqlite3.connect(str(DB_PATH))
    for (contract_code, scan_ts), ret_pct in outcomes.items():
        conn.execute(
            "UPDATE zerodte_scans SET contract_return_pct=? WHERE contract_code=? AND scan_ts=?",
            (ret_pct, contract_code, scan_ts),
        )
    conn.commit()
    conn.close()
    print(f"[INFO] Updated {len(outcomes)} contract outcomes in DB")


# ─────────────────────────────────────────────────────────────────────────────
# Outcome computation via yfinance
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_expiry_prices(symbol_expiry_pairs: set[tuple[str, str]]) -> dict[tuple[str, str], float | None]:
    """
    Fetch the closing price for each (symbol, expiry_date) pair from yfinance.
    expiry_date is the option expiry date (YYYY-MM-DD string).
    Returns {(symbol, expiry_date): close_price}.
    """
    if not _HAS_YF:
        return {}

    # Group by symbol to minimise yfinance calls
    by_symbol: dict[str, set[str]] = {}
    for sym, exp in symbol_expiry_pairs:
        by_symbol.setdefault(sym, set()).add(exp)

    results: dict[tuple[str, str], float | None] = {}

    for sym, expiries in by_symbol.items():
        # Skip invalid tickers
        if "/" in sym:
            for exp in expiries:
                results[(sym, exp)] = None
            continue

        # Fetch history covering all expiry dates for this symbol
        min_exp = min(expiries)
        max_exp = max(expiries)
        # Add a day buffer to ensure we get the close on max_exp
        start = min_exp
        end_dt = datetime.strptime(max_exp, "%Y-%m-%d") + timedelta(days=3)
        end = end_dt.strftime("%Y-%m-%d")

        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(start=start, end=end)
            # Normalise index to date strings
            hist.index = [d.strftime("%Y-%m-%d") for d in hist.index]
            for exp in expiries:
                close = None
                # Try the expiry date first, then the nearest trading day
                if exp in hist.index:
                    close = float(hist.loc[exp, "Close"])
                else:
                    # option expiry on a weekend? pick next trading day close
                    exp_dt = datetime.strptime(exp, "%Y-%m-%d")
                    for offset in (1, -1, 2, -2):
                        candidate = (exp_dt + timedelta(days=offset)).strftime("%Y-%m-%d")
                        if candidate in hist.index:
                            close = float(hist.loc[candidate, "Close"])
                            break
                results[(sym, exp)] = close
            time.sleep(0.2)
        except Exception as exc:
            print(f"[WARN] yfinance fetch failed for {sym}: {exc}")
            for exp in expiries:
                results[(sym, exp)] = None

    return results


def _compute_contract_returns(
    rows: list[dict],
    expiry_prices: dict[tuple[str, str], float | None],
) -> list[dict]:
    """
    Attach contract_return_pct to each row.
    If contract_return_pct is already set in DB, keep it.
    Otherwise compute from intrinsic value at expiry.
    """
    enriched = []
    for r in rows:
        row = dict(r)
        if row.get("contract_return_pct") is not None:
            enriched.append(row)
            continue

        sym = row["symbol"]
        exp = row["expiry"]
        direction = (row["direction"] or "").lower()
        strike = row.get("strike") or 0.0
        premium = row.get("premium") or 0.0

        exp_close = expiry_prices.get((sym, exp))
        if exp_close is None or premium <= 0:
            row["contract_return_pct"] = None  # still pending
            row["expired_itm"] = None
            enriched.append(row)
            continue

        if direction == "call":
            intrinsic = max(0.0, exp_close - strike)
        else:  # put
            intrinsic = max(0.0, strike - exp_close)

        return_pct = (intrinsic / premium - 1.0) * 100.0
        row["contract_return_pct"] = round(return_pct, 2)
        row["expired_itm"] = intrinsic > 0
        enriched.append(row)

    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# Bucket assignment
# ─────────────────────────────────────────────────────────────────────────────

def _assign_bucket(score: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= score < hi:
            return name
    return "APEX"


# ─────────────────────────────────────────────────────────────────────────────
# Statistics computation (vectorbt + numpy)
# ─────────────────────────────────────────────────────────────────────────────

def _bucket_stats(rows_with_outcomes: list[dict]) -> dict[str, dict]:
    """
    Compute per-bucket statistics.
    Returns {bucket_name: {count, resolved, win_rate, avg_return, ...}}.
    """
    from collections import defaultdict
    bucket_rows: dict[str, list[dict]] = defaultdict(list)

    for row in rows_with_outcomes:
        score = row.get("composite_score") or 0.0
        b = _assign_bucket(score)
        bucket_rows[b].append(row)

    stats: dict[str, dict] = {}
    for name, _, _ in BUCKETS:
        rows = bucket_rows.get(name, [])
        resolved = [r for r in rows if r.get("contract_return_pct") is not None]
        returns = [r["contract_return_pct"] for r in resolved]
        wins = [r for r in resolved if r.get("contract_return_pct", -100) > 0]
        premiums = [r.get("premium") or 0.0 for r in rows]
        scores = [r.get("composite_score") or 0.0 for r in rows]
        avg_ret = sum(returns) / len(returns) if returns else None
        avg_premium = sum(premiums) / len(premiums) if premiums else None
        avg_score = sum(scores) / len(scores) if scores else None

        # vectorbt equity curve (per-$1 invested per contract)
        equity_curve = None
        sharpe = None
        max_dd = None
        if _HAS_VBT and _HAS_PANDAS and _HAS_NUMPY and len(returns) >= 2:
            try:
                ret_series = pd.Series(
                    [(1.0 + r / 100.0) for r in returns],
                    name=name,
                )
                equity = ret_series.cumprod()
                equity_curve = equity.tolist()

                # Sharpe (annualised, assume 252 trading days, each trade is independent)
                ret_arr = np.array(returns) / 100.0
                if ret_arr.std() > 0:
                    sharpe = float(ret_arr.mean() / ret_arr.std() * (252 ** 0.5))

                # Max drawdown
                roll_max = equity.cummax()
                dd = (equity - roll_max) / roll_max
                max_dd = float(dd.min() * 100.0)
            except Exception as exc:
                print(f"[WARN] vectorbt stats failed for {name}: {exc}")

        stats[name] = {
            "bucket": name,
            "count": len(rows),
            "resolved": len(resolved),
            "pending": len(rows) - len(resolved),
            "wins": len(wins),
            "losses": len(resolved) - len(wins),
            "win_rate_pct": round(100.0 * len(wins) / len(resolved), 1) if resolved else None,
            "avg_return_pct": round(avg_ret, 1) if avg_ret is not None else None,
            "min_return_pct": round(min(returns), 1) if returns else None,
            "max_return_pct": round(max(returns), 1) if returns else None,
            "avg_premium": round(avg_premium, 2) if avg_premium is not None else None,
            "avg_score": round(avg_score, 1) if avg_score is not None else None,
            "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "max_drawdown_pct": round(max_dd, 1) if max_dd is not None else None,
            "equity_curve": equity_curve,
            "sample_rows": [
                {
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "strike": r["strike"],
                    "expiry": r["expiry"],
                    "dte": r["dte"],
                    "premium": r["premium"],
                    "composite_score": r["composite_score"],
                    "contract_return_pct": r.get("contract_return_pct"),
                    "expired_itm": r.get("expired_itm"),
                    "scan_date": r["scan_date"],
                }
                for r in rows
            ],
        }

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# HTML report generation
# ─────────────────────────────────────────────────────────────────────────────

def _pct_badge(pct: float | None) -> str:
    if pct is None:
        return "<span style='color:#aaa'>—</span>"
    color = "#198754" if pct >= 0 else "#dc3545"
    sign = "+" if pct >= 0 else ""
    return f"<span style='color:{color};font-weight:600'>{sign}{pct:.1f}%</span>"


def _bucket_summary_table(stats: dict[str, dict]) -> str:
    rows_html = ""
    for name, lo, _ in BUCKETS:
        s = stats.get(name, {})
        clr = BUCKET_COLOURS.get(name, "#333")
        win_rate = f"{s['win_rate_pct']:.0f}%" if s.get("win_rate_pct") is not None else "—"
        avg_ret = _pct_badge(s.get("avg_return_pct"))
        max_ret = _pct_badge(s.get("max_return_pct"))
        sharpe = f"{s['sharpe']:.2f}" if s.get("sharpe") is not None else "—"
        max_dd = f"{s['max_drawdown_pct']:.1f}%" if s.get("max_drawdown_pct") is not None else "—"
        rows_html += f"""
            <tr>
                <td><span style='background:{clr};color:#fff;padding:2px 8px;border-radius:12px;font-size:0.85em'>{name}</span></td>
                <td style='text-align:right'>{s.get('count', 0)}</td>
                <td style='text-align:right'>{s.get('resolved', 0)}</td>
                <td style='text-align:right'>{s.get('pending', 0)}</td>
                <td style='text-align:right'>{win_rate}</td>
                <td style='text-align:right'>{avg_ret}</td>
                <td style='text-align:right'>{max_ret}</td>
                <td style='text-align:right'>{s.get('avg_premium', '—') if s.get('avg_premium') is not None else '—'}</td>
                <td style='text-align:right'>{s.get('avg_score', '—') if s.get('avg_score') is not None else '—'}</td>
                <td style='text-align:right'>{sharpe}</td>
                <td style='text-align:right'>{max_dd}</td>
            </tr>"""
    return f"""
    <table style='width:100%;border-collapse:collapse;font-size:0.88em'>
        <thead>
            <tr style='background:#f1f3f5;'>
                <th style='padding:8px;text-align:left'>Bucket</th>
                <th style='padding:8px;text-align:right'>Total</th>
                <th style='padding:8px;text-align:right'>Resolved</th>
                <th style='padding:8px;text-align:right'>Pending</th>
                <th style='padding:8px;text-align:right'>Win Rate</th>
                <th style='padding:8px;text-align:right'>Avg Return</th>
                <th style='padding:8px;text-align:right'>Best Return</th>
                <th style='padding:8px;text-align:right'>Avg Premium</th>
                <th style='padding:8px;text-align:right'>Avg Score</th>
                <th style='padding:8px;text-align:right'>Sharpe</th>
                <th style='padding:8px;text-align:right'>Max DD</th>
            </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
    </table>"""


def _contracts_table(rows: list[dict]) -> str:
    """Top 50 contracts ranked by return_pct desc."""
    resolved = [r for r in rows if r.get("contract_return_pct") is not None]
    resolved.sort(key=lambda r: r["contract_return_pct"], reverse=True)
    top = resolved[:50]

    rows_html = ""
    for r in top:
        bucket = _assign_bucket(r.get("composite_score") or 0.0)
        clr = BUCKET_COLOURS.get(bucket, "#333")
        ret = r.get("contract_return_pct")
        itm = "✓ ITM" if r.get("expired_itm") else "✗ OTM"
        itm_clr = "#198754" if r.get("expired_itm") else "#dc3545"
        rows_html += f"""
            <tr>
                <td style='padding:6px'>{r['scan_date']}</td>
                <td style='padding:6px;font-weight:600'>{r['symbol']}</td>
                <td style='padding:6px'>{r['direction'].upper() if r.get('direction') else '—'}</td>
                <td style='padding:6px;text-align:right'>${r['strike']:.0f}</td>
                <td style='padding:6px'>{r['expiry']}</td>
                <td style='padding:6px;text-align:right'>{r['dte']}</td>
                <td style='padding:6px;text-align:right'>${r.get('premium', 0):.2f}</td>
                <td style='padding:6px;text-align:right'>{r.get('composite_score', 0):.0f}</td>
                <td style='padding:6px;text-align:center'><span style='background:{clr};color:#fff;padding:1px 6px;border-radius:10px;font-size:0.8em'>{bucket}</span></td>
                <td style='padding:6px;text-align:center;color:{itm_clr}'>{itm}</td>
                <td style='padding:6px;text-align:right'>{_pct_badge(ret)}</td>
            </tr>"""

    pending_count = sum(1 for r in rows if r.get("contract_return_pct") is None)
    pending_note = (
        f"<p style='color:#666;font-size:0.85em;margin-top:4px'>"
        f"⚠ {pending_count} contract(s) pending outcome (expiry date not yet reached or yfinance data unavailable)</p>"
        if pending_count > 0 else ""
    )

    return f"""
    {pending_note}
    <table style='width:100%;border-collapse:collapse;font-size:0.85em'>
        <thead>
            <tr style='background:#f1f3f5'>
                <th style='padding:7px;text-align:left'>Scan Date</th>
                <th style='padding:7px;text-align:left'>Symbol</th>
                <th style='padding:7px;text-align:left'>Type</th>
                <th style='padding:7px;text-align:right'>Strike</th>
                <th style='padding:7px;text-align:left'>Expiry</th>
                <th style='padding:7px;text-align:right'>DTE</th>
                <th style='padding:7px;text-align:right'>Premium</th>
                <th style='padding:7px;text-align:right'>Score</th>
                <th style='padding:7px;text-align:center'>Bucket</th>
                <th style='padding:7px;text-align:center'>Expiry Result</th>
                <th style='padding:7px;text-align:right'>Return</th>
            </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
    </table>"""


def _generate_html(
    stats: dict[str, dict],
    rows: list[dict],
    generated_at: str,
    db_rows_total: int,
) -> str:
    resolved_total = sum(s.get("resolved", 0) for s in stats.values())
    pending_total = db_rows_total - resolved_total
    scan_dates = sorted(set(r["scan_date"] for r in rows))

    vbt_note = "vectorbt ✓" if _HAS_VBT else "vectorbt not installed — basic stats only"
    yf_note = "yfinance ✓" if _HAS_YF else "yfinance not installed"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>ZeroDTE Vectorbt Backtest</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                margin: 0; padding: 20px; background: #f8f9fa; color: #212529; }}
        .card {{ background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 20px;
                 box-shadow: 0 1px 6px rgba(0,0,0,.08); }}
        h1 {{ margin: 0 0 4px; font-size: 1.5em; }}
        h2 {{ font-size: 1.1em; border-bottom: 2px solid #dee2e6; padding-bottom: 6px; margin-bottom: 14px; }}
        .meta {{ color: #6c757d; font-size: 0.85em; margin-bottom: 20px; }}
        .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 16px; }}
        .stat-box {{ background: #f8f9fa; border-radius: 8px; padding: 10px; text-align: center; }}
        .stat-box .val {{ font-size: 1.6em; font-weight: 700; }}
        .stat-box .lbl {{ font-size: 0.75em; color: #6c757d; margin-top: 2px; }}
        table tr:nth-child(even) {{ background: #f9f9f9; }}
        table th {{ font-weight: 600; color: #495057; border-bottom: 2px solid #dee2e6; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>ZeroDTE Vectorbt Backtest</h1>
        <div class="meta">
            Generated {generated_at} &nbsp;|&nbsp; {vbt_note} &nbsp;|&nbsp; {yf_note}
            &nbsp;|&nbsp; Scan dates: {', '.join(scan_dates) if scan_dates else '—'}
        </div>
        <div class="stat-grid">
            <div class="stat-box"><div class="val">{db_rows_total}</div><div class="lbl">Total Contracts</div></div>
            <div class="stat-box"><div class="val">{resolved_total}</div><div class="lbl">Resolved</div></div>
            <div class="stat-box"><div class="val">{pending_total}</div><div class="lbl">Pending</div></div>
            <div class="stat-box"><div class="val">{len(scan_dates)}</div><div class="lbl">Scan Dates</div></div>
        </div>
    </div>

    <div class="card">
        <h2>Score Bucket Performance</h2>
        <p style="color:#6c757d;font-size:0.82em">
            Score buckets: LOW (0–30) &nbsp; MED (30–45) &nbsp; HIGH (45–60) &nbsp; APEX (60+).
            Entry price = <strong>premium</strong> (mid-price at scan time).
            Exit value = intrinsic value at expiry close (yfinance).
            Max loss = −100% (option expires worthless).
        </p>
        {_bucket_summary_table(stats)}
    </div>

    <div class="card">
        <h2>All Contracts — Ranked by Return</h2>
        {_contracts_table(rows)}
    </div>

    <div class="card">
        <h2>Score Distribution</h2>
        <table style='width:100%;border-collapse:collapse;font-size:0.85em'>
            <thead><tr style='background:#f1f3f5'>
                <th style='padding:7px;text-align:left'>Bucket</th>
                <th style='padding:7px;text-align:left'>Symbol</th>
                <th style='padding:7px;text-align:left'>Direction</th>
                <th style='padding:7px;text-align:right'>Score</th>
                <th style='padding:7px;text-align:right'>Premium</th>
                <th style='padding:7px;text-align:right'>OTM %</th>
                <th style='padding:7px;text-align:right'>DTE</th>
                <th style='padding:7px;text-align:left'>Expiry</th>
                <th style='padding:7px;text-align:right'>Return</th>
            </tr></thead>
            <tbody>
                {"".join(_score_dist_row(r) for r in rows)}
            </tbody>
        </table>
    </div>
</body>
</html>"""


def _score_dist_row(r: dict) -> str:
    score = r.get("composite_score") or 0.0
    bucket = _assign_bucket(score)
    clr = BUCKET_COLOURS.get(bucket, "#333")
    ret = r.get("contract_return_pct")
    return (
        f"<tr>"
        f"<td style='padding:5px'><span style='background:{clr};color:#fff;padding:1px 6px;border-radius:10px;font-size:0.78em'>{bucket}</span></td>"
        f"<td style='padding:5px;font-weight:600'>{r['symbol']}</td>"
        f"<td style='padding:5px'>{r.get('direction','').upper()}</td>"
        f"<td style='padding:5px;text-align:right'>{score:.0f}</td>"
        f"<td style='padding:5px;text-align:right'>${r.get('premium',0):.2f}</td>"
        f"<td style='padding:5px;text-align:right'>{r.get('otm_pct',0):.1f}%</td>"
        f"<td style='padding:5px;text-align:right'>{r.get('dte','—')}</td>"
        f"<td style='padding:5px'>{r.get('expiry','—')}</td>"
        f"<td style='padding:5px;text-align:right'>{_pct_badge(ret)}</td>"
        f"</tr>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(out_html: str, out_json: str) -> None:
    print("[ZeroDTE Backtest] Loading scan data from database...")
    rows = _load_scans()
    if not rows:
        print("[ERROR] No rows found in zerodte_scans. Run the scanner first.")
        return

    print(f"[ZeroDTE Backtest] {len(rows)} contracts loaded.")

    # Compute outcomes for contracts without contract_return_pct
    need_outcomes = [r for r in rows if r.get("contract_return_pct") is None]
    if need_outcomes and _HAS_YF:
        print(f"[ZeroDTE Backtest] Fetching yfinance outcomes for {len(need_outcomes)} contracts...")
        pairs: set[tuple[str, str]] = {
            (r["symbol"], r["expiry"])
            for r in need_outcomes
            if "/" not in r["symbol"]
        }
        expiry_prices = _fetch_expiry_prices(pairs)
        rows = _compute_contract_returns(rows, expiry_prices)

        # Persist computed outcomes back to DB
        new_outcomes = {
            (r["contract_code"], r["scan_ts"]): r["contract_return_pct"]
            for r in rows
            if r.get("contract_return_pct") is not None
            and r.get("contract_code")
        }
        _update_outcomes(new_outcomes)
    elif not _HAS_YF:
        print("[WARN] yfinance not installed — cannot compute outcomes. Install with: pip install yfinance")

    # Compute bucket stats
    stats = _bucket_stats(rows)

    # Build JSON payload
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_resolved = sum(s.get("resolved", 0) for s in stats.values())
    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "db_path": str(DB_PATH),
        "total_contracts": len(rows),
        "resolved_contracts": total_resolved,
        "pending_contracts": len(rows) - total_resolved,
        "vectorbt_available": _HAS_VBT,
        "yfinance_available": _HAS_YF,
        "buckets": stats,
        "contracts": [
            {
                "symbol": r["symbol"],
                "direction": r.get("direction"),
                "option_type": r.get("option_type"),
                "scan_date": r.get("scan_date"),
                "expiry": r.get("expiry"),
                "dte": r.get("dte"),
                "strike": r.get("strike"),
                "spot_price": r.get("spot_price"),
                "otm_pct": r.get("otm_pct"),
                "bid": r.get("bid"),
                "ask": r.get("ask"),
                "premium": r.get("premium"),   # mid_price at scan time (ENTRY PRICE)
                "iv": r.get("iv"),
                "delta": r.get("delta"),
                "open_interest": r.get("open_interest"),
                "volume": r.get("volume"),
                "composite_score": r.get("composite_score"),
                "momentum_score": r.get("momentum_score"),
                "iv_back_score": r.get("iv_back_score"),
                "voi_score": r.get("voi_score"),
                "bucket": _assign_bucket(r.get("composite_score") or 0.0),
                "contract_return_pct": r.get("contract_return_pct"),
                "expired_itm": r.get("expired_itm"),
            }
            for r in rows
        ],
    }

    # Write JSON report
    out_json_path = Path(out_json)
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"[ZeroDTE Backtest] JSON report written: {out_json_path}")

    # Write HTML report
    out_html_path = Path(out_html)
    out_html_path.parent.mkdir(parents=True, exist_ok=True)
    html = _generate_html(stats, rows, generated_at, len(rows))
    out_html_path.write_text(html, encoding="utf-8")
    print(f"[ZeroDTE Backtest] HTML report written: {out_html_path}")

    # Summary to console
    print("\n=== ZeroDTE Backtest Summary ===")
    for name, _, _ in BUCKETS:
        s = stats.get(name, {})
        wr = f"{s['win_rate_pct']:.0f}%" if s.get("win_rate_pct") is not None else "pending"
        avg = f"{s['avg_return_pct']:+.1f}%" if s.get("avg_return_pct") is not None else "pending"
        print(f"  {name:6s}  contracts={s.get('count',0):3d}  win_rate={wr:>8s}  avg_return={avg:>10s}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZeroDTE Vectorbt Backtest")
    parser.add_argument(
        "--out",
        default=str(_ROOT_DIR / "reports" / "zerodte_backtest.html"),
        help="Path for HTML report output",
    )
    parser.add_argument(
        "--json",
        dest="out_json",
        default=str(_ROOT_DIR / "reports" / "zerodte_backtest.json"),
        help="Path for JSON report output",
    )
    args = parser.parse_args()
    main(args.out, args.out_json)
