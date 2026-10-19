"""
ML Signal Refiner — Data-driven signal weight optimization.
============================================================
Uses historical alerts from alert_tracker.db to train a logistic regression
(or gradient boosting) model that predicts 7-day win probability from L1–L4
scores. Outputs optimized weights to replace the fixed 30/30/20/20 allocation.

Data source: alert_tracker.db
Output:      tradingview/reports/ml_signal_report.html + ml_weights.json

Usage:
    python tradingview/analysis/ml_signal_refiner.py
    python tradingview/analysis/ml_signal_refiner.py --model xgboost --min-samples 200
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_THIS_DIR = Path(__file__).resolve().parent
_TV = _THIS_DIR.parent
_ROOT = _TV.parent
sys.path.insert(0, str(_ROOT))

try:
    from path_config import DATABASE, REPORTS
except ImportError:
    DATABASE = _ROOT / "database"
    REPORTS = _ROOT / "reports"

DB_PATH = DATABASE / "alert_tracker.db"
DEFAULT_OUT = REPORTS / "ml_signal_report.html"
WEIGHTS_OUT = REPORTS / "ml_weights.json"

# Current fixed weights for comparison
BASELINE_WEIGHTS = {"L1": 0.30, "L2": 0.30, "L3": 0.20, "L4": 0.20}


def load_training_data(
    db_path: Path = DB_PATH,
    min_samples: int = 50,
) -> tuple[list[list[float]], list[int], list[str]]:
    """
    Load training data from alert_tracker.db.

    Returns:
        (features, labels, symbols) where:
        - features: list of [l1, l2, l3, l4] score vectors
        - labels: 1 if return_7d > 0 (win), 0 otherwise
        - symbols: corresponding symbol names
    """
    if not db_path.exists():
        return [], [], []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Check available columns
    cursor = conn.execute("PRAGMA table_info(alerts)")
    columns = {row["name"] for row in cursor.fetchall()}

    # Determine score column names
    l1_col = "l1_technical" if "l1_technical" in columns else "L1_score" if "L1_score" in columns else None
    l2_col = "l2_fundamental" if "l2_fundamental" in columns else "L2_score" if "L2_score" in columns else None
    l3_col = "l3_sentiment" if "l3_sentiment" in columns else "L3_score" if "L3_score" in columns else None
    l4_col = "l4_market" if "l4_market" in columns else "L4_score" if "L4_score" in columns else None

    if not all([l1_col, l2_col, l3_col, l4_col]):
        conn.close()
        return [], [], []

    return_col = "return_7d" if "return_7d" in columns else None
    if not return_col:
        conn.close()
        return [], [], []

    query = f"""
        SELECT symbol, "{l1_col}" as l1, "{l2_col}" as l2,
               "{l3_col}" as l3, "{l4_col}" as l4, {return_col}
        FROM alerts
        WHERE {return_col} IS NOT NULL
          AND "{l1_col}" IS NOT NULL
          AND "{l2_col}" IS NOT NULL
    """

    rows = conn.execute(query).fetchall()
    conn.close()

    features = []
    labels = []
    symbols = []

    for row in rows:
        l1 = float(row["l1"] or 0)
        l2 = float(row["l2"] or 0)
        l3 = float(row["l3"] or 0)
        l4 = float(row["l4"] or 0)
        ret = float(row["return_7d"])
        symbol = row["symbol"] or ""

        features.append([l1, l2, l3, l4])
        labels.append(1 if ret > 0 else 0)
        symbols.append(symbol)

    if len(features) < min_samples:
        return [], [], []

    return features, labels, symbols


# ── Pure-Python Logistic Regression ───────────────────────────────────────────

def _sigmoid(x: float) -> float:
    """Sigmoid function with overflow protection."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    else:
        ez = math.exp(x)
        return ez / (1.0 + ez)


def _normalize_features(features: list[list[float]]) -> tuple[list[list[float]], list[float], list[float]]:
    """Z-score normalize features. Returns (normalized, means, stds)."""
    n = len(features)
    if n == 0:
        return features, [], []

    k = len(features[0])
    means = [sum(features[i][j] for i in range(n)) / n for j in range(k)]
    stds = []
    for j in range(k):
        var = sum((features[i][j] - means[j]) ** 2 for i in range(n)) / max(n - 1, 1)
        stds.append(math.sqrt(var) if var > 0 else 1.0)

    normalized = [
        [(features[i][j] - means[j]) / stds[j] for j in range(k)]
        for i in range(n)
    ]
    return normalized, means, stds


