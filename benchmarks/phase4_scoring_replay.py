#!/usr/bin/env python3
"""Phase 4 — paired point-in-time Signal Stack replay (no brokerage/market I/O).

This tool does NOT fetch retrospective fundamentals or market history.
Inputs must be archived *as observed at the original decision time*.
Run --mode historical for matured, timestamped underlying-stock outcomes;
run --mode shadow without outcomes to compare score/eligibility silently.
Nothing writes to alert stores, places orders or sends Telegram messages.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import hashlib
import math
from pathlib import Path
import random
import statistics
import sys

MARKETS = frozenset(("US", "AU", "IN"))
BUNDLES = ("meta", "screener", "fundamental", "sentiment", "internals", "yc")
SCORERS = frozenset((
    "_score_number", "_score_observed", "score_technical",
    "score_fundamental", "score_sentiment", "score_market_internals",
    "_data_confidence", "_composite_components", "composite",
    "_compute_decision_scores",
))
BASE_COMMIT = "f3925b3cc7bd4f74f61f6f5e8edb0e3d7fbbfe9f"
PHASE4_VERSION = "phase4-paired-v1"
MIN_ALERTS = 30
MIN_SYMBOLS = 10
SCORE_GATE = 62.5
N_BOOT = 400


def utc(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be offset-aware ISO-8601 text")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO-8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("naive timestamps are prohibited")
    return result.astimezone(timezone.utc)


def finite_number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("price/return must be a finite JSON number")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError("price/return must be finite and positive when required")
    return value


def load_jsonl(path):
    if not path.is_file():
        raise ValueError(f"Required point-in-time input does not exist: {path}")
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
            if not isinstance(item, dict):
                raise ValueError("expected object")
            rows.append(item)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path.name} line {line_number}: invalid JSON object") from exc
    if not rows:
        raise ValueError(f"{path.name}: zero records; refusing success-shaped empty report")
    return rows


def git_blob_sha(data):
    """Compute the Git object ID, not a normal file SHA-1 digest."""
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii")
                        + b"\0" + data).hexdigest()


def engine_from_source(path):
    """Load only the actual scoring function AST: no imports or provider I/O."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in SCORERS]
    names = {node.name for node in nodes}
    required = SCORERS - {"_score_number", "_score_observed"}
    if not required.issubset(names):
        raise ValueError(f"Missing source functions in {path.name}: {required-names}")
    ns = {"math": math, "CONFIDENCE_DEMOTION_FLOOR": 0.85}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)
    return ns


def validate_signal(record):
    event = record.get("event_id")
    if not isinstance(event, str) or not event or len(event) > 120:
        raise ValueError("event_id is required and must be a bounded string")
    symbol = record.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 90:
        raise ValueError(f"{event}: valid symbol required")
    market = record.get("market")
    if market not in MARKETS:
        raise ValueError(f"{event}: market must be US/AU/IN, not inferred")
    decision = utc(record.get("decision_at"))
    fire = utc(record.get("signal_at"))
    if fire > decision:
        raise ValueError(f"{event}: signal_at occurs after decision_at")
    captured = utc(record.get("captured_at"))
    if captured > decision or captured < fire:
        raise ValueError(f"{event}: no contemporaneous feature capture")
    sources = record.get("source_asof")
    if not isinstance(sources, dict):
        raise ValueError(f"{event}: source_asof provenance is mandatory")
    for key in BUNDLES:
        payload = record.get(key, {})
        if not isinstance(payload, dict):
            raise ValueError(f"{event}: {key} must be an object")
        if payload:
            if key not in sources:
                raise ValueError(f"{event}: missing {key} source_asof")
            if utc(sources[key]) > captured:
                raise ValueError(f"{event}: {key} was observed after the snapshot capture")
    vix = record.get("vix")
    if vix is not None:
        finite_number(vix)
        if "vix" not in sources or utc(sources["vix"]) > captured:
            raise ValueError(f"{event}: VIX is not point-in-time")
    extra = record.get("regime_weights")
    if extra is not None:
        if set(extra) != {"l1", "l2", "l3", "l4"}:
            raise ValueError(f"{event}: invalid regime weights")
        vals = [finite_number(x) for x in extra.values()]
        if any(v < 0 for v in vals) or abs(sum(vals) - 1) > 1e-6:
            raise ValueError(f"{event}: invalid regime weight normalization")
    for key in ("l5", "l6", "l7"):
        if record.get(key) is not None:
            val = finite_number(record[key])
            if not 0 <= val <= 10:
                raise ValueError(f"{event}: {key} outside 0..10")
            # Additional scores are only usable if known before decision.
            if key not in sources or utc(sources[key]) > captured:
                raise ValueError(f"{event}: {key} is not point-in-time")
    for key in ("book_bonus", "pool_boost", "basket_boost"):
        if record.get(key) is not None:
            finite_number(record[key])
            if record[key] != 0 and (
                key not in sources or utc(sources[key]) > captured
            ):
                raise ValueError(f"{event}: {key} must have point-in-time provenance")
    if extra is not None and (
        "regime_weights" not in sources or utc(sources["regime_weights"]) > captured
    ):
        raise ValueError(f"{event}: regime must have point-in-time provenance")
    return event, decision


