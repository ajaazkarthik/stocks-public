# coding: utf-8
"""
Zerodha Login Helper — manual and fully-automatic modes
─────────────────────────────────────────────────────────
Kite Connect requires browser-based OAuth to obtain an access_token.
There is NO direct API that accepts credentials/TOTP — the browser login
page is the only supported authentication path.

This helper automates that browser flow in three modes (tried in order):

  1. AUTO   — Playwright launches a headless Chromium browser, fills in
               your user ID, password, and the TOTP code (from your 2FA
               secret), then captures the request_token from the redirect.
               Requires: ZERODHA_USER_ID + ZERODHA_PASSWORD + ZERODHA_TOTP_SECRET
               Install once: pip install playwright pyotp && playwright install chromium

  2. SEMI   — Opens your default browser, waits for you to log in manually,
               then asks you to paste the redirect URL (or just the token).
               No credentials required.

  3. MANUAL — python zerodha_login.py <request_token>  (exchange directly,
               useful if you already have the request_token)

After obtaining a token, it is saved to the daily cache file so main.py
can load it automatically for the rest of the day without prompting.

AUTO mode one-time setup (add to PowerShell $PROFILE for persistence):
  $env:ZERODHA_USER_ID     = "DFX693"
  $env:ZERODHA_PASSWORD    = "your_password"
  $env:ZERODHA_TOTP_SECRET = "your_totp_base32_secret"   # from Zerodha 2FA setup page
"""

import json
import os
import sys
import urllib.parse
import webbrowser
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("[FAIL] kiteconnect not installed.  Run: pip install kiteconnect")
    sys.exit(1)

# ── Credentials (env overrides defaults) ──────────────────────────────────────
API_KEY    = os.environ.get("ZERODHA_API_KEY",    "").strip()
API_SECRET = os.environ.get("ZERODHA_API_SECRET", "").strip()

_THIS_DIR   = Path(__file__).parent
TOKEN_CACHE = _THIS_DIR / ".zerodha_token_cache"


# ── Cache helpers ─────────────────────────────────────────────────────────────

def save_cache(token: str) -> None:
    import time as _time
    TOKEN_CACHE.write_text(
        json.dumps({
            "token":     token,
            "date":      str(date.today()),
            "auth_time": _time.time(),  # epoch — used for 24.75h expiry check
        }),
        encoding="utf-8",
    )


def load_cache() -> str:
    """Return a valid cached token, or '' if absent/stale.

    Supports both old caches (date-only) and new caches (with auth_time).
    Zerodha tokens are valid until ~06:05 IST the next day (~24h 35min).
    We use a 24.75h upper bound so post-midnight runs don't re-authenticate.
    """
    import time as _time
    try:
        if TOKEN_CACHE.exists():
            data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            token = data.get("token", "")
            if not token:
                return ""
            # Prefer auth_time check (same logic as zerodha_auth.load_cache)
            auth_ts = data.get("auth_time")
            if auth_ts:
                age_hours = (_time.time() - float(auth_ts)) / 3600.0
                return token if age_hours < 24.75 else ""
            # Fallback: date-only cache (legacy — pre 2026-05-03)
            if data.get("date") == str(date.today()):
                return token
    except Exception:
        pass
    return ""


def _exchange(kite: KiteConnect, request_token: str) -> str:
    """Exchange request_token → access_token and save to cache. Returns token."""
    data         = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    save_cache(access_token)
    os.environ["ZERODHA_ACCESS_TOKEN"] = access_token
    return access_token


# ── Mode 1: Playwright fully-automated login ──────────────────────────────────

