"""
RBA FX Rates — Historical AUD exchange rates from the Reserve Bank of Australia
================================================================================
Australian CGT law requires foreign-currency amounts to be converted to AUD using
the RBA published rate on the DATE OF THE TRANSACTION (not today's rate).

Source: https://www.rba.gov.au/statistics/tables/csv/f11-data.csv
Cache:  .cache/rba_fx_rates.json (refreshed daily)

Usage:
    from rba_fx_rates import get_rba_rate
    rate = get_rba_rate("USD", date(2025, 6, 15))  # → 0.6543 (1 AUD = 0.6543 USD)
    aud_amount = usd_amount / rate  # convert USD to AUD
"""
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import csv
import io
import json
import logging
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RBA_CSV_URL = "https://www.rba.gov.au/statistics/tables/csv/f11-data.csv"

_CACHE_DIR = Path(__file__).parent / ".cache"
_CACHE_FILE = _CACHE_DIR / "rba_fx_rates.json"
_CACHE_MAX_AGE_HOURS = 24

# RBA CSV column header → our currency key.
# The RBA f11 CSV has headers like "FXRUSD" (units per AUD) for each currency.
_RBA_SERIES_MAP: dict[str, str] = {
    "FXRUSD": "USD",
    "FXRGBP": "GBP",
    "FXREUR": "EUR",
    "FXRJPY": "JPY",
    "FXRHKD": "HKD",
    "FXRSGD": "SGD",
    "FXRNZD": "NZD",
}

SUPPORTED_CURRENCIES = set(_RBA_SERIES_MAP.values())

# Maximum number of prior business days to search for a rate (weekends/holidays).
_MAX_LOOKBACK_DAYS = 10

# ---------------------------------------------------------------------------
# Internal data store: { "USD": { "2025-06-15": 0.6543, ... }, ... }
# ---------------------------------------------------------------------------

_rates: dict[str, dict[str, float]] = {}
_cache_loaded_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Download & parse
# ---------------------------------------------------------------------------

