"""Phase 4 replay tests: synthetic fixtures only, never investment evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks import phase4_scoring_replay as replay

UTC = timezone.utc


def stamp(day, hour=12):
    return datetime(2026, 8, day, hour, tzinfo=UTC).isoformat()


def event(n=0, day=10, market="US", *, meta=None):
    s = stamp(day)
    return {
        "event_id": f"public-synthetic-{n}", "symbol": f"TEST{n%13}",
        "market": market, "signal_at": s, "decision_at": stamp(day, 13),
        "captured_at": stamp(day, 12), "source_asof": {"meta": s},
        "meta": meta if meta is not None else {"WinRate": 52, "NetProfit%": 20},
    }


def outcome(record, horizon=5, price=110, benchmark=103):
    at = replay.utc(record["decision_at"])
    return {
        "event_id": record["event_id"], "horizon_days": horizon,
        "entry_at": (at + timedelta(minutes=5)).isoformat(),
        "exit_at": (at + timedelta(days=horizon, hours=1)).isoformat(),
        "available_at": (at + timedelta(days=horizon, hours=2)).isoformat(),
        "entry_price": 100.0, "exit_price": float(price),
        "benchmark_entry": 100.0, "benchmark_exit": float(benchmark),
    }


def fake_engine(new=False):
    """Fake only for input plumbing; separately load real old/new scorer AST."""
    def technical(meta, sc):
        score = 6.0 if meta.get("WinRate", 0) > 50 else 4.0
        return {"total": score + (0.2 if new else 0)}
    def simple(*_args, **_kw):
        return {"total": 5.0}
    def coverage(*_args):
        return {"overall": 0.70 if not new else 0.80}
    def final(a, b, c, d, *, data_confidence=None, **_):
        return 60.0, round((a+b+c+d) * 2 * data_confidence, 2)
    return {
        "score_technical": technical,
        "score_fundamental": simple,
        "score_sentiment": simple,
        "score_market_internals": simple,
        "_data_confidence": coverage,
        "_compute_decision_scores": final,
    }


class TestPhase4InputSafety(unittest.TestCase):
    def test_requires_provenance_for_nonempty_sources(self):
        signal = event()
        del signal["source_asof"]["meta"]
        with self.assertRaisesRegex(ValueError, "source_asof"):
            replay.validate_signal(signal)

    def test_rejects_future_vendor_snapshot_and_capture(self):
        for field in ("source", "capture"):
            with self.subTest(field=field):
                s = event()
                if field == "source":
                    s["source_asof"]["meta"] = stamp(11)
                else:
                    s["captured_at"] = stamp(11)
                with self.assertRaises(ValueError):
                    replay.validate_signal(s)

    def test_rejects_naive_timestamps(self):
        s = event()
        s["signal_at"] = "2026-08-10T12:00:00"
        with self.assertRaisesRegex(ValueError, "naive"):
            replay.validate_signal(s)

    def test_extra_layers_require_provenance(self):
        s = event()
        s["l5"] = 6.0
        with self.assertRaisesRegex(ValueError, "point-in-time"):
            replay.validate_signal(s)
        s["source_asof"]["l5"] = stamp(10)
        replay.validate_signal(s)

    def test_no_future_outcome_and_benchmark_pair_required(self):
        s = event()
        o = outcome(s)
        o["available_at"] = stamp(19)
        with self.assertRaises(ValueError):
            replay.outcome_index([o], evaluation_asof=replay.utc(stamp(18)))
        o = outcome(s)
        del o["benchmark_exit"]
        with self.assertRaises(ValueError):
            replay.outcome_index([o], evaluation_asof=replay.utc(stamp(18)))

    def test_lookahead_entry_disallowed_and_immaturity_rejected(self):
        s = event()
        o = outcome(s)
        o["entry_at"] = s["decision_at"]
        with self.assertRaisesRegex(ValueError, "strictly AFTER"):
            replay.replay([s], fake_engine(), fake_engine(True),
                          mode="historical", asof=replay.utc(stamp(24)),
                          cutoff=replay.utc(stamp(18)), outcomes=[o], horizons=[5])
        o = outcome(s)
        o["exit_at"] = (replay.utc(s["decision_at"]) + timedelta(days=4)).isoformat()
        with self.assertRaisesRegex(ValueError, "not matured"):
            replay.replay([s], fake_engine(), fake_engine(True),
                          mode="historical", asof=replay.utc(stamp(24)),
                          cutoff=replay.utc(stamp(18)), outcomes=[o], horizons=[5])

    def test_invalid_price_not_accepted(self):
        for px in (-1.0, 0.0, float("inf"), True):
            with self.subTest(px=str(px)):
                s = event()
                o = outcome(s)
                o["entry_price"] = px
                with self.assertRaises(ValueError):
                    replay.outcome_index([o], evaluation_asof=replay.utc(stamp(18)))

    def test_empty_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.jsonl"
            path.write_text("\n")
            with self.assertRaisesRegex(ValueError, "zero records"):
                replay.load_jsonl(path)

    def test_unvalidated_market_not_automatically_inferred(self):
        s = event(market="ASX")
        with self.assertRaisesRegex(ValueError, "US/AU/IN"):
            replay.validate_signal(s)


class TestPhase4Evaluation(unittest.TestCase):
    def test_shadow_never_reads_or_needs_outcomes(self):
        rows = [event(0), event(1)]
        report, scored = replay.replay(
            rows, fake_engine(), fake_engine(True),
            mode="shadow", asof=replay.utc(stamp(20)))
        self.assertEqual(report["mode"], "shadow")
        self.assertTrue(report["no_market_actions"])
        self.assertEqual(report["scored"], 2)
        self.assertNotIn("return_pct", scored[0])

    def test_historical_cutoff_embargo(self):
        signals = [event(0, day=10), event(1, day=15),
                   event(2, day=20), event(3, day=22)]
        outcomes = [outcome(x) for x in signals]
        report, rows = replay.replay(
            signals, fake_engine(), fake_engine(True),
            mode="historical", asof=replay.utc(stamp(31)),
            cutoff=replay.utc(stamp(18)), horizons=[5], outcomes=outcomes)
        self.assertEqual(len(rows), 3)
        self.assertEqual(report["exclusions"]["embargo"], 1)
        self.assertEqual([r["split"] for r in rows], ["train", "test", "test"])
        self.assertEqual(report["groups"]["US/5d/test"]["status"],
                         "INSUFFICIENT_DATA")
        self.assertIsNone(report["groups"]["US/5d/test"]["performance"])

    def test_cost_applied_to_underlying_only(self):
        s = event()
        r, rows = replay.replay(
            [s], fake_engine(), fake_engine(True),
            mode="historical", asof=replay.utc(stamp(18)),
            cutoff=replay.utc(stamp(16)), horizons=[5],
            outcomes=[outcome(s)], cost_bps=25)
        self.assertEqual(rows[0]["return_pct"], 9.75)
        self.assertEqual(rows[0]["excess"], 6.75)
        self.assertTrue(r["explicitly_not_options_backtest"])

    def test_duplicate_signal_and_outcome_refused(self):
        s = event()
        with self.assertRaisesRegex(ValueError, "duplicate signal"):
            replay.replay([s, dict(s)], fake_engine(), fake_engine(True),
                          mode="shadow", asof=replay.utc(stamp(18)))
        with self.assertRaisesRegex(ValueError, "duplicate outcome"):
            replay.outcome_index([outcome(s), outcome(s)],
                                 evaluation_asof=replay.utc(stamp(18)))

    def test_score_ranking_uses_same_matured_universe(self):
        rows = []
        for n in range(40):
            score = 40 + n
            rows.append({
                "event_id": str(n), "symbol": f"TEST{n%20}",
                "old": {"decision": score, "confidence": 0.75},
                "new": {"decision": 80-score+40, "confidence": 0.90},
                "excess": (n - 20) / 10, "return_pct": (n-20)/10,
            })
        result = replay.group_result(rows)
        self.assertEqual(result["status"], "EVALUABLE")
        self.assertLess(result["performance"]["new_minus_old_top_excess_pct"], 0)
        self.assertEqual(len(replay.top_selection(rows, "new")), 10)

    def test_rank_correlation_hand_checks(self):
        self.assertEqual(replay.rank_corr([1, 2, 3], [4, 5, 6]), 1.0)
        self.assertEqual(replay.rank_corr([1, 2, 3], [6, 5, 4]), -1.0)
        self.assertIsNone(replay.rank_corr([1, 1, 1], [3, 2, 1]))

    def test_report_can_be_written_without_market_actions(self):
        data = [event()]
        report, rows = replay.replay(
            data, fake_engine(), fake_engine(True), mode="shadow",
            asof=replay.utc(stamp(20)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            replay.write_results(path, report, rows)
            self.assertTrue((path/"phase4_summary.json").is_file())
            self.assertTrue((path/"phase4_scores.jsonl").is_file())
            self.assertNotIn("return_pct",
                             (path/"phase4_scores.jsonl").read_text())


class TestActualProductionReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidate = replay.engine_from_source(
            ROOT / "tradingview" / "signal_stack.py")
        try:
            cls.baseline = subprocess.check_output([
                "git", "show", replay.BASE_COMMIT+":tradingview/signal_stack.py"],
                cwd=ROOT, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Pinned old-model Git commit unavailable; "
                               "fetch full history for CI; do not skip") from exc

    def test_true_old_and_new_frozen_inputs_with_shadow(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory)/"baseline.py"
            baseline.write_bytes(self.baseline)
            old_engine = replay.engine_from_source(baseline)
            records = [event(0, meta={"WinRate": 30, "NetProfit%": 4}),
                       event(1, meta={"WinRate": 75, "NetProfit%": 38})]
            report, rows = replay.replay(
                records, old_engine, self.candidate, mode="shadow",
                asof=replay.utc(stamp(20)))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(0 <= r["new"]["decision"] <= 100 for r in rows))
            self.assertTrue(all(0 <= r["old"]["decision"] <= 100 for r in rows))
            self.assertEqual(report["no_market_actions"], True)

    def test_actual_old_new_cli_historical_synthetic_is_underpowered(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            baseline = directory/"baseline.py"
            baseline.write_bytes(self.baseline)
            signals = [event(n, day=10 if n % 2 == 0 else 22,
                             market=("US", "AU", "IN")[n % 3])
                       for n in range(24)]
            snapshots = directory/"snapshots.jsonl"
            snapshots.write_text("".join(json.dumps(x)+"\n" for x in signals))
            outcomes = directory/"outcomes.jsonl"
            outcomes.write_text("".join(json.dumps(outcome(x))+"\n" for x in signals))
            rc = replay.main([
                "--baseline-source", str(baseline),
                "--candidate-source", str(ROOT/"tradingview"/"signal_stack.py"),
                "--snapshots", str(snapshots), "--outcomes", str(outcomes),
                "--mode", "historical", "--cutoff", stamp(18),
                "--as-of", stamp(31), "--out", str(directory/"report"),
                "--cost-bps", "15",
            ])
            self.assertEqual(rc, 0)
            summary = json.loads((directory/"report"/"phase4_summary.json").read_text())
            self.assertEqual(summary["source_rows"], 24)
            self.assertEqual(summary["measured_rows"], 24)
            self.assertTrue(all(g["performance"] is None for g in summary["groups"].values()))
            self.assertEqual(summary["cost_bps"], 15)


if __name__ == "__main__":
    unittest.main()
