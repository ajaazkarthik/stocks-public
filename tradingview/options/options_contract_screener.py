"""
Options Contract Screener — Scan US Quality Stocks for Top CALL Options
=======================================================================
Scans up to 300 US quality stocks from the pre-built watchlist and returns
the top CALL option contracts by TQS v2 (Trade Quality Score).

Data sources:
  1. TradingView scan2 REST  → IV, Greeks, bid_iv/ask_iv, theoPrice (per symbol, all contracts)
  2. Moomoo OpenD            → Real open interest + last-session bid/ask (fills TV zeros on weekends)
  3. yfinance                → Last-session volume when market closed (weekend fallback)
  4. TradingView WebSocket   → Live OHLC, bid_size/ask_size (during market hours only)

Quality gate (weekend / after-hours safe):
  • mid_price ≥ $0.05     — no penny options (uses Moomoo last-session bid/ask on weekends)
  • spread_pct ≤ 20 %     — no extremely wide spreads
  • OI ≥ 100              — only applied when Moomoo is online AND market is open
                             (OI settles at market close; Moomoo returns 0 on weekends)

Output: reports/options_scan_us_300.html

Usage:
    python options_contract_screener.py
    python options_contract_screener.py --n 50 --verbose
    python options_contract_screener.py --refresh-universe --expiries 2
    python options_contract_screener.py --out reports/my_scan.html
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE        = Path(__file__).parent.resolve()   # tradingview/options/
_TV_DIR      = _HERE.parent.resolve()            # tradingview/
_ROOT_DIR    = _TV_DIR.parent.resolve()          # project root
_DEFAULT_OUT = _ROOT_DIR / "reports" / "options_scan_us_300.html"
_WATCHLIST   = _TV_DIR / "watchlists" / "us_300_watchlist.json"

# Ensure package context: tradingview.options.* imports must resolve correctly
# (same pattern as run_options.py and tv_options_screener.py)
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))


def _is_us_market_hours() -> bool:
    """True if US regular session: Mon–Fri 09:30–16:00 ET (UTC-4 in summer)."""
    now = _dt.datetime.now(_dt.timezone.utc)
    if now.weekday() >= 5:          # Sat/Sun
        return False
    h = now.hour + now.minute / 60.0
    return 13.5 <= h <= 20.0       # 09:30–16:00 ET in UTC


# ── Quality gate constants ────────────────────────────────────────────────────

_MIN_PREMIUM   = 0.05    # minimum mid-price (filter out sub-penny options)
_MAX_SPREAD    = 20.0    # maximum bid-ask spread % (filter illiquid contracts)
_MIN_OI_MOOMOO = 100     # minimum OI when Moomoo is online AND market hours
_MIN_DTE       = 1       # exclude same-day expiry (0DTE) options from screener
_MAX_OTM_PCT   = 30.0    # reject contracts >30% OTM (e.g. ADI C428 at +85% OTM on ~$231 spot)


# ── IV Rank computation (cached per underlying symbol) ────────────────────────

_IV_RANK_CACHE: dict[str, float | None] = {}


def _compute_iv_rank(symbol: str, current_iv_pct: float) -> float | None:
    """IV Rank: where current IV sits in 252-day realized vol distribution (0-100)."""
    if symbol in _IV_RANK_CACHE:
        return _IV_RANK_CACHE[symbol]
    if current_iv_pct <= 0:
        _IV_RANK_CACHE[symbol] = None
        return None
    try:
        import yfinance as yf
        import math as _m
        hist = yf.Ticker(symbol).history(period="282d")
        if hist is None or len(hist) < 30:
            _IV_RANK_CACHE[symbol] = None
            return None
        closes = hist["Close"]
        log_ret = (closes / closes.shift(1)).apply(
            lambda x: _m.log(x) if x and x > 0 else 0.0
        )
        rv_21 = log_ret.rolling(21).std() * (252 ** 0.5) * 100  # annualized %
        rv_21 = rv_21.dropna()
        if len(rv_21) < 10:
            _IV_RANK_CACHE[symbol] = None
            return None
        rank = float((rv_21 < current_iv_pct).sum()) / len(rv_21) * 100
        _IV_RANK_CACHE[symbol] = round(rank, 1)
        return _IV_RANK_CACHE[symbol]
    except Exception:
        _IV_RANK_CACHE[symbol] = None
        return None


# ── Lazy imports (avoid top-level import cost at main.py import time) ─────────

def _get_fetcher_fns():
    """Return (HybridOptionsFetcher, compute_tqs_v2, compute_wrs, inject_screener_subscores, compute_premium_win) from hybrid_options."""
    from tradingview.options.hybrid_options import (
        HybridOptionsFetcher, compute_tqs_v2, compute_wrs, inject_screener_subscores, compute_premium_win,
    )
    return HybridOptionsFetcher, compute_tqs_v2, compute_wrs, inject_screener_subscores, compute_premium_win


def _get_universe_fns():
    """Return (load_watchlist, build_watchlist) from us_stock_universe."""
    from tradingview.options.us_stock_universe import load_watchlist, build_watchlist
    return load_watchlist, build_watchlist


# ── Quality gate ──────────────────────────────────────────────────────────────

def passes_quality_gate(contract: dict) -> bool:
    """
    Return True if the contract meets minimum quality thresholds.

    • mid_price gate always applies (uses Moomoo last-session bid/ask on weekends).
    • OI gate only applies when Moomoo online AND market is currently open.
      OI settles at market close — Moomoo returns 0 on weekends even when reachable.
    • DTE gate: exclude same-day expiry (0DTE) options — they expire before market open.
    """
    mid_price  = contract.get("mid_price") or 0.0
    _raw_spread = contract.get("spread_pct")
    spread_pct = _raw_spread if _raw_spread is not None else 999.0
    oi         = contract.get("oi") or 0
    oi_source  = contract.get("oi_source", "")
    dte        = contract.get("dte")  # None when not provided — gate skipped
    iv         = contract.get("iv")   # None when not provided — gate skipped

    # DTE gate: only apply when dte is known; missing dte means we don't reject
    if dte is not None and dte < _MIN_DTE:
        return False
    if mid_price < _MIN_PREMIUM:
        return False
    if spread_pct > _MAX_SPREAD:
        return False
    # IV gate: reject contracts with explicitly zero/negative IV (no valid pricing)
    if iv is not None and iv <= 0:
        return False
    # OI gate: REMOVED — OI is unreliable (TV scan2 hardcodes 0, Moomoo only
    # covers near-ATM, yfinance is delayed).  OI kept for display only.
    # Moneyness gate: reject contracts >30% OTM — deep OTM has near-zero delta and no real market
    # This also catches TV-fail + spot_price=0 bypass artefacts (e.g. ADI C428 at +85% OTM)
    moneyness = contract.get("moneyness_pct")
    if moneyness is not None and moneyness > _MAX_OTM_PCT:
        return False
    return True


# ── Per-symbol scan ───────────────────────────────────────────────────────────

def scan_symbol(
    symbol: str,
    fetcher,
    compute_tqs_v2_fn,
    compute_wrs_fn,
    inject_subscores_fn=None,
    compute_premium_win_fn=None,
    n_expiries: int = 3,
    verbose: bool = False,
    option_type: str = "CALL",
    max_dte: int = 28,
    min_premium: float = 0.1,
    max_premium: float = 6.0,
) -> list[dict]:
    """
    Fetch and score option contracts for *symbol*.

    Returns a list of contracts that passed the quality gate, sorted by
    TQS v2 descending.  Returns [] on any error.
    """
    try:
        if option_type.upper() == "PUT":
            contracts = fetcher.get_puts(symbol, n_expiries=n_expiries, max_dte=max_dte)
        else:
            contracts = fetcher.get_calls(symbol, n_expiries=n_expiries, max_dte=max_dte)
    except Exception as exc:
        if verbose:
            print(f"  [SKIP] {symbol}: fetch error — {exc}")
        return []

    if not contracts:
        return []

    scored: list[dict] = []
    for c in contracts:
        if not passes_quality_gate(c):
            continue
        # CLI premium range filter
        _mid = c.get("mid_price") or 0.0
        if min_premium > 0 and _mid < min_premium:
            continue
        if max_premium > 0 and _mid > max_premium:
            continue

        # Re-compute TQS v2 from the full hybrid dict (HybridOptionsFetcher
        # Inject mode-neutral sub-scores (IV quality, delta quality, DTE quality)
        # so compute_tqs_v2 uses its PRIMARY path instead of liquidity-only fallback.
        if inject_subscores_fn is not None:
            inject_subscores_fn(c)
        tqs = compute_tqs_v2_fn(
            c,
            iv_spread=c.get("iv_spread"),
            ohlc_range_pct=c.get("ohlc_range_pct"),
            pressure=c.get("pressure"),
        )
        wrs = compute_wrs_fn(c)

        pw = compute_premium_win_fn(c) if compute_premium_win_fn else 0.0
        c["tqs_v2"]       = tqs
        c["wrs"]          = wrs
        c["premium_win"]  = round(pw, 1)
        c["symbol"]       = symbol
        scored.append(c)

    scored.sort(key=lambda x: x.get("tqs_v2", 0.0), reverse=True)
    return scored


# ── HTML report ───────────────────────────────────────────────────────────────

def _score_color(score: float) -> str:
    if score >= 75:
        return "#3fb950"   # green
    if score >= 60:
        return "#39d353"   # teal
    if score >= 45:
        return "#d29922"   # yellow
    if score >= 30:
        return "#f0883e"   # orange
    return "#f85149"       # red


def _bar(score: float, max_score: float = 100.0) -> str:
    pct   = max(0.0, min(100.0, score / max_score * 100.0))
    color = _score_color(score)
    return (
        f'<div style="background:#1c2335;border-radius:3px;height:6px;width:100%">'
        f'<div style="background:{color};border-radius:3px;height:6px;width:{pct:.0f}%"></div>'
        f'</div>'
    )


def _source_badge(label: str, bg: str, fg: str, border: str) -> str:
    return (
        f'<span style="font-size:.65rem;background:{bg};color:{fg};'
        f'border:1px solid {border};border-radius:3px;padding:.1rem .3rem">'
        f'{label}</span>'
    )


def _contract_card_html(rank: int, rec: dict) -> str:
    symbol    = rec.get("symbol", "")
    ticker    = symbol.split(":")[-1] if ":" in symbol else symbol
    strike    = rec.get("strike", 0)
    expiry    = rec.get("expiry", "")
    dte       = rec.get("dte", 0)
    premium   = rec.get("mid_price") or rec.get("theo_price") or 0.0
    tqs       = rec.get("tqs_v2", 0.0)
    wrs       = rec.get("wrs", 0.0)
    oi        = rec.get("oi", 0) or 0
    volume    = rec.get("volume", 0) or 0
    iv        = rec.get("iv", 0.0) or 0.0
    delta     = rec.get("delta", 0.0) or 0.0
    spread    = rec.get("spread_pct", 0.0) or 0.0
    spot      = rec.get("spot_price", 0.0) or 0.0
    moneyness = rec.get("moneyness_pct", 0.0) or 0.0
    tqs_badge   = rec.get("tqs_v2_badge", "")
    oi_src      = rec.get("oi_source", "")
    ws_src      = rec.get("ws_source", "")
    premium_win = rec.get("premium_win", 0.0) or 0.0

    border_color = "#3fb950" if rank == 1 else "#58a6ff" if rank == 2 else "#30363d"
    rank_bg      = "#1c4f2a" if rank == 1 else "#0d2249" if rank == 2 else "#2b3543"
    rank_fg      = "#3fb950" if rank == 1 else "#58a6ff" if rank == 2 else "#8b949e"

    moneyness_label = "ITM" if moneyness < -3 else "OTM" if moneyness > 3 else "ATM"
    moneyness_color = "#3fb950" if moneyness < -3 else "#f0883e" if moneyness > 3 else "#58a6ff"

    tqs_color = _score_color(tqs)
    wrs_color = _score_color(wrs)
    pw_color  = _score_color(premium_win)

    # ── Mechanics row (Task 6 improvements) ─────────────────────────────
    import math as _math
    be_pct   = ((strike + premium) / spot - 1.0) * 100.0 if spot > 0 else 0.0
    sigma_T  = (iv / 100.0) * _math.sqrt(max(dte, 1) / 365.0) * 100.0 if iv > 0 else 0.0  # 1σ move %
    be_sigma_ratio = be_pct / sigma_T if sigma_T > 0 else float('inf')
    leverage = (abs(delta) * spot / premium) if premium > 0 else 0.0
    vol_oi   = (volume / oi) if (oi > 0 and volume > 0) else 0.0
    vega     = rec.get("vega", 0) or 0
    iv_crush_pct = (vega * 10.0 / premium * 100.0) if premium > 0 and vega > 0 else 0.0
    theta_raw = rec.get("theta", 0) or 0
    theta_pct = (abs(theta_raw) / premium * 100.0) if premium > 0 and theta_raw else 0.0
    # Vega risk label
    if   iv_crush_pct > 35.0: vega_risk_label, vega_risk_color = "⚠️ HIGH IV-risk", "#f85149"
    elif iv_crush_pct > 22.0: vega_risk_label, vega_risk_color = "MED IV-risk", "#d29922"
    elif iv_crush_pct > 12.0: vega_risk_label, vega_risk_color = "LOW IV-risk", "#8b949e"
    else:                     vega_risk_label, vega_risk_color = "IV-safe", "#3fb950"
    # Vol/OI label  
    if   vol_oi > 1.5: vol_oi_color = "#3fb950"   # strong fresh interest
    elif vol_oi > 0.5: vol_oi_color = "#d29922"
    else:              vol_oi_color = "#8b949e"
    be_achievable = (sigma_T > 0 and be_pct <= sigma_T)  # break-even within 1σ
    be_color      = "#3fb950" if be_achievable else "#f0883e"
    # BE/1σ ratio color coding
    if   be_sigma_ratio <= 0.40: be_sig_color = "#3fb950"  # cheap (edge)
    elif be_sigma_ratio <= 0.55: be_sig_color = "#84cc16"  # fair
    elif be_sigma_ratio <= 0.70: be_sig_color = "#d29922"  # expensive
    else:                        be_sig_color = "#f85149"  # avoid
    be_sig_label = f"BE/1σ {be_sigma_ratio:.2f}" if be_sigma_ratio < 100 else "BE/1σ ∞"

    # Source badges
    badges = ""
    if oi_src == "moomoo":
        badges += _source_badge("OI:Moomoo", "#0d2049", "#58a6ff", "#1f6feb") + " "
    elif oi_src == "unavailable":
        badges += _source_badge("OI:Offline", "#2b2200", "#d29922", "#9e6a03") + " "
    if ws_src == "live":
        badges += _source_badge("WS:Live", "#1c3d1c", "#3fb950", "#2ea043")
    elif ws_src == "last-session":
        # WS connected and returned data but market is closed — shows after-hours/last-session values
        badges += _source_badge("WS:last-session", "#1c2d1c", "#3fb950", "#2ea043")
    elif ws_src == "unavailable":
        badges += _source_badge("WS:offline", "#2b1c1c", "#f85149", "#da3633")
    elif ws_src:
        badges += _source_badge(f"WS:{ws_src}", "#1c1c1c", "#8b949e", "#30363d")

    tqs_badge_html = (
        f'<div style="font-size:.68rem;color:#8b949e;margin-top:.25rem">{tqs_badge}</div>'
        if tqs_badge else ""
    )

    stale_warn = rec.get("weekend_stale_warn") or ""
    stale_banner = (
        f'<div style="background:#3b1c00;border:1px solid #9e6a03;border-radius:4px;'
        f'padding:.25rem .5rem;font-size:.73rem;font-weight:600;color:#d29922;margin:.3rem 0">'
        f'{stale_warn}</div>'
    ) if stale_warn else ""

    return f"""
    <div class="contract-card" data-symbol="{ticker}"
         data-tqs="{tqs}" data-wrs="{wrs}" data-pw="{premium_win}" data-dte="{dte}"
         style="margin-bottom:.75rem;background:#161b22;border:1px solid {border_color};
                border-radius:8px;padding:.75rem 1rem">
      <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem">
        <div style="width:1.6rem;height:1.6rem;display:flex;align-items:center;
                    justify-content:center;border-radius:50%;font-size:.8rem;font-weight:700;
                    background:{rank_bg};color:{rank_fg};flex-shrink:0">{rank}</div>
        <span style="font-size:1.1rem;font-weight:700;color:#58a6ff">{ticker}</span>
        <span style="font-size:.9rem;font-weight:600;color:#e6edf3">C{strike:.0f}</span>
        <span style="font-size:.75rem;color:#8b949e">{expiry} ({dte}d)</span>
        <span style="margin-left:auto;font-size:1.1rem;font-weight:700;color:#f0883e">${premium:.2f}</span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:.35rem;margin:.3rem 0">
        <span style="font-size:.7rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.12rem .35rem;color:{moneyness_color}">{moneyness_label} {moneyness:+.1f}%</span>
        <span style="font-size:.7rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.12rem .35rem;color:#8b949e">Spot ${spot:.2f}</span>
        <span style="font-size:.7rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.12rem .35rem;color:#8b949e">IV {iv:.1f}%</span>
        <span style="font-size:.7rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.12rem .35rem;color:#8b949e">&#916; {delta:.2f}</span>
        <span style="font-size:.7rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.12rem .35rem;color:#8b949e">Spread {spread:.1f}%</span>
        <span style="font-size:.7rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.12rem .35rem;color:#8b949e">OI {oi:,}</span>
        <span style="font-size:.7rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.12rem .35rem;color:#8b949e">Vol {volume:,}</span>
        {badges}
      </div>
      <div style="display:flex;gap:1rem;margin:.4rem 0;align-items:center">
        <div style="flex:1">
          <div style="font-size:.68rem;color:#8b949e;margin-bottom:.2rem">TQS v2 (Contract Quality)</div>
          {_bar(tqs)}
          <div style="font-size:.85rem;font-weight:700;color:{tqs_color};margin-top:.2rem">{tqs:.1f}</div>
        </div>
        <div style="flex:1">
          <div style="font-size:.68rem;color:#8b949e;margin-bottom:.2rem">Win Rate Score (hold to expiry)</div>
          {_bar(wrs)}
          <div style="font-size:.85rem;font-weight:700;color:{wrs_color};margin-top:.2rem">{wrs:.1f}%</div>
        </div>
        <div style="flex:1">
          <div style="font-size:.68rem;color:#f0883e;margin-bottom:.2rem">&#128178; Premium Win (50% gain before expiry)</div>
          {_bar(premium_win)}
          <div style="font-size:.85rem;font-weight:700;color:{pw_color};margin-top:.2rem">{premium_win:.1f}</div>
        </div>
      </div>
      {tqs_badge_html}
      {stale_banner}
      <div style="display:flex;flex-wrap:wrap;gap:.3rem;margin:.3rem 0 .05rem;
                  padding-top:.3rem;border-top:1px solid #21262d">
        <span title="Move needed to break even at expiry vs 1-sigma move" style="font-size:.68rem;
              background:#1a2536;border:1px solid #263652;border-radius:4px;padding:.1rem .3rem;color:{be_color}">
          ⚡ BE +{be_pct:.1f}% | 1σ ±{sigma_T:.1f}%</span>
        <span title="Delta-leverage: (|delta|×spot)/premium — how many dollars of stock exposure per $1 spent"
              style="font-size:.68rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.1rem .3rem;color:#8b949e">Lev {leverage:.1f}×</span>
        <span title="% option value lost if IV drops 10pp (vega×10/mid)"
              style="font-size:.68rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.1rem .3rem;color:{vega_risk_color}">{vega_risk_label}</span>
        <span title="Volume/OI ratio — fresh buying interest vs standing positions"
              style="font-size:.68rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.1rem .3rem;color:{vol_oi_color}"
              >Vol/OI {vol_oi:.2f}×</span>
        <span title="Theta decay: % of premium lost per calendar day"
              style="font-size:.68rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.1rem .3rem;color:#8b949e">θ -{theta_pct:.1f}%/day</span>
        <span title="Break-even as fraction of 1-sigma move (lower=cheaper). ≤0.40 cheap, 0.40-0.55 fair, 0.55-0.70 expensive, >0.70 avoid"
              style="font-size:.68rem;background:#1a2536;border:1px solid #263652;border-radius:4px;
                     padding:.1rem .3rem;color:{be_sig_color}">{be_sig_label}</span>
        {"" if rec.get("iv_rank") is None else (
            f'<span title="IV Rank: where current IV sits in 252-day realized vol distribution (0=cheap, 100=expensive)"'
            f' style="font-size:.68rem;background:#1a2536;border:1px solid #263652;border-radius:4px;'
            f' padding:.1rem .3rem;color:{"#3fb950" if rec.get("iv_rank", 50) < 30 else "#f0883e" if rec.get("iv_rank", 50) > 70 else "#8b949e"}">'
            f'IVR {rec.get("iv_rank"):.0f}</span>'
        )}
      </div>
    </div>"""


def build_html(
    recommendations: list[dict],
    scanned_count: int,
    found_count: int,
    generated_at: str,
) -> str:
    """Build self-contained dark-theme HTML report for the US 300 options scan."""
    top100 = recommendations[:100]

    if top100:
        cards_html = "".join(_contract_card_html(i + 1, r) for i, r in enumerate(top100))
    else:
        cards_html = (
            '<div style="color:#8b949e;padding:2rem;text-align:center">'
            "No contracts passed the quality gate. "
            "Try running during US market hours (09:30–16:00 ET) with Moomoo OpenD active."
            "</div>"
        )

    vis_count = len(top100)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>US Options Scan — Top 300 Quality Stocks</title>
<style>
:root {{
  --bg:#0e1117; --card:#161b22; --border:#30363d;
  --text:#e6edf3; --dim:#8b949e; --accent:#58a6ff;
  --green:#3fb950; --red:#f85149; --yellow:#d29922; --orange:#f0883e;
}}
* {{ box-sizing:border-box }}
body {{
  background:var(--bg); color:var(--text);
  font-family:'Segoe UI',system-ui,sans-serif;
  margin:0; padding:1rem 1.5rem;
}}
h1,h2,h3 {{ margin:0 }}
.hdr {{
  display:flex; align-items:center; gap:1rem;
  margin-bottom:1rem; padding-bottom:.75rem;
  border-bottom:1px solid var(--border);
}}
.stat-pill {{
  font-size:.78rem; background:#1a2536;
  border:1px solid #263652; border-radius:6px;
  padding:.3rem .65rem; color:var(--dim);
}}
.stat-pill b {{ color:var(--text) }}
.section-hdr {{
  font-size:.7rem; text-transform:uppercase;
  letter-spacing:.1em; color:var(--dim);
  padding:.3rem 0 .2rem;
  border-bottom:1px solid var(--border);
  margin:1rem 0 .5rem;
}}
.ctrl-bar {{
  display:flex; align-items:center; gap:.75rem;
  margin-bottom:1rem; background:#161b22;
  border:1px solid #30363d; border-radius:8px;
  padding:.5rem .75rem;
}}
input[type=text] {{
  background:#0d1117; border:1px solid var(--border);
  border-radius:6px; padding:.3rem .65rem;
  font-size:.8rem; color:var(--text); outline:none; width:200px;
}}
input[type=text]:focus {{ border-color:var(--accent) }}
</style>
<script>
function applyFilters() {{
  const q     = (document.getElementById('f-sym').value || '').toLowerCase();
  const tqsLo = parseFloat(document.getElementById('f-tqs-lo').value) || 0;
  const tqsHi = parseFloat(document.getElementById('f-tqs-hi').value) || 100;
  const wrsLo = parseFloat(document.getElementById('f-wrs-lo').value) || 0;
  const dteLo = parseFloat(document.getElementById('f-dte-lo').value) || 0;
  const dteHi = parseFloat(document.getElementById('f-dte-hi').value) || 999;
  let vis = 0;
  document.querySelectorAll('.contract-card').forEach(el => {{
    const sym = (el.dataset.symbol || '').toLowerCase();
    const tqs = parseFloat(el.dataset.tqs)  || 0;
    const wrs = parseFloat(el.dataset.wrs)  || 0;
    const dte = parseFloat(el.dataset.dte)  || 0;
    const show = (!q || sym.includes(q))
              && tqs >= tqsLo && tqs <= tqsHi
              && wrs >= wrsLo
              && dte >= dteLo && dte <= dteHi;
    el.style.display = show ? '' : 'none';
    if (show) vis++;
  }});
  document.getElementById('vis-count').textContent = vis + ' contracts';
}}
function filterSymbol(val) {{ applyFilters(); }}
function sortCards(key) {{
  const list = document.getElementById('contracts-list');
  const cards = Array.from(list.querySelectorAll('.contract-card'));
  cards.sort((a, b) => (parseFloat(b.dataset[key]) || 0) - (parseFloat(a.dataset[key]) || 0));
  cards.forEach(c => list.appendChild(c));
}}
</script>
</head>
<body>
<div class="hdr">
  <div>
    <h1 style="font-size:1.4rem;font-weight:700;color:var(--accent)">
      US Options Scan — Quality 300
    </h1>
    <div style="font-size:.8rem;color:var(--dim);margin-top:.25rem">Generated {generated_at}</div>
  </div>
  <span class="stat-pill">Scanned <b>{scanned_count}</b> stocks</span>
  <span class="stat-pill">Found <b>{found_count}</b> stocks with options</span>
  <span class="stat-pill">Top <b>{vis_count}</b> contracts (≤3/stock)</span>
</div>

<div class="ctrl-bar">
  <span style="font-size:.8rem;color:#8b949e">Search:</span>
  <input id="f-sym" type="text" placeholder="Filter by ticker..." oninput="applyFilters()">
  <span style="font-size:.75rem;color:#8b949e">TQS:</span>
  <input id="f-tqs-lo" type="text" placeholder="min" style="width:50px" oninput="applyFilters()" value="">
  <span style="font-size:.6rem;color:#555">–</span>
  <input id="f-tqs-hi" type="text" placeholder="max" style="width:50px" oninput="applyFilters()" value="">
  <span style="font-size:.75rem;color:#8b949e">WRS≥:</span>
  <input id="f-wrs-lo" type="text" placeholder="min" style="width:50px" oninput="applyFilters()" value="">
  <span style="font-size:.75rem;color:#8b949e">DTE:</span>
  <input id="f-dte-lo" type="text" placeholder="min" style="width:40px" oninput="applyFilters()" value="">
  <span style="font-size:.6rem;color:#555">–</span>
  <input id="f-dte-hi" type="text" placeholder="max" style="width:40px" oninput="applyFilters()" value="">
  <span style="font-size:.75rem;color:#8b949e;margin-left:.5rem">Sort:</span>
  <button onclick="sortCards('tqs')" style="background:#1a2536;border:1px solid #263652;color:var(--accent);border-radius:4px;padding:.2rem .5rem;font-size:.7rem;cursor:pointer">TQS</button>
  <button onclick="sortCards('wrs')" style="background:#1a2536;border:1px solid #263652;color:var(--green);border-radius:4px;padding:.2rem .5rem;font-size:.7rem;cursor:pointer">WRS</button>
  <button onclick="sortCards('pw')" style="background:#1a2536;border:1px solid #263652;color:var(--orange);border-radius:4px;padding:.2rem .5rem;font-size:.7rem;cursor:pointer">PW</button>
  <button onclick="sortCards('dte')" style="background:#1a2536;border:1px solid #263652;color:var(--yellow);border-radius:4px;padding:.2rem .5rem;font-size:.7rem;cursor:pointer">DTE</button>
  <span id="vis-count" style="font-size:.75rem;color:#8b949e;margin-left:auto">
    {vis_count} contracts
  </span>
</div>

<div class="section-hdr">
  Top CALL Opportunities — Sorted by TQS v2 (Contract Quality Score)
</div>

<details style="margin:0 0 1rem;background:#161b22;border:1px solid #263652;border-radius:8px;padding:.75rem 1rem">
  <summary style="cursor:pointer;font-size:.85rem;font-weight:600;color:#58a6ff">&#128209; Scoring Methodology — How We Predict Win Rates</summary>
  <div style="font-size:.75rem;color:#8b949e;line-height:1.6;margin-top:.5rem">
    <b>TQS v2 (Trade Quality Score, 0-100)</b> — Multi-factor composite:
    IV rank (IV vs 30d average), bid-ask tightness (spread/mid), OI weight (open interest depth),
    delta sweet-spot (0.25-0.45 ideal), DTE window (14-45d optimal), volume/OI ratio (fresh interest),
    moneyness (2-8% OTM ideal), Greeks quality (gamma-theta ratio), market-hours data freshness badge.
    <br><br>
    <b>WRS (Win Rate Score, 0-100)</b> — P(S<sub>T</sub> &gt; K + premium) at expiry.
    Uses lognormal model: z = (ln(breakeven/spot)) / (σ√T), where σ = IV/100 and T = DTE/365 (calendar days).
    Adjusts for: IV/RV ratio, signal quality (±12pt), delta-based ITM probability, DTE scaling.
    <br><br>
    <b>Premium Win (50% Gain Before Expiry)</b> — P(max option value ≥ 1.5× entry at any time before T).
    Uses reflection principle: P(max S<sub>t</sub> ≥ H) = erfc(ln(H/S) / (σ√T × √2)).
    Target spot derived from delta+gamma quadratic: Δx where ΔV = delta×x + 0.5×gamma×x² = 0.5×mid.
    Adjusts for: signal momentum, theta decay rate, gamma leverage, WebSocket bid/ask pressure.
    Should be ≥ WRS by reflection principle (any-time ≥ terminal probability for equal/easier barrier).
  </div>
</details>

<div id="contracts-list">
  {cards_html}
</div>
</body>
</html>"""


