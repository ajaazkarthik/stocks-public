"""
Options HTML Report Generator
==============================
Generates a self-contained HTML report for F&O option recommendations
from the OptionsAnalyzer engine output.

Renders:
  • Mode badge (SAFE/BALANCED/AGGRESSIVE)
  • Signal score badge
  • Spot price
  • Warnings section (IV high, earnings risk, etc.)
  • Option contract cards (top 3) with:
      - Strike, expiry, type, premium
      - Score decomposition (5 bars)
      - Greeks (delta, theta, IV)
      - Liquidity indicators
      - Lot size + cost in ₹/$ for India/US
  • "Not eligible" graceful fallback panel

Usage:
  from options.options_report import build_options_section_html, build_options_full_report

  # Embed inside signal_stack actionable report:
  html_snippet = build_options_section_html(analyzer_result)

  # Standalone full-page report:
  html = build_options_full_report(all_results)  # list of analyzer results
  Path("options_report.html").write_text(html)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

try:
    from options_predictor import predict_option_outcome  # noqa
    _PREDICTOR_AVAILABLE = True
except ImportError:
    _PREDICTOR_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# CSS (inlined, self-contained)
# ──────────────────────────────────────────────────────────────────────────────

_CSS = """
:root {
  --bg: #0e1117; --card: #161b22; --border: #30363d;
  --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --orange: #f0883e;
  --purple: #a371f7; --teal: #39d353; --pink: #e671b4;
}
body          { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif; margin:0; padding:1rem 1.5rem; }
h1,h2,h3      { margin:0; }
.ts           { font-size:.75rem; color:var(--dim); }
.section-hdr  { font-size:.7rem; text-transform:uppercase; letter-spacing:.1em; color:var(--dim); padding:.3rem 0 .2rem; border-bottom:1px solid var(--border); margin:1rem 0 .5rem; }

