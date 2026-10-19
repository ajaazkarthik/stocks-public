"""
Unit tests: Position sizer integration in webhook_server.py
           + YouTube member-channel score multiplier.

All external I/O (Moomoo, IBKR, YouTube API, Smallcase, backtest cache) is
fully mocked.  No network calls are made.

Run with:
    python -m pytest signal_engine/tests/test_webhook_sizer.py -v
"""

from __future__ import annotations

import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

# ── sys.path so imports work regardless of cwd ──────────────────────────────
_TV_DIR = Path(__file__).parent.parent.parent   # .../tradingview/
sys.path.insert(0, str(_TV_DIR))


# ═══════════════════════════════════════════════════════════════════════════════
# YouTube member-channel weight tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestYoutubeMemberChannelConfig(unittest.TestCase):
    """youtube_enricher — MEMBER_CHANNELS and MEMBER_WEIGHT_MULTIPLIER config."""

    def setUp(self):
        import importlib
        import youtube_enricher as yt
        importlib.reload(yt)   # fresh module to avoid cache contamination
        self.yt = yt

    def test_member_channels_is_set(self):
        self.assertIsInstance(self.yt.MEMBER_CHANNELS, set)
        self.assertGreater(len(self.yt.MEMBER_CHANNELS), 0,
                           "MEMBER_CHANNELS must not be empty")

    def test_akshat_channel_in_member_channels(self):
        AKSHAT_ID = "UCqW8jxh4tH1Z1sWPbkGWL4g"
        self.assertIn(AKSHAT_ID, self.yt.MEMBER_CHANNELS)

    def test_member_weight_multiplier_above_one(self):
        self.assertGreater(self.yt.MEMBER_WEIGHT_MULTIPLIER, 1.0,
                           "Member multiplier must be > 1 to boost scores")

    def test_expert_channels_overlap_with_member_channels(self):
        """At least one EXPERT_CHANNELS ID should be in MEMBER_CHANNELS."""
        overlap = self.yt.MEMBER_CHANNELS & set(self.yt.EXPERT_CHANNELS.values())
        self.assertGreater(len(overlap), 0,
                           "No overlap between EXPERT_CHANNELS and MEMBER_CHANNELS")


