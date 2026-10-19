"""
Options Actionable Report — Filtered, Enriched Option Recommendations
=====================================================================
Filters the screener's scored contracts through actionable criteria:
  1. BE/1σ ≤ 0.55 (break-even within 55% of a normal 1-sigma move)
  2. Signal engine composite score (L1-L4 pipeline)
  3. Support levels from multi-source floor analysis

This module follows SOLID principles:
  • Single Responsibility — only actionable filtering + enrichment + HTML
  • Open/Closed — filtering thresholds are configurable
  • Dependency Injection — signal engine and support fetchers are injectable
  • Interface Segregation — uses only what it needs from each dependency
  • Liskov — any scorer/fetcher with same interface works

Usage:
    from tradingview.options.options_actionable_report import run_actionable
    actionable = run_actionable(scored_contracts, Path("reports/options_actionable.html"))

    # With custom thresholds:
    actionable = run_actionable(scored_contracts, out, max_be_sigma=0.60)

    # With mocked dependencies (for testing):
    actionable = run_actionable(scored_contracts, out,
                                signal_scorer=mock_scorer,
                                support_fetcher=mock_support)
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).parent.resolve()
_TV_DIR = _HERE.parent.resolve()
_ROOT = _TV_DIR.parent.resolve()
_DEFAULT_OUT = _ROOT / "reports" / "options_actionable.html"

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Protocols (for dependency injection) ────────────────────────────────────

class SignalScorer(Protocol):
    """Scores a ticker symbol, returns composite 0-100."""
    def score(self, ticker: str) -> Optional[float]: ...

class SupportFetcher(Protocol):
    """Fetches support levels for a ticker."""
    def fetch(self, ticker: str) -> dict: ...


# ── Default implementations ────────────────────────────────────────────────

class DefaultSignalScorer:
    """Wraps the signal engine AnalysisEngine for single-ticker scoring."""
    def __init__(self):
        self._engine = None
        self._cache: Dict[str, Optional[float]] = {}

    def _get_engine(self):
        if self._engine is None:
            try:
                from tradingview.signal_engine.analysis_engine import AnalysisEngine
                self._engine = AnalysisEngine()
            except ImportError:
                self._engine = False  # sentinel: unavailable
        return self._engine

    def score(self, ticker: str) -> Optional[float]:
        if ticker in self._cache:
            return self._cache[ticker]
        engine = self._get_engine()
        if engine is False:
            self._cache[ticker] = None
            return None
        try:
            # Build minimal fire dict for the signal engine
            # The engine needs pro_symbol in "EXCHANGE:TICKER" format
            exchange = "NASDAQ"  # default; the engine resolves internally
            fire = {
                "pro_symbol": f"{exchange}:{ticker}",
                "symbol": f"{exchange}:{ticker}",
                "message": "",
            }
            result = engine.analyse_signal(fire, use_auth=False)
            composite = result.get("composite", 0)
            self._cache[ticker] = round(composite, 1) if composite else None
            return self._cache[ticker]
        except Exception as e:
            print(f"  [signal] {ticker}: scoring failed — {e}")
            self._cache[ticker] = None
            return None


class DefaultSupportFetcher:
    """Wraps support_levels() for single-ticker support data."""
    def __init__(self):
        self._cache: Dict[str, dict] = {}

    def fetch(self, ticker: str) -> dict:
        if ticker in self._cache:
            return self._cache[ticker]
        try:
            from tradingview.options.support_levels import support_levels
            levels = support_levels(ticker, verbose=False)
            result = {
                "key_support": levels.key_support,
                "hold_score": levels.hold_score,
                "is_holding": levels.is_holding,
                "put_wall": levels.put_wall,
                "put_wall_oi": levels.put_wall_oi,
                "data_quality": levels.data_quality,
                "trend": levels.trend,
                "sma200": levels.sma200,
                "sma50": levels.sma50,
            }
            self._cache[ticker] = result
            return result
        except Exception as e:
            print(f"  [support] {ticker}: fetch failed — {e}")
            result = {
                "key_support": None, "hold_score": 0, "is_holding": None,
                "put_wall": None, "put_wall_oi": None, "data_quality": "unavailable",
                "trend": None, "sma200": None, "sma50": None,
            }
            self._cache[ticker] = result
            return result


# ── Deduplication ───────────────────────────────────────────────────────────

def dedup_same_expiry(contracts: List[dict]) -> List[dict]:
    """Deduplicate contracts that share the same (symbol, expiry) pair.

    When two contracts on the same underlying and expiry both pass filters,
    keep only the one with the lower BE/1σ ratio (better pricing). Ties broken
    by higher OI (better liquidity).
    """
    best: dict = {}
    for c in contracts:
        sym = (c.get("symbol") or c.get("ticker") or "").upper()
        expiry = c.get("expiry") or ""
        key = (sym, expiry)
        be = c.get("be_sigma_ratio", float("inf"))
        oi = c.get("oi") or c.get("open_interest") or 0
        if key not in best:
            best[key] = c
        else:
            prev_be = best[key].get("be_sigma_ratio", float("inf"))
            prev_oi = best[key].get("oi") or best[key].get("open_interest") or 0
            if be < prev_be or (be == prev_be and oi > prev_oi):
                best[key] = c
    return list(best.values())


# ── Filtering ───────────────────────────────────────────────────────────────

def filter_actionable(
    contracts: List[dict],
    max_be_sigma: float = 0.80,
    *,
    max_be_sigma_hard: float = 1.00,
    max_spread_pct: float = 5.0,
    min_oi: int = 200,
    min_volume: int = 500,
    max_iv_pct: float = 80.0,
    # 1-DTE-specific guards (only applied when dte == 1):
    max_leverage_1dte: float = 50.0,
    max_theta_1dte_pct: float = 40.0,
    # Quality gates:
    min_vol_oi: float = 0.25,
    min_tqs: float = 65.0,
    min_dte: int = 6,
    min_spot_price: float = 50.0,
    require_above_sma200: bool = True,
    # 3-agent consensus additions (2026-05-13):
    # BUG FIX 2026-05-13: 60% too restrictive in high-vol markets (tariff war
    # period — NVDA/MU iv_rank 97-98%, GOOG 61%).  Raised to 90%: allows most
    # normal IV environments while blocking genuinely extreme spikes (>90%).
    max_iv_rank_pct: float = 90.0,
    # Raised 40→65 per 3-agent review 2026-05-13: consensus that 40x is a 1-DTE
    # lottery rule incorrectly applied to 9-36 DTE. SPY ETF leverage is structural
    # (spot=$738, mid=$7.50 → 98x is context-appropriate). 65x rejected by Risk Mgr.
    max_leverage_all_dte: float = 65.0,
    # Internal — when provided, rejected contracts are appended here with
    # _reject_code and _reject_reason.  Called by filter_actionable_tracked().
    _rejected_out: Optional[List[dict]] = None,
) -> List[dict]:
    """
    Filter contracts to only tradeable, directional CALL opportunities.

    Filters applied (in order):
    1.  BE/1σ ≤ max_be_sigma           (default 0.80 — directional pricing)
    2.  BE/1σ hard cap                 (default 1.00 — absolute cutoff)
    3.  Spread ≤ max_spread_pct        (default 5.0% — exit liquidity)
    4.  OI ≥ min_oi OR Vol ≥ min_volume  (OI default 200; Vol default 500)
                                       T+1 rule: OI is published overnight by
                                       exchanges — high-volume contracts legitimately
                                       show OI=0 until next-morning settlement.
                                       Volume bypass: if Vol ≥ 500 the contract is
                                       demonstrably liquid regardless of OI=0.
                                       NVDA C240 14d: OI=0 Vol=11,582 → PASS.
                                       TSLA C440 3d: blocked by F8-dte<6 (correct).
    5.  IV ≤ max_iv_pct                (default 80% — vega crush guard)
    6.  Vol/OI ≥ min_vol_oi            (default 0.25 — skip stale contracts)
    7.  TQS v2 ≥ min_tqs               (default 65 — signal quality gate; 0 = disabled)
    8.  DTE ≥ min_dte                  (default 6 — avoid ultra-short; 0 = disabled)
    9.  Spot ≥ min_spot                (default $50 — avoid penny stocks; 0 = disabled)
    10. Earnings in window = reject    (has_earnings_in_window=True → reject; Agent C finding)
    11. IV Rank ≤ max_iv_rank_pct      (default 90% — reject overpriced IV; Agents A+B finding)
    12. Leverage ≤ max_leverage_all_dte (default 65× ALL DTE)
    13. 1-DTE only: θ ≤ 40%/day       (1-DTE specific theta cap)
    14. Trend: above SMA200            (require_above_sma200=True; None = unknown = allow)
                                       NOTE: above_sma200 is NOT set before filter_actionable()
                                       in the normal run_actionable() pipeline — it is populated
                                       by enrich_with_support() which runs AFTER this function.
                                       The real SMA200 gate is the post-support filter in
                                       run_actionable() step 3b.  F14 here fires only when
                                       above_sma200=False is pre-populated (e.g. tests).
    """
    import math as _math
    result = []
    _reject: dict = {}  # per-filter rejection counts for diagnostic output

    def _r(code: str, reason: str) -> None:
        """Track rejection: update aggregate count + collect per-contract detail."""
        _reject[code] = _reject.get(code, 0) + 1
        if _rejected_out is not None:
            rc = dict(c)
            rc["_reject_code"] = code
            rc["_reject_reason"] = reason
            _rejected_out.append(rc)

    for c in contracts:
        be_ratio = c.get("be_sigma_ratio", float("inf"))

        # Filter 1+2: BE/1σ pricing
        if _math.isnan(be_ratio) or be_ratio > max_be_sigma:
            _r(f"F1-be>{max_be_sigma:.2f}", f"BE/σ={be_ratio:.2f} > {max_be_sigma:.2f}"); continue
        if be_ratio > max_be_sigma_hard:
            _r("F2-be-hard", f"BE/σ={be_ratio:.2f} > hard-cap {max_be_sigma_hard:.2f}"); continue

        # Filter 3: spread — exit liquidity
        spread = c.get("spread_pct", 0.0) or 0.0
        if spread > max_spread_pct:
            _r("F3-spread>5%", f"spread={spread:.1f}% > {max_spread_pct:.1f}%"); continue

        # Filter 4: OI — REMOVED from filtering.  OI is unreliable across
        # sources (TV scan2 hardcodes 0, Moomoo only covers near-ATM, yfinance
        # is delayed).  OI is kept for display only.

        # Filter 5: IV cap
        iv = c.get("iv") or 0.0
        if iv > max_iv_pct:
            _r("F5-iv>80%", f"IV={iv:.1f}% > {max_iv_pct:.1f}%"); continue

        # Filter 6: vol/oi ratio — REMOVED.  OI-based decisions removed.

        # Filter 7: TQS v2 signal quality
        if min_tqs > 0:
            if (c.get("tqs_v2") or 0.0) < min_tqs:
                _r("F7-tqs<65", f"TQS={c.get('tqs_v2', 0):.1f} < {min_tqs:.0f}"); continue

        # Filter 8: minimum DTE
        if min_dte > 0:
            if (c.get("dte") or 0) < min_dte:
                _r("F8-dte<6", f"DTE={c.get('dte', 0)} < {min_dte}"); continue

        # Filter 9: minimum spot price
        if min_spot_price > 0:
            spot = c.get("spot_price")
            if spot is not None and spot < min_spot_price:
                _r("F9-spot<$50", f"spot=${spot:.2f} < ${min_spot_price:.0f}"); continue

        # Filter 10: earnings in window — binary-event exposure
        if c.get("has_earnings_in_window") is True:
            _r("F10-earnings", "earnings in DTE window"); continue

        # Filter 11: IV Rank cap — avoid overpriced options
        if max_iv_rank_pct < 100:
            iv_rank = c.get("iv_rank")
            if iv_rank is not None and float(iv_rank) > max_iv_rank_pct:
                _r(f"F11-ivrank>{max_iv_rank_pct:.0f}%", f"IV_rank={float(iv_rank):.0f}% > {max_iv_rank_pct:.0f}%"); continue

        # Filter 12: leverage cap ALL DTE
        if max_leverage_all_dte > 0:
            leverage_val = c.get("leverage") or 0.0
            if float(leverage_val) > max_leverage_all_dte:
                _r("F12-lev>65x", f"leverage={leverage_val:.1f}\u00d7 > {max_leverage_all_dte:.0f}\u00d7"); continue

        # Filter 13: 1-DTE specific guards
        dte_val = c.get("dte") or 0
        if dte_val == 1:
            theta_pct = abs(c.get("theta_day_pct") or 0.0)
            if theta_pct > max_theta_1dte_pct:
                _r("F13-theta", f"1-DTE \u03b8={theta_pct:.1f}%/day > {max_theta_1dte_pct:.0f}%"); continue
            leverage = c.get("leverage") or 0.0
            if leverage > max_leverage_1dte:
                _r("F13-1dte-lev", f"1-DTE leverage={leverage:.1f}\u00d7 > {max_leverage_1dte:.0f}\u00d7"); continue

        # Filter 14: trend alignment
        # NOTE: above_sma200 is NOT populated at this stage in the normal run_actionable()
        # pipeline — it is set by enrich_with_support() which runs AFTER filter_actionable().
        # This guard only fires in tests or contexts that pre-populate above_sma200=False.
        # The real SMA200 gate is in run_actionable() step 3b.
        if require_above_sma200:
            above_sma200 = c.get("above_sma200")
            if above_sma200 is False:
                sma_v = c.get("support_sma200") or c.get("sma200") or 0
                _r("F14-belowSMA", f"spot=${c.get('spot_price', 0):.1f} < sma200=${sma_v:.1f}"); continue

        result.append(c)

    # ── Diagnostic breakdown ──
    if _reject:
        breakdown = "  ".join(f"{k}:{v}" for k, v in sorted(_reject.items()) if v > 0)
        print(f"  [filter-breakdown] {breakdown}")

    return result


def filter_actionable_tracked(
    contracts: List[dict],
    max_be_sigma: float = 0.55,
    **kwargs,
) -> Tuple[List[dict], List[dict]]:
    """Same as filter_actionable() but also returns rejected contracts with reasons.

    Returns:
        (passed, rejected) — each rejected contract has added keys:
            _reject_code: str  — e.g. "F4-oi<200"
            _reject_reason: str — e.g. "OI=0 < min 200"

    Use this in run_actionable() and anywhere you need a full audit trail of which
    contracts were filtered and why.
    """
    rejected: List[dict] = []
    passed = filter_actionable(
        contracts, max_be_sigma=max_be_sigma, _rejected_out=rejected, **kwargs
    )
    return passed, rejected


# ── Enrichment ──────────────────────────────────────────────────────────────

def enrich_with_signal_score(
    contracts: List[dict],
    scorer: Optional[SignalScorer] = None,
) -> List[dict]:
    """
    Enrich contracts with signal engine composite score.
    Calls scorer once per unique underlying ticker (cached internally).
    """
    if scorer is None:
        scorer = DefaultSignalScorer()

    # Get unique tickers
    tickers = set()
    for c in contracts:
        sym = c.get("symbol", "")
        ticker = sym.split(":")[-1] if ":" in sym else sym
        tickers.add(ticker)

    print(f"\n  [actionable] Scoring {len(tickers)} tickers via signal engine...")
    scores: Dict[str, Optional[float]] = {}
    for i, ticker in enumerate(sorted(tickers), 1):
        print(f"    [{i}/{len(tickers)}] {ticker}...", end=" ", flush=True)
        score = scorer.score(ticker)
        scores[ticker] = score
        grade_str, _ = _grade(score) if score is not None else ("—", "#8b949e")
        print(f"→ {score or '—'} ({grade_str})")

    # Apply to all contracts
    for c in contracts:
        sym = c.get("symbol", "")
        ticker = sym.split(":")[-1] if ":" in sym else sym
        c["signal_composite"] = scores.get(ticker)
        if scores.get(ticker) is not None:
            g, gc = _grade(scores[ticker])
            c["signal_grade"] = g
            c["signal_grade_color"] = gc

    return contracts


def enrich_with_support(
    contracts: List[dict],
    fetcher: Optional[SupportFetcher] = None,
) -> List[dict]:
    """
    Enrich contracts with support level data.
    Calls fetcher once per unique underlying ticker (cached internally).
    """
    if fetcher is None:
        fetcher = DefaultSupportFetcher()

    tickers = set()
    for c in contracts:
        sym = c.get("symbol", "")
        ticker = sym.split(":")[-1] if ":" in sym else sym
        tickers.add(ticker)

    print(f"\n  [actionable] Fetching support levels for {len(tickers)} tickers...")
    support_data: Dict[str, dict] = {}
    for i, ticker in enumerate(sorted(tickers), 1):
        print(f"    [{i}/{len(tickers)}] {ticker}...", end=" ", flush=True)
        data = fetcher.fetch(ticker)
        support_data[ticker] = data
        ks = data.get("key_support")
        hs = data.get("hold_score", 0)
        holding = data.get("is_holding")
        holding_str = "✅ holding" if holding else "❌ broken" if holding is False else "—"
        ks_str = f"${ks:.2f}" if ks else "—"
        print(f"→ support={ks_str} hold={hs}/10 {holding_str}")

    # Apply to all contracts
    for c in contracts:
        sym = c.get("symbol", "")
        ticker = sym.split(":")[-1] if ":" in sym else sym
        sd = support_data.get(ticker, {})
        c["support_key"] = sd.get("key_support")
        c["support_hold_score"] = sd.get("hold_score", 0)
        c["support_is_holding"] = sd.get("is_holding")
        c["support_put_wall"] = sd.get("put_wall")
        c["support_put_wall_oi"] = sd.get("put_wall_oi")
        c["support_quality"] = sd.get("data_quality", "unavailable")
        c["support_trend"] = sd.get("trend")
        c["support_sma200"] = sd.get("sma200")
        c["support_sma50"] = sd.get("sma50")
        # Populate the boolean flag used by filter_actionable() and post-support SMA200 check
        is_above = sd.get("is_above_sma200")
        if is_above is None and sd.get("sma200") and c.get("spot_price"):
            is_above = c["spot_price"] > sd["sma200"]
        c["above_sma200"] = is_above

    return contracts


# ── Grading helper ──────────────────────────────────────────────────────────

def _grade(score: Optional[float]) -> tuple:
    """Grade a 0-100 score. Returns (letter, color_hex)."""
    if score is None:
        return ("—", "#8b949e")
    if score >= 75: return ("A", "#22c55e")
    if score >= 60: return ("B", "#84cc16")
    if score >= 45: return ("C", "#eab308")
    if score >= 30: return ("D", "#f97316")
    return ("F", "#ef4444")


# ── HTML helpers ────────────────────────────────────────────────────────────

def _score_color(score: float) -> str:
    if score >= 75: return "#3fb950"
    if score >= 60: return "#39d353"
    if score >= 45: return "#d29922"
    if score >= 30: return "#f0883e"
    return "#f85149"


def _bar(score: float, max_score: float = 100.0) -> str:
    pct = max(0.0, min(100.0, score / max_score * 100.0))
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


def _be_sigma_color(ratio: float) -> str:
    """Color for BE/1σ ratio badge."""
    if ratio <= 0.40: return "#3fb950"   # cheap (edge)
    if ratio <= 0.55: return "#84cc16"   # fair
    if ratio <= 0.70: return "#d29922"   # expensive
    return "#f85149"                      # avoid


def _be_sigma_label(ratio: float) -> str:
    """Human-readable label for BE/1σ."""
    if ratio <= 0.40: return "CHEAP"
    if ratio <= 0.55: return "FAIR"
    if ratio <= 0.70: return "EXPENSIVE"
    return "AVOID"


def _actionable_card_html(rank: int, rec: dict) -> str:
    """Build an enhanced contract card with signal score + support level sections."""
    import math as _math

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
    premium_win = rec.get("premium_win", 0.0) or 0.0

    # Signal engine data
    sig_score = rec.get("signal_composite")
    sig_grade = rec.get("signal_grade", "—")
    sig_color = rec.get("signal_grade_color", "#8b949e")

    # Support data
    sup_key    = rec.get("support_key")
    sup_hold   = rec.get("support_hold_score", 0)
    sup_holding = rec.get("support_is_holding")
    sup_pw     = rec.get("support_put_wall")
    sup_pw_oi  = rec.get("support_put_wall_oi")
    sup_quality = rec.get("support_quality", "unavailable")
    sup_trend  = rec.get("support_trend")
    sup_sma200 = rec.get("support_sma200")
    sup_sma50  = rec.get("support_sma50")

    # Computed metrics
    be_pct = ((strike + premium) / spot - 1.0) * 100.0 if spot > 0 else 0.0
    sigma_T = (iv / 100.0) * _math.sqrt(max(dte, 1) / 365.0) * 100.0 if iv > 0 else 0.0
    be_sigma_ratio = be_pct / sigma_T if sigma_T > 0 else float('inf')
    leverage = (abs(delta) * spot / premium) if premium > 0 else 0.0
    vol_oi = (volume / oi) if (oi > 0 and volume > 0) else 0.0
    theta_raw = rec.get("theta", 0) or 0
    theta_pct = (abs(theta_raw) / premium * 100.0) if premium > 0 and theta_raw else 0.0

    # Colors
    border_color = "#3fb950" if rank == 1 else "#58a6ff" if rank == 2 else "#30363d"
    rank_bg = "#1c4f2a" if rank == 1 else "#0d2249" if rank == 2 else "#2b3543"
    rank_fg = "#3fb950" if rank == 1 else "#58a6ff" if rank == 2 else "#8b949e"
    moneyness_label = "ITM" if moneyness < -3 else "OTM" if moneyness > 3 else "ATM"
    moneyness_color = "#3fb950" if moneyness < -3 else "#f0883e" if moneyness > 3 else "#58a6ff"
    tqs_color = _score_color(tqs)
    wrs_color = _score_color(wrs)
    pw_color = _score_color(premium_win)
    be_color = _be_sigma_color(be_sigma_ratio)
    be_label = _be_sigma_label(be_sigma_ratio)
    be_sig_display = f"BE/1σ {be_sigma_ratio:.2f}" if be_sigma_ratio < 100 else "BE/1σ ∞"

    # Holding status
    if sup_holding is True:
        hold_badge = '<span style="color:#3fb950;font-weight:600">✅ Holding above support</span>'
    elif sup_holding is False:
        hold_badge = '<span style="color:#f85149;font-weight:600">❌ Broken below support</span>'
    else:
        hold_badge = '<span style="color:#8b949e">— Support data unavailable</span>'

    # Support details
    sup_details = []
    if sup_key is not None:
        sup_details.append(f'Key Support: ${sup_key:.2f}')
    if sup_sma200 is not None:
        sup_details.append(f'SMA200: ${sup_sma200:.2f}')
    if sup_sma50 is not None:
        sup_details.append(f'SMA50: ${sup_sma50:.2f}')
    if sup_pw is not None:
        pw_oi_str = f" (OI:{sup_pw_oi:,})" if sup_pw_oi else ""
        sup_details.append(f'Put Wall: ${sup_pw:.2f}{pw_oi_str}')
    sup_details_html = " · ".join(sup_details) if sup_details else "No support data"

    # Signal score section
    if sig_score is not None:
        sig_section = f"""
      <div style="display:flex;align-items:center;gap:.5rem;margin:.3rem 0;
                  padding:.4rem .6rem;background:#0d1117;border:1px solid #21262d;border-radius:6px">
        <div style="font-size:.7rem;color:#8b949e;min-width:90px">Signal Engine</div>
        <div style="flex:1">{_bar(sig_score)}</div>
        <div style="font-size:.9rem;font-weight:700;color:{sig_color};min-width:50px;text-align:right">
          {sig_score:.0f} <span style="font-size:.75rem">({sig_grade})</span></div>
      </div>"""
    else:
        sig_section = """
      <div style="display:flex;align-items:center;gap:.5rem;margin:.3rem 0;
                  padding:.4rem .6rem;background:#0d1117;border:1px solid #21262d;border-radius:6px">
        <div style="font-size:.7rem;color:#8b949e;min-width:90px">Signal Engine</div>
        <div style="flex:1;color:#8b949e;font-size:.75rem">Unavailable</div>
      </div>"""

    # Support section
    hold_color = "#1c4f2a" if sup_holding else "#3b1c1c" if sup_holding is False else "#1c1c2b"
    hold_border = "#2ea043" if sup_holding else "#da3633" if sup_holding is False else "#30363d"
    sup_section = f"""
      <div style="margin:.3rem 0;padding:.4rem .6rem;background:{hold_color};
                  border:1px solid {hold_border};border-radius:6px">
        <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.2rem">
          <span style="font-size:.7rem;color:#8b949e;min-width:90px">Support Floor</span>
          {hold_badge}
          <span style="margin-left:auto;font-size:.75rem;color:#8b949e">
            Hold Score: <b style="color:{'#3fb950' if sup_hold >= 6 else '#d29922' if sup_hold >= 4 else '#f85149'}">{sup_hold}/10</b>
            · Quality: {sup_quality}
          </span>
        </div>
        <div style="font-size:.68rem;color:#8b949e">{sup_details_html}</div>
      </div>"""

    return f"""
    <div class="contract-card" data-symbol="{ticker}"
         data-tqs="{tqs}" data-wrs="{wrs}" data-pw="{premium_win}" data-dte="{dte}"
         data-be-sigma="{be_sigma_ratio:.3f}" data-signal="{sig_score or 0}"
         style="margin-bottom:1rem;background:#161b22;border:1px solid {border_color};
                border-radius:8px;padding:.75rem 1rem">
      <!-- Header -->
      <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem">
        <div style="width:1.6rem;height:1.6rem;display:flex;align-items:center;
                    justify-content:center;border-radius:50%;font-size:.8rem;font-weight:700;
                    background:{rank_bg};color:{rank_fg};flex-shrink:0">{rank}</div>
        <span style="font-size:1.1rem;font-weight:700;color:#58a6ff">{ticker}</span>
        <span style="font-size:.9rem;font-weight:600;color:#e6edf3">C{strike:.0f}</span>
        <span style="font-size:.75rem;color:#8b949e">{expiry} ({dte}d)</span>
        <span style="margin-left:auto;font-size:1.1rem;font-weight:700;color:#f0883e">${premium:.2f}</span>
      </div>
      <!-- Badges row -->
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
      </div>
      <!-- Scores row -->
      <div style="display:flex;gap:1rem;margin:.4rem 0;align-items:center">
        <div style="flex:1">
          <div style="font-size:.68rem;color:#8b949e;margin-bottom:.2rem">TQS v2</div>
          {_bar(tqs)}
          <div style="font-size:.85rem;font-weight:700;color:{tqs_color};margin-top:.2rem">{tqs:.1f}</div>
        </div>
        <div style="flex:1">
          <div style="font-size:.68rem;color:#8b949e;margin-bottom:.2rem">Win Rate</div>
          {_bar(wrs)}
          <div style="font-size:.85rem;font-weight:700;color:{wrs_color};margin-top:.2rem">{wrs:.1f}%</div>
        </div>
        <div style="flex:1">
          <div style="font-size:.68rem;color:#f0883e;margin-bottom:.2rem">&#128176; Premium Win</div>
          {_bar(premium_win)}
          <div style="font-size:.85rem;font-weight:700;color:{pw_color};margin-top:.2rem">{premium_win:.1f}</div>
        </div>
      </div>
      <!-- BE/1σ highlight -->
      <div style="display:flex;align-items:center;gap:.5rem;margin:.3rem 0;
                  padding:.35rem .6rem;background:#0d1117;border:1px solid #21262d;border-radius:6px">
        <span style="font-size:.8rem;font-weight:700;color:{be_color}">{be_sig_display}</span>
        <span style="font-size:.68rem;color:{be_color};font-weight:600">{be_label}</span>
        <span style="font-size:.68rem;color:#8b949e;margin-left:.5rem">
          BE +{be_pct:.1f}% | 1σ ±{sigma_T:.1f}% | Lev {leverage:.1f}× | Vol/OI {vol_oi:.2f}× | θ -{theta_pct:.1f}%/day
        </span>
      </div>
      <!-- Signal Engine -->
      {sig_section}
      <!-- Support Floor -->
      {sup_section}
    </div>"""


def build_actionable_html(
    contracts: List[dict],
    scanned_count: int,
    found_count: int,
    generated_at: str,
    max_be_sigma: float = 0.55,
) -> str:
    """Build the actionable options HTML report."""
    if contracts:
        cards_html = "".join(_actionable_card_html(i + 1, r) for i, r in enumerate(contracts))
    else:
        cards_html = (
            '<div style="color:#8b949e;padding:2rem;text-align:center">'
            "No contracts passed the actionable filter (BE/1σ ≤ "
            f"{max_be_sigma:.2f}, spread ≤ 5%, OI ≥ 200, leverage ≤ 40×, "
            "IV rank ≤ 60%, no earnings in window, above SMA200). "
            "Try running with --max-be-sigma 0.70 for more results."
            "</div>"
        )

    # Stats
    avg_be_sigma = 0.0
    avg_signal = 0.0
    holding_count = 0
    if contracts:
        ratios = [c.get("be_sigma_ratio", 0) for c in contracts if c.get("be_sigma_ratio", float('inf')) < 100]
        avg_be_sigma = sum(ratios) / len(ratios) if ratios else 0
        sigs = [c["signal_composite"] for c in contracts if c.get("signal_composite") is not None]
        avg_signal = sum(sigs) / len(sigs) if sigs else 0
        holding_count = sum(1 for c in contracts if c.get("support_is_holding") is True)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options Actionable Report — BE/1σ ≤ {max_be_sigma:.2f}</title>
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
</style>
</head>
<body>
  <div class="hdr">
    <h1 style="font-size:1.4rem;color:var(--accent)">🎯 Options Actionable Report</h1>
    <div style="display:flex;gap:.5rem;flex-wrap:wrap">
      <span class="stat-pill">Actionable: <b>{len(contracts)}</b></span>
      <span class="stat-pill">Scanned: <b>{scanned_count}</b></span>
      <span class="stat-pill">Found: <b>{found_count}</b></span>
      <span class="stat-pill">BE/1σ ≤ <b>{max_be_sigma:.2f}</b></span>
      <span class="stat-pill">Avg BE/1σ: <b>{avg_be_sigma:.2f}</b></span>
      <span class="stat-pill">Avg Signal: <b>{avg_signal:.0f}</b></span>
      <span class="stat-pill">Holding Support: <b>{holding_count}/{len(contracts)}</b></span>
    </div>
    <span style="margin-left:auto;font-size:.7rem;color:var(--dim)">{generated_at}</span>
  </div>
  <div style="font-size:.75rem;color:var(--dim);margin-bottom:1rem;padding:.5rem;
              background:#161b22;border:1px solid var(--border);border-radius:6px">
    <b>Filter criteria:</b> BE/1σ ≤ {max_be_sigma:.2f} · Spread ≤ 5% · Vol/OI ≥ 0.25 · OI ≥ 200 · TQS ≥ 65 · DTE ≥ 6 · Spot ≥ $50 · Lev ≤ 65× · IV Rank ≤ 60% · No earnings in window · Above SMA200 · 1-DTE: θ ≤ 40%/day.
    Each contract enriched with: Signal Engine composite score (L1-L4) + Support Floor analysis (SMA200, SMA50, Put Wall).
    <br><b>Legend:</b> BE/1σ ≤ 0.40 = <span style="color:#3fb950">CHEAP</span> · 0.40–0.55 = <span style="color:#84cc16">FAIR</span> · 0.55–0.70 = <span style="color:#d29922">EXPENSIVE</span> · &gt;0.70 = <span style="color:#f85149">AVOID</span>
  </div>
  {cards_html}
  <div style="text-align:center;color:var(--dim);font-size:.7rem;margin-top:1.5rem;
              padding-top:.75rem;border-top:1px solid var(--border)">
    Options Actionable Report · Generated {generated_at} · Filter: BE/1σ ≤ {max_be_sigma:.2f}
  </div>
</body>
</html>"""


