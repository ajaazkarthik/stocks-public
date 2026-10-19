"""
ZeroDTE HTML Reporter
======================
Generates dark-theme HTML reports for call and put scan results.
Each report shows:
  - Header with scan timestamp, date, market context
  - Summary stats cards (candidates, signals found, contracts)
  - Per-stock signal breakdown table
  - Filterable contract table with DTE, strike, composite score
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import ZeroDTEContract, ZeroDTERunResult
from .archiver import archive_if_exists

# ── Color constants ───────────────────────────────────────────────────────────
_CALL_COLOR = "#00d4aa"   # teal-green for calls
_PUT_COLOR = "#ff6b6b"    # red for puts
_HIGH_COLOR = "#00ff88"   # bright green for HIGH confidence
_MED_COLOR = "#ffd93d"    # amber for MEDIUM
_LOW_COLOR = "#6c757d"    # grey for LOW


def _score_bar(score: float, max_score: float, color: str) -> str:
    """Render a thin SVG progress bar for a sub-score."""
    pct = min((score / max_score) * 100, 100)
    return (
        f'<div style="display:flex;align-items:center;gap:4px;">'
        f'<div style="width:60px;height:6px;background:#2d2d2d;border-radius:3px;'
        f'overflow:hidden;">'
        f'<div style="width:{pct:.0f}%;height:100%;background:{color};'
        f'border-radius:3px;"></div></div>'
        f'<span style="font-size:11px;color:#ccc">{score:.0f}</span>'
        f'</div>'
    )


def _confidence_badge(confidence: str) -> str:
    color = _HIGH_COLOR if confidence == "HIGH" else (_MED_COLOR if confidence == "MEDIUM" else _LOW_COLOR)
    return f'<span style="color:{color};font-weight:700;font-size:12px">{confidence}</span>'


def _contract_row(c: ZeroDTEContract, accent: str, idx: int) -> str:
    bg = "#0d1117" if idx % 2 == 0 else "#111820"
    momentum_bar = _score_bar(c.momentum_score, 40, "#4dabf7")
    iv_bar = _score_bar(c.iv_back_score, 30, "#ffd93d")
    voi_bar = _score_bar(c.voi_score, 30, "#ff922b")
    conf = _confidence_badge(c.confidence)
    spread_color = "#ff6b6b" if c.spread_pct > 20 else "#ccc"
    oi_color = "#ffd93d" if c.open_interest < 300 else "#ccc"

    return (
        f"<tr style='background:{bg};'>"
        f"<td style='padding:8px 12px;'><strong style='color:{accent}'>{c.symbol}</strong></td>"
        f"<td style='padding:8px;'>{c.contract_code}</td>"
        f"<td style='padding:8px;'><span style='background:#1a2332;padding:2px 8px;border-radius:4px;font-size:11px;'>DTE {c.dte}</span></td>"
        f"<td style='padding:8px;'>${c.strike:,.2f}</td>"
        f"<td style='padding:8px;'>${c.spot_price:,.2f}</td>"
        f"<td style='padding:8px;'>{c.otm_pct:.1f}%</td>"
        f"<td style='padding:8px;'>${c.bid:.2f} / ${c.ask:.2f}</td>"
        f"<td style='padding:8px;'><strong style='color:{accent}'>${c.ask:.2f}</strong></td>"
        f"<td style='padding:8px;'><span style='color:{spread_color}'>{c.spread_pct:.0f}%</span></td>"
        f"<td style='padding:8px;'><span style='color:{oi_color}'>{c.open_interest:,}</span></td>"
        f"<td style='padding:8px;'>{c.volume:,}</td>"
        f"<td style='padding:8px;'>{c.iv * 100:.0f}%</td>"
        f"<td style='padding:8px;'>"
        f"<div style='display:flex;flex-direction:column;gap:2px;'>"
        f"<div style='display:flex;align-items:center;gap:4px;'><span style='font-size:10px;color:#888;width:12px'>M</span>{momentum_bar}</div>"
        f"<div style='display:flex;align-items:center;gap:4px;'><span style='font-size:10px;color:#888;width:12px'>IV</span>{iv_bar}</div>"
        f"<div style='display:flex;align-items:center;gap:4px;'><span style='font-size:10px;color:#888;width:12px'>V</span>{voi_bar}</div>"
        f"</div>"
        f"</td>"
        f"<td style='padding:8px;'>"
        f"<div style='display:flex;flex-direction:column;align-items:center;'>"
        f"<span style='font-size:18px;font-weight:700;color:{accent}'>{c.composite_score:.0f}</span>"
        f"<span style='font-size:10px;color:#888'>/ 100</span>"
        f"</div>"
        f"</td>"
        f"<td style='padding:8px;'>{conf}</td>"
        f"</tr>"
    )


def _signal_summary_row(symbol: str, contracts: list[ZeroDTEContract], accent: str, idx: int) -> str:
    """One row per stock in the signal summary section."""
    if not contracts:
        return ""
    # Use first contract for stock-level data
    c = contracts[0]
    sig = c.signal
    bg = "#0d1117" if idx % 2 == 0 else "#111820"

    change_color = _CALL_COLOR if (sig.prev_day_change_pct >= 0 if sig else True) else _PUT_COLOR
    change_str = f"{'+' if sig.prev_day_change_pct >= 0 else ''}{sig.prev_day_change_pct:.1f}%" if sig else "—"
    vol_str = f"{sig.volume_ratio:.1f}x" if (sig and sig.volume_ratio > 0) else "—"
    iv_str = f"{sig.iv_1w*100:.0f}% / {sig.iv_1m*100:.0f}%" if (sig and sig.iv_available) else "n/a"
    slope_str = f"{sig.iv_back_slope*100:+.1f}%" if (sig and sig.iv_available and sig.iv_back_slope != 0) else "—"
    voi_str = f"{sig.voi_ratio:.0f}x" if (sig and sig.voi_available) else "n/a"

    rsi_cell = (
        f"<td style='padding:8px;'>{sig.rsi:.0f}</td>" if sig
        else "<td style='padding:8px;'>—</td>"
    )
    return (
        f"<tr style='background:{bg};'>"
        f"<td style='padding:8px 12px;'><strong style='color:{accent}'>{symbol}</strong></td>"
        f"<td style='padding:8px;'>${c.spot_price:,.2f}</td>"
        f"<td style='padding:8px;'><span style='color:{change_color}'>{change_str}</span></td>"
        f"<td style='padding:8px;'>{vol_str}</td>"
        + rsi_cell +
        f"<td style='padding:8px;'>{iv_str}</td>"
        f"<td style='padding:8px;'>"
        f"<span style='color:{'#00d4aa' if sig and sig.iv_back_slope > 0 else '#ff6b6b'}'>{slope_str}</span>"
        f"</td>"
        f"<td style='padding:8px;'>{voi_str}</td>"
        f"<td style='padding:8px;'><strong style='color:{accent}'>{c.composite_score:.0f}</strong></td>"
        f"<td style='padding:8px;'>{len(contracts)} contracts</td>"
        f"</tr>"
    )


def _build_html(
    result: ZeroDTERunResult,
    direction: str,
    contracts: list[ZeroDTEContract],
    config_params: dict,
) -> str:
    accent = _CALL_COLOR if direction == "call" else _PUT_COLOR
    dir_label = "CALL" if direction == "call" else "PUT"
    dir_emoji = "📈" if direction == "call" else "📉"
    scan_dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Group by symbol
    by_symbol: dict[str, list[ZeroDTEContract]] = {}
    for c in contracts:
        by_symbol.setdefault(c.symbol, []).append(c)

    # Stats
    n_symbols = len(by_symbol)
    n_contracts = len(contracts)
    high_conf = sum(1 for c in contracts if c.confidence == "HIGH")
    med_conf = sum(1 for c in contracts if c.confidence == "MEDIUM")

    # Signal table rows
    signal_rows = "".join(
        _signal_summary_row(sym, ctrs, accent, i)
        for i, (sym, ctrs) in enumerate(by_symbol.items())
    )

    # Contract table rows (all)
    contract_rows = "".join(
        _contract_row(c, accent, i) for i, c in enumerate(contracts)
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ZeroDTE {dir_label} Scanner — {result.run_date}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0d1117; color: #e6edf3;
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 13px; line-height: 1.5;
  }}
  .header {{
    background: linear-gradient(135deg, #161b22, #0d1117);
    border-bottom: 1px solid {accent}44;
    padding: 20px 32px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .header h1 {{ font-size: 20px; font-weight: 700; color: {accent}; letter-spacing: -0.5px; }}
  .header .meta {{ text-align: right; color: #7d8590; font-size: 12px; }}
  .stats {{ display: flex; gap: 16px; padding: 16px 32px; flex-wrap: wrap; }}
  .stat-card {{
    background: #161b22; border: 1px solid #30363d;
    border-radius: 8px; padding: 12px 20px; flex: 1; min-width: 120px;
  }}
  .stat-card .label {{ color: #7d8590; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat-card .value {{ font-size: 24px; font-weight: 700; color: {accent}; }}
  .section {{ padding: 0 32px 24px; }}
  .section h2 {{
    font-size: 14px; font-weight: 600; color: #7d8590;
    text-transform: uppercase; letter-spacing: 1px;
    margin-bottom: 12px; padding-bottom: 8px;
    border-bottom: 1px solid #21262d;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    background: #161b22; color: #7d8590;
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    padding: 8px 12px; text-align: left; letter-spacing: 0.5px;
    border-bottom: 1px solid #30363d;
    position: sticky; top: 0;
  }}
  td {{ color: #e6edf3; border-bottom: 1px solid #21262d; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600; background: {accent}22; color: {accent};
  }}
  .formula-box {{
    background: #161b22; border: 1px solid #30363d;
    border-left: 3px solid {accent}; border-radius: 6px;
    padding: 12px 16px; margin: 16px 32px;
    font-family: 'Consolas', monospace; font-size: 12px; color: #ccc;
  }}
  .formula-box strong {{ color: {accent}; }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>{dir_emoji} ZeroDTE {dir_label} Scanner</h1>
    <div style="color:#7d8590;font-size:12px;margin-top:4px;">
      0-2 DTE options with momentum + IV backwardation + VOI surge signals
    </div>
  </div>
  <div class="meta">
    <div>{scan_dt}</div>
    <div style="margin-top:4px;">Scan date: <strong style="color:#e6edf3">{result.run_date}</strong></div>
  </div>
</div>

<div class="stats">
  <div class="stat-card">
    <div class="label">Stocks Evaluated</div>
    <div class="value">{result.candidates_evaluated}</div>
  </div>
  <div class="stat-card">
    <div class="label">{dir_label} Signals</div>
    <div class="value">{result.signals_found_calls if direction == 'call' else result.signals_found_puts}</div>
  </div>
  <div class="stat-card">
    <div class="label">Contracts Found</div>
    <div class="value">{n_contracts}</div>
  </div>
  <div class="stat-card">
    <div class="label">HIGH Confidence</div>
    <div class="value" style="color:{_HIGH_COLOR}">{high_conf}</div>
  </div>
  <div class="stat-card">
    <div class="label">MEDIUM Confidence</div>
    <div class="value" style="color:{_MED_COLOR}">{med_conf}</div>
  </div>
</div>

<div class="formula-box">
  <strong>Pattern:</strong> Signal 1 (Momentum 0-40) + Signal 2 (IV Backwardation 0-30) + Signal 3 (VOI Surge 0-30) = Composite 0-100<br>
  <strong>HIGH</strong> confidence: ≥75 → 0DTE same-day play &nbsp;|&nbsp;
  <strong>MEDIUM</strong>: 60-74 → 1-2DTE for safety &nbsp;|&nbsp;
  <strong>LOW</strong>: 50-59 → monitor only
</div>

<div class="section">
  <h2>📊 Stock Signals ({n_symbols} stocks)</h2>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Price</th><th>Prev Day</th><th>Vol Ratio</th>
        <th>RSI</th><th>IV 1W / 1M</th><th>IV Slope</th><th>VOI Ratio</th>
        <th>Score</th><th>Contracts</th>
      </tr>
    </thead>
    <tbody>
      {signal_rows if signal_rows else '<tr><td colspan="10" style="text-align:center;color:#7d8590;padding:20px;">No signals met threshold</td></tr>'}
    </tbody>
  </table>
</div>

<div class="section">
  <h2>📋 All Contracts ({len(contracts)})</h2>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Contract</th><th>DTE</th><th>Strike</th><th>Spot</th>
        <th>OTM%</th><th>Bid/Ask</th><th>Premium</th><th>Spread</th>
        <th>OI</th><th>Vol</th><th>IV</th><th>Signals</th><th>Score</th><th>Confidence</th>
      </tr>
    </thead>
    <tbody>
      {contract_rows if contract_rows else '<tr><td colspan="15" style="text-align:center;color:#7d8590;padding:20px;">No contracts passed filters</td></tr>'}
    </tbody>
  </table>
</div>

<div style="padding:16px 32px;color:#7d8590;font-size:11px;border-top:1px solid #21262d;">
  ⚠️ This report is for informational purposes only. 0DTE options carry extreme risk.
  Validate all signals before trading. Generated by ZeroDTE Scanner v1.0.
</div>

</body>
</html>"""
    return html


def write_html_report(
    result: ZeroDTERunResult,
    direction: str,
    contracts: list[ZeroDTEContract],
    out_path: Path,
    config_params: dict | None = None,
) -> None:
    """Write the HTML report to out_path, archiving any existing file first."""
    archive_if_exists(out_path)
    html = _build_html(result, direction, contracts, config_params or {})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
