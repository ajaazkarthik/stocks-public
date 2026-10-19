"""
TQS v2 CBOE CDN OI Enricher
============================
Primary Open Interest source using CBOE's free delayed-quotes CDN.

    URL: https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json

Why CBOE is the primary OI source (not yfinance):
  - Returns ALL expiries for a symbol in ONE HTTP call (yfinance = one call per expiry)
  - 72-84% full-scan fill rate, 93-99% near-ATM fill rate (benchmarked 2026-05-26)
  - yfinance achieves ~20% fill rate in the same scan (many expiries fail/return 0)
  - CBOE is the authoritative OCC clearing house — same entity that settles US options
  - 15-minute delayed OI (not EOD) — fresher than yfinance's end-of-day OI

yfinance runs after CBOE as a supplement (Stage 3b) for contracts CBOE misses.

SD-004 compliance: uses _ex.shutdown(wait=False) — NEVER `with ThreadPoolExecutor()`.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
from collections import defaultdict

import requests

from ..config import TQSv2Config
from ..interfaces.contracts import IOIEnricher
from .models import RawContract

_log = logging.getLogger("tqs_v2.options_data.cboe_oi")

_BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (TQS-v2 bot)"}

# Parse OCC option code: SYMBOL(1-5) + YYMMDD + C/P + strike_in_thousandths(8 digits)
# e.g. AAPL260515C00220000  →  symbol=AAPL, date=260515, type=C, strike=220.000
_OCC_RE = re.compile(r"^([A-Z1-9]+?)(\d{6})(C|P)(\d{8})$")


def _parse_occ(opra_code: str) -> tuple[str, int, str, float] | None:
    """
    Parse OCC option code → (symbol, expiration_YYYYMMDD, option_type, strike).

    option_type is "call" or "put" to match RawContract.option_type exactly.
    Returns None if the code doesn't match OCC format.
    """
    m = _OCC_RE.match(opra_code.strip())
    if not m:
        return None
    sym, date_str, opt_char, strike_str = m.groups()
    # YYMMDD → YYYYMMDD (assume 2000-2099)
    yy, mm, dd = int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6])
    expiration = (2000 + yy) * 10000 + mm * 100 + dd
    strike = round(int(strike_str) / 1000.0, 3)
    # Use "call"/"put" — must match RawContract.option_type (never "c"/"p")
    option_type = "call" if opt_char.upper() == "C" else "put"
    return sym, expiration, option_type, strike


class CboeOIEnricher(IOIEnricher):
    """
    Primary OI enricher: fetches OI (and volume) from CBOE CDN and attaches it to RawContracts.

    One HTTP call per symbol returns ALL expiries (yfinance needs one call per expiry).
    yfinance runs after as a supplement (Stage 3b) for any contracts CBOE misses.

    Uses ThreadPoolExecutor(max_concurrent_yfinance workers) and SD-004 shutdown.

    Benchmark (2026-05-26, 10 symbols):
      avg latency       : 1.62 s per symbol
      near-ATM fill rate: 93.4%
      full-scan fill rate: 72-84% (all expiries including deep OTM)
    """

    def enrich(
        self,
        contracts: list[RawContract],
        config: TQSv2Config,
    ) -> list[RawContract]:
        """Enrich contracts with CBOE CDN OI (primary source)."""
        if not contracts:
            return contracts

        # Collect unique symbols (one CBOE call per symbol = all expiries)
        symbols = list({c.root for c in contracts})
        _log.info(
            "[CBOE CDN] Fetching OI for %d unique symbols (%d contracts)...",
            len(symbols), len(contracts),
        )

        # Parallel fetch — SD-004: never use `with` + fut.result(timeout=N)
        workers = min(config.max_concurrent_yfinance, len(symbols))
        _ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="cboe_oi",
        )
        futs: dict[concurrent.futures.Future, str] = {
            _ex.submit(self._fetch_symbol, sym, config.fetch_timeout_sec): sym
            for sym in symbols
        }

        # Build master lookup: {(symbol, expiration_int, option_type, strike): (oi, volume)}
        cboe_cache: dict[tuple[str, int, str, float], tuple[int, int | None]] = {}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=120):
                sym = futs[fut]
                try:
                    result = fut.result(timeout=config.fetch_timeout_sec)
                    cboe_cache.update(result)
                except concurrent.futures.TimeoutError:
                    _log.warning("[CBOE CDN] Timeout for %s", sym)
                except Exception as exc:
                    _log.warning("[CBOE CDN] Fetch failed for %s: %s", sym, exc)
        except concurrent.futures.TimeoutError:
            _log.error("[CBOE CDN] Overall timeout waiting for futures")
        finally:
            _ex.shutdown(wait=False)  # SD-004: never block

        # Apply to contracts (mutate in-place like yfinance_oi does)
        enriched_oi = 0
        enriched_vol = 0
        for c in contracts:
            key = (c.root, c.expiration, c.option_type, round(c.strike, 3))
            cached = cboe_cache.get(key)
            if cached is None:
                continue
            oi, vol = cached
            if oi > 0:
                c.open_interest = oi
                enriched_oi += 1
            # Fill volume if TV scan2 returned None/0 (market-closed situation)
            if (c.volume is None or c.volume == 0) and vol:
                c.volume = vol
                enriched_vol += 1

        total = len(contracts)
        _log.info(
            "[CBOE CDN] OI enriched: %d/%d (%.1f%%)  volume filled: %d",
            enriched_oi, total, 100.0 * enriched_oi / max(total, 1), enriched_vol,
        )
        if total > 0 and enriched_oi / total < 0.05:
            _log.warning(
                "[CBOE CDN] Low fill rate (%d/%d = %.1f%%). "
                "CBOE may be outside trading hours or the symbol list is unusual.",
                enriched_oi, total, 100.0 * enriched_oi / total,
            )
        return contracts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_symbol(
        self,
        symbol: str,
        timeout: float,
    ) -> dict[tuple[str, int, str, float], tuple[int, int | None]]:
        """
        Single HTTP GET for one symbol → return OI/volume lookup for ALL its contracts.

        Returns: {(symbol, expiration_YYYYMMDD, option_type, strike): (oi, volume)}
        """
        url = _BASE_URL.format(symbol=symbol)
        try:
            resp = requests.get(url, timeout=timeout, headers=_HEADERS)
            if resp.status_code != 200:
                _log.debug("[CBOE CDN] %s → HTTP %d", symbol, resp.status_code)
                return {}
            data = resp.json()
            options: list[dict] = data.get("data", {}).get("options", [])
        except Exception as exc:
            _log.debug("[CBOE CDN] Network error for %s: %s", symbol, exc)
            return {}

        result: dict[tuple[str, int, str, float], tuple[int, int | None]] = {}
        skipped = 0
        for item in options:
            opra = item.get("option", "")
            parsed = _parse_occ(opra)
            if parsed is None:
                skipped += 1
                continue
            sym, expiration, option_type, strike = parsed  # option_type = "call" or "put"
            oi = int(item.get("open_interest", 0) or 0)
            raw_vol = item.get("volume")
            volume = int(raw_vol) if raw_vol is not None and raw_vol != "" else None
            result[(sym, expiration, option_type, strike)] = (oi, volume)

        if skipped:
            _log.debug("[CBOE CDN] %s: skipped %d unparseable OCC codes", symbol, skipped)
        return result
