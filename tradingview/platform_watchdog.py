"""
Platform Watchdog — broker session health checker
==================================================
Monitors liveness of all broker platforms used by the trading system and
exposes a health gate that webhook_server.py consults before placing orders.

Brokers monitored:
  zerodha   — Kite Connect: token present + kite.profile() succeeds + token age
  angelone  — AngelOne SmartAPI: TOTP-based session, generateSession() validation
  moomoo    — OpenD: TCP reachability on configured host:port

Exposed API (module-level conveniences):
  start_watchdog(interval=90)     → launch background daemon thread
  is_healthy(broker)              → True if last check succeeded recently
  is_any_healthy()                → True if at least one broker is healthy
  get_health_report()             → full status dict for all brokers
  check_now()                     → force immediate check of all brokers

Health states per broker:
  HEALTHY   — last check passed, result is fresh
  STALE     — last check passed but result is older than STALE_THRESHOLD
  DEGRADED  — last check failed (auth error, connection error, etc.)
  UNKNOWN   — never checked, or missing credentials

Health JSON written to: $BASE_DIR/watchdog_health.json

Usage in webhook_server.py:
    import platform_watchdog as wdog
    wdog.start_watchdog(interval=90)
    ...
    if not wdog.is_healthy("zerodha"):
        print("⚠️  Zerodha session unhealthy — India order skipped")
        return
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Configuration ─────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
HEALTH_FILE = BASE_DIR / "watchdog_health.json"

# Seconds after a successful check before it is considered STALE
STALE_THRESHOLD = 300  # 5 minutes

# Default background poll interval
DEFAULT_INTERVAL = 90  # seconds

# Moomoo OpenD location (matches webhook_server.py defaults)
MOOMOO_HOST = os.environ.get("MOOMOO_HOST", "127.0.0.1")
MOOMOO_PORT = int(os.environ.get("MOOMOO_PORT", "11111"))
MOOMOO_CONNECT_TIMEOUT = 2.0  # seconds

# ─── AngelOne IST-midnight session cache ─────────────────────────────────────
# AngelOne sessions expire at midnight IST (not after a fixed duration).
# Cache the result for the current IST calendar day to avoid calling
# generateSession() on every 90-second watchdog tick.
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo as _ZoneInfo  # type: ignore
    except ImportError:
        _ZoneInfo = None  # type: ignore

_IST = _ZoneInfo("Asia/Kolkata") if _ZoneInfo else None
_ao_auth_date: "date | None" = None    # IST calendar date of last successful auth
_ao_detail_cache: "dict | None" = None  # cached detail dict from last successful auth

# ─── Internal state ────────────────────────────────────────────────────────────

_lock = threading.Lock()

_health: dict[str, dict] = {
    "zerodha":  {"status": "UNKNOWN", "last_check": None, "error": None, "detail": {}},
    "angelone": {"status": "UNKNOWN", "last_check": None, "error": None, "detail": {}},
    "moomoo":   {"status": "UNKNOWN", "last_check": None, "error": None, "detail": {}},
}

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


# ─── Individual broker checks ──────────────────────────────────────────────────

def _check_zerodha() -> dict:
    """
    Validate Zerodha session health using the token cache — NO live API calls.

    Why no kite.profile() here:
      kite.profile() makes a live Zerodha API call on every watchdog tick.
      The watchdog runs every 90 s and start_watchdog() fires check_all()
      immediately at startup, so calling profile() here would produce
      multiple unexpected Zerodha API requests within seconds of startup —
      even when the token is already valid.  For the watchdog's purpose
      ("is the session still alive?") checking the daily cache + singleton
      is sufficient and makes zero network calls.

      If you need a one-shot explicit profile lookup, call kite.profile()
      directly from your script — do NOT add it here.

    Returns detail dict with:
      ok          — bool: True if a today-valid token exists and singleton is live
      error       — str or None
      token_age_s — seconds since token was last cached (from auth_time)
    """
    detail: dict = {"ok": False, "error": None, "token_age_s": None, "user_name": None}
    try:
        _india = Path(__file__).resolve().parent.parent / "india-brokers"
        if str(_india) not in sys.path:
            sys.path.insert(0, str(_india))
        from zerodha_auth import get_kite, cache_age_hours, is_token_valid  # type: ignore

        # Lightweight cache check — zero network calls
        if not is_token_valid():
            detail["error"] = "No valid Zerodha token in daily cache or environment"
            return detail

        # Initialise singleton if not already done (reads cache, no network)
        # silent=True guarantees no run_login() is triggered
        get_kite(silent=True)

        age_h = cache_age_hours()
        detail["ok"] = True
        detail["token_age_s"] = round(age_h * 3600) if age_h < 999 else None
        detail["error"] = None
    except Exception as exc:
        detail["error"] = str(exc)
        detail["ok"] = False
    return detail


def _check_angelone() -> dict:
    """
    Validate AngelOne SmartAPI session by calling generateSession().

    Sessions expire at midnight IST, so we cache the result for the current
    IST calendar day.  Only one generateSession() call is made per day,
    regardless of how often the watchdog loop fires (default: every 90 s).

    Returns detail dict with:
      ok         — bool
      error      — str or None
      jwt_prefix — first 12 chars of jwtToken (sanity check)
    """
    global _ao_auth_date, _ao_detail_cache

    # ── Cache hit: same IST date → return last result without a network call ──
    today_ist: date = (
        datetime.now(tz=_IST).date() if _IST else datetime.now().date()
    )
    with _lock:
        if _ao_auth_date is not None and _ao_detail_cache is not None:
            if _ao_auth_date == today_ist:
                return dict(_ao_detail_cache)

    # ── Cache miss: call generateSession() once ───────────────────────────────
    detail: dict = {"ok": False, "error": None, "jwt_prefix": None}
    try:
        import pyotp
        from SmartApi import SmartConnect
    except ImportError:
        detail["error"] = "smartapi-python and/or pyotp not installed"
        return detail

    api_key     = os.environ.get("ANGELONE_API_KEY",     "")
    client_code = os.environ.get("ANGELONE_CLIENT_CODE", "")
    pin         = os.environ.get("ANGELONE_PIN",         "")
    totp_secret = os.environ.get("ANGELONE_TOTP_SECRET", "")

    if not all([api_key, client_code, pin, totp_secret]):
        detail["error"] = "Missing AngelOne credentials in environment"
        return detail

    try:
        totp = pyotp.TOTP(totp_secret).now()
        smart = SmartConnect(api_key=api_key)
        resp = smart.generateSession(client_code, pin, totp)
        if resp.get("status") and resp.get("data", {}).get("jwtToken"):
            jwt = resp["data"]["jwtToken"]
            detail["ok"] = True
            detail["jwt_prefix"] = jwt[:12] if len(jwt) >= 12 else jwt
            detail["error"] = None
        else:
            detail["error"] = f"generateSession failed: {resp.get('message', 'no message')}"
    except Exception as exc:
        detail["error"] = str(exc)

    # ── Update cache only on success ─────────────────────────────────────────
    if detail["ok"]:
        with _lock:
            _ao_auth_date = today_ist
            _ao_detail_cache = dict(detail)

    return detail


def _check_moomoo() -> dict:
    """
    Validate Moomoo OpenD availability via TCP connect.

    Returns detail dict with:
      ok           — bool
      error        — str or None
      host         — OpenD host
      port         — OpenD port
      latency_ms   — round-trip TCP connect latency in ms
    """
    detail: dict = {
        "ok":         False,
        "error":      None,
        "host":       MOOMOO_HOST,
        "port":       MOOMOO_PORT,
        "latency_ms": None,
    }
    t0 = time.monotonic()
    try:
        with socket.create_connection((MOOMOO_HOST, MOOMOO_PORT),
                                      timeout=MOOMOO_CONNECT_TIMEOUT):
            latency_ms = (time.monotonic() - t0) * 1000
            detail["ok"]         = True
            detail["latency_ms"] = round(latency_ms, 1)
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        detail["error"] = str(exc)
    return detail


# ─── Token age sidecar ────────────────────────────────────────────────────────

_TOKEN_AGE_FILE = BASE_DIR / "watchdog_token_ages.json"
_token_age_cache: dict[str, dict] = {}  # {broker: {token_hash, first_seen}}


def _get_token_age(broker: str, token_value: str) -> Optional[float]:
    """
    Return how many seconds ago this token was first seen.
    Persists first-seen timestamps in a sidecar JSON so restarts don't reset them.
    """
    global _token_age_cache
    # Load from file if empty
    if not _token_age_cache and _TOKEN_AGE_FILE.exists():
        try:
            _token_age_cache = json.loads(_TOKEN_AGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _token_age_cache = {}

    # Identify token by hash prefix (never store the actual token)
    import hashlib
    tok_hash = hashlib.sha256(token_value.encode()).hexdigest()[:16]
    now = time.time()

    entry = _token_age_cache.get(broker, {})
    if entry.get("token_hash") != tok_hash:
        # New token — record first-seen
        _token_age_cache[broker] = {"token_hash": tok_hash, "first_seen": now}
        try:
            _TOKEN_AGE_FILE.write_text(
                json.dumps(_token_age_cache, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
        return 0.0

    return now - entry["first_seen"]


# ─── Check all brokers ────────────────────────────────────────────────────────

def check_all() -> dict[str, dict]:
    """
    Run health checks for all brokers synchronously and update internal state.
    Returns the full health report.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    checks = {
        "zerodha":  _check_zerodha,
        "angelone": _check_angelone,
        "moomoo":   _check_moomoo,
    }
    results = {}
    for broker, fn in checks.items():
        try:
            detail = fn()
            status = "HEALTHY" if detail["ok"] else "DEGRADED"
        except Exception as exc:
            detail = {"ok": False, "error": f"watchdog exception: {exc}"}
            status = "DEGRADED"

        results[broker] = {
            "status":     status,
            "last_check": now_iso,
            "error":      detail.get("error"),
            "detail":     detail,
        }

    with _lock:
        _health.update(results)

    # Write to disk for external consumption (e.g. health endpoint, dashboards)
    try:
        payload = {"checked_at": now_iso, "brokers": _health}
        HEALTH_FILE.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass

    return dict(_health)


