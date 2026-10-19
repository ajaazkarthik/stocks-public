"""Tests for F14: Broker P&L Reconciliation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingview" / "analysis"))

from tradingview.analysis.pnl_reconciliation import (
    load_positions,
    reconcile_positions,
    broker_summary,
    generate_html,
    Discrepancy,
    _guess_cause,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. load_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadPositions:
    def test_valid_json(self, tmp_path):
        f = tmp_path / "pos.json"
        f.write_text('[{"symbol":"AAPL","pnl":100}]', encoding="utf-8")
        assert len(load_positions(f)) == 1

    def test_missing_file(self, tmp_path):
        assert load_positions(tmp_path / "nope.json") == []

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("NOT_JSON", encoding="utf-8")
        assert load_positions(f) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 2. reconcile_positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcilePositions:
    """Test the core P&L discrepancy detection."""

    def test_no_discrepancy(self):
        """shares*(cur-avg) == pnl → nothing flagged."""
        pos = [{"symbol": "NVDA", "broker": "IBKR",
                "shares": 10, "avg_cost": 100, "current_price": 120, "pnl": 200}]
        result = reconcile_positions(pos, threshold_pct=1.0)
        assert len(result) == 0

    def test_big_discrepancy(self):
        """shares*(110-100)=100 but reported 200 → 50% diff flagged."""
        pos = [{"symbol": "AAPL", "broker": "Moomoo-AU",
                "shares": 10, "avg_cost": 100, "current_price": 110, "pnl": 200}]
        result = reconcile_positions(pos, threshold_pct=1.0)
        assert len(result) == 1
        assert result[0].symbol == "AAPL"
        assert result[0].calculated_pnl == 100.0
        assert result[0].reported_pnl == 200.0

    def test_threshold_filters_small(self):
        """1% threshold filters out < 1% discrepancy."""
        # calc=100, reported=100.5 → diff=0.5, pct=0.5% → under 1%
        pos = [{"symbol": "X", "broker": "IBKR",
                "shares": 10, "avg_cost": 100, "current_price": 110, "pnl": 100.5}]
        result = reconcile_positions(pos, threshold_pct=1.0)
        assert len(result) == 0

    def test_negative_pnl(self):
        """Handles negative P&L correctly."""
        pos = [{"symbol": "LOSE", "broker": "Zerodha",
                "shares": 10, "avg_cost": 100, "current_price": 80, "pnl": -250}]
        result = reconcile_positions(pos, threshold_pct=1.0)
        assert len(result) == 1
        assert result[0].calculated_pnl == -200.0
        assert result[0].reported_pnl == -250.0

    def test_sorted_by_diff(self):
        """Results sorted by diff_abs descending."""
        pos = [
            {"symbol": "SMALL", "broker": "IBKR",
             "shares": 10, "avg_cost": 100, "current_price": 105, "pnl": 100},
            {"symbol": "BIG", "broker": "IBKR",
             "shares": 100, "avg_cost": 100, "current_price": 110, "pnl": 2000},
        ]
        result = reconcile_positions(pos, threshold_pct=1.0)
        assert len(result) == 2
        assert result[0].symbol == "BIG"  # larger diff first

    def test_zero_shares(self):
        """Zero shares with non-zero reported PnL → flagged."""
        pos = [{"symbol": "GHOST", "broker": "IBKR",
                "shares": 0, "avg_cost": 100, "current_price": 110, "pnl": 500}]
        result = reconcile_positions(pos, threshold_pct=1.0)
        assert len(result) == 1
        assert "zero shares" in result[0].likely_cause

    def test_empty_positions(self):
        assert reconcile_positions([]) == []

    def test_missing_fields(self):
        """Missing fields default to 0."""
        pos = [{"symbol": "X", "broker": "IBKR"}]
        # calc = 0*(0-0)=0, reported=0 → no diff
        result = reconcile_positions(pos, threshold_pct=1.0)
        assert len(result) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. broker_summary
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerSummary:
    """Test per-broker P&L aggregation."""

    def test_pre_seeded_all_six(self):
        """All 6 known brokers appear even with empty positions."""
        summary = broker_summary([])
        expected = {"AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"}
        assert set(summary.keys()) == expected

    def test_aggregation(self):
        pos = [
            {"symbol": "A", "broker": "IBKR", "shares": 10, "avg_cost": 100,
             "current_price": 110, "pnl": 100},
            {"symbol": "B", "broker": "IBKR", "shares": 5, "avg_cost": 50,
             "current_price": 60, "pnl": 50},
        ]
        summary = broker_summary(pos)
        assert summary["IBKR"]["count"] == 2
        assert summary["IBKR"]["calc_pnl"] == 150.0  # 100+50
        assert summary["IBKR"]["reported_pnl"] == 150.0

    def test_unknown_broker(self):
        pos = [{"symbol": "X", "broker": "NewBroker", "shares": 1, "avg_cost": 100,
                "current_price": 110, "pnl": 10}]
        summary = broker_summary(pos)
        assert "NewBroker" in summary
        assert summary["NewBroker"]["count"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 4. generate_html
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateHtml:
    def _make_summary(self):
        return {b: {"calc_pnl": 0, "reported_pnl": 0, "count": 0}
                for b in ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]}

    def test_html_with_discrepancies(self):
        d = Discrepancy(
            symbol="NVDA", broker="IBKR", shares=10, avg_cost=100,
            current_price=110, calculated_pnl=100, reported_pnl=200,
            diff_abs=100, diff_pct=50.0, likely_cause="test",
        )
        html = generate_html([d], self._make_summary())
        assert "NVDA" in html
        assert "IBKR" in html
        assert "50.0%" in html

    def test_html_empty(self):
        html = generate_html([], self._make_summary())
        assert "No discrepancies" in html

    def test_html_all_brokers(self):
        """All 6 known brokers appear in broker summary."""
        html = generate_html([], self._make_summary())
        for b in ["AngelOne", "IBKR", "Moomoo-AU", "Moomoo-SG", "Webull AU", "Zerodha"]:
            assert b in html, f"Broker '{b}' missing from HTML"

    def test_html_written(self, tmp_path):
        out = tmp_path / "recon.html"
        generate_html([], self._make_summary(), out)
        assert out.exists()
        assert "P&L Reconciliation" in out.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _guess_cause
# ═══════════════════════════════════════════════════════════════════════════════

class TestGuessCause:
    def test_zero_shares(self):
        assert "zero shares" in _guess_cause(0, 500, 0)

    def test_rounding(self):
        assert "rounding" in _guess_cause(100.0, 100.5, 10)

    def test_reported_zero(self):
        assert "zero" in _guess_cause(100, 0, 10)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
