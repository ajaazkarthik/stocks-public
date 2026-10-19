"""
Live Portfolio Dashboard — Streamlit
=====================================
Interactive real-time view of all broker positions.
Reads from the 1-hour position cache populated by global_portfolio_dashboard.py.

Usage:
    streamlit run tradingview/live_dashboard.py [-- --port 8501]

Features:
  • P&L summary cards per broker and currency
  • Interactive position table with broker/currency filters
  • Auto-refreshes every 60 s (or press the Refresh button)
  • Additive — does NOT replace the static HTML report in reports/portfolio_report.html
"""

import sys, os, json, time, argparse
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── repo root (works regardless of cwd at launch) ────────────────────────────
ROOT = Path(__file__).parent.parent
_CACHE_FILE = ROOT / ".cache_global_positions.json"
_CACHE_TTL  = 3600  # 1 hour, matches global_portfolio_dashboard.py

import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Page config — must be called before any other st.* call
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Portfolio Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_positions() -> tuple[list[dict], dict, float]:
    """Load positions from the global dashboard position cache.
    Returns (positions, statuses, saved_at).
    """
    if not _CACHE_FILE.exists():
        return [], {}, 0.0
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        positions = raw.get("positions") or []
        statuses  = raw.get("statuses") or {}
        saved_at  = float(raw.get("saved_at", 0))
        return positions, statuses, saved_at
    except Exception as exc:
        st.warning(f"Could not read position cache: {exc}")
        return [], {}, 0.0


def _age_label(saved_at: float) -> str:
    if saved_at == 0:
        return "no data"
    age_s = time.time() - saved_at
    if age_s < 60:
        return f"{age_s:.0f}s ago"
    elif age_s < 3600:
        return f"{age_s/60:.0f}m ago"
    else:
        return f"{age_s/3600:.1f}h ago"


def _fmt_currency(val: float, ccy: str) -> str:
    symbols = {"AUD": "A$", "USD": "$", "HKD": "HK$", "SGD": "S$",
               "INR": "₹", "JPY": "¥"}
    sym = symbols.get(ccy, f"{ccy} ")
    return f"{sym}{val:,.0f}"


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────

st.sidebar.title("Filters")

# Refresh controls
if st.sidebar.button("🔄 Refresh Now"):
    st.cache_data.clear()
    st.rerun()

auto_refresh = st.sidebar.checkbox("Auto-refresh (60 s)", value=True)
st.sidebar.divider()

# ──────────────────────────────────────────────────────────────────────────────
# Load data
# ──────────────────────────────────────────────────────────────────────────────

positions, statuses, saved_at = load_positions()

# ──────────────────────────────────────────────────────────────────────────────
# Header
# ──────────────────────────────────────────────────────────────────────────────

col_title, col_age = st.columns([4, 1])
with col_title:
    st.title("📈 Live Portfolio Dashboard")
with col_age:
    age = _age_label(saved_at)
    color = "green" if saved_at and (time.time() - saved_at) < 600 else "orange"
    st.markdown(f"<br><span style='color:{color};font-size:14px'>Data: {age}</span>",
                unsafe_allow_html=True)

if not positions:
    st.warning(
        "No position data found. Run option **1** (Global Portfolio Dashboard) from "
        "`main.py` first to populate the cache file.\n\n"
        f"Expected cache file: `{_CACHE_FILE}`"
    )
    if auto_refresh:
        time.sleep(10)
        st.cache_data.clear()
        st.rerun()
    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# Build broker / currency lists for filters
# ──────────────────────────────────────────────────────────────────────────────

all_brokers    = sorted({p.get("broker", "Unknown") for p in positions})
all_currencies = sorted({p.get("currency", "USD") for p in positions})

selected_brokers = st.sidebar.multiselect(
    "Brokers", options=all_brokers, default=all_brokers
)
selected_currencies = st.sidebar.multiselect(
    "Currencies", options=all_currencies, default=all_currencies
)

# ──────────────────────────────────────────────────────────────────────────────
# Apply filters
# ──────────────────────────────────────────────────────────────────────────────

filtered = [
    p for p in positions
    if p.get("broker", "Unknown") in selected_brokers
    and p.get("currency", "USD") in selected_currencies
]

# ──────────────────────────────────────────────────────────────────────────────
# Broker status bar
# ──────────────────────────────────────────────────────────────────────────────