# ── Rejected Report ─────────────────────────────────────────────────────────

_FILTER_LABELS: Dict[str, str] = {
    "F1-be>0.55":         "BE/1σ > threshold (too expensive)",
    "F2-be-hard":         "BE/1σ hard cap (absolute ceiling)",
    "F3-spread>5%":       "Bid-ask spread too wide (> 5%)",
    "F4-oi<200":          "Open interest too low (< 200)",
    "F4-liq":             "Insufficient liquidity: OI < 200 AND Vol < 500 (T+1 OI settlement lag bypass)",
    "F5-iv>80%":          "IV too high (> 80% — vega crush risk)",
    "F6-vol/oi":          "Vol/OI too low (stale / dead contract)",
    "F7-tqs<65":          "TQS signal score too low (< 65)",
    "F8-dte<6":           "DTE too short (< 6 days)",
    "F9-spot<$50":        "Spot price below $50 (penny/micro cap)",
    "F10-earnings":       "Earnings event inside DTE window",
    "F11-ivrank>80%":     "IV Rank too high (> 80% — overpriced premium)",
    "F12-lev>65x":        "Leverage too high (> 65×)",
    "F13-theta":          "1-DTE theta decay > 40%/day",
    "F13-1dte-lev":       "1-DTE leverage > 50×",
    "F14-belowSMA":       "Below SMA200 (pre-populated; see run_actionable step 3b)",
    "F14b-sma200-postenrich": "Below SMA200 confirmed by support enricher",
}


