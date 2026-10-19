# coding: utf-8
"""
zerodha_auth — Single Source of Truth for Zerodha Authentication
=================================================================
ALL modules must call get_kite() from here.  Never roll your own auth.

Why this exists
---------------
Five separate files used to each embed their own token resolution logic:
  india_cgt.py, india_enricher.py, india_gtt_manager.py,
  india_order_executor.py, india_portfolio_dashboard.py.

This caused two critical bugs:
  1. Token invalidated mid-session:  Zerodha allows only ONE active session
     per API key.  When two modules each called run_login() in the same
     process (e.g. combined_cgt_report.py imports both india_cgt and
     india_enricher), the second generate_session() call produced a new
     access_token and silently killed the first one.  Every subsequent
     API call with the old token returned TokenException.

  2. Wrong stale check in india_gtt_manager: the condition
       if cache_date != today AND cache_has_token → clear env var
     discarded a valid env var if the cache file happened to be from
     yesterday (e.g. cache was just deleted while env var was fresh).

Design
------
  • Module-level KiteConnect singleton (_kite) — only ONE session per process.
  • Token resolution order (first valid wins):
      1. Today's .zerodha_token_cache file  ← authoritative, date-gated
      2. ZERODHA_ACCESS_TOKEN env var        ← for subprocess inheritance
      3. Auto-login via zerodha_login.run_login() (Playwright / semi-manual)
  • get_kite(silent=False) — returns the singleton; creates it on first call.
  • invalidate_token()     — called on TokenException; clears singleton + cache.
  • get_access_token()     — returns the raw token string (for env passing).

Usage
-----
    from zerodha_auth import get_kite, invalidate_token

    kite = get_kite()
    try:
        holdings = kite.holdings()
    except Exception as exc:
        if "TokenException" in type(exc).__name__ or "Incorrect" in str(exc):
            invalidate_token()
            kite = get_kite()          # triggers fresh login
            holdings = kite.holdings()
        else:
            raise
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ── Credentials ───────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("ZERODHA_API_KEY",    "").strip()
API_SECRET = os.environ.get("ZERODHA_API_SECRET", "").strip()

_THIS_DIR   = Path(__file__).parent
TOKEN_CACHE = _THIS_DIR / ".zerodha_token_cache"

# ── Module-level singleton ─────────────────────────────────────────────────────
_kite: Optional[object] = None   # KiteConnect instance or None


# ── Cache helpers (re-exported so zerodha_login.py callers still work) ────────

def save_cache(token: str) -> None:
    """Persist today's token to the daily cache file."""
    TOKEN_CACHE.write_text(
        json.dumps({
            "token":     token,
            "date":      str(date.today()),
            "auth_time": time.time(),   # epoch seconds — used by cache_age_hours()
        }),
        encoding="utf-8",
    )


def load_cache() -> str:
    """Return a valid cached token, or '' if stale / absent.

    Expiry rule — matches Zerodha's actual token lifetime:
      Zerodha invalidates tokens at 06:00 IST (00:30 UTC) the NEXT day.
      We add a 5-minute buffer → expire our cache at 00:35 UTC.

    OLD (broken): compared `date == today` which discarded valid tokens at
    midnight, causing spurious re-authentication every night even though the
    token had several more hours of validity.  That caused 5 unexpected auth
    triggers any time the dashboard was first run after midnight.
    """
    try:
        if TOKEN_CACHE.exists():
            data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            token = data.get("token", "")
            if not token:
                return ""
            # Use auth_time for expiry if available (set since 2026-04-21).
            # Zerodha tokens expire at 06:05 IST = 00:35 UTC = 24h 35m after
            # the previous day's 06:00 IST session open.  We cap at 24.75 h.
            auth_ts = data.get("auth_time")
            if auth_ts:
                age_hours = (time.time() - float(auth_ts)) / 3600.0
                if age_hours < 24.75:          # ~24 h 45 min — safe upper bound
                    return token
                return ""                       # token aged past safe limit
            # Fallback for old caches without auth_time: accept if cached today
            # OR cached yesterday but current UTC time is still before 00:35.
            import datetime as _dt
            now_utc   = _dt.datetime.now(_dt.timezone.utc)
            cutoff_utc = now_utc.replace(hour=0, minute=35, second=0, microsecond=0)
            cache_date = data.get("date", "")
            today_str     = str(_dt.date.today())
            yesterday_str = str((_dt.date.today() - _dt.timedelta(days=1)))
            if cache_date == today_str:
                return token
            if cache_date == yesterday_str and now_utc < cutoff_utc:
                return token              # still within yesterday's grace window
    except Exception:
        pass
    return ""


