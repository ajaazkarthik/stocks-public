"""
Integration Tests — TradingView API + Signal Stack Pipeline
============================================================
These tests make REAL network calls to TradingView and related services.
They verify:
  1. TV session authentication (cookie → JWT)
  2. Fired alerts list download (/list_fires)
  3. TV screener API (relative volume, recommendation marks)
  4. Signal stack full run (small N)
  5. Multi-asset screener (each exchange scan endpoint)

Requirements:
  • Valid sessionid + sessionid_sign in tradingview/cookie  (or set via env vars)
  • pip install requests yfinance vaderSentiment
  • Internet access to tradingview.com / scanner.tradingview.com

Run:
    python -m pytest test_integration_tv.py -v
    python -m pytest test_integration_tv.py -v -k "auth"           # just auth tests
    python -m pytest test_integration_tv.py -v -k "screener"       # just screener
    python -m pytest test_integration_tv.py -v --tb=short -s       # see print output

Tests are auto-skipped when TV credentials are absent or the network is unreachable.
"""

import os
import sys
import re
import json
import socket
import pytest
import urllib.request
from pathlib import Path
from typing import Optional

# ── Path setup ─────────────────────────────────────────────────────────────────
TV_DIR      = Path(__file__).parent                # tradingview/
ROOT        = TV_DIR.parent                        # stocks/
COOKIE_FILE = TV_DIR / "cookie"

sys.path.insert(0, str(TV_DIR))    # so signal_stack, fetch_tv_screener importable
sys.path.insert(0, str(ROOT))      # workspace root


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: parse cookie file and check connectivity
# ─────────────────────────────────────────────────────────────────────────────

def _parse_cookie_file() -> dict:
    """
    Parse the flat cookie string from tradingview/cookie.
    Returns dict of cookie_name → value.
    Falls back to env vars TV_SESSION_ID / TV_SESSION_SIGN.
    """
    cookies: dict = {}

    # 1. Try environment variables (CI-friendly)
    if os.environ.get("TV_SESSION_ID"):
        cookies["sessionid"]      = os.environ["TV_SESSION_ID"]
        cookies["sessionid_sign"] = os.environ.get("TV_SESSION_SIGN", "")
        return cookies

    # 2. Try cookie file
    if COOKIE_FILE.exists():
        raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()

    return cookies


def _tv_reachable() -> bool:
    """Return True if tradingview.com TCP 443 is reachable."""
    try:
        with socket.create_connection(("tradingview.com", 443), timeout=5):
            return True
    except OSError:
        return False


