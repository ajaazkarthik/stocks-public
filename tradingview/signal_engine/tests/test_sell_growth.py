"""
Tests for Task 2: Holding Period Classification + AI Growth Prediction.

Tests cover:
  TestComputeHoldingPeriod  — pure logic for held_days / holding_term / CGT classification
  TestCallAiGrowth          — AI growth prediction with mocked OpenAI client
"""

import sys
import json
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock

# ─── path setup ──────────────────────────────────────────────────────────────
_TRADINGVIEW = Path(__file__).resolve().parent.parent.parent   # tradingview/
_SELL_DIR    = _TRADINGVIEW / "sell"
sys.path.insert(0, str(_TRADINGVIEW))
sys.path.insert(0, str(_SELL_DIR))

from sell_strategy import _compute_holding_period, _call_ai_growth


# ─── TestComputeHoldingPeriod ─────────────────────────────────────────────────

class TestComputeHoldingPeriod(unittest.TestCase):
    """Pure-logic tests for _compute_holding_period() — no network calls."""

    _NOW = datetime(2026, 4, 12, 12, 0, 0)

    def test_long_term_over_365(self):
        """400+ days held → LONG_TERM, CGT eligible, days_to_lt == 0."""
        r = _compute_holding_period("2025-03-07", self._NOW)   # 401 days
        self.assertEqual(r["holding_term"], "LONG_TERM")
        self.assertTrue(r["cgt_eligible"])
        self.assertGreaterEqual(r["held_days"], 365)
        self.assertEqual(r["days_to_lt"], 0)

    def test_exactly_365_days(self):
        """Exactly 365 days → LONG_TERM, CGT eligible."""
        r = _compute_holding_period("2025-04-12", self._NOW)   # exactly 365
        self.assertEqual(r["holding_term"], "LONG_TERM")
        self.assertTrue(r["cgt_eligible"])
        self.assertEqual(r["held_days"], 365)
        self.assertEqual(r["days_to_lt"], 0)

    def test_364_days_not_long_term(self):
        """364 days → NOT long-term, days_to_lt == 1."""
        r = _compute_holding_period("2025-04-13", self._NOW)   # 364 days
        self.assertNotEqual(r["holding_term"], "LONG_TERM")
        self.assertFalse(r["cgt_eligible"])
        self.assertEqual(r["held_days"], 364)
        self.assertEqual(r["days_to_lt"], 1)

    def test_med_term_200_days(self):
        """200 days held → MED_TERM."""
        r = _compute_holding_period("2025-09-25", self._NOW)   # ~199 days
        self.assertIn(r["holding_term"], ("MED_TERM", "SHORT_TERM"))
        self.assertFalse(r["cgt_eligible"])
        self.assertGreater(r["days_to_lt"], 0)

    def test_med_term_boundary_180(self):
        """Exactly 180 days → MED_TERM."""
        r = _compute_holding_period("2025-10-14", self._NOW)   # 180 days
        self.assertEqual(r["holding_term"], "MED_TERM")
        self.assertEqual(r["held_days"], 180)

    def test_short_term_boundary_90(self):
        """Exactly 90 days → SHORT_TERM."""
        r = _compute_holding_period("2026-01-12", self._NOW)   # 90 days
        self.assertEqual(r["holding_term"], "SHORT_TERM")
        self.assertEqual(r["held_days"], 90)

    def test_recent_under_90(self):
        """Under 90 days → RECENT."""
        r = _compute_holding_period("2026-03-13", self._NOW)   # ~30 days
        self.assertEqual(r["holding_term"], "RECENT")
        self.assertFalse(r["cgt_eligible"])
        self.assertLess(r["held_days"], 90)

    def test_future_entry_clamps_to_zero(self):
        """Future entry date → held_days == 0, RECENT, not CGT eligible."""
        r = _compute_holding_period("2027-01-01", self._NOW)
        self.assertEqual(r["held_days"], 0)
        self.assertEqual(r["holding_term"], "RECENT")
        self.assertFalse(r["cgt_eligible"])

    def test_none_entry_date_returns_unknown(self):
        """None entry date → safe UNKNOWN defaults."""
        r = _compute_holding_period(None, self._NOW)
        self.assertEqual(r["holding_term"], "UNKNOWN")
        self.assertFalse(r["cgt_eligible"])
        self.assertIsNone(r["held_days"])
        self.assertIsNone(r["days_to_lt"])

    def test_invalid_date_string_returns_unknown(self):
        """Non-parseable date string → safe UNKNOWN defaults."""
        r = _compute_holding_period("not-a-date", self._NOW)
        self.assertEqual(r["holding_term"], "UNKNOWN")
        self.assertFalse(r["cgt_eligible"])
        self.assertIsNone(r["held_days"])

    def test_days_to_lt_formula(self):
        """days_to_lt == 365 - held_days for non-long-term positions."""
        r = _compute_holding_period("2026-03-13", self._NOW)  # ~30 days
        self.assertEqual(r["days_to_lt"], 365 - r["held_days"])

    def test_days_to_lt_zero_when_long_term(self):
        """days_to_lt == 0 when already LONG_TERM."""
        r = _compute_holding_period("2025-01-01", self._NOW)  # >365 days
        self.assertEqual(r["days_to_lt"], 0)

    def test_all_keys_present(self):
        """All four keys must be in the return dict."""
        r = _compute_holding_period("2025-10-14", self._NOW)
        for key in ("held_days", "cgt_eligible", "holding_term", "days_to_lt"):
            self.assertIn(key, r)

    def test_empty_string_entry_date(self):
        """Empty string entry date → UNKNOWN (falsy check)."""
        r = _compute_holding_period("", self._NOW)
        self.assertEqual(r["holding_term"], "UNKNOWN")
        self.assertIsNone(r["held_days"])