def _download_csv(url: str = RBA_CSV_URL, timeout: int = 30) -> str:
    """Download the RBA f11 CSV and return raw text."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 rba_fx_rates.py"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _parse_rba_csv(raw: str) -> dict[str, dict[str, float]]:
    """Parse the RBA f11 CSV into {currency: {date_str: rate}}."""
    lines = raw.splitlines()

    # Find the header row that contains the series IDs (e.g. FXRUSD).
    # The RBA CSV has several metadata rows before the actual header.
    header_idx = -1
    for i, line in enumerate(lines):
        if "FXRUSD" in line:
            header_idx = i
            break

    if header_idx < 0:
        raise ValueError("Could not find RBA series header row (FXRUSD) in CSV")

    # The units row is immediately after the series header.
    # Data rows start after that.
    data_start = header_idx + 2  # skip header + units row

    reader = csv.reader(io.StringIO(lines[header_idx]))
    headers = next(reader)

    # Build column index → currency mapping
    col_map: dict[int, str] = {}
    for col_idx, hdr in enumerate(headers):
        hdr_clean = hdr.strip().upper()
        if hdr_clean in _RBA_SERIES_MAP:
            col_map[col_idx] = _RBA_SERIES_MAP[hdr_clean]

    if not col_map:
        raise ValueError("No recognised currency series found in RBA CSV headers")

    result: dict[str, dict[str, float]] = {ccy: {} for ccy in col_map.values()}

    for line in lines[data_start:]:
        if not line.strip():
            continue
        row = list(csv.reader(io.StringIO(line)))[0]
        if len(row) < 2:
            continue

        date_str = row[0].strip()
        # RBA date format: DD-Mon-YYYY (e.g. "02-Jan-2025")
        try:
            dt = datetime.strptime(date_str, "%d-%b-%Y").date()
        except ValueError:
            continue

        iso_date = dt.isoformat()  # "2025-01-02"

        for col_idx, ccy in col_map.items():
            if col_idx >= len(row):
                continue
            val = row[col_idx].strip()
            if not val or val.upper() in ("", "N/A", "-"):
                continue
            try:
                result[ccy][iso_date] = float(val)
            except ValueError:
                continue

    return result


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def _save_cache(rates: dict[str, dict[str, float]]) -> None:
    """Persist rates to JSON cache file."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now().isoformat(),
        "rates": rates,
    }
    _CACHE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_cache() -> Optional[dict[str, dict[str, float]]]:
    """Load rates from JSON cache if fresh enough. Returns None if stale/missing."""
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        age = datetime.now() - fetched_at
        if age > timedelta(hours=_CACHE_MAX_AGE_HOURS):
            return None
        return data["rates"]
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        _log.warning("RBA cache corrupt, will re-download: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Initialisation (lazy)
# ---------------------------------------------------------------------------

def _ensure_loaded() -> None:
    """Load rates from cache or download from RBA. Populates module-level _rates."""
    global _rates, _cache_loaded_at

    if _rates and _cache_loaded_at:
        age = datetime.now() - _cache_loaded_at
        if age < timedelta(hours=_CACHE_MAX_AGE_HOURS):
            return  # already loaded and fresh

    # Try cache first
    cached = _load_cache()
    if cached:
        _rates = cached
        _cache_loaded_at = datetime.now()
        return

    # Download from RBA
    try:
        raw = _download_csv()
        _rates = _parse_rba_csv(raw)
        _save_cache(_rates)
        _cache_loaded_at = datetime.now()
        _log.info("RBA FX rates downloaded and cached (%d currencies)", len(_rates))
    except Exception as exc:
        _log.warning("Failed to download RBA FX rates: %s", exc)
        # Try stale cache as last resort
        if _CACHE_FILE.exists():
            try:
                data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                _rates = data["rates"]
                _cache_loaded_at = datetime.now()
                _log.info("Using stale RBA cache as fallback")
                return
            except Exception:
                pass
        # _rates stays empty — get_rba_rate will fall back to yfinance


# ---------------------------------------------------------------------------
# yfinance fallback
# ---------------------------------------------------------------------------

def _yfinance_fallback(ccy: str, dt: date) -> Optional[float]:
    """Fetch AUD exchange rate from yfinance as a fallback."""
    try:
        import yfinance as yf

        # yfinance ticker format: AUDUSD=X gives "1 AUD = X USD"
        ticker = f"AUD{ccy}=X"
        data = yf.download(
            ticker,
            start=(dt - timedelta(days=7)).isoformat(),
            end=(dt + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=True,
        )
        if data is None or data.empty:
            return None

        # Handle MultiIndex columns from yfinance
        if hasattr(data.columns, "levels"):
            data.columns = data.columns.droplevel("Ticker")

        # Find the closest date <= dt
        data.index = data.index.date  # type: ignore[assignment]
        valid = data.loc[data.index <= dt]
        if valid.empty:
            return None

        close_val = valid["Close"].iloc[-1]
        rate = float(close_val)
        if rate > 0:
            _log.info("yfinance fallback for %s on %s: %.6f", ccy, dt, rate)
            return rate
    except Exception as exc:
        _log.warning("yfinance fallback failed for %s on %s: %s", ccy, dt, exc)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_rba_rate(from_ccy: str, to_aud_date: date) -> Optional[float]:
    """
    Get the RBA exchange rate for converting *from_ccy* to AUD on a given date.

    Returns the rate as "1 AUD = X units of from_ccy" (RBA convention).
    To convert foreign amount to AUD: ``aud = foreign_amount / rate``.

    Falls back to most recent prior business day if exact date unavailable,
    then to yfinance if RBA data is completely unavailable.

    Returns None if the currency is unsupported or no rate can be found.
    """
    ccy = from_ccy.strip().upper()
    if ccy == "AUD":
        return 1.0
    if ccy not in SUPPORTED_CURRENCIES:
        return None

    _ensure_loaded()

    ccy_rates = _rates.get(ccy, {})
    if ccy_rates:
        # Search backwards from the requested date
        for offset in range(_MAX_LOOKBACK_DAYS + 1):
            lookup = to_aud_date - timedelta(days=offset)
            rate = ccy_rates.get(lookup.isoformat())
            if rate is not None:
                return rate

    # Fallback to yfinance
    return _yfinance_fallback(ccy, to_aud_date)


def get_available_dates(ccy: str = "USD") -> list[date]:
    """Return sorted list of dates for which we have RBA rates for a currency."""
    _ensure_loaded()
    ccy_rates = _rates.get(ccy.upper(), {})
    return sorted(date.fromisoformat(d) for d in ccy_rates)


def force_refresh() -> None:
    """Force re-download of RBA rates (bypass cache)."""
    global _rates, _cache_loaded_at
    _rates = {}
    _cache_loaded_at = None
    if _CACHE_FILE.exists():
        _CACHE_FILE.unlink()
    _ensure_loaded()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RBA FX Rates — look up historical AUD exchange rates"
    )
    parser.add_argument("currency", nargs="?", default="USD",
                        help="Currency code (e.g. USD, HKD, SGD)")
    parser.add_argument("--date", "-d", default=None,
                        help="Date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-download from RBA")
    args = parser.parse_args()

    if args.refresh:
        force_refresh()
        print("RBA rates refreshed.")

    lookup_date = date.fromisoformat(args.date) if args.date else date.today()
    ccy = args.currency.upper()

    rate = get_rba_rate(ccy, lookup_date)
    if rate is None:
        print(f"No rate found for {ccy} on {lookup_date}")
        sys.exit(1)
    else:
        print(f"1 AUD = {rate:.6f} {ccy}  (date: {lookup_date})")
        print(f"To convert {ccy} to AUD: amount / {rate:.6f}")


if __name__ == "__main__":
    main()
