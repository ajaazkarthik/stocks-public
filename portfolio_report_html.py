"""
portfolio_report_html.py — HTML rendering helpers for Global Portfolio Dashboard.

Extracted from global_portfolio_dashboard.py to reduce god-file complexity.
Contains realised P&L HTML generation, coverage row formatting,
and FD comparison calculations.
"""
from __future__ import annotations

# Live FX rates (single source of truth — see fx_rates.py at workspace root)
from fx_rates import get_fx_to_usd, convert as _fx_convert


# ─── FD / Capital helpers ───────────────────────────────────────────────────────

def _compute_fd_value_at_month(deposit_events: list[dict], target_month: str,
                                annual_rate: float = 0.08) -> float:
    """
    Compute total FD value at end of target_month (YYYY-MM format).
    Each deposit compounds at `annual_rate` from its deposit date.
    Withdrawals reduce principal (LIFO — most recent deposits withdrawn first).

    Uses monthly compounding: monthly_rate = (1 + annual_rate)^(1/12) - 1
    """
    from datetime import date as _date
    monthly_rate = (1 + annual_rate) ** (1.0 / 12.0) - 1.0
    target_y, target_m = int(target_month[:4]), int(target_month[5:7])
    # Target date = last day of target_month (approximated as 1st of next month)
    if target_m == 12:
        target_end = _date(target_y + 1, 1, 1)
    else:
        target_end = _date(target_y, target_m + 1, 1)

    total_fd = 0.0
    for ev in deposit_events:
        ev_date = _date.fromisoformat(ev["date"])
        if ev_date >= target_end:
            continue  # future deposit, not yet active

        # Number of months from deposit to target month end
        months_elapsed = (target_y - ev_date.year) * 12 + (target_m - ev_date.month)
        if months_elapsed < 0:
            continue

        amt = ev["amount"]
        if amt > 0:
            # Deposit: compound interest
            total_fd += amt * ((1 + monthly_rate) ** months_elapsed)
        else:
            # Withdrawal: reduce accumulated value (simplified — remove principal)
            total_fd += amt  # negative amount reduces total

    return total_fd


def _compute_capital_deployed_at_month(deposit_events: list[dict], target_month: str) -> float:
    """Sum of all deposits - withdrawals up to end of target_month."""
    total = 0.0
    for ev in deposit_events:
        if ev["date"][:7] <= target_month:
            total += ev["amount"]
    return total


# ─── Coverage row ────────────────────────────────────────────────────────────────

def _html_coverage_row(broker: str, status: str) -> str:
    ok     = status.startswith("Live")
    cached = status.startswith("Cached") or status.startswith("Flex")
    skip   = status.startswith("Skipped")
    warn   = "expired" in status or "Token" in status or "OpenD" in status
    pdf_ok = status.startswith("PDF statements")
    if ok:      badge = '<span class="badge ok">LIVE</span>'
    elif cached:badge = '<span class="badge cached">CACHED</span>'
    elif skip:  badge = '<span class="badge skip">SKIP</span>'
    elif pdf_ok:badge = '<span class="badge pdf">PDF</span>'
    elif warn:  badge = '<span class="badge warn">WARN</span>'
    else:       badge = '<span class="badge fail">FAIL</span>'
    detail = status if len(status) < 60 else status[:57] + "…"
    return f"<tr><td><strong>{broker}</strong></td><td>{badge}</td><td class='dim'>{detail}</td></tr>"


# ─── Realised P&L HTML section ───────────────────────────────────────────────────

