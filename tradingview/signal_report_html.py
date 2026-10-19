"""
signal_report_html.py — HTML rendering helpers for Signal Stack reports.

Extracted from signal_stack.py to reduce god-file complexity.
Pure formatting functions with no external dependencies beyond stdlib.
"""
from __future__ import annotations

import math
from collections import Counter


# ─── Formatting helpers ──────────────────────────────────────────────────────────

def grade(score_0_100):
    if score_0_100 >= 75: return "A", "#22c55e"
    if score_0_100 >= 60: return "B", "#84cc16"
    if score_0_100 >= 45: return "C", "#eab308"
    if score_0_100 >= 30: return "D", "#f97316"
    return "F", "#ef4444"

def bar(score, maxv=10):
    pct = min(100, max(0, score / maxv * 100))
    c   = "#22c55e" if pct >= 65 else ("#eab308" if pct >= 40 else "#ef4444")
    return (f'<div class="bar-row">'
            f'<div class="bar-bg"><div class="bar-fill" style="width:{pct:.0f}%;background:{c}"></div></div>'
            f'<span class="bar-val">{score:.1f}</span></div>')

def hcap(v):
    if v is None: return "N/A"
    if v >= 1e12: return f"${v/1e12:.1f}T"
    if v >= 1e9:  return f"${v/1e9:.1f}B"
    if v >= 1e6:  return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"

def fmt(v, suf="", d=2):
    if v is None or (isinstance(v, float) and math.isnan(v)): return "–"
    return f"{v:.{d}f}{suf}"

def colour_cell(v, pos_thresh, neg_thresh, val_str):
    if v is None: return val_str
    if v >= pos_thresh: return f'<span class="green">{val_str}</span>'
    if v <= neg_thresh: return f'<span class="red">{val_str}</span>'
    return val_str


# ─── Backtest block ──────────────────────────────────────────────────────────────

def _backtest_block_html(backtest_data: dict | None) -> str:
    """Return the HTML block for position sizing from backtest data.

    Shows a buy-units recommendation badge + key metrics in plain English.
    Returns empty string when backtest data is unavailable.
    """
    if not backtest_data:
        return ""
    if backtest_data.get("error"):
        return (
            '<div class="bt-block">'
            '<div class="col-head">📐 Position Sizing (Backtest)</div>'
            f'<div class="bt-na">⚠ Backtest unavailable — defaulting to 1 unit</div>'
            '</div>'
        )

    units  = backtest_data.get("size_units", 1)
    label  = backtest_data.get("size_label", "")
    color  = backtest_data.get("size_color", "#9e9e9e")
    wr     = backtest_data.get("win_rate")
    pnl    = backtest_data.get("total_pnl")
    sharpe = backtest_data.get("sharpe")
    n_cl   = backtest_data.get("num_closed", 0)
    cached = " (cached)" if backtest_data.get("from_cache") else ""

    # Metric pills
    def _pill(metric_label, val, good_thresh, bad_thresh, fmt_str):
        if val is None:
            return f'<span class="bt-pill bt-na-pill">{metric_label} N/A</span>'
        cls = "bt-pill-good" if val >= good_thresh else ("bt-pill-bad" if val <= bad_thresh else "bt-pill-ok")
        return f'<span class="bt-pill {cls}">{metric_label} {fmt_str.format(val)}</span>'

    wr_pill     = _pill("Win",    wr,     55,  40, "{:.0f}%")
    pnl_pill    = _pill("P&L",   pnl,    20, -10, "{:+.0f}%")
    sharpe_pill = _pill("Sharpe", sharpe, 0.5, 0,  "{:.2f}")
    trades_pill = f'<span class="bt-pill bt-pill-ok">{n_cl} trades{cached}</span>'

    # Unit badge (big, colored)
    unit_label_display = {0: "AVOID", 1: "1 unit", 2: "2 units", 3: "3 units"}.get(units, f"{units}u")
    avoid_note = (' <span class="bt-avoid-note">— strategy has poor edge on this symbol</span>'
                  if units == 0 else "")

    return (
        '<div class="bt-block">'
        '<div class="col-head">📐 Position Sizing (Backtest)</div>'
        f'<div class="bt-header">'
        f'  <span class="bt-badge" style="background:{color}">{unit_label_display}</span>'
        f'  <span class="bt-label">{label}{avoid_note}</span>'
        f'</div>'
        f'<div class="bt-pills">{wr_pill}{pnl_pill}{sharpe_pill}{trades_pill}</div>'
        f'<div class="bt-legend">'
        f'<b>Win %</b> = % of closed trades profitable (≥55 good, &lt;40 poor) · '
        f'<b>P&L</b> = sum of all trade gains/losses, not compound · '
        f'<b>Sharpe</b> = return per unit of risk (≥0.5 good, &lt;0.2 risky)'
        f'</div>'
        '</div>'
    )


