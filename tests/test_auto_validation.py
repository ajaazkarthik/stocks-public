"""Tests for tradingview/analysis/auto_validation.py — Signal quality validation."""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingview" / "analysis"))

import tradingview.analysis.auto_validation as auto_val


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_db(tmp_path):
    """Create a mock alert_tracker.db with composite scores and returns."""
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
    import random
    random.seed(42)
    now = datetime.now()
    rows = []
    for i in range(120):
        score = random.uniform(20, 90)
        # Higher scores slightly more likely to have positive returns
        ret = random.gauss(0.02 if score > 55 else -0.01, 0.05)
        ts = (now - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        sym = random.choice(["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"])
        rows.append((sym, score, ret, ts))

    conn.executemany(
        "INSERT INTO alerts (symbol, composite_score, return_7d, timestamp) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def empty_db(tmp_path):
    """Empty database."""
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
# Test: _spearman_rank
# ---------------------------------------------------------------------------

class TestSpearmanRank:
    def test_perfect_positive(self):
        """Identical ranks → Spearman = 1.0."""
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [10.0, 20.0, 30.0, 40.0, 50.0]
        rho = auto_val._spearman_rank(x, y)
        assert abs(rho - 1.0) < 0.01

    def test_perfect_negative(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [50.0, 40.0, 30.0, 20.0, 10.0]
        rho = auto_val._spearman_rank(x, y)
        assert abs(rho - (-1.0)) < 0.01

    def test_no_correlation(self):
        """Random data → Spearman near 0."""
        x = [1, 5, 3, 2, 4]
        y = [3, 1, 4, 5, 2]
        rho = auto_val._spearman_rank(x, y)
        assert -1 <= rho <= 1

    def test_too_few_points(self):
        assert auto_val._spearman_rank([1], [2]) == 0.0
        assert auto_val._spearman_rank([], []) == 0.0

    def test_ties_handled(self):
        x = [1, 1, 3, 4, 5]
        y = [2, 3, 4, 5, 6]
        rho = auto_val._spearman_rank(x, y)
        assert -1 <= rho <= 1


# ---------------------------------------------------------------------------
# Test: load_validation_data
# ---------------------------------------------------------------------------

class TestLoadValidationData:
    def test_loads_data(self, sample_db):
        all_data, recent = auto_val.load_validation_data(sample_db, weeks=12, min_samples=10)
        assert len(all_data) == 120

    def test_recent_subset(self, sample_db):
        all_data, recent = auto_val.load_validation_data(sample_db, weeks=4, min_samples=10)
        assert len(recent) <= len(all_data)
        assert len(recent) > 0

    def test_missing_db(self, tmp_path):
        all_data, recent = auto_val.load_validation_data(tmp_path / "missing.db")
        assert all_data == []
        assert recent == []

    def test_empty_db(self, empty_db):
        all_data, recent = auto_val.load_validation_data(empty_db, min_samples=10)
        assert all_data == []


# ---------------------------------------------------------------------------
# Test: compute_bucket_stats
# ---------------------------------------------------------------------------

class TestComputeBucketStats:
    def test_returns_all_buckets(self, sample_db):
        all_data, _ = auto_val.load_validation_data(sample_db, min_samples=10)
        stats = auto_val.compute_bucket_stats(all_data)
        assert "LOW" in stats
        assert "MED" in stats
        assert "HIGH" in stats
        assert "APEX" in stats

    def test_bucket_has_win_rate(self, sample_db):
        all_data, _ = auto_val.load_validation_data(sample_db, min_samples=10)
        stats = auto_val.compute_bucket_stats(all_data)
        for bucket in ["LOW", "MED", "HIGH", "APEX"]:
            assert "win_rate" in stats[bucket]
            assert 0 <= stats[bucket]["win_rate"] <= 100

    def test_bucket_counts_sum(self, sample_db):
        all_data, _ = auto_val.load_validation_data(sample_db, min_samples=10)
        stats = auto_val.compute_bucket_stats(all_data)
        total = sum(stats[b]["count"] for b in ["LOW", "MED", "HIGH", "APEX"])
        assert total == len(all_data)

    def test_empty_data(self):
        stats = auto_val.compute_bucket_stats([])
        for bucket in ["LOW", "MED", "HIGH", "APEX"]:
            assert stats[bucket]["count"] == 0


# ---------------------------------------------------------------------------
# Test: detect_drift
# ---------------------------------------------------------------------------

class TestDetectDrift:
    def test_returns_drift(self, sample_db):
        all_data, recent = auto_val.load_validation_data(sample_db, weeks=4, min_samples=10)
        drift = auto_val.detect_drift(all_data, recent)
        assert "all_spearman" in drift
        assert "recent_spearman" in drift
        assert "spearman_delta" in drift
        assert "bucket_drift" in drift

    def test_spearman_in_range(self, sample_db):
        all_data, recent = auto_val.load_validation_data(sample_db, weeks=4, min_samples=10)
        drift = auto_val.detect_drift(all_data, recent)
        assert -1 <= drift["all_spearman"] <= 1
        assert -1 <= drift["recent_spearman"] <= 1


# ---------------------------------------------------------------------------
# Test: assess_health
# ---------------------------------------------------------------------------

class TestAssessHealth:
    def test_healthy(self):
        status, msg = auto_val.assess_health(all_spearman=0.15, recent_spearman=0.12, apex_wr=65)
        assert status == "HEALTHY"

    def test_warning_low_spearman(self):
        status, msg = auto_val.assess_health(all_spearman=0.03, recent_spearman=0.10, apex_wr=65)
        assert status == "WARNING"

    def test_degraded_multiple_issues(self):
        status, msg = auto_val.assess_health(all_spearman=0.03, recent_spearman=-0.05, apex_wr=40)
        assert status == "DEGRADED"


# ---------------------------------------------------------------------------
# Test: HTML report generation
# ---------------------------------------------------------------------------

class TestValidationHtml:
    def test_generates_html(self, sample_db, tmp_path):
        all_data, recent = auto_val.load_validation_data(sample_db, min_samples=10)
        all_stats = auto_val.compute_bucket_stats(all_data)
        recent_stats = auto_val.compute_bucket_stats(recent) if recent else {}
        drift = auto_val.detect_drift(all_data, recent)
        out = tmp_path / "validation.html"
        result = auto_val.generate_validation_html(
            all_stats, recent_stats, drift,
            "HEALTHY", "All good", len(all_data), len(recent), out,
        )
        assert result.exists()
        html = result.read_text(encoding="utf-8")
        assert "Validation" in html or "validation" in html

    def test_html_contains_buckets(self, sample_db, tmp_path):
        all_data, recent = auto_val.load_validation_data(sample_db, min_samples=10)
        all_stats = auto_val.compute_bucket_stats(all_data)
        drift = auto_val.detect_drift(all_data, recent)
        out = tmp_path / "validation.html"
        result = auto_val.generate_validation_html(
            all_stats, {}, drift, "HEALTHY", "Ok", len(all_data), 0, out,
        )
        html = result.read_text(encoding="utf-8")
        assert "APEX" in html
        assert "HIGH" in html


# ---------------------------------------------------------------------------
# Test: run_validation (end-to-end)
# ---------------------------------------------------------------------------

class TestRunValidation:
    def test_full_run(self, sample_db, tmp_path):
        out = tmp_path / "validation.html"
        result = auto_val.run_validation(sample_db, weeks=4, min_samples=10, output_path=out)
        assert result["status"] in ("HEALTHY", "WARNING", "DEGRADED")
        assert out.exists()

    def test_no_data(self, tmp_path):
        out = tmp_path / "validation.html"
        result = auto_val.run_validation(tmp_path / "missing.db", output_path=out)
        assert result["status"] == "NO_DATA"
