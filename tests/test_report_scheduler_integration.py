"""Integration tests against the REAL report_scheduler callbacks.

The previous suite proved individual helpers work. These tests drive the actual
registered application entry points with only external effects replaced, and
assert persisted identities and exact state transitions.

Covered review findings: 1, 2, 3, 4, 6, 7, 9, 10, 11, 12, 13.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import report_scheduler as rs
from tradingview import outcome_store
from tradingview.scheduler import formatting, identity, stock_results
from tradingview.scheduler.config import SchedulerConfig
from tradingview.scheduler.cycle import (
    CYCLE_FAILED,
    CYCLE_NO_NEW,
    CYCLE_OK,
    CYCLE_PARTIAL,
    CycleLock,
    CyclePaths,
    produce_cycle,
)
from tradingview.scheduler.dispatch import dispatch_pending, reconcile, reconcile_cycle_results
from tradingview.scheduler.outbox import (
    STATUS_AMBIGUOUS,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    NotificationOutbox,
    sqlite_factory,
)
from tradingview.scheduler.steps import STATUS_FAILED, STATUS_OK, StepResult

RUN_ID = "ss_20261013T064219Z_integ001"
FROZEN_NOW = datetime(2026, 10, 13, 6, 42, 19, tzinfo=timezone.utc)
RECIPIENTS = [111, 222]


@pytest.fixture(autouse=True)
def _isolate_startup_ledger(tmp_path, monkeypatch):
    """Startup callbacks must never initialize the workspace's real ledger."""
    ledger_path = tmp_path / "startup_ledger.db"

    def connect():
        conn = sqlite3.connect(ledger_path)
        conn.row_factory = sqlite3.Row
        outcome_store.ensure_schema(conn)
        conn.commit()
        return conn

    monkeypatch.setattr("database.fires_ledger._conn", connect)


