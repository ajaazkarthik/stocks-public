"""
Unit tests for india_enricher.py — L5 India Portfolio Context (AngelOne + Zerodha).

Focus:
  - AngelOne _load_angelone() dict vs list response format bug
  - broker_status field in enrich() return dict
  - html_badge() 3-state logic (silent / broker-warn / normal)
  - _score_holding() scoring thresholds

No network calls are made. All broker APIs are mocked via unittest.mock.

Run with:
    python -m pytest signal_engine/tests/test_india_enricher.py -v
"""

import sys
import os
import unittest
import types
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

# Add both the tradingview root and india-brokers to the path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "india-brokers"))

import india_enricher as ie


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_enricher(holdings: dict = None, zerodha_ok: bool = True,
                   angelone_ok: bool = True, total_value: float = 1_000_000.0):
    """
    Build an IndiaEnricher with mocked _refresh() and injected state.
    Saves test boilerplate; no network calls are triggered.
    """
    with patch.object(ie.IndiaEnricher, "_refresh", return_value=None):
        enricher = ie.IndiaEnricher()
    enricher._holdings = holdings or {}
    enricher._total_portfolio_value = total_value
    enricher._zerodha_ok = zerodha_ok
    enricher._angelone_ok = angelone_ok
    enricher._last_refresh = 1e18  # far future → no refresh
    return enricher


def _make_holding(symbol: str, avg: float, ltp: float, qty: int = 100,
                  angelone_qty: int = 0, angelone_avg: float = 0.0) -> dict:
    """
    Build a fully-structured combined holding dict.
    By default the position is Zerodha-only; pass angelone_qty to add Angel One.
    """
    z_qty  = qty
    a_qty  = angelone_qty
    total  = z_qty + a_qty
    z_cost = avg * z_qty
    a_cost = angelone_avg * a_qty
    combined_avg = (z_cost + a_cost) / total if total else 0
    ltp_val = ltp
    pnl_pct = ((ltp_val - combined_avg) / combined_avg * 100) if combined_avg else 0
    val     = ltp_val * total
    brokers = (["zerodha"] if z_qty else []) + (["angelone"] if a_qty else [])
    return {
        "symbol": symbol,
        "zerodha_qty": z_qty,  "zerodha_avg": avg,  "zerodha_ltp": ltp,
        "zerodha_pnl": (ltp - avg) * z_qty, "zerodha_val": ltp * z_qty,
        "zerodha_pnl_pct": ((ltp - avg) / avg * 100) if avg else 0,
        "angelone_qty": a_qty, "angelone_avg": angelone_avg, "angelone_ltp": ltp,
        "angelone_pnl": (ltp - angelone_avg) * a_qty if a_qty else 0,
        "angelone_val": ltp * a_qty,
        "angelone_pnl_pct": ((ltp - angelone_avg) / angelone_avg * 100) if angelone_avg else 0,
        "total_qty": total,
        "combined_avg": combined_avg,
        "combined_ltp": ltp_val,
        "combined_pnl": (ltp_val - combined_avg) * total,
        "combined_val": val,
        "combined_pnl_pct": pnl_pct,
        "brokers": brokers,
    }


# ─── _load_angelone: dict vs list response ────────────────────────────────────

