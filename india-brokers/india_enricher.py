"""
India Enricher — L5 Portfolio Context for Signal Stack
=======================================================
Enriches signal stack analysis with REAL holdings data from:
  • Zerodha (Kite Connect) — account DFX693
  • Angel One (SmartAPI)  — account K51989642

What this adds to every NSE/BSE signal:
  • Already holding? → exact qty, avg price, unrealised PnL %
  • Position sizing context → don't double-up on large losers
  • Score modifier:
      - No holding + bullish   → +5 (fresh opportunity)
      - Holding at -15% to -30% → +3 (averaging zone)
      - Holding at -30%+        → 0  (averaging danger zone)
      - Holding at +20%+        → -5 (avoid chasing winners)
      - Holding >5% of portfolio → -3 (overweight)

Usage in signal_stack.py (auto-import pattern):
    from india_enricher import IndiaEnricher
    enricher = IndiaEnricher()  # loads holdings once
    e = enricher.enrich("NSE:INFY")
    badge_html = enricher.html_badge(e)

Standalone test:
    python india_enricher.py --symbols NSE:BEL NSE:CAPACITE NSE:INFY

Requirements:
    pip install kiteconnect smartapi-python pyotp
    ENV: ZERODHA_ACCESS_TOKEN  (or ZERODHA_API_KEY + ZERODHA_API_SECRET)
    ENV: ANGELONE_API_KEY (provided via environment)
    ENV: ANGELONE_CLIENT_CODE (provided via environment)
    ENV: ANGELONE_PIN (provided via environment)
    ENV: ANGELONE_TOTP_SECRET (provided via environment)
"""

from __future__ import annotations

import os
import sys
import time
import json
import argparse
import traceback
from pathlib import Path
from typing import Optional
from datetime import datetime, date

# ─── IST timezone (for Angel One session cache) ───────────────────────────────
try:
    from zoneinfo import ZoneInfo as _ZI
    _AO_IST = _ZI("Asia/Kolkata")
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo as _ZI  # type: ignore
        _AO_IST = _ZI("Asia/Kolkata")
    except ImportError:
        _AO_IST = None  # fall back to local clock

# ─── Angel One session cache (one generateSession() per IST day) ───────────────
# This prevents the enricher from invalidating the platform_watchdog's session
# (and any other SmartConnect session) on every 5-minute holdings refresh.
_ao_smart: object = None           # cached SmartConnect instance
_ao_session_date: "date | None" = None  # IST calendar date of last successful auth

# ─── TV → NSE/BSE symbol conversion ──────────────────────────────────────────
# TradingView exchanges that map to India
INDIA_EXCHANGES = {"NSE", "BSE", "NSE_DLY", "BSE_DLY"}

# Symbols that Zerodha appends -EQ suffix (equity segment)
# Angel One also does this — strip for comparison
_KITE_SUFFIX = "-EQ"

def tv_to_nse(pro_symbol: str) -> Optional[str]:
    """
    Extract the bare NSE/BSE trading symbol from a TV pro_symbol.
    Returns None for non-India symbols.

    Examples:
      NSE:INFY      → INFY
      BSE:500209    → 500209
      NYSE:AAPL     → None
    """
    if ":" not in pro_symbol:
        return None
    exchange, ticker = pro_symbol.split(":", 1)
    if exchange.upper() not in INDIA_EXCHANGES:
        return None
    return ticker.upper().strip()


def _strip_eq(symbol: str) -> str:
    """Remove -EQ suffix that brokers add to equity instruments."""
    if symbol.upper().endswith(_KITE_SUFFIX):
        return symbol[: -len(_KITE_SUFFIX)]
    return symbol


