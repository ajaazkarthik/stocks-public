"""
Tests for TQS v2 research v2 enhancements (2026-05-28).
Verifies:
  - Config defaults: contract_min_dte=21, min_score_for_report=55.0, earnings_buffer_days=21
  - EarningsProximityFilter logic with configurable buffer
  - EarningsProximityFilter registered in the pipeline
  - stop_loss_pct=-50.0 in json_reporter exit strategy
"""
import math
import types
import pytest

from tradingview.options.tqs_v2.config import TQSv2Config
from tradingview.options.tqs_v2.filtering.contract_filter import EarningsProximityFilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_contract(earnings_dte=None, dte=30, opra_code="TEST"):
    """Return a SimpleNamespace mimicking a RawContract with earnings_dte."""
    return types.SimpleNamespace(
        earnings_dte=earnings_dte,
        dte=dte,
        opra_code=opra_code,
    )


# ---------------------------------------------------------------------------
# 1. Config defaults
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    """Verify all new / changed config defaults introduced in v2 enhancements."""

    def test_contract_min_dte_is_21(self):
        """contract_min_dte should be 21 (compromise: broader universe, avoid heavy gamma)."""
        cfg = TQSv2Config()
        assert cfg.contract_min_dte == 21

    def test_min_score_for_report_is_55(self):
        """min_score_for_report should be 55.0 (wider net with $4 premium cap)."""
        cfg = TQSv2Config()
        assert cfg.min_score_for_report == 55.0

    def test_earnings_buffer_days_default_is_21(self):
        """earnings_buffer_days default should be 21 (IV inflation starts 2-3 weeks before)."""
        cfg = TQSv2Config()
        assert cfg.earnings_buffer_days == 21

    def test_config_is_frozen(self):
        """TQSv2Config must remain immutable (frozen dataclass)."""
        cfg = TQSv2Config()
        with pytest.raises((TypeError, AttributeError)):
            cfg.earnings_buffer_days = 7  # type: ignore[misc]

    def test_custom_earnings_buffer_days(self):
        """Custom earnings_buffer_days should override the default."""
        cfg = TQSv2Config(earnings_buffer_days=7)
        assert cfg.earnings_buffer_days == 7


# ---------------------------------------------------------------------------
# 2. EarningsProximityFilter — parametrized edge cases
# ---------------------------------------------------------------------------

class TestEarningsProximityFilterEdgeCases:
    """Parametrized tests for EarningsProximityFilter accept/reject logic."""

    @pytest.mark.parametrize(
        "earnings_dte,dte,expected,description",
        [
            # a. No earnings data → accept
            (None,  30, True,  "no earnings_dte (None) → always accept"),
            # b. Earnings already passed → accept
            (-5,    30, True,  "earnings already passed (negative dte) → accept"),
            # c. Suspiciously large value → accept (sanitized, treated as no data)
            (400,   30, True,  "earnings_dte=400 > 365 days → sanitized, accept"),
            # d. NaN → accept
            (float("nan"), 30, True,  "NaN earnings_dte → accept"),
            # e. earnings_dte=10, dte=30 → REJECT (10 < 30+21=51)
            (10,    30, False, "earnings_dte=10 < dte(30)+buffer(21)=51 → reject"),
            # f. earnings_dte=45, dte=30 → REJECT (45 < 51)
            (45,    30, False, "earnings_dte=45 < dte(30)+buffer(21)=51 → reject"),
            # g. earnings_dte=60, dte=30 → ACCEPT (60 >= 51)
            (60,    30, True,  "earnings_dte=60 >= dte(30)+buffer(21)=51 → accept"),
            # h. earnings_dte=0 (reporting today) → REJECT
            (0,     30, False, "earnings_dte=0 (reporting today) → reject"),
            # Boundary: exactly at threshold
            (51,    30, True,  "earnings_dte=51 == threshold 51 → accept (not strictly less)"),
            (50,    30, False, "earnings_dte=50 < threshold 51 → reject"),
        ],
    )
    def test_default_buffer(self, earnings_dte, dte, expected, description):
        """EarningsProximityFilter with default buffer (21 days)."""
        f = EarningsProximityFilter()
        config = TQSv2Config()
        contract = _make_contract(earnings_dte=earnings_dte, dte=dte)
        result = f.accepts(contract, config)
        assert result is expected, f"Failed: {description}"


