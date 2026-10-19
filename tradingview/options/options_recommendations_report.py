"""
options_recommendations_report.py — US Options Recommendations Backtest Report
================================================================================
Generates a standalone HTML report analysing all US options recommendations
stored in alert_tracker.db → options_recommendations.

Filters to US-only via blocklist (excludes NSE/BSE market tags).

Win/Loss logic:
  • OPEN with current_premium → win if pnl_pct > 0
  • ITM_WIN / PREMIUM_WIN → win=1
  • OTM_LOSS / EXPIRED / DEEP_LOSS → win=0
  • OPEN without current_premium → pending (excluded from win rate)

Usage:
    python tradingview/options/options_recommendations_report.py
    python tradingview/options/options_recommendations_report.py --update-premiums
    python tradingview/options/options_recommendations_report.py --dry-run
"""

import sys
import os
import json
import argparse
import sqlite3
import math
import statistics
import subprocess
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone, date as _date
from collections import defaultdict

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
TV   = ROOT / "tradingview"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TV))

# Module-level today string for stale-open detection (BUG-002 fix)
_TODAY_STR = _date.today().isoformat()

try:
    from path_config import DB_ALERT_TRACKER, REPORTS
except ImportError:
    DB_ALERT_TRACKER = ROOT / "database" / "alert_tracker.db"
    REPORTS          = ROOT / "reports"

try:
    from scipy.stats import spearmanr as _spearmanr
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

_SQL_US_RECS = """
SELECT * FROM options_recommendations
WHERE (market IS NULL OR market NOT IN ('NSE', 'BSE'))
ORDER BY run_date DESC, rank ASC
"""

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _compute_be_sigma(r: dict) -> float | None:
    """Compute break-even / sigma ratio at runtime (NOT stored in DB)."""
    iv = r.get("iv_pct") or 0
    if iv > 1:
        iv /= 100  # unit check: TV stores as decimal
    dte  = r.get("dte_at_rec") or 0
    prem = r.get("premium") or 0
    spot = r.get("spot_price") or 0
    if spot <= 0 or dte <= 0 or iv <= 0:
        return None
    sigma_T = iv * math.sqrt(dte / 365.0)
    be_pct  = prem / spot
    ratio   = be_pct / sigma_T
    return None if not math.isfinite(ratio) else ratio


def _be_sigma_label(ratio: float | None) -> str:
    if ratio is None:
        return "N/A"
    if ratio < 0.40:
        return "< 0.40 (Cheap)"
    if ratio < 0.55:
        return "0.40-0.55 (Fair)"
    if ratio < 0.75:
        return "0.55-0.75 (Expensive)"
    return "> 0.75 (Reject)"


def _signal_bucket(score) -> str:
    if score is None:
        return "N/A"
    s = float(score)
    if s < 40:
        return "LOW (<40)"
    if s < 55:
        return "MED (40-55)"
    if s < 70:
        return "HIGH (55-70)"
    return "APEX (>=70)"


def _win_from_row(r: dict) -> int | None:
    outcome = (r.get("outcome") or "").upper()
    if outcome in ("ITM_WIN", "PREMIUM_WIN"):
        return 1
    if outcome in ("OTM_LOSS", "EXPIRED", "DEEP_LOSS"):
        return 0
    curr  = r.get("current_premium")
    entry = r.get("premium")
    if outcome == "OPEN" and curr is not None and entry is not None and entry > 0:
        return 1 if (curr - entry) > 0 else 0
    return None


def _pnl_pct_from_row(r: dict) -> float | None:
    outcome = (r.get("outcome") or "").upper()
    entry   = r.get("premium")
    curr    = r.get("current_premium")
    if entry is None or entry <= 0:
        return None
    if outcome in ("EXPIRED", "DEEP_LOSS"):
        return -100.0
    if curr is not None:
        return (curr - entry) / entry * 100.0
    return None


def _fmt(v, fmt=".1f", fallback="—") -> str:
    if v is None:
        return fallback
    try:
        return f"{v:{fmt}}"
    except Exception:
        return str(v)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─── Premium update (runs in a background thread when --update-premiums) ──────