# ── Main scan logic ───────────────────────────────────────────────────────────

def _build_cookie_required_html(generated_at: str) -> str:
    """Generate an actionable HTML report when the TV options API is unavailable."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>US Options Scan — Cookie Required</title>
<style>
body{{background:#0e1117;color:#e6edf3;font-family:'Segoe UI',system-ui,sans-serif;
      margin:0;padding:2rem;}}
.warn{{background:#2b2200;border:1px solid #9e6a03;border-radius:8px;padding:1.5rem;
       max-width:700px;margin:2rem auto;}}
h1{{color:#d29922;font-size:1.3rem;margin:0 0 .75rem}}
code{{background:#1c1c1c;border:1px solid #30363d;border-radius:4px;padding:.15rem .4rem;
      font-size:.85rem;color:#58a6ff}}
ol{{margin:.5rem 0 0 1.2rem;line-height:1.9}}
</style>
</head>
<body>
<div class="warn">
  <h1>⚠ TV Options Scan — Cookie Required</h1>
  <p>Generated {generated_at}</p>
  <p>The TradingView scan2 options API requires a fresh browser session cookie.
     The current <code>tradingview/cookie</code> file is missing or expired
     (<code>sessionid=</code> not found).</p>
  <h2 style="font-size:1rem;margin:1rem 0 .5rem">How to refresh:</h2>
  <ol>
    <li>Open <strong>tradingview.com</strong> in Chrome and log in</li>
    <li>Open DevTools → Application → Cookies → www.tradingview.com</li>
    <li>Copy <code>sessionid</code> and <code>sessionid_sign</code> values</li>
    <li>Write to <code>tradingview/cookie</code> as:<br>
        <code>sessionid=xxxxx; sessionid_sign=yyyyy;</code></li>
    <li>Re-run menu option <strong>10d</strong></li>
  </ol>
  <p style="margin-top:1rem;font-size:.85rem;color:#8b949e">
    Note: The 300 US quality stock universe was successfully built from the
    TradingView screener (which does not require authentication).
    Only the options chain fetch (scan2) needs the cookie.
  </p>
</div>
</body>
</html>"""


