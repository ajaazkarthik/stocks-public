"""
Option Position Manager — Strategy Evaluation for Existing Option Holdings
==========================================================================
Evaluates and selects the best strategy for an **already-held** option position.
Usable by both the Signal Engine (pre-trade structuring) and Sell Engine
(post-trade management / roll / hedge decisions).

Supported strategies evaluated:
  1. HOLD             — Keep existing position; monitor theta decay
  2. SELL_CLOSE       — Exit fully; lock in current profit/loss
  3. PARTIAL_CLOSE    — Sell 50% of position, let rest ride
  4. LONG_CALL        — Buy additional call leg (scale in)
  5. LONG_PUT         — Buy protective put to hedge downside
  6. COVERED_CALL     — Sell OTM call above current strike (if holding shares)
  7. PROTECTIVE_PUT   — Buy OTM put below current strike
  8. COLLAR           — Buy OTM put + sell OTM call simultaneously
  9. CASH_SECURED_PUT — Sell a cash-secured put at support level
 10. ROLL_UP          — Roll call to higher strike (take profit, keep exposure)
 11. ROLL_OUT         — Roll to later expiry (extend time, maintain position)

Decision factors:
  • Profit % on premium (entry vs current)
  • Moneyness (how deep ITM/OTM is the current holding)
  • DTE (days to expiry — theta acceleration zone <14d)
  • Scenario payoff (UP/FLAT/DOWN) for each strategy
  • IV regime (expanding = buy premium good; contracting = sell premium good)
  • Underlying trend (RSI, ADX from sell_engine helpers)
  • Risk/reward ratio per strategy

Integration:
  From Signal Engine:
    from options.option_position_manager import OptionPositionManager
    mgr = OptionPositionManager()
    result = mgr.evaluate(position)

  From Sell Engine:
    from options.option_position_manager import OptionPositionManager
    mgr = OptionPositionManager()
    result = mgr.evaluate(position)
    action = result["primary"]["action"]   # e.g. "ROLL_OUT", "PARTIAL_CLOSE"

Position dict format:
  {
    "symbol":           "NASDAQ:GOOGL",    # TradingView pro_symbol
    "option_type":      "CALL",            # "CALL" or "PUT"
    "strike":           335.0,             # strike price
    "expiry":           "2025-05-16",      # ISO date
    "entry_premium":    1.5,               # what you paid per share ($)
    "current_premium":  8.0,               # current market price of option
    "underlying_price": 338.0,             # current stock price
    "quantity":         1,                 # number of contracts
    "contract_size":    100,               # shares per contract (US=100)
    "iv":               None,              # current IV% (optional)
    "rsi":              None,              # RSI of underlying (optional)
    "adx":              None,              # ADX of underlying (optional)
    "vix":              None,              # VIX level (optional)
  }
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

if os.name == "nt":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        if sys.platform == "win32":  # cp1252 fix — not needed on macOS/Linux (already UTF-8)
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, "reconfigure"):
        if sys.platform == "win32":  # cp1252 fix — not needed on macOS/Linux (already UTF-8)
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding='utf-8', errors='replace')


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

_THETA_DANGER_DTE    = 14    # days — theta accelerates sharply inside this window
_THETA_CAUTION_DTE   = 21    # moderate caution zone
_DEEP_ITM_PCT        = 5.0   # moneyness % — beyond this = deep ITM
_DEEP_OTM_PCT        = 5.0   # beyond this OTM = deep OTM (low probability)
_BREAKEVEN_PROFIT    = 20.0  # %  — below this, don't hedge yet
_STRONG_PROFIT       = 80.0  # %  — above this, actively protect gains
_LOCK_PROFIT         = 150.0 # %  — above this, consider full exit or tight collar


# ══════════════════════════════════════════════════════════════════════════════
# Strategy enum + result dataclass
# ══════════════════════════════════════════════════════════════════════════════

class OptionStrategy(str, Enum):
    HOLD             = "HOLD"
    SELL_CLOSE       = "SELL_CLOSE"
    PARTIAL_CLOSE    = "PARTIAL_CLOSE"
    LONG_PUT         = "LONG_PUT"
    LONG_CALL        = "LONG_CALL"
    COVERED_CALL     = "COVERED_CALL"
    PROTECTIVE_PUT   = "PROTECTIVE_PUT"
    COLLAR           = "COLLAR"
    CASH_SECURED_PUT = "CASH_SECURED_PUT"
    ROLL_UP          = "ROLL_UP"
    ROLL_OUT         = "ROLL_OUT"


@dataclass
class ScenarioPayoff:
    """Estimated payoff under each price scenario."""
    up_pct:   float   # % gain/loss on position if underlying rises ~5%
    flat_pct: float   # % gain/loss if underlying stays flat
    down_pct: float   # % gain/loss if underlying falls ~5%


@dataclass
class StrategyResult:
    strategy:         OptionStrategy
    rank:             int
    score:            float           # composite 0–100
    action_label:     str             # concise one-liner (e.g. "Sell 50% of position")
    rationale:        str             # 2–3 sentence explanation
    timing_guidance:  str             # when / under what conditions to act
    scenario:         ScenarioPayoff
    risks:            list[str]       = field(default_factory=list)
    conditions_met:   list[str]       = field(default_factory=list)
    emoji:            str             = "📊"


# ══════════════════════════════════════════════════════════════════════════════
# Black-Scholes helpers (pure Python, no scipy)
# ══════════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _bs_price(S: float, K: float, T: float, r: float, sigma: float,
              option_type: str = "CALL") -> float:
    """Black-Scholes theoretical price. Returns 0.0 on degenerate inputs."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if option_type == "CALL" else (K - S))
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if option_type == "CALL":
            return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        else:
            return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _bs_delta(S: float, K: float, T: float, r: float, sigma: float,
              option_type: str = "CALL") -> float:
    """Black-Scholes delta."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if option_type == "CALL":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        if option_type == "CALL":
            return _norm_cdf(d1)
        return _norm_cdf(d1) - 1.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _bs_theta(S: float, K: float, T: float, r: float, sigma: float,
              option_type: str = "CALL") -> float:
    """Daily theta ($ per day per share)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if option_type == "CALL":
            theta = (
                -(S * _norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                - r * K * math.exp(-r * T) * _norm_cdf(d2)
            ) / 365
        else:
            theta = (
                -(S * _norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                + r * K * math.exp(-r * T) * _norm_cdf(-d2)
            ) / 365
        return round(theta, 4)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _implied_sigma_from_premium(S: float, K: float, T: float, r: float,
                                 market_premium: float,
                                 option_type: str = "CALL") -> float:
    """Newton-Raphson IV solver. Returns 0.30 (30%) as fallback."""
    if market_premium <= 0 or T <= 0:
        return 0.30
    sigma = 0.30
    for _ in range(50):
        price = _bs_price(S, K, T, r, sigma, option_type)
        diff  = price - market_premium
        if abs(diff) < 1e-5:
            break
        try:
            d1    = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
            vega  = S * _norm_pdf(d1) * math.sqrt(T)
        except (ValueError, ZeroDivisionError):
            break
        if vega < 1e-8:
            break
        sigma = sigma - diff / vega
        sigma = max(0.01, min(sigma, 5.0))
    return round(sigma, 4)


# ══════════════════════════════════════════════════════════════════════════════
# Position context (parsed from raw dict)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PositionContext:
    symbol:           str
    option_type:      str          # "CALL" or "PUT"
    strike:           float
    expiry_date:      date
    entry_premium:    float
    current_premium:  float
    underlying_price: float
    quantity:         int
    contract_size:    int
    iv:               Optional[float]   # as decimal (0.30 = 30%) — solved if None
    rsi:              Optional[float]
    adx:              Optional[float]
    vix:              Optional[float]

    # Derived fields (computed in __post_init__)
    dte:              int   = field(init=False)
    profit_pct:       float = field(init=False)
    moneyness_pct:    float = field(init=False)   # positive = ITM (for call), negative = OTM
    r:                float = field(init=False)   # risk-free rate
    T:                float = field(init=False)   # time to expiry in years
    delta:            float = field(init=False)
    theta_daily:      float = field(init=False)

    def __post_init__(self) -> None:
        today = date.today()
        self.dte = max(0, (self.expiry_date - today).days)
        self.T   = self.dte / 365.0
        self.r   = float(os.environ.get("BS_RISK_FREE_RATE", "0.045"))

        # Profit on premium
        if self.entry_premium > 0:
            self.profit_pct = ((self.current_premium - self.entry_premium)
                               / self.entry_premium) * 100.0
        else:
            self.profit_pct = 0.0

        # Moneyness: positive = in our favour (ITM for CALL, ITM for PUT)
        if self.option_type == "CALL":
            # ITM call: underlying > strike → positive
            self.moneyness_pct = ((self.underlying_price - self.strike)
                                  / self.strike) * 100.0
        else:
            # ITM put: strike > underlying → positive
            self.moneyness_pct = ((self.strike - self.underlying_price)
                                  / self.strike) * 100.0

        # Solve IV from current premium if not provided
        if self.iv is None and self.T > 0:
            self.iv = _implied_sigma_from_premium(
                self.underlying_price, self.strike, self.T, self.r,
                self.current_premium, self.option_type,
            )
        elif self.iv is None:
            self.iv = 0.30

        # Greeks
        self.delta = _bs_delta(
            self.underlying_price, self.strike, self.T, self.r,
            self.iv, self.option_type,
        )
        self.theta_daily = _bs_theta(
            self.underlying_price, self.strike, self.T, self.r,
            self.iv, self.option_type,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "PositionContext":
        expiry_raw = d.get("expiry", "")
        if isinstance(expiry_raw, date):
            expiry_date = expiry_raw
        else:
            try:
                expiry_date = datetime.strptime(str(expiry_raw), "%Y-%m-%d").date()
            except ValueError:
                # Default to 30 DTE if not parseable
                from datetime import timedelta
                expiry_date = date.today() + timedelta(days=30)

        # IV normalisation: accept % (30.0) or decimal (0.30)
        raw_iv = d.get("iv")
        if raw_iv is not None:
            raw_iv = float(raw_iv)
            if raw_iv > 2.0:       # looks like a percentage
                raw_iv = raw_iv / 100.0

        return cls(
            symbol           = str(d.get("symbol", "UNKNOWN")),
            option_type      = str(d.get("option_type", "CALL")).upper(),
            strike           = float(d["strike"]),
            expiry_date      = expiry_date,
            entry_premium    = float(d["entry_premium"]),
            current_premium  = float(d["current_premium"]),
            underlying_price = float(d["underlying_price"]),
            quantity         = int(d.get("quantity", 1)),
            contract_size    = int(d.get("contract_size", 100)),
            iv               = raw_iv,
            rsi              = float(d["rsi"]) if d.get("rsi") is not None else None,
            adx              = float(d["adx"]) if d.get("adx") is not None else None,
            vix              = float(d["vix"]) if d.get("vix") is not None else None,
        )

    @property
    def position_value(self) -> float:
        return self.current_premium * self.quantity * self.contract_size

    @property
    def entry_value(self) -> float:
        return self.entry_premium * self.quantity * self.contract_size

    @property
    def unrealised_pnl(self) -> float:
        return self.position_value - self.entry_value

    @property
    def is_itm(self) -> bool:
        return self.moneyness_pct > 0

    @property
    def is_deep_itm(self) -> bool:
        return self.moneyness_pct >= _DEEP_ITM_PCT

    @property
    def is_deep_otm(self) -> bool:
        return self.moneyness_pct <= -_DEEP_OTM_PCT

    @property
    def theta_danger(self) -> bool:
        return self.dte <= _THETA_DANGER_DTE

    @property
    def theta_caution(self) -> bool:
        return self.dte <= _THETA_CAUTION_DTE

    @property
    def iv_expanding(self) -> bool:
        """Rough proxy: high IV vs 30% baseline suggests expansion."""
        return self.iv is not None and self.iv > 0.40

    @property
    def iv_contracting(self) -> bool:
        return self.iv is not None and self.iv < 0.20

    @property
    def trend_bullish(self) -> bool:
        rsi_ok = self.rsi is None or self.rsi >= 50
        adx_ok = self.adx is None or self.adx >= 20
        return rsi_ok and adx_ok

    @property
    def trend_bearish(self) -> bool:
        rsi_flag = self.rsi is not None and self.rsi < 45
        adx_flag = self.adx is not None and self.adx < 20
        return rsi_flag or adx_flag

    @property
    def vix_regime(self) -> str:
        if self.vix is None:
            return "UNKNOWN"
        if self.vix >= 35:
            return "EXTREME_FEAR"
        if self.vix >= 25:
            return "FEAR"
        return "NORMAL"


# ══════════════════════════════════════════════════════════════════════════════
# Payoff calculators
# ══════════════════════════════════════════════════════════════════════════════

def _payoff_hold(ctx: PositionContext, shock: float = 0.05) -> ScenarioPayoff:
    """Payoff of simply holding the existing position under ±shock moves."""
    up_val   = _bs_price(ctx.underlying_price * (1 + shock), ctx.strike,
                         ctx.T, ctx.r, ctx.iv or 0.30, ctx.option_type)
    flat_val = ctx.current_premium
    down_val = _bs_price(ctx.underlying_price * (1 - shock), ctx.strike,
                         ctx.T, ctx.r, ctx.iv or 0.30, ctx.option_type)

    def pct(v: float) -> float:
        if ctx.entry_premium <= 0:
            return 0.0
        return round(((v - ctx.entry_premium) / ctx.entry_premium) * 100, 1)

    return ScenarioPayoff(pct(up_val), pct(flat_val), pct(down_val))


def _payoff_partial_close(ctx: PositionContext, shock: float = 0.05) -> ScenarioPayoff:
    """Payoff if half the position is closed now, half held."""
    base = _payoff_hold(ctx, shock)
    locked = ctx.profit_pct / 2   # locked-in profit on the sold half
    return ScenarioPayoff(
        round((base.up_pct   + locked) / 2, 1),
        round((base.flat_pct + locked) / 2, 1),
        round((base.down_pct + locked) / 2, 1),
    )


def _payoff_protective_put(ctx: PositionContext, shock: float = 0.05,
                           put_cost_pct: float = 5.0) -> ScenarioPayoff:
    """
    Holding existing + buying an ATM put to hedge downside.
    put_cost_pct = estimated cost of put as % of current premium.
    """
    base = _payoff_hold(ctx, shock)
    # Down scenario: put offsets some of the loss; estimate put gains ~80% of downside loss
    down_protected = base.down_pct * 0.20 - put_cost_pct  # 80% covered, minus put cost
    return ScenarioPayoff(
        round(base.up_pct   - put_cost_pct, 1),
        round(base.flat_pct - put_cost_pct, 1),
        round(down_protected, 1),
    )


def _payoff_collar(ctx: PositionContext, shock: float = 0.05,
                   net_cost_pct: float = 1.0) -> ScenarioPayoff:
    """
    Collar: buy put + sell OTM call.
    Caps upside at OTM call strike; floors downside at put strike.
    Net cost typically near zero (sold call offsets put premium).
    """
    base = _payoff_hold(ctx, shock)
    # Cap upside at ~80% of the unhedged up move
    up_capped   = min(base.up_pct * 0.80, 40.0) - net_cost_pct
    down_floored = max(base.down_pct * 0.15, -10.0)   # floor: only -10% max loss
    return ScenarioPayoff(
        round(up_capped,         1),
        round(base.flat_pct,     1),
        round(down_floored,      1),
    )


def _payoff_covered_call(ctx: PositionContext, shock: float = 0.05,
                         premium_income_pct: float = 8.0) -> ScenarioPayoff:
    """
    Sell an OTM call against existing long call position.
    Reduces net cost; caps upside.
    premium_income_pct: estimated credit received as % of current premium.
    """
    base = _payoff_hold(ctx, shock)
    # Upside capped; premium received improves flat/down
    return ScenarioPayoff(
        round(min(base.up_pct, base.up_pct * 0.60) + premium_income_pct, 1),
        round(base.flat_pct + premium_income_pct, 1),
        round(base.down_pct + premium_income_pct, 1),
    )


def _payoff_roll_out(ctx: PositionContext, shock: float = 0.05,
                     roll_cost_pct: float = 15.0) -> ScenarioPayoff:
    """Roll to next expiry. Pay some debit; gain more time."""
    base = _payoff_hold(ctx, shock)
    # More time = less theta drag; up and flat improve
    return ScenarioPayoff(
        round(base.up_pct   + 10.0 - roll_cost_pct, 1),
        round(base.flat_pct + 15.0 - roll_cost_pct, 1),
        round(base.down_pct        - roll_cost_pct, 1),
    )


def _payoff_roll_up(ctx: PositionContext, shock: float = 0.05,
                    roll_cost_pct: float = 20.0) -> ScenarioPayoff:
    """Roll to higher strike (lock partial profit, maintain theta exposure)."""
    base = _payoff_hold(ctx, shock)
    return ScenarioPayoff(
        round(base.up_pct   * 0.70 - roll_cost_pct, 1),
        round(base.flat_pct        - roll_cost_pct, 1),
        round(base.down_pct * 0.50 - roll_cost_pct, 1),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Strategy scorer
# ══════════════════════════════════════════════════════════════════════════════

def _score_hold(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    """Returns (score, conditions_met, risks, action_label, timing_guidance)."""
    conditions = []
    risks      = []
    score      = 50.0

    # Theta decay zone — penalise HOLD
    if ctx.theta_danger:
        score -= 20
        risks.append(f"⚠️ Only {ctx.dte}d to expiry — theta accelerates sharply, each day costs ~${abs(ctx.theta_daily * ctx.contract_size):.2f}/contract")
    elif ctx.theta_caution:
        score -= 8
        risks.append(f"DTE {ctx.dte}d — theta caution zone, consider rolling or exiting")

    # Profit management
    if ctx.profit_pct >= _LOCK_PROFIT:
        score -= 15
        risks.append(f"Unrealised gain +{ctx.profit_pct:.0f}% is exceptional — risk of giving back profits")
        conditions.append(f"+{ctx.profit_pct:.0f}% profit on premium")
    elif ctx.profit_pct >= _STRONG_PROFIT:
        score -= 5
        conditions.append(f"+{ctx.profit_pct:.0f}% profit — still room to run")
    elif ctx.profit_pct > 0:
        score += 5
        conditions.append(f"+{ctx.profit_pct:.0f}% in profit — early stage")

    # Trend support
    if ctx.trend_bullish and ctx.option_type == "CALL":
        score += 10
        conditions.append("Underlying trend bullish — momentum supports CALL holding")
    elif ctx.trend_bearish:
        score -= 12
        risks.append("Underlying showing weakness (RSI/ADX decay)")

    # VIX regime
    if ctx.vix_regime == "EXTREME_FEAR":
        score -= 20
        risks.append("VIX extreme fear — forced exit risk")
    elif ctx.vix_regime == "FEAR":
        score -= 8

    # Deeply OTM — low chance of recovery
    if ctx.is_deep_otm:
        score -= 15
        risks.append(f"Position {abs(ctx.moneyness_pct):.1f}% OTM — recovery requires significant move")

    score = max(0.0, min(100.0, score))
    timing = (
        "Hold while underlying stays above key support and RSI > 50. "
        "Re-evaluate on any 5%+ adverse move or if DTE drops below 7."
    )
    return score, conditions, risks, "Hold existing position — monitor closely", timing


def _score_sell_close(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    conditions = []
    risks      = []
    score      = 40.0

    if ctx.profit_pct >= _LOCK_PROFIT:
        score += 30
        conditions.append(f"+{ctx.profit_pct:.0f}% profit — exceptional, consider locking in")
    elif ctx.profit_pct >= _STRONG_PROFIT:
        score += 20
        conditions.append(f"+{ctx.profit_pct:.0f}% profit — strong, reasonable to exit")
    elif ctx.profit_pct < 0:
        score -= 15   # selling a loser early is rarely optimal unless theta is killing
        risks.append(f"Selling at a loss (-{abs(ctx.profit_pct):.0f}%) — ensure no recovery path exists")

    if ctx.theta_danger:
        score += 20
        conditions.append(f"Only {ctx.dte}d DTE — selling avoids maximum theta burn")
    if ctx.trend_bearish:
        score += 10
        conditions.append("Signal decay detected in underlying — reduces recovery odds")
    if ctx.vix_regime == "EXTREME_FEAR":
        score += 15
        conditions.append("VIX extreme fear — taking profits defensively")

    score = max(0.0, min(100.0, score))
    timing = (
        "Execute at market open if underlying shows gap or reversal. "
        "Use limit order at current mid-price; don't chase the bid."
    )
    return score, conditions, risks, f"Close full position — lock in ${ctx.unrealised_pnl:+.2f} P&L", timing


def _score_partial_close(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    conditions = []
    risks      = []
    score      = 45.0

    if _STRONG_PROFIT <= ctx.profit_pct < _LOCK_PROFIT:
        score += 25
        conditions.append(f"+{ctx.profit_pct:.0f}% — locking half protects downside while keeping upside")
    elif ctx.profit_pct >= _BREAKEVEN_PROFIT:
        score += 10
        conditions.append(f"+{ctx.profit_pct:.0f}% — partial exit reduces cost basis")
    if ctx.theta_caution and not ctx.theta_danger:
        score += 8
        conditions.append(f"DTE {ctx.dte}d — partial reduction sensible before theta accelerates")
    if ctx.is_deep_itm:
        score += 5
        conditions.append("Deep ITM — high delta, partial exit captures intrinsic value")

    risks.append("Remaining 50% still exposed to reversal and theta decay")

    score = max(0.0, min(100.0, score))
    timing = (
        "Sell 50% at current market. Hold the remainder with a mental stop "
        "at 50% of remaining profit (i.e. exit rest if premium drops to your "
        "entry cost + current-profit×0.5)."
    )
    return score, conditions, risks, "Sell 50% now, hold rest with trailing mental stop", timing


def _score_protective_put(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    """Buying a put hedge on top of existing position."""
    conditions = []
    risks      = []
    score      = 30.0

    # Only makes sense if holding a call with significant profit
    if ctx.option_type == "PUT":
        return 0.0, [], ["Already holding a put — protective put doesn't apply"], "N/A", "N/A"

    if ctx.profit_pct >= _STRONG_PROFIT:
        score += 30
        conditions.append(f"Protecting +{ctx.profit_pct:.0f}% gain with downside hedge")
    if ctx.vix_regime in ("FEAR", "EXTREME_FEAR"):
        score += 15
        conditions.append("VIX elevated — cheap puts may have disappeared, but protection warranted")
    if ctx.trend_bearish:
        score += 12
        conditions.append("Underlying showing weakness — put hedge justified")
    if ctx.theta_caution:
        score -= 10
        risks.append("Buying a put with <21d DTE is also expensive — short-lived protection")

    risks.append("Put premium cost reduces net profit by ~3–8% of position value")
    risks.append("If underlying continues up, put expires worthless")

    score = max(0.0, min(100.0, score))
    timing = (
        "Buy ATM or 5% OTM put with same or later expiry as your call. "
        "Act before any earnings event or when underlying shows first sign of reversal."
    )
    return score, conditions, risks, "Buy ATM put to hedge downside — convert to synthetic long strangle", timing


def _score_collar(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    """Buy OTM put + sell OTM call."""
    conditions = []
    risks      = []
    score      = 25.0

    if ctx.option_type == "PUT":
        return 0.0, [], ["Collar is designed for long call / stock positions"], "N/A", "N/A"

    if ctx.profit_pct >= _LOCK_PROFIT:
        score += 35
        conditions.append(f"+{ctx.profit_pct:.0f}% gain — collar locks in most profit cheaply")
    elif ctx.profit_pct >= _STRONG_PROFIT:
        score += 20
        conditions.append(f"+{ctx.profit_pct:.0f}% gain — collar provides balanced protection")

    if ctx.is_deep_itm:
        score += 10
        conditions.append("Deep ITM — sold call premium will be rich, offsetting put cost")

    if ctx.theta_caution:
        score -= 8
        risks.append("Short expiry makes selling the call less effective — low premium received")
    if ctx.vix_regime == "NORMAL" and ctx.iv_contracting:
        risks.append("Low IV environment — put protection is cheap but sold call also earns little")

    risks.append("Caps upside beyond the sold call strike")

    score = max(0.0, min(100.0, score))
    timing = (
        "Execute when underlying is at or near a short-term resistance level. "
        "Sell the call 5–10% above current underlying; buy put 5–10% below. "
        "Best done in one simultaneous order to avoid leg risk."
    )
    return score, conditions, risks, "Implement collar — sell OTM call + buy OTM put simultaneously", timing


def _score_covered_call(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    """Sell a higher-strike call against existing long call (diagonal spread)."""
    conditions = []
    risks      = []
    score      = 20.0

    if ctx.option_type == "PUT":
        return 0.0, [], ["Covered call not applicable to a put position"], "N/A", "N/A"

    # Best when underlying is flat / range-bound
    if not ctx.trend_bullish and ctx.profit_pct >= _BREAKEVEN_PROFIT:
        score += 25
        conditions.append("Muted momentum — selling call generates income while waiting")
    elif ctx.profit_pct >= _STRONG_PROFIT and not ctx.trend_bullish:
        score += 20
        conditions.append(f"+{ctx.profit_pct:.0f}% profit + weakening trend — reduce exposure via credit")

    if ctx.iv_expanding:
        score += 10
        conditions.append("Elevated IV — sold call premium is richer")

    if ctx.theta_danger:
        score -= 15
        risks.append("Selling call near expiry creates pin risk and limited income")
    if ctx.trend_bullish:
        score -= 10
        risks.append("Strong uptrend — sold call may cap a continued move")

    risks.append("If underlying surges past sold strike, gains are capped")

    score = max(0.0, min(100.0, score))
    timing = (
        "Sell a call 5–10% OTM from current underlying with 2–4 weeks to expiry. "
        "Target credit that covers at least 5% of current premium value. "
        "Roll or close if underlying approaches sold strike."
    )
    return score, conditions, risks, "Sell OTM call (diagonal) — collect credit, reduce effective cost", timing


def _score_roll_out(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    conditions = []
    risks      = []
    score      = 20.0

    if ctx.theta_danger:
        score += 35
        conditions.append(f"Only {ctx.dte}d DTE — rolling out buys crucial additional time")
    elif ctx.theta_caution:
        score += 20
        conditions.append(f"DTE {ctx.dte}d — roll extends theta buffer meaningfully")

    if ctx.trend_bullish:
        score += 10
        conditions.append("Trend still intact — worth paying to extend")

    if ctx.profit_pct < -20:
        score -= 10
        risks.append(f"Position already -${abs(ctx.unrealised_pnl):.2f} — rolling costs more capital on a loser")
    if ctx.is_deep_otm:
        score -= 12
        risks.append("Deep OTM position — roll may still expire worthless even with extra time")

    risks.append("Roll costs a debit (buying extra time is not free)")
    score = max(0.0, min(100.0, score))
    timing = (
        "Roll when DTE is 14–21 days out and you still have conviction on direction. "
        "Target next monthly expiry (30–45 DTE). Only roll if the debit ≤ 30% of "
        "remaining intrinsic value or current premium."
    )
    return score, conditions, risks, f"Roll to next expiry — buy time, maintain {ctx.option_type} exposure", timing


def _score_roll_up(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    conditions = []
    risks      = []
    score      = 15.0

    if ctx.option_type == "PUT":
        return 0.0, [], ["Roll up (to higher strike) applies to call positions; use 'roll down' for puts"], "N/A", "N/A"

    if ctx.profit_pct >= _STRONG_PROFIT and ctx.is_itm:
        score += 35
        conditions.append(f"Deep ITM with +{ctx.profit_pct:.0f}% profit — roll up locks partial gain and resets leverage")
    elif ctx.profit_pct >= _BREAKEVEN_PROFIT:
        score += 15
        conditions.append(f"+{ctx.profit_pct:.0f}% — rolling up reduces delta while taking partial profits")

    if ctx.trend_bullish:
        score += 10
        conditions.append("Trend still bullish — higher strike still in range")

    risks.append("Costs a debit to roll up; reduces absolute dollar upside")
    risks.append("New ATM strike has lower delta — smaller $ gain per 1% move")

    score = max(0.0, min(100.0, score))
    timing = (
        "Roll up when underlying is at a new resistance / recent high. "
        "Sell the current ITM call, buy the next ATM call at same expiry. "
        "Ideal if remaining DTE > 21 days."
    )
    return score, conditions, risks, "Roll call to higher strike — lock partial profit, reset upside leverage", timing


def _score_cash_secured_put(ctx: PositionContext) -> tuple[float, list[str], list[str], str, str]:
    """Sell a cash-secured put below current underlying to collect premium at support."""
    conditions = []
    risks      = []
    score      = 10.0

    # Only relevant when you've exited or are planning to exit the call
    if ctx.profit_pct >= _LOCK_PROFIT and not ctx.theta_danger:
        score += 20
        conditions.append("After locking large gains, selling CSP at support is an income trade")
    if ctx.vix_regime in ("FEAR", "EXTREME_FEAR"):
        score += 15
        conditions.append("Elevated VIX — put premiums are rich; good time to be a seller")
    if ctx.trend_bullish:
        score += 10
        conditions.append("Bullish trend — puts less likely to be exercised")

    risks.append("Requires capital reserved = strike × 100 per contract")
    risks.append("If stock drops below sold strike, you are obligated to buy shares")

    score = max(0.0, min(100.0, score))
    timing = (
        "Sell a put at a key support level, 5–10% below current underlying. "
        "Target 14–30 DTE for optimal theta decay. "
        "Best entered after closing or reducing the main call position."
    )
    return score, conditions, risks, "Sell cash-secured put at support — generate income after partial exit", timing


# ══════════════════════════════════════════════════════════════════════════════
# Main Engine
# ══════════════════════════════════════════════════════════════════════════════

class OptionPositionManager:
    """
    Evaluates all available strategies for an existing option position
    and returns a ranked list with a primary recommendation.

    Usage (Signal Engine — pre-trade structuring)::

        mgr = OptionPositionManager()
        result = mgr.evaluate({
            "symbol":           "NASDAQ:GOOGL",
            "option_type":      "CALL",
            "strike":           335.0,
            "expiry":           "2025-05-16",
            "entry_premium":    1.5,
            "current_premium":  8.0,
            "underlying_price": 338.0,
        })
        print(result["primary"]["action_label"])
        print(result["summary"])

    Usage (Sell Engine — post-trade management)::

        from options.option_position_manager import OptionPositionManager
        mgr = OptionPositionManager()
        opt_result = mgr.evaluate(position_dict)
        action = opt_result["primary"]["action"]   # OptionStrategy enum value
    """

    # strategies to always evaluate (all enabled by default)
    _EVALUATORS = [
        (OptionStrategy.HOLD,             _score_hold),
        (OptionStrategy.SELL_CLOSE,       _score_sell_close),
        (OptionStrategy.PARTIAL_CLOSE,    _score_partial_close),
        (OptionStrategy.PROTECTIVE_PUT,   _score_protective_put),
        (OptionStrategy.COLLAR,           _score_collar),
        (OptionStrategy.COVERED_CALL,     _score_covered_call),
        (OptionStrategy.ROLL_OUT,         _score_roll_out),
        (OptionStrategy.ROLL_UP,          _score_roll_up),
        (OptionStrategy.CASH_SECURED_PUT, _score_cash_secured_put),
    ]

    _EMOJIS = {
        OptionStrategy.HOLD:             "⏸️",
        OptionStrategy.SELL_CLOSE:       "💰",
        OptionStrategy.PARTIAL_CLOSE:    "📤",
        OptionStrategy.PROTECTIVE_PUT:   "🛡️",
        OptionStrategy.COLLAR:           "🔒",
        OptionStrategy.COVERED_CALL:     "📈",
        OptionStrategy.ROLL_OUT:         "⏩",
        OptionStrategy.ROLL_UP:          "⬆️",
        OptionStrategy.CASH_SECURED_PUT: "🏦",
        OptionStrategy.LONG_CALL:        "🚀",
        OptionStrategy.LONG_PUT:         "🐻",
    }

    def evaluate(self, position: dict) -> dict:
        """
        Evaluate all strategies for a given option position.

        Parameters
        ----------
        position : dict
            Option position dict. Required keys: symbol, option_type, strike,
            expiry, entry_premium, current_premium, underlying_price.
            Optional: quantity, contract_size, iv, rsi, adx, vix.

        Returns
        -------
        dict with keys:
          "symbol"        : str
          "position_summary" : str  — concise P&L + status description
          "context"       : dict    — parsed position context
          "primary"       : dict    — top-ranked strategy
          "alternative"   : dict    — second-ranked strategy
          "all_strategies": list[dict]  — all evaluated, sorted by score
          "action_plan"   : str     — narrative action plan
          "timing_guidance" : str   — when to act
          "summary"       : str     — one-paragraph summary
          "warnings"      : list[str]
        """
        ctx = PositionContext.from_dict(position)
        results: list[StrategyResult] = []

        for strategy, scorer in self._EVALUATORS:
            score, conditions, risks, action_label, timing = scorer(ctx)
            if score <= 0:
                continue  # N/A for this position type
            payoff = self._get_payoff(strategy, ctx)
            results.append(StrategyResult(
                strategy        = strategy,
                rank            = 0,
                score           = round(score, 1),
                action_label    = action_label,
                rationale       = self._build_rationale(strategy, ctx, conditions, risks),
                timing_guidance = timing,
                scenario        = payoff,
                risks           = risks,
                conditions_met  = conditions,
                emoji           = self._EMOJIS.get(strategy, "📊"),
            ))

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        for i, r in enumerate(results):
            r.rank = i + 1

        primary     = results[0] if results else None
        alternative = results[1] if len(results) > 1 else None

        # Build narrative
        action_plan     = self._build_action_plan(ctx, primary, alternative)
        timing_guidance = primary.timing_guidance if primary else "No action identified."
        summary         = self._build_summary(ctx, primary, alternative)
        warnings        = self._build_warnings(ctx)

        return {
            "symbol":           ctx.symbol,
            "position_summary": self._position_summary(ctx),
            "context":          self._ctx_to_dict(ctx),
            "primary":          self._result_to_dict(primary),
            "alternative":      self._result_to_dict(alternative),
            "all_strategies":   [self._result_to_dict(r) for r in results],
            "action_plan":      action_plan,
            "timing_guidance":  timing_guidance,
            "summary":          summary,
            "warnings":         warnings,
        }

    # ─── Private helpers ──────────────────────────────────────────────────────

    def _get_payoff(self, strategy: OptionStrategy,
                    ctx: PositionContext) -> ScenarioPayoff:
        payoff_map = {
            OptionStrategy.HOLD:             lambda: _payoff_hold(ctx),
            OptionStrategy.SELL_CLOSE:       lambda: ScenarioPayoff(ctx.profit_pct, ctx.profit_pct, ctx.profit_pct),
            OptionStrategy.PARTIAL_CLOSE:    lambda: _payoff_partial_close(ctx),
            OptionStrategy.PROTECTIVE_PUT:   lambda: _payoff_protective_put(ctx),
            OptionStrategy.COLLAR:           lambda: _payoff_collar(ctx),
            OptionStrategy.COVERED_CALL:     lambda: _payoff_covered_call(ctx),
            OptionStrategy.ROLL_OUT:         lambda: _payoff_roll_out(ctx),
            OptionStrategy.ROLL_UP:          lambda: _payoff_roll_up(ctx),
            OptionStrategy.CASH_SECURED_PUT: lambda: ScenarioPayoff(
                ctx.profit_pct + 8, ctx.profit_pct + 8, ctx.profit_pct - 5),
        }
        fn = payoff_map.get(strategy)
        if fn:
            return fn()
        return ScenarioPayoff(0.0, 0.0, 0.0)

    def _build_rationale(self, strategy: OptionStrategy, ctx: PositionContext,
                          conditions: list[str], risks: list[str]) -> str:
        cond_text = "; ".join(conditions[:3]) if conditions else "General suitability"
        risk_text = risks[0] if risks else "Monitor closely"
        return (
            f"{cond_text}. "
            f"Key risk: {risk_text}. "
            f"Position is {abs(ctx.moneyness_pct):.1f}% "
            f"{'ITM' if ctx.moneyness_pct > 0 else 'OTM'} "
            f"with {ctx.dte}d remaining and {ctx.profit_pct:+.0f}% premium gain."
        )

    def _build_action_plan(self, ctx: PositionContext,
                            primary: Optional[StrategyResult],
                            alternative: Optional[StrategyResult]) -> str:
        if not primary:
            return "No clear action identified. Reassess position fundamentals."

        plan = (
            f"PRIMARY ACTION ({primary.emoji} {primary.strategy.value}): "
            f"{primary.action_label}. {primary.timing_guidance}\n\n"
        )
        if alternative:
            plan += (
                f"ALTERNATIVE ({alternative.emoji} {alternative.strategy.value}): "
                f"{alternative.action_label}. {alternative.timing_guidance}\n\n"
            )
        plan += (
            f"POSITION STATUS: {ctx.option_type} ${ctx.strike:.0f} "
            f"| Premium {ctx.entry_premium:.2f} → {ctx.current_premium:.2f} "
            f"({ctx.profit_pct:+.0f}%) "
            f"| {ctx.dte}d DTE "
            f"| Underlying ${ctx.underlying_price:.2f} "
            f"({'ITM' if ctx.is_itm else 'OTM'} by {abs(ctx.moneyness_pct):.1f}%)"
        )
        return plan

    def _build_summary(self, ctx: PositionContext,
                        primary: Optional[StrategyResult],
                        alternative: Optional[StrategyResult]) -> str:
        if not primary:
            return "Insufficient data to recommend a strategy."

        pnl = f"${ctx.unrealised_pnl:+.2f}"
        theta_note = ""
        if ctx.theta_danger:
            theta_note = f" Theta is burning ~${abs(ctx.theta_daily * ctx.contract_size):.2f}/day — time is critical."

        alt_note = (
            f" As an alternative, {alternative.action_label.lower()}."
            if alternative else ""
        )

        return (
            f"Your {ctx.option_type} ${ctx.strike:.0f} position is "
            f"{abs(ctx.moneyness_pct):.1f}% {'ITM' if ctx.is_itm else 'OTM'} "
            f"with {pnl} unrealised P&L ({ctx.profit_pct:+.0f}% on premium) "
            f"and {ctx.dte} days to expiry.{theta_note} "
            f"Best strategy: {primary.emoji} {primary.action_label}.{alt_note}"
        )

    def _build_warnings(self, ctx: PositionContext) -> list[str]:
        warnings = []
        if ctx.theta_danger:
            warnings.append(
                f"🔴 THETA DANGER: Only {ctx.dte}d to expiry. "
                f"Daily theta cost ~${abs(ctx.theta_daily * ctx.contract_size):.2f}/contract."
            )
        elif ctx.theta_caution:
            warnings.append(
                f"⚠️ DTE CAUTION: {ctx.dte}d remaining — theta decay accelerating."
            )
        if ctx.vix_regime == "EXTREME_FEAR":
            warnings.append("🔴 VIX EXTREME FEAR — forced exits possible, option premiums volatile.")
        elif ctx.vix_regime == "FEAR":
            warnings.append("⚠️ VIX ELEVATED — expect wider bid-ask spreads.")
        if ctx.is_deep_otm:
            warnings.append(
                f"⚠️ DEEP OTM: Position {abs(ctx.moneyness_pct):.1f}% out-of-the-money."
            )
        if ctx.profit_pct >= _LOCK_PROFIT:
            warnings.append(
                f"💰 EXCEPTIONAL GAIN: +{ctx.profit_pct:.0f}% on premium — "
                "consider locking in at least partial profits."
            )
        return warnings

    def _position_summary(self, ctx: PositionContext) -> str:
        moneyness = f"{'ITM' if ctx.is_itm else 'OTM'} {abs(ctx.moneyness_pct):.1f}%"
        return (
            f"{ctx.option_type} ${ctx.strike:.0f} @ ${ctx.current_premium:.2f} "
            f"(entry ${ctx.entry_premium:.2f}, {ctx.profit_pct:+.0f}%) | "
            f"{moneyness} | DTE {ctx.dte} | "
            f"Underlying ${ctx.underlying_price:.2f}"
        )

    def _ctx_to_dict(self, ctx: PositionContext) -> dict:
        return {
            "symbol":           ctx.symbol,
            "option_type":      ctx.option_type,
            "strike":           ctx.strike,
            "expiry":           ctx.expiry_date.isoformat(),
            "dte":              ctx.dte,
            "entry_premium":    ctx.entry_premium,
            "current_premium":  ctx.current_premium,
            "profit_pct":       round(ctx.profit_pct, 1),
            "unrealised_pnl":   round(ctx.unrealised_pnl, 2),
            "underlying_price": ctx.underlying_price,
            "moneyness_pct":    round(ctx.moneyness_pct, 2),
            "is_itm":           ctx.is_itm,
            "iv":               round(ctx.iv * 100, 1) if ctx.iv else None,
            "delta":            round(ctx.delta, 3),
            "theta_daily":      round(ctx.theta_daily, 4),
            "vix_regime":       ctx.vix_regime,
        }

    def _result_to_dict(self, r: Optional[StrategyResult]) -> Optional[dict]:
        if r is None:
            return None
        return {
            "action":          r.strategy.value,
            "rank":            r.rank,
            "score":           r.score,
            "emoji":           r.emoji,
            "action_label":    r.action_label,
            "rationale":       r.rationale,
            "timing_guidance": r.timing_guidance,
            "conditions_met":  r.conditions_met,
            "risks":           r.risks,
            "scenario": {
                "up_5pct":   r.scenario.up_pct,
                "flat":      r.scenario.flat_pct,
                "down_5pct": r.scenario.down_pct,
            },
        }


# ══════════════════════════════════════════════════════════════════════════════
# Standalone CLI / demo
# ══════════════════════════════════════════════════════════════════════════════

def _demo() -> None:
    """Run the Google example from the requirement spec."""
    import json as _json

    position = {
        "symbol":           "NASDAQ:GOOGL",
        "option_type":      "CALL",
        "strike":           335.0,
        "expiry":           "2025-05-16",
        "entry_premium":    1.5,
        "current_premium":  8.0,
        "underlying_price": 338.0,
        "quantity":         1,
        "contract_size":    100,
        "rsi":              62.0,
        "adx":              28.0,
        "vix":              18.5,
    }

    mgr    = OptionPositionManager()
    result = mgr.evaluate(position)

    print("\n" + "═" * 70)
    print("  OPTION POSITION MANAGER — ANALYSIS REPORT")
    print("═" * 70)
    print(f"  Position : {result['position_summary']}")
    print("═" * 70)

    ctx = result["context"]
    print(f"  IV (solved) : {ctx['iv']}%     Delta : {ctx['delta']}     Theta : ${ctx['theta_daily']:.4f}/day")
    print(f"  VIX Regime  : {ctx['vix_regime']}")
    print()

    if result["warnings"]:
        for w in result["warnings"]:
            print(f"  {w}")
        print()

    print("  STRATEGY RANKINGS:")
    print(f"  {'#':<3} {'Strategy':<20} {'Score':>6}  {'UP+5%':>7} {'FLAT':>7} {'DOWN-5%':>9}  Action")
    print(f"  {'-'*3} {'-'*20} {'-'*6}  {'-'*7} {'-'*7} {'-'*9}  {'-'*35}")
    for s in result["all_strategies"]:
        sc    = s["scenario"]
        print(
            f"  {s['rank']:<3} {s['emoji']} {s['action']:<18} {s['score']:>6.1f}  "
            f"{sc['up_5pct']:>+6.1f}%  {sc['flat']:>+6.1f}%  {sc['down_5pct']:>+8.1f}%  "
            f"{s['action_label'][:40]}"
        )

    print()
    print("  PRIMARY RECOMMENDATION:")
    pr = result["primary"]
    print(f"    {pr['emoji']} {pr['action']} (Score: {pr['score']:.0f}/100)")
    print(f"    {pr['action_label']}")
    print(f"    Timing: {pr['timing_guidance'][:120]}")

    print()
    if result["alternative"]:
        alt = result["alternative"]
        print("  ALTERNATIVE:")
        print(f"    {alt['emoji']} {alt['action']} (Score: {alt['score']:.0f}/100)")
        print(f"    {alt['action_label']}")

    print()
    print("  ACTION PLAN:")
    for line in result["action_plan"].split("\n"):
        if line.strip():
            print(f"    {line}")

    print()
    print("  SUMMARY:")
    print(f"    {result['summary']}")
    print("═" * 70 + "\n")


if __name__ == "__main__":
    import argparse, json as _json, sys as _sys

    if _sys.platform == "win32":
        if hasattr(_sys.stdout, "reconfigure"):
            _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Option Position Manager — Evaluate strategies for existing option holdings"
    )
    parser.add_argument(
        "--position", type=str, default=None,
        help="JSON string or path to JSON file with position dict"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Run with built-in Google CALL example"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output result as JSON"
    )
    args = parser.parse_args()

    if args.demo or args.position is None:
        _demo()
    else:
        # Load position from JSON string or file
        raw = args.position.strip()
        if raw.startswith("{"):
            pos = _json.loads(raw)
        else:
            with open(raw) as f:
                pos = _json.load(f)

        mgr    = OptionPositionManager()
        result = mgr.evaluate(pos)

        if args.json:
            print(_json.dumps(result, indent=2, default=str))
        else:
            # Brief console output
            print(f"\nPosition  : {result['position_summary']}")
            print(f"Primary   : {result['primary']['emoji']} {result['primary']['action']} — {result['primary']['action_label']}")
            print(f"Alternative: {result['alternative']['emoji']} {result['alternative']['action']} — {result['alternative']['action_label']}" if result["alternative"] else "Alternative: None")
            print(f"\nSummary: {result['summary']}")