def attempt_auto_login(silent: bool = False) -> str:
    """
    Try to log in automatically via Playwright (headless browser).
    Requires ZERODHA_USER_ID + ZERODHA_PASSWORD + ZERODHA_TOTP_SECRET.
    Returns the access_token on success, or '' on failure/missing creds.
    """
    user_id  = os.environ.get("ZERODHA_USER_ID",    "").strip()
    password = os.environ.get("ZERODHA_PASSWORD",   "").strip()
    totp_sec = os.environ.get("ZERODHA_TOTP_SECRET","").strip()

    if not (user_id and password and totp_sec):
        return ""  # credentials not set → caller falls back to other mode

    try:
        import pyotp
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        if not silent:
            print("  Auto-login needs: pip install playwright pyotp")
            print("  Then run:        playwright install chromium")
        return ""

    kite      = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    if not silent:
        print("  🤖  Auto-login: launching headless browser…")

    request_token = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx     = browser.new_context()
            page    = ctx.new_page()

            # Capture redirect URL via network request interception
            caught_url: list[str] = []
            def _on_request(req):
                if "request_token=" in req.url:
                    caught_url.append(req.url)
            page.on("request", _on_request)

            page.goto(login_url, timeout=30_000)

            # Step 1: user + password
            page.fill("#userid",   user_id,  timeout=10_000)
            page.fill("#password", password, timeout=5_000)
            page.click("button[type=submit]", timeout=5_000)

            # Step 2: TOTP  (page may say "Enter TOTP" or show an input)
            try:
                # Try each selector variant Zerodha has used over different versions
                totp_sel = None
                for sel in ("#totp", "input[name='totp']", "input[type='number']",
                            "input[placeholder*='TOTP']", "input[placeholder*='6']"):
                    try:
                        page.wait_for_selector(sel, timeout=3_000)
                        totp_sel = sel
                        break
                    except PWTimeout:
                        continue
                if totp_sel:
                    totp = pyotp.TOTP(totp_sec).now()
                    page.fill(totp_sel, totp, timeout=5_000)
                    page.click("button[type=submit]", timeout=5_000)
            except PWTimeout:
                pass   # some flows skip TOTP if trusted device

            # Wait for redirect carrying request_token
            try:
                page.wait_for_url("*request_token=*", timeout=20_000)
                caught_url.append(page.url)
            except PWTimeout:
                pass

            browser.close()

        final_url = next((u for u in caught_url if "request_token=" in u), "")
        if not final_url:
            if not silent:
                print("  ⚠  Browser redirect not captured — falling back.")
            return ""

        qs            = urllib.parse.urlparse(final_url).query
        params        = urllib.parse.parse_qs(qs)
        request_token = params.get("request_token", [""])[0]

    except Exception as exc:
        if not silent:
            print(f"  ⚠  Auto-login failed: {exc}")
        return ""

    if not request_token:
        return ""

    try:
        token = _exchange(kite, request_token)
        if not silent:
            print(f"  ✅  Auto-login ok — token: {token[:12]}…")
        return token
    except Exception as exc:
        if not silent:
            print(f"  ⚠  Session generation failed: {exc}")
        return ""


# ── Mode 2 & 3: Semi-manual / arg-provided ────────────────────────────────────

def run_login() -> str:
    """
    Run the full login flow and return the access_token (or "" if user skips).
    Callable directly from main.py without spawning a subprocess.

    GUARDRAIL: Refuses to re-authenticate if a valid cached token already
    exists.  Zerodha allows only ONE active session per API key — logging in
    again would silently invalidate any script currently using the old token.

    Tries in order:
      1. Playwright auto-login (if credentials env vars set)
      2. Semi-manual: opens browser, asks for redirect URL paste
    Returns the access_token on success, or "" if user skips/cancels.
    """
    # ── GUARDRAIL: block unnecessary re-auth ────────────────────────────────
    existing = load_cache()
    if existing:
        print(
            f"\n{'=' * 66}\n"
            f"  \u26d4  ZERODHA GUARDRAIL \u2014 Login blocked\n"
            f"  A valid token is already cached for today ({date.today()}).\n"
            f"  Zerodha allows only ONE active session per API key.\n"
            f"  Logging in again would INVALIDATE the current session,\n"
            f"  breaking any running script that holds this token.\n\n"
            f"  To force re-authentication, first delete the cache:\n"
            f"    del india-brokers\\.zerodha_token_cache\n"
            f"  OR call: from zerodha_auth import invalidate_token; invalidate_token()\n"
            f"{'=' * 66}\n"
        )
        return existing  # return the still-valid token
    # ── proceed with normal login flow ──────────────────────────────────────
    kite = KiteConnect(api_key=API_KEY)

    # Auto (Playwright)
    token = attempt_auto_login(silent=False)
    if token:
        return token

    # Semi-manual
    login_url = kite.login_url()
    print(f"\n  Opening browser to Zerodha login page…")
    webbrowser.open(login_url)
    print(f"  URL: {login_url}")
    print("\n  After logging in, copy the full redirect URL or just the")
    print("  request_token value from it, e.g.:")
    print("    https://127.0.0.1/?request_token=XXXX&action=login&status=success\n")
    raw = input("  Paste redirect URL or request_token (Enter to skip): ").strip()
    if not raw:
        return ""

    if "request_token=" in raw:
        qs            = urllib.parse.urlparse(raw).query
        params        = urllib.parse.parse_qs(qs)
        request_token = params.get("request_token", [""])[0]
    else:
        request_token = raw

    if not request_token:
        return ""

    try:
        return _exchange(kite, request_token)
    except Exception as exc:
        print(f"  ❌  Session generation failed: {exc}")
        return ""


