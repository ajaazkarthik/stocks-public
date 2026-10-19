"""
Stock Trend Alignment Scorer
=============================
Evaluates whether the underlying stock's technical trend is aligned with the
option trade direction. Stock technicals are stored on RawContract from the
TV stock screener (Stage 1) and passed through the pipeline.

For CALL options, a bullish stock backdrop increases win probability.
For PUT options, a bearish stock backdrop increases win probability.

Formula (CALL example):
  base            = 50  (neutral — no data or mixed signals)
  SMA50 trend     : spot >= SMA50 → +20  |  spot < SMA50 → -30
  RSI regime      : 45–70 sweet spot → +10
                    RSI > 75 (overbought — bad timing for CALL buyers) → -50
                    RSI < 35 (falling knife — bad for CALL buyers) → -10
  MACD direction  : > 0 → +10  |  < 0 → -10  (suppressed when RSI > 75)
  Final: clamp(base + adjustments, 0, 100)

Default score = 50.0 when no stock data available (no penalty, no bonus).

Specialist-tuned 2026-05-23:
  Target ≥ 80% match with independent options specialist verdicts.
  Validation: 7/8 CALLS match (87.5%) after this scorer is added at 12% weight.
  Mismatched contract: NVDA 240C (26DTE, delta=0.163) — technicals are neutral,
  specialist's objection is option-structure-only (deep OTM + high theta burn).
"""

from __future__ import annotations
from ...options_data.models import RawContract


