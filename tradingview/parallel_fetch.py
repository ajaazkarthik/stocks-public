"""
parallel_fetch.py — Multithreading utilities for concurrent API calls.

Replaces sequential for-loops over yfinance, FinBERT, and TV screener calls
with a ThreadPoolExecutor-backed ParallelFetcher that is:

  • Thread-safe       — each call is independent, no shared mutable state
  • Error-resilient   — one failure never crashes the batch (skip_errors=True)
  • Configurable      — max_workers and timeout tuned per API type (see DEFAULTS)
  • Cache-aware       — works transparently with diskcache (each worker hits
                        its own cached path)

Recommended max_workers by API type:
    yfinance fast_info  : 5-10 workers  (fast I/O-bound)
    yfinance history    : 5-8 workers   (I/O-bound, larger responses)
    FinBERT inference   : 2-4 workers   (CPU-bound, large model in memory)
    TradingView API     : 3-5 workers   (rate-limited)

Usage:
    from parallel_fetch import ParallelFetcher, parallel_map

    fetcher = ParallelFetcher(max_workers=5)
    results = fetcher.fetch_many(["AAPL", "MSFT", "TSLA"], fetch_fn=my_api_call)
    # → {"AAPL": ..., "MSFT": ..., "TSLA": ...}

    # Or functional style:
    results = parallel_map(symbols, fetch_fn=my_api_call, max_workers=5)
"""

import sys
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, TypeVar

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_log = logging.getLogger(__name__)

T = TypeVar("T")

# ── Default worker counts per API type ────────────────────────────────────────
WORKERS_YFINANCE_INFO    = 8   # fast_info (I/O-bound, yfinance is thread-safe)
WORKERS_YFINANCE_HISTORY = 5   # history OHLCV (larger payloads)
WORKERS_FINBERT          = 3   # FinBERT inference (CPU-bound, ~400MB model)
WORKERS_TV_SCREENER      = 5   # TradingView screener (rate-limited)

# Default per-call timeout (seconds)
DEFAULT_TIMEOUT = 30


class ParallelFetcher:
    """
    Thread-safe fetcher for concurrent API calls with graceful error handling.

    Each call to fetch_many() spins up a fresh ThreadPoolExecutor, submits all
    work items, collects results via as_completed(), and returns a dict mapping
    each input item to its result (or None on error when skip_errors=True).
    """

    def __init__(
        self,
        max_workers: int = 5,
        timeout: int = DEFAULT_TIMEOUT,
        verbose: bool = False,
    ) -> None:
        self.max_workers = max_workers
        self.timeout = timeout
        self.verbose = verbose

    def fetch_many(
        self,
        items: List[str],
        fetch_fn: Callable[[str], T],
        desc: str = "Fetching",
        skip_errors: bool = True,
    ) -> Dict[str, Optional[T]]:
        """
        Fetch multiple items concurrently.

        Args:
            items:       Ordered list of keys (symbols, headlines, URLs, etc.)
            fetch_fn:    Callable[str] -> T — called once per item in a thread
            desc:        Short label for log/verbose output
            skip_errors: True  → failed items map to None (safe, default)
                         False → re-raises the first exception encountered

        Returns:
            Dict mapping each item → result  (or None on error)
        """
        if not items:
            return {}

        n = len(items)
        workers = min(self.max_workers, n)
        results: Dict[str, Optional[T]] = {}
        errors: List[Exception] = []

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_item = {executor.submit(fetch_fn, item): item for item in items}

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result(timeout=self.timeout)
                    results[item] = result
                    if self.verbose:
                        done = len(results) + len(errors)
                        _log.debug("%s %s (%d/%d)", desc, item, done, n)
                except Exception as exc:
                    errors.append(exc)
                    results[item] = None
                    if self.verbose:
                        _log.debug("%s %s FAILED: %s", desc, item, exc)
                    if not skip_errors:
                        # Cancel remaining futures and re-raise
                        for f in future_to_item:
                            f.cancel()
                        raise exc from exc

        elapsed = time.monotonic() - t0
        if errors:
            _log.debug(
                "parallel_fetch_partial_errors",
                desc=desc,
                total=n,
                errors=len(errors),
                elapsed_s=round(elapsed, 2),
            )
        else:
            _log.debug(
                "parallel_fetch_ok",
                desc=desc,
                total=n,
                elapsed_s=round(elapsed, 2),
            )

        return results

    def fetch_ordered(
        self,
        items: List[str],
        fetch_fn: Callable[[str], T],
        desc: str = "Fetching",
        default: Any = None,
    ) -> List[T]:
        """
        Like fetch_many() but returns a list in the same order as ``items``.

        Args:
            items:    Items to fetch
            fetch_fn: Per-item callable
            desc:     Log label
            default:  Value to use for failed/None results

        Returns:
            List of results aligned to ``items``
        """
        mapping = self.fetch_many(items, fetch_fn, desc=desc, skip_errors=True)
        return [mapping.get(item, default) if mapping.get(item) is not None else default
                for item in items]


# ── Module-level convenience function ─────────────────────────────────────────

def parallel_map(
    items: List[str],
    fetch_fn: Callable[[str], T],
    *,
    max_workers: int = 5,
    timeout: int = DEFAULT_TIMEOUT,
    desc: str = "Fetching",
    skip_errors: bool = True,
) -> Dict[str, Optional[T]]:
    """
    One-shot parallel fetch — convenience wrapper around ParallelFetcher.

    Example:
        results = parallel_map(symbols, get_cached_ticker_info, max_workers=8)
    """
    return ParallelFetcher(
        max_workers=max_workers,
        timeout=timeout,
    ).fetch_many(items, fetch_fn, desc=desc, skip_errors=skip_errors)