def run_premium_update(dry_run: bool = False) -> str:
    """Call options_outcome_checker.py --update-premiums in a subprocess."""
    checker = TV / "options" / "options_outcome_checker.py"
    cmd = [sys.executable, str(checker), "--update-premiums"]
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=150, cwd=str(TV)
        )
        if result.returncode != 0:
            return f"ERROR (rc={result.returncode}): {(result.stderr or '').strip()[:200]}"
        lines = (result.stdout or "").strip().split("\n")
        return lines[-1] if lines else "done"
    except subprocess.TimeoutExpired:
        return "timeout after 150s"
    except Exception as exc:
        return f"exception: {exc}"


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_recs(db_path: str) -> list[dict]:
    """Load all US options recommendations from DB."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(_SQL_US_RECS).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["_be_sigma"] = _compute_be_sigma(d)
            d["_win"]      = _win_from_row(d)
            d["_pnl_pct"]  = _pnl_pct_from_row(d)
            result.append(d)
        return result
    finally:
        con.close()


# ─── Group stats helper ───────────────────────────────────────────────────────

def _group_stats(rows: list[dict], key_fn, min_trades: int = 3) -> list[dict]:
    """Aggregate win/loss stats per group key."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append(r)

    result = []
    for grp in sorted(buckets):
        grp_rows = buckets[grp]
        n        = len(grp_rows)
        resolved = [r for r in grp_rows if r["_win"] is not None]
        wins     = [r for r in resolved if r["_win"] == 1]
        losses   = [r for r in resolved if r["_win"] == 0]
        pnls     = [r["_pnl_pct"] for r in resolved if r["_pnl_pct"] is not None]
        oscores  = [r["option_score"] for r in grp_rows if r.get("option_score") is not None]
        sscores  = [r["signal_score"] for r in grp_rows if r.get("signal_score") is not None]
        pending  = n - len(resolved)
        result.append({
            "group":    grp,
            "n":        n,
            "resolved": len(resolved),
            "wins":     len(wins),
            "losses":   len(losses),
            "pending":  pending,
            "win_rate": len(wins) / len(resolved) * 100 if resolved else None,
            "avg_pnl":  sum(pnls) / len(pnls) if pnls else None,
            "avg_oscore": sum(oscores) / len(oscores) if oscores else None,
            "avg_sscore": sum(sscores) / len(sscores) if sscores else None,
        })
    return [g for g in result if g["n"] >= min_trades]


# ─── HTML section builders ────────────────────────────────────────────────────

def _css() -> str:
    return """
body{font-family:sans-serif;margin:0;padding:20px;background:#f5f5f5;color:#333}
h1{color:#2c3e50;margin-bottom:4px}
h2{color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:4px;margin-top:24px}
h3{color:#555;margin-bottom:8px}
.meta{color:#888;font-size:0.85em;margin-bottom:20px}
.card{background:#fff;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.kpi-strip{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.kpi{background:#fff;border-radius:8px;padding:14px 20px;min-width:120px;box-shadow:0 1px 3px rgba(0,0,0,.1);text-align:center}
.kpi .val{font-size:1.6em;font-weight:bold;color:#2c3e50}
.kpi .lbl{font-size:0.75em;color:#888;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:0.9em}
th,td{padding:7px 10px;border:1px solid #ddd}
th{background:#2c3e50;color:#fff;font-weight:bold;cursor:pointer;user-select:none}
th:hover{background:#1a2b3c}
tr:nth-child(even) td{background:#fafafa}
.badge{display:inline-block;border-radius:4px;padding:1px 7px;font-size:0.8em;font-weight:bold}
.badge-win{background:#d5f5e3;color:#1d8348}
.badge-loss{background:#fadbd8;color:#922b21}
.badge-open{background:#eaf4fb;color:#1a5276}
.badge-stale{background:#fef9e7;color:#9a7d0a;border:1px solid #f9e79f}
.badge-pend{background:#fef9e7;color:#7d6608}
.warn{background:#fff3cd;border-left:4px solid #f39c12;padding:10px 14px;border-radius:4px;margin:10px 0;font-size:0.9em}
.sort-ind{font-size:0.75em;color:#aaa;margin-left:2px}
details{margin-bottom:12px}
details summary{cursor:pointer;font-weight:bold;color:#2980b9;padding:6px 0}
.pnl-pos{color:#1d8348;font-weight:bold}
.pnl-neg{color:#922b21;font-weight:bold}
.filter-bar{margin-bottom:8px}
.filter-bar input{padding:6px 10px;border:1px solid #ccc;border-radius:4px;width:260px;font-size:0.9em}
"""


