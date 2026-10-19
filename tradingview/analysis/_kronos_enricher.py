"""Kronos FM enricher — OHLCV next-candle direction prediction.

Wraps the official KronosPredictor (NeoQuasar/Kronos-mini, 4.1M params).
Provides:
  compute_kronos_data(tv_sym, yf_ticker)  → dict used by signal_stack.py
  kronos_html_badge(data)                 → HTML block for actionable report

Benchmarked: 35.9ms CPU / ticker (after model load).  Model load: ~2.4s (once).
Direction accuracy: 46.7% on synthetic walk-forward (trial only — see
system-integration-golden-rules.instructions.md §2.10).

The badge is INFORMATIONAL only.  Kronos adds NO weight to the composite
score — its trial direction signal appears visually in the report but does
not shift the L1-L4 calculation.  When conviction is proven on real market
data, a formal L-weight can be assigned.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

# ─── Lazy model cache (singleton) ────────────────────────────────────────────
_PREDICTOR = None   # KronosPredictor instance (loaded once)
_LOAD_TIME_MS: float = 0.0   # first-load latency for reporting
_KRONOS_IMPORT_ERROR: Optional[str] = None

_ROOT = Path(__file__).parent.parent.parent   # d:/workspace/repo/stocks
_KRONOS_SRC = _ROOT / "tradingview" / "analysis" / "kronos_src"


def _get_predictor():
    """Lazy-load KronosPredictor once; return None on any failure."""
    global _PREDICTOR, _LOAD_TIME_MS, _KRONOS_IMPORT_ERROR
    if _PREDICTOR is not None:
        return _PREDICTOR
    if _KRONOS_IMPORT_ERROR is not None:
        return None   # already tried, skip
    try:
        # Ensure project root is on sys.path for the relative import inside kronos_src
        _root_str = str(_ROOT)
        if _root_str not in sys.path:
            sys.path.insert(0, _root_str)
        from tradingview.analysis.kronos_src import Kronos, KronosTokenizer, KronosPredictor
        _t0 = time.time()
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        model     = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
        _PREDICTOR = KronosPredictor(model, tokenizer, device="cpu", max_context=128)
        _LOAD_TIME_MS = round((time.time() - _t0) * 1000, 1)
    except Exception as exc:
        _KRONOS_IMPORT_ERROR = str(exc)
        _PREDICTOR = None
    return _PREDICTOR


def compute_kronos_data(tv_sym: str, yf_ticker: str = "") -> dict:
    """Fetch 64-day OHLCV and run Kronos next-candle prediction.

    Returns a dict:
      {
        "available": True/False,
        "direction": "UP" | "DOWN",
        "pred_close": float,
        "last_close": float,
        "change_pct": float,     # predicted % change
        "latency_ms": float,
        "load_ms":    float,     # model load time (only on first call)
        "error":      str | None,
      }
    """
    result: dict = {
        "available": False,
        "direction": None,
        "pred_close": None,
        "last_close": None,
        "change_pct": None,
        "latency_ms": 0.0,
        "load_ms":    _LOAD_TIME_MS,
        "error": None,
    }
    predictor = _get_predictor()
    if predictor is None:
        result["error"] = _KRONOS_IMPORT_ERROR or "Kronos model unavailable"
        return result

    # yfinance symbol — use yf_ticker if provided, otherwise derive from tv_sym
    _tick = yf_ticker or _tv_sym_to_yf(tv_sym)
    if not _tick:
        result["error"] = "Cannot derive yfinance ticker"
        return result

    try:
        import yfinance as yf
        import pandas as pd
        df_raw = yf.Ticker(_tick).history(period="90d", interval="1d", auto_adjust=True)
        if df_raw is None or len(df_raw) < 32:
            result["error"] = f"Insufficient price history ({len(df_raw) if df_raw is not None else 0} bars)"
            return result
        # Normalise column names to lowercase
        df = df_raw.copy()
        df.columns = [c.lower() for c in df.columns]
        # Keep only OHLCV + Amount (amount = close*volume approximation)
        if "amount" not in df.columns:
            df["amount"] = df["close"] * df["volume"]
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col not in df.columns:
                result["error"] = f"Missing column: {col}"
                return result
        df = df[["open", "high", "low", "close", "volume", "amount"]].dropna()
        # Use up to 64 bars
        df = df.iloc[-64:]
        n = len(df)
        if n < 32:
            result["error"] = f"Too few bars after dropna: {n}"
            return result

        # Build timestamps as pd.Series (CRITICAL — not DatetimeIndex)
        x_ts = pd.Series(df.index.normalize())
        next_date = df.index[-1] + pd.Timedelta(days=1)
        y_ts = pd.Series(pd.date_range(next_date, periods=1, freq="D"))

        last_close = float(df["close"].iloc[-1])
        _t0 = time.time()
        pred = predictor.predict(df, x_ts, y_ts, pred_len=1, verbose=False)
        latency_ms = round((time.time() - _t0) * 1000, 1)

        pred_close = float(pred["close"].iloc[0])
        change_pct = round((pred_close - last_close) / last_close * 100, 2) if last_close else 0.0
        direction = "UP" if pred_close >= last_close else "DOWN"
        # Flag moves >10% as high uncertainty — single-day moves this large are rare;
        # they typically indicate high intra-window std (trending/volatile stocks) causing
        # the model's normalised output to denormalise to an exaggerated absolute change.
        high_uncertainty = abs(change_pct) > 10.0

        result.update({
            "available":         True,
            "direction":         direction,
            "pred_close":        round(pred_close, 4),
            "last_close":        round(last_close, 4),
            "change_pct":        change_pct,
            "high_uncertainty":  high_uncertainty,
            "latency_ms":        latency_ms,
            "load_ms":           _LOAD_TIME_MS,
            "error":             None,
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        # Non-fatal — traceback only in debug mode
        if os.environ.get("KRONOS_DEBUG"):
            traceback.print_exc()
    return result


def _tv_sym_to_yf(tv_sym: str) -> str:
    """Best-effort TV symbol → yfinance ticker conversion."""
    if not tv_sym:
        return ""
    parts = tv_sym.split(":")
    base  = parts[-1]
    exch  = parts[0].upper() if len(parts) > 1 else ""
    if exch in ("ASX",):
        return base + ".AX"
    if exch in ("NSE", "NSE_DLY"):
        return base + ".NS"
    if exch in ("BSE", "BSE_DLY"):
        return base + ".BO"
    if exch in ("SGX",):
        return base + ".SI"
    if exch in ("HKEX",):
        # HK tickers are 4-digit padded
        return base.lstrip("0").zfill(4) + ".HK"
    # US and others — return bare ticker
    return base


# ─── HTML badge renderer ──────────────────────────────────────────────────────
def kronos_html_badge(data: dict) -> str:
    """Return an HTML block to embed in the signal card.

    Renders nothing when data is empty or unavailable.
    """
    if not data or not data.get("available"):
        # Still show a dimmed badge so users know Kronos was attempted
        err = data.get("error") if data else "unavailable"
        if not err:
            return ""
        return (
            f'<div class="kronos-badge" style="opacity:0.45">'
            f'<span class="kronos-icon">🔮</span>'
            f'<strong>Kronos FM</strong>'
            f'<span class="kronos-status dim">unavailable</span>'
            f'<span class="kronos-err" title="{_esc(err)}">ⓘ</span>'
            f'</div>'
        )

    direction        = data.get("direction", "?")
    change_pct       = data.get("change_pct", 0.0)
    latency_ms       = data.get("latency_ms", 0.0)
    high_uncertainty = data.get("high_uncertainty", False)
    arrow            = "↑" if direction == "UP" else "↓"
    color            = "#22c55e" if direction == "UP" else "#ef4444"
    # Dim color slightly for high-uncertainty predictions
    if high_uncertainty:
        color = "#86efac" if direction == "UP" else "#fca5a5"
    sign    = "+" if (change_pct or 0) >= 0 else ""
    pct_str = f"{sign}{change_pct:.2f}%" if change_pct is not None else "N/A"
    load_note = (f' <span class="kronos-load" title="Model load time">(load {data.get("load_ms",0):.0f}ms)</span>'
                 if data.get("load_ms", 0) > 500 else "")
    uncertainty_note = (
        '<span class="kronos-warn" title="Predicted move >10%: high window volatility inflates magnitude. '
        'Direction may still be valid but the % is unreliable.">⚠ high σ</span>'
        if high_uncertainty else ""
    )

    return (
        f'<div class="kronos-badge{" kronos-uncertain" if high_uncertainty else ""}">' 
        f'<span class="kronos-icon">🔮</span>'
        f'<strong>Kronos FM</strong>'
        f'<span class="kronos-dir" style="color:{color};font-weight:700">'
        f'{arrow} {direction}</span>'
        f'<span class="kronos-pct" style="color:{color}">{pct_str}</span>'
        f'{uncertainty_note}'
        f'<span class="kronos-lat" title="Inference latency">{latency_ms:.0f}ms</span>'
        f'{load_note}'
        f'<span class="kronos-trial">TRIAL</span>'
        f'</div>'
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


# ─── CSS (imported by signal_stack.py for the <style> block) ─────────────────
KRONOS_CSS = """
/* ── Kronos FM badge ─────────────────────────────────────────────────────── */
.kronos-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  background: #1e1b4b;
  border: 1px solid #4338ca55;
  border-radius: 8px;
  padding: 8px 14px;
  margin: 8px 0;
  font-size: 0.82rem;
  color: #c7d2fe;
  flex-wrap: wrap;
}
.kronos-icon { font-size: 1.1rem; }
.kronos-dir  { font-size: 0.95rem; letter-spacing: 0.5px; }
.kronos-pct  { font-size: 0.88rem; }
.kronos-lat  { color: #818cf8; font-size: 0.75rem; margin-left: auto; }
.kronos-load { color: #6366f188; font-size: 0.7rem; }
.kronos-trial {
  background: #7c3aed33;
  color: #a78bfa;
  padding: 1px 6px;
  border-radius: 6px;
  font-size: 0.68rem;
  border: 1px solid #7c3aed55;
  letter-spacing: 0.5px;
}
.kronos-err  { cursor: help; color: #f87171; }
.kronos-badge.dim { color: #64748b; }
.kronos-warn {
  background: #78350f44;
  color: #fbbf24;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 0.7rem;
  border: 1px solid #92400e66;
  cursor: help;
  white-space: nowrap;
}
.kronos-uncertain {
  border-color: #92400e66;
  background: #1c1007;
  opacity: 0.88;
}
"""