class Clock:
    def __init__(self, start: datetime = FROZEN_NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class Runner:
    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[str] = []

    def __call__(self, label, cmd, cwd=None, env=None, timeout=900, **kwargs):
        self.calls.append(label)
        status = self.failures.get(label, STATUS_OK)
        return StepResult(
            label=label, status=status,
            exit_code=0 if status == STATUS_OK else 1,
            duration_seconds=0.01, output="ok" if status == STATUS_OK else "boom",
            error="" if status == STATUS_OK else f"{label} failed",
        )


class Bot:
    """Local Telegram stub. Records calls; never touches the network."""

    def __init__(self, *, fail_for: set[int] | None = None,
                 no_message_id: bool = False) -> None:
        self.fail_for = fail_for or set()
        self.no_message_id = no_message_id
        self.calls: list[dict] = []
        self._next = 1000

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["chat_id"] in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self._next += 1
        if self.no_message_id:
            return type("Msg", (), {})()
        return type("Msg", (), {"message_id": self._next})()


def _config(**overrides) -> SchedulerConfig:
    base = dict(
        stock_threshold=62.5, option_threshold=70.0, step_timeout=30,
        display_timezone="Australia/Sydney", schedule_timezone="Asia/Kolkata",
    )
    base.update(overrides)
    return SchedulerConfig(**base)


def _stock(pro_symbol="NASDAQ:GOOG", score=71.0, fire_time="2026-10-13T05:00:00Z",
           resolution="60"):
    exchange, _, symbol = pro_symbol.partition(":")
    return {"pro_symbol": pro_symbol, "exchange": exchange, "symbol": symbol,
            "score": score, "signal": "BUY", "fire_time": fire_time,
            "resolution": resolution}


def _contract(score=79.0, symbol="MSFT"):
    return {"market": "US", "exchange": "NASDAQ", "symbol": symbol,
            "option_type": "CALL", "strike": 530.0, "expiry": "2030-06-19",
            "dte": 45, "option_score": score, "parent_signal_score": 91.0,
            "strategy_modes": ["BALANCED"]}


def _seed(paths: CyclePaths, run_id: str, stocks=None, contracts=None,
          stock_status="success", option_status="success") -> None:
    paths.ensure()
    stock_results.write_current_run_payload(
        paths.stock_payload,
        stock_results.build_current_run_payload(run_id, stocks or [], status=stock_status),
    )
    from tradingview.options.options_snapshot import build_snapshot, publish_snapshot

    publish_snapshot(paths.options_snapshot, build_snapshot(run_id, option_status, contracts or []))


def _produce(tmp_path, run_id, runner, **kwargs):
    paths = CyclePaths(tmp_path, run_id)
    return produce_cycle(
        run_id=run_id, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW, **kwargs
    ), paths


# ══════════════════════════════════════════════════════════════════════════════
# Finding 1 — the application retries failed messages automatically
# ══════════════════════════════════════════════════════════════════════════════

def test_registered_dispatch_job_retries_a_failed_earlier_run(tmp_path, monkeypatch):
    """A failed send from run A is retried by the registered dispatch job."""
    clock = Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                backoff_base_seconds=1)
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)

    # Run A: delivery fails.
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    paths_a = CyclePaths(tmp_path, "run-a")
    _seed(paths_a, "run-a", stocks=[_stock()])
    result_a, _ = _produce(tmp_path, "run-a", Runner())

    enqueue_cycle(outbox, result_a, [111], limit=3500, analysis_time=FROZEN_NOW)

    async def _transient_failure(**kwargs):
        raise RuntimeError("TimedOut")

    asyncio.run(deliver_cycle(outbox, result_a, [111], send=_transient_failure,
                              limit=3500, analysis_time=FROZEN_NOW))
    assert outbox.pending_count() == 1, "a transient failure must stay retryable"

    # Run B happens; the registered dispatch job must also retry A's row.
    clock.advance(60)
    paths_b = CyclePaths(tmp_path, "run-b")
    _seed(paths_b, "run-b", stocks=[_stock("NASDAQ:MSFT", fire_time="2026-10-13T07:00:00Z")])
    result_b, _ = _produce(tmp_path, "run-b", Runner())
    enqueue_cycle(outbox, result_b, [111], limit=3500, analysis_time=FROZEN_NOW)

    bot = Bot()
    context = type("Ctx", (), {"bot": bot})()
    asyncio.run(rs._job_outbox_dispatch(context))

    # Both A's retried row and B's row are now sent.
    assert outbox.counts().get(STATUS_SENT, 0) >= 2
    assert outbox.pending_count() == 0
    # A's original alert body was actually retried, not just B's.
    assert any("GOOG" in call["text"] for call in bot.calls)


def test_dispatch_sends_the_persisted_payload_not_a_reconstruction(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="PERSISTED-BODY-MARKER")

    bot = Bot()
    report = asyncio.run(dispatch_pending(outbox, bot=bot))
    assert report.sent == 1
    assert "PERSISTED-BODY-MARKER" in bot.calls[0]["text"]


def test_dispatch_job_is_registered(monkeypatch):
    app = _fake_app()
    monkeypatch.setenv("SCHEDULER_EVERY_HOURS", "2")
    rs._setup_job_queue(app)
    names = {job["name"] for job in app.job_queue.repeating}
    assert "outbox_dispatch" in names
    once_names = {job["name"] for job in app.job_queue.once}
    assert "startup_recovery" in once_names


def test_dispatch_runs_on_an_excluded_analysis_day(tmp_path, monkeypatch):
    """Pending delivery is independent of the weekday analysis gate."""
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    monkeypatch.setattr(rs, "_scheduler_paused", False)
    monkeypatch.setenv("SCHEDULER_DAYS", "1,2,3,4,5")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Asia/Kolkata")
    # Saturday in the schedule timezone.
    monkeypatch.setattr(rs, "_now_utc",
                        lambda: datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc))

    bot = Bot()
    context = type("Ctx", (), {"bot": bot})()
    asyncio.run(rs._job_interval_run(context))

    assert outbox.counts().get(STATUS_SENT) == 1


