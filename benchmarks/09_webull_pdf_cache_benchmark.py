"""
Benchmark 09 — Webull PDF Cache (T1.4)
=======================================
Measures the speedup from caching PDF parse results vs re-parsing every call.

Simulates the two scenarios:
  • COLD  — no cache exists, parse_all_pdfs() must run (2–5 s on a real statement)
  • WARM  — cache exists and fingerprint matches, pure JSON load (<50 ms)

Uses a small in-memory synthetic PDF via reportlab (if available) or a pre-built
fixture dict to avoid real PDF I/O while still exercising all cache code paths.

Usage:
    python benchmarks/09_webull_pdf_cache_benchmark.py
"""

import sys, os, json, time, tempfile, shutil
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Stub for parse_all_pdfs — simulates a 2-second PDF parse
# ---------------------------------------------------------------------------

_FAKE_DATA = {
    "holdings": [
        {"symbol": "CBA", "qty": 100, "avg_price": 95.5, "close_price": 100.0,
         "currency": "AUD", "exchange": "ASX"},
        {"symbol": "BHP", "qty": 200, "avg_price": 42.0, "close_price": 44.0,
         "currency": "AUD", "exchange": "ASX"},
        {"symbol": "AAPL", "qty": 50, "avg_price": 170.0, "close_price": 175.0,
         "currency": "USD", "exchange": "NASDAQ"},
    ],
    "latest_date": "2026-04-10",
}

_PARSE_DELAY = 2.0  # simulated PDF parsing time (seconds)


def _simulated_parse_all_pdfs(pdf_dir: str) -> dict:
    """Simulates parse_all_pdfs() with realistic delay."""
    time.sleep(_PARSE_DELAY)
    return _FAKE_DATA


# ---------------------------------------------------------------------------
# Cache logic (exact copy from sell_strategy._fetch_webull_positions)
# ---------------------------------------------------------------------------

def _fetch_webull_positions_uncached(pdf_dir: Path) -> tuple[list[dict], str]:
    """Original uncached version."""
    data = _simulated_parse_all_pdfs(str(pdf_dir))
    return data.get("holdings", []), "parsed"


def _fetch_webull_positions_cached(pdf_dir: Path, cache_path: Path) -> tuple[list[dict], str]:
    """Cached version (mirrors the fix in sell_strategy.py)."""
    pdf_files = sorted(pdf_dir.glob("*.pdf"))
    fingerprint = {f.name: f.stat().st_mtime for f in pdf_files}

    data: dict | None = None
    _cache_source = "parsed"
    if cache_path.exists():
        try:
            _disk = json.loads(cache_path.read_text(encoding="utf-8"))
            if _disk.get("fingerprint") == fingerprint:
                data = _disk.get("data")
                _cache_source = "cache"
        except Exception:
            pass

    if data is None:
        data = _simulated_parse_all_pdfs(str(pdf_dir))
        try:
            cache_path.write_text(
                json.dumps({"fingerprint": fingerprint, "data": data},
                           indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    return data.get("holdings", []), _cache_source


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(trials: int = 3) -> None:
    print("=" * 60)
    print("Benchmark 09 — Webull PDF Cache (T1.4)")
    print("=" * 60)

    # Set up a temp directory with two fake .pdf files (just empty files)
    tmp = Path(tempfile.mkdtemp(prefix="wb_bench_"))
    try:
        (tmp / "statement_2026-03.pdf").touch()
        (tmp / "statement_2026-04.pdf").touch()
        cache_path = tmp / "_webull_parse_cache.json"

        # ── COLD (no cache) ──────────────────────────────────────────────
        cold_times = []
        print(f"\n  COLD (no cache, simulated {_PARSE_DELAY}s parse × {trials} trials):")
        for i in range(trials):
            cache_path.unlink(missing_ok=True)  # ensure no cache
            t0 = time.perf_counter()
            holdings, src = _fetch_webull_positions_cached(tmp, cache_path)
            elapsed = time.perf_counter() - t0
            cold_times.append(elapsed)
            print(f"    Trial {i+1}: {elapsed:.3f}s  source={src}  holdings={len(holdings)}")

        # ── WARM (cache hit) ─────────────────────────────────────────────
        warm_times = []
        print(f"\n  WARM (cache hit × {trials} trials):")
        # Pre-warm: cache is already written from last cold run
        for i in range(trials):
            t0 = time.perf_counter()
            holdings, src = _fetch_webull_positions_cached(tmp, cache_path)
            elapsed = time.perf_counter() - t0
            warm_times.append(elapsed)
            print(f"    Trial {i+1}: {elapsed:.3f}s  source={src}  holdings={len(holdings)}")

        # ── Summary ──────────────────────────────────────────────────────
        cold_avg = sum(cold_times) / len(cold_times)
        warm_avg = sum(warm_times) / len(warm_times)
        speedup  = cold_avg / warm_avg if warm_avg > 0 else float("inf")

        print("\n" + "=" * 60)
        print(f"  Cold (parse)  avg: {cold_avg:.3f}s")
        print(f"  Warm (cache)  avg: {warm_avg:.4f}s")
        print(f"  Speedup:          {speedup:.0f}x")
        print(f"\n  Result integrity: {len(holdings)} holdings match expected {len(_FAKE_DATA['holdings'])}")
        assert len(holdings) == len(_FAKE_DATA["holdings"]), "Holdings count mismatch!"
        print("  ✓ Integrity check PASSED")
        print("=" * 60)

        # Cache invalidation test
        print("\n  CACHE INVALIDATION TEST:")
        (tmp / "statement_2026-05.pdf").touch()  # add new PDF
        t0 = time.perf_counter()
        holdings2, src2 = _fetch_webull_positions_cached(tmp, cache_path)
        elapsed2 = time.perf_counter() - t0
        print(f"    New PDF added → source={src2}  elapsed={elapsed2:.3f}s")
        assert src2 == "parsed", f"Expected re-parse on new PDF, got: {src2}"
        print("  ✓ Cache invalidation PASSED (new PDF triggers re-parse)")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n✅  T1.4 Benchmark complete.\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Webull PDF cache benchmark (T1.4)")
    parser.add_argument("--trials", type=int, default=3, help="Number of trials per scenario")
    args = parser.parse_args()
    run_benchmark(trials=args.trials)
