"""
TQS v2 yfinance OI Enricher
============================
Enriches RawContracts with real Open Interest from yfinance.
TV scan2 does NOT provide OI (field does not exist → HTTP 400).
yfinance is the ONLY source of real OI data.

Implements IOIEnricher.

Pattern: batch all unique (symbol, expiry_str) pairs, fetch once each,
         then match contracts by (strike, option_type).
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from ..config import TQSv2Config
from ..exceptions import YFinanceError
from ..interfaces.contracts import IOIEnricher
from .models import RawContract

_log = logging.getLogger("tqs_v2.options_data.yfinance_oi")

# Lazy import yfinance to avoid overhead if not needed
_yf = None


def _get_yf():
    global _yf
    if _yf is None:
        import yfinance as yf
        _yf = yf
    return _yf


def _expiration_to_str(expiration: int) -> Optional[str]:
    """Convert YYYYMMDD int to 'YYYY-MM-DD' string yfinance uses."""
    try:
        s = str(expiration)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except (ValueError, IndexError):
        return None


class YFinanceOIEnricher(IOIEnricher):
    """
    Fetches OI from yfinance and attaches it to RawContracts.

    Uses ThreadPoolExecutor (NEVER with `with` + fut.result(timeout=N)).
    Follows SD-004 pattern: _ex.submit(); fut.result(timeout=N); _ex.shutdown(wait=False).
    """

    def enrich(
        self,
        contracts: list[RawContract],
        config: TQSv2Config,
    ) -> list[RawContract]:
        """
        Enrich contracts with real OI from yfinance.

        Groups contracts by (symbol, expiry_str) to minimise API calls.
        Returns the same list (mutates open_interest in-place).
        """
        if not contracts:
            return contracts

        # Build groups: {(symbol, expiry_str): [contract, ...]}
        groups: dict[tuple[str, str], list[RawContract]] = defaultdict(list)
        for c in contracts:
            expiry_str = _expiration_to_str(c.expiration)
            if expiry_str:
                groups[(c.root, expiry_str)].append(c)

        unique_pairs = list(groups.keys())
        _log.info(
            "OI enrichment: %d contracts in %d unique (symbol, expiry) groups",
            len(contracts),
            len(unique_pairs),
        )

        # Fetch OI + volume for each unique (symbol, expiry) pair
        oi_cache: dict[tuple[str, str, str, float], tuple[int, Optional[int]]] = {}
        # key: (symbol, expiry_str, option_type, strike) → OI

        # Use ThreadPoolExecutor with SD-004 pattern (NEVER with ctx manager + timeout)
        _ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=config.max_concurrent_yfinance,
            thread_name_prefix="yf_oi",
        )
        futs = {
            _ex.submit(self._fetch_oi_for_pair, sym, exp_str, config): (sym, exp_str)
            for sym, exp_str in unique_pairs
        }
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=60):
                sym, exp_str = futs[fut]
                try:
                    result = fut.result(timeout=config.fetch_timeout_sec)
                    oi_cache.update(result)
                except concurrent.futures.TimeoutError:
                    _log.warning(
                        "OI fetch timeout for %s %s", sym, exp_str
                    )
                except Exception as exc:
                    _log.warning(
                        "OI fetch failed for %s %s: %s", sym, exp_str, exc
                    )
        finally:
            _ex.shutdown(wait=False)   # SD-004: never blocks

        # Attach OI + volume to contracts
        enriched_count = 0
        for c in contracts:
            expiry_str = _expiration_to_str(c.expiration)
            if expiry_str is None:
                continue
            cache_key = (c.root, expiry_str, c.option_type, float(c.strike))
            oi, yf_volume = oi_cache.get(cache_key, (0, None))
            # Only write OI when yfinance returned a positive value.
            # 0 from yfinance means either: no data (API refresh, post-holiday gap),
            # or genuinely zero OI.  We cannot distinguish, so we leave c.open_interest
            # as None (its dataclass default).  The OpenInterestFilter already treats
            # None as "unknown → don't reject", so this is safe.  Genuinely zero-OI
            # contracts are caught by VolumeFilter (they also have zero volume).
            if oi > 0:
                c.open_interest = oi
                enriched_count += 1
            # Fill volume from yfinance if TV scan2 returned None or 0 (market closed: TV returns 0, not None)
            if (c.volume is None or c.volume == 0) and yf_volume:
                c.volume = yf_volume

        total = len(contracts)
        _log.info(
            "OI enrichment complete: %d/%d contracts have OI > 0",
            enriched_count,
            total,
        )
        # Warn when yfinance appears to be returning stale/empty data
        # (common on post-holiday first trading days or during daily data refresh).
        # When enrichment rate < 5%, OI filter is unreliable; contracts will pass
        # via the OpenInterestFilter's None→accept path.
        if total > 0 and enriched_count / total < 0.05:
            _log.warning(
                "OI enrichment rate is very low (%d/%d = %.1f%%). "
                "Yahoo Finance may be refreshing data (post-holiday / daily reset). "
                "OI filter will be bypassed for contracts without yfinance data.",
                enriched_count, total, 100.0 * enriched_count / total,
            )
        return contracts

    def _fetch_oi_for_pair(
        self,
        symbol: str,
        expiry_str: str,
        config: TQSv2Config,
    ) -> dict[tuple[str, str, str, float], tuple[int, Optional[int]]]:
        """
        Fetch OI + volume for all contracts of (symbol, expiry_str).

        Returns dict: {(symbol, expiry_str, option_type, strike): (oi, volume)}
        Volume fills the gap when TV scan2 returns None (market closed).
        """
        yf = _get_yf()
        result: dict[tuple[str, str, str, float], tuple[int, Optional[int]]] = {}

        try:
            ticker = yf.Ticker(symbol)
            chain = ticker.option_chain(expiry_str)

            # Calls
            if hasattr(chain, "calls") and not chain.calls.empty:
                for _, row in chain.calls.iterrows():
                    strike = float(row.get("strike", 0))
                    oi = int(row.get("openInterest", 0) or 0)
                    vol = row.get("volume")
                    volume = int(vol) if vol is not None and vol == vol else None  # NaN check
                    result[(symbol, expiry_str, "call", strike)] = (oi, volume)

            # Puts
            if hasattr(chain, "puts") and not chain.puts.empty:
                for _, row in chain.puts.iterrows():
                    strike = float(row.get("strike", 0))
                    oi = int(row.get("openInterest", 0) or 0)
                    vol = row.get("volume")
                    volume = int(vol) if vol is not None and vol == vol else None  # NaN check
                    result[(symbol, expiry_str, "put", strike)] = (oi, volume)

            time.sleep(config.yfinance_delay_sec)

        except Exception as exc:
            _log.debug(
                "yfinance OI+volume failed for %s %s: %s", symbol, expiry_str, exc
            )

        return result
