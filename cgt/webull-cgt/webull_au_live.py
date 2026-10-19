#!/usr/bin/env python3
"""
Webull AU — Live Holdings via Browser Session
==============================================
Webull AU (www.webull.com.au) does NOT expose a public API, and the
unofficial `tedchou12/webull` library targets the US platform only (no AU
region code).  This module uses THREE strategies (tried in order):

  Strategy 1 (PERSISTENT PROFILE) — Playwright launches chromium with a
      saved user-data directory (.webull_profile/).  After a one-time
      manual login (via --setup), all future runs reuse the session
      automatically.  This is the most reliable long-term approach.

  Strategy 2 (CDP) — Connect to the user's already-running Chrome via
      Chrome DevTools Protocol.  Requires Chrome launched with
      --remote-debugging-port=9222.  Zero login friction because the
      user is already authenticated in their own Chrome.

  Strategy 3 (DIRECT API) — Re-use a saved access-token (captured
      automatically by Strategies 1/2, or manually from DevTools) and
      call the AU REST API directly via requests.

───────────────────────────────────────────────────────────────────────────────
ONE-TIME SETUP
───────────────────────────────────────────────────────────────────────────────
1.  Install dependencies (once):
        pip install playwright requests
        playwright install chromium

2.  Run the one-time setup (opens a visible browser — you log in manually):
        python webull_au_live.py --setup

    This saves your session to .webull_profile/ — all future runs are
    fully automated with no login prompts.

3.  Alternative — CDP mode (connect to your existing Chrome):
    a.  Close Chrome fully, then relaunch with:
            chrome.exe --remote-debugging-port=9222
    b.  Log in to https://www.webull.com.au manually in Chrome.
    c.  Run:  python webull_au_live.py --cdp

───────────────────────────────────────────────────────────────────────────────
USAGE
───────────────────────────────────────────────────────────────────────────────
    # From Python
    from webull_au_live import get_holdings
    holdings = get_holdings()          # list of dicts
    for h in holdings:
        print(h['ticker'], h['qty'], h['mktValue'])

    # From command line
    python webull_au_live.py --setup          # ONE-TIME: log in manually, save session
    python webull_au_live.py --holdings       # fetch holdings (auto-picks best strategy)
    python webull_au_live.py --cdp            # fetch via CDP (Chrome must be running)
    python webull_au_live.py --probe          # dump raw JSON from browser intercept
    python webull_au_live.py --token          # print the captured access_token
"""

import asyncio
import json
import os
import pickle
import socket
import sys
import time
import hashlib
import uuid
from pathlib import Path
from typing import Optional

import requests

# ── Optional Playwright import (only needed for browser-intercept path) ───────
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_TOKEN_CACHE   = _HERE / ".webull_au_token.json"
_DID_FILE      = _HERE / "did.bin"
_PROFILE_DIR   = _HERE / ".webull_profile"   # persistent browser profile

# ── AU-specific base URLs (discovered via browser DevTools) ───────────────────
_BASE_URL   = "https://userapi.webull.com.au"   # account / auth
_QUOTE_URL  = "https://quoteapi.webull.com.au"  # market data
_TRADE_URL  = "https://tradeapi.webull.com.au"  # trading / positions

# Webull web-app origin header required by their API
_APP_ORIGIN  = "https://www.webull.com.au"
_APP_REFERER = "https://www.webull.com.au/"

# ── Region / header constants ─────────────────────────────────────────────────
_REGION_CODE  = 12  # AU numeric region code used in the AU web app
_APP_ID       = "webull-webapp"
_APP_VER      = "3.39.18"

# ── Credentials (env vars) ────────────────────────────────────────────────────
_EMAIL     = os.environ.get("WEBULL_AU_EMAIL",     "").strip()
_PASSWORD  = os.environ.get("WEBULL_AU_PASSWORD",  "").strip()
_TRADE_PIN = os.environ.get("WEBULL_AU_TRADE_PIN", "").strip()
_ACCESS_TOKEN_ENV = os.environ.get("WEBULL_AU_ACCESS_TOKEN", "").strip()
_DID_ENV          = os.environ.get("WEBULL_AU_DID",          "").strip()

# ── Google-SSO cookie file (same directory, named "webull") ──────────────────
_GOOGLE_COOKIE_FILE = _HERE / "webull"


def _parse_google_cookies() -> list[dict]:
    """
    Parse the 'webull' cookie file.

    The cookie line may contain:
      - Google SSO cookies (e.g. __Secure-3PSID, NID) -> .google.com
      - Webull site cookies (e.g. TDID, TDCPM) -> .webull.com.au

    Cookie string format (all on one line):
        name=value; name=value; ...
    """
    if not _GOOGLE_COOKIE_FILE.exists():
        return []
    raw = _GOOGLE_COOKIE_FILE.read_text(encoding="utf-8")
    # Find the cookie line: first non-empty line that contains "=" and ";"
    # but is NOT a $env: variable line, URL, or plain-text label
    cookie_line = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("$env:") or line.startswith("http") or line.startswith("#"):
            continue
        if "=" in line and ";" in line:
            cookie_line = line
            break
    if not cookie_line:
        return []

    def _cookie_domain(name: str) -> str:
        lower = name.lower()
        if name.startswith("__Secure-3P") or name == "NID" or "google" in lower:
            return ".google.com"
        return ".webull.com.au"

    cookies = []
    for part in cookie_line.split(";"):
        part = part.strip()
        idx = part.find("=")
        if idx < 1:
            continue
        name = part[:idx].strip()
        value = part[idx + 1:].strip()
        if not name:
            continue
        is_secure = name.startswith("__Secure-") or name.startswith("__Host-")
        cookies.append({
            "name": name,
            "value": value,
            "domain": _cookie_domain(name),
            "path": "/",
            "secure": is_secure,
            "httpOnly": False,
            "sameSite": "None" if is_secure else "Lax",
        })
    return cookies


# ─────────────────────────────────────────────────────────────────────────────
#  DID helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_did() -> str:
    """Load or create device-ID (did) — same `did.bin` pickle format as tedchou12/webull."""
    if _DID_ENV:
        return _DID_ENV
    if _DID_FILE.exists():
        with open(_DID_FILE, "rb") as f:
            return pickle.load(f)
    did = uuid.uuid4().hex
    with open(_DID_FILE, "wb") as f:
        pickle.dump(did, f)
    return did


def _save_did(did: str) -> None:
    with open(_DID_FILE, "wb") as f:
        pickle.dump(did, f)