def check_now() -> dict[str, dict]:
    """Force-check all brokers immediately. Alias for check_all()."""
    return check_all()


# ─── Health query interface ────────────────────────────────────────────────────

def is_healthy(broker: str) -> bool:
    """
    Return True if the specified broker's last health check passed recently.

    Returns False if:
      - broker was never checked (UNKNOWN)
      - last check failed (DEGRADED)
      - last check was too long ago (STALE — treat as unknown)
    """
    with _lock:
        entry = _health.get(broker.lower().split("-")[0])

    if not entry or entry["status"] == "UNKNOWN":
        return False
    if entry["status"] == "DEGRADED":
        return False

    # Check staleness
    last = entry.get("last_check")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if age > STALE_THRESHOLD:
                return False  # stale — treat as unknown
        except Exception:
            pass

    return entry["status"] == "HEALTHY"


def is_any_healthy() -> bool:
    """Return True if at least one broker is currently healthy."""
    return any(is_healthy(b) for b in ("zerodha", "angelone", "moomoo"))


def get_health_report() -> dict:
    """Return a snapshot of all broker health states."""
    with _lock:
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "brokers": {k: dict(v) for k, v in _health.items()},
        }


def get_status_text() -> str:
    """Return a one-line human-readable health summary for logging."""
    parts = []
    for broker in ("zerodha", "angelone", "moomoo"):
        with _lock:
            entry = _health.get(broker, {})
        status = entry.get("status", "UNKNOWN")
        icon = "✅" if status == "HEALTHY" else "⚠️" if status == "STALE" else "❌"
        err = entry.get("error", "")
        parts.append(f"{icon} {broker}={status}" + (f"({err[:40]})" if err else ""))
    return "  ".join(parts)