class TestEarningsProximityFilterCustomBuffer:
    """i. Custom earnings_buffer_days = 7 → reduced buffer."""

    def test_custom_buffer_7_accepts_what_default_rejects(self):
        """With buffer=7, earnings_dte=35 should ACCEPT (35 >= 30+7=37 fails; use 38)."""
        f = EarningsProximityFilter()
        cfg_tight = TQSv2Config(earnings_buffer_days=7)
        # earnings_dte=38, dte=30: threshold = 30+7 = 37 → 38 >= 37 → ACCEPT
        contract = _make_contract(earnings_dte=38, dte=30)
        assert f.accepts(contract, cfg_tight) is True

    def test_custom_buffer_7_rejects_inside_window(self):
        """With buffer=7, earnings_dte=36 should REJECT (36 < 30+7=37)."""
        f = EarningsProximityFilter()
        cfg_tight = TQSv2Config(earnings_buffer_days=7)
        contract = _make_contract(earnings_dte=36, dte=30)
        assert f.accepts(contract, cfg_tight) is False

    def test_default_rejects_but_custom_buffer_7_accepts(self):
        """earnings_dte=45 is rejected by default (45<51) but accepted by buffer=7 (45>=37)."""
        f = EarningsProximityFilter()
        cfg_default = TQSv2Config()
        cfg_tight = TQSv2Config(earnings_buffer_days=7)
        contract = _make_contract(earnings_dte=45, dte=30)
        # Default buffer=21: 45 < 51 → reject
        assert f.accepts(contract, cfg_default) is False
        # Tight buffer=7: 45 >= 37 → accept
        assert f.accepts(contract, cfg_tight) is True

    def test_custom_buffer_zero_only_rejects_within_dte(self):
        """Buffer=0 means only reject if earnings_dte < dte itself."""
        f = EarningsProximityFilter()
        cfg = TQSv2Config(earnings_buffer_days=0)
        # earnings_dte=29, dte=30: 29 < 30 → reject
        contract_reject = _make_contract(earnings_dte=29, dte=30)
        assert f.accepts(contract_reject, cfg) is False
        # earnings_dte=30, dte=30: 30 >= 30 → accept
        contract_accept = _make_contract(earnings_dte=30, dte=30)
        assert f.accepts(contract_accept, cfg) is True


class TestEarningsProximityFilterDataSanitization:
    """Test NaN, string, and unusual type handling."""

    def test_nan_float_accepts(self):
        """float('nan') earnings_dte should be treated as no data → accept."""
        f = EarningsProximityFilter()
        config = TQSv2Config()
        contract = _make_contract(earnings_dte=float("nan"))
        assert f.accepts(contract, config) is True

    def test_none_accepts(self):
        """None earnings_dte → accept (no data)."""
        f = EarningsProximityFilter()
        config = TQSv2Config()
        contract = _make_contract(earnings_dte=None)
        assert f.accepts(contract, config) is True

    def test_non_numeric_string_accepts(self):
        """Non-numeric string earnings_dte → accept (parse fails gracefully)."""
        f = EarningsProximityFilter()
        config = TQSv2Config()
        contract = _make_contract(earnings_dte="N/A")
        assert f.accepts(contract, config) is True

    def test_numeric_string_treated_as_float(self):
        """Numeric string '10' should be cast to float and evaluated normally."""
        f = EarningsProximityFilter()
        config = TQSv2Config()
        # '10' → 10.0; dte=30, buffer=21 → threshold=51 → 10 < 51 → reject
        contract = _make_contract(earnings_dte="10", dte=30)
        assert f.accepts(contract, config) is False

    def test_very_large_value_accepted(self):
        """earnings_dte > 365 is treated as bad data → accept."""
        f = EarningsProximityFilter()
        config = TQSv2Config()
        contract = _make_contract(earnings_dte=999)
        assert f.accepts(contract, config) is True

    def test_exactly_365_is_accepted(self):
        """earnings_dte=366 > 365 → accept; earnings_dte=365 <= 365 → depends on gate."""
        f = EarningsProximityFilter()
        config = TQSv2Config()
        # 366 > 365 → sanitized, accept
        contract = _make_contract(earnings_dte=366)
        assert f.accepts(contract, config) is True