# ─────────────────────────────────────────────────────────────────────────────
#  Token cache
# ─────────────────────────────────────────────────────────────────────────────

def _save_token(data: dict) -> None:
    _TOKEN_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_token() -> dict:
    try:
        if _TOKEN_CACHE.exists():
            return json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _token_valid(data: dict) -> bool:
    """Return True if cached token is non-empty and not within 5 min of expiry."""
    if not data.get("accessToken"):
        return False
    expire_str = data.get("tokenExpireTime", "")
    if not expire_str:
        return True
    try:
        from datetime import datetime, timezone
        # Webull returns ISO format like "2026-04-13T10:00:00.000+0000"
        expire_str_clean = expire_str.replace("+0000", "+00:00")
        expire_dt = datetime.fromisoformat(expire_str_clean)
        now = datetime.now(timezone.utc)
        return (expire_dt - now).total_seconds() > 300
    except Exception:
        return True  # Can't parse; assume valid


# ─────────────────────────────────────────────────────────────────────────────
#  Request helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_headers(access_token: str = "", did: str = "",
                   include_time: bool = False) -> dict:
    headers = {
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-AU,en;q=0.9",
        "Content-Type":    "application/json",
        "Origin":          _APP_ORIGIN,
        "Referer":         _APP_REFERER,
        "platform":        "web",
        "hl":              "en",
        "os":              "web",
        "osv":             "Mozilla/5.0",
        "app":             "global",
        "appid":           _APP_ID,
        "ver":             _APP_VER,
        "lzone":           "dc_au_r001",
        "ph":              "Windows Chrome",
        "locale":          "eng",
        "device-type":     "Web",
        "regionId":        str(_REGION_CODE),
        "did":             did or _load_did(),
    }
    if access_token:
        headers["access_token"] = access_token
    if include_time:
        headers["t_time"] = str(round(time.time() * 1000))
    return headers


def _hash_password(plain: str) -> str:
    """Webull password hashing — same as in tedchou12/webull."""
    salted = ("wl_app-a&b@!423^" + plain).encode("utf-8")
    return hashlib.md5(salted).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
#  Direct API authentication (Option B)
# ─────────────────────────────────────────────────────────────────────────────

class WebullAU:
    """
    Thin client for the Webull AU REST API.
    Authentication priority:
      1. Env var WEBULL_AU_ACCESS_TOKEN  (copy-paste from browser DevTools)
      2. Cached token in .webull_au_token.json
      3. Interactive login with credentials from env vars
    """

    def __init__(self):
        self._did          = _load_did()
        self._access_token = ""
        self._refresh_token = ""
        self._token_expire = ""
        self._account_id   = ""
        self._session      = requests.Session()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _ensure_auth(self) -> bool:
        """Return True if a valid access token is available after this call."""
        # 1. Env var
        if _ACCESS_TOKEN_ENV:
            self._access_token = _ACCESS_TOKEN_ENV
            if _DID_ENV:
                self._did = _DID_ENV
            return True

        # 2. Cached token
        cached = _load_token()
        if _token_valid(cached):
            self._access_token  = cached["accessToken"]
            self._refresh_token = cached.get("refreshToken", "")
            self._token_expire  = cached.get("tokenExpireTime", "")
            self._account_id    = cached.get("accountId", "")
            if cached.get("did"):
                self._did = cached["did"]
            return True

        # 3. Credential-based login
        if _EMAIL and _PASSWORD:
            return self._login_with_credentials()

        print("[WARN] No Webull AU credentials found.")
        print("       Set WEBULL_AU_EMAIL + WEBULL_AU_PASSWORD, or")
        print("       set WEBULL_AU_ACCESS_TOKEN (from browser DevTools).")
        return False

    def _login_with_credentials(self) -> bool:
        """POST to the Webull AU /passport/login/v5/account endpoint."""
        account_type = 2  # 2 = email, 1 = phone
        data = {
            "account":     _EMAIL,
            "accountType": str(account_type),
            "deviceId":    self._did,
            "deviceName":  "WebullAUScript",
            "grade":       1,
            "pwd":         _hash_password(_PASSWORD),
            "regionId":    _REGION_CODE,
        }
        headers = _build_headers(did=self._did)
        try:
            resp = self._session.post(
                f"{_BASE_URL}/passport/login/v5/account",
                json=data, headers=headers, timeout=15
            )
            result = resp.json()
        except Exception as exc:
            print(f"[ERROR] Login request failed: {exc}")
            return False

        if "accessToken" not in result:
            # May need MFA / security question — print the response for the user
            print(f"[WARN] Login response did not contain accessToken.")
            print(f"       Response: {json.dumps(result, indent=2)}")
            return False

        self._access_token  = result["accessToken"]
        self._refresh_token = result.get("refreshToken", "")
        self._token_expire  = result.get("tokenExpireTime", "")

        # Try to fetch account ID
        self._account_id = self._get_account_id_from_result(result)

        cache_data = {
            "accessToken":     self._access_token,
            "refreshToken":    self._refresh_token,
            "tokenExpireTime": self._token_expire,
            "accountId":       self._account_id,
            "did":             self._did,
        }
        _save_token(cache_data)
        print(f"[OK] Logged in to Webull AU.  account_id={self._account_id}")
        return True

    def _get_account_id_from_result(self, login_result: dict) -> str:
        """Fetch secAccountId from /account/list after login."""
        headers = _build_headers(self._access_token, self._did)
        try:
            resp = self._session.get(
                f"{_BASE_URL}/account/list",
                headers=headers, timeout=15
            )
            data = resp.json()
            if data.get("success") and data.get("data"):
                return str(data["data"][0].get("secAccountId", ""))
        except Exception:
            pass
        return login_result.get("uuid", "")

    def refresh_token(self) -> bool:
        if not self._refresh_token:
            return False
        headers = _build_headers(self._access_token, self._did)
        try:
            resp = self._session.post(
                f"{_BASE_URL}/passport/refreshToken/v2",
                json={"refreshToken": self._refresh_token},
                headers=headers, timeout=15
            )
            result = resp.json()
            if result.get("accessToken"):
                self._access_token  = result["accessToken"]
                self._refresh_token = result.get("refreshToken", self._refresh_token)
                self._token_expire  = result.get("tokenExpireTime", "")
                cached = _load_token()
                cached.update({
                    "accessToken":     self._access_token,
                    "refreshToken":    self._refresh_token,
                    "tokenExpireTime": self._token_expire,
                })
                _save_token(cached)
                return True
        except Exception as exc:
            print(f"[WARN] Token refresh failed: {exc}")
        return False

    # ── Account data ──────────────────────────────────────────────────────────

    def _get(self, url: str, params: dict | None = None,
             include_time: bool = False) -> dict:
        headers = _build_headers(self._access_token, self._did,
                                 include_time=include_time)
        resp = self._session.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def get_account(self) -> dict:
        """Full account snapshot including positions."""
        if not self._account_id:
            self._account_id = self._get_account_id_from_result({})
        url = f"{_TRADE_URL}/v2/option/account/accountId/{self._account_id}"
        # try the simpler account endpoint first
        endpoints = [
            f"{_TRADE_URL}/account/{self._account_id}",
            f"{_BASE_URL}/account/{self._account_id}",
            f"{_TRADE_URL}/v2/account/{self._account_id}",
        ]
        last_err = None
        for ep in endpoints:
            try:
                return self._get(ep)
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"Could not fetch account data: {last_err}")

    def get_positions(self) -> list[dict]:
        """Return list of current stock positions."""
        data = self.get_account()
        # The key may be 'positions' or 'holdingList' depending on AU endpoint
        return data.get("positions") or data.get("holdingList") or []

    def get_portfolio(self) -> dict:
        """Key-value portfolio summary (total value, cash, etc.)."""
        data = self.get_account()
        members = data.get("accountMembers", [])
        return {item["key"]: item["value"] for item in members}