def _js() -> str:
    return """
function sortTbl(tblId,col,isNum){
  var tb=document.getElementById(tblId);if(!tb)return;
  var rows=Array.from(tb.tBodies[0].rows);
  var prev=tb.dataset.sortCol,dir=(String(prev)===String(col)&&tb.dataset.sortDir==='asc')?'desc':'asc';
  tb.dataset.sortCol=col;tb.dataset.sortDir=dir;
  rows.sort(function(a,b){
    var av=a.cells[col]?(a.cells[col].getAttribute('data-val')||a.cells[col].textContent.trim()):'';
    var bv=b.cells[col]?(b.cells[col].getAttribute('data-val')||b.cells[col].textContent.trim()):'';
    if(isNum){av=parseFloat(av)||0;bv=parseFloat(bv)||0;}
    return (dir==='asc'?1:-1)*(av>bv?1:av<bv?-1:0);
  });
  rows.forEach(function(r){tb.tBodies[0].appendChild(r);});
  tb.querySelectorAll('th').forEach(function(th,i){
    var ind=th.querySelector('.sort-ind');
    if(ind)ind.innerHTML=(String(i)===String(col))?(dir==='asc'?'&#9650;':'&#9660;'):'&#8645;';
  });
}
function filterTbl(inputId,tblId){
  var q=document.getElementById(inputId).value.toLowerCase();
  var rows=Array.from(document.getElementById(tblId).tBodies[0].rows);
  rows.forEach(function(r){
    var txt=r.textContent.toLowerCase();
    r.style.display=(!q||txt.indexOf(q)>=0)?'':'none';
  });
}
"""


def _kpi_strip_html(rows: list[dict], total_db: int) -> str:
    us_count   = len(rows)
    open_recs  = [r for r in rows if (r.get("outcome") or "OPEN").upper() == "OPEN"]
    resolved   = [r for r in rows if r["_win"] is not None]
    wins       = [r for r in resolved if r["_win"] == 1]
    n_premium  = sum(1 for r in open_recs if r.get("current_premium") is not None)
    pnls       = [r["_pnl_pct"] for r in resolved if r["_pnl_pct"] is not None]
    sscores    = [r["signal_score"] for r in rows if r.get("signal_score") is not None]
    oscores    = [r["option_score"] for r in rows if r.get("option_score") is not None]
    # BUG-002: count stale opens (outcome=OPEN but expiry < today)
    stale_open = [r for r in open_recs if r.get("expiry") and str(r["expiry"]) < _TODAY_STR]

    win_rate   = len(wins) / len(resolved) * 100 if resolved else None
    # BUG-003: use trimmed mean (drop top/bottom 5%) to avoid outlier distortion; also compute median
    def _trimmed_mean(vals: list[float], pct: float = 0.05) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        cut = max(1, int(n * pct))
        trimmed = s[cut:-cut] if cut * 2 < n else s
        return sum(trimmed) / len(trimmed)
    avg_pnl    = _trimmed_mean(pnls)  # trimmed mean (outlier-resistant)
    med_pnl    = statistics.median(pnls) if pnls else None
    avg_ss     = sum(sscores) / len(sscores) if sscores else None
    avg_os     = sum(oscores) / len(oscores) if oscores else None
    data_as_of = max((r.get("run_date") or "" for r in rows), default="—")

    def kpi(val, lbl, sub: str = ""):
        sub_html = f'<div class="kpi-sub" style="font-size:0.7em;color:#aaa;margin-top:1px">{_esc(sub)}</div>' if sub else ""
        return f'<div class="kpi"><div class="val">{_esc(val)}</div><div class="lbl">{_esc(lbl)}</div>{sub_html}</div>'

    def _pnl_str(v: float | None) -> str:
        if v is None: return '—'
        return f'+{v:.1f}%' if v >= 0 else f'{v:.1f}%'

    pnl_str  = _pnl_str(avg_pnl)
    med_str  = _pnl_str(med_pnl)
    wr_str   = f'{win_rate:.1f}%' if win_rate is not None else '—'
    open_lbl = f"Open ({len(stale_open)} stale)" if stale_open else "Open"
    items = [
        kpi(str(total_db),          "Total DB"),
        kpi(str(us_count),          "US Only"),
        kpi(str(len(open_recs)),    open_lbl),
        kpi(str(len(resolved)),     "Resolved"),
        kpi(wr_str,                 "Win Rate"),
        kpi(pnl_str,                "Avg P&L% (trimmed)", sub=f"median: {med_str}"),
        kpi(f'{avg_ss:.1f}' if avg_ss is not None else '—',  "Avg Sig Score"),
        kpi(f'{avg_os:.1f}' if avg_os is not None else '—',  "Avg Opt Score"),
        kpi(str(n_premium),         "Open w/ Premium"),
        kpi(str(data_as_of),        "Data As Of"),
    ]
    strip = '<div class="kpi-strip">' + "".join(items) + '</div>'

    # Data completeness warning
    warn = ""
    if len(open_recs) > 0 and n_premium < len(open_recs) * 0.5:
        warn += (f'<div class="warn">⚠ Only {n_premium}/{len(open_recs)} open recommendations '
                 f'have current premium data. Run with <code>--update-premiums</code> to refresh. '
                 f'Win-rate on open trades may be understated.</div>')
    if stale_open:
        warn += (f'<div class="warn">⚠ {len(stale_open)} open recommendations have past expiry dates '
                 f'and may not yet be resolved by the outcome checker. '
                 f'Run the Outcome Checker (menu 10d) to update them.</div>')
    return strip + warn


