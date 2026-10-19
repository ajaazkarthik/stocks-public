"""
options_hedger_report.py — HTML report builder for the Options Hedger.

Provides:
  _CSS                     — shared stylesheet string
  _strategy_span           — coloured strategy badge HTML
  _urgency_span            — coloured urgency badge HTML
  _build_hedge_global_row  — <tr> for global holdings table
  _regime_box              — India regime badge HTML
  _nifty_portfolio_html    — consolidated NIFTY beta hedge box
  build_html               — full HTML report assembly
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .options_hedger_constants import (
    BIG_GAIN_PCT,
    MODERATE_GAIN_PCT,
    LOSS_PCT,
    LARGE_POS_INR,
    INDIA_VIX_FEAR,
)


_CSS = """

body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:0}

h1{background:linear-gradient(135deg,#1f6feb,#58a6ff);padding:18px 24px;margin:0;font-size:1.4rem}

h2{color:#58a6ff;padding:8px 24px;margin:8px 0 0;font-size:1.05rem;border-bottom:1px solid #30363d}

.section{padding:12px 24px 24px}

table{width:100%;border-collapse:collapse;font-size:.88rem;margin-top:8px}

th{background:#161b22;color:#8b949e;text-align:left;padding:7px 10px;border-bottom:2px solid #30363d}

td{padding:6px 10px;border-bottom:1px solid #21262d;vertical-align:top}

tr:hover td{background:#161b22}

.num{text-align:right;font-variant-numeric:tabular-nums}

.pos{color:#3fb950;font-weight:600}

.neg{color:#f85149;font-weight:600}

.hold{color:#8b949e}

.meta{color:#8b949e;font-size:.82rem;padding:4px 0}

.rationale{color:#8b949e;font-size:.82rem;list-style:none;margin:0;padding:0}

.rationale li::before{content:"• ";color:#58a6ff}

.details{font-size:.82rem;color:#d29922;font-style:italic}

.urgency-HIGH{color:#f85149;font-weight:700}

.urgency-MEDIUM{color:#d29922;font-weight:600}

.urgency-LOW{color:#8b949e}

.strategy-EXIT{background:#3d1515;color:#f85149;padding:3px 8px;border-radius:4px;font-weight:700;font-size:.85rem}

.strategy-PROTECTIVE\\ PUT,.strategy-PROTECTIVE_PUT{background:#2d2008;color:#d29922;padding:3px 8px;border-radius:4px;font-weight:600;font-size:.85rem}

.strategy-COLLAR{background:#0d1f36;color:#58a6ff;padding:3px 8px;border-radius:4px;font-weight:600;font-size:.85rem}

.strategy-COVERED\\ CALL,.strategy-COVERED_CALL{background:#1b2b1b;color:#3fb950;padding:3px 8px;border-radius:4px;font-weight:600;font-size:.85rem}

.strategy-HOLD{background:#21262d;color:#8b949e;padding:3px 8px;border-radius:4px;font-size:.85rem}

.strategy-CLOSE{background:#5a0a0a;color:#ff6b6b;padding:3px 8px;border-radius:4px;font-weight:700;font-size:.85rem}

.strategy-ROLL{background:#0d2a3d;color:#63b3ed;padding:3px 8px;border-radius:4px;font-weight:600;font-size:.85rem}

.strategy-LOCK_PROFIT{background:#0a1f0a;color:#68d391;padding:3px 8px;border-radius:4px;font-weight:600;font-size:.85rem}

.opt-badge{display:inline-block;background:#2d2008;color:#e3b341;padding:1px 6px;border-radius:3px;font-size:.75rem;font-family:monospace;margin-top:3px}

.opt-pnl-table{width:100%;border-collapse:collapse;font-size:.75rem;margin-top:6px;background:#0a0d12}

.opt-pnl-table th{background:#1c1c2e;color:#9f7aea;padding:4px 8px;font-size:.73rem;text-align:left}

.opt-pnl-table td{padding:4px 8px;border:1px solid #21262d;font-size:.75rem}

.opt-profit td{color:#3fb950}.opt-loss td{color:#f85149}.opt-neutral td{color:#8b949e}

/* ── Market filter bar ── */

.filter-bar{display:flex;align-items:center;gap:8px;padding:10px 24px;

  background:#161b22;border-bottom:1px solid #30363d;flex-wrap:wrap}

.filter-bar label{color:#8b949e;font-size:.82rem;margin-right:4px;white-space:nowrap}

.mf-btn{background:#21262d;color:#8b949e;border:1px solid #30363d;padding:5px 14px;

  border-radius:20px;cursor:pointer;font-size:.82rem;transition:.15s}

.mf-btn:hover{background:#30363d;color:#e6edf3}

.mf-btn.active{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:600}

.sec-hidden{display:none}

/* ── Portfolio NIFTY hedge summary ── */

.nifty-portfolio-box{background:#050e1a;border:1px solid #1f6feb55;border-radius:6px;

  padding:12px 16px;margin-bottom:12px}

.nifty-portfolio-box h4{color:#58a6ff;margin:0 0 6px;font-size:.9rem}

.nifty-portfolio-box .np-contract{font-family:'Consolas',monospace;color:#e3b341;

  font-size:.88rem;background:#1c2128;padding:4px 8px;border-radius:3px;

  display:inline-block;margin:4px 0}

.nifty-portfolio-box .np-breakdown{font-size:.75rem;color:#8b949e;margin-top:6px}

.nifty-portfolio-box .np-breakdown td{padding:2px 8px}

.nifty-portfolio-box .np-cost{color:#d29922;font-size:.78rem;margin-top:4px;font-style:italic}

.regime-box{display:inline-block;margin:4px 12px;padding:8px 16px;border-radius:6px;font-weight:600}

.regime-BULL{background:#152b1e;color:#3fb950}

.regime-BEAR{background:#3d1515;color:#f85149}

.regime-SIDEWAYS{background:#2d2008;color:#d29922}

/* ── scenario table, contract line, leveraged badge, NIFTY hedge ── */

.scenarios{width:100%;border-collapse:collapse;font-size:.75rem;margin-top:6px;background:#0a0d12}

.scenarios th{background:#0d2033;color:#6ea8fe;padding:4px 8px;font-size:.73rem;text-align:left}

.scenarios td{padding:4px 8px;border:1px solid #21262d;font-size:.75rem}

.sc-normal td{color:#8b949e}

.sc-bull td{color:#3fb950}

.sc-bear td{color:#d29922}

.sc-crash td{color:#f85149}

.contract-line{font-family:'Consolas',monospace;font-size:.80rem;color:#e3b341;background:#1c2128;

  padding:3px 7px;border-radius:3px;margin:3px 0;display:block;border-left:3px solid #e3b341}

.income-line{font-size:.80rem;color:#3fb950;font-style:italic;margin:2px 0}

.cost-line{font-size:.80rem;color:#d29922;font-style:italic;margin:2px 0}

.risk-line{font-size:.78rem;color:#8b949e;margin:2px 0}

.lev-badge{background:#1c2128;color:#e3b341;padding:2px 6px;border-radius:3px;

  font-size:.78rem;font-weight:600;border:1px solid #e3b34155}

.nifty-hedge-box{background:#0d2033;border-left:3px solid #58a6ff;padding:5px 8px;

  margin-top:5px;border-radius:3px;font-size:.80rem}

.nifty-hedge-box .nh-title{color:#58a6ff;font-weight:600;font-size:.78rem}

.nifty-hedge-box .nh-contract{color:#e3b341;font-family:'Consolas',monospace;font-size:.80rem}

/* ── Quick Recommendation Card ───────────────────────────── */

.quick-card{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:14px 18px;margin-bottom:20px}

.qc-header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding-bottom:10px;border-bottom:1px solid #21262d}

.qc-title{font-size:1.10rem;font-weight:700;color:#e6edf3}

.qc-expiry{color:#8b949e;font-size:.78rem;background:#21262d;padding:2px 8px;border-radius:10px}

.qc-paid{color:#8b949e;font-size:.80rem}

.qc-pnl{font-size:.92rem;font-weight:700}

.qc-outlook{background:#0a0d12;border-radius:4px;padding:8px 12px}

.qc-rank-1{background:rgba(35,134,54,0.15);border-left:3px solid #238636;padding:10px 12px;border-radius:4px;margin-bottom:6px}

.qc-rank-2{background:rgba(31,111,235,0.08);border-left:3px solid #1f6feb;padding:10px 12px;border-radius:4px;margin-bottom:6px}

.qc-rank-3{background:#161b22;border:1px solid #21262d;padding:10px 12px;border-radius:4px;margin-bottom:6px}

.qc-full-detail{margin-top:10px;padding-top:10px;border-top:1px solid #21262d}

details>summary{cursor:pointer;color:#8b949e;font-size:.80rem;padding:5px 0;user-select:none;list-style:none;display:block}

details>summary::-webkit-details-marker{display:none}

details[open]>summary{color:#58a6ff}

.not-avail-msg{color:#8b949e;font-style:italic;font-size:.82rem;padding:6px 0}

.nifty-hedge-box .nh-cost{color:#8b949e;font-style:italic;font-size:.75rem}

"""

def _strategy_span(strategy: str) -> str:

    css = strategy.replace(" ", "_")

    return f'<span class="strategy-{css}">{strategy}</span>'

def _urgency_span(urgency: str) -> str:

    return f'<span class="urgency-{urgency}">{urgency}</span>'


def _build_hedge_global_row(item: dict) -> str:
    """Build a single <tr> for the Global Holdings hedge table.  (B-9)"""
    h = item["holding"]
    rec = item["rec"]
    sym = h.get("symbol", "?")
    broker = h.get("broker", "?")
    exch = h.get("exchange", "?")
    qty = h.get("total_qty", 0)
    avg = h.get("combined_avg", 0)
    ltp = h.get("combined_ltp", 0)
    val = h.get("combined_val", 0)
    pnl = h.get("combined_pnl", 0)
    pnl_pct = h.get("combined_pnl_pct", 0)
    cur = h.get("currency", "USD")
    cur_sym = {"AUD": "A$", "HKD": "HK$", "SGD": "S$", "USD": "$",
               "JPY": "¥", "GBP": "£"}.get(cur, cur + " ")
    pnl_cls = "pos" if pnl >= 0 else "neg"
    pct_cls = "pos" if pnl_pct >= 0 else "neg"
    rationale_items = "".join(f"<li>{r}</li>" for r in rec.get("rationale", []))
    dets = rec.get("details", {})
    detail_parts: list[str] = []
    if dets.get("stop_loss_note"):
        detail_parts.append(f"<span class='details'>{dets['stop_loss_note']}</span>")
    if dets.get("call_contract"):
        detail_parts.append(f"<div class='contract-line'>📞 {dets['call_contract']}</div>")
    if dets.get("call_income_est"):
        detail_parts.append(f"<div class='income-line'>💰 {dets['call_income_est']}</div>")
    if dets.get("call_risk_note"):
        detail_parts.append(f"<div class='risk-line'>⚠ {dets['call_risk_note']}</div>")
    if dets.get("scenario_table"):
        detail_parts.append(dets["scenario_table"])
    if dets.get("put_contract"):
        detail_parts.append(f"<div class='contract-line'>🛡 {dets['put_contract']}</div>")
    if dets.get("put_cost_est"):
        detail_parts.append(f"<div class='cost-line'>💸 {dets['put_cost_est']}</div>")
    if dets.get("put_note"):
        if dets["put_note"].startswith("<table"):
            detail_parts.append(dets["put_note"])
        else:
            detail_parts.append(f"<div class='risk-line'>{dets['put_note']}</div>")
    detail_html = "".join(detail_parts)
    return (
        f"<tr>"
        f"<td><strong>{sym}</strong></td>"
        f"<td style='color:#8b949e;font-size:.82rem'>{broker}</td>"
        f"<td style='color:#8b949e;font-size:.82rem'>{exch}</td>"
        f"<td class='num'>{qty:,}</td>"
        f"<td class='num'>{cur_sym}{avg:,.2f}</td>"
        f"<td class='num'>{cur_sym}{ltp:,.2f}</td>"
        f"<td class='num'>{cur_sym}{val:,.0f}</td>"
        f"<td class='num {pct_cls}'>{pnl_pct:+.1f}%</td>"
        f"<td class='num {pnl_cls}'>{cur_sym}{pnl:+,.0f}</td>"
        f"<td>{_strategy_span(rec['strategy'])}</td>"
        f"<td>{_urgency_span(rec['urgency'])}</td>"
        f"<td><ul class='rationale'>{rationale_items}</ul>{detail_html}</td>"
        f"</tr>"
    )


def _regime_box(regime: dict) -> str:

    r = (regime.get("regime") or "Unknown").upper()

    score = regime.get("score", "")

    css = "BULL" if "BULL" in r else ("BEAR" if "BEAR" in r else "SIDEWAYS")

    score_s = f" (score: {score})" if score else ""

    return f'<span class="regime-box regime-{css}">Market Regime: {r}{score_s}</span>'

def _nifty_portfolio_html(nh: Optional[dict]) -> str:

    """Render the consolidated portfolio NIFTY beta-hedge summary box."""

    if not nh:

        return ""

    lots   = nh.get("lots", 0)

    if lots == 0:

        partial   = nh.get("partial", 0)

        positions = nh.get("positions", [])

        names     = ", ".join(p["symbol"] for p in positions[:6])

        return (

            "<div class='nifty-portfolio-box'>"

            "<h4>&#128208; Portfolio NIFTY Beta Hedge &#8212; Non-F&amp;O stocks</h4>"

            "<div style='color:#8b949e;font-size:.82rem'>"

            f"Combined position too small for 1 NIFTY lot "

            f"(need {partial:.2f} lots). "

            f"Stocks: {names}. Accumulate more to trigger a hedge.</div>"

            "</div>"

        )

    nifty_price = nh.get("nifty_price", 0)

    put_strike  = nh.get("put_strike", 0)

    expiry      = nh.get("expiry", "?")

    total_val   = nh.get("total_val", 0)

    cost_lo     = nh.get("cost_lo", 0)

    cost_hi     = nh.get("cost_hi", 0)

    bw          = nh.get("total_beta_wt", 0)

    positions   = nh.get("positions", [])

    breakdown_rows = "".join(

        f"<tr><td>{p['symbol']}</td>"

        f"<td style='text-align:right'>&#8377;{p['val']:,.0f}</td>"

        f"<td style='text-align:right'>{p['beta']:.2f} [{p['beta_src']}]</td>"

        f"<td style='text-align:right'>&#8377;{p['beta_weighted']:,.0f}</td></tr>"

        for p in positions

    )

    breakdown = (

        "<table class='np-breakdown'>"

        "<thead><tr><th>Stock</th><th>Value</th><th>Beta</th><th>&#946;-weighted</th></tr></thead>"

        f"<tbody>{breakdown_rows}</tbody></table>"

    ) if positions else ""

    pct = f"{cost_lo/total_val*100:.1f}" if total_val else "?"

    return (

        "<div class='nifty-portfolio-box'>"

        f"<h4>&#128208; Portfolio NIFTY Beta Hedge &#8212; {len(positions)} non-F&amp;O stock(s)</h4>"

        "<div class='np-contract'>"

        f"BUY {lots}&times; NIFTY {expiry} &#8377;{put_strike:,.0f}PE"

        f"&nbsp;&nbsp;({lots} lot{'s' if lots > 1 else ''} &times; 75 units)"

        "</div>"

        "<div style='font-size:.80rem;color:#8b949e;margin-top:4px'>"

        f"Portfolio &#946;-exposure: &#8377;{bw:,.0f} &nbsp;|&nbsp; "

        f"NIFTY lot value: &#8377;{nifty_price*75:,.0f} &nbsp;|&nbsp; "

        f"Covers &#8377;{total_val:,.0f} of non-F&amp;O positions"

        "</div>"

        "<div class='np-cost'>"

        f"Est. premium: &#8377;{cost_lo:,.0f}&#8211;&#8377;{cost_hi:,.0f} total "

        f"(~{pct}% of non-F&amp;O portfolio)</div>"

        f"{breakdown}"

        "</div>"

    )

# ─────────────────────────────────────────────────────────────────────────────

def build_html(

    recs: list[dict],

    regime: dict,

    pcr_map: dict,

    oi_map: dict,

    args,

    india_vix: Optional[float] = None,

    global_recs: Optional[list[dict]] = None,

    long_option_recs: Optional[list[dict]] = None,

    nifty_hedge_summary: Optional[dict] = None,

) -> str:

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Summary counts

    by_strategy: dict[str, int] = {}

    for r in recs:

        s = r["rec"]["strategy"]

        by_strategy[s] = by_strategy.get(s, 0) + 1

    summary_items = " &nbsp;|&nbsp; ".join(

        f"<strong>{v}</strong> {k}" for k, v in sorted(by_strategy.items())

    )

    regime_html = _regime_box(regime) if regime else ""

    vix_html = ""

    if india_vix is not None:

        vix_color = "#f85149" if india_vix > INDIA_VIX_FEAR else "#3fb950"

        vix_label = " ⚠ FEAR" if india_vix > INDIA_VIX_FEAR else " CALM"

        vix_html = (f'<span class="regime-box" style="background:#161b22;color:{vix_color}">'

                    f'India VIX: {india_vix:.1f}{vix_label}'

                    f'</span>')

    # ── India holdings table ──────────────────────────────────────────────────

    ths = ["Symbol", "Qty", "Avg ₹", "LTP ₹", "Value ₹", "P&L %", "P&L ₹",

           "Strategy", "Urgency", "PCR", "OI Signal", "Rationale / Action"]

    table = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in ths) + "</tr></thead><tbody>"

    for item in recs:

        h   = item["holding"]

        rec = item["rec"]

        sym = h.get("symbol", "?")

        qty = h.get("total_qty", 0)

        avg = h.get("combined_avg", 0)

        ltp = h.get("combined_ltp", 0)

        val = h.get("combined_val", 0)

        pnl = h.get("combined_pnl", 0)

        pnl_pct = h.get("combined_pnl_pct", 0)

        pnl_cls = "pos" if pnl >= 0 else "neg"

        pct_cls = "pos" if pnl_pct >= 0 else "neg"

        pcr_v   = rec.get("pcr")

        pcr_s   = f"{pcr_v:.2f}" if pcr_v is not None else "—"

        oi_s    = rec.get("oi_type") or "—"

        rationale_items = "".join(f"<li>{r}</li>" for r in rec.get("rationale", []))

        dets = rec.get("details", {})

        detail_parts: list[str] = []

        if dets.get("stop_loss_note"):

            detail_parts.append(f"<span class='details'>{dets['stop_loss_note']}</span>")

        for key in ("call_note", "put_note", "collar_note", "lot_note"):

            if dets.get(key):

                detail_parts.append(f"<span class='details'>{dets[key]}</span>")

        for key in ("cost_estimate", "income_est"):

            if dets.get(key):

                detail_parts.append(f"<span class='details'>{dets[key]}</span>")

        # NIFTY beta hedge box

        if dets.get("nifty_hedge"):

            nh_cost = dets.get("nifty_prem_est", "")

            detail_parts.append(

                f"<div class='nifty-hedge-box'>"

                f"<div class='nh-title'>📐 NIFTY Beta Hedge</div>"

                f"<div class='nh-contract'>{dets['nifty_hedge']}</div>"

                + (f"<div class='nh-cost'>{nh_cost}</div>" if nh_cost else "")

                + f"</div>"

            )

        detail_html = "".join(detail_parts)

        table += (

            f"<tr>"

            f"<td><strong>{sym}</strong></td>"

            f"<td class='num'>{qty:,}</td>"

            f"<td class='num'>₹{avg:,.2f}</td>"

            f"<td class='num'>₹{ltp:,.2f}</td>"

            f"<td class='num'>₹{val:,.0f}</td>"

            f"<td class='num {pct_cls}'>{pnl_pct:+.1f}%</td>"

            f"<td class='num {pnl_cls}'>₹{pnl:+,.0f}</td>"

            f"<td>{_strategy_span(rec['strategy'])}</td>"

            f"<td>{_urgency_span(rec['urgency'])}</td>"

            f"<td class='num'>{pcr_s}</td>"

            f"<td>{oi_s}</td>"

            f"<td><ul class='rationale'>{rationale_items}</ul>{detail_html}</td>"

            f"</tr>"

        )

    table += "</tbody></table>"

    # ── Global holdings table ─────────────────────────────────────────────────

    global_section_html = ""

    if global_recs:

        gths = ["Symbol", "Broker", "Exch", "Qty", "Avg", "LTP", "Value", "P&L %", "P&L",

                "Strategy", "Urgency", "Rationale / Action"]

        gtable = ("<table><thead><tr>"

                  + "".join(f"<th>{h}</th>" for h in gths)

                  + "</tr></thead><tbody>")

        for item in global_recs:

            gtable += _build_hedge_global_row(item)

        gtable += "</tbody></table>"

        global_count = len(global_recs)

        global_section_html = f"""

<section id="sec-global"

<h2>🌏 Global Holdings — Stocks &amp; ETFs ({global_count} positions)</h2>

<div class="section">

<p class="meta" style="color:#8b949e;font-size:.82rem">

  ⚠ <em>Global options signals (US VIX, HK/AU volatility) not yet integrated.

  Recommendations are P&amp;L threshold–based only.

  Execute via IBKR for US stocks; Moomoo for HK/SG/AU.</em>

</p>

{gtable}

</div>

</section>"""

# ── Long option positions section (Quick-Card layout) ──────────────────────

    long_option_section_html = ""

    if long_option_recs:

        cards_html = ""

        for item in long_option_recs:

            h        = item["holding"]

            rec      = item["rec"]

            opt      = h.get("opt_info", {})

            sym      = h.get("symbol", "?")

            broker   = h.get("broker", "?")

            avg      = h.get("combined_avg", 0)     # total cost

            val      = h.get("combined_val", 0)

            pnl      = h.get("combined_pnl", 0)

            pnl_pct  = h.get("combined_pnl_pct", 0)

            cur      = h.get("currency", "USD")

            cur_sym  = {"AUD": "A$", "HKD": "HK$", "SGD": "S$", "USD": "$"}.get(cur, cur + " ")

            strike_v = opt.get("strike", 0)

            expiry_v = opt.get("expiry_str", "?")

            opt_type = opt.get("option_type", "?")

            pnl_color = "#3fb950" if pnl >= 0 else "#f85149"

            # Days left

            d_left = ""

            try:

                from datetime import datetime as _dtq

                exp_dt = _dtq.strptime(expiry_v, "%d %b %Y")

                d_n    = max(0, (exp_dt - _dtq.now()).days)

                d_left = f"{d_n}d left"

                d_color = "#f85149" if d_n <= 7 else ("#e3b341" if d_n <= 14 else "#8b949e")

            except Exception:

                d_color = "#8b949e"

            # Market outlook

            dets        = rec.get("details", {})

            outlook     = dets.get("market_outlook", {})

            direction   = outlook.get("direction", "NEUTRAL")

            confidence  = outlook.get("confidence", 50)

            signals     = outlook.get("signals", [])

            dir_color   = {"BULLISH": "#3fb950", "BEARISH": "#f85149", "NEUTRAL": "#e3b341"}.get(direction, "#8b949e")

            dir_icon    = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➡️"}.get(direction, "❓")

            signal_items = "".join(

                f"<li style='margin-bottom:2px'>{sig}</li>" for sig in signals[:4]

            )

            # Ranked strategies

            ranked = dets.get("ranked_strategies", [])

            rank_classes = ["qc-rank-1", "qc-rank-2", "qc-rank-3"]

            strat_rows_html = ""

            for i, s in enumerate(ranked):

                cls       = rank_classes[i] if i < 3 else "qc-rank-3"

                floor_val = s.get("floor_val")

                gain_val  = s.get("gain_val")

                floor_str = f"{cur_sym}{floor_val:+,.0f}" if floor_val is not None else "—"

                gain_str  = ("<strong style='color:#3fb950'>UNLIMITED ↑</strong>"

                             if gain_val is None

                             else f"<strong style='color:#3fb950'>{cur_sym}{gain_val:+,.0f}</strong>")

                cost_disp = s.get("cost_display", "—")

                # Signal score badge — shown on all strategies

                _sig_data = s.get("signal_score")  # Show on all strategies

                _sig     = _sig_data.get("score")          if isinstance(_sig_data, dict) else _sig_data

                _live    = _sig_data.get("live", False)    if isinstance(_sig_data, dict) else False

                _age     = _sig_data.get("age_h")          if isinstance(_sig_data, dict) else None

                _partial = _sig_data.get("partial", False) if isinstance(_sig_data, dict) else False

                if _sig is not None:

                    if _partial:

                        # L1-only estimate: L2/L3/L4 not available -- never classify as

                        # WEAK/STRONG/NEUTRAL to avoid driving wrong sell decisions.

                        _ftag = " · <span style='font-size:.68rem;font-weight:400;opacity:.7'>⚠ technical est. only</span>"

                        _sc = "#8b949e"

                        _sm = (f"Signal estimate ~{_sig}/100 (technical only — "

                               f"run signal pipeline for full score){_ftag}")

                    else:

                        if _live:

                            _ftag = " · <span style='font-size:.68rem;font-weight:400;opacity:.8'>⚡ live</span>"

                        elif _age is not None:

                            _ftag = f" · <span style='font-size:.68rem;font-weight:400;opacity:.7'>{_age}h ago</span>"

                        else:

                            _ftag = " · <span style='font-size:.68rem;font-weight:400;opacity:.7'>from DB</span>"

                        if _sig >= 70:

                            _sc, _sm = "#3fb950", f"Signal score {_sig}/100 — STRONG · consider holding if time permits{_ftag}"

                        elif _sig <= 60:

                            _sc, _sm = "#f85149", f"Signal score {_sig}/100 — WEAK · selling now is the right call{_ftag}"

                        else:

                            _sc, _sm = "#e3b341", f"Signal score {_sig}/100 — NEUTRAL · weigh conviction vs theta risk{_ftag}"

                    _score_badge = f"<div style='font-size:.73rem;color:{_sc};margin-top:2px;font-weight:600'>📊 {_sm}</div>"

                else:

                    _score_badge = ""

                # Option score badge
                _opt_data = s.get("option_score_data") or {}
                _os = _opt_data.get("score")
                if _os is not None:
                    _os_color = "#3fb950" if _os >= 70 else ("#f85149" if _os < 50 else "#e3b341")
                    _liq_s = _opt_data.get("liq", "—")
                    _iv_s  = _opt_data.get("iv",  "—")
                    _str_s = _opt_data.get("strike", "—")
                    _dte_s = _opt_data.get("dte", "—")
                    _opt_score_badge = (
                        f"<div style='font-size:.73rem;color:{_os_color};margin-top:2px;font-weight:600'>"
                        f"🎯 Option score {_os}/100"
                        f" <span style='font-weight:400;color:#8b949e'>"
                        f"(Liq {_liq_s} · IV {_iv_s} · K-fit {_str_s} · DTE {_dte_s})</span></div>"
                    )
                else:
                    _opt_score_badge = ""

                strat_rows_html += f"""<div class="{cls}">

  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">

    <span style="color:#8b949e;font-size:.72rem;min-width:100px">{s.get('rank_label','')}</span>

    <span style="font-size:.88rem;font-weight:700">{s['icon']} {s['name']}</span>

    <span style="margin-left:auto;font-family:monospace;font-size:.77rem;color:#63b3ed;white-space:nowrap">{s['action']}</span>

  </div>

  <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:.80rem;margin-top:2px">

    <span style="color:#8b949e">{s['floor_label']}: <strong style="color:#f85149">{floor_str}</strong></span>

    <span style="color:#8b949e">{s['gain_label']}: {gain_str}</span>

    <span style="color:#8b949e">{s.get('cost_label','')}: <strong style="color:#e6edf3">{cost_disp}</strong></span>

  </div>

  <div style="font-size:.77rem;color:#d29922;margin-top:3px">💡 Why: {s['why']}</div>

{_score_badge}{_opt_score_badge}  <div style="font-size:.73rem;color:#8b949e;margin-top:2px">⚠ Tradeoff: {s['tradeoff']}</div>

</div>"""

            if not ranked:

                strat_rows_html = (f"<div class='not-avail-msg'>"

                                   f"{_strategy_span(rec.get('strategy','HOLD'))} — "

                                   f"see full analysis below</div>")

            # Detail parts (collapsible)

            detail_parts: list = []

            if dets.get("close_action"):

                detail_parts.append(f"<div class='contract-line'>🚪 CLOSE: {dets['close_action']}</div>")

            if dets.get("roll_action"):

                detail_parts.append(f"<div class='risk-line'>🔄 ROLL: {dets['roll_action']}</div>")

            if dets.get("spread_contract"):

                detail_parts.append(f"<div class='contract-line'>📊 SPREAD: {dets['spread_contract']}</div>")

            if dets.get("spread_locked_floor"):

                detail_parts.append(f"<div class='income-line'>🔒 {dets['spread_locked_floor']}</div>")

            if dets.get("spread_max_profit"):

                detail_parts.append(f"<div class='income-line'>🎯 {dets['spread_max_profit']}</div>")

            if dets.get("naked_beats_spread"):

                detail_parts.append(f"<div class='risk-line' style='color:#e3b341'>⚡ {dets['naked_beats_spread']}</div>")

            if dets.get("hold_naked_note"):

                detail_parts.append(f"<div class='risk-line'>💡 {dets['hold_naked_note']}</div>")

            if dets.get("scenario_table"):

                detail_parts.append(dets["scenario_table"])

            if dets.get("spread_comparison_table"):

                detail_parts.append(dets["spread_comparison_table"])

            if dets.get("roll_candidates_html"):

                detail_parts.append(dets["roll_candidates_html"])

            if dets.get("decision_matrix_html"):

                detail_parts.append(dets["decision_matrix_html"])

            if dets.get("all_strategies_html"):

                detail_parts.append(dets["all_strategies_html"])

            if dets.get("buy_put_candidates_html"):

                detail_parts.append(dets["buy_put_candidates_html"])

            cards_html += f"""

<div class="quick-card">

  <!-- Header -->

  <div class="qc-header">

    <span class="qc-title">{sym} ${strike_v:.0f}{'C' if opt_type == 'C' else 'P'}</span>

    <span class="qc-expiry">Expires {expiry_v}</span>

    {f"<span style='color:{d_color};font-size:.78rem;font-weight:600'>{d_left}</span>" if d_left else ""}

    <span class="qc-paid">Cost paid: {cur_sym}{avg:.0f} total</span>

    <span class="qc-pnl" style="color:{pnl_color}">{cur_sym}{pnl:+,.0f} ({pnl_pct:+.1f}%)</span>

    <span style="color:#8b949e;font-size:.77rem;margin-left:auto">Now: {cur_sym}{val:.0f} · {broker}</span>

  </div>

  <!-- Outlook + Snapshot grid -->

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px">

    <div class="qc-outlook" style="border-left:3px solid {dir_color}">

      <div style="color:{dir_color};font-weight:700;font-size:.87rem;margin-bottom:4px">

        {dir_icon} MARKET OUTLOOK: {direction}

        <span style="color:#8b949e;font-weight:400;font-size:.74rem"> ({confidence}% confidence)</span>

      </div>

      <ul style="margin:0;padding-left:14px;color:#8b949e;font-size:.77rem;list-style:disc">

        {signal_items}

      </ul>

    </div>

    <div style="background:#0a0d12;border:1px solid #30363d;border-radius:4px;padding:8px 12px">

      <div style="color:#8b949e;font-size:.72rem;margin-bottom:5px;text-transform:uppercase;letter-spacing:.05em">Position Snapshot</div>

      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px">

        <div><div style="color:#8b949e;font-size:.70rem">Cost</div><div style="color:#e6edf3;font-size:.85rem;font-weight:600">{cur_sym}{avg:.0f}</div></div>

        <div><div style="color:#8b949e;font-size:.70rem">Now</div><div style="color:{pnl_color};font-size:.85rem;font-weight:600">{cur_sym}{val:.0f}</div></div>

        <div><div style="color:#8b949e;font-size:.70rem">P&amp;L</div><div style="color:{pnl_color};font-size:.85rem;font-weight:600">{cur_sym}{pnl:+,.0f}</div></div>

      </div>

    </div>

  </div>

  <!-- Ranked recommendations -->

  <div style="margin-top:10px">

    <div style="color:#e6edf3;font-size:.84rem;font-weight:700;margin-bottom:6px">

      🏆 RECOMMENDED STRATEGIES

      <span style="color:#8b949e;font-weight:400;font-size:.74rem"> (system-ranked · outlook + profit + time)</span>

    </div>

    {strat_rows_html}

  </div>

  <!-- Full Analysis (collapsed by default) -->

  <details style="margin-top:8px">

    <summary>▼ Full Analysis — All Strategies, Spread Chain, Scenarios, P&amp;L Tables</summary>

    <div class="qc-full-detail">{''.join(detail_parts) if detail_parts else "<p class='not-avail-msg'>No detailed analysis available for this position.</p>"}</div>

  </details>

</div>"""

        long_option_section_html = f"""

<section id="sec-options">

<h2>📋 Long Option Positions — Quick Hedge Advisor ({len(long_option_recs)} positions)</h2>

<div class="section">

<p class="meta" style="color:#8b949e;font-size:.82rem">

  ℹ <em>Market outlook is system-generated from RSI, trend, and analyst data.

  Strategies are ranked by outlook + profit level + days to expiry.

  <strong style="color:#e6edf3">Read in 60 seconds — expand for full analysis.</strong></em>

</p>

{cards_html}

</div>

</section>"""

    return f"""<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Global Portfolio Hedging Advisor — {ts}</title>

<style>{_CSS}</style>

<script>

function filterMarket(btn, market) {{

  document.querySelectorAll('.mf-btn').forEach(function(b){{b.classList.remove('active');}});

  btn.classList.add('active');

  ['sec-india','sec-global','sec-options'].forEach(function(id){{

    var el=document.getElementById(id);

    if(!el) return;

    if(market==='all'){{el.classList.remove('sec-hidden');}}

    else{{el.classList.toggle('sec-hidden', el.dataset.market!==market);}}

  }});

}}

</script>

</head>

<body>

<h1>🛡 Global Portfolio Hedging Strategy Advisor &nbsp;|&nbsp; {ts}</h1>

<p class="meta" style="padding:8px 24px">

  {regime_html}

  {vix_html}

  &nbsp;|&nbsp; {summary_items}

  &nbsp;|&nbsp; {len(recs)} India + {len(global_recs or [])} global + {len(long_option_recs or [])} options analysed

</p>

<p class="meta" style="padding:0 24px 12px;color:#8b949e;font-size:.82rem">

  ⚠ <em>Strike prices are indicative only (rounded to nearest ₹50).

  Verify actual NSE strikes and lot sizes before placing any order.

  Always check current option premiums via Zerodha Sensibull or NSE website.</em>

</p>

<div class="filter-bar">

  <label>Filter by market:</label>

  <button type="button" class="mf-btn active" onclick="filterMarket(this,'all')">All</button>

  <button type="button" class="mf-btn" onclick="filterMarket(this,'india')">🇮🇳 India</button>

  <button type="button" class="mf-btn" onclick="filterMarket(this,'us')">🇺🇸 US</button>

  <button type="button" class="mf-btn" onclick="filterMarket(this,'au')">🇦🇺 AU / Global</button>

  <button type="button" class="mf-btn" onclick="filterMarket(this,'options')">📋 Long Options</button>

</div>

<section id="sec-india" data-market="india">

<h2>🇮🇳 India Holdings Hedge Recommendations ({len(recs)} positions)</h2>

<div class="section">

  {_nifty_portfolio_html(nifty_hedge_summary)}

  {table}

</div>

</section>

{global_section_html}

{long_option_section_html}

<h2>📖 Strategy Guide</h2>

<div class="section">

<table>

  <thead><tr><th>Strategy</th><th>When to Use</th><th>Payoff</th><th>Approx Cost</th></tr></thead>

  <tbody>

    <tr><td>{_strategy_span('EXIT')}</td>

        <td>P&L &lt;{LOSS_PCT:.0f}% + bearish OI/regime — stop the bleeding</td>

        <td>Realise loss, free capital</td><td>Brokerage only</td></tr>

    <tr><td>{_strategy_span('PROTECTIVE PUT')}</td>

        <td>Big gains (&gt;{BIG_GAIN_PCT:.0f}%) or bear regime — insurance</td>

        <td>Unlimited upside + floor on downside</td><td>~1–3% / month</td></tr>

    <tr><td>{_strategy_span('COLLAR')}</td>

        <td>Very large gains + OI turning bearish — lock in profits cheaply</td>

        <td>Capped upside, floor on downside</td><td>Near zero (call premium offsets put)</td></tr>

    <tr><td>{_strategy_span('COVERED CALL')}</td>

        <td>Moderate gains + bullish OI + neutral regime — income generation</td>

        <td>Income + slightly reduced upside</td><td>Negative (you receive premium)</td></tr>

    <tr><td>{_strategy_span('HOLD')}</td>

        <td>Stock trending + OI bullish — no action, momentum intact</td>

        <td>Full position exposure</td><td>Zero</td></tr>

  </tbody>

</table>

</div>

<script>

(function(){{

  var gen = new Date("{ts}");

  var age = (Date.now() - gen) / 3600000;

  if (!isNaN(age) && age > 8) {{

    var h = age < 24 ? (Math.floor(age) + ' hours') : (Math.floor(age/24) + ' days');

    var b = document.createElement('div');

    b.style = 'position:fixed;top:0;left:0;right:0;background:#7c5800;color:#ffd369;padding:.6rem 1rem;font-size:.9rem;font-weight:600;z-index:9999;text-align:center';

    b.textContent = '\u26a0 Report is ' + h + ' old \u2014 re-run for current data';

    document.body.prepend(b);

  }}

}})();

</script>

</body>

</html>

"""

