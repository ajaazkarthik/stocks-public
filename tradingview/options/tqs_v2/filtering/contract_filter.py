"""
TQS v2 Contract Quality Filter
================================
Applies strict quality gates to RawContracts before scoring.

Filters:
  0. Intrinsic value sanity (skip split-unadjusted / bad data contracts)
  1. Bid > config.contract_min_bid  (skip zero-bid illiquid contracts)
  2. Premium <= config.contract_max_premium  (skip contracts exceeding budget)
  3. Spread % <= config.contract_max_spread_pct  (skip wide-spread contracts)
  4. Delta in valid range for option_type  (skip near-worthless or deep ITM)
  5. OI >= config.min_open_interest  (skip contracts with no open interest)
  6. DTE in [min_dte, max_dte]  (skip near-expiry and far-dated)
  7. SMA200 alignment (calls >3% below SMA200 → REJECT; puts >5% above SMA200 → REJECT)
  8. Directional bias (SMA50 gate — calls need spot≥SMA50; puts need spot≤SMA50)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import TQSv2Config
from ..interfaces.contracts import IContractFilter
from ..options_data.models import RawContract

_log = logging.getLogger("tqs_v2.filtering.contract_filter")


class IntrinsicValueFilter(IContractFilter):
    """
    Reject contracts whose ask price is below 80% of intrinsic value.

    This catches split-unadjusted strikes (WMT 3:1, GOOG 20:1) and stale
    spot prices that produce impossible pricing artefacts.  A legitimate
    option cannot trade below its intrinsic value (arbitrage floor), so
    ask < 0.80 × intrinsic is unambiguously bad data.

    Only fires when intrinsic > 0 (i.e. contract is in-the-money).
    OTM contracts (intrinsic == 0) are always passed through.
    """

    @property
    def name(self) -> str:
        return "ask_below_intrinsic"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:  # noqa: ARG002
        if contract.spot_price <= 0:
            return True  # no spot data — cannot compute, pass through
        if contract.option_type == "call":
            intrinsic = max(0.0, contract.spot_price - contract.strike)
        else:
            intrinsic = max(0.0, contract.strike - contract.spot_price)
        if intrinsic <= 0:
            return True  # OTM — no intrinsic floor applies
        # Reject when ask < 80% of intrinsic (must be bad / stale data)
        return contract.ask >= intrinsic * 0.80


class BidFilter(IContractFilter):
    """Reject contracts with no meaningful bid."""

    @property
    def name(self) -> str:
        return "bid_floor"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        return contract.bid >= config.contract_min_bid


class PremiumFilter(IContractFilter):
    """Reject contracts whose ask price exceeds the budget cap (contract_max_premium)."""

    @property
    def name(self) -> str:
        return "premium_max"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        return contract.ask <= config.contract_max_premium


class SpreadFilter(IContractFilter):
    """Reject contracts with excessively wide spread."""

    @property
    def name(self) -> str:
        return "spread_ceiling"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        # Allow contracts with spread_pct == 0 (could be market-closed data)
        if contract.spread_pct <= 0:
            return True
        return contract.spread_pct <= config.contract_max_spread_pct


class DeltaFilter(IContractFilter):
    """Reject contracts outside the directional delta range."""

    @property
    def name(self) -> str:
        return "delta_range"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        d = contract.delta
        if contract.option_type == "call":
            return config.call_min_delta <= d <= config.call_max_delta
        else:  # put
            return config.put_min_delta <= d <= config.put_max_delta


class OpenInterestFilter(IContractFilter):
    """Reject contracts with insufficient open interest."""

    @property
    def name(self) -> str:
        return "oi_floor"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        # None means yfinance data is unavailable (e.g., post-holiday Yahoo refresh).
        # Pass the contract through — volume filter provides a secondary liquidity guard.
        if contract.open_interest is None:
            return True
        return contract.open_interest >= config.min_open_interest


class DTEFilter(IContractFilter):
    """Reject contracts outside the target DTE window."""

    @property
    def name(self) -> str:
        return "dte_window"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        return config.contract_min_dte <= contract.dte <= config.contract_max_dte


class ATRReachabilityFilter(IContractFilter):
    """
    Rejects contracts where the breakeven point is further than the stock
    can realistically travel given its Average True Range (ATR).

    Formula: breakeven_distance <= stock_atr × sqrt(trading_days) × 1.8
    where trading_days = dte × 5/7

    Rationale: ATR represents ~1 average daily trading range. Over N trading days,
    the projected 1.8σ range is ATR × sqrt(trading_days) × 1.8.
    Fix 2026-05-27 (M-1): 2.5σ filtered ~1.2% of outcomes (useless). 1.8σ filters ~6%.
    Using trading days (not calendar weeks) is correct because ATR is computed
    on trading-day bars, not calendar-day intervals.

    Fix 2026-05-26: Previous formula used calendar weeks (dte/7), which is sqrt(5)≈2.24×
    too conservative — AAPL with ATR=$3, 52 DTE: old projected $6.34 vs correct $14.17.
    This caused valid momentum contracts to be incorrectly rejected.

    Note: Passes contracts with missing stock_atr data (non-blocking).
    """

    @property
    def name(self) -> str:
        return "atr_reachability"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:  # noqa: ARG002
        atr = getattr(contract, "stock_atr", None)
        if atr is None or atr <= 0:
            return True  # no ATR data → do not reject

        spot = getattr(contract, "spot_price", 0.0) or 0.0
        if spot <= 0:
            return True

        dte = int(getattr(contract, "dte", 0) or 0)
        if dte <= 0:
            return True

        # Project ATR over DTE using TRADING DAYS (not calendar weeks).
        # ATR is a daily trading-bar metric — scaling requires trading days.
        # Fix 2026-05-26: dte / 7.0 (calendar weeks) was 2.24× too conservative.
        # Correct: trading_days = dte × (5 trading days / 7 calendar days).
        trading_days = max(1.0, dte * 5.0 / 7.0)
        projected_range = atr * (trading_days ** 0.5) * 1.8

        breakeven = getattr(contract, "breakeven", None) or spot
        breakeven_distance = abs(float(breakeven) - spot)

        return breakeven_distance <= projected_range


class SMA200AlignmentFilter(IContractFilter):
    """
    Hard gate: reject contracts whose stock is positioned against the trade direction
    relative to its 200-day simple moving average.

    Rules:
      CALL: reject when spot < sma200 × (1 - call_sma200_reject_below_pct)
            i.e. stock is more than X% below the SMA200 → bearish trend → no calls
      PUT:  reject when spot > sma200 × (1 + put_sma200_reject_above_pct)
            i.e. stock is more than X% above the SMA200 → strong uptrend → no puts

    Defaults (from config):
      call_sma200_reject_below_pct = 0.03  (3% below SMA200 → reject CALL)
      put_sma200_reject_above_pct  = 0.05  (5% above SMA200 → reject PUT)

    Pass-through when SMA200 data is unavailable (non-blocking).

    Rationale: SMA200 is the canonical long-term trend anchor. Buying calls against
    a confirmed downtrend (below SMA200) or puts against a confirmed uptrend (well
    above SMA200) significantly reduces win probability. This hard gate removes those
    directional conflicts before scoring — the SMA200 scoring penalty in stock_trend.py
    was insufficient (only 2.4 composite point impact as identified by Stocks Critic).
    [Added 2026-05-27 — TaskD BLOCKING finding]
    """

    @property
    def name(self) -> str:
        return "sma200_alignment"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        sma200 = getattr(contract, "stock_sma200", None)
        spot = getattr(contract, "spot_price", None)
        option_type = getattr(contract, "option_type", None)

        # Pass-through when key data is missing
        if sma200 is None or spot is None or sma200 <= 0 or spot <= 0:
            return True
        if option_type not in ("call", "put"):
            return True

        if option_type == "call":
            # Reject CALLs when stock is more than X% below SMA200
            reject_threshold = sma200 * (1.0 - config.call_sma200_reject_below_pct)
            if spot < reject_threshold:
                _log.debug(
                    "SMA200AlignmentFilter: CALL rejected — spot=%.2f is %.1f%% below SMA200=%.2f",
                    spot,
                    (sma200 - spot) / sma200 * 100,
                    sma200,
                )
                return False
        else:  # put
            # Reject PUTs when stock is more than X% above SMA200
            accept_threshold = sma200 * (1.0 + config.put_sma200_reject_above_pct)
            if spot > accept_threshold:
                _log.debug(
                    "SMA200AlignmentFilter: PUT rejected — spot=%.2f is %.1f%% above SMA200=%.2f",
                    spot,
                    (spot - sma200) / sma200 * 100,
                    sma200,
                )
                return False
        return True


class DirectionalBiasFilter(IContractFilter):
    """Reject contracts that conflict with short-term directional bias (SMA50).

    Root cause (2026-05-30): WMT appeared in BOTH calls and puts reports because
    the SMA200 filter has a 13% overlap zone (from -3% to +10% around SMA200).
    Any stock within this band passes both call AND put filters — a fundamental
    directional conflict for a directional options buyer.

    Fix: Use SMA50 as the short-term directional gate:
      CALL: spot must be >= SMA50 (bullish short-term momentum)
      PUT:  spot must be <= SMA50 (bearish short-term momentum)

    This is mutually exclusive — a stock can only be above OR below SMA50, never
    both — so the same stock CANNOT appear in both calls and puts.

    Pass-through when SMA50 data is unavailable (non-blocking).
    [Added 2026-05-30 — directional conflict fix]
    """

    @property
    def name(self) -> str:
        return "directional_bias"

    def accepts(self, contract: "RawContract", config: "TQSv2Config") -> bool:
        sma50 = getattr(contract, "stock_sma50", None)
        spot = getattr(contract, "spot_price", None)
        option_type = getattr(contract, "option_type", None)

        # Pass-through when key data is missing
        if sma50 is None or spot is None or sma50 <= 0 or spot <= 0:
            return True
        if option_type not in ("call", "put"):
            return True

        if option_type == "call":
            # Reject CALLs when spot is below SMA50 (bearish short-term)
            if spot < sma50:
                _log.debug(
                    "DirectionalBiasFilter: CALL rejected — spot=%.2f < SMA50=%.2f (bearish bias)",
                    spot, sma50,
                )
                return False
        else:  # put
            # Reject PUTs when spot is above SMA50 (bullish short-term)
            if spot > sma50:
                _log.debug(
                    "DirectionalBiasFilter: PUT rejected — spot=%.2f > SMA50=%.2f (bullish bias)",
                    spot, sma50,
                )
                return False
        return True


class VolumeFilter(IContractFilter):
    """Reject contracts with insufficient daily trading volume.

    Root cause (2026-05-25): CMCSA P25 volume=48 passed because OI=4112 compensated
    in the liquidity formula (0.5×oi_score + 0.3×spread_score + 0.2×vol_score).
    A contract with volume=48 cannot be traded at the mid price without severe slippage
    regardless of open interest.  This gate enforces a hard minimum.
    """

    @property
    def name(self) -> str:
        return "volume_floor"

    def accepts(self, contract: "RawContract", config: "TQSv2Config") -> bool:
        vol = getattr(contract, "volume", None)
        if vol is None or int(vol) == 0:
            return True  # no volume data or market closed (TV scan2 returns 0 after hours) → don't hard-reject
        return int(vol) >= config.min_option_volume


class IVExtremesFilter(IContractFilter):
    """Reject contracts with extreme IV + IV backwardation (vol crush traps).

    Motivation (Fix 2026-05-26 — CRITICAL-I): INTC at IV=87%+ with strong backwardation
    (IV_1m >> IV_3m) is a textbook vol crush setup:
      - IV is inflated by earnings / event uncertainty
      - After the event, IV collapses 40-60% in a single session
      - Option buyer suffers double-loss: direction wrong + vol crush
      - Even correct direction often loses because IV drops faster than delta gains

    This filter rejects when BOTH conditions hold:
      1. IV >= 0.60 (60% — extreme volatility regime)
      2. IV backwardation: iv_contango_score < 20 (near-term IV > term structure average)

    Either condition alone is acceptable (high IV with flat term = vol event; low IV with
    backwardation = minor curve shape). BOTH together = strong vol crush risk.
    """

    @property
    def name(self) -> str:
        return "iv_extremes_backwardation"

    def accepts(self, contract: "RawContract", config: "TQSv2Config") -> bool:
        iv = getattr(contract, "iv", None)
        iv_contango_score = getattr(contract, "iv_contango_score", None)

        if iv is None:
            return True  # no IV data → let scorer penalise

        iv_f = float(iv)
        # IV threshold: 0.60 (60%) — above this is extreme event-driven volatility
        if iv_f < 0.60:
            return True  # IV not extreme → no backwardation risk filter needed

        # IV is extreme. Check for backwardation (near-term IV > term structure).
        # iv_contango_score < 20 = strong backwardation (near-term expensive relative to term).
        if iv_contango_score is not None and float(iv_contango_score) < 20.0:
            return False  # reject: extreme IV + backwardation = vol crush trap

        return True  # extreme IV but contango/flat term = elevated but not a crush setup


class EarningsProximityFilter(IContractFilter):
    """Reject contracts whose expiry window overlaps a scheduled earnings event.

    Fix 2026-05-27 (C-4): Moved from scoring/engine.py to the filter pipeline
    so contracts near earnings are rejected BEFORE expensive enrichment/scoring
    stages (saving 30+ seconds per contract).

    Gate: reject if 0 <= earnings_dte < (contract_dte + config.earnings_buffer_days)
    The buffer (default 21 days) accounts for IV inflation that typically begins
    2-3 weeks before an earnings announcement.

    Pass-through when earnings data is unavailable (non-blocking).
    """

    @property
    def name(self) -> str:
        return "earnings_proximity"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:
        # Catalyst mode: SEEK earnings proximity, don't reject it
        if config.catalyst_mode:
            return True

        import math

        earnings_dte = getattr(contract, "earnings_dte", None)
        if earnings_dte is None:
            return True  # no earnings data → don't reject

        try:
            earnings_dte_f = float(earnings_dte)
        except (ValueError, TypeError):
            return True

        if math.isnan(earnings_dte_f):
            return True

        # Sanity cap: TV may return epoch timestamps or very large values
        if earnings_dte_f > 365:
            _log.warning(
                "earnings_dte=%s suspiciously large for %s — treating as no data",
                earnings_dte_f,
                getattr(contract, "opra_code", "?"),
            )
            return True

        # Negative earnings_dte means earnings already passed → accept
        if earnings_dte_f < 0:
            return True

        contract_dte = max(1, int(getattr(contract, "dte", 45) or 45))
        buffer = config.earnings_buffer_days

        # Reject if earnings falls within contract window + IV inflation buffer
        if 0 <= earnings_dte_f < (contract_dte + buffer):
            _log.debug(
                "earnings proximity rejected %s: earnings in %.0fd, threshold=%dd (dte=%d + %dd buffer)",
                getattr(contract, "opra_code", "?"),
                earnings_dte_f,
                contract_dte + buffer,
                contract_dte,
                buffer,
            )
            return False

        return True


class ThetaEfficiencyFilter(IContractFilter):
    """Reject contracts where daily theta decay outpaces expected daily stock movement.

    Fix 2026-05-27 (C-2): The #1 reason retail option buyers lose money — theta
    eats the expected move. This filter computes:
      daily_theta_cost_pct = |theta| / ask   (% of premium lost per day)
      daily_expected_move  = atr / spot       (stock's avg daily % move)
      theta_efficiency     = daily_expected_move / daily_theta_cost_pct

    Reject when theta_efficiency < 0.5 (theta is 2× the expected move).
    Pass-through when data is missing (non-blocking).
    """

    MIN_THETA_EFFICIENCY = 0.5  # default threshold

    @property
    def name(self) -> str:
        return "theta_efficiency"

    def accepts(self, contract: RawContract, config: TQSv2Config) -> bool:  # noqa: ARG002
        theta = getattr(contract, "theta", None)
        ask = getattr(contract, "ask", None)
        atr = getattr(contract, "stock_atr", None)
        spot = getattr(contract, "spot_price", None)

        # Skip filter if data missing — don't reject on missing data
        if theta is None or ask is None or atr is None or spot is None:
            return True
        if ask <= 0 or spot <= 0 or atr <= 0:
            return True

        daily_theta_cost_pct = abs(float(theta)) / float(ask)
        daily_expected_move = float(atr) / float(spot)

        if daily_theta_cost_pct <= 0:
            return True  # no theta decay = fine

        theta_efficiency = daily_expected_move / daily_theta_cost_pct

        if theta_efficiency < self.MIN_THETA_EFFICIENCY:
            _log.debug(
                "ThetaEfficiencyFilter: rejected %s — efficiency=%.2f < %.2f "
                "(theta=%.4f, ask=%.2f, atr=%.2f, spot=%.2f)",
                getattr(contract, "opra_code", "?"),
                theta_efficiency,
                self.MIN_THETA_EFFICIENCY,
                float(theta),
                float(ask),
                float(atr),
                spot,
            )
            return False
        return True



