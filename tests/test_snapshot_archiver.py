"""Tests for F16: Portfolio Snapshot Archiver."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview" / "analysis"))

from tradingview.analysis.snapshot_archiver import (
    init_db,
    load_positions,
    build_broker_breakdown,
    save_snapshot,
    get_snapshot_on_date,
    list_snapshots,
    compare_snapshots,
    generate_html,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. init_db
# ═══════════════════════════════════════════════════════════════════════════════

class TestInitDb:
    def test_creates_db(self, tmp_path):
        db = tmp_path / "test.db"
        conn = init_db(db)
        # Check table exists
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='snapshots'")
        assert cur.fetchone() is not None
        conn.close()

    def test_idempotent(self, tmp_path):
        db = tmp_path / "test.db"
        conn1 = init_db(db)
        conn1.close()
        conn2 = init_db(db)  # should not error
        conn2.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. load_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPositions:
    def test_valid(self, tmp_path):
        f = tmp_path / "pos.json"
        f.write_text('[{"symbol":"X","market_val":100}]', encoding="utf-8")
        assert len(load_positions(f)) == 1

    def test_missing(self, tmp_path):
        assert load_positions(tmp_path / "nope.json") == []

    def test_invalid(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("broken", encoding="utf-8")
        assert load_positions(f) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. build_broker_breakdown
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildBrokerBreakdown:
    def test_pre_seeds_all_six(self):
        result = build_broker_breakdown([])
        expected = {"AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"}
        assert set(result.keys()) == expected

    def test_aggregation(self):
        pos = [
            {"broker": "IBKR", "market_val": 10000, "pnl": 500},
            {"broker": "IBKR", "market_val": 5000, "pnl": -100},
        ]
        result = build_broker_breakdown(pos)
        assert result["IBKR"]["count"] == 2
        assert result["IBKR"]["value"] == 15000
        assert result["IBKR"]["pnl"] == 400

    def test_unknown_broker(self):
        pos = [{"broker": "NewBroker", "market_val": 100, "pnl": 10}]
        result = build_broker_breakdown(pos)
        assert "NewBroker" in result
        assert result["NewBroker"]["count"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 4. save_snapshot + get_snapshot_on_date
# ═══════════════════════════════════════════════════════════════════════════════

class TestSaveAndGet:
    @pytest.fixture
    def conn(self, tmp_path):
        c = init_db(tmp_path / "test.db")
        yield c
        c.close()

    def test_save_returns_id(self, conn):
        pos = [{"symbol": "NVDA", "broker": "IBKR", "market_val": 10000, "pnl": 500, "shares": 50}]
        row_id = save_snapshot(pos, conn, timestamp="2026-03-01T09:00:00Z")
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_get_exact_date(self, conn):
        pos = [{"symbol": "NVDA", "broker": "IBKR", "market_val": 10000, "pnl": 500, "shares": 50}]
        save_snapshot(pos, conn, timestamp="2026-03-01T09:00:00Z")
        result = get_snapshot_on_date(conn, "2026-03-01")
        assert result is not None
        assert result["total_value"] == 10000

    def test_get_closest_before(self, conn):
        """If no snapshot on exact date, gets closest before."""
        pos = [{"symbol": "A", "broker": "IBKR", "market_val": 5000, "pnl": 100, "shares": 10}]
        save_snapshot(pos, conn, timestamp="2026-02-28T09:00:00Z")
        result = get_snapshot_on_date(conn, "2026-03-15")
        # Should find the one on 2026-02-28 (closest before 2026-03-15 since no exact)
        assert result is not None
        assert "2026-02-28" in result["snapshot_ts"]

    def test_get_none(self, conn):
        assert get_snapshot_on_date(conn, "2099-01-01") is None

    def test_multiple_snapshots(self, conn):
        save_snapshot(
            [{"symbol": "A", "broker": "IBKR", "market_val": 1000, "pnl": 50, "shares": 5}],
            conn, timestamp="2026-01-01T09:00:00Z",
        )
        save_snapshot(
            [{"symbol": "B", "broker": "IBKR", "market_val": 2000, "pnl": 100, "shares": 10}],
            conn, timestamp="2026-02-01T09:00:00Z",
        )
        s = get_snapshot_on_date(conn, "2026-01-15")
        assert s is not None
        assert s["total_value"] == 2000  # gets first snapshot >= 2026-01-15 → Feb 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. list_snapshots
# ═══════════════════════════════════════════════════════════════════════════════

class TestListSnapshots:
    @pytest.fixture
    def conn(self, tmp_path):
        c = init_db(tmp_path / "test.db")
        yield c
        c.close()

    def test_empty(self, conn):
        assert list_snapshots(conn) == []

    def test_order(self, conn):
        save_snapshot([{"symbol": "A", "broker": "X", "market_val": 100, "pnl": 0, "shares": 1}],
                      conn, timestamp="2026-01-01T00:00:00Z")
        save_snapshot([{"symbol": "B", "broker": "Y", "market_val": 200, "pnl": 0, "shares": 2}],
                      conn, timestamp="2026-02-01T00:00:00Z")
        snaps = list_snapshots(conn)
        assert len(snaps) == 2
        assert snaps[0]["snapshot_ts"] > snaps[1]["snapshot_ts"]  # newest first

    def test_limit(self, conn):
        for i in range(10):
            save_snapshot(
                [{"symbol": f"S{i}", "broker": "X", "market_val": i * 100, "pnl": 0, "shares": 1}],
                conn, timestamp=f"2026-01-{i + 1:02d}T00:00:00Z",
            )
        assert len(list_snapshots(conn, limit=5)) == 5


# ═══════════════════════════════════════════════════════════════════════════════
# 6. compare_snapshots
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompareSnapshots:
    @pytest.fixture
    def conn(self, tmp_path):
        c = init_db(tmp_path / "test.db")
        yield c
        c.close()

    def test_compare(self, conn):
        save_snapshot(
            [{"symbol": "A", "broker": "IBKR", "market_val": 10000, "pnl": 500, "shares": 10}],
            conn, timestamp="2026-01-01T09:00:00Z",
        )
        save_snapshot(
            [{"symbol": "A", "broker": "IBKR", "market_val": 12000, "pnl": 1000, "shares": 10}],
            conn, timestamp="2026-02-01T09:00:00Z",
        )
        result = compare_snapshots(conn, "2026-01-01", "2026-02-01")
        assert result is not None
        assert result["value_change"] == 2000
        assert result["pnl_change"] == 500

    def test_compare_missing(self, conn):
        assert compare_snapshots(conn, "2099-01-01", "2099-02-01") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 7. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def test_with_data(self):
        snaps = [
            {"snapshot_ts": "2026-03-01T09:00:00Z", "total_value": 100000,
             "total_pnl": 5000, "position_count": 50},
        ]
        html = generate_html(snaps)
        assert "100,000" in html
        assert "5,000" in html

    def test_empty(self):
        html = generate_html([])
        assert "No snapshots" in html

    def test_written(self, tmp_path):
        out = tmp_path / "snaps.html"
        generate_html([], out)
        assert out.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
