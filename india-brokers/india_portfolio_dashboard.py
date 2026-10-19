"""
India Combined Portfolio Dashboard
====================================
Fetches live data from BOTH Zerodha and Angel One, then generates a single
HTML report combining all NSE/BSE holdings into one unified view.

Output: india_portfolio_dashboard.html (auto-opens in browser)

Usage:
  python india_portfolio_dashboard.py
  python india_portfolio_dashboard.py --no-open   # skip auto-open

Requirements:
  ENV: ZERODHA_ACCESS_TOKEN  (optional — omit to show Angel One holdings only)
  ENV: ANGELONE_TOTP_SECRET  (mandatory)
  pip install kiteconnect smartapi-python pyotp
"""
from __future__ import annotations
import os, sys, json, webbrowser
from datetime import datetime
from pathlib import Path

# ── Report analytics DB (optional — non-fatal if unavailable) ─────────────────
_REPORTS_DIR = Path(__file__).parent.parent / "reports"
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if str(_REPORTS_DIR) not in sys.path:
    sys.path.insert(0, str(_REPORTS_DIR))
from html_utils import system_footer_html  # C-12

os.environ.setdefault("ZERODHA_API_KEY",      "")
os.environ.setdefault("ANGELONE_API_KEY",     "")
os.environ.setdefault("ANGELONE_CLIENT_CODE", "")
os.environ.setdefault("ANGELONE_PIN",         "")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "")

_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")
OUTPUT = os.path.normpath(os.path.join(_REPORTS_DIR, "india_portfolio_dashboard.html"))

# ── Data fetch ────────────────────────────────────────────────────────────────

def _load_zerodha():
    from zerodha_auth import get_kite, invalidate_token
    try:
        kite = get_kite()
        holdings = kite.holdings() or []
        try:
            margins = kite.margins() or {}
        except Exception:
            margins = {}   # unavailable outside trading hours — non-fatal
        try:
            profile = kite.profile() or {}
        except Exception:
            profile = {}
        return holdings, margins, profile
    except EnvironmentError:
        # No token available (user skipped login)
        return [], {}, {}
    except Exception as e:
        err = str(e)
        if "Incorrect" in err or "TokenException" in type(e).__name__:
            invalidate_token()
            raise PermissionError(f"Zerodha token expired: {e}") from e
        raise


def _load_angelone():
    import pyotp
    from SmartApi import SmartConnect
    import logging as _l; _l.getLogger("logzero_default").setLevel(_l.CRITICAL)
    totp    = pyotp.TOTP(os.environ["ANGELONE_TOTP_SECRET"]).now()
    smart   = SmartConnect(api_key=os.environ["ANGELONE_API_KEY"])
    sess    = smart.generateSession(
                  os.environ["ANGELONE_CLIENT_CODE"],
                  os.environ["ANGELONE_PIN"], totp)
    if not sess.get("status"):
        return [], {}, []
    holdings = (smart.holding().get("data") or [])
    rms      = (smart.rmsLimit().get("data") or {})
    mf       = _fetch_angelone_mf(smart, sess)
    return holdings, rms, mf


def _fetch_angelone_mf(smart, sess: dict) -> list[dict]:
    """Fetch AngelOne mutual fund holdings. Returns [] if none or unavailable."""
    try:
        # Try allholding() which may include mutualFundHoldings
        ah = smart.allholding()
        data = ah.get("data") or {}
        raw = data.get("mutualFundHoldings") or []
        if isinstance(raw, list) and raw:
            result = []
            for h in raw:
                qty   = float(h.get("quantity", 0) or 0)
                avg   = float(h.get("averagePrice", 0) or 0)
                nav   = float(h.get("currentNav", 0) or 0)
                cur   = float(h.get("currentAmount", 0) or 0)
                inv   = float(h.get("investedAmount", 0) or 0)
                pnl   = float(h.get("profitAndLoss", 0) or 0)
                pnl_p = float(h.get("pnlPercentage", 0) or 0)
                if qty <= 0:
                    continue
                result.append({
                    "fund":     h.get("schemeName", h.get("tradingsymbol", "Unknown Fund")),
                    "folio":    h.get("folioNo", ""),
                    "isin":     h.get("tradingsymbol", ""),
                    "qty":      round(qty, 3),
                    "avg_nav":  round(avg, 4),
                    "nav":      round(nav, 4),
                    "cur_val":  round(cur, 2),
                    "inv_val":  round(inv, 2),
                    "pnl":      round(pnl, 2),
                    "pnl_pct":  round(pnl_p, 2),
                    "broker":   "AngelOne",
                })
            return result
    except Exception:
        pass

    # Fallback: try direct REST API using session JWT token
    try:
        import requests
        sess_data = sess.get("data") or {}
        token = sess_data.get("jwtToken", "")
        if not token:
            return []
        hdrs = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-ClientCode": os.environ.get("ANGELONE_CLIENT_CODE", ""),
            "X-PrivateKey": os.environ.get("ANGELONE_API_KEY", ""),
        }
        url  = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/mutualFunds/v1/allholdings"
        resp = requests.get(url, headers=hdrs, timeout=10)
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
            body = resp.json()
            raw  = (body.get("data") or [])
            result = []
            for h in (raw if isinstance(raw, list) else []):
                qty = float(h.get("quantity", 0) or 0)
                if qty <= 0:
                    continue
                avg = float(h.get("averagePrice", h.get("average_price", 0)) or 0)
                nav = float(h.get("currentNav", h.get("nav", 0)) or 0)
                cur = nav * qty
                inv = avg * qty
                pnl = cur - inv
                pnl_p = (pnl / inv * 100) if inv else 0
                result.append({
                    "fund":    h.get("schemeName", h.get("name", "Unknown Fund")),
                    "folio":   h.get("folioNo", h.get("folio", "")),
                    "isin":    h.get("isin", h.get("tradingsymbol", "")),
                    "qty":     round(qty, 3),
                    "avg_nav": round(avg, 4),
                    "nav":     round(nav, 4),
                    "cur_val": round(cur, 2),
                    "inv_val": round(inv, 2),
                    "pnl":     round(pnl, 2),
                    "pnl_pct": round(pnl_p, 2),
                    "broker":  "AngelOne",
                })
            return result
    except Exception:
        pass
    return []