# ─────────────────────────────────────────────────────────────────────────────
#  Playwright browser-intercept path
# ─────────────────────────────────────────────────────────────────────────────

_HOLDINGS_PATTERNS = [
    # URL fragments that the Webull AU web app uses for holdings/positions
    # Covers both webull.com.au and webullfintech.com API domains
    "holdingList",
    "positions",
    "account/",
    "portfolio",
    "holding",
    "assets",
    "center/sc",
]

_CDP_PORT = 9222
_PORTFOLIO_URL = "https://www.webull.com.au/center/sc"


def _cdp_available() -> bool:
    """Check if Chrome is running with CDP debug port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("localhost", _CDP_PORT))
        s.close()
        return True
    except Exception:
        return False


def _setup_response_interceptor(captured: dict, all_api_calls: list):
    """Return (on_response, on_request) handlers for a Playwright page."""

    async def on_response(response):
        url = response.url
        if "webull.com.au" not in url and "webullfintech.com" not in url:
            return
        try:
            body = await response.json()
            all_api_calls.append({"url": url, "status": response.status, "body": body})
            print(f"  [API] {response.status} {url}")
            if not any(pat in url for pat in _HOLDINGS_PATTERNS):
                return
            if not captured.get("positions"):
                positions = (
                    body.get("positions")
                    or body.get("holdingList")
                    or (body.get("data") or {}).get("positions")
                    or (body.get("data") or {}).get("holdingList")
                    or []
                )
                if positions or "positions" in url or "holding" in url.lower():
                    captured["raw_api_url"] = url
                    captured["raw_json"]    = body
                    captured["positions"]   = positions
                    print(f"  [HOLDINGS] Captured {len(positions)} positions from {url}")
        except Exception:
            pass

    async def on_request(request):
        url = request.url
        if "webull.com.au" not in url and "webullfintech.com" not in url:
            return
        hdrs = request.headers
        if "access_token" in hdrs and not captured.get("access_token"):
            captured["access_token"] = hdrs["access_token"]
            captured["did"]          = hdrs.get("did", "")
            existing = _load_token()
            existing["accessToken"] = hdrs["access_token"]
            if hdrs.get("did"):
                existing["did"] = hdrs["did"]
                _save_did(hdrs["did"])
            _save_token(existing)
            print(f"  [TOKEN] access_token captured → saved to {_TOKEN_CACHE.name}")

    return on_response, on_request


async def _wait_for_holdings(captured: dict, timeout_s: int) -> None:
    """Poll until holdings data is found or timeout."""
    print(f"[WAIT] Watching for holdings API call (up to {timeout_s}s) …")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if captured.get("positions") is not None:
            break
        await asyncio.sleep(1)
        remaining = int(deadline - time.time())
        if remaining % 10 == 0 and remaining > 0:
            print(f"  … {remaining}s remaining")


async def _extract_tokens_from_page(page: Page) -> dict:
    """
    Extract Webull auth tokens from the page's localStorage / cookies.
    Webull stores access_token, did, and account info in localStorage
    after login. We grab them and save to our token cache for the direct
    API path (Strategy 3).
    """
    tokens = {}
    try:
        # Webull stores tokens in localStorage under various keys
        ls_data = await page.evaluate("""() => {
            const result = {};
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                const val = localStorage.getItem(key);
                // Capture auth-related keys
                if (key.includes('token') || key.includes('Token') ||
                    key.includes('did') || key.includes('account') ||
                    key.includes('userId') || key.includes('uuid') ||
                    key.includes('auth') || key.includes('Auth') ||
                    key.includes('access') || key.includes('refresh') ||
                    key.includes('login') || key.includes('Login') ||
                    key.includes('sec_account') || key.includes('secAccount')) {
                    result[key] = val;
                }
            }
            return result;
        }""")
        if ls_data:
            print(f"  [TOKEN] Extracted {len(ls_data)} localStorage keys")
            tokens["localStorage"] = ls_data

            # Try to find the access token
            for key, val in ls_data.items():
                if not val:
                    continue
                lk = key.lower()
                if "access" in lk and "token" in lk:
                    tokens["accessToken"] = val
                elif lk == "did":
                    tokens["did"] = val
                elif "refresh" in lk and "token" in lk:
                    tokens["refreshToken"] = val
                elif "account" in lk and "id" in lk:
                    tokens["accountId"] = val
                # Also try to parse JSON values
                if val.startswith("{"):
                    try:
                        parsed = json.loads(val)
                        if isinstance(parsed, dict):
                            if parsed.get("accessToken"):
                                tokens["accessToken"] = parsed["accessToken"]
                            if parsed.get("refreshToken"):
                                tokens["refreshToken"] = parsed["refreshToken"]
                            if parsed.get("did"):
                                tokens["did"] = parsed["did"]
                            if parsed.get("secAccountId"):
                                tokens["accountId"] = str(parsed["secAccountId"])
                    except (json.JSONDecodeError, TypeError):
                        pass

        # Also grab cookies
        cookies = await page.context.cookies()
        webull_cookies = [c for c in cookies
                         if "webull" in c.get("domain", "")]
        if webull_cookies:
            print(f"  [TOKEN] Found {len(webull_cookies)} Webull cookies")
            tokens["cookies"] = webull_cookies
            for c in webull_cookies:
                if c["name"].lower() == "access_token":
                    tokens["accessToken"] = c["value"]
                elif c["name"].lower() == "did":
                    tokens["did"] = c["value"]

    except Exception as exc:
        print(f"  [TOKEN] Could not extract tokens: {exc}")

    # Save whatever we found
    if tokens.get("accessToken"):
        cache = _load_token()
        cache["accessToken"] = tokens["accessToken"]
        if tokens.get("refreshToken"):
            cache["refreshToken"] = tokens["refreshToken"]
        if tokens.get("accountId"):
            cache["accountId"] = tokens["accountId"]
        if tokens.get("did"):
            cache["did"] = tokens["did"]
            _save_did(tokens["did"])
        _save_token(cache)
        print(f"  [TOKEN] ✓ Saved access_token to {_TOKEN_CACHE.name}")
    elif tokens.get("localStorage"):
        # Dump all localStorage for debugging even if we didn't find tokens
        ls_dump = _HERE / "_webull_localstorage.json"
        ls_dump.write_text(json.dumps(tokens["localStorage"], indent=2),
                           encoding="utf-8")
        print(f"  [TOKEN] No access_token found. localStorage saved to {ls_dump.name}")

    return tokens


# ─────────────────────────────────────────────────────────────────────────────
#  Strategy 1: PERSISTENT BROWSER PROFILE
#  One-time manual login → session saved → all future runs automated.
# ─────────────────────────────────────────────────────────────────────────────

async def _intercept_via_persistent(headless: bool = True,
                                     timeout_s: int = 90,
                                     setup_mode: bool = False) -> dict:
    """
    Launch chromium with a persistent user-data directory.

    In setup_mode (--setup):
      - Opens visible browser.
      - Navigates to Webull login page.
      - Waits for user to log in manually.
      - Once logged in, navigates to portfolio to confirm.
      - Session persists to .webull_profile/ for future runs.

    In normal mode:
      - Opens headless (or headful) browser with saved profile.
      - Navigates to portfolio page.
      - Intercepts holdings API response.
    """
    if not _PLAYWRIGHT_OK:
        raise ImportError("playwright not installed. Run: pip install playwright && playwright install chromium")

    captured: dict = {}
    all_api_calls: list[dict] = []

    profile_str = str(_PROFILE_DIR)

    async with async_playwright() as p:
        # launch_persistent_context keeps cookies, localStorage, sessionStorage
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=profile_str,
            headless=False if setup_mode else headless,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        on_response, on_request = _setup_response_interceptor(captured, all_api_calls)
        page.on("response", on_response)
        page.on("request", on_request)

        if setup_mode:
            # ── SETUP MODE: let user log in manually ──────────────────────
            print("=" * 70)
            print("  WEBULL AU — ONE-TIME LOGIN SETUP")
            print("=" * 70)
            print()
            print("  The browser will open your portfolio page.")
            print("  If not logged in, Webull will redirect to the login page.")
            print("  Log in using Google SSO or email/password.")
            print("  After login, you'll be redirected back to your portfolio.")
            print("  The script auto-detects success. (Timeout: 5 minutes)")
            print()

            # Navigate to PORTFOLIO first — Webull will redirect to login if
            # not authenticated.  After SSO login, the redirect goes back to
            # the portfolio page WITH proper auth tokens set.  This is the
            # natural Webull flow and avoids losing auth on hard navigation.
            await page.goto(_PORTFOLIO_URL, timeout=30_000)
            await page.wait_for_timeout(3_000)

            # Check if already on portfolio (session from a previous run)
            current_url = page.url
            if "passport.webull.com.au" not in current_url and "login" not in current_url.lower():
                print("[SETUP] Already logged in! Extracting tokens …")
                await page.wait_for_timeout(5_000)
                await _extract_tokens_from_page(page)
                await _wait_for_holdings(captured, timeout_s=30)
                if captured.get("positions"):
                    print(f"[SETUP] ✓ Captured {len(captured['positions'])} holdings!")
                else:
                    print("[SETUP] ✓ Session valid. Holdings may load on next run.")
                captured["all_api_calls"] = all_api_calls
                await context.close()
                return captured

            # On login page — wait for user to complete login (max 5 min)
            print(f"[SETUP] On login page: {page.url}")
            print("        Please log in now …")
            login_deadline = time.time() + 300  # 5 minutes
            logged_in = False
            while time.time() < login_deadline:
                current_url = page.url
                # Success: redirected away from login page
                if "passport.webull.com.au" not in current_url and "login" not in current_url.lower():
                    print(f"\n  [SETUP] Login redirect detected → {current_url}")
                    logged_in = True
                    break
                await asyncio.sleep(2)
                remaining = int(login_deadline - time.time())
                if remaining % 30 == 0 and remaining > 0:
                    print(f"  … waiting for login ({remaining}s remaining)")

            if not logged_in:
                print("\n[SETUP] ⚠ Timeout — no login detected in 5 minutes.")
                print("        Run again:  python webull_au_live.py --setup")
                await context.close()
                return captured

            # Post-login: give the SPA time to process auth tokens
            print("[SETUP] Login successful! Waiting for portfolio to load …")
            await page.wait_for_timeout(8_000)

            # If we ended up on homepage instead of portfolio, navigate via SPA
            current_url = page.url
            if "/center/" not in current_url:
                print(f"[SETUP] Redirected to {current_url}, navigating to portfolio …")
                try:
                    await page.goto(_PORTFOLIO_URL, timeout=30_000)
                    await page.wait_for_timeout(5_000)
                except Exception:
                    pass

            # Extract tokens for the direct API fallback
            current_url = page.url
            if "passport.webull.com.au" not in current_url:
                print("[SETUP] ✓ Login saved! Session stored in .webull_profile/")
                print("        Future runs will be fully automated.")
                await _extract_tokens_from_page(page)
                await _wait_for_holdings(captured, timeout_s=30)
            else:
                print("[SETUP] ⚠ Auth not persisted. Try the CDP approach:")
                print("        python webull_au_live.py --cdp")

            captured["all_api_calls"] = all_api_calls
            await context.close()
            return captured

        # ── NORMAL MODE: reuse saved session ─────────────────────────────
        print("[PERSISTENT] Navigating to portfolio with saved session …")
        await page.goto(_PORTFOLIO_URL, timeout=30_000)
        await page.wait_for_timeout(3_000)

        # Check if session is still valid — only treat as expired if
        # we were actually redirected to the login page
        current_url = page.url

        if "passport.webull.com.au" in current_url:
            print("[PERSISTENT] Session expired — redirected to login page.")
            print("             Run:  python webull_au_live.py --setup")
            captured["session_expired"] = True
            await context.close()
            return captured

        print(f"[PERSISTENT] On page: {current_url}")

        # Extract tokens regardless (for direct API fallback)
        await _extract_tokens_from_page(page)

        await _wait_for_holdings(captured, timeout_s)

        captured["all_api_calls"] = all_api_calls

        if not captured.get("positions"):
            # Try refreshing the page to trigger API calls
            print("[PERSISTENT] No holdings found — refreshing page …")
            await page.reload(timeout=30_000)
            await page.wait_for_timeout(3_000)
            await _wait_for_holdings(captured, timeout_s=30)

        if not captured.get("positions"):
            print("[WARN] No holdings data intercepted.")
            out = _HERE / "_webull_au_probe.json"
            out.write_text(json.dumps(all_api_calls, indent=2, default=str),
                           encoding="utf-8")
            print(f"  → Saved {len(all_api_calls)} API calls to {out}")

        await context.close()

    return captured


# ─────────────────────────────────────────────────────────────────────────────
#  Strategy 2: CDP — CONNECT TO EXISTING CHROME
#  User has Chrome running with --remote-debugging-port=9222
# ─────────────────────────────────────────────────────────────────────────────

async def _intercept_via_cdp(timeout_s: int = 60) -> dict:
    """
    Connect to an existing Chrome instance via CDP and intercept holdings.

    The user must have started Chrome with:
        chrome.exe --remote-debugging-port=9222

    This connects to the user's real Chrome profile where they are already
    logged in to Webull AU.
    """
    if not _PLAYWRIGHT_OK:
        raise ImportError("playwright not installed.")

    captured: dict = {}
    all_api_calls: list[dict] = []

    async with async_playwright() as p:
        print(f"[CDP] Connecting to Chrome on localhost:{_CDP_PORT} …")
        browser = await p.chromium.connect_over_cdp(f"http://localhost:{_CDP_PORT}")
        print(f"[CDP] Connected! Contexts: {len(browser.contexts)}")

        # Use the default context (user's real Chrome profile)
        context = browser.contexts[0] if browser.contexts else None
        if not context:
            print("[CDP] No browser context found.")
            await browser.close()
            return captured

        # Check if there's already a Webull tab open
        webull_page = None
        for pg in context.pages:
            if "webull.com.au" in pg.url:
                webull_page = pg
                print(f"[CDP] Found existing Webull tab: {pg.url}")
                break

        if not webull_page:
            # Open a new tab to the portfolio page
            webull_page = await context.new_page()
            print("[CDP] No Webull tab found — opening portfolio page …")

        on_response, on_request = _setup_response_interceptor(captured, all_api_calls)
        webull_page.on("response", on_response)
        webull_page.on("request", on_request)

        # Navigate to portfolio
        await webull_page.goto(_PORTFOLIO_URL, timeout=30_000)
        await webull_page.wait_for_timeout(3_000)

        # Check login status
        page_text = (await webull_page.inner_text("body")).lower()
        if "passport.webull.com.au" in webull_page.url:
            print("[CDP] Not logged in to Webull in Chrome. Please log in first.")
            await browser.close()
            return captured

        print(f"[CDP] On portfolio page: {webull_page.url}")

        # Extract tokens for direct API path
        await _extract_tokens_from_page(webull_page)

        await _wait_for_holdings(captured, timeout_s)

        captured["all_api_calls"] = all_api_calls

        if not captured.get("positions"):
            # Reload to force API calls
            print("[CDP] No holdings yet — refreshing …")
            await webull_page.reload(timeout=30_000)
            await webull_page.wait_for_timeout(3_000)
            await _wait_for_holdings(captured, timeout_s=30)

        # Don't close the browser — it's the user's Chrome!
        # Just disconnect cleanly
        await browser.close()

    return captured


# ─────────────────────────────────────────────────────────────────────────────
#  Legacy: Fresh browser intercept (kept for --probe and compatibility)
# ─────────────────────────────────────────────────────────────────────────────

async def _intercept_holdings(headless: bool = True,
                               timeout_s: int = 90) -> dict:
    """
    Launch Playwright, log in to app.webull.com.au via Google SSO (if Google
    cookies are present in the 'webull' file), intercept all JSON API calls,
    and return the first response that looks like a holdings/positions payload.

    Returns a dict with keys:
        'raw_api_url'  : str   — the exact URL that returned the data
        'positions'    : list  — parsed list of positions
        'raw_json'     : dict  — full raw JSON response
        'access_token' : str   — token captured from request headers
        'did'          : str   — did header value
    """
    if not _PLAYWRIGHT_OK:
        raise ImportError("playwright not installed. Run: pip install playwright && playwright install chromium")

    captured: dict = {}
    all_api_calls: list[dict] = []   # all webull.com.au JSON responses (for probe)

    google_cookies = _parse_google_cookies()
    if google_cookies:
        google_count = sum(1 for c in google_cookies if c.get("domain") == ".google.com")
        webull_count = sum(1 for c in google_cookies if c.get("domain") == ".webull.com.au")
        print(
            f"[SSO] Loaded {len(google_cookies)} cookies "
            f"(google={google_count}, webull={webull_count})"
        )
    else:
        print("[INFO] No cookies found in webull file — will attempt credential login")

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        # ── Pre-inject cookies from the webull credential file ───────────────
        if google_cookies:
            try:
                await context.add_cookies(google_cookies)
                print("[SSO] Cookies injected into browser context")
            except Exception as exc:
                print(f"[WARN] Could not inject cookies: {exc}")

        page: Page = await context.new_page()

        # ── Capture ALL webull*.com* JSON responses ─────────────────────────────
        async def on_response(response):
            url = response.url
            if "webull.com.au" not in url and "webullfintech.com" not in url:
                return
            try:
                body = await response.json()
                all_api_calls.append({"url": url, "status": response.status, "body": body})
                print(f"  [API] {response.status} {url}")
                # Check if this looks like a holdings/positions payload
                if not any(pat in url for pat in _HOLDINGS_PATTERNS):
                    return
                if not captured.get("positions"):
                    positions = (
                        body.get("positions")
                        or body.get("holdingList")
                        or (body.get("data") or {}).get("positions")
                        or (body.get("data") or {}).get("holdingList")
                        or []
                    )
                    if positions or "positions" in url or "holding" in url.lower():
                        captured["raw_api_url"] = url
                        captured["raw_json"]    = body
                        captured["positions"]   = positions
                        print(f"  [HOLDINGS] Captured {len(positions)} positions from {url}")
            except Exception:
                pass

        # ── Capture tokens from request headers ──────────────────────────────
        async def on_request(request):
            url = request.url
            if "webull.com.au" not in url and "webullfintech.com" not in url:
                return
            hdrs = request.headers
            if "access_token" in hdrs and not captured.get("access_token"):
                captured["access_token"] = hdrs["access_token"]
                captured["did"]          = hdrs.get("did", "")
                existing = _load_token()
                existing["accessToken"] = hdrs["access_token"]
                if hdrs.get("did"):
                    existing["did"] = hdrs["did"]
                    _save_did(hdrs["did"])
                _save_token(existing)
                print(f"  [TOKEN] access_token captured → saved to {_TOKEN_CACHE.name}")

        page.on("response", on_response)
        page.on("request",  on_request)

        # ── Navigate directly to portfolio page (requires login) ────────────
        print("[BROWSER] Navigating to https://www.webull.com.au/center/sc …")
        await page.goto("https://www.webull.com.au/center/sc", timeout=30_000)
        await page.wait_for_timeout(3_000)

        # ── Attempt Google SSO login ──────────────────────────────────────────
        await _try_google_sso_login(page)

        # ── Wait for holdings data ────────────────────────────────────────────
        print(f"[WAIT] Watching for holdings API call (up to {timeout_s}s) …")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if captured.get("positions") is not None:
                break
            await asyncio.sleep(1)
            remaining = int(deadline - time.time())
            if remaining % 10 == 0 and remaining > 0:
                print(f"  … {remaining}s remaining")

        captured["all_api_calls"] = all_api_calls

        if not captured.get("positions"):
            print("[WARN] No holdings data intercepted.  Saving all API calls for inspection.")
            out = Path(_HERE) / "_webull_au_probe.json"
            out.write_text(json.dumps(all_api_calls, indent=2, default=str), encoding="utf-8")
            print(f"  → {out}")

        await browser.close()

    return captured


async def _try_google_sso_login(page: Page) -> None:
    """
    Detect the Webull AU login page and click 'Sign in with Google'.
    Flow:
      1. Check if already logged in (portfolio page looks populated).
      2. Click the top-nav "LOG IN" button if visible (Webull home/marketing page).
      3. Click "Continue with Google" / "Sign in with Google" on the login form.
      4. If redirected to Google accounts page, fill credentials (cookies may have expired).
      5. Navigate back to /center/sc if needed.
    """
    try:
        current_url = page.url
        print(f"[SSO] Current URL: {current_url}")

        # Save screenshot for diagnosis
        try:
            await page.screenshot(path=str(_HERE / "_webull_login_screenshot.png"))
            print(f"  [SSO] Screenshot → _webull_login_screenshot.png")
        except Exception:
            pass

        # Step 0: Check if already logged in (no login UI shown)
        page_text = await page.inner_text("body")
        if (
            "sign in" not in page_text.lower()
            and "log in" not in page_text.lower()
            and "login" not in page_text.lower()
        ):
            print("  [SSO] Page appears to be already logged in.")
            return

        # Step 1: Click the top-nav "LOG IN" link if we're on the marketing/home page
        nav_login_selectors = [
            "a:has-text('LOG IN')",
            "a:has-text('Log In')",
            "a:has-text('Login')",
            "button:has-text('LOG IN')",
            "button:has-text('Log In')",
            "a[href*='/login']",
            "a[href*='/signin']",
        ]
        for sel in nav_login_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    print(f"  [SSO] Clicked nav LOGIN: {sel}")
                    await page.wait_for_timeout(2_000)
                    break
            except Exception:
                continue

        # Save screenshot after clicking LOG IN
        try:
            await page.screenshot(path=str(_HERE / "_webull_login2_screenshot.png"))
            print(f"  [SSO] Screenshot (post nav-click) → _webull_login2_screenshot.png")
        except Exception:
            pass

        # Step 2: Click "Continue with Google" / "Sign in with Google"
        google_btn_selectors = [
            "button:has-text('Continue with Google')",
            "button:has-text('Sign in with Google')",
            "button:has-text('Log in with Google')",
            "a:has-text('Continue with Google')",
            "a:has-text('Sign in with Google')",
            "[data-provider='google']",
            "[class*='google-btn']",
            "[class*='google-login']",
            "[class*='googleLogin']",
            "[class*='googleBtn']",
            # Broad fallback — visible element containing "Google" but not a store badge
            "button:has-text('Google')",
        ]

        clicked = False
        for sel in google_btn_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    print(f"  [SSO] Clicked Google SSO: {sel}")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # Fallback 1: icon-only social login buttons (no visible Google text)
            icon_selectors = [
                "div[class*='csr140'][class*='csr136']",  # observed Google icon class combo
                "div.csr140.csr136",
                "div[class*='csr140']",
            ]
            for sel in icon_selectors:
                try:
                    loc = page.locator(sel)
                    if await loc.count() >= 2:
                        await loc.nth(1).click()
                        print(f"  [SSO] Clicked Google icon via selector: {sel} [nth=1]")
                        clicked = True
                        break
                    if await loc.count() == 1:
                        await loc.first.click()
                        print(f"  [SSO] Clicked social icon via selector: {sel}")
                        clicked = True
                        break
                except Exception:
                    continue

        if not clicked:
            print("  [SSO] Google SSO button not found. Trying direct Webull email login.")
            try:
                await page.screenshot(path=str(_HERE / "_webull_no_google_btn.png"))
            except Exception:
                pass
            await _try_webull_email_login(page)
            return

        # Wait for Google OAuth redirect or popup
        oauth_page: Page | None = None
        try:
            oauth_page = await page.wait_for_event("popup", timeout=4_000)
            await oauth_page.wait_for_load_state("domcontentloaded")
            print(f"  [SSO] OAuth popup opened: {oauth_page.url}")
        except Exception:
            pass

        await page.wait_for_timeout(5_000)
        print(f"  [SSO] After SSO click, URL: {page.url}")

        # Step 3: If on Google accounts page (main tab or popup), fill credentials
        target_page = oauth_page or page
        if "accounts.google.com" in target_page.url or "google.com/o/oauth2" in target_page.url:
            print("  [SSO] On Google accounts page — filling credentials …")
            await _fill_google_login(target_page)
            await target_page.wait_for_timeout(5_000)
            print(f"  [SSO] Post-login URL: {target_page.url}")

        # If click happened but still on Webull login page, fallback to direct email login.
        if "passport.webull.com.au" in page.url:
            body_text = (await page.inner_text("body")).lower()
            if "log in to webull" in body_text:
                print("  [SSO] Still on Webull login page after Google click; trying email/password fallback.")
                await _try_webull_email_login(page)

        # Step 4: Navigate back to portfolio page if needed
        if "webull.com.au" in page.url and "/center/sc" not in page.url:
            print("  [SSO] Navigating to portfolio /center/sc …")
            await page.goto("https://www.webull.com.au/center/sc", timeout=20_000)
            await page.wait_for_timeout(4_000)

    except Exception as exc:
        print(f"  [SSO] Login step failed: {exc}")


def _load_login_creds() -> tuple[str, str]:
    """Load Webull login credentials from env vars or the local webull credential file."""
    import re as _re

    email_val, pass_val = _EMAIL, _PASSWORD
    if _GOOGLE_COOKIE_FILE.exists() and (not email_val or not pass_val):
        text = _GOOGLE_COOKIE_FILE.read_text(encoding="utf-8")
        m = _re.search(r'WEBULL_AU_EMAIL\s*=\s*["\']([^"\']+)["\']', text)
        if m and not email_val:
            email_val = m.group(1).strip()
        m = _re.search(r'WEBULL_AU_PASSWORD\s*=\s*["\']([^"\']+)["\']', text)
        if m and not pass_val:
            pass_val = m.group(1).strip()
    return email_val, pass_val


async def _fill_google_login(page: Page) -> None:
    """Fill Google's login form using credentials from env vars or the webull file."""
    email_val, pass_val = _load_login_creds()
    if not email_val:
        return

    try:
        await page.wait_for_selector("input[type='email']", timeout=6_000)
        await page.fill("input[type='email']", email_val)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2_000)

        if pass_val:
            await page.wait_for_selector("input[type='password']", timeout=6_000)
            await page.fill("input[type='password']", pass_val)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3_000)
            print("  [SSO] Google credentials submitted")
    except Exception as exc:
        print(f"  [SSO] Could not fill Google login form: {exc}")