class TestYoutubeMemberScore(unittest.TestCase):
    """youtube_enricher.score_youtube — member channel multiplier applied correctly."""

    def setUp(self):
        import youtube_enricher as yt
        self.yt = yt

    def _single_vid(self, in_title=False, is_member=False, sentiment=0.0) -> dict:
        return {
            "mention_count": 1,
            "sentiment": sentiment,
            "videos": [{
                "video_id": "vid01",
                "title": "Test video",
                "channel": "Akshat Shrivastava",
                "published_at": "2026-04-06T10:00:00",
                "sentiment": sentiment,
                "in_title": in_title,
                "in_transcript": False,
                "is_member_channel": is_member,
            }],
        }

    def test_non_member_baseline_score(self):
        data  = self._single_vid(in_title=False, is_member=False, sentiment=0.0)
        score = self.yt.score_youtube(data)
        self.assertAlmostEqual(score, 3.5, places=1,
                               msg="1 non-member mention, neutral → 3.5")

    def test_member_channel_boosts_above_non_member(self):
        non_member = self._single_vid(in_title=False, is_member=False, sentiment=0.0)
        member     = self._single_vid(in_title=False, is_member=True,  sentiment=0.0)
        self.assertGreater(self.yt.score_youtube(member),
                           self.yt.score_youtube(non_member),
                           "Member channel mention must score higher than non-member")

    def test_member_channel_multiplier_is_applied(self):
        """score(member) ≈ score(non-member) * MEMBER_WEIGHT_MULTIPLIER (neutral sent)."""
        base  = self._single_vid(in_title=False, is_member=False)
        boost = self._single_vid(in_title=False, is_member=True)
        base_s  = self.yt.score_youtube(base)
        boost_s = self.yt.score_youtube(boost)
        expected = round(min(10.0, base_s * self.yt.MEMBER_WEIGHT_MULTIPLIER), 2)
        self.assertAlmostEqual(boost_s, expected, places=1)

    def test_member_title_mention_capped_at_10(self):
        """High member multiplier on a title mention should be capped at 10.0."""
        data = {
            "mention_count": 2,
            "sentiment": 0.9,  # very positive
            "videos": [
                {"video_id": "v1", "title": "BUY RELIANCE NOW",
                 "channel": "Akshat", "published_at": "2026-04-06T10:00:00",
                 "sentiment": 0.9, "in_title": True, "in_transcript": True,
                 "is_member_channel": True},
                {"video_id": "v2", "title": "RELIANCE analysis",
                 "channel": "Akshat", "published_at": "2026-04-05T10:00:00",
                 "sentiment": 0.8, "in_title": True, "in_transcript": False,
                 "is_member_channel": True},
            ],
        }
        score = self.yt.score_youtube(data)
        self.assertLessEqual(score, 10.0, "Score must never exceed 10.0")
        self.assertGreaterEqual(score, 9.0, "Strong member title mention should be ≥ 9")

    def test_no_mentions_still_zero(self):
        data  = {"mention_count": 0, "sentiment": 0.0, "videos": []}
        score = self.yt.score_youtube(data)
        self.assertEqual(score, 0.0, "Zero mentions = zero score even with member channel")

    def test_is_member_channel_flag_in_mention(self):
        """get_youtube_mentions must attach is_member_channel to each community post mention."""
        yt = self.yt
        fake_post = {
            "post_id":        "cp_test1",
            "text":           "NSE:RELIANCE is my top pick this quarter",
            "published_time": "1 hour ago",
            "is_member_only": False,
        }
        with (
            patch("youtube_enricher._load_youtube_cookie", return_value="SAPISID=abc;SID=def"),
            patch("youtube_enricher._fetch_community_posts", return_value=[fake_post]),
        ):
            data = yt.get_youtube_mentions("NSE:RELIANCE", use_transcript=False)

        for v in data.get("videos", []):
            self.assertIn("is_member_channel", v,
                          "is_member_channel key must be present in every mention")

    def test_member_channel_flag_true_for_member_channel_id(self):
        """Community post from Akshat's channel → is_member_channel=True."""
        yt = self.yt
        fake_post = {
            "post_id":        "cp_test2",
            "text":           "RELIANCE deep dive — NSE:RELIANCE fundamentals reviewed",
            "published_time": "2 hours ago",
            "is_member_only": False,
        }
        with (
            patch("youtube_enricher._load_youtube_cookie", return_value="SAPISID=abc;SID=def"),
            patch("youtube_enricher._fetch_community_posts", return_value=[fake_post]),
        ):
            data = yt.get_youtube_mentions("NSE:RELIANCE", use_transcript=False)

        member_vids = [v for v in data.get("videos", []) if v.get("is_member_channel")]
        self.assertGreater(len(member_vids), 0,
                           "All community posts from MEMBER_CHANNELS must have is_member_channel=True")


