"""
TQS v2 HTML Reporter
=====================
Generates the HTML report for call or put scan results.

Output: reports/tqs_v2_calls.html or reports/tqs_v2_puts.html

Report sections:
  1. Summary cards (total_results, avg_score, top symbol, scan time)
  2. Ranked contracts table (sortable, color-coded grades)
  3. Per-symbol breakdown
  4. Score methodology footnote
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from ..config import TQSv2Config
from ..interfaces.contracts import IReporter
from ..ranking.models import RankedContract
from .archiver import archive_if_exists

_log = logging.getLogger("tqs_v2.reporting.html_reporter")

_GRADE_COLORS = {
    "A+": ("#1a7a1a", "#d4edda"),   # dark green / light green bg
    "A":  ("#155724", "#d4edda"),
    "B+": ("#856404", "#fff3cd"),
    "B":  ("#856404", "#fff3cd"),
    "C+": ("#721c24", "#f8d7da"),
    "C":  ("#721c24", "#f8d7da"),
    "F":  ("#495057", "#e9ecef"),
}


def _grade_badge(grade: str) -> str:
    color, bg = _GRADE_COLORS.get(grade, ("#333", "#eee"))
    return (
        f'<span style="background:{bg};color:{color};padding:2px 8px;'
        f'border-radius:4px;font-weight:bold;font-size:0.85em">{html.escape(grade)}</span>'
    )


def _score_bar(score: float) -> str:
    pct = min(100, max(0, score))
    if pct >= 80:
        bar_color = "#28a745"
    elif pct >= 65:
        bar_color = "#ffc107"
    else:
        bar_color = "#dc3545"
    return (
        f'<div style="background:#e9ecef;border-radius:3px;height:8px;width:80px;display:inline-block">'
        f'<div style="background:{bar_color};height:8px;border-radius:3px;width:{pct:.0f}%"></div>'
        f'</div> <small>{pct:.1f}</small>'
    )


class HTMLReporter(IReporter):
    """Generates an HTML report for scan results."""

    def generate(
        self,
        ranked: list[RankedContract],
        scan_type: str,
        run_id: str,
        config: TQSv2Config,
    ) -> str:
        """
        Write HTML report. Returns absolute output path string.
        """
        filename = (
            config.call_html_report if scan_type == "calls" else config.put_html_report
        )
        out_path = config.reports_dir / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Archive previous report before overwriting (never lose a report)
        archive_if_exists(out_path)

        html_content = self._render(ranked, scan_type, run_id, config)
        out_path.write_text(html_content, encoding="utf-8")
        _log.info("HTML report written: %s (%d results)", out_path, len(ranked))
        return str(out_path)

    def _render(
        self,
        ranked: list[RankedContract],
        scan_type: str,
        run_id: str,
        config: TQSv2Config,
    ) -> str:
        title = f"TQS v2 — {'CALL' if scan_type == 'calls' else 'PUT'} Options Scan"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        avg_score = (
            sum(r.total_score for r in ranked) / len(ranked) if ranked else 0.0
        )
        top_symbols = sorted(
            set(r.symbol for r in ranked[:5]),
        )

        # Summary cards
        summary_html = f"""
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px">
            {self._card("Total Results", str(len(ranked)), "#007bff")}
            {self._card("Avg Score", f"{avg_score:.1f}", "#28a745")}
            {self._card("Scan Type", scan_type.upper(), "#6f42c1")}
            {self._card("Run ID", run_id[:16], "#fd7e14")}
            {self._card("Generated", now, "#6c757d")}
        </div>
        """

        # Contracts table
        rows = "\n".join(self._contract_row(rc, i) for i, rc in enumerate(ranked))
        table_html = f"""
        <table class="tqs-table" style="width:100%;border-collapse:collapse;font-size:0.88em">
            <thead>
                <tr style="background:#343a40;color:#fff">
                    <th>#</th>
                    <th>Symbol</th>
                    <th>Contract</th>
                    <th>Expiry</th>
                    <th>Strike</th>
                    <th>DTE</th>
                    <th>Bid</th>
                    <th>Ask</th>
                    <th>IV%</th>
                    <th>Delta</th>
                    <th>OI</th>
                    <th>Volume</th>
                    <th>Spread%</th>
                    <th>Moneyness%</th>
                    <th>BE%</th>
                    <th>Score</th>
                    <th>Liquidity</th>
                    <th>IV</th>
                    <th>Prob</th>
                    <th>R/R</th>
                    <th>Grade</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
        """

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background:#f8f9fa; color:#212529; margin:0; padding:20px; }}
        h1 {{ font-size:1.5rem; margin-bottom:4px; }}
        .subtitle {{ color:#6c757d; margin-bottom:20px; font-size:0.9rem; }}
        .tqs-table th, .tqs-table td {{ padding:6px 10px; text-align:left;
            border-bottom:1px solid #dee2e6; white-space:nowrap; }}
        .tqs-table tbody tr:hover {{ background:#e9ecef; }}
        .tqs-table tbody tr:nth-child(even) {{ background:#fff; }}
        .num {{ text-align:right; font-family:monospace; }}
        .card {{ background:#fff; border-radius:8px; padding:16px 20px;
                 box-shadow:0 1px 4px rgba(0,0,0,.1); min-width:140px; }}
        .card-label {{ font-size:0.75rem; color:#6c757d; text-transform:uppercase; }}
        .card-value {{ font-size:1.4rem; font-weight:700; }}
        .footnote {{ margin-top:24px; font-size:0.78rem; color:#6c757d;
                     border-top:1px solid #dee2e6; padding-top:12px; }}
    </style>
</head>
<body>
    <h1>📈 {html.escape(title)}</h1>
    <p class="subtitle">Run ID: {html.escape(run_id)} | {now}</p>
    {summary_html}
    {table_html}
    <div class="footnote">
        <strong>8-Dimension Scoring:</strong>
        Probability 18% + Liquidity 18% + Risk/Reward 16% + Stock Trend 12%
        + Win Rate 11% + IV Analysis 11% + Premium Efficiency 8% + Volatility Conditions 6%.
        Stock Trend: SMA50 alignment (±30/+20), RSI regime (overbought −50, sweet spot +10), MACD direction (±10).
        Minimum score for inclusion: {config.min_score_for_report:.0f}.
        OI from yfinance (delayed ~1 day). IV from TradingView scan2 (decimal → ×100 for display).
        Greeks stale outside US market hours (09:30–16:00 ET Mon–Fri).
    </div>
</body>
</html>"""

    @staticmethod
    def _card(label: str, value: str, color: str) -> str:
        return (
            f'<div class="card">'
            f'<div class="card-label">{html.escape(label)}</div>'
            f'<div class="card-value" style="color:{html.escape(color)}">'
            f'{html.escape(value)}</div>'
            f'</div>'
        )

    @staticmethod
    def _contract_row(rc: RankedContract, idx: int) -> str:
        c = rc.contract
        s = rc.scored.scores
        bg = "#fff" if idx % 2 == 0 else "#f8f9fa"

        exp_str = str(c.expiration)
        expiry_fmt = (
            f"{exp_str[:4]}-{exp_str[4:6]}-{exp_str[6:8]}"
            if len(exp_str) == 8
            else exp_str
        )

        # TASK 5iv: Breakeven % from spot
        be_pct_str = "—"
        if c.spot_price > 0 and c.breakeven > 0:
            be_pct = (c.breakeven - c.spot_price) / c.spot_price * 100.0
            be_pct_str = f"{be_pct:+.1f}%"

        # TASK 5iv: click-to-expand details (stock technicals + IV term slopes)
        stock_rsi   = getattr(c, "stock_rsi", None)
        stock_sma50 = getattr(c, "stock_sma50", None)
        stock_sma200= getattr(c, "stock_sma200", None)
        stock_macd  = getattr(c, "stock_macd", None)
        stock_atr   = getattr(c, "stock_atr", None)
        iv_contango = getattr(c, "iv_contango_score", None)
        slope_3m_1m = getattr(c, "slope_3m_1m", None)
        iv_skew_sc  = getattr(c, "iv_skew_score", None)
        theta_sc    = getattr(rc.scored, "score_details", {}).get("premium_efficiency", {}).get("theta_score")

        def _fmt(v, fmt=".2f", suffix=""):
            return f"{v:{fmt}}{suffix}" if v is not None else "—"

        details_rows = "".join([
            f"<tr><td>RSI</td><td>{_fmt(stock_rsi, '.1f')}</td></tr>",
            f"<tr><td>SMA50</td><td>${_fmt(stock_sma50, '.2f')}</td></tr>",
            f"<tr><td>SMA200</td><td>${_fmt(stock_sma200, '.2f')}</td></tr>",
            f"<tr><td>MACD</td><td>{_fmt(stock_macd, '.3f')}</td></tr>",
            f"<tr><td>ATR</td><td>{_fmt(stock_atr, '.2f')}</td></tr>",
            f"<tr><td><b>Stock Trend score (12%)</b></td><td><b>{_fmt(s.stock_trend, '.1f')}</b></td></tr>",
            f"<tr><td>Theta score</td><td>{_fmt(theta_sc, '.1f')}</td></tr>",
            f"<tr><td>IV contango score</td><td>{_fmt(iv_contango, '.1f')}</td></tr>",
            f"<tr><td>Slope 3m-1m</td><td>{_fmt(slope_3m_1m, '+.3f')}</td></tr>",
            f"<tr><td>IV skew score</td><td>{_fmt(iv_skew_sc, '.1f')}</td></tr>",
        ])
        details_html = (
            f'<details style="font-size:0.75em;margin-top:4px">'
            f'<summary style="cursor:pointer;color:#0d6efd">+ More Details</summary>'
            f'<table style="margin-top:4px;width:100%;border-collapse:collapse">'
            f'<tr style="background:#e9ecef"><th style="padding:2px 6px">Field</th>'
            f'<th style="padding:2px 6px">Value</th></tr>'
            f'{details_rows}'
            f'</table></details>'
        )

        oi_td = (
            f'<td class="num">{c.open_interest:,}</td>'
            if c.open_interest is not None
            else '<td class="num">—</td>'
        )
        vol_td = f'<td class="num">{c.volume if c.volume is not None else "—"}</td>'
        return (
            f'<tr style="background:{bg}">'
            f'<td class="num">{rc.rank_global}</td>'
            f'<td><strong>{html.escape(c.root)}</strong><br>'
            f'<small style="color:#6c757d">#{rc.rank_per_stock} in sym</small>'
            f'{details_html}</td>'
            f'<td style="font-size:0.75em;color:#495057">{html.escape(c.opra_code)}</td>'
            f'<td>{html.escape(expiry_fmt)}</td>'
            f'<td class="num">${c.strike:.1f}</td>'
            f'<td class="num">{c.dte}d</td>'
            f'<td class="num">${c.bid:.2f}</td>'
            f'<td class="num">${c.ask:.2f}</td>'
            f'<td class="num">{c.iv*100:.1f}%</td>'
            f'<td class="num">{c.delta:+.3f}</td>'
            f'{oi_td}'
            f'{vol_td}'
            f'<td class="num">{c.spread_pct:.1f}%</td>'
            f'<td class="num">{c.moneyness_pct:+.1f}%</td>'
            f'<td class="num">{be_pct_str}</td>'
            f'<td>{_score_bar(rc.total_score)}</td>'
            f'<td class="num">{s.liquidity:.1f}</td>'
            f'<td class="num">{s.iv_analysis:.1f}</td>'
            f'<td class="num">{s.probability:.1f}</td>'
            f'<td class="num">{s.risk_reward:.1f}</td>'
            f'<td>{_grade_badge(rc.grade)}</td>'
            f'</tr>'
        )
