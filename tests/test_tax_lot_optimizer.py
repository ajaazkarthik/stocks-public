"""Tests for cgt/tax_lot_optimizer.py — Tax lot selection for CGT minimisation."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cgt.tax_lot_optimizer as tlo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_lots():
    """Create sample tax lots for AAPL."""
    today = date.today()
    return [
        tlo.TaxLot(symbol="AAPL", buy_date=today - timedelta(days=400), shares=100,
                    cost_per_share=150.0, broker="IBKR"),  # long-term, gain
        tlo.TaxLot(symbol="AAPL", buy_date=today - timedelta(days=100), shares=50,
                    cost_per_share=180.0, broker="IBKR"),  # short-term, loss at price 170
        tlo.TaxLot(symbol="AAPL", buy_date=today - timedelta(days=200), shares=75,
                    cost_per_share=160.0, broker="IBKR"),  # short-term, gain
    ]


# ---------------------------------------------------------------------------
# Test: TaxLot
# ---------------------------------------------------------------------------

class TestTaxLot:
    def test_total_cost(self):
        lot = tlo.TaxLot(symbol="AAPL", buy_date=date.today(), shares=100, cost_per_share=150.0)
        assert lot.total_cost == 15000.0

    def test_holding_days(self):
        lot = tlo.TaxLot(symbol="AAPL", buy_date=date.today() - timedelta(days=100), shares=10, cost_per_share=100.0)
        assert lot.holding_days == 100

    def test_is_long_term(self):
        lot = tlo.TaxLot(symbol="AAPL", buy_date=date.today() - timedelta(days=400), shares=10, cost_per_share=100.0)
        assert lot.is_long_term is True

    def test_is_short_term(self):
        lot = tlo.TaxLot(symbol="AAPL", buy_date=date.today() - timedelta(days=100), shares=10, cost_per_share=100.0)
        assert lot.is_long_term is False

    def test_discount_factor_long(self):
        lot = tlo.TaxLot(symbol="AAPL", buy_date=date.today() - timedelta(days=400), shares=10, cost_per_share=100.0)
        assert lot.discount_factor == 0.5

    def test_discount_factor_short(self):
        lot = tlo.TaxLot(symbol="AAPL", buy_date=date.today() - timedelta(days=100), shares=10, cost_per_share=100.0)
        assert lot.discount_factor == 1.0


# ---------------------------------------------------------------------------
# Test: compute_tax_on_gain
# ---------------------------------------------------------------------------

class TestComputeTax:
    def test_positive_gain(self):
        tax = tlo.compute_tax_on_gain(1000.0, discount=1.0, marginal_rate=0.37)
        assert tax == pytest.approx(370.0)

    def test_positive_gain_with_discount(self):
        tax = tlo.compute_tax_on_gain(1000.0, discount=0.5, marginal_rate=0.37)
        assert tax == pytest.approx(185.0)

    def test_loss_returns_zero(self):
        tax = tlo.compute_tax_on_gain(-500.0, discount=1.0, marginal_rate=0.37)
        assert tax == 0.0

    def test_zero_gain(self):
        tax = tlo.compute_tax_on_gain(0.0, discount=1.0, marginal_rate=0.37)
        assert tax == 0.0


# ---------------------------------------------------------------------------
# Test: optimize_lot_selection
# ---------------------------------------------------------------------------

class TestOptimizeLotSelection:
    def test_returns_recommendations(self, sample_lots):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=30, current_price=170.0)
        assert len(recs) > 0
        assert all(isinstance(r, tlo.SellRecommendation) for r in recs)

    def test_prefers_loss_lots_first(self, sample_lots):
        """Loss lots should be priority 1 (first)."""
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=30, current_price=170.0)
        # First rec should be the loss lot (cost 180, price 170 → loss)
        first = recs[0]
        assert first.lot.cost_per_share == 180.0  # The loss lot
        assert first.priority == 1

    def test_loss_lot_has_negative_gain(self, sample_lots):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=30, current_price=170.0)
        loss_recs = [r for r in recs if r.priority == 1]
        for r in loss_recs:
            assert r.gain_per_share < 0

    def test_long_term_preferred_over_short_term(self):
        """Long-term lots (50% discount) preferred over short-term (no discount) when both are gains."""
        today = date.today()
        lots = [
            tlo.TaxLot(symbol="X", buy_date=today - timedelta(days=400), shares=100,
                        cost_per_share=100.0, broker="IBKR"),  # long-term gain
            tlo.TaxLot(symbol="X", buy_date=today - timedelta(days=100), shares=100,
                        cost_per_share=100.0, broker="IBKR"),  # short-term gain
        ]
        recs = tlo.optimize_lot_selection(lots, shares_to_sell=50, current_price=150.0)
        # Long-term (priority 2) should come before short-term (priority 3)
        assert recs[0].priority <= recs[-1].priority

    def test_empty_lots(self):
        recs = tlo.optimize_lot_selection([], shares_to_sell=10, current_price=100.0)
        assert recs == []

    def test_zero_shares(self, sample_lots):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=0, current_price=170.0)
        assert recs == []


# ---------------------------------------------------------------------------
# Test: compute_savings
# ---------------------------------------------------------------------------

class TestComputeSavings:
    def test_returns_dict(self, sample_lots):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=30, current_price=170.0)
        savings = tlo.compute_savings(recs)
        assert "optimal_tax" in savings
        assert "worst_case_tax" in savings
        assert "savings" in savings

    def test_savings_non_negative(self, sample_lots):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=30, current_price=170.0)
        savings = tlo.compute_savings(recs)
        assert savings["savings"] >= 0

    def test_empty_recommendations(self):
        savings = tlo.compute_savings([])
        assert savings["optimal_tax"] == 0
        assert savings["savings"] == 0


# ---------------------------------------------------------------------------
# Test: generate_tax_lot_html
# ---------------------------------------------------------------------------

class TestGenerateTaxLotHtml:
    def test_generates_html(self, sample_lots, tmp_path):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=30, current_price=170.0)
        savings = tlo.compute_savings(recs)
        out = tmp_path / "tax_lot.html"
        result = tlo.generate_tax_lot_html("AAPL", recs, savings, out)
        assert result.exists()
        html = result.read_text(encoding="utf-8")
        assert "AAPL" in html

    def test_html_contains_recommendations(self, sample_lots, tmp_path):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=30, current_price=170.0)
        savings = tlo.compute_savings(recs)
        out = tmp_path / "tax_lot.html"
        result = tlo.generate_tax_lot_html("AAPL", recs, savings, out)
        html = result.read_text(encoding="utf-8")
        assert "Tax" in html or "tax" in html


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestTaxLotIntegration:
    def test_full_pipeline(self, sample_lots, tmp_path):
        recs = tlo.optimize_lot_selection(sample_lots, shares_to_sell=50, current_price=170.0)
        assert len(recs) > 0
        savings = tlo.compute_savings(recs)
        assert savings["optimal_tax"] >= 0
        out = tmp_path / "tax_lot.html"
        result = tlo.generate_tax_lot_html("AAPL", recs, savings, out)
        assert result.exists()