async def _try_webull_email_login(page: Page) -> None:
    """
    Fallback login path for Webull AU native email/password form.
    Used when Google icon selectors are not detectable.
    """
    email_val, pass_val = _load_login_creds()
    if not email_val or not pass_val:
        print("  [SSO] No WEBULL_AU_EMAIL/WEBULL_AU_PASSWORD found for fallback login")
        return

    try:
        # Ensure Email Login tab is selected
        for sel in ["span:has-text('Email Login')", "div:has-text('Email Login')"]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                continue

        # Fill email/password fields on Webull login page
        if await page.locator("input[type='email']").count() > 0:
            await page.fill("input[type='email']", email_val)
        else:
            await page.fill("input[type='text']", email_val)

        await page.fill("input[type='password']", pass_val)

        # Click login button
        await page.click("button:has-text('Log In')")
        print("  [SSO] Submitted Webull email/password login")
        await page.wait_for_timeout(5_000)

        if "passport.webull.com.au" in page.url:
            print("  [SSO] Still on login page after submit (may require captcha/2FA).")
            try:
                await page.screenshot(path=str(_HERE / "_webull_email_login_after_submit.png"))
            except Exception:
                pass
    except Exception as exc:
        print(f"  [SSO] Direct Webull email login failed: {exc}")


