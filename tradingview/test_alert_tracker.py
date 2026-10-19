"""
Tests for alert_tracker.py
Run: pytest test_alert_tracker.py -v
All tests use in-memory SQLite — no persistent state, no network calls.
"""
import sys
import os
import tempfile
import threading
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from alert_tracker import (
    AlertTracker,
    DB_PATH,
    EXPIRY_DAYS,
    FAILURE_DRAWDOWN_PCT,
    SNAPSHOT_INTERVALS_H,
    SUCCESS_RETURN_PCT,
    _make_uuid,
    _normalise_tv_symbol,
    _now_iso,
    _parse_signal,
    _resolve_yf_ticker,
    _safe_float,
)


def _tracker() -> AlertTracker:
    """Fresh file-backed tracker (temp file) for every test."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_at_")
    os.close(fd)
    os.unlink(path)  # let AlertTracker create it fresh
    return AlertTracker(path)


def _make_fire(symbol="NASDAQ:NVDA", source="webhook",
               fire_time=None, message="BUY:1, rsi:65") -> dict:
    now = fire_time or _now_iso()
    return {
        "pro_symbol": symbol,
        "symbol":     symbol,
        "_source":    source,
        "fire_time":  now,
        "bar_time":   now,
        "message":    message,
        "resolution": "D",
        "close":      None,
    }


def _make_result(composite=72.5, actionable=True) -> dict:
    return {
        "composite":    composite,
        "actionable_at": _now_iso() if actionable else None,
        "yf_ticker":   "NVDA",
        "scores": {
            "l1": {"total": 80.0},
            "l2": {"total": 65.0},
            "l3": {"total": 70.0},
            "l4": {"total": 75.0},
        },
        "fundamentals": {"current_price": 850.0},
        "l5_enrichment": {"l5_score": 60.0},
        "l6_enrichment": {"l6_score": 55.0},
        "l7_enrichment": {"l7_score": 50.0},
    }


def test_wrapped_tv_symbol_is_normalised():
    wrapped = '={"adjustment":"splits","currency-id":"INR","session":"regular","symbol":"NSE:WAAREEENER"}'
    assert _normalise_tv_symbol(wrapped) == "NSE:WAAREEENER"


def test_wrapped_stored_yf_ticker_is_ignored_for_resolution():
    wrapped_fragment = '"adjustment":"splits","currency-id":"INR","session":"regular","symbol":"NSE:WAAREEENER"}'
    assert _resolve_yf_ticker("NSE:WAAREEENER", wrapped_fragment) is None


def test_record_alert_persists_plain_tv_symbol_from_wrapped_payload():
    tracker = _tracker()
    wrapped = '={"adjustment":"splits","currency-id":"INR","session":"regular","symbol":"NSE:WAAREEENER"}'
    result = _make_result()
    result["yf_ticker"] = '"adjustment":"splits","symbol":"NSE:WAAREEENER"}'

    alert_uuid = tracker.record_alert(_make_fire(symbol=wrapped), result)

    with tracker._conn() as conn:
        row = conn.execute(
            "SELECT tv_symbol, yf_ticker FROM alerts WHERE alert_uuid=?",
            (alert_uuid,),
        ).fetchone()

    assert row["tv_symbol"] == "NSE:WAAREEENER"
    assert row["yf_ticker"] == ""


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────
class TestMakeUuid(unittest.TestCase):

    def test_deterministic(self):
        u1 = _make_uuid("webhook", "NASDAQ:NVDA", "2026-04-09T10:00:00+00:00")
        u2 = _make_uuid("webhook", "NASDAQ:NVDA", "2026-04-09T10:00:00+00:00")
        self.assertEqual(u1, u2)

    def test_different_source_different_uuid(self):
        u1 = _make_uuid("webhook",  "NASDAQ:NVDA", "2026-04-09T10:00:00")
        u2 = _make_uuid("screener", "NASDAQ:NVDA", "2026-04-09T10:00:00")
        self.assertNotEqual(u1, u2)

    def test_different_symbol_different_uuid(self):
        u1 = _make_uuid("webhook", "NASDAQ:NVDA", "2026-04-09T10:00:00")
        u2 = _make_uuid("webhook", "NSE:INFY",    "2026-04-09T10:00:00")
        self.assertNotEqual(u1, u2)

    def test_different_fire_time_different_uuid(self):
        u1 = _make_uuid("webhook", "NASDAQ:NVDA", "2026-04-09T10:00:00")
        u2 = _make_uuid("webhook", "NASDAQ:NVDA", "2026-04-10T10:00:00")
        self.assertNotEqual(u1, u2)

    def test_returns_16_hex_chars(self):
        u = _make_uuid("webhook", "NYSE:ORCL", "2026-04-09T09:00:00")
        self.assertEqual(len(u), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in u))


class TestSafeFloat(unittest.TestCase):

    def test_valid_float(self):
        self.assertAlmostEqual(_safe_float(3.14), 3.14)

    def test_string_float(self):
        self.assertAlmostEqual(_safe_float("72.5"), 72.5)

    def test_none_returns_none(self):
        self.assertIsNone(_safe_float(None))

    def test_nan_returns_none(self):
        import math
        self.assertIsNone(_safe_float(math.nan))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_safe_float(""))

    def test_non_numeric_string_returns_none(self):
        self.assertIsNone(_safe_float("abc"))


class TestParseSignal(unittest.TestCase):

    def test_buy(self):
        self.assertEqual(_parse_signal("BUY:1, rsi:65, ADX:30"), "BUY")

    def test_sell(self):
        self.assertEqual(_parse_signal("SELL SIGNAL triggered"), "SELL")

    def test_screener(self):
        self.assertEqual(_parse_signal("SCREENER BUY:1, rsi:55"), "SCREENER")

    def test_manual(self):
        self.assertEqual(_parse_signal("MANUAL BUY:1"), "MANUAL")

    def test_unknown(self):
        self.assertEqual(_parse_signal("something unrecognised"), "UNKNOWN")

    def test_case_insensitive(self):
        self.assertEqual(_parse_signal("buy:1 close breakout"), "BUY")


class TestConstants(unittest.TestCase):

    def test_success_threshold_positive(self):
        self.assertGreater(SUCCESS_RETURN_PCT, 0)

    def test_failure_threshold_negative(self):
        self.assertLess(FAILURE_DRAWDOWN_PCT, 0)

    def test_expiry_days_positive(self):
        self.assertGreater(EXPIRY_DAYS, 0)

    def test_snapshot_intervals_sorted(self):
        self.assertEqual(SNAPSHOT_INTERVALS_H, sorted(SNAPSHOT_INTERVALS_H))

    def test_snapshot_includes_1h_and_7d(self):
        self.assertIn(1,   SNAPSHOT_INTERVALS_H)
        self.assertIn(168, SNAPSHOT_INTERVALS_H)

    def test_db_path_in_database_dir(self):
        """DB was moved from tradingview/ to database/ during folder consolidation (2026-04-24)."""
        db_str = str(DB_PATH).replace("\\", "/")
        self.assertIn("database", db_str)
        self.assertIn("alert_tracker.db", db_str)
        # Ensure we are NOT pointing into tradingview/ anymore
        self.assertNotIn("tradingview/alert_tracker.db", db_str)

    def test_db_path_is_absolute(self):
        """DB_PATH must be absolute so scripts work regardless of cwd."""
        self.assertTrue(DB_PATH.is_absolute(),
                        f"DB_PATH must be absolute, got: {DB_PATH}")

    def test_db_path_parent_is_database_folder(self):
        """DB_PATH parent directory name must be 'database' (not 'tradingview' or 'reports')."""
        self.assertEqual(DB_PATH.parent.name, "database",
                         f"Expected parent 'database', got '{DB_PATH.parent.name}'")

    def test_db_path_filename(self):
        """DB filename must remain alert_tracker.db."""
        self.assertEqual(DB_PATH.name, "alert_tracker.db")


# ─────────────────────────────────────────────────────────────────────────────
# Schema / init
# ─────────────────────────────────────────────────────────────────────────────
class TestSchemaInit(unittest.TestCase):

    def test_tables_created(self):
        t = _tracker()
        with t._conn() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        self.assertIn("alerts", tables)
        self.assertIn("price_snapshots", tables)
        self.assertIn("performance_metrics", tables)

    def test_indexes_created(self):
        t = _tracker()
        with t._conn() as conn:
            indexes = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
        self.assertIn("idx_alerts_symbol", indexes)
        self.assertIn("idx_alerts_source", indexes)
        self.assertIn("idx_snaps_uuid",    indexes)
        self.assertIn("idx_perf_status",   indexes)

    def test_alerts_column_count(self):
        t = _tracker()
        with t._conn() as conn:
            cols = conn.execute("PRAGMA table_info(alerts)").fetchall()
        self.assertGreaterEqual(len(cols), 20)

    def test_multiple_init_idempotent(self):
        t = _tracker()
        t._init_db()  # second call should not raise
        t._init_db()


# ─────────────────────────────────────────────────────────────────────────────
# record_alert
# ─────────────────────────────────────────────────────────────────────────────
class TestRecordAlert(unittest.TestCase):

    def test_outcome_failure_after_alert_insert_rolls_back_everything(self):
        import tradingview.outcome_store as outcome_store
        tracker = _tracker()
        fire = _make_fire(fire_time=_now_iso())
        fire_time = fire["fire_time"]
        result = _make_result()
        result["outcome_measurement"] = {
            "origin": "fire",
            "entry_price": 850.0,
            "entry_price_source": "fixture",
            "entry_price_at": fire_time,
            "entry_available_at": fire_time,
            "horizon_hours": 24,
        }
        with mock.patch.object(
            outcome_store,
            "record_resolution_mapping",
            side_effect=RuntimeError("injected provenance failure"),
        ), self.assertRaises(RuntimeError):
            tracker.record_alert(fire, result)

        with tracker._conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outcome_entries").fetchone()[0], 0)

    def test_returns_uuid_on_success(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        self.assertIsNotNone(uuid)
        self.assertIsInstance(uuid, str)
        self.assertEqual(len(uuid), 16)

    def test_row_stored_in_db(self):
        t = _tracker()
        t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_duplicate_returns_none(self):
        t = _tracker()
        fire = _make_fire()
        result = _make_result()
        first  = t.record_alert(fire, result)
        second = t.record_alert(fire, result)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_duplicate_does_not_add_row(self):
        t = _tracker()
        fire = _make_fire()
        t.record_alert(fire, _make_result())
        t.record_alert(fire, _make_result())
        with t._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_different_symbols_both_stored(self):
        t = _tracker()
        t.record_alert(_make_fire("NASDAQ:NVDA"), _make_result())
        t.record_alert(_make_fire("NSE:INFY"),    _make_result())
        with t._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        self.assertEqual(count, 2)

    def test_composite_score_stored(self):
        t = _tracker()
        t.record_alert(_make_fire(), _make_result(composite=88.3))
        with t._conn() as conn:
            row = conn.execute("SELECT composite_score FROM alerts").fetchone()
        self.assertAlmostEqual(row[0], 88.3, places=1)

    def test_source_tag_stored(self):
        t = _tracker()
        fire = _make_fire(source="screener")
        t.record_alert(fire, _make_result())
        with t._conn() as conn:
            row = conn.execute("SELECT source FROM alerts").fetchone()
        self.assertEqual(row[0], "screener")

    def test_layer_scores_stored(self):
        t = _tracker()
        t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            row = conn.execute("SELECT l1_score, l2_score, l3_score, l4_score FROM alerts").fetchone()
        self.assertAlmostEqual(row[0], 80.0, places=1)
        self.assertAlmostEqual(row[1], 65.0, places=1)

    def test_actionable_flag_when_above_threshold(self):
        t = _tracker()
        t.record_alert(_make_fire(), _make_result(actionable=True))
        with t._conn() as conn:
            row = conn.execute("SELECT actionable FROM alerts").fetchone()
        self.assertEqual(row[0], 1)

    def test_actionable_flag_when_below_threshold(self):
        t = _tracker()
        t.record_alert(_make_fire(), _make_result(actionable=False))
        with t._conn() as conn:
            row = conn.execute("SELECT actionable FROM alerts").fetchone()
        self.assertEqual(row[0], 0)

    def test_entry_price_stored(self):
        t = _tracker()
        fire = _make_fire()
        result = _make_result()
        result["fundamentals"]["current_price"] = 923.45
        t.record_alert(fire, result)
        with t._conn() as conn:
            row = conn.execute("SELECT entry_price FROM alerts").fetchone()
        self.assertAlmostEqual(row[0], 923.45, places=2)

    def test_entry_price_falls_back_to_close(self):
        t = _tracker()
        fire = _make_fire()
        fire["close"] = 155.0
        result = _make_result()
        result["fundamentals"] = {}  # no current_price
        t.record_alert(fire, result)
        with t._conn() as conn:
            row = conn.execute("SELECT entry_price FROM alerts").fetchone()
        self.assertAlmostEqual(row[0], 155.0, places=2)

    def test_expires_at_set_correctly(self):
        t = _tracker()
        before = datetime.now(timezone.utc)
        t.record_alert(_make_fire(), _make_result())
        after = datetime.now(timezone.utc)
        with t._conn() as conn:
            row = conn.execute("SELECT expires_at FROM alerts").fetchone()
        exp = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        self.assertGreater(exp, before + timedelta(days=EXPIRY_DAYS - 1))
        self.assertLess(exp,    after +  timedelta(days=EXPIRY_DAYS + 1))

    def test_broken_fire_does_not_raise(self):
        t = _tracker()
        # Empty dicts should fail gracefully, not raise
        try:
            uuid = t.record_alert({}, {})
            # May return None for empty pro_symbol — that's acceptable
        except Exception as exc:
            self.fail(f"record_alert raised unexpectedly: {exc}")

    def test_thread_safety(self):
        t = _tracker()
        results = []
        errors = []

        def _insert(i):
            fire = _make_fire(
                symbol=f"NASDAQ:SYM{i}",
                fire_time=f"2026-04-09T{10+i:02d}:00:00+00:00",
            )
            try:
                uuid = t.record_alert(fire, _make_result())
                results.append(uuid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_insert, args=(i,)) for i in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [])
        with t._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        self.assertEqual(count, 10)


# ─────────────────────────────────────────────────────────────────────────────
# compute_metrics
# ─────────────────────────────────────────────────────────────────────────────
class TestComputeMetrics(unittest.TestCase):

    def _insert_snapshot(self, conn, uuid, elapsed_h, return_pct):
        conn.execute(
            """
            INSERT INTO price_snapshots (alert_uuid, snapshot_time, price, elapsed_hours, return_pct)
            VALUES (?, ?, ?, ?, ?)
            """,
            (uuid, _now_iso(), 100.0 * (1 + return_pct / 100), elapsed_h, return_pct),
        )

    def test_returns_count_of_written_rows(self):
        t = _tracker()
        fire = _make_fire()
        uuid = t.record_alert(fire, _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 1.0, 2.5)
        written = t.compute_metrics()
        self.assertEqual(written, 1)

    def test_hit_target_set_when_return_exceeds_threshold(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 24.0, SUCCESS_RETURN_PCT + 1.0)
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute("SELECT hit_target, status FROM performance_metrics").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "success")

    def test_hit_stop_set_when_drawdown_exceeds_threshold(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 4.0, FAILURE_DRAWDOWN_PCT - 1.0)
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute("SELECT hit_stop, status FROM performance_metrics").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "failure")

    def test_open_status_within_window(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 2.0, 1.5)  # small return, within window
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute("SELECT status FROM performance_metrics").fetchone()
        self.assertEqual(row[0], "open")

    def test_return_1h_stored(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 1.0, 3.2)
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute("SELECT return_1h FROM performance_metrics").fetchone()
        self.assertAlmostEqual(row[0], 3.2, places=4)

    def test_late_snapshot_does_not_fill_short_horizons(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 48.0, 4.2)
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute(
                "SELECT return_1h, return_4h, return_1d FROM performance_metrics"
            ).fetchone()
        self.assertEqual(tuple(row), (None, None, None))

    def test_ten_day_snapshot_does_not_fill_seven_day_return(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 240.0, 6.4)
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute("SELECT return_7d FROM performance_metrics").fetchone()
        self.assertIsNone(row[0])

    def test_max_return_is_max_across_all_snapshots(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 1.0,  2.0)
            self._insert_snapshot(conn, uuid, 4.0,  6.5)   # max
            self._insert_snapshot(conn, uuid, 24.0, -1.0)
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute("SELECT max_return FROM performance_metrics").fetchone()
        self.assertAlmostEqual(row[0], 6.5, places=4)

    def test_max_drawdown_is_min_across_all_snapshots(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 1.0,   2.0)
            self._insert_snapshot(conn, uuid, 4.0,  -3.5)  # min
            self._insert_snapshot(conn, uuid, 24.0,  1.0)
        t.compute_metrics()
        with t._conn() as conn:
            row = conn.execute("SELECT max_drawdown FROM performance_metrics").fetchone()
        self.assertAlmostEqual(row[0], -3.5, places=4)

    def test_no_snapshots_writes_nothing(self):
        t = _tracker()
        t.record_alert(_make_fire(), _make_result())
        written = t.compute_metrics()
        self.assertEqual(written, 0)

    def test_insert_or_replace_on_recompute(self):
        t = _tracker()
        uuid = t.record_alert(_make_fire(), _make_result())
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 1.0, 2.0)
        t.compute_metrics()
        with t._conn() as conn:
            self._insert_snapshot(conn, uuid, 4.0, 7.0)
        t.compute_metrics()
        with t._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM performance_metrics").fetchone()[0]
        self.assertEqual(count, 1)


class TestSignalOutputProvenance(unittest.TestCase):

    def test_model_and_score_provenance_are_persisted(self):
        tracker = _tracker()
        result = _make_result()
        result.update({
            "tv_symbol": "NASDAQ:NVDA",
            "fire_time": "2026-07-10T20:00:00Z",
            "resolution": "60",
            "analysis_timestamp": "2026-07-13T00:00:00+00:00",
            "score_model_version": "signal-stack-test",
            "quality_score": 80.0,
            "freshness_decay": 0.9,
            "age_hours": 8.0,
            "calendar_age_hours": 52.0,
            "regime_weights": {"l1": 0.3, "l2": 0.3, "l3": 0.2, "l4": 0.2},
            "data_confidence": {"overall": 0.75},
            "actionability_reasons": [],
            "input_source": "download",
            "meta": {"WinRate": 55.0},
            "market_internals": {"vix_level": 18.0},
        })

        row_id = tracker.record_signal_output(result, run_id="run-one")

        with tracker._conn() as conn:
            row = conn.execute(
                "SELECT run_id, timestamp, win_rate, vix_at_signal, parameters_json "
                "FROM signal_engine_outputs WHERE id=?",
                (row_id,),
            ).fetchone()
        params = json.loads(row["parameters_json"])
        self.assertEqual(row["run_id"], "run-one")
        self.assertEqual(row["timestamp"], result["analysis_timestamp"])
        self.assertEqual(row["win_rate"], 55.0)
        self.assertEqual(row["vix_at_signal"], 18.0)
        self.assertEqual(params["model_version"], "signal-stack-test")
        self.assertEqual(params["quality_score"], 80.0)

    def test_same_signal_can_be_recorded_by_multiple_runs(self):
        tracker = _tracker()
        result = {**_make_result(), "tv_symbol": "NASDAQ:NVDA"}

        first = tracker.record_signal_output(result, run_id="run-one")
        second = tracker.record_signal_output(result, run_id="run-two")

        self.assertNotEqual(first, second)
        with tracker._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM signal_engine_outputs").fetchone()[0]
        self.assertEqual(count, 2)


# ─────────────────────────────────────────────────────────────────────────────
# get_stats
# ─────────────────────────────────────────────────────────────────────────────
class TestGetStats(unittest.TestCase):

    def test_legacy_top_level_keys_remain_unchanged(self):
        stats = _tracker().get_stats()
        self.assertTrue({
            "total_alerts", "by_source", "performance", "source_perf",
            "layer_averages",
        }.issubset(stats))

    def test_empty_db_returns_zero_totals(self):
        t = _tracker()
        stats = t.get_stats()
        self.assertEqual(stats["total_alerts"], 0)
        self.assertEqual(stats["by_source"], {})

    def test_total_alerts_count(self):
        t = _tracker()
        for i in range(3):
            t.record_alert(
                _make_fire(symbol=f"NASDAQ:SYM{i}",
                           fire_time=f"2026-04-09T{10+i:02d}:00:00+00:00"),
                _make_result(),
            )
        stats = t.get_stats()
        self.assertEqual(stats["total_alerts"], 3)

    def test_by_source_breakdown(self):
        t = _tracker()
        t.record_alert(
            _make_fire("NASDAQ:A", source="webhook",
                       fire_time="2026-04-09T10:00:00+00:00"),
            _make_result(),
        )
        t.record_alert(
            _make_fire("NASDAQ:B", source="screener",
                       fire_time="2026-04-09T11:00:00+00:00"),
            _make_result(),
        )
        stats = t.get_stats()
        self.assertEqual(stats["by_source"].get("webhook"),  1)
        self.assertEqual(stats["by_source"].get("screener"), 1)

    def test_layer_averages_returned(self):
        t = _tracker()
        t.record_alert(_make_fire(), _make_result())
        stats = t.get_stats()
        self.assertIn("layer_averages", stats)
        la = stats["layer_averages"]
        for key in ("l1", "l2", "l3", "l4", "composite"):
            self.assertIn(key, la)

    def test_stats_returns_dict(self):
        t = _tracker()
        self.assertIsInstance(t.get_stats(), dict)


# ─────────────────────────────────────────────────────────────────────────────
# build_performance_report
# ─────────────────────────────────────────────────────────────────────────────
class TestBuildPerformanceReport(unittest.TestCase):

    def test_returns_html_string(self):
        t = _tracker()
        html = t.build_performance_report()
        self.assertIsInstance(html, str)
        self.assertIn("<html", html.lower())

    def test_html_contains_doctype_or_head(self):
        t = _tracker()
        html = t.build_performance_report()
        self.assertTrue(
            "<!doctype" in html.lower() or "<head" in html.lower(),
            "HTML report must contain <!DOCTYPE> or <head>",
        )

    def test_html_is_self_contained(self):
        """Should not link to external stylesheets — entirely inline."""
        t = _tracker()
        html = t.build_performance_report()
        self.assertNotIn('href="http', html)

    def test_report_with_data_shows_nonzero_count(self):
        t = _tracker()
        t.record_alert(_make_fire("NASDAQ:NVDA"), _make_result(composite=72.5))
        html = t.build_performance_report()
        # The KPI block should show total > 0 (i.e. not "0" for Total Alerts Tracked)
        self.assertIn("72.5", html,
                      "Avg composite score should appear in the performance report")

    def test_empty_db_report_does_not_raise(self):
        t = _tracker()
        try:
            html = t.build_performance_report()
            self.assertIsInstance(html, str)
        except Exception as exc:
            self.fail(f"build_performance_report raised on empty DB: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# get_tracker singleton
# ─────────────────────────────────────────────────────────────────────────────
class TestGetTrackerSingleton(unittest.TestCase):

    def test_singleton_returns_same_instance(self):
        import alert_tracker as at
        from alert_tracker import get_tracker
        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch.object(at, "_tracker_instance", None):
            db_path = str(Path(temp_dir) / "tracker.db")
            t1 = get_tracker(db_path)
            t2 = get_tracker(db_path)
        self.assertIs(t1, t2)

    def test_singleton_is_alerttracker_instance(self):
        import alert_tracker as at
        from alert_tracker import get_tracker
        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch.object(at, "_tracker_instance", None):
            t = get_tracker(str(Path(temp_dir) / "tracker.db"))
        self.assertIsInstance(t, AlertTracker)


# ─────────────────────────────────────────────────────────────────────────────
# Fetch batching — one network call per unique ticker, not per row
# ─────────────────────────────────────────────────────────────────────────────
class TestFetchBatching(unittest.TestCase):

    def test_structured_adapter_keeps_provider_bar_timestamp(self):
        import alert_tracker as at
        import pandas as pd

        class FakeTicker:
            def history(self, **kwargs):
                return pd.DataFrame(
                    {"Close": [123.45]},
                    index=pd.DatetimeIndex([datetime.now(timezone.utc) - timedelta(hours=2)]),
                )

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()):
            observation = at._fetch_observation("AAPL")

        self.assertTrue(observation["trusted"])
        self.assertEqual(observation["price"], 123.45)
        self.assertIsNotNone(observation["observed_at"])
        self.assertEqual(observation["price_source"], "yfinance.history.last_completed_bar")
        self.assertAlmostEqual(
            (datetime.now(timezone.utc) - observation["observed_at"]).total_seconds(),
            3600,
            delta=5,
        )

    def test_still_forming_bar_is_untrusted(self):
        import alert_tracker as at
        import pandas as pd

        class FakeTicker:
            def history(self, **kwargs):
                return pd.DataFrame(
                    {"Close": [123.45]},
                    index=pd.DatetimeIndex([datetime.now(timezone.utc) - timedelta(minutes=10)]),
                )

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()), \
             mock.patch.object(at, "_fetch_price", return_value=123.45):
            observation = at._fetch_observation("AAPL")

        self.assertFalse(observation["trusted"])
        self.assertIsNone(observation["observed_at"])
        self.assertEqual(observation["reason"], "provider bar is still forming")

    def test_stale_bar_uses_interval_limit(self):
        import alert_tracker as at
        import pandas as pd

        class FakeTicker:
            def history(self, **kwargs):
                return pd.DataFrame(
                    {"Close": [123.45]},
                    index=pd.DatetimeIndex([datetime.now(timezone.utc) - timedelta(hours=10)]),
                )

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()), \
             mock.patch.object(at, "_fetch_price", return_value=123.45):
            observation = at._fetch_observation("AAPL")

        self.assertFalse(observation["trusted"])
        self.assertEqual(observation["reason"], "provider bar timestamp is stale")

    def test_weekend_closure_allows_latest_completed_bar(self):
        import alert_tracker as at
        import pandas as pd

        available_at = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
        class FakeTicker:
            def history(self, **kwargs):
                return pd.DataFrame(
                    {"Close": [123.45]},
                    index=pd.DatetimeIndex([datetime(2026, 10, 2, 10, tzinfo=timezone.utc)]),
                )

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at, "_utc_now", return_value=available_at), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()):
            observation = at._fetch_observation("AAPL")

        self.assertTrue(observation["trusted"])
        self.assertEqual(observation["observed_at"], datetime(2026, 10, 2, 11, tzinfo=timezone.utc))

    def test_provider_exception_reason_is_preserved(self):
        import alert_tracker as at

        class FakeTicker:
            def history(self, **kwargs):
                raise RuntimeError("fixture provider unavailable")

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()), \
             mock.patch.object(at, "_fetch_price", return_value=123.45):
            observation = at._fetch_observation("AAPL")

        self.assertEqual(observation["reason"], "provider error: RuntimeError: fixture provider unavailable")

    def test_invalid_close_reasons_are_untrusted(self):
        import alert_tracker as at
        import pandas as pd

        for close, reason in [(float("nan"), "non-finite"), (0, "non-positive")]:
            class FakeTicker:
                def history(self, **kwargs):
                    return pd.DataFrame(
                        {"Close": [close]},
                        index=pd.DatetimeIndex([datetime.now(timezone.utc) - timedelta(hours=2)]),
                    )

            with mock.patch.object(at, "_YF_OK", True), \
                 mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()), \
                 mock.patch.object(at, "_fetch_price", return_value=123.45):
                observation = at._fetch_observation("AAPL")
            self.assertFalse(observation["trusted"])
            self.assertIn(reason, observation["reason"])

    def test_out_of_order_history_uses_latest_bar_and_duplicate_tickers_fetch_once(self):
        import alert_tracker as at
        import pandas as pd
        calls = []

        class FakeTicker:
            def history(self, **kwargs):
                calls.append("history")
                return pd.DataFrame(
                    {"Close": [100.0, 123.45]},
                    index=pd.DatetimeIndex([
                        datetime.now(timezone.utc) - timedelta(hours=2),
                        datetime.now(timezone.utc) - timedelta(hours=3),
                    ]),
                )

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()):
            observations = at._fetch_observations(["AAPL", "AAPL"])

        self.assertEqual(calls, ["history"])
        self.assertEqual(observations["AAPL"]["price"], 100.0)

    def test_untrusted_fallback_keeps_price_without_market_timestamp(self):
        import alert_tracker as at

        class FakeTicker:
            def history(self, **kwargs):
                return []

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()), \
             mock.patch.object(at, "_fetch_price", return_value=123.45):
            observation = at._fetch_observation("AAPL")

        self.assertFalse(observation["trusted"])
        self.assertEqual(observation["price"], 123.45)
        self.assertIsNone(observation["observed_at"])

    def test_naive_provider_timestamp_is_untrusted(self):
        import alert_tracker as at
        import pandas as pd

        class FakeTicker:
            def history(self, **kwargs):
                return pd.DataFrame(
                    {"Close": [123.45]},
                    index=pd.DatetimeIndex([datetime.now() - timedelta(minutes=5)]),
                )

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at.yf, "Ticker", return_value=FakeTicker()), \
             mock.patch.object(at, "_fetch_price", return_value=123.45):
            observation = at._fetch_observation("AAPL")

        self.assertFalse(observation["trusted"])
        self.assertIn("naive", observation["reason"])

    def test_fetch_prices_calls_once_per_unique_ticker(self):
        import alert_tracker as at
        calls = []

        def _fake(ticker):
            calls.append(ticker)
            return 100.0

        with mock.patch.object(at, "_fetch_price", side_effect=_fake):
            prices = at._fetch_prices(["AAPL", "MSFT", "NVDA"])

        self.assertEqual(sorted(calls), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(prices, {"AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0})

    def test_fetch_prices_empty_input_makes_no_calls(self):
        import alert_tracker as at
        with mock.patch.object(at, "_fetch_price", side_effect=AssertionError("must not fetch")):
            self.assertEqual(at._fetch_prices([]), {})

    def test_update_prices_fetches_once_for_alerts_sharing_a_symbol(self):
        """Three alerts on one symbol must cost one network call, not three."""
        import alert_tracker as at
        t = _tracker()
        for i in range(3):
            fire = _make_fire(fire_time=(
                datetime.now(timezone.utc) - timedelta(hours=i + 1)
            ).isoformat())
            fire["message"] = f"BUY:1, rsi:{60 + i}"
            self.assertIsNotNone(t.record_alert(fire, _make_result()))

        calls = []

        def _fake(ticker):
            calls.append(ticker)
            return {
                "price": 900.0,
                "observed_at": datetime.now(timezone.utc),
                "available_at": datetime.now(timezone.utc),
                "price_source": "fixture",
                "trusted": True,
            }

        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at, "_fetch_observation", side_effect=_fake):
            result = t.update_prices()

        self.assertEqual(len(calls), 1, f"expected 1 fetch for 3 shared-symbol alerts, got {calls}")
        self.assertEqual(result["updated"], 3)
        with t._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
        self.assertEqual(n, 3, "every alert still gets its own snapshot row")

    def test_update_prices_counts_unfetchable_symbol_as_failed(self):
        import alert_tracker as at
        t = _tracker()
        self.assertIsNotNone(t.record_alert(_make_fire(), _make_result()))

        with mock.patch.object(at, "_YF_OK", True), \
               mock.patch.object(at, "_fetch_observation", return_value=None):
            result = t.update_prices()

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failed"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# Bulk MFE/MAE lookup replaces the per-row query
# ─────────────────────────────────────────────────────────────────────────────
class TestLatestEvolutionByOutput(unittest.TestCase):

    def _insert_evo(self, conn, output_id, snapshot_at, mfe, mae):
        conn.execute(
            """
            INSERT INTO price_evolution
                (output_id, snapshot_at, hours_elapsed, price, return_pct,
                 max_favorable, max_adverse, outcome)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (output_id, snapshot_at, 1.0, 100.0, 0.0, mfe, mae, "open"),
        )

    def test_returns_most_recent_row_per_output(self):
        t = _tracker()
        with t._conn() as conn:
            self._insert_evo(conn, 1, "2026-01-01T00:00:00Z", 1.0, -1.0)
            self._insert_evo(conn, 1, "2026-03-01T00:00:00Z", 5.0, -4.0)   # newest for 1
            self._insert_evo(conn, 2, "2026-02-01T00:00:00Z", 2.0, -2.0)

        got = t._latest_evolution_by_output([1, 2, 3])

        self.assertEqual(got[1], (5.0, -4.0))
        self.assertEqual(got[2], (2.0, -2.0))
        self.assertNotIn(3, got, "output with no history must be absent, not None-valued")

    def test_empty_input_returns_empty_dict(self):
        t = _tracker()
        self.assertEqual(t._latest_evolution_by_output([]), {})

    def test_handles_more_ids_than_one_query_chunk(self):
        """Chunking at 500 must not drop or duplicate rows."""
        t = _tracker()
        with t._conn() as conn:
            for oid in range(1, 601):
                self._insert_evo(conn, oid, "2026-01-01T00:00:00Z", float(oid), -1.0)

        got = t._latest_evolution_by_output(list(range(1, 601)))

        self.assertEqual(len(got), 600)
        self.assertEqual(got[1][0], 1.0)
        self.assertEqual(got[600][0], 600.0)