# ── Token resolution ───────────────────────────────────────────────────────────

def _resolve_token(silent: bool = False) -> str:
    """
    Return a valid access token, running login only if necessary.

    Resolution order:
      1. Today's cache file  (authoritative — date-gated)
      2. ZERODHA_ACCESS_TOKEN env var  (for subprocess inheritance from main.py)
      3. Auto-login via zerodha_login.run_login()
    """
    # 1. Today's cache — most reliable; also keeps env var in sync so child
    #    processes inherit a fresh token without needing a cache file.
    cached = load_cache()
    if cached:
        os.environ["ZERODHA_ACCESS_TOKEN"] = cached
        return cached

    # 2. Env var — subprocess inheritance from main.py or manually set token.
    #    Use it in-memory but do NOT persist to cache here: the env var might
    #    be stale (from a previous shell session or $PROFILE).  If it is valid,
    #    the first API call will succeed and the caller can cache it explicitly.
    #    If stale, TokenException fires, invalidate_token() clears the env var
    #    (cache_age_hours() stays 999 since we never wrote it), and the next
    #    get_kite() call falls through to run_login() for a fresh token.
    env_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "").strip()
    if env_token:
        if not silent:
            print("[zerodha_auth] Using ZERODHA_ACCESS_TOKEN from environment "
                  "(not re-caching — will validate on first API call)")
        return env_token

    # 3. No valid token anywhere.
    # When silent=True, NEVER open a browser or prompt — just return "".
    # This is the contract used by platform_watchdog._check_zerodha() and
    # validate_token(): they want a cache read only, not an interactive login.
    if silent:
        return ""

    print("[zerodha_auth] No valid Zerodha token found. Launching login…")
    try:
        sys.path.insert(0, str(_THIS_DIR))
        from zerodha_login import run_login as _run_login
        at = _run_login()
        if at:
            save_cache(at)
            os.environ["ZERODHA_ACCESS_TOKEN"] = at
            print(f"[zerodha_auth] Login successful — token cached for today.")
        return at
    except Exception as exc:
        print(f"[zerodha_auth] Login error: {exc}")
        return ""


# ── Public API ─────────────────────────────────────────────────────────────────

def get_kite(silent: bool = False):
    """
    Return the authenticated KiteConnect singleton.

    First call resolves the token and creates the instance.
    Subsequent calls within the same process return the cached instance —
    avoiding repeated generate_session() calls that would invalidate the
    existing token.

    Raises EnvironmentError if no token can be obtained.
    """
    global _kite
    if _kite is not None:
        return _kite

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        raise ImportError(
            "kiteconnect not installed.  Run: pip install kiteconnect"
        )

    at = _resolve_token(silent=silent)
    if not at:
        raise EnvironmentError(
            "Zerodha access token unavailable.\n"
            "Options:\n"
            "  1. Set ZERODHA_USER_ID + ZERODHA_PASSWORD + ZERODHA_TOTP_SECRET\n"
            "     for headless auto-login.\n"
            "  2. Run: python india-brokers/zerodha_login.py\n"
            "  3. Set: $env:ZERODHA_ACCESS_TOKEN='<token>'  (PowerShell)"
        )

    if not API_KEY:
        raise EnvironmentError(
            "ZERODHA_API_KEY environment variable is not set.\n"
            "Export it before running: $env:ZERODHA_API_KEY='<your_api_key>'"
        )

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(at)
    _kite = kite
    return kite


def get_access_token(silent: bool = False) -> str:
    """
    Return the current access token string without creating a KiteConnect
    instance.  Useful for passing the token to subprocess environments.
    """
    cached = load_cache()
    if cached:
        os.environ["ZERODHA_ACCESS_TOKEN"] = cached
        return cached
    env_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "").strip()
    if env_token:
        # Do NOT re-cache env var here — it may be stale from a previous session.
        # See _resolve_token() step 2 for rationale.
        return env_token
    return _resolve_token(silent=silent)