if statuses:
    with st.expander("Broker Status", expanded=False):
        cols = st.columns(min(len(statuses), 3))
        for i, (broker, status) in enumerate(statuses.items()):
            with cols[i % len(cols)]:
                ok = not any(kw in status.lower() for kw in
                             ("error", "fail", "not running", "timeout", "missing"))
                icon = "✅" if ok else "⚠️"
                st.markdown(f"**{icon} {broker}**\n\n`{status[:80]}`")

# ──────────────────────────────────────────────────────────────────────────────
# Summary cards — total market value and P&L per currency
# ──────────────────────────────────────────────────────────────────────────────

st.subheader("Portfolio Summary")
# Approximate AUD conversion rates — used both for the overall AUD panel and
# the per-broker chart so all values are on a common scale.
# Individual currency cards below show exact local-currency values.
_FX_TO_AUD: dict[str, float] = {
    "AUD": 1.0,  "USD": 1.54, "HKD": 0.197, "SGD": 1.14,
    "INR": 0.0185, "JPY": 0.0099, "GBP": 1.95, "CAD": 1.14, "KRW": 0.0011,
}
# Group by currency
from collections import defaultdict
ccy_groups: dict[str, dict] = defaultdict(lambda: {"market_val": 0.0, "cost": 0.0, "count": 0})

for p in filtered:
    ccy = p.get("currency", "USD")
    ccy_groups[ccy]["market_val"] += float(p.get("market_val", 0) or 0)
    ccy_groups[ccy]["cost"]       += float(p.get("avg_cost", 0) or 0) * float(p.get("shares", 0) or 0)
    ccy_groups[ccy]["count"]      += 1

# Overall AUD portfolio summary panel
if ccy_groups:
    # Compute AUD-equivalent totals across all positions
    total_value_aud = 0.0
    total_cost_aud  = 0.0
    for ccy, vals in ccy_groups.items():
        fx = _FX_TO_AUD.get(ccy.upper(), 1.0)
        total_value_aud += vals["market_val"] * fx
        total_cost_aud  += vals["cost"] * fx
    total_pnl_aud     = total_value_aud - total_cost_aud
    total_pnl_pct_aud = (total_pnl_aud / total_cost_aud * 100) if total_cost_aud else 0.0

    st.markdown("**Overall Portfolio Value (AUD equiv.)**")
    aud_cols = st.columns(3)
    with aud_cols[0]:
        st.metric(
            label="Total Value",
            value=f"A${total_value_aud:,.0f}",
            delta=f"P&L: A${total_pnl_aud:+,.0f} ({total_pnl_pct_aud:+.1f}%)",
            delta_color="normal",
        )
    with aud_cols[1]:
        st.metric(label="Total Cost Basis", value=f"A${total_cost_aud:,.0f}")
    with aud_cols[2]:
        n_positions = sum(v["count"] for v in ccy_groups.values())
        n_currencies = len(ccy_groups)
        st.metric(
            label="Positions",
            value=str(n_positions),
            delta=f"{n_currencies} currencies",
            delta_color="off",
        )
    st.caption(
        "\u26a0\ufe0f AUD totals are approximate — using fixed FX rates. "
        "Individual cards below show exact local-currency values."
    )
    st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# Per-currency KPI cards
# ─────────────────────────────────────────────────────────────────────────────
if ccy_groups:
    summary_cols = st.columns(min(len(ccy_groups), 4))
    for i, (ccy, vals) in enumerate(sorted(ccy_groups.items())):
        mval  = vals["market_val"]
        cost  = vals["cost"]
        pnl   = mval - cost
        pnl_pct = (pnl / cost * 100) if cost else 0
        with summary_cols[i % 4]:
            delta_str = f"{'+' if pnl>=0 else ''}{_fmt_currency(pnl, ccy)} ({pnl_pct:+.1f}%)"
            st.metric(
                label=f"{ccy} — {vals['count']} positions",
                value=_fmt_currency(mval, ccy),
                delta=delta_str,
                delta_color="normal",
            )

# ──────────────────────────────────────────────────────────────────────────────
# Per-broker P&L chart
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("P&L by Broker")

# Approximate AUD conversion rates were moved to top of Portfolio Summary section

broker_pnl: dict[str, float] = defaultdict(float)
for p in filtered:
    broker = p.get("broker", "Unknown")
    ccy    = str(p.get("currency", "USD")).upper()
    fx     = _FX_TO_AUD.get(ccy, 1.0)
    broker_pnl[broker] += float(p.get("pnl", 0) or 0) * fx

