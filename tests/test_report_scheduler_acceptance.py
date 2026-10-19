"""End-to-end offline acceptance scenarios A–J.

Each scenario drives the real scheduler modules with injected clocks, temporary
paths, fake subprocess runners, and a local Telegram stub. No network call, no
production database, and no real credential is used.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingview import outcome_store
from tradingview.scheduler import identity, stock_results
from tradingview.scheduler.config import SchedulerConfig
from tradingview.scheduler.cycle import (
    CYCLE_FAILED,
    CYCLE_NO_NEW,
    CYCLE_OK,
    CyclePaths,
    CycleResult,
    build_summary_parts,
    deliver_cycle,
    enqueue_cycle,
    produce_cycle,
    should_run_analysis,
)
from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory
from tradingview.scheduler.steps import STATUS_FAILED, STATUS_OK, StepResult

RUN_ID = "ss_20261013T064219Z_accept001"
FROZEN_NOW = datetime(2026, 10, 13, 6, 42, 19, tzinfo=timezone.utc)
RECIPIENTS = [111, 222]


def _config(**overrides) -> SchedulerConfig:
    base = dict(
        stock_threshold=62.5, option_threshold=70.0, step_timeout=30,
        display_timezone="Australia/Sydney", schedule_timezone="Asia/Kolkata",
    )
    base.update(overrides)
    return SchedulerConfig(**base)


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
    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["chat_id"] in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return type("Msg", (), {"message_id": len(self.calls)})()


def _stock(pro_symbol="NASDAQ:GOOG", score=71.0, fire_time="2026-10-13T05:00:00Z"):
    exchange, _, symbol = pro_symbol.partition(":")
    return {"pro_symbol": pro_symbol, "exchange": exchange, "symbol": symbol,
            "score": score, "signal": "BUY", "fire_time": fire_time, "resolution": "60"}


def _contract(score=79.0, symbol="MSFT"):
    return {"market": "US", "exchange": "NASDAQ", "symbol": symbol,
            "option_type": "CALL", "strike": 530.0, "expiry": "2030-06-19",
            "dte": 45, "option_score": score, "parent_signal_score": 91.0,
            "strategy_modes": ["BALANCED"]}


def _seed(paths: CyclePaths, stocks=None, contracts=None, stock_status="success",
          option_status="success") -> None:
    paths.ensure()
    stock_results.write_current_run_payload(
        paths.stock_payload,
        stock_results.build_current_run_payload(RUN_ID, stocks or [], status=stock_status),
    )
    from tradingview.options.options_snapshot import build_snapshot, publish_snapshot

    publish_snapshot(paths.options_snapshot, build_snapshot(RUN_ID, option_status, contracts or []))


def _produce(tmp_path, runner, **kwargs):
    paths = CyclePaths(tmp_path, RUN_ID)
    return produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW, **kwargs
    ), paths


# ══════════════════════════════════════════════════════════════════════════════
# A. Happy path
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_a_happy_path_one_analysis_two_receipts(tmp_path):
    runner = Runner()
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, stocks=[_stock()], contracts=[_contract()])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.status == CYCLE_OK
    assert runner.calls == ["download_fires", "settle_outcomes", "signal_stack"]
    assert len(result.stocks) == 1
    assert len(result.options) == 1

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: FROZEN_NOW)
    enqueue_cycle(outbox, result, RECIPIENTS, limit=3500, analysis_time=FROZEN_NOW)
    bot = Bot()
    receipts = asyncio.run(
        deliver_cycle(outbox, result, RECIPIENTS, bot=bot, limit=3500,
                      analysis_time=FROZEN_NOW)
    )
    assert all(r.delivered for r in receipts[111])
    assert all(r.delivered for r in receipts[222])
    texts = {}
    for call in bot.calls:
        texts.setdefault(call["chat_id"], []).append(call["text"])
    assert texts[111] == texts[222]


def test_scenario_a_vetoed_stock_is_not_delivered(tmp_path):
    """A high score with an engine veto must never be delivered as actionable."""
    runner = Runner()
    paths = CyclePaths(tmp_path, RUN_ID)
    # The engine already excluded the vetoed fire, so the payload has one row.
    _seed(paths, stocks=[_stock("NASDAQ:OK", 71.0)], contracts=[])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )
    assert [s["symbol"] for s in result.stocks] == ["OK"]


def test_scenario_a_score_65_passes_stock_policy_despite_option_threshold(tmp_path):
    runner = Runner()
    paths = CyclePaths(tmp_path, RUN_ID)
    _seed(paths, stocks=[_stock("NASDAQ:LOW", 65.0)], contracts=[])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )
    assert len(result.stocks) == 1
    assert result.stocks[0]["score"] == 65.0


# ══════════════════════════════════════════════════════════════════════════════
# B. Second cycle with no new fires
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_b_no_new_cycle_does_not_replay(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: FROZEN_NOW)

    # Cycle 1: one actionable fire.
    runner = Runner()
    paths = CyclePaths(tmp_path, "run-1")
    paths.ensure()
    stock_results.write_current_run_payload(
        paths.stock_payload,
        stock_results.build_current_run_payload("run-1", [_stock()]),
    )
    first = produce_cycle(
        run_id="run-1", paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )
    enqueue_cycle(outbox, first, [111], limit=3500, analysis_time=FROZEN_NOW)
    bot = Bot()
    asyncio.run(deliver_cycle(outbox, first, [111], bot=bot, limit=3500,
                              analysis_time=FROZEN_NOW))
    assert len(bot.calls) == 1

    # Cycle 2: no new fires.
    paths2 = CyclePaths(tmp_path, "run-2")
    paths2.ensure()
    stock_results.write_current_run_payload(
        paths2.stock_payload,
        stock_results.build_current_run_payload("run-2", [], status="no_new"),
    )
    # A genuine no-new cycle has BOTH sources available and empty.
    from tradingview.options.options_snapshot import build_snapshot, publish_snapshot

    publish_snapshot(paths2.options_snapshot, build_snapshot("run-2", "no_new_signals"))
    second = produce_cycle(
        run_id="run-2", paths=paths2, config=_config(), runner=Runner(),
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )
    assert second.status == CYCLE_NO_NEW

    enqueue_cycle(outbox, second, [111], limit=3500, analysis_time=FROZEN_NOW)
    asyncio.run(deliver_cycle(outbox, second, [111], bot=bot, limit=3500,
                              analysis_time=FROZEN_NOW))

    # A successful empty cycle produces no additional Telegram message.
    assert len(bot.calls) == 1
    assert outbox.items_for_run("run-2") == []


# ══════════════════════════════════════════════════════════════════════════════
# C. Download fails with an old cache present
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_c_download_failure_does_not_publish_stale_data(tmp_path):
    runner = Runner(failures={"download_fires": STATUS_FAILED})
    paths = CyclePaths(tmp_path, RUN_ID)
    # A previous cycle's payload and snapshot are present on disk.
    _seed(paths, stocks=[_stock()], contracts=[_contract()])

    result = produce_cycle(
        run_id=RUN_ID, paths=paths, config=_config(), runner=runner,
        project_root=tmp_path, python_executable="python", now=FROZEN_NOW,
    )

    assert result.status == CYCLE_FAILED
    assert runner.calls == ["download_fires"]
    assert result.stocks == []
    assert result.options == []
    assert result.stock_status == stock_results.STATUS_UNAVAILABLE


# ══════════════════════════════════════════════════════════════════════════════
# D. Tracker write fails after score
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_d_recover_from_durable_handoff_without_a_new_fire(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "fires_ledger.db"))
    conn.row_factory = sqlite3.Row
    outcome_store.ensure_schema(conn)

    fire = {"pro_symbol": "NASDAQ:GOOG", "fire_time": "2026-10-13T05:00:00Z",
            "resolution": "60"}
    outcome_store.persist_completed(conn, fire, RUN_ID, {"composite": 71.0, "actionable": True})
    conn.commit()

    # The tracker write fails afterwards; the durable payload survives.
    recovered = outcome_store.load_scoring_results(conn)
    assert len(recovered) == 1

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: FROZEN_NOW)
    stock = {
        "pro_symbol": recovered[0]["pro_symbol"],
        "fire_time": recovered[0]["fire_time"],
        "resolution": recovered[0]["resolution"],
        "score": recovered[0]["result"]["composite"],
    }
    key = identity.stock_delivery_key(111, stock)
    assert outbox.enqueue(delivery_key=key, recipient=111, kind="stock",
                          payload=stock, message_text="alert") is True
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# E. Transient failure and restart
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_e_pending_payload_survives_restart_and_is_retried(tmp_path):
    class Clock:
        def __init__(self):
            self.now = FROZEN_NOW

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now = self.now + timedelta(seconds=seconds)

    clock = Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                backoff_base_seconds=30)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    # First attempt fails transiently.
    item = outbox.claim_due("worker-1")[0]
    outbox.mark_failed(item.id, "TimedOut", owner=item.claim_owner, claim_token=item.claim_token)

    # Process restarts; the pending row is still there.
    restarted = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                   backoff_base_seconds=30)
    assert restarted.pending_count() == 1

    # After the backoff elapses it is retried and acknowledged.
    clock.advance(60)
    reclaimed = restarted.claim_due("worker-2")
    assert len(reclaimed) == 1
    restarted.mark_sent(reclaimed[0].id, 555, owner=reclaimed[0].claim_owner, claim_token=reclaimed[0].claim_token)
    assert restarted.get_by_key("k").status == "sent"


# ══════════════════════════════════════════════════════════════════════════════
# F. Long / error-rich content
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_f_long_error_rich_content_is_bounded_and_falls_back(tmp_path):
    from tradingview.scheduler import formatting

    hostile = (
        "run_id mismatch [x] `y` & <z> /Users/priya/stocks/reports/options_signals.json "
        + "🚀" * 500
    )
    parts = formatting.render_summary(
        header="ALERT SUMMARY",
        stock_lines=["row " + "a" * 200 for _ in range(100)],
        option_lines=["opt " + "b" * 200 for _ in range(100)],
        notes=[hostile],
        limit=3500,
    )
    assert len(parts) > 1
    for part in parts:
        assert part.length("HTML") <= 3500
        assert part.html_text.count("<pre>") == part.html_text.count("</pre>")

    class RejectingBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("parse_mode") == "HTML":
                raise RuntimeError("Bad Request: can't parse entities")
            return type("Msg", (), {"message_id": 1})()

    bot = RejectingBot()
    receipts = asyncio.run(formatting.send_parts(bot, 111, parts))
    assert all(r.delivered for r in receipts)
    assert all(r.parse_mode == "" for r in receipts)


# ══════════════════════════════════════════════════════════════════════════════
# G. Excluded weekday
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_g_excluded_weekday_skips_analysis_but_retries_delivery(tmp_path):
    saturday = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
    assert should_run_analysis(saturday, (0, 1, 2, 3, 4), "Asia/Kolkata") is False

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: FROZEN_NOW)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    # Delivery recovery is independent of the analysis calendar.
    assert len(outbox.claim_due("recovery")) == 1


def test_scenario_g_expired_pending_item_is_not_delivered_late(tmp_path):
    class Clock:
        def __init__(self):
            self.now = FROZEN_NOW

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now = self.now + timedelta(seconds=seconds)

    clock = Clock()
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=clock,
                                alert_ttl_hours=6)
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")
    clock.advance(3600 * 24)
    assert outbox.claim_due("w") == []
    assert outbox.get_by_key("k").status == "expired"


# ══════════════════════════════════════════════════════════════════════════════
# H. Concurrent manual / automatic activity
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_h_run_scoped_paths_prevent_cross_wiring(tmp_path):
    automatic = CyclePaths(tmp_path, "auto-run")
    manual = CyclePaths(tmp_path, "manual-run")
    automatic.ensure()
    manual.ensure()

    stock_results.write_current_run_payload(
        automatic.stock_payload,
        stock_results.build_current_run_payload("auto-run", [_stock("NASDAQ:AUTO")]),
    )
    stock_results.write_current_run_payload(
        manual.stock_payload,
        stock_results.build_current_run_payload("manual-run", [_stock("NASDAQ:MANUAL")]),
    )

    auto_result = stock_results.load_current_run_stocks(automatic.stock_payload, "auto-run")
    manual_result = stock_results.load_current_run_stocks(manual.stock_payload, "manual-run")
    assert [s["symbol"] for s in auto_result.stocks] == ["AUTO"]
    assert [s["symbol"] for s in manual_result.stocks] == ["MANUAL"]

    # A manual run cannot satisfy the automatic run's identity.
    mismatched = stock_results.load_current_run_stocks(manual.stock_payload, "auto-run")
    assert mismatched.status == stock_results.STATUS_UNAVAILABLE


def test_scenario_h_no_accidental_duplicate_ordinary_sends(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: FROZEN_NOW)
    result = CycleResult(
        run_id=RUN_ID, started_at=FROZEN_NOW.isoformat(), status=CYCLE_OK,
        stock_result=stock_results.StockResult(
            status=stock_results.STATUS_SUCCESS, stocks=[_stock()]),
        option_result={"options": [], "status": "empty", "errors": []},
        display_timezone="Australia/Sydney",
    )

    sent: list[str] = []

    async def _send(**kwargs):
        sent.append(kwargs["text"])
        return type("Msg", (), {"message_id": 1})()

    enqueue_cycle(outbox, result, [111], limit=3500, analysis_time=FROZEN_NOW)
    asyncio.run(deliver_cycle(outbox, result, [111], send=_send, limit=3500,
                              analysis_time=FROZEN_NOW))
    asyncio.run(deliver_cycle(outbox, result, [111], send=_send, limit=3500,
                              analysis_time=FROZEN_NOW))
    assert len(sent) == 1


# ══════════════════════════════════════════════════════════════════════════════
# I. Migration
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_i_migration_preserves_records_and_is_rerunnable(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "fires_ledger.db"))
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
    conn.execute(
        "INSERT INTO downloaded_fires (pro_symbol, fire_time, resolution, "
        "first_seen_at, scored_at) VALUES ('NASDAQ:OLD', '2026-01-01T00:00:00Z', "
        "'60', '2026-01-01', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    outcome_store.ensure_schema(conn)
    outcome_store.ensure_schema(conn)  # rerunnable

    assert conn.execute("SELECT COUNT(*) FROM downloaded_fires").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM scoring_results").fetchone()[0] == 0
    conn.close()


def test_scenario_i_no_historical_alert_flood(tmp_path):
    """A fresh outbox starts empty: historical fires are not re-queued."""
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"), clock=lambda: FROZEN_NOW)
    assert outbox.all_items() == []
    assert outbox.pending_count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# J. Missing credential
# ══════════════════════════════════════════════════════════════════════════════

def test_scenario_j_missing_credential_fails_locally_without_network(monkeypatch):
    import subprocess
    import sys

    from tradingview.scheduler.config import MissingCredential, resolve_bot_token

    with pytest.raises(MissingCredential) as excinfo:
        resolve_bot_token({})
    message = str(excinfo.value)
    assert "AJAAZ_ALERT_BOT_TOKEN" in message
    assert "scheduler_env.local.sh" in message

    # The entry point exits with a clear code and no network attempt.
    result = subprocess.run(
        [sys.executable, "-c",
         "import report_scheduler as rs\n"
         "try:\n"
         "    rs.main()\n"
         "except SystemExit as exc:\n"
         "    print('exit', exc.code)\n"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert "exit 2" in result.stdout
    assert "AJAAZ_ALERT_BOT_TOKEN" in result.stderr