async def _probe_all_api_calls(headless: bool = False, duration_s: int = 30) -> list[dict]:
    """
    Like _intercept_holdings but captures ALL webull.com.au API calls.
    Useful for discovering endpoint URLs.
    """
    if not _PLAYWRIGHT_OK:
        raise ImportError("playwright not installed.")

    found: list[dict] = []
    google_cookies = _parse_google_cookies()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        if google_cookies:
            await context.add_cookies(google_cookies)
            print(f"[PROBE] Injected {len(google_cookies)} cookies from webull file")

        page = await browser.new_page()

        async def on_response(response):
            url = response.url
            if "webull.com.au" not in url and "webullfintech.com" not in url:
                return
            try:
                body = await response.json()
                found.append({"url": url, "status": response.status, "body": body})
                print(f"  [API] {response.status} {url}")
            except Exception:
                pass

        page.on("response", on_response)

        print(f"[PROBE] Opening browser — will attempt Google SSO login …")
        await page.goto("https://www.webull.com.au/center/sc", timeout=30_000)
        await page.wait_for_timeout(2_000)
        await _try_google_sso_login(page)
        print(f"[PROBE] Watching for {duration_s}s …")
        await page.wait_for_timeout(duration_s * 1000)
        await browser.close()

    return found


# ─────────────────────────────────────────────────────────────────────────────
#  Public interface
# ─────────────────────────────────────────────────────────────────────────────

