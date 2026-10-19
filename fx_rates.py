#!/usr/bin/env python3
"""
fx_rates.py — Live FX rate fetching, caching, and conversion helpers.
=======================================================================
Single source of truth for currency conversion across the Global Portfolio
Dashboard, CGT reports, and analytics databases.

Replaces the ~8 independently hand-maintained hardcoded FX tables that used
to live in global_portfolio_dashboard.py, portfolio_report_html.py, and
reports/report_db.py. Those tables had drifted apart by up to 4.4% from each
other, and several of them silently omitted GBP/CAD/KRW — currencies that
ARE reachable via Moomoo positions on TSX/LON/KRX — causing those positions
to be silently valued at a 1:1 USD rate in some (but not all) report
sections and database snapshots.

Design:
  1. Live fetch (frankfurter.app, ECB reference rates — free, no API key)
  2. Fallback live fetch (open.er-api.com — free, no API key)
  3. File cache (.cache_fx_rates.json, 6h TTL) so we don't hit the network
     on every report generation
  4. Stale cache (any age) if both live sources fail
  5. Static fallback table (last resort — only if there is truly no cache
     and no network at all, e.g. very first run while offline)

Usage:
    from fx_rates import get_fx_to_usd, to_usd, to_aud, convert

    rates = get_fx_to_usd()             # {"USD": 1.0, "AUD": 0.68xx, ...}
    usd_val = to_usd(100, "AUD")        # 100 AUD -> USD
    aud_val = to_aud(100, "USD")        # 100 USD -> AUD
    val = convert(100, "GBP", "INR")    # 100 GBP -> INR
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.resolve()
_FX_CACHE_FILE = ROOT / ".cache_fx_rates.json"
_FX_CACHE_TTL = 6 * 3600  # 6 hours — portfolio valuation doesn't need per-second freshness

# Every currency reachable in this system's position data:
#   AUD/INR/SGD/USD/HKD/JPY — the 6 currencies actively traded (per user)
#   GBP/CAD — reachable via Moomoo positions on LON/TSX (_currency_for_prefix)
#   KRW     — reachable via Moomoo positions on KRX (_currency_for_prefix)
#   CNY     — used as a proxy for CNH (offshore yuan) on HK-adjacent statements
_CURRENCIES = ["USD", "AUD", "INR", "SGD", "HKD", "JPY", "GBP", "CAD", "KRW", "CNY"]

# Last-resort static fallback — used ONLY when live fetch fails AND no cache
# (of any age) exists at all, e.g. the very first run with no network access.
# Approximate rates as of 2026-06. NOT used in normal operation.
_STATIC_FALLBACK_TO_USD: dict[str, float] = {
    "USD": 1.0, "AUD": 0.6887, "INR": 0.01056, "SGD": 0.7723,
    "HKD": 0.1275, "JPY": 0.00616, "GBP": 1.322, "CAD": 0.7025,
    "KRW": 0.000645, "CNY": 0.1396,
}


def _fetch_frankfurter() -> dict[str, float] | None:
    """Primary source: frankfurter.app (ECB reference rates, free, no key)."""
    try:
        targets = ",".join(c for c in _CURRENCIES if c != "USD")
        url = f"https://api.frankfurter.app/latest?from=USD&to={targets}"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        usd_to = data.get("rates") or {}
        return _invert_usd_quotes(usd_to)
    except Exception:
        return None


def _fetch_open_er_api() -> dict[str, float] | None:
    """Fallback source: open.er-api.com (free, no key, ~160 currencies)."""
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        if data.get("result") != "success":
            return None
        usd_to = data.get("rates") or {}
        return _invert_usd_quotes(usd_to)
    except Exception:
        return None


def _invert_usd_quotes(usd_to: dict) -> dict[str, float] | None:
    """
    Both APIs quote as "1 USD = X <currency>" (usd_to[ccy] = X).
    We store the inverse convention: rates[ccy] = how many USD is 1 <ccy>.
    """
    if not usd_to:
        return None
    rates = {"USD": 1.0}
    for ccy in _CURRENCIES:
        if ccy == "USD":
            continue
        per_usd = usd_to.get(ccy)
        if per_usd:
            rates[ccy] = 1.0 / per_usd
    return rates if len(rates) > 1 else None


def _load_cache(max_age: float | None = None) -> dict[str, float] | None:
    try:
        if not _FX_CACHE_FILE.exists():
            return None
        raw = json.loads(_FX_CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - raw.get("saved_at", 0)
        if max_age is not None and age > max_age:
            return None
        rates = raw.get("rates")
        return rates or None
    except Exception:
        return None


def _save_cache(rates: dict[str, float]) -> None:
    try:
        _FX_CACHE_FILE.write_text(
            json.dumps({"saved_at": time.time(), "rates": rates}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # cache write failure must never break report generation


def get_fx_to_usd(force_refresh: bool = False) -> dict[str, float]:
    """
    Return {currency_code: rate_to_usd} for every currency this system trades.
    1 unit of `currency_code` == result[currency_code] USD.

    Resolution order:
      1. Fresh cache (< 6h old) — unless force_refresh=True
      2. Live fetch: frankfurter.app -> open.er-api.com
      3. Stale cache (any age) — better than nothing if both APIs are down
      4. Static fallback table (prints a clear warning — should be rare)
    """
    if not force_refresh:
        cached = _load_cache(max_age=_FX_CACHE_TTL)
        if cached:
            return cached

    for fetcher in (_fetch_frankfurter, _fetch_open_er_api):
        rates = fetcher()
        if rates:
            _save_cache(rates)
            return rates

    stale = _load_cache(max_age=None)  # any age, better than a hardcoded guess
    if stale:
        print(f"[fx_rates] WARNING: live FX fetch failed (frankfurter.app and "
              f"open.er-api.com both unreachable) — using stale cache from disk.")
        return stale

    print("[fx_rates] WARNING: live FX fetch failed and no cache exists — "
          "using static fallback rates (see _STATIC_FALLBACK_TO_USD in fx_rates.py). "
          "This should only happen on a brand-new machine with no network access.")
    return dict(_STATIC_FALLBACK_TO_USD)


def to_usd(amount: float, currency: str, rates: dict[str, float] | None = None) -> float:
    """Convert `amount` in `currency` to USD."""
    r = rates if rates is not None else get_fx_to_usd()
    rate = r.get(currency)
    if rate is None:
        print(f"[fx_rates] WARNING: no live rate for currency {currency!r} — "
              f"treating as 1:1 USD (value may be inaccurate).")
        rate = 1.0
    return amount * rate


def to_aud(amount: float, currency: str, rates: dict[str, float] | None = None) -> float:
    """Convert `amount` in `currency` to AUD."""
    r = rates if rates is not None else get_fx_to_usd()
    usd = to_usd(amount, currency, r)
    aud_rate = r.get("AUD", _STATIC_FALLBACK_TO_USD["AUD"])
    return usd / aud_rate if aud_rate else usd


def convert(amount: float, from_currency: str, to_currency: str,
            rates: dict[str, float] | None = None) -> float:
    """Convert `amount` from `from_currency` to `to_currency` via a USD pivot."""
    r = rates if rates is not None else get_fx_to_usd()
    usd = to_usd(amount, from_currency, r)
    target_rate = r.get(to_currency)
    if target_rate is None:
        print(f"[fx_rates] WARNING: no live rate for currency {to_currency!r} — "
              f"treating as 1:1 USD (value may be inaccurate).")
        target_rate = 1.0
    return usd / target_rate if target_rate else usd


if __name__ == "__main__":
    # Quick manual check: python fx_rates.py
    live = get_fx_to_usd(force_refresh=True)
    print("Live FX rates (1 unit -> USD):")
    for ccy, rate in sorted(live.items()):
        print(f"  {ccy:<4} {rate:.6f}")
