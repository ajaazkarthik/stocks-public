"""
BacktestReporter — generates HTML + JSON backtest report for TQS v2.

Output (light theme, no external deps):
  reports/tqs_v2_backtest_YYYYMMDD_HHMMSS.html
  reports/tqs_v2_backtest_YYYYMMDD_HHMMSS.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from tradingview.options.tqs_v2.backtest.vectorbt_engine import BacktestResult

_log = logging.getLogger(__name__)

# ── CSS / HTML constants ──────────────────────────────────────────────────────

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f8f9fa;color:#212529;margin:0;padding:24px}
h1{font-size:1.5rem;margin-bottom:4px}
h2{font-size:1.1rem;color:#495057;border-bottom:1px solid #dee2e6;padding-bottom:6px;
   margin-top:28px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.card{background:#fff;border:1px solid #dee2e6;border-radius:6px;padding:14px 20px;
      min-width:130px}
.card .label{font-size:.75rem;color:#6c757d;text-transform:uppercase;letter-spacing:.05em}
.card .value{font-size:1.5rem;font-weight:700;margin-top:2px}
.card .value.green{color:#28a745}
.card .value.red{color:#dc3545}
table{width:100%;border-collapse:collapse;font-size:.85rem;background:#fff;
      border:1px solid #dee2e6;border-radius:6px;overflow:hidden}
th{background:#e9ecef;padding:8px 10px;text-align:left;font-weight:600;
   border-bottom:2px solid #dee2e6;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #f1f3f5;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f8f9fa}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:.78rem;
       font-weight:600}
.badge-A{background:#d4edda;color:#155724}
.badge-B{background:#cce5ff;color:#004085}
.badge-C{background:#fff3cd;color:#856404}
.badge-D{background:#f8d7da;color:#721c24}
.badge-F{background:#e2e3e5;color:#383d41}
.bar-wrap{background:#e9ecef;border-radius:3px;height:10px;width:120px;display:inline-block;
          vertical-align:middle}
.bar-fill{height:100%;border-radius:3px;background:#007bff}
.bar-neg{background:#dc3545}
.num{text-align:right}
.footer{margin-top:32px;font-size:.75rem;color:#6c757d}
"""


def _card(label: str, value: str, colour: str = "") -> str:
    cls = f"value {colour}" if colour else "value"
    return (
        f"<div class='card'>"
        f"<div class='label'>{label}</div>"
        f"<div class='{cls}'>{value}</div>"
        f"</div>\n"
    )


def _grade_badge(grade: str) -> str:
    g = (grade or "?").upper()[:1]
    cls = f"badge badge-{g}" if g in "ABCDF" else "badge"
    return f"<span class='{cls}'>{g}</span>"


def _corr_bar(r: float) -> str:
    """Return a colour-coded bar for Spearman r ∈ [-1, 1]."""
    pct = abs(r) * 100
    neg = r < 0
    fill_cls = "bar-fill bar-neg" if neg else "bar-fill"
    return (
        f"<span class='bar-wrap'>"
        f"<span class='{fill_cls}' style='width:{pct:.0f}%'></span>"
        f"</span> {r:+.3f}"
    )