def _generate_realised_pnl_html(realised: dict) -> str:
    """
    Build the HTML block for the 'Realised P&L History' section including:
      - Summary table per FY
      - Chart.js bar + FD comparison line chart
    """
    ibkr_rows_data = realised.get("ibkr", [])
    moomoo_rows_data = realised.get("moomoo", [])

    if not ibkr_rows_data and not moomoo_rows_data:
        return (
            "<h2>&#x1F4B0; Realised P&amp;L History</h2>"
            "<p class='dim'>No historical realised P&amp;L data available. "
            "Place IBKR annual statement PDFs in the <code>ibkr-statement/</code> folder.</p>"
        )

    # Build consolidated per-FY data for table + chart
    fy_data: dict[str, dict] = {}  # fy → {ibkr_aud, moomoo_aud, fd_aud, deposits_aud, ...}

    # ── IBKR rows ────────────────────────────────────────────────────────────
    deposit_events_all = realised.get("deposit_events", [])
    cumulative_aud = 0.0
    _fx_rates = get_fx_to_usd()
    for entry in sorted(ibkr_rows_data, key=lambda x: x.get("fy", "")):
        fy  = entry["fy"]
        dep = entry.get("deposits_aud", 0.0)
        cumulative_aud += dep
        realized_aud = _fx_convert(entry.get("realized_usd", 0.0), "USD", "AUD", _fx_rates)
        # Compute proper FD return for this FY using all broker deposits
        _fy_yr = int(fy[2:]) if fy.startswith("FY") else 0
        _fy_end_month = f"{_fy_yr}-06"     # FY ends June
        _fy_start_month = f"{_fy_yr-1}-06"  # Previous FY end
        if deposit_events_all and _fy_yr > 0:
            _fd_end = _compute_fd_value_at_month(deposit_events_all, _fy_end_month)
            _fd_start = _compute_fd_value_at_month(deposit_events_all, _fy_start_month)
            _cap_end = _compute_capital_deployed_at_month(deposit_events_all, _fy_end_month)
            _cap_start = _compute_capital_deployed_at_month(deposit_events_all, _fy_start_month)
            # FD return for this FY = (FD value at end - capital at end) - (FD value at start - capital at start)
            fd_return_aud = (_fd_end - _cap_end) - (_fd_start - _cap_start)
            _cumul_deployed = _cap_end
        else:
            fd_return_aud = cumulative_aud * 0.08
            _cumul_deployed = cumulative_aud
        fy_data[fy] = {
            "ibkr_realized_aud":  realized_aud,
            "ibkr_realized_usd":  entry.get("realized_usd", 0.0),
            "ibkr_st_profit":     entry.get("st_profit_usd", 0.0),
            "ibkr_st_loss":       entry.get("st_loss_usd", 0.0),
            "ibkr_lt_profit":     entry.get("lt_profit_usd", 0.0),
            "ibkr_lt_loss":       entry.get("lt_loss_usd", 0.0),
            "ibkr_deposits_aud":  dep,
            "cumulative_aud":     _cumul_deployed,
            "fd_return_aud":      fd_return_aud,
            "moomoo_realized_aud": 0.0,
            "moomoo_realized_native": 0.0,
            "india_realized_aud": 0.0,
            "webull_realized_aud": 0.0,
            "period":             entry.get("period", ""),
            "start_nav_aud":      entry.get("start_nav_aud", 0.0),
            "end_nav_aud":        entry.get("end_nav_aud", 0.0),
            "dividends_usd":      entry.get("dividends_usd", 0.0),
            "per_symbol":         entry.get("per_symbol", {}),
        }

    # ── FY blank template with proper FD compounding ──────────────────────────
    def _blank_fy_with_fd(fy_str: str) -> dict:
        """Create blank FY dict with proper FD calculation from all deposit events."""
        _yr = int(fy_str[2:]) if fy_str.startswith("FY") else 0
        fd_ret = 0.0
        cap = 0.0
        if deposit_events_all and _yr > 0:
            _fy_end_m = f"{_yr}-06"
            _fy_start_m = f"{_yr-1}-06"
            _fd_e = _compute_fd_value_at_month(deposit_events_all, _fy_end_m)
            _fd_s = _compute_fd_value_at_month(deposit_events_all, _fy_start_m)
            _c_e = _compute_capital_deployed_at_month(deposit_events_all, _fy_end_m)
            _c_s = _compute_capital_deployed_at_month(deposit_events_all, _fy_start_m)
            fd_ret = (_fd_e - _c_e) - (_fd_s - _c_s)
            cap = _c_e
        return {
            "ibkr_realized_aud": 0.0, "ibkr_realized_usd": 0.0,
            "ibkr_deposits_aud": 0.0, "cumulative_aud": cap, "fd_return_aud": fd_ret,
            "moomoo_realized_aud": 0.0, "moomoo_realized_native": 0.0,
            "india_realized_aud": 0.0, "webull_realized_aud": 0.0,
            "period": "", "start_nav_aud": 0.0, "end_nav_aud": 0.0,
            "dividends_usd": 0.0, "per_symbol": {},
            "ibkr_st_profit": 0.0, "ibkr_st_loss": 0.0,
            "ibkr_lt_profit": 0.0, "ibkr_lt_loss": 0.0,
        }

    # ── Moomoo rows ───────────────────────────────────────────────────────────
    for entry in moomoo_rows_data:
        fy  = entry.get("fy", "")
        cur = entry.get("currency", "USD")
        net = entry.get("net_taxable", 0.0)
        net_aud = _fx_convert(net, cur, "AUD", _fx_rates)
        if fy not in fy_data:
            fy_data[fy] = _blank_fy_with_fd(fy)
        fy_data[fy]["moomoo_realized_aud"]    += net_aud
        fy_data[fy]["moomoo_realized_native"] += net

    # ── India rows ────────────────────────────────────────────────────────────
    for entry in realised.get("india", []):
        fy      = entry.get("fy", "")
        net_aud = entry.get("net_aud", 0.0)
        if not fy:
            continue
        if fy not in fy_data:
            fy_data[fy] = _blank_fy_with_fd(fy)
        fy_data[fy]["india_realized_aud"] = fy_data[fy].get("india_realized_aud", 0.0) + net_aud

    # ── Webull rows ───────────────────────────────────────────────────────────
    for entry in realised.get("webull", []):
        fy  = entry.get("fy", "")
        cur = entry.get("currency", "AUD")
        net = entry.get("net_taxable", 0.0)
        net_aud = _fx_convert(net, cur, "AUD", _fx_rates)
        if not fy:
            continue
        if fy not in fy_data:
            fy_data[fy] = _blank_fy_with_fd(fy)
        fy_data[fy]["webull_realized_aud"] = fy_data[fy].get("webull_realized_aud", 0.0) + net_aud

    # Sort FYs
    sorted_fys = sorted(fy_data.keys())

    # ── Chart.js data ─────────────────────────────────────────────────────────
    chart_labels     = [fy for fy in sorted_fys]
    ibkr_aud_vals    = [round(fy_data[fy]["ibkr_realized_aud"], 0) for fy in sorted_fys]
    moomoo_aud_vals  = [round(fy_data[fy]["moomoo_realized_aud"], 0) for fy in sorted_fys]
    india_aud_vals   = [round(fy_data[fy].get("india_realized_aud", 0.0), 0) for fy in sorted_fys]
    fd_vals          = [round(fy_data[fy]["fd_return_aud"], 0) for fy in sorted_fys]

    # ── Build monthly cumulative chart data (3 lines: Capital + Stocks + FD) ──
    import calendar as _cal
    deposit_events = realised.get("deposit_events", [])
    monthly_pnl: dict = dict(realised.get("monthly_trades", {}))  # from Moomoo+Webull trades
    # Distribute IBKR annual P&L evenly across 12 months of each FY
    for _entry in sorted(ibkr_rows_data, key=lambda x: x.get("fy", "")):
        _fy = _entry.get("fy", "")
        if not _fy or not _fy.startswith("FY"):
            continue
        _yr = int(_fy[2:])
        _fy_aud = _fx_convert(_entry.get("realized_usd", 0.0), "USD", "AUD", _fx_rates)
        _mp = _fy_aud / 12.0
        for _m in range(7, 13):
            _k = f"{_yr-1}-{_m:02d}"
            monthly_pnl[_k] = monthly_pnl.get(_k, 0.0) + _mp
        for _m in range(1, 7):
            _k = f"{_yr}-{_m:02d}"
            monthly_pnl[_k] = monthly_pnl.get(_k, 0.0) + _mp
    # Distribute India annual P&L evenly across 12 months of each FY
    for _entry in realised.get("india", []):
        _fy = _entry.get("fy", "")
        if not _fy or not _fy.startswith("FY"):
            continue
        _yr = int(_fy[2:])
        _mp = _entry.get("net_aud", 0.0) / 12.0
        for _m in range(7, 13):
            _k = f"{_yr-1}-{_m:02d}"
            monthly_pnl[_k] = monthly_pnl.get(_k, 0.0) + _mp
        for _m in range(1, 7):
            _k = f"{_yr}-{_m:02d}"
            monthly_pnl[_k] = monthly_pnl.get(_k, 0.0) + _mp
    # Build sorted month list from all data sources
    _dep_months = {ev["date"][:7] for ev in deposit_events}
    _all_months = sorted(set(monthly_pnl.keys()) | _dep_months)
    if _all_months:
        _run_pnl = 0.0
        cumul_all_monthly = []      # cumulative realised P&L only
        cumul_capital_monthly = []   # cumulative deposits (capital deployed)
        cumul_fd_monthly = []        # FD compound interest value
        cumul_portfolio_monthly = [] # capital + cumulative P&L (portfolio value)
        deposit_annotations = []     # {month_idx, label, amount} for chart annotations
        month_labels_nice = []
        for _idx, _mk in enumerate(_all_months):
            _run_pnl += monthly_pnl.get(_mk, 0.0)
            _capital  = _compute_capital_deployed_at_month(deposit_events, _mk)
            _fd_val   = _compute_fd_value_at_month(deposit_events, _mk)
            _portfolio = _capital + _run_pnl
            cumul_all_monthly.append(round(_run_pnl, 0))
            cumul_capital_monthly.append(round(_capital, 0))
            cumul_fd_monthly.append(round(_fd_val, 0))
            cumul_portfolio_monthly.append(round(_portfolio, 0))
            _y, _mo = int(_mk[:4]), int(_mk[5:7])
            month_labels_nice.append(f"{_cal.month_abbr[_mo]} {_y}")
            # Check if any deposits happened this month
            _month_deps = [ev for ev in deposit_events if ev["date"][:7] == _mk and ev["amount"] > 100]
            if _month_deps:
                _total_dep = sum(ev["amount"] for ev in _month_deps)
                _brokers = ", ".join(sorted(set(ev["broker"] for ev in _month_deps)))
                deposit_annotations.append({
                    "idx": _idx, "label": f"+A${_total_dep:,.0f}", "broker": _brokers
                })
    else:
        month_labels_nice = list(sorted_fys)
        _run_pnl = 0.0
        cumul_all_monthly, cumul_fd_monthly = [], []
        cumul_capital_monthly, cumul_portfolio_monthly = [], []
        deposit_annotations = []
        for _fy in sorted_fys:
            _run_pnl += (fy_data[_fy]["ibkr_realized_aud"] + fy_data[_fy]["moomoo_realized_aud"]
                         + fy_data[_fy].get("india_realized_aud", 0.0)
                         + fy_data[_fy].get("webull_realized_aud", 0.0))
            cumul_all_monthly.append(round(_run_pnl, 0))
            cumul_capital_monthly.append(0)
            cumul_fd_monthly.append(0)
            cumul_portfolio_monthly.append(round(_run_pnl, 0))
    # Compute summary stats for chart subtitle
    _total_deposited = sum(ev["amount"] for ev in deposit_events if ev["amount"] > 0)
    _total_withdrawn = sum(abs(ev["amount"]) for ev in deposit_events if ev["amount"] < 0)
    _net_deployed    = _total_deposited - _total_withdrawn
    _fd_final        = cumul_fd_monthly[-1] if cumul_fd_monthly else 0
    _fd_interest     = _fd_final - _net_deployed if _fd_final > 0 else 0
    _stock_final     = cumul_portfolio_monthly[-1] if cumul_portfolio_monthly else 0
    _alpha_over_fd   = _stock_final - _fd_final
    _n_deposit_events = len([ev for ev in deposit_events if ev["amount"] > 0])
    # CF-11 (audit 2026-04-29): Show all 6 brokers — those with events tracked and
    # those where deposit history is unavailable via API.
    _tracked_brokers = sorted(set(ev["broker"] for ev in deposit_events)) if deposit_events else []
    _no_api_brokers  = [b for b in ["AngelOne", "Moomoo-AU", "Moomoo-SG", "Zerodha"]
                        if b not in _tracked_brokers]
    if _tracked_brokers and _no_api_brokers:
        _deposit_brokers = (", ".join(_tracked_brokers)
                            + f"; no deposit API: {', '.join(_no_api_brokers)}")
    elif _tracked_brokers:
        _deposit_brokers = ", ".join(_tracked_brokers)
    else:
        _deposit_brokers = "N/A"

    # Stacked-bar breakdown per year: IBKR + Moomoo (combined total for bar)
    combined_annual = [round(
        ibkr_aud_vals[i] + moomoo_aud_vals[i]
        + [round(fy_data[fy].get("india_realized_aud", 0.0), 0) for fy in sorted_fys][i]
        + [round(fy_data[fy].get("webull_realized_aud", 0.0), 0) for fy in sorted_fys][i]
    , 0) for i in range(len(sorted_fys))]

    chart_js_data = f"""
// ── Chart 1: Annual bar chart (per-year P&L vs FD) ──────────────────────────
(function() {{
  var ctx1 = document.getElementById('annualPnlChart').getContext('2d');
  new Chart(ctx1, {{
    type: 'bar',
    data: {{
      labels: {chart_labels},
      datasets: [
        {{
          label: 'IBKR Realized ≈AUD',
          data: {ibkr_aud_vals},
          backgroundColor: 'rgba(88,166,255,0.75)',
          borderColor: '#58a6ff',
          borderWidth: 1,
          stack: 'stocks'
        }},
        {{
          label: 'Moomoo Realized ≈AUD',
          data: {moomoo_aud_vals},
          backgroundColor: 'rgba(46,204,113,0.75)',
          borderColor: '#2ecc71',
          borderWidth: 1,
          stack: 'stocks'
        }},
        {{
          label: 'India Realized ≈AUD',
          data: {india_aud_vals},
          backgroundColor: 'rgba(240,130,50,0.75)',
          borderColor: '#f08232',
          borderWidth: 1,
          stack: 'stocks'
        }},
        {{
          label: 'Webull Realized ≈AUD',
          data: [{", ".join(str(round(fy_data[fy].get("webull_realized_aud", 0.0), 0)) for fy in sorted_fys)}],
          backgroundColor: 'rgba(255,107,53,0.75)',
          borderColor: '#ff6b35',
          borderWidth: 1,
          stack: 'stocks'
        }},
        {{
          label: 'FD @ 8% (Annual Equivalent)',
          data: {fd_vals},
          backgroundColor: 'rgba(240,184,75,0.45)',
          borderColor: '#f0b84b',
          borderWidth: 2,
          borderDash: [4,3],
          stack: 'fd',
          type: 'bar'
        }}
      ]
    }},
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ labels: {{ color: '#e6edf3', font: {{ size: 11 }} }} }},
        tooltip: {{
          callbacks: {{
            label: function(ctx) {{
              return ctx.dataset.label + ': A$' + ctx.parsed.y.toLocaleString();
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }}, stacked: true }},
        y: {{
          stacked: true,
          ticks: {{ color: '#8b949e', callback: function(v) {{ return 'A$' + v.toLocaleString(); }} }},
          grid: {{ color: '#21262d' }}
        }}
      }}
    }}
  }});
}})();

// ── Chart 2: Your Money's Journey – Stocks vs FD @ 8% ──────────────────────
(function() {{
  var ctx2 = document.getElementById('cumulPnlChart').getContext('2d');
  // Deposit event annotations
  var depositAnnotations = {deposit_annotations};
  new Chart(ctx2, {{
    type: 'line',
    data: {{
      labels: {month_labels_nice},
      datasets: [
        {{
          label: 'Capital Deployed ≈AUD',
          data: {cumul_capital_monthly},
          borderColor: '#8b949e',
          backgroundColor: 'rgba(139,148,158,0.08)',
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: true,
          stepped: 'after',
          order: 3
        }},
        {{
          label: 'Stock Portfolio (Capital + Returns)',
          data: {cumul_portfolio_monthly},
          borderColor: '#58a6ff',
          backgroundColor: 'rgba(88,166,255,0.12)',
          borderWidth: 2.5,
          pointBackgroundColor: '#58a6ff',
          pointRadius: 2,
          pointHoverRadius: 6,
          fill: true,
          tension: 0.3,
          order: 1
        }},
        {{
          label: 'FD @ 8% (Compound Interest)',
          data: {cumul_fd_monthly},
          borderColor: '#f0b84b',
          backgroundColor: 'rgba(240,184,75,0.05)',
          borderWidth: 2.5,
          borderDash: [6,4],
          pointBackgroundColor: '#f0b84b',
          pointRadius: 2,
          pointHoverRadius: 6,
          fill: false,
          tension: 0.3,
          order: 2
        }}
      ]
    }},
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ labels: {{ color: '#e6edf3', font: {{ size: 11 }} }} }},
        tooltip: {{
          callbacks: {{
            title: function(items) {{
              if (!items.length) return '';
              var idx = items[0].dataIndex;
              var dep = depositAnnotations.find(function(d) {{ return d.idx === idx; }});
              var title = items[0].label;
              if (dep) title += '  ⬆ Deposit: ' + dep.label + ' (' + dep.broker + ')';
              return title;
            }},
            label: function(ctx) {{
              var val = ctx.parsed.y;
              var label = ctx.dataset.label;
              return label + ': A$' + val.toLocaleString();
            }},
            afterBody: function(items) {{
              if (!items.length) return '';
              var idx = items[0].dataIndex;
              var portfolio = {cumul_portfolio_monthly}[idx] || 0;
              var fd = {cumul_fd_monthly}[idx] || 0;
              var alpha = portfolio - fd;
              var sign = alpha >= 0 ? '+' : '';
              return '\\n━━━ Alpha over FD: ' + sign + 'A$' + Math.round(alpha).toLocaleString();
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8b949e', maxTicksLimit: 24, maxRotation: 45 }}, grid: {{ color: '#21262d' }} }},
        y: {{
          ticks: {{ color: '#8b949e', callback: function(v) {{ return 'A$' + v.toLocaleString(); }} }},
          grid: {{ color: '#21262d' }}
        }}
      }}
    }},
    plugins: [{{
      // Draw vertical deposit markers
      afterDraw: function(chart) {{
        var meta = chart.getDatasetMeta(0);
        if (!meta || !meta.data) return;
        var ctx = chart.ctx;
        var yAxis = chart.scales.y;
        depositAnnotations.forEach(function(dep) {{
          var point = meta.data[dep.idx];
          if (!point) return;
          var x = point.x;
          ctx.save();
          ctx.beginPath();
          ctx.setLineDash([3,3]);
          ctx.strokeStyle = 'rgba(46,204,113,0.5)';
          ctx.lineWidth = 1;
          ctx.moveTo(x, yAxis.top);
          ctx.lineTo(x, yAxis.bottom);
          ctx.stroke();
          ctx.font = '10px sans-serif';
          ctx.fillStyle = '#2ecc71';
          ctx.textAlign = 'center';
          ctx.fillText(dep.label, x, yAxis.top - 4);
          ctx.restore();
        }});
      }}
    }}]
  }});
}})();
"""

    # ── Summary table rows ────────────────────────────────────────────────────
    table_rows = ""
    for fy in sorted_fys:
        d = fy_data[fy]
        ibkr_aud  = d["ibkr_realized_aud"]
        mm_aud    = d["moomoo_realized_aud"]
        ind_aud   = d.get("india_realized_aud", 0.0)
        wb_aud    = d.get("webull_realized_aud", 0.0)
        total_aud = ibkr_aud + mm_aud + ind_aud + wb_aud
        fd_aud    = d["fd_return_aud"]
        cls_ibkr  = "pos" if ibkr_aud >= 0 else "neg"
        cls_mm    = "pos" if mm_aud >= 0 else "neg"
        cls_ind   = "pos" if ind_aud >= 0 else "neg"
        cls_wb    = "pos" if wb_aud >= 0 else "neg"
        cls_tot   = "pos" if total_aud >= 0 else "neg"
        cls_fd    = "pos"
        vs_fd     = total_aud - fd_aud
        cls_vs    = "pos" if vs_fd >= 0 else "neg"
        mm_cell   = (f"<span class='{cls_mm}'>A${mm_aud:+,.0f}</span>"
                     if mm_aud != 0 else "<span class='dim'>—</span>")
        ind_cell  = (f"<span class='{cls_ind}'>A${ind_aud:+,.0f}</span>"
                     if ind_aud != 0 else "<span class='dim'>—</span>")
        wb_cell   = (f"<span class='{cls_wb}'>A${wb_aud:+,.0f}</span>"
                     if wb_aud != 0 else "<span class='dim'>—</span>")
        table_rows += (
            f"<tr>"
            f"<td><strong>{fy}</strong><br><span class='dim' style='font-size:11px'>{d['period']}</span></td>"
            f"<td class='num'><span class='{cls_ibkr}'>A${ibkr_aud:+,.0f}</span>"
            f"<br><span class='dim' style='font-size:11px'>${d['ibkr_realized_usd']:+,.0f} USD</span></td>"
            f"<td class='num'>{mm_cell}</td>"
            f"<td class='num'>{wb_cell}</td>"
            f"<td class='num'>{ind_cell}</td>"
            f"<td class='num'><span class='{cls_tot}'><strong>A${total_aud:+,.0f}</strong></span></td>"
            f"<td class='num'><span class='{cls_fd}'>A${fd_aud:,.0f}</span>"
            f"<br><span class='dim' style='font-size:11px'>on A${d['cumulative_aud']:,.0f} deployed</span></td>"
            f"<td class='num'><span class='{cls_vs}'>A${vs_fd:+,.0f}</span></td>"
            f"</tr>\n"
        )

    # ── Status note ───────────────────────────────────────────────────────────
    status_items = realised.get("status", {})
    status_html  = " &nbsp;|&nbsp; ".join(
        f"<span class='dim'>{k}: {v}</span>" for k, v in status_items.items()
    )

    # ── Current FY card (in-progress year, restored 2026-04-21) ─────────────
    current_fy_card = ""
    if sorted_fys:
        # Latest FY tag = current/in-progress (CGT modules emit FY tags that
        # sort lexically; "FY2026" > "FY2025"). Cross-broker FY semantics differ
        # (AU/SG: Jul-Jun, IBKR: Jan-Dec, India: Apr-Mar) — we show the same
        # tag and let the per-broker columns convey jurisdiction.
        cur_fy = sorted_fys[-1]
        cur   = fy_data.get(cur_fy, {})
        cur_ibkr = cur.get("ibkr_realized_aud", 0.0)
        cur_mm   = cur.get("moomoo_realized_aud", 0.0)
        cur_wb   = cur.get("webull_realized_aud", 0.0)
        cur_ind  = cur.get("india_realized_aud", 0.0)
        cur_tot  = cur_ibkr + cur_mm + cur_wb + cur_ind
        def _cell(v: float, label: str) -> str:
            cls = "pos" if v >= 0 else ("neg" if v < 0 else "dim")
            txt = f"A${v:+,.0f}" if v != 0 else "—"
            return (
                f"<div style='text-align:center;padding:6px 4px'>"
                f"<div class='dim' style='font-size:11px;margin-bottom:4px'>{label}</div>"
                f"<div class='{cls}' style='font-size:14px;font-weight:600'>{txt}</div>"
                f"</div>"
            )
        cur_total_cls = "pos" if cur_tot >= 0 else "neg"
        current_fy_card = f"""
<div style='background:#161b22;border:1px solid #f0b84b;border-radius:8px;padding:14px 16px;margin-bottom:16px'>
  <h3 style='color:#f0b84b;margin:0 0 10px 0;font-size:13px'>
    &#x1F4C5; Current FY Realised P&amp;L (in-progress) — {cur_fy}
    <span style='color:#8b949e;font-size:11px;font-weight:400;margin-left:8px'>
      AU/SG: Jul–Jun · IBKR: Jan–Dec · India: Apr–Mar (FY tags differ by jurisdiction)
    </span>
  </h3>
  <div style='display:grid;grid-template-columns:repeat(5,1fr);gap:8px'>
    {_cell(cur_ibkr, "IBKR")}
    {_cell(cur_mm,   "Moomoo")}
    {_cell(cur_wb,   "Webull")}
    {_cell(cur_ind,  "India")}
    <div style='text-align:center;padding:6px 4px;border-left:1px solid #21262d'>
      <div class='dim' style='font-size:11px;margin-bottom:4px'>Combined</div>
      <div class='{cur_total_cls}' style='font-size:18px;font-weight:700'>A${cur_tot:+,.0f}</div>
    </div>
  </div>
</div>"""

    # ── Per-symbol breakdown (IBKR) ───────────────────────────────────────────
    all_syms: dict[str, dict] = {}  # sym → {fy: amount}
    for fy in sorted_fys:
        for sym, data in fy_data[fy].get("per_symbol", {}).items():
            if sym not in all_syms:
                all_syms[sym] = {}
            all_syms[sym][fy] = data["total"]

    sym_rows = ""
    for sym in sorted(all_syms.keys(), key=lambda s: -sum(all_syms[s].values())):
        total_realized = sum(all_syms[sym].values())
        cls = "pos" if total_realized >= 0 else "neg"
        cells = ""
        for fy in sorted_fys:
            v = all_syms[sym].get(fy, 0)
            c = "pos" if v > 0 else ("neg" if v < 0 else "dim")
            cells += f"<td class='num'><span class='{c}'>${v:+,.2f}</span></td>"
        sym_rows += (
            f"<tr><td><strong>{sym}</strong></td>{cells}"
            f"<td class='num'><span class='{cls}'><strong>${total_realized:+,.2f}</strong></span></td></tr>\n"
        )

    fy_headers = "".join(f"<th class='num'>{fy}</th>" for fy in sorted_fys)

    # ── India current portfolio card ──────────────────────────────────────────
    india_positions = realised.get("india_positions", [])
    if india_positions:
        z_holdings = [p for p in india_positions if p.get("broker") == "Zerodha"]
        a_holdings = [p for p in india_positions if p.get("broker") == "AngelOne"]
        def _india_broker_row(broker_name: str, holdings: list) -> str:
            if not holdings:
                return f"<tr><td><strong>{broker_name}</strong></td><td colspan='4' class='dim'>No data</td></tr>"
            n      = len(holdings)
            mval   = sum(p.get("market_val", 0) for p in holdings)
            pnl    = sum(p.get("pnl", 0) for p in holdings)
            cost   = sum(p.get("avg_cost", 0) * p.get("shares", 0) for p in holdings)
            pct    = pnl / cost * 100 if cost else 0
            cls    = "pos" if pnl >= 0 else "neg"
            return (f"<tr>"
                    f"<td><strong>{broker_name}</strong></td>"
                    f"<td class='num'>{n}</td>"
                    f"<td class='num'>₹{mval:,.0f}</td>"
                    f"<td class='num {cls}'>₹{pnl:+,.0f}</td>"
                    f"<td class='num {cls}'>{pct:+.1f}%</td>"
                    f"</tr>\n")
        india_card_html = f"""
<div style='background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:20px'>
  <h3 style='color:#f0b84b;margin-bottom:10px;font-size:13px'>
    &#x1F1EE;&#x1F1F3; India Brokers — Current Holdings (Unrealized P&amp;L)
    <span style='color:#8b949e;font-size:11px;font-weight:400;margin-left:8px'>
      Historical realized P&amp;L not available via API — download statement from Kite/AngelOne console
    </span>
  </h3>
  <table>
    <tr>
      <th>Broker</th>
      <th class='num'>Holdings</th>
      <th class='num'>Market Value</th>
      <th class='num'>Unrealized P&amp;L</th>
      <th class='num'>Return %</th>
    </tr>
    {_india_broker_row("Zerodha", z_holdings)}
    {_india_broker_row("AngelOne", a_holdings)}
  </table>
</div>"""
    else:
        india_card_html = ""

    return f"""
<h2>&#x1F4B0; Realised P&amp;L History</h2>
<p class='dim' style='margin-bottom:12px;font-size:12px'>{status_html}</p>
{current_fy_card}
<table>
  <tr>
    <th>Financial Year</th>
    <th class='num'>IBKR (≈AUD)</th>
    <th class='num'>Moomoo (≈AUD)</th>
    <th class='num'>Webull (≈AUD)</th>
    <th class='num'>India (≈AUD)</th>
    <th class='num'>Total (≈AUD)</th>
    <th class='num'>FD @ 8% Interest</th>
    <th class='num'>vs FD (+/-)</th>
  </tr>
  {table_rows}
</table>

<div style='background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px;margin-bottom:16px'>
  <h3 style='color:#79c0ff;margin-bottom:4px;font-size:13px'>
    Annual P&amp;L vs FD @ 8% Interest
    <span style='color:#8b949e;font-size:11px;font-weight:400;margin-left:8px'>
      (per financial year; FD compounded monthly on all broker deposits)
    </span>
  </h3>
  <canvas id='annualPnlChart' height='80'></canvas>
</div>

<div style='background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px;margin-bottom:20px'>
  <h3 style='color:#79c0ff;margin-bottom:4px;font-size:13px'>
    Your Money's Journey: Stocks vs Fixed Deposit @ 8%
    <span style='color:#8b949e;font-size:11px;font-weight:400;margin-left:8px'>
      (monthly compounding · {_n_deposit_events} deposits tracked · {_deposit_brokers})
    </span>
  </h3>
  <div style='display:flex;gap:20px;margin:8px 0 12px 0;font-size:12px'>
    <span style='color:#8b949e'>Deposited: <strong style='color:#e6edf3'>A${_total_deposited:,.0f}</strong></span>
    {"<span style='color:#8b949e'>Withdrawn: <strong style='color:#f85149'>A$" + f"{_total_withdrawn:,.0f}" + "</strong></span>" if _total_withdrawn > 0 else ""}
    <span style='color:#8b949e'>Stock Portfolio: <strong style='color:#58a6ff'>A${_stock_final:,.0f}</strong></span>
    <span style='color:#8b949e'>FD Would Be: <strong style='color:#f0b84b'>A${_fd_final:,.0f}</strong></span>
    <span style='color:#8b949e'>Alpha: <strong style='color:{"#2ecc71" if _alpha_over_fd >= 0 else "#f85149"}'>{"+" if _alpha_over_fd >= 0 else ""}A${_alpha_over_fd:,.0f}</strong></span>
  </div>
  <canvas id='cumulPnlChart' height='80'></canvas>
</div>

{india_card_html}

<details style='margin-bottom:20px'>
  <summary style='cursor:pointer;color:#79c0ff;font-size:13px;font-weight:600;padding:8px 0'>
    &#x1F50D; IBKR Per-Symbol Breakdown (click to expand)
  </summary>
  <table style='margin-top:12px'>
    <tr>
      <th>Symbol</th>
      {fy_headers}
      <th class='num'>Total (USD)</th>
    </tr>
    {sym_rows if sym_rows else "<tr><td colspan='10' class='dim'>No per-symbol data</td></tr>"}
  </table>
</details>

<script>
{chart_js_data}
</script>
"""
