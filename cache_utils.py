"""
cache_utils.py — Persistent disk cache for expensive API calls.

Drop-in memoize decorator for yfinance and other slow external calls.
Uses diskcache (SQLite-backed) stored at .cache/stocks/ in the repo root.

Usage:
    from cache_utils import disk_cache, get_cached_ticker_info, get_cached_vix

    # Option 1: use the decorator directly on any function
    @disk_cache.memoize(expire=3600)
    def fetch_fundamentals(symbol: str) -> dict:
        return yf.Ticker(symbol).fast_info

    # Option 2: use the pre-built helpers
    info = get_cached_ticker_info("AAPL")           # cached 1h
    hist = get_cached_ticker_history("AAPL", "1y")  # cached 24h
    vix  = get_cached_vix()                         # cached 1h

Cache TTL policy:
    - Ticker fast_info  : 1 hour  (intraday prices change)
    - Ticker history    : 24 hours (daily OHLCV is stable)
    - VIX               : 1 hour  (regime changes intraday)
    - Fundamentals      : 24 hours (PE/book/div rarely change intraday)

To clear cache:
    from cache_utils import disk_cache
    disk_cache.clear()
"""
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import diskcache
from pathlib import Path

# Cache stored in project-local .cache/ directory (gitignored)
_CACHE_DIR = Path(__file__).parent / ".cache" / "stocks"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

disk_cache = diskcache.Cache(str(_CACHE_DIR), size_limit=500 * 1024 * 1024)  # 500 MB max

# Session-level negative cache: symbols that returned 404 (BUG-004 fix)
# Each entry is (symbol, timestamp) so we can expire stale entries.
import time as _time

_yf_404_symbols: dict[str, float] = {}
_YF_404_TTL: float = 1800.0  # 30 minutes TTL for negative cache entries


def _is_yf_404(symbol: str) -> bool:
    """Check if symbol is in negative cache and not expired."""
    ts = _yf_404_symbols.get(symbol)
    if ts is None:
        return False
    if _time.monotonic() - ts > _YF_404_TTL:
        _yf_404_symbols.pop(symbol, None)
        return False
    return True


def _mark_yf_404(symbol: str) -> None:
    """Add symbol to negative cache with current timestamp."""
    _yf_404_symbols[symbol] = _time.monotonic()


def _is_http_404(exc: Exception) -> bool:
    """Check if the root cause is an HTTP 404 (symbol not found on Yahoo)."""
    # yfinance wraps requests.HTTPError in various ways
    import requests
    cause = exc.__cause__ or exc
    if isinstance(cause, requests.exceptions.HTTPError):
        resp = getattr(cause, "response", None)
        if resp is not None and resp.status_code == 404:
            return True
    return False



@disk_cache.memoize(expire=3600)          # 1 hour TTL
def get_cached_ticker_info(symbol: str) -> dict:
    """Fetch yfinance fast_info with 1-hour disk cache."""
    if _is_yf_404(symbol):
        return {}
    import yfinance as yf
    try:
        return dict(yf.Ticker(symbol).fast_info)
    except (KeyError, AttributeError) as exc:
        if _is_http_404(exc):
            _mark_yf_404(symbol)
        return {}
    except Exception:
        return {}  # transient error — don't blacklist


@disk_cache.memoize(expire=86400)         # 24 hour TTL
def get_cached_ticker_history(symbol: str, period: str = "1y"):
    """Fetch yfinance history with 24-hour disk cache."""
    if _is_yf_404(symbol):
        import pandas as pd
        return pd.DataFrame()
    import yfinance as yf
    try:
        return yf.Ticker(symbol).history(period=period)
    except (KeyError, AttributeError) as exc:
        if _is_http_404(exc):
            _mark_yf_404(symbol)
        import pandas as pd
        return pd.DataFrame()
    except Exception:
        import pandas as pd
        return pd.DataFrame()  # transient error — don't blacklist


