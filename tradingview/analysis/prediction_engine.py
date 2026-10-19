"""
Prediction Engine — Near-future direction forecast for stock signals.

Hybrid engine: Chronos-Bolt-Tiny (pretrained 9M-param model) as primary,
momentum ensemble as fallback.

The accuracy figures below come from a ONE-OFF OFFLINE BENCHMARK on 2024-2025
data. They are NOT recomputed at runtime and do NOT reflect live performance of
this engine:
  Chronos-Bolt-Tiny: 59.0% direction accuracy (5 stocks x 20 windows)
  Momentum Ensemble:  56.7% direction accuracy (10 stocks x 30 windows)
"""

import argparse
import functools
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Singleton model loader
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _load_chronos():
    """Load Chronos-Bolt-Tiny model (singleton). Returns None if unavailable."""
    try:
        import torch
        from chronos import ChronosBoltPipeline

        pipeline = ChronosBoltPipeline.from_pretrained(
            "amazon/chronos-bolt-tiny",
            device_map="cpu",
            dtype=torch.float32,
        )
        return pipeline
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Momentum ensemble (fallback predictor)
# ---------------------------------------------------------------------------

def _compute_rsi(prices: np.ndarray, period: int = 14) -> float:
    """Compute RSI for the last *period* deltas."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = float(np.mean(gains[-period:]))
    avg_loss = float(np.mean(losses[-period:]))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _momentum_predict(prices: np.ndarray) -> dict:
    """
    Simple momentum + mean-reversion ensemble.
    Returns dict with direction, confidence, predicted_move_pct.
    """
    if len(prices) < 50:
        return {"direction": "NEUTRAL", "confidence": 0.0, "predicted_move_pct": 0.0}

    ret5 = prices[-1] / prices[-6] - 1
    ret10 = prices[-1] / prices[-11] - 1
    ret20 = prices[-1] / prices[-21] - 1
    rsi = _compute_rsi(prices)
    sma20 = float(np.mean(prices[-20:]))
    sma50 = float(np.mean(prices[-50:]))

    # Score components (each contributes a signal vote)
    score = 0
    if ret5 > 0:
        score += 1
    if ret10 > 0:
        score += 1
    if ret20 > 0:
        score += 1
    if rsi < 30:
        score += 2  # oversold → likely bounce
    elif rsi > 70:
        score -= 2  # overbought → likely pullback
    if prices[-1] > sma20:
        score += 1
    if sma20 > sma50:
        score += 1

    # Map score to direction & confidence
    max_score = 7  # theoretical max absolute score
    if score >= 3:
        direction = "UP"
        confidence = min(abs(score) / max_score, 1.0)
    elif score <= 0:
        direction = "DOWN"
        confidence = min(abs(score) / max_score, 1.0)
    else:
        direction = "NEUTRAL"
        confidence = 0.3

    # Predicted move based on recent momentum magnitude
    avg_ret = float(np.mean([ret5, ret10, ret20]))
    predicted_move_pct = round(avg_ret * 100, 2)

    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "predicted_move_pct": predicted_move_pct,
    }


# ---------------------------------------------------------------------------
# Chronos predictor
# ---------------------------------------------------------------------------

def _chronos_predict(prices: np.ndarray, horizon: int = 7) -> dict:
    """
    Use Chronos-Bolt-Tiny to forecast *horizon* days ahead.
    Returns dict with direction, confidence, predicted_move_pct.
    """
    pipeline = _load_chronos()
    if pipeline is None:
        return None  # caller should fall back to momentum

    import torch

    context_tensor = torch.tensor(prices, dtype=torch.float32).unsqueeze(0)
    forecast = pipeline.predict(context_tensor, prediction_length=horizon)
    pred_values = forecast.numpy()

    if pred_values.ndim == 3:
        # (batch, samples, horizon) — take median of samples at last horizon step
        pred_last = float(np.median(pred_values[0, :, -1]))
        # Confidence from spread of samples
        spread = float(np.std(pred_values[0, :, -1]))
        relative_spread = spread / max(abs(prices[-1]), 1e-6)
        confidence = max(0.3, min(1.0, 1.0 - relative_spread * 10))
    else:
        pred_last = float(pred_values[0, -1])
        confidence = 0.6  # default for deterministic output

    last_price = float(prices[-1])
    move_pct = (pred_last / last_price - 1) * 100 if last_price > 0 else 0.0

    # Direction with 2% threshold (per spec)
    if pred_last > last_price * 1.02:
        direction = "UP"
    elif pred_last < last_price * 0.98:
        direction = "DOWN"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "predicted_move_pct": round(move_pct, 2),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PredictionEngine:
    """
    Hybrid prediction engine for 7-day stock direction forecast.

    Primary: Chronos-Bolt-Tiny (9M params, pretrained on diverse time series)
    Fallback: Momentum ensemble (RSI + SMA + returns)

    Usage::

        engine = PredictionEngine()
        result = engine.predict("AAPL", history_df)
        # result = {
        #     "direction": "UP",
        #     "confidence": 0.72,
        #     "predicted_move_pct": 3.14,
        #     "horizon_days": 7,
        #     "model": "chronos-bolt-tiny",
        # }
    """

    def __init__(self, horizon: int = 7, use_chronos: bool = True):
        self.horizon = horizon
        self.use_chronos = use_chronos

    def predict(self, symbol: str, history_df: pd.DataFrame) -> dict:
        """
        Predict near-future direction for *symbol*.

        Parameters
        ----------
        symbol : str
            Ticker symbol (for logging/reporting).
        history_df : pd.DataFrame
            Must contain a 'Close' column with at least 50 rows of daily prices.

        Returns
        -------
        dict with keys: direction, confidence, predicted_move_pct, horizon_days, model
        """
        if history_df is None or len(history_df) < 50:
            return {
                "direction": "NEUTRAL",
                "confidence": 0.0,
                "predicted_move_pct": 0.0,
                "horizon_days": self.horizon,
                "model": "insufficient_data",
                "symbol": symbol,
            }

        # Extract close prices as numpy array
        if "Close" in history_df.columns:
            prices = history_df["Close"].values.flatten().astype(float)
        elif "close" in history_df.columns:
            prices = history_df["close"].values.flatten().astype(float)
        else:
            return {
                "direction": "NEUTRAL",
                "confidence": 0.0,
                "predicted_move_pct": 0.0,
                "horizon_days": self.horizon,
                "model": "no_close_column",
                "symbol": symbol,
            }

        # Remove NaNs from end
        prices = prices[~np.isnan(prices)]
        if len(prices) < 50:
            return {
                "direction": "NEUTRAL",
                "confidence": 0.0,
                "predicted_move_pct": 0.0,
                "horizon_days": self.horizon,
                "model": "insufficient_data",
                "symbol": symbol,
            }

        # Try Chronos first
        model_used = "momentum-ensemble"
        result = None
        if self.use_chronos:
            t0 = time.time()
            result = _chronos_predict(prices, self.horizon)
            if result is not None:
                model_used = "chronos-bolt-tiny"
                elapsed = time.time() - t0
                result["inference_ms"] = round(elapsed * 1000, 1)

        # Fallback to momentum ensemble
        if result is None:
            t0 = time.time()
            result = _momentum_predict(prices)
            elapsed = time.time() - t0
            result["inference_ms"] = round(elapsed * 1000, 1)

        result["horizon_days"] = self.horizon
        result["model"] = model_used
        result["symbol"] = symbol
        return result


# ---------------------------------------------------------------------------
# HTML report builder
# ---------------------------------------------------------------------------

def build_html(predictions: list[dict], out_path: str) -> str:
    """Build an HTML report showing predictions for a batch of symbols."""
    rows = []
    for p in sorted(predictions, key=lambda x: x.get("confidence", 0), reverse=True):
        d = p.get("direction", "NEUTRAL")
        conf = p.get("confidence", 0)
        move = p.get("predicted_move_pct", 0)
        arrow = {"UP": "&#9650;", "DOWN": "&#9660;"}.get(d, "&#9644;")
        color = {"UP": "#22c55e", "DOWN": "#ef4444"}.get(d, "#9ca3af")
        conf_bar = round(conf * 100)
        rows.append(
            f"<tr>"
            f'<td>{p.get("symbol", "?")}</td>'
            f'<td style="color:{color};font-weight:bold">{arrow} {d}</td>'
            f"<td>{conf_bar}%</td>"
            f"<td>{move:+.2f}%</td>"
            f'<td>{p.get("model", "?")}</td>'
            f'<td>{p.get("inference_ms", "?")}ms</td>'
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prediction Engine — 7-Day Direction Forecast</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }}
  h1 {{ color: #38bdf8; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
  th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ background: #1e293b; color: #94a3b8; font-size: 0.85em; text-transform: uppercase; }}
  tr:hover {{ background: #1e293b; }}
  .note {{ color: #64748b; font-size: 0.85em; margin-top: 12px; }}
</style></head><body>
<h1>&#x1F52E; Prediction Engine — 7-Day Direction Forecast</h1>
<p class="note">Model: Chronos-Bolt-Tiny (9M params, pretrained) | Fallback: Momentum Ensemble</p>
<table>
<tr><th>Symbol</th><th>Direction</th><th>Confidence</th><th>Predicted Move</th><th>Model</th><th>Latency</th></tr>
{"".join(rows)}
</table>
<p class="note">Generated {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} | Horizon: 7 days</p>
<p class="note">Historical benchmark only &mdash; Chronos-Bolt-Tiny 59.0%, Momentum Ensemble 56.7% direction accuracy, measured once offline on 2024&ndash;2025 data. These figures are not derived from the predictions above and are not recomputed for this report.</p>
</body></html>"""

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"Report saved → {out_path}")
    return html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(args=None):
    parser = argparse.ArgumentParser(description="Prediction Engine — 7-day direction forecast")
    parser.add_argument("--symbols", default="AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,JPM,V,JNJ",
                        help="Comma-separated symbols")
    parser.add_argument("--horizon", type=int, default=7, help="Forecast horizon in days")
    parser.add_argument("--no-chronos", action="store_true", help="Skip Chronos, use momentum only")
    parser.add_argument("--out", default=str(Path(__file__).parent.parent.parent / "reports" / "prediction_report.html"),
                        help="Output HTML path")
    parsed = parser.parse_args(args)

    import yfinance as yf

    engine = PredictionEngine(horizon=parsed.horizon, use_chronos=not parsed.no_chronos)
    symbols = [s.strip() for s in parsed.symbols.split(",") if s.strip()]
    predictions = []

    print(f"Predicting {len(symbols)} symbols, horizon={parsed.horizon}d, "
          f"model={'hybrid' if not parsed.no_chronos else 'momentum-only'}")
    print()

    for sym in symbols:
        try:
            df = yf.download(sym, period="6mo", progress=False)
            if df is not None and len(df) > 50:
                result = engine.predict(sym, df)
                d = result["direction"]
                c = result["confidence"]
                m = result["predicted_move_pct"]
                arrow = {"UP": "↑", "DOWN": "↓"}.get(d, "→")
                print(f"  {sym:6s}  {arrow} {d:7s}  conf={c:.0%}  move={m:+.2f}%  ({result['model']})")
                predictions.append(result)
            else:
                print(f"  {sym:6s}  — insufficient data")
        except Exception as e:
            print(f"  {sym:6s}  ERROR: {e}")

    if predictions:
        build_html(predictions, parsed.out)

    return predictions


if __name__ == "__main__":
    main()
