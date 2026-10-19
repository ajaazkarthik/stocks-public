"""
TQS v2 Integration Tests
==========================
End-to-end tests for filtering, ranking, and reporting.
All tests are deterministic — no network calls.
Uses synthetic RawContracts.
"""

from __future__ import annotations

import json
import pytest
import tempfile
from datetime import date, timedelta
from pathlib import Path

from ..options_data.models import RawContract
from ..filtering.pipeline import FilterPipeline
from ..ranking.engine import RankingEngine
from ..ranking.models import RankedContract
from ..scoring.engine import ScoringEngine
from ..scoring.models import ScoredContract, ScoreBreakdown
from ..config import TQSv2Config, DEFAULT_CONFIG


def _make_contract(
    symbol="NVDA",
    strike=900.0,
    spot=880.0,
    bid=3.50,
    ask: float | None = None,   # defaults to bid * 1.03 (~3% spread) → ask ≈ $3.61 ≤ $4 budget
    iv=0.40,
    delta=0.35,
    dte=40,
    oi=2500,
    volume=400,
    option_type="call",
) -> RawContract:
    """Build a valid test RawContract using the real constructor."""
    if ask is None:
        ask = round(bid * 1.03, 2)  # ~3% spread
    exp_date = date.today() + timedelta(days=dte + 1)
    expiration = int(exp_date.strftime("%Y%m%d"))

    return RawContract(
        opra_code=f"OPRA:{symbol}{expiration}C{strike}",
        underlying_symbol=f"NASDAQ:{symbol}",
        root=symbol,
        exchange="NASDAQ",
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        expiry_date=exp_date,
        bid=bid,
        ask=ask,
        theo_price=(bid + ask) / 2 * 1.05,
        iv=iv,
        delta=delta,
        gamma=0.005,
        theta=-0.08,
        vega=0.2,
        rho=0.03,
        bid_iv=iv - 0.02,
        ask_iv=iv + 0.02,
        volume=volume,
        open_interest=oi,
        spot_price=spot,
    )


class TestFilterPipeline:
    def setup_method(self):
        self.pipeline = FilterPipeline()
        self.config = DEFAULT_CONFIG

    def test_good_contracts_pass(self):
        contracts = [_make_contract(bid=2.0, oi=2000, delta=0.30, dte=40)]
        filtered, stats = self.pipeline.apply(contracts, self.config)
        assert len(filtered) == 1
        assert stats.pass_rate == 100.0

    def test_low_bid_rejected(self):
        c = _make_contract(bid=0.05, ask=0.10)
        filtered, stats = self.pipeline.apply([c], self.config)
        assert len(filtered) == 0
        assert stats.rejections.get("bid_floor", 0) >= 1

    def test_low_oi_rejected_after_enrichment(self):
        c = _make_contract(oi=5)  # below min_open_interest=1500
        filtered, _ = self.pipeline.apply([c], self.config)
        assert len(filtered) == 0

    def test_out_of_delta_range_rejected(self):
        c_low = _make_contract(delta=0.05)   # too far OTM
        c_high = _make_contract(delta=0.75)  # too deep ITM
        filtered, stats = self.pipeline.apply([c_low, c_high], self.config)
        assert len(filtered) == 0
        assert stats.rejections.get("delta_range", 0) == 2

    def test_filter_result_stats(self):
        contracts = [
            _make_contract(bid=2.0, oi=2000, delta=0.30, dte=40),
            _make_contract(bid=0.01, oi=5, delta=0.05, dte=2),  # will be rejected
        ]
        _, stats = self.pipeline.apply(contracts, self.config)
        assert stats.total_in == 2
        assert stats.total_out <= 2