# ─── Background thread ────────────────────────────────────────────────────────

def _watchdog_loop(interval: int) -> None:
    """Background loop: check all brokers every `interval` seconds."""
    while not _stop_event.is_set():
        try:
            report = check_all()
            ts = datetime.now().strftime("%H:%M:%S")
            degraded = [b for b, s in report.items() if s["status"] != "HEALTHY"]
            if degraded:
                print(f"[{ts}] 🩺 Watchdog — DEGRADED: {', '.join(degraded)}", flush=True)
            else:
                print(f"[{ts}] 🩺 Watchdog — all brokers healthy", flush=True)
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 🩺 Watchdog exception: {exc}", flush=True)
        _stop_event.wait(interval)


def start_watchdog(interval: int = DEFAULT_INTERVAL) -> threading.Thread:
    """
    Start the background watchdog daemon thread.
    Safe to call multiple times — only one thread is started.

    Args:
        interval: seconds between health checks
    Returns:
        the running Thread
    """
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread

    _stop_event.clear()
    t = threading.Thread(
        target=_watchdog_loop,
        args=(interval,),
        daemon=True,
        name="platform-watchdog",
    )
    t.start()
    _thread = t
    # _watchdog_loop calls check_all() immediately on its first iteration,
    # so NO separate init thread is needed.  A redundant watchdog-init thread
    # would race with the loop thread and produce two simultaneous check_all()
    # calls — doubling the Zerodha/AngelOne API requests at startup.
    return t


def stop_watchdog() -> None:
    """Signal the background thread to stop."""
    _stop_event.set()


# ─── Standalone CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Check broker platform health")
    parser.add_argument("--broker", choices=["zerodha", "angelone", "moomoo", "all"],
                        default="all", help="Which broker to check")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.broker == "all":
        report = check_all()
    else:
        fn_map = {
            "zerodha":  _check_zerodha,
            "angelone": _check_angelone,
            "moomoo":   _check_moomoo,
        }
        detail = fn_map[args.broker]()
        report = {args.broker: {"detail": detail, "status": "HEALTHY" if detail["ok"] else "DEGRADED"}}

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for broker, data in report.items():
            status = data.get("status", "?")
            icon   = "✅" if status == "HEALTHY" else "❌"
            err    = data.get("error", "")
            print(f"{icon} {broker:12s} {status:10s}  {err or 'OK'}")
            detail = data.get("detail", {})
            for k, v in detail.items():
                if k not in ("ok", "error"):
                    print(f"   {k}: {v}")