def _fetch_zerodha_mf(kite) -> list[dict]:
    """Fetch Zerodha mutual fund holdings via kite.mf_holdings(). Returns [] if none."""
    try:
        raw = kite.mf_holdings() or []
        result = []
        for h in raw:
            qty = float(h.get("quantity", 0) or 0)
            if qty <= 0:
                continue
            avg = float(h.get("average_price", 0) or 0)
            nav = float(h.get("last_price", 0) or 0)
            cur = nav * qty
            inv = avg * qty
            pnl = float(h.get("pnl", 0) or cur - inv)
            pnl_p = (pnl / inv * 100) if inv else 0
            result.append({
                "fund":    h.get("fund", h.get("tradingsymbol", "Unknown Fund")),
                "folio":   h.get("folio", ""),
                "isin":    h.get("tradingsymbol", ""),
                "qty":     round(qty, 3),
                "avg_nav": round(avg, 4),
                "nav":     round(nav, 4),
                "cur_val": round(cur, 2),
                "inv_val": round(inv, 2),
                "pnl":     round(pnl, 2),
                "pnl_pct": round(pnl_p, 2),
                "broker":  "Zerodha",
            })
        return result
    except Exception:
        return []


def _strip_eq(s: str) -> str:
    return s[:-3] if s.upper().endswith("-EQ") else s

# ── HTML generation ───────────────────────────────────────────────────────────

def _row_color(pnl_pct: float) -> str:
    if pnl_pct > 5:   return "#0d2b0d"
    if pnl_pct > -10: return "#1a1a0a"
    if pnl_pct > -25: return "#1f0a0a"
    return "#2b0a1a"


def _load_mf_history_from_statements() -> list[dict]:
    """Load MF realized P&L rows from AngelOne/Zerodha statement readers."""
    import importlib.util as _ilu
    from pathlib import Path as _Path
    _root = _Path(__file__).parent.parent.resolve()
    result = []
    try:
        spec = _ilu.spec_from_file_location(
            "angelone_statement_reader",
            _root / "angelone-statement" / "angelone_statement_reader.py")
        m = _ilu.module_from_spec(spec)
        spec.loader.exec_module(m)
        for r in m.build_summary_for_dashboard(folder=_root / "angelone-statement"):
            ltcg = r.get("mf_ltcg_inr", 0.0)
            stcg = r.get("mf_stcg_inr", 0.0)
            if ltcg or stcg:
                result.append({"fy": r["fy"], "broker": r["broker"],
                                "ltcg": ltcg, "stcg": stcg, "total": ltcg + stcg})
    except Exception:
        pass
    return result


def _build_mf_section_html(mf_list: list[dict]) -> str:
    """Build the Mutual Funds section HTML."""
    mf_hist = _load_mf_history_from_statements()

    if not mf_list:
        # No live holdings — show realized history if available
        hist_html = ""
        if mf_hist:
            hist_rows = ""
            for h in mf_hist:
                ltcg_col = "#4caf50" if h["ltcg"] >= 0 else "#f44336"
                hist_rows += f"""
        <tr>
          <td style="color:#aaa">{h['fy']}</td>
          <td style="color:#e8a85c">{h['broker']}</td>
          <td class="num-b" style="color:{ltcg_col}">₹{h['ltcg']:,.0f}</td>
          <td class="num">₹{h['stcg']:,.0f}</td>
          <td class="num-b">₹{h['total']:,.0f}</td>
        </tr>"""
            total_mf = sum(h["total"] for h in mf_hist)
            tc = "#4caf50" if total_mf >= 0 else "#f44336"
            ts = "+" if total_mf >= 0 else ""
            hist_html = f"""
<div style="margin-top:10px;color:#7eb8f0;font-size:.8rem;font-weight:bold;text-transform:uppercase;letter-spacing:.05em">
  Realized MF Capital Gains (from Statements)
</div>
<table>
  <thead><tr>
    <th>FY</th><th>Broker</th>
    <th style="color:#ffd700">MF LTCG (₹)</th>
    <th>MF STCG (₹)</th>
    <th>Total Realized</th>
  </tr></thead>
  <tbody>
    {hist_rows}
    <tr style="background:#161630;font-weight:bold">
      <td colspan="2">TOTAL</td>
      <td class="num" style="color:#ffd700">—</td>
      <td class="num">—</td>
      <td class="num" style="color:{tc}">{ts}₹{total_mf:,.0f}</td>
    </tr>
  </tbody>
</table>"""
        return f"""<div class="sect-hdr">Mutual Funds</div>
<div style="background:#12122a;border:1px solid #2a2a4a;border-radius:8px;padding:16px;color:#666;font-size:.85rem">
  No active MF holdings in Zerodha/AngelOne demat accounts.<br>
  <span style="font-size:.78rem">Funds held with AMCs directly can be tracked via CAMS/Karvy statement.</span>
</div>
{hist_html}"""

    total_inv = sum(h.get("inv_val", 0) for h in mf_list)
    total_cur = sum(h.get("cur_val", 0) for h in mf_list)
    total_pnl = total_cur - total_inv
    total_pct = (total_pnl / total_inv * 100) if total_inv else 0
    t_col     = "#4caf50" if total_pnl >= 0 else "#f44336"
    t_sign    = "+" if total_pnl >= 0 else ""

    rows_html = ""
    for h in sorted(mf_list, key=lambda x: -(x.get("cur_val", 0))):
        pnl   = h.get("pnl", 0)
        pnl_p = h.get("pnl_pct", 0)
        col   = "#4caf50" if pnl >= 0 else "#f44336"
        sign  = "+" if pnl >= 0 else ""
        bkr   = h.get("broker", "")
        bkr_col = "#5c9de8" if bkr == "Zerodha" else "#e8a85c"
        rows_html += f"""
        <tr>
          <td style="font-weight:bold;max-width:280px;font-size:.82rem">{h['fund']}</td>
          <td style="text-align:center;color:{bkr_col};font-size:.78rem">{bkr}</td>
          <td style="font-size:.78rem;color:#888">{h.get('folio','')}</td>
          <td class="num">{h['qty']:,.3f}</td>
          <td class="num">₹{h['avg_nav']:,.3f}</td>
          <td class="num">₹{h['nav']:,.3f}</td>
          <td class="num">₹{h['inv_val']:,.0f}</td>
          <td class="num">₹{h['cur_val']:,.0f}</td>
          <td class="num-b" style="color:{col}">{sign}₹{pnl:,.0f} ({sign}{pnl_p:.1f}%)</td>
        </tr>"""

    return f"""<div class="sect-hdr">Mutual Funds — ₹{total_inv:,.0f} deployed · Current ₹{total_cur:,.0f} 
  <span style="color:{t_col}">{t_sign}₹{total_pnl:,.0f} ({t_sign}{total_pct:.1f}%)</span></div>
<table>
  <thead><tr>
    <th>Fund Name</th><th>Broker</th><th>Folio</th><th>Units</th>
    <th>Avg NAV</th><th>Current NAV</th><th>Invested</th><th>Current Value</th><th>P&amp;L</th>
  </tr></thead>
  <tbody>
    {rows_html}
    <tr style="background:#161630;font-weight:bold">
      <td>TOTAL MF</td><td></td><td></td><td></td><td></td><td></td>
      <td class="num">₹{total_inv:,.0f}</td>
      <td class="num">₹{total_cur:,.0f}</td>
      <td class="num" style="color:{t_col}">{t_sign}₹{total_pnl:,.0f} ({t_sign}{total_pct:.1f}%)</td>
    </tr>
  </tbody>
</table>"""