class BacktestReporter:
    """Generates HTML + JSON backtest report.

    Usage::

        reporter = BacktestReporter(out_html)
        reporter.generate(result)
    """

    def __init__(self, out_html: Path | str | None = None) -> None:
        from tradingview.options.tqs_v2.config import TQSv2Config
        cfg = TQSv2Config()
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        default_name = f"tqs_v2_backtest_{ts}.html"
        self._html_path = Path(out_html) if out_html else cfg.reports_dir / default_name

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(self, result: BacktestResult) -> tuple[Path, Path]:
        """Write HTML + JSON reports. Returns (html_path, json_path)."""
        html = self._build_html(result)
        payload = self._build_payload(result)

        self._html_path.parent.mkdir(parents=True, exist_ok=True)
        self._html_path.write_text(html, encoding="utf-8")

        json_path = self._html_path.with_suffix(".json")
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        _log.info("Backtest report: %s", self._html_path)
        return self._html_path, json_path

    # ── HTML builder ─────────────────────────────────────────────────────────

    def _build_html(self, r: BacktestResult) -> str:
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        wr_colour = "green" if r.win_rate >= 50 else "red"
        avg_colour = "green" if r.avg_pnl_pct >= 0 else "red"

        cards = "".join([
            _card("Contracts", str(r.total_contracts)),
            _card("Winners", str(r.winners), "green"),
            _card("Losers",  str(r.losers),  "red"),
            _card("Win Rate", f"{r.win_rate:.1f}%", wr_colour),
            _card("Avg P&L %", f"{r.avg_pnl_pct:+.2f}%", avg_colour),
            _card("Median P&L %", f"{r.median_pnl_pct:+.2f}%",
                  "green" if r.median_pnl_pct >= 0 else "red"),
        ])

        # ── Calls vs Puts table
        type_table = (
            "<table><thead><tr><th>Type</th><th>Count</th><th class='num'>Win Rate</th>"
            "<th class='num'>Win Rate Bar</th></tr></thead><tbody>"
        )
        for label, count, wr in [
            ("CALL", r.total_calls, r.calls_win_rate),
            ("PUT",  r.total_puts,  r.puts_win_rate),
        ]:
            bar_fill = f"<span class='bar-wrap'><span class='bar-fill' style='width:{wr:.0f}%'></span></span>"
            type_table += (
                f"<tr><td><b>{label}</b></td><td>{count}</td>"
                f"<td class='num'>{wr:.1f}%</td>"
                f"<td>{bar_fill} {wr:.1f}%</td></tr>\n"
            )
        type_table += "</tbody></table>"

        # ── Factor correlations table
        corr_html = "<p><i>scipy not installed — correlations unavailable</i></p>"
        if r.factor_correlations:
            sorted_corrs = sorted(
                r.factor_correlations.items(),
                key=lambda x: abs(x[1][0]),
                reverse=True,
            )
            corr_html = (
                "<table><thead><tr><th>Factor</th><th class='num'>Spearman r</th>"
                "<th>Strength</th><th class='num'>p-value</th></tr></thead><tbody>"
            )
            for factor, (rval, pval) in sorted_corrs:
                sig = "✓" if pval < 0.05 else ""
                corr_html += (
                    f"<tr><td>{factor}</td>"
                    f"<td class='num'>{rval:+.4f}</td>"
                    f"<td>{_corr_bar(rval)}</td>"
                    f"<td class='num'>{pval:.4f} {sig}</td></tr>\n"
                )
            corr_html += "</tbody></table>"

        # ── Grade stats table
        grade_html = ""
        if r.grade_stats:
            grade_html = (
                "<table><thead><tr><th>Grade</th><th class='num'>Count</th>"
                "<th class='num'>Winners</th><th class='num'>Win Rate</th>"
                "<th class='num'>Avg P&L %</th></tr></thead><tbody>"
            )
            for grade, gs in sorted(r.grade_stats.items()):
                wr_col = "green" if gs["win_rate"] >= 50 else "red"
                pnl_col = "green" if gs["avg_pnl"] >= 0 else "red"
                grade_html += (
                    f"<tr><td>{_grade_badge(grade)}</td>"
                    f"<td class='num'>{gs['count']}</td>"
                    f"<td class='num'>{gs['winners']}</td>"
                    f"<td class='num' style='color:{'#28a745' if wr_col=='green' else '#dc3545'}'>"
                    f"{gs['win_rate']:.1f}%</td>"
                    f"<td class='num' style='color:{'#28a745' if pnl_col=='green' else '#dc3545'}'>"
                    f"{gs['avg_pnl']:+.2f}%</td>"
                    f"</tr>\n"
                )
            grade_html += "</tbody></table>"

        # ── vectorbt stats
        vbt_html = ""
        if r.vbt_stats:
            vbt_html = "<h2>Equity Curve Stats (vectorbt)</h2><table>"
            vbt_html += "<thead><tr><th>Metric</th><th class='num'>Value</th></tr></thead><tbody>"
            for k, v in r.vbt_stats.items():
                vbt_html += f"<tr><td>{k}</td><td class='num'>{v}</td></tr>\n"
            vbt_html += "</tbody></table>"

        # ── Contracts table (top 100 by total_score)
        sample = sorted(r.rows, key=lambda x: x.get("total_score") or 0, reverse=True)[:100]
        contracts_html = ""
        if sample:
            contracts_html = (
                "<h2>Top Contracts (by Score)</h2>"
                "<table><thead><tr>"
                "<th>Symbol</th><th>Type</th><th>Strike</th><th>Expiry</th>"
                "<th class='num'>Score</th><th>Grade</th>"
                "<th class='num'>Entry Mid</th><th class='num'>Current Premium</th>"
                "<th class='num'>P&amp;L %</th><th class='num'>Status</th>"
                "</tr></thead><tbody>"
            )
            for row in sample:
                pnl = row.get("pnl_pct")
                w = row.get("is_winner")
                ofa = row.get("outcome_fetched_at") or ""
                is_live = ofa.startswith("LIVE:")
                cur_prem = row.get("current_premium")

                if is_live:
                    pnl_str = f"{pnl:+.1f}%&nbsp;<small style='color:#6c757d'>(unrealised)</small>" if pnl is not None else "—"
                    w_str = "<span style='background:#fff3cd;color:#856404;padding:2px 8px;border-radius:12px;font-size:.78rem;font-weight:600'>&#x1F504;&nbsp;LIVE</span>"
                    pnl_col = "#856404"  # amber for unrealised
                elif w == 1:
                    pnl_str = f"{pnl:+.1f}%" if pnl is not None else "—"
                    w_str = "<span style='color:#28a745;font-weight:700'>&#x2713;</span>"
                    pnl_col = "#28a745"
                elif w == 0:
                    pnl_str = f"{pnl:+.1f}%" if pnl is not None else "—"
                    w_str = "<span style='color:#dc3545;font-weight:700'>&#x2717;</span>"
                    pnl_col = "#dc3545"
                else:
                    pnl_str = f"{pnl:+.1f}%" if pnl is not None else "—"
                    w_str = "—"
                    pnl_col = "#495057"

                cur_prem_str = f"{cur_prem:.4f}" if cur_prem is not None else "—"
                contracts_html += (
                    f"<tr><td>{row.get('symbol','')}</td>"
                    f"<td>{row.get('option_type','')}</td>"
                    f"<td class='num'>{row.get('strike','')}</td>"
                    f"<td>{row.get('expiration','')}</td>"
                    f"<td class='num'>{row.get('total_score') or '—'}</td>"
                    f"<td>{_grade_badge(row.get('grade') or '?')}</td>"
                    f"<td class='num'>{row.get('mid') or '—'}</td>"
                    f"<td class='num'>{cur_prem_str}</td>"
                    f"<td class='num' style='color:{pnl_col}'>{pnl_str}</td>"
                    f"<td class='num'>{w_str}</td></tr>\n"
                )
            contracts_html += "</tbody></table>"

        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TQS v2 Backtest Report — {now_str}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>TQS v2 Backtest Analysis</h1>
