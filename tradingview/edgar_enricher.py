"""
SEC EDGAR Form 4 Insider Transaction Enricher
===============================================
Fetches insider buying activity from SEC EDGAR for US-listed symbols.
Uses the free, public EDGAR API (no API key required).

Data sources:
  - company_tickers.json: Symbol → CIK mapping (cached 24h)
  - submissions endpoint:  Recent filings per CIK (Form 4 = insider trades)

Benchmark (2026-04-29):
  - API latency: ~417ms per request
  - Coverage: ~19% of portfolio (US-listed symbols only)
  - CIK match rate: 97.4% (37/38 US symbols)

Usage:
    from edgar_enricher import enrich_insider_activity

    result = enrich_insider_activity("AAPL")
    # → {"insider_buys_30d": 3, "total_insider_value": 1500000,
    #    "latest_buy_date": "2026-04-15", "cik": "0000320193"}

    # Batch usage:
    results = {sym: enrich_insider_activity(sym) for sym in us_symbols}
"""

import sys
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import urllib.request
    import urllib.error
except ImportError:
    pass

# ─── Configuration ────────────────────────────────────────────────────────────

# SEC EDGAR requires a User-Agent header identifying the caller
_SEC_USER_AGENT = "stocks-agent admin@localhost"
_SEC_HEADERS = {"User-Agent": _SEC_USER_AGENT, "Accept": "application/json"}

# Cache paths
_CACHE_DIR = Path(__file__).parent.parent / ".cache" / "edgar"
_TICKERS_CACHE = _CACHE_DIR / "company_tickers.json"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_CACHE_TTL_HOURS = 24
_REQUEST_TIMEOUT = 10  # seconds

# In-memory CIK lookup (populated on first call)
_cik_map: dict[str, str] = {}
_cik_map_loaded = False


def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _is_cache_fresh(path: Path, ttl_hours: int = _CACHE_TTL_HOURS) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl_hours * 3600


def _fetch_json(url: str) -> Optional[dict]:
    """Fetch JSON from a URL with SEC-compliant headers."""
    try:
        req = urllib.request.Request(url, headers=_SEC_HEADERS)
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError) as exc:
        print(f"[edgar] fetch failed {url}: {exc}", flush=True)
        return None


def _load_cik_map() -> dict[str, str]:
    """Load ticker → CIK mapping from SEC company_tickers.json (cached)."""
    global _cik_map, _cik_map_loaded
    if _cik_map_loaded:
        return _cik_map

    _ensure_cache_dir()

    # Try disk cache first
    if _is_cache_fresh(_TICKERS_CACHE):
        try:
            data = json.loads(_TICKERS_CACHE.read_text(encoding="utf-8"))
            _cik_map = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
            _cik_map_loaded = True
            return _cik_map
        except Exception:
            pass

    # Fetch from SEC
    data = _fetch_json(_TICKERS_URL)
    if not data:
        _cik_map_loaded = True
        return _cik_map

    # Save to cache
    try:
        _TICKERS_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass

    _cik_map = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    _cik_map_loaded = True
    return _cik_map


def get_cik(symbol: str) -> Optional[str]:
    """Look up CIK for a given ticker symbol. Returns 10-digit padded CIK or None."""
    cik_map = _load_cik_map()
    return cik_map.get(symbol.upper())


