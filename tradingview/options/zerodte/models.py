"""
ZeroDTE Data Models
=====================
All dataclasses for the ZeroDTE scanner pipeline.

Pipeline: Universe → MoverCandidates → SignalScores → ZeroDTEContracts → ZeroDTEResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Stock-level models ────────────────────────────────────────────────────────

@dataclass
class MoverCandidate:
    """
    A stock that is being evaluated as a potential ZeroDTE play.
    Populated by the universe fetcher; enriched by signal detectors.
    """
    symbol: str
    price: float                  # current/last close price (USD)
    prev_day_change_pct: float    # prev-day % change (positive = up, negative = down)
    volume: float                 # prev-day volume (shares)
    volume_20d_avg: float         # 20-day average volume (may be 0 if unavailable)
    rsi: float                    # 14-day RSI (0-100)
    sector: str = ""
    industry: str = ""
    source: str = "yfinance"      # data source: "yfinance" | "tv_scanner"

    @property
    def volume_ratio(self) -> float:
        """Return volume / 20d average. 0.0 if unavailable."""
        if self.volume_20d_avg > 0:
            return self.volume / self.volume_20d_avg
        return 0.0

    @property
    def is_bullish_mover(self) -> bool:
        return self.prev_day_change_pct >= 0

    @property
    def is_bearish_mover(self) -> bool:
        return self.prev_day_change_pct < 0


@dataclass
class IVStructure:
    """IV term structure for a stock from TV in-time-iv endpoint."""
    symbol: str
    iv_1d: float = 0.0        # 1-day implied volatility (decimal, e.g. 0.45 = 45%)
    iv_1w: float = 0.0        # 1-week IV
    iv_1m: float = 0.0        # 1-month IV
    iv_3m: float = 0.0        # 3-month IV
    available: bool = False   # False if endpoint returned no data

    @property
    def backwardation_slope(self) -> float:
        """iv_1w - iv_1m. Positive = short-term event risk priced in (backwardation)."""
        return self.iv_1w - self.iv_1m

    @property
    def is_backwardation(self) -> bool:
        return self.backwardation_slope > 0


@dataclass
class VOIData:
    """VOI (Volume Open Interest) data from TV voi-lens endpoint."""
    symbol: str
    call_put_ratio: float = 0.0    # C/P volume ratio at 1-5% OTM strikes
    put_call_ratio: float = 0.0    # P/C ratio (inverse)
    dominant_direction: str = ""   # "call" | "put" | "neutral"
    available: bool = False


@dataclass
class SignalScore:
    """
    Complete signal breakdown for a MoverCandidate for one direction.
    """
    symbol: str
    direction: str           # "call" | "put"

    # Signal 1 — Momentum (0-40 pts)
    momentum_score: float = 0.0
    momentum_prev_day_pts: float = 0.0     # 0 or 20 based on threshold
    momentum_vol_pts: float = 0.0          # 0 or 10 based on volume ratio
    momentum_rsi_pts: float = 0.0          # 0-10 based on RSI position

    # Signal 2 — IV Backwardation (0-30 pts)
    iv_back_score: float = 0.0
    iv_back_slope: float = 0.0             # raw backwardation slope
    iv_back_pts_base: float = 0.0          # 0 or 15 for any backwardation
    iv_back_pts_mid: float = 0.0           # +10 for slope >= threshold
    iv_back_pts_high: float = 0.0          # +5 bonus for steep slope
    iv_available: bool = False

    # Signal 3 — VOI Surge (0-30 pts)
    voi_score: float = 0.0
    voi_ratio: float = 0.0                 # C/P or P/C depending on direction
    voi_pts_base: float = 0.0             # +10 for ratio >= 5x
    voi_pts_mid: float = 0.0              # +15 for ratio >= 10x
    voi_pts_high: float = 0.0             # +5 bonus for >= 50x
    voi_available: bool = False

    # Composite
    composite_score: float = 0.0          # sum of all three (0-100)

    # Metadata
    price: float = 0.0
    prev_day_change_pct: float = 0.0
    volume_ratio: float = 0.0
    rsi: float = 0.0
    iv_1w: float = 0.0
    iv_1m: float = 0.0
    sector: str = ""
    industry: str = ""

    def compute_composite(self) -> None:
        """Compute composite as sum of three signal scores."""
        self.composite_score = self.momentum_score + self.iv_back_score + self.voi_score


# ── Contract-level models ─────────────────────────────────────────────────────

@dataclass
class ZeroDTEContract:
    """
    A single option contract selected for a ZeroDTE play.
    """
    # Identity
    symbol: str
    option_type: str          # "call" | "put"
    strike: float
    expiry: str               # "YYYY-MM-DD"
    dte: int                  # 0, 1, or 2
    contract_code: str        # OCC format e.g. "NVDA260606C00150000"

    # Pricing
    bid: float
    ask: float
    last_price: float
    mid_price: float

    # Greeks & IV
    iv: float                 # implied volatility (decimal)
    delta: float
    gamma: float
    theta: float

    # Liquidity
    open_interest: int
    volume: int

    # Computed
    spread_pct: float         # (ask - bid) / mid * 100
    otm_pct: float            # % OTM from current spot price (always >= 0)
    spot_price: float         # current stock price

    # Signal score from the stock level
    signal: Optional[SignalScore] = None

    # Lottery ticket bonus (set by contract_filter for ultra-cheap deep OTM contracts)
    # May 28 MSFT 440C pattern: ask=$0.09, OTM=6.8% — +15 pts bonus for asymmetric leverage
    lottery_ticket_bonus: float = 0.0

    # Preferred-DTE bonus: contracts at config.preferred_dte (default DTE=1, next-day expiry)
    # get a bonus because they represent the May 28 → May 29 scan pattern:
    # find EOD (May 28) → buy at open (May 29) → ride intraday momentum → +2500%
    dte_bonus: float = 0.0

    @property
    def composite_score(self) -> float:
        base = self.signal.composite_score if self.signal else 0.0
        return base + self.lottery_ticket_bonus + self.dte_bonus

    @property
    def momentum_score(self) -> float:
        return self.signal.momentum_score if self.signal else 0.0

    @property
    def iv_back_score(self) -> float:
        return self.signal.iv_back_score if self.signal else 0.0

    @property
    def voi_score(self) -> float:
        return self.signal.voi_score if self.signal else 0.0

    @property
    def confidence(self) -> str:
        s = self.composite_score
        if s >= 75:
            return "HIGH"
        elif s >= 60:
            return "MEDIUM"
        else:
            return "LOW"


# ── Run-level results ─────────────────────────────────────────────────────────

@dataclass
class ZeroDTERunResult:
    """Complete result of one ZeroDTE scan run."""
    run_at: str                            # ISO datetime string
    run_date: str                          # "YYYY-MM-DD"
    candidates_evaluated: int = 0
    signals_found_calls: int = 0
    signals_found_puts: int = 0
    contracts_calls: list[ZeroDTEContract] = field(default_factory=list)
    contracts_puts: list[ZeroDTEContract] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Output paths (set after reports are written)
    call_html_path: str = ""
    call_json_path: str = ""
    put_html_path: str = ""
    put_json_path: str = ""