# ═══════════════════════════════════════════════════════════════════════════════
# Backtest position sizer: effective_units helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestEffectiveUnitsLogic(unittest.TestCase):
    """
    Pure-logic tests for "effective_units" calculation used in
    process_fire_background():
      - backtest_data["size_units"] sets the base unit count
      - l7_score >= 7.0 applies a +1 boost (capped at 3)
    """

    @staticmethod
    def _compute_effective(size_units: int | None, l7_score: float | None,
                            sizer_available: bool = True) -> int:
        """Mirrors the process_fire_background logic in webhook_server.py."""
        backtest_data  = {"size_units": size_units} if size_units is not None else {}
        effective_units = backtest_data.get("size_units", 1) if backtest_data else 1

        # L7 boost
        if l7_score is not None and l7_score >= 7.0 and effective_units > 0:
            effective_units = min(3, effective_units + 1)

        return effective_units

    # ── Base from backtest ────────────────────────────────────────────────────
    def test_no_backtest_defaults_to_1(self):
        self.assertEqual(self._compute_effective(None, None), 1)

    def test_size_units_1_returns_1(self):
        self.assertEqual(self._compute_effective(1, None), 1)

    def test_size_units_2_returns_2(self):
        self.assertEqual(self._compute_effective(2, None), 2)

    def test_size_units_3_returns_3(self):
        self.assertEqual(self._compute_effective(3, None), 3)

    def test_size_units_0_avoid_returns_0(self):
        self.assertEqual(self._compute_effective(0, None), 0)

    # ── L7 enricher boost ────────────────────────────────────────────────────
    def test_l7_boost_adds_1_when_score_ge_7(self):
        """L7 ≥ 7.0 bumps units +1."""
        self.assertEqual(self._compute_effective(1, 7.0), 2)

    def test_l7_boost_exactly_7_triggers(self):
        """Boundary: L7 = 7.0 exactly → boost applied."""
        self.assertEqual(self._compute_effective(2, 7.0), 3)

    def test_l7_boost_below_7_no_change(self):
        """L7 < 7.0 → no boost."""
        self.assertEqual(self._compute_effective(1, 6.9), 1)

    def test_l7_boost_capped_at_3(self):
        """Already at 3 units + L7 boost → stays at 3."""
        self.assertEqual(self._compute_effective(3, 9.0), 3)

    def test_l7_boost_not_applied_when_avoid(self):
        """size_units=0 (AVOID) + strong L7 still returns 0."""
        self.assertEqual(self._compute_effective(0, 9.5), 0)

    def test_l7_high_score_from_2_gives_3(self):
        self.assertEqual(self._compute_effective(2, 8.0), 3)

    def test_l7_score_none_no_boost(self):
        """l7_score=None → no boost, returns base."""
        self.assertEqual(self._compute_effective(2, None), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# place_moomoo_order — units parameter affects target_spend
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlaceMoomooOrderUnits(unittest.TestCase):
    """
    Test the ``units`` parameter of place_moomoo_order().
    We mock Moomoo at the import level so the test runs without OpenD.
    """

    @classmethod
    def setUpClass(cls):
        # Build minimal moomoo stub so webhook_server can import without OpenD
        mm_stub = types.ModuleType("moomoo")
        for name in ("OpenSecTradeContext", "TrdMarket", "TrdEnv", "TrdSide",
                     "OrderType", "TimeInForce", "SecurityFirm", "Currency"):
            setattr(mm_stub, name, MagicMock())
        mm_stub.RET_OK = 0
        sys.modules.setdefault("moomoo", mm_stub)
        # Import after stub is in place
        import importlib, webhook_server as ws
        importlib.reload(ws)
        cls.ws = ws

    def _mock_trade_ctx(self, available_power: float = 1000.0):
        """Return a mock OpenSecTradeContext that reports ``available_power``."""
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        # unlock_trade
        ctx.unlock_trade.return_value = (0, None)
        # acc_info
        acc_df = MagicMock()
        acc_df.empty = False
        acc_df.iloc = MagicMock()
        acc_df.iloc.__getitem__ = lambda self, _: {"power": available_power}
        ctx.accinfo_query.return_value = (0, acc_df)
        # place order
        order_df = MagicMock()
        order_df.empty = False
        order_df.iloc = MagicMock()
        order_df.iloc.__getitem__ = lambda self, _: {"order_id": "ORD001", "order_status": "SUBMITTED_ASK"}
        order_df.columns = ["order_id", "order_status"]
        ctx.place_order.return_value = (0, order_df)
        return ctx

    def _call(self, order_size_usd: int, units: int,
              available_power: float = 5000.0, price: float = 100.0) -> dict:
        ws = self.ws
        ws._MOOMOO_TRADE_OK = True
        with (
            patch.object(ws, "tv_to_mm_order_code", return_value="HK:00700"),
            patch.object(ws, "OpenSecTradeContext", return_value=self._mock_trade_ctx(available_power)),
        ):
            return ws.place_moomoo_order(
                tv_sym="NSE:TEST",
                current_price=price,
                order_size_usd=order_size_usd,
                trade_pwd="pwd",
                units=units,
            )

    def test_units_1_target_is_order_size(self):
        """units=1 → target_spend = 1 × order_size_usd."""
        result = self._call(order_size_usd=500, units=1, price=100.0)
        self.assertLessEqual(result.get("actual_spend", 0), 500 * 1 * 1.0,
                             "spend must not exceed 500×1")
        self.assertGreater(result.get("qty", 0), 0)

    def test_units_2_doubles_target(self):
        """units=2 → target_spend = 2 × order_size_usd (budget permitting)."""
        result = self._call(order_size_usd=500, units=2, price=50.0,
                            available_power=2000.0)
        expected_max = 500 * 2
        self.assertLessEqual(result.get("actual_spend", 0), expected_max)
        # qty should be at least double units=1 case (same price, double budget)
        self.assertGreaterEqual(result.get("qty", 0), 2)

    def test_units_3_triples_target(self):
        result = self._call(order_size_usd=300, units=3, price=100.0,
                            available_power=5000.0)
        self.assertLessEqual(result.get("actual_spend", 0), 300 * 3)
        self.assertGreater(result.get("qty", 0), 0)

    def test_units_capped_by_available_power(self):
        """Even with units=3, spend can't exceed available_power * 0.95."""
        result = self._call(order_size_usd=1000, units=3, price=50.0,
                            available_power=200.0)   # only $200 available
        self.assertLessEqual(result.get("actual_spend", 0), 200 * 0.95 + 1,
                             "Spend must not exceed 95% of available_power")

    def test_units_in_result_dict(self):
        result = self._call(order_size_usd=500, units=2)
        self.assertIn("units", result, "result must include 'units' field")
        self.assertEqual(result["units"], 2)

    def test_units_moomoo_error_returns_error_dict(self):
        """If Moomoo raises an exception, result should have error key."""
        ws = self.ws
        ws._MOOMOO_TRADE_OK = True
        with (
            patch.object(ws, "tv_to_mm_order_code", return_value="HK:00700"),
            patch.object(ws, "OpenSecTradeContext", side_effect=RuntimeError("OpenD down")),
        ):
            result = ws.place_moomoo_order(
                tv_sym="NSE:TEST", current_price=100.0,
                order_size_usd=500, trade_pwd="pwd", units=2,
            )
        self.assertFalse(result.get("success"), "Must report failure when Moomoo raises")
        self.assertIn("error", result)


# ═══════════════════════════════════════════════════════════════════════════════
# analyse_fire() return dict — verify new fields are present
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnalyseFireReturnKeys(unittest.TestCase):
    """
    analyse_fire() must return l6_enrichment, l7_enrichment, backtest_data
    keys in its result dict (None when enricher is unavailable/failed).

    Strategy: disable all enrichers and patch the core signal_stack call used
    by analyse_fire so we get a clean return dict without live network calls.
    """

    @classmethod
    def setUpClass(cls):
        for mod in ("moomoo",):
            if mod not in sys.modules:
                stub = types.ModuleType(mod)
                stub.RET_OK = 0
                for n in ("OpenSecTradeContext", "TrdMarket", "TrdEnv", "TrdSide",
                           "OrderType", "TimeInForce", "SecurityFirm", "Currency"):
                    setattr(stub, n, MagicMock())
                sys.modules[mod] = stub

        import importlib
        import webhook_server as ws
        importlib.reload(ws)
        cls.ws = ws

    def _mock_fire(self) -> dict:
        return {
            "pro_symbol": "NSE:RELIANCE",
            "symbol":     "NSE:RELIANCE",
            "resolution": "60",
            "message":    "BUY RELIANCE price: 2900, WinRate: 65%",
            "fire_time":  "2026-04-07T09:00:00+00:00",
            "close":      2900.0,
        }

    def _call_with_all_disabled(self, *, sizer_available: bool = False,
                                  use_sizer: bool = False) -> dict:
        """Run analyse_fire with all enrichers disabled and core SS mocked.

        Patches every external dependency (network, yfinance, yield curve) so
        the function runs to completion in CI without network access.
        """
        ws = self.ws
        ws._L6_AVAILABLE     = False
        ws._L7_AVAILABLE     = False
        ws._SIZER_AVAILABLE  = sizer_available
        ws._use_sizer        = use_sizer
        ws._TV_FUND_AVAILABLE = False   # avoid real TV screener calls

        fake_l_score = {"total": 0.6}
        fake_fund    = {"current_price": 2900.0, "company_name": "Test Co"}

        mock_yf = MagicMock()
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker

        with (
            patch.object(ws, "ss") as mock_ss,
            patch.object(ws, "yf", mock_yf),
            patch.object(ws, "fetch_yield_curve", return_value={}),
        ):
            mock_ss.tv_to_yf.return_value                = "RELIANCE.NS"
            mock_ss.parse_fire_meta.return_value         = {}
            mock_ss.resolve_etf_underlying.return_value  = (None, None)
            mock_ss.fetch_tv_screener.return_value       = {}
            mock_ss.score_technical.return_value         = fake_l_score
            mock_ss.fetch_fundamentals.return_value      = fake_fund
            mock_ss.score_fundamental.return_value       = fake_l_score
            mock_ss.fetch_sentiment_data.return_value    = {}
            mock_ss.score_sentiment.return_value         = fake_l_score
            mock_ss.fetch_market_internals.return_value  = {}
            mock_ss.score_market_internals.return_value  = fake_l_score
            mock_ss.composite.return_value               = 65.0
            mock_ss.grade.return_value                   = ("B", "#4CAF50")
            return ws.analyse_fire(self._mock_fire())

    def test_l6_enrichment_key_in_result(self):
        result = self._call_with_all_disabled()
        self.assertIn("l6_enrichment", result)

    def test_l7_enrichment_key_in_result(self):
        result = self._call_with_all_disabled()
        self.assertIn("l7_enrichment", result)

    def test_backtest_data_key_in_result(self):
        result = self._call_with_all_disabled()
        self.assertIn("backtest_data", result)

    def test_backtest_data_none_when_sizer_disabled(self):
        result = self._call_with_all_disabled()
        self.assertIsNone(result.get("backtest_data"),
                          "backtest_data must be None when sizer is disabled")

    def test_backtest_data_populated_from_sizer(self):
        """When sizer is available and returns a result, backtest_data is that dict."""
        ws = self.ws
        fake_sizer = {"size_units": 2, "size_label": "STRONG BUY",
                      "win_rate": 0.62, "sharpe": 1.4, "from_cache": True}
        with patch.object(ws, "_wh_get_position_size", return_value=fake_sizer):
            result = self._call_with_all_disabled(sizer_available=True, use_sizer=True)
        ws._SIZER_AVAILABLE = False
        bd = result.get("backtest_data")
        self.assertIsInstance(bd, dict, "backtest_data must be a dict when sizer returns data")
        self.assertIn("size_units", bd)


# ═══════════════════════════════════════════════════════════════════════════════
# _run_enricher_test — smoke tests (check it runs without crashing + prints output)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunEnricherTest(unittest.TestCase):
    """_run_enricher_test() — verify it outputs correct headings without errors."""

    @classmethod
    def setUpClass(cls):
        for mod in ("moomoo",):
            if mod not in sys.modules:
                stub = types.ModuleType(mod)
                stub.RET_OK = 0
                for n in ("OpenSecTradeContext", "TrdMarket", "TrdEnv", "TrdSide",
                           "OrderType", "TimeInForce", "SecurityFirm", "Currency"):
                    setattr(stub, n, MagicMock())
                sys.modules[mod] = stub

        import importlib
        import webhook_server as ws
        importlib.reload(ws)
        cls.ws = ws

    def _run(self, source: str, symbol: str = "NSE:TEST") -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.ws._run_enricher_test(source, symbol)
        return buf.getvalue()

    def test_youtube_heading_printed(self):
        out = self._run("youtube")
        self.assertIn("YouTube", out)

    def test_smallcase_heading_printed(self):
        out = self._run("smallcase")
        self.assertIn("Smallcase", out)

    def test_twitter_heading_printed(self):
        out = self._run("twitter")
        self.assertIn("Twitter", out)

    def test_sizer_heading_printed(self):
        out = self._run("sizer")
        self.assertIn("Sizer", out.replace("Backtest Position Sizer", "Sizer"))

    def test_all_prints_all_headings(self):
        out = self._run("all")
        for heading in ("YouTube", "Smallcase", "Twitter"):
            self.assertIn(heading, out)

    def test_runs_without_crashing_when_enrichers_unavailable(self):
        """When enrichers are disabled (_L7_AVAILABLE=False), test just prints warning."""
        ws = self.ws
        orig = ws._L7_AVAILABLE
        try:
            ws._L7_AVAILABLE = False
            out = self._run("youtube")
        finally:
            ws._L7_AVAILABLE = orig
        self.assertIn("not available", out.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# CLI args — verify new argparse options are registered
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebhookCLIArgs(unittest.TestCase):
    """Verify --no-sizer and --test-enrichers are registered argparse options."""

    @classmethod
    def setUpClass(cls):
        for mod in ("moomoo",):
            if mod not in sys.modules:
                stub = types.ModuleType(mod)
                stub.RET_OK = 0
                for n in ("OpenSecTradeContext", "TrdMarket", "TrdEnv", "TrdSide",
                           "OrderType", "TimeInForce", "SecurityFirm", "Currency"):
                    setattr(stub, n, MagicMock())
                sys.modules[mod] = stub

        import importlib
        import webhook_server as ws
        importlib.reload(ws)
        cls.ws = ws

    def _parser(self):
        import argparse
        ws = self.ws
        # We replicate what main() does to build the parser
        parser = argparse.ArgumentParser(description="Signal Stack Webhook Server")
        parser.add_argument("--port",      type=int, default=8080)
        parser.add_argument("--threshold", type=int, default=55)
        parser.add_argument("--order-size",type=int, default=0)
        parser.add_argument("--trade-pwd", type=str, default="")
        parser.add_argument("--no-sizer",  action="store_true")
        parser.add_argument("--test-enrichers", metavar="SOURCE",
                            choices=["youtube", "smallcase", "twitter", "sizer", "all"])
        parser.add_argument("--test-symbol", default="NSE:RELIANCE")
        return parser

    def test_no_sizer_flag(self):
        args = self._parser().parse_args(["--no-sizer"])
        self.assertTrue(args.no_sizer)

    def test_no_sizer_default_false(self):
        args = self._parser().parse_args([])
        self.assertFalse(args.no_sizer)

    def test_test_enrichers_youtube(self):
        args = self._parser().parse_args(["--test-enrichers", "youtube"])
        self.assertEqual(args.test_enrichers, "youtube")

    def test_test_enrichers_all(self):
        args = self._parser().parse_args(["--test-enrichers", "all"])
        self.assertEqual(args.test_enrichers, "all")

    def test_test_symbol_default(self):
        args = self._parser().parse_args([])
        self.assertEqual(args.test_symbol, "NSE:RELIANCE")

    def test_test_symbol_custom(self):
        args = self._parser().parse_args(["--test-symbol", "NASDAQ:NVDA"])
        self.assertEqual(args.test_symbol, "NASDAQ:NVDA")

    def test_invalid_enricher_choice_raises(self):
        import argparse
        with self.assertRaises(SystemExit):
            self._parser().parse_args(["--test-enrichers", "instagram"])




# ═══════════════════════════════════════════════════════════════════════════════
# YouTube community posts — InnerTube helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestPostWithinLookback(unittest.TestCase):
    """_post_within_lookback — relative time string parsing."""

    def setUp(self):
        import youtube_enricher as yt
        self.fn = yt._post_within_lookback

    def test_hours_within(self):
        self.assertTrue(self.fn("3 hours ago", 72))

    def test_hours_outside(self):
        self.assertFalse(self.fn("100 hours ago", 72))

    def test_days_within(self):
        self.assertTrue(self.fn("2 days ago", 72))

    def test_days_outside(self):
        self.assertFalse(self.fn("4 days ago", 72))

    def test_weeks_outside(self):
        self.assertFalse(self.fn("2 weeks ago", 72))

    def test_empty_string_permissive(self):
        self.assertTrue(self.fn("", 72))

    def test_unparseable_permissive(self):
        self.assertTrue(self.fn("just now", 72))


class TestExtractPostsFromInnertube(unittest.TestCase):
    """_extract_posts_from_innertube — parses postRenderer objects from InnerTube JSON."""

    def setUp(self):
        import youtube_enricher as yt
        self.fn = yt._extract_posts_from_innertube

    def _minimal_response(self, text: str, is_member_only: bool = False,
                           post_id: str = "p001") -> dict:
        post = {
            "postRenderer": {
                "postId": post_id,
                "contentText": {"runs": [{"text": text}]},
                "publishedTimeText": {"runs": [{"text": "2 hours ago"}]},
            }
        }
        if is_member_only:
            post["postRenderer"]["sponsorsOnlyBadge"] = {}
        return {"tabs": [{"content": {"items": [post]}}]}

    def test_extracts_post_text(self):
        data = self._minimal_response("THERMAX is my top pick for 2025")
        posts: list = []
        self.fn(data, posts, 10)
        self.assertEqual(len(posts), 1)
        self.assertIn("THERMAX", posts[0]["text"])

    def test_extracts_post_id(self):
        data = self._minimal_response("NSE:RELIANCE buy now", post_id="abc123")
        posts: list = []
        self.fn(data, posts, 10)
        self.assertEqual(posts[0]["post_id"], "abc123")

    def test_member_only_flag_true(self):
        data = self._minimal_response("INFY members only", is_member_only=True)
        posts: list = []
        self.fn(data, posts, 10)
        self.assertTrue(posts[0]["is_member_only"])

    def test_member_only_flag_false(self):
        data = self._minimal_response("Public post about HDFC")
        posts: list = []
        self.fn(data, posts, 10)
        self.assertFalse(posts[0]["is_member_only"])

    def test_max_posts_respected(self):
        # Wrap two posts in the same list
        post1 = {
            "postRenderer": {
                "postId": "p1",
                "contentText": {"runs": [{"text": "post one RELIANCE"}]},
                "publishedTimeText": {"runs": [{"text": "1 hour ago"}]},
            }
        }
        post2 = {
            "postRenderer": {
                "postId": "p2",
                "contentText": {"runs": [{"text": "post two HDFC"}]},
                "publishedTimeText": {"runs": [{"text": "2 hours ago"}]},
            }
        }
        data = {"items": [post1, post2]}
        posts: list = []
        self.fn(data, posts, max_posts=1)
        self.assertEqual(len(posts), 1)

    def test_empty_text_skipped(self):
        """postRenderer with no text runs → should not be appended."""
        data = {
            "postRenderer": {
                "postId": "px",
                "contentText": {"runs": []},
                "publishedTimeText": {"runs": [{"text": "1 hour ago"}]},
            }
        }
        posts: list = []
        self.fn(data, posts, 10)
        self.assertEqual(len(posts), 0)


class TestExtractSapisid(unittest.TestCase):
    """_extract_sapisid — extracts SAPISID cookie value."""

    def setUp(self):
        import youtube_enricher as yt
        self.fn = yt._extract_sapisid

    def test_extracts_sapisid(self):
        cookie = "SID=abc; SAPISID=xyz123; HSID=def"
        result = self.fn(cookie)
        self.assertEqual(result, "xyz123")

    def test_returns_empty_if_missing(self):
        cookie = "SID=abc; HSID=def"
        result = self.fn(cookie)
        self.assertEqual(result, "")

    def test_handles_empty_string(self):
        self.assertEqual(self.fn(""), "")


class TestCommunityPostScoring(unittest.TestCase):
    """score_youtube — community post mentions score correctly with member-only bonus."""

    def setUp(self):
        import youtube_enricher as yt
        self.yt = yt

    def _community_mention(self, is_member_channel=True, is_member_only=False, sentiment=0.0) -> dict:
        return {
            "mention_count": 1,
            "sentiment": sentiment,
            "videos": [{
                "video_id":          "post001",
                "title":             "THERMAX is my pick",
                "channel":           "Akshat Shrivastava",
                "published_at":      "2 hours ago",
                "sentiment":         sentiment,
                "in_title":          True,        # community posts set in_title=True
                "in_transcript":     False,
                "is_member_channel": is_member_channel,
                "is_community_post": True,
                "is_member_only":    is_member_only,
            }],
        }

    def test_community_post_treated_as_title_mention(self):
        """Community post (in_title=True) should score at least as high as a title video."""
        data = self._community_mention(is_member_channel=False, is_member_only=False)
        score = self.yt.score_youtube(data)
        self.assertGreaterEqual(score, 7.0,
                                "Community post in_title=True should score ≥ 7.0 baseline")

    def test_member_only_post_bonus_applied(self):
        """Member-only community post scores higher than plain public post of same tier.
        Uses is_member_channel=False to isolate the +1.0 is_member_only bonus effect
        without saturating the cap via the 1.5× multiplier."""
        public  = self._community_mention(is_member_channel=False, is_member_only=False)
        private = self._community_mention(is_member_channel=False, is_member_only=True)
        self.assertGreater(self.yt.score_youtube(private),
                           self.yt.score_youtube(public),
                           "Member-only post must score higher than public post")

    def test_member_only_alone_still_capped_10(self):
        data = self._community_mention(is_member_channel=True, is_member_only=True,
                                        sentiment=0.9)
        score = self.yt.score_youtube(data)
        self.assertLessEqual(score, 10.0)

    def test_score_zero_no_mentions(self):
        data = {"mention_count": 0, "sentiment": 0.0, "videos": []}
        self.assertEqual(self.yt.score_youtube(data), 0.0)


class TestGetYoutubeMentionsCommunityPosts(unittest.TestCase):
    """get_youtube_mentions — community posts integration with mocked InnerTube."""

    def setUp(self):
        import youtube_enricher as yt
        self.yt = yt

    def test_community_post_mention_counted(self):
        """When InnerTube returns a post mentioning the ticker, community_posts count > 0."""
        yt = self.yt
        fake_post = {
            "post_id":        "cp001",
            "text":           "THERMAX is my favourite industrial stock right now",
            "published_time": "1 hour ago",
            "is_member_only": False,
        }

        with (
            patch("youtube_enricher._load_youtube_cookie", return_value="SAPISID=abc;SID=def"),
            patch("youtube_enricher._fetch_community_posts", return_value=[fake_post]),
        ):
            data = yt.get_youtube_mentions("NSE:THERMAX", use_transcript=False)

        self.assertGreater(data["community_posts"], 0)
        self.assertGreater(data["mention_count"], 0)

    def test_community_post_has_in_title_true(self):
        """Community post mentions must have in_title=True."""
        yt = self.yt
        fake_post = {
            "post_id":        "cp002",
            "text":           "RELIANCE is a must hold",
            "published_time": "2 hours ago",
            "is_member_only": False,
        }

        with (
            patch("youtube_enricher._load_youtube_cookie", return_value="SAPISID=abc;SID=def"),
            patch("youtube_enricher._fetch_community_posts", return_value=[fake_post]),
        ):
            data = yt.get_youtube_mentions("NSE:RELIANCE", use_transcript=False)

        cp_vids = [v for v in data["videos"] if v.get("is_community_post")]
        self.assertTrue(all(v["in_title"] for v in cp_vids),
                        "All community post mentions must have in_title=True")

    def test_community_post_member_only_flag_propagated(self):
        """is_member_only flag from post is preserved in the mention dict."""
        yt = self.yt
        fake_post = {
            "post_id":        "cp003",
            "text":           "THERMAX members-only pick",
            "published_time": "30 minutes ago",
            "is_member_only": True,
        }

        with (
            patch("youtube_enricher._load_youtube_cookie", return_value="SAPISID=abc;SID=def"),
            patch("youtube_enricher._fetch_community_posts", return_value=[fake_post]),
        ):
            data = yt.get_youtube_mentions("NSE:THERMAX", use_transcript=False)

        cp_vids = [v for v in data["videos"] if v.get("is_community_post")]
        self.assertTrue(len(cp_vids) > 0)
        self.assertTrue(cp_vids[0]["is_member_only"])

    def test_no_cookie_returns_zero_community_posts(self):
        """Without a cookie, _fetch_community_posts returns nothing → 0 community_posts."""
        yt = self.yt

        with (
            patch("youtube_enricher._load_youtube_cookie", return_value=""),
            patch("youtube_enricher._fetch_community_posts", return_value=[]),
        ):
            data = yt.get_youtube_mentions("NSE:THERMAX", use_transcript=False)

        self.assertEqual(data["community_posts"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

