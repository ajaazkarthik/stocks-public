"""
Order Event Ledger — durable SQLite-backed signal state machine
================================================================
Tracks the full lifecycle of every TradingView fire from signal receipt
through execution and protection arming.

WHY THIS EXISTS:
  TradingView webhooks are at-least-once: a single alert fire can arrive 2–3×
  during network hiccups or TV retries.  Zerodha and Moomoo postbacks for order
  fills can arrive out of order.  Without a persistent state machine, duplicate
  orders are the inevitable failure mode.

SIGNAL ID (sid):
  Deterministic hash of (fire_id + symbol + minute-bucket of fire_time).
  Minute-resolution means: two webhook deliveries within 60 s for the same
  alert are treated as duplicates.  A retry 2 minutes later is treated as a
  new signal (e.g. legitimate re-entry after market re-opens).

STATES:
  RECEIVED      — webhook POST accepted, signal queued for analysis
  ANALYZED      — signal_stack scored the symbol
  ROUTED        — broker selected, order preparation started
  PLACED        — broker confirmed order accepted (order_id assigned)
  ACKNOWLEDGED  — broker returned fill confirmation
  FILLED        — full fill confirmed (qty, fill_price recorded)
  PROTECTED     — GTT / stop-loss protection armed post-fill
  REJECTED      — broker rejected the order explicitly
  CANCELLED     — order cancelled before fill

DB location: database/order_ledger.db  (centralised database directory)

Usage:
    from order_ledger import OrderLedger

    ledger = OrderLedger()     # creates DB if needed

    # Dedup check at webhook receipt:
    sid = ledger.make_sid(fire_id="abc123", symbol="NSE:INFY",
                          fire_time_iso="2026-04-08T09:15:30Z")
    if ledger.is_duplicate(sid):
        return  # already processing this fire

    ledger.record(sid, "RECEIVED", symbol="NSE:INFY", fire_id="abc123")
    ledger.record(sid, "ANALYZED", composite=78.4, threshold=65)
    ledger.record(sid, "PLACED",   broker="zerodha", order_id="OID123")
    ledger.record(sid, "FILLED",   qty=10, fill_price=1823.50)
    ledger.record(sid, "PROTECTED", gtt_id="GTT456", sl=1700.0, tp=2000.0)

    # Query:
    state  = ledger.get_state(sid)    # latest state dict
    hist   = ledger.get_history(sid)  # all transitions in order
    recent = ledger.recent(n=50)      # last 50 events across all signals
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─── Configuration ─────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DEFAULT_DB_PATH = BASE_DIR.parent / "database" / "order_ledger.db"

# Dedup window: two webhook deliveries within this many seconds → same signal
DEDUP_WINDOW_SECS = 60

# Valid state values (ordered for readability, not enforced as FSM)
VALID_STATES = frozenset([
    "RECEIVED",
    "ANALYZED",
    "ROUTED",
    "PLACED",
    "ACKNOWLEDGED",
    "FILLED",
    "PROTECTED",
    "REJECTED",
    "CANCELLED",
])

# Terminal states — once reached, no further transitions are expected
TERMINAL_STATES = frozenset(["FILLED", "REJECTED", "CANCELLED"])


# ─── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS signal_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sid        TEXT    NOT NULL,         -- deterministic signal ID
    state      TEXT    NOT NULL,         -- one of VALID_STATES
    symbol     TEXT,
    broker     TEXT,
    fire_id    TEXT,
    order_id   TEXT,
    data_json  TEXT,                     -- extra payload as JSON
    recorded_at TEXT NOT NULL            -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_sid        ON signal_events(sid);
CREATE INDEX IF NOT EXISTS idx_recorded   ON signal_events(recorded_at);
CREATE INDEX IF NOT EXISTS idx_state      ON signal_events(state);

-- Latest-state view for quick current-state queries
CREATE VIEW IF NOT EXISTS signal_latest AS
SELECT   se.*
FROM     signal_events se
WHERE    se.id = (
    SELECT MAX(id) FROM signal_events WHERE sid = se.sid
);
"""


# ─── OrderLedger class ─────────────────────────────────────────────────────────