def main() -> None:
    # ── Check for an already-valid cached token ──────────────────────────────
    cached = load_cache()
    if cached:
        print(f"\n✅  Token already cached for today ({date.today()}).")
        print(f"    First 12 chars: {cached[:12]}…")
        print("    main.py will load it automatically — nothing to do.\n")
        return

    kite = KiteConnect(api_key=API_KEY)

    # ── Mode 1: fully automatic ───────────────────────────────────────────────
    token = attempt_auto_login(silent=False)
    if token:
        print(f"\n✅  Access token saved to cache ({TOKEN_CACHE.name})")
        print(f"    First 12 chars: {token[:12]}…")
        print(f"    Valid until: 06:00 AM tomorrow\n")
        return

    # ── Mode 3: request_token passed as CLI argument ──────────────────────────
    if len(sys.argv) > 1:
        request_token = sys.argv[1].strip()
    else:
        # ── Mode 2: semi-manual — open browser, user logs in ─────────────────
        login_url = kite.login_url()
        print("\n" + "─" * 60)
        print("  Zerodha Login")
        print("─" * 60)
        print(f"\nOpening browser to:\n  {login_url}\n")
        webbrowser.open(login_url)
        print("After logging in you'll be redirected to a URL containing")
        print("  '?request_token=XXXX&action=login&status=success'")
        print("Copy that full URL (or just the token value) and paste below.\n")
        raw = input("Paste redirect URL or request_token: ").strip()
        if not raw:
            print("❌  Nothing pasted — exiting.")
            sys.exit(1)
        # Accept either the full URL or just the token
        if "request_token=" in raw:
            qs            = urllib.parse.urlparse(raw).query
            params        = urllib.parse.parse_qs(qs)
            request_token = params.get("request_token", [""])[0]
        else:
            request_token = raw

    if not request_token:
        print("❌  No request_token found — exiting.")
        sys.exit(1)

    try:
        token = _exchange(kite, request_token)
    except Exception as exc:
        print(f"\n❌  Session generation failed: {exc}")
        sys.exit(1)

    print(f"\n✅  Access token saved to cache ({TOKEN_CACHE.name})")
    print(f"    First 12 chars: {token[:12]}…")
    print(f"    Valid until: 06:00 AM tomorrow")
    print(f"\n    main.py will load this token automatically today.")
    _hint_auto_setup()


def _hint_auto_setup() -> None:
    """Print one-time tip about enabling full auto-login."""
    uid = os.environ.get("ZERODHA_USER_ID", "")
    if uid:
        return   # already set
    print("""
  💡  To never paste a token again, set these in your PowerShell profile
      ($PROFILE) and auto-login will run headlessly each morning:

        $env:ZERODHA_USER_ID     = "DFX693"
        $env:ZERODHA_PASSWORD    = "your_password"
        $env:ZERODHA_TOTP_SECRET = "your_totp_base32_secret"
""")


if __name__ == "__main__":
    main()