class TestLoadAngelOneResponseFormat(unittest.TestCase):
    """
    Angel One's getAllHolding endpoint returns either:
      - A dict: {"holdings": [...], "totalholding": {...}}
      - A plain list: [...]
    _load_angelone() must handle both without crashing.
    """

    def setUp(self):
        """Inject stub modules for pyotp and SmartApi into sys.modules."""
        # pyotp stub
        mock_pyotp = MagicMock()
        mock_pyotp.TOTP.return_value.now.return_value = "123456"
        sys.modules["pyotp"] = mock_pyotp

        # SmartApi / SmartConnect stub
        self._mock_smart = MagicMock()
        self._mock_smart.generateSession.return_value = {"status": True}
        mock_smart_module = types.ModuleType("SmartApi")
        mock_smart_module.SmartConnect = MagicMock(return_value=self._mock_smart)
        sys.modules["SmartApi"] = mock_smart_module

    def tearDown(self):
        """Remove stub modules so they don't leak into other tests."""
        sys.modules.pop("pyotp",    None)
        sys.modules.pop("SmartApi", None)

    def _call_load(self, holding_response_data):
        """Call _load_angelone() with the given payload under data key."""
        self._mock_smart.holding.return_value = {
            "status": True, "data": holding_response_data
        }
        with patch.object(ie.IndiaEnricher, "_refresh", return_value=None):
            enricher = ie.IndiaEnricher()
        enricher._zerodha_ok  = True
        enricher._angelone_ok = False
        return enricher._load_angelone()

    def test_list_format_returned_directly(self):
        """When data is a plain list, returned as-is."""
        holdings = [
            {"tradingsymbol": "INFY", "quantity": 50, "averageprice": 1400.0,
             "ltp": 1500.0, "profitandloss": 5000.0},
        ]
        result = self._call_load(holdings)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tradingsymbol"], "INFY")

    def test_dict_format_extracts_holdings_key(self):
        """When data is {'holdings': [...], 'totalholding': {...}}, extract 'holdings'."""
        holdings = [
            {"tradingsymbol": "TCS",  "quantity": 30, "averageprice": 3500.0,
             "ltp": 3600.0, "profitandloss": 3000.0},
            {"tradingsymbol": "HDFC", "quantity": 20, "averageprice": 1600.0,
             "ltp": 1700.0, "profitandloss": 2000.0},
        ]
        dict_response = {"holdings": holdings, "totalholding": {"totalholdingvalue": 200000}}
        result = self._call_load(dict_response)
        self.assertIsInstance(result, list,
                              "Dict response must yield a list, not a dict")
        self.assertEqual(len(result), 2)
        syms = {h["tradingsymbol"] for h in result}
        self.assertIn("TCS", syms)
        self.assertIn("HDFC", syms)

    def test_dict_format_empty_holdings(self):
        """Dict with empty 'holdings' list returns []."""
        result = self._call_load({"holdings": [], "totalholding": {}})
        self.assertEqual(result, [])

    def test_none_data_returns_empty_list(self):
        """None / falsy data returns []."""
        result = self._call_load(None)
        self.assertEqual(result, [])

    def test_dict_format_no_crash_in_refresh(self):
        """
        When getAllHolding returns dict format, _refresh() must NOT crash
        (previous bug: iterating dict keys -> AttributeError on h.get()).
        """
        holdings = [{"tradingsymbol": "RELIANCE", "quantity": 10,
                     "averageprice": 2800.0, "ltp": 2900.0, "profitandloss": 1000.0}]
        dict_response = {"holdings": holdings, "totalholding": {}}
        self._mock_smart.holding.return_value = {"status": True, "data": dict_response}

        with patch.object(ie.IndiaEnricher, "_load_zerodha", return_value=[]):
            # No _refresh patch → real _refresh() runs with mocked sub-calls
            enricher = ie.IndiaEnricher()

        # If we get here without AttributeError, the dict-format bug is fixed
        self.assertIn("RELIANCE", enricher._holdings)


# ─── enrich(): broker_status included ────────────────────────────────────────

class TestEnrichBrokerStatus(unittest.TestCase):
    """enrich() must include broker_status dict in every India-symbol result."""

    def test_not_held_includes_broker_status(self):
        enricher = _make_enricher(holdings={}, zerodha_ok=True, angelone_ok=True)
        result = enricher.enrich("NSE:INFY")
        self.assertIn("broker_status", result,
                      "enrich() must include 'broker_status' for India symbols")
        self.assertTrue(result["broker_status"]["zerodha"])
        self.assertTrue(result["broker_status"]["angelone"])

    def test_held_includes_broker_status(self):
        holdings = {"INFY": _make_holding("INFY", avg=1400.0, ltp=1500.0)}
        enricher = _make_enricher(holdings=holdings, zerodha_ok=True, angelone_ok=False)
        result = enricher.enrich("NSE:INFY")
        self.assertIn("broker_status", result)
        self.assertTrue(result["broker_status"]["zerodha"])
        self.assertFalse(result["broker_status"]["angelone"])

    def test_both_brokers_down_status_in_result(self):
        enricher = _make_enricher(holdings={}, zerodha_ok=False, angelone_ok=False)
        result = enricher.enrich("NSE:TCS")
        self.assertFalse(result["broker_status"]["zerodha"])
        self.assertFalse(result["broker_status"]["angelone"])

    def test_non_india_symbol_no_broker_status(self):
        """Non-India symbols return {} — no broker_status key."""
        enricher = _make_enricher()
        result = enricher.enrich("NYSE:AAPL")
        self.assertEqual(result, {})
        self.assertNotIn("broker_status", result)


# ─── html_badge(): 3-state logic ──────────────────────────────────────────────