def score(engine, record):
    meta = record.get("meta") or {}
    sc = record.get("screener") or {}
    fund = record.get("fundamental") or {}
    sent = record.get("sentiment") or {}
    internals = record.get("internals") or {}
    yc = record.get("yc") or {}
    subscores = [
        engine["score_technical"](meta, sc),
        engine["score_fundamental"](fund),
        engine["score_sentiment"](sent),
        engine["score_market_internals"](internals, yc, record.get("vix")),
    ]
    layers = tuple(float(x["total"]) for x in subscores)
    if any(not math.isfinite(n) or n < 0 or n > 10 for n in layers):
        raise ValueError("nonfinite/out-of-range layer total (invalid replay)")
    cov = engine["_data_confidence"](meta, fund, sent, internals, sc)
    confidence = finite_number(cov["overall"])
    if not 0 <= confidence <= 1:
        raise ValueError("invalid confidence score")
    quality, decision = engine["_compute_decision_scores"](
        *layers,
        institutional_l5=record.get("l5"),
        l6=record.get("l6"), l7=record.get("l7"),
        regime_weights=record.get("regime_weights"),
        book_bonus=record.get("book_bonus", 0.0),
        pool_boost=record.get("pool_boost", 0.0),
        basket_boost=record.get("basket_boost", 0.0),
        data_confidence=confidence,
    )
    if any(not math.isfinite(n) or n < 0 or n > 100 for n in (quality, decision)):
        raise ValueError("invalid quality/decision")
    return {"layers": layers, "quality": quality, "decision": decision,
            "confidence": confidence}


def outcome_index(rows, *, evaluation_asof):
    lookup = {}
    for row in rows:
        event = row.get("event_id")
        horizon = row.get("horizon_days")
        if not isinstance(event, str) or not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
            raise ValueError("outcome event_id and positive integer horizon_days required")
        key = (event, horizon)
        if key in lookup:
            raise ValueError(f"duplicate outcome {key}")
        entry, exit_at = utc(row.get("entry_at")), utc(row.get("exit_at"))
        available = utc(row.get("available_at"))
        if not entry < exit_at <= available <= evaluation_asof:
            raise ValueError(f"{event}: outcome timestamps are immature/inconsistent")
        ep = finite_number(row.get("entry_price"), positive=True)
        xp = finite_number(row.get("exit_price"), positive=True)
        benchmark = None
        if ("benchmark_entry" in row) != ("benchmark_exit" in row):
            raise ValueError(f"{event}: benchmark pair must both be supplied")
        if "benchmark_entry" in row:
            bp = finite_number(row["benchmark_entry"], positive=True)
            bx = finite_number(row["benchmark_exit"], positive=True)
            benchmark = (bx / bp - 1) * 100
        lookup[key] = {"entry": entry, "exit": exit_at, "available": available,
                       "return_pct": (xp / ep - 1) * 100, "bench_pct": benchmark}
    return lookup


def rank_corr(x, y):
    if len(x) < 3:
        return None
    def midranks(v):
        indexed = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.] * len(v)
        pos = 0
        while pos < len(v):
            end = pos + 1
            while end < len(v) and v[indexed[end]] == v[indexed[pos]]:
                end += 1
            avg = (pos + end - 1) / 2
            for j in range(pos, end):
                out[indexed[j]] = avg
            pos = end
        return out
    a, b = midranks(x), midranks(y)
    ax, bx = statistics.mean(a), statistics.mean(b)
    den = math.sqrt(sum((n-ax)**2 for n in a) * sum((n-bx)**2 for n in b))
    return round(sum((u-ax)*(v-bx) for u, v in zip(a, b))/den, 4) if den else None


