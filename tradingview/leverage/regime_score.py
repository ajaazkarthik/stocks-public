"""
RegimeScore Engine — Dynamic Leverage Multiplier
=================================================
Computes a composite market regime score (0–10) that determines how much
leverage is safe to deploy in the tactical 20% high-risk bucket.

Score interpretation
--------------------
  >= 8.0   STRONG BULL  → aggressive leverage (2x–5x, DLCs OK)
  6.0–7.9  MILD BULL    → moderate leverage (1.5x–2x, leveraged ETFs)
  4.0–5.9  NEUTRAL      → 1x only, no leverage products
  < 4.0    BEAR/CRASH   → de-risk, exit all leverage, consider inverse

Component weights (tunable via WEIGHTS)
-----------------------------------------
  Volatility (VIX)     35%   — primary gating signal
  Trend (MA)           25%   — structural regime
  Momentum (RSI/MACD)  25%   — entry timing within trend
  Macro (yield curve)  15%   — slow-moving tail risk filter

Usage
-----
  from leverage.regime_score import RegimeEngine

  engine = RegimeEngine()
  result = engine.compute("SPY")           # live mode (fetches yfinance + VIX)
  print(result["score"])                   # 0.0 – 10.0
  print(result["leverage_multiplier"])     # 0.5 | 1.0 | 1.5 | 2.0 | 3.0
  print(result["recommendation"])          # human-readable label
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError as e:
    sys.exit(f"pip install yfinance pandas numpy  ({e})")

# ── Pull VIX from the parent signal_stack if available ────────────────────────
_PARENT = Path(__file__).parent.parent
sys.path.insert(0, str(_PARENT))
try:
    from tv_screener_fundamentals import fetch_vix_level as _fetch_vix_live
    _VIX_LIVE_OK = True
except ImportError:
    _VIX_LIVE_OK = False
    def _fetch_vix_live() -> Optional[float]:  # type: ignore[misc]
        return None

# ── Instrument catalogue ───────────────────────────────────────────────────────
# Each entry: (description, leverage_multiple, decay_class, markets)
# decay_class: "high" = daily reset (ETF), "none" = futures/margin, "medium" = options
INSTRUMENT_CATALOGUE: dict[str, dict] = {
    # US Broad
    "SSO":   {"desc": "2x S&P500 ETF",         "lev": 2.0, "decay": "high",   "market": "US"},
    "UPRO":  {"desc": "3x S&P500 ETF (UPRO)",   "lev": 3.0, "decay": "high",   "market": "US"},
    "QLD":   {"desc": "2x QQQ ETF",             "lev": 2.0, "decay": "high",   "market": "US"},
    "TQQQ":  {"desc": "3x QQQ ETF (TQQQ)",      "lev": 3.0, "decay": "high",   "market": "US"},
    # US Sector
    "SOXL":  {"desc": "3x Semis (SOXL)",         "lev": 3.0, "decay": "high",   "market": "US"},
    "TECL":  {"desc": "3x Tech (TECL)",           "lev": 3.0, "decay": "high",   "market": "US"},
    "FNGU":  {"desc": "3x FANG+ (FNGU)",          "lev": 3.0, "decay": "high",   "market": "US"},
    "LABU":  {"desc": "3x Biotech (LABU)",         "lev": 3.0, "decay": "high",   "market": "US"},
    # Commodities
    "UCO":   {"desc": "2x Crude Oil (UCO)",        "lev": 2.0, "decay": "high",   "market": "US"},
    "UGL":   {"desc": "2x Gold (UGL)",             "lev": 2.0, "decay": "high",   "market": "US"},
    "BOIL":  {"desc": "2x Nat Gas (BOIL)",         "lev": 2.0, "decay": "high",   "market": "US"},
    # SG DLCs (SGX) — up to 7x, but extreme decay risk
    "BULL3": {"desc": "3x HSI Bull DLC (SGX)",     "lev": 3.0, "decay": "extreme", "market": "SG"},
    "BULL5": {"desc": "5x STI Bull DLC (SGX)",     "lev": 5.0, "decay": "extreme", "market": "SG"},
    # JP leveraged ETFs (TSE)
    "1570":  {"desc": "2x Nikkei Bull ETF (TSE)",  "lev": 2.0, "decay": "high",   "market": "JP"},
    "1579":  {"desc": "2x Nikkei Bull ETF (TSE)",  "lev": 2.0, "decay": "high",   "market": "JP"},
    # Futures / margin (no daily reset decay)
    "ES=F":  {"desc": "S&P500 E-mini Futures",     "lev": "variable", "decay": "none", "market": "US"},
    "NQ=F":  {"desc": "Nasdaq E-mini Futures",      "lev": "variable", "decay": "none", "market": "US"},
}

# ── Component weights ──────────────────────────────────────────────────────────
WEIGHTS = {
    "volatility": 0.35,   # VIX
    "trend":      0.25,   # 50/200 SMA topology
    "momentum":   0.25,   # RSI + MACD
    "macro":      0.15,   # yield curve + rate regime
}

# ── Look-back periods ──────────────────────────────────────────────────────────
MA_SHORT  = 50
MA_LONG   = 200
RSI_PER   = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG  = 9
ATR_PER   = 14


# ─────────────────────────────────────────────────────────────────────────────
# Pure indicator helpers (no I/O, fully testable)
# ─────────────────────────────────────────────────────────────────────────────

def _rsi(close: "pd.Series", period: int = 14) -> "pd.Series":
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag = gain.ewm(com=period - 1, min_periods=period).mean()
    al = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = ag / al.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def _macd(close: "pd.Series"):
    fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    m    = fast - slow
    sig  = m.ewm(span=MACD_SIG, adjust=False).mean()
    return m, sig


def _ma_slope_pct(series: "pd.Series", window: int = 5) -> float:
    """Percentage change of MA over last `window` bars — positive = rising."""
    if len(series) < window + 1:
        return 0.0
    return float((series.iloc[-1] - series.iloc[-window - 1]) / series.iloc[-window - 1] * 100)


# ─────────────────────────────────────────────────────────────────────────────
# Score sub-components (each returns 0.0–10.0)
# ─────────────────────────────────────────────────────────────────────────────

def score_volatility(vix: float) -> float:
    """
    VIX → volatility sub-score (0–10).

    VIX < 12 : panic compression; extreme calm but watch for mean reversion     → 9.5
    12–15    : very calm; ideal leveraged-ETF environment                       → 9.0
    15–18    : calm; good for 2x                                                → 8.0
    18–22    : normal market noise; modest leverage only                        → 6.5
    22–25    : elevated; 1x only                                                → 4.5
    25–30    : fear territory; reduce all leverage                              → 2.5
    30–40    : HIGH FEAR; HARD EXIT all leverage                                → 1.0
    > 40     : crisis/crash; 0 leverage, consider TLT/inverse                  → 0.0
    """
    if   vix > 40:  return 0.0
    elif vix > 30:  return 1.0
    elif vix > 25:  return 2.5
    elif vix > 22:  return 4.5
    elif vix > 18:  return 6.5
    elif vix > 15:  return 8.0
    elif vix > 12:  return 9.0
    else:           return 9.5


def score_trend(close: "pd.Series") -> tuple[float, dict]:
    """
    MA topology → trend sub-score (0–10).

    Uses SPY (or whichever asset is passed):
      +3.0  close > MA200 (price is in structural uptrend)
      +2.5  MA50 > MA200  (golden cross / uptrend confirmed)
      +2.5  MA200 slope rising (trend is accelerating, not just flat)
      +2.0  close > MA50  (short-term momentum confirms trend)
    """
    if len(close) < MA_LONG + 5:
        return 5.0, {"error": "insufficient data"}

    ma50  = close.rolling(MA_SHORT).mean()
    ma200 = close.rolling(MA_LONG).mean()

    cur       = float(close.iloc[-1])
    cur_ma50  = float(ma50.iloc[-1])
    cur_ma200 = float(ma200.iloc[-1])
    slope200  = _ma_slope_pct(ma200, window=10)   # 10-bar slope

    score = 0.0
    detail: dict = {}

    detail["close_vs_ma200"] = round((cur - cur_ma200) / cur_ma200 * 100, 2)
    detail["close_vs_ma50"]  = round((cur - cur_ma50)  / cur_ma50  * 100, 2)
    detail["ma50_vs_ma200"]  = round((cur_ma50 - cur_ma200) / cur_ma200 * 100, 2)
    detail["ma200_slope_pct"] = round(slope200, 3)

    if cur > cur_ma200:
        # Graduated: further above MA200 = stronger trend (caps at +3.0)
        dist_pct = detail["close_vs_ma200"]
        score += min(3.0, 1.5 + dist_pct / 10)
    else:
        # Below MA200: partial credit if just barely under (recovery attempt)
        dist_pct = detail["close_vs_ma200"]
        score += max(0.0, 1.5 + dist_pct / 10)   # dist_pct is negative here

    if cur_ma50 > cur_ma200:
        score += 2.5  # golden cross
    else:
        score += 0.0  # death cross

    if slope200 > 0.1:
        score += 2.5  # MA200 rising
    elif slope200 > 0:
        score += 1.5  # barely positive
    else:
        score += 0.0  # falling

    if cur > cur_ma50:
        score += 2.0 if detail["close_vs_ma50"] > 2 else 1.0
    else:
        score += 0.0

    score = round(min(10.0, max(0.0, score)), 2)
    return score, detail


def score_momentum(close: "pd.Series") -> tuple[float, dict]:
    """
    RSI + MACD → momentum sub-score (0–10).

    RSI ideal zone 55–72: strong momentum, not overbought
    MACD histogram > 0 and rising → bullish momentum
    """
    if len(close) < MACD_SLOW + MACD_SIG + 5:
        return 5.0, {"error": "insufficient data"}

    rsi        = _rsi(close, RSI_PER)
    macd, sig  = _macd(close)
    hist       = macd - sig

    cur_rsi  = float(rsi.iloc[-1])
    cur_hist = float(hist.iloc[-1])
    prev_hist = float(hist.iloc[-2])

    # RSI scoring (asymmetric: punish overbought more than we reward oversold)
    if   cur_rsi >= 80:  s_rsi = 2.0   # severely overbought
    elif cur_rsi >= 72:  s_rsi = 5.0   # overbought
    elif cur_rsi >= 55:  s_rsi = 9.5   # ideal zone
    elif cur_rsi >= 45:  s_rsi = 6.0   # neutral
    elif cur_rsi >= 35:  s_rsi = 3.0   # weakening
    else:                s_rsi = 1.0   # oversold / breakdown

    # MACD histogram direction + magnitude
    norm_hist = cur_hist / (close.iloc[-1] * 0.01) if close.iloc[-1] > 0 else 0.0
    if   norm_hist > 0.05 and cur_hist > prev_hist:  s_macd = 10.0  # strong + rising
    elif norm_hist > 0.02 and cur_hist > prev_hist:  s_macd = 8.0
    elif norm_hist > 0:                              s_macd = 6.5   # positive but fading
    elif norm_hist < -0.02:                          s_macd = 2.0   # negative and falling
    else:                                            s_macd = 4.0   # near zero

    score = round(s_rsi * 0.55 + s_macd * 0.45, 2)
    score = min(10.0, max(0.0, score))

    detail = {
        "rsi": round(cur_rsi, 1),
        "macd_hist": round(cur_hist, 4),
        "macd_hist_norm": round(norm_hist, 4),
        "s_rsi": s_rsi,
        "s_macd": s_macd,
    }
    return score, detail


def score_macro(
    vix: float,
    yield_spread_bps: Optional[float] = None,
    fed_rate_change: Optional[str] = None,   # "cutting" | "hiking" | "hold"
) -> tuple[float, dict]:
    """
    Macro regime sub-score (0–10).

    yield_spread_bps : 10Y minus 2Y in basis points (positive = normal, negative = inverted)
    fed_rate_change  : "cutting" boosts score, "hiking" reduces it
    VIX contrib      : already scored separately but we use extremes as macro flags
    """
    score = 5.0   # neutral default when no macro data
    detail: dict = {}

    if yield_spread_bps is not None:
        detail["yield_spread_bps"] = round(yield_spread_bps, 1)
        if   yield_spread_bps >= 100:  score += 2.0   # very healthy curve
        elif yield_spread_bps >= 25:   score += 1.5   # positive and meaningful
        elif yield_spread_bps >= 0:    score += 0.5   # barely positive
        elif yield_spread_bps >= -50:  score -= 1.5   # mild inversion
        elif yield_spread_bps >= -100: score -= 2.5   # significant inversion
        else:                          score -= 3.5   # deep inversion (recessionary)

    if fed_rate_change is not None:
        detail["fed_rate_change"] = fed_rate_change
        if   fed_rate_change == "cutting": score += 2.0
        elif fed_rate_change == "hiking":  score -= 1.0
        # "hold" = 0 change

    # VIX extreme as a macro tail-risk signal
    if vix > 35:
        score -= 2.0
        detail["vix_tail_risk"] = True
    elif vix < 13:
        score += 0.5   # extreme calm often precedes complacency — mild bonus only
        detail["vix_tail_risk"] = False

    score = round(min(10.0, max(0.0, score)), 2)
    return score, detail


# ─────────────────────────────────────────────────────────────────────────────
# Composite RegimeScore
# ─────────────────────────────────────────────────────────────────────────────

def compute_regime_score_from_components(
    s_vol: float,
    s_trend: float,
    s_mom: float,
    s_macro: float,
) -> float:
    """Weighted composite. All inputs must be 0–10."""
    raw = (
        s_vol   * WEIGHTS["volatility"] +
        s_trend * WEIGHTS["trend"]      +
        s_mom   * WEIGHTS["momentum"]   +
        s_macro * WEIGHTS["macro"]
    )
    return round(min(10.0, max(0.0, raw)), 2)


def leverage_from_score(score: float) -> float:
    """
    Map RegimeScore to leverage multiplier.

    >= 8.0  → 3.0x  (STRONG BULL — DLCs / 3x ETFs acceptable)
    7.0–7.9 → 2.0x  (BULL — 2x ETFs, leveraged margin)
    6.0–6.9 → 1.5x  (MILD BULL — modest leverage)
    4.0–5.9 → 1.0x  (NEUTRAL — 1x, no leverage products)
    < 4.0   → 0.5x  (BEAR — de-risk, hold cash/inverse)
    """
    if   score >= 8.0: return 3.0
    elif score >= 7.0: return 2.0
    elif score >= 6.0: return 1.5
    elif score >= 4.0: return 1.0
    else:              return 0.5


def regime_label(score: float) -> str:
    if   score >= 8.0: return "STRONG BULL"
    elif score >= 7.0: return "BULL"
    elif score >= 6.0: return "MILD BULL"
    elif score >= 4.0: return "NEUTRAL"
    else:              return "BEAR / CRASH"


def max_leverage_by_instrument(
    score: float,
    instrument_type: str,   # "etf_2x" | "etf_3x" | "dlc_5x" | "futures" | "margin"
) -> float:
    """
    Instrument-specific max leverage given regime score.

    DLCs (5x–7x) are only permitted at score >= 8.5 (extreme bull regime).
    3x ETFs permitted at score >= 7.5.
    2x ETFs permitted at score >= 6.0.
    Futures/margin: score >= 5.0 (decay-free, so threshold is lower).
    """
    caps = {
        "etf_2x":   {8.0: 2.0, 6.0: 2.0, 4.0: 0.0, 0.0: 0.0},
        "etf_3x":   {8.0: 3.0, 7.5: 3.0, 6.0: 0.0, 0.0: 0.0},
        "dlc_5x":   {8.5: 5.0, 7.0: 0.0, 0.0: 0.0},  # never below strong bull
        "dlc_7x":   {9.0: 7.0, 0.0: 0.0},             # only near-perfect regimes
        "futures":  {5.0: 2.0, 4.0: 1.0, 0.0: 0.0},  # decay-free = lower threshold
        "margin":   {5.0: 1.5, 4.0: 1.0, 0.0: 0.0},
    }
    thresholds = sorted(caps.get(instrument_type, {}), reverse=True)
    for t in thresholds:
        if score >= t:
            return caps[instrument_type][t]
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Live data fetcher
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_spy_data(period: str = "1y", interval: str = "1d") -> "pd.DataFrame":
    ticker = yf.Ticker("SPY")
    df = ticker.history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        raise RuntimeError("yfinance returned empty data for SPY")
    return df


def _fetch_vix() -> float:
    """Try live feed first (signal_stack), fallback to yfinance ^VIX."""
    if _VIX_LIVE_OK:
        v = _fetch_vix_live()
        if v is not None and v > 0:
            return float(v)
    try:
        vix_df = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if not vix_df.empty:
            return float(vix_df["Close"].iloc[-1])
    except Exception:
        pass
    return 20.0   # conservative fallback


def _fetch_yield_spread() -> Optional[float]:
    """Fetch US 10Y minus 2Y yield spread in basis points."""
    try:
        t10 = yf.Ticker("^TNX").history(period="5d")   # 10Y in %
        t2  = yf.Ticker("^IRX").history(period="5d")   # 13w proxy for short end
        if not t10.empty and not t2.empty:
            y10 = float(t10["Close"].iloc[-1])
            y2  = float(t2["Close"].iloc[-1]) / 10   # ^IRX is in % * 10 format
            return (y10 - y2) * 100   # convert % to bps
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# RegimeEngine — public API
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeResult:
    score:               float
    leverage_multiplier: float
    regime_label:        str
    is_leverage_safe:    bool          # True when score >= 6.0 AND vix < 30
    hard_exit_triggered: bool          # True when VIX > 30 — immediate exit signal
    components: dict = field(default_factory=dict)
    detail:     dict = field(default_factory=dict)
    timestamp:  str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def instrument_max_leverage(self, instrument_type: str) -> float:
        return max_leverage_by_instrument(self.score, instrument_type)

    def to_dict(self) -> dict:
        return {
            "score":               self.score,
            "leverage_multiplier": self.leverage_multiplier,
            "regime_label":        self.regime_label,
            "is_leverage_safe":    self.is_leverage_safe,
            "hard_exit_triggered": self.hard_exit_triggered,
            "components":          self.components,
            "detail":              self.detail,
            "timestamp":           self.timestamp,
        }


class RegimeEngine:
    """
    Main entry point for regime scoring.

    Quick start:
        engine = RegimeEngine()
        result = engine.compute("SPY")
        if result.hard_exit_triggered:
            exit_all_leveraged_positions()
        else:
            apply_leverage(result.leverage_multiplier)
    """

    def compute(
        self,
        ticker: str = "SPY",
        vix_override: Optional[float] = None,
        yield_spread_override: Optional[float] = None,
        fed_rate_change: Optional[str] = None,
    ) -> RegimeResult:
        """
        Compute live RegimeScore.

        Parameters
        ----------
        ticker            : base asset for trend/momentum signals (default SPY)
        vix_override      : use this VIX value instead of fetching live
        yield_spread_override : 10Y-2Y in bps if you have it from another source
        fed_rate_change   : "cutting" | "hiking" | "hold" | None
        """
        # 1. Fetch price data
        df = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=True)
        if df.empty:
            raise RuntimeError(f"No data for {ticker}")
        close = df["Close"].dropna()

        # 2. VIX
        vix = vix_override if vix_override is not None else _fetch_vix()

        # 3. Yield spread
        ys = yield_spread_override if yield_spread_override is not None else _fetch_yield_spread()

        # 4. Sub-scores
        s_vol  = score_volatility(vix)
        s_trend, trend_detail = score_trend(close)
        s_mom,   mom_detail   = score_momentum(close)
        s_macro, macro_detail = score_macro(vix, ys, fed_rate_change)

        # 5. Composite
        score = compute_regime_score_from_components(s_vol, s_trend, s_mom, s_macro)

        # 6. Hard exit gate — VIX > 30 overrides everything
        hard_exit = vix >= 30.0

        return RegimeResult(
            score               = score,
            leverage_multiplier = leverage_from_score(score),
            regime_label        = regime_label(score),
            is_leverage_safe    = (score >= 6.0 and not hard_exit),
            hard_exit_triggered = hard_exit,
            components = {
                "volatility_score": s_vol,
                "trend_score":      s_trend,
                "momentum_score":   s_mom,
                "macro_score":      s_macro,
                "vix":              round(vix, 2),
                "yield_spread_bps": round(ys, 1) if ys is not None else None,
            },
            detail = {
                "trend":   trend_detail,
                "momentum": mom_detail,
                "macro":    macro_detail,
            },
        )

    def compute_from_series(
        self,
        close: "pd.Series",
        vix: float,
        yield_spread_bps: Optional[float] = None,
        fed_rate_change: Optional[str] = None,
    ) -> RegimeResult:
        """
        Compute from pre-fetched data (used by backtester to avoid network calls).
        """
        s_vol  = score_volatility(vix)
        s_trend, trend_detail = score_trend(close)
        s_mom,   mom_detail   = score_momentum(close)
        s_macro, macro_detail = score_macro(vix, yield_spread_bps, fed_rate_change)

        score  = compute_regime_score_from_components(s_vol, s_trend, s_mom, s_macro)
        hard_exit = vix >= 30.0

        return RegimeResult(
            score               = score,
            leverage_multiplier = leverage_from_score(score),
            regime_label        = regime_label(score),
            is_leverage_safe    = (score >= 6.0 and not hard_exit),
            hard_exit_triggered = hard_exit,
            components = {
                "volatility_score": s_vol,
                "trend_score":      s_trend,
                "momentum_score":   s_mom,
                "macro_score":      s_macro,
                "vix":              round(vix, 2),
                "yield_spread_bps": round(yield_spread_bps, 1) if yield_spread_bps is not None else None,
            },
            detail = {
                "trend":    trend_detail,
                "momentum": mom_detail,
                "macro":    macro_detail,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    engine = RegimeEngine()
    result = engine.compute("SPY")
    print(json.dumps(result.to_dict(), indent=2))
    print(f"\n=== RegimeScore: {result.score:.1f} | {result.regime_label} | Leverage: {result.leverage_multiplier}x ===")
    if result.hard_exit_triggered:
        print("⚠️  HARD EXIT SIGNAL: VIX >= 30 — exit ALL leveraged positions now")