# ─────────────────────────────────────────────────────────────────────────────
# update_price_evolution — batched fetch + single write transaction
# ─────────────────────────────────────────────────────────────────────────────
class TestUpdatePriceEvolution(unittest.TestCase):

    def _record(self, tracker, n=1, entry=850.0):
        """Record n signal outputs on one symbol, all recent enough to be in scope."""
        ids = []
        for i in range(n):
            result = {**_make_result(), "tv_symbol": "NASDAQ:NVDA"}
            result["fundamentals"] = {"current_price": entry}
            result["analysis_timestamp"] = (
                datetime.now(timezone.utc) - timedelta(hours=i + 1)
            ).isoformat()
            ids.append(tracker.record_signal_output(result, run_id=f"run-{i}"))
        return ids

    def _run(self, tracker, price):
        import alert_tracker as at
        with mock.patch.object(at, "_YF_OK", True), \
             mock.patch.object(at, "_fetch_price", return_value=price) as m:
            return tracker.update_price_evolution(), m

    def test_writes_one_row_per_output_with_a_single_fetch(self):
        """Three outputs on one symbol: one network call, three evolution rows."""
        t = _tracker()
        self._record(t, n=3)

        result, fetch = self._run(t, 900.0)

        self.assertEqual(fetch.call_count, 1, "shared symbol must be fetched once")
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["failed"], 0)
        with t._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM price_evolution").fetchone()[0]
        self.assertEqual(n, 3)

    def test_prior_mfe_and_mae_carry_forward_between_runs(self):
        """The bulk prior lookup must feed rolling MFE/MAE exactly like the per-row query did."""
        t = _tracker()
        (output_id,) = self._record(t, n=1, entry=100.0)

        self._run(t, 110.0)   # +10%
        self._run(t, 90.0)    # -10%, but MFE must remember the +10%

        with t._conn() as conn:
            row = conn.execute(
                "SELECT return_pct, max_favorable, max_adverse FROM price_evolution "
                "WHERE output_id=? ORDER BY id DESC LIMIT 1",
                (output_id,),
            ).fetchone()

        self.assertAlmostEqual(row["return_pct"], -10.0, places=4)
        self.assertAlmostEqual(row["max_favorable"], 10.0, places=4)
        self.assertAlmostEqual(row["max_adverse"], -10.0, places=4)

    def test_unfetchable_symbol_counts_as_failed_and_writes_nothing(self):
        t = _tracker()
        self._record(t, n=2)

        result, _ = self._run(t, None)

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failed"], 2)
        with t._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM price_evolution").fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
