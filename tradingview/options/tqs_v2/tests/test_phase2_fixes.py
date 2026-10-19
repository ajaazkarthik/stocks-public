"""
Tests for Phase 2 TQS v2 fixes (2026-05-27).
Verifies C-4, M-2, M-3 fixes.
"""
import pytest
from dataclasses import replace
from unittest.mock import MagicMock


class TestFix04_EarningsFilter:
    """C-4: Earnings gate should be in the filter pipeline, not post-scoring."""

    def test_earnings_filter_rejects_near_earnings(self):
        """Contract with earnings within DTE + 14-day buffer should be rejected."""
        from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        filt = EarningsProximityFilter()
        config = TQSv2Config()

        contract = MagicMock()
        contract.dte = 30
        contract.earnings_dte = 25  # earnings within 30+14=44 day window
        assert not filt.accepts(contract, config), "Should reject: earnings_dte=25 < dte+14=44"

    def test_earnings_filter_accepts_far_earnings(self):
        """Contract with earnings well beyond DTE + buffer should be accepted."""
        from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        filt = EarningsProximityFilter()
        config = TQSv2Config()

        contract = MagicMock()
        contract.dte = 30
        contract.earnings_dte = 60  # earnings way after DTE+14=44
        assert filt.accepts(contract, config), "Should accept: earnings_dte=60 > dte+14=44"

    def test_earnings_filter_accepts_no_data(self):
        """Contract with no earnings data should be accepted."""
        from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        filt = EarningsProximityFilter()
        config = TQSv2Config()

        contract = MagicMock(spec=[])  # no attributes
        assert filt.accepts(contract, config)

    def test_earnings_filter_rejects_imminent(self):
        """Earnings at day 5 with DTE=45 should be rejected (5 < 45+14=59)."""
        from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        filt = EarningsProximityFilter()
        config = TQSv2Config()

        contract = MagicMock()
        contract.dte = 45
        contract.earnings_dte = 5
        assert not filt.accepts(contract, config)

    def test_earnings_filter_negative_earnings_dte(self):
        """Negative earnings_dte (already passed) should be accepted."""
        from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        filt = EarningsProximityFilter()
        config = TQSv2Config()

        contract = MagicMock()
        contract.dte = 30
        contract.earnings_dte = -5  # earnings already passed
        assert filt.accepts(contract, config), "Negative earnings_dte means already passed"

    def test_earnings_filter_large_value_treated_as_none(self):
        """Suspiciously large earnings_dte (epoch timestamp) should be treated as no data."""
        from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter
        from tradingview.options.tqs_v2.config import TQSv2Config

        filt = EarningsProximityFilter()
        config = TQSv2Config()

        contract = MagicMock()
        contract.dte = 30
        contract.earnings_dte = 45000  # epoch timestamp
        assert filt.accepts(contract, config), "Large earnings_dte should be treated as no data"

    def test_earnings_filter_in_pipeline(self):
        """EarningsProximityFilter should be registered in the pipeline."""
        from tradingview.options.tqs_v2.filtering.pipeline import FilterPipeline
        from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter

        pipeline = FilterPipeline()
        filter_types = [type(f) for f in pipeline._filters]
        assert EarningsProximityFilter in filter_types, "EarningsProximityFilter must be in pipeline"