<p style="color:#6c757d;font-size:.85rem">Generated {now_str}</p>

<h2>Summary</h2>
<div class="cards">{cards}</div>

<h2>By Type</h2>
{type_table}

<h2>Factor Correlations with Win/Loss (Spearman r)</h2>
{corr_html}

<h2>By Grade</h2>
{grade_html}

{vbt_html}

{contracts_html}

<div class="footer">TQS v2 Backtest · {now_str}</div>
</body>
</html>"""

    # ── JSON payload ──────────────────────────────────────────────────────────

    def _build_payload(self, r: BacktestResult) -> dict:
        # Enrich each contract row with a human-readable status field
        enriched_rows = []
        for row in r.rows[:500]:  # cap for JSON size
            enriched = dict(row)
            ofa = enriched.get("outcome_fetched_at") or ""
            if ofa.startswith("LIVE:"):
                enriched["status"] = "LIVE (unrealised)"
            elif enriched.get("is_winner") == 1:
                enriched["status"] = "winner"
            elif enriched.get("is_winner") == 0:
                enriched["status"] = "loser"
            else:
                enriched["status"] = "pending"
            enriched_rows.append(enriched)

        return {
            "generated_at": datetime.utcnow().isoformat(),
            "summary": {
                "total_contracts":  r.total_contracts,
                "winners":          r.winners,
                "losers":           r.losers,
                "win_rate":         r.win_rate,
                "avg_pnl_pct":      r.avg_pnl_pct,
                "median_pnl_pct":   r.median_pnl_pct,
                "total_calls":      r.total_calls,
                "calls_win_rate":   r.calls_win_rate,
                "total_puts":       r.total_puts,
                "puts_win_rate":    r.puts_win_rate,
            },
            "factor_correlations": {
                k: {"spearman_r": v[0], "p_value": v[1]}
                for k, v in r.factor_correlations.items()
            },
            "grade_stats": r.grade_stats,
            "vbt_stats":   r.vbt_stats,
            "contracts":   enriched_rows,
        }