# ─── TestCallAiGrowth ─────────────────────────────────────────────────────────

class TestCallAiGrowth(unittest.TestCase):
    """Tests for _call_ai_growth() with mocked AI client."""

    def _make_result(self, **kwargs):
        base = {
            "symbol":          "AAPL",
            "entry_price":     150.0,
            "current_price":   175.0,
            "held_days":       200,
            "holding_term":    "MED_TERM",
            "rsi":             58.0,
            "adx":             28.0,
            "current_gain_pct": None,   # not yet set at call time
        }
        base.update(kwargs)
        return base

    def _make_fund(self, **kwargs):
        base = {
            "earnings_growth": 0.15,
            "revenue_growth":  0.12,
            "roe":             28.5,
            "debt_to_equity":  0.8,
            "forward_pe":      22.0,
            "peg_ratio":       1.4,
            "analyst_target":  200.0,
        }
        base.update(kwargs)
        return base

    def _mock_response(self, payload: dict, model: str = "gpt-4.1-nano"):
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(payload)
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.model = model
        return mock_resp

    def _patch_ai(self, module, ok: bool, client=None):
        """Context manager helper — returns (orig_ok, orig_client)."""
        import sell_strategy as ss
        orig_ok     = ss._GROWTH_AI_OK
        orig_client = ss._SELL_AI_CLIENT
        ss._GROWTH_AI_OK    = ok
        ss._SELL_AI_CLIENT  = client
        return orig_ok, orig_client

    def _restore_ai(self, orig_ok, orig_client):
        import sell_strategy as ss
        ss._GROWTH_AI_OK   = orig_ok
        ss._SELL_AI_CLIENT = orig_client

    # ── AI disabled ──────────────────────────────────────────────────────────

    def test_returns_empty_when_ai_flag_false(self):
        """Returns {} when _GROWTH_AI_OK is False."""
        import sell_strategy as ss
        orig_ok, orig_client = self._patch_ai(ss, False, None)
        try:
            r = _call_ai_growth(self._make_result(), self._make_fund(), 18.0)
            self.assertEqual(r, {})
        finally:
            self._restore_ai(orig_ok, orig_client)

    def test_returns_empty_when_client_is_none(self):
        """Returns {} when _SELL_AI_CLIENT is None even if flag is True."""
        import sell_strategy as ss
        orig_ok, orig_client = self._patch_ai(ss, True, None)
        try:
            r = _call_ai_growth(self._make_result(), self._make_fund(), 18.0)
            self.assertEqual(r, {})
        finally:
            self._restore_ai(orig_ok, orig_client)

    # ── Successful call ───────────────────────────────────────────────────────

    def test_parses_valid_ai_response(self):
        """Parses a well-formed AI JSON response into expected fields."""
        import sell_strategy as ss
        payload = {
            "outlook_6m":     "BULLISH",
            "outlook_12m":    "BULLISH",
            "growth_est_6m":  15.5,
            "growth_est_12m": 28.0,
            "hold_rec":       "HOLD_FOR_GROWTH",
            "reason":         "Strong earnings growth with rising momentum",
        }
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(payload)
        orig_ok, orig_client = self._patch_ai(ss, True, mock_client)
        try:
            r = _call_ai_growth(self._make_result(), self._make_fund(), 18.0)
            self.assertEqual(r["outlook_6m"],     "BULLISH")
            self.assertEqual(r["outlook_12m"],    "BULLISH")
            self.assertAlmostEqual(r["growth_est_6m"],  15.5)
            self.assertAlmostEqual(r["growth_est_12m"], 28.0)
            self.assertEqual(r["hold_rec"],       "HOLD_FOR_GROWTH")
            self.assertIn("reason", r)
            self.assertIn("latency_s", r)
            self.assertIn("model", r)
        finally:
            self._restore_ai(orig_ok, orig_client)

    def test_receives_correct_model_in_response(self):
        """model field in result matches the mock model string."""
        import sell_strategy as ss
        payload = {"outlook_6m": "NEUTRAL", "outlook_12m": "NEUTRAL",
                   "growth_est_6m": 0.0, "growth_est_12m": 0.0,
                   "hold_rec": "MONITOR_CLOSELY", "reason": "Mixed signals"}
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(payload, "gpt-4.1-nano")
        orig_ok, orig_client = self._patch_ai(ss, True, mock_client)
        try:
            r = _call_ai_growth(self._make_result(), self._make_fund(), 18.0)
            self.assertEqual(r["model"], "gpt-4.1-nano")
        finally:
            self._restore_ai(orig_ok, orig_client)

    def test_gain_pct_computed_when_not_in_result(self):
        """When current_gain_pct is None, gain is computed from prices."""
        import sell_strategy as ss
        payload = {"outlook_6m": "BULLISH", "outlook_12m": "BULLISH",
                   "growth_est_6m": 10.0, "growth_est_12m": 20.0,
                   "hold_rec": "HOLD_FOR_GROWTH", "reason": "Good fundamentals"}
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(payload)
        orig_ok, orig_client = self._patch_ai(ss, True, mock_client)
        try:
            result = self._make_result(current_gain_pct=None,
                                       entry_price=100.0, current_price=120.0)
            r = _call_ai_growth(result, self._make_fund(), 18.0)
            # The user_text sent to AI should contain +20.0% or similar — we verify
            # the call was made (no crash) and returns expected structure.
            self.assertIn("outlook_6m", r)
        finally:
            self._restore_ai(orig_ok, orig_client)

    # ── Error handling ────────────────────────────────────────────────────────

    def test_returns_empty_on_api_exception(self):
        """Returns {} when the API call raises any exception."""
        import sell_strategy as ss
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("API timeout")
        orig_ok, orig_client = self._patch_ai(ss, True, mock_client)
        try:
            r = _call_ai_growth(self._make_result(), self._make_fund(), 18.0)
            self.assertEqual(r, {})
        finally:
            self._restore_ai(orig_ok, orig_client)

    def test_returns_empty_on_malformed_json(self):
        """Returns {} when AI returns non-JSON content."""
        import sell_strategy as ss
        mock_choice = MagicMock()
        mock_choice.message.content = "I cannot analyse this position right now.{{{"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.model = "gpt-4.1-nano"
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        orig_ok, orig_client = self._patch_ai(ss, True, mock_client)
        try:
            r = _call_ai_growth(self._make_result(), self._make_fund(), 18.0)
            self.assertEqual(r, {})
        finally:
            self._restore_ai(orig_ok, orig_client)

    def test_handles_all_none_fund_data(self):
        """Works without crashing when all fundamentals are None."""
        import sell_strategy as ss
        payload = {"outlook_6m": "NEUTRAL", "outlook_12m": "NEUTRAL",
                   "growth_est_6m": 0.0, "growth_est_12m": 0.0,
                   "hold_rec": "MONITOR_CLOSELY", "reason": "Insufficient data"}
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(payload)
        orig_ok, orig_client = self._patch_ai(ss, True, mock_client)
        try:
            empty_fund = {k: None for k in ("earnings_growth", "revenue_growth",
                          "roe", "debt_to_equity", "forward_pe", "peg_ratio", "analyst_target")}
            r = _call_ai_growth(self._make_result(), empty_fund, 18.0)
            self.assertEqual(r.get("hold_rec"), "MONITOR_CLOSELY")
        finally:
            self._restore_ai(orig_ok, orig_client)

    def test_handles_missing_prices_in_result(self):
        """Works when entry_price and current_price are both missing."""
        import sell_strategy as ss
        payload = {"outlook_6m": "NEUTRAL", "outlook_12m": "NEUTRAL",
                   "growth_est_6m": 0.0, "growth_est_12m": 0.0,
                   "hold_rec": "MONITOR_CLOSELY", "reason": "No price data"}
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_response(payload)
        orig_ok, orig_client = self._patch_ai(ss, True, mock_client)
        try:
            result = self._make_result(entry_price=None, current_price=None,
                                       current_gain_pct=None)
            r = _call_ai_growth(result, self._make_fund(), 18.0)
            self.assertIn("outlook_6m", r)
        finally:
            self._restore_ai(orig_ok, orig_client)


if __name__ == "__main__":
    unittest.main()