class TestRankingEngine:
    def setup_method(self):
        self.engine = RankingEngine()
        self.scoring = ScoringEngine()
        self.config = DEFAULT_CONFIG

    def _make_scored(self, symbol, total_score) -> ScoredContract:
        c = _make_contract(symbol=symbol, oi=2000)
        breakdown = ScoreBreakdown(
            liquidity=total_score,
            iv_analysis=total_score,
            probability=total_score,
            risk_reward=total_score,
        )
        return ScoredContract(contract=c, scores=breakdown)

    def test_top_n_per_stock_enforced(self):
        # 5 contracts for same symbol
        scored = [self._make_scored("NVDA", 80 - i) for i in range(5)]
        ranked = self.engine.rank(scored, self.config)
        nvda_ranks = [r for r in ranked if r.symbol == "NVDA"]
        # Should not exceed top_n_per_stock (default 3)
        assert len(nvda_ranks) <= self.config.top_n_per_stock

    def test_top_n_global_enforced(self):
        # Create many different symbols each with 1 contract
        scored = [self._make_scored(f"SYM{i}", 80 - i) for i in range(30)]
        ranked = self.engine.rank(scored, self.config)
        assert len(ranked) <= self.config.top_n_global

    def test_rank_1_is_highest_score(self):
        scored = [self._make_scored("NVDA", 80 - i) for i in range(3)]
        ranked = self.engine.rank(scored, self.config)
        if ranked:
            scores = [r.total_score for r in ranked]
            assert scores == sorted(scores, reverse=True)

    def test_empty_input(self):
        assert self.engine.rank([], self.config) == []


class TestJSONReporter:
    def test_json_companion_written(self, tmp_path):
        """Golden Rule #20: Every HTML report must have a JSON companion."""
        from ..reporting.json_reporter import JSONReporter, _contract_to_dict
        from ..ranking.models import RankedContract

        # Build a minimal RankedContract
        c = _make_contract()
        breakdown = ScoreBreakdown(
            liquidity=70.0, iv_analysis=75.0, probability=68.0, risk_reward=72.0
        )
        sc = ScoredContract(contract=c, scores=breakdown)
        rc = RankedContract(scored=sc, rank_global=1, rank_per_stock=1)

        config = TQSv2Config(
            reports_dir=tmp_path,
            database_dir=tmp_path / "db",
            logs_dir=tmp_path / "logs",
        )
        reporter = JSONReporter()
        out_path = reporter.generate([rc], "calls", "test-run-123", config)

        json_file = Path(out_path)
        assert json_file.exists()
        data = json.loads(json_file.read_text(encoding="utf-8"))
        assert data["scan_type"] == "calls"
        assert data["total_results"] == 1
        assert len(data["results"]) == 1
        r = data["results"][0]
        assert r["symbol"] == "NVDA"
        assert r["option_type"] == "call"
        assert "total_score" in r
        assert "grade" in r