def get_holdings(prefer_browser: bool = True, headless: bool = True) -> list[dict]:
    """
    Return current holdings from Webull AU.

    Strategy chain (tried in order):
      1. Persistent profile (if .webull_profile/ exists)
      2. CDP connection (if Chrome is running with debug port)
      3. Direct REST API (if cached token or credentials exist)
      4. Prompt user to run --setup

    Returns a list of dicts.  Each dict (field names from AU platform) includes:
        ticker        str   — symbol, e.g. "CBA"
        tickerName    str   — company name
        qty           str   — quantity held
        costPrice     str   — average cost per share (AUD)
        mktValue      str   — current market value (AUD)
        unrealProfitLoss str — unrealised P&L
        currency      str   — "AUD", "USD", etc.
    """
    if not prefer_browser:
        # Skip browser strategies, go straight to API
        return _get_holdings_via_api()

    if not _PLAYWRIGHT_OK:
        print("[WARN] playwright not installed — using direct API only.")
        return _get_holdings_via_api()

    # ── Strategy 1: Persistent profile ────────────────────────────────────────
    if _PROFILE_DIR.exists():
        print("[STRATEGY 1] Using persistent browser profile …")
        try:
            result = asyncio.run(_intercept_via_persistent(
                headless=headless, timeout_s=60
            ))
            if result.get("session_expired"):
                print("[INFO] Session expired. Run: python webull_au_live.py --setup")
            else:
                positions = result.get("positions", [])
                if positions:
                    return positions
                print("[INFO] Persistent profile returned no positions.")
        except Exception as exc:
            print(f"[WARN] Persistent profile failed: {exc}")

    # ── Strategy 2: CDP (connect to existing Chrome) ──────────────────────────
    if _cdp_available():
        print("[STRATEGY 2] Connecting to Chrome via CDP …")
        try:
            result = asyncio.run(_intercept_via_cdp(timeout_s=45))
            positions = result.get("positions", [])
            if positions:
                return positions
            print("[INFO] CDP returned no positions.")
        except Exception as exc:
            print(f"[WARN] CDP connection failed: {exc}")

    # ── Strategy 3: Direct REST API ───────────────────────────────────────────
    print("[STRATEGY 3] Trying direct REST API …")
    positions = _get_holdings_via_api()
    if positions:
        return positions

    # ── No strategy worked ────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  No holdings could be fetched. Run setup first:")
    print("    python webull_au_live.py --setup")
    print()
    print("  This opens a browser for one-time manual login.")
    print("  After that, all future runs are fully automated.")
    print("=" * 60)
    return []


