"""Tests for tradingview/analysis/ml_signal_refiner.py — ML weight optimization."""
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingview" / "analysis"))

import tradingview.analysis.ml_signal_refiner as ml_refiner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_db(tmp_path):
    """Create a mock alert_tracker.db with L1-L4 scores and return_7d."""
    db_path = tmp_path / "alert_tracker.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            l1_technical REAL,
            l2_fundamental REAL,
            l3_sentiment REAL,
            l4_market REAL,
            composite_score REAL,
            return_7d REAL,
            timestamp TEXT
        )
    """)
    import random
    random.seed(42)
    now = datetime.now()
    rows = []
    for i in range(100):
        l1 = random.uniform(10, 30)
        l2 = random.uniform(10, 30)
        l3 = random.uniform(5, 20)
        l4 = random.uniform(5, 20)
        score = l1 + l2 + l3 + l4
        # Higher scores slightly more likely to win
        ret = random.gauss(0.02 if score > 55 else -0.01, 0.05)
        ts = (now - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        sym = random.choice(["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"])
        rows.append((sym, l1, l2, l3, l4, score, ret, ts))

    conn.executemany(
        "INSERT INTO alerts (symbol, l1_technical, l2_fundamental, l3_sentiment, l4_market, composite_score, return_7d, timestamp) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def small_db(tmp_path):
    """DB with too few samples."""
    db_path = tmp_path / "alert_tracker.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            l1_technical REAL,
            l2_fundamental REAL,
            l3_sentiment REAL,
            l4_market REAL,
            return_7d REAL
        )
    """)
    conn.execute("INSERT INTO alerts (symbol, l1_technical, l2_fundamental, l3_sentiment, l4_market, return_7d) VALUES (?,?,?,?,?,?)",
                 ("AAPL", 20, 20, 10, 10, 0.05))
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def sample_features_labels():
    """Pre-built features and labels."""
    import random
    random.seed(42)
    features = [[random.uniform(10, 30), random.uniform(10, 30),
                 random.uniform(5, 20), random.uniform(5, 20)] for _ in range(80)]
    labels = [1 if sum(f) > 55 else 0 for f in features]
    return features, labels


# ---------------------------------------------------------------------------
# Test: load_training_data
# ---------------------------------------------------------------------------

class TestLoadTrainingData:
    def test_loads_from_db(self, sample_db):
        features, labels, symbols = ml_refiner.load_training_data(sample_db, min_samples=10)
        assert len(features) == 100
        assert len(labels) == 100
        assert len(symbols) == 100

    def test_features_are_4d(self, sample_db):
        features, labels, symbols = ml_refiner.load_training_data(sample_db, min_samples=10)
        for f in features:
            assert len(f) == 4

    def test_labels_are_binary(self, sample_db):
        _, labels, _ = ml_refiner.load_training_data(sample_db, min_samples=10)
        for label in labels:
            assert label in (0, 1)

    def test_returns_empty_for_missing_db(self, tmp_path):
        f, l, s = ml_refiner.load_training_data(tmp_path / "missing.db")
        assert f == []

    def test_returns_empty_for_insufficient_data(self, small_db):
        f, l, s = ml_refiner.load_training_data(small_db, min_samples=50)
        assert f == []


# ---------------------------------------------------------------------------
# Test: _sigmoid
# ---------------------------------------------------------------------------

class TestSigmoid:
    def test_sigmoid_zero(self):
        assert ml_refiner._sigmoid(0) == pytest.approx(0.5)

    def test_sigmoid_large_positive(self):
        assert ml_refiner._sigmoid(100) == pytest.approx(1.0)

    def test_sigmoid_large_negative(self):
        assert ml_refiner._sigmoid(-100) == pytest.approx(0.0)

    def test_sigmoid_range(self):
        for x in [-10, -1, 0, 1, 10]:
            val = ml_refiner._sigmoid(x)
            assert 0 <= val <= 1


# ---------------------------------------------------------------------------
# Test: _normalize_features
# ---------------------------------------------------------------------------

class TestNormalizeFeatures:
    def test_normalized_mean_near_zero(self):
        features = [[1, 2], [3, 4], [5, 6]]
        normalized, means, stds = ml_refiner._normalize_features(features)
        # Mean of normalized should be ~0
        col0_mean = sum(r[0] for r in normalized) / len(normalized)
        assert abs(col0_mean) < 0.01

    def test_returns_means_and_stds(self):
        features = [[10, 20], [30, 40]]
        _, means, stds = ml_refiner._normalize_features(features)
        assert len(means) == 2
        assert len(stds) == 2
        assert means[0] == pytest.approx(20.0)

    def test_empty_features(self):
        normalized, means, stds = ml_refiner._normalize_features([])
        assert normalized == []


