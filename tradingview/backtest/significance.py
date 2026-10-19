#!/usr/bin/env python3
"""Significance testing for score-bucket return differences.

Answers the only decision-grade question the bucket table raises: is the
return gap between high-score and low-score alerts real, or is it noise?

Two complementary tests:
  • Percentile bootstrap CI  — how uncertain is the observed gap?
  • Permutation test         — could random label assignment produce it?

Pure statistics: no database, no HTML, no network.
"""
import sys
import json
import argparse
from pathlib import Path

import numpy as np

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent.parent

# Below this many observations in either arm, a "significant" result is not
# trustworthy even when the interval excludes zero.
MIN_SAMPLES_FOR_POWER: int = 30

VERDICT_POSITIVE = "EDGE CONFIRMED"
VERDICT_NEGATIVE = "INVERTED EDGE"
VERDICT_UNKNOWN = "UNDETERMINED"
VERDICT_NO_DATA = "INSUFFICIENT DATA"


def bootstrap_mean_diff_ci(
    treatment: list[float],
    control: list[float],
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict:
    """Percentile bootstrap CI for mean(treatment) - mean(control).

    Resamples each arm independently with replacement, preserving each arm's
    own sample size so a small arm keeps its true fragility.
    """
    t = np.asarray(treatment, dtype=float)
    c = np.asarray(control, dtype=float)

    if t.size == 0 or c.size == 0:
        return {
            "n_treatment": int(t.size), "n_control": int(c.size),
            "observed_diff": 0.0, "ci_low": 0.0, "ci_high": 0.0,
            "confidence": confidence, "n_resamples": n_resamples,
            "excludes_zero": False, "underpowered": True,
        }

    observed = float(t.mean() - c.mean())
    rng = np.random.default_rng(seed)

    # Index-based resampling keeps the whole loop vectorised.
    t_draws = t[rng.integers(0, t.size, size=(n_resamples, t.size))].mean(axis=1)
    c_draws = c[rng.integers(0, c.size, size=(n_resamples, c.size))].mean(axis=1)
    diffs = t_draws - c_draws

    alpha = (1.0 - confidence) / 2.0
    ci_low = float(np.percentile(diffs, alpha * 100.0))
    ci_high = float(np.percentile(diffs, (1.0 - alpha) * 100.0))

    return {
        "n_treatment": int(t.size),
        "n_control": int(c.size),
        "observed_diff": round(observed, 4),
        "ci_low": round(ci_low, 4),
        "ci_high": round(ci_high, 4),
        "confidence": confidence,
        "n_resamples": n_resamples,
        "excludes_zero": bool(ci_low > 0.0 or ci_high < 0.0),
        "underpowered": bool(min(t.size, c.size) < MIN_SAMPLES_FOR_POWER),
    }


def permutation_test_mean_diff(
    treatment: list[float],
    control: list[float],
    n_resamples: int = 10_000,
    seed: int = 42,
) -> dict:
    """Two-sided permutation test for a difference in means.

    Shuffles the bucket labels across the pooled returns, so the null is
    "the score carries no information about the outcome".
    """
    t = np.asarray(treatment, dtype=float)
    c = np.asarray(control, dtype=float)

    if t.size == 0 or c.size == 0:
        return {"observed_diff": 0.0, "p_value": 1.0,
                "n_resamples": n_resamples, "significant": False}

    observed = float(t.mean() - c.mean())
    pooled = np.concatenate([t, c])
    n_t = t.size
    rng = np.random.default_rng(seed)

    perm = np.array([rng.permutation(pooled) for _ in range(n_resamples)])
    perm_diffs = perm[:, :n_t].mean(axis=1) - perm[:, n_t:].mean(axis=1)

    # +1 in numerator and denominator: the observed split is itself one of the
    # possible arrangements, which keeps p strictly positive.
    n_extreme = int(np.sum(np.abs(perm_diffs) >= abs(observed) - 1e-12))
    p_value = (n_extreme + 1) / (n_resamples + 1)

    return {
        "observed_diff": round(observed, 4),
        "p_value": round(float(p_value), 4),
        "n_resamples": n_resamples,
        "significant": bool(p_value < 0.05),
    }


def _verdict(ci: dict, perm: dict) -> str:
    if ci["n_treatment"] == 0 or ci["n_control"] == 0:
        return VERDICT_NO_DATA
    if not ci["excludes_zero"] or not perm["significant"]:
        return VERDICT_UNKNOWN
    return VERDICT_POSITIVE if ci["observed_diff"] > 0 else VERDICT_NEGATIVE


def compare_score_buckets(
    trades: list[dict],
    treatment_buckets: tuple[str, ...] = ("APEX",),
    control_buckets: tuple[str, ...] = ("LOW",),
    return_key: str = "actual_return",
    bucket_key: str = "bucket",
    n_resamples: int = 10_000,
    seed: int = 42,
) -> dict:
    """Bootstrap CI + permutation test for one bucket group against another."""
    def _returns(names: tuple[str, ...]) -> list[float]:
        out = []
        for t in trades:
            if t.get(bucket_key) not in names:
                continue
            val = t.get(return_key, t.get("ret"))
            if val is not None:
                out.append(float(val))
        return out

    treat = _returns(treatment_buckets)
    ctrl = _returns(control_buckets)

    ci = bootstrap_mean_diff_ci(treat, ctrl, n_resamples=n_resamples, seed=seed)
    perm = permutation_test_mean_diff(treat, ctrl, n_resamples=n_resamples, seed=seed)

    return {
        "label": f"{'+'.join(treatment_buckets)} vs {'+'.join(control_buckets)}",
        "treatment_buckets": list(treatment_buckets),
        "control_buckets": list(control_buckets),
        "treatment_mean": round(float(np.mean(treat)), 4) if treat else 0.0,
        "control_mean": round(float(np.mean(ctrl)), 4) if ctrl else 0.0,
        "bootstrap": ci,
        "permutation": perm,
        "verdict": _verdict(ci, perm),
    }


# Default comparisons: the narrow top-bucket test, plus a pooled version that
# trades purity for the sample size the narrow test lacks.
DEFAULT_COMPARISONS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("APEX",), ("LOW",)),
    (("HIGH", "APEX"), ("LOW", "MED")),
]


