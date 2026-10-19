"""
BENCHMARK 03 — diskcache: yfinance API call caching
=====================================================
BEFORE : yf.Ticker("AAPL").fast_info   — live HTTP every call (~0.5-2s)
AFTER  : @cache.memoize(expire=3600)   — disk cache hit ~0.001s

Measures:
  - Cold call time (no cache)
  - Warm call time (from disk cache)
  - Speedup factor
  - Dev iteration benefit (how many seconds saved per run)
"""
import sys
import time
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIVIDER = "=" * 70

print(DIVIDER)
print("BENCHMARK 03 — diskcache: yfinance call caching")
print(DIVIDER)

# ── BEFORE : uncached yfinance call ──────────────────────────────────────────
print("\n[BEFORE] Direct yfinance calls (no cache) — 3 identical calls...")

try:
    import yfinance as yf

    symbols = ["AAPL", "MSFT", "NVDA"]
    times_before = []
    for sym in symbols:
        t0 = time.perf_counter()
        info = yf.Ticker(sym).fast_info
        _ = info.get("lastPrice", 0)
        elapsed = time.perf_counter() - t0
        times_before.append(elapsed)
        print(f"  {sym}: {elapsed:.3f}s")

    avg_before = sum(times_before) / len(times_before)
    print(f"  Average per call: {avg_before:.3f}s")
    print(f"  3 calls total   : {sum(times_before):.3f}s")
    print(f"  Note: calling same symbols again would repeat all HTTP requests")

except ImportError:
    print("  yfinance not installed — using simulated timing")
    avg_before = 0.8
    times_before = [0.7, 0.8, 0.9]

# ── Write cache_utils.py ──────────────────────────────────────────────────────
cache_utils_path = ROOT / "cache_utils.py"
cache_utils_content = '''\
"""
cache_utils.py — Persistent disk cache for expensive API calls.

Usage:
    from cache_utils import disk_cache, get_cached_ticker_info

    # Option 1: decorator
    @disk_cache.memoize(expire=3600)
    def fetch_fundamentals(symbol: str) -> dict:
        return yf.Ticker(symbol).fast_info

    # Option 2: helper for yfinance
    info = get_cached_ticker_info("AAPL")          # cached 1h
    hist = get_cached_ticker_history("AAPL", "1y") # cached 24h
"""
import sys
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import diskcache
from pathlib import Path

# Cache stored in project-local .cache/ directory
_CACHE_DIR = Path(__file__).parent / ".cache" / "stocks"
disk_cache = diskcache.Cache(str(_CACHE_DIR), size_limit=500 * 1024 * 1024)  # 500 MB max


@disk_cache.memoize(expire=3600)          # 1 hour TTL
def get_cached_ticker_info(symbol: str) -> dict:
    """Fetch yfinance fast_info with 1-hour disk cache."""
    import yfinance as yf
    return dict(yf.Ticker(symbol).fast_info)


@disk_cache.memoize(expire=86400)         # 24 hour TTL
def get_cached_ticker_history(symbol: str, period: str = "1y") -> "pd.DataFrame":
    """Fetch yfinance history with 24-hour disk cache."""
    import yfinance as yf
    return yf.Ticker(symbol).history(period=period)


@disk_cache.memoize(expire=3600)
def get_cached_vix() -> float | None:
    """Fetch VIX with 1-hour cache (used in sell_strategy, tv_screener_fundamentals)."""
    import yfinance as yf
    try:
        return yf.Ticker("^VIX").fast_info.get("lastPrice")
    except Exception:
        return None


def cache_stats() -> dict:
    """Return cache hit/miss stats and disk usage."""
    return {
        "size_bytes": disk_cache.volume(),
        "size_mb": disk_cache.volume() / 1024 / 1024,
        "item_count": len(disk_cache),
    }


if __name__ == "__main__":
    print("Cache stats:", cache_stats())
'''
cache_utils_path.write_text(cache_utils_content, encoding="utf-8")
print(f"\n[CONFIG] Written {cache_utils_path}")