def train_logistic_regression(
    features: list[list[float]],
    labels: list[int],
    learning_rate: float = 0.01,
    epochs: int = 1000,
    reg_lambda: float = 0.01,
) -> tuple[list[float], float, dict]:
    """
    Train a logistic regression model using gradient descent.

    Args:
        features: N x 4 feature matrix [l1, l2, l3, l4]
        labels: Binary labels (1=win, 0=loss)
        learning_rate: SGD learning rate
        epochs: Number of training iterations
        reg_lambda: L2 regularization strength

    Returns:
        (weights, bias, metrics_dict)
    """
    n = len(features)
    if n == 0:
        return [0.25, 0.25, 0.25, 0.25], 0.0, {}

    k = len(features[0])

    # Normalize
    norm_features, means, stds = _normalize_features(features)

    # Initialize weights
    weights = [0.0] * k
    bias = 0.0

    # Training loop
    losses = []
    for epoch in range(epochs):
        total_loss = 0.0
        dw = [0.0] * k
        db = 0.0

        for i in range(n):
            z = sum(weights[j] * norm_features[i][j] for j in range(k)) + bias
            pred = _sigmoid(z)
            error = pred - labels[i]

            for j in range(k):
                dw[j] += error * norm_features[i][j]
            db += error

            # Binary cross-entropy loss
            pred_clamped = max(min(pred, 1 - 1e-7), 1e-7)
            total_loss += -(labels[i] * math.log(pred_clamped) + (1 - labels[i]) * math.log(1 - pred_clamped))

        # Update weights with L2 regularization
        for j in range(k):
            weights[j] -= learning_rate * (dw[j] / n + reg_lambda * weights[j])
        bias -= learning_rate * db / n

        avg_loss = total_loss / n
        losses.append(avg_loss)

    # Compute accuracy
    correct = 0
    predictions = []
    for i in range(n):
        z = sum(weights[j] * norm_features[i][j] for j in range(k)) + bias
        pred = _sigmoid(z)
        predictions.append(pred)
        if (pred >= 0.5) == (labels[i] == 1):
            correct += 1

    accuracy = correct / n * 100

    # Convert normalized weights to real-scale weights
    real_weights = [weights[j] / stds[j] if stds[j] > 0 else 0 for j in range(k)]

    # Normalize to percentages that sum to 100
    abs_sum = sum(abs(w) for w in real_weights)
    if abs_sum > 0:
        pct_weights = [abs(w) / abs_sum for w in real_weights]
    else:
        pct_weights = [0.25] * k

    metrics = {
        "accuracy": accuracy,
        "final_loss": losses[-1] if losses else 0,
        "n_samples": n,
        "n_wins": sum(labels),
        "n_losses": n - sum(labels),
        "baseline_win_rate": sum(labels) / n * 100,
        "raw_weights": real_weights,
        "normalized_weights": pct_weights,
        "means": means,
        "stds": stds,
    }

    return pct_weights, bias, metrics