# ─── Summary panels ─────────────────────────────────────────────────────────────

def _build_signal_summary_panel(ranked: list) -> str:
    """Return a compact HTML summary bar: total count, per-exchange breakdown, grade distribution."""
    if not ranked:
        return ""
    exchanges: Counter = Counter()
    grades: Counter = Counter()
    actionable_count = 0
    confidences = []
    for item in ranked:
        sym = item.get("tv_symbol", "") or (item.get("meta") or {}).get("pro_symbol", "")
        ex = sym.split(":")[0] if ":" in sym else "OTHER"
        exchanges[ex] += 1
        comp = item.get("composite", 0) or 0
        if comp >= 75:
            g = "A"
        elif comp >= 60:
            g = "B"
        elif comp >= 45:
            g = "C"
        else:
            g = "D"
        grades[g] += 1
        if not item.get("actionability_reasons"):
            actionable_count += 1
        confidence = (item.get("data_confidence") or {}).get("overall")
        if confidence is not None:
            confidences.append(float(confidence))

    grade_colors = {"A": "#22c55e", "B": "#eab308", "C": "#f97316", "D": "#ef4444"}
    grade_html = " &nbsp;".join(
        f'<span style="color:{grade_colors[g]};font-weight:600">{g}:{grades[g]}</span>'
        for g in ("A", "B", "C", "D") if grades[g]
    )
    exch_parts = sorted(exchanges.items(), key=lambda kv: -kv[1])
    exch_html = " &nbsp;".join(
        f'<span style="color:#94a3b8">{ex}:<b style="color:#e2e8f0">{c}</b></span>'
        for ex, c in exch_parts
    )
    return (
        '<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
        'padding:8px 14px;margin:8px 0;display:flex;flex-wrap:wrap;gap:16px;'
        'align-items:center;font-size:13px;color:#cbd5e1">'
        f'<span>&#x1F4CA; <b style="color:#f1f5f9">{len(ranked)}</b> signals</span>'
        f'<span><b style="color:#86efac">{actionable_count}</b> quality-gated</span>'
        f'<span>Data coverage: <b style="color:#93c5fd">'
        f'{(sum(confidences) / len(confidences) * 100 if confidences else 0):.0f}%</b></span>'
        f'<span style="color:#475569">|</span>{exch_html}'
        f'<span style="color:#475569">|</span>'
        f'<span>Grades: {grade_html}</span>'
        '</div>'
    )


def _build_coverage_warning_html(coverage: dict) -> str:
    """
    Render a yellow warning banner listing which enrichment layers were skipped.
    coverage dict keys (all bool): india, moomoo, external, ibkr
    Returns '' if all layers are active.
    """
    skipped = []
    if not coverage.get("india", True):
        skipped.append("India L5 enrichment (no Zerodha token — NSE/BSE portfolio data missing)")
    if not coverage.get("moomoo", True):
        skipped.append("Moomoo enrichment (OpenD not running — AU/SG position data missing)")
    if not coverage.get("external", True):
        skipped.append("External enrichers (YouTube / Smallcase / Twitter — skipped)")
    if not coverage.get("ibkr", True):
        skipped.append("IBKR L6 enrichment (not enabled)")
    if not skipped:
        return ""
    items = "".join(f"<li>{s}</li>" for s in skipped)
    return (
        '<div style="background:#2a1f00;border:1px solid #e07000;border-radius:6px;'
        'padding:10px 14px;margin:8px 0 12px 0;color:#f59e0b;font-size:12px">'
        '<strong>⚠ COVERAGE WARNING — enrichment layers skipped:</strong>'
        f'<ul style="margin:4px 0 0 16px;padding:0">{items}</ul>'
        '</div>'
    )