def _breakdown_html(title: str, rows: list[dict], key_fn, min_trades: int) -> str:
    stats = _group_stats(rows, key_fn, min_trades=min_trades)
    if not stats:
        return f'<details><summary>{_esc(title)}</summary><p class="meta">No groups with &ge;{min_trades} trades.</p></details>'

    def _pnl_cell(v):
        if v is None:
            return "<td>—</td>"
        cls = "pnl-pos" if v >= 0 else "pnl-neg"
        sign = "+" if v >= 0 else ""
        return f'<td class="{cls}">{sign}{v:.1f}%</td>'

    rows_html = ""
    for g in stats:
        wr = f'{g["win_rate"]:.1f}%' if g["win_rate"] is not None else '—'
        rows_html += (
            f'<tr><td>{_esc(str(g["group"]))}</td>'
            f'<td>{g["n"]}</td>'
            f'<td>{g["resolved"]}</td>'
            f'<td>{g["wins"]}</td>'
            f'<td>{g["losses"]}</td>'
            f'<td>{wr}</td>'
            + _pnl_cell(g["avg_pnl"])
            + f'<td>{_fmt(g["avg_oscore"])}</td>'
            f'<td>{_fmt(g["avg_sscore"])}</td>'
            f'<td>{g["pending"]}</td></tr>\n'
        )

    hdr = ('<tr><th>Group</th><th>N</th><th>Resolved</th><th>Wins</th>'
           '<th>Losses</th><th>Win Rate%</th><th>Avg P&L%</th>'
           '<th>Avg OScore</th><th>Avg SigScore</th><th>Pending</th></tr>')
    return (
        f'<details><summary>{_esc(title)}</summary>'
        '<div class="card" style="margin-top:8px">'
        f'<table><thead>{hdr}</thead><tbody>{rows_html}</tbody></table>'
        '</div></details>'
    )


