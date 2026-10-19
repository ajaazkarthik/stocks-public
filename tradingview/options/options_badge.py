"""
Options Badge -- OptionsAnalyzer integration for signal card HTML
================================================================
Wraps OptionsAnalyzer.analyze_all_modes() and renders a three-mode
options badge for embedding in signal_stack.py report cards.

How it works
------------
1.  Auto-detects market from TradingView pro_symbol exchange prefix
2.  Computes all strategy modes in one chain fetch:
    BALANCED (ATM), AGGRESSIVE (OTM), SAFE (ITM)
3.  Calls OptionsAnalyzer.analyze_all_modes() which internally tries:
      US  -> Moomoo OpenD (real-time + Greeks)  then  yfinance (B-S delta)
      HK  -> Moomoo OpenD (real-time + Greeks)
      IN  -> Angel One SmartAPI (optionGreek endpoint)
4.  Renders a three-tab card so users can switch among modes:
    ⚖️ BALANCED, 🚀 AGGRESSIVE, 🛡️ SAFE
      Strike / Expiry / DTE / Mid price / IV% / Delta / OI
      Option score bar (0-100) / Recommendation reason / Warnings
      Up to 2 alternative contracts shown compactly

Supported markets:  US / HK / IN (NSE/BSE)
Unsupported:        ASX / SGX / etc -> returns "" silently

Public API
----------
    compute_options_badge_data(tv_symbol, signal_score=50.0, spot_price=None) -> dict
    _options_html_badge(badge_data) -> str (HTML)
    OPTIONS_BADGE_CSS               -> str (CSS)
"""

from __future__ import annotations

from typing import Optional

# -- Supported exchange sets --------------------------------------------------
_US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "BATS", "CBOE", "ARCA"}
_HK_EXCHANGES = {"HKEX", "HKEX_DLY"}
_IN_EXCHANGES = {"NSE", "BSE"}
_ALL_SUPPORTED = _US_EXCHANGES | _HK_EXCHANGES | _IN_EXCHANGES


def _parse_exchange(tv_symbol: str) -> tuple[str, str]:
    """Return (exchange_upper, ticker) from 'EXCHANGE:TICKER'."""
    if ":" in tv_symbol:
        exch, ticker = tv_symbol.split(":", 1)
        return exch.upper(), ticker
    return "NYSE", tv_symbol   # bare ticker assumed US


# -- Lazy OptionsAnalyzer singleton ------------------------------------------
_analyzer = None


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        try:
            from options.options_analyzer import OptionsAnalyzer
            _analyzer = OptionsAnalyzer()
        except Exception:
            _analyzer = None
    return _analyzer


# -- Main compute function ----------------------------------------------------