# ---------------------------------------------------------------------------
# Test: train_logistic_regression
# ---------------------------------------------------------------------------

class TestTrainLogisticRegression:
    def test_returns_weights(self, sample_features_labels):
        features, labels = sample_features_labels
        weights, bias, metrics = ml_refiner.train_logistic_regression(features, labels)
        assert len(weights) == 4
        assert isinstance(bias, float)

    def test_weights_sum_to_one(self, sample_features_labels):
        features, labels = sample_features_labels
        weights, _, _ = ml_refiner.train_logistic_regression(features, labels)
        assert abs(sum(weights) - 1.0) < 0.01

    def test_weights_are_positive(self, sample_features_labels):
        features, labels = sample_features_labels
        weights, _, _ = ml_refiner.train_logistic_regression(features, labels)
        for w in weights:
            assert w >= 0

    def test_metrics_contain_accuracy(self, sample_features_labels):
        features, labels = sample_features_labels
        _, _, metrics = ml_refiner.train_logistic_regression(features, labels)
        assert "accuracy" in metrics
        assert 0 <= metrics["accuracy"] <= 100

    def test_metrics_contain_loss(self, sample_features_labels):
        _, _, metrics = ml_refiner.train_logistic_regression(*sample_features_labels)
        assert "final_loss" in metrics
        assert metrics["final_loss"] >= 0

    def test_empty_data(self):
        weights, _, _ = ml_refiner.train_logistic_regression([], [])
        assert weights == [0.25, 0.25, 0.25, 0.25]


# ---------------------------------------------------------------------------
# Test: compare_with_baseline
# ---------------------------------------------------------------------------

class TestCompareWithBaseline:
    def test_returns_comparison(self, sample_features_labels):
        features, labels = sample_features_labels
        new_weights = [0.35, 0.25, 0.20, 0.20]
        result = ml_refiner.compare_with_baseline(features, labels, new_weights)
        assert "baseline_accuracy" in result
        assert "new_accuracy" in result
        assert "improvement" in result

    def test_same_weights_no_improvement(self, sample_features_labels):
        features, labels = sample_features_labels
        baseline = [0.30, 0.30, 0.20, 0.20]
        result = ml_refiner.compare_with_baseline(features, labels, baseline)
        assert result["improvement"] == 0.0


# ---------------------------------------------------------------------------
# Test: HTML report generation
# ---------------------------------------------------------------------------

class TestMlReportGeneration:
    def test_generates_html(self, tmp_path):
        weights = [0.35, 0.25, 0.22, 0.18]
        metrics = {"accuracy": 62.5, "final_loss": 0.65, "n_samples": 100, "baseline_win_rate": 55.0}
        comparison = {"baseline_accuracy": 58.0, "new_accuracy": 62.5, "improvement": 4.5}

        out = tmp_path / "ml_report.html"
        result = ml_refiner.generate_ml_report(weights, metrics, comparison, output_path=out)
        assert result.exists()
        html = result.read_text(encoding="utf-8")
        assert "ML Signal" in html

    def test_html_contains_weights(self, tmp_path):
        weights = [0.35, 0.25, 0.22, 0.18]
        metrics = {"accuracy": 62.5, "final_loss": 0.65, "n_samples": 100, "baseline_win_rate": 55.0}
        comparison = {"baseline_accuracy": 58.0, "new_accuracy": 62.5, "improvement": 4.5}

        out = tmp_path / "ml_report.html"
        result = ml_refiner.generate_ml_report(weights, metrics, comparison, output_path=out)
        html = result.read_text(encoding="utf-8")
        assert "Technical" in html or "L1" in html


# ---------------------------------------------------------------------------
# Test: save_weights
# ---------------------------------------------------------------------------

class TestSaveWeights:
    def test_saves_json(self, tmp_path):
        weights = [0.35, 0.25, 0.22, 0.18]
        metrics = {"accuracy": 62.5}
        out = tmp_path / "weights.json"
        result = ml_refiner.save_weights(weights, metrics, out)
        assert result.exists()
        import json
        data = json.loads(result.read_text(encoding="utf-8"))
        assert "weights" in data
        assert data["weights"]["L1_technical"] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestMlRefinerIntegration:
    def test_full_pipeline(self, sample_db, tmp_path):
        """End-to-end: load → train → compare → report."""
        features, labels, symbols = ml_refiner.load_training_data(sample_db, min_samples=10)
        assert len(features) > 0

        weights, bias, metrics = ml_refiner.train_logistic_regression(features, labels)
        assert len(weights) == 4

        comparison = ml_refiner.compare_with_baseline(features, labels, weights)
        assert "improvement" in comparison

        out = tmp_path / "ml_report.html"
        result = ml_refiner.generate_ml_report(weights, metrics, comparison, output_path=out)
        assert result.exists()