def _spearman_html(rows: list[dict]) -> str:
    """Spearman correlation table for 12 predictors vs win outcome."""
    resolved = [r for r in rows if r["_win"] is not None]
    n = len(resolved)

    if n < 10:
        return (
            '<div class="card"><h2>Parameter Analysis — Spearman Correlations</h2>'
            f'<p class="meta">Insufficient data (n={n}, need &ge;10 for analysis).</p></div>'
        )

    warning = ""
    if n < 30:
        warning = (
            f'<div class="warn">⚠ Insufficient data for reliable statistics '
            f'(n={n}, need &ge;30). Treat correlations as exploratory only.</div>'
        )

    predictors = [
        ("option_score",         "Option Score",       False),
        ("signal_score",         "Signal Score",       False),
        ("moneyball_score",      "Moneyball Score",    False),
        ("_abs_delta",           "abs(delta)",         False),
        ("iv_pct",               "IV %",               False),
        ("spread_pct",           "Spread %",           False),
        ("dte_at_rec",           "DTE at Rec",         False),
        ("vix_at_rec",           "VIX at Rec",         False),
        ("pcr",                  "PCR",                False),
        ("news_sentiment_score", "News Sentiment",     False),
        ("moneyness_pct",        "Moneyness %",        False),
        ("_be_sigma",            "BE/σ Ratio",         False),
    ]

    # Compute abs_delta once
    for r in resolved:
        d = r.get("delta")
        r["_abs_delta"] = abs(d) if d is not None else None

    def _status_badge(p_val):
        if p_val is None:
            return '<span class="badge badge-pend">n/a</span>'
        if p_val < 0.01:
            return '<span class="badge badge-win">★★★ Strong</span>'
        if p_val < 0.05:
            return '<span class="badge badge-win" style="background:#d5f5e3;color:#1d8348">★★ Moderate</span>'
        if p_val < 0.10:
            return '<span class="badge badge-pend">★ Weak</span>'
        return '<span class="badge badge-loss">✗ None</span>'

    if not _HAS_SCIPY:
        return (
            '<div class="card"><h2>Parameter Analysis — Spearman Correlations</h2>'
            '<div class="warn">scipy not installed — install with '
            '<code>pip install scipy</code> to enable correlation analysis.</div></div>'
        )

    rows_html = ""
    for col_key, col_label, _ in predictors:
        vals = []
        y    = []
        for r in resolved:
            v = r.get(col_key)
            if v is not None:
                vals.append(float(v))
                y.append(float(r["_win"]))

        if len(vals) < 10:
            rows_html += (
                f'<tr><td>{_esc(col_label)}</td><td colspan="4" style="color:#aaa">'
                f'Insufficient data (n={len(vals)})</td></tr>\n'
            )
            continue

        rho, pval = _spearmanr(vals, y)
        direction = "&#8593; Higher &#8594; Better" if rho > 0 else "&#8595; Higher &#8594; Worse"
        rows_html += (
            f'<tr><td>{_esc(col_label)}</td>'
            f'<td data-val="{rho:.4f}">{rho:.4f}</td>'
            f'<td>{pval:.4f}</td>'
            f'<td>{_status_badge(pval)}</td>'
            f'<td>{direction}</td>'
            f'<td>{len(vals)}</td></tr>\n'
        )

    hdr = ('<tr><th onclick="sortTbl(\'spearman\',0,false)">Predictor<span class="sort-ind">&#8645;</span></th>'
           '<th onclick="sortTbl(\'spearman\',1,true)">Spearman r<span class="sort-ind">&#8645;</span></th>'
           '<th onclick="sortTbl(\'spearman\',2,true)">p-value<span class="sort-ind">&#8645;</span></th>'
           '<th>Significance</th><th>Direction</th>'
           '<th onclick="sortTbl(\'spearman\',5,true)">N<span class="sort-ind">&#8645;</span></th></tr>')

    return (
        '<div class="card">'
        '<h2>Parameter Analysis &#8212; Spearman Correlations vs Win</h2>'
        f'{warning}'
        f'<p class="meta">Resolved trades: {n}. Correlation of each predictor with binary win/loss outcome.</p>'
        f'<table id="spearman"><thead>{hdr}</thead><tbody>{rows_html}</tbody></table>'
        '</div>'
    )


def _distribution_html(rows: list[dict]) -> str:
    """Inline SVG P&L distribution histogram (8 bins)."""
    bins = [
        ("<-80",     float("-inf"),  -80),
        ("-80 to -50", -80,         -50),
        ("-50 to -20", -50,         -20),
        ("-20 to 0",   -20,           0),
        ("0 to +20",     0,          20),
        ("+20 to +50",  20,          50),
        ("+50 to +100", 50,         100),
        (">+100",      100, float("inf")),
    ]
    pnls = [r["_pnl_pct"] for r in rows if r["_pnl_pct"] is not None]
    if not pnls:
        return '<div class="card"><h2>P&amp;L Distribution</h2><p class="meta">No P&amp;L data available.</p></div>'

    counts = [sum(1 for p in pnls if lo <= p < hi) for _, lo, hi in bins]
    max_c  = max(counts) or 1
    svg_h, bar_w, gap = 120, 54, 8
    svg_w = len(bins) * (bar_w + gap)

    bars = ""
    for i, (lbl, lo, hi) in enumerate(bins):
        cnt   = counts[i]
        bh    = max(2, int(cnt / max_c * svg_h))
        x     = i * (bar_w + gap)
        y     = svg_h - bh
        color = "#1d8348" if lo >= 0 else "#c0392b"
        bars += (
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bh}" fill="{color}" opacity="0.8"/>'
            f'<text x="{x + bar_w//2}" y="{svg_h + 14}" text-anchor="middle" font-size="9" fill="#555">'
            f'{_esc(lbl)}</text>'
            f'<text x="{x + bar_w//2}" y="{y - 3}" text-anchor="middle" font-size="10" fill="#333">'
            f'{cnt}</text>'
        )

    svg = (
        f'<svg width="{svg_w}" height="{svg_h + 30}" '
        f'style="overflow:visible;display:block;margin:0 auto">'
        f'{bars}</svg>'
    )
    return (
        '<div class="card"><h2>P&amp;L Distribution</h2>'
        f'<p class="meta">Resolved trades with P&amp;L data: {len(pnls)}</p>'
        f'{svg}</div>'
    )