def avg(vals):
    return round(statistics.mean(vals), 4) if vals else None


def top_selection(rows, engine, portion=0.25):
    by_score = sorted(rows, key=lambda v: (-v[engine]["decision"], v["event_id"]))
    return by_score[:max(1, math.ceil(portion*len(by_score)))]


def top_gap(rows):
    old = [r["excess"] for r in top_selection(rows, "old") if r["excess"] is not None]
    new = [r["excess"] for r in top_selection(rows, "new") if r["excess"] is not None]
    return statistics.mean(new)-statistics.mean(old) if old and new else None


def cluster_ci(rows, *, draws=N_BOOT):
    symbols = sorted({r["symbol"] for r in rows})
    if len(symbols) < MIN_SYMBOLS:
        return None
    by_symbol = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)
    rng = random.Random(41027)
    samples = []
    for _ in range(draws):
        sampled = [r for symbol in rng.choices(symbols, k=len(symbols))
                   for r in by_symbol[symbol]]
        delta = top_gap(sampled)
        if delta is not None:
            samples.append(delta)
    if len(samples) < draws // 2:
        return None
    samples.sort()
    return [round(samples[int(.025*(len(samples)-1))], 4),
            round(samples[int(.975*(len(samples)-1))], 4)]


def score_bands(rows, model):
    """Descriptive return calibration, suppressed for thin bands."""
    edges = ((0, 50), (50, 60), (60, 70), (70, 80), (80, 101))
    result = {}
    for lower, upper in edges:
        matched = [r for r in rows
                   if lower <= r[model]["decision"] < upper]
        benchmarked = [r for r in matched if r["excess"] is not None]
        sufficiently_observed = (len(benchmarked) >= 10
                                  and len({r["symbol"] for r in benchmarked}) >= 5)
        result[f"{lower}-{upper}"] = {
            "count": len(matched), "benchmarked": len(benchmarked),
            "excess_pct": avg([r["excess"] for r in benchmarked])
                         if sufficiently_observed else None,
            "win_rate": avg([float(r["return_pct"] > 0) for r in benchmarked])
                        if sufficiently_observed else None,
        }
    return result


def coverage_bands(rows):
    """Assess whether corrections work differently across observed-data levels."""
    edges = ((0.0, 0.5, "sparse"), (0.5, 0.8, "partial"),
             (0.8, 1.00001, "well_observed"))
    output = {}
    for low, high, label in edges:
        matched = [r for r in rows if low <= r["new"]["confidence"] < high]
        valid = [r for r in matched if r["excess"] is not None]
        output[label] = {
            "count": len(matched), "benchmarked": len(valid),
            "distinct_symbols": len({r["symbol"] for r in valid}),
            "new_minus_old_top_excess_pct": round(top_gap(valid), 4)
                if (len(valid) >= MIN_ALERTS
                    and len({r["symbol"] for r in valid}) >= MIN_SYMBOLS
                    and top_gap(valid) is not None) else None,
        }
    return output