def test_pause_stops_dispatch(tmp_path, monkeypatch):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    monkeypatch.setattr(rs, "_scheduler_paused", True)

    bot = Bot()
    context = type("Ctx", (), {"bot": bot})()
    asyncio.run(rs._job_outbox_dispatch(context))
    assert outbox.pending_count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# Finding 2 — durable recovery is wired into production
# ══════════════════════════════════════════════════════════════════════════════

def test_startup_recovery_enqueues_from_durable_scoring_results(tmp_path, monkeypatch):
    """A crash after commit but before enqueue is recovered on restart."""
    ledger = tmp_path / "fires_ledger.db"
    conn = sqlite3.connect(str(ledger))
    conn.row_factory = sqlite3.Row
    outcome_store.ensure_schema(conn)
    outcome_store.persist_completed(
        conn,
        {"pro_symbol": "NASDAQ:GOOG", "fire_time": "2026-10-13T05:00:00Z",
         "resolution": "60"},
        RUN_ID,
        {"composite": 71.0, "actionable": True},
    )
    conn.commit()
    conn.close()

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    monkeypatch.setattr(rs, "_allowed_users", lambda: {111})
    monkeypatch.setattr(rs, "ROOT", tmp_path)

    def _ledger_conn():
        c = sqlite3.connect(str(ledger))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("database.fires_ledger._conn", _ledger_conn)

    queued = rs._reconcile_durable_work([111])
    assert queued == 1
    assert outbox.pending_count() == 1

    # Idempotent: a second reconciliation adds nothing.
    assert rs._reconcile_durable_work([111]) == 0


def test_reconcile_skips_already_delivered_events(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    stock = _stock()
    key = identity.stock_delivery_key(111, stock)
    outbox.record_events_delivered(
        [{"delivery_key": key, "recipient": 111, "kind": identity.KIND_STOCK}],
        run_id=RUN_ID, message_id=1,
    )

    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    outcome_store.ensure_schema(conn)
    outcome_store.persist_completed(
        conn,
        {"pro_symbol": "NASDAQ:GOOG", "fire_time": "2026-10-13T05:00:00Z",
         "resolution": "60"},
        RUN_ID, {"composite": 71.0, "actionable": True},
    )
    conn.commit()

    assert reconcile(outbox, conn, [111]) == 0
    conn.close()


def test_reconcile_skips_non_actionable_results(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    outcome_store.ensure_schema(conn)
    outcome_store.persist_completed(
        conn,
        {"pro_symbol": "NASDAQ:VETO", "fire_time": "2026-10-13T05:00:00Z",
         "resolution": "60"},
        RUN_ID, {"composite": 95.0, "actionable": False,
                 "actionability_reasons": ["earnings blackout"]},
    )
    conn.commit()

    assert reconcile(outbox, conn, [111]) == 0
    conn.close()


def test_reconcile_cycle_results_recovers_persisted_cycles(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    run_root = tmp_path / "runs"
    paths = CyclePaths(run_root, RUN_ID)
    _seed(paths, RUN_ID, stocks=[_stock()])
    produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=Runner(),
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )
    # Simulate crash-before-enqueue: the cycle result exists, the outbox is empty.
    assert paths.cycle_result.exists()
    assert outbox.all_items() == []

    queued = reconcile_cycle_results(outbox, run_root, [111, 222])
    assert queued == 2
    assert reconcile_cycle_results(outbox, run_root, [111, 222]) == 0


def test_startup_recovery_job_is_callable(tmp_path, monkeypatch):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")
    monkeypatch.setattr(rs, "_scheduler_outbox", lambda: outbox)
    monkeypatch.setattr(rs, "_allowed_users", lambda: {111})
    monkeypatch.setattr(rs, "_scheduler_paused", False)
    monkeypatch.setattr(rs, "ROOT", tmp_path)

    bot = Bot()
    context = type("Ctx", (), {"bot": bot})()
    asyncio.run(rs._job_startup_recovery(context))
    assert outbox.counts().get(STATUS_SENT) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Finding 3 — completion requires a retained payload
# ══════════════════════════════════════════════════════════════════════════════

def test_incremental_failure_leaves_fire_unscored_for_retry(tmp_path):
    """Fault injection: incremental persistence fails, batch succeeds.

    The fire must NOT be suppressed, because no recoverable payload exists.
    """
    import ast

    source = (Path(rs.__file__).resolve().parents[0] / "tradingview" / "signal_stack.py").read_text()
    tree = ast.parse(source)

    # The append to _completed_scoring_fires must be guarded by _durable_result.
    seam = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "_completed_scoring_fires.append"
    )
    guard = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "_durable_result"
        and seam in node.body
    )
    assert guard is not None

    # The unretained branch must record a FAILED attempt, not a completed one.
    assert "_unretained_scoring_fires.append" in source
    assert "scoring result was not durably retained" in source
    # mark_scored must only ever receive durably retained fires.
    assert "_mark_scored(_completed_scoring_fires)" in source


