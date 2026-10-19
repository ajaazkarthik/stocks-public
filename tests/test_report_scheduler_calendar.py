"""Offline tests for weekday calendar enforcement (Issue 8).

The interval job previously ignored ``SCHEDULER_DAYS`` entirely. These tests
pin the pure ``should_run_analysis`` helper, the job registration, and the
pause/override semantics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tradingview.scheduler.config import InvalidConfiguration, parse_days
from tradingview.scheduler.cycle import should_run_analysis

WEEKDAYS = (0, 1, 2, 3, 4)  # Mon–Fri


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# Pure calendar helper
# ══════════════════════════════════════════════════════════════════════════════

def test_friday_is_permitted():
    # 2026-10-16 is a Friday.
    assert should_run_analysis(_utc(2026, 10, 16, 6, 0), WEEKDAYS, "Asia/Kolkata") is True


def test_saturday_is_excluded():
    # 2026-10-17 is a Saturday.
    assert should_run_analysis(_utc(2026, 10, 17, 6, 0), WEEKDAYS, "Asia/Kolkata") is False


def test_sunday_is_excluded_by_default():
    assert should_run_analysis(_utc(2026, 10, 18, 6, 0), WEEKDAYS, "Asia/Kolkata") is False


def test_configurable_sunday_is_permitted():
    assert should_run_analysis(_utc(2026, 10, 18, 6, 0), (6,), "Asia/Kolkata") is True


def test_utc_local_date_boundary_uses_schedule_timezone():
    """A UTC Saturday can still be a Friday in the schedule timezone."""
    # 2026-10-16 23:00 UTC == 2026-10-17 04:30 IST (Saturday) → excluded.
    assert should_run_analysis(_utc(2026, 10, 16, 23, 0), WEEKDAYS, "Asia/Kolkata") is False
    # 2026-10-16 17:00 UTC == 2026-10-16 22:30 IST (Friday) → permitted.
    assert should_run_analysis(_utc(2026, 10, 16, 17, 0), WEEKDAYS, "Asia/Kolkata") is True


def test_sydney_schedule_timezone_shifts_the_boundary():
    # 2026-10-16 15:00 UTC == 2026-10-17 02:00 AEDT (Saturday) → excluded.
    assert should_run_analysis(_utc(2026, 10, 16, 15, 0), WEEKDAYS, "Australia/Sydney") is False
    # 2026-10-16 13:00 UTC == 2026-10-17 00:00 AEDT (Saturday) → excluded.
    assert should_run_analysis(_utc(2026, 10, 16, 13, 0), WEEKDAYS, "Australia/Sydney") is False
    # 2026-10-16 12:00 UTC == 2026-10-16 23:00 AEDT (Friday) → permitted.
    assert should_run_analysis(_utc(2026, 10, 16, 12, 0), WEEKDAYS, "Australia/Sydney") is True


def test_dst_transition_does_not_shift_the_weekday():
    """Sydney DST starts 2026-10-04; the weekday must be unaffected."""
    before = _utc(2026, 10, 2, 6, 0)   # Friday, AEST
    after = _utc(2026, 10, 9, 6, 0)    # Friday, AEDT
    assert should_run_analysis(before, WEEKDAYS, "Australia/Sydney") is True
    assert should_run_analysis(after, WEEKDAYS, "Australia/Sydney") is True
    saturday = _utc(2026, 10, 10, 6, 0)
    assert should_run_analysis(saturday, WEEKDAYS, "Australia/Sydney") is False


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        should_run_analysis(datetime(2026, 10, 16, 6, 0), WEEKDAYS, "Asia/Kolkata")


def test_invalid_timezone_is_rejected():
    from tradingview.scheduler.timeutil import InvalidTimezone

    with pytest.raises(InvalidTimezone):
        should_run_analysis(_utc(2026, 10, 16, 6, 0), WEEKDAYS, "Not/AZone")


def test_empty_allowed_days_never_runs():
    assert should_run_analysis(_utc(2026, 10, 16, 6, 0), (), "Asia/Kolkata") is False


def test_all_seven_days_always_runs():
    for offset in range(7):
        moment = _utc(2026, 10, 12, 6, 0) + timedelta(days=offset)
        assert should_run_analysis(moment, tuple(range(7)), "Asia/Kolkata") is True


# ══════════════════════════════════════════════════════════════════════════════
# Configuration parsing
# ══════════════════════════════════════════════════════════════════════════════

def test_days_config_maps_to_python_weekdays():
    assert parse_days("1,2,3,4,5") == (0, 1, 2, 3, 4)
    assert parse_days("6,7") == (5, 6)


def test_invalid_days_config_is_rejected():
    with pytest.raises(InvalidConfiguration):
        parse_days("0")
    with pytest.raises(InvalidConfiguration):
        parse_days("monday")


# ══════════════════════════════════════════════════════════════════════════════
# Job registration and pause semantics
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


def _setup(monkeypatch, env: dict) -> _FakeApp:
    import report_scheduler as rs

    for key in ("SCHEDULER_EVERY_HOURS", "SCHEDULER_DAYS", "SCHEDULER_TIMEZONE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    app = _FakeApp()
    rs._setup_job_queue(app)
    return app


def test_interval_mode_registers_two_hour_interval_with_startup_delay(monkeypatch):
    app = _setup(monkeypatch, {"SCHEDULER_EVERY_HOURS": "2"})
    interval_jobs = [job for job in app.job_queue.repeating if job["name"] == "interval_run"]
    assert len(interval_jobs) == 1
    assert interval_jobs[0]["interval"] == 2 * 3600
    assert interval_jobs[0]["first"] == 60


def test_interval_mode_does_not_register_daily_jobs(monkeypatch):
    app = _setup(monkeypatch, {"SCHEDULER_EVERY_HOURS": "2"})
    names = {job["name"] for job in app.job_queue.daily}
    assert "morning_run" not in names
    assert "evening_run" not in names


def test_daily_mode_still_registers_morning_and_evening(monkeypatch):
    app = _setup(monkeypatch, {})
    names = {job["name"] for job in app.job_queue.daily}
    assert {"morning_run", "evening_run"} <= names


def test_premium_job_is_preserved(monkeypatch):
    app = _setup(monkeypatch, {"SCHEDULER_EVERY_HOURS": "2"})
    names = {job["name"] for job in app.job_queue.repeating}
    assert "options_premium_update" in names


def test_outbox_dispatch_job_is_registered(monkeypatch):
    app = _setup(monkeypatch, {"SCHEDULER_EVERY_HOURS": "2"})
    names = {job["name"] for job in app.job_queue.repeating}
    assert "outbox_dispatch" in names


def test_startup_recovery_job_is_registered(monkeypatch):
    app = _setup(monkeypatch, {"SCHEDULER_EVERY_HOURS": "2"})
    names = {job["name"] for job in app.job_queue.once}
    assert "startup_recovery" in names


def test_interval_job_skips_analysis_on_an_excluded_day(monkeypatch):
    """The interval callback must consult the calendar before analysing."""
    import asyncio

    import report_scheduler as rs

    monkeypatch.setenv("SCHEDULER_DAYS", "1,2,3,4,5")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setattr(rs, "_scheduler_paused", False)

    calls: list[str] = []

    async def _fake_broadcast(bot):
        calls.append("broadcast")

    monkeypatch.setattr(rs, "_run_signal_cycle_broadcast", _fake_broadcast)
    monkeypatch.setattr(
        rs, "_now_utc", lambda: datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
    )

    context = type("Ctx", (), {"bot": object()})()
    asyncio.run(rs._job_interval_run(context))
    assert calls == [], "analysis ran on an excluded weekday"


def test_interval_job_runs_analysis_on_a_permitted_day(monkeypatch):
    import asyncio

    import report_scheduler as rs

    monkeypatch.setenv("SCHEDULER_DAYS", "1,2,3,4,5")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setattr(rs, "_scheduler_paused", False)

    calls: list[str] = []

    async def _fake_broadcast(bot):
        calls.append("broadcast")

    monkeypatch.setattr(rs, "_run_signal_cycle_broadcast", _fake_broadcast)
    monkeypatch.setattr(
        rs, "_now_utc", lambda: datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
    )

    context = type("Ctx", (), {"bot": object()})()
    asyncio.run(rs._job_interval_run(context))
    assert calls == ["broadcast"]


def test_pause_stops_analysis(monkeypatch):
    import asyncio

    import report_scheduler as rs

    monkeypatch.setattr(rs, "_scheduler_paused", True)
    calls: list[str] = []

    async def _fake_broadcast(bot):
        calls.append("broadcast")

    monkeypatch.setattr(rs, "_run_signal_cycle_broadcast", _fake_broadcast)
    context = type("Ctx", (), {"bot": object()})()
    asyncio.run(rs._job_interval_run(context))
    assert calls == []


def test_pending_delivery_is_independent_of_analysis_permission(tmp_path):
    """Outbox recovery must not be gated by the weekday calendar."""
    from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory

    outbox = NotificationOutbox(sqlite_factory(tmp_path / "outbox.db"))
    outbox.enqueue(delivery_key="k", recipient=111, kind="stock",
                   payload={}, message_text="m")

    # Saturday: analysis is not permitted, but the pending item is still due.
    saturday = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
    assert should_run_analysis(saturday, WEEKDAYS, "Asia/Kolkata") is False
    assert outbox.pending_count() == 1
    assert len(outbox.claim_due("recovery")) == 1


def test_manual_override_is_documented_and_available(monkeypatch):
    """Manual commands keep their documented override behaviour."""
    import report_scheduler as rs

    source = (rs.__file__)
    text = open(source, encoding="utf-8").read()
    assert "cmd_run_morning" in text
    assert "cmd_alerts_now" in text
    # Manual handlers must not consult the weekday calendar.
    assert "should_run_analysis" not in text.split("async def cmd_run_morning")[1].split("async def cmd_run_evening")[0]