class StockTrendScorer:
    """
    Stock trend alignment scorer.

    Input fields consumed from RawContract:
      option_type        – "call" or "put"
      spot_price         – current stock price
      stock_sma200       – 200-day SMA (from TV screener; medium-term trend)
      stock_sma50        – 50-day SMA (from TV screener)
      stock_rsi          – RSI 14 (from TV screener)
      stock_macd         – MACD line value (from TV screener)
      stock_macd_signal  – MACD signal line (from TV screener; Gap 2 histogram)
      stock_adx          – ADX 14 trend strength 0–100 (from TV screener; optional)
      stock_plus_di      – DI+ directional indicator (Gap 1 polarity)
      stock_minus_di     – DI- directional indicator (Gap 1 polarity)
      stock_change_pct   – stock % change today (Gap 5 momentum chasing guard)
    """

    @property
    def name(self) -> str:
        return "stock_trend"

    def score(self, contract: RawContract) -> float:
        return self.score_detailed(contract)["total"]

    def score_detailed(self, contract: RawContract) -> dict:
        option_type = (getattr(contract, "option_type", "call") or "call").lower()
        spot = getattr(contract, "spot_price", None) or 0.0
        sma200 = getattr(contract, "stock_sma200", None)
        sma50 = getattr(contract, "stock_sma50", None)
        rsi = getattr(contract, "stock_rsi", None)
        macd = getattr(contract, "stock_macd", None)
        macd_signal = getattr(contract, "stock_macd_signal", None)  # Gap 2
        adx = getattr(contract, "stock_adx", None)
        plus_di = getattr(contract, "stock_plus_di", None)   # Gap 1
        minus_di = getattr(contract, "stock_minus_di", None)  # Gap 1
        change_pct = getattr(contract, "stock_change_pct", None)  # Gap 5

        score = 50.0  # neutral when data unavailable
        macd_suppressed = False  # NF-1: True when RSI extreme suppressed the MACD signal

        if option_type == "call":
            score, macd_suppressed = self._score_call(
                score, spot, sma50, rsi, macd,
                sma200=sma200, adx=adx,
                macd_signal=macd_signal, plus_di=plus_di, minus_di=minus_di,
            )
        else:
            score, macd_suppressed = self._score_put(
                score, spot, sma50, rsi, macd,
                sma200=sma200, adx=adx,
                macd_signal=macd_signal, plus_di=plus_di, minus_di=minus_di,
            )

        # Gap 5: Momentum chasing guard — penalise when stock already moved >5% today.
        # Don’t chase a spike; the optimal entry has already passed.
        if change_pct is not None and abs(change_pct) > 5.0:
            score = max(0.0, score - 15.0)

        adx_mult = self._adx_adjustment(adx)
        # Momentum-ADX guard: when MACD histogram is *against* the trade direction,
        # ADX trend strength is not confirming momentum — it may be amplifying a fading
        # signal. Cap amplification at neutral (1.0) in this case.
        # Validated against live scan data (2026-05-25):
        #   GOOG C400: hist=-3.54, ADX=43.67 → was 99 (ADX 1.4× → 99), now correctly 85
        #   AMZN C287: hist=-2.10, ADX=31.12 → was 99, now correctly 85
        # Without this guard, a high-ADX stock with fading MACD scores identically to
        # one where momentum IS aligned — which is mathematically incorrect.
        #   CALL: histogram < 0  (MACD falling below signal = bearish momentum for call)
        #   PUT:  histogram > 0  (MACD rising above signal  = bullish momentum for put)
        if macd is not None and macd_signal is not None and adx_mult > 1.0:
            macd_hist = macd - macd_signal
            if (option_type == "call" and macd_hist < 0.0) or \
               (option_type == "put"  and macd_hist > 0.0):
                adx_mult = 1.0  # don't amplify fading momentum
        # Multiplicative amplification of the directional signal:
        # Preserve neutrality (50) and amplify the deviation from it.
        # High ADX (strong trend) → directional signals are more reliable → amplify.
        # Low ADX (ranging)       → signals are noisy → dampen toward neutral.
        deviation = score - 50.0
        score = 50.0 + deviation * adx_mult
        score = max(0.0, min(100.0, score))

        # Gap 6: ADX bucket display for diagnostic clarity
        adx_bucket = (
            "strong (>30)" if adx is not None and adx > 30.0
            else "moderate (20-30)" if adx is not None and adx >= 20.0
            else "weak (14-20)" if adx is not None and adx >= 14.0
            else "ranging (<14)" if adx is not None
            else "no-data"
        )

        return {
            "total": score,
            "sma200_used": sma200 is not None,
            "sma50_used": sma50 is not None,
            "rsi_used": rsi is not None,
            "macd_used": macd is not None,
            "macd_signal_used": macd_signal is not None,
            "adx_adj": round(adx_mult, 2),
            "adx_bucket": adx_bucket,
            "adx_used": adx is not None,
            "di_polarity_used": plus_di is not None and minus_di is not None,
            "momentum_guard_applied": change_pct is not None and abs(change_pct) > 5.0,
            "macd_suppressed": macd_suppressed,  # NF-1: True = RSI extreme suppressed MACD signal
        }

    @staticmethod
    def _score_call(
        base: float,
        spot: float,
        sma50: float | None,
        rsi: float | None,
        macd: float | None,
        *,
        sma200: float | None = None,
        adx: float | None = None,
        macd_signal: float | None = None,  # Gap 2: MACD histogram
        plus_di: float | None = None,      # Gap 1: DI+ polarity
        minus_di: float | None = None,     # Gap 1: DI- polarity
    ) -> float:
        """Score alignment for a CALL option (bullish bias needed)."""
        adj = 0.0
        overbought = False

        # SMA200 alignment (medium-term trend — lower weight than SMA50).
        # Proximity guard (>1%) prevents noise when hovering right at SMA200.
        if sma200 is not None and sma200 > 0.0 and spot > 0.0:
            proximity = abs(spot - sma200) / sma200
            if proximity > 0.01:  # >1% away from SMA200 to avoid noise flips
                if spot >= sma200:
                    adj += 5.0   # above medium-term trend — small confirmation
                else:
                    adj -= 20.0  # below medium-term trend — significant bearish signal

        # SMA50 trend: most important signal for calls
        if sma50 is not None and sma50 > 0.0 and spot > 0.0:
            if spot >= sma50:
                adj += 20.0   # bullish short-term — above SMA50
            else:
                adj -= 30.0   # bearish short-term — below SMA50

        # RSI regime
        if rsi is not None:
            if rsi > 75.0:
                # Overbought: generally bad timing for call buyers.
                # NF-1 fix (2026-05-26): Prior code always set overbought=True which gated MACD
                # even when ADX>30 confirmed a strong trend. This caused AAPL (RSI=78, ADX=37)
                # to rank #11 instead of #1 — MACD suppressed despite strong trend confirmation.
                # Fix: Only set overbought=True (which gates MACD) when there is NO strong trend.
                # In a strong trend, RSI>75 = momentum persistence, not reversal warning.
                if adx is not None and adx > 30.0:
                    adj -= 20.0  # strong trend: softer overbought penalty
                    # Do NOT set overbought — MACD should still confirm the strong trend signal.
                else:
                    adj -= 50.0  # ranging/weak trend: full overbought penalty
                    overbought = True  # suppress MACD when there's no strong trend to rely on
            elif 55.0 <= rsi <= 70.0:    # Gap 3: tightened sweet spot 45→55 (PineScript RSI>=60)
                adj += 10.0   # confirmed bullish momentum sweet spot
            elif rsi < 35.0:
                adj -= 10.0   # falling momentum — elevated risk for calls

        # Gap 2: MACD histogram (macd - signal_line) confirms/refutes momentum.
        # Histogram > 0 = MACD accelerating above signal → bullish.
        # Histogram < 0 = MACD falling below signal → bearish.
        # Fallback: if no signal line data, use raw macd > 0 check (prior behaviour).
        if macd is not None and not overbought:
            if macd_signal is not None:
                hist = macd - macd_signal  # MACD histogram
                if hist > 0.0:
                    adj += 10.0   # MACD accelerating — bullish confirmation
                else:
                    adj -= 10.0   # MACD decelerating — against call direction
            else:
                # Fallback: no signal line available
                if macd > 0.0:
                    adj += 10.0
                else:
                    adj -= 10.0

        # Gap 1: DI+/DI- polarity — confirms directional bias (PineScript: plusDI > minusDI).
        # When DI+ > DI-: buyers dominate → bullish confirmation for CALLs.
        # When DI- >= DI+: sellers dominate → against call direction.
        # Fix 2026-05-26: Scale bonus/penalty by DI spread MAGNITUDE.
        # Prior binary +10/-20 gave same weight to a 1-pt spread as a 22-pt spread.
        # WMT DI-=38 vs DI+=16 (22-pt bearish spread) should be far stronger than a 2-pt spread.
        if plus_di is not None and minus_di is not None:
            di_spread = abs(plus_di - minus_di)
            # 30% floor prevents zeroing out signal entirely; full effect at ≥15 point spread.
            scale = max(0.3, min(1.0, di_spread / 15.0))
            if plus_di > minus_di:
                adj += 15.0 * scale   # up to +15 for strong bullish DI divergence
            else:
                adj -= 20.0 * scale   # up to -20 for strong bearish DI divergence

        return base + adj, overbought

    @staticmethod
    def _adx_adjustment(adx: float | None) -> float:
        """
        Trend strength multiplier for the directional signal deviation from neutral.

        Returns a MULTIPLIER applied to (score - 50):
          score_final = 50 + (score_before_adx - 50) × multiplier

        High ADX = strong trend = directional signals are reliable → amplify deviation.
        Low ADX = ranging = signals are noisy → dampen toward neutral (50).

          ADX > 30    → 1.4  (strong trend: amplify directional signal 40%)
          ADX 20–30   → 1.2  (moderate trend: amplify 20%)
          ADX 14–20   → 1.0  (weak trend: no adjustment)
          ADX < 14    → 0.7  (ranging / no trend: dampen toward neutral 30%)
          None        → 1.0  (no ADX data: no adjustment)
        """
        if adx is None:
            return 1.0
        if adx > 30.0:
            return 1.4    # strong trend: amplify signals
        elif adx >= 20.0:
            return 1.2    # moderate trend: moderate amplification
        elif adx >= 14.0:
            return 1.0    # weak trend: no adjustment
        else:
            return 0.7    # ranging: dampen toward neutral

    @staticmethod
    def _score_put(
        base: float,
        spot: float,
        sma50: float | None,
        rsi: float | None,
        macd: float | None,
        *,
        sma200: float | None = None,
        adx: float | None = None,
        macd_signal: float | None = None,  # Gap 2: MACD histogram
        plus_di: float | None = None,      # Gap 1: DI+ polarity
        minus_di: float | None = None,     # Gap 1: DI- polarity
    ) -> float:
        """Score alignment for a PUT option (bearish bias needed)."""
        adj = 0.0
        oversold = False

        # SMA200 alignment (inverted for puts — below SMA200 = bearish confirmation).
        # Proximity guard (>1%) prevents noise when hovering right at SMA200.
        if sma200 is not None and sma200 > 0.0 and spot > 0.0:
            proximity = abs(spot - sma200) / sma200
            if proximity > 0.01:  # >1% away from SMA200 to avoid noise flips
                if spot < sma200:
                    adj += 5.0   # below medium-term trend — confirms bearish setup
                else:
                    adj -= 20.0  # above medium-term trend — headwind for puts

        # SMA50 trend: reversed logic for puts
        if sma50 is not None and sma50 > 0.0 and spot > 0.0:
            if spot < sma50:
                adj += 20.0   # bearish short-term — good for puts
            else:
                adj -= 30.0   # bullish short-term — headwind for puts

        # RSI regime (reversed for puts)
        if rsi is not None:
            if rsi < 25.0:
                # Oversold: generally bad timing for put buyers (likely to bounce).
                # NF-1 fix (2026-05-26): Same as CALL fix — only set oversold=True when
                # there is NO strong confirmed downtrend (ADX<=30). In a strong downtrend,
                # RSI<25 = momentum persistence (panic selling). MACD should still confirm.
                if adx is not None and adx > 30.0:
                    adj -= 20.0  # strong downtrend: softer oversold penalty
                    # Do NOT set oversold — MACD should still confirm the strong downtrend.
                else:
                    adj -= 50.0  # ranging/weak trend: full oversold penalty
                    oversold = True  # suppress MACD only when no strong trend confirmation
            elif 30.0 <= rsi <= 55.0:
                adj += 10.0   # bearish momentum sweet spot for puts
            elif rsi > 65.0:
                adj -= 10.0   # bullish momentum — against put direction

        # Gap 2: MACD histogram for puts — histogram < 0 = bearish confirmation.
        if macd is not None and not oversold:
            if macd_signal is not None:
                hist = macd - macd_signal  # MACD histogram
                if hist < 0.0:
                    adj += 10.0   # MACD decelerating — bearish confirmation for puts
                else:
                    adj -= 10.0   # MACD accelerating — against put direction
            else:
                # Fallback: no signal line available
                if macd < 0.0:
                    adj += 10.0
                else:
                    adj -= 10.0

        # Gap 1: DI+/DI- polarity — inverted for puts.
        # When DI- > DI+: sellers dominate → bearish confirmation for PUTs.
        # When DI+ >= DI-: buyers dominate → against put direction.
        # Fix 2026-05-26: Scale bonus/penalty by DI spread magnitude.
        # Prior binary +10/-20 gave same weight to a 1-pt spread as a 22-pt spread.
        # WMT DI-=38 vs DI+=16 (22-pt bearish spread) should score far higher than marginal.
        if minus_di is not None and plus_di is not None:
            di_spread = abs(plus_di - minus_di)
            scale = max(0.3, min(1.0, di_spread / 15.0))
            if minus_di > plus_di:
                adj += 15.0 * scale   # up to +15 for strong bearish DI divergence (good for puts)
            else:
                adj -= 20.0 * scale   # up to -20 for strong bullish DI divergence (bad for puts)

        return base + adj, oversold