@disk_cache.memoize(expire=3600)          # 1 hour TTL
def get_cached_vix() -> float | None:
    """Fetch VIX with 1-hour cache (used in sell_strategy, tv_screener_fundamentals)."""
    import yfinance as yf
    try:
        return yf.Ticker("^VIX").fast_info.get("lastPrice")
    except Exception:
        return None


@disk_cache.memoize(expire=86400)         # 24 hour TTL
def get_cached_fundamentals(symbol: str) -> dict:
    """Fetch yfinance info (PE, book value, dividend, sector) with 24h cache."""
    if _is_yf_404(symbol):
        return {}
    import yfinance as yf
    try:
        return yf.Ticker(symbol).info or {}
    except (KeyError, AttributeError) as exc:
        if _is_http_404(exc):
            _mark_yf_404(symbol)
        return {}
    except Exception:
        return {}  # transient error — don't blacklist


def cache_stats() -> dict:
    """Return cache stats and disk usage."""
    return {
        "size_bytes": disk_cache.volume(),
        "size_mb": round(disk_cache.volume() / 1024 / 1024, 2),
        "item_count": len(disk_cache),
        "cache_dir": str(_CACHE_DIR),
    }


def cache_clear():
    """Clear all cached entries."""
    disk_cache.clear()
    print("Cache cleared.")


# ── Batch / Parallel helpers ───────────────────────────────────────────────────

def get_cached_fast_info_batch(
    symbols: "list[str]",
    max_workers: int = 8,
) -> "dict[str, dict]":
    """
    Fetch fast_info for multiple symbols in parallel using ThreadPoolExecutor.

    Each symbol is resolved independently through ``get_cached_ticker_info()``
    which applies the disk cache, negative-404 cache, and error handling.

    Args:
        symbols:     List of Yahoo Finance ticker symbols (e.g. ["AAPL", "MSFT"])
        max_workers: Concurrent threads.  yfinance is thread-safe; 8 is a safe
                     default.  Reduce to 3 if you see rate-limit warnings.

    Returns:
        Dict mapping symbol → fast_info dict (empty dict on failure).

    Cold-start speedup vs sequential:
        20 symbols × 740ms ≈ 14.8s  →  ~3.2s with 8 workers  (4.6× speedup)
    """
    from tradingview.parallel_fetch import parallel_map, WORKERS_YFINANCE_INFO

    workers = min(max_workers, WORKERS_YFINANCE_INFO, len(symbols)) if symbols else 1
    raw = parallel_map(
        symbols,
        fetch_fn=get_cached_ticker_info,
        max_workers=workers,
        desc="fast_info",
        skip_errors=True,
    )
    return {s: (info or {}) for s, info in raw.items()}


def get_cached_history_batch(
    symbols: "list[str]",
    period: str = "1y",
    max_workers: int = 5,
) -> "dict[str, 'pd.DataFrame']":
    """
    Fetch historical OHLCV data for multiple symbols in parallel.

    Args:
        symbols:     List of ticker symbols
        period:      yfinance period string (default "1y")
        max_workers: Concurrent threads (default 5)

    Returns:
        Dict mapping symbol → pd.DataFrame (empty DataFrame on failure).

    Cold-start speedup vs sequential:
        15 symbols × 1.2s ≈ 18s  →  ~3.5s with 5 workers  (5.1× speedup)
    """
    import pandas as pd
    from tradingview.parallel_fetch import parallel_map, WORKERS_YFINANCE_HISTORY

    workers = min(max_workers, WORKERS_YFINANCE_HISTORY, len(symbols)) if symbols else 1
    raw = parallel_map(
        symbols,
        fetch_fn=lambda s: get_cached_ticker_history(s, period),
        max_workers=workers,
        desc="history",
        skip_errors=True,
    )
    return {s: (df if df is not None else pd.DataFrame()) for s, df in raw.items()}


if __name__ == "__main__":
    stats = cache_stats()
    print(f"Cache: {stats['item_count']} items, {stats['size_mb']:.1f} MB at {stats['cache_dir']}")