def _build_sgb_section_html(sgb_list: list[dict]) -> str:
    """Build the Sovereign Gold Bond section HTML."""
    if not sgb_list:
        return """<div class="sect-hdr">Sovereign Gold Bonds (SGBs)</div>
<div style="background:#12122a;border:1px solid #2a2a4a;border-radius:8px;padding:16px;color:#666;font-size:.85rem">
  No SGB holdings found in connected accounts.
</div>"""

    total_inv = 0.0
    total_cur = 0.0
    rows_html = ""
    for h in sgb_list:
        # Normalize field names across brokers
        sym  = _strip_eq(h.get("tradingsymbol", "") or h.get("symbolname", "")).upper()
        qty  = int(h.get("quantity", h.get("z_qty", 0)) or 0)
        avg  = float(h.get("average_price", h.get("averageprice", 0)) or 0)
        ltp  = float(h.get("last_price", h.get("ltp", 0)) or 0)
        pnl  = float(h.get("pnl", h.get("profitandloss", 0)) or 0)
        bkr  = h.get("_broker", "Zerodha" if h.get("z_qty") else "AngelOne")
        inv  = avg * qty
        cur  = ltp * qty
        pnl  = cur - inv
        pnl_p = (pnl / inv * 100) if inv else 0
        col   = "#4caf50" if pnl >= 0 else "#f44336"
        sign  = "+" if pnl >= 0 else ""
        bkr_col = "#5c9de8" if bkr == "Zerodha" else "#e8a85c"
        total_inv += inv
        total_cur += cur
        rows_html += f"""
        <tr>
          <td style="font-weight:bold;color:#ffd700">{sym}</td>
          <td style="text-align:center;color:{bkr_col};font-size:.78rem">{bkr}</td>
          <td class="num">{qty}</td>
          <td class="num">₹{avg:,.2f}</td>
          <td class="num">₹{ltp:,.2f}</td>
          <td class="num">₹{inv:,.0f}</td>
          <td class="num">₹{cur:,.0f}</td>
          <td class="num-b" style="color:{col}">{sign}₹{pnl:,.0f} ({sign}{pnl_p:.1f}%)</td>
        </tr>"""

    total_pnl = total_cur - total_inv
    total_pct = (total_pnl / total_inv * 100) if total_inv else 0
    t_col  = "#4caf50" if total_pnl >= 0 else "#f44336"
    t_sign = "+" if total_pnl >= 0 else ""
    n = len(sgb_list)
    return f"""<div class="sect-hdr" style="color:#ffd700">🥇 Sovereign Gold Bonds (SGBs) — {n} series 
  <span style="color:{t_col}">{t_sign}₹{total_pnl:,.0f} ({t_sign}{total_pct:.1f}%)</span></div>
<table>
  <thead><tr>
    <th>Series</th><th>Broker</th><th>Units</th><th>Issue Price</th>
    <th>Current LTP</th><th>Invested</th><th>Current Value</th><th>P&amp;L</th>
  </tr></thead>
  <tbody>
    {rows_html}
    <tr style="background:#161630;font-weight:bold">
      <td>TOTAL SGB</td><td></td><td></td><td></td><td></td>
      <td class="num">₹{total_inv:,.0f}</td>
      <td class="num">₹{total_cur:,.0f}</td>
      <td class="num" style="color:{t_col}">{t_sign}₹{total_pnl:,.0f} ({t_sign}{total_pct:.1f}%)</td>
    </tr>
  </tbody>
</table>"""


