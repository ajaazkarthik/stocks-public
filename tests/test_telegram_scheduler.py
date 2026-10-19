"""
tests/test_telegram_scheduler.py
==================================
Comprehensive tests for tradingview/telegram_scheduler.py

Tests cover:
  1. DedupDB — schema creation, TTL window, atomic insert, cleanup, thread safety
  2. AlertFormatter — all 3 table formats, MarkdownV2 escaping, chunking, empty states
  3. _parse_numeric / _parse_pct helpers — locale-aware, Indian + US formats (MEDIUM-8)
  4. _any_market_open — market hours gate (MEDIUM-9)
  5. TelegramScheduler.run_cycle() — with full subprocess + DB mocking
  6. Integration: dedup correctly skips re-sends; second cycle sends 0 alerts
  7. Dry-run flag: no Telegram send, tables printed to stdout
  8. Error-isolation: if options screener fails, stocks table still sends
  9. Empty-all gate: all 3 tables empty → no Telegram send at all (LOW-11)
 10. Market-hours gate: cycle skips silently when all markets closed (MEDIUM-9)
 11. Concurrent DedupDB writes (MEDIUM-7): two threads marking same key — only one wins
"""

import sqlite3
import sys
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

# ── Ensure tradingview package is importable ────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from tradingview.telegram_scheduler import (
    DedupDB,
    AlertFormatter,
    AlertRunner,
    TelegramScheduler,
    _parse_numeric,
    _parse_pct,
    _any_market_open,
    _DEDUP_TTL_HOURS,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def tmp_dedup(tmp_path: Path) -> DedupDB:
    """DedupDB backed by a temp file (isolated per test)."""
    return DedupDB(db_path=tmp_path / "test_dedup.db")


@pytest.fixture()
def fmt() -> AlertFormatter:
    return AlertFormatter()


SAMPLE_STOCKS = [
    {"symbol": "NASDAQ:NVDA", "score": 82.0, "signal": "BUY",  "company": "NVIDIA"},
    {"symbol": "ASX:BHP",     "score": 75.5, "signal": "BUY",  "company": "BHP Group"},
    {"symbol": "NSE:INFY",    "score": 63.0, "signal": "HOLD", "company": "Infosys"},
]

SAMPLE_SCREENER = [
    {"underlying": "NASDAQ:NVDA", "strike": 900.0, "expiry": "2026-06-20", "type": "CALL",
     "iv": 0.42, "tqs": 7.5, "dte": 33, "premium": 12.50},
    {"underlying": "ASX:BHP",     "strike": 45.0,  "expiry": "2026-06-20", "type": "PUT",
     "iv": 0.28, "tqs": 6.1, "dte": 33, "premium": 1.20},
]

SAMPLE_ANALYSIS = [
    {"symbol": "NASDAQ:NVDA", "type": "CALL", "strike": 900.0, "expiry": "2026-06-20",
     "premium": 12.50, "score": 68.0, "strategy": "CALL 20JUN2026",
     "recommendation": "BUY", "market": "US"},
]