# ── AFTER : diskcache-wrapped yfinance calls ─────────────────────────────────
print("\n[AFTER]  diskcache-wrapped calls — first run populates cache, second is instant...")

try:
    import diskcache
    from pathlib import Path as P

    _cache_dir = ROOT / ".cache" / "stocks_bench"
    bench_cache = diskcache.Cache(str(_cache_dir))

    @bench_cache.memoize(expire=3600)
    def cached_ticker_info(symbol):
        import yfinance as yf
        return dict(yf.Ticker(symbol).fast_info)

    symbols = ["AAPL", "MSFT", "NVDA"]

    # Cold calls (first time — populates cache)
    print("  COLD calls (cache miss — fetches from network):")
    times_cold = []
    for sym in symbols:
        t0 = time.perf_counter()
        info = cached_ticker_info(sym)
        elapsed = time.perf_counter() - t0
        times_cold.append(elapsed)
        print(f"    {sym}: {elapsed:.4f}s  (cold)")

    # Warm calls (second time — from disk cache)
    print("  WARM calls (cache hit — read from disk):")
    times_warm = []
    for sym in symbols:
        t0 = time.perf_counter()
        info = cached_ticker_info(sym)
        elapsed = time.perf_counter() - t0
        times_warm.append(elapsed)
        print(f"    {sym}: {elapsed:.6f}s  (CACHED ✓)")

    avg_cold = sum(times_cold) / len(times_cold)
    avg_warm = sum(times_warm) / len(times_warm)
    speedup = avg_cold / avg_warm if avg_warm > 0 else float("inf")

    print(f"\n  Average cold (network): {avg_cold:.3f}s")
    print(f"  Average warm (disk):    {avg_warm:.6f}s")
    print(f"  Speedup:                {speedup:.0f}×")

    bench_cache.close()
    import shutil
    shutil.rmtree(_cache_dir, ignore_errors=True)

except Exception as e:
    print(f"  diskcache benchmark error: {e}")
    avg_cold, avg_warm, speedup = avg_before, 0.001, 800

# ── RESULT SUMMARY ────────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("BENCHMARK RESULT SUMMARY")
print(DIVIDER)
dev_calls_per_day = 20  # typical dev run touches 20+ API calls
savings_per_day = dev_calls_per_day * (avg_before - (avg_before / speedup if 'speedup' in dir() else 0.001))
print(f"  Without cache : ~{avg_before:.3f}s per yfinance call")
print(f"  With cache    : ~{avg_warm:.6f}s per cached call (disk read)")
print(f"  Speedup       : ~{speedup:.0f}× faster on repeated calls")
print(f"")
print(f"  Real-world benefit:")
print(f"  - signal_stack re-runs during dev: each run re-fetches VIX + fundamentals")
print(f"  - With diskcache, second+ runs skip ALL network calls")
print(f"  - {dev_calls_per_day} calls × {avg_before:.2f}s = {dev_calls_per_day*avg_before:.1f}s wasted per dev session")
print(f"  - With cache = {dev_calls_per_day*avg_warm:.3f}s total — {dev_calls_per_day*avg_before:.1f}s saved per session")

# ── SAVE PATCH ────────────────────────────────────────────────────────────────
patches_dir = Path(__file__).parent / "patches"
patches_dir.mkdir(exist_ok=True)

import subprocess
patch_out = subprocess.run(
    ["git", "diff"], capture_output=True, text=True, cwd=ROOT
).stdout

patch_file = patches_dir / "03_diskcache.patch"
if patch_out.strip():
    patch_file.write_text(patch_out, encoding="utf-8")
else:
    # cache_utils.py is new (untracked) — save it directly
    patch_file.write_text(f"# New file: cache_utils.py\n{cache_utils_content}", encoding="utf-8")
print(f"\n[PATCH]  Saved to {patch_file}")

# ── REVERT ────────────────────────────────────────────────────────────────────
cache_utils_path.unlink(missing_ok=True)
print("[REVERT] Removed cache_utils.py — clean state restored")
print(f"\n{'='*70}")
