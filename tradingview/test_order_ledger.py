"""
Tests for order_ledger.py
Run: pytest test_order_ledger.py -v
Uses in-memory / temp SQLite DB — no persistent state.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from order_ledger import OrderLedger, VALID_STATES, TERMINAL_STATES, DEDUP_WINDOW_SECS


def _tmp_ledger() -> OrderLedger:
    """Create a fresh in-memory ledger for each test."""
    return OrderLedger(":memory:")


class TestMakeSid(unittest.TestCase):
    """Deterministic signal ID generation."""

    def test_same_inputs_same_sid(self):
        sid1 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:15:30Z")
        sid2 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:15:30Z")
        self.assertEqual(sid1, sid2)

    def test_same_minute_bucket_same_sid(self):
        """Two deliveries in the same minute → same SID (dedup window)."""
        sid1 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:15:05Z")
        sid2 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:15:55Z")
        self.assertEqual(sid1, sid2)

    def test_different_minute_different_sid(self):
        """Different minute → distinct SID (legitimate re-entry)."""
        sid1 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:15:30Z")
        sid2 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:17:00Z")
        self.assertNotEqual(sid1, sid2)

    def test_different_symbol_different_sid(self):
        sid1 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:15:30Z")
        sid2 = OrderLedger.make_sid("fire123", "NSE:RELIANCE", "2026-04-08T09:15:30Z")
        self.assertNotEqual(sid1, sid2)

    def test_symbol_case_insensitive(self):
        sid1 = OrderLedger.make_sid("fire123", "nse:infy", "2026-04-08T09:15:30Z")
        sid2 = OrderLedger.make_sid("fire123", "NSE:INFY", "2026-04-08T09:15:30Z")
        self.assertEqual(sid1, sid2)

    def test_sid_is_24_hex_chars(self):
        sid = OrderLedger.make_sid("x", "SYM", "2026-04-08T10:00:00Z")
        self.assertEqual(len(sid), 24)
        self.assertTrue(all(c in "0123456789abcdef" for c in sid))

    def test_no_fire_time_uses_current_minute(self):
        sid1 = OrderLedger.make_sid("fire999", "AAPL", None)
        sid2 = OrderLedger.make_sid("fire999", "AAPL", None)
        # Both called in same second → same minute bucket → same sid
        self.assertEqual(sid1, sid2)


class TestRecord(unittest.TestCase):
    """record() writes events; invalid states raise."""

    def test_record_valid_state_returns_rowid(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f1", "SYM", "2026-04-08T09:00:00Z")
        rowid = ledger.record(sid, "RECEIVED", symbol="SYM", fire_id="f1")
        self.assertIsInstance(rowid, int)
        self.assertGreater(rowid, 0)

    def test_record_invalid_state_raises(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f2", "SYM", "2026-04-08T09:00:00Z")
        with self.assertRaises(ValueError):
            ledger.record(sid, "INVALID_STATE")

    def test_record_all_valid_states(self):
        ledger = _tmp_ledger()
        for i, state in enumerate(sorted(VALID_STATES)):
            sid = OrderLedger.make_sid(f"f{i}", "SYM", f"2026-04-08T09:{i:02d}:00Z")
            rowid = ledger.record(sid, state)
            self.assertIsInstance(rowid, int)

    def test_record_stores_extra_kwargs_as_json(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f3", "SYM", "2026-04-08T09:00:00Z")
        ledger.record(sid, "ANALYZED", symbol="SYM", composite=78.4, threshold=65)
        ev = ledger.get_state(sid)
        import json
        data = json.loads(ev["data_json"])
        self.assertAlmostEqual(data["composite"], 78.4, places=1)
        self.assertEqual(data["threshold"], 65)


class TestGetState(unittest.TestCase):
    """get_state() returns the most recent event for the sid."""

    def test_get_state_none_for_unknown_sid(self):
        ledger = _tmp_ledger()
        self.assertIsNone(ledger.get_state("unknown_sid_123456789012"))

    def test_get_state_returns_latest(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f4", "SYM", "2026-04-08T09:00:00Z")
        ledger.record(sid, "RECEIVED")
        ledger.record(sid, "ANALYZED")
        ledger.record(sid, "PLACED", order_id="OID999")
        state = ledger.get_state(sid)
        self.assertEqual(state["state"], "PLACED")
        self.assertEqual(state["order_id"], "OID999")


class TestGetHistory(unittest.TestCase):
    """get_history() returns all transitions in order."""

    def test_history_ordered_chronologically(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f5", "SYM", "2026-04-08T09:00:00Z")
        for state in ("RECEIVED", "ANALYZED", "ROUTED", "PLACED", "FILLED"):
            ledger.record(sid, state)
        history = ledger.get_history(sid)
        states = [h["state"] for h in history]
        self.assertEqual(states, ["RECEIVED", "ANALYZED", "ROUTED", "PLACED", "FILLED"])

    def test_history_empty_for_unknown_sid(self):
        ledger = _tmp_ledger()
        self.assertEqual(ledger.get_history("nonexistent_sid_00000000"), [])


class TestIsDuplicate(unittest.TestCase):
    """is_duplicate() prevents re-processing."""

    def test_new_sid_is_not_duplicate(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f6", "SYM", "2026-04-08T09:05:00Z")
        self.assertFalse(ledger.is_duplicate(sid))

    def test_recently_received_is_duplicate(self):
        """A SID received within DEDUP_WINDOW_SECS is a duplicate."""
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f7", "SYM", "2026-04-08T09:06:00Z")
        ledger.record(sid, "RECEIVED")
        self.assertTrue(ledger.is_duplicate(sid))

    def test_placed_is_always_duplicate(self):
        """A SID that reached PLACED is always a duplicate."""
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f8", "SYM", "2026-04-08T09:07:00Z")
        ledger.record(sid, "RECEIVED")
        ledger.record(sid, "PLACED", order_id="OID123")
        self.assertTrue(ledger.is_duplicate(sid))

    def test_filled_is_always_duplicate(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f9", "SYM", "2026-04-08T09:08:00Z")
        ledger.record(sid, "PLACED", order_id="X")
        ledger.record(sid, "FILLED", qty=10, fill_price=200.0)
        self.assertTrue(ledger.is_duplicate(sid))

    def test_rejected_outside_window_is_not_duplicate(self):
        """A REJECTED signal outside the dedup window can be retried."""
        import sqlite3
        from datetime import datetime, timezone, timedelta
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f10", "SYM", "2026-04-08T09:09:00Z")
        # Insert a REJECTED event with an old timestamp directly
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=DEDUP_WINDOW_SECS + 5)).isoformat()
        with ledger._lock:
            with ledger._connect() as conn:
                conn.execute(
                    "INSERT INTO signal_events (sid, state, symbol, broker, fire_id, order_id, "
                    "data_json, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                    (sid, "REJECTED", "SYM", None, None, None, None, old_ts)
                )
                conn.commit()
        self.assertFalse(ledger.is_duplicate(sid))


class TestRecent(unittest.TestCase):
    """recent() returns last N events."""

    def test_recent_returns_chronological_order(self):
        ledger = _tmp_ledger()
        for i in range(5):
            sid = OrderLedger.make_sid(f"fire{i}", f"SYM{i}", f"2026-04-08T0{i+1}:00:00Z")
            ledger.record(sid, "RECEIVED", symbol=f"SYM{i}")
        recent = ledger.recent(3)
        self.assertEqual(len(recent), 3)
        # Should be last 3 insertions in chronological order
        syms = [r["symbol"] for r in recent]
        self.assertIn("SYM4", syms)
        self.assertIn("SYM3", syms)
        self.assertIn("SYM2", syms)

    def test_recent_with_fewer_rows_than_n(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("f_only", "SYM", "2026-04-08T10:00:00Z")
        ledger.record(sid, "RECEIVED")
        recent = ledger.recent(50)
        self.assertEqual(len(recent), 1)


class TestStats(unittest.TestCase):
    """stats() returns aggregate counts."""

    def test_stats_empty_ledger(self):
        ledger = _tmp_ledger()
        s = ledger.stats()
        self.assertEqual(s["total_signals"], 0)
        self.assertEqual(s["by_state"], {})

    def test_stats_counts_by_state(self):
        ledger = _tmp_ledger()
        for i in range(3):
            sid = OrderLedger.make_sid(f"statf{i}", "SYM", f"2026-04-08T0{i+1}:00:00Z")
            ledger.record(sid, "RECEIVED")
        sid_placed = OrderLedger.make_sid("statf_placed", "SYM", "2026-04-08T05:00:00Z")
        ledger.record(sid_placed, "RECEIVED")
        ledger.record(sid_placed, "PLACED", order_id="O1")
        s = ledger.stats()
        self.assertEqual(s["total_signals"], 4)
        self.assertEqual(s["by_state"]["RECEIVED"], 4)  # all 4 have RECEIVED event
        self.assertEqual(s["by_state"]["PLACED"], 1)


class TestPurgeOld(unittest.TestCase):
    """purge_old() removes aged records."""

    def test_purge_removes_old_events(self):
        import sqlite3
        from datetime import datetime, timezone, timedelta
        ledger = _tmp_ledger()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        with ledger._lock:
            with ledger._connect() as conn:
                conn.execute(
                    "INSERT INTO signal_events (sid, state, symbol, broker, fire_id, "
                    "order_id, data_json, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                    ("old_sid_12345678901234", "RECEIVED", "SYM", None, None, None, None, old_ts)
                )
                conn.commit()
        # Verify it's there
        self.assertEqual(len(ledger.recent(100)), 1)
        deleted = ledger.purge_old(days=90)
        self.assertEqual(deleted, 1)
        self.assertEqual(len(ledger.recent(100)), 0)

    def test_purge_keeps_recent_events(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("recent_fire", "SYM", "2026-04-08T10:00:00Z")
        ledger.record(sid, "RECEIVED")
        deleted = ledger.purge_old(days=7)
        self.assertEqual(deleted, 0)
        self.assertEqual(len(ledger.recent()), 1)


class TestFindByOrderId(unittest.TestCase):
    """find_by_order_id() finds events by broker order ID."""

    def test_find_by_order_id(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("order_lookup", "NSE:INFY", "2026-04-08T10:30:00Z")
        ledger.record(sid, "RECEIVED", symbol="NSE:INFY")
        ledger.record(sid, "PLACED", broker="zerodha", order_id="ZKOrder_12345")
        events = ledger.find_by_order_id("ZKOrder_12345")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "PLACED")
        self.assertEqual(events[0]["broker"], "zerodha")

    def test_find_by_order_id_with_broker_filter(self):
        ledger = _tmp_ledger()
        sid = OrderLedger.make_sid("multi_broker", "US.AAPL", "2026-04-08T10:31:00Z")
        ledger.record(sid, "PLACED", broker="zerodha",  order_id="OID_SHARED")
        ledger.record(sid, "PLACED", broker="moomoo",   order_id="OID_SHARED")
        zerodha_events = ledger.find_by_order_id("OID_SHARED", broker="zerodha")
        self.assertEqual(len(zerodha_events), 1)
        self.assertEqual(zerodha_events[0]["broker"], "zerodha")


class TestThreadSafety(unittest.TestCase):
    """Concurrent writes should not cause data corruption."""

    def test_concurrent_records(self):
        ledger = _tmp_ledger()
        errors = []
        n_threads = 10
        n_writes  = 20

        def worker(thread_id):
            try:
                for i in range(n_writes):
                    sid = OrderLedger.make_sid(
                        f"thread{thread_id}_fire{i}",
                        f"SYM{thread_id}",
                        f"2026-04-08T10:{thread_id:02d}:{i:02d}Z",
                    )
                    ledger.record(sid, "RECEIVED", symbol=f"SYM{thread_id}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        s = ledger.stats()
        self.assertEqual(s["total_signals"], n_threads * n_writes)


if __name__ == "__main__":
    unittest.main()
