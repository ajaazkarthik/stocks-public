"""Quiet successful jobs, readable recommendations, and visible failures."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import report_scheduler as rs
from tradingview.scheduler.cycle import CycleResult, build_summary_parts, enqueue_cycle, deliver_cycle
from tradingview.scheduler.outbox import NotificationOutbox, sqlite_factory
from tradingview.scheduler.stock_results import StockResult

NOW = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)

class Bot:
    def __init__(self):
        self.messages = []
    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.messages))

def cycle(stocks=(), options=(), **kwargs):
    return CycleResult(run_id="quiet-test", started_at=NOW.isoformat(),
                       display_timezone="Australia/Sydney",
                       stock_result=StockResult(status="success", stocks=list(stocks)),
                       option_result={"status": "disabled", "options": list(options)}, **kwargs)

def stock():
    return dict(pro_symbol="ASX:CLV", exchange="ASX", symbol="CLV", score=65.9,
                signal="SCR", direction="BUY", source="scanner", market="AU",
                session_date="2026-10-16", fire_time=NOW.isoformat(), resolution="D")

@pytest.mark.parametrize("status", ["ok", "no_new"])
def test_empty_success_never_queues_or_sends(tmp_path, status):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "queue.db"), clock=lambda: NOW)
    result = cycle(status=status, warnings=["Scanner AU: no_new (0 actionable)"])
    bot = Bot()
    assert enqueue_cycle(outbox, result, [111], limit=3500) == 0
    asyncio.run(deliver_cycle(outbox, result, [111], bot=bot, limit=3500))
    assert bot.messages == []
    assert outbox.all_items() == []

def test_phone_layout_keeps_contract_identity_and_escapes_dynamic_text():
    option = dict(exchange="NASDAQ", symbol="A&B<Z>", opt_type="CALL", strike=100,
                  expiry="2026-11-20", score=78, contract_id="TEST-CONTRACT")
    result = cycle([stock()], [option], warnings=["Scanner AU: success (1 actionable)"])
    parts = build_summary_parts(result, limit=3500, analysis_time=NOW)
    assert len(parts) == 1
    text = parts[0].html_text
    for expected in ["ASX:CLV · BUY", "Score 65.9/100", "A&amp;B&lt;Z&gt;", "CALL",
                     "Strike 100", "Expiry 2026-11-20", "TEST-CONTRACT"]:
        assert expected in text
    for forbidden in ["<pre>", "Run ", "status:", "disabled", "success", "SCR ", "No new"]:
        assert forbidden not in text


def test_scanner_freshness_failure_is_compact_and_not_duplicated():
    detail = ("scanner freshness rejected candidate(s): "
              "APA: $APA.AX: possibly delisted; no price data found (period=1d); "
              "CSL: $CSL.AX: possibly delisted; no price data found (period=1d)")
    result = cycle(status="failed", warnings=[f"Scanner AU: failed (0 actionable) — {detail}"])
    result.stock_result = StockResult(status="failed", stocks=[], errors=[f"AU: {detail}"])
    text = "\n".join(p.plain_text for p in build_summary_parts(result, limit=3500, analysis_time=NOW))
    assert text.count("Yahoo intraday price data unavailable") == 1
    assert "possibly delisted" not in text
    assert len(text) < 300
    assert result.stock_result.errors == [f"AU: {detail}"]


def test_compact_errors_keep_other_failures_and_markets():
    from tradingview.scheduler.formatting import compact_error_detail
    assert compact_error_detail("Options provider timeout") == "Options provider timeout"
    for market in ("US", "AU", "IN", "SG"):
        text = compact_error_detail(f"{market}: scanner freshness rejected candidate(s): X: stale bar")
        assert text.startswith(f"{market}:")
        assert "failed price freshness validation" in text

def test_partial_failure_alert_survives_recommendation_dedup(tmp_path):
    outbox = NotificationOutbox(sqlite_factory(tmp_path / "queue.db"), clock=lambda: NOW)
    bot = Bot()
    first = cycle([stock()])
    enqueue_cycle(outbox, first, [111], limit=3500)
    asyncio.run(deliver_cycle(outbox, first, [111], bot=bot, limit=3500))
    second = replace(first, run_id="later", status="partial", errors=["India scanner failed"])
    assert enqueue_cycle(outbox, second, [111], limit=3500) == 1
    asyncio.run(deliver_cycle(outbox, second, [111], bot=bot, limit=3500))
    assert len(bot.messages) == 2
    assert "India scanner failed" in bot.messages[-1]["text"]
    assert "ASX:CLV" not in bot.messages[-1]["text"]
    assert enqueue_cycle(outbox, second, [111], limit=3500) == 0

@pytest.mark.parametrize("rc", [0, 1])
def test_scheduled_premium_step_only_sends_on_failure(tmp_path, monkeypatch, rc):
    monkeypatch.setattr(rs, "_run_cmd", lambda *args: (rc, "bad <payload> & error"))
    bot = Bot()
    ok = asyncio.run(rs._run_step_scheduled(bot, 111, "Options Premium Update", ["fake"], tmp_path))
    assert ok is (rc == 0)
    assert len(bot.messages) == (0 if rc == 0 else 1)
    if rc:
        assert "failed (exit 1)" in bot.messages[0]["text"]
        assert "&lt;payload&gt;" in bot.messages[0]["text"]

def test_scheduled_step_exception_is_reported(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("launch failed")
    monkeypatch.setattr(rs, "_run_cmd", fail)
    bot = Bot()
    assert not asyncio.run(rs._run_step_scheduled(bot, 111, "Premium update", ["fake"], tmp_path))
    assert "launch failed" in bot.messages[0]["text"]


def test_scanner_failure_dedup_survives_restart_but_new_session_alerts(tmp_path):
    path = tmp_path / 'queue.db'
    outbox = NotificationOutbox(sqlite_factory(path), clock=lambda: NOW)
    first = cycle(status='failed', errors=['Yahoo provider timeout'])
    first.scanner_markets = {'AU': {'session_date': '2026-10-16', 'status': 'failed'}}
    assert enqueue_cycle(outbox, first, [111], limit=3500) == 1
    restarted = NotificationOutbox(sqlite_factory(path), clock=lambda: NOW)
    retry = replace(first, run_id='retry')
    assert enqueue_cycle(restarted, retry, [111], limit=3500) == 0
    assert enqueue_cycle(restarted, retry, [222], limit=3500) == 1
    changed = replace(first, run_id='other', errors=['TradingView provider timeout'])
    assert enqueue_cycle(restarted, changed, [111], limit=3500) == 1
    next_session = replace(first, run_id='next', scanner_markets={'AU': {'session_date': '2026-10-19'}})
    assert enqueue_cycle(restarted, next_session, [111], limit=3500) == 1