# ═══════════════════════════════════════════════════════════════════════════
# 1. DedupDB tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDedupDB:

    def test_schema_created_on_init(self, tmp_dedup: DedupDB) -> None:
        """DedupDB creates the sent_alerts table on first use."""
        with tmp_dedup._connect() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sent_alerts'"
            ).fetchall()
        assert len(tables) == 1, "sent_alerts table must exist after DedupDB init"

    def test_new_key_not_sent(self, tmp_dedup: DedupDB) -> None:
        assert tmp_dedup.is_sent_recently("stock:NASDAQ:NVDA") is False

    def test_key_sent_after_mark(self, tmp_dedup: DedupDB) -> None:
        tmp_dedup.mark_sent_atomic("stock:NASDAQ:NVDA", "stock")
        assert tmp_dedup.is_sent_recently("stock:NASDAQ:NVDA") is True

    def test_mark_sent_atomic_returns_true_on_first(self, tmp_dedup: DedupDB) -> None:
        won = tmp_dedup.mark_sent_atomic("stock:ASX:BHP", "stock")
        assert won is True

    def test_mark_sent_atomic_returns_false_on_duplicate(self, tmp_dedup: DedupDB) -> None:
        tmp_dedup.mark_sent_atomic("stock:ASX:BHP", "stock")
        won_again = tmp_dedup.mark_sent_atomic("stock:ASX:BHP", "stock")
        assert won_again is False

    def test_ttl_window_respects_24h(self, tmp_dedup: DedupDB) -> None:
        """Key inserted 25h ago should NOT appear as recently sent (TTL=24h)."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with tmp_dedup._connect() as conn:
            conn.execute(
                "INSERT INTO sent_alerts (alert_key, alert_type, sent_at) VALUES (?, ?, ?)",
                ("stock:STALE:KEY", "stock", old_ts),
            )
            conn.commit()
        assert tmp_dedup.is_sent_recently("stock:STALE:KEY") is False

    def test_ttl_window_within_24h_is_sent(self, tmp_dedup: DedupDB) -> None:
        """Key inserted 2h ago should appear as recently sent (TTL=24h)."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with tmp_dedup._connect() as conn:
            conn.execute(
                "INSERT INTO sent_alerts (alert_key, alert_type, sent_at) VALUES (?, ?, ?)",
                ("stock:FRESH:KEY", "stock", recent_ts),
            )
            conn.commit()
        assert tmp_dedup.is_sent_recently("stock:FRESH:KEY") is True

    def test_cleanup_removes_old_entries(self, tmp_dedup: DedupDB) -> None:
        """cleanup_old() removes entries older than 24h."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with tmp_dedup._connect() as conn:
            conn.execute(
                "INSERT INTO sent_alerts (alert_key, alert_type, sent_at) VALUES (?, ?, ?)",
                ("stock:OLD:ONE", "stock", old_ts),
            )
            conn.commit()
        removed = tmp_dedup.cleanup_old()
        assert removed >= 1
        assert tmp_dedup.is_sent_recently("stock:OLD:ONE") is False

    def test_cleanup_keeps_recent_entries(self, tmp_dedup: DedupDB) -> None:
        """cleanup_old() does NOT remove entries from the last 24h."""
        tmp_dedup.mark_sent_atomic("stock:RECENT:ONE", "stock")
        tmp_dedup.cleanup_old()
        assert tmp_dedup.is_sent_recently("stock:RECENT:ONE") is True

    def test_thread_safe_concurrent_mark(self, tmp_dedup: DedupDB) -> None:
        """
        MEDIUM-7: Two threads marking the same key simultaneously — only one wins.
        No sqlite3 ProgrammingError should be raised (each call opens its own connection).
        """
        key = "stock:CONCURRENT:TEST"
        results: list[bool] = []
        errors: list[Exception] = []

        def _mark() -> None:
            try:
                won = tmp_dedup.mark_sent_atomic(key, "stock")
                results.append(won)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_mark) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
        # Exactly one thread should have won (INSERT OR IGNORE)
        assert results.count(True) == 1, "Only 1 thread should win the atomic insert"
        assert results.count(False) == 9, "All other threads should see duplicate"


# ═══════════════════════════════════════════════════════════════════════════
# 2. AlertFormatter tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAlertFormatter:

    def test_stocks_table_basic(self, fmt: AlertFormatter) -> None:
        msg = fmt.format_stocks_table(SAMPLE_STOCKS, skipped=0)
        assert "Stocks Actionable" in msg
        assert "NASDAQ:NVDA" in msg
        assert "ASX:BHP" in msg
        assert "3 new" in msg

    def test_stocks_table_empty_no_message(self, fmt: AlertFormatter) -> None:
        """Empty alerts + 0 skipped → always returns non-empty empty-notice string."""
        msg = fmt.format_stocks_table([], skipped=0)
        assert msg != ""
        assert "no new alerts" in msg.lower() or "no new" in msg.lower()

    def test_stocks_table_empty_with_skipped(self, fmt: AlertFormatter) -> None:
        """Empty alerts + some skipped → brief message (not an empty string)."""
        msg = fmt.format_stocks_table([], skipped=3)
        assert msg != ""
        assert "already sent" in msg

    def test_options_screener_table_basic(self, fmt: AlertFormatter) -> None:
        msg = fmt.format_options_screener_table(SAMPLE_SCREENER, skipped=1)
        assert "Options Screener" in msg
        assert "NASDAQ:NVDA" in msg
        assert "2 new" in msg
        assert "1 already sent" in msg

    def test_options_screener_empty(self, fmt: AlertFormatter) -> None:
        """Empty contracts + 0 skipped → always returns non-empty empty-notice string."""
        msg = fmt.format_options_screener_table([], skipped=0)
        assert msg != ""
        assert "no new" in msg.lower()

    def test_options_analysis_table_basic(self, fmt: AlertFormatter) -> None:
        msg = fmt.format_options_analysis_table(SAMPLE_ANALYSIS, skipped=0)
        assert "Options Analysis" in msg
        assert "NASDAQ:NVDA" in msg
        assert "1 new" in msg

    def test_options_analysis_empty(self, fmt: AlertFormatter) -> None:
        """Empty recs + 0 skipped → always returns non-empty empty-notice string."""
        msg = fmt.format_options_analysis_table([], skipped=0)
        assert msg != ""
        assert "no new" in msg.lower()

    def test_chunk_short_message_single_chunk(self, fmt: AlertFormatter) -> None:
        msg = "Hello world"
        chunks = fmt.chunk(msg)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_chunk_long_message_splits(self, fmt: AlertFormatter) -> None:
        # Create a message longer than 4000 chars
        long_msg = ("A" * 100 + "\n") * 45   # 4545 chars
        chunks = fmt.chunk(long_msg)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= fmt._CHUNK_MAX + 50  # allow minor overage for suffix

    def test_markdownv2_escaping_in_symbol(self, fmt: AlertFormatter) -> None:
        """MarkdownV2 must escape '.' in company names and '-' in broker names."""
        e = fmt._esc
        # Dot in company name (e.g., "Apple Inc.")
        escaped = e("Apple Inc.")
        assert "\\." in escaped

        # Dash in buy vehicle (e.g., "Moomoo-AU")
        escaped2 = e("Moomoo-AU")
        assert "\\-" in escaped2

        # Colon in symbol (e.g., "NASDAQ:NVDA") — colon is NOT a V2 special char
        escaped3 = e("NASDAQ:NVDA")
        assert escaped3 == "NASDAQ:NVDA"   # colon is safe in V2

    def test_score_bucket_thresholds(self, fmt: AlertFormatter) -> None:
        assert fmt._score_bucket(90.0) == "APEX"
        assert fmt._score_bucket(85.0) == "APEX"
        assert fmt._score_bucket(84.9) == "HIGH"
        assert fmt._score_bucket(70.0) == "HIGH"
        assert fmt._score_bucket(69.9) == "MED"
        assert fmt._score_bucket(55.0) == "MED"
        assert fmt._score_bucket(54.9) == "LOW"
        assert fmt._score_bucket(0.0)  == "LOW"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Helper function tests
# ═══════════════════════════════════════════════════════════════════════════

class TestParseHelpers:

    def test_parse_numeric_us_dollar(self) -> None:
        """MEDIUM-8: '$220.50' → 220.5"""
        assert _parse_numeric("$220.50") == pytest.approx(220.50)

    def test_parse_numeric_indian_rupee(self) -> None:
        """MEDIUM-8: '₹36,800' → 36800.0"""
        assert _parse_numeric("₹36,800") == pytest.approx(36800.0)

    def test_parse_numeric_with_prefix_text(self) -> None:
        """MEDIUM-8: 'CALL ₹36,800' → 36800.0"""
        assert _parse_numeric("CALL ₹36,800") == pytest.approx(36800.0)

    def test_parse_numeric_small_premium(self) -> None:
        """'$2.50' → 2.5"""
        assert _parse_numeric("$2.50") == pytest.approx(2.5)

    def test_parse_numeric_plain(self) -> None:
        """'1525.49' → 1525.49"""
        assert _parse_numeric("1525.49") == pytest.approx(1525.49)

    def test_parse_numeric_empty(self) -> None:
        assert _parse_numeric("") == 0.0

    def test_parse_numeric_none_like(self) -> None:
        assert _parse_numeric("N/A") == 0.0

    def test_parse_pct_high_value(self) -> None:
        """'45.2%' should return 0.452."""
        assert _parse_pct("45.2%") == pytest.approx(0.452)

    def test_parse_pct_low_value(self) -> None:
        """'0.45' (already fractional) should return 0.45."""
        assert _parse_pct("0.45") == pytest.approx(0.45)

    def test_parse_pct_empty(self) -> None:
        assert _parse_pct("") == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 4. Market hours gate tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMarketHoursGate:

    def test_market_open_during_us_hours(self) -> None:
        """UTC 15:00 (US market hours 13:30–20:00) should be open."""
        with patch("tradingview.telegram_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 18, 15, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Direct test using the function
        from tradingview.telegram_scheduler import _MARKET_WINDOWS_UTC
        hour = 15
        result = any(s <= hour < e for s, e in _MARKET_WINDOWS_UTC)
        assert result is True

    def test_market_closed_at_midnight_utc(self) -> None:
        """UTC 11:00 is between AU (0-6) and IN (3-10) and US (13-21) — closed."""
        from tradingview.telegram_scheduler import _MARKET_WINDOWS_UTC
        hour = 11
        result = any(s <= hour < e for s, e in _MARKET_WINDOWS_UTC)
        assert result is False

    def test_market_open_during_au_hours(self) -> None:
        """UTC 02:00 (AU market 00:00–06:00) should be open."""
        from tradingview.telegram_scheduler import _MARKET_WINDOWS_UTC
        hour = 2
        result = any(s <= hour < e for s, e in _MARKET_WINDOWS_UTC)
        assert result is True

    def test_market_open_during_india_hours(self) -> None:
        """UTC 06:00 (IN market 03:45–10:00) should be open."""
        from tradingview.telegram_scheduler import _MARKET_WINDOWS_UTC
        hour = 6
        result = any(s <= hour < e for s, e in _MARKET_WINDOWS_UTC)
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════
# 5. TelegramScheduler.run_cycle() with full mocking
# ═══════════════════════════════════════════════════════════════════════════

class TestTelegramSchedulerRunCycle:
    """
    Test run_cycle() end-to-end with:
    - AlertRunner methods mocked (no actual subprocesses)
    - DedupDB backed by tmp SQLite (real dedup logic)
    - send_table_v2 mocked (no actual Telegram API calls)
    """

    @pytest.fixture()
    def scheduler_with_mocks(self, tmp_path, monkeypatch):
        """Return a TelegramScheduler with mocked runner and send_table_v2."""
        db = DedupDB(db_path=tmp_path / "dedup.db")
        sched = TelegramScheduler(dry_run=False, force_market=True)
        sched._dedup = db  # inject real dedup with tmp DB
        return sched, db

    def test_run_cycle_sends_3_tables_when_all_have_data(
        self, scheduler_with_mocks, monkeypatch
    ):
        sched, db = scheduler_with_mocks

        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: (SAMPLE_STOCKS, datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener",
                            lambda: SAMPLE_SCREENER)
        monkeypatch.setattr(sched._runner, "run_options_analysis",
                            lambda syms: SAMPLE_ANALYSIS)

        sent_messages: list[str] = []

        # send_table_v2 is imported lazily inside run_cycle() from tradingview.telegram_alerts
        # so we patch the source module and is_enabled together
        import tradingview.telegram_scheduler as ts_mod
        import tradingview.telegram_alerts as ta_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: True)
        monkeypatch.setattr(ta_mod, "is_enabled", lambda: True)
        monkeypatch.setattr(ta_mod, "send_table_v2",
                            lambda text: sent_messages.append(text) or True)

        stats = sched.run_cycle()

        assert stats["stocks_new"] == 3
        assert stats["screener_new"] == 2
        assert stats["analysis_new"] == 1
        assert stats["stocks_skipped"] == 0
        assert stats["screener_skipped"] == 0
        assert stats["analysis_skipped"] == 0
        assert not stats["errors"]

    def test_run_cycle_dedup_on_second_call(self, scheduler_with_mocks, monkeypatch):
        """Second cycle should skip all symbols already sent in first cycle."""
        sched, db = scheduler_with_mocks

        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: (SAMPLE_STOCKS, datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener",
                            lambda: SAMPLE_SCREENER)
        monkeypatch.setattr(sched._runner, "run_options_analysis",
                            lambda syms: SAMPLE_ANALYSIS)

        sent_messages: list[str] = []
        monkeypatch.setattr("tradingview.telegram_alerts.send_table_v2",
                            lambda text: sent_messages.append(text) or True)
        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: True)
        import tradingview.telegram_alerts as ta_mod
        monkeypatch.setattr(ta_mod, "is_enabled", lambda: True)

        # First cycle
        stats1 = sched.run_cycle()
        assert stats1["stocks_new"] == 3

        # Second cycle with same data
        stats2 = sched.run_cycle()
        assert stats2["stocks_new"] == 0
        assert stats2["stocks_skipped"] == 3
        assert stats2["screener_new"] == 0
        assert stats2["screener_skipped"] == 2
        assert stats2["analysis_new"] == 0
        assert stats2["analysis_skipped"] == 1

    def test_run_cycle_skips_market_hours_when_closed(self, scheduler_with_mocks, monkeypatch):
        """Cycle skips silently when all markets are closed and force=False."""
        sched, db = scheduler_with_mocks
        sched._force_market = False
        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: False)

        stats = sched.run_cycle()
        assert stats["skipped_market_hours"] is True
        assert stats["stocks_new"] == 0

    def test_run_cycle_force_market_bypasses_gate(self, scheduler_with_mocks, monkeypatch):
        """force_market=True should run even when market is closed."""
        sched, db = scheduler_with_mocks
        sched._force_market = True

        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: False)
        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: ([], datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener", lambda: [])
        monkeypatch.setattr(sched._runner, "run_options_analysis", lambda syms: [])

        stats = sched.run_cycle()
        assert stats["skipped_market_hours"] is False

    def test_run_cycle_no_send_when_all_empty(self, scheduler_with_mocks, monkeypatch):
        """When all 3 tables have 0 new alerts → sends nothing (flood prevention)."""
        sched, db = scheduler_with_mocks

        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: ([], datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener", lambda: [])
        monkeypatch.setattr(sched._runner, "run_options_analysis", lambda syms: [])

        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: True)

        sent_messages: list[str] = []
        monkeypatch.setattr("tradingview.telegram_alerts.send_table_v2",
                            lambda text: sent_messages.append(text) or True)
        import tradingview.telegram_alerts as ta_mod
        monkeypatch.setattr(ta_mod, "is_enabled", lambda: True)

        stats = sched.run_cycle()
        assert stats["stocks_new"] == 0
        # Flood prevention: sends NOTHING when all 3 tables are empty
        assert len(sent_messages) == 0

    def test_run_cycle_options_screener_error_still_sends_stocks(
        self, scheduler_with_mocks, monkeypatch
    ):
        """Error in options screener should not abort the stocks table send."""
        sched, db = scheduler_with_mocks

        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: (SAMPLE_STOCKS, datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener",
                            lambda: (_ for _ in ()).throw(RuntimeError("yfinance timeout")))
        monkeypatch.setattr(sched._runner, "_parse_options_actionable_html", lambda: [])
        monkeypatch.setattr(sched._runner, "run_options_analysis",
                            lambda syms: SAMPLE_ANALYSIS)

        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: True)

        sent_messages: list[str] = []
        monkeypatch.setattr("tradingview.telegram_alerts.send_table_v2",
                            lambda text: sent_messages.append(text) or True)
        import tradingview.telegram_alerts as ta_mod
        monkeypatch.setattr(ta_mod, "is_enabled", lambda: True)

        stats = sched.run_cycle()
        # Stocks should still have results even though screener errored
        assert stats["stocks_new"] == 3
        # Error captured in stats
        assert any("screener" in e.lower() or "error" in e.lower() for e in stats["errors"])

    def test_run_cycle_dry_run_no_telegram_send(self, tmp_path, monkeypatch):
        """--dry-run: run all 3 workflows but never call send_table_v2."""
        db = DedupDB(db_path=tmp_path / "dedup.db")
        sched = TelegramScheduler(dry_run=True, force_market=True)
        sched._dedup = db

        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: (SAMPLE_STOCKS, datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener",
                            lambda: SAMPLE_SCREENER)
        monkeypatch.setattr(sched._runner, "run_options_analysis",
                            lambda syms: SAMPLE_ANALYSIS)

        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: True)

        send_called: list[str] = []
        monkeypatch.setattr("tradingview.telegram_alerts.send_table_v2",
                            lambda text: send_called.append(text) or True)
        import tradingview.telegram_alerts as ta_mod
        monkeypatch.setattr(ta_mod, "is_enabled", lambda: True)

        stats = sched.run_cycle()
        # In dry-run, dedup marks are NOT written (no actual send)
        assert send_called == []   # send_table_v2 was never called
        # But stats still reflect what WOULD have been sent
        assert stats["stocks_new"] == 3

    def test_options_analysis_skipped_when_no_symbols(self, scheduler_with_mocks, monkeypatch):
        """When signal_stack returns 0 results AND DB fallback also empty,
        run_options_analysis is called with an empty list (pre-flight gate inside it handles the skip)."""
        sched, db = scheduler_with_mocks

        analysis_called = []
        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: ([], datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener", lambda: [])
        monkeypatch.setattr(sched._runner, "run_options_analysis",
                            lambda syms: analysis_called.append(syms) or [])
        # Mock DB fallback to also return empty so options analysis gets []
        monkeypatch.setattr(sched._runner, "_query_top_symbols_from_db",
                            lambda hours=24, limit=5: [])

        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: True)

        sched.run_cycle()
        # run_options_analysis should be called with empty list → returns immediately
        assert analysis_called == [[]]  # called once with empty list

    def test_options_analysis_uses_db_fallback_when_signal_stack_empty(self, scheduler_with_mocks, monkeypatch):
        """Bug #2 fix: when signal_stack returns 0 results, top_symbols falls back to DB query."""
        sched, db = scheduler_with_mocks

        fallback_syms = ["NASDAQ:AAPL", "NYSE:MSFT"]
        analysis_called = []
        monkeypatch.setattr(sched._runner, "run_signal_stack",
                            lambda: ([], datetime.now(timezone.utc)))
        monkeypatch.setattr(sched._runner, "run_options_screener", lambda: [])
        monkeypatch.setattr(sched._runner, "run_options_analysis",
                            lambda syms: analysis_called.append(syms) or [])
        # DB fallback returns symbols
        monkeypatch.setattr(sched._runner, "_query_top_symbols_from_db",
                            lambda hours=24, limit=5: fallback_syms)

        import tradingview.telegram_scheduler as ts_mod
        monkeypatch.setattr(ts_mod, "_any_market_open", lambda: True)

        sched.run_cycle()
        # run_options_analysis must be called with the DB fallback symbols
        assert analysis_called == [fallback_syms], (
            f"Expected fallback symbols, got: {analysis_called}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. send_table_v2 in telegram_alerts.py
# ═══════════════════════════════════════════════════════════════════════════

class TestSendTableV2:

    def test_send_table_v2_uses_markdownv2_parse_mode(self, monkeypatch):
        """BLOCKER-3: send_table_v2 must use parse_mode=MarkdownV2, not Markdown."""
        import tradingview.telegram_alerts as ta

        # Temporarily enable Telegram by patching env
        monkeypatch.setattr(ta, "BOT_TOKEN", "FAKE_TOKEN")
        monkeypatch.setattr(ta, "CHAT_ID", "12345")

        captured_payload: list[dict] = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        def mock_post(url, json=None, timeout=None):
            captured_payload.append(json or {})
            return mock_resp

        monkeypatch.setattr("requests.post", mock_post, raising=False)
        try:
            import requests
            monkeypatch.setattr(requests, "post", mock_post)
        except ImportError:
            pass

        ta.send_table_v2("Test *MarkdownV2* message")

        assert captured_payload, "requests.post was never called"
        payload = captured_payload[0]
        assert payload.get("parse_mode") == "MarkdownV2", (
            f"Expected parse_mode='MarkdownV2', got {payload.get('parse_mode')!r}"
        )

    def test_send_table_v2_returns_false_when_not_configured(self, monkeypatch):
        """send_table_v2 returns False silently when token not set (BUG-T5 pattern)."""
        import tradingview.telegram_alerts as ta
        # Dynamic credential functions read env vars on every call
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
        # Clear any cached values
        monkeypatch.setattr(ta, "_BOT_TOKEN_CACHE", [None])
        monkeypatch.setattr(ta, "_CHAT_ID_CACHE", [None])
        result = ta.send_table_v2("Test message")
        assert result is False

    def test_original_send_still_uses_markdown_v1(self, monkeypatch):
        """Verify _send() still uses V1 Markdown (not broken by our changes)."""
        import tradingview.telegram_alerts as ta
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE_TOKEN")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        monkeypatch.setattr(ta, "_BOT_TOKEN_CACHE", [None])
        monkeypatch.setattr(ta, "_CHAT_ID_CACHE", [None])

        captured_payload: list[dict] = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        def mock_post(url, json=None, timeout=None):
            captured_payload.append(json or {})
            return mock_resp

        try:
            import requests
            monkeypatch.setattr(requests, "post", mock_post)
        except ImportError:
            return  # requests not available, skip

        ta._send("Test Markdown V1 message")

        assert captured_payload, "requests.post was never called"
        payload = captured_payload[0]
        assert payload.get("parse_mode") == "Markdown", (
            f"_send() must use V1 Markdown, got {payload.get('parse_mode')!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Integration smoke test — dry-run via subprocess
# ═══════════════════════════════════════════════════════════════════════════

class TestDryRunSmoke:

    def test_dry_run_imports_without_error(self):
        """The scheduler module must be importable without side effects."""
        import tradingview.telegram_scheduler  # noqa: F401

    def test_argparse_dry_run_flag(self):
        """--dry-run and --once flags parse without error."""
        from tradingview.telegram_scheduler import _parse_args
        import sys as _sys
        old_argv = _sys.argv
        try:
            _sys.argv = ["telegram_scheduler.py", "--dry-run", "--once"]
            args = _parse_args()
            assert args.dry_run is True
            assert args.once is True
            assert args.force is False
        finally:
            _sys.argv = old_argv

    def test_argparse_force_flag(self):
        """--force flag parses correctly."""
        from tradingview.telegram_scheduler import _parse_args
        import sys as _sys
        old_argv = _sys.argv
        try:
            _sys.argv = ["telegram_scheduler.py", "--force", "--once"]
            args = _parse_args()
            assert args.force is True
        finally:
            _sys.argv = old_argv


# ═══════════════════════════════════════════════════════════════════════════
# 8. AlertRunner HTML parsing (unit tests, no actual subprocesses)
# ═══════════════════════════════════════════════════════════════════════════

class TestAlertRunnerHTMLParsing:

    @pytest.fixture()
    def runner(self) -> AlertRunner:
        return AlertRunner()

    def test_parse_options_actionable_html_empty_state(self, runner, tmp_path, monkeypatch):
        """Empty-state HTML (no .contract-card) returns [] without crash."""
        empty_html = """<html><body>
            <div>No contracts passed the actionable filter for today's scan.</div>
        </body></html>"""
        html_file = tmp_path / "options_actionable.html"
        html_file.write_text(empty_html, encoding="utf-8")

        import tradingview.telegram_scheduler as ts
        monkeypatch.setattr(ts, "_REPORTS", tmp_path)

        result = runner._parse_options_actionable_html()
        assert result == []

    def test_parse_options_actionable_html_with_cards(self, runner, tmp_path, monkeypatch):
        """HTML with .contract-card elements parses correctly."""
        html_with_cards = """<html><body>
            <div class="contract-card"
                 data-symbol="NASDAQ:NVDA"
                 data-tqs="7.5"
                 data-dte="33"
                 data-pw="0.45"
                 data-strike="900"
                 data-expiry="2026-06-20"
                 data-type="CALL"
                 data-iv="0.42"
                 data-premium="12.50">
            </div>
        </body></html>"""
        html_file = tmp_path / "options_actionable.html"
        html_file.write_text(html_with_cards, encoding="utf-8")

        import tradingview.telegram_scheduler as ts
        monkeypatch.setattr(ts, "_REPORTS", tmp_path)

        result = runner._parse_options_actionable_html()
        assert len(result) == 1
        assert result[0]["underlying"] == "NASDAQ:NVDA"
        assert result[0]["tqs"] == pytest.approx(7.5)
        assert result[0]["dte"] == 33

    def test_parse_options_report_html_indian_strike(self, runner, tmp_path, monkeypatch):
        """MEDIUM-8: Indian rupee strike '₹36,800' parses to 36800.0 without crash."""
        html_with_indian = """<html><body>
            <div class="sym-card" data-sym="NSE:NIFTY" data-market="IN">
                <div class="contract-card">
                    <div class="cc-strike">CALL ₹36,800</div>
                    <div class="cc-expiry">exp 30JUN2026 (44d)</div>
                    <div class="cc-premium">₹1525.49</div>
                    <div class="comp-score">65</div>
                </div>
            </div>
        </body></html>"""
        html_file = tmp_path / "options_report.html"
        html_file.write_text(html_with_indian, encoding="utf-8")

        import tradingview.telegram_scheduler as ts
        monkeypatch.setattr(ts, "_REPORTS", tmp_path)

        result = runner._parse_options_report_html()
        assert len(result) == 1
        assert result[0]["symbol"] == "NSE:NIFTY"
        assert result[0]["type"] == "CALL"
        # Strike should parse to 36800 (no ValueError)
        assert result[0]["strike"] == pytest.approx(36800.0)

    def test_parse_options_report_html_us_strike(self, runner, tmp_path, monkeypatch):
        """US dollar strike '$220.50' parses to 220.5."""
        html_with_us = """<html><body>
            <div class="sym-card" data-sym="NASDAQ:NVDA" data-market="US">
                <div class="contract-card">
                    <div class="cc-strike">CALL $220.50</div>
                    <div class="cc-expiry">exp 20JUN2026 (33d)</div>
                    <div class="cc-premium">$12.50</div>
                    <div class="comp-score">72</div>
                </div>
            </div>
        </body></html>"""
        html_file = tmp_path / "options_report.html"
        html_file.write_text(html_with_us, encoding="utf-8")

        import tradingview.telegram_scheduler as ts
        monkeypatch.setattr(ts, "_REPORTS", tmp_path)

        result = runner._parse_options_report_html()
        assert len(result) == 1
        assert result[0]["strike"] == pytest.approx(220.5)
        assert result[0]["type"] == "CALL"

    def test_parse_nonexistent_html_returns_empty(self, runner, tmp_path, monkeypatch):
        """Missing HTML file returns [] without crash."""
        import tradingview.telegram_scheduler as ts
        monkeypatch.setattr(ts, "_REPORTS", tmp_path)  # tmp_path has no HTML files
        result1 = runner._parse_options_actionable_html()
        result2 = runner._parse_options_report_html()
        assert result1 == []
        assert result2 == []

# ═══════════════════════════════════════════════════════════════════════════
# 12. _validate_telegram + --health-check tests (BVT requirement)
# ═══════════════════════════════════════════════════════════════════════════

class TestValidateTelegram:
    """Tests for _validate_telegram() connectivity check and --health-check CLI flag."""

    def test_validate_telegram_success(self, monkeypatch) -> None:
        """_validate_telegram returns True when getMe API succeeds."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"result": {"username": "TestBot", "id": 999}}
        with patch("requests.get", return_value=mock_resp) as mock_get:
            from tradingview.telegram_scheduler import _validate_telegram
            result = _validate_telegram()
        assert result is True
        mock_get.assert_called_once()

    def test_validate_telegram_missing_token(self, monkeypatch) -> None:
        """_validate_telegram returns False when BOT_TOKEN is empty."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
        from tradingview.telegram_scheduler import _validate_telegram
        result = _validate_telegram()
        assert result is False

    def test_validate_telegram_missing_chat_id(self, monkeypatch) -> None:
        """_validate_telegram returns False when both CHAT_ID and ALLOWED_USERS empty."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
        from tradingview.telegram_scheduler import _validate_telegram
        result = _validate_telegram()
        assert result is False

    def test_validate_telegram_http_error(self, monkeypatch) -> None:
        """_validate_telegram returns False on HTTP 401 (invalid token)."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:badtoken")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
        mock_resp = MagicMock(status_code=401)
        with patch("requests.get", return_value=mock_resp):
            from tradingview.telegram_scheduler import _validate_telegram
            result = _validate_telegram()
        assert result is False

    def test_validate_telegram_connection_error(self, monkeypatch) -> None:
        """_validate_telegram returns False on network timeout."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
        with patch("requests.get", side_effect=Exception("Connection timeout")):
            from tradingview.telegram_scheduler import _validate_telegram
            result = _validate_telegram()
        assert result is False

    def test_health_check_flag_success(self, monkeypatch) -> None:
        """--health-check exits 0 when validation passes."""
        monkeypatch.setattr("sys.argv", ["telegram_scheduler.py", "--health-check"])
        with patch("tradingview.telegram_scheduler._validate_telegram", return_value=True):
            with pytest.raises(SystemExit) as exc_info:
                from tradingview.telegram_scheduler import main
                main()
            assert exc_info.value.code == 0

    def test_health_check_flag_failure(self, monkeypatch) -> None:
        """--health-check exits 1 when validation fails."""
        monkeypatch.setattr("sys.argv", ["telegram_scheduler.py", "--health-check"])
        with patch("tradingview.telegram_scheduler._validate_telegram", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                from tradingview.telegram_scheduler import main
                main()
            assert exc_info.value.code == 1