def group_result(rows):
    """Score/rank comparisons; performance withheld until sufficient samples."""
    valid = [r for r in rows if r["excess"] is not None]
    count = len(rows)
    symbols = len({r["symbol"] for r in rows})
    eligible = len(valid) >= MIN_ALERTS and len({r["symbol"] for r in valid}) >= MIN_SYMBOLS
    result = {
        "records": count, "distinct_symbols": symbols,
        "benchmarked": len(valid),
        "mean_score_old": avg([r["old"]["decision"] for r in rows]),
        "mean_score_new": avg([r["new"]["decision"] for r in rows]),
        "mean_delta": avg([r["new"]["decision"]-r["old"]["decision"] for r in rows]),
        "score_rank_correlation": rank_corr(
            [r["old"]["decision"] for r in rows],
            [r["new"]["decision"] for r in rows]),
        "gate_crossings_up": sum(r["old"]["decision"] < SCORE_GATE <= r["new"]["decision"] for r in rows),
        "gate_crossings_down": sum(r["new"]["decision"] < SCORE_GATE <= r["old"]["decision"] for r in rows),
        "decision_changed": sum(r["old"]["decision"] != r["new"]["decision"] for r in rows),
        "status": "EVALUABLE" if eligible else "INSUFFICIENT_DATA",
        "score_bands": {
            "old": score_bands(rows, "old"),
            "new": score_bands(rows, "new"),
        },
        "coverage_bands": coverage_bands(rows),
    }
    if not eligible:
        result["performance"] = None
        return result
    old_top, new_top = top_selection(valid, "old"), top_selection(valid, "new")
    gap = top_gap(valid)
    ci = cluster_ci(valid)
    result["performance"] = {
        "old_top_mean_excess_pct": avg([r["excess"] for r in old_top]),
        "new_top_mean_excess_pct": avg([r["excess"] for r in new_top]),
        "new_minus_old_top_excess_pct": round(gap, 4) if gap is not None else None,
        "old_top_win_rate": avg([float(r["return_pct"] > 0) for r in old_top]),
        "new_top_win_rate": avg([float(r["return_pct"] > 0) for r in new_top]),
        "old_score_vs_excess_rho": rank_corr(
            [r["old"]["decision"] for r in valid], [r["excess"] for r in valid]),
        "new_score_vs_excess_rho": rank_corr(
            [r["new"]["decision"] for r in valid], [r["excess"] for r in valid]),
        "cluster_bootstrap_95pct_gap": ci,
        "verdict": ("UNDETERMINED" if ci is None or ci[0] <= 0 <= ci[1]
                    else "POSITIVE_ASSOCIATION" if ci[0] > 0 else "NEGATIVE_ASSOCIATION"),
        "note": "Observational association; selection bias and trading costs not fully modeled",
    }
    return result


def replay(records, old_engine, new_engine, *, mode, asof, cutoff=None,
           outcomes=(), horizons=(5, 10, 20), cost_bps=0):
    if mode not in ("shadow", "historical"):
        raise ValueError("mode must be shadow or historical")
    if mode == "historical" and cutoff is None:
        raise ValueError("historical mode requires a chronological --cutoff")
    if mode == "historical" and not outcomes:
        raise ValueError("historical mode refuses missing outcome data")
    costs = finite_number(cost_bps)
    if costs < 0:
        raise ValueError("cost_bps must not be negative")
    observed = outcome_index(outcomes, evaluation_asof=asof) if mode == "historical" else {}
    seen = set()
    rows = []
    excluded = defaultdict(int)
    for record in records:
        event, decision = validate_signal(record)
        if event in seen:
            raise ValueError(f"duplicate signal event_id {event}")
        seen.add(event)
        if decision > asof:
            raise ValueError(f"{event}: future decision relative to evaluation time")
        old, new = score(old_engine, record), score(new_engine, record)
        base = {"event_id": event, "symbol": record["symbol"],
                "market": record["market"], "decision_at": decision.isoformat(),
                "old": old, "new": new, "changed": old["decision"] != new["decision"]}
        if mode == "shadow":
            rows.append(base)
            continue
        for horizon in horizons:
            # Holdout dates and embargo are based on ORIGINAL decision, not on outcomes.
            if decision <= cutoff - timedelta(days=horizon):
                split = "train"
            elif decision > cutoff:
                split = "test"
            else:
                excluded["embargo"] += 1
                continue
            item = observed.get((event, horizon))
            if item is None:
                excluded["missing_outcome"] += 1
                continue
            # Training targets must have actually matured and become available
            # BEFORE the train/test boundary. No outcome known only after the
            # holdout starts may influence train-period metrics or tuning.
            if split == "train" and item["available"] > cutoff:
                excluded["training_label_after_cutoff"] += 1
                continue
            if item["entry"] <= decision:
                raise ValueError(f"{event}: entry must be strictly AFTER decision time")
            if item["exit"] < decision + timedelta(days=horizon):
                raise ValueError(f"{event}: {horizon}-day outcome is not matured")
            net_return = item["return_pct"]-costs/100
            excess = (net_return-item["bench_pct"]
                      if item["bench_pct"] is not None else None)
            rows.append({**base, "split": split, "horizon_days": horizon,
                         "return_pct": round(net_return, 6),
                         "excess": round(excess, 6) if excess is not None else None})
    if mode == "shadow":
        return {"mode": mode, "version": PHASE4_VERSION,
                "source_rows": len(records), "scored": len(rows),
                "no_market_actions": True}, rows
    groups = {}
    for market in sorted(MARKETS):
        for horizon in horizons:
            for split in ("train", "test"):
                arm = [r for r in rows if r["market"] == market
                       and r["horizon_days"] == horizon and r["split"] == split]
                groups[f"{market}/{horizon}d/{split}"] = group_result(arm)
    return {
        "mode": mode, "version": PHASE4_VERSION, "baseline_commit": BASE_COMMIT,
        "source_rows": len(records), "measured_rows": len(rows),
        "exclusions": dict(excluded), "cost_bps": costs,
        "cutoff": cutoff.isoformat(), "evaluation_asof": asof.isoformat(),
        "explicitly_not_options_backtest": True, "groups": groups,
    }, rows