class TestIndiaBadge(unittest.TestCase):
    """html_badge() must render the correct badge for each broker state."""

    # ── State 1: empty dict (non-India) ──────────────────────────────────────

    def test_empty_dict_returns_empty_string(self):
        enricher = _make_enricher()
        self.assertEqual(enricher.html_badge({}), "")

    # ── State 2: both brokers down ────────────────────────────────────────────

    def test_both_brokers_down_shows_amber_warning(self):
        enricher = _make_enricher(holdings={}, zerodha_ok=False, angelone_ok=False)
        e = enricher.enrich("NSE:INFY")
        html = enricher.html_badge(e)
        self.assertIn("⚠", html,
                      "Both brokers down must show amber warning badge")
        self.assertNotIn("FRESH", html,
                         "Should NOT show FRESH when broker data is unavailable")
        self.assertIn("Broker data N/A", html)

    def test_both_brokers_down_has_tooltip(self):
        """Warning badge must include a tooltip explaining the situation."""
        enricher = _make_enricher(holdings={}, zerodha_ok=False, angelone_ok=False)
        e = enricher.enrich("NSE:INFY")
        html = enricher.html_badge(e)
        self.assertIn("title=", html)
        self.assertIn("unreachable", html.lower())

    # ── State 3a: one broker OK → FRESH with partial-data warning ────────────

    def test_only_zerodha_ok_shows_fresh_with_warning(self):
        enricher = _make_enricher(holdings={}, zerodha_ok=True, angelone_ok=False)
        e = enricher.enrich("NSE:INFY")
        html = enricher.html_badge(e)
        self.assertIn("FRESH", html)
        self.assertIn("⚠", html,
                      "Partial broker data must show ⚠ alongside FRESH")

    def test_only_angelone_ok_shows_fresh_with_warning(self):
        enricher = _make_enricher(holdings={}, zerodha_ok=False, angelone_ok=True)
        e = enricher.enrich("NSE:TCS")
        html = enricher.html_badge(e)
        self.assertIn("FRESH", html)
        self.assertIn("⚠", html)

    # ── State 3b: both brokers OK → clean FRESH ───────────────────────────────

    def test_both_ok_shows_clean_fresh(self):
        enricher = _make_enricher(holdings={}, zerodha_ok=True, angelone_ok=True)
        e = enricher.enrich("NSE:INFY")
        html = enricher.html_badge(e)
        self.assertIn("FRESH", html)
        self.assertNotIn("⚠", html,
                         "Both brokers OK → no warning symbol needed")

    # ── Holding badge ─────────────────────────────────────────────────────────

    def test_holding_badge_shows_quantity_and_pnl(self):
        holdings = {"INFOSYS": _make_holding("INFOSYS", avg=1400.0, ltp=1500.0, qty=50)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:INFOSYS")
        html = enricher.html_badge(e)
        self.assertIn("HOLDING", html)
        self.assertIn("50", html,
                      "Badge must show quantity")

    def test_holding_badge_pnl_positive_green(self):
        """Positive PnL (0–15%) → green colour."""
        holdings = {"BEL": _make_holding("BEL", avg=100.0, ltp=110.0)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:BEL")
        html = enricher.html_badge(e)
        self.assertIn("#4caf50", html, "0–15% gain should use green colour")

    def test_holding_badge_pnl_large_gain_orange(self):
        """PnL > 15% → orange (be cautious) colour."""
        holdings = {"CDSL": _make_holding("CDSL", avg=100.0, ltp=120.0)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:CDSL")
        html = enricher.html_badge(e)
        self.assertIn("#ff9800", html, ">=15% gain should use orange colour")

    def test_holding_badge_pnl_large_loss_red(self):
        """PnL -15% to -30% → red colour."""
        holdings = {"CAPACITE": _make_holding("CAPACITE", avg=100.0, ltp=76.0)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:CAPACITE")
        html = enricher.html_badge(e)
        self.assertIn("#f44336", html, "-15% to -30% loss should use red colour")

    def test_holding_badge_includes_avg_price_tooltip(self):
        """Holding badge must include avg price in tooltip."""
        holdings = {"INFY": _make_holding("INFY", avg=1400.0, ltp=1500.0)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:INFY")
        html = enricher.html_badge(e)
        self.assertIn("1,400.00", html)

    # ── Backwards compat: missing broker_status key ───────────────────────────

    def test_missing_broker_status_key_renders_normally(self):
        """Old enrichment dicts without broker_status must still render (backwards compat)."""
        enricher = _make_enricher()
        old_enrichment = {"holding": False}  # no broker_status key
        html = enricher.html_badge(old_enrichment)
        # Should not raise; should show FRESH (both assumed OK → no warning)
        self.assertIn("FRESH", html)
        self.assertNotIn("⚠", html)


# ─── AngelOne + Zerodha combined holdings ─────────────────────────────────────

class TestCombinedBrokerHoldings(unittest.TestCase):
    """Holdings that span both Zerodha and Angel One must be merged correctly."""

    def test_combined_avg_weighted(self):
        """
        100 shares @ ₹1000 (Zerodha) + 50 shares @ ₹1100 (Angel One)
        → combined avg = (1000*100 + 1100*50) / 150 = 155000/150 ≈ 1033.33
        """
        holdings = {"INFY": _make_holding("INFY", avg=1000.0, ltp=1200.0, qty=100,
                                          angelone_qty=50, angelone_avg=1100.0)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:INFY")
        self.assertAlmostEqual(e["combined_avg"], 1033.33, places=1)

    def test_both_brokers_shown_in_badge(self):
        holdings = {"TCS": _make_holding("TCS", avg=3500.0, ltp=3600.0, qty=10,
                                         angelone_qty=5, angelone_avg=3520.0)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:TCS")
        html = enricher.html_badge(e)
        # broker_str = "Z+A"
        self.assertIn("Z+A", html, "Badge tooltip must list both broker initials")

    def test_angelone_only_holding(self):
        """Stock held only through Angel One (Zerodha qty = 0)."""
        holdings = {"CDSL": _make_holding("CDSL", avg=0.0, ltp=1200.0, qty=0,
                                          angelone_qty=30, angelone_avg=1100.0)}
        enricher = _make_enricher(holdings=holdings)
        e = enricher.enrich("NSE:CDSL")
        self.assertEqual(e["total_qty"], 30)
        html = enricher.html_badge(e)
        self.assertIn("HOLDING", html)
        self.assertIn("A", html)  # broker_str should contain "A"


# ─── _score_holding scoring thresholds ────────────────────────────────────────

class TestScoreHolding(unittest.TestCase):
    """_score_holding() thresholds validated against the docstring spec."""

    def _score(self, pnl_pct: float, weight_pct: float = 1.0) -> tuple:
        return ie._score_holding(pnl_pct, weight_pct)

    def test_big_gain_avoids_chasing(self):
        score, reason = self._score(25.0)
        self.assertEqual(score, 3.0)
        self.assertIn("chasing", reason)

    def test_neutral_gain(self):
        score, _ = self._score(10.0)
        self.assertEqual(score, 5.0)

    def test_near_breakeven_is_good_avg(self):
        score, reason = self._score(-3.0)
        self.assertEqual(score, 7.0)
        self.assertIn("avg", reason.lower())

    def test_small_loss_avg_opportunity(self):
        score, _ = self._score(-10.0)
        self.assertEqual(score, 6.5)

    def test_moderate_loss_caution(self):
        score, _ = self._score(-22.0)
        self.assertEqual(score, 5.0)

    def test_deep_loss_verify_fundamentals(self):
        score, reason = self._score(-35.0)
        self.assertEqual(score, 3.0)
        self.assertIn("fundamental", reason.lower())

    def test_overweight_severe_penalty(self):
        """portfolio weight > 10% → -4 penalty"""
        base_score, _ = self._score(0.0, weight_pct=1.0)
        heavy_score, reason = self._score(0.0, weight_pct=12.0)
        self.assertLess(heavy_score, base_score)
        self.assertIn("OVERWEIGHT", reason)

    def test_overweight_moderate_penalty(self):
        """portfolio weight 5–10% → -2 penalty"""
        base_score, _ = self._score(0.0, weight_pct=1.0)
        moderate_score, reason = self._score(0.0, weight_pct=7.0)
        self.assertLess(moderate_score, base_score)
        self.assertIn("weight", reason.lower())

    def test_score_never_below_zero(self):
        """Score must be clamped at 0 even for worst-case combination."""
        score, _ = self._score(-50.0, weight_pct=15.0)
        self.assertGreaterEqual(score, 0.0)

    def test_score_never_above_ten(self):
        for pnl, wt in [(-3.0, 1.0), (0.0, 2.0), (10.0, 0.5)]:
            score, _ = self._score(pnl, wt)
            self.assertLessEqual(score, 10.0, f"Score exceeded 10 for pnl={pnl}, wt={wt}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