# ─────────────────────────────────────────────────────────────────────────────
class IndiaEnricher:
    """
    Loads holdings from Zerodha + Angel One once, then answers enrichment
    queries per symbol in O(1) from a local cache.

    Refresh interval: CACHE_TTL seconds (default 5 minutes during market hours).
    """

    CACHE_TTL = 300  # seconds between re-fetches

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._holdings: dict[str, dict] = {}  # symbol → merged holding dict
        self._total_portfolio_value: float = 0.0
        self._last_refresh: float = 0.0
        self._zerodha_ok = False
        self._angelone_ok = False
        self._errors: list[str] = []
        self._refresh()

    # ── Internal: load Zerodha ────────────────────────────────────────────────

    def _load_zerodha(self) -> list[dict]:
        try:
            from zerodha_auth import get_kite, invalidate_token
            _retried = False
            while True:
                try:
                    kite = get_kite(silent=True)
                    raw  = kite.holdings()
                    self._zerodha_ok = True
                    return raw or []
                except EnvironmentError:
                    print("[india_enricher] ⚠  Zerodha token unavailable — no env var, no cache, "
                          "auto-login failed. Set ZERODHA_USER_ID+ZERODHA_PASSWORD+ZERODHA_TOTP_SECRET "
                          "for headless auto-login, or run: python india-brokers/zerodha_login.py")
                    return []
                except Exception as exc:
                    err = str(exc)
                    if not _retried and ("Incorrect" in err or "TokenException" in type(exc).__name__):
                        _retried = True
                        invalidate_token()
                        continue   # retry with fresh login
                    self._errors.append(f"Zerodha: {exc}")
                    if self.verbose:
                        traceback.print_exc()
                    return []
        except Exception as exc:
            self._errors.append(f"Zerodha: {exc}")
            if self.verbose:
                traceback.print_exc()
            return []

    # ── Internal: load Angel One ──────────────────────────────────────────────

    def _load_angelone(self) -> list[dict]:
        """
        Fetch Angel One holdings, creating at most one SmartConnect session
        per IST calendar day.  Subsequent calls within the same IST day reuse
        the cached SmartConnect instance — avoiding session invalidation on
        every 5-minute cache refresh.
        """
        global _ao_smart, _ao_session_date
        try:
            import pyotp
            from SmartApi import SmartConnect
            import logging as _l; _l.getLogger("logzero_default").setLevel(_l.CRITICAL)

            api_key     = os.environ.get("ANGELONE_API_KEY",     "")
            client_code = os.environ.get("ANGELONE_CLIENT_CODE", "")
            pin         = os.environ.get("ANGELONE_PIN",         "")
            totp_secret = os.environ.get("ANGELONE_TOTP_SECRET", "")

            today_ist: date = (
                datetime.now(tz=_AO_IST).date() if _AO_IST else date.today()
            )

            # Cache hit: reuse existing session for the same IST day
            if _ao_smart is not None and _ao_session_date == today_ist:
                smart = _ao_smart
            else:
                # Cache miss: create a new session (once per IST day)
                totp  = pyotp.TOTP(totp_secret).now()
                smart = SmartConnect(api_key=api_key)
                auth  = smart.generateSession(client_code, pin, totp)
                if not auth.get("status"):
                    self._errors.append(f"Angel One login failed: {auth}")
                    return []
                _ao_smart        = smart
                _ao_session_date = today_ist

            resp = smart.holding()
            self._angelone_ok = True
            # getAllHolding returns {"holdings": [...], "totalholding": {...}}
            # while older endpoints return a plain list — handle both.
            raw = resp.get("data") or []
            if isinstance(raw, dict):
                raw = raw.get("holdings") or []
            return raw
        except Exception as exc:
            # Invalidate cached session on any error so next call retries auth
            _ao_smart        = None
            _ao_session_date = None
            self._errors.append(f"Angel One: {exc}")
            if self.verbose:
                traceback.print_exc()
            return []

    # ── Internal: merge holdings into unified cache ───────────────────────────

    def _refresh(self) -> None:
        now = time.time()
        if now - self._last_refresh < self.CACHE_TTL:
            return  # still fresh

        z_raw = self._load_zerodha()
        a_raw = self._load_angelone()

        merged: dict[str, dict] = {}
        total_val = 0.0

        # ── Zerodha holdings ─────────────────────────────────────────
        for h in z_raw:
            sym = _strip_eq(h.get("tradingsymbol", "")).upper()
            if not sym:
                continue
            qty  = int(h.get("quantity", 0) or 0)
            avg  = float(h.get("average_price", 0) or 0)
            ltp  = float(h.get("last_price", 0) or 0)
            pnl  = float(h.get("pnl", 0) or 0)
            val  = ltp * qty
            total_val += val
            merged[sym] = {
                "symbol": sym,
                "zerodha_qty":   qty,
                "zerodha_avg":   avg,
                "zerodha_ltp":   ltp,
                "zerodha_pnl":   pnl,
                "zerodha_val":   val,
                "zerodha_pnl_pct": ((ltp - avg) / avg * 100) if avg else 0,
                "angelone_qty":  0,
                "angelone_avg":  0,
                "angelone_ltp":  0,
                "angelone_pnl":  0,
                "angelone_val":  0,
                "angelone_pnl_pct": 0,
            }

        # ── Angel One holdings ───────────────────────────────────────
        for h in a_raw:
            raw_sym = h.get("tradingsymbol") or h.get("symbolname") or ""
            sym  = _strip_eq(raw_sym).upper()
            if not sym:
                continue
            qty  = int(h.get("quantity", 0) or 0)
            avg  = float(h.get("averageprice", 0) or 0)
            ltp  = float(h.get("ltp", 0) or 0)
            pnl  = float(h.get("profitandloss", 0) or 0)
            val  = ltp * qty
            total_val += val

            if sym in merged:
                merged[sym].update({
                    "angelone_qty":     qty,
                    "angelone_avg":     avg,
                    "angelone_ltp":     ltp,
                    "angelone_pnl":     pnl,
                    "angelone_val":     val,
                    "angelone_pnl_pct": ((ltp - avg) / avg * 100) if avg else 0,
                })
            else:
                merged[sym] = {
                    "symbol": sym,
                    "zerodha_qty":     0,
                    "zerodha_avg":     0,
                    "zerodha_ltp":     0,
                    "zerodha_pnl":     0,
                    "zerodha_val":     0,
                    "zerodha_pnl_pct": 0,
                    "angelone_qty":    qty,
                    "angelone_avg":    avg,
                    "angelone_ltp":    ltp,
                    "angelone_pnl":    pnl,
                    "angelone_val":    val,
                    "angelone_pnl_pct": ((ltp - avg) / avg * 100) if avg else 0,
                }

        # ── Compute combined fields ──────────────────────────────────
        for sym, h in merged.items():
            z_qty  = h["zerodha_qty"]
            a_qty  = h["angelone_qty"]
            total_qty = z_qty + a_qty

            # Weighted average price across both brokers
            z_cost = h["zerodha_avg"] * z_qty
            a_cost = h["angelone_avg"] * a_qty
            total_cost = z_cost + a_cost
            combined_avg = (total_cost / total_qty) if total_qty else 0

            # Best available LTP
            combined_ltp = h["zerodha_ltp"] or h["angelone_ltp"]

            combined_pnl = h["zerodha_pnl"] + h["angelone_pnl"]
            combined_val = h["zerodha_val"] + h["angelone_val"]
            combined_pnl_pct = ((combined_ltp - combined_avg) / combined_avg * 100) if combined_avg else 0

            h.update({
                "total_qty":      total_qty,
                "combined_avg":   combined_avg,
                "combined_ltp":   combined_ltp,
                "combined_pnl":   combined_pnl,
                "combined_val":   combined_val,
                "combined_pnl_pct": combined_pnl_pct,
                "brokers":        (["zerodha"] if z_qty > 0 else []) + (["angelone"] if a_qty > 0 else []),
            })

        self._holdings       = merged
        self._total_portfolio_value = total_val
        self._last_refresh   = now

        if self.verbose:
            print(f"[india_enricher] Loaded {len(merged)} India holdings "
                  f"(Zerodha: {self._zerodha_ok}, AngelOne: {self._angelone_ok}). "
                  f"Total value: INR {total_val:,.0f}")

    # ── Public: enrich(tv_symbol) ──────────────────────────────────────────────

    def enrich(self, tv_symbol: str) -> dict:
        """
        Return enrichment dict for a TradingView symbol.

        Returns empty dict if not an India symbol or not found in holdings.
        Always returns a dict — never raises.
        """
        self._refresh()  # no-op if cache is fresh

        nse_sym = tv_to_nse(tv_symbol)
        if not nse_sym:
            return {}  # non-India symbol

        h = self._holdings.get(nse_sym)
        if not h:
            # Not currently held — still return context
            return {
                "symbol":  nse_sym,
                "holding": False,
                "l5_score": 5.0,  # fresh opportunity
                "l5_reason": "Not currently held — fresh entry",
                "zerodha_qty": 0,
                "angelone_qty": 0,
                "total_qty": 0,
                "combined_avg": 0,
                "combined_ltp": 0,
                "combined_pnl_pct": 0,
                "portfolio_weight_pct": 0,
                "broker_status": {
                    "zerodha":  self._zerodha_ok,
                    "angelone": self._angelone_ok,
                },
            }

        # Compute portfolio weight
        portfolio_weight_pct = (
            (h["combined_val"] / self._total_portfolio_value * 100)
            if self._total_portfolio_value
            else 0
        )

        pnl_pct = h["combined_pnl_pct"]
        l5_score, l5_reason = _score_holding(pnl_pct, portfolio_weight_pct)

        return {
            **h,
            "holding": True,
            "portfolio_weight_pct": round(portfolio_weight_pct, 2),
            "l5_score":  l5_score,
            "l5_reason": l5_reason,
            "broker_status": {
                "zerodha":  self._zerodha_ok,
                "angelone": self._angelone_ok,
            },
        }

    # ── Public: html_badge ────────────────────────────────────────────────────

    def html_badge(self, enrichment: dict) -> str:
        """Generate compact HTML badge for signal_stack report cards.

        Three states:
          1. Empty dict (non-India symbol)              → '' (silent)
          2. Brokers unavailable (both failed)          → amber warning badge
          3. Normal flow: not-held (FRESH) or holding   → coloured badge
             If only one broker is available a dim ⚠ is appended.
        """
        if not enrichment:
            return ""

        broker_status = enrichment.get("broker_status", {})
        zerodha_ok  = broker_status.get("zerodha",  True)  # assume OK when key absent
        angelone_ok = broker_status.get("angelone", True)  # (backwards compat)
        neither_ok  = not zerodha_ok and not angelone_ok
        partial_ok  = (zerodha_ok ^ angelone_ok)  # exactly one broker OK

        if not enrichment.get("holding"):
            if neither_ok:
                # Both brokers down — cannot tell if stock is held or not
                return (
                    '<span style="background:#1c1005;color:#d97706;border:1px solid #92400e;'
                    'padding:2px 6px;border-radius:3px;font-size:10px;font-weight:bold;" '
                    'title="Zerodha and Angel One both unreachable — holding status unknown">'
                    "⚠ Broker data N/A</span>"
                )
            warn = " ⚠" if partial_ok else ""
            return (
                f'<span style="background:#0d2b0d;color:#4caf50;border:1px solid #4caf50;'
                f'padding:2px 6px;border-radius:3px;font-size:10px;font-weight:bold;">'
                f"🟢 FRESH{warn}</span>"
            )

        pnl_pct = enrichment.get("combined_pnl_pct", 0)
        total_qty = enrichment.get("total_qty", 0)
        avg = enrichment.get("combined_avg", 0)
        brokers = enrichment.get("brokers", [])
        weight  = enrichment.get("portfolio_weight_pct", 0)

        # Colour by PnL
        if pnl_pct >= 15:
            col = "#ff9800"   # orange — strong gain, be cautious
        elif pnl_pct >= 0:
            col = "#4caf50"   # green — slight gain
        elif pnl_pct >= -15:
            col = "#ffc107"   # yellow — small loss
        elif pnl_pct >= -30:
            col = "#f44336"   # red — moderate loss
        else:
            col = "#9c27b0"   # purple — heavy loss

        broker_str = "+".join(b.upper()[0] for b in brokers)  # Z, A, Z+A
        sign = "+" if pnl_pct >= 0 else ""

        return (
            f'<span style="background:#0d0d20;color:{col};border:1px solid {col};'
            f'padding:2px 6px;border-radius:3px;font-size:10px;font-weight:bold;" '
            f'title="Avg: ₹{avg:,.2f} | Weight: {weight:.1f}% | {broker_str}">'
            f"HOLDING {total_qty} @ {sign}{pnl_pct:.1f}%</span>"
        )

    # ── Public: summary ───────────────────────────────────────────────────────

    def summary(self) -> str:
        """Human-readable summary for CLI."""
        self._refresh()
        lines = [
            f"India Holdings ({len(self._holdings)} stocks, "
            f"INR {self._total_portfolio_value:,.0f} total)",
            f"  Zerodha: {'✓' if self._zerodha_ok else '✗'}  "
            f"Angel One: {'✓' if self._angelone_ok else '✗'}",
            "",
        ]
        for sym, h in sorted(self._holdings.items()):
            pnl_pct = h["combined_pnl_pct"]
            sign    = "+" if pnl_pct >= 0 else ""
            weight  = (h["combined_val"] / self._total_portfolio_value * 100) if self._total_portfolio_value else 0
            l5, reason = _score_holding(pnl_pct, weight)
            z_info = f"Z:{h['zerodha_qty']}" if h["zerodha_qty"] else ""
            a_info = f"A:{h['angelone_qty']}" if h["angelone_qty"] else ""
            brokers = "  ".join(x for x in [z_info, a_info] if x)
            lines.append(
                f"  {sym:<22} {brokers:<20} "
                f"avg=₹{h['combined_avg']:>8,.2f}  ltp=₹{h['combined_ltp']:>8,.2f}  "
                f"pnl={sign}{pnl_pct:+5.1f}%  wt={weight:4.1f}%  "
                f"L5={l5:.1f}  [{reason}]"
            )
        if self._errors:
            lines += ["", "ERRORS:"] + [f"  {e}" for e in self._errors]
        return "\n".join(lines)