def run_scan(
    n: int = 300,
    n_expiries: int = 3,
    out_path: Path = _DEFAULT_OUT,
    refresh_universe: bool = False,
    verbose: bool = False,
    max_per_symbol: int = 2,
    option_type: str = "CALL",
    max_dte: int = 28,
    min_premium: float = 0.1,
    max_premium: float = 6.0,
    max_be_sigma: float = 0.80,
) -> list[dict]:
    """
    Main entry point: load universe → scan each stock → generate HTML report.

    Returns the sorted list of top recommendations.
    """
    HybridOptionsFetcher, compute_tqs_v2_fn, compute_wrs_fn, inject_subscores_fn, compute_premium_win_fn = _get_fetcher_fns()
    load_watchlist, build_watchlist = _get_universe_fns()

    # ── Moomoo daemon threads: prevent SDK threads from blocking process exit ──
    try:
        from moomoo import SysConfig
        SysConfig.set_all_thread_daemon(True)
    except ImportError:
        pass

    # ── Step 1: Load or build stock universe ──────────────────────────────
    stocks = load_watchlist(_WATCHLIST) if not refresh_universe else []
    # Rebuild cache if it has fewer entries than the requested universe size.
    # This happens when a previous --n 5 test run cached only 5 stocks.
    if stocks and len(stocks) < n:
        if verbose:
            print(f"Cached universe has {len(stocks)} stocks but {n} requested — rebuilding...")
        stocks = []
    if not stocks:
        if verbose:
            print("Building US stock universe (fetching from TV screener)...")
        stocks = build_watchlist(count=n, out_path=_WATCHLIST, verbose=verbose)

    if not stocks:
        print("[ERROR] Could not load stock universe. Check network connectivity.")
        return []

    stocks = stocks[:n]

    if verbose:
        print(f"Scanning {len(stocks)} stocks for {option_type.upper()} options...")

    # ── Step 2: Scan each stock ────────────────────────────────────────────
    fetcher = HybridOptionsFetcher(verbose=verbose)

    # ── Auto-start Moomoo OpenD for real OI data (check ONCE, not per-stock) ──
    _moomoo_ok = False
    try:
        import importlib
        mc = importlib.import_module("moomoo_config")
        mc.ensure_opend_running(timeout=5)
        _moomoo_ok = True
    except Exception:
        print("  [INFO] OpenD not available — using TV scan2 data only (faster scan)")
    fetcher._moomoo_available = _moomoo_ok

    if not fetcher.is_available():
        print("[WARN] TV options not available — cookie missing or expired.")
        print("       To fix: copy fresh sessionid= from browser DevTools → tradingview/cookie")
        print("       Generating universe-only report...")
        ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        html = _build_cookie_required_html(ts)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        print(f"Report saved → {out_path}")
        return []

    all_top: list[dict] = []
    scanned = 0
    found   = 0
    _interrupted = False
    # scan_id is an ISO datetime stamp used as DB key for this entire scan run
    _scan_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _save_partial(label: str = "partial") -> None:
        """Write HTML from whatever contracts we have so far."""
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        snap   = sorted(all_top, key=lambda x: x.get("tqs_v2", 0.0), reverse=True)
        html   = build_html(snap, scanned, found, ts_now)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        print(f"  [autosave] {label} — {len(snap)} contracts from {scanned} stocks → {out_path}")

    def _sigint_handler(sig, frame):  # noqa: ANN001
        nonlocal _interrupted
        _interrupted = True
        print("\n  [Ctrl+C] Saving partial results before exit...")

    signal.signal(signal.SIGINT, _sigint_handler)

    # ── Two-pass WebSocket optimisation ─────────────────────────────────────
    # Phase 1 (this loop): fetch TV scan2 + Moomoo OI for every stock WITHOUT
    # opening a WS connection per stock (saving 3-8s × N stocks).
    # Phase 2 (after loop): open ONE WS session for ALL collected contracts.
    fetcher.skip_ws = True

    _t_wall = time.time()

    for i, stock in enumerate(stocks, 1):
        symbol = stock.get("symbol", "")
        if not symbol:
            continue
        # Skip preferred shares (e.g. NYSE:JPM/PM, NYSE:BAC/PQ) — they have no listed options
        if "/" in symbol:
            print(f"  [{i:>3}/{len(stocks)}] {symbol:<20s} (skipped: preferred share)", flush=True)
            continue

        # Skip dotted tickers (e.g. BRK.B) — yfinance fails on them
        bare = symbol.split(":")[-1] if ":" in symbol else symbol
        if "." in bare:
            print(f"  [{i:>3}/{len(stocks)}] {symbol:<20s} (skipped: dotted ticker)", flush=True)
            continue

        _t_sym = time.time()
        try:
            results = scan_symbol(
                symbol,
                fetcher,
                compute_tqs_v2_fn,
                compute_wrs_fn,
                inject_subscores_fn,
                compute_premium_win_fn,
                n_expiries=n_expiries,
                verbose=verbose,
                option_type=option_type,
                max_dte=max_dte,
                min_premium=min_premium,
                max_premium=max_premium,
            )
        except Exception as exc:
            print(f"  [{i:>3}/{len(stocks)}] {symbol:<20s} (ERROR: {exc})")
            time.sleep(0.15)
            continue
        _dt_sym = time.time() - _t_sym
        scanned += 1
        # Compute be_sigma_ratio for ALL contracts (needed for console display)
        import math as _m_be
        for _c in results:
            _iv_c = _c.get("iv", 0) or 0
            _dte_c = _c.get("dte", 0) or 0
            _spot_c = _c.get("spot_price", 0) or 0
            _strike_c = _c.get("strike", 0) or 0
            _mid_c = _c.get("mid_price", 0) or 0
            if _iv_c > 0 and _dte_c > 0 and _spot_c > 0:
                _sigma = (_iv_c / 100.0) * _m_be.sqrt(max(_dte_c, 1) / 365.0) * 100.0
                _be = ((_strike_c + _mid_c) / _spot_c - 1.0) * 100.0
                _c["be_sigma_ratio"] = _be / _sigma if _sigma > 0 else float("inf")
            else:
                _c["be_sigma_ratio"] = float("inf")
        top_results = results[:3]
        # Tag each contract with its underlying TV symbol before storing
        for r in top_results:
            r.setdefault("tv_symbol", symbol)
        contracts_this = len(top_results)
        if results:
            found += 1
            all_top.extend(top_results)

        # Log progress after every stock so user can track long scans
        top_tqs = results[0].get("tqs_v2", 0.0) if results else 0.0
        ws_src  = results[0].get("ws_source", "?") if results else "?"
        status = (
            f"+{contracts_this}c TQS={top_tqs:.0f} [{ws_src}]"
            if results else "(no qualifying contracts)"
        )
        print(f"  [{i:>3}/{len(stocks)}] {symbol:<20s} {status}  | total={len(all_top)} ({_dt_sym:.1f}s)", flush=True)
        # Per-contract detail logging (always show — production UX)
        if results:
            for c in results:
                c_strike = c.get("strike", 0)
                c_dte = c.get("dte", 0)
                c_spr = c.get("spread_pct", 0) or 0
                c_oi = c.get("oi", 0) or 0
                c_src = c.get("oi_source", "?")
                c_tqs = c.get("tqs_v2", 0) or 0
                c_be = c.get("be_sigma_ratio", 0) or 0
                c_mid = c.get("mid_price", 0) or 0
                c_spot = c.get("spot_price", 1)
                c_lev = (c_spot / c_mid) if c_mid > 0 else 0
                src_tag = {"moomoo": "moomoo", "yfinance": "yfinanc",
                           "mm-miss": "mm-miss", "unavailable": "no-src"}.get(c_src, c_src[:7])
                print(f"             C{c_strike:<8g} {c_dte:>3}d  spr={c_spr:5.1f}%"
                      f"  oi={c_oi:>6}[{src_tag}]"
                      f"  tqs={c_tqs:>4.0f}  be={c_be:.2f}  lev={c_lev:.1f}\u00d7"
                      f"  \u2192 {c.get('recommendation_reason', '')[:60]}")
        if i % 25 == 0:
            # Summary every 25 stocks
            print(f"  --- {i}/{len(stocks)} processed: {found} with contracts, {len(all_top)} total ---")
        # Autosave every 50 stocks so Ctrl+C doesn't lose all progress
        if i % 50 == 0:
            _save_partial(f"{i}/{len(stocks)} stocks done")
        if _interrupted:
            print(f"  [stopped] Scan interrupted at {i}/{len(stocks)}")
            break
        time.sleep(0.15)  # rate-limit TV scan2 API

    # ── Step 3: Sort by TQS v2 and write report ────────────────────────────
    _elapsed = time.time() - _t_wall
    print(f"\n⏱  Scan complete: {_elapsed:.0f}s ({_elapsed / 60:.1f}min) — {scanned} stocks scanned")

    # ── Phase 2: Batch WebSocket enrichment (one WS session for all contracts) ──
    if all_top:
        ws_timeout_batch = min(60.0, max(15.0, len(all_top) * 0.5))
        print(
            f"\n[WS-batch] {len(all_top)} contracts (already enriched by TV scan2 in Phase 1)",
            flush=True,
        )
        _ws_t0 = time.time()
        HybridOptionsFetcher.batch_enrich_ws(
            all_top,
            ws_timeout=ws_timeout_batch,
            verbose=verbose,
        )
        _ws_dt = time.time() - _ws_t0
        print(f"[WS-batch] done in {_ws_dt:.1f}s")
        # Re-sort after TQS v2 scores are updated with WS-derived inputs
        all_top.sort(key=lambda x: x.get("tqs_v2", 0.0), reverse=True)
    else:
        all_top.sort(key=lambda x: x.get("tqs_v2", 0.0), reverse=True)

    # Diversification cap: max N contracts per underlying symbol
    # Prevents all-NVDA or all-AAPL output when scanning few stocks.
    _max_per_sym = max_per_symbol
    if _max_per_sym and _max_per_sym > 0:
        _sym_count: dict[str, int] = {}
        _diverse: list = []
        for _c in all_top:
            _sym = _c.get("underlying") or (_c.get("symbol", "").split(":")[1].split("2")[0] if ":" in _c.get("symbol", "") else "?")
            if _sym_count.get(_sym, 0) < _max_per_sym:
                _diverse.append(_c)
                _sym_count[_sym] = _sym_count.get(_sym, 0) + 1
        if len(_diverse) < len(all_top):
            print(f"  Diversity cap ({_max_per_sym}/symbol): {len(all_top)} → {len(_diverse)} contracts")
        all_top = _diverse

    # ── Enrich top contracts with IV Rank (only for final output, max ~100) ──
    _top_for_ivr = all_top[:100]
    if _top_for_ivr:
        _ivr_symbols_done: set = set()
        for _c in _top_for_ivr:
            _sym_raw = _c.get("symbol", "")
            _underlying = _sym_raw.split(":")[1] if ":" in _sym_raw else _sym_raw
            # Strip trailing digits from OPRA-style codes (e.g. NVDA260511C187 → NVDA)
            _underlying = _underlying.split("2")[0] if "2" in _underlying else _underlying
            _iv_val = _c.get("iv", 0) or 0
            _c["iv_rank"] = _compute_iv_rank(_underlying, _iv_val)
            _ivr_symbols_done.add(_underlying)
        # Also store be_sigma_ratio for each contract
        import math as _m_ivr
        for _c in _top_for_ivr:
            _iv_c = _c.get("iv", 0) or 0
            _dte_c = _c.get("dte", 0) or 0
            _spot_c = _c.get("spot_price", 0) or 0
            _strike_c = _c.get("strike", 0) or 0
            _mid_c = _c.get("mid_price", 0) or 0
            if _iv_c > 0 and _dte_c > 0 and _spot_c > 0:
                _sigma = (_iv_c / 100.0) * _m_ivr.sqrt(max(_dte_c, 1) / 365.0) * 100.0
                _be = ((_strike_c + _mid_c) / _spot_c - 1.0) * 100.0
                _c["be_sigma_ratio"] = _be / _sigma if _sigma > 0 else float('inf')
            else:
                _c["be_sigma_ratio"] = float('inf')

    if verbose:
        print(f"\nScanned {scanned} stocks, found {found} with qualifying contracts")
        if all_top:
            top = all_top[0]
            print(
                f"Top contract: {top.get('symbol','')} {option_type[0].upper()}{top.get('strike',0):.0f} "
                f"TQS={top.get('tqs_v2',0):.1f}"
            )
        else:
            print("No contracts found — check TV cookie and market hours")

    # ── Top-picks summary table (always shown) ────────────────────────────
    if all_top:
        import math as _m
        _ot_col = option_type[0].upper()   # "C" or "P"
        print(f"\n{'─'*105}")
        print(f"  {'#':>3}  {'Symbol':<22} {_ot_col:>7} {'DTE':>4} {'TQS':>5} {'WRS':>5} {'PW':>5} "
              f"{'BE%':>6} {'1σ%':>5} {'Lev':>5} {'Vol/OI':>7}")
        print(f"{'─'*105}")
        for rank, c in enumerate(all_top[:15], 1):
            sym  = c.get("symbol", "?")
            K    = c.get("strike", 0)
            dte  = c.get("dte", 0)
            tqs  = c.get("tqs_v2", 0)
            wrs  = c.get("wrs", 0)
            pw   = c.get("premium_win", 0)
            spot = c.get("spot_price", 0) or 0
            mid  = c.get("mid_price", 0) or 0
            d    = c.get("delta", 0.3) or 0.3
            iv   = c.get("iv", 30) or 30
            oi   = c.get("oi", 0) or 0
            vol  = c.get("volume", 0) or 0
            # BE% = how much the stock must move from spot to break even
            if option_type.upper() == "PUT":
                be_pct = (1.0 - (K - mid) / spot) * 100.0 if spot > 0 else 0.0
            else:
                be_pct = ((K + mid) / spot - 1.0) * 100.0 if spot > 0 else 0.0
            sigma_T  = (iv / 100.0) * _m.sqrt(max(dte, 1) / 365.0) * 100.0 if iv > 0 else 0.0
            leverage = (abs(d) * spot / mid) if mid > 0 else 0.0
            vol_oi   = (vol / oi) if oi > 0 and vol > 0 else 0.0
            achievable = "✓" if (sigma_T > 0 and be_pct <= sigma_T) else "✗"
            print(f"  {rank:>3}  {sym:<22} {K:>7.0f} {dte:>4} {tqs:>5.0f} {wrs:>5.0f} {pw:>5.0f} "
                  f"{be_pct:>5.1f}%{achievable} {sigma_T:>5.1f}% {leverage:>5.1f}× {vol_oi:>6.2f}×")
        print(f"{'─'*105}")
        print(f"  ✓ = break-even within 1-sigma move  Lev = delta-leverage  Vol/OI = fresh flow ratio")

    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = build_html(all_top, scanned, found, ts)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Report saved → {out_path}")

    # ── Actionable report (filtered + signal engine + support levels) ──────
    try:
        from tradingview.options.options_actionable_report import run_actionable
        actionable_out = out_path.parent / "options_actionable.html"
        run_actionable(
            all_top,
            actionable_out,
            scanned_count=scanned,
            found_count=found,
            generated_at=ts,
            max_be_sigma=max_be_sigma,
        )
    except Exception as e:
        print(f"  [actionable] Report generation failed: {e}")

    # ── Step 4: Archive report ─────────────────────────────────────────────
    # Every scan run is archived to reports/archive/options/ with a timestamp
    # so historical reports are never overwritten and can be compared later.
    _archive_report(out_path)

    # ── Step 5: Persist results to alert_tracker.db ────────────────────────
    # Contracts must be stored in DB for vectorbt backtesting:
    # "did TQS≥80 contracts profit after 7/14/30 days?"
    # Golden Rule #15: ALL computed parameters (TQS, scores, Greeks, IV) must
    # be in dedicated DB columns, not only in result_json.
    _save_to_db(all_top, scan_id=_scan_ts)

    return all_top