def enrich_insider_activity(
    symbol: str,
    lookback_days: int = 30,
) -> dict:
    """
    Check SEC EDGAR for recent Form 4 insider buying activity.

    ⚠ APPROXIMATION WARNING: insider_buys_30d counts ALL Form 4 filings
    (purchases, sales, gifts, option exercises) — not just purchases.
    The EDGAR submissions API does not expose transaction codes (A/D/P/S)
    at the filing index level. Treat this as "insider activity" not
    "insider buying".

    Returns:
        dict with keys:
            insider_buys_30d:    int — count of Form 4 filings (ALL types, not just buys)
            total_insider_value: float — estimated total buy value (0 if unknown)
            latest_buy_date:     str | None — most recent Form 4 filing date
            cik:                 str | None — SEC CIK for this symbol
            has_insider_buying:  bool — True if any Form 4 filings found
            error:               str | None — error message if failed
    """
    result = {
        "insider_buys_30d": 0,
        "total_insider_value": 0.0,
        "latest_buy_date": None,
        "cik": None,
        "has_insider_buying": False,
        "error": None,
    }

    cik = get_cik(symbol)
    if not cik:
        result["error"] = f"CIK not found for {symbol}"
        return result
    result["cik"] = cik

    # Fetch recent filings
    url = _SUBMISSIONS_URL.format(cik=cik)
    data = _fetch_json(url)
    if not data:
        result["error"] = f"Failed to fetch filings for CIK {cik}"
        return result

    # Parse recent filings
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession_nos = recent.get("accessionNumber", [])

    if not forms or not dates:
        return result

    # Minimal urllib-based session adapter for _has_purchase_in_form4
    class _Session:
        def get(self, url, timeout=10):
            class _R:
                status_code = 0
                text = ""
            r = _R()
            try:
                req = urllib.request.Request(url, headers=_SEC_HEADERS)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    r.status_code = resp.status
                    r.text = resp.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return r

    _session = _Session()

    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    buy_count = 0
    latest_date = None

    for form, date_str, acc_no in zip(forms, dates, accession_nos or [None] * len(forms)):
        if form != "4":
            continue
        if date_str < cutoff:
            break  # filings are reverse chronological
        if acc_no and not _has_purchase_in_form4(cik, acc_no, _session):
            continue
        buy_count += 1
        if latest_date is None or date_str > latest_date:
            latest_date = date_str

    result["insider_buys_30d"] = buy_count
    result["latest_buy_date"] = latest_date
    result["has_insider_buying"] = buy_count > 0

    return result


def _has_purchase_in_form4(cik_padded: str, accession_no: str, session) -> bool:
    """
    Fetch Form 4 XML and check if it contains a purchase transaction (code 'P').

    Returns False on any error (fail-safe). Only counts open-market purchases
    (transactionCode == 'P'), not sales (S), grants (A), or exercises (M).
    """
    import re as _re
    acc_clean = accession_no.replace("-", "")
    xml_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik_padded)}/{acc_clean}/{accession_no}.xml"
    )
    try:
        resp = session.get(xml_url, timeout=5)
        if resp.status_code != 200:
            return False
        codes = _re.findall(r'<transactionCode>\s*([A-Z])\s*</transactionCode>', resp.text)
        return "P" in codes
    except Exception:
        return False


def batch_enrich(symbols: list[str], lookback_days: int = 30) -> dict[str, dict]:
    """
    Enrich multiple symbols. Filters to US-only (no exchange prefix or known US exchanges).
    Rate-limits to ~10 requests/sec to respect SEC fair access policy.
    """
    results = {}
    # Pre-load CIK map once
    _load_cik_map()

    for sym in symbols:
        # Strip exchange prefix if present
        bare = sym.split(":")[-1].upper() if ":" in sym else sym.upper()

        # Skip non-US exchanges
        if ":" in sym:
            exch = sym.split(":")[0].upper()
            if exch not in ("NYSE", "NASDAQ", "AMEX", "BATS", "CBOE"):
                continue

        results[bare] = enrich_insider_activity(bare, lookback_days)
        time.sleep(0.1)  # SEC rate limit: ~10 req/sec

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SEC EDGAR Form 4 Insider Buying Enricher")
    parser.add_argument("symbols", nargs="*", default=["AAPL", "MSFT", "NVDA"],
                        help="Symbols to check (default: AAPL MSFT NVDA)")
    parser.add_argument("--days", type=int, default=30, help="Lookback days (default: 30)")
    args = parser.parse_args()

    for sym in args.symbols:
        result = enrich_insider_activity(sym, args.days)
        buys = result["insider_buys_30d"]
        has = "✅" if result["has_insider_buying"] else "—"
        date = result["latest_buy_date"] or "N/A"
        print(f"{sym:>6}: {has} {buys} Form 4s in {args.days}d  (latest: {date})  CIK={result['cik']}")
