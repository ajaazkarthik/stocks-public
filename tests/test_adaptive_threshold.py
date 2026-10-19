"""Tests for adaptive_threshold.py — ensures correct DB and semantic alignment (H-03)."""
import pytest
import sqlite3
import tempfile
from pathlib import Path


def test_alerts_db_path_correct():
    """_ALERTS_DB must point to database/alert_tracker.db, not sell_history.db."""
    from tradingview.adaptive_threshold import _ALERTS_DB
    assert _ALERTS_DB.parent.name == "database", (
        f"Expected database/ folder, got {_ALERTS_DB.parent}"
    )
    assert "alert_tracker" in _ALERTS_DB.name, (
        f"Expected alert_tracker.db, got {_ALERTS_DB.name}"
    )


def test_analyze_thresholds_from_alerts_empty_db(tmp_path):
    """analyze_thresholds_from_alerts handles empty/missing DB gracefully."""
    from tradingview.adaptive_threshold import analyze_thresholds_from_alerts
    result = analyze_thresholds_from_alerts(tmp_path / "nonexistent.db")
    assert result["total_trades"] == 0
    assert "recommendation" in result
    assert result["recommendation"] != ""


def test_analyze_thresholds_from_alerts_with_data(tmp_path):
    """analyze_thresholds_from_alerts uses composite_score >= threshold correctly (JOIN schema)."""
    from tradingview.adaptive_threshold import analyze_thresholds_from_alerts

    db = tmp_path / "alert_tracker.db"
    conn = sqlite3.connect(str(db))
    # Correct schema: alerts + performance_metrics JOIN (no return_7d on alerts)
    conn.execute("""
        CREATE TABLE alerts (
            alert_uuid TEXT PRIMARY KEY,
            composite_score REAL
        )
    """)
    conn.execute("""
        CREATE TABLE performance_metrics (
            alert_uuid TEXT,
            return_7d REAL
        )
    """)
    # 30 winning trades at score >= 55
    for i in range(30):
        uid = f"win-{i}"
        conn.execute("INSERT INTO alerts VALUES (?, 65.0)", (uid,))
        conn.execute("INSERT INTO performance_metrics VALUES (?, 5.0)", (uid,))
    # 10 losing trades at low score
    for i in range(10):
        uid = f"loss-{i}"
        conn.execute("INSERT INTO alerts VALUES (?, 40.0)", (uid,))
        conn.execute("INSERT INTO performance_metrics VALUES (?, -3.0)", (uid,))
    conn.commit()
    conn.close()

    result = analyze_thresholds_from_alerts(db)
    assert result["total_trades"] == 40
    assert result["win_rate"] == 75.0  # 30/40
    assert len(result["thresholds"]) > 0
    # High threshold should filter to winning trades only
    high_threshold = next((t for t in result["thresholds"] if t["threshold"] == 60), None)
    if high_threshold and high_threshold["trades"] > 0:
        assert high_threshold["win_rate"] == 100.0  # all 30 winning trades at score 65 >= 60
        assert "avg_win" in high_threshold
        assert "avg_loss" in high_threshold


def test_analyze_thresholds_falls_back_to_sell_history():
    """analyze_thresholds falls back to sell_history.db when alerts DB has insufficient data."""
    from tradingview.adaptive_threshold import analyze_thresholds
    result = analyze_thresholds()  # uses defaults
    # Should return a dict with required keys (no exception)
    assert "total_trades" in result
    assert "thresholds" in result
    assert "recommendation" in result


def test_win_condition_is_buy_not_sell():
    """Verify the win condition uses return_7d (buy outcome), NOT conviction_score (sell urgency)."""
    import inspect
    from tradingview import adaptive_threshold
    src = inspect.getsource(adaptive_threshold.analyze_thresholds_from_alerts)
    assert "return_7d" in src, "analyze_thresholds_from_alerts must use return_7d for win condition"
    assert "composite_score" in src, (
        "analyze_thresholds_from_alerts must filter by composite_score"
    )


# ─── Tests for H-03 fix: JOIN schema, avg_win/avg_loss, data_source field ─────