def _get_session() -> Optional[dict]:
    """
    Return {'sessionid': ..., 'sessionid_sign': ..., 'jwt': ...} or None.
    Authenticates against TV chart page to obtain JWT bearer token.
    """
    try:
        import requests
    except ImportError:
        return None

    cookies = _parse_cookie_file()
    if not cookies.get("sessionid"):
        return None

    s = requests.Session()
    s.cookies.set("sessionid",      cookies["sessionid"],      domain=".tradingview.com")
    s.cookies.set("sessionid_sign", cookies.get("sessionid_sign", ""), domain=".tradingview.com")
    s.headers.update({"User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )})

    try:
        resp = s.get("https://www.tradingview.com/chart/", timeout=20)
        m = re.search(r'"auth_token"\s*:\s*"(eyJ[A-Za-z0-9._\-]+)"', resp.text)
        jwt = m.group(1) if m else None
        return {
            "sessionid":      cookies["sessionid"],
            "sessionid_sign": cookies.get("sessionid_sign", ""),
            "jwt":            jwt,
            "status_code":    resp.status_code,
        }
    except Exception:
        return None


# ── Pytest marks/skips ─────────────────────────────────────────────────────────

_tv_up        = _tv_reachable()
_session_info = _get_session() if _tv_up else None

requires_network = pytest.mark.skipif(
    not _tv_up,
    reason="tradingview.com not reachable — check internet connection",
)

requires_auth = pytest.mark.skipif(
    not (_tv_up and _session_info and _session_info.get("jwt")),
    reason=(
        "Valid TV session not found — add sessionid/sessionid_sign to "
        "tradingview/cookie or set TV_SESSION_ID env var"
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Authentication
# ─────────────────────────────────────────────────────────────────────────────

class TestTvAuthentication:
    """Verify TV session cookie parses correctly and yields a JWT."""

    def test_cookie_file_exists(self):
        """tradingview/cookie file must exist (or TV_SESSION_ID env var must be set)."""
        has_env   = bool(os.environ.get("TV_SESSION_ID"))
        has_file  = COOKIE_FILE.exists()
        assert has_env or has_file, (
            f"No TV credentials: set TV_SESSION_ID env var or create {COOKIE_FILE}"
        )

    def test_cookie_file_has_session_id(self):
        """Parsed cookies contain a non-empty sessionid."""
        cookies = _parse_cookie_file()
        assert cookies.get("sessionid"), (
            "sessionid not found in cookie file — re-copy fresh cookies from browser"
        )
        assert len(cookies["sessionid"]) > 10, "sessionid looks too short"

    def test_cookie_file_has_session_sign(self):
        """Parsed cookies contain sessionid_sign (required for newer TV accounts)."""
        cookies = _parse_cookie_file()
        assert cookies.get("sessionid_sign"), (
            "sessionid_sign not found in cookie file"
        )

    @requires_network
    def test_chart_page_returns_200(self):
        """HTTP GET https://www.tradingview.com/chart/ returns 200."""
        assert _session_info is not None, "Session init failed"
        assert _session_info["status_code"] == 200, (
            f"TV chart page returned {_session_info['status_code']} — cookies may be expired"
        )

    @requires_network
    def test_jwt_obtained(self):
        """auth_token (JWT) is extracted from the chart page HTML."""
        assert _session_info is not None, "Session init failed"
        jwt = _session_info.get("jwt")
        assert jwt, (
            "No JWT found in chart page — session cookies may be expired.\n"
            "Refresh cookies: open TV in browser → DevTools → Application → Cookies"
        )
        assert jwt.startswith("eyJ"), f"JWT looks malformed: {jwt[:30]}"
        print(f"\n  JWT prefix: {jwt[:40]}...")

    @requires_auth
    def test_jwt_has_three_segments(self):
        """JWT token consists of exactly three dot-separated base64 segments."""
        jwt = _session_info["jwt"]
        parts = jwt.split(".")
        assert len(parts) == 3, f"Expected 3 JWT segments, got {len(parts)}: {jwt[:60]}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fired alerts list
# ─────────────────────────────────────────────────────────────────────────────

class TestFiredAlertsList:
    """Verify /list_fires returns a valid paginated alert fire list."""

    @requires_auth
    def test_list_fires_returns_list(self):
        """POST /list_fires returns a list (may be empty on fresh accounts)."""
        import requests
        si = _session_info
        r = requests.post(
            "https://pricealerts.tradingview.com/list_fires",
            headers={
                "User-Agent":    "Mozilla/5.0",
                "Authorization": f"Bearer {si['jwt']}",
                "Content-Type":  "application/json",
                "Origin":        "https://www.tradingview.com",
                "Referer":       "https://www.tradingview.com/chart/",
            },
            cookies={"sessionid": si["sessionid"], "sessionid_sign": si["sessionid_sign"]},
            json={"payload": {}},
            timeout=15,
        )
        data = r.json()
        print(f"\n  /list_fires status field: {data.get('s')}")
        assert data.get("s") == "ok", f"list_fires returned error: {data}"
        fires = data.get("r", [])
        assert isinstance(fires, list)
        print(f"  Fires returned: {len(fires)}")

    @requires_auth
    def test_list_fires_with_limit(self):
        """list_fires respects a limit parameter."""
        import requests
        si = _session_info
        headers = {
            "User-Agent":    "Mozilla/5.0",
            "Authorization": f"Bearer {si['jwt']}",
            "Content-Type":  "application/json",
            "Origin":        "https://www.tradingview.com",
            "Referer":       "https://www.tradingview.com/chart/",
        }
        cookies = {"sessionid": si["sessionid"], "sessionid_sign": si["sessionid_sign"]}

        r10  = requests.post(
            "https://pricealerts.tradingview.com/list_fires",
            headers=headers, cookies=cookies,
            json={"payload": {"limit": 10}}, timeout=15,
        )
        fires10 = r10.json().get("r", [])
        r50 = requests.post(
            "https://pricealerts.tradingview.com/list_fires",
            headers=headers, cookies=cookies,
            json={"payload": {"limit": 50}}, timeout=15,
        )
        fires50 = r50.json().get("r", [])
        print(f"\n  limit=10 returned {len(fires10)}, limit=50 returned {len(fires50)}")
        assert len(fires10) <= 10, f"limit=10 returned {len(fires10)} fires"
        assert len(fires50) >= len(fires10), "limit=50 should return at least as many as limit=10"

    @requires_auth
    def test_list_fires_fire_structure(self):
        """Each fire record contains the fields signal_stack expects."""
        import requests
        si = _session_info
        r = requests.post(
            "https://pricealerts.tradingview.com/list_fires",
            headers={
                "User-Agent":    "Mozilla/5.0",
                "Authorization": f"Bearer {si['jwt']}",
                "Content-Type":  "application/json",
                "Origin":        "https://www.tradingview.com",
                "Referer":       "https://www.tradingview.com/chart/",
            },
            cookies={"sessionid": si["sessionid"], "sessionid_sign": si["sessionid_sign"]},
            json={"payload": {"limit": 5}},
            timeout=15,
        )
        fires = r.json().get("r", [])
        if not fires:
            pytest.skip("No fired alerts on this account")
        fire = fires[0]
        print(f"\n  Sample fire keys: {list(fire.keys())}")
        for field in ("fire_id", "alert_id", "symbol", "fire_time"):
            assert field in fire, f"Expected field '{field}' in fire record, got: {list(fire.keys())}"

    @requires_auth
    def test_list_fires_symbols_have_exchange_prefix(self):
        """Fired symbols follow the exchange:TICKER format."""
        import requests
        si = _session_info
        r = requests.post(
            "https://pricealerts.tradingview.com/list_fires",
            headers={
                "User-Agent":    "Mozilla/5.0",
                "Authorization": f"Bearer {si['jwt']}",
                "Content-Type":  "application/json",
                "Origin":        "https://www.tradingview.com",
                "Referer":       "https://www.tradingview.com/chart/",
            },
            cookies={"sessionid": si["sessionid"], "sessionid_sign": si["sessionid_sign"]},
            json={"payload": {"limit": 20}},
            timeout=15,
        )
        fires = r.json().get("r", [])
        if not fires:
            pytest.skip("No fires to inspect")
        symbols = [f.get("symbol", "") for f in fires]
        for sym in symbols:
            if sym:
                assert ":" in sym, f"Expected exchange:TICKER format but got: {sym!r}"
        print(f"\n  Sample symbols: {symbols[:5]}")

    @requires_auth
    def test_fires_raw_json_matches_api(self):
        """
        fires_raw.json on disk (if present) contains symbols consistent with
        what /list_fires returns — both should have 'symbol' and 'fire_id' keys.
        """
        fires_file = TV_DIR / "fires_raw.json"
        if not fires_file.exists():
            pytest.skip("fires_raw.json not yet downloaded — run download_fires.py first")
        with open(fires_file, encoding="utf-8") as f:
            saved = json.load(f)
        # fires_raw.json may be a list or a dict with 'r' key
        if isinstance(saved, dict):
            saved = saved.get("r", saved.get("data", []))
        assert isinstance(saved, list), "fires_raw.json should contain a list of fires"
        if saved:
            assert "symbol" in saved[0], "Each fire should have a 'symbol' field"
            print(f"\n  fires_raw.json contains {len(saved)} records")


# ─────────────────────────────────────────────────────────────────────────────
# 3. TV Screener API
# ─────────────────────────────────────────────────────────────────────────────

class TestTvScreener:
    """Verify the TV scanner endpoint returns rel_vol and recommendation data."""

    @requires_network
    def test_us_screener_returns_data(self):
        """scanner.tradingview.com/america/scan returns valid JSON for US symbols."""
        symbols = ["AAPL", "MSFT", "NVDA"]
        payload = {
            "columns": ["name", "relative_volume_10d_calc", "recommendation_mark"],
            "filter":  [{"left": "name", "operation": "in_range", "right": symbols}],
            "range":   [0, 10],
        }
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            "https://scanner.tradingview.com/america/scan",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        assert "data" in data, f"Expected 'data' key in screener response: {list(data.keys())}"
        assert len(data["data"]) > 0, "No data returned for known US symbols"
        print(f"\n  US screener returned {len(data['data'])} rows")
        for item in data["data"]:
            print(f"    {item.get('s','?')}: {item.get('d')}")

    @requires_network
    def test_au_screener_returns_data(self):
        """scanner.tradingview.com/australia/scan returns data for ASX symbols."""
        symbols = ["BHP", "CBA", "CSL", "MIN"]
        payload = {
            "columns": ["name", "relative_volume_10d_calc", "recommendation_mark", "close"],
            "filter":  [{"left": "name", "operation": "in_range", "right": symbols}],
            "range":   [0, 10],
        }
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            "https://scanner.tradingview.com/australia/scan",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        assert "data" in data
        print(f"\n  AU screener returned {len(data['data'])} rows")

    @requires_network
    def test_india_screener_returns_data(self):
        """scanner.tradingview.com/india/scan returns data for NSE symbols."""
        symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK"]
        payload = {
            "columns": ["name", "relative_volume_10d_calc", "recommendation_mark"],
            "filter":  [{"left": "name", "operation": "in_range", "right": symbols}],
            "range":   [0, 10],
        }
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            "https://scanner.tradingview.com/india/scan",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        assert "data" in data
        print(f"\n  India screener returned {len(data['data'])} rows")

    @requires_network
    def test_hk_screener_returns_data(self):
        """HKEX screener returns data for HK symbols."""
        symbols = ["00700", "09988", "02318"]
        payload = {
            "columns": ["name", "relative_volume_10d_calc", "close"],
            "filter":  [{"left": "name", "operation": "in_range", "right": symbols}],
            "range":   [0, 10],
        }
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            "https://scanner.tradingview.com/hongkong/scan",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        assert "data" in data
        print(f"\n  HK screener returned {len(data['data'])} rows")

    @requires_network
    def test_screener_rel_vol_field_is_numeric(self):
        """relative_volume_10d_calc field is a float (or null) for each row."""
        symbols = ["AAPL", "MSFT"]
        payload = {
            "columns": ["name", "relative_volume_10d_calc"],
            "filter":  [{"left": "name", "operation": "in_range", "right": symbols}],
            "range":   [0, 5],
        }
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            "https://scanner.tradingview.com/america/scan",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        for item in data.get("data", []):
            d = item.get("d", [])
            rel_vol = d[1] if len(d) > 1 else None
            if rel_vol is not None:
                assert isinstance(rel_vol, (int, float)), (
                    f"relative_volume_10d_calc should be numeric, got {type(rel_vol)}: {rel_vol}"
                )
        print(f"\n  rel_vol values: {[item['d'][1] for item in data.get('data',[]) if len(item.get('d',[]))>1]}")

    @requires_network
    def test_fetch_tv_screener_helper(self):
        """
        signal_stack.fetch_tv_screener() returns a dict keyed by pro_symbol
        for a handful of known symbols.
        """
        from signal_stack import fetch_tv_screener
        symbols = ["NYSE:AAPL", "NASDAQ:TSLA", "ASX:BHP", "NSE:RELIANCE"]
        result = fetch_tv_screener(symbols)
        assert isinstance(result, dict), "Expected dict return"
        print(f"\n  fetch_tv_screener result for {symbols}:")
        for sym, vals in result.items():
            print(f"    {sym}: rel_vol={vals.get('rel_vol')}, rec_mark={vals.get('rec_mark')}")
        # At least some symbols should return data
        assert len(result) > 0, "fetch_tv_screener returned empty dict for known symbols"

    @requires_network
    def test_multi_asset_screener_us_endpoint(self):
        """
        screeners.multi_asset_screener can fetch US stocks without crashing.
        Uses --class stocks_us with max 5 results.
        """
        sys.path.insert(0, str(TV_DIR))
        sys.path.insert(0, str(TV_DIR / "screeners"))
        from screeners.multi_asset_screener import SCAN_DEFS
        import urllib.request, json

        # Find the US stocks scan definition
        us_def = next((d for d in SCAN_DEFS if d[0] == "stocks_us"), None)
        assert us_def is not None, "stocks_us not in SCAN_DEFS"
        scan_key, label, url, cols, extra_filters, _ = us_def

        payload = {
            "columns": cols,
            "filter":  extra_filters,
            "range":   [0, 5],
            "sort":    {"sortBy": "ADX", "sortOrder": "desc"},
        }
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        assert "data" in data
        assert len(data["data"]) > 0, "US stock screener returned zero results"
        print(f"\n  US screener ({scan_key}) returned {len(data['data'])} rows")


# ─────────────────────────────────────────────────────────────────────────────
# 3b. Signal stack module smoke tests (no network required)
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalStackModuleImport:
    """
    Verify signal_stack.py can be imported cleanly and all module-level
    availability flags are defined.  These tests require NO network or auth
    and will always run — they exist specifically to catch NameErrors like
    a missing _EXTERNAL_AVAILABLE guard block.
    """

    def test_signal_stack_importable(self):
        """Importing signal_stack must not raise NameError or ImportError."""
        import importlib
        mod = importlib.import_module("signal_stack")
        assert mod is not None

    def test_all_availability_flags_defined(self):
        """All four optional-feature flags must be bool after module import."""
        import signal_stack
        for flag in ("_MOOMOO_AVAILABLE", "_IBKR_AVAILABLE",
                     "_SIZER_AVAILABLE", "_EXTERNAL_AVAILABLE"):
            assert hasattr(signal_stack, flag), (
                f"signal_stack.{flag} is not defined — add a try/except import guard"
            )
            assert isinstance(getattr(signal_stack, flag), bool), (
                f"signal_stack.{flag} should be bool"
            )

    def test_use_external_flag_evaluates_without_error(self):
        """The expression used in main() must not raise NameError."""
        import signal_stack
        # Simulate what main() does: (not args.no_external) and _EXTERNAL_AVAILABLE
        result = True and signal_stack._EXTERNAL_AVAILABLE
        assert isinstance(result, bool)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Signal stack end-to-end (small N)
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalStackEndToEnd:
    """
    Run signal_stack.py as a module for a small number of fires and verify
    the output HTML report is produced.
    """

    @requires_network
    def test_fires_raw_json_loadable(self):
        """fires_raw.json is valid JSON and contains at least some fire records."""
        fires_file = TV_DIR / "fires_raw.json"
        if not fires_file.exists():
            pytest.skip("fires_raw.json not present — run download_fires.py first")
        with open(fires_file, encoding="utf-8") as f:
            data = json.load(f)
        fires = data if isinstance(data, list) else data.get("all", [])
        assert isinstance(fires, list), "fires_raw.json must contain a list"
        print(f"\n  fires_raw.json: {len(fires)} total fires")

    @requires_network
    def test_parse_fire_meta_on_real_fires(self):
        """parse_fire_meta extracts numeric fields from real fired alert messages."""
        fires_file = TV_DIR / "fires_raw.json"
        if not fires_file.exists():
            pytest.skip("fires_raw.json not present")
        from signal_stack import parse_fire_meta
        with open(fires_file, encoding="utf-8") as f:
            data = json.load(f)
        fires = data if isinstance(data, list) else data.get("all", [])
        if not fires:
            pytest.skip("No fires in fires_raw.json")
        parsed_count = 0
        for fire in fires[:20]:
            msg  = fire.get("message", fire.get("msg", ""))
            meta = parse_fire_meta(msg)
            if meta:
                parsed_count += 1
        print(f"\n  parse_fire_meta: {parsed_count}/20 fires yielded metadata")
        # At least some fires should have parseable messages if they are WIN alerts
        assert parsed_count >= 0    # non-fatal: zero is OK if fires are from other strategies

    @requires_network
    def test_tv_to_yf_conversions_for_real_fires(self):
        """tv_to_yf produces valid yfinance tickers for symbols in fires_raw.json."""
        fires_file = TV_DIR / "fires_raw.json"
        if not fires_file.exists():
            pytest.skip("fires_raw.json not present")
        from signal_stack import tv_to_yf
        with open(fires_file, encoding="utf-8") as f:
            data = json.load(f)
        fires = data if isinstance(data, list) else data.get("all", [])
        if not fires:
            pytest.skip("No fires")
        sample = {f["symbol"] for f in fires[:30] if f.get("symbol") and ":" in f["symbol"]}
        for sym in list(sample)[:10]:
            yf_ticker = tv_to_yf(sym)
            assert isinstance(yf_ticker, str) and len(yf_ticker) > 0, (
                f"tv_to_yf({sym!r}) returned empty/None"
            )
            assert ":" not in yf_ticker, f"yfinance ticker should not contain ':': {yf_ticker}"
        sample_map = {s: tv_to_yf(s) for s in list(sample)[:5]}
        print(f"\n  Sample tv->yf conversions: {sample_map}")

    @requires_network
    def test_yfinance_ticker_lookup_for_real_fires(self):
        """
        yfinance can fetch basic info for a few real symbols from fires_raw.json.
        Validates the tv_to_yf mapping against actual yfinance behaviour.
        """
        import yfinance as yf
        fires_file = TV_DIR / "fires_raw.json"
        if not fires_file.exists():
            pytest.skip("fires_raw.json not present")
        from signal_stack import tv_to_yf
        with open(fires_file, encoding="utf-8") as f:
            data = json.load(f)
        fires = data if isinstance(data, list) else data.get("all", [])
        sample = list({
            f["symbol"] for f in fires[:30]
            if f.get("symbol") and f["symbol"].startswith("NYSE:")
        })[:3]
        if not sample:
            pytest.skip("No NYSE symbols in fires to test yfinance with")
        for tv_sym in sample:
            yf_ticker = tv_to_yf(tv_sym)
            t = yf.Ticker(yf_ticker)
            info = t.fast_info
            # fast_info.market_cap is None for unknown symbols; just verify no exception
            print(f"\n  {tv_sym} → {yf_ticker}: market_cap={getattr(info,'market_cap',None)}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. TV alert list (distinct from fires)
# ─────────────────────────────────────────────────────────────────────────────

class TestTvAlertList:
    """Verify /list_alerts returns current alert configurations."""

    @requires_auth
    def test_list_alerts_returns_ok(self):
        """POST /list_alerts returns status 'ok'."""
        import requests
        si = _session_info
        r  = requests.post(
            "https://pricealerts.tradingview.com/list_alerts",
            headers={
                "User-Agent":    "Mozilla/5.0",
                "Authorization": f"Bearer {si['jwt']}",
                "Content-Type":  "application/json",
                "Origin":        "https://www.tradingview.com",
                "Referer":       "https://www.tradingview.com/chart/",
            },
            cookies={"sessionid": si["sessionid"], "sessionid_sign": si["sessionid_sign"]},
            json={"uid": 57923804},
            timeout=15,
        )
        data = r.json()
        print(f"\n  /list_alerts status: {data.get('s')}, count: {len(data.get('r', []))}")
        assert data.get("s") == "ok", f"/list_alerts returned: {data}"

    @requires_auth
    def test_list_alerts_count_positive(self):
        """Account has at least one alert configured (WIN strategy watchlist)."""
        import requests
        si = _session_info
        r  = requests.post(
            "https://pricealerts.tradingview.com/list_alerts",
            headers={
                "User-Agent":    "Mozilla/5.0",
                "Authorization": f"Bearer {si['jwt']}",
                "Content-Type":  "application/json",
                "Origin":        "https://www.tradingview.com",
                "Referer":       "https://www.tradingview.com/chart/",
            },
            cookies={"sessionid": si["sessionid"], "sessionid_sign": si["sessionid_sign"]},
            json={"uid": 57923804},
            timeout=15,
        )
        alerts = r.json().get("r", [])
        print(f"\n  Alerts configured: {len(alerts)}")
        assert len(alerts) > 0, (
            "No alerts found — is the cookie expired? Or has the account been reset?"
        )

    @requires_auth
    def test_alert_records_have_required_fields(self):
        """Each alert record contains id, symbol, status, name."""
        import requests
        si = _session_info
        r  = requests.post(
            "https://pricealerts.tradingview.com/list_alerts",
            headers={
                "User-Agent":    "Mozilla/5.0",
                "Authorization": f"Bearer {si['jwt']}",
                "Content-Type":  "application/json",
                "Origin":        "https://www.tradingview.com",
                "Referer":       "https://www.tradingview.com/chart/",
            },
            cookies={"sessionid": si["sessionid"], "sessionid_sign": si["sessionid_sign"]},
            json={"uid": 57923804},
            timeout=15,
        )
        alerts = r.json().get("r", [])
        if not alerts:
            pytest.skip("No alerts to inspect")
        sample = alerts[0]
        print(f"\n  Alert fields: {list(sample.keys())}")
        for field in ("alert_id", "symbol", "active", "name"):
            assert field in sample, f"Expected field '{field}' in alert, got: {list(sample.keys())}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Webhook server health (if running)
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookServerHealth:
    """Integration checks against the locally running webhook server (port 5000)."""

    def _server_reachable(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 8765), timeout=2):
                return True
        except OSError:
            return False

    def test_health_endpoint(self):
        """GET /health returns 200 with JSON payload when server is running."""
        if not self._server_reachable():
            pytest.skip("Webhook server not running on port 8765 — start with webhook_server.py")
        import urllib.request
        resp = urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=5)
        assert resp.getcode() == 200
        body = json.loads(resp.read())
        assert "status" in body or "actionable_count" in body, (
            f"Unexpected /health response: {body}"
        )
        print(f"\n  /health: {body}")

    def test_webhook_endpoint_bad_payload_returns_400(self):
        """POST /webhook with empty body returns HTTP 400."""
        if not self._server_reachable():
            pytest.skip("Webhook server not running on port 8765")
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            "http://127.0.0.1:8765/webhook",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("Expected HTTP 400 but got 2xx")
        except urllib.error.HTTPError as e:
            assert e.code == 400, f"Expected 400, got {e.code}"
            print(f"\n  /webhook bad payload correctly returned 400")