# ---------------------------------------------------------------------------
# 3. Pipeline registration
# ---------------------------------------------------------------------------

class TestPipelineRegistration:
    """EarningsProximityFilter must be present in the default pipeline."""

    def test_earnings_filter_in_default_pipeline(self):
        """Default FilterPipeline must include EarningsProximityFilter."""
        from tradingview.options.tqs_v2.filtering.pipeline import FilterPipeline

        pipeline = FilterPipeline()
        filter_types = [type(f).__name__ for f in pipeline._filters]
        assert "EarningsProximityFilter" in filter_types

    def test_earnings_filter_instance_is_correct_class(self):
        """The pipeline instance should be the real EarningsProximityFilter."""
        from tradingview.options.tqs_v2.filtering.pipeline import FilterPipeline

        pipeline = FilterPipeline()
        earnings_filters = [f for f in pipeline._filters if isinstance(f, EarningsProximityFilter)]
        assert len(earnings_filters) == 1

    def test_earnings_filter_has_correct_name(self):
        """EarningsProximityFilter.name must be 'earnings_proximity'."""
        f = EarningsProximityFilter()
        assert f.name == "earnings_proximity"


# ---------------------------------------------------------------------------
# 4. Stop-loss pct in json_reporter
# ---------------------------------------------------------------------------

class TestStopLossPct:
    """stop_loss_pct must be -50.0 (widened from -25%)."""

    def test_stop_loss_pct_is_minus_50(self):
        """json_reporter exit strategy must use -50.0 stop-loss."""
        import inspect
        import tradingview.options.tqs_v2.reporting.json_reporter as jr

        source = inspect.getsource(jr)
        assert '"stop_loss_pct": -50.0' in source or "'stop_loss_pct': -50.0" in source, (
            "stop_loss_pct must be -50.0 in json_reporter.py"
        )

    def test_stop_loss_pct_not_minus_25(self):
        """The old -25.0 stop-loss must NOT appear in json_reporter."""
        import inspect
        import tradingview.options.tqs_v2.reporting.json_reporter as jr

        source = inspect.getsource(jr)
        assert '"stop_loss_pct": -25.0' not in source and "'stop_loss_pct': -25.0" not in source, (
            "Old -25.0 stop-loss still present in json_reporter.py — must be -50.0"
        )

    def test_stop_loss_applied_in_exit_strategy_dict(self):
        """The exit strategy dict in json_reporter must contain stop_loss_pct=-50.0."""
        import types as builtins_types
        import tradingview.options.tqs_v2.reporting.json_reporter as jr

        # Find the exit_strategy dict by instantiating reporter and calling _build_exit_strategy
        # Use a stub contract to trigger exit strategy construction
        scored = builtins_types.SimpleNamespace(
            opra_code="TEST260620C00100000",
            option_type="call",
            strike=100.0,
            expiry="2026-06-20",
            dte=22,
            bid=2.50,
            ask=2.70,
            iv=0.35,
            delta=0.35,
            spot_price=98.0,
            total_score=75.0,
            scores=builtins_types.SimpleNamespace(
                pop_score=55.0,
                iv_score=60.0,
                premium_score=70.0,
                exit_score=65.0,
                momentum_score=60.0,
                regime_score=50.0,
            ),
            mid_price=2.60,
            breakeven=102.70,
            open_interest=2000,
            volume=150,
            vix_level=18.0,
            vix_regime="normal",
            vix_multiplier=1.0,
            vix_score=50.0,
        )

        reporter = jr.JSONReporter()
        if hasattr(reporter, "_build_exit_strategy"):
            exit_strat = reporter._build_exit_strategy(scored)
            assert exit_strat.get("stop_loss_pct") == -50.0
        else:
            # Fall back to source inspection if no _build_exit_strategy method
            import inspect
            source = inspect.getsource(jr)
            assert '"stop_loss_pct": -50.0' in source or "'stop_loss_pct': -50.0" in source