# ─── L5 scoring logic ─────────────────────────────────────────────────────────

def _score_holding(pnl_pct: float, portfolio_weight_pct: float) -> tuple[float, str]:
    """
    Returns (l5_score 0–10, reason_string).

    Portfolio context rules (validated against averaging-down research):
      pnl > +20%    : -5 → "large unrealised gain — risk of chasing top"
      +5 to +20%    : 0  → "modest gain — neutral on adding"
      -5 to +5%     : +5 → "near breakeven — strong avg opportunity"
      -15 to -5%    : +4 → "small loss — reasonable avg zone"
      -30 to -15%   : +2 → "moderate loss — avg with caution"
      < -30%         : 0  → "deep loss — fundamental re-check needed"
      weight > 5%   : -2 → "overweight in portfolio"
      weight > 10%  : -4 → "heavily overweight — do NOT add"
    """
    score = 5.0  # base

    if pnl_pct > 20:
        score = 3.0
        reason = f"already +{pnl_pct:.0f}% — avoid chasing"
    elif pnl_pct > 5:
        score = 5.0
        reason = f"+{pnl_pct:.0f}% gain — neutral"
    elif pnl_pct >= -5:
        score = 7.0
        reason = f"{pnl_pct:+.0f}% near breakeven — good avg zone"
    elif pnl_pct >= -15:
        score = 6.5
        reason = f"{pnl_pct:.0f}% small loss — avg opportunity"
    elif pnl_pct >= -30:
        score = 5.0
        reason = f"{pnl_pct:.0f}% moderate loss — avg with caution"
    else:
        score = 3.0
        reason = f"{pnl_pct:.0f}% deep loss — verify fundamentals first"

    # Portfolio weight penalty
    if portfolio_weight_pct > 10:
        score = max(0, score - 4)
        reason += f" | OVERWEIGHT {portfolio_weight_pct:.1f}%"
    elif portfolio_weight_pct > 5:
        score = max(0, score - 2)
        reason += f" | large weight {portfolio_weight_pct:.1f}%"

    return round(score, 1), reason


