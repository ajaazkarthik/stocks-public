"""
Benchmark 10 — vectorbt Walk-Forward Backtest (T2.6)
=====================================================
Measures runtime of score-bucketed portfolio construction + walk-forward
compared to the old manual pandas-only approach.

Usage:
    python benchmarks/10_vectorbt_benchmark.py
"""

import sys, time, sqlite3, warnings
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent.parent
TV   = ROOT / "tradingview"
_DB  = ROOT / "database" / "alert_tracker.db"

sys.path.insert(0, str(TV))
from backtest_vectorbt import load_trades, compute_bucket_stats, walk_forward  # type: ignore


# ─── Baseline: manual pandas-only stats (no vectorbt Portfolio) ───────────────

def _manual_stats(trades: list[dict]) -> dict[str, dict]:
    """Compute stats manually (pandas only) — baseline comparison."""
    BUCKETS = [("LOW", 0, 55), ("MED", 55, 65), ("HIGH", 65, 75), ("APEX", 75, 101)]
    results = {}
    for name, lo, hi in BUCKETS:
        rets = [t["return_7d"] for t in trades if lo <= t["score"] < hi]
        if not rets:
            results[name] = {}
            continue
        import pandas as pd
        s = pd.Series(rets)
        results[name] = {
            "n":         len(rets),
            "win_rate":  float((s > 0).mean() * 100),
            "avg_return": float(s.mean()),
        }
    return results


def run_benchmark(trials: int = 3) -> None:
    print("=" * 60)
    print("Benchmark 10 — vectorbt Walk-Forward Backtest (T2.6)")
    print("=" * 60)

    if not _DB.exists():
        print(f"\n  [SKIP] alert_tracker.db not found at {_DB}")
        print("  Creating synthetic dataset for benchmark ...")
        import random
        random.seed(42)
        trades = [
            {
                "symbol":    f"TICKER{i}",
                "fire_time": __import__("datetime").datetime(2026, 4, i % 30 + 1),
                "score":     random.uniform(30, 85),
                "entry_price": random.uniform(10, 1000),
                "return_7d":   random.gauss(0.5, 3.0),
                "max_return":  random.uniform(0, 5),
                "max_drawdown": random.uniform(-3, 0),
                "status": "open",
            }
            for i in range(600)
        ]
    else:
        print(f"\n  Loading from {_DB.name} ...")
        trades = load_trades(str(_DB), min_return_abs=0.001)

    print(f"  Trades loaded: {len(trades)}")
    print()

    # ── Baseline: manual pandas ──────────────────────────────────────────────
    baseline_times = []
    print(f"  BASELINE (manual pandas, no vectorbt) × {trials} trials:")
    for i in range(trials):
        t0 = time.perf_counter()
        _manual_stats(trades)
        elapsed = time.perf_counter() - t0
        baseline_times.append(elapsed)
        print(f"    Trial {i+1}: {elapsed:.4f}s")

    # ── vectorbt portfolio stats ─────────────────────────────────────────────
    vbt_times = []
    print(f"\n  VECTORBT portfolio stats × {trials} trials:")
    for i in range(trials):
        t0 = time.perf_counter()
        stats = compute_bucket_stats(trades)
        elapsed = time.perf_counter() - t0
        vbt_times.append(elapsed)
        print(f"    Trial {i+1}: {elapsed:.4f}s")

    # ── Walk-forward ──────────────────────────────────────────────────────────
    wf_times = []
    print(f"\n  WALK-FORWARD (3 windows × buckets) × {trials} trials:")
    for i in range(trials):
        t0 = time.perf_counter()
        windows = walk_forward(trades, n_windows=3)
        elapsed = time.perf_counter() - t0
        wf_times.append(elapsed)
        print(f"    Trial {i+1}: {elapsed:.4f}s  windows={len(windows)}")

    # ── Summary ───────────────────────────────────────────────────────────────
    base_avg = sum(baseline_times) / len(baseline_times)
    vbt_avg  = sum(vbt_times)      / len(vbt_times)
    wf_avg   = sum(wf_times)       / len(wf_times)

    print("\n" + "=" * 60)
    print(f"  Baseline (pandas)    avg: {base_avg:.4f}s")
    print(f"  vectorbt stats       avg: {vbt_avg:.4f}s")
    print(f"  Walk-forward 3w      avg: {wf_avg:.4f}s  (includes 3 × bucket stats)")
    print(f"  Walk-forward overhead:   +{(wf_avg - vbt_avg):.4f}s vs single pass")
    print()
    print(f"  Bucket results (overall):")
    for bname in ("LOW", "MED", "HIGH", "APEX"):
        s = stats.get(bname, {})
        if s.get("n", 0) == 0:
            continue
        print(f"    {bname:4s}: n={s['n']:3d}  win={s['win_rate']:.1f}%  "
              f"sharpe={s['sharpe']:.3f}  max_dd={s['max_drawdown']:.2f}%")
    print("=" * 60)

    # Integrity check
    assert stats.get("MED", {}).get("n", 0) > 0, "MED bucket must have trades"
    print("\n  ✓ Integrity check PASSED")
    print("✅  T2.6 Benchmark complete.\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="vectorbt backtest benchmark (T2.6)")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    run_benchmark(trials=args.trials)