def _monthly_trend_html(rows: list[dict]) -> str:
    """Month-by-month win rate trend."""
    monthly: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        rd = (r.get("run_date") or "")[:7]  # YYYY-MM
        if rd:
            monthly[rd].append(r)

    months = sorted(monthly)
    if not months:
        return ""

    rows_html = ""
    for m in months:
        m_rows  = monthly[m]
        res     = [r for r in m_rows if r["_win"] is not None]
        wins    = [r for r in res if r["_win"] == 1]
        wr      = f'{len(wins)/len(res)*100:.1f}%' if res else '—'
        rows_html += (
            f'<tr><td>{m}</td><td>{len(m_rows)}</td>'
            f'<td>{len(res)}</td><td>{wr}</td></tr>\n'
        )

    hdr = '<tr><th>Month</th><th>N Recs</th><th>Resolved</th><th>Win Rate%</th></tr>'
    return (
        '<div class="card"><h2>Monthly Win Rate Trend</h2>'
        f'<table><thead>{hdr}</thead><tbody>{rows_html}</tbody></table></div>'
    )


def _all_trades_html(rows: list[dict]) -> str:
    """Full sortable trades table with all key columns."""
    tbl_id = "all_trades"
    headers = [
        ("Date",      0,  False), ("Symbol",    1,  False),
        ("Type",      2,  False), ("Mode",       3,  False),
        ("Strike",    4,  True),  ("Expiry",     5,  False),
        ("DTE",       6,  True),  ("BE-σ",       7,  True),
        ("Entry$",    8,  True),  ("Curr$",      9,  True),
        ("P&L%",     10,  True),  ("Delta",     11,  True),
        ("IV%",      12,  True),  ("OScore",    13,  True),
        ("SigScore", 14,  True),  ("VIX",       15,  True),
        ("Outcome",  16,  False),
    ]

    hdr_html = "".join(
        '<th onclick="sortTbl(\'{}\',{},{})">'.format(tbl_id, i, "true" if n else "false")
        + f'{_esc(lbl)}<span class="sort-ind">&#8645;</span></th>'
        for lbl, i, n in headers
    )

    rows_html = ""
    for r in rows:
        outcome_raw = (r.get("outcome") or "OPEN").upper()
        # BUG-002: mark expired-but-unresolved rows visually
        expiry_s = str(r.get("expiry") or "")
        outcome  = outcome_raw
        if outcome_raw == "OPEN" and expiry_s and expiry_s < _TODAY_STR:
            outcome = "EXPIRED?"
        pnl      = r["_pnl_pct"]
        be_sig   = r["_be_sigma"]
        curr_p   = r.get("current_premium")
        pnl_str  = ("+" if pnl is not None and pnl >= 0 else "") + f'{pnl:.1f}%' if pnl is not None else "—"
        pnl_cls  = "pnl-pos" if (pnl is not None and pnl >= 0) else ("pnl-neg" if pnl is not None else "")
        badge_cls = (
            "badge-win" if outcome in ("ITM_WIN", "PREMIUM_WIN") else
            "badge-loss" if outcome in ("OTM_LOSS", "EXPIRED", "DEEP_LOSS") else
            "badge-stale" if outcome == "EXPIRED?" else
            "badge-open"
        )
        rows_html += (
            f'<tr>'
            f'<td>{_esc(r.get("run_date") or "")}</td>'
            f'<td>{_esc(r.get("tv_symbol") or "")}</td>'
            f'<td>{_esc(r.get("option_type") or "")}</td>'
            f'<td>{_esc(r.get("mode") or "")}</td>'
            f'<td data-val="{r.get("strike") or 0}">{_fmt(r.get("strike"), ".2f")}</td>'
            f'<td>{_esc(r.get("expiry") or "")}</td>'
            f'<td data-val="{r.get("dte_at_rec") or 0}">{r.get("dte_at_rec") or "—"}</td>'
            f'<td data-val="{be_sig if be_sig is not None else -999}">'
            f'{"—" if be_sig is None else f"{be_sig:.3f}"}</td>'
            f'<td data-val="{r.get("premium") or 0}">{_fmt(r.get("premium"), ".2f")}</td>'
            f'<td data-val="{curr_p if curr_p is not None else -999}">'
            f'{"—" if curr_p is None else f"${curr_p:.2f}"}</td>'
            f'<td data-val="{pnl if pnl is not None else -9999}" class="{pnl_cls}">{pnl_str}</td>'
            f'<td data-val="{r.get("delta") or 0}">{_fmt(r.get("delta"), ".3f")}</td>'
            f'<td data-val="{r.get("iv_pct") or 0}">{_fmt(r.get("iv_pct"), ".1f")}</td>'
            f'<td data-val="{r.get("option_score") or 0}">{_fmt(r.get("option_score"), ".1f")}</td>'
            f'<td data-val="{r.get("signal_score") or 0}">{_fmt(r.get("signal_score"), ".1f")}</td>'
            f'<td data-val="{r.get("vix_at_rec") or 0}">{_fmt(r.get("vix_at_rec"), ".1f")}</td>'
            f'<td><span class="badge {badge_cls}">{_esc(outcome)}</span></td>'
            f'</tr>\n'
        )

    return (
        '<div class="card">'
        '<details>'
        '<summary style="cursor:pointer;list-style:none;padding:4px 0">'
        '<h2 style="display:inline;margin:0">All Trades &#8212; Full Detail'
        ' <small style="font-size:0.75em;color:#888;font-weight:normal">(click to expand)</small>'
        '</h2></summary>'
        '<div class="filter-bar" style="margin-top:12px"><input type="text" id="sym-filter" '
        f'placeholder="Filter by symbol..." oninput="filterTbl(\'sym-filter\',\'{tbl_id}\')" /></div>'
        f'<table id="{tbl_id}"><thead><tr>{hdr_html}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table>'
        '</details></div>'
    )


