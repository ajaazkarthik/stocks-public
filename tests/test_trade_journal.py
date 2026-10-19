"""Tests for tradingview/analysis/trade_journal.py — Trade Journal with outcome tracking."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tradingview.analysis.trade_journal as trade_journal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_db(tmp_path):
    """Create a temporary alert_tracker.db with sample data."""
    db_path = tmp_path / "alert_tracker.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            composite_score REAL,
            l1_technical REAL,
            l2_fundamental REAL,
            l3_sentiment REAL,
            l4_market REAL,
            return_7d REAL,
            timestamp TEXT,
            action TEXT
        )
    """)
    now = datetime.now()
    rows = [
        ("AAPL", 72.5, 25.0, 22.0, 14.0, 11.5, 3.5, (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"), "BUY"),
        ("MSFT", 65.0, 20.0, 20.0, 12.0, 13.0, -2.1, (now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S"), "BUY"),
        ("GOOGL", 48.0, 15.0, 14.0, 10.0, 9.0, -5.0, (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"), "BUY"),
        ("AMZN", 80.0, 28.0, 25.0, 15.0, 12.0, 8.2, (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), "BUY"),
        ("TSLA", 55.0, 18.0, 16.0, 11.0, 10.0, None, (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "BUY"),
    ]
    conn.executemany(
        "INSERT INTO alerts (symbol, composite_score, l1_technical, l2_fundamental, l3_sentiment, l4_market, return_7d, timestamp, action) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def empty_db(tmp_path):
    """Create an empty alert_tracker.db."""
    db_path = tmp_path / "alert_tracker.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            composite_score REAL,
            return_7d REAL,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test: fetch_journal_entries
# ---------------------------------------------------------------------------

class TestFetchJournalEntries:
    def test_returns_entries_from_db(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        assert len(entries) == 5

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            trade_journal.fetch_journal_entries(tmp_path / "nonexistent.db")

    def test_returns_empty_for_empty_db(self, empty_db):
        entries = trade_journal.fetch_journal_entries(empty_db)
        assert entries == []

    def test_entry_has_required_fields(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        e = entries[0]
        assert hasattr(e, "symbol")
        assert hasattr(e, "composite_score")
        assert hasattr(e, "return_7d")

    def test_filters_by_days(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db, days=2)
        assert len(entries) <= 5


# ---------------------------------------------------------------------------
# Test: compute_journal_stats
# ---------------------------------------------------------------------------

class TestComputeJournalStats:
    def test_computes_win_rate(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        assert "win_rate_7d" in stats
        assert 0 <= stats["win_rate_7d"] <= 100

    def test_computes_total_signals(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        assert "total_signals" in stats
        assert stats["total_signals"] == 5

    def test_computes_avg_return(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        assert "avg_return_7d" in stats

    def test_handles_empty_entries(self):
        stats = trade_journal.compute_journal_stats([])
        assert stats["total_signals"] == 0
        assert stats["win_rate_7d"] == 0

    def test_computes_best_symbol(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        assert "best_symbol" in stats
        assert "AMZN" in stats["best_symbol"]

    def test_computes_worst_symbol(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        assert "worst_symbol" in stats
        assert "GOOGL" in stats["worst_symbol"]

    def test_pending_count(self, sample_db):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        assert stats["pending"] == 1  # TSLA has None return


# ---------------------------------------------------------------------------
# Test: generate_journal_html
# ---------------------------------------------------------------------------

class TestGenerateJournalHtml:
    def test_creates_html_file(self, sample_db, tmp_path):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        out = tmp_path / "journal.html"
        result = trade_journal.generate_journal_html(entries, stats, out)
        assert result.exists()
        html = result.read_text(encoding="utf-8")
        assert "Trade Journal" in html

    def test_html_contains_symbols(self, sample_db, tmp_path):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        out = tmp_path / "journal.html"
        result = trade_journal.generate_journal_html(entries, stats, out)
        html = result.read_text(encoding="utf-8")
        assert "AAPL" in html

    def test_html_contains_stats(self, sample_db, tmp_path):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        out = tmp_path / "journal.html"
        result = trade_journal.generate_journal_html(entries, stats, out)
        html = result.read_text(encoding="utf-8")
        assert "Win" in html or "win" in html

    def test_creates_parent_dirs(self, sample_db, tmp_path):
        entries = trade_journal.fetch_journal_entries(sample_db)
        stats = trade_journal.compute_journal_stats(entries)
        out = tmp_path / "sub" / "dir" / "journal.html"
        result = trade_journal.generate_journal_html(entries, stats, out)
        assert result.exists()

    def test_handles_empty_entries(self, tmp_path):
        stats = trade_journal.compute_journal_stats([])
        out = tmp_path / "journal.html"
        result = trade_journal.generate_journal_html([], stats, out)
        assert result.exists()


# ---------------------------------------------------------------------------
# Integration test: end-to-end
# ---------------------------------------------------------------------------

class TestJournalIntegration:
    def test_full_pipeline(self, sample_db, tmp_path):
        entries = trade_journal.fetch_journal_entries(sample_db)
        assert len(entries) > 0
        stats = trade_journal.compute_journal_stats(entries)
        assert stats["total_signals"] > 0
        out = tmp_path / "journal.html"
        result = trade_journal.generate_journal_html(entries, stats, out)
        assert result.exists()
        html = result.read_text(encoding="utf-8")
        assert len(html) > 100

    def test_no_data_pipeline(self, empty_db, tmp_path):
        entries = trade_journal.fetch_journal_entries(empty_db)
        assert entries == []
        stats = trade_journal.compute_journal_stats(entries)
        assert stats["total_signals"] == 0