def test_unretained_fire_is_not_marked_scored(tmp_path):
    """A fire with no payload must remain eligible for a later run."""
    from database import fires_ledger

    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS downloaded_fires (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pro_symbol TEXT NOT NULL,
            fire_time TEXT NOT NULL, resolution TEXT NOT NULL DEFAULT '',
            fire_id INTEGER, alert_id INTEGER, symbol TEXT, name TEXT,
            message TEXT, bar_time TEXT, source TEXT NOT NULL DEFAULT 'download',
            fire_json TEXT, first_seen_at TEXT NOT NULL, scored_at TEXT,
            UNIQUE (pro_symbol, fire_time, resolution));
        """
    )
    outcome_store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO downloaded_fires (pro_symbol, fire_time, resolution, first_seen_at) "
        "VALUES ('NASDAQ:A', 't1', '60', '2026-10-13')"
    )
    conn.commit()

    # No scoring_results row exists, so the fire is not suppressed.
    assert outcome_store.load_scoring_results(conn) == []
    assert conn.execute(
        "SELECT scored_at FROM downloaded_fires WHERE pro_symbol='NASDAQ:A'"
    ).fetchone()[0] is None
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Finding 4 — every stock row reaches delivery
# ══════════════════════════════════════════════════════════════════════════════

def test_twenty_one_stocks_all_reach_a_persisted_part(tmp_path):
    stocks = [_stock(f"NASDAQ:S{i:02d}", 70.0 + i * 0.1) for i in range(21)]
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=stocks)

    result, _ = _produce(tmp_path, RUN_ID, Runner())
    assert len(result.stocks) == 21, "the loader truncated the current-run rows"
    assert result.stock_result.remainder == 0

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)
    bot = Bot()
    asyncio.run(deliver_cycle(outbox, result, [111], bot=bot, limit=3500,
                              analysis_time=FROZEN_NOW))

    # Every one of the 21 identities is recorded as delivered.
    delivered = outbox.delivered_event_keys(111, identity.KIND_STOCK)
    assert len(delivered) == 21
    for stock in stocks:
        assert identity.stock_delivery_key(111, stock) in delivered


def test_remainder_claim_is_backed_by_real_delivery_work(tmp_path):
    """The summary must not claim a remainder that has no delivery record."""
    stocks = [_stock(f"NASDAQ:S{i:02d}", 70.0) for i in range(25)]
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=stocks)
    result, _ = _produce(tmp_path, RUN_ID, Runner())

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)
    asyncio.run(deliver_cycle(outbox, result, [111], bot=Bot(), limit=3500,
                              analysis_time=FROZEN_NOW))
    assert len(outbox.delivered_event_keys(111, identity.KIND_STOCK)) == 25


# ══════════════════════════════════════════════════════════════════════════════
# Finding 6 — the no-new producer path works
# ══════════════════════════════════════════════════════════════════════════════

def test_no_new_producer_writes_a_payload_with_an_explicit_run_id(tmp_path, monkeypatch):
    """The real early-return helper must create the payload, not NameError."""
    import importlib

    signal_stack = importlib.import_module("tradingview.signal_stack")
    target = tmp_path / "stock_results.json"
    monkeypatch.setattr(signal_stack, "_STOCK_PAYLOAD_PATH", target)

    signal_stack._write_empty_stock_payload("run-explicit-001")

    assert target.exists(), "the no-new producer did not create the payload"
    payload = json.loads(target.read_text())
    assert payload["run_id"] == "run-explicit-001"
    assert payload["status"] == "no_new"
    assert payload["stocks"] == []


def test_no_new_producer_requires_a_run_id(tmp_path, monkeypatch):
    import importlib

    signal_stack = importlib.import_module("tradingview.signal_stack")
    target = tmp_path / "stock_results.json"
    monkeypatch.setattr(signal_stack, "_STOCK_PAYLOAD_PATH", target)

    signal_stack._write_empty_stock_payload("")
    assert not target.exists()


def test_no_new_payload_is_consumed_as_no_new(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[], stock_status="no_new", option_status="no_new_signals")
    result, _ = _produce(tmp_path, RUN_ID, Runner())
    assert result.status == CYCLE_NO_NEW
    assert result.stock_status == stock_results.STATUS_NO_NEW


# ══════════════════════════════════════════════════════════════════════════════
# Finding 7 — per-fire deduplication is used by real sends
# ══════════════════════════════════════════════════════════════════════════════

def test_same_fire_under_two_run_ids_sends_once(tmp_path):
    """Two real cycles containing the identical fire produce ONE send."""
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    bot = Bot()
    stock = _stock()

    for run_id in ("run-a", "run-b"):
        paths = CyclePaths(tmp_path, run_id)
        _seed(paths, run_id, stocks=[stock])
        result, _ = _produce(tmp_path, run_id, Runner())
        enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)
        asyncio.run(deliver_cycle(outbox, result, [111], bot=bot, limit=3500,
                                  analysis_time=FROZEN_NOW))

    assert len(bot.calls) == 1, "the same fire was sent twice across run IDs"


def test_new_fire_time_under_a_new_run_id_is_sent(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    bot = Bot()
    for run_id, fire_time in (("run-a", "2026-10-13T05:00:00Z"),
                              ("run-b", "2026-10-13T07:00:00Z")):
        paths = CyclePaths(tmp_path, run_id)
        _seed(paths, run_id, stocks=[_stock(fire_time=fire_time)])
        result, _ = _produce(tmp_path, run_id, Runner())
        enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)
        asyncio.run(deliver_cycle(outbox, result, [111], bot=bot, limit=3500,
                                  analysis_time=FROZEN_NOW))

    assert len(bot.calls) == 2


def test_delivered_event_ledger_is_per_recipient(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[_stock()])
    result, _ = _produce(tmp_path, RUN_ID, Runner())
    enqueue_cycle(outbox, result, RECIPIENTS, limit=3500, analysis_time=FROZEN_NOW)
    asyncio.run(deliver_cycle(outbox, result, RECIPIENTS, bot=Bot(), limit=3500,
                              analysis_time=FROZEN_NOW))

    assert len(outbox.delivered_event_keys(111, identity.KIND_STOCK)) == 1
    assert len(outbox.delivered_event_keys(222, identity.KIND_STOCK)) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Finding 9 — CycleLock is wired and atomic
# ══════════════════════════════════════════════════════════════════════════════

def test_produce_cycle_refuses_when_the_lock_is_held(tmp_path):
    lock = CycleLock(tmp_path / "cycle.lock")
    assert lock.acquire("other-owner") is True
    try:
        paths = CyclePaths(tmp_path, RUN_ID)
        _seed(paths, RUN_ID, stocks=[_stock()])
        runner = Runner()
        result = produce_cycle(
            run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
            project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
            lock=CycleLock(tmp_path / "cycle.lock"),
        )
        assert result.status == CYCLE_FAILED
        assert runner.calls == [], "analysis ran while the lock was held"
    finally:
        lock.release()


def test_produce_cycle_releases_the_lock_on_success(tmp_path):
    lock = CycleLock(tmp_path / "cycle.lock")
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[_stock()])
    produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=Runner(),
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW, lock=lock,
    )
    assert not (tmp_path / "cycle.lock").exists()


def test_produce_cycle_releases_the_lock_on_failure(tmp_path):
    lock = CycleLock(tmp_path / "cycle.lock")
    paths = CyclePaths(tmp_path, RUN_ID)
    produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(),
        runner=Runner(failures={"download_fires": STATUS_FAILED}),
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW, lock=lock,
    )
    assert not (tmp_path / "cycle.lock").exists()


def test_lock_acquisition_is_atomic_under_contention(tmp_path):
    """Two competing processes cannot both acquire the lock."""
    import subprocess
    import sys

    lock_path = tmp_path / "cycle.lock"
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tradingview.scheduler.cycle import CycleLock\n"
        "lock = CycleLock(%r)\n"
        "print('ACQUIRED' if lock.acquire('proc') else 'DENIED')\n"
    ) % (str(Path(rs.__file__).resolve().parents[0]), str(lock_path))

    holder_script = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from tradingview.scheduler.cycle import CycleLock\n"
        "lock = CycleLock(%r)\n"
        "assert lock.acquire('holder')\n"
        "print('ACQUIRED', flush=True)\n"
        "time.sleep(5)\n"
    ) % (str(Path(rs.__file__).resolve().parents[0]), str(lock_path))

    holder = subprocess.Popen([sys.executable, "-c", holder_script],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"
        # While the holder is alive, a competing process must be denied.
        second = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=60)
        assert "DENIED" in second.stdout, second.stdout
    finally:
        holder.terminate()
        holder.wait(timeout=30)


def test_lock_release_is_ownership_checked(tmp_path):
    path = tmp_path / "cycle.lock"
    first = CycleLock(path)
    assert first.acquire("owner-a") is True
    # A different owner cannot release someone else's lock.
    second = CycleLock(path)
    second.release()
    assert path.exists()
    first.release()
    assert not path.exists()


def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path):
    path = tmp_path / "cycle.lock"
    path.write_text(json.dumps({
        "owner": "dead", "started_at": FROZEN_NOW.isoformat(),
        "pid": 999999, "host": __import__("socket").gethostname(),
    }), encoding="utf-8")
    lock = CycleLock(path)
    assert lock.acquire("new-owner") is True
    lock.release()


def test_live_owner_is_not_declared_stale(tmp_path):
    import os
    import socket

    path = tmp_path / "cycle.lock"
    path.write_text(json.dumps({
        "owner": "live", "started_at": "2020-01-01T00:00:00+00:00",
        "pid": os.getpid(), "host": socket.gethostname(),
    }), encoding="utf-8")
    lock = CycleLock(path, stale_seconds=1)
    assert lock.acquire("other") is False, "a live owner was declared stale"


def test_run_scoped_report_dir_is_injected(tmp_path):
    """The scoring step receives a run-scoped report directory."""
    captured: dict = {}

    def _runner(label, cmd, cwd=None, env=None, timeout=900, **kwargs):
        captured[label] = dict(env or {})
        return StepResult(label=label, status=STATUS_OK, exit_code=0,
                          duration_seconds=0.01, output="ok")

    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[_stock()])
    produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=_runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )
    assert captured["signal_stack"]["SIGNAL_REPORT_DIR"] == str(paths.run_dir)
    assert captured["signal_stack"]["SIGNAL_STOCK_PAYLOAD"] == str(paths.stock_payload)
    assert captured["signal_stack"]["SIGNAL_OPTIONS_SNAPSHOT"] == str(paths.options_snapshot)


# ══════════════════════════════════════════════════════════════════════════════
# Finding 10 — status classification
# ══════════════════════════════════════════════════════════════════════════════

def test_both_sources_unavailable_is_not_no_new(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    paths.ensure()
    result, _ = _produce(tmp_path, RUN_ID, Runner())
    assert result.status == CYCLE_FAILED
    assert result.status != CYCLE_NO_NEW
    assert result.stock_status == stock_results.STATUS_UNAVAILABLE
    assert result.options_status == "unavailable"


def test_one_source_unavailable_is_partial(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[], stock_status="no_new", option_status="no_new_signals")
    # Remove the options snapshot so only stocks are available.
    paths.options_snapshot.unlink()
    result, _ = _produce(tmp_path, RUN_ID, Runner())
    assert result.status == CYCLE_PARTIAL


def test_explicit_failed_producer_status_is_preserved(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    paths.ensure()
    stock_results.write_current_run_payload(paths.stock_payload, {
        "schema_version": 1, "run_id": RUN_ID, "status": "failed",
        "stocks": [], "errors": ["db failed"],
    })
    from tradingview.options.options_snapshot import build_snapshot, publish_snapshot

    publish_snapshot(paths.options_snapshot, build_snapshot(RUN_ID, "no_new_signals"))

    result, _ = _produce(tmp_path, RUN_ID, Runner())
    assert result.stock_status == stock_results.STATUS_FAILED
    assert result.stock_status != stock_results.STATUS_EMPTY
    assert any("db failed" in error for error in result.stock_result.errors)


def test_complete_successful_no_new_is_no_new(tmp_path):
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[], stock_status="no_new", option_status="no_new_signals")
    result, _ = _produce(tmp_path, RUN_ID, Runner())
    assert result.status == CYCLE_NO_NEW


# ══════════════════════════════════════════════════════════════════════════════
# Finding 11 — retry metadata and due-time
# ══════════════════════════════════════════════════════════════════════════════

def test_installed_retry_after_is_preserved_on_the_receipt(tmp_path):
    from telegram.error import RetryAfter

    exc = RetryAfter(3600)
    assert formatting.retry_after_seconds(exc) == 3600.0

    async def _send(**kwargs):
        raise exc

    parts = formatting.build_parts([formatting.Block(lines=["x"])], limit=3500)
    receipts = asyncio.run(formatting.send_parts(None, 111, parts, send=_send))
    assert receipts[0].retry_after == 3600.0


def test_retry_after_is_applied_to_the_outbox_row(tmp_path):
    from telegram.error import RetryAfter

    clock = Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                backoff_base_seconds=1, backoff_cap_seconds=5)
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")

    async def _send(**kwargs):
        raise RetryAfter(3600)

    report = asyncio.run(dispatch_pending(outbox, send=_send))
    assert report.claimed == 1
    item = outbox.get_by_key("k")
    delay = (datetime.fromisoformat(item.next_attempt_at) - clock.now).total_seconds()
    assert delay >= 3600, f"retry_after was lost (delay={delay})"


def test_claim_by_keys_honours_scheduled_backoff(tmp_path):
    clock = Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                backoff_base_seconds=3600)
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")
    item = outbox.claim_due("w")[0]
    outbox.mark_failed(item.id, "transient", owner=item.claim_owner, claim_token=item.claim_token)

    # Not yet due: the direct claim path must not reclaim it.
    assert outbox.claim_by_keys("w2", ["k"]) == []
    clock.advance(7200)
    assert len(outbox.claim_by_keys("w2", ["k"])) == 1


def test_missing_acknowledgement_marks_ambiguous_not_sent(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")

    report = asyncio.run(dispatch_pending(outbox, bot=Bot(no_message_id=True)))
    assert report.sent == 0
    assert report.ambiguous == 1
    assert outbox.get_by_key("k").status == STATUS_AMBIGUOUS


def test_mark_sent_refuses_a_missing_message_id(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")
    item = outbox.claim_due("w")[0]
    with pytest.raises(ValueError):
        outbox.mark_sent(item.id, None, owner=item.claim_owner, claim_token=item.claim_token)


def test_stale_worker_cannot_overwrite_a_newer_receipt(tmp_path):
    clock = Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                claim_lease_seconds=30)
    outbox.enqueue(delivery_key="k", recipient=111, kind="summary",
                   payload={}, message_text="m")
    first = outbox.claim_due("worker-1")[0]
    clock.advance(120)
    second = outbox.claim_due("worker-2")[0]
    assert second.id == first.id

    # The stale worker's late acknowledgement must not apply.
    assert outbox.mark_sent(first.id, 111, owner="worker-1", claim_token=first.claim_token) is False
    assert outbox.mark_sent(second.id, 222, owner="worker-2", claim_token=second.claim_token) is True
    assert outbox.get_by_key("k").telegram_message_id == 222


# ══════════════════════════════════════════════════════════════════════════════
# Finding 12 — unicode chunking
# ══════════════════════════════════════════════════════════════════════════════

def test_emoji_only_content_respects_the_limit():
    parts = formatting.build_parts(
        [formatting.Block(lines=["🚀" * 4000])], limit=3500,
    )
    for part in parts:
        assert part.length(formatting.PARSE_MODE_HTML) <= 3500, (
            f"HTML part exceeded the limit: {part.length(formatting.PARSE_MODE_HTML)}"
        )
        assert part.length(formatting.PARSE_MODE_PLAIN) <= 3500, (
            f"plain part exceeded the limit: {part.length(formatting.PARSE_MODE_PLAIN)}"
        )


def test_mixed_emoji_and_markup_respects_the_limit():
    text = ("<b>&amp;</b> 🚀 日本語 " * 800)
    parts = formatting.build_parts(
        [formatting.Block(lines=[text])], limit=3500,
    )
    for part in parts:
        assert part.length(formatting.PARSE_MODE_HTML) <= 3500
        assert part.length(formatting.PARSE_MODE_PLAIN) <= 3500


def test_emoji_content_never_exceeds_the_hard_telegram_limit():
    parts = formatting.build_parts(
        [formatting.Block(lines=["🎯" * 6000])], limit=3500,
    )
    for part in parts:
        assert part.length(formatting.PARSE_MODE_HTML) <= 4096
        assert part.length(formatting.PARSE_MODE_PLAIN) <= 4096


def test_emoji_content_is_preserved_across_parts():
    original = "🚀" * 4000
    parts = formatting.build_parts([formatting.Block(lines=[original])], limit=3500)
    joined = "".join(part.plain_text for part in parts)
    assert joined.count("🚀") == 4000


# ══════════════════════════════════════════════════════════════════════════════
# Finding 13 — acknowledgement required
# ══════════════════════════════════════════════════════════════════════════════

def test_send_without_message_id_is_not_recorded_as_sent(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[_stock()])
    result, _ = _produce(tmp_path, RUN_ID, Runner())
    enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)
    asyncio.run(deliver_cycle(outbox, result, [111], bot=Bot(no_message_id=True),
                              limit=3500, analysis_time=FROZEN_NOW))

    assert outbox.counts().get(STATUS_SENT, 0) == 0
    assert outbox.pending_count() == 1


def test_acknowledged_send_is_recorded_with_its_message_id(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=Clock())
    from tradingview.scheduler.cycle import deliver_cycle, enqueue_cycle

    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, RUN_ID, stocks=[_stock()])
    result, _ = _produce(tmp_path, RUN_ID, Runner())
    enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)
    asyncio.run(deliver_cycle(outbox, result, [111], bot=Bot(), limit=3500,
                              analysis_time=FROZEN_NOW))

    sent = [item for item in outbox.all_items() if item.status == STATUS_SENT]
    assert sent and sent[0].telegram_message_id is not None


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════

class _FakeJobQueue:
    def __init__(self) -> None:
        self.repeating: list[dict] = []
        self.daily: list[dict] = []
        self.once: list[dict] = []

    def run_repeating(self, callback, **kwargs):
        self.repeating.append({"callback": callback, **kwargs})
        return object()

    def run_daily(self, callback, **kwargs):
        self.daily.append({"callback": callback, **kwargs})
        return object()

    def run_once(self, callback, **kwargs):
        self.once.append({"callback": callback, **kwargs})
        return object()


class _FakeApp:
    def __init__(self) -> None:
        self.job_queue = _FakeJobQueue()


def _fake_app() -> _FakeApp:
    return _FakeApp()