class TestFix08_GammaScoring:
    """M-2: Gamma should be scored in premium_efficiency."""

    def test_gamma_score_positive(self):
        """Contract with gamma should get non-neutral gamma_score."""
        from tradingview.options.tqs_v2.scoring.metrics.premium_efficiency import PremiumEfficiencyScorer

        scorer = PremiumEfficiencyScorer()

        contract = MagicMock()
        contract.bid = 3.30
        contract.ask = 3.50
        contract.spot_price = 300.0
        contract.strike = 310.0
        contract.option_type = "call"
        contract.iv = 0.22
        contract.dte = 30
        contract.delta = 0.4
        contract.theta = -0.10
        contract.gamma = 0.008  # typical ATM gamma

        result = scorer.score_detailed(contract)
        assert "gamma_score" in result, "gamma_score should be in output"
        assert result["gamma_score"] > 0, "Non-zero gamma should produce positive score"

    def test_higher_gamma_higher_score(self):
        """Higher gamma should produce higher gamma_score."""
        from tradingview.options.tqs_v2.scoring.metrics.premium_efficiency import PremiumEfficiencyScorer

        scorer = PremiumEfficiencyScorer()

        def make_contract(gamma_val):
            c = MagicMock()
            c.bid = 3.30; c.ask = 3.50; c.spot_price = 50.0; c.strike = 55.0
            c.option_type = "call"; c.iv = 0.22; c.dte = 30; c.delta = 0.4
            c.theta = -0.10; c.gamma = gamma_val
            return c

        r_low = scorer.score_detailed(make_contract(0.001))
        r_high = scorer.score_detailed(make_contract(0.015))
        assert r_high["gamma_score"] > r_low["gamma_score"]

    def test_gamma_zero_gets_neutral(self):
        """Gamma=0 should get neutral score (50)."""
        from tradingview.options.tqs_v2.scoring.metrics.premium_efficiency import PremiumEfficiencyScorer

        scorer = PremiumEfficiencyScorer()

        contract = MagicMock()
        contract.bid = 3.30; contract.ask = 3.50; contract.spot_price = 300.0
        contract.strike = 310.0; contract.option_type = "call"; contract.iv = 0.22
        contract.dte = 30; contract.delta = 0.4; contract.theta = -0.10
        contract.gamma = 0.0

        result = scorer.score_detailed(contract)
        assert result["gamma_score"] == 50.0, "Zero gamma should get neutral score"

    def test_gamma_score_capped_at_100(self):
        """Very high gamma should be capped at 100."""
        from tradingview.options.tqs_v2.scoring.metrics.premium_efficiency import PremiumEfficiencyScorer

        scorer = PremiumEfficiencyScorer()

        contract = MagicMock()
        contract.bid = 3.30; contract.ask = 3.50; contract.spot_price = 300.0
        contract.strike = 310.0; contract.option_type = "call"; contract.iv = 0.22
        contract.dte = 30; contract.delta = 0.4; contract.theta = -0.10
        contract.gamma = 0.1  # extreme gamma

        result = scorer.score_detailed(contract)
        assert result["gamma_score"] == 100.0, "Very high gamma should cap at 100"


class TestFix09_SectorCap:
    """M-3: Sector diversification cap should prevent concentration."""

    def test_sector_cap_limits_contracts(self):
        """No more than max_per_sector contracts from same sector."""
        from tradingview.options.tqs_v2.ranking.engine import RankingEngine
        from tradingview.options.tqs_v2.config import TQSv2Config

        engine = RankingEngine()
        config = replace(TQSv2Config(), top_n_per_stock=5, top_n_global=20, max_per_sector=2)

        # Create 5 scored contracts, all from "Technology" sector, different symbols
        scored = []
        for i in range(5):
            sc = MagicMock()
            sc.total_score = 90.0 - i
            sc.symbol = f"TECH{i}"
            sc.sector = "Technology"
            sc.expiry = "2026-07-17"
            sc.opra_code = f"TECH{i}260717C100"
            scored.append(sc)

        # Add 3 from different sectors
        for i, sector in enumerate(["Healthcare", "Finance", "Energy"]):
            sc = MagicMock()
            sc.total_score = 80.0 - i
            sc.symbol = f"OTHER{i}"
            sc.sector = sector
            sc.expiry = "2026-07-17"
            sc.opra_code = f"OTHER{i}260717C100"
            scored.append(sc)

        ranked = engine.rank(scored, config)

        # Count tech sector in results
        tech_count = sum(1 for r in ranked if getattr(r.scored, "sector", None) == "Technology")
        max_sector = getattr(config, "max_per_sector", 3)
        assert tech_count <= max_sector, f"Tech sector has {tech_count} contracts, max should be {max_sector}"

    def test_sector_cap_preserves_diverse_sectors(self):
        """Contracts from diverse sectors should all survive."""
        from tradingview.options.tqs_v2.ranking.engine import RankingEngine
        from tradingview.options.tqs_v2.config import TQSv2Config

        engine = RankingEngine()
        config = replace(TQSv2Config(), top_n_per_stock=5, top_n_global=20, max_per_sector=3, top_n_per_expiry=10)

        # 6 contracts, each from a different sector
        sectors = ["Technology", "Healthcare", "Finance", "Energy", "Consumer", "Industrial"]
        scored = []
        for i, sector in enumerate(sectors):
            sc = MagicMock()
            sc.total_score = 90.0 - i
            sc.symbol = f"SYM{i}"
            sc.sector = sector
            sc.expiry = "2026-07-17"
            sc.opra_code = f"SYM{i}260717C100"
            scored.append(sc)

        ranked = engine.rank(scored, config)
        assert len(ranked) == 6, f"All 6 diverse-sector contracts should survive, got {len(ranked)}"