def _is_sgb(h: dict) -> bool:
    """Return True if this holding looks like a Sovereign Gold Bond."""
    sym  = str(h.get("tradingsymbol", "") or h.get("symbolname", "")).upper()
    isin = str(h.get("isin", "")).upper()
    # Zerodha SGB tradingsymbols: SGBFEB25, SGBMAY24, etc.
    # AngelOne may use the nominal name
    return ("SGB" in sym or isin.startswith("IN009"))


def _build_fo_analysis_section() -> str:
    """Build F&O / Options loss analysis panel for the India dashboard."""
    try:
        from pathlib import Path as _Path
        import importlib.util as _ilu
        _root = _Path(__file__).parent.parent.resolve()
        spec = _ilu.spec_from_file_location(
            "india_fo_analyzer",
            _Path(__file__).parent / "india_fo_analyzer.py")
        m = _ilu.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.build_fo_analysis_html(_root / "zerodha-statement")
    except Exception as e:
        return f'<p style="color:#94a3b8;font-size:12px">F&amp;O analysis unavailable: {e}</p>'


def _build_india_cgt_section_html() -> str:
    """Build full Historical Capital Gains table from Zerodha + AngelOne statements."""
    import importlib.util as _ilu
    from pathlib import Path as _Path
    _root = _Path(__file__).parent.parent.resolve()

    rows_z, rows_ao = [], []
    try:
        spec = _ilu.spec_from_file_location(
            "zerodha_statement_reader",
            _root / "zerodha-statement" / "zerodha_statement_reader.py")
        m = _ilu.module_from_spec(spec); spec.loader.exec_module(m)
        rows_z = m.build_summary_for_dashboard(folder=_root / "zerodha-statement")
    except Exception as e:
        print(f"  CGT: Zerodha reader unavailable: {e}")

    try:
        spec = _ilu.spec_from_file_location(
            "angelone_statement_reader",
            _root / "angelone-statement" / "angelone_statement_reader.py")
        m = _ilu.module_from_spec(spec); spec.loader.exec_module(m)
        rows_ao = m.build_summary_for_dashboard(folder=_root / "angelone-statement")
    except Exception as e:
        print(f"  CGT: AngelOne reader unavailable: {e}")

    all_rows = sorted(rows_z + rows_ao, key=lambda r: (r.get("fy", ""), r.get("broker", "")))
    if not all_rows:
        return ""

    def _fmt(v: float) -> str:
        if v == 0:
            return '<span style="color:#444">—</span>'
        c = "#4caf50" if v > 0 else "#f44336"
        s = "+" if v > 0 else ""
        return f'<span style="color:{c}">{s}₹{v:,.0f}</span>'

    row_htmls = ""
    grand = {k: 0.0 for k in ("net", "st", "lt", "intra", "opts", "mf", "divs")}
    for r in all_rows:
        net   = r.get("net_inr", 0.0)
        st    = r.get("st_inr", 0.0)
        lt    = r.get("lt_inr", 0.0)
        intra = r.get("intraday_inr", 0.0)
        opts  = r.get("options_inr", 0.0)
        mf    = r.get("mf_ltcg_inr", 0.0) + r.get("mf_stcg_inr", 0.0)
        divs  = r.get("dividends_inr", 0.0)
        for k, v in zip(("net", "st", "lt", "intra", "opts", "mf", "divs"),
                        (net, st, lt, intra, opts, mf, divs)):
            grand[k] += v
        nc  = "#4caf50" if net >= 0 else "#f44336"
        bc  = "#5c9de8" if r.get("broker") == "Zerodha" else "#e8a85c"
        mst = "color:#ffd700;font-weight:bold" if mf > 0 else ""
        row_htmls += f"""
        <tr>
          <td style="color:#aaa">{r.get("fy","")}</td>
          <td style="color:{bc}">{r.get("broker","")}</td>
          <td class="num-b" style="color:{nc}">₹{net:,.0f}</td>
          <td class="num">{_fmt(st)}</td>
          <td class="num">{_fmt(lt)}</td>
          <td class="num">{_fmt(intra)}</td>
          <td class="num">{_fmt(opts)}</td>
          <td class="num" style="{mst}">{_fmt(mf)}</td>
          <td class="num">{_fmt(divs)}</td>
        </tr>"""

    gc = "#4caf50" if grand["net"] >= 0 else "#f44336"
    gs = "+" if grand["net"] >= 0 else ""
    return f"""<div class="sect-hdr">📊 Historical Capital Gains (Statement-Based)</div>
<table>
  <thead><tr>
    <th>FY</th><th>Broker</th><th>Net P&amp;L (₹)</th>
    <th>STCG</th><th>LTCG</th><th>Intraday</th><th>Options/Fut</th>
    <th style="color:#ffd700">MF LTCG</th><th>Dividends</th>
  </tr></thead>
  <tbody>
    {row_htmls}
    <tr style="background:#161630;font-weight:bold">
      <td colspan="2">GRAND TOTAL</td>
      <td class="num" style="color:{gc}">{gs}₹{grand["net"]:,.0f}</td>
      <td class="num">₹{grand["st"]:,.0f}</td>
      <td class="num">₹{grand["lt"]:,.0f}</td>
      <td class="num">₹{grand["intra"]:,.0f}</td>
      <td class="num">₹{grand["opts"]:,.0f}</td>
      <td class="num" style="color:#ffd700">₹{grand["mf"]:,.0f}</td>
      <td class="num">₹{grand["divs"]:,.0f}</td>
    </tr>
  </tbody>
</table>
<div style="color:#555;font-size:.75rem;margin-top:4px">
  Source: Zerodha taxpnl XLSX · AngelOne P&amp;L PDFs &nbsp;·&nbsp; INR values</div>"""