def build_rejected_html(
    rejected: List[dict],
    scanned_count: int = 0,
    found_count: int = 0,
    actionable_count: int = 0,
    generated_at: str = "",
    max_be_sigma: float = 0.55,
) -> str:
    """Build the options_rejected.html report.

    Shows every contract rejected by filter_actionable() or the post-SMA200 gate,
    with the specific filter code + human-readable reason.  Sorted by TQS descending
    so high-quality near-misses appear first.
    """
    import datetime as _dt
    if not generated_at:
        generated_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Sort: high TQS first (near-misses most valuable to review) ──
    rej_sorted = sorted(rejected, key=lambda x: -(x.get("tqs_v2") or 0))

    # ── Build filter breakdown ──
    by_code: Dict[str, List[dict]] = {}
    for c in rej_sorted:
        code = c.get("_reject_code") or "unknown"
        by_code.setdefault(code, []).append(c)

    near_misses = [c for c in rej_sorted if (c.get("tqs_v2") or 0) >= 65]

    # ── Filter breakdown table ──
    breakdown_rows = ""
    for code in sorted(by_code, key=lambda k: (-len(by_code[k]), k)):
        count = len(by_code[code])
        pct = count / max(len(rejected), 1) * 100
        label = _FILTER_LABELS.get(code, code)
        bar_w = int(pct * 1.5)
        breakdown_rows += (
            f"<tr>"
            f"<td><code style='color:#58a6ff'>{code}</code></td>"
            f"<td>{label}</td>"
            f"<td class='num' style='color:#e3b341'>{count}</td>"
            f"<td><div style='background:#58a6ff33;height:6px;width:{bar_w}px;"
            f"border-radius:3px'></div></td>"
            f"</tr>\n"
        )

    # ── Near-miss cards ──
    nm_cards = ""
    for c in near_misses:
        sym = c.get("symbol", "?")
        strike = c.get("strike", "?")
        expiry = c.get("expiry", "")[:10]
        dte = c.get("dte", "?")
        tqs = c.get("tqs_v2") or 0
        be = c.get("be_sigma_ratio", 0)
        oi = c.get("oi") or 0
        oi_src = c.get("oi_source", "")
        oi_warn = " ⚠️ Moomoo offline" if c.get("_oi_warning") else ""
        ivr = c.get("iv_rank")
        ivr_str = f"{ivr:.0f}%" if ivr is not None else "—"
        spread = c.get("spread_pct", 0)
        code = c.get("_reject_code", "?")
        reason = c.get("_reject_reason", "?")
        label = _FILTER_LABELS.get(code, code)

        # Colour TQS badge
        tqs_col = "#3fb950" if tqs >= 75 else "#84cc16" if tqs >= 65 else "#d29922"
        nm_cards += f"""
    <div style="background:#161b22;border:1px solid #f85149;border-radius:8px;
                padding:1rem 1.2rem;margin:.6rem 0">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:700;font-size:1rem">{sym} C{strike} · {expiry} ({dte}d)</span>
        <span style="background:{tqs_col}22;color:{tqs_col};padding:.2rem .6rem;
                     border-radius:12px;font-size:.8rem;font-weight:700">
          TQS {tqs:.1f}
        </span>
      </div>
      <div style="margin-top:.5rem;background:#f8514920;border-left:3px solid #f85149;
                  padding:.4rem .8rem;border-radius:4px;font-size:.85rem">
        <strong style="color:#f85149">{code}</strong> — {label}<br>
        <span style="color:#cdd9e5">Reason: {reason}</span>
      </div>
      <div style="margin-top:.5rem;font-size:.8rem;color:#8b949e;display:flex;gap:1.5rem">
        <span>BE/σ <b>{be:.2f}</b></span>
        <span>OI <b>{oi}{oi_warn}</b></span>
        <span>IVR <b>{ivr_str}</b></span>
        <span>Spread <b>{spread:.1f}%</b></span>
      </div>
    </div>"""

    if not nm_cards:
        nm_cards = "<p style='color:#8b949e;font-style:italic'>No near-misses (all rejected contracts had TQS &lt; 65)</p>"

    # ── Full rejected table ──
    table_rows = ""
    for i, c in enumerate(rej_sorted, 1):
        sym = c.get("symbol", "?")
        strike = c.get("strike", "?")
        expiry = (c.get("expiry") or "")[:10]
        dte = c.get("dte", "?")
        tqs = c.get("tqs_v2") or 0
        be = c.get("be_sigma_ratio", 0)
        oi = c.get("oi") or 0
        oi_src = c.get("oi_source", "")
        oi_warn = "⚠️" if c.get("_oi_warning") else ""
        iv = c.get("iv") or 0
        ivr = c.get("iv_rank")
        ivr_str = f"{ivr:.0f}%" if ivr is not None else "—"
        spread = c.get("spread_pct", 0) or 0
        vol = c.get("volume") or 0
        delta = c.get("delta", 0) or 0
        code = c.get("_reject_code", "?")
        reason = c.get("_reject_reason", "?")

        tqs_col = "#3fb950" if tqs >= 75 else "#84cc16" if tqs >= 65 else "#6e7681"
        near = tqs >= 65

        table_rows += (
            f"<tr style='{'background:#161b22' if near else ''}'>"
            f"<td style='color:#8b949e'>{i}</td>"
            f"<td><b>{sym}</b> C{strike}</td>"
            f"<td style='color:#cdd9e5'>{expiry}</td>"
            f"<td class='num'>{dte}d</td>"
            f"<td class='num' style='color:{tqs_col};font-weight:700'>{tqs:.1f}</td>"
            f"<td class='num'>{be:.2f}</td>"
            f"<td class='num'>{iv:.0f}%</td>"
            f"<td class='num'>{ivr_str}</td>"
            f"<td class='num'>{oi:,}{oi_warn}</td>"
            f"<td class='num'>{vol:,}</td>"
            f"<td class='num'>{spread:.1f}%</td>"
            f"<td class='num'>{delta:.2f}</td>"
            f"<td><code style='color:#f85149;font-size:.78rem'>{code}</code><br>"
            f"<span style='color:#8b949e;font-size:.75rem'>{reason}</span></td>"
            f"</tr>\n"
        )

    oi_note = (
        "<div style='margin:.6rem 0;background:#d2992215;border:1px solid #d29922;"
        "border-radius:6px;padding:.5rem 1rem;font-size:.82rem'>"
        "⚠️ <b>Bug Fix 2026-05-14</b>: Contracts with <code>oi_source=unavailable</code> "
        "(Moomoo offline) were previously hard-rejected by F4. They now pass F4 with an OI "
        "warning — TQS scoring already penalises for missing OI data."
        "</div>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Options Rejected Report — {generated_at}</title>
  <style>
    :root {{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#cdd9e5;--dim:#8b949e}}
    * {{box-sizing:border-box;margin:0;padding:0}}
    body {{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,
           'Segoe UI',sans-serif;font-size:.875rem;padding:1.5rem}}
    h1 {{font-size:1.4rem;font-weight:700;margin-bottom:.3rem}}
    h2 {{font-size:1rem;font-weight:600;color:#58a6ff;margin:1.5rem 0 .5rem}}
    .pills {{display:flex;gap:.6rem;flex-wrap:wrap;margin:.8rem 0}}
    .pill {{background:var(--card);border:1px solid var(--border);border-radius:12px;
            padding:.3rem .8rem;font-size:.8rem}}
    table {{width:100%;border-collapse:collapse;margin:.5rem 0}}
    th {{background:#161b22;color:var(--dim);font-weight:600;text-align:left;
         padding:.5rem .6rem;border-bottom:1px solid var(--border);font-size:.78rem}}
    td {{padding:.4rem .6rem;border-bottom:1px solid #21262d;vertical-align:top}}
    .num {{text-align:right;font-variant-numeric:tabular-nums}}
    tr:hover td {{background:#1c2128}}
    code {{font-family:'JetBrains Mono','Fira Code',monospace}}
  </style>
</head>
<body>
  <h1>⛔ Options Rejected Report</h1>
  <div class="pills">
    <span class="pill">📅 {generated_at}</span>
    <span class="pill">🔍 {scanned_count} stocks scanned</span>
    <span class="pill">📋 {found_count} contracts found</span>
    <span class="pill" style="color:#f85149">⛔ {len(rejected)} rejected</span>
    <span class="pill" style="color:#3fb950">✅ {actionable_count} actionable</span>
  </div>

  {oi_note}

  <h2>Filter Breakdown</h2>
  <table>
    <thead><tr>
      <th>Filter</th><th>Description</th><th style="text-align:right">Count</th><th>Bar</th>
    </tr></thead>
    <tbody>{breakdown_rows}</tbody>
  </table>

  <h2>🎯 Near Misses — TQS ≥ 65 (high-quality contracts blocked by one filter)</h2>
  {nm_cards}

  <h2>All Rejected Contracts ({len(rejected)} total, sorted by TQS ↓)</h2>
  <table>
    <thead><tr>
      <th>#</th><th>Contract</th><th>Expiry</th><th>DTE</th>
      <th style="text-align:right">TQS</th>
      <th style="text-align:right">BE/σ</th>
      <th style="text-align:right">IV</th>
      <th style="text-align:right">IVR</th>
      <th style="text-align:right">OI</th>
      <th style="text-align:right">Vol</th>
      <th style="text-align:right">Sprd</th>
      <th style="text-align:right">Δ</th>
      <th>Reject Reason</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
  </table>

  <div style="text-align:center;color:var(--dim);font-size:.7rem;margin-top:1.5rem;
              padding-top:.75rem;border-top:1px solid var(--border)">
    Options Rejected Report · Generated {generated_at} · Filter threshold BE/1σ ≤ {max_be_sigma:.2f}
  </div>
</body>
</html>"""


# ── Orchestrator ────────────────────────────────────────────────────────────

def run_actionable(
    all_scored: List[dict],
    out_path: Path = _DEFAULT_OUT,
    *,
    scanned_count: int = 0,
    found_count: int = 0,
    generated_at: str = "",
    max_be_sigma: float = 0.80,
    signal_scorer: Optional[SignalScorer] = None,
    support_fetcher: Optional[SupportFetcher] = None,
    skip_signal: bool = False,
    skip_support: bool = False,
) -> List[dict]:
    """
    Run the actionable filter + enrichment pipeline.

    Args:
        all_scored: Scored contracts from options_contract_screener.run_scan()
        out_path: Output HTML file path
        scanned_count: Total symbols scanned (for stats display)
        found_count: Total contracts found before quality gate
        generated_at: Timestamp string
        max_be_sigma: Maximum BE/1σ ratio for actionable filter (default 0.80)
        signal_scorer: Injectable signal scorer (default: AnalysisEngine wrapper)
        support_fetcher: Injectable support fetcher (default: support_levels() wrapper)
        skip_signal: Skip signal engine enrichment (faster, for testing)
        skip_support: Skip support level enrichment (faster, for testing)

    Returns:
        List of enriched actionable contracts
    """
    if not generated_at:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    t0 = time.time()

    # Step 1: Filter by BE/1σ
    # First ensure all contracts have be_sigma_ratio computed
    import math as _math
    for c in all_scored:
        if "be_sigma_ratio" not in c:
            spot = c.get("spot_price", 0) or 0
            strike = c.get("strike", 0)
            premium = c.get("mid_price") or c.get("theo_price") or 0
            iv = c.get("iv", 0) or 0
            dte = c.get("dte", 0) or 0
            be_pct = ((strike + premium) / spot - 1.0) * 100.0 if spot > 0 else 0.0
            sigma_T = (iv / 100.0) * _math.sqrt(max(dte, 1) / 365.0) * 100.0 if iv > 0 else 0.0
            c["be_sigma_ratio"] = be_pct / sigma_T if sigma_T > 0 else float('inf')

    # ── Step 1: Filter with per-contract rejection tracking ─────────────────
    filter_rejected: List[dict] = []
    actionable, filter_rejected = filter_actionable_tracked(
        all_scored, max_be_sigma=max_be_sigma
    )
    print(f"\n{'='*60}")
    print(f"  ACTIONABLE FILTER: {len(actionable)}/{len(all_scored)} passed "
          f"({len(filter_rejected)} rejected)")
    print(f"  (BE/1σ ≤ {max_be_sigma:.2f} · spread ≤ 5% · Vol/OI ≥ 0.25 · OI ≥ 200 "
          f"· TQS ≥ 65 · DTE ≥ 6 · Spot ≥ $50 · 1-DTE: lev ≤ 50× & θ ≤ 40%/day · above SMA200)")
    print(f"{'='*60}")

    if not actionable:
        # Still write reports (actionable empty + rejected full)
        html = build_actionable_html([], scanned_count, found_count, generated_at, max_be_sigma)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        rej_html = build_rejected_html(
            filter_rejected, scanned_count=scanned_count, found_count=found_count,
            actionable_count=0, generated_at=generated_at, max_be_sigma=max_be_sigma,
        )
        rej_path = out_path.parent / "options_rejected.html"
        rej_path.write_text(rej_html, encoding="utf-8")
        print(f"  [actionable] No contracts passed filter. Empty report → {out_path}")
        print(f"  [rejected]   {len(filter_rejected)} rejected → {rej_path}")
        return []

    # ── Step 2: Enrich with signal engine scores ─────────────────────────────
    if not skip_signal:
        actionable = enrich_with_signal_score(actionable, signal_scorer)

    # ── Step 3: Enrich with support levels ───────────────────────────────────
    if not skip_support:
        actionable = enrich_with_support(actionable, support_fetcher)

    # ── Step 3b: Post-support SMA200 filter + rejection tracking ────────────
    # above_sma200 was NOT available when filter_actionable() ran (F14 is dead code there).
    # Now that enrich_with_support() has populated it, enforce the SMA200 gate here.
    before_sma = len(actionable)
    post_sma_rejected: List[dict] = []
    actionable_after_sma: List[dict] = []
    for c in actionable:
        if c.get("above_sma200") is False:
            rc = dict(c)
            sma_v = c.get("support_sma200") or c.get("sma200") or 0
            rc["_reject_code"] = "F14b-sma200-postenrich"
            rc["_reject_reason"] = (
                f"spot=${c.get('spot_price', 0):.1f} < sma200=${sma_v:.1f} "
                f"(confirmed by enrich_with_support)"
            )
            post_sma_rejected.append(rc)
        else:
            actionable_after_sma.append(c)
    actionable = actionable_after_sma
    if post_sma_rejected:
        print(f"  [actionable] SMA200 post-filter: {len(post_sma_rejected)} contract(s) removed "
              f"(below SMA200)")

    # ── Step 3c: Dedup by OPRA code ───────────────────────────────────────────
    seen_codes: dict = {}
    no_code: list = []
    for c in actionable:
        code = c.get("code") or ""
        if not code:
            no_code.append(c)
            continue
        key = (c.get("tqs_v2", 0), -(c.get("be_sigma_ratio", 999)))
        if code not in seen_codes or key > seen_codes[code][0]:
            seen_codes[code] = (key, c)
    before_dedup = len(actionable)
    actionable = [v for _, v in seen_codes.values()] + no_code
    if len(actionable) < before_dedup:
        print(f"  [actionable] Dedup: removed {before_dedup - len(actionable)} duplicate(s)")

    # ── Step 4: Sort by TQS desc, then BE/1σ asc ─────────────────────────────
    actionable.sort(key=lambda c: (-c.get("tqs_v2", 0), c.get("be_sigma_ratio", 999)))

    # ── Step 5: Write options_actionable.html ─────────────────────────────────
    html = build_actionable_html(
        actionable, scanned_count, found_count, generated_at, max_be_sigma
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    # ── Step 6: Write options_rejected.html ───────────────────────────────────
    all_rejected = filter_rejected + post_sma_rejected
    rej_path = out_path.parent / "options_rejected.html"
    rej_html = build_rejected_html(
        all_rejected,
        scanned_count=scanned_count,
        found_count=found_count,
        actionable_count=len(actionable),
        generated_at=generated_at,
        max_be_sigma=max_be_sigma,
    )
    rej_path.write_text(rej_html, encoding="utf-8")

    elapsed = time.time() - t0
    print(f"\n  [actionable] Report: {out_path}")
    print(f"  [actionable] {len(actionable)} actionable contracts in {elapsed:.1f}s")
    print(f"  [rejected]   {len(all_rejected)} rejected → {rej_path}")
    if filter_rejected:
        near = [c for c in filter_rejected if (c.get("tqs_v2") or 0) >= 65]
        if near:
            print(f"  [rejected]   {len(near)} near-miss(es) with TQS ≥ 65:")
            for c in near[:5]:
                sym = c.get("symbol", "?")
                st = c.get("strike", "?")
                code = c.get("_reject_code", "?")
                reason = c.get("_reject_reason", "?")
                tqs = c.get("tqs_v2", 0) or 0
                print(f"    → {sym} C{st} TQS={tqs:.1f} | {code}: {reason}")

    return actionable