def write_results(path, report, rows):
    path.mkdir(parents=True, exist_ok=True)
    (path / "phase4_summary.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    with (path / "phase4_scores.jsonl").open("w", encoding="utf-8") as dest:
        for row in rows:
            dest.write(json.dumps(row, allow_nan=False, separators=(",", ":"))+"\n")
    markdown = ["# Phase 4 paired replay", "",
                f"Mode: **{report['mode']}**. Scored rows: **{len(rows)}**.", "",
                "These are underlying-stock observational outcomes, **not live trades or option P&L**.",
                "Synthetic test data must never be reported as evidence of trading edge.", ""]
    if report["mode"] == "historical":
        markdown += ["| Market / horizon / split | Samples | Symbols | Top excess Δ (pp) | Status |",
                     "|---|---:|---:|---:|---|"]
        for key, group in report["groups"].items():
            perf = group["performance"] or {}
            delta = perf.get("new_minus_old_top_excess_pct")
            markdown.append(f"| {key} | {group['benchmarked']} | {group['distinct_symbols']} | "
                            f"{delta if delta is not None else '—'} | {group['status']} |")
    (path / "phase4_summary.md").write_text("\n".join(markdown)+"\n", encoding="utf-8")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--baseline-source", type=Path, required=True,
                   help="Frozen source from public main at f3925b3; not a provider refetch")
    p.add_argument("--candidate-source", type=Path, required=True)
    p.add_argument("--snapshots", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--as-of", required=True, help="offset-aware evaluation timestamp")
    p.add_argument("--mode", choices=("historical", "shadow"), required=True)
    p.add_argument("--outcomes", type=Path)
    p.add_argument("--cutoff", help="offset-aware chronological train/test cutoff")
    p.add_argument("--cost-bps", type=float, default=0)
    args = p.parse_args(argv)
    try:
        if args.mode == "historical" and (args.outcomes is None or not args.cutoff):
            raise ValueError("historical requires --outcomes AND --cutoff")
        if args.mode == "shadow" and (args.outcomes or args.cutoff):
            raise ValueError("shadow forbids outcomes/cutoff to avoid outcome leakage")
        baseline_bytes = args.baseline_source.read_bytes()
        expected_blob = "d02df819c67385cd22a406649855dad73d68eac4"
        if git_blob_sha(baseline_bytes) != expected_blob:
            raise ValueError(
                "baseline source is not the exact public f3925b3 snapshot; "
                "the previous-model comparison would be untrustworthy")
        old = engine_from_source(args.baseline_source)
        new = engine_from_source(args.candidate_source)
        signals = load_jsonl(args.snapshots)
        outcomes = load_jsonl(args.outcomes) if args.outcomes else []
        report, rows = replay(
            signals, old, new, mode=args.mode, asof=utc(args.as_of),
            cutoff=utc(args.cutoff) if args.cutoff else None,
            outcomes=outcomes, cost_bps=args.cost_bps)
        write_results(args.out, report, rows)
    except (ValueError, KeyError, TypeError, OSError, SyntaxError, ZeroDivisionError) as exc:
        p.error(f"REFUSED incomplete/unsafe evaluation: {exc}")
    print(json.dumps({"report": str(args.out / "phase4_summary.json"),
                      "mode": args.mode, "evaluated": len(rows),
                      "data_sufficiency": "NOT_PROVEN" if args.mode == "historical" else "SHADOW_ONLY"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