def compute_options_badge_data(
    tv_symbol: str,
    signal_score: float = 50.0,
    spot_price: Optional[float] = None,
    scanner_all_candidates: bool = False,
) -> dict:
    """
    Compute options badge data for *tv_symbol* for ALL THREE strategy modes.

    Parameters
    ----------
    tv_symbol    : TradingView pro_symbol e.g. "NASDAQ:MU"
    signal_score : Composite signal score 0-100
    spot_price   : Optional override (not used currently)

    Returns
    -------
    dict with keys "SAFE", "BALANCED", "AGGRESSIVE", "meta",
    plus "badge_exchange" and "badge_ticker".
    Returns {} if market is not supported (ASX, SGX, etc.)
    """
    exch, ticker = _parse_exchange(tv_symbol)

    if exch not in _ALL_SUPPORTED:
        return {}   # unsupported market -> silently skip

    analyzer = _get_analyzer()
    if analyzer is None:
        # Return a renderable error badge — include 'meta' so _options_html_badge
        # does NOT silently return ''. Without 'meta' the badge is invisible.
        return {
            "error":             "OptionsAnalyzer not available",
            "options_available": False,
            "badge_exchange":    exch,
            "badge_ticker":      ticker,
            "meta": {
                "market":        ("HK" if exch in _HK_EXCHANGES
                                   else "NSE" if exch in _IN_EXCHANGES
                                   else "US"),
                "data_source":   "none",
                "spot_price":    None,
                "earnings_risk": False,
                "signal_score":  signal_score,
            },
        }

    try:
        analyzer_args = {
            "pro_symbol": tv_symbol,
            "signal_score": signal_score,
        }
        if scanner_all_candidates:
            analyzer_args["include_all_contracts"] = True
        result = analyzer.analyze_all_modes(**analyzer_args)
        # Backward compatibility: keep legacy top-level shape so existing
        # callers/tests that read result["market"] or result["options_available"]
        # still work while new callers can use SAFE/BALANCED/AGGRESSIVE.
        legacy_mode = (
            "AGGRESSIVE" if signal_score >= 75
            else ("BALANCED" if signal_score >= 55 else "SAFE")
        )
        meta = result.get("meta", {})
        legacy = result.get(legacy_mode, {})

        result["market"]            = meta.get("market", exch)
        result["symbol"]            = meta.get("symbol", tv_symbol)
        result["spot_price"]        = meta.get("spot_price", spot_price)
        result["data_source"]       = meta.get("data_source", "none")
        result["earnings_risk"]     = meta.get("earnings_risk", False)
        result["mode"]              = legacy_mode
        result["badge_mode"]        = legacy_mode
        result["fo_eligible"]       = legacy.get("fo_eligible", True)
        result["options_available"] = legacy.get("options_available", False)
        result["top_contracts"]     = legacy.get("top_contracts", [])
        result["warnings"]          = legacy.get("warnings", [])
        result["no_options_reason"] = legacy.get("no_options_reason")

        result["badge_exchange"] = exch
        result["badge_ticker"]   = ticker

        # These legacy display metrics can make extra history/network calls.
        # The scanner recommendation path needs only the existing analyzer's
        # contract ranking and must remain bounded to that one chain fetch.
        if scanner_all_candidates:
            result["iv_percentile"] = None
            result["put_call_skew"] = 0.0
            result["max_pain"] = None
        else:
            # ── Enhanced metrics: IV rank, Put/Call skew, Max pain ──────
            try:
                from options.options_analyzer import iv_percentile, put_call_skew, max_pain as _max_pain

                # Gather all contracts across all modes for skew & max pain
                all_contracts: list[dict] = []
                for _mode in ("BALANCED", "AGGRESSIVE", "SAFE"):
                    all_contracts.extend(result.get(_mode, {}).get("top_contracts", []))

                # IV percentile using median IV of BALANCED contracts
                bal_contracts = result.get("BALANCED", {}).get("top_contracts", [])
                median_iv = None
                if bal_contracts:
                    ivs = [c.get("iv") or 0 for c in bal_contracts if c.get("iv")]
                    if ivs:
                        median_iv = sorted(ivs)[len(ivs) // 2]
                result["iv_percentile"] = iv_percentile(tv_symbol, median_iv) if median_iv and median_iv > 0 else None

                result["put_call_skew"] = put_call_skew(all_contracts) if all_contracts else 0.0
                result["max_pain"] = _max_pain(all_contracts) if all_contracts else None
            except Exception:
                result["iv_percentile"] = None
                result["put_call_skew"] = 0.0
                result["max_pain"] = None

        return result
    except Exception as exc:
        return {
            "error":          str(exc),
            "options_available": False,
            "badge_exchange": exch,
            "badge_ticker":   ticker,
            # Include meta so _options_html_badge can render an error badge
            # instead of returning empty string.
            "meta": {
                "market":       ("HK" if exch in _HK_EXCHANGES
                                 else "NSE" if exch in _IN_EXCHANGES
                                 else "US"),
                "data_source":  "none",
                "spot_price":   None,
                "earnings_risk": False,
                "signal_score": signal_score,
            },
        }


# -- HTML helpers -------------------------------------------------------------

_MARKET_FLAG  = {
    "US":  "\U0001f1fa\U0001f1f8",
    "HK":  "\U0001f1ed\U0001f1f0",
    "NSE": "\U0001f1ee\U0001f1f3",
    "BSE": "\U0001f1ee\U0001f1f3",
}
_MARKET_LABEL = {
    "US":  "US Options",
    "HK":  "HK Options",
    "NSE": "India F&O",
    "BSE": "India F&O",
}
_MODE_ICON    = {
    "AGGRESSIVE": "\U0001f680",
    "BALANCED":   "\u2696\ufe0f",
    "SAFE":       "\U0001f6e1\ufe0f",
}
_MODE_TOOLTIP = {
    "AGGRESSIVE": "OTM calls | \u0394 0.20-0.48 | DTE 7-21d | Signal \u226575 (high conviction)",
    "BALANCED":   "ATM calls | \u0394 0.40-0.65 | DTE 14-35d | Signal 55-74 (standard)",
    "SAFE":       "ITM calls | \u0394 0.60-0.80 | DTE 28-60d | Signal &lt;55 (conservative)",
}
_SOURCE_LABEL = {
    "yfinance": "yfinance",
    "moomoo":   "Moomoo",
    "angelone": "Angel One",
    "none":     "\u2014",
}


def _exp_label(expiry: str) -> str:
    """e.g. '2026-04-24' -> '24 Apr'"""
    try:
        from datetime import datetime
        return datetime.strptime(expiry[:10], "%Y-%m-%d").strftime("%d %b").lstrip("0")
    except (ValueError, TypeError):
        return expiry[:10] if expiry else ""


def _fmt_price(val: float, decimals: int = 2) -> str:
    if not val:
        return "\u2014"
    if val >= 10_000:
        return f"{val:,.0f}"
    if val >= 100:
        return f"{val:,.1f}"
    return f"{val:.{decimals}f}"


def _fmt_oi(oi: int) -> str:
    if oi >= 1_000_000:
        return f"{oi/1_000_000:.1f}M"
    if oi >= 1_000:
        return f"{oi/1_000:.0f}K"
    return str(oi) if oi else "\u2014"


def _score_bar_html(score: float) -> str:
    """Inline score bar + label."""
    pct = min(max(score / 100.0, 0.0), 1.0)
    bar_col = "#4ade80" if score >= 70 else ("#f59e0b" if score >= 50 else "#f87171")
    return (
        f'<span class="ob-score-wrap">'
        f'<span class="ob-bar-wrap">'
        f'<span class="ob-bar-fill" style="width:{pct*100:.0f}%;background:{bar_col}"></span>'
        f'</span>'
        f'<span class="ob-bar-label" style="color:{bar_col}">{score:.0f}/100</span>'
        f'</span>'
    )


def _pick_row_html(c: dict, rank: int, ccy: str) -> str:
    """Render one scored contract row."""
    strike = c.get("strike") or 0
    expiry = c.get("expiry") or ""
    mid    = c.get("mid_price") or c.get("mid") or 0
    iv     = c.get("iv") or 0
    delta  = c.get("delta") or 0
    oi     = int(c.get("oi") or 0)
    dte    = c.get("dte") or 0
    score  = c.get("option_score") or 0
    cs_abs    = c.get("current_score") or score       # absolute gate score (pre-Moneyball)
    mb_score  = c.get("moneyball_score")              # chain-relative population score
    mb_active = c.get("mb_active", False)             # True only for ≥2-contract chains
    reason = c.get("recommendation_reason") or ""
    spread = c.get("spread_pct") or 0
    mono   = c.get("moneyness_pct") or 0

    delta_col = "#4ade80" if delta >= 0.42 else ("#f59e0b" if delta >= 0.25 else "#64748b")
    mono_str  = f"+{mono:.1f}%" if mono >= 0 else f"{mono:.1f}%"
    iv_str    = f"{iv:.0f}%" if iv >= 1 else f"{iv*100:.0f}%"

    if spread < 5:
        spread_badge = '<span class="ob-liq-good">&#9989; liquid</span>'
    elif spread < 15:
        spread_badge = f'<span class="ob-liq-warn">&#9888; spd {spread:.0f}%</span>'
    else:
        spread_badge = f'<span class="ob-liq-bad">&#128683; wide spd {spread:.0f}%</span>'

    rank_icon = "#1" if rank == 1 else f"#{rank}"
    rank_cls  = "ob-rank-1" if rank == 1 else "ob-rank-alt"

    reason_html = (
        f'<div class="ob-reason-line">{reason}</div>'
        if reason and rank == 1 else ""
    )

    # Build Gate·MB compact display for multi-contract chains (shown in signal reports)
    if mb_active and mb_score is not None:
        mb_detail_html = (
            f'<div style="font-size:.6rem;color:#6b7280;margin-top:2px;white-space:nowrap">'
            f'Gate\u00a0{cs_abs:.0f}\u00a0\u00b7\u00a0MB\u00a0{mb_score:.0f}'
            f'</div>'
        )
    else:
        mb_detail_html = ""

    return (
        f'<div class="ob-pick-row {rank_cls}">'
        f'<div class="ob-pick-header">'
        f'<span class="ob-pick-rank-tag">{rank_icon} PICK</span>'
        f'<span class="ob-pick-contract">'
        f'<b style="color:#f8fafc">{ccy}{_fmt_price(strike, 0)}</b>'
        f'<span class="ob-pick-type">CALL</span>'
        f'<span class="ob-pick-exp">{_exp_label(expiry)} ({dte}d)</span>'
        f'</span>'
        f'{_score_bar_html(score)}'
        f'{mb_detail_html}'
        f'</div>'
        f'<div class="ob-pick-detail">'
        f'<span class="ob-mid">mid {ccy}{_fmt_price(mid)}</span>'
        f'<span class="ob-iv-val">IV {iv_str}</span>'
        f'<span class="ob-delta-val" style="color:{delta_col}">&Delta; {delta:.2f}</span>'
        f'<span class="ob-oi-val">OI {_fmt_oi(oi)}</span>'
        f'<span class="ob-mono-val">{mono_str}</span>'
        f'{spread_badge}'
        f'</div>'
        f'{reason_html}'
        f'</div>'
    )


# -- Unique ID counter for CSS-only tabs (no JS) -----------------------------
_badge_counter = 0


def _next_badge_id() -> str:
    global _badge_counter
    _badge_counter += 1
    return f"ob{_badge_counter}"


def _render_mode_panel(mode_data: dict, mode_name: str, ccy: str) -> str:
    """Render the content panel for ONE mode."""
    available = mode_data.get("options_available", False)
    no_reason = mode_data.get("no_options_reason", "")
    top_c     = mode_data.get("top_contracts", [])
    warnings  = mode_data.get("warnings", [])
    earnings  = mode_data.get("earnings_risk", False)

    if not available:
        msg = no_reason or "No contracts available."
        return f'<div class="ob-panel-inner ob-panel-empty">&#128205; {msg}</div>'

    picks_html = "".join(
        _pick_row_html(c, i + 1, ccy)
        for i, c in enumerate(top_c[:2])
    )

    alts = top_c[2:5]
    alts_html = ""
    if alts:
        alt_labels = ", ".join(
            f"{ccy}{_fmt_price(c.get('strike', 0), 0)}&middot;{_exp_label(c.get('expiry', ''))}"
            for c in alts
        )
        alts_html = f'<div class="ob-alts">+ {len(alts)} more: {alt_labels}</div>'

    warn_html = ""
    for w in warnings[:2]:
        warn_html += f'<div class="ob-warn-line">&#9888; {w}</div>'

    earn_banner = ""
    if earnings:
        earn_banner = (
            '<div class="ob-earn-warn">'
            "&#128197; Earnings near &mdash; IV inflated, reduce size or wait"
            '</div>'
        )

    return (
        f'<div class="ob-panel-inner">'
        f'{earn_banner}'
        f'{picks_html}'
        f'{alts_html}'
        f'{warn_html}'
        f'</div>'
    )


def _options_html_badge(badge_data: Optional[dict]) -> str:
    """
    Render the options badge HTML with all 3 mode tabs (BALANCED / AGGRESSIVE / SAFE).
    The user clicks a tab to switch modes.  BALANCED is shown by default.
    Returns '' for unsupported markets or when data is unavailable.
    """
    if not badge_data:
        return ""

    error  = badge_data.get("error", "")
    exch   = badge_data.get("badge_exchange", "")
    ticker = badge_data.get("badge_ticker", "")

    # New all-modes layout: keys are "SAFE", "BALANCED", "AGGRESSIVE", "meta"
    meta = badge_data.get("meta")
    if meta is None:
        # No meta key at all: old single-mode shape or bare error dict.
        # Return empty so unsupported-market {} callers stay silent.
        return ""

    market  = meta.get("market", exch) or exch
    source  = meta.get("data_source", "yfinance")
    spot    = meta.get("spot_price", 0) or 0
    earnings = meta.get("earnings_risk", False)

    if market in ("HKEX", "HKEX_DLY"):
        market = "HK"

    flag     = _MARKET_FLAG.get(market, "&#128202;")
    label    = _MARKET_LABEL.get(market, "Options")
    src_text = _SOURCE_LABEL.get(source, source)

    ccy = "HK$" if market == "HK" else ("&#8377;" if market in ("NSE", "BSE", "IN") else "$")
    spot_str = (
        f"{ccy}{spot:,.0f}" if spot >= 100 else
        f"{ccy}{spot:,.2f}" if spot > 0 else ""
    )

    if not market:
        return ""

    # ── Enhanced metrics row (IV Rank, P/C Skew, Max Pain) ──────────
    iv_pct   = badge_data.get("iv_percentile")
    pc_skew  = badge_data.get("put_call_skew", 0.0)
    mp_val   = badge_data.get("max_pain")

    _enhanced_items: list[str] = []
    if iv_pct is not None:
        iv_col = "#f87171" if iv_pct > 75 else ("#f59e0b" if iv_pct > 25 else "#4ade80")
        _enhanced_items.append(
            f'<span class="ob-enh-item">'
            f'IV Rank <b style="color:{iv_col}">{iv_pct:.0f}%</b>'
            f'</span>'
        )
    if pc_skew and abs(pc_skew) >= 0.01:
        sk_col = "#f87171" if pc_skew > 5 else ("#4ade80" if pc_skew < -5 else "#94a3b8")
        _enhanced_items.append(
            f'<span class="ob-enh-item">'
            f'P/C Skew <b style="color:{sk_col}">{pc_skew:+.1f}%</b>'
            f'</span>'
        )
    if mp_val is not None:
        _enhanced_items.append(
            f'<span class="ob-enh-item">'
            f'Max Pain <b style="color:#7dd3fc">{ccy}{_fmt_price(mp_val, 0)}</b>'
            f'</span>'
        )
    enhanced_row = ""
    if _enhanced_items:
        enhanced_row = (
            '<div class="ob-enhanced-row">'
            + "".join(_enhanced_items)
            + '</div>'
        )

    if error and not badge_data.get("BALANCED"):
        return (
            f'<div class="options-badge ob-unavail">'
            f'<div class="ob-header">{flag} {label}</div>'
            f'<div class="ob-msg">&#9888; {error}</div>'
            f'</div>'
        )

    bid = _next_badge_id()

    # Per-mode tooltips
    tip_b = _MODE_TOOLTIP.get("BALANCED", "")
    tip_a = _MODE_TOOLTIP.get("AGGRESSIVE", "")
    tip_s = _MODE_TOOLTIP.get("SAFE", "")

    # Panel content per mode
    panel_b = _render_mode_panel(badge_data.get("BALANCED", {}),   "BALANCED",   ccy)
    panel_a = _render_mode_panel(badge_data.get("AGGRESSIVE", {}), "AGGRESSIVE", ccy)
    panel_s = _render_mode_panel(badge_data.get("SAFE", {}),       "SAFE",       ccy)

    # Availability indicators on tabs
    def _avail_dot(mode_key):
        ok = badge_data.get(mode_key, {}).get("options_available", False)
        return '<span class="ob-tab-dot ob-dot-ok"></span>' if ok else '<span class="ob-tab-dot ob-dot-na"></span>'

    # Badge class: ob-active only when at least one mode has valid picks
    _any_avail = any(
        badge_data.get(m, {}).get("options_available", False)
        for m in ("BALANCED", "AGGRESSIVE", "SAFE")
    )
    badge_cls = "ob-active" if _any_avail else "ob-no-opts"

    return (
        f'<div class="options-badge {badge_cls}">'
        # Scoped style for this badge's tab selector (unique IDs per badge)
        f'<style>'
        f'#ob-tab-b-{bid}:checked~.ob-tab-bar label[for="ob-tab-b-{bid}"],'
        f'#ob-tab-a-{bid}:checked~.ob-tab-bar label[for="ob-tab-a-{bid}"],'
        f'#ob-tab-s-{bid}:checked~.ob-tab-bar label[for="ob-tab-s-{bid}"]'
        f'{{color:#f8fafc!important;background:#132138!important;border-color:#1d4e89!important}}'
        f'#ob-tab-b-{bid}:checked~.ob-panels .ob-panel-b-{bid},'
        f'#ob-tab-a-{bid}:checked~.ob-panels .ob-panel-a-{bid},'
        f'#ob-tab-s-{bid}:checked~.ob-panels .ob-panel-s-{bid}'
        f'{{display:block!important}}'
        f'</style>'
        # Header
        f'<div class="ob-header">'
        f'<span>{flag} {label}</span>'
        f'<span class="ob-spot">{ticker} {spot_str}</span>'
        f'<span class="ob-src-tag">{src_text}</span>'
        f'</div>'
        # Enhanced metrics row
        f'{enhanced_row}'
        # CSS-only tabs: hidden radio inputs must come BEFORE the labels + panels
        f'<input type="radio" name="ob-mode-{bid}" id="ob-tab-b-{bid}" class="ob-tab-radio" checked>'
        f'<input type="radio" name="ob-mode-{bid}" id="ob-tab-a-{bid}" class="ob-tab-radio">'
        f'<input type="radio" name="ob-mode-{bid}" id="ob-tab-s-{bid}" class="ob-tab-radio">'
        # Tab bar
        f'<div class="ob-tab-bar">'
        f'<label for="ob-tab-b-{bid}" class="ob-tab" title="{tip_b}">'
        f'&#9878;&#65039; BALANCED{_avail_dot("BALANCED")}</label>'
        f'<label for="ob-tab-a-{bid}" class="ob-tab" title="{tip_a}">'
        f'&#128640; AGGRESSIVE{_avail_dot("AGGRESSIVE")}</label>'
        f'<label for="ob-tab-s-{bid}" class="ob-tab" title="{tip_s}">'
        f'&#128737;&#65039; SAFE{_avail_dot("SAFE")}</label>'
        f'</div>'
        # Panel container — each panel has unique class so scoped CSS can target it
        f'<div class="ob-panels">'
        f'<div class="ob-panel ob-panel-b-{bid}">{panel_b}</div>'
        f'<div class="ob-panel ob-panel-a-{bid}">{panel_a}</div>'
        f'<div class="ob-panel ob-panel-s-{bid}">{panel_s}</div>'
        f'</div>'
        f'</div>'
    )


# -- CSS ----------------------------------------------------------------------

OPTIONS_BADGE_CSS = """
/* == Options Badge (L8b) ================================================== */
.options-badge{border-radius:8px;padding:12px 16px;border:1px solid #1e293b;margin:0 20px 12px}
.ob-active{background:linear-gradient(135deg,#0c1a2e 0%,#0f1f35 100%);border-color:#1d4e89}
.ob-unavail{background:#120d0d;border-color:#2a1a1a}
.ob-no-opts{background:#0f172a;border-color:#1e293b;opacity:.55}
.ob-header{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:700;color:#7dd3fc;margin-bottom:8px;flex-wrap:wrap}
.ob-spot{color:#94a3b8;font-size:11px;font-weight:400;margin-left:2px}
.ob-src-tag{margin-left:auto;background:#1e3a5f;color:#7dd3fc;border-radius:3px;padding:1px 6px;font-size:10px;font-weight:500}
/* -- CSS-only mode tabs -- */
.ob-tab-radio{display:none}
.ob-tab-bar{display:flex;gap:2px;margin-bottom:8px;border-bottom:1px solid #1e293b;padding-bottom:0}
.ob-tab{cursor:pointer;font-size:11px;font-weight:700;padding:5px 10px;border-radius:5px 5px 0 0;color:#64748b;background:#0c1422;border:1px solid #1e293b;border-bottom:none;transition:color .15s,background .15s;user-select:none;display:inline-flex;align-items:center;gap:4px}
.ob-tab:hover{color:#94a3b8;background:#0f2035}
.ob-tab-dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-left:3px}
.ob-dot-ok{background:#4ade80}
.ob-dot-na{background:#475569}
/* Panels — all hidden by default; active shown via scoped <style> per badge */
.ob-panels{min-height:60px}
.ob-panel{display:none}
/* -- Pick rows -- */
.ob-panel-inner{padding:4px 0}
.ob-panel-empty{font-size:11px;color:#475569;padding:8px 4px}
.ob-pick-row{border-radius:6px;padding:8px 12px;margin-bottom:6px}
.ob-rank-1{background:#091422;border:1px solid #1d4e89}
.ob-rank-alt{background:#060d1a;border:1px solid #1e293b;opacity:.85}
.ob-pick-header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:5px}
.ob-pick-rank-tag{font-size:10px;font-weight:800;color:#0f172a;background:#38bdf8;border-radius:3px;padding:1px 6px;letter-spacing:.04em}
.ob-rank-alt .ob-pick-rank-tag{background:#334155;color:#94a3b8}
.ob-pick-contract{font-size:13px;font-weight:700;display:flex;align-items:center;gap:5px}
.ob-pick-type{font-size:10px;font-weight:600;color:#4ade80;background:#0c2a1a;border-radius:2px;padding:1px 5px}
.ob-pick-exp{color:#94a3b8;font-size:12px;font-weight:400}
.ob-score-wrap{display:inline-flex;align-items:center;gap:4px;margin-left:auto}
.ob-bar-wrap{width:70px;height:6px;background:#1e293b;border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle}
.ob-bar-fill{height:100%;border-radius:3px;display:block}
.ob-bar-label{font-size:11px;font-weight:700;vertical-align:middle;white-space:nowrap}
.ob-pick-detail{display:flex;flex-wrap:wrap;gap:10px;font-size:12px}
.ob-mid{color:#38bdf8;font-weight:700}
.ob-iv-val{color:#c084fc}
.ob-delta-val{font-weight:700}
.ob-oi-val{color:#64748b}
.ob-mono-val{color:#94a3b8;font-size:11px}
.ob-liq-good{color:#4ade80;font-size:10px}
.ob-liq-warn{color:#f59e0b;font-size:10px}
.ob-liq-bad{color:#f87171;font-size:10px}
.ob-reason-line{font-size:11px;color:#7dd3fc;margin-top:5px;line-height:1.5;padding:4px 8px;background:#0c1422;border-left:2px solid #1d4e89;border-radius:0 4px 4px 0}
.ob-alts{font-size:10px;color:#475569;margin-top:4px;padding:2px 0}
.ob-warn-line{font-size:11px;color:#f59e0b;margin-top:4px}
.ob-earn-warn{font-size:11px;color:#f87171;background:#2a0a0a;border-radius:4px;padding:4px 8px;margin-bottom:6px}
.ob-msg{font-size:11px;color:#64748b;padding:2px 0}
/* Enhanced metrics row */
.ob-enhanced-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px;padding:4px 8px;background:#0c1422;border-radius:4px;border:1px solid #1e293b}
.ob-enh-item{font-size:11px;color:#94a3b8;display:inline-flex;align-items:center;gap:4px}
"""