def run_significance_suite(
    trades: list[dict],
    n_resamples: int = 10_000,
    seed: int = 42,
    return_key: str = "actual_return",
) -> list[dict]:
    """Run every default bucket comparison and return one result per pair."""
    return [
        compare_score_buckets(
            trades, treatment_buckets=t, control_buckets=c,
            return_key=return_key, n_resamples=n_resamples, seed=seed,
        )
        for t, c in DEFAULT_COMPARISONS
    ]


def format_result(res: dict) -> str:
    """One-line human summary of a single comparison."""
    ci = res["bootstrap"]
    perm = res["permutation"]
    warn = "  [UNDERPOWERED]" if ci["underpowered"] else ""
    return (
        f"{res['label']:24s} diff={ci['observed_diff']:+7.3f}%  "
        f"95% CI [{ci['ci_low']:+7.3f}%, {ci['ci_high']:+7.3f}%]  "
        f"p={perm['p_value']:.4f}  n={ci['n_treatment']}v{ci['n_control']}  "
        f"→ {res['verdict']}{warn}"
    )


def main(args) -> None:
    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    trades = data.get("scatter_trades", [])
    if not trades:
        raise SystemExit(f"No scatter_trades found in {args.json}")

    print(f"Loaded {len(trades)} trades from {args.json}")
    print(f"Bootstrap + permutation with {args.n_resamples} resamples (seed={args.seed})\n")

    results = run_significance_suite(
        trades, n_resamples=int(args.n_resamples), seed=int(args.seed),
        return_key="ret",
    )
    for res in results:
        print(format_result(res))

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nSaved: {args.out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bootstrap CI + permutation test on score-bucket return gaps"
    )
    parser.add_argument(
        "--json", default=str(ROOT / "reports" / "backtest_vectorbt_report.json"),
        help="Path to backtest_vectorbt_report.json sidecar",
    )
    parser.add_argument("--n-resamples", default=10_000, type=int)
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--out", default="", help="Optional path to write results JSON")
    main(parser.parse_args())