def _make_alerts_db(tmp_path, winners=10, losers=5, win_return=5.0, loss_return=-2.0,
                    win_score=65.0, loss_score=45.0):
    """Helper: create a temp alert_tracker.db with alerts + performance_metrics JOIN schema."""
    db = tmp_path / "alert_tracker.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE alerts (
            alert_uuid TEXT PRIMARY KEY,
            composite_score REAL
        )
    """)
    conn.execute("""
        CREATE TABLE performance_metrics (
            alert_uuid TEXT,
            return_7d REAL
        )
    """)
    for i in range(winners):
        uid = f"win-{i}"
        conn.execute("INSERT INTO alerts VALUES (?, ?)", (uid, win_score))
        conn.execute("INSERT INTO performance_metrics VALUES (?, ?)", (uid, win_return))
    for i in range(losers):
        uid = f"loss-{i}"
        conn.execute("INSERT INTO alerts VALUES (?, ?)", (uid, loss_score))
        conn.execute("INSERT INTO performance_metrics VALUES (?, ?)", (uid, loss_return))
    conn.commit()
    conn.close()
    return db


def test_analyze_thresholds_data_source_field(tmp_path):
    """analyze_thresholds_from_alerts result must always carry data_source='alert_tracker.db'."""
    from tradingview.adaptive_threshold import analyze_thresholds_from_alerts

    db = _make_alerts_db(tmp_path, winners=5, losers=5)
    result = analyze_thresholds_from_alerts(db)
    assert result["data_source"] == "alert_tracker.db"


def test_analyze_thresholds_avg_win_avg_loss_keys(tmp_path):
    """Each threshold bucket dict must contain 'avg_win' and 'avg_loss' keys."""
    from tradingview.adaptive_threshold import analyze_thresholds_from_alerts

    db = _make_alerts_db(tmp_path, winners=15, losers=5)
    result = analyze_thresholds_from_alerts(db)
    assert len(result["thresholds"]) > 0, "Expected at least one threshold bucket"
    for bucket in result["thresholds"]:
        assert "avg_win" in bucket, f"avg_win missing from bucket {bucket['threshold']}"
        assert "avg_loss" in bucket, f"avg_loss missing from bucket {bucket['threshold']}"


def test_analyze_thresholds_avg_win_avg_loss_values(tmp_path):
    """avg_win > 0 for winning trades and avg_loss < 0 for losing trades in the all-trades bucket."""
    from tradingview.adaptive_threshold import analyze_thresholds_from_alerts

    # Both winners (score=65, return=5.0) and losers (score=45, return=-2.0)
    # qualify at threshold=40, so that bucket has both avg_win and avg_loss populated.
    db = _make_alerts_db(tmp_path, winners=10, losers=5,
                         win_return=5.0, loss_return=-2.0,
                         win_score=65.0, loss_score=45.0)
    result = analyze_thresholds_from_alerts(db)

    # threshold=40 is the lowest bucket — all rows qualify
    bucket_40 = next((b for b in result["thresholds"] if b["threshold"] == 40), None)
    assert bucket_40 is not None, "Expected threshold=40 bucket"
    assert bucket_40["trades"] == 15, f"Expected 15 trades in bucket 40, got {bucket_40['trades']}"
    assert bucket_40["avg_win"] > 0, (
        f"avg_win should be positive for winning trades, got {bucket_40['avg_win']}"
    )
    assert bucket_40["avg_loss"] < 0, (
        f"avg_loss should be negative for losing trades, got {bucket_40['avg_loss']}"
    )


def test_analyze_thresholds_no_performance_metrics_rows(tmp_path):
    """Graceful empty result when performance_metrics table exists but is empty (JOIN returns 0)."""
    from tradingview.adaptive_threshold import analyze_thresholds_from_alerts

    db = tmp_path / "alert_tracker.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE alerts (alert_uuid TEXT PRIMARY KEY, composite_score REAL)")
    conn.execute("CREATE TABLE performance_metrics (alert_uuid TEXT, return_7d REAL)")
    # Insert alerts but NO matching performance_metrics rows
    conn.execute("INSERT INTO alerts VALUES ('abc', 60.0)")
    conn.commit()
    conn.close()

    result = analyze_thresholds_from_alerts(db)
    assert result["total_trades"] == 0
    assert result["win_rate"] == 0.0
    assert result["thresholds"] == []
    assert result["recommendation"] != ""
    assert result["data_source"] == "alert_tracker.db"


def test_legacy_analyze_data_source(tmp_path):
    """analyze_thresholds(db_path=...) returns data_source='sell_history.db' on legacy path."""
    from tradingview.adaptive_threshold import analyze_thresholds

    # Non-existent path triggers early return with data_source still set to sell_history.db
    legacy_db = tmp_path / "sell_history.db"
    result = analyze_thresholds(db_path=legacy_db)
    assert result["data_source"] == "sell_history.db"