class TestHTMLReporter:
    def test_html_written(self, tmp_path):
        from ..reporting.html_reporter import HTMLReporter

        c = _make_contract()
        breakdown = ScoreBreakdown(
            liquidity=70.0, iv_analysis=75.0, probability=68.0, risk_reward=72.0
        )
        sc = ScoredContract(contract=c, scores=breakdown)
        rc = RankedContract(scored=sc, rank_global=1, rank_per_stock=1)

        config = TQSv2Config(
            reports_dir=tmp_path,
            database_dir=tmp_path / "db",
            logs_dir=tmp_path / "logs",
        )
        reporter = HTMLReporter()
        out_path = reporter.generate([rc], "calls", "test-run-123", config)

        html_file = Path(out_path)
        assert html_file.exists()
        content = html_file.read_text(encoding="utf-8")
        assert "TQS v2" in content
        assert "NVDA" in content
        assert "<!DOCTYPE html>" in content


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Preset registry and config field tests
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyPresets:
    """Verify all 12 presets are registered and have correct DTE / config values."""

    def setup_method(self):
        from ..presets import STRATEGY_PRESETS
        self.presets = STRATEGY_PRESETS

    def test_twelve_presets_registered(self):
        assert len(self.presets) == 12

    def test_all_call_presets_present(self):
        expected = {
            "budget_calls", "scalp_calls", "momentum_calls",
            "standard_calls", "swing_calls", "high_conviction_calls",
        }
        assert expected.issubset(self.presets.keys())

    def test_all_put_presets_present(self):
        expected = {
            "budget_puts", "scalp_puts", "momentum_puts",
            "standard_puts", "swing_puts", "high_conviction_puts",
        }
        assert expected.issubset(self.presets.keys())

    def test_budget_calls_dte_reduced(self):
        """Req 1: budget max $4 requires shorter DTE (7–21)."""
        cfg = self.presets["budget_calls"]
        assert cfg.contract_min_dte == 7,  "budget_calls min_dte must be 7 (not 21)"
        assert cfg.contract_max_dte == 21, "budget_calls max_dte must be 21 (not 45)"
        assert cfg.contract_max_premium == 4.00
        assert cfg.stock_max_price == 150.0

    def test_budget_puts_dte_reduced(self):
        """Req 1: budget_puts must mirror budget_calls DTE."""
        cfg = self.presets["budget_puts"]
        assert cfg.contract_min_dte == 7
        assert cfg.contract_max_dte == 21
        assert cfg.contract_max_premium == 4.00

    def test_scalp_calls_ultra_short_dte(self):
        """scalp_calls should be 3–7 DTE weeklies."""
        cfg = self.presets["scalp_calls"]
        assert cfg.contract_min_dte == 3
        assert cfg.contract_max_dte == 7
        assert cfg.contract_max_premium <= 2.50

    def test_scalp_puts_ultra_short_dte(self):
        cfg = self.presets["scalp_puts"]
        assert cfg.contract_min_dte == 3
        assert cfg.contract_max_dte == 7

    def test_momentum_calls_rsi_filter(self):
        """Req 2: momentum_calls must have RSI bullish zone filter."""
        cfg = self.presets["momentum_calls"]
        assert cfg.stock_rsi_min == 50.0
        assert cfg.stock_rsi_max == 68.0
        assert cfg.stock_min_relative_volume >= 1.2

    def test_momentum_puts_rsi_filter(self):
        """Req 2: momentum_puts must have RSI bearish zone filter."""
        cfg = self.presets["momentum_puts"]
        assert cfg.stock_rsi_min == 30.0
        assert cfg.stock_rsi_max == 50.0
        assert cfg.stock_min_relative_volume >= 1.2

    def test_high_conviction_calls_strict_oi(self):
        cfg = self.presets["high_conviction_calls"]
        assert cfg.min_open_interest >= 2000
        assert cfg.contract_max_spread_pct <= 10.0

    def test_high_conviction_puts_strict_oi(self):
        cfg = self.presets["high_conviction_puts"]
        assert cfg.min_open_interest >= 2000
        assert cfg.contract_max_spread_pct <= 10.0

    def test_swing_calls_longer_dte(self):
        cfg = self.presets["swing_calls"]
        assert cfg.contract_min_dte >= 45
        assert cfg.contract_max_dte >= 90

    def test_swing_puts_longer_dte(self):
        cfg = self.presets["swing_puts"]
        assert cfg.contract_min_dte >= 45
        assert cfg.contract_max_dte >= 90


class TestNewConfigFields:
    """Verify new TQSv2Config stock universe momentum filter fields."""

    def test_default_sort_is_market_cap(self):
        """Default sort is market_cap_basic (stable, avoids low-float traps)."""
        cfg = TQSv2Config()
        assert cfg.stock_sort_by == "market_cap_basic"

    def test_default_relative_volume_filter_disabled(self):
        """Default rel_vol filter is 0.0 (disabled) — caller can override via CLI."""
        cfg = TQSv2Config()
        assert cfg.stock_min_relative_volume == 0.0

    def test_default_rsi_filters_disabled(self):
        """Default RSI filters should be disabled (0/100) for standard scan."""
        cfg = TQSv2Config()
        assert cfg.stock_rsi_min == 0.0
        assert cfg.stock_rsi_max == 100.0

    def test_default_max_price_disabled(self):
        """Default stock_max_price = 0 means no cap (standard scan)."""
        cfg = TQSv2Config()
        assert cfg.stock_max_price == 0.0

    def test_custom_rsi_filter_accepted(self):
        """Config override for RSI range should be accepted."""
        cfg = TQSv2Config(stock_rsi_min=50.0, stock_rsi_max=68.0)
        assert cfg.stock_rsi_min == 50.0
        assert cfg.stock_rsi_max == 68.0

    def test_custom_max_price_accepted(self):
        cfg = TQSv2Config(stock_max_price=150.0)
        assert cfg.stock_max_price == 150.0

    def test_custom_sort_by_accepted(self):
        cfg = TQSv2Config(stock_sort_by="market_cap_basic")
        assert cfg.stock_sort_by == "market_cap_basic"


