#!/usr/bin/env python3
"""
Webull AU — Official OpenAPI Client (Live Holdings, pure HTTP)
======================================================
Replaces the PDF-statement / browser-automation approach for CURRENT HOLDINGS
with Webull's official OpenAPI: https://developer.webull.com.au/apis/docs/

Implemented via raw HTTP + manual HMAC-SHA256 request signing — NOT the
official `webull-openapi-python-sdk` package. That SDK requires Python
3.8-3.13 (`Requires-Python >=3.8,<3.14`) and CANNOT be installed under this
project's Python 3.14 interpreter (verified: pip install fails with "No
matching distribution found"). The signing algorithm below follows
https://developer.webull.com.au/apis/docs/authentication/signature exactly
(HMAC-SHA256, no external dependency beyond `requests` which is already a
project requirement) — this also means the Mac Mini doesn't need the SDK
installed either, regardless of its Python version.

This module is the single source of truth for talking to the Webull AU
OpenAPI. It is imported by:
  • global_portfolio_dashboard.py (_fetch_webull_api / _fetch_webull)
  • tradingview/sell/sell_strategy.py (_fetch_webull_positions_api)

Historical trade parsing for Australian CGT (FIFO cost-basis matching across
tax years) still uses webull_statement_reader.py / webull_cgt_analysis.py —
the OpenAPI only exposes CURRENT positions/balance, not the full multi-year
trade ledger needed for tax lodgement.

MARKET COVERAGE:
The Account Positions endpoint has been verified live to return US, AU, and HK
holdings for the Webull AU account. The "Supported Markets and Products" table
appears to describe order placement rather than this read-only endpoint.
Current-holdings callers treat a successful API response as authoritative and
use PDF statements only when the API is unavailable.

─────────────────────────────────────────────────────────────────────────────
ONE-TIME SETUP
─────────────────────────────────────────────────────────────────────────────
1. Apply for API access (approval takes 1-2 business days):
       https://www.webull.com.au/open-api-management
   Individual Application Process:
       https://developer.webull.com.au/apis/docs/authentication/individual-application

2. After approval, register an application under API Keys Management to
   generate your App Key + App Secret.

3. Set environment variables (see below).

4. First call triggers 2FA — this module blocks/polls (see get_access_token)
    until you approve the push/SMS prompt in the Webull App (5 min window).
    The verified token and its post-verification expiry are cached to disk so
    subsequent runs need no interaction (tokens are valid ~15 days).

Environment variables:
    WEBULL_AU_APP_KEY        (required)  App Key from Webull OpenAPI Management
    WEBULL_AU_APP_SECRET     (required)  App Secret — NEVER sent over the wire,
                                         used locally only to compute the
                                         HMAC-SHA256 request signature
    WEBULL_AU_ACCOUNT_ID     (optional)  Skips the get_account_list() lookup
    WEBULL_AU_API_ENDPOINT   (optional)  Default: api.webull.com.au (production)
                                         Use au-api.uat.webullbroker.com for the
                                         shared test accounts (see SDK docs)

─────────────────────────────────────────────────────────────────────────────
USAGE
─────────────────────────────────────────────────────────────────────────────
    # From Python
    from webull_au_api import is_configured, get_holdings, get_balance
    if is_configured():
        positions, status = get_holdings()

    # From command line (also used to complete the one-time 2FA setup)
    python webull_au_api.py --accounts    # list accounts (also verifies 2FA)
    python webull_au_api.py --balance     # show account balance
    python webull_au_api.py --holdings    # show current holdings (US only)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths (cross-platform — no OS-specific assumptions) ───────────────────────
_HERE = Path(__file__).parent
_TOKEN_FILE = _HERE / ".webull_api_token" / "token.json"   # gitignored

# ── Credentials (env vars, with hardcoded fallback defaults) ──────────────────
# NOTE: hardcoded fallback added at explicit user request (2026-07-11) so the
# dashboard works when run directly (double-click / no shell env vars set).
# Same pattern as the ANGELONE_* os.environ.setdefault() fallbacks used
# elsewhere in this codebase. Explicit env vars still take precedence.
os.environ.setdefault("WEBULL_AU_APP_KEY", "")
os.environ.setdefault("WEBULL_AU_APP_SECRET", "")

_APP_KEY      = os.environ.get("WEBULL_AU_APP_KEY", "").strip()
_APP_SECRET   = os.environ.get("WEBULL_AU_APP_SECRET", "").strip()
_ACCOUNT_ID   = os.environ.get("WEBULL_AU_ACCOUNT_ID", "").strip()
_API_HOST     = (os.environ.get("WEBULL_AU_API_ENDPOINT", "api.webull.com.au").strip()
                 or "api.webull.com.au")

_BASE_URL            = f"https://{_API_HOST}"
_SIGNATURE_ALGORITHM = "HMAC-SHA256"
_SIGNATURE_VERSION   = "1.0"
_API_VERSION         = "v2"
_HTTP_TIMEOUT        = 30  # seconds

_account_id_cache: str | None = None


def is_configured() -> bool:
    """True when the minimum required credentials are present."""
    return bool(_APP_KEY) and bool(_APP_SECRET)


# ─────────────────────────────────────────────────────────────────────────────
# Request signing — see developer.webull.com.au/apis/docs/authentication/signature
# ─────────────────────────────────────────────────────────────────────────────

def _generate_signature(path: str, query_params: dict, body_string: str | None,
                         timestamp: str, nonce: str) -> str:
    """3-step HMAC-SHA256 signature: sorted(query+signing headers) -> optional
    SHA256(body) -> HMAC-SHA256(app_secret + '&', url-encoded string) -> base64."""
    signing_headers = {
        "x-app-key": _APP_KEY,
        "x-timestamp": timestamp,
        "x-signature-algorithm": _SIGNATURE_ALGORITHM,
        "x-signature-version": _SIGNATURE_VERSION,
        "x-signature-nonce": nonce,
        "host": _API_HOST,
    }
    all_params: dict = {}
    all_params.update(query_params or {})
    all_params.update(signing_headers)
    str1 = "&".join(f"{k}={all_params[k]}" for k in sorted(all_params.keys()))

    if body_string:
        str2 = hashlib.sha256(body_string.encode("utf-8")).hexdigest().upper()
        str3 = f"{path}&{str1}&{str2}"
    else:
        str3 = f"{path}&{str1}"

    encoded_string = urllib.parse.quote(str3, safe="")
    signing_key = f"{_APP_SECRET}&"
    signature = base64.b64encode(
        hmac.new(signing_key.encode("utf-8"), encoded_string.encode("utf-8"),
                  hashlib.sha256).digest()
    ).decode("utf-8")
    return signature


def _build_headers(path: str, query_params: dict, body_string: str | None,
                    access_token: str | None) -> dict:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = uuid.uuid4().hex
    signature = _generate_signature(path, query_params, body_string, timestamp, nonce)
    headers = {
        "x-app-key": _APP_KEY,
        "x-timestamp": timestamp,
        "x-signature": signature,
        "x-signature-algorithm": _SIGNATURE_ALGORITHM,
        "x-signature-version": _SIGNATURE_VERSION,
        "x-signature-nonce": nonce,
        "x-version": _API_VERSION,
        "Accept": "application/json",
    }
    if access_token:
        headers["x-access-token"] = access_token
    if body_string is not None:
        headers["Content-Type"] = "application/json"
    return headers


def _call(method: str, path: str, query_params: dict | None = None,
          body: dict | None = None, access_token: str | None = None) -> requests.Response:
    """Sign and send one API request. NOTE: app_secret is never sent as a
    header — it is used solely on the client side to compute x-signature."""
    query_params = query_params or {}
    body_string = json.dumps(body, separators=(",", ":")) if body is not None else None
    headers = _build_headers(path, query_params, body_string, access_token)
    url = f"{_BASE_URL}{path}"
    if method.upper() == "GET":
        return requests.get(url, headers=headers, params=query_params, timeout=_HTTP_TIMEOUT)
    return requests.post(url, headers=headers, data=body_string, timeout=_HTTP_TIMEOUT)


# ─────────────────────────────────────────────────────────────────────────────
# Token lifecycle — see developer.webull.com.au/apis/docs/authentication/token
# ─────────────────────────────────────────────────────────────────────────────

def _load_cached_token() -> tuple[str, int] | None:
    try:
        if _TOKEN_FILE.exists():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            token = data.get("token")
            expires = int(data.get("expires", 0))
            if token:
                return token, expires
    except Exception:
        pass
    return None


def _save_token(token: str, expires: int) -> None:
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(json.dumps({"token": token, "expires": expires}), encoding="utf-8")
    except Exception:
        pass


def _create_token() -> tuple[str | None, int, str]:
    """POST /openapi/auth/token/create -> (token, expires_ms, status)."""
    res = _call("POST", "/openapi/auth/token/create", body={})
    if res.status_code != 200:
        raise RuntimeError(f"create_token failed: HTTP {res.status_code} — {res.text[:300]}")
    data = res.json() or {}
    return data.get("token"), int(data.get("expires", 0) or 0), data.get("status", "")


def _check_token(token: str) -> tuple[str | None, int, str]:
    """POST /openapi/auth/token/check -> (token, expires_ms, status)."""
    res = _call("POST", "/openapi/auth/token/check", body={"token": token})
    if res.status_code != 200:
        raise RuntimeError(f"check_token failed: HTTP {res.status_code} — {res.text[:300]}")
    data = res.json() or {}
    return data.get("token"), int(data.get("expires", 0) or 0), data.get("status", "")


def get_access_token(wait_for_2fa: bool = True, poll_interval: float = 5.0,
                      timeout: float = 300.0) -> str:
    """
    Return a valid (NORMAL) access token, creating + verifying one if needed.
    On first-ever call, this BLOCKS (polls _check_token) until the user
    approves the push/SMS prompt in the Webull App, or `timeout` elapses
    (Webull expires unverified tokens after 5 minutes).
    """
    token: str | None = None
    expires = 0
    status = ""
    cached = _load_cached_token()
    if cached:
        token, expires = cached
        if time.time() * 1000 < (expires - 60_000):  # 60s safety margin
            return token
        try:
            checked_token, checked_expires, status = _check_token(token)
        except Exception as exc:
            raise RuntimeError(
                "Cached Webull token validation failed; refusing to create a "
                "replacement token because that would trigger 2FA."
            ) from exc
        if status == "NORMAL":
            token = checked_token or token
            if checked_expires <= time.time() * 1000:
                raise RuntimeError("check_token returned NORMAL without a future expiry")
            _save_token(token, checked_expires)
            return token
        if status in ("INVALID", "EXPIRED"):
            token = None
        elif status == "PENDING":
            expires = checked_expires or expires
        else:
            raise RuntimeError(f"Cached Webull token has unknown status={status!r}")

    if token is None:
        token, expires, status = _create_token()
        if not token:
            raise RuntimeError(f"create_token returned no token (status={status!r})")

    if status == "NORMAL":
        _save_token(token, expires)
        return token

    if not wait_for_2fa:
        raise RuntimeError(
            f"Token status={status} — approve the verification prompt in the "
            "Webull App (Menu -> Messages -> OpenAPI Notifications), then retry."
        )

    print(f"Webull AU: token status={status} — approve the OpenAPI verification "
          f"prompt in your Webull App now (Menu -> Messages -> OpenAPI "
          f"Notifications). Waiting up to {int(timeout)}s...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            checked_token, checked_expires, status = _check_token(token)
        except Exception as exc:
            # Transient network hiccup during polling must NOT abort the whole
            # wait — only a definitive INVALID/EXPIRED status (below) should.
            print(f"Webull AU: check_token transient error ({exc}) — retrying...")
            continue
        if status == "NORMAL":
            token = checked_token or token
            if checked_expires <= time.time() * 1000:
                raise RuntimeError("check_token returned NORMAL without a future expiry")
            # The create response expires after the five-minute verification
            # window; NORMAL status supplies the actual ~15-day token expiry.
            _save_token(token, checked_expires)
            print("Webull AU: token verified and cached.")
            return token
        if status in ("INVALID", "EXPIRED"):
            raise RuntimeError(f"Token {status.lower()} while waiting for verification "
                                "— run again to create a new one.")
    raise RuntimeError("Timed out waiting for Webull App verification (5 min window). "
                        "Run again to create a new token.")


# ─────────────────────────────────────────────────────────────────────────────
# Account / positions / balance
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_account_id(access_token: str) -> str | None:
    """Return WEBULL_AU_ACCOUNT_ID if set, else the first account from
    /openapi/account/list (cached per-process to avoid repeat HTTP calls)."""
    global _account_id_cache
    if _ACCOUNT_ID:
        return _ACCOUNT_ID
    if _account_id_cache:
        return _account_id_cache
    res = _call("GET", "/openapi/account/list", access_token=access_token)
    if res.status_code != 200:
        return None
    accounts = res.json() or []
    if not accounts:
        return None
    _account_id_cache = accounts[0].get("account_id")
    return _account_id_cache


def _exchange_for_currency(currency: str) -> str:
    """Best-effort exchange label from currency — the Account Positions
    response does not include an explicit exchange/market field."""
    currency = (currency or "").upper()
    if currency == "AUD":
        return "AU"
    if currency in ("HKD", "CNH"):
        return "HK"
    if currency == "SGD":
        return "SG"
    return "US"


def get_accounts() -> tuple[list[dict], str]:
    """Return (accounts, status_msg). Also the simplest way to trigger/verify
    the one-time 2FA handshake."""
    if not is_configured():
        return [], "API not configured (WEBULL_AU_APP_KEY/APP_SECRET missing)"
    try:
        token = get_access_token()
        res = _call("GET", "/openapi/account/list", access_token=token)
        if res.status_code != 200:
            return [], f"API error: HTTP {res.status_code} — {res.text[:200]}"
        accounts = res.json() or []
        return accounts, f"OK — {len(accounts)} account(s)"
    except Exception as e:
        return [], f"API error: {e}"


def get_balance(account_id: str | None = None) -> tuple[dict | None, str]:
    """Return (balance_dict, status_msg) for the Account Balance endpoint."""
    if not is_configured():
        return None, "API not configured (WEBULL_AU_APP_KEY/APP_SECRET missing)"
    try:
        token = get_access_token()
        acc_id = account_id or _resolve_account_id(token)
        if not acc_id:
            return None, "API error: could not resolve account_id"
        res = _call("GET", "/openapi/assets/balance", query_params={"account_id": acc_id},
                    access_token=token)
        if res.status_code != 200:
            return None, f"API error: HTTP {res.status_code} — {res.text[:200]}"
        return res.json() or {}, "OK"
    except Exception as e:
        return None, f"API error: {e}"


def get_holdings(account_id: str | None = None) -> tuple[list[dict], str]:
    """
    Fetch current Webull positions via the official OpenAPI.

    Returns (positions, status_msg) in the SAME shape produced by the PDF-based
    fetchers (global_portfolio_dashboard._fetch_webull_pdf /
    sell_strategy._fetch_webull_positions), so callers can combine sources
    with zero downstream changes:
        {symbol, shares, avg_cost, current_price, market_val, pnl, pnl_pct,
         currency, broker, exchange}

    Field mapping is per the official schema (developer.webull.com.au
    /apis/docs/reference/account-position):
        symbol, quantity, cost_price, last_price, currency,
        unrealized_profit_loss   (quantity/cost_price/last_price/upl are strings)
    """
    if not is_configured():
        return [], "API not configured (WEBULL_AU_APP_KEY/APP_SECRET missing)"

    try:
        token = get_access_token()
        acc_id = account_id or _resolve_account_id(token)
        if not acc_id:
            return [], "API error: could not resolve account_id"

        res = _call("GET", "/openapi/assets/positions", query_params={"account_id": acc_id},
                    access_token=token)
        if res.status_code != 200:
            return [], f"API error: HTTP {res.status_code} — {res.text[:200]}"

        raw_positions = res.json() or []
        positions = []
        for p in raw_positions:
            symbol = str(p.get("symbol", "")).strip().upper()
            try:
                qty = float(p.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if not symbol or qty <= 0:
                continue

            def _num(key: str) -> float:
                try:
                    return float(p.get(key) or 0)
                except (TypeError, ValueError):
                    return 0.0

            cost = _num("cost_price")
            last = _num("last_price")
            currency = str(p.get("currency", "USD")).upper()

            mval = (last * qty) if last else (cost * qty)
            cost_basis = cost * qty
            _upl_raw = p.get("unrealized_profit_loss")
            pnl = _num("unrealized_profit_loss") if _upl_raw not in (None, "") else (mval - cost_basis)
            pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0.0

            positions.append({
                "symbol":        symbol,
                "shares":        int(qty) if qty == int(qty) else qty,
                "avg_cost":      round(cost, 4),
                "current_price": round(last, 4),
                "market_val":    round(mval, 2),
                "pnl":           round(pnl, 2),
                "pnl_pct":       round(pnl_pct, 1),
                "currency":      currency,
                "broker":        "Webull AU",
                "exchange":      _exchange_for_currency(currency),
            })

        return positions, f"Live API — {len(positions)} positions"
    except Exception as e:
        return [], f"API error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# CLI — manual setup verification / one-time 2FA completion
# ─────────────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    if not is_configured():
        print("NOT CONFIGURED:")
        if not _APP_KEY:
            print("  - WEBULL_AU_APP_KEY env var not set")
        if not _APP_SECRET:
            print("  - WEBULL_AU_APP_SECRET env var not set")
        sys.exit(1)

    if args.accounts:
        accounts, status = get_accounts()
        print(f"Status: {status}")
        print(json.dumps(accounts, indent=2, default=str))
    elif args.balance:
        balance, status = get_balance()
        print(f"Status: {status}")
        print(json.dumps(balance, indent=2, default=str))
    elif args.holdings:
        positions, status = get_holdings()
        print(f"Status: {status}")
        print(json.dumps(positions, indent=2, default=str))
    else:
        print("Nothing to do — pass --accounts, --balance, or --holdings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Webull AU OpenAPI — manual setup verification / diagnostics"
    )
    parser.add_argument("--accounts", action="store_true", help="List accounts (also verifies 2FA)")
    parser.add_argument("--balance", action="store_true", help="Show account balance")
    parser.add_argument("--holdings", action="store_true", help="Show current holdings")
    main(parser.parse_args())
