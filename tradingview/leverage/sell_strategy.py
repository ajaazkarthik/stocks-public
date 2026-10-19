"""
Leverage Sell Strategy — Tiered Exit Rules for Leveraged Positions
==================================================================
Applies hierarchical exit rules to open leveraged positions.

Exit tier hierarchy (checked in order, first triggered wins):
  TIER 1 — HARD EXIT (immediate, no exceptions)
    H1: VIX >= 30                          → exit ALL leverage
    H2: RegimeScore < 4.0                  → exit ALL leverage
    H3: 8% trailing drawdown from HWM      → exit THIS position
    H4: SPY breaks 200-SMA for 3 days      → exit broad market leverage

  TIER 2 — SCALE DOWN (reduce to 1x, do not fully exit)
    S1: RegimeScore drops to 4.0–5.9       → reduce to 1x
    S2: RSI > 78 on underlying             → overbought, trim 50%
    S3: VIX spikes +50% in 1 week          → reduce to 1x
    S4: MACD crossunder on SPY weekly      → reduce to 1x

  TIER 3 — HOLD (no action needed)
    No exit conditions triggered

All exits are non-negotiable once triggered.  No overrides.

Usage
-----
  from leverage.sell_strategy import LeverageSellEngine, LeveragePosition

  position = LeveragePosition(
      ticker="UPRO", entry_price=55.0, current_units=100,
      entry_leverage=3.0, entry_date="2026-01-15", hwm=62.0,
      hold_days=25, underlying_ticker="SPY",
  )
  engine = LeverageSellEngine()
  result = engine.evaluate(position)
  print(result.action, result.reason)
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

_PARENT = Path(__file__).parent.parent
sys.path.insert(0, str(_PARENT))

from leverage.regime_score import RegimeEngine, RegimeResult, _fetch_vix, _rsi, _macd


# ─────────────────────────────────────────────────────────────────────────────
# Thresholds (tune here — do NOT scatter magic numbers in logic)
# ─────────────────────────────────────────────────────────────────────────────

VIX_HARD_EXIT       = 30.0   # H1
REGIME_HARD_EXIT    = 4.0    # H2  (score below this = exit all leverage)
TRAILING_DD_EXIT    = 0.08   # H3  8% drawdown from HWM
SPY_MA_BREAK_DAYS   = 3      # H4  consecutive closes below SPY SMA200

REGIME_SCALE_DOWN   = 6.0    # S1  score below this = reduce to 1x
RSI_OVERBOUGHT_TRIM = 78.0   # S2  underlying RSI
VIX_SPIKE_PCT       = 0.50   # S3  VIX up 50%+ in 1 week
MAX_HOLD_DAYS       = 90     # T1  time-based review trigger


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LeveragePosition:
    """
    Represents one open leveraged position.

    Fields
    ------
    ticker           : the leveraged product (e.g. "UPRO", "TQQQ", "1570.T")
    underlying_ticker: the unleveraged proxy used for signal computation (e.g. "SPY")
    entry_price      : price at which the leveraged product was entered
    current_units    : number of shares/units held
    entry_leverage   : multiple at entry (2.0, 3.0, 5.0, etc.)
    entry_date       : ISO date string "YYYY-MM-DD"
    hwm              : highest watermark price (update daily with max(hwm, close))
    hold_days        : calendar days held so far
    instrument_type  : "etf_2x" | "etf_3x" | "dlc_5x" | "futures" | "margin"
    """
    ticker:             str
    underlying_ticker:  str
    entry_price:        float
    current_units:      float
    entry_leverage:     float
    entry_date:         str
    hwm:                float
    hold_days:          int
    instrument_type:    str = "etf_2x"
    market:             str = "US"          # "US" | "SG" | "JP"
    notes:              str = ""


@dataclass
class ExitDecision:
    action:         str    # "HARD_EXIT" | "SCALE_DOWN" | "TIME_REVIEW" | "HOLD"
    tier:           int    # 1=hard, 2=scale, 3=time, 0=hold
    reason:         str
    triggered_rule: str    # H1, H2, H3, H4, S1, S2, S3, S4, T1
    urgency:        str    # "IMMEDIATE" | "TODAY" | "REVIEW" | "NONE"
    scale_to_units: Optional[float] = None   # if SCALE_DOWN, target unit count
    detail:         dict = field(default_factory=dict)
    timestamp:      str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def is_exit(self) -> bool:
        return self.action in ("HARD_EXIT", "SCALE_DOWN")

    def summary(self) -> str:
        return f"[{self.action}] {self.triggered_rule}: {self.reason} ({self.urgency})"


# ─────────────────────────────────────────────────────────────────────────────
# Market data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _latest_close(ticker: str, days: int = 250) -> "pd.Series":
    df = yf.Ticker(ticker).history(period=f"{days}d", interval="1d", auto_adjust=True)
    return df["Close"].dropna()


def _check_spy_ma_break(n_days: int = 3) -> tuple[bool, dict]:
    """
    True if SPY has closed below its 200-SMA for `n_days` consecutive sessions.
    """
    close  = _latest_close("SPY", days=250)
    ma200  = close.rolling(200).mean()
    below  = (close < ma200).astype(int)
    streak = int(below.iloc[-n_days:].sum()) if len(below) >= n_days else 0
    triggered = streak >= n_days
    return triggered, {
        "consecutive_closes_below_ma200": streak,
        "spy_close":    round(float(close.iloc[-1]), 2),
        "spy_ma200":    round(float(ma200.iloc[-1]), 2),
    }


def _check_vix_spike(spike_pct: float = 0.50) -> tuple[bool, dict]:
    """
    True if VIX has risen more than `spike_pct` (50%) over the past 5 trading days.
    """
    vix_df = yf.Ticker("^VIX").history(period="15d", interval="1d")
    if len(vix_df) < 6:
        return False, {}
    vix_now  = float(vix_df["Close"].iloc[-1])
    vix_5ago = float(vix_df["Close"].iloc[-6])
    change   = (vix_now - vix_5ago) / vix_5ago
    triggered = change >= spike_pct
    return triggered, {
        "vix_now":    round(vix_now, 2),
        "vix_5d_ago": round(vix_5ago, 2),
        "change_pct": round(change * 100, 2),
    }


def _check_macd_crossunder_weekly(ticker: str = "SPY") -> tuple[bool, dict]:
    """
    True if SPY (or `ticker`) had a MACD crossunder on the most recent weekly bar.
    """
    close = yf.Ticker(ticker).history(period="2y", interval="1wk", auto_adjust=True)["Close"].dropna()
    if len(close) < 35:
        return False, {}
    m, sig = _macd(close)
    hist   = m - sig
    triggered = (hist.iloc[-1] < 0) and (hist.iloc[-2] >= 0)
    return triggered, {
        "weekly_macd_hist":      round(float(hist.iloc[-1]), 4),
        "weekly_macd_hist_prev": round(float(hist.iloc[-2]), 4),
    }


def _underlying_rsi(ticker: str) -> tuple[float, dict]:
    close = _latest_close(ticker, days=60)
    rsi   = _rsi(close)
    cur   = float(rsi.iloc[-1])
    return cur, {"rsi": round(cur, 1)}


# ─────────────────────────────────────────────────────────────────────────────
# Sell Engine
# ─────────────────────────────────────────────────────────────────────────────

class LeverageSellEngine:
    """
    Evaluate one or more leveraged positions against all exit rules.

    Design philosophy
    -----------------
    - Tier 1 rules (hard exits) are checked first and short-circuit.
    - Tier 2 rules (scale-downs) only run if Tier 1 not triggered.
    - All live data is fetched once and reused across multiple positions.
    - Works with both individual positions and a full portfolio list.
    """

    def __init__(self, regime_engine: Optional[RegimeEngine] = None):
        self.regime_engine = regime_engine or RegimeEngine()
        # Cache live data for the session (avoid redundant yfinance calls)
        self._regime:  Optional[RegimeResult] = None
        self._vix:     Optional[float] = None
        self._spy_break: Optional[tuple[bool, dict]] = None
        self._vix_spike: Optional[tuple[bool, dict]] = None
        self._macd_xu:   Optional[tuple[bool, dict]] = None

    # ── Cache helpers ──────────────────────────────────────────────────────────

    def _get_regime(self) -> RegimeResult:
        if self._regime is None:
            self._regime = self.regime_engine.compute("SPY")
        return self._regime

    def _get_vix(self) -> float:
        if self._vix is None:
            self._vix = _fetch_vix()
        return self._vix

    def _get_spy_break(self) -> tuple[bool, dict]:
        if self._spy_break is None:
            self._spy_break = _check_spy_ma_break(SPY_MA_BREAK_DAYS)
        return self._spy_break

    def _get_vix_spike(self) -> tuple[bool, dict]:
        if self._vix_spike is None:
            self._vix_spike = _check_vix_spike(VIX_SPIKE_PCT)
        return self._vix_spike

    def _get_macd_xu(self) -> tuple[bool, dict]:
        if self._macd_xu is None:
            self._macd_xu = _check_macd_crossunder_weekly("SPY")
        return self._macd_xu

    def reset_cache(self) -> None:
        """Call at start of each daily session to clear stale cached data."""
        self._regime = None
        self._vix    = None
        self._spy_break = None
        self._vix_spike = None
        self._macd_xu   = None

    # ── Drawdown helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _current_drawdown_from_hwm(position: LeveragePosition, current_price: float) -> float:
        """Percentage drawdown from HWM. Positive = below HWM."""
        if position.hwm <= 0:
            return 0.0
        return (position.hwm - current_price) / position.hwm

    @staticmethod
    def _fetch_current_price(ticker: str) -> float:
        """Fetch latest close for the leveraged product itself."""
        try:
            df = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True)
            return float(df["Close"].iloc[-1])
        except Exception:
            return 0.0

    # ── Core evaluation ────────────────────────────────────────────────────────

    def evaluate(self, position: LeveragePosition) -> ExitDecision:
        """
        Evaluate a single position against all exit rules.

        Returns ExitDecision — action, reason, urgency.
        """
        regime = self._get_regime()
        vix    = self._get_vix()

        current_price = self._fetch_current_price(position.ticker)
        dd_from_hwm   = self._current_drawdown_from_hwm(position, current_price)

        # ──────────────────────────────────────────────────────────────────────
        # TIER 1 — HARD EXIT (immediate)
        # ──────────────────────────────────────────────────────────────────────

        # H1: VIX >= 30 — regime is broken, exit all leverage
        if vix >= VIX_HARD_EXIT:
            return ExitDecision(
                action="HARD_EXIT", tier=1, triggered_rule="H1",
                urgency="IMMEDIATE",
                reason=f"VIX = {vix:.1f} ≥ {VIX_HARD_EXIT} — regime invalidated",
                detail={"vix": vix},
            )

        # H2: RegimeScore < 4.0 — bear/crash regime
        if regime.score < REGIME_HARD_EXIT:
            return ExitDecision(
                action="HARD_EXIT", tier=1, triggered_rule="H2",
                urgency="IMMEDIATE",
                reason=f"RegimeScore = {regime.score:.1f} < {REGIME_HARD_EXIT} — bear regime",
                detail={"regime_score": regime.score, "regime_label": regime.regime_label},
            )

        # H3: 8% trailing drawdown from high-water-mark
        if dd_from_hwm >= TRAILING_DD_EXIT:
            return ExitDecision(
                action="HARD_EXIT", tier=1, triggered_rule="H3",
                urgency="IMMEDIATE",
                reason=f"Drawdown {dd_from_hwm:.1%} from HWM ${position.hwm:.2f} ≥ {TRAILING_DD_EXIT:.0%}",
                detail={
                    "current_price": round(current_price, 2),
                    "hwm": position.hwm,
                    "drawdown_pct": round(dd_from_hwm * 100, 2),
                },
            )

        # H4: SPY closes below 200-SMA for 3 consecutive days
        spy_break, spy_detail = self._get_spy_break()
        if spy_break:
            return ExitDecision(
                action="HARD_EXIT", tier=1, triggered_rule="H4",
                urgency="TODAY",
                reason=f"SPY below 200-SMA for {SPY_MA_BREAK_DAYS}+ consecutive closes",
                detail=spy_detail,
            )

        # ──────────────────────────────────────────────────────────────────────
        # TIER 2 — SCALE DOWN (reduce to 1x, not full exit)
        # ──────────────────────────────────────────────────────────────────────

        target_1x_units = position.current_units / position.entry_leverage   # approx 1x exposure

        # S1: RegimeScore in neutral zone (4.0–5.9) — reduce leverage
        if regime.score < REGIME_SCALE_DOWN:
            return ExitDecision(
                action="SCALE_DOWN", tier=2, triggered_rule="S1",
                urgency="TODAY",
                reason=f"RegimeScore = {regime.score:.1f} (neutral) — reduce to 1x",
                scale_to_units=target_1x_units,
                detail={"regime_score": regime.score},
            )

        # S2: Underlying RSI overbought
        cur_rsi, rsi_detail = _underlying_rsi(position.underlying_ticker)
        if cur_rsi >= RSI_OVERBOUGHT_TRIM:
            half_units = position.current_units * 0.5
            return ExitDecision(
                action="SCALE_DOWN", tier=2, triggered_rule="S2",
                urgency="TODAY",
                reason=f"Underlying RSI = {cur_rsi:.0f} ≥ {RSI_OVERBOUGHT_TRIM} — trim 50%",
                scale_to_units=half_units,
                detail=rsi_detail,
            )

        # S3: VIX spike 50%+ in 1 week
        spike, spike_detail = self._get_vix_spike()
        if spike:
            return ExitDecision(
                action="SCALE_DOWN", tier=2, triggered_rule="S3",
                urgency="TODAY",
                reason=f"VIX spiked {spike_detail.get('change_pct',0):.0f}% in 5d — reduce to 1x",
                scale_to_units=target_1x_units,
                detail=spike_detail,
            )

        # S4: MACD crossunder on weekly SPY
        macd_xu, macd_detail = self._get_macd_xu()
        if macd_xu:
            return ExitDecision(
                action="SCALE_DOWN", tier=2, triggered_rule="S4",
                urgency="TODAY",
                reason="SPY weekly MACD crossunder — momentum failing, reduce to 1x",
                scale_to_units=target_1x_units,
                detail=macd_detail,
            )

        # ──────────────────────────────────────────────────────────────────────
        # TIER 3 — TIME REVIEW (not an exit, but flag for re-evaluation)
        # ──────────────────────────────────────────────────────────────────────

        if position.hold_days >= MAX_HOLD_DAYS:
            return ExitDecision(
                action="TIME_REVIEW", tier=3, triggered_rule="T1",
                urgency="REVIEW",
                reason=f"Held {position.hold_days}d ≥ {MAX_HOLD_DAYS}d — re-evaluate entry thesis",
                detail={"hold_days": position.hold_days, "regime_score": regime.score},
            )

        # ── No exit triggered ──────────────────────────────────────────────────
        return ExitDecision(
            action="HOLD", tier=0, triggered_rule="NONE",
            urgency="NONE",
            reason="All exit conditions clear — maintain position",
            detail={
                "regime_score":  regime.score,
                "vix":           vix,
                "dd_from_hwm":   round(dd_from_hwm * 100, 2),
                "hold_days":     position.hold_days,
                "underlying_rsi": round(cur_rsi, 1),
            },
        )

    def evaluate_portfolio(self, positions: list[LeveragePosition]) -> list[tuple[LeveragePosition, ExitDecision]]:
        """
        Evaluate a list of positions.  Live data is fetched once and reused.
        """
        self.reset_cache()
        return [(pos, self.evaluate(pos)) for pos in positions]


# ─────────────────────────────────────────────────────────────────────────────
# CLI demo
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import date, timedelta

    demo_positions = [
        LeveragePosition(
            ticker="UPRO", underlying_ticker="SPY",
            entry_price=52.0, current_units=100,
            entry_leverage=3.0, entry_date="2026-03-01",
            hwm=64.0,   # 20% above entry — currently at ~62 = -3% from HWM
            hold_days=40, instrument_type="etf_3x",
        ),
        LeveragePosition(
            ticker="TQQQ", underlying_ticker="QQQ",
            entry_price=44.0, current_units=200,
            entry_leverage=3.0, entry_date="2026-02-15",
            hwm=48.0,   # currently 15% below HWM — should trigger H3
            hold_days=54, instrument_type="etf_3x",
        ),
        LeveragePosition(
            ticker="SSO", underlying_ticker="SPY",
            entry_price=80.0, current_units=50,
            entry_leverage=2.0, entry_date="2026-03-20",
            hwm=85.0,   # small drawdown from HWM
            hold_days=21, instrument_type="etf_2x",
        ),
    ]

    engine = LeverageSellEngine()
    results = engine.evaluate_portfolio(demo_positions)

    print(f"\n=== LEVERAGE SELL ANALYSIS  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===\n")
    for pos, decision in results:
        print(f"  {pos.ticker:6s}  {decision.summary()}")
