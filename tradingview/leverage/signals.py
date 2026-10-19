"""
Multi-Asset Signal Generator
=============================
Identifies and ranks leveraged-exposure opportunities across US, SG, JP markets.

For each candidate asset, computes:
  - Trend quality          (MA alignment, slope)
  - Volatility regime      (realised vol, VIX-implied)
  - Momentum               (RSI, MACD, rate of change)
  - Leverage suitability   (decay cost vs expected trend return)
  - Recommended instrument (ETF / DLC / futures / margin)

Usage
-----
  from leverage.signals import MultiAssetScanner

  scanner = MultiAssetScanner()
  ranked  = scanner.scan()           # returns list[AssetSignal], best first
  for sig in ranked[:5]:
      print(sig.summary())
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError as e:
    sys.exit(f"pip install yfinance pandas numpy  ({e})")

_PARENT = Path(__file__).parent.parent
sys.path.insert(0, str(_PARENT))

from leverage.regime_score import (
    RegimeEngine, RegimeResult,
    score_trend, score_momentum, score_volatility,
    _rsi, _macd, _fetch_vix,
)


# ─────────────────────────────────────────────────────────────────────────────
# Asset universe — covers US, SG, JP markets
# ─────────────────────────────────────────────────────────────────────────────
#
# Each entry:
#   yf_ticker   : ticker understood by yfinance
#   tv_symbol   : TradingView symbol for cross-referencing with your existing stack
#   description : human label
#   asset_class : "equity_broad" | "equity_sector" | "equity_sg" | "equity_jp"
#                 | "commodity" | "volatility" | "bond"
#   available_instruments: which leverage wrappers are liquid for this asset
#   base_currency: USD | SGD | JPY

UNIVERSE: list[dict] = [
    # ── US Broad ──────────────────────────────────────────────────────────────
    {"yf": "SPY",  "tv": "AMEX:SPY",   "desc": "S&P 500",              "class": "equity_broad",   "ccy": "USD", "instruments": ["etf_2x","etf_3x","futures","options"]},
    {"yf": "QQQ",  "tv": "NASDAQ:QQQ", "desc": "Nasdaq 100",           "class": "equity_broad",   "ccy": "USD", "instruments": ["etf_2x","etf_3x","futures","options"]},
    {"yf": "IWM",  "tv": "AMEX:IWM",   "desc": "Russell 2000",         "class": "equity_broad",   "ccy": "USD", "instruments": ["etf_2x","options"]},
    # ── US Sectors ────────────────────────────────────────────────────────────
    {"yf": "SMH",  "tv": "NASDAQ:SMH", "desc": "Semiconductors",       "class": "equity_sector",  "ccy": "USD", "instruments": ["etf_3x","options"]},
    {"yf": "XLK",  "tv": "AMEX:XLK",   "desc": "Technology",           "class": "equity_sector",  "ccy": "USD", "instruments": ["etf_2x","etf_3x","options"]},
    {"yf": "XLF",  "tv": "AMEX:XLF",   "desc": "Financials",           "class": "equity_sector",  "ccy": "USD", "instruments": ["etf_2x","options"]},
    {"yf": "XLE",  "tv": "AMEX:XLE",   "desc": "Energy",               "class": "equity_sector",  "ccy": "USD", "instruments": ["etf_2x","options"]},
    {"yf": "XLV",  "tv": "AMEX:XLV",   "desc": "Healthcare",           "class": "equity_sector",  "ccy": "USD", "instruments": ["etf_2x","options"]},
    # ── Singapore Equities (Moomoo SG / SGX) ─────────────────────────────────
    {"yf": "^STI", "tv": "SGX:STI",    "desc": "Straits Times Index",  "class": "equity_sg",      "ccy": "SGD", "instruments": ["dlc_5x"]},
    {"yf": "D05.SI","tv": "SGX:D05",   "desc": "DBS Group",            "class": "equity_sg",      "ccy": "SGD", "instruments": ["margin"]},
    {"yf": "O39.SI","tv": "SGX:O39",   "desc": "OCBC Bank",            "class": "equity_sg",      "ccy": "SGD", "instruments": ["margin"]},
    {"yf": "U11.SI","tv": "SGX:U11",   "desc": "UOB",                  "class": "equity_sg",      "ccy": "SGD", "instruments": ["margin"]},
    # ── Japan Equities (Moomoo JP) ────────────────────────────────────────────
    {"yf": "^N225", "tv": "TVC:NI225", "desc": "Nikkei 225",           "class": "equity_jp",      "ccy": "JPY", "instruments": ["etf_2x"]},
    {"yf": "EWJ",  "tv": "AMEX:EWJ",   "desc": "Japan ETF (USD)",      "class": "equity_jp",      "ccy": "USD", "instruments": ["etf_2x","options"]},
    # ── Commodities ───────────────────────────────────────────────────────────
    {"yf": "GLD",  "tv": "AMEX:GLD",   "desc": "Gold ETF",             "class": "commodity",      "ccy": "USD", "instruments": ["etf_2x","options","futures"]},
    {"yf": "GC=F", "tv": "COMEX:GC1!", "desc": "Gold Futures",         "class": "commodity",      "ccy": "USD", "instruments": ["futures"]},
    {"yf": "USO",  "tv": "AMEX:USO",   "desc": "Crude Oil ETF",        "class": "commodity",      "ccy": "USD", "instruments": ["etf_2x","options"]},
    {"yf": "CL=F", "tv": "NYMEX:CL1!", "desc": "WTI Crude Futures",    "class": "commodity",      "ccy": "USD", "instruments": ["futures"]},
    {"yf": "PDBC", "tv": "NASDAQ:PDBC","desc": "Diversified Commodity", "class": "commodity",      "ccy": "USD", "instruments": ["margin"]},
    # ── Volatility (tactical / inverse) ──────────────────────────────────────
    {"yf": "SVXY", "tv": "CBOE:SVXY",  "desc": "Inverse VIX (-0.5x)",  "class": "volatility",     "ccy": "USD", "instruments": ["etf_2x"]},
    {"yf": "VIXY", "tv": "CBOE:VIXY",  "desc": "Long VIX (hedge)",     "class": "volatility",     "ccy": "USD", "instruments": ["etf_2x"]},
    # ── Bonds (for de-risk rotation) ─────────────────────────────────────────
    {"yf": "TLT",  "tv": "NASDAQ:TLT", "desc": "20Y Treasury",         "class": "bond",            "ccy": "USD", "instruments": ["etf_2x","options"]},
    {"yf": "TMF",  "tv": "AMEX:TMF",   "desc": "3x 20Y Treasury",      "class": "bond",            "ccy": "USD", "instruments": ["etf_3x"]},
]


# ─────────────────────────────────────────────────────────────────────────────
# Volatility decay cost estimator
# ─────────────────────────────────────────────────────────────────────────────

def annual_decay_drag(leverage: float, annual_vol: float) -> float:
    """
    Estimate annualised volatility decay drag for a daily-reset leveraged product.

    Formula: L*(L-1)/2 * σ²
    where L = leverage multiple, σ = annualised volatility of underlying.

    Example: L=3, σ=0.20 → drag = 3*2/2 * 0.04 = 0.12 = 12% per year
    """
    return leverage * (leverage - 1) / 2.0 * annual_vol ** 2


def trend_return_estimate(close: "pd.Series", window_days: int = 252) -> float:
    """Estimate annualised trend return from recent price action."""
    if len(close) < window_days:
        return 0.0
    ret = (close.iloc[-1] / close.iloc[-min(window_days, len(close)-1)]) - 1.0
    # Annualise if window is shorter
    factor = 252.0 / window_days
    return float(ret * factor)


def realised_vol(close: "pd.Series", window: int = 30) -> float:
    """Annualised realised volatility over last `window` trading days."""
    if len(close) < window + 1:
        return 0.25   # conservative default
    log_rets = np.log(close / close.shift(1)).dropna()
    return float(log_rets.tail(window).std() * np.sqrt(252))


# ─────────────────────────────────────────────────────────────────────────────
# AssetSignal data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AssetSignal:
    yf_ticker:       str
    tv_symbol:       str
    description:     str
    asset_class:     str
    currency:        str
    available_instruments: list[str]

    # Computed
    trend_score:     float = 0.0
    momentum_score:  float = 0.0
    vol_score:       float = 0.0   # asset-level volatility (not VIX)
    composite_score: float = 0.0   # 0–10
    annual_vol:      float = 0.0
    trend_return_1y: float = 0.0
    current_price:   Optional[float] = None
    change_pct_1m:   Optional[float] = None

    # Leverage analysis
    recommended_leverage: float = 1.0
    recommended_instrument: str = "direct"
    decay_drag_pct:  float = 0.0   # annual decay at recommended leverage
    net_expected_return: float = 0.0   # trend return - decay drag

    detail: dict = field(default_factory=dict)
    error:  Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"{self.yf_ticker:8s} ERROR: {self.error}"
        return (
            f"{self.yf_ticker:8s} | Score:{self.composite_score:4.1f} "
            f"| {self.asset_class:20s} "
            f"| Lev:{self.recommended_leverage:.1f}x via {self.recommended_instrument:10s} "
            f"| Trend1Y:{self.trend_return_1y:+.0%} "
            f"| DecayDrag:{self.decay_drag_pct:.1%}/yr "
            f"| Net:{self.net_expected_return:+.0%}/yr"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Per-asset volatility scoring (SEPARATE from VIX regime scoring)
# Uses the asset's own realised vol, not ^VIX
# ─────────────────────────────────────────────────────────────────────────────

def score_asset_volatility(annual_vol: float) -> float:
    """
    Score an asset's own volatility for leverage suitability.

    Low vol → higher score (leverage decay cost is lower, smoother trend)
    High vol → lower score (decay cost explodes, drawdowns are larger)

    Annual vol thresholds:
      < 12%   → 9.5  (bond/gold-like, very smooth)
      12–18%  → 8.5  (SPY-like, good for 2x leverage)
      18–25%  → 7.0  (typical large-cap equity)
      25–35%  → 5.0  (volatile — 2x OK if trend is strong, 3x risky)
      35–50%  → 3.0  (very volatile — leveraged ETF decay will dominate)
      > 50%   → 1.0  (do NOT use daily-reset leverage; futures only)
    """
    if   annual_vol < 0.12: return 9.5
    elif annual_vol < 0.18: return 8.5
    elif annual_vol < 0.25: return 7.0
    elif annual_vol < 0.35: return 5.0
    elif annual_vol < 0.50: return 3.0
    else:                   return 1.0


def recommend_instrument(
    composite_score: float,
    regime_score: float,
    annual_vol: float,
    available: list[str],
) -> tuple[float, str, float]:
    """
    Given asset score, regime score, and available instruments,
    return (leverage_multiple, instrument_type, estimated_decay_drag).

    Logic:
      1. Futures are preferred when available (no decay).
      2. DLCs only when regime >= 8 AND asset vol < 30%
      3. 3x ETFs when regime >= 7.5 AND asset vol < 30%
      4. 2x ETFs when regime >= 6.0
      5. Direct (1x) otherwise
    """
    # Futures: no decay — most permissive threshold
    if "futures" in available and composite_score >= 6.0 and regime_score >= 5.0:
        return 2.0, "futures", 0.0

    # DLCs: extreme leverage, only in perfect regimes and calm assets
    if "dlc_5x" in available and regime_score >= 8.5 and annual_vol < 0.28 and composite_score >= 7.5:
        drag = annual_decay_drag(5.0, annual_vol)
        return 5.0, "dlc_5x", drag

    # 3x ETF
    if "etf_3x" in available and regime_score >= 7.5 and annual_vol < 0.30 and composite_score >= 7.0:
        drag = annual_decay_drag(3.0, annual_vol)
        return 3.0, "etf_3x", drag

    # Options (calls): limited downside, no decay from regime exposure
    if "options" in available and composite_score >= 6.5 and regime_score >= 6.0:
        # Options leverage is implicit; model as 2x effective with zero structural decay
        # (theta decay is the cost, not product decay)
        return 2.0, "options", 0.0

    # 2x ETF
    if "etf_2x" in available and regime_score >= 6.0 and annual_vol < 0.40 and composite_score >= 6.0:
        drag = annual_decay_drag(2.0, annual_vol)
        return 2.0, "etf_2x", drag

    # Margin (lower threshold than ETF because no daily reset)
    if "margin" in available and composite_score >= 5.5 and regime_score >= 5.0:
        drag = 0.0   # margin cost modelled separately (borrow rate)
        return 1.5, "margin", drag

    # No leverage
    return 1.0, "direct", 0.0


# ─────────────────────────────────────────────────────────────────────────────
# MultiAssetScanner
# ─────────────────────────────────────────────────────────────────────────────

class MultiAssetScanner:
    """
    Scan all assets in UNIVERSE, score each, and return ranked list.

    Integration with existing stack:
      - regime_score.RegimeEngine() provides the global regime gate
      - Asset-level signals are computed fresh per scan
      - Results integrate with your existing signal_stack.py composite
    """

    def __init__(self, regime_engine: Optional[RegimeEngine] = None):
        self.engine = regime_engine or RegimeEngine()
        self._vix: Optional[float] = None

    def _get_vix(self) -> float:
        if self._vix is None:
            self._vix = _fetch_vix()
        return self._vix

    def _score_asset(self, asset: dict, regime: RegimeResult) -> AssetSignal:
        sig = AssetSignal(
            yf_ticker    = asset["yf"],
            tv_symbol    = asset["tv"],
            description  = asset["desc"],
            asset_class  = asset["class"],
            currency     = asset["ccy"],
            available_instruments = asset["instruments"],
        )

        try:
            df = yf.Ticker(asset["yf"]).history(period="1y", interval="1d", auto_adjust=True)
            if df.empty or len(df) < 60:
                sig.error = "insufficient data"
                return sig

            close = df["Close"].dropna()
            sig.current_price   = float(close.iloc[-1])
            sig.trend_return_1y = trend_return_estimate(close)
            sig.annual_vol      = realised_vol(close)
            sig.change_pct_1m   = float((close.iloc[-1] / close.iloc[-22] - 1)) if len(close) >= 22 else None

            # Sub-scores
            sig.trend_score,    trend_d = score_trend(close)
            sig.momentum_score, mom_d   = score_momentum(close)
            sig.vol_score               = score_asset_volatility(sig.annual_vol)

            # Composite: equal weight trend + momentum, gate by asset vol
            sig.composite_score = round(
                sig.trend_score * 0.40 +
                sig.momentum_score * 0.35 +
                sig.vol_score * 0.25,
                2
            )

            # Instrument recommendation
            (sig.recommended_leverage,
             sig.recommended_instrument,
             sig.decay_drag_pct) = recommend_instrument(
                sig.composite_score,
                regime.score,
                sig.annual_vol,
                asset["instruments"],
            )

            # Net expected return (annual trend return × leverage, minus decay)
            gross = sig.trend_return_1y * sig.recommended_leverage
            sig.net_expected_return = round(gross - sig.decay_drag_pct, 4)

            sig.detail = {"trend": trend_d, "momentum": mom_d}

        except Exception as exc:
            sig.error = str(exc)

        return sig

    def scan(
        self,
        min_composite_score: float = 5.0,
        asset_classes: Optional[list[str]] = None,
    ) -> list[AssetSignal]:
        """
        Scan universe, return scored assets sorted by net_expected_return DESC.

        Parameters
        ----------
        min_composite_score : filter out assets below this score
        asset_classes       : whitelist of asset classes; None = all
        """
        # Get global regime once
        regime = self.engine.compute("SPY")

        results: list[AssetSignal] = []
        targets = [
            a for a in UNIVERSE
            if asset_classes is None or a["class"] in asset_classes
        ]

        for asset in targets:
            sig = self._score_asset(asset, regime)
            if sig.error is None and sig.composite_score >= min_composite_score:
                results.append(sig)

        # Sort: net expected return (primary), composite_score (secondary)
        results.sort(key=lambda s: (s.net_expected_return, s.composite_score), reverse=True)
        return results

    def top_opportunities(self, n: int = 5) -> list[AssetSignal]:
        return self.scan()[:n]

    def regime_summary(self) -> dict:
        """Return current global regime assessment with per-market notes."""
        regime = self.engine.compute("SPY")
        vix    = self._get_vix()

        notes = []
        if regime.hard_exit_triggered:
            notes.append("🔴 VIX ≥ 30 — HARD EXIT all leveraged positions")
        elif regime.score >= 8.0:
            notes.append("✅ Strong bull regime — aggressive leverage permitted (2x–3x)")
        elif regime.score >= 6.0:
            notes.append("🟡 Mild bull — moderate leverage OK (1.5x–2x)")
        else:
            notes.append("⚠️  Neutral/Bear — 1x only, preserve capital")

        return {
            "regime":             regime.to_dict(),
            "notes":              notes,
            "leverage_permitted": not regime.hard_exit_triggered and regime.score >= 6.0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scanner = MultiAssetScanner()

    print("\n=== GLOBAL REGIME ===")
    rs = scanner.regime_summary()
    for note in rs["notes"]:
        print(" ", note)
    print(f"  RegimeScore: {rs['regime']['score']:.1f} | "
          f"Leverage multiplier: {rs['regime']['leverage_multiplier']}x")

    print("\n=== TOP OPPORTUNITIES ===")
    opps = scanner.top_opportunities(n=10)
    for sig in opps:
        print(sig.summary())