def _get_holdings_via_api() -> list[dict]:
    """Try the direct REST API path."""
    client = WebullAU()
    if not client._ensure_auth():
        return []
    try:
        return client.get_positions()
    except Exception as exc:
        print(f"[ERROR] Direct API call failed: {exc}")
        return []


def get_portfolio_summary() -> dict:
    """Return portfolio summary (total value, cash, etc.) via direct API."""
    client = WebullAU()
    if not client._ensure_auth():
        return {}
    try:
        return client.get_portfolio()
    except Exception as exc:
        print(f"[ERROR] get_portfolio_summary failed: {exc}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────

def _print_holdings_table(holdings: list[dict]) -> None:
    if not holdings:
        print("No holdings returned.")
        return
    print(f"\n{'Ticker':<12} {'Name':<30} {'Qty':>8} {'Cost':>10} {'Mkt Val':>12} {'P&L':>12}")
    print("-" * 86)
    for h in holdings:
        ticker = h.get("ticker", h.get("symbol", h.get("disSymbol", "?")))
        name   = (h.get("tickerName", h.get("name", "")) or "")[:29]
        qty    = h.get("qty", h.get("quantity", "?"))
        cost   = h.get("costPrice",  h.get("cost",   "?"))
        mval   = h.get("mktValue",   h.get("marketValue", "?"))
        pnl    = h.get("unrealProfitLoss", h.get("unrealizedProfitLoss", "?"))
        print(f"{ticker:<12} {name:<30} {qty:>8} {cost:>10} {mval:>12} {pnl:>12}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Webull AU live holdings scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quick start:
  python webull_au_live.py --setup      # ONE-TIME: log in manually, save session
  python webull_au_live.py --holdings   # fetch holdings (fully automated)

Alternative (CDP — connect to your existing Chrome):
  1. Launch Chrome:  chrome.exe --remote-debugging-port=9222
  2. Log in to webull.com.au in Chrome
  3. Run:  python webull_au_live.py --cdp
""",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--setup",       action="store_true",
                       help="ONE-TIME: open browser, log in manually, save session for future runs")
    group.add_argument("--holdings",    action="store_true",
                       help="Print current holdings (default) — auto-picks best strategy")
    group.add_argument("--cdp",         action="store_true",
                       help="Fetch holdings via CDP (Chrome must be running with --remote-debugging-port=9222)")
    group.add_argument("--probe",       action="store_true",
                       help="Open visible browser and dump ALL webull.com.au API calls")
    group.add_argument("--token",       action="store_true",
                       help="Print the captured / cached access_token")
    group.add_argument("--portfolio",   action="store_true",
                       help="Print portfolio-level totals")
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip browser, use direct API only (requires token)")
    parser.add_argument("--headful",    action="store_true",
                        help="Show browser window (helpful when debugging)")
    parser.add_argument("--probe-secs", type=int, default=60,
                        help="How many seconds to wait in probe mode (default 60)")
    args = parser.parse_args()

    if args.setup:
        if not _PLAYWRIGHT_OK:
            print("[ERROR] playwright not installed. Run: pip install playwright && playwright install chromium")
            sys.exit(1)
        result = asyncio.run(_intercept_via_persistent(setup_mode=True))
        positions = result.get("positions", [])
        if positions:
            print(f"\n✓ Captured {len(positions)} holdings during setup!")
            _print_holdings_table(positions)
        sys.exit(0)

    if args.cdp:
        if not _PLAYWRIGHT_OK:
            print("[ERROR] playwright not installed.")
            sys.exit(1)
        if not _cdp_available():
            print("[ERROR] Chrome is not running with --remote-debugging-port=9222")
            print()
            print("  Start Chrome with:  chrome.exe --remote-debugging-port=9222")
            print("  Then log in to https://www.webull.com.au")
            print("  Then run this command again.")
            sys.exit(1)
        result = asyncio.run(_intercept_via_cdp(timeout_s=60))
        _print_holdings_table(result.get("positions", []))
        sys.exit(0)

    if args.token:
        cached = _load_token()
        tok = cached.get("accessToken") or _ACCESS_TOKEN_ENV or "(none)"
        print(f"access_token : {tok}")
        print(f"did          : {cached.get('did') or _DID_ENV or _load_did()}")
        print(f"expires      : {cached.get('tokenExpireTime', 'unknown')}")
        sys.exit(0)

    if args.probe:
        if not _PLAYWRIGHT_OK:
            print("[ERROR] playwright not installed. Run: pip install playwright && playwright install chromium")
            sys.exit(1)
        print(f"[PROBE] Collecting API calls for {args.probe_secs}s …")
        calls = asyncio.run(_probe_all_api_calls(headless=False, duration_s=args.probe_secs))
        out_file = _HERE / "_webull_au_probe.json"
        out_file.write_text(json.dumps(calls, indent=2, default=str), encoding="utf-8")
        print(f"\n[PROBE] Captured {len(calls)} API calls → {out_file}")
        sys.exit(0)

    if args.portfolio:
        summary = get_portfolio_summary()
        print(json.dumps(summary, indent=2))
        sys.exit(0)

    # Default: --holdings
    prefer = not args.no_browser
    headless = not args.headful
    _print_holdings_table(get_holdings(prefer_browser=prefer, headless=headless))