class TestTVScannerPayload:
    """Verify _build_scan_payload generates correct dynamic filter conditions."""

    def setup_method(self):
        from ..market_data.tv_scanner import TVStockScanner
        self.build_payload = TVStockScanner._build_scan_payload

    def test_base_filters_always_present(self):
        payload = self.build_payload(TQSv2Config())
        left_fields = {f["left"] for f in payload["filter"]}
        assert "type" in left_fields
        assert "market_cap_basic" in left_fields
        assert "close" in left_fields
        assert "volume" in left_fields

    def test_relative_volume_filter_added_when_positive(self):
        cfg = TQSv2Config(stock_min_relative_volume=1.2)
        payload = self.build_payload(cfg)
        left_fields = {f["left"] for f in payload["filter"]}
        assert "relative_volume_10d_calc" in left_fields

    def test_relative_volume_filter_skipped_when_zero(self):
        cfg = TQSv2Config(stock_min_relative_volume=0.0)
        payload = self.build_payload(cfg)
        left_fields = {f["left"] for f in payload["filter"]}
        assert "relative_volume_10d_calc" not in left_fields

    def test_rsi_filters_added_when_set(self):
        cfg = TQSv2Config(stock_rsi_min=50.0, stock_rsi_max=68.0)
        payload = self.build_payload(cfg)
        rsi_filters = [f for f in payload["filter"] if f["left"] == "RSI"]
        assert len(rsi_filters) == 2
        ops = {f["operation"] for f in rsi_filters}
        assert "greater" in ops
        assert "less" in ops

    def test_rsi_filters_not_added_when_disabled(self):
        cfg = TQSv2Config(stock_rsi_min=0.0, stock_rsi_max=100.0)
        payload = self.build_payload(cfg)
        rsi_filters = [f for f in payload["filter"] if f["left"] == "RSI"]
        assert len(rsi_filters) == 0

    def test_max_price_filter_added_when_set(self):
        cfg = TQSv2Config(stock_max_price=150.0)
        payload = self.build_payload(cfg)
        close_filters = [f for f in payload["filter"] if f["left"] == "close"]
        # Should have both "greater than min_price" AND "less than max_price"
        assert len(close_filters) == 2
        ops = {f["operation"] for f in close_filters}
        assert "greater" in ops
        assert "less" in ops

    def test_max_price_filter_not_added_when_zero(self):
        cfg = TQSv2Config(stock_max_price=0.0)
        payload = self.build_payload(cfg)
        close_filters = [f for f in payload["filter"] if f["left"] == "close"]
        # Only the "greater" filter (min_price), no "less" filter
        assert len(close_filters) == 1
        assert close_filters[0]["operation"] == "greater"

    def test_sort_uses_config_sort_by(self):
        cfg = TQSv2Config(stock_sort_by="relative_volume_10d_calc")
        payload = self.build_payload(cfg)
        assert payload["sort"]["sortBy"] == "relative_volume_10d_calc"

    def test_sort_by_market_cap_when_overridden(self):
        cfg = TQSv2Config(stock_sort_by="market_cap_basic")
        payload = self.build_payload(cfg)
        assert payload["sort"]["sortBy"] == "market_cap_basic"