class OrderLedger:
    """
    SQLite-backed order event ledger.

    Thread-safe: a per-instance lock serialises all write operations.
    Multiple OrderLedger instances pointing at the same DB are safe (SQLite WAL).
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self._db_path = Path(db_path)
        self._lock    = threading.Lock()
        # For ':memory:' databases each sqlite3.connect() call would create a
        # brand-new empty DB.  Keep a single persistent connection instead.
        self._mem_conn: Optional[sqlite3.Connection] = None
        if str(db_path) == ":memory:":
            self._mem_conn = self._make_conn(":memory:")
        self._init_db()

    # ── DB bootstrap ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create schema if the DB does not yet exist."""
        with self._connect() as conn:
            conn.executescript(_DDL)
            # Wire into versioned migration system so future schema changes
            # can be applied via database/migrations.py instead of ALTER TABLE.
            try:
                _root_dir = str(self._db_path.parent.parent) if str(self._db_path) != ":memory:" else ""
                if _root_dir and _root_dir not in __import__("sys").path:
                    __import__("sys").path.insert(0, _root_dir)
                from database.migrations import run_migrations
                run_migrations("order_ledger", conn)
            except Exception:
                pass  # migrations module unavailable (standalone use)

    @staticmethod
    def _make_conn(path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        """Return (or open) a connection to the ledger DB."""
        if self._mem_conn is not None:
            return self._mem_conn
        return self._make_conn(str(self._db_path))

    # ── Signal-ID generation ──────────────────────────────────────────────────

    @staticmethod
    def make_sid(
        fire_id: str,
        symbol:  str,
        fire_time_iso: Optional[str] = None,
    ) -> str:
        """
        Build a deterministic signal ID from the fire.

        fire_id        — TV fire_id or fire_time string
        symbol         — pro_symbol (e.g. 'NSE:INFY')
        fire_time_iso  — ISO-8601 timestamp; resolution truncated to 1-minute
                         bucket so TV retries within 60 s are treated as dupes.
                         If None, uses current UTC minute.
        """
        if fire_time_iso:
            try:
                dt = datetime.fromisoformat(
                    str(fire_time_iso).replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                # Truncate to minute bucket
                minute_bucket = dt.strftime("%Y%m%dT%H%M")
            except Exception:
                minute_bucket = fire_time_iso[:16]  # best-effort
        else:
            minute_bucket = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")

        raw = f"{fire_id}|{symbol.upper()}|{minute_bucket}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    # ── Idempotency check ─────────────────────────────────────────────────────

    def is_duplicate(self, sid: str) -> bool:
        """
        Return True if this signal ID has already been received within
        DEDUP_WINDOW_SECS seconds, or has already been placed/filled.

        This prevents TV webhook retries from creating double orders.
        """
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT state, recorded_at
                    FROM   signal_events
                    WHERE  sid = ?
                    ORDER  BY id DESC
                    LIMIT  1
                    """,
                    (sid,),
                ).fetchone()

        if row is None:
            return False  # never seen

        state = row["state"]
        # If already placed or beyond, always a duplicate
        if state in ("PLACED", "ACKNOWLEDGED", "FILLED", "PROTECTED"):
            return True

        # If received recently, still within dedup window
        try:
            rec_dt = datetime.fromisoformat(row["recorded_at"])
            if rec_dt.tzinfo is None:
                rec_dt = rec_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - rec_dt).total_seconds()
            if age <= DEDUP_WINDOW_SECS:
                return True
        except Exception:
            return True  # unparseable — treat as dupe, fail-safe

        return False

    # ── Record a state transition ─────────────────────────────────────────────

    def record(
        self,
        sid:     str,
        state:   str,
        symbol:  Optional[str]  = None,
        broker:  Optional[str]  = None,
        fire_id: Optional[str]  = None,
        order_id: Optional[str] = None,
        **extra: Any,
    ) -> int:
        """
        Append a state-transition event to the ledger.

        Args:
            sid      — signal ID from make_sid()
            state    — one of VALID_STATES
            symbol   — trading symbol (optional, for readability)
            broker   — 'zerodha' | 'moomoo' | 'angelone' | None
            fire_id  — original TV fire_id
            order_id — broker order ID once placed
            **extra  — any additional context stored as JSON payload

        Returns the auto-incremented row ID.
        """
        if state not in VALID_STATES:
            raise ValueError(f"Unknown state '{state}'. Valid: {VALID_STATES}")

        data_json = json.dumps(extra, default=str) if extra else None
        now_iso   = datetime.now(timezone.utc).isoformat()

        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO signal_events
                        (sid, state, symbol, broker, fire_id, order_id, data_json, recorded_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (sid, state, symbol, broker, fire_id, order_id, data_json, now_iso),
                )
                conn.commit()
                return cur.lastrowid  # type: ignore[return-value]

    # ── Query helpers ─────────────────────────────────────────────────────────

    def get_state(self, sid: str) -> Optional[dict]:
        """Return the latest state event dict for this signal, or None."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM signal_events
                    WHERE  sid = ?
                    ORDER  BY id DESC
                    LIMIT  1
                    """,
                    (sid,),
                ).fetchone()
        return dict(row) if row else None

    def get_history(self, sid: str) -> list[dict]:
        """Return all state events for this signal in chronological order."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM signal_events
                    WHERE  sid = ?
                    ORDER  BY id ASC
                    """,
                    (sid,),
                ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, n: int = 50) -> list[dict]:
        """Return the last `n` ledger events across all signals."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM signal_events
                    ORDER  BY id DESC
                    LIMIT  ?
                    """,
                    (n,),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def find_by_order_id(self, order_id: str, broker: Optional[str] = None) -> list[dict]:
        """Find all events associated with a broker order_id."""
        query = "SELECT * FROM signal_events WHERE order_id = ?"
        params: list = [order_id]
        if broker:
            query += " AND broker = ?"
            params.append(broker)
        query += " ORDER BY id ASC"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        """Return aggregate counts per state for monitoring."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT state, COUNT(*) as cnt FROM signal_events GROUP BY state"
                ).fetchall()
                total_signals = conn.execute(
                    "SELECT COUNT(DISTINCT sid) FROM signal_events"
                ).fetchone()[0]
        counts = {r["state"]: r["cnt"] for r in rows}
        return {"total_signals": total_signals, "by_state": counts}

    def purge_old(self, days: int = 90) -> int:
        """Delete events older than `days` days. Returns deleted row count."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM signal_events WHERE recorded_at < ?", (cutoff,)
                )
                conn.commit()
                return cur.rowcount


# ─── Module-level singleton ────────────────────────────────────────────────────

_default_ledger: Optional[OrderLedger] = None
_default_lock = threading.Lock()


def get_ledger(db_path: Path | str = DEFAULT_DB_PATH) -> OrderLedger:
    """Return the module-level singleton ledger (created on first call)."""
    global _default_ledger
    with _default_lock:
        if _default_ledger is None:
            _default_ledger = OrderLedger(db_path)
    return _default_ledger


# ─── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, pprint

    parser = argparse.ArgumentParser(description="Inspect order event ledger")
    parser.add_argument("--recent", type=int, metavar="N", default=20,
                        help="Show last N events")
    parser.add_argument("--sid",    type=str, help="Show full history for a signal ID")
    parser.add_argument("--stats",  action="store_true", help="Show aggregate statistics")
    parser.add_argument("--purge",  type=int, metavar="DAYS",
                        help="Purge events older than DAYS days")
    parser.add_argument("--db",     type=str, default=str(DEFAULT_DB_PATH),
                        help="Path to ledger DB")
    args = parser.parse_args()

    ledger = OrderLedger(args.db)

    if args.purge:
        deleted = ledger.purge_old(args.purge)
        print(f"Purged {deleted} rows older than {args.purge} days.")

    if args.stats:
        pprint.pprint(ledger.stats())

    if args.sid:
        history = ledger.get_history(args.sid)
        if not history:
            print(f"No events found for sid={args.sid}")
        else:
            print(f"History for sid={args.sid} ({len(history)} events):")
            for ev in history:
                print(f"  [{ev['recorded_at']}] {ev['state']:15s}  "
                      f"broker={ev['broker'] or '-':10s} "
                      f"order_id={ev['order_id'] or '-'}")
    else:
        events = ledger.recent(args.recent)
        if not events:
            print("Ledger is empty.")
        else:
            print(f"Last {len(events)} events:")
            for ev in events:
                print(f"  [{ev['recorded_at']}] {ev['sid'][:12]}  "
                      f"{ev['state']:15s}  {ev['symbol'] or '-':20s}  "
                      f"broker={ev['broker'] or '-'}")