def build_html(rows: list[dict], db_path: str, total_db: int, min_trades: int = 3, subtitle: str = "") -> str:
    """Assemble the full HTML report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = []
    sections.append(_kpi_strip_html(rows, total_db))
    sections.append(_spearman_html(rows))

    # 7 Win/Loss breakdowns
    sections.append("<h2>Win / Loss Breakdowns</h2>")
    sections.append(_breakdown_html(
        "By Mode",
        rows, lambda r: r.get("mode") or "UNKNOWN", min_trades))
    sections.append(_breakdown_html(
        "By Option Type",
        rows, lambda r: r.get("option_type") or "UNKNOWN", min_trades))
    sections.append(_breakdown_html(
        "By DTE Bucket",
        rows, lambda r: r.get("dte_bucket") or "UNKNOWN", min_trades))
    sections.append(_breakdown_html(
        "By Moneyness",
        rows, lambda r: r.get("moneyness_label") or "UNKNOWN", min_trades))
    sections.append(_breakdown_html(
        "By Signal Score Bucket",
        rows, lambda r: _signal_bucket(r.get("signal_score")), min_trades))
    sections.append(_breakdown_html(
        "By Earnings Risk",
        rows, lambda r: str(r.get("earnings_risk") or "NONE"), min_trades))
    sections.append(_breakdown_html(
        "By BE/\u03c3 Tier",
        rows, lambda r: _be_sigma_label(r.get("_be_sigma")), min_trades))

    sections.append(_monthly_trend_html(rows))
    sections.append(_distribution_html(rows))
    sections.append(_all_trades_html(rows))

    body = "\n".join(s for s in sections if s)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>US Options Recommendations Backtest Report{' — ' + subtitle if subtitle else ''}</title>
<style>{_css()}</style>
<script>{_js()}</script>
</head>
<body>
<h1>&#127919; US Options Recommendations Report{(' — ' + subtitle) if subtitle else ''}</h1>
<div class="meta">Generated: {now} &nbsp;&bull;&nbsp; DB: {_esc(db_path)}</div>
{body}
<hr style="margin-top:32px;border:none;border-top:1px solid #ddd"/>
<div class="meta" style="margin-top:8px">
Data source: alert_tracker.db &rarr; options_recommendations &nbsp;&bull;&nbsp;
US only (excludes NSE/BSE) &nbsp;&bull;&nbsp; Generated: {now}
</div>
</body>
</html>
"""


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate US options recommendations backtest HTML report"
    )
    parser.add_argument("--db",  default=str(DB_ALERT_TRACKER), help="Path to alert_tracker.db")
    parser.add_argument("--out", default=str(REPORTS / "options_recommendations_report.html"),
                        help="Output HTML path")
    parser.add_argument("--update-premiums", default=True,
                        action=argparse.BooleanOptionalAction,
                        help="Refresh current_premium for OPEN recs (default: on). Use --no-update-premiums to skip.")
    parser.add_argument("--min-option-score", type=float, default=60.0,
                        help="option_score threshold: >= this = actionable, < this = rejected (default: 60)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Pass --dry-run to run_premium_update (preview only)")
    parser.add_argument("--min-trades", type=int, default=3,
                        help="Minimum trades per group for breakdown tables (default 3)")
    args = parser.parse_args()

    # Optional non-blocking premium update
    if args.update_premiums and not os.environ.get("PYTEST_CURRENT_TEST"):
        _dry = args.dry_run
        _ex  = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _fut = _ex.submit(lambda: run_premium_update(dry_run=_dry))
        try:
            result = _fut.result(timeout=180)
            print(f"[INFO] Premium update: {result}")
        except concurrent.futures.TimeoutError:
            print("[WARN] Premium update timed out after 3 min — using cached values")
        finally:
            _ex.shutdown(wait=False)

    print(f"[INFO] Loading recommendations from {args.db} ...")
    con_total = sqlite3.connect(args.db)
    try:
        total_db = con_total.execute("SELECT count(*) FROM options_recommendations").fetchone()[0]
    except Exception as exc:
        print(f"[WARN] Could not count total DB rows: {exc}")
        total_db = 0
    finally:
        con_total.close()

    rows = load_recs(args.db)
    print(f"[INFO] US recommendations: {len(rows)} | Building report ...")

    reports_dir = Path(args.out).parent
    reports_dir.mkdir(parents=True, exist_ok=True)
    threshold = args.min_option_score

    def _write_report(sub_rows: list, fname: str, subtitle: str) -> None:
        out_p = reports_dir / fname
        sub_html = build_html(sub_rows, args.db, total_db,
                              min_trades=args.min_trades, subtitle=subtitle)
        try:
            from reports.report_db import archive_before_write as _abw  # type: ignore
            _abw(out_p)
        except Exception:
            pass
        out_p.write_text(sub_html, encoding="utf-8")
        print(f"[OK]  {subtitle} report \u2192 {out_p}")
        # Companion JSON (Task 1: every HTML report gets a sibling .json)
        json_p = out_p.with_suffix(".json")
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(args.db),
            "subset": subtitle,
            "us_recs": len(sub_rows),
            "total_db": total_db,
            "rows": sub_rows,
        }
        json_p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"[OK]  JSON companion \u2192 {json_p}")

    # Report 1 — Full scan (all US recommendations, equivalent to options_scan_us_300)
    _write_report(rows, "options_scan_us_300_backtest.html", "Full Scan (All US)")

    # Report 2 — Actionable (option_score >= threshold, equivalent to options_actionable)
    act_rows = [r for r in rows if (r.get("option_score") or 0) >= threshold]
    _write_report(act_rows, "options_actionable_backtest.html",
                  f"Actionable (option_score \u2265 {threshold:.0f})")

    # Report 3 — Rejected (option_score < threshold, equivalent to options_rejected)
    rej_rows = [r for r in rows if (r.get("option_score") or 0) < threshold]
    _write_report(rej_rows, "options_rejected_backtest.html",
                  f"Rejected (option_score < {threshold:.0f})")

    print(f"[DONE] 3 backtest reports + 3 JSON files generated in {reports_dir}/")


if __name__ == "__main__":
    main()