/* Symbol header */
.sym-block    { display:flex; align-items:center; gap:.75rem; margin:.5rem 0; }
.sym-name     { font-size:1.4rem; font-weight:700; color:var(--accent); }
.badge        { font-size:.7rem; font-weight:600; padding:.2rem .55rem; border-radius:9999px; white-space:nowrap; }
.badge-green  { background:#1c4f2a; color:var(--green); border:1px solid #2ea043; }
.badge-red    { background:#3d1a1a; color:var(--red);   border:1px solid #b62324; }
.badge-yellow { background:#3d2f00; color:var(--yellow);border:1px solid #9e6a03; }
.badge-blue   { background:#0d2249; color:var(--accent); border:1px solid #1f6feb; }
.badge-purple { background:#2b1e4c; color:var(--purple); border:1px solid #8957e5; }
.badge-orange { background:#3d2200; color:var(--orange); border:1px solid #bd561d; }

.spot-row     { font-size:.85rem; color:var(--dim); margin-bottom:.4rem; }
.spot-price   { color:var(--text); font-weight:600; }

/* Warnings */
.warn-box     { background:#1c1a10; border:1px solid var(--yellow); border-radius:6px; padding:.5rem .75rem; margin:.4rem 0; font-size:.82rem; }
.warn-box.red { background:#1c1010; border-color:var(--red); }

/* NOT eligible panel */
.not-eligible { background:#161b22; border:1px dashed var(--border); border-radius:8px; padding:.75rem 1rem; margin:.5rem 0; font-size:.85rem; color:var(--dim); }
.not-eligible b { color:var(--text); }

/* Contract cards */
.contracts-grid    { display:flex; flex-direction:column; gap:.75rem; margin:.5rem 0; }
.contract-card     { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:.75rem 1rem; }
.contract-card.top { border-color:var(--green); box-shadow:0 0 0 1px #1c4f2a inset; }
.contract-card.second { border-color:var(--accent); }
.cc-header         { display:flex; align-items:center; gap:.5rem; margin-bottom:.4rem; }
.cc-rank           { font-size:.8rem; font-weight:700; width:1.6rem; height:1.6rem; display:flex; align-items:center; justify-content:center; border-radius:50%; flex-shrink:0; }
.cc-rank.r1        { background:#1c4f2a; color:var(--green); }
.cc-rank.r2        { background:#0d2249; color:var(--accent); }
.cc-rank.r3        { background:#2b3543; color:var(--dim); }
.cc-strike         { font-size:1.05rem; font-weight:700; color:var(--text); }
.cc-expiry         { font-size:.75rem; color:var(--dim); }
.cc-premium        { margin-left:auto; font-size:1.1rem; font-weight:700; color:var(--orange); }
.cc-meta           { display:flex; flex-wrap:wrap; gap:.35rem; margin:.35rem 0; }
.cc-score-row      { display:flex; align-items:center; gap:.5rem; margin:.35rem 0; }
.score-pill        { font-size:.7rem; background:#1a2536; border:1px solid #263652; border-radius:4px; padding:.15rem .4rem; color:var(--dim); }
.score-pill b      { color:var(--text); }
.cc-reason         { font-size:.78rem; color:var(--dim); margin-top:.3rem; border-top:1px solid var(--border); padding-top:.3rem; }
.cc-lot-calc       { font-size:.75rem; color:var(--dim); margin-top:.25rem; }
.cc-lot-calc b     { color:var(--accent); }
.cc-warn           { font-size:.75rem; color:var(--yellow); margin-top:.25rem; }
.cc-warn.red       { color:var(--red); }

/* Score bar */
.bar-wrap          { font-size:.7rem; color:var(--dim); display:flex; align-items:center; gap:.3rem; }
.bar-outer         { background:#1c2335; border-radius:2px; height:5px; }
.bar-inner         { border-radius:2px; height:5px; }
.comp-score        { font-size:1.2rem; font-weight:700; }
.grade-a           { color:var(--green); }
.grade-b           { color:var(--teal); }
.grade-c           { color:var(--yellow); }
.grade-d           { color:var(--orange); }
.grade-f           { color:var(--red); }

/* Mode toggle */
.mode-bar          { display:flex; gap:.4rem; margin:.5rem 0; }

/* Mode tabs (--all-modes report) */
.mode-tabs         { display:flex; gap:0; margin-bottom:1rem; border-bottom:2px solid var(--border); }
.mode-tab-btn      { background:transparent; color:var(--dim); border:none; border-bottom:3px solid transparent;
                     padding:.55rem 1.1rem; cursor:pointer; font-size:.85rem; font-weight:600; transition:color .15s; }
.mode-tab-btn:hover{ color:var(--text); }
.mode-tab-btn.active{ color:var(--accent); border-bottom-color:var(--accent); }
.mode-panel        { display:none; }
.mode-panel.active { display:block; }

/* Symbol quick-nav bar */
.sym-nav            { display:flex; gap:.4rem; flex-wrap:wrap; margin-bottom:.5rem; }
.sym-nav-btn        { background:var(--card); border:1px solid var(--border); border-radius:6px;
                      padding:.3rem .7rem; font-size:.8rem; color:var(--dim); cursor:pointer; }
.sym-nav-btn:hover  { color:var(--accent); border-color:var(--accent); }

/* Filter + Search controls bar */
.ctrl-bar           { display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;
                      background:var(--card);border:1px solid var(--border);border-radius:8px;
                      padding:.5rem .75rem;margin-bottom:.75rem; }
.ctrl-grp           { display:flex;gap:.25rem;flex-wrap:wrap; }
.flt-btn            { background:transparent;border:1px solid var(--border);border-radius:6px;
                      padding:.22rem .55rem;font-size:.76rem;color:var(--dim);cursor:pointer;
                      transition:all .15s; }
.flt-btn:hover      { color:var(--text);border-color:var(--accent); }
.flt-btn.active     { background:#0d2249;color:var(--accent);border-color:var(--accent);font-weight:600; }
.flt-cnt            { font-size:.68rem;background:#1a2845;border-radius:999px;
                      padding:.03rem .28rem;margin-left:.2rem; }
.ctrl-sep           { width:1px;height:18px;background:var(--border);align-self:center;flex-shrink:0; }
.sym-search         { flex:1;min-width:110px;max-width:210px;background:#0d1117;
                      border:1px solid var(--border);border-radius:6px;
                      padding:.22rem .55rem;font-size:.8rem;color:var(--text);outline:none; }
.sym-search::placeholder { color:var(--dim); }
.sym-search:focus   { border-color:var(--accent); }
#visible-count      { font-size:.72rem;color:var(--dim);white-space:nowrap;margin-left:auto; }
"""


def _grade_css(score: float) -> str:
    if score >= 75: return "grade-a"
    if score >= 60: return "grade-b"
    if score >= 45: return "grade-c"
    if score >= 30: return "grade-d"
    return "grade-f"


def _bar(value: float, max_val: float = 10.0, color: str = "#58a6ff", width: int = 80) -> str:
    """Render a thin progress bar."""
    pct = int(max(0, min(100, value / max_val * 100)))
    return (
        f'<div class="bar-outer" style="width:{width}px">'
        f'  <div class="bar-inner" style="width:{pct}%;background:{color}"></div>'
        f'</div>'
    )


def _score_pill(label: str, value: float) -> str:
    col = "#3fb950" if value >= 7 else ("#d29922" if value >= 4 else "#f85149")
    return (
        f'<span class="score-pill">'
        f'{label} <b style="color:{col}">{value:.1f}</b>'
        f'</span>'
    )


def _mode_badge(mode: str) -> str:
    mapping = {
        "SAFE":       ("🛡️ SAFE",        "badge-green"),
        "BALANCED":   ("⚖️ BALANCED",    "badge-blue"),
        "AGGRESSIVE": ("🚀 AGGRESSIVE",  "badge-orange"),
    }
    label, css = mapping.get(mode, (mode, "badge-blue"))
    return f'<span class="badge {css}">{label}</span>'


def _signal_badge(score: float) -> str:
    if score >= 70: css, label = "badge-green", "🔥"
    elif score >= 55: css, label = "badge-blue", "✅"
    elif score >= 40: css, label = "badge-yellow", "⚠️"
    else: css, label = "badge-red", "🔴"
    return f'<span class="badge {css}">{label} Signal {score:.0f}</span>'


def _render_prediction_widget(pred: dict, currency: str) -> str:
    """Render the Prediction Engine widget inside a contract card."""
    p_win        = pred.get("p_win_adjusted", 0)
    p_itm        = pred.get("p_itm_market", 0)
    p_hit        = pred.get("p_hit_target", 0)      # honest P(underlying reaches target)
    edge         = pred.get("edge", 0)
    t_prem       = pred.get("target_premium")
    mult         = pred.get("premium_multiple", 0)
    tgt_sp       = pred.get("target_spot", 0)
    exp_mv       = pred.get("expected_move_pct", 0)
    tgt_sig      = pred.get("target_sigma", 0)       # sigma moves required
    conf         = pred.get("confidence", "")
    note         = pred.get("model_note", "")
    cur_p        = pred.get("current_premium")
    mkt_ev       = pred.get("market_ev")             # BS_fair - entry (honest EV)
    mkt_ev_pct   = pred.get("market_ev_pct", 0)
    bs_fair      = pred.get("bs_fair_value", 0)
    calibrated   = pred.get("alpha_calibrated", False)
    emp_r        = pred.get("empirical_r", 0)

    # — colours ——————————————————————————————————————————————————————————————
    # P(ITM) is the only reliable number; use it for the primary traffic light
    if p_itm >= 0.50:   pitm_col = "#3fb950"
    elif p_itm >= 0.35: pitm_col = "#d29922"
    else:               pitm_col = "#f85149"

    # P(WIN) hypothesis colour — always muted while uncalibrated
    pw_col = "#8b949e" if not calibrated else ("#3fb950" if p_win >= 0.5 else "#d29922")

    # Market EV colour
    if mkt_ev is not None:
        mev_col = "#3fb950" if mkt_ev_pct >= 0 else "#f85149"
        mev_str = f"{mkt_ev_pct:+.0f}%"
    else:
        mev_col = "#8b949e"
        mev_str = "n/a"

    # Header badge
    if not calibrated:
        badge = ('<span style="font-size:.55rem;background:#6e1a1a;color:#f85149;'
                 'border-radius:3px;padding:.1rem .3rem;font-weight:700">UNCALIBRATED</span>')
    else:
        badge = ('<span style="font-size:.55rem;background:#1a3a2a;color:#3fb950;'
                 'border-radius:3px;padding:.1rem .3rem;font-weight:700">CALIBRATED</span>')

    # Target scenario row
    tgt_html = ""
    if t_prem is not None and cur_p and cur_p > 0:
        mult_col = "#3fb950" if mult >= 3 else ("#d29922" if mult >= 1.5 else "#8b949e")
        phit_col = "#3fb950" if p_hit >= 0.20 else ("#d29922" if p_hit >= 0.08 else "#f85149")
        tgt_html = (
            f'<div style="margin-top:.4rem;padding:.3rem .4rem;background:rgba(0,0,0,.25);'
            f'border-radius:4px;border-left:2px solid #334155">'
            f'<span style="font-size:.6rem;color:#64748b;font-weight:700">SCENARIO (if {exp_mv:+.1f}% move at half-DTE)</span><br>'
            f'<div style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-top:.2rem">'
            f'<span style="color:var(--dim);font-size:.7rem">Premium</span>'
            f'<b style="color:{mult_col}">{currency}{t_prem:.2f}</b>'
            f'<span style="color:{mult_col};font-size:.8rem;font-weight:700">{mult:.1f}×</span>'
            f'<span style="color:{phit_col};font-size:.7rem">P(reach target)={p_hit*100:.1f}%</span>'
            f'<span style="color:var(--dim);font-size:.65rem">({tgt_sig:.1f}\u03c3 move)</span>'
            f'</div></div>'
        )

    spot_html = ""
    if tgt_sp and exp_mv:
        sp_col = "#3fb950" if exp_mv > 0 else "#f85149"
        spot_html = (
            f'<span style="color:var(--dim);font-size:.7rem">Target spot</span> '
            f'<b style="color:{sp_col}">{currency}{tgt_sp:,.2f}</b> '
            f'<span style="color:{sp_col};font-size:.7rem">({exp_mv:+.1f}%)</span>'
        )

    # Calibration warning strip
    cal_warning = ""
    if not calibrated:
        cal_warning = (
            f'<div style="font-size:.6rem;color:#9d4a4a;margin-top:.3rem;padding:.25rem .4rem;'
            f'background:#1a0a0a;border-radius:3px">'
            f'⚠ Signal r={emp_r:.3f} vs returns &bull; 0 options outcomes settled &bull; '
            f'P(WIN) is hypothesis only &bull; rely on P(ITM) below'
            f'</div>'
        )

    return (
        f'<div style="margin:.5rem 0;padding:.55rem .75rem;background:linear-gradient(135deg,#0d1117 0%,#0d1f3c 100%);'
        f'border-radius:7px;border:1px solid #21262d;font-size:.78rem">'
        f'<div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.3rem">'
        f'<span style="font-size:.65rem;color:#58a6ff;font-weight:700;letter-spacing:.05em">⚡ PREDICTION ENGINE</span>'
        f'{badge}'
        f'</div>'
        + cal_warning
        + f'<div style="display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin-top:.3rem">'
        f'<div style="display:flex;flex-direction:column;align-items:center;'
        f'background:rgba(0,0,0,.3);padding:.3rem .6rem;border-radius:5px;min-width:4rem">'
        f'<span style="font-size:.6rem;color:#58a6ff;font-weight:700">P(ITM) ✓</span>'
        f'<b style="color:{pitm_col};font-size:1.2rem">{p_itm*100:.0f}%</b>'
        f'<span style="font-size:.55rem;color:#4a5568">BS N(d2)</span>'
        f'</div>'
        f'<div style="display:flex;flex-direction:column;align-items:center;'
        f'background:rgba(0,0,0,.2);padding:.3rem .5rem;border-radius:5px;min-width:4rem">'
        f'<span style="font-size:.6rem;color:#8b949e">P(WIN) {"hyp" if not calibrated else "adj"}</span>'
        f'<b style="color:{pw_col};font-size:1.1rem">{p_win*100:.0f}%</b>'
        f'<span style="font-size:.55rem;color:#4a5568">{"uncal" if not calibrated else "calibrated"}</span>'
        f'</div>'
        f'<div style="display:flex;flex-direction:column;align-items:center">'
        f'<span style="font-size:.65rem;color:var(--dim)">Mkt EV</span>'
        f'<b style="color:{mev_col}">{mev_str}</b>'
        f'<span style="font-size:.55rem;color:#4a5568">BS-entry</span>'
        f'</div>'
        f'</div>'
        + tgt_html
        + (f'<div style="display:flex;align-items:center;gap:.4rem;margin-top:.25rem;flex-wrap:wrap">{spot_html}</div>' if spot_html else '')
        + (f'<div style="font-size:.6rem;color:#4a5568;margin-top:.3rem;line-height:1.4">{note}</div>' if note else '')
        + '</div>'
    )


def _tqs_comparison_row(
    liq_s: float,
    tqs_v2: float | None,
    tqs_v2_badge: str,
    oi_source: str,
    ws_source: str,
    source: str,
) -> str:
    """
    Render a TQS comparison row for contract cards.

    Always shows TQS v1 (original TV-only score).
    When source=="hybrid", also shows TQS v2 (with OI + WS enrichment) and data-source badges.
    """
    def _tqs_color(score: float) -> str:
        if score >= 70: return "#3fb950"
        if score >= 50: return "#d29922"
        return "#f85149"

    tqs_v1_scaled = round(liq_s * 10.0, 1)  # liq_score is 0–10; v1 report scale is 0–100
    v1_col = _tqs_color(tqs_v1_scaled)

    if source != "hybrid" or tqs_v2 is None:
        # Non-hybrid: show TQS v1 only with a muted label
        return (
            f'<div style="font-size:.72rem;color:var(--dim);margin:.2rem 0">'
            f'TQS&nbsp;<b style="color:{v1_col}">{tqs_v1_scaled:.0f}</b>'
            f'<span style="font-size:.6rem;color:#4a5568">&nbsp;(liquidity, 0–100)</span>'
            f'</div>'
        )

    # Hybrid: show both TQS v1 and TQS v2 side-by-side
    v2_col = _tqs_color(tqs_v2)

    # OI source badge colour
    if oi_source == "moomoo":
        oi_src_html = '<span style="font-size:.6rem;color:#3fb950;background:#0f2d1a;border:1px solid #2ea043;border-radius:3px;padding:0 4px">🦸 Moomoo&nbsp;OI</span>'
    elif oi_source == "yfinance":
        oi_src_html = '<span style="font-size:.6rem;color:#d2a8ff;background:#1c1040;border:1px solid #8b5cf6;border-radius:3px;padding:0 4px">📊 yFinance&nbsp;OI</span>'
    elif oi_source == "mm-miss":
        oi_src_html = '<span style="font-size:.6rem;color:#d29922;background:#2d2200;border:1px solid #5a4000;border-radius:3px;padding:0 4px">⚠ OI&nbsp;mm-miss</span>'
    elif oi_source == "unavailable":
        oi_src_html = '<span style="font-size:.6rem;color:#8b949e;background:#1c2335;border:1px solid #30363d;border-radius:3px;padding:0 4px">⚠ OI&nbsp;unavailable</span>'
    else:
        oi_src_html = '<span style="font-size:.6rem;color:#8b949e;background:#1c2335;border:1px solid #30363d;border-radius:3px;padding:0 4px">OI&nbsp;placeholder</span>'

    # WS source badge
    if ws_source == "market_hours":
        ws_src_html = '<span style="font-size:.6rem;color:#58a6ff;background:#0d2249;border:1px solid #1f6feb;border-radius:3px;padding:0 4px">📡 Market&nbsp;Hours</span>'
    elif ws_source == "market_closed":
        ws_src_html = '<span style="font-size:.6rem;color:#d29922;background:#2d2200;border:1px solid #5a4000;border-radius:3px;padding:0 4px">🌙 Market&nbsp;Closed</span>'
    else:
        ws_src_html = '<span style="font-size:.6rem;color:#4a5568;background:#161b22;border:1px solid #30363d;border-radius:3px;padding:0 4px">⚡ No&nbsp;WebSocket</span>'

    return (
        f'<div style="display:flex;align-items:center;gap:.5rem;margin:.2rem 0;'
        f'padding:.3rem .5rem;background:#0d1117;border-radius:5px;'
        f'border-left:2px solid #1f6feb;flex-wrap:wrap">'
        # v1
        f'<span style="font-size:.65rem;color:var(--dim)">TQS&nbsp;v1</span>'
        f'<b style="color:{v1_col}">{tqs_v1_scaled:.0f}</b>'
        f'<span style="color:#30363d;font-size:.75rem">│</span>'
        # v2
        f'<span style="font-size:.65rem;color:#58a6ff;font-weight:600">TQS&nbsp;v2&nbsp;{tqs_v2_badge}</span>'
        f'<b style="color:{v2_col};font-size:1rem">{tqs_v2:.0f}</b>'
        f'<span style="color:#30363d;font-size:.75rem">│</span>'
        # badges
        f'{oi_src_html}&nbsp;{ws_src_html}'
        f'</div>'
    )


def _contract_card(c: dict, rank: int, market: str, signal_score: float = 0.0) -> str:
    """Render one contract recommendation card."""
    currency    = "₹" if market in ("NSE", "BSE") else "$"
    strike      = c.get("strike", 0)
    expiry      = c.get("expiry", "?")
    dte         = c.get("dte", 0)
    mid         = c.get("mid_price", 0)
    iv          = c.get("iv")
    delta       = c.get("delta", 0)
    theta       = c.get("theta", 0)
    moneyness   = c.get("moneyness_pct", 0)
    spot        = c.get("spot_price", 0)
    volume      = c.get("volume", 0)
    bid_raw     = c.get("bid", 0) or 0
    ask_raw     = c.get("ask", 0) or 0
    spread_pct  = c.get("spread_pct", 0)
    spread_closed = (bid_raw == 0 and ask_raw == 0)  # market closed → spread meaningless
    liq_s       = c.get("liq_score",    0)
    iv_s        = c.get("iv_score",     0)
    strike_s    = c.get("strike_score", 0)
    dte_s       = c.get("dte_score",    0)
    opt_score   = c.get("option_score", 0)
    current_score   = c.get("current_score",   opt_score)
    moneyball_score = c.get("moneyball_score",  None)
    final_score     = c.get("final_score",      opt_score)
    reason      = c.get("recommendation_reason", "")
    # Hybrid TQS v2 fields (populated only when source=="hybrid")
    tqs_v2      = c.get("tqs_v2")
    tqs_v2_badge = c.get("tqs_v2_badge", "")
    oi_source   = c.get("oi_source", "")
    ws_source   = c.get("ws_source", "")
    source      = c.get("source", "tv")

    # Moneyness label
    if moneyness < -3:
        m_label = f"ITM {moneyness:+.1f}%"
        m_color = "#3fb950"
    elif moneyness > 3:
        m_label = f"OTM {moneyness:+.1f}%"
        m_color = "#d29922"
    else:
        m_label = f"ATM {moneyness:+.1f}%"
        m_color = "#58a6ff"

    card_class = {1: "top", 2: "second", 3: ""}.get(rank, "")
    rank_class = {1: "r1",  2: "r2",    3: "r3"}.get(rank, "r3")
    rank_icon  = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")

    iv_str   = f"{iv:.0f}%" if iv else "N/A"
    # Flag when yfinance IV was ATM-substituted (zero-bid / stale OTM data)
    iv_source = c.get("iv_source", "")
    iv_badge_extra = (
        ' <span title="IV replaced with ATM median (zero-bid OTM stale data)" '
        'style="font-size:.62rem;color:#d29922;background:#2d2200;border:1px solid #5a4000;'
        'border-radius:3px;padding:0 4px;vertical-align:middle">~ATM</span>'
        if iv_source == "yfinance_atm_sub" else ""
    )
    delta_s  = f"Δ {delta:.2f}" if delta else ""
    theta_s  = f"θ {theta:.4f}/day" if theta else ""

    # Lot cost (India)
    lot_html = ""
    lot_size = c.get("lot_size", 0)
    if lot_size and market in ("NSE", "BSE"):
        lot_cost = round(mid * lot_size, 2)
        lot_html = (
            f'<div class="cc-lot-calc">'
            f'Lot size: <b>{lot_size}</b> | 1 Lot cost: <b>{currency}{lot_cost:,.0f}</b>'
            f'</div>'
        )

    # Warnings inside card
    warn_items = c.get("_contract_warnings", [])
    warn_html  = "".join(
        f'<div class="cc-warn{"" if "⚠️" in w else " red"}">{w}</div>'
        for w in warn_items
    )

    grade_cls = _grade_css(final_score)

    # Build 3-score trio block.
    # Only shown for multi-contract chains where Moneyball actually ran with a real
    # population (mb_active=True). Single-contract chains set moneyball_score==current_score
    # so showing 3 identical numbers would be misleading. The mb_active flag (set in
    # options_analyzer.py GUARDRAIL block) is the authoritative signal for this.
    if c.get("mb_active", False):
        mb_col    = "#3fb950" if moneyball_score >= 70 else ("#d29922" if moneyball_score >= 45 else "#f85149")
        cur_col   = "#3fb950" if current_score   >= 70 else ("#d29922" if current_score   >= 45 else "#f85149")
        fin_col   = "#3fb950" if final_score      >= 70 else ("#d29922" if final_score      >= 45 else "#f85149")
        trio_html = f"""
  <div style="display:flex;align-items:center;gap:.75rem;margin:.35rem 0;
              padding:.4rem .6rem;background:#0d1117;border-radius:6px;
              border:1px solid #1e2d3d;font-size:.75rem">
    <div style="display:flex;flex-direction:column;align-items:center;gap:.1rem">
      <span style="color:var(--dim);font-size:.65rem">CURRENT</span>
      <b style="color:{cur_col};font-size:1rem">{current_score:.0f}</b>
    </div>
    <span style="color:var(--dim)">+</span>
    <div style="display:flex;flex-direction:column;align-items:center;gap:.1rem">
      <span style="color:var(--dim);font-size:.65rem">MONEYBALL</span>
      <b style="color:{mb_col};font-size:1rem">{moneyball_score:.0f}</b>
    </div>
    <span style="color:var(--dim)">=</span>
    <div style="display:flex;flex-direction:column;align-items:center;gap:.1rem">
      <span style="color:var(--accent);font-size:.65rem;font-weight:700">Blended (60/40)</span>
      <b style="color:{fin_col};font-size:1.15rem">{final_score:.0f}</b>
    </div>
    <span style="color:var(--dim);font-size:.65rem;margin-left:auto">
      Current = absolute thresholds<br>
      Moneyball = chain-relative rank<br>
      Blended = 60% Current + 40% Moneyball
    </span>
  </div>"""
    else:
        trio_html = ""

    # Prediction Engine widget
    pred_html = ""
    if _PREDICTOR_AVAILABLE and signal_score >= 40 and spot > 0 and strike > 0:
        try:
            pred = predict_option_outcome(
                spot=spot, strike=strike, iv_pct=iv or 0.0,
                dte_at_rec=max(1, dte or 15),
                option_type=c.get("option_type", "CALL"),
                signal_score=signal_score, option_score=opt_score,
                moneyball_score=moneyball_score if moneyball_score is not None else opt_score,
                final_score=final_score,
                current_premium=mid if mid and mid > 0 else None,
            )
            pred_html = _render_prediction_widget(pred, currency)
        except Exception:
            pass

    return f"""
<div class="contract-card {card_class}">
  <div class="cc-header">
    <div class="cc-rank {rank_class}">{rank_icon}</div>
    <div>
      <span class="cc-strike">CALL {currency}{strike:,.0f}</span>
      <span class="cc-expiry"> exp {expiry} ({dte}d)</span>
    </div>
    <div class="cc-premium">{currency}{mid:.2f}</div>
  </div>

  <div class="cc-meta">
    <span class="badge badge-blue">{m_label}</span>
    {'<span class="badge badge-green">' + delta_s + '</span>' if delta_s else ''}
    {'<span class="badge badge-purple">IV ' + iv_str + '</span>' + iv_badge_extra if iv else ''}
    {'<span class="badge badge-orange">' + theta_s + '</span>' if theta_s else ''}
  </div>

  <div cc-meta style="font-size:.75rem;color:var(--dim);margin:.2rem 0">
    Vol: <b style="color:var(--text)">{volume:,}</b>&nbsp;&nbsp;
    Spread: {'<b style="color:var(--dim)">—</b>' if spread_closed else '<b style="color:' + ('var(--red)' if spread_pct > 8 else 'var(--green)') + '">' + f'{spread_pct:.1f}%' + '</b>'}
    {f'&nbsp;&nbsp;Spot: <b style="color:var(--text)">{currency}{spot:,.2f}</b>' if spot else ''}
  </div>

  {_tqs_comparison_row(liq_s, tqs_v2, tqs_v2_badge, oi_source, ws_source, source)}

  {trio_html}

  <div class="cc-score-row">
    <span class="comp-score {grade_cls}">{final_score:.0f}</span>
    <div style="display:flex;flex-direction:column;gap:.15rem">
      <div class="bar-wrap">Liquidity {_bar(liq_s,10,'#58a6ff',60)}</div>
      <div class="bar-wrap">IV Quality {_bar(iv_s,10,'#3fb950',60)}</div>
      <div class="bar-wrap">Strike Fit {_bar(strike_s,10,'#a371f7',60)}</div>
      <div class="bar-wrap">DTE Fit    {_bar(dte_s,10,'#d29922',60)}</div>
    </div>
    <div style="display:flex;flex-direction:column;gap:.15rem;margin-left:.5rem">
      {_score_pill("Liq", liq_s)}
      {_score_pill("IV",  iv_s)}
      {_score_pill("K",   strike_s)}
      {_score_pill("DTE", dte_s)}
    </div>
  </div>

  {pred_html}
  {lot_html}
  {warn_html}
  <div class="cc-reason">{reason}</div>
</div>
"""


def _build_sym_nav(symbols: list) -> str:
    """Render a quick-nav bar of clickable symbol buttons for the full report."""
    if len(symbols) < 2:
        return ""
    btns = []
    for s in symbols:
        sym_id = s.replace(":", "-")
        btns.append(
            f'<button type="button" class="sym-nav-btn" '
            f"onclick=\"document.getElementById('sym-{sym_id}').scrollIntoView({{behavior:'smooth'}})\">"
            f'{s}</button>'
        )
    return '<div class="sym-nav">' + "".join(btns) + "</div>"


def _build_controls_bar(results: list[dict]) -> str:
    """Filter/search controls bar: market tabs, availability toggle, symbol search."""
    if not results:
        return ""

    market_counts: dict[str, int] = {}
    n_avail = 0
    for r in results:
        m = r.get("market", "US")
        market_counts[m] = market_counts.get(m, 0) + 1
        if r.get("options_available"):
            n_avail += 1

    total = len(results)

    mkt_btns = [
        f'<button type="button" class="flt-btn active" data-flt="mkt" data-val="ALL"'
        f' onclick="toggleFlt(this)">All <span class="flt-cnt">{total}</span></button>'
    ]
    for mkt in sorted(market_counts):
        cnt = market_counts[mkt]
        mkt_btns.append(
            f'<button type="button" class="flt-btn" data-flt="mkt" data-val="{mkt}"'
            f' onclick="toggleFlt(this)">{mkt} <span class="flt-cnt">{cnt}</span></button>'
        )

    avail_btns = [
        f'<button type="button" class="flt-btn active" data-flt="avail" data-val="ALL"'
        f' onclick="toggleFlt(this)">All setups</button>',
        f'<button type="button" class="flt-btn" data-flt="avail" data-val="1"'
        f' onclick="toggleFlt(this)">✅ Available Only <span class="flt-cnt">{n_avail}</span></button>',
    ]

    filter_js = """
<script>
function toggleFlt(btn) {
  var grp = btn.parentElement;
  Array.from(grp.querySelectorAll('.flt-btn')).forEach(function(b){ b.classList.remove('active'); });
  btn.classList.add('active');
  applyFilters();
}
function applyFilters() {
  var mktVal   = (document.querySelector('#ctrl-mkt-grp   .flt-btn.active')||{dataset:{val:'ALL'}}).dataset.val;
  var availVal = (document.querySelector('#ctrl-avail-grp .flt-btn.active')||{dataset:{val:'ALL'}}).dataset.val;
  var q        = ((document.getElementById('sym-search')||{}).value||'').trim().toUpperCase();
  var visible  = 0;
  document.querySelectorAll('.sym-card').forEach(function(card) {
    var ok = true;
    if (mktVal   !== 'ALL' && card.dataset.market !== mktVal)   ok = false;
    if (availVal !== 'ALL' && card.dataset.avail  !== availVal) ok = false;
    if (q && !(card.dataset.sym||'').toUpperCase().includes(q)) ok = false;
    card.style.display = ok ? '' : 'none';
    if (ok) visible++;
  });
  var cnt = document.getElementById('visible-count');
  if (cnt) cnt.textContent = visible + ' shown';
}
</script>"""

    bar_html = (
        '<div class="ctrl-bar">'
        + '<div class="ctrl-grp" id="ctrl-mkt-grp">' + "".join(mkt_btns) + "</div>"
        + '<div class="ctrl-sep"></div>'
        + '<div class="ctrl-grp" id="ctrl-avail-grp">' + "".join(avail_btns) + "</div>"
        + '<div class="ctrl-sep"></div>'
        + '<input type="text" class="sym-search" id="sym-search"'
        + ' placeholder="Search symbol…" oninput="applyFilters()">'
        + f'<span id="visible-count">{total} shown</span>'
        + "</div>"
    )
    return bar_html + filter_js


def _render_options_signal_insight(r: dict) -> str:
    """Render a compact RSI / ADX / regime indicator row for an options card."""
    parts: list[str] = []
    rsi    = r.get("rsi")
    adx    = r.get("adx")
    regime = r.get("market_regime", "")

    if rsi is not None:
        try:
            rsi = float(rsi)
            col = "#22c55e" if 45 <= rsi <= 65 else "#f59e0b" if rsi < 35 or rsi > 75 else "#94a3b8"
            parts.append(f'<span style="color:{col}">RSI {rsi:.0f}</span>')
        except (TypeError, ValueError):
            pass

    if adx is not None:
        try:
            adx = float(adx)
            col = "#22c55e" if adx >= 25 else "#f59e0b"
            parts.append(f'<span style="color:{col}">ADX {adx:.0f}</span>')
        except (TypeError, ValueError):
            pass

    if regime:
        parts.append(f'<span style="color:#94a3b8">{regime}</span>')

    sep = ' <span style="color:#334155">|</span> '
    return sep.join(parts) if parts else ""


def build_options_section_html(result: dict) -> str:
    """
    Render an options analysis section for embedding in the signal_stack
    actionable report card.

    Parameters
    ----------
    result : dict
        Output from OptionsAnalyzer.analyze()

    Returns
    -------
    str
        HTML snippet (no <html>/<body> tags).
    """
    symbol    = result.get("symbol", "?")
    market    = result.get("market", "US")
    mode      = result.get("mode", "BALANCED")
    sig_score = result.get("signal_score", 0)
    available = result.get("options_available", False)
    warnings  = result.get("warnings", [])
    contracts = result.get("top_contracts", [])
    fo_elig   = result.get("fo_eligible", True)
    spot      = result.get("spot_price")
    no_reason = result.get("no_options_reason", "")
    currency  = "₹" if market in ("NSE", "BSE") else "$"
    tv_vol    = result.get("tv_daily_vol_pct")

    html = [f'<div class="section-hdr">F&amp;O OPTIONS ANALYSIS</div>']

    # Header row
    html.append('<div class="sym-block">')
    html.append(f'<span style="font-size:.8rem;color:var(--dim)">OPTIONS</span>')
    html.append(_mode_badge(mode))
    html.append(_signal_badge(sig_score))
    if spot:
        html.append(
            f'<span class="badge badge-blue">'
            f'{currency}{spot:,.2f} spot</span>'
        )
    if tv_vol is not None:
        html.append(
            f'<span class="badge badge-purple" title="TradingView daily realized volatility">'
            f'TV Vol {tv_vol:.2f}%/d</span>'
        )
    if market == "NSE" and fo_elig:
        html.append('<span class="badge badge-green">F&amp;O Eligible ✓</span>')
    elif market == "NSE" and not fo_elig:
        html.append('<span class="badge badge-red">F&amp;O ✗ Not Eligible</span>')
    html.append('</div>')

    # Warnings
    for w in warnings:
        is_red = "🔴" in w or "HIGH IV" in w or "EARNINGS" in w
        html.append(
            f'<div class="warn-box {"red" if is_red else ""}">{w}</div>'
        )

    # Not available / not eligible
    if not available:
        html.append(
            f'<div class="not-eligible">'
            f'<b>Options not available:</b> {no_reason or "No data"}'
            f'</div>'
        )
        return "\n".join(html)

    # Contract cards
    html.append('<div class="contracts-grid">')
    for i, c in enumerate(contracts, 1):
        html.append(_contract_card(c, i, market, signal_score=sig_score))
    html.append('</div>')

    return "\n".join(html)


def build_options_full_report(results: list[dict], title: str = "F&O Options Analysis") -> str:
    """
    Generate a standalone full-page HTML report for multiple symbols.

    Parameters
    ----------
    results : list[dict]
        List of OptionsAnalyzer.analyze() outputs.
    title : str
        Page title.

    Returns
    -------
    str
        Complete self-contained HTML document.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Partition: filter out NSE/BSE stocks not in F&O eligible list (Task 1)
    ineligible = [
        r for r in results
        if r.get("market") in ("NSE", "BSE") and not r.get("fo_eligible", True)
    ]
    results = [
        r for r in results
        if not (r.get("market") in ("NSE", "BSE") and not r.get("fo_eligible", True))
    ]
    inelig_banner = ""
    if ineligible:
        syms = ", ".join(r.get("symbol", "?") for r in ineligible)
        n    = len(ineligible)
        inelig_banner = (
            f'<div style="background:#1a1a2e;border:1px solid #334155;border-radius:7px;'
            f'padding:.6rem .9rem;margin-bottom:1rem;font-size:.82rem;color:#8b949e">'
            f'\u2139\ufe0f <b>{n} NSE/BSE stock{"s" if n != 1 else ""} skipped</b>'
            f' — not in F&amp;O eligible list: '
            f'<span style="color:#58a6ff">{syms}</span>'
            f'</div>'
        )

    # Summary counts
    available_count = sum(1 for r in results if r.get("options_available"))
    blocked_count   = sum(1 for r in results if not r.get("options_available"))
    warning_count   = sum(1 for r in results if r.get("warnings"))
    earnings_count  = sum(1 for r in results if r.get("earnings_risk"))

    # No-setups prominence banner
    no_setup_banner = ""
    if results and available_count == 0:
        top_reason = next(
            (r.get("no_options_reason", "") for r in results if r.get("no_options_reason")), ""
        )
        no_setup_banner = f"""
<div style="background:#2b1700;border:1px solid #f59e0b;border-radius:8px;
            padding:1rem 1.2rem;margin-bottom:1.5rem;color:#f59e0b">
  <strong>\u26a0 0 usable setups from {len(results)} symbols analysed.</strong><br>
  <span style="font-size:.85rem">{top_reason}</span>
</div>"""

    symbols_html = []
    for r in results:
        sym      = r.get("symbol", "?")
        market   = r.get("market", "US")
        section  = build_options_section_html(r)
        top      = r.get("top_contracts", [])
        anchor   = sym.replace(":", "-")
        insight  = _render_options_signal_insight(r)
        # Big score if available
        if top:
            best_score  = top[0].get("option_score", 0)
            grade_cls   = _grade_css(best_score)
            score_badge = (
                f'<span class="comp-score {grade_cls}" style="font-size:1.6rem">'
                f'{best_score:.0f}</span>'
            )
        else:
            score_badge = '<span style="color:var(--dim);font-size:.85rem">N/A</span>'

        insight_html = (
            f'<div style="font-size:.75rem;margin-top:.25rem">{insight}</div>'
            if insight else ""
        )

        avail_flag = "1" if r.get("options_available") else "0"
        symbols_html.append(f"""
<div id="sym-{anchor}" class="sym-card"
     data-market="{market}" data-avail="{avail_flag}" data-sym="{sym}"
     style="background:var(--card);border:1px solid var(--border);border-radius:10px;
            padding:1rem 1.2rem;margin:.75rem 0">
  <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:.5rem">
    <span style="font-size:1.1rem;font-weight:700;color:var(--accent)">{sym}</span>
    <span class="badge badge-blue" style="font-size:.7rem">{market}</span>
    {score_badge}
  </div>
  {insight_html}
  {section}
</div>
""")

    # ── Tabbed layout when multiple modes are present ─────────────────────────
    ctrl_bar_html = _build_controls_bar(results)
    modes_present = sorted({r.get("mode", "BALANCED") for r in results})
    if len(modes_present) > 1:
        mode_icons = {"SAFE": "🛡️", "BALANCED": "⚖️", "AGGRESSIVE": "🚀"}
        tab_buttons = []
        tab_panels  = []
        for idx, mode in enumerate(modes_present):
            active_cls = "active" if idx == 0 else ""
            icon       = mode_icons.get(mode, "")
            tab_buttons.append(
                f'<button type="button" class="mode-tab-btn {active_cls}" '
                f'onclick="switchTab(this,\'panel-{mode}\')">' 
                f'{icon} {mode}</button>'
            )
            mode_cards = [
                sym_html
                for sym_html, r in zip(symbols_html, results)
                if r.get("mode", "BALANCED") == mode
            ]
            tab_panels.append(
                f'<div class="mode-panel {active_cls}" id="panel-{mode}">'
                + "".join(mode_cards)
                + "</div>"
            )
        body = (
            ctrl_bar_html
            + '<div class="mode-tabs">' + "".join(tab_buttons) + "</div>\n"
            + "\n".join(tab_panels)
            + """
<script>
function switchTab(btn, panelId) {
  document.querySelectorAll('.mode-tab-btn').forEach(function(b) { b.classList.remove('active'); });
  document.querySelectorAll('.mode-panel').forEach(function(p) { p.classList.remove('active'); });
  btn.classList.add('active');
  document.getElementById(panelId).classList.add('active');
}
</script>"""
        )
    else:
        body = ctrl_bar_html + "\n".join(symbols_html)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
{_CSS}
</style>
</head>
<body>

<div style="display:flex;align-items:center;gap:1rem;margin-bottom:1.5rem">
  <h1 style="font-size:1.4rem;color:var(--accent)">{title}</h1>
  <span class="ts">{now}</span>
</div>

<div style="display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1.5rem">
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;
              padding:.5rem 1rem;font-size:.85rem">
    <div style="color:var(--dim);font-size:.72rem">SYMBOLS ANALYSED</div>
    <div style="font-size:1.4rem;font-weight:700;color:var(--accent)">{len(results)}</div>
  </div>
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;
              padding:.5rem 1rem;font-size:.85rem">
    <div style="color:var(--dim);font-size:.72rem">OPTIONS AVAILABLE</div>
    <div style="font-size:1.4rem;font-weight:700;color:var(--green)">{available_count}</div>
  </div>
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;
              padding:.5rem 1rem;font-size:.85rem">
    <div style="color:var(--dim);font-size:.72rem">EARNINGS RISK</div>
    <div style="font-size:1.4rem;font-weight:700;color:var(--red)">{earnings_count}</div>
  </div>
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;
              padding:.5rem 1rem;font-size:.85rem">
    <div style="color:var(--dim);font-size:.72rem">BLOCKED / NO SETUP</div>
    <div style="font-size:1.4rem;font-weight:700;color:{'var(--red)' if blocked_count>0 else 'var(--dim)'}">{blocked_count}</div>
  </div>
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;
              padding:.5rem 1rem;font-size:.85rem">
    <div style="color:var(--dim);font-size:.72rem">WARNINGS</div>
    <div style="font-size:1.4rem;font-weight:700;color:var(--yellow)">{warning_count}</div>
  </div>
</div>

{no_setup_banner}
{inelig_banner}
{body}

<p style="text-align:center;color:var(--dim);font-size:.75rem;margin-top:2rem">
  Generated {now} · F&O Options Analyzer · For informational use only, not financial advice.
</p>
</body>
</html>"""
