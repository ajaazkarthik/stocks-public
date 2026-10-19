"""
TQS v2 JSON Reporter
=====================
Writes scan results to a JSON file alongside each HTML report.

Golden Rule #20: Every HTML report MUST have a companion JSON file.

Output: reports/tqs_v2_calls.json or reports/tqs_v2_puts.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import TQSv2Config
from ..interfaces.contracts import IReporter
from ..ranking.models import RankedContract
from ..scoring.metrics.stock_trend import StockTrendScorer
from .archiver import archive_if_exists

_stock_trend_scorer = StockTrendScorer()  # singleton — stateless

_log = logging.getLogger("tqs_v2.reporting.json_reporter")


def _contract_to_dict(rc: RankedContract, scan_type: str, run_id: str) -> dict:
    """Serialize a RankedContract to a JSON-friendly dict — all 7 score dimensions."""
    c = rc.contract
    s = rc.scored.scores
    return {
        "run_id": run_id,
        "scan_type": scan_type,
        "rank_global": rc.rank_global,
        "rank_per_stock": rc.rank_per_stock,

        # ── Contract identifiers ────────────────────────────────────────────
        "opra_code": c.opra_code,
        "symbol": c.root,
        "exchange": c.exchange,
        "option_type": c.option_type,
        "expiration": c.expiration,
        "strike": c.strike,

        # ── Terms ───────────────────────────────────────────────────────────
        "dte": c.dte,
        "earnings_dte": getattr(c, "earnings_dte", None),  # CRITICAL-2: days to next earnings (None if unknown)
        "moneyness_pct": round(c.moneyness_pct, 2),

        # ── Pricing ─────────────────────────────────────────────────────────
        "bid": c.bid,
        "ask": c.ask,
        "mid": round(c.mid, 2),
        "theo_price": c.theo_price,
        "breakeven": round(c.breakeven, 2),
        "breakeven_pct": round((c.breakeven - c.spot_price) / c.spot_price * 100, 2) if c.spot_price else None,
        "spot_price": c.spot_price,

        # ── Greeks ──────────────────────────────────────────────────────────
        "iv":     round(c.iv * 100, 2),       # percentage, e.g. 38.9
        "delta":  round(c.delta, 4),
        "gamma":  round(c.gamma, 4),
        "theta":  round(c.theta, 4),
        "vega":   round(c.vega, 4),
        "bid_iv": round(c.bid_iv * 100, 2),   # percentage
        "ask_iv": round(c.ask_iv * 100, 2),   # percentage

        # ── Liquidity ───────────────────────────────────────────────────────
            # volume: TV scan2 returns the value when market is open.
            # When market is closed, yfinance OI enrichment fills c.volume
            # from the option_chain() response (same API call as OI).
            "volume": c.volume,
        "open_interest": c.open_interest,
        "spread_pct": round(c.spread_pct, 2),

        # ── IV term structure (optional enrichment) ─────────────────────────
        "iv_term_slope": round(getattr(c, "iv_term_slope", 0.0), 4),
        "iv_1w": getattr(c, "iv_1w", None),
        "iv_1m": getattr(c, "iv_1m", None),
        "iv_3m": getattr(c, "iv_3m", None),
        # TASK 5ii: multi-slope
        "slope_3m_1m": getattr(c, "slope_3m_1m", None),
        "slope_6m_3m": getattr(c, "slope_6m_3m", None),
        "iv_contango_score": getattr(c, "iv_contango_score", None),
        # TASK 5vi: IV smile skew
        "iv_skew_25d": getattr(c, "iv_skew_25d", None),
        "iv_skew_score": getattr(c, "iv_skew_score", None),
        # TASK 5iii: Stock technical context
        "stock_rsi": getattr(c, "stock_rsi", None),
        "stock_sma200": getattr(c, "stock_sma200", None),
        "stock_sma50": getattr(c, "stock_sma50", None),
        "stock_macd": getattr(c, "stock_macd", None),
        "stock_macd_signal": getattr(c, "stock_macd_signal", None),  # Gap 2: MACD histogram
        "stock_atr": getattr(c, "stock_atr", None),
        "stock_adx": getattr(c, "stock_adx", None),
        "stock_plus_di": getattr(c, "stock_plus_di", None),          # Gap 1: DI+ polarity
        "stock_minus_di": getattr(c, "stock_minus_di", None),         # Gap 1: DI- polarity
        "stock_change_pct": getattr(c, "stock_change_pct", None),     # Gap 5: momentum guard

        # ═══════════════════════════════════════════════════════════════════
        # 8-Dimension score breakdown (weights: 18/16/15/15/11/11/8/6 %)
        # D8=stock_trend(18%), D3=risk_reward(16%), D1=probability(15%), D2=liquidity(15%),
        # D4=win_rate(11%), D5=iv_analysis(11%), D7=premium_eff(8%), D6=volatility_cond(6%)
        # Updated 2026-05-27 to reflect Gap 4 rebalance (was 18/18/16/12/11/11/8/6)
        # ═══════════════════════════════════════════════════════════════════
        "total_score": round(rc.total_score, 2),
        "grade": rc.grade,

        # D1: Probability (15%) — delta-based PoP, DTE zone, moneyness
        "probability_score": round(s.probability, 2),
        "delta_score":       round(s.delta_score, 2),
        "dte_score":         round(s.dte_score, 2),
        "moneyness_score":   round(s.moneyness_score, 2),

        # D2: Liquidity (15%) — OI, spread, volume
        "liquidity_score": round(s.liquidity, 2),
        "oi_score":        round(s.oi_score, 2),
        "spread_score":    round(s.spread_score, 2),
        "volume_score":    round(s.volume_score, 2),

        # D3: Risk/Reward (16%) — breakeven %, theo vs market
        "risk_reward_score": round(s.risk_reward, 2),
        "breakeven_score":   round(s.breakeven_score, 2),
        "theo_score":        round(s.theo_score, 2),

        # D4: Win Rate (11%) — PoP proxy + IV rank proxy + DTE factor
        "win_rate_score": round(s.win_rate, 2),
        "pop_score":      round(s.pop_score, 2),
        "iv_pct_adj":     round(s.iv_pct_adj, 2),
        "iv_rank_proxy":  round(max(0.0, min(100.0, 100.0 - (getattr(c, "iv_contango_score", 50.0) or 50.0))), 1),

        # D5: IV Analysis (11%) — IV level, bid/ask IV spread, term slope
        "iv_analysis_score": round(s.iv_analysis, 2),
        "iv_level_score":    round(s.iv_level_score, 2),
        "iv_spread_score":   round(s.iv_spread_score, 2),
        "term_slope_score":  round(s.term_slope_score, 2),

        # D6: Volatility Conditions (6%) — market-wide PCR + skew
        "volatility_conditions_score": round(s.volatility_conditions, 2),
        "pcr_score":                   round(s.pcr_score, 2),
        "skew_score":                  round(s.skew_score, 2),

        # D7: Premium Efficiency (8%) — BE/1σ, |δ|×IV, leverage, time value
        "premium_efficiency_score": round(s.premium_efficiency, 2),
        "tv_score":                 round(s.tv_score, 2),
        "spread_eff_score":         round(s.spread_eff_score, 2),
        "be_score":                 round(s.be_score, 2),
        "be_1sigma_score":          round(s.be_1sigma_score, 2),
        "delta_iv_score":           round(s.delta_iv_score, 2),
        "leverage_score":           round(s.leverage_score, 2),

        # D8: Stock Trend (18%) — SMA50 alignment, RSI regime, MACD histogram, DI polarity, ADX strength, momentum guard
        "stock_trend_score": round(s.stock_trend, 2),
        "stock_adx_score":   round(
            10.0 if (getattr(c, "stock_adx", None) is not None and (getattr(c, "stock_adx", None) or 0) > 30.0)
            else (5.0 if (getattr(c, "stock_adx", None) is not None and (getattr(c, "stock_adx", None) or 0) >= 20.0)
            else (-10.0 if (getattr(c, "stock_adx", None) is not None and (getattr(c, "stock_adx", None) or 0) < 14.0)
            else 0.0)), 1
        ),
        # ── Score diagnostics — Gap 6: adx_bucket, DI polarity, MACD signal, momentum guard ──
        "score_detail": _stock_trend_scorer.score_detailed(c),

        # ═══════════════════════════════════════════════════════════════════
        # Exit Strategy (CRITICAL-3 fix 2026-05-26 — missing from prior output)
        # These are standard directional long-option exit rules.
        # They are NOT personalized financial advice — they are system defaults
        # derived from professional options buying discipline (50/25 rule).
        # ═══════════════════════════════════════════════════════════════════
        "exit_strategy": {
            "take_profit_pct": 50.0,      # close position at +50% gain (preserve edge)
            "stop_loss_pct": -50.0,        # cut at -50% loss (widened from -25%: long options need room; research validated 2026-05-28)
            "exit_before_dte": 21,         # exit no later than 21 DTE (avoid gamma decay acceleration)
            "note": (
                f"Target +50% gain or -50% stop-loss. "
                f"Exit by {(getattr(c, 'dte', 45) or 45) - 21} DTE remaining "
                f"(current DTE={getattr(c, 'dte', 45) or 45}). "
                f"Do not hold through earnings if earnings_dte < contract_dte."
            ),
        },
    }


class JSONReporter(IReporter):
    """Writes scan results as JSON."""

    def generate(
        self,
        ranked: list[RankedContract],
        scan_type: str,
        run_id: str,
        config: TQSv2Config,
        *,
        pipeline_stats: dict | None = None,
    ) -> str:
        """
        Write JSON report. Returns absolute output path string.

        scan_type: "calls" or "puts"
        """
        filename = (
            config.call_json_report if scan_type == "calls" else config.put_json_report
        )
        out_path = config.reports_dir / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Archive previous report before overwriting (never lose a report)
        archive_if_exists(out_path)

        payload = {
            "run_id": run_id,
            "scan_type": scan_type,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_results": len(ranked),
            "pipeline_stats": pipeline_stats or {},  # HIGH-5 fix: filter throughput now observable
            "results": [_contract_to_dict(rc, scan_type, run_id) for rc in ranked],
        }

        out_path.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        _log.info("JSON report written: %s (%d results)", out_path, len(ranked))
        return str(out_path)