def _archive_report(report_path: Path) -> None:
    """Copy the HTML report to reports/archive/options/ with a timestamp suffix."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = _ROOT_DIR / "reports" / "archive" / "options"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"options_scan_{ts}.html"
    try:
        shutil.copy2(report_path, dest)
        print(f"Report archived → {dest}")
    except Exception as exc:
        print(f"[WARN] Report archive failed: {exc}")


def _save_to_db(contracts: list[dict], scan_id: str) -> None:
    """Persist scan results to alert_tracker.db (options_scan_results table)."""
    try:
        from tradingview.alert_tracker import save_options_scan, DB_PATH  # noqa: PLC0415
        n = save_options_scan(
            contracts=contracts,
            scan_id=scan_id,
            scan_date=scan_id[:10],
            db_path=DB_PATH,
        )
        print(f"DB: {n}/{len(contracts)} contracts saved to alert_tracker.db (options_scan_results)")
    except Exception as exc:
        print(f"[WARN] DB save failed: {exc}")
        print("       alert_tracker.db not updated — run will still complete normally")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scan US quality stocks for top options"
    )
    parser.add_argument(
        "--n", type=int, default=300,
        help="Max stocks to scan (default: 300)"
    )
    parser.add_argument(
        "--expiries", type=int, default=3,
        help="Number of expiry dates per symbol (default: 3)"
    )
    parser.add_argument(
        "--out", default=str(_DEFAULT_OUT),
        help="Output HTML report path"
    )
    parser.add_argument(
        "--refresh-universe", action="store_true",
        help="Re-fetch the stock universe even if cached"
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--max-dte", type=int, default=28, dest="max_dte",
        help="Maximum days to expiry for option contracts (default: 28)"
    )
    parser.add_argument(
        "--min-premium", type=float, default=0.1, dest="min_premium",
        help="Minimum contract premium in $ (default: 0.10)"
    )
    parser.add_argument(
        "--max-premium", type=float, default=6.0, dest="max_premium",
        help="Maximum contract premium in $ (default: 6.00)"
    )
    parser.add_argument(
        "--max-per-symbol", type=int, default=2, dest="max_per_symbol",
        help="Max contracts per underlying in final output (default: 2, 0=unlimited)"
    )
    parser.add_argument(
        "--option-type", choices=["CALL", "PUT", "BOTH"], default="CALL", dest="option_type",
        help="Option type to scan (default: CALL)"
    )
    parser.add_argument(
        "--actionable-out", default=str(_ROOT_DIR / "reports" / "options_actionable.html"),
        dest="actionable_out",
        help="Output path for actionable report"
    )
    parser.add_argument(
        "--max-be-sigma", type=float, default=0.80, dest="max_be_sigma",
        help="Maximum BE/1σ ratio for actionable filter (default: 0.80)"
    )
    args = parser.parse_args()

    run_scan(
        n=args.n,
        n_expiries=args.expiries,
        out_path=Path(args.out),
        refresh_universe=args.refresh_universe,
        verbose=args.verbose,
        max_per_symbol=args.max_per_symbol,
        option_type=args.option_type,
        max_dte=args.max_dte,
        min_premium=args.min_premium,
        max_premium=args.max_premium,
        max_be_sigma=args.max_be_sigma,
    )
    # Force-exit to prevent ThreadPoolExecutor atexit handler from hanging
    # when Moomoo/WebSocket worker threads are still running in background.
    import os as _os_exit
    _os_exit._exit(0)