def generate(z_holdings, z_margins, z_profile,
             a_holdings, a_rms,
             z_authenticated: bool = True,
             z_mf: list | None = None,
             a_mf: list | None = None,
             z_sgb: list | None = None,
             a_sgb: list | None = None) -> str:

    z_mf  = z_mf  or []
    a_mf  = a_mf  or []
    z_sgb = z_sgb or []
    a_sgb = a_sgb or []

    # ── Build combined equity map (excluding SGBs) ────────────────────────────
    combined: dict[str, dict] = {}

    for h in z_holdings:
        if _is_sgb(h):
            continue  # SGBs go to their own section
        sym  = _strip_eq(h.get("tradingsymbol", "")).upper()
        qty  = int(h.get("quantity", 0) or 0)
        avg  = float(h.get("average_price", 0) or 0)
        ltp  = float(h.get("last_price", 0) or 0)
        pnl  = float(h.get("pnl", 0) or 0)
        if qty == 0:
            continue
        combined[sym] = {
            "symbol":sym, "exchange":"NSE",
            "z_qty":qty,"z_avg":avg,"z_ltp":ltp,"z_pnl":pnl,"z_val":ltp*qty,
            "a_qty":0,"a_avg":0,"a_ltp":0,"a_pnl":0,"a_val":0,
        }

    for h in a_holdings:
        if _is_sgb(h):
            continue  # SGBs go to their own section
        sym  = _strip_eq(h.get("tradingsymbol") or h.get("symbolname") or "").upper()
        qty  = int(h.get("quantity", 0) or 0)
        avg  = float(h.get("averageprice", 0) or 0)
        ltp  = float(h.get("ltp", 0) or 0)
        pnl  = float(h.get("profitandloss", 0) or 0)
        if qty == 0:
            continue
        if sym in combined:
            combined[sym].update({"a_qty":qty,"a_avg":avg,"a_ltp":ltp,"a_pnl":pnl,"a_val":ltp*qty})
        else:
            combined[sym] = {
                "symbol":sym,"exchange":"NSE",
                "z_qty":0,"z_avg":0,"z_ltp":0,"z_pnl":0,"z_val":0,
                "a_qty":qty,"a_avg":avg,"a_ltp":ltp,"a_pnl":pnl,"a_val":ltp*qty,
            }

    # Compute combined fields
    total_val   = 0.0
    total_pnl   = 0.0
    for sym, h in combined.items():
        total_qty = h["z_qty"] + h["a_qty"]
        z_cost    = h["z_avg"] * h["z_qty"]
        a_cost    = h["a_avg"] * h["a_qty"]
        comb_avg  = (z_cost + a_cost) / total_qty if total_qty else 0
        comb_ltp  = h["z_ltp"] or h["a_ltp"]
        comb_pnl  = h["z_pnl"] + h["a_pnl"]
        comb_val  = h["z_val"] + h["a_val"]
        comb_pct  = ((comb_ltp - comb_avg) / comb_avg * 100) if comb_avg else 0
        h.update({
            "total_qty":total_qty,"comb_avg":comb_avg,"comb_ltp":comb_ltp,
            "comb_pnl":comb_pnl,"comb_val":comb_val,"comb_pct":comb_pct,
        })
        total_val += comb_val
        total_pnl += comb_pnl

    # Sort by PnL %
    rows_sorted = sorted(combined.values(), key=lambda x: x["comb_pct"])

    # ── Zerodha funds ─────────────────────────────────────────────────────────
    eq = z_margins.get("equity", {}) if isinstance(z_margins, dict) else {}
    z_net  = float(eq.get("net", 0) or 0)
    z_cash = float((eq.get("available") or {}).get("cash", 0) or 0)

    # ── Zerodha missing-data card ─────────────────────────────────────────────
    zerodha_card_html = ""
    if not z_authenticated:
        zerodha_card_html = """
  <div class="card" style="border-color:#f59e0b;background:#1a1200">
    <div class="label" style="color:#f59e0b">⚠ Zerodha</div>
    <div class="value" style="color:#f59e0b;font-size:1rem">NOT AUTHENTICATED</div>
    <div class="sub-val" style="color:#f59e0b">Set ZERODHA_ACCESS_TOKEN to see live data</div>
  </div>"""
    else:
        zerodha_card_html = f"""
  <div class="card">
    <div class="label">Zerodha Cash</div>
    <div class="value" style="color:#4caf50">₹{z_cash:,.0f}</div>
    <div class="sub-val">Net: ₹{z_net:,.0f}</div>
  </div>"""

    # ── Angel One funds ───────────────────────────────────────────────────────
    a_net  = float(a_rms.get("net", 0) or 0)
    a_cash = float(a_rms.get("availablecash", 0) or 0)

    # ── AngelOne missing-data card ────────────────────────────────────────────
    angelone_card_html = ""
    if not a_holdings and not a_rms:
        angelone_card_html = """
  <div class="card" style="border-color:#f59e0b;background:#1a1200">
    <div class="label" style="color:#f59e0b">⚠ AngelOne</div>
    <div class="value" style="color:#f59e0b;font-size:1rem">DATA UNAVAILABLE</div>
    <div class="sub-val" style="color:#f59e0b">TOTP auth may have failed — check credentials</div>
  </div>"""
    else:
        angelone_card_html = f"""
  <div class="card">
    <div class="label">AngelOne Cash</div>
    <div class="value" style="color:#4caf50">₹{a_cash:,.0f}</div>
    <div class="sub-val">Net: ₹{a_net:,.0f}</div>
  </div>"""

    # ── HTML ──────────────────────────────────────────────────────────────────
    now_str = datetime.now().strftime("%d %b %Y  %H:%M")
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    z_name  = z_profile.get("user_name", "DFX693")
    a_name  = "K51989642 (AngelOne)"

    rows_html = ""
    for h in rows_sorted:
        pct  = h["comb_pct"]
        sign = "+" if pct >= 0 else ""
        pct_col = "#4caf50" if pct >= 0 else ("#f44336" if pct < -20 else "#ff9800")
        bg   = _row_color(pct)
        z_b  = f"Zerodha ({h['z_qty']})" if h["z_qty"] else ""
        a_b  = f"AngelOne ({h['a_qty']})" if h["a_qty"] else ""
        brokers = " | ".join(x for x in [z_b, a_b] if x)
        weight = (h["comb_val"] / total_val * 100) if total_val else 0
        bar_w = min(100, int(abs(pct) * 2))  # visual bar
        bar_col = "#4caf50" if pct >= 0 else "#f44336"
        rows_html += f"""
        <tr style="background:{bg}">
          <td style="font-weight:bold">{h["symbol"]}</td>
          <td>{brokers}</td>
          <td class="num">{h["total_qty"]}</td>
          <td class="num">₹{h["comb_avg"]:,.2f}</td>
          <td class="num">₹{h["comb_ltp"]:,.2f}</td>
          <td class="num">₹{h["comb_pnl"]:,.0f}</td>
          <td class="num-b" style="color:{pct_col}">{sign}{pct:.1f}%</td>
          <td class="num">₹{h["comb_val"]:,.0f}</td>
          <td class="num">{weight:.1f}%</td>
          <td><div style="height:8px;width:{bar_w}px;background:{bar_col};border-radius:4px;display:inline-block"></div></td>
        </tr>"""

    total_pct = (total_pnl / (total_val - total_pnl) * 100) if (total_val - total_pnl) else 0
    t_sign    = "+" if total_pct >= 0 else ""
    t_col     = "#4caf50" if total_pct >= 0 else "#f44336"

    # ── Best / Worst holding for KPI cards ────────────────────────────────────
    best_hold = max(rows_sorted, key=lambda x: x["comb_pct"]) if rows_sorted else None
    worst_hold = min(rows_sorted, key=lambda x: x["comb_pct"]) if rows_sorted else None
    best_card = ""
    worst_card = ""
    if best_hold:
        bp = best_hold["comb_pct"]
        best_card = f"""  <div class="card">
    <div class="label">Best Holding</div>
    <div class="value" style="color:#4caf50">{best_hold["symbol"]}</div>
    <div class="sub-val" style="color:#4caf50">+{bp:.1f}%</div>
  </div>"""
    if worst_hold:
        wp = worst_hold["comb_pct"]
        wp_col = "#f44336" if wp < 0 else "#4caf50"
        wp_sign = "+" if wp >= 0 else ""
        worst_card = f"""  <div class="card">
    <div class="label">Worst Holding</div>
    <div class="value" style="color:{wp_col}">{worst_hold["symbol"]}</div>
    <div class="sub-val" style="color:{wp_col}">{wp_sign}{wp:.1f}%</div>
  </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>India Portfolio Dashboard</title>
<style>
  body{{background:#0a0a1a;color:#e0e0e0;font-family:'Segoe UI',sans-serif;margin:0;padding:20px}}
  h1{{color:#7eb8f0;margin:0 0 4px 0;font-size:1.6rem}}
  .sub{{color:#888;font-size:.85rem;margin-bottom:20px}}
  .top-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}}
  .card{{background:#12122a;border:1px solid #2a2a4a;border-radius:8px;padding:14px}}
  .card .label{{color:#888;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}
  .card .value{{font-size:1.4rem;font-weight:bold;margin-top:4px}}
  .card .sub-val{{color:#888;font-size:.8rem;margin-top:2px}}
  table{{width:100%;border-collapse:collapse;background:#0d0d20;border-radius:8px;overflow:hidden}}
  th{{background:#161630;color:#888;font-size:.75rem;text-transform:uppercase;padding:10px 8px;text-align:left}}
  td{{padding:8px;font-size:.85rem;border-bottom:1px solid #1a1a30}}
  tr:hover td{{background:#16163a}}
  .broker-z{{color:#5c9de8}}.broker-a{{color:#e8a85c}}
  .sect-hdr{{color:#7eb8f0;margin:20px 0 8px 0;font-size:.9rem;font-weight:bold;text-transform:uppercase}}
  .risk-warn{{background:#2b0a0a;border:1px solid #f44336;border-radius:6px;padding:12px;margin-bottom:16px;color:#f44336;font-size:.85rem}}
  /* A-8: utility classes (replace 145 inline text-align:right + variants) */
  .num{{text-align:right;font-variant-numeric:tabular-nums}}
  .num-b{{text-align:right;font-weight:bold;font-variant-numeric:tabular-nums}}
  .pos{{color:#22c55e}}
  .neg{{color:#f44336}}
  .gold{{color:#ffd700}}
</style>
</head><body>
<header>
<h1>🇮🇳 India Portfolio Dashboard</h1>
<div class="sub">Live data · {now_str} · Zerodha ({z_name}) + AngelOne ({a_name})</div>
</header>
<main>

<div class="risk-warn">
  ⚠️  <strong>RISK ALERT:</strong> {sum(1 for h in rows_sorted if h["comb_pct"] < -20)} positions down &gt;20% with no active GTT stop-losses.
  Use menu option <strong>6</strong> (GTT Place Stop-Losses — Zerodha) or <strong>10</strong> (AO GTT Place — AngelOne) to auto-protect them.
</div>

<div class="top-grid">
  <div class="card">
    <div class="label">Total India Equity</div>
    <div class="value">₹{total_val:,.0f}</div>
    <div class="sub-val">{len(combined)} positions</div>
  </div>
  <div class="card">
    <div class="label">Unrealised P&amp;L</div>
    <div class="value" style="color:{t_col}">{t_sign}₹{total_pnl:,.0f}</div>
    <div class="sub-val" style="color:{t_col}">{t_sign}{total_pct:.1f}%</div>
  </div>
  {zerodha_card_html}
  {angelone_card_html}
  <div class="card">
    <div class="label">Unprotected Positions</div>
    <div class="value" style="color:#f44336">{sum(1 for h in rows_sorted if h["comb_pct"] < -20)}</div>
    <div class="sub-val">down &gt;20%, no GTT</div>
  </div>
  <div class="card">
    <div class="label">Zerodha Positions</div>
    <div class="value">{sum(1 for h in rows_sorted if h["z_qty"] > 0)}</div>
    <div class="sub-val">{"authenticated" if z_authenticated else "NOT AUTHENTICATED"}</div>
  </div>
  <div class="card">
    <div class="label">AngelOne Positions</div>
    <div class="value">{sum(1 for h in rows_sorted if h["a_qty"] > 0)}</div>
    <div class="sub-val">NSE equity (CNC)</div>
  </div>
  {best_card}
  {worst_card}
</div>

<div class="sect-hdr">Holdings — sorted by P&amp;L (worst first)</div>
<table>
  <thead><tr>
    <th>Symbol</th><th>Broker</th><th>Qty</th><th>Avg Price</th>
    <th>LTP</th><th>P&amp;L (₹)</th><th>P&amp;L %</th><th>Value</th><th>Weight</th><th>Bar</th>
  </tr></thead>
  <tbody>
    {rows_html}
    <tr style="background:#161630;font-weight:bold">
      <td>TOTAL</td><td></td><td></td><td></td><td></td>
      <td class="num" style="color:{t_col}">₹{total_pnl:,.0f}</td>
      <td class="num" style="color:{t_col}">{t_sign}{total_pct:.1f}%</td>
      <td class="num">₹{total_val:,.0f}</td>
      <td>100%</td><td></td>
    </tr>
  </tbody>
</table>

{_build_mf_section_html(z_mf + a_mf)}
{_build_sgb_section_html(z_sgb + a_sgb)}
{_build_india_cgt_section_html()}
{_build_fo_analysis_section()}

<div style="margin-top:24px;color:#444;font-size:.75rem">
  Generated by india_portfolio_dashboard.py · Zerodha Kite Connect + AngelOne SmartAPI
</div>
<script>
(function(){{
  var gen = new Date("{now_iso}");
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
</main>
{system_footer_html()}
</body></html>"""
    return html