if broker_pnl:
    try:
        import pandas as pd
        df_chart = pd.DataFrame({
            "Broker":          list(broker_pnl.keys()),
            "P&L (AUD equiv.)": list(broker_pnl.values()),
        }).sort_values("P&L (AUD equiv.)", ascending=False)
        df_chart["Color"] = df_chart["P&L (AUD equiv.)"].apply(
            lambda x: "#2ecc71" if x >= 0 else "#e74c3c"
        )

        # Use Plotly if available for richer chart, else Streamlit native
        try:
            import plotly.express as px
            fig = px.bar(
                df_chart, x="Broker", y="P&L (AUD equiv.)",
                color="Color", color_discrete_map="identity",
                title="Unrealised P&L by Broker (AUD equivalent, approx. FX)",
                labels={"P&L (AUD equiv.)": "P&L (A$ equiv.)"},
            )
            fig.update_layout(showlegend=False, height=300)
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            st.bar_chart(df_chart.set_index("Broker")["P&L (AUD equiv.)"])
    except ImportError:
        for broker, pnl in sorted(broker_pnl.items(), key=lambda x: -x[1]):
            arrow = "▲" if pnl >= 0 else "▼"
            st.markdown(f"**{broker}**: {arrow} A${pnl:+,.2f}")
    st.caption("⚠️ P&L values above converted to approximate AUD for cross-broker comparison. "
               "Individual currency cards show exact local-currency values.")

# ──────────────────────────────────────────────────────────────────────────────
# Position table
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.subheader(f"Positions ({len(filtered)} shown)")

if filtered:
    try:
        import pandas as pd

        rows = []
        for p in filtered:
            rows.append({
                "Symbol":       p.get("symbol", ""),
                "Broker":       p.get("broker", ""),
                "Shares":       p.get("shares", 0),
                "Avg Cost":     p.get("avg_cost", 0),
                "Price":        p.get("current_price", 0),
                "Market Val":   p.get("market_val", 0),
                "P&L":          p.get("pnl", 0),
                "P&L %":        p.get("pnl_pct", 0),
                "Currency":     p.get("currency", "USD"),
                "Exchange":     p.get("exchange", ""),
            })

        df = pd.DataFrame(rows)

        # Sort by P&L descending
        df = df.sort_values("P&L", ascending=False).reset_index(drop=True)

        # Apply colour styling for P&L columns
        def _style_pnl(val):
            if isinstance(val, (int, float)):
                color = "#2ecc71" if val >= 0 else "#e74c3c"
                return f"color: {color}; font-weight: bold"
            return ""

        styled = df.style.applymap(_style_pnl, subset=["P&L", "P&L %"])
        styled = styled.format({
            "Avg Cost":   "{:.2f}",
            "Price":      "{:.2f}",
            "Market Val": "{:,.0f}",
            "P&L":        "{:+,.2f}",
            "P&L %":      "{:+.1f}%",
        })

        st.dataframe(styled, use_container_width=True, height=400)

    except ImportError:
        # No pandas — fall back to plain table
        st.table(filtered)
else:
    st.info("No positions match the current filters.")

# ──────────────────────────────────────────────────────────────────────────────
# Footer
# ──────────────────────────────────────────────────────────────────────────────

st.divider()
st.caption(
    f"Cache: `{_CACHE_FILE}` · Updated: "
    f"{datetime.fromtimestamp(saved_at).strftime('%Y-%m-%d %H:%M:%S') if saved_at else 'never'} · "
    f"Static HTML: `reports/portfolio_report.html`"
)

# ──────────────────────────────────────────────────────────────────────────────
# Auto-refresh
# ──────────────────────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(60)
    st.cache_data.clear()
    st.rerun()


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point (pass-through to streamlit)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import subprocess
    parser = argparse.ArgumentParser(description="Launch live portfolio Streamlit dashboard")
    parser.add_argument("--port",    default="8501",      help="Streamlit server port")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(Path(__file__).resolve()),
        "--server.port", args.port,
        "--server.headless", "false" if not args.no_browser else "true",
        "--",  # separator so Streamlit doesn't eat --port
    ]
    print(f"Launching dashboard on http://localhost:{args.port}")
    subprocess.run(cmd)