# ─── Module-level helpers (signal_stack.py drop-in interface) ─────────────────

_default_enricher: Optional[IndiaEnricher] = None

def _get_enricher() -> IndiaEnricher:
    global _default_enricher
    if _default_enricher is None:
        _default_enricher = IndiaEnricher()
    return _default_enricher


def enrich_india(tv_symbol: str) -> dict:
    """Module-level convenience: enrich a single TV symbol."""
    return _get_enricher().enrich(tv_symbol)


def india_html_badge(enrichment: dict) -> str:
    """Module-level convenience: generate HTML badge."""
    return _get_enricher().html_badge(enrichment)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys
    if _sys.platform == "win32":
        try: _sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  pass  # cleanup

    parser = argparse.ArgumentParser(description="India Enricher — test L5 portfolio context")
    parser.add_argument("--symbols", nargs="+",
                        default=["NSE:BEL", "NSE:CAMS", "NSE:CAPACITE", "NSE:INFY",
                                 "NSE:RELIANCE", "NSE:LALPATHLAB", "NSE:CDSL"],
                        help="TradingView symbols to test")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    enricher = IndiaEnricher(verbose=True)
    print()
    print(enricher.summary())

    print()
    print("=" * 70)
    print("  Per-symbol enrichment:")
    print("=" * 70)
    for sym in args.symbols:
        e = enricher.enrich(sym)
        if not e:
            print(f"  {sym:<25}  (non-India or no data)")
            continue
        holding = e.get("holding", False)
        if holding:
            print(f"  {sym:<25}  HOLDING  qty={e['total_qty']}  "
                  f"avg=₹{e['combined_avg']:,.2f}  pnl={e['combined_pnl_pct']:+.1f}%  "
                  f"L5={e['l5_score']}  {e['l5_reason']}")
        else:
            print(f"  {sym:<25}  NOT HELD  L5={e['l5_score']}  {e['l5_reason']}")