# ── Main ──────────────────────────────────────────────────────────────────────

def _print_cli_summary(z_holdings, z_margins, z_profile, z_authenticated,
                       a_holdings, a_rms):
    """Print a concise terminal summary so the user doesn't need to open the browser."""
    INR = lambda v: f"₹{v:>10,.0f}"
    PCT = lambda v: f"{v:+.1f}%"
    SEP = "─" * 62

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'─'*62}")
    print(f"  INDIA PORTFOLIO SUMMARY  |  {ts}")
    print(SEP)

    # ── Zerodha ────────────────────────────────────────────────────────────────
    if z_authenticated and z_holdings:
        z_value  = sum(float(h.get("last_price", 0)) * float(h.get("quantity", 0))
                       for h in z_holdings)
        z_pnl    = sum(float(h.get("pnl", 0)) for h in z_holdings)
        z_cost   = sum(float(h.get("average_price", 0)) * float(h.get("quantity", 0))
                       for h in z_holdings)
        z_pct    = (z_pnl / z_cost * 100) if z_cost else 0
        z_cash   = float((z_margins.get("equity") or {}).get("net", 0))
        z_id     = z_profile.get("user_id", "")
        print(f"  Zerodha  ({z_id}):")
        print(f"    Holdings: {len(z_holdings):>3}  |  Value: {INR(z_value)}  |  PnL: {INR(z_pnl)} ({PCT(z_pct)})")
        print(f"    Cash available:    {INR(z_cash)}")
    elif not z_authenticated:
        print(f"  Zerodha  : ⚠  Token not available — holdings not loaded")

    # ── Angel One ─────────────────────────────────────────────────────────────
    if a_holdings:
        a_value  = sum(float(h.get("ltp", 0)) * float(h.get("quantity", 1))
                       for h in a_holdings)
        a_pnl    = sum(float(h.get("profitandloss", 0)) for h in a_holdings)
        a_cost   = sum(float(h.get("averageprice", h.get("avgnetprice", 0))) *
                       float(h.get("quantity", 1)) for h in a_holdings)
        a_pct    = (a_pnl / a_cost * 100) if a_cost else 0
        a_cash   = float(a_rms.get("net", 0)) if isinstance(a_rms, dict) else 0
        print(f"  AngelOne (K51989):")
        print(f"    Holdings: {len(a_holdings):>3}  |  Value: {INR(a_value)}  |  PnL: {INR(a_pnl)} ({PCT(a_pct)})")
        print(f"    Cash available:    {INR(a_cash)}")

    # ── Combined ───────────────────────────────────────────────────────────────
    z_val2 = (sum(float(h.get("last_price", 0)) * float(h.get("quantity", 0))
                  for h in z_holdings) if z_authenticated and z_holdings else 0)
    z_pnl2 = (sum(float(h.get("pnl", 0)) for h in z_holdings)
               if z_authenticated and z_holdings else 0)
    a_val2 = (sum(float(h.get("ltp", 0)) * float(h.get("quantity", 1))
                  for h in a_holdings) if a_holdings else 0)
    a_pnl2 = (sum(float(h.get("profitandloss", 0)) for h in a_holdings)
               if a_holdings else 0)
    total_val  = z_val2 + a_val2
    total_pnl  = z_pnl2 + a_pnl2
    total_n    = (len(z_holdings) if z_authenticated else 0) + len(a_holdings)
    total_cost = total_val - total_pnl
    total_pct  = (total_pnl / total_cost * 100) if total_cost else 0

    print(SEP)
    print(f"  COMBINED:  Holdings: {total_n:>3}  |  Value: {INR(total_val)}  |  PnL: {INR(total_pnl)} ({PCT(total_pct)})")
    print(SEP)
    print(f"  ⚠  Tip: Run GTT audit (Zerodha option 4 / AngelOne option 8) to check for")
    print(f"          unprotected positions with no stop-loss orders.")