def invalidate_token(reason: str = "") -> None:
    """
    Clear the singleton and delete the cache so the next get_kite() call
    triggers a fresh login.  Call this inside a TokenException handler.

    GUARDRAIL: If the token was cached less than 2 hours ago, prints a
    prominent warning.  An early invalidation usually indicates an overly
    broad error-match in the caller, not a genuinely expired token.
    Zerodha allows only ONE active session — unnecessary re-auth will break
    any other currently running script holding the same token.
    """
    global _kite
    age = cache_age_hours()
    if age < 2.0:
        _reason_str = f"  Reason supplied: {reason}\n" if reason else ""
        print(
            f"\n{'=' * 66}\n"
            f"  ⛔  ZERODHA GUARDRAIL — Suspicious token invalidation\n"
            f"  Token was cached only {age:.1f}h ago (tokens normally last ~24h).\n"
            f"{_reason_str}"
            f"  Logging in again will BREAK any script currently using this\n"
            f"  token (Zerodha allows only ONE active session per API key).\n"
            f"  If this invalidation is a mistake, check the caller's error\n"
            f"  matching logic — 'api_key' / 'access_token' alone are too broad.\n"
            f"{'=' * 66}\n"
        )
    _kite = None
    os.environ.pop("ZERODHA_ACCESS_TOKEN", None)
    try:
        if TOKEN_CACHE.exists():
            TOKEN_CACHE.unlink()
    except Exception:
        pass


def is_token_valid() -> bool:
    """Return True if a today's token is available (cache or env), False otherwise."""
    return bool(load_cache() or os.environ.get("ZERODHA_ACCESS_TOKEN", "").strip())


def cache_age_hours() -> float:
    """
    Return how many hours ago the token was cached.
    Returns 999.0 if the cache file is absent or has no auth_time field.
    Useful for deciding whether to warn the user that the token is ageing.
    Normal lifetime: ~6 hours (Zerodha invalidates tokens at 06:00 IST next day).
    """
    try:
        if TOKEN_CACHE.exists():
            data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            saved = data.get("auth_time")
            if saved:
                return (time.time() - float(saved)) / 3600.0
    except Exception:
        pass
    return 999.0


def _log_abnormal_expiry(context: str = "") -> None:
    """
    Log a warning when a token that should still be valid throws TokenException.
    Zerodha tokens are valid until ~06:00 IST the next day.  An expiry before
    that is abnormal (manual revoke, parallel session, etc.).

    This function is intentionally non-fatal — it just prints a warning so
    operators can investigate without crashing the calling script.
    """
    age = cache_age_hours()
    msg = (
        f"[zerodha_auth] ⚠ ABNORMAL TOKEN EXPIRY detected{(' — ' + context) if context else ''}. "
        f"Token age: {age:.1f}h. Zerodha tokens should last until 06:00 IST next day. "
        f"Possible causes: manual token revocation, second active session, API key reset."
    )
    print(msg)


def validate_token(silent: bool = False) -> bool:
    """
    Make a live Zerodha API call (profile endpoint) to confirm the cached token
    is still accepted by the server.  Returns True if profile() succeeds.

    ⚠  This makes ONE live network call to Zerodha.  Do NOT call from background
    health-check loops — use is_token_valid() for the lightweight cache check.

    Uses get_kite(silent=True) so that:
      - the existing singleton is reused (no second KiteConnect instance)
      - no fresh login is triggered if the token is absent (raises EnvironmentError)
    """
    try:
        kite = get_kite(silent=True)  # reuse or create singleton; no login
        kite.profile()                # one live read call to verify the token
        return True
    except EnvironmentError:
        # No token in cache or env — cannot validate
        return False
    except Exception as exc:
        low = str(exc).lower()
        if "token" in low or "permission" in low or "access" in low:
            _log_abnormal_expiry(f"validate_token: {exc}")
            invalidate_token()
        if not silent:
            print(f"[zerodha_auth] validate_token failed: {exc}")
        return False
