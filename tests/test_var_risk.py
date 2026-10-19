"""Tests for tradingview/analysis/var_risk_dashboard.py — VaR and risk metrics."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tradingview.analysis.var_risk_dashboard as risk


# ---------------------------------------------------------------------------
# Test: compute_var_parametric — returns (var_dollar, var_pct) tuple
# ---------------------------------------------------------------------------

class TestVarParametric:
    def test_returns_tuple(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.008, -0.01, 0.012, -0.003]
        var_dollar, var_pct = risk.compute_var_parametric(returns, portfolio_value=100000)
        assert isinstance(var_dollar, float)
        assert isinstance(var_pct, float)

    def test_var_is_positive(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.008, -0.01, 0.012, -0.003]
        var_dollar, var_pct = risk.compute_var_parametric(returns, portfolio_value=100000)
        assert var_dollar >= 0
        assert var_pct >= 0

    def test_99_is_worse_than_95(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.008, -0.01, 0.012, -0.003] * 10
        var_95, _ = risk.compute_var_parametric(returns, portfolio_value=100000, confidence=95)
        var_99, _ = risk.compute_var_parametric(returns, portfolio_value=100000, confidence=99)
        assert var_99 >= var_95

    def test_empty_returns(self):
        var_dollar, var_pct = risk.compute_var_parametric([], portfolio_value=100000)
        assert var_dollar == 0.0
        assert var_pct == 0.0

    def test_single_return(self):
        var_dollar, var_pct = risk.compute_var_parametric([0.01], portfolio_value=100000)
        # Should still produce a result (single element, std=0)
        assert isinstance(var_dollar, float)


# ---------------------------------------------------------------------------
# Test: compute_var_historical — returns (var_dollar, var_pct) tuple
# ---------------------------------------------------------------------------

class TestVarHistorical:
    def test_returns_tuple(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.008, -0.01, 0.012, -0.003]
        var_dollar, var_pct = risk.compute_var_historical(returns, portfolio_value=100000)
        assert isinstance(var_dollar, float)
        assert isinstance(var_pct, float)

    def test_99_is_worse_than_95(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.008, -0.01, 0.012, -0.003] * 10
        var_95, _ = risk.compute_var_historical(returns, portfolio_value=100000, confidence=95)
        var_99, _ = risk.compute_var_historical(returns, portfolio_value=100000, confidence=99)
        assert var_99 >= var_95

    def test_empty_returns(self):
        var_dollar, var_pct = risk.compute_var_historical([], portfolio_value=100000)
        assert var_dollar == 0.0


# ---------------------------------------------------------------------------
# Test: compute_max_drawdown — returns positive float (0=no drawdown, 1=100%)
# ---------------------------------------------------------------------------

class TestMaxDrawdown:
    def test_returns_non_negative(self):
        curve = [100, 95, 98, 90, 92, 88, 95]
        dd = risk.compute_max_drawdown(curve)
        assert dd >= 0

    def test_drawdown_for_crash(self):
        curve = [100, 90, 80, 70, 60, 50]  # 50% drawdown
        dd = risk.compute_max_drawdown(curve)
        assert dd > 0.2  # At least 20% drawdown
        assert dd <= 1.0

    def test_no_drawdown_on_flat(self):
        curve = [100, 100, 100]
        dd = risk.compute_max_drawdown(curve)
        assert dd == 0.0

    def test_empty_curve(self):
        dd = risk.compute_max_drawdown([])
        assert dd == 0.0


# ---------------------------------------------------------------------------
# Test: compute_beta
# ---------------------------------------------------------------------------

class TestBeta:
    def test_returns_float(self):
        port = [0.01, -0.02, 0.015, -0.005, 0.008]
        bench = [0.008, -0.015, 0.01, -0.003, 0.006]
        beta = risk.compute_beta(port, bench)
        assert isinstance(beta, float)

    def test_empty_returns_default(self):
        # Default is 1.0 for insufficient data
        beta = risk.compute_beta([], [])
        assert beta == 1.0

    def test_perfect_correlation(self):
        returns = [0.01, -0.02, 0.015, -0.005]
        beta = risk.compute_beta(returns, returns)
        assert abs(beta - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Test: compute_sharpe
# ---------------------------------------------------------------------------

class TestSharpe:
    def test_returns_float(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.008, -0.01, 0.012, -0.003] * 10
        sharpe = risk.compute_sharpe(returns)
        assert isinstance(sharpe, float)

    def test_empty_returns_zero(self):
        sharpe = risk.compute_sharpe([])
        assert sharpe == 0.0

    def test_constant_returns_zero_std(self):
        sharpe = risk.compute_sharpe([0.001] * 50)
        assert sharpe == 0.0  # Zero variance


# ---------------------------------------------------------------------------
# Test: compute_concentration — returns {top5, top10, region, broker}
# ---------------------------------------------------------------------------

class TestConcentration:
    def test_returns_dict_keys(self):
        positions = [{"market_val": 10000, "broker": "IBKR"}]
        result = risk.compute_concentration(positions)
        assert "top5" in result
        assert "top10" in result
        assert "region" in result
        assert "broker" in result

    def test_single_position_max_top5(self):
        positions = [{"market_val": 10000, "broker": "IBKR"}]
        result = risk.compute_concentration(positions)
        assert result["top5"] == 100.0

    def test_equal_weight(self):
        positions = [{"market_val": 1000, "broker": "IBKR"} for _ in range(10)]
        result = risk.compute_concentration(positions)
        assert abs(result["top5"] - 50.0) < 1.0

    def test_empty_positions(self):
        result = risk.compute_concentration([])
        assert result["top5"] == 0
        assert result["top10"] == 0

    def test_broker_breakdown(self):
        positions = [
            {"market_val": 5000, "broker": "IBKR"},
            {"market_val": 3000, "broker": "Zerodha"},
        ]
        result = risk.compute_concentration(positions)
        assert "broker" in result
        assert "IBKR" in result["broker"]


# ---------------------------------------------------------------------------
# Test: RiskMetrics dataclass
# ---------------------------------------------------------------------------

class TestRiskMetrics:
    def test_default_creation(self):
        m = risk.RiskMetrics()
        assert m.portfolio_value == 0.0
        assert m.var_95_1d == 0.0
        assert m.var_99_1d == 0.0

    def test_custom_creation(self):
        m = risk.RiskMetrics(
            portfolio_value=100000,
            var_95_1d=1500,
            var_99_1d=2500,
            max_drawdown=0.15,
            beta_vs_spy=1.1,
            sharpe_ratio=1.5,
        )
        assert m.portfolio_value == 100000
        assert m.var_95_1d == 1500
        assert m.max_drawdown == 0.15


# ---------------------------------------------------------------------------
# Test: HTML report generation
# ---------------------------------------------------------------------------

class TestRiskHtmlReport:
    def test_generates_file(self, tmp_path):
        metrics = risk.RiskMetrics(
            portfolio_value=100000,
            var_95_1d=1500,
            var_99_1d=2500,
            max_drawdown=0.15,
            beta_vs_spy=1.1,
            sharpe_ratio=1.5,
        )
        out = tmp_path / "risk.html"
        result = risk.generate_risk_html(metrics, out)
        assert result.exists()
        html = result.read_text(encoding="utf-8")
        assert "Risk" in html or "VaR" in html

    def test_html_contains_key_metrics(self, tmp_path):
        metrics = risk.RiskMetrics(
            portfolio_value=100000,
            var_95_1d=1500,
            var_99_1d=2500,
        )
        out = tmp_path / "risk.html"
        result = risk.generate_risk_html(metrics, out)
        html = result.read_text(encoding="utf-8")
        assert "100" in html  # Portfolio value appears somewhere


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestRiskIntegration:
    def test_metrics_computation(self):
        positions = [
            {"market_val": 10000, "broker": "IBKR"},
            {"market_val": 5000, "broker": "Zerodha"},
        ]
        returns = [0.01, -0.02, 0.015, -0.005, 0.008] * 10
        metrics = risk.compute_risk_metrics(positions, daily_returns=returns)
        assert metrics.portfolio_value == 15000
        assert isinstance(metrics.var_95_1d, float)