if __name__ == "__main__":
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  pass  # cleanup

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--morning", action="store_true",
                        help="Non-interactive morning mode: no-open with all defaults. Zero prompts.")
    args  = parser.parse_args()
    # --morning overrides
    if args.morning:
        args.no_open = True

    # ── Zerodha token resolution — delegate entirely to zerodha_auth ─────────
    # zerodha_auth.get_kite() is the single source of truth:
    #   1. Checks today's cache file  (date-gated, authoritative)
    #   2. Falls back to ZERODHA_ACCESS_TOKEN env var (subprocess inheritance)
    #   3. Auto-triggers login if neither is available
    # Calling run_login() / cache reads here would create a DUPLICATE AUTH PATH
    # that can invalidate an existing session (Zerodha allows only one active
    # session per API key).
    sys.path.insert(0, os.path.dirname(__file__))
    from zerodha_auth import get_kite as _get_kite, invalidate_token as _invalidate_token

    z_authenticated = False
    print("Fetching Zerodha data...", end=" ", flush=True)
    try:
        _get_kite()          # warm up singleton — triggers login if needed
        z_authenticated = True
        z_holdings, z_margins, z_profile = _load_zerodha()
        print(f"✓ {len(z_holdings)} holdings")
    except EnvironmentError:
        # No token and login was skipped/failed — Angel One only mode
        print("⚠ no access token — Zerodha data missing")
        z_holdings, z_margins, z_profile = [], {}, {}
    except PermissionError:
        # Token expired during API call (TokenException) — invalidate; do NOT open
        # browser from this subprocess.  If a fresh token is available in the env
        # (placed there by main.py's _india_env), _get_kite(silent=True) will find
        # it and succeed.  Otherwise it raises EnvironmentError and we degrade
        # gracefully to "no Zerodha data" without triggering a browser login.
        _invalidate_token()
        print("\n⚠  Zerodha token expired. Re-running main.py will prompt re-auth.")
        try:
            _get_kite(silent=True)   # no browser — silent retry only
            z_authenticated = True
            z_holdings, z_margins, z_profile = _load_zerodha()
            print(f"✓ {len(z_holdings)} holdings")
        except Exception as _exc:
            print(f"  Token unavailable: {_exc}")
            z_holdings, z_margins, z_profile = [], {}, {}
            z_authenticated = False
    except Exception as _api_exc:
        # Transient API error (GeneralException, NetworkException etc.)
        print(f"\n⚠  Zerodha API error: {_api_exc}")
        z_holdings, z_margins, z_profile = [], {}, {}
        z_authenticated = False

    print("Fetching AngelOne data...", end=" ", flush=True)
    a_holdings, a_rms, a_mf = _load_angelone()
    print(f"✓ {len(a_holdings)} holdings, {len(a_mf)} MF funds")

    # ── Mutual funds (Zerodha) ─────────────────────────────────────────────────
    z_mf: list = []
    if z_authenticated:
        try:
            # Reuse the zerodha_auth singleton — never create a second KiteConnect
            kite_mf = _get_kite()
            z_mf = _fetch_zerodha_mf(kite_mf)
            if z_mf:
                print(f"  Zerodha MF: {len(z_mf)} fund(s)")
        except Exception as _e:
            print(f"  Zerodha MF fetch skipped: {_e}")

    # ── SGBs (filter from equity holdings) ────────────────────────────────────
    z_sgb = [dict(h, _broker="Zerodha")  for h in z_holdings if _is_sgb(h)]
    a_sgb = [dict(h, _broker="AngelOne") for h in a_holdings if _is_sgb(h)]
    if z_sgb or a_sgb:
        print(f"  SGBs: {len(z_sgb)} Zerodha, {len(a_sgb)} AngelOne")

    html = generate(z_holdings, z_margins, z_profile, a_holdings, a_rms,
                    z_authenticated=z_authenticated,
                    z_mf=z_mf, a_mf=a_mf,
                    z_sgb=z_sgb, a_sgb=a_sgb)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Dashboard saved: {OUTPUT}")
    _print_cli_summary(z_holdings, z_margins, z_profile, z_authenticated,
                       a_holdings, a_rms)

    # ── Persist snapshot to report analytics DB ───────────────────────────────
    try:
        from report_db import save_india_snapshot, archive_report as _arc
        save_india_snapshot(
            z_holdings if z_authenticated else [],
            z_margins  if z_authenticated else {},
            a_holdings,
            a_rms,
            zerodha_ok=z_authenticated,
        )
        _arc(Path(OUTPUT))
    except Exception as _rdb_exc:
        print(f"  [report_db] snapshot not saved: {_rdb_exc}")
    # Browser auto-open removed — reports are accessed via file links in terminal output