def try_sklearn_model(
    features: list[list[float]],
    labels: list[int],
    model_type: str = "logistic",
) -> Optional[tuple[list[float], dict]]:
    """
    Try to use sklearn if available for better model quality.

    Returns:
        (weights, metrics) or None if sklearn not available
    """
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
        import numpy as np

        X = np.array(features)
        y = np.array(labels)

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        if model_type == "logistic":
            model = LogisticRegression(max_iter=1000, C=1.0, random_state=42)
        else:
            try:
                from sklearn.ensemble import GradientBoostingClassifier
                model = GradientBoostingClassifier(n_estimators=100, max_depth=3, random_state=42)
            except ImportError:
                model = LogisticRegression(max_iter=1000, C=1.0, random_state=42)

        # Cross-validation
        cv_scores = cross_val_score(model, X_scaled, y, cv=min(5, len(y) // 10), scoring="accuracy")

        # Fit on all data for feature importances
        model.fit(X_scaled, y)

        # Extract weights
        if hasattr(model, "coef_"):
            raw_weights = model.coef_[0].tolist()
        elif hasattr(model, "feature_importances_"):
            raw_weights = model.feature_importances_.tolist()
        else:
            raw_weights = [0.25] * 4

        abs_sum = sum(abs(w) for w in raw_weights)
        pct_weights = [abs(w) / abs_sum for w in raw_weights] if abs_sum > 0 else [0.25] * 4

        metrics = {
            "model_type": model_type,
            "cv_accuracy_mean": float(cv_scores.mean() * 100),
            "cv_accuracy_std": float(cv_scores.std() * 100),
            "n_samples": len(features),
            "raw_weights": raw_weights,
            "sklearn_available": True,
        }

        return pct_weights, metrics

    except ImportError:
        return None


def compare_with_baseline(
    features: list[list[float]],
    labels: list[int],
    new_weights: list[float],
) -> dict:
    """Compare new weights against baseline fixed weights."""
    baseline = [BASELINE_WEIGHTS["L1"], BASELINE_WEIGHTS["L2"], BASELINE_WEIGHTS["L3"], BASELINE_WEIGHTS["L4"]]

    def score_with_weights(f: list[float], w: list[float]) -> float:
        # Normalize weights to sum to 1
        ws = sum(w)
        if ws == 0:
            return 0
        return sum(fi * wi / ws for fi, wi in zip(f, w))

    baseline_correct = 0
    new_correct = 0
    n = len(features)

    for i in range(n):
        baseline_score = score_with_weights(features[i], baseline)
        new_score = score_with_weights(features[i], new_weights)

        baseline_pred = 1 if baseline_score >= 0.55 else 0
        new_pred = 1 if new_score >= 0.55 else 0

        if baseline_pred == labels[i]:
            baseline_correct += 1
        if new_pred == labels[i]:
            new_correct += 1

    return {
        "baseline_accuracy": baseline_correct / n * 100 if n > 0 else 0,
        "new_accuracy": new_correct / n * 100 if n > 0 else 0,
        "improvement": (new_correct - baseline_correct) / n * 100 if n > 0 else 0,
    }


def generate_ml_report(
    weights: list[float],
    metrics: dict,
    comparison: dict,
    sklearn_result: Optional[tuple] = None,
    output_path: Path = DEFAULT_OUT,
) -> Path:
    """Generate HTML report showing ML analysis results."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    weight_names = ["L1 Technical", "L2 Fundamental", "L3 Sentiment", "L4 Market"]
    baseline_vals = [30, 30, 20, 20]

    weights_comparison = ""
    for i, name in enumerate(weight_names):
        new_pct = weights[i] * 100
        old_pct = baseline_vals[i]
        delta = new_pct - old_pct
        color = "#3fb950" if delta > 0 else "#f85149" if delta < 0 else "#8b949e"
        weights_comparison += f"""<tr>
  <td>{name}</td>
  <td>{old_pct:.0f}%</td>
  <td style="color:{color}; font-weight:bold">{new_pct:.1f}%</td>
  <td style="color:{color}">{'+'if delta>0 else ''}{delta:.1f}%</td>
</tr>"""

    sklearn_section = ""
    if sklearn_result:
        sk_weights, sk_metrics = sklearn_result
        sklearn_section = f"""
<h2>sklearn Model Results</h2>
<div class="grid">
  <div class="card"><div class="value ok">{sk_metrics['cv_accuracy_mean']:.1f}%</div><div class="label">CV Accuracy</div></div>
  <div class="card"><div class="value neutral">±{sk_metrics['cv_accuracy_std']:.1f}%</div><div class="label">CV Std Dev</div></div>
  <div class="card"><div class="value neutral">{sk_metrics['model_type']}</div><div class="label">Model Type</div></div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ML Signal Refinement</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; background: #0d1117; color: #c9d1d9; }}
  h1,h2 {{ color: #58a6ff; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }}
  .card .value {{ font-size: 1.8em; font-weight: bold; }}
  .card .label {{ font-size: 0.85em; color: #8b949e; }}
  .ok {{ color: #3fb950; }}
  .warn {{ color: #d29922; }}
  .danger {{ color: #f85149; }}
  .neutral {{ color: #58a6ff; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th,td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: center; }}
  th {{ background: #161b22; color: #58a6ff; }}
  footer {{ margin-top: 20px; color: #484f58; font-size: 0.85em; }}
</style>
</head><body>
<h1>🤖 ML Signal Refinement</h1>
<p>{metrics.get('n_samples', 0)} training samples | Baseline win rate: {metrics.get('baseline_win_rate', 0):.1f}%</p>

<div class="grid">
  <div class="card"><div class="value {'ok' if metrics.get('accuracy',0)>55 else 'warn'}">{metrics.get('accuracy', 0):.1f}%</div><div class="label">Model Accuracy</div></div>
  <div class="card"><div class="value neutral">{metrics.get('n_samples', 0)}</div><div class="label">Training Samples</div></div>
  <div class="card"><div class="value {'ok' if comparison.get('improvement',0)>0 else 'danger'}">{comparison.get('improvement', 0):+.1f}%</div><div class="label">vs Baseline</div></div>
  <div class="card"><div class="value warn">{metrics.get('final_loss', 0):.4f}</div><div class="label">Final Loss</div></div>
</div>

<h2>Weight Comparison</h2>
<table>
<thead><tr><th>Layer</th><th>Current</th><th>ML Optimized</th><th>Change</th></tr></thead>
<tbody>{weights_comparison}</tbody>
</table>

{sklearn_section}

<h2>Recommendation</h2>
<p>{'✅ ML weights show improvement — consider adopting.' if comparison.get('improvement', 0) > 2 else '⚠️ Marginal improvement — keep baseline weights for stability.' if comparison.get('improvement', 0) > 0 else '❌ No improvement — baseline weights are better.'}</p>

<footer>ML Signal Refiner v1.0 | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</footer>
</body></html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"✅ ML report saved to {output_path}")
    return output_path


def save_weights(weights: list[float], metrics: dict, output_path: Path = WEIGHTS_OUT) -> Path:
    """Save optimized weights to JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "weights": {
            "L1_technical": weights[0],
            "L2_fundamental": weights[1],
            "L3_sentiment": weights[2],
            "L4_market": weights[3],
        },
        "metrics": {k: v for k, v in metrics.items() if isinstance(v, (int, float, str, bool))},
        "generated": datetime.now().isoformat(),
    }
    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return output_path


def main(args: Optional[argparse.Namespace] = None) -> None:
    if args is None:
        parser = argparse.ArgumentParser(description="ML Signal Weight Optimizer")
        parser.add_argument("--model", choices=["logistic", "xgboost"], default="logistic")
        parser.add_argument("--min-samples", type=int, default=50, help="Minimum training samples required")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output HTML path")
        parser.add_argument("--save-weights", action="store_true", help="Save optimized weights to JSON")
        args = parser.parse_args()

    print("🤖 Loading training data from alert_tracker.db...")
    features, labels, symbols = load_training_data(DB_PATH, args.min_samples)

    if not features:
        print(f"❌ Not enough training data (need ≥{args.min_samples} samples with return_7d)")
        return

    print(f"   {len(features)} samples loaded ({sum(labels)} wins, {len(labels)-sum(labels)} losses)")

    # Train logistic regression
    print("   Training logistic regression...")
    weights, bias, metrics = train_logistic_regression(features, labels)

    # Try sklearn if available
    sklearn_result = try_sklearn_model(features, labels, args.model)
    if sklearn_result:
        print(f"   sklearn {args.model}: CV accuracy {sklearn_result[1]['cv_accuracy_mean']:.1f}%")

    # Compare with baseline
    comparison = compare_with_baseline(features, labels, weights)
    print(f"   Baseline accuracy: {comparison['baseline_accuracy']:.1f}%")
    print(f"   ML accuracy: {comparison['new_accuracy']:.1f}%")
    print(f"   Improvement: {comparison['improvement']:+.1f}%")

    # Generate report
    generate_ml_report(weights, metrics, comparison, sklearn_result, Path(args.out))

    # Save weights
    if args.save_weights:
        save_weights(weights, metrics)
        print(f"   Weights saved to {WEIGHTS_OUT}")


if __name__ == "__main__":
    main()
